"""Unit tests for nn_shadow_strategy.pure_nn_decide."""
import sys
sys.path.insert(0, "/home/ubuntu/paper_judge_bot")

import unittest
import nn_shadow_strategy as nss


def _pkt(**overrides):
    """Build a minimal valid packet."""
    base = {
        "ticker": "KXHIGHATL-26MAY18-B83.5",
        "series": "KXHIGH",
        "floor": 83, "cap": 84, "bracket_kind": "B",
        "yes_ask_c": 38, "no_ask_c": 64,
        "mu_method": "nn_match_high_n50",
        "mu_chosen": 78.9,
        "sigma_chosen": 1.3,
        "running_min_or_max": 79.0,
        "days_out": 0,
        "local_clock": {"past_peak_today": True, "past_min_today": True},
    }
    base.update(overrides)
    return base


class TestPureNnDecide(unittest.TestCase):

    def test_canonical_rm_locked_buy_no(self):
        """Past-peak HIGH, rm well below floor, μ also below — clean BUY_NO with rm-lock bypass."""
        d = nss.pure_nn_decide(_pkt())
        self.assertEqual(d["decision"], "BUY_NO")
        self.assertGreater(d["edge"], 0.30)
        self.assertTrue(d["rm_locked"])
        self.assertGreaterEqual(d["qty"], 1)

    def test_skip_when_no_nn_match(self):
        d = nss.pure_nn_decide(_pkt(mu_method="best_mae_NBM"))
        self.assertEqual(d["decision"], "SKIP")
        self.assertIn("not nn_match", d["reason"])

    def test_skip_when_edge_below_floor(self):
        # μ in middle of YES window → low edge on both sides
        d = nss.pure_nn_decide(_pkt(mu_chosen=83.5, sigma_chosen=2.0,
                                     running_min_or_max=83.5))
        self.assertEqual(d["decision"], "SKIP")
        self.assertIn("< 0.06", d["reason"])

    def test_skip_when_edge_above_ceiling_no_lock(self):
        # μ way below floor, rm also below but NOT past peak → no lock.
        # Edge on BUY_NO huge (~36pp) → exceeds 25pp ceiling, SKIP.
        d = nss.pure_nn_decide(_pkt(mu_chosen=70.0, sigma_chosen=1.0,
                                     running_min_or_max=70.0,
                                     local_clock={"past_peak_today": False,
                                                  "past_min_today": True}))
        self.assertEqual(d["decision"], "SKIP")
        self.assertIn("without rm-lock", d["reason"])

    def test_rm_anchor_overrides_mu(self):
        # HIGH: rm above μ; rm-anchor should set μ = rm
        d = nss.pure_nn_decide(_pkt(mu_chosen=75.0, sigma_chosen=1.0,
                                     running_min_or_max=80.0))
        # After anchoring μ=80, both YES and NO probs change; edge still BUY_NO
        self.assertIn(d["decision"], ("BUY_NO", "SKIP"))

    def test_low_side_buy_no_when_already_below(self):
        d = nss.pure_nn_decide(_pkt(
            ticker="KXLOWTATL-26MAY18-B70.5",
            series="KXLOWT",
            floor=70, cap=71, bracket_kind="B",
            yes_ask_c=15, no_ask_c=87,
            mu_method="nn_match_low_n50",
            mu_chosen=65.0, sigma_chosen=1.2,
            running_min_or_max=64.0,
            local_clock={"past_peak_today": True, "past_min_today": True},
        ))
        self.assertEqual(d["decision"], "BUY_NO")
        self.assertTrue(d["rm_locked"])

    def test_t_warm_tail(self):
        d = nss.pure_nn_decide(_pkt(
            ticker="KXHIGHTATL-26MAY18-T70",
            floor=70, cap=None, bracket_kind="T",
            yes_ask_c=85, no_ask_c=16,
            mu_chosen=64.6, sigma_chosen=1.4,
            running_min_or_max=64.0,
            local_clock={"past_peak_today": True, "past_min_today": True},
        ))
        self.assertEqual(d["decision"], "BUY_NO")
        self.assertGreater(d["edge"], 0.50)
        self.assertTrue(d["rm_locked"])

    def test_t_cold_tail_low_locked(self):
        # LOW T-cold "Will LOW be < 47°?" rm already 44 → LOW already below
        # the threshold → BUY_YES rm-locked (LOW BUY_YES T-cold case).
        d = nss.pure_nn_decide(_pkt(
            ticker="KXLOWTPHX-26MAY18-T47",
            series="KXLOW",
            floor=None, cap=47, bracket_kind="T",
            yes_ask_c=85, no_ask_c=16,
            mu_method="nn_match_low_n50",
            mu_chosen=44.0, sigma_chosen=1.0,
            running_min_or_max=44.0,
            local_clock={"past_peak_today": True, "past_min_today": True},
        ))
        self.assertEqual(d["decision"], "BUY_YES")
        self.assertTrue(d["rm_locked"])
        self.assertGreater(d["edge"], 0.10)

    def test_t_cold_high_no_lock_path(self):
        # HIGH T-cold has no rm-lock branch in the bot's _is_rm_locked_for_side
        # — match that. Even with a huge edge, BUY_YES SKIPs over the ceiling.
        d = nss.pure_nn_decide(_pkt(
            ticker="KXHIGHCHI-26MAY18-T71",
            series="KXHIGH",
            floor=None, cap=71, bracket_kind="T",
            yes_ask_c=10, no_ask_c=92,
            mu_chosen=60.0, sigma_chosen=2.0,
            running_min_or_max=62.0,
            local_clock={"past_peak_today": True, "past_min_today": True},
        ))
        self.assertEqual(d["decision"], "SKIP")
        self.assertFalse(d["rm_locked"])
        self.assertIn("without rm-lock", d["reason"])

    def test_size_respects_min_buy(self):
        # 50c ask, $5 cap, $1 min buy → qty=10, $5
        d = nss.pure_nn_decide(_pkt(no_ask_c=50, mu_chosen=78.0))
        self.assertGreater(d["qty"], 5)
        self.assertLessEqual(d["size_usd"], 5.0)

    def test_size_zero_when_no_headroom(self):
        d = nss.pure_nn_decide(_pkt(), ticker_remaining_usd=0.0)
        self.assertEqual(d["decision"], "SKIP")
        self.assertIn("size=0", d["reason"])

    def test_returns_p_yes_for_skip_too(self):
        # Even on SKIP we should have computed p_yes
        d = nss.pure_nn_decide(_pkt(mu_chosen=83.5, sigma_chosen=2.0,
                                     running_min_or_max=83.5))
        self.assertIsNotNone(d["p_yes"])


