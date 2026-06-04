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


class TestBlendOnlyExecution(_Base):
    """2026-06-02: only execute when mu came from the blend (mu_method='blend_*');
    the nn_match matcher fallback must not place orders. (conftest disables this gate
    for the suite by default; here we re-enable it explicitly.)"""
    def _pkt(self, mu_method):
        return {"yes_ask_c": 50, "no_ask_c": 50, "seconds_to_close": 10_000,
                "mu_chosen": 92.0, "floor": 88.0, "cap": 89.0, "sigma_chosen": 1.17,
                "mu_method": mu_method, "local_clock": {"h_to_peak": 2.0}}

    def test_matcher_mu_blocked(self):
        # With the paper book OFF, a matcher mu is hard-blocked from real execution.
        import config as _cfg
        cand = _cand("KXHIGHMIA-26MAY20-B88.5", "KMIA")
        with mock.patch.object(_cfg, "BLEND_ONLY_EXECUTION", True), \
             mock.patch.object(_cfg, "MATCHER_PAPER_ENABLED", False):
            ok, reason = self._run(cand, self._pkt("nn_match_high_n50"), _decision("BUY_NO"))
        self.assertFalse(ok)
        self.assertIn("blend_only", reason)

    def test_none_mu_method_blocked(self):
        import config as _cfg
        cand = _cand("KXHIGHMIA-26MAY20-B88.5", "KMIA")
        with mock.patch.object(_cfg, "BLEND_ONLY_EXECUTION", True):
            ok, reason = self._run(cand, self._pkt(None), _decision("BUY_NO"))
        self.assertFalse(ok)
        self.assertIn("blend_only", reason)

    def test_blend_mu_passes_gate0(self):
        import config as _cfg
        cand = _cand("KXHIGHMIA-26MAY20-B88.5", "KMIA")
        with mock.patch.object(_cfg, "BLEND_ONLY_EXECUTION", True):
            ok, reason = self._run(cand, self._pkt("blend_KXHIGH"), _decision("BUY_NO"))
        self.assertNotIn("blend_only", reason)   # passes gate 0; may stop at a later gate

    def test_flag_off_allows_matcher(self):
        import config as _cfg
        cand = _cand("KXHIGHMIA-26MAY20-B88.5", "KMIA")
        with mock.patch.object(_cfg, "BLEND_ONLY_EXECUTION", False):
            ok, reason = self._run(cand, self._pkt("nn_match_high_n50"), _decision("BUY_NO"))
        self.assertNotIn("blend_only", reason)


