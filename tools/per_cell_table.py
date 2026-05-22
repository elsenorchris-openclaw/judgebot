#!/usr/bin/env python3
"""Per-(station,side) 30-min START-window table -- FAITHFUL recipe (validated:
mu-replay pre-peak 62% vs faithful 58%). Fixes vs v1:
  (1) apply the bot's asymmetric PRICE GATE (BUY_YES yes_ask in [30,80],
      BUY_NO no_ask in [10,80]) -- this was the missing ~20pp.
  (2) price lookup at the matcher's OWN per-row cur_lst_min (no averaged peak).
  (3) offset h2pk relative to the per-day TRACE peak (day_max/min_lst_min).
May-only (current trading month). Bot buys ONCE -> score the best-edge bet placed
at the START of each 30-min band; report the highest-WR start (n>=8) per cell.
Also dumps per-band detail to /tmp/per_cell_bands.csv for later analysis."""
import csv, gzip, math, sqlite3, datetime
from collections import defaultdict

DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ = "/home/ubuntu/data/per_hour_quality_offset_cond"   # 25yr per-date mu, has cur_lst_min
TRACES = "/home/ubuntu/data/heating_traces_ext.sqlite"
MONTH = "05"; MIN_DAY = "2023-01-01"; EDGE = 0.12
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5, "AUS": -6, "DFW": -6,
      "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6, "DEN": -7,
      "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8, "DCA": -5}
STARTS = [3.5, 3.0, 2.5, 2.0, 1.5, 1.0, 0.5]   # band = [S-0.5, S], buy at start (high h2pk edge)


