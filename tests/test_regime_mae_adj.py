"""Tests for the global regime-MAE adjustment helpers (2026-05-21)."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402


class TestRegimeBuckets(unittest.TestCase):
    def test_sigma(self):
        self.assertEqual(nsw._rt_sigma_bucket(0.9), "low")
        self.assertEqual(nsw._rt_sigma_bucket(2.0), "mid")
        self.assertEqual(nsw._rt_sigma_bucket(3.0), "high")
        self.assertIsNone(nsw._rt_sigma_bucket(None))

    def test_sky(self):
        for c in ("CLR", "SKC", "FEW", "few"):
            self.assertEqual(nsw._rt_sky_bucket(c), "clear")
        self.assertEqual(nsw._rt_sky_bucket("SCT"), "partly")
        for c in ("BKN", "OVC", "VV"):
            self.assertEqual(nsw._rt_sky_bucket(c), "cloudy")
        self.assertIsNone(nsw._rt_sky_bucket(None))
        self.assertIsNone(nsw._rt_sky_bucket(""))

    def test_wind(self):
        self.assertEqual(nsw._rt_wind_bucket(3.0), "calm")     # ~2.6 kt
        self.assertEqual(nsw._rt_wind_bucket(12.0), "moderate")  # ~10.4 kt
        self.assertEqual(nsw._rt_wind_bucket(25.0), "strong")    # ~21.7 kt
        self.assertIsNone(nsw._rt_wind_bucket(None))

    def test_anomaly(self):
        nsw._climate_normals = {"KMIA": {"05-21": [None] * 14 + [80.0] + [None] * 9}}
        nsw._regime_tables_loaded = True
        # hour 14, normal 80; cur 90 -> +10 -> hot
        self.assertEqual(nsw._rt_anomaly_bucket("KMIA", "2026-05-21", 14, 90.0), "hot")
        # cur 70 -> -10 -> cold
        self.assertEqual(nsw._rt_anomaly_bucket("KMIA", "2026-05-21", 14, 70.0), "cold")
        # cur 82 -> +2 -> normal
        self.assertEqual(nsw._rt_anomaly_bucket("KMIA", "2026-05-21", 14, 82.0), "normal")
        # missing normal hour -> None
        self.assertIsNone(nsw._rt_anomaly_bucket("KMIA", "2026-05-21", 3, 82.0))
        self.assertIsNone(nsw._rt_anomaly_bucket("KXXX", "2026-05-21", 14, 82.0))


class TestRegimeAdjustedMae(unittest.TestCase):
    def setUp(self):
        self._d, self._n, self._l = (nsw._regime_deltas, nsw._climate_normals,
                                     nsw._regime_tables_loaded)
        nsw._regime_deltas = {
            "sigma": {"low": -0.33, "high": 0.71},
            "anomaly": {"hot": 0.98, "cold": -0.67, "normal": -0.15},
            "sky": {"clear": -0.26, "cloudy": 0.51},
            "wind": {"calm": -0.26, "strong": 0.75},
        }
        nsw._climate_normals = {"KMIA": {"05-21": [None] * 14 + [80.0] + [None] * 9}}
        nsw._regime_tables_loaded = True
        self._damp = getattr(config, "PUSH_REGIME_MAE_DAMP", 0.6)
        config.PUSH_REGIME_MAE_DAMP = 0.6

    def tearDown(self):
        nsw._regime_deltas, nsw._climate_normals, nsw._regime_tables_loaded = (
            self._d, self._n, self._l)
        config.PUSH_REGIME_MAE_DAMP = self._damp

    def _cand(self):
        return SimpleNamespace(station="KMIA", climate_day="2026-05-21")

    def test_hard_regime_raises_mae(self):
        # hot + cloudy + strong wind + high sigma -> all positive deltas
        pkt = {"wethr_obs": {"temp_f": 92.0, "cloud_1_coverage": "OVC", "wind_speed_mph": 25.0},
               "local_clock": {"local_hour": 14}}
        nn_res = {"sigma_natural": 3.0}
        adj, dbg = nsw._regime_adjusted_mae(1.4, self._cand(), pkt, nn_res)
        # raw_delta = 0.98+0.51+0.75+0.71 = 2.95; *0.6 = 1.77; adj = 1.4+1.77 = 3.17
        self.assertGreater(adj, 1.4)
        self.assertAlmostEqual(adj, round(1.4 + 0.6 * (0.98 + 0.51 + 0.75 + 0.71), 3), places=2)

    def test_easy_regime_lowers_mae(self):
        # cold + clear + calm + low sigma -> all negative deltas
        pkt = {"wethr_obs": {"temp_f": 70.0, "cloud_1_coverage": "CLR", "wind_speed_mph": 3.0},
               "local_clock": {"local_hour": 14}}
        nn_res = {"sigma_natural": 0.9}
        adj, dbg = nsw._regime_adjusted_mae(1.4, self._cand(), pkt, nn_res)
        self.assertLess(adj, 1.4)

    def test_floor_at_0_1(self):
        pkt = {"wethr_obs": {"temp_f": 70.0, "cloud_1_coverage": "CLR", "wind_speed_mph": 3.0},
               "local_clock": {"local_hour": 14}}
        nn_res = {"sigma_natural": 0.9}
        adj, _ = nsw._regime_adjusted_mae(0.2, self._cand(), pkt, nn_res)
        self.assertGreaterEqual(adj, 0.1)

    def test_none_cell_mae_passthrough(self):
        adj, dbg = nsw._regime_adjusted_mae(None, self._cand(), {}, {})
        self.assertIsNone(adj)

    def test_no_deltas_returns_cell_mae(self):
        nsw._regime_deltas = {}
        adj, _ = nsw._regime_adjusted_mae(1.4, self._cand(),
                                          {"wethr_obs": {}, "local_clock": {}}, {})
        self.assertEqual(adj, 1.4)


if __name__ == "__main__":
    unittest.main()
