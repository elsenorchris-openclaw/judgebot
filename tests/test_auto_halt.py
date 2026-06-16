"""Tests for the 2026-06-16 trailing settled-P&L auto-halt
(paper_judge_bot._settled_pnl_by_day + _check_auto_halt). The breaker writes the
KILL file when the bot's own settled P&L bleeds past the configured thresholds.
"""
import os
import sys
import time
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import paper_judge_bot as pjb  # noqa: E402


def _entry(tk, action, ep, cnt, day):
    return json.dumps({"kind": "entry", "market_ticker": tk, "action": action,
                       "entry_price": ep, "count": cnt, "date_str": day})


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.trades = Path(self._tmp) / "trades.jsonl"
        self.kill = Path(self._tmp) / "KILL"
        self.state = Path(self._tmp) / "auto_halt_state.json"
        pjb._auto_halt_last_check[0] = 0.0
        self._patches = [
            mock.patch.object(config, "TRADES_PATH", self.trades),
            mock.patch.object(config, "KILL_SWITCH_PATH", self.kill),
            mock.patch.object(pjb, "_AUTO_HALT_STATE_PATH", self.state),
            mock.patch.object(config, "AUTO_HALT_ENABLED", True),
            mock.patch.object(config, "AUTO_HALT_TRAILING_DAYS", 3),
            mock.patch.object(config, "AUTO_HALT_TRAILING_LOSS_USD", -60.0),
            mock.patch.object(config, "AUTO_HALT_MIN_DAYS", 3),
            mock.patch.object(config, "AUTO_HALT_DAY_LOSS_USD", -75.0),
            mock.patch.object(pjb, "notify_trade", lambda *a, **k: None),
        ]
        for p in self._patches:
            p.start()
        self.rt = SimpleNamespace(ctx=SimpleNamespace(kill_switch_active=False))

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _write(self, rows):
        self.trades.write_text("\n".join(rows) + "\n")

    def _setts(self, mapping):
        return mock.patch.object(pjb.kalshi_client, "list_settlements",
                                 return_value=[{"ticker": tk, "market_result": r}
                                               for tk, r in mapping.items()])


class TestSettledPnlByDay(_Base):
    def test_fee_bearing_win_and_loss(self):
        self._write([
            _entry("KXHIGHNY-26JUN10-B79.5", "BUY_NO", 0.50, 10, "2026-06-10"),
            _entry("KXHIGHCHI-26JUN10-B90.5", "BUY_NO", 0.40, 10, "2026-06-10"),
        ])
        with self._setts({"KXHIGHNY-26JUN10-B79.5": "no",      # NO wins
                          "KXHIGHCHI-26JUN10-B90.5": "yes"}):   # NO loses
            days = pjb._settled_pnl_by_day()
        # win: 10*(1-0.5) - ceil(0.07*.5*.5*10*100)/100 = 5.0-0.18 = 4.82
        # loss: -10*.4 - ceil(.07*.4*.6*10*100)/100 = -4.0-0.17 = -4.17
        self.assertAlmostEqual(days["2026-06-10"], 4.82 + (-4.17), places=2)

    def test_exit_subtracted(self):
        self._write([
            _entry("KXHIGHNY-26JUN10-B79.5", "BUY_NO", 0.50, 10, "2026-06-10"),
            json.dumps({"kind": "exit", "market_ticker": "KXHIGHNY-26JUN10-B79.5",
                        "sell_count": 10}),
        ])
        with self._setts({"KXHIGHNY-26JUN10-B79.5": "no"}):
            days = pjb._settled_pnl_by_day()
        # fully sold -> held=0 -> excluded entirely
        self.assertNotIn("2026-06-10", days)


