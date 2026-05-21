"""Tests for nn_shadow_worker._try_auto_execute LOW cold-front gate (2c).

2026-05-21: LOW push BUYs are skipped when sustained wind >= PUSH_LOW_FRONT_
WIND_MPH (default 18mph ~= 15kt). Sustained wind at an overnight LOW is a
frontal / cold-air-advection signature -- the nn matcher over-projects the
daily minimum (+1.5..+3F bias, 25-yr backtest, cross-year validated 18/20
stations) and its sigma does not widen to flag it. HIGH is storm-robust ->
LOW-side only. KLAX/KMIA excluded (marine climate, no frontal bias). The gate
fires a throttled Discord alert, deduped per (station, climate_day).
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402


def _make_candidate(ticker, station, series_prefix, climate_day,
                    bracket_kind="B", floor=51.0, cap=52.0, label=51.5):
    return market_universe.Candidate(
        ticker=ticker, series_prefix=series_prefix, city_code="TEST",
        station=station, climate_day=climate_day, bracket_kind=bracket_kind,
        floor=floor, cap=cap, bracket_label=label, market={},
    )


def _make_packet(wind_mph, h_to_peak=2.0):
    return {
        "yes_ask_c": 50, "no_ask_c": 50, "seconds_to_close": 10_000,
        "local_clock": {"h_to_peak": h_to_peak},
        "wethr_obs": {"wind_speed_mph": wind_mph},
    }


def _make_decision(direction="BUY_NO"):
    return {
        "decision": direction, "edge": 0.20,
        "p_yes": 0.30 if direction == "BUY_NO" else 0.70,
        "reason": f"{direction} test edge=20.0pp",
    }


class TestLowFrontGate(unittest.TestCase):

    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})
        nsw._low_front_alert_seen.clear()

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window
        nsw._low_front_alert_seen.clear()

    def _run(self, cand, packet, decision, series="LOW"):
        import paper_judge_bot as pjb
        import kalshi_client
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(pjb, "discord_send", lambda *a, **kw: None), \
             mock.patch.object(kalshi_client, "get_balance_cached",
                               return_value=100.0):
            return nsw._try_auto_execute(
                cand, packet, decision, series=series, local_hour=5.0,
            )

    def test_low_strong_wind_blocked(self):
        """LOW + sustained wind 25mph (>= 18) must be blocked."""
        cand = _make_candidate("KXLOWTMSP-26JAN15-B12.5", "KMSP",
                                "KXLOW", "2026-01-15")
        executed, reason = self._run(cand, _make_packet(25.0),
                                     _make_decision("BUY_NO"))
        self.assertFalse(executed)
        self.assertIn("low_frontal_wind", reason)

    def test_low_at_threshold_blocked(self):
        """LOW + wind exactly 18mph must be blocked (>= is inclusive)."""
        cand = _make_candidate("KXLOWTOKC-26FEB02-B30.5", "KOKC",
                                "KXLOW", "2026-02-02")
        executed, reason = self._run(cand, _make_packet(18.0),
                                     _make_decision("BUY_NO"))
        self.assertFalse(executed)
        self.assertIn("low_frontal_wind", reason)

    def test_low_just_below_threshold_allowed(self):
        """LOW + wind 17.9mph (just below 18) must NOT be blocked by 2c."""
        cand = _make_candidate("KXLOWTDEN-26JAN15-B20.5", "KDEN",
                                "KXLOW", "2026-01-15")
        executed, reason = self._run(cand, _make_packet(17.9),
                                     _make_decision("BUY_NO"))
        self.assertNotIn("low_frontal_wind", reason)

    def test_low_calm_allowed(self):
        """LOW + calm wind (6mph) must NOT be blocked by 2c."""
        cand = _make_candidate("KXLOWTATL-26JAN15-B40.5", "KATL",
                                "KXLOW", "2026-01-15")
        executed, reason = self._run(cand, _make_packet(6.0),
                                     _make_decision("BUY_NO"))
        self.assertNotIn("low_frontal_wind", reason)

    def test_high_not_affected(self):
        """HIGH series must NOT be blocked by the LOW cold-front gate even at
        high sustained wind (HIGH is storm-robust)."""
        cand = _make_candidate("KXHIGHTMSP-26JAN15-B30.5", "KMSP",
                                "KXHIGH", "2026-01-15")
        executed, reason = self._run(cand, _make_packet(30.0),
                                     _make_decision("BUY_NO"), series="HIGH")
        self.assertNotIn("low_frontal_wind", reason)

    def test_klax_excluded(self):
        """KLAX (marine) must be exempt -- strong wind is sea-breeze."""
        cand = _make_candidate("KXLOWTLAX-26JAN15-B50.5", "KLAX",
                                "KXLOW", "2026-01-15")
        executed, reason = self._run(cand, _make_packet(30.0),
                                     _make_decision("BUY_NO"))
        self.assertNotIn("low_frontal_wind", reason)

    def test_kmia_excluded(self):
        """KMIA (marine) must be exempt."""
        cand = _make_candidate("KXLOWTMIA-26JAN15-B60.5", "KMIA",
                                "KXLOW", "2026-01-15")
        executed, reason = self._run(cand, _make_packet(30.0),
                                     _make_decision("BUY_NO"))
        self.assertNotIn("low_frontal_wind", reason)

    def test_wind_none_allowed(self):
        """Missing wind_speed_mph (None) must NOT block (defensive)."""
        cand = _make_candidate("KXLOWTBOS-26JAN15-B15.5", "KBOS",
                                "KXLOW", "2026-01-15")
        executed, reason = self._run(cand, _make_packet(None),
                                     _make_decision("BUY_NO"))
        self.assertNotIn("low_frontal_wind", reason)

    def test_config_disable_via_zero(self):
        """PUSH_LOW_FRONT_WIND_MPH=0 must disable the gate."""
        import config as cfg
        cand = _make_candidate("KXLOWTMSP-26JAN15-B12.5", "KMSP",
                                "KXLOW", "2026-01-15")
        with mock.patch.object(cfg, "PUSH_LOW_FRONT_WIND_MPH", 0.0):
            executed, reason = self._run(cand, _make_packet(25.0),
                                         _make_decision("BUY_NO"))
        self.assertNotIn("low_frontal_wind", reason)

    def test_discord_alert_fires_once_per_station_day(self):
        """The gate must fire a Discord alert, deduped per (station, day)."""
        import paper_judge_bot as pjb
        import kalshi_client
        calls = []
        cand1 = _make_candidate("KXLOWTMSP-26JAN15-B12.5", "KMSP",
                                "KXLOW", "2026-01-15")
        cand2 = _make_candidate("KXLOWTMSP-26JAN15-B14.5", "KMSP",
                                "KXLOW", "2026-01-15", floor=13.0, cap=14.0,
                                label=13.5)
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(pjb, "discord_send",
                               lambda m, *a, **kw: calls.append(m)), \
             mock.patch.object(kalshi_client, "get_balance_cached",
                               return_value=100.0):
            nsw._try_auto_execute(cand1, _make_packet(25.0),
                                  _make_decision("BUY_NO"),
                                  series="LOW", local_hour=5.0)
            nsw._try_auto_execute(cand2, _make_packet(25.0),
                                  _make_decision("BUY_NO"),
                                  series="LOW", local_hour=5.0)
        self.assertEqual(len(calls), 1)
        self.assertIn("LOW COLD-FRONT GATE", calls[0])
        self.assertIn("KMSP", calls[0])


if __name__ == "__main__":
    unittest.main()
