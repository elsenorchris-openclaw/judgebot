#!/usr/bin/env python3
"""Split WR/PnL by BET SIDE (BUY_NO vs BUY_YES) and offset. The matcher's edge
bets are mostly low-WR BUY_YES (longshots on the projected bracket); BUY_NO is
high-WR but maybe no-edge. This decides whether a HIGH-WR window can also be +EV.
Pooled across stations + per-station best BUY_NO window."""
import csv, gzip, math, sqlite3, datetime
from collections import defaultdict

DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ = "/home/ubuntu/data/phq_ext"
MIN_DAY = "2026-03-15"; EDGE = 0.12
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5, "AUS": -6, "DFW": -6,
      "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6, "DEN": -7,
      "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8}
BK = [(2.0, 3.5, ">=2.0"), (1.0, 2.0, "1-2"), (0.0, 1.0, "0-1"), (-1.5, 0.0, "<0")]


def phi(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def p_yes(fl, cp, mu, sig):
    if not sig or sig <= 0: sig = 1.5
    if fl is not None and cp is not None: return phi((cp + .5 - mu) / sig) - phi((fl - .5 - mu) / sig)
    if fl is not None: return 1.0 - phi((fl - .5 - mu) / sig)
    if cp is not None: return phi((cp + .5 - mu) / sig)
    return None


conn = sqlite3.connect(DB, timeout=60); conn.execute("PRAGMA busy_timeout=60000")
# pooled[bucket][side] = {w,l,pnl}; also per-station NO
pooled = defaultdict(lambda: defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0}))
pstat_no = defaultdict(lambda: defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0}))
for iata, off in sorted(ST.items()):
    if iata == "DCA": continue
    phq = defaultdict(list)
    try:
        with gzip.open("%s/phq_raw_%s.csv.gz" % (PHQ, iata), "rt") as f:
            for r in csv.DictReader(f):
                if r["side"] != "high" or not r.get("mu_proj_f") or r["date"] < MIN_DAY: continue
                try: phq[r["date"]].append((float(r["cur_lst_min"]), float(r["offset"]), float(r["mu_proj_f"]), float(r["sigma_proj_f"] or 1.5)))
                except (ValueError, TypeError): continue
    except FileNotFoundError: continue
    for d in phq: phq[d].sort(key=lambda x: -x[1])
    brs = defaultdict(list)
    for date, br, fl, cp, res in conn.execute("SELECT climate_day,bracket,floor,cap,result FROM market_meta WHERE station=? AND side='HIGH' AND climate_day>=?", ("K" + iata, MIN_DAY)):
        if res in ("yes", "no"): brs[date].append((br, fl, cp, res))
    cand = defaultdict(list)
    for date, br, ts, yb, ya in conn.execute("SELECT climate_day,bracket,ts,yes_bid,yes_ask FROM candle_history WHERE station=? AND side='HIGH' AND climate_day>=?", ("K" + iata, MIN_DAY)):
        lst = datetime.datetime.utcfromtimestamp(ts + off * 3600)
        if lst.strftime("%Y-%m-%d") != date: continue
        cand[(date, br)].append((lst.hour * 60 + lst.minute, yb, ya))
    for k in cand: cand[k].sort()
    def price_at(date, br, t):
        seq = cand.get((date, br))
        if not seq: return None
        b = None
        for r in seq:
            if r[0] <= t and (b is None or r[0] > b[0]): b = r
        return b if (b and t - b[0] <= 60 and b[1] is not None and b[2] is not None and 0 < b[2] < 100) else None
    for date, rows in phq.items():
        if date not in brs: continue
        for lo, hi, lab in BK:
            done = {"BUY_NO": False, "BUY_YES": False}
            for lst_min, offset, mu, sig in rows:
                if done["BUY_NO"] and done["BUY_YES"]: break
                h = -offset
                if not (lo <= h < hi): continue
                bestn = besty = None
                for br, fl, cp, res in brs[date]:
                    cd = price_at(date, br, lst_min)
                    if not cd: continue
                    py = p_yes(fl, cp, mu, sig)
                    if py is None: continue
                    e = abs(py - cd[2] / 100.0)
                    if e < EDGE: continue
                    if py > cd[2] / 100.0:   # YES
                        cost = cd[2]
                        if 0 < cost < 100 and (besty is None or e > besty[0]):
                            besty = (e, res == "yes", (100 - cost) if res == "yes" else -cost)
                    else:                     # NO
                        cost = 100 - cd[1]
                        if 0 < cost < 100 and (bestn is None or e > bestn[0]):
                            bestn = (e, res == "no", (100 - cost) if res == "no" else -cost)
                for side, best in (("BUY_NO", bestn), ("BUY_YES", besty)):
                    if best and not done[side]:
                        d = pooled[lab][side]; d["w" if best[1] else "l"] += 1; d["pnl"] += best[2]
                        if side == "BUY_NO":
                            ps = pstat_no[iata][lab]; ps["w" if best[1] else "l"] += 1; ps["pnl"] += best[2]
                        done[side] = True
print("=== POOLED: BUY_NO vs BUY_YES by offset ===")
print("offset | NO: wr%% n pnl/bet | YES: wr%% n pnl/bet")
for lo, hi, lab in BK:
    n = pooled[lab]["BUY_NO"]; y = pooled[lab]["BUY_YES"]
    nn = n["w"] + n["l"]; yn = y["w"] + y["l"]
    print("%-6s | NO %3.0f%% %3d %+5.1f | YES %3.0f%% %3d %+5.1f" % (
        lab, (100 * n["w"] / nn) if nn else 0, nn, (n["pnl"] / nn) if nn else 0,
        (100 * y["w"] / yn) if yn else 0, yn, (y["pnl"] / yn) if yn else 0))
print("\n=== per-station BUY_NO (highest-WR offset, n>=12) ===")
for iata in sorted(pstat_no):
    best = None
    for lo, hi, lab in BK:
        d = pstat_no[iata][lab]; n = d["w"] + d["l"]
        if n >= 12:
            wr = 100 * d["w"] / n
            if best is None or wr > best[1]: best = (lab, wr, n, d["pnl"] / n)
    if best:
        print("  %-5s NO best WR %3.0f%% @ %-6s (n=%d, pnl/bet %+.1f)" % (iata, best[1], best[0], best[2], best[3]))
