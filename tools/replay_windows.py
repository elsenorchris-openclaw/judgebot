#!/usr/bin/env python3
"""Replay the CURRENT live HIGH windows on a past day's LIVE-RECORDER data (the
judge's shadow_nn_strategy.jsonl), to see how today's shipped windows WOULD have
traded that day. Faithful: buy at window-open (deepest in-window h_to_peak),
gated NO (edge>=PUSH_MIN_EDGE_PP, NO-cost in [10,80], spread<=PUSH_MAX_SPREAD_C_HIGH),
PLUS the (2d) thin-margin B-bracket gate (skip BUY_NO when mu-offset lands inside
[floor-0.5,cap+0.5]). Reports WITH-gate (matches the live bot) vs NO-gate so the
gate's value is visible. One trade per (station, climate_day), settled vs live
Kalshi results. Usage: replay_windows.py [YYYY-MM-DD]"""
import json, sys, collections, datetime
sys.path.insert(0, "/home/ubuntu/paper_judge_bot")
import config as c

DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-05-22"
SHADOW = "/home/ubuntu/paper_judge_bot/data/shadow_nn_strategy.jsonl"
WINS = c.PUSH_HIGH_TEMP_WINDOW_BY_STATION
GLOBAL = c.PUSH_HIGH_TEMP_WINDOW
EDGE = c.PUSH_MIN_EDGE_PP / 100.0
MAXSPREAD = float(getattr(c, "PUSH_MAX_SPREAD_C_HIGH", 0) or 0)
TM_ON = bool(getattr(c, "PUSH_SKIP_NO_MU_NEAR_BRACKET", False))
TM_OFF = getattr(c, "PUSH_NO_MU_CLI_OFFSET_BY_STATION", {})
TM_DEF = float(getattr(c, "PUSH_NO_MU_CLI_OFFSET_DEFAULT", 0.5))
GLO, GHI = 10, 80

sys.path.insert(0, "/home/ubuntu/paper_judge_bot/tools")
from backfill_historical_candles import SERIES_MAP, _get
DAY_SHORT = datetime.datetime.strptime(DAY, "%Y-%m-%d").strftime("%y%b%d").upper()
res = {}
for series, (st, side) in SERIES_MAP.items():
    if side != "HIGH":
        continue
    try:
        mks = _get("/trade-api/v2/markets?series_ticker=%s&status=settled&limit=1000" % series).get("markets", [])
    except Exception:
        continue
    for m in mks:
        tk = m.get("ticker", "")
        if DAY_SHORT not in tk:
            continue
        res[(st, "-".join(tk.split("-")[2:]))] = m.get("result")


def tm_blocks(st, fl, cp, kind, mu):
    """(2d) thin-margin: B-bracket BUY_NO where mu-offset is inside [fl-.5,cp+.5]."""
    if not TM_ON or kind != "B" or mu is None or fl is None or cp is None:
        return False
    off = float(TM_OFF.get(st, TM_DEF))
    return (float(fl) - 0.5) <= (float(mu) - off) <= (float(cp) + 0.5)


bystation = collections.defaultdict(list)
with open(SHADOW) as f:
    for ln in f:
        if DAY not in ln or "KXHIGH" not in ln:
            continue
        try: d = json.loads(ln)
        except Exception: continue
        if d.get("climate_day") != DAY or "KXHIGH" not in (d.get("ticker") or ""):
            continue
        sig = d.get("signals") or {}; m = d.get("market") or {}; br = d.get("bracket") or {}
        h = sig.get("h_to_peak"); py = d.get("p_yes")
        ya = m.get("yes_ask_c"); na = m.get("no_ask_c"); sp = m.get("spread_c")
        mu = d.get("mu_chosen", d.get("mu_pre_bias"))
        if h is None or py is None or ya is None or na is None:
            continue
        tk = d["ticker"]; brk = "-".join(tk.split("-")[2:])
        bystation[d["station"]].append((h, ya, na, sp, py, brk, br.get("floor"), br.get("cap"), br.get("kind"), mu))


def settle(st, na, brk):
    r = res.get((st, brk), "?")
    if r not in ("yes", "no"): return None, r
    return ((100 - na) if r == "no" else -na), r


print("REPLAY %s — WITH (2d) thin-margin gate vs NO-gate. edge>=%.0fpp NO[%d,%d] spread<=%dc\n"
      % (DAY, EDGE*100, GLO, GHI, MAXSPREAD))
print("%-5s %-12s | %-26s | %s" % ("stn", "window", "NO-GATE trade", "WITH-GATE (live bot)"))
sums = {"ng": [0, 0.0, 0], "wg": [0, 0.0, 0]}  # [n, pnl, wins]
for st in sorted(bystation):
    before, after = WINS.get(st, GLOBAL)
    inwin = sorted([e for e in bystation[st] if -after <= e[0] <= before], key=lambda e: (-e[0], -(e[1]/100.0 - e[4])))
    ng = wg = None
    for h, ya, na, sp, py, brk, fl, cp, kind, mu in inwin:
        if py >= ya/100.0: continue
        if (ya/100.0 - py) < EDGE: continue
        if not (GLO <= na <= GHI): continue
        if MAXSPREAD > 0 and sp is not None and sp > MAXSPREAD: continue
        if ng is None: ng = (brk, na, fl, cp, kind, mu)
        if wg is None and not tm_blocks(st, fl, cp, kind, mu):
            wg = (brk, na)
        if ng is not None and wg is not None: break
    def fmt(t):
        if t is None: return "-- none --"
        p, r = settle(st, t[1], t[0]);
        return "NO %s @%dc -> %s %s" % (t[0], t[1], r.upper(), ("%+dc" % p) if p is not None else "(uns)")
    blocked = ng is not None and tm_blocks(st, ng[2], ng[3], ng[4], ng[5])
    print("%-5s [pk%+.1f,pk%+.1f] | %-26s | %s%s" % (
        st, -before, after, fmt(ng), fmt(wg), "   <-- TM blocked" if (blocked and (wg is None or (ng and wg and ng[0] != wg[0]))) else ""))
    for key, t in (("ng", ng[:2] if ng else None), ("wg", wg)):
        if t is None: continue
        p, r = settle(st, t[1], t[0])
        if p is not None:
            sums[key][0] += 1; sums[key][1] += p; sums[key][2] += 1 if p > 0 else 0
print("\nNO-GATE   : %d trades, %dW/%dL, PnL %+.0fc (%+.2f$), avg %+.1fc/bet" % (
    sums["ng"][0], sums["ng"][2], sums["ng"][0]-sums["ng"][2], sums["ng"][1], sums["ng"][1]/100, sums["ng"][1]/sums["ng"][0] if sums["ng"][0] else 0))
print("WITH-GATE : %d trades, %dW/%dL, PnL %+.0fc (%+.2f$), avg %+.1fc/bet  <-- the live bot" % (
    sums["wg"][0], sums["wg"][2], sums["wg"][0]-sums["wg"][2], sums["wg"][1], sums["wg"][1]/100, sums["wg"][1]/sums["wg"][0] if sums["wg"][0] else 0))
print("\nGate value = WITH-GATE minus NO-GATE PnL: %+.0fc" % (sums["wg"][1] - sums["ng"][1]))
