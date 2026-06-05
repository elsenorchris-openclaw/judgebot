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


def _cur_side_mid_c(ticker: str, side: str) -> Optional[int]:
    """Current MID for the order's side in cents from the WS BBO cache (None if no
    fresh book). NO mid = 100 - yes_mid. Used to detect an adverse move vs our bid."""
    bbo = kalshi_ws.get_bbo(ticker)
    if not bbo:
        return None
    try:
        yb = float(bbo["yes_bid"]) * 100.0
        ya = float(bbo["yes_ask"]) * 100.0
    except (KeyError, TypeError, ValueError):
        return None
    if yb <= 0 or ya <= 0 or ya <= yb:
        return None
    yes_mid = (yb + ya) / 2.0
    return int(round(yes_mid if side == "yes" else (100.0 - yes_mid)))


def _cur_side_ask_c(ticker: str, side: str) -> Optional[int]:
    """Current ASK (the cross/taker price) for the order's side in cents, from the
    WS BBO. YES ask = yes_ask; NO ask = 100 - yes_bid. None if no fresh two-sided
    book. Used by the taker-fallback to re-price + re-validate edge before crossing."""
    bbo = kalshi_ws.get_bbo(ticker)
    if not bbo:
        return None
    try:
        yb = float(bbo["yes_bid"]) * 100.0
        ya = float(bbo["yes_ask"]) * 100.0
    except (KeyError, TypeError, ValueError):
        return None
    if yb <= 0 or ya <= 0 or ya <= yb:
        return None
    ask = ya if side == "yes" else (100.0 - yb)
    ask_c = int(round(ask))
    return ask_c if 0 < ask_c <= 100 else None


