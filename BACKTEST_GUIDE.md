---
name: reference-judgebot-backtest-guide
description: "AUTHORITATIVE guide for backtesting paper_judge_bot — the database, schema, conventions, the validated backtest recipe, the capture-gap trap, and the tools. READ FIRST for any judge backtest/data work."
metadata: 
  node_type: memory
  type: reference
  originSessionId: 6510740e-c9ce-4a1b-b03a-cf33f04b35eb
---

# Judgebot Backtest & Database Guide (read first for any judge data work)

Everything below is on the weather VPS: `ssh -i ~/.ssh/kalshi-bot-key ubuntu@54.225.174.220`.
Bot dir: `/home/ubuntu/paper_judge_bot` (git repo `judgebot.git`, branch main —
commit+push every change). The bot is REAL money (account2 wallet).

## ⚡ Golden rules (the five things that bite)
1. **AUDIT the DB before any "no data / illiquid / thin n" conclusion.** Low n is
   USUALLY a capture gap, not a real market limit. Run `tools/db_audit.py` first.
   (This trap caused two wrong calls on 2026-05-22: HIGH "thinly traded" + LOW
   "illiquidity wall" — both were the gap. See [[reference-backtest-db-capture-gap]].)
2. **Apply the bot's PRICE GATE in every backtest** — NO-ask (=100−yes_bid) ∈ [10,80].
   Skipping it understates win rate by ~20pp. See [[feedback-mu-replay-needs-price-gate]].
3. **FAITHFUL = buy at the window OPEN.** The bot fires on the first in-window cycle
   with a qualifying bracket and the position cap (=1) blocks later cycles, so entry
   pins to the deep/early edge (h ≈ `before`). Sim must buy there, not the best moment.
4. **Validate OOS with the early/late date split** (split the live era at its median
   date; a real edge is positive in BOTH halves). NO cross-year on the live era — and
   the cross-year HISTORICAL data is a DIFFERENT/older regime, do NOT trust it for the
   live bot (it said "thin edge / NYC-MIA only"; the live era says broader). 
5. **The market out-calibrates the model almost everywhere.** The only validated edge
   is HIGH BUY_NO **deep pre-peak, closing BEFORE the peak**. Trading into the peak
   loses (market sharp at the extreme). LOW NO loses crossing the wide spread.

## The database — `/home/ubuntu/data/market_history_backfill.sqlite`
Tables:
- `market_meta(ticker, station, side, climate_day, bracket, floor, cap, result,
  close_time, volume)` — settled markets. `result` ∈ {yes, no, ""}. `floor`/`cap` =
  bracket strikes (Bxx.5 brackets are 2°F wide: floor ≤ actual ≤ cap).
- `candle_history(ticker, station, side, climate_day, bracket, ts, yes_bid, yes_ask,
  price, volume, open_interest)` — hourly (60-min) BBO in **CENTS**. `ts` = end_period_ts
  (Unix UTC).
- `hist_progress(ticker, done)` — backfill progress (done=1 → skipped on re-run).

Conventions:
- `station` = "K"+IATA (KNYC, KMIA, KDCA…). `side` ∈ {HIGH, LOW}.
- `climate_day` = the market's settlement day in LST.
- candle ts → LST: `utcfromtimestamp(ts + off*3600)`, `off` = station UTC offset
  (below). Keep candles where `lst.date == climate_day`.
- `price_at(date, br, t)`: latest candle with `0 < t − candle_min ≤ 60` and valid
  yes_bid/yes_ask.

Coverage: after the 2026-05-23 gap-fill, ALL 40 series have ~67 live-era days (back
to ~2026-03-15). Pre-03-15 is sparse (only the deep-history stations).

