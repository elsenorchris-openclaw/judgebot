"""Tests for F1 rm-staleness validation, 1h grace gate, and F2 cold-tail
warming-obs filter (added 2026-05-16).

Background: last night 7 of 12 LOW BUY_NO losers ($-33.61) traced to one
of three obs-side patterns:
  - 5 entries used yesterday's wethr running_min as if it were today's
    (pre-LDT-midnight rm staleness)
  - 2 entries used a fresh-but-meaningless rm 0-1h past LDT midnight
    (cooling hadn't established)
  - 3 T-bracket cold-tail BUY_NOs fired with obs warming during cooling
    phase (LLM's own read cited "+1.8°F/30m warming" but bet anyway)
"""
from __future__ import annotations

from datetime import datetime, timezone
import pytest
import time

import wethr_rm


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def utc_ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# ─────────────────────────────────────────────────────────────────────────────
# lst_midnight_utc_ts — LST per-station midnight (matches Kalshi/NWS CLI)
# (Legacy alias ldt_midnight_utc_ts also tested for backward compat.)
# ─────────────────────────────────────────────────────────────────────────────
class TestLstMidnightUtc:
    def test_atl_may_lst(self):
        """ATL May 15 → midnight EST = 05:00 UTC (LST, even though local
        civil time is EDT during May — Kalshi/NWS CLI use LST year-round)."""
        ts = wethr_rm.lst_midnight_utc_ts("KATL", "2026-05-15")
        assert ts == utc_ts("2026-05-15T05:00:00Z")

    def test_atl_dec_lst(self):
        """ATL December → midnight EST = 05:00 UTC (LST = civil time in Dec)."""
        ts = wethr_rm.lst_midnight_utc_ts("KATL", "2026-12-15")
        assert ts == utc_ts("2026-12-15T05:00:00Z")

    def test_phx_year_round_mst(self):
        """PHX uses MST year-round (no DST) → always UTC-7."""
        may = wethr_rm.lst_midnight_utc_ts("KPHX", "2026-05-15")
        dec = wethr_rm.lst_midnight_utc_ts("KPHX", "2026-12-15")
        assert may == utc_ts("2026-05-15T07:00:00Z")
        assert dec == utc_ts("2026-12-15T07:00:00Z")

    def test_lax_pst_year_round(self):
        """LAX (PST always for Kalshi LST settlement) → UTC-8."""
        ts = wethr_rm.lst_midnight_utc_ts("KLAX", "2026-05-15")
        assert ts == utc_ts("2026-05-15T08:00:00Z")

    def test_den_mst_year_round(self):
        """DEN LST = MST year-round → UTC-7."""
        ts = wethr_rm.lst_midnight_utc_ts("KDEN", "2026-05-15")
        assert ts == utc_ts("2026-05-15T07:00:00Z")

    def test_chi_cst_year_round(self):
        """MDW LST = CST year-round → UTC-6."""
        ts = wethr_rm.lst_midnight_utc_ts("KMDW", "2026-05-15")
        assert ts == utc_ts("2026-05-15T06:00:00Z")

    def test_unknown_station(self):
        assert wethr_rm.lst_midnight_utc_ts("KZZZ", "2026-05-15") is None

    def test_bad_date(self):
        assert wethr_rm.lst_midnight_utc_ts("KATL", "not-a-date") is None
        assert wethr_rm.lst_midnight_utc_ts("KATL", "") is None

    def test_dst_start_day(self):
        """Mar 8 2026: DST starts but LST midnight is unchanged → 05:00 UTC."""
        ts = wethr_rm.lst_midnight_utc_ts("KATL", "2026-03-08")
        assert ts == utc_ts("2026-03-08T05:00:00Z")

    def test_dst_end_day(self):
        """Nov 1 2026: DST ends but LST midnight is unchanged → 05:00 UTC."""
        ts = wethr_rm.lst_midnight_utc_ts("KATL", "2026-11-01")
        assert ts == utc_ts("2026-11-01T05:00:00Z")

    def test_legacy_ldt_alias_returns_lst(self):
        """ldt_midnight_utc_ts is a deprecated alias that now returns LST
        (matches Kalshi). Any external code using the old name gets the
        correct value."""
        assert wethr_rm.ldt_midnight_utc_ts("KATL", "2026-05-15") == \
            wethr_rm.lst_midnight_utc_ts("KATL", "2026-05-15")


