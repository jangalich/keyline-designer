"""
irradiance_data.py

Fetches a regional solar irradiance/production baseline for a property
from NREL's PVWatts API, for a single representative point (the parcel
centroid) — NOT per solar-candidate-zone. Irradiance genuinely doesn't
vary meaningfully across a 20-acre property; the useful signal for
differentiating one candidate zone from another is slope/aspect/shading
(terrain_metrics.py), not irradiance. This module exists to give the
Scale of Permanence report a rough "expect roughly X kWh/kW/year here"
regional context note, not to rank anything.

Endpoint: per current guidance, NREL's developer API has moved off
developer.nrel.gov (being retired) onto developer.nlr.gov — this module
targets that current domain. NLR_PVWATTS_ENDPOINT below is the one place
that would need updating if that ever changes again.

Requires a free API key (same one covers PVWatts and the broader NREL/NLR
developer API), set as NREL_API_KEY. Like report_generator.py's
ANTHROPIC_API_KEY, this is a real credential requirement, not a network
flakiness thing — but unlike report_generator.py's hard dependency, a
missing key here just means the irradiance context note is skipped
(get_regional_irradiance_baseline returns None), the same graceful-
degradation pattern imagery_data.py and now water_candidate_zones.py use
for their own optional layers.

Docs: https://developer.nlr.gov/docs/solar/pvwatts/v8/
"""

import os
from typing import Optional

import requests

NLR_PVWATTS_ENDPOINT = "https://developer.nlr.gov/api/pvwatts/v8.json"

# A generic small reference system for the regional baseline — this is
# NOT sized to any actual candidate solar zone (that would need a real
# design pass this pipeline doesn't do), just a standard fixed reference
# so year-over-year/site-over-site numbers are comparable. Fixed-tilt,
# open-rack, tilt=latitude (a conventional simple default that roughly
# maximizes annual yield for a fixed array), true south.
REFERENCE_SYSTEM_CAPACITY_KW = 4.0
REFERENCE_MODULE_TYPE = 0  # standard
REFERENCE_ARRAY_TYPE = 0  # fixed, open rack
REFERENCE_LOSSES_PCT = 14.0  # PVWatts' own documented default
REFERENCE_AZIMUTH_DEG = 180.0  # true south


def get_regional_irradiance_baseline(
    latitude: float, longitude: float, api_key: Optional[str] = None
) -> Optional[dict]:
    """
    Returns a rough regional production/irradiance baseline for
    (latitude, longitude):

        {
            'annual_ac_kwh_per_kw': float,        # AC energy per kW of DC capacity, per year
            'avg_solar_radiation_kwh_per_m2_per_day': float,
            'capacity_factor_pct': float,
            'station_distance_miles': float,      # how far the weather station used is from this point
        }

    Returns None if no API key is available (NREL_API_KEY env var, or
    passed explicitly) — same "optional layer, don't crash the pipeline"
    pattern as imagery_data.py and water_candidate_zones.py. A request
    failure (bad key, service outage) also returns None rather than
    raising, for the same reason: this is regional context, not a
    required input to the constraint stack.
    """
    api_key = api_key or os.environ.get("NREL_API_KEY")
    if not api_key:
        return None

    params = {
        "api_key": api_key,
        "lat": latitude,
        "lon": longitude,
        "system_capacity": REFERENCE_SYSTEM_CAPACITY_KW,
        "module_type": REFERENCE_MODULE_TYPE,
        "array_type": REFERENCE_ARRAY_TYPE,
        "losses": REFERENCE_LOSSES_PCT,
        "tilt": abs(latitude),
        "azimuth": REFERENCE_AZIMUTH_DEG,
        "timeframe": "monthly",
    }

    try:
        response = requests.get(NLR_PVWATTS_ENDPOINT, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException:
        return None

    outputs = data.get("outputs")
    if not outputs:
        return None

    station_info = data.get("station_info", {})

    return {
        "annual_ac_kwh_per_kw": outputs.get("ac_annual"),
        "avg_solar_radiation_kwh_per_m2_per_day": outputs.get("solrad_annual"),
        "capacity_factor_pct": outputs.get("capacity_factor"),
        "station_distance_miles": station_info.get("distance"),
    }


def summarize_irradiance_baseline(baseline: Optional[dict]) -> str:
    if not baseline:
        return (
            "No regional irradiance baseline available (NREL_API_KEY not "
            "set, or the PVWatts request failed) — this is optional "
            "regional context, not a required input."
        )

    return (
        f"Regional PVWatts baseline (reference {REFERENCE_SYSTEM_CAPACITY_KW}kW fixed, "
        f"open-rack, tilt=latitude, true south): "
        f"~{baseline['annual_ac_kwh_per_kw']:.0f} AC kWh/kW/year, "
        f"avg solar radiation {baseline['avg_solar_radiation_kwh_per_m2_per_day']:.2f} kWh/m2/day. "
        f"This is parcel-scale regional context, not a per-candidate differentiator — "
        f"irradiance barely varies across a property this size."
    )


if __name__ == "__main__":
    # Test case: the user's own property (centroid of the drawn boundary).
    latitude, longitude = 40.6444, -79.9822

    print(f"Fetching regional irradiance baseline for ({latitude}, {longitude})...\n")

    baseline = get_regional_irradiance_baseline(latitude, longitude)
    print(summarize_irradiance_baseline(baseline))
