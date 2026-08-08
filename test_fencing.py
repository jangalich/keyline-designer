"""
test_fencing.py

Offline (no-network) checks for fencing.py's stream exclusion and
boundary fencing geometry -- the two fencing types that get real
computed geometry (see fencing.py's module docstring for why everything
else in Subdivision Fences is narrative-only and deliberately NOT
generated here).

Stream features here are hand-built schema Features in the same shape
hydrology_data.get_water_features_geojson() actually produces (real
WGS84 line geometry, wrapped with make_feature()), not fetched from NHD
at all -- this tests find_stream_exclusion_fencing()'s buffer/geometry
logic directly, independent of whether the live NHD service is reachable
(it isn't from every environment -- see other test_*.py files' notes
on the same limitation for their own network-backed layers).

find_boundary_fencing() (the pure geometric core behind boundary
fencing) gets its own dedicated, purely-synthetic section below, same
"pure core is independently testable" pattern this file already uses for
find_stream_exclusion_fencing() -- plain shapely boxes in an arbitrary
UTM-like coordinate space, no boundary_coordinates/DEM/CRS involved at
all. identify_boundary_fencing()/identify_fencing() (the fetch-and-wrap
entry points) additionally need production_area.get_canopy_height_for_
boundary() mocked (same pattern test_production_area_ceiling.py already
uses for its own full-pipeline canopy-gate scenarios) to stay offline,
since both now carry a MANDATORY canopy fetch.
"""

import math

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Point, Polygon, box, shape
from unittest.mock import patch as mock_patch

import fencing
import production_area as pa
from feature_schema import make_feature, validate_feature_collection
from fencing import (
    BOUNDARY_FENCE_CANOPY_BUFFER_METERS,
    BOUNDARY_FENCE_MIN_SEGMENT_ACRES,
    EXISTING_FARM_ROAD_FENCE_BUFFER_METERS,
    ROAD_CORRIDOR_FENCE_DILATION_CELLS,
    ROAD_FENCE_LINE_INSET_METERS,
    STREAM_EXCLUSION_BUFFER_METERS,
    TREE_ZONE_FENCE_BUFFER_METERS,
    WATER_ZONE_FENCE_BUFFER_METERS,
    boundary_fencing_to_geojson,
    existing_farm_road_fencing_to_geojson,
    find_boundary_fencing,
    find_existing_farm_road_fencing,
    find_road_corridor_fencing,
    find_stream_exclusion_fencing,
    find_tree_zone_fencing,
    find_water_zone_fencing,
    identify_boundary_fencing,
    identify_fencing,
    road_corridor_fencing_to_geojson,
    stream_exclusion_fencing_to_geojson,
    tree_zone_fencing_to_geojson,
    water_zone_fencing_to_geojson,
)
from raster_grid import binary_dilate, cell_union_footprint

UTM_CRS = "EPSG:32617"

# The user's real property boundary (same one every other module's
# __main__ block tests against).
PROPERTY_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

STREAM_FEATURE = make_feature(
    feature_id="nhd-streams-12345",
    geometry={
        "type": "LineString",
        "coordinates": [(-79.9838154, 40.6458343), (-79.9827466, 40.6428581), (-79.9813665, 40.6440549)],
    },
    layer="hydrology-streams",
    label="Montour Run",
    confidence="medium",
    confidence_notes="test fixture, not real NHD data",
)

STREAM_WITH_NO_GEOMETRY = make_feature(
    feature_id="nhd-streams-99999",
    geometry={"type": "LineString", "coordinates": [(0, 0), (0, 1)]},  # placeholder, overwritten below
    layer="hydrology-streams",
    label="Broken stream",
    confidence="medium",
    confidence_notes="test fixture, not real NHD data",
)
STREAM_WITH_NO_GEOMETRY["geometry"] = {"type": "LineString", "coordinates": []}


# --- find_stream_exclusion_fencing: buffer distance is geometrically correct ---

entries = find_stream_exclusion_fencing([STREAM_FEATURE], UTM_CRS, buffer_meters=STREAM_EXCLUSION_BUFFER_METERS)
assert len(entries) == 1, f"expected exactly 1 fencing entry, got {len(entries)}"

entry = entries[0]
assert entry["source_feature_id"] == "nhd-streams-12345"
assert entry["source_label"] == "Montour Run"
assert entry["geometry_wgs84"]["type"] in ("LineString", "MultiLineString")

# The output is the OUTLINE of the buffer, not the filled polygon -- confirm
# every point on that outline sits close to STREAM_EXCLUSION_BUFFER_METERS
# from the original stream centerline (within a couple meters of tolerance
# for buffer-corner/projection rounding), and none of them sit ON the
# centerline itself (which a filled-polygon bug would produce, since a
# polygon's boundary can pass through the source line at self-intersections
# -- this stream is a simple open curve, so that shouldn't happen here).
from rasterio.warp import transform_geom
from shapely.geometry import mapping

original_line_utm = shape(transform_geom("EPSG:4326", UTM_CRS, STREAM_FEATURE["geometry"]))
fence_line_utm = shape(transform_geom("EPSG:4326", UTM_CRS, entry["geometry_wgs84"]))

sample_points = list(fence_line_utm.coords) if fence_line_utm.geom_type == "LineString" else [
    pt for part in fence_line_utm.geoms for pt in part.coords
]
assert len(sample_points) > 0, "buffered fence line should have vertices"

distances = [Point(pt).distance(original_line_utm) for pt in sample_points]
assert all(abs(d - STREAM_EXCLUSION_BUFFER_METERS) < 2.0 for d in distances), (
    f"fence-line vertices should sit ~{STREAM_EXCLUSION_BUFFER_METERS}m from the stream centerline, "
    f"got distances ranging {min(distances):.2f}-{max(distances):.2f}m"
)
print(
    f"Stream exclusion fence line sits ~{STREAM_EXCLUSION_BUFFER_METERS}m from the stream "
    "centerline (buffer OUTLINE, not a filled zone)."
)


# --- a custom buffer distance is honored ---

wide_entries = find_stream_exclusion_fencing([STREAM_FEATURE], UTM_CRS, buffer_meters=25.0)
wide_fence_line_utm = shape(transform_geom("EPSG:4326", UTM_CRS, wide_entries[0]["geometry_wgs84"]))
wide_points = list(wide_fence_line_utm.coords) if wide_fence_line_utm.geom_type == "LineString" else [
    pt for part in wide_fence_line_utm.geoms for pt in part.coords
]
wide_distances = [Point(pt).distance(original_line_utm) for pt in wide_points]
assert all(abs(d - 25.0) < 2.0 for d in wide_distances), "a custom buffer_meters should be honored exactly"
print("Custom exclusion_buffer_meters is honored in the output geometry.")


# --- streams with empty/missing geometry are skipped, not raised on ---

skip_entries = find_stream_exclusion_fencing([STREAM_FEATURE, STREAM_WITH_NO_GEOMETRY], UTM_CRS)
assert len(skip_entries) == 1, "a stream with empty coordinates should be skipped, not crash or produce bad geometry"
print("Stream features with empty/missing geometry are skipped without raising.")


# --- stream_exclusion_fencing_to_geojson: schema-valid, correct layer/properties ---

