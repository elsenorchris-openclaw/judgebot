"""shadow_compare.py — side-by-side PnL comparison of LLM vs pure-nn shadow.

Usage:
    python3 shadow_compare.py [--date YYYY-MM-DD] [--days 1]

For the chosen window:
  - LLM realized PnL: pulled from trades.jsonl positions + Kalshi settle
  - Pure-nn hypothetical PnL: from shadow_nn_strategy_settled.jsonl
    (run shadow_settle.py first to backfill outcomes)

Outputs:
  - Volume / WR / total PnL for each strategy
  - Decision agreement matrix on overlapping (ticker, ts) pairs
  - Top winners / losers each side
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/paper_judge_bot")

import config
config.apply_env()
import kalshi_client

DATA = Path("/home/ubuntu/paper_judge_bot/data")


def _load_settled_shadow(target_dates: set[str]) -> list[dict]:
    """Already-backfilled shadow records (need shadow_settle.py run first)."""
    p = DATA / "shadow_nn_strategy_settled.jsonl"
    if not p.exists():
        return []
    out = []
    for ln in p.open():
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("climate_day") in target_dates:
            out.append(r)
    return out


def _load_llm_trades(target_dates: set[str]) -> list[dict]:
    """Bot's actual trades: opens + cost + price. Realized outcome comes from
    Kalshi settlement (lookup per ticker)."""
    p = DATA / "trades.jsonl"
    if not p.exists():
        return []
    trades = []
    for ln in p.open():
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("kind") != "entry":
            continue
        if r.get("date_str") in target_dates:
            trades.append(r)
    return trades


def _settle_trade(t: dict, kalshi_cache: dict) -> dict:
    """Look up real Kalshi settlement and compute realized PnL for a trade."""
    ticker = t["market_ticker"]
    if ticker not in kalshi_cache:
        try:
            m = kalshi_client.get_market(ticker)
        except Exception as e:
            kalshi_cache[ticker] = {"settled": False, "err": str(e)}
            return {"settled": False}
        if not m:
            kalshi_cache[ticker] = {"settled": False, "err": "no_market"}
            return {"settled": False}
        result = (m.get("result") or "").lower()
        if result not in ("yes", "no"):
            kalshi_cache[ticker] = {"settled": False, "err": f"status={m.get('status')} result={result}"}
            return {"settled": False}
        kalshi_cache[ticker] = {
            "settled": True, "yes_won": (result == "yes"), "result_str": result,
        }
    info = kalshi_cache[ticker]
    if not info.get("settled"):
        return {"settled": False}
    yes_won = info["yes_won"]
    action = t.get("action")
    qty = t.get("count", 0)
    cost = t.get("cost", 0.0)
    revenue = qty if (yes_won and action == "BUY_YES") or (not yes_won and action == "BUY_NO") else 0
    pnl = revenue - cost
    return {"settled": True, "yes_won": yes_won, "pnl": pnl, "cost": cost, "revenue": revenue}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="single climate day (YYYY-MM-DD). default: yesterday UTC")
    ap.add_argument("--days", type=int, default=1, help="window size ending at --date")
    args = ap.parse_args()

    if args.date:
        end = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        from datetime import timedelta
        end = datetime.now(timezone.utc) - timedelta(days=1)
        end = end.replace(hour=0, minute=0, second=0, microsecond=0)

    from datetime import timedelta
    target_dates = set()
    for i in range(args.days):
        d = end - timedelta(days=i)
        target_dates.add(d.strftime("%Y-%m-%d"))

    print(f"=== shadow_compare for climate days: {sorted(target_dates)} ===")
    print()

    # ── LLM side ──
    llm_trades = _load_llm_trades(target_dates)
    print(f"LLM trades opened in window: {len(llm_trades)}")
    kalshi_cache: dict = {}
    llm_settled = []
    llm_unsettled = 0
    for t in llm_trades:
        s = _settle_trade(t, kalshi_cache)
        if s["settled"]:
            llm_settled.append({**t, **s})
        else:
            llm_unsettled += 1
    print(f"  settled: {len(llm_settled)}   unsettled: {llm_unsettled}")
    if llm_settled:
        total_pnl = sum(r["pnl"] for r in llm_settled)
        wins = sum(1 for r in llm_settled if r["pnl"] > 0)
        total_cost = sum(r["cost"] for r in llm_settled)
        wr = 100.0 * wins / len(llm_settled)
        roi = 100.0 * total_pnl / max(0.01, total_cost)
        print(f"  LLM realized PnL: ${total_pnl:+.2f}  wins={wins}/{len(llm_settled)} ({wr:.1f}%)  cost=${total_cost:.2f}  ROI={roi:+.1f}%")
    print()

    # ── Pure-nn side ──
    shadow = _load_settled_shadow(target_dates)
    print(f"Pure-nn shadow records (settled) in window: {len(shadow)}")
    if not shadow:
        print("  (none — did you run shadow_settle.py yet?)")
    else:
        buys = [r for r in shadow if r.get("decision") in ("BUY_YES", "BUY_NO")]
        skips = [r for r in shadow if r.get("decision") == "SKIP"]
        print(f"  decisions: BUY_YES={sum(1 for r in buys if r['decision']=='BUY_YES')}  "
              f"BUY_NO={sum(1 for r in buys if r['decision']=='BUY_NO')}  SKIP={len(skips)}")
        if buys:
            total_pnl = sum(r.get("pnl_hypothetical_usd", 0.0) for r in buys)
            wins = sum(1 for r in buys if r.get("pnl_hypothetical_usd", 0.0) > 0)
            total_cost = sum((r.get("qty", 0) * (r.get("price_c", 0) / 100.0)) for r in buys)
            wr = 100.0 * wins / len(buys)
            roi = 100.0 * total_pnl / max(0.01, total_cost)
            print(f"  Pure-nn hypothetical PnL: ${total_pnl:+.2f}  wins={wins}/{len(buys)} ({wr:.1f}%)  "
                  f"cost=${total_cost:.2f}  ROI={roi:+.1f}%")
    print()

    # ── Agreement on overlapping (ticker, climate_day) pairs ──
    if llm_settled and shadow:
        llm_by_tk = {(t["market_ticker"], t.get("date_str")): t for t in llm_settled}
        shadow_by_tk: dict = {}
        for r in shadow:
            shadow_by_tk.setdefault((r.get("ticker"), r.get("climate_day")), []).append(r)
        agree = collections.Counter()
        for key, t in llm_by_tk.items():
            sh = shadow_by_tk.get(key) or []
            if not sh:
                agree["llm_only_no_shadow_record"] += 1
                continue
            # Use the closest-in-time shadow record (last by ts before LLM trade ts)
            t_ts = t.get("ts")
            if isinstance(t_ts, str):
                try:
                    t_ts = datetime.fromisoformat(t_ts.replace("Z", "+00:00")).timestamp()
                except Exception:
                    t_ts = None
            best = None
            for r in sh:
                rt = r.get("ts", 0)
                if t_ts is None or rt <= t_ts:
                    if best is None or rt > best.get("ts", 0):
                        best = r
            best = best or sh[0]
            llm_side = t["action"]
            sh_dec = best["decision"]
            agree[f"LLM_{llm_side}_vs_nn_{sh_dec}"] += 1
        print("Agreement (LLM trade vs shadow at same ticker/day):")
        for k, v in sorted(agree.items(), key=lambda x: -x[1]):
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
