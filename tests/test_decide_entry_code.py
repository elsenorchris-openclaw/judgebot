"""Regression tests for decide_entry_code.py — the pure-code shadow path."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from decide_entry_code import decide_entry_code, _phi, _compute_p_yes


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────────
def test_phi_at_zero_is_half():
    assert _phi(0) == pytest.approx(0.5, abs=1e-6)


def test_phi_at_plus_two_sigma():
    assert _phi(2.0) == pytest.approx(0.9772, abs=1e-3)


def test_p_yes_b_bracket_mu_at_center():
    # μ at bracket midpoint, σ=1, window [84.5, 86.5)
    # P = Φ(1) - Φ(-1) = 0.8413 - 0.1587 = 0.6826
    p = _compute_p_yes(mu=85.5, sigma=1.0, floor=85.0, cap=86.0, kind="B")
    assert 0.65 < p < 0.72


def test_p_yes_t_warm_tail():
    # μ well above threshold → P(YES) high
    p = _compute_p_yes(mu=88.0, sigma=1.0, floor=85.0, cap=None, kind="T")
    assert p > 0.99


def test_p_yes_t_cold_tail():
    # μ well below cap → P(YES) high
    p = _compute_p_yes(mu=60.0, sigma=1.0, floor=None, cap=70.0, kind="T")
    assert p > 0.99


def test_p_yes_missing_sigma_returns_none():
    assert _compute_p_yes(mu=80, sigma=0, floor=80, cap=81, kind="B") is None


# ─────────────────────────────────────────────────────────────────────────────
# decide_entry_code — guard against non-nn_match
# ─────────────────────────────────────────────────────────────────────────────
def test_non_nn_match_skips():
    pkt = {"mu_method": "best_mae_NBM"}
    d = decide_entry_code(pkt)
    assert d.decision == "SKIP"
    assert "non-nn_match" in (d.skip_reason or "")


def test_missing_mu_skips():
    pkt = {"mu_method": "nn_match_high_n30"}
    d = decide_entry_code(pkt)
    assert d.decision == "SKIP"
    assert "missing" in (d.skip_reason or "").lower()


# ─────────────────────────────────────────────────────────────────────────────
# Full BUY path
# ─────────────────────────────────────────────────────────────────────────────
def _packet_strong_buy_no_high():
    """nn_match HIGH BUY_NO: μ way below floor, stays-below path."""
    return {
        "mu_method": "nn_match_high_n30",
        "mu_chosen": 75.0,
        "sigma_chosen": 1.5,
        "series": "KXHIGH",
        "station": "KMDW",
        "bracket_kind": "B",
        "floor": 85.0, "cap": 86.0,
        "yes_bid_c": 15, "yes_ask_c": 20,
        "no_bid_c": 80, "no_ask_c": 85,
        "running_min_or_max": 73.0,
        "wethr_obs": {"temp_f": 72.0, "high_f": 73.0,
                      "highest_probable_f": 73.0, "lowest_probable_f": 70.0},
        "local_clock": {"local_hour": 13.0, "peak_hour_local": 15.0,
                        "min_hour_local": 5.0},
        "obs_trend_60m_regression": {"slope_f_per_h": -0.5, "r_squared": 0.4},
    }


def test_strong_buy_no_high_returns_buy_no():
    pkt = _packet_strong_buy_no_high()
    d = decide_entry_code(pkt)
    assert d.decision == "BUY_NO"
    assert d.conviction >= 0.83
    assert d.size_factor >= 0.50
    assert d.obs_anchor  # non-empty
    assert d.obs_anchor_valid


def test_strong_buy_no_high_picks_rm_anchor_when_far_from_window():
    pkt = _packet_strong_buy_no_high()  # rm=73, window [84.5, 86.5) — 11°F away
    d = decide_entry_code(pkt)
    # rm is clearly outside window → should anchor on rm
    assert d.obs_anchor.startswith("running_min_or_max=") \
        or d.obs_anchor.startswith("rm=") \
        or d.obs_anchor.startswith("wethr_temp_f=")


# ─────────────────────────────────────────────────────────────────────────────
# Step 7.5 — obs-anchored veto
# ─────────────────────────────────────────────────────────────────────────────
def test_obs_anchored_veto_blocks_buy_no_when_wt_inside_window():
    """Reproduces HOU-B88.5 / OKC-B87.5 pattern. Strong overshoot μ + cheap
    NO ask so the edge is real and the veto fires (not the edge threshold)."""
    pkt = {
        "mu_method": "nn_match_high_n30",
        "mu_chosen": 91.5,    # μ well above cap → strong BUY_NO via overshoot
        "sigma_chosen": 1.0,
        "series": "KXHIGH", "station": "KHOU",
        "bracket_kind": "B", "floor": 88.0, "cap": 89.0,
        "yes_bid_c": 20, "yes_ask_c": 25,
        "no_bid_c": 45, "no_ask_c": 50,  # cheap → big edge
        "running_min_or_max": 87.0,
        "wethr_obs": {"temp_f": 87.8, "high_f": 87.0,
                      "highest_probable_f": 88.0, "lowest_probable_f": 87.0},
        "local_clock": {"local_hour": 13.3, "peak_hour_local": 14.79,
                        "min_hour_local": 6.0},
        "obs_trend_60m_regression": {"slope_f_per_h": 3.5, "r_squared": 0.56},
        "temp_history_range_60m": {"range_f": 3.6},
    }
    d = decide_entry_code(pkt)
    assert d.decision == "SKIP"
    assert "obs-anchored veto" in (d.skip_reason or "")


def test_obs_anchored_veto_bypass_a_lets_through():
    """If wethr_high already escaped past cap+0.5, veto doesn't fire."""
    pkt = {
        "mu_method": "nn_match_high_n30",
        "mu_chosen": 92.0, "sigma_chosen": 1.0,
        "series": "KXHIGH", "station": "KHOU",
        "bracket_kind": "B", "floor": 88.0, "cap": 89.0,
        "yes_bid_c": 25, "yes_ask_c": 30, "no_bid_c": 70, "no_ask_c": 75,
        "running_min_or_max": 90.0,
        "wethr_obs": {"temp_f": 87.8, "high_f": 91.0,  # > cap+0.5=89.5
                      "highest_probable_f": 91.0},
        "local_clock": {"local_hour": 13.3, "peak_hour_local": 14.79,
                        "min_hour_local": 6.0},
        "obs_trend_60m_regression": {"slope_f_per_h": 2.0, "r_squared": 0.6},
        "temp_history_range_60m": {"range_f": 3.0},
    }
    d = decide_entry_code(pkt)
    assert d.decision == "BUY_NO"