class TestMatcherPaper(_Base):
    """2026-06-02: with BLEND_ONLY_EXECUTION + MATCHER_PAPER_ENABLED, a genuine
    nn_match matcher mu is routed to the ISOLATED paper book (return True, reason
    'paper_matcher') instead of being hard-blocked or really executed. The paper
    diversion happens at the dedup step, BEFORE any real-state code, so the blend's
    real trading is untouched. sigma=2.5 clears the per-station floor; mu=92 sits
    outside the [88,89] bracket so the tail-bet gate stays clear."""
    def _pkt(self, mu_method):
        return {"yes_ask_c": 50, "no_ask_c": 50, "seconds_to_close": 10_000,
                "mu_chosen": 92.0, "floor": 88.0, "cap": 89.0, "sigma_chosen": 2.5,
                "mu_method": mu_method,
                "local_clock": {"h_to_peak": 3.0, "local_hour": 12.0}}

    def test_matcher_routed_to_paper(self):
        import config as _cfg
        cand = _cand("KXHIGHMIA-26MAY20-B88.5", "KMIA")
        with mock.patch.object(_cfg, "BLEND_ONLY_EXECUTION", True), \
             mock.patch.object(_cfg, "MATCHER_PAPER_ENABLED", True), \
             mock.patch.object(nsw, "_paper_positions", set()), \
             mock.patch.object(nsw, "_paper_record_entry") as rec:
            ok, reason = self._run(cand, self._pkt("nn_match_high_n50"), _decision("BUY_NO"))
        self.assertTrue(ok)
        self.assertIn("paper_matcher", reason)
        rec.assert_called_once()

    def test_paper_dedup_blocks_second(self):
        import config as _cfg
        cand = _cand("KXHIGHMIA-26MAY20-B88.5", "KMIA")
        with mock.patch.object(_cfg, "BLEND_ONLY_EXECUTION", True), \
             mock.patch.object(_cfg, "MATCHER_PAPER_ENABLED", True), \
             mock.patch.object(nsw, "_paper_positions", {"KXHIGHMIA-26MAY20-B88.5"}), \
             mock.patch.object(nsw, "_paper_record_entry") as rec:
            ok, reason = self._run(cand, self._pkt("nn_match_high_n50"), _decision("BUY_NO"))
        self.assertFalse(ok)
        self.assertIn("paper_dup", reason)
        rec.assert_not_called()

    def test_blend_row_not_papered(self):
        # A blend mu still takes the REAL path (execute_buy is mocked in _run), never paper.
        import config as _cfg
        cand = _cand("KXHIGHMIA-26MAY20-B88.5", "KMIA")
        with mock.patch.object(_cfg, "BLEND_ONLY_EXECUTION", True), \
             mock.patch.object(_cfg, "MATCHER_PAPER_ENABLED", True), \
             mock.patch.object(nsw, "_paper_positions", set()), \
             mock.patch.object(nsw, "_paper_record_entry") as rec:
            ok, reason = self._run(cand, self._pkt("blend_KXHIGH"), _decision("BUY_NO"))
        self.assertNotIn("paper_matcher", reason)
        rec.assert_not_called()

    def test_none_mu_not_papered_still_blocked(self):
        # A missing mu_method is NOT a matcher mu -> hard-blocked even with paper on.
        import config as _cfg
        cand = _cand("KXHIGHMIA-26MAY20-B88.5", "KMIA")
        with mock.patch.object(_cfg, "BLEND_ONLY_EXECUTION", True), \
             mock.patch.object(_cfg, "MATCHER_PAPER_ENABLED", True), \
             mock.patch.object(nsw, "_paper_record_entry") as rec:
            ok, reason = self._run(cand, self._pkt(None), _decision("BUY_NO"))
        self.assertFalse(ok)
        self.assertIn("blend_only", reason)
        rec.assert_not_called()


class TestHighMakerRouting(_Base):
    """2026-06-03: with PUSH_HIGH_POST_AT_MID, a HIGH blend buy that clears every gate
    is routed to low_post_probe.place (maker-first) instead of execute_buy (taker).
    Flag OFF -> HIGH still takes via execute_buy. sigma=2.5 clears the per-station floor;
    mu=92 outside [88,89] keeps the tail-bet gate clear; tight 2c spread + 48/50 bbo."""
    def _pkt(self):
        return {"yes_ask_c": 50, "no_ask_c": 50, "yes_bid_c": 48, "no_bid_c": 48,
                "seconds_to_close": 10_000, "mu_chosen": 92.0, "floor": 88.0, "cap": 89.0,
                "sigma_chosen": 2.5, "mu_method": "blend_KXHIGH", "push_target_usd": 10.0,
                "local_clock": {"h_to_peak": 3.0, "local_hour": 12.0}}

    def test_high_routes_to_maker_when_flag_on(self):
        import config as _cfg
        import low_post_probe
        cand = _cand("KXHIGHMIA-26MAY20-B88.5", "KMIA")
        with mock.patch.object(_cfg, "BLEND_ONLY_EXECUTION", True), \
             mock.patch.object(_cfg, "PUSH_HIGH_POST_AT_MID", True), \
             mock.patch.object(low_post_probe, "has_resting", return_value=False), \
             mock.patch.object(low_post_probe, "place",
                               return_value=(True, "low_post no @49c")) as place:
            ok, reason = self._run(cand, self._pkt(), _decision("BUY_NO"))
        self.assertTrue(ok)
        place.assert_called_once()           # routed to the maker engine

    def test_high_takes_when_flag_off(self):
        import config as _cfg
        import low_post_probe
        cand = _cand("KXHIGHMIA-26MAY20-B88.5", "KMIA")
        with mock.patch.object(_cfg, "BLEND_ONLY_EXECUTION", True), \
             mock.patch.object(_cfg, "PUSH_HIGH_POST_AT_MID", False), \
             mock.patch.object(low_post_probe, "place") as place:
            ok, reason = self._run(cand, self._pkt(), _decision("BUY_NO"))
        self.assertTrue(ok)
        self.assertIn("executed", reason)    # took via execute_buy (mocked in _run)
        place.assert_not_called()


if __name__ == "__main__":
    unittest.main()
