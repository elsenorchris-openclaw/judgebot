#!/usr/bin/env python3
"""Validate the HIGH spread filter OOS: HIGH gated deep-pre-peak BUY_NO, compare
spread-cap thresholds (skip if yes_ask-yes_bid > cap) on full + early/late."""
import csv, gzip, math, sqlite3, datetime
from collections import defaultdict
DB = "/home/ubuntu/data/market_history_backfill.sqlite"; PHQ = "/home/ubuntu/data/phq_ext"
MIN_DAY = "2026-03-15"; EDGE = 0.12; GLO, GHI = 10, 80
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5, "AUS": -6, "DFW": -6, "HOU": -6,
      "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6, "DEN": -7, "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8}
def phi(x): return 0.5*(1.0+math.erf(x/math.sqrt(2.0)))
def p_yes(fl, cp, mu, sig):
    if not sig or sig <= 0: sig = 1.5
    if fl is not None and cp is not None: return phi((cp+.5-mu)/sig)-phi((fl-.5-mu)/sig)
    if fl is not None: return 1.0-phi((fl-.5-mu)/sig)
    if cp is not None: return phi((cp+.5-mu)/sig)
    return None
conn = sqlite3.connect(DB, timeout=60); conn.execute("PRAGMA busy_timeout=60000")
bets = []
for iata, off in sorted(ST.items()):
    phq = defaultdict(list)
    with gzip.open("%s/phq_raw_%s.csv.gz" % (PHQ, iata), "rt") as f:
        for r in csv.DictReader(f):
            if r["side"] != "high" or not r.get("mu_proj_f") or r["date"] < MIN_DAY: continue
            try: phq[r["date"]].append((float(r["cur_lst_min"]), float(r["offset"]), float(r["mu_proj_f"]), float(r["sigma_proj_f"] or 1.5)))
            except (ValueError, TypeError): continue
    for d in phq: phq[d].sort(key=lambda x: -x[1])
    brs = defaultdict(list)
    for date, br, fl, cp, res in conn.execute("SELECT climate_day,bracket,floor,cap,result FROM market_meta WHERE station=? AND side='HIGH' AND climate_day>=?", ("K"+iata, MIN_DAY)):
        if res in ("yes", "no"): brs[date].append((br, fl, cp, res))
    cand = defaultdict(list)
    for date, br, ts, yb, ya in conn.execute("SELECT climate_day,bracket,ts,yes_bid,yes_ask FROM candle_history WHERE station=? AND side='HIGH' AND climate_day>=?", ("K"+iata, MIN_DAY)):
        lst = datetime.datetime.utcfromtimestamp(ts+off*3600)
        if lst.strftime("%Y-%m-%d") != date: continue
        cand[(date, br)].append((lst.hour*60+lst.minute, yb, ya))
    for k in cand: cand[k].sort()
    def price_at(date, br, t):
        seq = cand.get((date, br)); b = None
        if not seq: return None
        for r in seq:
            if r[0] <= t and (b is None or r[0] > b[0]): b = r
        return b if (b and t-b[0] <= 60 and b[1] is not None and b[2] is not None and 0 < b[2] < 100) else None
    for date, rows in phq.items():
        if date not in brs: continue
        chosen = None
        for clm, offv, mu, sig in rows:
            h = -offv
            if not (1.0 <= h <= 3.5): continue
            best = None
            for br, fl, cp, res in brs[date]:
                cd = price_at(date, br, clm)
                if not cd: continue
                py = p_yes(fl, cp, mu, sig)
                if py is None or py > cd[2]/100.0: continue
                e = abs(py-cd[2]/100.0); yb, ya = cd[1], cd[2]; cost = 100-yb
                if e < EDGE or not (GLO <= cost <= GHI): continue
                if best is None or e > best[0]: best = (e, res == "no", ya-yb, cost)
            if best: chosen = best; break
        if chosen: bets.append((chosen[2], chosen[1], chosen[3], date))   # spread, won, cost, date
dates = sorted(set(b[3] for b in bets)); MED = dates[len(dates)//2]
def agg(sub):
    n = len(sub)
    if not n: return "n=0       "
    w = sum(1 for b in sub if b[1]); p = sum(((100-b[2]) if b[1] else -b[2]) for b in sub)/n
    return "%2.0f%% n=%-3d %+5.1f" % (100*w/n, n, p)
print("HIGH spread-filter validation (deep-pre-peak gated NO) split@%s\n" % MED)
print("%-16s | %-14s | %-14s | %-14s" % ("filter", "full", "early", "late"))
for cap, lab in [(999, "none"), (20, "<=20c"), (15, "<=15c"), (12, "<=12c"), (8, "<=8c")]:
    f = [b for b in bets if b[0] <= cap]
    print("%-16s | %s | %s | %s" % (lab, agg(f), agg([b for b in f if b[3] < MED]), agg([b for b in f if b[3] >= MED])))
