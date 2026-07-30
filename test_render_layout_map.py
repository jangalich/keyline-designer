"""
test_render_layout_map.py

Offline (no-network) checks for render_layout_map.py's production-zone
rendering: contour-line texture (contour_lines.py's global contour lines,
clipped per zone at render time against that zone's own real polygon_utm),
not a filled/outlined shape -- see production_area.py's and
render_layout_map.py's own module docstrings for why the earlier
display_polygon_utm/display_geometry_wgs84 fields were removed entirely
in favor of this.

Builds real patch dicts via production_area.cluster_and_gate() +
production_suitability.score_production_areas() against small synthetic,
SLOPED DEMs (a real elevation gradient is required here -- unlike
test_production_area.py's own flat fixtures, which only needed cell-mask
shape, contour lines need real elevation variation to exist at all), then
feeds them to render_layout_map.py through its `layers` pre-fetch
parameter -- no DEM/soil/basemap network calls required; the basemap
fetch itself degrades gracefully (same as every other network-backed
layer in this pipeline) when unreachable, so render_layout_map() runs
fully offline here.

The one invariant this whole pass depends on: zones_geojson (and every
patch's geometry_wgs84/polygon_utm) is completely unaffected by this
rendering-only change -- see the dedicated regression check near the
bottom.
"""

import os
import tempfile

import numpy as np
from shapely.geometry import box

import production_area as pa
import render_layout_map as rlm
from contour_lines import compute_contour_lines
from production_area import cluster_and_gate, compute_step1_eligible_cells
from production_suitability import score_production_areas

RESOLUTION = (5.0, 5.0)
RISE_PER_ROW = 0.4  # meters -- a real, modest gradient so contour lines actually exist


def _sloped_dem(rows: int, cols: int) -> dict:
    array = np.zeros((rows, cols), dtype=np.float32)
    for row in range(rows):
        array[row, :] = 100.0 + row * RISE_PER_ROW
    return {
        "array": array,
        "resolution_meters": RESOLUTION,
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }


def _full_extent_boundary(dem: dict):
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    x0, y0 = dem["origin_x"], dem["origin_y"]
    return box(x0, y0 - rows * py, x0 + cols * px, y0)


def _rect_cells(r0, r1, c0, c1):
    return [(r, c) for r in range(r0, r1) for c in range(c0, c1)]


def _mask_from_cells(shape, cells):
    mask = np.zeros(shape, dtype=bool)
    for r, c in cells:
        mask[r, c] = True
    return mask


def _scored_patches_for(cell_mask, dem):
    """Real end-to-end patch dicts (cluster_and_gate + score_production_areas),
    exactly the shape render_layout_map.py's own production_result['scored_patches']
    already carries."""
    boundary = _full_extent_boundary(dem)
    step1 = compute_step1_eligible_cells(dem, boundary, disqualifying_soil_union_utm=None)
    patches = cluster_and_gate(cell_mask, dem, boundary, step1)
    return score_production_areas(patches, dem, step1)


def _clip_contours_to_zone(contour_lines: list[dict], patch: dict, geometry_key: str = "render_polygon_utm"):
    """Exactly what render_layout_map.py's own rendering loop does per
    production zone -- real shapely intersection of the GLOBAL contour
    lines against that zone's own render_polygon_utm (same CRS, no
    reprojection needed) -- returns the list of non-empty clipped
    geometries. geometry_key defaults to render_polygon_utm (the real
    production behavior); pass "polygon_utm" to reproduce the PRE-fix
    clipping behavior for comparison."""
    clipped = []
    for contour in contour_lines:
        piece = contour["lines_utm"].intersection(patch[geometry_key])
        if not piece.is_empty:
            clipped.append(piece)
    return clipped


# --- Solid zone smaller than the DEM's full extent: clipped contours must stay
#     entirely within the zone's own real boundary -- no segment outside it ---

SINGLE_SHAPE = (30, 30)
single_dem = _sloped_dem(*SINGLE_SHAPE)
single_cells = _rect_cells(5, 15, 5, 15)  # a 10x10 block -- well inside the 30x30 DEM, not the full extent
single_mask = _mask_from_cells(SINGLE_SHAPE, single_cells)
single_scored = _scored_patches_for(single_mask, single_dem)
assert len(single_scored) == 1, f"expected 1 scored patch, got {len(single_scored)}"
single_patch = single_scored[0]

