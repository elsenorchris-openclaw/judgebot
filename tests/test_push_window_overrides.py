"""Tests for per-(station, series, month) push-window overrides.

Verify _in_decision_window honors USE_PUSH_WINDOW_OVERRIDES + the override dict
and falls back cleanly when the cell is missing or the flag is off.

2026-05-20: overrides are now FRAC-aligned. Tests mock frac peak (matches
the live USE_FRACTIONAL_PEAK_FOR_WINDOW=True behavior). Expected windows
derived from override values + mocked frac peak directly, not hardcoded.
"""
import sys
import os
import unittest
import re
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nn_shadow_worker as nsw  # noqa: E402


class TestPushWindowOverrides(unittest.TestCase):

    def setUp(self):
        # Mock to a known frac peak (matches the live config which uses
        # fractional peaks). KATL HIGH May frac peak from
        # peak_fractional_5yr_10day.json ≈ 15.883 for May 19.
        self._orig_lookup = nsw._lookup_peak_hour
        nsw._lookup_peak_hour = lambda station, series, climate_day: 15.883

    def tearDown(self):
        nsw._lookup_peak_hour = self._orig_lookup

    def test_override_applied_when_flag_on_and_cell_present(self):
        """KATL HIGH May has override; window should center near peak."""
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True):
                ok, dbg = nsw._in_decision_window("KATL", "HIGH", 15.2,
                                                  "2026-05-19")
        self.assertTrue(ok, dbg)
        self.assertIn("src=override", dbg)
        # 14.0 should be inside whatever the override window is for ATL HIGH May
        m = re.search(r"window=\[(-?[\d.]+),(-?[\d.]+)\]", dbg)
        self.assertIsNotNone(m)
        lo, hi = float(m.group(1)), float(m.group(2))
        self.assertTrue(lo <= 15.2 <= hi, dbg)
        # Sanity: window width 1-4h, opens within 3.5h before peak
        self.assertTrue(0.5 <= hi - lo <= 4.5, f"width {hi-lo}: {dbg}")
        self.assertTrue(11.0 <= lo <= 16.0, f"lo {lo}: {dbg}")

    def test_override_excludes_far_after_peak(self):
        """Peak + 2h should be outside even the most generous HIGH override."""
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True):
                ok, dbg = nsw._in_decision_window("KATL", "HIGH", 18.0,
                                                  "2026-05-19")
        self.assertFalse(ok, dbg)
        self.assertIn("src=override", dbg)

    def test_fallback_when_flag_off(self):
        """USE_PUSH_WINDOW_OVERRIDES=False → global PUSH_PEAK_HOURS_*.
        peak=15.883, before=1.0, after=0.5 → window [14.883, 16.383]."""
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", False):
                ok, dbg = nsw._in_decision_window("KATL", "HIGH", 13.5,
                                                  "2026-05-19")
        # 14.0 < 14.883 → outside global window
        self.assertFalse(ok, dbg)
        self.assertIn("src=global", dbg)

    def test_fallback_when_cell_missing(self):
        """Robust to override-dict regen: monkey-patch PUSH_WINDOW_OVERRIDES
        to empty dict, then verify fallback."""
        import push_window_overrides as pwo
        nsw._lookup_peak_hour = lambda *a, **kw: 5.367  # KATL LOW May frac
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True), \
                 mock.patch.object(pwo, "PUSH_WINDOW_OVERRIDES", {}):
                ok, dbg = nsw._in_decision_window("KATL", "LOW", 5.0,
                                                  "2026-05-19")
        # peak=5.367, global LOW window [-1.0, +0.5] = [4.367, 5.867]; h=5.0 inside
        self.assertTrue(ok, dbg)
        self.assertIn("src=global", dbg)

    def test_override_month_resolved_from_climate_day(self):
        """Different month uses different override; lookup picks via climate_day."""
        nsw._lookup_peak_hour = lambda *a, **kw: 14.65  # mock Jan peak
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True):
                ok, dbg = nsw._in_decision_window("KATL", "HIGH", 14.0,
                                                  "2026-01-15")
        self.assertIn("src=override", dbg)
        # 13.0 should be in the ATL HIGH Jan window (range check)
        m = re.search(r"window=\[(-?[\d.]+),(-?[\d.]+)\]", dbg)
        lo, hi = float(m.group(1)), float(m.group(2))
        self.assertTrue(lo <= 14.0 <= hi, dbg)

    def test_dict_loads_and_has_expected_size(self):
        """PUSH_WINDOW_OVERRIDES has ~460 cells (frac-aligned) and ATL HIGH 5
        is well-formed with peak-relative offsets."""
        from push_window_overrides import PUSH_WINDOW_OVERRIDES
        self.assertGreaterEqual(len(PUSH_WINDOW_OVERRIDES), 400)
        self.assertIn(("KATL", "HIGH", 5), PUSH_WINDOW_OVERRIDES)
        b, a = PUSH_WINDOW_OVERRIDES[("KATL", "HIGH", 5)]
        # Sanity: before in [0, 5.5], after in [-3.0, 1.5] (HIGH bounds)
        self.assertTrue(0.0 <= b <= 5.5, f"before {b} out of HIGH range")
        self.assertTrue(-3.0 <= a <= 1.5, f"after {a} out of HIGH range")


if __name__ == "__main__":
    unittest.main()
