#!/usr/bin/env python3
"""Fill the backtest-DB capture gap for ALL 40 series (HIGH + LOW) from Kalshi's
LIVE settled endpoint (recent ~67d) + the series candlesticks endpoint. The
historical backfill only served pre-67d data; this gets the live era. Live
candle schema is yes_bid.close_dollars (dollars) -> convert to cents. Idempotent:
re-inserts market_meta, fetches candles only for tickers not already stored."""
import sys, sqlite3, datetime, time
sys.path.insert(0, "/home/ubuntu/paper_judge_bot/tools")
from backfill_historical_candles import SERIES_MAP, _get, _cday, _flt, DB, init_db

INTERVAL = 60; SLEEP = 0.45


def cents(d):
    if not d: return None
    v = d.get("close_dollars")
    if v is not None:
        try: return int(round(float(v) * 100))
        except (TypeError, ValueError): return None
    v = d.get("close")
    try: return int(v) if v is not None else None
    except (TypeError, ValueError): return None


def fnum(s):
    try: return float(s)
    except (TypeError, ValueError): return None


conn = sqlite3.connect(DB, timeout=120); conn.execute("PRAGMA busy_timeout=120000")
init_db(conn)
have_cand = set(r[0] for r in conn.execute("SELECT DISTINCT ticker FROM candle_history"))
print("FILL start: %d series; %d tickers already have candles" % (len(SERIES_MAP), len(have_cand)), flush=True)
g_mk = g_c = 0
for series in sorted(SERIES_MAP):
    st, side = SERIES_MAP[series]
    try:
        mks = _get("/trade-api/v2/markets?series_ticker=%s&status=settled&limit=1000" % series).get("markets", [])
    except Exception as e:
        print("%s LIST FAIL %s" % (series, str(e)[:60]), flush=True); continue
    n_mk = n_c = 0
    for m in mks:
        tk = m.get("ticker"); cday = _cday(tk or "")
        if not tk or not cday: continue
        vol = fnum(m.get("volume_fp")) or fnum(m.get("volume")) or 0
        conn.execute("INSERT OR REPLACE INTO market_meta VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (tk, st, side, cday, "-".join(tk.split("-")[2:]),
                      _flt(m.get("floor_strike")), _flt(m.get("cap_strike")),
                      m.get("result"), m.get("close_time"), vol))
        n_mk += 1
        if tk in have_cand: continue
        try:
            ot = int(datetime.datetime.fromisoformat(m["open_time"].replace("Z", "+00:00")).timestamp())
            ct = int(datetime.datetime.fromisoformat(m["close_time"].replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        try:
            cs = _get("/trade-api/v2/series/%s/markets/%s/candlesticks?start_ts=%d&end_ts=%d&period_interval=%d"
                      % (series, tk, ot, ct, INTERVAL)).get("candlesticks", []) or []
        except Exception as e:
            print("  cand fail %s %s" % (tk, str(e)[:40]), flush=True); time.sleep(SLEEP); continue
        rows = [(tk, st, side, cday, "-".join(tk.split("-")[2:]), x.get("end_period_ts"),
                 cents(x.get("yes_bid")), cents(x.get("yes_ask")), cents(x.get("price")),
                 fnum(x.get("volume_fp")) or fnum(x.get("volume")),
                 fnum(x.get("open_interest_fp")) or fnum(x.get("open_interest"))) for x in cs]
        if rows:
            conn.executemany("INSERT OR IGNORE INTO candle_history VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            n_c += len(rows)
        time.sleep(SLEEP)
    conn.commit()
    print("%-14s %s/%-4s +%d mkts +%d candles" % (series, st, side, n_mk, n_c), flush=True)
    g_mk += n_mk; g_c += n_c
conn.commit(); conn.close()
print("FILL DONE: %d mkts touched, %d candles added" % (g_mk, g_c), flush=True)