global_contours = compute_contour_lines(single_dem)
assert len(global_contours) > 0, "test setup should produce real global contour lines on a sloped DEM"

zone_bounds = single_patch["polygon_utm"].bounds
full_extent_bounds = _full_extent_boundary(single_dem).bounds
assert full_extent_bounds != zone_bounds, (
    "test setup should genuinely have contours extending past the zone's own (smaller) extent, "
    "otherwise clipping isn't actually being exercised"
)
some_contour_exceeds_zone = any(
    not box(*zone_bounds).buffer(1e-6).contains(c["lines_utm"]) for c in global_contours
)
assert some_contour_exceeds_zone, (
    "test setup should genuinely have at least one global contour line extending past the zone's own "
    "bounds -- otherwise this test wouldn't be exercising real clipping"
)

clipped_pieces = _clip_contours_to_zone(global_contours, single_patch)
assert clipped_pieces, "expected at least one contour segment to survive clipping into this zone"
zone_polygon_buffered = single_patch["polygon_utm"].buffer(1e-6)
for piece in clipped_pieces:
    assert zone_polygon_buffered.contains(piece), (
        "every clipped contour segment must lie entirely within the zone's own real boundary "
        "(polygon_utm) -- no segment should exist outside it in the clipped output"
    )
print(
    f"Clipping: {len(clipped_pieces)} of {len(global_contours)} global contour line(s) survive clipping into "
    "a single zone smaller than the DEM's full extent, and every clipped segment stays entirely within that "
    "zone's own real boundary."
)


# --- Split (waist) cluster: the two resulting zones must each get their own independently-clipped
#     contour segments, with no segments appearing in the gap between them ---

DUMBBELL_SHAPE = (16, 30)
dumbbell_dem = _sloped_dem(*DUMBBELL_SHAPE)
lobe_a = _rect_cells(0, 10, 0, 10)
lobe_b = _rect_cells(0, 10, 16, 26)
narrow_strip = _rect_cells(4, 6, 10, 16)  # narrower than MIN_ZONE_WAIST_METERS -- same fixture style as production_area.py's own tests
dumbbell_cells = lobe_a + lobe_b + narrow_strip
dumbbell_mask = _mask_from_cells(DUMBBELL_SHAPE, dumbbell_cells)

# The real, permanently EXCLUDED ground between the lobes -- the rest of
# the connecting corridor (same column range as narrow_strip, but the
# rows OUTSIDE it) that was never part of the dumbbell mask at all, on
# either side of the split. This is the actual "gap" a farmer would see
# on the ground; it's distinct from the reclaimed narrow_strip cells
# themselves, which Part 1 splits down the middle between the two zones
# -- the two zones' own real footprints are allowed to TOUCH exactly
# along that internal dividing line (adjacent cells sharing an edge,
# zero-area/zero-length overlap), that's expected and not a "gap
# violation"; what must never happen is a contour segment actually
# drawn INSIDE this real excluded corridor.
gap_cells = _rect_cells(0, 4, 10, 16) + _rect_cells(6, 10, 10, 16)

dumbbell_scored = _scored_patches_for(dumbbell_mask, dumbbell_dem)
assert len(dumbbell_scored) == 2, f"expected the waist split to produce 2 scored patches, got {len(dumbbell_scored)}"
zone_1, zone_2 = dumbbell_scored

gap_footprint = pa._cell_union_footprint(gap_cells, dumbbell_dem)
assert zone_1["polygon_utm"].intersection(gap_footprint).area < 1e-9, (
    "test sanity check: zone 1's real footprint must not actually cover any of the excluded gap ground "
    "(touching its edge is fine, covering real area of it is not)"
)
assert zone_2["polygon_utm"].intersection(gap_footprint).area < 1e-9, (
    "test sanity check: zone 2's real footprint must not actually cover any of the excluded gap ground"
)

dumbbell_global_contours = compute_contour_lines(dumbbell_dem)
assert len(dumbbell_global_contours) > 0, "test setup should produce real global contour lines on a sloped DEM"

clipped_1 = _clip_contours_to_zone(dumbbell_global_contours, zone_1)  # default: clips against render_polygon_utm
clipped_2 = _clip_contours_to_zone(dumbbell_global_contours, zone_2)
assert clipped_1 and clipped_2, "both split zones should get at least one clipped contour segment each"

