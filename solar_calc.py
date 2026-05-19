"""solar_calc.py — solar noon + sunrise + peak-temp lag for diurnal context.

The daily peak temperature LAGS solar noon by a station-dependent amount
(~1.5h coastal, 2-2.5h continental, 3h desert). The daily minimum is at
or just before sunrise. We compute both from lat/lon + date.

Approximations are accurate to ~10 minutes — fine for the prompt's
"are we past peak yet?" reasoning.

Formulas:
  - Equation of Time E(min) ≈ 9.87 sin(2B) − 7.53 cos(B) − 1.5 sin(B)
    where B = 2π·(N−81)/365 and N = day-of-year
  - Solar noon (UTC, hours) = 12 − E/60 − lon/15
  - Sunrise/sunset hour-angle H₀ = arccos(−tan(φ)·tan(δ))
    where δ = solar declination
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional


def _day_of_year(dt: datetime) -> int:
    return dt.timetuple().tm_yday


def _equation_of_time_min(dt: datetime) -> float:
    """Minutes the sun is "fast" relative to mean solar noon."""
    n = _day_of_year(dt)
    b = 2.0 * math.pi * (n - 81) / 365.0
    return 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)


def _solar_declination_deg(dt: datetime) -> float:
    """Solar declination in degrees, ~±23.45° over the year."""
    n = _day_of_year(dt)
    # Cooper's equation
    return 23.45 * math.sin(math.radians(360.0 * (284 + n) / 365.0))


def solar_noon_utc_h(lon: float, dt: datetime) -> float:
    """Hours of UTC at which solar noon occurs for this longitude + date."""
    e_min = _equation_of_time_min(dt)
    return 12.0 - e_min / 60.0 - lon / 15.0


def sunrise_sunset_utc_h(lat: float, lon: float, dt: datetime) -> tuple[float, float]:
    """(sunrise_h, sunset_h) in UTC hours. Returns (None,None)-shaped tuple
    of NaN if polar day/night."""
    delta = math.radians(_solar_declination_deg(dt))
    phi = math.radians(lat)
    cos_h0 = -math.tan(phi) * math.tan(delta)
    if cos_h0 > 1.0 or cos_h0 < -1.0:
        nan = float("nan")
        return (nan, nan)
    h0_h = math.degrees(math.acos(cos_h0)) / 15.0
    noon = solar_noon_utc_h(lon, dt)
    return (noon - h0_h, noon + h0_h)


def local_offset_h(tz_name: str, dt_utc: datetime) -> float:
    """UTC-offset in hours for a station tz at a given UTC dt (handles DST)."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    dt_local = dt_utc.astimezone(tz)
    off = dt_local.utcoffset()
    return off.total_seconds() / 3600.0 if off else 0.0


def diurnal_context(lat: float, lon: float, tz_name: str, now_utc_ts: float,
                    peak_lag_h: float = 2.5) -> dict:
    """Return all the clock numbers Claude needs for diurnal reasoning.

    `peak_lag_h` is station-specific (see station_meta.peak_lag_h).
    Min hour is approximated as sunrise + small lag (~0.25h).
    """
    dt_utc = datetime.fromtimestamp(now_utc_ts, tz=timezone.utc)
    off_h = local_offset_h(tz_name, dt_utc)
    # Solar noon in local time
    noon_utc = solar_noon_utc_h(lon, dt_utc)
    noon_local = (noon_utc + off_h) % 24
    # Sunrise / sunset
    sr_utc, ss_utc = sunrise_sunset_utc_h(lat, lon, dt_utc)
    sr_local = (sr_utc + off_h) % 24 if not math.isnan(sr_utc) else None
    ss_local = (ss_utc + off_h) % 24 if not math.isnan(ss_utc) else None
    # Peak local
    peak_local = (noon_local + peak_lag_h) % 24
    # Min local: just after sunrise (sun starts heating ~immediately)
    min_local = sr_local if sr_local is not None else 6.0
    # Current local hour
    dt_local = dt_utc.astimezone(__import__("zoneinfo").ZoneInfo(tz_name))
    cur_local_h = dt_local.hour + dt_local.minute / 60.0 + dt_local.second / 3600.0

    def _signed_h_to(target_local: float) -> float:
        """Signed hours from now to target local hour, within ±24h."""
        delta = target_local - cur_local_h
        # wrap to (-12, 12] so we get the "next" target (could be tomorrow's)
        if delta <= -12: delta += 24
        if delta > 12:   delta -= 24
        return delta

    return {
        "local_iso": dt_local.isoformat(),
        "local_hour": round(cur_local_h, 2),
        "local_dow": dt_local.strftime("%a"),
        "solar_noon_local_h": round(noon_local, 2),
        "sunrise_local_h": round(sr_local, 2) if sr_local is not None else None,
        "sunset_local_h": round(ss_local, 2) if ss_local is not None else None,
        "peak_hour_local": round(peak_local, 2),
        "min_hour_local": round(min_local, 2),
        "h_to_peak": round(_signed_h_to(peak_local), 2),
        "h_to_min": round(_signed_h_to(min_local), 2),
        "past_peak_today": _signed_h_to(peak_local) < 0,
        "past_min_today": _signed_h_to(min_local) < 0,
        "tz_offset_h": off_h,
    }
