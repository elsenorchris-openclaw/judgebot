"""Decision lock: HIGH matcher runs on the FULL climate-day curve (no 180-min
truncation), matching LOW and the regime the shipped push windows were built on.

2026-05-25: NN_LOOKBACK_HIGH_MIN flipped 180 -> 0. The 180-min truncation fed the
matcher a different mu than the windows (which derive from the per_hour_quality
backtest on the FULL morning curve) and caused live no-fire for sparse-feed
stations. Faithful full-vs-180 backtest: per-bet EV ~unchanged, full adds coverage.

If this test fails because NN_LOOKBACK_HIGH_MIN was changed back to a positive
value, re-read that rationale before "fixing" the test -- reverting reintroduces
the window/mu mismatch and the sparse-feed no-fire.

Behavioral coverage of the truncation itself lives in nn_shadow (a 0 lookback
means the `if _lb_min > 0:` truncation block is skipped, so the full trajectory
reaches predict()).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402


class TestNNLookbackHigh(unittest.TestCase):
    def test_high_lookback_is_full_curve(self):
        self.assertEqual(config.NN_LOOKBACK_HIGH_MIN, 0,
                         "HIGH must run on the full curve (0); see module docstring "
                         "before reverting to a positive truncation.")

    def test_high_matches_low_lookback(self):
        # Both sides now use the full climate-day trajectory.
        self.assertEqual(config.NN_LOOKBACK_HIGH_MIN, config.NN_LOOKBACK_LOW_MIN)


if __name__ == "__main__":
    unittest.main()
