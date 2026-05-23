#!/usr/bin/env python3
"""Replay the CURRENT live HIGH windows on a past day's LIVE-RECORDER data (the
judge's shadow_nn_strategy.jsonl), to see how today's shipped windows WOULD have
traded that day. Faithful: buy at window-open (deepest in-window h_to_peak),
gated NO (edge>=PUSH_MIN_EDGE_PP, NO-cost in [10,80], spread<=PUSH_MAX_SPREAD_C_HIGH),
one trade per (station, climate_day), settled vs market_meta. Usage: replay_windows.py [YYYY-MM-DD]"""
import json, sys, sqlite3, collections
sys.path.insert(0, "/home/ubuntu/paper_judge_bot")
import config as c

DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-05-22"
SHADOW = "/home/ubuntu/paper_judge_bot/data/shadow_nn_strategy.jsonl"
DB = "/home/ubuntu/data/market_history_backfill.sqlite"
WINS = c.PUSH_HIGH_TEMP_WINDOW_BY_STATION
GLOBAL = c.PUSH_HIGH_TEMP_WINDOW
EDGE = c.PUSH_MIN_EDGE_PP / 100.0
MAXSPREAD = float(getattr(c, "PUSH_MAX_SPREAD_C_HIGH", 0) or 0)
GLO, GHI = 10, 80

# settled results — pulled LIVE from Kalshi (DB may not have yesterday yet if the
# gap-fill ran before settlement). Self-contained.
import datetime
sys.path.insert(0, "/home/ubuntu/paper_judge_bot/tools")
from backfill_historical_candles import SERIES_MAP, _get
DAY_SHORT = datetime.datetime.strptime(DAY, "%Y-%m-%d").strftime("%y%b%d").upper()
res = {}
for series, (st, side) in SERIES_MAP.items():
    if side != "HIGH":
        continue
    try:
        mks = _get("/trade-api/v2/markets?series_ticker=%s&status=settled&limit=1000" % series).get("markets", [])
    except Exception:
        continue
    for m in mks:
        tk = m.get("ticker", "")
        if DAY_SHORT not in tk:
            continue
        res[(st, "-".join(tk.split("-")[2:]))] = m.get("result")

# load recorder entries for the day
bystation = collections.defaultdict(list)
with open(SHADOW) as f:
    for ln in f:
        if DAY not in ln or "KXHIGH" not in ln:
            continue
        try: d = json.loads(ln)
        except Exception: continue
        if d.get("climate_day") != DAY or "KXHIGH" not in (d.get("ticker") or ""):
            continue
        sig = d.get("signals") or {}; m = d.get("market") or {}
        h = sig.get("h_to_peak"); py = d.get("p_yes")
        ya = m.get("yes_ask_c"); na = m.get("no_ask_c"); sp = m.get("spread_c")
        if h is None or py is None or ya is None or na is None:
            continue
        tk = d["ticker"]; brk = "-".join(tk.split("-")[2:])
        bystation[d["station"]].append((h, ya, na, sp, py, brk))

print("REPLAY current HIGH windows on %s (live-recorder data). edge>=%.0fpp, NO-cost[%d,%d], spread<=%dc\n"
      % (DAY, EDGE*100, GLO, GHI, MAXSPREAD))
print("%-5s %-12s %-26s %s" % ("stn", "window", "trade@open", "result"))
tot = collections.Counter(); pnl_tot = 0.0; n = 0
for st in sorted(bystation):
    before, after = WINS.get(st, GLOBAL)
    inwin = [e for e in bystation[st] if -after <= e[0] <= before]
    # buy at OPEN: deepest h; among same-ish depth pick best edge
    cand = None
    for h, ya, na, sp, py, brk in sorted(inwin, key=lambda e: (-e[0], -(e[1]/100.0 - e[4]))):
        if py >= ya / 100.0: continue            # must be a NO
        edge = ya / 100.0 - py
        if edge < EDGE: continue
        if not (GLO <= na <= GHI): continue       # NO-cost gate
        if MAXSPREAD > 0 and sp is not None and sp > MAXSPREAD: continue
        cand = (h, na, edge, brk); break
    if cand is None:
        print("%-5s [pk%+.1f,pk%+.1f]  -- no qualifying NO in window --" % (st, -before, after))
        continue
    h, cost, edge, brk = cand
    r = res.get((st, brk), "?")
    won = (r == "no")
    p = (100 - cost) if won else (-cost if r in ("yes", "no") else 0)
    if r in ("yes", "no"):
        pnl_tot += p; n += 1; tot["W" if won else "L"] += 1
    print("%-5s [pk%+.1f,pk%+.1f]  NO %s @%dc h2pk=%.1f e=%.0fpp  %s %s" % (
        st, -before, after, brk, cost, h, edge*100, r.upper(),
        ("%+dc" % p) if r in ("yes", "no") else "(unsettled)"))
print("\n=== TOTAL: %d trades, %dW/%dL, PnL %+.0fc (%+.2f$ at 1 contract), avg %+.1fc/bet ==="
      % (n, tot["W"], tot["L"], pnl_tot, pnl_tot/100.0, pnl_tot/n if n else 0))
