"""wethr_client.py — minimal wethr.net observations client.

Adapted from V2 min's wethr integration. We use the observations endpoint
ONLY (not forecasts/model_accuracy — we already have NBM/HRRR/ECMWF/NBP
from the shared cache). Wethr's obs adds:

  - cloud_layer_count (numeric, vs NWS's text description)
  - wind_speed + wind_gust (mph)
  - relative_humidity
  - pressure_tendency
  - heat_index_f
  - highest_probable / lowest_probable (statistical bounds)
  - suspect_temperature flag
  - precision metadata

Cycle-level prefetch (one batched call per cycle, ~20 stations × 1s
stagger = ~20s) — results cached for the entire cycle.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger("judge.wethr")

WETHR_BASE = "https://wethr.net/api/v2"
_STAGGER_SEC = 1.0

_client = None
_obs_cache: dict[str, tuple[float, dict]] = {}  # station -> (ts, obs)
_OBS_CACHE_TTL_SEC = 60.0
_mae_cache: dict[tuple, tuple[float, dict]] = {}  # (station, kind) -> (ts, model_mae)
_MAE_CACHE_TTL_SEC = 12 * 3600.0  # 12h — daily MAE rolls slowly


def _ensure_client():
    global _client
    if _client is not None:
        return _client
    api_key = os.environ.get("WETHR_API_KEY", "")
    if not api_key:
        log.warning("WETHR_API_KEY missing — wethr disabled")
        return None
    try:
        import httpx
        _client = httpx.Client(
            base_url=WETHR_BASE,
            timeout=10.0,
            headers={"X-API-Key": api_key, "User-Agent": "paper_judge_bot/1.0"},
        )
        log.info("wethr client initialized")
        return _client
    except Exception as e:
        log.warning("wethr client init failed: %s", e)
        return None


def fetch_one(station: str, allow_cache: bool = True) -> Optional[dict]:
    """Fetch the latest observation for one station from wethr.net.
    Returns parsed dict in °F units or None on failure.

    Cached per-station for 60s. `allow_cache=False` forces a fresh fetch
    (use sparingly — wethr rate-limits).

    Units in wethr's response:
      - temperature, dew_point, anomaly: °C (CONVERTED to °F here)
      - highest_probable, lowest_probable: °C (CONVERTED)
      - wind_speed, wind_gust: mph (used as-is)
      - relative_humidity: %
      - heat_index_f: already °F (when present, else absent)
      - cloud_layer_count: int 0+
      - precipitation_*: mm (left as-is; rarely used)

    Wethr's wind_gust is -999.0 when no gust reported — we map that to None.
    """
    if allow_cache:
        hit = _obs_cache.get(station)
        if hit and (time.time() - hit[0]) < _OBS_CACHE_TTL_SEC:
            return hit[1]
    c = _ensure_client()
    if c is None:
        return None
    try:
        r = c.get("/observations.php",
                  params={"station_code": station, "mode": "latest"})
        if r.status_code == 429:
            log.warning("wethr 429 rate-limited for %s", station)
            return None
        r.raise_for_status()
        d = r.json() or {}
        wg_raw = _maybe_f(d.get("wind_gust"))
        if wg_raw is not None and wg_raw < -100:
            wg_raw = None
        out = {
            "temp_f": _c_to_f(d.get("temperature")),
            "dew_point_f": _c_to_f(d.get("dew_point")),
            "anomaly_f": _c_to_f_delta(d.get("anomaly")),
            "highest_probable_f": _maybe_f(d.get("highest_probable_f")),
            "lowest_probable_f": _maybe_f(d.get("lowest_probable_f")),
            "relative_humidity": _maybe_f(d.get("relative_humidity")),
            "heat_index_f": _maybe_f(d.get("heat_index_f")),
            "wind_speed_mph": _maybe_f(d.get("wind_speed")),
            "wind_gust_mph": wg_raw,
            "cloud_layer_count": d.get("cloud_layer_count"),
            "cloud_1_coverage": d.get("cloud_1_coverage"),
            "cloud_1_height_ft": _maybe_f(d.get("cloud_1_height")),
            "pressure_tendency": d.get("pressure_tendency"),
            "precipitation_mm": _maybe_f(d.get("precipitation")),
            "precipitation_24hr_mm": _maybe_f(d.get("precipitation_24hr")),
            "visibility": _maybe_f(d.get("visibility")),
            "wind_direction": d.get("wind_direction"),
            "suspect_temperature": d.get("suspect_temperature"),
            "observation_time_utc": d.get("observation_time"),
            "precision_level": d.get("precision_level"),
            "data_source": d.get("data_source"),
            "ts_fetched": time.time(),
        }
        _obs_cache[station] = (time.time(), out)
        return out
    except Exception as e:
        log.warning("wethr fetch %s failed: %s", station, e)
        return None


def _c_to_f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        c = float(v)
        if c < -100:  # likely a sentinel
            return None
        return round(c * 9.0 / 5.0 + 32.0, 1)
    except (TypeError, ValueError):
        return None


def fetch_model_mae(station: str, kind: str = "high") -> dict:
    """Fetch per-model MAE + bias + ensemble from wethr model_accuracy.

    Endpoint matches V2 min's fetch:
      /model_accuracy.php?station_code=X&window=14d&extreme={low|high}

    Returns: {
      "per_model": {model_name: {mae, bias, rmse}, ...},
      "best": {model, mae, bias},
      "worst": {model, mae},
      "ensemble": {mae, bias}
    }
    Empty dict on failure.
    """
    key = (station, kind)
    hit = _mae_cache.get(key)
    if hit and (time.time() - hit[0]) < _MAE_CACHE_TTL_SEC:
        return hit[1]
    c = _ensure_client()
    if c is None:
        return {}
    try:
        r = c.get(
            "/model_accuracy.php",
            params={"station_code": station, "window": "14d", "extreme": kind},
        )
        r.raise_for_status()
        d = r.json() or {}
        per_model_raw = d.get("per_model") or {}
        if not isinstance(per_model_raw, dict) or not per_model_raw:
            _mae_cache[key] = (time.time(), {})
            return {}
        per_model = {
            m: {
                "mae": round(float(v.get("mae", 0) or 0), 2),
                "bias": round(float(v.get("bias", 0) or 0), 2),
                "rmse": round(float(v.get("rmse", 0) or 0), 2),
            }
            for m, v in per_model_raw.items()
            if isinstance(v, dict)
        }
        ranked = sorted(per_model.items(), key=lambda x: x[1]["mae"])
        out = {
            "per_model": per_model,
            "best": {"model": ranked[0][0], **ranked[0][1]} if ranked else None,
            "worst": {"model": ranked[-1][0], **ranked[-1][1]} if ranked else None,
            "ensemble": {
                "mae": round(float((d.get("ensemble") or {}).get("mae", 0) or 0), 2),
                "bias": round(float((d.get("ensemble") or {}).get("bias", 0) or 0), 2),
            },
        }
        _mae_cache[key] = (time.time(), out)
        return out
    except Exception as e:
        log.warning("wethr model_accuracy %s %s failed: %s", station, kind, e)
        return {}


def _c_to_f_delta(v) -> Optional[float]:
    """For deltas/anomalies (no +32 offset, just scale)."""
    if v is None or v == "":
        return None
    try:
        c = float(v)
        if c < -100:
            return None
        return round(c * 9.0 / 5.0, 1)
    except (TypeError, ValueError):
        return None


# 2026-05-16: fetch_all removed — no callers after live_data.prefetch's wethr
# batch removal (P1 fix) and obs_refresh wethr-branch removal. The bot reads
# wethr observations from /home/ubuntu/shared/wethr_cache.json directly.
# fetch_one() is retained as a debug-script entry point but is unused by the
# live bot.


def _maybe_f(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
