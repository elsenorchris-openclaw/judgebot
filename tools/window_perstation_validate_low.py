#!/usr/bin/env python3
"""PER-STATION vs GLOBAL LOW window — honest out-of-sample test.

Each station's data is split train/test at its own median date. We pick windows
on TRAIN (per-station's own argmax AND a single pooled-global argmax), then score
both on TEST. If per-station selection beats global on TEST, per-station
generalizes; if global wins, the per-station peaks are overfit to thin data.

Windows are 30-min, buy-at-START (bot pins entry to the open edge), so a window
opening at offset h0 == buying at instant h0 (phq native 0.5h grid). Reuses the
per-offset bet machinery."""
import csv, gzip, math, sqlite3, datetime
from collections import defaultdict

DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ = "/home/ubuntu/data/phq_ext"
MIN_DAY = "2026-02-18"; EDGE = 0.12; GLO, GHI = 10, 80
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5, "AUS": -6, "DFW": -6,
      "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6, "DEN": -7,
      "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8}
HS = [2.5, 2.0, 1.5, 1.0, 0.5, 0.0]   # candidate window-open offsets
MIN_TRAIN, MIN_TEST = 5, 3


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
    phq = defaultdict(dict)
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


def price_at(cand, date, br, t):
    seq = cand.get((date, br)); b = None
    if not seq: return None
    for r in seq:
        if r[0] <= t and (b is None or r[0] > b[0]): b = r
    return b if (b and t-b[0] <= 60 and b[1] is not None and b[2] is not None and 0 < b[2] < 100) else None


def bets(iata, h):
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
                best = (e, (100-cost) if res == "no" else -cost)
        if best: out.append((best[1], date))
    return out

# precompute raw bets, then split at the median of ACTUAL bet dates (phq spans
# Feb-May but winter stations' candles/bets end Mar21 -> must split on bet dates).
RAW = {iata: {h: bets(iata, h) for h in HS} for iata in STORE}
SPLIT = {}
for iata in STORE:
    bdates = sorted({date for h in HS for _, date in RAW[iata][h]})
    SPLIT[iata] = bdates[len(bdates)//2] if bdates else "9999-99-99"
B = {iata: {} for iata in STORE}
for iata in STORE:
    for h in HS:
        tr = [p for p, d in RAW[iata][h] if d < SPLIT[iata]]
        te = [p for p, d in RAW[iata][h] if d >= SPLIT[iata]]
        B[iata][h] = (tr, te)


def mean(x): return sum(x)/len(x) if x else 0.0

# GLOBAL train pick: pooled train pnl per h
glob_train = {}
for h in HS:
    alltr = []
    for iata in STORE: alltr += B[iata][h][0]
    glob_train[h] = (mean(alltr), len(alltr))
gh = max(HS, key=lambda h: glob_train[h][0] if glob_train[h][1] >= 15 else -999)
print("Pooled TRAIN best window-open offset = +%.1fh (train %.2fc/n%d)\n" % (gh, glob_train[gh][0], glob_train[gh][1]))

print("=== per-station: TRAIN-pick vs GLOBAL, scored on TEST (out-of-sample) ===")
print(" stn  train-best  trainPnL(n) | TEST@own  TEST@global(+%.1fh)  delta(own-glob)" % gh)
sum_own_test = sum_glob_test = 0.0; n_eval = 0
rows = []
for iata in sorted(STORE):
    # own train pick (require enough train n)
    cands = [(h, mean(B[iata][h][0]), len(B[iata][h][0])) for h in HS if len(B[iata][h][0]) >= MIN_TRAIN]
    if not cands:
        print("  %-4s  (insufficient train)" % iata); continue
    bh, btr, btrn = max(cands, key=lambda x: x[1])
    own_test = B[iata][bh][1]; glob_test = B[iata][gh][1]
    if len(own_test) < MIN_TEST or len(glob_test) < MIN_TEST:
        print("  %-4s  +%.1fh  %+5.1fc(%d) | (insufficient test: own n=%d glob n=%d)" % (
            iata, bh, btr, btrn, len(own_test), len(glob_test))); continue
    ot, gt = mean(own_test), mean(glob_test)
    sum_own_test += ot * len(own_test); sum_glob_test += gt * len(glob_test); n_eval += 1
    rows.append((iata, bh, btr, btrn, ot, len(own_test), gt, len(glob_test)))
    print("  %-4s  +%.1fh  %+5.1fc(%d) | %+6.1f(%d)  %+6.1f(%d)         %+6.1f" % (
        iata, bh, btr, btrn, ot, len(own_test), gt, len(glob_test), ot - gt))

print("\n=== STRATEGY COMPARISON on TEST (weighted mean pnl/bet) ===")
# eligible stations = those with a valid train pick AND enough test at own + every compared offset
def agg(pick_fn):
    """pick_fn(iata)->offset h. weighted-mean test pnl over stations w/ test n>=MIN_TEST at that h."""
    s = nn = 0
    for iata in STORE:
        h = pick_fn(iata)
        te = B[iata][h][1]
        if len(te) < MIN_TEST: continue
        s += sum(te); nn += len(te)
    return (s/nn if nn else 0.0, nn)

def train_argmax(iata):
    c = [(h, mean(B[iata][h][0]), len(B[iata][h][0])) for h in HS if len(B[iata][h][0]) >= MIN_TRAIN]
    return max(c, key=lambda x: x[1])[0] if c else 0.0

ps = agg(train_argmax)
g00 = agg(lambda i: 0.0); g05 = agg(lambda i: 0.5); g15 = agg(lambda i: 1.5)
# hybrid: near-min global, but AUS uses its (deep) train pick
hyb = agg(lambda i: train_argmax(i) if i == "AUS" else 0.0)
print("  PER-STATION (own train argmax) : %+.2fc/bet (n=%d)" % ps)
print("  GLOBAL @ +0.0h (at min)        : %+.2fc/bet (n=%d)" % g00)
print("  GLOBAL @ +0.5h (near-min)      : %+.2fc/bet (n=%d)" % g05)
print("  GLOBAL @ +1.5h (winter-peak)   : %+.2fc/bet (n=%d)" % g15)
print("  HYBRID near-min + AUS-deep     : %+.2fc/bet (n=%d)" % hyb)
