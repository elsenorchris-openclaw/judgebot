"""Tests for the per-station obs-window prescreen + circular-window helper.

These cover:
  - _in_circular_window pure helper (wrap-around, midnight=24, full-day, etc.)
  - prescreen() time-of-day rejection: per-station peak/min, asymmetric HIGH,
    LOW pre-dawn + late-evening, d+1 LOW preview, d+1 HIGH skip, d+2 skip,
    and per-station peak shifts (KSEA peak ≈ 13, KPHX peak ≈ 16, default 15).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
from paper_judge_bot import _in_circular_window, prescreen  # noqa: E402


# 2026-05-17: skip_forecast_only_mu was added to prescreen on the same day.
# It short-circuits before time-of-day gates when _numerical_edge returns a
# forecast-only mu_method. These tests exercise the time-of-day gates with
# fixture packets that don't set up nn_match (which would require seeding the
# heating_traces DB). Disable the gate for the module so the tests exercise
# the gates they're written to cover.
@pytest.fixture(autouse=True)
def _disable_forecast_only_gate(monkeypatch):
    monkeypatch.setitem(config.PRESCREEN, "skip_forecast_only_mu", False)
    # 2026-05-18: skip_unless_nn_match is stricter — also disable for tests.
    monkeypatch.setitem(config.PRESCREEN, "skip_unless_nn_match", False)


# ─────────────────────────────────────────────────────────────────────────────
# _in_circular_window — pure helper
# ─────────────────────────────────────────────────────────────────────────────
class TestCircularWindow:
    def test_simple_interior(self):
        assert _in_circular_window(14.0, 12.0, 16.0) is True

    def test_simple_at_low_inclusive(self):
        assert _in_circular_window(12.0, 12.0, 16.0) is True

    def test_simple_at_high_exclusive(self):
        assert _in_circular_window(16.0, 12.0, 16.0) is False

    def test_simple_before(self):
        assert _in_circular_window(11.9, 12.0, 16.0) is False

    def test_simple_after(self):
        assert _in_circular_window(16.1, 12.0, 16.0) is False

    def test_late_evening_hi_24_explicit(self):
        # [22, 24) — must NOT wrap. hour=23.99 inside, hour=0 outside, hour=22 inside.
        assert _in_circular_window(22.0, 22.0, 24.0) is True
        assert _in_circular_window(23.99, 22.0, 24.0) is True
        assert _in_circular_window(0.0, 22.0, 24.0) is False
        assert _in_circular_window(21.9, 22.0, 24.0) is False

    def test_wrap_around_midnight(self):
        # peak=2 (highly unusual but possible at extreme lat in deep winter)
        # window [peak-3, peak+1) = [-1, 3) = [23, 3) — wraps midnight
        assert _in_circular_window(23.5, -1.0, 3.0) is True
        assert _in_circular_window(0.5, -1.0, 3.0) is True
        assert _in_circular_window(2.9, -1.0, 3.0) is True
        assert _in_circular_window(3.0, -1.0, 3.0) is False
        assert _in_circular_window(12.0, -1.0, 3.0) is False
        assert _in_circular_window(22.5, -1.0, 3.0) is False

    def test_min_hour_4_predawn_wraps(self):
        # min=4 → pre-dawn [min-5, min-1) = [-1, 3) = [23, 3) wraps
        assert _in_circular_window(23.5, -1.0, 3.0) is True
        assert _in_circular_window(0.0, -1.0, 3.0) is True
        assert _in_circular_window(2.9, -1.0, 3.0) is True
        assert _in_circular_window(3.0, -1.0, 3.0) is False  # exclusive upper

    def test_none_hour_rejected(self):
        assert _in_circular_window(None, 12.0, 16.0) is False

    def test_full_day_when_equal(self):
        # lo == hi (after normalization) → full 24h, all hours pass
        assert _in_circular_window(0.0, 0.0, 24.0) is True   # 24 % 24 == 0 normally, but hi=24 special-case keeps it 24
        # Use a clean lo==hi case where both wrap to same point:
        assert _in_circular_window(13.0, 5.0, 5.0) is True
        assert _in_circular_window(0.0, 5.0, 5.0) is True


# ─────────────────────────────────────────────────────────────────────────────
# Test packet builder
# ─────────────────────────────────────────────────────────────────────────────
def _base_packet(series="KXHIGH", days_out=0, local_hour=14.0,
                 peak_hour_local=15.0, min_hour_local=6.0,
                 station="KNYC", floor=70, cap=71):
    """Construct a packet that already passes every NON-time-of-day gate.
    Each test then mutates the field under test."""
    return {
        "ticker": f"{series}TEST-26MAY15-B70.5",
        "series": series,
        "station": station,
        "climate_day": "2026-05-15",
        "days_out": days_out,
        "spread_c": 4,
        # 2026-05-16: market must be confident (dom side >= 60c per
        # min_market_confidence_cents) AND edge must clear 6pp floor +
        # stay under 25pp Rule#2 ceiling. With default μ=70.5 in B[70,71]:
        # bot's P(YES_in_bracket)≈0.31; yes_ask=20 → BUY_YES edge ≈ 11pp
        # (clears 6pp floor); gap=11pp (under 25pp ceiling); no_ask=80
        # (dom side 80c, clears 60c confidence floor).
        "yes_ask_c": 20,
        "no_ask_c": 80,
        "volume": 100,
        "seconds_to_close": 6 * 3600,           # 6 hours
        "floor": floor,
        "cap": cap,
        "mu_nbm": 70.5,                          # forecasts present
        "mu_hrrr": 70.5,
        "live_obs": {"temp_f": 68.0, "age_sec": 60},
        # 2026-05-15: wethr-only policy. Prescreen gates on wethr_obs freshness.
        "wethr_obs": {"temp_f": 68.0, "age_sec": 60},
        "local_clock": {
            "local_hour": local_hour,
            "peak_hour_local": peak_hour_local,
            "min_hour_local": min_hour_local,
        },
        # 2026-05-16: d+0 entries require rm anchor. Tests inherit a fresh
        # rm + past-grace rm_validation so the d+0 rm-required gate doesn't
        # mask the time-of-day gates these tests are designed to exercise.
        "running_min_or_max": 60.0,
        "rm_validation": {
            "ok": True, "reason": "ok",
            "secs_into_climate_day": 7200,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# HIGH d+0 — peak − 3 to peak + 1
# ─────────────────────────────────────────────────────────────────────────────
class TestHighD0Window:
    def test_default_peak15_in_window_at_peak(self):
        p = _base_packet(series="KXHIGH", local_hour=15.0, peak_hour_local=15.0)
        assert prescreen(p) is None

    def test_default_peak15_in_window_pre_peak_minus_3(self):
        p = _base_packet(series="KXHIGH", local_hour=12.0, peak_hour_local=15.0)
        assert prescreen(p) is None

    def test_default_peak15_in_window_post_peak_plus_1_minus_eps(self):
        p = _base_packet(series="KXHIGH", local_hour=15.99, peak_hour_local=15.0)
        assert prescreen(p) is None

    def test_default_peak15_rejected_before(self):
        p = _base_packet(series="KXHIGH", local_hour=11.9, peak_hour_local=15.0)
        rej = prescreen(p)
        assert rej is not None and "HIGH d+0 outside peak window" in rej

    def test_default_peak15_rejected_after(self):
        p = _base_packet(series="KXHIGH", local_hour=16.0, peak_hour_local=15.0)
        rej = prescreen(p)
        assert rej is not None and "HIGH d+0 outside peak window" in rej

    def test_kphx_peak16_shifts_window(self):
        # KPHX peak ≈ 16 → window [13, 17)
        p = _base_packet(series="KXHIGH", station="KPHX",
                         local_hour=13.0, peak_hour_local=16.0)
        assert prescreen(p) is None
        p["local_clock"]["local_hour"] = 16.99
        assert prescreen(p) is None
        p["local_clock"]["local_hour"] = 12.9
        assert prescreen(p) is not None

    def test_ksea_peak13_shifts_window(self):
        # KSEA peak ≈ 13 → window [10, 14)
        p = _base_packet(series="KXHIGH", station="KSEA",
                         local_hour=10.0, peak_hour_local=13.0)
        assert prescreen(p) is None
        p["local_clock"]["local_hour"] = 13.99
        assert prescreen(p) is None
        p["local_clock"]["local_hour"] = 9.9
        assert prescreen(p) is not None

    def test_ksfo_peak13_morning_rejected(self):
        # KSFO peak ≈ 13 — 5am should be far outside even though it would
        # have passed the old bot-wide 5-18 gate.
        p = _base_packet(series="KXHIGH", station="KSFO",
                         local_hour=5.0, peak_hour_local=13.0)
        assert prescreen(p) is not None


# ─────────────────────────────────────────────────────────────────────────────
# LOW d+0 — pre-dawn OR late-evening
# ─────────────────────────────────────────────────────────────────────────────
class TestLowD0Window:
    def test_predawn_window_open_at_min_minus_5(self):
        # min=6 → pre-dawn window [1, 5)
        p = _base_packet(series="KXLOWT", local_hour=1.0, min_hour_local=6.0)
        assert prescreen(p) is None

    def test_predawn_window_just_before_min_minus_1_passes(self):
        p = _base_packet(series="KXLOWT", local_hour=4.99, min_hour_local=6.0)
        assert prescreen(p) is None

    def test_predawn_excludes_last_hour_before_min(self):
        # min=6, window ends at min-1=5 exclusive → 5.0 itself is outside the
        # pre-dawn window, but should still pass IF and only if late-evening
        # also fails. local_hour=5.0 is neither pre-dawn nor late-evening.
        p = _base_packet(series="KXLOWT", local_hour=5.0, min_hour_local=6.0)
        rej = prescreen(p)
        assert rej is not None and "LOW d+0" in rej

    def test_late_evening_d0_passes_cold_front_case(self):
        # Late-evening evaluation of d+0 LOW (rare cold-front case)
        p = _base_packet(series="KXLOWT", local_hour=22.5, min_hour_local=6.0)
        assert prescreen(p) is None

    def test_late_evening_d0_just_before_midnight(self):
        p = _base_packet(series="KXLOWT", local_hour=23.99, min_hour_local=6.0)
        assert prescreen(p) is None

    def test_midday_d0_rejected(self):
        p = _base_packet(series="KXLOWT", local_hour=14.0, min_hour_local=6.0)
        rej = prescreen(p)
        assert rej is not None and "LOW d+0" in rej

    def test_early_evening_d0_rejected(self):
        # 19:00 local — outside both pre-dawn and the new 22-24 evening
        p = _base_packet(series="KXLOWT", local_hour=19.0, min_hour_local=6.0)
        rej = prescreen(p)
        assert rej is not None

    def test_late_morning_d0_rejected(self):
        # 10:00 local — past min, outside any d+0 window
        p = _base_packet(series="KXLOWT", local_hour=10.0, min_hour_local=6.0)
        rej = prescreen(p)
        assert rej is not None

    def test_summer_min_5_shifts_window(self):
        # Solstice sunrise ~04:30-05:00 local → min_hour ~5
        # Pre-dawn window = [min-5, min-1) = [0, 4)
        p = _base_packet(series="KXLOWT", local_hour=0.0, min_hour_local=5.0)
        assert prescreen(p) is None  # at the open of the window
        p["local_clock"]["local_hour"] = 3.99
        assert prescreen(p) is None  # just before close
        # 4.0 is the exclusive upper bound, so it's outside the pre-dawn
        # window. It's also outside [22, 24) late-evening. Should reject.
        p["local_clock"]["local_hour"] = 4.0
        rej = prescreen(p)
        assert rej is not None and "LOW d+0" in rej

    def test_kxlow_series_alt_prefix_matches(self):
        # Some packets use "KXLOW" instead of "KXLOWT". Both should match.
        p = _base_packet(series="KXLOW", local_hour=3.0, min_hour_local=6.0)
        assert prescreen(p) is None


# ─────────────────────────────────────────────────────────────────────────────
# d+1 LOW — late-evening preview only
# ─────────────────────────────────────────────────────────────────────────────
class TestLowD1Window:
    def test_d1_late_evening_passes(self):
        p = _base_packet(series="KXLOWT", days_out=1, local_hour=22.5,
                         min_hour_local=6.0)
        assert prescreen(p) is None

    def test_d1_predawn_rejected(self):
        # d+1 pre-dawn means looking at tomorrow's LOW from THIS dawn — useless.
        p = _base_packet(series="KXLOWT", days_out=1, local_hour=3.0,
                         min_hour_local=6.0)
        rej = prescreen(p)
        assert rej is not None and "LOW d+1" in rej

    def test_d1_afternoon_rejected(self):
        p = _base_packet(series="KXLOWT", days_out=1, local_hour=15.0,
                         min_hour_local=6.0)
        rej = prescreen(p)
        assert rej is not None and "LOW d+1" in rej

    def test_d1_early_evening_rejected(self):
        # 21:30 — Chris's pick is 22-24 only, so 21:59 is out.
        p = _base_packet(series="KXLOWT", days_out=1, local_hour=21.99,
                         min_hour_local=6.0)
        rej = prescreen(p)
        assert rej is not None

    def test_d1_midnight_exclusive(self):
        # 00:00 local is OUTSIDE the [22, 24) window — it's the start of d+0
        p = _base_packet(series="KXLOWT", days_out=1, local_hour=0.0,
                         min_hour_local=6.0)
        rej = prescreen(p)
        assert rej is not None


# ─────────────────────────────────────────────────────────────────────────────
# d+1 HIGH — hard skip
# ─────────────────────────────────────────────────────────────────────────────
class TestHighD1Skip:
    def test_d1_high_always_rejected_even_at_peak(self):
        p = _base_packet(series="KXHIGH", days_out=1, local_hour=15.0,
                         peak_hour_local=15.0)
        rej = prescreen(p)
        assert rej is not None and "HIGH d+1" in rej

    def test_d1_high_rejected_at_evening(self):
        p = _base_packet(series="KXHIGH", days_out=1, local_hour=22.0,
                         peak_hour_local=15.0)
        rej = prescreen(p)
        assert rej is not None and "HIGH d+1" in rej


# ─────────────────────────────────────────────────────────────────────────────
# d+2+ — hard skip
# ─────────────────────────────────────────────────────────────────────────────
class TestFarOut:
    def test_d2_low_rejected(self):
        p = _base_packet(series="KXLOWT", days_out=2, local_hour=22.5,
                         min_hour_local=6.0)
        rej = prescreen(p)
        assert rej is not None and "d+2" in rej

    def test_d3_high_rejected(self):
        p = _base_packet(series="KXHIGH", days_out=3, local_hour=15.0,
                         peak_hour_local=15.0)
        rej = prescreen(p)
        assert rej is not None and ("d+3" in rej or "past max_days_out" in rej)


# ─────────────────────────────────────────────────────────────────────────────
# Defensive: missing fields
# ─────────────────────────────────────────────────────────────────────────────
class TestDefensive:
    def test_missing_days_out_rejected(self):
        p = _base_packet(series="KXHIGH", local_hour=15.0)
        p.pop("days_out")
        rej = prescreen(p)
        assert rej is not None and "days_out" in rej

    def test_missing_local_clock_rejected(self):
        p = _base_packet(series="KXHIGH", local_hour=15.0)
        p.pop("local_clock")
        rej = prescreen(p)
        assert rej is not None and "local clock" in rej

    def test_missing_local_hour_rejected(self):
        p = _base_packet(series="KXHIGH", local_hour=15.0)
        p["local_clock"].pop("local_hour")
        rej = prescreen(p)
        assert rej is not None and "local clock" in rej

    def test_missing_peak_hour_rejected(self):
        p = _base_packet(series="KXHIGH", local_hour=15.0)
        p["local_clock"].pop("peak_hour_local")
        rej = prescreen(p)
        assert rej is not None and "peak_hour_local" in rej

    def test_missing_min_hour_rejected(self):
        p = _base_packet(series="KXLOWT", local_hour=3.0)
        p["local_clock"].pop("min_hour_local")
        rej = prescreen(p)
        assert rej is not None and "min_hour_local" in rej

    def test_unknown_series_rejected(self):
        p = _base_packet(series="KXSOMETHING", local_hour=15.0)
        rej = prescreen(p)
        assert rej is not None and "unknown series" in rej
