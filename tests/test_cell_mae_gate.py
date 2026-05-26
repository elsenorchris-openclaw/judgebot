"""Tests for nn_shadow_worker._try_auto_execute per-cell MAE reliability gate.

2026-05-25: skip a BUY when the matcher's historical MAE for the
(station, season, local_hour, side) cell exceeds config.PUSH_MAE_GATE_F
(reason "cell_mae_gate"). MAE from cell_mae_table.CELL_MAE (2022-2025 OOS).
Fail-OPEN: unknown cell -> not gated. Backtest n=315: +$23 realized, both
date-halves. Reversible: PUSH_MAE_GATE_ENABLED=False.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402
import cell_mae_table  # noqa: E402


def _make_candidate(ticker, station, series_prefix, climate_day,
                    bracket_kind="B", floor=88.0, cap=89.0, label=88.5):
    return market_universe.Candidate(
        ticker=ticker, series_prefix=series_prefix, city_code="TEST",
        station=station, climate_day=climate_day, bracket_kind=bracket_kind,
        floor=floor, cap=cap, bracket_label=label, market={},
    )


def _make_packet(mu=92.0, floor=88.0, cap=89.0):
    return {
        "yes_ask_c": 50, "no_ask_c": 50, "seconds_to_close": 10_000,
        "mu_chosen": mu, "floor": floor, "cap": cap,
        "local_clock": {"h_to_peak": 2.0},
    }


def _make_decision(direction="BUY_NO"):
    return {
        "decision": direction, "edge": 0.20,
        "p_yes": 0.30 if direction == "BUY_NO" else 0.70,
        "reason": f"{direction} test edge=20.0pp",
    }


class TestCellMaeGate(unittest.TestCase):

    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window

    def _run(self, cand, packet, decision, series="HIGH", local_hour=12.0):
        import paper_judge_bot as pjb
        import kalshi_client
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(kalshi_client, "get_balance_cached",
                               return_value=100.0):
            return nsw._try_auto_execute(
                cand, packet, decision, series=series, local_hour=local_hour,
            )

    def test_high_mae_cell_blocked(self):
        """A cell with MAE > threshold is gated. Patch the table so the test is
        independent of live values."""
        import config
        cand = _make_candidate("KXHIGHTEST-26MAY20-B88.5", "KTEST",
                               "KXHIGH", "2026-05-20")
        with mock.patch.dict(cell_mae_table.CELL_MAE,
                             {("TEST", "MAM", 12, "high"): 3.5}, clear=False), \
             mock.patch.object(config, "PUSH_MAE_GATE_ENABLED", True), \
             mock.patch.object(config, "PUSH_MAE_GATE_F", 2.0):
            executed, reason = self._run(cand, _make_packet(), _make_decision(),
                                         local_hour=12.0)
        self.assertFalse(executed)
        self.assertIn("cell_mae_gate", reason)

    def test_low_mae_cell_not_blocked(self):
        """A reliable cell (MAE < threshold) is not gated by this rule."""
        import config
        cand = _make_candidate("KXHIGHTEST-26MAY20-B88.5", "KTEST",
                               "KXHIGH", "2026-05-20")
        with mock.patch.dict(cell_mae_table.CELL_MAE,
                             {("TEST", "MAM", 18, "high"): 0.6}, clear=False), \
             mock.patch.object(config, "PUSH_MAE_GATE_ENABLED", True), \
             mock.patch.object(config, "PUSH_MAE_GATE_F", 2.0):
            executed, reason = self._run(cand, _make_packet(), _make_decision(),
                                         local_hour=18.0)
        self.assertNotIn("cell_mae_gate", reason)

    def test_unknown_cell_fail_open(self):
        """An unknown cell (not in table) is NOT gated (fail-open)."""
        import config
        cand = _make_candidate("KXHIGHZZZ-26MAY20-B88.5", "KZZZ",
                               "KXHIGH", "2026-05-20")
        with mock.patch.object(config, "PUSH_MAE_GATE_ENABLED", True), \
             mock.patch.object(config, "PUSH_MAE_GATE_F", 2.0):
            executed, reason = self._run(cand, _make_packet(), _make_decision(),
                                         local_hour=12.0)
        self.assertNotIn("cell_mae_gate", reason)

    def test_disabled_flag_no_gate(self):
        """PUSH_MAE_GATE_ENABLED=False -> gate never fires even on a bad cell."""
        import config
        cand = _make_candidate("KXHIGHTEST-26MAY20-B88.5", "KTEST",
                               "KXHIGH", "2026-05-20")
        with mock.patch.dict(cell_mae_table.CELL_MAE,
                             {("TEST", "MAM", 12, "high"): 5.0}, clear=False), \
             mock.patch.object(config, "PUSH_MAE_GATE_ENABLED", False):
            executed, reason = self._run(cand, _make_packet(), _make_decision(),
                                         local_hour=12.0)
        self.assertNotIn("cell_mae_gate", reason)

    def test_threshold_boundary(self):
        """MAE exactly at threshold is NOT gated (strict >)."""
        import config
        cand = _make_candidate("KXHIGHTEST-26MAY20-B88.5", "KTEST",
                               "KXHIGH", "2026-05-20")
        with mock.patch.dict(cell_mae_table.CELL_MAE,
                             {("TEST", "MAM", 12, "high"): 2.0}, clear=False), \
             mock.patch.object(config, "PUSH_MAE_GATE_ENABLED", True), \
             mock.patch.object(config, "PUSH_MAE_GATE_F", 2.0):
            executed, reason = self._run(cand, _make_packet(), _make_decision(),
                                         local_hour=12.0)
        self.assertNotIn("cell_mae_gate", reason)

    def test_table_lookup_strips_k_and_maps_season(self):
        """cell_mae lookup: K-prefix strip + month->season mapping."""
        with mock.patch.dict(cell_mae_table.CELL_MAE,
                             {("ABC", "JJA", 15, "low"): 1.23}, clear=False):
            self.assertEqual(cell_mae_table.cell_mae("KABC", 7, 15.4, "low"), 1.23)
            self.assertEqual(cell_mae_table.cell_mae("ABC", 7, 15.0, "low"), 1.23)
            self.assertIsNone(cell_mae_table.cell_mae("KABC", 1, 15, "low"))  # DJF


if __name__ == "__main__":
    unittest.main()
