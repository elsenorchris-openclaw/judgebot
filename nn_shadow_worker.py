"""nn_shadow_worker.py — the LIVE, event-driven trading brain.

NOTE (2026-06-02): despite the legacy "shadow" name, this module IS the live
trader — it places real Kalshi orders. It is the primary decision engine for the
BLEND weather bot; paper_judge_bot.py is reduced to maintenance + the shared
execute_buy/execute_sell it calls into here. See README.md.

Triggers on:
  - Kalshi WS BBO change (via kalshi_ws.register_bbo_callback)
  - wethr cache push (Unix socket) + a 5s file-poll fallback

Per event, `_evaluate_ticker` builds the data packet, computes the matcher mu
(fallback) then OVERRIDES it with the BLEND mu/sigma (`_compute_blend_override`,
the primary forecast), runs `nn_shadow_strategy.pure_nn_decide`, applies the
`_try_auto_execute` gate stack + decision window, and on a pass PLACES THE ORDER
(HIGH via paper_judge_bot.execute_buy; LOW via low_post_probe.place). Every eval
is also logged to data/shadow_nn_strategy.jsonl. `_check_adverse_drift_exit` (run
first in each eval) is the only sell path; otherwise positions hold to settlement.

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
import forecast_delta as _fd  # NWP daily-high (NBM/HRRR/ECMWF) for the agreement gate
import datetime as _dtm
from zoneinfo import ZoneInfo as _ZI

_mu_nwp_cache: dict = {}  # (station, climate_day) -> (ts, mu_nwp)


def _compute_mu_nwp(station: str, climate_day: str):
    """Independent NWP daily-high (median across NBM/HRRR/ECMWF of each model's
    MAX over its recent runs, from the shared GRIB cache via forecast_delta) used
    by the NWP-agreement gate. Returns None when unavailable (gate fails open)."""
    key = (station, climate_day)
    now = time.time()
    c = _mu_nwp_cache.get(key)
    if c and (now - c[0]) < 900:
        return c[1]
    mu = None
    nbm = None
    try:
        tz_name = nn_shadow._STATION_TZ.get(station)
        if tz_name:
            d0 = _dtm.datetime.strptime(climate_day, "%Y-%m-%d").replace(tzinfo=_ZI(tz_name))
            cs = d0.timestamp(); ce = (d0 + _dtm.timedelta(days=1)).timestamp()
            # 2026-05-26: take the MAX across the last 6 runs per model, not just the
            # newest run. A run issued AFTER the local afternoon peak only forecasts
            # forward into the cooling evening, so its in-window max misses the peak
            # it already passed -> a systematic cold bias that worsens through the day
            # (~-2F in the trading window, -8F by evening). Max over recent runs
            # recovers the peak an earlier run forecast; then median across models.
            runs = _fd.get_recent_runs(station, cs, ce, kind="high", n_runs=6)
            _nbm_entries = runs.get("NBM") or []
            _nbm_vals = [e["extreme_f"] for e in _nbm_entries
                         if e and e.get("extreme_f") is not None]
            if _nbm_vals:
                nbm = round(max(_nbm_vals), 2)
            per_model = []
            for entries in runs.values():
                vals = [e["extreme_f"] for e in entries
                        if e and e.get("extreme_f") is not None]
                if vals:
                    per_model.append(max(vals))
            if per_model:
                per_model.sort()
                mu = round(per_model[len(per_model) // 2], 2)
    except Exception:
        mu = None
        nbm = None
    _mu_nwp_cache[key] = (now, mu, nbm)
    return mu


def _compute_nbm_high(station: str, climate_day: str):
    """NBM-specific daily-high (max over recent NBM runs). Companion to
    _compute_mu_nwp -- shares its cache + single get_recent_runs read. Used by
    the (2g) one-sided NBM BUY_NO veto. None when unavailable (gate fails open)."""
    _compute_mu_nwp(station, climate_day)  # ensure cache populated
    c = _mu_nwp_cache.get((station, climate_day))
    return c[2] if c and len(c) > 2 else None

# ─────────────────────────────────────────────────────────────────────────────
# 2026-06-02: supervised blend-forecast mu override (project_blend_edge_FOUND).
# Replaces the obs-analog matcher mu with a ridge blend of market-implied mu +
# live running-extreme (wethr) + cur temp, predicted with a calibrated sigma.
# Backtest (2024-10..2026-05, Kalshi settlement, FORWARD-CHAINED, net taker fee,
# positive in EVERY forward month): HIGH +8.55c/ct, LOW +7.22c/ct.
# FAIL-SAFE: every path returns None -> the caller keeps the matcher mu.
# ─────────────────────────────────────────────────────────────────────────────
_market_mu_cache: dict = {}
# 2026-06-02: histogram of "fresh two-sided brackets found" per market_mu call,
# bucketed 0 / 1 / 2 / 3+ . implied_mu needs >=3, so 0/1/2 are the fallback cause
# (thin or stale book); 3+ that still returns None would be an implied_mu reject.
_mktmu_nbrk_hist = {"0": 0, "1": 0, "2": 0, "3+": 0}
# 2026-06-13: REST ladder-recovery state. The WS BBO cache is sometimes SPARSE
# for a station-day (tickers not yet populated / stale past the 10-min bound)
# even when Kalshi's real ladder is fat (verified live: SEA/LV/CHI/NY each had
# 8-9 two-sided brackets on REST while the cache held 1). Discarding those =
# handing genuinely-priceable markets to the matcher/paper book. When the cache
# is too sparse to fit the implied mean, fetch the real orderbook before going
# dark. Per-event 120s cache bounds the REST cost.
_rest_ladder_cache: dict = {}     # event_ticker -> (ts, [bracket dicts])
_mktmu_rest_recover = [0, 0]       # [events_rest_fetched, calls_recovered_to_3plus]

# 2026-06-15 (code audit): transient-failure backoff for the blend's data-helper
# caches. They cached their result for a FULL TTL -- which wrongly pinned a transient
# failure (a 429, a timeout, a momentary sparse book) as "the answer" for the whole
# TTL, darkening/degrading the blend long after the source recovered (directly defeats
# the 6/13 REST-recovery ship). Fix: cache an HONEST result at full TTL, but cache a
# transient FAILURE only briefly so it retries soon. _cache_ts ages a failure entry so
# (now - ts) crosses the TTL after `backoff` seconds. (A legitimately-thin market still
# caches at full TTL -- that's an honest answer, not a failure, so no re-REST storm.)
_FAIL_BACKOFF_FAST = 20.0    # Kalshi (cheap, fast-moving books): retry ~20s after a blip
_FAIL_BACKOFF_SLOW = 300.0   # OpenMeteo (paid, slow-moving NWP): retry ~5min, not the full 1h


def _cache_ts(now: float, ttl: float, ok: bool, backoff: float) -> float:
    """Cache timestamp helper: `now` for an honest result (full TTL); for a transient
    failure, aged so the entry expires after `backoff` seconds instead of the full TTL."""
    return now if ok else (now - max(0.0, ttl - backoff))


def _rest_ladder_brackets(events, station, climate_day, seen):
    """REST-fetch the live quoted ladder for a station-day when the WS BBO cache
    is too sparse to trust the implied mean. Returns bracket dicts (cents, fresh
    two-sided quotes from the live orderbook), deduped against `seen` (tickers
    already taken from the cache). Per-event 120s cache; capped orderbook fetches.
    Fail-safe [] on any error -> caller just stays dark, exactly as before."""
    import kalshi_client as kc
    now = time.time()
    max_ob = int(getattr(_cfg, "BLEND_MARKET_MU_REST_MAX_OB", 16))
    out = []
    fetched = 0
    for ev in list(events)[:3]:
        cached = _rest_ladder_cache.get(ev)
        if cached and (now - cached[0]) < 120:
            out.extend(cached[1]); continue
        ev_br = []
        list_ok = False
        try:
            d = kc.get("/trade-api/v2/markets",
                       {"event_ticker": ev, "status": "open", "limit": 60})
            list_ok = True
            for m in (d.get("markets") or []):
                if fetched >= max_ob:
                    break
                tk = m.get("ticker")
                if not tk:
                    continue
                c2 = market_universe.parse_ticker(tk)
                if not c2 or c2.station != station or c2.climate_day != climate_day:
                    continue
                # one bad orderbook (429/5xx) must NOT discard the whole event's
                # ladder -- skip just that bracket and keep the good ones collected.
                try:
                    ob = kc.get_orderbook(tk); fetched += 1
                    yds = ob.get("yes_dollars") or []
                    nds = ob.get("no_dollars") or []
                    if not yds or not nds:
                        continue
                    # best yes bid = top of yes_dollars; best yes ask = 100 - best no bid
                    yb_c = float(yds[-1][0]) * 100.0
                    ya_c = 100.0 - float(nds[-1][0]) * 100.0
                    if ya_c <= yb_c or ya_c <= 0 or ya_c > 100:
                        continue
                    ev_br.append({"kind": c2.bracket_kind, "floor": c2.floor, "cap": c2.cap,
                                  "yes_bid": yb_c, "yes_ask": ya_c, "_tk": tk})
                except Exception:
                    continue
        except Exception:
            list_ok = False
        # cache an HONEST enumeration (even if thin/empty) for 120s; on a list-fetch
        # failure DON'T pin an empty ladder -- expire fast (~20s) so it retries.
        _rest_ladder_cache[ev] = (_cache_ts(now, 120.0, list_ok, _FAIL_BACKOFF_FAST), ev_br)
        out.extend(ev_br)
    _mktmu_rest_recover[0] += 1
    return [b for b in out if b.get("_tk") not in seen]


def _compute_market_mu(station, climate_day, prefix):
    """Ladder-implied mu (cents) from the live BBO cache for this station-day,
    with a REST orderbook fallback when the cache is too sparse to fit the mean
    (2026-06-13: sparse CACHE != thin Kalshi ladder)."""
    key = (station, climate_day, prefix)
    now = time.time()
    c = _market_mu_cache.get(key)
    if c and (now - c[0]) < 180:
        return c[1]
    mu = None
    try:
        import blend_forecast
        import kalshi_ws as _kws
        brackets = []
        seen = set()
        events = set()
        for tk in list(_kws._bbo_cache.keys()):
            if not tk.startswith(prefix):
                continue
            c2 = market_universe.parse_ticker(tk)
            if not c2 or c2.station != station or c2.climate_day != climate_day:
                continue
            # capture the event ticker even for stale/empty cache rows so the REST
            # fallback can enumerate the full ladder when the cache is sparse.
            events.add(tk.rsplit("-", 1)[0])
            ce = _kws._bbo_cache.get(tk)
            if not ce:
                continue
            # 10-min staleness bound: the ladder-implied mu is a slow aggregate
            # (the model ANCHOR); the execution still crosses the fresh ask. A
            # generous bound keeps the blend active when books update sparsely
            # (overnight) without trusting truly stale quotes.
            if now - ce.get("ts", 0) > 600:
                continue
            yb = ce.get("yes_bid"); ya = ce.get("yes_ask")
            if yb is None or ya is None:
                continue
            yb_c = yb * 100.0; ya_c = ya * 100.0
            if ya_c <= yb_c or ya_c <= 0 or ya_c > 100:
                continue
            brackets.append({"kind": c2.bracket_kind, "floor": c2.floor, "cap": c2.cap,
                             "yes_bid": yb_c, "yes_ask": ya_c})
            seen.add(tk)
        # REST fallback: the cache is too sparse to trust the implied mean. The
        # Kalshi ladder may still be fat -> fetch it before going dark. Keeps the
        # n>=3 quality bar (a genuinely thin ladder still returns None below).
        if len(brackets) < 3 and getattr(_cfg, "BLEND_MARKET_MU_REST_FALLBACK", True):
            rb = _rest_ladder_brackets(events, station, climate_day, seen)
            if rb:
                brackets.extend(rb)
                if len(brackets) >= 3:
                    _mktmu_rest_recover[1] += 1
        nb = len(brackets)
        _mktmu_nbrk_hist["3+" if nb >= 3 else str(nb)] += 1
        mu = blend_forecast.implied_mu(brackets)
        ok = True   # honest computation -- mu may legitimately be None if truly thin
    except Exception:
        mu = None
        ok = False
    # cache an honest result (incl legit-thin None) at full TTL to avoid re-REST
    # storms; an EXCEPTION expires fast (~20s) so a transient blip doesn't pin the
    # blend dark for 3 min.
    _market_mu_cache[key] = (_cache_ts(now, 180.0, ok, _FAIL_BACKOFF_FAST), mu)
    return mu

_OM_MODELS = ["gfs_seamless", "ecmwf_ifs025", "icon_seamless", "gem_global",
              "ecmwf_aifs025", "jma_seamless", "ukmo_seamless"]
_blend_nwp_cache: dict = {}

def _om_key():
    k = os.environ.get("OPEN_METEO_API_KEY")
    if k:
        return k
    for p in ("/home/ubuntu/paper_judge_bot/.env", "/home/ubuntu/.env"):
        try:
            for ln in open(p):
                if ln.startswith("OPEN_METEO_API_KEY"):
                    return ln.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return None

def _compute_blend_nwp(station, climate_day, side):
    """Live OpenMeteo 7-model forecast (max/min) for the climate_day. Cached 1h.
    Returns {model: fc} or None (fail-safe -> caller drops to conservative blend)."""
    key = (station, climate_day, side)
    now = time.time()
    c = _blend_nwp_cache.get(key)
    if c and (now - c[0]) < 3600:
        return c[1]
    out = None
    try:
        import urllib.request, urllib.parse
        import station_meta as _sm
        meta = _sm.STATION_META.get(station)
        apikey = _om_key()
        if meta and apikey:
            var = "temperature_2m_max" if side == "high" else "temperature_2m_min"
            q = urllib.parse.urlencode({
                "latitude": meta["lat"], "longitude": meta["lon"],
                "past_days": 1, "forecast_days": 2, "daily": var,
                "models": ",".join(_OM_MODELS), "temperature_unit": "fahrenheit",
                "timezone": "auto", "apikey": apikey})
            url = "https://customer-api.open-meteo.com/v1/forecast?" + q
            with urllib.request.urlopen(url, timeout=8) as r:
                d = json.load(r)
            dd = d.get("daily", {}) or {}
            dates = dd.get("time", []) or []
            if climate_day in dates:
                i = dates.index(climate_day)
                mm = {}
                for mdl in _OM_MODELS:
                    vals = dd.get(var + "_" + mdl, [])
                    if i < len(vals) and vals[i] is not None:
                        mm[mdl] = float(vals[i])
                if len(mm) >= 5:
                    out = mm
    except Exception:
        out = None
    # honest forecast cached 1h; a transient fetch failure / <5-model response
    # expires in ~5min so we don't sit on the conservative blend for a full hour.
    _blend_nwp_cache[key] = (_cache_ts(now, 3600.0, out is not None, _FAIL_BACKOFF_SLOW), out)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# LST clock helpers (2026-06-03, Chris): Kalshi climate-days + the empirical
# peak/min tables are LST (no DST). local_clock_context()/OpenMeteo are DAYLIGHT
# time. These convert between them so the WHOLE clock lives in one LST frame.
# ─────────────────────────────────────────────────────────────────────────────
def _dst_offset_h(station: str) -> float:
    """Current daylight-saving offset in HOURS for the station: ~1.0 while DST is in
    effect, 0.0 in standard time (and 0.0 year-round for no-DST zones like Arizona).
    = cur_utcoffset(now) - standard_utcoffset(January). Used to shift solar/OpenMeteo
    DAYLIGHT-time hours to LST. Never raises (returns 0.0 on any failure)."""
    try:
        import climate_normals as _cn
        tz = _cn._STATION_TZ.get(station)
        if not tz:
            return 0.0
        from zoneinfo import ZoneInfo
        z = ZoneInfo(tz)
        now = _dtm.datetime.now(_dtm.timezone.utc)
        cur = now.astimezone(z).utcoffset()
        jan = _dtm.datetime(now.year, 1, 15, 12, tzinfo=_dtm.timezone.utc).astimezone(z).utcoffset()
        if cur is None or jan is None:
            return 0.0
        return round((cur - jan).total_seconds() / 3600.0, 3)
    except Exception:
        return 0.0


def _station_local_date(station: str) -> Optional[str]:
    """Station's CURRENT LOCAL-STANDARD-TIME (LST) calendar date as 'YYYY-MM-DD' — the
    NWS climate-day / Kalshi climate_day convention (midnight-to-midnight STANDARD time,
    NOT wall-clock LDT). None on any tz miss/failure (callers fail OPEN). Used by the
    window gate to refuse a bracket whose extreme is on a different calendar date than
    now (e.g. a next-day bracket open during today's deep window)."""
    try:
        import climate_normals as _cn
        tz = _cn._STATION_TZ.get(station)
        if not tz:
            return None
        from zoneinfo import ZoneInfo
        # 2026-06-05 (audit): LST, not wall-clock LDT. They differ only during the DST
        # hour 00:00-01:00 LDT, but LDT there would reject today's LST-day bracket and
        # could accept a next-day one (the ~24h-early class this guard exists to prevent).
        # Shift UTC by the STANDARD (January) offset = LST.
        now = _dtm.datetime.now(_dtm.timezone.utc)
        jan = _dtm.datetime(now.year, 1, 15, 12, tzinfo=_dtm.timezone.utc).astimezone(ZoneInfo(tz)).utcoffset()
        if jan is None:
            return None
        return (now + jan).strftime("%Y-%m-%d")
    except Exception:
        return None


def _lst_signed_h(target, cur):
    """Signed hours from cur to target local hour, wrapped to (-12, 12] — matches
    solar_calc._signed_h_to. None if either input is None."""
    if target is None or cur is None:
        return None
    d = float(target) - float(cur)
    if d <= -12:
        d += 24.0
    if d > 12:
        d -= 24.0
    return round(d, 2)


def _apply_lst_clock(ctx: dict, station: str, climate_day: str) -> dict:
    """Override the packet's local-clock fields into ONE consistent LST frame.

    Kalshi climate-days + the empirical peak/min tables (_lookup_peak_hour, observed
    5yr-P50 — the most accurate climatology we have) are LST (no DST). The incoming
    ctx (from climate_normals.local_clock_context) is solar+ZoneInfo = DAYLIGHT time
    AND a cruder sunrise+lag model. Mixing them put the window gate ~1h off in summer
    (rejected 194 in-window LOW buys on 6/3). Here local_hour + peak/min + h_to_* +
    past_* are all re-derived from the empirical LST table; the solar values are
    dropped (kept only as an LST-shifted FALLBACK for a station absent from the table,
    so none loses its clock). Inert when LST_CLOCK_ENABLED is False."""
    if not getattr(_cfg, "LST_CLOCK_ENABLED", True):
        return ctx
    try:
        dst = _dst_offset_h(station)
        lh = ctx.get("local_hour")
        lh_lst = ((float(lh) - dst) % 24.0) if lh is not None else None
        pk = _window_peak_hour(station, "HIGH", climate_day)
        if pk is None and ctx.get("peak_hour_local") is not None:
            pk = (float(ctx["peak_hour_local"]) - dst) % 24.0
        mn = _window_peak_hour(station, "LOW", climate_day)
        if mn is None and ctx.get("min_hour_local") is not None:
            mn = (float(ctx["min_hour_local"]) - dst) % 24.0
        ctx["local_hour"] = lh_lst
        ctx["peak_hour_local"] = pk
        ctx["min_hour_local"] = mn
        ctx["h_to_peak"] = _lst_signed_h(pk, lh_lst)
        ctx["h_to_min"] = _lst_signed_h(mn, lh_lst)
        ctx["past_peak_today"] = (ctx["h_to_peak"] is not None and ctx["h_to_peak"] < 0)
        ctx["past_min_today"] = (ctx["h_to_min"] is not None and ctx["h_to_min"] < 0)
        ctx["tz_convention"] = "LST"
        ctx["dst_offset_h"] = dst
    except Exception:
        log.exception("LST clock override failed for %s", station)
    return ctx


def _compute_fc_min_hour(station, climate_day):
    """Live hourly OpenMeteo forecast -> LOCAL hour of the day's forecast MIN.
    Cached 1h. Returns None on any failure (caller skips the lock = current behavior)."""
    key = (station, climate_day)
    now = time.time()
    c = _fc_minhour_cache.get(key)
    if c and (now - c[0]) < 3600:
        return c[1]
    out = None
    try:
        import urllib.request, urllib.parse
        import station_meta as _sm
        meta = _sm.STATION_META.get(station)
        apikey = _om_key()
        if meta and apikey:
            q = urllib.parse.urlencode({
                "latitude": meta["lat"], "longitude": meta["lon"],
                "past_days": 1, "forecast_days": 2, "hourly": "temperature_2m",
                "temperature_unit": "fahrenheit", "timezone": "auto", "apikey": apikey})
            url = "https://customer-api.open-meteo.com/v1/forecast?" + q
            with urllib.request.urlopen(url, timeout=8) as r:
                d = json.load(r)
            hh = d.get("hourly", {}) or {}
            times = hh.get("time", []) or []
            temps = hh.get("temperature_2m", []) or []
            best = None
            for t, tv in zip(times, temps):
                if tv is None or not str(t).startswith(climate_day):
                    continue
                try:
                    hr = int(t[11:13]) + int(t[14:16]) / 60.0
                except Exception:
                    continue
                if best is None or tv < best[0]:
                    best = (tv, hr)
            if best:
                out = best[1]
    except Exception:
        out = None
    # honest result cached 1h; a transient fetch failure expires in ~5min so the LOW
    # forecast-lock isn't disabled for a full hour after a blip.
    _fc_minhour_cache[key] = (_cache_ts(now, 3600.0, out is not None, _FAIL_BACKOFF_SLOW), out)
    return out


_fc_minhour_cache: dict = {}
_blend_log_ts = [0.0]
# 2026-06-02: diagnostics for WHY the blend falls back to the matcher. Cumulative
# counts (fired vs each None reason), logged every 5 min, so we can see whether the
# fallback is market_mu (thin/stale book = <3 fresh brackets), obs, or it's firing.
_blend_diag = {"fired": 0, "none_market_mu": 0, "none_rm_curt": 0, "none_blend_mu": 0}
_blend_diag_ts = [0.0]
def _blend_diag_tick(key):
    _blend_diag[key] = _blend_diag.get(key, 0) + 1
    if time.time() - _blend_diag_ts[0] > 300:
        _blend_diag_ts[0] = time.time()
        try:
            log.info("BLEND fallback diag (cumulative): %s | market_mu fresh-bracket hist: %s "
                     "| REST-recover [events_fetched, recovered_to_3+]: %s",
                     dict(_blend_diag), dict(_mktmu_nbrk_hist), list(_mktmu_rest_recover))
        except Exception:
            pass
def _compute_blend_override(cand, pkt, nn_res):
    """Return (mu, sigma) from the blend, or None to keep the matcher mu."""
    try:
        if not getattr(_cfg, "BLEND_FORECAST_ENABLED", False):
            return None
        is_high = (cand.series_prefix == "KXHIGH")
        if not is_high and not getattr(_cfg, "BLEND_FORECAST_LOW_ENABLED", False):
            return None
        import blend_forecast
        side = "high" if is_high else "low"
        variant = getattr(_cfg, "BLEND_FORECAST_VARIANT", "conservative")
        mkt = _compute_market_mu(cand.station, cand.climate_day, cand.series_prefix)
        if mkt is None:
            _blend_diag_tick("none_market_mu")
            return None
        rm = pkt.get("running_min_or_max")
        curt = (nn_res or {}).get("cur_tmpf")
        if curt is None:
            curt = pkt.get("cur_tmpf")
        if rm is None or curt is None:
            _blend_diag_tick("none_rm_curt")
            return None
        nwp = None
        if variant == "full":
            nwp = _compute_blend_nwp(cand.station, cand.climate_day, side)
        r = blend_forecast.blend_mu(side, float(mkt), float(rm), float(curt), nwp, variant)
        if r is None and variant == "full":
            # graceful degradation: OpenMeteo fetch failed -> conservative blend
            r = blend_forecast.blend_mu(side, float(mkt), float(rm), float(curt), None, "conservative")
        # LOW forecast-lock: when the hourly forecast says the daily low ALREADY
        # occurred (forecast-min-time behind the eval time), the running-min IS the
        # answer -> anchor mu to it instead of letting NWP over-predict a pre-dawn
        # low that won't come. Backtest: LOW +11% (early-morning -$69->-$16; evening/
        # pre-dawn untouched). FAIL-SAFE: no hourly/clock -> no lock = current behavior.
        if (r is not None and side == "low"
                and getattr(_cfg, "BLEND_LOW_FORECAST_LOCK_ENABLED", True)):
            _lh = (pkt.get("local_clock") or {}).get("local_hour")   # LST (post-override)
            _fmh = _compute_fc_min_hour(cand.station, cand.climate_day)
            # _compute_fc_min_hour uses OpenMeteo timezone:auto = DAYLIGHT time; the
            # clock is now LST, so shift the forecast min-hour to LST to compare
            # like-for-like (else the lock would be ~1h off in summer).
            if _fmh is not None and getattr(_cfg, "LST_CLOCK_ENABLED", True):
                _fmh = (float(_fmh) - _dst_offset_h(cand.station)) % 24.0
            _marg = float(getattr(_cfg, "BLEND_LOW_LOCK_MARGIN_H", 1.5))
            if _lh is not None and _fmh is not None and _fmh < float(_lh) - _marg:
                r = (round(float(rm), 2), r[1])
                pkt["blend_low_locked"] = True
        if r is not None and (time.time() - _blend_log_ts[0]) > 60:
            _blend_log_ts[0] = time.time()
            try:
                log.info("BLEND applied %s %s/%s: mu %.1f->%.1f sigma=%.2f (mkt_mu=%.1f)",
                         cand.series_prefix, cand.station, cand.climate_day,
                         pkt.get("mu_chosen", 0.0), r[0], r[1], mkt)
            except Exception:
                pass
        _blend_diag_tick("fired" if r is not None else "none_blend_mu")
        return r
    except Exception:
        return None


log = logging.getLogger("judge.nn_shadow_worker")

# ─────────────────────────────────────────────────────────────────────────────
# Module config
# ─────────────────────────────────────────────────────────────────────────────
SHADOW_LOG_PATH = Path("/home/ubuntu/paper_judge_bot/data/shadow_nn_strategy.jsonl")
WETHR_CACHE_PATH = Path("/home/ubuntu/shared/wethr_cache.json")

# 2026-06-01: pure-additive logging of current obs temp + wethr probable-high/low
# lock signals (settlement-grade source) for post-hoc edge validation. Defensive:
# never raises into the logging path; 5s TTL cache avoids per-eval file reads.
_wethr_extra_cache = {}
def _wethr_obs_extra(station):
    try:
        _now = time.time()
        _c = _wethr_extra_cache.get("__all__")
        if _c is None or _now - _c[0] > 5.0:
            with open(WETHR_CACHE_PATH) as _f:
                _data = json.load(_f)
            _wethr_extra_cache["__all__"] = (_now, _data.get("stations", {}) or {})
        _sts = _wethr_extra_cache["__all__"][1]
        _st = _sts.get(station) or _sts.get(station.lstrip("K")) or _sts.get("K" + station) or {}
        return {
            "current_temp_f": _st.get("temp_f"),
            "wethr_highest_probable_f": _st.get("highest_probable_f"),
            "wethr_lowest_probable_f": _st.get("lowest_probable_f"),
        }
    except Exception:
        return {}

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
# 2026-06-05 (audit): per-(station,series,direction) in-flight buy reservations so the 3
# concurrent eval threads (WS callback, wethr-poll, wethr-SSE) can't race two same-
# (station,dir) brackets past the gate-5 position cap — a TOCTOU where both read
# n_existing<cap before either records its position. The count+check+reserve is atomic
# under _pending_buys_lock; the reservation is released in _try_auto_execute's execution
# finally / on its gate-6,7 rejects.
_pending_buys: dict = {}
_pending_buys_lock = threading.Lock()


def _release_pending(cap_key) -> None:
    """Release a per-(station,series,direction) in-flight buy reservation. Bulletproof
    (never raises) so a caller can put it before a return without risking the return."""
    try:
        with _pending_buys_lock:
            v = _pending_buys.get(cap_key, 0) - 1
            if v > 0:
                _pending_buys[cap_key] = v
            else:
                _pending_buys.pop(cap_key, None)
    except Exception:
        pass


def _count_high_fills_today(climate_day) -> int:
    """This bot's FILLED HIGH positions for `climate_day` across ALL stations/directions
    — the daily HIGH exposure for the per-day cap (2026-06-16). Distinct from the
    per-station cap: the correlated-forecast-miss risk is ACROSS stations (6/11 was 10
    different stations losing together). Call under _pending_buys_lock."""
    n = 0
    try:
        for tk, p in (getattr(_rt, "positions", {}) or {}).items():
            if not isinstance(p, dict):
                continue
            try:
                if float(p.get("cost", 0)) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            if not str(tk).startswith("KXHIGH"):
                continue
            pd = p.get("date_str") or p.get("climate_day")
            if pd and pd != climate_day:
                continue
            n += 1
    except Exception:
        pass
    return n


def _count_existing_slots(cand, direction) -> int:
    """Filled positions + resting maker orders occupying this (station, series, direction,
    climate_day) slot = the gate-5 position-cap occupancy. Call under _pending_buys_lock."""
    n = 0
    try:
        if hasattr(_rt, "positions"):
            sp = cand.series_prefix
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
                if not str(tk).startswith(sp):
                    continue
                if p.get("action") != direction:
                    continue
                pos_date = p.get("date_str") or p.get("climate_day")
                if pos_date and pos_date != cand.climate_day:
                    continue
                n += 1
    except Exception:
        pass
    try:
        import low_post_probe
        pos_tickers = set(getattr(_rt, "positions", {}) or {})
        for r in low_post_probe.resting_rows():
            tk = str(r.get("ticker", ""))
            if tk == cand.ticker or tk in pos_tickers:
                continue
            if not tk.startswith(cand.series_prefix):
                continue
            ctx = r.get("entry_ctx") or {}
            if ctx.get("station") != cand.station:
                continue
            if ctx.get("action") != direction:
                continue
            if str(r.get("climate_day", "")) != cand.climate_day:
                continue
            n += 1
    except Exception:
        pass
    return n
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

# 2026-05-26: adverse-drift exit — per-ticker sustained-breach state.
# ticker -> epoch when the held-side bid first fell >= ADVERSE_DRIFT_EXIT_PP
# below its entry baseline. Reset when the bid recovers above the threshold.
_drift_breach: dict = {}

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


# =============================================================================
# Peak-data alerting (2026-05-21): NO SILENT FAILURES on peak data. Two kinds:
#   (a) missing_peak  -- _lookup_peak_hour returned None (no peak in fractional
#       OR pace_curves) -> the cell is NOT traded. Loud Discord alert so the
#       skip is never silent.
#   (b) frac_fallback -- the precise fractional peak is missing for this
#       (station, side, MM-DD); we are silently substituting the coarser
#       pace_curves integer hour. Currently fires only for KDCA in February
#       (sparse heating_traces history); zero in-season impact today, but
#       surfaced so any future in-season fractional gap is never silent.
# Dedup per (kind, station, series, climate_day): each cell is evaluated
# thousands of times/day, so we alert ONCE. Alerts are rare (true data gaps),
# so the dedup set stays small.
# =============================================================================
_peak_alert_seen: set = set()
_window_alert_seen: set = set()  # dedup for missing-window alerts, per (station,series,month)


def _alert_peak_issue(kind: str, station: str, series: str,
                      climate_day: str, detail: str) -> None:
    key = (kind, station, series, climate_day)
    if key in _peak_alert_seen:
        return
    _peak_alert_seen.add(key)
    _bump(f"peak_alert_{kind}")
    log.error("peak-data alert [%s] %s/%s %s: %s",
              kind, station, series, climate_day, detail)
    try:
        import paper_judge_bot as _pjb
        _pjb.discord_send(
            f"\u26d4 PEAK DATA [{kind}] {station}/{series} {climate_day}: {detail}"
        )
    except Exception:
        log.exception("discord_send failed for peak-data alert [%s]", kind)


def _alert_missing_window(station: str, series: str, month, climate_day: str,
                          detail: str) -> None:
    """Throttled Discord alert when the window table has NO entry for a cell.
    As of 2026-05-21 the window table (push_window_overrides.PUSH_WINDOW_OVERRIDES)
    is the SOLE source of trading windows -- there is no default fallback -- so a
    missing cell means the bot will NOT trade it. This makes that loud, never
    silent. Dedup per (station, series, month)."""
    key = (station, series, month)
    if key in _window_alert_seen:
        return
    _window_alert_seen.add(key)
    _bump("window_alert_missing")
    log.error("missing-window alert %s/%s month=%s (%s): %s",
              station, series, month, climate_day, detail)
    try:
        import paper_judge_bot as _pjb
        _pjb.discord_send(
            f"\u26d4 PUSH WINDOW MISSING {station}/{series} month={month}: {detail}"
        )
    except Exception:
        log.exception("discord_send failed for missing-window alert")


_low_front_alert_seen: set = set()  # dedup low cold-front gate alerts, per (station, climate_day)


def _alert_low_front(station: str, climate_day: str, wind_mph: float,
                     threshold: float) -> None:
    """Throttled Discord alert when the LOW cold-front gate (2c) blocks a push
    BUY. Sustained wind >= PUSH_LOW_FRONT_WIND_MPH at an overnight LOW is a
    frontal / cold-air-advection signature the nn matcher mis-handles: it
    over-projects the daily minimum and -- unlike high-variance regimes -- its
    sigma does not widen to flag it. Dedup per (station, climate_day): the cell
    is evaluated thousands of times/day, so we alert ONCE per station per day."""
    key = (station, climate_day)
    if key in _low_front_alert_seen:
        return
    _low_front_alert_seen.add(key)
    _bump("low_front_gate_fired")
    log.warning("LOW cold-front gate fired %s %s: sustained wind %.1fmph "
                "(>=%.0fmph) -- skipping LOW push BUYs",
                station, climate_day, wind_mph, threshold)
    try:
        import paper_judge_bot as _pjb
        _pjb.discord_send(
            f"⛔ LOW COLD-FRONT GATE {station} {climate_day}: skipping "
            f"LOW push BUYs — sustained wind {wind_mph:.0f}mph "
            f"(≥{threshold:.0f}mph). Matcher over-projects the low in "
            f"frontal regimes."
        )
    except Exception:
        log.exception("discord_send failed for low cold-front gate alert")


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
    frac_enabled = False
    try:
        import config as _cfg
        if getattr(_cfg, "USE_FRACTIONAL_PEAK_FOR_WINDOW", False):
            frac_enabled = True
            frac_table = (_peak_table_frac_cache if series == "HIGH"
                          else _min_table_frac_cache)
            frac_row = frac_table.get(station) or {}
            frac_val = frac_row.get(md_key)
            if frac_val is not None:
                return frac_val
    except Exception:
        pass

    # Fallback to int (legacy). (b) When fractional was ENABLED but missed this
    # (station, side, MM-DD), we are silently substituting the coarser int hour
    # -- alert so the degradation is never silent. (If int is also None, both
    # sources missed -> caller's (a) missing_peak alert fires; skip (b) here.)
    table = _peak_table_cache if series == "HIGH" else _min_table_cache
    row = table.get(station) or {}
    int_val = row.get(month)
    if frac_enabled and int_val is not None:
        _alert_peak_issue(
            "frac_fallback", station, series, climate_day,
            f"fractional peak missing for {md_key}; using coarse pace_curves int={int_val}",
        )
    return int_val


# ─────────────────────────────────────────────────────────────────────────────
# Push override lookup (read-only, for per-decision logging + future sizing)
# ─────────────────────────────────────────────────────────────────────────────
def _lookup_push_override(station: str, series: str,
                          climate_day: str) -> Optional[dict]:
    """Return the matched unconditional push override as a dict for logging:
    {before, after, bias, mae, src}, or None when overrides are disabled or no
    entry exists. READ-ONLY — does not affect the decision window itself
    (that stays in _in_decision_window). `mae` is the cell's expected pre-peak
    accuracy (°F); `bias` is the residual μ correction. Both are logged per
    decision so we can later validate MAE-based sizing / bias application.
    Handles legacy 2-/3-tuples gracefully (bias/mae → None)."""
    try:
        import config as _cfg
        if not getattr(_cfg, "USE_PUSH_WINDOW_OVERRIDES", False):
            return None
        try:
            from push_window_overrides import PUSH_WINDOW_OVERRIDES
        except ImportError:
            return None
        month = int(climate_day.split("-")[1])
        ov = PUSH_WINDOW_OVERRIDES.get((station, series, month))
        if ov is None:
            return None
        before = float(ov[0])
        after = float(ov[1])
        bias = float(ov[2]) if len(ov) > 2 and ov[2] is not None else None
        mae = float(ov[3]) if len(ov) > 3 and ov[3] is not None else None
        return {"before": before, "after": after, "bias": bias,
                "mae": mae, "src": "unconditional"}
    except Exception:
        return None


def _mae_conf_mult(mae) -> float:
    """Confidence/sizing multiplier from a cell's expected pre-peak MAE (°F).
    Lower MAE = more reliable matcher = full size; higher MAE = scale down.
    ONLY reduces size (never increases) → risk-reducing. mae=None (no override
    / fallback) → moderate 0.5. Tiers in config.PUSH_MAE_CONF_TIERS.

    Out-of-sample validated (2026-05-21): cell MAE predicts holdout accuracy
    (corr 0.62, monotonic tiers train<1.0→1.32°F .. ≥2.5→2.96°F)."""
    if mae is None:
        return 0.5
    tiers = getattr(_cfg, "PUSH_MAE_CONF_TIERS", None)
    if tiers:
        for lo, hi, mult in tiers:
            if lo <= mae < hi:
                return float(mult)
        return float(tiers[-1][2])
    if mae < 1.0:
        return 1.0
    if mae < 1.5:
        return 0.75
    if mae < 2.5:
        return 0.5
    return 0.3


# ─────────────────────────────────────────────────────────────────────────────
# Global regime-MAE adjustment (2026-05-21) — adjusts the cell's expected MAE by
# the day's regime, then sizes off the adjusted MAE. Out-of-sample validated:
# adding global (pooled-across-all-cells) deltas for sigma/anomaly/sky/wind lifts
# per-decision MAE-prediction corr 0.167 -> 0.229. The deltas are a CORRECTION on
# top of the per-cell baseline ("today is hot+cloudy -> add +Δ"), robust because
# learned on 200K-1M days each. Sizing-only (no flip risk).
# ─────────────────────────────────────────────────────────────────────────────
_regime_deltas = None          # {dim: {bucket: delta_F}}
_climate_normals = None        # {"Kxxx": {"MM-DD": [24 hourly medians]}}
_regime_tables_loaded = False
_regime_tables_lock = threading.Lock()


def _ensure_regime_tables() -> None:
    global _regime_deltas, _climate_normals, _regime_tables_loaded
    if _regime_tables_loaded:
        return
    with _regime_tables_lock:
        if _regime_tables_loaded:
            return
        base = Path("/home/ubuntu/paper_judge_bot/data")
        try:
            with open(base / "regime_mae_deltas.json") as f:
                _regime_deltas = json.load(f)
        except Exception:
            _regime_deltas = {}
        try:
            with open(base / "climate_normals_hourly.json") as f:
                _climate_normals = json.load(f)
        except Exception:
            _climate_normals = {}
        _regime_tables_loaded = True


def _rt_sigma_bucket(sig):
    if sig is None:
        return None
    if sig < 1.5:
        return "low"
    if sig < 2.5:
        return "mid"
    return "high"


def _rt_sky_bucket(cov):
    # wethr cloud_1_coverage string -> clear/partly/cloudy (matches skyc1 enum)
    if not cov:
        return None
    c = str(cov).strip().upper()
    if c in ("CLR", "SKC", "FEW"):
        return "clear"
    if c == "SCT":
        return "partly"
    if c in ("BKN", "OVC", "VV"):
        return "cloudy"
    return None


def _rt_wind_bucket(mph):
    # backtest bucketed knots (<5 calm, <15 moderate, else strong); convert mph
    if mph is None:
        return None
    try:
        kt = float(mph) / 1.15078
    except (TypeError, ValueError):
        return None
    if kt < 5.0:
        return "calm"
    if kt < 15.0:
        return "moderate"
    return "strong"


def _rt_tspeak_bucket(rm_age_sec):
    # minutes since the running extreme was set (proxy for backtest tspeak)
    if rm_age_sec is None:
        return None
    try:
        m = float(rm_age_sec) / 60.0
    except (TypeError, ValueError):
        return None
    if m < 10.0:
        return "not_yet"
    if m < 30.0:
        return "fresh"
    if m < 120.0:
        return "recent"
    return "stale"


def _rt_anomaly_bucket(station, climate_day, local_hour, cur_tmpf):
    if cur_tmpf is None or local_hour is None:
        return None
    try:
        parts = climate_day.split("-")
        md = "%02d-%02d" % (int(parts[1]), int(parts[2]))
        hr = int(float(local_hour)) % 24
    except Exception:
        return None
    st_norm = (_climate_normals or {}).get(station)
    if not st_norm:
        return None
    arr = st_norm.get(md)
    if not arr or hr >= len(arr) or arr[hr] is None:
        return None
    anom = float(cur_tmpf) - float(arr[hr])
    if anom < -5.0:
        return "cold"
    if anom > 5.0:
        return "hot"
    return "normal"


def _regime_adjusted_mae(cell_mae, cand, pkt, nn_res):
    """cell_mae + damped sum of global regime deltas for today's buckets.
    Returns (adjusted_mae, debug_dict). Falls back to cell_mae on any miss."""
    if cell_mae is None:
        return cell_mae, {}
    _ensure_regime_tables()
    if not _regime_deltas:
        return cell_mae, {}
    wo = pkt.get("wethr_obs") or {}
    ctx = pkt.get("local_clock") or {}
    # tspeak proxy: minutes since the running extreme was set (rm_age). The
    # backtest tspeak = mins since the trajectory's max(HIGH)/min(LOW) bin; the
    # bot's rm_age (time since wethr last set the running max/min) measures the
    # same "time since the extreme so far" and is the closest runtime signal.
    _rm_age = (pkt.get("rm_age_max_sec") if cand.series_prefix == "KXHIGH"
               else pkt.get("rm_age_min_sec"))
    bk = {
        "sigma":   _rt_sigma_bucket(nn_res.get("sigma_natural")),
        "anomaly": _rt_anomaly_bucket(cand.station, cand.climate_day,
                                      ctx.get("local_hour"), wo.get("temp_f")),
        "sky":     _rt_sky_bucket(wo.get("cloud_1_coverage")),
        "wind":    _rt_wind_bucket(wo.get("wind_speed_mph")),
        "tspeak":  _rt_tspeak_bucket(_rm_age),
    }
    # 2026-05-21: per-side deltas — regime affects HIGH vs LOW oppositely
    # (e.g. hot-anomaly: HIGH more accurate −0.25, LOW much less +1.46). Deltas
    # keyed {side:{dim:{bucket}}}. Falls back to legacy pooled {dim:{bucket}}.
    _side = "high" if cand.series_prefix == "KXHIGH" else "low"
    _src = (_regime_deltas.get(_side) if _side in _regime_deltas
            else _regime_deltas) or {}
    total = 0.0
    applied = {}
    for dim, b in bk.items():
        if b is None:
            continue
        dlt = (_src.get(dim) or {}).get(b)
        if dlt is not None:
            total += float(dlt)
            applied[dim] = (b, dlt)
    damp = float(getattr(_cfg, "PUSH_REGIME_MAE_DAMP", 1.0))
    adj = max(0.1, cell_mae + damp * total)
    return round(adj, 3), {"buckets": applied, "raw_delta": round(total, 3),
                           "damp": damp, "cell_mae": cell_mae}


# ─────────────────────────────────────────────────────────────────────────────
# Forecast-anchored window peak/min hour (2026-06-03, Chris). The empirical LST
# table is the most accurate CLIMATOLOGY; the live NWP forecast is more accurate
# for ANOMALOUS days (a front moves the peak/min hours). We anchor the window to the
# forecast hour ONLY when it is trustworthy — searched in the physical band (so a
# low-diurnal-range station's calendar-day argmin doesn't land in the EVENING) and
# SHARP (a clear peak/min, not a flat plateau where the argmax is just noise). Else
# we fall back to the empirical LST climatology. Both the gate and the packet clock
# call _window_peak_hour so they can never diverge.
# ─────────────────────────────────────────────────────────────────────────────
_FC_PEAK_BAND_LST = (10.0, 19.0)   # afternoon — daily HIGH search band (LST)
_FC_MIN_BAND_LST = (1.0, 9.0)      # around dawn — daily LOW search band (LST)
_fc_curve_cache: dict = {}

def _fc_curve(station, climate_day):
    """Live OpenMeteo hourly temps for the climate_day as [(local_daylight_hour, tempF)].
    Cached 1h. Returns [] on any failure. ONE fetch/station/hour serves both extremes."""
    key = (station, climate_day)
    now = time.time()
    c = _fc_curve_cache.get(key)
    if c and (now - c[0]) < 3600:
        return c[1]
    pts = []
    try:
        import urllib.request, urllib.parse
        import station_meta as _sm
        meta = _sm.STATION_META.get(station)
        apikey = _om_key()
        if meta and apikey:
            q = urllib.parse.urlencode({
                "latitude": meta["lat"], "longitude": meta["lon"],
                "past_days": 1, "forecast_days": 2, "hourly": "temperature_2m",
                "temperature_unit": "fahrenheit", "timezone": "auto", "apikey": apikey})
            with urllib.request.urlopen(
                    "https://customer-api.open-meteo.com/v1/forecast?" + q, timeout=8) as r:
                d = json.load(r)
            hh = d.get("hourly", {}) or {}
            for t, tv in zip(hh.get("time", []) or [], hh.get("temperature_2m", []) or []):
                if tv is None or not str(t).startswith(climate_day):
                    continue
                try:
                    pts.append((int(t[11:13]) + int(t[14:16]) / 60.0, float(tv)))
                except Exception:
                    continue
    except Exception:
        pts = []
    _fc_curve_cache[key] = (now, pts)
    return pts

def _fc_extreme_hour(station, climate_day, series):
    """Forecast hour (LST) of the daily HIGH (argmax) / LOW (argmin), SEARCHED ONLY
    in the physical band so a low-diurnal-range station's calendar-day argmin can't
    land in the evening. Returns (hour_lst, is_sharp). is_sharp = few hours within
    FORECAST_FLAT_TOL_F of the extreme (a clear peak/min, not a flat plateau where
    the timing is just noise). (None, False) when unavailable / no in-band hours."""
    pts = _fc_curve(station, climate_day)
    if not pts:
        return None, False
    dst = _dst_offset_h(station)
    band = _FC_PEAK_BAND_LST if series == "HIGH" else _FC_MIN_BAND_LST
    inband = []
    for h_local, tv in pts:
        h_lst = (h_local - dst) % 24.0
        if band[0] <= h_lst <= band[1]:
            inband.append((h_lst, tv))
    if not inband:
        return None, False
    ext = (max if series == "HIGH" else min)(inband, key=lambda x: x[1])
    tol = float(getattr(_cfg, "FORECAST_FLAT_TOL_F", 1.0))
    near = sum(1 for _h, tv in inband if abs(tv - ext[1]) <= tol)
    sharp = near <= int(getattr(_cfg, "FORECAST_FLAT_MAX_HOURS", 3))
    # band-edge guard: an extreme sitting AT the search-band boundary usually means
    # the true extreme is OUTSIDE the band (clipped) -> the timing is unreliable, so
    # don't trust it (fall back to climatology). Margin in hours.
    edge_m = float(getattr(_cfg, "FC_BAND_EDGE_MARGIN_H", 0.5))
    if ext[0] <= band[0] + edge_m or ext[0] >= band[1] - edge_m:
        sharp = False
    return round(ext[0], 2), sharp

def _window_peak_hour(station, series, climate_day):
    """THE window peak (HIGH) / min (LOW) hour, in LST. Forecast-anchored: the live
    NWP forecast hour when it is in-band AND sharp (a real, clearly-timed extreme —
    e.g. a front day); otherwise the empirical LST climatology (_lookup_peak_hour).
    Used by BOTH the gate and the packet clock so they stay in lockstep."""
    emp = _lookup_peak_hour(station, series, climate_day)
    if not getattr(_cfg, "FORECAST_ANCHOR_ENABLED", True):
        return emp
    try:
        fc_lst, sharp = _fc_extreme_hour(station, climate_day, series)
        if fc_lst is None or not sharp:
            return emp
        return fc_lst
    except Exception:
        return emp


# ─────────────────────────────────────────────────────────────────────────────
# Per-station decision-window check (auto-execute gate)
# ─────────────────────────────────────────────────────────────────────────────
def _in_decision_window(station: str, series: str, local_hour: float,
                        climate_day: str) -> tuple[bool, str]:
    """Return (in_window, debug_str). Window is [peak − before, peak + after].

    2026-05-21: (before, after) come SOLELY from the per-(station, series,
    month) window table (push_window_overrides.PUSH_WINDOW_OVERRIDES). There is
    NO default-window fallback. A cell missing from the table is NOT traded and
    fires a throttled Discord alert (_alert_missing_window) -- no silent gaps.
    USE_PUSH_WINDOW_OVERRIDES=False is a clean master kill-switch (no trades, no
    alert). The peak hour itself comes from _lookup_peak_hour (fractional +
    pace_curves int fallback); a missing peak fires the (a) missing_peak alert.
    """
    if local_hour is None:
        return False, "no_local_hour"
    # 2026-06-04 (Chris): only trade a bracket on the calendar date its extreme
    # occurs. The window test below is purely time-of-day (local_hour vs peak), so a
    # FUTURE-day bracket open during today's deep window would otherwise PASS (e.g. a
    # Jun-4 HIGH evaluated at Jun-3 noon -> h_to_peak lands in-window -> bought ~27h
    # early). Guard on the station's WALL-CLOCK date (= Kalshi's climate_day). Fail
    # OPEN on any tz miss so a real same-day trade is never blocked.
    import config as _cfg
    if getattr(_cfg, "CLIMATE_DAY_GUARD_ENABLED", True):
        _ld = _station_local_date(station)
        if _ld is not None and climate_day != _ld:
            return False, f"not_today_climate_day {climate_day}!=local:{_ld}"
    peak = _window_peak_hour(station, series, climate_day)
    if peak is None:
        # (a) NO peak in fractional OR pace_curves -> not trading this cell.
        # Loud alert so a true missing-peak is never a silent skip.
        _alert_peak_issue(
            "missing_peak", station, series, climate_day,
            "no peak in fractional OR pace_curves tables; NOT trading this cell",
        )
        return False, f"no_peak_for_{station}_{series}_{climate_day}"

    import config as _cfg
    if not getattr(_cfg, "USE_PUSH_WINDOW_OVERRIDES", True):
        # Master kill-switch: push window system disabled. Intentional (not a
        # data gap) -> no alert, no trade.
        return False, "push_window_system_disabled"

    try:
        month = int(climate_day.split("-")[1])
    except Exception:
        return False, f"bad_climate_day:{climate_day}"

    try:
        from push_window_overrides import PUSH_WINDOW_OVERRIDES
    except ImportError:
        _alert_missing_window(station, series, month, climate_day,
                              "push_window_overrides module failed to import")
        return False, "window_table_import_failed"

    win = PUSH_WINDOW_OVERRIDES.get((station, series, month))
    if win is None:
        # Window table is the SOLE source -- no default fallback. A cell with no
        # validated window is NOT traded, and we alert loudly (no silent gaps).
        _alert_missing_window(station, series, month, climate_day,
                              "cell absent from window table; NOT trading until table regenerated")
        return False, f"no_window_defined:{station}/{series}/m{month}"

    before, after = float(win[0]), float(win[1])
    # 2026-05-21 TEMP: pre-peak HIGH window from the 67-day price backtest (P1).
    # Beats the current at-peak windows (+559c vs -416c on settled 3/15-5/20).
    # Applied before early-trim; (1.5,-1.0) is trim-compatible so it survives.
    # Reversible via config.PUSH_HIGH_TEMP_WINDOW=None. Superseded by per-(station,
    # month) regen once the full historical backfill lands.
    _temp_win = getattr(_cfg, "PUSH_HIGH_TEMP_WINDOW", None)
    # 2026-05-24: per-station month override (KMDW/KBOS use their price window in Mar+Apr
    # too -- their MAE override windows open near/post-peak and lose in the live era).
    # Station absent -> global PUSH_TEMP_WINDOW_MONTHS. Reversible: empty the by-station dict.
    _tw_months = (getattr(_cfg, "PUSH_TEMP_WINDOW_MONTHS_BY_STATION", {}) or {}).get(
        station, getattr(_cfg, "PUSH_TEMP_WINDOW_MONTHS", {5}))
    if series == "HIGH" and _temp_win and month in _tw_months:
        # 2026-05-22: per-station HIGH window (price-gated backtest, v1).
        # Looked up first; station absent -> global temp window above.
        # Reversible by clearing PUSH_HIGH_TEMP_WINDOW_BY_STATION.
        _by_stn = getattr(_cfg, "PUSH_HIGH_TEMP_WINDOW_BY_STATION", None) or {}
        # 2026-05-30 (Chris): NO DEFAULT WINDOW. A HIGH station with no explicit
        # per-station entry is NOT traded (loud alert) -- mirrors the
        # PUSH_WINDOW_OVERRIDES missing-cell rule. Removes the silent global-default
        # fallback whose ambiguity hid the 5/29 shallowing (logged to README, never
        # applied to config).
        if station not in _by_stn:
            _alert_missing_window(station, series, month, climate_day,
                "HIGH station absent from PUSH_HIGH_TEMP_WINDOW_BY_STATION (no default window) -- NOT trading")
            return False, f"no_explicit_high_window:{station}/m{month}"
        _w = _by_stn[station]
        before, after = float(_w[0]), float(_w[1])
    _low_temp = getattr(_cfg, "PUSH_LOW_TEMP_WINDOW", None)
    if series == "LOW" and _low_temp and month in getattr(_cfg, "PUSH_TEMP_WINDOW_MONTHS", {5}):
        # 2026-05-22: LOW placeholder window. MAE-built LOW overrides open
        # too deep pre-min (h2pk>=2.0 = 40% WR faithful); good zone is
        # near/post-min (65%). Placeholder until LOW candles land for a
        # price-gated regen. Reversible: clear PUSH_LOW_TEMP_WINDOW.
        _low_by = getattr(_cfg, "PUSH_LOW_TEMP_WINDOW_BY_STATION", None) or {}
        _lw = _low_by.get(station, _low_temp)
        before, after = float(_lw[0]), float(_lw[1])
    # 2026-05-21: early-side trim for HIGH accurate-but-wide cells. The window
    # table is built on MAE (mu accuracy), but accuracy != PnL: in the ~40 HIGH
    # cells that are accurate (mae < MAE_MAX) yet open >1h before peak, the
    # early offsets are where the matcher hadn't seen enough of the day's curve
    # to call the bracket. Validated on 2024-2025 holdout (n=12,548): at offset
    # < -1.25 the matcher lands in the WRONG ~1F bracket 60% of the time and
    # misses by >=2F (Miami-scale) 32% of the time, vs 46%/16% in the [-1.0,0]
    # keep zone; 38 of 40 cells worse early. Live PnL (5/19-21, n=52) agreed.
    # So cap how early these cells open WITHOUT touching their `after` edge or
    # peak time (per-station shape preserved). Inaccurate wide cells (high mae)
    # are intentionally LEFT ALONE -- MAE-sizing already shrinks those bets and
    # narrowing an unpredictable cell adds nothing. mae is win[3] (4-tuple).
    _trim_dbg = ""
    if (series == "HIGH"
            and getattr(_cfg, "PUSH_EARLY_TRIM_HIGH_ENABLED", True)
            and len(win) >= 4 and win[3] is not None):
        _cap = float(getattr(_cfg, "PUSH_EARLY_TRIM_BEFORE_CAP", 1.0))
        _mae_max = float(getattr(_cfg, "PUSH_EARLY_TRIM_MAE_MAX", 1.6))
        # Preserve a minimum 0.5h window (mirrors the generator's MIN_WIN_WIDTH_H):
        # when `after` < 0 the window closes before peak, so a flat cap to 1.0 can
        # collapse it to zero width and SILENTLY disable the cell -- e.g. KLAX/KATL
        # HIGH (2.0,-1.0) -> [peak-1.0, peak-1.0]. Cap to max(1.0, 0.5 - after) so
        # the post-trim width stays >= 0.5h.
        _eff_cap = max(_cap, 0.5 - after)
        if float(win[3]) < _mae_max and before > _eff_cap:
            _trim_dbg = f" early_trim:before {before}->{_eff_cap}(mae={win[3]})"
            before = _eff_cap
    # 2026-06-02: blend deep-window override (project_blend_edge_FOUND). The blend
    # edge is concentrated 2.5-4h before peak (market soft far from peak, sharp into
    # it); near/post-peak ~dead. When the blend is active, trade HIGH only in the
    # deep window, overriding the table windows. Backtest (climo-peak, fwd-chain,
    # $10 liquid): -3h +$3257/WR.59 vs -2h +$1980 vs -1h +$280/WR.37.
    if (getattr(_cfg, "BLEND_FORECAST_ENABLED", False)
            and getattr(_cfg, "BLEND_DEEP_WINDOW_ENABLED", True)):
        _dwh = None
        if series == "HIGH":
            # HIGH: deep window (edge 2-3x bigger 3-4h before peak).
            _dwh = getattr(_cfg, "BLEND_DEEP_WINDOW_HOURS", (4.0, 2.5))
        elif series == "LOW" and getattr(_cfg, "BLEND_FORECAST_LOW_ENABLED", False):
            # LOW: edge peaks ~2h before min (deeper does NOT help, unlike HIGH);
            # concentrate around the min-2h sweet spot, cut the weak near-min bets.
            _dwh = getattr(_cfg, "BLEND_DEEP_WINDOW_HOURS_LOW", (3.0, 1.5))
        if _dwh:
            before, after = float(_dwh[0]), -float(_dwh[1])
            _trim_dbg += f" blend_deep[{series} peak-{_dwh[0]},peak-{_dwh[1]}]"
    lo = peak - before
    hi = peak + after
    ok = (lo <= local_hour <= hi)
    return ok, f"peak={peak} window=[{lo:.1f},{hi:.1f}] cur={local_hour:.2f} src=window_table{_trim_dbg}"


# ─────────────────────────────────────────────────────────────────────────────
# Matcher PAPER book (2026-06-02, Chris) — the nn_match fallback is blocked from
# REAL orders by BLEND_ONLY_EXECUTION, so paper-trade it to an ISOLATED log to
# later judge whether it has live edge. Fully separate from _rt.positions / cash /
# caps / execute_buy — zero impact on the blend's real trading.
# ─────────────────────────────────────────────────────────────────────────────
_paper_positions: set = set()
_paper_log_ts = [0.0]

def _fmt_local_time(lh):
    """Local clock hour float (e.g. 13.52) -> 'HH:MM', or None."""
    if lh is None:
        return None
    try:
        lh = float(lh) % 24.0
        h = int(lh)
        m = int(round((lh - h) * 60))
        if m == 60:
            h = (h + 1) % 24
            m = 0
        return f"{h:02d}:{m:02d}"
    except (TypeError, ValueError):
        return None

def _paper_record_entry(cand, packet, decision, direction, ask_c_i, series, edge_pp):
    """Append one isolated matcher PAPER entry (no real order/position/cash). One
    row per ticker; joined to Kalshi settlement later for paper P&L analysis."""
    try:
        lc = packet.get("local_clock") or {}
        rec = {
            "ts": time.time(),
            "kind": "paper_entry",
            "book": "matcher_paper",
            "ticker": cand.ticker,
            "station": cand.station,
            "series": cand.series_prefix,
            "climate_day": cand.climate_day,
            "bracket_kind": cand.bracket_kind,
            "floor": cand.floor,
            "cap": cand.cap,
            "label": getattr(cand, "bracket_label", None),
            "action": direction,
            "entry_price": round(ask_c_i / 100.0, 4),
            "market_price_c": ask_c_i,
            "mu_method": packet.get("mu_method"),
            "mu_chosen": packet.get("mu_chosen"),
            "sigma_chosen": packet.get("sigma_chosen"),
            "edge_pp": round(edge_pp, 2),
            "p_yes": decision.get("p_yes"),
            "h_to_peak": lc.get("h_to_peak"),
            "local_hour": lc.get("local_hour"),
            "local_time": _fmt_local_time(lc.get("local_hour")),
        }
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "paper_trades.jsonl")
        with open(path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        if (time.time() - _paper_log_ts[0]) > 60:
            _paper_log_ts[0] = time.time()
            log.info("PAPER matcher entry %s %s @%dc edge=%.1fpp h2p=%s",
                     direction, cand.ticker, ask_c_i, edge_pp, lc.get("h_to_peak"))
    except Exception:
        log.exception("paper record failed for %s", getattr(cand, "ticker", "?"))


# ─────────────────────────────────────────────────────────────────────────────
# One-bracket-per-station-day selection (PUSH_ONE_BRACKET_PER_STATION_HIGH).
# The blend mu/sigma is shared across a station-day's brackets, so a sibling's
# edge is a cheap normal-CDF integral (mirrors nn_shadow_strategy._yes_window:
# B=[floor-0.5, cap+0.5); T-warm(floor only)=[floor+0.5, inf); T-cold(cap only)=
# (-inf, cap-0.5)). Both the self and sibling edges go through _bracket_edge_pp
# so the comparison is apples-to-apples (independent of the decision's own edge).
# ─────────────────────────────────────────────────────────────────────────────
def _bracket_edge_pp(floor, cap, yes_bid_c, yes_ask_c, mu, sigma) -> Optional[float]:
    """Best-side edge (in pp) for one bracket under blend mu/sigma. None if uncomputable.
    Bracket shape is inferred from floor/cap presence (B = both; T-warm = floor only;
    T-cold = cap only), matching the (2t) tail-bet gate's _yes_window convention."""
    import math as _math
    try:
        mu = float(mu); sigma = float(sigma)
        ya_c = float(yes_ask_c); yb_c = float(yes_bid_c)
    except (TypeError, ValueError):
        return None
    if not (sigma > 0) or not (0 < ya_c <= 100) or ya_c <= yb_c:
        return None

    def _phi(x):
        return 0.5 * (1.0 + _math.erf(x / _math.sqrt(2.0)))

    fl = None if floor is None else float(floor)
    cp = None if cap is None else float(cap)
    if fl is not None and cp is not None:
        p_yes = _phi((cp + 0.5 - mu) / sigma) - _phi((fl - 0.5 - mu) / sigma)
    elif fl is not None:                       # T-warm (>= floor)
        p_yes = 1.0 - _phi((fl + 0.5 - mu) / sigma)
    elif cp is not None:                       # T-cold (<= cap)
        p_yes = _phi((cp - 0.5 - mu) / sigma)
    else:
        return None
    no_ask_c = 100.0 - yb_c
    edge_yes = p_yes - ya_c / 100.0
    edge_no = (1.0 - p_yes) - no_ask_c / 100.0
    return max(edge_yes, edge_no) * 100.0


def _max_sibling_edge_pp(cand, mu, sigma) -> tuple[Optional[float], Optional[str]]:
    """Max best-side edge (pp) among this station-day's OTHER currently-quoted brackets,
    using the shared blend mu/sigma. Returns (max_edge_pp, ticker) or (None, None).
    Mirrors _compute_market_mu's BBO-cache enumeration + 10-min staleness bound. Called
    OUTSIDE _pending_buys_lock (read-only on the WS cache) to keep the lock hold brief."""
    if mu is None or sigma is None:
        return None, None
    prefix = cand.series_prefix
    best_pp = None
    best_tk = None
    now = time.time()
    try:
        for tk in list(kalshi_ws._bbo_cache.keys()):
            if tk == cand.ticker or not tk.startswith(prefix):
                continue
            c2 = market_universe.parse_ticker(tk)
            if not c2 or c2.station != cand.station or c2.climate_day != cand.climate_day:
                continue
            ce = kalshi_ws._bbo_cache.get(tk)
            if not ce or (now - ce.get("ts", 0) > 600):
                continue
            yb = ce.get("yes_bid")
            ya = ce.get("yes_ask")
            if yb is None or ya is None:
                continue
            e_pp = _bracket_edge_pp(c2.floor, c2.cap,
                                    yb * 100.0, ya * 100.0, mu, sigma)
            if e_pp is None:
                continue
            if best_pp is None or e_pp > best_pp:
                best_pp = e_pp
                best_tk = tk
    except Exception:
        return None, None
    return best_pp, best_tk


def _irreversible_no_lock(packet: dict, cand) -> tuple[bool, str]:
    """2026-06-10: IRREVERSIBLE-lock detector for the locked-only trading mode.

    Returns (locked, dbg). True ONLY when the climate-day-validated running
    extreme has already physically killed this bracket's YES in a way no later
    weather can undo (running max only rises / running min only falls):

        HIGH BUY_NO: rm >= cap   + PUSH_IRREV_LOCK_BUFFER_F   (blew out the top)
        LOW  BUY_NO: rm <= floor - PUSH_IRREV_LOCK_BUFFER_F   (broke below)

    The 1.0F default buffer is the household OBS-CONFIRMED-LOSER standard (obs
    typically reads ~1F hot vs CLI settlement; the buffer absorbs that gap).
    The REVERSIBLE lock flavors (HIGH stays-below past-peak, LOW stays-above
    past-min) are deliberately NOT accepted -- those are forecasts that the
    extreme is in ("premature lock": the locklag KATL 6/5 + judge DEN-T89 6/9
    failure shape), not physics. T-tails on the open side (HIGH warm tail /
    LOW cold tail) can never be NO-locked -- an overshoot there makes YES
    CERTAIN, so they return False.
    """
    import config as _cfg
    if packet.get("days_out") != 0:
        return False, "not_d0"
    rm = packet.get("running_min_or_max")
    if rm is None:
        return False, "rm_none"
    try:
        rm = float(rm)
    except (TypeError, ValueError):
        return False, "rm_not_numeric"
    buf = float(getattr(_cfg, "PUSH_IRREV_LOCK_BUFFER_F", 1.0))
    fl = packet.get("floor")
    if fl is None:
        fl = getattr(cand, "floor", None)
    cp = packet.get("cap")
    if cp is None:
        cp = getattr(cand, "cap", None)
    prefix = getattr(cand, "series_prefix", "") or ""
    try:
        if prefix == "KXHIGH":
            if cp is None:
                return False, "high_no_cap_tail"  # T-warm: overshoot = YES certain, never NO
            if rm >= float(cp) + buf:
                return True, f"HIGH rm={rm:.1f}>=cap+{buf:.1f}({float(cp)+buf:.1f})"
            return False, f"high_rm {rm:.1f}<cap+{buf:.1f}"
        if prefix == "KXLOW":
            if fl is None:
                return False, "low_no_floor_tail"  # T-cold: undershoot = YES certain, never NO
            if rm <= float(fl) - buf:
                return True, f"LOW rm={rm:.1f}<=floor-{buf:.1f}({float(fl)-buf:.1f})"
            return False, f"low_rm {rm:.1f}>floor-{buf:.1f}"
    except (TypeError, ValueError):
        return False, "bad_floor_cap"
    return False, f"unknown_series:{prefix}"


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
      3. (station, series, hour) inside the per-(station, series, month)
         window from push_window_overrides.PUSH_WINDOW_OVERRIDES (the SOLE
         window source as of 2026-05-21; no default fallback -- a missing cell
         is not traded + Discord-alerted). Peak hour from _lookup_peak_hour.
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
    # (Gate -1) IRREVERSIBLE-LOCK-ONLY mode (2026-06-10, Chris: "make it trade with
    # something NEW that could bring profit"). The blend's mu-vs-market thesis is
    # live-refuted (6/2-6/9 = -$156, every cell negative), so when this flag is on
    # the bot trades ONLY the mechanism with live proof next door (locklag's): an
    # obs-DETERMINED outcome the market hasn't fully repriced. BUY_NO only, and only
    # when the validated running extreme has IRREVERSIBLY killed the bracket
    # (_irreversible_no_lock above). mu/sigma are irrelevant for these rows (the rm
    # truncation alone forces P(NO)=1), so locked rows bypass the mu-QUALITY gates
    # below (blend-only, decision window, thin-margin, sigma floor/ceiling, LOW
    # front-wind, off-peak veto) while keeping every MARKET/EXEC gate (edge floor,
    # spread, price band incl. the 50c locked floor, dedup, position caps, cash,
    # one-bracket cap, sizing, maker/taker exec). Flag off -> legacy path, no
    # locked special-casing anywhere.
    _irrev = False
    if getattr(_cfg, "PUSH_IRREV_LOCK_ONLY", False):
        if direction != "BUY_NO":
            return False, "irrev_lock_only: BUY_NO only"
        _irrev, _irrev_dbg = _irreversible_no_lock(packet, cand)
        if not _irrev:
            return False, f"irrev_lock_only: not locked ({_irrev_dbg})"
        packet["irrev_locked"] = True
        packet["irrev_lock_dbg"] = _irrev_dbg
        try:
            decision["reason"] = f"{decision.get('reason') or ''} IRREV-LOCKED[{_irrev_dbg}]"
        except Exception:
            pass
    # (Gate 0) BLEND-ONLY EXECUTION (2026-06-02, Chris). Only place orders when the
    # forecast came from the BLEND (the validated edge, mu_method="blend_*"). The
    # nn_match matcher still runs as a shadow/fallback mu (and is logged) but does
    # NOT trade -- it has no proven live edge, and it only becomes the active mu when
    # _compute_market_mu is None, i.e. on thin/illiquid or post-peak markets where the
    # blend can't price and there's no edge anyway. Reversible: BLEND_ONLY_EXECUTION=False.
    paper_mode = False
    # locked rows are mu-source-agnostic (P(NO)=1 from the rm truncation), so the
    # blend-only gate does not apply to them -- a matcher-fallback mu still trades.
    if not _irrev and getattr(_cfg, "BLEND_ONLY_EXECUTION", True):
        _mm = str(packet.get("mu_method") or "")
        if not _mm.startswith("blend_"):
            # 2026-06-02 (Chris): route the nn_match matcher fallback to an ISOLATED
            # PAPER book instead of hard-blocking it, so we can later analyze whether
            # it has live edge. Paper rows flow through the SAME signal gates below
            # (edge/window/price/spread/sigma/tail) but divert at the dedup step,
            # BEFORE any _rt.positions / cash / cap / execute_buy code -> zero impact
            # on the blend's real trading. Only genuine matcher mu is papered; a
            # missing/other mu_method is still hard-blocked.
            if (getattr(_cfg, "MATCHER_PAPER_ENABLED", False)
                    and _mm.startswith("nn_match_")):
                paper_mode = True
            else:
                return False, f"blend_only: mu_method={_mm or 'none'} (matcher fallback not executed)"
    short_dir = "NO" if direction == "BUY_NO" else "YES"
    # (Gate 1.5) LOW = B-NO ONLY. 2026-06-06 backtest (low_tight.py, 2mo / 943 trades /
    # 20 stations): B-NO is the ONLY +EV LOW cell (+2.8-3.3c/ct at >=8pp edge, both
    # halves +, leave-one-station-out +2.0..+4.8c, reconstructed mu MAE 1.66F < market
    # 1.98F). B-YES (-2.7c), T-NO (-5.4c), T-YES (-5.7c) are ALL robustly -EV both halves.
    # Mechanism: the blend rules OUT brackets well (NO wins ~71%) but can't pinpoint the
    # winning 2F bin (YES wins ~24%); T-tails are noise. Drop LOW YES + all LOW T-tails.
    # Flag-gated (PUSH_LOW_B_NO_ONLY); HIGH unaffected. Rollback: flag=False.
    if cand.series_prefix == "KXLOW" and getattr(_cfg, "PUSH_LOW_B_NO_ONLY", False):
        if direction == "BUY_YES":
            return False, "low_b_no_only: LOW YES dropped (-2.7c/ct backtest)"
        if cand.bracket_kind != "B":
            return False, f"low_b_no_only: LOW T-tail dropped ({cand.bracket_kind}, -5c/ct)"
    # (Gate 1.6) LOW P(NO) floor (2026-06-10 LOW deep-dive). A cheap ask can
    # manufacture "edge" while the model itself is ~coinflip (DAL 6/10: P(NO)=0.52
    # at 28c = 23.6pp "edge", lost -- the recurring LOW loss shape). Require the
    # model to actually lean NO before shorting a bracket. Stream-swept on 4452
    # blend-era B-NO decision rows: P(NO)>=0.55 flips the kept book +1.4 ->
    # +9.4c/ct; the losing thesis classes (cheap falls-past / post-min) sit below
    # it. LOW BUY_NO only; PUSH_LOW_MIN_PNO=0 disables.
    if cand.series_prefix == "KXLOW" and direction == "BUY_NO":
        _pno_min = float(getattr(_cfg, "PUSH_LOW_MIN_PNO", 0.0))
        _py_f = decision.get("p_yes")
        if _pno_min > 0 and _py_f is not None:
            try:
                if (1.0 - float(_py_f)) < _pno_min:
                    return False, f"low_pno_floor P(NO)={1.0-float(_py_f):.2f}<{_pno_min:.2f}"
            except (TypeError, ValueError):
                pass
    # 2026-06-09 (Claude, Chris-approved): SUMMER HIGH NO-only. Mirror of the LOW
    # B-NO-only gate above, for the middle-path size-up to $3: concentrate the larger
    # size on the tail-robust NO side. Audit (frozen prod model, 427 summer recon days):
    # NO-only >=10pp = +9.4c/ct/66%WR, both halves +, LOSO all 7 folds +, robust to a
    # blowup-day rate up to ~37%; live ex-6/4 +11.4c/ct. YES is marginally +EV in recon
    # (+9.5c/ct) but is the forecast-miss-day tail amplifier live (6/4: YES -24c/ct vs
    # NO -7.7) -> dropped at 3x size. Keeps HIGH NO on BOTH B and T (high-edge T-tail NO
    # is fine). Flag-gated; rollback ->False (restore YES) in fall with the seasonal swap.
    if cand.series_prefix == "KXHIGH" and getattr(_cfg, "PUSH_HIGH_NO_ONLY", False):
        if direction == "BUY_YES":
            return False, "high_no_only: HIGH YES dropped (summer size-up, NO-only core)"
    # (Gate 2) Edge floor — bot only fires above PUSH_MIN_EDGE_PP. The
    # nn_shadow_strategy.pure_nn_decide internal floor stays at 6pp so the
    # shadow log keeps logging marginal-edge candidates for diagnostics.
    min_edge_pp = int(getattr(_cfg, "PUSH_MIN_EDGE_PP", 12))
    # 2026-05-28 (Chris): side-specific YES edge floor. NO floor unchanged;
    # 12-18pp YES is +EV on pooled real fills (n=12, 67%WR, +17.9c/ct). Tail-bet gate still stacks.
    # 2026-06-06 (Chris): LOW needs a MUCH higher edge bar than HIGH. Backtest: at HIGH's
    # 2pp bar EVERY LOW cell is -EV incl B-NO (-0.4c); B-NO turns +EV both-halves only at
    # >=8pp (low_tight.py edge sweep). LOW is B-NO-only (gate above) so this is its bar.
    if cand.series_prefix == "KXLOW":
        min_edge_pp = int(getattr(_cfg, "PUSH_MIN_EDGE_PP_LOW", min_edge_pp))
    elif direction == "BUY_YES":
        min_edge_pp = int(getattr(_cfg, "PUSH_MIN_EDGE_PP_YES", min_edge_pp))
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
    # 2026-05-22: LOW auto-exec PAUSED (over-trading pre-dawn into illiquid
    # books -> phantom MTM). Shadow-eval still logs; HIGH unaffected.
    if series == "LOW" and not getattr(_cfg, "AUTO_EXEC_LOW_ENABLED", True):
        return False, "low_auto_exec_paused"
    # 2026-05-25: HIGH BUY_YES PAUSED -- backtest 5/19-5/23 (n=22) shows it is a
    # structural loser (36% win, -20% ROI) vs HIGH BUY_NO (the edge). lift +$27,
    # helps:hurts 14:8. Shadow-eval still logs; HIGH BUY_NO + LOW probe unaffected.
    if series == "HIGH" and direction == "BUY_YES" and not getattr(_cfg, "AUTO_EXEC_HIGH_YES_ENABLED", True):
        return False, "high_buy_yes_paused"
    # 2026-05-25 (Chris): per-station HIGH bench. Stations in PUSH_HIGH_DISABLED_STATIONS
    # had no +EV window at ANY offset in the last-month faithful regen (e.g. KSFO -26c).
    # Skip HIGH push entirely rather than trade a least-bad loser. LOW unaffected.
    if series == "HIGH" and cand.station in (getattr(_cfg, "PUSH_HIGH_DISABLED_STATIONS", frozenset()) or frozenset()):
        return False, f"high_station_benched:{cand.station}"
    # 2026-05-25 (Chris): NWP-agreement gate (HIGH). k-NN mu blows up 5-6F on bad
    # days; the independent NBM/HRRR/ECMWF mu does not. Skip when they disagree by
    # more than MU_AGREEMENT_MAX_DIFF_F. Phase-1 5/19-5/21: agree<=2F kept +23%
    # ROI vs disagree>2F -34%. Fail-OPEN: if mu_nwp unavailable, do not gate.
    # 2026-05-26: SYMMETRIC -- gate now fires on |disagree| > thr (was: only
    # positive direction). 5/23-5/24 calibration-failure deep-dive: matcher
    # UNDER-predicted highs (matcher p_yes=20% vs actual yes_rate=75%), so the
    # bad-day signature was matcher COLDER than NWP, not hotter. One-sided gate
    # would have missed it. Symmetric form catches both regimes.
    if series == "HIGH" and getattr(_cfg, "USE_MU_AGREEMENT_GATE", False):
        _nd = packet.get("nwp_disagree")
        _thr = float(getattr(_cfg, "MU_AGREEMENT_MAX_DIFF_F", 2.0))
        if _nd is not None and abs(_nd) > _thr:
            # 2026-05-26 (rm carve-out): if today's actual high SO FAR (rm) has
            # already reached/exceeded the NWP forecast, the NWP blend is
            # PROVABLY too low and must not veto an even-higher matcher mu. This
            # fires only when the matcher is the HOTTER side (mu >= rm by the
            # physical max-floor in nn_match_fast), so it does NOT weaken the
            # symmetric gate's matcher-COLDER-than-NWP protection. Diagnosed
            # 2026-05-26: a low-NWP hot day where rm beat the NWP forecast
            # pre-peak on DFW/OKC/DEN/SFO/LAX while the 2.0F gate blocked 100%.
            _rm = packet.get("running_min_or_max")
            _mn = packet.get("mu_nwp")
            _nwp_proven_low = (_rm is not None and _mn is not None
                               and float(_rm) >= float(_mn))
            if _nwp_proven_low:
                packet["nwp_gate_rm_override"] = True
            else:
                return False, f"nwp_disagree |{_nd:+.1f}F|>{_thr:.1f}F (mu_nwp={packet.get('mu_nwp')})"
    # (2-mae) Per-cell reliability gate. Skip when the matcher's HISTORICAL MAE
    # for this (station, season, local_hour, side) cell exceeds PUSH_MAE_GATE_F
    # -- the k-NN projection is provably unreliable there (e.g. KMSP/KAUS morning
    # HIGH MAE ~5F vs KLAS/KMSY late-afternoon HIGH ~0.5F). Backtest settled
    # 2026-05-14..24 (n=315): gating MAE>2.0F lifts realized P&L +$23 (both
    # date-halves +). MAE table (cell_mae_table) is OOS 2022-2025. Distinct from
    # PUSH_MAE_CONF_TIERS (size shrink); this hard-SKIPs. Fail-OPEN: unknown cell
    # (n<20/missing) -> not gated. Sigma calibration was tried first + rejected
    # (variance transforms can't separate the BUY_NO winners/losers; this can).
    if getattr(_cfg, "PUSH_MAE_GATE_ENABLED", False):
        _mae_thr = float(getattr(_cfg, "PUSH_MAE_GATE_F", 2.0))
        _cell_mae = None
        try:
            import cell_mae_table as _cmt
            _mn = int(str(cand.climate_day)[5:7])
            _cell_mae = _cmt.cell_mae(
                cand.station, _mn, local_hour,
                "high" if series == "HIGH" else "low")
        except Exception:
            _cell_mae = None
        if _cell_mae is not None and _cell_mae > _mae_thr:
            return False, (f"cell_mae_gate {_cell_mae:.2f}F>{_mae_thr:.1f}F "
                           f"({cand.station}/{series}/h{int(local_hour)})")
    # (2) Decision window — peak-relative per (station, month, series).
    # IRREV-LOCKED bypass: an irreversible lock is valid at ANY hour (the window
    # exists because forecast quality decays with lead; a lock is not a forecast).
    if _irrev:
        in_win, win_dbg = True, "irrev_locked_bypass"
    else:
        in_win, win_dbg = _in_decision_window(cand.station, series, local_hour, cand.climate_day)
        if not in_win:
            return False, f"outside_window {cand.station}/{series}/{short_dir}: {win_dbg}"
    # (2-spread) HIGH-only spread gate: crossing a wide bid-ask to buy pays
    # away the edge -- backtest HIGH spread>15c loses -21..-31c/bet vs +1.9c
    # filtered (both date-halves OOS). LOW left unfiltered ($1 live probe).
    if series == "HIGH":
        _msp = float(getattr(_cfg, "PUSH_MAX_SPREAD_C_HIGH", 0) or 0)
        if _msp > 0:
            _yb = packet.get("yes_bid_c"); _ya = packet.get("yes_ask_c")
            if _yb is not None and _ya is not None and (_ya - _yb) > _msp:
                return False, f"spread_too_wide {_ya - _yb:.0f}c>{_msp:.0f}c"
    # (2-spread-low) LOW spread gate (2026-06-02). Under the blend the LOW edge is
    # liquidity-gated: faithful fwd-chain sim of the live config shows LOW BUY_NO
    # at spread==1c = +$154/n16/WR.75, but spread>=2c flips negative (2c -$52/n12,
    # 3c+ noise/neg). Overnight KXLOW books are thin; crossing a >1c spread pays
    # away the edge + adverse selection. Mirror of the HIGH spread gate (which the
    # LOW book lacked from its "$1 probe" era). Thin n -> ship-small/monitor;
    # PUSH_MAX_SPREAD_C_LOW=0 disables. cf project_blend_edge_FOUND 2026-06-02.
    if series == "LOW":
        _msp_low = float(getattr(_cfg, "PUSH_MAX_SPREAD_C_LOW", 0) or 0)
        if _msp_low > 0:
            _yb = packet.get("yes_bid_c"); _ya = packet.get("yes_ask_c")
            if _yb is not None and _ya is not None and (_ya - _yb) > _msp_low:
                return False, f"spread_too_wide_low {_ya - _yb:.0f}c>{_msp_low:.0f}c"
    # (2d) HIGH-only thin-margin BUY_NO gate. Skip a B-bracket BUY_NO when the
    # CLI-adjusted forecast (mu - per-station obs->CLI offset) lands INSIDE the
    # bracket [floor - band, cap + band] -- shorting a bracket our own mu points
    # into. Band default 1.5°F (was 0.5°F pre-2026-05-26), per-station override
    # in PUSH_NO_MU_BOUNDARY_BAND_BY_STATION. Live-era 8-day EXEC pool: widening
    # from 0.5°F to 1.5°F lifts HIGH BUY_NO from +$8.69 to +$58.45 (lift $+49.76);
    # per-station tuning lifts further to +$63.79. WR 55%->66% on both pools.
    if series == "HIGH" and direction == "BUY_NO" and not _irrev and getattr(
            _cfg, "PUSH_SKIP_NO_MU_NEAR_BRACKET", False):
        _fl = packet.get("floor"); _cp = packet.get("cap"); _mu = packet.get("mu_chosen")
        if _fl is not None and _cp is not None and _mu is not None:
            try:
                _off = float(getattr(_cfg, "PUSH_NO_MU_CLI_OFFSET_BY_STATION", {}).get(
                    cand.station, getattr(_cfg, "PUSH_NO_MU_CLI_OFFSET_DEFAULT", 0.5)))
                _band = float(getattr(_cfg, "PUSH_NO_MU_BOUNDARY_BAND_BY_STATION", {}).get(
                    cand.station, getattr(_cfg, "PUSH_NO_MU_BOUNDARY_BAND_DEFAULT", 1.5)))
                if (float(_fl) - _band) <= (float(_mu) - _off) <= (float(_cp) + _band):
                    return False, (f"thin_margin_no mu={float(_mu):.1f}-off{_off:+.1f} "
                                   f"in[{float(_fl)-_band:.1f},{float(_cp)+_band:.1f}] band={_band:.1f}")
            except (TypeError, ValueError):
                pass
    # (2d2) HIGH B-bracket "WON'T-REACH" NO veto (2026-06-10 HIGH deep-dive).
    # A B-bracket BUY_NO with mu BELOW the bracket (mu < floor-0.5) bets the
    # day's run-up falls short -- a forecast-ceiling call with ZERO obs support
    # (the running-max ratchet can only hurt it; one hot hour kills it). The
    # decision-stream replay (38k blend NO rows, 374 tickers, all-markets
    # settled results, current gates): wont-reach = -32.3c/ct NEGATIVE IN ALL 4
    # SPLITS (n=10) vs blows-past +10.0 / T-tail +18.8 (both all-splits +);
    # dropping it lifts the kept book +7.8 -> +14.1c/ct (n=43, every split
    # improved). The 6/9 SEA/ATL/DEN losers were exactly this shape. With the
    # thin-margin gate above, a B-NO now requires mu ABOVE cap+band ("the heat
    # blows past this bracket"). T-tail shorts unaffected (deep-margin tail NOs
    # are +EV with USE_TAIL_EMPIRICAL_PYES). Rollback -> flag False.
    if (series == "HIGH" and direction == "BUY_NO" and not _irrev
            and getattr(_cfg, "PUSH_HIGH_NO_SKIP_WONT_REACH", False)):
        _fl3 = packet.get("floor"); _cp3 = packet.get("cap"); _mu3 = packet.get("mu_chosen")
        if _fl3 is not None and _cp3 is not None and _mu3 is not None:
            try:
                if float(_mu3) < float(_fl3) - 0.5:
                    return False, (f"wont_reach_no mu={float(_mu3):.1f} < floor-0.5 "
                                   f"({float(_fl3)-0.5:.1f}) -- no-obs-support ceiling bet")
            except (TypeError, ValueError):
                pass
    # (2d3) Blows-past CLEARANCE floor (2026-06-11, the DEN-B76.5 loss shape).
    # A B-bracket NO with mu only marginally above the bracket top is a coin
    # flip wearing a confident price: stream clearance bands (38k rows) --
    # clear [0,0.5)F = -7.8c/ct WR33, [0.5,1.0) split-unstable, >=1.0F =
    # +15.1c/ct WR83 positive all 4 splits. Require mu >= cap + 0.5 +
    # PUSH_HIGH_NO_MIN_CLEARANCE_F ("the heat clears the bracket top by a full
    # degree", ~0.85 sigma). Subsumes/extends the wont-reach + thin-margin
    # geometry on the upper side. B-brackets only; 0 disables.
    if series == "HIGH" and direction == "BUY_NO" and not _irrev:
        _clr_min = float(getattr(_cfg, "PUSH_HIGH_NO_MIN_CLEARANCE_F", 0.0))
        if _clr_min > 0:
            _fl4 = packet.get("floor"); _cp4 = packet.get("cap"); _mu4 = packet.get("mu_chosen")
            if _fl4 is not None and _cp4 is not None and _mu4 is not None:
                try:
                    _clr = float(_mu4) - (float(_cp4) + 0.5)
                    if _clr < _clr_min:
                        return False, (f"bp_clearance mu={float(_mu4):.1f} clears cap+0.5 "
                                       f"by {_clr:+.2f}F < {_clr_min:.2f}F")
                except (TypeError, ValueError):
                    pass
    # (2d4) T-tail P(NO) floor (2026-06-11, the CHI-T86 loss shape). A cheap
    # T-tail ask can manufacture "edge" while the model is ~coinflip
    # (CHI 6/11: P(NO)=0.53 @28c, lost) -- the exact LOW pathology already
    # gated by PUSH_LOW_MIN_PNO. Historically FREE on the kept book (every
    # surviving T-NO had P(NO)>=0.75; n=0 below 0.60). T-brackets only
    # (B covered by the clearance gate above); 0 disables.
    if (series == "HIGH" and direction == "BUY_NO" and not _irrev
            and (packet.get("floor") is None or packet.get("cap") is None)):
        _tp_min = float(getattr(_cfg, "PUSH_HIGH_T_NO_MIN_PNO", 0.0))
        _pyv = decision.get("p_yes")
        if _tp_min > 0 and _pyv is not None:
            try:
                if (1.0 - float(_pyv)) < _tp_min:
                    return False, f"t_pno_floor P(NO)={1.0-float(_pyv):.2f}<{_tp_min:.2f}"
            except (TypeError, ValueError):
                pass
    # (2g) HIGH-only one-sided NBM veto for BUY_NO (JUDGE-ONLY, 2026-05-29).
    # The kNN matcher structurally under-projects hot days (cannot exceed its
    # historical analogs' deltas), so on heat it fires confident BUY_NO on
    # brackets the high actually reaches; NBM (independent, ignored by the
    # matcher) catches this. Skip BUY_NO when NBM's daily-high lands in
    # [floor - LO_MARGIN, cap]. Settled backtest @judge lead (peak-1.75h, CLI):
    # band = -5..-15c/bet WR.44-.58, DISTINCT from the (2d) mu thin-margin gate
    # (catches mu-far / matcher-cold cases (2d) misses); kept book flips +.
    # Thin n (~26 incremental settled bets; v1 no OOS half) -> behind a flag.
    if series == "HIGH" and direction == "BUY_NO" and getattr(
            _cfg, "PUSH_HIGH_NO_NBM_VETO_ENABLED", False):
        _fl2 = packet.get("floor"); _cp2 = packet.get("cap"); _nbm = packet.get("nbm_high")
        if _fl2 is not None and _cp2 is not None and _nbm is not None:
            try:
                _lo_m = float(getattr(_cfg, "PUSH_HIGH_NO_NBM_VETO_LO_MARGIN_F", 2.0))
                if (float(_fl2) - _lo_m) <= float(_nbm) <= float(_cp2):
                    return False, (f"nbm_veto nbm={float(_nbm):.1f} in "
                                   f"[{float(_fl2)-_lo_m:.1f},{float(_cp2):.1f}] "
                                   f"(matcher under-projects; NBM in/near bracket)")
            except (TypeError, ValueError):
                pass
    # (2e) HIGH BUY_NO σ floor -- skip when matcher's sigma_chosen is below
    # the configured threshold (matcher-overconfidence regime). 5/23-5/24
    # deep-dive: bad-day losers had σ avg 1.65 vs good-day winners 1.79;
    # σ < 1.0 isolates the extreme tail with 0 false positives in the sample.
    # Complements (2d) -- together they catch "μ near boundary" + "matcher
    # confident outside boundary". Applies to B and T HIGH BUY_NO alike.
    if series == "HIGH" and direction == "BUY_NO" and not _irrev:
        # 2026-05-28: per-station override extends the global floor at stations where
        # matcher σ is structurally under-calibrated (RMSz > 1.3 from 75-day phq backfill).
        _sig_floor_global = float(getattr(_cfg, "PUSH_HIGH_NO_MIN_SIGMA_F", 0.0))
        _sig_floor_by_st = getattr(_cfg, "PUSH_HIGH_NO_MIN_SIGMA_BY_STATION", {}) or {}
        # 2026-06-02: the per-station σ-floors (1.34-2.26) were calibrated for the
        # MATCHER's variable σ as an over-confidence filter. The blend emits a FIXED
        # calibrated σ (~1.17 HIGH) that is below all 7 per-station floors, so they
        # silently bench HIGH BUY_NO at KPHX/KSAT/KOKC/KLAX/KSEA/KMIA/KMDW when the
        # blend is live (faithful sim: ~+$110 left on the table). The blend's σ is
        # its own calibration, not matcher over-confidence, so exempt blend rows from
        # the per-station floors (keep the global PUSH_HIGH_NO_MIN_SIGMA_F=1.0 sanity
        # floor, which 1.17 clears). mu_pre_blend is set iff the blend override fired.
        # BLEND_EXEMPT_HIGH_SIGMA_FLOOR=False reverts. cf project_blend_edge_FOUND.
        if (packet.get("mu_pre_blend") is not None
                and getattr(_cfg, "BLEND_EXEMPT_HIGH_SIGMA_FLOOR", True)):
            _sig_floor = _sig_floor_global
        else:
            _sig_floor = float(_sig_floor_by_st.get(cand.station, _sig_floor_global))
        if _sig_floor > 0:
            _sig = packet.get("sigma_chosen")
            if _sig is not None:
                try:
                    if float(_sig) < _sig_floor:
                        return False, f"sigma_floor_no σ={float(_sig):.2f}<{_sig_floor:.2f}"
                except (TypeError, ValueError):
                    pass

    # (2f) HIGH BUY_NO σ ceiling -- mirror of (2e) at the opposite tail: skip
    # when sigma_chosen is ABOVE threshold (low-confidence / wide-analog regime,
    # matcher unsure the high won't reach the bracket -> shorting unreliable).
    # Real-trade validation (judge+v1max actual n=165, 2026-05-15..25): sigma>2.5
    # BUY_NO 25%WR -$2.14/bet, negative BOTH date-halves AND both bots; skip lifts
    # the BUY_NO book +$34. Complements the fit-quality (stdev_delta) gate. 0=off.
    if series == "HIGH" and direction == "BUY_NO" and not _irrev:
        _sig_ceil = float(getattr(_cfg, "PUSH_HIGH_NO_MAX_SIGMA_F", 0.0))
        if _sig_ceil > 0:
            _sig = packet.get("sigma_chosen")
            if _sig is not None:
                try:
                    if float(_sig) > _sig_ceil:
                        return False, f"sigma_ceiling_no σ={float(_sig):.2f}>{_sig_ceil:.2f}"
                except (TypeError, ValueError):
                    pass
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
    # (2c) LOW cold-front gate. Sustained wind >= ~15kt at an overnight LOW is
    # a frontal / cold-air-advection signature. The nn matcher (trained on calm
    # nights) over-projects the daily minimum by +1.5..+3°F -- and unlike
    # high-variance regimes its sigma does NOT widen to flag it (68% of these
    # rows backtest as sigma low/mid), so the bot trades a confident but wrong
    # estimate. 25-yr backtest (3.17M evals): LOW wind>15kt MAE 3.1-4.3 / bias
    # +1.6..+3.1 in cold season, cross-year validated 18/20 stations. HIGH is
    # storm-robust -> LOW-only. Sustained wind only (a gust without sustained
    # wind is convective, not frontal). KLAX/KMIA excluded -- marine climate,
    # strong wind there is onshore sea-breeze with no frontal bias. Fires a
    # throttled Discord alert (_alert_low_front, deduped per station/day).
    if series == "LOW" and not _irrev:  # locked = the min already happened; wind can't undo the past
        front_wind = float(getattr(_cfg, "PUSH_LOW_FRONT_WIND_MPH", 18.0))
        excl = getattr(_cfg, "PUSH_LOW_FRONT_EXCLUDE", ())
        if front_wind > 0 and cand.station not in excl:
            ws = wo.get("wind_speed_mph")
            try:
                ws_f = float(ws) if ws is not None else None
            except (TypeError, ValueError):
                ws_f = None
            if ws_f is not None and ws_f >= front_wind:
                _alert_low_front(cand.station, cand.climate_day, ws_f, front_wind)
                return False, (f"low_frontal_wind {ws_f:.1f}mph >= "
                               f"{front_wind:.0f}mph")
    # (3) Price floor/ceiling — entry must be in [min_c, max_c]
    # 2026-05-19 v3: BUY_YES gets a higher floor (cheap-YES lottery trap).
    max_c = int(getattr(_cfg, "PUSH_MAX_ENTRY_C", 90))
    if direction == "BUY_YES":
        min_c = int(getattr(_cfg, "PUSH_MIN_ENTRY_C_BUY_YES",
                            getattr(_cfg, "PUSH_MIN_ENTRY_C", 25)))
    elif cand.series_prefix == "KXLOW":
        # 2026-05-28 (Chris): the 50c BUY_NO floor (PUSH_MIN_ENTRY_C) is a HIGH-book
        # finding; applied to the LOW book it inverts (hurts PnL), so LOW uses its
        # own lower floor (PUSH_MIN_ENTRY_C_LOW=10 = pre-e5d6e01 behaviour).
        min_c = int(getattr(_cfg, "PUSH_MIN_ENTRY_C_LOW",
                            getattr(_cfg, "PUSH_MIN_ENTRY_C", 10)))
    else:
        min_c = int(getattr(_cfg, "PUSH_MIN_ENTRY_C", 10))
    # IRREV-LOCKED floor: a locked-NO offered below 50c means the market prices
    # >=50% that OUR OBS IS WRONG (the KMDW 6/9 under-read shape) -- RULE#2 says
    # the market wins that argument; walk away. Overrides the LOW 10c floor too.
    if _irrev:
        min_c = max(min_c, int(getattr(_cfg, "PUSH_IRREV_LOCK_MIN_ENTRY_C", 50)))
    ask_c = packet.get("yes_ask_c") if direction == "BUY_YES" else packet.get("no_ask_c")
    if ask_c is None:
        return False, f"no_ask_for_{direction}"
    try:
        ask_c_i = int(ask_c)
    except (TypeError, ValueError):
        return False, f"bad_ask_{direction}={ask_c}"
    if ask_c_i < min_c or ask_c_i > max_c:
        return False, f"price_oor ask={ask_c_i}c not in [{min_c},{max_c}]"
    # (3.5) HIGH off-peak ENTRY veto (near-peak only). Skip a NEW HIGH BUY once the
    # observed temp has fallen >= PUSH_HIGH_SKIP_IF_OFF_PEAK_F below the day's running
    # max AND we are within PUSH_HIGH_OFF_PEAK_MAX_H2PK hours of peak -- the daily high
    # is resolving, the market is sharp, and we'd only pay the spread. RULE#2-ALIGNED
    # (decline vs a sharp market; an ENTRY gate, NOT a sell). HIGH+BUY only; LOW + exits
    # untouched. drop = traj_max - cur_tmpf (matcher obs trajectory). The h_to_peak<=2
    # guard exempts the 4 deep windows (AUS/BOS/HOU/DFW enter at h2pk>=2.5, where a dip
    # is a cloud not a past-peak signal and those bets win). Fail-OPEN: missing
    # traj_max/cur_tmpf/h_to_peak -> not gated. DISTINCT from the nn_match peak-CLAMP
    # (that adjusts mu; this declines the entry) -> no double-veto. 0 disables.
    if series == "HIGH" and not _irrev:  # locked entries are typically post-drop; the veto is a mu-quality gate
        _off_peak_f = float(getattr(_cfg, "PUSH_HIGH_SKIP_IF_OFF_PEAK_F", 0.0))
        if _off_peak_f > 0:
            _tmax = packet.get("traj_max")
            _cur = packet.get("cur_tmpf")
            _h2pk = (packet.get("local_clock") or {}).get("h_to_peak")
            _max_h2pk = float(getattr(_cfg, "PUSH_HIGH_OFF_PEAK_MAX_H2PK", 2.0))
            if (_tmax is not None and _cur is not None
                    and _h2pk is not None and float(_h2pk) <= _max_h2pk):
                try:
                    _drop = float(_tmax) - float(_cur)
                    if _drop >= _off_peak_f:
                        return False, f"high_off_peak:{_drop:.1f}F@h2pk{float(_h2pk):.1f}"
                except (TypeError, ValueError):
                    pass
    # (4) Position dedup — never add to existing position on this exact ticker.
    # PAPER DIVERSION: a matcher paper row has now cleared every SIGNAL gate above
    # (edge/window/price/spread/sigma/tail). Dedup against the isolated paper book,
    # record it, and return HERE — before any real-state code (caps/cash/correlation/
    # execute_buy) runs. This is what keeps the paper book from touching real trading.
    if paper_mode:
        if cand.ticker in _paper_positions:
            return False, "paper_dup_position"
        _paper_record_entry(cand, packet, decision, direction, ask_c_i, series, edge_pp)
        _paper_positions.add(cand.ticker)
        return True, f"paper_matcher {direction} edge={edge_pp:.1f}pp ask={ask_c_i}c"
    try:
        pos = _rt.positions.get(cand.ticker) if hasattr(_rt, "positions") else None
        if pos and float(pos.get("cost", 0)) > 0:
            return False, f"already_held_cost_${float(pos.get('cost', 0)):.2f}"
    except Exception:
        pass
    # (5) Position cap per (station, series_prefix, direction). 2026-06-05 (audit): the
    # count + cap check + reservation are ATOMIC under _pending_buys_lock so the 3 eval
    # threads can't race two same-(station,dir) brackets past the cap (TOCTOU). The
    # reservation is released in the execution finally / on the gate-6,7 rejects below.
    # 2026-06-03 (Chris): HIGH BUY_NO may hold a 2nd same-station bracket (sigma-play,
    # cap_tiers.py); HIGH+NO scoped, PUSH_MAX_TICKERS_PER_STATION_NO reverted to 1 on 6/5.
    # 2026-06-05 (Chris): ONE bracket per station-day for HIGH, across BOTH directions,
    # committing the MAX-EDGE bracket (PUSH_ONE_BRACKET_PER_STATION_HIGH). Backtest:
    # cuts worst-5% station-day drawdown ~3x + lifts Sharpe vs stacking correlated legs.
    # The sibling scan is read-only on the WS cache → done BEFORE the lock so the lock
    # hold (count+reserve) stays brief. Self+sibling edges both go through
    # _bracket_edge_pp (apples-to-apples; independent of the decision's own edge).
    _one_per_stn = (bool(getattr(_cfg, "PUSH_ONE_BRACKET_PER_STATION_HIGH", False))
                    and cand.series_prefix == "KXHIGH")
    _self_edge = _msib_edge = None
    _msib_tk = None
    if _one_per_stn:
        _mu_c = packet.get("mu_chosen")
        _sig_c = packet.get("sigma_chosen")
        _self_edge = _bracket_edge_pp(cand.floor, cand.cap,
                                      packet.get("yes_bid_c"), packet.get("yes_ask_c"),
                                      _mu_c, _sig_c)
        _msib_edge, _msib_tk = _max_sibling_edge_pp(cand, _mu_c, _sig_c)
    _cap_key = (cand.station, cand.series_prefix, direction)
    with _pending_buys_lock:
        # (5-day) Per-day HIGH exposure cap (2026-06-16, faithful-replay finding).
        # Days with 4+ qualifying HIGH brackets are correlated-forecast-miss days that
        # LOSE (settled-fill replay: 2-3 fills/day +$56, 4+ fills/day -$9.28; capping
        # both-halves-positive). The bot's real unit of risk is the day's FORECAST, not
        # the bracket — when the airmass call is wrong it's wrong across many stations at
        # once. This caps total HIGH fills/day across stations (complements the per-STATION
        # one-bracket cap, which 6/11's 10-different-station blowup slipped past). Counts
        # filled-today + all pending HIGH (pending is inherently today/in-window). 0=off.
        _day_cap = int(getattr(_cfg, "PUSH_MAX_HIGH_FILLS_PER_DAY", 0))
        if _day_cap > 0 and cand.series_prefix == "KXHIGH":
            _pend_high = sum(v for k, v in _pending_buys.items()
                             if isinstance(k, tuple) and len(k) >= 2 and k[1] == "KXHIGH")
            _day_n = _count_high_fills_today(cand.climate_day) + _pend_high
            if _day_n >= _day_cap:
                return False, f"high_day_cap {cand.climate_day}: {_day_n}>={_day_cap}"
        if _one_per_stn:
            # cap = 1 bracket/station-day across BOTH directions (filled + resting + pending)
            _stn_n = (_count_existing_slots(cand, "BUY_NO")
                      + _count_existing_slots(cand, "BUY_YES")
                      + _pending_buys.get((cand.station, cand.series_prefix, "BUY_NO"), 0)
                      + _pending_buys.get((cand.station, cand.series_prefix, "BUY_YES"), 0))
            if _stn_n >= 1:
                return False, f"one_bracket_per_station {cand.station}/{cand.series_prefix}: {_stn_n}>=1"
            # don't commit a lesser bracket while a higher-edge sibling is quoted
            _tol = float(getattr(_cfg, "PUSH_ONE_BRACKET_EDGE_TOL_PP", 0.25))
            if (_self_edge is not None and _msib_edge is not None
                    and _msib_edge > _self_edge + _tol):
                return False, (f"not_best_bracket {cand.station}: sibling {_msib_tk} "
                               f"edge {_msib_edge:.1f}pp > self {_self_edge:.1f}pp")
            _pending_buys[_cap_key] = _pending_buys.get(_cap_key, 0) + 1
        else:
            cap_per_dir = int(getattr(_cfg, "PUSH_MAX_TICKERS_PER_STATION_SIDE_DIRECTION", 1))
            if direction == "BUY_NO" and cand.series_prefix == "KXHIGH":
                cap_per_dir = int(getattr(_cfg, "PUSH_MAX_TICKERS_PER_STATION_NO", cap_per_dir))
            n_existing = _count_existing_slots(cand, direction) + _pending_buys.get(_cap_key, 0)
            if n_existing >= cap_per_dir:
                return False, (f"position_cap {direction}@{cand.station}/{cand.series_prefix}: "
                               f"{n_existing}>={cap_per_dir}")
            _pending_buys[_cap_key] = _pending_buys.get(_cap_key, 0) + 1
    # (6) Cash check
    try:
        import kalshi_client as _kc
        balance = _kc.get_balance_cached()
        min_buy = float(getattr(_cfg, "MIN_BUY_USD", 1.0))
        if balance is not None and balance < min_buy:
            _release_pending(_cap_key)
            return False, f"low_cash_${balance:.2f}<${min_buy:.2f}"
    except Exception:
        pass
    # (7) Correlation cap (mirror LLM-path)
    side_label = "HIGH" if cand.series_prefix == "KXHIGH" else "LOW"
    cap_key = (cand.station, side_label, cand.climate_day)
    try:
        cap = _cfg.GUARDRAILS.get(f"max_buys_per_station_side_{side_label.lower()}",
                                  _cfg.GUARDRAILS.get("max_buys_per_station_side", 999))
        cycle_buys = getattr(_rt, "cycle_buys_by_station_side", {}).get(cap_key, 0)
        if cycle_buys >= cap:
            _release_pending(_cap_key)
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
        # 2026-05-27 SIZING FIX: execute_buy sizes target_cost = side_cap($15) x
        # size_factor, which DISCARDED the worker's per-station cap + up-tilt +
        # fat-edge de-size (all of which live in decision["size_usd"]/qty). Result:
        # every HIGH bet executed at ~$15 (e.g. KLAS B90.5 5/26: decided $1.40 de-sized,
        # executed $15.33). Pass the intended de-sized size so execute_buy honors it
        # (capped by the $15 backstop). Push-path only; LLM/other paths unaffected.
        packet["push_target_usd"] = decision.get("size_usd")
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
        # 2026-05-24: LOW posting probe. Post a maker limit at MID and rest it
        # (async-adopted by low_post_probe.sweep) instead of crossing the spread.
        # 2026-06-03 (Chris): HIGH maker-first too (PUSH_HIGH_POST_AT_MID) -- the
        # backtest says crossing the HIGH spread + taker fee costs ~2.8c/ct (+28% of
        # the edge); posting at mid + the taker-fallback recovers it. Same engine, same
        # double-buy-safe cross. Flag-gated per series; default = HIGH still takes.
        _post_at_mid = ((cand.series_prefix == "KXLOW"
                         and getattr(_cfg, "PUSH_LOW_POST_AT_MID", False))
                        or (cand.series_prefix == "KXHIGH"
                            and getattr(_cfg, "PUSH_HIGH_POST_AT_MID", False)))
        if _post_at_mid:
            import low_post_probe
            if low_post_probe.has_resting(cand.ticker) or cand.ticker in _rt.positions:
                return False, "post_already_active"
            return low_post_probe.place(_rt, cand, packet, entry_dec,
                                        short_dir.lower(), decision)
        _pjb.execute_buy(_rt, cand, packet, entry_dec)
        return True, (f"executed {direction} edge={edge*100:.1f}pp ask={ask_c_i}c "
                      f"win={win_dbg}")
    except Exception as e:
        log.exception("auto-execute crashed for %s: %s", cand.ticker, e)
        return False, f"exception: {e}"
    finally:
        # release the gate-5 reservation on EVERY execution-path exit (place /
        # execute_buy / post_already_active / exception) — the slot is now held by the
        # recorded position instead. (gate-5 reject + gate-6,7 rejects release inline.)
        _release_pending(_cap_key)


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
    # 2026-06-04 (Chris): touch depth at decision time -> future fill/liquidity
    # filter design (the thin-HIGH-book problem we can't currently backtest).
    yes_bid_size, no_bid_size = kalshi_ws.get_touch_sizes(cand.ticker)

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
    # 2026-05-24: wethr_cache_service writes the hourly curve as "hourly_history",
    # not "hourly_obs_today" (a dead key no producer fills). Without it the matcher
    # runs only on the 60-min temp_history and sits on its >=12-bin trajectory gate,
    # so sparser-feed stations chronically/intermittently no-fire (KNYC always;
    # KBOS/KDCA/KSEA intermittent). Map hourly_history -> the shape nn_shadow.py
    # expects: {hour_utc_iso, temp_f, dewpt_f}.
    if not hourly_obs_today:
        _hh = wethr.get("hourly_history") or []
        hourly_obs_today = [
            {"hour_utc_iso": _h.get("hour_iso"), "temp_f": _h.get("temp_f"),
             "dewpt_f": _h.get("dew_point_f")}
            for _h in _hh
            if _h.get("temp_f") is not None and _h.get("hour_iso")
        ]
    # 2026-05-21 bugfix: get_rm_age_sec expects kind in {"max","min"}, not
    # "high"/"low" — it had silently returned None (rm_age_sec logged as None,
    # and the regime tspeak proxy never fired). Now passes the correct kind.
    rm_age_max = shared_cache_reader.get_rm_age_sec(cand.station, "max")
    rm_age_min = shared_cache_reader.get_rm_age_sec(cand.station, "min")
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

    # 2026-06-03 (Chris): SINGLE min/peak-hour source, in LST (see _apply_lst_clock).
    ctx = _apply_lst_clock(ctx, cand.station, cand.climate_day)

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
        "yes_bid_size": yes_bid_size,
        "no_bid_size": no_bid_size,
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
def _check_adverse_drift_exit(cand, pkt) -> bool:
    """Adverse-drift stop-loss (the ONLY sell path). If we hold a paper-judge
    position on this ticker whose held-side BID has drifted >= ADVERSE_DRIFT_EXIT_PP
    cents below its entry baseline AND stayed there >= ADVERSE_DRIFT_SUSTAIN_MIN
    minutes (within ADVERSE_DRIFT_WINDOW_MIN of entry), sell at the current bid.
    Returns True if a sell was placed (caller should stop evaluating this ticker).

    Mechanism: the market corrects against a losing position within ~30-60 min
    (informed order flow). The sustain window filters momentary dip-and-recover
    whipsaws. Sells at the bid (crosses the spread). Only acts on positions with
    a recorded entry_bid_c baseline (entered after 2026-05-26)."""
    if not getattr(_cfg, "ENABLE_ADVERSE_DRIFT_EXIT", False):
        return False
    if _rt is None or not hasattr(_rt, "positions"):
        return False
    pos = (_rt.positions or {}).get(cand.ticker)
    if not pos or pos.get("opened_by") != "paper-judge":
        return False
    base = pos.get("entry_bid_c"); ets = pos.get("entry_ts_epoch")
    if base is None or ets is None:
        return False  # pre-baseline position → hold to settlement
    cnt = pos.get("count") or 0
    if cnt <= 0:
        return False
    now = time.time()
    win_min = float(getattr(_cfg, "ADVERSE_DRIFT_WINDOW_MIN", 60))
    if (now - float(ets)) > win_min * 60:
        _drift_breach.pop(cand.ticker, None)
        return False  # past the watch window → hold
    action = pos.get("action")
    held_bid = pkt.get("no_bid_c") if action == "BUY_NO" else pkt.get("yes_bid_c")
    if held_bid is None or held_bid < 1 or held_bid > 99:
        return False  # no sane bid to sell into
    X = float(getattr(_cfg, "ADVERSE_DRIFT_EXIT_PP", 10))
    sustain_min = float(getattr(_cfg, "ADVERSE_DRIFT_SUSTAIN_MIN", 15))
    if held_bid <= float(base) - X:
        bs = _drift_breach.get(cand.ticker)
        if bs is None:
            _drift_breach[cand.ticker] = now
            return False  # first breach — start the sustain clock
        if (now - bs) < sustain_min * 60:
            return False  # breached but not yet sustained
        # Sustained adverse drift → SELL at the current bid.
        import paper_judge_bot as _pjb
        import judgment as _jd
        dec = _jd.ExitDecision(
            decision="SELL_ALL", sell_count=int(cnt),
            limit_price_cents=int(held_bid), conviction=0.90,
            read=(f"adverse-drift exit: {action} held_bid {held_bid}c <= "
                  f"entry_bid {base}c - {X:.0f}c, sustained {sustain_min:.0f}m"),
            regret_check=("market drifted against position post-entry "
                          "(informed-flow signal); cutting per validated stop"))
        try:
            _pjb.execute_sell(_rt, cand.ticker, pos, pkt, dec)
        except Exception:
            log.exception("adverse-drift exit sell failed for %s", cand.ticker)
            return False
        _drift_breach.pop(cand.ticker, None)
        _bump("adverse_drift_exits")
        return True
    else:
        _drift_breach.pop(cand.ticker, None)  # recovered → reset the clock
        return False


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

        # 2026-05-26: adverse-drift exit runs FIRST. If we hold a position on
        # this ticker and it sold, stop here (do not fall through to entry —
        # avoids a same-event re-buy of what we just sold).
        if _check_adverse_drift_exit(cand, pkt):
            return

        # Tag the packet with its matched push override (window + bias + mae).
        # Read-only — logged per decision for analysis; bias/mae not yet applied
        # to the decision (Phase 2). mae = cell's expected pre-peak accuracy (°F).
        _ov_series = "HIGH" if cand.series_prefix == "KXHIGH" else "LOW"
        pkt["push_override"] = _lookup_push_override(
            cand.station, _ov_series, cand.climate_day)

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
                           "spread_c": pkt.get("spread_c"),
                           "yes_bid_c": pkt.get("yes_bid_c"), "no_bid_c": pkt.get("no_bid_c"),
                           "yes_bid_size": pkt.get("yes_bid_size"), "no_bid_size": pkt.get("no_bid_size")},
                "nn_fired": False,
                "rm": pkt.get("running_min_or_max"),
                **_wethr_obs_extra(cand.station),
                "signals": _signals_block(pkt),
                "push_override": pkt.get("push_override"),
                "decision": "SKIP",
                "reason": "nn_match did not fire (no projection)",
            })
            return

        _bump("evals_nn_fired")
        pkt["mu_method"] = (f"nn_match_{nn_res.get('side', '')}_n{nn_res.get('n_neighbors')}"
                            + ("_locked" if nn_res.get("extreme_locked") else ""))
        pkt["mu_chosen"] = nn_res["mu"]
        pkt["sigma_chosen"] = nn_res["sigma"]
        pkt["cur_tmpf"] = nn_res.get("cur_tmpf")     # latest obs temp (matcher obs_trajectory[-1][1])
        pkt["traj_max"] = nn_res.get("traj_max")     # running intraday max (for (2h) off-peak entry veto)

        # 2026-05-21: per-cell MEDIAN bias correction, HIGH only. Out-of-sample
        # validation: median-bias cut HIGH holdout MAE −2.1% (159/235 cells),
        # LOW neutral (−0.1%) → LOW excluded. The MEAN bias was −8.6% WORSE
        # (skewed errors) and is NOT used (the override file ships median). The
        # residual is additive on top of the matcher's internal bias-corr, so
        # subtract it so edge/p_yes downstream reflect the calibrated μ.
        # Gated by USE_PUSH_BIAS_CORRECTION; raw μ kept in mu_pre_bias.
        if (getattr(_cfg, "USE_PUSH_BIAS_CORRECTION", False)
                and cand.series_prefix == "KXHIGH"):
            _po = pkt.get("push_override")
            if _po and _po.get("bias") is not None:
                _raw = pkt["mu_chosen"]
                pkt["mu_chosen"] = round(_raw - float(_po["bias"]), 3)
                pkt["mu_pre_bias"] = round(_raw, 3)
                pkt["bias_applied"] = round(float(_po["bias"]), 3)

        # 2026-05-25 (Chris): NWP-agreement signal. Independent NBM/HRRR/ECMWF
        # daily-high (forecast_delta) as a cross-check on the k-NN mu; large
        # |mu_knn - mu_nwp| flags an unreliable mu (the 5-6F blow-up days).
        if cand.series_prefix == "KXHIGH":
            _mn = _compute_mu_nwp(cand.station, cand.climate_day)
            pkt["mu_nwp"] = _mn
            pkt["nbm_high"] = _compute_nbm_high(cand.station, cand.climate_day)
            pkt["nwp_disagree"] = round(abs(pkt["mu_chosen"] - _mn), 2) if _mn is not None else None

        # 2026-06-02: blend-forecast mu override (fail-safe -> keeps matcher mu).
        _bov = _compute_blend_override(cand, pkt, nn_res)
        if _bov is not None:
            pkt["mu_pre_blend"] = pkt.get("mu_chosen")
            pkt["mu_chosen"], pkt["sigma_chosen"] = _bov
            pkt["mu_method"] = "blend_" + str(cand.series_prefix or "")

        # Per-series bet cap — single source of truth is the GUARDRAILS dict
        # (guardrails.check_buy enforces the same numbers downstream; sizing
        # here must match or guardrails would REJECT an over-cap bet outright).
        # 2026-05-20: HIGH raised to $15 (max_bet_high_series_usd).
        # 2026-05-21: LOW cut to $1 (max_bet_low_series_usd) — losing book.
        _gr = getattr(_cfg, "GUARDRAILS", {}) or {}
        _is_high_sizing = (cand.series_prefix == "KXHIGH")
        # 2026-05-22: per-station HIGH cap -- NYC/MIA (edge cells) $5, all others $3.
        _high_cap = float(getattr(_cfg, "PUSH_HIGH_MAX_BET_BY_STATION", {}).get(
            cand.station, getattr(_cfg, "PUSH_HIGH_MAX_BET_DEFAULT", 3.0)))
        _series_cap_usd = _high_cap if _is_high_sizing \
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

        # 2026-05-21: MAE-based confidence sizing. A cell's historical pre-peak
        # MAE predicts its out-of-sample accuracy (corr 0.62, monotonic), so
        # scale the bet DOWN where the matcher is less reliable. Only ever
        # reduces size (never increases) → risk-reducing. Gated by
        # USE_PUSH_MAE_SIZING; conf_mult logged. Note: for LOW (small cap) a low
        # multiplier can drop the bet below min_buy → that cell simply skips,
        # which is the intended "don't trade where unreliable" behavior.
        _conf_mult = 1.0
        if getattr(_cfg, "USE_PUSH_MAE_SIZING", False):
            _po = pkt.get("push_override")
            _size_mae = _po.get("mae") if _po else None
            # 2026-05-21: optionally adjust the cell MAE by the day's regime
            # (sigma/anomaly/sky/wind global deltas) before tiering. Better
            # per-decision accuracy estimate -> better-calibrated sizing.
            if getattr(_cfg, "USE_PUSH_REGIME_MAE_ADJ", False) and _size_mae is not None:
                _size_mae, _adj_dbg = _regime_adjusted_mae(_size_mae, cand, pkt, nn_res)
                pkt["regime_mae_adj"] = _adj_dbg
                pkt["mae_adjusted"] = _size_mae
            _conf_mult = _mae_conf_mult(_size_mae)
            ticker_remaining *= _conf_mult
        pkt["mae_conf_mult"] = round(_conf_mult, 3)

        # 2026-05-18 (Chris): shadow logs EVERY positive-edge candidate, no
        # 25pp ceiling. Whole point of the shadow is to figure out which
        # filters add value post-hoc. We still record rm_locked status so
        # the analysis can replay any ceiling rule. edge_max=1.0 disables
        # the in-function ceiling without changing pure_nn_decide.
        decision = nn_shadow_strategy.pure_nn_decide(
            pkt, ticker_remaining_usd=ticker_remaining, edge_max=1.0,
            min_buy_usd=_min_buy_usd,
            series_cap_high_usd=_high_cap,
            series_cap_low_usd=float(_gr.get("max_bet_low_series_usd", 5.0)),
            use_tail_empirical=getattr(_cfg, "USE_TAIL_EMPIRICAL_PYES", False),
            edge_tier=(tuple(getattr(_cfg, "PUSH_EDGE_TIER_SIZING"))
                       if getattr(_cfg, "PUSH_EDGE_TIER_SIZING_ENABLED", False)
                       else None),
        )

        # 2026-05-22 (Chris): scale the proven NYC/MIA BUY_NO edge up to its
        # per-station cap (PUSH_HIGH_NO_BET_BY_STATION) — the only OOS-robust HIGH
        # edge. Only ever INCREASES a BUY_NO on a listed station; the YES side and
        # every other cell keep their PUSH_HIGH_MAX_BET_BY_STATION cap. Mirrors
        # _compute_size (qty = budget // price) and reuses the existing-cost +
        # MAE-conf-mult logic from the base sizing above.
        if _is_high_sizing and decision.get("decision") == "BUY_NO":
            _no_cap = float(getattr(_cfg, "PUSH_HIGH_NO_BET_BY_STATION", {}).get(cand.station, 0.0))
            _price_c = decision.get("price_c")
            if _no_cap > _high_cap and _price_c:
                _price_usd = float(_price_c) / 100.0
                _rem_big = _no_cap
                if _rt is not None:
                    try:
                        _pos = _rt.positions.get(ticker) if hasattr(_rt, "positions") else None
                        if _pos:
                            _rem_big = max(0.0, _no_cap - float(_pos.get("cost", 0)))
                    except Exception:
                        pass
                _rem_big *= _conf_mult
                _new_qty = int(min(_no_cap, _rem_big) // _price_usd) if _price_usd > 0 else 0
                if _new_qty > (decision.get("qty") or 0):
                    decision["qty"] = _new_qty
                    decision["size_usd"] = round(_new_qty * _price_usd, 2)
                    pkt["no_bet_scaled_usd"] = _no_cap

        # 2026-05-26 (Chris): EDGE-BAND sizing tilt (REVERSES the 5/25 fat-edge tilt).
        # Size up a HIGH BUY_NO only when its edge is in the RELIABLE band [LO, HI) pp.
        # The deep dive found edge and win-rate INVERSELY related (18-26pp = 60% WR;
        # >=35pp = 41% WR with the late half negative -- high edge is sigma-overconfidence),
        # so we lean into the moderate band, not the fat tail. effective cap =
        # min(guardrail, base x MULT) so $3 stations -> $6 and BOS/SEA stay $15. Only ever
        # INCREASES. Mirrors the NO-resize qty math above + the existing-cost / MAE-conf-mult
        # logic. Gated by PUSH_HIGH_EDGE_TILT_ENABLED.
        if (_is_high_sizing and decision.get("decision") == "BUY_NO"
                and getattr(_cfg, "PUSH_HIGH_EDGE_TILT_ENABLED", False)):
            _tilt_mult = float(getattr(_cfg, "PUSH_HIGH_EDGE_TILT_MULT", 2.0))
            _band_lo = float(getattr(_cfg, "PUSH_HIGH_EDGE_TILT_BAND_LO_PP", 18.0))
            _band_hi = float(getattr(_cfg, "PUSH_HIGH_EDGE_TILT_BAND_HI_PP", 26.0))
            _edge = decision.get("edge")
            _price_c = decision.get("price_c")
            if (_edge is not None and _price_c and _tilt_mult > 1.0
                    and _band_lo <= _edge * 100.0 < _band_hi):
                _guard = float(_gr.get("max_bet_high_series_usd", _high_cap))
                _tilt_cap = min(_guard, _high_cap * _tilt_mult)
                if _tilt_cap > _high_cap:
                    _price_usd = float(_price_c) / 100.0
                    _rem_tilt = _tilt_cap
                    if _rt is not None:
                        try:
                            _pos = _rt.positions.get(ticker) if hasattr(_rt, "positions") else None
                            if _pos:
                                _rem_tilt = max(0.0, _tilt_cap - float(_pos.get("cost", 0)))
                        except Exception:
                            pass
                    _rem_tilt *= _conf_mult
                    _new_qty = int(min(_tilt_cap, _rem_tilt) // _price_usd) if _price_usd > 0 else 0
                    if _new_qty > (decision.get("qty") or 0):
                        decision["qty"] = _new_qty
                        decision["size_usd"] = round(_new_qty * _price_usd, 2)
                        pkt["edge_tilt_scaled_usd"] = _tilt_cap
                        pkt["edge_tilt_pp"] = round(_edge * 100.0, 1)

        # 2026-05-26 (Chris) S3: EDGE-BAND DE-SIZE — the down-side complement of the
        # tilt above. A FAT edge (>= DESIZE_PP) means the model WILDLY disagrees with the
        # market, which the deep dive showed is usually our own sigma-overconfidence
        # (>=35pp edges win ~41% vs ~60% in the 18-26pp band, late half negative). HALVE
        # those bets -> same expected PnL with ~23% less capital at risk. Skill-sized
        # stations (base cap > the $3 default, i.e. BOS/SEA) are EXEMPT. Only ever
        # DECREASES (floored at min_buy so guardrails don't reject). Mutually exclusive
        # with the up-tilt band. Gated by PUSH_HIGH_EDGE_TILT_ENABLED.
        if (_is_high_sizing and decision.get("decision") == "BUY_NO"
                and getattr(_cfg, "PUSH_HIGH_EDGE_TILT_ENABLED", False)):
            _desize_pp = float(getattr(_cfg, "PUSH_HIGH_EDGE_TILT_DESIZE_PP", 26.0))
            _desize_mult = float(getattr(_cfg, "PUSH_HIGH_EDGE_TILT_DESIZE_MULT", 0.5))
            _default_cap = float(getattr(_cfg, "PUSH_HIGH_MAX_BET_DEFAULT", 3.0))
            _edge = decision.get("edge")
            _price_c = decision.get("price_c")
            _is_skill_cap = _high_cap > _default_cap   # BOS/SEA etc. -> exempt
            if (_edge is not None and _price_c and 0.0 < _desize_mult < 1.0
                    and not _is_skill_cap and _edge * 100.0 >= _desize_pp):
                _price_usd = float(_price_c) / 100.0
                _desize_cap = _high_cap * _desize_mult
                _cur_qty = decision.get("qty") or 0
                _new_qty = int(_desize_cap // _price_usd) if _price_usd > 0 else 0
                # never drop below the min-buy floor (guardrails would reject the bet)
                if _price_usd > 0 and _new_qty * _price_usd < _min_buy_usd:
                    _new_qty = int(-(-_min_buy_usd // _price_usd))  # ceil(min_buy/price)
                if 0 < _new_qty < _cur_qty:
                    decision["qty"] = _new_qty
                    decision["size_usd"] = round(_new_qty * _price_usd, 2)
                    pkt["edge_desize_usd"] = round(_desize_cap, 2)
                    pkt["edge_desize_pp"] = round(_edge * 100.0, 1)

        # 2026-05-25 (Chris): HIGH BUY_YES re-enabled at a reduced $3 cap (weaker
        # side -- backtest 36% win / -20% ROI -- run small). pure_nn_decide sized to
        # _high_cap ($5); cap the qty DOWN so cost <= PUSH_HIGH_YES_MAX_BET_USD.
        if _is_high_sizing and decision.get("decision") == "BUY_YES":
            _yes_cap = float(getattr(_cfg, "PUSH_HIGH_YES_MAX_BET_USD", _high_cap))
            _price_c = decision.get("price_c")
            if _yes_cap < _high_cap and _price_c:
                _price_usd = float(_price_c) / 100.0
                _max_qty = int(_yes_cap // _price_usd) if _price_usd > 0 else 0
                if _max_qty >= 1 and (decision.get("qty") or 0) > _max_qty:
                    decision["qty"] = _max_qty
                    decision["size_usd"] = round(_max_qty * _price_usd, 2)
                    pkt["yes_bet_capped_usd"] = _yes_cap

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
                       "spread_c": pkt.get("spread_c"),
                       "yes_bid_c": pkt.get("yes_bid_c"), "no_bid_c": pkt.get("no_bid_c"),
                       "yes_bid_size": pkt.get("yes_bid_size"), "no_bid_size": pkt.get("no_bid_size")},
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
                "match_dist_mean": nn_res.get("match_dist_mean"),
                "match_dist_min": nn_res.get("match_dist_min"),
            },
            "rm": pkt.get("running_min_or_max"),
            **_wethr_obs_extra(cand.station),
            "rm_age_sec": (pkt.get("rm_age_max_sec") if cand.series_prefix == "KXHIGH"
                           else pkt.get("rm_age_min_sec")),
            "wethr_age_sec": _wethr_age_sec(pkt),
            # 2026-05-19: obs/wethr signals for post-hoc BUY_YES filter discovery.
            # Pure additive — pure_nn_decide does NOT use these yet.
            "signals": _signals_block(pkt),
            # 2026-05-21: matched push override {before,after,bias,mae,src} +
            # how it was APPLIED. mu_pre_bias = raw matcher μ before HIGH-only
            # median-bias; mae_conf_mult = MAE-based bet-size multiplier.
            "push_override": pkt.get("push_override"),
            "mu_pre_bias": pkt.get("mu_pre_bias"),
            "mu_chosen": pkt.get("mu_chosen"),   # 2026-05-23: raw matcher μ — recorded so the daily window replay can apply the (2d) thin-margin gate (needs μ).
            "bias_applied": pkt.get("bias_applied"),
            "mae_conf_mult": pkt.get("mae_conf_mult"),
            "mae_adjusted": pkt.get("mae_adjusted"),
            "regime_mae_adj": pkt.get("regime_mae_adj"),
            "decision": decision["decision"],
            "side": decision["side"],
            "edge_pp": round(decision["edge"] * 100, 2) if decision.get("edge") is not None else None,
            "p_yes": round(decision["p_yes"], 4) if decision.get("p_yes") is not None else None,
            "qty": decision.get("qty"),
            "price_c": decision.get("price_c"),
            "size_usd": decision.get("size_usd"),
            "rm_locked": decision.get("rm_locked"),
            "reason": decision.get("reason"),
            "mu_nwp": pkt.get("mu_nwp"),
            "nwp_disagree": pkt.get("nwp_disagree"),
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
