"""Tests for nn_shadow_worker._try_auto_execute position cap behavior.

2026-05-20: position cap must scope to candidate's climate_day so a stuck
prior-day position (e.g. KMSY 5/19 Kalshi-pending settlement) doesn't
block today's BUYs on the same (station, series_prefix, direction).
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
                    bracket_kind="B", floor=86.0, cap=87.0, label=86.5):
    return market_universe.Candidate(
        ticker=ticker,
        series_prefix=series_prefix,
        city_code="TNOLA",
        station=station,
        climate_day=climate_day,
        bracket_kind=bracket_kind,
        floor=floor,
        cap=cap,
        bracket_label=label,
        market={},
    )


def _make_packet():
    """Packet with mid-range ask + non-zero seconds_to_close."""
    return {
        "yes_ask_c": 50,
        "no_ask_c": 50,
        "seconds_to_close": 10_000,
    }


def _make_decision(direction):
    return {
        "decision": direction,
        "edge": 0.20,
        "p_yes": 0.30 if direction == "BUY_NO" else 0.70,
        "reason": f"{direction} test edge=20.0pp",
    }


class TestPositionCapClimateDay(unittest.TestCase):
    """Verify position cap scopes to candidate.climate_day."""

    def setUp(self):
        self._orig_rt = nsw._rt
        self._orig_window = nsw._in_decision_window
        nsw._in_decision_window = lambda *a, **kw: (True, "test-window")

    def tearDown(self):
        nsw._rt = self._orig_rt
        nsw._in_decision_window = self._orig_window

    def _run(self, positions, candidate, decision):
        """Run _try_auto_execute with downstream mocked to never place an
        order. Returns (executed, reason)."""
        nsw._rt = SimpleNamespace(
            positions=positions,
            cycle_buys_by_station_side={},
        )
        import paper_judge_bot as pjb
        import kalshi_client
        with mock.patch.object(pjb, "execute_buy", lambda *a, **kw: None), \
             mock.patch.object(kalshi_client, "get_balance_cached",
                               return_value=100.0):
            return nsw._try_auto_execute(
                candidate, _make_packet(), decision,
                series="HIGH", local_hour=15.0,
            )

    def test_prior_day_position_does_not_block_todays_buy(self):
        """Stuck KMSY 5/19 BUY_NO must NOT block KMSY 5/20 BUY_NO.

        This is the bug fix for 2026-05-20: a position from a previous
        climate_day (e.g. Kalshi-pending settlement) was blocking same-
        station+series BUYs on the current climate_day.
        """
        positions = {
            "KXHIGHTNOLA-26MAY19-B86.5": {
                "station": "KMSY", "action": "BUY_NO",
                "cost": 4.5, "date_str": "2026-05-19",
            },
        }
        cand = _make_candidate(
            ticker="KXHIGHTNOLA-26MAY20-B88.5", station="KMSY",
            series_prefix="KXHIGH", climate_day="2026-05-20",
            floor=88.0, cap=89.0, label=88.5,
        )
        executed, reason = self._run(positions, cand, _make_decision("BUY_NO"))
        self.assertNotIn("position_cap", reason,
                         f"prior-day position blocked today's BUY: {reason}")

    def test_same_day_position_still_blocks(self):
        """Same-day BUY_NO positions MUST count toward the cap (only prior-day
        positions are ignored — that is what this test exists to pin). 2026-06-03:
        HIGH NO cap was raised to 2 (PUSH_MAX_TICKERS_PER_STATION_NO, +EV 2nd
        wing NO via the sigma-play), so it takes TWO same-day NOs before blocking
        the 3rd. The climate_day scoping under test is unchanged."""
        positions = {
            "KXHIGHTNOLA-26MAY20-B84.5": {
                "station": "KMSY", "action": "BUY_NO",
                "cost": 4.5, "date_str": "2026-05-20",
            },
            "KXHIGHTNOLA-26MAY20-B86.5": {
                "station": "KMSY", "action": "BUY_NO",
                "cost": 4.5, "date_str": "2026-05-20",
            },
        }
        cand = _make_candidate(
            ticker="KXHIGHTNOLA-26MAY20-B88.5", station="KMSY",
            series_prefix="KXHIGH", climate_day="2026-05-20",
            floor=88.0, cap=89.0, label=88.5,
        )
        executed, reason = self._run(positions, cand, _make_decision("BUY_NO"))
        self.assertFalse(executed, f"same-day cap failed to block: {reason}")
        self.assertIn("position_cap", reason)   # now 2>=2

    def test_prior_day_position_different_direction_irrelevant(self):
        """Prior-day BUY_YES position must not block today's BUY_NO either
        (direction filter already handles this, but worth pinning)."""
        positions = {
            "KXHIGHTNOLA-26MAY19-B90.5": {
                "station": "KMSY", "action": "BUY_YES",
                "cost": 4.94, "date_str": "2026-05-19",
            },
        }
        cand = _make_candidate(
            ticker="KXHIGHTNOLA-26MAY20-B88.5", station="KMSY",
            series_prefix="KXHIGH", climate_day="2026-05-20",
            floor=88.0, cap=89.0, label=88.5,
        )
        executed, reason = self._run(positions, cand, _make_decision("BUY_NO"))
        self.assertNotIn("position_cap", reason)

    def test_climate_day_field_fallback(self):
        """If a position has `climate_day` but no `date_str`, the cap
        check must still scope correctly."""
        positions = {
            "KXHIGHTNOLA-26MAY19-B86.5": {
                "station": "KMSY", "action": "BUY_NO",
                "cost": 4.5, "climate_day": "2026-05-19",
            },
        }
        cand = _make_candidate(
            ticker="KXHIGHTNOLA-26MAY20-B88.5", station="KMSY",
            series_prefix="KXHIGH", climate_day="2026-05-20",
            floor=88.0, cap=89.0, label=88.5,
        )
        executed, reason = self._run(positions, cand, _make_decision("BUY_NO"))
        self.assertNotIn("position_cap", reason)


if __name__ == "__main__":
    unittest.main()
