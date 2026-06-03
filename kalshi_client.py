"""kalshi_client.py — Kalshi REST + order placement.

Adapted from paper_min_bot's auth pattern. Same RSA-PSS signing scheme used by
all 4 existing bots — we just bind to a dedicated key for this bot.

If you set DRY_RUN=True in config, place_order() / place_sell() return a fake
order_id and never hit the network. Use this for end-to-end smoke tests.
"""
from __future__ import annotations

import base64
import logging
import time
from pathlib import Path
from typing import Any, Optional

import config


log = logging.getLogger("judge.kalshi")


# ─────────────────────────────────────────────────────────────────────────────
# Auth — lazy load on first call, cached for process lifetime
# ─────────────────────────────────────────────────────────────────────────────
_PRIVATE_KEY = None


def _load_private_key():
    global _PRIVATE_KEY
    if _PRIVATE_KEY is None:
        try:
            from cryptography.hazmat.primitives import serialization
        except ImportError as e:
            raise RuntimeError("cryptography not installed") from e
        if not config.KALSHI_PEM_PATH.exists():
            raise RuntimeError(f"PEM missing: {config.KALSHI_PEM_PATH}")
        _PRIVATE_KEY = serialization.load_pem_private_key(
            config.KALSHI_PEM_PATH.read_bytes(), password=None
        )
    return _PRIVATE_KEY


def _sign(method: str, path: str) -> dict[str, str]:
    """Mirror of paper_min_bot's _sign — RSA-PSS over (ts_ms || method || path)."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    key = _load_private_key()
    ts_ms = str(int(time.time() * 1000))
    msg = (ts_ms + method + path).encode()
    sig = key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    if not config.KALSHI_KEY_ID:
        raise RuntimeError("KALSHI_KEY_ID not set")
    return {
        "KALSHI-ACCESS-KEY": config.KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "Content-Type": "application/json",
    }


# ─────────────────────────────────────────────────────────────────────────────
# REST helpers
# ─────────────────────────────────────────────────────────────────────────────
def _httpx():
    """Lazy-import httpx so unit tests don't need it."""
    import httpx
    return httpx


def get(path: str, params: dict | None = None) -> dict:
    httpx = _httpx()
    r = httpx.get(
        config.KALSHI_API_BASE + path,
        params=params,
        headers=_sign("GET", path),
        timeout=config.KALSHI_TIMEOUT_SEC,
    )
    r.raise_for_status()
    return r.json()


def post(path: str, body: dict) -> dict:
    httpx = _httpx()
    r = httpx.post(
        config.KALSHI_API_BASE + path,
        json=body,
        headers=_sign("POST", path),
        timeout=config.KALSHI_TIMEOUT_SEC,
    )
    if r.status_code >= 400:
        raise httpx.HTTPStatusError(
            f"{r.status_code}: {r.text[:200]}", request=r.request, response=r
        )
    return r.json()


def delete(path: str) -> dict:
    httpx = _httpx()
    r = httpx.delete(
        config.KALSHI_API_BASE + path,
        headers=_sign("DELETE", path),
        timeout=config.KALSHI_TIMEOUT_SEC,
    )
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Public API used by the daemon
# ─────────────────────────────────────────────────────────────────────────────
def get_balance() -> Optional[float]:
    """Available balance in USD (live fetch, no cache)."""
    try:
        d = get("/trade-api/v2/portfolio/balance")
        bc = d.get("balance")
        return float(bc) / 100.0 if bc is not None else None
    except Exception as e:
        log.warning("balance fetch failed: %s", e)
        return None


# Cached balance — wallet is shared with V1/V2 max+min bots, so we
# need to refresh frequently but skipping a live fetch on every cycle
# saves an HTTP round-trip per candidate. TTL is short (15s) so a buy
# from a sibling bot is observed within one scan cycle.
_BALANCE_TTL_SEC = 15.0
_balance_cache: Optional[float] = None
_balance_cache_ts: float = 0.0


def get_balance_cached(force_refresh: bool = False) -> Optional[float]:
    """Return cached available balance in USD; refresh if stale or forced.
    Returns None only if no successful fetch has ever happened (cold start).
    """
    global _balance_cache, _balance_cache_ts
    now = time.time()
    if force_refresh or _balance_cache is None or (now - _balance_cache_ts) > _BALANCE_TTL_SEC:
        b = get_balance()
        if b is not None:
            _balance_cache = b
            _balance_cache_ts = now
    return _balance_cache


