"""Tests for the HIGH early-side window trim in _in_decision_window.

2026-05-21: HIGH accurate-but-wide cells (mae < PUSH_EARLY_TRIM_MAE_MAX,
before > PUSH_EARLY_TRIM_BEFORE_CAP) have their `before` capped so they
don't open >1h before peak. Validated 2024-2025 holdout: early offsets
mis-call the bracket 60% / miss >=2F 32% vs 46%/16% near peak. `after`,
peak time, inaccurate wide cells, and LOW cells are untouched.
"""
import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nn_shadow_worker as nsw  # noqa
import config as cfg  # noqa


class TestEarlyTrim(unittest.TestCase):
    def setUp(self):
        self._orig = nsw._lookup_peak_hour
        nsw._lookup_peak_hour = lambda station, series, climate_day: 14.0  # peak 14:00 LST

    def tearDown(self):
        nsw._lookup_peak_hour = self._orig

    def _win(self, overrides, station, series, local_hour, cap=1.0, mmax=1.6, enabled=True):
        import push_window_overrides as pwo
        with mock.patch.object(pwo, "PUSH_WINDOW_OVERRIDES", overrides), \
             mock.patch.object(cfg, "PUSH_EARLY_TRIM_HIGH_ENABLED", enabled), \
             mock.patch.object(cfg, "PUSH_EARLY_TRIM_BEFORE_CAP", cap), \
             mock.patch.object(cfg, "PUSH_EARLY_TRIM_MAE_MAX", mmax), \
             mock.patch.object(cfg, "PUSH_HIGH_TEMP_WINDOW", None), \
             mock.patch.object(cfg, "USE_PUSH_WINDOW_OVERRIDES", True):
            return nsw._in_decision_window(station, series, local_hour, "2026-05-21")

    def test_high_accurate_wide_gets_trimmed(self):
        """HIGH (1.5, 0.0, mae 1.2): before capped 1.5->1.0. peak=14.0.
        local 12.7 (=peak-1.3) was inside old [12.5,14.0], now outside [13.0,14.0]."""
        ov = {("KATL", "HIGH", 5): (1.5, 0.0, -0.2, 1.2)}
        ok, dbg = self._win(ov, "KATL", "HIGH", 12.7)
        self.assertFalse(ok, dbg)
        self.assertIn("early_trim", dbg)
        # 13.5 is inside the trimmed window [13.0, 14.0]
        ok2, _ = self._win(ov, "KATL", "HIGH", 13.5)
        self.assertTrue(ok2)

    def test_after_edge_preserved(self):
        """Trim must NOT touch the `after` edge. (1.5, +1.0) → hi stays peak+1.0."""
        ov = {("KATL", "HIGH", 5): (1.5, 1.0, -0.2, 1.2)}
        ok, dbg = self._win(ov, "KATL", "HIGH", 14.8)  # peak+0.8, inside after=+1.0
        self.assertTrue(ok, dbg)

    def test_high_inaccurate_wide_not_trimmed(self):
        """HIGH wide but mae>=1.6 → NOT trimmed (MAE-sizing handles those)."""
        ov = {("KSEA", "HIGH", 5): (1.5, 0.0, 0.1, 2.7)}
        ok, dbg = self._win(ov, "KSEA", "HIGH", 12.7)  # peak-1.3, inside untrimmed [12.5,14.0]
        self.assertTrue(ok, dbg)
        self.assertNotIn("early_trim", dbg)

    def test_low_not_trimmed(self):
        """LOW cells are HIGH-only-gate exempt even if accurate+wide."""
        ov = {("KMIA", "LOW", 5): (3.0, 0.5, -0.1, 1.2)}
        ok, dbg = self._win(ov, "KMIA", "LOW", 11.5)  # peak-2.5, inside [11.0,14.5]
        self.assertTrue(ok, dbg)
        self.assertNotIn("early_trim", dbg)

    def test_already_narrow_unaffected(self):
        """HIGH cell with before<=cap is unchanged."""
        ov = {("KAUS", "HIGH", 5): (0.75, 0.0, -0.1, 1.0)}
        ok, dbg = self._win(ov, "KAUS", "HIGH", 13.3)  # peak-0.7, inside [13.25,14.0]
        self.assertTrue(ok, dbg)
        self.assertNotIn("early_trim", dbg)

    def test_flag_disables_trim(self):
        """ENABLED=False reverts to the untrimmed window."""
        ov = {("KATL", "HIGH", 5): (1.5, 0.0, -0.2, 1.2)}
        ok, dbg = self._win(ov, "KATL", "HIGH", 12.7, enabled=False)
        self.assertTrue(ok, dbg)  # back inside [12.5, 14.0]
        self.assertNotIn("early_trim", dbg)

    def test_negative_after_does_not_collapse(self):
        """after<0 (window closes before peak): cap must NOT collapse the
        window to zero width. (2.0,-1.0,mae1.4) under cap=1.0 would give
        [peak-1,peak-1]; min-width preservation caps before->1.5 instead,
        leaving [peak-1.5,peak-1.0] (0.5h)."""
        ov = {("KLAX", "HIGH", 5): (2.0, -1.0, 0.1, 1.4)}
        # peak=14.0 -> trimmed window [12.5, 13.0]; 12.7 is inside
        ok, dbg = self._win(ov, "KLAX", "HIGH", 12.7)
        self.assertTrue(ok, dbg)
        self.assertIn("early_trim", dbg)
        self.assertIn("2.0->1.5", dbg)
        # 13.5 is past the (negative-after) close -> out
        ok2, _ = self._win(ov, "KLAX", "HIGH", 13.5)
        self.assertFalse(ok2)


if __name__ == "__main__":
    unittest.main()
