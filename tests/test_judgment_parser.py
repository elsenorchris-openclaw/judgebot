"""Parser tests — these run with no network access.

The parser is the choke point between Claude (LLM, unpredictable) and the
order-execution path (deterministic). If the parser passes a bad object
through, guardrails still catch it — but ideally we never get there.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from judgment import parse_entry_response, parse_exit_response


# ─────────────────────────────────────────────────────────────────────────────
# Entry parser
# ─────────────────────────────────────────────────────────────────────────────
class TestEntryParser:
    def test_clean_buy_no(self):
        text = '''```json
{
  "decision": "BUY_NO",
  "conviction": 0.75,
  "size_factor": 0.6,
  "read": "Forecast and obs both agree.",
  "key_risks": ["wet-bulb cool"],
  "what_would_change_my_mind": "temp drops"
}
```'''
        d = parse_entry_response(text)
        assert d.parse_ok
        assert d.decision == "BUY_NO"
        assert d.conviction == 0.75
        assert d.size_factor == 0.6
        assert d.read.startswith("Forecast")
        assert d.key_risks == ["wet-bulb cool"]

    def test_bare_json_no_fence(self):
        text = '{"decision":"SKIP","conviction":0.5,"size_factor":0.0,"read":"too noisy","key_risks":[],"what_would_change_my_mind":""}'
        d = parse_entry_response(text)
        assert d.parse_ok and d.decision == "SKIP"

    def test_text_around_json(self):
        text = """Sure, here's my decision:

```json
{"decision":"BUY_YES","conviction":0.8,"size_factor":0.5,"read":"ok","key_risks":[],"what_would_change_my_mind":""}
```

