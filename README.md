# paper_judge_bot — Blend-Forecast Kalshi Weather Bot

> ⚠️ **This is NOT the old "judgebot."** It no longer uses NN-matching as its primary
> forecast, and it has **no LLM/Claude in the decision path**. It trades off a
> **supervised blend forecast**. The in-code comments and docstrings are largely
> **stale** (legacy LLM/NN-matching era) — trust the code and this README, not the comments.

Live bot on EC2 `54.225.174.220`, dir `~/paper_judge_bot`, systemd service
`paper-judge-bot.service`, repo `github.com/elsenorchris-openclaw/judgebot.git`
(branch `main`). Trades daily HIGH/LOW temperature brackets on Kalshi
(`KXHIGH*` / `KXLOW*`). Shares the **v1max Kalshi wallet** with `locklag_bot`.

> 🛑 **HALTED 2026-06-09 (`KILL` file in repo root — hot-checked, delete to resume).**
> Blend era 6/2–6/9 = **−$156 net** (settled + 6/9 book-resolved). **Every cell is
> live-negative**: HIGH NO −3.1c/ct (n=115) · HIGH YES −14.1c/ct (n=55, disabled 6/9)
> · LOW NO −16.0c/ct (n=19) · LOW YES −15.1c/ct (n=3, disabled 6/6). The first day
> under the $5 NO-only ≥10pp config (6/9) lost −$33.74 (6W/11L) — the "+9.4c/ct"
> 427-day recon edge never appeared in real fills (reconstruction ≠ live: no
> slippage/counterparty). Sizes are parked at the **$1/$1 resume-safe floor** with
> both edge-tiers OFF (2026-06-09 LATE de-risk). ⛔ **Do not `rm KILL` without a
> live-demonstrated edge** (settled fills — not replays, not reconstructions). Open
> positions settle as-is (no-sell policy stands).

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
> **2026-06-09 SUMMER middle-path (Claude, Chris-approved, commits 4d05e18+c376448):** HIGH = **NO-only** (`PUSH_HIGH_NO_ONLY=True` drops HIGH YES — new gate below) at `PUSH_MIN_EDGE_PP=10` and **`$5`** (`PUSH_HIGH_MAX_BET_DEFAULT`, 1→3→5 as the audit held); thin-margin-NO is **ON** (band 0.5/offset 0). Audit: NO-only ≥10pp = +9.4c/ct/66%WR on 427 summer recon days (both halves +, LOSO all 7 +, robust to ~37% blowup rate). LOW = $3 B-NO-only edge-tiered. **→ 2026-06-09 LATE OUTCOME: this config's first live day lost −$33.74 (6W/11L) and the bot was HALTED (see banner at top); sizes are now $1 HIGH / $1 LOW, LOW edge-tier OFF.** ⛔ The 6/8 "summer HIGH −EV / WR 0.41 / −$879 / high_edge.py seasonal" rationale was DELETED from config — UNREPRODUCIBLE (that tool was never committed and is gone; the production model never goes −EV in summer at ANY gating level, ungated +4.2c/ct → gated +9.4). ⚠️ The `=2` / `$8` / `thin-margin off` / `LOW $5` values in the prose below are the STALE pre-seasonal-swap (≤6/6) baseline — restore in fall ~Sep. cf `memory/project_blend_high_middlepath_shipped_20260609`.
edge floor (NO `PUSH_MIN_EDGE_PP=2` / YES `=2`) · in-bracket tail-bet gate (`=25`) · **HIGH no-only** (`PUSH_HIGH_NO_ONLY`, summer)
· direction/series toggles · per-station bench (empty) · NWP-agreement gate (off)
· cell-MAE gate (off) · **decision window** · HIGH spread ≤25c / **LOW spread ≤1c**
· thin-margin-NO (off) · NBM-veto (off) · σ-floor (1.0, per-station floors exempt
for blend) / σ-ceiling (2.5) · physics (vsby<0.5mi, wind>40mph) · LOW front-wind
(≥18mph) · price band (NO≥25 / YES≥30 / LOW-NO≥10 / ≤90) · HIGH off-peak veto ·
position dedup · **per-station cap** · cash · correlation cap.

**Per-station cap (`PUSH_ONE_BRACKET_PER_STATION_HIGH=True`, 2026-06-05).** HIGH is
capped at **1 bracket per station-day across BOTH directions**, committing only the
**max-edge** bracket: a buy is blocked if a currently-quoted sibling (same station-day,
from the WS BBO cache, scored on the shared blend μ/σ via `_bracket_edge_pp`) has a
higher edge. The bot's real unit of risk is the station *forecast*, not the bracket;
stacking 2–3 correlated brackets just levers one forecast (6/4: MIA/DC/CHI/ATL each lost
*both* legs). Backtest (14mo, `judge_dyn/blend_rows.pkl`): one-best-bracket/station cuts
the worst-5% station-day drawdown ~3× (−$1930→−$636) and lifts per-stn-day Sharpe
0.085→0.089; on the 6/4 tape it would have been −$23 vs −$71. Max-edge selection (not
greedy first-qualify) is required — committing the *worst* leg collapses Sharpe to 0.022.
Rollback → `False` reverts to the legacy per-(station,series,dir,day) cap. LOW unaffected.

### Sizing  (2026-06-06, Chris)
> **2026-06-09 LATE (post-KILL):** live values are now `PUSH_HIGH_MAX_BET_DEFAULT=$1`,
> `max_bet_low_series_usd=$1`, `PUSH_EDGE_TIER_SIZING_LOW_ENABLED=False` — the prose
> below is the stale pre-halt baseline.

HIGH base `PUSH_HIGH_MAX_BET_DEFAULT` = **$5 SUMMER NO-only** (see the 2026-06-09 note above) / $8 fall baseline (NO/default — tail-protected by
the one-bracket-per-station cap), `PUSH_HIGH_YES_MAX_BET_USD=$5` (YES held at $5 — thinner,
walks thin books); LOW `max_bet_low_series_usd=$5` (raised 1→5; edge +7.22c/ct 14mo,
live-confirmed @85% fill) with `PUSH_LOW_NO_BET_BY_STATION={"KDEN":10}` (the one robust
both-halves LOW-NO cell); `min_buy_usd=0.40`; backstop `max_bet_high_series_usd=$20`;
MAE-confidence shrink + edge-band tilt applied; `qty = budget // price`.

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
