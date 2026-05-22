# Exact May-20-2026 Setup (paper_judge_bot)

The config that produced the 2026-05-20 HIGH result. Captured 2026-05-22 from
judgebot commit `c9f10c4` (the accuracy-first window regen that was live on 5/20)
plus the gate values from that day's commits.

## Performance that day (HIGH, from the trade ledger + Kalshi settlement)
- **14W / 6L = 70%** (20 executed HIGH trades).
- **Pre-peak (h2pk ≥ 1): 13W/1L = 93%.**
- **Near/at-peak (h2pk < 1): 1W/5L = 17%** — every near-peak trade but one lost.
- The whole result was driven by *timing*: pre-peak won, near-peak lost.

## Gates / filters (May-20 values)
| gate | value |
|---|---|
| edge floor | `PUSH_MIN_EDGE_PP = 12` (pp) |
| h2pk gate (HIGH) | `PUSH_MIN_H_TO_PEAK_HIGH = 0.5` — block entries < 0.5h to peak |
| price gate | BUY_YES `yes_ask ∈ [30, 80]¢`; BUY_NO `no_ask ∈ [10, 80]¢` |
| tier-1 skip | sustained wind/gust > 40 mph; visibility < 0.5 mi |
| per-bet cap | ~$5 / position (HIGH) |
| position cap | 1 BUY_NO + 1 BUY_YES per (station, series) / day; buy at first qualifying |
| peak source | fractional 5yr-10day rolling peak |
| window source | `PUSH_WINDOW_OVERRIDES` (c9f10c4) — SOLE source, no temp window, no per-station overlay, no early-trim |

Note: the windows below are **accuracy-first** and many open *near* the peak —
the `h2pk ≥ 0.5` gate is what kept them from buying *at* the peak. Window +
gate work together.

## Windows — all 40 cells
Format: `(before, after)` → window `[peak − before, peak + after]` (peak = daily
max for HIGH, daily min for LOW), in hours. "—" = no window in the c9f10c4 table,
so that cell did **not** trade on May 20.

### HIGH (15 traded, 5 had no window)
| station | (before, after) | window |
|---|---|---|
| KATL | (1.0, -0.5) | [pk-1.0, pk-0.5] |
| KAUS | (1.0, -0.5) | [pk-1.0, pk-0.5] |
| KBOS | (2.0, -1.0) | [pk-2.0, pk-1.0] |
| KDCA | (1.0, 1.0) | [pk-1.0, pk+1.0] |
| KDEN | (0.5, 0.5) | [pk-0.5, pk+0.5] |
| KDFW | (0.5, 0.0) | [pk-0.5, pk-0.0] |
| KHOU | (1.5, -0.5) | [pk-1.5, pk-0.5] |
| KLAS | — | no window (did not trade) |
| KLAX | (1.5, -0.5) | [pk-1.5, pk-0.5] |
| KMDW | — | no window (did not trade) |
| KMIA | (1.5, 0.0) | [pk-1.5, pk-0.0] |
| KMSP | — | no window (did not trade) |
| KMSY | — | no window (did not trade) |
| KNYC | (1.0, 1.0) | [pk-1.0, pk+1.0] |
| KOKC | (1.5, 1.0) | [pk-1.5, pk+1.0] |
| KPHL | (1.0, -0.5) | [pk-1.0, pk-0.5] |
| KPHX | (1.0, -0.5) | [pk-1.0, pk-0.5] |
| KSAT | — | no window (did not trade) |
| KSEA | (1.5, -0.5) | [pk-1.5, pk-0.5] |
| KSFO | (1.5, 0.0) | [pk-1.5, pk-0.0] |

### LOW (19 had windows, LAX none) — peak = daily MIN
| station | (before, after) | window |
|---|---|---|
| KATL | (1.5, 0.5) | [min-1.5, min+0.5] |
| KAUS | (4.0, 0.5) | [min-4.0, min+0.5] |
| KBOS | (3.5, 0.0) | [min-3.5, min-0.0] |
| KDCA | (4.0, 0.0) | [min-4.0, min-0.0] |
| KDEN | (2.0, 0.5) | [min-2.0, min+0.5] |
| KDFW | (3.0, 0.5) | [min-3.0, min+0.5] |
| KHOU | (3.0, 0.0) | [min-3.0, min-0.0] |
| KLAS | (1.5, 0.0) | [min-1.5, min-0.0] |
| KLAX | — | no window (did not trade) |
| KMDW | (3.5, -0.5) | [min-3.5, min-0.5] |
| KMIA | (3.0, 0.0) | [min-3.0, min-0.0] |
| KMSP | (2.5, -0.5) | [min-2.5, min-0.5] |
| KMSY | (3.5, 0.0) | [min-3.5, min-0.0] |
| KNYC | (4.0, 0.0) | [min-4.0, min-0.0] |
| KOKC | (3.5, 0.5) | [min-3.5, min+0.5] |
| KPHL | (3.0, 0.5) | [min-3.0, min+0.5] |
| KPHX | (1.0, 0.0) | [min-1.0, min-0.0] |
| KSAT | (4.0, 0.0) | [min-4.0, min-0.0] |
| KSEA | (3.5, 0.0) | [min-3.5, min-0.0] |
| KSFO | (2.0, 0.5) | [min-2.0, min+0.5] |

## Robustness — read before treating this as "the profitable setup"
Cross-year validation (train ≤2024 / test ≥2025, multi-year May, on the 6 deep
cells) shows **May 20 was a good *day*, not a robust edge**:
- The 93% pre-peak WR that day is well above the cross-validated average pre-peak
  (~53% WR, **negative** PnL/bet across all h2pk thresholds 0.5–2.0).
- Pooled across the deep cells, **every** filter combo (price floors, edge floors,
  σ-regime, side, window) is net-negative out-of-sample.
- The **only** robustly profitable configs found: **NYC and MIA, BUY_NO-only**
  (+2 to +5 ¢/bet on held-out years). The other cells are −EV out-of-sample.

So this setup is a faithful record of what ran on 5/20, but the 70% was partly
variance. The durable lessons: (1) **block near/at-peak (h2pk < 1)** — the one
robust timing lever; (2) **BUY_NO-only on the edge cells**. See
`BACKTEST_METHODOLOGY.md`.

## Where it runs now
The `v1max-high-bot` (`~/v1max_high_bot/`, real money on the v1 wallet) runs these
exact c9f10c4 HIGH windows + `h2pk ≥ 0.5` + the gates above, HIGH-only, $5/position
(the 5 no-window HIGH cells were since given placeholder `[pk-1.0, pk-0.5]` windows).
