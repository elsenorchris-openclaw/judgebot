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
ENABLE_SELLS: bool = True    # 2026-05-26: enabled SOLELY for the adverse-drift exit
                            # (the dormant LLM run_exit_loop stays OFF via
                            # ENABLE_LLM_EXIT_LOOP=False below). Origin-tag guard
                            # in execute_sell prevents touching V2-max / V2-min
                            # positions on the shared wallet.

# 2026-05-26: keep the dormant LLM-era exit loop (run_exit_loop) OFF even though
# ENABLE_SELLS is now True. Sells are gated to the adverse-drift exit ONLY.
ENABLE_LLM_EXIT_LOOP: bool = False

# 2026-05-26: adverse-drift stop-loss (the ONLY sell path). The market corrects
# against a losing position within ~30-60 min of entry (informed order flow,
# measured: positions where the held-side bid drifts >X against us settle ~14-30%
# vs ~69% when it drifts toward us). Exit when the held-side BID falls >=
# ADVERSE_DRIFT_EXIT_PP cents below its entry-time value, SUSTAINED for
# ADVERSE_DRIFT_SUSTAIN_MIN minutes (filters momentary dip-and-recover whipsaws),
# within ADVERSE_DRIFT_WINDOW_MIN of entry. Conservative variant (10c/60m/sustain15).
# Backtest settled 2026-05-14..24 (n=258): hold -$174.71 -> exit -$151.17
# (+$23.54, both date-halves +, whipsaw ~-$1.0). May-25 MTM +$5.05. Sells at the
# bid (crosses spread); only fires on positions opened by paper-judge that have a
# recorded entry baseline (entry_bid_c) -- pre-existing open positions hold to
# settlement as before. Rollback: ENABLE_ADVERSE_DRIFT_EXIT=False (and optionally
# ENABLE_SELLS=False to fully re-disable selling).
ENABLE_ADVERSE_DRIFT_EXIT: bool = True
ADVERSE_DRIFT_EXIT_PP: int = 10        # held-side bid must fall this many cents
ADVERSE_DRIFT_WINDOW_MIN: int = 60     # only watch the first 60 min after entry
ADVERSE_DRIFT_SUSTAIN_MIN: int = 15    # breach must persist this long before exit


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
    # LOW BUY_YES is unaffected — it's capped by max_bet_low_series_usd (currently $3).
    "max_bet_yes_usd": 15.0,
    # 2026-05-16: HIGH-series brackets (KXHIGH-*) capped tighter after a string
    # of forecast-anchored BUY_NO losses (HOU B88.5, MIN B80.5, NY B78.5, LV B95.5).
    # Applied as min(side_cap, high_series_cap) when ticker starts with "KXHIGH".
    # 2026-05-20: raised 5 -> 15 (Chris directive). HIGH is the profitable book
    # (+$40 on 5/20 vs LOW -$24); lean bet size into it. pure_nn_decide sizing
    # reads this same value via the worker so qty is sized to match the cap.
    "max_bet_high_series_usd": 20.0,  # 2026-05-28 (Chris): raised 15->20 to match BOS/SEA skill tier raise to $20. Backstop ceiling for per-station sizing -- REJECTS bets above this. Was $15 since 2026-05-25 (matched BOS/SEA=$15 then).
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
    "max_bet_low_series_usd": 3.0,  # 2026-05-26: bumped 1->3 (3x harvest) after live validation of the PUSH_LOW_POST_AT_MID probe. n=11 fills over 2 days at $1: +$3.72 realized (+$0.34/trade), 85% fill rate, +~11c/contract -- matches the +11.7c posted-at-mid backtest mechanism. Per-trade absolute risk small; LOW BUY_YES (previously the cross-loser) flipped to +$4.31/7 in the probe sample as expected (cheaper entry post-at-mid changes EV sign). KDEN BUY_NO B-bracket stays at $10 via PUSH_LOW_NO_BET_BY_STATION; all other LOW (incl. BUY_YES any station, T tails) lifts from $1 to $3.
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
AUTO_EXEC_LOW_ENABLED: bool = False   # 2026-05-27 (Chris): OFF -- judge low side handed to min_bot_jua on the v2 wallet. Was True ($3 probe since 05-23). Set True to restore.
AUTO_EXEC_HIGH_YES_ENABLED: bool = True  # 2026-05-25 (Chris): RE-ENABLED at reduced $3 cap (PUSH_HIGH_YES_MAX_BET_USD). Reverses the 077b511 pause (HIGH YES 36% win/-20% ROI) -- small live YES probe. False to re-pause.
PUSH_HIGH_YES_MAX_BET_USD: float = 6.0  # 2026-05-28 (Chris): raised 3->6 after 5/27 +$105 combined day where HIGH YES winners hit the $3 cap (judge MIA YES +$4.27; v1max MSP/DCA/MIA/AUS/MSY YES winners netted +$45+). Tracks ~60% of the NO default ($10 via PUSH_HIGH_MAX_BET_DEFAULT). False AUTO_EXEC_HIGH_YES_ENABLED to re-pause.
USE_MU_AGREEMENT_GATE: bool = False  # 2026-05-26 (Chris): DISABLED. Faithful BUY_NO PnL backtest (n=170, 04-27..05-21, both halves) shows the gate is net-negative in EVERY form: NO-gate +1124c vs live@4.0 +704c vs old@2.0 -89c. High matcher<->NWP disagreement = matcher-HOT = fat-edge = our PROFITABLE INDEPENDENT bets; the gate removed net WINNERS (old@2.0 removed +1213c of winners). Our edge IS independence from the NWP/market consensus, not agreement with it. Tool /tmp/gate_pnl_bt.py. Set True to re-enable (rm carve-out + MU_AGREEMENT_MAX_DIFF_F below then apply again).
MU_AGREEMENT_MAX_DIFF_F: float = 4.0  # 2026-05-26 (Chris): loosened 2.0->4.0. At 2.0 the gate blocked ~100% of HIGH exec on 2026-05-26 (matcher ran +2..+6F above the NWP blend on a hot day where NWP was running LOW; rm already exceeded NWP pre-peak on DFW/OKC/DEN/SFO/LAX). 2.0 was too tight for a low-NWP regime. Paired with the rm carve-out in nn_shadow_worker (don't veto when rm>=mu_nwp). disagreement (deg F) above which the HIGH trade is skipped.

