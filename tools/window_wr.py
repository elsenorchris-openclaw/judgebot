#!/usr/bin/env python3
"""Per-station WIN RATE by buy-offset. Honors the position cap: one buy per
station/day at the FIRST qualifying offset (window start). For each 0.5h h2pk
bucket, simulate 'window starts here' -> buy the best-edge bracket at the first
qualifying offset in the bucket -> record win/lose. Reports WR + PnL + n per
bucket per station, and the highest-WR bucket (the window start to use)."""
import csv, gzip, math, sqlite3, datetime
from collections import defaultdict

DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ = "/home/ubuntu/data/phq_ext"
MIN_DAY = "2026-03-15"; EDGE = 0.12
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5, "AUS": -6, "DFW": -6,
      "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6, "DEN": -7,
      "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8}
# 0.5h h2pk buckets from deep-pre-peak to past-peak
BK = [(2.5, 3.5, "2.5-3.5"), (2.0, 2.5, "2.0-2.5"), (1.5, 2.0, "1.5-2.0"),
      (1.0, 1.5, "1.0-1.5"), (0.5, 1.0, "0.5-1.0"), (0.0, 0.5, "0.0-0.5"),
      (-0.5, 0.0, "-.5-0.0"), (-1.5, -0.5, "<-0.5")]


def phi(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def p_yes(fl, cp, mu, sig):
    if not sig or sig <= 0: sig = 1.5
    if fl is not None and cp is not None: return phi((cp + .5 - mu) / sig) - phi((fl - .5 - mu) / sig)
    if fl is not None: return 1.0 - phi((fl - .5 - mu) / sig)
    if cp is not None: return phi((cp + .5 - mu) / sig)
    return None


conn = sqlite3.connect(DB, timeout=60); conn.execute("PRAGMA busy_timeout=60000")
print("%-5s | " % "stn" + " ".join("%9s" % b[2] for b in BK))
print("      | " + " ".join("%9s" % "wr%(n)" for _ in BK))
best_summary = []
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
    for d in phq: phq[d].sort(key=lambda x: -x[1])  # highest h2pk first
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
        best = None
        for r in seq:
            if r[0] <= t and (best is None or r[0] > best[0]): best = r
        return best if (best and t - best[0] <= 60 and best[1] is not None and best[2] is not None and 0 < best[2] < 100) else None
    agg = {b[2]: {"w": 0, "l": 0, "pnl": 0} for b in BK}
    for date, rows in phq.items():
        if date not in brs: continue
        for lo, hi, lab in BK:
            placed = False
            for lst_min, offset, mu, sig in rows:   # highest h2pk first within the bucket
                if placed: break
                h = -offset
                if not (lo <= h < hi): continue
                best = None
                for br, fl, cp, res in brs[date]:
                    cd = price_at(date, br, lst_min)
                    if not cd: continue
                    py = p_yes(fl, cp, mu, sig)
                    if py is None: continue
                    e = abs(py - cd[2] / 100.0)
                    if e < EDGE: continue
                    sy = py > cd[2] / 100.0; cost = cd[2] if sy else (100 - cd[1])
                    if not (0 < cost < 100): continue
                    if best is None or e > best[0]:
                        won = (res == "yes") if sy else (res == "no")
                        best = (e, won, (100 - cost) if won else -cost)
                if best:
                    a = agg[lab]; a["w" if best[1] else "l"] += 1; a["pnl"] += best[2]; placed = True
    cells = []
    bestwr = None
    for lo, hi, lab in BK:
        a = agg[lab]; n = a["w"] + a["l"]
        wr = (100 * a["w"] / n) if n else 0
        cells.append("%3.0f%%(%2d)" % (wr, n) if n else "    -    ")
        if n >= 12 and (bestwr is None or wr > bestwr[1]):
            bestwr = (lab, wr, n, a["pnl"] / n if n else 0)
    print("%-5s | " % iata + " ".join("%9s" % c for c in cells))
    if bestwr:
        best_summary.append((iata, bestwr))
print("\n=== highest-WR bucket per station (n>=12) ===")
for iata, (lab, wr, n, pb) in best_summary:
    print("  %-5s best WR %3.0f%% @ h2pk %-8s (n=%d, PnL/bet %+.1f)" % (iata, wr, lab, n, pb))
