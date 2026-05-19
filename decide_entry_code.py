"""decide_entry_code.py — pure-code entry decision path (shadow mode).

A drop-in replacement for `judgment.judge_entry()` that produces the same
EntryDecision shape using only deterministic rules. No LLM, no
hallucinations, no latency.

The bot calls this on every packet that survives prescreen, alongside the
LLM dispatch. The LLM still drives real trades; this module's output is
shadow-logged to `data/shadow_code_decisions.jsonl` for A/B analysis.

Decision logic mirrors the prompt's nn_match-only strategy:
  1. Guard: require mu_method to start with nn_match (prescreen ensures
     this, but defensive)
  2. Compute P(YES)/P(NO) from nn_match μ/σ via bracket math
  3. rm anchor (HIGH only): μ = max(μ, rm), re-derive P
  4. Pick edge side; SKIP if both sides below MIN_EDGE_PP
  5. Obs-anchored hard veto (Step 7.5)
  6. Rule #2 — 60pp gap ceiling without rm-lock
  7. Conviction + size_factor from EV table
  8. Pick obs_anchor
  9. Build read + key_risks; return EntryDecision

After ~1 week of shadow data: compare WR + P&L vs LLM, decide cutover.
See `project_code_only_shadow_20260518.md` for the design doc.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional


# ─────────────────────────────────────────────────────────────────────────────
# Result type — mirrors judgment.EntryDecision shape so it can drop into the
# same downstream code. We do NOT import EntryDecision to keep this module
# independent of judgment.py; callers can convert if needed.
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class CodeDecision:
    decision: str                       # "BUY_NO" | "BUY_YES" | "SKIP"
    conviction: float
    size_factor: float
    read: str
    key_risks: list[str]
    what_would_change_my_mind: str
    obs_anchor: str
    obs_anchor_valid: bool
    skip_reason: Optional[str] = None   # populated when decision=="SKIP"
    p_yes: Optional[float] = None       # for diagnostics
    p_no: Optional[float] = None
    edge_pp: Optional[float] = None
    side_picked: Optional[str] = None
    mu_used: Optional[float] = None     # μ after rm anchor (if applied)
    elapsed_sec: float = 0.001          # always near-zero (pure code)
    raw_response: str = "(code path)"
    parse_ok: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Config (mirror prompt + paper_judge_bot.py constants)
# ─────────────────────────────────────────────────────────────────────────────
MIN_EDGE_PP = 0.08          # edge threshold — prompt: "Numerical edge ≥ 8pp"
MAX_GAP_NO_LOCK = 0.60      # Rule #2 — 60pp ceiling unless rm-locked

# EV table from prompt (Step 10 sizing). edge → (conviction, size_factor).
_EV_TABLE: list[tuple[float, float, float]] = [
    # (max_edge_inclusive, conviction, size_factor)
    (0.13, 0.83, 0.50),     # 8-13pp
    (0.18, 0.85, 0.65),     # 13-18pp
    (0.25, 0.87, 0.75),     # 18-25pp
    (0.60, 0.85, 0.65),     # 25-60pp — size DOWN per prompt
]


def _phi(x: float) -> float:
    """Standard normal CDF via math.erf — no scipy dep."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _bracket_label(packet: dict) -> str:
    floor = packet.get("floor")
    cap = packet.get("cap")
    kind = packet.get("bracket_kind") or "?"
    if kind == "B" and floor is not None and cap is not None:
        return f"B{(floor + cap) / 2:.1f}"
    if kind == "T" and floor is not None and cap is None:
        return f"T{floor:.0f} warm tail (floor={floor:.0f})"
    if kind == "T" and cap is not None and floor is None:
        return f"T{cap:.0f} cold tail (cap={cap:.0f})"
    return f"{kind} bracket"


def _is_high(packet: dict) -> bool:
    return "HIGH" in (packet.get("series") or "").upper()