stream_geojson = stream_exclusion_fencing_to_geojson(entries)
validate_feature_collection(stream_geojson)
stream_feature_out = stream_geojson["features"][0]
assert stream_feature_out["properties"]["layer"] == "exclusion_fencing"
assert stream_feature_out["properties"]["source_feature_id"] == "nhd-streams-12345"
assert stream_feature_out["properties"]["exclusion_buffer_meters"] == STREAM_EXCLUSION_BUFFER_METERS
notes = stream_feature_out["properties"]["confidence_notes"].lower()
assert "suggested" in notes and "not a surveyed fence line" in notes, (
    "stream exclusion confidence_notes must flag this as a suggested boundary, not a surveyed fence line"
)
print("stream_exclusion_fencing_to_geojson output is schema-valid, layer='exclusion_fencing'.")


# =====================================================================
# find_boundary_fencing(): pure geometric core, purely synthetic geometry,
# no boundary_coordinates/DEM/CRS/network involved at all -- same "pure
# core is independently testable" pattern as find_stream_exclusion_
# fencing() above. A plain 100m x 100m square standing in for
# boundary_polygon_utm; canopy_union_utm fixtures are hand-built shapely
# geometry standing in for an already-buffered canopy footprint (the
# buffering itself is identify_boundary_fencing()'s job, not this
# function's -- see its own docstring).
# =====================================================================

TEST_BOUNDARY_UTM = box(0, 0, 100, 100)  # 10,000 sq m, ~2.47 acres


# --- 1. no canopy at all -> the plain-wrap case, unchanged: the boundary's own exterior ring ---

no_canopy_rings = find_boundary_fencing(TEST_BOUNDARY_UTM, None)
assert len(no_canopy_rings) == 1, f"no canopy should return exactly 1 ring, got {len(no_canopy_rings)}"
assert no_canopy_rings[0].equals(Polygon(TEST_BOUNDARY_UTM.exterior).exterior), (
    "with no canopy at all, the returned ring must match the boundary's own exterior ring exactly"
)
print("find_boundary_fencing(): no canopy returns the boundary's own unmodified exterior ring.")


# --- 2. canopy touching the boundary at one location, not splitting it -> 1 ring, genuinely notched ---

touching_canopy = box(40, 90, 60, 110)  # straddles the top edge (y=100) from x=40-60, doesn't reach either side edge
touching_rings = find_boundary_fencing(TEST_BOUNDARY_UTM, touching_canopy)
assert len(touching_rings) == 1, f"a single non-splitting notch should return exactly 1 ring, got {len(touching_rings)}"
assert not touching_rings[0].equals(Polygon(TEST_BOUNDARY_UTM.exterior).exterior), (
    "a real notch must NOT match the plain boundary ring -- confirms the notch actually happened"
)
print("find_boundary_fencing(): canopy touching one edge location returns 1 ring, genuinely notched inward.")


# --- 3. canopy entirely interior, never touching the boundary edge -> ignored: unmodified boundary ring ---

interior_canopy = box(30, 30, 50, 50)  # well inside TEST_BOUNDARY_UTM, doesn't touch any edge
interior_rings = find_boundary_fencing(TEST_BOUNDARY_UTM, interior_canopy)
assert len(interior_rings) == 1, f"interior-only canopy should return exactly 1 ring, got {len(interior_rings)}"
# NOT an exact .equals() any more: find_boundary_fencing()'s own morphological opening
# (buffer(-BOUNDARY_FENCE_MIN_SLIVER_WIDTH_METERS).buffer(+that)) now runs unconditionally
# whenever there's ANY canopy, including this interior-only case -- and a buffer-based
# opening inherently rounds convex corners by a small amount (a real, expected side effect
# of continuous-geometry morphology, not a raster operation), so the returned outer ring is
# no longer byte-identical to the plain square even though the interior hole itself never
# reaches the boundary edge. Checked via symmetric-difference AREA instead: the interior
# hole itself is 400 sq m (20x20m) -- if it were leaking into the outer ring at all, the
# symmetric difference would be on that order. The opening's own corner-rounding alone
# accounts for well under 10 sq m (four ~100 sq m corners, each rounded by a sub-meter
# amount) on this 10,000 sq m square, so a 10 sq m ceiling cleanly distinguishes "hole
# genuinely ignored, only cosmetic opening rounding visible" from "hole leaked into the ring".
interior_ring_polygon = Polygon(interior_rings[0])
plain_boundary_polygon = Polygon(TEST_BOUNDARY_UTM.exterior)
interior_symmetric_diff_area = interior_ring_polygon.symmetric_difference(plain_boundary_polygon).area
assert interior_symmetric_diff_area < 10.0, (
    "canopy that never touches the boundary edge carves a HOLE, not a change to the fence line -- the "
    "returned ring must closely match the unmodified boundary ring (up to the opening's own tiny "
    f"corner-rounding), got symmetric difference area {interior_symmetric_diff_area:.4f} sq m"
)
print("find_boundary_fencing(): interior-only canopy (a hole, discarded) leaves the boundary ring unmodified.")


# --- 4. canopy spanning end-to-end, splitting the parcel -> exactly 2 closed loops ---

spanning_canopy = box(-10, 45, 110, 55)  # crosses both the left (x=0) and right (x=100) edges
split_rings = find_boundary_fencing(TEST_BOUNDARY_UTM, spanning_canopy)
assert len(split_rings) == 2, f"canopy spanning end-to-end should split the parcel into 2 loops, got {len(split_rings)}"
for ring in split_rings:
    assert list(ring.coords)[0] == list(ring.coords)[-1], "each split loop must be a closed LineString"
print(f"find_boundary_fencing(): canopy spanning end-to-end splits the parcel into {len(split_rings)} closed loops.")


# --- 5. a tiny sliver-touch case (canopy just grazing a corner) -> dropped by the min-segment-acres floor ---

# A diagonal band (a buffered line, real width -- not a single-point touch) that crosses both the top
# and right edges near the (100,100) corner without covering the corner itself, cleanly severing a tiny
# triangular sliver (~0.025 acres, well under BOUNDARY_FENCE_MIN_SEGMENT_ACRES) from the main body.
from shapely.geometry import LineString as _LineString

sliver_canopy = _LineString([(75, 105), (105, 75)]).buffer(4.0)
sliver_pieces_raw = TEST_BOUNDARY_UTM.difference(sliver_canopy)
assert sliver_pieces_raw.geom_type == "MultiPolygon" and len(sliver_pieces_raw.geoms) == 2, (
    "test setup should genuinely produce a main body + a separate tiny corner sliver"
)
sliver_piece_acres = min(g.area for g in sliver_pieces_raw.geoms) / 4046.8564224
assert sliver_piece_acres < BOUNDARY_FENCE_MIN_SEGMENT_ACRES, (
    f"test setup's sliver ({sliver_piece_acres} ac) must genuinely sit below the "
    f"{BOUNDARY_FENCE_MIN_SEGMENT_ACRES} ac floor for this to be a real test of the filter"
)

