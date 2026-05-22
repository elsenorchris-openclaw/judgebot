"""Tests for nn_shadow_worker._try_auto_execute h_to_peak gate.

2026-05-20: HIGH entries at h_to_peak < PUSH_MIN_H_TO_PEAK_HIGH (default
0.5) must be blocked. At peak, rm has converged on the day's true max,
so the nn_match mu over-extrapolates and flips adjacent brackets the
wrong way (3 today losses -$13.07 at h_to_peak<0.5).
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
                    bracket_kind="B", floor=86.0, cap=87.0, label=86.5):
    return market_universe.Candidate(
        ticker=ticker, series_prefix=series_prefix, city_code="TEST",
        station=station, climate_day=climate_day, bracket_kind=bracket_kind,
        floor=floor, cap=cap, bracket_label=label, market={},
    )


def _make_packet(h_to_peak):
    return {
        "yes_ask_c": 50, "no_ask_c": 50, "seconds_to_close": 10_000,
        "local_clock": {"h_to_peak": h_to_peak},
    }


def _make_decision(direction):
    return {
        "decision": direction, "edge": 0.20,
        "p_yes": 0.30 if direction == "BUY_NO" else 0.70,
        "reason": f"{direction} test edge=20.0pp",
    }


class TestH2pkGate(unittest.TestCase):

    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})
        # Production default is now None (gate disabled 2026-05-22); these
        # tests validate the gate LOGIC, so force it on at 0.5.
        import config as _cfg
        self._h2 = mock.patch.object(_cfg, "PUSH_MIN_H_TO_PEAK_HIGH", 0.5)
        self._h2.start()

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window
        self._h2.stop()

    def _run(self, cand, packet, decision, series="HIGH"):
        import paper_judge_bot as pjb
        import kalshi_client
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(kalshi_client, "get_balance_cached",
                               return_value=100.0):
            return nsw._try_auto_execute(
                cand, packet, decision, series=series, local_hour=15.0,
            )

    def test_high_at_peak_blocked(self):
        """h_to_peak = 0.0 (exactly at peak) must be blocked on HIGH."""
        cand = _make_candidate("KXHIGHTNOLA-26MAY20-B86.5", "KMSY",
                                "KXHIGH", "2026-05-20")
        executed, reason = self._run(cand, _make_packet(0.0),
                                      _make_decision("BUY_NO"))
        self.assertFalse(executed)
        self.assertIn("h2pk_too_low", reason)

    def test_high_past_peak_blocked(self):
        """h_to_peak = -0.5 (past peak) must be blocked on HIGH."""
        cand = _make_candidate("KXHIGHTPHIL-26MAY20-B95.5", "KPHL",
                                "KXHIGH", "2026-05-20",
                                floor=95.0, cap=96.0, label=95.5)
        executed, reason = self._run(cand, _make_packet(-0.5),
                                      _make_decision("BUY_NO"))
        self.assertFalse(executed)
        self.assertIn("h2pk_too_low", reason)

    def test_high_below_threshold_blocked(self):
        """h_to_peak = 0.3 (below default 0.5 threshold) must be blocked."""
        cand = _make_candidate("KXHIGHTNOLA-26MAY20-B88.5", "KMSY",
                                "KXHIGH", "2026-05-20",
                                floor=88.0, cap=89.0, label=88.5)
        executed, reason = self._run(cand, _make_packet(0.3),
                                      _make_decision("BUY_YES"))
        self.assertFalse(executed)
        self.assertIn("h2pk_too_low", reason)

    def test_high_at_threshold_allowed(self):
        """h_to_peak = 0.5 (exactly at threshold) must NOT be blocked
        (strict less-than)."""
        cand = _make_candidate("KXHIGHTDAL-26MAY20-B81.5", "KDFW",
                                "KXHIGH", "2026-05-20")
        executed, reason = self._run(cand, _make_packet(0.5),
                                      _make_decision("BUY_YES"))
        self.assertNotIn("h2pk_too_low", reason)

    def test_high_above_threshold_allowed(self):
        """h_to_peak = 1.5 (well above threshold) must NOT be blocked."""
        cand = _make_candidate("KXHIGHMIA-26MAY20-B86.5", "KMIA",
                                "KXHIGH", "2026-05-20")
        executed, reason = self._run(cand, _make_packet(1.5),
                                      _make_decision("BUY_NO"))
        self.assertNotIn("h2pk_too_low", reason)

    def test_low_series_not_affected(self):
        """LOW series must NOT be affected by the HIGH h2pk gate even at
        h_to_peak < 0.5."""
        cand = _make_candidate("KXLOWTSEA-26MAY20-B51.5", "KSEA",
                                "KXLOW", "2026-05-20",
                                floor=51.0, cap=52.0, label=51.5)
        executed, reason = self._run(cand, _make_packet(0.0),
                                      _make_decision("BUY_NO"), series="LOW")
        self.assertNotIn("h2pk_too_low", reason)

    def test_h_to_peak_none_allowed(self):
        """Missing h_to_peak (None) must NOT block (defensive: don't
        double-penalize when telemetry is incomplete)."""
        cand = _make_candidate("KXHIGHMIA-26MAY20-B86.5", "KMIA",
                                "KXHIGH", "2026-05-20")
        executed, reason = self._run(cand, _make_packet(None),
                                      _make_decision("BUY_NO"))
        self.assertNotIn("h2pk_too_low", reason)

    def test_config_disable_via_none(self):
        """Setting PUSH_MIN_H_TO_PEAK_HIGH=None must disable the gate."""
        import config as cfg
        cand = _make_candidate("KXHIGHTNOLA-26MAY20-B86.5", "KMSY",
                                "KXHIGH", "2026-05-20")
        with mock.patch.object(cfg, "PUSH_MIN_H_TO_PEAK_HIGH", None):
            executed, reason = self._run(cand, _make_packet(0.0),
                                          _make_decision("BUY_NO"))
        self.assertNotIn("h2pk_too_low", reason)


    def test_gate_disabled_when_none(self):
        """PUSH_MIN_H_TO_PEAK_HIGH=None disables the gate (2026-05-22): an
        at-peak HIGH entry is NOT blocked by h2pk -- windows control timing."""
        import config as _cfg
        cand = _make_candidate("KXHIGHMIA-26MAY20-B86.5", "KMIA",
                                "KXHIGH", "2026-05-20")
        with mock.patch.object(_cfg, "PUSH_MIN_H_TO_PEAK_HIGH", None):
            executed, reason = self._run(cand, _make_packet(0.0),
                                          _make_decision("BUY_NO"))
        self.assertNotIn("h2pk_too_low", reason)

if __name__ == "__main__":
    unittest.main()
