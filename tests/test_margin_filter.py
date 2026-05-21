"""Tests for the μ-margin prescreen filter (shipped 2026-05-19).

The filter blocks BUY candidates whose μ is too close to (or inside) the
YES window for the chosen side to be a confident bet. See
config.PRESCREEN["margin_*"] for thresholds.

Live failure motivating this:
  KXHIGHPHIL-26MAY18-B96.5  BUY_NO @ 35c × 11 → settled YES, −$3.85
  μ=96.3, σ=2.10, YES=[95.5, 97.5], margin=−0.20°F (μ INSIDE YES)
"""
from __future__ import annotations
import pytest
import sys
sys.path.insert(0, "/home/ubuntu/paper_judge_bot")

import config
from paper_judge_bot import _margin_outside_yes_f


# ─────────────────────────────────────────────────────────────────────────────
# Helper unit tests
# ─────────────────────────────────────────────────────────────────────────────
class TestMarginOutsideYes:
    """_margin_outside_yes_f signed-distance correctness."""

    def test_b_bracket_buy_no_below_yes_supports_undershoot(self):
        # B92.5 floor=92 cap=93 YES=[91.5, 93.5]; μ=88 sits 3.5°F below YES
        m = _margin_outside_yes_f(88.0, 92.0, 93.0, "B", "BUY_NO")
        assert m == pytest.approx(3.5)

    def test_b_bracket_buy_no_above_yes_supports_overshoot(self):
        # μ=95 sits 1.5°F above YES_hi=93.5 → supports BUY_NO overshoot
        m = _margin_outside_yes_f(95.0, 92.0, 93.0, "B", "BUY_NO")
        assert m == pytest.approx(1.5)

    def test_b_bracket_buy_no_mu_inside_yes_negative(self):
        # The PHIL B96.5 scenario: μ=96.3, YES=[95.5, 97.5]
        # μ-yes_lo = 0.8, yes_hi-μ = 1.2 → min = 0.8 → margin = -0.8
        m = _margin_outside_yes_f(96.3, 96.0, 97.0, "B", "BUY_NO")
        assert m == pytest.approx(-0.8)
        assert m < 0  # inside YES — wrong direction

    def test_b_bracket_buy_yes_mu_inside_yes_positive(self):
        # For BUY_YES, μ inside YES is the supportive direction
        m = _margin_outside_yes_f(96.3, 96.0, 97.0, "B", "BUY_YES")
        assert m == pytest.approx(0.8)  # 0.8°F into YES (nearest edge dist)

    def test_b_bracket_buy_yes_mu_outside_yes_negative(self):
        # μ=88 outside YES=[91.5, 93.5] → -3.5
        m = _margin_outside_yes_f(88.0, 92.0, 93.0, "B", "BUY_YES")
        assert m == pytest.approx(-3.5)

    def test_t_warm_buy_no_below_yes(self):
        # T70 floor=70 cap=None; YES if x > 70.5; μ=65 → margin = 70.5-65 = 5.5
        m = _margin_outside_yes_f(65.0, 70.0, None, "T", "BUY_NO")
        assert m == pytest.approx(5.5)

    def test_t_warm_buy_yes_above_yes(self):
        # μ=75 → margin = 75-70.5 = 4.5 (inside YES, supports BUY_YES)
        m = _margin_outside_yes_f(75.0, 70.0, None, "T", "BUY_YES")
        assert m == pytest.approx(4.5)

    def test_t_cold_buy_no_above_yes(self):
        # T58 cap=58 floor=None; YES if x < 57.5; μ=62 → margin = 62-57.5 = 4.5
        m = _margin_outside_yes_f(62.0, None, 58.0, "T", "BUY_NO")
        assert m == pytest.approx(4.5)

    def test_none_inputs_return_none(self):
        assert _margin_outside_yes_f(None, 92.0, 93.0, "B", "BUY_NO") is None
        assert _margin_outside_yes_f(96.0, None, None, "B", "BUY_NO") is None
        assert _margin_outside_yes_f(96.0, 92.0, 93.0, None, "BUY_NO") is None
        assert _margin_outside_yes_f(96.0, 92.0, 93.0, "B", None) is None