# 2026-05-24: LOW posting probe. When True, LOW push buys POST a maker limit at
# MID (round((bid+ask)/2)) and REST it (async-adopted on fill) instead of crossing
# the wide LOW spread (+1c taker). Backtest: LOW BUY at cross -8.1c/bet vs +1.8c at
# mid -- LOW problem is execution, not signal. Measures live fill-rate at mid.
# LOW-only; HIGH + the cross path are untouched. Set False to revert to crossing.
PUSH_LOW_POST_AT_MID: bool = True
# 2026-05-26: resting-order risk mgmt. A LOW maker bid only fills when the
# market comes DOWN to it (adverse selection); the 120s loop is too slow to
# cancel before a collapse picks us off, so cap exposure with a NATIVE Kalshi
# order TTL (expiration_ts) instead of resting GTC. On expiry the model-gated
# auto-exec re-posts fresh at the live mid (no chasing: edge gate blocks it if
# the drop was real). post_only guarantees we never cross. 0 = old GTC behavior.
PUSH_LOW_POST_TTL_S: int = 90
PUSH_LOW_POST_POST_ONLY: bool = True
PUSH_LOW_POST_ADVERSE_C: int = 3   # belt: per-cycle early-cancel if our side's mid fell >= this many c below post
PUSH_HIGH_MAX_BET_DEFAULT: float = 10.0  # 2026-05-28 (Chris): raised 6->10 default after 5/27 +$105 combined day showed judge under-sized when matcher was right (judge +$18 on $57 cost = +32% ROI vs v1max +$88 on $135 = +65%; judge's biggest realized winners MIA YES/MSY/PHX were all sized at the de-sized/YES floor $2.76-$3.60). Only BOS/SEA stay $15 via PUSH_HIGH_MAX_BET_BY_STATION. NOTE cascade (edge-band sizes relative to this): 18-26pp up-tilt -> min($15, 10*2)=$15 (now CLIPS at the guardrail = same effective size as skill tier); fat >=26pp de-size -> 10*0.5=$5. (6->10 on 5/28; 3->6 on 5/27; 5->3 on 5/25; $5 uniform 5/23.)
PUSH_HIGH_MAX_BET_BY_STATION = {
    # 2026-05-25 (Chris): BOS + SEA sized to $15 -- the ONLY two stations whose
    # matcher actually beats the market on Brier (last-month, h2pk 2-5: BOS
    # +9-12%, SEA +8%, n~300 each, robust across aggregators). Concentrate
    # capital where we have a real, market-relative edge. ALL OTHER stations
    # stay $5 via PUSH_HIGH_MAX_BET_DEFAULT (unchanged). Requires the guardrail
    # max_bet_high_series_usd=15 (backstop) or these get rejected. HIGH YES is
    # unaffected -- it stays $3 via the PUSH_HIGH_YES_MAX_BET_USD down-size.
    "KBOS": 20.0,
    "KSEA": 20.0,
    # 2026-05-25 (Chris): SFO un-benched and left on the $3 default (no explicit
    # entry). The bench wasn't OOS-robust -- SFO sign-FLIPS across the early/late
    # split (+8.9c early, -23.9c late; cross-station PnL corr ~0.06) -- but it's
    # not a +EV edge either, so it stays at the base $3 size, not the $15 skill tier.
}
PUSH_HIGH_NO_BET_BY_STATION = {}  # 2026-05-22 (Chris): removed the $30 MIA-NO carve-out — uniform $15 max for all HIGH now (PUSH_HIGH_MAX_BET_DEFAULT). NO-resize code in nn_shadow_worker stays but is dormant while empty; re-add {station: usd} to size a NO cell up.