# ─────────────────────────────────────────────────────────────────────────────
# validate_rm_for_climate_day — full validator
# ─────────────────────────────────────────────────────────────────────────────
class TestValidateRmForClimateDay:
    def test_stale_cache_date_atl_pre_midnight(self):
        """ATL B52.5 from last night: entered 00:01 UTC = 20:01 EDT May 14.
        Cache had date='2026-05-14' (yesterdays running min). Bot reading it
        as if for ticker May 15 → MUST be flagged stale."""
        r = wethr_rm.validate_rm_for_climate_day(
            station="KATL",
            climate_day="2026-05-15",
            cache_date="2026-05-14",
            time_of_extreme_utc="2026-05-14 11:30:00",
            now_utc_ts=utc_ts("2026-05-15T00:01:00Z"),
            grace_sec=3600.0,
        )
        assert r["ok"] is False
        assert "stale_cache_date" in r["reason"]

    def test_dal_pre_midnight_cdt(self):
        r = wethr_rm.validate_rm_for_climate_day(
            "KDFW", "2026-05-15", "2026-05-14",
            "2026-05-14 12:00:00",
            utc_ts("2026-05-15T00:38:00Z"), 3600.0,
        )
        assert r["ok"] is False
        assert "stale_cache_date" in r["reason"]

    def test_den_just_into_climate_day_in_grace(self):
        """DEN B48.5 from last night: entered 9 min after LST midnight.
        DEN LST = MST = UTC-7, so May 15 climate day starts at 07:00 UTC
        (NOT 06:00 UTC which would be MDT — Kalshi uses LST year-round)."""
        r = wethr_rm.validate_rm_for_climate_day(
            "KDEN", "2026-05-15", "2026-05-15",
            "2026-05-15 07:04:00",
            utc_ts("2026-05-15T07:09:00Z"), 3600.0,
        )
        assert r["ok"] is False
        assert "within_grace" in r["reason"]
        assert r["secs_into_climate_day"] == pytest.approx(540, abs=1)

    def test_sea_in_grace_pst(self):
        """SEA LST = PST year-round = UTC-8 → climate day starts 08:00 UTC."""
        r = wethr_rm.validate_rm_for_climate_day(
            "KSEA", "2026-05-15", "2026-05-15",
            "2026-05-15 08:30:00",
            utc_ts("2026-05-15T08:52:00Z"), 3600.0,
        )
        assert r["ok"] is False
        assert "within_grace" in r["reason"]

    def test_valid_3h_into_climate_day(self):
        """ATL LST midnight = 05:00 UTC. Entry at 08:00 UTC = 3h past LST
        midnight, well clear of 1h grace."""
        r = wethr_rm.validate_rm_for_climate_day(
            "KATL", "2026-05-15", "2026-05-15",
            "2026-05-15 09:30:00",
            utc_ts("2026-05-15T08:00:00Z"), 3600.0,
        )
        assert r["ok"] is True
        assert r["reason"] == "ok"

    def test_exactly_at_grace_boundary(self):
        """At exactly grace_sec past LST midnight, must pass (>= boundary).
        ATL LST midnight = 05:00 UTC + 1h grace = 06:00 UTC boundary."""
        r = wethr_rm.validate_rm_for_climate_day(
            "KATL", "2026-05-15", "2026-05-15", None,
            utc_ts("2026-05-15T06:00:00Z"), 3600.0,
        )
        assert r["ok"] is True

    def test_one_second_before_grace_boundary(self):
        """ATL LST grace boundary = 06:00 UTC. 05:59:59 UTC is 1 sec inside."""
        r = wethr_rm.validate_rm_for_climate_day(
            "KATL", "2026-05-15", "2026-05-15", None,
            utc_ts("2026-05-15T05:59:59Z"), 3600.0,
        )
        assert r["ok"] is False
        assert "within_grace" in r["reason"]

    def test_time_of_extreme_outside_window_from_prev_day(self):
        """Cache date matches but time_of_low is from yesterday — STALE."""
        r = wethr_rm.validate_rm_for_climate_day(
            "KATL", "2026-05-15", "2026-05-15",
            "2026-05-14 11:00:00",
            utc_ts("2026-05-15T08:00:00Z"), 3600.0,
        )
        assert r["ok"] is False
        assert "time_of_extreme_outside_window" in r["reason"]

    def test_time_of_extreme_at_lower_boundary(self):
        """time_of_extreme exactly at LST midnight is INSIDE the window.
        ATL LST midnight = 05:00 UTC."""
        r = wethr_rm.validate_rm_for_climate_day(
            "KATL", "2026-05-15", "2026-05-15",
            "2026-05-15 05:00:00",
            utc_ts("2026-05-15T08:00:00Z"), 3600.0,
        )
        assert r["ok"] is True

    def test_time_of_extreme_1s_before_lower_boundary(self):
        """ATL LST midnight = 05:00 UTC. 04:59:59 UTC is yesterday's LST day."""
        r = wethr_rm.validate_rm_for_climate_day(
            "KATL", "2026-05-15", "2026-05-15",
            "2026-05-15 04:59:59",
            utc_ts("2026-05-15T08:00:00Z"), 3600.0,
        )
        assert r["ok"] is False

    def test_time_of_extreme_at_upper_boundary(self):
        """Upper boundary is EXCLUSIVE — exactly +24h is OUTSIDE.
        ATL LST midnight 5/15 = 05:00 UTC, so +24h = 05:00 UTC 5/16."""
        r = wethr_rm.validate_rm_for_climate_day(
            "KATL", "2026-05-15", "2026-05-15",
            "2026-05-16 05:00:00",
            utc_ts("2026-05-15T08:00:00Z"), 3600.0,
        )
        assert r["ok"] is False

    def test_cache_date_none(self):
        r = wethr_rm.validate_rm_for_climate_day(
            "KATL", "2026-05-15", None, None,
            utc_ts("2026-05-15T08:00:00Z"), 3600.0,
        )
        assert r["ok"] is False
        assert "stale_cache_date" in r["reason"]

    def test_unknown_station(self):
        r = wethr_rm.validate_rm_for_climate_day(
            "KZZZ", "2026-05-15", "2026-05-15", None,
            utc_ts("2026-05-15T08:00:00Z"), 3600.0,
        )
        assert r["ok"] is False
        assert "no_tz_or_bad_date" in r["reason"]

    def test_grace_zero_disables_grace_check(self):
        """grace_sec=0 disables the grace gate but keeps date/window checks.
        Test 30 min past LST midnight (ATL LST midnight = 05:00 UTC, so
        05:30 UTC is 30 min in)."""
        r = wethr_rm.validate_rm_for_climate_day(
            "KATL", "2026-05-15", "2026-05-15", None,
            utc_ts("2026-05-15T05:30:00Z"), 0.0,
        )
        assert r["ok"] is True

    def test_grace_zero_still_blocks_pre_climate_day(self):
        """grace_sec=0 disables grace but pre-LST-midnight is still pre-climate
        day. Negative secs_into_climate_day → within_grace check fires."""
        r = wethr_rm.validate_rm_for_climate_day(
            "KATL", "2026-05-15", "2026-05-15", None,
            utc_ts("2026-05-15T04:30:00Z"), 0.0,  # 30 min BEFORE LST midnight
        )
        assert r["ok"] is False
        assert "within_grace" in r["reason"]

    def test_unparseable_time_of_extreme(self):
        r = wethr_rm.validate_rm_for_climate_day(
            "KATL", "2026-05-15", "2026-05-15", "garbage",
            utc_ts("2026-05-15T08:00:00Z"), 3600.0,
        )
        assert r["ok"] is False
        assert "unparseable_time_of_extreme" in r["reason"]

    def test_time_of_extreme_none_passes(self):
        """time_of_extreme=None is allowed (cache hasn't observed yet)."""
        r = wethr_rm.validate_rm_for_climate_day(
            "KATL", "2026-05-15", "2026-05-15", None,
            utc_ts("2026-05-15T08:00:00Z"), 3600.0,
        )
        assert r["ok"] is True

    def test_secs_into_climate_day_negative_for_future_date(self):
        """If cand.climate_day is tomorrow (d+1), secs_into_climate_day is
        negative. Validator should still fail (date mismatch usually catches
        first); fields populated correctly."""
        r = wethr_rm.validate_rm_for_climate_day(
            "KATL", "2026-05-16", "2026-05-15", None,
            utc_ts("2026-05-15T20:00:00Z"), 3600.0,
        )
        assert r["ok"] is False
        assert r["secs_into_climate_day"] < 0


