"""
climate_data.py

Fetches historical climate data from Open-Meteo (free, no API key, ERA5
reanalysis back to 1940) and computes summaries relevant to farm design.

Two categories of output, deliberately kept separate:

1. DESIGN-RELEVANT: prevailing wind direction and rainfall intensity.
   These directly change design decisions elsewhere in the Scale of
   Permanence (windbreak orientation, pond/swale sizing) and get fed into
   report_generator.py's reasoning.

2. CONTEXTUAL: temperature ranges, frost-adjacent info. Useful reference
   for the report, but not something this tool reasons hard about yet —
   that's really the domain of a future crop/species-selection module.

Docs: https://open-meteo.com/en/docs/historical-weather-api
"""

import math
import requests
from datetime import date
from typing import Optional

ARCHIVE_ENDPOINT = "https://archive-api.open-meteo.com/v1/archive"

COMPASS_DIRECTIONS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def _degrees_to_compass(degrees: float) -> str:
    """Converts a wind direction in degrees to a 16-point compass label."""
    index = round(degrees / 22.5) % 16
    return COMPASS_DIRECTIONS[index]


def _circular_mean_wind_direction(directions: list[float], weights: list[float]) -> float:
    """
    Computes a weighted circular mean of wind directions in degrees.

    Wind direction can't be averaged like normal numbers — 350 degrees and
    10 degrees are only 20 degrees apart, but a naive average gives 180
    (the exact opposite direction). This averages the sin/cos components
    instead, which handles the wraparound correctly.

    Weighted by wind speed on each day, since a handful of strong-wind days
    matter more for windbreak design than many calm days from a random
    direction.
    """
    sin_sum = sum(math.sin(math.radians(d)) * w for d, w in zip(directions, weights))
    cos_sum = sum(math.cos(math.radians(d)) * w for d, w in zip(directions, weights))

    mean_radians = math.atan2(sin_sum, cos_sum)
    mean_degrees = math.degrees(mean_radians)

    return mean_degrees % 360


def get_climate_summary_for_point(
    latitude: float, longitude: float, years: int = 10
) -> dict:
    """
    Fetches the last `years` complete calendar years of daily climate data
    for a point and returns a summary dict:

        {
            'prevailing_wind_direction': '' (compass label),
            'prevailing_wind_direction_degrees': float,
            'avg_annual_precipitation_mm': float,
            'max_daily_precipitation_mm': float,  # storm intensity proxy
            'avg_high_temp_c': float,
            'avg_low_temp_c': float,
            'record_high_temp_c': float,
            'record_low_temp_c': float,
            'years_analyzed': int,
        }
    """
    current_year = date.today().year
    end_year = current_year - 1  # last complete calendar year
    start_year = end_year - years + 1

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "start_date": f"{start_year}-01-01",
        "end_date": f"{end_year}-12-31",
        "daily": (
            "temperature_2m_max,temperature_2m_min,precipitation_sum,"
            "wind_speed_10m_max,wind_direction_10m_dominant"
        ),
        "timezone": "auto",
    }

    response = requests.get(ARCHIVE_ENDPOINT, params=params, timeout=60)
    response.raise_for_status()

    data = response.json()
    daily = data["daily"]

    temps_max = daily["temperature_2m_max"]
    temps_min = daily["temperature_2m_min"]
    precip = daily["precipitation_sum"]
    wind_speeds = daily["wind_speed_10m_max"]
    wind_directions = daily["wind_direction_10m_dominant"]

    # Filter out any None values (occasional data gaps) before aggregating
    valid_temps_max = [t for t in temps_max if t is not None]
    valid_temps_min = [t for t in temps_min if t is not None]
    valid_precip = [p for p in precip if p is not None]

    wind_pairs = [
        (d, s) for d, s in zip(wind_directions, wind_speeds) if d is not None and s is not None
    ]
    valid_directions = [d for d, s in wind_pairs]
    valid_speeds = [s for d, s in wind_pairs]

    num_years = end_year - start_year + 1
    total_precip = sum(valid_precip)

    prevailing_degrees = _circular_mean_wind_direction(valid_directions, valid_speeds)

    return {
        "prevailing_wind_direction": _degrees_to_compass(prevailing_degrees),
        "prevailing_wind_direction_degrees": round(prevailing_degrees, 1),
        "avg_annual_precipitation_mm": round(total_precip / num_years, 1),
        "max_daily_precipitation_mm": round(max(valid_precip), 1),
        "avg_high_temp_c": round(sum(valid_temps_max) / len(valid_temps_max), 1),
        "avg_low_temp_c": round(sum(valid_temps_min) / len(valid_temps_min), 1),
        "record_high_temp_c": round(max(valid_temps_max), 1),
        "record_low_temp_c": round(min(valid_temps_min), 1),
        "years_analyzed": num_years,
    }


def summarize_climate(summary: dict) -> str:
    """Plain-language summary of the climate data, split into design-relevant
    and contextual sections."""
    lines = [
        "DESIGN-RELEVANT:",
        f"  Prevailing wind: {summary['prevailing_wind_direction']} "
        f"({summary['prevailing_wind_direction_degrees']}°) — relevant for windbreak orientation.",
        f"  Average annual precipitation: {summary['avg_annual_precipitation_mm']} mm",
        f"  Heaviest single-day rainfall on record (last {summary['years_analyzed']} yrs): "
        f"{summary['max_daily_precipitation_mm']} mm — relevant for pond/swale/spillway sizing.",
        "",
        "CONTEXTUAL:",
        f"  Average high/low temperature: {summary['avg_high_temp_c']}°C / {summary['avg_low_temp_c']}°C",
        f"  Record high/low (last {summary['years_analyzed']} yrs): "
        f"{summary['record_high_temp_c']}°C / {summary['record_low_temp_c']}°C",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    lat, lon = 40.642485, -79.981816

    print(f"Fetching {10}-year climate summary for ({lat}, {lon})...\n")

    try:
        summary = get_climate_summary_for_point(lat, lon)
        print(summarize_climate(summary))
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
