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
all. identify_fencing() (the full-pipeline fetch-and-wrap entry point)
additionally needs production_area.get_canopy_height_for_boundary()
mocked (same pattern test_production_area_ceiling.py already uses for its
own full-pipeline canopy-gate scenarios) to stay offline, because its
OWN self-compute fallbacks (road/water/tree candidates) still carry a
canopy fetch -- identify_boundary_fencing() itself no longer fetches
canopy at all (the boundary fence no longer routes around canopy).
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
    BOUNDARY_FENCE_MARGIN_METERS,
    BOUNDARY_FENCE_MIN_SEGMENT_ACRES,
    STREAM_EXCLUSION_BUFFER_METERS,
    TREE_ZONE_FENCE_BUFFER_METERS,
    WATER_ZONE_FENCE_BUFFER_METERS,
    boundary_fencing_to_geojson,
    find_boundary_fencing,
    find_stream_exclusion_fencing,
    find_tree_zone_fencing,
    find_water_zone_fencing,
    identify_boundary_fencing,
    identify_fencing,
    stream_exclusion_fencing_to_geojson,
    tree_zone_fencing_to_geojson,
    water_zone_fencing_to_geojson,
)

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
# boundary_polygon_utm; developed-footprint/zone inputs are hand-built
# shapely geometry.
#
# Canopy is no longer an input to find_boundary_fencing() at all (a fence
# through wooded ground is fine in practice, so the boundary fence no
# longer routes around it) -- the earlier canopy-difference tests
# (single-notch, interior-hole-ignored, end-to-end-split, tiny-sliver-
# dropped, close-canopy-neck) tested behavior that no longer exists and
# have been removed. The bare-property degenerate case (case 1 below) and
# the developed-footprint behavior (cases 7-11 further below, including the
# convex-hull regression) are what this function is tested on now.
# =====================================================================

from shapely.geometry import LineString as _LineString

TEST_BOUNDARY_UTM = box(0, 0, 100, 100)  # 10,000 sq m, ~2.47 acres

# Shorthand for "no developed footprint, no zones" -- the empty/None values that
# collapse find_boundary_fencing() back to the plain drawn-boundary loop.
NO_DEVELOPED_FOOTPRINT = dict(
    production_zone_polygons_utm=[],
    structure_site_polygon_utm=None,
    road_corridor_cell_footprint_polygon_utm=None,
    water_zone_polygon_utm=None,
    tree_zone_polygons_utm=[],
)


# --- 1. bare property (no developed footprint, no zones) -> the plain-wrap case: the boundary's own ring ---

bare_rings = find_boundary_fencing(TEST_BOUNDARY_UTM, **NO_DEVELOPED_FOOTPRINT)
assert len(bare_rings) == 1, f"a bare property should return exactly 1 ring, got {len(bare_rings)}"
assert bare_rings[0].equals(Polygon(TEST_BOUNDARY_UTM.exterior).exterior), (
    "with no developed footprint and no zones, the returned ring must match the boundary's own exterior "
    "ring exactly (the degenerate plain-wrap fallback -- no convex-hull/clip re-noding of its coordinates)"
)
print("find_boundary_fencing(): a bare property returns the boundary's own unmodified exterior ring.")


# =====================================================================
# find_boundary_fencing(): this branch's developed-footprint rewrite (cases 7-10). The boundary
# fence now protects the DEVELOPED FOOTPRINT (production + structure + road corridor path),
# buffered by BOUNDARY_FENCE_MARGIN_METERS and clipped to the drawn boundary, with water/tree
# zones unioned in at their OWN fence buffer. All still purely synthetic geometry on the same
# TEST_BOUNDARY_UTM square -- no DEM/CRS/network.
# =====================================================================

BOUNDARY_POLYGON = Polygon(TEST_BOUNDARY_UTM.exterior)


# --- 7. developed footprint smaller than the drawn boundary, no zones nearby -> the fence ring is
#        MEANINGFULLY smaller than the boundary's own ring (not just clipped/identical): the core
#        "don't waste fencing on unused land" case this whole change exists for ---

