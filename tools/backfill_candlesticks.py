#!/usr/bin/env python3
"""Backfill historical intraday BBO for settled weather markets from Kalshi's
public candlesticks endpoint (1-min yes_bid/yes_ask). The WS socket is live-only
and can't be backfilled, but candlesticks reach ~34 days back. Combined with the
v3 build's per-date mu-by-offset (consistent current matcher) + settlement, this
yields real profit-by-offset per (station, month) -- the proper, price-based way
to pick the best buy time for matching, per city, over a robust sample.

Resumable (backfill_progress), rate-limited (429 backoff). Read-only public API.
Writes to data/market_price_history.sqlite (candle_history + market_meta).

Usage:  python3 backfill_candlesticks.py [SERIES1 SERIES2 ...]
        (no args = all 40 series)
"""
import datetime
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request

DB = "/home/ubuntu/data/market_history_backfill.sqlite"  # separate file: no
# contention with the live market-price-recorder writing market_price_history.sqlite
HOST = "https://api.elections.kalshi.com"
CALL_SLEEP = 0.8
MIN_VOLUME = 1.0          # skip never-traded brackets (no opportunity signal)
CUTOFF_DAY = "2026-03-10"  # don't go earlier than this

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


def _get(url):
    for attempt in range(4):
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, headers={"Accept": "application/json"}),
                    timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(3.0 * (attempt + 1))
                continue
            raise
    return {}


def _cents(d):
    try:
        return int(round(float(d) * 100))
    except (TypeError, ValueError):
        return None


def _cday(tk):
    parts = tk.split("-")
    if len(parts) < 2:
        return None
    try:
        return datetime.datetime.strptime(parts[1], "%y%b%d").strftime("%Y-%m-%d")
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
    c.execute("CREATE TABLE IF NOT EXISTS backfill_progress (ticker TEXT PRIMARY KEY, done INTEGER)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ch_st_day ON candle_history(station, climate_day)")
    c.commit()


def list_settled(series):
    out, cursor = [], None
    while True:
        url = "%s/trade-api/v2/markets?series_ticker=%s&status=settled&limit=200" % (HOST, series)
        if cursor:
            url += "&cursor=%s" % cursor
        d = _get(url)
        mks = d.get("markets", []) or []
        out.extend(mks)
        cursor = d.get("cursor")
        oldest = min((_cday(m.get("ticker", "")) or "9999") for m in mks) if mks else None
        if not cursor or not mks or (oldest and oldest < CUTOFF_DAY):
            break
        time.sleep(CALL_SLEEP)
    return out


def fetch_candles(series, tk, open_ts, close_ts):
    url = ("%s/trade-api/v2/series/%s/markets/%s/candlesticks?start_ts=%d&end_ts=%d&period_interval=1"
           % (HOST, series, tk, open_ts, close_ts))
    return _get(url).get("candlesticks", []) or []


def backfill_series(c, series):
    st, side = SERIES_MAP[series]
    mks = list_settled(series)
    done = {r[0] for r in c.execute("SELECT ticker FROM backfill_progress WHERE done=1")}
    n_mk = n_candle = 0
    for m in mks:
        tk = m.get("ticker")
        cday = _cday(tk)
        if not tk or not cday or cday < CUTOFF_DAY or tk in done:
            continue
        vol = float(m.get("volume_fp") or 0)
        c.execute("INSERT OR REPLACE INTO market_meta VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (tk, st, side, cday, "-".join(tk.split("-")[2:]),
                   m.get("floor_strike"), m.get("cap_strike"), m.get("result"),
                   m.get("close_time"), vol))
        if vol < MIN_VOLUME:
            c.execute("INSERT OR REPLACE INTO backfill_progress VALUES (?,1)", (tk,))
            continue
        try:
            ct = m.get("close_time", "")
            ce = int(datetime.datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp())
        except Exception:
            ce = int(time.time())
        ot = m.get("open_time", "")
        try:
            cs = int(datetime.datetime.fromisoformat(ot.replace("Z", "+00:00")).timestamp())
        except Exception:
            cs = ce - 86400
        try:
            candles = fetch_candles(series, tk, cs, ce)
        except Exception as e:
            print("  candle fail %s: %s" % (tk, str(e)[:60]), flush=True)
            time.sleep(CALL_SLEEP)
            continue
        rows = []
        for cd in candles:
            yb = cd.get("yes_bid") or {}
            ya = cd.get("yes_ask") or {}
            pr = cd.get("price") or {}
            rows.append((tk, st, side, cday, "-".join(tk.split("-")[2:]),
                         cd.get("end_period_ts"),
                         _cents(yb.get("close_dollars")), _cents(ya.get("close_dollars")),
                         _cents(pr.get("close_dollars") or pr.get("previous_dollars")),
                         float(cd.get("volume_fp") or 0), float(cd.get("open_interest_fp") or 0)))
        c.executemany("INSERT OR IGNORE INTO candle_history VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        c.execute("INSERT OR REPLACE INTO backfill_progress VALUES (?,1)", (tk,))
        n_mk += 1
        n_candle += len(rows)
        if n_mk % 25 == 0:
            c.commit()
            print("  %s: %d markets, %d candles..." % (series, n_mk, n_candle), flush=True)
        time.sleep(CALL_SLEEP)
    c.commit()
    print("%s DONE: %d markets backfilled, %d candle rows (of %d settled)" % (series, n_mk, n_candle, len(mks)), flush=True)


def main():
    series_list = sys.argv[1:] or sorted(SERIES_MAP)
    c = sqlite3.connect(DB, timeout=30)
    c.execute("PRAGMA busy_timeout=30000")
    c.execute("PRAGMA journal_mode=WAL")
    init_db(c)
    print("backfill candlesticks: %d series, cutoff %s" % (len(series_list), CUTOFF_DAY), flush=True)
    for s in series_list:
        try:
            backfill_series(c, s)
        except Exception as e:
            print("SERIES %s FAILED: %s" % (s, str(e)[:100]), flush=True)
    c.close()
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
