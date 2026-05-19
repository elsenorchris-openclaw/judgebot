"""live_data.py — cycle-scoped prefilled data layer.

Called once at the top of each entry cycle. Pulls everything Claude could
need to evaluate any ticker, per-station and global, into a single dict.
Per-ticker packet builders read from this cache — no per-decision API calls,
no race conditions across workers.

Schema returned by prefetch():
{
  "by_station": {
      "KATL": {
          "nws_obs":       {temp_f, dewpt_f, sky, wind_mph, trend_30m_f, ts_iso, age_sec},
          "wethr_obs":     {temp_f, dew_point_f, cloud_layer_count, wind_speed_mph,
                            wind_gust_mph, relative_humidity, anomaly_f, ...},
          "running_min":   float | None,
          "running_max":   float | None,
          "climate":       {peak_f, low_f, month},
          "clock":         {local_iso, local_hour, peak_hour_local, min_hour_local,
                            h_to_peak, h_to_min, past_peak_today, past_min_today},
      },
      ...
  },
  "forecasts_by_station_day_kind": { (station, climate_day, kind): {NBP, NBM, HRRR, ECMWF-IFS} },
  "fetched_ts": <unix-ts>,
}
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import config
import forecast_delta
import nbp_reader
import nws_afd
import nws_grid
import nws_fc_history
import obs_client
import persistence
import shared_cache_reader as scache
import station_meta
import wethr_client
import wethr_rm
from climate_normals import get_normals, local_clock_context


log = logging.getLogger("judge.live_data")


def prefetch(stations: list[str]) -> dict:
    """Pre-fetch all per-station and per-(station,date) data for the cycle.
    Stations comes from config.STATIONS unless overridden. Forecasts are
    fetched for each station × today/tomorrow × {low, high}."""
    t0 = time.time()
    now_utc = time.time()
    now_month = datetime.fromtimestamp(now_utc, tz=timezone.utc).month

    # ── per-station: NWS obs, wethr obs, climate normals, local clock ──
    by_station: dict[str, dict] = {}

    # 2026-05-16 (P1 fix): wethr_client.fetch_all() removed from prefetch.
    # Its result populated by_station[st]["wethr_obs"] which has no consumers
    # (build_entry_packet reads scache.get_wethr_obs() directly from the
    # shared cache, build_exit_packet does the same). The fetch_all loop was
    # ~20s/cycle of network time on a discarded result. fetch_model_mae below
    # is a separate endpoint and still needed.
    log.info("prefetch: fetching NWS + climate normals (wethr served by shared cache)")

    for st in stations:
        nws = obs_client.get_obs(st)
        wethr = None
        today_utc_iso = datetime.fromtimestamp(now_utc, tz=timezone.utc).strftime("%Y-%m-%d")
        # 2026-05-14: wethr_rm is now the SOLE source for running_max/min.
        # Audit (n=190): wethr MAE 0.13°F vs obs-pipeline 0.42°F. No fallback —
        # if wethr unavailable for a station, return None and skip trading.
        # 2026-05-16 (F1): also propagate the wethr cache's `date` field and
        # time_of_low/high so the caller can validate freshness against the
        # ticker's climate_day (catches pre-LDT-midnight stale-rm scenario).
        _w = wethr_rm.get(st)
        if _w is None:
            rmin_rmax = None
            wethr_running_date = None
            wethr_date_low = None
            wethr_date_high = None
            wethr_time_of_low_utc = None
            wethr_time_of_high_utc = None
        else:
            rmin_rmax = (
                float(_w["low_f"]) if _w.get("low_f") is not None else None,
                float(_w["high_f"]) if _w.get("high_f") is not None else None,
            )
            wethr_running_date = _w.get("date")
            # 2026-05-16: wethr-cache-service now derives per-side LST climate
            # day from time_of_*_utc. wethr's own `date` label lags 1-2 days
            # (only flips at CLI ingest), so F1 validator should prefer these.
            wethr_date_low = _w.get("date_low")
            wethr_date_high = _w.get("date_high")
            wethr_time_of_low_utc = _w.get("time_of_low_utc")
            wethr_time_of_high_utc = _w.get("time_of_high_utc")
        normals = get_normals(st, now_month)
        clock = local_clock_context(st, now_utc)
        meta = station_meta.get(st) or {}
        lat, lon = meta.get("lat"), meta.get("lon")

        # Hourly forecast (next 24h)
        hourly_fc = nws_grid.get_hourly_forecast(lat, lon, st, hours=24) if lat and lon else []
        # 2026-05-16: snapshot the rolling forecast so pace_slope can match
        # past obs hours against the forecast that was valid at that hour.
        # Without this, NWS gridpoint (future-only) never overlaps past obs
        # and obs_vs_forecast_pace_slope is null 100% of the time.
        try:
            nws_fc_history.record_snapshot(st, hourly_fc, now_ts=now_utc)
        except Exception:  # never let snapshot persistence break a cycle
            log.exception('nws_fc_history.record_snapshot failed for %s', st)
        # 2026-05-18: snapshot altimeter for nn_match pres1_trajectory.
        # Backtest -0.040°F LOW MAE on seed=1 held-out n=11k. nn_shadow
        # reads pres_history.jsonl, converts altimeter→station_pres via
        # station elevation, builds trajectory for nn_match_fast.predict().
        try:
            import pres_history as _ph
            _alt = _w.get("altimeter") if _w else None
            if _alt is not None:
                _ph.record_snapshot(st, _alt, now_ts=now_utc)
        except Exception:
            log.exception("pres_history.record_snapshot failed for %s", st)
        # AFD excerpt (per WFO; cached 1h)
        gridpoint = nws_grid._gridpoint_cache.get(st) or {}
        wfo = gridpoint.get("office")
        afd = nws_afd.get_afd_excerpt(wfo) if wfo else None
        # Per-model MAE from wethr daily_detail (12h cache)
        mae_high = wethr_client.fetch_model_mae(st, "high")
        mae_low = wethr_client.fetch_model_mae(st, "low")
        # 3-day persistence (actual vs forecast bias)
        bias_high = persistence.get_3day_bias(st, "high")
        bias_low = persistence.get_3day_bias(st, "low")

        by_station[st] = {
            "nws_obs": (
                {
                    "temp_f": nws.temp_f, "dewpt_f": nws.dewpt_f,
                    "sky": nws.sky, "wind_mph": nws.wind_mph,
                    "trend_30m_f": nws.trend_30m_f, "ts_iso": nws.ts_iso,
                    "age_sec": nws.age_sec, "source": nws.source,
                } if nws else None
            ),
            "wethr_obs": wethr,
            "running_min_today": rmin_rmax[0] if rmin_rmax else None,
            "running_max_today": rmin_rmax[1] if rmin_rmax else None,
            # 2026-05-16 (F1): freshness metadata used downstream to detect
            # stale rm (e.g., pre-LDT-midnight entries reading yesterday's
            # cached low/high).
            "wethr_running_date": wethr_running_date,
            "wethr_date_low": wethr_date_low,
            "wethr_date_high": wethr_date_high,
            "wethr_time_of_low_utc": wethr_time_of_low_utc,
            "wethr_time_of_high_utc": wethr_time_of_high_utc,
            "climate": (
                {"peak_f": normals[0], "low_f": normals[1], "month": now_month}
                if normals else None
            ),
            "clock": clock,
            "hourly_forecast_24h": hourly_fc,
            "afd": afd,
            "model_mae_high": mae_high,
            "model_mae_low": mae_low,
            "persistence_high": bias_high,
            "persistence_low": bias_low,
            "wfo": wfo,
            "lat": lat, "lon": lon,
        }

    # ── forecasts: per (station, climate_day, kind) ──
    # climate_days we care about: today, tomorrow (UTC frame). Per-ticker
    # mapping handled by packet builder using actual ticker climate_day.
    today_utc_date = datetime.fromtimestamp(now_utc, tz=timezone.utc).strftime("%Y-%m-%d")
    tomorrow_utc_date = datetime.fromtimestamp(now_utc + 86400, tz=timezone.utc).strftime("%Y-%m-%d")

    forecasts: dict[tuple, dict] = {}
    for st in stations:
        for day in (today_utc_date, tomorrow_utc_date):
            for kind in ("low", "high"):
                fcs = scache.get_forecast(st, day, kind=kind)
                nbp = nbp_reader.get_nbp(st, day, kind=kind)
                if nbp and nbp.get("value_f") is not None:
                    fcs["NBP"] = {
                        "value_f": nbp["value_f"],
                        "age_sec": nbp.get("age_sec"),
                        "raw": nbp.get("raw"),
                    }
                forecasts[(st, day, kind)] = {
                    "sources": fcs,
                    "nbp_extra": (
                        {"sigma": nbp.get("sigma"), "p10": nbp.get("p10"),
                         "p50": nbp.get("p50"), "p90": nbp.get("p90")}
                        if nbp else None
                    ),
                    "disagreement_f": scache.summarize_disagreement(fcs),
                }

    elapsed = time.time() - t0
    log.info("prefetch complete in %.1fs (%d stations, %d forecast keys)",
             elapsed, len(by_station), len(forecasts))
    return {
        "by_station": by_station,
        "forecasts_by_station_day_kind": forecasts,
        "fetched_ts": now_utc,
    }


def get_station(cycle_data: dict, station: str) -> dict:
    """Convenience getter — returns the per-station block or empty dict."""
    return (cycle_data.get("by_station") or {}).get(station) or {}


def get_forecast(cycle_data: dict, station: str, climate_day: str, kind: str) -> dict:
    """Convenience getter for forecast block. Returns empty dict on miss."""
    key = (station, climate_day, kind)
    return (cycle_data.get("forecasts_by_station_day_kind") or {}).get(key) or {}