def _fallback_deadline_and_ttl(packet: dict, is_low: bool) -> tuple[float, int]:
    """(fallback_deadline_ts, maker_ttl_s). The deadline (ABSOLUTE UTC, survives restart)
    is when sweep() should cross an unfilled maker as a taker = now + (h_to_event -
    window_close_lead - fb_lead)h. window_close_lead = the series' deep-window close
    offset (LOW 1.5h / HIGH 2.5h); fb_lead fires the cross slightly before close.

    2026-06-04 (Chris): the maker now RESTS until that deadline (ttl = deadline - now,
    floored 30s) instead of a short 90s TTL. The old 90s churn relied on the EVENT-DRIVEN
    eval re-firing to re-post, unreliable on thin pre-dawn LOW books (WS-quiet) -> the
    maker was dropped and never re-posted, so the taker-fallback never fired (6/4: 14
    posts, 10 ttl_expired, 0 crosses, 0 LOW fills). Resting to the deadline keeps a maker
    on the book until sweep() 3b crosses it (if the edge still clears); the adverse-move
    guard (3a) still cancels early on a hostile move. Only LOW + TAKER_FALLBACK_ENABLED
    gets the long rest (HIGH is taker, no low_post); otherwise the base TTL is used."""
    lc = packet.get("local_clock") or {}
    h_to_evt = lc.get("h_to_min") if is_low else lc.get("h_to_peak")
    try:
        wc_lead = float((config.BLEND_DEEP_WINDOW_HOURS_LOW if is_low
                         else config.BLEND_DEEP_WINDOW_HOURS)[1])
    except Exception:
        wc_lead = 1.5 if is_low else 2.5
    fb_lead = float(getattr(config, "TAKER_FALLBACK_LEAD_H", 0.2))
    if h_to_evt is not None:
        fb_deadline = time.time() + max(0.0, (float(h_to_evt) - wc_lead - fb_lead)) * 3600.0
    else:
        fb_deadline = time.time() + float(getattr(config, "TAKER_FALLBACK_MAX_REST_S", 600))
    base_ttl = int(getattr(config, "PUSH_LOW_POST_TTL_S", 0) or 0)
    if is_low and getattr(config, "TAKER_FALLBACK_ENABLED", False):
        ttl = max(30, int(fb_deadline - time.time()))
    else:
        ttl = base_ttl
    return fb_deadline, ttl


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
    is_low = (cand.series_prefix == "KXLOW")
    base_low_cap = float(config.GUARDRAILS.get("max_bet_low_series_usd", 1.0))
    if is_low:
        side_cap = base_low_cap
        # 2026-05-24 (Chris): size up the VALIDATED LOW edge only -- NYC/DEN BUY_NO on
        # B brackets (calibrated deep-history stations, both-halves-positive; see
        # config.PUSH_LOW_NO_BET_BY_STATION). LOW YES, T tails, and all other stations
        # stay at the $1 base cap.
        if side == "no" and getattr(cand, "bracket_kind", "") == "B":
            side_cap = float(getattr(config, "PUSH_LOW_NO_BET_BY_STATION", {}).get(
                cand.station, base_low_cap))
    else:
        # 2026-06-03 (Chris): HIGH maker-first. Honor the worker's already-sized target
        # (packet.push_target_usd = decision.size_usd -- carries the per-station cap,
        # edge-tilt, fat-edge de-size, same value execute_buy would use); fall back to
        # the HIGH side cap. This keeps HIGH sizing identical to the taker path.
        _tgt = packet.get("push_target_usd")
        if _tgt and float(_tgt) > 0:
            side_cap = float(_tgt)
        else:
            side_cap = float(getattr(config, "PUSH_HIGH_YES_MAX_BET_USD", 10.0) if side == "yes"
                             else getattr(config, "PUSH_HIGH_MAX_BET_DEFAULT", 10.0))
    cnt = max(1, int(side_cap / (post_c / 100.0)))
    max_aff = int(balance_usd / (post_c / 100.0))
    if max_aff < 1:
        return False, f"low_post_balance ${balance_usd:.2f}<{post_c}c"
    cnt = min(cnt, max_aff)
    cost = cnt * (post_c / 100.0)

    # Guardrails: a LOW cell sized above the base cap needs a per-call override so
    # check_buy honors the same number; HIGH uses the standard max_bet_high_series_usd
    # backstop (the $10 cap is under the $20 backstop, so no override needed).
    _gr = config.GUARDRAILS
    if is_low and side_cap > base_low_cap:
        _gr = {**config.GUARDRAILS, "max_bet_low_series_usd": side_cap}
    bd = guardrails.BuyDecision(
        ticker=cand.ticker, side=side, count=cnt, price_cents=post_c,
        cost_usd=cost, seconds_to_close=packet.get("seconds_to_close") or 0)
    ok, reason = guardrails.check_buy(rt.ctx, bd, _gr)
    if not ok:
        return False, f"low_post_guardrail:{reason[:40]}"

    spread_c = ask_c - bid_c
    state.claim_ticker(cand.ticker)
    # Taker-fallback deadline + maker TTL: rest the maker until the deadline so the
    # cross reliably fires (see _fallback_deadline_and_ttl). Computed BEFORE place_buy.
    _fb_deadline, _ttl = _fallback_deadline_and_ttl(packet, cand.series_prefix == "KXLOW")
    _exp = (int(time.time()) + _ttl) if _ttl else None
    resp = kalshi_client.place_buy(
        cand.ticker, side, cnt, post_c, expiration_ts=_exp,
        post_only=bool(getattr(config, "PUSH_LOW_POST_POST_ONLY", False)))
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
        "placed_ts": time.time(), "ttl_s": _ttl, "entry_ctx": entry_ctx,
        "fallback_deadline_ts": _fb_deadline, "model_prob": model_prob,
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


def _fallback_walk(ticker: str, row: dict, reason: str) -> None:
    """Give up on an order WITHOUT crossing (edge gone / no live ask / disabled).
    Release the claim so a future eval can re-evaluate the ticker cleanly."""
    try:
        state.release_ticker(ticker)
    except Exception:
        pass
    try:
        state.log_decision({"kind": "taker_fallback_walk", "ticker": ticker,
            "side": row.get("side"), "reason": reason, "post_c": row.get("post_c"),
            "order_id": row.get("order_id"), "ts": time.time()})
    except Exception:
        pass
    log.info("TAKER-FALLBACK WALK %s: %s", ticker, reason)


