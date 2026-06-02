#!/usr/bin/env python3.12
"""daily_pnl_readout.py — post the judge bot's SETTLED P&L (Kalshi truth) to Discord.

Runs daily on the box via cron (use python3.12 — system python3 breaks Kalshi
signing). Pulls /portfolio/settlements, joins to this bot's own trades.jsonl
entries, computes realized P&L for the most recent settled climate-day, and
splits BLEND ("blend_*") vs the nn_match fallback ("nn_match_*") so we can track
the blend edge vs its backtest. Realized = Kalshi settlement, NOT obs/MTM.
cf project_blend_bot_architecture_20260602.
"""
import json, os, pathlib, collections, math, datetime

HERE = pathlib.Path(__file__).resolve().parent
os.chdir(HERE)
for f in (HERE / ".env", pathlib.Path("/home/ubuntu/.env")):
    try:
        for ln in f.read_text().splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                k, _, v = ln.partition("="); os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass

import kalshi_client  # noqa: E402

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CHAN = os.environ.get("DISCORD_TRADE_CHANNEL_ID", "1511264871151304725")

def post(msg: str) -> None:
    print(msg)
    if not (TOKEN and CHAN):
        return
    try:
        import httpx
        httpx.post(f"https://discord.com/api/v10/channels/{CHAN}/messages",
                   json={"content": msg},
                   headers={"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"},
                   timeout=8.0)
    except Exception as e:
        print("discord post failed:", e)

# --- load this bot's own entries (so we only count judge trades on the shared wallet) ---
ent = {}
for ln in open("data/trades.jsonl"):
    try:
        d = json.loads(ln)
    except Exception:
        continue
    if d.get("kind") == "entry" and d.get("market_ticker"):
        ent[d["market_ticker"]] = d

# --- Kalshi-settled truth ---
try:
    setts = kalshi_client.list_settlements(limit=500)
except Exception as e:
    post(f"📊 Judge settled-P&L readout FAILED: {e}")
    raise

byday = collections.defaultdict(list)
for s in setts:
    tk = s.get("ticker")
    e = ent.get(tk)
    if not e:
        continue
    res = s.get("market_result")
    if res not in ("yes", "no"):
        continue
    act = e.get("action"); ep = e.get("entry_price"); cnt = e.get("count") or 0
    if ep is None or not cnt:
        continue
    won = (res == "no") if act == "BUY_NO" else (res == "yes")
    fee = math.ceil(0.07 * ep * (1 - ep) * cnt * 100) / 100.0
    pnl = (cnt * (1 - ep) - fee) if won else (-cnt * ep - fee)
    mm = str(e.get("mu_method") or "")
    src = "blend" if mm.startswith("blend") else ("matcher" if mm.startswith("nn_match") else "untagged")
    byday[str(e.get("date_str"))].append(
        dict(tk=tk, act=act, src=src, won=won, pnl=pnl, cost=cnt * ep, cnt=cnt,
             series="LOW" if "KXLOW" in tk else "HIGH"))

if not byday:
    post("📊 **Judge settled P&L** — no settled trades found in the recent window.")
    raise SystemExit

def line(rs):
    if not rs:
        return "n=0"
    n = len(rs); w = sum(r["won"] for r in rs); pl = sum(r["pnl"] for r in rs)
    cost = sum(r["cost"] for r in rs); ct = sum(r["cnt"] for r in rs)
    return (f"n={n} WR={100*w/n:.0f}% **${pl:+.2f}** on ${cost:.0f} "
            f"(ROI {100*pl/cost if cost else 0:+.0f}%, {100*pl/ct if ct else 0:+.1f}c/ct)")

day = max(byday)
rows = byday[day]
blend = [r for r in rows if r["src"] == "blend"]
other = [r for r in rows if r["src"] != "blend"]
# cumulative blend since it went live (2026-06-02)
cum_blend = [r for ds, rs in byday.items() if ds >= "2026-06-02" for r in rs if r["src"] == "blend"]

msg = [f"📊 **Judge settled P&L — {day}** (Kalshi truth)",
       f"TOTAL: {line(rows)}",
       f"🟢 BLEND:  {line(blend)}   (backtest ~+11c/ct loosened)",
       f"⚪ matcher/untagged: {line(other)}"]
if cum_blend and any(r for ds in byday for r in byday[ds] if ds > "2026-06-02"):
    msg.append(f"— cumulative BLEND since 6/2: {line(cum_blend)}")
worst = sorted(rows, key=lambda r: r["pnl"])[:3]
if worst and worst[0]["pnl"] < 0:
    msg.append("biggest losers: " + ", ".join(f"{r['tk'].split('-')[0]} {r['act'][4:]} ${r['pnl']:+.1f}" for r in worst))
post("\n".join(msg))
