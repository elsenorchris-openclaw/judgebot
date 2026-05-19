"""judgment.py — Claude API caller, prompt builder, response parser.

Critical path: the rest of the bot calls judge_entry() / judge_exit() which
each do (a) build prompt, (b) call Anthropic API with prompt caching,
(c) parse JSON response, (d) return a validated dataclass.

Failures here propagate up to guardrails.record_llm_failure(); after 3 in
a row, new entries pause for 5 minutes.

No network calls in the parser path — keep it deterministic and test-friendly.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import config


log = logging.getLogger("judge.judgment")


PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


# ─────────────────────────────────────────────────────────────────────────────
# Result types
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class EntryDecision:
    decision: Literal["BUY_NO", "BUY_YES", "SKIP"]
    conviction: float
    size_factor: float
    read: str
    key_risks: list[str]
    what_would_change_my_mind: str
    # R2 2026-05-17: required structured obs anchor — Claude must declare
    # which packet obs field anchored the decision. Validator checks the
    # value against the actual packet at parse time (see _validate_obs_anchor).
    obs_anchor: str = ""             # raw "field=value" string from response
    obs_anchor_valid: bool = False   # True iff parsed + matched packet ±tol
    obs_anchor_reason: str = ""      # validation message (empty on success)
    # Trace fields (filled by caller)
    raw_response: str = ""
    parse_ok: bool = True
    parse_error: Optional[str] = None
    elapsed_sec: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    est_cost_usd: float = 0.0


@dataclass
class ExitDecision:
    decision: Literal["HOLD", "SELL_ALL", "SELL_PARTIAL"]
    sell_count: int
    limit_price_cents: Optional[int]
    conviction: float
    read: str
    regret_check: str
    raw_response: str = ""
    parse_ok: bool = True
    parse_error: Optional[str] = None
    elapsed_sec: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    est_cost_usd: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Prompt builders
# ─────────────────────────────────────────────────────────────────────────────
_ENTRY_SYSTEM_CACHED: Optional[str] = None
_EXIT_SYSTEM_CACHED: Optional[str] = None


def _load_static_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text()


def entry_system_prompt() -> str:
    global _ENTRY_SYSTEM_CACHED
    if _ENTRY_SYSTEM_CACHED is None:
        _ENTRY_SYSTEM_CACHED = _load_static_prompt("entry_prompt.md")
    return _ENTRY_SYSTEM_CACHED


def exit_system_prompt() -> str:
    global _EXIT_SYSTEM_CACHED
    if _EXIT_SYSTEM_CACHED is None:
        _EXIT_SYSTEM_CACHED = _load_static_prompt("exit_prompt.md")
    return _EXIT_SYSTEM_CACHED


def _bracket_distance_lines(packet: dict[str, Any]) -> list[str]:
    """Lines that make the bracket geometry explicit for Claude. Shows where
    rm + wethr_temp sit relative to the YES window edges, and (for B-brackets)
    how much further temp would need to move to escape into the overshoot path.

    Empty list for malformed brackets. Series-aware (HIGH vs LOW). Handles
    B-brackets (both edges) + T-brackets (one-sided tails).

    Originally shipped 2026-05-16 morning, removed by an intermediate edit;
    re-added 2026-05-16 evening per prompt audit.
    """
    fl = packet.get("floor")
    cp = packet.get("cap")
    bk = packet.get("bracket_kind")
    series = packet.get("series") or ""
    is_high = "HIGH" in series
    is_low = series.startswith("KXLOW")
    if not (is_high or is_low) or bk not in ("B", "T"):
        return []
    wethr = packet.get("wethr_obs") or {}
    wx = wethr.get("temp_f")
    rm = packet.get("running_min_or_max")
    if wx is None and rm is None:
        return []

    def _fmt(v):
        return f"{v:+.1f}°F" if v is not None else "n/a"

    out: list[str] = []
    if is_high and bk == "B" and fl is not None and cp is not None:
        # YES = [floor-0.5, cap+0.5]. BUY_NO wins if high stays below YES floor
        # OR climbs above YES cap. Both edges matter.
        yes_lo = fl - 0.5
        yes_hi = cp + 0.5
        rm_to_floor = (yes_lo - rm) if rm is not None else None
        wx_to_floor = (yes_lo - wx) if wx is not None else None
        rm_climb = (yes_hi - rm) if rm is not None else None
        wx_climb = (yes_hi - wx) if wx is not None else None
        in_yes_rm = "INSIDE YES from below" if rm_to_floor is not None and rm_to_floor <= 0 else None
        in_yes_wx = "INSIDE YES" if wx_to_floor is not None and wx_to_floor <= 0 else None
        out.append(
            f"- Δ to YES floor ({yes_lo:.1f}°F): rm={_fmt(rm_to_floor)}"
            f"{f' ({in_yes_rm})' if in_yes_rm else ''}, "
            f"wethr={_fmt(wx_to_floor)}"
            f"{f' ({in_yes_wx})' if in_yes_wx else ''}"
        )
        out.append(
            f"- More climb needed for overshoot (>{yes_hi:.1f}°F): "
            f"rm needs {_fmt(rm_climb)}, wethr needs {_fmt(wx_climb)}"
            "  (negative = already escaped above YES)"
        )
        out.append(
            f"- For BUY_NO to win: day's high must STAY BELOW {yes_lo:.1f}°F "
            f"OR CLIMB ABOVE {yes_hi:.1f}°F by peak."
        )
    elif is_low and bk == "B" and fl is not None and cp is not None:
        yes_lo = fl - 0.5
        yes_hi = cp + 0.5
        rm_to_cap = (rm - yes_hi) if rm is not None else None
        wx_to_cap = (wx - yes_hi) if wx is not None else None
        rm_drop = (rm - yes_lo) if rm is not None else None
        wx_drop = (wx - yes_lo) if wx is not None else None
        in_yes_rm = "INSIDE YES from above" if rm_to_cap is not None and rm_to_cap <= 0 else None
        in_yes_wx = "INSIDE YES" if wx_to_cap is not None and wx_to_cap <= 0 else None
        out.append(
            f"- Δ to YES cap ({yes_hi:.1f}°F): rm={_fmt(rm_to_cap)}"
            f"{f' ({in_yes_rm})' if in_yes_rm else ''}, "
            f"wethr={_fmt(wx_to_cap)}"
            f"{f' ({in_yes_wx})' if in_yes_wx else ''}"
        )
        out.append(
            f"- More drop needed for cold-overshoot (<{yes_lo:.1f}°F): "
            f"rm needs {_fmt(rm_drop)}, wethr needs {_fmt(wx_drop)}"
            "  (negative = already dropped below YES)"
        )
        out.append(
            f"- For BUY_NO to win: day's low must STAY ABOVE {yes_hi:.1f}°F "
            f"OR DROP BELOW {yes_lo:.1f}°F by min."
        )
    elif bk == "T":
        if is_high and fl is not None:
            edge = fl + 0.5
            rm_d = (edge - rm) if rm is not None else None
            wx_d = (edge - wx) if wx is not None else None
            in_yes_rm = "INSIDE YES" if rm_d is not None and rm_d <= 0 else None
            in_yes_wx = "INSIDE YES" if wx_d is not None and wx_d <= 0 else None
            out.append(
                f"- Δ to T-bracket boundary ({edge:.1f}°F): rm={_fmt(rm_d)}"
                f"{f' ({in_yes_rm})' if in_yes_rm else ''}, "
                f"wethr={_fmt(wx_d)}"
                f"{f' ({in_yes_wx})' if in_yes_wx else ''}"
            )
            out.append(
                f"- For BUY_NO to win (T-high): day's high must STAY BELOW {edge:.1f}°F."
            )
        elif is_low and cp is not None:
            edge = cp - 0.5
            rm_d = (rm - edge) if rm is not None else None
            wx_d = (wx - edge) if wx is not None else None
            in_yes_rm = "INSIDE YES" if rm_d is not None and rm_d <= 0 else None
            in_yes_wx = "INSIDE YES" if wx_d is not None and wx_d <= 0 else None
            out.append(
                f"- Δ to T-bracket boundary ({edge:.1f}°F): rm={_fmt(rm_d)}"
                f"{f' ({in_yes_rm})' if in_yes_rm else ''}, "
                f"wethr={_fmt(wx_d)}"
                f"{f' ({in_yes_wx})' if in_yes_wx else ''}"
            )
            out.append(
                f"- For BUY_NO to win (T-low): day's low must STAY ABOVE {edge:.1f}°F."
            )
    return out


def build_entry_user_message(packet: dict[str, Any]) -> str:
    """Render the per-candidate situation packet as a Markdown user message.

    `packet` keys (all optional but most are expected):
        ticker, bracket, series, station, label, climate_day, time_now_iso,
        seconds_to_close, action_proposed, side, yes_ask_c, yes_bid_c,
        no_ask_c, no_bid_c, spread_c, volume, recent_prints,
        mu, sigma, mu_source, mu_nbp, mu_nbm, mu_hrrr, mu_ecmwf,
        disagreement_f, model_prob, edge, days_out,
        running_min_or_max, live_obs (dict), obs_trend_30m,
        existing_position (optional dict),
        bot_filters_passed (list — for transparency),
    """
    lines: list[str] = []
    lines.append("## SITUATION")
    lines.append("")
    lines.append(f"- Ticker: `{packet.get('ticker')}` — {packet.get('label','')}")
    lines.append(f"- Series: {packet.get('series')}  Station: {packet.get('station')}")
    lines.append(f"- Bracket: floor={packet.get('floor')}, cap={packet.get('cap')} ({packet.get('bracket_kind','?')})")
    lines.append(f"- Climate day (LST): {packet.get('climate_day')}  Days out: {packet.get('days_out')}")
    if packet.get("climate_day_start_utc"):
        lines.append(f"  - LST window: {packet.get('climate_day_start_utc')} → {packet.get('climate_day_end_utc')}")
        sss = packet.get("seconds_since_climate_start")
        if sss is not None:
            lines.append(f"  - {sss/3600:.1f}h into the climate day (of 24h)")
    lines.append(f"- Now (UTC): {packet.get('time_now_iso')}")
    secs = packet.get("seconds_to_close")
    if secs is not None:
        lines.append(f"- Time to close: {secs/60:.1f} min ({secs/3600:.2f} h)")
    lines.append("")
    lines.append("## MARKET")
    lines.append(f"- yes_bid/ask: {packet.get('yes_bid_c')}c / {packet.get('yes_ask_c')}c")
    lines.append(f"- no_bid/ask: {packet.get('no_bid_c')}c / {packet.get('no_ask_c')}c")
    lines.append(f"- spread: {packet.get('spread_c')}c   volume: {packet.get('volume')}")
    prints = packet.get("recent_prints") or []
    if prints:
        lines.append(f"- recent prints (last hr, NO side): {prints}")
    lines.append("")

    # Forecast section rendering retired 2026-05-17 21:xx UTC (Option 2 strip
    # — all forecast content). Settled-BUY leakage audit: 78-93% of Claude
    # reads cited forecasts (NBM/HRRR/ECMWF/NBP/mean_gap/etc.) despite
    # prompt-side bans, and the 30-trade settled forecast-anchored pool was
    # -15.8% ROI. Strategy is now obs + nn_match only; forecast data is
    # still computed and logged to candidates.jsonl for retroactive
    # analysis, but not shown to the LLM. To re-enable for A/B, flip
    # RENDER_FORECASTS to True. Sections gated below:
    #   ## FORECASTS, ## HOURLY FORECAST, ## PACE TRAJECTORY,
    #   ## RECENT MODEL ACCURACY, ## FORECAST RUN-OVER-RUN DELTAS,
    #   ## 3-DAY PERSISTENCE, ## NWS AREA FORECAST DISCUSSION
    RENDER_FORECASTS = False

    if RENDER_FORECASTS:
        lines.append("## FORECASTS")
        if packet.get("disagreement_f") is not None:
            lines.append(f"- disagreement (max pairwise diff): {packet.get('disagreement_f')}°F")
        sources = []
        for s in ("nbp", "nbm", "hrrr", "ecmwf"):
            v = packet.get(f"mu_{s}")
            if v is not None:
                ages = packet.get("source_ages_sec") or {}
                key = {"nbp":"NBP","nbm":"NBM","hrrr":"HRRR","ecmwf":"ECMWF-IFS"}[s]
                a = ages.get(key)
                age_str = f" ({a/60:.0f}m old)" if a else ""
                sources.append(f"{s.upper()}={v}°F{age_str}")
        if sources:
            lines.append(f"- per-source: {', '.join(sources)}")
        if packet.get("nbp_p10") is not None:
            lines.append(
                f"- NBP percentiles: p10={packet.get('nbp_p10')}, "
                f"p50={packet.get('nbp_p50')}, p90={packet.get('nbp_p90')}, "
                f"σ={packet.get('nbp_sigma')}"
            )
        ei = packet.get("_edge_info")
        if ei:
            lines.append(
                f"- numerical baseline (median μ): {ei['side']} with edge {ei['edge']:+.3f} "
                f"(model P={ei['prob']:.3f}; μ_med={ei['mu']:.1f}°F σ={ei['sigma']:.2f}°F)"
            )
        lines.append("")

    # 2026-05-17: NN_MATCH block. The prompt's Step 2 gates on mu_method and
    # Step 3 cites mu_chosen / sigma_chosen — these fields must be in the
    # rendered packet or every dispatch hallucinates "no nn_match signal".
    # Flip RENDER_NN_MATCH to False to disable the block (instant rollback).
    #
    # Rich-metadata expansion 2026-05-17 (post-k50 ship): also render
    # n_neighbors, pool_size, extreme_locked, sigma_raw vs sigma_chosen,
    # bias_correction, fit_quality_thresh, and the top-3 analog neighbors.
    # Source: packet["_nn_match_meta"], stashed by paper_judge_bot.py
    # when shadow_nn_proj returns a usable μ. Without these the prompt's
    # examples reference fields the rendered packet doesn't carry — Claude
    # then fabricates plausible-looking values.
    RENDER_NN_MATCH = True
    if RENDER_NN_MATCH:
        # 2026-05-18: mu_method / mu / sigma live inside packet["_edge_info"]
        # (the dict returned by _numerical_edge in paper_judge_bot.py:1340).
        # Reading top-level keys returned None for every nn_match candidate,
        # so the block never rendered and Claude SKIPped every one with
        # "no nn_match signal". Fall back to top-level keys defensively in
        # case a future refactor flattens them.
        ei = packet.get("_edge_info") or {}
        mu_method = ei.get("mu_method") or packet.get("mu_method")
        mu_chosen = ei.get("mu") if ei.get("mu") is not None else packet.get("mu_chosen")
        sigma_chosen = ei.get("sigma") if ei.get("sigma") is not None else packet.get("sigma_chosen")
        meta = packet.get("_nn_match_meta") or {}
        if mu_method or mu_chosen is not None or sigma_chosen is not None:
            lines.append("## NN_MATCH")
            lines.append(f"- mu_method: {mu_method}")
            if mu_chosen is not None:
                _sig = f"{sigma_chosen:.2f}" if sigma_chosen is not None else "?"
                lines.append(f"- mu_chosen: {mu_chosen:.1f}°F   sigma_chosen: {_sig}°F")
            else:
                lines.append("- mu_chosen: None   sigma_chosen: None")
            if meta:
                _nn = meta.get("n_neighbors")
                _pool = meta.get("pool_size")
                _locked = meta.get("extreme_locked")
                _sraw = meta.get("sigma_raw")
                _sfac = meta.get("sigma_factor")
                _bias = meta.get("bias_correction")
                _fqt = meta.get("fit_quality_thresh")
                if _nn is not None or _pool is not None:
                    lines.append(
                        f"- n_neighbors: {_nn}   pool_size: {_pool}   "
                        f"extreme_locked: {bool(_locked)}"
                    )
                if _sraw is not None:
                    _sfac_str = f"{_sfac:.2f}" if isinstance(_sfac, (int, float)) else "?"
                    lines.append(
                        f"- sigma_raw (neighbor cluster stdev): {_sraw:.2f}°F   "
                        f"sigma_factor applied: ×{_sfac_str}  "
                        f"(then floored by intraday range / disagreement → sigma_chosen)"
                    )
                if _bias is not None:
                    lines.append(
                        f"- bias_correction applied to median Δ: {_bias:+.2f}°F  "
                        f"(hour-aware for HIGH: morning−0.3 / afternoon+0.3; LOW=0.0)"
                    )
                if _fqt is not None:
                    lines.append(
                        f"- fit_quality_thresh: {_fqt}°F (matcher returns null μ when "
                        f"neighbor cluster stdev exceeds this — μ here PASSED the gate)"
                    )
                # 2026-05-18: replace cherry-pick top-3 framing with full-distribution
                # view + bracket-fraction. Top-3 retained as cross-check only.
                # Backtest 2024-25 n=2308: top3_med is +9% (HIGH) / +12% (LOW) WORSE
                # MAE than median-of-50 — the LLM trusting top-3 over mu_chosen is
                # an antipattern. PHL B96.5 2026-05-18 loss was caused by exactly
                # this (top-3 settled 93-95°F → LLM read undershoot → BUY_NO at
                # 35c; actual hit 96.8°F mid-YES window).
                RENDER_NN_MATCH_DISTRIBUTION = True
                asum = meta.get("analog_summary") or {}
                de_pcts = asum.get("day_extremes_p25_p50_p75")
                dl_pcts = asum.get("deltas_p25_p50_p75")
                _extremes_arr = asum.get("day_extremes") or []
                _in_p = meta.get("analog_in_bracket_pct")
                _abv_p = meta.get("analog_above_pct")
                _bel_p = meta.get("analog_below_pct")
                _cur_t = meta.get("cur_tmpf")
                if RENDER_NN_MATCH_DISTRIBUTION and de_pcts and len(_extremes_arr) > 0:
                    lines.append(
                        f"- analog distribution (all {len(_extremes_arr)} neighbors’ settled day_extremes):"
                    )
                    lines.append(
                        f"  - day_extreme: p25={de_pcts[0]:.1f}°F  p50={de_pcts[1]:.1f}°F  p75={de_pcts[2]:.1f}°F"
                    )
                    if dl_pcts and _cur_t is not None:
                        lines.append(
                            f"  - Δ from cur_tmpf={_cur_t:.1f}°F:  p25={dl_pcts[0]:+.1f}°F  p50={dl_pcts[1]:+.1f}°F  p75={dl_pcts[2]:+.1f}°F"
                        )
                    if _in_p is not None:
                        _parts = [f"in YES window {_in_p}%"]
                        if _abv_p is not None: _parts.append(f"above {_abv_p}%")
                        if _bel_p is not None: _parts.append(f"below {_bel_p}%")
                        lines.append(f"  - bracket-fraction: {'  '.join(_parts)}")
                neighbors = meta.get("top_neighbors") or []
                if neighbors:
                    lines.append(
                        "- top-3 CLOSEST analogs (cross-check ONLY; these are cherry-picks, "
                        "NOT the central estimate — trust mu_chosen / distribution over these):"
                    )
                    for nb in neighbors[:3]:
                        _dt = nb.get("date")
                        _delta = nb.get("delta_f")
                        _ext_key = "day_max_f" if "day_max_f" in nb else "day_min_f"
                        _ext = nb.get(_ext_key)
                        _trm = nb.get("tmpf_rmse")
                        lines.append(
                            f"  - {_dt}: Δ from cur={_delta:+.2f}°F, day_extreme={_ext}°F, "
                            f"tmpf_rmse={_trm}°F"
                        )
            lines.append("")

    # 2026-05-15: single LIVE OBS block (wethr-only). NWS METAR is no longer
    # shown to the LLM — it's used internally for backtest analytics only.
    # The 30m trend is now wethr-derived (computed from temp_history in the
    # shared cache); running max/min has always been wethr (wethr_rm).
    wethr = packet.get("wethr_obs") or {}
    if wethr:
        lines.append("## LIVE OBS")
        wx_age = wethr.get("age_sec")
        age_str = f"{wx_age:.0f}s" if wx_age is not None else "?"
        lines.append(f"- wethr_temp_f: {wethr.get('temp_f')}°F   dew_point_f: {wethr.get('dew_point_f')}°F   relative_humidity: {wethr.get('relative_humidity')}%   age_sec: {age_str}")
        if wethr.get("obs_source") == "poll_fallback":
            lines.append(
                "- source: 10-min poll fallback (metar co-op station, e.g. KNYC). "
                "Trend resolution is coarser (~6 points/hr vs ~30 for ASOS); weight 60m r² less."
            )
        clc = wethr.get("cloud_layer_count")
        sky_label = {0: "Clear", 1: "Few clouds", 2: "Scattered"}.get(clc, "Overcast" if (clc is not None and clc >= 3) else "?")
        lines.append(f"- sky: {sky_label} (cloud_layer_count={clc})")
        lines.append(f"- wind: {wethr.get('wind_speed_mph')} mph   gust: {wethr.get('wind_gust_mph')} mph")
        if wethr.get("pressure_tendency") is not None:
            lines.append(f"- pressure_tendency: {wethr.get('pressure_tendency')}")
        if wethr.get("anomaly_f") is not None:
            lines.append(f"- anomaly vs normal: {wethr.get('anomaly_f'):+.1f}°F")
        trend = packet.get("obs_trend_30m")
        if trend is not None:
            lines.append(f"- obs_trend_30m: {trend:+.2f}°F (point-in-point delta over last 30 min)")
        trend_reg = packet.get("obs_trend_60m_regression") or {}
        if trend_reg.get("slope_f_per_h") is not None:
            lines.append(
                f"- obs_trend_60m_slope: {trend_reg['slope_f_per_h']:+.2f}°F/h  "
                f"obs_trend_60m_r_squared: {trend_reg.get('r_squared')}  "
                f"n={trend_reg.get('n_points')}  span={trend_reg.get('span_min')}min"
            )
        th_rng = packet.get("temp_history_range_60m") or {}
        if th_rng.get("range_f") is not None:
            lines.append(
                f"- temp_history_range_60m: {th_rng['range_f']:.1f}°F  "
                f"(n={th_rng.get('n')} pts, span={th_rng.get('span_min')}min)"
            )
        rm = packet.get("running_min_or_max")
        if rm is not None:
            _is_high = "HIGH" in (packet.get('series') or '')
            rm_label = "max" if _is_high else "min"
            # 2026-05-16 (Chris): rm age + UTC timestamp it was set.
            # Short age + h_to_peak ≥ 0 = rm being driven, expect movement.
            # Long age + past_peak/past_min = settled extreme, high lock conf.
            _age_sec = (packet.get("rm_age_max_sec") if _is_high
                        else packet.get("rm_age_min_sec"))
            _time_of = (wethr.get("time_of_high_utc") if _is_high
                        else wethr.get("time_of_low_utc"))
            age_str = ""
            if _age_sec is not None:
                age_str = f"   (set {_age_sec/3600:.1f}h ago"
                if _time_of:
                    age_str += f" at {_time_of} UTC"
                age_str += ")"
            # Surface both the wethr_obs alias (wethr_high_f / wethr_low_f)
            # and the top-level alias (running_min_or_max) so Claude knows
            # the obs_anchor field names that map to this value.
            _wethr_alias = "wethr_high_f" if _is_high else "wethr_low_f"
            lines.append(f"- running_min_or_max ({_wethr_alias}, running_{rm_label} today): {rm}°F{age_str}")
        if wethr.get("highest_probable_f") is not None or wethr.get("lowest_probable_f") is not None:
            lines.append(
                f"- wethr_lowest_probable_f: {wethr.get('lowest_probable_f')}°F   "
                f"wethr_highest_probable_f: {wethr.get('highest_probable_f')}°F  "
                f"(per-snapshot uncertainty band, NOT a daily forecast)"
            )
        if wethr.get("suspect_temperature"):
            lines.append("- ⚠ suspect_temperature flag set on this obs")
        # Bracket geometry: distances to YES window + overshoot path.
        for ln in _bracket_distance_lines(packet):
            lines.append(ln)
        lines.append("")

    # Local clock + climate normals
    clock = packet.get("local_clock")
    if clock:
        lines.append("## LOCAL CLOCK + DIURNAL")
        lines.append(f"- local time: {clock.get('local_iso')}  (hour={clock.get('local_hour'):.2f}, {clock.get('local_dow')})")
        lines.append(f"- typical peak hour local: {clock.get('peak_hour_local')}   h_to_peak: {clock.get('h_to_peak'):+.2f}  past_peak: {clock.get('past_peak_today')}")
        lines.append(f"- typical min hour local: {clock.get('min_hour_local')}   h_to_min: {clock.get('h_to_min'):+.2f}  past_min: {clock.get('past_min_today')}")
    if packet.get("climate_normal_peak_f") is not None:
        lines.append(f"- climate normals (this month): peak {packet.get('climate_normal_peak_f')}°F, low {packet.get('climate_normal_low_f')}°F")
    lines.append("")

    # Hourly forecast trajectory (next 12h is most relevant)
    hfc = packet.get("hourly_forecast_24h") or []
    if RENDER_FORECASTS and hfc:
        lines.append("## HOURLY FORECAST (next 12h)")
        lines.append("h | utc | tempF | dewF | wind | cloud% | precip% | wx")
        for r in hfc[:12]:
            lines.append(
                f"+{r.get('hour_offset'):>2}h | {r.get('utc_iso')} | "
                f"{r.get('temp_f') if r.get('temp_f') is not None else '-':>5} | "
                f"{r.get('dewpt_f') if r.get('dewpt_f') is not None else '-':>4} | "
                f"{r.get('wind_mph') if r.get('wind_mph') is not None else '-':>4} | "
                f"{r.get('sky_cover_pct') if r.get('sky_cover_pct') is not None else '-':>5} | "
                f"{r.get('precip_prob_pct') if r.get('precip_prob_pct') is not None else '-':>5} | "
                f"{r.get('weather') or '-'}"
            )
        lines.append("")

    # Hourly obs history today
    hist = packet.get("hourly_obs_today") or []
    if hist:
        lines.append("## HOURLY OBS TODAY (climate-day so far)")
        lines.append("hr | utc | tempF | dewF | wind | sky")
        for r in hist[-12:]:  # last 12 entries
            lines.append(
                f"+{r.get('hour_offset_h'):>2}h | {r.get('hour_utc_iso')} | "
                f"{r.get('temp_f') if r.get('temp_f') is not None else '-':>5} | "
                f"{r.get('dewpt_f') if r.get('dewpt_f') is not None else '-':>4} | "
                f"{r.get('wind_mph') if r.get('wind_mph') is not None else '-':>4} | "
                f"{r.get('sky_short') or '-'}"
            )
        lines.append("")

    # PACE / TAIL band rendering RETIRED 2026-05-17.
    # The pace_band, tail_band, pace_low_band, tail_low_band data is still
    # computed by the packet builder and SAVED into candidates.jsonl for
    # retroactive analysis, but no longer rendered into the LLM packet.
    # Backtest n=178: overlap-with-YES-window veto blocked BUY_NO winners
    # 100% of the time (0/22 overlap brackets settled YES). See
    # project_pace_band_retirement_20260517.md. To re-enable rendering
    # (e.g., for an A/B test), flip RENDER_PACE_BAND to True below.
    RENDER_PACE_BAND = False
    if RENDER_PACE_BAND:
        pb = packet.get("pace_band")
        if pb and pb.get("median") is not None:
            _res = packet.get("pace_band_resolution") or "monthly"
            _emp = packet.get("empirical_peak_hour_local")
            lines.append(f"## PACE BAND — HIGH ({_res} resolution, 3-yr ASOS)")
            lines.append(
                f"- pace p25: {pb.get('p25')}   median: {pb.get('median')}   p75: {pb.get('p75')}"
                f"   (n={pb.get('n')} days)"
            )
            lines.append(f"- empirical_peak_hour_local (data-derived): {_emp}")
            lines.append("")
        tb = packet.get("tail_band")
        if tb and tb.get("p75") is not None:
            _res = packet.get("tail_band_resolution") or "monthly"
            lines.append(f"## TAIL BAND — HIGH ({_res})")
            lines.append(
                f"- p50: +{tb.get('p50'):.1f}°F   p75: +{tb.get('p75'):.1f}°F   "
                f"p90: +{tb.get('p90'):.1f}°F   p95: +{tb.get('p95'):.1f}°F"
            )
            lines.append("")
        plb = packet.get("pace_low_band")
        if plb and plb.get("median") is not None:
            _res = packet.get("pace_low_band_resolution") or "monthly"
            _emp = packet.get("empirical_min_hour_local")
            lines.append(f"## PACE BAND — LOW ({_res} resolution, 3-yr ASOS)")
            lines.append(
                f"- pace_low p25: {plb.get('p25')}   median: {plb.get('median')}   "
                f"p75: {plb.get('p75')}   (n={plb.get('n')} days)"
            )
            lines.append(f"- empirical_min_hour_local (data-derived): {_emp}")
            lines.append("")
        tlb = packet.get("tail_low_band")
        if tlb and tlb.get("p75") is not None:
            _res = packet.get("tail_low_band_resolution") or "monthly"
            lines.append(f"## TAIL BAND — LOW ({_res})")
            lines.append(
                f"- p50: −{tlb.get('p50'):.1f}°F   p75: −{tlb.get('p75'):.1f}°F   "
                f"p90: −{tlb.get('p90'):.1f}°F   p95: −{tlb.get('p95'):.1f}°F"
            )
            lines.append("")

    # Pace divergence slope — diagnostic of forecast-bust-in-progress.
    pace = packet.get("obs_vs_forecast_pace_slope") or {}
    if RENDER_FORECASTS and pace.get("slope_per_h") is not None:
        lines.append("## PACE TRAJECTORY (obs vs hourly forecast)")
        lines.append(
            f"- gap slope: {pace['slope_per_h']:+.2f}°F/h  "
            f"current gap: {pace['current_gap_f']:+.1f}°F  "
            f"mean gap (last {pace['n_hours']}h): {pace['mean_gap_f']:+.1f}°F"
        )
        lines.append(
            "  ↳ positive slope = obs running INCREASINGLY hotter than forecast "
            "(forecast peak likely too low for HIGH bracket); "
            "negative slope = obs falling behind forecast "
            "(forecast peak likely too high for HIGH bracket; LOW: real min may be colder)"
        )
        lines.append("")
    elif RENDER_FORECASTS:
        # F3 2026-05-17: surface why pace_slope is null so the LLM can cite
        # the gap and downgrade rather than silently ignoring it.
        _reason = packet.get("pace_slope_unavailable_reason")
        if _reason:
            _reason_msg = {
                "no_obs": "no hourly obs in last 12h (wethr cache fresh-empty)",
                "no_fc_sources": "no forecast available (NWS gridpoint + snapshots both empty)",
                "all_pairs_unmatched": "obs hours exist but no matching forecast snapshots — data gap",
                "insufficient_pairs": "<2 matched (obs, forecast) hourly pairs available",
                "degenerate_x": "all matched pairs collapsed to a single hour offset",
            }.get(_reason, _reason)
            lines.append("## PACE TRAJECTORY (obs vs hourly forecast)")
            lines.append(
                f"- pace_slope unavailable ({_reason_msg}). Fall back to "
                f"`wethr_high_f`/`wethr_low_f` vs forecast peak, and to the "
                f"60m regression slope. Do not infer forecast-bust direction "
                f"from a single-hour delta."
            )
            lines.append("")

    # Per-model MAE — shape: {per_model: {model: {mae,bias,rmse}}, best, worst, ensemble}
    mae = packet.get("model_mae_recent") or {}
    per_model = (mae or {}).get("per_model") if isinstance(mae, dict) else None
    if RENDER_FORECASTS and per_model:
        lines.append("## RECENT MODEL ACCURACY (last 14 days) for this station + direction")
        best = mae.get("best") or {}
        worst = mae.get("worst") or {}
        ensemble = mae.get("ensemble") or {}
        if best.get("model"):
            lines.append(f"- BEST: {best['model']} (MAE {best.get('mae')}°F, bias {best.get('bias'):+.2f}°F)")
        if worst.get("model"):
            lines.append(f"- WORST: {worst['model']} (MAE {worst.get('mae')}°F, bias {worst.get('bias'):+.2f}°F)")
        if ensemble.get("mae") is not None:
            lines.append(f"- Ensemble: MAE {ensemble.get('mae')}°F, bias {ensemble.get('bias'):+.2f}°F")
        # Surface just the 4 models we use directly (NBM, HRRR, ECMWF-IFS, NBP)
        focused = []
        for m in ("NBM", "HRRR", "ECMWF-IFS", "ECMWF-HRES", "GEFS", "GFS"):
            if m in per_model:
                pm = per_model[m]
                focused.append(f"{m}=MAE {pm['mae']:.2f} bias {pm['bias']:+.2f}")
        if focused:
            lines.append("- Our forecast sources: " + ", ".join(focused))
        lines.append("- Lean toward the lower-MAE model when sources disagree; subtract a model's persistent bias from its forecast.")
        lines.append("")

    # Forecast deltas
    fd = packet.get("forecast_deltas") or {}
    if RENDER_FORECASTS and fd:
        lines.append("## FORECAST RUN-OVER-RUN DELTAS (this climate day)")
        for model, info in fd.items():
            vals = info.get("chrono_values") or []
            d = info.get("delta_f")
            trend = info.get("trend")
            if vals and len(vals) >= 2:
                lines.append(
                    f"- {model}: {' → '.join(str(v) for v in vals)}  "
                    f"(Δ {d:+.1f}°F over {len(vals)} runs, trend: {trend})"
                )
        lines.append("")

    # Persistence bias
    pb = packet.get("persistence_3day") or {}
    if RENDER_FORECASTS and pb and pb.get("by_day"):
        lines.append("## 3-DAY PERSISTENCE (actual vs forecast)")
        for d in pb["by_day"]:
            a = d.get("actual_f"); f = d.get("forecast_f"); b = d.get("bias_f")
            lines.append(f"- {d.get('climate_date')}: actual={a} forecast={f} bias={b}")
        if pb.get("mean_bias_f") is not None:
            lines.append(f"- Mean bias: {pb['mean_bias_f']:+.1f}°F")
        lines.append("")

    # AFD excerpt. 2026-05-16: many WFOs (e.g., HGX) don't break out SYNOPSIS
    # or SHORT TERM as labeled sections — the section splitter only catches
    # specific .HEADER... markers. For those WFOs the named subsections come
    # back empty even when the AFD has real content. Render Discussion when
    # present, then fall back to full_excerpt so Claude never sees just the
    # "Issued:" header with no body.
    afd = packet.get("afd")
    if RENDER_FORECASTS and afd:
        rendered = False
        lines.append("## NWS AREA FORECAST DISCUSSION (synoptic context)")
        lines.append(f"- Issued: {afd.get('issued_iso')}  (WFO: {afd.get('office')})")
        if afd.get("synopsis"):
            lines.append("### Synopsis")
            lines.append(afd["synopsis"])
            rendered = True
        if afd.get("short_term"):
            lines.append("### Short term")
            lines.append(afd["short_term"])
            rendered = True
        if afd.get("discussion"):
            lines.append("### Discussion")
            lines.append(afd["discussion"])
            rendered = True
        if not rendered and afd.get("full_excerpt"):
            lines.append("### Full excerpt")
            lines.append(afd["full_excerpt"])
        lines.append("")

    existing = packet.get("existing_position")
    if existing:
        lines.append("## EXISTING POSITION (we already hold)")
        lines.append(f"- {existing.get('count')}x {existing.get('action')} @ ${existing.get('entry_price'):.2f}")
        lines.append("")
    lines.append("Return your decision as JSON per the schema above. No extra text.")
    return "\n".join(lines)


def build_exit_user_message(packet: dict[str, Any]) -> str:
    """Render an open-position exit-decision packet."""
    lines: list[str] = []
    lines.append("## POSITION")
    lines.append(f"- Ticker: `{packet.get('ticker')}` — {packet.get('label','')}")
    lines.append(f"- {packet.get('count')}x {packet.get('action')} @ ${packet.get('entry_price'):.2f} (cost ${packet.get('cost'):.2f})")
    lines.append(f"- Bracket: floor={packet.get('floor')}, cap={packet.get('cap')}")
    lines.append(f"- Now (UTC): {packet.get('time_now_iso')}   Time to close: {packet.get('seconds_to_close',0)/60:.1f} min")
    lines.append("")
    lines.append("## LIVE MARKET")
    lines.append(f"- bid/ask (our side): {packet.get('bid_side_c')}c / {packet.get('ask_side_c')}c")
    lines.append(f"- spread: {packet.get('spread_c')}c   volume: {packet.get('volume')}")
    lines.append(f"- current MTM: {packet.get('mtm_pct')}  peak: {packet.get('peak_mtm_pct')}  trough: {packet.get('trough_mtm_pct')}")
    prints = packet.get("recent_prints") or []
    if prints:
        lines.append(f"- recent prints: {prints}")
    lines.append("")
    # 2026-05-15: exit prompt now sees wethr-only LIVE OBS, matching entry.
    wethr = packet.get("wethr_obs") or {}
    if wethr:
        lines.append("## LIVE OBS")
        wx_age = wethr.get("age_sec")
        age_str = f"{wx_age:.0f}s" if wx_age is not None else "?"
        lines.append(f"- wethr_temp_f: {wethr.get('temp_f')}°F   dew_point_f: {wethr.get('dew_point_f')}°F   relative_humidity: {wethr.get('relative_humidity')}%   age_sec: {age_str}")
        if wethr.get("obs_source") == "poll_fallback":
            lines.append(
                "- source: 10-min poll fallback (metar co-op station, e.g. KNYC). "
                "Trend resolution is coarser; weight 60m r² less."
            )
        clc = wethr.get("cloud_layer_count")
        sky_label = {0: "Clear", 1: "Few clouds", 2: "Scattered"}.get(clc, "Overcast" if (clc is not None and clc >= 3) else "?")
        lines.append(f"- sky: {sky_label} (cloud_layer_count={clc})")
        lines.append(f"- wind: {wethr.get('wind_speed_mph')} mph   gust: {wethr.get('wind_gust_mph')} mph")
    rm = packet.get("running_min_or_max")
    if rm is not None:
        lines.append(f"- running: {rm}°F   peak running: {packet.get('peak_running')}°F")
    trend = packet.get("obs_trend_30m")
    if trend is not None:
        lines.append(f"- 30m trend (point-in-point): {trend:+.2f}°F")
    trend_reg = packet.get("obs_trend_60m_regression") or {}
    if trend_reg.get("slope_f_per_h") is not None:
        lines.append(
            f"- 60m trend (regression): {trend_reg['slope_f_per_h']:+.2f}°F/h  "
            f"r²={trend_reg.get('r_squared')}  n={trend_reg.get('n_points')}"
        )
    # Bracket geometry: distances to YES window + overshoot path.
    for ln in _bracket_distance_lines(packet):
        lines.append(ln)
    lines.append("")
    lines.append(f"## EXIT TRIGGER: {packet.get('trigger_reason','(manual call)')}")
    lines.append(f"  triggered_by_anomaly: {packet.get('triggered',False)}")
    lines.append("")
    lines.append("Return JSON per schema. No extra text.")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Response parser (PURE — no network, fully tested)
# ─────────────────────────────────────────────────────────────────────────────
_ALLOWED_ENTRY = {"BUY_NO", "BUY_YES", "SKIP"}
_ALLOWED_EXIT = {"HOLD", "SELL_ALL", "SELL_PARTIAL"}


def _extract_json(text: str) -> Optional[dict]:
    """Pull the first JSON object out of a string. Handles fenced ```json ... ```
    blocks and bare {...}. Returns None on no parseable object."""
    if not text:
        return None
    # Try fenced block first
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Try bare {...} — greedy from first { to last }
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    return None


# R2 2026-05-17: structured obs anchor — allowed field names + how to look
# them up in a packet + numeric tolerance for the validator. Tolerances are
# generous because wethr_obs updates every ~5s and the value Claude saw in
# the prompt may have refreshed by the time we validate. The tolerance is
# intentionally LOOSE — purpose is to catch fabrication, not pinpoint-match.
_OBS_ANCHOR_FIELDS: dict[str, tuple] = {
    # name: (packet_lookup_fn, abs_tol)
    # 2026-05-17: removed `temp_obs` (NWS METAR fallback retired by wethr-only
    # policy 2026-05-15) and `pace_slope` / `pace_slope_current_gap` (forecast-
    # derived signals explicitly banned by prompt's "Forbidden citations" list
    # but silently accepted by this validator — inconsistency closed).
    "wethr_temp_f":              (lambda p: (p.get("wethr_obs") or {}).get("temp_f"),            1.0),
    "wethr_high_f":              (lambda p: (p.get("wethr_obs") or {}).get("high_f"),            1.0),
    "wethr_low_f":               (lambda p: (p.get("wethr_obs") or {}).get("low_f"),             1.0),
    "wethr_highest_probable_f":  (lambda p: (p.get("wethr_obs") or {}).get("highest_probable_f"),1.0),
    "wethr_lowest_probable_f":   (lambda p: (p.get("wethr_obs") or {}).get("lowest_probable_f"), 1.0),
    "running_min_or_max":        (lambda p: p.get("running_min_or_max"),                         1.0),
    "rm":                        (lambda p: p.get("running_min_or_max"),                         1.0),   # alias
    "obs_trend_30m":             (lambda p: p.get("obs_trend_30m"),                              0.5),
    "obs_trend_60m_slope":       (lambda p: ((p.get("obs_trend_60m_regression") or {}).get("slope_f_per_h")),   0.5),
    "obs_trend_60m_r_squared":   (lambda p: ((p.get("obs_trend_60m_regression") or {}).get("r_squared")),       0.15),
    "temp_history_range_60m":    (lambda p: ((p.get("temp_history_range_60m") or {}).get("range_f")),           0.5),
}


def _validate_obs_anchor(anchor_raw: str, packet: dict) -> tuple[bool, str]:
    """Parse Claude's obs_anchor field and verify against the packet.

    Expected format: '<field_name>=<number>' (units like °F optional).
    Returns (valid, reason). reason is empty on success.
    """
    if not anchor_raw or not isinstance(anchor_raw, str):
        return False, "empty"
    s = anchor_raw.strip()
    # Allow trailing units like "°F", "F", "/h" — strip after value.
    m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?)", s)
    if not m:
        return False, "unparsable (need 'field=value'): " + s[:80]
    field = m.group(1)
    try:
        claimed = float(m.group(2))
    except ValueError:
        return False, "value not numeric: " + m.group(2)
    if field not in _OBS_ANCHOR_FIELDS:
        return False, "unknown field {!r}; allowed: {}".format(
            field, ",".join(sorted(_OBS_ANCHOR_FIELDS))[:200])
    lookup_fn, tol = _OBS_ANCHOR_FIELDS[field]
    try:
        actual = lookup_fn(packet)
    except Exception as e:
        return False, "lookup error for {!r}: {}".format(field, e)
    if actual is None:
        return False, "field {!r} is null in packet".format(field)
    try:
        actual_f = float(actual)
    except (TypeError, ValueError):
        return False, "packet field {!r} not numeric: {!r}".format(field, actual)
    if abs(actual_f - claimed) > tol:
        return False, "value mismatch: claimed {} vs packet {} (tol {})".format(
            claimed, actual_f, tol)
    return True, ""


def parse_entry_response(text: str, packet: Optional[dict] = None) -> EntryDecision:
    """Parse a Claude response into an EntryDecision. Always returns a
    decision — on parse failure, returns SKIP with parse_error set.

    R2 2026-05-17: if `packet` is provided, validates the `obs_anchor`
    field against actual packet values. Callers that don't pass packet get
    obs_anchor_valid=False with reason 'no packet for validation'.
    """
    obj = _extract_json(text or "")
    if obj is None:
        return EntryDecision(
            decision="SKIP",
            conviction=0.0,
            size_factor=0.0,
            read="(parse error — no JSON found)",
            key_risks=[],
            what_would_change_my_mind="",
            raw_response=text or "",
            parse_ok=False,
            parse_error="no json object",
        )

    decision = (obj.get("decision") or "").strip().upper()
    if decision not in _ALLOWED_ENTRY:
        return EntryDecision(
            decision="SKIP",
            conviction=0.0,
            size_factor=0.0,
            read=str(obj.get("read", ""))[:500],
            key_risks=[],
            what_would_change_my_mind="",
            raw_response=text,
            parse_ok=False,
            parse_error=f"bad decision: {decision!r}",
        )

    def _clamp(v: Any, lo: float, hi: float, default: float) -> float:
        try:
            f = float(v)
            if f != f:  # NaN
                return default
            return max(lo, min(hi, f))
        except (TypeError, ValueError):
            return default

    conviction = _clamp(obj.get("conviction"), 0.0, 1.0, 0.0)
    size_factor = _clamp(obj.get("size_factor"), 0.0, 1.0, 0.0)
    # SKIP must have size 0
    if decision == "SKIP":
        size_factor = 0.0

    read = str(obj.get("read", ""))[:1000]
    krs = obj.get("key_risks") or []
    if not isinstance(krs, list):
        krs = []
    key_risks = [str(k)[:200] for k in krs[:5]]
    wwcmm = str(obj.get("what_would_change_my_mind", ""))[:500]

    obs_anchor = str(obj.get("obs_anchor", ""))[:200]
    if packet is not None:
        anchor_valid, anchor_reason = _validate_obs_anchor(obs_anchor, packet)
    else:
        anchor_valid, anchor_reason = False, "no packet for validation"

    return EntryDecision(
        decision=decision,
        conviction=conviction,
        size_factor=size_factor,
        read=read,
        key_risks=key_risks,
        what_would_change_my_mind=wwcmm,
        obs_anchor=obs_anchor,
        obs_anchor_valid=anchor_valid,
        obs_anchor_reason=anchor_reason,
        raw_response=text,
        parse_ok=True,
    )


def parse_exit_response(text: str, position_count: int) -> ExitDecision:
    """Parse a Claude exit response. Defaults to HOLD on parse failure."""
    obj = _extract_json(text or "")
    if obj is None:
        return ExitDecision(
            decision="HOLD",
            sell_count=0,
            limit_price_cents=None,
            conviction=0.0,
            read="(parse error — no JSON found)",
            regret_check="",
            raw_response=text or "",
            parse_ok=False,
            parse_error="no json object",
        )

    decision = (obj.get("decision") or "").strip().upper()
    if decision not in _ALLOWED_EXIT:
        return ExitDecision(
            decision="HOLD",
            sell_count=0,
            limit_price_cents=None,
            conviction=0.0,
            read=str(obj.get("read", ""))[:500],
            regret_check="",
            raw_response=text,
            parse_ok=False,
            parse_error=f"bad decision: {decision!r}",
        )

    try:
        sell_count = int(obj.get("sell_count") or 0)
    except (TypeError, ValueError):
        sell_count = 0
    if sell_count < 0:
        sell_count = 0
    if sell_count > position_count:
        sell_count = position_count

    lpc = obj.get("limit_price_cents")
    try:
        limit_price_cents = int(lpc) if lpc is not None else None
    except (TypeError, ValueError):
        limit_price_cents = None
    if limit_price_cents is not None:
        if limit_price_cents < 1:
            limit_price_cents = 1
        if limit_price_cents > 99:
            limit_price_cents = 99

    # Coherence: HOLD => sell_count 0, limit None. SELL_ALL => sell_count == position_count.
    if decision == "HOLD":
        sell_count = 0
        limit_price_cents = None
    elif decision == "SELL_ALL":
        sell_count = position_count
        if limit_price_cents is None:
            return ExitDecision(
                decision="HOLD",
                sell_count=0,
                limit_price_cents=None,
                conviction=0.0,
                read=str(obj.get("read", ""))[:500],
                regret_check="",
                raw_response=text,
                parse_ok=False,
                parse_error="SELL_ALL missing limit_price_cents",
            )
    elif decision == "SELL_PARTIAL":
        if sell_count <= 0 or sell_count >= position_count:
            return ExitDecision(
                decision="HOLD",
                sell_count=0,
                limit_price_cents=None,
                conviction=0.0,
                read=str(obj.get("read", ""))[:500],
                regret_check="",
                raw_response=text,
                parse_ok=False,
                parse_error=f"SELL_PARTIAL count {sell_count} not in (0, {position_count})",
            )
        if limit_price_cents is None:
            return ExitDecision(
                decision="HOLD",
                sell_count=0,
                limit_price_cents=None,
                conviction=0.0,
                read=str(obj.get("read", ""))[:500],
                regret_check="",
                raw_response=text,
                parse_ok=False,
                parse_error="SELL_PARTIAL missing limit_price_cents",
            )

    try:
        conviction = float(obj.get("conviction") or 0.0)
        conviction = max(0.0, min(1.0, conviction))
    except (TypeError, ValueError):
        conviction = 0.0

    read = str(obj.get("read", ""))[:1000]
    regret = str(obj.get("regret_check", ""))[:500]

    return ExitDecision(
        decision=decision,
        sell_count=sell_count,
        limit_price_cents=limit_price_cents,
        conviction=conviction,
        read=read,
        regret_check=regret,
        raw_response=text,
        parse_ok=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Anthropic SDK call (with prompt caching)
# ─────────────────────────────────────────────────────────────────────────────
_anthropic_client = None


def _client():
    """Lazy-init the Anthropic client. Imports anthropic only when needed
    so tests / dry-runs without the SDK still work."""
    global _anthropic_client
    if _anthropic_client is None:
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "anthropic SDK not installed. `pip install anthropic`."
            ) from e
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY not set in env or .env")
        _anthropic_client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _anthropic_client


# Pricing per Anthropic Sonnet 4.6 (as of build date). Update if pricing changes.
# input: $3/Mtok, output: $15/Mtok, cached input: $0.30/Mtok (90% discount).
_PRICE_PER_INPUT_TOK = 3.0 / 1_000_000
_PRICE_PER_OUTPUT_TOK = 15.0 / 1_000_000
_PRICE_PER_CACHED_INPUT_TOK = 0.30 / 1_000_000


def _call_claude_sdk(
    system_prompt: str,
    user_message: str,
    *,
    model: str = None,
    max_tokens: int = None,
    temperature: float = None,
    timeout: float = None,
) -> tuple[str, dict]:
    """Call Anthropic API with prompt caching on system. Returns (text, usage)."""
    client = _client()
    t0 = time.time()
    resp = client.messages.create(
        model=model or config.CLAUDE_MODEL,
        max_tokens=max_tokens or config.CLAUDE_MAX_TOKENS_OUT,
        temperature=temperature if temperature is not None else config.CLAUDE_TEMPERATURE,
        system=[
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {"role": "user", "content": user_message},
        ],
        timeout=timeout or config.CLAUDE_TIMEOUT_SEC,
    )
    elapsed = time.time() - t0
    text_parts = []
    for blk in resp.content:
        if getattr(blk, "type", None) == "text":
            text_parts.append(blk.text)
    text = "\n".join(text_parts)

    usage = getattr(resp, "usage", None)
    input_tokens = getattr(usage, "input_tokens", 0) if usage else 0
    output_tokens = getattr(usage, "output_tokens", 0) if usage else 0
    cached_input = getattr(usage, "cache_read_input_tokens", 0) if usage else 0
    cost = (
        input_tokens * _PRICE_PER_INPUT_TOK
        + output_tokens * _PRICE_PER_OUTPUT_TOK
        + cached_input * _PRICE_PER_CACHED_INPUT_TOK
    )
    return text, {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input,
        "est_cost_usd": cost,
        "elapsed_sec": elapsed,
    }


def _call_claude_cli(
    system_prompt: str,
    user_message: str,
    *,
    timeout: float = None,
) -> tuple[str, dict]:
    """Call `claude -p` headless mode. Uses Claude Code subscription quota,
    not API credits. Returns (text, usage).

    Auth: passes CLAUDE_CODE_OAUTH_TOKEN to the subprocess via env. This is
    the long-lived OAuth token generated by `claude setup-token` (subscription
    tier). Do NOT set ANTHROPIC_API_KEY for the subprocess — the CLI would
    try to use that for Console API billing, which we don't want.
    """
    import json as _json
    import os
    import subprocess

    # 2026-05-17: Switched from concat-into-user to --system-prompt + JSON
    # output. Three wins:
    #   1. The 14.5K-token system prompt now sits in the cacheable slot, so
    #      cache_read_input_tokens dominates on warm calls (saw 10.8K cache
    #      reads on the first warm call vs 28K cache_creation on cold).
    #   2. --output-format json exposes per-call usage (input/output/cache
    #      tokens) and total_cost_usd. Pre-change, the CLI path had no
    #      telemetry — we logged 0s on every dispatch (silent-cost bug).
    #   3. stdin=DEVNULL skips the CLI's 3s "no stdin yet" wait warning.
    # Auth: still passes CLAUDE_CODE_OAUTH_TOKEN — subscription quota, not
    # API billing. Do NOT add --bare; that flag forces ANTHROPIC_API_KEY
    # auth and would switch billing to metered API.

    user_with_instr = (
        f"{user_message}\n\n"
        f"---\n\n"
        f"Reply with ONLY the JSON object specified in the schema. No prose "
        f"before or after. No code-fence."
    )

    sub_env = os.environ.copy()
    oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not oauth:
        raise RuntimeError(
            "CLAUDE_CODE_OAUTH_TOKEN missing — set in .env or env (config.apply_env "
            "should have loaded it)"
        )
    sub_env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth
    sub_env.pop("ANTHROPIC_API_KEY", None)

    cmd = [
        config.CLAUDE_CLI_PATH, "-p",
        "--no-session-persistence",
        "--output-format", "json",
        "--system-prompt", system_prompt,
        user_with_instr,
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=sub_env,
            timeout=timeout or config.CLAUDE_TIMEOUT_SEC,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude CLI timed out after {timeout or config.CLAUDE_TIMEOUT_SEC}s")
    elapsed = time.time() - t0
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:300]}"
        )

    # Parse the JSON envelope. Shape (per claude -p --output-format json):
    #   {"type":"result","result":"<assistant text>",
    #    "usage":{"input_tokens":N,"output_tokens":N,
    #             "cache_creation_input_tokens":N,"cache_read_input_tokens":N,...},
    #    "total_cost_usd":N, ...}
    # On parse failure, fall back to raw stdout so the existing JSON-extract
    # logic in parse_entry_response still has something to work with.
    raw = proc.stdout.strip()
    text = raw
    usage_in = usage_out = usage_cache = 0
    cost = 0.0
    try:
        env = _json.loads(raw)
        if isinstance(env, dict) and env.get("type") == "result":
            text = (env.get("result") or "").strip()
            u = env.get("usage") or {}
            usage_in = int(u.get("input_tokens") or 0)
            usage_out = int(u.get("output_tokens") or 0)
            # Treat cache_read as the "cheap input" bucket for est_cost
            # parity with the SDK path (which reports cache_read_input_tokens
            # as `cached_input_tokens`).
            usage_cache = int(u.get("cache_read_input_tokens") or 0)
            cost = float(env.get("total_cost_usd") or 0.0)
    except (ValueError, TypeError) as e:
        log.warning("claude CLI JSON envelope parse failed (%s) — using raw stdout", e)

    if config.CLAUDE_CLI_INTERCALL_SLEEP_SEC > 0:
        time.sleep(config.CLAUDE_CLI_INTERCALL_SLEEP_SEC)
    return text, {
        "input_tokens": usage_in,
        "output_tokens": usage_out,
        "cached_input_tokens": usage_cache,
        "est_cost_usd": cost,
        "elapsed_sec": elapsed,
    }


def _call_claude(system_prompt: str, user_message: str, **kw) -> tuple[str, dict]:
    """Dispatch to the configured backend."""
    backend = config.JUDGE_BACKEND
    if backend == "claude_cli":
        return _call_claude_cli(system_prompt, user_message,
                                timeout=kw.get("timeout"))
    elif backend == "anthropic_sdk":
        return _call_claude_sdk(system_prompt, user_message, **kw)
    raise RuntimeError(f"unknown JUDGE_BACKEND={backend!r}")


_last_cli_alert_ts: float = 0.0
_cli_errors_since_alert: int = 0
_CLI_ALERT_INTERVAL_SEC: float = 300.0  # 5 min


def _alert_cli_error(err: str) -> None:
    """Throttled Discord post when Claude CLI fails. Rolls up errors over
    a 5-min window so a quota outage doesn't flood the channel."""
    global _last_cli_alert_ts, _cli_errors_since_alert
    _cli_errors_since_alert += 1
    now = time.time()
    if now - _last_cli_alert_ts < _CLI_ALERT_INTERVAL_SEC:
        return
    _last_cli_alert_ts = now
    count = _cli_errors_since_alert
    _cli_errors_since_alert = 0
    if not (config.DISCORD_BOT_TOKEN and config.DISCORD_CHANNEL_ID):
        return
    try:
        import httpx
        url = f"https://discord.com/api/v10/channels/{config.DISCORD_CHANNEL_ID}/messages"
        msg = (
            f"⚠️ **paper_judge_bot** — Claude CLI error x{count} in last 5min\n"
            f"```\n{err[:1500]}\n```"
        )
        httpx.post(
            url,
            json={"content": msg[:2000]},
            headers={
                "Authorization": f"Bot {config.DISCORD_BOT_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=5.0,
        )
    except Exception:
        pass  # alerting must never crash the bot


def judge_entry(packet: dict[str, Any]) -> EntryDecision:
    """Public entry: build prompt, call Claude, parse. Returns EntryDecision
    with usage stats attached."""
    sys_p = entry_system_prompt()
    user = build_entry_user_message(packet)
    try:
        text, usage = _call_claude(sys_p, user)
    except Exception as e:
        log.warning("judge_entry API call failed: %s", e)
        _alert_cli_error(str(e))
        return EntryDecision(
            decision="SKIP",
            conviction=0.0,
            size_factor=0.0,
            read="(API error — defaulting to SKIP)",
            key_risks=[],
            what_would_change_my_mind="",
            raw_response="",
            parse_ok=False,
            parse_error=str(e),
        )
    d = parse_entry_response(text, packet=packet)
    d.elapsed_sec = usage["elapsed_sec"]
    d.input_tokens = usage["input_tokens"]
    d.output_tokens = usage["output_tokens"]
    d.cached_input_tokens = usage["cached_input_tokens"]
    d.est_cost_usd = usage["est_cost_usd"]
    return d


def judge_exit(packet: dict[str, Any], position_count: int) -> ExitDecision:
    sys_p = exit_system_prompt()
    user = build_exit_user_message(packet)
    try:
        text, usage = _call_claude(sys_p, user)
    except Exception as e:
        log.warning("judge_exit API call failed: %s", e)
        _alert_cli_error(str(e))
        return ExitDecision(
            decision="HOLD",
            sell_count=0,
            limit_price_cents=None,
            conviction=0.0,
            read="(API error — defaulting to HOLD)",
            regret_check="",
            raw_response="",
            parse_ok=False,
            parse_error=str(e),
        )
    d = parse_exit_response(text, position_count)
    d.elapsed_sec = usage["elapsed_sec"]
    d.input_tokens = usage["input_tokens"]
    d.output_tokens = usage["output_tokens"]
    d.cached_input_tokens = usage["cached_input_tokens"]
    d.est_cost_usd = usage["est_cost_usd"]
    return d
