#!/usr/bin/env python3
"""blend_forecast.py — supervised blend forecast for the day's extreme.

Replaces the obs-analog matcher μ with a ridge-regression blend of:
  market-implied μ (from the live bracket ladder) + live running-extreme (wethr)
  + current temp [+ optional OpenMeteo multi-model NWP].
Predicted with a CALIBRATED σ (~1.1°F) — the matcher's σ was over-wide, leaving
the market's under-confidence un-exploited.

Backtest (2024-10..2026-05, KXHIGH, Kalshi settlement, net of taker fee, FORWARD-
CHAINED, positive in all 14/14 months):
  conservative [market+running_max+cur_temp]  +8.55¢/ct  (look-ahead-free)
  full [+ OpenMeteo 7-model NWP]              +12.68¢/ct  (+9.55¢ in liquid <2¢ brackets)
See project_blend_edge_FOUND_20260601 + ~/judge_dyn/featblend.py for derivation.

FAIL-SAFE: every entry point returns None on any missing/bad input; the caller
keeps the existing matcher μ. A blend bug can never make the bot worse than today.
"""
from __future__ import annotations
import json, math, os, time
from pathlib import Path
from typing import Optional

_DIR = Path(__file__).resolve().parent
_MODELS: dict[str, dict] = {}
_MODEL_FILES = {
    ("high", "conservative"): "blend_model_high_conservative.json",
    ("high", "full"): "blend_model_high.json",
    ("low", "conservative"): "blend_model_low_conservative.json",
    ("low", "full"): "blend_model_low.json",
}


def _load(side: str, variant: str) -> Optional[dict]:
    key = (side, variant)
    if key in _MODELS:
        return _MODELS[key]
    fn = _MODEL_FILES.get(key)
    if not fn:
        return None
    p = _DIR / fn
    if not p.exists():
        _MODELS[key] = None
        return None
    try:
        with open(p) as f:
            m = json.load(f)
        m["_mean"] = m["mean"]; m["_std"] = m["std"]; m["_beta"] = m["beta"]
        _MODELS[key] = m
        return m
    except Exception:
        _MODELS[key] = None
        return None


def _bracket_midpoint(kind: str, floor, cap) -> Optional[float]:
    if floor is not None and cap is not None:
        return (float(floor) + float(cap)) / 2.0
    if floor is not None and cap is None:   # warm tail
        return float(floor) + 1.0
    if cap is not None and floor is None:    # cold tail
        return float(cap) - 1.0
    return None


def implied_mu(brackets: list[dict]) -> Optional[float]:
    """Ladder-implied μ = Σ p_yes·midpoint / Σ p_yes over the station-day's brackets.
    Each bracket: {kind, floor, cap, yes_bid, yes_ask} in CENTS. Returns None if
    fewer than 3 valid brackets (too thin to trust the implied mean)."""
    num = den = 0.0
    n = 0
    for b in brackets:
        yb, ya = b.get("yes_bid"), b.get("yes_ask")
        if yb is None or ya is None:
            continue
        try:
            yb = float(yb); ya = float(ya)
        except (TypeError, ValueError):
            continue
        if ya <= 0 or ya > 100 or yb < 0 or ya < yb:
            continue
        mp = _bracket_midpoint(b.get("kind") or "", b.get("floor"), b.get("cap"))
        if mp is None:
            continue
        p = (yb + ya) / 2.0 / 100.0
        num += p * mp; den += p; n += 1
    if n < 3 or den <= 0:
        return None
    return num / den


def blend_mu(side: str, market_mu: float, running_extreme: float, cur_temp: float,
             nwp_models: Optional[dict] = None, variant: str = "conservative") -> Optional[tuple]:
    """Apply the fitted blend → (mu, sigma). Returns None if the model is missing
    or a required feature is absent. nwp_models (full variant) = {model_name: fc}.
    """
    m = _load(side, variant)
    if not m:
        return None
    feats = m["feats"]
    vals = {"mkt": market_mu, "rmax": running_extreme, "rmin": running_extreme,
            "curt": cur_temp}
    if nwp_models:
        modvals = [nwp_models[k] for k in m.get("models", []) if nwp_models.get(k) is not None]
        for k in m.get("models", []):
            vals[k] = nwp_models.get(k)
        if modvals:
            import statistics as _st
            vals["nwp_spread"] = _st.pstdev(modvals) if len(modvals) > 1 else 0.0
            for k in m.get("models", []):
                if vals.get(k) is None:
                    vals[k] = _st.fmean(modvals)
    # build standardized feature vector [1, f1, f2, ...]
    x = [1.0]
    for f in feats:
        v = vals.get(f)
        if v is None:
            return None   # FAIL-SAFE: missing feature -> no blend, keep matcher
        try:
            x.append(float(v))
        except (TypeError, ValueError):
            return None
    mean = m["_mean"]; std = m["_std"]; beta = m["_beta"]
    if not (len(x) == len(mean) == len(std) == len(beta)):
        return None
    mu = 0.0
    for i in range(len(x)):
        s = std[i] if std[i] else 1.0
        mu += ((x[i] - mean[i]) / s) * beta[i]
    sigma = float(m.get("sigma", 1.1))
    if not (math.isfinite(mu) and 200.0 > mu > -100.0):   # sanity band (°F)
        return None
    return round(mu, 2), round(max(0.8, sigma), 2)
