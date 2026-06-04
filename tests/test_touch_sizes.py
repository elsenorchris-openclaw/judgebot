"""kalshi_ws.get_touch_sizes — touch depth (best-bid size per book) for the
decision-log liquidity features added 2026-06-04 (future fill/spread-filter design).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kalshi_ws as k  # noqa: E402


class TestTouchSizes(unittest.TestCase):
    def setUp(self):
        self._yb = {kk: dict(v) if isinstance(v, dict) else v for kk, v in k._yes_bids.items()}
        self._nb = {kk: dict(v) if isinstance(v, dict) else v for kk, v in k._no_bids.items()}

    def tearDown(self):
        k._yes_bids.clear(); k._yes_bids.update(self._yb)
        k._no_bids.clear(); k._no_bids.update(self._nb)

    def test_best_bid_size(self):
        # best bid = highest price; report the size resting there
        k._yes_bids["T"] = {10: 5, 12: 3}
        k._no_bids["T"] = {80: 7, 78: 9}
        self.assertEqual(k.get_touch_sizes("T"), (3, 7))

    def test_empty_book(self):
        self.assertEqual(k.get_touch_sizes("NOPE"), (None, None))

    def test_one_side_only(self):
        k._yes_bids["Y"] = {20: 4}
        self.assertEqual(k.get_touch_sizes("Y"), (4, None))

    def test_failsafe_on_bad_state(self):
        k._yes_bids["B"] = "corrupt"   # must never raise -> (None, None)
        self.assertEqual(k.get_touch_sizes("B"), (None, None))


if __name__ == "__main__":
    unittest.main()
