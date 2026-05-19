"""pres_history.py — rolling altimeter snapshots for nn_match pres trajectory.

The trace DB's `pres1_trace` is station pressure (inHg, ×100). Live wethr
serves `altimeter` (inHg, sea-level corrected). We snapshot altimeter every
cycle; the consumer (nn_shadow.py) converts altimeter → station_pres using
station_meta.elev_ft, then builds a trajectory for nn_match_fast.predict().

Layout:
  data/pres_history/<station>.jsonl
  one record per cycle:
    {"ts": <epoch_utc>, "alt_inhg": <float>}

Records older than RETAIN_HOURS are pruned on every record_snapshot call.

2026-05-18: created for pres1_trajectory matching ship (backtest -0.04°F LOW
MAE on top of relh+k50 production baseline, seed=1 held-out n=11k).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger("judge.pres_history")

_DATA_DIR = os.environ.get(
    "JUDGE_PRES_HISTORY_DIR",
    "/home/ubuntu/paper_judge_bot/data/pres_history",
)
RETAIN_HOURS = 6  # 3h needed for full nn_match window, 2x slack


def _path_for(station: str, base_dir: Optional[str] = None) -> str:
    d = base_dir or _DATA_DIR
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{station}.jsonl")


def record_snapshot(
    station: str,
    altimeter_inhg: Optional[float],
    now_ts: Optional[float] = None,
    base_dir: Optional[str] = None,
) -> bool:
    """Append one (ts, alt_inhg) row. Prune rows older than RETAIN_HOURS.

    Returns True if a row was appended, False if input was missing/invalid.
    """
    if not station or altimeter_inhg is None:
        return False
    try:
        alt = float(altimeter_inhg)
    except (TypeError, ValueError):
        return False
    if not (25.0 <= alt <= 32.0):  # sanity: inHg range
        return False
    snap_ts = float(now_ts) if now_ts is not None else time.time()
    path = _path_for(station, base_dir)
    cutoff = snap_ts - RETAIN_HOURS * 3600.0
    existing: list[dict] = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    ts = r.get("ts")
                    if ts is not None and ts >= cutoff:
                        existing.append(r)
        except OSError:
            existing = []
    existing.append({"ts": snap_ts, "alt_inhg": alt})
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            for r in existing:
                f.write(json.dumps(r) + "\n")
        os.replace(tmp, path)
    except OSError:
        log.exception("pres_history: write failed for %s", station)
        return False
    return True


def get_history(
    station: str,
    lookback_sec: float = 3 * 3600.0,
    base_dir: Optional[str] = None,
) -> list[dict]:
    """Return recent [{ts, alt_inhg}, ...] ascending by ts. Empty list if none."""
    path = _path_for(station, base_dir)
    if not os.path.exists(path):
        return []
    cutoff = time.time() - float(lookback_sec)
    out: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                ts = r.get("ts")
                alt = r.get("alt_inhg")
                if ts is None or alt is None:
                    continue
                if ts >= cutoff:
                    out.append({"ts": float(ts), "alt_inhg": float(alt)})
    except OSError:
        return []
    out.sort(key=lambda r: r["ts"])
    return out