def _h_to_extreme(packet: dict) -> Optional[float]:
    """Hours until next peak (HIGH) or min (LOW). Returns None if missing."""
    clk = packet.get("local_clock") or {}
    lh = clk.get("local_hour")
    if lh is None:
        return None
    ex_hr = clk.get("peak_hour_local") if _is_high(packet) else clk.get("min_hour_local")
    if ex_hr is None:
        return None
    h = ex_hr - lh
    if h < 0:
        h += 24.0
    return h


def _compute_p_yes(mu: float, sigma: float, floor: Optional[float],
                   cap: Optional[float], kind: str) -> Optional[float]:
    """Bracket math from prompt. Returns P(YES) ∈ [0, 1] or None for malformed."""
    if sigma <= 0:
        return None
    if kind == "B" and floor is not None and cap is not None:
        return _phi((cap + 0.5 - mu) / sigma) - _phi((floor - 0.5 - mu) / sigma)
    if kind == "T" and floor is not None and cap is None:        # warm tail
        return 1.0 - _phi((floor + 0.5 - mu) / sigma)
    if kind == "T" and cap is not None and floor is None:        # cold tail
        return _phi((cap - 0.5 - mu) / sigma)
    return None


def _conviction_size_from_edge(edge: float) -> tuple[float, float]:
    """Map edge magnitude to (conviction, size_factor) per prompt's EV table."""
    for max_edge, conv, size in _EV_TABLE:
        if edge <= max_edge:
            return conv, size
    # >60pp — should have been Rule#2-blocked but defensive
    return 0.83, 0.50


def _wethr_lock_for_buy_no(packet: dict) -> Optional[str]:
    """Detect a wethr running-extreme lock that makes BUY_NO a near-certainty.

    Prompt's Step 6A: HIGH lock fires when `rm ≥ cap + 1.0` (overshoot) OR
    `rm ≤ floor − 1.0 AND past_peak_today` (stays-below). LOW mirrors.
    Here we substitute `wethr_high_f` / `wethr_low_f` for rm (they are the
    same wethr-sourced running extreme).

    Returns the lock reason string when fired, None otherwise. When a lock
    fires, the caller should override the edge-driven side pick → BUY_NO
    and bypass Rule #2 (per prompt: "When this lock fires, the
    code-enforced 60pp ceiling is bypassed too").
    """
    floor = packet.get("floor")
    cap = packet.get("cap")
    if floor is None or cap is None or packet.get("bracket_kind") != "B":
        return None
    wo = packet.get("wethr_obs") or {}
    clk = packet.get("local_clock") or {}
    if _is_high(packet):
        wh = wo.get("high_f")
        if wh is None:
            return None
        # Overshoot lock: wethr_high >= cap + 1 (any time)
        if wh >= float(cap) + 1.0:
            return f"overshoot_lock: wethr_high_f={wh} >= cap+1={cap+1}"
        # Stays-below lock: wethr_high <= floor - 1 AND past peak
        if wh <= float(floor) - 1.0 and clk.get("past_peak_today") is True:
            return (f"stays_below_lock: wethr_high_f={wh} <= floor-1={floor-1} "
                    f"AND past_peak_today")
    else:
        wl = wo.get("low_f")
        if wl is None:
            return None
        # Undershoot lock: wethr_low <= floor - 1 (any time)
        if wl <= float(floor) - 1.0:
            return f"undershoot_lock: wethr_low_f={wl} <= floor-1={floor-1}"
        # Stays-above lock: wethr_low >= cap + 1 AND past min
        if wl >= float(cap) + 1.0 and clk.get("past_min_today") is True:
            return (f"stays_above_lock: wethr_low_f={wl} >= cap+1={cap+1} "
                    f"AND past_min_today")
    return None


