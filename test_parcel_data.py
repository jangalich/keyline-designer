"""
test_parcel_data.py

Offline (no-network, mocked) checks for parcel_data.py's own hard-fail
contract: fetch_parcel_data() fetches every raw layer exactly once,
upfront, and raises -- uncaught, uncached -- on ANY failure among ANY of
them, with no soft-fail exception anywhere, imagery and canopy height
included.

Every real fetch function fetch_parcel_data() calls is mocked here, so
this file never touches the network. Patches target parcel_data's OWN
namespace (parcel_data.get_dem_for_boundary, etc.) rather than each
source module's namespace, since parcel_data.py imports each function
directly with `from X import Y` -- patching X.Y would leave parcel_data's
own already-bound reference untouched.

Covers:
  1. Happy path: every fetch succeeds (real-shaped synthetic returns) ->
     ParcelData comes back with every field populated, and
     boundary_polygon_utm is a real, correctly-reprojected Polygon
     (checked against an independent warp_transform computed here).
  2. HARD FAIL, all twelve layers individually (dem, soil_components,
     farmland_classification, erosion_factor, saturated_hydraulic_
     conductivity, soil_geometries, water_features, farm_roads,
     climate_summary, elevation_grid, canopy_height, imagery_summary):
     mocking just that one layer's fetch to raise makes fetch_parcel_
     data() raise the SAME exception instance (identity-checked), no
     ParcelData is returned, and a second call under the same failing
     mock raises again with the failing mock's call count incremented --
     proving nothing about the failure is cached/memoized away.
  3. canopy_height_data.get_canopy_height_for_boundary() receives the
     REAL dem this call already fetched (identity check, `is`), not a
     second, independent DEM.
  4. All five soil functions receive the SAME wkt_polygon string object
     (identity check, `is`) -- computed once, not recomputed per call.

Bonus (beyond the required twelve hard-fail cases above): imagery_data.
get_imagery_summary_for_boundary() and canopy_height_data.get_canopy_
height_for_boundary() both document returning None as a genuine, non-
exceptional "nothing usable found" outcome distinct from a raised
exception (see parcel_data.py's own module docstring) -- confirmed live
against their real docstrings in this branch's own Step 0 signature
check. Since ParcelData.canopy_height/imagery_summary are required dict
fields, not Optional, parcel_data.py converts that None into a raised
ParcelDataIncompleteError instead of passing it through. Sections 5-6
below prove that conversion.
"""

from contextlib import ExitStack
from unittest.mock import Mock, patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform

import parcel_data
from dem_data import _utm_epsg_for_lonlat
from parcel_data import ParcelData, ParcelDataIncompleteError, fetch_parcel_data

# --- synthetic boundary + DEM, real-world centroid so the UTM CRS math is genuine ---

BOUNDARY_COORDINATES = [
    (-79.980, 40.640),
    (-79.975, 40.640),
    (-79.975, 40.645),
    (-79.980, 40.645),
    (-79.980, 40.640),
]

_center_lon = sum(pt[0] for pt in BOUNDARY_COORDINATES) / len(BOUNDARY_COORDINATES)
_center_lat = sum(pt[1] for pt in BOUNDARY_COORDINATES) / len(BOUNDARY_COORDINATES)
DEM_CRS = f"EPSG:{_utm_epsg_for_lonlat(_center_lon, _center_lat)}"

FAKE_DEM = {
    "array": np.full((10, 10), 300.0, dtype="float32"),
    "resolution_meters": (5.0, 5.0),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": DEM_CRS,
}
FAKE_SOIL_COMPONENTS = [{"mukey": "123", "component_name": "Loam", "pct_of_mapunit": 85}]
FAKE_FARMLAND_CLASSIFICATION = [{"mukey": "123", "farmlndcl": "All areas are prime farmland"}]
FAKE_EROSION_FACTOR = [{"mukey": "123", "kwfact": 0.32}]
FAKE_KSAT = [{"mukey": "123", "ksat_r": 9.5}]
FAKE_SOIL_GEOMETRIES = {"123": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}
FAKE_WATER_FEATURES = {"streams": [], "waterbodies": []}
FAKE_FARM_ROADS = [
    {"name": "N Montour Rd", "geometry": {"type": "LineString", "coordinates": [[-79.98, 40.64], [-79.97, 40.65]]}}
]
FAKE_CLIMATE_SUMMARY = {
    "prevailing_wind_direction": "SW",
    "prevailing_wind_direction_degrees": 225.0,
    "avg_annual_precipitation_mm": 1000.0,
}
FAKE_ELEVATION_GRID = [{"longitude": -79.98, "latitude": 40.64, "elevation_meters": 300.0}]
FAKE_CANOPY_HEIGHT = {
    "array": np.zeros((10, 10), dtype="float32"),
    "resolution_meters": (5.0, 5.0),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": DEM_CRS,
    "source_item_id": "3dep-lidar-hag-item",
}
FAKE_IMAGERY_SUMMARY = {
    "scene_date": "2026-05-14",
    "days_since_scene": 63,
    "cloud_cover_pct": 4.2,
    "pct_open_water": 2.0,
    "pct_bare_or_degraded_soil": 12.3,
    "pct_low_vegetation": 45.6,
    "pct_dense_vegetation": 40.1,
    "avg_ndvi": 0.35,
    "ndvi_min": -0.12,
    "ndvi_max": 0.82,
    "valid_pixel_count": 10234,
}