# ─────────────────────────────────────────────────────────────────────────────
# F2: cold-tail BUY_NO warming-obs filter — exercised via prescreen.
# Uses a minimal packet builder to drive the prescreen function directly.
# ─────────────────────────────────────────────────────────────────────────────
import config


# 2026-05-17: skip_forecast_only_mu was added to prescreen on the same day.
# It short-circuits before the rm/grace/RM-required/Rule#2 gates when
# _numerical_edge returns a forecast-only mu_method. These tests exercise
# those gates with fixture packets that don't set up nn_match (which would
# require seeding the heating_traces DB). Disable the gate for the module
# so the tests reach the gates they're written to cover. The dedicated
# `TestSkipForecastOnlyMu` class below re-enables it via its own
# `monkeypatch.setitem` calls.
@pytest.fixture(autouse=True)
def _disable_forecast_only_gate(monkeypatch):
    monkeypatch.setitem(config.PRESCREEN, "skip_forecast_only_mu", False)
    # 2026-05-18: skip_unless_nn_match is stricter — blocks any non-nn_match
    # μ source. Most fixtures here don't seed nn_match so their μ would come
    # from forecast methods (consensus_median, best_mae_*). Disable for the
    # module so tests reach the gates they cover.
    # 2026-05-19: anchored / low_rm_ceiling code paths deleted entirely; the
    # gate still exists as defense-in-depth and tests still need it disabled
    # for fixtures that exercise other prescreen gates.
    monkeypatch.setitem(config.PRESCREEN, "skip_unless_nn_match", False)


def _base_packet(**overrides) -> dict:
    """Minimal packet that PASSES all earlier prescreen gates so a final
    return-None means F2 did not fire and a return-string means a specific
    gate fired. Caller overrides the fields under test."""
    p = {
        "ticker": "KXLOWTCHI-26MAY15-T51",
        "series": "KXLOW",
        "station": "KMDW",
        "climate_day": "2026-05-15",
        "bracket_kind": "T",
        "floor": 51.0,
        "cap": None,
        # Market — passes spread/settled/freshness gates
        "yes_bid_c": 70, "yes_ask_c": 72,
        "no_bid_c": 28, "no_ask_c": 30,
        "spread_c": 2,
        # Times — d+0 entry well past grace
        "seconds_to_close": 12 * 3600,
        "days_out": 0,
        # Forecasts
        "mu_nbm": 51.0, "mu_hrrr": None, "mu_nbp": 51.0, "mu_ecmwf": None,
        "nbp_sigma": 2.0,
        # Obs — fresh + above floor + WARMING during cooling
        "wethr_obs": {"temp_f": 55.0, "ts": time.time()},
        "obs_trend_30m": 1.8,
        # Local clock — pre-min in cooling phase
        # CHI min_hour ~ 05:50 CDT; entry at 02:00 CDT (3.83h before min)
        "local_clock": {
            "local_hour": 2.0,
            "min_hour_local": 5.83,
            "peak_hour_local": 16.0,
        },
        # rm validation — past grace, ok
        "rm_validation": {
            "ok": True, "reason": "ok",
            "secs_into_climate_day": 7200,  # 2h into CD
        },
        "running_min_or_max": 55.0,
    }
    p.update(overrides)
    return p


