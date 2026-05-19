"""nn_shadow_strategy.py — pure-nn auto-execute decision function.

A drop-in replacement for the LLM `judge_entry()` call when running the
event-driven shadow path. Takes a fully-built packet (same shape that
build_entry_user_message reads from) and returns a decision dict:

    {"decision": "BUY_NO" | "BUY_YES" | "SKIP",
     "side":     "BUY_NO" | "BUY_YES" | None,
     "edge":     float | None,        # P(direction) - market_implied
     "p_yes":    float | None,
     "qty":      int | None,
     "price_c":  int | None,          # what we'd pay per contract (cents)
     "size_usd": float | None,
     "reason":   str,                 # human-readable
     "rm_locked": bool}

Pure function — no I/O, no logging, no orders. The caller (event-driven
shadow worker) decides what to do with the result (log, or in the future,
execute).

Decision logic mirrors what the prompt instructs the LLM to do, but
deterministic:
  1. Gate on `mu_method` starting with "nn_match_".
  2. Compute P(YES) via the bracket-shape formula with CLI rounding.
  3. Apply rm-anchor: HIGH μ = max(μ, rm); LOW μ = min(μ, rm).
  4. Pick the side with larger edge.
  5. Apply edge floor (6pp) and ceiling (25pp unless rm-locked).
  6. Size per the bot's $5 series caps, min-buy floor, and existing-position
     headroom.
"""
from __future__ import annotations

import math
from typing import Any, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Gaussian CDF helpers
# ─────────────────────────────────────────────────────────────────────────────
_INF = float("inf")
_NEGINF = float("-inf")


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _gauss_interval(mu: float, sigma: float, lo: float, hi: float) -> float:
    """P(lo ≤ X < hi) for X ~ N(mu, σ²). Handles ±∞ bounds."""
    sigma = max(sigma, 0.1)
    p_hi = 1.0 if hi == _INF else _ncdf((hi - mu) / sigma)
    p_lo = 0.0 if lo == _NEGINF else _ncdf((lo - mu) / sigma)
    return max(0.0, p_hi - p_lo)


def _yes_window(floor, cap) -> tuple[float, float]:
    """YES window in true-temp space.
      B-bracket: [floor − 0.5, cap + 0.5)
      T warm tail (floor only): [floor + 0.5, +∞)
      T cold tail (cap only):   (−∞, cap − 0.5)
    """
    if floor is not None and cap is not None:
        return (floor - 0.5, cap + 0.5)
    if floor is not None:
        return (floor + 0.5, _INF)
    if cap is not None:
        return (_NEGINF, cap - 0.5)
    return (_NEGINF, _INF)  # malformed


def _p_yes_constrained(mu: float, sigma: float, floor, cap,
                       rm, is_high: bool) -> float:
    """Compute P(YES | physical rm constraint).

    HIGH series: day_high ≥ rm (rm = running_max).
    LOW series:  day_low  ≤ rm (rm = running_min).

    Returns the conditional probability P(YES window | constraint). When
    rm is None or days_out != 0, this reduces to the untruncated formula.

    Math:
      P(YES | C) = P(YES ∩ C) / P(C)
        where C = [rm, ∞) for HIGH or (−∞, rm] for LOW.
      Both terms are integrals of N(mu, σ²) over intervals.
    """
    yes_lo, yes_hi = _yes_window(floor, cap)
    if yes_lo >= yes_hi:
        return 0.0
    sigma = max(sigma, 0.1)

    # Constraint half-line
    if rm is None:
        return _gauss_interval(mu, sigma, yes_lo, yes_hi)
    if is_high:
        c_lo, c_hi = float(rm), _INF
    else:
        c_lo, c_hi = _NEGINF, float(rm)

    # Effective YES = YES ∩ constraint
    eff_lo = max(yes_lo, c_lo)
    eff_hi = min(yes_hi, c_hi)
    if eff_lo >= eff_hi:
        return 0.0  # No overlap

    p_constraint = _gauss_interval(mu, sigma, c_lo, c_hi)
    if p_constraint < 1e-9:
        # Degenerate: nn's distribution puts ≈0 mass at the observed rm.
        # That means nn was WAY off; we can't trust the conditional either.
        # Fall back to "is rm itself inside YES?" — the constraint boundary
        # is the best point estimate we have for day_extreme.
        boundary = c_lo if is_high else c_hi
        return 1.0 if (yes_lo <= boundary < yes_hi) else 0.0

    p_yes_and_c = _gauss_interval(mu, sigma, eff_lo, eff_hi)
    return min(1.0, max(0.0, p_yes_and_c / p_constraint))


# Backwards-compat wrappers — same names as before but route through the
# constrained formula with rm=None (no truncation). Kept so existing tests
# and any external callers continue to work.
def _p_yes_b(mu: float, sigma: float, floor: float, cap: float) -> float:
    return _p_yes_constrained(mu, sigma, floor, cap, rm=None, is_high=True)


