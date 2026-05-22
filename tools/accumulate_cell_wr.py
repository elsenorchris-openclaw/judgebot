#!/usr/bin/env python3
"""Daily FAITHFUL per-cell WR accumulator for paper_judge_bot.

Reads the bot's actually-executed trades (trades.jsonl 'entry' records), joins
their h_to_peak (shadow_nn_strategy.jsonl) and Kalshi settlement, and APPENDS
each newly-settled trade to a persistent log. This is the gold-standard faithful
per-cell win rate -- the bot's real fills + real settlement, NO mu-replay and no
price-gate modelling, so it has none of the backtest caveats. Over ~2-3 weeks it
becomes the authoritative per-(station,side,offset) WR. Read-only wrt the bot.

Run daily via systemd timer (after overnight settlement). Idempotent: a ticker is
recorded once; unsettled/void tickers are retried on the next run.
"""
import json, urllib.request, time, os, datetime
from collections import defaultdict

LEDGER = "/home/ubuntu/paper_judge_bot/data/trades.jsonl"
SHADOW = "/home/ubuntu/paper_judge_bot/data/shadow_nn_strategy.jsonl"
OUT = "/home/ubuntu/data/faithful_cell_wr.jsonl"


def main():
    done = set()
    if os.path.exists(OUT):
        with open(OUT) as f:
            for line in f:
                try: done.add(json.loads(line)["ticker"])
                except Exception: continue

    # h_to_peak per ticker (nearest shadow eval to the entry ts)
    sh = defaultdict(list)
    if os.path.exists(SHADOW):
        with open(SHADOW) as f:
            for line in f:
                try: d = json.loads(line)
                except Exception: continue
                tk = d.get("ticker", "")
                if "KXHIGH" not in tk and "KXLOW" not in tk: continue
                h = (d.get("signals") or {}).get("h_to_peak")
                if h is not None: sh[tk].append((d.get("ts", 0), h))
    for t in sh: sh[t].sort()

    def h_at(tk, ts):
        s = sh.get(tk)
        return min(s, key=lambda x: abs(x[0] - (ts or 0)))[1] if s else None

    # executed entries not yet recorded, only climate days old enough to be settled
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=18)).strftime("%Y-%m-%d")
    todo = []
    with open(LEDGER) as f:
        for line in f:
            try: d = json.loads(line)
            except Exception: continue
            if d.get("kind") != "entry": continue
            tk = d.get("market_ticker", "")
            if tk in done or ("KXHIGH" not in tk and "KXLOW" not in tk): continue
            cd = d.get("date_str", "")
            if not cd or cd > cutoff: continue
            todo.append(d)

    added = 0
    with open(OUT, "a") as out:
        for d in todo:
            tk = d["market_ticker"]
            try:
                with urllib.request.urlopen("https://api.elections.kalshi.com/trade-api/v2/markets/%s" % tk, timeout=10) as r:
                    res = json.load(r).get("market", {}).get("result")
            except Exception:
                res = None
            time.sleep(0.2)
            if res not in ("yes", "no"):
                continue  # not settled / void -> retry next run
            side = d.get("action")
            won = (res == "no") if side == "BUY_NO" else (res == "yes")
            rec = {"ticker": tk, "station": d.get("station"), "series": d.get("series"),
                   "side": side, "climate_day": d.get("date_str"),
                   "h_to_peak": h_at(tk, d.get("ts")), "floor": d.get("floor"),
                   "cap": d.get("cap"), "result": res, "won": won,
                   "cost": d.get("cost"), "recorded_ts": time.time()}
            out.write(json.dumps(rec) + "\n")
            done.add(tk)
            added += 1
    print("accumulator: +%d settled trades this run, %d total recorded" % (added, len(done)))

    # running report: faithful WR by (series, h2pk band)
    rows = []
    if os.path.exists(OUT):
        with open(OUT) as f:
            for line in f:
                try: rows.append(json.loads(line))
                except Exception: continue

    def band(h):
        if h is None: return "?"
        return ">=1.0" if h >= 1.0 else ("0.5-1.0" if h >= 0.5 else "<0.5")
    agg = defaultdict(lambda: {"w": 0, "l": 0})
    cell = defaultdict(lambda: {"w": 0, "l": 0})
    for r in rows:
        ser = "HIGH" if "KXHIGH" in r["ticker"] else "LOW"
        agg[(ser, band(r.get("h_to_peak")))]["w" if r["won"] else "l"] += 1
        cell[(r.get("station"), ser)]["w" if r["won"] else "l"] += 1
    print("\nfaithful WR by series x h2pk band:")
    for k in sorted(agg):
        a = agg[k]; n = a["w"] + a["l"]
        print("  %-5s %-8s : %dW/%dL  WR %s" % (k[0], k[1], a["w"], a["l"], ("%.0f%%" % (100*a["w"]/n)) if n else "-"))
    print("\nfaithful WR by station (n>=4):")
    for k in sorted(cell):
        a = cell[k]; n = a["w"] + a["l"]
        if n >= 4:
            print("  %-6s %-5s : %dW/%dL  WR %.0f%%" % (k[0], k[1], a["w"], a["l"], 100*a["w"]/n))


if __name__ == "__main__":
    main()
