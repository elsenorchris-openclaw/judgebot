#!/usr/bin/env python3
"""Sweep uniform pre-peak HIGH windows (early-trim OFF) to find max PnL on the
67-day look-ahead-free data, vs CURRENT. Window = h2pk in [lo,hi]; one best-edge
bet per station/day at the earliest qualifying offset."""
import csv, gzip, importlib.util, math, sqlite3, datetime
from collections import defaultdict

DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ = "/home/ubuntu/data/phq_ext"
OVR = "/home/ubuntu/paper_judge_bot/push_window_overrides.py"
MIN_DAY = "2026-03-15"; MONTH = 5; EDGE = 0.12; TRIM_MAE = 1.6; H2PK_MIN = 0.5
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5, "AUS": -6, "DFW": -6,
      "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6, "DEN": -7,
      "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8}
spec = importlib.util.spec_from_file_location("ov", OVR); ov = importlib.util.module_from_spec(spec); spec.loader.exec_module(ov)
# candidate windows: (label, lo_h2pk, hi_h2pk) -- early-trim OFF
CANDS = [("CURRENT", None, None), ("h1.0-2.5", 1.0, 2.5), ("h1.5-2.5", 1.5, 2.5),
         ("h1.0-3.0", 1.0, 3.0), ("h1.5-3.0", 1.5, 3.0), ("h1.0-2.0", 1.0, 2.0),
         ("h2.0-3.0", 2.0, 3.0), ("h1.5-4.0", 1.5, 4.0)]


def phi(x): return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def p_yes(fl, cp, mu, sig):
    if not sig or sig <= 0: sig = 1.5
    if fl is not None and cp is not None: return phi((cp + .5 - mu) / sig) - phi((fl - .5 - mu) / sig)
    if fl is not None: return 1.0 - phi((fl - .5 - mu) / sig)
    if cp is not None: return phi((cp + .5 - mu) / sig)
    return None


def cur_elig(h, bc, ac, mae):
    be = bc
    if mae is not None and mae < TRIM_MAE: be = min(be, max(1.0, 0.5 - ac))
    return max(H2PK_MIN, -ac) <= h <= be


conn = sqlite3.connect(DB, timeout=60); conn.execute("PRAGMA busy_timeout=60000")
tot = {c[0]: {"pnl": 0, "n": 0, "w": 0} for c in CANDS}
pstat = defaultdict(lambda: defaultdict(int))
for iata, off in sorted(ST.items()):
    if iata == "DCA": continue
    cw = ov.PUSH_WINDOW_OVERRIDES.get(("K" + iata, "HIGH", MONTH))
    if not cw: continue
    bc, ac = cw[0], cw[1]; mae = cw[3] if len(cw) >= 4 else None
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
    for date, br, fl, cp, res in conn.execute("SELECT climate_day,bracket,floor,cap,result FROM market_meta WHERE station=? AND side='HIGH' AND climate_day>=?", ("K" + iata, MIN_DAY)):
        if res in ("yes", "no"): brs[date].append((br, fl, cp, res))
    cand = defaultdict(list)
    for date, br, ts, yb, ya in conn.execute("SELECT climate_day,bracket,ts,yes_bid,yes_ask FROM candle_history WHERE station=? AND side='HIGH' AND climate_day>=?", ("K" + iata, MIN_DAY)):
        lst = datetime.datetime.utcfromtimestamp(ts + off * 3600)
        if lst.strftime("%Y-%m-%d") != date: continue
        cand[(date, br)].append((lst.hour * 60 + lst.minute, yb, ya))
    for k in cand: cand[k].sort()
    def price_at(date, br, t):
        seq = cand.get((date, br));
        if not seq: return None
        best = None
        for r in seq:
            if r[0] <= t and (best is None or r[0] > best[0]): best = r
        return best if (best and t - best[0] <= 60 and best[1] is not None and best[2] is not None and 0 < best[2] < 100) else None
    for date, rows in phq.items():
        if date not in brs: continue
        for lab, lo, hi in CANDS:
            placed = False
            for lst_min, offset, mu, sig in rows:
                if placed: break
                h = -offset
                ok = cur_elig(h, bc, ac, mae) if lab == "CURRENT" else (lo <= h <= hi)
                if not ok: continue
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
                    tot[lab]["pnl"] += best[2]; tot[lab]["n"] += 1; tot[lab]["w"] += 1 if best[1] else 0
                    pstat[iata][lab] += best[2]
                    placed = True
print("window      | trades win%  totPnL  /trade")
for lab, lo, hi in CANDS:
    t = tot[lab]; n = t["n"]
    print("%-11s | %5d  %3s  %+6d  %+5.1f" % (lab, n, ("%.0f%%" % (100 * t["w"] / n)) if n else "-", t["pnl"], (t["pnl"] / n) if n else 0))
print("\nper-station PnL  (CURRENT | h1.5-3.0 | h2.0-3.0):")
pos = sum(1 for s in pstat if pstat[s].get("h2.0-3.0", 0) > 0)
for s in sorted(pstat):
    p = pstat[s]
    print("  %-5s %+6d | %+6d | %+6d" % (s, p.get("CURRENT", 0), p.get("h1.5-3.0", 0), p.get("h2.0-3.0", 0)))
print("h2.0-3.0 positive on %d/%d stations" % (pos, len(pstat)))
