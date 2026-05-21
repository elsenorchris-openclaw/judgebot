#!/usr/bin/env python3
"""One-shot backfill: extract market BBO already captured in the bot's shadow
log into a SEPARATE table (shadow_price_backfill) of market_price_history.sqlite.
Kept separate from the live recorder's all-bracket sweeps because shadow rows are
bot-eval-gated (only brackets the bot evaluated) and span the matcher-unstable
period. But the PRICES are matcher-independent and carry h_to_peak (offset),
so they bootstrap the "market price by offset" analysis immediately."""
import json
import sqlite3

DB = "/home/ubuntu/data/market_price_history.sqlite"
SHADOW = "/home/ubuntu/paper_judge_bot/data/shadow_nn_strategy.jsonl"

c = sqlite3.connect(DB)
c.execute("""CREATE TABLE IF NOT EXISTS shadow_price_backfill (
    ts REAL NOT NULL, ticker TEXT NOT NULL, station TEXT, side TEXT,
    climate_day TEXT, bracket TEXT, yes_ask INTEGER, no_ask INTEGER,
    spread INTEGER, h_to_peak REAL, edge_pp REAL, nn_fired INTEGER, decision TEXT,
    PRIMARY KEY (ticker, ts))""")
c.execute("CREATE INDEX IF NOT EXISTS idx_bf_st_day ON shadow_price_backfill(station, climate_day)")

n = 0
batch = []
for line in open(SHADOW):
    try:
        d = json.loads(line)
    except Exception:
        continue
    tk = d.get("ticker")
    if not tk:
        continue
    mkt = d.get("market") or {}
    ya, na = mkt.get("yes_ask_c"), mkt.get("no_ask_c")
    if ya is None and na is None:
        continue
    side = "HIGH" if "KXHIGH" in tk else ("LOW" if "KXLOW" in tk else None)
    sig = d.get("signals") or {}
    parts = tk.split("-")
    bracket = "-".join(parts[2:]) if len(parts) >= 3 else None
    batch.append((
        d.get("ts"), tk, d.get("station"), side, d.get("climate_day"), bracket,
        ya, na, mkt.get("spread_c"), sig.get("h_to_peak"),
        d.get("edge_pp"), 1 if d.get("nn_fired") else 0, d.get("decision")))
    if len(batch) >= 5000:
        c.executemany("INSERT OR IGNORE INTO shadow_price_backfill VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        n += len(batch)
        batch = []
if batch:
    c.executemany("INSERT OR IGNORE INTO shadow_price_backfill VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
    n += len(batch)
c.commit()
got = c.execute("SELECT COUNT(*) FROM shadow_price_backfill").fetchone()[0]
days = [r[0] for r in c.execute("SELECT DISTINCT climate_day FROM shadow_price_backfill ORDER BY climate_day")]
print("processed %d price rows -> %d unique (ticker,ts) in shadow_price_backfill" % (n, got))
print("days covered:", days)
