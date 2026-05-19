"""wethr_rm.py — thin client over the shared wethr-cache-service.

Reads /home/ubuntu/shared/wethr_cache.json (written by wethr-cache-service)
and exposes the same interface paper_judge_bot already uses:

  - get(station) → dict | None
  - is_degraded() → bool
  - degraded_seconds() → float
  - status() → dict
  - start() / stop() → no-op (cache is owned by separate service)
  - validate_rm_for_climate_day(...) → dict (added 2026-05-16, F1)

The shared cache file holds wethr_high running max/min PLUS latest observation
data (current temp, dewpt, wind, etc.) for all 20 stations. We use this as
the SOLE source for running_max — no fallback to obs-pipeline.

Audit 2026-05-14 (n=190 over 14 days):
  wethr_high MAE 0.13°F vs obs-pipeline 0.42°F. 3x more accurate.

2026-05-16 — F1 (stale-rm defense): added validate_rm_for_climate_day().
  Last night 5/7 rm-having LOW losers (-$19.46) had wethr cache pointing
  to YESTERDAY's climate day at decision time (bot entered pre-LDT-midnight
  treating the cached running-min as if it were today's anchor). Validator
  enforces: cache.date matches ticker.climate_day, time_of_extreme falls
  within the LDT window for that day, and now is past a configurable
  grace period after LDT midnight (default 3600s).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("judge.wethr_rm")

CACHE_FILE = Path("/home/ubuntu/shared/wethr_cache.json")

# Cache snapshot is considered fresh while updated_ts < this many seconds old.
# wethr-cache-service writes every 5s; 120s gives 24x safety margin.
CACHE_FRESH_SEC = 120.0

# Station → IANA timezone. DST-aware via ZoneInfo (Phoenix opts out year-round).
# Mirrors climate_normals._STATION_TZ and shared_cache_reader._STATION_TZ —
# duplicated here so this module has no circular import on those.
_STATION_TZ: dict[str, str] = {
    "KATL": "America/New_York",  "KAUS": "America/Chicago",
    "KBOS": "America/New_York",  "KDCA": "America/New_York",
    "KDEN": "America/Denver",    "KDFW": "America/Chicago",
    "KHOU": "America/Chicago",   "KLAS": "America/Los_Angeles",
    "KLAX": "America/Los_Angeles","KMDW":"America/Chicago",
    "KMIA": "America/New_York",  "KMSP": "America/Chicago",
    "KMSY": "America/Chicago",   "KNYC": "America/New_York",
    "KOKC": "America/Chicago",   "KPHL": "America/New_York",
    "KPHX": "America/Phoenix",   "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles","KSFO":"America/Los_Angeles",
}


def _load_cache() -> Optional[dict]:
    """Read + parse the shared cache file. Returns dict or None on any error."""
    try:
        if not CACHE_FILE.exists():
            return None
        with open(CACHE_FILE) as f:
            return json.load(f)
    except (IOError, ValueError) as e:
        log.debug("shared cache load failed: %s", e)
        return None


def get(station: str) -> Optional[dict]:
    """Return the wethr record for the station, or None if stale/missing.

    Returned dict matches the legacy interface paper_judge_bot expects:
      {high_f, low_f, time_of_high_utc, time_of_low_utc, date, ts, ...}
    plus extra current-obs fields if present (temp_f, dew_point_f, etc.).
    """
    d = _load_cache()
    if d is None:
        return None
    updated_ts = d.get("updated_ts") or 0
    if time.time() - updated_ts > CACHE_FRESH_SEC:
        return None  # whole cache file is stale
    rec = (d.get("stations") or {}).get(station)
    if not rec:
        return None
    if rec.get("high_f") is None and rec.get("low_f") is None:
        return None
    return rec


def is_degraded() -> bool:
    """True if shared cache is missing or stale > CACHE_FRESH_SEC."""
    d = _load_cache()
    if d is None:
        return True
    updated_ts = d.get("updated_ts") or 0
    return (time.time() - updated_ts) > CACHE_FRESH_SEC


def degraded_seconds() -> float:
    """Seconds since the shared cache was last updated. 0 if fresh/unknown."""
    d = _load_cache()
    if d is None:
        return float("inf")
    updated_ts = d.get("updated_ts") or 0
    age = time.time() - updated_ts
    return age if age > 0 else 0.0


def status() -> dict:
    """Diagnostic snapshot — useful for /health-style reporting."""
    d = _load_cache()
    if d is None:
        return {"cache_present": False, "stations": 0}
    stations = d.get("stations") or {}
    populated = sum(1 for v in stations.values() if v.get("high_f") is not None)
    return {
        "cache_present": True,
        "cache_age_sec": time.time() - (d.get("updated_ts") or 0),
        "service_started_ts": d.get("service_started_ts"),
        "stations_total": len(stations),
        "stations_populated": populated,
        "degraded": is_degraded(),
    }


def start() -> None:
    """No-op — cache is owned by wethr-cache-service (separate systemd unit)."""
    log.info("wethr_rm: using shared cache at %s (owned by wethr-cache.service)",
             CACHE_FILE)


def stop() -> None:
    """No-op."""
    pass


# Legacy polling helper kept for fallback compatibility (not used).
def fetch_one(station: str) -> Optional[dict]:
    return get(station)


# ─────────────────────────────────────────────────────────────────────────────
# F1: rm-staleness validator (2026-05-16)
# ─────────────────────────────────────────────────────────────────────────────
def lst_midnight_utc_ts(station: str, climate_day: str) -> Optional[float]:
    """Return UTC epoch seconds for LST midnight starting `climate_day` at
    `station`. Uses Local Standard Time (no DST) to match Kalshi/NWS CLI.
    Returns None on missing TZ or bad date.

    Per Kalshi docs (help.kalshi.com/en/articles/13823837-weather-markets)
    and NWS CLI rules: temperature markets settle on the NWS Climatological
    Report which uses **LST throughout the year**. During DST the daily
    window runs 1:00 AM local DST → 12:59 AM next-day local DST = midnight
    LST → midnight LST.

    obs-pipeline schema (`db.py:52,76`) also explicitly documents
    `climate_date in LST (no DST)`. This function matches both.

    Example: ATL "2026-05-15" → 2026-05-15 00:00 EST → 05:00 UTC.
             (NOT 04:00 UTC — that would be EDT/LDT midnight, which is
             yesterday's LST climate day at this exact moment.)
    PHX "2026-05-15" → 07:00 UTC year-round (AZ never observes DST).

    Implementation: Jan-15 trick — Jan 15 is always in standard time
    everywhere in CONUS, so utcoffset() at Jan-15 = LST offset for the year.
    """
    tz_name = _STATION_TZ.get(station)
    if not tz_name:
        return None
    try:
        tz = ZoneInfo(tz_name)
        d = datetime.strptime(climate_day, "%Y-%m-%d")
        lst_offset_h = datetime(d.year, 1, 15, tzinfo=tz).utcoffset().total_seconds() / 3600.0
        # Local midnight in LST → UTC: subtract negative offset
        # (e.g., EST=-5 → adds 5 hours, MST=-7 → adds 7 hours).
        start_utc = d.replace(tzinfo=timezone.utc) - timedelta(hours=lst_offset_h)
        return start_utc.timestamp()
    except (ValueError, KeyError) as e:
        log.debug("lst_midnight_utc_ts(%s, %s) failed: %s", station, climate_day, e)
        return None


# Backward-compat alias (deprecated). The function name "ldt_midnight" was
# a misnomer — the implementation predated the LST/Kalshi verification.
# Kept temporarily so any external test/script that imports the old name
# continues to work. Prefer `lst_midnight_utc_ts` in new code.
ldt_midnight_utc_ts = lst_midnight_utc_ts


def _parse_wethr_time_utc(s: Optional[str]) -> Optional[float]:
    """Parse 'YYYY-MM-DD HH:MM:SS' or ISO timestamp string as UTC. None on
    bad input.
    """
    if not s or not isinstance(s, str):
        return None
    try:
        # wethr format: '2026-05-15 11:10:00' (no TZ → treat as UTC)
        return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None


def validate_rm_for_climate_day(
    station: str,
    climate_day: str,
    cache_date: Optional[str],
    time_of_extreme_utc: Optional[str],
    now_utc_ts: float,
    grace_sec: float = 3600.0,
) -> dict:
    """Return {ok: bool, reason: str, lst_midnight_ts, secs_into_climate_day}.

    All checks must pass for ok=True:
      1. station has a known TZ mapping
      2. climate_day parses as YYYY-MM-DD
      3. cache_date string equals climate_day (wethr cache pointing at right day)
      4. now_utc_ts >= LST_midnight(climate_day) + grace_sec
      5. time_of_extreme_utc, if provided, falls within LST window
         [midnight, midnight+24h)

    All boundaries are **LST** (Local Standard Time, no DST) — per Kalshi
    docs and obs-pipeline schema. During DST months, LST midnight is 1 hour
    LATER than civil midnight (e.g., EDT 01:00 = EST 00:00 = LST midnight).

    Notes:
      - cache_date == None or != climate_day → rm is stale → ok=False
      - grace_sec defaults to 3600s (1h) to ensure post-midnight cooling has
        established before treating rm as predictive. Set to 0 to disable
        the grace gate while keeping date-match.
      - time_of_extreme_utc=None passes check 5 (cache hasn't observed a new
        extreme yet for that day). Check 3 (date match) still ensures the rm
        value (if any) corresponds to the right day.
      - Output field `lst_midnight_ts` is the authoritative LST midnight UTC.
        Legacy alias `ldt_midnight_ts` is provided for backward compat but
        carries the same value (LST) — new code should read `lst_midnight_ts`.
    """
    out = {
        "ok": False,
        "reason": "",
        "lst_midnight_ts": None,
        "ldt_midnight_ts": None,  # deprecated alias (same value as lst_midnight_ts)
        "secs_into_climate_day": None,
        "cache_date": cache_date,
        "expected_date": climate_day,
        "grace_sec": grace_sec,
    }

    # 1+2: tz + date parse via lst_midnight_utc_ts (LST, matches Kalshi)
    midnight_ts = lst_midnight_utc_ts(station, climate_day)
    if midnight_ts is None:
        out["reason"] = f"no_tz_or_bad_date:station={station}:climate_day={climate_day}"
        return out
    out["lst_midnight_ts"] = midnight_ts
    out["ldt_midnight_ts"] = midnight_ts  # alias for back-compat
    out["secs_into_climate_day"] = now_utc_ts - midnight_ts

    # 3: cache date string match
    if cache_date != climate_day:
        out["reason"] = (
            f"stale_cache_date:cache={cache_date!r}_vs_climate_day={climate_day!r}"
        )
        return out

    # 4: grace period
    secs_into_cd = now_utc_ts - midnight_ts
    if secs_into_cd < grace_sec:
        out["reason"] = (
            f"within_grace:secs_into_cd={secs_into_cd:.0f}<grace={grace_sec:.0f}"
        )
        return out

    # 5: time_of_extreme_utc within LST window
    if time_of_extreme_utc is not None:
        tx_ts = _parse_wethr_time_utc(time_of_extreme_utc)
        if tx_ts is None:
            out["reason"] = f"unparseable_time_of_extreme:{time_of_extreme_utc!r}"
            return out
        if not (midnight_ts <= tx_ts < midnight_ts + 24 * 3600):
            out["reason"] = (
                f"time_of_extreme_outside_window:{time_of_extreme_utc}"
                f"_vs_window=[{midnight_ts:.0f},{midnight_ts+86400:.0f})"
            )
            return out

    out["ok"] = True
    out["reason"] = "ok"
    return out