def invalidate_balance_cache() -> None:
    """Force the next get_balance_cached() to re-fetch from Kalshi.
    Call after any insufficient_balance error so the retry sees fresh state."""
    global _balance_cache_ts
    _balance_cache_ts = 0.0


def list_open_markets(series_prefix: str, status: str = "open", limit: int = 200) -> list[dict]:
    """List open markets matching a series ticker prefix.
    The bot scans by series (e.g., KXLOWTDC, KXHIGHATL) — caller filters by station + date."""
    out: list[dict] = []
    cursor = ""
    for _ in range(8):  # cap pagination loops
        params = {
            "series_ticker": series_prefix,
            "status": status,
            "limit": min(limit, 200),
        }
        if cursor:
            params["cursor"] = cursor
        d = get("/trade-api/v2/markets", params)
        out.extend(d.get("markets") or [])
        cursor = d.get("cursor") or ""
        if not cursor:
            break
    return out


def get_market(ticker: str) -> dict:
    return get(f"/trade-api/v2/markets/{ticker}").get("market") or {}


def get_orderbook(ticker: str) -> dict:
    """Fetch the orderbook. Returns the inner dict:
        {"no_dollars": [[price_str, size_str], ...], "yes_dollars": [...]}

    Kalshi response shape (as of 2026-05-15):
        {"orderbook_fp": {"no_dollars": [...], "yes_dollars": [...]}}
    Each sub-array is a list of [price_dollar_string, size_string] pairs
    representing BIDS on that side, sorted ascending by price. Best bid
    is therefore at the END of each array (highest price someone will pay).

    Use offers_for_side() to translate to cheapest-first "asks" we'd cross
    when buying a given side.
    """
    d = get(f"/trade-api/v2/markets/{ticker}/orderbook")
    return d.get("orderbook_fp") or d.get("orderbook") or {}


def offers_for_side(orderbook: dict, side: str) -> list[tuple[int, int]]:
    """Convert Kalshi orderbook → cheapest-first list of [(price_c, count)]
    representing offers we'd CROSS when buying `side` ("no" or "yes").

    Mechanics: to BUY NO at price R, our order crosses with a YES BID at
    (100 − R). So NO offers (= what we pay to buy NO) come from the
    yes_dollars array. Symmetrically, YES offers come from no_dollars.

    Price comes in as a dollar string ("0.4700"), size as a string ("3.00").
    """
    other_side_key = "yes_dollars" if side == "no" else "no_dollars"
    bids = orderbook.get(other_side_key) or []
    out: list[tuple[int, int]] = []
    for row in bids:
        try:
            price_d = float(row[0])  # bid price on the OTHER side, in dollars
            count = int(float(row[1]))
            inv_c = 100 - int(round(price_d * 100))
            if 0 < inv_c < 100 and count > 0:
                out.append((inv_c, count))
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda x: x[0])  # cheapest first = highest other-side bid first
    return out


def list_positions() -> list[dict]:
    """All open positions on this account."""
    d = get("/trade-api/v2/portfolio/positions", {"limit": 200})
    return d.get("market_positions") or []


def open_position_tickers() -> set[str]:
    """Return the set of ticker IDs that have a non-zero open position on
    the WALLET (across all bots sharing this key). Used by the entry loop
    to skip candidates that another bot already owns."""
    try:
        recs = list_positions()
    except Exception as e:
        log.warning("open_position_tickers failed: %s", e)
        return set()
    out: set[str] = set()
    for r in recs:
        tkr = r.get("ticker")
        # Kalshi v2 uses 'position_fp' (signed float string): positive=long YES,
        # negative=long NO. Any non-zero magnitude = held.
        count = r.get("position_fp", 0) or r.get("position", 0) or 0
        try:
            count = int(float(count))
        except (TypeError, ValueError):
            count = 0
        if tkr and count != 0:
            out.add(tkr)
    return out


def list_settlements(limit: int = 100) -> list[dict]:
    d = get("/trade-api/v2/portfolio/settlements", {"limit": limit})
    return d.get("settlements") or []


