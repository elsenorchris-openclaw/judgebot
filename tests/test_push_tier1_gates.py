"""Tests for nn_shadow_worker._try_auto_execute Tier 1 runtime gates.

2026-05-20: Tier 1 gates skip the push pure-nn buy when wethr_obs reports a
physics-catastrophic regime the matcher cannot represent:

  - PUSH_MIN_VSBY_MI: dense fog / heavy precip (visibility < 0.5 mi)
  - PUSH_MAX_WIND_MPH: tropical / severe wind (sustained or gust > 40 mph)

These tests pin the gate ordering (gates fire after window check but before
price check) and verify each threshold can be flipped via config.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402


def _make_candidate():
    return market_universe.Candidate(
        ticker="KXHIGHTATL-26MAY20-B86.5",
        series_prefix="KXHIGH",
        city_code="TATL",
        station="KATL",
        climate_day="2026-05-20",
        bracket_kind="B",
        floor=86.0,
        cap=87.0,
        bracket_label=86.5,
        market={},
    )


def _make_packet(wethr_obs):
    return {
        "yes_ask_c": 50,
        "no_ask_c": 50,
        "seconds_to_close": 10_000,
        "wethr_obs": wethr_obs,
    }


def _make_decision():
    return {
        "decision": "BUY_NO",
        "edge": 0.20,
        "p_yes": 0.30,
        "reason": "BUY_NO test edge=20.0pp",
    }


class TestPushTier1Gates(unittest.TestCase):

    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})
        # Pin defaults so tests don't depend on prod tunables drifting.
        self._orig_min_vsby = getattr(config, "PUSH_MIN_VSBY_MI", None)
        self._orig_max_wind = getattr(config, "PUSH_MAX_WIND_MPH", None)
        config.PUSH_MIN_VSBY_MI = 0.5
        config.PUSH_MAX_WIND_MPH = 40.0
        # Enable the push toggle for BUY_NO so we reach the Tier 1 gates.
        self._orig_toggle = getattr(config, "AUTO_EXECUTE_BUY_NO_PUSH", None)
        config.AUTO_EXECUTE_BUY_NO_PUSH = True

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window
        if self._orig_min_vsby is not None:
            config.PUSH_MIN_VSBY_MI = self._orig_min_vsby
        if self._orig_max_wind is not None:
            config.PUSH_MAX_WIND_MPH = self._orig_max_wind
        if self._orig_toggle is not None:
            config.AUTO_EXECUTE_BUY_NO_PUSH = self._orig_toggle

    def _run(self, wethr_obs):
        import paper_judge_bot as pjb
        import kalshi_client
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(kalshi_client, "get_balance_cached",
                               return_value=100.0):
            return nsw._try_auto_execute(
                _make_candidate(), _make_packet(wethr_obs), _make_decision(),
                series="HIGH", local_hour=15.0,
            )

    # ── Visibility gate ────────────────────────────────────────────────
    def test_vsby_below_floor_blocks_buy(self):
        executed, reason = self._run({"visibility_miles": 0.25,
                                      "wind_speed_mph": 5.0})
        self.assertFalse(executed)
        self.assertIn("tier1_vsby", reason)

    def test_vsby_above_floor_passes(self):
        executed, reason = self._run({"visibility_miles": 10.0,
                                      "wind_speed_mph": 5.0})
        self.assertNotIn("tier1_vsby", reason)
        self.assertNotIn("tier1_wind", reason)

    def test_vsby_fallback_field(self):
        # Some wethr snapshots use 'visibility' instead of 'visibility_miles'.
        executed, reason = self._run({"visibility": 0.1,
                                      "wind_speed_mph": 5.0})
        self.assertFalse(executed)
        self.assertIn("tier1_vsby", reason)

    def test_vsby_missing_does_not_block(self):
        # If wethr doesn't report visibility, don't block — fail open.
        executed, reason = self._run({"wind_speed_mph": 5.0})
        self.assertNotIn("tier1_vsby", reason)

    # ── Wind gate ──────────────────────────────────────────────────────
    def test_sustained_wind_above_ceiling_blocks(self):
        executed, reason = self._run({"visibility_miles": 10.0,
                                      "wind_speed_mph": 45.0})
        self.assertFalse(executed)
        self.assertIn("tier1_wind", reason)
        self.assertIn("wind_speed_mph", reason)

    def test_gust_above_ceiling_blocks_even_with_calm_sustained(self):
        executed, reason = self._run({"visibility_miles": 10.0,
                                      "wind_speed_mph": 12.0,
                                      "wind_gust_mph": 55.0})
        self.assertFalse(executed)
        self.assertIn("tier1_wind", reason)
        self.assertIn("wind_gust_mph", reason)

    def test_wind_at_ceiling_does_not_block(self):
        # Threshold is strict > 40, equality passes.
        executed, reason = self._run({"visibility_miles": 10.0,
                                      "wind_speed_mph": 40.0})
        self.assertNotIn("tier1_wind", reason)

    # ── Disable knobs ──────────────────────────────────────────────────
    def test_zero_min_vsby_disables_gate(self):
        config.PUSH_MIN_VSBY_MI = 0.0
        executed, reason = self._run({"visibility_miles": 0.1,
                                      "wind_speed_mph": 5.0})
        self.assertNotIn("tier1_vsby", reason)

    def test_zero_max_wind_disables_gate(self):
        config.PUSH_MAX_WIND_MPH = 0.0
        executed, reason = self._run({"visibility_miles": 10.0,
                                      "wind_speed_mph": 70.0})
        self.assertNotIn("tier1_wind", reason)


if __name__ == "__main__":
    unittest.main()