# 2026-05-26 (Chris): EDGE-BAND sizing tilt — size up the band where the model's edge is
# RELIABLE, not the fat tail. REVERSES the 5/25 fat-edge tilt (commit 5673b63). Deep dive
# (faithful live-era harness, Mar15-May20, /tmp/edge_diag*.py + sizing_test.py): model edge
# and win-rate are INVERSELY related (18-26pp = 60% WR / +8.7c/bet; >=35pp = 41% WR /
# +2.1c/bet with the LATE half NEGATIVE) — high edge is manufactured by an over-tight sigma
# (overconfidence), so the fattest edges are the LEAST trustworthy. Recalibrating p_yes
# (sigma-widen OR empirical isotonic) is a WASH (monotone; the 18pp floor already does it),
# so the lever is SELECTION/SIZING by edge band. Moving the x2 from >=35pp to [18,26)pp:
# $3-station ROC 1.8->5.2% with BOTH OOS halves turning positive (the >=35pp tilt was
# early+7.1/late-2.8 = both-halves FAIL). HIGH BUY_NO ONLY. Effective cap =
# min(max_bet_high_series_usd guardrail, base x MULT), so a moderate-edge NO at a $3 station
# -> $6 and BOS/SEA stay $15; YES untouched. JUDGE-ONLY (v1max frozen). Set
# PUSH_HIGH_EDGE_TILT_ENABLED False to revert; to restore the old fat tilt set LO=35/HI=101.
# 2026-05-26 (Chris) S3 add-on: ALSO size DOWN the fat tail. An edge >= DESIZE_PP means the
# model WILDLY disagrees with the market = usually our own sigma-overconfidence (those bets
# win ~41% vs ~60% in the reliable band, late half negative), so HALVE them (x DESIZE_MULT) ->
# SAME expected PnL with ~23% less capital at risk (faithful book ROC 13.5->17.6%, both OOS
# halves +; May-25 walk-through: fat bets went 2-3, halving them ~flat PnL / -23% capital).
# Only ever DECREASES (floored at min_buy). Skill-sized stations (base cap > the $3 default =
# BOS/SEA) are EXEMPT — their $15 is a Brier-skill call, not an edge call. Set DESIZE_MULT >= 1.0
# to disable the de-size while keeping the up-tilt. JUDGE-ONLY (v1max frozen).
PUSH_HIGH_EDGE_TILT_ENABLED: bool = True
PUSH_HIGH_EDGE_TILT_BAND_LO_PP: float = 18.0  # size up a HIGH BUY_NO when its edge is in
PUSH_HIGH_EDGE_TILT_BAND_HI_PP: float = 26.0  # [LO, HI) pp — the reliable-edge band
PUSH_HIGH_EDGE_TILT_MULT: float = 2.0         # up-multiplier on the station base cap, clipped to the guardrail
PUSH_HIGH_EDGE_TILT_DESIZE_PP: float = 35.0   # 2026-05-28: 26->35 (deep-dive: .26-.35 band is +28c/bet PROFITABLE, was wrongly halved; .35+ stays de-sized)   # at/above this edge (pp) a HIGH BUY_NO is sized DOWN
PUSH_HIGH_EDGE_TILT_DESIZE_MULT: float = 0.5  # de-size multiplier on the station base cap (skill stations exempt)

# 2026-05-24 (Chris): per-station LOW BUY_NO size-up. Deep-dive found pooled LOW
# loses because the matcher's sigma is ~2.75x too small (RMSz 2.69) -> fake NO
# edges; sigma-inflation BACKFIRES (shorts every narrow bracket). The exception
# is the deep-history stations where the matcher IS calibrated: NYC (RMSz 1.45)
# and DEN (RMSz 1.30, ~= HIGH's 1.32). There, faithful gated BUY_NO is positive
# in BOTH date halves on B brackets (NYC +8.0/+7.7c n=45, DEN +4.3/+16.1c n=30;
# T-tail brackets are NOT validated, n=6). So size up ONLY the validated subset:
# {station} BUY_NO on B brackets -> this cap; everything else (LOW YES, T tails,
# all other stations) stays at GUARDRAILS.max_bet_low_series_usd ($3 as of 2026-05-26;
# was $1 probe). Applied in low_post_probe.place() (LOW posts at MID, doesn't cross).
# Empty {} = uniform $3 base cap.
PUSH_LOW_NO_BET_BY_STATION = {"KDEN": 10.0}  # 2026-05-24 (Claude review): KNYC HELD -- independent faithful backtest shows NYC LOW-NO early-half NEGATIVE (-7.5c; net +8c but not robustly both-halves on my split). DEN robust both halves (+32.7/+25.6c). Re-add "KNYC": 10.0 after reconciling methodology (NO-only? RMSz).

# 2026-05-21: the push decision window comes SOLELY from the per-(station,
# series, month) window table in push_window_overrides.PUSH_WINDOW_OVERRIDES.
# There is NO default-window fallback -- it was removed to eliminate a confusing
# second source of truth and silent guessing on un-validated cells. A cell
# missing from the table is NOT traded and fires a Discord alert
# (nn_shadow_worker._alert_missing_window). The old global defaults
# PUSH_PEAK_HOURS_BEFORE / AFTER_HIGH / AFTER_LOW / AFTER were removed.
# USE_PUSH_WINDOW_OVERRIDES is now a master kill-switch: True (default) = the
# table is the sole window source; False = push window system OFF (no trades,
# no alert). Table generated from /home/ubuntu/data/phq_combined.csv backtest.
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

