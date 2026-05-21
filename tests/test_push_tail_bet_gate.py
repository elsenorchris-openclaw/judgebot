"""Tests for nn_shadow_worker._try_auto_execute in-bracket tail-bet gate (Gate 2).

2026-05-20: When the nn mu sits INSIDE the YES window [floor-0.5, cap+0.5) but
the bot picks the smaller-mass (tail) side (p_chosen < 0.5), it is betting
against its own central estimate for a thin edge. The gate raises the edge
floor to PUSH_TAIL_BET_MIN_EDGE_PP for those trades only.

Backtest (5/19+5/20 settled pure-nn pool): 4 blocks, 4 losers, 0 winners,
+$13.87. Sibling Gate 1 (boundary-gap) was parked -- it killed real winners.

2026-05-20: extended from B-only to T-brackets (YES window computed per shape,
mirroring nn_shadow_strategy._yes_window) after HOU T84 BUY_NO slipped past the
B-only gate and lost -$5.16.

These tests pin the gate trigger (mu in YES + tail side + edge below floor)
and confirm it does NOT fire on non-tail setups or when disabled.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402


def _make_candidate():
    return market_universe.Candidate(
        ticker="KXHIGHPHIL-26MAY20-B95.5",
        series_prefix="KXHIGH",
        city_code="PHIL",
        station="KPHL",
        climate_day="2026-05-20",
        bracket_kind="B",
        floor=95.0,
        cap=96.0,
        bracket_label=95.5,
        market={},
    )


def _make_packet(mu_chosen, floor=95.0, cap=96.0, bracket_kind="B"):
    return {
        "yes_ask_c": 50,
        "no_ask_c": 50,
        "seconds_to_close": 10_000,
        "wethr_obs": {"visibility_miles": 10.0, "wind_speed_mph": 5.0},
        "mu_chosen": mu_chosen,
        "floor": floor,
        "cap": cap,
        "bracket_kind": bracket_kind,
        "local_clock": {"h_to_peak": 2.0},
    }


def _make_decision(direction, edge, p_yes):
    return {
        "decision": direction,
        "edge": edge,
        "p_yes": p_yes,
        "reason": f"{direction} test edge={edge*100:.1f}pp",
    }


class TestPushTailBetGate(unittest.TestCase):

    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})
        self._orig_tail = getattr(config, "PUSH_TAIL_BET_MIN_EDGE_PP", None)
        self._orig_min_edge = getattr(config, "PUSH_MIN_EDGE_PP", None)
        self._orig_h2pk = getattr(config, "PUSH_MIN_H_TO_PEAK_HIGH", None)
        config.PUSH_TAIL_BET_MIN_EDGE_PP = 25
        config.PUSH_MIN_EDGE_PP = 12
        config.PUSH_MIN_H_TO_PEAK_HIGH = 0.5
        self._orig_no = getattr(config, "AUTO_EXECUTE_BUY_NO_PUSH", None)
        self._orig_yes = getattr(config, "AUTO_EXECUTE_BUY_YES_PUSH", None)
        config.AUTO_EXECUTE_BUY_NO_PUSH = True
        config.AUTO_EXECUTE_BUY_YES_PUSH = True

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window
        if self._orig_tail is not None:
            config.PUSH_TAIL_BET_MIN_EDGE_PP = self._orig_tail
        if self._orig_min_edge is not None:
            config.PUSH_MIN_EDGE_PP = self._orig_min_edge
        if self._orig_h2pk is not None:
            config.PUSH_MIN_H_TO_PEAK_HIGH = self._orig_h2pk
        if self._orig_no is not None:
            config.AUTO_EXECUTE_BUY_NO_PUSH = self._orig_no
        if self._orig_yes is not None:
            config.AUTO_EXECUTE_BUY_YES_PUSH = self._orig_yes

    def _run(self, packet, decision, series="HIGH", local_hour=12.0):
        import paper_judge_bot as pjb
        import kalshi_client
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(kalshi_client, "get_balance_cached",
                               return_value=100.0):
            return nsw._try_auto_execute(
                _make_candidate(), packet, decision,
                series=series, local_hour=local_hour,
            )

    # ── Tail-bet trigger ────────────────────────────────────────────────
    def test_tail_bet_low_edge_blocked(self):
        # mu=95.52 in YES window [94.5, 96.5); BUY_NO with p_yes=0.69 ->
        # p_chosen(NO)=0.31 < 0.5; edge=13pp < 25pp floor -> blocked.
        packet = _make_packet(mu_chosen=95.52)
        decision = _make_decision("BUY_NO", edge=0.13, p_yes=0.69)
        executed, reason = self._run(packet, decision)
        self.assertFalse(executed)
        self.assertIn("edge_below_floor", reason)
        self.assertIn("tail_bet", reason)
        self.assertIn("25pp", reason)

    def test_tail_bet_high_edge_passes_gate(self):
        # Same tail structure but edge=30pp >= 25pp -> tail gate does not block.
        packet = _make_packet(mu_chosen=95.52)
        decision = _make_decision("BUY_NO", edge=0.30, p_yes=0.69)
        executed, reason = self._run(packet, decision)
        self.assertNotIn("tail_bet", reason)
        self.assertNotIn("edge_below_floor", reason)

    def test_buy_yes_tail_bet_blocked(self):
        # mu in YES window, BUY_YES with p_yes=0.40 -> p_chosen(YES)=0.40 < 0.5;
        # edge=15pp < 25pp -> blocked.
        packet = _make_packet(mu_chosen=95.50)
        decision = _make_decision("BUY_YES", edge=0.15, p_yes=0.40)
        executed, reason = self._run(packet, decision)
        self.assertFalse(executed)
        self.assertIn("tail_bet", reason)

    # ── Non-tail setups unaffected ──────────────────────────────────────
    def test_majority_side_not_tail_passes(self):
        # mu in YES window, BUY_NO with p_yes=0.30 -> p_chosen(NO)=0.70 >= 0.5;
        # NOT a tail bet. Base 12pp floor applies; edge=13pp passes.
        packet = _make_packet(mu_chosen=95.50)
        decision = _make_decision("BUY_NO", edge=0.13, p_yes=0.30)
        executed, reason = self._run(packet, decision)
        self.assertNotIn("tail_bet", reason)

    def test_mu_outside_bracket_not_tail(self):
        # mu=98.0 above YES window top 96.5 -> not in-bracket; tail gate skipped.
        # BUY_NO with p_yes=0.20 (p_chosen=0.80), edge=13pp > base 12pp -> passes.
        packet = _make_packet(mu_chosen=98.0)
        decision = _make_decision("BUY_NO", edge=0.13, p_yes=0.20)
        executed, reason = self._run(packet, decision)
        self.assertNotIn("tail_bet", reason)

    def test_base_edge_floor_still_applies_to_tail_passing_setup(self):
        # Non-tail but below base floor: edge=8pp < 12pp base -> blocked,
        # but reason must NOT mention tail_bet.
        packet = _make_packet(mu_chosen=98.0)
        decision = _make_decision("BUY_NO", edge=0.08, p_yes=0.20)
        executed, reason = self._run(packet, decision)
        self.assertFalse(executed)
        self.assertIn("edge_below_floor", reason)
        self.assertNotIn("tail_bet", reason)

    # ── Disable knob ────────────────────────────────────────────────────
    def test_zero_threshold_disables_gate(self):
        config.PUSH_TAIL_BET_MIN_EDGE_PP = 0
        packet = _make_packet(mu_chosen=95.52)
        decision = _make_decision("BUY_NO", edge=0.13, p_yes=0.69)
        executed, reason = self._run(packet, decision)
        self.assertNotIn("tail_bet", reason)

    # ── T-bracket coverage (2026-05-20 extension) ──────────────────────
    def test_t_cold_tail_bet_blocked(self):
        # T-cold (cap=84, floor=None): YES window (-inf, 83.5). mu=83.0 in YES;
        # BUY_NO with p_yes=0.59 -> p_chosen(NO)=0.41 < 0.5; edge=20pp < 25 ->
        # blocked. This is the HOU T84 loser the B-only gate missed.
        packet = _make_packet(mu_chosen=83.0, floor=None, cap=84.0,
                              bracket_kind="T")
        decision = _make_decision("BUY_NO", edge=0.20, p_yes=0.59)
        executed, reason = self._run(packet, decision)
        self.assertFalse(executed)
        self.assertIn("tail_bet", reason)

    def test_t_warm_tail_bet_blocked(self):
        # T-warm (floor=70, cap=None): YES window [70.5, +inf). mu=71.0 in YES;
        # BUY_NO with p_yes=0.62 -> p_chosen(NO)=0.38 < 0.5; edge=20pp -> blocked.
        packet = _make_packet(mu_chosen=71.0, floor=70.0, cap=None,
                              bracket_kind="T")
        decision = _make_decision("BUY_NO", edge=0.20, p_yes=0.62)
        executed, reason = self._run(packet, decision)
        self.assertFalse(executed)
        self.assertIn("tail_bet", reason)

    def test_t_bracket_mu_outside_yes_not_tail(self):
        # T-cold (cap=84): mu=90 is above the YES region top (83.5) -> not in
        # YES -> tail gate does not fire.
        packet = _make_packet(mu_chosen=90.0, floor=None, cap=84.0,
                              bracket_kind="T")
        decision = _make_decision("BUY_NO", edge=0.13, p_yes=0.20)
        executed, reason = self._run(packet, decision)
        self.assertNotIn("tail_bet", reason)


if __name__ == "__main__":
    unittest.main()
