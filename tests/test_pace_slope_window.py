"""Regression tests for F1+F2+F3 pace_slope changes (2026-05-17)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shared_cache_reader as scache


# ─────────────────────────────────────────────────────────────────────────────
# F3 tuple return — unavailable_reason codes
# ─────────────────────────────────────────────────────────────────────────────
class TestPaceSlopeUnavailableReason:
    def test_empty_obs_returns_no_obs(self):
        result, reason = scache.compute_obs_vs_forecast_pace_slope([], [])
        assert result is None
        assert reason == "no_obs"

    def test_no_fc_sources_returns_no_fc_sources(self):
        obs = [{"hour_offset_h": 0, "temp_f": 70.0,
                "hour_utc_iso": "2026-05-17T00:00Z"}]
        result, reason = scache.compute_obs_vs_forecast_pace_slope(
            obs, [], fc_lookup_fn=None)
        assert result is None
        assert reason == "no_fc_sources"

    def test_all_pairs_unmatched_returns_that_reason(self):
        # obs hours exist but no forecast matches any of them
        obs = [{"hour_offset_h": h, "temp_f": 70.0 + h,
                "hour_utc_iso": f"2026-05-17T{h:02d}:00Z"} for h in (0, 1, 2)]
        # forecast is for unrelated hours
        fc = [{"utc_iso": "2026-05-19T10:00Z", "temp_f": 80.0}]
        result, reason = scache.compute_obs_vs_forecast_pace_slope(obs, fc)
        assert result is None
        assert reason == "all_pairs_unmatched"

    def test_insufficient_pairs_returns_that_reason(self):
        # only 1 matched obs/forecast pair (F2 lowered to 2 minimum)
        obs = [{"hour_offset_h": 0, "temp_f": 70.0,
                "hour_utc_iso": "2026-05-17T00:00Z"}]
        fc = [{"utc_iso": "2026-05-17T00:00Z", "temp_f": 72.0}]
        result, reason = scache.compute_obs_vs_forecast_pace_slope(obs, fc)
        assert result is None
        assert reason == "insufficient_pairs"


# ─────────────────────────────────────────────────────────────────────────────
# F2 — 2-pair regression now works (was: required ≥3)
# ─────────────────────────────────────────────────────────────────────────────
class TestPaceSlopeTwoPair:
    def test_two_matched_pairs_returns_slope(self):
        obs = [
            {"hour_offset_h": 0, "temp_f": 70.0,
             "hour_utc_iso": "2026-05-17T00:00Z"},
            {"hour_offset_h": 1, "temp_f": 72.0,
             "hour_utc_iso": "2026-05-17T01:00Z"},
        ]
        fc = [
            {"utc_iso": "2026-05-17T00:00Z", "temp_f": 70.0},
            {"utc_iso": "2026-05-17T01:00Z", "temp_f": 71.0},
        ]
        # obs gap: 0, +1 over 1h → slope = +1.0°F/h
        result, reason = scache.compute_obs_vs_forecast_pace_slope(
            obs, fc, lookback_hours=5)
        assert reason is None
        assert result is not None
        assert result["n_hours"] == 2
        assert result["slope_per_h"] == pytest.approx(1.0, abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# F1 — get_hourly_obs_window helper
# ─────────────────────────────────────────────────────────────────────────────
class TestGetHourlyObsWindow:
    def test_window_filters_correctly(self, monkeypatch):
        # Build a fake wethr station entry with 6 hourly entries spanning 6h
        fake_hist = []
        base_ts = 1000000.0  # arbitrary
        for h in range(6):
            fake_hist.append({
                "hour_ts": base_ts + h * 3600,
                "hour_iso": f"hour_{h}",
                "temp_f": 70.0 + h,
                "dew_point_f": 60.0,
                "wind_speed_mph": 5.0,
                "cloud_layer_count": 1,
            })
        # Mock _wethr_station_entry to return our fake
        monkeypatch.setattr(
            scache, "_wethr_station_entry",
            lambda _stn: {"hourly_history": fake_hist})
        # Request middle 3 hours (h=2, h=3, h=4)
        out = scache.get_hourly_obs_window(
            "KFAKE",
            start_ts=base_ts + 2 * 3600,
            end_ts=base_ts + 4 * 3600,
        )
        assert len(out) == 3
        assert [r["temp_f"] for r in out] == [72.0, 73.0, 74.0]
        # hour_offset_h is relative to start_ts
        assert [r["hour_offset_h"] for r in out] == [0, 1, 2]

    def test_window_empty_when_no_obs_in_range(self, monkeypatch):
        monkeypatch.setattr(
            scache, "_wethr_station_entry",
            lambda _stn: {"hourly_history": []})
        out = scache.get_hourly_obs_window(
            "KFAKE", start_ts=1000.0, end_ts=2000.0)
        assert out == []