# 2026-05-21: early-side trim for HIGH "accurate-but-wide" window cells. The
# window table is built on MAE (mu accuracy), but accuracy != PnL. ~40 HIGH
# cells are accurate (mae < MAE_MAX) yet open >1h before peak; at those early
# offsets the matcher hasn't seen enough of the day's curve to call the ~1-2F
# bracket. Validated 2024-2025 holdout (n=12,548): offset < -1.25 lands in the
# WRONG bracket 60% of the time and misses by >=2F (Miami-scale) 32% of the
# time, vs 46%/16% in the [-1.0,0] keep zone; 38 of 40 cells worse early. Live
# PnL (5/19-21, n=52) agreed: trimming to offset>=-1.0 lifted HIGH +$18.58 ->
# +$65.51. So cap how EARLY these cells open, leaving `after`, peak time, the
# wide-but-INACCURATE cells (MAE-sizing already shrinks those), and all LOW
# windows untouched. Applied in _in_decision_window. Set ENABLED=False to revert.
PUSH_EARLY_TRIM_HIGH_ENABLED: bool = False  # 2026-05-21 OFF: the temp deep-pre-peak
# window (PUSH_HIGH_TEMP_WINDOW, below) needs before=3.0 which the trim would cap;
# the 67-day backtest showed the un-trimmed early zone is where the PnL is. Re-enable
# with True if reverting PUSH_HIGH_TEMP_WINDOW to None.
PUSH_EARLY_TRIM_BEFORE_CAP: float = 1.0   # HIGH cell opens no earlier than peak-1.0h
PUSH_EARLY_TRIM_MAE_MAX: float = 1.6      # only "accurate" cells (full-size tier); inaccurate wide cells left to MAE-sizing

# 2026-05-21 TEMP deep-pre-peak HIGH window from the 67-day candlestick-price
# backtest sweep (look-ahead-free, settled 3/15-5/20): h2pk 2-3h before peak was
# the max, +1329c / 31% win / +3.2 per trade, positive on 9/12 stations, vs the
# current at-peak windows' -416c. Mechanism: market softest when the high is
# hours away, where the matcher's analog projection has the most edge. Needs the
# early-trim OFF (below) since before=3.0 would otherwise be capped. Reversible:
# set to None (and re-enable trim) to revert. Superseded by the per-(station,
# month) regen once the full multi-year backfill lands. HIGHER VARIANCE (31% win).
PUSH_HIGH_TEMP_WINDOW = (2.0, -1.5)   # (before, after); None=off. 2026-05-23: 30-min deep default [peak-2,peak-1.5], BEGINS at peak-2 (profitable deep zone). For stations not in BY_STATION.

# 2026-05-25 (Chris): per-station HIGH price windows, REGENERATED from the
# LAST-MONTH faithful sweep (Apr 22-May 20, 29 days), buy-at-window-open, live
# 18pp edge floor, 30-min windows. Looked up before the global
# PUSH_HIGH_TEMP_WINDOW above; a station ABSENT here falls back to that global
# deep default. Reversible: set to None/{} to revert all HIGH to the global.
# (before, after) hrs vs fractional peak; window = [peak-before, peak+after].
# Dominant finding vs the prior windows: they were too SHALLOW -> most stations
# moved DEEPER (the market is soft 3-5h pre-peak and sharp into the peak).
PUSH_HIGH_TEMP_WINDOW_BY_STATION = {
    # Defensible ship rule: moved to a new window only where well-supported
    # (n>=8, BOTH date-halves positive, profitable); else kept the prior live
    # window. c/bet + n are the last-month buy-at-open numbers. KSFO was benched
    # 2026-05-25 then RE-ENABLED same day at $5 (the bench wasn't OOS-robust --
    # SFO flips sign across the early/late split); it uses the global deep window.
    "KATL": (2.0, -1.5),   # +14c n17 (was 3.5; shallower)
    "KAUS": (4.5, -4.0),   # +19c n8  (was 2.5 -5c -> flips +, DEEPER)
    "KBOS": (5.0, -4.5),   # +44c n10 90%WR (was 1.5 +7c; DEEPER, clean monotonic rise)
    "KDCA": (2.0, -1.5),   # no last-month data -> global deep default (unchanged)
    "KDEN": (2.0, -1.5),   # +6c n14 (unchanged; deep is +EV but thin n6)
    "KDFW": (3.0, -2.5),   # +38c n8  (was 2.5 +7c; DEEPER)
    "KHOU": (4.0, -3.5),   # +8c n12  (was 1.0 -12c -> flips +, DEEPER; deep -> watch stale AM fills)
    "KLAS": (2.5, -2.0),   # +14c n18 (was 2.0; DEEPER)
    "KLAX": (2.0, -1.5),   # +11c n15 (was 3.0 -1c -> flips +, shallower)
    "KMDW": (2.5, -2.0),   # +17c n12 (was 1.0 -18c -> flips +, DEEPER)
    "KMIA": (2.5, -2.0),   # +26c n15 (was 3.0 +18c; shallower)
    "KMSP": (3.0, -2.5),   # +6c n14  (was 1.5 -2c -> flips +, DEEPER)
    "KMSY": (2.5, -2.0),   # +9c n13  (unchanged)
    "KNYC": (2.5, -2.0),   # 2026-05-25: 3.5->2.5. The 3.5h window backtests NEGATIVE at the live 18pp floor (-7.2c May / -7.5c all-era, NO 37%) -- mu over-projects the high ~+1F that deep, so it shorts brackets the high lands in. peak-2.5h is robust both-halves +EV (+12.1c May / +3.6c all-era, NO 57%). On full-curve mu (NN_LOOKBACK_HIGH_MIN=0). Prior 3.5 "+9c n17" was a last-month/12pp-floor artifact.
    "KOKC": (2.5, -2.0),   # +19c n12 (was 1.0; DEEPER)
    "KPHL": (3.0, -2.5),   # +11c n13 (unchanged; no better both-halves+ window)
    "KPHX": (3.0, -2.5),   # +13c n12 (was 2.0; DEEPER)
    "KSAT": (3.0, -2.5),   # +17c n13 (was 2.0; DEEPER)
    "KSEA": (3.0, -2.5),   # +27c n14 (unchanged; already best)
    "KSFO": (2.0, -1.5),   # 2026-05-25: re-enabled at $3 default; global deep window (no +EV last-month slot, not optimized)
}

