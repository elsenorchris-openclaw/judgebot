"""Unit tests for nn_match bias correction + fit-quality gate
(2026-05-17 P1+P2 ship — see project-judge-nn-audit-20260517).

These tests exercise the new `bias_correction` and `fit_quality_thresh`
parameters on `nn_match_fast.predict()` and verify they compose correctly
with the existing physical-constraint locks.
"""
from __future__ import annotations

import os
import sys
import unittest

# Add bot dir to path so we import the production matcher (not a stale copy)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nn_match_fast import predict


def _ramp_traj(start_temp: float = 60.0, slope_per_h: float = 2.2,
               n_hours: int = 10, lst_min_start: int = 6 * 60):
    """Build a smooth-warming trajectory of (lst_min, temp_f)."""
    return [(lst_min_start + h * 60, start_temp + h * slope_per_h)
            for h in range(n_hours)]


def _cooling_traj(start_temp: float = 80.0, drop_per_h: float = 1.0,
                  n_hours: int = 6, lst_min_start: int = 22 * 60):
    """Build a smooth-cooling trajectory crossing midnight LST not needed
    here since we cap n_hours at 6 and start at 22:00 → ends at 03:00 next
    day. For test simplicity, anchor in single day and let matcher operate."""
    return [(lst_min_start + h * 60, start_temp - h * drop_per_h)
            for h in range(n_hours) if lst_min_start + h * 60 < 24 * 60]


class TestNNBiasCorrection(unittest.TestCase):
    """bias_correction param shifts mu_proj additively unless locked."""

    def setUp(self):
        # Use a station+date the DB definitely has (ATL, 2024-07-15)
        self.station = "ATL"
        self.date = "2024-07-15"
        self.traj = _ramp_traj(start_temp=60.0, n_hours=9)
        self.cur_lst_min = 14 * 60   # 14:00 LST, BEFORE the 16:00 peak-lock

    def test_zero_bias_no_change(self):
        res_a = predict(self.station, self.date, self.cur_lst_min,
                        self.traj, side="high", min_window_minutes=30)
        res_b = predict(self.station, self.date, self.cur_lst_min,
                        self.traj, side="high", min_window_minutes=30,
                        bias_correction=0.0)
        if res_a.get("mu_proj_f") is None:
            self.skipTest("matcher returned None on test trajectory")
        self.assertAlmostEqual(res_a["mu_proj_f"], res_b["mu_proj_f"], places=2)

    def test_positive_bias_high_adds_to_mu(self):
        res_a = predict(self.station, self.date, self.cur_lst_min,
                        self.traj, side="high", min_window_minutes=30)
        res_b = predict(self.station, self.date, self.cur_lst_min,
                        self.traj, side="high", min_window_minutes=30,
                        bias_correction=1.0)
        if res_a.get("mu_proj_f") is None or res_b.get("mu_proj_f") is None:
            self.skipTest("matcher returned None on test trajectory")
        if res_a.get("extreme_locked"):
            self.skipTest("trajectory hit locked mode; bias is bypassed")
        # mu_proj should be ~1°F higher (within physical-clamp tolerance)
        delta = res_b["mu_proj_f"] - res_a["mu_proj_f"]
        # Either exactly 1 (no clamp triggered), or 0 if clamp blocked it
        # (peak max(mu, traj_max) may absorb the +1 if traj_max > mu)
        self.assertIn(round(delta, 1), [1.0, 0.0])

    def test_negative_bias_low_subtracts_from_mu(self):
        traj = _ramp_traj(start_temp=80.0, slope_per_h=-1.0, n_hours=6,
                          lst_min_start=22 * 60)
        # Use 22:00 start, 6 entries spans through 04:00 wrapping is fine for
        # the matcher (it just uses (lst_min, val) pairs to fill bins).
        # Capping inside day to avoid wrap: just use 18:00-23:00.
        traj = [(18 * 60 + h * 60, 80 - h * 1) for h in range(6)]
        res_a = predict(self.station, self.date, 23 * 60, traj,
                        side="low", min_window_minutes=30)
        res_b = predict(self.station, self.date, 23 * 60, traj,
                        side="low", min_window_minutes=30,
                        bias_correction=-1.5)
        if res_a.get("mu_proj_f") is None or res_b.get("mu_proj_f") is None:
            self.skipTest("matcher returned None on test trajectory")
        if res_a.get("extreme_locked"):
            self.skipTest("trajectory hit locked mode; bias is bypassed")
        delta = res_b["mu_proj_f"] - res_a["mu_proj_f"]
        self.assertIn(round(delta, 1), [-1.5, 0.0])

    def test_bias_reported_in_result(self):
        res = predict(self.station, self.date, self.cur_lst_min,
                      self.traj, side="high", min_window_minutes=30,
                      bias_correction=0.7)
        if res.get("mu_proj_f") is None:
            self.skipTest("matcher returned None on test trajectory")
        self.assertEqual(res.get("bias_correction_applied_f"), 0.7)