developed_zone = box(30, 30, 70, 70)  # a 40x40m developed core well inside the 100x100 boundary
smaller_rings = find_boundary_fencing(
    TEST_BOUNDARY_UTM,
    production_zone_polygons_utm=[developed_zone],
    structure_site_polygon_utm=None,
    road_corridor_cell_footprint_polygon_utm=None,
    water_zone_polygon_utm=None,
    tree_zone_polygons_utm=[],
)
assert len(smaller_rings) == 1, f"a single developed core should return exactly 1 ring, got {len(smaller_rings)}"
smaller_ring_polygon = Polygon(smaller_rings[0])
# The developed core (1,600 sq m) buffered by BOUNDARY_FENCE_MARGIN_METERS (5m) is roughly a
# 50x50m rounded square (~2,500 sq m) -- far smaller than the 10,000 sq m boundary. Assert the ring
# genuinely shrank to the developed footprint, not merely got clipped to (or left equal to) the boundary.
assert smaller_ring_polygon.area < BOUNDARY_POLYGON.area * 0.5, (
    "the boundary fence must enclose only the margined DEVELOPED footprint, not the full drawn boundary -- "
    f"expected the ring's area ({smaller_ring_polygon.area:.1f} sq m) to be well under half the boundary's "
    f"({BOUNDARY_POLYGON.area:.1f} sq m)"
)
assert not smaller_ring_polygon.equals(BOUNDARY_POLYGON), "the developed-footprint ring must not equal the boundary ring"
# It should also sit comfortably inside the boundary (margined core never reaches the boundary edge here).
assert BOUNDARY_POLYGON.contains(smaller_ring_polygon), "the developed-footprint ring must sit inside the drawn boundary"
print(
    "find_boundary_fencing(): a developed footprint smaller than the drawn boundary yields a fence ring "
    f"meaningfully smaller than the boundary ({smaller_ring_polygon.area:.0f} vs {BOUNDARY_POLYGON.area:.0f} "
    "sq m) -- fencing follows the developed land, not the full parcel."
)


# --- 8. a water zone well INSIDE margined_core with real clearance -> the boundary fence ring is
#        UNCHANGED by that zone (it stays its own independent loop), and the zone's own fence ring
#        is a genuinely separate loop nested inside it: the "stays two independent loops" case ---

developed_zone_8 = box(20, 20, 80, 80)  # margined by 5m -> ~ (15,15)-(85,85)
interior_water_zone = box(40, 40, 50, 50)  # buffered by 2.5m -> (37.5,37.5)-(52.5,52.5), well inside margined_core

ring_without_zone = find_boundary_fencing(
    TEST_BOUNDARY_UTM,
    production_zone_polygons_utm=[developed_zone_8],
    structure_site_polygon_utm=None,
    road_corridor_cell_footprint_polygon_utm=None,
    water_zone_polygon_utm=None,
    tree_zone_polygons_utm=[],
)
ring_with_interior_zone = find_boundary_fencing(
    TEST_BOUNDARY_UTM,
    production_zone_polygons_utm=[developed_zone_8],
    structure_site_polygon_utm=None,
    road_corridor_cell_footprint_polygon_utm=None,
    water_zone_polygon_utm=interior_water_zone,
    tree_zone_polygons_utm=[],
)
assert len(ring_without_zone) == 1 and len(ring_with_interior_zone) == 1
# A zone fully inside margined_core with real clearance changes the union's outer boundary by NOTHING --
# so the boundary fence ring is byte-for-byte identical with and without that interior zone supplied.
assert Polygon(ring_with_interior_zone[0]).equals(Polygon(ring_without_zone[0])), (
    "a water zone sitting well inside the margined developed core (with real clearance) must NOT merge into "
    "or alter the boundary fence ring -- the boundary fence stays its own independent loop"
)
# And the zone's own individually-drawn fence loop is present and genuinely separate: a distinct ring,
# strictly nested inside the boundary fence ring, sharing none of its boundary.
interior_water_fence = find_water_zone_fencing(interior_water_zone)
assert interior_water_fence is not None
assert Polygon(ring_with_interior_zone[0]).contains(Polygon(interior_water_fence)), (
    "the interior water zone's own fence loop must sit strictly inside the (separate) boundary fence loop"
)
assert not Polygon(ring_with_interior_zone[0]).exterior.intersects(Polygon(interior_water_fence).exterior), (
    "the two loops must be genuinely separate -- the boundary fence ring and the zone's own fence ring share "
    "no boundary (no merge)"
)
print(
    "find_boundary_fencing(): a water zone well inside the margined developed core leaves the boundary fence "
    "ring unchanged AND stays its own separate, nested fence loop -- two genuinely independent loops."
)