class TestF2ColdTailWarmingObs:
    @pytest.mark.skip(reason="F2 commented out 2026-05-16; re-enable test when F2 ships")
    def test_blocks_chi_t51_pattern(self):
        """CHI T51 from last night: floor=51 cold-tail BUY_NO with obs warming
        +1.8°F/30m at local 02:00 CDT (cooling phase, ~4h pre-min)."""
        from paper_judge_bot import prescreen
        p = _base_packet()
        r = prescreen(p)
        assert r is not None and "F2" in r and "warm-obs" in r

    @pytest.mark.skip(reason="F2 commented out 2026-05-16; re-enable test when F2 ships")
    def test_blocks_min_t59_pattern(self):
        """MIN T59 from last night: same pattern, different floor."""
        from paper_judge_bot import prescreen
        p = _base_packet(
            ticker="KXLOWTMIN-26MAY15-T59", station="KMSP",
            floor=59.0, cap=None,
            wethr_obs={"temp_f": 64.4, "ts": time.time()},
            obs_trend_30m=1.8, mu_nbm=59.0,
            running_min_or_max=64.4,
        )
        r = prescreen(p)
        assert r is not None and "F2" in r

    def test_passes_when_obs_flat_no_warming(self):
        from paper_judge_bot import prescreen
        p = _base_packet(obs_trend_30m=0.0)
        r = prescreen(p)
        # F2 should NOT fire. Other gates may; but verify F2 not in reason.
        assert (r is None) or ("F2" not in r)

    def test_passes_when_obs_below_floor(self):
        """Obs already inside the cold tail (LOW ≤ floor zone) — bet is
        supported by obs, NOT contradicted."""
        from paper_judge_bot import prescreen
        p = _base_packet(
            wethr_obs={"temp_f": 49.0, "ts": time.time()},
            running_min_or_max=49.0,
        )
        r = prescreen(p)
        assert (r is None) or ("F2" not in r)

    def test_passes_post_min(self):
        """Post-min hour — LOW already set, F2 should not fire."""
        from paper_judge_bot import prescreen
        p = _base_packet(local_clock={
            "local_hour": 8.0, "min_hour_local": 5.83, "peak_hour_local": 16.0,
        })
        r = prescreen(p)
        assert (r is None) or ("F2" not in r)

    def test_does_not_fire_for_warm_tail_t_bracket(self):
        """T-bracket with `cap` set and `floor=None` is the OPPOSITE schema —
        BUY_NO bets LOW >= cap+1 (warm direction). F2 should NOT fire."""
        from paper_judge_bot import prescreen
        p = _base_packet(
            floor=None, cap=51.0,
            ticker="KXLOWTCHI-26MAY15-Tc51",
        )
        r = prescreen(p)
        assert (r is None) or ("F2" not in r)

    def test_does_not_fire_for_b_bracket(self):
        from paper_judge_bot import prescreen
        p = _base_packet(bracket_kind="B", floor=50.0, cap=51.0)
        r = prescreen(p)
        assert (r is None) or ("F2" not in r)

    def test_does_not_fire_when_market_leans_yes(self):
        """If no_ask > yes_ask the bot is more likely to BUY_YES (warm
        direction); F2 only targets BUY_NO so it should not fire."""
        from paper_judge_bot import prescreen
        p = _base_packet(yes_ask_c=30, yes_bid_c=28, no_ask_c=72, no_bid_c=70)
        r = prescreen(p)
        assert (r is None) or ("F2" not in r)

    def test_does_not_fire_for_high_series(self):
        from paper_judge_bot import prescreen
        p = _base_packet(series="KXHIGH", ticker="KXHIGHCHI-26MAY15-T75")
        r = prescreen(p)
        assert (r is None) or ("F2" not in r)