sliver_rings = find_boundary_fencing(TEST_BOUNDARY_UTM, sliver_canopy)
assert len(sliver_rings) == 1, (
    f"the tiny corner sliver must be dropped by BOUNDARY_FENCE_MIN_SEGMENT_ACRES, not returned as a "
    f"spurious extra segment -- expected 1 ring (main body only), got {len(sliver_rings)}"
)
print(
    f"find_boundary_fencing(): a tiny corner sliver ({sliver_piece_acres:.4f} ac) grazing a boundary "
    f"corner is dropped by BOUNDARY_FENCE_MIN_SEGMENT_ACRES ({BOUNDARY_FENCE_MIN_SEGMENT_ACRES} ac), "
    "leaving just the main body ring."
)


# --- 6. two canopy patches close together on the boundary, leaving a narrow land "neck"
#        between them (pic #1's shape) -> the morphological opening collapses the neck; the
#        returned ring no longer traces up into and back out of it ---

neck_notch_a = box(30, 80, 49.6, 105)  # touches the top edge (y=100), left patch
neck_notch_b = box(50.4, 80, 70, 105)  # touches the top edge (y=100), right patch -- 0.8m gap from A
neck_canopy = neck_notch_a.union(neck_notch_b)

# Test setup validity: the RAW (pre-opening) difference must genuinely have kept land reaching
# all the way up through the neck to the boundary edge -- the neck's own 0.8m width is narrower
# than 2 * BOUNDARY_FENCE_MIN_SLIVER_WIDTH_METERS (2.0m), so the opening should collapse it
# entirely (merge it into the excluded/canopy side) rather than leave it as real usable land.
raw_neck_difference = TEST_BOUNDARY_UTM.difference(neck_canopy)
neck_probe_point = Point(50.0, 99.0)  # deep in the neck, right at the boundary edge
assert raw_neck_difference.contains(neck_probe_point), (
    "test setup must genuinely produce kept land reaching up through the neck to the boundary "
    "edge (pre-opening) for this to be a real test of the collapse"
)

neck_rings = find_boundary_fencing(TEST_BOUNDARY_UTM, neck_canopy)
assert len(neck_rings) == 1, (
    f"expected 1 ring (main body only, neck collapsed into the canopy side), got {len(neck_rings)}"
)
neck_ring_polygon = Polygon(neck_rings[0])
assert not neck_ring_polygon.intersects(neck_probe_point), (
    "the opening should have collapsed the narrow neck into the excluded/canopy side -- the "
    "returned ring's own enclosed area must no longer reach up through the neck to the boundary edge"
)
print(
    "find_boundary_fencing(): a narrow land neck between two close canopy notches (pic #1's shape) "
    "is collapsed by the morphological opening step, not traced into and back out of by the fence line."
)


# =====================================================================
# identify_boundary_fencing()/identify_fencing(): fetch-and-wrap entry points. Both now carry a
# MANDATORY canopy fetch (production_area.get_required_tree_root_zone_mask_utm()) -- mocked here
# via production_area.get_canopy_height_for_boundary (same pattern test_production_area_ceiling.py
# already uses for its own full-pipeline canopy-gate scenarios) so this stays fully offline. A
# synthetic DEM sized to PROPERTY_BOUNDARY's own UTM extent stands in for a real fetch.
# =====================================================================


def _fake_no_canopy(boundary_coordinates, dem):
    return {
        "array": np.full(dem["array"].shape, 1.0, dtype=np.float32),  # below threshold everywhere -- no trees
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source_item_id": "offline-test-stub-no-canopy",
    }


_boundary_xs, _boundary_ys = warp_transform(
    "EPSG:4326", UTM_CRS, [pt[0] for pt in PROPERTY_BOUNDARY], [pt[1] for pt in PROPERTY_BOUNDARY]
)
_PAD_M = 50.0
_RES = (5.0, 5.0)
_minx, _maxx = min(_boundary_xs) - _PAD_M, max(_boundary_xs) + _PAD_M
_miny, _maxy = min(_boundary_ys) - _PAD_M, max(_boundary_ys) + _PAD_M
_cols = int((_maxx - _minx) / _RES[0]) + 1
_rows = int((_maxy - _miny) / _RES[1]) + 1
TEST_DEM = {
    "array": np.full((_rows, _cols), 300.0, dtype=np.float32),
    "resolution_meters": _RES,
    "origin_x": _minx,
    "origin_y": _maxy,
    "crs": UTM_CRS,
}


# --- identify_boundary_fencing: no canopy -> 1 schema-valid "perimeter_fencing" feature, fence_type=boundary ---

with mock_patch.object(pa, "get_canopy_height_for_boundary", _fake_no_canopy):
    boundary_result = identify_boundary_fencing(PROPERTY_BOUNDARY, dem=TEST_DEM)
validate_feature_collection(boundary_result["fencing_geojson"])
assert boundary_result["segment_count"] == 1
boundary_features = boundary_result["fencing_geojson"]["features"]
assert len(boundary_features) == 1
boundary_feature = boundary_features[0]
assert boundary_feature["properties"]["layer"] == "perimeter_fencing"
assert boundary_feature["properties"]["fence_type"] == "boundary"
assert boundary_feature["properties"]["canopy_buffer_meters"] == BOUNDARY_FENCE_CANOPY_BUFFER_METERS
assert boundary_feature["properties"]["confidence"] == "high"
assert boundary_feature["geometry"]["type"] == "LineString", (
    "boundary fencing must be a LineString (a fence line), not a Polygon (a filled zone)"
)
boundary_out_coords = boundary_feature["geometry"]["coordinates"]
assert tuple(boundary_out_coords[0]) == tuple(boundary_out_coords[-1]), "boundary fence line should be a closed ring"
print("identify_boundary_fencing() with no real canopy produces 1 schema-valid 'perimeter_fencing' feature.")

assert "no fence type, height, or material" in boundary_feature["properties"]["confidence_notes"].lower(), (
    "boundary fencing confidence_notes must explicitly state fence type/height/material is out of scope"
)
assert "inset inward" in boundary_feature["properties"]["confidence_notes"].lower(), (
    "boundary fencing confidence_notes must explicitly describe the canopy-inset behavior"
)
print("Boundary fencing confidence_notes explicitly excludes fence type/height/material guidance.")


# --- boundary_fencing_to_geojson: a multi-segment split labels/annotates each segment ---

split_geojson = boundary_fencing_to_geojson(
    [_LineString(box(0, 0, 10, 10).exterior.coords), _LineString(box(20, 20, 30, 30).exterior.coords)]
)
validate_feature_collection(split_geojson)
split_features = split_geojson["features"]
assert len(split_features) == 2
assert [f["properties"]["segment_index"] for f in split_features] == [1, 2]
assert [f["properties"]["label"] for f in split_features] == ["Boundary fencing 1", "Boundary fencing 2"]
for f in split_features:
    assert f"{2} separate fence loops" in f["properties"]["confidence_notes"], (
        "a multi-segment result's confidence_notes must explicitly call out the split and how many "
        "separate physical fence runs it requires"
    )
print("boundary_fencing_to_geojson() labels/annotates a multi-segment split (segment_index, split note) correctly.")


