"""Regression tests for state.save/load_dispatch_state (2026-05-17)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import state


@pytest.fixture
def tmp_state_path(tmp_path, monkeypatch):
    p = tmp_path / "last_dispatch_state.json"
    monkeypatch.setattr(state, "_DISPATCH_STATE_PATH", p)
    return p


def test_save_load_roundtrip(tmp_state_path):
    now = time.time()
    d = {
        "KXHIGHTABC-26MAY17-B89.5": {"ts": now, "no_ask_c": 35, "rm": 82.0},
        "KXLOWTXYZ-26MAY17-T70": {"ts": now - 100, "no_ask_c": 80, "rm": 71.0},
    }
    state.save_dispatch_state(d)
    out = state.load_dispatch_state()
    assert out.keys() == d.keys()
    for tk in d:
        assert out[tk]["ts"] == pytest.approx(d[tk]["ts"], abs=0.01)
        assert out[tk]["no_ask_c"] == d[tk]["no_ask_c"]
        assert out[tk]["rm"] == d[tk]["rm"]


def test_load_prunes_stale_entries(tmp_state_path):
    """Entries older than the prune window are dropped on load."""
    now = time.time()
    d = {
        "FRESH": {"ts": now, "no_ask_c": 10, "rm": 50},
        "STALE_3H": {"ts": now - 3 * 3600, "no_ask_c": 20, "rm": 60},
    }
    state.save_dispatch_state(d)
    out = state.load_dispatch_state(prune_older_than_sec=7200.0)  # 2h
    assert "FRESH" in out
    assert "STALE_3H" not in out


def test_load_missing_file_returns_empty(tmp_state_path):
    assert not tmp_state_path.exists()
    assert state.load_dispatch_state() == {}


def test_load_corrupt_file_returns_empty(tmp_state_path):
    tmp_state_path.write_text("not valid json{{{")
    out = state.load_dispatch_state()
    assert out == {}
