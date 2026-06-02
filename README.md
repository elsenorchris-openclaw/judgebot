# paper_judge_bot — Blend-Forecast Kalshi Weather Bot

> ⚠️ **This is NOT the old "judgebot."** It no longer uses NN-matching as its primary
> forecast, and it has **no LLM/Claude in the decision path**. It trades off a
> **supervised blend forecast**. The in-code comments and docstrings are largely
> **stale** (legacy LLM/NN-matching era) — trust the code and this README, not the comments.

Live bot on EC2 `54.225.174.220`, dir `~/paper_judge_bot`, systemd service
`paper-judge-bot.service`, repo `github.com/elsenorchris-openclaw/judgebot.git`
(branch `main`). Trades daily HIGH/LOW temperature brackets on Kalshi
(`KXHIGH*` / `KXLOW*`). Shares the **v1max Kalshi wallet** with `locklag_bot`.

---

## What it forecasts — the BLEND (the edge)

For each station-day it predicts the CLI settlement temperature with a supervised
ridge regression (`blend_forecast.py`, fitted models in `blend_model_{high,low}.json`):

```
settled ≈ market_implied_μ + running_extreme(wethr obs) + cur_temp + 7 OpenMeteo NWP models + nwp_spread
```

predicted with a **fixed calibrated σ** (~1.17°F HIGH / ~1.51°F LOW). The edge is
that the market is under-confident (its implied σ is too wide) and under-weights
the multi-model NWP. It bets a bracket when the blend's P(bracket) disagrees with
the market by the edge floor, at the deep window (peak−4..−2.5h HIGH, min−3..−1.5h
LOW), where the market is softest. See `memory/project_blend_edge_FOUND_20260601`.

**NN-matching (`nn_match_fast`) is now only a FAIL-SAFE FALLBACK** μ, used when the
blend returns None (e.g., market-implied μ unavailable on a thin market, or NWP
fetch fails). The blend (`mu_method="blend_*"`) is the primary; the matcher
(`mu_method="nn_match_*"`) is the backstop.

---

## Architecture (the LIVE path)

Event-driven, **not** cycle-driven. The brain is **`nn_shadow_worker.py`**, triggered
by WebSocket BBO changes (`kalshi_ws`) and wethr-cache pushes.

```
_evaluate_ticker (nn_shadow_worker)
  → _build_shadow_packet         market BBO + wethr obs (running max/min, cur temp) + local clock
  → _check_adverse_drift_exit    the ONLY sell path (first-hour stop-loss; else hold to settlement)
  → nn_shadow.shadow_nn_proj     matcher μ  (the FALLBACK)
  → _compute_blend_override      blend μ/σ (the PRIMARY): blend_forecast.blend_mu(
                                   market_mu=_compute_market_mu, nwp=_compute_blend_nwp[7-model OpenMeteo],
                                   running_extreme, cur_temp); LOW also has a forecast-min-hour lock
  → nn_shadow_strategy.pure_nn_decide   μ-agnostic: truncated-normal P(YES), edge=p−ask, pick side
  → _try_auto_execute            ~25-gate stack + decision window (below)
       HIGH → paper_judge_bot.execute_buy   (crosses the ask)
       LOW  → low_post_probe.place          (posts maker-at-mid)
```

`paper_judge_bot.py` is now mostly **maintenance** (`one_cycle` every 120s:
reconcile positions, resolve settlements, hourly summary) plus the shared
`execute_buy` / `execute_sell`. **Dead code** (never reached, kept for history):
`run_entry_loop`, `run_exit_loop`, `build_entry_packet`, and all `judgment.judge_*`
LLM calls (`LLM_DISPATCH_MODE="off"`, `ENABLE_LLM_EXIT_LOOP=False`).

### Decision window (`_in_decision_window`)
Trades only when `local_hour ∈ [peak−before, peak+after]`. Peak hour from the 5yr
P50 fractional table (`/home/ubuntu/data/peak_fractional_5yr_10day.json`). When the
blend is on, `BLEND_DEEP_WINDOW_HOURS` overrides to the deep window
(HIGH `(4.0, 2.5)` → peak−4..−2.5h; LOW `(3.0, 1.5)` → min−3..−1.5h).

