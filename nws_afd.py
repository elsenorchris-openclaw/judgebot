"""nws_afd.py — Area Forecast Discussion text excerpt.

NWS Weather Forecast Offices issue a free-text AFD ~3-4 times per day
covering synoptic situation, short-term forecast reasoning, aviation
considerations. The DISCUSSION + SHORT TERM sections give Claude the
plain-English meteorological context the structured data can't.

Caches per-office for 1h since AFDs update at 0Z/4Z/12Z/20Z UTC roughly.

Endpoint pattern:
  1. /products/types/AFD/locations/{WFO}  → list of recent AFDs for an office
  2. /products/{id}                       → full product text
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

log = logging.getLogger("judge.afd")

NWS_BASE = "https://api.weather.gov"
NWS_UA = "paper_judge_bot (chris@example.com)"

_cache: dict[str, tuple[float, dict]] = {}  # office → (ts, {issued, excerpt})
_TTL = 3600.0


def _httpx():
    import httpx
    return httpx


def get_afd_excerpt(office: str, max_chars: int = 1200) -> Optional[dict]:
    """Return {issued_iso, full, discussion, short_term} for the most recent
    AFD from the given WFO (3-letter code like "LWX", "PHI", "PSR").
    Discussion/short_term keys hold trimmed sections; full is the raw text
    truncated to `max_chars`.
    """
    if not office:
        return None
    hit = _cache.get(office)
    if hit and (time.time() - hit[0]) < _TTL:
        return hit[1]
    try:
        httpx = _httpx()
        r = httpx.get(
            f"{NWS_BASE}/products/types/AFD/locations/{office}",
            headers={"User-Agent": NWS_UA, "Accept": "application/ld+json"},
            timeout=10.0,
        )
        r.raise_for_status()
        graph = (r.json() or {}).get("@graph") or []
        if not graph:
            return None
        # Most recent first
        latest = graph[0]
        prod_id = latest.get("@id") or latest.get("id")
        if not prod_id:
            return None
        # Fetch full text
        r2 = httpx.get(
            prod_id,
            headers={"User-Agent": NWS_UA, "Accept": "application/ld+json"},
            timeout=10.0,
        )
        r2.raise_for_status()
        prod = r2.json() or {}
        text = prod.get("productText") or ""
        issued = prod.get("issuanceTime") or ""
        # Parse section headers — AFDs have all-caps labels like
        # ".SYNOPSIS...", ".SHORT TERM /TODAY THROUGH TUESDAY/...", ".AVIATION..."
        sections = _split_sections(text)
        rec = {
            "issued_iso": issued,
            "office": office,
            "synopsis": _trim(sections.get("SYNOPSIS"), max_chars // 3),
            "short_term": _trim(sections.get("SHORT TERM"), max_chars // 2),
            "discussion": _trim(sections.get("DISCUSSION"), max_chars // 2),
            "aviation": _trim(sections.get("AVIATION"), max_chars // 4),
            "full_excerpt": _trim(text, max_chars),
        }
        _cache[office] = (time.time(), rec)
        return rec
    except Exception as e:
        log.warning("AFD fetch for %s failed: %s", office, e)
        return None


_SECTION_RX = re.compile(r"\.([A-Z][A-Z /,\-]+?)(?:\.{2,}|\s*\n)")


def _split_sections(text: str) -> dict[str, str]:
    """Split an AFD into labeled sections. Section header pattern is
    `.NAME...` or `.NAME /qualifier/...`. Returns dict of NAME → body."""
    out: dict[str, str] = {}
    if not text:
        return out
    # Find all section starts
    indices: list[tuple[str, int]] = []
    for m in _SECTION_RX.finditer(text):
        raw = m.group(1).strip()
        # Normalize "SHORT TERM /TODAY THROUGH..." → "SHORT TERM"
        key = re.split(r"\s+/", raw, 1)[0].strip()
        indices.append((key, m.end()))
    if not indices:
        return out
    for i, (key, start_idx) in enumerate(indices):
        end_idx = indices[i + 1][1] if i + 1 < len(indices) else len(text)
        body = text[start_idx:end_idx].strip()
        out[key] = body
    return out


def _trim(s: Optional[str], n: int) -> Optional[str]:
    if not s: return None
    s = s.strip()
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"
