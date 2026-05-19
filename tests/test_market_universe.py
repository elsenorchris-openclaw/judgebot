"""Tests for ticker parsing — pure functions, no network."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from market_universe import parse_ticker


class TestTickerParser:
    def test_low_b_bracket(self):
        c = parse_ticker("KXLOWTDC-26MAY13-B54.5")
        assert c is not None
        assert c.series_prefix == "KXLOW"
        assert c.city_code == "DC"
        assert c.station == "KDCA"
        assert c.climate_day == "2026-05-13"
        assert c.bracket_kind == "B"
        assert c.floor == 54.0 and c.cap == 55.0

    def test_low_t_unpatched(self):
        # 2026-05-15: parser no longer heuristic-fills T-bracket floor/cap.
        # Tail direction (warm vs cold) is ambiguous from the ticker alone —
        # both KXLOW and KXHIGH carry both shapes. list_candidates patches
        # the correct field from the live Kalshi market record's strike_type.
        c = parse_ticker("KXLOWTAUS-26MAY13-T59")
        assert c is not None
        assert c.bracket_kind == "T"
        assert c.floor is None
        assert c.cap is None
        assert c.bracket_label == 59.0

    def test_high_b_bracket(self):
        c = parse_ticker("KXHIGHNYC-26MAY13-B72.5")
        assert c is not None
        assert c.series_prefix == "KXHIGH"
        assert c.station == "KNYC"
        assert c.bracket_kind == "B"
        assert c.floor == 72.0 and c.cap == 73.0

    def test_high_t_unpatched(self):
        # See test_low_t_unpatched — T-bracket direction not set by parser.
        c = parse_ticker("KXHIGHATL-26MAY13-T80")
        assert c is not None
        assert c.bracket_kind == "T"
        assert c.floor is None
        assert c.cap is None
        assert c.bracket_label == 80.0

    def test_unknown_city(self):
        c = parse_ticker("KXLOWTXYZ-26MAY13-B54.5")
        assert c is None

    def test_bad_month(self):
        c = parse_ticker("KXLOWTDC-26ZZZ13-B54.5")
        assert c is None

    def test_non_weather_ticker(self):
        assert parse_ticker("INXD-26DEC31-T5000") is None
        assert parse_ticker("garbage") is None
        assert parse_ticker("") is None

    def test_alternate_city_codes(self):
        # NY → KNYC, WASH → KDCA, TLV → KLAS  (aliases)
        for tkr, station in [
            ("KXLOWTNY-26MAY13-B40.5", "KNYC"),
            ("KXLOWTTLV-26MAY13-B74.5", "KLAS"),
        ]:
            c = parse_ticker(tkr)
            assert c is not None and c.station == station

    def test_multi_digit_levels(self):
        c = parse_ticker("KXHIGHPHX-26MAY13-B109.5")
        assert c is not None and c.floor == 109.0 and c.cap == 110.0
