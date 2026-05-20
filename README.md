# paper_judge_bot — Claude-as-Trader Weather Bot

A judgment-first Kalshi trading bot for daily weather markets. Claude is the
entry+exit decision-maker; deterministic guardrails wrap the LLM so the
worst-case blast radius is bounded by code, not by prompt quality.

## BUY_YES floor 25c→30c, max ask 90c→80c — 2026-05-20 06:26 UTC

Two-knob defensive change after cross-session replay on May 19 candidates showed every config (current + all proposals tested) was losing on settled-only data. This is the LEAST-LOSING config of those tested — not expected to be profitable, just less unprofitable while the underlying signal layer (nn_match) is investigated for the recent hot-everywhere regime.

  config.py
    PUSH_MIN_ENTRY_C_BUY_YES:  25 -> 30
    PUSH_MAX_ENTRY_C:          90 -> 80

May 19 replay under corrected logic (retry-every-event dedup + integer peaks + settle-when-available + 24 settlements as of ship time):

  config                              settled n   PnL       ROI
  Current shipped (yes=25, max=90)      8 of 41   -$15.70   -59%
  This tweak  (yes=30, max=80)          6 of 35   -$8.18    -43%   <-- shipped
  Other configs tested                  6-12      -$18..-$37   -62..-96%

Mechanism:
- max_c=80 removes asymmetric high-price BUY_NO entries (82c+ has no spread-of-survival room on a reversal). Specific catch: KXLOWTMIA-B77.5 NO entry 82c -> settle 42c = -$2.40 on May 19.
- yes_min=30 raises BUY_YES floor +5c. Filters two known marginal losers from May 19 (TLV-B82.5 @29c -$4.76, MIA-B88.5 @31c -$3.52) plus the cheap-YES lottery cohort more broadly.

This is the proposal from a separate replay session. My own session's analysis reached a different conclusion which DID NOT survive when settled-only ground truth was used and when the integer-peak/retry-dedup logic was corrected. Both sessions had bugs; the conservative recommendation won.

Caveat: signal-layer (nn_match mu accuracy) appears miscalibrated for the current regime. Filter tweaks rearrange losses but do not eliminate them. Pending diagnostic: per-station mu vs actual closing high on May 19, regime detection on hot-everywhere days, related to existing memory entries on sigma undercal and h2pk catastrophic at-peak.

Files: config.py:514-515. Tests: 332 pass / 4 skip (unchanged).

PID: 1232475 single+current. Verified via RULE #3c snippet immediately post-restart.

Rollback: `cp config.py.pre_tinytweak_20260520_055500 config.py` and restart.


## Push edge_pp execution floor 6pp → 12pp — 2026-05-20 01:19 UTC

`nn_shadow_worker._try_auto_execute` now refuses to fire when
`decision.edge * 100 < PUSH_MIN_EDGE_PP` (default 12). `pure_nn_decide`'s
internal floor stays at 6pp so the shadow log keeps capturing 6-12pp
candidates for post-hoc analysis.

Backtest n=196 (166 settled May 14-19 LLM-mode + 30 pure-nn from 2026-05-19
treated as proxy-settled via current bid):

  edge_pp >=  6pp   (current)  n=24 wr=20.8%  ROI= -0.0%   pure-nn cohort
  edge_pp >= 12pp              n=21 wr=19.0%  ROI= +0.8%
  edge_pp >= 15pp              n=18 wr=22.2%  ROI= +3.3%
  edge_pp >= 20pp              n=15 wr=26.7%  ROI= +6.0%

Sweep is monotonic (every threshold improves vs prior). 12pp picked as
conservative move that preserves ~70% of volume. The yesterday-was-bad
margin_sig filter idea was REJECTED — non-monotonic across thresholds
(>=0.25 ROI +5.8% on HIGH-only, >=0.50 collapses to -55.1%), classic
sample-noise signature. Halving the override windows was also REJECTED —
it rejects 29 of 30 yesterday's entries (effectively turns the bot off).

Files: config.py adds `PUSH_MIN_EDGE_PP: int = 12`. nn_shadow_worker.py
adds gate-2 in `_try_auto_execute` (returns "edge_below_floor Xpp < 12pp").

Tests: 350 passed / 4 skipped / 1 pre-existing fail (unchanged).

Rollback: set `PUSH_MIN_EDGE_PP = 6` in config.py and restart, or
`cp config.py.pre_edge_floor_20260520 config.py` and same for nn_shadow_worker.py.


## Push pure-code entry window tightened — 2026-05-19 18:58 UTC

Tightened the per-(station, series) decision window in `nn_shadow_worker._in_decision_window`:

  config.py
    PUSH_PEAK_HOURS_BEFORE:     2.5  -> 1.0
    PUSH_PEAK_HOURS_AFTER_HIGH: 1.5  -> 0.5
    PUSH_PEAK_HOURS_AFTER_LOW:  0.5  (unchanged)
    PUSH_PEAK_HOURS_AFTER:      1.5  -> 0.5   # deprecated compat read

Window is now [peak-1.0h, peak+0.5h] for BOTH HIGH and LOW (was [peak-2.5h,
peak+1.5h] HIGH and [peak-2.5h, peak+0.5h] LOW).

Driver: 23 post-hotfix BUYs on 5/19 had mean h_to_peak = -2.15h (median -2.48,
0/23 inside +/-30min). Position-cap-of-1 + ASAP-fire-at-window-open pinned every
entry at the far-left edge of the old window, exactly where backtest shows the
worst edge (50% wr in the [-2.5, -1.5h] bucket).

Backtest n=157 settled BUYs 5/14-5/19 (LLM-era + push-era):

  Window                  n   wr   pnl/$   sum$
  [-2.5, +1.5h] (old)    83  61%  +1.04   +$544
  [-1.0, +0.5h] (NEW)    32  75%  +1.66   +$372
  [-0.5, +0.5h]          13  85%  +2.05   +$85

BUY_NO in shipped window: n=24, 88% wr, +$383.
BUY_YES in shipped window: n=8, 38% wr, -$11 (bleeds in every window — open
follow-up, separate ship).

LOO-by-date holds 64-79% wr across all 6 days.

Verification: post-restart shadow log shows window debug strings
`outside_window KMDW/HIGH/NO: peak=13 window=[12.0,13.5]` etc., confirming
new bounds active. 22/23 of today's pre-restart BUYs would have been blocked
under the new window.

Tooling: `/home/ubuntu/tools/window_backtest_20260519/backtest.py` — h_to_peak
vs settled PnL, window sweep, LOO. Reusable for future window/edge analyses.

Backup: `config.py.bak.pre_window_tighten_20260519_185624`.

## Per-(station, series, month) push window overrides — 2026-05-19 19:43 UTC

Followup to the 18:58 global-window tightening above. An 800-day backtest
showed substantial city + month variance in optimal entry windows:
e.g. ATL HIGH May best window is [13.0, 15.0] LST (peak −2.62 / peak −0.62),
whereas SEA HIGH May is [14.0, 16.0] LST (peak −1.90 / peak +0.10). A single
global setting can't capture this — overrides do.

New behavior:
  config.py
    USE_PUSH_WINDOW_OVERRIDES: bool = True    # new flag

  nn_shadow_worker._in_decision_window(...)
    if USE_PUSH_WINDOW_OVERRIDES and (station, series, month) in PUSH_WINDOW_OVERRIDES:
        before, after = PUSH_WINDOW_OVERRIDES[(station, series, month)]
        # src=override in debug string
    else:
        # fall back to PUSH_PEAK_HOURS_BEFORE / AFTER_<HIGH|LOW>
        # src=global in debug string

`push_window_overrides.py` (new, 394 entries) defines:

    PUSH_WINDOW_OVERRIDES: dict[tuple[str, str, int], tuple[float, float]] = {
        ('KATL', 'HIGH',  1): (2.65, -0.65),
        ('KATL', 'HIGH',  2): (1.03, -0.03),
        ...
    }

Each value is `(BEFORE_h, AFTER_h)`; window opens at `peak − BEFORE`, closes at
`peak + AFTER`.

Generation pipeline:

    /home/ubuntu/tools/per_hour_quality/per_hour_quality.py STATION [out_dir] [max_days]
      runs nn_match.predict() at each LST hour x station-day x side; outputs
      per-(station, month, hour, side) MAE/firing/settled grid CSV.
    /home/ubuntu/tools/per_hour_quality/aggregate_phq.py
      reads per-station CSVs + peak_dist.csv -> phq_combined.csv (6480 rows).
    /home/ubuntu/tools/per_hour_quality/build_overrides.py
      derives tight_win per (station, month, side); applies quality gates
      (HIGH before in [0.5, 4.5], LOW before in [0.5, 5.5], after in
       [-2.0/-2.5, 1.0/1.5], width >= 1h) -> 394 valid cells emitted.

Cells outside the quality envelope fall back to global. 86 cells in the
fallback set: 84 out-of-bounds (matcher's tight_win lands too far from
peak — single-hour artifact in low-N data) and 2 have no qualifying window
(KDCA LOW Feb, KDEN LOW May — overnight ASOS gaps).

Coverage source: 5000 most-recent station-days per station (~13 years).
Regenerated 2026-05-20 00:34 UTC after the full sweep at
`/home/ubuntu/data/per_hour_quality_full/`. Coverage went from 394/480
(800-day) to 424/480 (5000-day) = 88%. The 800-day override is preserved
at `push_window_overrides.py.800day` for diff; the active map is
`push_window_overrides.py`. To regen from new data: re-run
`aggregate_phq.py` then `build_overrides.py > push_window_overrides.py`
and restart.

Verification (post-restart 2026-05-20 00:34 UTC, PID 627424):
  ATL HIGH May 14:00 LST -> src=override window=[13.0,15.0] (inside) ✓
  ATL HIGH May 15:30 LST -> src=override (outside) ✓
  KDEN LOW May was 800-day fallback, now has override (3.93, -0.93) ✓

Tests: 350 passed (was 344, +6 TestPushWindowOverrides; 2 of those 6
rewritten to monkey-patch PUSH_WINDOW_OVERRIDES so they're robust to
future dict regens) / 4 skipped / 1 pre-existing fail
(test_truncation_reduces_buy_no_edge_when_rm_in_yes, unrelated).

Rollback:
  Set USE_PUSH_WINDOW_OVERRIDES=False in config.py + restart, or
  cp config.py.pre_window_overrides_20260519 config.py
  cp nn_shadow_worker.py.pre_window_overrides_20260519 nn_shadow_worker.py
  sudo systemctl restart paper-judge-bot.service

Backups: `config.py.pre_window_overrides_20260519`,
`nn_shadow_worker.py.pre_window_overrides_20260519`.

## NN_MATCH two-tier peak clamp — 2026-05-19 07:42 UTC

`nn_match_fast.predict()` HIGH path now applies a two-tier cap on `mu_proj`,
gated by past per-(station, month) P50 historical peak time (built lazily at
module init from `/home/ubuntu/data/heating_traces.sqlite`, ~240 entries).

  Tier 1 — POST-PEAK (tight cap, high confidence):
    cur_lst_min ≥ P50_peak_time[station, month]
    AND traj_max occurred ≥ 30 min ago
    AND max temp in last 30 min < traj_max − 0.5°F  (drop confirmed)
    → cap mu_proj at traj_max + 0.75°F

  Tier 2 — AT-PEAK (loose cap, medium confidence):
    cur_lst_min ≥ P50_peak_time[station, month]
    AND cur_tmpf ≥ traj_max − 1.0°F  (temp at/near observed peak)
    → cap mu_proj at traj_max + 1.0°F

When both fire (post-peak AND temp still near max), the lower cap wins (tier
1's tighter +0.75°F). The existing physical floor `mu_proj ≥ traj_max` is
preserved. Diagnostic field `peak_clamp_tier` in result: `"post_peak"`,
`"at_peak"`, or `None`. Method label suffixed: `nn_match_high_pkclamp_post_peak`.

Cross-year backtest 2024-25 + 2023 hold-out (n≈23k eval rows / 20 stations,
38-strategy full grid sweep on tier1 × tier2 × at_peak_band):
  overall MAE   -14.5% / -12.7%
  at_peak ±30   -25.0% / -26.3%  ("buy at peak" failure mode)
  post_peak >90 -33.4% / -29.5%  (mid-afternoon "killer-window" failure mode)
  pre_peak >60m  +2.5% /  +3.3%  (acceptable damage on the worst-anyway cohort)
All 20 stations have positive overall lift in BOTH years.

Per-station overrides tested + rejected: gap < 0.02°F vs global, gap-noise on
6/20 stations (best alternative flips between years).

Config flags (in `config.py`, rollback by setting `_ENABLED=False`):
  NN_HIGH_PEAK_CLAMP_ENABLED:  bool = True
  NN_HIGH_POST_PEAK_MARGIN_F:  float = 0.75
  NN_HIGH_AT_PEAK_MARGIN_F:    float = 1.0
  NN_HIGH_AT_PEAK_TEMP_BAND_F: float = 1.0

See `tests/test_nn_calibration.py::TestPeakClamp` for 8 unit tests covering
both tiers, disabled flag, K-prefix station handling, and edge cases.


Lives on EC2 `54.225.174.220` alongside the other 4 bots (V1/V2 max/min),
and reuses the shared S3 forecast cache, NWS obs sources, Kalshi API
plumbing, and `bot_decisions.sqlite` audit DB.

## NN_MATCH B-Gate-21 (LOW locked floor) — 2026-05-19

Floors the LOW locked branch in `nn_match_fast.predict()` at 21:00 LST
(was effectively 12:00). Old rule trusted an 11-hour-stale morning trough at
6 PM, but evening cooling routinely drove actual day_min 3-16°F BELOW traj_min
on cold/dry/clear nights — especially Nov-Feb on continental stations.

### What changes
- `config.py` — new flag `NN_LOCK_FLOOR_LST_MIN: int = 21 * 60` (9 PM LST).
- `nn_match_fast.py` (`predict()`, LOW branch): condition for `extreme_locked=True`
  changes from `cur_lst_min >= 12*60` to `cur_lst_min >= _lock_floor` read from
  config (default `21*60`).
- Cells that previously locked at hours 12-20 now fall through to the
  `gate_low_postnoon_unlocked` branch (already in production) → returns
  `mu_proj_f=None` → bot receives no nn_match μ and `skip_unless_nn_match`
  blocks the trade at the prescreen layer.

### Why
Cross-year backtest on existing nn_agg_sweep data (sweep 2024-25 n=2501 LOW
cells, holdout 2023 n=2649 LOW cells):

| variant | LOW MAE_ho | LOW CRPS_ho | ΔCRPS |
|---|---|---|---|
| V0 production (no floor) | 1.878 | 1.405 | — |
| **V1 B-Gate-21 (cur < 21*60)** | **1.850** | **1.373** | **−2.3%** |
| V2 B-Gate-20 (cur < 20*60) | 1.848 | 1.376 | −2.1% |
| V4 per-hour bias (no gate) | 1.872 | 1.395 | −0.7% |

Per-hour locked median bias cross-year stability (identical fits):

| hour | bias_sweep | bias_holdout | drift |
|---|---|---|---|
| 18 | +3.00 | +3.00 | 0.000 |
| 20 | +2.00 | +2.00 | 0.000 |
| 22 | +2.00 | +2.00 | 0.000 |

Hour-18 gated cells alone (n=257 holdout) had MAE 2.146, bias +1.66, RMSE
3.482, CRPS 1.705, p95 **8.52°F** — the LOW residual tail. Bias-correction
can't shrink the long right tail (51% of these cells have actual ≥ 3°F below
the lock); gating is the structural fix.