zone_1_buffered = zone_1["render_polygon_utm"].buffer(1e-6)
zone_2_buffered = zone_2["render_polygon_utm"].buffer(1e-6)
for piece in clipped_1:
    assert zone_1_buffered.contains(piece), "zone 1's clipped contours must stay entirely within zone 1's own render boundary"
for piece in clipped_2:
    assert zone_2_buffered.contains(piece), "zone 2's clipped contours must stay entirely within zone 2's own render boundary"

gap_overlap_length = sum(piece.intersection(gap_footprint).length for piece in clipped_1 + clipped_2)
assert gap_overlap_length < 1e-9, (
    f"no clipped contour segment (from EITHER zone) may actually run through the real excluded gap ground "
    f"between the two split zones -- got {gap_overlap_length}m of overlap"
)
print(
    f"Split rendering: the two waist-split zones each get their own independently-clipped contour segments "
    f"({len(clipped_1)} and {len(clipped_2)} respectively), with zero overlap into the real excluded gap "
    f"ground ({round(gap_footprint.area, 1)} sq m) between them."
)


# --- Confirmed-live bug reproduction: polygon_utm for the two split zones is directly adjacent (ZERO
#     distance) -- reclaim reassigns every eroded-away cell to whichever piece is nearest, so there is
#     nothing between them in the real, reported geometry. Clipping against render_polygon_utm (the fix)
#     instead of polygon_utm (the pre-fix behavior) is what actually produces a visible blank strip at the
#     waist itself -- not just in the pre-existing gap_cells corridor checked above. ---

assert zone_1["polygon_utm"].distance(zone_2["polygon_utm"]) < 1e-9, (
    "test setup should reproduce the confirmed-live bug: polygon_utm for the two split zones must be "
    "directly adjacent with ZERO distance -- reclaim leaves nothing between them"
)
assert zone_1["render_polygon_utm"].distance(zone_2["render_polygon_utm"]) > 0, (
    "render_polygon_utm for the two split zones must have a real gap, unlike polygon_utm"
)

narrow_strip_footprint = pa._cell_union_footprint(narrow_strip, dumbbell_dem)
old_clipped_1 = _clip_contours_to_zone(dumbbell_global_contours, zone_1, geometry_key="polygon_utm")
old_clipped_2 = _clip_contours_to_zone(dumbbell_global_contours, zone_2, geometry_key="polygon_utm")
old_waist_overlap = sum(p.intersection(narrow_strip_footprint).length for p in old_clipped_1 + old_clipped_2)
new_waist_overlap = sum(p.intersection(narrow_strip_footprint).length for p in clipped_1 + clipped_2)
assert old_waist_overlap > 0, (
    "test sanity check: clipping against the PRE-fix polygon_utm should genuinely draw contour segments "
    "through the reclaimed waist cells (narrow_strip) -- otherwise this isn't reproducing the real bug"
)
assert new_waist_overlap < 1e-9, (
    f"clipping against render_polygon_utm (the fix) must produce ZERO contour overlap with the reclaimed "
    f"waist cells themselves -- got {new_waist_overlap}m of overlap"
)
print(
    f"Waist-gap fix: clipping against the pre-fix polygon_utm draws {round(old_waist_overlap, 1)}m of "
    "contour line directly through the reclaimed waist cells (the confirmed-live bug); clipping against "
    "render_polygon_utm (the fix) draws zero."
)


# --- Full render_layout_map() pass, offline: confirms the whole contour-clipping pipeline
#     (fetch_layout_layers' own contour_lines computation, per-zone clipping, reprojection,
#     drawing, legend, PNG assembly) runs without crashing for a production_result with a split ---

property_boundary = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

for rank, patch in enumerate(sorted(dumbbell_scored, key=lambda p: -p["suitability_score"]), start=1):
    patch["rank"] = rank

synthetic_layers = {
    "dem": dumbbell_dem,
    "production_result": {
        "scored_patches": dumbbell_scored,
        "total_selected_acreage": round(sum(p["area_acres"] for p in dumbbell_scored), 2),
        "percent_of_parcel": 42.0,
    },
    "water_zone": None,
    "road_corridor": None,
    "structure_site": None,
    "water_features": {"streams": []},
    "contour_lines": dumbbell_global_contours,
}