def _taker_fallback(rt, row: dict, held: set) -> bool:
    """Double-buy-safe maker->taker cross for an unfilled resting order at its
    fallback deadline (near window-close). Returns True when the order is RESOLVED
    (adopted / crossed / walked -> drop from registry), or False to RETRY next
    sweep (the cancel could not be CONFIRMED -> we must never cross a possibly-live
    order). Safety sequence (cf the edge-case table):
      1. cancel the resting maker.
      2. get_order -> AUTHORITATIVE state. Unavailable / still-live -> retry (no cross).
      3. our order filled (full/partial) -> ADOPT that fill, never cross.
      4. fresh wallet holds the ticker but our order shows 0 fill -> another bot owns
         it (co-exist) -> skip (no cross, no adopt).
      5. confirmed dead + 0 fill + not held -> re-validate edge at the LIVE ask;
         cross as taker only if it still clears the bar, else walk.
    """
    oid = row.get("order_id"); ticker = row.get("ticker"); side = row.get("side")
    # 1) Cancel the resting maker (best effort).
    try:
        kalshi_client.cancel_order(oid)
    except Exception:
        pass
    # 2) Authoritative final state. If we cannot read it, DO NOT cross (retry).
    o = kalshi_client.get_order(oid)
    if o is None:
        log.warning("taker-fallback %s: get_order failed -> retry, NO cross", ticker)
        return False
    status = str(o.get("status") or "")
    our_filled = kalshi_client.order_filled_count(o)
    try:
        remaining = o.get("remaining_count")
        remaining = int(float(remaining)) if remaining is not None else None
    except (TypeError, ValueError):
        remaining = None
    dead = (status in ("canceled", "executed", "expired")) or (remaining == 0)
    if not dead:
        log.warning("taker-fallback %s: order still LIVE (status=%s rem=%s) -> retry, NO cross",
                    ticker, status, remaining)
        return False
    # 3) Our maker filled (fully/partially) -> adopt that fill, never cross.
    if our_filled > 0:
        # 2026-06-05 (audit): adopt the LARGER of REST (order_filled_count = authoritative
        # fill_count_fp) and the WS fill cache (which can lag / be evicted and UNDER-report).
        # The old code PREFERRED WS whenever it was >0, so a partial/evicted WS count
        # silently under-recorded the fill -> real contracts left untracked in _rt.positions
        # (they settle on Kalshi but the bot's P&L/exit never sees them; reconcile is
        # drop-only). A maker fills at its limit (post_c), so adopt at post_c.
        f = kalshi_ws.get_fill(oid)
        ws_fl = int(float(f.get("total_count") or 0)) if f else 0
        fl = max(our_filled, ws_fl)
        _adopt(rt, row, fl, row.get("post_c", 0) / 100.0, via="maker_fill_at_deadline")
        return True
    # 4) Held by the wallet but OUR order is 0-filled -> another bot's position.
    try:
        fresh_held = kalshi_client.open_position_tickers()
    except Exception:
        fresh_held = set()
    if ticker in fresh_held or ticker in (held or set()):
        log.info("taker-fallback %s: held but our order 0-filled -> coexist skip (NO cross)", ticker)
        try:
            state.release_ticker(ticker)
        except Exception:
            pass
        return True
    # 5) Confirmed dead, 0 fill, not held -> cross as taker IF the edge still clears.
    if not getattr(config, "TAKER_FALLBACK_ENABLED", False):
        _fallback_walk(ticker, row, "disabled_guard")
        return True
    ask_c = _cur_side_ask_c(ticker, side)
    if ask_c is None:
        _fallback_walk(ticker, row, "no_live_ask")
        return True
    model_prob = row.get("model_prob")
    min_edge = float(getattr(config, "TAKER_FALLBACK_MIN_EDGE_PP", 8.0)) / 100.0
    max_cross_c = int(getattr(config, "TAKER_FALLBACK_MAX_CROSS_C", 90))
    edge_now = (float(model_prob) - ask_c / 100.0) if model_prob is not None else None
    if edge_now is None or edge_now < min_edge or ask_c > max_cross_c:
        _fallback_walk(ticker, row, f"edge_gone edge={edge_now} ask={ask_c}c max={max_cross_c}c")
        return True
    cnt = int(row.get("cnt", 1) or 1)
    _exp_s = int(getattr(config, "TAKER_CROSS_EXPIRY_S", 5) or 0)
    _exp_ts = (int(time.time()) + _exp_s) if _exp_s else None
    resp = kalshi_client.place_buy(ticker, side, cnt, ask_c,
                                   expiration_ts=_exp_ts, post_only=False)
    crossed = int(resp.get("filled") or 0) if resp.get("ok") else 0
    if crossed > 0:
        _adopt(rt, {**row, "cross_c": ask_c}, crossed, ask_c / 100.0, via="taker_fallback")
        if crossed < cnt:
            log.warning("taker-fallback %s: PARTIAL cross %d/%d @ %dc (remainder expires in %ss)",
                        ticker, crossed, cnt, ask_c, _exp_s)
        _discord(f"\U0001F501 **TAKER-FALLBACK** `{ticker}` {side.upper()} crossed {crossed}x "
                 f"@ {ask_c}c (maker unfilled at window-close; edge {edge_now*100:.1f}pp)")
    else:
        log.warning("taker-fallback %s: cross got 0 fill (ok=%s err=%s) -> walk",
                    ticker, resp.get("ok"), resp.get("error_code"))
        try:
            state.release_ticker(ticker)
        except Exception:
            pass
    return True