### Gate stack (`_try_auto_execute`, in order; current live values)
edge floor (NO `PUSH_MIN_EDGE_PP=2` / YES `=2`) · in-bracket tail-bet gate (`=25`)
· direction/series toggles · per-station bench (empty) · NWP-agreement gate (off)
· cell-MAE gate (off) · **decision window** · HIGH spread ≤25c / **LOW spread ≤1c**
· thin-margin-NO (off) · NBM-veto (off) · σ-floor (1.0, per-station floors exempt
for blend) / σ-ceiling (2.5) · physics (vsby<0.5mi, wind>40mph) · LOW front-wind
(≥18mph) · price band (NO≥25 / YES≥30 / LOW-NO≥10 / ≤90) · HIGH off-peak veto ·
position dedup · 1-per-(station,series,dir,day) cap · cash · correlation cap.

### Sizing
HIGH base `PUSH_HIGH_MAX_BET_DEFAULT=$5` (NO), `PUSH_HIGH_YES_MAX_BET_USD=$5`;
LOW `max_bet_low_series_usd=$1`; `min_buy_usd=0.40`; MAE-confidence shrink + edge-band
tilt applied; `qty = budget // price`.

---

## Current config status (2026-06-02) — a LIVE, UNPROVEN experiment

The gates were aggressively **loosened** ($1→$5 live experiment): edge floor 18→2,
price floor 50→25, ceiling 80→90, spread 15→25 (HIGH), thin-margin/NBM/MAE gates
**off**, KLAS un-benched, σ-floors exempt for blend; size $1→$5 HIGH (LOW stays $1).

- **Backtest (blend μ, fwd-chain, $5, net fee, HIGH):** loosened **+$3,360** (liquid≤2c
  +$1,373) vs original-gates +$686 — loosening ~5× the total sim P&L (lower per-trade
  edge 11.3 vs 17.8¢/ct, ~9× volume; liquid subset holds). ~7 trades/day projected.
- **No live confirmation.** The blend was **dead-gated until 2026-06-02** (see below),
  so it has **zero settled trades**. The bot's −15.5% historical realized P&L is the
  **matcher** era, not the blend. Treat every backtest number as a hypothesis until
  live settled P&L confirms it.

### History: the dead-gated-blend bug (fixed 2026-06-02)
The worker computed the blend and set `mu_method="blend_*"`, but `pure_nn_decide`
(`nn_shadow_strategy.py`) gated to `mu_method.startswith("nn_match_")` only → it
**SKIPped every blend row** → the blend never executed and the bot ran on matcher μ
the entire time. Fixed by accepting `blend_` too. The matcher remains the fallback.

---

## Operations

- **Restart after any change:** `sudo systemctl restart paper-judge-bot.service`
  (then commit + push — `restart ≠ done`).
- **Tests:** `python3.12 -m pytest tests/ -q` (488 tests). Use **`python3.12`**, not
  `python3` (system python3 is cryptography 3.4.8 and breaks Kalshi request signing).
- **Realized P&L = Kalshi settlement truth** via `kalshi_client.list_settlements`
  (run with `python3.12`). Do **not** judge edge from obs/`running_max`, MTM, or the
  old trade log (RULE #2: the market is right, our obs runs warm vs CLI).
- **Discord:** general feed via `discord_send`; dedicated **buys+errors feed** via
  `notify_trade` → channel `1511264871151304725` (`DISCORD_TRADE_CHANNEL_ID`).
- **Secrets** (in `.env`): `KALSHI_*`, `OPEN_METEO_API_KEY`, `WETHR_API_KEY`,
  `DISCORD_BOT_TOKEN`/`DISCORD_CHANNEL_ID`. Never print them.
- **Local backtest tools** (Chris's machine): `~/judge_dyn/` —
  `cand_resolution.sqlite` (HIGH candidates×price×settlement), `anen_models.sqlite`
  (7-model OpenMeteo archive, 20 stations incl. KMSP), `sim_high2.py`, `featblend.py`,
  `peak_frac.json`.

## Key files
`nn_shadow_worker.py` (live brain) · `blend_forecast.py` + `blend_model_*.json`
(the blend) · `nn_shadow_strategy.py` (`pure_nn_decide`) · `nn_shadow.py` +
`nn_match_fast.py` (matcher fallback) · `config.py` (all knobs) · `paper_judge_bot.py`
(maintenance + execute_buy/sell) · `kalshi_ws.py`/`kalshi_client.py` (exchange) ·
`wethr_client.py`/`wethr_rm.py`/`shared_cache_reader.py` (obs) · `push_window_overrides.py`
(windows) · `low_post_probe.py` (LOW maker) · `guardrails.py`/`state.py` (risk/persist).
