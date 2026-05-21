"""Tests for MAE-based confidence sizing (_mae_conf_mult) + tier config.

2026-05-21: a cell's expected pre-peak MAE (override 4th tuple element) scales
the bet size down where the matcher is less reliable. Validated out-of-sample
(corr 0.62 train-MAE vs holdout-MAE). The multiplier ONLY reduces (<=1.0).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402


class TestMaeConfMult(unittest.TestCase):
    def setUp(self):
        self._orig = getattr(config, "PUSH_MAE_CONF_TIERS", None)
        config.PUSH_MAE_CONF_TIERS = [
            (0.0, 1.0, 1.0), (1.0, 1.5, 0.75), (1.5, 2.5, 0.5), (2.5, 99.0, 0.3),
        ]

    def tearDown(self):
        if self._orig is not None:
            config.PUSH_MAE_CONF_TIERS = self._orig

    def test_low_mae_full_size(self):
        self.assertEqual(nsw._mae_conf_mult(0.5), 1.0)
        self.assertEqual(nsw._mae_conf_mult(0.99), 1.0)

    def test_mid_tiers(self):
        self.assertEqual(nsw._mae_conf_mult(1.0), 0.75)   # boundary -> upper tier
        self.assertEqual(nsw._mae_conf_mult(1.3), 0.75)
        self.assertEqual(nsw._mae_conf_mult(1.5), 0.5)
        self.assertEqual(nsw._mae_conf_mult(2.4), 0.5)

    def test_high_mae_minimal(self):
        self.assertEqual(nsw._mae_conf_mult(2.5), 0.3)
        self.assertEqual(nsw._mae_conf_mult(4.3), 0.3)

    def test_none_is_moderate(self):
        self.assertEqual(nsw._mae_conf_mult(None), 0.5)

    def test_never_increases_size(self):
        # The multiplier must never exceed 1.0 for any plausible MAE.
        for mae in [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 5.0, 10.0]:
            self.assertLessEqual(nsw._mae_conf_mult(mae), 1.0)

    def test_monotonic_non_increasing(self):
        maes = [0.5, 1.2, 2.0, 3.0]
        mults = [nsw._mae_conf_mult(m) for m in maes]
        self.assertEqual(mults, sorted(mults, reverse=True))


class TestConfTiersConfig(unittest.TestCase):
    def test_config_tiers_wellformed(self):
        tiers = getattr(config, "PUSH_MAE_CONF_TIERS", None)
        self.assertIsNotNone(tiers)
        for lo, hi, mult in tiers:
            self.assertLess(lo, hi)
            self.assertGreater(mult, 0.0)
            self.assertLessEqual(mult, 1.0)


if __name__ == "__main__":
    unittest.main()
