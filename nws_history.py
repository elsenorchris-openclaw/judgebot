"""nws_history.py — hourly METAR observations for the climate day so far.

Used to give Claude the actual temperature trajectory: "obs at 06=58,
07=62, 08=67, 09=70" so it can judge whether the day is on pace, ahead,
or behind the forecast.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("judge.nws_history")

NWS_BASE = "https://api.weather.gov"
NWS_UA = "paper_judge_bot (chris@example.com)"

_cache: dict[str, tuple[float, list]] = {}  # (station, climate_day) → (ts, hourly_list)
_TTL = 600.0  # 10 min — METARs come in ~30-60 min; this is generous


def _httpx():
    import httpx
    return httpx


def _c_to_f(v) -> Optional[float]:
    if v is None: return None
    try: return round(float(v) * 9.0 / 5.0 + 32.0, 1)
    except (TypeError, ValueError): return None


def _ms_to_mph(v) -> Optional[float]:
    if v is None: return None
    try: return round(float(v) * 2.23694, 1)
    except (TypeError, ValueError): return None


def get_today_hourly(
    station: str, climate_day_start_utc_ts: float
) -> list[dict]:
    """Pull METAR observations from climate-day-start to now, aggregated to
    1-per-hour. Returns list of {hour_utc_iso, hour_offset_h, temp_f, dewpt_f,
    wind_mph, sky_short, precip_mm}.

    Each entry represents the LATEST METAR at-or-before that hour boundary.
    """
    key = f"{station}|{int(climate_day_start_utc_ts)}"
    hit = _cache.get(key)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]
    try:
        httpx = _httpx()
        start = datetime.fromtimestamp(climate_day_start_utc_ts, tz=timezone.utc)
        end = datetime.now(timezone.utc)
        r = httpx.get(
            f"{NWS_BASE}/stations/{station}/observations",
            params={"start": start.isoformat(), "end": end.isoformat(), "limit": 100},
            headers={"User-Agent": NWS_UA, "Accept": "application/geo+json"},
            timeout=15.0,
        )
        r.raise_for_status()
        feats = (r.json() or {}).get("features") or []
    except Exception as e:
        log.warning("nws_history fetch %s failed: %s", station, e)
        return []

    # Parse each observation
    records: list[tuple[float, dict]] = []
    for f in feats:
        p = (f.get("properties") or {})
        ts_iso = p.get("timestamp")
        if not ts_iso: continue
        try:
            ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if ts < climate_day_start_utc_ts: continue
        tc = (p.get("temperature") or {}).get("value")
        td = (p.get("dewpoint") or {}).get("value")
        wsp = (p.get("windSpeed") or {}).get("value")
        precip = (p.get("precipitationLastHour") or {}).get("value")
        sky = p.get("textDescription") or ""
        records.append((ts, {
            "ts_iso": ts_iso,
            "temp_f": _c_to_f(tc),
            "dewpt_f": _c_to_f(td),
            "wind_mph": _ms_to_mph(wsp),
            "precip_mm": precip,
            "sky_short": sky,
        }))

    if not records:
        _cache[key] = (time.time(), [])
        return []

    records.sort(key=lambda x: x[0])
    # Bucket to one entry per hour: use the LAST observation in each hour bucket
    by_hour: dict[int, tuple[float, dict]] = {}
    for ts, rec in records:
        hr = int(ts // 3600)
        by_hour[hr] = (ts, rec)

    out: list[dict] = []
    base_hr = int(climate_day_start_utc_ts // 3600)
    for hr_offset in range(0, 26):
        hr_key = base_hr + hr_offset
        if hr_key not in by_hour: continue
        ts, rec = by_hour[hr_key]
        rec["hour_offset_h"] = hr_offset
        rec["hour_utc_iso"] = datetime.fromtimestamp(
            hr_key * 3600, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:00Z")
        out.append(rec)

    _cache[key] = (time.time(), out)
    return out