def sweep(rt) -> None:
    """Per-cycle: adopt fills on resting probe orders; cross unfilled ones at the
    taker-fallback deadline; clean up stale ones. Call from one_cycle right after
    reconcile_positions_with_kalshi(rt)."""
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
        # 3) Intra-day risk mgmt (2026-05-26): native TTL-expiry + adverse-move.
        #    The order carries a Kalshi expiration_ts, so it auto-cancels after
        #    ttl_s; drop the expired row so the model-gated auto-exec can re-post
        #    fresh at the live mid. Also cancel EARLY if our side's mid has fallen
        #    >= PUSH_LOW_POST_ADVERSE_C below our bid (a collapse picking us off).
        age_s = time.time() - float(row.get("placed_ts") or time.time())
        ttl_s = int(row.get("ttl_s") or getattr(config, "PUSH_LOW_POST_TTL_S", 0) or 0)
        adv = int(getattr(config, "PUSH_LOW_POST_ADVERSE_C", 0) or 0)
        cur_mid = _cur_side_mid_c(ticker, side) if adv else None
        adverse = (adv and cur_mid is not None
                   and (int(row.get("post_c", 0)) - cur_mid) >= adv)
        expired = bool(ttl_s) and age_s >= ttl_s
        # 3a) Adverse move -> cancel early; NEVER cross into a collapse (top priority,
        #     ahead of the taker-fallback: a mid that fell below our post is exactly
        #     when we must NOT pay up to cross).
        if adverse:
            try:
                kalshi_client.cancel_order(oid)
            except Exception:
                pass
            try:
                state.release_ticker(ticker)
            except Exception:
                pass
            try:
                state.log_decision({"kind": "low_post_cancel", "ticker": ticker,
                    "side": side, "post_c": row.get("post_c"), "reason": "adverse",
                    "cur_mid_c": cur_mid, "rest_s": round(age_s, 1),
                    "order_id": oid, "ts": time.time()})
            except Exception:
                pass
            _discord(f"\U0001F6D1 **LOW-POST ADVERSE-CANCEL** `{ticker}` "
                     f"{side.upper()} post {row.get('post_c')}c vs mid {cur_mid}c "
                     f"(rested {age_s:.0f}s)")
            log.info("LOW-POST ADVERSE-CANCEL %s: post %sc vs mid %sc (%.0fs)",
                     ticker, row.get("post_c"), cur_mid, age_s)
            done_ids.add(oid)
            continue
        # 3b) TAKER-FALLBACK (2026-06-03): unfilled at/after the deadline (~window-
        #     close) -> double-buy-safe maker->taker cross if the edge still clears.
        #     Replaces the TTL-expire-and-abandon near window-close so "the maker
        #     never fills" no longer means "no position". Inert unless enabled.
        _fb_deadline = row.get("fallback_deadline_ts")
        if (getattr(config, "TAKER_FALLBACK_ENABLED", False) and _fb_deadline
                and time.time() >= float(_fb_deadline)):
            if _taker_fallback(rt, row, held):
                done_ids.add(oid)
            # else: cancel could not be confirmed -> leave resting, retry next sweep.
            continue
        # 3c) TTL-expired BEFORE the deadline -> drop so the eval re-posts fresh at
        #     the live mid (keep making while we still have window left).
        if expired:
            try:
                kalshi_client.cancel_order(oid)
            except Exception:
                pass
            try:
                state.release_ticker(ticker)
            except Exception:
                pass
            try:
                state.log_decision({"kind": "low_post_cancel", "ticker": ticker,
                    "side": side, "post_c": row.get("post_c"), "reason": "ttl_expired",
                    "cur_mid_c": cur_mid, "rest_s": round(age_s, 1),
                    "order_id": oid, "ts": time.time()})
            except Exception:
                pass
            done_ids.add(oid)
            continue
        # 4) Stale: climate day passed -> market closed. Cancel + release.
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
