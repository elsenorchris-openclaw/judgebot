"""guardrails.py — deterministic safety layer.

Wraps every LLM decision. The LLM can never override these. Any change here
requires Chris to read the diff and approve — these are the guarantees that
bound worst-case behavior independent of prompt quality.

Public surface:
  - GuardrailContext: snapshot of bot state needed to evaluate guardrails
  - check_buy(ctx, decision)  -> Result
  - check_sell(ctx, decision) -> Result
  - record_buy(ctx, ...)       — update mutable counters
  - record_sell(ctx, ...)

Result is a (ok: bool, reason: str | None) tuple. ok=False MUST be respected
by callers — log the rejection, do not execute.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class GuardrailContext:
    """In-memory snapshot of state the guardrails need.

    Owned by state.py and refreshed each cycle. Pass into check_*; mutate via
    record_*. No I/O lives here.
    """
    now_utc: float
    today_utc_date: str
    # Mutable per-day counters
    daily_spend_usd: float = 0.0
    daily_buy_count: int = 0
    daily_sell_count: int = 0
    daily_realized_pnl_usd: float = 0.0
    daily_api_spend_usd: float = 0.0
    # Per-bot state
    open_positions_count: int = 0
    # Per-ticker state
    ticker_total_cost_usd: dict[str, float] = field(default_factory=dict)
    ticker_last_sell_ts: dict[str, float] = field(default_factory=dict)
    # Failure tracking
    consecutive_llm_failures: int = 0
    llm_paused_until_ts: float = 0.0
    # Mode flags (mirror of config but mutable at runtime)
    mode: str = "observer_only"
    enable_buys: bool = True
    enable_sells: bool = True
    kill_switch_active: bool = False


@dataclass
class BuyDecision:
    ticker: str
    side: str          # "yes" or "no"
    count: int
    price_cents: int
    cost_usd: float    # count * price_cents/100
    seconds_to_close: float


@dataclass
class SellDecision:
    ticker: str
    side: str          # side we currently HOLD
    count: int
    limit_price_cents: int
    seconds_to_close: float
    triggered: bool    # True if exit triggered by anomaly predicate


Result = tuple[bool, Optional[str]]


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers (testable without I/O)
# ─────────────────────────────────────────────────────────────────────────────
def today_utc(now_ts: float) -> str:
    return datetime.fromtimestamp(now_ts, tz=timezone.utc).strftime("%Y-%m-%d")


def maybe_roll_day(ctx: GuardrailContext) -> None:
    """If now_utc is a new UTC day vs. ctx.today_utc_date, zero the daily
    counters. Called by record_* and check_* so callers don't have to."""
    today = today_utc(ctx.now_utc)
    if today != ctx.today_utc_date:
        ctx.today_utc_date = today
        ctx.daily_spend_usd = 0.0
        ctx.daily_buy_count = 0
        ctx.daily_sell_count = 0
        ctx.daily_realized_pnl_usd = 0.0
        ctx.daily_api_spend_usd = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Buy guardrails
