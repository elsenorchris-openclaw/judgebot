"""Tests for the 2026-06-03 HIGH BUY_NO per-station cap raise (Chris).

A station may now hold TWO BUY_NO HIGH brackets (the 2nd-best same-station wing NO
is +EV via the sigma-play: +3.9c/ct, WR 0.87, +both backtest halves; cap_tiers.py).
Scoped HIGH+NO only and bounded at 2:
  - PUSH_MAX_TICKERS_PER_STATION_NO=2 raises the per-(station,direction) cap (gate 5)
  - GUARDRAILS max_buys_per_station_side_high=2 raises the correlation cap (gate 7)
YES stays 1, LOW stays 1 (both untested for >1). These tests pin the scoping so a
future cap change can't silently widen to YES or LOW.
"""
import os
import sys
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import kalshi_client  # noqa: E402
import low_post_probe  # noqa: E402
import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402
import paper_judge_bot as pjb  # noqa: E402


def _cand(ticker, series_prefix, station="KMIA", day="2026-05-31",
          floor=85.0, cap=86.0, label=85.5):
    city = "MIA"
    return market_universe.Candidate(
        ticker=ticker, series_prefix=series_prefix, city_code=city,
        station=station, climate_day=day, bracket_kind="B",
        floor=floor, cap=cap, bracket_label=label, market={})


def _packet(direction, series, floor, cap):
    # NO: mu placed FAR outside the bracket so the NO is "safe" (no thin-margin /
    # tail veto). HIGH NO -> mu below; LOW NO -> mu above. YES: mu inside.
    if direction == "BUY_YES":
        mu = (floor + cap) / 2.0
    else:
        mu = floor - 10.0 if series == "HIGH" else cap + 10.0
    return {
        "yes_ask_c": 40, "yes_bid_c": 38, "no_ask_c": 60, "no_bid_c": 58,
        "seconds_to_close": 10_000, "mu_chosen": mu, "floor": floor, "cap": cap,
        "sigma_chosen": 2.0, "local_clock": {"h_to_peak": 3.0},
        "traj_max": cap, "cur_tmpf": cap,
    }


def _decision(direction):
    return {"decision": direction, "edge": 0.20,
            "p_yes": 0.30 if direction == "BUY_NO" else 0.70,
            "reason": f"{direction} test edge=20.0pp"}


def _pos(station, action, day="2026-05-31", cost=0.40):
    return {"station": station, "action": action, "cost": cost, "date_str": day}


class _CapBase(unittest.TestCase):
    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._pending_buys.clear()   # 2026-06-05: no in-flight reservations leak between tests

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window
        nsw._pending_buys.clear()

    def _run(self, cand, decision, series, positions, cycle_buys=None, local_hour=12.0):
        nsw._rt = SimpleNamespace(positions=positions,
                                  cycle_buys_by_station_side=(cycle_buys or {}))
        packet = _packet(decision["decision"], series, cand.floor, cand.cap)
        with ExitStack() as es:
            P = lambda n, v: es.enter_context(mock.patch.object(config, n, v))
            P("PUSH_MAE_GATE_ENABLED", False)
            P("USE_MU_AGREEMENT_GATE", False)
            P("PUSH_HIGH_SKIP_IF_OFF_PEAK_F", 0.0)
            P("PUSH_TAIL_BET_MIN_EDGE_PP", 0)
            P("AUTO_EXECUTE_BUY_YES_PUSH", True)
            P("AUTO_EXECUTE_BUY_NO_PUSH", True)
            P("AUTO_EXEC_LOW_ENABLED", True)
            P("PUSH_HIGH_POST_AT_MID", False)
            P("PUSH_LOW_POST_AT_MID", True)
            # Pin the NO cap to 2 so this tests the cap-2 LOGIC regardless of the live
            # default (reverted to 1 on 2026-06-05 as a de-risk). The mechanism is what's
            # under test here; the live default is a separate config choice.
            P("PUSH_MAX_TICKERS_PER_STATION_NO", 2)
            es.enter_context(mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None))
            es.enter_context(mock.patch.object(kalshi_client, "get_balance_cached", return_value=100.0))
            es.enter_context(mock.patch.object(low_post_probe, "resting_rows", return_value=[]))
            es.enter_context(mock.patch.object(low_post_probe, "has_resting", return_value=False))
            es.enter_context(mock.patch.object(low_post_probe, "place", return_value=(True, "placed")))
            return nsw._try_auto_execute(cand, packet, decision, series=series, local_hour=local_hour)