def _parse_kalshi_error(exc: Exception) -> tuple[str, str]:
    """Extract (error_code, error_msg) from a Kalshi HTTP error.
    Falls back to ("unknown", str(exc)) when the body isn't the expected shape."""
    import json
    msg = str(exc)
    try:
        # Error string format: "400: {\"error\":{\"code\":\"insufficient_balance\",\"message\":\"...\"}}"
        if ": " in msg:
            body = msg.split(": ", 1)[1]
            d = json.loads(body)
            err = d.get("error") or {}
            return (str(err.get("code") or "unknown"), str(err.get("message") or msg))
    except Exception:
        pass
    return ("unknown", msg)


def place_buy(ticker: str, side: str, count: int, price_cents: int,
              expiration_ts: int | None = None, post_only: bool = False) -> dict:
    """Place a limit BUY at price_cents. side ∈ {"yes", "no"}.

    Returns a dict {ok, order_id, status, filled, error_code, error_msg}:
      - ok=True on successful submission (whether executed or resting)
      - ok=False with error_code on failure (caller can branch on
        "insufficient_balance" to retry with reduced size)
    Honors DRY_RUN.
    """
    if side not in ("yes", "no"):
        log.warning("place_buy bad side: %s", side)
        return {"ok": False, "order_id": None, "status": None, "filled": 0,
                "error_code": "bad_side", "error_msg": f"bad side: {side}"}
    body: dict[str, Any] = {
        "ticker": ticker,
        "action": "buy",
        "side": side,
        "type": "limit",
        "count": count,
    }
    body["yes_price" if side == "yes" else "no_price"] = price_cents
    if expiration_ts:
        body["expiration_ts"] = int(expiration_ts)
    if post_only:
        body["post_only"] = True

    if config.DRY_RUN:
        fake = f"DRYRUN-buy-{ticker}-{side}-{int(time.time()*1000)}"
        log.info("DRY_RUN buy %dx %s @ %dc on %s -> %s",
                 count, side, price_cents, ticker, fake)
        return {"ok": True, "order_id": fake, "status": "executed",
                "filled": count, "error_code": None, "error_msg": None}

    try:
        r = post("/trade-api/v2/portfolio/orders", body)
        order = r.get("order") or {}
        oid = order.get("order_id")
        status = order.get("status", "?")
        # Kalshi marks status="executed" once fully filled. fill_count_fp is
        # the contracts filled. If status is executed, treat filled = count.
        try:
            filled = int(float(order.get("fill_count_fp") or 0))
        except (TypeError, ValueError):
            filled = 0
        if status == "executed":
            filled = max(filled, count)
        log.info("BUY %dx %s @ %dc on %s -> %s (%s, filled=%d)",
                 count, side, price_cents, ticker, oid, status, filled)
        return {"ok": True, "order_id": oid, "status": status, "filled": filled,
                "error_code": None, "error_msg": None}
    except Exception as e:
        code, msg = _parse_kalshi_error(e)
        log.error("BUY FAILED %s %s %d@%dc: %s", ticker, side, count, price_cents, e)
        return {"ok": False, "order_id": None, "status": None, "filled": 0,
                "error_code": code, "error_msg": msg}


def place_sell(ticker: str, side: str, count: int, price_cents: int) -> Optional[dict]:
    """Place a limit SELL. side is the side we HOLD.
    Returns dict {order_id, status, filled} or None on failure."""
    if side not in ("yes", "no"):
        log.warning("place_sell bad side: %s", side)
        return None
    body: dict[str, Any] = {
        "ticker": ticker,
        "action": "sell",
        "side": side,
        "type": "limit",
        "count": count,
    }
    body["yes_price" if side == "yes" else "no_price"] = price_cents

    if config.DRY_RUN:
        fake = f"DRYRUN-sell-{ticker}-{side}-{int(time.time()*1000)}"
        log.info("DRY_RUN sell %dx %s @ %dc on %s -> %s",
                 count, side, price_cents, ticker, fake)
        return {"order_id": fake, "status": "executed", "filled": count}

    try:
        r = post("/trade-api/v2/portfolio/orders", body)
        order = r.get("order") or {}
        oid = order.get("order_id")
        status = order.get("status", "?")
        try:
            filled = int(float(order.get("fill_count_fp") or 0))
        except (TypeError, ValueError):
            filled = 0
        if status == "executed":
            filled = max(filled, count)
        log.info("SELL %dx %s @ %dc on %s -> %s (%s, filled=%d)",
                 count, side, price_cents, ticker, oid, status, filled)
        return {"order_id": oid, "status": status, "filled": filled}
    except Exception as e:
        log.error("SELL FAILED %s %s %d@%dc: %s", ticker, side, count, price_cents, e)
        return None


