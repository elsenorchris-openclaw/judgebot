"""Tests for the 2026-06-11 HIGH gates:
  - PUSH_HIGH_NO_MIN_CLEARANCE_F: blows-past B-NO must clear cap+0.5 by >= X F
    (the DEN-B76.5 thin-clearance loss shape; [0,0.5)F band = -7.8c/ct WR33).
  - PUSH_HIGH_T_NO_MIN_PNO: T-tail NO requires model P(NO) >= floor
    (the CHI-T86 coinflip-P cheap-ask shape).
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402


def _cand(kind="B", floor=76.0, cap=77.0, tk="KXHIGHDEN-26JUN11-B76.5"):
    return market_universe.Candidate(
        ticker=tk, series_prefix="KXHIGH", city_code="TEST",
        station="KDFW", climate_day="2026-06-11", bracket_kind=kind,
        floor=floor, cap=cap, bracket_label=76.5, market={},
    )


def _pkt(mu, floor=76.0, cap=77.0):
    return {"days_out": 0, "running_min_or_max": 70.0, "floor": floor, "cap": cap,
            "no_ask_c": 60, "yes_bid_c": 38, "yes_ask_c": 42,
            "mu_chosen": mu, "sigma_chosen": 1.17, "mu_method": "blend_KXHIGH",
            "mu_pre_blend": 75.0,
            "seconds_to_close": 10_000, "wethr_obs": {},
            "local_clock": {"h_to_peak": 3.0}}


def _decision(p_yes=0.20, edge=0.25):
    return {"decision": "BUY_NO", "edge": edge, "p_yes": p_yes, "reason": "BUY_NO test"}


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window

    def _run(self, cand, packet, decision, clearance=1.0, tpno=0.60):
        import paper_judge_bot as pjb
        import kalshi_client
        import config as _cfg
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(kalshi_client, "get_balance_cached", return_value=100.0), \
             mock.patch.object(_cfg, "PUSH_MAE_GATE_ENABLED", False), \
             mock.patch.object(_cfg, "PUSH_ONE_BRACKET_PER_STATION_HIGH", False), \
             mock.patch.object(_cfg, "PUSH_HIGH_NO_MIN_CLEARANCE_F", clearance), \
             mock.patch.object(_cfg, "PUSH_HIGH_T_NO_MIN_PNO", tpno):
            return nsw._try_auto_execute(
                cand, packet, decision, series="HIGH", local_hour=12.0)


class TestClearanceFloor(_Base):
    def test_thin_clearance_blocked(self):
        # DEN 6/11 shape: mu 77.8 vs cap+0.5=77.5 -> clearance +0.30F < 1.0F
        ok, reason = self._run(_cand(), _pkt(mu=77.8), _decision())
        self.assertFalse(ok)
        self.assertIn("bp_clearance", reason)

    def test_full_degree_clearance_passes(self):
        ok, reason = self._run(_cand(), _pkt(mu=78.6), _decision())
        self.assertTrue(ok, reason)

    def test_clearance_zero_disables(self):
        ok, reason = self._run(_cand(), _pkt(mu=77.8), _decision(), clearance=0.0)
        self.assertNotIn("bp_clearance", reason)


class TestTtailPnoFloor(_Base):
    def test_coinflip_t_no_blocked(self):
        # CHI 6/11 shape: T-cold (cap-only), P(NO)=0.53
        cand = _cand(kind="T", floor=None, cap=86.0, tk="KXHIGHCHI-26JUN11-T86")
        ok, reason = self._run(cand, _pkt(mu=85.6, floor=None, cap=86.0),
                               _decision(p_yes=0.47))
        self.assertFalse(ok)
        self.assertIn("t_pno_floor", reason)

    def test_confident_t_no_passes(self):
        cand = _cand(kind="T", floor=None, cap=86.0, tk="KXHIGHCHI-26JUN11-T86")
        ok, reason = self._run(cand, _pkt(mu=89.0, floor=None, cap=86.0),
                               _decision(p_yes=0.10))
        self.assertTrue(ok, reason)

    def test_b_bracket_not_t_gated(self):
        # B bracket with low P(NO) hits the clearance gate, not the T floor
        ok, reason = self._run(_cand(), _pkt(mu=77.8), _decision(p_yes=0.47))
        self.assertNotIn("t_pno_floor", reason)


if __name__ == "__main__":
    unittest.main()
