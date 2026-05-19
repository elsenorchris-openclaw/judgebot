"""obs_refresh.py — background thread that keeps NWS obs cache continuously fresh.

Polls each station's NWS observations on a 90s cadence so any Claude worker
reading the cache always sees data ≤ 90s old with no per-decision network
wait. Started once at bot startup; runs for the lifetime of the process.

2026-05-16: removed wethr polling. The wethr branch populated wethr_client's
local _obs_cache but the bot reads wethr from /home/ubuntu/shared/wethr_cache.json
(owned by wethr-cache-service). The poll was a closed loop — fetching for a
cache nothing else consumed. Saves ~96 wethr API calls/day + ~20s of
background work every 5min.
"""
from __future__ import annotations

import logging
import threading
import time

import config
import obs_client


log = logging.getLogger("judge.obs_refresh")

_thread: threading.Thread | None = None
_stop_evt = threading.Event()

NWS_INTERVAL_SEC = 90.0


def _loop():
    """Refresh loop. Runs until _stop_evt is set."""
    last_nws = 0.0
    log.info("obs_refresh background loop started (NWS-only)")
    while not _stop_evt.is_set():
        now = time.time()
        if (now - last_nws) >= NWS_INTERVAL_SEC:
            for st in config.STATIONS:
                if _stop_evt.is_set(): break
                try:
                    obs_client.get_obs(st)  # has its own 60s cache; this primes it
                except Exception as e:
                    log.debug("NWS refresh %s: %s", st, e)
            last_nws = time.time()
        # Short sleep so we react to shutdown quickly
        _stop_evt.wait(5.0)
    log.info("obs_refresh background loop stopped")


def start() -> None:
    """Idempotent — starts the daemon thread if not already running."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_evt.clear()
    _thread = threading.Thread(target=_loop, name="obs_refresh", daemon=True)
    _thread.start()


def stop() -> None:
    _stop_evt.set()
