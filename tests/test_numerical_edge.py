"""Tests for _numerical_edge — bracket math + CLI rounding."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from paper_judge_bot import _numerical_edge


def _packet(floor=None, cap=None, mu=70.0, sigma=2.0,
            yes_ask_c=50, no_ask_c=50):
    """Build a minimal packet for _numerical_edge."""
    return {
        "floor": floor, "cap": cap,
        "mu_nbm": mu,  # single source — μ = median = that value
        "nbp_sigma": sigma,
        "yes_ask_c": yes_ask_c, "no_ask_c": no_ask_c,
    }


class TestBracketMath:
    """Verify CLI-rounding ±0.5°F shifts in P(YES) for each shape."""

    def test_b_bracket_window_is_2f_wide(self):
        # B45.5 bracket: floor=45, cap=46. True YES window [44.5, 46.5).
        # At μ=45.5 (dead-center), σ=1.0, P(YES) should be high (~0.68).
        p = _packet(floor=45, cap=46, mu=45.5, sigma=1.0)
        info = _numerical_edge(p, default_sigma=2.0)
        assert info is not None
        # P(YES) = Φ((46.5-45.5)/1.0) - Φ((44.5-45.5)/1.0)
        #        = Φ(1) - Φ(-1) = 0.8413 - 0.1587 = 0.6826
        # 1-Φ(1) for outside == 1 - 0.6826 = 0.3174 → P(NO) ≈ 0.32
        # So edge_BUY_NO = 0.32 - 0.50 = -0.18 (negative).
        # edge_BUY_YES = 0.68 - 0.50 = +0.18.
        assert info["side"] == "BUY_YES"
        assert 0.65 < info["prob"] < 0.71

    def test_b_bracket_buy_no_at_high_mu(self):
        # B45.5, μ way above bracket → P(NO) high.
        p = _packet(floor=45, cap=46, mu=60.0, sigma=2.0)
        info = _numerical_edge(p, default_sigma=2.0)
        assert info is not None
        assert info["side"] == "BUY_NO"
        assert info["prob"] > 0.95  # P(NO)

    def test_t_warm_tail_window_uses_floor_plus_half(self):
        # KXLOWTMIN-T59 warm tail: floor=59, cap=None. YES if true ≥ 59.5.
        # At μ=59.5, P(YES) = 1 - Φ(0) = 0.5 exactly.
        p = _packet(floor=59, cap=None, mu=59.5, sigma=1.0)
        info = _numerical_edge(p, default_sigma=2.0)
        assert info is not None
        # Both sides have the same prob → edge tie. P(YES)=0.5, P(NO)=0.5.
        # At yes_ask=50c, no_ask=50c → both edges = 0.
        assert 0.49 <= info["prob"] <= 0.51

    def test_t_cold_tail_window_uses_cap_minus_half(self):
        # KXHIGHCHI-T71 cold tail: cap=71, floor=None. YES if true < 70.5.
        # At μ=70.5, P(YES) = Φ(0) = 0.5 exactly.
        p = _packet(floor=None, cap=71, mu=70.5, sigma=1.0)
        info = _numerical_edge(p, default_sigma=2.0)
        assert info is not None
        assert 0.49 <= info["prob"] <= 0.51

    def test_t_warm_tail_clear_yes(self):
        # KXLOWTMIN-T59: μ way above floor → YES (warm low) very likely.
        p = _packet(floor=59, cap=None, mu=68.0, sigma=2.0,
                    yes_ask_c=80, no_ask_c=22)
        info = _numerical_edge(p, default_sigma=2.0)
        assert info is not None
        # P(YES) = 1 - Φ((59.5-68)/2) = 1 - Φ(-4.25) ≈ 1.0
        assert info["side"] == "BUY_YES"
        assert info["prob"] > 0.99

    def test_t_cold_tail_clear_no(self):
        # KXHIGHCHI-T71 cold tail with μ well above cap → NO (HIGH won't drop).
        p = _packet(floor=None, cap=71, mu=85.0, sigma=2.0,
                    yes_ask_c=10, no_ask_c=92)
        info = _numerical_edge(p, default_sigma=2.0)
        assert info is not None
        # P(YES) = Φ((70.5-85)/2) ≈ 0 → P(NO) ≈ 1
        assert info["side"] == "BUY_NO"
        assert info["prob"] > 0.99


class TestNoBracketSet:
    def test_returns_none_with_no_bracket(self):
        p = _packet(floor=None, cap=None, mu=70.0, sigma=2.0)
        info = _numerical_edge(p, default_sigma=2.0)
        assert info is None
