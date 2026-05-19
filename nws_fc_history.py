"""nws_fc_history.py — rolling NWS gridpoint hourly-forecast snapshots.

Each cycle records the (snapshot_ts, target_hour_iso, temp_f) tuples for a
station so the obs-vs-forecast pace_slope can regress past observations
against the forecast that was valid at the time those observations occurred.

The NWS gridpoint endpoint only returns FUTURE hourly forecasts (now → +N
hours). Without persistence, pace_slope cannot match past obs hours
against past forecast rows — which is why the live packet has
obs_vs_forecast_pace_slope = null in every recent cycle.

Layout on disk:
  data/nws_fc_history/<station>.jsonl
  one record per snapshot+target-hour pair:
    {"snapshot_ts": <epoch>, "target_iso": "YYYY-MM-DDTHH",
     "temp_f": <float>}
Records older than RETAIN_HOURS hours from now (default 48h) are pruned on
each record_snapshot call.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Iterable, Optional

log = logging.getLogger("judge.nws_fc_history")

# Default data dir mirrors other persistence (overridable for tests).
_DATA_DIR = os.environ.get(
    "JUDGE_NWS_FC_HISTORY_DIR",
    "/home/ubuntu/paper_judge_bot/data/nws_fc_history",
)
RETAIN_HOURS = 48


def _path_for(station: str, base_dir: Optional[str] = None) -> str:
    d = base_dir or _DATA_DIR
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{station}.jsonl")


def record_snapshot(
    station: str,
    hourly_fc: Iterable[dict],
    now_ts: Optional[float] = None,
    base_dir: Optional[str] = None,
) -> int:
    """Append (snapshot_ts, target_iso, temp_f) for every hourly forecast row.

    hourly_fc rows: {"utc_iso": "YYYY-MM-DDTHH:00Z", "temp_f": <float>, ...}
    Returns: number of rows appended. Prunes records older than RETAIN_HOURS.
    """
    if not station:
        return 0
    rows = []
    snap_ts = float(now_ts) if now_ts is not None else time.time()
    for r in hourly_fc or []:
        iso = (r or {}).get("utc_iso")
        t = (r or {}).get("temp_f")
        if not iso or t is None:
            continue
        # Normalize to YYYY-MM-DDTHH (13 chars), the same key used in
        # compute_obs_vs_forecast_pace_slope for cross-referencing.
        key = iso[:13]
        try:
            t_f = float(t)
        except (TypeError, ValueError):
            continue
        rows.append({"snapshot_ts": snap_ts, "target_iso": key, "temp_f": t_f})
    if not rows:
        return 0
    path = _path_for(station, base_dir)
    # Prune: read existing, drop rows with snapshot_ts < cutoff, rewrite.
    cutoff = snap_ts - RETAIN_HOURS * 3600.0
    existing: list[dict] = []
    if os.path.exists(path):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    sts = rec.get("snapshot_ts")
                    if isinstance(sts, (int, float)) and sts >= cutoff:
                        existing.append(rec)
        except OSError:
            existing = []
    out = existing + rows
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for rec in out:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    os.replace(tmp, path)
    return len(rows)


def get_fc_for_hour(
    station: str,
    target_hour_iso: str,
    before_ts: Optional[float] = None,
    base_dir: Optional[str] = None,
) -> Optional[float]:
    """Return the most recent forecast temp_f for `target_hour_iso`
    (truncated to YYYY-MM-DDTHH) from snapshots taken at or before
    `before_ts` (defaults to now).

    Returns None if no matching record exists.
    """
    if not station or not target_hour_iso:
        return None
    key = target_hour_iso[:13]
    path = _path_for(station, base_dir)
    if not os.path.exists(path):
        return None
    cutoff = float(before_ts) if before_ts is not None else time.time()
    best_ts = -1.0
    best_t: Optional[float] = None
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if rec.get("target_iso") != key:
                    continue
                sts = rec.get("snapshot_ts")
                t = rec.get("temp_f")
                if not isinstance(sts, (int, float)) or t is None:
                    continue
                if sts > cutoff:
                    continue
                if sts > best_ts:
                    best_ts = sts
                    try:
                        best_t = float(t)
                    except (TypeError, ValueError):
                        continue
    except OSError:
        return None
    return best_t