# --- identify_fencing: combines both layers, reusing a pre-fetched water_features_geojson ---
#
# farm_road_features=[] and tree_zone_render_fill_polygons_utm=[] are passed explicitly
# (rather than left to identify_fencing()'s own default fetch) so this stays deterministic
# and offline regardless of whether a given test environment happens to have network access --
# unlike the farm road fetch (which fails fast in this sandbox via a proxy 403), an
# unmocked water/tree zone fetch can hang for a long time retrying before finally giving up,
# so leaving either at its own default (None) here would make this test far too slow, not
# just nondeterministic. selected_water_zone_render_fill_polygon_utm has no such "empty but
# not None" sentinel available (a single selected zone is naturally Optional[Polygon] --
# None already means BOTH "not supplied" and "no zone sited"), so fetch_and_select_optimal_
# water_zone() itself is mocked out instead, same pattern as the existing canopy-height mock
# just below it. selected_road_corridor_cells is left at its own default (None, with
# anchor_lon_lat also None) which already resolves to "no corridor" with zero network calls
# (see identify_fencing()'s own docstring), so no explicit override/mock is needed there.

from feature_schema import make_feature_collection


def _fake_no_water_zone(boundary_coordinates, dem=None, **kwargs):
    return None


prefetched_water = make_feature_collection([STREAM_FEATURE])
with mock_patch.object(pa, "get_canopy_height_for_boundary", _fake_no_canopy), mock_patch.object(
    fencing, "fetch_and_select_optimal_water_zone", _fake_no_water_zone
):
    result = identify_fencing(
        PROPERTY_BOUNDARY,
        water_features_geojson=prefetched_water,
        dem=TEST_DEM,
        farm_road_features=[],
        tree_zone_render_fill_polygons_utm=[],
    )
validate_feature_collection(result["fencing_geojson"])

layers = sorted(f["properties"]["layer"] for f in result["fencing_geojson"]["features"])
assert layers == ["exclusion_fencing", "perimeter_fencing"], f"expected both fencing layers, got {layers}"
assert result["segment_count"] == 1
print("identify_fencing() combines exclusion_fencing + perimeter_fencing into one schema-valid FeatureCollection.")


# --- identify_fencing: a water_features_geojson with no streams still produces boundary fencing ---

no_stream_water = make_feature_collection([])
with mock_patch.object(pa, "get_canopy_height_for_boundary", _fake_no_canopy), mock_patch.object(
    fencing, "fetch_and_select_optimal_water_zone", _fake_no_water_zone
):
    no_stream_result = identify_fencing(
        PROPERTY_BOUNDARY,
        water_features_geojson=no_stream_water,
        dem=TEST_DEM,
        farm_road_features=[],
        tree_zone_render_fill_polygons_utm=[],
    )
no_stream_layers = [f["properties"]["layer"] for f in no_stream_result["fencing_geojson"]["features"]]
assert no_stream_layers == ["perimeter_fencing"], (
    f"with no streams, only perimeter_fencing should be produced, got {no_stream_layers}"
)
print("identify_fencing() still produces boundary fencing when no streams are present.")


# =====================================================================
# find_road_corridor_fencing(): pure geometric core, purely synthetic DEM/cells,
# no network/boundary_coordinates involved -- same "pure core is independently
# testable" pattern as find_boundary_fencing() above. A small synthetic DEM
# (its own 'array'/'resolution_meters'/'origin_x'/'origin_y'/'crs', same shape
# raster_grid.py's own functions expect) stands in for a real fetched DEM.
# =====================================================================

ROAD_FENCE_DEM = {
    "array": np.zeros((20, 20), dtype=np.float32),
    # 5.0m resolution (was 2.0m, widened for ROAD_FENCE_LINE_INSET_METERS's 0.3048m -> 2.0m
    # increase) -- a 1-cell dilation on either side of a single-cell-wide path gives a 15.0m-
    # wide dilated footprint here, leaving an 11.0m-wide residual after the 2.0m inset on both
    # sides: a comfortable, unambiguous margin above the "empties out" fallback case below,
    # not a near-miss that could collapse the two cases (normal inset success vs. genuine
    # pinch-triggered fallback) into the same outcome.
    "resolution_meters": (5.0, 5.0),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": UTM_CRS,
}


# --- 1. None/empty cells -> returns None, not an error or an empty LineString ---

assert find_road_corridor_fencing(ROAD_FENCE_DEM, None) is None
assert find_road_corridor_fencing(ROAD_FENCE_DEM, []) is None
print("find_road_corridor_fencing(): None/empty selected_road_corridor_cells returns None.")


# --- 2. a simple straight-line synthetic path -> a single closed LineString, genuinely
#        inset INSIDE the dilated footprint's own outer edge (not just the footprint itself) ---

straight_path_cells = [(10, c) for c in range(3, 17)]

straight_fence_line = find_road_corridor_fencing(ROAD_FENCE_DEM, straight_path_cells)
assert straight_fence_line is not None and straight_fence_line.geom_type == "LineString"
straight_coords = list(straight_fence_line.coords)
assert straight_coords[0] == straight_coords[-1], "road corridor fence line must be a closed ring"

straight_mask = np.zeros(ROAD_FENCE_DEM["array"].shape, dtype=bool)
for r, c in straight_path_cells:
    straight_mask[r, c] = True
straight_dilated_mask = binary_dilate(straight_mask, ROAD_CORRIDOR_FENCE_DILATION_CELLS)
straight_dilated_footprint = cell_union_footprint(ROAD_FENCE_DEM, straight_dilated_mask)
straight_would_be_inset = straight_dilated_footprint.buffer(-ROAD_FENCE_LINE_INSET_METERS)
assert not straight_would_be_inset.is_empty and straight_would_be_inset.is_valid, (
    "test setup must genuinely produce a real, non-empty negative-buffer result here -- otherwise "
    "this case and the narrow/pinched case below would both hit the same fallback path, and this "
    "would no longer be a real test of the normal (non-fallback) inset behavior"
)

straight_fence_polygon = Polygon(straight_coords)
assert straight_fence_polygon.area < straight_dilated_footprint.area, (
    "the inset fence line must enclose strictly less area than the dilated footprint -- "
    "confirms the inset actually moved the line inward, not just returning the dilated "
    "footprint's own boundary unchanged"
)
assert straight_dilated_footprint.buffer(1e-9).contains(straight_fence_polygon), (
    "the inset fence polygon must sit inside the dilated footprint's own outer edge"
)
print(
    "find_road_corridor_fencing(): a straight path returns a single closed LineString, "
    "genuinely inset inside the dilated footprint's own outer edge."
)


# --- 3. a deliberately narrow/pinched synthetic path where the negative buffer empties out
#        entirely -> falls back to the dilated footprint's own outer edge, not an error/drop ---

# A very fine DEM resolution (0.1m) makes ROAD_CORRIDOR_FENCE_DILATION_CELLS's own single-cell
# dilation produce a genuinely thin strip (3 cells wide perpendicular to the path, 0.3m total)
# -- thinner than ROAD_FENCE_LINE_INSET_METERS's own current 2.0m inset, so the negative
# buffer has nowhere to go and empties out completely.
NARROW_ROAD_FENCE_DEM = {
    "array": np.zeros((20, 20), dtype=np.float32),
    "resolution_meters": (0.1, 0.1),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": UTM_CRS,
}
narrow_path_cells = [(10, c) for c in range(3, 17)]