def _obs_anchored_veto(packet: dict, side: str) -> Optional[str]:
    """Return skip reason if Step 7.5 obs-anchored hard veto fires, else None.

    Fires when: BUY_NO on B-bracket, wethr_temp_f in YES window,
    h_to_extreme > 0.5h, and neither bypass (a) nor (b) holds.
    """
    if side != "BUY_NO":
        return None
    if packet.get("bracket_kind") != "B":
        return None
    floor = packet.get("floor")
    cap = packet.get("cap")
    if floor is None or cap is None:
        return None
    wo = packet.get("wethr_obs") or {}
    wt = wo.get("temp_f")
    if wt is None:
        return None
    yes_lo, yes_hi = float(floor) - 0.5, float(cap) + 0.5
    if not (yes_lo <= wt < yes_hi):
        return None
    # Past-extreme detection: prefer explicit flag, else infer from
    # h_to_extreme wraparound (>12h = past peak/min, not "almost a full
    # day until next one"). Veto only engages when extreme is genuinely
    # ahead within climb/cool time.
    is_high = _is_high(packet)
    clk = packet.get("local_clock") or {}
    past_ex = clk.get("past_peak_today") if is_high else clk.get("past_min_today")
    if past_ex is True:
        return None
    h_to_ex = _h_to_extreme(packet)
    if h_to_ex is None or h_to_ex <= 0.5 or h_to_ex > 12.0:
        return None
    # Bypass (a): wethr running extreme has already escaped
    if is_high:
        wext = wo.get("high_f")
        bypass_a = wext is not None and wext > yes_hi
    else:
        wext = wo.get("low_f")
        bypass_a = wext is not None and wext < yes_lo
    if bypass_a:
        return None

    # Bypass (b): probable band predicts escape AND coherent trend
    if is_high:
        whp = wo.get("highest_probable_f")
        prob_escape = whp is not None and whp > yes_hi
    else:
        wlp = wo.get("lowest_probable_f")
        prob_escape = wlp is not None and wlp < yes_lo
    bypass_b = False
    if prob_escape:
        reg = packet.get("obs_trend_60m_regression") or {}
        slp = reg.get("slope_f_per_h")
        r2 = reg.get("r_squared")
        rng = (packet.get("temp_history_range_60m") or {}).get("range_f")
        slp_sign_ok = slp is not None and (
            (is_high and slp > 0) or (not is_high and slp < 0)
        )
        r2_ok = r2 is not None and r2 >= 0.5
        rng_ok = rng is not None and rng >= 2.0
        bypass_b = slp_sign_ok and r2_ok and rng_ok
    if bypass_b:
        return None

    wext_str = f"{wext}" if wext is not None else "n/a"
    whp_str = "n/a"
    if is_high and wo.get("highest_probable_f") is not None:
        whp_str = str(wo["highest_probable_f"])
    elif not is_high and wo.get("lowest_probable_f") is not None:
        whp_str = str(wo["lowest_probable_f"])
    return (f"obs-anchored veto: wethr_temp_f={wt}°F in YES [{yes_lo:.1f}, {yes_hi:.1f}), "
            f"h_to_extreme={h_to_ex:.2f}h, wethr_extreme={wext_str}, "
            f"wethr_probable={whp_str} (no bypass)")


def _pick_obs_anchor(packet: dict, side: str) -> str:
    """Pick the most decision-relevant obs field as anchor."""
    wo = packet.get("wethr_obs") or {}
    wt = wo.get("temp_f")
    floor = packet.get("floor")
    cap = packet.get("cap")
    rm = packet.get("running_min_or_max")
    reg = packet.get("obs_trend_60m_regression") or {}
    slp = reg.get("slope_f_per_h")
    r2 = reg.get("r_squared")

    # 1. If wethr_temp is near the decision boundary (inside or within 1°F),
    #    it's the most relevant signal.
    if wt is not None and floor is not None and cap is not None:
        yes_lo, yes_hi = float(floor) - 0.5, float(cap) + 0.5
        if (yes_lo - 1.0) <= wt <= (yes_hi + 1.0):
            return f"wethr_temp_f={wt}"
    # 2. If rm is locked clearly outside the bracket, anchor there.
    if rm is not None and floor is not None and cap is not None:
        yes_lo, yes_hi = float(floor) - 0.5, float(cap) + 0.5
        if rm > yes_hi + 1.0 or rm < yes_lo - 1.0:
            return f"running_min_or_max={rm}"
    # 3. Decision-grade 60m slope.
    if slp is not None and r2 is not None and r2 >= 0.5 and abs(slp) >= 1.0:
        return f"obs_trend_60m_slope={slp}"
    # 4. Fallback: rm if present, else wethr_temp.
    if rm is not None:
        return f"running_min_or_max={rm}"
    if wt is not None:
        return f"wethr_temp_f={wt}"
    return ""