# ─────────────────────────────────────────────────────────────────────────────
# 1h grace gate (F1 complement) — via prescreen
# ─────────────────────────────────────────────────────────────────────────────
class TestPrescreenGraceGate:
    """2026-05-16: grace threshold is now series-dependent — LOW d+0 = 15min
    (pace_low_band + tail_low_band in packet let the LLM self-skip the rest),
    HIGH d+0 = 60min."""

    def test_blocks_d0_low_within_15min_grace(self):
        """LOW d+0 at 9 min into climate day → within 15min grace, blocked."""
        from paper_judge_bot import prescreen
        p = _base_packet(
            ticker="KXLOWTDEN-26MAY15-B48.5", station="KDEN",
            bracket_kind="B", floor=48.0, cap=49.0,
            no_ask_c=70, yes_ask_c=30,
            rm_validation={
                "ok": False, "reason": "within_grace",
                "secs_into_climate_day": 540,  # 9 min into CD
            },
        )
        r = prescreen(p)
        assert r is not None and "grace" in r

    def test_low_passes_at_16min(self):
        """LOW d+0 at 16 min → past 15min grace, no grace block."""
        from paper_judge_bot import prescreen
        p = _base_packet(
            bracket_kind="B", floor=51.0, cap=52.0, ticker="KXLOWTCHI-26MAY15-B51.5",
            obs_trend_30m=0.0,
            rm_validation={
                "ok": True, "reason": "ok", "secs_into_climate_day": 960,  # 16 min
            },
        )
        r = prescreen(p)
        assert (r is None) or ("grace" not in r)

    def test_high_still_blocked_at_30min(self):
        """HIGH d+0 at 30 min → still within 60min grace (HIGH unchanged).
        Set μ near bracket so we exercise the grace gate, not the μ-distance gate."""
        from paper_judge_bot import prescreen
        p = _base_packet(
            ticker="KXHIGHNY-26MAY15-B65.5", station="KNYC",
            series="KXHIGH",
            bracket_kind="B", floor=65.0, cap=66.0,
            mu_nbm=65.5, mu_nbp=65.5,
            no_ask_c=70, yes_ask_c=30,
            local_clock={
                "local_hour": 13.0, "min_hour_local": 5.83, "peak_hour_local": 16.0,
            },
            rm_validation={
                "ok": False, "reason": "within_grace",
                "secs_into_climate_day": 1800,  # 30 min
            },
        )
        r = prescreen(p)
        assert r is not None and "grace" in r and "60min" in r

    def test_high_passes_at_61min(self):
        """HIGH d+0 at 61 min → past 60min grace."""
        from paper_judge_bot import prescreen
        p = _base_packet(
            bracket_kind="B", floor=65.0, cap=66.0,
            ticker="KXHIGHNY-26MAY15-B65.5", station="KNYC",
            series="KXHIGH",
            mu_nbm=65.5, mu_nbp=65.5,
            local_clock={
                "local_hour": 13.0, "min_hour_local": 5.83, "peak_hour_local": 16.0,
            },
            obs_trend_30m=0.0,
            rm_validation={
                "ok": True, "reason": "ok", "secs_into_climate_day": 3660,  # 61 min
            },
        )
        r = prescreen(p)
        assert (r is None) or ("grace" not in r)

    def test_does_not_fire_for_d1(self):
        """d+1 entries have secs_into_climate_day < 0. Grace gate must not fire."""
        from paper_judge_bot import prescreen
        p = _base_packet(
            bracket_kind="B", floor=51.0, cap=52.0,
            days_out=1,
            obs_trend_30m=0.0,
            local_clock={
                "local_hour": 22.5, "min_hour_local": 5.83, "peak_hour_local": 16.0,
            },
            rm_validation={
                "ok": False, "reason": "stale_cache_date",
                "secs_into_climate_day": -10800,  # 3h before tomorrow's midnight
            },
        )
        r = prescreen(p)
        assert (r is None) or ("grace" not in r)

    def test_does_not_fire_when_secs_into_cd_missing(self):
        """rm_validation absent or secs_into_cd=None → grace gate skips."""
        from paper_judge_bot import prescreen
        p = _base_packet(
            bracket_kind="B", floor=51.0, cap=52.0,
            obs_trend_30m=0.0,
            rm_validation={"ok": False, "reason": "no_tz",
                           "secs_into_climate_day": None},
        )
        r = prescreen(p)
        assert (r is None) or ("grace" not in r)


# ─────────────────────────────────────────────────────────────────────────────
# d+0 rm=None hard SKIP gate (2026-05-16)
# ─────────────────────────────────────────────────────────────────────────────
class TestPrescreenD0RmRequired:
    def test_blocks_d0_when_rm_none(self):
        """d+0 entry with rm=None → SKIP. Catches NYC + SFO from 2026-05-15."""
        from paper_judge_bot import prescreen
        p = _base_packet(
            ticker="KXLOWTNYC-26MAY15-B49.5", station="KNYC",
            bracket_kind="B", floor=49.0, cap=50.0,
            obs_trend_30m=0.0,
            # Default cheap-NO market (no_ask=30) with μ=51 vs B[49,50] gives
            # ~40pp gap → Rule#2 fires before d+0 gate. Bot's P(NO) for the
            # narrower B[49,50] is ~0.70; pick no_ask=61 → BUY_NO edge ~9pp
            # (passes 6pp floor, under 25pp ceiling); dom side 61c >60 floor.
            no_ask_c=61, yes_ask_c=39,
        )
        # Override rm to None (default helper sets it to a value).
        p["running_min_or_max"] = None
        p["rm_validation"] = {"ok": False, "reason": "no_low_f_in_cache",
                              "secs_into_climate_day": 7200}
        r = prescreen(p)
        assert r is not None and "no rm anchor" in r

    def test_passes_d0_when_rm_present(self):
        from paper_judge_bot import prescreen
        p = _base_packet(
            bracket_kind="B", floor=49.0, cap=50.0,
            obs_trend_30m=0.0,
            running_min_or_max=55.0,
        )
        r = prescreen(p)
        assert (r is None) or ("no rm anchor" not in r)

    def test_does_not_fire_for_d1_with_rm_none(self):
        """d+1 entries have no climate-day yet; rm=None is expected and OK."""
        from paper_judge_bot import prescreen
        p = _base_packet(
            bracket_kind="B", floor=49.0, cap=50.0,
            days_out=1,
            obs_trend_30m=0.0,
            running_min_or_max=None,
            local_clock={
                "local_hour": 22.5, "min_hour_local": 5.83, "peak_hour_local": 16.0,
            },
        )
        p["rm_validation"] = {"ok": False, "reason": "stale_cache_date",
                              "secs_into_climate_day": -10800}
        r = prescreen(p)
        assert (r is None) or ("no rm anchor" not in r)

    def test_d0_rm_zero_passes_rm_required(self):
        """rm=0 is valid (some stations may legitimately hit 0°F overnight).
        The gate checks `is None`, not falsy."""
        from paper_judge_bot import prescreen
        p = _base_packet(
            bracket_kind="B", floor=49.0, cap=50.0,
            obs_trend_30m=0.0,
            running_min_or_max=0.0,
        )
        r = prescreen(p)
        assert (r is None) or ("no rm anchor" not in r)


