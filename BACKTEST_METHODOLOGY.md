# Push-Window Backtesting Methodology (paper_judge_bot)

How to backtest the pure-nn push strategy *correctly*. Written 2026-05-22 after a
multi-day effort to find why our μ-replay backtest disagreed with reality by ~20pp.
Read this before trusting any window/WR number.

---

## 0. The one mistake that cost us 20pp: forgetting the bot's PRICE GATE

A μ-replay backtest (replay the matcher's μ over historical prices, recompute
edges, settle) **understated win rate by ~20 percentage points** until we modeled
the bot's **asymmetric price gate**:

- `BUY_YES`: only if `yes_ask ∈ [30, 80]` (cents)
- `BUY_NO`:  only if `no_ask  ∈ [10, 80]` (cents)

Without it, the replay "buys" cheap longshot BUY_YES (yes_ask < 30) that the live
bot rejects. Those mostly lose and drag aggregate WR down. We spent days blaming a
μ obs-source error (archived ASOS vs live SSE, ~0.7°F) — that was **not** it.
Adding the gate alone reconciled the replay: pre-peak HIGH **62%** vs the faithful
**58%**. If your backtest disagrees with reality, check the gates you're NOT
modeling before you blame the signal.

**Every push backtest = window + PRICE GATE + edge≥12 + position cap.**

---

## 1. Data fidelity hierarchy (use the highest tier you can)

1. **Faithful accumulator** — the bot's REAL executed trades + Kalshi settlement.
   Gold standard, zero modeling. Thin (only what was traded) but accrues forward.
   `tools/accumulate_cell_wr.py` → `~/data/faithful_cell_wr.jsonl` (daily timer).
2. **Faithful shadow-log replay** — the bot's live logged decisions
   (`data/shadow_nn_strategy.jsonl`: real `mu_chosen`, `edge_pp`, `market.no_ask_c`
   / `yes_ask_c`, `signals.h_to_peak`) + settlement. Covers ALL cells but only the
   pure-nn era (~days). No μ-replay error because the prices/edges are what the bot
   actually saw. This is the right tool for "what would a different window have done
   over the last few days."
3. **μ-replay over historical candles** — phq μ × Kalshi historical candle prices ×
   settlement. The only way to get *years* of depth, but it carries a ~0.7°F μ
   understatement and REQUIRES the corrections in §3. Treat as RELATIVE; always
   validate against tier 1/2 before trusting absolute numbers.

---

## 2. The faithful backtest recipe (tiers 1–2)

Per `(station, climate_day, side)`:
- collect the bot's decisions in the candidate window,
- keep those with `edge_pp ≥ 12` AND passing the price gate (§0),
- **position cap**: one BUY_NO + one BUY_YES per `(station, series)` per day; the
  bot buys the FIRST qualifying by time → that's the **window START**. So a window's
  WR ≈ the WR of the bet placed at its earliest qualifying moment.
- settle via Kalshi `markets/{ticker}` `result` (authoritative; `yes`/`no`).

Reference: `tools/shadow_bt3.py` (validated: pre-peak 60% vs actual 58%, near 31%
vs 33%); `tools/shadow_percell.py` (per-cell version).

---

## 3. The μ-replay recipe (tier 3 — historical depth)

Inputs: `~/data/per_hour_quality_offset_cond/phq_raw_<IATA>.csv.gz` (per-date μ back
to 2000; **has `cur_lst_min`**) × `~/data/market_history_backfill.sqlite`
(candle_history + market_meta).

Three corrections, all mandatory:
1. **Apply the price gate** (§0). This is the big one.
2. **Price lookup at the matcher's own per-row `cur_lst_min`** — NOT a peak-derived
   or month-averaged time. Averaging the peak misaligns the candle lookup by 1–2h
   (prices move fast near peak) and silently corrupts every bet.
