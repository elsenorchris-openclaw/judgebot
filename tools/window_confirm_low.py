#!/usr/bin/env python3
"""Confirm LOW windows under CORRECT buy-at-start entry (bot fires on the first
in-window cycle w/ a qualifying bracket; position-cap pins it to the deep/early
edge). Flips the sort to largest-h-first so the sim enters at the window START,
matching the bot. Evaluates a focused window list + reports the realized entry-h
distribution so we can see where bets actually land."""
import csv, gzip, math, sqlite3, datetime
from collections import defaultdict

DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ = "/home/ubuntu/data/phq_ext"
MIN_DAY = "2026-02-18"; EDGE = 0.12; GLO, GHI = 10, 80
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5, "AUS": -6, "DFW": -6,
      "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6, "DEN": -7,
      "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8}
# (before, after) windows to confirm; window = [min-before, min+after], opens at h=before
WINS = [(1.5, -1.0), (2.0, -1.0), (1.5, -0.5), (1.0, -0.5), (0.5, 0.0), (0.5, 1.5)]


def phi(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def p_yes(fl, cp, mu, sig):
    if not sig or sig <= 0: sig = 1.5
    if fl is not None and cp is not None: return phi((cp+.5-mu)/sig) - phi((fl-.5-mu)/sig)
    if fl is not None: return 1.0 - phi((fl-.5-mu)/sig)
    if cp is not None: return phi((cp+.5-mu)/sig)
    return None


conn = sqlite3.connect(DB, timeout=60); conn.execute("PRAGMA busy_timeout=60000")
STORE = {}
for iata, off in sorted(ST.items()):
    phq = defaultdict(list)
    try:
        with gzip.open("%s/phq_raw_%s.csv.gz" % (PHQ, iata), "rt") as f:
            for r in csv.DictReader(f):
                if r["side"] != "low" or not r.get("mu_proj_f") or r["date"] < MIN_DAY: continue
                try: phq[r["date"]].append((float(r["cur_lst_min"]), float(r["offset"]), float(r["mu_proj_f"]), float(r["sigma_proj_f"] or 1.5)))
                except (ValueError, TypeError): continue
    except FileNotFoundError: continue
    # START-EDGE: largest h first => offset ASCENDING (h = -offset)
    for d in phq: phq[d].sort(key=lambda x: x[1])
    brs = defaultdict(list)
    for date, br, fl, cp, res in conn.execute("SELECT climate_day,bracket,floor,cap,result FROM market_meta WHERE station=? AND side='LOW' AND climate_day>=?", ("K"+iata, MIN_DAY)):
        if res in ("yes", "no"): brs[date].append((br, fl, cp, res))
    cand = defaultdict(list)
    for date, br, ts, yb, ya in conn.execute("SELECT climate_day,bracket,ts,yes_bid,yes_ask FROM candle_history WHERE station=? AND side='LOW' AND climate_day>=?", ("K"+iata, MIN_DAY)):
        lst = datetime.datetime.utcfromtimestamp(ts + off*3600)
        if lst.strftime("%Y-%m-%d") != date: continue
        cand[(date, br)].append((lst.hour*60+lst.minute, yb, ya))
    for k in cand: cand[k].sort()
    if phq and brs and cand: STORE[iata] = (phq, brs, cand)

ALL_DATES = sorted({d for iata in STORE for d in STORE[iata][0]})
MED = ALL_DATES[len(ALL_DATES)//2]


def price_at(cand, date, br, t):
    seq = cand.get((date, br)); b = None
    if not seq: return None
    for r in seq:
        if r[0] <= t and (b is None or r[0] > b[0]): b = r
    return b if (b and t-b[0] <= 60 and b[1] is not None and b[2] is not None and 0 < b[2] < 100) else None


def sim(before, after):
    """buy-at-START: first qualifying row scanning from window open (largest h)."""
    lo_h, hi_h = -after, before   # h in [-after, before]
    allb = []
    for iata in STORE:
        phq, brs, cand = STORE[iata]
        for date, rows in phq.items():
            if date not in brs: continue
            placed = None
            for lst_min, offset, mu, sig in rows:   # already h-descending
                h = -offset
                if not (lo_h <= h <= hi_h): continue
                best = None
                for br, fl, cp, res in brs[date]:
                    cd = price_at(cand, date, br, lst_min)
                    if not cd: continue
                    py = p_yes(fl, cp, mu, sig)
                    if py is None or py > cd[2]/100.0: continue
                    e = abs(py - cd[2]/100.0); cost = 100 - cd[1]
                    if e < EDGE or not (GLO <= cost <= GHI): continue
                    if best is None or e > best[0]:
                        best = (e, res == "no", (100-cost) if res == "no" else -cost)
                if best:
                    placed = (best[1], best[2], date, h); break
            if placed: allb.append(placed)
    return allb


def stat(sub):
    n = len(sub)
    if not n: return (0, 0.0, 0.0)
    return (n, 100*sum(1 for b in sub if b[0])/n, sum(b[1] for b in sub)/n)


print("LOW windows under BUY-AT-START | MIN_DAY=%s | %d stns | split@%s\n" % (MIN_DAY, len(STORE), MED))
print(" window(before,after)  = [min-b,min+a]   full   n   pnl/bet | early(n)  late(n)  mean_entry_h  flag")
for before, after in WINS:
    b = sim(before, after); n, wr, pnl = stat(b)
    ne, _, ep = stat([x for x in b if x[2] < MED]); nl, _, lp = stat([x for x in b if x[2] >= MED])
    mh = sum(x[3] for x in b)/n if n else 0
    flag = "ROBUST" if (ep > 0 and lp > 0 and n >= 15) else ("THIN" if n < 15 else "soft")
    tag = "  <-- current placeholder" if (before, after) == (0.5, 1.5) else ""
    print("  (%.1f,%+.1f) = [min-%.1f,min%+.1f]   %2.0f%% %3d %+6.2fc | %+5.1f(%d) %+5.1f(%d)  h=%.2f  %s%s" % (
        before, after, before, after, wr, n, pnl, ep, ne, lp, nl, mh, flag, tag))
