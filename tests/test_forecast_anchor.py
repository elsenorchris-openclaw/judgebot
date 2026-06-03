"""Tests for forecast-anchored window peak/min hour (nn_shadow_worker).

Verifies the two discriminators that decide whether to trust the live NWP forecast
hour over the empirical climatology:
  - BAND: search the argmax/argmin only in the physical band, so a low-diurnal-range
    station's calendar-day argmin (which lands in the EVENING -- verified KMIA/KLAX/
    KSEA) is ignored.
  - SHARP: trust only a clear extreme (few hours within tol of it), not a flat plateau
    where the argmax is just noise.
cf the forecast-anchor build, 2026-06-03 (Chris).
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nn_shadow_worker as nsw  # noqa: E402


def _curve(temps_by_hour):
    """{daylight_hour: tempF} -> sorted [(hour, temp)] like _fc_curve returns."""
    return [(h, t) for h, t in sorted(temps_by_hour.items())]


class TestFcExtremeHour(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(nsw, "_dst_offset_h", return_value=1.0).start()   # summer DST
        mock.patch.object(nsw._cfg, "FORECAST_FLAT_TOL_F", 1.0).start()
        mock.patch.object(nsw._cfg, "FORECAST_FLAT_MAX_HOURS", 3).start()
        mock.patch.object(nsw._cfg, "FC_BAND_EDGE_MARGIN_H", 0.5).start()

    def _curve_patch(self, temps):
        return mock.patch.object(nsw, "_fc_curve", return_value=_curve(temps))

    def test_low_band_rejects_evening_argmin(self):
        # KMIA-like: GLOBAL min is the evening (21-22h), but there's a dawn min at 5h.
        temps = {h: 80.0 for h in range(24)}
        temps[21] = 70.0; temps[22] = 70.5     # evening coldest (calendar-day argmin)
        temps[4] = 75.0; temps[5] = 73.0; temps[6] = 75.0   # dawn min (in-band)
        with self._curve_patch(temps):
            hr, sharp = nsw._fc_extreme_hour("KMIA", "2026-06-03", "LOW")
        self.assertAlmostEqual(hr, 4.0, places=1)   # 5h daylight -> 4h LST, NOT the 21h evening

    def test_low_sharp_dawn_min(self):
        temps = {h: 80.0 for h in range(24)}
        temps[6] = 60.0                          # one clear cold hour: 6h LDT -> 5h LST
        with self._curve_patch(temps):
            hr, sharp = nsw._fc_extreme_hour("KAUS", "2026-06-03", "LOW")
        self.assertAlmostEqual(hr, 5.0, places=1)
        self.assertTrue(sharp)

    def test_high_flat_plateau_not_sharp(self):
        temps = {h: 60.0 for h in range(24)}
        for h in range(12, 18):
            temps[h] = 90.0                      # 6-hour afternoon plateau all at max
        with self._curve_patch(temps):
            hr, sharp = nsw._fc_extreme_hour("KLAX", "2026-06-03", "HIGH")
        self.assertFalse(sharp)                  # plateau -> timing is noise -> not sharp

    def test_high_sharp_front_peak(self):
        # BOS-like front: a clear noon peak (12h LDT -> 11h LST), far from a 15h climo.
        temps = {h: 60.0 for h in range(24)}
        temps[12] = 80.0; temps[11] = 76.0; temps[13] = 76.0
        with self._curve_patch(temps):
            hr, sharp = nsw._fc_extreme_hour("KBOS", "2026-06-03", "HIGH")
        self.assertAlmostEqual(hr, 11.0, places=1)
        self.assertTrue(sharp)

    def test_band_edge_extreme_distrusted(self):
        # KMSY-like: in-band min lands on the band floor (2h LDT -> 1.0 LST), which
        # likely means the true min is OUTSIDE the band (clipped) -> not sharp -> climo.
        temps = {h: 80.0 for h in range(24)}
        temps[2] = 60.0
        with self._curve_patch(temps):
            hr, sharp = nsw._fc_extreme_hour("KMSY", "2026-06-03", "LOW")
        self.assertAlmostEqual(hr, 1.0, places=1)
        self.assertFalse(sharp)

    def test_no_inband_hours(self):
        with self._curve_patch({20: 70.0, 21: 69.0, 22: 70.0}):
            hr, sharp = nsw._fc_extreme_hour("KMIA", "2026-06-03", "LOW")
        self.assertIsNone(hr)
        self.assertFalse(sharp)

    def test_empty_curve(self):
        with mock.patch.object(nsw, "_fc_curve", return_value=[]):
            hr, sharp = nsw._fc_extreme_hour("KAUS", "2026-06-03", "LOW")
        self.assertIsNone(hr)


class TestWindowPeakHour(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(nsw, "_lookup_peak_hour", return_value=5.0).start()   # empirical LST
        mock.patch.object(nsw._cfg, "FORECAST_ANCHOR_ENABLED", True).start()

    def test_uses_forecast_when_sharp(self):
        with mock.patch.object(nsw, "_fc_extreme_hour", return_value=(7.0, True)):
            self.assertEqual(nsw._window_peak_hour("KAUS", "LOW", "2026-06-03"), 7.0)

    def test_climo_when_flat(self):
        with mock.patch.object(nsw, "_fc_extreme_hour", return_value=(20.0, False)):
            self.assertEqual(nsw._window_peak_hour("KMIA", "LOW", "2026-06-03"), 5.0)

    def test_climo_when_no_forecast(self):
        with mock.patch.object(nsw, "_fc_extreme_hour", return_value=(None, False)):
            self.assertEqual(nsw._window_peak_hour("KAUS", "LOW", "2026-06-03"), 5.0)

    def test_climo_and_no_fetch_when_disabled(self):
        with mock.patch.object(nsw._cfg, "FORECAST_ANCHOR_ENABLED", False), \
             mock.patch.object(nsw, "_fc_extreme_hour", return_value=(7.0, True)) as fc:
            self.assertEqual(nsw._window_peak_hour("KAUS", "LOW", "2026-06-03"), 5.0)
        fc.assert_not_called()


class TestGateFollowsForecast(unittest.TestCase):
    """The whole point: when the forecast is sharp+in-band, the gate's window centers
    on the FORECAST hour, not the climatology -- so a front day is traded at the right
    lead. (deep LOW window = peak-3..peak-1.5)"""
    def test_gate_window_shifts_to_forecast(self):
        import push_window_overrides as pwo
        with mock.patch.object(nsw, "_lookup_peak_hour", return_value=5.0), \
             mock.patch.object(nsw, "_fc_extreme_hour", return_value=(7.0, True)), \
             mock.patch.object(nsw._cfg, "FORECAST_ANCHOR_ENABLED", True), \
             mock.patch.object(nsw._cfg, "USE_PUSH_WINDOW_OVERRIDES", True), \
             mock.patch.object(nsw._cfg, "BLEND_FORECAST_ENABLED", True), \
             mock.patch.object(nsw._cfg, "BLEND_FORECAST_LOW_ENABLED", True), \
             mock.patch.object(nsw._cfg, "BLEND_DEEP_WINDOW_ENABLED", True), \
             mock.patch.object(nsw._cfg, "PUSH_LOW_TEMP_WINDOW", None), \
             mock.patch.dict(pwo.PUSH_WINDOW_OVERRIDES, {("KAUS", "LOW", 6): (2.0, -1.0)}):
            # forecast min 7.0 -> window [4.0, 5.5]; climo min 5.0 -> window [2.0, 3.5]
            ok_in, _ = nsw._in_decision_window("KAUS", "LOW", 4.5, "2026-06-03")
            ok_climo_only, _ = nsw._in_decision_window("KAUS", "LOW", 3.0, "2026-06-03")
        self.assertTrue(ok_in)            # inside the forecast window
        self.assertFalse(ok_climo_only)   # 3.0 was in the climo window, NOT the forecast one


if __name__ == "__main__":
    unittest.main()
