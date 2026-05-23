#!/usr/bin/env python3
"""Per-station + POOLED-GLOBAL LOW BUY_NO window sweep. Mirrors the validated HIGH
window_sweep_station.py exactly (same faithful gated buy-at-open sim, same EDGE/
price gate, same early/late split robustness flag) so results are directly
comparable and the emitted (before, after) maps 1:1 into PUSH_LOW_TEMP_WINDOW*.

Differences from HIGH:
  - side='low' / 'LOW'; MIN_DAY lowered to 2026-02-18 to capture the Feb18-Mar21
    winter overlap (phq_ext starts 02-18; the 7 winter LOW candle stations end
    03-21) the prior 03-15 cutoff discarded. PHX adds Apr15-May19 (spring).
  - CANDS = near-min / close-before-min grid (the LOW edge is pre-min and DIES
    past the min: pooled NO past-min = -3.9c; the HIGH deep-pre window shape does
    NOT apply). Sign: open_h/close_h are bounds on h=hours-to-min; window is
    close_h <= h < open_h; config (before,after) = (open_h, -close_h).
  - Emits a recommended pooled-GLOBAL window + per-station dict; stations whose own
    best is negative/thin inherit the pooled-global (data-thin LOW => global first).
"""
import csv, gzip, math, sqlite3, datetime
from collections import defaultdict

DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ = "/home/ubuntu/data/phq_ext"
MIN_DAY = "2026-02-18"; EDGE = 0.12; GLO, GHI = 10, 80
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5, "AUS": -6, "DFW": -6,
      "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6, "DEN": -7,
      "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8}
# near-min grid: open_h (deep edge, hrs before min), close_h (near edge; >0 closes
# before min, <0 closes after). entry ~ close edge, so close_h drives the bet.
OPENS = [2.0, 1.5, 1.0, 0.5]
CLOSES = [1.0, 0.5, 0.0, -0.5, -1.0, -1.5]
CANDS = [(o, c) for o in OPENS for c in CLOSES if o - c >= 0.5 and o > c]


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
    if phq and brs and cand: STORE[iata] = (phq, brs, cand)

ALL_DATES = sorted({d for iata in STORE for d in STORE[iata][0]})
MED = ALL_DATES[len(ALL_DATES)//2]


def price_at(cand, date, br, t):
    seq = cand.get((date, br)); b = None
    if not seq: return None
    for r in seq:
        if r[0] <= t and (b is None or r[0] > b[0]): b = r
    return b if (b and t-b[0] <= 60 and b[1] is not None and b[2] is not None and 0 < b[2] < 100) else None


def sim(iata, open_h, close_h):
    phq, brs, cand = STORE[iata]; bets = []
    for date, rows in phq.items():
        if date not in brs: continue
        placed = None
        for lst_min, offset, mu, sig in rows:
            h = -offset
            if not (close_h <= h < open_h): continue
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


def stat(sub):
    n = len(sub)
    if not n: return (0, 0.0, 0.0)
    return (n, 100*sum(1 for b in sub if b[0])/n, sum(b[1] for b in sub)/n)


def split(b):
    ne, _, epnl = stat([x for x in b if x[2] < MED]); nl, _, lpnl = stat([x for x in b if x[2] >= MED])
    return epnl, ne, lpnl, nl

# ---- POOLED-GLOBAL sweep (all stations combined) ----
print("LOW BUY_NO window sweep | MIN_DAY=%s | %d stations w/ data | split @ %s\n" % (MIN_DAY, len(STORE), MED))
print("=== POOLED-GLOBAL: every candidate window ===")
print(" window(before,after)   full   n   pnl/bet | early(n)  late(n)  flag")
glob_best = None
for oh, ch in CANDS:
    allb = []
    for iata in STORE: allb += sim(iata, oh, ch)
    n, wr, pnl = stat(allb)
    if n < 20: continue
    epnl, ne, lpnl, nl = split(allb)
    flag = "ROBUST" if (epnl > 0 and lpnl > 0) else "soft"
    print("  (%.1f,%+.1f)  [min-%.1f,min%+.1f]  %2.0f%% %3d %+6.2fc | %+5.1f(%d) %+5.1f(%d)  %s" % (
        oh, -ch, oh, -ch, wr, n, pnl, epnl, ne, lpnl, nl, flag))
    if glob_best is None or pnl > glob_best[0]:
        glob_best = (pnl, oh, ch, n, wr, epnl, lpnl, flag)
gpnl, goh, gch, gn, gwr, gep, glp, gflag = glob_best
print("\n  >>> POOLED-GLOBAL BEST: (before=%.1f, after=%+.1f) = [min-%.1f, min%+.1f]  %+.2fc/bet n=%d %2.0f%% [%s]"
      % (goh, -gch, goh, -gch, gpnl, gn, gwr, gflag))
GLOBAL = (goh, -gch)

# ---- per-station sweep ----
print("\n=== per-station best window (own data) ===")
chosen = {}
for iata in sorted(STORE):
    best = None
    for oh, ch in CANDS:
        b = sim(iata, oh, ch); n, wr, pnl = stat(b)
        if n < 8: continue
        epnl, ne, lpnl, nl = split(b)
        if best is None or pnl > best[0]:
            best = (pnl, oh, ch, n, wr, epnl, lpnl, ne, nl)
    if best is None:
        print("  %-5s  (no window n>=8) -> global %s" % (iata, GLOBAL)); chosen[iata] = (GLOBAL[0], GLOBAL[1], "thin->global"); continue
    pnl, oh, ch, n, wr, epnl, lpnl, ne, nl = best
    flag = "ROBUST" if (epnl > 0 and lpnl > 0 and n >= 15) else ("THIN(n<15)" if n < 15 else "SOFT(1half-)")
    note = flag
    use = (oh, -ch)
    if pnl <= 0:
        use = GLOBAL; note = "neg-best->global"
    chosen[iata] = (use[0], use[1], note)
    print("  %-5s best (%.1f,%+.1f)=[min-%.1f,min%+.1f] full %2.0f%% n=%-3d %+6.2fc | early %+5.1f(%d) late %+5.1f(%d)  %s%s" % (
        iata, oh, -ch, oh, -ch, wr, n, pnl, epnl, ne, lpnl, nl, flag, "  -> use GLOBAL (neg)" if pnl <= 0 else ""))

print("\n# recommended (data-thin LOW): SHIP the pooled-global as PUSH_LOW_TEMP_WINDOW;")
print("# only the ROBUST per-station cells below are candidates for a BY_STATION override.")
print("PUSH_LOW_TEMP_WINDOW = (%.1f, %.1f)   # pooled-global best" % (GLOBAL[0], GLOBAL[1]))
print("PUSH_LOW_TEMP_WINDOW_BY_STATION = {")
for iata in sorted(chosen):
    oh, aft, note = chosen[iata]
    tag = "  <-- ROBUST own" if note == "ROBUST" else ""
    print('    "K%s": (%.1f, %.1f),  # %s%s' % (iata, oh, aft, note, tag))
print("}")