# 2026-05-25 (Chris): HIGH stations to BENCH (skip HIGH push auto-exec entirely).
# Gated in nn_shadow_worker._try_auto_execute as "high_station_benched". LOW is
# unaffected. Add a station ("KXXX") to bench it; empty = none benched.
# NB: simply OMITTING a station from PUSH_HIGH_TEMP_WINDOW_BY_STATION does NOT bench
# it -- it falls back to the global PUSH_HIGH_TEMP_WINDOW window. Benching needs this set.
# KSFO was benched here 2026-05-25 then RE-ENABLED same day at the $3 default: the bench
# wasn't OOS-robust (SFO is a sign-flip across the early/late split, not a structural -EV edge).
PUSH_HIGH_DISABLED_STATIONS = frozenset()

# 2026-05-22: LOW placeholder window (analog to PUSH_HIGH_TEMP_WINDOW). The
# MAE-built LOW overrides open too deep pre-min (h2pk>=2.0 = 40% WR in faithful
# trades); the good zone is near/post-min (65%). This near/post-min placeholder
# replaces all 20 LOW windows until the LOW candle backfill lands for a proper
# price-gated per-cell regen. The offset is global but anchored to each
# station's OWN min time. (before, after): window = [min-before, min+after].
# Reversible: set PUSH_LOW_TEMP_WINDOW=None to revert all LOW to the overrides.
PUSH_LOW_TEMP_WINDOW = (2.5, -2.0)   # 2026-05-23: 30-min deep-pre-min [min-2.5,min-2.0], BEGINS at min-2.5h = the offset curve LEAST-BAD LOW zone (-4.3c vs -15c near/post-min, which the old (0.5,1.5) targeted -- the worst). Still -EV crossing; $1 probe tests live exec. (Supersedes the near/post-min note above.)
PUSH_TEMP_WINDOW_MONTHS = {5}   # 2026-05-22: months the per-station temp windows are active (May = profit-optimized). Other months fall to the month-keyed PUSH_WINDOW_OVERRIDES table.
# 2026-05-24: per-station month override for the HIGH price window. A station listed
# here uses its PUSH_HIGH_TEMP_WINDOW_BY_STATION price window in THESE months instead of
# the global PUSH_TEMP_WINDOW_MONTHS. Added KMDW/KBOS for Mar+Apr: their month-keyed
# MAE override windows open near/after peak (KMDW peak+0.5/-0.5, KBOS peak-2.5/-3.0 wide)
# and LOSE in the live era (faithful Mar15-Apr: KMDW -5.0c/bet NOwin36%, KBOS -5.0c/bet
# NOwin44%). Their deep price windows (KMDW peak-1.0, KBOS peak-1.5) flip both to ROBUST
# (+14.6c/+8.1c per bet, both OOS halves +). Head-to-head delta +561c/+279c. LAX EXCLUDED
# (its MAE window already wins +10c robust; price window slightly worse). Reversible:
# empty this dict -> all stations revert to the global PUSH_TEMP_WINDOW_MONTHS.
PUSH_TEMP_WINDOW_MONTHS_BY_STATION = {
    # 2026-05-24 (Claude review): KMDW HELD -- independent faithful backtest Mar-Apr price-window -26c/bet (2W/13L); re-add "KMDW": {3,4,5} after reconciling (NO-only?). KBOS confirmed Mar-Apr +23.9c.
    "KBOS": {3, 4, 5},
}
PUSH_LOW_TEMP_WINDOW_BY_STATION = {}

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
# 2026-05-21: re-calibrated against the holdout (predicted regime-adjusted MAE
# -> actual MAE). Decile calibration is monotonic for adjusted_mae>=~1.0; the
# favorable extreme over-corrects (predicted 0.57 -> actual 1.63, since the
# additive deltas extrapolate below the ~1.2F irreducible floor), so the lowest
# tier is widened to <1.6 (all full size — over-correction can't over-size past
# the 1.0 cap). Per-tier actual MAE: <1.6→~1.4, 1.6-2.4→1.67, 2.4-3.2→2.21,
# >3.2→3.81; multipliers ~ accuracy (between 1/MAE and Kelly 1/MAE^2).
PUSH_MAE_CONF_TIERS: list = [
    (0.0, 1.6, 1.0),     # adjusted MAE <1.6F: full size (actual ~1.4F)
    (1.6, 2.4, 0.7),     # actual ~1.67F
    (2.4, 3.2, 0.5),     # actual ~2.21F
    (3.2, 99.0, 0.3),    # actual ~3.81F: minimal
]