### Mechanism
On clear-dry-cold nights (esp. Nov-Feb on continental stations), evening
radiational cooling continues past sunset, dropping temp below the morning
trough. Worst per-station bias on hour=18 locked: SAT +5.10, AUS +4.69, BOS
+2.71, DFW +2.35 (high-diurnal continental). Coastal stations are unaffected:
LAX −0.23, SFO +0.11. By 22:00 LST evening cooling is mostly complete and the
lock is reliable again (bias ~0°F across stations).

### Files shipped
1. `config.py` — added `NN_LOCK_FLOOR_LST_MIN`. Backup `config.py.pre_b_gate_21_20260519`.
2. `nn_match_fast.py` — config read + floor variable in LOW locked branch.
   Backup `nn_match_fast.py.pre_b_gate_21_20260519`.
3. `tests/test_nn_calibration.py` — new `TestNNLockFloor` (5 tests); updated
   one existing test to use cur=22*60 instead of 15*60. Backup `tests/test_nn_calibration.py.pre_b_gate_21_20260519`.
4. README updated (this block). Backup `README.md.pre_b_gate_21_20260519`.

### Rollback
Set `NN_LOCK_FLOOR_LST_MIN = 12 * 60` in `config.py` and restart — instant
revert to old behavior with no code change. Full file revert: restore the
four `.pre_b_gate_21_20260519` backups.

### Tooling used for the backtest
- `~/tools/nn_agg_sweep/deep_v2_2024_25.csv` / `deep_v2_2023.csv` (row-level)
- `~/tools/nn_agg_sweep/cohort_breakdown.py` (per-hour + per-station CRPS)
- `~/tools/nn_agg_sweep/hour18_lock_audit.py` (mechanism + month/station breakdown)
- `~/tools/nn_agg_sweep/gate_simulation.py` (cross-year variant comparison)

## Dead-code cleanup: anchored / low_rm_ceiling branches removed — 2026-05-19

Deletes the `anchored` and `low_rm_ceiling` μ-selection branches from
`paper_judge_bot.py`. Both derived μ from `mu_consensus_corr` (a forecast-blend
value) and were blocked at the trade layer by `PRESCREEN['skip_unless_nn_match']
=True` (shipped 2026-05-18). The branches still computed and stored a μ that
`skip_unless_nn_match` then rejected — pure overhead that misled future audits
into thinking the fallback chain still worked.

### What changes
- `paper_judge_bot.py` — removed both branches from `_select_mu()`. If
  `nn_match` returns None, `mu_method` stays on the `consensus_median` /
  `best_mae_*` path (also blocked by `skip_unless_nn_match`, but retained as
  the diagnostic μ field downstream tooling reads).
- Shadow helpers `_shadow_pace_median`, `_shadow_pace_proj`,
  `_shadow_anchored_proj` deleted — they only existed to populate fields
  consumed by the dead branches.
- Candidate log fields `shadow_mu_pace_proj`, `shadow_mu_anchored_proj`,
  `shadow_pace_median_at_hour` dropped.
- Prescreen comments updated to reflect that anchored/low_rm_ceiling no longer
  exist as separate code paths.

### Why
`skip_unless_nn_match=True` already blocks every non-nn_match μ method.
Anchored's formula `mu = consensus_corr + (rm - consensus_corr × pace_median)`
is structurally a forecast read with a pace-weighted rm overlay; it had been
the source of recurring "why is the bot looking at forecasts" confusion.
Removing the dead code prevents future sessions from spending cycles trying
to repair a path the bot will never dispatch.

### Validation
- Tests updated: `test_rm_validation_and_prescreen.py` (TestSkipUnlessNnMatch
  fixture renamed; TestSkipForecastOnlyMu docstrings refreshed) and
  `test_nn_calibration.py` (TestNNLockFloor docstring no longer references
  the deleted fallback chain).
- Full suite still 303/3-skip after the deletion.
- No live behavioral change: every trade that would have hit the deleted
  branches was already being skipped at the prescreen layer.

## NN_MATCH aggregator swap — Action C partial — 2026-05-18 23:39 UTC

Replaces median-of-top-k aggregation in `nn_match_fast.predict()` with per-side picks:

- **HIGH** → `idw3` (inverse-cube-distance weighted mean of all top-k neighbors, k=50)
- **LOW**  → `wins10_k20` (winsorized 10/90 mean of the 20 closest neighbors)

New rollback flag `NN_USE_NEW_AGGREGATORS: bool = True` in `config.py` — set False to revert to median behavior without a code edit.

**Why:** Cross-year backtest (2024-25 sweep n=72k; 2023 hold-out n=95k) — median-of-50 was mid-pack. Best per-side picks:

| side | aggregator | hold-out MAE | hold-out CRPS | per-station max regression |
|------|------------|--------------|---------------|---------------------------|
| HIGH | `k050_idw3` | −0.020°F (−0.9%) | **−1.7%** | +2.9% (zero >5%) |
| LOW  | `k020_wins10` | **−0.055°F (−2.7%)** | **−5.6%** | +4.1% (zero >5%) |

