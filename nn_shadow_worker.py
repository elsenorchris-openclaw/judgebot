"""nn_shadow_worker.py — event-driven pure-nn shadow harness.

Triggers on:
  - Kalshi WS BBO change (via kalshi_ws.register_bbo_callback)
  - wethr cache file mtime change (5s poll thread)

For each event, builds a lightweight packet, runs nn_shadow + pure_nn_decide,
and logs the decision to data/shadow_nn_strategy.jsonl. No orders are placed.

Design constraints:
  - All callbacks must be try/except-wrapped so a shadow bug never crashes
    the WS or wethr cache loops.
  - Per-ticker mutex + 30s debounce to avoid flooding on BBO flutter.
  - In-process. Shares wallet/position read state via the same modules the
    bot uses; no external sockets.
  - Pure-nn decide path is pool-cached (via nn_match_fast._cache_get).

Lifecycle:
  - `start(rt)` registers the WS callback and spawns the wethr poll thread.
  - `stop()` signals the wethr thread to exit and unregisters the WS callback.
  - rt is the bot's Runtime; we use it to read positions, universe, climate
    normals — never to write state.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import kalshi_client
import kalshi_ws
import market_universe
import nn_shadow
import nn_shadow_strategy
import shared_cache_reader
import climate_normals
import wethr_rm
import config as _cfg

log = logging.getLogger("judge.nn_shadow_worker")

# ─────────────────────────────────────────────────────────────────────────────
# Module config
# ─────────────────────────────────────────────────────────────────────────────
SHADOW_LOG_PATH = Path("/home/ubuntu/paper_judge_bot/data/shadow_nn_strategy.jsonl")
WETHR_CACHE_PATH = Path("/home/ubuntu/shared/wethr_cache.json")
DEBOUNCE_SEC = 30.0          # min seconds between evaluations of same ticker
WETHR_POLL_SEC = 5.0         # how often the wethr filewatch thread wakes up
ASK_CHANGE_MIN_C = 1         # ignore BBO callbacks where ask didn't move ≥ this

# ─────────────────────────────────────────────────────────────────────────────
# Module state
# ─────────────────────────────────────────────────────────────────────────────
_started = False
_stop_event = threading.Event()
_wethr_thread: Optional[threading.Thread] = None
_wethr_socket_thread: Optional[threading.Thread] = None
WETHR_EVENT_SOCK_PATH = "/tmp/wethr_events.sock"
_log_writer_lock = threading.Lock()
_per_ticker_locks: dict[str, threading.Lock] = {}
_per_ticker_locks_lock = threading.Lock()
_last_eval_ts: dict[str, float] = {}
_last_eval_lock = threading.Lock()
_wethr_obs_ts_seen: dict[str, float] = {}  # station → last obs_ts processed

# 2026-05-18: T-bracket geometry cache. market_universe.parse_ticker returns
# floor=None, cap=None for T-brackets (one-sided tails) — the bot's cycle
# patches these via list_candidates which reads Kalshi's strike_type +
# floor_strike / cap_strike. The event-driven worker bypasses that path,
# so we cache the patched geometry per-ticker on first lookup. Bracket
# geometry doesn't change for the life of the ticker, so this is cache-
# forever. (None, None) means lookup failed; will retry next time.
_t_bracket_cache: dict[str, tuple] = {}
_t_bracket_cache_lock = threading.Lock()

# Shared with the bot via start(rt)
_rt = None

# Telemetry
_stats = {
    "ws_callbacks_received": 0,
    "ws_evals_skipped_debounce": 0,
    "ws_evals_skipped_ask_unchanged": 0,
    "ws_evals_skipped_universe": 0,
    "ws_evals_attempted": 0,
    "wethr_polls": 0,
    "wethr_station_events": 0,
    "wethr_evals_attempted": 0,
    "evals_total": 0,
    "evals_nn_fired": 0,
    "evals_buy_decisions": 0,
    "evals_skip_decisions": 0,
    "evals_errors": 0,
    "started_ts": 0.0,
}
_stats_lock = threading.Lock()


def _bump(key: str, n: int = 1) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


def get_stats() -> dict:
    with _stats_lock:
        return dict(_stats)


# ─────────────────────────────────────────────────────────────────────────────
# Signal extraction (for post-hoc filter discovery)
# ─────────────────────────────────────────────────────────────────────────────
def _wethr_age_sec(pkt: dict) -> Optional[float]:
    """Seconds since latest wethr obs_ts. None if unavailable."""
    w = pkt.get("wethr_obs") or {}
    obs_ts = w.get("obs_ts")
    if obs_ts is None:
        return None
    try:
        return round(time.time() - float(obs_ts), 1)
    except (TypeError, ValueError):
        return None


def _signals_block(pkt: dict) -> dict:
    """Surface wethr + obs-trend + diurnal signals to the shadow log so we
    can analyze which combinations predict winners vs losers post-hoc.
    Pure-read — no decision-logic impact."""
    w = pkt.get("wethr_obs") or {}
    ctx = pkt.get("local_clock") or {}
    trend60 = pkt.get("obs_trend_60m_regression") or {}
    th_range = pkt.get("temp_history_range_60m") or {}
    return {
        "wethr_temp_f": w.get("temp_f"),
        "wethr_high_f": w.get("high_f"),
        "wethr_low_f": w.get("low_f"),
        "wethr_highest_probable_f": w.get("highest_probable_f"),
        "wethr_lowest_probable_f": w.get("lowest_probable_f"),
        "dew_point_f": w.get("dew_point_f"),
        "wind_speed_mph": w.get("wind_speed_mph"),
        "wind_gust_mph": w.get("wind_gust_mph"),
        "cloud_layer_count": w.get("cloud_layer_count"),
        "relative_humidity": w.get("relative_humidity"),
        "obs_trend_30m": pkt.get("obs_trend_30m"),
        "obs_60m_slope": trend60.get("slope_f_per_h"),
        "obs_60m_r2": trend60.get("r_squared"),
        "obs_60m_n_points": trend60.get("n_points"),
        "temp_history_range_60m_f": th_range.get("range_f"),
        "temp_history_n": th_range.get("n"),
        "local_hour": ctx.get("local_hour"),
        "peak_hour_local": ctx.get("peak_hour_local"),
        "min_hour_local": ctx.get("min_hour_local"),
        "h_to_peak": ctx.get("h_to_peak"),
        "h_to_min": ctx.get("h_to_min"),
        "past_peak_today": ctx.get("past_peak_today"),
        "past_min_today": ctx.get("past_min_today"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Per-station peak/min hour lookup (production pace_curves)
# ─────────────────────────────────────────────────────────────────────────────
_peak_table_cache: dict = {}     # station(K-prefix) -> {month_int: peak_hour_int}
_min_table_cache: dict = {}       # station(K-prefix) -> {month_int: min_hour_int}
# 2026-05-20: fractional peak source (5yr 10-day rolling P50 from
# heating_traces.sqlite). Loaded alongside int peaks; selected via
# config.USE_FRACTIONAL_PEAK_FOR_WINDOW.
_peak_table_frac_cache: dict = {}  # station(K-prefix) -> {"MM-DD": peak_hour_float}
_min_table_frac_cache: dict = {}   # station(K-prefix) -> {"MM-DD": min_hour_float}
_peak_table_loaded = False
_peak_table_lock = threading.Lock()

# Map K-prefixed station code → pace_curves series key (HIGH).
# Built from the actual ticker prefixes observed in pace_curves_v2.json.
_STATION_TO_HIGH_SERIES = {
    "KATL": "KXHIGHTATL", "KAUS": "KXHIGHAUS",   "KBOS": "KXHIGHTBOS",
    "KDCA": "KXHIGHTDC",  "KDEN": "KXHIGHDEN",   "KDFW": "KXHIGHTDAL",
    "KHOU": "KXHIGHTHOU", "KLAS": "KXHIGHTLV",   "KLAX": "KXHIGHLAX",
    "KMDW": "KXHIGHCHI",  "KMIA": "KXHIGHMIA",   "KMSP": "KXHIGHTMIN",
    "KMSY": "KXHIGHTNOLA","KNYC": "KXHIGHNY",    "KOKC": "KXHIGHTOKC",
    "KPHL": "KXHIGHPHIL", "KPHX": "KXHIGHTPHX",  "KSAT": "KXHIGHTSATX",
    "KSEA": "KXHIGHTSEA", "KSFO": "KXHIGHTSFO",
}
_STATION_TO_LOW_SERIES = {
    "KATL": "KXLOWTATL",  "KAUS": "KXLOWTAUS",   "KBOS": "KXLOWTBOS",
    "KDCA": "KXLOWTDC",   "KDEN": "KXLOWTDEN",   "KDFW": "KXLOWTDAL",
    "KHOU": "KXLOWTHOU",  "KLAS": "KXLOWTLV",    "KLAX": "KXLOWTLAX",
    "KMDW": "KXLOWTCHI",  "KMIA": "KXLOWTMIA",   "KMSP": "KXLOWTMIN",
    "KMSY": "KXLOWTNOLA", "KNYC": "KXLOWTNYC",   "KOKC": "KXLOWTOKC",
    "KPHL": "KXLOWTPHIL", "KPHX": "KXLOWTPHX",   "KSAT": "KXLOWTSATX",
    "KSEA": "KXLOWTSEA",  "KSFO": "KXLOWTSFO",
}


def _ensure_peak_tables_loaded() -> None:
    """Load pace_curves once at first call. Builds station→{month: hour}."""
    global _peak_table_loaded
    if _peak_table_loaded:
        return
    with _peak_table_lock:
        if _peak_table_loaded:
            return
        try:
            import config as _cfg
            high_path = getattr(_cfg, "PUSH_PACE_CURVES_HIGH_PATH",
                                "/home/ubuntu/data/pace_curves_v2.json")
            low_path = getattr(_cfg, "PUSH_PACE_CURVES_LOW_PATH",
                               "/home/ubuntu/data/pace_curves_low_v2.json")
            with open(high_path) as f:
                high = json.load(f)
            with open(low_path) as f:
                low = json.load(f)
            inverse_high = {v: k for k, v in _STATION_TO_HIGH_SERIES.items()}
            inverse_low = {v: k for k, v in _STATION_TO_LOW_SERIES.items()}
            for series_key, curve in (high.get("curves") or {}).items():
                st = inverse_high.get(series_key)
                if not st: continue
                monthly = curve.get("monthly") or {}
                row = {}
                for m_str, m_data in monthly.items():
                    try: m_int = int(m_str)
                    except: continue
                    ph = m_data.get("empirical_peak_hour_local")
                    if ph is not None:
                        row[m_int] = int(ph)
                if row:
                    _peak_table_cache[st] = row
            for series_key, curve in (low.get("curves") or {}).items():
                st = inverse_low.get(series_key)
                if not st: continue
                monthly = curve.get("monthly") or {}
                row = {}
                for m_str, m_data in monthly.items():
                    try: m_int = int(m_str)
                    except: continue
                    mh = m_data.get("empirical_min_hour_local")
                    if mh is not None:
                        row[m_int] = int(mh)
                if row:
                    _min_table_cache[st] = row
            # 2026-05-20: also load fractional peak table (5yr 10-day rolling
            # P50 from heating_traces). Falls back gracefully if file missing.
            try:
                frac_path = getattr(_cfg, "PUSH_PEAK_FRACTIONAL_PATH",
                                    "/home/ubuntu/data/peak_fractional_5yr_10day.json")
                with open(frac_path) as f:
                    frac_data = json.load(f).get("peaks", {})
                n_h, n_l = 0, 0
                for k, v in frac_data.items():
                    parts = k.split("|")
                    if len(parts) != 3: continue
                    K_st, side, md = parts
                    try: fv = float(v)
                    except (TypeError, ValueError): continue
                    if side == "HIGH":
                        _peak_table_frac_cache.setdefault(K_st, {})[md] = fv
                        n_h += 1
                    elif side == "LOW":
                        _min_table_frac_cache.setdefault(K_st, {})[md] = fv
                        n_l += 1
                log.info("fractional peak table loaded: HIGH=%d cells across %d stations, "
                         "LOW=%d cells across %d stations",
                         n_h, len(_peak_table_frac_cache),
                         n_l, len(_min_table_frac_cache))
            except FileNotFoundError:
                log.warning("fractional peak table not found, falling back to int")
            except Exception as e:
                log.exception("failed to load fractional peak table: %s", e)
            _peak_table_loaded = True
            log.info("peak/min hour tables loaded: HIGH=%d stations LOW=%d stations",
                     len(_peak_table_cache), len(_min_table_cache))
        except Exception as e:
            log.exception("failed to load pace_curves: %s", e)


def _lookup_peak_hour(station: str, series: str, climate_day: str) -> Optional[float]:
    """Return empirical peak/min hour LST for (station, series, climate-day).

    series ∈ {'HIGH','LOW'}. Returns None on lookup failure.

    If config.USE_FRACTIONAL_PEAK_FOR_WINDOW is True, returns the 5yr 10-day
    rolling fractional P50 from heating_traces (e.g., 15.62). Otherwise
    returns the legacy int from pace_curves (e.g., 15). On fractional miss
    (e.g., no data for this specific (station, side, mm-dd)), falls back
    to the int value to preserve old behavior.
    """
    _ensure_peak_tables_loaded()
    try:
        parts = climate_day.split("-")
        month = int(parts[1])
        day = int(parts[2])
        md_key = f"{month:02d}-{day:02d}"
    except Exception:
        return None

    # Try fractional first when flag is on
    try:
        import config as _cfg
        if getattr(_cfg, "USE_FRACTIONAL_PEAK_FOR_WINDOW", False):
            frac_table = (_peak_table_frac_cache if series == "HIGH"
                          else _min_table_frac_cache)
            frac_row = frac_table.get(station) or {}
            frac_val = frac_row.get(md_key)
            if frac_val is not None:
                return frac_val
    except Exception:
        pass

    # Fallback to int (legacy)
    table = _peak_table_cache if series == "HIGH" else _min_table_cache
    row = table.get(station) or {}
    return row.get(month)


# ─────────────────────────────────────────────────────────────────────────────
# Per-station decision-window check (auto-execute gate)
# ─────────────────────────────────────────────────────────────────────────────
def _in_decision_window(station: str, series: str, local_hour: float,
                        climate_day: str) -> tuple[bool, str]:
    """Return (in_window, debug_str). Window is [peak_hr − BEFORE, peak_hr + AFTER]
    using the empirical per-(station, month) peak/min hour from pace_curves.

    BEFORE/AFTER selection:
      1. If config.USE_PUSH_WINDOW_OVERRIDES is True AND (station, series, month)
         is present in push_window_overrides.PUSH_WINDOW_OVERRIDES → use that.
      2. Otherwise fall back to global PUSH_PEAK_HOURS_BEFORE / AFTER_<HIGH|LOW>.
    """
    if local_hour is None:
        return False, "no_local_hour"
    peak = _lookup_peak_hour(station, series, climate_day)
    if peak is None:
        return False, f"no_peak_for_{station}_{series}_{climate_day}"

    before: Optional[float] = None
    after: Optional[float] = None
    src = "global"
    try:
        import config as _cfg
        if getattr(_cfg, "USE_PUSH_WINDOW_OVERRIDES", False):
            try:
                from push_window_overrides import PUSH_WINDOW_OVERRIDES
            except ImportError:
                PUSH_WINDOW_OVERRIDES = {}
            try:
                month = int(climate_day.split("-")[1])
            except Exception:
                month = None
            if month is not None:
                ov = PUSH_WINDOW_OVERRIDES.get((station, series, month))
                if ov is not None:
                    before, after = float(ov[0]), float(ov[1])
                    src = "override"
        if before is None:
            before = float(getattr(_cfg, "PUSH_PEAK_HOURS_BEFORE", 2.5))
            if series == "HIGH":
                after = float(getattr(_cfg, "PUSH_PEAK_HOURS_AFTER_HIGH",
                                      getattr(_cfg, "PUSH_PEAK_HOURS_AFTER", 1.5)))
            else:
                after = float(getattr(_cfg, "PUSH_PEAK_HOURS_AFTER_LOW", 0.5))
    except Exception:
        before = 2.5
        after = 1.5 if series == "HIGH" else 0.5
        src = "fallback"

    lo = peak - before
    hi = peak + after
    ok = (lo <= local_hour <= hi)
    return ok, f"peak={peak} window=[{lo:.1f},{hi:.1f}] cur={local_hour:.2f} src={src}"


# ─────────────────────────────────────────────────────────────────────────────
# Auto-execute via real Kalshi order (push pure-code architecture)
# ─────────────────────────────────────────────────────────────────────────────
def _try_auto_execute(cand, packet: dict, decision: dict,
                      series: str, local_hour: float) -> tuple[bool, str]:
    """Place a real Kalshi order for a pure-nn decision.

    Returns (executed, reason). Safety checks (push pure-code arch 2026-05-19):
      1. Direction-specific toggle ON (AUTO_EXECUTE_BUY_<NO|YES>_PUSH)
      2. decision.edge >= PUSH_MIN_EDGE_PP/100 (raised from pure_nn_decide's
         shadow-log floor of 6pp to filter marginal-edge bottom-tail trades).
      3. (station, series, hour) inside [peak-PUSH_PEAK_HOURS_BEFORE,
         peak+PUSH_PEAK_HOURS_AFTER] using empirical per-(station, month)
         peak/min hour from pace_curves.
      4. Entry ask is within [PUSH_MIN_ENTRY_C, PUSH_MAX_ENTRY_C].
      5. No existing position on this exact ticker.
      6. Open position count for (station, series_prefix, direction)
         below PUSH_MAX_TICKERS_PER_STATION_SIDE_DIRECTION.
      7. Wallet has cash for min_buy.
      8. Series correlation cap not exceeded.

    Reuses paper_judge_bot.execute_buy() so all the freshness/drift/dust
    safeguards apply identically to LLM-driven trades.
    """
    import config as _cfg
    direction = decision.get("decision", "")  # "BUY_NO" or "BUY_YES"
    if direction not in ("BUY_NO", "BUY_YES"):
        return False, "not_a_buy"
    short_dir = "NO" if direction == "BUY_NO" else "YES"
    # (Gate 2) Edge floor — bot only fires above PUSH_MIN_EDGE_PP. The
    # nn_shadow_strategy.pure_nn_decide internal floor stays at 6pp so the
    # shadow log keeps logging marginal-edge candidates for diagnostics.
    min_edge_pp = int(getattr(_cfg, "PUSH_MIN_EDGE_PP", 12))
    edge_val = decision.get("edge")
    if edge_val is None:
        return False, "no_edge"
    edge_pp = edge_val * 100.0
    # (2t) In-bracket tail-bet gate. When mu sits INSIDE the YES window but the
    # bot picks the smaller-mass (tail) side, the bet is "I think it lands in
    # the bracket, but I'll bet it doesn't" -- a wager against our own central
    # estimate that depends entirely on sigma being calibrated in the tails
    # (the most fragile assumption). Demand a larger edge before firing.
    # 2026-05-20: 5/19+5/20 settled pool, 4 blocks, 4 losers, 0 winners,
    # +$13.87 net. Mechanism-clean: betting against your own mean for a thin
    # edge has no winning regime. Boundary-gap sibling (Gate 1) PARKED -- it
    # killed real winners. Set PUSH_TAIL_BET_MIN_EDGE_PP=0 to disable.
    tail_min_edge_pp = int(getattr(_cfg, "PUSH_TAIL_BET_MIN_EDGE_PP", 25))
    effective_min_edge_pp = min_edge_pp
    tail_reason = None
    if tail_min_edge_pp > 0:
        _mu = packet.get("mu_chosen")
        _fl = packet.get("floor")
        _cp = packet.get("cap")
        # YES window [ylo, yhi) per bracket shape, mirroring
        # nn_shadow_strategy._yes_window: B = [floor-0.5, cap+0.5);
        # T-warm (floor only) = [floor+0.5, +inf); T-cold (cap only) =
        # (-inf, cap-0.5). 2026-05-20: extended from B-only to T after
        # HOU T84 BUY_NO (mu=83.0 favored the YES tail-cold region, bot bet
        # the NO tail, p_chosen=0.41) slipped past the B-only gate and lost
        # -$5.16.
        _ylo = _yhi = None
        try:
            if _fl is not None and _cp is not None:
                _ylo, _yhi = float(_fl) - 0.5, float(_cp) + 0.5
            elif _fl is not None:
                _ylo, _yhi = float(_fl) + 0.5, float("inf")
            elif _cp is not None:
                _ylo, _yhi = float("-inf"), float(_cp) - 0.5
        except (TypeError, ValueError):
            _ylo = _yhi = None
        if _mu is not None and _ylo is not None:
            try:
                _muf = float(_mu)
                _mu_in_yes = (_ylo <= _muf < _yhi)
            except (TypeError, ValueError):
                _mu_in_yes = False
            if _mu_in_yes:
                _p_yes = decision.get("p_yes")
                if _p_yes is not None:
                    try:
                        _pf = float(_p_yes)
                        _p_chosen = _pf if direction == "BUY_YES" else (1.0 - _pf)
                        if _p_chosen < 0.5:
                            effective_min_edge_pp = max(min_edge_pp, tail_min_edge_pp)
                            tail_reason = f"tail_bet mu_in_YES p_chosen={_p_chosen:.2f}"
                    except (TypeError, ValueError):
                        pass
    if edge_pp < effective_min_edge_pp:
        detail = f" ({tail_reason})" if tail_reason else ""
        return False, f"edge_below_floor {edge_pp:.1f}pp < {effective_min_edge_pp}pp{detail}"
    toggle_attr = f"AUTO_EXECUTE_BUY_{short_dir}_PUSH"
    if not getattr(_cfg, toggle_attr, False):
        return False, f"{toggle_attr}=False"
    # (2) Decision window — peak-relative per (station, month, series)
    in_win, win_dbg = _in_decision_window(cand.station, series, local_hour, cand.climate_day)
    if not in_win:
        return False, f"outside_window {cand.station}/{series}/{short_dir}: {win_dbg}"
    # (2a) HIGH-only: block at-or-past-peak entries. At peak, rm has converged
    # on the day's true max, leaving no headroom for the nn_match mu to add
    # real signal -- instead it over-extrapolates and flips adjacent brackets
    # the wrong way. 2026-05-20: 3 today losses -$13.07 at h_to_peak<0.5.
    if series == "HIGH":
        h2pk = (packet.get("local_clock") or {}).get("h_to_peak")
        min_h2pk_raw = getattr(_cfg, "PUSH_MIN_H_TO_PEAK_HIGH", 0.5)
        if h2pk is not None and min_h2pk_raw is not None:
            min_h2pk = float(min_h2pk_raw)
            if h2pk < min_h2pk:
                return False, f"h2pk_too_low {h2pk:.2f}<{min_h2pk}"
    if _rt is None:
        return False, "rt_not_initialized"
    # (2b) Tier 1 runtime gates — physics-catastrophic regimes the matcher
    # cannot represent (dense fog / heavy precip kill the diurnal cycle;
    # extreme wind = tropical or severe storm). Thresholds in config.
    # Visibility doubles as a precip proxy (no precip_in_h field in wethr yet).
    wo = packet.get("wethr_obs") or {}
    min_vsby = float(getattr(_cfg, "PUSH_MIN_VSBY_MI", 0.5))
    if min_vsby > 0:
        vsby = wo.get("visibility_miles")
        if vsby is None:
            vsby = wo.get("visibility")
        try:
            if vsby is not None and float(vsby) < min_vsby:
                return False, f"tier1_vsby {float(vsby):.2f}mi < {min_vsby}mi"
        except (TypeError, ValueError):
            pass
    max_wind = float(getattr(_cfg, "PUSH_MAX_WIND_MPH", 40.0))
    if max_wind > 0:
        for fld in ("wind_speed_mph", "wind_gust_mph"):
            v = wo.get(fld)
            try:
                if v is not None and float(v) > max_wind:
                    return False, f"tier1_wind {fld}={float(v):.1f}mph > {max_wind}mph"
            except (TypeError, ValueError):
                pass
    # (3) Price floor/ceiling — entry must be in [min_c, max_c]
    # 2026-05-19 v3: BUY_YES gets a higher floor (cheap-YES lottery trap).
    max_c = int(getattr(_cfg, "PUSH_MAX_ENTRY_C", 90))
    if direction == "BUY_YES":
        min_c = int(getattr(_cfg, "PUSH_MIN_ENTRY_C_BUY_YES",
                            getattr(_cfg, "PUSH_MIN_ENTRY_C", 25)))
    else:
        min_c = int(getattr(_cfg, "PUSH_MIN_ENTRY_C", 10))
    ask_c = packet.get("yes_ask_c") if direction == "BUY_YES" else packet.get("no_ask_c")
    if ask_c is None:
        return False, f"no_ask_for_{direction}"
    try:
        ask_c_i = int(ask_c)
    except (TypeError, ValueError):
        return False, f"bad_ask_{direction}={ask_c}"
    if ask_c_i < min_c or ask_c_i > max_c:
        return False, f"price_oor ask={ask_c_i}c not in [{min_c},{max_c}]"
    # (4) Position dedup — never add to existing position on this exact ticker
    try:
        pos = _rt.positions.get(cand.ticker) if hasattr(_rt, "positions") else None
        if pos and float(pos.get("cost", 0)) > 0:
            return False, f"already_held_cost_${float(pos.get('cost', 0)):.2f}"
    except Exception:
        pass
    # (5) Position cap per (station, series_prefix, direction)
    cap_per_dir = int(getattr(_cfg, "PUSH_MAX_TICKERS_PER_STATION_SIDE_DIRECTION", 1))
    n_existing = 0
    try:
        if hasattr(_rt, "positions"):
            series_prefix = cand.series_prefix  # "KXHIGH" or "KXLOW"
            for tk, p in (_rt.positions or {}).items():
                if not isinstance(p, dict):
                    continue
                try:
                    if float(p.get("cost", 0)) <= 0:
                        continue
                except (TypeError, ValueError):
                    continue
                if p.get("station") != cand.station:
                    continue
                if not str(tk).startswith(series_prefix):
                    continue
                if p.get("action") != direction:
                    continue
                # 2026-05-20: scope cap to candidate's climate_day so a stuck
                # prior-day position (e.g. KMSY 5/19 Kalshi-pending settlement)
                # doesn't block today's BUYs on the same station+series.
                pos_date = p.get("date_str") or p.get("climate_day")
                if pos_date and pos_date != cand.climate_day:
                    continue
                n_existing += 1
    except Exception:
        pass
    if n_existing >= cap_per_dir:
        return False, (f"position_cap {direction}@{cand.station}/{cand.series_prefix}: "
                       f"{n_existing}>={cap_per_dir}")
    # (6) Cash check
    try:
        import kalshi_client as _kc
        balance = _kc.get_balance_cached()
        min_buy = float(getattr(_cfg, "MIN_BUY_USD", 1.0))
        if balance is not None and balance < min_buy:
            return False, f"low_cash_${balance:.2f}<${min_buy:.2f}"
    except Exception:
        pass
    # (7) Correlation cap (mirror LLM-path)
    side_label = "HIGH" if cand.series_prefix == "KXHIGH" else "LOW"
    cap_key = (cand.station, side_label, cand.climate_day)
    try:
        cap = _cfg.GUARDRAILS.get("max_buys_per_station_side", 999)
        cycle_buys = getattr(_rt, "cycle_buys_by_station_side", {}).get(cap_key, 0)
        if cycle_buys >= cap:
            return False, f"correlation_cap {side_label}@{cand.station}"
    except Exception:
        pass
    # Pre-populate packet._edge_info so execute_buy's _claude_prob_for_side
    # can extract the prob. Our decision.read = "pure-nn auto: ..." has no
    # P(NO)/P(YES) literal, so its regex returns None and code falls back
    # to packet._edge_info. (Bug observed 2026-05-19: 11 push BUYs hit
    # "no_prob_signal" rejection because _edge_info wasn't set.)
    p_yes_raw = decision.get("p_yes")
    if p_yes_raw is not None:
        try:
            p_yes_f = float(p_yes_raw)
            if direction == "BUY_YES":
                packet["_edge_info"] = {
                    "side": "BUY_YES",
                    "prob": p_yes_f,
                    "mu_method": "nn_match_push",
                }
            else:  # BUY_NO
                packet["_edge_info"] = {
                    "side": "BUY_NO",
                    "prob": 1.0 - p_yes_f,
                    "mu_method": "nn_match_push",
                }
        except (TypeError, ValueError):
            pass
    # Construct EntryDecision and execute via the main bot's path
    try:
        import judgment
        edge = decision.get("edge") or 0.0
        size_factor = min(1.0, max(0.30, edge / 0.20))
        entry_dec = judgment.EntryDecision(
            decision=direction,
            conviction=0.85,
            size_factor=size_factor,
            read=f"pure-nn auto: {(decision.get('reason') or '')[:200]}",
            key_risks=["pure-nn auto-exec, no LLM review"],
            what_would_change_my_mind=("rm crosses bracket boundary OR wethr probable updates "
                                       "outside current bracket"),
            obs_anchor="",
            obs_anchor_valid=False,
            obs_anchor_reason="pure-nn push auto-execute",
            parse_ok=True,
            parse_error=None,
        )
        import paper_judge_bot as _pjb
        _pjb.execute_buy(_rt, cand, packet, entry_dec)
        return True, (f"executed {direction} edge={edge*100:.1f}pp ask={ask_c_i}c "
                      f"win={win_dbg}")
    except Exception as e:
        log.exception("auto-execute crashed for %s: %s", cand.ticker, e)
        return False, f"exception: {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Log writer
# ─────────────────────────────────────────────────────────────────────────────
def _log_shadow(rec: dict) -> None:
    """Append a shadow record (one JSON per line). Thread-safe."""
    try:
        SHADOW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _log_writer_lock:
            with open(SHADOW_LOG_PATH, "a") as f:
                f.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:
        log.exception("shadow log write failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Per-ticker mutex + debounce
# ─────────────────────────────────────────────────────────────────────────────
def _get_ticker_lock(ticker: str) -> threading.Lock:
    with _per_ticker_locks_lock:
        lk = _per_ticker_locks.get(ticker)
        if lk is None:
            lk = threading.Lock()
            _per_ticker_locks[ticker] = lk
        return lk


def _debounce_ok(ticker: str) -> bool:
    """True if enough time has elapsed since the last eval of this ticker.
    Updates the timestamp atomically when returning True."""
    now = time.time()
    with _last_eval_lock:
        last = _last_eval_ts.get(ticker, 0.0)
        if now - last < DEBOUNCE_SEC:
            return False
        _last_eval_ts[ticker] = now
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Lightweight packet builder for shadow eval
# ─────────────────────────────────────────────────────────────────────────────
def _patch_t_bracket(cand: market_universe.Candidate) -> bool:
    """For T-bracket candidates with no floor/cap (parse_ticker leaves them
    None), look up the Kalshi market and set the appropriate side from
    strike_type + floor_strike/cap_strike. Caches result indefinitely
    per-ticker. Returns True on success (cand mutated in place); False if
    we can't determine the geometry yet."""
    if cand.bracket_kind != "T":
        return True  # not a T-bracket; nothing to patch
    if cand.floor is not None or cand.cap is not None:
        return True  # already patched
    with _t_bracket_cache_lock:
        cached = _t_bracket_cache.get(cand.ticker)
    if cached is not None:
        floor, cap = cached
        # Only cache successful lookups; failures fall through and retry.
        if floor is not None or cap is not None:
            cand.floor = floor
            cand.cap = cap
            return True
    # Cache miss OR previous failure — fetch from Kalshi.
    try:
        m = kalshi_client.get_market(cand.ticker)
    except Exception as e:
        log.debug("t-bracket lookup %s failed: %s", cand.ticker, e)
        m = None
    floor = cap = None
    if m:
        fc = m.get("floor_strike")
        cc = m.get("cap_strike")
        st = (m.get("strike_type") or "").lower()
        try:
            fc_f = float(fc) if fc is not None else None
        except (TypeError, ValueError):
            fc_f = None
        try:
            cc_f = float(cc) if cc is not None else None
        except (TypeError, ValueError):
            cc_f = None
        if st == "greater":
            floor = fc_f
        elif st == "less":
            cap = cc_f
        else:
            # Trust whichever field Kalshi populated.
            if fc_f is not None and cc_f is None:
                floor = fc_f
            elif cc_f is not None and fc_f is None:
                cap = cc_f
    if floor is None and cap is None:
        # Don't cache failures — retry on next event (Kalshi market may
        # have come online, or this may be a transient lookup error).
        return False
    with _t_bracket_cache_lock:
        _t_bracket_cache[cand.ticker] = (floor, cap)
    cand.floor = floor
    cand.cap = cap
    return True


