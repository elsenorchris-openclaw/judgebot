"""state.py — positions, trades, decisions persistence.

Mirrors paper_min_bot's JSONL+JSON file layout so monitoring tools that
work for the existing bots also work here:

  data/positions.json      — dict {ticker → position_record} (open)
  data/trades.jsonl        — append-only entry/exit/candidate events
  data/decisions.jsonl     — append-only full Claude request/response log

Plus integration with the shared bot_decisions.sqlite via shared_tools.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import config


log = logging.getLogger("judge.state")


# ─────────────────────────────────────────────────────────────────────────────
# Positions (rich state per open ticker, atomic writes)
# ─────────────────────────────────────────────────────────────────────────────
def load_positions() -> dict[str, dict]:
    if not config.POSITIONS_PATH.exists():
        return {}
    try:
        return json.loads(config.POSITIONS_PATH.read_text())
    except Exception as e:
        log.warning("positions load failed: %s — starting empty", e)
        return {}


def save_positions(d: dict[str, dict]) -> None:
    """Atomic write: tmp + rename."""
    tmp = config.POSITIONS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, indent=2, default=str))
    os.replace(tmp, config.POSITIONS_PATH)


# ─────────────────────────────────────────────────────────────────────────────
# Append-only logs
# ─────────────────────────────────────────────────────────────────────────────
def _append_jsonl(path: Path, rec: dict) -> None:
    """One-line JSON append. fsync omitted — too expensive per cycle."""
    rec.setdefault("ts", time.time())
    try:
        with path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:
        log.warning("write %s failed: %s", path.name, e)


# ─────────────────────────────────────────────────────────────────────────────
# Foreign-bot claim registry
# ─────────────────────────────────────────────────────────────────────────────
# Anti-adoption guard: V2 max + V2 min's orphan-reconcile loops will adopt
# ANY ticker on the shared wallet that they don't have locally. We publish
# the set of tickers we own to a small JSON file; their reconcile patches
# read it and skip anything we've claimed. The judge bot claims a ticker
# IMMEDIATELY before placing the order so the V2 bots' next reconcile cycle
# can't race-adopt mid-fill.

import threading

_CLAIM_PATH = Path(__file__).resolve().parent / "data" / "claimed_tickers.json"
_claim_lock = threading.Lock()


def _today_dir() -> Path:
    """Per-day data folder: data/by_date/YYYY-MM-DD/. Created lazily."""
    import time as _time
    from datetime import datetime as _dt, timezone as _tz
    today = _dt.fromtimestamp(_time.time(), tz=_tz.utc).strftime("%Y-%m-%d")
    d = config.DATA_DIR / "by_date" / today
    d.mkdir(parents=True, exist_ok=True)
    return d


def log_candidate(rec: dict) -> None:
    """One row per (cycle, candidate) inspected — passed prescreen or not.
    Date-partitioned. Used for retrospective filter-design analysis."""
    _append_jsonl(_today_dir() / "candidates.jsonl", rec)


def log_cycle(rec: dict) -> None:
    """One row per entry cycle — totals + per-bucket counts."""
    _append_jsonl(_today_dir() / "cycles.jsonl", rec)


def log_settlement(rec: dict) -> None:
    """One row when a market we hold/held resolves."""
    _append_jsonl(_today_dir() / "settlements.jsonl", rec)


def log_scout(rec: dict) -> None:
    """One row per scout-and-sweep decision (orderbook-based BUY planning).
    Enables post-hoc analysis: how often does scout block? are skips on
    thin books accurate? did planned cum_cost match actual fill_cost?"""
    _append_jsonl(_today_dir() / "scouts.jsonl", rec)


def load_settled_tickers_today() -> set[str]:
    """Read today's settlements.jsonl and return the set of tickers already
    logged. Used to seed _settled_logged at startup so a restart doesn't
    duplicate every still-active settled position into the log."""
    import json as _json
    p = _today_dir() / "settlements.jsonl"
    out: set[str] = set()
    if not p.exists():
        return out
    try:
        with open(p) as f:
            for line in f:
                try:
                    rec = _json.loads(line)
                    tk = rec.get("ticker")
                    if tk:
                        out.add(tk)
                except Exception:
                    pass
    except Exception:
        log.exception("load_settled_tickers_today failed")
    return out


def log_trade_dated(rec: dict) -> None:
    """Date-partitioned mirror of trades.jsonl."""
    _append_jsonl(_today_dir() / "trades.jsonl", rec)


def log_decision_dated(rec: dict) -> None:
    """Date-partitioned mirror of decisions.jsonl."""
    _append_jsonl(_today_dir() / "decisions.jsonl", rec)


def _read_claims() -> set[str]:
    try:
        with _CLAIM_PATH.open() as f:
            data = json.load(f)
        return set(data.get("claimed") or [])
    except FileNotFoundError:
        return set()
    except Exception as e:
        log.warning("claim read failed: %s", e)
        return set()


def _write_claims(claimed: set[str]) -> None:
    try:
        from datetime import datetime, timezone
        body = {
            "claimed": sorted(claimed),
            "_updated": datetime.now(timezone.utc).isoformat(),
            "_owner": "paper-judge",
        }
        tmp = _CLAIM_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, indent=2))
        import os
        os.replace(tmp, _CLAIM_PATH)
    except Exception as e:
        log.warning("claim write failed: %s", e)


def claim_ticker(ticker: str) -> None:
    """Add `ticker` to the claimed set. Idempotent. Call before place_buy."""
    with _claim_lock:
        s = _read_claims()
        if ticker not in s:
            s.add(ticker)
            _write_claims(s)


def release_ticker(ticker: str) -> None:
    """Remove `ticker` from the claimed set. Call after full close."""
    with _claim_lock:
        s = _read_claims()
        if ticker in s:
            s.discard(ticker)
            _write_claims(s)


def list_claims() -> set[str]:
    """Return current claimed set. Used at startup to rehydrate."""
    return _read_claims()


def sync_claims_with_positions(positions: dict) -> None:
    """Rebuild the claimed set from `positions` keys. Called at bot startup
    so the file matches reality after a crash/restart."""
    with _claim_lock:
        s = set(positions.keys())
        _write_claims(s)


def log_trade(rec: dict) -> None:
    _append_jsonl(config.TRADES_PATH, rec)
    try: log_trade_dated(rec)
    except Exception: pass


def log_decision(rec: dict) -> None:
    _append_jsonl(config.DECISIONS_PATH, rec)
    try: log_decision_dated(rec)
    except Exception: pass


def log_shadow_code_decision(rec: dict) -> None:
    """Append a shadow-mode code-only decision record to
    data/shadow_code_decisions.jsonl. Never raises into the bot's hot path.

    Pairs the code-only decision with the LLM's decision for the same packet
    so that after ~7 days we can A/B both against settled outcomes.
    """
    try:
        _append_jsonl(config.SHADOW_CODE_DECISIONS_PATH, rec)
    except Exception as e:
        log.warning("log_shadow_code_decision failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# last_dispatch_state persistence (2026-05-17)
#
# In-memory dict tracking the most-recent LLM dispatch per ticker, used by
# the prescreen to enforce a re-dispatch cooldown. Previously this was
# Runtime-instance state and got wiped on every restart — observed as
# CHI-B85.5 dispatched 3 times in 30 min when the bot restarted twice.
# Persisting to disk so cooldowns survive restarts.
# ─────────────────────────────────────────────────────────────────────────────
_DISPATCH_STATE_PATH = config.DATA_DIR / "last_dispatch_state.json"


def save_dispatch_state(state_dict: dict) -> None:
    """Atomic dump of {ticker: {ts, no_ask_c, rm}} to disk."""
    try:
        tmp = _DISPATCH_STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state_dict, default=str))
        os.replace(tmp, _DISPATCH_STATE_PATH)
    except Exception as e:
        log.warning("save_dispatch_state failed: %s", e)


def load_dispatch_state(prune_older_than_sec: float = 7200.0) -> dict:
    """Load + prune. Default prune: 2h (way past the 30-min cooldown window)."""
    try:
        if not _DISPATCH_STATE_PATH.exists():
            return {}
        data = json.loads(_DISPATCH_STATE_PATH.read_text()) or {}
        import time as _time
        cutoff = _time.time() - prune_older_than_sec
        return {tkr: rec for tkr, rec in data.items()
                if isinstance(rec, dict) and (rec.get("ts") or 0) >= cutoff}
    except Exception as e:
        log.warning("load_dispatch_state failed: %s", e)
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Shared decisions DB — optional integration
# ─────────────────────────────────────────────────────────────────────────────
_decision_log_module = None


def _get_decision_log():
    """Lazy import shared_tools/decision_log.py. Returns module or None."""
    global _decision_log_module
    if _decision_log_module is not None:
        return _decision_log_module
    # Try to import from the live shared_tools directory.
    candidates = [
        Path("/home/ubuntu/shared_tools"),
        Path(__file__).resolve().parent.parent / "shared_tools",
    ]
    for d in candidates:
        if not (d / "decision_log.py").exists():
            continue
        sys.path.insert(0, str(d))
        try:
            import decision_log  # type: ignore
            _decision_log_module = decision_log
            return decision_log
        except Exception as e:
            log.warning("shared_tools/decision_log import failed: %s", e)
            return None
    return None


def record_shared_decision(**kwargs) -> bool:
    """Wrap shared_tools.decision_log.record with bot='paper-judge'.
    Returns True if written (or False if module missing — non-fatal)."""
    mod = _get_decision_log()
    if mod is None:
        return False
    kwargs.setdefault("bot", "paper-judge")
    try:
        return bool(mod.record(**kwargs))
    except Exception as e:
        log.warning("shared decision_log.record failed: %s", e)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Position bookkeeping helpers
# ─────────────────────────────────────────────────────────────────────────────
def upsert_entry(positions: dict[str, dict], ticker: str, entry_rec: dict) -> None:
    """If ticker exists in positions, merge as addon. Otherwise add fresh."""
    existing = positions.get(ticker)
    if existing:
        # Merge — preserves entry timestamp, sums counts/cost, weighted-avg price.
        old_cnt = float(existing.get("count") or 0)
        new_cnt = float(entry_rec.get("count") or 0)
        old_cost = float(existing.get("cost") or 0.0)
        new_cost = float(entry_rec.get("cost") or 0.0)
        total_cnt = old_cnt + new_cnt
        total_cost = old_cost + new_cost
        avg_price = total_cost / total_cnt if total_cnt > 0 else entry_rec.get("entry_price", 0)
        existing["count"] = int(total_cnt)
        existing["cost"] = total_cost
        existing["entry_price"] = round(avg_price, 4)
        existing["last_addon_ts"] = entry_rec.get("ts", time.time())
        existing.setdefault("_addons", []).append({
            "ts": entry_rec.get("ts"),
            "count": int(new_cnt),
            "price": entry_rec.get("entry_price"),
            "reason": entry_rec.get("reason"),
        })
    else:
        positions[ticker] = dict(entry_rec)


def record_exit(positions: dict[str, dict], ticker: str, sell_count: int,
                sell_revenue: float, reason: str) -> Optional[dict]:
    """Reduce position by sell_count. If sell_count >= position count, removes
    the position. Returns the final state of the position (or None if removed),
    and writes an exit row to trades.jsonl."""
    pos = positions.get(ticker)
    if not pos:
        log.warning("record_exit no-op: %s not in positions", ticker)
        return None
    cnt = int(pos.get("count") or 0)
    sold = min(sell_count, cnt)
    cost_basis = float(pos.get("entry_price") or 0) * sold
    pnl = sell_revenue - cost_basis
    log_trade({
        "kind": "exit",
        "market_ticker": ticker,
        "action": pos.get("action"),
        "sell_count": sold,
        "sell_revenue": round(sell_revenue, 4),
        "cost_basis": round(cost_basis, 4),
        "pnl": round(pnl, 4),
        "reason": reason,
        "station": pos.get("station"),
        "date_str": pos.get("date_str"),
    })
    remaining = cnt - sold
    if remaining <= 0:
        positions.pop(ticker, None)
        return None
    pos["count"] = remaining
    pos["cost"] = round(remaining * float(pos.get("entry_price") or 0), 4)
    return pos