Let me know if you have questions."""
        d = parse_entry_response(text)
        assert d.parse_ok and d.decision == "BUY_YES"

    def test_no_json_at_all(self):
        text = "I don't think I have enough information to make a decision."
        d = parse_entry_response(text)
        assert not d.parse_ok
        assert d.decision == "SKIP"
        assert d.parse_error == "no json object"

    def test_invalid_decision_value(self):
        text = '{"decision":"YOLO_BUY","conviction":0.5,"size_factor":1.0,"read":"a","key_risks":[],"what_would_change_my_mind":""}'
        d = parse_entry_response(text)
        assert not d.parse_ok
        assert d.decision == "SKIP"
        assert "bad decision" in d.parse_error

    def test_skip_forces_size_zero(self):
        """Even if Claude returns size_factor=0.8 on a SKIP, we force 0."""
        text = '{"decision":"SKIP","conviction":0.5,"size_factor":0.8,"read":"","key_risks":[],"what_would_change_my_mind":""}'
        d = parse_entry_response(text)
        assert d.decision == "SKIP" and d.size_factor == 0.0

    def test_conviction_clamped(self):
        text = '{"decision":"BUY_NO","conviction":1.5,"size_factor":0.5,"read":"","key_risks":[],"what_would_change_my_mind":""}'
        d = parse_entry_response(text)
        assert d.conviction == 1.0

    def test_size_factor_clamped(self):
        text = '{"decision":"BUY_NO","conviction":0.5,"size_factor":-0.5,"read":"","key_risks":[],"what_would_change_my_mind":""}'
        d = parse_entry_response(text)
        assert d.size_factor == 0.0

    def test_missing_optional_fields(self):
        text = '{"decision":"SKIP","conviction":0.5,"size_factor":0.0}'
        d = parse_entry_response(text)
        assert d.parse_ok and d.decision == "SKIP"
        assert d.read == ""
        assert d.key_risks == []

    def test_non_list_key_risks(self):
        text = '{"decision":"SKIP","conviction":0.5,"size_factor":0.0,"read":"","key_risks":"not a list","what_would_change_my_mind":""}'
        d = parse_entry_response(text)
        assert d.key_risks == []

    def test_truncates_long_strings(self):
        long_read = "x" * 5000
        text = f'{{"decision":"SKIP","conviction":0.5,"size_factor":0.0,"read":"{long_read}","key_risks":[],"what_would_change_my_mind":""}}'
        d = parse_entry_response(text)
        assert len(d.read) <= 1000

    def test_lowercase_decision_normalized(self):
        text = '{"decision":"buy_no","conviction":0.5,"size_factor":0.5,"read":"","key_risks":[],"what_would_change_my_mind":""}'
        d = parse_entry_response(text)
        assert d.decision == "BUY_NO" and d.parse_ok

    def test_garbage_input(self):
        for bad in ["", None, "{", "{}", "}{", "{not json}"]:
            d = parse_entry_response(bad)
            assert d.decision == "SKIP"

    def test_nan_conviction_defaults_safe(self):
        """nan in JSON would be invalid, but if it gets in it should not crash."""
        text = '{"decision":"BUY_NO","conviction":null,"size_factor":null,"read":"","key_risks":[],"what_would_change_my_mind":""}'
        d = parse_entry_response(text)
        assert d.conviction == 0.0 and d.size_factor == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Exit parser
# ─────────────────────────────────────────────────────────────────────────────
class TestExitParser:
    def test_clean_hold(self):
        text = '{"decision":"HOLD","sell_count":0,"limit_price_cents":null,"conviction":0.6,"read":"hold to settle","regret_check":"if obs reverses"}'
        d = parse_exit_response(text, position_count=100)
        assert d.parse_ok and d.decision == "HOLD"
        assert d.sell_count == 0
        assert d.limit_price_cents is None

    def test_sell_all(self):
        text = '{"decision":"SELL_ALL","sell_count":100,"limit_price_cents":15,"conviction":0.9,"read":"bracket lost","regret_check":""}'
        d = parse_exit_response(text, position_count=100)
        assert d.parse_ok and d.decision == "SELL_ALL"
        assert d.sell_count == 100
        assert d.limit_price_cents == 15

    def test_sell_all_count_corrected_to_position_size(self):
        """If Claude says sell_count=50 with SELL_ALL on 100-position, force 100."""
        text = '{"decision":"SELL_ALL","sell_count":50,"limit_price_cents":20,"conviction":0.7,"read":"","regret_check":""}'
        d = parse_exit_response(text, position_count=100)
        assert d.decision == "SELL_ALL" and d.sell_count == 100

    def test_sell_all_missing_price_falls_to_hold(self):
        text = '{"decision":"SELL_ALL","sell_count":100,"limit_price_cents":null,"conviction":0.7,"read":"","regret_check":""}'
        d = parse_exit_response(text, position_count=100)
        assert not d.parse_ok and d.decision == "HOLD"
        assert "missing limit" in d.parse_error

    def test_sell_partial_valid(self):
        text = '{"decision":"SELL_PARTIAL","sell_count":40,"limit_price_cents":25,"conviction":0.55,"read":"","regret_check":""}'
        d = parse_exit_response(text, position_count=100)
        assert d.parse_ok and d.decision == "SELL_PARTIAL" and d.sell_count == 40

    def test_sell_partial_zero_falls_to_hold(self):
        text = '{"decision":"SELL_PARTIAL","sell_count":0,"limit_price_cents":25,"conviction":0.55,"read":"","regret_check":""}'
        d = parse_exit_response(text, position_count=100)
        assert not d.parse_ok and d.decision == "HOLD"

    def test_sell_partial_full_size_falls_to_hold(self):
        """SELL_PARTIAL with sell_count == position_count is incoherent."""
        text = '{"decision":"SELL_PARTIAL","sell_count":100,"limit_price_cents":25,"conviction":0.55,"read":"","regret_check":""}'
        d = parse_exit_response(text, position_count=100)
        assert not d.parse_ok and d.decision == "HOLD"

    def test_sell_partial_overflow_clamped(self):
        text = '{"decision":"SELL_PARTIAL","sell_count":150,"limit_price_cents":25,"conviction":0.55,"read":"","regret_check":""}'
        d = parse_exit_response(text, position_count=100)
        # Clamped to 100; then SELL_PARTIAL with full size → HOLD
        assert d.decision == "HOLD"

    def test_limit_price_clamped(self):
        text = '{"decision":"SELL_ALL","sell_count":100,"limit_price_cents":150,"conviction":0.9,"read":"","regret_check":""}'
        d = parse_exit_response(text, position_count=100)
        assert d.limit_price_cents == 99

    def test_negative_sell_count_zeroed(self):
        text = '{"decision":"HOLD","sell_count":-10,"limit_price_cents":null,"conviction":0.5,"read":"","regret_check":""}'
        d = parse_exit_response(text, position_count=100)
        assert d.sell_count == 0

    def test_hold_forces_clean_state(self):
        """HOLD should always end with sell_count=0, limit=None even if Claude
        provided dirty values."""
        text = '{"decision":"HOLD","sell_count":50,"limit_price_cents":40,"conviction":0.6,"read":"","regret_check":""}'
        d = parse_exit_response(text, position_count=100)
        assert d.decision == "HOLD"
        assert d.sell_count == 0
        assert d.limit_price_cents is None

    def test_invalid_decision_falls_to_hold(self):
        text = '{"decision":"PANIC","sell_count":50,"limit_price_cents":25,"conviction":0.9,"read":"","regret_check":""}'
        d = parse_exit_response(text, position_count=100)
        assert not d.parse_ok and d.decision == "HOLD"


# ─────────────────────────────────────────────────────────────────────────────
# Probability extraction (paper_judge_bot._extract_prob_from_read)
# ─────────────────────────────────────────────────────────────────────────────
class TestExtractProb:
    """Regression tests for the prob-from-read regex.

    Bug seen 2026-05-16 (TLV-B95.5): the scan extended past the answer
    sentence and picked up `r²=0.52` from the next clause, returning 0.52
    instead of P(NO)=0.944. That false-rejected a +13pp BUY_NO at submit.
    The fix bounds the scan at sentence punctuation `. ` / `; ` / newline.
    """

    @staticmethod
    def _extract(text, side):
        # Lazy import — paper_judge_bot pulls a lot at import time.
        import paper_judge_bot
        return paper_judge_bot._extract_prob_from_read(text, prefer_side=side)

    def test_tlv_b955_r2_does_not_leak(self):
        """The exact text from the 2026-05-16 TLV bug must extract 0.944,
        not 0.52 (which was the r² of a 60m regression in the next clause)."""
        read = ("B95.5 … σ=0.96°F; P(NO)≈0.944 vs market 81c = +13pp gap. "
                "60m regression +2.22°F/h r²=0.52 (range_f=1.8°F, soft).")
        side, p = self._extract(read, "BUY_NO")
        assert side == "BUY_NO"
        assert p == 0.944

    def test_formula_then_answer_preserved(self):
        """Pre-existing behavior — formula constants like (88.5−91.0)/2.24
        must not steal the answer (≈ 0.87)."""
        read = "P(NO) = 1 − Φ((88.5−91.0)/2.24) ≈ 0.87"
        side, p = self._extract(read, "BUY_NO")
        assert side == "BUY_NO"
        assert p == 0.87

    def test_percent_form(self):
        read = "P(NO) ~65% vs 20c no_ask → ~45pp gap; sized down per rule."
        side, p = self._extract(read, "BUY_NO")
        assert p == 0.65

    def test_colon_separator_does_not_break(self):
        """TPHX-style: 'At μ=97.0: P(NO)=0.651, gap=−3.9pp'."""
        read = ("At μ=97.0: P(NO)=0.651, gap=−3.9pp vs market 69c NO. "
                "At HRRR μ=97.52: gap=+3.5pp — below 8pp threshold.")
        side, p = self._extract(read, "BUY_NO")
        assert p == 0.651

    def test_multi_side_semicolon(self):
        read = "P(NO)=0.85; P(YES)=0.15"
        # BUY_NO side
        side, p = self._extract(read, "BUY_NO")
        assert p == 0.85
        # BUY_YES side
        side, p = self._extract(read, "BUY_YES")
        assert p == 0.15

    def test_newline_separated_clause_does_not_leak(self):
        read = "P(NO)=0.85\nThen the r² was 0.42"
        side, p = self._extract(read, "BUY_NO")
        assert p == 0.85