def _build_shadow_packet(cand: market_universe.Candidate) -> Optional[dict]:
    """Construct the minimal packet shape pure_nn_decide + nn_shadow need.
    Returns None if essential data is missing."""
    # Market: BBO from kalshi_ws cache
    bbo = kalshi_ws.get_bbo(cand.ticker)
    if not bbo:
        return None
    yes_ask_c = int(round(bbo["yes_ask"] * 100))
    yes_bid_c = int(round(bbo["yes_bid"] * 100))
    no_ask_c = 100 - yes_bid_c if yes_bid_c > 0 else None
    no_bid_c = 100 - yes_ask_c if yes_ask_c > 0 else None
    if yes_ask_c <= 0 and (no_ask_c is None or no_ask_c <= 0):
        return None
    spread_c = max((yes_ask_c - yes_bid_c) if yes_bid_c else 100,
                   (no_ask_c - no_bid_c) if (no_bid_c is not None and no_ask_c is not None) else 100)

    # Wethr live obs
    wethr = shared_cache_reader._wethr_station_entry(cand.station) or {}
    if not wethr:
        return None
    age_sec = None
    obs_ts = wethr.get("obs_ts")
    if obs_ts is not None:
        try:
            age_sec = time.time() - float(obs_ts)
        except (TypeError, ValueError):
            pass

    # Hourly obs trajectory (matches what build_entry_packet would supply
    # for nn_shadow's temp_history augmentation).
    temp_hist = shared_cache_reader.get_temp_history(cand.station, lookback_sec=3600.0)
    hourly_obs_today = wethr.get("hourly_obs_today") or []
    rm_age_max = shared_cache_reader.get_rm_age_sec(cand.station, "high")
    rm_age_min = shared_cache_reader.get_rm_age_sec(cand.station, "low")
    th_range = shared_cache_reader.temp_history_range_60m(cand.station)
    trend60 = shared_cache_reader.compute_trend_60m_regression(cand.station)
    trend30 = shared_cache_reader.compute_trend_30m(cand.station)

    # Local clock — series-relevant extreme. local_clock_context() expects
    # a UTC unix timestamp (not a date string); previous call passed
    # cand.climate_day, which silently failed and left ctx empty.
    try:
        ctx = climate_normals.local_clock_context(cand.station, time.time()) or {}
    except Exception:
        ctx = {}

    # rm choice — high series uses high_f, low series uses low_f
    is_high = cand.series_prefix == "KXHIGH"
    rm = wethr.get("high_f") if is_high else wethr.get("low_f")

    # F1 (2026-05-20): validate rm freshness against Kalshi LST climate-day boundary.
    # Kalshi market close_time confirms LST midnight per market (e.g.,
    # KXLOWTAUS-26MAY20 close_time=2026-05-21T06:00Z = LST midnight ending 5/20).
    # wethr_rm.lst_midnight_utc_ts encodes the same. Without this guard, a stale
    # 5/19-evening rm reading was used as a 5/20 anchor (KAUS B67.5 BUY_NO loss).
    # paper_judge_bot.py:458,753 already does this for the LLM/exit paths; push was
    # missing. Use per-side LST date (date_low/date_high derived by wethr-cache-service
    # from time_of_*_utc; legacy 'date' field lags 1-2d behind CLI ingest).
    if rm is not None and bool(getattr(_cfg, "PUSH_VALIDATE_RM_CLIMATE_DAY", True)):
        _cache_date = (wethr.get("date_high") if is_high else wethr.get("date_low")) or wethr.get("date")
        _time_of_ext = wethr.get("time_of_high_utc") if is_high else wethr.get("time_of_low_utc")
        _grace = float(getattr(_cfg, "PUSH_RM_GRACE_SEC_HIGH" if is_high else "PUSH_RM_GRACE_SEC_LOW",
                                3600.0 if is_high else 900.0))
        try:
            _rmv = wethr_rm.validate_rm_for_climate_day(
                station=cand.station,
                climate_day=cand.climate_day,
                cache_date=_cache_date,
                time_of_extreme_utc=_time_of_ext,
                now_utc_ts=time.time(),
                grace_sec=_grace,
            )
        except Exception:
            log.exception("push: F1 validator raised for %s %s", cand.station, cand.ticker)
            _rmv = {"ok": False, "reason": "validator_exception"}
        if not _rmv.get("ok"):
            log.warning(
                "push: nulling stale rm for %s %s side=%s reason=%s cache_date=%s climate_day=%s rm=%s",
                cand.station, cand.ticker, ("high" if is_high else "low"),
                _rmv.get("reason"), _cache_date, cand.climate_day, rm,
            )
            rm = None

    # Compute seconds_to_close (UTC seconds until LST midnight close) so
    # execute_buy's guardrails.check_buy time-to-close gate doesn't reject
    # with "0.0min < 30min". Without this field, packet.get("seconds_to_close")
    # returns None → "or 0" → guardrails sees 0 seconds remaining.
    try:
        import paper_judge_bot as _pjb_for_close
        close_ts = _pjb_for_close.lst_close_ts(cand.station, cand.climate_day)
        secs_to_close = (close_ts - time.time()) if close_ts else None
    except Exception:
        secs_to_close = None

    pkt: dict = {
        "ticker": cand.ticker,
        "label": str(cand.bracket_label),
        "station": cand.station,
        "series": cand.series_prefix,
        "climate_day": cand.climate_day,
        "floor": cand.floor,
        "cap": cand.cap,
        "bracket_kind": cand.bracket_kind,
        "days_out": 0,  # event-driven shadow only operates on live d+0
        "yes_bid_c": yes_bid_c,
        "yes_ask_c": yes_ask_c,
        "no_bid_c": no_bid_c,
        "no_ask_c": no_ask_c,
        "spread_c": spread_c,
        "wethr_obs": wethr,
        "obs_trend_30m": trend30,
        "obs_trend_60m_regression": trend60 or {},
        "temp_history_range_60m": th_range or {},
        "rm_age_max_sec": rm_age_max,
        "rm_age_min_sec": rm_age_min,
        "running_min_or_max": rm,
        "hourly_obs_today": hourly_obs_today,
        "local_clock": ctx,
        "seconds_to_close": secs_to_close,
    }
    return pkt


