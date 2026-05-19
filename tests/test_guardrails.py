"""Tests for guardrails.py.

These are the safety guarantees — any change to guardrails.py that doesn't
keep these tests green is a regression. Run with:

    pytest tests/test_guardrails.py -v
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from guardrails import (
    BuyDecision,
    GuardrailContext,
    SellDecision,
    check_buy,
    check_sell,
    record_buy,
    record_llm_failure,
    record_llm_success,
    record_sell,
)


# Match config.GUARDRAILS but keep tests independent of import.
G = {
    "max_bet_usd": 30.0,
    "max_ticker_total_usd": 50.0,
    "daily_spend_cap_usd": 200.0,
    "max_open_positions": 15,
    "max_daily_buys": 30,
    "max_daily_sells": 50,
    "min_price_cents": 5,
    "max_price_cents": 90,
    "no_new_buys_within_sec_of_close": 30 * 60,
    "no_sells_before_close_unless_triggered_sec": 6 * 3600,
    "rebuy_cooldown_sec_after_sell": 30 * 60,
    "daily_loss_kill_usd": -100.0,
    "consecutive_llm_failure_threshold": 3,
    "llm_failure_pause_sec": 5 * 60,
    "max_daily_api_spend_usd": 5.0,
}


def _ctx(**overrides) -> GuardrailContext:
    now = 1778800000.0  # 2026-05-14 18:13 UTC
    base = GuardrailContext(
        now_utc=now,
        today_utc_date="2026-05-14",
        mode="trader",
        enable_buys=True,
        enable_sells=True,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def _buy(**overrides) -> BuyDecision:
    base = dict(
        ticker="KXLOWTDC-26MAY13-B54.5",
        side="no",
        count=10,
        price_cents=60,
        cost_usd=6.0,
        seconds_to_close=2 * 3600,  # 2h
    )
    base.update(overrides)
    return BuyDecision(**base)


def _sell(**overrides) -> SellDecision:
    base = dict(
        ticker="KXLOWTDC-26MAY13-B54.5",
        side="no",
        count=10,
        limit_price_cents=30,
        seconds_to_close=2 * 3600,
        triggered=True,
    )
    base.update(overrides)
    return SellDecision(**base)


# ─────────────────────────────────────────────────────────────────────────────
# Mode + kill switch
# ─────────────────────────────────────────────────────────────────────────────
class TestMode:
    def test_observer_blocks_buys(self):
        ok, reason = check_buy(_ctx(mode="observer_only"), _buy(), G)
        assert not ok and "observer_only" in reason

    def test_observer_blocks_sells(self):
        ok, reason = check_sell(_ctx(mode="observer_only"), _sell(), G)
        assert not ok and "observer_only" in reason

    def test_killed_blocks_all(self):
        ok, _ = check_buy(_ctx(mode="killed"), _buy(), G)
        assert not ok
        ok, _ = check_sell(_ctx(mode="killed"), _sell(), G)
        assert not ok

    def test_kill_switch_blocks(self):
        ok, reason = check_buy(_ctx(kill_switch_active=True), _buy(), G)
        assert not ok and "kill" in reason.lower()

    def test_enable_flags(self):
        ok, _ = check_buy(_ctx(enable_buys=False), _buy(), G)
        assert not ok
        ok, _ = check_sell(_ctx(enable_sells=False), _sell(), G)
        assert not ok

    def test_trader_with_clean_state_allows_buy(self):
        ok, reason = check_buy(_ctx(), _buy(), G)
        assert ok, f"expected pass, got reason: {reason}"


# ─────────────────────────────────────────────────────────────────────────────
# Sizing caps
# ─────────────────────────────────────────────────────────────────────────────
class TestSizingCaps:
    def test_per_bet_cap(self):
        # 60 contracts × 60c = $36 > $30 cap
        # Reason can name either the legacy "max_bet_usd" key or the
        # side-specific "max_bet_no_usd" / "max_bet_yes_usd" — match prefix.
        ok, reason = check_buy(_ctx(), _buy(count=60, cost_usd=36.0), G)
        assert not ok and "max_bet" in reason

    def test_per_ticker_cap(self):
        ctx = _ctx()
        ctx.ticker_total_cost_usd["KXLOWTDC-26MAY13-B54.5"] = 30.0
        # +25 would push to $55 > $50 ticker cap
        ok, reason = check_buy(ctx, _buy(cost_usd=25.0), G)
        assert not ok and "max_ticker_total_usd" in reason

    def test_daily_spend_cap(self):
        ctx = _ctx(daily_spend_usd=195.0)
        ok, reason = check_buy(ctx, _buy(cost_usd=10.0), G)
        assert not ok and "daily spend" in reason

    def test_buy_at_exact_caps_allowed(self):
        # $30 bet exactly = should pass
        ok, reason = check_buy(_ctx(), _buy(count=50, cost_usd=30.0), G)
        assert ok, reason

    def test_open_position_count_limit(self):
        ctx = _ctx(open_positions_count=15)
        ok, reason = check_buy(ctx, _buy(), G)
        assert not ok and "open positions" in reason

    def test_open_position_limit_allows_addon(self):
        """Addons to existing tickers should not be blocked by position count."""
        ctx = _ctx(open_positions_count=15)
        ctx.ticker_total_cost_usd["KXLOWTDC-26MAY13-B54.5"] = 10.0
        ok, _ = check_buy(ctx, _buy(cost_usd=5.0), G)
        assert ok


class TestHighSeriesCap:
    """KXHIGH-* tighter cap shipped 2026-05-16 after string of forecast-anchored losses."""

    G_HIGH = dict(G, max_bet_no_usd=30.0, max_bet_yes_usd=10.0,
                  max_bet_high_series_usd=5.0)

    def test_high_series_no_blocks_above_5(self):
        # $6 BUY_NO on KXHIGH ticker should be blocked by HIGH cap
        ok, reason = check_buy(
            _ctx(), _buy(ticker="KXHIGHTHOU-26MAY16-B88.5", cost_usd=6.0), self.G_HIGH
        )
        assert not ok and "max_bet_high_series_usd" in reason

    def test_high_series_no_exact_5_allowed(self):
        ok, reason = check_buy(
            _ctx(), _buy(ticker="KXHIGHTHOU-26MAY16-B88.5", cost_usd=5.0), self.G_HIGH
        )
        assert ok, reason

    def test_high_series_yes_blocks_above_5(self):
        # YES side also capped because high_cap < yes_cap ($10).
        ok, reason = check_buy(
            _ctx(),
            _buy(ticker="KXHIGHTSEA-26MAY16-B54.5", side="yes", cost_usd=6.0),
            self.G_HIGH,
        )
        assert not ok and "max_bet_high_series_usd" in reason

    def test_low_series_unaffected(self):
        # KXLOW ticker should follow normal side cap, NOT the HIGH cap.
        ok, reason = check_buy(
            _ctx(), _buy(ticker="KXLOWTNYC-26MAY16-B68.5", cost_usd=20.0), self.G_HIGH
        )
        assert ok, reason

    def test_high_series_ticker_total_capped(self):
        # Existing $4 on KXHIGH ticker; +$2 push to $6 > $5 cap → blocked.
        ctx = _ctx()
        ctx.ticker_total_cost_usd["KXHIGHTHOU-26MAY16-B88.5"] = 4.0
        ok, reason = check_buy(
            ctx, _buy(ticker="KXHIGHTHOU-26MAY16-B88.5", cost_usd=2.0), self.G_HIGH
        )
        assert not ok and "max_bet_high_series_usd" in reason


# ─────────────────────────────────────────────────────────────────────────────
# Price caps
# ─────────────────────────────────────────────────────────────────────────────
class TestPriceCaps:
    def test_too_cheap(self):
        ok, reason = check_buy(_ctx(), _buy(price_cents=3), G)
        assert not ok and "min" in reason

    def test_too_expensive(self):
        ok, reason = check_buy(_ctx(), _buy(price_cents=95), G)
        assert not ok and "max" in reason

    def test_at_boundary(self):
        ok, _ = check_buy(_ctx(), _buy(price_cents=5), G)
        assert ok
        ok, _ = check_buy(_ctx(), _buy(price_cents=90), G)
        assert ok


# ─────────────────────────────────────────────────────────────────────────────
# Time-window guards
# ─────────────────────────────────────────────────────────────────────────────
class TestTimeWindow:
    def test_buy_too_close_to_close(self):
        ok, reason = check_buy(_ctx(), _buy(seconds_to_close=20 * 60), G)
        assert not ok and "time-to-close" in reason

    def test_buy_at_window_edge(self):
        ok, _ = check_buy(_ctx(), _buy(seconds_to_close=30 * 60), G)
        assert ok

    def test_untriggered_sell_too_early(self):
        """An exit predicate must trigger for early sells."""
        ok, reason = check_sell(
            _ctx(), _sell(seconds_to_close=8 * 3600, triggered=False), G
        )
        assert not ok and "untriggered" in reason

    def test_triggered_sell_early_allowed(self):
        ok, _ = check_sell(
            _ctx(), _sell(seconds_to_close=8 * 3600, triggered=True), G
        )
        assert ok

    def test_untriggered_sell_near_close_allowed(self):
        ok, _ = check_sell(
            _ctx(), _sell(seconds_to_close=2 * 3600, triggered=False), G
        )
        assert ok


# ─────────────────────────────────────────────────────────────────────────────
# Cooldowns
# ─────────────────────────────────────────────────────────────────────────────
class TestCooldowns:
    def test_rebuy_blocked_immediately_after_sell(self):
        ctx = _ctx()
        ctx.ticker_last_sell_ts["KXLOWTDC-26MAY13-B54.5"] = ctx.now_utc - 60
        ok, reason = check_buy(ctx, _buy(), G)
        assert not ok and "cooldown" in reason

    def test_rebuy_allowed_after_cooldown(self):
        ctx = _ctx()
        ctx.ticker_last_sell_ts["KXLOWTDC-26MAY13-B54.5"] = ctx.now_utc - 31 * 60
        ok, _ = check_buy(ctx, _buy(), G)
        assert ok


# ─────────────────────────────────────────────────────────────────────────────
# Circuit breakers
# ─────────────────────────────────────────────────────────────────────────────
class TestCircuitBreakers:
    def test_daily_loss_kill(self):
        ok, reason = check_buy(_ctx(daily_realized_pnl_usd=-105.0), _buy(), G)
        assert not ok and "daily P&L" in reason

    def test_llm_pause_blocks_buys(self):
        ctx = _ctx()
        ctx.llm_paused_until_ts = ctx.now_utc + 60
        ok, reason = check_buy(ctx, _buy(), G)
        assert not ok and "LLM" in reason

    def test_llm_failure_threshold_sets_pause(self):
        ctx = _ctx()
        record_llm_failure(ctx, pause_sec=300)
        record_llm_failure(ctx, pause_sec=300)
        assert ctx.llm_paused_until_ts == 0  # 2 failures, not yet at threshold
        record_llm_failure(ctx, pause_sec=300)
        assert ctx.llm_paused_until_ts > ctx.now_utc

    def test_llm_success_resets_counter(self):
        ctx = _ctx()
        record_llm_failure(ctx, pause_sec=300)
        record_llm_failure(ctx, pause_sec=300)
        record_llm_success(ctx)
        assert ctx.consecutive_llm_failures == 0

    def test_api_spend_cap(self):
        ctx = _ctx(daily_api_spend_usd=5.50)
        ok, reason = check_buy(ctx, _buy(), G)
        assert not ok and "API spend" in reason


# ─────────────────────────────────────────────────────────────────────────────
# Count limits
# ─────────────────────────────────────────────────────────────────────────────
class TestCountLimits:
    def test_daily_buy_count(self):
        ctx = _ctx(daily_buy_count=30)
        ok, reason = check_buy(ctx, _buy(), G)
        assert not ok and "daily buy count" in reason

    def test_daily_sell_count(self):
        ctx = _ctx(daily_sell_count=50)
        ok, reason = check_sell(ctx, _sell(), G)
        assert not ok and "daily sell count" in reason


# ─────────────────────────────────────────────────────────────────────────────
# Side / count sanity
# ─────────────────────────────────────────────────────────────────────────────
class TestSanity:
    def test_invalid_side_buy(self):
        ok, _ = check_buy(_ctx(), _buy(side="maybe"), G)
        assert not ok

    def test_invalid_side_sell(self):
        ok, _ = check_sell(_ctx(), _sell(side="maybe"), G)
        assert not ok

    def test_zero_count_buy(self):
        ok, _ = check_buy(_ctx(), _buy(count=0), G)
        assert not ok

    def test_negative_count_buy(self):
        ok, _ = check_buy(_ctx(), _buy(count=-1), G)
        assert not ok

    def test_sell_price_at_boundary(self):
        ok, _ = check_sell(_ctx(), _sell(limit_price_cents=1), G)
        assert ok
        ok, _ = check_sell(_ctx(), _sell(limit_price_cents=99), G)
        assert ok
        ok, _ = check_sell(_ctx(), _sell(limit_price_cents=0), G)
        assert not ok
        ok, _ = check_sell(_ctx(), _sell(limit_price_cents=100), G)
        assert not ok


# ─────────────────────────────────────────────────────────────────────────────
# Day rollover
# ─────────────────────────────────────────────────────────────────────────────
class TestDayRollover:
    def test_counters_reset_on_new_utc_day(self):
        # Set state at end of day
        ctx = _ctx(
            today_utc_date="2026-05-13",
            daily_spend_usd=150.0,
            daily_buy_count=12,
            daily_realized_pnl_usd=-50.0,
        )
        # Move clock forward 24h
        ctx.now_utc += 24 * 3600
        # Any operation should trigger rollover
        record_buy(ctx, "KX1", 5.0)
        assert ctx.today_utc_date != "2026-05-13"
        assert ctx.daily_spend_usd == 5.0
        assert ctx.daily_buy_count == 1
        assert ctx.daily_realized_pnl_usd == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Mutator round-trip
# ─────────────────────────────────────────────────────────────────────────────
class TestMutators:
    def test_record_buy_updates_state(self):
        ctx = _ctx()
        record_buy(ctx, "KX1", 10.0)
        record_buy(ctx, "KX1", 5.0)
        record_buy(ctx, "KX2", 8.0)
        assert ctx.daily_spend_usd == 23.0
        assert ctx.daily_buy_count == 3
        assert ctx.ticker_total_cost_usd["KX1"] == 15.0
        assert ctx.ticker_total_cost_usd["KX2"] == 8.0

    def test_record_sell_updates_state(self):
        ctx = _ctx(open_positions_count=2)
        ctx.ticker_total_cost_usd["KX1"] = 10.0
        record_sell(ctx, "KX1", realized_pnl_usd=-3.0, full_close=True)
        assert ctx.daily_sell_count == 1
        assert ctx.daily_realized_pnl_usd == -3.0
        assert ctx.open_positions_count == 1
        assert "KX1" not in ctx.ticker_total_cost_usd
        assert ctx.ticker_last_sell_ts["KX1"] == ctx.now_utc

    def test_partial_sell_keeps_position(self):
        ctx = _ctx(open_positions_count=2)
        ctx.ticker_total_cost_usd["KX1"] = 10.0
        record_sell(ctx, "KX1", realized_pnl_usd=2.0, full_close=False)
        assert "KX1" in ctx.ticker_total_cost_usd
        assert ctx.open_positions_count == 2
