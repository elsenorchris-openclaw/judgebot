"""shared_cache_reader.py — read NBM/HRRR/ECMWF-IFS from /home/ubuntu/shared_cache.

Mirrors the reader the paper_min_bot/paper_min_bot_v2/V1/V2 bots use. Single
source of truth: the kalshi-s3-cache.service writes these files atomically
every 30s; readers see either old or new, never partial.

Schema produced by the writer:
  {
    "model": "NBM" | "HRRR" | "ECMWF-IFS",
    "ts": <unix ts of last write>,
    "last_ingested_run": "...",
    "f_hour_range": [start, end],
    "stations": {
        "<KICAO>": [
            [valid_iso_utc, value_f, run_iso_utc, ingest_iso_utc],
            ...
        ],
        ...
    }
  }

Each station's list is an HOURLY forecast stream. To get the daily extreme
for a climate-day window we filter by valid_ts and aggregate.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import config


log = logging.getLogger("judge.shared_cache")


_FILES = {
    "NBM": config.SHARED_CACHE_DIR / "nbm.json",
    "HRRR": config.SHARED_CACHE_DIR / "hrrr.json",
    "ECMWF-IFS": config.SHARED_CACHE_DIR / "ecmwf-ifs.json",
}

# Per-station LST timezone (no DST). Mirrors paper_judge_bot.STATION_TZ.
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


# 2026-05-16: mtime-keyed caches are read/written from multiple threads
# (entry-loop workers + obs_refresh + main loop). CPython GIL makes individual
# dict assignment atomic but a torn `(mtime, dict)` tuple read between the
# check and the return is theoretically possible. Locks add ~µs cost on a
# cache hit, negligible compared to the file read on a miss.
import threading as _threading
_parsed_cache: dict[str, tuple[float, dict]] = {}
_parsed_cache_lock = _threading.Lock()


def _load(model: str) -> dict:
    path = _FILES[model]
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    with _parsed_cache_lock:
        cached = _parsed_cache.get(model)
        if cached and cached[0] == mtime:
            return cached[1]
    try:
        with path.open() as f:
            data = json.load(f)
        with _parsed_cache_lock:
            _parsed_cache[model] = (mtime, data)
        return data
    except Exception as e:
        log.warning("shared_cache load %s failed: %s", model, e)
        return {}


def _climate_day_window_utc(station: str, climate_date: str) -> Optional[tuple[float, float]]:
    """LST midnight-to-midnight window in UTC seconds.

    Mirrors paper_min_bot._lst_climate_window_utc. Returns (start_ts, end_ts)
    or None if the station/date is malformed."""
    tz_name = _STATION_TZ.get(station)
    if not tz_name:
        return None
    try:
        tz = ZoneInfo(tz_name)
        y = int(climate_date.split("-")[0])
        # Use Jan 15 of the same year to get the LST offset (no DST).
        lst_offset_h = datetime(y, 1, 15, tzinfo=tz).utcoffset().total_seconds() / 3600.0
        d = datetime.strptime(climate_date, "%Y-%m-%d")
        start_utc = d.replace(tzinfo=timezone.utc) + timedelta(hours=-lst_offset_h)
        end_utc = start_utc + timedelta(hours=24)
        return (start_utc.timestamp(), end_utc.timestamp())
    except Exception:
        return None


def _parse_iso_loose(s: str) -> Optional[float]:
    """The writer stores valid_ts as 'YYYY-MM-DD HH:MM:SS' (no timezone).
    Treat as UTC. Returns unix ts or None on bad parse."""
    if not s:
        return None
    try:
        dt = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        try:
            # ISO fallback
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except Exception:
            return None


def get_forecast(
    station: str, climate_date: str, kind: str = "low"
) -> dict[str, dict]:
    """Return per-model summary for the climate-day extreme.

    kind ∈ {"low","high"}. Aggregates hourly forecasts within the LST
    climate-day window via min() (low) or max() (high).

    Output dict keyed by model name → {value_f, valid_ts_iso, age_sec, run_iso, raw_n}.
    """
    out: dict[str, dict] = {}
    window = _climate_day_window_utc(station, climate_date)
    if window is None:
        return out
    start_ts, end_ts = window
    now = time.time()

    for model in _FILES:
        data = _load(model)
        stations = (data or {}).get("stations") or {}
        rows = stations.get(station) or []
        in_window: list[tuple[float, float, str, str]] = []
        for r in rows:
            if not isinstance(r, list) or len(r) < 2:
                continue
            valid_str, value = r[0], r[1]
            run_str = r[2] if len(r) > 2 else ""
            ingest_str = r[3] if len(r) > 3 else ""
            vt = _parse_iso_loose(valid_str)
            if vt is None:
                continue
            if not (start_ts <= vt < end_ts):
                continue
            try:
                vf = float(value)
            except (TypeError, ValueError):
                continue
            in_window.append((vt, vf, run_str, ingest_str))
        if not in_window:
            continue
        if kind == "low":
            chosen = min(in_window, key=lambda x: x[1])
        else:
            chosen = max(in_window, key=lambda x: x[1])
        valid_ts, value_f, run_str, ingest_str = chosen
        # age = how stale is this forecast (vs the most recent ingest in our row set)
        latest_ingest = max(
            (_parse_iso_loose(r[3]) or 0) for r in rows if len(r) > 3
        ) if rows else 0
        age = (now - latest_ingest) if latest_ingest else None
        out[model] = {
            "value_f": value_f,
            "valid_ts_iso": datetime.fromtimestamp(valid_ts, tz=timezone.utc).isoformat(),
            "run_iso": run_str,
            "age_sec": age,
            "raw_n": len(in_window),
        }
    return out


def summarize_disagreement(forecasts: dict[str, dict]) -> Optional[float]:
    """Max pairwise diff across forecast values (°F). Returns None if <2 sources."""
    values = [v["value_f"] for v in forecasts.values() if "value_f" in v]
    if len(values) < 2:
        return None
    return max(values) - min(values)


# ─── Wethr cache readers (temp history, trend, hourly history) ────────────────
# 2026-05-15: paper_judge_bot wethr-only policy. The wethr-cache-service writes
# /home/ubuntu/shared/wethr_cache.json every 5s, including per-station
# temp_history (last 60 min) and hourly_history (last 30 hours, one entry per
# UTC hour). These readers expose them in the shapes the bot needs.

from pathlib import Path

_WETHR_CACHE_PATH = Path("/home/ubuntu/shared/wethr_cache.json")
_wethr_parsed: tuple[float, dict] | None = None  # (mtime, snapshot)
_wethr_parsed_lock = _threading.Lock()  # see _parsed_cache_lock comment above


def _load_wethr_cache() -> dict:
    global _wethr_parsed
    if not _WETHR_CACHE_PATH.exists():
        return {}
    try:
        mtime = _WETHR_CACHE_PATH.stat().st_mtime
    except OSError:
        return {}
    with _wethr_parsed_lock:
        if _wethr_parsed and _wethr_parsed[0] == mtime:
            return _wethr_parsed[1]
    try:
        with _WETHR_CACHE_PATH.open() as f:
            data = json.load(f)
        with _wethr_parsed_lock:
            _wethr_parsed = (mtime, data)
        return data
    except Exception as e:
        log.warning("wethr cache load failed: %s", e)
        return {}


def _wethr_station_entry(station: str) -> dict:
    snap = _load_wethr_cache()
    return ((snap.get("stations") or {}).get(station) or {})


def get_temp_history(station: str, lookback_sec: float = 3600.0) -> list[dict]:
    """Return temp_history entries for the station within lookback_sec of now.
    Each entry: {"ts": float, "temp_f": float}. Sorted oldest→newest.
    Returns [] if cache missing/empty or no history yet."""
    entry = _wethr_station_entry(station)
    hist = entry.get("temp_history") or []
    if not hist:
        return []
    cutoff = time.time() - lookback_sec
    return sorted((h for h in hist if h.get("ts", 0) >= cutoff),
                  key=lambda h: h["ts"])


# 2026-05-16 (Chris): obs-pipeline RM was removed per "wethr-only policy".
# The fallback that combined obs-pipeline base + wethr live extender is gone.
# Now: rm comes solely from wethr's high_f/low_f. When wethr cache is stale
# (date != climate_day) the validator returns ok=False → rm_val=None →
# prescreen "no rm anchor → SKIP". Pre-dawn LOW d+0 candidates that previously
# got an obs-pipeline seed will now SKIP. pace_low_band / tail_low_band in
# the packet already let the LLM self-skip those cases.
#
# Removed:
#   - _OBS_PIPELINE_DB constant
#   - get_obs_pipeline_running_extreme()
#   - _STATION_TZ_FOR_LST + lst_midnight_utc_ts() (duplicate of wethr_rm.py)
#   - running_extreme_with_obs_pipeline_base()


def get_rm_age_sec(station: str, kind: str,
                   now_ts: Optional[float] = None) -> Optional[float]:
    """Seconds elapsed since wethr last set the running extreme for (station, kind).
    kind ∈ {'min','max'}. Returns None if cache has no `time_of_{high,low}_utc`
    or the field can't be parsed. Used to surface 'rm staleness' to the LLM.
    """
    if kind not in ("min", "max"):
        return None
    entry = _wethr_station_entry(station)
    key = "time_of_high_utc" if kind == "max" else "time_of_low_utc"
    s = entry.get(key)
    if not s:
        return None
    try:
        from datetime import datetime as _dt, timezone as _tz
        t = _dt.fromisoformat(str(s).replace(" ", "T")).replace(tzinfo=_tz.utc)
        return (now_ts if now_ts is not None else time.time()) - t.timestamp()
    except (ValueError, AttributeError):
        return None


def temp_history_range_60m(station: str,
                            now_ts: Optional[float] = None) -> Optional[dict]:
    """Range (max − min) of wethr temp_history readings in the last 60 minutes.
    Returns {range_f, n, span_min} or None when fewer than 2 points are
    available. Used as a volatility indicator alongside the 60m regression
    trend — high range + low r² = volatile/disrupted regime.
    """
    entry = _wethr_station_entry(station)
    th = entry.get("temp_history") or []
    now = now_ts if now_ts is not None else time.time()
    cutoff = now - 3600.0
    pts: list[tuple[float, float]] = []
    for h in th:
        t = h.get("ts"); v = h.get("temp_f")
        if t is None or v is None:
            continue
        if t >= cutoff:
            pts.append((float(t), float(v)))
    if len(pts) < 2:
        return None
    temps = [v for _, v in pts]
    ts = [t for t, _ in pts]
    return {
        "range_f": round(max(temps) - min(temps), 2),
        "n": len(pts),
        "span_min": round((max(ts) - min(ts)) / 60.0, 1),
    }


def compute_trend_30m(station: str) -> Optional[float]:
    """30-minute temperature trend in °F. Mirrors obs_client._compute_trend
    semantics: latest temp minus the reading at-or-just-before 30 min ago.
    Returns None if <2 history points or temp values missing."""
    pts = get_temp_history(station, lookback_sec=3600.0)
    pts = [(p["ts"], p["temp_f"]) for p in pts
           if p.get("temp_f") is not None and p.get("ts") is not None]
    if len(pts) < 2:
        return None
    pts.sort()
    last_ts, last_f = pts[-1]
    cutoff = last_ts - 30 * 60
    earlier = [(t, f) for t, f in pts if t <= cutoff]
    if not earlier:
        earlier = [pts[0]]  # fall back to oldest available
    earlier_f = earlier[-1][1]
    return round(last_f - earlier_f, 2)


def compute_trend_60m_regression(station: str) -> Optional[dict]:
    """60-minute temperature trend via linear regression of all temp_history
    points in the window. Returns slope in °F/hour + r² fit quality + meta.

    Why: the single-snapshot `compute_trend_30m` is vulnerable to noise — a
    single anomalous reading can dominate the signal (PHX 2026-05-15 flipped
    +1.8°F/30m → −1.8°F/30m in 19 min on the same weather). A regression
    across the full 60-min window is more stable, and r² tells the caller
    whether the trend is *coherent* (line fits well) vs. noisy/non-linear.

    Returns: {slope_f_per_h, r_squared, n_points, span_min} or None if <3 pts.
    """
    pts = get_temp_history(station, lookback_sec=3600.0)
    pts = [(float(p["ts"]), float(p["temp_f"])) for p in pts
           if p.get("temp_f") is not None and p.get("ts") is not None]
    if len(pts) < 3:
        return None
    pts.sort()
    # Normalize x to hours from the earliest point so slope is °F/hour.
    t0 = pts[0][0]
    xs = [(t - t0) / 3600.0 for t, _ in pts]
    ys = [y for _, y in pts]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = sum((x - mean_x) ** 2 for x in xs)
    if den_x <= 0:
        return None  # all points at the same time — can't regress
    slope = num / den_x
    intercept = mean_y - slope * mean_x
    # r² = 1 - SS_res / SS_tot
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    if ss_tot <= 0:
        # All y identical → constant temp → slope=0 by definition, perfect fit
        r2 = 1.0
    else:
        ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
        r2 = max(0.0, 1.0 - ss_res / ss_tot)
    span_min = (pts[-1][0] - pts[0][0]) / 60.0
    return {
        "slope_f_per_h": round(slope, 3),
        "r_squared": round(r2, 3),
        "n_points": n,
        "span_min": round(span_min, 1),
    }


def _iso_hour_end_ts(iso_str: str) -> float:
    """Return epoch ts for the END of the UTC hour in iso_str.

    iso_str looks like "2026-05-16T17:00Z" or "2026-05-16T17:00:00+00:00".
    Returns the timestamp for the boundary of that hour (HH:59:59.999 + 1μs
    ≈ next-hour start), which is the correct "snapshot cutoff" when
    matching past obs hours to forecasts that were valid AT or before that
    hour."""
    if not iso_str:
        return 0.0
    s = iso_str.strip()
    # Normalize trailing Z → +00:00 for fromisoformat.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # If only YYYY-MM-DDTHH supplied, pad to ISO.
    if len(s) == 13 and "T" in s:
        s = s + ":00:00+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return 0.0
    # End of the hour = start + 3600.
    return dt.timestamp() + 3600.0


def compute_obs_vs_forecast_pace_slope(
    hourly_obs: list[dict],
    hourly_forecast: list[dict],
    lookback_hours: int = 5,
    fc_lookup_fn=None,
) -> tuple[Optional[dict], Optional[str]]:
    """Linear regression of (obs - forecast) gap across the last
    `lookback_hours` hours of observed data. Tells the caller whether the
    forecast bias is WIDENING (slope > 0: obs running increasingly hotter
    than forecast → real peak will be higher) or NARROWING (slope < 0:
    obs catching down to forecast → forecast was right all along).

    Why: prompt's Step 5 currently uses a single-hour delta which is noisy.
    A slope across 3–5 hours is much more diagnostic of forecast-bust-in-
    progress vs transient hour-to-hour wobble.

    Args:
      hourly_obs: from get_hourly_obs_window (preferred) or get_hourly_obs_today
                  — list of {hour_offset_h, temp_f}
      hourly_forecast: NWS hourly forecast — list of {hour_offset, temp_f}
      lookback_hours: how many recent matched hours to regress on

    Returns (F3 2026-05-17): a tuple `(result, unavailable_reason)`.
      - On success: ({slope_per_h, current_gap_f, mean_gap_f, n_hours}, None)
      - On failure: (None, "<reason>") where reason is one of:
          "no_obs"            — hourly_obs was empty
          "no_fc_sources"     — neither live forecast nor snapshot lookup
          "all_pairs_unmatched" — obs hours all failed to match a forecast
          "insufficient_pairs"  — <2 obs/forecast pairs (lookback too tight)
          "degenerate_x"      — all matched pairs at the same hour_offset
    """
    if not hourly_obs:
        return None, "no_obs"
    # Index live (future-only) forecast by hour for the fallback path. The
    # NWS gridpoint endpoint only returns hours from now into the future,
    # so past obs hours never match against it — which is why pace_slope
    # was 100% null in production. Rolling snapshots via fc_lookup_fn fix
    # this; the in-memory fc_by_iso below covers the current-hour fallback.
    fc_by_iso: dict[str, float] = {}
    for r in (hourly_forecast or []):
        iso = r.get("utc_iso")
        temp = r.get("temp_f")
        if iso and temp is not None:
            try:
                fc_by_iso[iso[:13]] = float(temp)  # match by YYYY-MM-DDTHH
            except (TypeError, ValueError):
                pass
    if not fc_by_iso and fc_lookup_fn is None:
        return None, "no_fc_sources"
    pairs: list[tuple[int, float]] = []  # (hour_offset_h, gap_f)
    for r in hourly_obs:
        ho = r.get("hour_offset_h")
        obs_t = r.get("temp_f")
        obs_iso = r.get("hour_utc_iso")
        if ho is None or obs_t is None or obs_iso is None:
            continue
        key = obs_iso[:13]
        fc_t = None
        # Prefer rolling-snapshot lookup: gives us the forecast that was
        # VALID AT the past obs hour, not the current (future-only) forecast.
        if fc_lookup_fn is not None:
            try:
                cutoff_ts = _iso_hour_end_ts(obs_iso)
                fc_t = fc_lookup_fn(key, cutoff_ts)
            except Exception:  # never let snapshot lookup break the math
                fc_t = None
        if fc_t is None:
            fc_t = fc_by_iso.get(key)
        if fc_t is None:
            continue
        try:
            pairs.append((int(ho), float(obs_t) - float(fc_t)))
        except (TypeError, ValueError):
            continue
    if not pairs:
        return None, "all_pairs_unmatched"
    # F2 2026-05-17: min pairs lowered 3 → 2. Two-point regression is
    # mathematically well-defined (den_x > 0 for distinct hour offsets).
    # Slope is noisier with n=2 but `n_hours` is returned so the LLM
    # downweights — and getting *some* signal beats null.
    if len(pairs) < 2:
        return None, "insufficient_pairs"
    pairs.sort(key=lambda p: p[0])
    pairs = pairs[-lookback_hours:]
    if len(pairs) < 2:
        return None, "insufficient_pairs"
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = sum((x - mean_x) ** 2 for x in xs)
    if den_x <= 0:
        return None, "degenerate_x"
    slope = num / den_x
    return {
        "slope_per_h": round(slope, 3),
        "current_gap_f": round(ys[-1], 2),
        "mean_gap_f": round(mean_y, 2),
        "n_hours": n,
    }, None


def _cloud_to_sky_short(clc) -> str:
    """Map wethr cloud_layer_count (0..N) → NWS-style sky_short label for
    prompt parity with the prior nws_history.get_today_hourly output."""
    if clc is None:
        return ""
    try:
        n = int(clc)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return "Clear"
    if n == 1:
        return "Few clouds"
    if n == 2:
        return "Scattered"
    return "Overcast"


def get_hourly_obs_today(
    station: str, climate_day_start_utc_ts: float
) -> list[dict]:
    """Return hourly observations for the station from climate-day-start through
    now, one entry per UTC hour. Drop-in replacement for
    nws_history.get_today_hourly — same output shape:
        [{hour_offset_h, hour_utc_iso, temp_f, dewpt_f, wind_mph, sky_short,
          precip_mm}, ...]
    sky_short is derived from cloud_layer_count via _cloud_to_sky_short.
    precip_mm is not retained in the wethr cache today — emitted as None."""
    entry = _wethr_station_entry(station)
    hist = entry.get("hourly_history") or []
    if not hist:
        return []
    base_hr = int(climate_day_start_utc_ts // 3600)
    out: list[dict] = []
    for h in sorted(hist, key=lambda x: x.get("hour_ts", 0)):
        hour_ts = h.get("hour_ts")
        if hour_ts is None or hour_ts < climate_day_start_utc_ts:
            continue
        hour_offset = int(hour_ts // 3600) - base_hr
        out.append({
            "hour_offset_h": hour_offset,
            "hour_utc_iso": h.get("hour_iso") or datetime.fromtimestamp(
                hour_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:00Z"),
            "temp_f": h.get("temp_f"),
            "dewpt_f": h.get("dew_point_f"),
            "wind_mph": h.get("wind_speed_mph"),
            "sky_short": _cloud_to_sky_short(h.get("cloud_layer_count")),
            "precip_mm": None,
        })
    return out


def get_hourly_obs_window(
    station: str, start_ts: float, end_ts: float
) -> list[dict]:
    """Same shape as get_hourly_obs_today but with an EXPLICIT wall-clock
    window, not tied to the candidate's climate day.

    2026-05-17 (F1): obs_vs_forecast_pace_slope was 96% null because
    get_hourly_obs_today is bound to the current LST climate day — for
    pre-LST-midnight + early-morning candidates the climate day has 0-2
    hours of obs and the slope regression fails. A 12h rolling wall-clock
    window has ≥10 hours of obs at any time of day, so post-fix the
    pace_slope availability jumps from ~5% to ~70% (validated by replay
    backtest on last 18h of null-pace candidates).

    Snapshots in nws_fc_history have 48h retention, so the matching
    forecast lookup works across the climate-day boundary in this window.
    """
    entry = _wethr_station_entry(station)
    hist = entry.get("hourly_history") or []
    if not hist:
        return []
    base_hr = int(start_ts // 3600)
    out: list[dict] = []
    for h in sorted(hist, key=lambda x: x.get("hour_ts", 0)):
        hour_ts = h.get("hour_ts")
        if hour_ts is None or not (start_ts <= hour_ts <= end_ts):
            continue
        hour_offset = int(hour_ts // 3600) - base_hr
        out.append({
            "hour_offset_h": hour_offset,
            "hour_utc_iso": h.get("hour_iso") or datetime.fromtimestamp(
                hour_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:00Z"),
            "temp_f": h.get("temp_f"),
            "dewpt_f": h.get("dew_point_f"),
            "wind_mph": h.get("wind_speed_mph"),
            "sky_short": _cloud_to_sky_short(h.get("cloud_layer_count")),
            "precip_mm": None,
        })
    return out


def get_wethr_obs(station: str) -> Optional[dict]:
    """Return current wethr obs for the station from the shared cache, augmented
    with computed age_sec (since wethr_cache.json stores obs_ts but not age).
    Returns None if cache missing or station has no temp_f."""
    entry = _wethr_station_entry(station)
    if not entry or entry.get("temp_f") is None:
        return None
    out = dict(entry)
    ts = out.get("obs_ts") or out.get("ts")
    if ts is not None:
        try:
            out["age_sec"] = max(0.0, time.time() - float(ts))
        except (TypeError, ValueError):
            out["age_sec"] = None
    else:
        out["age_sec"] = None
    return out