def test_obs_anchored_veto_skipped_when_past_peak():
    """If h_to_peak <= 0.5h, the veto doesn't engage."""
    pkt = _packet_strong_buy_no_high()
    pkt["wethr_obs"] = {"temp_f": 85.2, "high_f": 85.0,
                        "highest_probable_f": 85.0, "lowest_probable_f": 85.0}
    pkt["local_clock"]["local_hour"] = 15.4  # past peak (15.0)
    # wethr_temp=85.2 is in YES window [84.5, 86.5) but h_to_peak negative
    d = decide_entry_code(pkt)
    # Past-peak → veto doesn't fire → should proceed (BUY_NO via stays-below
    # if edge is there). At μ=75 + σ=1.5, P(NO) is very high → edge>>8pp.
    assert d.decision == "BUY_NO"


# ─────────────────────────────────────────────────────────────────────────────
# Edge threshold
# ─────────────────────────────────────────────────────────────────────────────
def test_below_min_edge_skips():
    pkt = _packet_strong_buy_no_high()
    pkt["no_ask_c"] = 99  # market already priced in
    pkt["yes_ask_c"] = 1
    d = decide_entry_code(pkt)
    assert d.decision == "SKIP"
    assert "edge below threshold" in (d.skip_reason or "")


def test_60pp_gap_blocked_rule2():
    pkt = _packet_strong_buy_no_high()
    pkt["no_ask_c"] = 5    # market says BUY_NO is essentially free
    d = decide_entry_code(pkt)
    # P(NO) very high (~99%), market 5c → edge ~94pp → > 60pp ceiling
    assert d.decision == "SKIP"
    assert "Rule#2" in (d.skip_reason or "")


# ─────────────────────────────────────────────────────────────────────────────
# wethr-lock path (2026-05-18) — reproduces LAX-B60.5 miss
# ─────────────────────────────────────────────────────────────────────────────
def test_wethr_lock_low_undershoot():
    """KXLOWTLAX-26MAY18-B60.5 reproduction: wethr_low=59 already below
    floor-1=59 → lock fires → BUY_NO regardless of bracket-math edge.
    The earlier version SKIPped due to edge=+0.080 falling to float
    precision; this version locks BUY_NO."""
    pkt = {
        "mu_method": "nn_match_low_n50",
        "mu_chosen": 59.8,
        "sigma_chosen": 2.7,
        "series": "KXLOWT",
        "station": "KLAX",
        "bracket_kind": "B",
        "floor": 60.0, "cap": 61.0,
        "yes_bid_c": 15, "yes_ask_c": 20,
        "no_bid_c": 80, "no_ask_c": 85,
        "running_min_or_max": 59.0,
        "wethr_obs": {"temp_f": 58.5, "low_f": 59.0, "high_f": 65.0,
                      "highest_probable_f": 65.0, "lowest_probable_f": 58.0},
        "local_clock": {"local_hour": 4.0, "peak_hour_local": 15.0,
                        "min_hour_local": 5.0, "past_min_today": False},
        "obs_trend_60m_regression": {"slope_f_per_h": 0.0, "r_squared": 0.1},
    }
    d = decide_entry_code(pkt)
    assert d.decision == "BUY_NO"
    assert d.conviction >= 0.90  # lock = high conviction
    assert d.obs_anchor.startswith("wethr_low_f=")
    assert "undershoot_lock" in d.read or "wethr-lock" in d.read


