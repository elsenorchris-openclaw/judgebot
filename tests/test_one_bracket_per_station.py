"""Tests for the 2026-06-05 one-bracket-per-station-day HIGH gate (Chris).

PUSH_ONE_BRACKET_PER_STATION_HIGH caps HIGH at 1 bracket/station-day across BOTH
directions, committing the MAX-EDGE bracket among currently-quoted siblings. Backtest
(14mo, judge_dyn/blend_rows.pkl): one-best-bracket/station cuts worst-5% station-day
drawdown ~3x (-$1930 -> -$636) AND lifts per-stn-day Sharpe 0.085->0.089 — the 2nd/3rd
legs are low-edge + correlated (same forecast), so they add more variance than return.
A greedy first-qualify cap would risk committing the WORST leg (Sharpe -> 0.022), so the
gate only commits when NO currently-quoted sibling has a higher edge.

The LEGACY per-(station,direction) cap (rollback path, flag=False) stays covered by
test_high_no_cap.py / test_position_cap_climate_day.py.
"""
import os
import sys
import time
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import kalshi_client  # noqa: E402
import kalshi_ws  # noqa: E402
import low_post_probe  # noqa: E402
import market_universe  # noqa: E402
import nn_shadow_worker as nsw  # noqa: E402
import paper_judge_bot as pjb  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────
def _cand(ticker, series_prefix="KXHIGH", station="KMIA", city="MIA",
          day="2026-05-31", floor=85.0, cap=86.0, label=85.5):
    return market_universe.Candidate(
        ticker=ticker, series_prefix=series_prefix, city_code=city,
        station=station, climate_day=day, bracket_kind="B",
        floor=floor, cap=cap, bracket_label=label, market={})


def _packet(direction, series, floor, cap, yes_bid_c=38, yes_ask_c=40):
    mu = (floor + cap) / 2.0 if direction == "BUY_YES" else (
        floor - 10.0 if series == "HIGH" else cap + 10.0)
    return {
        "yes_ask_c": yes_ask_c, "yes_bid_c": yes_bid_c,
        "no_ask_c": 100 - yes_bid_c, "no_bid_c": 100 - yes_ask_c,
        "seconds_to_close": 10_000, "mu_chosen": mu, "floor": floor, "cap": cap,
        "sigma_chosen": 2.0, "local_clock": {"h_to_peak": 3.0},
        "traj_max": cap, "cur_tmpf": cap,
    }


def _decision(direction):
    return {"decision": direction, "edge": 0.20,
            "p_yes": 0.30 if direction == "BUY_NO" else 0.70,
            "reason": f"{direction} test edge=20.0pp"}


def _pos(station, action, day="2026-05-31", cost=0.40):
    return {"station": station, "action": action, "cost": cost, "date_str": day}


# ── unit: _bracket_edge_pp ───────────────────────────────────────────────────
class TestBracketEdgePP(unittest.TestCase):
    def test_no_side_high_edge_when_mu_far_below(self):
        e = nsw._bracket_edge_pp(85.0, 86.0, 30, 34, 82.73, 1.17)
        self.assertIsNotNone(e)
        self.assertGreater(e, 15.0)

    def test_matches_explicit_normal_integral(self):
        import math
        mu, sg, fl, cp, yb, ya = 87.85, 1.17, 87.0, 88.0, 45, 49
        phi = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
        p = phi((cp + 0.5 - mu) / sg) - phi((fl - 0.5 - mu) / sg)
        exp = max(p - ya / 100.0, (1 - p) - (100 - yb) / 100.0) * 100
        self.assertAlmostEqual(nsw._bracket_edge_pp(fl, cp, yb, ya, mu, sg), exp, places=6)

    def test_t_warm_and_t_cold_compute(self):
        self.assertIsNotNone(nsw._bracket_edge_pp(89.0, None, 40, 44, 91.0, 1.5))   # >=89
        self.assertIsNotNone(nsw._bracket_edge_pp(None, 70.0, 40, 44, 68.0, 1.5))   # <=70

    def test_invalid_inputs_return_none(self):
        self.assertIsNone(nsw._bracket_edge_pp(85, 86, 30, 34, None, 1.17))      # mu None
        self.assertIsNone(nsw._bracket_edge_pp(85, 86, 30, 34, 82.0, 0))         # sigma 0
        self.assertIsNone(nsw._bracket_edge_pp(85, 86, 40, 30, 82.0, 1.17))      # ask<=bid
        self.assertIsNone(nsw._bracket_edge_pp(None, None, 30, 34, 82.0, 1.17))  # no bounds