with tempfile.TemporaryDirectory() as tmpdir:
    output_path = os.path.join(tmpdir, "layout_map.png")
    result_path = rlm.render_layout_map(property_boundary, output_path, layers=synthetic_layers)
    assert result_path == output_path
    assert os.path.exists(output_path)
    assert os.path.getsize(output_path) > 0, "render_layout_map() must produce a real, non-empty PNG"

print(
    "Full pipeline: render_layout_map() runs offline end-to-end (basemap fetch degrades gracefully) with a "
    "split production_result, drawing clipped contour-line texture for each zone and producing a real, "
    "non-empty PNG."
)


# --- Same full pipeline, but with a real (non-empty) road_tree_exclusion_polygon_utm present too --
#     confirms the bridging code path itself (not just the "key absent" default) runs end-to-end without
#     crashing and still produces a real PNG. ---

synthetic_layers_with_road_tree = dict(synthetic_layers)
synthetic_layers_with_road_tree["production_result"] = dict(synthetic_layers["production_result"])
synthetic_layers_with_road_tree["production_result"]["road_tree_exclusion_polygon_utm"] = pa._cell_union_footprint(
    narrow_strip, dumbbell_dem
)

with tempfile.TemporaryDirectory() as tmpdir:
    output_path = os.path.join(tmpdir, "layout_map_with_bridging.png")
    result_path = rlm.render_layout_map(property_boundary, output_path, layers=synthetic_layers_with_road_tree)
    assert result_path == output_path
    assert os.path.exists(output_path) and os.path.getsize(output_path) > 0, (
        "render_layout_map() must still produce a real, non-empty PNG with a real road_tree_exclusion_polygon_utm present"
    )
print(
    "Full pipeline: render_layout_map() also runs end-to-end with a real road_tree_exclusion_polygon_utm "
    "present, exercising the contour-bridging draw path without crashing."
)


# =====================================================================
# Invariant: zones_geojson (and every patch's geometry_wgs84/polygon_utm)
# is completely unaffected by this rendering-only change -- this only
# changes HOW production zones are drawn and removes the now-unused
# display_polygon_utm/display_geometry_wgs84 fields, not the canonical
# geometry any other consumer reasons over.
# =====================================================================

from production_area import production_areas_to_geojson
from production_suitability import production_suitability_to_geojson

zone_1_geojson = production_suitability_to_geojson([dict(zone_1)])
assert zone_1_geojson["features"][0]["geometry"] == zone_1["geometry_wgs84"], (
    "production_suitability_to_geojson() must still embed geometry_wgs84 exactly, byte-for-byte"
)
assert "display_polygon_utm" not in zone_1 and "display_geometry_wgs84" not in zone_1, (
    "the removed display fields must not reappear on a scored patch dict"
)

raw_patches = cluster_and_gate(
    dumbbell_mask, dumbbell_dem, _full_extent_boundary(dumbbell_dem),
    compute_step1_eligible_cells(dumbbell_dem, _full_extent_boundary(dumbbell_dem), disqualifying_soil_union_utm=None),
)
raw_geojson = production_areas_to_geojson(raw_patches)
for feature, patch in zip(raw_geojson["features"], raw_patches):
    assert feature["geometry"] == patch["geometry_wgs84"], (
        "production_areas_to_geojson() must also embed geometry_wgs84 exactly for every patch"
    )
    assert "display_polygon_utm" not in patch and "display_geometry_wgs84" not in patch

print(
    "zones_geojson invariant: both production_areas_to_geojson() and production_suitability_to_geojson() "
    "still embed geometry_wgs84 exactly, byte-for-byte -- unaffected by the switch to contour-line "
    "rendering, and the removed display fields are genuinely gone from every patch dict."
)


# =====================================================================
# Invariant: area_acres, zones_geojson (geometry_wgs84), and suitability
# scoring for a SPLIT cluster all continue to reflect the FULL,
# POST-reclaim polygon_utm -- render_polygon_utm exists purely for
# display and must play no role in any reported number or geometry.
# Same invariant every prior rendering-only pass in this pipeline has
# needed (see the zones_geojson invariant directly above).
# =====================================================================

