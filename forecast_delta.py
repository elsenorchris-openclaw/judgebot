"""forecast_delta.py — run-over-run forecast deltas from shared cache.

Each shared cache file (`nbm.json`, `hrrr.json`) holds hourly forecast
entries with `run_iso` of the model run that produced them. By grouping
entries by run_iso and computing the climate-day extreme for each run,
we can see how the forecast has been revised over recent model cycles.

A 4°F downward revision of the HIGH over the last 4 runs is a signal
the model has new info — and the Kalshi market may lag the latest run.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from typing import Optional

import config

log = logging.getLogger("judge.fcst_delta")

_FILES = {
    "NBM": config.SHARED_CACHE_DIR / "nbm.json",
    "HRRR": config.SHARED_CACHE_DIR / "hrrr.json",
    "ECMWF-IFS": config.SHARED_CACHE_DIR / "ecmwf-ifs.json",
}


def _load(model: str) -> dict:
    p = _FILES.get(model)
    if not p or not p.exists():
        return {}
    try:
        with p.open() as f:
            return json.load(f)
    except Exception:
        return {}


def get_recent_runs(
    station: str, climate_day_start_ts: float, climate_day_end_ts: float,
    kind: str = "high", n_runs: int = 4
) -> dict:
    """For each model, return a list of (run_iso, extreme_f) for the last
    `n_runs` model runs that produced any forecast for this climate day.

    Result schema: {model: [{run_iso, extreme_f, run_age_h}, ...]}
    """
    out: dict[str, list] = {}
    for model in _FILES:
        data = _load(model)
        stations = (data or {}).get("stations") or {}
        rows = stations.get(station) or []
        # Group hourly entries by run_iso
        by_run: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            if not isinstance(r, list) or len(r) < 4: continue
            valid_iso, value, run_iso, _ = r[0], r[1], r[2], r[3]
            try:
                vt = datetime.strptime(valid_iso.strip(), "%Y-%m-%d %H:%M:%S").timestamp()
            except Exception:
                continue
            if not (climate_day_start_ts <= vt < climate_day_end_ts):
                continue
            try:
                by_run[run_iso].append(float(value))
            except (TypeError, ValueError):
                continue
        # For each run, extreme over climate day
        entries = []
        for run_iso, vals in by_run.items():
            if not vals: continue
            ext = max(vals) if kind == "high" else min(vals)
            try:
                run_ts = datetime.strptime(run_iso.strip(), "%Y-%m-%d %H:%M:%S").timestamp()
                run_age_h = (datetime.now().timestamp() - run_ts) / 3600.0
            except Exception:
                run_age_h = None
            entries.append({"run_iso": run_iso, "extreme_f": round(ext, 1),
                            "run_age_h": round(run_age_h, 1) if run_age_h else None})
        # Sort newest first, take n_runs
        entries.sort(key=lambda e: e.get("run_iso") or "", reverse=True)
        out[model] = entries[:n_runs]
    return out


def summary_delta(per_model_runs: dict, kind: str = "high") -> dict:
    """Reduce the per-model list to a summary delta string per model:
    "82.0 → 81.0 → 79.5 → 78.0 (Δ -4.0°F over 4 runs)" if downward trend."""
    out: dict[str, dict] = {}
    for model, entries in per_model_runs.items():
        if len(entries) < 2:
            out[model] = {"trend": "insufficient_runs", "values": [e.get("extreme_f") for e in entries]}
            continue
        vals = [e.get("extreme_f") for e in entries]
        # Entries are newest-first; reverse to show oldest → newest
        chrono = list(reversed(vals))
        delta = round(chrono[-1] - chrono[0], 1) if all(v is not None for v in chrono) else None
        direction = "stable"
        if delta is not None:
            if delta > 1.5: direction = "up"
            elif delta < -1.5: direction = "down"
        out[model] = {
            "chrono_values": chrono,
            "delta_f": delta,
            "trend": direction,
        }
    return out