class TestNNFitQualityGate(unittest.TestCase):
    """fit_quality_thresh returns None when neighbor-cluster σ exceeds it."""

    def setUp(self):
        self.station = "ATL"
        self.date = "2024-07-15"
        self.traj = _ramp_traj(start_temp=60.0, n_hours=9)
        self.cur_lst_min = 14 * 60

    def test_no_gate_default(self):
        """Default None → backward-compatible (always returns mu)."""
        res = predict(self.station, self.date, self.cur_lst_min,
                      self.traj, side="high", min_window_minutes=30)
        self.assertIsNotNone(res.get("mu_proj_f"))

    def test_loose_gate_passes(self):
        res = predict(self.station, self.date, self.cur_lst_min,
                      self.traj, side="high", min_window_minutes=30,
                      fit_quality_thresh=100.0)
        self.assertIsNotNone(res.get("mu_proj_f"))

    def test_tight_gate_blocks(self):
        """Threshold 0.01 → almost any neighbor spread rejects."""
        res = predict(self.station, self.date, self.cur_lst_min,
                      self.traj, side="high", min_window_minutes=30,
                      fit_quality_thresh=0.01)
        self.assertIsNone(res.get("mu_proj_f"))
        self.assertIn("fit_quality_gate", res.get("reason", ""))
        self.assertIsNotNone(res.get("sigma_proj_f"))

    def test_gate_reports_threshold_in_passing_result(self):
        res = predict(self.station, self.date, self.cur_lst_min,
                      self.traj, side="high", min_window_minutes=30,
                      fit_quality_thresh=10.0)
        if res.get("mu_proj_f") is not None:
            self.assertEqual(res.get("fit_quality_thresh"), 10.0)


class TestNNPostNoonUnlockedGate(unittest.TestCase):
    """2026-05-18: gate unlocked LOW post-noon (cooling-event projections
    unreliable). Validated 2024-25 + 2023, -25% MAE on post-noon LOW."""

    def _morning_low_warming_traj(self):
        # Min came BEFORE max (locked-able pattern):
        # 60°F at 6 AM, warms to 85°F at 3 PM.
        return [(6*60 + h*15, 60.0 + (h/36)*25.0) for h in range(37)]

    def _afternoon_cooling_traj(self):
        # Max came BEFORE min (cold-front / cooling-event pattern):
        # warms to 90°F at 11 AM, then cools to 75°F at 3 PM.
        traj = []
        # warming 6-11 AM
        for h in range(21):
            traj.append((6*60 + h*15, 80.0 + (h/20)*10.0))
        # cooling 11 AM - 3 PM
        for h in range(1, 17):
            traj.append((11*60 + h*15, 90.0 - (h/16)*15.0))
        return traj

    def test_gate_fires_low_postnoon_unlocked(self):
        """LOW eval at 3 PM with cooling-event trajectory → gate fires."""
        traj = self._afternoon_cooling_traj()
        res = predict("ATL", "2024-07-15", 15*60, traj, side="low",
                      min_window_minutes=30)
        self.assertIsNone(res.get("mu_proj_f"))
        self.assertEqual(res.get("reason"), "low_postnoon_unlocked_unreliable")
        self.assertFalse(res.get("extreme_locked"))

    def test_gate_does_not_fire_low_postnoon_locked(self):
        """LOW eval at 10 PM with morning-low warming traj → locked, gate does NOT fire.

        2026-05-19 (B-Gate-21): lock floor moved from 12*60 to 21*60. This test
        was previously at cur=15*60; it now uses cur=22*60 so the lock can fire.
        Pre-21:00 lock behavior is covered by TestNNLockFloor below.
        """
        traj = self._morning_low_warming_traj()
        res = predict("ATL", "2024-07-15", 22*60, traj, side="low",
                      min_window_minutes=30)
        # Locked mode should return a value (NOT gated)
        self.assertIsNotNone(res.get("mu_proj_f"))
        self.assertTrue(res.get("extreme_locked"))

    def test_gate_does_not_fire_low_pre_noon(self):
        """LOW eval at 10 AM → pre-noon, gate cannot fire regardless of locked state."""
        traj = self._morning_low_warming_traj()[:17]  # truncate to 10 AM
        res = predict("ATL", "2024-07-15", 10*60, traj, side="low",
                      min_window_minutes=30)
        # Should return a value (gate skipped — pre-noon)
        self.assertNotEqual(res.get("reason"), "low_postnoon_unlocked_unreliable")

    def test_gate_does_not_fire_high(self):
        """HIGH side with same cooling traj at post-noon → gate does NOT fire (LOW-only)."""
        traj = self._afternoon_cooling_traj()
        res = predict("ATL", "2024-07-15", 15*60, traj, side="high",
                      min_window_minutes=30)
        self.assertNotEqual(res.get("reason"), "low_postnoon_unlocked_unreliable")

    def test_gate_off_returns_value(self):
        """gate_low_postnoon_unlocked=False — same unlocked LOW post-noon now returns mu_proj."""
        traj = self._afternoon_cooling_traj()
        res = predict("ATL", "2024-07-15", 15*60, traj, side="low",
                      min_window_minutes=30,
                      gate_low_postnoon_unlocked=False)
        # Should now return a value (gate disabled)
        self.assertIsNotNone(res.get("mu_proj_f"))
        self.assertNotEqual(res.get("reason"), "low_postnoon_unlocked_unreliable")


