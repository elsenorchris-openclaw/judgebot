# Push Window Overrides — Implementation Plan & Data Reference

**Living handoff doc for the push pure-nn override system in paper_judge_bot.**
Any session picking this up: read this first. Last updated 2026-05-21
(status patched 2026-05-23: HIGH early-trim is now OFF; the override-table windows
are superseded by per-station temp windows in May — see §0).

Bot lives on VPS `ubuntu@54.225.174.220:/home/ubuntu/paper_judge_bot/` (repo
`github.com/elsenorchris-openclaw/judgebot.git`, branch `main`). SSH first —
local copies are stale (CLAUDE.md RULE #0.7).

---

## 0. TL;DR status

| Piece | State |
|---|---|
| 18-dim conditional-MAE backtest (v3) | ✅ done (2000-2025, 3.17M rows) |
| `push_window_overrides.py` — 480/480 windows + bias + mae | ✅ SHIPPED `cc11a38` + `6aa2f51` |
| Windows live in bot | ✅ (`_in_decision_window` reads `ov[0]/ov[1]`) — **⚠️ NOTE (2026-05-22+):** in months ∈ `config.PUSH_TEMP_WINDOW_MONTHS` (currently `{5}`=May) the table window is SUPERSEDED by the per-station `PUSH_HIGH_TEMP_WINDOW_BY_STATION` / `PUSH_LOW_TEMP_WINDOW`; the table drives windows only OUTSIDE those months. The `mae` field is still used for sizing every month. |
| Out-of-sample validation of bias + MAE | ✅ done 2026-05-21 (see §4a) |
| ~~MEDIAN-bias applied to μ~~ | ⛔ REVERTED 2026-05-21 — flipped 2 MSP winners→losses on 5/20 (Kalshi-settled 16-6→14-8). `USE_PUSH_BIAS_CORRECTION=False`; bias still logged, not applied. |
| **MAE-based confidence sizing (cell-level)** | ✅ SHIPPED `USE_PUSH_MAE_SIZING` |
| **GLOBAL regime-MAE adjustment (anomaly/sigma/sky/wind/tspeak)** | ✅ SHIPPED 2026-05-21 `USE_PUSH_REGIME_MAE_ADJ` — sizing-only. **Per-side (HIGH/LOW) deltas @ `PUSH_REGIME_MAE_DAMP=1.0`** (HIGH/LOW respond oppositely; hot-anomaly HIGH −0.25 / LOW +1.46). corr 0.167→0.229(pooled)→0.250(per-side). Deltas in `data/regime_mae_deltas.json` (gitignored, per-side `{high|low:{dim:{bucket}}}`). |
| **HIGH early-side window trim (accurate-but-wide cells)** | ⚠️ SHIPPED 2026-05-21, **then DISABLED 2026-05-22 (`PUSH_EARLY_TRIM_HIGH_ENABLED=False`)** — turned off because the deep-pre-peak `PUSH_HIGH_TEMP_WINDOW` (before=3.0) would be capped by the trim; logic is intact and flag-reversible. Original mechanism: caps `before`→1.0 on the 40 HIGH cells with mae<1.6 AND before>1.0. Windows are MAE-built but accuracy≠PnL: early offsets mis-call the bracket. **2024-2025 holdout (n=12,548): offset<-1.25 → wrong bracket 60% / ≥2F miss 32% vs 46%/16% near peak; 38/40 cells worse early.** Live PnL (n=52) agreed (+$18.58→+$65.51). `after`/peak/inaccurate-wide/LOW untouched. Applied in `_in_decision_window`, not the table → survives regen. |
| **Empirical tail-loss correction (T brackets)** | ✅ SHIPPED 2026-05-23 `103270a` `USE_TAIL_EMPIRICAL_PYES=True`. Raises P(YES) of the fat-surprise tail (HIGH hot / LOW cold) on open-ended T brackets to the empirical floor (`_emp_tail_p` in `nn_shadow_strategy.py`); deflates overconfident deep-margin tail BUY_NO below the 12pp gate. Interior B untouched. See §12. |
| MIA-class interior over-projection fixes | ⛔ ALL REJECTED 2026-05-22/23 — see §12. Forecast-divergence clamp, boundary-fragility/σ haircut, global two-piece σ-recal. MIA 5/21 is irreducible variance. |
| MEAN-bias application | ⛔ REJECTED (−8.6% holdout — never ship) |
| Per-cell regime slicing (MAE) | ⛔ REJECTED — no better than cell-level (noise) |
| Low-confidence (37-cell) gating | ⛔ REJECTED — flag is noise (clean fwd test: flagged did BETTER) |
| 22 conditional WINDOW entries (per-cell) | ⏸ tested: 7/7 checkable HELD but tiny footprint; superseded by the global regime adjustment |

Bias field in the override file is now the **MEDIAN** residual (patched by
`tools/per_hour_quality/patch_median_bias.py` after the generator). Current bot
PID changes whenever restarted.

### §4a. Out-of-sample validation (2026-05-21) — READ BEFORE TOUCHING BIAS

Validated train(2000-23)→holdout(2024-25) on the `phq_raw_*.csv.gz` sidecars,
473 cells / 79,248 pre-peak in-window holdout decisions:

- **MEAN bias correction: −8.6% (WORSE).** Holdout MAE 2.19→2.38°F; 313 cells
  worse. ⛔ NEVER ship `mu -= mean_bias`. Root cause: error distribution is
  skewed — the mean is inflated by extreme cold-snap outliers, the typical
  (median) error is ~0, so subtracting the mean over-corrects. (Same lesson as
  the 2026-05-18 LOW bias work: MAE is minimized at the **median**, not mean.)
- **MEDIAN bias correction: +0.4% overall, +2.1% HIGH (159/235 cells), LOW
  neutral (−0.1%).** → SHIPPED HIGH-only. Modest but real and safe. The override
  file's bias field is the median; `_evaluate_ticker` applies it for HIGH only.
- **MAE sizing signal: VALIDATED.** corr(train_mae, holdout_mae)=0.62; monotonic
  tiers (uncorrected holdout MAE): train<1.0→1.32°F, 1.0-1.5→1.60, 1.5-2.5→1.78,
  ≥2.5→2.96. → SHIPPED: bet size scales by MAE tier (only reduces).

**Takeaway: the bias was NOT the big lever it looked like — it's marginal
(+2.1% HIGH). MAE-based sizing is the more useful validated signal.** Scripts:
`/tmp/validate_bias_mae.py`, `/tmp/validate_median_bias.py` on VPS.

---

## 1. Goal

The push pure-nn path (`nn_shadow_worker.py`) decides weather-bracket trades
from the kNN analog matcher (`nn_match_fast.predict()` via
`nn_shadow.shadow_nn_proj`). Two levers improve it, both derived from a
historical backtest of the matcher's own accuracy, conditioned on observable
regime:

1. **Window** `(before, after)` — *when* in the day (relative to the
   per-(station,month) fractional peak/min hour) the matcher is accurate enough
   to trade. Already consumed by `_in_decision_window`.
2. **Bias** — the matcher's *residual* systematic μ error in that cell
   (`mean(mu_proj − actual_extreme)`), to be subtracted before P(YES).
3. **MAE** — the matcher's *expected accuracy* in that cell, as a
   confidence/sizing signal.

All three are keyed per `(station, side, month)` cell, optionally refined by
regime bucket.

---

## 2. The v3 backtest

**What:** for every (station, side, month, offset) and every combination of 18
observable conditioning dimensions, measure the matcher's pre-peak MAE and bias.
Offsets are relative to the **fractional** per-(station,side,month-day) peak/min
hour (`peak_fractional_5yr_10day.json`), span `[-4.0h, +1.0h]` in 0.5h steps.

**Data:** `heating_traces.sqlite` — 1-min ASOS traces, 2000-2025, ~8,000-9,000
days/station × 20 stations. `predict()` is called with the bot's live config
(k=50, gates, bias-corr, sigma factors) so the measured μ == what the bot
produces. 3,166,141 evaluation rows total. Train = years <2024, holdout
= 2024-2025.

**18 conditioning dimensions** (bucket thresholds in `per_hour_quality_v3.py`):

| Dim | Source | Buckets |
|---|---|---|
| slope | 60-min temp regression | rising>0.5 / flat / falling<-0.5 (°F/h) |
| vol | 60-min temp range | stable<1 / moderate / variable>3 (°F) |
| dewpoint | dwpf at cur | low<40 / mid / high>60 (°F) |
| pres | 60-min **alti** tendency | rising>0.9 / steady / falling<-0.9 (centi-inHg/h) |
| sky | skyc1 at cur | clear(CLR/SKC/FEW) / partly(SCT) / cloudy(BKN/OVC/VV) |
| sigma | predict sigma_natural_f | low<1.5 / mid / high>2.5 (°F) |
| nnbr | predict n_neighbors_used | thin<20 / moderate / thick>40 |
| lock | predict extreme_locked | locked / unlocked |
| clamp | predict peak_clamp_tier (HIGH) | none / tier1 / tier2 / na(LOW) |
| anom | cur_tmpf − climate-normal(md,bin) | cold<-5 / normal / hot>+5 (°F) |
| wind | sknt at cur | calm<5 / moderate / strong>15 (kt) |
| relh | relh at cur | dry<40 / moist / humid>70 (%) |
| accel | Δslope over 30-min halves | decel<-0.5 / steady / accel>0.5 |
| yanom | yesterday day_max − its normal | cold / normal / hot |
| mmdiv | mean_delta − median_delta | neg_skew<-0.3 / symmetric / pos_skew>0.3 |
| psize | predict pool_size | small<60 / medium / large>150 |
| tspeak | mins since traj extreme | not_yet / fresh<30 / recent<120 / stale |
| dpdep | cur_tmpf − dewpoint | tight<5 / moderate / dry>15 (°F) |

Missing data → `"unknown"`/`"missing"` token per dim (graceful degradation —
a sparse variable opts out of its own dim only, never drops the row). Only a
usable temp trajectory (≥4 pts) is required.

**Tooling** (`/home/ubuntu/tools/per_hour_quality/`):
- `per_hour_quality_v3.py <STATION> <outdir> <max_days>` — the backtest. Writes
  `phq_<ST>.csv` (bucketed MAE/bias) + `phq_raw_<ST>.csv.gz` (per-decision raw
  values sidecar, for re-bucketing without re-running).
- `aggregate_phq_v3.py` — streams per-station CSVs → `phq_offset_cond_combined.csv`.
- `build_overrides_hierarchical.py` — generates `push_window_overrides.py`.
- Launcher: `/tmp/launch_only_v3.sh` (parallel=2, ~8-9h full run).

**Runtime:** ~8-9h full (parallel=2, predict() is the bottleneck — ~2-3 days/sec
under load). Generator is per-station streaming (~180MB peak; do NOT load the
620MB combined CSV as dicts — it OOMs the 8GB box).

---

## 3. Override file format & coverage

`/home/ubuntu/paper_judge_bot/push_window_overrides.py` defines:

```python
PUSH_WINDOW_OVERRIDES: dict = {
    # unconditional (every cell): keyed (station, side, month)
    ('KATL', 'HIGH', 1): (1.5, 0.0, -0.371, 1.856),   # (before, after, bias, mae)
    # conditional (regime refinements, 22): keyed (station, side, month, gran_tag, bucket[, bucket2])
    ('KATL', 'HIGH', 8, 'mmdiv', 'neg_skew'): (1.5, -0.5, -0.33, 0.471),
    ...
}
PUSH_WINDOW_LOW_CONFIDENCE: set = {  # 37 holdout-degraded cells
    ('KATL', 'LOW', 3), ...
}
```

- **Tuple = `(before, after, bias, mae)`.** `before`/`after` = hours
  before/after the peak/min (window = `[peak−before, peak+after]`). `bias` =
  μ correction (subtract from μ). `mae` = expected pre-peak accuracy (°F);
  `None` for fallback cells.
- **Coverage: 480/480** (20 stations × 12 months × 2 sides). 480 unconditional
  + 22 conditional = 502 entries.
- Conditional keys carry a `granularity_tag` (e.g. `anom`, `sig`, `mmdiv`, or a
  2D combo like `sky_anom`) + the bucket value(s). They ship only when that
  regime's MAE < 0.7°F (ultra-predictable). 1-letter combo codes: s=slope,
  v=vol, d=dew, p=pres, k=sky, g=sig, n=nnbr, l=lock, c=clamp, a=anom, w=wind,
  r=relh, x=accel, y=yanom, m=mmdiv, z=psize, t=tspeak, e=dpdep.
- The lookup handles legacy 2-/3-tuples gracefully (`bias`/`mae` → None).

---

## 4. Key results

**Granularity distribution** (which dim won per cell, Stage 2):
```
anom 109,  unconditional 109,  sig 55,  relh 34,  sky_anom 27,  sig_anom 25,
yanom 24,  mmdiv 16,  dew 15,  pres_anom 12,  dpdep 10,  mmdiv_sig 9,
dew_anom 8,  wind 6,  tspeak 5,  sky 5,  dpdep_sky 4,  accel 3,  pres 2,
vol 1,  slope 1
```
**Anomaly-from-normal dominates** — 109 direct + 72 in combos (sky_anom,
sig_anom, pres_anom, dew_anom) = **~181 cells (38%)**. Matcher self-uncertainty
(sigma) is #2 (~89 incl. combos). Humidity (relh) #3. **The classic
trajectory-shape dims the project started with — slope (1), vol (1), pres (2) —
are nearly worthless.** This is why the expanded 18-dim scope mattered.

**MAE distribution** (unconditional cells, °F): min 0.708, p10 0.996,
**p50 1.581**, p90 3.033, max 4.338. Wide spread = strong sizing signal (some
cells predict 3× more accurately than others). Conditional entries carry lower
MAE (their regime is the ultra-predictable subset).

**Bias distribution** (°F): min −1.69, **p50 +0.19**, max +3.04. LOW winter
months carry large positive bias = matcher systematically over-projects
cold-night lows. For ~2°F brackets a 1-2°F bias correction can flip which
bracket μ lands in — this is the biggest expected PnL lever.

---

## 5. Bot decision flow & integration points

`nn_shadow_worker._evaluate_ticker(ticker)`:
```
_build_shadow_packet(cand)                       # pkt: wethr_obs, obs_trend_60m,
                                                 #   temp_history_range_60m, local_clock
pkt["push_override"] = _lookup_push_override(...)  # ← SHIPPED: {before,after,bias,mae,src}
nn_shadow.shadow_nn_proj(pkt)  → nn_res          # calls predict()
pkt["mu_chosen"] = nn_res["mu"]                  # ~line 913  ← BIAS INJECTION POINT
pkt["sigma_chosen"] = nn_res["sigma"]
pure_nn_decide(pkt)  → decision                  # ~line 945: computes edge + p_yes FROM mu_chosen
_try_auto_execute(cand, pkt, decision, ...)      # gates: edge, window, h2pk, Tier1, price, caps
_log_shadow({... "push_override": pkt["push_override"] ...})  # ← SHIPPED: logged every decision
```

- **Bias application (Phase 2):** between line ~913 and ~945, do
  `pkt["mu_chosen"] -= bias` (from `pkt["push_override"]["bias"]`) so edge/p_yes
  reflect corrected μ. Gate behind a new `USE_PUSH_BIAS_CORRECTION` flag.
- `_in_decision_window` is the window gate (unchanged; reads `ov[0]/ov[1]`).
- `_lookup_push_override` (read-only) surfaces the matched entry for logging.

---

## 6. Runtime data-availability map (for Phase 2 bucketing)

To use *conditional* (regime-specific) overrides, the bot must compute its
current bucket per dim from live data:

| Bucket | Runtime source | Status |
|---|---|---|
| slope, vol | `pkt["obs_trend_60m_regression"]`, `["temp_history_range_60m"]` | ✅ in packet |
| dewpoint, dpdep, wind, relh | `pkt["wethr_obs"]` | ✅ (wind: kt→mph convert) |
| sky | `wethr_obs.cloud_1_coverage` → clear/partly/cloudy | ✅ maps to skyc1 |
| nnbr, psize, lock, sigma(proj), sigma_raw, median_delta | `nn_shadow.shadow_nn_proj` return | ✅ already exposed |
| **sigma_natural, mean_delta, peak_clamp_tier** | in `predict()` `res`, NOT passed through | ⚠️ add 3 lines to shadow_nn_proj |
| pres | `wethr_obs.pressure_tendency` (verify units/window vs alti) | ⚠️ verify |
| accel, tspeak | need temp bin-trajectory (only have regression) | ⚠️ approximate |
| **anom, yest_anomaly** | existing `climate_normals` is monthly only; backtest used (month-day, hour-bin) | ⚠️⚠️ **need new normals table** (export from backtest `precompute_climate_normals`) |

Note: 329 of the value is in the **unconditional** overrides which need NO
bucketing. Conditional refinement (22 cells) + regime-MAE-for-sizing is what
needs the above.

---

## 7. SHIPPED (Phase 1)

- **`cc11a38`** — full 480/480 coverage `(before, after, bias)`. Removed the
  MAE≥0.7 deletion gate; relaxed bounds to physical-sanity; width-collapsed
  cells widened on own offset; holdout demoted veto→`PUSH_WINDOW_LOW_CONFIDENCE`
  flag (37); neighbor/season fallback (0 cells today).
- **`6aa2f51`** — added `mae` 4th element; `_lookup_push_override` helper;
  `pkt["push_override"]` stamped + logged into every `shadow_nn_strategy.jsonl`
  record. Observability only — mae/bias NOT applied to trades. +7 lookup tests,
  fixed stale test. 370 pass.
- Related (separate): **`7f92cb6`** — Tier 1 runtime gates
  (`PUSH_MIN_VSBY_MI=0.5`, `PUSH_MAX_WIND_MPH=40`) in `_try_auto_execute`.

Backups on VPS: `push_window_overrides.py.pre_fullcoverage_20260521`,
`.pre_mae_20260521`; `nn_shadow_worker.py.pre_mae_logging_20260521`.

---

## 8. PENDING (Phase 2) — exact steps

1. **Validate MAE→outcome first** (collect-then-validate; do NOT size blind).
   Once settled push trades accumulate tagged with `push_override.mae` in
   `shadow_nn_strategy.jsonl`, check: do low-MAE cells settle more accurately /
   win more? (See §9.) Only then design the sizing curve.
2. **Apply bias** — `pkt["mu_chosen"] -= bias` in `_evaluate_ticker` (~line 913),
   behind `USE_PUSH_BIAS_CORRECTION`. Backtest/replay against settled trades
   first if possible. Bias is the largest expected lever.
3. **MAE-based sizing** — once validated, scale bet (or edge floor) by MAE
   (e.g. bet ∝ 1/MAE, or MAE-tiered). Wire into the sizing in `_evaluate_ticker`
   / `pure_nn_decide`.
4. **Conditional entries (22) + regime MAE** — add runtime bucketing:
   (a) export `(station, month-day, hour-bin)` climate-normal table from
   `precompute_climate_normals`, ship + load in bot for anom/yanom;
   (b) expose `sigma_natural_f`, `mean_delta_f`, `peak_clamp_tier` from
   `shadow_nn_proj`; (c) compute the 18 buckets at decision time;
   (d) multi-granularity lookup (most-specific key → marginal → unconditional).
5. **Low-confidence handling** — for the 37 `PUSH_WINDOW_LOW_CONFIDENCE` cells,
   trade more conservatively (higher edge floor or smaller size).

---

## 9. Design decisions & rationale (don't relitigate without reason)

- **0.7°F gate governs CONDITIONAL entries only, not the base window.** Every
  cell ships its own window+bias regardless of MAE — a cell's own data-driven
  window always beats the hand-picked global default (2.5h/1.5h).
- **A cell's OWN estimate beats borrowing.** Width-collapsed cells are widened
  around their own best offset, not neighbor-averaged. Neighbor→season→
  cross-station→default fallback exists but only for cells with NO own window
  (0 today).
- **Holdout = confidence flag, not veto.** A window failing cross-year (2024-25
  MAE >1.5× train) is NOT dropped — the test only says accuracy is less stable
  out-of-sample (could be overfit OR the cell got harder OR holdout noise on
  ~55 days). Keep the window, flag it.
- **MAE/bias: collect-then-validate.** Logged now, applied only after we confirm
  the relationship on settled trades.
- **No "shadow then wait N days"** (CLAUDE.md RULE #0). The above is enriching
  an existing log per explicit request, not a new logging system or wait period.

---

## 10. File / path inventory

| Thing | Path (VPS) |
|---|---|
| Override file (live) | `~/paper_judge_bot/push_window_overrides.py` |
| Decision/candidate log | `~/paper_judge_bot/data/shadow_nn_strategy.jsonl` |
| Bot worker | `~/paper_judge_bot/nn_shadow_worker.py` |
| Matcher adapter | `~/paper_judge_bot/nn_shadow.py` (`shadow_nn_proj`) |
| Matcher core | `~/paper_judge_bot/nn_match_fast.py` (`predict`) |
| Backtest | `~/tools/per_hour_quality/per_hour_quality_v3.py` |
| Aggregator | `~/tools/per_hour_quality/aggregate_phq_v3.py` |
| Generator | `~/tools/per_hour_quality/build_overrides_hierarchical.py` |
| Trace DB | `~/data/heating_traces.sqlite` |
| Fractional peaks | `~/data/peak_fractional_5yr_10day.json` |
| Backtest CSVs + sidecars | `~/data/per_hour_quality_offset_cond/phq_<ST>.csv`, `phq_raw_<ST>.csv.gz` |
| Combined CSV | `~/data/phq_offset_cond_combined.csv` |

---

## 11. Maintenance

- **Bias/MAE are coupled to the matcher config at backtest time.** If anyone
  changes `NN_BIAS_CORR_*`, sigma factors, k, aggregators, peak-clamp, or the
  fit gate, the override bias/mae go stale — regenerate:
  `python3 ~/tools/per_hour_quality/build_overrides_hierarchical.py > ~/paper_judge_bot/push_window_overrides.py`
  (then restart + commit + push). The backtest itself (~9h) only needs re-running
  if `predict()`'s μ changes; pure re-bucketing (e.g. new thresholds) can use the
  `phq_raw_*.csv.gz` sidecars without re-running predict.
- Regenerating reads per-station CSVs — keep `~/data/per_hour_quality_offset_cond/`.

---

## 12. MIA 5/21 loss investigation + σ-tail calibration (2026-05-22/23)

Triggered by the MIA `KXHIGHMIA-26MAY21-B89.5` BUY_NO loss (μ=91.8 vs actual ~89).
Three "prevention" hypotheses were built + backtested and **all REJECTED**; one
adjacent real leak was found and SHIPPED. **Do not relitigate the rejected ones.**

**Verdict on MIA itself: irreducible variance.** It was an interior B-bracket,
a +EV bet (P(NO)=0.79 @ 45¢) that lost its tail. Calibration shows interior
brackets are well-calibrated (HIGH 0.046 model vs 0.042 real). No signal
separates it from winners; gating it = refusing +EV bets.

Rejected fixes (data: `phq_ext/phq_raw_*.csv.gz` sidecars — matcher μ/σ/actual,
**Nov-2024→May-2026 only, no summer**; forecasts from `bot_decisions.sqlite`,
`analog_mu` there is always NULL):
1. **Forecast-divergence clamp** (cap μ at NWP consensus+buffer). REJECTED: the
   matcher *beats* forecast (MAE 1.13 vs 1.83); large matcher>forecast divergence
   usually means the matcher is RIGHT (e.g. OKC 5/19 same +7.4 div, dead-on).
   Clamp hurts>helps at every buffer.
2. **Boundary-fragility / σ haircut.** REJECTED: HIGH over-projection tail
   (MIA's direction) is *thin* (0.72–0.85× Gaussian at 1–1.5σ).
3. **Global two-piece-normal σ recal.** REJECTED: worsened interior-B Brier;
   the earlier "edges overstated 5–7pp" was a measurement error (conflated tail
   probability with bracket-occupancy — a cold snap that blows *past* an interior
   bracket WINS the BUY_NO).

**SHIPPED — empirical tail-loss correction (T brackets only).** The conflation
above pointed to the correct, narrower target: open-ended **T** brackets, where
the loss IS the whole fat tail. There the matcher's Gaussian P(YES) genuinely
under-states it. Confirmed on the live rm-conditioned `_p_yes_constrained` path
(conditioning barely helps, ×1.04 HIGH / ×1.09 LOW): realized loss vs model —
**1.5σ 1.2×(H)/1.7×(L), 2σ 2.0×/3.7×, 2.5σ 4.8×/9.8×**; cross-station stable
(grpA≈grpB). Fix in `nn_shadow_strategy.pure_nn_decide`: for fat-direction T
(HIGH T-warm / LOW T-cold) raise P(YES) to `_emp_tail_p(is_high, m)` via `max()`
(never lowers; capped at the 2.5σ empirical value). Footprint: ~14% of BUY
decisions are T; ~5% fat-direction; replay blocks ~13/day overconfident tail
BUY_NO, edge deflation ~5–7pp. Flag `USE_TAIL_EMPIRICAL_PYES`; revert = False +
restart. **Caveat:** validated vs *actual* (not directly vs market — settled
pure-nn n too small); rests on the v1 "market is well-calibrated" verdict that
overconfident edges are false edges. No summer data.

## 13. Thin-margin HIGH BUY_NO gate (2026-05-23) — SHIPPED

**Question (Chris):** does WHERE matcher μ sits relative to a bracket predict
profit (μ=89.3 in the 88-89 bracket vs μ=88.5)?

**Finding.** Within-bracket position is washed out for BUY_YES (matcher's ±2-3°F
bracket-level error swamps the 1°F position). On the **BUY_NO** side it is strongly
predictive: a HIGH B-bracket BUY_NO where μ sits **inside / barely outside** the
bracket it shorts is a robust live-era loser. Faithful gated buy-at-open replay
(2026-03-15..05-19, production windows, outcome = `market_meta.result` =
CLI-authoritative): μ inside the shorted bracket → **WR 32%, −3.9c/bet**; the effect
is **edge-INDEPENDENT** (still −8.6c holding model edge fixed in [12,20]pp) and
negative in BOTH date-halves → the model's NO "edge" is illusory near the boundary
(market out-calibrates it, §9 / v1 verdict). THIN-boundary complement of §12 (which
trims the deep-SAFE T-tail). Nearly orthogonal to the (2t) in-bracket tail-bet gate
(only catches the rare p_yes>0.5 case — modelled in the backtest it moves 2 bets).

**CLI rounding/offset (Chris's follow-up).** The ±0.5°F bracket band + authoritative
settlement were already handled → not a rounding artifact. Decomp: our obs runs
**+0.5°F hot** vs CLI, the matcher **undershoots obs −0.2°F**, partly cancelling →
net **μ→CLI offset ≈ +0.46°F median** (per-station ~0 at SAT/LAX/DFW, ~+0.9 at
BOS/MIA/MSP). The HIGH BUY_NO WR-50% crossing sits ~+0.5°F *outside* the edge, matching
the offset → the gate uses **(μ − offset)** inside-test, not raw μ.

**SHIPPED.** `nn_shadow_worker._try_auto_execute` gate **(2d)**: HIGH B-bracket BUY_NO
skipped when `(μ − offset[station]) ∈ [floor−0.5, cap+0.5]`. offset = per-station
median(μ − yes_bracket_center) over the live era (`PUSH_NO_MU_CLI_OFFSET_BY_STATION`,
`PUSH_NO_MU_CLI_OFFSET_DEFAULT`=+0.5 for unlisted incl. KDCA). Flag
`PUSH_SKIP_NO_MU_NEAR_BRACKET` (revert = False + restart). Validation (faithful
one-bet/day, prod windows): **+4.1→+7.6c/bet, +$7.9 incremental** over the shipped
bot, WR 46→54%, ~28% fewer bets, both OOS halves + (early +$13.0→+$19.4, late
+$10.7→+$12.2); per-station helps 12 / hurts 6 (only SFO notably worse — a pre-existing
net loser). Tests `tests/test_thin_margin_gate.py` (7). **Caveats:** only SKIPS (never
shifts p_yes → distinct from the REVERTED median-bias correction, §9); offset is
per-station pooled (per-station-month when more data exists; ~9 cells show early/late
drift but the gate is robust to ±0.5°F offset noise); HIGH only (LOW flips sign);
validated vs settlement (pure-nn settled n still small), rests on the v1 verdict.
