"""R2 regression tests for obs_anchor validation (2026-05-17)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import judgment


@pytest.fixture
def packet():
    return {
        "wethr_obs": {
            "temp_f": 80.6, "high_f": 82.0, "low_f": 65.0,
            "highest_probable_f": 81.0, "lowest_probable_f": 80.0,
        },
        "running_min_or_max": 82.0,
        "live_obs": {"temp_f": 80.5},
        "obs_trend_30m": -0.75,  # negative value used by test_negative_value
        "obs_trend_60m_regression": {"slope_f_per_h": 2.22, "r_squared": 0.52},
        "temp_history_range_60m": {"range_f": 2.8},
        "obs_vs_forecast_pace_slope": {"slope_per_h": -0.75, "current_gap_f": 1.0},
    }


def _make_resp(obs_anchor: str = "wethr_temp_f=80.6", decision: str = "BUY_NO") -> str:
    import json
    return json.dumps({
        "decision": decision,
        "conviction": 0.85,
        "size_factor": 0.65,
        "read": "stub",
        "obs_anchor": obs_anchor,
        "key_risks": ["x"],
        "what_would_change_my_mind": "x",
    })


class TestObsAnchorValidPaths:
    def test_exact_match(self, packet):
        d = judgment.parse_entry_response(_make_resp("wethr_temp_f=80.6"), packet=packet)
        assert d.obs_anchor_valid
        assert d.obs_anchor_reason == ""

    def test_tolerance_match_within_1f(self, packet):
        d = judgment.parse_entry_response(_make_resp("wethr_temp_f=80.0"), packet=packet)
        assert d.obs_anchor_valid

    def test_rm_alias(self, packet):
        d = judgment.parse_entry_response(_make_resp("rm=82.0"), packet=packet)
        assert d.obs_anchor_valid

    def test_60m_slope(self, packet):
        d = judgment.parse_entry_response(_make_resp("obs_trend_60m_slope=2.22"), packet=packet)
        assert d.obs_anchor_valid

    def test_negative_value(self, packet):
        # 2026-05-17: was pace_slope=-0.75; pace_slope removed from validator
        # (forecast-derived field banned by prompt). Use obs_trend_30m which
        # is now the negative fixture value and a valid obs_anchor field.
        d = judgment.parse_entry_response(_make_resp("obs_trend_30m=-0.75"), packet=packet)
        assert d.obs_anchor_valid

    def test_running_min_or_max_full(self, packet):
        d = judgment.parse_entry_response(_make_resp("running_min_or_max=82.0"), packet=packet)
        assert d.obs_anchor_valid


class TestObsAnchorInvalidPaths:
    def test_empty_anchor_invalid(self, packet):
        d = judgment.parse_entry_response(_make_resp(""), packet=packet)
        assert not d.obs_anchor_valid
        assert "empty" in d.obs_anchor_reason

    def test_value_mismatch(self, packet):
        d = judgment.parse_entry_response(_make_resp("wethr_temp_f=90.0"), packet=packet)
        assert not d.obs_anchor_valid
        assert "mismatch" in d.obs_anchor_reason

    def test_forecast_field_rejected(self, packet):
        d = judgment.parse_entry_response(_make_resp("mu_nbm=83.0"), packet=packet)
        assert not d.obs_anchor_valid
        assert "unknown field" in d.obs_anchor_reason

    def test_unparsable_format(self, packet):
        d = judgment.parse_entry_response(_make_resp("wethr_temp_f is 80"), packet=packet)
        assert not d.obs_anchor_valid
        assert "unparsable" in d.obs_anchor_reason

    def test_packet_field_null(self, packet):
        packet["wethr_obs"]["temp_f"] = None
        d = judgment.parse_entry_response(_make_resp("wethr_temp_f=80.6"), packet=packet)
        assert not d.obs_anchor_valid
        assert "null in packet" in d.obs_anchor_reason


class TestParseEntryResponseBackcompat:
    def test_old_callsite_without_packet_still_works(self, packet):
        """Callers that don't pass packet shouldn't crash — anchor just stays invalid."""
        d = judgment.parse_entry_response(_make_resp())  # no packet=
        assert d.parse_ok
        assert d.decision == "BUY_NO"
        assert not d.obs_anchor_valid
        assert "no packet" in d.obs_anchor_reason
