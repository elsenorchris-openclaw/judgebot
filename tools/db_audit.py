#!/usr/bin/env python3
"""Thorough audit: for all 40 series (20 HIGH + 20 LOW), compare what Kalshi's
live settled endpoint has (recent ~67d) vs what's in our backtest DB
(market_meta + candle_history). Reports every gap so we know exactly what to
fill."""
import sys, sqlite3
sys.path.insert(0, "/home/ubuntu/paper_judge_bot/tools")
from backfill_historical_candles import SERIES_MAP, _get, _cday, DB

conn = sqlite3.connect(DB, timeout=60)
print("%-14s %-9s | live | db_meta db_cand | MISS_meta MISS_cand" % ("series", "stn/side"))
print("-" * 74)
tot_mm = tot_mc = 0
gaps = {}
for series in sorted(SERIES_MAP):
    st, side = SERIES_MAP[series]
    try:
        mks = _get("/trade-api/v2/markets?series_ticker=%s&status=settled&limit=1000" % series).get("markets", [])
    except Exception as e:
        print("%-14s LIVE FETCH FAIL: %s" % (series, str(e)[:40])); continue
    live_days = set(d for d in (_cday(m.get("ticker", "")) for m in mks) if d)
    db_meta = set(r[0] for r in conn.execute("SELECT DISTINCT climate_day FROM market_meta WHERE station=? AND side=? AND result IN ('yes','no')", (st, side)))
    db_cand = set(r[0] for r in conn.execute("SELECT DISTINCT climate_day FROM candle_history WHERE station=? AND side=?", (st, side)))
    miss_meta = live_days - db_meta
    miss_cand = live_days - db_cand
    tot_mm += len(miss_meta); tot_mc += len(miss_cand)
    if miss_meta or miss_cand:
        gaps[series] = (len(miss_meta), len(miss_cand))
    flag = "  <== GAP" if (miss_meta or miss_cand) else ""
    print("%-14s %s/%-4s | %4d | %6d %7d | %8d %9d%s" % (
        series, st, side, len(live_days),
        len(db_meta & live_days), len(db_cand & live_days),
        len(miss_meta), len(miss_cand), flag))
print("-" * 74)
print("TOTAL missing: market_meta days=%d, candle days=%d, across %d series" % (tot_mm, tot_mc, len(gaps)))
conn.close()