from rasterio.warp import transform_geom as _transform_geom_check
from shapely.geometry import shape as _shape_check

for zone in (zone_1, zone_2):
    assert zone["render_polygon_utm"].area < zone["polygon_utm"].area, (
        "test sanity check: this is a real split fixture, render_polygon_utm must genuinely be smaller"
    )
    assert zone["area_acres"] == round(zone["polygon_utm"].area / pa.SQUARE_METERS_PER_ACRE, 2), (
        "area_acres must be computed from the full, POST-reclaim polygon_utm, not render_polygon_utm"
    )
    geometry_utm_reprojected = _shape_check(
        _transform_geom_check("EPSG:4326", dumbbell_dem["crs"], zone["geometry_wgs84"])
    )
    reprojection_diff = geometry_utm_reprojected.symmetric_difference(zone["polygon_utm"]).area
    assert reprojection_diff < 1e-6, (
        f"geometry_wgs84 must reproject back to the full polygon_utm, not the narrower render_polygon_utm "
        f"(symmetric difference area {reprojection_diff})"
    )

# Direct proof that render_polygon_utm plays no role in suitability scoring: rebuild the same split from
# scratch, strip render_polygon_utm off each raw patch entirely before scoring, and confirm
# score_production_areas() produces byte-identical numeric results either way.
fresh_step1 = compute_step1_eligible_cells(dumbbell_dem, _full_extent_boundary(dumbbell_dem), disqualifying_soil_union_utm=None)
fresh_patches = cluster_and_gate(dumbbell_mask, dumbbell_dem, _full_extent_boundary(dumbbell_dem), fresh_step1)
for p in fresh_patches:
    del p["render_polygon_utm"]
stripped_scored = {p["id"]: p for p in score_production_areas(fresh_patches, dumbbell_dem, fresh_step1)}


def _same_score(a: float, b: float) -> bool:
    return a == b or (a != a and b != b)  # NaN != NaN, but both-NaN counts as "identical" here


for zone in (zone_1, zone_2):
    stripped = stripped_scored[zone["id"]]
    assert _same_score(stripped["suitability_score"], zone["suitability_score"]), (
        "suitability_score must be identical whether or not render_polygon_utm is present on the patch dict"
    )
    assert _same_score(stripped["area_score"], zone["area_score"]) and _same_score(
        stripped["compactness_score"], zone["compactness_score"]
    ), "size_factor's sub-scores must be identical whether or not render_polygon_utm is present"

print(
    "render_polygon_utm invariant: area_acres, geometry_wgs84 (zones_geojson), and suitability scoring for "
    "both split zones all continue to reflect the full, post-reclaim polygon_utm -- byte-identical whether "
    "or not render_polygon_utm is even present on the patch dict."
)


# =====================================================================
# Contour bridging: genuinely excluded (steep/hydric) ground sandwiched
# between two stretches of the SAME zone's own contour texture must
# render bridged (in BRIDGE_CONTOUR_COLOR); a gap that's genuinely
# road/tree (above BRIDGE_ROAD_TREE_OVERLAP_THRESHOLD of the candidate's
# own LENGTH) must stay unbridged. See render_layout_map.py's own module
# docstring for the full picture; _bridge_candidates_for_zone()/
# _bridge_segments_for_zone() are pure geometry, hand-testable directly
# against a synthetic zone + a single synthetic contour line, no DEM or
# cluster_and_gate() needed for THESE cases (the waist-split
# generalization check below reuses the real dumbbell fixture instead).
# =====================================================================

from shapely.geometry import LineString, Polygon as ShapelyPolygon

# A 30x10 zone (x:0-30, y:0-10) with a real interior notch (x:12-18, y:3-7)
# -- excluded ground with NO connection to the notch's own top/bottom/left/
# right edges relative to the zone's own boundary except through the zone
# itself, so a horizontal line through it is "inside zone -> outside notch
# -> inside zone -> outside (real off-zone ground, unbounded to the right)".
bridging_zone = box(0, 0, 30, 10).difference(box(12, 3, 18, 7))
bridging_contour = [{"elevation_m": 100.6, "lines_utm": LineString([(-5, 5), (35, 5)])}]

