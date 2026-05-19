#!/usr/bin/env python3
"""nn_match_fast.py — numpy-optimized version of nn_match.

Drop-in replacement with the same API. Key optimizations:

  1. **In-memory candidate pool cache.** First call for a (station, month_set,
     sunrise_window) loads all matching candidates into a numpy ndarray; later
     calls re-use the loaded arrays.
  2. **Vectorized L2 distance.** L2 over the trace window is one numpy
     subtract + square + nanmean op.
  3. **Compact representation.** Traces stored as float32 (NaN for missing)
     instead of being unpacked per-query.

Expected speedup ~30x vs the stdlib version for repeated queries on the same
station/month — which is the bot's usage pattern (many candidates per cycle,
all same station for that day).

Cache eviction: keyed by (station, frozenset(months), sunrise_min). LRU-style;
default cap 128 keys.
"""
from __future__ import annotations

import math
import sqlite3
import statistics
import struct
import sys
from collections import OrderedDict
from datetime import datetime
from typing import Optional

import numpy as np

DB_PATH = "/home/ubuntu/data/heating_traces.sqlite"
N_BINS = 288
BIN_WIDTH_MIN = 5
MISSING = -32768
_CACHE_CAP = 128
_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
# 2026-05-18: lock added for event-driven shadow path. The existing cycle
# loop is single-threaded so didn't need this, but the WS callback +
# wethr filewatch worker call predict() from other threads.
import threading as _threading
_CACHE_LOCK = _threading.Lock()

# 2026-05-19: per-(station, month) P50 of historical day_max_lst_min, used
# to gate the HIGH peak-clamp (post-peak + at-peak tiers). Built lazily on
# first predict() call. ~240 entries, <1MB. See [[project-nn-peak-clamp-20260519]].
_PEAK_QTABLE: dict = {}
_PEAK_QTABLE_LOCK = _threading.Lock()


def _build_peak_qtable(db_path=DB_PATH):
    """Populate _PEAK_QTABLE with per-(station, month) P50 of day_max_lst_min.
    Keys: (station_id_without_K_prefix, month_int). Value: float (minutes since
    LST midnight). Idempotent; safe to call multiple times."""
    with _PEAK_QTABLE_LOCK:
        if _PEAK_QTABLE:
            return _PEAK_QTABLE
        from collections import defaultdict as _dd
        by_sm: dict = _dd(list)
        conn = sqlite3.connect(db_path)
        try:
            for st, mo, dm in conn.execute(
                "SELECT station, month, day_max_lst_min FROM station_days"):
                by_sm[(st, mo)].append(dm)
        finally:
            conn.close()
        for k, vs in by_sm.items():
            arr = np.array(vs, dtype=np.float64)
            _PEAK_QTABLE[k] = float(np.percentile(arr, 50))
        return _PEAK_QTABLE


def _peak_qtable_p50(station, month, db_path=DB_PATH):
    """Lookup P50 for (station, month). Returns None if unknown.
    Strips a leading 'K' from station id (trace DB uses 'ATL', bot passes
    'KATL'). Triggers table build on first call."""
    if not station or month is None:
        return None
    sta = station
    if isinstance(sta, str) and sta.startswith("K") and len(sta) > 3:
        sta = sta[1:]
    if not _PEAK_QTABLE:
        _build_peak_qtable(db_path)
    return _PEAK_QTABLE.get((sta, int(month)))


def _unpack_int16_arr(blob, scale):
    arr = np.frombuffer(blob, dtype=np.int16).astype(np.float32)
    mask = arr == MISSING
    arr = arr / scale
    arr[mask] = np.nan
    return arr


def _unpack_int8_arr(blob):
    return np.frombuffer(blob, dtype=np.int8)


def _unpack_drct_arr(blob):
    arr = np.frombuffer(blob, dtype=np.int16).astype(np.float32)
    arr[arr == -1] = np.nan
    return arr


def _circular_abs_diff(a, b):
    d = np.abs(a - b) % 360.0
    return np.where(d > 180.0, 360.0 - d, d)


def _month_window(target_month: int, half: int) -> tuple[int, ...]:
    out = set()
    for d in range(-half, half + 1):
        m = ((target_month - 1 + d) % 12) + 1
        out.add(m)
    return tuple(sorted(out))


