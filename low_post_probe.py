#!/usr/bin/env python3
"""LOW posting probe (config.PUSH_LOW_POST_AT_MID).

LOW BUY at the crossing ask is -EV (backtest: -8.1c/bet) because the LOW book
has wide spreads; the same bets are +1.8c at MID and +11.7c posted at the bid.
The bot's normal path crosses (+1c taker) and cancels an unfilled order in ~5s.
This module instead POSTS a maker limit at MID and lets it REST, then adopts the
fill ASYNCHRONOUSLY when (if) it fills -- reconcile_positions_with_kalshi is
drop-only (it never adopts), so without this the filled rest-order would be an
untracked position.

Lifecycle:
  place()  -- compute MID from the packet BBO, place a GTC maker limit, claim the
              ticker, and register the resting order with enough entry context to
              rebuild the trade record at fill time. Caller (_try_auto_execute)
              dedups via has_resting()/positions so we post once per ticker/day.
  sweep()  -- per cycle: for each resting order, adopt the fill if kalshi_ws
              reports one (record position+trade at the realized price, mirroring
              execute_buy bookkeeping; log the cross-vs-posted measurement). A
              position-held safety net catches WS fill-cache eviction. Orders
              whose climate_day has passed (market closed) are cancelled + cleared.

Flag-gated, LOW-only. HIGH and the normal cross path are untouched.
"""
from __future__ import annotations
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import config
import guardrails
import kalshi_client
import kalshi_ws
import state

log = logging.getLogger("judge.low_post")

_REG_PATH = Path(__file__).resolve().parent / "data" / "low_post_resting.json"
_lock = threading.Lock()


def _load() -> list[dict]:
    try:
        with open(_REG_PATH) as f:
            return json.load(f) or []
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning("low_post registry load failed: %s", e)
        return []


def _save(rows: list[dict]) -> None:
    try:
        _REG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _REG_PATH.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(rows, f, indent=2, default=str)
        tmp.replace(_REG_PATH)
    except Exception as e:
        log.warning("low_post registry save failed: %s", e)


def has_resting(ticker: str) -> bool:
    """True if an unadopted resting probe order exists for ticker (dedup)."""
    with _lock:
        return any(r.get("ticker") == ticker for r in _load())


def resting_rows() -> list[dict]:
    """Snapshot of currently-resting (unadopted) probe orders. The auto-exec
    position cap counts these so a resting maker order occupies its per-(station,
    direction) slot, instead of letting a second same-direction bracket slip
    through before the first fills."""
    with _lock:
        return list(_load())


def _mid_post_c(bid_c, ask_c) -> Optional[int]:
    """MID = round((bid+ask)/2), clamped to a maker price in [bid, ask-1]."""
    if bid_c is None or ask_c is None:
        return None
    try:
        bid_c = int(bid_c); ask_c = int(ask_c)
    except (TypeError, ValueError):
        return None
    if bid_c <= 0 or ask_c <= 0 or ask_c <= bid_c:
        return None
    post = int(round((bid_c + ask_c) / 2.0))
    post = max(bid_c, min(post, ask_c - 1))
    return post if post > 0 else None


