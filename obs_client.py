"""obs_client.py — live point-obs fetcher (NWS API) with caching + MADIS fallback.

The bot calls `get_obs(station)` once per cycle per candidate ticker. NWS
rate limits at ~5 req/sec; we cache per-station for 60s, and fall back to
MADIS via the shared obs-pipeline sqlite when NWS is down.

Cached values include temp, dewpt, sky text, wind, recent-30-min trend.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import config


log = logging.getLogger("judge.obs")


# ─────────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────────
CACHE_TTL_SEC = 60.0

_obs_cache: dict[str, tuple[float, "LiveObs"]] = {}  # station -> (ts, obs)


# ─────────────────────────────────────────────────────────────────────────────
# Types
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class LiveObs:
    station: str
    ts_iso: str
    temp_f: Optional[float]
    dewpt_f: Optional[float]
    sky: str
    wind_mph: Optional[float]
    source: str  # "nws" | "madis_fallback" | "stale"
    age_sec: float = 0.0
    # Trailing 30-min trend in °F (signed): positive = warming
    trend_30m_f: Optional[float] = None


# ─────────────────────────────────────────────────────────────────────────────
# NWS fetch
# ─────────────────────────────────────────────────────────────────────────────
NWS_BASE = "https://api.weather.gov"
NWS_USER_AGENT = "paper_judge_bot (chris@example.com)"


def _httpx():
    import httpx
    return httpx


def _fetch_nws_latest(station: str) -> Optional[LiveObs]:
    httpx = _httpx()
    try:
        r = httpx.get(
            f"{NWS_BASE}/stations/{station}/observations/latest",
            headers={"User-Agent": NWS_USER_AGENT, "Accept": "application/geo+json"},
            timeout=10.0,
        )
        r.raise_for_status()
        d = r.json()
        p = d.get("properties") or {}
        ts = p.get("timestamp", "")
        tc = (p.get("temperature") or {}).get("value")
        td = (p.get("dewpoint") or {}).get("value")
        sky = p.get("textDescription") or ""
        ws_obj = p.get("windSpeed") or {}
        wind = ws_obj.get("value")
        wind_unit = ws_obj.get("unitCode", "")
        temp_f = round(tc * 9 / 5 + 32, 1) if tc is not None else None
        dewpt_f = round(td * 9 / 5 + 32, 1) if td is not None else None
        if wind is None:
            wind_mph = None
        elif "km_h" in wind_unit:
            wind_mph = round(wind * 0.621371, 1)
        elif "m_s" in wind_unit:
            wind_mph = round(wind * 2.23694, 1)
        else:
            wind_mph = round(wind * 0.621371, 1)
        age_sec = 0.0
        if ts:
            try:
                ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_sec = (datetime.now(timezone.utc) - ts_dt).total_seconds()
            except Exception:
                pass
        return LiveObs(
            station=station,
            ts_iso=ts,
            temp_f=temp_f,
            dewpt_f=dewpt_f,
            sky=sky,
            wind_mph=wind_mph,
            source="nws",
            age_sec=age_sec,
        )
    except Exception as e:
        log.warning("NWS fetch %s failed: %s", station, e)
        return None


def _fetch_nws_history(station: str, hours: int = 1) -> list[dict]:
    """Fetch ~1h of observations to compute a trend."""
    httpx = _httpx()
    try:
        end = datetime.now(timezone.utc)
        start = end - __import__("datetime").timedelta(hours=hours)
        r = httpx.get(
            f"{NWS_BASE}/stations/{station}/observations",
            params={"start": start.isoformat(), "end": end.isoformat(), "limit": 20},
            headers={"User-Agent": NWS_USER_AGENT, "Accept": "application/geo+json"},
            timeout=10.0,
        )
        r.raise_for_status()
        return (r.json() or {}).get("features") or []
    except Exception as e:
        log.warning("NWS history %s failed: %s", station, e)
        return []


def _compute_trend(features: list[dict]) -> Optional[float]:
    """Return temp delta over the most-recent 30 minutes (°F). Positive = warming."""
    if len(features) < 2:
        return None
    pts: list[tuple[float, float]] = []
    for f in features:
        p = (f.get("properties") or {})
        tc = (p.get("temperature") or {}).get("value")
        ts = p.get("timestamp")
        if tc is None or not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            pts.append((dt.timestamp(), tc * 9 / 5 + 32))
        except Exception:
            continue
    if len(pts) < 2:
        return None
    pts.sort()
    last_ts, last_f = pts[-1]
    cutoff = last_ts - 30 * 60
    earlier = [(t, f) for t, f in pts if t <= cutoff]
    if not earlier:
        # fall back to oldest available
        earlier = [pts[0]]
    earlier_f = earlier[-1][1]
    return round(last_f - earlier_f, 2)


# ─────────────────────────────────────────────────────────────────────────────
# MADIS fallback via obs-pipeline sqlite
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_madis_fallback(station: str) -> Optional[LiveObs]:
    """Read the most recent observation row from obs-pipeline DB.

    Schema (same as other bots assume): observations(station, ts, temp_f,
    dewpt_f, ...). Read-only, short timeout, never blocks the cycle."""
    if not config.OBS_DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(
            f"file:{config.OBS_DB_PATH}?mode=ro", uri=True, timeout=2.0
        )
        row = conn.execute(
            """SELECT ts, temp_f, dewpt_f FROM observations
               WHERE station=? ORDER BY ts DESC LIMIT 1""",
            (station,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        ts, temp_f, dewpt_f = row
        return LiveObs(
            station=station,
            ts_iso=datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            temp_f=temp_f,
            dewpt_f=dewpt_f,
            sky="(madis fallback)",
            wind_mph=None,
            source="madis_fallback",
            age_sec=time.time() - ts,
        )
    except Exception as e:
        log.warning("MADIS fallback for %s failed: %s", station, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
def get_obs(station: str, *, allow_stale_sec: float = 600.0) -> Optional[LiveObs]:
    """Return latest obs for a station, cached for CACHE_TTL_SEC.

    If NWS fetch fails, falls back to MADIS via obs-pipeline sqlite.
    Returns None only if both sources are dead AND no cached value is
    fresher than `allow_stale_sec`.
    """
    now = time.time()
    cached = _obs_cache.get(station)
    if cached and now - cached[0] < CACHE_TTL_SEC:
        return cached[1]

    obs = _fetch_nws_latest(station)
    if obs is not None:
        # Compute trend opportunistically — failure is silent.
        try:
            history = _fetch_nws_history(station, hours=1)
            obs.trend_30m_f = _compute_trend(history)
        except Exception:
            pass
    else:
        obs = _fetch_madis_fallback(station)

    if obs is not None:
        _obs_cache[station] = (now, obs)
        return obs

    # Last resort: stale cache
    if cached and now - cached[0] < allow_stale_sec:
        stale = cached[1]
        stale.source = "stale"
        stale.age_sec = now - cached[0]
        return stale
    return None


# 2026-05-16 (Chris): get_running_min_max() REMOVED — obs-pipeline RM gone.
# Exit packets now read rm directly from wethr_rm. The other obs-pipeline DB
# uses in this file (_fetch_madis_fallback for raw observations) are kept
# because they're not RM-related.
