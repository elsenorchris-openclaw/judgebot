#!/usr/bin/env python3
"""Head-to-head: does a pre-peak temp window beat the CURRENT live windows on the
67-day (>=3/15) look-ahead-free data? Simulates the bot's bet (one best-edge bet
per station/day, earliest qualifying offset in the effective window) under each
window definition and tallies realized PnL from settlement.

Definitions:
  CURRENT  = push_window_overrides HIGH + early-trim (cap before<=max(1,0.5-after)
             when mae<1.6) + h2pk gate (h2pk>=0.5)
  P1       = [peak-1.5, peak-1.0]  (before=1.5: NOT truncated by early-trim)
  P2       = [peak-2.5, peak-1.0]  (before=2.5: needs early-trim relaxed)
  P2nt     = P2 ignoring early-trim (what shipping P2 + raising the trim cap gives)
"""
import csv
import gzip
import importlib.util
import math
import sqlite3
import datetime
from collections import defaultdict

BACKFILL_DB = "/home/ubuntu/data/market_history_backfill.sqlite"
PHQ_DIR = "/home/ubuntu/data/phq_ext"
OVR = "/home/ubuntu/paper_judge_bot/push_window_overrides.py"
MIN_DAY = "2026-03-15"
MONTH = 5
EDGE = 0.12
TRIM_MAE = 1.6
H2PK_MIN = 0.5
ST = {"ATL": -5, "BOS": -5, "MIA": -5, "NYC": -5, "PHL": -5,
      "AUS": -6, "DFW": -6, "HOU": -6, "MDW": -6, "MSP": -6, "MSY": -6, "OKC": -6, "SAT": -6,
      "DEN": -7, "PHX": -7, "LAX": -8, "SEA": -8, "SFO": -8, "LAS": -8}

spec = importlib.util.spec_from_file_location("ov", OVR)
ov = importlib.util.module_from_spec(spec); spec.loader.exec_module(ov)


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


def eligible(h2pk, defn, before_cur, after_cur, mae):
    """Is this h2pk in the effective window for the definition?"""
    if defn == "CURRENT":
        be = before_cur
        if mae is not None and mae < TRIM_MAE:
            be = min(be, max(1.0, 0.5 - after_cur))
        # window h2pk in [-after_cur, be]; h2pk gate >= H2PK_MIN
        return max(H2PK_MIN, -after_cur) <= h2pk <= be
    if defn == "P1":
        return 1.0 <= h2pk <= 1.5
    if defn == "P2":   # with early-trim still active -> before capped to 1.5
        return 1.0 <= h2pk <= 1.5
    if defn == "P2nt":  # P2 with trim relaxed -> full pre-peak
        return 1.0 <= h2pk <= 2.5
    return False


def run():
    conn = sqlite3.connect(BACKFILL_DB, timeout=60)
    conn.execute("PRAGMA busy_timeout=60000")
    defs = ["CURRENT", "P1", "P2", "P2nt"]
    tot = {d: {"pnl": 0, "n": 0, "w": 0} for d in defs}
    per_station = defaultdict(lambda: {d: 0 for d in defs})
    for iata, lst_off in sorted(ST.items()):
        if iata == "DCA":
            continue
        cw = ov.PUSH_WINDOW_OVERRIDES.get(("K" + iata, "HIGH", MONTH))
        if not cw:
            continue
        before_cur, after_cur = cw[0], cw[1]
        mae = cw[3] if len(cw) >= 4 else None
        # phq
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
            continue
        for d in phq:
            phq[d].sort(key=lambda x: -x[1])   # earliest first = most negative offset = highest h2pk
        brs = defaultdict(list)
        for date, br, fl, cp, res in conn.execute(
                "SELECT climate_day,bracket,floor,cap,result FROM market_meta WHERE station=? AND side='HIGH' AND climate_day>=?",
                ("K" + iata, MIN_DAY)):
            if res in ("yes", "no"):
                brs[date].append((br, fl, cp, res))
        cand = defaultdict(list)
        for date, br, ts, yb, ya in conn.execute(
                "SELECT climate_day,bracket,ts,yes_bid,yes_ask FROM candle_history WHERE station=? AND side='HIGH' AND climate_day>=?",
                ("K" + iata, MIN_DAY)):
            lst = datetime.datetime.utcfromtimestamp(ts + lst_off * 3600)
            if lst.strftime("%Y-%m-%d") != date:
                continue
            cand[(date, br)].append((lst.hour * 60 + lst.minute, yb, ya))
        for k in cand:
            cand[k].sort()

        def price_at(date, br, t):
            seq = cand.get((date, br))
            if not seq:
                return None
            best = None
            for r in seq:
                if r[0] <= t and (best is None or r[0] > best[0]):
                    best = r
            if best and (t - best[0]) <= 60 and best[1] is not None and best[2] is not None and 0 < best[2] < 100:
                return best
            return None

        for date, rows in phq.items():
            if date not in brs:
                continue
            for defn in defs:
                placed = False
                for lst_min, offset, mu, sig in rows:   # earliest (highest h2pk) first
                    if placed:
                        break
                    h2pk = -offset
                    if not eligible(h2pk, defn, before_cur, after_cur, mae):
                        continue
                    best = None
                    for br, fl, cp, res in brs[date]:
                        cd = price_at(date, br, lst_min)
                        if not cd:
                            continue
                        py = p_yes(fl, cp, mu, sig)
                        if py is None:
                            continue
                        edge = abs(py - cd[2] / 100.0)
                        if edge < EDGE:
                            continue
                        side_yes = py > cd[2] / 100.0
                        cost = cd[2] if side_yes else (100 - cd[1])
                        if not (0 < cost < 100):
                            continue
                        if best is None or edge > best[0]:
                            won = (res == "yes") if side_yes else (res == "no")
                            best = (edge, won, (100 - cost) if won else -cost)
                    if best:
                        tot[defn]["pnl"] += best[2]
                        tot[defn]["n"] += 1
                        tot[defn]["w"] += 1 if best[1] else 0
                        per_station[iata][defn] += best[2]
                        placed = True
    print("def    | trades  win%  totPnL(c)  PnL/trade")
    for d in defs:
        t = tot[d]; n = t["n"]
        print("%-6s | %5d  %4s  %+8d  %+6.1f" % (
            d, n, ("%.0f%%" % (100 * t["w"] / n)) if n else "-", t["pnl"], (t["pnl"] / n) if n else 0))
    print("\nper-station total PnL (CURRENT -> P2nt):")
    for s in sorted(per_station):
        p = per_station[s]
        print("  %-5s CUR %+6d | P1 %+6d | P2 %+6d | P2nt %+6d" % (s, p["CURRENT"], p["P1"], p["P2"], p["P2nt"]))


run()