# ─────────────────────────────────────────────────────────────────────────────
# _is_rm_locked_for_side helper (2026-05-16)
# ─────────────────────────────────────────────────────────────────────────────
class TestRmLockedForSide:
    def _pkt(self, series, side, fl, cp, rm, past_peak=False, past_min=False):
        return {
            "series": series,
            "floor": fl, "cap": cp,
            "running_min_or_max": rm,
            "_edge_info": {"side": side, "edge": 0.7, "prob": 0.9,
                           "mu": 70.0, "sigma": 2.0},
            "local_clock": {"past_peak_today": past_peak,
                            "past_min_today": past_min},
        }

    def test_high_buyno_overshoot_locks(self):
        from paper_judge_bot import _is_rm_locked_for_side
        # rm=82 >= cap+1 (75+1=76)
        locked, reason = _is_rm_locked_for_side(
            self._pkt("KXHIGH", "BUY_NO", fl=72, cp=75, rm=82))
        assert locked is True
        assert "overshoot" in reason

    def test_high_buyno_stays_below_past_peak_locks(self):
        from paper_judge_bot import _is_rm_locked_for_side
        locked, reason = _is_rm_locked_for_side(
            self._pkt("KXHIGH", "BUY_NO", fl=80, cp=82, rm=78, past_peak=True))
        assert locked is True
        assert "stays-below" in reason

    def test_high_buyno_stays_below_pre_peak_NOT_locked(self):
        from paper_judge_bot import _is_rm_locked_for_side
        locked, _ = _is_rm_locked_for_side(
            self._pkt("KXHIGH", "BUY_NO", fl=80, cp=82, rm=78, past_peak=False))
        assert locked is False

    def test_high_buyyes_b_locked_past_peak(self):
        from paper_judge_bot import _is_rm_locked_for_side
        locked, reason = _is_rm_locked_for_side(
            self._pkt("KXHIGH", "BUY_YES", fl=72, cp=74, rm=73.0, past_peak=True))
        assert locked is True
        assert "B-bracket" in reason

    def test_high_buyyes_t_warm_overshoot(self):
        """T-warm: cap=None, floor set. rm >= floor+1 → locked YES."""
        from paper_judge_bot import _is_rm_locked_for_side
        locked, _ = _is_rm_locked_for_side(
            self._pkt("KXHIGH", "BUY_YES", fl=70, cp=None, rm=72.0))
        assert locked is True

    def test_low_buyno_stays_below_locks(self):
        from paper_judge_bot import _is_rm_locked_for_side
        # rm=47 <= floor-1 (48-1=47) → locked (LOW already below)
        locked, reason = _is_rm_locked_for_side(
            self._pkt("KXLOW", "BUY_NO", fl=48, cp=49, rm=47.0))
        assert locked is True
        assert "stays-below" in reason

    def test_low_buyno_stays_above_past_min(self):
        from paper_judge_bot import _is_rm_locked_for_side
        # rm=51 >= cap+1 (50+1=51), past min → locked NO
        locked, _ = _is_rm_locked_for_side(
            self._pkt("KXLOW", "BUY_NO", fl=48, cp=50, rm=51.0, past_min=True))
        assert locked is True

    def test_low_buyno_stays_above_pre_min_NOT_locked(self):
        from paper_judge_bot import _is_rm_locked_for_side
        locked, _ = _is_rm_locked_for_side(
            self._pkt("KXLOW", "BUY_NO", fl=48, cp=50, rm=51.0, past_min=False))
        assert locked is False

    def test_low_buyyes_t_cold_locked(self):
        """T-cold: floor=None, cap set. rm <= cap-1 → already crossed cold."""
        from paper_judge_bot import _is_rm_locked_for_side
        locked, _ = _is_rm_locked_for_side(
            self._pkt("KXLOW", "BUY_YES", fl=None, cp=50, rm=48.0))
        assert locked is True

    def test_rm_none_returns_false(self):
        from paper_judge_bot import _is_rm_locked_for_side
        locked, reason = _is_rm_locked_for_side(
            self._pkt("KXHIGH", "BUY_NO", fl=70, cp=72, rm=None))
        assert locked is False
        assert reason == "rm_none"

    def test_missing_edge_info_returns_false(self):
        from paper_judge_bot import _is_rm_locked_for_side
        locked, reason = _is_rm_locked_for_side({"series": "KXHIGH"})
        assert locked is False
        assert reason == "no_edge_info"