# --- 9. a water zone at the true OUTER edge of the developed footprint, forming part of the convex hull ->
#        the boundary fence ring's coordinates along that stretch are IDENTICAL to the zone's own buffered
#        fence edge (a real geometric equality, not just "a ring was returned"): the "meets up, uses it,
#        continues" case, re-verified against hulled geometry ---
#
# NOTE (post-hull redesign): the pre-hull version put a NARROW water zone protruding above a WIDE developed
# core, so the zone's vertical sides were the outer edge and coincided with the raw union. Under the convex
# hull (step 4) those vertical sides are no longer on the outer boundary -- the hull cuts a diagonal chord
# from the zone's top corners to the wider core below them -- so that setup would no longer exercise a
# shared stretch. Reworked so the water zone is the DOMINANT OUTER feature (wider than the developed core
# and sitting on top of it): its top and upper side edges ARE hull edges, so the boundary fence runs on the
# zone's own buffered outline there. And because the hull reuses the zone buffer's OWN vertices verbatim
# along that stretch (no morphological-opening re-approximation anymore -- that step is gone), the
# coincidence is now EXACT, not merely near-zero.

developed_zone_9 = box(30, 25, 70, 50)  # the narrower developed core, margined by 5m -> ~ (25,20)-(75,55)
# A water zone WIDER than the developed core, sitting on top of it -- its 2.5m buffer (-> x 12.5..87.5,
# y 47.5..72.5) is the dominant outer feature, so its top and upper sides become hull edges.
edge_water_zone = box(15, 50, 85, 70)

edge_rings = find_boundary_fencing(
    TEST_BOUNDARY_UTM,
    production_zone_polygons_utm=[developed_zone_9],
    structure_site_polygon_utm=None,
    road_corridor_cell_footprint_polygon_utm=None,
    water_zone_polygon_utm=edge_water_zone,
    tree_zone_polygons_utm=[],
)
assert len(edge_rings) == 1, f"expected a single merged loop, got {len(edge_rings)}"
edge_boundary_ring = edge_rings[0]
edge_water_fence = find_water_zone_fencing(edge_water_zone)  # the zone's OWN buffered fence edge

# The zone's outer stretch (its top + upper sides, y > 55 -- clear of the developed core's own top at y~55,
# where the two shapes join and the hull cuts diagonally): every such vertex must lie ON the boundary fence
# ring. Because the same buffered polygon feeds both the zone's own fence AND find_boundary_fencing()'s
# union, and the convex hull reuses those exact vertices along this outer stretch, the coincidence is exact.
outer_stretch_pts = [pt for pt in edge_water_fence.coords if pt[1] > 55]
assert len(outer_stretch_pts) >= 3, "test setup must expose a real outer stretch of the zone edge forming the hull"
outer_stretch_distances = [Point(pt).distance(edge_boundary_ring) for pt in outer_stretch_pts]
max_outer_deviation = max(outer_stretch_distances)
# EXACT coincidence (same coordinates, not a nearby parallel line -- a genuine parallel offset would sit a
# whole WATER_ZONE_FENCE_BUFFER_METERS/BOUNDARY_FENCE_MARGIN_METERS away). A hair of floating-point slack for
# the union/hull re-noding, far below any parallel-offset scale.
assert max_outer_deviation < 1e-6, (
    "along the stretch where the water zone forms the developed footprint's convex-hull edge, the boundary "
    "fence ring must coincide EXACTLY with the zone's own buffered fence edge (same coordinates, not a nearby "
    f"parallel line) -- max point-to-ring distance {max_outer_deviation:.2e}m exceeds the exact-coincidence tolerance"
)
print(
    "find_boundary_fencing(): where a water zone forms the developed footprint's true outer (convex-hull) "
    f"edge, the boundary fence ring's coordinates coincide EXACTLY with that zone's own buffered fence edge "
    f"along that stretch (max deviation {max_outer_deviation:.2e}m over {len(outer_stretch_pts)} vertices) "
    "-- it meets the zone's fence, uses it, continues."
)


