#!/usr/bin/env python3
"""Most-profitable 30-min HIGH BUY_NO window per station (Chris). Data is on a
0.5h h2pk grid, so a 30-min window = one buy-slot. We buy at the START of the
window, so the bet sits at the slot's h2pk. For each station, test each slot
X in {3.5..0.5} (gated [10,80], one NO/day at that slot), pick the max-PnL slot,
flag via the early/late split. Ship window = [peak-X, peak-(X-0.5)] (buy at
peak-X). NOTE close<peak-? : slots X<1.5 close within 1h of peak (riskier)."""
import csv, gzip, math, sqlite3, datetime, statistics
from collections import defaultdict

DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ = "/home/ubuntu/data/phq_ext"
MIN_DAY = "2026-03-15"; EDGE = 0.12; GLO, GHI = 10, 80
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5, "AUS": -6, "DFW": -6,
      "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6, "DEN": -7,
      "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8, "DCA": -5}
SLOTS = [3.5, 3.0, 2.5, 2.0, 1.5, 1.0, 0.5]   # buy-point h2pk (30-min window opens here)


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
                if r["side"] != "high" or not r.get("mu_proj_f") or r["date"] < MIN_DAY: continue
                try: phq[r["date"]].append((float(r["cur_lst_min"]), float(r["offset"]), float(r["mu_proj_f"]), float(r["sigma_proj_f"] or 1.5)))
                except (ValueError, TypeError): continue
    except FileNotFoundError: continue
    for d in phq: phq[d].sort(key=lambda x: -x[1])
    brs = defaultdict(list)
    for date, br, fl, cp, res in conn.execute("SELECT climate_day,bracket,floor,cap,result FROM market_meta WHERE station=? AND side='HIGH' AND climate_day>=?", ("K"+iata, MIN_DAY)):
        if res in ("yes", "no"): brs[date].append((br, fl, cp, res))
    cand = defaultdict(list)
    for date, br, ts, yb, ya in conn.execute("SELECT climate_day,bracket,ts,yes_bid,yes_ask FROM candle_history WHERE station=? AND side='HIGH' AND climate_day>=?", ("K"+iata, MIN_DAY)):
        lst = datetime.datetime.utcfromtimestamp(ts + off*3600)
        if lst.strftime("%Y-%m-%d") != date: continue
        cand[(date, br)].append((lst.hour*60+lst.minute, yb, ya))
    for k in cand: cand[k].sort()
    STORE[iata] = (phq, brs, cand)

ALL_DATES = sorted({d for iata in STORE for d in STORE[iata][0]})
MED = ALL_DATES[len(ALL_DATES)//2]


def price_at(cand, date, br, t):
    seq = cand.get((date, br)); b = None
    if not seq: return None
    for r in seq:
        if r[0] <= t and (b is None or r[0] > b[0]): b = r
    return b if (b and t-b[0] <= 60 and b[1] is not None and b[2] is not None and 0 < b[2] < 100) else None


def sim_slot(iata, X):
    """one gated BUY_NO/day at the h2pk=X slot (window [X-0.25, X+0.25))."""
    phq, brs, cand = STORE[iata]; bets = []
    for date, rows in phq.items():
        if date not in brs: continue
        placed = None
        for lst_min, offset, mu, sig in rows:
            h = -offset
            if not (X-0.25 <= h < X+0.25): continue
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
                placed = (best[1], best[2], date); break
        if placed: bets.append(placed)
    return bets


def st(sub):
    n = len(sub)
    if not n: return (0, 0.0, 0.0)
    return (n, 100*sum(1 for b in sub if b[0])/n, sum(b[1] for b in sub)/n)


print("MOST-PROFITABLE 30-MIN HIGH BUY_NO SLOT per station (faithful buy-at-slot, gated). split @ %s\n" % MED)
print("%-5s | %-46s | all-slots table (PnL/bet by buy-slot h2pk)" % ("stn", "BEST 30-min"))
ship = {}
for iata in sorted(STORE):
    row = []; best = None
    for X in SLOTS:
        b = sim_slot(iata, X); n, wr, pnl = st(b)
        row.append("%.1f:%+4.1f(%d)" % (X, pnl, n) if n else "%.1f:--" % X)
        if n >= 3:
            ne, _, ep = st([x for x in b if x[2] < MED]); nl, _, lp = st([x for x in b if x[2] >= MED])
            if best is None or pnl > best[1]:
                best = (X, pnl, n, wr, ep, lp, ne, nl)
    if best is None:
        print("%-5s | (no slot n>=3)                                | %s" % (iata, "  ".join(row)))
        continue
    X, pnl, n, wr, ep, lp, ne, nl = best
    if n < 10: flag = "LOWCONF(n=%d)" % n
    elif ep > 0 and lp > 0 and n >= 15: flag = "ROBUST"
    elif n >= 15: flag = "SOFT"
    else: flag = "THIN"
    if pnl <= 0: flag = "NEG-EV/" + flag
    risk = " [closes <1h to peak]" if X < 1.5 else ""
    print("%-5s | X=%.1f  %2.0f%% n=%-3d %+5.1fc  e%+.1f/l%+.1f  %s%s | %s" % (
        iata, X, wr, n, pnl, ep, lp, flag, risk, "  ".join(row)))
    ship[iata] = (X, pnl, flag)

print("\n=== ship-ready 30-min windows (before, after)=[peak-X, peak-(X-0.5)], buy at peak-X ===")
print("PUSH_HIGH_TEMP_WINDOW_BY_STATION = {")
for iata in sorted(ship):
    X, pnl, flag = ship[iata]
    print('    "K%s": (%.2f, %.2f),  # 30-min @ peak-%.1f  %+.1fc/bet  %s' % (iata, X, -(X-0.5), X, pnl, flag))
print("}")
