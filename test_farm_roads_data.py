"""
test_farm_roads_data.py

Offline (no-network, mocked) checks for farm_roads_data.py — specifically
a regression test for the real live bug this module had: querying layer 0
(a non-queryable container/group layer) returned an ArcGIS error embedded
in an HTTP 200 response, which the old code silently read as "zero roads
found" at every buffer size. See the module's own docstring for the full
story; this file locks in both fixes:

  1. _query_road_layer() must detect and raise on an "error" key in the
     JSON body even when the HTTP status is 200 -- this failure mode
     can't be caught by requests' own raise_for_status().
  2. get_farm_roads_for_boundary() queries every layer in ROAD_LAYERS and
     merges results, degrading independently per layer (one bad/missing
     layer doesn't discard data successfully fetched from the others).
"""

from unittest.mock import Mock, patch

import requests

import farm_roads_data
from farm_roads_data import (
    ROAD_EXCLUSION_BUFFER_METERS,
    ROAD_LAYERS,
    _deduplicate_road_features,
    _query_road_layer,
    get_farm_roads_for_boundary,
    get_road_exclusion_union_utm,
)

BBOX = (-80.0, 40.6, -79.9, 40.7)

LINE_A = {"type": "LineString", "coordinates": [[-79.98, 40.64], [-79.97, 40.65]]}
LINE_B = {"type": "LineString", "coordinates": [[-79.96, 40.63], [-79.95, 40.62]]}


def _feature(name: str, geometry: dict) -> dict:
    return {"type": "Feature", "properties": {"name": name}, "geometry": geometry}


# --- regression: an ArcGIS error embedded in an HTTP-200 body must be
# detected and raised, not silently read as "zero results" ---

def _mock_response(json_body: dict) -> Mock:
    response = Mock()
    response.raise_for_status = Mock()  # HTTP 200 -- no exception here
    response.json = Mock(return_value=json_body)
    return response


arcgis_error_body = {"error": {"code": 400, "message": "Invalid or missing input parameters."}}

with patch.object(requests, "get", return_value=_mock_response(arcgis_error_body)):
    try:
        _query_road_layer(0, BBOX)
        raised = False
    except RuntimeError as e:
        raised = True
        error_message = str(e)

assert raised, (
    "an ArcGIS error embedded in an HTTP 200 response body must raise, not be silently treated "
    "as zero features -- this is the exact real bug found live against layer 0"
)
assert "layer 0" in error_message and "400" in error_message, (
    f"the raised error should name the offending layer and ArcGIS error code, got: {error_message}"
)
print("_query_road_layer correctly detects and raises on an ArcGIS error embedded in an HTTP 200 body "
      "(the exact real bug: layer 0 silently reading as zero roads found).")

# A clean, error-free response must still work normally.
clean_body = {"type": "FeatureCollection", "features": [_feature("N Montour Rd", LINE_A)]}
with patch.object(requests, "get", return_value=_mock_response(clean_body)):
    features = _query_road_layer(32, BBOX)
assert len(features) == 1 and features[0]["properties"]["name"] == "N Montour Rd"
print("_query_road_layer returns real features normally when the response carries no ArcGIS error.")


# --- get_farm_roads_for_boundary queries every ROAD_LAYERS entry and merges results ---

boundary = [(-79.98, 40.64), (-79.97, 40.64), (-79.97, 40.65), (-79.98, 40.65), (-79.98, 40.64)]

per_layer_features = {
    30: [_feature("Highway 1", LINE_A)],
    31: [],
    32: [_feature("N Montour Rd", LINE_B)],
}


def fake_query_road_layer(layer_id, bbox, max_retries=2):
    return per_layer_features.get(layer_id, [])


with patch.object(farm_roads_data, "_query_road_layer", fake_query_road_layer):
    roads = get_farm_roads_for_boundary(boundary)

assert {r["name"] for r in roads} == {"Highway 1", "N Montour Rd"}, (
    f"expected results merged across all queried layers, got {[r['name'] for r in roads]}"
)
print(f"get_farm_roads_for_boundary merges results across ROAD_LAYERS={ROAD_LAYERS}: "
      f"{[r['name'] for r in roads]}.")