def _key_risks(packet: dict, side: str, mu: float, sigma: float,
               edge: float) -> list[str]:
    risks: list[str] = []
    if sigma > 2.5:
        risks.append(f"high nn_match σ ({sigma:.1f}°F) — analog cluster spread")
    floor = packet.get("floor")
    cap = packet.get("cap")
    rm = packet.get("running_min_or_max")
    if rm is not None and floor is not None and cap is not None:
        boundary = float(floor) if side == "BUY_NO" else float(cap)
        if abs(rm - boundary) < 1.0:
            risks.append(f"rm={rm}°F within 1°F of bracket boundary {boundary}°F")
    clk = packet.get("local_clock") or {}
    if clk.get("past_peak_today") and side == "BUY_NO" and _is_high(packet):
        risks.append("past peak — late METAR surge could land in bracket")
    if edge > 0.25:
        risks.append(f"large {edge*100:.0f}pp gap — market disagrees materially")
    return risks


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────
def decide_entry_code(packet: dict) -> CodeDecision:
    """Decide BUY/SKIP via pure code rules. Never raises; on missing data
    or malformed packets, returns SKIP with a descriptive skip_reason."""
    def _skip(reason: str, **extra) -> CodeDecision:
        return CodeDecision(
            decision="SKIP",
            conviction=0.0, size_factor=0.0,
            read=f"(code path) SKIP: {reason}",
            key_risks=[], what_would_change_my_mind="",
            obs_anchor="", obs_anchor_valid=False,
            skip_reason=reason,
            **extra,
        )

    # Step 0 — Defensive guard: require nn_match
    mu_method = (packet.get("mu_method") or packet.get("_edge_info", {}).get("mu_method") or "")
    if not mu_method.startswith("nn_match"):
        return _skip(f"non-nn_match μ ({mu_method[:40] or 'empty'})")

    # Step 1 — Compute P(YES)/P(NO)
    mu = packet.get("mu_chosen") or packet.get("_edge_info", {}).get("mu")
    sigma = packet.get("sigma_chosen") or packet.get("_edge_info", {}).get("sigma")
    if mu is None or sigma is None:
        return _skip("missing mu_chosen or sigma_chosen")
    try:
        mu = float(mu)
        sigma = float(sigma)
    except (TypeError, ValueError):
        return _skip(f"non-numeric mu/sigma: mu={mu!r} sigma={sigma!r}")

    floor = packet.get("floor")
    cap = packet.get("cap")
    kind = packet.get("bracket_kind")

    # Step 2 — rm anchor for HIGH (peak ≥ rm physically)
    rm = packet.get("running_min_or_max")
    mu_used = mu
    if _is_high(packet) and rm is not None:
        try:
            rm_f = float(rm)
            if rm_f > mu_used:
                mu_used = rm_f
        except (TypeError, ValueError):
            pass

    p_yes = _compute_p_yes(mu_used, sigma, floor, cap, kind or "")
    if p_yes is None:
        return _skip(f"cannot compute P(YES): kind={kind} floor={floor} cap={cap} sigma={sigma}")
    p_no = 1.0 - p_yes

    # Step 3 — Pick edge side
    yes_ask_c = packet.get("yes_ask_c")
    no_ask_c = packet.get("no_ask_c")
    if yes_ask_c is None or no_ask_c is None:
        return _skip("missing yes_ask_c or no_ask_c")
    yes_implied = float(yes_ask_c) / 100.0
    no_implied = float(no_ask_c) / 100.0
    edge_yes = p_yes - yes_implied
    edge_no = p_no - no_implied

    # Step 3.5 — wethr-lock check. If wethr's running extreme has already
    # escaped the bracket on the BUY_NO winning side, the outcome is near-
    # certain regardless of edge calc. LLM gets this from the prompt's
    # Step 6A; replicating it in code closes the LAX-B60.5 miss (CODE
    # skipped at edge=+0.080 while LLM correctly BUY_NO'd via wethr_low=59
    # already < floor-1=59).
    lock_reason = _wethr_lock_for_buy_no(packet)
    if lock_reason is not None:
        # Lock overrides everything: BUY_NO at high conviction, skip Step
        # 4 (obs-anchored veto) since the lock IS the obs anchor, skip
        # Step 5 (Rule #2) since the prompt explicitly says locks bypass.
        is_high = _is_high(packet)
        wext = (packet.get("wethr_obs") or {}).get("high_f" if is_high else "low_f")
        anchor_field = "wethr_high_f" if is_high else "wethr_low_f"
        anchor = f"{anchor_field}={wext}"
        # Use no-side edge if positive (we're still buying NO at no_ask).
        # If edge_no is negative the market is paying us a real premium —
        # still take it at moderate size; the lock makes prob ≈ 1.
        conv = 0.90  # high — lock is essentially physical
        size = 0.65 if edge_no >= 0.18 else 0.50
        ts_floor = float(floor) - 0.5 if floor is not None else None
        ts_cap = float(cap) + 0.5 if cap is not None else None
        win_str = (f"[{ts_floor:.1f}, {ts_cap:.1f})"
                   if (ts_floor is not None and ts_cap is not None) else "")
        read = (
            f"(code path) wethr-lock fires: {lock_reason}. YES window "
            f"{win_str} cannot be reached → BUY_NO via physical lock. "
            f"P(NO) by math={p_no:.2f} (informational); price no_ask={no_ask_c}c."
        )
        return CodeDecision(
            decision="BUY_NO",
            conviction=conv, size_factor=size,
            read=read,
            key_risks=["wethr running extreme could be revised (1.6% historical >0.5°F miss rate)"],
            what_would_change_my_mind=f"wethr running extreme revises inside YES window",
            obs_anchor=anchor, obs_anchor_valid=True,
            skip_reason=None,
            p_yes=p_yes, p_no=p_no, edge_pp=edge_no * 100,
            side_picked="BUY_NO", mu_used=mu_used,
        )

    # Step 3.6 — edge threshold check (epsilon-tolerant). MIN_EDGE_PP is
    # 0.08; due to float math, a true 0.08 edge can compute as 0.07999..
    # The earlier LAX-B60.5 case computed edge_yes=0.07999... and got
    # rejected. Allow values within 1e-9 of the threshold.
    if max(edge_yes, edge_no) < MIN_EDGE_PP - 1e-9:
        return _skip(
            f"edge below threshold: edge_yes={edge_yes:+.4f} edge_no={edge_no:+.4f} < {MIN_EDGE_PP}",
            p_yes=p_yes, p_no=p_no, mu_used=mu_used,
        )

    if edge_no >= edge_yes:
        side, edge, P, ask_c = "BUY_NO", edge_no, p_no, no_ask_c
    else:
        side, edge, P, ask_c = "BUY_YES", edge_yes, p_yes, yes_ask_c

    # Step 4 — Obs-anchored hard veto (Step 7.5)
    veto = _obs_anchored_veto(packet, side)
    if veto is not None:
        return _skip(veto, p_yes=p_yes, p_no=p_no, mu_used=mu_used,
                     side_picked=side, edge_pp=edge*100)

    # Step 5 — Rule #2 (60pp gap ceiling) — defensive; prescreen catches it,
    # but if we ever skip the prescreen this still fires. Note: wethr-lock
    # would have bypassed this above per the prompt — we only reach here
    # when no lock fires, so the ceiling applies.
    if edge > MAX_GAP_NO_LOCK:
        return _skip(
            f"Rule#2: gap {edge*100:.0f}pp > {MAX_GAP_NO_LOCK*100:.0f}pp",
            p_yes=p_yes, p_no=p_no, mu_used=mu_used,
            side_picked=side, edge_pp=edge*100,
        )

    # Step 6 — Conviction + size from EV table
    conviction, size_factor = _conviction_size_from_edge(edge)

    # Step 7 — Pick obs_anchor
    anchor = _pick_obs_anchor(packet, side)

    # Step 8 — Read + key_risks + what_would_change_my_mind
    blabel = _bracket_label(packet)
    yes_lo = (float(floor) - 0.5) if floor is not None else None
    yes_hi = (float(cap) + 0.5) if cap is not None else None
    wo = packet.get("wethr_obs") or {}
    wt = wo.get("temp_f")
    reg = packet.get("obs_trend_60m_regression") or {}
    slp = reg.get("slope_f_per_h")
    r2 = reg.get("r_squared")
    rm_disp = rm if rm is not None else "n/a"
    slp_str = f"{slp:+.2f}°F/h" if slp is not None else "n/a"
    r2_str = f"{r2:.2f}" if r2 is not None else "n/a"
    win_str = (f"[{yes_lo:.1f}, {yes_hi:.1f})"
               if (yes_lo is not None and yes_hi is not None)
               else f"(T-bracket: floor={floor} cap={cap})")
    read = (
        f"(code path) {blabel} → YES window {win_str}. "
        f"nn_match μ={mu_used:.1f}°F σ={sigma:.2f}°F → P({side.replace('BUY_','')})={P:.2f} "
        f"vs market {ask_c}c → edge {edge*100:+.1f}pp. "
        f"rm={rm_disp}, wethr_temp={wt}, 60m slope={slp_str} r²={r2_str}."
    )
    risks = _key_risks(packet, side, mu_used, sigma, edge)

    # What would change my mind: the bracket-boundary obs threshold.
    # Defensive against T-brackets where one of floor/cap is None — only
    # format when the relevant boundary exists, else fall through.
    if side == "BUY_NO" and yes_lo is not None and yes_hi is not None:
        # B-bracket BUY_NO
        if _is_high(packet):
            wwcmm = f"wethr_temp_f climbs above {yes_lo:.1f}°F before peak"
        else:
            wwcmm = f"wethr_temp_f drops below {yes_hi:.1f}°F before min"
    elif side == "BUY_NO" and yes_lo is not None:
        # T-warm-tail BUY_NO (cap=None)
        wwcmm = f"wethr_temp_f climbs above {yes_lo:.1f}°F before extreme"
    elif side == "BUY_NO" and yes_hi is not None:
        # T-cold-tail BUY_NO (floor=None)
        wwcmm = f"wethr_temp_f drops below {yes_hi:.1f}°F before extreme"
    elif side == "BUY_YES" and yes_lo is not None and yes_hi is not None:
        wwcmm = f"wethr_temp_f drifts outside [{yes_lo:.1f}, {yes_hi:.1f}) before extreme"
    else:
        wwcmm = "obs trajectory diverges from nn_match μ by ≥1.5°F"

    return CodeDecision(
        decision=side,
        conviction=conviction,
        size_factor=size_factor,
        read=read,
        key_risks=risks,
        what_would_change_my_mind=wwcmm,
        obs_anchor=anchor,
        obs_anchor_valid=True,
        skip_reason=None,
        p_yes=p_yes, p_no=p_no, edge_pp=edge*100,
        side_picked=side, mu_used=mu_used,
    )
