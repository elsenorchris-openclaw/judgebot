"""Tests for nn_shadow_worker._try_auto_execute per-station HIGH bench gate.

2026-05-25: stations in config.PUSH_HIGH_DISABLED_STATIONS (e.g. KSFO) had no
+EV HIGH window at any offset in the last-month faithful regen, so HIGH push
auto-exec is skipped entirely (reason "high_station_benched"). HIGH only; LOW
unaffected. Reversible: remove the station from the set. NB: omitting a station
from PUSH_HIGH_TEMP_WINDOW_BY_STATION does NOT bench it (it falls back to the
global PUSH_HIGH_TEMP_WINDOW) -- benching requires this set + this gate.
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
                    bracket_kind="B", floor=88.0, cap=89.0, label=88.5):
    return market_universe.Candidate(
        ticker=ticker, series_prefix=series_prefix, city_code="TEST",
        station=station, climate_day=climate_day, bracket_kind=bracket_kind,
        floor=floor, cap=cap, bracket_label=label, market={},
    )


def _make_packet(mu=92.0, floor=88.0, cap=89.0):
    # edge 0.20 clears the 18pp floor; no yes_bid/ask -> spread gate skipped;
    # no_ask_c in [10,80] for the price gate; h_to_peak high.
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


class TestHighStationBench(unittest.TestCase):

    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window

    def _run(self, cand, packet, decision, series="HIGH"):
        import paper_judge_bot as pjb
        import kalshi_client
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(kalshi_client, "get_balance_cached",
                               return_value=100.0):
            return nsw._try_auto_execute(
                cand, packet, decision, series=series, local_hour=12.0,
            )

    def test_benched_high_station_blocked(self):
        """A station IN PUSH_HIGH_DISABLED_STATIONS -> blocked with high_station_benched.
        Patches the disabled set so the test verifies the gate MECHANISM independent of
        live config (KSFO was un-benched 2026-05-25, so don't couple to live state)."""
        import config
        cand = _make_candidate("KXHIGHSFO-26MAY20-B88.5", "KSFO",
                               "KXHIGH", "2026-05-20")
        with mock.patch.object(config, "PUSH_HIGH_DISABLED_STATIONS",
                               frozenset({"KSFO"})):
            executed, reason = self._run(cand, _make_packet(), _make_decision())
        self.assertFalse(executed)
        self.assertIn("high_station_benched", reason)

    def test_non_benched_high_station_not_blocked(self):
        """A non-benched HIGH station (KMIA) is not blocked by this gate."""
        cand = _make_candidate("KXHIGHMIA-26MAY20-B88.5", "KMIA",
                               "KXHIGH", "2026-05-20")
        executed, reason = self._run(cand, _make_packet(), _make_decision())
        self.assertNotIn("high_station_benched", reason)

    def test_low_series_not_benched(self):
        """Bench is HIGH-only: KSFO LOW is not blocked by this gate."""
        cand = _make_candidate("KXLOWSFO-26MAY20-B55.5", "KSFO",
                               "KXLOW", "2026-05-20",
                               floor=55.0, cap=56.0, label=55.5)
        executed, reason = self._run(
            cand, _make_packet(55.5, floor=55.0, cap=56.0),
            _make_decision(), series="LOW")
        self.assertNotIn("high_station_benched", reason)

    def test_reversible_empty_set(self):
        """Emptying PUSH_HIGH_DISABLED_STATIONS un-benches KSFO."""
        import config as _cfg
        cand = _make_candidate("KXHIGHSFO-26MAY20-B88.5", "KSFO",
                               "KXHIGH", "2026-05-20")
        with mock.patch.object(_cfg, "PUSH_HIGH_DISABLED_STATIONS", frozenset()):
            executed, reason = self._run(cand, _make_packet(), _make_decision())
        self.assertNotIn("high_station_benched", reason)


if __name__ == "__main__":
    unittest.main()
