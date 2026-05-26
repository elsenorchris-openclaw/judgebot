# paper_judge_bot — pure-code weather-bracket trading bot

> **Current state (2026-05-23).** This bot trades Kalshi daily **HIGH/LOW temperature
> bracket** markets for 20 US cities. It runs a **pure-code, event-driven
> auto-executor** driven by a k-nearest-neighbor "analog day" temperature
> forecaster. The original LLM ("Claude-as-Trader") entry/exit design still lives in
> the tree but is **dormant** — `LLM_DISPATCH_MODE="off"` and `ENABLE_SELLS=False`,
> so neither the Claude entry loop nor the exit loop runs. Everything in this
> reference describes how the bot **actually runs today**. The **Change log**
> further down is the dated history of how it got here; the older LLM-first
> reference sections have been removed (that history is in the change log).

> ⚠️ Bot lives on the VPS at `ubuntu@54.225.174.220:/home/ubuntu/paper_judge_bot`
> (systemd `paper-judge-bot.service`, git repo `judgebot.git`, branch `main`).
> **SSH first — local checkouts are stale.**

---

## TL;DR — how it trades now

- **Universe:** day-0 only (`DAYS_OUT_RANGE=(0,)`), 20 cities, both `KXHIGH*`
  (daily-high) and `KXLOWT*` (daily-low) 2 °F bracket markets.
- **Signal:** kNN analog matcher (`nn_match_fast.predict`) over a 2000–2025 1-min
  ASOS trace DB → μ (projected daily extreme) + σ.
- **Decision:** `nn_shadow_strategy.pure_nn_decide` turns μ/σ + bracket geometry +
  the live running extreme into P(YES), picks the higher-edge side
  (BUY_YES / BUY_NO) and sizes it.
- **Execution:** `nn_shadow_worker` fires on live market + weather events and, if a
  stack of gates passes, places a **real Kalshi order** via
  `paper_judge_bot.execute_buy`. Every decision is logged to
  `data/shadow_nn_strategy.jsonl`.
- **No LLM in the trade loop. No selling — positions are held to settlement.**
- **Mode:** `MODE="trader"` on the shared **v2 / account-2** Kalshi wallet (co-exists
  with the V2-max and V2-min bots).

---

## Process model

`main()` (in `paper_judge_bot.py`) starts, in order:

1. `obs_refresh.start()` — background thread priming NWS obs for all stations (~90 s).
2. `wethr_rm.start()` — running-min/max client over the shared wethr cache (no-op
   thread; the cache is owned by an external service).
3. `kalshi_ws.start(kalshi_client._sign, …)` — one authenticated WebSocket
   (RSA-PSS, **reusing the REST signer**) maintaining a live BBO/orderbook cache
   with an inversion/drift guard.
4. **`nn_shadow_worker.start(rt)`** — the trading engine (gated by
   `SHADOW_NN_EVENT_DRIVEN=True`).
5. The `one_cycle` maintenance loop, every `EXIT_CYCLE_SEC` (120 s).

With the LLM off, **`one_cycle` is quiet maintenance only — it does not trade**:

- `reconcile_positions_with_kalshi` — authoritative `/portfolio/positions` read;
- subscribe the **full candidate universe** to the WS (so the worker receives BBO
  events for every market — this is what feeds the trading engine);
- halt the cycle + Discord-alert if the wethr feed is degraded;
- `_resolve_settlements` — log settled outcomes;
- hourly Discord summary.

`run_entry_loop` is **skipped outright** when `LLM_DISPATCH_MODE="off"` (and even if
reached, the dispatch gate empties the survivor list). The exit loop is skipped
because `ENABLE_SELLS=False`. All actual buying happens **asynchronously in the push
worker**, off the cycle clock.

---

## The trading engine: `nn_shadow_worker`

> The module is historically named "shadow," but it is the **live auto-trader** — it
> places real orders. (Its file docstring still says "No orders are placed"; that
> line is stale.)

### Triggers
A ticker is (re-)evaluated on any of:
- a **Kalshi WS BBO change** (`_on_bbo_change`; ignores sub-1 ¢ flutter);
- a **wethr SSE event** over a Unix socket (`/tmp/wethr_events.sock`, sub-50 ms);
- a **5 s file-poll** of `wethr_cache.json` (fallback if the socket drops).

Each evaluation is per-ticker mutex'd + 30 s-debounced.

### Per-evaluation flow (`_evaluate_ticker`)
1. Parse ticker → `Candidate`; patch T-bracket floor/cap from the Kalshi market
   record (cached per ticker).
2. `_build_shadow_packet`: BBO (from `kalshi_ws`), live obs + 30/60-min trends (from
   `shared_cache_reader` over `wethr_cache.json`), local solar clock, the running
   extreme (`high_f`/`low_f`), and the **F1 rm-staleness check**
   (`wethr_rm.validate_rm_for_climate_day` — nulls a stale running extreme so a prior
   climate-day reading can't anchor today).
3. Look up the push override (window / bias / MAE) for `(station, side, month)`.
4. `nn_shadow.shadow_nn_proj(pkt)` → `nn_match_fast.predict` → μ, σ (+ neighbor
   distribution).
5. Build the sizing budget: per-series cap × MAE-confidence multiplier
   (regime-adjusted; only ever shrinks).
6. `pure_nn_decide` → BUY_YES / BUY_NO / SKIP with edge, P(YES), qty, price.
7. If a BUY: `_try_auto_execute` runs the gate stack; on pass, `execute_buy` places
   the order (which re-runs `guardrails.check_buy`).
8. Log the full decision to `data/shadow_nn_strategy.jsonl`.

### The signal — `nn_match_fast.predict`
- Source: `/home/ubuntu/data/heating_traces.sqlite` — 1-min ASOS traces, 2000–2025,
  ~8–9 k days/station × 20 stations, packed as 288×5-min bins.
- Finds today's intraday trajectory's nearest historical analogs: L2 distance over
  the temperature trace in `[sunrise−30 min, now]`, plus weighted dewpoint / wind /
  sky / RH / station-pressure penalties; takes top-k (k=50 both sides).
- μ = current temp + aggregated neighbor delta-to-extreme + bias correction.
  Aggregator is side-specific (`NN_USE_NEW_AGGREGATORS=True`): **HIGH** = inverse-cube-
  distance-weighted mean (idw3); **LOW** = winsorized-10/90 mean of the closest 20.
- Physical constraints: HIGH μ floored at the running max; **two-tier HIGH peak
  clamp** near/after the historical peak time; LOW locks to the trajectory min only
  after 21:00 LST (`NN_LOCK_FLOOR_LST_MIN`).
- **Returns null μ → the bot skips** on: the fit-quality gate (analog cluster too
  dispersed, thresh 3.0) and the LOW-post-noon-unlocked gate.
- σ = neighbor spread × side σ-factor (0.90 HIGH / 1.10 LOW-AM).

### The decision — `nn_shadow_strategy.pure_nn_decide`
- Gate: μ must come from `nn_match_*` (else SKIP).
- d+0 **rm constraint**: truncate the Gaussian to the physically-possible half-line
  (HIGH day-high ≥ running max; LOW day-low ≤ running min) — a Bayesian truncation,
  not a hard `max(μ, rm)` clamp.
- P(YES) = Gaussian mass in the YES window: **B** = `[floor−0.5, cap+0.5)`;
  **T-warm** = `[floor+0.5, ∞)`; **T-cold** = `(−∞, cap−0.5)`.
- **Empirical tail-loss correction (`USE_TAIL_EMPIRICAL_PYES=True`):** for open-ended
  T brackets, raises P(YES) of the fat-surprise tail to an empirically-measured floor
  (the Gaussian under-states it ~2–10× at 2–2.5 σ). Only ever *deflates* an
  overconfident deep-margin tail BUY_NO.
- Edge = P(side) − ask; pick the higher-edge side. An rm-lock lets a high-edge bet
  bypass the in-function edge ceiling.
- Size = `min(series_cap, ticker_remaining) // price`, floored to whole contracts,
  must clear `min_buy` (else qty 0 → SKIP).

### The gate stack — `_try_auto_execute` (the real risk controls)
A BUY is rejected unless it clears every gate, in execution order:

| Gate | Rule (config flag) |
|---|---|
| Edge floor | `edge ≥ PUSH_MIN_EDGE_PP` (**12 pp**) |
| In-bracket tail-bet | if μ sits inside the YES window but the chosen side is the tail (p_chosen < 0.5), require `PUSH_TAIL_BET_MIN_EDGE_PP` (**25 pp**) |
| Direction toggle | `AUTO_EXECUTE_BUY_{NO,YES}_PUSH` (both **True**) |
| LOW enabled | `AUTO_EXEC_LOW_ENABLED` (**True** — $1 live probe) |
| Cell-MAE reliability | skip if the matcher's historical MAE for `(station, season, local_hour, side)` > `PUSH_MAE_GATE_F` (**2.0 °F**) — `cell_mae_table.CELL_MAE`, 2022-2025 OOS, fail-open on unknown cells (`PUSH_MAE_GATE_ENABLED=True`) |
| Decision window | hour ∈ `[peak−before, peak+after]` (see **Windows**) |
| HIGH spread | skip if `yes_ask − yes_bid > PUSH_MAX_SPREAD_C_HIGH` (**15 ¢**) — crossing a wide book pays away the edge |
| HIGH h₂peak | `PUSH_MIN_H_TO_PEAK_HIGH` (**None / disabled** — windows already end ≥ 1 h pre-peak) |
| HIGH thin-margin NO | skip BUY_NO when `(μ − per-station CLI offset)` lands inside `[floor−0.5, cap+0.5]` — i.e. shorting a bracket your own μ points into (`PUSH_SKIP_NO_MU_NEAR_BRACKET=True`) |
| Tier-1 physics | skip if visibility < `PUSH_MIN_VSBY_MI` (0.5 mi) or wind/gust > `PUSH_MAX_WIND_MPH` (40) |
| LOW cold-front | skip LOW if sustained wind ≥ `PUSH_LOW_FRONT_WIND_MPH` (18 mph); excludes KLAX/KMIA |
| Price band | ask ∈ `[PUSH_MIN_ENTRY_C` (10) / `PUSH_MIN_ENTRY_C_BUY_YES` (30), `PUSH_MAX_ENTRY_C` (80)] ¢ |
| Position dedup | never add to an existing position on the exact ticker |
| Position cap | ≤ `PUSH_MAX_TICKERS_PER_STATION_SIDE_DIRECTION` (1) per (station, series, direction), **scoped to climate-day** |
| Cash | wallet balance ≥ min_buy |
| Correlation cap | ≤ `GUARDRAILS["max_buys_per_station_side"]` (1) per (station, HIGH/LOW, day) |