# ── unit: _max_sibling_edge_pp ───────────────────────────────────────────────
class TestMaxSiblingEdge(unittest.TestCase):
    def setUp(self):
        self._orig = dict(kalshi_ws._bbo_cache)
        kalshi_ws._bbo_cache.clear()

    def tearDown(self):
        kalshi_ws._bbo_cache.clear()
        kalshi_ws._bbo_cache.update(self._orig)

    def test_empty_cache_returns_none(self):
        c = _cand("KXHIGHMIA-26MAY31-B85.5")
        self.assertEqual(nsw._max_sibling_edge_pp(c, 82.0, 1.17), (None, None))

    def test_finds_sibling_excludes_self_and_other_stations(self):
        now = time.time()
        kalshi_ws._bbo_cache["KXHIGHMIA-26MAY31-B85.5"] = {"yes_bid": 0.38, "yes_ask": 0.42, "ts": now}  # self
        kalshi_ws._bbo_cache["KXHIGHMIA-26MAY31-B83.5"] = {"yes_bid": 0.60, "yes_ask": 0.64, "ts": now}  # sibling, higher NO edge
        kalshi_ws._bbo_cache["KXHIGHAUS-26MAY31-B85.5"] = {"yes_bid": 0.90, "yes_ask": 0.94, "ts": now}  # other station
        c = _cand("KXHIGHMIA-26MAY31-B85.5")
        e, tk = nsw._max_sibling_edge_pp(c, 80.0, 1.17)
        self.assertEqual(tk, "KXHIGHMIA-26MAY31-B83.5")
        self.assertGreater(e, 0)

    def test_stale_sibling_skipped(self):
        kalshi_ws._bbo_cache["KXHIGHMIA-26MAY31-B83.5"] = {"yes_bid": 0.60, "yes_ask": 0.64, "ts": time.time() - 9999}
        c = _cand("KXHIGHMIA-26MAY31-B85.5")
        self.assertEqual(nsw._max_sibling_edge_pp(c, 80.0, 1.17), (None, None))