3. **Offset relative to the per-day TRACE peak** (`heating_traces.station_days.
   day_max_lst_min` for HIGH, `day_min_lst_min` for LOW) — the actual daily extreme
   time, not a rolling average.

**Validation gate:** before trusting a μ-replay, confirm it reproduces the faithful
(tier 1/2) WR. Ours hit 62% vs 58% pre-peak only after all three corrections.

Reference: `tools/push_window_backtest.py`, `tools/per_cell_table.py`.

---

## 4. Kalshi data sources + gotchas

- **Historical tier** (no auth): `/trade-api/v2/historical/markets?series_ticker=X`
  (paginated via `cursor`) and `/historical/markets/{ticker}/candlesticks`. Retains
  back to each series' launch (years for some). The live `/markets` endpoint prunes
  to ~67 days — don't use it for history.
- **Candle BBO** is under `yes_bid.close` / `yes_ask.close` (integers, cents) in the
  HISTORICAL schema. The LIVE schema uses `.close_dollars` — different field; mixing
  them silently yields garbage prices.
- **Bracket geometry**: `Bxx.5` brackets are 2°F wide (`floor ≤ a ≤ cap`).
- **Settlement** is authoritative from `markets/{ticker}.result`. ASOS
  `weather_actuals` is ~1–2°F off the Kalshi CLI settlement — do NOT calibrate on it.

---

## 5. Launch dates cap how deep any cell can go (this is a hard ceiling)

Backtest depth is bounded by when Kalshi listed each series:
- **Legacy `KXHIGH<CITY>` (deep, 2021–2025):** NY (2021-08), MIA/AUS (2023-05),
  CHI/MDW (2023-08), DEN/PHIL (2024-11), LAX (2025-01). ~7 HIGH stations.
- **`KXHIGHT<CITY>` + ALL `KXLOWT<CITY>` (shallow, 2026):** the other 13 HIGH and
  all 20 LOW launched Feb–Apr 2026 → ~1–2 months of price history exists, PERIOD.

So **only ~7 of 40 cells can ever have multi-year windows.** For the rest, the
forward faithful accumulator (tier 1) is the only path to depth — there is no
historical data to backfill.

---

## 6. Profit ≠ accuracy (don't optimize windows on MAE)

Windows built to minimize μ error (MAE) drift toward the extreme (peak/min) because
that's where μ is most accurate. But the MARKET is sharp there (the extreme is
nearly known → no edge), and soft *before* the extreme. So:
- **HIGH profit is PRE-peak** (h_to_peak ≈ 2–3h, market soft); at-peak loses.
- **LOW** (from faithful trades): deep-pre-min (h2pk≥2) is the weak zone (40%);
  near/post-min is better (65%, thin n).

Optimize windows on **WR/PnL, not MAE.** A high-WR window can still lose if it buys
expensive favorites — screen on PnL too.

---

## 7. When NOT to ship a window

- The backtest that produced it does NOT reproduce the faithful WR → fix the
  backtest first (§3 validation gate).
- The cell has thin per-band n (n < ~8 per offset band is noise; n=1–2 is a
  coin-flip). Shipping a noise-window is worse than a validated default.
- For thin/no-data cells, keep the validated aggregate default (HIGH: deep-pre-peak
  `[peak-3, peak-2]`) rather than a per-cell guess. Data-driven > hand-picked, but a
  validated aggregate > per-cell noise.

---

## 8. Tool index (in `tools/`)

| tool | purpose |
|---|---|
| `push_window_backtest.py` | validated price-gated μ-replay (the §0 fix) |
| `per_cell_table.py` | per-(station,side) 30-min START-window WR table |
| `shadow_bt3.py` / `shadow_percell.py` | faithful shadow-log backtest (tiers 1–2) |
| `accumulate_cell_wr.py` (+ `.timer`) | daily faithful accumulator (tier 1) |
| `backfill_historical_candles.py` | pulls historical-tier candles → sqlite |
