"""Tests for per-(station, series, month) push-window overrides.

Verify _in_decision_window honors USE_PUSH_WINDOW_OVERRIDES + the override dict
and falls back cleanly when the cell is missing or the flag is off.
"""
import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nn_shadow_worker as nsw  # noqa: E402


class TestPushWindowOverrides(unittest.TestCase):

    def setUp(self):
        # Make _lookup_peak_hour deterministic. ATL HIGH May P50 peak hour
        # is 15.62 LST per the trace DB.
        self._orig_lookup = nsw._lookup_peak_hour
        nsw._lookup_peak_hour = lambda station, series, climate_day: 15.62

    def tearDown(self):
        nsw._lookup_peak_hour = self._orig_lookup

    def test_override_applied_when_flag_on_and_cell_present(self):
        """ATL HIGH May has override (before=2.62, after=-0.62).
        Window = [15.62-2.62, 15.62-0.62] = [13.0, 15.0]. h=14 inside.
        """
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True):
                ok, dbg = nsw._in_decision_window("KATL", "HIGH", 14.0,
                                                  "2026-05-19")
        self.assertTrue(ok, dbg)
        self.assertIn("src=override", dbg)
        self.assertIn("window=[13.0,15.0]", dbg)

    def test_override_excludes_outside_window(self):
        """h=17 LST is outside ATL HIGH May override window [13.0, 15.0]."""
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True):
                ok, dbg = nsw._in_decision_window("KATL", "HIGH", 17.0,
                                                  "2026-05-19")
        self.assertFalse(ok, dbg)
        self.assertIn("src=override", dbg)

    def test_fallback_when_flag_off(self):
        """USE_PUSH_WINDOW_OVERRIDES=False → use global PUSH_PEAK_HOURS_*.
        Global window for ATL May = [peak-1.0, peak+0.5] = [14.62, 16.12]."""
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", False):
                ok, dbg = nsw._in_decision_window("KATL", "HIGH", 14.0,
                                                  "2026-05-19")
        # 14.0 < 14.62 → outside global window
        self.assertFalse(ok, dbg)
        self.assertIn("src=global", dbg)
        self.assertIn("window=[14.6,16.1]", dbg)

    def test_fallback_when_cell_missing(self):
        """KDEN LOW May has no override (filtered: no_qualifying_window).
        Must fall back to global LOW window [peak-1.0, peak+0.5]."""
        nsw._lookup_peak_hour = lambda *a, **kw: 4.93   # DEN LOW May trough
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True):
                ok, dbg = nsw._in_decision_window("KDEN", "LOW", 4.5,
                                                  "2026-05-15")
        # h=4.5 inside global [3.93, 5.43] → True with src=global
        self.assertTrue(ok, dbg)
        self.assertIn("src=global", dbg)

    def test_override_month_resolved_from_climate_day(self):
        """ATL HIGH Jan override (before=2.65, after=-0.65) ≠ May override."""
        nsw._lookup_peak_hour = lambda *a, **kw: 14.65    # ATL HIGH Jan peak
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True):
                ok, dbg = nsw._in_decision_window("KATL", "HIGH", 12.5,
                                                  "2026-01-15")
        # peak 14.65, before=2.65, after=-0.65 → [12.0, 14.0]. h=12.5 ok.
        self.assertTrue(ok, dbg)
        self.assertIn("src=override", dbg)
        self.assertIn("window=[12.0,14.0]", dbg)

    def test_dict_loads_and_has_expected_size(self):
        """Sanity: PUSH_WINDOW_OVERRIDES exists and covers 394 cells."""
        from push_window_overrides import PUSH_WINDOW_OVERRIDES
        self.assertGreaterEqual(len(PUSH_WINDOW_OVERRIDES), 380)
        # ATL HIGH May is one of the well-formed cells
        self.assertIn(("KATL", "HIGH", 5), PUSH_WINDOW_OVERRIDES)
        b, a = PUSH_WINDOW_OVERRIDES[("KATL", "HIGH", 5)]
        self.assertAlmostEqual(b, 2.62, places=2)
        self.assertAlmostEqual(a, -0.62, places=2)
        # KDEN LOW May is one of the gaps (filtered: no_qualifying_window)
        self.assertNotIn(("KDEN", "LOW", 5), PUSH_WINDOW_OVERRIDES)


if __name__ == "__main__":
    unittest.main()