# --- 10. a road corridor footprint reaching a real anchor point ON the drawn boundary -> the hard-ceiling
#         clip still holds: the fence never extends past boundary_polygon_utm even though the corridor's own
#         margined shape would otherwise cross it ---

# A corridor running from the interior straight to the top boundary edge (y=100). Its own 5m margin would
# push a fence out to y=105 (past the parcel) -- the clip must pull it back to the boundary.
road_to_boundary = box(45, 80, 55, 100)
road_rings = find_boundary_fencing(
    TEST_BOUNDARY_UTM,
    production_zone_polygons_utm=[],
    structure_site_polygon_utm=None,
    road_corridor_cell_footprint_polygon_utm=road_to_boundary,
    water_zone_polygon_utm=None,
    tree_zone_polygons_utm=[],
)
assert len(road_rings) == 1, f"the road corridor footprint should produce a single fence ring, got {len(road_rings)}"
road_ring_polygon = Polygon(road_rings[0])
# Confirm the margin genuinely WANTED to cross the boundary (the un-clipped margined corridor reaches y=105)...
assert road_to_boundary.buffer(BOUNDARY_FENCE_MARGIN_METERS).bounds[3] > 100.0, (
    "test setup: the corridor's own margined shape must extend past the boundary for the clip to be under test"
)
# ...yet the returned fence ring never escapes the drawn boundary (the hard-ceiling clip, step 4).
assert BOUNDARY_POLYGON.buffer(1e-6).covers(road_ring_polygon), (
    "the drawn boundary is a hard ceiling -- the fence ring must never extend past boundary_polygon_utm, even "
    "though the road corridor's own margined footprint would otherwise cross it"
)
assert road_ring_polygon.bounds[3] <= 100.0 + 1e-6, "the fence ring must not cross the top boundary edge (y=100)"
print(
    "find_boundary_fencing(): a road corridor reaching an anchor ON the drawn boundary still gets clipped back "
    "to the parcel -- the hard-ceiling boundary clip holds even against the corridor's own margined footprint."
)


# --- 11. CONVEX-HULL regression (the photo that motivated this pass): a developed footprint with a
#         genuine concavity -- two separated feature clusters with a gap between them -> the resulting
#         fence ring is CONVEX and bridges the gap, rather than dipping inward through it the way the
#         pre-hull margined union did ---

cluster_a = box(15, 40, 35, 60)  # left developed cluster
cluster_b = box(65, 40, 85, 60)  # right developed cluster -- a wide gap (x 35..65) between them
hull_rings = find_boundary_fencing(
    TEST_BOUNDARY_UTM,
    production_zone_polygons_utm=[cluster_a, cluster_b],
    structure_site_polygon_utm=None,
    road_corridor_cell_footprint_polygon_utm=None,
    water_zone_polygon_utm=None,
    tree_zone_polygons_utm=[],
)
assert len(hull_rings) == 1, f"two clusters bridged by the convex hull should be a single ring, got {len(hull_rings)}"
hull_ring_polygon = Polygon(hull_rings[0])

# The pre-hull margined union would have left a concave gap between the two clusters -- the fence would dip
# inward through it. Confirm the OLD behavior genuinely dips (the raw margined union does NOT bridge the gap)
# so this is a real regression test, then confirm the NEW ring bridges it.
from shapely.ops import unary_union as _unary_union

raw_margined_union = _unary_union([cluster_a.buffer(BOUNDARY_FENCE_MARGIN_METERS), cluster_b.buffer(BOUNDARY_FENCE_MARGIN_METERS)])
gap_midpoint = Point(50.0, 50.0)  # dead center of the gap between the two clusters
assert not raw_margined_union.contains(gap_midpoint), (
    "test setup: the pre-hull margined union must genuinely leave the gap open (dip inward) for the hull fix "
    "to be under test"
)
# NEW behavior: the fence ring is convex (equals its own convex hull) and encloses the gap midpoint.
assert hull_ring_polygon.symmetric_difference(hull_ring_polygon.convex_hull).area < 1e-6, (
    "the developed-footprint fence ring must be CONVEX (equal to its own convex hull) -- it must not dip "
    "inward through the gap between the two separated clusters the way the pre-hull union did"
)
assert hull_ring_polygon.contains(gap_midpoint), (
    "the convex-hull fence ring must BRIDGE the gap between the two clusters (enclose its midpoint), not "
    "route inward around each cluster separately"
)
print(
    "find_boundary_fencing(): two separated developed clusters produce a single CONVEX fence ring that "
    "bridges the gap between them (the pre-hull union would have dipped inward through it) -- the direct "
    "regression test for the two-cluster-with-a-gap photo."
)