class TestHighNoPositionCap(_CapBase):
    """Position cap (gate 5): HIGH NO -> 2, everything else -> 1."""

    def test_high_no_second_allowed(self):
        """1 existing HIGH BUY_NO -> a 2nd (different bracket) is ALLOWED."""
        positions = {"KXHIGHMIA-26MAY31-B83.5": _pos("KMIA", "BUY_NO")}
        cand = _cand("KXHIGHMIA-26MAY31-B85.5", "KXHIGH")
        executed, reason = self._run(cand, _decision("BUY_NO"), "HIGH", positions)
        self.assertNotIn("position_cap", reason, f"2nd HIGH NO wrongly blocked: {reason}")

    def test_high_no_third_blocked(self):
        """2 existing HIGH BUY_NO -> the 3rd is BLOCKED (bounded at 2)."""
        positions = {"KXHIGHMIA-26MAY31-B83.5": _pos("KMIA", "BUY_NO"),
                     "KXHIGHMIA-26MAY31-B87.5": _pos("KMIA", "BUY_NO")}
        cand = _cand("KXHIGHMIA-26MAY31-B85.5", "KXHIGH")
        executed, reason = self._run(cand, _decision("BUY_NO"), "HIGH", positions)
        self.assertFalse(executed)
        self.assertIn("position_cap", reason)
        self.assertIn("2>=2", reason)

    def test_high_yes_second_blocked(self):
        """YES is untouched: 1 existing HIGH BUY_YES -> 2nd BLOCKED (1>=1)."""
        positions = {"KXHIGHMIA-26MAY31-B83.5": _pos("KMIA", "BUY_YES")}
        cand = _cand("KXHIGHMIA-26MAY31-B85.5", "KXHIGH")
        executed, reason = self._run(cand, _decision("BUY_YES"), "HIGH", positions)
        self.assertFalse(executed)
        self.assertIn("position_cap", reason)
        self.assertIn("1>=1", reason)

    def test_low_no_second_blocked(self):
        """LOW is untouched: 1 existing LOW BUY_NO -> 2nd BLOCKED (1>=1)."""
        positions = {"KXLOWMIA-26MAY31-B70.5": _pos("KMIA", "BUY_NO")}
        cand = _cand("KXLOWMIA-26MAY31-B72.5", "KXLOW", floor=72.0, cap=73.0, label=72.5)
        executed, reason = self._run(cand, _decision("BUY_NO"), "LOW", positions, local_hour=4.0)
        self.assertFalse(executed)
        self.assertIn("position_cap", reason)
        self.assertIn("1>=1", reason)


class TestCorrelationCapSeriesAware(_CapBase):
    """Correlation cap (gate 7): HIGH -> 2, LOW -> 1 (series-aware lookup)."""

    def test_high_correlation_allows_two(self):
        """cycle_buys[HIGH]=1 -> a HIGH buy is still allowed (cap is 2)."""
        cyc = {("KMIA", "HIGH", "2026-05-31"): 1}
        cand = _cand("KXHIGHMIA-26MAY31-B85.5", "KXHIGH")
        executed, reason = self._run(cand, _decision("BUY_NO"), "HIGH", {}, cycle_buys=cyc)
        self.assertNotIn("correlation_cap", reason, f"HIGH corr-cap fired at 1: {reason}")

    def test_high_correlation_blocks_three(self):
        """cycle_buys[HIGH]=2 -> blocked (2>=2)."""
        cyc = {("KMIA", "HIGH", "2026-05-31"): 2}
        cand = _cand("KXHIGHMIA-26MAY31-B85.5", "KXHIGH")
        executed, reason = self._run(cand, _decision("BUY_NO"), "HIGH", {}, cycle_buys=cyc)
        self.assertFalse(executed)
        self.assertIn("correlation_cap", reason)

    def test_low_correlation_blocks_two(self):
        """LOW correlation cap stays 1: cycle_buys[LOW]=1 -> blocked."""
        cyc = {("KMIA", "LOW", "2026-05-31"): 1}
        cand = _cand("KXLOWMIA-26MAY31-B72.5", "KXLOW", floor=72.0, cap=73.0, label=72.5)
        executed, reason = self._run(cand, _decision("BUY_NO"), "LOW", {}, cycle_buys=cyc, local_hour=4.0)
        self.assertFalse(executed)
        self.assertIn("correlation_cap", reason)


class TestCapReservation(_CapBase):
    """Gate-5 in-flight reservation closes the cross-thread TOCTOU cap race (2026-06-05)."""

    def test_reservation_counts_toward_cap(self):
        """0 positions but 2 in-flight reservations (cap=2 via _run) -> the next NO is
        BLOCKED — a concurrent thread mid-place occupies the slot, so the race can't
        place past the cap."""
        nsw._pending_buys[("KMIA", "KXHIGH", "BUY_NO")] = 2
        cand = _cand("KXHIGHMIA-26MAY31-B85.5", "KXHIGH")
        executed, reason = self._run(cand, _decision("BUY_NO"), "HIGH", {})
        self.assertFalse(executed)
        self.assertIn("position_cap", reason)
        self.assertIn("2>=2", reason)

    def test_reservation_released_after_buy(self):
        """A successful buy reserves then releases (execution finally) -> no leak."""
        cand = _cand("KXHIGHMIA-26MAY31-B85.5", "KXHIGH")
        executed, reason = self._run(cand, _decision("BUY_NO"), "HIGH", {})
        self.assertNotIn("position_cap", reason)            # 0 positions + 0 reservations -> allowed
        self.assertEqual(nsw._pending_buys.get(("KMIA", "KXHIGH", "BUY_NO"), 0), 0)  # released

    def test_reservation_released_on_later_gate_reject(self):
        """A reject AFTER the reservation (gate-7 correlation cap) must release it, not
        leak it (a leak would permanently block the station/dir)."""
        cyc = {("KMIA", "HIGH", "2026-05-31"): 2}   # HIGH corr cap=2 -> reject
        cand = _cand("KXHIGHMIA-26MAY31-B85.5", "KXHIGH")
        executed, reason = self._run(cand, _decision("BUY_NO"), "HIGH", {}, cycle_buys=cyc)
        self.assertFalse(executed)
        self.assertIn("correlation_cap", reason)
        self.assertEqual(nsw._pending_buys.get(("KMIA", "KXHIGH", "BUY_NO"), 0), 0)  # not leaked


if __name__ == "__main__":
    unittest.main()