# --- one layer failing doesn't discard data successfully fetched from the others ---

def flaky_query_road_layer(layer_id, bbox, max_retries=2):
    if layer_id == ROAD_LAYERS[0]:
        raise requests.exceptions.ConnectionError("simulated network failure")
    return per_layer_features.get(layer_id, [])


with patch.object(farm_roads_data, "_query_road_layer", flaky_query_road_layer):
    roads_with_one_failure = get_farm_roads_for_boundary(boundary)

assert {r["name"] for r in roads_with_one_failure} == {"N Montour Rd"}, (
    "a single failed layer should degrade gracefully, keeping results from the layers that succeeded"
)
print("A single failed layer degrades gracefully -- results from the other, successful layers are kept.")


# --- every layer failing is a real, total failure -- must raise, not return [] silently ---

def always_failing_query_road_layer(layer_id, bbox, max_retries=2):
    raise requests.exceptions.ConnectionError("simulated total outage")


with patch.object(farm_roads_data, "_query_road_layer", always_failing_query_road_layer):
    try:
        get_farm_roads_for_boundary(boundary)
        raised_total_failure = False
    except requests.exceptions.ConnectionError:
        raised_total_failure = True

assert raised_total_failure, (
    "when every layer fails, get_farm_roads_for_boundary must raise (a real total outage), "
    "not silently return an empty list indistinguishable from 'no roads nearby'"
)
print("When every layer fails, get_farm_roads_for_boundary correctly raises rather than returning "
      "an empty list indistinguishable from a genuine 'no roads nearby' result.")


# --- deduplication drops exact-geometry duplicates, keeps distinct segments ---

duplicate_features = [_feature("N Montour Rd", LINE_A), _feature("N Montour Rd", LINE_A), _feature("N Montour Dr", LINE_B)]
deduped = _deduplicate_road_features(duplicate_features)
assert len(deduped) == 2, f"expected the exact-geometry duplicate dropped, got {len(deduped)} feature(s)"
print("_deduplicate_road_features drops exact-geometry duplicates while keeping distinct segments.")

# =====================================================================
# get_road_exclusion_union_utm(): buffers+unions get_farm_roads_for_boundary()'s
# own fetch into a single polygon in dem['crs'] -- the shapely-polygon-union
# input production_area.compute_step1_eligible_cells() tests each eligible
# cell's own center point against (the SAME pattern hydric soil already
# uses there, NOT canopy_height_data.py's raster-dilation approach -- road
# data is already clean vector geometry, not derived from a raster source).
# =====================================================================

ROAD_EXCLUSION_DEM = {
    "resolution_meters": (5.0, 5.0),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}

# --- no roads found nearby -> None (the common, clean case, not an error) ---
with patch.object(farm_roads_data, "get_farm_roads_for_boundary", lambda boundary_coordinates: []):
    no_roads_union = get_road_exclusion_union_utm(boundary, ROAD_EXCLUSION_DEM)
assert no_roads_union is None, "no roads found nearby must return None, same convention as _fetch_disqualifying_soil_union()"
print("get_road_exclusion_union_utm(): no roads found nearby correctly returns None.")

# --- ROAD_EXCLUSION_BUFFER_METERS default (0.0): buffering a LineString by 0 yields an
#     empty geometry, so the exclusion union is empty -> None (a documented, deliberate
#     consequence of the 0.0 default -- see that constant's own comment). ---
assert ROAD_EXCLUSION_BUFFER_METERS == 0.0, (
    f"this test documents the CURRENT default's consequence -- update it if "
    f"ROAD_EXCLUSION_BUFFER_METERS (currently {ROAD_EXCLUSION_BUFFER_METERS}) is ever retuned off exactly 0.0"
)
with patch.object(farm_roads_data, "get_farm_roads_for_boundary", lambda boundary_coordinates: [{"name": "N Montour Rd", "geometry": LINE_A}]):
    zero_buffer_union = get_road_exclusion_union_utm(boundary, ROAD_EXCLUSION_DEM)