# =====================================================================
# identify_boundary_fencing()/identify_fencing(): fetch-and-wrap entry points. identify_boundary_
# fencing() no longer fetches canopy at all (the boundary fence does not route around canopy anymore),
# so it needs no canopy mock. identify_fencing()'s OWN self-compute fallbacks (road/water/tree
# candidates) still carry a canopy fetch, so production_area.get_canopy_height_for_boundary is mocked
# here (same pattern test_production_area_ceiling.py already uses) to keep those offline. A synthetic
# DEM sized to PROPERTY_BOUNDARY's own UTM extent stands in for a real fetch.
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


# --- identify_boundary_fencing: bare property (no developed footprint) -> 1 schema-valid
#     "perimeter_fencing" feature, fence_type=boundary, and NO canopy fetch is needed at all ---

# No canopy mock here: identify_boundary_fencing() no longer fetches canopy. If it still tried to,
# this call (with pa.get_canopy_height_for_boundary left REAL) would attempt a network fetch and
# fail offline -- so a clean pass is itself proof the canopy fetch is gone.
boundary_result = identify_boundary_fencing(PROPERTY_BOUNDARY, dem=TEST_DEM)
validate_feature_collection(boundary_result["fencing_geojson"])
assert boundary_result["segment_count"] == 1
boundary_features = boundary_result["fencing_geojson"]["features"]
assert len(boundary_features) == 1
boundary_feature = boundary_features[0]
assert boundary_feature["properties"]["layer"] == "perimeter_fencing"
assert boundary_feature["properties"]["fence_type"] == "boundary"
assert "canopy_buffer_meters" not in boundary_feature["properties"], (
    "the boundary fence no longer carries a canopy_buffer_meters property -- canopy is not a factor anymore"
)
assert boundary_feature["properties"]["confidence"] == "high"
assert boundary_feature["geometry"]["type"] == "LineString", (
    "boundary fencing must be a LineString (a fence line), not a Polygon (a filled zone)"
)
boundary_out_coords = boundary_feature["geometry"]["coordinates"]
assert tuple(boundary_out_coords[0]) == tuple(boundary_out_coords[-1]), "boundary fence line should be a closed ring"
print("identify_boundary_fencing() on a bare property produces 1 schema-valid 'perimeter_fencing' feature (0 canopy fetches).")

boundary_notes = boundary_feature["properties"]["confidence_notes"].lower()
assert "no fence type, height, or material" in boundary_notes, (
    "boundary fencing confidence_notes must explicitly state fence type/height/material is out of scope"
)
assert "developed footprint" in boundary_notes, (
    "boundary fencing confidence_notes must describe the developed-footprint behavior"
)
assert "does not route around tree canopy" in boundary_notes, (
    "boundary fencing confidence_notes must state the fence no longer routes around canopy"
)
print("Boundary fencing confidence_notes describes the developed-footprint (canopy-agnostic) behavior.")


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
# tree_zone_render_fill_polygons_utm=[] is passed explicitly (rather than left to
# identify_fencing()'s own default fetch) so this stays deterministic and offline regardless
# of whether a given test environment happens to have network access -- an unmocked tree
# zone fetch can hang for a long time retrying before finally giving up, so leaving it at its
# own default (None) here would make this test far too slow, not just nondeterministic.
# selected_water_zone_render_fill_polygon_utm has no such "empty but
# not None" sentinel available (a single selected zone is naturally Optional[Polygon] --
# None already means BOTH "not supplied" and "no zone sited"), so fetch_and_select_optimal_
# water_zone() itself is mocked out instead, same pattern as the existing canopy-height mock
# just below it. selected_road_corridor is never derived at all here -- with
# tree_zone_render_fill_polygons_utm=[] already supplied, identify_fencing()'s own tree-zone
# self-compute fallback (the only consumer of a derived selected_road_corridor) never runs,
# so no explicit override/mock is needed there either (see identify_fencing()'s own docstring).

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
        tree_zone_render_fill_polygons_utm=[],
    )