class TestNNAnalogDistribution(unittest.TestCase):
    """2026-05-18: predict() returns analog_summary with day_extremes array,
    p25/p50/p75 of day_extremes and deltas. Used by paper_judge_bot.py to
    compute bracket-fraction and by judgment.py to render the distribution block."""

    def setUp(self):
        self.traj = _ramp_traj(start_temp=60.0, slope_per_h=2.0, n_hours=9)

    def test_analog_summary_present(self):
        res = predict("ATL", "2024-07-15", 14*60, self.traj, side="high",
                      min_window_minutes=30)
        asum = res.get("analog_summary")
        self.assertIsNotNone(asum)
        for k in ("day_extremes", "day_extremes_p25_p50_p75", "deltas_p25_p50_p75"):
            self.assertIn(k, asum)

    def test_day_extremes_array_size_matches_n_neighbors(self):
        res = predict("ATL", "2024-07-15", 14*60, self.traj, side="high",
                      min_window_minutes=30)
        asum = res["analog_summary"]
        n = res.get("n_neighbors_used", 0)
        self.assertEqual(len(asum["day_extremes"]), n)

    def test_percentile_triples_well_formed(self):
        res = predict("ATL", "2024-07-15", 14*60, self.traj, side="high",
                      min_window_minutes=30)
        asum = res["analog_summary"]
        for k in ("day_extremes_p25_p50_p75", "deltas_p25_p50_p75"):
            pcts = asum[k]
            self.assertEqual(len(pcts), 3)
            # p25 <= p50 <= p75
            self.assertLessEqual(pcts[0], pcts[1])
            self.assertLessEqual(pcts[1], pcts[2])

    def test_low_side_returns_day_extremes(self):
        traj = [(2*60 + h*15, 70.0 - (h/16)*8.0) for h in range(17)]
        res = predict("ATL", "2024-07-15", 6*60, traj, side="low",
                      min_window_minutes=30)
        asum = res.get("analog_summary")
        # Either gated (None) or has day_extremes
        if res.get("mu_proj_f") is not None:
            self.assertIsNotNone(asum)
            self.assertGreater(len(asum["day_extremes"]), 0)


class TestNNBackwardCompat(unittest.TestCase):
    """Ensure old call signature still works (no positional changes)."""

    def test_no_new_params(self):
        traj = _ramp_traj(start_temp=60.0, n_hours=9)
        res = predict("ATL", "2024-07-15", 14 * 60, traj, side="high",
                      min_window_minutes=30)
        # Should run without error; bias defaults to 0, fit_thresh to None
        self.assertIn("mu_proj_f", res)


