"""low_post_probe._fallback_deadline_and_ttl (2026-06-04, Chris).

The LOW maker now rests until the taker-fallback deadline instead of a 90s TTL, so
the cross reliably fires on thin pre-dawn books (the 90s churn relied on the
event-driven eval re-posting, which is WS-quiet at dawn -> 6/4 had 14 posts, 10
ttl_expired, 0 crosses, 0 LOW fills).
"""
import os
import sys
import time as _time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import low_post_probe as lpp  # noqa: E402


class TestFallbackDeadlineTtl(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(lpp.config, "TAKER_FALLBACK_ENABLED", True).start()
        mock.patch.object(lpp.config, "TAKER_FALLBACK_LEAD_H", 0.2).start()
        mock.patch.object(lpp.config, "PUSH_LOW_POST_TTL_S", 90).start()
        mock.patch.object(lpp.config, "BLEND_DEEP_WINDOW_HOURS_LOW", (3.0, 1.5)).start()
        mock.patch.object(lpp.config, "BLEND_DEEP_WINDOW_HOURS", (4.0, 2.5)).start()
        mock.patch.object(lpp.config, "TAKER_FALLBACK_MAX_REST_S", 600).start()

    def test_low_rests_until_deadline_not_90s(self):
        # h_to_min 2.5 -> deadline = now + (2.5 - 1.5 - 0.2)h = 0.8h = 2880s; ttl matches
        dl, ttl = lpp._fallback_deadline_and_ttl({"local_clock": {"h_to_min": 2.5}}, True)
        self.assertAlmostEqual(dl - _time.time(), 2880, delta=5)
        self.assertAlmostEqual(ttl, 2880, delta=5)      # the fix: rests to deadline, not 90

    def test_low_past_deadline_floors_at_30(self):
        # h_to_min 1.6 < window-close+lead 1.7 -> deadline ~now; ttl floored at 30s
        dl, ttl = lpp._fallback_deadline_and_ttl({"local_clock": {"h_to_min": 1.6}}, True)
        self.assertLessEqual(dl - _time.time(), 1)
        self.assertEqual(ttl, 30)

    def test_low_fallback_disabled_uses_base_ttl(self):
        with mock.patch.object(lpp.config, "TAKER_FALLBACK_ENABLED", False):
            _, ttl = lpp._fallback_deadline_and_ttl({"local_clock": {"h_to_min": 2.5}}, True)
        self.assertEqual(ttl, 90)

    def test_high_uses_base_ttl(self):
        # is_low False -> base ttl (HIGH is taker; helper still returns base, not a long rest)
        _, ttl = lpp._fallback_deadline_and_ttl({"local_clock": {"h_to_peak": 3.0}}, False)
        self.assertEqual(ttl, 90)

    def test_missing_h_to_evt_uses_max_rest(self):
        dl, ttl = lpp._fallback_deadline_and_ttl({"local_clock": {}}, True)
        self.assertAlmostEqual(dl - _time.time(), 600, delta=5)
        self.assertAlmostEqual(ttl, 600, delta=2)   # int() truncation may shave 1s


if __name__ == "__main__":
    unittest.main()
