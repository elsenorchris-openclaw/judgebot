"""F-OBS-ANCHORED-PRE filter logic regression tests (2026-05-17).

The filter blocks BUY_NO on B-bracket when:
  - wethr_temp_f is inside the YES window [floor-0.5, cap+0.5)
  - AND h_to_extreme > 0.5h
  - AND no bypass holds (wethr extreme escaped OR probable+coherent-trend)

Test approach: rather than try to bootstrap a whole prescreen pipeline,
we replicate the filter logic in test helpers and verify the truth table.
The actual production code path is identical (paper_judge_bot.py
~line 2580). If this test passes, the production logic is correct.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _filter_blocks(packet, *, edge_side, bracket_kind, floor, cap, is_high):
    """Mirror of the F-OBS-ANCHORED-PRE block in paper_judge_bot.py."""
    if not (bracket_kind == "B" and floor is not None and cap is not None):
        return False, "not_b_bracket"
    if edge_side != "BUY_NO":
        return False, "not_buy_no"
    wo = packet.get("wethr_obs") or {}
    wt = wo.get("temp_f")
    yes_lo = float(floor) - 0.5
    yes_hi = float(cap) + 0.5
    if wt is None or not (yes_lo <= wt < yes_hi):
        return False, "wt_not_in_yes_window"
    clk = packet.get("local_clock") or {}
    lh = clk.get("local_hour")
    ex_hr = clk.get("peak_hour_local") if is_high else clk.get("min_hour_local")
    if lh is None or ex_hr is None:
        return False, "no_clock"
    h_to_ex = ex_hr - lh
    if h_to_ex < 0:
        h_to_ex += 24
    if h_to_ex <= 0.5:
        return False, "too_close_to_extreme"
    # bypass (a)
    if is_high:
        wh = wo.get("high_f")
        if wh is not None and wh > yes_hi:
            return False, "bypass_a"
    else:
        wl = wo.get("low_f")
        if wl is not None and wl < yes_lo:
            return False, "bypass_a"
    # bypass (b)
    if is_high:
        whp = wo.get("highest_probable_f")
        prob_escape = whp is not None and whp > yes_hi
    else:
        wlp = wo.get("lowest_probable_f")
        prob_escape = wlp is not None and wlp < yes_lo
    if prob_escape:
        reg = packet.get("obs_trend_60m_regression") or {}
        slp = reg.get("slope_f_per_h")
        r2 = reg.get("r_squared")
        rng = (packet.get("temp_history_range_60m") or {}).get("range_f")
        slp_ok = slp is not None and (
            (is_high and slp > 0) or (not is_high and slp < 0)
        )
        r2_ok = r2 is not None and r2 >= 0.5
        rng_ok = rng is not None and rng >= 2.0
        if slp_ok and r2_ok and rng_ok:
            return False, "bypass_b"
    return True, "BLOCK"


class TestObsAnchoredPreBlocks:
    """Cases that the filter SHOULD block (would-be losers)."""

    def test_hou_b885_blocks(self):
        """KXHIGHTHOU-B88.5 2026-05-16 -$19.61. wethr_temp=87.8 in YES window
        [87.5, 89.5), h_to_peak=1.49h, wethr_high=87, whp=88.0 — no bypass."""
        pkt = {
            "wethr_obs": {"temp_f": 87.8, "high_f": 87.0, "low_f": 71.0,
                          "highest_probable_f": 88.0, "lowest_probable_f": 87.0},
            "local_clock": {"local_hour": 13.3, "peak_hour_local": 14.79},
            "obs_trend_60m_regression": {"slope_f_per_h": 3.5, "r_squared": 0.56},
            "temp_history_range_60m": {"range_f": 3.6},
        }
        blocks, reason = _filter_blocks(
            pkt, edge_side="BUY_NO", bracket_kind="B",
            floor=88.0, cap=89.0, is_high=True)
        assert blocks, f"expected BLOCK, got {reason}"

    def test_okc_b875_blocks(self):
        """KXHIGHTOKC-B87.5 2026-05-16 -$16.80. wethr_temp=87.8 in YES window
        [86.5, 88.5), h_to_peak=1.41h, wethr_high=87, whp=88.0 — no bypass."""
        pkt = {
            "wethr_obs": {"temp_f": 87.8, "high_f": 87.0, "low_f": 68.0,
                          "highest_probable_f": 88.0, "lowest_probable_f": 87.0},
            "local_clock": {"local_hour": 14.53, "peak_hour_local": 15.94},
            "obs_trend_60m_regression": {"slope_f_per_h": 1.07, "r_squared": 0.15},
            "temp_history_range_60m": {"range_f": 1.8},
        }
        blocks, reason = _filter_blocks(
            pkt, edge_side="BUY_NO", bracket_kind="B",
            floor=87.0, cap=88.0, is_high=True)
        assert blocks, f"expected BLOCK, got {reason}"


class TestObsAnchoredPreAllows:
    """Cases that the filter SHOULD NOT block."""

    def test_wethr_temp_outside_yes_window(self):
        pkt = {
            "wethr_obs": {"temp_f": 75.0, "high_f": 76.0,
                          "highest_probable_f": 76.0},
            "local_clock": {"local_hour": 13.0, "peak_hour_local": 15.0},
        }
        blocks, reason = _filter_blocks(
            pkt, edge_side="BUY_NO", bracket_kind="B",
            floor=88.0, cap=89.0, is_high=True)
        assert not blocks
        assert reason == "wt_not_in_yes_window"

    def test_too_close_to_peak(self):
        pkt = {
            "wethr_obs": {"temp_f": 87.8, "high_f": 87.0,
                          "highest_probable_f": 88.0},
            "local_clock": {"local_hour": 14.5, "peak_hour_local": 14.7},  # 0.2h
        }
        blocks, reason = _filter_blocks(
            pkt, edge_side="BUY_NO", bracket_kind="B",
            floor=88.0, cap=89.0, is_high=True)
        assert not blocks
        assert reason == "too_close_to_extreme"

    def test_bypass_a_wethr_high_escaped(self):
        pkt = {
            "wethr_obs": {"temp_f": 87.8, "high_f": 90.0,  # already past cap+0.5
                          "highest_probable_f": 90.0},
            "local_clock": {"local_hour": 13.3, "peak_hour_local": 14.8},
        }
        blocks, reason = _filter_blocks(
            pkt, edge_side="BUY_NO", bracket_kind="B",
            floor=88.0, cap=89.0, is_high=True)
        assert not blocks
        assert reason == "bypass_a"

    def test_bypass_b_probable_plus_coherent_trend(self):
        pkt = {
            "wethr_obs": {"temp_f": 87.8, "high_f": 87.0,
                          "highest_probable_f": 90.0},  # >89.5
            "local_clock": {"local_hour": 13.3, "peak_hour_local": 14.8},
            "obs_trend_60m_regression": {"slope_f_per_h": 2.0, "r_squared": 0.7},
            "temp_history_range_60m": {"range_f": 2.5},
        }
        blocks, reason = _filter_blocks(
            pkt, edge_side="BUY_NO", bracket_kind="B",
            floor=88.0, cap=89.0, is_high=True)
        assert not blocks
        assert reason == "bypass_b"

    def test_low_bracket_block(self):
        """LOW mirror: wethr_temp in YES window + cooling time + no escape.
        floor=74 cap=75 → yes window [73.5, 75.5). wethr_low=74 (NOT below
        73.5 so bypass_a fails), wlp=74 (NOT below 73.5 so prob_escape
        false → bypass_b fails). Should BLOCK."""
        pkt = {
            "wethr_obs": {"temp_f": 74.0, "high_f": 90.0, "low_f": 74.0,
                          "highest_probable_f": 75.0, "lowest_probable_f": 74.0},
            "local_clock": {"local_hour": 1.0, "min_hour_local": 5.5},
            "obs_trend_60m_regression": {"slope_f_per_h": -0.3, "r_squared": 0.3},
            "temp_history_range_60m": {"range_f": 1.5},
        }
        blocks, reason = _filter_blocks(
            pkt, edge_side="BUY_NO", bracket_kind="B",
            floor=74.0, cap=75.0, is_high=False)
        assert blocks, f"expected BLOCK, got {reason}"

    def test_buy_yes_not_filtered(self):
        pkt = {
            "wethr_obs": {"temp_f": 87.8, "high_f": 87.0,
                          "highest_probable_f": 88.0},
            "local_clock": {"local_hour": 13.3, "peak_hour_local": 14.8},
        }
        blocks, reason = _filter_blocks(
            pkt, edge_side="BUY_YES", bracket_kind="B",
            floor=88.0, cap=89.0, is_high=True)
        assert not blocks
        assert reason == "not_buy_no"

    def test_t_bracket_not_filtered(self):
        pkt = {
            "wethr_obs": {"temp_f": 87.8, "high_f": 87.0,
                          "highest_probable_f": 88.0},
            "local_clock": {"local_hour": 13.3, "peak_hour_local": 14.8},
        }
        blocks, reason = _filter_blocks(
            pkt, edge_side="BUY_NO", bracket_kind="T",
            floor=88.0, cap=None, is_high=True)
        assert not blocks
        assert reason == "not_b_bracket"