# ─────────────────────────────────────────────────────────────────────────────
# Single-ticker evaluation (the shared path used by both triggers)
# ─────────────────────────────────────────────────────────────────────────────
def _evaluate_ticker(ticker: str, trigger: str) -> None:
    """Build packet → run nn_shadow → run pure_nn_decide → log. Idempotent
    under the per-ticker lock + debounce."""
    # Parse ticker → candidate. Skip non-weather tickers (kalshi WS may
    # carry tickers we didn't subscribe to in some edge cases).
    cand = market_universe.parse_ticker(ticker)
    if cand is None or cand.series_prefix not in ("KXHIGH", "KXLOW"):
        _bump("ws_evals_skipped_universe")
        return
    # T-brackets need a Kalshi market lookup to patch floor/cap. Cached.
    if not _patch_t_bracket(cand):
        _bump("ws_evals_skipped_universe")
        return

    if not _debounce_ok(ticker):
        _bump("ws_evals_skipped_debounce")
        return

    lk = _get_ticker_lock(ticker)
    if not lk.acquire(blocking=False):
        # Another thread is already evaluating this ticker — skip.
        return
    try:
        _bump("evals_total")
        pkt = _build_shadow_packet(cand)
        if pkt is None:
            return

        # Run nn_match via the shadow adapter (handles trajectory build,
        # rm-anchor data, side gating, etc).
        nn_res = nn_shadow.shadow_nn_proj(pkt)
        if nn_res is None:
            # Log a "no nn signal" record for accounting
            _log_shadow({
                "ts": time.time(),
                "trigger": trigger,
                "ticker": ticker,
                "station": cand.station,
                "climate_day": cand.climate_day,
                "bracket": {"floor": cand.floor, "cap": cand.cap, "kind": cand.bracket_kind},
                "market": {"yes_ask_c": pkt.get("yes_ask_c"), "no_ask_c": pkt.get("no_ask_c"),
                           "spread_c": pkt.get("spread_c")},
                "nn_fired": False,
                "rm": pkt.get("running_min_or_max"),
                "signals": _signals_block(pkt),
                "decision": "SKIP",
                "reason": "nn_match did not fire (no projection)",
            })
            return

        _bump("evals_nn_fired")
        pkt["mu_method"] = (f"nn_match_{nn_res.get('side', '')}_n{nn_res.get('n_neighbors')}"
                            + ("_locked" if nn_res.get("extreme_locked") else ""))
        pkt["mu_chosen"] = nn_res["mu"]
        pkt["sigma_chosen"] = nn_res["sigma"]

        # Per-series bet cap — single source of truth is the GUARDRAILS dict
        # (guardrails.check_buy enforces the same numbers downstream; sizing
        # here must match or guardrails would REJECT an over-cap bet outright).
        # 2026-05-20: HIGH raised to $15 (max_bet_high_series_usd).
        # 2026-05-21: LOW cut to $1 (max_bet_low_series_usd) — losing book.
        _gr = getattr(_cfg, "GUARDRAILS", {}) or {}
        _is_high_sizing = (cand.series_prefix == "KXHIGH")
        _series_cap_usd = float(_gr.get("max_bet_high_series_usd", 5.0)) if _is_high_sizing \
            else float(_gr.get("max_bet_low_series_usd", 5.0))
        # Min-buy floor: LOW uses a smaller floor so a $1 cap doesn't collapse
        # the integer-contract math (min_buy == cap => nothing fits). HIGH keeps
        # the standard $1 floor (its $15 cap never binds on min-buy anyway).
        _min_buy_usd = float(getattr(_cfg, "PUSH_MIN_BUY_USD_LOW", 0.40)) if not _is_high_sizing \
            else float(_gr.get("min_buy_usd", 1.0))
        ticker_remaining = _series_cap_usd
        if _rt is not None:
            try:
                pos = _rt.positions.get(ticker) if hasattr(_rt, "positions") else None
                if pos:
                    existing_cost = float(pos.get("cost", 0))
                    ticker_remaining = max(0.0, _series_cap_usd - existing_cost)
            except Exception:
                pass

        # 2026-05-18 (Chris): shadow logs EVERY positive-edge candidate, no
        # 25pp ceiling. Whole point of the shadow is to figure out which
        # filters add value post-hoc. We still record rm_locked status so
        # the analysis can replay any ceiling rule. edge_max=1.0 disables
        # the in-function ceiling without changing pure_nn_decide.
        decision = nn_shadow_strategy.pure_nn_decide(
            pkt, ticker_remaining_usd=ticker_remaining, edge_max=1.0,
            min_buy_usd=_min_buy_usd,
            series_cap_high_usd=float(_gr.get("max_bet_high_series_usd", 5.0)),
            series_cap_low_usd=float(_gr.get("max_bet_low_series_usd", 5.0)),
        )

        if decision["decision"] in ("BUY_YES", "BUY_NO"):
            _bump("evals_buy_decisions")
        else:
            _bump("evals_skip_decisions")

        # 2026-05-19: push pure-code auto-execute. If decision is a BUY,
        # check direction-specific toggle + per-station decision window.
        # In-window BUYs become real Kalshi orders. Outside-window or
        # toggled-off → shadow log only (no LLM fallback).
        executed = False
        executed_reason = "not_attempted"
        local_clk = pkt.get("local_clock") or {}
        local_hr = local_clk.get("local_hour")
        series_label = "HIGH" if cand.series_prefix == "KXHIGH" else "LOW"
        if decision["decision"] in ("BUY_NO", "BUY_YES"):
            executed, executed_reason = _try_auto_execute(
                cand, pkt, decision, series_label, local_hr,
            )
            if executed:
                _bump(f"auto_exec_{decision['decision'].lower()}")
            else:
                _bump(f"auto_exec_skipped_{decision['decision'].lower()}")

        _log_shadow({
            "ts": time.time(),
            "trigger": trigger,
            "ticker": ticker,
            "station": cand.station,
            "climate_day": cand.climate_day,
            "bracket": {"floor": cand.floor, "cap": cand.cap, "kind": cand.bracket_kind},
            "market": {"yes_ask_c": pkt.get("yes_ask_c"), "no_ask_c": pkt.get("no_ask_c"),
                       "spread_c": pkt.get("spread_c")},
            "nn_fired": True,
            "nn": {
                "mu_method": pkt["mu_method"],
                "mu_chosen": round(pkt["mu_chosen"], 3),
                "sigma_chosen": round(pkt["sigma_chosen"], 3),
                "n_neighbors": nn_res.get("n_neighbors"),
                "pool_size": nn_res.get("pool_size"),
                "extreme_locked": nn_res.get("extreme_locked"),
                "sigma_raw": nn_res.get("sigma_raw"),
                "sigma_factor": nn_res.get("sigma_factor"),
                "bias_correction": nn_res.get("bias_correction"),
            },
            "rm": pkt.get("running_min_or_max"),
            "rm_age_sec": (pkt.get("rm_age_max_sec") if cand.series_prefix == "KXHIGH"
                           else pkt.get("rm_age_min_sec")),
            "wethr_age_sec": _wethr_age_sec(pkt),
            # 2026-05-19: obs/wethr signals for post-hoc BUY_YES filter discovery.
            # Pure additive — pure_nn_decide does NOT use these yet.
            "signals": _signals_block(pkt),
            "decision": decision["decision"],
            "side": decision["side"],
            "edge_pp": round(decision["edge"] * 100, 2) if decision.get("edge") is not None else None,
            "p_yes": round(decision["p_yes"], 4) if decision.get("p_yes") is not None else None,
            "qty": decision.get("qty"),
            "price_c": decision.get("price_c"),
            "size_usd": decision.get("size_usd"),
            "rm_locked": decision.get("rm_locked"),
            "reason": decision.get("reason"),
            # 2026-05-19: auto-execute outcome (push pure-code path)
            "auto_exec_attempted": decision["decision"] in ("BUY_NO", "BUY_YES"),
            "auto_exec_executed": executed,
            "auto_exec_reason": executed_reason,
        })
    except Exception as e:
        _bump("evals_errors")
        log.exception("shadow eval failed for %s (trigger=%s): %s", ticker, trigger, e)
    finally:
        lk.release()


