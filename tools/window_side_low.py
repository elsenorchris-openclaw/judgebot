#!/usr/bin/env python3
"""LOW analog of window_side.py — split WR/PnL by BET SIDE (BUY_NO vs BUY_YES)
and offset bucket, for LOW (daily-min) markets. Lowers MIN_DAY to 2026-02-18 to
capture the Feb18-Mar21 winter overlap (phq_ext starts 2026-02-18; the 7 winter
LOW candle stations end 2026-03-21) the prior 03-15 cutoff threw away. Also prints
per-station n + usable date range so per-station viability is judgeable.
offset is hours-rel-to-MIN; h = -offset = hours-to-min (h>0 before, h<0 after)."""
import csv, gzip, math, sqlite3, datetime
from collections import defaultdict

DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ = "/home/ubuntu/data/phq_ext"
MIN_DAY = "2026-02-18"; EDGE = 0.12
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5, "AUS": -6, "DFW": -6,
      "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6, "DEN": -7,
      "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8}
# finer near-min buckets (h = hours to min)
BK = [(2.0, 3.5, ">=2.0"), (1.0, 2.0, "1-2"), (0.0, 1.0, "0-1"),
      (-1.0, 0.0, "0to1past"), (-2.0, -1.0, "1to2past")]


def phi(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def p_yes(fl, cp, mu, sig):
    if not sig or sig <= 0: sig = 1.5
    if fl is not None and cp is not None: return phi((cp + .5 - mu) / sig) - phi((fl - .5 - mu) / sig)
    if fl is not None: return 1.0 - phi((fl - .5 - mu) / sig)
    if cp is not None: return phi((cp + .5 - mu) / sig)
    return None


conn = sqlite3.connect(DB, timeout=60); conn.execute("PRAGMA busy_timeout=60000")
pooled = defaultdict(lambda: defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0}))
pstat_no = defaultdict(lambda: defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0}))
pstat_meta = {}  # iata -> (n_bets, min_date, max_date)
for iata, off in sorted(ST.items()):
    phq = defaultdict(list)
    try:
        with gzip.open("%s/phq_raw_%s.csv.gz" % (PHQ, iata), "rt") as f:
            for r in csv.DictReader(f):
                if r["side"] != "low" or not r.get("mu_proj_f") or r["date"] < MIN_DAY: continue
                try: phq[r["date"]].append((float(r["cur_lst_min"]), float(r["offset"]), float(r["mu_proj_f"]), float(r["sigma_proj_f"] or 1.5)))
                except (ValueError, TypeError): continue
    except FileNotFoundError: continue
    for d in phq: phq[d].sort(key=lambda x: -x[1])
    brs = defaultdict(list)
    for date, br, fl, cp, res in conn.execute("SELECT climate_day,bracket,floor,cap,result FROM market_meta WHERE station=? AND side='LOW' AND climate_day>=?", ("K" + iata, MIN_DAY)):
        if res in ("yes", "no"): brs[date].append((br, fl, cp, res))
    cand = defaultdict(list)
    for date, br, ts, yb, ya in conn.execute("SELECT climate_day,bracket,ts,yes_bid,yes_ask FROM candle_history WHERE station=? AND side='LOW' AND climate_day>=?", ("K" + iata, MIN_DAY)):
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
    nb = 0; bet_dates = []
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
                            nb += 1; bet_dates.append(date)
                        done[side] = True
    if bet_dates:
        pstat_meta[iata] = (nb, min(bet_dates), max(bet_dates))

print("=== LOW POOLED: BUY_NO vs BUY_YES by offset (MIN_DAY=%s) ===" % MIN_DAY)
print("h-to-min | NO: wr%% n pnl/bet | YES: wr%% n pnl/bet")
for lo, hi, lab in BK:
    n = pooled[lab]["BUY_NO"]; y = pooled[lab]["BUY_YES"]
    nn = n["w"] + n["l"]; yn = y["w"] + y["l"]
    print("%-9s | NO %3.0f%% %3d %+6.1f | YES %3.0f%% %3d %+6.1f" % (
        lab, (100 * n["w"] / nn) if nn else 0, nn, (n["pnl"] / nn) if nn else 0,
        (100 * y["w"] / yn) if yn else 0, yn, (y["pnl"] / yn) if yn else 0))

print("\n=== per-station BUY_NO usable data (n + date range) ===")
for iata in sorted(pstat_meta):
    nb, d0, d1 = pstat_meta[iata]
    print("  %-5s n=%-4d  %s .. %s" % (iata, nb, d0, d1))

print("\n=== per-station BUY_NO by offset bucket (n>=6) ===")
for iata in sorted(pstat_no):
    parts = []
    for lo, hi, lab in BK:
        d = pstat_no[iata][lab]; n = d["w"] + d["l"]
        if n >= 6:
            parts.append("%s:%2.0f%%/%d/%+.0f" % (lab, 100 * d["w"] / n, n, d["pnl"] / n))
    if parts: print("  %-5s %s" % (iata, "  ".join(parts)))
