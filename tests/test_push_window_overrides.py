"""Tests for per-(station, series, month) push-window overrides.

Verify _in_decision_window honors USE_PUSH_WINDOW_OVERRIDES + the override dict
and falls back cleanly when the cell is missing or the flag is off.

Override values are computed against the bot's integer peak (matching
nn_shadow_worker._lookup_peak_hour), so test mocks use integer peaks too.
"""
import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nn_shadow_worker as nsw  # noqa: E402


class TestPushWindowOverrides(unittest.TestCase):

    def setUp(self):
        # Make _lookup_peak_hour deterministic. ATL HIGH May integer peak
        # from pace_curves_v2.json is 15 LST (matches int(empirical_peak_hour_local)).
        self._orig_lookup = nsw._lookup_peak_hour
        nsw._lookup_peak_hour = lambda station, series, climate_day: 15

    def tearDown(self):
        nsw._lookup_peak_hour = self._orig_lookup

    def test_override_applied_when_flag_on_and_cell_present(self):
        """ATL HIGH May override = (2.0, 0.0); peak=15 -> window [13.0, 15.0].
        h=14 inside."""
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
        """h=17 LST outside ATL HIGH May override window [13.0, 15.0]."""
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True):
                ok, dbg = nsw._in_decision_window("KATL", "HIGH", 17.0,
                                                  "2026-05-19")
        self.assertFalse(ok, dbg)
        self.assertIn("src=override", dbg)

    def test_fallback_when_flag_off(self):
        """USE_PUSH_WINDOW_OVERRIDES=False → global PUSH_PEAK_HOURS_*.
        Peak=15 + global window [-1.0, +0.5] = [14.0, 15.5]."""
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", False):
                ok, dbg = nsw._in_decision_window("KATL", "HIGH", 13.5,
                                                  "2026-05-19")
        # 13.5 < 14.0 → outside global window
        self.assertFalse(ok, dbg)
        self.assertIn("src=global", dbg)
        self.assertIn("window=[14.0,15.5]", dbg)

    def test_fallback_when_cell_missing(self):
        """Robust to override-dict regen: monkey-patch PUSH_WINDOW_OVERRIDES
        to a dict that excludes our test cell, then verify fallback."""
        import push_window_overrides as pwo
        nsw._lookup_peak_hour = lambda *a, **kw: 5    # any LOW trough
        empty_dict: dict = {}
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True), \
                 mock.patch.object(pwo, "PUSH_WINDOW_OVERRIDES", empty_dict):
                ok, dbg = nsw._in_decision_window("KDEN", "LOW", 4.5,
                                                  "2026-05-15")
        # peak=5, global LOW window [-1.0, +0.5] = [4.0, 5.5]; h=4.5 inside
        self.assertTrue(ok, dbg)
        self.assertIn("src=global", dbg)

    def test_override_month_resolved_from_climate_day(self):
        """ATL HIGH Jan override differs from May (different bot peak)."""
        nsw._lookup_peak_hour = lambda *a, **kw: 14    # ATL HIGH Jan int peak
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True):
                ok, dbg = nsw._in_decision_window("KATL", "HIGH", 12.5,
                                                  "2026-01-15")
        # ATL HIGH Jan override (~ 3.0, -1.0) -> window roughly [11.0, 13.0]
        # h=12.5 should be inside (override values can shift slightly with regens)
        self.assertIn("src=override", dbg)
        # window range sanity
        import re
        m = re.search(r"window=\[(-?[\d.]+),(-?[\d.]+)\]", dbg)
        self.assertIsNotNone(m)
        lo, hi = float(m.group(1)), float(m.group(2))
        self.assertTrue(lo <= 12.5 <= hi, dbg)
        self.assertTrue(0.5 <= hi - lo <= 5.0, dbg)

    def test_dict_loads_and_has_expected_size(self):
        """Sanity: PUSH_WINDOW_OVERRIDES loads with at least 400 entries
        (current ~458 with 5000-day + integer-peak-aligned); ATL HIGH May present."""
        from push_window_overrides import PUSH_WINDOW_OVERRIDES
        self.assertGreaterEqual(len(PUSH_WINDOW_OVERRIDES), 400)
        self.assertIn(("KATL", "HIGH", 5), PUSH_WINDOW_OVERRIDES)
        b, a = PUSH_WINDOW_OVERRIDES[("KATL", "HIGH", 5)]
        # Sanity-range: before in [0.5, 4.5], after in [-2.0, 1.5] for HIGH
        self.assertTrue(0.5 <= b <= 4.5, f"before {b} out of HIGH range")
        self.assertTrue(-2.0 <= a <= 1.5, f"after {a} out of HIGH range")


if __name__ == "__main__":
    unittest.main()