# name (as bound in parcel_data's own namespace) -> success return value
DEFAULTS = {
    "get_dem_for_boundary": FAKE_DEM,
    "get_soil_data_for_polygon": FAKE_SOIL_COMPONENTS,
    "get_farmland_classification_for_polygon": FAKE_FARMLAND_CLASSIFICATION,
    "get_erosion_factor_for_polygon": FAKE_EROSION_FACTOR,
    "get_saturated_hydraulic_conductivity_for_polygon": FAKE_KSAT,
    "get_soil_geometries_for_polygon": FAKE_SOIL_GEOMETRIES,
    "get_water_features_for_boundary": FAKE_WATER_FEATURES,
    "get_farm_roads_for_boundary": FAKE_FARM_ROADS,
    "get_climate_summary_for_point": FAKE_CLIMATE_SUMMARY,
    "get_elevation_grid": FAKE_ELEVATION_GRID,
    "get_canopy_height_for_boundary": FAKE_CANOPY_HEIGHT,
    "get_imagery_summary_for_boundary": FAKE_IMAGERY_SUMMARY,
}

SOIL_POLYGON_FN_NAMES = [
    "get_soil_data_for_polygon",
    "get_farmland_classification_for_polygon",
    "get_erosion_factor_for_polygon",
    "get_saturated_hydraulic_conductivity_for_polygon",
    "get_soil_geometries_for_polygon",
]


def _mocked(overrides=None):
    """
    ExitStack of mock_patch.object() calls, one per real fetch function
    fetch_parcel_data() calls, patched onto parcel_data's own namespace.
    Each defaults to a Mock(return_value=<the matching FAKE_* value>);
    pass overrides={'name': some_mock} to swap in a custom Mock (e.g.
    side_effect=SomeError(...)) for one entry. Returns (stack, mocks_dict)
    -- caller is responsible for entering/exiting the stack.
    """
    overrides = overrides or {}
    stack = ExitStack()
    mocks = {}
    for name, default_value in DEFAULTS.items():
        mock = overrides.get(name, Mock(return_value=default_value))
        stack.enter_context(mock_patch.object(parcel_data, name, mock))
        mocks[name] = mock
    return stack, mocks


class _Boom(Exception):
    pass


# --- 1. happy path ---

stack, mocks = _mocked()
with stack:
    result = fetch_parcel_data(BOUNDARY_COORDINATES)

assert isinstance(result, ParcelData)
assert result.dem is FAKE_DEM
assert result.soil_components is FAKE_SOIL_COMPONENTS
assert result.farmland_classification is FAKE_FARMLAND_CLASSIFICATION
assert result.erosion_factor is FAKE_EROSION_FACTOR
assert result.saturated_hydraulic_conductivity is FAKE_KSAT
assert result.soil_geometries is FAKE_SOIL_GEOMETRIES
assert result.water_features is FAKE_WATER_FEATURES
assert result.farm_roads is FAKE_FARM_ROADS
assert result.climate_summary is FAKE_CLIMATE_SUMMARY
assert result.elevation_grid is FAKE_ELEVATION_GRID
assert result.canopy_height is FAKE_CANOPY_HEIGHT
assert result.imagery_summary is FAKE_IMAGERY_SUMMARY

expected_xs, expected_ys = warp_transform(
    "EPSG:4326",
    DEM_CRS,
    [pt[0] for pt in BOUNDARY_COORDINATES],
    [pt[1] for pt in BOUNDARY_COORDINATES],
)
expected_coords = list(zip(expected_xs, expected_ys))
actual_coords = list(result.boundary_polygon_utm.exterior.coords)
assert len(actual_coords) == len(expected_coords)
for (ax, ay), (ex, ey) in zip(actual_coords, expected_coords):
    assert abs(ax - ex) < 1e-6 and abs(ay - ey) < 1e-6, (
        f"boundary_polygon_utm coordinate mismatch: got ({ax}, {ay}), expected ({ex}, {ey})"
    )