narrow_mask = np.zeros(NARROW_ROAD_FENCE_DEM["array"].shape, dtype=bool)
for r, c in narrow_path_cells:
    narrow_mask[r, c] = True
narrow_dilated_mask = binary_dilate(narrow_mask, ROAD_CORRIDOR_FENCE_DILATION_CELLS)
narrow_dilated_footprint = cell_union_footprint(NARROW_ROAD_FENCE_DEM, narrow_dilated_mask)
narrow_would_be_inset = narrow_dilated_footprint.buffer(-ROAD_FENCE_LINE_INSET_METERS)
assert narrow_would_be_inset.is_empty, (
    "test setup must genuinely produce an empty negative-buffer result for this to be a real "
    "test of the fallback path"
)

narrow_fence_line = find_road_corridor_fencing(NARROW_ROAD_FENCE_DEM, narrow_path_cells)
assert narrow_fence_line is not None, "the fallback must still return a real fence line, not None"
narrow_fence_polygon = Polygon(narrow_fence_line.coords)
assert narrow_fence_polygon.equals(narrow_dilated_footprint), (
    "on a narrow/pinched stretch where the inset empties out, the fallback must return the "
    "dilated footprint's own outer edge unchanged, not a broken/empty result"
)
print(
    "find_road_corridor_fencing(): a narrow/pinched path where the inset would empty out "
    "falls back to the dilated footprint's own outer edge."
)


# =====================================================================
# find_existing_farm_road_fencing(): pure geometric core, no network I/O -- same "pure core is
# independently testable" pattern as find_stream_exclusion_fencing() above. Real WGS84
# coordinates (this module's own PROPERTY_BOUNDARY) are reused so the reprojection this function
# does internally (EPSG:4326 -> UTM_CRS) behaves like it would against a real property, rather
# than arbitrary "UTM-like" numbers standing in for lon/lat (which find_boundary_fencing() above
# can get away with since IT never reprojects anything itself).
# =====================================================================

_property_boundary_polygon_wgs84 = Polygon(PROPERTY_BOUNDARY)
_inside_point = _property_boundary_polygon_wgs84.representative_point()  # guaranteed inside

FARM_ROAD_BOUNDARY_XS, FARM_ROAD_BOUNDARY_YS = warp_transform(
    "EPSG:4326", UTM_CRS, [pt[0] for pt in PROPERTY_BOUNDARY], [pt[1] for pt in PROPERTY_BOUNDARY]
)
FARM_ROAD_BOUNDARY_POLYGON_UTM = Polygon(zip(FARM_ROAD_BOUNDARY_XS, FARM_ROAD_BOUNDARY_YS))


def _farm_road_feature(name: str, coordinates: list[tuple[float, float]]) -> dict:
    return {"name": name, "geometry": {"type": "LineString", "coordinates": coordinates}}


# --- 1. a road entirely outside the boundary -> [] (clips to empty, correctly produces nothing) ---

OUTSIDE_ROAD = _farm_road_feature("Far Away Road", [(-79.5, 41.0), (-79.4, 41.1)])
outside_boundary_wgs84 = shape(transform_geom(UTM_CRS, "EPSG:4326", mapping(FARM_ROAD_BOUNDARY_POLYGON_UTM)))
assert not outside_boundary_wgs84.intersects(shape(OUTSIDE_ROAD["geometry"])), (
    "test setup must genuinely place this road outside the boundary for this to be a real test"
)

outside_entries = find_existing_farm_road_fencing([OUTSIDE_ROAD], FARM_ROAD_BOUNDARY_POLYGON_UTM, UTM_CRS)
assert outside_entries == [], f"a road entirely outside the boundary should produce no entries, got {outside_entries}"
print("find_existing_farm_road_fencing(): a road entirely outside the boundary returns [].")


# --- 2. a road partially crossing the boundary -> one entry, derived only from the on-parcel
#        clipped portion (not the full original line) ---

_outside_point = (_inside_point.x - 0.03, _inside_point.y)
CROSSING_ROAD = _farm_road_feature("Crossing Road", [_outside_point, (_inside_point.x, _inside_point.y)])
crossing_line_wgs84 = shape(CROSSING_ROAD["geometry"])
assert crossing_line_wgs84.intersects(outside_boundary_wgs84) and not outside_boundary_wgs84.contains(
    crossing_line_wgs84
), "test setup must genuinely have this road cross the boundary (partly in, partly out) for a real test"

crossing_entries = find_existing_farm_road_fencing([CROSSING_ROAD], FARM_ROAD_BOUNDARY_POLYGON_UTM, UTM_CRS)
assert len(crossing_entries) == 1, f"a road partially crossing the boundary should produce exactly 1 entry, got {len(crossing_entries)}"
crossing_fence_line_utm = shape(transform_geom("EPSG:4326", UTM_CRS, crossing_entries[0]["geometry_wgs84"]))
full_road_line_utm = shape(transform_geom("EPSG:4326", UTM_CRS, CROSSING_ROAD["geometry"]))
on_parcel_portion_utm = FARM_ROAD_BOUNDARY_POLYGON_UTM.intersection(full_road_line_utm)
assert on_parcel_portion_utm.length < full_road_line_utm.length, (
    "test setup must genuinely clip to a shorter on-parcel sub-segment for this to be a real test"
)

# The output fence line is the buffer OUTLINE of just the on-parcel portion -- it should match
# a buffer built directly from that clipped sub-segment, and must NOT match a buffer built from
# the FULL original (mostly off-parcel) line, confirming only the clipped portion fed the buffer.
crossing_fence_polygon_utm = Polygon(crossing_fence_line_utm.coords)
expected_on_parcel_buffer = on_parcel_portion_utm.buffer(EXISTING_FARM_ROAD_FENCE_BUFFER_METERS)
full_line_buffer = full_road_line_utm.buffer(EXISTING_FARM_ROAD_FENCE_BUFFER_METERS)
assert full_line_buffer.area > expected_on_parcel_buffer.area, (
    "test setup must genuinely make the full line's own buffer larger than the clipped portion's "
    "own buffer for this to be a real test"
)
assert crossing_fence_polygon_utm.equals_exact(expected_on_parcel_buffer, 1e-6) or crossing_fence_polygon_utm.symmetric_difference(
    expected_on_parcel_buffer
).area < 1e-3, "the fence polygon must match a buffer built from the clipped on-parcel portion alone"
assert crossing_fence_polygon_utm.area < full_line_buffer.area, (
    "the fence polygon must NOT match (and must enclose less area than) a buffer built from the "
    "full original line -- confirms only the on-parcel clipped portion fed the buffer, not the "
    "full (mostly off-parcel) line"
)
print(
    "find_existing_farm_road_fencing(): a road partially crossing the boundary returns 1 entry "
    "derived only from the on-parcel clipped portion."
)


# --- 3. multiple on-parcel road segments -> one entry per segment ---

