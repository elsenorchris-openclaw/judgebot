#!/usr/bin/env python3
"""Find the best buy-time (offset) window per station for the nn matcher, using
REAL historical prices. Joins, per (station, date, offset):
  - matcher mu/sigma  (v3 sidecar phq_raw_<ST>.csv.gz; CURRENT matcher replayed)
  - market BBO        (candle_history, backfilled from Kalshi candlesticks)
  - settlement        (market_meta.result)
Replays the bot's decision (edge >= 12pp -> bet the side mu favors) at each
offset and tallies realized PnL. Output: PnL/win-rate by offset bucket -> the
profit-optimal window. Matcher-independent prices over ~30 days = robust, and
sidesteps the contaminated-3-day problem.

Usage: python3 window_analysis.py PHX -7   (station IATA, LST utc-offset hours)
"""
import csv
import gzip
import math
import sqlite3
import sys
from collections import defaultdict

BACKFILL_DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ_DIR = "/home/ubuntu/data/per_hour_quality_offset_cond"  # overridden by argv[3]
EDGE_FLOOR = 0.12
SIDE = "high"
SERIES_PREFIX = "KXHIGH"

st_iata = sys.argv[1]
lst_off = int(sys.argv[2])
PHQ_DIR_ARG = sys.argv[3] if len(sys.argv) > 3 else None


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def p_yes(floor, cap, mu, sig):
    if sig is None or sig <= 0:
        sig = 1.5
    if floor is not None and cap is not None:        # B bracket [floor, cap]
        return phi((cap + 0.5 - mu) / sig) - phi((floor - 0.5 - mu) / sig)
    if floor is not None:                            # T high: >= floor
        return 1.0 - phi((floor - 0.5 - mu) / sig)
    if cap is not None:                              # T low: <= cap
        return phi((cap + 0.5 - mu) / sig)
    return None


# 1) matcher mu/sigma per (date, nearest cur_lst_min) -> offset
phq = defaultdict(list)   # date -> [(lst_min, offset, mu, sigma)]
_phq_dir = PHQ_DIR_ARG or PHQ_DIR
with gzip.open("%s/phq_raw_%s.csv.gz" % (_phq_dir, st_iata), "rt") as f:
    for r in csv.DictReader(f):
        if r["side"] != SIDE or not r.get("mu_proj_f"):
            continue
        if r["date"] < "2026-04-15":
            continue
        try:
            phq[r["date"]].append((float(r["cur_lst_min"]), float(r["offset"]),
                                   float(r["mu_proj_f"]),
                                   float(r["sigma_proj_f"] or 1.5)))
        except (ValueError, TypeError):
            continue
for d in phq:
    phq[d].sort()
print("phq dates (>=4/15): %d  range %s..%s" % (
    len(phq), min(phq) if phq else "-", max(phq) if phq else "-"))

# 2) candles + meta
conn = sqlite3.connect(BACKFILL_DB)
meta = {}   # (date, bracket) -> (floor, cap, result)
for date, bracket, fl, cp, res in conn.execute(
        "SELECT climate_day, bracket, floor, cap, result FROM market_meta WHERE station=? AND side=?",
        ("K" + st_iata, "HIGH")):
    meta[(date, bracket)] = (fl, cp, res)
candles = defaultdict(list)   # (date, bracket) -> [(lst_min, yes_bid, yes_ask)]
for date, bracket, ts, yb, ya in conn.execute(
        "SELECT climate_day, bracket, ts, yes_bid, yes_ask FROM candle_history WHERE station=? AND side=?",
        ("K" + st_iata, "HIGH")):
    lst_min = ((ts + lst_off * 3600) % 86400) / 60.0
    candles[(date, bracket)].append((lst_min, yb, ya))
for k in candles:
    candles[k].sort()
print("candle (date,bracket) series: %d  meta markets: %d" % (len(candles), len(meta)))


def nearest(seq, t, idx):
    best = None
    for row in seq:
        if best is None or abs(row[0] - t) < abs(best[0] - t):
            best = row
        elif row[0] > t and abs(row[0] - t) > abs(best[0] - t):
            break
    return best


# 3) replay bets by offset
buckets = [(2, 99, ">=2.0"), (1.5, 2, "1.5-2.0"), (1.0, 1.5, "1.0-1.5"),
           (0.5, 1.0, "0.5-1.0"), (0.0, 0.5, "0-0.5"), (-0.5, 0, "-0.5-0"),
           (-99, -0.5, "<-0.5")]


def bk(h):
    for lo, hi, lab in buckets:
        if lo <= h < hi:
            return lab
    return "?"


brackets_by_date = defaultdict(list)   # date -> [(bracket, fl, cp, res)]
for (date2, bracket), (fl, cp, res) in meta.items():
    if res in ("yes", "no"):
        brackets_by_date[date2].append((bracket, fl, cp, res))

agg = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0, "n": 0})
soft = defaultdict(lambda: {"px": [], "n": 0})   # winning-side price by offset
for date, rows in phq.items():
    brs = brackets_by_date.get(date)
    if not brs:
        continue
    for lst_min, offset, mu, sig in rows:
        h2pk = -offset
        best = None     # the bot picks ONE bet per offset: highest edge
        for bracket, fl, cp, res in brs:
            cseq = candles.get((date, bracket))
            if not cseq:
                continue
            cd = nearest(cseq, lst_min, 0)
            if not cd or abs(cd[0] - lst_min) > 20:
                continue
            yb, ya = cd[1], cd[2]
            if ya is None or yb is None or ya <= 0 or ya >= 100:
                continue
            py = p_yes(fl, cp, mu, sig)
            if py is None:
                continue
            mkt_py = ya / 100.0
            # softness: cost of eventually-winning side (all brackets)
            sb = soft[bk(h2pk)]
            sb["px"].append(ya if res == "yes" else (100 - yb))
            sb["n"] += 1
            edge = abs(py - mkt_py)
            if edge < EDGE_FLOOR:
                continue
            side = "BUY_YES" if py > mkt_py else "BUY_NO"
            cost = ya if side == "BUY_YES" else (100 - yb)
            if cost <= 0 or cost >= 100:
                continue
            if best is None or edge > best[0]:
                won = (res == "yes") if side == "BUY_YES" else (res == "no")
                best = (edge, won, (100 - cost) if won else -cost)
        if best:
            a = agg[bk(h2pk)]
            a["w" if best[1] else "l"] += 1
            a["pnl"] += best[2]
            a["n"] += 1

print("\n=== %s HIGH: matcher-realized PnL by offset (edge>=12pp, real prices) ===" % st_iata)
print("h_to_peak   bets  win%  totPnL(c)  PnL/bet | winning-side avg cost (softness)")
for _, _, lab in buckets:
    a = agg.get(lab)
    s = soft.get(lab)
    softc = (sum(s["px"]) / len(s["px"])) if s and s["px"] else None
    if not a or a["n"] == 0:
        print("%-10s   (no bets)                              | soft=%s" % (
            lab, ("%.0fc n=%d" % (softc, s["n"]) if softc else "-")))
        continue
    n = a["n"]
    print("%-10s  %4d  %3.0f%%  %+7d   %+6.1f | %s" % (
        lab, n, 100 * a["w"] / n, a["pnl"], a["pnl"] / n,
        ("%.0fc n=%d" % (softc, s["n"]) if softc else "-")))
