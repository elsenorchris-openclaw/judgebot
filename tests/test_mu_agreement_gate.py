"""Tests for nn_shadow_worker._try_auto_execute mu_agreement gate (HIGH).

Shipped 2026-05-25 (asymmetric: matcher > NWP); made symmetric 2026-05-26
(|matcher - NWP| > threshold) so it catches BOTH matcher-too-hot AND
matcher-too-cold regimes. The 5/23-5/24 calibration failure was matcher
COLD vs reality; the original one-sided gate would have missed it.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402


def _make_candidate(ticker, station, series_prefix, climate_day,
                    bracket_kind="B", floor=88.0, cap=89.0, label=88.5):
    return market_universe.Candidate(
        ticker=ticker, series_prefix=series_prefix, city_code="TEST",
        station=station, climate_day=climate_day, bracket_kind=bracket_kind,
        floor=floor, cap=cap, bracket_label=label, market={},
    )


def _make_packet(mu, mu_nwp, floor=88.0, cap=89.0):
    """Packet with mu_chosen and nwp_disagree. Far-out mu so the boundary gate
    doesn't fire (we're testing mu_agreement, not boundary)."""
    return {
        "yes_ask_c": 50, "no_ask_c": 50, "seconds_to_close": 10_000,
        "mu_chosen": mu, "floor": floor, "cap": cap,
        "mu_nwp": mu_nwp,
        "nwp_disagree": (mu - mu_nwp) if mu_nwp is not None else None,
        "sigma_chosen": 2.0,  # well above the σ floor so that gate doesn't fire
        "local_clock": {"h_to_peak": 2.0},
    }


def _make_decision(direction):
    return {
        "decision": direction, "edge": 0.20,
        "p_yes": 0.30 if direction == "BUY_NO" else 0.70,
        "reason": f"{direction} test edge=20.0pp",
    }


class TestMuAgreementGate(unittest.TestCase):

    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window

    def _run(self, cand, packet, decision, series="HIGH"):
        import paper_judge_bot as pjb
        import kalshi_client
        import config as _cfg
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(kalshi_client, "get_balance_cached",
                               return_value=100.0), \
             mock.patch.object(_cfg, "PUSH_MAE_GATE_ENABLED", False):
            return nsw._try_auto_execute(
                cand, packet, decision, series=series, local_hour=12.0,
            )

    def test_matcher_too_hot_blocks(self):
        """Original (asymmetric) behavior: matcher >> NWP -> SKIP.
        μ=95, μ_NWP=88 -> disagree=+7 -> abs > 2.0 -> blocked."""
        cand = _make_candidate("KXHIGHMIA-26MAY26-B85.5", "KMIA",
                                "KXHIGH", "2026-05-26",
                                floor=85.0, cap=86.0)
        executed, reason = self._run(cand, _make_packet(95.0, 88.0, floor=85.0, cap=86.0),
                                     _make_decision("BUY_NO"))
        self.assertFalse(executed)
        self.assertIn("nwp_disagree", reason)

    def test_matcher_too_cold_blocks_symmetric(self):
        """2026-05-26 (new): matcher << NWP -> SKIP (symmetric form).
        μ=80, μ_NWP=88 -> disagree=-8 -> abs > 2.0 -> blocked.
        Under the OLD asymmetric form this would have been ALLOWED."""
        cand = _make_candidate("KXHIGHMIA-26MAY26-B85.5", "KMIA",
                                "KXHIGH", "2026-05-26",
                                floor=85.0, cap=86.0)
        executed, reason = self._run(cand, _make_packet(80.0, 88.0, floor=85.0, cap=86.0),
                                     _make_decision("BUY_NO"))
        self.assertFalse(executed)
        self.assertIn("nwp_disagree", reason)

    def test_matcher_agrees_allowed(self):
        """|disagree| <= threshold -> allowed."""
        cand = _make_candidate("KXHIGHMIA-26MAY26-B85.5", "KMIA",
                                "KXHIGH", "2026-05-26",
                                floor=85.0, cap=86.0)
        executed, reason = self._run(cand, _make_packet(95.0, 94.0, floor=85.0, cap=86.0),
                                     _make_decision("BUY_NO"))
        self.assertNotIn("nwp_disagree", reason)

    def test_mu_nwp_null_fails_open(self):
        """If mu_nwp / nwp_disagree is None, gate does NOT fire (fail-open).
        Matches the design comment: 'if mu_nwp unavailable, do not gate'."""
        cand = _make_candidate("KXHIGHMIA-26MAY26-B85.5", "KMIA",
                                "KXHIGH", "2026-05-26",
                                floor=85.0, cap=86.0)
        executed, reason = self._run(cand, _make_packet(95.0, None, floor=85.0, cap=86.0),
                                     _make_decision("BUY_NO"))
        self.assertNotIn("nwp_disagree", reason)

    def test_gate_disabled_flag(self):
        """USE_MU_AGREEMENT_GATE=False bypasses the gate entirely."""
        import config as _cfg
        cand = _make_candidate("KXHIGHMIA-26MAY26-B85.5", "KMIA",
                                "KXHIGH", "2026-05-26",
                                floor=85.0, cap=86.0)
        with mock.patch.object(_cfg, "USE_MU_AGREEMENT_GATE", False):
            executed, reason = self._run(cand, _make_packet(95.0, 80.0, floor=85.0, cap=86.0),
                                         _make_decision("BUY_NO"))
        self.assertNotIn("nwp_disagree", reason)

    def test_low_series_not_affected(self):
        """LOW series is not subject to this gate (HIGH only)."""
        cand = _make_candidate("KXLOWMIA-26MAY26-B72.5", "KMIA",
                                "KXLOW", "2026-05-26",
                                floor=72.0, cap=73.0, label=72.5)
        pkt = _make_packet(60.0, 75.0, floor=72.0, cap=73.0)
        executed, reason = self._run(cand, pkt, _make_decision("BUY_NO"), series="LOW")
        self.assertNotIn("nwp_disagree", reason)


if __name__ == "__main__":
    unittest.main()
