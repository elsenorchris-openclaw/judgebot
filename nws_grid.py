"""nws_grid.py — NWS gridpoint hourly forecast + cloud/precip/wind trajectory.

Two-step API:
  1. /points/{lat},{lon}  → gridpoint metadata (forecastOffice + grid X,Y).
                            Cached forever — fixed per station.
  2. /gridpoints/{wfo}/{x},{y}  → raw gridded per-hour data: temperature,
                            dewpoint, skyCover, windSpeed, windDirection,
                            probabilityOfPrecipitation, weather text.

Returns a normalized list of {utc_iso, temp_f, dewpt_f, wind_mph,
wind_dir_deg, sky_cover_pct, precip_prob_pct, weather} dicts for the
next N hours.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("judge.nws_grid")

NWS_BASE = "https://api.weather.gov"
NWS_UA = "paper_judge_bot (chris@example.com)"

_gridpoint_cache: dict[str, dict] = {}   # station -> {office, x, y}
_forecast_cache: dict[str, tuple[float, list]] = {}  # station -> (ts, hourly_list)
_FORECAST_CACHE_TTL = 600.0  # 10 min — gridpoint hourly updates ~every hour


def _httpx():
    import httpx
    return httpx


def _resolve_gridpoint(lat: float, lon: float, station: str) -> Optional[dict]:
    """Resolve station coords to NWS gridpoint. Cached per-station."""
    if station in _gridpoint_cache:
        return _gridpoint_cache[station]
    try:
        httpx = _httpx()
        r = httpx.get(
            f"{NWS_BASE}/points/{lat},{lon}",
            headers={"User-Agent": NWS_UA, "Accept": "application/geo+json"},
            timeout=10.0,
        )
        r.raise_for_status()
        d = r.json() or {}
        props = d.get("properties") or {}
        rec = {
            "office": props.get("gridId") or props.get("cwa"),
            "x": props.get("gridX"),
            "y": props.get("gridY"),
            "forecast_zone": props.get("forecastZone"),
            "forecast_url": props.get("forecast"),
            "hourly_url": props.get("forecastHourly"),
            "raw_url": (
                f"{NWS_BASE}/gridpoints/{props.get('gridId')}/{props.get('gridX')},{props.get('gridY')}"
                if all(props.get(k) is not None for k in ("gridId", "gridX", "gridY"))
                else None
            ),
        }
        _gridpoint_cache[station] = rec
        return rec
    except Exception as e:
        log.warning("gridpoint resolve %s (%f,%f) failed: %s", station, lat, lon, e)
        return None


def _c_to_f(v) -> Optional[float]:
    if v is None: return None
    try:
        c = float(v)
        return round(c * 9.0 / 5.0 + 32.0, 1)
    except (TypeError, ValueError): return None


def _ms_to_mph(v) -> Optional[float]:
    if v is None: return None
    try: return round(float(v) * 2.23694, 1)
    except (TypeError, ValueError): return None


def _parse_iso(s) -> Optional[float]:
    if not s: return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _series_at_time(series: list, target_ts: float) -> Optional[float]:
    """Given a series of {validTime: 'ISO/PT1H', value: X} entries, return the
    value whose validTime range contains target_ts. Returns None if outside.

    NWS raw gridpoint format uses validTime like '2026-05-14T18:00:00+00:00/PT3H'
    meaning "valid from this start time, for this many hours". A single value
    covers a span; we expand into per-hour buckets at the calling layer.
    """
    if not series: return None
    for entry in series:
        vt = entry.get("validTime", "")
        if "/" not in vt: continue
        start_iso, dur = vt.split("/", 1)
        start_ts = _parse_iso(start_iso)
        if start_ts is None: continue
        # PT1H, PT3H, PT12H, etc.
        try:
            hours = int(dur.lstrip("PT").rstrip("H")) if "H" in dur else 1
        except ValueError:
            hours = 1
        end_ts = start_ts + hours * 3600
        if start_ts <= target_ts < end_ts:
            return entry.get("value")
    return None


def get_hourly_forecast(
    lat: float, lon: float, station: str, hours: int = 24
) -> list[dict]:
    """Pull NWS raw gridpoint data + flatten into per-hour records for the
    next `hours` hours from now. Each record:
      {utc_iso, local_offset_h, temp_f, dewpt_f, wind_mph, wind_dir_deg,
       sky_cover_pct, precip_prob_pct, weather, ceiling_ft}.
    Returns empty list on failure.
    """
    # Cached for 10 min
    hit = _forecast_cache.get(station)
    if hit and (time.time() - hit[0]) < _FORECAST_CACHE_TTL:
        return hit[1]
    gp = _resolve_gridpoint(lat, lon, station)
    if not gp or not gp.get("raw_url"):
        return []
    try:
        httpx = _httpx()
        r = httpx.get(
            gp["raw_url"],
            headers={"User-Agent": NWS_UA, "Accept": "application/geo+json"},
            timeout=15.0,
        )
        r.raise_for_status()
        props = (r.json() or {}).get("properties") or {}
    except Exception as e:
        log.warning("gridpoint hourly fetch %s failed: %s", station, e)
        return []

    temp_series   = (props.get("temperature") or {}).get("values") or []
    dew_series    = (props.get("dewpoint") or {}).get("values") or []
    wind_s_series = (props.get("windSpeed") or {}).get("values") or []
    wind_d_series = (props.get("windDirection") or {}).get("values") or []
    sky_series    = (props.get("skyCover") or {}).get("values") or []
    pop_series    = (props.get("probabilityOfPrecipitation") or {}).get("values") or []
    weather_series = (props.get("weather") or {}).get("values") or []
    ceil_series   = (props.get("ceilingHeight") or {}).get("values") or []

    now_ts = time.time()
    out: list[dict] = []
    for h in range(hours):
        ts = now_ts + h * 3600
        rec = {
            "utc_iso": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:00Z"),
            "hour_offset": h,
            "temp_f": _c_to_f(_series_at_time(temp_series, ts)),
            "dewpt_f": _c_to_f(_series_at_time(dew_series, ts)),
            "wind_mph": (
                # gridpoint wind is km/h
                round(_series_at_time(wind_s_series, ts) * 0.621371, 1)
                if _series_at_time(wind_s_series, ts) is not None else None
            ),
            "wind_dir_deg": _series_at_time(wind_d_series, ts),
            "sky_cover_pct": _series_at_time(sky_series, ts),
            "precip_prob_pct": _series_at_time(pop_series, ts),
            "ceiling_ft": (
                # ceiling is meters → ft
                round(_series_at_time(ceil_series, ts) * 3.281, 0)
                if _series_at_time(ceil_series, ts) is not None else None
            ),
        }
        # weather is a structured list at validTime — grab any non-empty entry
        wx = _series_at_time(weather_series, ts)
        if isinstance(wx, list) and wx:
            w0 = wx[0] if isinstance(wx[0], dict) else {}
            rec["weather"] = w0.get("weather") or w0.get("intensity") or None
        out.append(rec)
    _forecast_cache[station] = (time.time(), out)
    return out
