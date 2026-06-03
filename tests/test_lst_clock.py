"""Tests for the single LST min/peak-hour source (nn_shadow_worker).

The window gate uses the empirical peak/min table (LST, observed P50); the eval clock
was solar+ZoneInfo (DAYLIGHT time + a cruder model), so in summer the two were ~1h
apart and the gate rejected 194 in-window LOW buys/day. _apply_lst_clock unifies the
packet clock into ONE LST frame. cf the DST window-mismatch fix, 2026-06-03 (Chris).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nn_shadow_worker as nsw  # noqa: E402


class TestLstSignedH(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(nsw._lst_signed_h(15.0, 12.0), 3.0)
        self.assertEqual(nsw._lst_signed_h(12.0, 15.0), -3.0)

    def test_wrap(self):
        self.assertEqual(nsw._lst_signed_h(1.0, 23.0), 2.0)    # next day, not -22
        self.assertEqual(nsw._lst_signed_h(23.0, 1.0), -2.0)   # just passed, not +22

    def test_none(self):
        self.assertIsNone(nsw._lst_signed_h(None, 12.0))
        self.assertIsNone(nsw._lst_signed_h(12.0, None))


class TestDstOffset(unittest.TestCase):
    def test_arizona_never_dst(self):
        self.assertEqual(nsw._dst_offset_h("KPHX"), 0.0)

    def test_unknown_station(self):
        self.assertEqual(nsw._dst_offset_h("ZZZZ"), 0.0)

    def test_dst_zone_zero_or_one(self):
        self.assertIn(nsw._dst_offset_h("KNYC"), (0.0, 1.0))


class TestApplyLstClock(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(nsw, "_lookup_peak_hour",
                          side_effect=lambda st, series, day: 15.0 if series == "HIGH" else 5.0).start()
        mock.patch.object(nsw, "_dst_offset_h", return_value=1.0).start()   # summer DST
        mock.patch.object(nsw._cfg, "LST_CLOCK_ENABLED", True).start()

    def test_local_hour_shifted_and_peak_from_table(self):
        ctx = {"local_hour": 3.29, "peak_hour_local": 16.0, "min_hour_local": 6.5}
        out = nsw._apply_lst_clock(ctx, "KPHL", "2026-06-03")
        self.assertAlmostEqual(out["local_hour"], 2.29, places=2)   # 3.29 LDT - 1 DST
        self.assertEqual(out["peak_hour_local"], 15.0)              # empirical table
        self.assertEqual(out["min_hour_local"], 5.0)
        self.assertEqual(out["tz_convention"], "LST")
        self.assertEqual(out["dst_offset_h"], 1.0)

    def test_h_to_values_consistent(self):
        ctx = {"local_hour": 3.29}                                  # 3.29 LDT -> 2.29 LST
        out = nsw._apply_lst_clock(ctx, "KPHL", "2026-06-03")
        self.assertAlmostEqual(out["h_to_min"], 2.71, places=2)     # 5.0 - 2.29 (in deep window)
        # h_to_peak wraps to (-12, 12]: 15.0 - 2.29 = 12.71 > 12 -> -11.29 (matches solar_calc)
        self.assertAlmostEqual(out["h_to_peak"], -11.29, places=2)
        self.assertFalse(out["past_min_today"])

    def test_h_to_peak_no_wrap_midday(self):
        # local 13.0 LDT -> 12.0 LST: peak 15.0 -> h_to_peak 3.0 (no wrap), min past.
        out = nsw._apply_lst_clock({"local_hour": 13.0}, "KPHL", "2026-06-03")
        self.assertAlmostEqual(out["h_to_peak"], 3.0, places=2)
        self.assertAlmostEqual(out["h_to_min"], -7.0, places=2)
        self.assertTrue(out["past_min_today"])

    def test_table_miss_falls_back_to_solar_lst(self):
        with mock.patch.object(nsw, "_lookup_peak_hour", return_value=None):
            ctx = {"local_hour": 3.29, "peak_hour_local": 16.0, "min_hour_local": 6.5}
            out = nsw._apply_lst_clock(ctx, "KZZZ", "2026-06-03")
        self.assertAlmostEqual(out["min_hour_local"], 5.5, places=2)   # 6.5 LDT - 1
        self.assertAlmostEqual(out["peak_hour_local"], 15.0, places=2)  # 16.0 - 1

    def test_inert_when_disabled(self):
        with mock.patch.object(nsw._cfg, "LST_CLOCK_ENABLED", False):
            out = nsw._apply_lst_clock({"local_hour": 3.29, "min_hour_local": 6.5},
                                       "KPHL", "2026-06-03")
        self.assertEqual(out["local_hour"], 3.29)       # untouched
        self.assertNotIn("tz_convention", out)


class TestGateConsistency(unittest.TestCase):
    """The core invariant: after _apply_lst_clock the eval clock's h_to_min equals the
    gate's (empirical peak - local_hour) -- same LST frame -> a row in-window by the
    eval clock passes the gate. Reproduces the 6/3 PHIL rejection being recovered."""
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(nsw, "_lookup_peak_hour",
                          side_effect=lambda st, series, day: 4.683 if series == "LOW" else 15.0).start()
        mock.patch.object(nsw, "_dst_offset_h", return_value=1.0).start()
        mock.patch.object(nsw._cfg, "LST_CLOCK_ENABLED", True).start()

    def test_eval_clock_and_gate_agree(self):
        # PHIL 6/3: solar local 3.29 LDT. After fix -> 2.29 LST.
        ctx = nsw._apply_lst_clock({"local_hour": 3.29}, "KPHL", "2026-06-03")
        gate_h2m = nsw._lookup_peak_hour("KPHL", "LOW", "2026-06-03") - ctx["local_hour"]
        self.assertAlmostEqual(ctx["h_to_min"], gate_h2m, places=2)   # same LST frame
        self.assertTrue(1.5 <= ctx["h_to_min"] <= 3.0)                # in the deep window

    def test_gate_recovers_phil(self):
        # Full gate: PHIL was rejected "too late" pre-fix; now in-window.
        import push_window_overrides as pwo
        with mock.patch.dict(pwo.PUSH_WINDOW_OVERRIDES, {("KPHL", "LOW", 6): (2.0, -1.0)}), \
             mock.patch.object(nsw._cfg, "USE_PUSH_WINDOW_OVERRIDES", True), \
             mock.patch.object(nsw._cfg, "BLEND_FORECAST_ENABLED", True), \
             mock.patch.object(nsw._cfg, "BLEND_FORECAST_LOW_ENABLED", True), \
             mock.patch.object(nsw._cfg, "BLEND_DEEP_WINDOW_ENABLED", True), \
             mock.patch.object(nsw._cfg, "PUSH_LOW_TEMP_WINDOW", None):
            ctx = nsw._apply_lst_clock({"local_hour": 3.29}, "KPHL", "2026-06-03")
            ok, dbg = nsw._in_decision_window("KPHL", "LOW", ctx["local_hour"], "2026-06-03")
        self.assertTrue(ok, dbg)


if __name__ == "__main__":
    unittest.main()