## THE CAPTURE GAP (re-read rule #1)
`tools/backfill_historical_candles.py` pulls only `/historical/` — OLD markets back
to launch, but it **PRUNES the recent ~67 days**. The live era comes from the LIVE
endpoint, which that tool never queries. So the DB silently loses the live era.
- AUDIT: `tools/db_audit.py` (all 40 series, DB vs live endpoint).
- FILL: `tools/fill_gap.py` (idempotent).
- Endpoints (public, no auth, HOST=https://api.elections.kalshi.com):
  - live settled (recent ~67d): `/trade-api/v2/markets?series_ticker=<S>&status=settled&limit=1000`
  - candles (recent): `/trade-api/v2/series/<S>/markets/<TK>/candlesticks?start_ts=&end_ts=&period_interval=60`
    — **LIVE schema = `yes_bid.close_dollars` (DOLLARS → ×100 for cents)**, NOT `.close`.
  - historical (old only): `/trade-api/v2/historical/markets...` (`.close` in cents).

## The model data — `/home/ubuntu/data/phq_ext/phq_raw_<IATA>.csv.gz`
The matcher's per-cycle projections. Columns incl: `side`, `date`, `offset`,
`cur_lst_min`, `mu_proj_f` (projected extreme °F), `sigma_proj_f`, + regime features.
- `offset` = signed hours; **h = −offset = hours to the extreme** (peak for HIGH, min
  for LOW; h>0 before, h<0 after). NATIVE 0.5h grid (~11 rows/day) → a 30-min window
  = one slot.
- Coverage starts 2026-02-18. **DCA's phq is STALE (ends 2026-01-15) → DCA can't be
  backtested** even though its market/candle data fills fine.

## How the bot decides (replicate this in a backtest)
1. matcher (nn_match, k-NN trajectory) → μ + σ. Pure code (LLM off).
2. P(extreme in bracket) = Normal(μ,σ): `p_yes(floor,cap,mu,sig)` (the erf helper used
   in every tool). σ floor 1.5.
3. edge = |p_yes − market_implied|; trade if ≥ `PUSH_MIN_EDGE_PP`/100 (=0.12).
4. SIDE: BUY_NO when p_yes < yes_ask/100; cost(cross) = 100−yes_bid. BUY_YES else;
   cost = yes_ask.
5. PRICE GATE: NO-ask ∈ [10,80]; YES-ask ∈ [30,80] (`PUSH_MAX_ENTRY_C`=80,
   `PUSH_MIN_ENTRY_C_BUY_YES`=30). MUST apply.
6. SPREAD: bot CROSSES (pays cost above). spread = yes_ask − yes_bid. Wide spread eats
   the edge. HIGH gate `PUSH_MAX_SPREAD_C_HIGH`=15. packet exposes `yes_bid_c`/`yes_ask_c`.
7. WINDOW: per (station, side, month). `(before, after)` = [extreme−before,
   extreme+after]; bot buys at the OPEN = extreme−before. Live config:
   `PUSH_HIGH_TEMP_WINDOW_BY_STATION` (30-min per-station), `PUSH_LOW_TEMP_WINDOW`,
   month-scoped via `PUSH_TEMP_WINDOW_MONTHS`. `PUSH_EARLY_TRIM_HIGH_ENABLED` must stay
   False (it caps before→1.0).
8. settle: BUY_NO wins if result==no; pnl = (100−cost) if win else −cost (cents/contract).

## The validated backtest recipe (faithful, gated, OOS)
One bet/day at the window-open: rows sorted by h DESC (deepest first), take the FIRST
row in the window with a qualifying gated bracket (max-edge), settle it. Gate:
edge≥0.12 AND NO-cost∈[10,80]. Then split early/late at the median date and require
BOTH halves positive. DATA FIDELITY: faithful accumulator (real fills) > price-gated
backtest > μ-replay. Beware phantom MTM on empty books (yes_ask=0/no_ask=None sentinel).

## Stations + UTC offsets
−5: ATL BOS MIA NYC PHL DCA · −6: AUS DFW HOU MDW MSP MSY OKC SAT · −7: DEN PHX ·
−8: LAX SEA SFO LAS. Deep history: NYC 2021, MIA/AUS/MDW 2023, DEN/PHL 2024, LAX 2025;
13 T-series HIGH + 20 LOW launched 2026.

## Tools (`paper_judge_bot/tools/`)
- `db_audit.py` — DB-vs-live gap audit (RUN FIRST). `fill_gap.py` — fill gaps.
- `backfill_historical_candles.py` — original backfill (OLD data only; don't rely on it
  for the live era).
- `window_sweep_station.py`, `window_30min_sweep.py` — per-station HIGH window sweeps.
- `low_gated_split.py` — LOW offset analysis. `window_*_low.py` (5) — another session's
  LOW suite (side / offset-curve / sweep / validate / confirm).
- `spread_analysis.py` (cross/mid/passive by spread), `spread_validate.py` (filter OOS).

## Current state pointers (may drift — verify live)
HIGH = 30-min per-station BUY_NO windows + spread filter, $15/pos. LOW = $1 live probe
(execution test). See [[project-live-era-deep-prepeak-no-edge-20260522]],
[[project-spread-filter-low-probe-20260523]], [[project-nyc-mia-no30-judge-20260522]].