def test_wethr_lock_high_overshoot():
    """HIGH BUY_NO via overshoot lock: wethr_high >= cap+1."""
    pkt = {
        "mu_method": "nn_match_high_n30",
        "mu_chosen": 92.0, "sigma_chosen": 1.5,
        "series": "KXHIGH", "station": "KHOU",
        "bracket_kind": "B", "floor": 88.0, "cap": 89.0,
        "yes_bid_c": 5, "yes_ask_c": 10,
        "no_bid_c": 88, "no_ask_c": 95,
        "running_min_or_max": 90.0,
        "wethr_obs": {"temp_f": 90.5, "high_f": 91.0,  # >= cap+1 = 90
                      "highest_probable_f": 91.0, "lowest_probable_f": 90.0},
        "local_clock": {"local_hour": 16.0, "peak_hour_local": 15.0,
                        "min_hour_local": 6.0, "past_peak_today": True},
        "obs_trend_60m_regression": {"slope_f_per_h": 0.0, "r_squared": 0.1},
    }
    d = decide_entry_code(pkt)
    assert d.decision == "BUY_NO"
    assert d.conviction >= 0.90
    assert "overshoot_lock" in d.read


def test_wethr_lock_high_stays_below_requires_past_peak():
    """HIGH stays-below lock needs past_peak_today=True (margin 1°F alone
    isn't enough — pre-peak rm can still climb)."""
    pkt = {
        "mu_method": "nn_match_high_n30",
        "mu_chosen": 75.0, "sigma_chosen": 1.0,
        "series": "KXHIGH", "station": "KMDW",
        "bracket_kind": "B", "floor": 85.0, "cap": 86.0,
        "yes_bid_c": 5, "yes_ask_c": 10, "no_bid_c": 88, "no_ask_c": 95,
        "running_min_or_max": 84.0,
        "wethr_obs": {"temp_f": 83.0, "high_f": 84.0,  # = floor-1
                      "highest_probable_f": 84.0, "lowest_probable_f": 75.0},
        # past_peak_today FALSE — lock should NOT fire
        "local_clock": {"local_hour": 12.0, "peak_hour_local": 15.0,
                        "min_hour_local": 5.0, "past_peak_today": False},
    }
    d = decide_entry_code(pkt)
    # Lock didn't fire, so fall through to edge path
    # Likely BUY_NO via edge (μ=75 way below floor, P_no high)
    assert "stays_below_lock" not in d.read


def test_wethr_lock_high_stays_below_fires_with_past_peak():
    """HIGH stays-below lock fires when both wethr_high <= floor-1 AND past peak."""
    pkt = {
        "mu_method": "nn_match_high_n30",
        "mu_chosen": 75.0, "sigma_chosen": 1.0,
        "series": "KXHIGH", "station": "KMDW",
        "bracket_kind": "B", "floor": 85.0, "cap": 86.0,
        "yes_bid_c": 3, "yes_ask_c": 5, "no_bid_c": 92, "no_ask_c": 97,
        "running_min_or_max": 84.0,
        "wethr_obs": {"temp_f": 80.0, "high_f": 84.0,  # = floor-1
                      "highest_probable_f": 84.0, "lowest_probable_f": 75.0},
        "local_clock": {"local_hour": 17.0, "peak_hour_local": 15.0,
                        "min_hour_local": 5.0, "past_peak_today": True},
    }
    d = decide_entry_code(pkt)
    assert d.decision == "BUY_NO"
    assert d.conviction >= 0.90
    assert "stays_below_lock" in d.read


# ─────────────────────────────────────────────────────────────────────────────
# Edge precision (the 0.080 == 0.08 boundary case)
# ─────────────────────────────────────────────────────────────────────────────
def test_edge_at_exact_threshold_passes():
    """Exact 0.08 edge (float-precision close) should NOT be rejected.
    Reproduces LAX-B60.5 where edge_yes=0.07999... due to float math
    rounded the comparison to fail. Now epsilon-tolerant."""
    pkt = {
        "mu_method": "nn_match_high_n30",
        "mu_chosen": 75.0, "sigma_chosen": 1.0,   # P(NO) ≈ 1.0
        "series": "KXHIGH", "station": "KMDW",
        "bracket_kind": "B", "floor": 85.0, "cap": 86.0,
        "yes_bid_c": 90, "yes_ask_c": 92,   # P(YES) ~0
        "no_bid_c": 8, "no_ask_c": 92,      # no_ask=92 → P_no-implied = ~1-0.92 = 0.08 exactly
        "running_min_or_max": 73.0,
        "wethr_obs": {"temp_f": 72.0, "high_f": 73.0,
                      "highest_probable_f": 73.0, "lowest_probable_f": 70.0},
        "local_clock": {"local_hour": 13.0, "peak_hour_local": 15.0,
                        "min_hour_local": 5.0, "past_peak_today": False},
    }
    d = decide_entry_code(pkt)
    # Edge should NOT be rejected for being epsilon-below 0.08.
    # Either lock fires (wethr_high=73 NOT <= floor-1=84, so no lock for now —
    # actually waiiit, 73 <= 84 ✓ AND past_peak_today is False → lock doesn't fire)
    # → falls through to edge check, P(NO) ≈ 1, edge_no ≈ 1 - 0.92 = 0.08 → passes
    assert d.decision == "BUY_NO"
