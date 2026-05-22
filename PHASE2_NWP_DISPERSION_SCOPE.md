# Phase 2 Scope: NWP-Dispersion Good-Day Predictor

Scoped 2026-05-22, after Phase 1 (the judge's *internal* features) failed cross-
validation. Phase 2 brings in an **external signal the judge does not currently
use** — NWP model agreement — to predict the high-WR days. This is the only path
left that isn't re-fitting the data we've already exhausted.

## Hypothesis
When independent NWP models **agree** on a station's daily high (low forecast
dispersion), the day's outcome is predictable → the judge's far-bracket BUY_NO bets
win. When they **disagree** (high dispersion), the extreme is uncertain → bets lose.
Trade only the low-dispersion days.

## Why this is genuinely untested (not just "the weather bots found no edge")
The weather bots use NWP as the *forecast* (to set μ) and the market out-calibrated
that μ in all conditions. Phase 2 uses NWP differently: **dispersion as a day-
selection meta-filter** (trade-or-skip), layered on the judge's trajectory-matched
bets. That specific use has never been tested. Still low-odds — but a real, distinct
hypothesis.

## The signal
Per (station, climate_day): **dispersion of the daily-high forecast across
independent models / ensemble members**, e.g. the std-dev of {GFS, ECMWF-IFS, ICON,
GEM} daily-Tmax, and/or GEFS ensemble spread. Lower std = higher agreement = candidate
"good day."

## Data acquisition (the crux / hard part)
We need this **multi-year** to cross-validate. Options, cheapest first:
1. **Open-Meteo Historical-Forecast API** (`historical-forecast-api.open-meteo.com`)
   — past forecasts from multiple models, back ~2–3 yr, free (bot already has
   `OPEN_METEO_API_KEY`). Pull daily-Tmax per model per (station, date) → cross-model std.
   *Primary source for 2a.*
2. **GEFS ensemble spread** (NOAA reforecast / Open-Meteo `gfs_seamless` ensemble) —
   a direct predictability measure (member spread on the forecast day).
3. **Bot's own recent logs** (`weather_candidates_*.jsonl`, v1/v2 max) — recent only;
   use to sanity-check the API-derived dispersion against what the bots actually saw.

## Method
1. Build `forecast_dispersion[(station, day)]` from source #1 (and #2).
2. Join to the judge's multi-year backtest bet outcomes (the per-(cell,day) pre-peak
   BUY_NO results we already compute).
3. **Cross-validate** (train ≤2024 / test ≥2025): bin bets by dispersion tercile;
   is the low-dispersion tercile **robustly profitable out-of-sample**? (The exact
   bar that edge/σ/anomaly/analog-dispersion all failed.)

## Phasing & decision gates
- **2a — PoC (~2–3 days):** deep-6 cells only, Open-Meteo multi-model dispersion,
  ~2–3 yr, the cross-validated tercile test. **GO/NO-GO:** low-dispersion tercile must
  be test-positive with adequate n. If not → stop; the thin-edge verdict is final.
- **2b — expand (only if 2a passes):** all 20 cells, add GEFS ensemble spread, longest
  available history, robust per-cell + pooled validation, define the dispersion gate
  threshold.
- **2c — ship (only if 2b robust):** add the dispersion gate to `_try_auto_execute`
  (trade only when dispersion < threshold), or as a sizing multiplier. Same
  test+commit+push+restart workflow.

## Effort & honest risk
- Effort: 2a ≈ 2–3 days (Open-Meteo integration + backfill 6 cells × ~3 yr + the
  analysis harness, which already exists from Phase 1).
- **Odds: low-to-moderate.** Every predictor tested so far has failed the holdout. The
  case for hope is narrow: dispersion-as-day-filter is a mechanism we haven't tried,
  and forecast disagreement is a plausible real proxy for "unpredictable extreme."
- **Kill criterion:** if the 2a low-dispersion tercile isn't test-positive, we stop and
  accept that the HIGH good-days are not forward-predictable from available data. No
  further sweeping.

## What it does NOT change
The robust core stays as-is regardless: NYC/MIA BUY_NO-only, $3/$5 caps, LOW paused,
near-peak blocked. Phase 2, if it works, only adds a day-filter on top.
