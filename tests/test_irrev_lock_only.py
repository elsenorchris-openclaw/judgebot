"""Tests for the 2026-06-10 IRREVERSIBLE-LOCK-ONLY trading mode
(nn_shadow_worker._irreversible_no_lock + the Gate -1 integration in
_try_auto_execute).

The mode: BUY_NO only, and only when the validated running extreme has
IRREVERSIBLY killed the bracket (HIGH rm >= cap+1F / LOW rm <= floor-1F).
Locked rows bypass the mu-quality gates (blend-only, window, thin-margin,
sigma, front-wind, off-peak) and keep the market/exec gates (price band incl.
the 50c locked floor, spread, caps, cash). Flag off -> legacy path untouched.
"""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402
import low_post_probe  # noqa: E402


def _cand(ticker, station, series_prefix="KXHIGH", kind="B", floor=88.0, cap=89.0, label=88.5):
    return market_universe.Candidate(
        ticker=ticker, series_prefix=series_prefix, city_code="TEST",
        station=station, climate_day="2026-06-10", bracket_kind=kind,
        floor=floor, cap=cap, bracket_label=label, market={},
    )


def _decision(direction="BUY_NO", edge=0.40):
    return {"decision": direction, "edge": edge,
            "p_yes": 0.0 if direction == "BUY_NO" else 1.0,
            "reason": f"{direction} test"}


def _pkt(rm, floor=88.0, cap=89.0, no_ask=60, yes_bid=38, yes_ask=42,
         mu=88.5, sigma=1.17, mu_method="blend_KXHIGH", wind=None):
    p = {"days_out": 0, "running_min_or_max": rm,
         "floor": floor, "cap": cap,
         "no_ask_c": no_ask, "yes_bid_c": yes_bid, "yes_ask_c": yes_ask,
         "mu_chosen": mu, "sigma_chosen": sigma, "mu_method": mu_method,
         "seconds_to_close": 10_000,
         "wethr_obs": ({"wind_speed_mph": wind} if wind is not None else {}),
         "local_clock": {"h_to_peak": -2.0}}
    return p


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._rt = SimpleNamespace(positions={}, cycle_buys_by_station_side={})

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window

    def _run(self, cand, packet, decision, series="HIGH", lock_only=True):
        import paper_judge_bot as pjb
        import kalshi_client
        import config as _cfg
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(low_post_probe, "has_resting", lambda *a, **kw: False), \
             mock.patch.object(low_post_probe, "place",
                               lambda *a, **kw: (True, "posted_mid_test")), \
             mock.patch.object(kalshi_client, "get_balance_cached", return_value=100.0), \
             mock.patch.object(_cfg, "PUSH_MAE_GATE_ENABLED", False), \
             mock.patch.object(_cfg, "PUSH_ONE_BRACKET_PER_STATION_HIGH", False), \
             mock.patch.object(_cfg, "PUSH_IRREV_LOCK_ONLY", lock_only):
            return nsw._try_auto_execute(
                cand, packet, decision, series=series, local_hour=18.0)


class TestIrrevLockDetector(unittest.TestCase):
    def test_high_overshoot_locked(self):
        cand = _cand("KXHIGHTEST-26JUN10-B88.5", "KSEA")
        ok, dbg = nsw._irreversible_no_lock(_pkt(rm=90.0), cand)
        self.assertTrue(ok)
        self.assertIn("HIGH rm", dbg)

    def test_high_below_cap_plus_buffer_not_locked(self):
        cand = _cand("KXHIGHTEST-26JUN10-B88.5", "KSEA")
        ok, _ = nsw._irreversible_no_lock(_pkt(rm=89.9), cand)
        self.assertFalse(ok)

    def test_high_stays_below_is_reversible_not_locked(self):
        # rm far BELOW the bracket + past peak = the premature-lock class -> NOT accepted
        cand = _cand("KXHIGHTEST-26JUN10-B88.5", "KSEA")
        ok, _ = nsw._irreversible_no_lock(_pkt(rm=84.0), cand)
        self.assertFalse(ok)

    def test_high_t_warm_tail_never_no_locked(self):
        # floor-only warm tail: an overshoot makes YES CERTAIN, never NO
        cand = _cand("KXHIGHTEST-26JUN10-T89", "KSEA", kind="T", floor=89.0, cap=None, label=89)
        ok, dbg = nsw._irreversible_no_lock(_pkt(rm=95.0, floor=89.0, cap=None), cand)
        self.assertFalse(ok)
        self.assertIn("high_no_cap_tail", dbg)

    def test_low_undershoot_locked(self):
        cand = _cand("KXLOWTEST-26JUN10-B71.5", "KMIA", "KXLOW", floor=71.0, cap=72.0, label=71.5)
        ok, dbg = nsw._irreversible_no_lock(_pkt(rm=69.9, floor=71.0, cap=72.0), cand)
        self.assertTrue(ok)
        self.assertIn("LOW rm", dbg)

    def test_low_stays_above_is_reversible_not_locked(self):
        cand = _cand("KXLOWTEST-26JUN10-B71.5", "KMIA", "KXLOW", floor=71.0, cap=72.0, label=71.5)
        ok, _ = nsw._irreversible_no_lock(_pkt(rm=74.0, floor=71.0, cap=72.0), cand)
        self.assertFalse(ok)

    def test_not_d0_not_locked(self):
        cand = _cand("KXHIGHTEST-26JUN11-B88.5", "KSEA")
        p = _pkt(rm=90.0)
        p["days_out"] = 1
        ok, dbg = nsw._irreversible_no_lock(p, cand)
        self.assertFalse(ok)
        self.assertIn("not_d0", dbg)