def place(rt, cand, packet: dict, entry_dec, side: str,
          decision: Optional[dict] = None) -> tuple[bool, str]:
    """Post a maker limit at MID for a LOW buy and register it to rest.
    side in {"no","yes"}. Returns (placed, reason)."""
    if side == "no":
        bid_c, ask_c = packet.get("no_bid_c"), packet.get("no_ask_c")
    else:
        bid_c, ask_c = packet.get("yes_bid_c"), packet.get("yes_ask_c")
    post_c = _mid_post_c(bid_c, ask_c)
    if post_c is None:
        return False, f"low_post_no_bbo bid={bid_c} ask={ask_c}"
    bid_c, ask_c = int(bid_c), int(ask_c)

    balance_usd = kalshi_client.get_balance_cached()
    if balance_usd is None:
        return False, "low_post_no_balance"
    base_low_cap = float(config.GUARDRAILS.get("max_bet_low_series_usd", 1.0))
    side_cap = base_low_cap
    # 2026-05-24 (Chris): size up the VALIDATED LOW edge only -- NYC/DEN BUY_NO on
    # B brackets (calibrated deep-history stations, both-halves-positive; see
    # config.PUSH_LOW_NO_BET_BY_STATION). LOW YES, T tails, and all other stations
    # stay at the $1 base cap.
    if side == "no" and getattr(cand, "bracket_kind", "") == "B":
        side_cap = float(getattr(config, "PUSH_LOW_NO_BET_BY_STATION", {}).get(
            cand.station, base_low_cap))
    cnt = max(1, int(side_cap / (post_c / 100.0)))
    max_aff = int(balance_usd / (post_c / 100.0))
    if max_aff < 1:
        return False, f"low_post_balance ${balance_usd:.2f}<{post_c}c"
    cnt = min(cnt, max_aff)
    cost = cnt * (post_c / 100.0)

    # Guardrails enforces max_bet_low_series_usd as the LOW ceiling; when this cell
    # is sized above the base cap, hand check_buy a per-call override so it honors
    # the same number (still bounded by the absolute max_bet_no_usd $30 side cap).
    _gr = config.GUARDRAILS
    if side_cap > base_low_cap:
        _gr = {**config.GUARDRAILS, "max_bet_low_series_usd": side_cap}
    bd = guardrails.BuyDecision(
        ticker=cand.ticker, side=side, count=cnt, price_cents=post_c,
        cost_usd=cost, seconds_to_close=packet.get("seconds_to_close") or 0)
    ok, reason = guardrails.check_buy(rt.ctx, bd, _gr)
    if not ok:
        return False, f"low_post_guardrail:{reason[:40]}"

    spread_c = ask_c - bid_c
    state.claim_ticker(cand.ticker)
    resp = kalshi_client.place_buy(cand.ticker, side, cnt, post_c)
    if not resp.get("ok"):
        state.release_ticker(cand.ticker)
        return False, f"low_post_place_fail:{resp.get('error_code')}"
    order_id = resp.get("order_id")
    filled_now = int(resp.get("filled") or 0)

    p_yes = (decision or {}).get("p_yes")
    if side == "no":
        model_prob = (1.0 - p_yes) if p_yes is not None else None
    else:
        model_prob = p_yes
    entry_ctx = {
        "station": cand.station, "city_code": cand.city_code,
        "floor": cand.floor, "cap": cand.cap,
        "series_prefix": cand.series_prefix, "bracket_kind": cand.bracket_kind,
        "climate_day": cand.climate_day, "action": entry_dec.decision,
        "read": entry_dec.read, "conviction": entry_dec.conviction,
        "size_factor": entry_dec.size_factor, "key_risks": entry_dec.key_risks,
        "what_would_change_my_mind": entry_dec.what_would_change_my_mind,
        "obs_anchor": entry_dec.obs_anchor,
        "obs_anchor_valid": entry_dec.obs_anchor_valid,
        "obs_anchor_reason": entry_dec.obs_anchor_reason,
        "model_prob": round(model_prob, 3) if model_prob is not None else None,
    }
    row = {
        "order_id": order_id, "ticker": cand.ticker, "side": side,
        "post_c": post_c, "bid_c": bid_c, "ask_c": ask_c, "cross_c": ask_c,
        "spread_c": spread_c, "cnt": cnt, "climate_day": cand.climate_day,
        "placed_ts": time.time(), "entry_ctx": entry_ctx,
    }
    with _lock:
        rows = _load()
        rows.append(row)
        _save(rows)

    try:
        state.log_decision({"kind": "low_post_place", "ticker": cand.ticker,
            "side": side, "post_c": post_c, "bid_c": bid_c, "ask_c": ask_c,
            "cross_c": ask_c, "spread_c": spread_c, "cnt": cnt,
            "order_id": order_id, "filled_on_place": filled_now, "ts": time.time()})
    except Exception:
        pass
    _discord(
        f"\U0001F4EE **LOW-POST** `{cand.ticker}` {side.upper()} {cnt}x @ {post_c}c "
        f"(mid; bid {bid_c}/ask {ask_c}, spread {spread_c}c) — "
        f"{'filled '+str(filled_now)+' on place' if filled_now>0 else 'resting'} "
        f"(cross would pay {ask_c}c)")

    if filled_now > 0:
        # Rare: the clamp made it marketable and it took immediately. Maker
        # fills execute at our limit, so realized == post_c.
        _adopt(rt, row, filled_now, post_c / 100.0)

    log.info("LOW-POST %s: %dx %s @ %dc (mid; bid %d ask %d) order=%s filled_now=%d",
             cand.ticker, cnt, side, post_c, bid_c, ask_c, order_id, filled_now)
    return True, (f"low_post {side} @ {post_c}c (mid; bid {bid_c}/ask {ask_c}, "
                  f"spread {spread_c}c, cross {ask_c}c) filled_now={filled_now}")