# ── integration: the gate inside _try_auto_execute ──────────────────────────
class TestOneBracketGate(unittest.TestCase):
    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")
        nsw._pending_buys.clear()
        self._orig_cache = dict(kalshi_ws._bbo_cache)
        kalshi_ws._bbo_cache.clear()

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window
        nsw._pending_buys.clear()
        kalshi_ws._bbo_cache.clear()
        kalshi_ws._bbo_cache.update(self._orig_cache)

    def _run(self, cand, decision, positions, series="HIGH"):
        nsw._rt = SimpleNamespace(positions=positions, cycle_buys_by_station_side={})
        packet = _packet(decision["decision"], series, cand.floor, cand.cap)
        with ExitStack() as es:
            P = lambda n, v: es.enter_context(mock.patch.object(config, n, v))
            P("PUSH_ONE_BRACKET_PER_STATION_HIGH", True)   # the gate under test
            P("PUSH_MAE_GATE_ENABLED", False)
            P("USE_MU_AGREEMENT_GATE", False)
            P("PUSH_HIGH_SKIP_IF_OFF_PEAK_F", 0.0)
            P("PUSH_TAIL_BET_MIN_EDGE_PP", 0)
            P("AUTO_EXECUTE_BUY_YES_PUSH", True)
            P("AUTO_EXECUTE_BUY_NO_PUSH", True)
            P("AUTO_EXEC_LOW_ENABLED", True)
            P("PUSH_HIGH_POST_AT_MID", False)
            P("PUSH_LOW_POST_AT_MID", True)
            es.enter_context(mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None))
            es.enter_context(mock.patch.object(kalshi_client, "get_balance_cached", return_value=100.0))
            es.enter_context(mock.patch.object(low_post_probe, "resting_rows", return_value=[]))
            es.enter_context(mock.patch.object(low_post_probe, "has_resting", return_value=False))
            es.enter_context(mock.patch.object(low_post_probe, "place", return_value=(True, "placed")))
            return nsw._try_auto_execute(cand, packet, decision, series=series, local_hour=12.0)

    def test_first_bracket_allowed(self):
        """No same-station position, no quoted sibling → not blocked by the new gate."""
        cand = _cand("KXHIGHMIA-26MAY31-B85.5")
        _, reason = self._run(cand, _decision("BUY_NO"), {})
        self.assertNotIn("one_bracket_per_station", reason)
        self.assertNotIn("not_best_bracket", reason)

    def test_second_bracket_blocked_across_directions(self):
        """Already hold a BUY_YES at the station → a BUY_NO (other direction) is BLOCKED."""
        positions = {"KXHIGHMIA-26MAY31-B83.5": _pos("KMIA", "BUY_YES")}
        cand = _cand("KXHIGHMIA-26MAY31-B85.5")
        executed, reason = self._run(cand, _decision("BUY_NO"), positions)
        self.assertFalse(executed)
        self.assertIn("one_bracket_per_station", reason)

    def test_blocked_when_quoted_sibling_has_higher_edge(self):
        """A quoted sibling with higher edge blocks this bracket (max-edge selection)."""
        now = time.time()
        # self B85.5 (yes_bid 38 → NO edge ~38pp); sibling B83.5 yes_bid 60 → NO edge ~60pp
        kalshi_ws._bbo_cache["KXHIGHMIA-26MAY31-B83.5"] = {"yes_bid": 0.60, "yes_ask": 0.64, "ts": now}
        cand = _cand("KXHIGHMIA-26MAY31-B85.5")
        executed, reason = self._run(cand, _decision("BUY_NO"), {})
        self.assertFalse(executed)
        self.assertIn("not_best_bracket", reason)

    def test_allowed_when_self_is_best_quoted(self):
        """A quoted sibling with LOWER edge does NOT block (self is the best)."""
        now = time.time()
        # sibling B83.5 yes_bid 10 → NO edge ~10pp < self ~38pp
        kalshi_ws._bbo_cache["KXHIGHMIA-26MAY31-B83.5"] = {"yes_bid": 0.10, "yes_ask": 0.14, "ts": now}
        cand = _cand("KXHIGHMIA-26MAY31-B85.5")
        _, reason = self._run(cand, _decision("BUY_NO"), {})
        self.assertNotIn("not_best_bracket", reason)
        self.assertNotIn("one_bracket_per_station", reason)

    def test_low_series_unaffected(self):
        """HIGH-only: a LOW bracket with a same-station opposite-direction LOW position
        is NOT blocked by the one-bracket gate (LOW keeps its per-direction cap)."""
        positions = {"KXLOWMIA-26MAY31-B66.5": _pos("KMIA", "BUY_YES")}
        cand = _cand("KXLOWMIA-26MAY31-B70.5", series_prefix="KXLOW",
                     floor=70.0, cap=71.0, label=70.5)
        _, reason = self._run(cand, _decision("BUY_NO"), positions, series="LOW")
        self.assertNotIn("one_bracket_per_station", reason)


if __name__ == "__main__":
    unittest.main()