assert result.boundary_polygon_utm.is_valid
print("Happy path: fetch_parcel_data() returns a fully populated ParcelData with a correctly "
      "reprojected boundary_polygon_utm.")


# --- 2. hard fail, every layer, individually ---

for failing_name in DEFAULTS:
    boom = _Boom(f"{failing_name} failed")
    stack, mocks = _mocked({failing_name: Mock(side_effect=boom)})
    with stack:
        try:
            fetch_parcel_data(BOUNDARY_COORDINATES)
            raised = None
        except _Boom as e:
            raised = e

        assert raised is boom, (
            f"fetch_parcel_data() must raise the SAME exception instance when {failing_name} fails, "
            f"got {raised!r}"
        )
        assert mocks[failing_name].call_count == 1

        # uncached: calling again under the same failing mock raises again,
        # and the failing mock's own call count goes up -- nothing about the
        # failure (or a partial ParcelData) was memoized away.
        try:
            fetch_parcel_data(BOUNDARY_COORDINATES)
            raised_again = None
        except _Boom as e:
            raised_again = e

        assert raised_again is boom
        assert mocks[failing_name].call_count == 2, (
            f"a second call should re-invoke {failing_name} (call_count==2), not serve a cached result"
        )

    print(f"Hard fail confirmed: {failing_name} raising propagates the SAME exception "
          f"uncached, no ParcelData returned.")


# --- 3. canopy_height_data.get_canopy_height_for_boundary() receives the REAL dem, not a re-fetch ---

stack, mocks = _mocked()
with stack:
    fetch_parcel_data(BOUNDARY_COORDINATES)

canopy_call_args = mocks["get_canopy_height_for_boundary"].call_args
canopy_dem_arg = canopy_call_args.args[1] if len(canopy_call_args.args) > 1 else canopy_call_args.kwargs["dem"]
assert canopy_dem_arg is FAKE_DEM, (
    "get_canopy_height_for_boundary() must receive the exact dem object this call's own "
    "get_dem_for_boundary() fetch produced, not a second independent DEM"
)
assert mocks["get_dem_for_boundary"].call_count == 1
print("canopy_height fetch receives the real, already-fetched dem by identity, not a second DEM fetch.")


# --- 4. every soil function receives the SAME wkt_polygon string object ---

stack, mocks = _mocked()
with stack:
    fetch_parcel_data(BOUNDARY_COORDINATES)

wkt_args = [mocks[name].call_args.args[0] for name in SOIL_POLYGON_FN_NAMES]
first_wkt = wkt_args[0]
assert isinstance(first_wkt, str) and len(first_wkt) > 0
for name, wkt_arg in zip(SOIL_POLYGON_FN_NAMES, wkt_args):
    assert wkt_arg is first_wkt, (
        f"{name} should receive the SAME wkt_polygon string object computed once by "
        f"coordinates_to_wkt_polygon(), not an independently recomputed one"
    )
print("All five soil functions receive the identical, once-computed wkt_polygon string.")


# --- 5-6. bonus: None from imagery/canopy (their own documented "nothing found" outcome,
# distinct from a raised exception) is converted into a hard ParcelDataIncompleteError here ---

stack, mocks = _mocked({"get_canopy_height_for_boundary": Mock(return_value=None)})
with stack:
    try:
        fetch_parcel_data(BOUNDARY_COORDINATES)
        raised = None
    except ParcelDataIncompleteError as e:
        raised = e
assert raised is not None, (
    "get_canopy_height_for_boundary() returning None (its own documented 'no LiDAR HAG coverage' "
    "outcome) must still hard-fail fetch_parcel_data(), since canopy_height is a required, "
    "non-Optional ParcelData field"
)
print("Bonus: canopy_height fetch returning None (its own non-exceptional 'no coverage' outcome) "
      "is converted into a raised ParcelDataIncompleteError, not passed through as None.")

stack, mocks = _mocked({"get_imagery_summary_for_boundary": Mock(return_value=None)})
with stack:
    try:
        fetch_parcel_data(BOUNDARY_COORDINATES)
        raised = None
    except ParcelDataIncompleteError as e:
        raised = e
assert raised is not None, (
    "get_imagery_summary_for_boundary() returning None (its own documented 'no usable scene' "
    "outcome) must still hard-fail fetch_parcel_data(), since imagery_summary is a required, "
    "non-Optional ParcelData field -- the map is essential to the report"
)
print("Bonus: imagery_summary fetch returning None (its own non-exceptional 'no scene' outcome) "
      "is converted into a raised ParcelDataIncompleteError, not passed through as None.")

print("\nAll parcel_data.py checks passed.")
