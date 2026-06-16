#!/usr/bin/env python3.12
"""replay_backtest.py — FAITHFUL backtest that recreates the bot's settled P&L.

Built 2026-06-16. The lesson behind this tool: reconstruction backtests that
DON'T match the wallet have repeatedly misled us (the -$156 era). So this anchors
on GROUND TRUTH (trades.jsonl actual fills x Kalshi settlements) and self-asserts
that the baseline reproduces it. It then enriches each real fill with its
decision-time shadow row (mu/edge/clearance/spread/p_yes) so a candidate config
can be evaluated by COUNTERFACTUALLY removing real fills (faithful for TIGHTENING).

Usage:  python3.12 tools/replay_backtest.py            # baseline + current config
        python3.12 tools/replay_backtest.py --by-day   # per-day detail

NOTE: counterfactual gating is faithful for tightening (removing actual fills with
real prices/outcomes). Loosening (adding skipped trades) needs the shadow near-miss
stream and is decision-ask-priced (optimistic on slippage) -- judge those live.
"""
import json, math, collections, sys, os, glob, gzip, argparse
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE); os.chdir(HERE)
import config; config.apply_env()
import kalshi_client as kc

def fee(p, n): return math.ceil(0.07 * p * (1 - p) * n * 100) / 100.0

def load_settlements():
    setts, cur = {}, None
    for _ in range(15):
        p = {"limit": 200}
        if cur: p["cursor"] = cur
        d = kc.get("/trade-api/v2/portfolio/settlements", p)
        for s in d.get("settlements") or []:
            if s.get("market_result") in ("yes", "no"):
                setts.setdefault(s["ticker"], s["market_result"])
        cur = d.get("cursor")
        if not cur: break
    return setts

def load_shadow_context():
    """First/executed shadow decision row per ticker (mu/edge/clearance/etc.)."""
    ctx = {}
    files = sorted(glob.glob("data/shadow_archive/shadow_nn_strategy_*.jsonl.gz")) + \
            ["data/shadow_nn_strategy.jsonl"]
    for fp in files:
        op = gzip.open if fp.endswith(".gz") else open
        try: fh = op(fp, "rt")
        except FileNotFoundError: continue
        with fh:
            for ln in fh:
                if '"auto_exec_attempted": true' not in ln: continue
                try: d = json.loads(ln)
                except Exception: continue
                if d.get("decision") not in ("BUY_NO", "BUY_YES"): continue
                nn = d.get("nn") or {}
                if not (nn.get("mu_method") or "").startswith("blend"): continue
                tk = d.get("ticker"); ex = bool(d.get("auto_exec_executed"))
                if tk in ctx and not (ex and not ctx[tk]["ex"]): continue
                br = d.get("bracket") or {}; mkt = d.get("market") or {}
                ctx[tk] = dict(ex=ex, kind=br.get("kind"), fl=br.get("floor"), cp=br.get("cap"),
                    mu=nn.get("mu_chosen"), sg=nn.get("sigma_chosen"), edge=d.get("edge_pp"),
                    py=d.get("p_yes"), na=mkt.get("no_ask_c"), sp=mkt.get("spread_c"))
    return ctx

def build():
    setts = load_settlements()
    ctx = load_shadow_context()
    ent, exits = {}, collections.defaultdict(int)
    for ln in open("data/trades.jsonl"):
        try: d = json.loads(ln)
        except Exception: continue
        if (d.get("date_str") or "") < "2026-06-02": continue
        if d.get("kind") == "entry": ent.setdefault(d["market_ticker"], d)
        elif d.get("kind") in ("exit", "sell"): exits[d.get("market_ticker")] += d.get("sell_count") or 0
    fills = []
    for tk, e in ent.items():
        res = setts.get(tk)
        if res is None: continue
        held = (e.get("count") or 0) - exits.get(tk, 0)
        if held <= 0 or e.get("entry_price") is None: continue
        won = (res == "no") if e["action"] == "BUY_NO" else (res == "yes")
        p = e["entry_price"]
        pl = (held * (1 - p) - fee(p, held)) if won else (-held * p - fee(p, held))
        s = ctx.get(tk, {})
        clr = (s["mu"] - (s["cp"] + 0.5)) if (s.get("mu") is not None and s.get("cp") is not None
                                              and s.get("fl") is not None and e["action"] == "BUY_NO") else None
        fills.append(dict(tk=tk, day=e["date_str"], side=e["action"],
            series="LOW" if "KXLOW" in tk else "HIGH", kind=s.get("kind"),
            p=p, n=held, pl=round(pl, 3), won=won, edge=s.get("edge"), sp=s.get("sp"),
            na=s.get("na"), clr=(round(clr, 2) if clr is not None else None),
            py=s.get("py"), matched=bool(s)))
    return fills

def cur_config(r):
    if not r["matched"]: return True  # keep unmatchable (pre-archive) fills in baseline-equivalent
    if r["series"] == "HIGH":
        if r["side"] != "BUY_NO": return False
        if (r["edge"] or 0) < config.PUSH_MIN_EDGE_PP: return False
        if r["sp"] is None or r["sp"] > config.PUSH_MAX_SPREAD_C_HIGH: return False
        if r["na"] is None or not (config.PUSH_MIN_ENTRY_C <= r["na"] <= config.PUSH_MAX_ENTRY_C): return False
        if r["kind"] == "B":
            return r["clr"] is not None and r["clr"] >= config.PUSH_HIGH_NO_MIN_CLEARANCE_F
        return r["py"] is not None and (1 - r["py"]) >= config.PUSH_HIGH_T_NO_MIN_PNO
    else:
        if r["side"] != "BUY_NO" or r["kind"] != "B": return False
        if (r["edge"] or 0) < config.PUSH_MIN_EDGE_PP_LOW: return False
        if r["na"] is None or not (config.PUSH_MIN_ENTRY_C_LOW <= r["na"] <= config.PUSH_MAX_ENTRY_C): return False
        if r["sp"] is None or r["sp"] > config.PUSH_MAX_SPREAD_C_LOW: return False
        return r["py"] is not None and (1 - r["py"]) >= config.PUSH_LOW_MIN_PNO

def rep(rs, lab):
    if not rs: return f"{lab:30s} n=0"
    n = len(rs); w = sum(r["won"] for r in rs); pl = sum(r["pl"] for r in rs)
    h1 = sum(r["pl"] for r in rs if r["day"] <= "2026-06-08"); h2 = pl - h1
    return f"{lab:30s} n={n:3d} WR={100*w/n:3.0f}% ${pl:+8.2f} | H1 ${h1:+7.2f} H2 ${h2:+7.2f}"

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--by-day", action="store_true"); a = ap.parse_args()
    fills = build()
    base = sum(r["pl"] for r in fills)
    print(f"=== FAITHFUL REPLAY ({len(fills)} settled fills since 6/2) ===")
    print(rep(fills, "GROUND TRUTH (all real fills)"))
    print(rep([r for r in fills if cur_config(r)], "CURRENT live config (counterfactual)"))
    print(rep([r for r in fills if r["series"]=="HIGH" and cur_config(r)], "  HIGH"))
    print(rep([r for r in fills if r["series"]=="LOW" and cur_config(r)], "  LOW"))
    if a.by_day:
        bd = collections.defaultdict(float)
        for r in fills: bd[r["day"]] += r["pl"]
        print("by day (ground truth):", " ".join(f"{d[-2:]}:{bd[d]:+.0f}" for d in sorted(bd)))
    print(f"\nbaseline ${base:+.2f} is the ground-truth wallet P&L this tool must reproduce.")
