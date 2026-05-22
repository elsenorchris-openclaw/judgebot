# Research Scope: Good-Day Predictor (paper_judge_bot HIGH)

Scoped 2026-05-22. Goal: find a signal that predicts the high-WR days *in advance*
so we can trade only those — turning the thin HIGH edge profitable.

## The problem (what motivates this)
HIGH pre-peak win rate averages **~53% (negative PnL)** but varies enormously day
to day — May-20 hit **93%**. If we can identify the good days ahead of time, we
trade them and skip the rest. But cross-validation has shown the model's existing
features **do not** predict good days:
- edge magnitude, |anomaly|: no out-of-sample predictive power.
- **σ is ANTI-predictive** — low-σ ("confident") bets win only 35% OOS. The
  matcher's own confidence ≠ accuracy (σ-undercalibration).

So a real fix needs **new signals the model doesn't currently use**, not more
sweeping of existing ones (that space is exhausted and overfits).

## Hypothesis
Good days = **stable, predictable synoptic regimes** (high pressure, models agree,
no front). Bad days = transitions / frontal passage / model disagreement, where the
matcher's μ is unreliable.

## Candidate features (priority = cheapest + most-likely first)
1. **Forecast dispersion** — spread across the NWP sources the bot already ingests
   (NBM, HRRR, ECMWF-IFS via `kalshi-s3-cache.service`) + the matcher's neighbor
   spread (`sigma_natural`, `pool_size`). Low spread = agreement = predictable.
   *Cheapest — data already flows.*
2. **Frontal-passage / instability** — pressure tendency (have `pres_tend`), wind
   shift & speed (have `wind_kt`, `drct`), temp-gradient / 850mb thickness change
   (needs NWP fields). A front signals an unpredictable extreme.
3. **Trajectory cleanliness** — deviation of the day's obs trajectory from the
   climatological pace curve (partial via `pace_curves`). Smooth track = predictable.

## Implementation
Compute a per-(station, day) **predictability score** from the feature(s); use it as
a GATE (trade only when score > threshold) and/or a sizing multiplier.

## Validation bar (non-negotiable)
- Cross-year: train ≤2024 / test ≥2025, per-cell + pooled.
- **SUCCESS = high-score days are robustly profitable out-of-sample** (held-out
  PnL > 0, adequate n) — i.e., the feature does what edge/σ/anomaly failed to do.
- If it doesn't lift held-out WR, it's noise → the edge isn't forward-predictable →
  stop and accept the thin-edge verdict.

## Phasing
- **Phase 1 (~1–2 days):** compute forecast-dispersion on the data we already have,
  correlate with pre-peak WR, cross-validate. Cheapest, most-likely signal.
- **Phase 2 (only if Phase 1 holds OOS):** backfill the feature historically (the
  hard part — dispersion history may not be archived; may need reconstruction or a
  limited window), full per-cell cross-validation.
- **Phase 3 (only if robust):** ship as a day-quality gate/sizing on the judge.

## Honest risk assessment
Every feature tested so far has failed cross-validation. The edge may genuinely not
be forward-predictable from available data. Realistic odds of success: **moderate-to-
low.** Phase 1 is the cheap go/no-go; if dispersion doesn't predict WR out-of-sample,
the thin-edge conclusion stands and we stop. The win condition is honest: a *new*
signal that survives the same cross-validation that killed everything else.