# ─────────────────────────────────────────────────────────────────────────────
# WS BBO callback
# ─────────────────────────────────────────────────────────────────────────────
def _on_bbo_change(ticker: str, prev: Optional[dict], new: dict) -> None:
    """Fired by kalshi_ws on every BBO recompute. Only acts on real ask
    movements; ignores bid-only changes and unchanged-ask flutter."""
    _bump("ws_callbacks_received")
    if not _started or _stop_event.is_set():
        return
    # Ignore if the ask side didn't move materially
    if prev is not None:
        try:
            old_yes_ask_c = int(round(prev.get("yes_ask", 0) * 100))
            new_yes_ask_c = int(round(new.get("yes_ask", 0) * 100))
            old_yes_bid_c = int(round(prev.get("yes_bid", 0) * 100))
            new_yes_bid_c = int(round(new.get("yes_bid", 0) * 100))
            if (abs(new_yes_ask_c - old_yes_ask_c) < ASK_CHANGE_MIN_C
                    and abs(new_yes_bid_c - old_yes_bid_c) < ASK_CHANGE_MIN_C):
                _bump("ws_evals_skipped_ask_unchanged")
                return
        except Exception:
            pass
    _bump("ws_evals_attempted")
    _evaluate_ticker(ticker, trigger="ws_bbo")


