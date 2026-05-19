"""nbp_reader.py — read NBP probabilistic forecast from V1 max + V1 min caches.

V1 max writes max-temp NBP to /home/ubuntu/data/nbp_cache_v1.json
V1 min  writes min-temp NBP to /home/ubuntu/paper_min_bot/data/nbp_cache.json

Schema (per cache):
  V1 max: cache[station][date] = {maxt, p10, p50, p90, sigma}
  V1 min: cache[station][date] = {mu, sigma}

For the judge bot, we want a unified lookup:
  get_nbp(station, climate_date, "high") -> {mu, sigma, p10, p50, p90, age_sec}
  get_nbp(station, climate_date, "low")  -> {mu, sigma, age_sec}
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("judge.nbp")


V1_MAX_NBP_PATH = Path("/home/ubuntu/data/nbp_cache_v1.json")
V1_MIN_NBP_PATH = Path("/home/ubuntu/paper_min_bot/data/nbp_cache.json")

_cache_max: tuple[float, dict] | None = None
_cache_min: tuple[float, dict] | None = None


def _load(path: Path, slot: str) -> dict:
    """Load + cache by mtime so we don't re-parse on every call."""
    global _cache_max, _cache_min
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    cached = _cache_max if slot == "max" else _cache_min
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with path.open() as f:
            data = json.load(f)
    except Exception as e:
        log.warning("nbp load %s failed: %s", path, e)
        return {}
    val = (mtime, data)
    if slot == "max":
        _cache_max = val
    else:
        _cache_min = val
    return data


def get_nbp(station: str, climate_date: str, kind: str) -> Optional[dict]:
    """Return NBP record for (station, date) on the LOW or HIGH side.

    kind: "high" → reads V1 max cache (maxt + percentiles)
          "low"  → reads V1 min cache (mu + sigma)

    Returns dict with:
      value_f       — the central forecast (maxt for high; mu for low)
      sigma         — predicted σ in °F
      p10/p50/p90   — percentiles (high only)
      age_sec       — staleness of the source file
      raw           — the original JSON record
    Returns None if no entry found.
    """
    if kind == "high":
        path = V1_MAX_NBP_PATH
        slot = "max"
        outer = _load(path, slot)
        cache = outer.get("cache") if isinstance(outer, dict) else outer
        rec = (cache or {}).get(station, {}).get(climate_date)
        if not rec:
            return None
        return {
            "value_f": rec.get("maxt") if rec.get("maxt") is not None else rec.get("p50"),
            "sigma": rec.get("sigma"),
            "p10": rec.get("p10"),
            "p50": rec.get("p50"),
            "p90": rec.get("p90"),
            "age_sec": time.time() - path.stat().st_mtime,
            "raw": rec,
        }
    elif kind == "low":
        path = V1_MIN_NBP_PATH
        slot = "min"
        outer = _load(path, slot)
        cache = outer.get("cache") if isinstance(outer, dict) else outer
        rec = (cache or {}).get(station, {}).get(climate_date)
        if not rec:
            return None
        return {
            "value_f": rec.get("mu"),
            "sigma": rec.get("sigma"),
            "age_sec": time.time() - path.stat().st_mtime,
            "raw": rec,
        }
    return None
