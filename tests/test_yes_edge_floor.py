"""Side-specific YES edge floor (PUSH_MIN_EDGE_PP_YES) — 2026-05-28.

NO uses PUSH_MIN_EDGE_PP (18 on judge); YES uses PUSH_MIN_EDGE_PP_YES (12).
Pooled real-fill YES (n=63, 05-14..28) showed the 12-18pp YES band is +EV
(67% WR, +17.9c/contract, n=12) while the 12-18pp NO band bleeds (-5.7c/bet,
n=867) — so the floor is split by side. Pins: a 14pp YES clears the floor, a
14pp NO is still blocked at 18.
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


def _cand():
    return market_universe.Candidate(
        ticker="KXHIGHPHIL-26MAY20-B95.5", series_prefix="KXHIGH",
        city_code="PHIL", station="KPHL", climate_day="2026-05-20",
        bracket_kind="B", floor=95.0, cap=96.0, bracket_label=95.5, market={})


def _packet(mu):
    return {"yes_ask_c": 50, "no_ask_c": 50, "seconds_to_close": 10_000,
            "wethr_obs": {"visibility_miles": 10.0, "wind_speed_mph": 5.0},
            "mu_chosen": mu, "floor": 95.0, "cap": 96.0, "bracket_kind": "B",
            "local_clock": {"h_to_peak": 2.0}}


def _dec(direction, edge, p_yes):
    return {"decision": direction, "edge": edge, "p_yes": p_yes,
            "reason": f"{direction} {edge*100:.0f}pp"}


class TestYesEdgeFloor(unittest.TestCase):
    def setUp(self):
        self._o = {k: getattr(config, k, None) for k in (
            "PUSH_MIN_EDGE_PP", "PUSH_MIN_EDGE_PP_YES", "PUSH_TAIL_BET_MIN_EDGE_PP",
            "AUTO_EXECUTE_BUY_NO_PUSH", "AUTO_EXECUTE_BUY_YES_PUSH")}
        config.PUSH_MIN_EDGE_PP = 18
        config.PUSH_MIN_EDGE_PP_YES = 12
        config.PUSH_TAIL_BET_MIN_EDGE_PP = 25
        config.AUTO_EXECUTE_BUY_NO_PUSH = True
        config.AUTO_EXECUTE_BUY_YES_PUSH = True
        self._rt = nsw._rt
        self._win = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **k: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})

    def tearDown(self):
        nsw._rt = self._rt
        nsw._in_decision_window = self._win
        for k, v in self._o.items():
            if v is not None:
                setattr(config, k, v)

    def _run(self, packet, decision):
        import paper_judge_bot as pjb
        import kalshi_client
        with mock.patch.object(pjb, "execute_buy", lambda *a, **k: None), \
             mock.patch.object(kalshi_client, "get_balance_cached", return_value=100.0):
            return nsw._try_auto_execute(_cand(), packet, decision,
                                         series="HIGH", local_hour=12.0)

    def test_yes_14pp_clears_floor(self):
        # mu=88 is OUTSIDE the YES window [94.5,96.5) -> tail-bet gate inert;
        # YES floor=12 -> 14pp clears (not edge-blocked).
        _ex, reason = self._run(_packet(88.0), _dec("BUY_YES", 0.14, 0.70))
        self.assertNotIn("edge_below_floor", reason)

    def test_no_14pp_blocked_at_18(self):
        # Same 14pp edge but NO side -> floor stays 18 -> blocked.
        ex, reason = self._run(_packet(88.0), _dec("BUY_NO", 0.14, 0.30))
        self.assertFalse(ex)
        self.assertIn("edge_below_floor", reason)
        self.assertIn("18pp", reason)


if __name__ == "__main__":
    unittest.main()