def _interp_to_bins(traj):
    sums = np.zeros(N_BINS, dtype=np.float64)
    cnts = np.zeros(N_BINS, dtype=np.int32)
    for lst_min, val in traj:
        b = lst_min // BIN_WIDTH_MIN
        if 0 <= b < N_BINS:
            sums[b] += val
            cnts[b] += 1
    out = np.where(cnts > 0, sums / np.maximum(cnts, 1), np.nan).astype(np.float32)
    return out


def _load_pool(station, months, sunrise_min, sunrise_window_min, db_path):
    """Load all matching candidates as numpy arrays. Note: _cache_get wraps
    this with the module-level _CACHE (LRU, cap=128) — callers should go
    through _cache_get for fast repeated access (event-driven shadow path)."""
    placeholders = ",".join("?" * len(months))
    sql = (f"SELECT lst_date, day_max_f, day_max_lst_min, day_min_f, day_min_lst_min, "
           f"sunrise_lst_min, tmpf_trace, dwpf_trace, skyc1_trace, sknt_trace, drct_trace, "
           f"relh_trace, pres1_trace "
           f"FROM station_days "
           f"WHERE station=? AND month IN ({placeholders}) "
           f"  AND ABS(sunrise_lst_min - ?) <= ?")
    conn = sqlite3.connect(db_path)
    rows = conn.execute(sql, [station, *months, sunrise_min, sunrise_window_min]).fetchall()
    conn.close()
    if not rows:
        return None
    n = len(rows)
    dates = np.empty(n, dtype="U10")
    day_max = np.empty(n, dtype=np.float32)
    day_max_min = np.empty(n, dtype=np.int32)
    day_min = np.empty(n, dtype=np.float32)
    day_min_min = np.empty(n, dtype=np.int32)
    sr = np.empty(n, dtype=np.int32)
    tmpf = np.empty((n, N_BINS), dtype=np.float32)
    dwpf = np.empty((n, N_BINS), dtype=np.float32)
    sknt = np.empty((n, N_BINS), dtype=np.float32)
    skyc1 = np.empty((n, N_BINS), dtype=np.int8)
    drct = np.empty((n, N_BINS), dtype=np.float32)
    relh = np.empty((n, N_BINS), dtype=np.float32)
    pres1 = np.empty((n, N_BINS), dtype=np.float32)
    for i, r in enumerate(rows):
        dates[i] = r[0]
        day_max[i] = r[1]
        day_max_min[i] = r[2]
        day_min[i] = r[3]
        day_min_min[i] = r[4]
        sr[i] = r[5]
        tmpf[i] = _unpack_int16_arr(r[6], 10)
        dwpf[i] = _unpack_int16_arr(r[7], 10)
        skyc1[i] = _unpack_int8_arr(r[8])
        sknt[i] = _unpack_int16_arr(r[9], 10)
        drct[i] = _unpack_drct_arr(r[10])
        relh[i] = _unpack_int16_arr(r[11], 10)
        pres1[i] = _unpack_int16_arr(r[12], 100)
    return {
        "dates": dates, "day_max": day_max, "day_max_min": day_max_min,
        "day_min": day_min, "day_min_min": day_min_min,
        "sunrise": sr, "tmpf": tmpf, "dwpf": dwpf, "skyc1": skyc1,
        "sknt": sknt, "drct": drct, "relh": relh, "pres1": pres1,
    }


def _cache_get(station, months, sunrise_min, sunrise_window_min, db_path):
    key = (station, months, sunrise_min, sunrise_window_min)
    with _CACHE_LOCK:
        pool = _CACHE.get(key)
        if pool is not None:
            _CACHE.move_to_end(key)
            return pool
    # Load outside the lock — the SQL+unpack takes ~50ms and we don't want
    # to block other readers. Worst case two threads load the same pool
    # once each; second one wins the cache slot.
    pool = _load_pool(station, months, sunrise_min, sunrise_window_min, db_path)
    if pool is not None:
        with _CACHE_LOCK:
            _CACHE[key] = pool
            while len(_CACHE) > _CACHE_CAP:
                _CACHE.popitem(last=False)
    return pool