assert zero_buffer_union is None, (
    "at the 0.0 default buffer, a real road line still produces an empty (zero-area) buffered union, "
    "which must read as None -- this gate is a documented no-op until a real buffer value is set"
)
print("get_road_exclusion_union_utm(): the 0.0 default buffer produces an empty union (None) even with a real road present.")

# --- a real, nonzero buffer produces a real polygon, reprojected into dem['crs'] ---
with patch.object(farm_roads_data, "get_farm_roads_for_boundary", lambda boundary_coordinates: [{"name": "N Montour Rd", "geometry": LINE_A}]):
    buffered_union = get_road_exclusion_union_utm(boundary, ROAD_EXCLUSION_DEM, buffer_meters=10.0)
assert buffered_union is not None and not buffered_union.is_empty
assert buffered_union.geom_type in ("Polygon", "MultiPolygon")
assert buffered_union.area > 0
print(
    f"get_road_exclusion_union_utm(): a real road line buffered by 10m produces a real "
    f"{buffered_union.geom_type} ({buffered_union.area:.1f} sq m) in the DEM's own CRS."
)

# --- multiple road lines union into one combined polygon, not separately returned ---
with patch.object(
    farm_roads_data,
    "get_farm_roads_for_boundary",
    lambda boundary_coordinates: [{"name": "N Montour Rd", "geometry": LINE_A}, {"name": "S Montour Rd", "geometry": LINE_B}],
):
    two_road_union = get_road_exclusion_union_utm(boundary, ROAD_EXCLUSION_DEM, buffer_meters=10.0)
assert two_road_union is not None and two_road_union.area > buffered_union.area, (
    "unioning a second, disjoint road line should produce strictly more excluded area than just the first"
)
print("get_road_exclusion_union_utm(): multiple fetched road lines union into a single combined exclusion polygon.")

# --- farm_roads= override: a caller-supplied roads list skips get_farm_roads_for_boundary() entirely ---
#
# get_farm_roads_for_boundary is stubbed to raise if called at all -- a regression that ignores the
# override and fetches anyway fails loudly here rather than silently passing with a self-fetched list.
def _raise_if_called(boundary_coordinates):
    raise AssertionError("get_farm_roads_for_boundary must not be called when farm_roads= is supplied")


with patch.object(farm_roads_data, "get_farm_roads_for_boundary", _raise_if_called):
    override_union = get_road_exclusion_union_utm(
        boundary, ROAD_EXCLUSION_DEM, buffer_meters=10.0, farm_roads=[{"name": "N Montour Rd", "geometry": LINE_A}]
    )
assert override_union is not None and not override_union.is_empty
assert override_union.geom_type in ("Polygon", "MultiPolygon")
assert override_union.area == buffered_union.area, (
    "the farm_roads= override must produce the exact same union as the equivalent self-fetched result "
    "above -- same input data, just sourced differently"
)
print(
    "get_road_exclusion_union_utm(): a caller-supplied farm_roads= skips get_farm_roads_for_boundary() "
    "entirely (call would raise if it fired) and produces the same union a self-fetch of the same data would."
)

# --- regression: farm_roads= omitted (None default) still self-fetches, unchanged ---
with patch.object(farm_roads_data, "get_farm_roads_for_boundary", lambda boundary_coordinates: [{"name": "N Montour Rd", "geometry": LINE_A}]):
    no_override_union = get_road_exclusion_union_utm(boundary, ROAD_EXCLUSION_DEM, buffer_meters=10.0)
assert no_override_union is not None and no_override_union.area == buffered_union.area, (
    "omitting farm_roads= entirely must still self-fetch via get_farm_roads_for_boundary(), unchanged "
    "from before this override was added"
)
print("get_road_exclusion_union_utm(): omitting farm_roads= entirely still self-fetches, unchanged.")

print("\nAll farm_roads_data checks passed.")