def _adopt(rt, row: dict, filled: int, realized_price_dollars: float,
           via: str = "ws_fill") -> None:
    """Record a filled probe order as a real position+trade (mirrors
    execute_buy bookkeeping) and log the cross-vs-posted measurement."""
    ctx = row["entry_ctx"]
    ticker = row["ticker"]
    filled_cost = filled * realized_price_dollars
    realized_c = int(round(realized_price_dollars * 100))
    rec = {
        "ts": time.time(), "kind": "entry", "market_ticker": ticker,
        "action": ctx["action"], "entry_price": realized_price_dollars,
        "count": filled, "cost": round(filled_cost, 4), "order_id": row["order_id"],
        "station": ctx["station"], "date_str": ctx["climate_day"],
        "label": ctx["city_code"], "floor": ctx["floor"], "cap": ctx["cap"],
        "opened_by": "paper-judge", "series": ctx["series_prefix"],
        "bracket_kind": ctx["bracket_kind"], "market_price_c": realized_c,
        "model_prob": ctx.get("model_prob"), "gap_pp": None,
        "judge": {
            "conviction": ctx["conviction"], "size_factor": ctx["size_factor"],
            "read": ctx["read"], "key_risks": ctx["key_risks"],
            "what_would_change_my_mind": ctx["what_would_change_my_mind"],
            "obs_anchor": ctx["obs_anchor"], "obs_anchor_valid": ctx["obs_anchor_valid"],
            "obs_anchor_reason": ctx["obs_anchor_reason"],
            "low_post": True, "post_c": row["post_c"], "cross_c": row["cross_c"],
            "adopt_via": via,
        },
    }
    try:
        state.log_trade(rec)
        state.upsert_entry(rt.positions, ticker, rec)
        guardrails.record_buy(rt.ctx, ticker, filled_cost)
        rt.persist_positions()
        kalshi_client.invalidate_balance_cache()
    except Exception as e:
        log.exception("low_post adopt bookkeeping failed for %s: %s", ticker, e)
    saved_c = row["cross_c"] - realized_c
    try:
        state.log_decision({"kind": "low_post_fill", "ticker": ticker,
            "side": row["side"], "post_c": row["post_c"], "realized_c": realized_c,
            "cross_c": row["cross_c"], "saved_c": saved_c, "spread_c": row["spread_c"],
            "filled": filled, "via": via,
            "time_to_fill_s": round(time.time() - row.get("placed_ts", time.time()), 1),
            "ts": time.time()})
    except Exception:
        pass
    _discord(
        f"✅ **LOW-POST FILL** `{ticker}` {row['side'].upper()} {filled}x @ {realized_c}c "
        f"(posted {row['post_c']}c; cross would pay {row['cross_c']}c → "
        f"saved {saved_c}c/contract; spread was {row['spread_c']}c; via {via})")
    log.info("LOW-POST FILL %s: %dx @ %dc (cross %dc saved %dc via %s)",
             ticker, filled, realized_c, row["cross_c"], saved_c, via)


def sweep(rt) -> None:
    """Per-cycle: adopt fills on resting probe orders; clean up stale ones.
    Call from one_cycle right after reconcile_positions_with_kalshi(rt)."""
    with _lock:
        rows = _load()
    if not rows:
        return
    today = guardrails.today_utc(time.time())
    held = getattr(rt, "wallet_held_tickers", None) or set()
    done_ids: set = set()   # order_ids adopted or expired -> drop from registry
    for row in rows:
        oid = row.get("order_id")
        ticker = row.get("ticker")
        side = row.get("side")
        if ticker in getattr(rt, "positions", {}):
            # Already tracked (adopted earlier or filled-on-place). Drop.
            done_ids.add(oid)
            continue
        # 1) Primary: WS fill cache gives count + realized (avg) price.
        f = kalshi_ws.get_fill(oid) if oid else None
        filled = 0
        realized = None
        if f:
            try:
                filled = int(float(f.get("total_count") or 0))
            except (TypeError, ValueError):
                filled = 0
            if filled > 0:
                notional = (f.get("total_no_notional_dollars") if side == "no"
                            else f.get("total_yes_notional_dollars")) or 0.0
                realized = (notional / filled) if filled else None
        if filled > 0 and realized:
            _adopt(rt, row, filled, realized, via="ws_fill")
            done_ids.add(oid)
            continue
        # 2) Safety net: Kalshi holds the ticker but the WS fill was missed/
        #    evicted. Maker fills execute at our limit, so realized == post_c.
        if ticker in held:
            _adopt(rt, row, row.get("cnt", 1), row.get("post_c", 0) / 100.0,
                   via="position_fallback")
            done_ids.add(oid)
            continue
        # 3) Stale: climate day passed -> market closed. Cancel + release.
        if str(row.get("climate_day", "")) < today:
            try:
                kalshi_client.cancel_order(oid)
            except Exception:
                pass
            try:
                state.release_ticker(ticker)
            except Exception:
                pass
            try:
                state.log_decision({"kind": "low_post_expired", "ticker": ticker,
                    "side": side, "post_c": row.get("post_c"),
                    "cross_c": row.get("cross_c"), "spread_c": row.get("spread_c"),
                    "order_id": oid, "ts": time.time()})
            except Exception:
                pass
            log.info("LOW-POST EXPIRED %s: unfilled at climate-day close, cancelled",
                     ticker)
            done_ids.add(oid)
            continue
        # else: still resting, keep it.
    # Rewrite registry: re-load (place() may have appended concurrently) and
    # drop only the order_ids we adopted/expired this pass.
    with _lock:
        cur = _load()
        merged = [r for r in cur if r.get("order_id") not in done_ids]
        _save(merged)


def _discord(msg: str) -> None:
    try:
        from paper_judge_bot import discord_send
        discord_send(msg)
    except Exception:
        pass
