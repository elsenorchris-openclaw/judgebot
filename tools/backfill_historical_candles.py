#!/usr/bin/env python3
"""Backfill PRE-CUTOFF (historical-tier) candlesticks from Kalshi's /historical/
endpoints, which retain settled markets + candles back to each series' launch
(PHX ~Feb 2026, MIA/NY ~May 2023). The live /markets endpoint prunes to ~67 days;
this reaches the full multi-year archive. Writes to the SAME candle_history /
market_meta tables (separate DB from the live recorder). Resumable, rate-limited.

NOTE: historical candle BBO is under yes_bid.close / yes_ask.close (plain dollar
strings) -- NOT yes_bid.close_dollars (that's the LIVE schema). Uses 60-min
interval (adequate for offset-window analysis; keeps row/call counts sane).
"""
import datetime
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DB = "/home/ubuntu/data/market_history_backfill.sqlite"
HOST = "https://api.elections.kalshi.com"
CALL_SLEEP = 1.3
MIN_VOLUME = 1.0
INTERVAL = 60   # minutes per candle (hourly)

SERIES_MAP = {
    "KXHIGHAUS": ("KAUS", "HIGH"), "KXHIGHCHI": ("KMDW", "HIGH"),
    "KXHIGHDEN": ("KDEN", "HIGH"), "KXHIGHLAX": ("KLAX", "HIGH"),
    "KXHIGHMIA": ("KMIA", "HIGH"), "KXHIGHNY": ("KNYC", "HIGH"),
    "KXHIGHPHIL": ("KPHL", "HIGH"), "KXHIGHTATL": ("KATL", "HIGH"),
    "KXHIGHTBOS": ("KBOS", "HIGH"), "KXHIGHTDAL": ("KDFW", "HIGH"),
    "KXHIGHTDC": ("KDCA", "HIGH"), "KXHIGHTHOU": ("KHOU", "HIGH"),
    "KXHIGHTLV": ("KLAS", "HIGH"), "KXHIGHTMIN": ("KMSP", "HIGH"),
    "KXHIGHTNOLA": ("KMSY", "HIGH"), "KXHIGHTOKC": ("KOKC", "HIGH"),
    "KXHIGHTPHX": ("KPHX", "HIGH"), "KXHIGHTSATX": ("KSAT", "HIGH"),
    "KXHIGHTSEA": ("KSEA", "HIGH"), "KXHIGHTSFO": ("KSFO", "HIGH"),
    "KXLOWTATL": ("KATL", "LOW"), "KXLOWTAUS": ("KAUS", "LOW"),
    "KXLOWTBOS": ("KBOS", "LOW"), "KXLOWTCHI": ("KMDW", "LOW"),
    "KXLOWTDAL": ("KDFW", "LOW"), "KXLOWTDC": ("KDCA", "LOW"),
    "KXLOWTDEN": ("KDEN", "LOW"), "KXLOWTHOU": ("KHOU", "LOW"),
    "KXLOWTLAX": ("KLAX", "LOW"), "KXLOWTLV": ("KLAS", "LOW"),
    "KXLOWTMIA": ("KMIA", "LOW"), "KXLOWTMIN": ("KMSP", "LOW"),
    "KXLOWTNOLA": ("KMSY", "LOW"), "KXLOWTNYC": ("KNYC", "LOW"),
    "KXLOWTOKC": ("KOKC", "LOW"), "KXLOWTPHIL": ("KPHL", "LOW"),
    "KXLOWTPHX": ("KPHX", "LOW"), "KXLOWTSATX": ("KSAT", "LOW"),
    "KXLOWTSEA": ("KSEA", "LOW"), "KXLOWTSFO": ("KSFO", "LOW"),
}


def _get(path):
    for attempt in range(5):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(HOST + path, headers={"Accept": "application/json"}),
                    timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 4:
                time.sleep(5.0 * (attempt + 1))
                continue
            raise
    return {}


def _cents(s):
    try:
        return int(round(float(s) * 100))
    except (TypeError, ValueError):
        return None