def cancel_order(order_id: str) -> None:
    if config.DRY_RUN:
        log.info("DRY_RUN cancel %s", order_id)
        return
    try:
        delete(f"/trade-api/v2/portfolio/orders/{order_id}")
    except Exception as e:
        log.warning("cancel %s failed: %s", order_id, e)


def get_order(order_id: str) -> Optional[dict]:
    """Fetch a single order's AUTHORITATIVE state from Kalshi (status +
    fill_count_fp + remaining_count). Returns None on query failure (e.g. a 404
    for an order Kalshi has already purged). The taker-fallback uses this to
    CONFIRM a maker is dead (status in canceled/executed, remaining 0) and to read
    how much filled BEFORE crossing -- the core double-buy guard. Never raises."""
    if config.DRY_RUN:
        return {"order_id": order_id, "status": "canceled",
                "fill_count_fp": "0", "remaining_count": 0}
    try:
        d = get(f"/trade-api/v2/portfolio/orders/{order_id}")
        return (d or {}).get("order") or None
    except Exception as e:
        log.warning("get_order %s failed: %s", order_id, e)
        return None


def order_filled_count(order: dict) -> int:
    """Parse the filled-contract count from an order dict (Kalshi fill_count_fp)."""
    if not order:
        return 0
    try:
        return int(float(order.get("fill_count_fp") or 0))
    except (TypeError, ValueError):
        return 0


def wait_for_fill(order_id: str, expected_count: int, timeout_sec: float = 5.0
                  ) -> tuple[str, int]:
    """Poll /portfolio/orders/{id} until fill_count_fp >= expected_count or timeout.

    WS fast path: when kalshi_ws is running, check its fill-channel cache each
    iteration before hitting REST. If the WS cache already has filled>=expected,
    return immediately. Falls back transparently if WS not started or empty
    (kalshi_ws.get_fill returns None and we drop into the REST poll)."""
    if config.DRY_RUN:
        return ("filled", expected_count)
    try:
        import kalshi_ws as _ws  # noqa: F401 — safe even if start() never called
    except Exception:
        _ws = None
    deadline = time.time() + timeout_sec
    last_status = "?"
    last_filled = 0
    # Try WS once up-front (single dict lookup, no sleep) before any REST.
    # NOTE 2026-05-16 (B2 fix): kalshi_ws._record_fill stores accumulated fill
    # size as `total_count`, not `filled`. Pre-fix this lookup always saw 0 and
    # fell through to 0.25s-interval REST polling — silent disable of the WS
    # fast-path. Field name now matches the WS module's schema.
    if _ws is not None:
        try:
            ws_fill = _ws.get_fill(order_id)
        except Exception:
            ws_fill = None
        if ws_fill and int(float(ws_fill.get("total_count", 0) or 0)) >= expected_count:
            return ("executed", int(float(ws_fill["total_count"])))
    while time.time() < deadline:
        # Check WS first each iteration — almost always wins the race.
        if _ws is not None:
            try:
                ws_fill = _ws.get_fill(order_id)
            except Exception:
                ws_fill = None
            if ws_fill and int(float(ws_fill.get("total_count", 0) or 0)) >= expected_count:
                return ("executed", int(float(ws_fill["total_count"])))
        try:
            d = get(f"/trade-api/v2/portfolio/orders/{order_id}")
            order = d.get("order") or {}
            last_status = order.get("status", "?")
            last_filled = int(order.get("fill_count_fp", 0) or 0)
            if last_filled >= expected_count or last_status in ("canceled", "executed"):
                break
        except Exception as e:
            log.warning("wait_for_fill %s err: %s", order_id, e)
        time.sleep(0.25)
    return (last_status, last_filled)
