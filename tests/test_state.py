"""Tests for state.py — positions bookkeeping. No I/O against real disk."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import state


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    """Redirect state.py paths into a tmp directory.

    2026-05-16: also stub `state._today_dir()` so the date-partitioned
    mirror in `log_trade()` / `log_decision()` writes into tmp_path
    instead of `data/by_date/{today}/…`. Without this, every pytest run
    of test_log_trade_appends / test_log_decision_appends leaks fixture
    records (e.g. ticker="KX1") into the production date-partitioned
    log, which corrupts retroactive analysis.
    """
    monkeypatch.setattr(config, "POSITIONS_PATH", tmp_path / "positions.json")
    monkeypatch.setattr(config, "TRADES_PATH", tmp_path / "trades.jsonl")
    monkeypatch.setattr(config, "DECISIONS_PATH", tmp_path / "decisions.jsonl")
    by_date = tmp_path / "by_date_today"
    by_date.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(state, "_today_dir", lambda: by_date)
    return tmp_path


# ─────────────────────────────────────────────────────────────────────────────
# positions.json round-trip
# ─────────────────────────────────────────────────────────────────────────────
def test_save_load_positions_atomic(tmp_data):
    state.save_positions({"KX1": {"count": 10, "cost": 6.0}})
    out = state.load_positions()
    assert out == {"KX1": {"count": 10, "cost": 6.0}}


def test_load_positions_missing_returns_empty(tmp_data):
    assert state.load_positions() == {}


def test_load_positions_corrupt_returns_empty(tmp_data):
    config.POSITIONS_PATH.write_text("{{not json")
    assert state.load_positions() == {}


# ─────────────────────────────────────────────────────────────────────────────
# upsert_entry: fresh + addon merge
# ─────────────────────────────────────────────────────────────────────────────
def test_upsert_fresh_position():
    p: dict = {}
    state.upsert_entry(p, "KX1", {
        "count": 10, "entry_price": 0.5, "cost": 5.0,
        "action": "BUY_NO", "ts": 100.0,
    })
    assert p["KX1"]["count"] == 10
    assert p["KX1"]["entry_price"] == 0.5


def test_upsert_addon_merges_count_and_avg_price():
    p: dict = {
        "KX1": {"count": 10, "entry_price": 0.50, "cost": 5.0, "action": "BUY_NO"}
    }
    state.upsert_entry(p, "KX1", {
        "count": 10, "entry_price": 0.60, "cost": 6.0,
        "action": "BUY_NO", "ts": 200.0,
    })
    assert p["KX1"]["count"] == 20
    assert p["KX1"]["cost"] == 11.0
    # Weighted avg: (5 + 6) / 20 = 0.55
    assert p["KX1"]["entry_price"] == 0.55
    assert "_addons" in p["KX1"]
    assert len(p["KX1"]["_addons"]) == 1


def test_upsert_multiple_addons():
    p: dict = {}
    state.upsert_entry(p, "KX1", {"count": 5, "entry_price": 0.40, "cost": 2.0, "action": "BUY_NO"})
    state.upsert_entry(p, "KX1", {"count": 5, "entry_price": 0.50, "cost": 2.5, "action": "BUY_NO"})
    state.upsert_entry(p, "KX1", {"count": 10, "entry_price": 0.60, "cost": 6.0, "action": "BUY_NO"})
    assert p["KX1"]["count"] == 20
    # (2 + 2.5 + 6) / 20 = 0.525
    assert p["KX1"]["entry_price"] == 0.525


# ─────────────────────────────────────────────────────────────────────────────
# record_exit: partial + full
# ─────────────────────────────────────────────────────────────────────────────
def test_full_exit_removes_position(tmp_data):
    p = {"KX1": {"count": 10, "entry_price": 0.50, "cost": 5.0,
                 "action": "BUY_NO", "station": "KDCA", "date_str": "2026-05-13"}}
    remaining = state.record_exit(p, "KX1", sell_count=10, sell_revenue=4.0, reason="test")
    assert remaining is None
    assert "KX1" not in p
    # Trade log line written
    lines = config.TRADES_PATH.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "exit"
    assert rec["sell_count"] == 10
    assert rec["pnl"] == -1.0  # 4 - 5


def test_partial_exit_keeps_position(tmp_data):
    p = {"KX1": {"count": 10, "entry_price": 0.50, "cost": 5.0,
                 "action": "BUY_NO", "station": "KDCA", "date_str": "2026-05-13"}}
    remaining = state.record_exit(p, "KX1", sell_count=4, sell_revenue=2.0, reason="test")
    assert remaining is not None
    assert p["KX1"]["count"] == 6
    assert p["KX1"]["cost"] == 3.0


def test_exit_count_clamped_to_position_size(tmp_data):
    p = {"KX1": {"count": 5, "entry_price": 0.50, "cost": 2.5,
                 "action": "BUY_NO", "station": "KDCA", "date_str": "2026-05-13"}}
    state.record_exit(p, "KX1", sell_count=20, sell_revenue=1.0, reason="test")
    assert "KX1" not in p
    lines = config.TRADES_PATH.read_text().strip().splitlines()
    rec = json.loads(lines[0])
    assert rec["sell_count"] == 5  # clamped


def test_exit_on_missing_ticker_is_noop(tmp_data):
    p = {}
    assert state.record_exit(p, "KXNONE", sell_count=5, sell_revenue=1.0, reason="x") is None
    assert not config.TRADES_PATH.exists() or config.TRADES_PATH.read_text() == ""


# ─────────────────────────────────────────────────────────────────────────────
# jsonl logs
# ─────────────────────────────────────────────────────────────────────────────
def test_log_trade_appends(tmp_data):
    state.log_trade({"kind": "entry", "x": 1})
    state.log_trade({"kind": "exit", "x": 2})
    lines = config.TRADES_PATH.read_text().strip().splitlines()
    assert len(lines) == 2
    recs = [json.loads(l) for l in lines]
    assert recs[0]["x"] == 1 and "ts" in recs[0]


def test_log_decision_appends(tmp_data):
    state.log_decision({"ticker": "KX1", "decision": "SKIP"})
    line = config.DECISIONS_PATH.read_text().strip()
    rec = json.loads(line)
    assert rec["ticker"] == "KX1" and "ts" in rec
