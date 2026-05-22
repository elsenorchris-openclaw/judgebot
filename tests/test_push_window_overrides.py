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
import types
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
        # disable the temp HIGH window override so these tests exercise the
        # underlying override-table logic (2026-05-21 PUSH_HIGH_TEMP_WINDOW ship)
        import config as _c
        self._tw = mock.patch.object(_c, "PUSH_HIGH_TEMP_WINDOW", None)
        self._tw.start()

    def tearDown(self):
        nsw._lookup_peak_hour = self._orig_lookup
        self._tw.stop()

    def test_override_applied_when_flag_on_and_cell_present(self):
        """KATL HIGH May has override; window should center near peak."""
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True):
                ok, dbg = nsw._in_decision_window("KATL", "HIGH", 15.2,
                                                  "2026-05-19")
        self.assertTrue(ok, dbg)
        self.assertIn("src=window_table", dbg)
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
        self.assertIn("src=window_table", dbg)

    def test_kill_switch_when_flag_off(self):
        """USE_PUSH_WINDOW_OVERRIDES=False → master kill-switch: no trade, no
        window, no alert (intentional, not a data gap)."""
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", False):
                ok, dbg = nsw._in_decision_window("KATL", "HIGH", 15.0,
                                                  "2026-05-19")
        self.assertFalse(ok, dbg)
        self.assertIn("push_window_system_disabled", dbg)

    def test_missing_cell_no_trade_and_alert(self):
        """Cell absent from window table → NOT traded + missing-window alert
        fires once (deduped). No default-window fallback."""
        import push_window_overrides as pwo
        nsw._lookup_peak_hour = lambda *a, **kw: 5.367  # KATL LOW May frac
        nsw._window_alert_seen.clear()
        captured = []
        fake_pjb = types.ModuleType("paper_judge_bot")
        fake_pjb.discord_send = lambda m: captured.append(m)
        with mock.patch.dict("sys.modules", {"paper_judge_bot": fake_pjb}):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True), \
                 mock.patch.object(pwo, "PUSH_WINDOW_OVERRIDES", {}):
                ok, dbg = nsw._in_decision_window("KATL", "LOW", 5.0, "2026-05-19")
                nsw._in_decision_window("KATL", "LOW", 5.0, "2026-05-19")  # dedup
        self.assertFalse(ok, dbg)
        self.assertIn("no_window_defined", dbg)
        self.assertEqual(len(captured), 1, captured)
        self.assertIn("PUSH WINDOW MISSING", captured[0])

    def test_override_month_resolved_from_climate_day(self):
        """Different month uses different override; lookup picks via climate_day."""
        nsw._lookup_peak_hour = lambda *a, **kw: 14.65  # mock Jan peak
        with mock.patch.dict("sys.modules"):
            import importlib
            cfg = importlib.import_module("config")
            with mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True):
                ok, dbg = nsw._in_decision_window("KATL", "HIGH", 14.0,
                                                  "2026-01-15")
        self.assertIn("src=window_table", dbg)
        # 13.0 should be in the ATL HIGH Jan window (range check)
        m = re.search(r"window=\[(-?[\d.]+),(-?[\d.]+)\]", dbg)
        lo, hi = float(m.group(1)), float(m.group(2))
        self.assertTrue(lo <= 14.0 <= hi, dbg)

    def test_dict_loads_and_has_expected_size(self):
        """PUSH_WINDOW_OVERRIDES has full 480/480 coverage and ATL HIGH 5 is a
        well-formed (before, after, bias, mae) 4-tuple.

        2026-05-21: format is now (before, after, bias, mae) — bias is the μ
        correction, mae the cell's expected pre-peak accuracy (°F). Bounds are
        physical-sanity only (before ∈ [-1, 4], after ∈ [-4, 1])."""
        from push_window_overrides import PUSH_WINDOW_OVERRIDES
        uncond = [k for k in PUSH_WINDOW_OVERRIDES if len(k) == 3]
        self.assertGreaterEqual(len(uncond), 480)
        self.assertIn(("KATL", "HIGH", 5), PUSH_WINDOW_OVERRIDES)
        ov = PUSH_WINDOW_OVERRIDES[("KATL", "HIGH", 5)]
        self.assertEqual(len(ov), 4, f"expected 4-tuple, got {ov}")
        b, a, bias, mae = ov
        # Physical-sanity window bounds
        self.assertTrue(-1.0 <= b <= 4.0, f"before {b} out of range")
        self.assertTrue(-4.0 <= a <= 1.0, f"after {a} out of range")
        self.assertGreaterEqual(b + a, 0.5, f"width {b + a} below min")
        # bias is a float; mae is a positive float (or None for fallback cells)
        self.assertIsInstance(bias, float)
        if mae is not None:
            self.assertGreater(mae, 0.0, f"mae {mae} should be positive")


if __name__ == "__main__":
    unittest.main()
