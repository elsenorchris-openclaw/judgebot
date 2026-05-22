#!/usr/bin/env python3
"""Fix the mu-replay backtest: (1) use the matcher's own per-row cur_lst_min for
the candle-price lookup (NOT an averaged peak -> was looking up price 1-2h off),
(2) use the per-day TRACE peak (day_max/min_lst_min from heating_traces) for the
offset. Validate the fixed mu-replay WR-by-offset against the faithful shadow-log
result (pre-peak 58%, near/post 33%) on the overlapping recent days."""
import csv, gzip, math, sqlite3, datetime
from collections import defaultdict

BK = "/home/ubuntu/data/market_history_backfill.sqlite"
TRACES = "/home/ubuntu/data/heating_traces_ext.sqlite"
PHQ = "/home/ubuntu/data/phq_ext"
MIN_DAY = "2026-05-15"; EDGE = 0.12
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5, "AUS": -6, "DFW": -6,
      "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6, "DEN": -7,
      "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8}


def phi(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def p_yes(fl, cp, mu, sig):
    if not sig or sig <= 0: sig = 1.5
    if fl is not None and cp is not None: return phi((cp+.5-mu)/sig) - phi((fl-.5-mu)/sig)
    if fl is not None: return 1.0 - phi((fl-.5-mu)/sig)
    if cp is not None: return phi((cp+.5-mu)/sig)
    return None

# trace peaks per (station, date): actual day_max/min LST minute
tr = sqlite3.connect(TRACES)
peak = {}  # (Kstation, date, side) -> lst_min
for st, dt, mx, mn in tr.execute("SELECT station,lst_date,day_max_lst_min,day_min_lst_min FROM station_days WHERE lst_date>=?", (MIN_DAY,)):
    peak[("K"+st, dt, "high")] = mx
    peak[("K"+st, dt, "low")] = mn

conn = sqlite3.connect(BK, timeout=60)
agg = defaultdict(lambda: {"w": 0, "l": 0})  # bucket -> counts (HIGH only, for validation)
for iata, off in sorted(ST.items()):
    if iata == "DCA": continue
    # phq: per (date) -> [(cur_lst_min, mu, sig)]
    rows = defaultdict(list)
    try:
        with gzip.open("%s/phq_raw_%s.csv.gz" % (PHQ, iata), "rt") as f:
            for r in csv.DictReader(f):
                if r["side"] != "high" or not r.get("mu_proj_f") or r["date"] < MIN_DAY: continue
                try: rows[r["date"]].append((float(r["cur_lst_min"]), float(r["mu_proj_f"]), float(r["sigma_proj_f"] or 1.5)))
                except (ValueError, TypeError): continue
    except FileNotFoundError: continue
    for d in rows: rows[d].sort()
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
        seq = cand.get((date, br))
        if not seq: return None
        b = None
        for r in seq:
            if r[0] <= t and (b is None or r[0] > b[0]): b = r
        return b if (b and t-b[0] <= 60 and b[1] is not None and b[2] is not None and 0 < b[2] < 100) else None
    def bk(h):
        return ">=1" if h >= 1.0 else "<1"
    for date, prows in rows.items():
        if date not in brs: continue
        pk = peak.get(("K"+iata, date, "high"))
        if pk is None: continue
        # one bet per (bucket): first qualifying (earliest cur_lst_min) in bucket
        seen = set()
        # sort by h2pk descending (earliest time = highest h2pk)
        srt = sorted(prows, key=lambda x: -(pk - x[0]))
        for clm, mu, sig in srt:
            h2 = (pk - clm) / 60.0
            b = bk(h2)
            if b in seen: continue
            best = None
            for br, fl, cp, res in brs[date]:
                cd = price_at(date, br, clm)        # <-- price at the matcher's OWN cur_lst_min
                if not cd: continue
                py = p_yes(fl, cp, mu, sig)
                if py is None: continue
                e = abs(py - cd[2]/100.0)
                if e < EDGE: continue
                sy = py > cd[2]/100.0
                if sy:
                    cost = cd[2]                       # yes_ask; bot gate [30,80]
                    if not (30 <= cost <= 80): continue
                else:
                    cost = 100 - cd[1]                 # no_ask; bot gate [10,80]
                    if not (10 <= cost <= 80): continue
                if best is None or e > best[0]:
                    won = (res=="yes") if sy else (res=="no")
                    best = (e, won)
            if best:
                agg[b]["w" if best[1] else "l"] += 1
                seen.add(b)
print("=== FIXED mu-replay (cur_lst_min price lookup + trace peak), HIGH, days >=5/15 ===")
for b in (">=1", "<1"):
    a = agg[b]; n = a["w"]+a["l"]
    print("  h2pk %-4s : %dW/%dL  WR %s" % (b, a["w"], a["l"], ("%.0f%%" % (100*a["w"]/n)) if n else "-"))
print("  (faithful target: pre-peak >=1 = 58%, near/post <1 = 33%)")