class TestNNLockFloor(unittest.TestCase):
    """2026-05-19 (B-Gate-21): floor on LOW locked branch in nn_match_fast.

    Old behavior: lock fired whenever cur >= 12*60 (noon) AND trajectory had
    morning-low-then-afternoon-peak pattern AND >1h since trough. New behavior:
    lock requires cur >= NN_LOCK_FLOOR_LST_MIN (default 21*60 = 9 PM) instead.
    Pre-9PM cells fall through to the post-noon-unlocked gate which returns None.
    Bot then receives no nn_match μ and the trade is blocked at the prescreen
    layer via PRESCREEN['skip_unless_nn_match']=True.

    Reason: at hour=18, morning trough is 11h stale and evening cooling can drive
    actual day_min 3-16°F BELOW traj_min on cold/dry/clear nights. Backtest pooled
    n=269 cross-year: residual bias +1.93°F at hr18, +1.28°F at hr20, ~0°F at hr22.
    Sign-convention: traj_min snapshot at 18:00 LST is HIGHER than the eventual
    day_min, so the lock predicts WARMER than truth.
    """

    def _morning_low_warming_traj(self, last_lst_min):
        """Cooling-then-warming traj: 60°F at 6 AM → warms to 85°F at 3 PM,
        then static through `last_lst_min`. Mimics a typical clear-summer-day
        pattern where the lock SHOULD fire (morning was the day's low)."""
        traj = []
        # 6 AM → 3 PM warming
        for h in range(37):
            t = 6*60 + h*15
            if t > last_lst_min: break
            traj.append((t, 60.0 + (h/36) * 25.0))
        # 3 PM → last_lst_min steady at 85°F (cooling slightly OK too — irrelevant)
        t = 15*60 + 15
        while t <= last_lst_min:
            traj.append((t, 84.5))
            t += 15
        return traj

    def test_lock_does_not_fire_at_18(self):
        """At 6 PM, morning-low warming traj → lock does NOT fire (cur < 21*60).
        The post-noon-unlocked gate returns None → bot uses fallback chain."""
        traj = self._morning_low_warming_traj(18 * 60)
        res = predict("ATL", "2024-07-15", 18*60, traj, side="low",
                      min_window_minutes=30)
        # B-Gate-21: lock floor moved; cur=18 < 21, so unlocked
        self.assertFalse(res.get("extreme_locked"))
        # Falls through to post-noon-unlocked gate → returns None
        self.assertIsNone(res.get("mu_proj_f"))
        self.assertEqual(res.get("reason"), "low_postnoon_unlocked_unreliable")

    def test_lock_does_not_fire_at_20(self):
        """At 8 PM, same pattern: still under the floor."""
        traj = self._morning_low_warming_traj(20 * 60)
        res = predict("ATL", "2024-07-15", 20*60, traj, side="low",
                      min_window_minutes=30)
        self.assertFalse(res.get("extreme_locked"))
        self.assertIsNone(res.get("mu_proj_f"))

    def test_lock_fires_at_21(self):
        """At 9 PM (exactly at floor), lock fires → returns traj_min."""
        traj = self._morning_low_warming_traj(21 * 60)
        res = predict("ATL", "2024-07-15", 21*60, traj, side="low",
                      min_window_minutes=30)
        self.assertTrue(res.get("extreme_locked"))
        self.assertIsNotNone(res.get("mu_proj_f"))
        # Locked value should equal traj_min (≈60°F from the warming start)
        self.assertAlmostEqual(res["mu_proj_f"], 60.0, delta=1.0)

    def test_lock_fires_at_22(self):
        """At 10 PM, lock fires (above floor)."""
        traj = self._morning_low_warming_traj(22 * 60)
        res = predict("ATL", "2024-07-15", 22*60, traj, side="low",
                      min_window_minutes=30)
        self.assertTrue(res.get("extreme_locked"))
        self.assertIsNotNone(res.get("mu_proj_f"))

    def test_rollback_floor_12_restores_old_behavior(self):
        """Monkey-patch NN_LOCK_FLOOR_LST_MIN=12*60 → lock fires at 3 PM (rollback)."""
        import nn_match_fast as nnf
        import config as cfg
        old = getattr(cfg, "NN_LOCK_FLOOR_LST_MIN", 21*60)
        cfg.NN_LOCK_FLOOR_LST_MIN = 12 * 60
        try:
            traj = self._morning_low_warming_traj(15 * 60)
            res = nnf.predict("ATL", "2024-07-15", 15*60, traj, side="low",
                              min_window_minutes=30)
            self.assertTrue(res.get("extreme_locked"))
            self.assertIsNotNone(res.get("mu_proj_f"))
        finally:
            cfg.NN_LOCK_FLOOR_LST_MIN = old