def _flt(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _cday(tk):
    p = tk.split("-")
    if len(p) < 2:
        return None
    try:
        return datetime.datetime.strptime(p[1], "%y%b%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def init_db(c):
    c.execute("""CREATE TABLE IF NOT EXISTS candle_history (
        ticker TEXT, station TEXT, side TEXT, climate_day TEXT, bracket TEXT,
        ts INTEGER, yes_bid INTEGER, yes_ask INTEGER, last_price INTEGER,
        volume REAL, open_interest REAL, PRIMARY KEY (ticker, ts))""")
    c.execute("""CREATE TABLE IF NOT EXISTS market_meta (
        ticker TEXT PRIMARY KEY, station TEXT, side TEXT, climate_day TEXT,
        bracket TEXT, floor REAL, cap REAL, result TEXT, close_time TEXT, volume REAL)""")
    c.execute("CREATE TABLE IF NOT EXISTS hist_progress (ticker TEXT PRIMARY KEY, done INTEGER)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ch_st_day2 ON candle_history(station, climate_day)")
    c.commit()


def list_historical(series):
    out, cursor = [], None
    while True:
        p = "/trade-api/v2/historical/markets?series_ticker=%s&limit=200" % series
        if cursor:
            p += "&cursor=%s" % cursor
        d = _get(p)
        mks = d.get("markets", []) or []
        out.extend(mks)
        cursor = d.get("cursor")
        if not cursor or not mks:
            break
        time.sleep(CALL_SLEEP)
    return out


def backfill_series(c, series):
    st, side = SERIES_MAP[series]
    try:
        mks = list_historical(series)
    except Exception as e:
        print("%s list FAILED: %s" % (series, str(e)[:80]), flush=True)
        return
    done = {r[0] for r in c.execute("SELECT ticker FROM hist_progress WHERE done=1")}
    n_mk = n_c = 0
    for m in mks:
        tk = m.get("ticker")
        cday = _cday(tk)
        if not tk or not cday or tk in done:
            continue
        vol = float(m.get("volume_fp") or m.get("volume") or 0)
        c.execute("INSERT OR REPLACE INTO market_meta VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (tk, st, side, cday, "-".join(tk.split("-")[2:]),
                   _flt(m.get("floor_strike")), _flt(m.get("cap_strike")),
                   m.get("result"), m.get("close_time"), vol))
        if vol < MIN_VOLUME:
            c.execute("INSERT OR REPLACE INTO hist_progress VALUES (?,1)", (tk,))
            continue
        try:
            ce = int(datetime.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())
        except Exception:
            c.execute("INSERT OR REPLACE INTO hist_progress VALUES (?,1)", (tk,))
            continue
        try:
            cs = int(datetime.datetime.fromisoformat(m["open_time"].replace("Z", "+00:00")).timestamp())
        except Exception:
            cs = ce - 3 * 86400
        try:
            cd = _get("/trade-api/v2/historical/markets/%s/candlesticks?start_ts=%d&end_ts=%d&period_interval=%d"
                      % (tk, cs, ce, INTERVAL))
            candles = cd.get("candlesticks", []) or []
        except Exception as e:
            print("  candle fail %s: %s" % (tk, str(e)[:50]), flush=True)
            time.sleep(CALL_SLEEP)
            continue
        rows = []
        for x in candles:
            yb = x.get("yes_bid") or {}
            ya = x.get("yes_ask") or {}
            pr = x.get("price") or {}
            rows.append((tk, st, side, cday, "-".join(tk.split("-")[2:]),
                         x.get("end_period_ts"),
                         _cents(yb.get("close")), _cents(ya.get("close")),
                         _cents(pr.get("close") or pr.get("previous")),
                         _flt(x.get("volume")), _flt(x.get("open_interest"))))
        c.executemany("INSERT OR IGNORE INTO candle_history VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        c.execute("INSERT OR REPLACE INTO hist_progress VALUES (?,1)", (tk,))
        n_mk += 1
        n_c += len(rows)
        if n_mk % 50 == 0:
            c.commit()
            print("  %s: %d mkts, %d candles..." % (series, n_mk, n_c), flush=True)
        time.sleep(CALL_SLEEP)
    c.commit()
    print("%s DONE: %d mkts, %d candles (of %d historical)" % (series, n_mk, n_c, len(mks)), flush=True)


def main():
    series_list = sys.argv[1:] or sorted(SERIES_MAP)
    c = sqlite3.connect(DB, timeout=60)
    c.execute("PRAGMA busy_timeout=60000")
    c.execute("PRAGMA journal_mode=WAL")
    init_db(c)
    print("HISTORICAL backfill: %d series" % len(series_list), flush=True)
    for s in series_list:
        try:
            backfill_series(c, s)
        except Exception as e:
            print("SERIES %s FAILED: %s" % (s, str(e)[:100]), flush=True)
    c.close()
    print("ALL HISTORICAL DONE", flush=True)


if __name__ == "__main__":
    main()
