"""Tests for the 2026-06-10 HIGH B-bracket won't-reach NO veto
(PUSH_HIGH_NO_SKIP_WONT_REACH): skip BUY_NO when mu < floor-0.5 -- the
no-obs-support "heat falls short" bet (-32.3c/ct, negative all 4 splits).
T-tail NOs are unaffected.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402


def _cand(kind="B", floor=88.0, cap=89.0):
    return market_universe.Candidate(
        ticker="KXHIGHTEST-26JUN10-B88.5", series_prefix="KXHIGH", city_code="TEST",
        station="KDFW", climate_day="2026-06-10", bracket_kind=kind,
        floor=floor, cap=cap, bracket_label=88.5, market={},
    )


def _pkt(mu, floor=88.0, cap=89.0):
    return {"days_out": 0, "running_min_or_max": 80.0, "floor": floor, "cap": cap,
            "no_ask_c": 60, "yes_bid_c": 38, "yes_ask_c": 42,
            "mu_chosen": mu, "sigma_chosen": 1.17, "mu_method": "blend_KXHIGH",
            "mu_pre_blend": 85.0,  # blend sigma-floor exemption
            "seconds_to_close": 10_000, "wethr_obs": {},
            "local_clock": {"h_to_peak": 3.0}}


def _decision(edge=0.25):
    return {"decision": "BUY_NO", "edge": edge, "p_yes": 0.20, "reason": "BUY_NO test"}


class TestWontReachGate(unittest.TestCase):
    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window

    def _run(self, cand, packet, enabled=True):
        import paper_judge_bot as pjb
        import kalshi_client
        import config as _cfg
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(kalshi_client, "get_balance_cached", return_value=100.0), \
             mock.patch.object(_cfg, "PUSH_MAE_GATE_ENABLED", False), \
             mock.patch.object(_cfg, "PUSH_ONE_BRACKET_PER_STATION_HIGH", False), \
             mock.patch.object(_cfg, "PUSH_HIGH_NO_SKIP_WONT_REACH", enabled):
            return nsw._try_auto_execute(
                cand, packet, _decision(), series="HIGH", local_hour=12.0)

    def test_wont_reach_blocked(self):
        # mu 85.0 vs bracket [88,89]: betting the heat falls short -> blocked
        ok, reason = self._run(_cand(), _pkt(mu=85.0))
        self.assertFalse(ok)
        self.assertIn("wont_reach_no", reason)

    def test_blows_past_passes(self):
        # mu 91.0 above cap+0.5: the heat blows past this bracket -> trades
        ok, reason = self._run(_cand(), _pkt(mu=91.0))
        self.assertTrue(ok, reason)

    def test_t_tail_unaffected(self):
        # T-cold (cap-only) bracket: gate requires BOTH floor and cap -> not blocked
        cand = _cand(kind="T", floor=None, cap=89.0)
        ok, reason = self._run(cand, _pkt(mu=80.0, floor=None, cap=89.0))
        self.assertNotIn("wont_reach_no", reason)

    def test_flag_off_legacy(self):
        ok, reason = self._run(_cand(), _pkt(mu=85.0), enabled=False)
        self.assertNotIn("wont_reach_no", reason)


if __name__ == "__main__":
    unittest.main()
