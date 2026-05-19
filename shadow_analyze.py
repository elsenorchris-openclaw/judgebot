#!/usr/bin/env python3
"""End-of-day shadow trade analyzer.

Reads /home/ubuntu/paper_judge_bot/data/shadow_trades.jsonl, fetches each
ticker's settlement (or current state) from Kalshi, and computes hypothetical
P&L: did the bot's conviction beat the market?

Usage: python3 shadow_analyze.py [--date YYYY-MM-DD]
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

SHADOW_PATH = Path("/home/ubuntu/paper_judge_bot/data/shadow_trades.jsonl")
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

# Auth: use bot's Kalshi keys if available (read-only endpoints can work unauth-d)
def _auth_headers():
    try:
        sys.path.insert(0, "/home/ubuntu/paper_judge_bot")
        import config; config.apply_env()
        import kalshi_client
        c = kalshi_client.KalshiClient(config.KALSHI_KEY_ID_V2, config.KALSHI_PEM_PATH)
        # Use the bot's signer
        return None  # Markets endpoint is public, fall back to unauth
    except Exception:
        return None


def fetch_market(ticker: str) -> dict | None:
    try:
        r = httpx.get(f"{KALSHI_API}/markets/{ticker}", timeout=10.0)
        if r.status_code != 200:
            return None
        return r.json().get("market", {})
    except Exception as e:
        print(f"  ! fetch {ticker} failed: {e}", file=sys.stderr)
        return None


def evaluate(record: dict) -> dict:
    """Score one shadow trade.
    Returns dict with: settled (bool), result (WIN/LOSS/PENDING),
                       hypothetical_pnl_per_contract_c, etc."""
    ticker = record["ticker"]
    side = record["would_action"]
    price_c = record["market_price_c"]
    m = fetch_market(ticker) or {}
    status = m.get("status", "?")
    settlement_value = m.get("settlement_value")
    result_text = m.get("result")  # "yes" / "no" / null
    out = {
        "ticker": ticker,
        "side": side,
        "entry_c": price_c,
        "status": status,
        "settled_result": result_text,
        "would_prob": record["would_prob"],
        "gap_pp": record["gap_pp"],
        "reason": record.get("reason"),
        "conviction": record.get("conviction"),
    }
    if status != "finalized" or result_text is None:
        out["outcome"] = "PENDING"
        out["pnl_c"] = None
        return out
    # Settled: did our side win?
    won = (side == "BUY_YES" and result_text == "yes") or \
          (side == "BUY_NO" and result_text == "no")
    out["outcome"] = "WIN" if won else "LOSS"
    out["pnl_c"] = (100 - price_c) if won else -price_c
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD filter (default today)")
    args = ap.parse_args()

    target_date = args.date or datetime.now(timezone.utc).date().isoformat()
    print(f"Analyzing shadow trades for climate_day={target_date}")
    print(f"Reading: {SHADOW_PATH}")
    print()

    if not SHADOW_PATH.exists():
        print(f"No shadow log at {SHADOW_PATH}")
        return

    records = []
    with open(SHADOW_PATH) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("climate_day") == target_date:
                records.append(r)
    if not records:
        print(f"No shadow trades for {target_date}")
        return

    print(f"Found {len(records)} shadow trades for {target_date}")
    print()

    # De-dupe by ticker — keep the LATEST shadow per ticker
    by_ticker = {}
    for r in records:
        tk = r["ticker"]
        if tk not in by_ticker or r["ts"] > by_ticker[tk]["ts"]:
            by_ticker[tk] = r
    print(f"Unique tickers: {len(by_ticker)}")
    print()

    # Evaluate each
    results = []
    for tk, r in sorted(by_ticker.items()):
        res = evaluate(r)
        results.append(res)
        outc = res["outcome"]
        pnl = res["pnl_c"]
        pnl_str = f"{pnl:+d}c" if pnl is not None else "(pending)"
        print(f"  {tk:32s} {res['side']:8s} @{res['entry_c']:3d}c  "
              f"gap={res['gap_pp']:+5.1f}pp conv={res['conviction']:.2f}  "
              f"reason={res['reason']:<25s} {outc:8s} {pnl_str}")

    # Summary
    settled = [r for r in results if r["outcome"] != "PENDING"]
    pending = [r for r in results if r["outcome"] == "PENDING"]
    wins = [r for r in settled if r["outcome"] == "WIN"]
    losses = [r for r in settled if r["outcome"] == "LOSS"]
    total_pnl_c = sum(r["pnl_c"] for r in settled)
    print()
    print(f"== SUMMARY ==")
    print(f"  Total shadow trades: {len(results)}")
    print(f"  Pending (not settled): {len(pending)}")
    print(f"  Settled: {len(settled)}  WIN={len(wins)}  LOSS={len(losses)}")
    if settled:
        wr = 100 * len(wins) / len(settled)
        avg_pnl_c = total_pnl_c / len(settled)
        print(f"  Win rate: {wr:.1f}%")
        print(f"  Hypothetical pnl per contract: {avg_pnl_c:+.1f}c")
        print(f"  Total hypothetical pnl: {total_pnl_c:+d}c = ${total_pnl_c/100:+.2f} per shadow contract")

    # Per-reason breakdown
    by_reason = defaultdict(list)
    for r in settled:
        by_reason[r["reason"]].append(r)
    print()
    print(f"== BY REASON ==")
    for reason, rs in sorted(by_reason.items()):
        ws = [r for r in rs if r["outcome"] == "WIN"]
        ls = [r for r in rs if r["outcome"] == "LOSS"]
        pnl = sum(r["pnl_c"] for r in rs)
        wr = 100*len(ws)/len(rs) if rs else 0
        print(f"  {reason:30s} n={len(rs)}  W={len(ws)} L={len(ls)}  WR={wr:.0f}%  pnl={pnl:+d}c")


if __name__ == "__main__":
    main()
