"""paper_judge_bot config.

All tunables in one place. Edit and `systemctl restart paper-judge-bot` to
apply. Sensitive values (API keys, webhooks) come from .env, never this file.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

POSITIONS_PATH = DATA_DIR / "positions.json"
TRADES_PATH = DATA_DIR / "trades.jsonl"
DECISIONS_PATH = DATA_DIR / "decisions.jsonl"
SHADOW_TRADES_PATH = DATA_DIR / "shadow_trades.jsonl"
# 2026-05-18: shadow log of code-only decision path (decide_entry_code.py).
# Runs alongside the LLM on every dispatched candidate; the LLM still drives
# trades. After ~7d we A/B vs settled outcomes to decide LLM cutover.
SHADOW_CODE_DECISIONS_PATH = DATA_DIR / "shadow_code_decisions.jsonl"
KILL_SWITCH_PATH = ROOT / "KILL"

ENV_PATH = ROOT / ".env"

# Shared resources (live alongside the other 4 bots)
SHARED_CACHE_DIR = Path("/home/ubuntu/shared_cache")
OBS_DB_PATH = Path("/home/ubuntu/obs-pipeline/data/obs.sqlite")
BOT_DECISIONS_DB = Path("/home/ubuntu/shared_tools/data/bot_decisions.sqlite")


# ─────────────────────────────────────────────────────────────────────────────
# Mode flags (the most important block — read this first)
# ─────────────────────────────────────────────────────────────────────────────
#
# observer_only  → bot scans, judges, logs, posts Discord. NO orders.
# trader         → bot executes buys + sells per guardrails.
# killed         → bot does literally nothing (heartbeat only).
#
# Default is observer_only. To promote: edit this, then systemctl restart.
MODE: str = "trader"

# DRY_RUN is an additional safety on top of MODE. If True, the order
# placement code path runs through validation+sizing+guardrails but stops
# short of the actual Kalshi POST. Used for end-to-end smoke tests.
DRY_RUN: bool = False

ENABLE_BUYS: bool = True
ENABLE_SELLS: bool = False   # Bot may exit its OWN positions (opened_by=paper-judge).
                            # Origin-tag guard in run_exit_loop prevents touching
                            # V2-max / V2-min positions on the shared wallet.


# ─────────────────────────────────────────────────────────────────────────────
# Kalshi wallet + API
# ─────────────────────────────────────────────────────────────────────────────
KALSHI_API_BASE = "https://api.elections.kalshi.com"
KALSHI_TIMEOUT_SEC = 15.0

# Wallet selection. Three modes:
#   "v1"  → ~/.env KALSHI_KEY_ID + ~/kalshi_key.pem (paper_min_bot v1 + V1 max)
#   "v2"  → hardcoded _KALSHI_KEY_ID_V2 const + obs-pipeline-bot/kalshi_key_v2_account2.pem
#           (shared with obs-pipeline-bot V2 max and kalshi-min-bot-v2)
#   "own" → use KALSHI_KEY_ID + KALSHI_PEM_PATH from .env (dedicated key)
#
# CO-EXISTENCE: when using "v2", this bot SHARES the wallet with two other
# services. To avoid stepping on them, the entry loop calls
# /portfolio/positions every cycle and SKIPS any ticker that already has an
# open position from ANY bot. The exit loop only acts on positions in our
# own positions.json (we never sell another bot's position).
WALLET: str = "v2"

_KALSHI_KEY_ID_V2 = "7224fdb1-f5c9-4dc5-a1ce-b85013ad34d1"
_KALSHI_V2_PEM_PATH = Path("/home/ubuntu/obs-pipeline-bot/kalshi_key_v2_account2.pem")
_KALSHI_V1_PEM_PATH = Path("/home/ubuntu/kalshi_key.pem")

# Resolved at apply_env() — see _resolve_kalshi_auth() below.
KALSHI_KEY_ID: str = ""
KALSHI_PEM_PATH: Path = ROOT / "kalshi_key.pem"


# ─────────────────────────────────────────────────────────────────────────────
# Claude — pick backend
# ─────────────────────────────────────────────────────────────────────────────
#
# Two backends are supported:
#
#   "anthropic_sdk" — calls the Anthropic API directly via the `anthropic`
#     Python SDK. Requires ANTHROPIC_API_KEY in .env. Costs ~$0.012/decision
#     with prompt caching after the first call of a 5-min window. Lower
#     latency (~1s per call). Recommended for production.
#
#   "claude_cli" — subprocess-invokes the `claude` CLI in headless mode
#     (`claude -p --output-format json`). Uses your Claude Pro/Max
#     SUBSCRIPTION quota instead of API credits. NO API key needed; only
#     requires the CLI to be installed and authenticated on the host.
#     Tradeoffs:
#       - Higher latency (~2-3s per call)
#       - Subject to subscription rate limits (Pro ~30/5h, Max ~150/5h)
#       - No explicit prompt-cache control (CLI manages its own session)
#       - Competes with your interactive Claude Code use
#     Pick this if you want zero new API billing and your subscription is
#     Max-tier or higher.
JUDGE_BACKEND: str = "claude_cli"

# Anthropic API mode
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

# Always default to the latest Sonnet (cheap + fast + good).
# Use Opus only for paid-vetted Hard Calls — gate via CLAUDE_MODEL_HARD.
CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MODEL_HARD: Optional[str] = None  # e.g., "claude-opus-4-7" if Chris approves

CLAUDE_MAX_TOKENS_OUT = 1200
CLAUDE_TEMPERATURE = 0.2   # Low — consistent judgment, not creativity
# CLI mode runs the full reasoning chain. With the 11-step methodology
# block, complex cases (multi-bracket same-station, anomalous regimes
# like Chinook) can take 200-300s. 360s tail-covers those.
# 2026-05-16: bumped 600 → 800. Today's data shows median 238s, p90 354s,
# max non-timeout 542s. Two calls hit the 600s wall and returned parse_error
# (each one a free SKIP + a tick toward the 3-fail circuit breaker). 800s
# covers the p99+ tail without changing typical-case behavior.
# 2026-05-17 16:41 UTC: bumped 800 → 1200. After the A+B+D prompt refresh
# (added ## nn_match section, Step 7 regime A/B/C) median is now 245s and
# p90 ~470s — small overall shift, but KXHIGHPHIL-B88.5 timed out twice at
# 800s on a hard ambiguous packet (mu_chosen=87.7°F sitting inside YES
# window [87.5, 89.5), obs climbing +2.4°F/h, obs-anchored gate blocks
# BUY_NO, P(YES)≈0.38 below conviction floor → Claude burns time
# deliberating). 1200s covers the new p99+ tail and prevents the 3-fail
# circuit breaker tripping on legitimately hard packets.
CLAUDE_TIMEOUT_SEC = 1200.0

# Claude CLI mode — only used if JUDGE_BACKEND == "claude_cli"
CLAUDE_CLI_PATH: str = os.environ.get("CLAUDE_CLI_PATH", "claude")
# Inter-call sleep to be polite to the subscription rate limiter.
CLAUDE_CLI_INTERCALL_SLEEP_SEC: float = 0.5

# R2 2026-05-17: obs_anchor validation gate. When True, BUY decisions
# with missing/invalid obs_anchor get auto-downgraded to SKIP. When False,
# log-only shadow mode (the would-be skip is recorded but the trade
# proceeds). Backtest on 70 historical BUYs showed 92.9% pass an
# extractable-anchor check, suggesting low enforcement cost. Flip to
# False without restart by editing this file and SIGHUP'ing the process
# — actually no SIGHUP support yet, so restart is needed. Keep this
# togglable until we have ≥7d of post-ship data.
R2_ENFORCE_OBS_ANCHOR: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Universe (which markets to scan)
# ─────────────────────────────────────────────────────────────────────────────
# 20 cities the other bots already trade. Keep aligned so shared forecast cache
# covers everything we scan.
STATIONS: tuple[str, ...] = (
    "KATL", "KAUS", "KBOS", "KDCA", "KDEN", "KDFW", "KHOU", "KLAS",
    "KLAX", "KMDW", "KMIA", "KMSP", "KMSY", "KNYC", "KOKC", "KPHL",
    "KPHX", "KSAT", "KSEA", "KSFO",
)

# BOTH high-temp ("max") and low-temp ("min") markets are in the universe.
# KXHIGH* series → daily-high brackets (what V1/V2 max bots trade)
# KXLOWT*  series → daily-low brackets  (what V1/V2 min bots trade)
SERIES_PREFIXES: tuple[str, ...] = ("KXHIGH", "KXLOWT")

# Days_out window. 0=today, 1=tomorrow, 2=day-after.
# Day-0 only for now: the bot's distinctive edge is live-obs reasoning
# (current temp, dewpoint, wind, frontal pattern) — only relevant for the
# CURRENT climate day. Day-1+ is pure forecast play, which the numerical
# bots already do better. Add 1 back later if we want to widen.
DAYS_OUT_RANGE: tuple[int, ...] = (0,)


# ─────────────────────────────────────────────────────────────────────────────
# Loop cadence
# ─────────────────────────────────────────────────────────────────────────────
ENTRY_CYCLE_SEC = 900     # 25min — relaxed when no station is in HIGH or LOW obs window
PEAK_CYCLE_SEC = 900       # 10min — at least one station in a HIGH (5-18 local) or LOW (19-08 local) window
# 2026-05-14: TIGHT cadence removed — owner request "rarely action at climate-day close,
# leave intervals relaxed at end of day". Bot still holds open positions to settlement.
EXIT_CYCLE_SEC = 120
DISCORD_HEARTBEAT_SEC = 1800

# Parallel Claude calls. 6 is the experimentally-validated sweet spot:
# manual + python-subprocess tests at 6-parallel both complete in <100s.
# Earlier 360s-timeout failures cleared after killing zombie subprocesses
# from prior cycles. If we see new timeouts, kill any stray claude -p
# processes and restart — don't rebuild back to serial.
ENTRY_PARALLEL_WORKERS = 10
EXIT_PARALLEL_WORKERS = 10


# ─────────────────────────────────────────────────────────────────────────────
# Pre-screen thresholds (numerical filters before any LLM call)
# ─────────────────────────────────────────────────────────────────────────────
PRESCREEN = {
    "max_spread_cents": 10,                    # tightened — illiquid markets aren't actionable
    "min_price_cents": 5,
    "max_price_cents": 90,
    "min_time_to_close_sec": 90 * 60,          # 30 min
    "max_time_to_close_sec": 48 * 3600,        # 48 h cap — d+2 markets get filtered later
    "require_recent_obs_age_sec": 45 * 60,
    "require_recent_forecast_age_sec": 6 * 3600,
    # Edge floor — require at least this much numerical edge (vs Kalshi ask)
    # on one side before we burn an LLM call. Edge = max(P(YES) - yes_ask,
    # P(NO) - no_ask). Numerical mp is computed from median forecast μ and
    # σ (NBP σ if available, else default below).
    # 2026-05-15: lowered 0.08 → 0.06 with scout-and-sweep ship. Scout
    # walks book to ≥6pp per level (was 8pp); prescreen floor should
    # match so prescreen-passed candidates can actually reach the scout.
    "min_numerical_edge": 0.06,
    # RULE #2 ceiling (2026-05-16, extracted from prompt Step 10):
    # gap > this on the bot's chosen side → SKIP unless physically rm-locked
    # (see _is_rm_locked_for_side in paper_judge_bot.py). When market sees a
    # large gap with our model, market almost always knows something we don't
    # (microclimate, sea-breeze cap, persistent humidity damping) — the only
    # legit override is when the running extreme has already crossed the
    # bracket boundary.
    #
    # 2026-05-16 (PM): tightened 0.60 → 0.25 after market-vs-model analysis
    # on 192 settled snapshots since the bracket fix. In the 25-50pp gap
    # band (n=40), market was right 87.5% vs model 12.5%; below 25pp both
    # ran ~80%. The data is clear: when the bot disagrees with market by
    # more than 25pp, it should defer to market unless physically rm-locked.
    "max_numerical_edge_gap": 0.25,
    "default_sigma_f": 2.5,
    # Market-confidence floor — at least one ask side must be >= this.
    # If both yes_ask and no_ask are < this cents, the market is undecided
    # (coin-flip band). Bot's edge is disagreeing with a CONFIDENT market
    # via live obs; an undecided market gives no edge to exploit.
    #
    # 2026-05-16 (PM): re-enabled 0 → 60. Was disabled May 15 ("let Claude
    # handle undecided markets") but the May 15-16 settled-snapshot analysis
    # showed the bot's edge requires beating an informed market. When
    # neither side is confident, there's no signal to lean on.
    # 2026-05-16 (PM, later): Chris turned it back off — wants LLM to see
    # undecided markets again. Reverted 60 → 0.
    "min_market_confidence_cents": 0,
    "max_settled_threshold_cents": 90,
    # Re-dispatch cool-down: when the bot dispatches the same ticker to LLM
    # multiple times in a single cycle window, noisy single-snapshot signals
    # (e.g., 30-min trend) can flip conviction across the BUY threshold on
    # essentially identical state (PHX 2026-05-15 B99.5: 3 dispatches in
    # 40 min, conviction climbed 0.85→0.93 on trend reversal alone, BUY at
    # the high water mark). Skip same-ticker re-dispatches within the
    # cool-down window UNLESS price moved materially or rm crossed a
    # bracket boundary.
    "dispatch_cooldown_sec": 1800,             # 30 min
    "dispatch_cooldown_price_delta_c": 5,      # 5c price move breaks cool-down
    # 2026-05-17: skip candidates whose μ came from a PURE-FORECAST fallback
    # (best_mae_*, consensus_median, raw_median). The bot's stated edge is
    # observations, not forecasts. When neither nn_match (k-NN heating-curve)
    # nor rm-anchored methods (anchored/low_rm_ceiling) produce a μ, we have
    # no obs-anchored projection — better to SKIP than to take a forecast-only
    # bet. Backtest evidence (n=73 settled BUYs, 3-day window):
    #   - FORECAST_HEAVY reads bucket: 22 trades, 45% WR, −$83.79 net, −37% ROI
    #   - Balanced reads: 9 trades, 89% WR, +78.6% ROI
    #   - Directional accuracy on n=13 with logged mu_method: pure-forecast
    #     33% vs rm-anchored 67% vs nn_match (n=0 settled yet)
    # Expected volume impact: ~50-60% reduction (most current BUYs would have
    # come from best_mae_*/consensus_median paths). Set False to roll back.
    "skip_forecast_only_mu": True,
    # 2026-05-18: STRICTER successor to skip_forecast_only_mu. Where the
    # earlier flag only blocked pure-forecast fallbacks (best_mae_*,
    # consensus_median, raw_median), this one blocks EVERY mu_method that
    # isn't nn_match_*. The prompt's "nn_match-only" rule says the LLM
    # should SKIP non-nn_match — but live data showed the LLM was BUYing
    # `low_rm_ceiling` 100% of the time it slipped through to dispatch
    # (3/3 in 48h, rule violations), and citing banned forecast/pace_band
    # data in its reads. Adding anchored + low_rm_ceiling to the prescreen
    # eliminates the rule-violation path structurally. When True, this
    # supersedes skip_forecast_only_mu (any non-nn_match μ is dropped).
    # Backtest impact (48h): blocks 3 low_rm_ceiling BUYs (all
    # rule-violating). Total ~21 LLM-call savings vs prior flag alone.
    # Set False to fall back to skip_forecast_only_mu only.
    "skip_unless_nn_match": True,
    # 2026-05-19: μ-margin filter. Block trades where the bot's projection
    # sits inside (or too close to) the YES window. Defines confidence
    # required to bet a given direction:
    #   margin_outside_yes_f(μ, floor, cap, side) ≥ k_side · σ_chosen
    # where margin is signed distance from μ to the nearest YES boundary,
    # positive when μ supports the side direction.
    #
    # KXHIGHPHIL-26MAY18-B96.5 case (settled YES, BUY_NO −$3.85):
    #   μ=96.3, σ=2.10, YES=[95.5, 97.5], side=BUY_NO
    #   margin = -0.20  (μ INSIDE YES, ~0σ from edge)  ← would be filtered.
    #
    # Backtest n=28 settled BUYs (5/16-5/18, all pre-strip-forecasts):
    #   baseline: 53.6% WR, -$44.60 net, -14.3% ROI
    #   filter on (k_NO=1.5, k_YES=1.0, σ≤2.5): 6/6, +$16.88, +23% ROI
    #   skip rate: 71-79% (volume drops sharply but expected ROI flips positive)
    #
    # Asymmetric: BUY_NO needs more margin (1.5σ) than BUY_YES (1.0σ) because
    # BUY_YES naturally points at where μ projects, while BUY_NO bets against
    # the bot's own projection direction.
    "margin_filter_enabled": True,
    "margin_k_sigma_buy_no":  1.5,   # BUY_NO requires μ ≥ 1.5σ outside YES
    "margin_k_sigma_buy_yes": 1.0,   # BUY_YES requires μ ≥ 1.0σ outside YES
    "margin_max_sigma_f":     2.5,   # extra gate: skip if σ > this (too wide)
    # rm-lock OVERRIDES this filter (lock means physical settlement, math
    # uncertainty no longer matters).
    "margin_filter_bypass_when_rm_locked": True,
}


# ─────────────────────────────────────────────────────────────────────────────
# Obs-relevance windows (per-city, local clock)
# ─────────────────────────────────────────────────────────────────────────────
#
# The bot's only edge is live obs informing the realized extreme. Outside
# the windows below the obs adds nothing the numerical bots don't have, so
# we skip the LLM call entirely.
#
# Each station's peak_hour_local / min_hour_local is computed per-day from
# real solar math (solar_noon + climate-class lag for peak; sunrise for min)
# in climate_normals.local_clock_context — so these constants describe the
# *width* of the window around each station's own peak/min, not absolute
# clock hours. That makes them seasonally + per-city correct automatically:
# KSEA's June peak (~14:30 local) and KPHX's December peak (~15:30 local)
# both center the same ±window without per-station overrides.
#
# Window choices come from empirical BUY distribution in decisions.jsonl
# (n=1100 historical entries): HIGH BUYs cluster delta=−3 to +1 hours
# around peak (≥80% of BUYs); LOW BUYs split into pre-dawn (delta=−5 to
# −1, d+0 case) and late-evening (22:00–24:00 local, both d+0 cold-front
# evening lows and d+1 evening previews of tomorrow's overnight low).
#
OBS_WINDOWS = {
    # HIGH d+0: peak − 3h to peak + 1h (asymmetric, slightly pre-peak weighted)
    "high_d0_pre_peak_h": 3.0,
    "high_d0_post_peak_h": 1.0,

    # LOW d+0 pre-dawn: min − 5h to min − 1h (4h pre-dawn window)
    # post-min is excluded — the data shows zero post-min BUYs for d+0
    # because the dawn LOW is locked by then.
    "low_d0_pre_min_start_h": 5.0,  # window opens this many hours BEFORE min
    "low_d0_pre_min_end_h": 1.0,    # window closes this many hours BEFORE min

    # LOW late-evening window (local clock, NOT relative to min):
    #   d+0: covers the "min happens before midnight" case — sometimes a
    #        cold-front passage drops temps below the dawn min in the
    #        last 2h of the climate day.
    #   d+1: covers the evening-preview window where the bot evaluates
    #        tomorrow's overnight LOW from this evening's setup.
    "low_late_evening_lo_local": 22.0,
    "low_late_evening_hi_local": 24.0,  # 24 = midnight exclusive

    # d+1 HIGH: hard-skip. Today's obs has no signal for tomorrow's peak.
    "high_d1_allowed": False,

    # d+2 or further: hard-skip regardless of series.
    "max_days_out": 1,
}


# ─────────────────────────────────────────────────────────────────────────────
# Guardrails — HARD CAPS — LLM CANNOT OVERRIDE
# ─────────────────────────────────────────────────────────────────────────────
GUARDRAILS = {
    # Sizing — 2026-05-15 update: side-aware caps. BUY_NO at $30 (typical
    # BUY_NO entries pay 50-90c for ~10-50c upside — capping at $30 = ~33
    # contracts at 90c worst case); BUY_YES at $10 (asymmetric loss profile:
    # cheap YES with low payoff hits cost-full when wrong, V1/V2 numerical
    # bots have learned to size YES smaller for the same reason).
    "max_bet_no_usd": 30.0,
    # 2026-05-20: raised 10 -> 15 so HIGH-series BUY_YES can reach the new $15
    # HIGH cap (guardrails REJECTS, not truncates, when cost > side_cap, so a
    # $15-sized HIGH YES would otherwise be killed by the old $10 YES cap).
    # LOW BUY_YES is unaffected — it stays capped at $5 by max_bet_low_series_usd.
    "max_bet_yes_usd": 15.0,
    # 2026-05-16: HIGH-series brackets (KXHIGH-*) capped tighter after a string
    # of forecast-anchored BUY_NO losses (HOU B88.5, MIN B80.5, NY B78.5, LV B95.5).
    # Applied as min(side_cap, high_series_cap) when ticker starts with "KXHIGH".
    # 2026-05-20: raised 5 -> 15 (Chris directive). HIGH is the profitable book
    # (+$40 on 5/20 vs LOW -$24); lean bet size into it. pure_nn_decide sizing
    # reads this same value via the worker so qty is sized to match the cap.
    "max_bet_high_series_usd": 15.0,
    # 2026-05-16 (evening): LOW-series brackets (KXLOW-*) capped at $5 alongside
    # HIGH while validating the nn_match k-NN heating-curve projector as the
    # primary μ source. Symmetric to max_bet_high_series_usd; applied at
    # fresh-buy sizing AND check_buy validation.
    # 2026-05-21: LOW cut $5 -> $1 (Chris directive). LOW is the losing book
    # (5/20: LOW -$24.23 vs HIGH +$40.29) — shrink exposure to a token size
    # while the nn LOW projector keeps misfiring. The push worker passes a
    # smaller LOW min-buy (PUSH_MIN_BUY_USD_LOW) into pure_nn_decide so the
    # integer-contract math doesn't collapse the way $5/$5 did on 2026-05-17
    # (min_buy == series_cap => no integer qty fits => all LOW buys skip).
    "max_bet_low_series_usd": 1.0,
    # Legacy single-cap field — kept for any reader unaware of side-specific
    # caps. Set to the higher of the two so generic checks don't false-positive.
    "max_bet_usd": 30.0,
    # 2026-05-15: 20.0 → 30.0 — set to match max_bet_no_usd so a single
    # full-size BUY_NO at the side cap doesn't get rejected by this gate.
    # Previously a $21 BUY_NO sweep would fail with "ticker total $21 >
    # max_ticker_total_usd $20" even though it's the FIRST entry on the
    # ticker. $30 = side cap for NO, room for one full-size entry plus
    # any top-up. Side caps (BUY_NO $30, BUY_YES $10) remain the primary
    # per-bet limit.
    "max_ticker_total_usd": 30.0,
    "daily_spend_cap_usd": 300.0,
    "max_open_positions": 999,  # effectively unlimited per user request
    "max_daily_buys": 9999,
    "max_daily_sells": 50,
    # Quality + correlation gates (post-Claude, applied at execute time):
    # 2026-05-15: bumped 0.78 → 0.83 after May 14 review.
    # n=19 settled trades showed conv[0.80, 0.83) at 43% WR / -$12.68 net,
    # vs conv≥0.83 at 82% WR / +$24.17 net. The 0.80-0.83 band is a coin flip
    # with structural -EV; raising the floor here saves the expected loss.
    "min_conviction_for_buy": 0.83,        # cream of the crop only (Tier A)
    "min_size_factor_for_buy": 0.50,       # Claude's own sizing must also signal confidence
    "max_buys_per_cycle": 9999,               # cap total cycle exposure
    "max_buys_per_station_side": 1,        # correlation guard:
                                            # ≤ 2 BUYs per (city, HIGH|LOW) so
                                            # Denver can have e.g. B83.5 + B85.5
                                            # both NO without being 5-bracket
                                            # all-or-nothing correlated.
    # Price
    "min_price_cents": 5,
    # Insufficient-funds skip: if wallet balance is below this, the entry
    # cycle is fully skipped (no prefetch, no LLM, no candidate scan).
    # Default $1 = cannot afford 1 contract at max_price_cents=90.
    "min_cycle_balance_usd": 5.0,
    # Minimum dollar-cost per buy. Prevents tiny positions (e.g. 1 contract
    # at 17c) from locking out a (city, side) slot for the whole day under
    # the correlation cap. Buys clamped below this are skipped.
    # 2026-05-17 09:20 UTC: lowered $5 → $1 because the integer-contract math
    # collapses when min_buy_usd == max_bet_*_series_usd ($5/$5). At typical
    # BUY_NO ask 72-99c, no integer qty satisfies both floor ≥ $5 and cap ≤
    # $5. Diagnosed by 12 high-conviction Claude BUYs in 2.5h post-restart,
    # 0 executed (all skipped "reachable $X.XX < floor $5.00").
    "min_buy_usd": 1.0,
    # Hourly Discord summary cadence (replaces older 30-min heartbeat).
    "hourly_summary_sec": 3600,
    "max_price_cents": 90,
    # Time
    "no_new_buys_within_sec_of_close": 30 * 60,
    "no_sells_before_close_unless_triggered_sec": 6 * 3600,
    "rebuy_cooldown_sec_after_sell": 30 * 60,
    # Circuit breakers
    "daily_loss_kill_usd": -100.0,
    "consecutive_llm_failure_threshold": 3,
    "llm_failure_pause_sec": 5 * 60,
    # 2026-05-17 21:xx UTC: disabled per Chris ("turn this off") after
    # KXHIGHTPHX-26MAY17-B93.5 was blocked at $6.63 vs $5 cap. To re-enable,
    # set a finite USD value.
    "max_daily_api_spend_usd": float("inf"),
}


# ─────────────────────────────────────────────────────────────────────────────
# F-OBS-ANCHORED-PRE prescreen toggle
# ─────────────────────────────────────────────────────────────────────────────
# 2026-05-17 21:xx UTC: disabled per Chris ("turn off obs anchored pre reject")
# after entry-prompt nn_match-only purge + skip_forecast_only_mu landed earlier
# today made this prescreen redundant/over-restrictive. The Step 7.5 LLM-side
# language stays in the prompt. To re-enable, flip back to True.
OBS_ANCHORED_PRE_ENABLED: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Event-driven pure-nn shadow worker (2026-05-18)
# ─────────────────────────────────────────────────────────────────────────────
# When True, the bot starts a background worker that listens on:
#   - kalshi_ws BBO callbacks (real push)
#   - wethr_cache.json mtime changes (5s filewatch)
# For each event, the worker computes the pure-nn decision (no LLM) and
# logs to data/shadow_nn_strategy.jsonl. NO ORDERS are placed; this is
# purely an A/B-style log to compare LLM-augmented vs pure-nn PnL once
# settlement data is backfilled. Toggle off to disable the worker
# entirely (callback unregisters, thread exits).
SHADOW_NN_EVENT_DRIVEN: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# 2026-05-19: Push-based pure-code auto-execute architecture
# ─────────────────────────────────────────────────────────────────────────────
# Pure-nn decisions auto-executed via WS/socket push when:
#   1. AUTO_EXECUTE_BUY_<NO|YES>_PUSH = True for the direction
#   2. local_hour is within (peak/min ± offsets) for that station's empirical
#      peak/min hour for the CURRENT MONTH (from pace_curves_*_v2.json)
#   3. entry_price (cents) is in [PUSH_MIN_ENTRY_C, PUSH_MAX_ENTRY_C]
#   4. position count for (station, series, direction) is below PUSH_MAX_TICKERS
#
# Direction toggles ON — pure-code architecture per Chris 2026-05-19.
AUTO_EXECUTE_BUY_NO_PUSH: bool = True
AUTO_EXECUTE_BUY_YES_PUSH: bool = True

# Peak-relative window offsets (in hours). Window is open when:
#   peak_hour - PUSH_PEAK_HOURS_BEFORE  <=  local_hour  <=  peak_hour + (HIGH|LOW after)
PUSH_PEAK_HOURS_BEFORE: float = 1.0
# 2026-05-19 push v2: asymmetric post-peak. LOW markets close shortly after the
# daily LOW resolves, so post-peak BUYs hit the 30-min-to-close guardrail.
# HIGH markets generally have more time. Per Chris: LOW after = 0.5h.
PUSH_PEAK_HOURS_AFTER_HIGH: float = 0.5
PUSH_PEAK_HOURS_AFTER_LOW: float = 0.5
PUSH_PEAK_HOURS_AFTER: float = 0.5  # deprecated, kept for compat reads

# Per-(station, series, month) override of the push decision window.
# When True, nn_shadow_worker._in_decision_window consults
# push_window_overrides.PUSH_WINDOW_OVERRIDES first; cells absent from the
# dict fall back to PUSH_PEAK_HOURS_BEFORE / AFTER_<HIGH|LOW>. Map generated
# from /home/ubuntu/data/phq_combined.csv (800-day backtest, 2026-05-19).
USE_PUSH_WINDOW_OVERRIDES: bool = True

# Fractional peak source for nn_shadow_worker._lookup_peak_hour.
# When True, the decision-window check uses 5-year 10-day-rolling P50 of
# day_max_lst_min / day_min_lst_min from heating_traces.sqlite (per K-station,
# side, month-day). When False, falls back to int(empirical_peak_hour_local)
# from pace_curves_v2.json (legacy behavior).
# Validated 2026-05-20 on n=99,812 station-days: window hit-rate
# 29.8% (int) → 38.0% (5yr-10day frac) HIGH; 29.3% → 36.9% LOW. May 19
# replay PnL +$15.93 (frac) vs +$6.91 (int) under same overrides.
# Source data: /home/ubuntu/data/peak_fractional_5yr_10day.json (14,610 cells).
USE_FRACTIONAL_PEAK_FOR_WINDOW: bool = True
PUSH_PEAK_FRACTIONAL_PATH: str = "/home/ubuntu/data/peak_fractional_5yr_10day.json"

# 2026-05-21: per-cell MEDIAN bias correction (HIGH only) + MAE-based confidence
# sizing. Both consume push_window_overrides 4-tuples (before, after, bias, mae)
# and apply in nn_shadow_worker._evaluate_ticker. Out-of-sample validated
# 2026-05-21 (train→2024-25 holdout, 79,248 decisions): median-bias −2.1% HIGH
# MAE (159/235 cells; LOW neutral −0.1% → excluded; the MEAN bias was −8.6%
# WORSE due to skewed errors, so the override file ships MEDIAN). Cell MAE
# predicts holdout accuracy (corr 0.62, monotonic tiers) → scale bet down where
# the matcher is less reliable (only ever reduces size = risk-reducing).
USE_PUSH_BIAS_CORRECTION: bool = False  # REVERTED 2026-05-21: even the MEDIAN
    # bias would have flipped 2 MSP winners→losses on 5/20 (Kalshi-settled
    # 16-6 → 14-8) — MSP's −0.8 bias over-corrected on a cold day, pushing μ
    # across the bracket boundary. Marginal +2.1% avg HIGH MAE not worth the
    # boundary-flip risk. Bias still LOGGED (push_override.bias) for analysis,
    # just not applied to μ. Flip True to re-enable.
USE_PUSH_MAE_SIZING: bool = True        # KEPT — validated (corr 0.62); only
    # scales bet size, never flips a bet (no boundary risk).
# (mae_lo, mae_hi, size_multiplier). mae=None → 0.5 (unknown/fallback).
PUSH_MAE_CONF_TIERS: list = [
    (0.0, 1.0, 1.0),     # <=1F MAE: full size (settles ~1.3F)
    (1.0, 1.5, 0.75),
    (1.5, 2.5, 0.5),
    (2.5, 99.0, 0.3),    # >=2.5F MAE: minimal (settles ~3F)
]

# Minimum edge_pp (percentage-points of P(direction) − market_implied) for
# nn_shadow_strategy.pure_nn_decide to fire. Default in the function is 6pp;
# we raise to 12pp based on 2026-05-20 backtest on n=196 trades (166 settled
# + 30 today as proxy via current bid). Edge floor sweep was monotonic on
# pure-nn cohort: 6→12pp lifts ROI from −0.0% → +0.8%; 6→15pp to +3.3%;
# 6→20pp to +6.0%. 12pp chosen as conservative move that preserves 70% of
# volume while filtering bottom-edge marginal trades.
PUSH_MIN_EDGE_PP: int = 12

# Minimum hours-until-peak for HIGH-series entries (defense-in-depth on top
# of the window override). At peak, rm has converged on the day's true max,
# so the nn_match mu projection over-extrapolates and flips adjacent
# brackets the wrong way. 2026-05-20 backtest n=47 HIGH push trades:
# h_to_peak<0.5 catches 3 losers (PHIL B95.5 BUY_NO, NOLA B86.5 BUY_NO,
# NOLA B88.5 BUY_YES) for -$13.07; 0 winners caught (nearest winner at
# h_to_peak=+0.70). Mechanism: at peak, rm ~= final max with stable
# obs_trend_30m; mu still projects upward = systematic over-projection.
# Set to None or 0.0 to disable.
PUSH_MIN_H_TO_PEAK_HIGH: float = 0.5

# In-bracket tail-bet gate (Gate 2). When the nn mu sits INSIDE the YES window
# [floor-0.5, cap+0.5) but the bot picks the smaller-mass (tail) side
# (p_chosen < 0.5), it is betting against its own central estimate for a thin
# edge -- a structure with no winning weather regime. Raise the edge floor to
# this value for those trades only. Backtest: 5/19+5/20 settled pure-nn pool,
# 4 blocks, 4 losers, 0 winners killed, +$13.87 net. Mechanism-clean. Sibling
# Gate 1 (boundary-gap) was PARKED -- it killed real winners (DAL/SFO/DEN).
# Set to 0 to disable (reverts to PUSH_MIN_EDGE_PP for these trades).
PUSH_TAIL_BET_MIN_EDGE_PP: int = 25

# LOW-series per-bet min-buy floor for the push sizer. 2026-05-21: when LOW was
# cut to a $1 cap (max_bet_low_series_usd), the default $1 min-buy equals the
# cap and the integer-contract math collapses (no qty satisfies both cost >= $1
# floor AND cost <= $1 cap except exact-divisor prices), which would silently
# skip nearly all LOW buys. This lower floor lets LOW place genuine ~$0.40-$1.00
# bets. HIGH keeps the standard $1 min-buy (its $15 cap never binds on min-buy).
PUSH_MIN_BUY_USD_LOW: float = 0.40

# Entry-price guardrails (cents). Skip if the ask we'd pay is outside [floor, ceil].
# Floor protects against long-shot bets; ceiling protects against settled markets.
# 2026-05-19 v3: BUY_YES needs a higher floor than BUY_NO. Analysis of 170 shadow
# decisions showed 0/12 settled wins on BUY_YES at <15c entry (n=52 cohort,
# MTM −$0.13/$). Cheap YES = market consensus near-zero; nn overconfident on tails.
PUSH_MIN_ENTRY_C: int = 10           # BUY_NO floor (unchanged)
PUSH_MIN_ENTRY_C_BUY_YES: int = 30   # BUY_YES needs >= 30c (raised from 25 per 2026-05-20 sweep — filters cheap-YES lottery)
PUSH_MAX_ENTRY_C: int = 80

# Tier 1 runtime gates — physics-catastrophic conditions where the nn matcher
# (trained on normal-weather days) literally cannot work. Conservative
# thresholds catch only the extreme tail. No backtest required because the
# mechanism is obvious (fog kills the diurnal cycle, extreme wind = tropical /
# severe regime — both are <1% of station-days and reliably reported by the
# wethr feed). Visibility also serves as a precipitation proxy: heavy rain/snow
# almost always drops vsby below 1 mi. A real precip-rate gate is a follow-up
# (wethr cache doesn't currently emit precip_in_h). Set to 999 / -1 to disable.
PUSH_MIN_VSBY_MI: float = 0.5              # visibility < 0.5 mi (dense fog / heavy precip) → skip
PUSH_MAX_WIND_MPH: float = 40.0            # sustained wind or gust > 40 mph (~35 kt) → skip

# 2026-05-20: F1 rm-staleness validation for push pure-nn worker.
# Bug discovered today: nn_shadow_worker bypassed wethr_rm.validate_rm_for_climate_day,
# leaving the push path vulnerable to using yesterday-evening rm readings as todays
# anchor. KAUS LOW 5/20 entered BUY_NO at B67.5 rm-locked on a 66°F reading from
# 5/19 21:08 CDT (still 5/19 LST climate day); actual 5/20 min was 68°F → loss.
# Kalshi confirms LST climate-day boundary via market close_time field (verified by
# API: KXLOWTAUS-26MAY20 close_time=2026-05-21T06:00Z = LST midnight ending 5/20).
# Set to False to instantly revert to legacy behavior (uses any rm whatever its date).
PUSH_VALIDATE_RM_CLIMATE_DAY: bool = True
# Grace period (seconds) after LST midnight before treating rm as predictive. LOW gets
# 15min (rapid pre-dawn cooling already informative); HIGH gets 60min (longer warmup).
# Matches paper_judge_bot.py:449,757 existing pattern.
PUSH_RM_GRACE_SEC_LOW: float = 900.0
PUSH_RM_GRACE_SEC_HIGH: float = 3600.0

# Max positions per (station, series, direction). 1 → at most one BUY_YES and
# one BUY_NO ticker active per station per series at any time.
PUSH_MAX_TICKERS_PER_STATION_SIDE_DIRECTION: int = 1

# Pace-curve source files for empirical peak/min lookup
PUSH_PACE_CURVES_HIGH_PATH: str = "/home/ubuntu/data/pace_curves_v2.json"
PUSH_PACE_CURVES_LOW_PATH: str = "/home/ubuntu/data/pace_curves_low_v2.json"

# LLM dispatch mode (paper_judge_bot 15-min cycle):
#   "all"      — dispatch every survivor (legacy behavior)
#   "yes_only" — only dispatch BUY_YES candidates (push handles BUY_NO)
#   "off"      — disable LLM dispatch entirely (pure-code push-only)
LLM_DISPATCH_MODE: str = "off"

# NOTE: NN_LOW_GATE_UNLOCKED_POSTNOON stays True per its original setting
# (post-noon unlocked LOW projections are unreliable per 2024-25 backtest).
# Push windows for LOW are naturally bounded to morning + locked cohort by
# the peak-relative window above.


# ─────────────────────────────────────────────────────────────────────────────
# Discord
# ─────────────────────────────────────────────────────────────────────────────
# Two delivery modes (auto-selected — webhook preferred if both set):
#   1. DISCORD_WEBHOOK_URL — easiest. Create webhook in the target channel
#      via channel Settings → Integrations → Webhooks. No bot needed.
#   2. DISCORD_BOT_TOKEN + DISCORD_CHANNEL_ID — uses the Discord REST API
#      with a bot user. Required for cross-server/multi-channel reuse.
DISCORD_WEBHOOK_URL: str = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_BOT_TOKEN: str = os.environ.get("DISCORD_BOT_TOKEN", "")
DISCORD_CHANNEL_ID: str = os.environ.get("DISCORD_CHANNEL_ID", "")
DISCORD_PING_USER_ID: str = os.environ.get("DISCORD_PING_USER_ID", "")  # optional @
DISCORD_NOTIFY_ON_SKIP_CONVICTION = 0.6


# ─────────────────────────────────────────────────────────────────────────────
# nn_match calibration (2026-05-17 P1+P2 ship — see project-judge-nn-audit-20260517)
# ─────────────────────────────────────────────────────────────────────────────
# Bias correction (P1 #3) — revised 2026-05-17 09:55 UTC after fresh backtest.
# Earlier shipped constants (HIGH +0.99 / LOW −1.82) were calibrated on a
# narrower eval-hour cohort. Fresh n=5937 HIGH / 7623 LOW backtest across the
# bot's actual trading hours (HIGH 6/9/12/15, LOW 2/4/18/20) showed:
#   - constant LOW −1.82 corr REGRESSES MAE +40% vs raw nn_match (skewed
#     errors push undershoot tail dramatically more negative)
#   - constant HIGH +0.99 over-corrects afternoon (raw afternoon bias is
#     NEGATIVE so afternoon mu drifts further from actual)
# Replaced with hour-aware HIGH bias (zero LOW). Backtest combined MAE:
#   HIGH: 1.946 raw → 1.769 old current → **1.659 modified (−14.7% vs raw)**
#   LOW : 1.556 raw → 2.179 old current → **1.472 modified (−5.4% vs raw)**
NN_BIAS_CORR_HIGH_F: float = 0.0     # legacy constant (always-applied, both halves)
NN_BIAS_CORR_LOW_F: float = 0.11     # 2026-05-18 (Action C partial): refit for wins10 LOW aggregator (was 0.0 for median); near-zero residual on unlocked AM; locked LOW uses traj_min so unaffected
# 2026-05-18 (Action C partial): bias refit for new aggregators (idw3 HIGH).
# Pre-2026-05-18 median values were -0.3 morn / +0.3 aft. idw3's natural
# bias is much smaller (~+0.05), so per-half corrections are near-zero.
# Cross-year stable (drift <0.18°F between 2024-25 and 2023 fits).
NN_BIAS_CORR_HIGH_MORNING_F: float = 0.05    # applied when cur LST hour < cutoff (was -0.3 for median)
NN_BIAS_CORR_HIGH_AFTERNOON_F: float = 0.04  # applied when cur LST hour >= cutoff (was +0.3 for median)
NN_BIAS_HIGH_CUTOFF_HOUR: int = 11

# 2026-05-18 (Action C partial): aggregator switch flag. True (default) uses
# the per-side aggregators in nn_match_fast.predict():
#   HIGH: idw3 (inverse-cube-distance weighted mean of top-k) — hold-out CRPS -1.7%
#   LOW : wins10_k20 (winsorized 10/90 mean of top-20 closest) — hold-out CRPS -5.6%
# Cross-year stable on n=5243 HIGH / n=3411 LOW; zero per-station regression >5%
# (k020_wins10 max +4.1%, k050_idw3 max +2.9%). Per-station map tested + rejected
# (overfits sweep noise; see prior project_nn_per_station_k_negative finding).
# Real P&L replay 2026-05-14 settled trades: counterfactual +$24.95 vs production
# median (13 unique trade events). Set to False to revert behavior to median-of-k
# without redeploying; if False, the bias/sigma constants above behave as legacy.
NN_USE_NEW_AGGREGATORS: bool = True

# Fit-quality gate (P2 #6). When neighbor delta stdev > thresh, predict()
# returns None and bot's fallback chain (anchored / rm_ceiling / consensus_corr)
# handles. Backtest n=5937 HIGH / 7623 LOW:
#   HIGH gate=3.5: fires ~83%, kept_MAE 1.60, Pearson r(σ, |err|)=+0.40
#   LOW  gate=4.0: fires ~80%, kept_MAE 1.45, Pearson r(σ, |err|)=+0.02
# Locked-mode bypasses gating (observed extreme has no neighbor uncertainty).
NN_FIT_QUALITY_THRESH_HIGH: float = 3.0    # 2026-05-18 tightened from 3.5; cross-year backtest n=1767/1785 -7.7%/-9.4% kept_MAE
NN_FIT_QUALITY_THRESH_LOW: float = 3.0     # 2026-05-18 tightened from 4.0; cross-year backtest n=1730/1792 -5.0%/-12.9% kept_MAE
NN_LOW_GATE_UNLOCKED_POSTNOON: bool = True   # 2026-05-18 skip LOW post-noon evals when not extreme_locked (cooling-event projections unreliable); 2024-25+2023 backtest -25%/-25.6% MAE on post-noon LOW

# 2026-05-19 (B-Gate-21): floor on the LOW locked branch in nn_match_fast.predict().
# The old rule fired the lock whenever (morning_min < afternoon_max) AND cur >= noon
# AND >1h since trough — but at hour=18 the morning trough is 11+ hours stale and
# evening cooling routinely drives actual day_min BELOW traj_min (especially Nov-Feb
# on continental stations). Pooled n=269 cross-year: residual bias +1.93°F at hr18,
# +1.28°F at hr20, ~0°F at hr22. 51% of hr18 locked cases had actual >= 3°F below
# the lock; p99 was −16°F below. Hold-out 2023 LOW CRPS −2.3% by gating locked
# when cur < 21*60 (let bot fallback chain anchored/rm_ceiling/consensus take over).
# Set to 12*60 to revert to old behavior (locks any time post-noon).
NN_LOCK_FLOOR_LST_MIN: int = 21 * 60   # 9 PM LST

# ─────────────────────────────────────────────────────────────────────────────
# 2026-05-19: HIGH-side two-tier peak clamp. Caps mu_proj when the day's peak
# has been reached or is imminent, gated by past per-(station, month) P50
# historical peak time (computed from heating_traces.sqlite at module init).
#
# Tier 1 (post-peak, tight cap):
#   cur_lst_min >= P50_peak_time[station, month]
#   AND traj_max occurred >= 30 min ago
#   AND max temp in last 30 min < traj_max - 0.5°F  (drop confirmed)
#   → cap mu_proj at traj_max + NN_HIGH_POST_PEAK_MARGIN_F
#
# Tier 2 (at-peak, loose cap):
#   cur_lst_min >= P50_peak_time[station, month]
#   AND cur_tmpf >= traj_max - NN_HIGH_AT_PEAK_TEMP_BAND_F  (temp at/near peak)
#   → cap mu_proj at traj_max + NN_HIGH_AT_PEAK_MARGIN_F
#
# When both tiers fire (post-peak AND temp still near max), lowest cap wins
# (tier 1 applies). Floor mu_proj >= traj_max always preserved.
#
# Cross-year backtest 2024-25 + 2023 hold-out (n=23k eval rows / 20 stations):
#   overall MAE     -14.5% / -12.7%
#   at_peak ±30     -25.0% / -26.3%  (the user-flagged "buy at peak" failure mode)
#   post_peak >90   -33.4% / -29.5%  (the "killer-window" failure mode)
#   pre_peak >60m    +2.5% /  +3.3%  (acceptable damage)
# Per-station: all 20 stations have positive overall lift in BOTH years.
#
# Full margin grid swept: t1 ∈ {0.5, 0.75, 1.0}, t2 ∈ {0.5, 0.75, 1.0, 1.5, 2.0},
# at_peak_band ∈ {0.5, 1.0}. Cross-year winner: t1=0.75, t2=1.0, band=1.0.
# (Final 0.75 vs 1.0 on Tier 1 within 0.01°F overall lift but consistently better
# on at_peak; 1.0 vs 1.5 on Tier 2 within 0.02°F but consistently better.)
#
# Rollback: NN_HIGH_PEAK_CLAMP_ENABLED=False disables both tiers (mu_proj stays
# at the existing max(mu_raw + bias, traj_max) floor — current production).
NN_HIGH_PEAK_CLAMP_ENABLED: bool = True
NN_HIGH_POST_PEAK_MARGIN_F: float = 0.75   # tier 1 cap = traj_max + this
NN_HIGH_AT_PEAK_MARGIN_F: float = 1.0      # tier 2 cap = traj_max + this
NN_HIGH_AT_PEAK_TEMP_BAND_F: float = 1.0   # tier 2 fires when cur_tmpf >= traj_max - this

# ─────────────────────────────────────────────────────────────────────────────
# 2026-05-18: nn_match k + lookback window tuning (HIGH side only).
#
# Deep-dive sweep on 2024-2025 random sample (n=1010-1133 × 3 eval hours):
#   prod baseline (k=50, sunrise-anchored):  MAE 1.836, p95 4.31
#   k=150, lookback=180min:                  MAE 1.701, p95 3.99  (−7.3% MAE)
# Cross-year validation on 2023 hold-out (n=932, seed=43 different from sweep):
#   prod baseline (k=50 + old +0.99 bias):   MAE 1.670, bias +0.682
#   k=150, lookback=180, bias=+0.331 refit:  MAE 1.516, bias −0.025 (−9.2%)
# Note: actual production bias at afternoon HIGH evals is +0.3 (=
# NN_BIAS_CORR_HIGH_F + NN_BIAS_CORR_HIGH_AFTERNOON_F = 0 + 0.3), which is
# ≈ the refit's +0.331 — so the bias_correction itself is NOT changed
# in this ship.  Time-decay (τ=12/24) was flat to worse; variable additions
# (pres1/gust/cloud) tested at w=0.30 in prior work were marginal — may
# re-test on top of the new k/window in follow-up.
NN_K_HIGH: int = 50                       # rolled back 2026-05-18 from k=150; per-station equal-weighted analysis showed k=150 regressed 10/20 stations (MIA +17%, AUS +10%, NYC +7%, DFW +6%); lookback=180min change retained
NN_K_LOW: int = 50                       # unchanged — LOW lookback gain
                                         # was -1.7% on n=1163, not material
NN_LOOKBACK_HIGH_MIN: int = 180          # truncate trajectory to last 180min
NN_LOOKBACK_LOW_MIN: int = 0             # 0 = full climate-day trajectory (current)

# 2026-05-18: pres1 trajectory matching weight (LOW only).
# Held-out backtest seed=1 n=11k on TODAY's prod baseline (k=50, relh w=0.30,
# bias=0): LOW MAE 1.929 → 1.889 at w=5.0 (−0.040°F, −2.1%). Per-hour Δ
# stable across hours 2-7 in same direction (−0.02 to −0.11). HIGH gets
# w=0 (Exp3 showed pres traj added 0 to HIGH).
# Live pres trajectory built from pres_history.jsonl snapshots (altimeter
# every cycle, converted altimeter→station_pres via station elevation_ft).
# Bot needs ~3h post-restart to accumulate enough snapshots before the
# matching has full effect (≥6 paired bins gate in predict()).
NN_PRES_TRAJ_WEIGHT_LOW: float = 5.0
NN_PRES_TRAJ_WEIGHT_HIGH: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("PAPER_JUDGE_BOT_LOG_LEVEL", "INFO")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


# ─────────────────────────────────────────────────────────────────────────────
# .env loader (minimal, no third-party dep)
# ─────────────────────────────────────────────────────────────────────────────
def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Lines starting with # are
    comments. Returns dict, does NOT mutate os.environ (caller decides)."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Z0-9_]+)\s*=\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip().strip('"').strip("'")
            out[key] = val
    return out


def _resolve_kalshi_auth() -> None:
    """Bind KALSHI_KEY_ID and KALSHI_PEM_PATH based on WALLET. Called by
    apply_env(). After this runs, kalshi_client._load_private_key works."""
    global KALSHI_KEY_ID, KALSHI_PEM_PATH
    if WALLET == "v2":
        KALSHI_KEY_ID = _KALSHI_KEY_ID_V2
        KALSHI_PEM_PATH = _KALSHI_V2_PEM_PATH
    elif WALLET == "v1":
        KALSHI_KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
        KALSHI_PEM_PATH = _KALSHI_V1_PEM_PATH
    else:  # "own"
        KALSHI_KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
        KALSHI_PEM_PATH = Path(
            os.environ.get("KALSHI_PEM_PATH", str(ROOT / "kalshi_key.pem"))
        )


def apply_env() -> None:
    """Load .env and overlay onto os.environ + module globals. Idempotent."""
    env = load_env()
    for k, v in env.items():
        os.environ.setdefault(k, v)
    # Refresh module-level vars that may have been read before .env loaded.
    global ANTHROPIC_API_KEY, CLAUDE_CLI_PATH
    global DISCORD_WEBHOOK_URL, DISCORD_PING_USER_ID
    global DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", ANTHROPIC_API_KEY)
    CLAUDE_CLI_PATH = os.environ.get("CLAUDE_CLI_PATH", CLAUDE_CLI_PATH)
    DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL)
    DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", DISCORD_BOT_TOKEN)
    DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", DISCORD_CHANNEL_ID)
    DISCORD_PING_USER_ID = os.environ.get("DISCORD_PING_USER_ID", DISCORD_PING_USER_ID)
    _resolve_kalshi_auth()


# Resolve once at import so anything reading the constants before main()
# sees the right values for the default WALLET.
_resolve_kalshi_auth()
