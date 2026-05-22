#!/usr/bin/env python3
"""67-day per-station preview: matcher-realized PnL by offset, using REAL live
prices (>=3/15, complete + stable) x extended μ (phq_ext) x settlement. One
best-edge bet per offset (mimics the bot's single pick). Restricted to the live
window so the in-progress historical backfill can't contaminate it."""
import csv
import gzip
import math
import sqlite3
from collections import defaultdict

BACKFILL_DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ_DIR = "/home/ubuntu/data/phq_ext"
MIN_DAY = "2026-03-15"
EDGE_FLOOR = 0.12
# station -> LST utc-offset (standard time, no DST)
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5,
      "AUS": -6, "DFW": -6, "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6,
      "DEN": -7, "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8}

BUCKETS = [(2, 99, ">=2"), (1.5, 2, "1.5-2"), (1.0, 1.5, "1-1.5"),
           (0.5, 1.0, ".5-1"), (0.0, 0.5, "0-.5"), (-0.5, 0, "-.5-0"), (-99, -0.5, "<-.5")]


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def p_yes(fl, cp, mu, sig):
    if not sig or sig <= 0:
        sig = 1.5
    if fl is not None and cp is not None:
        return phi((cp + 0.5 - mu) / sig) - phi((fl - 0.5 - mu) / sig)
    if fl is not None:
        return 1.0 - phi((fl - 0.5 - mu) / sig)
    if cp is not None:
        return phi((cp + 0.5 - mu) / sig)
    return None


def bk(h):
    for lo, hi, lab in BUCKETS:
        if lo <= h < hi:
            return lab
    return "?"


def at_or_before(seq, t):
    """Latest candle with lst_min <= t (price available at decision time, no
    look-ahead), within 60 min before."""
    best = None
    for r in seq:
        if r[0] <= t and (best is None or r[0] > best[0]):
            best = r
    if best is not None and (t - best[0]) <= 60:
        return best
    return None


def analyze(iata, lst_off, conn):
    phq = defaultdict(list)
    try:
        with gzip.open("%s/phq_raw_%s.csv.gz" % (PHQ_DIR, iata), "rt") as f:
            for r in csv.DictReader(f):
                if r["side"] != "high" or not r.get("mu_proj_f") or r["date"] < MIN_DAY:
                    continue
                try:
                    phq[r["date"]].append((float(r["cur_lst_min"]), float(r["offset"]),
                                           float(r["mu_proj_f"]), float(r["sigma_proj_f"] or 1.5)))
                except (ValueError, TypeError):
                    continue
    except FileNotFoundError:
        return None
    brs = defaultdict(list)
    for date, br, fl, cp, res in conn.execute(
            "SELECT climate_day,bracket,floor,cap,result FROM market_meta WHERE station=? AND side='HIGH' AND climate_day>=?",
            ("K" + iata, MIN_DAY)):
        if res in ("yes", "no"):
            brs[date].append((br, fl, cp, res))
    import datetime as _dt
    cand = defaultdict(list)
    for date, br, ts, yb, ya in conn.execute(
            "SELECT climate_day,bracket,ts,yes_bid,yes_ask FROM candle_history WHERE station=? AND side='HIGH' AND climate_day>=?",
            ("K" + iata, MIN_DAY)):
        # only candles ON the climate day (LST), so minute-of-day matching can't
        # grab a different day of a multi-day market
        lst = _dt.datetime.utcfromtimestamp(ts + lst_off * 3600)
        if lst.strftime("%Y-%m-%d") != date:
            continue
        cand[(date, br)].append((lst.hour * 60 + lst.minute, yb, ya))
    for k in cand:
        cand[k].sort()
    agg = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
    for date, rows in phq.items():
        if date not in brs:
            continue
        for lst_min, offset, mu, sig in rows:
            best = None
            for br, fl, cp, res in brs[date]:
                seq = cand.get((date, br))
                if not seq:
                    continue
                cd = at_or_before(seq, lst_min)
                if not cd or cd[2] is None or cd[1] is None or cd[2] <= 0 or cd[2] >= 100:
                    continue
                py = p_yes(fl, cp, mu, sig)
                if py is None:
                    continue
                edge = abs(py - cd[2] / 100.0)
                if edge < EDGE_FLOOR:
                    continue
                side_yes = py > cd[2] / 100.0
                cost = cd[2] if side_yes else (100 - cd[1])
                if cost <= 0 or cost >= 100:
                    continue
                if best is None or edge > best[0]:
                    won = (res == "yes") if side_yes else (res == "no")
                    best = (edge, won, (100 - cost) if won else -cost)
            if best:
                a = agg[bk(-offset)]
                a["w" if best[1] else "l"] += 1
                a["pnl"] += best[2]
    return agg


def main():
    conn = sqlite3.connect(BACKFILL_DB, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    print("station | " + " ".join("%6s" % b[2] for b in BUCKETS) + " | PRE>=1  NEAR<1  verdict")
    print("        | " + " ".join("%6s" % "pnl/bet" for _ in BUCKETS))
    for iata in sorted(ST):
        if iata == "DCA":
            print("%-7s | (excluded: μ coverage stops 1/15, sparse ASOS feed)" % iata)
            continue
        agg = analyze(iata, ST[iata], conn)
        if not agg:
            print("%-7s | (no data)" % iata); continue
        cells = []
        for lo, hi, lab in BUCKETS:
            a = agg.get(lab)
            n = (a["w"] + a["l"]) if a else 0
            cells.append("%+5.1f" % (a["pnl"] / n) if n else "   - ")
        # pre-peak (h2pk>=1) vs near/post (<1)
        pre = {"pnl": 0, "n": 0}; near = {"pnl": 0, "n": 0}
        for lo, hi, lab in BUCKETS:
            a = agg.get(lab)
            if not a:
                continue
            n = a["w"] + a["l"]
            (pre if lo >= 1.0 else near)["pnl"] += a["pnl"]
            (pre if lo >= 1.0 else near)["n"] += n
        prebet = pre["pnl"] / pre["n"] if pre["n"] else 0
        nearbet = near["pnl"] / near["n"] if near["n"] else 0
        verdict = "EDGE pre-peak" if prebet > 1.5 and pre["n"] >= 20 else (
            "weak/none" if max(prebet, nearbet) < 1.5 else "near-peak?")
        print("%-7s | %s | %+5.1f(%d) %+5.1f(%d)  %s" % (
            iata, " ".join("%6s" % c for c in cells), prebet, pre["n"], nearbet, near["n"], verdict))


if __name__ == "__main__":
    main()
