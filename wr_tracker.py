#!/usr/bin/env python3.12
"""Per-side settled WR tracker for the blend bot. One-glance daily + cumulative
WR/P&L split by HIGH/LOW x NO/YES, vs backtest baselines. Read-only (Kalshi settlement
truth). Usage: python3.12 wr_tracker.py [YYYY-MM-DD start, default 2026-06-03]"""
import sys, os, json, collections
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) or ".")
os.chdir(os.path.dirname(os.path.abspath(__file__)) or ".")
import kalshi_client

START = sys.argv[1] if len(sys.argv) > 1 else "2026-06-03"
# backtest peak-4 baselines (from lead_trade.py reconstruction)
BASE = {"HIGH-NO": 77, "HIGH-YES": 60, "LOW-NO": 71, "ALL": 73}

res = {}
for s in kalshi_client.list_settlements(limit=1000):
    t = s.get("ticker") or s.get("market_ticker"); r = s.get("market_result")
    if t and r: res[t] = r

trades = []
for l in open("data/trades.jsonl"):
    try: r = json.loads(l)
    except: continue
    if r.get("kind") != "entry" or r.get("date_str", "") < START: continue
    if "blend" not in str(r.get("mu_method", "")): continue
    tk = r["market_ticker"]
    r["_series"] = "HIGH" if tk.startswith("KXHIGH") else ("LOW" if tk.startswith("KXLOW") else "?")
    r["_cell"] = f'{r["_series"]}-{r["action"][4:]}'  # HIGH-NO etc
    trades.append(r)

def outcome(r):
    rr = res.get(r["market_ticker"])
    if rr is None: return None, None
    win = (rr == "yes" and r["action"] == "BUY_YES") or (rr == "no" and r["action"] == "BUY_NO")
    return win, ((1 - r["entry_price"]) if win else -r["entry_price"]) * r["count"]

CELLS = ["HIGH-NO", "HIGH-YES", "LOW-NO", "LOW-YES"]
def line(rows, label, baseline_key=None):
    agg = collections.defaultdict(lambda: [0, 0, 0.0])  # cell -> n,w,pnl
    tot = [0, 0, 0.0]; unsettled = 0
    for r in rows:
        w, p = outcome(r)
        if p is None: unsettled += 1; continue
        a = agg[r["_cell"]]; a[0]+=1; a[1]+=1 if w else 0; a[2]+=p
        tot[0]+=1; tot[1]+=1 if w else 0; tot[2]+=p
    cells_s = []
    for c in CELLS:
        n, w, p = agg[c]
        if n == 0: cells_s.append(f'{c:8}    -        '); continue
        flag = ""
        if c in BASE and n >= 5:
            d = w/n*100 - BASE[c]
            flag = " ⚠" if d <= -15 else (" ✓" if d >= -5 else "")
        cells_s.append(f'{c:8} n={n:<3d} {w/n*100:3.0f}% ${p:+6.1f}{flag}')
    twr = f'{tot[1]/tot[0]*100:.0f}%' if tot[0] else "n/a"
    print(f'{label:11} | ' + " | ".join(cells_s) + f'  ||  TOT n={tot[0]} WR={twr} ${tot[2]:+.1f}' + (f' (uns {unsettled})' if unsettled else ''))

print(f"=== BLEND SETTLED WR TRACKER  (backtest peak-4: HIGH-NO 77% / HIGH-YES 60% / LOW-NO 71% / all 73%) ===")
days = sorted(set(r["date_str"] for r in trades))
for d in days:
    line([r for r in trades if r["date_str"] == d], d)
print("-"*150)
line(trades, "CUM")
# 1-best bracket/station (HIGH) — the backtest's selection policy
hi = [r for r in trades if r["_series"]=="HIGH" and outcome(r)[1] is not None]
best = collections.defaultdict(list)
for r in hi: best[(r["station"], r["date_str"])].append(r)
one = [max(v, key=lambda r:(r.get("gap_pp") or 0)) for v in best.values()]
W=sum(1 for r in one if outcome(r)[0]); P=sum(outcome(r)[1] for r in one)
print(f'\n1-best-bracket/stn (HIGH, backtest policy): n={len(one)} WR={W/max(1,len(one))*100:.0f}% ${P:+.1f}   (live took {len(hi)/max(1,len(best)):.2f} brackets/stn)')
# flags
yn = [r for r in hi if r["_cell"]=="HIGH-YES"]; ynw = sum(1 for r in yn if outcome(r)[0])
if yn and ynw/len(yn)*100 < 50:
    print(f'⚠ HIGH-YES at {ynw/len(yn)*100:.0f}% WR (n={len(yn)}) — backtest 60%. Watch the 6/7 YES-6pp-bar effect.')
print("\nLegend: ✓ at/above backtest, ⚠ ≥15pp below. Run: python3.12 wr_tracker.py [start-date]")
