"""shadow_settle.py — backfill realized outcomes + hypothetical PnL into the
nn-shadow log.

Run manually (or via cron) after climate days close. Reads
`data/shadow_nn_strategy.jsonl`, for any record whose climate_day is past
and whose `settled` field is missing, queries Kalshi for the market's
settlement value and computes hypothetical PnL on the shadow decision.

Writes a separate file `data/shadow_nn_strategy_settled.jsonl` (append-only)
with the enriched records — the source log is never modified.

PnL convention (per shadow trade):
  qty × $1 / contract, no spread cost, no fees.
  - BUY_YES at price_c: YES wins → +qty × (1 - price). NO wins → -qty × price.
  - BUY_NO  at price_c: NO  wins → +qty × (1 - price). YES wins → -qty × price.
  SKIP records carry pnl=0 but settle the outcome so we can later count
  hypothetical "would-have-won" trades among LLM-bought / pure-nn-skipped
  divergences.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/home/ubuntu/paper_judge_bot")

import config

config.apply_env()
import kalshi_client

SHADOW_LOG = Path("/home/ubuntu/paper_judge_bot/data/shadow_nn_strategy.jsonl")
SETTLED_LOG = Path("/home/ubuntu/paper_judge_bot/data/shadow_nn_strategy_settled.jsonl")
CHECKPOINT = Path("/home/ubuntu/paper_judge_bot/data/shadow_settle_checkpoint.json")


# A climate day is "past" if now is ≥ 30h after its 04:00 UTC start —
# ensures CLI report has been published and the Kalshi market settled.
def _past(rec: dict) -> bool:
    cd = rec.get("climate_day")
    if not cd:
        return False
    try:
        d = datetime.strptime(cd, "%Y-%m-%d").replace(
            hour=4, minute=0, tzinfo=timezone.utc
        )
    except ValueError:
        return False
    # +30h buffer to ensure CLI report is published
    cutoff_ts = d.timestamp() + 30 * 3600
    return time.time() >= cutoff_ts


def _settle_via_kalshi(ticker: str) -> dict:
    """Returns dict with: settled (bool), yes_won (bool), result_str.
    Returns settled=False if market not yet settled or lookup fails."""
    try:
        m = kalshi_client.get_market(ticker)
    except Exception as e:
        return {"settled": False, "yes_won": None, "result_str": f"fetch_error: {e}"}
    if not m or not isinstance(m, dict):
        return {"settled": False, "yes_won": None, "result_str": "no_market"}
    status = m.get("status") or m.get("market_status") or ""
    result = m.get("result") or ""
    if status.lower() not in ("settled", "finalized", "closed") and not result:
        return {"settled": False, "yes_won": None, "result_str": f"status={status} result={result}"}
    yes_won = None
    if result:
        r = result.lower()
        if r in ("yes", "y"):
            yes_won = True
        elif r in ("no", "n"):
            yes_won = False
    if yes_won is None:
        return {"settled": False, "yes_won": None, "result_str": f"unclear: status={status} result={result}"}
    return {"settled": True, "yes_won": yes_won, "result_str": result}


def _compute_pnl(rec: dict, yes_won: bool) -> float:
    decision = rec.get("decision")
    qty = rec.get("qty")
    price_c = rec.get("price_c")
    if decision not in ("BUY_YES", "BUY_NO") or qty is None or price_c is None:
        return 0.0
    price = price_c / 100.0
    won = (yes_won and decision == "BUY_YES") or (not yes_won and decision == "BUY_NO")
    if won:
        return qty * (1.0 - price)
    return -qty * price


def main():
    if not SHADOW_LOG.exists():
        print(f"no shadow log at {SHADOW_LOG}, nothing to do")
        return 0

    # Track which (ticker, ts) combos we've already settled
    already_settled = set()
    if SETTLED_LOG.exists():
        for ln in SETTLED_LOG.open():
            try:
                r = json.loads(ln)
                already_settled.add((r.get("ticker"), r.get("ts")))
            except Exception:
                pass

    n_total = 0
    n_skipped_already = 0
    n_skipped_not_past = 0
    n_skipped_no_kalshi_result = 0
    n_settled = 0
    pnl_sum = 0.0
    by_decision_pnl = {"BUY_YES": 0.0, "BUY_NO": 0.0, "SKIP": 0.0}
    by_decision_n = {"BUY_YES": 0, "BUY_NO": 0, "SKIP": 0}

    # Cache market lookups to avoid hammering the API
    ticker_cache: dict[str, dict] = {}

    with SETTLED_LOG.open("a") as out_f:
        for ln in SHADOW_LOG.open():
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            n_total += 1
            ticker = rec.get("ticker")
            key = (ticker, rec.get("ts"))
            if key in already_settled:
                n_skipped_already += 1
                continue
            if not _past(rec):
                n_skipped_not_past += 1
                continue
            if ticker not in ticker_cache:
                ticker_cache[ticker] = _settle_via_kalshi(ticker)
            settle = ticker_cache[ticker]
            if not settle["settled"]:
                n_skipped_no_kalshi_result += 1
                continue
            yes_won = settle["yes_won"]
            pnl = _compute_pnl(rec, yes_won)
            enriched = dict(rec)
            enriched["settled"] = True
            enriched["yes_won"] = yes_won
            enriched["pnl_hypothetical_usd"] = round(pnl, 4)
            enriched["settle_result"] = settle["result_str"]
            out_f.write(json.dumps(enriched, default=str) + "\n")
            n_settled += 1
            decision = rec.get("decision") or "SKIP"
            pnl_sum += pnl
            by_decision_pnl[decision] = by_decision_pnl.get(decision, 0.0) + pnl
            by_decision_n[decision] = by_decision_n.get(decision, 0) + 1
            already_settled.add(key)

    print(f"shadow_settle: total_records={n_total}  newly_settled={n_settled}")
    print(f"  skipped (already settled prior run): {n_skipped_already}")
    print(f"  skipped (climate day not yet past):  {n_skipped_not_past}")
    print(f"  skipped (kalshi has no result yet):  {n_skipped_no_kalshi_result}")
    if n_settled > 0:
        print(f"  total hypothetical PnL: ${pnl_sum:.2f}")
        for d in ("BUY_YES", "BUY_NO", "SKIP"):
            n = by_decision_n.get(d, 0)
            p = by_decision_pnl.get(d, 0.0)
            print(f"  {d:>8}: n={n:>4}  PnL=${p:>8.2f}  avg=${p/max(1,n):.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
