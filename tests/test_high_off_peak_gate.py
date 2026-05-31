"""Tests for nn_shadow_worker._try_auto_execute (3.5) HIGH off-peak entry veto.

Shipped 2026-05-31 (JUDGE-ONLY). Skip a NEW HIGH BUY (NO or YES) once the observed
temp has fallen >= PUSH_HIGH_SKIP_IF_OFF_PEAK_F below the day's running max
(drop = traj_max - cur_tmpf) AND we are within PUSH_HIGH_OFF_PEAK_MAX_H2PK hours of
peak. The h_to_peak guard exempts the deep windows (AUS/BOS/HOU/DFW, h2pk>=2.5).
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402


def _make_candidate(direction):
    # BUY_NO: mu far BELOW bracket (NO safe, no thin-margin). BUY_YES: mu INSIDE.
    return market_universe.Candidate(
        ticker="KXHIGHMIA-26MAY31-B85.5", series_prefix="KXHIGH", city_code="MIA",
        station="KMIA", climate_day="2026-05-31", bracket_kind="B",
        floor=85.0, cap=86.0, bracket_label=85.5, market={},
    )


def _make_packet(traj_max, cur_tmpf, h_to_peak, direction):
    mu = 85.5 if direction == "BUY_YES" else 75.0   # YES inside bracket, NO far below
    return {
        "yes_ask_c": 40, "yes_bid_c": 38, "no_ask_c": 60, "no_bid_c": 58,
        "seconds_to_close": 10_000,
        "mu_chosen": mu, "floor": 85.0, "cap": 86.0,
        "sigma_chosen": 2.0,                 # within sigma floor/ceiling
        "local_clock": {"h_to_peak": h_to_peak},
        "traj_max": traj_max, "cur_tmpf": cur_tmpf,
    }


def _make_decision(direction):
    return {
        "decision": direction, "edge": 0.20,
        "p_yes": 0.30 if direction == "BUY_NO" else 0.70,
        "reason": f"{direction} test edge=20.0pp",
    }


class TestHighOffPeakGate(unittest.TestCase):

    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window

    def _run(self, direction, traj_max, cur_tmpf, h_to_peak,
             series="HIGH", off_peak_f=1.0, max_h2pk=2.0):
        cand = _make_candidate(direction)
        packet = _make_packet(traj_max, cur_tmpf, h_to_peak, direction)
        decision = _make_decision(direction)
        import paper_judge_bot as pjb
        import kalshi_client
        import config as _cfg
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(kalshi_client, "get_balance_cached", return_value=100.0), \
             mock.patch.object(_cfg, "PUSH_MAE_GATE_ENABLED", False), \
             mock.patch.object(_cfg, "USE_MU_AGREEMENT_GATE", False), \
             mock.patch.object(_cfg, "PUSH_HIGH_SKIP_IF_OFF_PEAK_F", off_peak_f), \
             mock.patch.object(_cfg, "PUSH_HIGH_OFF_PEAK_MAX_H2PK", max_h2pk):
            return nsw._try_auto_execute(
                cand, packet, decision, series=series, local_hour=12.0,
            )

    def test_off_peak_blocks_buy_no(self):
        # drop = 85 - 83.5 = 1.5 >= 1.0, h2pk 1.0 <= 2.0 -> BLOCK
        executed, reason = self._run("BUY_NO", 85.0, 83.5, 1.0)
        self.assertFalse(executed)
        self.assertIn("high_off_peak", reason)

    def test_off_peak_blocks_buy_yes(self):
        executed, reason = self._run("BUY_YES", 85.0, 83.5, 1.0)
        self.assertFalse(executed)
        self.assertIn("high_off_peak", reason)

    def test_temp_at_max_allowed(self):
        # drop = 85 - 84.7 = 0.3 < 1.0 -> PASS (temp still at its max)
        executed, reason = self._run("BUY_NO", 85.0, 84.7, 1.0)
        self.assertNotIn("high_off_peak", reason)

    def test_deep_window_exempt(self):
        # drop 1.5 >= 1.0 but h2pk 3.0 > 2.0 (deep, AUS/BOS/HOU/DFW) -> PASS
        executed, reason = self._run("BUY_NO", 85.0, 83.5, 3.0)
        self.assertNotIn("high_off_peak", reason)

    def test_disabled_flag(self):
        # PUSH_HIGH_SKIP_IF_OFF_PEAK_F = 0 -> gate off
        executed, reason = self._run("BUY_NO", 85.0, 83.5, 1.0, off_peak_f=0.0)
        self.assertNotIn("high_off_peak", reason)

    def test_missing_traj_max_fails_open(self):
        executed, reason = self._run("BUY_NO", None, 83.5, 1.0)
        self.assertNotIn("high_off_peak", reason)

    def test_missing_h_to_peak_fails_open(self):
        executed, reason = self._run("BUY_NO", 85.0, 83.5, None)
        self.assertNotIn("high_off_peak", reason)

    def test_low_series_not_affected(self):
        # LOW never subject to this HIGH-only gate
        executed, reason = self._run("BUY_NO", 85.0, 83.5, 1.0, series="LOW")
        self.assertNotIn("high_off_peak", reason)


if __name__ == "__main__":
    unittest.main()