# 2026-05-21: GLOBAL regime-MAE adjustment. Before tiering the sizing MAE, adjust
# the cell's MAE by the day's regime via global (pooled-across-all-cells) deltas
# for sigma/anomaly/sky/wind (data/regime_mae_deltas.json + climate_normals_
# hourly.json). Out-of-sample validated: adding these lifts per-decision MAE-
# prediction corr 0.167 -> 0.229; the deltas are stable train->holdout and
# physical (high sigma/anomaly/cloud/wind = harder day). The correction is a
# DAMPED sum (dims correlate) added on top of the per-cell baseline. Sizing-only
# (no bet-direction flip). Flip False to revert to pure cell-MAE sizing.
USE_PUSH_REGIME_MAE_ADJ: bool = True
# 2026-05-21: deltas are now PER-SIDE ({high/low:{dim:{bucket}}}) — regime
# affects HIGH vs LOW oppositely (hot-anomaly: HIGH -0.25 / LOW +1.46), so the
# pooled version averaged opposite effects and was wrong for both. Per-side
# lifts holdout MAE-prediction corr 0.231 -> 0.250. Damping 0.6 -> 1.0: pooled
# deltas needed shrinkage to offset being wrong; clean per-side ones don't.
# Tier cutpoints unchanged (per-side@1.0 calibration <1.6->1.39, 1.6-2.4->1.81,
# 2.4-3.2->2.32, >3.2->3.61 still matches the 1.0/0.7/0.5/0.3 multipliers).
PUSH_REGIME_MAE_DAMP: float = 1.0

# Empirical tail-loss correction for open-ended T brackets (nn_shadow_strategy).
# The kNN matcher's Gaussian P(YES) under-states the fat-surprise tail (HIGH hot
# / LOW cold): measured Nov-2024→May-2026 (n≈5500, rm-conditioned, cross-station
# stable), realized loss on deep-margin tail BUY_NO is ~2× model at 2σ, ~5–10×
# at 2.5σ. When True, P(YES) of the fat tail is raised to the empirical floor so
# overconfident "deep-safe" tail BUY_NO deflate below the edge floor. Interior B
# brackets are well-calibrated and untouched. NOT a fix for interior over-
# projection losses (e.g. MIA 5/21), which are irreducible variance.
USE_TAIL_EMPIRICAL_PYES: bool = True

# Minimum edge_pp (percentage-points of P(direction) − market_implied) for
# nn_shadow_strategy.pure_nn_decide to fire. Default in the function is 6pp;
# we raise to 12pp based on 2026-05-20 backtest on n=196 trades (166 settled
# + 30 today as proxy via current bid). Edge floor sweep was monotonic on
# pure-nn cohort: 6→12pp lifts ROI from −0.0% → +0.8%; 6→15pp to +3.3%;
# 6→20pp to +6.0%. 12pp chosen as conservative move that preserves 70% of
# volume while filtering bottom-edge marginal trades.
PUSH_MIN_EDGE_PP: int = 18  # 2026-05-25 (validated): 12->18. Faithful HIGH backtest n=867 both OOS halves: 12-18pp band = -5.7c/bet; floor 18 lifts total +49->+55, c/bet +5.7->+7.2, both halves up; 20 over-trims. JUDGE-ONLY (v1max frozen).

# 2026-05-25: per-cell reliability trade-enable gate. Skip a BUY when the
# matcher's HISTORICAL MAE for this (station, season, local_hour, side) cell
# exceeds PUSH_MAE_GATE_F -- the k-NN projection is provably unreliable there,
# so the edge calc rests on a bad mu/sigma. This is the trade-time form of the
# accuracy-heatmap study: trade only where the matcher is historically sharp.
# DISTINCT from PUSH_MAE_CONF_TIERS (which only SHRINKS size) -- this hard-SKIPs.
# Additive on top of that sizing (the backtest P&L already reflects the shrink).
# MAE table: cell_mae_table.CELL_MAE, built from 2022-2025 heating_traces
# (n>=20/cell, 1184 cells) -- fully out-of-sample to 2026 live trades.
# Backtest (settled 2026-05-14..24, n=315): gating MAE>2.0F lifts realized P&L
# +$23.29 (kept -$118.53 vs ungated -$141.82); robust BOTH date-halves
# (H1 +$10.93, H2 +$12.36). Sigma calibration was tested first and REJECTED
# (per-cell, global inflation, and HIGH sigma-factor raise all hurt -- the
# BUY_NO miscalibration is a signal-skill limit, not a variance error, so no
# sigma transform separates winners from losers; this hard cell gate does).
# Fail-OPEN: unknown cell (n<20 or not in table) -> NOT gated.
# Rollback: PUSH_MAE_GATE_ENABLED=False.
PUSH_MAE_GATE_ENABLED: bool = True
PUSH_MAE_GATE_F: float = 2.0

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
# cut to a $1 cap (max_bet_low_series_usd), the default $1 min-buy equaled the
# cap and the integer-contract math collapsed (no qty satisfied both cost >= $1
# floor AND cost <= $1 cap except at exact-divisor prices), which would silently
# skip nearly all LOW buys. This lower floor lets LOW place genuine ~$0.40-$3.00
# bets (cap is now $3 as of 2026-05-26; floor stays $0.40 to preserve range).
# HIGH keeps the standard $1 min-buy (its $15 cap never binds on min-buy).
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
PUSH_MAX_SPREAD_C_HIGH: float = 15.0       # 2026-05-23: skip HIGH push BUY when (yes_ask - yes_bid) > 15c. Crossing a wide spread pays away the edge; backtest HIGH spread>15c = -21..-31c/bet vs +1.9c filtered (both halves OOS). LOW NOT filtered (it is a $1 probe). 0 = off.

