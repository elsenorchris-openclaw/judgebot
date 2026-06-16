"""Tests for the 2026-06-16 per-day HIGH exposure cap (PUSH_MAX_HIGH_FILLS_PER_DAY):
caps TOTAL HIGH fills/day across stations to limit correlated-forecast-miss-day
over-exposure. Distinct from the per-station one-bracket cap.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402


def _cand(station, tk):
    return market_universe.Candidate(
        ticker=tk, series_prefix="KXHIGH", city_code="T", station=station,
        climate_day="2026-06-16", bracket_kind="B", floor=88.0, cap=89.0,
        bracket_label=88.5, market={})


def _pkt(mu=91.0):
    return {"days_out": 0, "running_min_or_max": 80.0, "floor": 88.0, "cap": 89.0,
            "no_ask_c": 60, "yes_bid_c": 38, "yes_ask_c": 42, "mu_chosen": mu,
            "sigma_chosen": 1.17, "mu_method": "blend_KXHIGH", "mu_pre_blend": 90.0,
            "seconds_to_close": 10_000, "wethr_obs": {}, "local_clock": {"h_to_peak": 3.0}}


def _dec(): return {"decision": "BUY_NO", "edge": 0.25, "p_yes": 0.20, "reason": "t"}


def _positions(n_high_today):
    """n filled HIGH positions on the cap's climate_day, distinct stations."""
    pos = {}
    for i in range(n_high_today):
        pos[f"KXHIGHX{i}-26JUN16-B88.5"] = {"cost": 5.0, "station": f"KS{i}",
            "action": "BUY_NO", "date_str": "2026-06-16"}
    return pos


class TestHighDayCap(unittest.TestCase):
    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_win = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **k: (True, "w")
        nsw._pending_buys.clear()

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_win
        nsw._pending_buys.clear()

    def _run(self, n_high_today, cap=3):
        import paper_judge_bot as pjb
        import kalshi_client
        import config as _cfg
        nsw._rt = SimpleNamespace(positions=_positions(n_high_today),
                                  cycle_buys_by_station_side={})
        with mock.patch.object(pjb, "execute_buy", lambda *a, **k: None), \
             mock.patch.object(kalshi_client, "get_balance_cached", return_value=100.0), \
             mock.patch.object(_cfg, "PUSH_MAE_GATE_ENABLED", False), \
             mock.patch.object(_cfg, "PUSH_ONE_BRACKET_PER_STATION_HIGH", False), \
             mock.patch.object(_cfg, "PUSH_MAX_HIGH_FILLS_PER_DAY", cap):
            return nsw._try_auto_execute(
                _cand("KNEW", "KXHIGHNEW-26JUN16-B88.5"), _pkt(), _dec(),
                series="HIGH", local_hour=12.0)

    def test_under_cap_allows(self):
        ok, reason = self._run(n_high_today=2, cap=3)   # 2 today + this = 3, under>=? 2<3 ok
        self.assertTrue(ok, reason)

    def test_at_cap_blocks(self):
        ok, reason = self._run(n_high_today=3, cap=3)
        self.assertFalse(ok)
        self.assertIn("high_day_cap", reason)

    def test_cap_zero_disables(self):
        ok, reason = self._run(n_high_today=9, cap=0)
        self.assertNotIn("high_day_cap", reason)
        self.assertTrue(ok, reason)

    def test_other_day_positions_dont_count(self):
        # positions from a different climate_day must not count toward today's cap
        import paper_judge_bot as pjb
        import kalshi_client
        import config as _cfg
        pos = {f"KXHIGHX{i}-26JUN15-B88.5": {"cost": 5.0, "station": f"KS{i}",
               "action": "BUY_NO", "date_str": "2026-06-15"} for i in range(5)}
        nsw._rt = SimpleNamespace(positions=pos, cycle_buys_by_station_side={})
        with mock.patch.object(pjb, "execute_buy", lambda *a, **k: None), \
             mock.patch.object(kalshi_client, "get_balance_cached", return_value=100.0), \
             mock.patch.object(_cfg, "PUSH_MAE_GATE_ENABLED", False), \
             mock.patch.object(_cfg, "PUSH_ONE_BRACKET_PER_STATION_HIGH", False), \
             mock.patch.object(_cfg, "PUSH_MAX_HIGH_FILLS_PER_DAY", 3):
            ok, reason = nsw._try_auto_execute(
                _cand("KNEW", "KXHIGHNEW-26JUN16-B88.5"), _pkt(), _dec(),
                series="HIGH", local_hour=12.0)
        self.assertTrue(ok, reason)  # yesterday's 5 don't count toward today


if __name__ == "__main__":
    unittest.main()
