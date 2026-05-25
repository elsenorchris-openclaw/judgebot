"""Tests for the LOW posting-probe resting-order position-cap gap (2026-05-25).

A resting LOW posting-probe order (low_post_probe posts a maker at mid and rests
until it fills) is not yet in _rt.positions, so before this fix a second
same-direction bracket on the same (station, climate_day) could slip past the
per-(station, series_prefix, direction) cap while the first order rested
unfilled -- e.g. LV 5/25 ended up holding two BUY_YES (T70 + B69.5).
nn_shadow_worker._try_auto_execute now counts resting orders toward the cap.
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


def _cand(ticker, station="KLAS", climate_day="2026-05-25",
          bracket_kind="B", floor=69.0, cap=70.0, label=69.5):
    return market_universe.Candidate(
        ticker=ticker, series_prefix="KXLOW", city_code="TLV",
        station=station, climate_day=climate_day, bracket_kind=bracket_kind,
        floor=floor, cap=cap, bracket_label=label, market={})


def _packet():
    return {"yes_ask_c": 50, "no_ask_c": 50, "seconds_to_close": 10_000}


def _decision(direction):
    return {"decision": direction, "edge": 0.20,
            "p_yes": 0.30 if direction == "BUY_NO" else 0.70,
            "reason": f"{direction} test edge=20.0pp"}


def _resting(ticker, station, climate_day, action):
    return {"ticker": ticker, "side": "yes" if action == "BUY_YES" else "no",
            "climate_day": climate_day,
            "entry_ctx": {"station": station, "action": action,
                          "series_prefix": "KXLOW", "climate_day": climate_day}}


class TestLowPostRestingCap(unittest.TestCase):
    """A resting probe order must occupy its per-(station, direction) slot."""

    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window

    def _run(self, resting, candidate, decision):
        """Run _try_auto_execute (LOW) with the probe + config controlled and
        place()/execute mocked so nothing hits the network."""
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})
        with ExitStack() as es:
            es.enter_context(mock.patch.object(config, "AUTO_EXEC_LOW_ENABLED", True))
            es.enter_context(mock.patch.object(config, "AUTO_EXECUTE_BUY_YES_PUSH", True))
            es.enter_context(mock.patch.object(config, "AUTO_EXECUTE_BUY_NO_PUSH", True))
            es.enter_context(mock.patch.object(config, "PUSH_LOW_POST_AT_MID", True))
            es.enter_context(mock.patch.object(
                config, "PUSH_MAX_TICKERS_PER_STATION_SIDE_DIRECTION", 1))
            es.enter_context(mock.patch.object(
                low_post_probe, "resting_rows", return_value=resting))
            es.enter_context(mock.patch.object(
                low_post_probe, "has_resting", return_value=False))
            es.enter_context(mock.patch.object(
                low_post_probe, "place", return_value=(True, "test-placed")))
            es.enter_context(mock.patch.object(
                kalshi_client, "get_balance_cached", return_value=100.0))
            return nsw._try_auto_execute(
                candidate, _packet(), decision, series="LOW", local_hour=4.0)

    def test_same_direction_resting_blocks_second_bracket(self):
        """The fix: a resting BUY_YES (LV T70) blocks a 2nd BUY_YES (LV B69.5)."""
        resting = [_resting("KXLOWTLV-26MAY25-T70", "KLAS", "2026-05-25", "BUY_YES")]
        cand = _cand("KXLOWTLV-26MAY25-B69.5")
        executed, reason = self._run(resting, cand, _decision("BUY_YES"))
        self.assertFalse(executed, f"resting order failed to block: {reason}")
        self.assertIn("position_cap", reason)

    def test_opposite_direction_resting_does_not_block(self):
        """1 YES + 1 NO per market is intended: a resting NO must not block YES."""
        resting = [_resting("KXLOWTLV-26MAY25-B63.5", "KLAS", "2026-05-25", "BUY_NO")]
        cand = _cand("KXLOWTLV-26MAY25-B69.5")
        executed, reason = self._run(resting, cand, _decision("BUY_YES"))
        self.assertTrue(executed, f"opposite-direction resting wrongly blocked: {reason}")
        self.assertNotIn("position_cap", reason)

    def test_prior_day_resting_does_not_block(self):
        """A resting order from a prior climate_day must not block today."""
        resting = [_resting("KXLOWTLV-26MAY24-B69.5", "KLAS", "2026-05-24", "BUY_YES")]
        cand = _cand("KXLOWTLV-26MAY25-B69.5")
        executed, reason = self._run(resting, cand, _decision("BUY_YES"))
        self.assertNotIn("position_cap", reason)

    def test_other_station_resting_does_not_block(self):
        """A resting order on a different station must not block."""
        resting = [_resting("KXLOWTLAX-26MAY25-B57.5", "KLAX", "2026-05-25", "BUY_YES")]
        cand = _cand("KXLOWTLV-26MAY25-B69.5")
        executed, reason = self._run(resting, cand, _decision("BUY_YES"))
        self.assertNotIn("position_cap", reason)

    def test_same_ticker_resting_not_counted_by_cap(self):
        """A resting order for the SAME ticker is handled by has_resting() in the
        LOW branch, not the cap -- it must not count toward n_existing (which
        would shadow the dedicated dedup). With has_resting mocked False here,
        the candidate proceeds past the cap."""
        resting = [_resting("KXLOWTLV-26MAY25-B69.5", "KLAS", "2026-05-25", "BUY_YES")]
        cand = _cand("KXLOWTLV-26MAY25-B69.5")
        executed, reason = self._run(resting, cand, _decision("BUY_YES"))
        self.assertNotIn("position_cap", reason)

    def test_filled_position_not_double_counted_with_resting(self):
        """If a ticker is both resting and already in positions (brief pre-sweep
        window), it must be counted once. A single same-direction filled position
        already trips the cap; a duplicate resting row for it must not matter."""
        nsw._rt = SimpleNamespace(
            positions={"KXLOWTLV-26MAY25-T70": {
                "station": "KLAS", "action": "BUY_YES", "cost": 0.93,
                "date_str": "2026-05-25"}},
            cycle_buys_by_station_side={})
        resting = [_resting("KXLOWTLV-26MAY25-T70", "KLAS", "2026-05-25", "BUY_YES")]
        cand = _cand("KXLOWTLV-26MAY25-B69.5")
        with ExitStack() as es:
            es.enter_context(mock.patch.object(config, "AUTO_EXEC_LOW_ENABLED", True))
            es.enter_context(mock.patch.object(config, "AUTO_EXECUTE_BUY_YES_PUSH", True))
            es.enter_context(mock.patch.object(config, "PUSH_LOW_POST_AT_MID", True))
            es.enter_context(mock.patch.object(
                config, "PUSH_MAX_TICKERS_PER_STATION_SIDE_DIRECTION", 1))
            es.enter_context(mock.patch.object(
                low_post_probe, "resting_rows", return_value=resting))
            es.enter_context(mock.patch.object(
                low_post_probe, "has_resting", return_value=False))
            es.enter_context(mock.patch.object(
                low_post_probe, "place", return_value=(True, "test-placed")))
            es.enter_context(mock.patch.object(
                kalshi_client, "get_balance_cached", return_value=100.0))
            executed, reason = nsw._try_auto_execute(
                cand, _packet(), _decision("BUY_YES"), series="LOW", local_hour=4.0)
        self.assertFalse(executed, f"filled position failed to block: {reason}")
        self.assertIn("position_cap", reason)
        self.assertIn("1>=1", reason)


if __name__ == "__main__":
    unittest.main()