class TestLockOnlyGate(_Base):
    def test_unlocked_buy_blocked_in_mode(self):
        cand = _cand("KXHIGHTEST-26JUN10-B88.5", "KSEA")
        ok, reason = self._run(cand, _pkt(rm=88.0), _decision())
        self.assertFalse(ok)
        self.assertIn("irrev_lock_only", reason)

    def test_buy_yes_blocked_in_mode(self):
        cand = _cand("KXHIGHTEST-26JUN10-B88.5", "KSEA")
        ok, reason = self._run(cand, _pkt(rm=90.0), _decision("BUY_YES"))
        self.assertFalse(ok)
        self.assertIn("BUY_NO only", reason)

    def test_locked_executes_and_bypasses_window_sigma_thinmargin_blendonly(self):
        # window forced OUT + matcher mu + sigma 5.0 (> 2.5 ceiling) + mu inside
        # the thin-margin band -> all four bypasses must hold for this to execute.
        nsw._in_decision_window = lambda *a, **kw: (False, "forced_out")
        cand = _cand("KXHIGHTEST-26JUN10-B88.5", "KSEA")
        pkt = _pkt(rm=90.0, mu=88.5, sigma=5.0, mu_method="nn_match_idw3_n50")
        ok, reason = self._run(cand, pkt, _decision())
        self.assertTrue(ok, reason)
        self.assertIn("executed", reason)
        self.assertTrue(pkt.get("irrev_locked"))

    def test_locked_price_floor_50c(self):
        cand = _cand("KXHIGHTEST-26JUN10-B88.5", "KSEA")
        ok, reason = self._run(cand, _pkt(rm=90.0, no_ask=45), _decision())
        self.assertFalse(ok)
        self.assertIn("price_oor", reason)
        ok2, reason2 = self._run(cand, _pkt(rm=90.0, no_ask=50), _decision())
        self.assertTrue(ok2, reason2)

    def test_locked_price_ceiling_90c_kept(self):
        cand = _cand("KXHIGHTEST-26JUN10-B88.5", "KSEA")
        ok, reason = self._run(cand, _pkt(rm=90.0, no_ask=93), _decision())
        self.assertFalse(ok)
        self.assertIn("price_oor", reason)

    def test_locked_spread_gate_kept(self):
        cand = _cand("KXHIGHTEST-26JUN10-B88.5", "KSEA")
        ok, reason = self._run(cand, _pkt(rm=90.0, yes_bid=10, yes_ask=40), _decision())
        self.assertFalse(ok)
        self.assertIn("spread_too_wide", reason)

    def test_low_locked_executes_bypasses_front_wind(self):
        # 30mph sustained wind would trip the LOW front gate; locked rows bypass it.
        cand = _cand("KXLOWTEST-26JUN10-B71.5", "KMSP", "KXLOW",
                     floor=71.0, cap=72.0, label=71.5)
        pkt = _pkt(rm=69.5, floor=71.0, cap=72.0, no_ask=60, wind=30.0,
                   mu_method="blend_KXLOW")
        ok, reason = self._run(cand, pkt, _decision(), series="LOW")
        self.assertTrue(ok, reason)

    def test_low_unlocked_blocked_in_mode(self):
        cand = _cand("KXLOWTEST-26JUN10-B71.5", "KMSP", "KXLOW",
                     floor=71.0, cap=72.0, label=71.5)
        ok, reason = self._run(cand, _pkt(rm=70.5, floor=71.0, cap=72.0, no_ask=60),
                               _decision(), series="LOW")
        self.assertFalse(ok)
        self.assertIn("irrev_lock_only", reason)

    def test_flag_off_legacy_path_intact(self):
        # mode OFF: a normal unlocked blend row passes the legacy gates and executes.
        # KDFW: no per-station sigma floor (KSEA's 1.41 would block sigma=1.17 since
        # this packet has no mu_pre_blend and thus no blend sigma exemption).
        cand = _cand("KXHIGHTEST-26JUN10-B88.5", "KDFW")
        pkt = _pkt(rm=85.0, mu=84.0, sigma=1.17)  # mu far below bracket: thin-margin passes
        pkt["local_clock"] = {"h_to_peak": 3.0}   # outside the off-peak veto zone
        ok, reason = self._run(cand, pkt, _decision(), lock_only=False)
        self.assertTrue(ok, reason)
        self.assertIn("executed", reason)


if __name__ == "__main__":
    unittest.main()
