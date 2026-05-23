#!/usr/bin/env python3
"""LOW analog of the HIGH deep-pre-peak finding: is there a deep-pre-MIN BUY_NO
edge (close before the daily min) under the bot's real [10,80] price gate?
Same faithful gated method + early/late split. h = -offset = hours to the min
(daily min is the LOW extreme). NOTE: LOW candle/market backfill is incomplete
(mostly KPHX), so expect thin n outside PHX."""
import csv, gzip, math, sqlite3, datetime, statistics
from collections import defaultdict

DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ = "/home/ubuntu/data/phq_ext"
MIN_DAY = "2026-03-15"; EDGE = 0.12; GLO, GHI = 10, 80
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5, "AUS": -6, "DFW": -6,
      "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6, "DEN": -7,
      "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8}
BK = [(2.0, 3.5, ">=2.0"), (1.0, 2.0, "1-2"), (0.0, 1.0, "0-1"), (-1.5, 0.0, "<0")]


def phi(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def p_yes(fl, cp, mu, sig):
    if not sig or sig <= 0: sig = 1.5
    if fl is not None and cp is not None: return phi((cp+.5-mu)/sig) - phi((fl-.5-mu)/sig)
    if fl is not None: return 1.0 - phi((fl-.5-mu)/sig)
    if cp is not None: return phi((cp+.5-mu)/sig)
    return None


conn = sqlite3.connect(DB, timeout=60); conn.execute("PRAGMA busy_timeout=60000")
bets = []
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
    for date, br, fl, cp, res in conn.execute("SELECT climate_day,bracket,floor,cap,result FROM market_meta WHERE station=? AND side='LOW' AND climate_day>=?", ("K"+iata, MIN_DAY)):
        if res in ("yes", "no"): brs[date].append((br, fl, cp, res))
    cand = defaultdict(list)
    for date, br, ts, yb, ya in conn.execute("SELECT climate_day,bracket,ts,yes_bid,yes_ask FROM candle_history WHERE station=? AND side='LOW' AND climate_day>=?", ("K"+iata, MIN_DAY)):
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
        for lo, hi, lab in BK:
            best = None
            for lst_min, offset, mu, sig in rows:
                h = -offset
                if not (lo <= h < hi): continue
                for br, fl, cp, res in brs[date]:
                    cd = price_at(date, br, lst_min)
                    if not cd: continue
                    py = p_yes(fl, cp, mu, sig)
                    if py is None or py > cd[2]/100.0: continue
                    e = abs(py - cd[2]/100.0); cost = 100 - cd[1]
                    if e < EDGE or not (GLO <= cost <= GHI): continue
                    if best is None or e > best[0]:
                        best = (e, res == "no", (100-cost) if res == "no" else -cost)
            if best:
                bets.append((lab, best[1], best[2], date, iata))

dates = sorted(set(b[3] for b in bets))
if not dates:
    print("NO LOW bets — no candle/market data for the gate. Backfill incomplete."); raise SystemExit
med = dates[len(dates)//2]
print("LOW gated BUY_NO  | %s..%s  split@%s  total bets %d\n" % (dates[0], dates[-1], med, len(bets)))


def agg(sub):
    n = len(sub)
    if not n: return "n=0"
    w = sum(1 for b in sub if b[1]); pnl = [b[2] for b in sub]
    se = statistics.pstdev(pnl)/math.sqrt(n) if n > 1 else 0
    return "%2.0f%% n=%-3d %+5.1f±%4.1f" % (100*w/n, n, sum(pnl)/n, se)


print("=== POOLED LOW gated BUY_NO by offset-to-MIN ===")
print("%-6s | %-18s | %-18s | %-18s" % ("h2min", "full", "early", "late"))
for lo, hi, lab in BK:
    g = [b for b in bets if b[0] == lab]
    print("%-6s | %s | %s | %s" % (lab, agg(g), agg([b for b in g if b[3] < med]), agg([b for b in g if b[3] >= med])))

print("\n=== per-station deep-pre-min (h2min>=1.0) LOW gated BUY_NO ===")
for iata in sorted(set(b[4] for b in bets)):
    g = [b for b in bets if b[4] == iata and b[0] in (">=2.0", "1-2")]
    if not g: continue
    print("  %-5s full %s | early %s | late %s" % (iata, agg(g), agg([b for b in g if b[3] < med]), agg([b for b in g if b[3] >= med])))

allg = [b for b in bets if b[0] in (">=2.0", "1-2")]
print("\n  POOLED deep-pre-min: full %s | early %s | late %s" % (
    agg(allg), agg([b for b in allg if b[3] < med]), agg([b for b in allg if b[3] >= med])))
