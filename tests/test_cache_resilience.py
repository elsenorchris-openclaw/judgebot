"""Tests for the 2026-06-15 cache-resilience fix (code audit): the blend's data
helpers must NOT pin a transient failure for the full cache TTL, and one bad
orderbook must not discard a whole REST ladder.
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
    return {"yes_dollars": [[("%.4f" % (yes_bid_c / 100.0)), "10"]],
            "no_dollars": [[("%.4f" % (no_bid_c / 100.0)), "10"]]}


class TestCacheTsHelper(unittest.TestCase):
    def test_honest_result_full_ttl(self):
        now = 1000.0
        self.assertEqual(nsw._cache_ts(now, 180.0, True, 20.0), now)

    def test_failure_aged_to_backoff(self):
        now = 1000.0
        ts = nsw._cache_ts(now, 180.0, False, 20.0)
        # entry should read as ~backoff seconds from expiry: now - (ttl - backoff)
        self.assertAlmostEqual(ts, now - 160.0)
        # i.e. (now - ts) == 160, so it expires (>=180) after another 20s
        self.assertTrue((now - ts) < 180.0)            # still valid right now
        self.assertTrue((now + 21 - ts) >= 180.0)      # expired ~20s later


class TestMarketMuExceptionBackoff(unittest.TestCase):
    def setUp(self):
        nsw._market_mu_cache.clear()
        self._orig = dict(kws._bbo_cache)
        kws._bbo_cache.clear()

    def tearDown(self):
        kws._bbo_cache.clear(); kws._bbo_cache.update(self._orig)

    def test_exception_not_pinned_full_ttl(self):
        # force an exception inside the body by making parse_ticker raise
        kws._bbo_cache["KXHIGHTSEA-26JUN15-B82.5"] = {
            "yes_bid": 0.33, "yes_ask": 0.35, "ts": time.time()}
        with mock.patch.object(nsw.market_universe, "parse_ticker",
                               side_effect=RuntimeError("boom")):
            mu = nsw._compute_market_mu("KSEA", "2026-06-15", "KXHIGH")
        self.assertIsNone(mu)
        ts, val = nsw._market_mu_cache[("KSEA", "2026-06-15", "KXHIGH")]
        # a transient exception must be aged (expire fast), NOT cached at full 180s
        self.assertLess(ts, time.time() - 100.0, "exception result should be aged for fast retry")


class TestRestLadderPerBracketResilience(unittest.TestCase):
    def setUp(self):
        nsw._rest_ladder_cache.clear()
        nsw._mktmu_rest_recover[0] = 0

    def test_one_bad_orderbook_keeps_the_rest(self):
        markets = {"markets": [{"ticker": "KXHIGHTSEA-26JUN15-B%d.5" % f}
                               for f in range(80, 85)]}
        good = {
            "KXHIGHTSEA-26JUN15-B80.5": _ob(12, 86),
            "KXHIGHTSEA-26JUN15-B81.5": _ob(24, 74),
            # B82.5 raises (simulated 429)
            "KXHIGHTSEA-26JUN15-B83.5": _ob(22, 76),
            "KXHIGHTSEA-26JUN15-B84.5": _ob(10, 88),
        }
        def _ob_side(tk):
            if tk == "KXHIGHTSEA-26JUN15-B82.5":
                raise RuntimeError("429")
            return good[tk]
        with mock.patch.object(kc, "get", side_effect=lambda p, q=None: markets), \
             mock.patch.object(kc, "get_orderbook", side_effect=_ob_side):
            br = nsw._rest_ladder_brackets({"KXHIGHTSEA-26JUN15"}, "KSEA", "2026-06-15", set())
        # 4 good brackets survive the one raise (old code returned 0)
        self.assertEqual(len(br), 4, "one bad orderbook must not discard the whole ladder")

    def test_list_failure_not_cached_long(self):
        with mock.patch.object(kc, "get", side_effect=RuntimeError("429")):
            br = nsw._rest_ladder_brackets({"KXHIGHTSEA-26JUN15"}, "KSEA", "2026-06-15", set())
        self.assertEqual(br, [])
        ts, val = nsw._rest_ladder_cache["KXHIGHTSEA-26JUN15"]
        # list-fetch failure must be aged for fast retry, not pinned 120s
        self.assertLess(ts, time.time() - 90.0)


if __name__ == "__main__":
    unittest.main()