no_stream_layers = [f["properties"]["layer"] for f in no_stream_result["fencing_geojson"]["features"]]
assert no_stream_layers == ["perimeter_fencing"], (
    f"with no streams, only perimeter_fencing should be produced, got {no_stream_layers}"
)
print("identify_fencing() still produces boundary fencing when no streams are present.")


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
# TEST_DEM/_fake_no_canopy from the identify_fencing() section above.
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
    mock_patch.object(fencing, "find_water_zone_fencing", _capture_water_fencing),
    mock_patch.object(fencing, "find_tree_zone_fencing", _capture_tree_fencing),
):
    high_level_result = identify_fencing(
        PROPERTY_BOUNDARY,
        water_features_geojson=no_stream_water,
        dem=TEST_DEM,
        selected_road_corridor=HIGH_LEVEL_ROAD_CORRIDOR,
        selected_water_zone=HIGH_LEVEL_WATER_ZONE,
        tree_zone_patches=HIGH_LEVEL_TREE_ZONE_PATCHES,
    )
validate_feature_collection(high_level_result["fencing_geojson"])
assert _captured_low_level["water_polygon"] is HIGH_LEVEL_WATER_POLYGON, (
    "selected_water_zone_render_fill_polygon_utm must be derived directly from "
    "selected_water_zone['render_fill_polygon_utm']"
)
assert _captured_low_level["tree_polygons"] == [HIGH_LEVEL_TREE_POLYGON_A, HIGH_LEVEL_TREE_POLYGON_B]
assert _captured_low_level["tree_polygons"][0] is HIGH_LEVEL_TREE_POLYGON_A
assert _captured_low_level["tree_polygons"][1] is HIGH_LEVEL_TREE_POLYGON_B
print(
    "identify_fencing(): high-level selected_water_zone/tree_zone_patches overrides derive the "
    "low-level values directly (identity-checked), and a supplied selected_road_corridor skips its "
    "own self-compute too -- identify_road_corridor_candidates()/fetch_and_select_optimal_water_"
    "zone()/identify_tree_zone_candidates() are each called zero times."
)


# --- Scenario 2 (regression): none of the six new overrides supplied -> all three self-compute
# calls still run exactly once each, same as pre-branch behavior, producing identical output on the
# existing fixture (no_stream_water/TEST_DEM) ---

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


# --- canopy_height override forwarding ---
#
# The boundary fence no longer uses canopy at all, so identify_boundary_fencing() has NO
# canopy_height parameter and issues NO canopy fetch anymore -- the earlier probe test that
# asserted it forwarded canopy_height to a mandatory gate is gone with that behavior. What
# remains to prove is that identify_fencing() still forwards canopy_height to its three OWN
# self-compute fallback calls (road/water/tree candidates), each of which still has its own
# downstream canopy gate, AND that the boundary-fence path itself triggers ZERO canopy fetches.
# Reuses the TEST_DEM/PROPERTY_BOUNDARY fixture above.
from _canopy_override_probe import CanopyOverrideProbe, clean_canopy_for  # noqa: E402


# =====================================================================
# identify_fencing(): canopy_height= override forwarding. It must reach identify_fencing()'s three
# self-compute fallback calls (identify_road_corridor_candidates()/fetch_and_select_optimal_water_
# zone()/identify_tree_zone_candidates()), each of which independently accepts canopy_height and has
# its own canopy gate one level further down. Those three are mocked (a fully real run would also hit
# real, slow NHD/SSURGO/soil network fetches unrelated to canopy forwarding), with their canopy_height
# kwarg captured for an identity check. identify_boundary_fencing() is left REAL -- and the
# CanopyOverrideProbe proves it triggers ZERO canopy fetches now (the boundary fence no longer routes
# around canopy), so no canopy gate is reached inside fencing.py's own direct call path at all.
# =====================================================================

CANOPY_FENCING_OVERRIDE = clean_canopy_for(TEST_DEM)
_captured_fencing_canopy_kwargs = {}


def _capture_road_corridor_canopy(*args, **kwargs):
    _captured_fencing_canopy_kwargs["road"] = kwargs.get("canopy_height")
    return {"selected_road_corridor": None}


def _capture_water_zone_canopy(*args, **kwargs):
    _captured_fencing_canopy_kwargs["water"] = kwargs.get("canopy_height")
    return None