# 2026-05-23: HIGH B-bracket BUY_NO thin-margin gate. Skip a BUY_NO on a 2-sided
# (B) bracket when the CLI-adjusted forecast (mu - per-station obs->CLI offset)
# lands INSIDE the bracket [floor-0.5, cap+0.5] -- i.e. the bot would short a
# bracket its OWN forecast points into. Faithful gated buy-at-open replay (live
# era 2026-03-15..05-19, production windows): these win 32% / -3.9c/bet, the
# effect is edge-INDEPENDENT (still -8.6c holding model edge fixed in [12,20]pp)
# and negative in BOTH date-halves. Gating lifts the kept HIGH push book
# +4.1->+7.6c/bet, +$7.9 INCREMENTAL over the shipped (2t) tail-bet gate (which
# only catches the rare p_yes>0.5 case), both OOS halves +. This is the
# THIN-boundary complement of USE_TAIL_EMPIRICAL_PYES (the deep-SAFE T-tail
# correction). HIGH only -- LOW flips sign (and is a $1 probe). Offset = the net
# mu->CLI bias (our obs runs ~+0.5F hot vs CLI; the matcher undershoots obs
# ~0.2F, partly cancelling) = per-station median(mu - yes_bracket_center) over
# the live era; stations absent here (e.g. KDCA, stale projections) use DEFAULT.
# Distinct from the REVERTED p_yes median-bias correction: this only SKIPS a bet
# (never shifts p_yes / flips a side), so it cannot turn a winner into a loser.
# Set PUSH_SKIP_NO_MU_NEAR_BRACKET=False to revert.
PUSH_SKIP_NO_MU_NEAR_BRACKET: bool = True
PUSH_NO_MU_CLI_OFFSET_DEFAULT: float = 0.5
PUSH_NO_MU_CLI_OFFSET_BY_STATION: dict = {
    "KATL": 0.1, "KAUS": 0.3, "KBOS": 0.9, "KDEN": 0.3, "KDFW": 0.0,
    "KHOU": 0.7, "KLAS": 0.6, "KLAX": -0.1, "KMDW": 0.2, "KMIA": 0.9,
    "KMSP": 0.8, "KMSY": 0.6, "KNYC": 0.4, "KOKC": 0.5, "KPHL": 0.4,
    "KPHX": 0.5, "KSAT": -0.3, "KSEA": 0.3, "KSFO": 0.4,
}

# 2026-05-26: Per-station BOUNDARY BAND for the thin-margin gate above. The
# shipped gate used a fixed 0.5°F band -- skip when (mu - offset) lands in
# [floor - 0.5, cap + 0.5]. Live-era 8-day analysis (5/18-5/25 EXEC pool n=93
# real-money + FIRST counterfactual pool n=181) shows the matcher's residual
# bias-low μ creates additional boundary risk within ~1.5°F of either edge.
# Validation: uniform 1.5°F band lifts EXEC HIGH BUY_NO from +$8.69 baseline to
# +$58.45 (lift +$49.76), WR 55%→66% on both pools; per-station tuning lifts
# further to +$63.79 (+$55.10). Per-station thresholds set from FIRST-pool
# subsample stability (≥69% stable runs): stations where matcher is reliable
# (KATL/KAUS/KDFW/KHOU/KSEA/KSFO) keep the narrow 0.5°F band; high-variance
# coastal/transitional (KBOS/KLAX) get 2.0°F; rest use DEFAULT 1.5°F.
PUSH_NO_MU_BOUNDARY_BAND_DEFAULT: float = 1.5
PUSH_NO_MU_BOUNDARY_BAND_BY_STATION: dict = {
    "KATL": 0.5, "KAUS": 0.5, "KDFW": 0.5, "KHOU": 0.5,
    "KSEA": 0.5, "KSFO": 0.5,
    "KBOS": 2.0, "KLAX": 2.0,
    # All others use DEFAULT 1.5°F:
    # KDCA, KDEN, KLAS, KMDW, KMIA, KMSP, KMSY, KNYC, KOKC, KPHL, KPHX, KSAT
}