# ─────────────────────────────────────────────────────────────────────────────
def check_buy(ctx: GuardrailContext, d: BuyDecision, g: dict) -> Result:
    """Return (ok, reason). ok=False means reject the order.

    `g` is config.GUARDRAILS — passed in so tests can substitute their own.
    """
    maybe_roll_day(ctx)

    if ctx.mode == "killed":
        return False, "mode=killed"
    if ctx.kill_switch_active:
        return False, "kill switch active"
    if ctx.mode == "observer_only":
        return False, "mode=observer_only (no orders)"
    if not ctx.enable_buys:
        return False, "ENABLE_BUYS=False"

    # Circuit breakers
    if ctx.daily_realized_pnl_usd <= g["daily_loss_kill_usd"]:
        return False, f"daily P&L {ctx.daily_realized_pnl_usd:.2f} ≤ kill {g['daily_loss_kill_usd']:.2f}"
    if ctx.now_utc < ctx.llm_paused_until_ts:
        return False, "LLM-failure pause active"
    if ctx.daily_api_spend_usd >= g["max_daily_api_spend_usd"]:
        return False, f"daily API spend ${ctx.daily_api_spend_usd:.2f} ≥ cap ${g['max_daily_api_spend_usd']:.2f}"

    # Sizing — side-aware per-bet cap (BUY_NO larger, BUY_YES smaller).
    if d.cost_usd <= 0:
        return False, "non-positive cost"
    if d.side == "no":
        side_cap = g.get("max_bet_no_usd", g.get("max_bet_usd", 15.0))
        side_key = "max_bet_no_usd"
    else:
        side_cap = g.get("max_bet_yes_usd", g.get("max_bet_usd", 15.0))
        side_key = "max_bet_yes_usd"
    # 2026-05-16: HIGH-series tighter cap (KXHIGH-*) overrides side_cap when lower.
    if d.ticker.startswith("KXHIGH"):
        high_cap = g.get("max_bet_high_series_usd")
        if high_cap is not None and high_cap < side_cap:
            side_cap = high_cap
            side_key = "max_bet_high_series_usd"
    # 2026-05-16 (evening): LOW-series tighter cap (KXLOW-*) — symmetric to HIGH.
    if d.ticker.startswith("KXLOW"):
        low_cap = g.get("max_bet_low_series_usd")
        if low_cap is not None and low_cap < side_cap:
            side_cap = low_cap
            side_key = "max_bet_low_series_usd"
    if d.cost_usd > side_cap:
        return False, f"cost ${d.cost_usd:.2f} > {side_key} ${side_cap:.2f}"
    existing = ctx.ticker_total_cost_usd.get(d.ticker, 0.0)
    # HIGH-series ticker-total mirror so topups can't balloon a $5 entry to $30.
    ticker_cap = g["max_ticker_total_usd"]
    ticker_cap_key = "max_ticker_total_usd"
    if d.ticker.startswith("KXHIGH"):
        high_cap = g.get("max_bet_high_series_usd")
        if high_cap is not None and high_cap < ticker_cap:
            ticker_cap = high_cap
            ticker_cap_key = "max_bet_high_series_usd"
    if d.ticker.startswith("KXLOW"):
        low_cap = g.get("max_bet_low_series_usd")
        if low_cap is not None and low_cap < ticker_cap:
            ticker_cap = low_cap
            ticker_cap_key = "max_bet_low_series_usd"
    if existing + d.cost_usd > ticker_cap:
        return False, (
            f"ticker total would be ${existing + d.cost_usd:.2f} "
            f"> {ticker_cap_key} ${ticker_cap:.2f}"
        )
    if ctx.daily_spend_usd + d.cost_usd > g["daily_spend_cap_usd"]:
        return False, (
            f"daily spend would be ${ctx.daily_spend_usd + d.cost_usd:.2f} "
            f"> cap ${g['daily_spend_cap_usd']:.2f}"
        )

    # Counts
    if ctx.daily_buy_count >= g["max_daily_buys"]:
        return False, f"daily buy count {ctx.daily_buy_count} ≥ {g['max_daily_buys']}"
    if ctx.open_positions_count >= g["max_open_positions"] and existing == 0:
        # New ticker only — addons to existing are allowed
        return False, f"open positions {ctx.open_positions_count} ≥ {g['max_open_positions']}"

    # Price
    if d.price_cents < g["min_price_cents"]:
        return False, f"price {d.price_cents}c < min {g['min_price_cents']}c"
    if d.price_cents > g["max_price_cents"]:
        return False, f"price {d.price_cents}c > max {g['max_price_cents']}c"

    # Time-to-close
    if d.seconds_to_close < g["no_new_buys_within_sec_of_close"]:
        mins = d.seconds_to_close / 60.0
        return False, (
            f"time-to-close {mins:.1f}min < "
            f"{g['no_new_buys_within_sec_of_close']/60.0:.0f}min"
        )

    # Re-buy cooldown after a recent sell
    last_sell = ctx.ticker_last_sell_ts.get(d.ticker, 0.0)
    cooldown = g["rebuy_cooldown_sec_after_sell"]
    if last_sell and (ctx.now_utc - last_sell) < cooldown:
        remaining = cooldown - (ctx.now_utc - last_sell)
        return False, f"rebuy cooldown active ({remaining/60:.1f}min remaining)"

    # Side / count sanity
    if d.side not in ("yes", "no"):
        return False, f"invalid side: {d.side}"
    if d.count <= 0:
        return False, "non-positive count"

    return True, None


