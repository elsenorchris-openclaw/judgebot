"""Tests for nn_shadow_worker._try_auto_execute (2d) thin-margin BUY_NO gate.

2026-05-23: HIGH B-bracket BUY_NO must be blocked when the CLI-adjusted forecast
(mu - per-station obs->CLI offset) lands INSIDE the bracket [floor-0.5, cap+0.5]
-- shorting a bracket our own mu points into (live-era WR 32% / -3.9c/bet,
edge-independent, both OOS halves). HIGH only; B brackets only; flag-gated by
PUSH_SKIP_NO_MU_NEAR_BRACKET; offset from PUSH_NO_MU_CLI_OFFSET_BY_STATION
(DEFAULT for unlisted stations).
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


def _make_packet(mu, floor=88.0, cap=89.0):
    # h_to_peak high so the (2a) h2pk gate never fires; no yes_bid/ask so the
    # (2-spread) gate is skipped; no_ask_c in [10,80] for the price gate.
    return {
        "yes_ask_c": 50, "no_ask_c": 50, "seconds_to_close": 10_000,
        "mu_chosen": mu, "floor": floor, "cap": cap,
        "local_clock": {"h_to_peak": 2.0},
    }


def _make_decision(direction):
    return {
        "decision": direction, "edge": 0.20,
        "p_yes": 0.30 if direction == "BUY_NO" else 0.70,
        "reason": f"{direction} test edge=20.0pp",
    }


class TestThinMarginGate(unittest.TestCase):

    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window

    def _run(self, cand, packet, decision, series="HIGH", thin_margin=True):
        import paper_judge_bot as pjb
        import kalshi_client
        import config as _cfg
        # Isolate the thin-margin mechanism: disable the per-cell MAE gate (sits
        # earlier in the stack) and PIN PUSH_SKIP_NO_MU_NEAR_BRACKET to the value
        # under test. 2026-06-02: the live default flipped to False ($1 experiment),
        # so these tests must set the flag explicitly to exercise the gate logic.
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(kalshi_client, "get_balance_cached",
                               return_value=100.0), \
             mock.patch.object(_cfg, "PUSH_MAE_GATE_ENABLED", False), \
             mock.patch.object(_cfg, "PUSH_SKIP_NO_MU_NEAR_BRACKET", thin_margin):
            return nsw._try_auto_execute(
                cand, packet, decision, series=series, local_hour=12.0,
            )

    def test_mu_inside_blocked(self):
        """2026-06-08 (band 0.5 / offset 0): mu=88.5 sits inside bracket [88,89]
        -> within [87.5,89.5] -> blocked (shorting a bracket our mu points into)."""
        cand = _make_candidate("KXHIGHMIA-26MAY20-B88.5", "KMIA",
                                "KXHIGH", "2026-05-20")
        executed, reason = self._run(cand, _make_packet(88.5),
                                     _make_decision("BUY_NO"))
        self.assertFalse(executed)
        self.assertIn("thin_margin_no", reason)

    def test_mu_clear_allowed(self):
        """band 0.5 / offset 0: mu=92.0 clear of [87.5,89.5] -> allowed."""
        cand = _make_candidate("KXHIGHMIA-26MAY20-B88.5", "KMIA",
                                "KXHIGH", "2026-05-20")
        executed, reason = self._run(cand, _make_packet(92.0),
                                     _make_decision("BUY_NO"))
        self.assertNotIn("thin_margin_no", reason)

    def test_offset_zero_uniform(self):
        """2026-06-08: offset is now 0 for ALL stations (matcher per-station
        obs->CLI offsets cleared; blend predicts CLI directly). KDCA mu=89.4 ->
        within [87.5,89.5] -> blocked (no per-station offset applied)."""
        cand = _make_candidate("KXHIGHDCA-26MAY20-B88.5", "KDCA",
                                "KXHIGH", "2026-05-20")
        executed, reason = self._run(cand, _make_packet(89.4),
                                     _make_decision("BUY_NO"))
        self.assertFalse(executed)
        self.assertIn("thin_margin_no", reason)

    def test_buy_yes_not_affected(self):
        """Gate is BUY_NO-only: a BUY_YES with mu inside is not gated by it."""
        cand = _make_candidate("KXHIGHMIA-26MAY20-B88.5", "KMIA",
                                "KXHIGH", "2026-05-20")
        executed, reason = self._run(cand, _make_packet(88.5),
                                     _make_decision("BUY_YES"))
        self.assertNotIn("thin_margin_no", reason)

    def test_low_series_not_affected(self):
        """Gate is HIGH-only: LOW BUY_NO with mu inside is not gated by it."""
        cand = _make_candidate("KXLOWMIA-26MAY20-B72.5", "KMIA",
                                "KXLOW", "2026-05-20",
                                floor=72.0, cap=73.0, label=72.5)
        executed, reason = self._run(cand, _make_packet(72.5, floor=72.0, cap=73.0),
                                     _make_decision("BUY_NO"), series="LOW")
        self.assertNotIn("thin_margin_no", reason)

    def test_t_bracket_not_affected(self):
        """Open-ended T bracket (cap=None) is not gated (no in-bracket position;
        the deep-tail T case is handled by USE_TAIL_EMPIRICAL_PYES)."""
        cand = _make_candidate("KXHIGHTMIA-26MAY20-T88", "KMIA",
                               "KXHIGH", "2026-05-20",
                               bracket_kind="T", floor=88.0, cap=None, label=88.0)
        pkt = _make_packet(85.0, floor=88.0, cap=None)
        executed, reason = self._run(cand, pkt, _make_decision("BUY_NO"))
        self.assertNotIn("thin_margin_no", reason)

    def test_flag_disabled(self):
        """PUSH_SKIP_NO_MU_NEAR_BRACKET=False disables the gate (now the live default)."""
        cand = _make_candidate("KXHIGHMIA-26MAY20-B88.5", "KMIA",
                                "KXHIGH", "2026-05-20")
        executed, reason = self._run(cand, _make_packet(90.0),
                                     _make_decision("BUY_NO"), thin_margin=False)
        self.assertNotIn("thin_margin_no", reason)

    def test_default_band_0_5(self):
        """2026-06-08: band tightened 1.5°F -> 0.5°F (offset 0), per-station dicts
        cleared. KDCA bracket [88,89], band [87.5,89.5]. mu=89.0 inside -> BLOCKED
        with band=0.5 in the reason."""
        cand = _make_candidate("KXHIGHDCA-26MAY26-B88.5", "KDCA",
                                "KXHIGH", "2026-05-26")
        executed, reason = self._run(cand, _make_packet(89.0),
                                     _make_decision("BUY_NO"))
        self.assertFalse(executed)
        self.assertIn("thin_margin_no", reason)
        self.assertIn("band=0.5", reason)

    def test_band_0_5_edge_allows(self):
        """The 0.5°F tightening MATTERS at the edge: KDCA bracket [88,89], band
        [87.5,89.5]. mu=87.0 is BELOW 87.5 -> ALLOWED. (Under the old 1.5°F band
        [86.5,90.5] this same bet was BLOCKED -- the tightening recovers the +EV
        tail bets while still cutting the in-bracket coin flips.)"""
        cand = _make_candidate("KXHIGHDCA-26MAY26-B88.5", "KDCA",
                                "KXHIGH", "2026-05-26")
        executed, reason = self._run(cand, _make_packet(87.0),
                                     _make_decision("BUY_NO"))
        self.assertNotIn("thin_margin_no", reason)

    def test_cleared_per_station_overrides(self):
        """2026-06-08: per-station offset+band dicts CLEARED -> every station uses
        the uniform offset 0 / band 0.5. KLAX (formerly band=2.0F override) now
        uses 0.5: mu=86.2, bracket [88,89], band [87.5,89.5] -> 86.2 BELOW ->
        ALLOWED. (The old 2.0F override [86.0,91.0] would have BLOCKED it.)"""
        cand = _make_candidate("KXHIGHLAX-26MAY26-B88.5", "KLAX",
                                "KXHIGH", "2026-05-26")
        executed, reason = self._run(cand, _make_packet(86.2),
                                     _make_decision("BUY_NO"))
        self.assertNotIn("thin_margin_no", reason)

    def test_sigma_floor_blocks_low_sigma(self):
        """2026-05-26: σ_chosen < PUSH_HIGH_NO_MIN_SIGMA_F (default 1.0) -> SKIP.
        Use μ=95 far above cap=89 so the boundary gate doesn't fire first, and
        sigma_chosen=0.8 (below the 1.0 floor) so the σ gate triggers."""
        cand = _make_candidate("KXHIGHMIA-26MAY26-B88.5", "KMIA",
                                "KXHIGH", "2026-05-26")
        packet = _make_packet(95.0)
        packet["sigma_chosen"] = 0.8
        executed, reason = self._run(cand, packet, _make_decision("BUY_NO"))
        self.assertFalse(executed)
        self.assertIn("sigma_floor_no", reason)

    def test_sigma_floor_allows_high_sigma(self):
        """σ_chosen >= floor -> NOT gated by the σ floor. μ=95 far outside
        the boundary band too, so neither gate fires -- trade flows through."""
        cand = _make_candidate("KXHIGHMIA-26MAY26-B88.5", "KMIA",
                                "KXHIGH", "2026-05-26")
        packet = _make_packet(95.0)
        packet["sigma_chosen"] = 1.5
        executed, reason = self._run(cand, packet, _make_decision("BUY_NO"))
        self.assertNotIn("sigma_floor_no", reason)

    def test_sigma_floor_disabled_when_zero(self):
        """PUSH_HIGH_NO_MIN_SIGMA_F=0 + empty per-station dict disables the gate
        even on very low σ. 2026-05-28: per-station override (PUSH_HIGH_NO_MIN_SIGMA_BY_STATION)
        also needs to be cleared since station floors override the global."""
        import config as _cfg
        cand = _make_candidate("KXHIGHMIA-26MAY26-B88.5", "KMIA",
                                "KXHIGH", "2026-05-26")
        packet = _make_packet(95.0)
        packet["sigma_chosen"] = 0.5
        with mock.patch.object(_cfg, "PUSH_HIGH_NO_MIN_SIGMA_F", 0.0), \
             mock.patch.object(_cfg, "PUSH_HIGH_NO_MIN_SIGMA_BY_STATION", {}):
            executed, reason = self._run(cand, packet, _make_decision("BUY_NO"))
        self.assertNotIn("sigma_floor_no", reason)


if __name__ == "__main__":
    unittest.main()