_second_outside_point = (_inside_point.x, _inside_point.y + 0.03)
SECOND_CROSSING_ROAD = _farm_road_feature(
    "Second Crossing Road", [_second_outside_point, (_inside_point.x, _inside_point.y)]
)
multi_entries = find_existing_farm_road_fencing(
    [CROSSING_ROAD, SECOND_CROSSING_ROAD], FARM_ROAD_BOUNDARY_POLYGON_UTM, UTM_CRS
)
assert len(multi_entries) == 2, f"two separate on-parcel road segments should produce 2 entries, got {len(multi_entries)}"
assert {e["source_label"] for e in multi_entries} == {"Crossing Road", "Second Crossing Road"}
print("find_existing_farm_road_fencing(): multiple on-parcel road segments return one entry per segment.")


# --- existing_farm_road_fencing_to_geojson: schema-valid, correct layer/fence_type ---

farm_road_geojson = existing_farm_road_fencing_to_geojson(crossing_entries)
validate_feature_collection(farm_road_geojson)
farm_road_feature_out = farm_road_geojson["features"][0]
assert farm_road_feature_out["properties"]["layer"] == "perimeter_fencing"
assert farm_road_feature_out["properties"]["fence_type"] == "existing_farm_road_exclusion"
assert farm_road_feature_out["properties"]["confidence"] == "medium"
print("existing_farm_road_fencing_to_geojson() output is schema-valid, layer='perimeter_fencing'.")


# --- road_corridor_fencing_to_geojson: None fence_line -> empty FeatureCollection ---

empty_road_corridor_geojson = road_corridor_fencing_to_geojson(None)
validate_feature_collection(empty_road_corridor_geojson)
assert empty_road_corridor_geojson["features"] == [], "None fence_line should produce zero features, not an error"
print("road_corridor_fencing_to_geojson(): None fence_line produces an empty, schema-valid FeatureCollection.")


# =====================================================================
# find_water_zone_fencing(): pure geometric core, purely synthetic geometry, no network
# involved -- same "pure core is independently testable" pattern as every other pure core
# in this file. A plain shapely box stands in for a real render_fill_polygon_utm (an
# already-computed real fill footprint -- water_candidate_zones.py's own convex-hull-and-
# intersect construction, not raw DEM cells, so no DEM/cell-mask fixture is needed here).
# =====================================================================

WATER_ZONE_TEST_POLYGON_UTM = box(0, 0, 40, 30)  # a plain 40m x 30m render_fill_polygon_utm stand-in


# --- 1. None input -> returns None ---

assert find_water_zone_fencing(None) is None
print("find_water_zone_fencing(): None input returns None.")


# --- 2. a simple synthetic polygon -> a closed LineString, every point OUTSIDE the source
#        polygon (buffer-direction sanity check -- confirms this buffers OUTWARD, not inward) ---

water_fence_line = find_water_zone_fencing(WATER_ZONE_TEST_POLYGON_UTM)
assert water_fence_line is not None and water_fence_line.geom_type == "LineString"
water_fence_coords = list(water_fence_line.coords)
assert water_fence_coords[0] == water_fence_coords[-1], "water zone fence line must be a closed ring"
assert all(
    not WATER_ZONE_TEST_POLYGON_UTM.contains(Point(pt)) for pt in water_fence_coords
), "every point on the water zone fence line must sit OUTSIDE the source polygon -- an inward buffer would put points inside/on it instead"
water_fence_distances = [Point(pt).distance(WATER_ZONE_TEST_POLYGON_UTM) for pt in water_fence_coords]
assert all(abs(d - WATER_ZONE_FENCE_BUFFER_METERS) < 1e-6 for d in water_fence_distances), (
    f"the fence line should sit exactly {WATER_ZONE_FENCE_BUFFER_METERS}m outside the source polygon, "
    f"got distances ranging {min(water_fence_distances)}-{max(water_fence_distances)}"
)
print(
    "find_water_zone_fencing(): a synthetic polygon returns a closed LineString sitting "
    f"~{WATER_ZONE_FENCE_BUFFER_METERS}m outside (never inside) the source polygon."
)


# --- water_zone_fencing_to_geojson: schema-valid, correct layer/fence_type ---

water_zone_geojson = water_zone_fencing_to_geojson(water_fence_line)
validate_feature_collection(water_zone_geojson)
water_zone_feature_out = water_zone_geojson["features"][0]
assert water_zone_feature_out["properties"]["layer"] == "perimeter_fencing"
assert water_zone_feature_out["properties"]["fence_type"] == "water_zone_exclusion"
assert water_zone_feature_out["properties"]["confidence"] == "high"
print("water_zone_fencing_to_geojson() output is schema-valid, layer='perimeter_fencing'.")


# --- water_zone_fencing_to_geojson: None fence_line -> empty FeatureCollection ---

empty_water_zone_geojson = water_zone_fencing_to_geojson(None)
validate_feature_collection(empty_water_zone_geojson)
assert empty_water_zone_geojson["features"] == [], "None fence_line should produce zero features, not an error"
print("water_zone_fencing_to_geojson(): None fence_line produces an empty, schema-valid FeatureCollection.")


# =====================================================================
# find_tree_zone_fencing(): pure geometric core, purely synthetic geometry, no network
# involved -- same buffer-and-outline recipe as find_water_zone_fencing() above, applied
# independently to a whole LIST of polygons (tree_zone_candidates.py has no selection
# step, so every candidate gets fenced -- see fencing.py's own module docstring).
# =====================================================================

TREE_ZONE_TEST_POLYGON_A_UTM = box(0, 0, 20, 20)
TREE_ZONE_TEST_POLYGON_B_UTM = box(100, 100, 130, 115)  # a second, well-separated candidate


# --- 1. empty list -> returns [] ---

assert find_tree_zone_fencing([]) == []
print("find_tree_zone_fencing(): an empty list returns [].")


# --- 2. multiple synthetic polygons -> one LineString per input, same order, each
#        confirmed outside its own source polygon ---

tree_fence_lines = find_tree_zone_fencing([TREE_ZONE_TEST_POLYGON_A_UTM, TREE_ZONE_TEST_POLYGON_B_UTM])
assert len(tree_fence_lines) == 2, f"expected 1 fence line per input polygon, got {len(tree_fence_lines)}"

for source_polygon, fence_line in zip(
    [TREE_ZONE_TEST_POLYGON_A_UTM, TREE_ZONE_TEST_POLYGON_B_UTM], tree_fence_lines
):
    assert fence_line.geom_type == "LineString"
    fence_coords = list(fence_line.coords)
    assert fence_coords[0] == fence_coords[-1], "each tree zone fence line must be a closed ring"
    assert all(
        not source_polygon.contains(Point(pt)) for pt in fence_coords
    ), "every point on a tree zone fence line must sit OUTSIDE its own source polygon"
    fence_distances = [Point(pt).distance(source_polygon) for pt in fence_coords]
    assert all(abs(d - TREE_ZONE_FENCE_BUFFER_METERS) < 1e-6 for d in fence_distances), (
        f"the fence line should sit exactly {TREE_ZONE_FENCE_BUFFER_METERS}m outside its own source "
        f"polygon, got distances ranging {min(fence_distances)}-{max(fence_distances)}"
    )