# ─────────────────────────────────────────────────────────────────────────────
# Sell guardrails  (weaker than buy — sells unwind risk)
# ─────────────────────────────────────────────────────────────────────────────
def check_sell(ctx: GuardrailContext, d: SellDecision, g: dict) -> Result:
    maybe_roll_day(ctx)

    if ctx.mode == "killed":
        return False, "mode=killed"
    if ctx.kill_switch_active:
        return False, "kill switch active"
    if ctx.mode == "observer_only":
        return False, "mode=observer_only (no orders)"
    if not ctx.enable_sells:
        return False, "ENABLE_SELLS=False"

    if d.side not in ("yes", "no"):
        return False, f"invalid side: {d.side}"
    if d.count <= 0:
        return False, "non-positive count"
    if d.limit_price_cents < 1 or d.limit_price_cents > 99:
        return False, f"sell price {d.limit_price_cents}c outside [1, 99]"

    if ctx.daily_sell_count >= g["max_daily_sells"]:
        return False, f"daily sell count {ctx.daily_sell_count} ≥ {g['max_daily_sells']}"

    # Time-window: prevent early sells unless the exit predicate triggered.
    # RULE #2 (market is right) makes "panic sell" the most common mistake.
    if (
        not d.triggered
        and d.seconds_to_close > g["no_sells_before_close_unless_triggered_sec"]
    ):
        hrs = d.seconds_to_close / 3600.0
        max_hrs = g["no_sells_before_close_unless_triggered_sec"] / 3600.0
        return False, (
            f"sell rejected: untriggered + {hrs:.1f}h to close > {max_hrs:.0f}h gate"
        )

    return True, None


# ─────────────────────────────────────────────────────────────────────────────
# Mutators (call after a successful execute)
# ─────────────────────────────────────────────────────────────────────────────
def record_buy(ctx: GuardrailContext, ticker: str, cost_usd: float) -> None:
    maybe_roll_day(ctx)
    ctx.daily_spend_usd += cost_usd
    ctx.daily_buy_count += 1
    ctx.ticker_total_cost_usd[ticker] = (
        ctx.ticker_total_cost_usd.get(ticker, 0.0) + cost_usd
    )
    ctx.open_positions_count += 1 if ctx.ticker_total_cost_usd[ticker] == cost_usd else 0


def record_sell(
    ctx: GuardrailContext, ticker: str, realized_pnl_usd: float, full_close: bool
) -> None:
    maybe_roll_day(ctx)
    ctx.daily_sell_count += 1
    ctx.daily_realized_pnl_usd += realized_pnl_usd
    ctx.ticker_last_sell_ts[ticker] = ctx.now_utc
    if full_close:
        ctx.open_positions_count = max(0, ctx.open_positions_count - 1)
        ctx.ticker_total_cost_usd.pop(ticker, None)


def record_llm_failure(ctx: GuardrailContext, pause_sec: float) -> None:
    """Bump failure counter; on threshold, set pause window."""
    ctx.consecutive_llm_failures += 1
    if ctx.consecutive_llm_failures >= 3:  # threshold inlined here as a guarantee
        ctx.llm_paused_until_ts = ctx.now_utc + pause_sec


def record_llm_success(ctx: GuardrailContext) -> None:
    ctx.consecutive_llm_failures = 0


def record_api_spend(ctx: GuardrailContext, usd: float) -> None:
    maybe_roll_day(ctx)
    ctx.daily_api_spend_usd += usd
