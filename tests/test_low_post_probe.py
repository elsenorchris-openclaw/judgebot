"""Tests for the LOW posting probe (low_post_probe.py)."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import low_post_probe as lpp


def _cand(ticker="KXLOWTMIA-26MAY24-B78.5"):
    return SimpleNamespace(
        ticker=ticker, station="KMIA", city_code="MIA", floor=78.0, cap=79.0,
        series_prefix="KXLOW", bracket_kind="B", climate_day="2026-05-24")


def _entry_dec(action="BUY_NO"):
    return SimpleNamespace(
        decision=action, read="pure-nn auto: test", conviction=0.85,
        size_factor=1.0, key_risks=["x"], what_would_change_my_mind="y",
        obs_anchor="", obs_anchor_valid=False, obs_anchor_reason="z")


def _rt(positions=None, held=None):
    return SimpleNamespace(
        ctx=SimpleNamespace(now_utc=time.time()),
        positions=positions if positions is not None else {},
        wallet_held_tickers=held if held is not None else set(),
        persist_positions=mock.Mock())


def _row(ticker="KXLOWTMIA-26MAY24-B78.5", side="no", climate_day="2099-01-01",
         order_id="OID1", post_c=53, cross_c=66, cnt=1):
    return {
        "order_id": order_id, "ticker": ticker, "side": side, "post_c": post_c,
        "bid_c": 40, "ask_c": cross_c, "cross_c": cross_c, "spread_c": cross_c - 40,
        "cnt": cnt, "climate_day": climate_day, "placed_ts": time.time(),
        "entry_ctx": {
            "station": "KMIA", "city_code": "MIA", "floor": 78.0, "cap": 79.0,
            "series_prefix": "KXLOW", "bracket_kind": "B", "climate_day": climate_day,
            "action": "BUY_NO", "read": "r", "conviction": 0.85, "size_factor": 1.0,
            "key_risks": [], "what_would_change_my_mind": "", "obs_anchor": "",
            "obs_anchor_valid": False, "obs_anchor_reason": "", "model_prob": 0.86},
    }


class _RegTmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.close()
        Path(self._tmp.name).unlink(missing_ok=True)  # start absent
        self._patch = mock.patch.object(lpp, "_REG_PATH", Path(self._tmp.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        Path(self._tmp.name).unlink(missing_ok=True)


class MidPostTest(unittest.TestCase):
    def test_clamps_to_maker_band(self):
        self.assertEqual(lpp._mid_post_c(40, 66), 53)    # mid 53, in [40,65]
        self.assertEqual(lpp._mid_post_c(50, 52), 51)    # mid 51
        self.assertEqual(lpp._mid_post_c(50, 51), 50)    # 1c spread -> bid
        self.assertEqual(lpp._mid_post_c(59, 100), 80)   # 79.5 -> 80 (banker)

    def test_invalid_returns_none(self):
        self.assertIsNone(lpp._mid_post_c(None, 50))
        self.assertIsNone(lpp._mid_post_c(60, 50))       # ask <= bid
        self.assertIsNone(lpp._mid_post_c(0, 0))


class HasRestingTest(_RegTmp):
    def test_dedup(self):
        self.assertFalse(lpp.has_resting("KXLOWTMIA-26MAY24-B78.5"))
        lpp._save([_row()])
        self.assertTrue(lpp.has_resting("KXLOWTMIA-26MAY24-B78.5"))
        self.assertFalse(lpp.has_resting("KXLOWTDC-26MAY24-B54.5"))


class PlaceTest(_RegTmp):
    def test_place_registers_and_claims(self):
        pkt = {"no_bid_c": 40, "no_ask_c": 66, "seconds_to_close": 9999}
        rt = _rt()
        with mock.patch.object(lpp, "kalshi_client") as kc, \
             mock.patch.object(lpp, "guardrails") as gr, \
             mock.patch.object(lpp, "state") as st, \
             mock.patch.object(lpp, "_discord"):
            kc.get_balance_cached.return_value = 100.0
            kc.place_buy.return_value = {"ok": True, "order_id": "OID1",
                                         "filled": 0, "status": "resting"}
            gr.check_buy.return_value = (True, "ok")
            gr.BuyDecision = mock.Mock()
            ok, reason = lpp.place(rt, _cand(), pkt, _entry_dec(), "no",
                                   {"p_yes": 0.14})
        self.assertTrue(ok)
        # posted at mid 53, not crossed at 66
        kc.place_buy.assert_called_once()
        self.assertEqual(kc.place_buy.call_args[0][3], 53)
        st.claim_ticker.assert_called_once_with("KXLOWTMIA-26MAY24-B78.5")
        rows = lpp._load()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["order_id"], "OID1")
        self.assertEqual(rows[0]["post_c"], 53)
        self.assertEqual(rows[0]["cross_c"], 66)

    def test_place_skips_when_no_bbo(self):
        pkt = {"no_bid_c": None, "no_ask_c": None}
        with mock.patch.object(lpp, "_discord"):
            ok, reason = lpp.place(_rt(), _cand(), pkt, _entry_dec(), "no", {})
        self.assertFalse(ok)
        self.assertIn("no_bbo", reason)
        self.assertEqual(lpp._load(), [])


class SweepTest(_RegTmp):
    def test_adopts_ws_fill(self):
        lpp._save([_row()])  # resting, future climate_day
        rt = _rt()
        with mock.patch.object(lpp, "kalshi_ws") as kw, \
             mock.patch.object(lpp, "guardrails") as gr, \
             mock.patch.object(lpp, "kalshi_client") as kc, \
             mock.patch.object(lpp, "state") as st, \
             mock.patch.object(lpp, "_discord"):
            kw.get_fill.return_value = {"total_count": 1.0,
                                        "total_no_notional_dollars": 0.53}
            gr.today_utc.return_value = "2026-05-24"
            lpp.sweep(rt)
            # adopted -> trade recorded at realized 53c, position upserted
            st.log_trade.assert_called_once()
            rec = st.log_trade.call_args[0][0]
            self.assertEqual(rec["market_ticker"], "KXLOWTMIA-26MAY24-B78.5")
            self.assertEqual(rec["entry_price"], 0.53)
            self.assertEqual(rec["market_price_c"], 53)
            self.assertEqual(rec["judge"]["cross_c"], 66)
            st.upsert_entry.assert_called_once()
            gr.record_buy.assert_called_once()
        self.assertEqual(lpp._load(), [])  # dropped from registry

    def test_position_fallback_adopts(self):
        lpp._save([_row()])
        rt = _rt(held={"KXLOWTMIA-26MAY24-B78.5"})
        with mock.patch.object(lpp, "kalshi_ws") as kw, \
             mock.patch.object(lpp, "guardrails") as gr, \
             mock.patch.object(lpp, "kalshi_client"), \
             mock.patch.object(lpp, "state") as st, \
             mock.patch.object(lpp, "_discord"):
            kw.get_fill.return_value = None  # WS missed/evicted
            gr.today_utc.return_value = "2026-05-24"
            lpp.sweep(rt)
            st.log_trade.assert_called_once()
            rec = st.log_trade.call_args[0][0]
            self.assertEqual(rec["judge"]["adopt_via"], "position_fallback")
            self.assertEqual(rec["market_price_c"], 53)  # adopted at post_c
        self.assertEqual(lpp._load(), [])

    def test_cancels_stale_unfilled(self):
        lpp._save([_row(climate_day="2026-05-20")])  # past -> stale
        rt = _rt()
        with mock.patch.object(lpp, "kalshi_ws") as kw, \
             mock.patch.object(lpp, "guardrails") as gr, \
             mock.patch.object(lpp, "kalshi_client") as kc, \
             mock.patch.object(lpp, "state") as st, \
             mock.patch.object(lpp, "_discord"):
            kw.get_fill.return_value = None
            gr.today_utc.return_value = "2026-05-24"
            lpp.sweep(rt)
            kc.cancel_order.assert_called_once_with("OID1")
            st.release_ticker.assert_called_once_with("KXLOWTMIA-26MAY24-B78.5")
            st.log_trade.assert_not_called()
        self.assertEqual(lpp._load(), [])  # dropped

    def test_keeps_resting_unfilled(self):
        lpp._save([_row(climate_day="2099-01-01")])  # not stale
        rt = _rt()
        with mock.patch.object(lpp, "kalshi_ws") as kw, \
             mock.patch.object(lpp, "guardrails") as gr, \
             mock.patch.object(lpp, "kalshi_client"), \
             mock.patch.object(lpp, "state") as st, \
             mock.patch.object(lpp, "_discord"):
            kw.get_fill.return_value = None
            gr.today_utc.return_value = "2026-05-24"
            lpp.sweep(rt)
            st.log_trade.assert_not_called()
        rows = lpp._load()
        self.assertEqual(len(rows), 1)  # still resting


if __name__ == "__main__":
    unittest.main()