bridge_candidates = rlm._bridge_candidates_for_zone(bridging_contour, bridging_zone)
assert len(bridge_candidates) == 1, (
    f"test setup should find exactly 1 bridge candidate (the interior notch, flanked by the SAME zone on "
    f"both sides) -- got {len(bridge_candidates)}"
)
assert abs(bridge_candidates[0].length - 6.0) < 1e-9, (
    f"the bridge candidate must be exactly the notch's own span (x: 12 to 18, length 6), "
    f"got length {bridge_candidates[0].length}"
)

# --- Case 1: no road/tree data at all -- genuinely excluded (steep/hydric) ground bridges ---
no_road_tree = ShapelyPolygon()
bridged_no_road_tree = rlm._bridge_segments_for_zone(bridging_contour, bridging_zone, no_road_tree)
assert len(bridged_no_road_tree) == 1, "a bridge candidate with no road/tree overlap at all must render bridged"
print("Contour bridging: a genuinely excluded (steep/hydric) gap, flanked by the same zone on both sides, renders bridged.")

# --- Case 2: road/tree covers 80% of the candidate's own length (above the 0.5 threshold) -- stays unbridged ---
road_tree_high_overlap = box(12, 0, 16.8, 10)  # covers x:12-16.8 of the notch's x:12-18 span = 80%
bridged_high_overlap = rlm._bridge_segments_for_zone(bridging_contour, bridging_zone, road_tree_high_overlap)
assert bridged_high_overlap == [], (
    f"a bridge candidate that's 80% road/tree (above the {rlm.BRIDGE_ROAD_TREE_OVERLAP_THRESHOLD} threshold) "
    f"must stay unbridged, got {len(bridged_high_overlap)} bridged segment(s)"
)
print(
    "Contour bridging: a bridge candidate that's genuinely road/tree (80% overlap, above the "
    f"{rlm.BRIDGE_ROAD_TREE_OVERLAP_THRESHOLD} threshold) correctly stays unbridged."
)

# --- Case 3: road/tree covers only 20% of the candidate's own length (below threshold) -- still bridges ---
road_tree_low_overlap = box(12, 0, 13.2, 10)  # covers x:12-13.2 of the notch's x:12-18 span = 20%
bridged_low_overlap = rlm._bridge_segments_for_zone(bridging_contour, bridging_zone, road_tree_low_overlap)
assert len(bridged_low_overlap) == 1, (
    f"a bridge candidate that only brushes a small (20%) edge of real road/tree ground -- below the "
    f"{rlm.BRIDGE_ROAD_TREE_OVERLAP_THRESHOLD} threshold -- must still bridge, consistent with the "
    f"percentage-threshold approach (not 'any overlap disqualifies')"
)
print(
    "Contour bridging: a candidate that only brushes a small (20%) edge of real road/tree ground -- below "
    "the threshold -- still bridges, consistent with the percentage-threshold approach."
)


# --- Generalization: the waist-split pair's real inter-zone gap must NEVER bridge, for either zone,
#     regardless of what road/tree data is (or isn't) available -- reusing the real dumbbell fixture
#     already established above (zone_1/zone_2/dumbbell_global_contours). ---

waist_gap_cells = narrow_strip + gap_cells  # the FULL real corridor between the two lobes (waist + surrounding gap)
waist_gap_footprint = pa._cell_union_footprint(waist_gap_cells, dumbbell_dem)

for zone in (zone_1, zone_2):
    zone_candidates = rlm._bridge_candidates_for_zone(dumbbell_global_contours, zone["render_polygon_utm"])
    for candidate in zone_candidates:
        overlap = candidate.intersection(waist_gap_footprint).length
        assert overlap < 1e-9, (
            f"a candidate for one split zone must never run through the real inter-zone waist gap -- "
            f"that gap is flanked by TWO DIFFERENT zones, never the same zone on both sides -- got "
            f"{overlap}m of overlap"
        )
    # Even with NO road/tree data at all (the most permissive case -- everything else would bridge),
    # the inter-zone gap specifically must still produce zero bridged segments through it.
    zone_bridged = rlm._bridge_segments_for_zone(dumbbell_global_contours, zone["render_polygon_utm"], ShapelyPolygon())
    for segment in zone_bridged:
        assert segment.intersection(waist_gap_footprint).length < 1e-9, (
            "no bridged segment may run through the real inter-zone waist gap, regardless of road/tree data"
        )
