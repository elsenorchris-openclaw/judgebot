"""market_universe.py — discover and filter Kalshi tickers we want to scan.

Each cycle of the entry loop calls `list_candidates()` which returns the
universe of (ticker, market_record, station, climate_day, bracket_kind, floor, cap)
tuples we'll consider. Pre-screen lives in the daemon, not here — this just
enumerates the candidate space.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import config
import kalshi_client


log = logging.getLogger("judge.universe")


# KXLOW... and KXHIGH... market tickers follow:
#   {SERIES_PREFIX}{CITY}-{YY}{MMM}{DD}-{B|T}{LEVEL}
# where SERIES_PREFIX is:
#   "KXLOWT" for daily-low series   (the trailing T is part of the series name)
#   "KXHIGH" for daily-high series
# Examples:
#   KXLOWTDC-26MAY13-B54.5      → low,  KDCA, 2026-05-13, B [54, 55)
#   KXLOWTAUS-26MAY13-T59       → low,  KAUS, 2026-05-13, T cold-tail (≤ 58.5)
#   KXHIGHATL-26MAY07-B59.5     → high, KATL, 2026-05-07, B [59, 60)
#   KXHIGHLAX-26MAY13-T68       → high, KLAX, 2026-05-13, T (warm-tail, ≥ 68.5)
_TICKER_RX = re.compile(
    r"^(KXLOWT|KXHIGH)([A-Z]{2,5})-(\d{2})([A-Z]{3})(\d{2})-(B|T)([\d.]+)$"
)
_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


@dataclass
class Candidate:
    ticker: str
    series_prefix: str   # "KXLOW" or "KXHIGH"
    city_code: str       # e.g., "DC", "ATL"
    station: str         # mapped, e.g., KDCA, KATL
    climate_day: str     # YYYY-MM-DD
    bracket_kind: str    # "B" or "T"
    floor: Optional[float]
    cap: Optional[float]
    bracket_label: float
    market: dict         # raw Kalshi market record


# City-code → station mapping (matches the other bots' tables). Could be
# pushed into config but kept here for locality.
CITY_TO_STATION = {
    "ATL": "KATL", "AUS": "KAUS", "BOS": "KBOS", "DC":  "KDCA",
    "DEN": "KDEN", "DAL": "KDFW", "HOU": "KHOU", "LV":  "KLAS",
    "LAX": "KLAX", "CHI": "KMDW", "MIA": "KMIA", "MIN": "KMSP",
    "MSY": "KMSY", "NYC": "KNYC", "OKC": "KOKC", "PHIL":"KPHL",
    "PHX": "KPHX", "SAT": "KSAT", "SEA": "KSEA", "SFO": "KSFO",
    # alternates / both ways
    "NY":  "KNYC", "WASH":"KDCA", "TLV":"KLAS",
    # 2026-05-15: Kalshi uses a SECOND HIGH-temp series naming convention
    # — "KXHIGHT{CITY}" (note extra "T") — for 12 of 20 stations. The bot
    # iterates `for city in CITY_TO_STATION` × prefixes, so adding these
    # aliases makes "KXHIGH" + "TATL" → "KXHIGHTATL" hit Kalshi's series
    # for Atlanta. Confirmed via /trade-api/v2/series listing: only 8
    # cities (AUS/CHI/DEN/HOU/LAX/MIA/NY/PHIL) use the original
    # "KXHIGH{CITY}" form. Without these aliases the bot was silently
    # missing HIGH coverage on 12/20 cities — we never even SAW their
    # candidates. (parse_ticker captures "TATL" as city_code via the
    # KXLOWT|KXHIGH regex alternation; CITY_TO_STATION resolves it here.)
    "TATL":  "KATL",   # KXHIGHTATL = Atlanta Max Temperature
    "TBOS":  "KBOS",   # KXHIGHTBOS = Boston Maximum Daily Temperature
    "TDAL":  "KDFW",   # KXHIGHTDAL = Dallas Maximum Temperature
    "TDC":   "KDCA",   # KXHIGHTDC  = Washington DC Daily Max Temp
    "TMIN":  "KMSP",   # KXHIGHTMIN = Minneapolis Daily High Temperature
    "TNOLA": "KMSY",   # KXHIGHTNOLA = New Orleans Max temp Daily (note: NOLA != MSY suffix)
    "TOKC":  "KOKC",   # KXHIGHTOKC = Oklahoma City Maximum High Temperature
    "TPHX":  "KPHX",   # KXHIGHTPHX = Phoenix High Temperature Daily
    "TSATX": "KSAT",   # KXHIGHTSATX = San Antonio Daily Maximum Temperature (note: SATX != SAT)
    "TSEA":  "KSEA",   # KXHIGHTSEA = Seattle Maximum Temperature Daily
    "TSFO":  "KSFO",   # KXHIGHTSFO = San Francisco High Temperature Daily
    # TLV already mapped above to KLAS — covers KXHIGHTLV.
    # Houston has BOTH KXHIGHHOU (orig prefix, AUS alias group) and
    # KXHIGHTHOU (extended prefix) — the iteration finds both via
    # existing "HOU" alias and the new "THOU" alias below:
    "THOU":  "KHOU",   # KXHIGHTHOU = Daily High Temperature Houston
}


def parse_ticker(ticker: str) -> Optional[Candidate]:
    """Decompose a Kalshi weather ticker. Returns None for non-weather or
    unrecognized formats."""
    m = _TICKER_RX.match(ticker)
    if not m:
        return None
    series_prefix, city, yy, mon, dd, kind, level_s = m.groups()
    # Normalize to LOW/HIGH for downstream code.
    series_kind = "KXLOW" if series_prefix == "KXLOWT" else "KXHIGH"
    mnum = _MONTH_MAP.get(mon)
    if not mnum:
        return None
    try:
        year = 2000 + int(yy)
        day = int(dd)
        climate_day = f"{year}-{mnum:02d}-{day:02d}"
    except ValueError:
        return None
    try:
        level = float(level_s)
    except ValueError:
        return None
    # Bracket math (both series, all shapes — stored as raw CLI integers):
    #   B-bracket ("B45.5"): floor=N, cap=N+1, BOTH inclusive in YES window
    #                        (true-temp YES = [floor-0.5, cap+0.5), 2°F wide).
    #   T-bracket ("T59"):   one-sided; can be either tail:
    #     warm tail ("greater"): floor=N, cap=None, YES if CLI > N
    #                            (true-temp YES = T ≥ N+0.5).
    #     cold tail ("less"):    cap=N, floor=None, YES if CLI < N
    #                            (true-temp YES = T < N-0.5).
    #   IMPORTANT: T-bracket direction is NOT determined by series prefix —
    #   both KXLOW and KXHIGH carry both warm-tail and cold-tail T-brackets
    #   (e.g., KXLOWTMIN-T59 = "LOW > 59" warm, KXHIGHCHI-T71 = "HIGH < 71"
    #   cold). The parser cannot tell from the ticker alone, so it returns
    #   floor=None, cap=None for T-brackets and the caller (list_candidates)
    #   patches the correct field from the Kalshi market record's
    #   strike_type / floor_strike / cap_strike.
    floor = None
    cap = None
    if kind == "B":
        # Label like 45.5 → integer floor=45, cap=46 (both YES-inclusive CLI).
        floor = float(int(level))
        cap = floor + 1.0
    # T-bracket: leave floor=None, cap=None — patched by list_candidates.

    station = CITY_TO_STATION.get(city)
    if not station:
        log.debug("unknown city code %s in ticker %s", city, ticker)
        return None

    return Candidate(
        ticker=ticker,
        series_prefix=series_kind,
        city_code=city,
        station=station,
        climate_day=climate_day,
        bracket_kind=kind,
        floor=floor,
        cap=cap,
        bracket_label=level,
        market={},
    )


def list_candidates(
    now_utc: float | None = None,
) -> list[Candidate]:
    """Enumerate the candidate universe by hitting Kalshi /markets for each
    series prefix matching config.STATIONS × config.SERIES_PREFIXES.

    De-duplicates by ticker — Kalshi sometimes returns the same market under
    multiple city-alias series queries (e.g., NYC and NY both map to KNYC)
    and pagination occasionally overlaps.
    """
    out: list[Candidate] = []
    seen_tickers: set[str] = set()
    # 2026-05-16 cleanup: previously read `now_utc or 0 or 0 + 0` which
    # collapsed to `now_utc or 0` then went through the else branch when 0,
    # making it functionally equivalent to the explicit ternary below.
    today = (datetime.fromtimestamp(now_utc, tz=timezone.utc).date()
             if now_utc else datetime.now(timezone.utc).date())
    valid_days = {
        (today + __import__("datetime").timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in config.DAYS_OUT_RANGE
    }

    for prefix in config.SERIES_PREFIXES:
        for city in CITY_TO_STATION:
            station = CITY_TO_STATION[city]
            if station not in config.STATIONS:
                continue
            series = f"{prefix}{city}"
            try:
                markets = kalshi_client.list_open_markets(series, limit=200)
            except Exception as e:
                log.warning("list_open_markets %s failed: %s", series, e)
                continue
            for m in markets:
                tkr = m.get("ticker")
                if not tkr or tkr in seen_tickers:
                    continue
                cand = parse_ticker(tkr)
                if not cand:
                    continue
                if cand.climate_day not in valid_days:
                    continue
                cand.market = m
                # Patch bracket boundaries from Kalshi's authoritative strike
                # fields. For T-brackets, exactly ONE of floor/cap is set —
                # never both (which would mis-trigger the B-bracket branch in
                # downstream edge math). strike_type tells us the direction.
                fc = m.get("floor_strike")
                cc = m.get("cap_strike")
                st = (m.get("strike_type") or "").lower()
                try: fc_f = float(fc) if fc is not None else None
                except (TypeError, ValueError): fc_f = None
                try: cc_f = float(cc) if cc is not None else None
                except (TypeError, ValueError): cc_f = None
                if cand.bracket_kind == "B":
                    # B-bracket: both floor_strike AND cap_strike are set
                    # (Kalshi title reads e.g. "45° to 46°", both inclusive).
                    cand.floor = fc_f if fc_f is not None else cand.floor
                    cand.cap = cc_f if cc_f is not None else cand.cap
                else:
                    # T-bracket: trust strike_type to set exactly one side.
                    if st == "greater":
                        # YES if CLI > floor_strike (warm tail).
                        cand.floor = fc_f
                        cand.cap = None
                    elif st == "less":
                        # YES if CLI < cap_strike (cold tail).
                        cand.floor = None
                        cand.cap = cc_f
                    else:
                        # Fallback: trust whichever field Kalshi populated.
                        if fc_f is not None and cc_f is None:
                            cand.floor = fc_f; cand.cap = None
                        elif cc_f is not None and fc_f is None:
                            cand.floor = None; cand.cap = cc_f
                        else:
                            # Both or neither — log + skip (don't trust).
                            log.warning("T-bracket %s: strike_type=%r fc=%s cc=%s — skipping",
                                        tkr, st, fc, cc)
                            continue
                seen_tickers.add(tkr)
                out.append(cand)
    return out