# ─────────────────────────────────────────────────────────────────────────────
# Integration: prescreen behavior with the margin filter
# ─────────────────────────────────────────────────────────────────────────────
class TestPrescreenMarginFilter:
    """Verifies the prescreen gate fires + bypasses correctly."""

    def _build_packet(self, side, mu, sigma, floor=92.0, cap=93.0, rm=88.0,
                      past_peak=False, past_min=False, bracket_kind="B"):
        """Construct a packet where _numerical_edge will return the desired
        side/mu/sigma. Easiest: pre-inject _edge_info so prescreen's call to
        _numerical_edge has clear fields to work from.

        We provide enough other fields for prescreen's prior gates to pass.
        """
        import time
        return {
            "ticker": "KXHIGHTEST-26MAY19-B92.5",
            "series": "KXHIGH" if bracket_kind == "B" else "KXHIGH",
            "station": "KTEST",
            "climate_day": "2026-05-19",
            "bracket_kind": bracket_kind,
            "floor": floor, "cap": cap,
            "yes_bid_c": 40, "yes_ask_c": 42,
            "no_bid_c": 58, "no_ask_c": 60,
            "spread_c": 2,
            "seconds_to_close": 6 * 3600,
            "days_out": 0,
            # mu/sigma_chosen so _numerical_edge can short-circuit to nn_match
            "mu_chosen": mu, "sigma_chosen": sigma,
            "mu_nbm": mu, "mu_hrrr": mu, "mu_nbp": mu, "mu_ecmwf": mu,
            "wethr_obs": {"temp_f": rm - 1.0, "ts": time.time()},
            "obs_trend_30m": 0.0,
            "local_clock": {"local_hour": 14.5, "peak_hour_local": 16.0,
                            "min_hour_local": 5.0,
                            "past_peak_today": past_peak,
                            "past_min_today": past_min},
            "running_min_or_max": rm,
            "rm_validation": {"ok": True, "reason": "ok",
                              "secs_into_climate_day": 50000},
            "model_mae_recent": {"per_model": {
                "NBM": {"mae": 1.0, "bias": 0.0}}},
            # Pre-inject edge_info to control the side prescreen sees
            "_edge_info": {
                "side": side,
                "mu": mu, "sigma": sigma,
                "edge": 0.10,
                "prob": 0.65,
                "mu_method": "nn_match_high_n50",
            },
        }

    def test_filter_disabled_passes_marginal(self, monkeypatch):
        """When margin_filter_enabled=False, the gate must not fire."""
        from paper_judge_bot import prescreen
        monkeypatch.setitem(config.PRESCREEN, "margin_filter_enabled", False)
        # PHIL-like: μ inside YES window, would have been blocked
        p = self._build_packet("BUY_NO", mu=96.3, sigma=2.10,
                                floor=96.0, cap=97.0, rm=92.0)
        r = prescreen(p)
        # may fail other gates, but NOT for margin reason
        assert r is None or ("margin" not in r and "σ" not in r)

    def test_phil_scenario_blocks(self, monkeypatch):
        """The exact PHIL B96.5 scenario must be blocked."""
        from paper_judge_bot import prescreen
        monkeypatch.setitem(config.PRESCREEN, "margin_filter_enabled", True)
        monkeypatch.setitem(config.PRESCREEN, "skip_unless_nn_match", False)
        monkeypatch.setitem(config.PRESCREEN, "skip_forecast_only_mu", False)
        p = self._build_packet("BUY_NO", mu=96.3, sigma=2.10,
                                floor=96.0, cap=97.0, rm=92.0)
        r = prescreen(p)
        assert r is not None
        assert "margin" in r.lower() or "σ" in r

    def test_buy_no_with_sufficient_margin_passes(self, monkeypatch):
        """BUY_NO with μ ≥ 1.5σ outside YES window passes the margin gate."""
        from paper_judge_bot import prescreen
        monkeypatch.setitem(config.PRESCREEN, "margin_filter_enabled", True)
        monkeypatch.setitem(config.PRESCREEN, "skip_unless_nn_match", False)
        # μ=87, σ=1.5, YES=[91.5, 93.5] → margin=4.5°F ≥ 1.5×1.5=2.25 ✓
        p = self._build_packet("BUY_NO", mu=87.0, sigma=1.5,
                                floor=92.0, cap=93.0, rm=85.0)
        r = prescreen(p)
        assert r is None or ("margin" not in r and "σ" not in r)

    def test_buy_yes_with_sufficient_margin_passes(self, monkeypatch):
        """BUY_YES with μ inside YES by ≥1.0σ passes."""
        from paper_judge_bot import prescreen
        monkeypatch.setitem(config.PRESCREEN, "margin_filter_enabled", True)
        monkeypatch.setitem(config.PRESCREEN, "skip_unless_nn_match", False)
        # μ=92.5 centered in YES=[91.5, 93.5], σ=0.5 → margin=1.0 ≥ 0.5 ✓
        p = self._build_packet("BUY_YES", mu=92.5, sigma=0.5,
                                floor=92.0, cap=93.0, rm=90.0)
        r = prescreen(p)
        assert r is None or ("margin" not in r and "σ" not in r)

    def test_sigma_cap_blocks(self, monkeypatch):
        """σ > σ_max_f blocks even if margin is sufficient."""
        from paper_judge_bot import prescreen
        monkeypatch.setitem(config.PRESCREEN, "margin_filter_enabled", True)
        monkeypatch.setitem(config.PRESCREEN, "margin_max_sigma_f", 2.5)
        monkeypatch.setitem(config.PRESCREEN, "skip_unless_nn_match", False)
        monkeypatch.setitem(config.PRESCREEN, "skip_forecast_only_mu", False)
        # μ=80, σ=3.5 (huge), YES=[91.5, 93.5] → margin=11.5°F ≥ 5.25 ✓
        # But σ=3.5 > σ_cap=2.5 → still blocks
        p = self._build_packet("BUY_NO", mu=80.0, sigma=3.5,
                                floor=92.0, cap=93.0, rm=78.0)
        r = prescreen(p)
        assert r is not None
        assert "σ" in r and "cap" in r.lower()

    def test_rm_lock_bypasses_filter(self, monkeypatch):
        """rm-lock means physical settlement — margin uncertainty no longer matters."""
        from paper_judge_bot import prescreen
        monkeypatch.setitem(config.PRESCREEN, "margin_filter_enabled", True)
        monkeypatch.setitem(config.PRESCREEN, "margin_filter_bypass_when_rm_locked", True)
        monkeypatch.setitem(config.PRESCREEN, "skip_unless_nn_match", False)
        # PHIL-like but with rm-lock: rm=95 ≥ cap+1=98 would trigger HIGH BUY_NO
        # overshoot lock (rm ≥ cap+1.0). Set rm above cap+1.
        p = self._build_packet("BUY_NO", mu=96.3, sigma=2.10,
                                floor=96.0, cap=97.0, rm=99.0)
        r = prescreen(p)
        # margin would normally block, but rm-lock bypasses
        assert r is None or "margin" not in (r or "")

    def test_buy_no_inside_yes_blocks(self, monkeypatch):
        """μ inside YES + BUY_NO + no lock → blocked even with reasonable σ.

        Requires σ wide enough that _numerical_edge naturally picks BUY_NO
        (P(NO) > P(YES)). For μ at YES center σ ≥ ~1.5°F gives tail mass favor.
        """
        from paper_judge_bot import prescreen
        monkeypatch.setitem(config.PRESCREEN, "margin_filter_enabled", True)
        monkeypatch.setitem(config.PRESCREEN, "skip_unless_nn_match", False)
        monkeypatch.setitem(config.PRESCREEN, "skip_forecast_only_mu", False)
        # μ=92.5 centered in YES=[91.5,93.5], σ=2.0 → P(YES)=0.383 P(NO)=0.617
        # Market: no_ask=42c → NO edge = 0.617-0.42=0.197 (passes 6pp gate)
        # margin = -min(1.0, 1.0) = -1.0  vs required 1.5×2.0=3.0 → blocks
        p = self._build_packet("BUY_NO", mu=92.5, sigma=2.0,
                                floor=92.0, cap=93.0, rm=90.0)
        # Override market to make NO side cheap → bot picks NO
        p["yes_ask_c"] = 75; p["no_ask_c"] = 27
        r = prescreen(p)
        assert r is not None
        assert "margin" in r.lower() or "σ" in r
