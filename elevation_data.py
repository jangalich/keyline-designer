"""
elevation_data.py

Fetches elevation data from USGS's 3D Elevation Program (3DEP) via the
Elevation Point Query Service (EPQS). Free, no API key required, covers
the entire US.

Where available, 3DEP interpolates from real LiDAR-derived DEMs (as fine
as 1-meter resolution) — the same underlying data source behind county-
level LiDAR scans, just not always at the same resolution a dedicated
local scan would give you. Where high-res LiDAR hasn't been flown yet for
an area, it falls back to coarser 1/3 arc-second (~10m) data. Either way,
it's a reasonable nationwide default — a user with their own more precise
LiDAR survey could eventually upload it to override this layer, but this
gives every user a real starting point with zero setup.

Docs: https://epqs.nationalmap.gov/v1/docs
"""

import requests
import time
from typing import Optional

EPQS_ENDPOINT = "https://epqs.nationalmap.gov/v1/json"


def get_elevation_for_point(latitude: float, longitude: float) -> Optional[float]:
    """
    Returns the elevation in meters for a single lat/long point.
    Returns None if USGS has no data for that location (rare in the US,
    but possible right at coastlines or data gaps).
    """
    params = {
        "x": longitude,
        "y": latitude,
        "units": "Meters",
        "wkid": 4326,
        "includeDate": "false",
    }

    response = requests.get(EPQS_ENDPOINT, params=params, timeout=30)
    response.raise_for_status()

    data = response.json()
    elevation = data.get("value")

    if elevation is None:
        return None

    return float(elevation)


def _bounding_box(coordinates: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    """
    Given a list of (longitude, latitude) boundary points, returns the
    bounding box as (min_lon, min_lat, max_lon, max_lat).
    """
    lons = [pt[0] for pt in coordinates]
    lats = [pt[1] for pt in coordinates]
    return min(lons), min(lats), max(lons), max(lats)


def get_elevation_grid(
    boundary_coordinates: list[tuple[float, float]], grid_size: int = 10
) -> list[dict]:
    """
    Given a property boundary (list of (longitude, latitude) points, e.g.
    from a user-drawn shape or parcel_boundary.py), samples elevation at
    an evenly spaced grid_size x grid_size grid of points across the
    boundary's bounding box.

    This is the foundation for contour/slope/keypoint analysis — a single
    point tells you nothing about terrain shape, but a grid lets you see
    how elevation changes across the property.

    Note: this samples the bounding BOX around the property, not just
    points strictly inside an irregular boundary shape — some sampled
    points may fall just outside an oddly-shaped parcel. Refining that
    (true point-in-polygon filtering) is a reasonable next improvement,
    not necessary to get useful results today.

    Returns a list of dicts: [{'latitude', 'longitude', 'elevation'}, ...]
    Points where USGS had no data are skipped rather than included as None,
    so downstream code doesn't have to handle missing values.
    """
    min_lon, min_lat, max_lon, max_lat = _bounding_box(boundary_coordinates)

    results = []
    skipped = 0

    for i in range(grid_size):
        for j in range(grid_size):
            lon = min_lon + (max_lon - min_lon) * (i / (grid_size - 1))
            lat = min_lat + (max_lat - min_lat) * (j / (grid_size - 1))

            try:
                elevation = get_elevation_for_point(lat, lon)
            except requests.exceptions.RequestException:
                # USGS occasionally returns a transient error on an
                # individual point (server hiccup, brief rate limit).
                # Skip it rather than crashing the whole grid — losing
                # one point out of many doesn't meaningfully hurt the
                # terrain picture.
                elevation = None
                skipped += 1

            if elevation is not None:
                results.append(
                    {"latitude": lat, "longitude": lon, "elevation": elevation}
                )

            # Small pause between requests — polite to USGS's free public
            # service and reduces the odds of tripping a rate limit when
            # fetching a full grid of points back to back.
            time.sleep(0.3)

    if skipped:
        print(f"(Note: {skipped} of {grid_size * grid_size} points failed and were skipped.)")

    return results


def summarize_elevation_grid(grid: list[dict]) -> str:
    """
    Plain-language summary of an elevation grid — min/max/range, which is
    the first useful signal for understanding a property's terrain before
    any real contour analysis is built.
    """
    if not grid:
        return "No elevation data found for this area."

    elevations = [pt["elevation"] for pt in grid]
    min_elev = min(elevations)
    max_elev = max(elevations)
    relief = max_elev - min_elev

    return (
        f"Elevation range: {min_elev:.1f}m to {max_elev:.1f}m "
        f"(total relief: {relief:.1f}m) across {len(grid)} sample points."
    )


if __name__ == "__main__":
    # Test case: single point first (fast, simple sanity check)
    lat, lon = 40.642485, -79.981816

    print(f"Fetching elevation for point ({lat}, {lon})...\n")

    try:
        elevation = get_elevation_for_point(lat, lon)

        if elevation is None:
            print("No elevation data found for this point.")
        else:
            print(f"Elevation: {elevation:.1f} meters ({elevation * 3.28084:.1f} feet)")
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