def _capture_tree_zone_canopy(*args, **kwargs):
    _captured_fencing_canopy_kwargs["tree"] = kwargs.get("canopy_height")
    return {"patches": []}


with (
    mock_patch.object(fencing, "identify_road_corridor_candidates", _capture_road_corridor_canopy),
    mock_patch.object(fencing, "fetch_and_select_optimal_water_zone", _capture_water_zone_canopy),
    mock_patch.object(fencing, "identify_tree_zone_candidates", _capture_tree_zone_canopy),
):
    with CanopyOverrideProbe() as fencing_canopy_probe:
        canopy_fencing_result = identify_fencing(
            PROPERTY_BOUNDARY,
            water_features_geojson=no_stream_water,
            dem=TEST_DEM,
            canopy_height=CANOPY_FENCING_OVERRIDE,
        )

validate_feature_collection(canopy_fencing_result["fencing_geojson"])
# The boundary-fence path (identify_boundary_fencing() -> find_boundary_fencing()) reaches NO canopy
# gate now -- 0 fetches AND 0 real gates -- since canopy is no longer a factor there.
assert fencing_canopy_probe.fetch_calls == 0, (
    "identify_fencing() must trigger ZERO canopy fetches from its own boundary-fence path (the boundary "
    f"fence no longer uses canopy), got {fencing_canopy_probe.fetch_calls}"
)
assert len(fencing_canopy_probe.mask_arrays) == 0, (
    "identify_boundary_fencing() must reach NO canopy gate now (canopy removed from the boundary fence), "
    f"but {len(fencing_canopy_probe.mask_arrays)} gate(s) ran"
)
for call_name in ("road", "water", "tree"):
    assert _captured_fencing_canopy_kwargs[call_name] is CANOPY_FENCING_OVERRIDE, (
        f"identify_fencing() must forward canopy_height to its own {call_name} self-compute call by identity"
    )
print(
    "identify_fencing(): a supplied canopy_height override is forwarded by identity into all three "
    "self-compute fallback calls (road/water/tree candidates), while the boundary-fence path itself "
    "triggers ZERO canopy fetches (canopy is no longer a factor in boundary fencing)."
)


# --- REGRESSION: no canopy_height supplied -> identify_boundary_fencing() still runs with
# canopy_height=None (same as the plain identify_fencing() scenario earlier in this file), and all
# three self-compute calls receive canopy_height=None too -- a pure no-op default, not a behavior change ---

_captured_fencing_canopy_kwargs_regression = {}


def _capture_road_corridor_canopy_regression(*args, **kwargs):
    _captured_fencing_canopy_kwargs_regression["road"] = kwargs.get("canopy_height")
    return {"selected_road_corridor": None}


def _capture_water_zone_canopy_regression(*args, **kwargs):
    _captured_fencing_canopy_kwargs_regression["water"] = kwargs.get("canopy_height")
    return None


def _capture_tree_zone_canopy_regression(*args, **kwargs):
    _captured_fencing_canopy_kwargs_regression["tree"] = kwargs.get("canopy_height")
    return {"patches": []}


with (
    mock_patch.object(pa, "get_canopy_height_for_boundary", _fake_no_canopy),
    mock_patch.object(fencing, "identify_road_corridor_candidates", _capture_road_corridor_canopy_regression),
    mock_patch.object(fencing, "fetch_and_select_optimal_water_zone", _capture_water_zone_canopy_regression),
    mock_patch.object(fencing, "identify_tree_zone_candidates", _capture_tree_zone_canopy_regression),
):
    canopy_regression_result = identify_fencing(
        PROPERTY_BOUNDARY,
        water_features_geojson=no_stream_water,
        dem=TEST_DEM,
    )
validate_feature_collection(canopy_regression_result["fencing_geojson"])
for call_name in ("road", "water", "tree"):
    assert _captured_fencing_canopy_kwargs_regression[call_name] is None, (
        f"identify_fencing() with no canopy_height= supplied must still pass canopy_height=None through to "
        f"its {call_name} self-compute call -- a pure no-op default, not a behavior change"
    )
print(
    "REGRESSION: identify_fencing() with no canopy_height= supplied still passes canopy_height=None through "
    "to all three self-compute calls -- adding the parameter is a pure no-op for existing callers."
)


print("\nAll fencing checks passed.")