# Confirm the two returned fence lines actually correspond to their own separate source
# polygons in the SAME order given -- the first fence line should sit near polygon A's own
# location, not polygon B's (they're far apart, at (0-20,0-20) vs (100-130,100-115)).
assert Point(tree_fence_lines[0].coords[0]).distance(TREE_ZONE_TEST_POLYGON_A_UTM) < 5.0
assert Point(tree_fence_lines[1].coords[0]).distance(TREE_ZONE_TEST_POLYGON_B_UTM) < 5.0
print(
    "find_tree_zone_fencing(): multiple synthetic polygons return one LineString per input, "
    "in the same order, each confirmed outside its own source polygon."
)


# --- 3. None entries mixed into the input list -> skipped, not erroring ---

mixed_tree_fence_lines = find_tree_zone_fencing([TREE_ZONE_TEST_POLYGON_A_UTM, None, TREE_ZONE_TEST_POLYGON_B_UTM])
assert len(mixed_tree_fence_lines) == 2, (
    f"a None entry in the input list should be skipped, not raise or produce a spurious entry -- "
    f"expected 2 fence lines, got {len(mixed_tree_fence_lines)}"
)
print("find_tree_zone_fencing(): None entries mixed into the input list are skipped, not erroring.")


# --- tree_zone_fencing_to_geojson: schema-valid, correct layer/fence_type, 1-based candidate_rank ---

tree_zone_geojson = tree_zone_fencing_to_geojson(tree_fence_lines)
validate_feature_collection(tree_zone_geojson)
assert len(tree_zone_geojson["features"]) == 2
for i, feature in enumerate(tree_zone_geojson["features"], start=1):
    assert feature["properties"]["layer"] == "perimeter_fencing"
    assert feature["properties"]["fence_type"] == "tree_zone_exclusion"
    assert feature["properties"]["confidence"] == "high"
    assert feature["properties"]["candidate_rank"] == i
print(
    "tree_zone_fencing_to_geojson() output is schema-valid, layer='perimeter_fencing', "
    "each feature carrying its own 1-based candidate_rank."
)


# --- tree_zone_fencing_to_geojson: empty list -> empty FeatureCollection ---

empty_tree_zone_geojson = tree_zone_fencing_to_geojson([])
validate_feature_collection(empty_tree_zone_geojson)
assert empty_tree_zone_geojson["features"] == [], "an empty fence_lines list should produce zero features, not an error"
print("tree_zone_fencing_to_geojson(): an empty list produces an empty, schema-valid FeatureCollection.")


# =====================================================================
# identify_fencing(): HIGH-LEVEL overrides (selected_road_corridor/selected_water_zone/
# tree_zone_patches, matching pipeline_context.py's own field names/shapes) and PASS-THROUGH-ONLY
# overrides (boundary_polygon_utm/production_areas/valleys). All scenarios below reuse no_stream_water/
# TEST_DEM/_fake_no_canopy from the identify_fencing() section above, and pass
# farm_road_features=[] to stay deterministic/offline (same reasoning as that section's own comment).
# =====================================================================


def _must_not_be_called(name):
    def _raise(*args, **kwargs):
        raise AssertionError(f"{name} must not be called when the matching high-level/low-level override "
                              "already supplies what it would have computed")

    return _raise


# --- Scenario 1: high-level overrides supplied -> identify_road_corridor_candidates()/
# fetch_and_select_optimal_water_zone()/identify_tree_zone_candidates() are each called ZERO times,
# and the low-level values used are derived directly from the high-level dicts, not self-computed ---

HIGH_LEVEL_ROAD_CORRIDOR = {"cells": [(5, 5), (5, 6), (5, 7)]}
HIGH_LEVEL_WATER_POLYGON = box(100, 100, 110, 110)
HIGH_LEVEL_WATER_ZONE = {"render_fill_polygon_utm": HIGH_LEVEL_WATER_POLYGON}
HIGH_LEVEL_TREE_POLYGON_A = box(200, 200, 210, 210)
HIGH_LEVEL_TREE_POLYGON_B = box(220, 220, 230, 230)
HIGH_LEVEL_TREE_ZONE_PATCHES = [
    {"render_fill_polygon_utm": HIGH_LEVEL_TREE_POLYGON_A},
    {"render_fill_polygon_utm": HIGH_LEVEL_TREE_POLYGON_B},
]

_captured_low_level = {}


def _capture_road_fencing(dem_arg, cells_arg, **kwargs):
    _captured_low_level["road_cells"] = cells_arg
    return None


def _capture_water_fencing(polygon_arg, **kwargs):
    _captured_low_level["water_polygon"] = polygon_arg
    return None


def _capture_tree_fencing(polygons_arg, **kwargs):
    _captured_low_level["tree_polygons"] = polygons_arg
    return []


with (
    mock_patch.object(pa, "get_canopy_height_for_boundary", _fake_no_canopy),
    mock_patch.object(fencing, "identify_road_corridor_candidates", _must_not_be_called("identify_road_corridor_candidates")),
    mock_patch.object(fencing, "fetch_and_select_optimal_water_zone", _must_not_be_called("fetch_and_select_optimal_water_zone")),
    mock_patch.object(fencing, "identify_tree_zone_candidates", _must_not_be_called("identify_tree_zone_candidates")),
    mock_patch.object(fencing, "find_road_corridor_fencing", _capture_road_fencing),
    mock_patch.object(fencing, "find_water_zone_fencing", _capture_water_fencing),
    mock_patch.object(fencing, "find_tree_zone_fencing", _capture_tree_fencing),
):
    high_level_result = identify_fencing(
        PROPERTY_BOUNDARY,
        water_features_geojson=no_stream_water,
        dem=TEST_DEM,
        farm_road_features=[],
        selected_road_corridor=HIGH_LEVEL_ROAD_CORRIDOR,
        selected_water_zone=HIGH_LEVEL_WATER_ZONE,
        tree_zone_patches=HIGH_LEVEL_TREE_ZONE_PATCHES,
    )
validate_feature_collection(high_level_result["fencing_geojson"])
assert _captured_low_level["road_cells"] is HIGH_LEVEL_ROAD_CORRIDOR["cells"], (
    "selected_road_corridor_cells must be derived directly from selected_road_corridor['cells']"
)
assert _captured_low_level["water_polygon"] is HIGH_LEVEL_WATER_POLYGON, (
    "selected_water_zone_render_fill_polygon_utm must be derived directly from "
    "selected_water_zone['render_fill_polygon_utm']"
)
assert _captured_low_level["tree_polygons"] == [HIGH_LEVEL_TREE_POLYGON_A, HIGH_LEVEL_TREE_POLYGON_B]
assert _captured_low_level["tree_polygons"][0] is HIGH_LEVEL_TREE_POLYGON_A
assert _captured_low_level["tree_polygons"][1] is HIGH_LEVEL_TREE_POLYGON_B
print(
    "identify_fencing(): high-level selected_road_corridor/selected_water_zone/tree_zone_patches overrides "
    "derive the low-level values directly (identity-checked) -- identify_road_corridor_candidates()/"
    "fetch_and_select_optimal_water_zone()/identify_tree_zone_candidates() are called zero times."
)


