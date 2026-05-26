"""nn_shadow.py — paper_judge_bot side adapter for nn_match.

Builds the trajectory from existing `today_obs_hist` (the bot already pulls
this for the prompt block), wraps the call to `nn_match_fast.predict`, and
returns a compact dict for shadow logging.

Returns ``None`` on any error — never raises into the bot's hot path.

Wire-up (one-line addition to paper_judge_bot.py):

    from nn_shadow import shadow_nn_proj
    candidate_record["shadow_mu_nn"] = shadow_nn_proj(packet)

A None result is fine; downstream backtest will just see a missing field.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from typing import Optional

# Use the same station_tz table the bot uses
_STATION_TZ = {
    "KATL": "America/New_York", "KAUS": "America/Chicago",
    "KBOS": "America/New_York", "KDCA": "America/New_York",
    "KDEN": "America/Denver", "KDFW": "America/Chicago",
    "KHOU": "America/Chicago", "KLAS": "America/Los_Angeles",
    "KLAX": "America/Los_Angeles", "KMDW": "America/Chicago",
    "KMIA": "America/New_York", "KMSP": "America/Chicago",
    "KMSY": "America/Chicago", "KNYC": "America/New_York",
    "KOKC": "America/Chicago", "KPHL": "America/New_York",
    "KPHX": "America/Phoenix", "KSAT": "America/Chicago",
    "KSEA": "America/Los_Angeles", "KSFO": "America/Los_Angeles",
}

# Lazy import to avoid hard dependency at import time. 2026-05-17: removed
# sys.path.insert(0, "/home/ubuntu/tools") so nn_match_fast loads ONLY from
# the bot's CWD (/home/ubuntu/paper_judge_bot/). Single source of truth,
# eliminates two-file drift. /home/ubuntu/tools/nn_match_fast.py is preserved
# but no longer imported by the bot.
_PREDICT = None
def _get_predict():
    global _PREDICT
    if _PREDICT is None:
        try:
            from nn_match_fast import predict
            _PREDICT = predict
        except Exception:
            _PREDICT = False  # mark as unavailable
    return _PREDICT if _PREDICT is not False else None


def shadow_nn_proj(packet: dict) -> Optional[dict]:
    """Return {"mu", "sigma", "method", "n_neighbors", "neighbors"} or None.

    Requires the packet to have:
      - station (ICAO, e.g. "KATL")
      - series (string containing "HIGH" or "LOW")
      - climate_day ("YYYY-MM-DD" in station LST)
      - today_obs_hist (list of {hour_utc_iso, temp_f, dewpt_f, ...})

    If a key is missing or the matcher returns no pool, returns None.
    """
    try:
        return _shadow_nn_proj_inner(packet)
    except Exception:
        return None


def _shadow_nn_proj_inner(packet: dict) -> Optional[dict]:
    predict = _get_predict()
    if predict is None:
        return None

    station_icao = (packet.get("station") or "").upper()
    if not station_icao.startswith("K"):
        return None
    station_iata = station_icao[1:]

    target_lst_date = packet.get("climate_day")
    if not target_lst_date:
        return None

    series = (packet.get("series") or "").upper()
    if "HIGH" in series:
        side = "high"
    elif "LOW" in series:
        side = "low"
    else:
        return None

    tz_name = _STATION_TZ.get(station_icao)
    if not tz_name:
        return None
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name)
    now_lst = datetime.now(tz)
    cur_lst_min = now_lst.hour * 60 + now_lst.minute

    # Build trajectory from hourly obs already in packet (the bot loads these
    # for the prompt block — same data source, no extra IO).
    today_obs = (packet.get("hourly_obs_today")
                 or packet.get("today_obs_hist") or [])
    trajectory: list[tuple[int, float]] = []
    dw_traj: list[tuple[int, float]] = []
    for h in today_obs:
        if h.get("temp_f") is None:
            continue
        iso = h.get("hour_utc_iso")
        if not iso:
            continue
        try:
            ts = datetime.strptime(iso, "%Y-%m-%dT%H:00Z").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        dt_lst = ts.astimezone(tz)
        if dt_lst.strftime("%Y-%m-%d") != target_lst_date:
            continue
        lst_min = dt_lst.hour * 60 + dt_lst.minute
        trajectory.append((lst_min, float(h["temp_f"])))
        if h.get("dewpt_f") is not None:
            dw_traj.append((lst_min, float(h["dewpt_f"])))

    # P0 2026-05-17: enrich trajectory with 5-min cadence wethr temp_history
    # (last 60 min from shared cache). Required because hourly_obs_today is
    # 1-hour cadence → at most 5-6 5-min bins populated → predict()'s
    # min_window_minutes=60 gate (≥12 bins) was rejecting every call. See
    # [[project-judge-nn-audit-20260517]] §1. temp_history has ~13 entries /
    # hour at ~5-min cadence, density-matches the historical DB trace.
    try:
        import shared_cache_reader as scache
        th = scache.get_temp_history(station_icao, lookback_sec=3600.0)
        for h in th:
            tv = h.get("temp_f")
            if tv is None:
                continue
            tts = h.get("ts")
            if tts is None:
                continue
            try:
                dt_utc = datetime.fromtimestamp(float(tts), tz=timezone.utc)
            except (ValueError, OSError, TypeError):
                continue
            dt_lst = dt_utc.astimezone(tz)
            if dt_lst.strftime("%Y-%m-%d") != target_lst_date:
                continue
            lst_min = dt_lst.hour * 60 + dt_lst.minute
            trajectory.append((lst_min, float(tv)))
    except Exception:
        # If shared_cache_reader unavailable or temp_history malformed,
        # fall through with hourly-only trajectory (matcher may still skip
        # for trajectory-too-short, that's the prior behavior).
        pass

    # Append current obs (more recent than the most recent hourly snapshot).
    # paper_judge_bot packet stores latest live obs under "live_obs" with
    # field temp_f / dewpt_f.
    lo = packet.get("live_obs") or {}
    cur_temp = lo.get("temp_f")
    if cur_temp is not None:
        trajectory.append((cur_lst_min, float(cur_temp)))
        cur_dw = lo.get("dewpt_f")
        if cur_dw is not None:
            dw_traj.append((cur_lst_min, float(cur_dw)))

    # Sort by lst_min after merging hourly + temp_history + live (different
    # cadences may interleave). _interp_to_bins averages duplicates per bin.
    trajectory.sort(key=lambda x: x[0])
    dw_traj.sort(key=lambda x: x[0])

    # 2026-05-18: per-side trajectory lookback window (config: NN_LOOKBACK_*).
    # Deep-dive backtest n=1010-1133 (HIGH 2024-2025) + n=932 (HIGH 2023 hold-
    # out): k=150 + lookback=180min beats sunrise-anchored at k=50 by -9.2%
    # MAE / bias goes to -0.025 (near zero). LOW lookback gain was -1.7%
    # (not material) so LOW defaults NN_LOOKBACK_LOW_MIN=0 = no truncation.
    try:
        import config as _cfg_lb
        _lb_min = int(getattr(_cfg_lb,
            "NN_LOOKBACK_HIGH_MIN" if side == "high" else "NN_LOOKBACK_LOW_MIN", 0))
    except Exception:
        _lb_min = 0
    if _lb_min > 0:
        _cutoff = cur_lst_min - _lb_min
        trajectory = [(m, t) for (m, t) in trajectory if m >= _cutoff]
        dw_traj = [(m, t) for (m, t) in dw_traj if m >= _cutoff]

    if len(trajectory) < 3:
        return None

    # 2026-05-17: relh full-window L2 — LOW-side only (HIGH gets w=0).
    # Backtest n=5000: LOW MAE −0.021°F at w=0.30 alone, −0.014°F stacked
    # with k=50. Derive per-point RH from (temp_F, dewpoint_F) via Magnus
    # formula since hourly_obs_today records carry temp_f / dewpt_f but not
    # relh directly. Stays in production path (no upstream schema dep).
    import math as _math
    def _rh_from_t_td(t_f, td_f):
        t_c = (t_f - 32.0) * 5.0 / 9.0
        td_c = (td_f - 32.0) * 5.0 / 9.0
        # Magnus-Tetens
        es_t = 6.112 * _math.exp(17.67 * t_c / (t_c + 243.5))
        es_td = 6.112 * _math.exp(17.67 * td_c / (td_c + 243.5))
        return max(0.0, min(100.0, 100.0 * es_td / es_t))

    relh_traj: list[tuple[int, float]] = []
    # Match by lst_min: any tmpf point with a matching dwpt at same lst_min
    dw_by_min = {m: v for m, v in dw_traj}
    for m, t in trajectory:
        td = dw_by_min.get(m)
        if td is None:
            continue
        try:
            relh_traj.append((m, _rh_from_t_td(t, td)))
        except Exception:
            continue
    relh_traj.sort(key=lambda x: x[0])

    # 2026-05-17 (F2): wind direction at cur for synoptic-regime kNN match.
    # Backtest (n=5937 HIGH / 7623 LOW) at drct_weight=0.015: HIGH MAE
    # -0.051°F (-2.5%), LOW p95 5.40→5.20. See
    # [[project-nn-match-f2-backtest-20260517]]. wethr_obs.wind_direction is
    # degrees (0..360, may be int) or absent.
    wo = packet.get("wethr_obs") or {}
    drct_now = wo.get("wind_direction")
    if isinstance(drct_now, (int, float)):
        drct_now = float(drct_now)
    else:
        drct_now = None

    # 2026-05-17 (P1+P2): per-side bias correction + fit-quality gate from
    # config. Defaults to no-op if config missing. See
    # [[project-judge-nn-audit-20260517]] and [[project-nn-match-f2-backtest-20260517]].
    # 09:55 UTC revision: hour-aware HIGH bias (constant LOW correction
    # was REGRESSING MAE +40%). HIGH bias inverts sign at LST cutoff hour.
    try:
        import config as _cfg
        if side == "high":
            # Legacy constant (default 0) + hour-aware delta
            base = float(getattr(_cfg, "NN_BIAS_CORR_HIGH_F", 0.0))
            cutoff_h = int(getattr(_cfg, "NN_BIAS_HIGH_CUTOFF_HOUR", 11))
            if now_lst.hour < cutoff_h:
                bias_corr = base + float(getattr(_cfg, "NN_BIAS_CORR_HIGH_MORNING_F", 0.0))
            else:
                bias_corr = base + float(getattr(_cfg, "NN_BIAS_CORR_HIGH_AFTERNOON_F", 0.0))
            fit_thresh = getattr(_cfg, "NN_FIT_QUALITY_THRESH_HIGH", None)
        else:
            bias_corr = float(getattr(_cfg, "NN_BIAS_CORR_LOW_F", 0.0))
            fit_thresh = getattr(_cfg, "NN_FIT_QUALITY_THRESH_LOW", None)
        if fit_thresh is not None:
            fit_thresh = float(fit_thresh)
    except Exception:
        bias_corr = 0.0
        fit_thresh = None

    # 2026-05-17: side-aware relh weight. LOW gets w=0.30, HIGH gets 0
    # (HIGH backtest showed neutral/marginal regression at any w>0).
    relh_w = 0.30 if side == "low" else 0.0

    # 2026-05-18: per-side k from config (NN_K_HIGH=150 / NN_K_LOW=50).
    try:
        import config as _cfg_k
        _k = int(getattr(_cfg_k,
            "NN_K_HIGH" if side == "high" else "NN_K_LOW", 50))
    except Exception:
        _k = 50

    # 2026-05-18: build pres1 trajectory from rolling altimeter snapshots
    # (pres_history.jsonl, written each cycle by live_data.py). Convert
    # altimeter (inHg, sea-level corrected) → station pressure (inHg) via
    # standard-atmosphere formula using station_meta elevation_ft.
    # Backtest n=11k LOW MAE −0.040°F at pres_traj_weight=5.0 (held-out
    # seed=1). HIGH gets weight=0 — Exp3 showed no HIGH gain. Trajectory
    # builds up over ~3h post-restart before matcher gate (≥6 paired bins)
    # has effect; before then matcher silently degrades to baseline (no
    # pres rmse term contribution).
    pres_traj: list[tuple[int, float]] = []
    pres_traj_w = 0.0
    try:
        import config as _cfg_pres
        pres_traj_w = float(getattr(_cfg_pres,
            "NN_PRES_TRAJ_WEIGHT_HIGH" if side == "high"
            else "NN_PRES_TRAJ_WEIGHT_LOW", 0.0))
    except Exception:
        pres_traj_w = 0.0
    if pres_traj_w > 0:
        try:
            import pres_history as _ph
            import station_meta as _smeta
            meta = (_smeta.STATION_META or {}).get(station_icao) or {}
            elev_ft = meta.get("elev_ft")
            recs = _ph.get_history(station_icao, lookback_sec=3 * 3600.0)
            if elev_ft is not None and recs:
                elev_m = float(elev_ft) * 0.3048
                # station_pres = altimeter × (1 − 0.0065·h/288.15)^5.2561
                # Hypsometric / standard-atmosphere conversion.
                factor = (1.0 - 6.5e-3 * elev_m / 288.15) ** 5.2561
                for r in recs:
                    try:
                        dt_utc = datetime.fromtimestamp(
                            float(r["ts"]), tz=timezone.utc)
                    except (ValueError, OSError, TypeError):
                        continue
                    dt_lst = dt_utc.astimezone(tz)
                    if dt_lst.strftime("%Y-%m-%d") != target_lst_date:
                        continue
                    lst_min = dt_lst.hour * 60 + dt_lst.minute
                    pres_traj.append((lst_min, float(r["alt_inhg"]) * factor))
                pres_traj.sort(key=lambda x: x[0])
        except Exception:
            pres_traj = []

    # 2026-05-18: gate flag (skip LOW post-noon unlocked — cooling events)
    try:
        import config as _cfg_gate
        _gate_low_pn_unl = bool(getattr(_cfg_gate, "NN_LOW_GATE_UNLOCKED_POSTNOON", True))
    except Exception:
        _gate_low_pn_unl = True

    res = predict(
        station=station_iata,
        target_date_lst=target_lst_date,
        cur_lst_min=cur_lst_min,
        obs_trajectory=trajectory,
        side=side,
        k=_k,
        dewpoint_trajectory=dw_traj or None,
        drct_now=drct_now,
        bias_correction=bias_corr,
        fit_quality_thresh=fit_thresh,
        gate_low_postnoon_unlocked=_gate_low_pn_unl,
        relh_trajectory=relh_traj or None,
        relh_weight=relh_w,
        pres1_trajectory=pres_traj or None,
        pres_traj_weight=pres_traj_w,
    )
    mu = res.get("mu_proj_f")
    if mu is None:
        return None
    return {
        "mu": mu,
        "sigma": res.get("sigma_proj_f"),
        "sigma_raw": res.get("sigma_raw_f"),
        "sigma_natural": res.get("sigma_natural_f"),  # raw analog-spread, for regime bucketing
        "sigma_factor": res.get("sigma_factor_applied"),
        "bias_correction": res.get("bias_correction_applied_f"),
        "fit_quality_thresh": res.get("fit_quality_thresh"),
        "method": res.get("method"),
        "side": side,
        "extreme_locked": res.get("extreme_locked", False),
        "n_neighbors": res.get("n_neighbors_used"),
        "pool_size": res.get("pool_size"),
        "median_delta": res.get("median_delta_f"),
        "match_dist_mean": res.get("match_dist_mean_f"),
        "match_dist_min": res.get("match_dist_min_f"),
        "cur_tmpf": res.get("cur_tmpf"),
        "neighbors": res.get("top_neighbors", []),
        "analog_summary": res.get("analog_summary"),
    }
