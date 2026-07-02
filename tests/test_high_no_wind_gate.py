"""Tests for the HIGH-NO stable-airmass wind gate (PUSH_HIGH_NO_MAX_WIND_MPH).

2026-06-30 (wind deep-dive): a HIGH BUY_NO is skipped when decision-time SUSTAINED
wind exceeds the ceiling. Real-fill evidence: ~100% of the bot's lifetime -$399 came
from fills with sustained wind > 8mph; wind<=7mph was +$153/64%WR, within-station
14/16, era-robust. This is the mirror of the LOW frontal gate on the HIGH-NO side.

Mirrors test_push_tier1_gates.py: a wethr-only packet (no mu_chosen) fails open past
the mu-based clearance/wont-reach gates and reaches the wind gate.
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


def _make_decision(direction="BUY_NO"):
    return {
        "decision": direction,
        "edge": 0.20,
        "p_yes": 0.30 if direction == "BUY_NO" else 0.70,
        "reason": f"{direction} test edge=20.0pp",
    }


class TestHighNoWindGate(unittest.TestCase):

    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})
        self._saved = {k: getattr(config, k, None) for k in
                       ("PUSH_HIGH_NO_MAX_WIND_MPH", "PUSH_MAX_WIND_MPH",
                        "PUSH_MIN_VSBY_MI", "AUTO_EXECUTE_BUY_NO_PUSH")}
        config.PUSH_HIGH_NO_MAX_WIND_MPH = 7.0
        config.PUSH_MAX_WIND_MPH = 40.0     # tier1 catch-all off in this range
        config.PUSH_MIN_VSBY_MI = 0.5
        config.AUTO_EXECUTE_BUY_NO_PUSH = True

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window
        for k, v in self._saved.items():
            if v is not None:
                setattr(config, k, v)

    def _run(self, wethr_obs, direction="BUY_NO"):
        import paper_judge_bot as pjb
        import kalshi_client
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(kalshi_client, "get_balance_cached", return_value=100.0):
            return nsw._try_auto_execute(
                _make_candidate(), _make_packet(wethr_obs), _make_decision(direction),
                series="HIGH", local_hour=15.0,
            )

    def test_windy_high_no_blocked(self):
        # 10mph sustained > 7 ceiling -> skip (the -$2.75/fill band).
        executed, reason = self._run({"visibility_miles": 10.0, "wind_speed_mph": 10.0})
        self.assertFalse(executed)
        self.assertIn("high_no_unstable_wind", reason)

    def test_calm_high_no_not_blocked_by_wind(self):
        # 5mph sustained <= 7 -> this gate does not fire (calm = +EV band).
        executed, reason = self._run({"visibility_miles": 10.0, "wind_speed_mph": 5.0})
        self.assertNotIn("high_no_unstable_wind", reason)

    def test_wind_at_ceiling_passes(self):
        # Strict > 7; exactly 7 is allowed.
        executed, reason = self._run({"visibility_miles": 10.0, "wind_speed_mph": 7.0})
        self.assertNotIn("high_no_unstable_wind", reason)

    def test_missing_wind_fails_open(self):
        # No sustained-wind reading -> do not block (house style, matches tier1).
        executed, reason = self._run({"visibility_miles": 10.0})
        self.assertNotIn("high_no_unstable_wind", reason)

    def test_zero_ceiling_disables_gate(self):
        config.PUSH_HIGH_NO_MAX_WIND_MPH = 0.0
        executed, reason = self._run({"visibility_miles": 10.0, "wind_speed_mph": 25.0})
        self.assertNotIn("high_no_unstable_wind", reason)

    def test_gate_is_no_only(self):
        # A BUY_YES in the same windy conditions must NOT trip the HIGH-NO wind gate.
        executed, reason = self._run({"visibility_miles": 10.0, "wind_speed_mph": 25.0},
                                     direction="BUY_YES")
        self.assertNotIn("high_no_unstable_wind", reason)


if __name__ == "__main__":
    unittest.main()