class TestAutoHalt(_Base):
    def test_no_halt_when_flat(self):
        self._write([_entry("KXHIGHNY-26JUN1%d-B79.5" % i, "BUY_NO", 0.50, 2, "2026-06-1%d" % i)
                     for i in range(3)])
        with self._setts({"KXHIGHNY-26JUN1%d-B79.5" % i: "no" for i in range(3)}):
            pjb._check_auto_halt(self.rt)
        self.assertFalse(self.kill.exists())
        self.assertFalse(self.rt.ctx.kill_switch_active)

    def test_trailing_bleed_halts(self):
        # 3 days each ~-$20.70 (40 contracts @50c NO that all lose) -> -$62.1 <= -$60
        rows, setts = [], {}
        for i in range(3):
            tk = "KXHIGHX%d-26JUN1%d-B79.5" % (i, i)
            rows.append(_entry(tk, "BUY_NO", 0.50, 40, "2026-06-1%d" % i))
            setts[tk] = "yes"  # NO loses
        self._write(rows)
        with self._setts(setts):
            pjb._check_auto_halt(self.rt)
        self.assertTrue(self.kill.exists(), "trailing 3d bleed should have written KILL")
        self.assertIn("trailing", self.kill.read_text())
        self.assertTrue(self.rt.ctx.kill_switch_active)

    def test_min_days_guard(self):
        # only 2 settled days, even if each is bad -> trailing needs >=3 (catastrophe
        # threshold not hit at -$21/day) -> no halt
        rows, setts = [], {}
        for i in range(2):
            tk = "KXHIGHX%d-26JUN1%d-B79.5" % (i, i)
            rows.append(_entry(tk, "BUY_NO", 0.50, 10, "2026-06-1%d" % i))
            setts[tk] = "yes"
        self._write(rows)
        with self._setts(setts):
            pjb._check_auto_halt(self.rt)
        self.assertFalse(self.kill.exists())

    def test_single_day_catastrophe_halts_without_min_days(self):
        # one day, -$80 (10 @ ~88c NO that loses) -> <= -$75 catastrophe, fires on day 1
        self._write([_entry("KXHIGHX-26JUN10-B79.5", "BUY_NO", 0.88, 100, "2026-06-10")])
        with self._setts({"KXHIGHX-26JUN10-B79.5": "yes"}):
            pjb._check_auto_halt(self.rt)
        self.assertTrue(self.kill.exists(), "single catastrophic day should halt")
        self.assertIn("single-day", self.kill.read_text())

    def test_idempotent_when_kill_exists(self):
        self.kill.write_text("manual halt")
        rows, setts = [], {}
        for i in range(3):
            tk = "KXHIGHX%d-26JUN1%d-B79.5" % (i, i)
            rows.append(_entry(tk, "BUY_NO", 0.50, 10, "2026-06-1%d" % i))
            setts[tk] = "yes"
        self._write(rows)
        with self._setts(setts):
            pjb._check_auto_halt(self.rt)
        # KILL preserved as-is, not overwritten
        self.assertEqual(self.kill.read_text(), "manual halt")

    def test_disabled_flag(self):
        with mock.patch.object(config, "AUTO_HALT_ENABLED", False):
            self._write([_entry("KXHIGHX-26JUN10-B79.5", "BUY_NO", 0.88, 100, "2026-06-10")])
            with self._setts({"KXHIGHX-26JUN10-B79.5": "yes"}):
                pjb._check_auto_halt(self.rt)
        self.assertFalse(self.kill.exists())

    def test_resume_is_sticky_same_data_does_not_rehalt(self):
        # catastrophe halts; ack watermark written; after `rm KILL` the SAME data
        # must NOT re-halt (otherwise resume is impossible).
        self._write([_entry("KXHIGHX-26JUN10-B79.5", "BUY_NO", 0.88, 100, "2026-06-10")])
        with self._setts({"KXHIGHX-26JUN10-B79.5": "yes"}):
            pjb._check_auto_halt(self.rt)
            self.assertTrue(self.kill.exists())
            self.assertTrue(self.state.exists(), "ack watermark should be persisted")
            # operator resumes
            self.kill.unlink()
            self.rt.ctx.kill_switch_active = False
            pjb._auto_halt_last_check[0] = 0.0   # bypass throttle for the test
            pjb._check_auto_halt(self.rt)
        self.assertFalse(self.kill.exists(), "same acknowledged data must not re-halt after resume")

    def test_fresh_catastrophe_after_resume_rehalts(self):
        # after a halt+resume, a NEW catastrophic day (newer than the watermark) re-halts.
        self._write([_entry("KXHIGHX-26JUN10-B79.5", "BUY_NO", 0.88, 100, "2026-06-10")])
        with self._setts({"KXHIGHX-26JUN10-B79.5": "yes"}):
            pjb._check_auto_halt(self.rt)
        self.kill.unlink(); self.rt.ctx.kill_switch_active = False
        pjb._auto_halt_last_check[0] = 0.0
        # a new, newer bad day arrives
        self._write([_entry("KXHIGHX-26JUN10-B79.5", "BUY_NO", 0.88, 100, "2026-06-10"),
                     _entry("KXHIGHY-26JUN11-B79.5", "BUY_NO", 0.88, 100, "2026-06-11")])
        with self._setts({"KXHIGHX-26JUN10-B79.5": "yes", "KXHIGHY-26JUN11-B79.5": "yes"}):
            pjb._check_auto_halt(self.rt)
        self.assertTrue(self.kill.exists(), "a fresh catastrophe after resume must re-halt")
        self.assertIn("2026-06-11", self.kill.read_text())

    def test_recompute_throttle(self):
        # second call within 5 min short-circuits (last_check set) -> no compute
        pjb._auto_halt_last_check[0] = time.time()
        self._write([_entry("KXHIGHX-26JUN10-B79.5", "BUY_NO", 0.88, 100, "2026-06-10")])
        with self._setts({"KXHIGHX-26JUN10-B79.5": "yes"}):
            pjb._check_auto_halt(self.rt)
        self.assertFalse(self.kill.exists(), "throttled call should not compute/halt")


if __name__ == "__main__":
    unittest.main()
