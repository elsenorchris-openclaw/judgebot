#!/usr/bin/env python3
"""Does bid-ask SPREAD help? For HIGH (deep-pre-peak) and LOW (deep-pre-min)
gated BUY_NO bets, bin by spread (yes_ask-yes_bid) and compare PnL at the
AGGRESSIVE cross price (100-yes_bid, what the backtest+bot pay) vs MID
(100-(yb+ya)/2) vs PASSIVE post (100-yes_ask). Tells us: (1) is the edge
concentrated in tight-spread markets (=> spread filter helps), (2) how much the
spread-crossing costs (=> would posting/mid help)."""
import csv, gzip, math, sqlite3, datetime, statistics
from collections import defaultdict

DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ = "/home/ubuntu/data/phq_ext"
MIN_DAY = "2026-03-15"; EDGE = 0.12; GLO, GHI = 10, 80
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5, "AUS": -6, "DFW": -6,
      "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6, "DEN": -7,
      "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8}
SPREAD_BK = [(0, 3, "0-3c (tight)"), (3, 7, "3-7c"), (7, 15, "7-15c"), (15, 30, "15-30c"), (30, 101, "30c+ (wide)")]


def phi(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def p_yes(fl, cp, mu, sig):
    if not sig or sig <= 0: sig = 1.5
    if fl is not None and cp is not None: return phi((cp+.5-mu)/sig) - phi((fl-.5-mu)/sig)
    if fl is not None: return 1.0 - phi((fl-.5-mu)/sig)
    if cp is not None: return phi((cp+.5-mu)/sig)
    return None


def collect(side_phq, side_db):
    conn = sqlite3.connect(DB, timeout=60); conn.execute("PRAGMA busy_timeout=60000")
    bets = []   # (spread, won, cost_cross)
    for iata, off in sorted(ST.items()):
        phq = defaultdict(list)
        try:
            with gzip.open("%s/phq_raw_%s.csv.gz" % (PHQ, iata), "rt") as f:
                for r in csv.DictReader(f):
                    if r["side"] != side_phq or not r.get("mu_proj_f") or r["date"] < MIN_DAY: continue
                    try: phq[r["date"]].append((float(r["cur_lst_min"]), float(r["offset"]), float(r["mu_proj_f"]), float(r["sigma_proj_f"] or 1.5)))
                    except (ValueError, TypeError): continue
        except FileNotFoundError: continue
        for d in phq: phq[d].sort(key=lambda x: -x[1])
        brs = defaultdict(list)
        for date, br, fl, cp, res in conn.execute("SELECT climate_day,bracket,floor,cap,result FROM market_meta WHERE station=? AND side=? AND climate_day>=?", ("K"+iata, side_db, MIN_DAY)):
            if res in ("yes", "no"): brs[date].append((br, fl, cp, res))
        cand = defaultdict(list)
        for date, br, ts, yb, ya in conn.execute("SELECT climate_day,bracket,ts,yes_bid,yes_ask FROM candle_history WHERE station=? AND side=? AND climate_day>=?", ("K"+iata, side_db, MIN_DAY)):
            lst = datetime.datetime.utcfromtimestamp(ts + off*3600)
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
                if not (1.0 <= h <= 3.5): continue   # deep pre-extreme
                best = None
                for br, fl, cp, res in brs[date]:
                    cd = price_at(date, br, clm)
                    if not cd: continue
                    py = p_yes(fl, cp, mu, sig)
                    if py is None or py > cd[2]/100.0: continue
                    e = abs(py - cd[2]/100.0); yb, ya = cd[1], cd[2]; cost = 100 - yb
                    if e < EDGE or not (GLO <= cost <= GHI): continue
                    if best is None or e > best[0]:
                        best = (e, res == "no", ya - yb, yb, ya)
                if best: chosen = best; break
            if chosen:
                _, won, spread, yb, ya = chosen
                bets.append((spread, won, 100-yb, 100-(yb+ya)/2.0, 100-ya))
    conn.close(); return bets


def pnl(sub, costidx):
    n = len(sub)
    if not n: return "n=0"
    w = sum(1 for b in sub if b[1])
    p = sum(((100-b[costidx]) if b[1] else -b[costidx]) for b in sub)/n
    return "%2.0f%% n=%-4d %+5.1f" % (100*w/n, n, p)


for label, sp, sd in (("HIGH (deep-pre-peak)", "high", "HIGH"), ("LOW (deep-pre-min)", "low", "LOW")):
    bets = collect(sp, sd)
    spreads = [b[0] for b in bets]
    print("\n===== %s BUY_NO, gated, h2ext 1-3h | n=%d =====" % (label, len(bets)))
    print("  spread median=%.0fc mean=%.0fc  (cross cost = 100-yes_bid)" % (
        statistics.median(spreads) if spreads else 0, statistics.mean(spreads) if spreads else 0))
    print("  %-14s | %-18s | %-18s | %-18s" % ("spread bucket", "CROSS (bot pays)", "MID price", "PASSIVE (post)"))
    for lo, hi, lab in SPREAD_BK:
        seg = [b for b in bets if lo <= b[0] < hi]
        print("  %-14s | %s | %s | %s" % (lab, pnl(seg, 2), pnl(seg, 3), pnl(seg, 4)))
    print("  ALL            | %s | %s | %s" % (pnl(bets, 2), pnl(bets, 3), pnl(bets, 4)))