def phi(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
def p_yes(fl, cp, mu, sig):
    if not sig or sig <= 0: sig = 1.5
    if fl is not None and cp is not None: return phi((cp+.5-mu)/sig) - phi((fl-.5-mu)/sig)
    if fl is not None: return 1.0 - phi((fl-.5-mu)/sig)
    if cp is not None: return phi((cp+.5-mu)/sig)
    return None


# per-day trace peaks (May)
tr = sqlite3.connect(TRACES)
peak = {}; pk_avg = defaultdict(list)
for st, dt, mx, mn in tr.execute("SELECT station,lst_date,day_max_lst_min,day_min_lst_min FROM station_days WHERE substr(lst_date,6,2)=? AND lst_date>=?", (MONTH, MIN_DAY)):
    peak[("K"+st, dt, "high")] = mx; peak[("K"+st, dt, "low")] = mn
    if mx is not None: pk_avg[("K"+st, "high")].append(mx)
    if mn is not None: pk_avg[("K"+st, "low")].append(mn)

conn = sqlite3.connect(DB, timeout=60); conn.execute("PRAGMA busy_timeout=60000")
rows_out = []; band_csv = [("station", "side", "band_start", "band_end", "wr", "n", "pnl", "edge", "cost")]
for iata, off in sorted(ST.items()):
    if iata == "DCA":
        rows_out.append((iata, "BOTH", None, "—", "-", 0, "-", "-", "-", "excl: sparse feed")); continue
    for side_l, side_db in (("high", "HIGH"), ("low", "LOW")):
        phq = defaultdict(list)   # date -> [(cur_lst_min, mu, sig)]
        try:
            with gzip.open("%s/phq_raw_%s.csv.gz" % (PHQ, iata), "rt") as f:
                for r in csv.DictReader(f):
                    if r["side"] != side_l or not r.get("mu_proj_f"): continue
                    if r["date"] < MIN_DAY or r["date"][5:7] != MONTH: continue
                    try: phq[r["date"]].append((float(r["cur_lst_min"]), float(r["mu_proj_f"]), float(r["sigma_proj_f"] or 1.5)))
                    except (ValueError, TypeError): continue
        except FileNotFoundError:
            rows_out.append((iata, side_db, None, "—", "-", 0, "-", "-", "-", "no phq")); continue
        brs = defaultdict(list)
        for date, br, fl, cp, res in conn.execute("SELECT climate_day,bracket,floor,cap,result FROM market_meta WHERE station=? AND side=? AND substr(climate_day,6,2)=? AND climate_day>=?", ("K"+iata, side_db, MONTH, MIN_DAY)):
            if res in ("yes", "no"): brs[date].append((br, fl, cp, res))
        cand = defaultdict(list)
        for date, br, ts, yb, ya in conn.execute("SELECT climate_day,bracket,ts,yes_bid,yes_ask FROM candle_history WHERE station=? AND side=? AND substr(climate_day,6,2)=? AND climate_day>=?", ("K"+iata, side_db, MONTH, MIN_DAY)):
            lst = datetime.datetime.utcfromtimestamp(ts + off*3600)
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
        band = {}
        for S in STARTS:
            w = l = 0; pnls = []; edges = []; costs = []
            for date, prows in phq.items():
                if date not in brs: continue
                pk = peak.get(("K"+iata, date, side_l))
                if pk is None: continue
                pick = None
                for clm, mu, sig in sorted(prows, key=lambda x: -(pk - x[0])):  # highest h2pk first
                    h2 = (pk - clm) / 60.0
                    if not (S-0.5 <= h2 < S): continue
                    best = None
                    for br, fl, cp, res in brs[date]:
                        cd = price_at(date, br, clm)
                        if not cd: continue
                        py = p_yes(fl, cp, mu, sig)
                        if py is None: continue
                        e = abs(py - cd[2]/100.0)
                        if e < EDGE: continue
                        sy = py > cd[2]/100.0
                        if sy:
                            cost = cd[2]
                            if not (30 <= cost <= 80): continue
                        else:
                            cost = 100 - cd[1]
                            if not (10 <= cost <= 80): continue
                        if best is None or e > best[0]:
                            won = (res == "yes") if sy else (res == "no")
                            best = (e, won, (100-cost) if won else -cost, cost)
                    if best: pick = best; break
                if pick:
                    w += 1 if pick[1] else 0; l += 0 if pick[1] else 1
                    pnls.append(pick[2]); edges.append(pick[0]); costs.append(pick[3])
            n = w + l
            if n >= 1:
                wr = 100*w/n
                band[S] = (wr, n, sum(pnls)/n, sum(edges)/len(edges)*100, sum(costs)/len(costs))
                band_csv.append((iata, side_db, "%.1f" % S, "%.1f" % (S-0.5), round(wr), n, round(sum(pnls)/n, 1), round(sum(edges)/len(edges)*100), round(sum(costs)/len(costs))))
        elig = {s: v for s, v in band.items() if v[1] >= 8}
        bestS = max(elig, key=lambda s: elig[s][0]) if elig else (max(band, key=lambda s: band[s][1]) if band else None)
        pkv = (sum(pk_avg[("K"+iata, side_l)])/len(pk_avg[("K"+iata, side_l)])/60.0) if pk_avg.get(("K"+iata, side_l)) else None
        if bestS is not None:
            wr, n, pnl, ae, cost = band[bestS]
            flag = "" if band[bestS][1] >= 8 else "THIN(n<8)"
            rows_out.append((iata, side_db, pkv, "[pk-%.1f,pk-%.1f]" % (bestS, bestS-0.5), round(wr), n, round(pnl, 1), round(ae), round(cost), flag))
        else:
            rows_out.append((iata, side_db, pkv, "—", "-", 0, "-", "-", "-", "no data"))

with open("/tmp/per_cell_bands.csv", "w", newline="") as f:
    csv.writer(f).writerows(band_csv)
print("PER-CELL best 30-min START window (FAITHFUL recipe; price-gated mu-replay validated 62% vs 58%)")
print("May only, 2023+. WR/PnL meaningful where n>=8; THIN flagged. Per-band detail -> /tmp/per_cell_bands.csv")
print("%-5s %-4s %-7s %-16s %4s %4s %7s %5s %5s %s" % ("stn", "side", "peakLST", "best 30m win", "WR%", "n", "pnl/bet", "edge", "cost", "flag"))
for r in rows_out:
    pk = ("%.2f" % r[2]) if isinstance(r[2], float) else "?"
    print("%-5s %-4s %-7s %-16s %4s %4s %7s %5s %5s %s" % (r[0], r[1], pk, r[3], r[4], r[5], r[6], r[7], r[8], r[9] if len(r) > 9 else ""))
