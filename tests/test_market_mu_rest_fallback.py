"""Tests for the 2026-06-13 REST ladder-recovery in _compute_market_mu:
when the WS BBO cache is too sparse (<3 fresh two-sided brackets) the blend
fetches the live orderbook ladder before going dark, so a fat Kalshi ladder
hidden by a sparse cache still gets priced. Keeps the n>=3 quality bar.
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nn_shadow_worker as nsw  # noqa: E402
import kalshi_ws as kws  # noqa: E402
import kalshi_client as kc  # noqa: E402


def _ob(yes_bid_c, no_bid_c):
    """orderbook_fp shape: *_dollars = bids, best at end."""
    return {"yes_dollars": [[("%.4f" % (yes_bid_c / 100.0)), "10"]],
            "no_dollars": [[("%.4f" % (no_bid_c / 100.0)), "10"]]}


# A fat SEA ladder (9 brackets) as it lives on Kalshi via REST.
_SEA_DAY = "2026-06-13"
_SEA_MARKETS = [{"ticker": "KXHIGHTSEA-26JUN13-B%d.5" % f} for f in range(78, 87)]
# yes_bid/no_bid per bracket -> a coherent ladder centered ~83F
_SEA_BOOKS = {
    "KXHIGHTSEA-26JUN13-B78.5": _ob(2, 96),
    "KXHIGHTSEA-26JUN13-B79.5": _ob(5, 93),
    "KXHIGHTSEA-26JUN13-B80.5": _ob(12, 86),
    "KXHIGHTSEA-26JUN13-B81.5": _ob(24, 74),
    "KXHIGHTSEA-26JUN13-B82.5": _ob(33, 65),
    "KXHIGHTSEA-26JUN13-B83.5": _ob(22, 76),
    "KXHIGHTSEA-26JUN13-B84.5": _ob(10, 88),
    "KXHIGHTSEA-26JUN13-B85.5": _ob(4, 94),
    "KXHIGHTSEA-26JUN13-B86.5": _ob(2, 97),
}


class TestMarketMuRestFallback(unittest.TestCase):
    def setUp(self):
        # reset caches + counters so each test is isolated
        nsw._market_mu_cache.clear()
        nsw._rest_ladder_cache.clear()
        nsw._mktmu_rest_recover[0] = 0
        nsw._mktmu_rest_recover[1] = 0
        self._orig_cache = dict(kws._bbo_cache)
        kws._bbo_cache.clear()

    def tearDown(self):
        kws._bbo_cache.clear()
        kws._bbo_cache.update(self._orig_cache)

    def _sparse_cache(self):
        # only ONE bracket present in the WS cache, fresh
        kws._bbo_cache["KXHIGHTSEA-26JUN13-B82.5"] = {
            "yes_bid": 0.33, "yes_ask": 0.35, "ts": time.time()}

    def _markets(self, path, params=None):
        assert "markets" in path
        return {"markets": _SEA_MARKETS}

    def test_rest_recovers_sparse_cache(self):
        self._sparse_cache()
        with mock.patch.object(kc, "get", side_effect=self._markets), \
             mock.patch.object(kc, "get_orderbook", side_effect=lambda tk: _SEA_BOOKS.get(tk, {})):
            mu = nsw._compute_market_mu("KSEA", _SEA_DAY, "KXHIGH")
        self.assertIsNotNone(mu, "blend should price SEA via REST ladder recovery")
        self.assertTrue(80.0 < mu < 86.0, "implied mu should land in the ladder, got %s" % mu)
        self.assertEqual(nsw._mktmu_rest_recover[1], 1, "should count a recovery to 3+")

    def test_flag_off_stays_dark(self):
        self._sparse_cache()
        import config as _cfg
        with mock.patch.object(_cfg, "BLEND_MARKET_MU_REST_FALLBACK", False), \
             mock.patch.object(kc, "get", side_effect=self._markets), \
             mock.patch.object(kc, "get_orderbook", side_effect=lambda tk: _SEA_BOOKS.get(tk, {})):
            mu = nsw._compute_market_mu("KSEA", _SEA_DAY, "KXHIGH")
        self.assertIsNone(mu, "with the flag off, a sparse cache stays dark (matcher fallback)")

    def test_genuinely_thin_ladder_stays_dark(self):
        # Real ladder is genuinely thin: total distinct two-sided brackets < 3.
        # Cache has B82.5; REST surfaces only that same bracket (deduped) plus a
        # one-sided book (no no_dollars -> not two-sided) -> total stays < 3 -> None.
        self._sparse_cache()
        thin = [{"ticker": "KXHIGHTSEA-26JUN13-B82.5"},
                {"ticker": "KXHIGHTSEA-26JUN13-B83.5"}]
        thin_books = {
            "KXHIGHTSEA-26JUN13-B82.5": _ob(33, 65),
            "KXHIGHTSEA-26JUN13-B83.5": {"yes_dollars": [["0.2200", "10"]], "no_dollars": []},
        }
        with mock.patch.object(kc, "get", side_effect=lambda p, q=None: {"markets": thin}), \
             mock.patch.object(kc, "get_orderbook", side_effect=lambda tk: thin_books.get(tk, {})):
            mu = nsw._compute_market_mu("KSEA", _SEA_DAY, "KXHIGH")
        self.assertIsNone(mu, "a genuinely thin real ladder must NOT be forced to a price")

    def test_fat_cache_skips_rest(self):
        # 3 fresh cached brackets -> no REST call at all
        now = time.time()
        for f, yb, ya in [(81, 0.24, 0.26), (82, 0.33, 0.35), (83, 0.22, 0.24)]:
            kws._bbo_cache["KXHIGHTSEA-26JUN13-B%d.5" % f] = {
                "yes_bid": yb, "yes_ask": ya, "ts": now}
        called = {"n": 0}
        def _boom(*a, **k):
            called["n"] += 1
            raise AssertionError("REST should not be called when cache is fat")
        with mock.patch.object(kc, "get", side_effect=_boom):
            mu = nsw._compute_market_mu("KSEA", _SEA_DAY, "KXHIGH")
        self.assertIsNotNone(mu)
        self.assertEqual(called["n"], 0)
        self.assertEqual(nsw._mktmu_rest_recover[0], 0)


if __name__ == "__main__":
    unittest.main()