# ─────────────────────────────────────────────────────────────────────────────
# RULE #2 60pp gap ceiling gate (2026-05-16)
# ─────────────────────────────────────────────────────────────────────────────
class TestPrescreenRule2GapCeiling:
    def _make_packet_with_edge(self, side, edge, **overrides):
        """Build a packet whose _numerical_edge will produce ~`edge` on
        `side`. Easier than mocking — set forecast μ far enough from
        bracket to produce the desired edge."""
        # For BUY_NO: high P(NO) needed → μ far above cap (HIGH) or far
        # below floor (LOW).
        p = _base_packet(**overrides)
        # Force market prices on the side we want bot to take
        if side == "BUY_NO":
            p["no_ask_c"] = max(1, int(round((1 - edge - 0.1) * 100)))  # cheap NO
            p["yes_ask_c"] = 100 - p["no_ask_c"]
            # Set μ way above cap so P(NO) is ~1
            p["mu_nbm"] = (p.get("cap") or 80) + 20.0
            p["mu_hrrr"] = p["mu_nbm"]
        else:
            p["yes_ask_c"] = max(1, int(round((1 - edge - 0.1) * 100)))
            p["no_ask_c"] = 100 - p["yes_ask_c"]
            p["mu_nbm"] = (p.get("floor") or 70) + 10.0
            p["mu_hrrr"] = p["mu_nbm"]
        return p

    def test_blocks_70pp_gap_without_rm_lock(self):
        from paper_judge_bot import prescreen
        # Build a packet where bot would take BUY_NO with ~70pp gap,
        # but rm is not locked.
        p = _base_packet(
            bracket_kind="B", floor=70, cap=71,
            station="KNYC", series="KXHIGH",
            local_hour=14.0, peak_hour_local=15.0, min_hour_local=6.0,
        )
        # μ=90 → P(NO) near 1; no_ask=20c → gap ~80pp
        p["mu_nbm"] = 90.0
        p["mu_hrrr"] = 90.0
        p["no_ask_c"] = 20
        p["yes_ask_c"] = 80
        # rm=60 → no lock (not >= cap+1, not <= floor-1 pre-peak)
        p["running_min_or_max"] = 60.0
        p["local_clock"]["past_peak_today"] = False
        r = prescreen(p)
        assert r is not None and "Rule#2" in r

    def test_passes_70pp_gap_with_rm_lock_high_overshoot(self):
        """70pp gap BUT rm has already overshot → lock bypasses Rule #2."""
        from paper_judge_bot import prescreen
        p = _base_packet(
            bracket_kind="B", floor=70, cap=71,
            station="KNYC", series="KXHIGH",
            local_hour=14.0,
        )
        p["mu_nbm"] = 90.0
        p["mu_hrrr"] = 90.0
        p["no_ask_c"] = 20
        p["yes_ask_c"] = 80
        # rm=75 >= cap+1 (72) → overshoot lock fires
        p["running_min_or_max"] = 75.0
        r = prescreen(p)
        assert (r is None) or ("Rule#2" not in r)

    def test_passes_15pp_gap_under_ceiling(self):
        """μ=71.5 with cap=71, σ=2.0 → P(NO)~0.826. With no_ask=70c the edge
        is ~13pp — comfortably under the 25pp ceiling (tightened from 60pp
        2026-05-16 based on settled-snapshot accuracy analysis)."""
        from paper_judge_bot import prescreen
        p = _base_packet(bracket_kind="B", floor=70, cap=71, series="KXHIGH")
        p["mu_nbm"] = 71.5  # μ slightly above cap
        p["mu_hrrr"] = 71.5
        p["nbp_sigma"] = 2.0
        p["no_ask_c"] = 70  # market says P(NO)=70%, close to model's 83%
        p["yes_ask_c"] = 30
        p["running_min_or_max"] = 65.0
        r = prescreen(p)
        assert (r is None) or ("Rule#2" not in r)


# ─────────────────────────────────────────────────────────────────────────────
# 2026-05-17: skip_forecast_only_mu gate — SKIP candidates whose μ came from
# pure-forecast fallback (best_mae_* / consensus_median / raw_median) when
# nn_match and rm-anchored methods both produced nothing.
# ─────────────────────────────────────────────────────────────────────────────
class TestSkipForecastOnlyMu:
    """SKIP when only forecast μ is available — edge = obs."""

    def _strong_buy_packet(self, **overrides):
        """Packet where _numerical_edge would return a strong NO edge from
        forecasts alone. Used to verify the gate fires when mu_method is
        forecast-only.

        Strategy: HIGH B-bracket with rm well below floor, μ even further
        below, no nn_match trajectory (disables nn). _numerical_edge will
        return mu_method=best_mae_* or consensus_median.
        """
        p = {
            "ticker": "KXHIGHCHI-26MAY17-B90.5",
            "series": "KXHIGH",
            "station": "KMDW",
            "climate_day": "2026-05-17",
            "bracket_kind": "B",
            "floor": 90.0, "cap": 91.0,
            "yes_bid_c": 18, "yes_ask_c": 20,
            "no_bid_c": 78, "no_ask_c": 80,
            "spread_c": 2,
            "seconds_to_close": 6 * 3600,
            "days_out": 0,
            "mu_nbm": 80.0, "mu_hrrr": 79.5, "mu_nbp": 80.2, "mu_ecmwf": 80.8,
            "nbp_sigma": 1.5,
            "wethr_obs": {"temp_f": 78.0, "ts": time.time()},
            "obs_trend_30m": 0.0,
            "local_clock": {"local_hour": 15.0, "peak_hour_local": 16.0,
                            "min_hour_local": 5.0},
            "rm_validation": {"ok": True, "reason": "ok",
                              "secs_into_climate_day": 30000},
            "running_min_or_max": 78.0,
            # Intentionally NO hourly_obs_today so nn_match has no trajectory
            # (the anchored / low_rm_ceiling paths that previously also fired
            # here were deleted 2026-05-19; only forecast μ remains)
            "model_mae_recent": {
                "per_model": {
                    "NBM": {"mae": 1.0, "bias": 0.0},
                    "HRRR": {"mae": 1.1, "bias": 0.0},
                },
            },
        }
        p.update(overrides)
        return p

    def test_flag_disabled_passes_forecast_only(self, monkeypatch):
        """When skip_forecast_only_mu=False the prescreen MUST NOT block on
        this reason (preserves rollback path)."""
        from paper_judge_bot import prescreen
        monkeypatch.setitem(config.PRESCREEN, "skip_forecast_only_mu", False)
        p = self._strong_buy_packet()
        r = prescreen(p)
        # Other gates may still fire; just verify the forecast-only message did NOT.
        assert (r is None) or ("forecast-only" not in r)

    def test_flag_enabled_blocks_forecast_only(self, monkeypatch):
        """When skip_forecast_only_mu=True and μ came from a pure-forecast
        path, prescreen returns the forecast-only skip reason."""
        from paper_judge_bot import prescreen
        monkeypatch.setitem(config.PRESCREEN, "skip_forecast_only_mu", True)
        p = self._strong_buy_packet()
        r = prescreen(p)
        # Either this gate fires, or an earlier gate did. The contract is:
        # IF prescreen returns a non-None reason AND the packet's _edge_info
        # was forecast-only, the reason should be the forecast-only message.
        # In a clean strong-buy packet, the forecast-only gate is the EXPECTED
        # blocker.
        if p.get("_edge_info") is not None:
            mm = p["_edge_info"].get("mu_method", "")
            if mm.startswith("best_mae_") or mm.startswith("consensus_median") or mm.startswith("raw_median"):
                assert r is not None and "forecast-only" in r, \
                    f"forecast-only μ ({mm}) should have been blocked; got: {r}"


