"""persistence.py — actual vs forecast for the past 3 climate days.

Detects regime drift: "KPHX has been running 2.5°F hot vs NBM-blended
forecast for 5 days." When the recent forecast bias is persistent, we
should trust the live obs more than the forecast.

Reads from /home/ubuntu/obs-pipeline/data/obs.sqlite which has:
  - cli_reports table — actual NWS CLI HIGH/LOW per station-day
  - forecasts table — what models said for that station-day
  - running_max / running_min — final observed extremes

For HIGH: actual_high - forecast_at_t0 (forecast made the morning of)
For LOW: actual_low - forecast_at_t0
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import config

log = logging.getLogger("judge.persistence")


def get_3day_bias(station: str, kind: str = "high") -> Optional[dict]:
    """Return summary of actual vs blended-forecast for past 3 climate days.
    Returns {by_day: [{date, actual_f, forecast_f, bias_f}, ...], mean_bias_f}.

    Falls back gracefully if the obs DB is missing or schema differs.
    """
    if not Path(config.OBS_DB_PATH).exists():
        return None
    try:
        conn = sqlite3.connect(
            f"file:{config.OBS_DB_PATH}?mode=ro", uri=True, timeout=2.0
        )
        # cli_reports schema: station, climate_date, max_f, min_f, ...
        today = datetime.now(timezone.utc).date()
        results = []
        for d_offset in range(-3, 0):
            target = (today + timedelta(days=d_offset)).strftime("%Y-%m-%d")
            col = "high_f" if kind == "high" else "low_f"
            try:
                # Use latest CLI report (highest issued_time) per station+date.
                row = conn.execute(
                    f"""SELECT {col} FROM cli_reports
                        WHERE station=? AND climate_date=?
                        ORDER BY issued_time DESC LIMIT 1""",
                    (station, target),
                ).fetchone()
                actual = float(row[0]) if row and row[0] is not None else None
            except sqlite3.OperationalError:
                actual = None
            forecast = None
            if kind == "high":
                try:
                    # Earliest forecast issued for this date (closest to start of day)
                    frow = conn.execute(
                        """SELECT high_f FROM forecasts
                            WHERE station=? AND forecast_date=?
                            ORDER BY issued_time ASC LIMIT 1""",
                        (station, target),
                    ).fetchone()
                    forecast = float(frow[0]) if frow and frow[0] is not None else None
                except sqlite3.OperationalError:
                    forecast = None
            bias = None
            if actual is not None and forecast is not None:
                bias = round(actual - forecast, 2)
            results.append({
                "climate_date": target,
                "actual_f": actual,
                "forecast_f": forecast,
                "bias_f": bias,
            })
        conn.close()
        # Compute mean bias only if we have any forecast_f values
        biases = [r["bias_f"] for r in results if r["bias_f"] is not None]
        mean = round(sum(biases) / len(biases), 2) if biases else None
        return {"by_day": results, "mean_bias_f": mean}
    except Exception as e:
        log.warning("persistence query %s %s failed: %s", station, kind, e)
        return None