# 2026-05-26: HIGH BUY_NO σ floor -- skip the bet when matcher's sigma_chosen
# is below this threshold (matcher overconfidence regime). 5/23-5/24 deep-dive:
# bad-day losers had σ avg 1.65 vs good-day winners 1.79; σ < 1.0 specifically
# isolates the extreme-overconfidence tail. Of 9 bad-day losers, the boundary
# band catches 3 and this σ floor catches an additional 2, with 0 false-positive
# winners caught at 1.0 in the 8-day sample. Set 0.0 to disable. Complements
# PUSH_SKIP_NO_MU_NEAR_BRACKET (boundary band) -- together they cover both
# "μ near boundary" AND "matcher overconfident outside boundary" failure modes.
PUSH_HIGH_NO_MIN_SIGMA_F: float = 1.0

# 2026-05-28: Per-station HIGH BUY_NO σ floor override. Stations where the matcher's
# claimed σ is structurally under-calibrated get a higher minimum than the global
# PUSH_HIGH_NO_MIN_SIGMA_F (=1.0). RMSz computed on n=75 days/station phq backfill
# (Feb-May 2026, mu_proj_f vs ext_f at h≈1.5 entry slot): floor = RMSz where RMSz > 1.3
# (clearly above sample noise -- 1.0-1.3 band is within calibration jitter). Counterfactual
# on 96 settled live HIGH BUY_NO trades: skipping 12 bets recovers +$36.83, concentrated
# at KPHX -$20.75 (4/4 losers skipped) and KSAT -$6.84 (3/3 losers skipped). Tool
# /tmp/judge_sigskip_cf.py. Confirms σ-overconfidence is station-specific, not new-vs-old
# (KLAX is OLD; KLAS dropped from RMSz 2.11 at n=3 to 0.96 at n=75 -- noise). Empty {}
# disables and falls back to global floor.
PUSH_HIGH_NO_MIN_SIGMA_BY_STATION: dict = {
    "KPHX": 2.26,   # RMSz 2.26 -- most overconfident; med_sigma 1.26 vs residual stdev 3.01
    "KSAT": 1.96,   # RMSz 1.96
    "KOKC": 1.74,   # RMSz 1.74
    "KLAX": 1.59,   # RMSz 1.59 -- OLD station; σ-overconfidence is not new-vs-old
    "KSEA": 1.41,   # RMSz 1.41
    "KMIA": 1.41,   # RMSz 1.41
    "KMDW": 1.34,   # RMSz 1.34
    # all others use PUSH_HIGH_NO_MIN_SIGMA_F default (=1.0)
}

# 2026-05-27: HIGH BUY_NO σ CEILING -- skip when matcher sigma_chosen is ABOVE
# this (low-confidence / wide-analog-cluster regime). Real-trade validation
# (judge+v1max actual trades n=165, 2026-05-15..25): sigma>2.5 BUY_NO 25%WR
# -$2.14/bet, negative BOTH date-halves AND both bots; skipping lifts the BUY_NO
# book +$34. Opposite-tail mirror of PUSH_HIGH_NO_MIN_SIGMA_F above. 0 = off.
PUSH_HIGH_NO_MAX_SIGMA_F: float = 2.5

# 2026-05-21: LOW cold-front gate ("Tier 1.5"). Distinct from PUSH_MAX_WIND_MPH
# above (40 mph, both sides, catastrophic). Sustained wind ≥ ~15 kt at an
# overnight LOW is a frontal / cold-air-advection signature: the nn matcher
# (trained on calm nights) over-projects the daily minimum, and — unlike
# high-variance regimes — its sigma does NOT widen to flag it, so the bot would
# trade a confident but wrong estimate. 25-yr backtest (3.17M evals,
# phq_offset_cond_combined): LOW sustained wind > 15 kt → MAE 3.1-4.3°F vs ~1.7
# calm, bias +1.6..+3.1°F in cold season; cross-year validated, 18/20 stations.
# HIGH is storm-robust (MAE flat, no systematic bias) → LOW-side only. Sustained
# wind only — a gust without sustained wind is convective, not frontal. 18 mph
# ≈ 15 kt. Set to 0 / -1 to disable.
PUSH_LOW_FRONT_WIND_MPH: float = 18.0      # LOW sustained wind ≥ this (mph) → skip
# Stations excluded from the LOW cold-front gate: marine climates where strong
# wind is onshore sea-breeze, not a cold front (backtest bias ≈ 0 both seasons).
PUSH_LOW_FRONT_EXCLUDE: tuple = ("KLAX", "KMIA")

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
NN_LOOKBACK_HIGH_MIN: int = 0            # 2026-05-25: 180->0 (full climate-day curve, like LOW). The 180-min truncation ran the matcher on a different mu than the shipped windows were built on (push windows derive from the per_hour_quality backtest, which uses the FULL morning curve, traj_n_bins~140). It also caused chronic/intermittent live no-fire for sparse-feed stations (NYC/BOS/SEA/DEN) when the last 180min had <12 5-min bins. Faithful full-vs-180 backtest (live era, current windows, 18pp floor): full +6.4c/bet n369 vs 180 +6.0c/bet n348 -- per-bet EV ~unchanged, full adds coverage (+21 trades; OKC/SEA clearly prefer full). JUDGE-ONLY (v1max frozen). Revert to 180 to restore truncation.
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