# --- Scenario 2 (regression): none of the six new overrides supplied -> all three self-compute
# calls still run exactly once each, same as pre-branch behavior, producing identical output on the
# existing fixture (no_stream_water/TEST_DEM/farm_road_features=[]) ---

import road_corridors as _road_corridors_module

_self_compute_call_counts = {"road": 0, "water": 0, "tree": 0}


def _counting_road_corridor(*args, **kwargs):
    _self_compute_call_counts["road"] += 1
    return _road_corridors_module.identify_road_corridor_candidates(*args, **kwargs)


def _counting_water_zone(*args, **kwargs):
    _self_compute_call_counts["water"] += 1
    return _fake_no_water_zone(*args, **kwargs)


def _counting_tree_zone(*args, **kwargs):
    _self_compute_call_counts["tree"] += 1
    return {"patches": []}


with (
    mock_patch.object(pa, "get_canopy_height_for_boundary", _fake_no_canopy),
    mock_patch.object(fencing, "identify_road_corridor_candidates", _counting_road_corridor),
    mock_patch.object(fencing, "fetch_and_select_optimal_water_zone", _counting_water_zone),
    mock_patch.object(fencing, "identify_tree_zone_candidates", _counting_tree_zone),
):
    regression_result = identify_fencing(
        PROPERTY_BOUNDARY,
        water_features_geojson=no_stream_water,
        dem=TEST_DEM,
        farm_road_features=[],
    )
validate_feature_collection(regression_result["fencing_geojson"])
assert _self_compute_call_counts == {"road": 1, "water": 1, "tree": 1}, (
    f"with none of the six new overrides supplied, each self-compute call must still run exactly once "
    f"(unchanged pre-branch behavior), got {_self_compute_call_counts}"
)
regression_layers = sorted(f["properties"]["layer"] for f in regression_result["fencing_geojson"]["features"])
assert regression_layers == no_stream_layers, (
    f"identical (boundary_coordinates, dem, no new overrides) input must produce the same fencing layers "
    f"pre- and post-branch, got {regression_layers} vs {no_stream_layers}"
)
assert regression_result["segment_count"] == no_stream_result["segment_count"]
print(
    "identify_fencing(): with none of the six new overrides supplied, all three self-compute calls still "
    "run exactly once each and produce identical output to pre-branch behavior on the existing fixture."
)


# --- Scenario 3: boundary_polygon_utm/production_areas/valleys supplied WITHOUT the three
# high-level dicts -> all three self-compute calls receive those three as kwargs (identity checks) ---

SENTINEL_BOUNDARY_POLYGON_UTM = box(-1000, -1000, 1000, 1000)
SENTINEL_PRODUCTION_AREAS = [{"id": "prod-1"}]
SENTINEL_VALLEYS = [{"id": "valley-1"}]

_captured_passthrough_kwargs = {}


def _capture_road_corridor_kwargs(*args, **kwargs):
    _captured_passthrough_kwargs["road"] = kwargs
    return {"selected_road_corridor": None}


def _capture_water_zone_kwargs(*args, **kwargs):
    _captured_passthrough_kwargs["water"] = kwargs
    return None


def _capture_tree_zone_kwargs(*args, **kwargs):
    _captured_passthrough_kwargs["tree"] = kwargs
    return {"patches": []}


with (
    mock_patch.object(pa, "get_canopy_height_for_boundary", _fake_no_canopy),
    mock_patch.object(fencing, "identify_road_corridor_candidates", _capture_road_corridor_kwargs),
    mock_patch.object(fencing, "fetch_and_select_optimal_water_zone", _capture_water_zone_kwargs),
    mock_patch.object(fencing, "identify_tree_zone_candidates", _capture_tree_zone_kwargs),
):
    passthrough_result = identify_fencing(
        PROPERTY_BOUNDARY,
        water_features_geojson=no_stream_water,
        dem=TEST_DEM,
        farm_road_features=[],
        boundary_polygon_utm=SENTINEL_BOUNDARY_POLYGON_UTM,
        production_areas=SENTINEL_PRODUCTION_AREAS,
        valleys=SENTINEL_VALLEYS,
    )
validate_feature_collection(passthrough_result["fencing_geojson"])

for call_name in ("road", "water", "tree"):
    call_kwargs = _captured_passthrough_kwargs[call_name]
    assert call_kwargs["boundary_polygon_utm"] is SENTINEL_BOUNDARY_POLYGON_UTM, (
        f"{call_name} self-compute fallback call must receive the caller's own boundary_polygon_utm, not "
        "re-derive it"
    )
    assert call_kwargs["production_areas"] is SENTINEL_PRODUCTION_AREAS, (
        f"{call_name} self-compute fallback call must receive the caller's own production_areas, not "
        "re-derive it"
    )
    assert call_kwargs["valleys"] is SENTINEL_VALLEYS, (
        f"{call_name} self-compute fallback call must receive the caller's own valleys, not re-derive it"
    )
print(
    "identify_fencing(): boundary_polygon_utm/production_areas/valleys supplied without the three "
    "high-level dicts are forwarded (identity-checked) into all three self-compute fallback calls -- "
    "closing the nested, un-deduped-chain gap for standalone/non-context callers too."
)


# --- Scenario 4: low-level override still wins when BOTH a low-level and high-level override are
# supplied for the same thing (selected_water_zone_render_fill_polygon_utm AND selected_water_zone) --
# no self-compute AND no extraction from the high-level dict (which is deliberately missing the key
# extraction would need, so any accidental extraction attempt would raise KeyError, not silently pass) ---

LOW_LEVEL_WATER_POLYGON = box(300, 300, 310, 310)
HIGH_LEVEL_WATER_ZONE_MISSING_KEY = {}  # deliberately no 'render_fill_polygon_utm' key

_captured_precedence = {}


def _capture_water_fencing_precedence(polygon_arg, **kwargs):
    _captured_precedence["water_polygon"] = polygon_arg
    return None


with (
    mock_patch.object(pa, "get_canopy_height_for_boundary", _fake_no_canopy),
    mock_patch.object(fencing, "fetch_and_select_optimal_water_zone", _must_not_be_called("fetch_and_select_optimal_water_zone")),
    mock_patch.object(fencing, "find_water_zone_fencing", _capture_water_fencing_precedence),
):
    precedence_result = identify_fencing(
        PROPERTY_BOUNDARY,
        water_features_geojson=no_stream_water,
        dem=TEST_DEM,
        farm_road_features=[],
        tree_zone_render_fill_polygons_utm=[],
        selected_water_zone_render_fill_polygon_utm=LOW_LEVEL_WATER_POLYGON,
        selected_water_zone=HIGH_LEVEL_WATER_ZONE_MISSING_KEY,
    )
validate_feature_collection(precedence_result["fencing_geojson"])
assert _captured_precedence["water_polygon"] is LOW_LEVEL_WATER_POLYGON, (
    "the low-level selected_water_zone_render_fill_polygon_utm override must win over the high-level "
    "selected_water_zone override when both are supplied"
)
print(
    "identify_fencing(): when both a low-level and high-level override are supplied for the same value "
    "(water zone), the low-level override wins as-is -- no self-compute, no extraction from the "
    "high-level dict (proven by a deliberately key-less high-level dict not raising)."
)


print("\nAll fencing checks passed.")
