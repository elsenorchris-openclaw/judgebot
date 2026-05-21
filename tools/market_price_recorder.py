#!/usr/bin/env python3
"""market_price_recorder.py — standalone Kalshi BBO recorder for all weather
brackets (20 stations x HIGH/LOW). Polls the public markets-list API every
POLL_INTERVAL sec and appends BBO snapshots to market_price_history.sqlite.

Phase 1 of the window-optimization plan: historic temp data has no prices, so
the MAE-based window builder can't see profitability. This recorder accumulates
a clean (price, offset, settle) dataset under a stable matcher so windows can
later be chosen for PROFIT, per station/month — and so the "market undecided
well past peak" cases (which historic temps can't reveal) become visible.

Fully decoupled from the trading bot: read-only public API, no auth, no shared
state, own process/service. Touches nothing in the trade path.
"""
import datetime
import json
import logging
import signal
import sqlite3
import time
import urllib.error
import urllib.request

DB = "/home/ubuntu/data/market_price_history.sqlite"
HOST = "https://api.elections.kalshi.com"
POLL_INTERVAL = 120          # seconds between full sweeps
CALL_SLEEP = 1.0             # politeness between API calls (avoid 429)
MAX_DAYS_AHEAD = 1           # log today + this many future days only

# series_ticker -> (station_icao, side). Derived from the live shadow log.
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
SERIES = sorted(SERIES_MAP)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mpr")


def init_db():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS price_history (
        ts INTEGER NOT NULL, ticker TEXT NOT NULL, station TEXT, side TEXT,
        climate_day TEXT, bracket TEXT, floor REAL, cap REAL,
        yes_bid INTEGER, yes_ask INTEGER, no_bid INTEGER, no_ask INTEGER,
        last_price INTEGER, volume REAL, open_interest REAL,
        status TEXT, close_time TEXT,
        PRIMARY KEY (ticker, ts))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tk_ts ON price_history(ticker, ts)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_st_day ON price_history(station, climate_day)")
    c.commit()
    return c


def _cents(v):
    if v is None:
        return None
    try:
        return int(round(float(v) * 100))
    except (TypeError, ValueError):
        return None


def _flt(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_series(series):
    url = "%s/trade-api/v2/markets?series_ticker=%s&status=open&limit=1000" % (HOST, series)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r).get("markets", []) or []
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(3.0 * (attempt + 1))
                continue
            raise
    return []


def parse_ticker(tk):
    parts = (tk or "").split("-")
    if len(parts) < 3:
        return None, None
    bracket = "-".join(parts[2:])
    try:
        cday = datetime.datetime.strptime(parts[1], "%y%b%d").strftime("%Y-%m-%d")
    except ValueError:
        cday = parts[1]
    return cday, bracket


def sweep(conn):
    now = int(time.time())
    horizon = (datetime.datetime.utcnow() + datetime.timedelta(days=MAX_DAYS_AHEAD)).strftime("%Y-%m-%d")
    n = 0
    for series in SERIES:
        st, side = SERIES_MAP[series]
        try:
            mks = fetch_series(series)
        except Exception as e:
            log.warning("fetch %s failed: %s", series, str(e)[:80])
            time.sleep(CALL_SLEEP)
            continue
        rows = []
        for m in mks:
            tk = m.get("ticker")
            cday, bracket = parse_ticker(tk)
            if cday and cday > horizon:
                continue
            rows.append((
                now, tk, st, side, cday, bracket,
                _flt(m.get("floor_strike")), _flt(m.get("cap_strike")),
                _cents(m.get("yes_bid_dollars")), _cents(m.get("yes_ask_dollars")),
                _cents(m.get("no_bid_dollars")), _cents(m.get("no_ask_dollars")),
                _cents(m.get("last_price_dollars")),
                _flt(m.get("volume_fp")), _flt(m.get("open_interest_fp")),
                m.get("status"), m.get("close_time"),
            ))
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO price_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows)
            n += len(rows)
        except Exception as e:
            log.warning("insert %s: %s", series, str(e)[:80])
        time.sleep(CALL_SLEEP)
    conn.commit()
    return n


_run = True


def _stop(*_a):
    global _run
    _run = False


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    conn = init_db()
    log.info("market_price_recorder start: %d series -> %s (interval %ds)",
             len(SERIES), DB, POLL_INTERVAL)
    while _run:
        t0 = time.time()
        try:
            n = sweep(conn)
            log.info("sweep wrote %d rows in %.0fs", n, time.time() - t0)
        except Exception:
            log.exception("sweep error")
        slept = 0.0
        while _run and slept < POLL_INTERVAL - (time.time() - t0):
            time.sleep(1.0)
            slept += 1.0
    conn.close()
    log.info("stopped")


if __name__ == "__main__":
    main()