# ─────────────────────────────────────────────────────────────────────────────
# wethr filewatch thread
# ─────────────────────────────────────────────────────────────────────────────
def _wethr_poll_loop() -> None:
    """Poll wethr_cache.json every WETHR_POLL_SEC seconds. On per-station
    obs_ts advance, dispatch evaluation for all currently-subscribed tickers
    for that station."""
    last_mtime = 0.0
    while not _stop_event.is_set():
        _bump("wethr_polls")
        try:
            mtime = WETHR_CACHE_PATH.stat().st_mtime
            if mtime != last_mtime:
                last_mtime = mtime
                # Identify which stations have a fresh obs_ts
                changed_stations = []
                for station in market_universe.CITY_TO_STATION.values():
                    entry = shared_cache_reader._wethr_station_entry(station) or {}
                    ts = entry.get("obs_ts")
                    if ts is None:
                        continue
                    try:
                        ts = float(ts)
                    except (TypeError, ValueError):
                        continue
                    last_seen = _wethr_obs_ts_seen.get(station, 0.0)
                    if ts > last_seen:
                        _wethr_obs_ts_seen[station] = ts
                        if last_seen > 0:  # skip first-seen (boot warm-up)
                            changed_stations.append(station)
                for station in changed_stations:
                    _bump("wethr_station_events")
                    # Find all subscribed tickers for this station and dispatch
                    for ticker in _tickers_for_station(station):
                        _bump("wethr_evals_attempted")
                        _evaluate_ticker(ticker, trigger="wethr_obs")
        except FileNotFoundError:
            pass
        except Exception as e:
            log.exception("wethr poll loop iter failed: %s", e)
        _stop_event.wait(WETHR_POLL_SEC)


