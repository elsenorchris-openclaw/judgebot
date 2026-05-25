"""kalshi_ws.py — WebSocket client for live BBO updates from Kalshi.

Connects to wss://api.elections.kalshi.com/trade-api/ws/v2 and maintains an
in-memory orderbook cache for subscribed tickers. The bot reads BBO from
this cache instead of waiting for the next /markets REST refresh, dropping
stale-quote latency from up to 30 seconds (cache TTL) to ~50 milliseconds.

Architecture
------------
  - One asyncio event loop runs in a dedicated daemon thread.
    (websockets is asyncio-only; rest of the bot is sync/threaded.)
  - Thread-safe public functions: start(), subscribe(), get_bbo(), get_stats().
  - Subscribes to channel 'orderbook_delta', which delivers an initial
    orderbook_snapshot then orderbook_delta messages for each subscribed ticker.
  - Computes BBO (yes_bid, yes_ask) from the L2 books on every update.
  - Reconnects with exponential backoff. On reconnect, re-subscribes to all
    currently tracked tickers (server delivers fresh snapshots).

Safety
------
  - For QUOTING ONLY. Order placement and fill confirmation continue to use
    REST as the canonical path.
  - get_bbo() returns None if the cached BBO is older than WS_BBO_FRESH_SEC
    or if the ticker isn't in the cache. Caller falls back to REST silently.
  - This module never places orders, never modifies positions, and only
    requires read-side WS auth (the same RSA-PSS signature used for REST GETs).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Callable, Optional

# Kalshi WS endpoint and signing path (must match exactly for the signature
# to verify on the server side).
KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_WS_PATH = "/trade-api/ws/v2"

# Cache freshness threshold in seconds. Older than this → fall back to REST.
# 2026-05-15 (judge-bot fork): raised from 10s. Module auto-resyncs idle
# tickers on a 300s cycle (see _RESYNC_AGE_SEC below); a 10s threshold made
# get_bbo() return None for any quiet ticker — most long-tail weather brackets
# go many seconds without an L2 delta. 60s keeps us well within the
# auto-resync window so a "fresh" cache entry is always backed by a snapshot
# < ~5 minutes old.
WS_BBO_FRESH_SEC = 60.0

# Reconnect backoff
_RECONNECT_INITIAL_S = 1.0
_RECONNECT_MAX_S = 30.0

# 2026-04-25: L2 book resync — fix for stale-state inversions.
# Per-ticker WS L2 books drift from reality when deltas are dropped. Symptom:
# yes_bid_c > yes_ask_c (impossible in a healthy book). Today V2 rejected 1789
# brackets on inverted quotes that REST showed normal — bot's local cache was
# wrong, not the market. Fix: trigger fresh orderbook_snapshot via re-subscribe
# (a) on inversion detection, (b) periodically per-ticker.
_RESYNC_AGE_SEC = 300.0           # periodic: resync any ticker whose snapshot is older than this
_RESYNC_PER_TICKER_RATE_SEC = 5.0  # 2026-05-20: tightened 30->5s for faster drift recovery; max ~46 resyncs/sec at 230 tickers, well under Kalshi limits
_RESYNC_BATCH_MAX = 20            # per worker tick, cap throughput
_RESYNC_INTERVAL_SEC = 10.0       # worker tick cadence

# 2026-05-20: write+read guards against transient inversion races (yes_bid + no_bid > 100).
# When Kalshi matches two crossing limit orders, deltas arrive sequentially; between them
# our cache momentarily shows the cross. With the guard on, _recompute_bbo refuses to
# overwrite the cache with inverted state (keeps last good BBO visible) and get_bbo refuses
# to return inverted state from any source. Set False to revert to pre-2026-05-20 behavior.
_INVERSION_GUARD_ENABLED = True

# ─────────────────────────────────────────────────────────────────────────────
# Module state (single connection per process)
# ─────────────────────────────────────────────────────────────────────────────

_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None
_ws = None  # current websockets connection (or None when reconnecting)
_started = False

_subscribed_tickers: set[str] = set()  # set of tickers we've requested
_yes_bids: dict[str, dict[int, int]] = {}  # ticker -> {price_cents: total_size}
_no_bids: dict[str, dict[int, int]] = {}
_bbo_cache: dict[str, dict[str, Any]] = {}  # ticker -> {yes_bid, yes_ask, ts}
_cache_lock = threading.Lock()

# 2026-04-25: L2 book resync state. Tracks snapshot freshness + drift detection.
_last_snapshot_ts: dict[str, float] = {}     # ticker -> ts of last orderbook_snapshot
_pending_resync: set[str] = set()            # tickers awaiting next resync send
_last_resync_ts: dict[str, float] = {}       # ticker -> ts of last resync request
_resync_state_lock = threading.Lock()

# 2026-05-24: a plain re-subscribe is a SILENT NO-OP on Kalshi (returns {"type":"ok"} with
# NO snapshot), so the old resync never repaired drifted/inverted books -> inversions piled up
# unbounded over uptime -> stale BBO -> matcher eval starvation -> bot stops trading. Kalshi
# assigns ONE sid per channel: the first subscribe -> {"type":"subscribed", sid}; bulk/later
# subscribes -> {"type":"ok"} added to that same sid. Capture that one orderbook_delta sid and
# re-snapshot drifted tickers via update_subscription delete_markets+add_markets (which DOES
# resend a snapshot). Reversible: _RESYNC_VIA_UPDATE_SUB=False -> legacy (broken) re-subscribe.
_RESYNC_VIA_UPDATE_SUB = True
_orderbook_sid = None        # the connection's single orderbook_delta subscription id

# 2026-04-11: fill channel (private). Subscribed once at startup, no per-ticker
# subscription needed. Each fill event populates _fills_by_order so the bot's
# check_order_fill() can shortcircuit the REST poll.
_fill_subscribed: bool = False
_fills_by_order: dict[str, dict[str, Any]] = {}
_fills_lock = threading.Lock()
_FILL_CACHE_MAX = 5000  # cap memory; oldest entries pruned in _record_fill

_cmd_id_counter = 0

# Wired by start():
_sign_fn: Optional[Callable[[str, str], dict[str, str]]] = None
_log_fn: Callable[[str], None] = print
# 2026-05-24: WS-health Discord alerting. _alert_fn(msg) injected via start(); the periodic
# stats loop computes per-interval deltas and fires THROTTLED alerts on degradation
# (drift not repairing / feed stalled / reconnect / error spike). Self-contained (no config
# import). Disable with _WS_HEALTH_ALERTS=False.
_alert_fn: Optional[Callable[[str], None]] = None
try:
    import os as _os
    _BOT_LABEL = _os.path.basename(_os.path.dirname(_os.path.abspath(__file__))) or "bot"
except Exception:
    _BOT_LABEL = "bot"
_WS_HEALTH_ALERTS = True
_WS_DRIFT_SKIP_PER_INTERVAL = 1500
_WS_ALERT_THROTTLE_SEC = 1800
_ws_health_prev: dict = {}
_ws_alert_last: dict = {}

# 2026-05-18: BBO change callback registry. Event-driven consumers
# (e.g., nn_shadow_worker) register here to be notified on every BBO
# recompute. Each callback is called as fn(ticker, prev_bbo, new_bbo)
# where prev_bbo / new_bbo are dicts with yes_bid / yes_ask / ts (or
# None for prev_bbo on first compute). Callbacks are wrapped in
# try/except so a failing consumer never crashes the WS loop.
_bbo_callbacks: list[Callable[[str, Optional[dict], dict], None]] = []
_bbo_callbacks_lock = threading.Lock()


def register_bbo_callback(fn: Callable[[str, Optional[dict], dict], None]) -> None:
    """Register a callback fired on every _recompute_bbo. fn signature:
    fn(ticker, prev_bbo, new_bbo). prev_bbo is None on first compute."""
    with _bbo_callbacks_lock:
        if fn not in _bbo_callbacks:
            _bbo_callbacks.append(fn)


def unregister_bbo_callback(fn: Callable) -> None:
    with _bbo_callbacks_lock:
        if fn in _bbo_callbacks:
            _bbo_callbacks.remove(fn)

# Stats counters
_stats = {
    "snapshots": 0,
    "deltas": 0,
    "bbo_updates": 0,
    "reconnects": 0,
    "subscribe_cmds": 0,
    "errors": 0,
    "last_msg_ts": 0.0,
    # NEW 2026-04-11: fill channel telemetry
    "fills_received": 0,
    "fill_cache_hits": 0,
    "fill_cache_misses": 0,
    "fill_subs": 0,
    # NEW 2026-04-25: L2 resync telemetry
    "inversions_detected": 0,   # yes_bid_c > yes_ask_c at compute-time
    "resyncs_scheduled_drift": 0,    # queued by drift detection
    "resyncs_scheduled_age": 0,      # queued by periodic age check
    "resyncs_sent": 0,               # actually sent to Kalshi
    "resyncs_resnapshotted": 0,      # 2026-05-24: tickers re-snapshotted via update_subscription
    "resyncs_rate_limited": 0,       # dropped by per-ticker rate limit
    "cache_writes_skipped_inverted": 0,  # 2026-05-20: write-side drift guard fires
    "bbo_reads_blocked_inverted": 0,     # 2026-05-20: read-side drift guard fires
    # NEW 2026-05-16: full-ladder accessor telemetry (REST round-trip replacement)
    "ob_hits": 0,           # get_orderbook() served from WS cache
    "ob_misses_stale": 0,   # rejected: snapshot older than WS_BBO_FRESH_SEC
    "ob_misses_empty": 0,   # rejected: no ladder for ticker (not subscribed yet)
    "ob_misses_inverted": 0,  # rejected: yes_bid_top > yes_ask_top in cache
}
_stats_lock = threading.Lock()


def _bump(key: str, n: int = 1) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n
        _stats["last_msg_ts"] = time.time()


def _next_cmd_id() -> int:
    global _cmd_id_counter
    _cmd_id_counter += 1
    return _cmd_id_counter


# ─────────────────────────────────────────────────────────────────────────────
# Book maintenance
# ─────────────────────────────────────────────────────────────────────────────

def _recompute_bbo(ticker: str) -> None:
    """Recompute BBO for a ticker from the current L2 books and update cache.

    Kalshi binary-market book convention:
        yes_bid = max price someone is willing to pay for YES   (yes book top)
        yes_ask = 100 - max price someone is willing to pay for NO (no book top)
        Equivalent: yes_ask is the lowest price you'd pay to buy YES,
        which equals 100 cents minus the best NO bid.
    """
    yb = _yes_bids.get(ticker, {})
    nb = _no_bids.get(ticker, {})
    yes_bid_c = max(yb.keys()) if yb else 0
    no_bid_c = max(nb.keys()) if nb else 0
    yes_ask_c = (100 - no_bid_c) if no_bid_c > 0 else 0
    # 2026-04-25: drift detection. yes_bid > yes_ask means the L2 book has
    # gone stale (typically a missed delta) or is mid-match-race. Always log
    # + schedule a fresh snapshot.
    is_inverted = (yes_bid_c > 0 and yes_ask_c > 0 and yes_bid_c > yes_ask_c)
    if is_inverted:
        _bump("inversions_detected")
        _schedule_resync(ticker, source="drift")
    # 2026-05-20: when inverted, refuse to write the bad BBO into the cache.
    # Consumers keep seeing the last known-good BBO from get_bbo until either
    # the next clean delta resolves the cross or the scheduled resync lands.
    # Callbacks also suppressed so event-driven consumers (nn_shadow_worker)
    # do not fire on phantom-edge BBO.
    if is_inverted and _INVERSION_GUARD_ENABLED:
        _bump("cache_writes_skipped_inverted")
        return
    with _cache_lock:
        prev = _bbo_cache.get(ticker)
        new_bbo = {
            "yes_bid": yes_bid_c / 100.0,
            "yes_ask": yes_ask_c / 100.0,
            "ts": time.time(),
        }
        _bbo_cache[ticker] = new_bbo
    _bump("bbo_updates")
    # 2026-05-18: notify event-driven consumers (nn_shadow_worker).
    # Snapshot the callback list to release the lock fast; iterate outside
    # the lock so consumer code (which may take its own locks) can't deadlock us.
    with _bbo_callbacks_lock:
        cbs = list(_bbo_callbacks)
    for fn in cbs:
        try:
            fn(ticker, prev, new_bbo)
        except Exception as e:
            _log_fn(f"kalshi_ws bbo_callback {fn} raised: {e}")


def _dollars_to_cents(s) -> int:
    """Convert Kalshi dollar string ('0.3900') to int cents (39).
    Tolerates ints/floats too. Returns 0 on parse failure."""
    try:
        return int(round(float(s) * 100))
    except (TypeError, ValueError):
        return 0


def _apply_snapshot(msg: dict) -> None:
    """Kalshi snapshot fields:
        market_ticker: str
        yes_dollars_fp: [[price_dollars_str, size_str], ...]   yes-side bids
        no_dollars_fp:  [[price_dollars_str, size_str], ...]   no-side bids
    Older field names (yes/no with cents) are also tolerated as a fallback.
    """
    ticker = msg.get("market_ticker") or msg.get("ticker")
    if not ticker:
        return
    yes = msg.get("yes_dollars_fp") or msg.get("yes") or []
    no = msg.get("no_dollars_fp") or msg.get("no") or []
    yb: dict[int, float] = {}
    for entry in yes:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            p = _dollars_to_cents(entry[0])
            try:
                size = float(entry[1])
            except (TypeError, ValueError):
                continue
            if p > 0 and size > 0:
                yb[p] = size
    nb: dict[int, float] = {}
    for entry in no:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            p = _dollars_to_cents(entry[0])
            try:
                size = float(entry[1])
            except (TypeError, ValueError):
                continue
            if p > 0 and size > 0:
                nb[p] = size
    _yes_bids[ticker] = yb
    _no_bids[ticker] = nb
    # 2026-04-25: track snapshot freshness for periodic resync. Set BEFORE
    # _recompute_bbo so a freshly-snapshotted ticker isn't immediately re-flagged
    # by the age-based check on its first compute.
    _last_snapshot_ts[ticker] = time.time()
    _recompute_bbo(ticker)
    _bump("snapshots")


def _apply_delta(msg: dict) -> None:
    """Kalshi delta fields:
        market_ticker: str
        side: 'yes' | 'no'
        price_dollars: str ('0.3900')
        delta_fp: str (signed size, '3.00' or '-175.00')
    """
    ticker = msg.get("market_ticker") or msg.get("ticker")
    if not ticker:
        return
    side = (msg.get("side") or "").lower()
    price_raw = msg.get("price_dollars") if "price_dollars" in msg else msg.get("price")
    delta_raw = msg.get("delta_fp") if "delta_fp" in msg else msg.get("delta")
    if price_raw is None or delta_raw is None:
        return
    p = _dollars_to_cents(price_raw)
    try:
        d = float(delta_raw)
    except (TypeError, ValueError):
        return
    if p <= 0:
        return
    book = _yes_bids if side == "yes" else _no_bids
    level = book.setdefault(ticker, {})
    new_size = level.get(p, 0.0) + d
    if new_size <= 0:
        level.pop(p, None)
    else:
        level[p] = new_size
    _recompute_bbo(ticker)
    _bump("deltas")


def _schedule_resync(ticker: str, source: str = "drift") -> None:
    """Mark `ticker` for a fresh orderbook_snapshot. Rate-limited per ticker.

    `source`: 'drift' (inversion detected) or 'age' (periodic). Affects only
    telemetry — the rate limit and queue are shared.

    Idempotent: if the ticker is already pending or was resynced within
    `_RESYNC_PER_TICKER_RATE_SEC`, this is a no-op (counts as rate_limited)."""
    if not ticker:
        return
    now = time.time()
    with _resync_state_lock:
        last = _last_resync_ts.get(ticker, 0.0)
        if now - last < _RESYNC_PER_TICKER_RATE_SEC:
            _bump("resyncs_rate_limited")
            return
        _pending_resync.add(ticker)
        # Note: don't set _last_resync_ts here. That happens when the resync
        # is actually sent (in _resync_worker), so a queued-but-not-sent entry
        # doesn't block a fresh attempt if the worker is delayed.
    if source == "drift":
        _bump("resyncs_scheduled_drift")
    else:
        _bump("resyncs_scheduled_age")


def _record_fill(msg: dict) -> None:
    """Handle a fill channel message from Kalshi WS.

    Schema (verified live 2026-04-11 via diagnostic):
        order_id: str (uuid)
        trade_id: str
        market_ticker: str           ← NOT 'ticker'
        side: 'yes' | 'no'
        action: 'buy' | 'sell'
        count_fp: str (e.g. "1.00")  ← NOT 'count', and STRING not int
        yes_price_dollars: str       ← NOT 'yes_price', dollars not cents
        no_price_dollars: str (optional, present on no-side fills)
        is_taker: bool
        fee_cost: str (dollars)
        ts: int (unix seconds)
        post_position_fp: str (position contract count after this fill)
        subaccount: int

    A single order may produce MULTIPLE fill events (partials). We accumulate per
    order_id so the bot can see total filled count + average price.

    Also accepts the old field names (count, yes_price, ticker) defensively
    in case Kalshi changes the schema again — older keys take fallback priority.
    """
    order_id = msg.get("order_id") or msg.get("orderId")
    if not order_id:
        return
    # Tolerate both string-fp and int formats
    raw_count = msg.get("count_fp", msg.get("count", 0))
    try:
        count = float(raw_count)
    except (TypeError, ValueError):
        count = 0.0
    # Prices: prefer dollars-string, fall back to int cents
    yes_px_dollars = msg.get("yes_price_dollars")
    no_px_dollars = msg.get("no_price_dollars")
    try:
        yes_px = float(yes_px_dollars) if yes_px_dollars is not None else (msg.get("yes_price") or 0) / 100.0
    except (TypeError, ValueError):
        yes_px = 0.0
    try:
        no_px = float(no_px_dollars) if no_px_dollars is not None else (msg.get("no_price") or 0) / 100.0
    except (TypeError, ValueError):
        no_px = 0.0
    side = (msg.get("side") or "").lower()
    action = (msg.get("action") or "").lower()
    ticker = msg.get("market_ticker") or msg.get("ticker", "")
    ts = msg.get("ts", "")
    with _fills_lock:
        existing = _fills_by_order.get(order_id)
        if existing is None:
            existing = {
                "order_id": order_id,
                "ticker": ticker,
                "side": side,
                "action": action,
                "fills": [],
                "total_count": 0.0,
                "total_yes_notional_dollars": 0.0,
                "total_no_notional_dollars": 0.0,
                "total_fee_dollars": 0.0,
                "first_ts": ts,
                "last_ts": ts,
            }
            _fills_by_order[order_id] = existing
        try:
            fee = float(msg.get("fee_cost") or 0)
        except (TypeError, ValueError):
            fee = 0.0
        existing["fills"].append({
            "count": count,
            "yes_price_dollars": yes_px,
            "no_price_dollars": no_px,
            "is_taker": bool(msg.get("is_taker")),
            "fee_cost": fee,
            "ts": ts,
            "trade_id": msg.get("trade_id", ""),
        })
        existing["total_count"] += count
        existing["total_yes_notional_dollars"] += count * yes_px
        existing["total_no_notional_dollars"] += count * no_px
        existing["total_fee_dollars"] += fee
        existing["last_ts"] = ts
        # Memory cap — drop oldest by first_ts
        if len(_fills_by_order) > _FILL_CACHE_MAX:
            oldest = sorted(_fills_by_order.items(), key=lambda kv: kv[1].get("first_ts", ""))[:500]
            for k, _ in oldest:
                _fills_by_order.pop(k, None)
    _bump("fills_received")


def get_fill(order_id: str) -> Optional[dict]:
    """Public API. Return the cached fill summary for an order_id, or None if
    no fill events have been received for it. Thread-safe."""
    if not order_id:
        return None
    with _fills_lock:
        rec = _fills_by_order.get(order_id)
        if rec is None:
            _bump("fill_cache_misses")
            return None
        _bump("fill_cache_hits")
        # Return a shallow copy so caller can't mutate the cache
        return dict(rec)


def get_fill_stats() -> dict:
    """For telemetry: how many fills cached, hit/miss ratio, etc."""
    with _fills_lock:
        n_orders = len(_fills_by_order)
    with _stats_lock:
        return {
            "cached_orders": n_orders,
            "fills_received": _stats.get("fills_received", 0),
            "cache_hits": _stats.get("fill_cache_hits", 0),
            "cache_misses": _stats.get("fill_cache_misses", 0),
            "fill_subs": _stats.get("fill_subs", 0),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Async WS loop
# ─────────────────────────────────────────────────────────────────────────────

async def _send_subscribe(tickers: list[str]) -> None:
    global _ws
    if _ws is None or not tickers:
        return
    cmd = {
        "id": _next_cmd_id(),
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta"],
            "market_tickers": tickers,
        },
    }
    try:
        await _ws.send(json.dumps(cmd))
        _bump("subscribe_cmds")
    except Exception as e:
        _log_fn(f"kalshi_ws: subscribe send failed: {e}")


async def _send_update_subscription(sid, tickers: list, action: str) -> None:
    """2026-05-24: add_markets/delete_markets on an existing sid. A delete then add forces a
    fresh orderbook_snapshot (a plain re-subscribe does NOT). Used by the resync worker to
    repair drifted/inverted books."""
    global _ws
    if _ws is None or not tickers or sid is None:
        return
    cmd = {
        "id": _next_cmd_id(),
        "cmd": "update_subscription",
        "params": {"sid": sid, "market_tickers": list(tickers), "action": action},
    }
    try:
        await _ws.send(json.dumps(cmd))
    except Exception as e:
        _log_fn(f"kalshi_ws: update_subscription({action}) send failed: {e}")


async def _send_subscribe_fill() -> None:
    """Subscribe to the private 'fill' channel — pushes our own fills in real-time.
    No per-ticker subscription needed; the channel is account-scoped on the
    authenticated WS connection. Re-issued on every reconnect."""
    global _ws, _fill_subscribed
    if _ws is None:
        return
    cmd = {
        "id": _next_cmd_id(),
        "cmd": "subscribe",
        "params": {"channels": ["fill"]},
    }
    try:
        await _ws.send(json.dumps(cmd))
        _bump("fill_subs")
        _fill_subscribed = True
        _log_fn("kalshi_ws: subscribed to fill channel")
    except Exception as e:
        _log_fn(f"kalshi_ws: fill subscribe failed: {e}")


async def _resync_worker():
    """Background task: re-subscribe stale or drift-detected tickers to force a
    fresh `orderbook_snapshot` from Kalshi. Runs concurrently with the message
    loop in `_ws_main`. Cancelled when the WS connection drops; restarted on
    reconnect.

    Each tick:
      1. Add age-based candidates (snapshot older than _RESYNC_AGE_SEC).
      2. Drain `_pending_resync` (drift-triggered + age-triggered).
      3. Send `subscribe` for up to _RESYNC_BATCH_MAX tickers; Kalshi replies
         with a fresh snapshot which `_apply_snapshot` writes over the stale
         book.
      4. Stamp `_last_resync_ts` to enforce per-ticker rate limit.

    Resyncs do NOT remove the ticker from `_subscribed_tickers` — we keep
    receiving deltas from the existing subscription throughout the resync.
    Once the new snapshot arrives, the book is overwritten atomically (full
    book replacement in `_apply_snapshot`)."""
    while True:
        try:
            await asyncio.sleep(_RESYNC_INTERVAL_SEC)
            if _ws is None:
                continue
            now = time.time()
            batch: list[str] = []
            with _resync_state_lock:
                # Age-based candidates: subscribed tickers with old/missing snapshot
                for tk in list(_subscribed_tickers):
                    age = now - _last_snapshot_ts.get(tk, 0.0)
                    last_resync = _last_resync_ts.get(tk, 0.0)
                    if age > _RESYNC_AGE_SEC and (now - last_resync) >= _RESYNC_PER_TICKER_RATE_SEC:
                        if tk not in _pending_resync:
                            _pending_resync.add(tk)
                            _bump("resyncs_scheduled_age")
                # Drain pending → batch (sorted for determinism in tests)
                for tk in sorted(_pending_resync):
                    if len(batch) >= _RESYNC_BATCH_MAX:
                        break
                    last_resync = _last_resync_ts.get(tk, 0.0)
                    if (now - last_resync) < _RESYNC_PER_TICKER_RATE_SEC:
                        # Got rate-limited between schedule and send; skip this tick
                        continue
                    batch.append(tk)
                # Mark sent + clear queue entries
                for tk in batch:
                    _pending_resync.discard(tk)
                    _last_resync_ts[tk] = now
            if not batch:
                continue
            if _RESYNC_VIA_UPDATE_SUB and _orderbook_sid is not None:
                # 2026-05-24: re-snapshot via update_subscription delete+add on the connection's
                # orderbook sid (a plain re-subscribe is a no-op -> no snapshot -> never repairs).
                await _send_update_subscription(_orderbook_sid, batch, "delete_markets")
                await _send_update_subscription(_orderbook_sid, batch, "add_markets")
                _bump("resyncs_resnapshotted", len(batch))
            else:
                await _send_subscribe(batch)   # legacy fallback (no-op on Kalshi)
            _bump("resyncs_sent", len(batch))
        except asyncio.CancelledError:
            return
        except Exception as e:
            _bump("errors")
            try:
                _log_fn(f"kalshi_ws: resync worker error: {e}")
            except Exception:
                pass


async def _ws_main():
    global _ws, _orderbook_sid
    import websockets

    backoff = _RECONNECT_INITIAL_S
    while True:
        headers: dict[str, str] = {}
        if _sign_fn is not None:
            try:
                headers = _sign_fn("GET", KALSHI_WS_PATH)
            except Exception as e:
                _log_fn(f"kalshi_ws: signing failed: {e}; retry in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_MAX_S)
                continue

        # websockets v12+: additional_headers; older: extra_headers
        try:
            connect_kw = dict(
                additional_headers=list(headers.items()),
                ping_interval=20,
                ping_timeout=10,
                max_size=2 ** 22,
            )
            ws_ctx = websockets.connect(KALSHI_WS_URL, **connect_kw)
        except TypeError:
            ws_ctx = websockets.connect(
                KALSHI_WS_URL,
                extra_headers=list(headers.items()),
                ping_interval=20,
                ping_timeout=10,
                max_size=2 ** 22,
            )

        try:
            async with ws_ctx as ws:
                _ws = ws
                _log_fn("kalshi_ws: connected")
                backoff = _RECONNECT_INITIAL_S
                _orderbook_sid = None   # per-connection sid; recaptured from the subscribed ack
                # Re-subscribe to private fill channel on every (re)connect.
                # Idempotent and cheap. Done first so any pending fills land.
                await _send_subscribe_fill()
                if _subscribed_tickers:
                    # Re-subscribe in chunks of 200 to avoid huge frames
                    pending = list(_subscribed_tickers)
                    for i in range(0, len(pending), 200):
                        await _send_subscribe(pending[i:i + 200])
                # 2026-04-25: spawn L2 resync worker concurrent with message loop.
                # Cancelled below when the connection drops; restarted on reconnect.
                resync_task = asyncio.create_task(_resync_worker())
                try:
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        mtype = msg.get("type", "")
                        body = msg.get("msg") or {}
                        if mtype == "orderbook_snapshot":
                            _apply_snapshot(body)
                        elif mtype == "orderbook_delta":
                            _apply_delta(body)
                        elif mtype == "fill":
                            # 2026-04-11: private fill channel — push fast-path for
                            # bot's check_order_fill(). Cached by order_id.
                            _record_fill(body)
                        elif mtype == "subscribed":
                            # 2026-05-24: capture the connection's single orderbook_delta sid
                            # so the resync worker can re-snapshot via update_subscription.
                            if body.get("channel") == "orderbook_delta" and body.get("sid") is not None:
                                _orderbook_sid = body.get("sid")
                        elif mtype == "error":
                            _log_fn(f"kalshi_ws: server error: {msg}")
                            _bump("errors")
                finally:
                    resync_task.cancel()
                    try:
                        await resync_task
                    except (asyncio.CancelledError, Exception):
                        pass
        except Exception as e:
            _bump("reconnects")
            _log_fn(f"kalshi_ws: connection ended: {e}; reconnect in {backoff:.1f}s")
        finally:
            _ws = None
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _RECONNECT_MAX_S)


def _run_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_ws_main())
    except Exception as e:
        _log_fn(f"kalshi_ws: loop crashed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Public API (called from any thread)
# ─────────────────────────────────────────────────────────────────────────────

def _ws_alert(cond: str, msg: str) -> None:
    """Throttled Discord alert via the injected _alert_fn (per-condition throttle)."""
    if not (_WS_HEALTH_ALERTS and _alert_fn):
        return
    now = time.time()
    if now - _ws_alert_last.get(cond, 0.0) < _WS_ALERT_THROTTLE_SEC:
        return
    _ws_alert_last[cond] = now
    try:
        _alert_fn(msg)
    except Exception as e:
        try:
            _log_fn(f"kalshi_ws: alert send failed: {e}")
        except Exception:
            pass


def _ws_health_check(s: dict) -> None:
    """Compare current stats to the previous interval; fire degradation alerts."""
    if not _WS_HEALTH_ALERTS:
        return
    p = _ws_health_prev
    if p:
        def d(k):
            return s.get(k, 0) - p.get(k, 0)
        d_skip = d("cache_writes_skipped_inverted"); d_snap = d("snapshots")
        d_bbo = d("bbo_updates"); d_delta = d("deltas"); d_recon = d("reconnects")
        d_err = d("errors"); d_rsent = d("resyncs_sent")
        if d_skip > _WS_DRIFT_SKIP_PER_INTERVAL or (d_rsent >= 5 and d_snap == 0 and d_skip > 200):
            _ws_alert("drift", "WARN [%s] WS orderbook drift not clearing: cache_skip_inv +%d/30s, snapshots +%d, resyncs_sent +%d -> resync repair may be failing -> stale BBO starves the matcher." % (_BOT_LABEL, d_skip, d_snap, d_rsent))
        if s.get("connected") and d_delta == 0 and d_bbo == 0:
            _ws_alert("stall", "WARN [%s] WS feed stalled: 0 deltas + 0 bbo_updates in 30s while connected -> matcher cannot fire." % _BOT_LABEL)
        if d_recon > 0:
            _ws_alert("reconnect", "INFO [%s] WS reconnected (+%d)." % (_BOT_LABEL, d_recon))
        if d_err > 5:
            _ws_alert("errors", "WARN [%s] WS error spike: +%d errors in 30s." % (_BOT_LABEL, d_err))
    _ws_health_prev.clear(); _ws_health_prev.update(s)


def start(sign_fn: Callable[[str, str], dict[str, str]],
          log_fn: Optional[Callable[[str], None]] = None,
          alert_fn: Optional[Callable[[str], None]] = None) -> None:
    """Start the WS client thread.

    sign_fn(method, path) -> dict[str, str] of auth headers (same RSA-PSS
    signature scheme used for REST GET requests). Sign with method='GET'
    and path=KALSHI_WS_PATH.
    """
    global _thread, _started, _sign_fn, _log_fn, _alert_fn
    if _started:
        return
    _sign_fn = sign_fn
    if log_fn is not None:
        _log_fn = log_fn
    if alert_fn is not None:
        _alert_fn = alert_fn
        try:
            alert_fn("INFO [%s] kalshi_ws started -- WS-health alerts armed." % _BOT_LABEL)
        except Exception:
            pass
    _thread = threading.Thread(target=_run_loop, name="kalshi_ws", daemon=True)
    _thread.start()
    _started = True
    _log_fn("kalshi_ws: thread started")

    # 2026-04-11 (originally v2-only, now both bots): periodic stats logger
    # so we can verify the WS is healthy. Includes fill-channel telemetry.
    def _periodic_stats():
        import time as _t
        while True:
            _t.sleep(30)
            try:
                s = get_stats()
                fs = get_fill_stats()
                _log_fn(
                    f"kalshi_ws stats: connected={s['connected']} "
                    f"subs={s['subscribed']} cached={s['cached']} "
                    f"snapshots={s['snapshots']} deltas={s['deltas']} "
                    f"bbo_updates={s['bbo_updates']} reconnects={s['reconnects']} "
                    f"errors={s['errors']} | fills={fs['fills_received']} "
                    f"cached_orders={fs['cached_orders']} hits={fs['cache_hits']} "
                    f"misses={fs['cache_misses']} | "
                    f"inv={s.get('inversions_detected',0)} "
                    f"resync_drift={s.get('resyncs_scheduled_drift',0)} "
                    f"resync_age={s.get('resyncs_scheduled_age',0)} "
                    f"resync_sent={s.get('resyncs_sent',0)} "
                    f"resync_rl={s.get('resyncs_rate_limited',0)} "
                    f"cache_skip_inv={s.get('cache_writes_skipped_inverted',0)} "
                    f"read_block_inv={s.get('bbo_reads_blocked_inverted',0)} "
                    f"pending={s.get('resync_pending',0)} | "
                    f"ob_hits={s.get('ob_hits',0)} "
                    f"ob_miss_empty={s.get('ob_misses_empty',0)} "
                    f"ob_miss_stale={s.get('ob_misses_stale',0)} "
                    f"ob_miss_inv={s.get('ob_misses_inverted',0)}"
                )
                _ws_health_check(s)
            except Exception:
                pass
    threading.Thread(target=_periodic_stats, name="kalshi_ws_stats", daemon=True).start()


def subscribe(tickers: list[str]) -> None:
    """Add tickers to the subscription set. Idempotent and safe to call
    repeatedly. New tickers are sent to the server immediately if connected;
    otherwise queued for the next reconnect."""
    new = [t for t in tickers if t and t not in _subscribed_tickers]
    if not new:
        return
    _subscribed_tickers.update(new)
    if _loop is not None:
        # Send in chunks of 200 to avoid huge frames
        for i in range(0, len(new), 200):
            chunk = new[i:i + 200]
            asyncio.run_coroutine_threadsafe(_send_subscribe(chunk), _loop)


def get_bbo(ticker: str) -> Optional[dict[str, Any]]:
    """Return cached BBO for ticker if fresh, else None.

    Returns: {'yes_bid': float, 'yes_ask': float, 'ts': float} or None
    Caller falls back to REST when this returns None.
    """
    with _cache_lock:
        e = _bbo_cache.get(ticker)
        if e is None:
            return None
        if time.time() - e["ts"] > WS_BBO_FRESH_SEC:
            return None
        # 2026-05-20: defensive read-side guard. Belt-and-suspenders with the
        # write-side guard in _recompute_bbo. Catches any path that could put
        # inverted state in the cache (e.g., direct snapshot apply, state
        # restored on startup, or guard temporarily disabled then re-enabled).
        if _INVERSION_GUARD_ENABLED:
            yb_c = int(round(e["yes_bid"] * 100))
            ya_c = int(round(e["yes_ask"] * 100))
            if yb_c > 0 and ya_c > 0 and yb_c >= ya_c:
                _bump("bbo_reads_blocked_inverted")
                return None
        return dict(e)


def get_orderbook(ticker: str) -> Optional[dict[str, list]]:
    """Return cached full L2 ladder for ticker in Kalshi REST shape, or None.

    Shape matches kalshi_client.get_orderbook():
        {"yes_dollars": [[price_str, size_str], ...],
         "no_dollars":  [[price_str, size_str], ...]}
    Each sub-array is BIDS on that side, sorted ASCENDING by price (best at end).

    Returns None when:
      - ticker not subscribed yet / no snapshot received
      - last snapshot is older than WS_BBO_FRESH_SEC (treated as stale)
      - top-of-book is inverted (yes_bid_top > yes_ask_top) — resync will be
        triggered by the next BBO recompute; caller should hit REST.
    """
    yb_map = _yes_bids.get(ticker)
    nb_map = _no_bids.get(ticker)
    if not yb_map and not nb_map:
        _bump("ob_misses_empty")
        return None
    snap_ts = _last_snapshot_ts.get(ticker, 0.0)
    if snap_ts == 0.0 or (time.time() - snap_ts) > WS_BBO_FRESH_SEC:
        _bump("ob_misses_stale")
        return None
    yes_top_c = max(yb_map.keys()) if yb_map else 0
    no_top_c = max(nb_map.keys()) if nb_map else 0
    yes_ask_top_c = (100 - no_top_c) if no_top_c > 0 else 0
    if yes_top_c > 0 and yes_ask_top_c > 0 and yes_top_c > yes_ask_top_c:
        _bump("ob_misses_inverted")
        return None

    def _rows(book: dict[int, int]) -> list[list[str]]:
        # Skip zero/negative sizes; emit dollar strings matching REST format.
        out = []
        for px_c, sz in sorted(book.items()):
            if px_c <= 0 or sz <= 0:
                continue
            out.append([f"{px_c / 100.0:.4f}", str(int(sz))])
        return out

    _bump("ob_hits")
    return {
        "yes_dollars": _rows(yb_map or {}),
        "no_dollars": _rows(nb_map or {}),
    }


def get_stats() -> dict[str, Any]:
    with _cache_lock:
        n_cached = len(_bbo_cache)
    with _stats_lock:
        out = dict(_stats)
    out["connected"] = _ws is not None
    out["subscribed"] = len(_subscribed_tickers)
    out["cached"] = n_cached
    with _resync_state_lock:
        out["resync_pending"] = len(_pending_resync)
    return out
