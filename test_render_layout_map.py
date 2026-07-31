"""
test_render_layout_map.py

Offline (no-network) checks for render_layout_map.py's production-zone
rendering: contour-line texture (contour_lines.py's global contour lines,
clipped per zone at render time against that zone's own render_fill_
polygon_utm), not a filled/outlined shape -- see production_area.py's and
render_layout_map.py's own module docstrings for why the earlier
display_polygon_utm/display_geometry_wgs84 fields were removed entirely
in favor of this, and for render_polygon_utm (waist-split visual
separation) / render_fill_polygon_utm (small-excluded-pocket closing)'s
own separate roles.

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


def _clip_contours_to_zone(contour_lines: list[dict], patch: dict, geometry_key: str = "render_fill_polygon_utm"):
    """Exactly what render_layout_map.py's own rendering loop does per
    production zone -- real shapely intersection of the GLOBAL contour
    lines against that zone's own render_fill_polygon_utm (same CRS, no
    reprojection needed) -- returns the list of non-empty clipped
    geometries. geometry_key defaults to render_fill_polygon_utm (the
    real production behavior); pass "polygon_utm" or "render_polygon_utm"
    to reproduce an earlier clipping behavior for comparison."""
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
# render_fill_polygon_utm is now a real vector buffer(+r).buffer(-r) round-trip on render_polygon_utm --
# a solid zone with no small excluded pockets to close over must come back with essentially the same area,
# not bit-exact (shapely's buffer curve approximation rounds sharp corners by a tiny amount).
single_fill_diff = single_patch["render_fill_polygon_utm"].symmetric_difference(single_patch["polygon_utm"]).area
assert single_fill_diff / single_patch["polygon_utm"].area < 0.01, (
    f"a solid zone with no small excluded pockets to close over must have render_fill_polygon_utm "
    f"essentially equal to polygon_utm (within the buffer round-trip's own curve-approximation tolerance) "
    f"-- got a {single_fill_diff / single_patch['polygon_utm'].area:.4%} relative difference"
)
zone_polygon_buffered = single_patch["render_fill_polygon_utm"].buffer(1e-6)
for piece in clipped_pieces:
    assert zone_polygon_buffered.contains(piece), (
        "every clipped contour segment must lie entirely within the zone's own real boundary "
        "(render_fill_polygon_utm) -- no segment should exist outside it in the clipped output"
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

clipped_1 = _clip_contours_to_zone(dumbbell_global_contours, zone_1)  # default: clips against render_fill_polygon_utm
clipped_2 = _clip_contours_to_zone(dumbbell_global_contours, zone_2)
assert clipped_1 and clipped_2, "both split zones should get at least one clipped contour segment each"

zone_1_buffered = zone_1["render_fill_polygon_utm"].buffer(1e-6)
zone_2_buffered = zone_2["render_fill_polygon_utm"].buffer(1e-6)
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
#     nothing between them in the real, reported geometry. Clipping against render_fill_polygon_utm (the
#     current production behavior) instead of polygon_utm (the pre-fix behavior) is what actually produces
#     a visible blank strip at the waist itself -- not just in the pre-existing gap_cells corridor checked
#     above. render_polygon_utm (the intermediate, unsmoothed waist-split fix) must ALSO still show the
#     same real gap -- render_fill_polygon_utm's closing operation must never bridge it (the whole point of
#     FILL_SMOOTHING_RADIUS_METERS's own hard constraint, see production_area.py's docstring). ---

assert zone_1["polygon_utm"].distance(zone_2["polygon_utm"]) < 1e-9, (
    "test setup should reproduce the confirmed-live bug: polygon_utm for the two split zones must be "
    "directly adjacent with ZERO distance -- reclaim leaves nothing between them"
)
assert zone_1["render_polygon_utm"].distance(zone_2["render_polygon_utm"]) > 0, (
    "render_polygon_utm for the two split zones must have a real gap, unlike polygon_utm"
)
assert zone_1["render_fill_polygon_utm"].distance(zone_2["render_fill_polygon_utm"]) > 0, (
    "render_fill_polygon_utm for the two split zones must ALSO still have a real gap -- the fill-smoothing "
    "closing operation must never bridge a genuine waist-split gap, only small excluded pockets"
)
assert zone_1["render_fill_polygon_utm"].intersection(zone_2["render_fill_polygon_utm"]).area < 1e-9, (
    "render_fill_polygon_utm for the two split zones must not overlap"
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
    f"clipping against render_fill_polygon_utm (current production behavior) must produce ZERO contour "
    f"overlap with the reclaimed waist cells themselves -- got {new_waist_overlap}m of overlap"
)
print(
    f"Waist-gap fix: clipping against the pre-fix polygon_utm draws {round(old_waist_overlap, 1)}m of "
    "contour line directly through the reclaimed waist cells (the confirmed-live bug); clipping against "
    "render_fill_polygon_utm (current production behavior) draws zero."
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
# POST-reclaim polygon_utm -- render_polygon_utm/render_fill_polygon_utm
# both exist purely for display and must play no role in any reported
# number or geometry. Same invariant every prior rendering-only pass in
# this pipeline has needed (see the zones_geojson invariant directly
# above).
# =====================================================================

from rasterio.warp import transform_geom as _transform_geom_check
from shapely.geometry import shape as _shape_check

for zone in (zone_1, zone_2):
    assert zone["render_polygon_utm"].area < zone["polygon_utm"].area, (
        "test sanity check: this is a real split fixture, render_polygon_utm must genuinely be smaller"
    )
    assert zone["area_acres"] == round(zone["polygon_utm"].area / pa.SQUARE_METERS_PER_ACRE, 2), (
        "area_acres must be computed from the full, POST-reclaim polygon_utm, not render_polygon_utm/"
        "render_fill_polygon_utm"
    )
    geometry_utm_reprojected = _shape_check(
        _transform_geom_check("EPSG:4326", dumbbell_dem["crs"], zone["geometry_wgs84"])
    )
    reprojection_diff = geometry_utm_reprojected.symmetric_difference(zone["polygon_utm"]).area
    assert reprojection_diff < 1e-6, (
        f"geometry_wgs84 must reproject back to the full polygon_utm, not the narrower render_polygon_utm/"
        f"render_fill_polygon_utm (symmetric difference area {reprojection_diff})"
    )

# Direct proof that render_polygon_utm/render_fill_polygon_utm play no role in suitability scoring:
# rebuild the same split from scratch, strip BOTH render-only fields off each raw patch entirely before
# scoring, and confirm score_production_areas() produces byte-identical numeric results either way.
fresh_step1 = compute_step1_eligible_cells(dumbbell_dem, _full_extent_boundary(dumbbell_dem), disqualifying_soil_union_utm=None)
fresh_patches = cluster_and_gate(dumbbell_mask, dumbbell_dem, _full_extent_boundary(dumbbell_dem), fresh_step1)
for p in fresh_patches:
    del p["render_polygon_utm"]
    del p["render_fill_polygon_utm"]
stripped_scored = {p["id"]: p for p in score_production_areas(fresh_patches, dumbbell_dem, fresh_step1)}


def _same_score(a: float, b: float) -> bool:
    return a == b or (a != a and b != b)  # NaN != NaN, but both-NaN counts as "identical" here


for zone in (zone_1, zone_2):
    stripped = stripped_scored[zone["id"]]
    assert _same_score(stripped["suitability_score"], zone["suitability_score"]), (
        "suitability_score must be identical whether or not render_polygon_utm/render_fill_polygon_utm are "
        "present on the patch dict"
    )
    assert _same_score(stripped["area_score"], zone["area_score"]) and _same_score(
        stripped["compactness_score"], zone["compactness_score"]
    ), "size_factor's sub-scores must be identical whether or not the render-only fields are present"

print(
    "render_polygon_utm/render_fill_polygon_utm invariant: area_acres, geometry_wgs84 (zones_geojson), and "
    "suitability scoring for both split zones all continue to reflect the full, post-reclaim polygon_utm -- "
    "byte-identical whether or not either render-only field is even present on the patch dict."
)


# =====================================================================
# Fill-smoothing: a small excluded (steep/hydric) pocket entirely inside an otherwise-solid zone must be
# fully closed over in render_fill_polygon_utm (so contour lines drawn against it continue right through
# the pocket, matching the confirmed-live screenshot problem this feature fixes) -- while a real waist-
# split gap (see above) must NEVER be bridged, since it's always wider than FILL_SMOOTHING_RADIUS_METERS
# can reach. This is the "hard constraint": re-run at whatever radius gets chosen.
# =====================================================================

POCKET_SHAPE = (40, 40)
pocket_dem = _sloped_dem(*POCKET_SHAPE)
pocket_boundary = _full_extent_boundary(pocket_dem)

solid_cells = set(_rect_cells(5, 35, 5, 35))  # 30x30 solid block
small_pocket = set(_rect_cells(18, 22, 18, 22))  # 4x4 cells = 20x20m -- well within 2x FILL_SMOOTHING_RADIUS_METERS
pocket_cells = list(solid_cells - small_pocket)
pocket_mask = _mask_from_cells(POCKET_SHAPE, pocket_cells)

pocket_step1 = compute_step1_eligible_cells(pocket_dem, pocket_boundary, disqualifying_soil_union_utm=None)
pocket_patches = cluster_and_gate(pocket_mask, pocket_dem, pocket_boundary, pocket_step1)
assert len(pocket_patches) == 1, f"a solid block with one small interior pocket must stay one cluster, got {len(pocket_patches)}"
pocket_patch = pocket_patches[0]

pocket_footprint = pa._cell_union_footprint(list(small_pocket), pocket_dem)
assert pocket_patch["polygon_utm"].intersection(pocket_footprint).area < 1e-6, (
    "test sanity check: the pocket must genuinely be excluded ground -- polygon_utm must not cover it"
)
fill_recovered_area = pocket_patch["render_fill_polygon_utm"].intersection(pocket_footprint).area
assert abs(fill_recovered_area - pocket_footprint.area) < 1e-6, (
    f"render_fill_polygon_utm must fully close over a small (20x20m) excluded pocket -- recovered "
    f"{fill_recovered_area} of {pocket_footprint.area} sq m"
)

pocket_global_contours = compute_contour_lines(pocket_dem)
pocket_clipped_old = _clip_contours_to_zone(pocket_global_contours, pocket_patch, geometry_key="render_polygon_utm")
pocket_clipped_new = _clip_contours_to_zone(pocket_global_contours, pocket_patch)  # render_fill_polygon_utm
old_pocket_overlap = sum(p.intersection(pocket_footprint).length for p in pocket_clipped_old)
new_pocket_overlap = sum(p.intersection(pocket_footprint).length for p in pocket_clipped_new)
assert old_pocket_overlap < 1e-9, (
    "test sanity check: clipping against render_polygon_utm (pre-fill-smoothing) should leave the pocket "
    "as a real blank gap -- otherwise this isn't reproducing the live screenshot problem"
)
assert new_pocket_overlap > 0, (
    f"clipping against render_fill_polygon_utm must draw real contour line THROUGH the small excluded "
    f"pocket (closed over), got {new_pocket_overlap}m of overlap"
)
print(
    f"Fill-smoothing: a small (20x20m) excluded pocket entirely inside a zone is fully closed over in "
    f"render_fill_polygon_utm (FILL_SMOOTHING_RADIUS_METERS={pa.FILL_SMOOTHING_RADIUS_METERS}m) -- contour "
    f"lines now draw {round(new_pocket_overlap, 1)}m through it instead of leaving a blank gap."
)


# --- Hard constraint, worst case: the tightest real waist-split gap this pipeline's own erosion math can
#     produce (a single-pixel-wide, single-row-long throat, right at the MIN_ZONE_WAIST_METERS threshold,
#     at dem_data.py's fixed 5m production DEM resolution) must survive fill-smoothing fully intact. ---

TIGHT_SHAPE = (30, 20)
tight_dem = _sloped_dem(*TIGHT_SHAPE)
tight_boundary = _full_extent_boundary(tight_dem)
tight_top_lobe = _rect_cells(0, 10, 0, 20)
tight_throat = _rect_cells(10, 11, 10, 11)  # single cell -- as thin a real waist as this pipeline can split on
tight_bottom_lobe = _rect_cells(11, 21, 0, 20)
tight_cells = tight_top_lobe + tight_throat + tight_bottom_lobe
tight_mask = _mask_from_cells(TIGHT_SHAPE, tight_cells)

tight_step1 = compute_step1_eligible_cells(tight_dem, tight_boundary, disqualifying_soil_union_utm=None)
tight_patches = cluster_and_gate(tight_mask, tight_dem, tight_boundary, tight_step1)
assert len(tight_patches) == 2, (
    f"test setup should genuinely trigger a waist split on the tightest possible throat, got "
    f"{len(tight_patches)} cluster(s)"
)
tight_1, tight_2 = tight_patches
tight_render_gap = tight_1["render_polygon_utm"].distance(tight_2["render_polygon_utm"])
tight_fill_gap = tight_1["render_fill_polygon_utm"].distance(tight_2["render_fill_polygon_utm"])
assert tight_render_gap > 2 * pa.FILL_SMOOTHING_RADIUS_METERS, (
    f"test setup should produce a real waist gap ({tight_render_gap}m) comfortably wider than twice "
    f"FILL_SMOOTHING_RADIUS_METERS ({2 * pa.FILL_SMOOTHING_RADIUS_METERS}m) -- otherwise this isn't a "
    "meaningful hard-constraint check"
)
assert tight_fill_gap > 0, (
    f"HARD CONSTRAINT VIOLATION: FILL_SMOOTHING_RADIUS_METERS ({pa.FILL_SMOOTHING_RADIUS_METERS}m) bridges "
    f"the tightest real waist-split gap this pipeline's own erosion math can produce ({tight_render_gap}m) "
    "-- render_fill_polygon_utm for the two zones now touch or overlap, silently defeating the waist split"
)
assert tight_1["render_fill_polygon_utm"].intersection(tight_2["render_fill_polygon_utm"]).area < 1e-9, (
    "HARD CONSTRAINT VIOLATION: render_fill_polygon_utm for the two split zones overlaps at the tightest "
    "possible real waist"
)
print(
    f"Hard constraint: at FILL_SMOOTHING_RADIUS_METERS={pa.FILL_SMOOTHING_RADIUS_METERS}m, even the "
    f"tightest real waist-split gap this pipeline's erosion math can produce ({tight_render_gap}m) survives "
    f"fill-smoothing fully intact (render_fill_polygon_utm gap {tight_fill_gap}m) -- no touch, no overlap."
)


print("\nAll render_layout_map checks passed.")
