"""Edge-case tests for the unified maker-first TAKER-FALLBACK (low_post_probe).

Each test exercises one row of the design's edge-case table and asserts whether
the CROSS (kalshi_client.place_buy) is called — the proof of the double-buy guard:
the taker cross fires ONLY after a CONFIRMED-dead, ZERO-filled, not-held order.
cf the unified maker/taker build, 2026-06-03 (Chris).
"""
import os
import sys
import time as _time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import low_post_probe as lpp  # noqa: E402


def _row(**over):
    r = {"order_id": "OID1", "ticker": "KXLOWTMIA-26JUN03-B71.5", "side": "no",
         "post_c": 45, "cross_c": 50, "cnt": 2, "climate_day": "2026-06-03",
         "placed_ts": 1.0, "ttl_s": 0, "model_prob": 0.80,
         "fallback_deadline_ts": 1.0, "entry_ctx": {"station": "KMIA"}}
    r.update(over)
    return r


class _Base(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.rt = SimpleNamespace(positions={}, wallet_held_tickers=set())
        self.kc = mock.patch.object(lpp, "kalshi_client").start()
        self.kws = mock.patch.object(lpp, "kalshi_ws").start()
        self.state = mock.patch.object(lpp, "state").start()
        self.adopt = mock.patch.object(lpp, "_adopt").start()
        mock.patch.object(lpp, "_discord", lambda *a, **k: None).start()
        # real fill-count parse on the mocked client
        self.kc.order_filled_count.side_effect = (
            lambda o: int(float((o or {}).get("fill_count_fp") or 0)))
        self.kws.get_fill.return_value = None
        self.kc.open_position_tickers.return_value = set()
        # feature ON by default for these tests
        mock.patch.object(lpp.config, "TAKER_FALLBACK_ENABLED", True).start()
        mock.patch.object(lpp.config, "TAKER_FALLBACK_MIN_EDGE_PP", 8.0).start()
        mock.patch.object(lpp.config, "TAKER_FALLBACK_MAX_CROSS_C", 90).start()
        mock.patch.object(lpp.config, "TAKER_CROSS_EXPIRY_S", 5).start()

    def _fb(self, row=None, held=None):
        return lpp._taker_fallback(self.rt, row or _row(), held or set())


class TestTakerFallbackSafety(_Base):
    def test_double_buy_blocked_when_maker_filled(self):
        # our order fully filled -> adopt, NEVER cross (the core double-buy guard).
        self.kc.get_order.return_value = {"status": "canceled", "fill_count_fp": "2", "remaining_count": 0}
        self.assertTrue(self._fb())
        self.adopt.assert_called_once()
        self.kc.place_buy.assert_not_called()

    def test_cancel_unconfirmed_retries(self):
        # get_order unavailable -> cannot confirm dead -> retry, never cross.
        self.kc.get_order.return_value = None
        self.assertFalse(self._fb())
        self.kc.place_buy.assert_not_called()
        self.adopt.assert_not_called()

    def test_order_still_live_retries(self):
        # order still resting (remaining>0) -> cancel didn't take -> retry, no cross.
        self.kc.get_order.return_value = {"status": "resting", "fill_count_fp": "0", "remaining_count": 2}
        self.assertFalse(self._fb())
        self.kc.place_buy.assert_not_called()

    def test_partial_maker_fill_adopts_not_cross(self):
        # 1 of 2 filled before cancel -> adopt the 1, do NOT cross the remainder.
        self.kc.get_order.return_value = {"status": "canceled", "fill_count_fp": "1", "remaining_count": 0}
        self.kws.get_fill.return_value = {"total_count": "1", "total_no_notional_dollars": 0.45}
        self.assertTrue(self._fb())
        self.adopt.assert_called_once()
        self.assertEqual(self.adopt.call_args[0][2], 1)        # adopted 1
        self.kc.place_buy.assert_not_called()

    def test_coexist_skip_when_held_by_other(self):
        # dead + 0 fill on OUR order, but the wallet holds the ticker (another bot)
        # -> skip: no cross, no adopt.
        self.kc.get_order.return_value = {"status": "canceled", "fill_count_fp": "0", "remaining_count": 0}
        self.kc.open_position_tickers.return_value = {"KXLOWTMIA-26JUN03-B71.5"}
        self.assertTrue(self._fb())
        self.kc.place_buy.assert_not_called()
        self.adopt.assert_not_called()
        self.state.release_ticker.assert_called()


class TestTakerFallbackCross(_Base):
    @mock.patch.object(lpp, "_cur_side_ask_c", return_value=48)
    def test_clean_cross_when_dead_zero_fill_not_held(self, _ask):
        # confirmed dead + 0 fill + not held + edge holds -> cross once, adopt the fill.
        self.kc.get_order.return_value = {"status": "canceled", "fill_count_fp": "0", "remaining_count": 0}
        self.kc.place_buy.return_value = {"ok": True, "filled": 2, "status": "executed", "order_id": "X2"}
        self.assertTrue(self._fb())
        self.kc.place_buy.assert_called_once()
        args = self.kc.place_buy.call_args[0]
        self.assertEqual(args[0], "KXLOWTMIA-26JUN03-B71.5")
        self.assertEqual(args[1], "no")
        self.assertEqual(args[2], 2)                            # full count
        self.assertEqual(args[3], 48)                           # at the live ask
        self.assertEqual(self.adopt.call_args[0][2], 2)         # adopted 2
        self.assertEqual(self.adopt.call_args[1].get("via"), "taker_fallback")

    @mock.patch.object(lpp, "_cur_side_ask_c", return_value=75)
    def test_edge_gone_walks(self, _ask):
        # model_prob 0.80, ask 75c -> edge 5pp < 8pp -> walk, no cross.
        self.kc.get_order.return_value = {"status": "canceled", "fill_count_fp": "0", "remaining_count": 0}
        self.assertTrue(self._fb())
        self.kc.place_buy.assert_not_called()
        self.state.release_ticker.assert_called()

    @mock.patch.object(lpp, "_cur_side_ask_c", return_value=95)
    def test_ask_too_expensive_walks(self, _ask):
        # edge would pass at min=1pp, but ask 95 > max_cross 90 -> walk.
        self.kc.get_order.return_value = {"status": "canceled", "fill_count_fp": "0", "remaining_count": 0}
        with mock.patch.object(lpp.config, "TAKER_FALLBACK_MIN_EDGE_PP", 1.0):
            self.assertTrue(self._fb(row=_row(model_prob=0.99)))
        self.kc.place_buy.assert_not_called()

    @mock.patch.object(lpp, "_cur_side_ask_c", return_value=None)
    def test_no_live_ask_walks(self, _ask):
        self.kc.get_order.return_value = {"status": "canceled", "fill_count_fp": "0", "remaining_count": 0}
        self.assertTrue(self._fb())
        self.kc.place_buy.assert_not_called()
        self.state.release_ticker.assert_called()

    @mock.patch.object(lpp, "_cur_side_ask_c", return_value=48)
    def test_disabled_guard_walks(self, _ask):
        # even if reached with the flag OFF, never cross.
        self.kc.get_order.return_value = {"status": "canceled", "fill_count_fp": "0", "remaining_count": 0}
        with mock.patch.object(lpp.config, "TAKER_FALLBACK_ENABLED", False):
            self.assertTrue(self._fb())
        self.kc.place_buy.assert_not_called()

    @mock.patch.object(lpp, "_cur_side_ask_c", return_value=48)
    def test_cross_zero_fill_releases(self, _ask):
        # cross rejected (e.g. insufficient balance) -> no adopt, release claim.
        self.kc.get_order.return_value = {"status": "canceled", "fill_count_fp": "0", "remaining_count": 0}
        self.kc.place_buy.return_value = {"ok": False, "filled": 0, "error_code": "insufficient_balance"}
        self.assertTrue(self._fb())
        self.kc.place_buy.assert_called_once()
        self.adopt.assert_not_called()
        self.state.release_ticker.assert_called()


class TestSweepGating(_Base):
    """sweep() must call the fallback ONLY for past-deadline orders, and never when
    the feature is disabled (inert deploy)."""
    def _sweep_with(self, rows):
        with mock.patch.object(lpp, "_load", return_value=list(rows)), \
             mock.patch.object(lpp, "_save"), \
             mock.patch.object(lpp, "_taker_fallback", return_value=True) as fb:
            lpp.sweep(SimpleNamespace(positions={}, wallet_held_tickers=set()))
        return fb

    def test_not_past_deadline_no_fallback(self):
        fb = self._sweep_with([_row(order_id="A", fallback_deadline_ts=_time.time() + 9999)])
        fb.assert_not_called()

    def test_past_deadline_invokes_fallback(self):
        fb = self._sweep_with([_row(order_id="A", fallback_deadline_ts=_time.time() - 10)])
        fb.assert_called_once()

    def test_disabled_is_inert(self):
        # flag OFF + past deadline + future climate day -> stays resting, no fallback.
        with mock.patch.object(lpp.config, "TAKER_FALLBACK_ENABLED", False):
            fb = self._sweep_with([_row(order_id="A", fallback_deadline_ts=_time.time() - 10,
                                        climate_day="2099-01-01")])
        fb.assert_not_called()


if __name__ == "__main__":
    unittest.main()