def _p_yes_t_warm(mu: float, sigma: float, floor: float) -> float:
    return _p_yes_constrained(mu, sigma, floor, None, rm=None, is_high=True)


def _p_yes_t_cold(mu: float, sigma: float, cap: float) -> float:
    return _p_yes_constrained(mu, sigma, None, cap, rm=None, is_high=True)


# ─────────────────────────────────────────────────────────────────────────────
# rm-lock detection — mirrors paper_judge_bot._is_rm_locked_for_side
# ─────────────────────────────────────────────────────────────────────────────
def _rm_locked(packet: dict, side: str) -> tuple[bool, str]:
    """Returns (locked, reason). Exact mirror of
    paper_judge_bot._is_rm_locked_for_side — change one, change both."""
    rm = packet.get("running_min_or_max")
    if rm is None:
        return False, "rm_none"
    try:
        rm = float(rm)
    except (TypeError, ValueError):
        return False, "rm_not_numeric"
    fl = packet.get("floor")
    cp = packet.get("cap")
    series = packet.get("series") or ""
    clk = packet.get("local_clock") or {}
    past_peak = bool(clk.get("past_peak_today"))
    past_min = bool(clk.get("past_min_today"))

    if series == "KXHIGH" or series.startswith("KXHIGHT"):
        if side == "BUY_NO":
            if cp is not None and rm >= cp + 1.0:
                return True, f"HIGH BUY_NO overshoot: rm {rm} >= cap+1 ({cp+1})"
            if fl is not None and rm <= fl - 1.0 and past_peak:
                return True, f"HIGH BUY_NO stays-below: rm {rm} <= floor-1 ({fl-1}) AND past peak"
            return False, "no_high_buyno_lock"
        elif side == "BUY_YES":
            if fl is not None and cp is not None:
                if past_peak and (fl - 0.5) <= rm <= (cp + 0.5):
                    return True, f"HIGH BUY_YES B-bracket locked: rm {rm} in [{fl-0.5},{cp+0.5}] AND past peak"
            elif fl is not None and cp is None and rm >= fl + 1.0:
                return True, f"HIGH BUY_YES T-warm overshoot: rm {rm} >= floor+1 ({fl+1})"
            return False, "no_high_buyyes_lock"
    elif series.startswith("KXLOW"):
        if side == "BUY_NO":
            if fl is not None and rm <= fl - 1.0:
                return True, f"LOW BUY_NO stays-below: rm {rm} <= floor-1 ({fl-1})"
            if cp is not None and rm >= cp + 1.0 and past_min:
                return True, f"LOW BUY_NO stays-above: rm {rm} >= cap+1 ({cp+1}) AND past min"
            return False, "no_low_buyno_lock"
        elif side == "BUY_YES":
            if fl is not None and cp is not None:
                if past_min and (fl - 0.5) <= rm <= (cp + 0.5):
                    return True, f"LOW BUY_YES B-bracket locked: rm {rm} in [{fl-0.5},{cp+0.5}] AND past min"
            elif cp is not None and fl is None and rm <= cp - 1.0:
                return True, f"LOW BUY_YES T-cold crossed: rm {rm} <= cap-1 ({cp-1})"
            return False, "no_low_buyyes_lock"
    return False, "unknown_series_or_side"