# ─────────────────────────────────────────────────────────────────────────────
# Push subscriber — Unix-socket subscription to wethr-cache-service events
# ─────────────────────────────────────────────────────────────────────────────
def _wethr_socket_subscriber_loop() -> None:
    """Subscribe to wethr-cache-service's event socket and trigger
    evaluations within milliseconds of an SSE event arriving. Replaces
    the 5s file-poll latency. Falls back gracefully (file-poll still
    runs as a long-cycle safety net)."""
    import socket
    backoff = 1.0
    while not _stop_event.is_set():
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect(WETHR_EVENT_SOCK_PATH)
            s.settimeout(2.0)
            log.info("subscribed to wethr events socket at %s", WETHR_EVENT_SOCK_PATH)
            backoff = 1.0
            buf = b""
            while not _stop_event.is_set():
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    continue
                except Exception as e:
                    log.warning("subscriber recv err: %s", e)
                    break
                if not chunk:
                    log.info("subscriber: connection closed by server")
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip(): continue
                    try:
                        evt = json.loads(line)
                    except Exception:
                        continue
                    _bump("wethr_socket_events")
                    station = evt.get("station") or ""
                    if not station: continue
                    # Trigger evaluation for all subscribed tickers at this station
                    for tk in _tickers_for_station(station):
                        _bump("wethr_socket_evals_attempted")
                        _evaluate_ticker(tk, trigger=f"sse_{evt.get('event_type', 'event')}")
            try: s.close()
            except Exception: pass
        except (FileNotFoundError, ConnectionRefusedError) as e:
            log.info("wethr event socket not available (%s); retry in %.0fs", e, backoff)
            if _stop_event.wait(min(backoff, 30.0)): break
            backoff = min(backoff * 2, 30.0)
        except Exception as e:
            log.warning("subscriber outer err: %s; retry in %.0fs", e, backoff)
            if _stop_event.wait(min(backoff, 30.0)): break
            backoff = min(backoff * 2, 30.0)


