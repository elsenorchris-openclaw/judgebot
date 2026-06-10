"""Tests for the 2026-06-10 LOW P(NO) floor (PUSH_LOW_MIN_PNO):
LOW BUY_NO requires the model itself to lean NO (P(NO) >= floor) so a cheap
ask can't manufacture edge off a ~coinflip forecast (the DAL 6/10 loss shape).
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402
import low_post_probe  # noqa: E402


def _cand():
    return market_universe.Candidate(
        ticker="KXLOWTDAL-26JUN10-B77.5", series_prefix="KXLOW", city_code="DAL",
        station="KDFW", climate_day="2026-06-10", bracket_kind="B",
        floor=77.0, cap=78.0, bracket_label=77.5, market={},
    )


def _pkt(no_ask=60, yes_bid=38, yes_ask=42):
    return {"days_out": 0, "running_min_or_max": 80.0, "floor": 77.0, "cap": 78.0,
            "no_ask_c": no_ask, "yes_bid_c": yes_bid, "yes_ask_c": yes_ask,
            "mu_chosen": 80.0, "sigma_chosen": 1.51, "mu_method": "blend_KXLOW",
            "seconds_to_close": 10_000, "wethr_obs": {},
            "local_clock": {"h_to_peak": 2.0}}


def _decision(p_yes, edge=0.20):
    return {"decision": "BUY_NO", "edge": edge, "p_yes": p_yes,
            "reason": "BUY_NO test"}


class TestLowPnoFloor(unittest.TestCase):
    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window

    def _run(self, decision, pno_min=0.55, packet=None):
        import paper_judge_bot as pjb
        import kalshi_client
        import config as _cfg
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(low_post_probe, "has_resting", lambda *a, **kw: False), \
             mock.patch.object(low_post_probe, "place",
                               lambda *a, **kw: (True, "posted_mid_test")), \
             mock.patch.object(kalshi_client, "get_balance_cached", return_value=100.0), \
             mock.patch.object(_cfg, "PUSH_MAE_GATE_ENABLED", False), \
             mock.patch.object(_cfg, "PUSH_LOW_MIN_PNO", pno_min):
            return nsw._try_auto_execute(
                _cand(), packet or _pkt(), decision, series="LOW", local_hour=4.0)

    def test_coinflip_pno_blocked(self):
        # P(NO)=0.52 -- the DAL shape: cheap-ask edge off a coinflip model
        ok, reason = self._run(_decision(p_yes=0.48))
        self.assertFalse(ok)
        self.assertIn("low_pno_floor", reason)

    def test_confident_pno_passes(self):
        ok, reason = self._run(_decision(p_yes=0.07))
        self.assertTrue(ok, reason)

    def test_floor_zero_disables(self):
        ok, reason = self._run(_decision(p_yes=0.48), pno_min=0.0)
        self.assertNotIn("low_pno_floor", reason)


if __name__ == "__main__":
    unittest.main()
