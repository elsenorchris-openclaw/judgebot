"""climate_normals.py — per-station May climate normals + diurnal timing.

For each station, exposes:
  - normal_peak_f / normal_low_f : 30-year May averages (rough — within 2°F)
  - typical_peak_hour_local       : hour-of-day the daily HIGH usually happens
  - typical_min_hour_local        : hour-of-day the daily LOW usually happens

These are used by the entry-packet builder to give Claude diurnal context:
"is local time before/at/past the peak window?", "how far is current temp
from the climate norm?".

Source: NOAA NCEI 1991-2020 normals, approximated. Precision target ±2°F
on the normals (good enough for prompt context).
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo


# Month-of-year (1-12) → (peak_normal_f, low_normal_f) per station
_NORMALS: dict[str, dict[int, tuple[float, float]]] = {
    "KATL": {5: (79, 60), 6: (86, 67), 7: (88, 70), 8: (87, 70)},
    "KAUS": {5: (85, 65), 6: (91, 71), 7: (96, 73), 8: (97, 73)},
    "KBOS": {5: (67, 52), 6: (76, 61), 7: (82, 67), 8: (81, 66)},
    "KDCA": {5: (76, 57), 6: (84, 66), 7: (89, 71), 8: (87, 70)},
    "KDEN": {5: (71, 45), 6: (82, 54), 7: (89, 60), 8: (87, 58)},
    "KDFW": {5: (84, 65), 6: (92, 72), 7: (96, 76), 8: (97, 76)},
    "KHOU": {5: (87, 70), 6: (91, 74), 7: (93, 75), 8: (94, 75)},
    "KLAS": {5: (89, 65), 6: (99, 74), 7: (105, 81), 8: (103, 79)},
    "KLAX": {5: (70, 58), 6: (74, 61), 7: (77, 64), 8: (78, 65)},
    "KMDW": {5: (71, 51), 6: (81, 61), 7: (85, 67), 8: (83, 65)},
    "KMIA": {5: (86, 74), 6: (88, 77), 7: (90, 78), 8: (90, 79)},
    "KMSP": {5: (71, 52), 6: (81, 62), 7: (85, 67), 8: (82, 65)},
    "KMSY": {5: (86, 70), 6: (90, 75), 7: (92, 77), 8: (92, 77)},
    "KNYC": {5: (72, 57), 6: (80, 66), 7: (85, 72), 8: (84, 71)},
    "KOKC": {5: (79, 57), 6: (87, 67), 7: (94, 71), 8: (93, 70)},
    "KPHL": {5: (74, 55), 6: (83, 65), 7: (88, 70), 8: (86, 69)},
    "KPHX": {5: (96, 68), 6: (105, 77), 7: (107, 83), 8: (105, 82)},
    "KSAT": {5: (87, 68), 6: (92, 73), 7: (95, 74), 8: (96, 74)},
    "KSEA": {5: (67, 49), 6: (72, 53), 7: (78, 57), 8: (77, 57)},
    "KSFO": {5: (66, 52), 6: (69, 55), 7: (70, 56), 8: (71, 57)},
}

# Typical local-clock-time for daily peak and minimum. Most US stations
# follow a similar pattern with ~14:30 peak; desert stations shift a bit
# later (more solar heating to dissipate), coastal earlier.
_PEAK_HOUR_LOCAL: dict[str, int] = {
    "KPHX": 16, "KLAS": 16,
    "KSEA": 14, "KSFO": 13, "KLAX": 14,
    # everyone else: 15 local
}
_MIN_HOUR_LOCAL: dict[str, int] = {
    # Most stations have minimum near dawn, ~06:00 local in May.
}

_STATION_TZ: dict[str, str] = {
    "KATL": "America/New_York", "KAUS": "America/Chicago",
    "KBOS": "America/New_York", "KDCA": "America/New_York",
    "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles",
    "KLAX": "America/Los_Angeles", "KMDW": "America/Chicago",
    "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York",
    "KOKC": "America/Chicago", "KPHL": "America/New_York",
    "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}


def get_normals(station: str, month: int) -> Optional[tuple[float, float]]:
    """Return (normal_peak_f, normal_low_f) for the station/month, or None."""
    rec = _NORMALS.get(station, {}).get(month)
    return rec


def get_peak_min_hours(station: str) -> tuple[int, int]:
    """Return (typical_peak_hour_local, typical_min_hour_local). Defaults
    to (15, 6) when station not in the override tables."""
    return (
        _PEAK_HOUR_LOCAL.get(station, 15),
        _MIN_HOUR_LOCAL.get(station, 6),
    )


def local_clock_context(
    station: str, now_utc: float
) -> Optional[dict]:
    """For a station and current UTC ts, return a diurnal-context dict.

    Solar noon, sunrise, sunset are computed from lat/lon + date (real
    astronomical math, not hardcoded). Peak-temp hour = solar noon plus
    a station-class lag (continental ~2.5h, coastal ~1.5h, desert ~3h).
    Min hour = sunrise + small lag.
    """
    import station_meta
    import solar_calc
    meta = station_meta.get(station)
    tz_name = _STATION_TZ.get(station)
    if not meta or not tz_name:
        return None
    try:
        lag = station_meta.peak_lag_h(station)
        ctx = solar_calc.diurnal_context(
            lat=meta["lat"], lon=meta["lon"], tz_name=tz_name,
            now_utc_ts=now_utc, peak_lag_h=lag,
        )
        ctx["climate_class"] = meta.get("climate_class")
        ctx["station_lat"] = meta["lat"]
        ctx["station_lon"] = meta["lon"]
        ctx["station_elev_ft"] = meta.get("elev_ft")
        ctx["cli_report"] = meta.get("cli_report")
        ctx["peak_lag_h"] = lag
        return ctx
    except Exception as _e:
        return None