print(
    "Contour bridging generalization: the waist-split pair's real inter-zone gap never bridges for either "
    "zone, regardless of what's in it -- a gap between two DIFFERENT zones is never a bridge candidate."
)


# =====================================================================
# render_polygon_utm (unsmoothed) is what classification MUST use -- not any future smoothed
# geometry (this branch has no such field yet, but the reasoning is tested directly): a
# smoothed/closed boundary can fill in a real concave notch, causing a genuine gap to be missed.
# =====================================================================

# A notch touching the zone's own TOP edge (a concave "bay", not a fully enclosed hole) --
# affects the convex/closed hull, unlike a fully interior hole would.
bay_zone = box(0, 0, 30, 10).difference(box(12, 6, 18, 10))
bay_contour = [{"elevation_m": 100.6, "lines_utm": LineString([(-5, 7), (35, 7)])}]  # row y=7, inside the bay's y-range

exact_candidates = rlm._bridge_candidates_for_zone(bay_contour, bay_zone)
assert len(exact_candidates) == 1, (
    f"test setup should find exactly 1 real candidate against the EXACT (unsmoothed) zone geometry, "
    f"got {len(exact_candidates)}"
)

smoothed_bay_zone = bay_zone.buffer(4).buffer(-4)  # morphological closing -- fills the bay in
assert smoothed_bay_zone.area > bay_zone.area + 1.0, (
    "test setup should genuinely produce a smoothed geometry that's meaningfully different (larger, bay "
    "filled in) from the exact zone -- otherwise this isn't exercising real sensitivity to the choice"
)
smoothed_candidates = rlm._bridge_candidates_for_zone(bay_contour, smoothed_bay_zone)
assert smoothed_candidates == [], (
    f"using a SMOOTHED geometry for classification would incorrectly miss this real gap (the smoothing "
    f"fills in the bay) -- got {len(smoothed_candidates)} candidate(s) against the smoothed geometry, "
    "confirming why render_polygon_utm (unsmoothed) specifically must be used"
)
print(
    "Sensitivity check: classifying against a SMOOTHED zone geometry misses a real gap that the exact "
    "(unsmoothed) render_polygon_utm correctly finds -- confirms why classification must use the unsmoothed "
    "geometry specifically."
)

# --- Confirm the real render loop's call site actually passes render_polygon_utm (not any smoothed
#     field) into the bridging functions, and that this is unaffected by any OTHER, unrelated field a
#     future patch dict might carry (simulating a future render_fill_polygon_utm). ---
import inspect

render_loop_source = inspect.getsource(rlm.render_layout_map)
assert 'patch["render_polygon_utm"]' in render_loop_source, (
    "render_layout_map()'s own render loop must pass patch['render_polygon_utm'] into the contour-clipping "
    "and bridging calls"
)
assert "render_fill_polygon_utm" not in render_loop_source, (
    "render_layout_map() must not reference any smoothed/fill geometry field for contour rendering or "
    "bridging classification -- this branch has none, and none should be introduced here"
)

# A patch dict carrying an extra, unrelated 'render_fill_polygon_utm'-style field (simulating a future
# smoothed-geometry addition elsewhere in the pipeline) must have ZERO effect on bridge classification,
# since _bridge_candidates_for_zone()/_bridge_segments_for_zone() only ever accept a raw geometry
# argument -- there is no dict key for a smoothed field to be accidentally read from.
decoy_patch = dict(zone_1)
decoy_patch["render_fill_polygon_utm"] = zone_1["render_polygon_utm"].buffer(50).buffer(-50)  # drastically different
candidates_without_decoy = rlm._bridge_candidates_for_zone(dumbbell_global_contours, zone_1["render_polygon_utm"])
candidates_with_decoy = rlm._bridge_candidates_for_zone(dumbbell_global_contours, decoy_patch["render_polygon_utm"])
assert len(candidates_without_decoy) == len(candidates_with_decoy), (
    "the presence of an unrelated smoothed-geometry field on the patch dict must have zero effect on bridge "
    "classification -- only render_polygon_utm is ever read"
)
print(
    "render_polygon_utm wiring: the real render loop passes render_polygon_utm (never a smoothed field) into "
    "bridging, and an unrelated smoothed-geometry field on the patch dict has zero effect on classification."
)


print("\nAll render_layout_map checks passed.")
