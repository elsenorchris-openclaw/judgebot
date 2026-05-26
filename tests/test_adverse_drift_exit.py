"""Tests for nn_shadow_worker._check_adverse_drift_exit (2026-05-26).

The adverse-drift stop-loss sells a held paper-judge position when its held-side
BID drifts >= ADVERSE_DRIFT_EXIT_PP cents below the entry baseline, sustained for
ADVERSE_DRIFT_SUSTAIN_MIN minutes, within ADVERSE_DRIFT_WINDOW_MIN of entry.
Conservative: filters momentary dips, exempts pre-baseline positions, sells at bid.
"""
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402


def _cand(ticker="KXHIGHMIA-26MAY26-B89.5", station="KMIA", series="KXHIGH"):
    return market_universe.Candidate(
        ticker=ticker, series_prefix=series, city_code="TEST", station=station,
        climate_day="2026-05-26", bracket_kind="B", floor=89.0, cap=90.0,
        bracket_label=89.5, market={})


def _pos(action="BUY_NO", entry_bid_c=40, count=5, age_min=5):
    return {"action": action, "entry_bid_c": entry_bid_c, "count": count,
            "entry_ts_epoch": time.time() - age_min*60, "opened_by": "paper-judge"}


def _pkt(no_bid_c=40, yes_bid_c=58):
    return {"no_bid_c": no_bid_c, "yes_bid_c": yes_bid_c, "seconds_to_close": 10_000}


class TestAdverseDriftExit(unittest.TestCase):
    def setUp(self):
        self._orig_rt = nsw._rt
        nsw._drift_breach.clear()
        self.sells = []
        import config
        self._cfg = config
        config.ENABLE_ADVERSE_DRIFT_EXIT = True
        config.ADVERSE_DRIFT_EXIT_PP = 10
        config.ADVERSE_DRIFT_WINDOW_MIN = 60
        config.ADVERSE_DRIFT_SUSTAIN_MIN = 15

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._drift_breach.clear()

    def _set_pos(self, pos):
        nsw._rt = SimpleNamespace(positions={"KXHIGHMIA-26MAY26-B89.5": pos})

    def _run(self, cand, pkt):
        import paper_judge_bot as pjb
        with mock.patch.object(pjb, "execute_sell",
                               lambda rt, tk, pos, p, dec: self.sells.append((tk, dec))):
            return nsw._check_adverse_drift_exit(cand, pkt)

    def test_first_breach_no_sell(self):
        """First time bid breaches threshold → start clock, no sell yet."""
        self._set_pos(_pos(action="BUY_NO", entry_bid_c=40))
        sold = self._run(_cand(), _pkt(no_bid_c=28))  # drifted 12c < base-10
        self.assertFalse(sold)
        self.assertEqual(len(self.sells), 0)
        self.assertIn("KXHIGHMIA-26MAY26-B89.5", nsw._drift_breach)

    def test_sustained_breach_sells(self):
        """Breach sustained >= 15 min → sell at the current bid."""
        self._set_pos(_pos(action="BUY_NO", entry_bid_c=40))
        # Seed a breach 16 min ago, then re-check with bid still down.
        nsw._drift_breach["KXHIGHMIA-26MAY26-B89.5"] = time.time() - 16*60
        sold = self._run(_cand(), _pkt(no_bid_c=28))
        self.assertTrue(sold)
        self.assertEqual(len(self.sells), 1)
        tk, dec = self.sells[0]
        self.assertEqual(dec.decision, "SELL_ALL")
        self.assertEqual(dec.limit_price_cents, 28)
        self.assertEqual(dec.sell_count, 5)

    def test_recovery_resets_clock(self):
        """Bid recovers above threshold → breach clock resets, no sell."""
        self._set_pos(_pos(action="BUY_NO", entry_bid_c=40))
        nsw._drift_breach["KXHIGHMIA-26MAY26-B89.5"] = time.time() - 16*60
        sold = self._run(_cand(), _pkt(no_bid_c=35))  # only 5c down < 10c threshold
        self.assertFalse(sold)
        self.assertEqual(len(self.sells), 0)
        self.assertNotIn("KXHIGHMIA-26MAY26-B89.5", nsw._drift_breach)

    def test_no_baseline_holds(self):
        """Pre-baseline position (no entry_bid_c) is never stopped."""
        pos = _pos(); pos.pop("entry_bid_c")
        self._set_pos(pos)
        nsw._drift_breach["KXHIGHMIA-26MAY26-B89.5"] = time.time() - 16*60
        self.assertFalse(self._run(_cand(), _pkt(no_bid_c=28)))
        self.assertEqual(len(self.sells), 0)

    def test_past_window_holds(self):
        """Beyond the watch window (entry > 60 min ago) → hold to settlement."""
        self._set_pos(_pos(action="BUY_NO", entry_bid_c=40, age_min=90))
        nsw._drift_breach["KXHIGHMIA-26MAY26-B89.5"] = time.time() - 16*60
        self.assertFalse(self._run(_cand(), _pkt(no_bid_c=28)))
        self.assertEqual(len(self.sells), 0)

    def test_disabled_flag(self):
        self._cfg.ENABLE_ADVERSE_DRIFT_EXIT = False
        self._set_pos(_pos(action="BUY_NO", entry_bid_c=40))
        nsw._drift_breach["KXHIGHMIA-26MAY26-B89.5"] = time.time() - 16*60
        self.assertFalse(self._run(_cand(), _pkt(no_bid_c=28)))
        self.assertEqual(len(self.sells), 0)

    def test_foreign_position_untouched(self):
        """A position opened by another bot is never sold."""
        pos = _pos(); pos["opened_by"] = "v2-max"
        self._set_pos(pos)
        nsw._drift_breach["KXHIGHMIA-26MAY26-B89.5"] = time.time() - 16*60
        self.assertFalse(self._run(_cand(), _pkt(no_bid_c=28)))
        self.assertEqual(len(self.sells), 0)

    def test_buy_yes_uses_yes_bid(self):
        """BUY_YES position measures drift on the yes bid."""
        self._set_pos(_pos(action="BUY_YES", entry_bid_c=58))
        nsw._drift_breach["KXHIGHMIA-26MAY26-B89.5"] = time.time() - 16*60
        sold = self._run(_cand(), _pkt(yes_bid_c=45))  # 13c down on yes
        self.assertTrue(sold)
        self.assertEqual(self.sells[0][1].limit_price_cents, 45)


if __name__ == "__main__":
    unittest.main()