def predict(
    station,
    target_date_lst,
    cur_lst_min,
    obs_trajectory,
    side="high",
    k=50,
    month_window=1,
    min_window_minutes=60,
    dewpoint_trajectory=None,
    skyc_now=None,
    sknt_trajectory=None,
    drct_now=None,
    relh_trajectory=None,
    pres1_trajectory=None,
    sunrise_window_min=30,
    dewpoint_weight=0.30,
    wind_weight=0.10,
    sky_penalty_per_step=0.40,
    drct_weight=0.015,
    relh_weight=0.0,
    pres_traj_weight=0.0,
    bias_correction=0.0,
    fit_quality_thresh=None,
    gate_low_postnoon_unlocked=True,
    db_path=DB_PATH,
):
    if side not in ("high", "low"):
        raise ValueError("side must be 'high' or 'low'")
    if not obs_trajectory:
        return {"mu_proj_f": None, "reason": "no obs trajectory"}
    cur_tmpf = obs_trajectory[-1][1]
    today = _interp_to_bins(obs_trajectory)
    n_today = int(np.sum(~np.isnan(today)))
    if n_today * BIN_WIDTH_MIN < min_window_minutes:
        return {"mu_proj_f": None, "reason": f"trajectory too short: {n_today} bins"}

    today_dw = _interp_to_bins(dewpoint_trajectory) if dewpoint_trajectory else None
    today_wind = _interp_to_bins(sknt_trajectory) if sknt_trajectory else None
    today_relh = _interp_to_bins(relh_trajectory) if relh_trajectory else None
    today_pres = _interp_to_bins(pres1_trajectory) if pres1_trajectory else None
    target_dt = datetime.strptime(target_date_lst, "%Y-%m-%d")
    months = _month_window(target_dt.month, month_window)

    cur_bin = cur_lst_min // BIN_WIDTH_MIN
    if cur_bin < 0 or cur_bin >= N_BINS:
        return {"mu_proj_f": None, "reason": f"cur_lst_min out of range: {cur_lst_min}"}

    # First fetch with wide sunrise window so we can compute the median.
    wide_pool = _cache_get(station, months, 360, 240, db_path)
    if wide_pool is None:
        return {"mu_proj_f": None, "reason": "no candidates"}
    median_sunrise = int(np.median(wide_pool["sunrise"]))

    pool = _cache_get(station, months, median_sunrise, sunrise_window_min, db_path)
    if pool is None or len(pool["dates"]) < 10:
        return {"mu_proj_f": None,
                "reason": "sunrise window too restrictive",
                "pool_size": 0 if pool is None else len(pool["dates"])}

    # Exclude target date
    keep_mask = pool["dates"] != target_date_lst
    if not keep_mask.all():
        sub_idx = np.where(keep_mask)[0]
    else:
        sub_idx = np.arange(len(pool["dates"]))
    if sub_idx.size < 10:
        return {"mu_proj_f": None, "reason": "pool too small post-filter",
                "pool_size": int(sub_idx.size)}

    # Match window: [sunrise - 30min, cur]
    lo_bin = max(0, (median_sunrise - 30) // BIN_WIDTH_MIN)
    hi_bin = cur_bin
    if hi_bin - lo_bin < 6:
        lo_bin = max(0, cur_bin - 12)
    win = slice(lo_bin, hi_bin + 1)

    hist_tmpf = pool["tmpf"][sub_idx][:, win]                 # (n, W)
    today_win = today[win]                                     # (W,)
    # Require value at cur_bin in candidate
    cur_valid = ~np.isnan(pool["tmpf"][sub_idx][:, cur_bin])
    if cur_valid.sum() < 10:
        return {"mu_proj_f": None,
                "reason": f"few candidates have cur-bin data: {int(cur_valid.sum())}",
                "pool_size": int(sub_idx.size)}

    diff = hist_tmpf - today_win[None, :]
    # ignore NaN pairs
    valid_pairs = ~np.isnan(diff)
    n_paired = valid_pairs.sum(axis=1)
    sse = np.where(valid_pairs, diff ** 2, 0.0).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        mse = np.where(n_paired > 0, sse / np.maximum(n_paired, 1), np.inf)
    tmpf_rmse = np.sqrt(mse)

    # Apply minimum-pair filter (>=6 paired bins) and cur_valid
    valid_match = (n_paired >= 6) & cur_valid
    if valid_match.sum() < 10:
        return {"mu_proj_f": None,
                "reason": f"few well-paired neighbors: {int(valid_match.sum())}",
                "pool_size": int(sub_idx.size)}

    # Dewpoint penalty (optional)
    if today_dw is not None:
        hist_dw = pool["dwpf"][sub_idx][:, win]
        diff_dw = hist_dw - today_dw[None, win]
        valid_dw = ~np.isnan(diff_dw)
        sse_dw = np.where(valid_dw, diff_dw ** 2, 0.0).sum(axis=1)
        n_dw = valid_dw.sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            mse_dw = np.where(n_dw >= 6, sse_dw / np.maximum(n_dw, 1), np.nan)
        dwpf_rmse = np.where(np.isnan(mse_dw), 0.0, np.sqrt(mse_dw))
    else:
        dwpf_rmse = np.zeros_like(tmpf_rmse)

    # Relative humidity penalty (full-window L2). 2026-05-17 backtest n=5000:
    # LOW MAE −0.021°F at weight 0.30, side-aware (HIGH gets w=0). See nn_shadow
    # for side gating.
    if today_relh is not None and relh_weight > 0:
        hist_relh = pool["relh"][sub_idx][:, win]
        diff_rh = hist_relh - today_relh[None, win]
        valid_rh = ~np.isnan(diff_rh)
        sse_rh = np.where(valid_rh, diff_rh ** 2, 0.0).sum(axis=1)
        n_rh = valid_rh.sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            mse_rh = np.where(n_rh >= 6, sse_rh / np.maximum(n_rh, 1), np.nan)
        relh_rmse = np.where(np.isnan(mse_rh), 0.0, np.sqrt(mse_rh))
    else:
        relh_rmse = np.zeros_like(tmpf_rmse)

    # 2026-05-18: pres1 trajectory L2 — LOW-side only (HIGH gets w=0).
    # Held-out backtest seed=1 n=11k LOW MAE −0.040°F at w=5.0 on TODAY's
    # production baseline (k=50, relh w=0.30, bias=0). Per-hour Δ stable
    # across hours 2-7: −0.02, −0.03, −0.04, −0.05, −0.11. Pres1 is station
    # pressure (inHg, stored ×100 in trace blob). Live trajectory comes
    # from pres_history.jsonl snapshots converted altimeter→station_pres
    # via station elevation (see nn_shadow.py).
    if today_pres is not None and pres_traj_weight > 0:
        hist_pres = pool["pres1"][sub_idx][:, win]
        diff_p = hist_pres - today_pres[None, win]
        valid_p = ~np.isnan(diff_p)
        sse_p = np.where(valid_p, diff_p ** 2, 0.0).sum(axis=1)
        n_p = valid_p.sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            mse_p = np.where(n_p >= 6, sse_p / np.maximum(n_p, 1), np.nan)
        pres_rmse = np.where(np.isnan(mse_p), 0.0, np.sqrt(mse_p))
    else:
        pres_rmse = np.zeros_like(tmpf_rmse)

    # Wind speed distance (small weight)
    if today_wind is not None:
        hist_wind = pool["sknt"][sub_idx][:, win]
        diff_wd = hist_wind - today_wind[None, win]
        valid_wd = ~np.isnan(diff_wd)
        sse_wd = np.where(valid_wd, diff_wd ** 2, 0.0).sum(axis=1)
        n_wd = valid_wd.sum(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            mse_wd = np.where(n_wd >= 6, sse_wd / np.maximum(n_wd, 1), np.nan)
        wind_rmse = np.where(np.isnan(mse_wd), 0.0, np.sqrt(mse_wd))
    else:
        wind_rmse = np.zeros_like(tmpf_rmse)

    # Sky cover penalty (categorical, at cur_bin)
    if skyc_now is not None:
        hist_sky = pool["skyc1"][sub_idx][:, cur_bin]
        sky_diff = np.abs(hist_sky.astype(np.int16) - int(skyc_now))
        sky_penalty = np.where(hist_sky > 0,
                               np.minimum(2.5, sky_diff * sky_penalty_per_step),
                               0.0).astype(np.float32)
    else:
        sky_penalty = np.zeros_like(tmpf_rmse)

    # 2026-05-17: wind direction circular distance at cur_bin (F2 backtest
    # n=5937 HIGH / 7623 LOW: HIGH MAE -0.051°F, LOW p95 5.40→5.20).
    if drct_now is not None and not (isinstance(drct_now, float) and math.isnan(drct_now)):
        hist_drct = pool["drct"][sub_idx][:, cur_bin]
        dd = _circular_abs_diff(hist_drct, float(drct_now))
        drct_pen = np.where(np.isnan(dd), 0.0, dd).astype(np.float32)
    else:
        drct_pen = np.zeros_like(tmpf_rmse)

    score = (tmpf_rmse + dewpoint_weight * dwpf_rmse
             + wind_weight * wind_rmse + sky_penalty
             + drct_weight * drct_pen
             + relh_weight * relh_rmse
             + pres_traj_weight * pres_rmse)
    score = np.where(valid_match, score, np.inf)

    # Top-k by score
    order = np.argsort(score)[:k]
    n_used = (~np.isinf(score[order])).sum()
    if n_used < 10:
        return {"mu_proj_f": None,
                "reason": f"few neighbors after scoring: {int(n_used)}",
                "pool_size": int(sub_idx.size)}
    order = order[:n_used]

    if side == "high":
        peak = pool["day_max"][sub_idx][order]
    else:
        peak = pool["day_min"][sub_idx][order]
    tmpf_at_cur = pool["tmpf"][sub_idx][order, cur_bin]
    deltas = (peak - tmpf_at_cur).astype(np.float64)
    median_delta = float(np.median(deltas))
    mean_delta = float(np.mean(deltas))
    stdev_delta = float(np.std(deltas, ddof=1)) if deltas.size > 1 else 0.0

    # 2026-05-18 (Action C partial): per-side aggregator. HIGH uses idw3
    # (inverse-cube-distance weighted mean of top-k); LOW uses wins10 of top-20
    # (winsorized 10/90 mean of the 20 closest). Cross-year hold-out CRPS
    # −1.7% (HIGH idw3) / −5.6% (LOW wins10), zero per-station regression >5%.
    # Per-station tested + rejected (same lesson as project_nn_per_station_k_negative).
    # fit_quality gate still uses raw stdev_delta of top-k (analog dispersion).
    # Rollback: NN_USE_NEW_AGGREGATORS=False reverts to median behavior.
    try:
        import config as _cfg_agg
        _use_new_agg = bool(getattr(_cfg_agg, "NN_USE_NEW_AGGREGATORS", True))
    except Exception:
        _use_new_agg = True
    _scores_sub = score[order].astype(np.float64)
    _aggregator_name = "median"
    if _use_new_agg and side == "high":
        # idw3 weighted mean over all top-k
        _w = 1.0 / np.power(_scores_sub + 0.05, 3.0)
        _w_sum = float(np.sum(_w))
        if _w_sum > 0:
            mu_delta_agg = float(np.sum(_w * deltas) / _w_sum)
            _w_norm = _w / _w_sum
            _mu_w = float(np.sum(_w_norm * deltas))
            _var_w = float(np.sum(_w_norm * (deltas - _mu_w) ** 2))
            _n_eff = _w_sum ** 2 / max(float(np.sum(_w ** 2)), 1e-9)
            if _n_eff > 1:
                _var_w *= _n_eff / (_n_eff - 1)
            sigma_natural = math.sqrt(max(_var_w, 0.0))
        else:
            mu_delta_agg = median_delta
            sigma_natural = stdev_delta
        _aggregator_name = "idw3"
        _n_aggregated = int(deltas.size)
    elif _use_new_agg and side == "low":
        # wins10 of top-20: winsorize 10/90 percentiles then take mean
        _n_sub = min(20, int(deltas.size))
        _d20 = deltas[:_n_sub]
        if _d20.size >= 3:
            _lo = float(np.percentile(_d20, 10))
            _hi = float(np.percentile(_d20, 90))
            _d_clip = np.clip(_d20, _lo, _hi)
            mu_delta_agg = float(np.mean(_d_clip))
            sigma_natural = float(np.std(_d_clip, ddof=1)) if _d_clip.size > 1 else stdev_delta
        elif _d20.size >= 1:
            mu_delta_agg = float(np.mean(_d20))
            sigma_natural = float(np.std(_d20, ddof=1)) if _d20.size > 1 else stdev_delta
        else:
            mu_delta_agg = median_delta
            sigma_natural = stdev_delta
        _aggregator_name = "wins10_k20"
        _n_aggregated = int(_d20.size)
    else:
        mu_delta_agg = median_delta
        sigma_natural = stdev_delta
        _n_aggregated = int(deltas.size)

    # 2026-05-18: analog distribution summary for packet (replaces top-3 cherry-pick).
    # Backtest 2024-25 n=2308: top3_med MAE is +9% (HIGH) / +12% (LOW) WORSE than
    # median-of-50. The LLM cherry-picking top-3 to justify contrarian reads is
    # an antipattern; exposing the full p25/p50/p75 + bracket-fraction (computed
    # downstream in paper_judge_bot.py using floor/cap) prevents it.
    _peak_arr = peak.astype(np.float64)
    _analog_summary = {
        "day_extremes": [round(float(x), 2) for x in _peak_arr.tolist()],
        "day_extremes_p25_p50_p75": [
            round(float(np.quantile(_peak_arr, 0.25)), 2),
            round(float(np.quantile(_peak_arr, 0.50)), 2),
            round(float(np.quantile(_peak_arr, 0.75)), 2),
        ],
        "deltas_p25_p50_p75": [
            round(float(np.quantile(deltas, 0.25)), 2),
            round(float(np.quantile(deltas, 0.50)), 2),
            round(float(np.quantile(deltas, 0.75)), 2),
        ],
    }

    # 2026-05-17 (P2): fit-quality gate. Reject when neighbor cluster is too
    # spread (poor analog fit → unreliable projection). Bot's fallback chain
    # (anchored / rm_ceiling / consensus_corr) takes over. Realistic backtest
    # n=1200/side: HIGH thresh=3.0 fires 39% MAE 1.72°F; LOW thresh=4.0 fires
    # 78% MAE 1.97°F. Locked-mode (extreme already in trajectory) bypasses
    # the gate below — handled after physical-constraint block.
    if (fit_quality_thresh is not None and fit_quality_thresh > 0
            and stdev_delta > fit_quality_thresh):
        return {"mu_proj_f": None,
                "sigma_proj_f": round(stdev_delta, 2),
                "reason": f"fit_quality_gate (σ={stdev_delta:.2f} > {fit_quality_thresh})",
                "pool_size": int(sub_idx.size),
                "n_neighbors_used": int(n_used)}

    # 2026-05-17 (P1): per-side bias correction. Bias is applied to the
    # aggregator's mu_delta (idw3 for HIGH, wins10 for LOW post-2026-05-18,
    # or legacy median when NN_USE_NEW_AGGREGATORS=False). All downstream
    # paths (physical clamp, locked-mode) see a calibrated base.
    median_delta_corrected = mu_delta_agg + bias_correction
    mu_proj = cur_tmpf + median_delta_corrected

    # Apply trajectory-based physical constraints:
    #   HIGH peak >= max(traj). If peak already in trajectory (max-bin before
    #     cur) AND we're past typical peak time (>= 14:00 LST), trust traj_max.
    #   LOW  trough <= min(traj). If trough already in trajectory (min-bin
    #     before cur) AND trough_bin < peak_bin (morning low pattern), trust
    #     traj_min.
    traj_lst_mins = np.array([m for m, _ in obs_trajectory], dtype=np.int32)
    traj_vals = np.array([v for _, v in obs_trajectory], dtype=np.float32)
    extreme_locked = False
    peak_clamp_tier = None  # diagnostic: "post_peak" | "at_peak" | None
    if side == "high":
        traj_max = float(np.nanmax(traj_vals))
        # 2026-05-17 (Action A): HIGH lock removed entirely. Stratify n=32k
        # 2024-2025 across 20 stns: locked HIGH at 16:00 had MAE 1.32 / bias
        # -1.32; unlocked at same hour MAE 1.06 / bias -0.31 (-9% MAE, -56%
        # bias). Peak often arrives LATER than the locked trajectory_max
        # snapshot, so locking truncates μ below the eventual true peak.
        # The physical max-floor (mu >= traj_max) still handles past-peak
        # cases. See [[project-action-a-no-high-lock-20260517]].
        mu_proj = max(mu_proj, traj_max)

        # 2026-05-19: two-tier HIGH peak clamp. Gated by past per-(station, month)
        # P50 historical peak time. Tier 1 (post-peak, tight): peak >=30 min old
        # AND temp dropped >=0.5°F in last 30 min → cap at traj_max + 0.75°F.
        # Tier 2 (at-peak, loose): cur_tmpf within 1.0°F of traj_max → cap at
        # traj_max + 1.0°F. When both fire, lowest cap wins (tier 1's tighter
        # cap applies). Both are NO-OPs before P50.
        # Cross-year backtest 2024-25 + 2023 hold-out (~23k eval rows / 20 stns):
        #   overall MAE     -14.5% / -12.7%
        #   at_peak ±30     -25.0% / -26.3%
        #   post_peak >90   -33.4% / -29.5%
        #   pre_peak >60m    +2.5% /  +3.3%  (acceptable cost)
        # Full margin grid swept: t1 ∈ {0.5, 0.75, 1.0}, t2 ∈ {0.5, 0.75, 1.0,
        # 1.5, 2.0}, band ∈ {0.5, 1.0}. Cross-year winner: t1=0.75, t2=1.0,
        # band=1.0. See [[project-nn-peak-clamp-20260519]].
        try:
            import config as _cfg_pkclamp
            _pkclamp_on = bool(getattr(_cfg_pkclamp, "NN_HIGH_PEAK_CLAMP_ENABLED", True))
            _post_peak_margin_f = float(getattr(_cfg_pkclamp, "NN_HIGH_POST_PEAK_MARGIN_F", 0.75))
            _at_peak_margin_f = float(getattr(_cfg_pkclamp, "NN_HIGH_AT_PEAK_MARGIN_F", 1.0))
            _at_peak_band_f = float(getattr(_cfg_pkclamp, "NN_HIGH_AT_PEAK_TEMP_BAND_F", 1.0))
        except Exception:
            _pkclamp_on = True
            _post_peak_margin_f = 0.75
            _at_peak_margin_f = 1.0
            _at_peak_band_f = 1.0
        if _pkclamp_on:
            _p50 = _peak_qtable_p50(station, target_dt.month, db_path)
            if _p50 is not None and cur_lst_min >= _p50:
                max_idx_hi = int(np.nanargmax(traj_vals))
                max_lst_min_hi = int(traj_lst_mins[max_idx_hi])
                peak_age = cur_lst_min - max_lst_min_hi
                # Recent 30-min window: any reading within 0.5°F of traj_max?
                recent30_mask = traj_lst_mins >= (cur_lst_min - 30)
                if recent30_mask.any():
                    recent30_max = float(np.nanmax(traj_vals[recent30_mask]))
                else:
                    recent30_max = traj_max
                tier1_fires = (peak_age >= 30 and recent30_max < traj_max - 0.5)
                tier2_fires = (cur_tmpf >= traj_max - _at_peak_band_f)
                if tier1_fires:
                    _cap = traj_max + _post_peak_margin_f
                    mu_proj = max(traj_max, min(mu_proj, _cap))
                    peak_clamp_tier = "post_peak"
                elif tier2_fires:
                    _cap = traj_max + _at_peak_margin_f
                    mu_proj = max(traj_max, min(mu_proj, _cap))
                    peak_clamp_tier = "at_peak"
    else:
        traj_min = float(np.nanmin(traj_vals))
        min_idx = int(np.nanargmin(traj_vals))
        min_lst_min = int(traj_lst_mins[min_idx])
        traj_max = float(np.nanmax(traj_vals))
        max_idx = int(np.nanargmax(traj_vals))
        max_lst_min = int(traj_lst_mins[max_idx])
        # 2026-05-19 (B-Gate-21): require cur_lst_min >= NN_LOCK_FLOOR_LST_MIN
        # (default 21*60 = 9 PM) before locking. Old floor of 12*60 (noon) trusted
        # an 11-hour-stale morning trough at 6 PM, but evening cooling routinely
        # drops actual day_min BELOW traj_min on cold/dry/clear nights. Pooled
        # n=269 cross-year: residual bias +1.93°F at hr18, +1.28°F at hr20, ~0°F
        # at hr22. Gating to 21:00 drops the broken-lock cells; the
        # gate_low_postnoon_unlocked branch below routes them to fallback chain
        # (anchored / rm_ceiling / consensus_corr). Hold-out CRPS −2.3% on LOW
        # global, cross-year stable per-hour median bias identical (+3/+2/+2).
        # Rollback: NN_LOCK_FLOOR_LST_MIN=12*60 in config.py restores old behavior.
        try:
            import config as _cfg_lock
            _lock_floor = int(getattr(_cfg_lock, "NN_LOCK_FLOOR_LST_MIN", 21 * 60))
        except Exception:
            _lock_floor = 21 * 60
        if (min_lst_min < max_lst_min
                and cur_lst_min >= _lock_floor
                and (cur_lst_min - min_lst_min) > 60):
            mu_proj = traj_min
            extreme_locked = True
        else:
            mu_proj = min(mu_proj, traj_min)

    # 2026-05-18: gate unlocked LOW post-noon (cooling-event projections
    # unreliable). When LOW post-noon (cur_lst_min >= 12*60) but NOT
    # extreme_locked, the daily minimum either came AFTER the daily max
    # (cold front / T-storm cooling) or is too recent (< 1h ago, still
    # falling). Investigation 2026-05-18 (n=4800 2024-25 + n=3600 2023):
    # such cases have MAE ~3.0°F with negative-bias long tail (DCA cold
    # snap 2025-11-27 had err +35°F). Locked cases MAE 0.73°F. Skipping
    # the 9.9-12.4% unlocked post-noon evals drops post-noon LOW MAE by
    # -25% on both years.
    if (gate_low_postnoon_unlocked and side == "low"
            and cur_lst_min >= 12 * 60 and not extreme_locked):
        return {"mu_proj_f": None,
                "sigma_proj_f": None,
                "reason": "low_postnoon_unlocked_unreliable",
                "n_neighbors_used": int(n_used),
                "pool_size": int(sub_idx.size),
                "extreme_locked": False,
                "side": side}

    # 2026-05-17 (Action C): cohort-aware σ multiplier.
    # 2026-05-18 refit for new aggregators (idw3 HIGH, wins10 LOW):
    #   HIGH idw3            best sigma_factor 0.90 (was 0.85 for median)
    #   LOW wins10 unl_am    best sigma_factor 1.10 (was 0.85 for median)
    #   LOW unl_pm / locked  unchanged (post-noon-unlocked gated; locked uses traj_min)
    # Multiplier applied AFTER fit_quality gate (which uses raw stdev_delta
    # of top-k for analog-cluster spread). sigma_natural is aggregator-specific
    # (weighted-stdev for idw3, winsorized-stdev for wins10, plain stdev for median).
    sigma_factor = 1.0
    if side == "high":
        sigma_factor = 0.90 if _use_new_agg else 0.85
    elif side == "low" and not extreme_locked and cur_lst_min < 12 * 60:
        sigma_factor = 1.10 if _use_new_agg else 0.85
    sigma_proj_out = sigma_natural * sigma_factor

    return {
        "mu_proj_f": round(float(mu_proj), 2),
        "sigma_proj_f": round(sigma_proj_out, 2),
        "sigma_raw_f": round(stdev_delta, 2),
        "sigma_natural_f": round(sigma_natural, 2),
        "analog_summary": _analog_summary,
        "sigma_factor_applied": sigma_factor,
        "aggregator": _aggregator_name,
        "n_aggregated": _n_aggregated,
        "mu_delta_agg_f": round(mu_delta_agg, 3),
        "mean_delta_f": round(mean_delta, 2),
        "median_delta_f": round(median_delta, 2),
        "bias_correction_applied_f": round(float(bias_correction), 3),
        "fit_quality_thresh": fit_quality_thresh,
        "cur_tmpf": float(cur_tmpf),
        "extreme_locked": extreme_locked,
        "peak_clamp_tier": peak_clamp_tier,
        "n_neighbors_used": int(n_used),
        "pool_size": int(sub_idx.size),
        "match_window_bins": (int(lo_bin), int(hi_bin)),
        "sunrise_used": int(median_sunrise),
        "side": side,
        "method": (f"nn_match_{side}_locked" if extreme_locked
                   else (f"nn_match_{side}_pkclamp_{peak_clamp_tier}"
                         if peak_clamp_tier else f"nn_match_{side}")),
        "top_neighbors": [
            {"date": str(pool["dates"][sub_idx][order[i]]),
             "tmpf_rmse": round(float(tmpf_rmse[order[i]]), 2),
             "dwpf_rmse": round(float(dwpf_rmse[order[i]]), 2),
             "sky_pen": round(float(sky_penalty[order[i]]), 2),
             "delta_f": round(float(deltas[i]), 2),
             ("day_max_f" if side == "high" else "day_min_f"):
                 float(peak[i])}
            for i in range(min(8, len(order)))
        ],
    }


if __name__ == "__main__":
    # Speed test: run 100 queries on ATL 2023-07-04
    import time
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT day_max_f, tmpf_trace, sunrise_lst_min FROM station_days "
        "WHERE station='ATL' AND lst_date='2023-07-04'").fetchone()
    conn.close()
    if not row:
        print("test day not in DB"); sys.exit(1)
    dmax, blob, sr = row
    tmpf_arr = _unpack_int16_arr(blob, 10)
    cur_bin = 144
    cur_lst_min = cur_bin * 5
    trajectory = []
    for b in range(sr // 5, cur_bin + 1):
        v = float(tmpf_arr[b])
        if not math.isnan(v):
            trajectory.append((b * 5, v))

    print(f"Speed test: 100 calls on ATL 2023-07-04 noon")
    t0 = time.time()
    for _ in range(100):
        res = predict("ATL", "2023-07-04", cur_lst_min, trajectory,
                      side="high", k=30, month_window=1)
    dt = time.time() - t0
    print(f"  {dt*10:.1f} ms/call  (100 calls in {dt:.2f}s)")
    print(f"  Last result: mu={res['mu_proj_f']} sigma={res['sigma_proj_f']} actual={dmax}")
    print(f"  Pool: {res['pool_size']}  Used neighbors: {res['n_neighbors_used']}")