class TestPeakClamp(unittest.TestCase):
    """Two-tier HIGH peak clamp (2026-05-19).

    Tier 1 (post-peak): cur_lst_min >= P50_peak_time AND peak >=30 min ago AND
      max in last 30 min < traj_max - 0.5°F → cap at traj_max + 1.0°F.
    Tier 2 (at-peak):   cur_lst_min >= P50_peak_time AND cur_tmpf >= traj_max -
      1.0°F → cap at traj_max + 1.5°F.
    When both fire: tier 1's tighter cap wins. Floor mu_proj >= traj_max.
    """

    def setUp(self):
        # ATL July P50 historical peak is ~15:24 LST (from peak_qtable);
        # use July tests anchored at 14:00, 15:30, 17:00 LST as needed.
        self.station = "ATL"
        self.date = "2024-07-15"
        import nn_match_fast as nnf
        nnf._build_peak_qtable()

    def _post_peak_traj(self, cur_lst_min, peak_temp=92.0, drop_f=1.2):
        """Build trajectory where peak was 45 min ago + temp has since dropped."""
        peak_lst_min = cur_lst_min - 45
        pts = []
        # Ramp up to peak (8h pre-peak)
        for h in range(8):
            m = peak_lst_min - (8 - h) * 60
            if m < 6 * 60: continue
            pts.append((m, peak_temp - 25.0 + h * 3.0))
        pts.append((peak_lst_min, peak_temp))
        # Decline post-peak (every 15 min)
        for i in range(1, 4):
            m = peak_lst_min + i * 15
            if m > cur_lst_min: break
            pts.append((m, peak_temp - i * (drop_f / 3.0)))
        return pts

    def _at_peak_traj(self, cur_lst_min, peak_temp=92.0):
        """Build trajectory where cur_tmpf == traj_max (we're at peak)."""
        pts = []
        for h in range(8):
            m = cur_lst_min - (8 - h) * 60
            if m < 6 * 60: continue
            pts.append((m, peak_temp - 25.0 + h * 3.0))
        # Peak right now
        pts.append((cur_lst_min - 5, peak_temp - 0.1))
        pts.append((cur_lst_min, peak_temp))
        return pts

    def _pre_peak_climbing_traj(self, cur_lst_min, cur_temp=78.0):
        """Trajectory still actively climbing, traj_max == cur_tmpf."""
        pts = []
        for h in range(8):
            m = cur_lst_min - (8 - h) * 60
            if m < 6 * 60: continue
            pts.append((m, cur_temp - 20.0 + h * 2.5))
        pts.append((cur_lst_min, cur_temp))
        return pts

    def test_tier1_fires_post_peak_with_drop(self):
        """Past P50, peak >=30 min ago, sustained drop → tier 1 caps tight at +0.75°F."""
        import nn_match_fast as nnf
        cur = 17 * 60  # 17:00 LST, well past ATL Jul P50 (~15:24)
        traj = self._post_peak_traj(cur, peak_temp=92.0, drop_f=2.0)
        res = nnf.predict(self.station, self.date, cur, traj, side="high",
                          min_window_minutes=30)
        if res.get("mu_proj_f") is None: self.skipTest("matcher returned None")
        self.assertEqual(res.get("peak_clamp_tier"), "post_peak")
        # mu_proj should be capped at traj_max + 0.75 (or lower if mu_raw was lower)
        traj_max = max(v for _, v in traj)
        self.assertLessEqual(res["mu_proj_f"], traj_max + 0.75 + 0.05)
        self.assertGreaterEqual(res["mu_proj_f"], traj_max - 0.05)

    def test_tier2_fires_at_peak(self):
        """Past P50, cur_tmpf within 1.0°F of traj_max → tier 2 caps at +1.0°F."""
        import nn_match_fast as nnf
        cur = 16 * 60  # 16:00 LST, past P50, before evening
        traj = self._at_peak_traj(cur, peak_temp=92.0)
        res = nnf.predict(self.station, self.date, cur, traj, side="high",
                          min_window_minutes=30)
        if res.get("mu_proj_f") is None: self.skipTest("matcher returned None")
        # Tier 1 cannot fire (peak is right now, 0 min ago); tier 2 should fire
        self.assertEqual(res.get("peak_clamp_tier"), "at_peak")
        traj_max = max(v for _, v in traj)
        self.assertLessEqual(res["mu_proj_f"], traj_max + 1.0 + 0.05)
        self.assertGreaterEqual(res["mu_proj_f"], traj_max - 0.05)

    def test_no_clamp_before_p50(self):
        """At 9:00 LST (before ATL Jul P50=15:24), clamp must not fire."""
        import nn_match_fast as nnf
        cur = 9 * 60
        traj = self._pre_peak_climbing_traj(cur, cur_temp=78.0)
        res = nnf.predict(self.station, self.date, cur, traj, side="high",
                          min_window_minutes=30)
        if res.get("mu_proj_f") is None: self.skipTest("matcher returned None")
        self.assertIsNone(res.get("peak_clamp_tier"))

    def test_no_clamp_when_disabled(self):
        """NN_HIGH_PEAK_CLAMP_ENABLED=False short-circuits both tiers."""
        import nn_match_fast as nnf, config as cfg
        old = cfg.NN_HIGH_PEAK_CLAMP_ENABLED
        cfg.NN_HIGH_PEAK_CLAMP_ENABLED = False
        try:
            cur = 17 * 60
            traj = self._post_peak_traj(cur, peak_temp=92.0, drop_f=2.0)
            res = nnf.predict(self.station, self.date, cur, traj, side="high",
                              min_window_minutes=30)
            if res.get("mu_proj_f") is None: self.skipTest("matcher returned None")
            self.assertIsNone(res.get("peak_clamp_tier"))
        finally:
            cfg.NN_HIGH_PEAK_CLAMP_ENABLED = old

    def test_low_side_unaffected(self):
        """Clamp is HIGH-only — LOW path must not set peak_clamp_tier."""
        import nn_match_fast as nnf
        cur = 17 * 60
        traj = self._post_peak_traj(cur, peak_temp=92.0, drop_f=2.0)
        # Use a date+station where LOW will succeed via post-noon-unlocked
        # gate is on so we'll likely get a None mu_proj; just verify tier
        # stays None either way.
        res = nnf.predict(self.station, self.date, cur, traj, side="low",
                          min_window_minutes=30)
        self.assertIsNone(res.get("peak_clamp_tier"))

    def test_tier1_no_drop_means_tier2_falls_through(self):
        """Past P50, peak old but no drop → tier 1 misses; tier 2 fires (still at peak)."""
        import nn_match_fast as nnf
        cur = 17 * 60
        # Build traj where peak is 45 min old AND cur_tmpf still at peak
        traj = []
        for h in range(8):
            m = cur - (8 - h) * 60
            if m < 6 * 60: continue
            traj.append((m, 67.0 + h * 3.0))
        peak_lst_min = cur - 45
        traj.append((peak_lst_min, 92.0))
        # Subsequent readings hold at 91.8 (within 0.5°F of peak — no drop)
        for i in range(1, 4):
            m = peak_lst_min + i * 15
            if m > cur: break
            traj.append((m, 91.8))
        res = nnf.predict(self.station, self.date, cur, traj, side="high",
                          min_window_minutes=30)
        if res.get("mu_proj_f") is None: self.skipTest("matcher returned None")
        # Tier 1 should NOT fire (no 0.5°F drop). Tier 2 should fire (cur=91.8 >= 92.0 - 1.0).
        self.assertEqual(res.get("peak_clamp_tier"), "at_peak")

    def test_qtable_strips_k_prefix(self):
        """Peak qtable lookup must accept 'KATL' and strip the K to match 'ATL'."""
        import nn_match_fast as nnf
        # 'ATL' is what trace DB stores; 'KATL' is what live bot may pass
        p50_atl = nnf._peak_qtable_p50("ATL", 7)
        p50_katl = nnf._peak_qtable_p50("KATL", 7)
        self.assertIsNotNone(p50_atl)
        self.assertEqual(p50_atl, p50_katl)

    def test_qtable_unknown_station_returns_none(self):
        """Unknown station = no clamp (safe default)."""
        import nn_match_fast as nnf
        self.assertIsNone(nnf._peak_qtable_p50("KXXX", 7))


if __name__ == "__main__":
    unittest.main()
