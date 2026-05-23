#!/usr/bin/env python3
"""LOW BUY_NO profitability AS A FUNCTION OF ENTRY OFFSET (buy-at-start semantics).

The bot buys at the START of the decision window: _try_auto_execute fires on the
FIRST in-window cycle with a qualifying bracket, and the position cap (=1) blocks
all later cycles -> entry is pinned to the deep/early edge. So the right question
is NOT "best 1.5h window" but "at exactly which offset is buy-at-that-instant most
profitable" -> then put a NARROW 30-min window's START there.

phq_ext rows are at native 0.5h spacing, so each offset bin = 30 min. For each
(station, day, offset) we place one max-edge BUY_NO bet at that instant (same
EDGE=0.12 + NO-cost in [10,80] gate as the bot) and settle it. Aggregated pooled +
per-station, with the early(winter)/late(PHX-spring) split. The argmax offset b*
gives the 30-min window (before=b*, after=0.5-b*) = [min-b*, min-(b*-0.5)]."""
import csv, gzip, math, sqlite3, datetime
from collections import defaultdict

DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ = "/home/ubuntu/data/phq_ext"
MIN_DAY = "2026-02-18"; EDGE = 0.12; GLO, GHI = 10, 80
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5, "AUS": -6, "DFW": -6,
      "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6, "DEN": -7,
      "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8}
# offsets to profile (h = hours to min; h>0 before min). 30-min steps.
HS = [2.5, 2.0, 1.5, 1.0, 0.5, 0.0, -0.5, -1.0]


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
    phq = defaultdict(dict)  # date -> {h: (lst_min, mu, sig)}
    try:
        with gzip.open("%s/phq_raw_%s.csv.gz" % (PHQ, iata), "rt") as f:
            for r in csv.DictReader(f):
                if r["side"] != "low" or not r.get("mu_proj_f") or r["date"] < MIN_DAY: continue
                try:
                    h = round(-float(r["offset"]) * 2) / 2.0
                    phq[r["date"]][h] = (float(r["cur_lst_min"]), float(r["mu_proj_f"]), float(r["sigma_proj_f"] or 1.5))
                except (ValueError, TypeError): continue
    except FileNotFoundError: continue
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


def bet_at(iata, h):
    """one max-edge BUY_NO bet at exactly offset h, per day. -> list of (win,pnl,date)."""
    phq, brs, cand = STORE[iata]; out = []
    for date, hmap in phq.items():
        if date not in brs or h not in hmap: continue
        lst_min, mu, sig = hmap[h]
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
        if best: out.append((best[1], best[2], date))
    return out


def stat(sub):
    n = len(sub)
    if not n: return (0, 0.0, 0.0)
    return (n, 100*sum(1 for b in sub if b[0])/n, sum(b[1] for b in sub)/n)


print("LOW BUY_NO profit by ENTRY OFFSET (buy-at-instant) | MIN_DAY=%s | %d stns | split@%s\n" % (MIN_DAY, len(STORE), MED))
print("=== POOLED-GLOBAL: pnl/bet vs entry offset (h = hrs before min) ===")
print(" h(before min)  full   n   pnl/bet | early(n)  late(n)")
pooled_by_h = {}
for h in HS:
    allb = []
    for iata in STORE: allb += bet_at(iata, h)
    n, wr, pnl = stat(allb)
    if n < 15:
        print("  %+4.1fh   (n=%d too thin)" % (h, n)); continue
    ne, _, ep = stat([x for x in allb if x[2] < MED]); nl, _, lp = stat([x for x in allb if x[2] >= MED])
    pooled_by_h[h] = (pnl, n, wr, ep, ne, lp, nl)
    rob = "ROBUST" if (ep > 0 and lp > 0) else ""
    print("  %+4.1fh        %2.0f%% %3d %+6.2fc | %+5.1f(%d) %+5.1f(%d)  %s" % (h, wr, n, pnl, ep, ne, lp, nl, rob))

bh = max(pooled_by_h, key=lambda k: pooled_by_h[k][0])
bp, bn, bw, bep, bne, blp, bnl = pooled_by_h[bh]
before = bh; after = round(0.5 - bh, 1)
print("\n  >>> PEAK PROFIT OFFSET = %+.1fh before min  (%+.2fc/bet, n=%d, %2.0f%%)" % (bh, bp, bn, bw))
print("  >>> 30-min window with START at peak: PUSH_LOW_TEMP_WINDOW = (%.1f, %.1f) = [min-%.1f, min-%.1f]"
      % (before, after, before, before - 0.5))

print("\n=== per-station: pnl/bet at each entry offset (n>=4 shown) ===")
print(" stn  " + "  ".join("%+.1fh" % h for h in HS))
for iata in sorted(STORE):
    cells = []
    for h in HS:
        b = bet_at(iata, h); n, wr, pnl = stat(b)
        cells.append(("%+5.0f/%d" % (pnl, n)) if n >= 4 else ("  -/%d" % n if n else "   -"))
    print("  %-4s " % iata + "  ".join("%-8s" % c for c in cells))