def _tickers_for_station(station: str) -> list[str]:
    """Subscribed tickers whose station matches. Read from kalshi_ws's
    subscription set."""
    out = []
    # kalshi_ws keeps _subscribed_tickers as module state; access via attr
    subs = getattr(kalshi_ws, "_subscribed_tickers", None) or set()
    for tk in list(subs):
        cand = market_universe.parse_ticker(tk)
        if cand and cand.station == station:
            out.append(tk)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Public lifecycle
# ─────────────────────────────────────────────────────────────────────────────
def start(rt) -> None:
    """Register the WS callback and start the wethr threads. Idempotent.

    Spawns TWO wethr threads:
      1. Socket subscriber — push notifications from wethr-cache-service
         (sub-50ms latency vs the old 5s polling).
      2. File poll — long-cycle fallback (60s) if socket disconnects.
    """
    global _started, _rt, _wethr_thread, _wethr_socket_thread
    if _started:
        return
    _rt = rt
    _stop_event.clear()
    with _stats_lock:
        _stats["started_ts"] = time.time()
    try:
        kalshi_ws.register_bbo_callback(_on_bbo_change)
    except Exception as e:
        log.exception("kalshi_ws.register_bbo_callback failed: %s", e)
        return
    _wethr_socket_thread = threading.Thread(
        target=_wethr_socket_subscriber_loop,
        name="nn_shadow_wethr_socket", daemon=True,
    )
    _wethr_socket_thread.start()
    _wethr_thread = threading.Thread(
        target=_wethr_poll_loop, name="nn_shadow_wethr_poll", daemon=True
    )
    _wethr_thread.start()
    _started = True
    log.info("nn_shadow_worker started: WS callback + socket subscriber + "
             "file-poll fallback all alive")


def stop() -> None:
    """Unregister and signal stop. Idempotent."""
    global _started
    if not _started:
        return
    _stop_event.set()
    try:
        kalshi_ws.unregister_bbo_callback(_on_bbo_change)
    except Exception:
        pass
    if _wethr_socket_thread is not None:
        _wethr_socket_thread.join(timeout=10)
    if _wethr_thread is not None:
        _wethr_thread.join(timeout=10)
    _started = False
    log.info("nn_shadow_worker stopped")
