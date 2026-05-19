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

        # Existing-position headroom: skip if we already hold the cap on this ticker
        ticker_remaining = 5.0
        if _rt is not None:
            try:
                pos = _rt.positions.get(ticker) if hasattr(_rt, "positions") else None
                if pos:
                    existing_cost = float(pos.get("cost", 0))
                    ticker_remaining = max(0.0, 5.0 - existing_cost)
            except Exception:
                pass

        # 2026-05-18 (Chris): shadow logs EVERY positive-edge candidate, no
        # 25pp ceiling. Whole point of the shadow is to figure out which
        # filters add value post-hoc. We still record rm_locked status so
        # the analysis can replay any ceiling rule. edge_max=1.0 disables
        # the in-function ceiling without changing pure_nn_decide.
        decision = nn_shadow_strategy.pure_nn_decide(
            pkt, ticker_remaining_usd=ticker_remaining, edge_max=1.0,
        )

        if decision["decision"] in ("BUY_YES", "BUY_NO"):
            _bump("evals_buy_decisions")
        else:
            _bump("evals_skip_decisions")

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
    """Register the WS callback and start the wethr poll thread. Idempotent."""
    global _started, _rt, _wethr_thread
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
    _wethr_thread = threading.Thread(
        target=_wethr_poll_loop, name="nn_shadow_wethr_poll", daemon=True
    )
    _wethr_thread.start()
    _started = True
    log.info("nn_shadow_worker started: WS callback registered, wethr poll thread alive")


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
    if _wethr_thread is not None:
        _wethr_thread.join(timeout=10)
    _started = False
    log.info("nn_shadow_worker stopped")
