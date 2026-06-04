"""Climate-day guard in nn_shadow_worker._in_decision_window (2026-06-04, Chris).

The window gate tests time-of-day only (local_hour vs the peak window), so a bracket
for a DIFFERENT calendar day that happens to be open during today's deep window would
pass and be bought ~a day early (the Jun-4 LV HIGH concern). The guard refuses any
bracket whose climate_day != the station's current wall-clock date, fail-OPEN on a
tz miss. (The suite-wide conftest fixture disables the guard; this test re-enables it.)
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402
import push_window_overrides as pwo  # noqa: E402


class TestClimateDayGuard(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        # re-enable the guard (conftest turns it OFF for the rest of the suite)
        mock.patch.object(config, "CLIMATE_DAY_GUARD_ENABLED", True).start()

    def _call(self, local_date, climate_day, station="KLAS"):
        with mock.patch.object(nsw, "_station_local_date", return_value=local_date), \
             mock.patch.object(nsw, "_window_peak_hour", return_value=15.0), \
             mock.patch.object(config, "USE_PUSH_WINDOW_OVERRIDES", True), \
             mock.patch.dict(pwo.PUSH_WINDOW_OVERRIDES,
                             {(station, "HIGH", int(climate_day.split("-")[1])): (4.0, -2.5)}):
            return nsw._in_decision_window(station, "HIGH", 11.5, climate_day)

    def test_future_day_bracket_rejected(self):
        # local date Jun-3, bracket Jun-4 (open early) -> refuse
        ok, dbg = self._call("2026-06-03", "2026-06-04")
        self.assertFalse(ok)
        self.assertIn("not_today_climate_day", dbg)

    def test_past_day_bracket_rejected(self):
        ok, dbg = self._call("2026-06-04", "2026-06-03")
        self.assertFalse(ok)
        self.assertIn("not_today_climate_day", dbg)

    def test_today_bracket_passes_guard(self):
        # same date -> guard does NOT fire (proceeds into the window machinery)
        ok, dbg = self._call("2026-06-04", "2026-06-04")
        self.assertNotIn("not_today_climate_day", dbg)

    def test_failopen_on_tz_miss(self):
        # _station_local_date None -> guard skipped, never blocks a real trade
        ok, dbg = self._call(None, "2026-06-04")
        self.assertNotIn("not_today_climate_day", dbg)

    def test_disabled_flag_no_guard(self):
        with mock.patch.object(config, "CLIMATE_DAY_GUARD_ENABLED", False):
            ok, dbg = self._call("2026-06-03", "2026-06-04")
        self.assertNotIn("not_today_climate_day", dbg)


class TestStationLocalDate(unittest.TestCase):
    def test_returns_yyyymmdd_for_known_station(self):
        d = nsw._station_local_date("KLAS")
        self.assertIsNotNone(d)
        self.assertRegex(d, r"^\d{4}-\d{2}-\d{2}$")

    def test_none_for_unknown_station(self):
        self.assertIsNone(nsw._station_local_date("ZZZZ"))


if __name__ == "__main__":
    unittest.main()
