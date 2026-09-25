"""
census_geography.py

THE COUNTY AND STATE the parcel lies in, for the cover and the site
overview: one reverse lookup at the boundary's centroid against the U.S.
Census Bureau geocoder -- the same service geocode.py already calls
forward, so no new host.

    get_county_state_for_point(lat, lon) -> the raw response (a fetch)
    parse_county_state(raw)              -> {'county', 'state', ...}

ONE POINT, AND WHAT THAT MEANS. The county is the one containing the
centroid. A parcel straddling a county line is named for the county under
its centre -- the same point every point-based report layer uses
(report_data.boundary_centroid_lat_lon).

TERMS. A U.S. federal work in the public domain; the geocoder takes no
key and states no use restriction. The response names its own vintage and
benchmark, which the vintage table prints.
"""

from typing import Optional

import requests

import fetch_attempts

GEOGRAPHIES_ENDPOINT = "https://geocoding.geo.census.gov/geocoder/geographies/coordinates"
BENCHMARK = "Public_AR_Current"
VINTAGE = "Current_Current"
LAYERS = "Counties,States"

CITATION = "U.S. Census Bureau Geocoder, geographies by coordinates, benchmark Public_AR_Current, vintage Current_Current."
TERMS = "U.S. federal work; public domain. No key and no stated use restriction."


class CensusGeographyIncompleteError(RuntimeError):
    """The geocoder answered, but with no county or state for the point
    (offshore, outside the United States)."""


def __getattr__(name):
    return fetch_attempts.published(__name__, name)


@fetch_attempts.publishes
def get_county_state_for_point(lat: float, lon: float, max_retries: int = 2) -> dict:
    params = {"x": f"{lon:.6f}", "y": f"{lat:.6f}", "benchmark": BENCHMARK, "vintage": VINTAGE,
              "layers": LAYERS, "format": "json"}
    last_error = None
    for attempt in fetch_attempts.attempts(max_retries):
        try:
            response = requests.get(GEOGRAPHIES_ENDPOINT, params=params, timeout=30 + attempt * 30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < max_retries:
                fetch_attempts.sleep(fetch_attempts.RETRY_PAUSE_SECONDS)
                continue
            raise last_error


def parse_county_state(raw: dict) -> dict:
    """
    {'county': 'Allegheny County', 'state': 'Pennsylvania', 'county_fips':
     '42003', 'state_fips': '42', 'vintage': 'Current_Current',
     'benchmark': 'Public_AR_Current'}

    Raises CensusGeographyIncompleteError when the response carries no
    county or no state.
    """
    result = (raw or {}).get("result") or {}
    geographies = result.get("geographies") or {}
    counties = geographies.get("Counties") or []
    states = geographies.get("States") or []
    if not counties or not states:
        raise CensusGeographyIncompleteError("the Census geocoder returned no county or state for the point")
    county, state = counties[0], states[0]
    inputs = result.get("input") or {}
    return {
        "county": county.get("NAME"),
        "state": state.get("NAME"),
        "county_fips": county.get("GEOID"),
        "state_fips": state.get("GEOID"),
        "vintage": (inputs.get("vintage") or {}).get("vintageName"),
        "benchmark": (inputs.get("benchmark") or {}).get("benchmarkName"),
    }


def county_state_label(block: Optional[dict]) -> Optional[str]:
    """'Allegheny County, Pennsylvania', or None."""
    if not block:
        return None
    return f"{block['county']}, {block['state']}"