class TestRmTruncation(unittest.TestCase):
    """The MSP-style fix: when rm is inside YES window, untruncated math
    assigns probability mass to physically-impossible 'below rm' outcomes.
    Truncation removes that mass and gives a smaller (more honest) edge
    on BUY_NO. Validates issue Chris flagged 2026-05-18 ~21:00 UTC."""

    def test_truncation_reduces_buy_no_edge_when_rm_in_yes(self):
        # KXHIGHTMIN-B66.5 case. nn raw μ=62.4, σ=2.53. rm=66 (inside YES).
        # Untruncated: P(YES)=0.30 → edge_no=0.68. Truncated should be smaller.
        d = nss.pure_nn_decide(_pkt(
            floor=66, cap=67,
            yes_ask_c=99, no_ask_c=2,
            mu_chosen=62.4, sigma_chosen=2.53,
            running_min_or_max=66.0,
            local_clock={"past_peak_today": True, "past_min_today": True},
        ))
        # Should be SKIP (rm-locked path) — rm=66 floor-1=65 condition not met
        # but the truncated P(YES|C) is computed via the proper formula.
        # Just verify p_yes < 0.5 (NO is still favored after truncation) and
        # that the edge is smaller than the untruncated 67.8pp.
        self.assertIsNotNone(d["p_yes"])
        # With rm-anchor + truncation, P(YES) should be ~0.44 (not 0.30).
        self.assertGreater(d["p_yes"], 0.30)
        self.assertLess(d["p_yes"], 0.60)
        # Edge_no = (1 - p_yes) - 0.02. Should be <= 70pp and > 30pp.
        if d["side"] == "BUY_NO" and d["edge"] is not None:
            self.assertLess(d["edge"], 0.70)
            self.assertGreater(d["edge"], 0.30)

    def test_truncation_when_rm_above_yes_window(self):
        # rm already overshot the YES window: P(YES | day_high ≥ rm) = 0.
        d = nss.pure_nn_decide(_pkt(
            floor=70, cap=71,
            yes_ask_c=80, no_ask_c=21,
            mu_chosen=68.0, sigma_chosen=1.5,
            running_min_or_max=73.0,  # above cap+0.5=71.5
            local_clock={"past_peak_today": True, "past_min_today": True},
        ))
        # day_high ≥ 73 → can NEVER fall in [69.5, 71.5) → P(YES)=0
        self.assertEqual(d["p_yes"], 0.0)
        # rm-locked HIGH overshoot rule → BUY_NO
        self.assertEqual(d["decision"], "BUY_NO")
        self.assertTrue(d["rm_locked"])

    def test_truncation_when_rm_below_yes_window_high(self):
        # HIGH pre-peak: rm below YES floor. Truncation barely changes math
        # because most of the probability mass is already above rm.
        d = nss.pure_nn_decide(_pkt(
            floor=83, cap=84,
            yes_ask_c=38, no_ask_c=64,
            mu_chosen=78.9, sigma_chosen=1.3,
            running_min_or_max=79.0,
            local_clock={"past_peak_today": True, "past_min_today": True},
        ))
        # Should still BUY_NO with a similar edge to the untruncated case
        self.assertEqual(d["decision"], "BUY_NO")
        self.assertTrue(d["rm_locked"])
        self.assertGreater(d["edge"], 0.30)

    def test_low_side_rm_truncates_above(self):
        # LOW: day_low ≤ rm. If rm=64 and YES is [69.5, 71.5), constraint
        # and YES don't overlap → P(YES|C) = 0.
        d = nss.pure_nn_decide(_pkt(
            ticker="KXLOWTATL-26MAY18-B70.5",
            series="KXLOW",
            floor=70, cap=71,
            yes_ask_c=15, no_ask_c=87,
            mu_method="nn_match_low_n50",
            mu_chosen=66.0, sigma_chosen=1.2,
            running_min_or_max=64.0,
            local_clock={"past_peak_today": True, "past_min_today": True},
        ))
        self.assertEqual(d["p_yes"], 0.0)
        self.assertEqual(d["decision"], "BUY_NO")


class TestRmLocked(unittest.TestCase):

    def test_high_overshoot(self):
        locked, _ = nss._rm_locked({
            "running_min_or_max": 86.0, "cap": 84, "floor": 83,
            "series": "KXHIGH",
            "local_clock": {"past_peak_today": False, "past_min_today": False},
        }, "BUY_NO")
        self.assertTrue(locked)

    def test_high_undershoot_past_peak(self):
        locked, _ = nss._rm_locked({
            "running_min_or_max": 79.0, "cap": 84, "floor": 83,
            "series": "KXHIGH",
            "local_clock": {"past_peak_today": True, "past_min_today": True},
        }, "BUY_NO")
        self.assertTrue(locked)

    def test_high_undershoot_not_past_peak(self):
        locked, _ = nss._rm_locked({
            "running_min_or_max": 79.0, "cap": 84, "floor": 83,
            "series": "KXHIGH",
            "local_clock": {"past_peak_today": False, "past_min_today": False},
        }, "BUY_NO")
        self.assertFalse(locked)


if __name__ == "__main__":
    unittest.main()