# ─────────────────────────────────────────────────────────────────────────────
# Sizing — mirrors the bot's max_bet_*_series_usd + min_buy_usd logic
# ─────────────────────────────────────────────────────────────────────────────
def _compute_size(packet: dict, side: str, ask_c: int, *,
                  series_cap_usd: float, min_buy_usd: float,
                  ticker_remaining_usd: float) -> tuple[int, float]:
    """Returns (qty, total_cost_usd). qty=0 means 'cannot size into this'."""
    if ask_c <= 0 or ask_c >= 100:
        return 0, 0.0
    price = ask_c / 100.0
    # Cap is the minimum of: series cap, headroom on this ticker, min-buy floor
    max_spend = min(series_cap_usd, ticker_remaining_usd)
    if max_spend < min_buy_usd:
        return 0, 0.0
    # Try to size up to max_spend; floor to integer contracts
    qty = int(max_spend // price)
    if qty < 1:
        return 0, 0.0
    cost = qty * price
    if cost < min_buy_usd:
        # Bump qty until min_buy is met, if still under series cap
        while cost < min_buy_usd and (qty + 1) * price <= max_spend:
            qty += 1
            cost = qty * price
        if cost < min_buy_usd:
            return 0, 0.0
    return qty, cost


# ─────────────────────────────────────────────────────────────────────────────
# Main decision
# ─────────────────────────────────────────────────────────────────────────────
def pure_nn_decide(
    packet: dict[str, Any],
    *,
    edge_min: float = 0.06,
    edge_max: float = 0.25,
    series_cap_high_usd: float = 5.0,
    series_cap_low_usd: float = 5.0,
    min_buy_usd: float = 1.0,
    ticker_remaining_usd: float = 5.0,
) -> dict[str, Any]:
    """Decide what pure-nn would do on this packet. See module docstring."""
    out: dict[str, Any] = {
        "decision": "SKIP", "side": None, "edge": None, "p_yes": None,
        "qty": None, "price_c": None, "size_usd": None,
        "reason": "", "rm_locked": False,
    }

    mu_method = packet.get("mu_method") or ""
    if not mu_method.startswith("nn_match_"):
        out["reason"] = f"mu_method={mu_method!r} not nn_match"
        return out

    mu = packet.get("mu_chosen")
    sigma = packet.get("sigma_chosen")
    if mu is None or sigma is None:
        out["reason"] = "missing mu/sigma"
        return out
    try:
        mu = float(mu)
        sigma = float(sigma)
    except (TypeError, ValueError):
        out["reason"] = "mu/sigma not numeric"
        return out

    floor = packet.get("floor")
    cap = packet.get("cap")
    series = packet.get("series") or ""
    is_high = "HIGH" in series

    # Bracket shape gates
    if floor is None and cap is None:
        out["reason"] = "no bracket geometry"
        return out
    try:
        if floor is not None:
            floor = float(floor)
        if cap is not None:
            cap = float(cap)
    except (TypeError, ValueError):
        out["reason"] = "floor/cap not numeric"
        return out

    # Physical rm constraint (d+0 only): for HIGH, day_high ≥ rm; for LOW,
    # day_low ≤ rm. Used both to truncate the probability distribution
    # (proper Bayesian update) and to report rm-anchor status for logging.
    # Replaces the older "μ = max(μ, rm)" anchor — truncation is the
    # mathematically correct version (the impossible-region mass is
    # redistributed, not just the mean clamped).
    rm = None
    rm_anchored = False
    raw_rm = packet.get("running_min_or_max")
    if raw_rm is not None and packet.get("days_out") == 0:
        try:
            rm = float(raw_rm)
            # An anchor is "active" whenever rm pushes the distribution —
            # i.e., rm is on the "constraining" side of μ.
            if is_high and rm > mu:
                rm_anchored = True
            elif not is_high and rm < mu:
                rm_anchored = True
        except (TypeError, ValueError):
            rm = None

    # Compute P(YES) via the truncated distribution (untruncated when rm=None)
    if floor is not None and cap is not None:
        shape = "B"
    elif cap is not None:
        shape = "T-cold"
    else:
        shape = "T-warm"
    p_yes = _p_yes_constrained(mu, sigma, floor, cap, rm, is_high)
    p_yes = max(0.0, min(1.0, p_yes))
    p_no = 1.0 - p_yes

    yes_ask = packet.get("yes_ask_c")
    no_ask = packet.get("no_ask_c")
    if yes_ask is None or no_ask is None:
        out["reason"] = "missing yes_ask_c/no_ask_c"
        return out
    try:
        yes_ask = int(yes_ask)
        no_ask = int(no_ask)
    except (TypeError, ValueError):
        out["reason"] = "ask not numeric"
        return out

    edge_yes = p_yes - yes_ask / 100.0
    edge_no = p_no - no_ask / 100.0
    if edge_yes >= edge_no:
        side = "BUY_YES"
        edge = edge_yes
        prob = p_yes
        ask_c = yes_ask
    else:
        side = "BUY_NO"
        edge = edge_no
        prob = p_no
        ask_c = no_ask

    out["side"] = side
    out["edge"] = edge
    out["p_yes"] = p_yes
    out["price_c"] = ask_c

    if edge < edge_min:
        out["reason"] = f"edge {edge:.3f} < {edge_min} (best={side})"
        return out

    locked, lock_reason = _rm_locked(packet, side)
    out["rm_locked"] = locked

    if edge > edge_max and not locked:
        out["reason"] = (f"edge {edge:.3f} > {edge_max} on {side} "
                         f"without rm-lock ({lock_reason})")
        return out

    # Sizing
    series_cap = series_cap_high_usd if is_high else series_cap_low_usd
    qty, cost = _compute_size(
        packet, side, ask_c,
        series_cap_usd=series_cap,
        min_buy_usd=min_buy_usd,
        ticker_remaining_usd=ticker_remaining_usd,
    )
    if qty == 0:
        out["reason"] = (f"size=0 (price={ask_c}c, cap=${series_cap}, "
                         f"min_buy=${min_buy_usd}, remaining=${ticker_remaining_usd})")
        return out

    out["decision"] = side
    out["qty"] = qty
    out["size_usd"] = round(cost, 2)
    out["reason"] = (
        f"{side} edge={edge*100:.1f}pp P({side[4:]})={prob:.3f} "
        f"μ={mu:.2f}°F σ={sigma:.2f}°F {shape} bracket"
        + (f" rm-locked: {lock_reason}" if locked else "")
        + (" (rm-truncated)" if rm_anchored else "")
    )
    return out