`execute_buy` then re-runs the deterministic `guardrails.check_buy` (same caps) before
the Kalshi POST — so the worker's sizing must match the caps or the order is rejected
(guardrails *reject*, they don't truncate).

### Windows
- Trading windows are **per-(station, side, month)**, read solely from
  `push_window_overrides.PUSH_WINDOW_OVERRIDES`. The peak/min hour is the 5-yr
  10-day-rolling fractional P50 (`USE_FRACTIONAL_PEAK_FOR_WINDOW=True`); window =
  `[peak − before, peak + after]`. **There is no default fallback** — a missing cell
  (or missing peak) is not traded and fires a throttled Discord alert.
- **May override (the current month):** for `month ∈ PUSH_TEMP_WINDOW_MONTHS` (`{5}`)
  the table window is *replaced* by the price-gated per-station 30-min windows in
  `PUSH_HIGH_TEMP_WINDOW_BY_STATION` (HIGH) / `PUSH_LOW_TEMP_WINDOW` (LOW). **So in May
  the overrides table is bypassed**; other months use the table.
- The HIGH early-side trim (`PUSH_EARLY_TRIM_HIGH_ENABLED`) is **off** (the
  deep-pre-peak temp windows need the early offsets).

### Sizing (current)
- **HIGH:** `PUSH_HIGH_MAX_BET_BY_STATION` = **$15** for the 15 robust cells;
  everything else (MSP/MSY/SFO/SAT/DCA + unlisted) = `PUSH_HIGH_MAX_BET_DEFAULT`
  (**$3**).
- **LOW:** `max_bet_low_series_usd` = **$1** — a live-execution probe (backtest says
  crossing the wide LOW spread is −EV), with a $0.40 min-buy floor so the
  integer-contract math doesn't collapse.
- **MAE-confidence sizing on** (`USE_PUSH_MAE_SIZING`, regime-adjusted via
  `USE_PUSH_REGIME_MAE_ADJ`) — scales bets **down** where the matcher's historical
  accuracy is worse; never up.
- **Per-cell median-bias correction OFF** (`USE_PUSH_BIAS_CORRECTION=False`,
  reverted — it flipped winners on a cold day). Bias is logged, not applied.

---

## Guardrails (`guardrails.py`) — deterministic, non-overridable

`check_buy` enforces (current `config.GUARDRAILS`): side caps **BUY_NO $30 /
BUY_YES $15**; series caps **HIGH $15 / LOW $1** (each min'd with the side cap);
`max_ticker_total_usd` $30; `daily_spend_cap_usd` $300; price ∈ [5, 90] ¢; no new
buys < 30 min to close; 30 min rebuy cooldown; **daily-loss kill at −$100**;
`max_open_positions` / `max_daily_buys` effectively unlimited; the API-spend breaker
is disabled (∞). `check_sell` is intentionally weaker but moot while
`ENABLE_SELLS=False`. The mode / kill-switch / enable flags short-circuit everything.

---

## Safety / mode flags (current live values)

| Flag | Value | Meaning |
|---|---|---|
| `MODE` | `trader` | executes orders (`observer_only` = scan/log only; `killed` = nothing) |
| `DRY_RUN` | `False` | real Kalshi POSTs |
| `ENABLE_BUYS` | `True` | |
| `ENABLE_SELLS` | `False` | bot holds to settlement; exit loop skipped |
| `LLM_DISPATCH_MODE` | `off` | LLM never dispatched — push worker owns trades |
| `SHADOW_NN_EVENT_DRIVEN` | `True` | the push worker is the trading engine |
| `AUTO_EXECUTE_BUY_{NO,YES}_PUSH` | `True` | |
| `KILL` file | `~/paper_judge_bot/KILL` | touch to halt new actions (open positions untouched) |
| `WALLET` | `v2` | shared account-2 key (not dedicated) |

---

## Co-existence on the shared v2 wallet

The v2 wallet is shared three ways: **V2-max** (`obs-pipeline-bot`, KXHIGH), **V2-min**
(`kalshi-min-bot-v2`, KXLOWT), and **judge** (this bot, both). Collision rules in
code: every cycle reads `/portfolio/positions` and skips any ticker already held by
*any* bot; exits only ever touch judge-opened positions (origin tag +
`state.claim_ticker`, which also keeps the sibling bots' orphan-reconcilers from
adopting our tickers); caps are sized conservatively against the shared balance; one
shared Kalshi rate-limit bucket. To give judge its own key/balance, set
`WALLET="own"` + `.env` `KALSHI_KEY_ID`/`KALSHI_PEM_PATH`.

---

## Data sources (current)

| Source | Module(s) | Notes |
|---|---|---|
| Kalshi REST + WS | `kalshi_client.py`, `kalshi_ws.py` | RSA-PSS-signed; WS BBO cache w/ inversion guard; PEM is the shared account-2 key `/home/ubuntu/obs-pipeline-bot/kalshi_key_v2_account2.pem` |
| Analog trace DB | `nn_match_fast.py` | `/home/ubuntu/data/heating_traces.sqlite` (the matcher's training data) |
| Live obs + running extreme | `wethr_rm.py`, `shared_cache_reader.py` | `/home/ubuntu/shared/wethr_cache.json` (wethr-cache-service, ~5 s) — **sole RM source** since the 2026-05-16 wethr-only policy |
| NWP model cache | `shared_cache_reader.py`, `forecast_delta.py`, `nbp_reader.py` | `/home/ubuntu/shared_cache/{nbm,hrrr,ecmwf-ifs}.json` — feeds the **dormant** LLM packet; **not** used in the push decision |
| Solar/clock, climate normals, station meta | `solar_calc.py`, `climate_normals.py`, `station_meta.py` | pure tables/astronomy (no network) |
| Settlement | `kalshi_client.list_settlements` + `markets/{ticker}.result` | CLI-authoritative outcomes |
| Notifications | `paper_judge_bot.discord_send` | Discord webhook or bot token |

(The NWS modules `obs_client` / `nws_grid` / `nws_afd` / `nws_history` /
`nws_fc_history`, plus `wethr_client` and `persistence`, feed the dormant LLM packet
and the obs-primer — they are **not** on the push decision path.)

---

## File map

```
paper_judge_bot/
├── README.md                  ← this file (current reference + change log)
├── paper_judge_bot.py         ← daemon: Runtime, one_cycle, run_entry/exit_loop,
│                                 execute_buy/sell, reconcile, prescreen (LLM-era)
├── config.py                  ← ALL tunables + .env loader + wallet resolve
│
│  ── signal pipeline (the live brain) ──
├── nn_shadow_worker.py        ← event-driven auto-trader (triggers, gate stack)
├── nn_shadow.py               ← packet → predict() adapter (μ/σ)
├── nn_match_fast.py           ← kNN analog matcher over heating_traces.sqlite
├── nn_shadow_strategy.py      ← pure_nn_decide: P(YES), edge, side, sizing
├── push_window_overrides.py   ← generated per-(station,side,month) window/bias/MAE table
│
│  ── execution / market IO ──
├── kalshi_client.py           ← RSA-PSS REST: balance, markets, orders, settlements
├── kalshi_ws.py               ← live BBO/orderbook WS + fill channel + inversion guard
├── market_universe.py         ← ticker grammar, Candidate, daily universe discovery
├── shared_cache_reader.py     ← reads wethr_cache.json + NWP shared_cache
├── wethr_rm.py                ← running min/max + F1 LST climate-day staleness validator
├── guardrails.py              ← deterministic hard caps (check_buy/check_sell)
├── state.py                   ← positions/trades/decisions persistence + claim registry
│
│  ── data adapters (mostly feed the dormant LLM path / obs primer) ──
├── obs_client.py, obs_refresh.py, wethr_client.py, live_data.py
├── nws_grid.py, nws_afd.py, nws_history.py, nws_fc_history.py
├── climate_normals.py, solar_calc.py, station_meta.py
├── pres_history.py, forecast_delta.py, nbp_reader.py, persistence.py
│
│  ── dormant LLM ("Claude-as-Trader") path ──
├── judgment.py                ← Claude SDK/CLI caller + prompt build + parser
├── decide_entry_code.py       ← pure-code shadow comparator (logs only, never trades)
├── prompts/                   ← entry_prompt.md / exit_prompt.md (+ backups)
│
├── tools/                     ← backtest/analysis utilities (NOT on the trade path)
├── tests/                     ← 26 files / ~428 tests (pytest)
├── data/                      ← runtime files (gitignored): positions.json,
│                                 trades.jsonl, shadow_nn_strategy.jsonl, by_date/, …
├── paper-judge-bot.service    ← systemd unit
└── *.md                       ← BACKTEST_GUIDE / BACKTEST_METHODOLOGY / MAY20_SETUP /
                                  GOODDAY_PREDICTOR_SCOPE / PHASE2_NWP_DISPERSION_SCOPE /
                                  PUSH_OVERRIDES_PLAN
```

---

## Operational runbook

- **Host / service:** VPS `ubuntu@54.225.174.220`, `~/paper_judge_bot`, systemd
  `paper-judge-bot.service`. Git-tracked (`judgebot.git`, branch `main`). SSH first —
  local copies are stale.
- **Logs:** `journalctl -u paper-judge-bot -f`; live decisions
  `tail -F data/shadow_nn_strategy.jsonl`; fills `data/trades.jsonl`.
- **Kill switch:** `touch ~/paper_judge_bot/KILL` halts new actions (open positions
  are *not* closed); `rm` the file to resume.
- **After any config/code change:** `sudo systemctl restart paper-judge-bot`, verify a
  single PID with code mtime ≤ start, then **commit + push** to `judgebot.git`
  (restart ≠ done).
- **Forward tracking:** `tools/accumulate_cell_wr.py` (daily timer) appends
  faithful settled-trade win-rate to `data/faithful_cell_wr.jsonl`;
  `tools/replay_windows.py` + `window-replay.timer` replay the current windows on a
  past day into `data/window_replay_log.txt`.
- **Toggling behavior:** every lever is a flag in `config.py` (restart to apply) —
  see the inline comments there for each flag's backtest provenance.

---

## Tools, tests, docs

- **`tools/`** — research/backtest utilities, **not** part of the running bot: DB
  audit + gap-fill (the capture-gap suite), candlestick backfill, HIGH/LOW window
  sweeps that emit `PUSH_*_WINDOW*` dicts, spread analysis, the faithful WR
  accumulator, and a standalone live price recorder. Authoritative data store:
  `/home/ubuntu/data/market_history_backfill.sqlite`. **Read `BACKTEST_GUIDE.md`
  first** for the backtest conventions (audit-first, asymmetric price gate,
  faithful = buy-at-open, OOS early/late split).
- **`tests/`** — 26 files, ~428 tests (`pytest tests/`): guardrails, F1 rm-validation
  + LST climate-day math, push windows + every gate (tier-1, h₂peak, tail-bet,
  thin-margin, cold-front), nn calibration, `pure_nn_decide`, market-universe
  parsing, state bookkeeping, MAE sizing, regime-MAE. (A root-level
  `test_nn_shadow_strategy.py` is a stale duplicate of the one in `tests/`.)
- **Aux docs:** `BACKTEST_GUIDE.md`, `BACKTEST_METHODOLOGY.md`, `MAY20_SETUP.md`,
  `GOODDAY_PREDICTOR_SCOPE.md`, `PHASE2_NWP_DISPERSION_SCOPE.md` (NWP dispersion —
  later confirmed NO-GO), `PUSH_OVERRIDES_PLAN.md` (the override-system handoff doc).

---

## Legacy: the LLM "Claude-as-Trader" path (dormant)

The original design dispatched each prescreened candidate to Claude
(`judgment.judge_entry`, `prompts/entry_prompt.md`) for a BUY/SKIP read, with a
matching Claude exit loop. That code is intact — `judgment.py`,
`decide_entry_code.py` (a pure-code shadow comparator that only logs), the
`prescreen` in `paper_judge_bot.py`, and `prompts/` — but **not executed**:
`LLM_DISPATCH_MODE="off"` skips the entry loop, and `ENABLE_SELLS=False` skips
exits. Even the entry prompt had forecasts disabled (`RENDER_FORECASTS=False`) before
it was shut off, so its last live form saw obs + nn_match only. The full evolution
from LLM-first to pure-code push is in the change log below.

---

# Change log (newest first)

> Historical record. Current behavior is summarized in the reference above; where an
> entry below conflicts with the reference, **the reference wins** (it reflects the
> live `config.py`). Notable later reversals: median-bias correction was shipped then
> reverted; the HIGH early-side trim is currently off.

## Discord alerts: WS-health watchdog + uncaught-crash hooks — 2026-05-24

Proactive Discord visibility for the failure modes that silently stop the bot trading.
(1) WS-health watchdog in `kalshi_ws._periodic_stats`: every 30s it diffs the counters and
fires THROTTLED (1/30min per condition) alerts on: orderbook drift not clearing
(cache_skip_inv climbing while snapshots flat = the resync-failure signature), feed stalled
(0 deltas+0 bbo_updates/30s while connected), WS reconnect, and error spikes. (2) Crash
hooks: `sys.excepthook` + `threading.excepthook` route ANY uncaught exception (main or
background thread) to Discord with the traceback (throttled 1/60s). Both reuse
`discord_send` (same channel as peak/window alerts); each bot labels alerts by dir name
(judge vs v1max). A one-time 'WS-health alerts armed' ping posts on startup (doubles as a
restart heartbeat). Reversible: `kalshi_ws._WS_HEALTH_ALERTS=False`. Built after the WS-
resync bug silently degraded v1max for hours before anyone noticed.

## WS resync now actually re-snapshots (root-cause fix for drift accumulation) — 2026-05-24

The L2 orderbook resync was a SILENT NO-OP. On drift (a stale level makes top-YES-bid +
top-NO-bid > 100c = an inversion) it re-sent `subscribe` for the already-subscribed ticker --
but Kalshi snapshots only on the FIRST subscribe of a channel; later subscribes return
`{"type":"ok"}` and are added to the existing sid with NO snapshot. So drifted books were
NEVER repaired -> inversions accumulated unbounded over uptime (cache_skip_inv into the
millions) -> stale BBO -> the matcher's eval trigger starved -> the bot slowly stopped
trading. Only a restart/reconnect fixed it. Fix: capture the connection's single
orderbook_delta sid and re-snapshot drifted tickers via update_subscription
delete_markets+add_markets, which DOES resend a snapshot. Verified live on v1max: snapshots
climb past sub-count (122->184 in 5 min), cache_skip_inv=0. Reversible:
_RESYNC_VIA_UPDATE_SUB=False. Daily v1max-restart timer stays as a safety net until ~a day
confirms durability.

## MIA HIGH window deepened peak-1.5h -> peak-3.0h — 2026-05-24

`PUSH_HIGH_TEMP_WINDOW_BY_STATION["KMIA"]` changed `(1.5, -1.0)` -> `(3.0, -2.5)`: the MIA
30-min window now OPENS at peak-3.0h instead of peak-1.5h. Per-station live-era sweep (OOS,
full gates incl. thin-margin (2d) + spread<=15c) found MIA's profit is DEEP: NO-only **+$7.50
vs +$3.20** shipped, combined **+$9.02 vs +$1.75**, positive in BOTH date halves; the deep
slots (2.5-3.5h) all win, shallow 0.5h loses (-$3.89). The shallow 1.5h window let mu rise
near the bracket -> boundary YES bets (the 5/23 MIA B87-88 YES coin-flip). Sweep also tested
DFW/PHX/PHL: DFW/PHL DROPPED (DFW worse under the thin-margin gate; PHL overfit zigzag), PHX
HELD (NO-only ~even). Reversible: restore `(1.5, -1.0)`.

## Per-cell MAE reliability gate — 2026-05-25

Skip a BUY when the matcher's **historical MAE** for this `(station, season, local_hour,
side)` cell exceeds `PUSH_MAE_GATE_F` (=2.0°F). The k-NN projection's accuracy varies ~10×
by cell — e.g. KMSP/KAUS morning HIGH ≈ 5°F MAE (noise) vs KLAS/KMSY/DFW late-afternoon
HIGH ≈ 0.5°F (sharp). Where MAE is high the edge calc rests on an unreliable μ/σ, so the
trade is skipped regardless of computed edge. This is the trade-time form of the
accuracy-heatmap study ("trade only where the matcher is historically accurate").

- **Table**: `cell_mae_table.CELL_MAE` — 1184 cells, built from a 2022-2025 `heating_traces`
  backtest (`tools/nn_agg_sweep/gen_mae_table.py`, n≥20/cell). Fully **out-of-sample** to
  2026 live trades. Lookup `cell_mae(station, month, local_hour, side)` strips a K-prefix
  and maps month→season; **fail-open** (unknown/thin cell → not gated).
- **Gate**: `nn_shadow_worker._try_auto_execute`, after the NWP-agreement gate. Flags
  `PUSH_MAE_GATE_ENABLED` / `PUSH_MAE_GATE_F`. Reason string `cell_mae_gate`.
- **Distinct from `PUSH_MAE_CONF_TIERS`** (which only *shrinks size*) — this **hard-SKIPs**,
  additive on top of the sizing.
- **Backtest** (settled 2026-05-14..24, n=315): gating MAE>2.0°F lifts realized P&L
  **+$23.29** (kept −$118.53 vs ungated −$141.82), robust in **both** date-halves
  (H1 +$10.93, H2 +$12.36). Sigma calibration was tested first and **rejected** (per-cell,
  global inflation ×1.2-2.0, and HIGH σ-factor 0.90→1.7 all *hurt* — the BUY_NO
  miscalibration is a signal-skill limit, not a variance error, so no σ transform separates
  winners from losers; this hard cell gate does).
- **Reversible**: `PUSH_MAE_GATE_ENABLED=False`.

## NWP-agreement gate (HIGH) — 2026-05-25

Skip a HIGH trade when the k-NN analog μ disagrees with an **independent NWP daily-high**
(median of the latest NBM/HRRR/ECMWF runs from the shared GRIB cache, via `forecast_delta`)
by more than `MU_AGREEMENT_MAX_DIFF_F` (=2.0°F). Flags: `USE_MU_AGREEMENT_GATE`. Gate in
`_try_auto_execute` (HIGH only); **fail-open** if μ_nwp is unavailable. μ_nwp + disagreement
are logged to the shadow log and appended to the Discord buy message.

**Why:** the catastrophic losses are k-NN μ blow-ups (5–6°F off) on bad days; an independent
NWP doesn't err the same way. Phase-1 backtest 5/19–5/21 (the only lever this session that
adds *independent* information rather than re-slicing the k-NN signal): mean |μ_nwp − actual|
**1.17°F** vs k-NN **1.88°F**; k-NN big-misses had mean disagreement 3.33°F vs 1.85°F for good
ones (disagreement predicts error). Gate at 2.0°F: kept pool **+23% ROI (n=30)** vs removed
**−34% (n=23)**; baseline was −2%. Threshold has a stable 1.75–2.25 plateau (not knife-edge).

Caveats: n=53 backtest (5/19–5/21, the v1max candidate-log window — μ_nwp live source is the
GRIB cache, ~same NWP); helps:hurts ~1:1 by count but the dollar separation is decisive; it
still hurts on 5/20 (the one day high-disagreement won). Judge only (not v1max, the frozen
control). Logs μ_nwp natively now so future backtests don't need the v1max cross-ref.

## Pause HIGH BUY_YES (structural losing side) — 2026-05-25

Backtest on the faithful settled pool 5/19-5/23 (n=22): **HIGH BUY_YES is 36% win,
-20% ROI** -- a structural loser, vs HIGH BUY_NO (the bot's actual edge, the larger
book). Pausing it: lift **+$27**, helps:hurts **14:8**. New flag
`AUTO_EXEC_HIGH_YES_ENABLED=False` + a gate in `_try_auto_execute` (mirrors the LOW
pause). HIGH BUY_NO and the LOW $1 probe are unaffected; shadow-eval still logs.

Two sibling candidates were backtested and **held** (failed the helps:hurts bar):
gap ceiling (~1.3:1, hurts 5/20, lift concentrated on the 5/22 sizing-disasters that
the cap already addresses) and one-bet-per-station (1.13:1 -- drops about as many
winners as losers). Note: even applying all three leaves the settled pool net-negative
(-$12 from -$110) -- these reduce the bleed, they don't create an edge. The real fix
is a sharper mu; the matcher's +/-1-3F error is too coarse for 1F-wide brackets.

## Matcher obs fix: feed hourly_history (the dead hourly_obs_today key) — 2026-05-24

The k-NN matcher's trajectory builder read `hourly_obs_today` from the wethr cache, but
**no producer writes that key** — `wethr_cache_service` writes the hourly curve as
`hourly_history`. So the matcher ran only on the last-60-min `temp_history` and sat on its
`min_window_minutes=60` gate (>=12 five-min bins). Stations with sparser 5-min feeds fell
to 7-11 bins -> "trajectory too short" -> chronic/intermittent no-projection: **KNYC fired
0/all every day; KBOS/KDCA/KSEA intermittent** (5/23: NY/BOS/DC fired 0 all day despite
BOS/DC having healthy obs density).

Fix (`nn_shadow_worker` packet build): when `hourly_obs_today` is empty, map `hourly_history`
-> the `{hour_utc_iso, temp_f, dewpt_f}` shape `nn_shadow.py` expects, restoring the morning
curve. Replay 5/23 @peak-1h: **20/20 stations fire** (vs ~16 before). Confirmed live
post-restart: BOS/SEA now produce projections. Same fix applied to v1max-high.

Caveat: this is a coverage/correctness fix (more matcher fires), **not** a confirmed profit
win -- the newly-covered stations have no settled track record and the broader signal still
loses to the market. Pair with loss-reduction work, don't treat more volume as more profit.

## HIGH sizing tiers ($15 robust / $3 soft) + daily window-replay cron — 2026-05-23

(1) HIGH max bet tiered by window validation: **$15** for the 15 ROBUST 30-min cells (PUSH_HIGH_MAX_BET_BY_STATION), **$3** for SOFT/NEG/default cells (MSP/MSY/SFO/SAT/DCA) via PUSH_HIGH_MAX_BET_DEFAULT; guardrail backstop $15. MAE conf-sizing still scales below the cap.

(2) Daily systemd timer **window-replay.timer** (13:00 UTC) runs tools/daily_window_replay.sh -> replays the CURRENT windows on the prior day live-recorder data (shadow log) + live Kalshi settlement (tools/replay_windows.py), appending to data/window_replay_log.txt = a forward track record. 2026-05-22: 6W/3L +$1.04 (+11.6c/bet); biggest loss SAT (the NEG cell). Units live in /etc/systemd/system/window-replay.{service,timer}.

## LOW $1-probe min-buy floor fix — 2026-05-23

The $1 LOW probe never placed an order: execute_buy used the global $1 min_buy_usd floor as the scout floor_cost, so a 1-contract LOW buy (~$0.60-0.80) at the $1 cap was rejected ("reachable $0.60 < floor $1.00"). The bot CROSSES (+1c over ask), so it was not a fill/posting issue -- the order was skipped pre-placement. Fix: execute_buy now uses PUSH_MIN_BUY_USD_LOW ($0.40) as the floor for KXLOW* tickers. LOW can now place 1-contract orders. (Coverage is still sparse -- ~78% of LOW evals get no matcher projection -- so LOW trades infrequently.)

## HIGH max bet $15 -> $5 — 2026-05-23

Per Chris: judgebot uniform HIGH max bet lowered $15 -> $5 (PUSH_HIGH_MAX_BET_DEFAULT=5, guardrail backstop max_bet_high_series_usd=5; dicts stay empty so it is uniform). LOW stays $1. v1max-high was already $5.

## HIGH spread filter + LOW $1 live probe — 2026-05-23

**Spread analysis** (`tools/spread_analysis.py`): the bot buys NO by CROSSING
(cost = 100 - yes_bid), so a wide bid-ask eats the edge. HIGH markets are tight
(median 4c) and +EV, but the rare wide-spread HIGH bets are catastrophic
(spread 15-30c -> -21.8c/bet, 30c+ -> -31.3c). **Added a HIGH spread gate**
(`PUSH_MAX_SPREAD_C_HIGH=15`): skip HIGH push BUY when yes_ask-yes_bid > 15c.
Validated +1.0 -> +1.9c/bet OOS, both date-halves (`tools/spread_validate.py`).

**LOW back ON as a $1 live probe** (`AUTO_EXEC_LOW_ENABLED=True`,
`max_bet_low_series_usd=1`). LOW markets are very wide (median 17c spread), so
crossing loses (-8c/bet) -- BUT the directional model is fine (52% WR) and at MID
it's +1.8c: the loss is execution, not signal. The $1 probe tests whether live
fills beat the aggressive-cross backtest. Pointed at the LEAST-BAD deep-pre-min
window `PUSH_LOW_TEMP_WINDOW=(2.5,-2.0)` [min-2.5,min-2.0] (offset curve -4.3c
there vs -15c near/post-min, which the old (0.5,1.5) wrongly targeted). LOW is
NOT spread-filtered (so the probe trades). Expect small losses -- it's a probe,
not a validated edge. `AUTO_EXEC_LOW_ENABLED=False` to re-pause.

## 30-min per-station HIGH windows + DB gap-fill + LOW no-edge — 2026-05-23

**(1) Fixed a backtest-DB capture gap.** The historical backfill only served
pre-67d data, so **1,639 live-era market-days were missing across all 40
series** — the "thinly traded" HIGH stations and the LOW "illiquidity wall"
were both this gap, not real signals. Filled from Kalshi's live settled
endpoint + series candlesticks (+359k candles, `.close_dollars` schema);
`tools/db_audit.py` confirms 0 gaps.

**(2) Narrowed HIGH windows to 30 MINUTES, per station.** Re-ran the BUY_NO
sweep on full data; each window is 30 min and **begins at the most-profitable
buy-slot** (the bot buys at window-open), i.e. (before, after)=(X, 0.5-X). 15/20
are ROBUST (+PnL both date-halves): e.g. SEA peak-3.0 +19.5c, DFW peak-2.5
+19.6c, BOS peak-1.5 +14.9c, MIA peak-1.5 +12.9c. MSP/MSY/SFO are SOFT (one
half negative, flagged); SAT is NEG (no +EV slot) and DCA uses the deep default
(its model projections are stale) — both shipped per Chris's "all stations".
Global default also 30-min: (2.0,-1.5).

**(3) LOW re-analysis — now backtestable, and it's a no.** With full data
(n=1837), LOW BUY_NO is NEGATIVE at every offset in both halves (deep-pre-min
-7.0c, near-min -14.7c). The thin-data "near-min +14.2c" was noise (flipped to
-14.7). **LOW stays PAUSED** — now confirmed by real data, not a gap.
Tools: `db_audit.py`, `fill_gap.py`, `window_30min_sweep.py`, `low_gated_split.py`.

## Uniform $15 HIGH max bet (all stations) — 2026-05-22

Per Chris: every HIGH position caps at **$15**, all stations — replacing the
$3 default / $5 NYC-MIA / $30 MIA-NO sizing. `PUSH_HIGH_MAX_BET_DEFAULT=15.0`,
`PUSH_HIGH_MAX_BET_BY_STATION={}`, `PUSH_HIGH_NO_BET_BY_STATION={}` (the
after-decision NO-resize code stays but is dormant while the dict is empty), and
the guardrail `max_bet_high_series_usd=15.0` backstop clamps both HIGH sides to
$15 (NO min(30,15), YES min(15,15)). The per-station window TIMING
(`PUSH_HIGH_TEMP_WINDOW_BY_STATION`) is unchanged — this is bet SIZE only. LOW
sizing unchanged.

## Live-era per-station HIGH BUY_NO windows + NYC dropped from $30 — 2026-05-22

Replaced the per-station HIGH temp windows with a LIVE-era (2026-03-15+, all 19
stations) profit sweep. Finding: BUY_NO is +EV DEEP pre-peak and LOSES in the
final hour into peak (pooled [peak-2,peak-1] +3.0c/bet, both date-halves positive;
any window trading into peak is negative). So every per-station window now CLOSES
>=1h before peak, with the open profit-optimized per station (faithful gated
buy-at-open sim, early/late split as a confidence flag). `PUSH_HIGH_TEMP_WINDOW`
global fallback -> (2.0,-1.0) for stations without their own data (e.g. DCA). Dict
flags: ROBUST (both halves +, 9 cells), SOFT (one half -, DFW/LAX), THIN (HOU n=12),
fallback (7 low-volume stations). **NYC dropped from the $30 sizing** — live-era NYC
NO is only ~breakeven (+2.8 on its own window) so it trades at base $5; only MIA
(robust in BOTH the historical and live eras) keeps $30. The cross-year HISTORICAL
data had looked thin / NYC-MIA-only; the live era (current matcher) shows a broader
deep-pre-peak NO edge. Reversible (config). Tools: tools/window_side.py,
tools/window_wr.py.

## NYC/MIA BUY_NO sized to $30 — 2026-05-22

`PUSH_HIGH_NO_BET_BY_STATION = {"KNYC": 30, "KMIA": 30}`. NYC and MIA BUY_NO are
the only HIGH cells with a robust *out-of-sample* edge (cross-year backtest; every
other cell/side/filter — and the NWP forecast-dispersion good-day predictor — came
up negative or pure noise). So the proven NO bets size up to $30; the YES side and
all other cells keep their `PUSH_HIGH_MAX_BET_BY_STATION` cap ($5 NYC/MIA, $3
default). Applied in `nn_shadow_worker` *after* `pure_nn_decide` (so it is BUY_NO-
and station-specific), mirroring `_compute_size` and reusing the existing-cost +
MAE-conf-mult logic. The guardrail `max_bet_high_series_usd` backstop was raised
5 -> 30 so the larger NO bet is not rejected downstream — the per-station/side
worker caps remain the real limiter; this is only the backstop. MAE confidence
sizing still applies, so a less-accurate cell-day sizes below $30. Reversible:
empty the dict.

## HIGH default window -> [peak-1.5, peak-1.0] — 2026-05-22

`PUSH_HIGH_TEMP_WINDOW` (3.0,-2.0) -> (1.5,-1.0): the global HIGH default (used by
the 12 non-backtested cells) moves from deep-pre-peak `[peak-3,peak-2]` to
moderate-pre-peak `[peak-1.5,peak-1.0]`. Per Chris. NOTE: aggregate faithful data
leans toward the deeper zone (h2pk 2-3) outperforming 1-1.5h, so this may
underperform on those cells until they get their own backtested windows. Reversible.

## LOW placeholder windows (near/post-min) — 2026-05-22

`PUSH_LOW_TEMP_WINDOW = (0.5, 1.5)` -> all 20 LOW windows become `[min-0.5, min+1.5]`
(offset global, anchored to each station's own min), replacing the MAE-built LOW
overrides. Those mostly opened 2.5-4h before the min, but the faithful trades show
deep-pre-min (h2pk>=2.0) is the WEAK zone (40% WR, n=5) while near/post-min is good
(65%, n=23). Mechanism mirrors PUSH_HIGH_TEMP_WINDOW (resolved in
`_in_decision_window`, gated behind a base override existing). **Placeholder on thin
data** -- replaced per-cell by the LOW price-gated backtest once the LOW candle
backfill lands. Reversible: PUSH_LOW_TEMP_WINDOW=None.

## LOW cap $5 + h2pk gate disabled — 2026-05-22

Two push-entry tweaks:
- **LOW max bet $1 -> $5** (`max_bet_low_series_usd`). The faithful WR accumulator
  (real executed trades, gold standard) shows LOW is now a positive book: near-min
  pure-nn 65% (n=23), per-station 60-88%. The price-gated backtest reads 0% but only
  on n=5/KPHX (LOW candle backfill barely started; it contradicts KPHX's own 88% real
  record), so that's a coverage gap, not a signal.
- **h2pk gate disabled** (`PUSH_MIN_H_TO_PEAK_HIGH` 0.5 -> None). The per-station HIGH
  windows all end >=1h before peak, so the at-peak gate can never fire -- redundant
  under the current windows. Disabled, not deleted (code guards on None); set back to
  0.5 to re-enable.

## Per-station HIGH push windows (v1) + faithful WR accumulator — 2026-05-22 05:45 UTC

HIGH decision windows are now **per-station** for 8 cells, replacing the single
global deep-pre-peak window for those stations. `PUSH_HIGH_TEMP_WINDOW_BY_STATION`
(config.py) is looked up first in `nn_shadow_worker._in_decision_window` (gated
behind the global `PUSH_HIGH_TEMP_WINDOW` being set); a station absent from the
dict falls back to the global `(3.0,-2.0)` default.

| station | (before, after) | buys at |
|---|---|---|
| KATL | (2.0, -1.5) | peak-2.0 |
| KAUS | (1.5, -1.0) | peak-1.5 |
| KBOS | (3.0, -2.5) | peak-3.0 |
| KMDW | (2.5, -2.0) | peak-2.5 |
| KMIA | (2.5, -2.0) | peak-2.5 |
| KNYC | (3.5, -3.0) | peak-3.5 |
| KPHL | (2.5, -2.0) | peak-2.5 |
| KPHX | (3.0, -2.5) | peak-3.0 |

**Why.** A price-gated mu-replay backtest (`tools/push_window_backtest.py`)
reproduces the faithful per-cell WR once the bot's asymmetric price gate is
applied (pre-peak HIGH 62% vs faithful 58%; the earlier "~20pp understatement"
was the MISSING price gate, NOT a mu error). The per-band WR table
(`tools/per_cell_table.py`) showed (a) at-peak entries lose at every station
(validates the h2pk>=0.5 gate) and (b) per-station sweet spots. **AUS is the key
change**: it LOSES deep pre-peak (29% WR) and only wins near peak, so the global
deep window sat in its dead zone.

**v1 / thin data.** Windows are from May-2026 candle depth only (n~8-11/band) --
to be regenerated from the deep historical backfill (`candle-hist-backfill.service`,
pulling prior-year May). 30-min windows may reduce fill count vs the prior 60-min;
watch and widen if positions drop. **Reversible:** set
`PUSH_HIGH_TEMP_WINDOW_BY_STATION = {}` to revert all HIGH to the global.

**Faithful WR accumulator** (`tools/accumulate_cell_wr.py` + `accumulate-cell-wr.timer`,
daily 12:00 UTC): records every real executed trade + Kalshi settlement to
`~/data/faithful_cell_wr.jsonl` -- gold-standard per-cell WR, no replay. Seeded
184 trades; corroborates HIGH pre-peak 56% vs at-peak 31%.

## LOW cold-front gate — 2026-05-21 21:28 UTC

The pure-nn push path now skips **LOW** buys when sustained wind at the
overnight low signals a frontal / cold-air-advection regime. Distinct from
the Tier 1 wind gate (40 mph, both sides, catastrophic) — this fires far
lower (≈ 15 kt) and LOW-side only.

**Why.** 25-yr backtest of the nn matcher's own accuracy (3.17M evals,
`~/data/phq_offset_cond_combined.csv`, conditioned on 18 observable regime
buckets):
- **HIGH is storm-robust** — MAE flat (~1.4-1.6 °F) across every storm
  regime, no systematic bias. No new HIGH gate is justified; the existing
  Tier 1 gate covers its catastrophic tail.
- **LOW collapses under sustained wind.** With METAR sustained wind > 15 kt
  in the trade window, the matcher's MAE jumps to 3.1-4.3 °F (vs ~1.7 °F
  calm) and it carries a **systematic bias of +1.6 to +3.1 °F** in the cold
  season — it over-projects the daily minimum, because a frontal passage
  delivers a much colder low than the warm pre-frontal trajectory implies.
  Cross-year validated (train < 2024 vs holdout 2024-25); holds at 18 of 20
  stations.
- **Sigma does not flag it.** ~68% of the badly-over-projected LOW rows are
  NOT in the matcher's high-sigma bucket; within sigma=low, sustained wind
  still drives bias to +1.25 °F. Sigma widening protects against variance,
  not against a biased mean — so the bot would otherwise compute a confident
  edge off a wrong center. That is why this is a hard gate, not a sizing
  tweak (the global regime-MAE sizing already shrinks the bet, but cannot
  re-center it).
- Pressure tendency, temperature volatility, and slope were tested and
  rejected as noise (falling pressure is just the normal diurnal cycle).

**Gate** (`tier1`-sibling block `(2c)` in
`nn_shadow_worker._try_auto_execute`, fires right after the Tier 1 wind
gate, before the price floor):

- `low_frontal_wind`: `series == "LOW"` AND
  `wethr_obs.wind_speed_mph >= PUSH_LOW_FRONT_WIND_MPH` (default 18 mph
  ≈ 15 kt). **Sustained wind only** — a gust without sustained wind is
  convective (thunderstorm outflow), not a frontal cold-air advection, and
  does not carry the bias.
- `PUSH_LOW_FRONT_EXCLUDE` (default `("KLAX", "KMIA")`) — marine-climate
  stations where strong wind is onshore sea-breeze, not a cold front
  (backtest bias ≈ 0 there in both seasons).

**Discord alert.** When the gate blocks a would-be LOW buy it fires a
throttled Discord alert (`_alert_low_front`), deduped per
`(station, climate_day)` — one message per station per day, e.g.
`⛔ LOW COLD-FRONT GATE KMSP 2026-01-15: skipping LOW push BUYs — sustained
wind 24mph (≥18mph). Matcher over-projects the low in frontal regimes.`
Firing rate ≈ 3.8% of LOW evals/year (5-6% in cold-season months, ~1% in
summer).

**Disable knob:** set `PUSH_LOW_FRONT_WIND_MPH = 0` (or negative) in
`config.py` and restart.

**Files:**
- `config.py` — adds `PUSH_LOW_FRONT_WIND_MPH = 18.0`,
  `PUSH_LOW_FRONT_EXCLUDE = ("KLAX", "KMIA")`.
- `nn_shadow_worker.py` — new gate-2c block in `_try_auto_execute` plus the
  throttled `_alert_low_front` Discord helper.
- `tests/test_low_front_gate.py` — 10 new tests (threshold boundary,
  HIGH-unaffected, excluded stations, missing-wind fail-open, zero-disable,
  Discord-alert dedup).

Full suite 416 passed / 4 skipped / 1 pre-existing unrelated fail
(`test_truncation_reduces_buy_no_edge_when_rm_in_yes`). Commit 8dd3dcf.
Backups `config.py.pre_lowfront_gate_20260521`,
`nn_shadow_worker.py.pre_lowfront_gate_20260521`.

## Window table is the SOLE window source (no default fallback) — 2026-05-21 20:55 UTC

`push_window_overrides.PUSH_WINDOW_OVERRIDES` is now the ONLY source of the
push decision window. Removed the global default fallback
(`PUSH_PEAK_HOURS_BEFORE` / `AFTER_HIGH` / `AFTER_LOW` / `AFTER`) from config.
The "override" framing implied an optional tweak on top of a default — a
confusing second source of truth that also let the bot silently trade cells
where no window was ever validated.

`_in_decision_window` now:
- cell present → window `[peak-before, peak+after]`, `src=window_table`
- cell missing → NOT traded + throttled Discord alert (`_alert_missing_window`,
  dedup per station/series/month). No silent gaps.
- `USE_PUSH_WINDOW_OVERRIDES=False` → clean master kill-switch (no trade, no alert)

Coverage verified 480/480 (all 40 May cells present) → no current trade stops;
this only removes the fallback-that-masks-gaps and makes any future gap loud.
Tests updated (kill-switch + missing-cell-alert). 388 passed / 4 skipped.
Commit f76fb1e. Backups `*.pre_window_sole_20260521`.

## Peak-data alerts: no silent failures on peak lookup — 2026-05-21 20:53 UTC

Two throttled Discord alerts added to `nn_shadow_worker`:
- **(a) missing_peak** — `_lookup_peak_hour` returned None (no peak in fractional
  OR pace_curves) → cell not traded; previously a silent skip.
- **(b) frac_fallback** — precise fractional peak missing for (station,side,MM-DD),
  silently substituting the coarse pace_curves int hour. Currently fires only for
  KDCA in February (DCA Feb history is ~32% sparser than ATL, so the 5yr/10-day
  rolling P50 has no value for 02-03..02-12 + 02-24..02-28). Zero in-season
  impact; surfaced so any future in-season frac gap is never silent.

Peak lookup is two-tier: fractional table (`peak_fractional_5yr_10day.json`,
PRIMARY) → pace_curves int (`pace_curves_v2.json`/`_low_v2.json`, FALLBACK).
Full-year sweep (14,600 lookups) confirmed pace_curves fallback is 480/480
complete, so a true None never occurs today — (a) is a safety net, (b) is the
real current silent path. Dedup per (kind,station,series,climate_day).
Commit bb52111. Backup `nn_shadow_worker.py.pre_peak_alerts_20260521`.


## Global regime-MAE adjustment for sizing — 2026-05-21

The conditional fields (anomaly, sigma, sky, wind) now feed sizing — as a
GLOBAL correction on top of the per-cell MAE, not per-cell slicing. Per-cell
regime slicing failed (too few samples/slice → noise). The global delta —
"across ALL cells, hot-anomaly days run +0.98°F MAE, cloudy +0.51, strong-wind
+0.75, high-sigma +0.71; cold/clear/calm/low-sigma run negative" — is learned on
200K–1M days each, so it's precise and stable train→holdout. Adding it lifts
per-decision MAE-prediction corr 0.167 → 0.229 (out-of-sample).

`nn_shadow_worker._regime_adjusted_mae`: `adjusted_mae = cell_mae + DAMP * Σ
delta(dim, today's bucket)` for sigma/anomaly/sky/wind, then `_mae_conf_mult`
tiers off the adjusted MAE. A clear/calm day revises the cell MAE *down* (size
up toward the cap); a hot+cloudy+windy day revises it *up* (size down).
**Sizing-only — never flips a bet** (unlike the reverted bias). Flag
`USE_PUSH_REGIME_MAE_ADJ`, damping `PUSH_REGIME_MAE_DAMP=0.6` (dims correlate).

Inputs: `sigma_natural` (exposed from `shadow_nn_proj`), `wethr_obs`
cloud_1_coverage + wind_speed_mph, anomaly = current temp − climate normal.
Tables (gitignored, regenerate via `tools/per_hour_quality/export_regime_tables.py`):
`data/regime_mae_deltas.json` + `data/climate_normals_hourly.json`.
Verified live: 66/66 decisions adjusted. Backups `*.pre_regime_20260521`.

**Update (later 2026-05-21): tiers re-calibrated + tspeak folded in.** Holdout
calibration of `predicted regime-MAE → actual MAE` is monotonic for adjusted
MAE ≥ ~1.0, but the favorable extreme over-corrects (predicted 0.57 → actual
1.63 — additive deltas extrapolate below the ~1.2°F irreducible floor). Re-tiered
`PUSH_MAE_CONF_TIERS` to `<1.6→1.0× / 1.6-2.4→0.7× / 2.4-3.2→0.5× / >3.2→0.3×`
(per-tier actual MAE ~1.4/1.67/2.21/3.81); the wide lowest tier means the
over-corrected favorable extreme just caps at full size (no over-sizing).
`tspeak` (time since the running extreme; stale +0.72) added as a 5th regime
delta — runtime via `rm_age`. **Bugfix:** `_build_shadow_packet` was calling
`get_rm_age_sec(station,"high"/"low")` but it expects `"max"/"min"` → had
silently returned None forever (rm_age_sec logged as None; tspeak never fired).
Fixed → 78/78 decisions now apply all 5 regime dims. Backups `*.pre_tiers_20260521`.

## Push override: HIGH median-bias + MAE confidence-sizing — 2026-05-21

> **UPDATE 2026-05-21 (later): bias REVERTED, MAE sizing KEPT.** A Kalshi-settled
> replay of 5/20's HIGH book (16-6) showed the median-bias would have flipped 2
> Minneapolis winners→losses (→14-8), all from MSP's −0.8 bias over-correcting on
> a cold day across the bracket boundary, with zero losses avoided. Marginal
> +2.1% avg MAE not worth the boundary-flip risk → `USE_PUSH_BIAS_CORRECTION=False`.
> `USE_PUSH_MAE_SIZING=True` stays (only scales size, never flips a bet). Bias is
> still logged (`push_override.bias`) for analysis, just not applied.

Out-of-sample validation (train 2000-23 → holdout 2024-25, 79,248 decisions)
turned the dormant `bias`/`mae` override fields into two live levers in
`nn_shadow_worker._evaluate_ticker`:

- **MEAN-bias correction was REJECTED** — applying `μ −= mean_bias` made holdout
  MAE 8.6% WORSE (errors are skewed; the mean is outlier-inflated, the median
  error is ~0, so subtracting the mean over-corrects). The override file's bias
  field is now the **MEDIAN** residual (`patch_median_bias.py`).
- **MEDIAN-bias correction, HIGH only** (`USE_PUSH_BIAS_CORRECTION`): −2.1% HIGH
  holdout MAE (159/235 cells); LOW was neutral so it's excluded. `μ −= bias`
  after the matcher sets μ, before `pure_nn_decide`, so edge/p_yes use the
  calibrated μ. Raw μ kept in `mu_pre_bias`.
- **MAE confidence-sizing** (`USE_PUSH_MAE_SIZING`): a cell's expected MAE
  predicts its out-of-sample accuracy (corr 0.62, monotonic), so bet size scales
  down where the matcher is less reliable — `PUSH_MAE_CONF_TIERS` (≤1°F→1.0x,
  1-1.5→0.75x, 1.5-2.5→0.5x, ≥2.5→0.3x; None→0.5x). Only ever reduces size
  (risk-reducing). `mae_conf_mult` logged per decision.

Honest framing: the bias turned out **marginal** (+2.1% HIGH only), not the big
lever it first appeared — MAE-based sizing is the more useful validated signal.
Both gated, both logged, both reversible via their flags. Verified live: HIGH
decisions carry `bias_applied`, LOW do not; `mae_conf_mult` on all.
Backups `nn_shadow_worker.py.pre_biassize_20260521`, `config.py.pre_biassize_20260521`,
`push_window_overrides.py.pre_medianbias_20260521`. Full context:
`PUSH_OVERRIDES_PLAN.md` §4a.

## Push window overrides — full 480/480 coverage + per-cell bias — 2026-05-21 13:50 UTC

Regenerated `push_window_overrides.py` from the corrected 18-dimension
conditional-MAE backtest (`per_hour_quality_v3.py`, 2000-2025, 3.17M rows,
alti-based pressure + graceful per-dim degradation). The new file ships a
`(before, after, bias)` 3-tuple for **every** (station, side, month) cell —
480/480 coverage, up from 424 (and only 329 after the old MAE gate). Replaces
the prior `build_overrides_accuracy.py` output.

**Generator changes (`build_overrides_hierarchical.py`):**
- **Every cell ships its own window + bias.** Removed the old `MAE ≥ 0.7°F →
  delete` gate on the unconditional window — a cell's own data-driven window
  always beats the hand-picked global default (2.5h/1.5h). The 0.7°F gate now
  only governs *conditional* regime entries (the 22 ultra-precise refinements).
- **Bounds relaxed to physical sanity** (full offset span [-4,+1]); the old
  arbitrary [0,4.5]/[-2.5,1.5] fences had rejected 56 cells whose own window
  was simply early or narrow.
- **Width-collapsed cells widened around their OWN best offset** to the minimum
  tradeable width (0.5h) — never borrow from neighbors when the cell has its own
  timing. Where the widened window lands post-peak, the bot's h2pk gate
  correctly declines (the cell isn't predictable pre-peak — don't force a bet).
- **Holdout demoted from veto to confidence flag.** A window whose 2024-25 MAE
  degraded >1.5× vs train is no longer dropped — it's still the cell's best
  *timing* estimate (the test only says accuracy is less stable out-of-sample,
  itself noisy on ~55 holdout days). These 37 cells are emitted as
  `PUSH_WINDOW_LOW_CONFIDENCE` for later conservative handling.
- Neighbor→season→cross-station→default fallback exists for cells with *no*
  usable own window — fires for **0 cells** today (future insurance).

**The bias is the prize.** Each tuple's 3rd element is the pooled pre-peak
`mean(mu_proj − actual_extreme)` over the window — the matcher's *residual*
systematic error after its own internal per-side correction. Range −1.69 to
+3.04°F (LOW winter months carry large positive bias = matcher over-projects
cold-night lows). For ~2°F brackets this can flip which bracket μ lands in.

**Tuple format is now `(before, after, bias, mae)` — 2026-05-21 14:xx UTC.**
`mae` is the cell's expected pre-peak accuracy (°F), range 0.71–4.34, p50 1.58.
`nn_shadow_worker._lookup_push_override()` surfaces the matched entry and
`_evaluate_ticker` stamps `pkt["push_override"] = {before, after, bias, mae, src}`,
which is written into every `shadow_nn_strategy.jsonl` record. This is
**observability only** — `mae`/`bias` are logged per decision (as a future
confidence/sizing signal) but NOT yet applied to the trade. Lets us validate
MAE-based sizing on settled trades before committing to it. Conditional entries
carry their regime's (lower) MAE; the *regime-conditional* MAE at runtime needs
the Phase-2 bucketing. The lookup handles legacy 2-/3-tuples (bias/mae → None).

**Deployment status (IMPORTANT):** the **windows are live now** —
`_in_decision_window` reads `ov[0]/ov[1]`. The **bias (`ov[2]`) and mae (`ov[3]`)
are logged but NOT applied** — applying bias needs a code change in
`nn_shadow_worker._evaluate_ticker` (`pkt["mu_chosen"] -= bias` after the matcher
sets μ, before `pure_nn_decide`), gated behind a new `USE_PUSH_BIAS_CORRECTION`
flag. That, MAE-based sizing, the 22 conditional entries, and low-confidence
handling are the deferred Phase 2.

**Validation:** 480/480 coverage, 0 pathological windows, sane bias distribution,
per-station streaming generator (no OOM). Backup
`push_window_overrides.py.pre_fullcoverage_20260521`. PID 831618 single+current,
clean startup.

**Maintenance:** the per-cell bias is coupled to the matcher config at backtest
time. If `NN_BIAS_CORR_*` or sigma factors change, regenerate via
`python3 tools/per_hour_quality/build_overrides_hierarchical.py >
push_window_overrides.py`.

## In-bracket tail-bet gate (Gate 2) — 2026-05-20

The pure-nn push path now raises the edge floor for a structurally-doomed
trade shape: when the nn `mu_chosen` sits INSIDE the YES window
`[floor-0.5, cap+0.5)` but the bot picks the smaller-mass (tail) side
(`p_chosen < 0.5`), it is betting against its own central estimate for a thin
edge. There is no weather regime where "I think it lands in the bracket, but
I'll bet it doesn't" is systematically a winning play.

**Gate** (`nn_shadow_worker._try_auto_execute`, folded into the edge-floor
check): for a candidate where `mu_chosen ∈ YES window` and the chosen side's
probability `< 0.5`, require `edge >= PUSH_TAIL_BET_MIN_EDGE_PP` (default 25pp)
instead of the base `PUSH_MIN_EDGE_PP` (12pp). Skip reason: `edge_below_floor …
(tail_bet mu_in_YES p_chosen=…)`. The YES window is computed per bracket shape
(mirrors `nn_shadow_strategy._yes_window`): B = `[floor-0.5, cap+0.5)`,
T-warm (floor only) = `[floor+0.5, +inf)`, T-cold (cap only) = `(-inf, cap-0.5)`.

**2026-05-20 extension — T-brackets:** originally B-only; extended to T-brackets
after **HOU T84 BUY_NO** (mu=83.0 sat in the T-cold YES region `< 83.5`, bot bet
the NO tail at p_chosen=0.41) slipped past the B-only gate and lost -$5.16.

**Backtest** (5/19+5/20 settled pure-nn pool, the full push-arch history at
ship time): B-only version blocked 4 (CHI B80.5, LAX B73.5, PHIL B71.5, PHIL
B95.5), 4 losers, **0 winners killed**, +$13.87. The T extension additionally
catches HOU T84 (-$5.16). Mechanism is clean — unlike the sibling **boundary-gap
gate (Gate 1, PARKED)** which on the same pool blocked 14 trades but killed 3
real winners (DAL +$9.75, SFO +$2.97, DEN +$3.52) because near-edge calls are
genuine coin-flips. Gate 1 may return with a trend-aware carve-out once more
settled data exists.

**Tests:** `tests/test_push_tail_bet_gate.py` — 10 tests pinning trigger
(mu-in-YES + tail side + edge below floor for B and T brackets), non-trigger
(majority side, mu outside bracket/region), base-floor independence, disable.

**Rollback:** `PUSH_TAIL_BET_MIN_EDGE_PP = 0` in config.py and restart.
**Backups:** `config.py.pre_tail_bet_gate_20260520`,
`nn_shadow_worker.py.pre_tail_bet_gate_20260520` (B-only),
`*.pre_hicap_tbracket_20260521` (T-extension + HIGH cap raise).

## HIGH-series bet cap raised $5 → $15 — 2026-05-20

`GUARDRAILS.max_bet_high_series_usd` raised `5.0 → 15.0` (Chris directive). HIGH
is the profitable book (5/20: HIGH +$40.29 vs LOW −$24.23), so bet size leans
into it. `max_bet_yes_usd` also raised `10.0 → 15.0` so HIGH BUY_YES can reach
$15 — guardrails *rejects* (not truncates) over-cap bets, so the old $10 YES cap
would have killed $15-sized HIGH YES entries. LOW BUY_YES is unaffected (still
capped at $5 by `max_bet_low_series_usd`). The push sizing in
`nn_shadow_worker` now reads `max_bet_high_series_usd` / `max_bet_low_series_usd`
from `GUARDRAILS` as the single source of truth and passes them to
`pure_nn_decide` so the qty is sized to match the cap. Verified: HIGH NO/YES
size to ~$14.8, LOW stays ~$5.

**Risk note:** this 3× HIGH sizing also 3×'s the downside on a bad HIGH day.
**Rollback:** set `max_bet_high_series_usd = 5.0` (and optionally
`max_bet_yes_usd = 10.0`) in `config.py` GUARDRAILS and restart.

## LOW-series bet cap cut $5 → $1 — 2026-05-21

`GUARDRAILS.max_bet_low_series_usd` cut `5.0 → 1.0` (Chris directive). LOW is
the losing book (5/20: LOW −$24.23 vs HIGH +$40.29) — shrink exposure to a
token size while the nn LOW projector keeps misfiring (tight σ, high stated
edge, frequent misses; see the LOW BUY_YES −$19.58 cohort).

**Min-buy gotcha (and fix):** a $1 cap with the default $1 `min_buy_usd`
recreates the 2026-05-17 integer-contract collapse — when `min_buy == cap`, no
integer qty satisfies both `cost ≥ $1 floor` and `cost ≤ $1 cap` except at
exact-divisor prices (50c, 25c, 20c), so nearly all LOW buys would silently
skip. Fix: new `PUSH_MIN_BUY_USD_LOW = 0.40`; the push worker passes it as the
LOW min-buy into `pure_nn_decide` so LOW places genuine ~$0.40–$1.00 bets
across the price range (verified: 34c→$0.68, 50c→$1.00, 70c→$0.70, 79c→$0.79).
HIGH keeps the standard $1 min-buy (its $15 cap never binds on min-buy).

**Rollback:** set `max_bet_low_series_usd = 5.0` in `config.py` GUARDRAILS
(and optionally `PUSH_MIN_BUY_USD_LOW = 1.0`) and restart.
**Backups:** `config.py.pre_lowcap1_20260521`,
`nn_shadow_worker.py.pre_lowcap1_20260521`.

## Tier 1 push-buy runtime gates (vsby + wind) — 2026-05-20 20:09 UTC

The pure-nn push path now skips buys when wethr_obs reports a
physics-catastrophic regime the nn matcher was never trained to represent.
This protects the matcher from being asked to predict during dense fog /
heavy precipitation (diurnal cycle suppressed) or extreme wind
(tropical / severe storm regime).

**Gates** (both in `nn_shadow_worker._try_auto_execute`, fire after the
push decision-window check and before price-floor):

- `tier1_vsby`: `wethr_obs.visibility_miles < PUSH_MIN_VSBY_MI` (default 0.5
  mi). Falls back to `wethr_obs.visibility` if `visibility_miles` is absent.
  Visibility doubles as a precipitation proxy — heavy rain/snow reliably
  drops vsby below 1 mi. A real precip-rate gate is a follow-up (the
  wethr cache doesn't currently emit a precip_in_h field; once it does,
  we'll add `PUSH_MAX_ACTIVE_PRECIP_INH`).
- `tier1_wind`: `wind_speed_mph > PUSH_MAX_WIND_MPH` OR
  `wind_gust_mph > PUSH_MAX_WIND_MPH` (default 40 mph ≈ 35 kt). Sustained
  winds at that level are tropical storm / severe-thunderstorm regimes
  that mix the boundary layer aggressively and decorrelate from any
  diurnal-shape analog the matcher could find.

**Why no backtest required:** these are physics-obvious, low-firing-rate
gates. Dense fog and 40+ mph winds are <1% of station-days. The mechanism
is structural, not statistical (no nn-trained analog days exist for these
regimes, so any prediction is implicit extrapolation). Threshold tuning
won't move them onto the wrong side — 0.5 mi vs 1.0 mi vsby doesn't change
the qualitative call.

**Disable knob:** set either threshold to `0` (or negative) in `config.py`
to bypass the corresponding gate. Removing the gate without rollback is a
one-line config edit + restart.

**Files:**
- `config.py` — adds `PUSH_MIN_VSBY_MI = 0.5`, `PUSH_MAX_WIND_MPH = 40.0`.
- `nn_shadow_worker.py` — new gate-2b block in `_try_auto_execute` reading
  `packet["wethr_obs"]`.
- `tests/test_push_tier1_gates.py` — 9 new tests pinning gate behavior
  (below/above threshold, fallback field, missing field fails open,
  zero-threshold disables, sustained vs gust trigger paths).

Full suite: 345 passed / 4 skipped (was 336/4 — +9 from new tests).

**Open follow-up (Tier 2):** sigma_natural and forecast-disagreement gates
require backtest validation before ship.

**Backups:** `config.py.pre_tier1_gates_20260520`,
`nn_shadow_worker.py.pre_tier1_gates_20260520`.

## HIGH h_to_peak gate (block at-or-past-peak entries) — 2026-05-20 22:07 UTC

Defense-in-depth on top of the window override. New gate (2a) in
`nn_shadow_worker._try_auto_execute`: HIGH-series entries with
`h_to_peak < PUSH_MIN_H_TO_PEAK_HIGH` (default 0.5) are blocked. At
peak, `rm` has converged on the day's true max, leaving no room for the
nn_match `mu` projection to add real signal — instead it
over-extrapolates and flips adjacent brackets the wrong way.

**Today's losses caught (would have been blocked):**
- `KXHIGHPHIL-26MAY20-B95.5` BUY_NO @ 15c (h2pk=-0.04) → -$3.64
- `KXHIGHTNOLA-26MAY20-B86.5` BUY_NO @ 62c (h2pk=-0.01) → -$4.88
- `KXHIGHTNOLA-26MAY20-B88.5` BUY_YES @ 36c (h2pk=-0.01) → -$4.55
- Total: **-$13.07 saved, 0 winners blocked.**

**Backtest:** n=47 executed HIGH push trades (27 settled 5/19 + 20 today
fair_pnl proxy). Filter sweep:

| T | blocks | h:hu | lift | buffer to nearest winner |
|---|--------|------|------|--------------------------|
| 0.5 | 3 | 3:0 | +$13.07 | +0.20 (winner at h2pk=+0.70) |
| 0.7 | 4 | 4:0 | +$17.83 | +0.00 (zero buffer, rejected) |

Chose 0.5 for the +0.20 buffer + bulletproof mechanism (T=0.7 catches
BOS B90.5 at h2pk=+0.55 — same direction but 33 min pre-peak so the
"rm has converged" claim is weaker).

**Why this is defensible at low n:** mechanism is mechanical, not
statistical. At peak, `rm` is essentially the final `day_max`; any
upward μ projection is structurally wrong. Confirms the 5/19 h2pk
deepdive ("HIGH at-peak entries catastrophic: h2pk in [-99, 0.5) WR
31%") which today's accuracy-first override regen didn't fully
constrain.

**Tests:** `tests/test_h2pk_gate.py` — 8 new tests (at-peak blocked,
past-peak blocked, below-threshold blocked, at-threshold allowed, above
allowed, LOW series unaffected, None h2pk allowed, config-None
disables). Full suite 353 passed / 4 skipped.

**Rollback:** `PUSH_MIN_H_TO_PEAK_HIGH = None` or `= 0.0` disables.
Or revert: `cp nn_shadow_worker.py.pre_h2pk_gate_20260520 nn_shadow_worker.py && cp config.py.pre_h2pk_gate_20260520 config.py && sudo systemctl restart paper-judge-bot.service`.

**Notes for future readers:**
- HOU T84 today (h2pk=+1.7, lost) is NOT caught — different mechanism
  (T-cold BUY_NO with high σ inflated edge). Separate filter needed.
- LOW series intentionally not gated; mechanism may apply but no data
  validates yet. Revisit if LOW losses cluster near peak/min.
- Recommended follow-up: audit `push_window_overrides.py` for cells
  with `after > -0.3` and decide whether to tighten. The h2pk gate
  makes that audit lower-pressure.

## Position cap scoped to candidate.climate_day — 2026-05-20 19:30 UTC

Bug fix. The per-(station, series_prefix, direction) position cap in
`nn_shadow_worker._try_auto_execute` was counting positions across ALL
climate_days. So a stuck position from a previous climate_day blocked
today's BUY decisions on the same station+series.

**Trigger:** KMSY 5/19 HIGH NOLA positions (B86.5 BUY_NO, B90.5 BUY_YES)
sat unsettled in positions.json because Kalshi never posted `result`
(NOAA Tmax dispute on KMSY). Both `status=closed result=` via API,
`expected_expiration_time=2026-05-20T19:00:00Z` (passed), hard deadline
2026-05-26T14:00:00Z. Last-hour analysis at 19:00 UTC: 36 KMSY HIGH
BUY_NO decisions + 7 BUY_YES blocked by `position_cap BUY_NO@KMSY/KXHIGH`
even though no live-day KMSY HIGH position existed.

**Fix:** add a date check inside the cap-counting loop. Only positions
whose `date_str` (or fallback `climate_day`) matches `cand.climate_day`
count toward the cap.

```python
if p.get(action) != direction:
    continue
# 2026-05-20: scope cap to candidate's climate_day so a stuck
# prior-day position (e.g. KMSY 5/19 Kalshi-pending settlement)
# doesn't block today's BUYs on the same station+series.
pos_date = p.get(date_str) or p.get(climate_day)
if pos_date and pos_date != cand.climate_day:
    continue
n_existing += 1
```

**Tests:** `tests/test_position_cap_climate_day.py` — 4 new tests:
- prior-day position does NOT block today's BUY (regression for the fix)
- same-day position STILL blocks (no regression on intended behavior)
- prior-day opposite-direction position irrelevant (direction filter check)
- climate_day field fallback works when date_str absent

Full suite: 336 passed / 4 skipped (was 332/4 — added 4 new, no regressions).

**Notes for future readers:**
- The matching correlation cap at `nn_shadow_worker.py:482` (`cap_key =
  (cand.station, side_label, cand.climate_day)`) was already climate_day-
  scoped — this fix aligns the position cap with it.
- Prior-day positions still appear in positions.json until the settle
  worker resolves them (waiting on Kalshi `result` field). The cap fix
  doesn't affect settlement — it just stops them from blocking new BUYs
  during the wait.
- Backup file: `nn_shadow_worker.py.pre_climateday_poscap_20260520`.

## WS drift guard — block inverted BBO at write+read — 2026-05-20 19:32 UTC

`kalshi_ws` was caching inverted BBO state (yes_bid + no_bid > 100 → impossible
arb) during transient races between Kalshi's delta updates. nn_shadow_worker's
packet builder calls `kalshi_ws.get_bbo()`, so pure-nn was firing phantom-edge
BUY signals on ~6.1% of evaluations today (3,958 of 65,237). Scout's REST
fallback caught most at execution time ("no level passed edge gate") but the
signal layer was still wasting CPU + Discord alerts on cases that could never
fill.

**Root cause.** Kalshi WS sends one delta per book update. When matched orders
are removed, two deltas arrive sequentially. Between them, `_recompute_bbo`
sees only one side removed → inverted cache. Existing drift detection scheduled
a resync but still wrote the bad BBO into the cache. 30s per-ticker rate limit
made recovery slow.

**Source-adjacent fix — refuse to publish intermediate state.** Three coordinated
changes in `kalshi_ws.py`:

1. `_recompute_bbo`: drift check moved before cache write; on inversion, skip
   `_bbo_cache[ticker] = new_bbo` AND suppress BBO callback. Consumers keep
   seeing last known-good BBO. Counter `cache_writes_skipped_inverted`.
2. `get_bbo`: defensive read-side re-check; returns None if cache somehow holds
   inverted state. Counter `bbo_reads_blocked_inverted`. Caller falls back to
   REST (already-existing path).
3. `_RESYNC_PER_TICKER_RATE_SEC` 30.0 → 5.0. Drift recovery window shrinks from
   up to 30s to up to 5s. Max ~46 resyncs/sec at 230 tickers — well under
   Kalshi rate limits.

Plus stats formatter extended to include `cache_skip_inv` and `read_block_inv`
on the periodic log line.

**Note: scout/execute_buy WS-first orderbook (Item 3 of the fast/safe plan)
was already shipped 2026-05-16** — `kalshi_ws.get_orderbook` already rejects
inverted state. This ship closes the same gap on the BBO path used by the
signal layer.

**Module flag for instant rollback:** `_INVERSION_GUARD_ENABLED = True` at top
of `kalshi_ws.py`. Set False + restart to revert.

**Live verification (4 min post-restart):**
- `inv=112` (drift detections), `cache_skip_inv=112` (100% blocked, perfect 1:1)
- `read_block_inv=1` (read-side caught a startup-state case)
- Shadow evals since restart: 212 packets, **0 arb-stale (0.0%)** vs **6.1% baseline**
- `resync_drift=56`, `resync_sent=3`, `resync_rl=56` (rate-limit working at 5s)

**Files shipped.** kalshi_ws.py (3 logic edits + 2 stat counters + log-formatter
extension). Tests 336 passed / 4 skipped. Backup `kalshi_ws.py.pre_drift_guard_20260520`.

## Scout label: claude_read → nn_read (push pure-nn path) — 2026-05-20 18:00 UTC

Cosmetic + diagnostic fix. The scout's `prob_source` field was hard-coded to
"claude_read" whenever the regex in `_claude_prob_for_side()` matched a
`P(YES)=X.XX` or `P(NO)=X.XX` literal in `decision.read`. With LLM_DISPATCH_MODE=off
since 2026-05-19, the only thing populating `decision.read` is the push pure-nn
worker, which builds a synthetic EntryDecision with read="pure-nn auto: BUY_YES
edge=21.7pp P(YES)=0.567 ..." — i.e., the regex matches the NN-derived prob
embedded in the strategy's own reason string. So the "claude_read" label was
misleading: no Claude call had happened, and the prob was from nn_match_fast.

Fix: in `_claude_prob_for_side()`, when the regex matches, check whether the
read starts with "pure-nn auto" and return "nn_read" instead of "claude_read".
Legacy path (if LLM dispatch is ever re-enabled) still returns "claude_read"
for real Claude responses. Three unit-style cases verified against the running
module: push synthetic→nn_read, llm-style→claude_read, no-P-fallback→edge_info.

**Notes for future readers**:
- `prob_source` field appears in `data/by_date/<day>/scouts.jsonl` for every
  scout invocation, telemetry-only (not used for filtering or branching).
- The scout itself isn't an LLM thing — it does orderbook-level sweep planning
  inside `execute_buy()` for any entry path (LLM or pure-nn). When you see a
  scout SKIP "no level passed edge gate", it's because the live REST orderbook
  diverged from the WS BBO that triggered the entry: by the time the scout
  fetched the book, the top quote that pure-nn keyed on was filled or pulled.
  This is normal anti-staleness behavior, not a bug.

**Files shipped.** paper_judge_bot.py (7-line replacement at _claude_prob_for_side).
Backup: paper_judge_bot.py.pre_nn_read_label_20260520. Tests 332 passed / 4
skipped. PID 2420694.

**Rollback.** Trivial: replace the conditional return with `return p, "claude_read"`.

## Push F1 rm-staleness validator — 2026-05-20 14:31 UTC

Wired wethr_rm.validate_rm_for_climate_day into nn_shadow_worker._build_push_packet.
The validator already existed (paper_judge_bot.py:458,753 uses it for the LLM
dispatch + exit paths) but nn_shadow_worker was reading wethr.get("low_f") /
wethr.get("high_f") straight into the packet without the climate-day match check
— the push pure-code arch shipped 2026-05-19 inherited that gap.

**Bug it fixes.** 2026-05-20 KAUS LOW B67.5 BUY_NO loss (-$78). At entry, wethr
cache held low_f=66, date_low=2026-05-19, time_of_low_utc=2026-05-20 02:53 UTC
(i.e., 5/19 21:53 CDT — still 5/19 LST climate day). Bot used that 5/19 rm as
the 5/20 rm-lock anchor. NWS CLI confirms 5/20 KAUS min was 68F at 4:54 AM →
in YES window [66.5, 68.5] → BUY_NO loses. KOKC LOW B53.5 (-$72) hit the same
pattern (low_f=53, date_low=2026-05-19).

**Kalshi boundary confirmation.** API call to KXLOWTAUS-26MAY20-B67.5 returns
close_time=2026-05-21T06:00Z = LST midnight ending 5/20 (KAUS = CST, UTC-6).
Rules: settles on NWS Climatological Report (Daily). Matches the LST midnight
encoding in wethr_rm.lst_midnight_utc_ts that the validator was already using.

**What the validator checks (per wethr_rm.validate_rm_for_climate_day):**
1. station has a TZ mapping
2. climate_day parses
3. cache_date string equals ticker climate_day (the gate KAUS failed)
4. now >= LST_midnight + grace_sec
5. time_of_extreme_utc, if present, falls within [LST_midnight, LST_midnight+24h)

Per-side LST date preferred: date_low for LOW tickers, date_high for HIGH
(wethr-cache-service derives these from time_of_*_utc since 2026-05-16). Falls
back to wethr's legacy "date" field if the per-side field is absent.

**Config (config.py):**
- PUSH_VALIDATE_RM_CLIMATE_DAY: bool = True   # instant rollback flag
- PUSH_RM_GRACE_SEC_LOW: float = 900.0        # 15-min grace; matches LLM-path LOW
- PUSH_RM_GRACE_SEC_HIGH: float = 3600.0      # 60-min grace; matches LLM-path HIGH

**Files shipped.** nn_shadow_worker.py (imports + ~30-line validator block at
_build_push_packet), config.py (3 new constants). Tests 332 passed / 4 skipped
— same as pre-patch baseline (no new tests added; behavior covered by the
existing wethr_rm validator tests). Backups:
- nn_shadow_worker.py.pre_f1_push_20260520
- config.py.pre_f1_push_20260520
- README.md.pre_f1_push_20260520

**Verified live.** Restart via "sudo systemctl start paper-judge-bot.service"
(PID 2066812; previous nohup orphan PID 2059438 cleaned up — Rule #3 violation
during initial debugging, corrected immediately). F1 warnings fire on KAUS LOW
5/20, KOKC LOW 5/20, KLAX HIGH 5/20 (cache held 5/19 rm=75 for KLAX HIGH because
today's high had not yet beaten yesterday's at that point). Fresh-date stations
(KDFW LOW 5/20, KAUS HIGH 5/20) kept their rm unchanged.

**Rollback.** Set PUSH_VALIDATE_RM_CLIMATE_DAY=False in config.py and restart.

## Push window overrides: ACCURACY-FIRST — 2026-05-20 11:56 UTC

Fundamental redesign of the override selection criterion. Previously the
windows were chosen by the contiguous good-run filter (firing>=65%,
settled<=30%, n>=20). The settled<=30% gate was too tight — it threw out
the most-accurate offsets (closest to peak, where trajectory is most
complete).

New criterion: pick the offset with LOWEST mae_pre_peak, expand to
contiguous offsets within 1.15 * best_mae. Only constraint on settled:
< 100%. This puts windows tightly around the matcher's most accurate
region, accepting that many days will be settled-at-trade-time —
we only need the 10-30% of unsettled days for profitable bets.

ALSO: critical simulator bug fixed. The replay was deduping BUY events
by first occurrence per (ticker, action), so it never tried later BUY
events when the first was outside the window. Real bot retries every
push event until one passes all gates. Fixed simulator now properly
counts all events.

Sample changes (per cell):
  ATL HIGH May:  was (4.0, -1.0)    →  (1.0, -0.5)
                 window 4h to 1h before peak  →  1h to 30min before peak
  ATL LOW May:   was (3.5, -1.5)    →  (1.5,  0.5)
                 → window 1.5h before to 30min AFTER min

May 19 replay (with FIXED simulator):
  CURRENT  (settled<=30%, grid-snapped):   n=35  PnL=-\$25.22  ROI=-15.2%
                                            settled subset: 13 trades, -\$30.81
  NEW      (accuracy-first, settled<100%): n=31  PnL=+\$21.77  ROI=+14.6%
                                            settled subset: 5 trades, -\$2.38

ΔPnL = +\$47 on May 19. HIGH BUY_NO alone swung -\$4 → +\$34 (n=10).

Coverage 424/480 (was 462). Narrower windows reject more cells where
no offset has reliable enough MAE within the cap.

Live verification:
  ATL HIGH 2026-05-01 at h=15.0:  window [14.4, 14.9]  (inside ← yes)
  ATL HIGH 2026-05-19 at h=15.0:  window [14.9, 15.4]
  ATL HIGH 2026-06-15 at h=15.0:  window [13.8, 15.3]  (June override)

Tests: 350 passed / 4 skipped / 1 pre-existing fail. Test mocks updated
to use h values inside the new (narrower) windows.

Rollback: cp push_window_overrides.py.pre_accuracy_20260520 ...

Honest caveats:
  - One-day replay sample (May 19). The +\$47 swing is mechanism-sound but
    needs cross-day validation.
  - 0.5h offset grid resolution is at the data noise floor; a finer grid
    re-run would not change the conclusions materially.
  - HIGH BUY_YES still bleeds (-\$5.71 even with accuracy-first). The
    boundary-confidence problem isn't a window problem; it's a mu-bias
    problem on the recent hot regime.

## Push window overrides: linear-interp endpoints — 2026-05-20 11:42 UTC

Refinement of the 11:33 offset-derived overrides. Previously, the offset
grid was 0.5h granular so endpoints like (4.0, -1.0) looked artificially
integer-snapped. This commit applies linear interpolation between adjacent
offset grid points to find:
  - LATE end: exact offset where `settled_share` crosses 30%
  - EARLY end: exact offset where `mae_pre_peak` crosses 1.3 × best_mae

Endpoints are now genuinely fractional, e.g.:
  ATL HIGH May:  (4.0, -1.0)   →  (4.0, -0.811)
  ATL HIGH Feb:  (2.0, -0.5)   →  (1.884, -0.745)
  ATL HIGH Apr:  (3.0, -0.5)   →  (2.659, -0.689)

Many `before` values remain at grid boundary (e.g., 4.0 for HIGH) because
the MAE curve doesn't cross the cap within the {-4.0, ..., +1.0} grid
range — extending the grid would help but isn't done yet since stderr per
cell (~0.05°F at n=432) is already at the data's resolution limit.

Generator: `/home/ubuntu/tools/per_hour_quality/build_overrides_offset_interp.py`

Coverage: 463/480 (was 462). 17 cells fall back to global.

Live window verification (continuous endpoints):
  ATL HIGH 2026-05-01: window [11.4, 14.6]  (peak 15.42)
  ATL HIGH 2026-05-19: window [11.9, 15.1]  (peak 15.88)
  ATL HIGH 2026-05-31: window [11.9, 15.1]  (peak 15.88)
  ATL HIGH 2026-06-15: window [12.3, 14.5]  (June override, peak 15.30)

May 19 replay: n=18, PnL +$3.22, ROI +3.8%.  (Slightly more permissive
than non-interp version (n=12, +$5.65) because settled crossover at
-0.811 instead of grid-snapped -1.0 admits the extra entries near peak.)

Tests: 350 passed / 4 skipped / 1 pre-existing fail.

Honest note on resolution: at n=432 per cell, MAE stderr is ~0.05°F.
Adjacent 0.5h-grid MAE values often differ by less than that, so the
"true" finer-than-0.5h endpoint estimate is mostly noise. Interpolation
produces principled fractional values without claiming finer signal than
the data supports.

## Push window overrides from FRACTIONAL OFFSET 5000-day backtest — 2026-05-20 11:33 UTC

Re-ran the 5000-day MAE backtest, but binned by **fractional offset from
fractional peak** (5yr 10-day rolling) instead of integer LST hour. End-to-
end fractional: no integer hour bins anywhere in the pipeline.

Why this matters: the previous (per-LST-hour) MAE pooled across days with
different peak times, so "LST hour 11" mixed days where peak was at 13 (2h
gap) with days where peak was at 15 (4h gap). Per-offset analysis always
compares the matcher at consistent peak-relative positions.

Methodology:
  1. /home/ubuntu/tools/per_hour_quality/per_hour_quality_v2.py walks every
     historical station-day. For each (station, side, day-of-month), looks
     up the 5yr-10day fractional peak. For each offset in
     [-4.0, -3.5, ..., +1.0] (0.5h grid), computes
     cur_lst = frac_peak + offset, runs nn_match.predict(), records err.
  2. Aggregates per (station, month, offset, side) to phq_offset_combined.csv.
     n=5280 cells from ~100K station-days × 22 offsets.
  3. /home/ubuntu/tools/per_hour_quality/build_overrides_offset.py picks
     tight_win = contiguous offset bins with firing>=65% AND settled<=30%
     AND n>=20 AND mae <= 1.3*best_mae. Min window width 0.5h.
  4. before = -offset_lo, after = offset_hi.

Sample ATL HIGH May (all months also regenerated):
  Previous (per-LST-hour 800-day):  (2.867, -0.867)  window [13.0, 15.0] at May 15
  NEW      (offset 5000-day):        (4.0, -1.0)      window [11.88, 14.88] at May 19

Coverage: 462/480 cells (was 460). 18 fall back to global.

May 19 replay PnL (real settlements + bid MTM):
  CURRENT (frac-aligned per-LST-hour 800-day): n=17  PnL=-$3.45  ROI=-4.3%
  NEW (offset-derived 5000-day):                n=12  PnL=+$5.65  ROI=+10.1%

The NEW windows are MORE SELECTIVE (n=12 vs n=17) and produce
+$9 lift on May 19. Per-direction:
  HIGH BUY_NO:  +$5.53 (was +$1.16)
  HIGH BUY_YES: -$10.12 (was -$4.20)   ← still the regime bleeder
  LOW BUY_NO:   +$3.84  (was -$7.65)   ← biggest swing
  LOW BUY_YES:  +$6.40  (was +$7.24)

Day-by-day drift + month rollover both verified live:
  ATL HIGH 2026-05-01: peak 15.42, window [11.4, 14.4]
  ATL HIGH 2026-05-19: peak 15.88, window [11.9, 14.9]
  ATL HIGH 2026-06-15: peak 15.30, window [12.3, 14.3]  (June override picks up)

Source data (NEW):
  /home/ubuntu/data/per_hour_quality_offset/  (per-station CSVs)
  /home/ubuntu/data/phq_offset_combined.csv   (aggregated, 5280 rows)

Tests: 350 passed / 4 skipped / 1 pre-existing fail.

Rollback: cp push_window_overrides.py.pre_offset_20260520 push_window_overrides.py + restart.

## Push window overrides regenerated frac-aligned — 2026-05-20 07:00 UTC

Follow-up to the fractional peak source ship 09 min earlier. Overrides were
previously generated against integer peak; with the bot now using fractional
peak, the effective LST windows had drifted ~0.5h later than the MAE-derived
data hours. This regen anchors override values to the fractional peak so the
effective LST windows match the data-derived hours exactly.

Generator: `/home/ubuntu/tools/per_hour_quality/build_overrides_frac.py`
Reference peak per (K-station, side, month) = `peak_fractional_5yr_10day.json`
at month-midpoint (day 15). Quality bounds widened proportional to the int-
vs-frac delta:
  HIGH: before [0.0, 5.5]  after [-3.0, 1.5]   (was 4.5 / [-2.0, 1.5])
  LOW:  before [0.0, 7.0]  after [-3.0, 1.0]   (was 5.5 / [-2.5, 1.0])

Coverage: 460/480 cells (was 470 under int alignment). 10 fewer cells admit
overrides; those fall back to the global window.

What changed in practice:
  ATL HIGH May before  2.0 → 2.867  (the +0.867 = int-vs-frac peak delta)
  ATL HIGH May after   0.0 → -0.867
  Effective window     [13.0, 15.0] LST — UNCHANGED at month midpoint

Day-by-day drift now captured. Same ATL HIGH May override (2.867, -0.867):
  May 1  + frac peak 15.42  → window [12.6, 14.6]
  May 19 + frac peak 15.88  → window [13.0, 15.0]
  May 31 + frac peak 15.95  → window [13.1, 15.1]

Month-boundary rollover verified live:
  ATL HIGH 2026-05-19 → src=override window=[13.0,15.0] (May override picks up)
  ATL HIGH 2026-06-15 → src=override window=[13.0,15.0] (June override picks up, no restart)

May 19 sim PnL re-checked under the regen: $+6.91 ROI +7.6% (n=19).
Same as the pre-frac-peak baseline. The +$9 gain that came from running
int-aligned overrides through the new frac peak source was an accidental
window-shift-later effect; this regen reverts that for methodological
consistency. The +$9 sample was n=15 on a single day (regime-mismatch day
that lost -$34 in real settled outcomes anyway) — not strong evidence.

Tests: 350 passed / 4 skipped / 1 pre-existing fail. Tests updated to
mock frac peak and assert window ranges rather than hardcoded values.

Rollback: `cp push_window_overrides.py.pre_fracalign_20260520 push_window_overrides.py`
and restart. Or set `USE_FRACTIONAL_PEAK_FOR_WINDOW=False` to revert to the
int peak source AND int-aligned overrides simultaneously.

## Fractional peak source (5yr 10-day rolling) — 2026-05-20 06:51 UTC

`nn_shadow_worker._lookup_peak_hour` now returns the 5-year 10-day-rolling
P50 of `day_max_lst_min` / `day_min_lst_min` from heating_traces.sqlite
(per K-station, side, month-day), instead of `int(empirical_peak_hour_local)`
from pace_curves_v2.json.

Source data: `/home/ubuntu/data/peak_fractional_5yr_10day.json` — 14,610
cells (20 stations × 2 sides × 366 days, minus 30 cells with <10 samples).
Window per cell: ±5 days × last 5 years. Generator:
`/home/ubuntu/tools/per_hour_quality/build_peak_frac.py`.

Validated on n=99,812 historical station-days (window-hit-rate = % of days
where the bot's decision window actually contains the day's true peak time):

  HIGH:  int 29.8% → frac 38.0%   (+8.2pp, +27% relative)
  LOW:   int 29.3% → frac 36.9%   (+7.6pp, +26% relative)

Replay PnL on yesterday's May 19 candidates (n=15 fills under frac vs
n=19 under int, using current overrides):

  int peak  + current overrides:  PnL=+$6.91   ROI=+7.6%
  frac peak + current overrides:  PnL=+$15.93  ROI=+22.5%   ← shipped

Mechanism: per-(station, side, month) overrides were generated against
integer peak. Switching the peak source shifts every effective window
LATER by 0-1h (the int-vs-frac delta). This drops the "way before peak"
early entries, which is where most boundary-noise BUY_YES losers sat.

Implementation:
  config.py adds
    USE_FRACTIONAL_PEAK_FOR_WINDOW: bool = True
    PUSH_PEAK_FRACTIONAL_PATH: str = "/home/ubuntu/data/peak_fractional_5yr_10day.json"
  nn_shadow_worker.py:
    _peak_table_frac_cache, _min_table_frac_cache loaded by
    _ensure_peak_tables_loaded(). _lookup_peak_hour() returns frac when
    flag is on AND cell exists, else falls back to int (legacy).

Override values were NOT regenerated. Doing so would re-align windows to
the same LST hours and reverse the gain — the win is precisely from the
window-shift-later effect.

Tests: 350 passed / 4 skipped / 1 pre-existing fail.

Rollback: set USE_FRACTIONAL_PEAK_FOR_WINDOW=False in config.py + restart,
or restore from .pre_frac_peak_20260520 backups.

Caveat: KNYC HIGH is the only station with negative window hit-rate lift
(-1.2pp). Single-station noise on n=1; not worth a per-station veto.

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