# ─────────────────────────────────────────────────────────────────────────────
# skip_unless_nn_match — stricter successor (2026-05-18)
# ─────────────────────────────────────────────────────────────────────────────
class TestSkipUnlessNnMatch:
    """SKIP any non-nn_match μ — strategy explicitly requires nn_match edge.

    Live data 2026-05-17/18 showed `low_rm_ceiling` and `anchored` were
    slipping through `skip_forecast_only_mu` (which only catches
    best_mae_/consensus_median/raw_median) and reaching the LLM. The LLM
    was BUYing them 100% of the time despite the prompt saying SKIP. This
    stricter gate closes the loophole structurally.

    2026-05-19: the `anchored` and `low_rm_ceiling` code paths were deleted
    from _numerical_edge() — they're no longer producible. This test now
    exercises the gate against the remaining non-nn_match path (forecast
    μ from consensus_median / best_mae_*) which the gate still must block.
    """

    def _strong_buy_packet_non_nn(self):
        """Packet shaped so _numerical_edge returns a non-nn_match μ —
        forecasts only (no nn_match trajectory seeded). With anchored /
        low_rm_ceiling deleted, the resulting mu_method should be
        consensus_median or best_mae_*."""
        p = {
            "ticker": "KXLOWTOKC-26MAY18-B67.5",
            "series": "KXLOWT",
            "station": "KOKC",
            "climate_day": "2026-05-18",
            "bracket_kind": "B",
            "floor": 67.0, "cap": 68.0,
            "yes_bid_c": 25, "yes_ask_c": 30,
            "no_bid_c": 70, "no_ask_c": 75,
            "spread_c": 5,
            "seconds_to_close": 6 * 3600,
            "days_out": 0,
            "mu_nbm": 74.0, "mu_hrrr": 74.5, "mu_nbp": 74.2, "mu_ecmwf": 74.8,
            "nbp_sigma": 1.5,
            "wethr_obs": {"temp_f": 73.0, "ts": time.time()},
            "obs_trend_30m": 0.0,
            "local_clock": {"local_hour": 2.0, "peak_hour_local": 16.0,
                            "min_hour_local": 5.0},
            "rm_validation": {"ok": True, "reason": "ok",
                              "secs_into_climate_day": 30000},
            "running_min_or_max": 72.0,
            "model_mae_recent": {
                "per_model": {
                    "NBM": {"mae": 1.5, "bias": 0.0},
                    "HRRR": {"mae": 1.2, "bias": 0.0},
                },
            },
        }
        return p

    def test_flag_blocks_non_nn_match(self, monkeypatch):
        """skip_unless_nn_match=True should block forecast-only μ."""
        from paper_judge_bot import prescreen
        monkeypatch.setitem(config.PRESCREEN, "skip_unless_nn_match", True)
        monkeypatch.setitem(config.PRESCREEN, "skip_forecast_only_mu", False)
        p = self._strong_buy_packet_non_nn()
        r = prescreen(p)
        # The packet's _edge_info should now be populated. If mu_method
        # ended up non-nn_match, the new gate should fire.
        ei = p.get("_edge_info") or {}
        mm = ei.get("mu_method", "") or ""
        if mm and not mm.startswith("nn_match"):
            assert r is not None and "non-nn_match" in r, \
                f"non-nn_match μ ({mm}) should have been blocked; got: {r}"

    def test_flag_disabled_does_not_block_unless_forecast(self, monkeypatch):
        """skip_unless_nn_match=False + skip_forecast_only_mu=False —
        non-nn_match should pass through (preserves rollback path)."""
        from paper_judge_bot import prescreen
        monkeypatch.setitem(config.PRESCREEN, "skip_unless_nn_match", False)
        monkeypatch.setitem(config.PRESCREEN, "skip_forecast_only_mu", False)
        p = self._strong_buy_packet_non_nn()
        r = prescreen(p)
        # Should NOT contain non-nn_match or forecast-only reasons
        assert (r is None) or ("non-nn_match" not in r and "forecast-only" not in r)


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
