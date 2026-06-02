"""Tests for the 2026-06-02 blend gate changes in nn_shadow_worker._try_auto_execute:
  (1) LOW spread gate     -- PUSH_MAX_SPREAD_C_LOW: skip LOW BUY when yes spread > cap.
  (2) HIGH σ-floor exempt -- BLEND_EXEMPT_HIGH_SIGMA_FLOOR: blend rows (mu_pre_blend set)
      fall back to the global σ-floor instead of the matcher-era per-station floors.
cf project_blend_edge_FOUND 2026-06-02 (faithful current-config P/L sim).
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402


def _cand(ticker, station, series_prefix="KXHIGH", kind="B", floor=88.0, cap=89.0, label=88.5):
    return market_universe.Candidate(
        ticker=ticker, series_prefix=series_prefix, city_code="TEST",
        station=station, climate_day="2026-05-20", bracket_kind=kind,
        floor=floor, cap=cap, bracket_label=label, market={},
    )


def _decision(direction, edge=0.20):
    return {"decision": direction, "edge": edge,
            "p_yes": 0.30 if direction == "BUY_NO" else 0.70,
            "reason": f"{direction} test"}


class _Base(unittest.TestCase):
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
        import config as _cfg
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(kalshi_client, "get_balance_cached", return_value=100.0), \
             mock.patch.object(_cfg, "PUSH_MAE_GATE_ENABLED", False):
            return nsw._try_auto_execute(
                cand, packet, decision, series=series, local_hour=12.0)


class TestLowSpreadGate(_Base):
    def _pkt(self, yb, ya):
        # LOW BUY_NO: no_ask_c = 100 - yes_bid_c; spread = yes_ask_c - yes_bid_c
        return {"yes_bid_c": yb, "yes_ask_c": ya, "no_ask_c": 100 - yb,
                "seconds_to_close": 10_000, "mu_chosen": 60.0,
                "floor": 71.0, "cap": 72.0, "local_clock": {"h_to_peak": 2.0}}

    def test_low_wide_spread_blocked(self):
        import config as _cfg
        cand = _cand("KXLOWTMIA-26MAY20-B71.5", "KMIA", "KXLOW", floor=71.0, cap=72.0, label=71.5)
        with mock.patch.object(_cfg, "PUSH_MAX_SPREAD_C_LOW", 1.0):
            ok, reason = self._run(cand, self._pkt(48, 51), _decision("BUY_NO"), series="LOW")
        self.assertFalse(ok)
        self.assertIn("spread_too_wide_low", reason)

    def test_low_tight_spread_passes_spread_gate(self):
        import config as _cfg
        cand = _cand("KXLOWTMIA-26MAY20-B71.5", "KMIA", "KXLOW", floor=71.0, cap=72.0, label=71.5)
        with mock.patch.object(_cfg, "PUSH_MAX_SPREAD_C_LOW", 1.0):
            ok, reason = self._run(cand, self._pkt(49, 50), _decision("BUY_NO"), series="LOW")
        self.assertNotIn("spread_too_wide_low", reason)

    def test_low_spread_gate_off(self):
        import config as _cfg
        cand = _cand("KXLOWTMIA-26MAY20-B71.5", "KMIA", "KXLOW", floor=71.0, cap=72.0, label=71.5)
        with mock.patch.object(_cfg, "PUSH_MAX_SPREAD_C_LOW", 0.0):
            ok, reason = self._run(cand, self._pkt(40, 55), _decision("BUY_NO"), series="LOW")
        self.assertNotIn("spread_too_wide_low", reason)

    def test_high_unaffected_by_low_gate(self):
        # A wide HIGH spread is governed by PUSH_MAX_SPREAD_C_HIGH, never the LOW gate.
        import config as _cfg
        cand = _cand("KXHIGHMIA-26MAY20-B88.5", "KMIA", "KXHIGH")
        pkt = {"yes_bid_c": 48, "yes_ask_c": 51, "no_ask_c": 52,
               "seconds_to_close": 10_000, "mu_chosen": 92.0,
               "floor": 88.0, "cap": 89.0, "local_clock": {"h_to_peak": 2.0}}
        with mock.patch.object(_cfg, "PUSH_MAX_SPREAD_C_LOW", 1.0), \
             mock.patch.object(_cfg, "PUSH_MAX_SPREAD_C_HIGH", 15.0):
            ok, reason = self._run(cand, pkt, _decision("BUY_NO"), series="HIGH")
        self.assertNotIn("spread_too_wide_low", reason)


class TestSigmaFloorBlendExempt(_Base):
    # KPHX per-station σ-floor = 2.26; blend σ ~1.17 < floor.
    def _pkt(self, sigma, blend):
        # mu=92 keeps the (2d) thin-margin gate clear of the [88,89] bracket;
        # no nbm_high so the (2g) NBM veto never fires; spread gate skipped (no yb).
        pkt = {"yes_ask_c": 50, "no_ask_c": 50, "seconds_to_close": 10_000,
               "mu_chosen": 92.0, "floor": 88.0, "cap": 89.0,
               "sigma_chosen": sigma, "local_clock": {"h_to_peak": 2.0}}
        if blend:
            pkt["mu_pre_blend"] = 90.5
        return pkt

    def test_blend_row_exempt_from_per_station_floor(self):
        import config as _cfg
        cand = _cand("KXHIGHTPHX-26MAY20-B88.5", "KPHX")
        with mock.patch.object(_cfg, "BLEND_EXEMPT_HIGH_SIGMA_FLOOR", True):
            ok, reason = self._run(cand, self._pkt(1.17, blend=True), _decision("BUY_NO"))
        self.assertNotIn("sigma_floor_no", reason)

    def test_matcher_row_still_blocked_by_per_station_floor(self):
        import config as _cfg
        cand = _cand("KXHIGHTPHX-26MAY20-B88.5", "KPHX")
        with mock.patch.object(_cfg, "BLEND_EXEMPT_HIGH_SIGMA_FLOOR", True):
            ok, reason = self._run(cand, self._pkt(1.17, blend=False), _decision("BUY_NO"))
        self.assertFalse(ok)
        self.assertIn("sigma_floor_no", reason)

    def test_exempt_flag_off_blocks_blend_row(self):
        import config as _cfg
        cand = _cand("KXHIGHTPHX-26MAY20-B88.5", "KPHX")
        with mock.patch.object(_cfg, "BLEND_EXEMPT_HIGH_SIGMA_FLOOR", False):
            ok, reason = self._run(cand, self._pkt(1.17, blend=True), _decision("BUY_NO"))
        self.assertFalse(ok)
        self.assertIn("sigma_floor_no", reason)

    def test_blend_row_below_global_floor_still_blocked(self):
        # Exemption falls back to the global floor (1.0), not "no floor": σ=0.9 < 1.0.
        import config as _cfg
        cand = _cand("KXHIGHTPHX-26MAY20-B88.5", "KPHX")
        with mock.patch.object(_cfg, "BLEND_EXEMPT_HIGH_SIGMA_FLOOR", True), \
             mock.patch.object(_cfg, "PUSH_HIGH_NO_MIN_SIGMA_F", 1.0):
            ok, reason = self._run(cand, self._pkt(0.9, blend=True), _decision("BUY_NO"))
        self.assertFalse(ok)
        self.assertIn("sigma_floor_no", reason)


if __name__ == "__main__":
    unittest.main()
