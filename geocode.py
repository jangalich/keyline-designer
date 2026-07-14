"""
geocode.py

Converts a street address into latitude/longitude coordinates using the
US Census Bureau's free Geocoding API. No API key required.

Docs: https://geocoding.geo.census.gov/geocoder/Geocoding_Services_API.html
"""

import requests
from typing import Optional

CENSUS_GEOCODER_ENDPOINT = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"


def geocode_address(address: str) -> Optional[dict]:
    """
    Takes a full address string (e.g. "5614 N Montour Rd, Gibsonia, PA 15044")
    and returns a dict with latitude, longitude, and the matched/standardized
    address the Census Bureau found.

    Returns None if no match was found (bad address, typo, address outside
    the US, etc.) — the caller should handle that case rather than assume
    a result always comes back.
    """
    params = {
        "address": address,
        "benchmark": "Public_AR_Current",
        "format": "json",
    }

    response = requests.get(CENSUS_GEOCODER_ENDPOINT, params=params, timeout=30)
    response.raise_for_status()

    data = response.json()
    matches = data.get("result", {}).get("addressMatches", [])

    if not matches:
        return None

    # Take the first (best) match. The Census Geocoder ranks matches by
    # confidence, so the top result is generally the right one for a
    # well-formed address.
    best_match = matches[0]
    coordinates = best_match["coordinates"]

    return {
        "latitude": coordinates["y"],
        "longitude": coordinates["x"],
        "matched_address": best_match["matchedAddress"],
    }


if __name__ == "__main__":
    # Quick manual test — swap this address for any US address to try it.
    test_address = "5614 N Montour Rd, Gibsonia, PA 15044"

    print(f"Geocoding: {test_address}\n")

    try:
        result = geocode_address(test_address)

        if result is None:
            print("No match found. Check the address for typos, or try a")
            print("simpler version (e.g. drop the ZIP code).")
        else:
            print(f"Matched address: {result['matched_address']}")
            print(f"Latitude:  {result['latitude']}")
            print(f"Longitude: {result['longitude']}")
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
