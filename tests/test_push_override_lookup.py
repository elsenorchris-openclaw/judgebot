"""Tests for nn_shadow_worker._lookup_push_override (MAE/bias logging helper).

2026-05-21: overrides ship (before, after, bias, mae) 4-tuples. The lookup
helper surfaces the matched entry for per-decision logging (and future
sizing). Must handle 4/3/2-tuples gracefully and respect the config flag.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402
import push_window_overrides as pwo  # noqa: E402


class TestLookupPushOverride(unittest.TestCase):
    def setUp(self):
        self._orig_flag = getattr(config, "USE_PUSH_WINDOW_OVERRIDES", True)
        self._orig_dict = pwo.PUSH_WINDOW_OVERRIDES
        config.USE_PUSH_WINDOW_OVERRIDES = True

    def tearDown(self):
        config.USE_PUSH_WINDOW_OVERRIDES = self._orig_flag
        pwo.PUSH_WINDOW_OVERRIDES = self._orig_dict

    def test_four_tuple_returns_bias_and_mae(self):
        pwo.PUSH_WINDOW_OVERRIDES = {("KATL", "HIGH", 1): (1.5, 0.0, -0.371, 1.856)}
        out = nsw._lookup_push_override("KATL", "HIGH", "2026-01-15")
        self.assertEqual(out["before"], 1.5)
        self.assertEqual(out["after"], 0.0)
        self.assertEqual(out["bias"], -0.371)
        self.assertEqual(out["mae"], 1.856)
        self.assertEqual(out["src"], "unconditional")

    def test_three_tuple_mae_none(self):
        pwo.PUSH_WINDOW_OVERRIDES = {("KATL", "HIGH", 1): (1.5, 0.0, -0.371)}
        out = nsw._lookup_push_override("KATL", "HIGH", "2026-01-15")
        self.assertEqual(out["bias"], -0.371)
        self.assertIsNone(out["mae"])

    def test_two_tuple_bias_and_mae_none(self):
        pwo.PUSH_WINDOW_OVERRIDES = {("KATL", "HIGH", 1): (1.5, 0.0)}
        out = nsw._lookup_push_override("KATL", "HIGH", "2026-01-15")
        self.assertEqual(out["before"], 1.5)
        self.assertIsNone(out["bias"])
        self.assertIsNone(out["mae"])

    def test_none_mae_in_tuple(self):
        pwo.PUSH_WINDOW_OVERRIDES = {("KATL", "LOW", 6): (2.5, 0.0, 0.0, None)}
        out = nsw._lookup_push_override("KATL", "LOW", "2026-06-15")
        self.assertEqual(out["before"], 2.5)
        self.assertIsNone(out["mae"])

    def test_missing_entry_returns_none(self):
        pwo.PUSH_WINDOW_OVERRIDES = {("KATL", "HIGH", 1): (1.5, 0.0, -0.371, 1.856)}
        self.assertIsNone(nsw._lookup_push_override("KDEN", "HIGH", "2026-01-15"))

    def test_flag_off_returns_none(self):
        config.USE_PUSH_WINDOW_OVERRIDES = False
        pwo.PUSH_WINDOW_OVERRIDES = {("KATL", "HIGH", 1): (1.5, 0.0, -0.371, 1.856)}
        self.assertIsNone(nsw._lookup_push_override("KATL", "HIGH", "2026-01-15"))

    def test_bad_climate_day_returns_none(self):
        pwo.PUSH_WINDOW_OVERRIDES = {("KATL", "HIGH", 1): (1.5, 0.0, -0.371, 1.856)}
        self.assertIsNone(nsw._lookup_push_override("KATL", "HIGH", "garbage"))


if __name__ == "__main__":
    unittest.main()