Real P&L counterfactual on 63 settled judge_bot positions (May 14, 13 unique trade events): **+$24.95 vs production median** (~+108% over actual $30.75; concentrated in DEN/NYC LOW trades where wins10's wider σ correctly SKIPped loser-bracket bets).

Per-station map tested + rejected — 14/20 stations show negative effect on hold-out (sweep noise). Same lesson as the prior k-sweep finding.

**Co-shipped bias / sigma refit (per-aggregator best-fit values, cross-year stable):**

| knob | before (for median) | after (for new aggregator) |
|------|--------------------|----------------------------|
| `NN_BIAS_CORR_HIGH_MORNING_F` | `-0.3` | `+0.05` |
| `NN_BIAS_CORR_HIGH_AFTERNOON_F` | `+0.3` | `+0.04` |
| `NN_BIAS_CORR_LOW_F` | `0.0` | `+0.11` |
| HIGH `sigma_factor` | `0.85` | `0.90` |
| LOW unlocked-AM `sigma_factor` | `0.85` | `1.10` |

`fit_quality_thresh` still uses raw stdev_delta on top-k (analog dispersion). Locked LOW unchanged — `mu_proj = traj_min`, bias/sigma adjustments don't apply. Locked-LOW bias correction (structural +2°F gap; `traj_min` over-projects `day_min`) deferred — open follow-up.

**Method label unchanged** (`nn_match_high` / `nn_match_low`). Claude reads μ/σ from packet; the aggregator name isn't surfaced to the prompt.

**Files modified:**

1. `nn_match_fast.py` — per-side aggregator branch in `predict()`. New return fields: `aggregator`, `n_aggregated`, `mu_delta_agg_f`, `sigma_natural_f`. Backup `nn_match_fast.py.pre_action_c_partial_20260518_193511`.
2. `config.py` — 3 bias edits + new `NN_USE_NEW_AGGREGATORS` flag. Backup `config.py.pre_action_c_partial_20260518_193511`.
3. `nn_shadow.py` — unchanged.
4. `prompts/entry_prompt.md` — unchanged.

Tests: 316 passed + 3 skipped + 1 pre-existing failure (`test_truncation_reduces_buy_no_edge_when_rm_in_yes` was failing pre-ship — unrelated, in `nn_shadow_strategy.py`).

PID 1706509 → 1863970 single+current via `sudo systemctl restart paper-judge-bot.service` at 23:39 UTC.

**Backtest tooling** lives in `/home/ubuntu/tools/nn_agg_sweep/`: `sweep.py`, `deep_dive.py`, `calibration_fit.py`, `judge_pnl_v3.py`, `per_station_v2.py`. Sweep CSVs `deep_2024_25.csv` / `deep_2023.csv`.

**Rollback:** flip `NN_USE_NEW_AGGREGATORS = False` in `config.py` and restart — instant revert to median behavior with sigma_factor 0.85 both sides. Full file revert: restore the two `.pre_action_c_partial_20260518_193511` backups.

## NN_MATCH analog distribution packet + anti-cherry-pick rule — 2026-05-18 22:16 UTC

Coordinated 6-file change to surface the FULL analog distribution (p25/p50/p75 + bracket-fraction) in the LLM packet, replacing the cherry-pick top-3 framing as the headline. Top-3 retained as a cross-check footnote. Prompt updated with a hard rule against trusting top-3 over mu_chosen.

**Why:** PHL B96.5 loss 2026-05-18 — bot opened BUY_NO at 35c because the LLM cited "top-3 analogs settled 93-95°F (all below YES floor 95.5°F)" as undershoot evidence. But mu_chosen was 96.3°F — dead center of YES window [95.5, 97.5). Actual hit >=96.8°F -> YES settled -> -$0.30/c x 11 contracts. Backtest 2024-25 n=2308 confirmed top3_med MAE is **+9% (HIGH) / +12% (LOW) WORSE** than median-of-50. The LLM was cherry-picking the worst-performing summary the bot could have shown.

**Files modified:**

1. `nn_match_fast.py` — `predict()` now returns `analog_summary`: full day_extremes array (n_used floats), p25/p50/p75 of day_extremes, p25/p50/p75 of deltas.
2. `nn_shadow.py` — passes `analog_summary` through to caller.
3. `paper_judge_bot.py` — computes `analog_in_bracket_pct / above_pct / below_pct` from the day_extremes array using the candidate's floor/cap (B-bracket [floor-0.5, cap+0.5); T-warm >= floor+0.5; T-cold < cap-0.5). Stashes in `_nn_match_meta` alongside top-3.
4. `judgment.py` — renders new block ABOVE top-3:
   ```
   - analog distribution (all 50 neighbors' settled day_extremes):
     - day_extreme: p25=94.1°F  p50=96.3°F  p75=98.2°F
     - Δ from cur_tmpf=91.4°F:  p25=+2.7°F  p50=+4.9°F  p75=+6.8°F
     - bracket-fraction: in YES window 32%  above 36%  below 32%
   - top-3 CLOSEST analogs (cross-check ONLY; cherry-picks, NOT the central estimate):
     - 2024-07-12: Δ=+4.20°F, day_extreme=95.5°F, tmpf_rmse=0.8°F
     - ...
   ```
   Hardcoded `RENDER_NN_MATCH_DISTRIBUTION = True` flag for instant rollback.
5. `prompts/entry_prompt.md` — updated NN_MATCH packet description (3 sections) with hard rule: "trust the distribution over the top-3 when they disagree"; cites PHL loss as cautionary example; backtest n=2308 reference.
6. `tests/test_nn_calibration.py` — new `TestNNAnalogDistribution` class (4 cases): analog_summary present, array size matches n_neighbors, percentile triples well-formed (p25<=p50<=p75), LOW side returns day_extremes.

**Tests:** 298 passed / 3 skipped (+4 new).

**Verification:**
- PID 1706509 ✓ single ✓ current
- `predict()` returns `analog_summary` dict with `day_extremes`, `day_extremes_p25_p50_p75`, `deltas_p25_p50_p75` — verified in-process
- Clean startup, no errors in 60s post-restart journalctl

**Rollback paths:**
- Disable new block only: edit `judgment.py` -> `RENDER_NN_MATCH_DISTRIBUTION = False` + restart
- Full revert: `cp .pre_distribution_packet_20260518_221308` backups + restart

**Open follow-up (from PHL forensics, NOT shipped):**
- Hard SKIP rule: `mu_chosen` ∈ YES window AND `|obs_trend_60m_slope|` > 1.5°F/h AND h_to_peak > 0.5h — defensive gate against the exact PHL pattern. Needs backtest before ship.

---

## LOW post-noon gate + fit-quality thresh tighten — 2026-05-18 18:41 UTC

Three coordinated nn_match changes. 2024-25 sample + 2023 hold-out both validated.

**Change 1 — config knobs (`config.py`):**
```python
NN_FIT_QUALITY_THRESH_HIGH: float = 3.0    # was 3.5; -7.7%/-9.4% kept_MAE
NN_FIT_QUALITY_THRESH_LOW: float = 3.0     # was 4.0; -5.0%/-12.9% kept_MAE
NN_LOW_GATE_UNLOCKED_POSTNOON: bool = True # new
```

**Change 2 — `nn_match_fast.py` predict() gate:**

When `side=="low"` AND `cur_lst_min >= 12*60` AND NOT `extreme_locked`, return `mu_proj_f=None` with `reason="low_postnoon_unlocked_unreliable"`. These cases (9.9% of post-noon LOW on 2024-25, 12.4% on 2023) have **MAE ~3.0°F** vs locked-mode MAE 0.73°F — the daily min came AFTER the daily max (cold-front / late-cooling-event pattern), so the kNN projection of further cooling is unreliable. Worst example: DCA 2025-11-27 saw err +35°F (bot projected 34°F, actual day_min was -1°F).

Gating these cases drops **post-noon LOW MAE by -25% on both years**.

**Change 3 — `nn_shadow.py`:** Reads `NN_LOW_GATE_UNLOCKED_POSTNOON` from config and passes `gate_low_postnoon_unlocked` kwarg into `predict()`. Safe rollback path = config flip.

**Validation:**

| Proposal | 2024-25 gain | 2023 hold-out gain | Throughput cost |
|---|---|---|---|
| SKIP unlocked-post-noon LOW | -25.0% MAE | -25.6% MAE | 9.9% / 12.4% LOW post-noon evals |
| HIGH gate 3.5→3.0 | -7.7% kept_MAE | -9.4% kept_MAE | 81%→71% / 83%→71% throughput |
| LOW gate 4.0→3.0 | -5.0% kept_MAE | -12.9% kept_MAE | 78%→67% / 80%→69% throughput |

**Tests:** 275 passed / 3 skipped. New `TestNNPostNoonUnlockedGate` (5 cases): gate fires when expected (low post-noon unlocked), does NOT fire when locked / pre-noon / HIGH side / explicit disable.

**Backups:** `config.py.pre_postnoon_gate_fitthresh_20260518_183853` (and matching for nn_match_fast.py, nn_shadow.py, tests/test_nn_calibration.py).

**Rollback paths:**
- Gate only: set `NN_LOW_GATE_UNLOCKED_POSTNOON = False` in config.py + restart
- Fit thresholds: restore from `.pre_postnoon_gate_fitthresh` backups + restart
- All three: `cp config.py.pre_postnoon_gate_fitthresh_<ts> config.py` + same for nn_match_fast.py + nn_shadow.py + restart

PID 1287209 ✓ single ✓ current.

---

## nn_match pres1_trajectory matching SHIPPED (LOW) — 2026-05-18 17:02 UTC

`NN_PRES_TRAJ_WEIGHT_LOW: 5.0` (HIGH=0). Stacks on top of the existing
relh+k50+bias=0 LOW baseline. Held-out backtest seed=1 n=11,009 on
TODAY's production: **LOW MAE 1.929 → 1.889 (−0.040°F / −2.1%).**
Per-hour Δ stable across hours 2-7 (all negative: −0.02, −0.03, −0.04,
−0.05, −0.11). HIGH gets weight=0 — Exp3 showed no HIGH gain.

**Why w=5.0 not w=15:** w5 was the SAFER weight with nearly identical
overall gain (−0.032 vs −0.041 on yesterday's baseline, −0.040 vs
−0.041 on today's). At hour 7 cohort specifically, w5 beats w15
(−0.12 vs −0.07). Lighter weight = less risk of pres-RMSE swamping
temperature in edge cases.

**Why discipline mattered:** original Exp3 (seed=42) showed gust_w0.15
LOW −0.044°F AND pres_traj_w15 LOW −0.041°F. Re-validation on held-out
seed=1: gust collapsed to −0.006°F (in-sample noise), pres held at
−0.041°F. Held-out validation = the ship/reject discriminator.

**Live data plumbing (new infrastructure):**
- `pres_history.py` NEW module: per-station rolling snapshots of altimeter
  (inHg) at cycle cadence to `data/pres_history/<station>.jsonl`.
  RETAIN_HOURS=6 prune, atomic write, mirror `nws_fc_history.py` pattern.
- `live_data.py` hook: per-cycle `pres_history.record_snapshot(st,
  _w["altimeter"], now_ts)` immediately after the existing nws_fc_history
  snapshot call.
- `nn_shadow.py` builds today's trajectory: reads last 3h from
  `pres_history.get_history()`, converts altimeter → station_pressure
  via standard-atmosphere formula
  `station_pres = altimeter × (1 − 0.0065·elev_m/288.15)^5.2561`,
  using `station_meta.STATION_META[icao]["elev_ft"]`.
- `nn_match_fast.py`: `pres1_trace` loaded in `_load_pool` SQL + numpy
  arrays; `pres1_trajectory` + `pres_traj_weight` kwargs on `predict()`;
  pres_rmse term in score (matches relh pattern, `≥6 paired bins` gate).

**Conversion sanity (altimeter=30.00 → station_pres vs DB pres1 mean):**
ATL +0.12, AUS −0.08, DEN n/a (DB pres1 mostly NaN, matcher handles),
LAS +0.11, SFO −0.03 inHg. Within ±0.1 for 17/19 stations vs typical
weather std 0.15 inHg.

**Activation timeline:** bot needs ~3h post-restart to accumulate
≥12 records per station. Before then matcher silently drops pres1 from
scoring (≥6 paired bins gate → pres_rmse=0). Expect gain to appear
~3h after 17:02 UTC restart.

**Verification:**
- `data/pres_history/K{station}.jsonl` files writing 1 record per cycle ✓
- `shadow_code_decisions.jsonl` shows `nn_match_low_n50` mu_method ✓
- Manual `spy_predict.py` confirms LOW gets `pres_traj_weight=5.0`,
  HIGH gets `pres_traj_weight=0.0` ✓
- Tests 271 pass / 2 pre-existing skip ✓

**Backups** (timestamp `pre_pres_traj_20260518_170154`):
`nn_match_fast.py`, `nn_shadow.py`, `config.py`, `live_data.py`, `README.md`.

**Rejected in same session** (all backtested, all failed
held-out or cost/benefit bar):
- Time-decay weighting on RMSE (Exp1): HIGH tau24 −0.026°F /
  LOW tau6 −0.022°F, optimal τ differs HIGH vs LOW, within noise.
- 1-min ASOS resolution (Exp2): per-bin signal floor 0.17°F vs
  match RMSE 1-2°F scale; 5× DB infra cost. Cost/benefit reject.
- Cloud/gust/pres trajectory on HIGH (Exp3): all within noise; cloud
  actively HURTS HIGH (+0.015).
- Gust trajectory LOW (Exp3 + Exp5 carve-outs): seed=42 −0.044°F →
  seed=1 −0.006°F. In-sample noise. Per-hour carve-outs also failed.
- Adaptive LOW pre-dawn window narrow_120 / midnight_focus (Exp4):
  mixed cohort effects, net −0.014°F.

## nn_match k=50 ROLLBACK (HIGH) — 2026-05-18 03:13 UTC

`NN_K_HIGH: 150 → 50`. **`NN_LOOKBACK_HIGH_MIN: 180` retained.** No
code changes — `nn_shadow.py` reads `NN_K_<side>` from config at
runtime, so a config-only edit fully reverts the k change while
preserving the trajectory-window improvement (which carried the
bulk of the original deep-dive gain).

**Why rolled back:** Phase-2 per-station k tuning sweep on the same
2024-2025 sample at lookback=180 surfaced that k=150 regressed
**10 of 20 stations** when MAE is equal-weighted per station rather
than sample-weighted. The biggest regressions are the highest-volume
trading stations:

| station | k=50 MAE | k=150 MAE | Δ |
|---|---|---|---|
| MIA | 0.958 | 1.123 | +17% |
| AUS | 1.574 | 1.728 | +10% |
| NYC | 1.242 | 1.331 | +7%  |
| DFW | 1.295 | 1.374 | +6%  |
| MDW | 1.498 | 1.649 | +10% |
| OKC | 1.609 | 1.704 | +6%  |
| MSY | 1.100 | 1.153 | +5%  |
| LAS | 1.306 | 1.368 | +5%  |
| DCA | 1.397 | 1.453 | +4%  |
| SAT | 1.344 | 1.398 | +4%  |

Stations where k=150 helped (BOS, PHL, SEA, DEN) are lower-volume
on the HIGH side and dont net out the regression on busy stations.

The originally-reported -7.3%/-9.2% MAE gain from the deep-dive ship
was almost entirely the **trajectory change** (sunrise → lookback=180min),
not the k change. The trajectory change is being kept.

**Variables sweep (pres1 / gust / cloud) on top of k=150+win=180:**
all weights at 0.05/0.10/0.20 — flat or within noise. None shipped.
gust_w005 best at -0.8% MAE (below precedent ship bar).

**Backups:** `config.py.pre_k50_rollback_<ts>` next to the prior
`pre_nn_k150_win180_20260517_225809` backup.

**Rollback restore:** `cp config.py.pre_nn_k150_win180_20260517_225809
config.py` if k=150 turns out to be live-better; restart.

**Verification:** tests 249/2-skip; PID 3796693 single+current;
`config.NN_K_HIGH==50` confirmed in-process; `NN_LOOKBACK_HIGH_MIN==180`
retained.

**Open follow-up:** cross-year per-station k validation on 2023
hold-out. If per-station preferences hold up across years, ship a
station-keyed k map (NN_K_PER_STATION dict). Conservative starting
candidates: BOS=100, PHL=150, SEA=200, DEN=200; rest default to 50.

## nn-only prompt + NN_MATCH packet block enrichment — 2026-05-18 00:30 UTC

Post-k50 ship audit found 6 drift bugs between the LLM prompt and the
live code that could each confuse Claude's nn-only reasoning. Fixed in
a coordinated ship to **`prompts/entry_prompt.md`**, **`judgment.py`**,
**`paper_judge_bot.py`**, and **`nn_shadow.py`**.

**Prompt rewrites (`prompts/entry_prompt.md` 966 → 1028 lines):**

  1. All `_n30` → `_n50` in 6 few-shot examples + Step 3 read examples
     (today's k=50 ship made `_n30` strings stale).
  2. Stale `bias_correction_applied: +0.99°F (P1)` (HIGH) and `−1.82°F
     (P1)` (LOW) removed from every example — replaced with current
     hour-aware HIGH (`+0.30°F afternoon` / `−0.30°F morning`) and
     LOW = 0.0. Constant values were specifically rejected by
     [[feedback_calibration_per_hour_decomposition]].
  3. Stale fit-quality gate text "exceeds 3.0°F HIGH / 4.0°F LOW" →
     "3.5°F HIGH / 4.0°F LOW".
  4. Stale `60pp` ceiling references → `25pp` (config tightened
     2026-05-16). The 25-60pp scrutiny band is now unreachable (code
     blocks at 25pp unless rm-locked); Step 9 EV check + EV sizing
     table rewritten — old 25-60pp band replaced with a single
     "rm-locked >25pp" row.
  5. Stale `8pp` minimum edge → `6pp` (config lowered 2026-05-15).
  6. Example 5 (was a 57pp pre-peak-rising-obs SKIP, illustrating the
     25-60pp scrutiny band that no longer exists) replaced with a
     clean small-edge BUY_NO example in the 6-12pp band.

**Packet block enrichment (`judgment.py`):**

  - `## NN_MATCH` block now renders `n_neighbors`, `pool_size`,
    `extreme_locked`, `sigma_raw` × `sigma_factor`, `bias_correction`
    applied, `fit_quality_thresh` (PASSED), and the top 3 analog
    neighbor days (date + Δ from cur + day_extreme + tmpf_rmse). Source:
    new `packet["_nn_match_meta"]` dict, stashed by
    `paper_judge_bot.py` when `shadow_nn_proj()` returns μ.
  - `(treat as decision-grade only when r² ≥ 0.7)` annotation REMOVED
    from the 60m regression line in BOTH entry and exit prompt
    renderers. Step 4 of the prompt explicitly walks back from this
    hard cutoff (and lists "below 0.7 threshold" as a banned phrasing)
    — the in-packet annotation was directly contradicting the rule.

**Metadata plumbing (`nn_shadow.py` + `paper_judge_bot.py`):**

  - `shadow_nn_proj()` now returns `sigma_raw`, `sigma_factor`,
    `bias_correction`, `fit_quality_thresh`, `median_delta`, `cur_tmpf`
    in addition to existing fields.
  - `paper_judge_bot.py` `_numerical_edge` stashes a compact
    `_nn_match_meta` on the packet when nn_match fires; this also
    flows into `candidates.jsonl` for backtest attribution.

**Validation:**

  - Tests: 249 passed / 2 skipped (unchanged).
  - Synthetic render: 3 cases (full meta / forecast-only mu_method /
    empty meta dict) all render cleanly.
  - Structural backtest: 121/121 nn_match LLM-decided candidates from
    past 2 days have all prompt-required fields populated (100%).
  - One live LLM dispatch on a known-good packet (past-peak HIGH +
    rm-lock + nn_match agrees → expected BUY_NO): Claude returned
    `BUY_NO conviction=0.93 size=0.9 obs_anchor=rm=79.0` with a clean
    nn_match-only evidence chain. No forecast model names, no banned
    phrasings, obs_anchor validated.

Backups `pre_nn_only_polish_20260518` on all 4 files. Instant rollback
is `RENDER_NN_MATCH=False` in `judgment.py` (drops the new block; the
old prompt examples still drift but the bot keeps running).

## nn_match k=150 + lookback=180min (HIGH only) — 2026-05-18 02:58 UTC

Tuned `nn_match`'s top-k and trajectory window for HIGH side based on a
deep-dive sweep. LOW unchanged (gain was marginal).

**Findings (HIGH):**

| Metric | prod baseline | new config | delta |
|--------|---------------|------------|-------|
| MAE (2024-2025 n=1010-1133) | 1.836°F | 1.701°F | **-7.3%** |
| MAE (2023 hold-out n=932) | 1.670°F | 1.516°F | **-9.2%** |
| p95 | 4.31 | 3.99 | -7% |

The 2023 hold-out used a different random seed (43) from the 2024-2025
sweep (42), so cross-year validation is independent. The −9.2% on 2023 is
*better* than the −7.3% on 2024-2025, suggesting the gain is robust to
year-to-year climate variation, not overfit.

**Config knobs added (`config.py`):**
```python
NN_K_HIGH = 150         # was implicit k=50 in nn_match_fast.predict default
NN_K_LOW  = 50          # unchanged
NN_LOOKBACK_HIGH_MIN = 180   # truncate trajectory to last 180min
NN_LOOKBACK_LOW_MIN  = 0     # 0 = full climate-day trajectory (current)
```

**nn_shadow.py edits:**
- After merging hourly_obs + temp_history + live_obs into the trajectory,
  truncates entries older than `cur_lst_min - NN_LOOKBACK_<side>_MIN`.
- Passes explicit `k=NN_K_<side>` to `predict()`.

**What didn't help (tested + rejected in this sweep):**
- Time-decay weighting (τ=12/24 bins): flat to slightly worse.
- Very small k (k=20): -3.1% vs baseline (worse).
- Very large k (k=150) on LOW side: +1.9% (worse).
- Shorter trajectory windows (60min, 90min): each -5 to -6% on HIGH but
  the 180min was best.

**Bias correction unchanged.** Refit on 2023 gave +0.331; production HIGH
afternoon bias is +0.3 (NN_BIAS_CORR_HIGH_F=0 + NN_BIAS_CORR_HIGH_AFTERNOON_F=0.3),
which is functionally equivalent. The refit's near-zero residual (-0.025
on 2023) means existing bias is already calibrated for the new config.

**Verification:** Config loads (NN_K_HIGH=150, NN_LOOKBACK_HIGH_MIN=180,
others at defaults). Tests 249 passed / 2 skipped.

PID 3768833 single + current.

**Backups (VPS):**
- `config.py.pre_nn_k150_win180_20260517_225809`
- `nn_shadow.py.pre_nn_k150_win180_20260517_225809`

**Rollback:** Flip `NN_K_HIGH: int = 150` → `50` and `NN_LOOKBACK_HIGH_MIN
= 180` → `0` in config.py, restart. No code rollback needed.

**Caveat for live validation.** Production prod-baseline MAE figures
above used the OLD +0.99 bias (pre-hour-aware); actual recent production
uses +0.3 afternoon bias, so the live "before" MAE is somewhat lower
than the 1.670 baseline in my refit. The 2024-2025 sweep was apples-to-
apples (same bias throughout) so the -7.3% is the cleanest signal.
Watch a few days of live data to confirm gain transfers.


## Prompt-packet alignment fixes — 2026-05-17 23:58 UTC

Three discrepancies between `prompts/entry_prompt.md` and the rendered
LLM packet identified by audit and fixed:

**#1. `_OBS_ANCHOR_FIELDS` validator trimmed (judgment.py).** Removed
`pace_slope`, `pace_slope_current_gap`, and `temp_obs` from the
validator table. The first two are forecast-derived signals the
prompt's Output schema explicitly forbids — but the validator was
silently accepting them, so a Claude submission like
`obs_anchor=pace_slope=-0.75` would have passed code validation despite
violating the prompt rule. `temp_obs` (NWS METAR fallback) was retired
2026-05-15 by the wethr-only policy and now always looks up to None;
removed for cleanliness. Test `test_negative_value` updated to use
`obs_trend_30m=-0.75` instead of the removed `pace_slope=-0.75`.

**#2. `skip_forecast_only_mu` documented in prompt pre-filters.** The
prompt's "Pre-filters enforced in code" list now mentions the
2026-05-17 20:01 UTC code-side gate (`config.PRESCREEN[
"skip_forecast_only_mu"]`) that drops `best_mae_*` / `consensus_median` /
`raw_median` candidates before they reach the LLM. Combined with Step
2's wider prompt-side gate, every candidate Claude sees should have
`mu_method.startswith("nn_match_")`. Documentation drift closed.

**#3. Renderer labels surface `obs_anchor` field names directly.** The
`## LIVE OBS` block was rendering values under friendly labels
(`temp: 78.4°F`, `running_max today: 79.0°F`, `probable bounds (...
lowest=X, highest=Y)`) that the `obs_anchor` schema referenced by
*different* names (`wethr_temp_f`, `running_min_or_max`,
`wethr_lowest_probable_f`/`wethr_highest_probable_f`). Claude had to
mentally map friendly → schema; some submissions probably failed
validation as "unknown field". The renderer now uses schema-canonical
labels in the packet text itself:

```
- wethr_temp_f: 78.4°F   dew_point_f: 62.0°F   relative_humidity: 56%   age_sec: 18s
- obs_trend_30m: -0.20°F (point-in-point delta over last 30 min)
- obs_trend_60m_slope: -1.10°F/h  obs_trend_60m_r_squared: 0.86  n=12  span=58min
- temp_history_range_60m: 3.1°F  (n=12 pts, span=58min)
- running_min_or_max (wethr_high_f, running_max today): 79.0°F  (set 1.2h ago at ...UTC)
- wethr_lowest_probable_f: 78.0°F   wethr_highest_probable_f: 79.5°F
```

Claude can now copy a field name straight from the packet into its
`obs_anchor` output without translation.

**Backups (VPS):**
- `judgment.py.pre_align_20260517_195825`
- `prompts/entry_prompt.md.pre_align_20260517_195825`
- `tests/test_r2_obs_anchor.py.pre_align_20260517_195825`

**Tests:** 249 passed / 2 skipped.

**PID 3432531** single + current.


## NN_MATCH packet block restored — 2026-05-17 23:35 UTC

Added `## NN_MATCH` rendering block in `judgment.py` (above `## LIVE OBS`).
Without it, the LLM packet carried no `mu_method` / `mu_chosen` /
`sigma_chosen` after the 22:01 UTC forecast-strip, so every dispatch
hit Step 2's "if mu_method does not start with nn_match_* SKIP" but
Claude couldn't see the field — every read hallucinated *"no nn_match
signal present in packet"* and returned SKIP. Audit of today's
decisions.jsonl: 141 entry dispatches × $0.28 avg = $39.79 spent for
**zero BUYs**, all hallucinated SKIPs.

**The fix.** Single rendered block:

```
## NN_MATCH
- mu_method: nn_match_high_n30
- mu_chosen: 84.1°F   sigma_chosen: 2.10°F
```

Rendered unconditionally when any of `mu_method` / `mu_chosen` /
`sigma_chosen` is present in the packet. Controlled by
`RENDER_NN_MATCH = True` flag for instant rollback. Placed between
the (now gated-off) FORECASTS branch and the active LIVE OBS block.

**Why it matters.** The prompt's Step 2 and Step 3 reference these
fields by exact name; without them rendered, Claude has no choice but
to claim they're absent. The fix restores Step 2 / Step 3 functionality
so the LLM can actually evaluate `mu_method.startswith("nn_match_")`
and adopt `mu_chosen` / `sigma_chosen` as central μ/σ.

**Verification.** Simulated `build_entry_user_message()` on a known
`mu_method=nn_match__n30` candidate from earlier today (KXHIGHAUS-T89):
the rendered packet now contains the NN_MATCH block with the correct
values. Live confirmation pending — the bot post-restart hit a quiet
window (208 candidates/cycle pre-screened as "already settled" because
of end-of-UTC-day market closures); first real test on fresh
nn_match candidates lands when markets reopen tomorrow morning UTC.

Backup `judgment.py.pre_nn_block_20260517_193446`. Tests 249/2-skip.
PID 3381949 single + current (or successor — bot may have been
restarted concurrently by other workflows; check RULE #3c).

**Rollback:** flip `RENDER_NN_MATCH = True → False` in `judgment.py`
and restart.


## Entry prompt: nn_match-only regime — 2026-05-17 20:55 UTC

Full forecast purge in `prompts/entry_prompt.md` (1090 → 965 lines).
The prompt now reasons exclusively from `nn_match` projections + wethr
observations. Forecasts and forecast-derived metrics (`pace_slope`,
`hourly_forecast_24h`, `model_mae_recent`, `persistence_3day`,
`forecast_deltas`, NBM / HRRR / ECMWF / NBP names) are explicitly
banned from citations in `read`.

**What changed.**

1. **Step 2 — gate on `mu_method`.** If `mu_method` does not start
   with `nn_match_*`, the prompt instructs SKIP with a one-line read
   and `size_factor=0.0`. The bot already enforces a code-side
   `skip_forecast_only_mu` prescreen gate (paper_judge_bot.py:1491)
   for the pure-forecast fallbacks; the prompt's Step 2 is
   belt-and-suspenders that also covers the rm-anchored fallbacks
   (`anchored`, `low_rm_ceiling`) since those still derive μ from
   forecasts under the hood.
2. **Step 3 — use `mu_chosen` / `sigma_chosen` directly.** Old Steps 2
   and 3 (forecast consensus audit, bias correction) are removed
   entirely. There is no "compute μ from forecasts" path anymore.
3. **Step 4 (truth + cross-check).** Forecast tier removed from the
   truth hierarchy. Only wethr_obs, rm, and nn_match's mu_chosen are
   truth-tier. The 60m regression cross-check stays (it's obs-derived);
   pace_slope is removed.
4. **Step 6 (diurnal projection).** Pace_slope-based projection
   replaced with `mu_chosen ± slope×h_remaining`, capped at σ to avoid
   over-extrapolation. rm physical lock and past-extreme cushion paths
   unchanged.
5. **Step 6.5 (obs-anchored hard veto).** Unchanged in mechanism;
   wording cleaned to drop "forecast cluster" references.
6. **Step 10 (citation discipline).** Explicit forbidden-citations
   list: forecast model names, `pace_slope`, `mean_gap_f`, "obs ahead
   of forecast", "forecast bust", "model disagreement", `hourly_forecast_24h`.
7. **Few-shot examples.** All 6 examples rewritten from scratch:
   - Old packets carried `## PACE BAND` / `## TAIL BAND` / `## PACE
     TRAJECTORY` / `FORECASTS:` blocks. New packets carry `## NN_MATCH`
     (mu_chosen, sigma_chosen, mu_method, P1 bias, P2 fit-quality)
     plus `## LIVE OBS (wethr)` only.
   - Old reads cited NBM / HRRR / ECMWF / "best-MAE source" / pace_slope.
     New reads cite `mu_method`, `mu_chosen`, `rm`, `wethr_high_f` /
     `wethr_low_f` / `wethr_temp_f`, `wethr_highest_probable_f` /
     `wethr_lowest_probable_f`, and 60m regression slope + r² + range_60m.
   - The 6 examples cover: BUY_NO past-peak rm-lock (Ex1), SKIP HIGH
     pre-peak with obs gate (Ex2), BUY_NO LOW d+0 nn well above
     bracket (Ex3), SKIP LOW d+0 with nn in YES window (Ex4), SKIP
     large gap with no rm-lock + rising obs (Ex5), BUY_NO T-bracket
     Rule#2 bypass via rm-lock (Ex6).
8. **`obs_anchor` allowed fields.** `pace_slope` and
   `pace_slope_current_gap` removed. Remaining allowed fields are all
   wethr/obs/60m-regression-derived.

**Why.** Per Chris (2026-05-17 evening): forecasts are publicly
priced; the bot's edge is `nn_match`'s analog-match against ~99k
historical station-day traces. Citing forecasts in reads (a) blurs
what the bot's actually doing, (b) confuses the LLM into recomputing
μ from forecasts when it should trust `mu_chosen` directly. The
2026-05-17 morning A+B+D refresh tried to nudge Claude toward
`mu_chosen` but kept Steps 2/3's forecast-audit scaffolding as
fallback; this ship deletes the fallback and makes nn_match the
only path.

**Test impact.** Two test files needed an autouse fixture to disable
the `skip_forecast_only_mu` gate so their fixtures (which don't seed
nn_match) reach the gates they're written to cover. 32 stale failures
→ 245 passed / 2 skipped. The dedicated `TestSkipForecastOnlyMu` class
(test_rm_validation_and_prescreen.py:754) still toggles the flag in
its own monkeypatch calls — autouse + per-test monkeypatch compose
correctly.

Backups (on VPS `54.225.174.220`):
- `prompts/entry_prompt.md.pre_nn_only_20260517_164219`
- `tests/test_prescreen_time_windows.py.pre_forecast_only_fixture_20260517_165446`
- `tests/test_rm_validation_and_prescreen.py.pre_forecast_only_fixture_20260517_165446`

PID 3102371 single + current. Tests 245 / 2-skip. Clean startup, no
errors in journal.


## pace_band retirement from packet — 2026-05-17 19:20 UTC

Dropped pace_band / tail_band / pace_low_band / tail_low_band sections
from the LLM-rendered packet (`judgment.py`) and stripped the
pace_band-driven Step 7 / Step 4 / Step 5 logic from
`prompts/entry_prompt.md`. **Data is still computed and persisted to
`candidates.jsonl`** for retroactive A/B analysis — only the rendering
is disabled. Flip `RENDER_PACE_BAND = False → True` in `judgment.py`
to re-enable.

**Why.** Pure-pace_band backtest across 2026-05-15 → 2026-05-17 (n=178
brackets, both HIGH and LOW, eligible cases with pace_band data + 3h+
forward outcome + final `wethr_high_f`/`wethr_low_f`):

| pace_band band vs YES window | n   | settled YES |
|------------------------------|-----|-------------|
| OVERLAPS YES window          | 22  | **0 (0.0%)**|
| CLEARS YES window            | 156 | 34 (21.8%)  |

If pace_band were predictive, the OVERLAPS cohort would have a *higher*
YES-rate than CLEARS (the band overlaps because the projection touches
the YES window, so it should bias toward YES). Instead overlap predicts
0% YES — the signal points the wrong direction. And the n=22 overlap
group was being used as Step 7's BUY_NO veto: in production, the Cohort
B (would-be SKIP due to overlap) over 3 days = n=2, 100% would-have-won
BUY_NO, +21¢/share lift forfeited.

**Why the signal inverts.** pace_band assumes (rm at hour H) × (1 /
pace_p25_p75) gives the day's peak distribution. But on the days where
the band straddles a bracket boundary, the bot is already mid-cycle —
rm has typically converged to the median pace by the time these
candidates evaluate, so the band squeezes around `rm / 1.0 = rm`. When
rm sits just below `cap+0.5`, the band by construction overlaps the
YES window. The veto then blocks BUY_NO on exactly the brackets where
the obs trajectory (60m regression + pace_slope + Step 7.5 obs-anchored
gate) is most informative, because those are the ones where rm is hugging
the bracket edge.

**Mechanism preserved.** The replacement Step 7 uses only obs-anchored
signals: rm physical lock (rm > cap+0.5 forces NO; rm < floor−0.5 in
post-peak forces NO; etc.), 60m regression slope projection, and the
past-extreme cushion from `wethr_highest_probable_f` /
`wethr_lowest_probable_f`. Step 7.5's hard veto (wethr_temp_f in YES
window + h_to_extreme > 0.5h + no overshoot confirmation) is unchanged.
This keeps the bot's stated edge — observations, not forecast climatology
— and removes a signal that was actively inverted.

**Few-shot examples.** The two pace_band-citing few-shots in the prompt
(`Example 2: BUY_NO — HIGH d+0 pre-peak`, `Example 3: BUY_NO — LOW d+0
overnight, pace_low_band shows insufficient cooling`, `Example 4: SKIP
BUY_NO — HIGH d+0 pre-peak, pace_band gives projection band`) are left
in place but a warning at the top of the EXAMPLES section flags that
their pace_band citations are obsolete and the read should be reasoned
from obs trajectory + rm + forecasts alone. Removing the examples
entirely would be a bigger prompt-cache reset than necessary; the
warning is sufficient.

**Re-enable path (A/B test):**
1. `sudo systemctl stop paper-judge-bot`
2. Edit `judgment.py` → flip `RENDER_PACE_BAND` to `True`
3. (Optional) Restore Step 7 from `entry_prompt.md.pre_paceband_retire_20260517_151909`
4. `sudo systemctl start paper-judge-bot`

Backups `entry_prompt.md.pre_paceband_retire_20260517_151909` and
`judgment.py.pre_paceband_retire_20260517_151909`. Tests 243 / 2-skip.
PID 2949293 single + current.


## pace_slope prompt rule: always + 0.5·mean_gap — 2026-05-17 18:38 UTC

Replaced the slope-gated mean_gap adjustment in Step 5 with an always-on
`0.5 × mean_gap_f` adjustment. Slope sign is now informational only.

**Why.** Two backtests showed the slope-gating discards real signal:

1. **Climatology-baseline backtest, n=4554** (synthetic forecast = 3-yr
   median temp[station, month, hour]; tests the underlying mechanism):
   - mean_gap_sign matches actual-bust-direction **83-85%** of the time
   - slope_sign only 57-63%
   - `always + 0.5 × mean_gap` MAE reduction vs baseline: HIGH 6.70 → 4.62
     (−31%); LOW 5.54 → 3.66 (−34%)
   - `+mean_gap if |slope| ≥ 0.5` (the old prompt rule): only −20% to −31%

2. **Production NWS-baseline backtest, n=109** (real `mu_consensus_corr`
   vs final `wethr_high_f`/`wethr_low_f`, 2026-05-16 to 2026-05-17):
   - mean_gap_sign 68.7% HIGH, 69.4% LOW
   - slope_sign **47.8% HIGH (worse than coin flip!)**, 58.3% LOW
   - `always + 0.5 × mean_gap`: MAE 2.96 → 2.68 (−9.5% overall); HIGH
     3.47 → 3.24 (−7%); LOW 2.15 → 1.78 (−17%)
   - Old prompt rule: only −1.6% overall (the slope gate fires rarely
     because NWS forecasts already capture local trend, leaving the
     bot stuck at baseline)

**Mechanism.** The slope tells you whether forecast bias is widening,
but the gap itself (mean_gap_f) already captures the bust direction
and magnitude. Gating mean_gap on |slope| ≥ 0.5 throws away the signal
in stable-bias regimes (which dominate production: NWS forecasts mostly
have steady biases, not accelerating ones). The 0.5× factor hedges
against the ~30% of cases where mean_gap direction is misleading.

**What changed in the prompt.** Step 5's `obs_vs_forecast_pace_slope`
subsection now says: "Apply 0.5 × mean_gap_f regardless of slope sign"
+ "slope is informational only" + new banned-phrasings list to stop
Claude reverting to the old gated framing. rm-anchor exception preserved.

Backup `entry_prompt.md.pre_paceslope_v2_20260517_183636`. Tests 243 / 2-skip.
PID 2870671 single + current.

## CLAUDE_TIMEOUT_SEC $800 \to 1200$ — 2026-05-17 16:41 UTC

Bumped LLM call timeout from 800s to 1200s in `config.py`.

**Why.** After the A+B+D entry-prompt refresh (09:53 UTC), the same
ticker `KXHIGHPHIL-26MAY17-B88.5` timed out twice at 800s (15:54 + 16:24
UTC). That packet is a worst-case ambiguous case: `mu_chosen=87.7°F`
sits inside YES window `[87.5, 89.5)`, obs at 82.4°F climbing
+2.4°F/h with r²=0.575, all 3 forecasts cluster 88-90°F above bracket,
but the obs-anchored gate (Step 7.5) blocks BUY_NO because
`wethr_highest_probable_f = 83°F < cap+0.5 = 89.5°F`. P(YES) ≈ 0.38 is
below the 0.83 conviction floor for BUY_YES. Claude burned 13+ minutes
deliberating between two blocked options and hit the 800s wall.

Overall latency distribution post-A+B+D refresh: median 245s, p90 ~470s
(vs pre-refresh median 238s, p90 354s) — small shift, not a regression.
The timeout is for the p99+ ambiguous tail. 1200s covers it without
changing typical-case behavior. Backup
`config.py.pre_timeout_1200_20260517_164141`. Tests 234 / 2-skip.
PID 2684954 single + current.

## Entry prompt: A+B+D refresh for nn_match awareness — 2026-05-17 09:53 UTC

The entry prompt predated `nn_match` and was instructing Claude to
compute μ from raw forecasts in Step 4. Diagnostic evidence: a live
read on KXLOWTCHI-B63.5 where `mu_chosen=64.9°F` from
`nn_match__n30` was ignored in favor of "Best-MAE source ECMWF-IFS …
corrected μ=64.92°F", and σ was recomputed to 1.46°F (Claude's
1.25×MAE rule) instead of using `sigma_chosen=3.15°F`.

Three coordinated edits to `prompts/entry_prompt.md`:

**A — `nn_match` added to the bot's edge list + new `## nn_match`
section** (~50 lines added). The "Your edge" preamble now lists 5
items instead of 4, with k-NN heating-curve analog projection called
out as the bot's strongest single obs-derived signal. A new section
above Step 1 explains the `mu_method` taxonomy (nn_match, anchored,
low_rm_ceiling, consensus_median, best_mae_*, raw_median), the
trajectory matching, P1 bias + P2 fit-quality gates, Action C cohort-
aware σ multiplier, and the `_locked` semantics.

**B — Step 4 rewrite** (~30 lines). New rule: when `mu_method` starts
with `nn_match_*`, use `mu_chosen` and `sigma_chosen` directly; do not
recompute from forecasts. When `mu_method` is a fallback, cross-check
against the lowest-MAE forecast source. Banned the 1.25 × MAE σ
re-derivation (the bot has already done it). Truth hierarchy in Step 5
gets a new tier-3 entry placing nn_match-derived μ alongside wethr
obs / rm, above forecast tier. Step 9 simplified to "use `mu_chosen`
+ `sigma_chosen` directly". Step 11 citation discipline now requires
naming the `mu_method` value.

**D — Step 7 consolidation** (HIGH and LOW unified into one direction-
parametric block, ~146 lines → ~120 lines, but cleaner). Three named
regimes: A pre-extreme (pace-band projection), B outlier (rm exceeds
cohort by 5°F+ → tail.p90 central), C saturation/past-extreme (tail-
band projection). The heat-outlier backtest evidence (tail_p90 MAE
2.47°F vs pace_med MAE 7.98°F over n=15 outlier days) preserved
verbatim. Cold-outlier is now the LOW arm of regime B (no longer a
separate copy-pasted block). rm-lock bypass kept in regime A, also
applies in C. The "do NOT use HRRR credible-cluster" warning kept.

Backup `entry_prompt.md.pre_abd_20260517_095350`. Bot reloads the
prompt at startup only (`_load_static_prompt` caches in module-level
`_ENTRY_SYSTEM_CACHED`), so restart was required. Tests 222 pass / 2
skip. PID 2059315 single + current per RULE #3c.

## min_buy_usd $5 → $1 — 2026-05-17 09:21 UTC

**Bug fixed: bot was 0-for-12 on Claude high-conviction BUYs since the
nn_match live ship 6h earlier.**

Lowered `GUARDRAILS["min_buy_usd"]` from $5.00 to $1.00 in `config.py`.
Caps unchanged (`max_bet_low_series_usd = max_bet_high_series_usd = 5.0`).

**Why.** When the $5/$5 series caps were added alongside the nn_match live
ship, the existing $5.00 `min_buy_usd` floor collapsed the integer-contract
trade window. At typical BUY_NO ask 78c on a YES-window LOW B-bracket:

| qty | cost  | floor ≥ $5 | cap ≤ $5 |
|----:|------:|:----------:|:--------:|
|   5 | $3.90 |     ✗      |     ✓    |
|   6 | $4.68 |     ✗      |     ✓    |
|   7 | $5.46 |     ✓      |     ✗    |

No integer satisfies both constraints. For ask in roughly 72c–99c (most
BUY_NO opportunities on this bot's edge), the bot was structurally locked
out. Additionally, Claude's `size_factor` (0.55–0.70 typical) shrinks
`target_cost` to ~$3, further from the $5 floor.

**Live diagnosis.** Between bot restart 06:38 UTC and config-fix restart
09:21 UTC: 12 high-conviction Claude BUYs (PHX/CHI/OKC/MIA/PHIL LOW + PHX/
CHI YES, conv 0.83–0.88), 10 reached scout, **0** plan_ok. Every scout
skipped with `"reachable $X.XX (N contracts at edge ≥ 6pp) < floor $5.00"`.

Backup `config.py.pre_min_buy_1usd_20260517_092054`. No code change beyond
the one config value. Tests 215 pass / 2 skip.

PID 1999501 single + current per RULE #3c.

## nn_match Action C — cohort-aware σ multiplier — 2026-05-17 09:08 UTC

Added a cohort-aware σ scaling step at the end of `predict()`, applied
**after** the fit-quality gate (which still uses raw `stdev_delta` for the
analog-cluster spread check):

```python
sigma_factor = 1.0
if side == "high":
    sigma_factor = 0.85
elif side == "low" and not extreme_locked and cur_lst_min < 12*60:
    sigma_factor = 0.85
sigma_proj_out = stdev_delta * sigma_factor
```

`predict()` return dict now also exposes `sigma_raw_f` and
`sigma_factor_applied` for diagnostics; `sigma_proj_f` is now the **scaled**
σ that downstream bot logic should consume.

**Why.** Synth-bracket calibration on 71,424 1°F brackets (2024-2025, 20
stns, 9 brackets centered on each cell's actual day extreme) showed nn σ is
systematically too wide on HIGH and pre-dawn LOW cohorts — `pred(in_bracket)`
under-shoots the empirical hit rate. Sweep of {1.00, 0.85, 0.70, 0.55, 0.40}
RMSE_calib by cohort:

```
cohort              factor 1.00  factor 0.85  factor 0.70  factor 0.55  factor 0.40
HIGH                  0.0455      0.0366*      0.0383       0.0580       0.0896
LOW-unlocked-am       0.0593      0.0483*      0.0513       0.0734       0.0981
LOW-unlocked-pm       0.0608*     0.0656       0.0812       0.1321       0.1572
LOW-locked            0.2187      0.1982       0.1829       0.1658       0.1487*
```

(*= best-of-tested factor for that cohort; RMSE is `sqrt(mean((emp-pred)^2))`
across 10 probability bins, lower = better calibrated.)

**Cohort decisions:**
- **HIGH** factor 0.85 → ship (−20% RMSE)
- **LOW-unlocked-am** factor 0.85 → ship (−19% RMSE; covers most live LOW activity, which is the pre-dawn pre-trough window)
- **LOW-unlocked-pm** factor 1.00 → keep raw (shrinking hurts)
- **LOW-locked** factor 0.40 → **NOT shipped this round.** Best of tested factors but residual RMSE 0.149 reflects a bimodal-ish error distribution (50% perfect predictions + asymmetric +1.4°F sub-5min sampling tail). A Gaussian σ alone can't fully calibrate; needs a custom non-Gaussian P(in-bracket) function. Future work.

**Live verification.** Direct-call test on PHX 2024-07-15:
- HIGH cohort: σ_raw 1.32 → σ_proj 1.12 (factor 0.85 applied)
- LOW-locked cohort: σ_raw 3.01 → σ_proj 3.01 (factor 1.0, as designed)

Backups `pre_action_c_20260517_090721` on `nn_match_fast.py` in both
`paper_judge_bot/` and `tools/`.

## nn_match Action A — HIGH lock removed — 2026-05-17 09:01 UTC

Replaced the HIGH-side conditional lock
```
if cur_lst_min >= 16*60 and (cur_lst_min - max_lst_min) > 60:
    mu_proj = traj_max
    extreme_locked = True
else:
    mu_proj = max(mu_proj, traj_max)
```
with the unconditional physical max-floor
```
mu_proj = max(mu_proj, traj_max)
```

HIGH side can no longer return `extreme_locked = True`; LOW side unchanged.

**Why.** Stratify n=32,723 cells across 20 stations × 150 sampled 2024-2025
days × {6,9,12,14,15,16} LST eval hours. At eval hour 16 LST the locked
cohort (peak >60min in the past) had MAE 1.32°F / bias −1.32°F (n=1611) vs
the unlocked cohort 0.97°F / bias 0.00°F (n=1389). Weighted v2-baseline
HIGH-16 MAE 1.16°F vs v3 unconditional-floor 1.06°F — `−9% MAE`, `−56%
bias`. Mechanism: actual day_max often arrives LATER than the locked
trajectory_max snapshot, so locking truncates μ below the eventual true
peak. The `max(mu_proj, traj_max)` floor still prevents nn from predicting
below an already-observed peak.

**Validated alongside, not shipped:**
- **Action B (flat per-cohort bias correction):** rejected. Error
  distributions are one-sided fat-tail — e.g. LOW-22-locked p25=p50=0,
  p75=+0.6, p95=+1.4, mean +0.54. Subtracting the mean shifted errors
  negative; MAE went UP in every cohort tested.
- **Action C (cohort-aware σ×0.85 multiplier):** σ-shrink sweep on 71k
  synth brackets shows rmse_calib drops 19-20% at factor 0.85 for HIGH +
  LOW-unlocked-am, no improvement (or worse) for LOW-unlocked-pm. Worth
  doing but more invasive — held back as a separate ship.
- **Action D (non-Gaussian P for LOW-locked):** synth-bracket calibration
  shows pred 0.40 → empirical 0.83 (massive under-confidence). σ-shrink
  factor 0.40 reduces rmse 0.22→0.15 but residual is bimodal-ish, so
  Gaussian-σ alone can't fully calibrate. Future work.

Backups `pre_action_a_20260517_085827` on `nn_match_fast.py` in both
`paper_judge_bot/` and `tools/`.

## nn_match P1+P2 calibration — bias correction + fit-quality gate — 2026-05-17

Two changes layered on the existing nn_match path:

**P1 — per-side bias correction** (`NN_BIAS_CORR_HIGH_F = +0.99`,
`NN_BIAS_CORR_LOW_F = −1.82`). The matcher's neighbor-median Δ systematically
under-projects HIGH (~−1°F) and over-projects LOW (~+1.8°F) on the bot's
realistic eval-hour distribution. Applied additively to `median_delta` before
`mu_proj` is built, so physical-constraint clamps (peak ≥ traj_max, trough
≤ traj_min) and locked-mode see a calibrated base.

**P2 — fit-quality gate** (`NN_FIT_QUALITY_THRESH_HIGH = 3.0`,
`NN_FIT_QUALITY_THRESH_LOW = 4.0`). When the top-30 neighbor cluster's day-
extreme stdev exceeds the threshold, `predict()` returns None and the bot's
fallback chain (`anchored` / `rm_ceiling` / `consensus_corr`) handles. Tight
HIGH threshold + looser LOW threshold reflect the inherent LOW spread.

Realistic backtest n=1200/side (2018-2025 random sample, HIGH eval 5-14 LST
climate-day morning, LOW eval 0-5 LST climate-day pre-dawn, current F2-enabled
matcher with drct_weight=0.015):

```
HIGH raw 5-min:         MAE 3.05°F  bias −0.99°F   p95 9.50  (100% fire)
  + bias only:          MAE 2.97°F  bias  0.00°F   p95 9.41
  + gate σ≤3.0:         MAE 1.72°F  bias −0.71°F   p95 5.10  (39% fire)
  + bias + gate σ≤3.0:  ~MAE 1.71°F well-calibrated  (39% fire)

LOW raw 5-min:          MAE 2.49°F  bias +1.82°F   p95 9.00  (100% fire)
  + bias only:          MAE 2.51°F  bias  0.00°F   p95 7.39
  + gate σ≤4.0:         MAE 1.97°F  bias +1.36°F   p95 5.80  (78% fire)
  + bias + gate σ≤4.0:  ~MAE 1.94°F well-calibrated  (78% fire)
```

Rejected on same backtest (no measurable lift, kept for record): pressure
analog, detrended/shape matching, multi-layer cloud trajectory, year-decay
weighting. See `tests/test_nn_calibration.py` for backward-compat guarantees.

Locked-mode (extreme already in trajectory) bypasses the gate — the matcher
just returns `mu_proj = traj_max/min`, which is an observed value, not a
neighbor-cluster guess.

Files: `nn_match_fast.py` (new params `bias_correction` + `fit_quality_thresh`,
backward-compatible defaults), `nn_shadow.py` (config-driven per-side wire-up),
`config.py` (4 new constants), `tests/test_nn_calibration.py` (9 new tests).
Tests 215/2-skipped. Backups `*.bak.pre_p1p2_calib_20260517_045043`.

## nn_match primary μ + $5 LOW/HIGH caps — 2026-05-16 evening

Activated `nn_match` — a k-NN heating/cooling curve matcher — as the primary
μ source for both HIGH and LOW. Pulls from
`/home/ubuntu/data/heating_traces.sqlite` (20 stations × 2010-2026 × 5-min
resolution × 17 weather vars).

How it works: given today's hourly obs trajectory (temp + dewpoint), find the
30 most-similar historical days at the same station by curve RMSE
(+ dewpoint, + cloud cover penalty); project μ = today's cur_tmpf +
median(neighbor.day_extreme − neighbor.tmpf_at_cur_bin). When the day's
extreme is already in the trajectory (afternoon peak past for HIGH, morning
trough past for LOW), the matcher locks μ to the observed extreme.

Backtest:
- paper_min_bot settled n=41: nn 1.58°F MAE vs bot_mu 1.96°F (19% better)
- LOW 20-station n=36k: nn 1.50°F vs pace 3.51°F (57% better); evening
  hours nn 0.90 vs pace 4.26
- HIGH 20-station: ~13% better than pace baseline
- Catches forecast busts (nbm_d1_override case: bot 8.10°F → nn 4.30°F)

When `nn_match` returns no projection (trajectory < 60 min, pool < 10),
falls back to anchored (HIGH) / rm_ceiling (LOW) / consensus.

Risk-managed rollout: `max_bet_low_series_usd` added at $5 to mirror
`max_bet_high_series_usd` while validating live. Both HIGH and LOW now
capped at $5 per bet and $5 per ticker total.

Files: `nn_match_fast.py` (numpy matcher, 7 ms/call cached), `nn_shadow.py`
(packet adapter), `/home/ubuntu/tools/HEATING_TRACES_README.md` (full data
pipeline docs). Backups `*.bak.pre_nn_live_20260517_030048`.

**Defaults out of the box**:
  - `WALLET = "v2"` — shares the v2 Kalshi key with obs-pipeline-bot (V2 max)
    and kalshi-min-bot-v2 (V2 min). Co-existence rules below.
  - `JUDGE_BACKEND = "claude_cli"` — uses your Claude Pro/Max subscription via
    the `claude -p` headless CLI. No `ANTHROPIC_API_KEY` needed.
  - `MODE = "observer_only"` + `DRY_RUN = True` — no orders placed until you
    flip these flags in `config.py`.
  - Universe covers BOTH `KXHIGH*` (max-temp markets) and `KXLOWT*` (min-temp
    markets) across all 20 cities the other bots trade.

---

## Why this bot exists

The 4 numerical bots are excellent at the steady-state — they ingest
NBP/NBM/HRRR/ECMWF, blend, calibrate, run a long stack of pipeline-invariant
filters, and execute. They miss situations where:

  - Live point obs reveal what the forecast cascade didn't (e.g., current
    temp == running_min near settlement → "minimum is being made *now*",
    not in the past — see the DC-26MAY13-B54.5 case)
  - Wet-bulb / dewpoint analysis sets a different floor than model σ implies
    (active rain + dewpt 53.6°F → cooling-to-wet-bulb is the real risk)
  - Market spread/volume/recent-prints reveal trader conviction the bot
    can't see (NO bid 27c with a 39c spread is a *signal*, not noise)
  - Multiple weak signals compound into a "this looks wrong" gestalt that
    no single rule-based filter can encode without overfitting

A general-purpose reasoner can pull these threads together in real time,
narrate its read, and act. That's this bot's edge.

It is **not** trying to replicate or out-perform the 4 numerical bots'
steady-state edge. It's after the situations they leave on the table.

---

## Co-existence with the V2 max + V2 min bots

The v2 Kalshi wallet is shared three ways:

| Bot | Service | Series |
|---|---|---|
| V2 max | `obs-pipeline-bot.service` | KXHIGH* |
| V2 min | `kalshi-min-bot-v2.service` | KXLOWT* |
| **judge** | `paper-judge-bot.service` (this) | both |

Without rules, three bots fighting for the same balance + double-buying the
same ticker would be a mess. The rules enforced in code:

1. **Entry loop calls `/portfolio/positions` every cycle.** Any ticker
   with `position != 0` on the wallet is silently skipped — `paper-judge`
   refuses to enter where V2 max or V2 min already has size.

2. **Exit loop only acts on positions we opened.** Each entry row gets
   `opened_by: "paper-judge"`. The exit loop short-circuits on any
   position without that tag. So we never accidentally sell V2 max's
   position out from under it.

3. **Wallet balance is shared.** The bot's daily / per-ticker / per-trade
   caps still apply, but the wallet's actual cash is finite across all
   three bots. Set `DAILY_SPEND_CAP_USD` conservatively (default $200)
   relative to wallet balance.

4. **Rate limit bucket is shared.** Kalshi rate-limits per API key; we
   share with the other two v2 bots. The entry loop's cadence (60s
   default) is loose enough to coexist.

If you ever want to give `paper-judge` its own dedicated key + balance,
flip `WALLET = "own"` and set `KALSHI_KEY_ID` + `KALSHI_PEM_PATH` in `.env`.

## Architecture

```
                       ┌──────────────────────────────────┐
                       │     paper_judge_bot daemon        │
                       │                                   │
   ┌─── shared S3 ─────┤   ENTRY LOOP (every 60s):         │
   │   cache reader    │     1. scan Kalshi LOW+HIGH       │
   │  (nbm/hrrr/       │        markets matching universe  │
   │   ecmwf-ifs.json) │     2. numerical pre-screen       │
   │                   │     3. fetch live obs (NWS)       │
   ├─── NWS obs API ───┤     4. build entry prompt         │
   │   (cached 60s)    │     5. Claude.judge_entry()       │
   │                   │     6. parse, validate, size      │
   ├─── Kalshi REST ───┤     7. guardrails check           │
   │   + WS BBO        │     8. execute or reject          │
   │                   │                                   │
   ├─── obs sqlite ────┤   EXIT LOOP (every 30s):          │
   │   (running_min)   │     for each open position:       │
   │                   │       a. compute MTM, time-left   │
   ├─── bot_decisions ─┤       b. trigger predicates       │
   │   .sqlite (audit) │       c. if triggered: Claude     │
   │                   │          .judge_exit()            │
   ├── Anthropic API ──┤       d. execute sell or hold     │
   │   (Claude         │                                   │
   │    Sonnet 4.6,    │   GUARDRAILS (always):            │
   │    prompt cached) │     - daily $ spend cap           │
   │                   │     - per-ticker size cap         │
   │                   │     - max open positions          │
   │                   │     - no-trade window pre-close   │
   │                   │     - circuit breaker on -$X day  │
   │                   │                                   │
   └─── Discord ───────┤   AUDIT every cycle:              │
       webhook         │     decisions.jsonl + sqlite      │
                       └──────────────────────────────────┘
```

All long-running connections (Kalshi WS, sqlite) live in the same process.
Each loop's body is idempotent so a crash + restart never double-orders.

---

## Decision loop in detail

### ENTRY (one pass per cycle, default 60s)

1. **Universe scan.** Pull all open Kalshi tickers matching `KXHIGH*-26*-B*` /
   `KXHIGH*-26*-T*` / `KXLOW*-26*-B*` / `KXLOW*-26*-T*` for stations in
   `STATIONS` and dates in {today, tomorrow, day-after}. (Universe is
   configurable; default includes all 20 cities both bots already trade.)

2. **Numerical pre-screen.** Reject candidates that clearly don't deserve
   an LLM call:
   - Spread > `MAX_SPREAD_CENTS` (default 25¢) — too illiquid to act on
   - Both yes_ask and no_ask < `MIN_PRICE_FRAC` (default 0.05) or
     > `MAX_PRICE_FRAC` (0.95) — already crushed
   - Time-to-close < `MIN_TIME_TO_CLOSE_MIN` (default 30 min) — no
     room to be wrong
   - Existing position on this ticker — defer to EXIT loop

   This is cost control, not edge. A pre-screen reject is logged but
   never causes a missed entry that the prompt would have caught.

3. **Live data assembly.** For each surviving candidate, gather in parallel:
   - Current NWS obs at the station: temp, dewpt, sky, wind, ts
   - Last ~6 obs (30 min trailing) for trend direction
   - Latest NBM + HRRR forecast for the climate day from shared cache
   - Running_min/max from obs-pipeline sqlite
   - Kalshi BBO from WS cache (yes_bid/ask, no_bid/ask, spread, volume,
     last 5 prints if available)
   - Climate-day-close UTC time + minutes remaining

4. **Prompt build + Claude call.** See `prompts/entry_prompt.md`. The
   static portion (RULE #2 from CLAUDE.md, recent settlement summary,
   per-station idiosyncrasies, output schema) is sent with
   `cache_control: ephemeral` so the first call of the cycle warms the
   cache and subsequent candidates hit it. ~10K input tokens cached,
   ~1.5K dynamic per candidate, ~800 output tokens. With Sonnet 4.6
   pricing that's ~$0.012 per entry decision after warmup.

5. **Response parse + validate.** Claude returns strict JSON:
   ```json
   {
     "decision": "BUY_NO" | "BUY_YES" | "SKIP",
     "conviction": 0.0 - 1.0,
     "size_factor": 0.0 - 1.0,
     "read": "...one paragraph...",
     "key_risks": ["...", "..."],
     "what_would_change_my_mind": "..."
   }
   ```
   Schema mismatches → forced SKIP. Unknown decisions → forced SKIP.
   `size_factor` is multiplied by `MAX_BET_USD` to size the bet.

6. **Guardrails check** (see "Safety" below).

7. **Execute** via `kalshi_client.place_order()`. Log to decisions.jsonl,
   bot_decisions.sqlite, and Discord.

### EXIT (one pass per cycle, default 30s)

For each open position:

1. **Compute live state**: MTM%, time-to-close, current_rm vs bracket,
   live market BBO, recent prints.

2. **Trigger predicates** — only call Claude if at least one fires
   (saves cost, focuses LLM on situations the bot's snapshot view
   may be wrong about):
   - `mtm_swing`: |current_mtm - peak_mtm| > 30%
   - `rm_near_boundary`: running_min within 1.5°F of cap (BUY_NO) or floor (BUY_YES)
   - `time_to_close`: <90 min remaining AND position size > $20
   - `spread_widening`: spread doubled vs entry
   - `obs_anomaly`: current temp equals running_min AND time-to-close < 3h
     (the DC pattern — minimum is being made now)

3. **Live data assembly** — same as entry but tailored to exit context.

4. **Claude call.** Returns:
   ```json
   {
     "decision": "HOLD" | "SELL_ALL" | "SELL_PARTIAL",
     "sell_count": int,
     "limit_price_cents": int,
     "conviction": 0.0 - 1.0,
     "read": "...",
     "regret_check": "if this turns out wrong, what's the most likely reason?"
   }
   ```

5. **Guardrails check** (sell-side has weaker guardrails — selling is
   generally safer than buying).

6. **Execute** via `kalshi_client.place_sell()`.

---

## Safety

Multi-layer. The LLM is the *least*-trusted component; everything else is
deterministic.

### Layer 1: Mode flags (set in `config.py`, runtime-flippable)

| Flag | Default | Effect |
|---|---|---|
| `MODE` | `"observer_only"` | observer_only \| trader \| killed |
| `DRY_RUN` | `True` | If True, log orders but never call Kalshi |
| `ENABLE_BUYS` | `True` | If False, only sells allowed |
| `ENABLE_SELLS` | `True` | Should rarely be False (emergency exits) |
| `KILL_SWITCH_FILE` | `~/paper_judge_bot/KILL` | If file exists, bot won't take any action this cycle |

**`observer_only` is the default.** First week of operation: bot scans,
analyzes, posts decisions to Discord. No orders. Chris validates Claude's
calls against settled outcomes. Flip to `trader` only after vetting.

### Layer 2: Budget + sizing hard caps (`guardrails.py` + balance clamp)

| Cap | Default | Notes |
|---|---|---|
| `max_bet_usd` | $15 | Per single buy order. Final `count` is clamped by both this cap AND the live wallet (see below). |
| `max_ticker_total_usd` | $20 | Cumulative cost across all addons on one ticker |
| `daily_spend_cap_usd` | $300 | Resets at UTC midnight |
| `max_open_positions` | 999 | Effectively unlimited per user request |
| `min_price_cents` | 5 | Don't buy below 5c (no Kelly value, fee-dominated) |
| `max_price_cents` | 90 | Don't buy above 90c (10c upside, asymmetric loss) |

Any candidate buy that would violate these is rejected pre-execute with
a logged reason. No LLM override.

**Balance-aware sizing (added 2026-05-15)** — the V2 wallet is shared with
V2 max + V2 min. `execute_buy()` now refreshes the cached Kalshi balance
before every order (`kalshi_client.get_balance_cached()`, 15s TTL) and caps
`count` to the largest size the wallet can afford:

```
max_affordable = floor(balance_cents / ask_cents)
final_count    = min(intended_count, max_affordable)
```

If `max_affordable < 1` (wallet can't cover even one contract at the ask),
the candidate is **skipped + Discord-notified** rather than POSTed to
Kalshi. If a sibling bot eats the cash between our `get_balance` and
`POST /orders` (race → 400 `insufficient_balance`), the balance cache is
invalidated, refreshed, and the order retried once with the new clamp.
After every successful buy the cache is invalidated so the next candidate
in the same cycle sees the post-debit wallet.

**Discord on every attempt (added 2026-05-15)** — `execute_buy()` now
posts a Discord notification for **every** outcome of every buy attempt:

| Emoji | Meaning |
|---|---|
| 🎯 ATTEMPT | About to submit order — includes count, price, wallet, conv, size |
| ⛔ SKIP | Pre-submit reject: wallet < ask, no balance yet |
| ⛔ REJECTED | Guardrail (price band, daily cap, ticker cap, etc.) |
| ♻️ RETRY | First attempt hit insufficient_balance; retrying with new clamp |
| ❌ FAILED | Submission rejected (with Kalshi error code/message) |
| ⚠️ NOT FILLED | Order accepted but didn't execute → cancelled |
| ✅ filled | Position opened — existing success notification |

This replaces the previous behavior where failed buys were silently logged
to journald only, leaving Chris blind to e.g. an insufficient_balance run.

### Layer 3: Time-window guards

  - **No new buys < 30 min before climate-day close** — bot's edge is in
    early-cycle judgment, not last-second scrambles. The market has
    digested everything by then.
  - **No sells > 6h before close UNLESS triggered by an obs anomaly** — selling
    early forfeits the bot's hold-to-settlement edge (RULE #2).
  - **Cooldown** after a sell on a ticker: 30 min before any re-entry on
    same ticker. Prevents Claude oscillation.

### Layer 4: Circuit breaker

  - **Daily realized P&L < -$100**: bot enters `MODE=killed` automatically;
    sends Discord alarm. Only Chris can flip back to `trader`.
  - **3 consecutive Claude calls return parse errors / timeouts**: pause
    new entries for 5 min. After 15 min of failures, switch to
    `observer_only`.
  - **Anthropic API spend tracking**: if today's spend > `MAX_API_SPEND_USD`
    (default $5), pause LLM calls and fall back to "hold all positions,
    no new entries."

### Layer 5: Decision logging

Every Claude call (including SKIPs and parse failures) is logged to:
  - `data/decisions.jsonl` — full request+response with timestamps
  - `bot_decisions.sqlite` — joined with other 4 bots' decisions for
    cross-comparison

If a position later loses, the audit trail shows exactly what Claude saw
and said. This is the dataset for tuning the prompt over time.

---

## Token-saving changes — 2026-05-15

Following high HOLD rate on exit Claude calls (246/250 = 98%) and high SKIP rate on entry calls (838/1023 = 82%), the bot was tightened to spend tokens only where they have edge:

| Change | Where | Why |
|---|---|---|
| `ENABLE_SELLS: False` | `config.py:52` | Exit Claude calls were 98% HOLD — pure overhead. Bot now holds to settlement (matches design intent per RULE #2). |
| `min_numerical_edge: 0.03 → 0.08` | `config.py:188` | Claude self-applies an 8pp internal floor — we were paying him to apply it. Pre-kills ~40% of entry SKIPs. |
| `max_buys_per_station_side: 2 → 1` | `config.py:215` | One HIGH + one LOW per station max. Cuts correlation-clustered losses (e.g. CHI B62/B64/B66 cluster, only 1/3 won). |
| `ENTRY_CYCLE_SEC: 1500 → 900`, `PEAK_CYCLE_SEC: 600 → 900` | `config.py:157-158` | Unified 15-min cadence. PEAK/NORMAL distinction kept in code but both at 15 min. |
| μ-distance pre-gate | `paper_judge_bot.py` prescreen | Skip before Claude when forecast median is >10°F outside the bracket — those markets are locked and Claude has nothing to add. |
| Exit loop gated by `ENABLE_SELLS` | `paper_judge_bot.py` main loop | When sells are disabled, skip the entire exit-loop body (no per-position Kalshi fetches every cycle). |

Reversal: restore `paper_judge_bot.py.bak_token_reduce_20260515` + `config.py.bak_token_reduce_20260515`.

## F1: stale-rm defense + 1h LDT-midnight grace — 2026-05-16

Background: 5/7 rm-having LOW BUY_NO losers from 2026-05-15 (-$19.46) entered
pre-LDT-midnight while the previous `cand.climate_day == today_utc` check
silently passed. The wethr cache `low_f` at decision time was yesterday's
running min, not today's. Two more losers (-$14.15) entered within the first
hour of LDT midnight when rm reflected obs at the very start of cooling.

| Layer | Where | What it does |
|---|---|---|
| Validator | `wethr_rm.py` | New `validate_rm_for_climate_day(station, climate_day, cache_date, time_of_extreme_utc, now_utc_ts, grace_sec=3600)` — DST-aware (ZoneInfo, PHX=year-round-MST). Checks (1) cache.date == climate_day, (2) time_of_extreme within LDT window, (3) now ≥ LDT_midnight + grace. Returns `{ok, reason, ldt_midnight_ts, secs_into_climate_day, ...}`. |
| Propagation | `live_data.py` | Prefetch also passes through `wethr_running_date`, `wethr_time_of_low_utc`, `wethr_time_of_high_utc` per station. |
| Assignment | `paper_judge_bot.py:317` build_entry_packet | `rm_val=None` unless validator passes. Stamps `rm_validation` into packet for telemetry. |
| Grace gate | `paper_judge_bot.py` prescreen | Hard-blocks d+0 entries when `0 <= secs_into_climate_day < 3600` (reuses `rm_validation` already in packet — no double computation). |

**Behavioral change**: bot will now refuse `rm_val` for ~4h of every day per
station (pre-LDT-midnight + 1h grace). For ATL that's 00:00–05:00 UTC in EDT
or 00:00–06:00 UTC in EST; for SFO/SEA/LAX 04:00–08:00 UTC PDT or
05:00–09:00 UTC PST. d+1 candidates (late-evening preview window) will see
rm=None, which is correct — there is no climate-day anchor for tomorrow yet.

**Test coverage**: 40 new tests in `tests/test_rm_validation_and_prescreen.py` —
LDT midnight per timezone, DST transition days, PHX year-round MST, every
loser scenario from 2026-05-15 reproduced + ±1s boundary cases at grace and
LDT window edges. 173/175 total pass (2 F2-block tests skipped pending F2
re-enable, see below).

**F2 (cold-tail BUY_NO warming-obs) — coded but commented out at ship time.**
Block in prescreen targets T-bracket LOW with floor set/cap=None when
`obs_trend_30m >= +0.3°F/30m` AND `obs > floor+1°F` AND market leans NO AND
in cooling phase. Would have caught CHI T51 (-$8.64) + MIN T59 (-$5.16) from
2026-05-15. To re-enable: uncomment the F2 block in prescreen and remove the
`@pytest.mark.skip` decorators on the two `test_blocks_*` tests.

Backups: `wethr_rm.py.bak.pre_f1_stale_rm_20260516`,
`live_data.py.bak.pre_f1_stale_rm_20260516`,
`paper_judge_bot.py.bak.pre_f1_stale_rm_20260516`.

## Pace-slope FC snapshotting + wethr-as-truth prompt — 2026-05-16 17:35 UTC

Four interlocking fixes for SKIP-reasoning quality.

**Bug surfaced 2026-05-16**: SKIP reads on KXHIGHPHIL-26MAY16-B82.5,
KXHIGHTNOLA-26MAY16-B84.5 and others were anchoring to NWS gridpoint
forecast as if it were truth, dismissing pace_band projections as
"physically implausible", and gating 60m regression on strict r²≥0.7.
Root-cause audit: `obs_vs_forecast_pace_slope` was **null in 100% of
16,286 candidates that day** because NWS gridpoint returns future-only
hours but `hourly_obs_today` covers past-only hours — they never
overlap by more than 1 hour, so the function always returned None.
That forced the LLM onto single-hour eyeball deltas vs the gridpoint
and onto fallback paths that ignored the wethr-side signals.

### Fix 1 — Rolling NWS gridpoint forecast snapshots

New module `nws_fc_history.py` (~150 lines):

  - `record_snapshot(station, hourly_fc, now_ts)` appends each
    `(snapshot_ts, target_iso, temp_f)` row to
    `data/nws_fc_history/{station}.jsonl`.
  - `get_fc_for_hour(station, target_iso, before_ts)` returns the
    most-recent forecast temp for `target_iso` from snapshots taken
    at or before `before_ts`.
  - 48h retention; prune-on-write; corruption-tolerant.

`live_data.fetch_live` calls `record_snapshot` right after
`nws_grid.get_hourly_forecast` each cycle. `paper_judge_bot.build_packet`
passes `fc_lookup_fn` into `compute_obs_vs_forecast_pace_slope`, so past
obs hours now match the snapshot that was valid AT that past hour.

Pace-slope starts resolving after ≥3 climate-day hours of obs have
accumulated since the latest restart (the regression needs 3 matched
obs-vs-snapshot pairs). For a typical restart that's ~2-3h to first
non-null pace_slope; from then on every cycle should populate.

### Fix 2 — prompt: wethr is TRUTH

New Step 5 preamble in `prompts/entry_prompt.md`:

  - `wethr_obs` + `wethr_high_f` / `wethr_low_f` are TRUTH (0.13°F MAE
    vs NWS METAR pipeline 0.42°F, 3x more accurate).
  - `hourly_forecast_24h` (NWS gridpoint) is a FORECAST that can be
    busting in real time. Reads must say "forecast is busting cold by
    X°F", not "obs is X°F ahead of pace" (which falsely treats the
    forecast as the reference).
  - Every read MUST cite at least one wethr-derived signal by value.
    Reads anchored purely to forecast μs forfeit the bot's edge.

### Fix 3 — heat-outlier / cold-outlier guardrails

Step 7 SATURATION cases were being misread as "fall back to NBM/HRRR"
when pace_band/pace_low_band were saturated. New explicit guardrails:

  - HIGH d+0 heat outlier: when `rm` exceeds the cohort's typical
    projection, do NOT revert to stale NBM/HRRR. Use `rm + tail_band.p75`
    (or p90 for the hot tail).
  - LOW d+0 cold outlier: symmetric — use `rm − tail_low_band.p75/p90`.

### Fix 4 — 60m regression r² gate loosened

Old prompt rule: "decision-grade only when r²≥0.7". New: weight slope
by r² as a continuous signal, paired with `temp_history_range_60m.range_f`:

  - r² ≥ 0.7: high-confidence trend.
  - r² 0.3–0.7 AND range_f ≥ 2°F AND slope sign matches diurnal direction:
    trend is real even at moderate r².
  - r² < 0.3 AND range_f ≥ 2°F: volatile regime — cite the magnitude
    and slope sign, not the slope value.
  - r² < 0.3 AND range_f < 1.5°F: ignore the slope sign.

### Backups + rollback

All pre-fix files tagged `pre_paceslope_fix_20260516_173446`:
- `shared_cache_reader.py`, `live_data.py`, `paper_judge_bot.py`,
  `prompts/entry_prompt.md`.

Rollback: restore the four `.bak.pre_paceslope_fix_20260516_173446`
files, delete `nws_fc_history.py` and `data/nws_fc_history/`, restart
service.

### Tests

195 pass + 2 skipped (same baseline as before the fix). 8 new unit
tests cover `nws_fc_history.record_snapshot` + `.get_fc_for_hour`
(empty input, missing fields, snapshot ordering, before_ts cutoff,
ISO suffix normalization, prune-on-write, corrupt-file tolerance).
Integration test on real KPHL hourly_obs (14h history) confirmed
`slope_per_h=+2.28°F/h, current_gap=+1.6°F` with `fc_lookup_fn`
populated, vs `None` without it. End-to-end dry-run import-clean.

---

## Gap ceiling + market confidence tightening — 2026-05-16 06:11 UTC

Settled-snapshot accuracy analysis on all enriched candidates since the
bracket fix (2026-05-15 20:34 UTC, n=2127 across 87 tickers) showed:
- Brier score: market 0.106 vs model 0.183 (market is 1.7× better-calibrated).
- When market & model disagree (n=584): market right 73%, model right 19%.
- Largest single mispricing bucket = +25 to +50pp gap (model >> market),
  n=80, market right 87.5%. Model only "wins" big in the ±8pp band where
  both agree to within statistical noise.
- Market confidence: when dominant side ask ≥ 60c, market right 88%.
  Below 60c, only 64%.

| Change | Where | What it does |
|---|---|---|
| `max_numerical_edge_gap: 0.60 → 0.25` | `config.py` | Rule#2 ceiling. Blocks any side where `(model_prob − market_implied) > 0.25` unless rm-locked. First-minute restart: 3 fires (gaps 63/78/61pp). |
| `min_market_confidence_cents: 0 → 60` | `config.py` | Blocks pre-LLM when `max(yes_ask_c, no_ask_c) < 60` — the "undecided market" zone where the model's directional pick is most often wrong. |

Rule#2 still bypasses on rm-lock via `_is_rm_locked_for_side` (overshoot or
stays-below-past-peak/past-min). Backup `config.py.bak.pre_tighten_gap_mktconf_20260516`.

Tests: 191 pass + 2 F2 skipped. Test-data updates required (base packets
needed to produce edge ≥ 6pp AND ≤ 25pp AND dom-ask ≥ 60c simultaneously).

## Data sources

| Source | Module | Notes |
|---|---|---|
| Kalshi markets/positions/orders | `kalshi_client.py` | RSA-PSS signed REST + WS BBO cache. Auth via PEM at `~/paper_judge_bot/kalshi_key.pem`. Wallet configurable. |
| NWS live obs | `obs_client.py` | `api.weather.gov/stations/{ID}/observations/latest`. Cached 60s per station to avoid rate-limiting (limit 5 req/sec). Falls back to MADIS via obs-pipeline sqlite if NWS is down. |
| Forecast cache | `shared_cache_reader.py` | Reads `/home/ubuntu/shared_cache/{nbm,hrrr,ecmwf-ifs}.json`. Same merge logic as min/max bots. |
| Running min/max | obs-pipeline sqlite | Read-only. Same path as min bot uses. |
| Claude (LLM) | `judgment.py` | Two backends: `claude_cli` (uses subscription via `claude -p`, default) or `anthropic_sdk` (uses `ANTHROPIC_API_KEY`, prompt-cached). |
| Audit | `bot_decisions.sqlite` | Via `shared_tools/decision_log.record()`. Bot name `paper-judge`. |
| Notifications | `discord.py` (stdlib + webhook) | Channel configurable per env. |

---

## Operational runbook

### First-time deploy (DRY_RUN observer mode)

```bash
# 1. SCP the directory
scp -i ~/.ssh/kalshi-bot-key -r paper_judge_bot ubuntu@54.225.174.220:~/

# 2. Configure
ssh -i ~/.ssh/kalshi-bot-key ubuntu@54.225.174.220
cd ~/paper_judge_bot
cp .env.example .env
$EDITOR .env  # set ANTHROPIC_API_KEY, KALSHI_KEY_ID, DISCORD_WEBHOOK_URL

# 3. Install systemd unit (does NOT start the service)
sudo cp paper-judge-bot.service /etc/systemd/system/
sudo systemctl daemon-reload

# 4. Run a one-shot dry-run cycle to test plumbing
python3.12 -m paper_judge_bot --once

# 5. Start as a service
sudo systemctl enable --now paper-judge-bot.service

# 6. Watch logs
journalctl -u paper-judge-bot -f
```

The bot starts in `observer_only` mode by default. Decisions go to Discord
+ `decisions.jsonl` but no orders are placed.

### Promoting to trader

After ≥7 days in observer_only:

```bash
# Edit config
$EDITOR ~/paper_judge_bot/config.py  # set MODE = "trader", DRY_RUN = False

# Restart
sudo systemctl restart paper-judge-bot.service
```

Verify per RULE #3c snippet (see CLAUDE.md): single PID, code mtime ≤ start.

### Kill switch

```bash
# Immediate halt (no new actions until file is removed)
touch ~/paper_judge_bot/KILL

# Resume
rm ~/paper_judge_bot/KILL
```

The bot checks for this file every cycle. Existing open positions are *not*
closed — kill switch only stops new actions.

### Monitoring

  - **Discord**: every entry, sell, and SKIP-with-conviction>0.6 posts a one-liner.
  - **journalctl -u paper-judge-bot**: full stdout/stderr.
  - **`tail -F data/decisions.jsonl`**: raw decision stream.
  - **`tail -F data/trades.jsonl`**: filled entries/exits only.
  - **Weather dashboard**: planned integration (see "Backlog").

### Daily reconciliation

`tools/judge_daily_summary.py` (planned) reports:
  - Entries today / sells today / P&L
  - Win rate vs. Claude's stated conviction (calibration)
  - Top 3 best calls + top 3 worst calls
  - Claude API spend vs. realized P&L

---

## Prompt design notes

See `prompts/entry_prompt.md` and `prompts/exit_prompt.md` for the actual
templates.

The static portion of each prompt is ~10K tokens covering:
  - Bot's role + objective (one paragraph)
  - RULE #2 from CLAUDE.md (market-is-right doctrine) — non-negotiable
  - Recent settlement context (last 7 days' P&L by station)
  - Per-station idiosyncrasies (heat-island bias, dewpoint floors, etc.)
  - Output JSON schema with examples
  - Few-shot exemplars (2-3 hand-picked good decisions + 1 SKIP)

This block is sent with `cache_control: {"type": "ephemeral"}` so it costs
the full ~10K only on the first call of a cache window (5 min TTL).
Subsequent calls cost ~1.5K input + ~800 output.

The dynamic portion (~1.5K tokens) per candidate is purely the situation:
position state, live obs, forecast snapshot, market BBO, climate timing.

### What the prompt does NOT include

  - Long bot logs / source code (use for debugging via separate Claude Code
    sessions, not in trading prompts)
  - Per-trade backtest results (would bias toward overfitting to recent pool)
  - Other bots' recent decisions (would create herding, defeats the
    independence-of-judgment edge)

---

## Sample decision (manual reconstruction of the DC case)

**Situation packet sent to Claude:**

```
TICKER: KXLOWTDC-26MAY13-B54.5
BRACKET: LOW in [54, 55) for KDCA on climate-day 2026-05-13
TIME: 2026-05-14T02:35:00Z (2h 25m to climate-day close at 05:00 UTC)
POSITION: BUY_NO 103 @ $0.58 = $59.74 cost (2× MAX_BET_USD — already loaded)
LIVE MTM: -53% (bid 27c, ask 66c, spread 39c — wide)
RUNNING_MIN: 57.2°F (set at morning dawn ~10:30 UTC)
LIVE OBS (KDCA 02:35Z): temp 57.2°F, dewpt 53.6°F, rain+fog, wind 29mph
RECENT TREND: temp held 57.2-58.5°F since 22:00Z; not dropping yet
FORECAST: NBM tonight low 55.5°F, HRRR 57.6°F, model_prob (current) 0.19
MARKET: yes_ask 73c, no_ask 66c; recent prints (last hr): 0.49, 0.51, 0.47
```

**Claude response (target shape):**

```json
{
  "decision": "HOLD",
  "sell_count": 0,
  "limit_price_cents": null,
  "conviction": 0.62,
  "read": "Running_min is 57.2°F but current temp is also 57.2°F — the minimum is being made right now, not in the past. Wet-bulb floor with dewpt 53.6°F is ~55°F, exactly at cap. Active rain + 29mph wind currently mixes the boundary layer (suppressing radiative cooling), so the wet-bulb-route isn't engaging yet. Market priced fairly (~50% NO via midpoint) given this. Bot's 80% NO conviction is overconfident. However selling at 27c locks in -53% and the position is already past the point where the bot would have skipped on a 2.2°F margin. Hold and accept the bet.",
  "regret_check": "If a cold front passes in the next 90 min and clears the rain, radiative cooling could drop temp to ~54°F. That's the most likely path to a loss. If I see temp drop below 56°F before 04:00Z I'd revisit selling at then-current bid."
}
```

This is *not* a "sell now" recommendation — but it's a high-quality read of
the situation, and it'd surface the regret_check trigger to the next cycle.

---

## Implementation backlog (post-MVP)

  - Per-station prompt context blocks (e.g., KAUS heat-island, KLAX coastal
    layer dewpoint floor)
  - NWS Area Forecast Discussion ingestion for frontal/synoptic context
  - Calibration audit: track Claude's stated conviction vs. realized outcome
    over 100 decisions, surface calibration curve
  - Whisper-mode: Claude can suggest a position size change on an open
    position without selling (i.e., add to a winner). Off by default.
  - Cross-bot decision joins: query `bot_decisions.sqlite` for what the
    other 4 bots decided on the same ticker, include in prompt context.
  - Shared Kalshi client module (refactor across all 5 bots).

---

## Files in this directory

```
paper_judge_bot/
├── README.md                 ← this file
├── paper_judge_bot.py        ← main daemon
├── config.py                 ← all tunables + env loading
├── guardrails.py             ← hard safety rules (LLM-bypassed)
├── judgment.py               ← Claude API + prompt builders + parser
├── kalshi_client.py          ← auth, REST, order placement
├── shared_cache_reader.py    ← reads /home/ubuntu/shared_cache/
├── obs_client.py             ← NWS live obs with caching + MADIS fallback
├── market_universe.py        ← ticker discovery + filtering
├── state.py                  ← positions, orders, decisions persistence
├── .env.example              ← env var template
├── paper-judge-bot.service   ← systemd unit
├── prompts/
│   ├── entry_prompt.md       ← static + dynamic template for entries
│   └── exit_prompt.md        ← static + dynamic template for exits
├── tests/
│   ├── test_guardrails.py
│   ├── test_judgment_parser.py
│   ├── test_state.py
│   └── test_market_universe.py
└── data/                     ← runtime files (gitignored)
    ├── positions.json
    ├── trades.jsonl
    └── decisions.jsonl
```
