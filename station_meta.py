"""station_meta.py — per-station coordinates + Kalshi settlement source.

Each entry maps the ICAO station ID to:
  - (lat, lon) decimal degrees (the airport / observing site)
  - elevation_ft
  - cli_report — the NWS CLI report identifier Kalshi cites for settlement
                 (e.g., CLIDCA → KDCA Daily Climate Report)
  - climate_class — used for diurnal peak-lag math:
      "coastal" → peak lags solar noon by ~1.5h
      "continental" → ~2.0h lag
      "desert" → ~3.0h lag
      "marine_west" → ~1.5h lag (cool marine layer dominates)

Coords are airport ASOS sites (matches NWS daily climate report station).
References: NOAA station metadata + airport AOPA records.
"""
from __future__ import annotations
from typing import Optional


STATION_META: dict[str, dict] = {
    "KATL": {"lat": 33.6367, "lon": -84.4281, "elev_ft": 1026,
             "cli_report": "CLIATL", "climate_class": "continental",
             "label": "Atlanta GA"},
    "KAUS": {"lat": 30.1944, "lon": -97.6700, "elev_ft":  542,
             "cli_report": "CLIAUS", "climate_class": "continental",
             "label": "Austin TX"},
    "KBOS": {"lat": 42.3656, "lon": -71.0096, "elev_ft":   20,
             "cli_report": "CLIBOS", "climate_class": "coastal",
             "label": "Boston MA"},
    "KDCA": {"lat": 38.8521, "lon": -77.0377, "elev_ft":   15,
             "cli_report": "CLIDCA", "climate_class": "continental",
             "label": "Washington DC"},
    "KDEN": {"lat": 39.8617, "lon": -104.6731, "elev_ft": 5431,
             "cli_report": "CLIDEN", "climate_class": "continental",
             "label": "Denver CO"},
    "KDFW": {"lat": 32.8968, "lon": -97.0380, "elev_ft":  607,
             "cli_report": "CLIDFW", "climate_class": "continental",
             "label": "Dallas/Fort Worth TX"},
    "KHOU": {"lat": 29.6454, "lon": -95.2789, "elev_ft":   46,
             "cli_report": "CLIHOU", "climate_class": "coastal",
             "label": "Houston Hobby TX"},
    "KLAS": {"lat": 36.0840, "lon": -115.1537, "elev_ft": 2181,
             "cli_report": "CLILAS", "climate_class": "desert",
             "label": "Las Vegas NV"},
    "KLAX": {"lat": 33.9425, "lon": -118.4081, "elev_ft":  125,
             "cli_report": "CLILAX", "climate_class": "marine_west",
             "label": "Los Angeles CA"},
    "KMDW": {"lat": 41.7860, "lon": -87.7524, "elev_ft":  620,
             "cli_report": "CLIMDW", "climate_class": "continental",
             "label": "Chicago Midway IL"},
    "KMIA": {"lat": 25.7959, "lon": -80.2870, "elev_ft":   11,
             "cli_report": "CLIMIA", "climate_class": "coastal",
             "label": "Miami FL"},
    "KMSP": {"lat": 44.8848, "lon": -93.2223, "elev_ft":  841,
             "cli_report": "CLIMSP", "climate_class": "continental",
             "label": "Minneapolis-St Paul MN"},
    "KMSY": {"lat": 29.9934, "lon": -90.2580, "elev_ft":    4,
             "cli_report": "CLIMSY", "climate_class": "coastal",
             "label": "New Orleans LA"},
    "KNYC": {"lat": 40.7794, "lon": -73.9692, "elev_ft":  154,
             "cli_report": "CLINYC", "climate_class": "coastal",
             "label": "New York Central Park"},
    "KOKC": {"lat": 35.3931, "lon": -97.6007, "elev_ft": 1295,
             "cli_report": "CLIOKC", "climate_class": "continental",
             "label": "Oklahoma City OK"},
    "KPHL": {"lat": 39.8729, "lon": -75.2437, "elev_ft":   36,
             "cli_report": "CLIPHL", "climate_class": "coastal",
             "label": "Philadelphia PA"},
    "KPHX": {"lat": 33.4373, "lon": -112.0078, "elev_ft": 1135,
             "cli_report": "CLIPHX", "climate_class": "desert",
             "label": "Phoenix Sky Harbor AZ"},
    "KSAT": {"lat": 29.5337, "lon": -98.4698, "elev_ft":  809,
             "cli_report": "CLISAT", "climate_class": "continental",
             "label": "San Antonio TX"},
    "KSEA": {"lat": 47.4502, "lon": -122.3088, "elev_ft":  433,
             "cli_report": "CLISEA", "climate_class": "marine_west",
             "label": "Seattle-Tacoma WA"},
    "KSFO": {"lat": 37.6189, "lon": -122.3750, "elev_ft":   13,
             "cli_report": "CLISFO", "climate_class": "marine_west",
             "label": "San Francisco CA"},
}


# Hours the daily peak temperature LAGS solar noon, by climate class.
# Calibrated from typical 30-yr NCEI hourly climate normals.
_PEAK_LAG_H_BY_CLASS = {
    "coastal":     1.5,
    "continental": 2.5,
    "desert":      3.0,
    "marine_west": 1.5,
}


def get(station: str) -> Optional[dict]:
    return STATION_META.get(station)


def peak_lag_h(station: str) -> float:
    rec = STATION_META.get(station) or {}
    cls = rec.get("climate_class", "continental")
    return _PEAK_LAG_H_BY_CLASS.get(cls, 2.5)
