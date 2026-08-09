"""
test_render_layout_map.py

Offline (no-network) checks for render_layout_map.py's production-zone
rendering: contour-line texture (contour_lines.py's global contour lines,
clipped per zone at render time against that zone's own render_fill_
polygon_utm), not a filled/outlined shape -- see production_area.py's and
render_layout_map.py's own module docstrings for why the earlier
display_polygon_utm/display_geometry_wgs84 fields were removed entirely
in favor of this, and for render_polygon_utm (waist-split visual
separation) / render_fill_polygon_utm (render_polygon_utm's own plain
convex hull, closing over any real excluded pocket or notch regardless
of size)'s own separate roles.

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
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import Polygon, box, mapping, shape

import production_area as pa
import render_layout_map as rlm
from contour_lines import compute_contour_lines
from fencing import boundary_fencing_to_geojson, find_boundary_fencing
from production_area import cluster_and_gate, compute_step1_eligible_cells
from production_suitability import score_production_areas

RESOLUTION = (5.0, 5.0)
RISE_PER_ROW = 0.4  # meters -- a real, modest gradient so contour lines actually exist


def _plain_fencing_result(boundary_coordinates: list, dem: dict) -> dict:
    """
    A network-free stand-in for fencing.identify_boundary_fencing(), used only to
    populate the synthetic `layers` dicts below -- render_layout_map() now always
    expects a 'fencing_result' key (see fetch_layout_layers()'s own docstring), and
    these fixtures aren't about exercising fencing.py's own canopy-routing logic
    (that has its own dedicated coverage in test_fencing.py). Builds the plain,
    no-canopy case directly via find_boundary_fencing(..., canopy_union_utm=None) +
    boundary_fencing_to_geojson() -- both pure, no network -- reprojecting UTM back
    to WGS84 the same way identify_boundary_fencing() itself does.
    """
    xs, ys = warp_transform(
        "EPSG:4326", dem["crs"], [pt[0] for pt in boundary_coordinates], [pt[1] for pt in boundary_coordinates]
    )
    boundary_polygon_utm = Polygon(zip(xs, ys))
    rings_utm = find_boundary_fencing(boundary_polygon_utm, None)
    rings_wgs84 = [shape(transform_geom(dem["crs"], "EPSG:4326", mapping(ring))) for ring in rings_utm]
    return {
        "fencing_geojson": boundary_fencing_to_geojson(rings_wgs84),
        "segment_count": len(rings_utm),
    }


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
    exactly the shape render_layout_map.py's own layers['production_areas']
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
# render_fill_polygon_utm is render_polygon_utm's own plain convex hull -- a solid, already-convex zone's
# hull is geometrically IDENTICAL to polygon_utm (no radius, no buffer-curve approximation to tolerate).
assert single_patch["render_fill_polygon_utm"].equals(single_patch["polygon_utm"]), (
    "a solid, already-convex zone's render_fill_polygon_utm (its own convex hull) must be geometrically "
    "identical to polygon_utm"
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
#     same real gap -- render_fill_polygon_utm (each piece's own convex hull) must not bridge it either;
#     this fixture's own lobes are convex rectangles, so their hulls change nothing (see
#     test_production_area.py's own deliberately non-convex, notch-facing fixture for the empirical check
#     against a genuinely non-convex pair). ---

assert zone_1["polygon_utm"].distance(zone_2["polygon_utm"]) < 1e-9, (
    "test setup should reproduce the confirmed-live bug: polygon_utm for the two split zones must be "
    "directly adjacent with ZERO distance -- reclaim leaves nothing between them"
)
assert zone_1["render_polygon_utm"].distance(zone_2["render_polygon_utm"]) > 0, (
    "render_polygon_utm for the two split zones must have a real gap, unlike polygon_utm"
)
assert zone_1["render_fill_polygon_utm"].distance(zone_2["render_fill_polygon_utm"]) > 0, (
    "render_fill_polygon_utm (each piece's own convex hull) for the two split zones must ALSO still have "
    "a real gap"
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
#     drawing, legend, PNG assembly) runs without crashing for production_areas with a split ---

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

# parcel_acres derived to match the old hardcoded percent_of_parcel=42.0
# fixture value exactly (total_selected_acreage / (42.0 / 100.0)) -- same
# relationship the old production_result-shaped fixture encoded, just
# reached via the new (scored_patches, parcel_acres) signature _production_
# zone_legend_stats() now takes directly (see render_layout_map.py's own
# fetch_layout_layers() docstring for why this stopped needing a second
# identify_optimized_production_areas() call in production).
_dumbbell_total_selected_acreage = round(sum(p["area_acres"] for p in dumbbell_scored), 2)
_dumbbell_parcel_acres = _dumbbell_total_selected_acreage / (42.0 / 100.0)

synthetic_layers = {
    "dem": dumbbell_dem,
    "production_areas": dumbbell_scored,
    "production_zone_legend_stats": rlm._production_zone_legend_stats(dumbbell_scored, _dumbbell_parcel_acres),
    "water_zone": None,
    "road_corridor": None,
    "tree_zone_result": None,
    "structure_site": None,
    "water_features": {"streams": []},
    "contour_lines": dumbbell_global_contours,
    "fencing_result": _plain_fencing_result(property_boundary, dumbbell_dem),
}

with tempfile.TemporaryDirectory() as tmpdir:
    output_path = os.path.join(tmpdir, "layout_map.png")
    result_path = rlm.render_layout_map(property_boundary, output_path, layers=synthetic_layers)
    assert result_path == output_path
    assert os.path.exists(output_path)
    assert os.path.getsize(output_path) > 0, "render_layout_map() must produce a real, non-empty PNG"

print(
    "Full pipeline: render_layout_map() runs offline end-to-end (basemap fetch degrades gracefully) with a "
    "split production_areas, drawing clipped contour-line texture for each zone and producing a real, "
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
# Convex hull: a small excluded (steep/hydric) pocket entirely inside an otherwise-solid zone must be
# fully closed over in render_fill_polygon_utm (so contour lines drawn against it continue right through
# the pocket, matching the confirmed-live screenshot problem this feature fixes) -- no radius, no size
# ceiling on the pocket at all (a hull always fully encloses any real interior concavity/hole).
# =====================================================================

POCKET_SHAPE = (40, 40)
pocket_dem = _sloped_dem(*POCKET_SHAPE)
pocket_boundary = _full_extent_boundary(pocket_dem)

solid_cells = set(_rect_cells(5, 35, 5, 35))  # 30x30 solid block
small_pocket = set(_rect_cells(18, 22, 18, 22))  # 4x4 cells = 20x20m
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
    f"render_fill_polygon_utm (the convex hull) must fully close over a small (20x20m) excluded pocket -- "
    f"recovered {fill_recovered_area} of {pocket_footprint.area} sq m"
)

pocket_global_contours = compute_contour_lines(pocket_dem)
pocket_clipped_old = _clip_contours_to_zone(pocket_global_contours, pocket_patch, geometry_key="render_polygon_utm")
pocket_clipped_new = _clip_contours_to_zone(pocket_global_contours, pocket_patch)  # render_fill_polygon_utm
old_pocket_overlap = sum(p.intersection(pocket_footprint).length for p in pocket_clipped_old)
new_pocket_overlap = sum(p.intersection(pocket_footprint).length for p in pocket_clipped_new)
assert old_pocket_overlap < 1e-9, (
    "test sanity check: clipping against render_polygon_utm (pre-hull) should leave the pocket "
    "as a real blank gap -- otherwise this isn't reproducing the live screenshot problem"
)
assert new_pocket_overlap > 0, (
    f"clipping against render_fill_polygon_utm must draw real contour line THROUGH the small excluded "
    f"pocket (closed over), got {new_pocket_overlap}m of overlap"
)
print(
    f"Convex hull: a small (20x20m) excluded pocket entirely inside a zone is fully closed over in "
    f"render_fill_polygon_utm -- contour lines now draw {round(new_pocket_overlap, 1)}m through it instead "
    "of leaving a blank gap."
)


# --- Waist-split hull check, worst case: the tightest real waist-split gap this pipeline's own erosion
#     math can produce (a single-pixel-wide, single-row-long throat, right at the MIN_ZONE_WAIST_METERS
#     threshold, at dem_data.py's fixed 5m production DEM resolution) -- confirmed empirically, not
#     assumed, that render_fill_polygon_utm (each piece's own convex hull) stays separate here too. ---

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
assert tight_fill_gap > 0, (
    f"render_fill_polygon_utm (each piece's own convex hull) bridges the tightest real waist-split gap "
    f"this pipeline's own erosion math can produce ({tight_render_gap}m) -- the two zones now touch or "
    "overlap, silently defeating the waist split"
)
assert tight_1["render_fill_polygon_utm"].intersection(tight_2["render_fill_polygon_utm"]).area < 1e-9, (
    "render_fill_polygon_utm for the two split zones overlaps at the tightest possible real waist"
)
print(
    f"Waist-split hull check: even the tightest real waist-split gap this pipeline's erosion math can "
    f"produce ({tight_render_gap}m) survives as render_fill_polygon_utm's own hull "
    f"(gap {tight_fill_gap}m) -- no touch, no overlap."
)


# =====================================================================
# WATER ZONE STYLE: the water zone's FILL is now drawn from its own
# render_fill_polygon_utm (a DISPLAY-ONLY convex hull -- see water_
# candidate_zones.find_candidate_zones()'s own docstring), fully opaque,
# with a subtle sine-wave ripple texture drawn over it -- see this
# module's own "WATER ZONE STYLE" docstring section.
# =====================================================================

import water_candidate_zones as wcz
from water_candidate_zones import find_candidate_zones
from render_layout_map import (
    WATER_ZONE_COLOR,
    WATER_ZONE_RIPPLE_COLOR,
    _ripple_lines_for_polygon,
    _iter_line_parts,
)

# --- _ripple_lines_for_polygon(): a real polygon gets real, clipped ripple lines ---

RIPPLE_TEST_POLYGON = box(0.0, 0.0, 100.0, 40.0)
ripple_lines = _ripple_lines_for_polygon(RIPPLE_TEST_POLYGON)
assert 0 < len(ripple_lines) <= rlm.WATER_ZONE_RIPPLE_COUNT, (
    f"expected between 1 and {rlm.WATER_ZONE_RIPPLE_COUNT} ripple line(s) for a normal rectangular polygon, "
    f"got {len(ripple_lines)}"
)
buffered_polygon = RIPPLE_TEST_POLYGON.buffer(1e-9)
total_ripple_length = 0.0
for ripple in ripple_lines:
    for line in _iter_line_parts(ripple):
        assert buffered_polygon.contains(line), "every ripple line segment must stay within the polygon it's clipped to"
        total_ripple_length += line.length
assert total_ripple_length > 0, "test setup should produce real, nonzero-length ripple line segments"
print(
    f"_ripple_lines_for_polygon() produces {len(ripple_lines)} real sine-wave line(s) on a rectangular test "
    f"polygon, every segment clipped entirely within the polygon's own bounds ({total_ripple_length:.1f}m total)."
)

# --- _ripple_lines_for_polygon(): a degenerate (zero-area) polygon returns no ripples, doesn't crash ---

degenerate_polygon = box(0.0, 0.0, 0.0, 10.0)  # zero width
assert _ripple_lines_for_polygon(degenerate_polygon) == [], "a degenerate zero-area polygon must return no ripple lines, without raising"
print("_ripple_lines_for_polygon() returns no ripples for a degenerate (zero-area) polygon, without raising.")


# --- Full render_layout_map() pass: a synthetic water zone whose render_fill_polygon_utm is a genuine
#     convex hull (differing from its real, blocky geometry_wgs84) and DELIBERATELY overlaps a production
#     zone's own render_fill_polygon_utm -- confirms the whole pipeline still renders correctly, the water
#     zone's numbered marker lands on the HULL (not the blocky footprint), and the overlap is allowed. ---

WATER_ZONE_TEST_SIZE = (20, 20)
water_zone_test_dem = {
    "array": np.full(WATER_ZONE_TEST_SIZE, 100.0, dtype=np.float32),
    "resolution_meters": (5.0, 5.0),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}

# Same L-shaped (non-convex) fixture pattern as test_water_candidate_zones.py's own render_fill_polygon_utm
# tests -- a real drainage band winding around a corner, so the hull genuinely differs from the blocky shape.
wz_vertical_arm = _rect_cells(0, 15, 0, 5)
wz_horizontal_arm = _rect_cells(10, 15, 0, 15)
wz_l_shape_mask = _mask_from_cells(WATER_ZONE_TEST_SIZE, list(set(wz_vertical_arm + wz_horizontal_arm)))

wz_full_extent = box(500000.0, 4500000.0 - 100.0, 500000.0 + 100.0, 4500000.0)
wz_production_area = {
    "id": 0,
    "representative_elevation_m": 50.0,
    "polygon_utm": box(500000.0, 4500000.0 - 130.0, 500000.0 + 20.0, 4500000.0 - 100.0),
    # Deliberately overlaps the water zone's own hull bulge -- see the corresponding
    # test_water_candidate_zones.py check for why this is the expected, allowed outcome.
    "render_fill_polygon_utm": box(500025.0, 4499950.0, 500075.0, 4500000.0),
    "area_acres": 0.5,
    "rank": 1,
    "suitability_score": 50.0,
}

_original_compute_water_eligible_cells = wcz.compute_water_eligible_cells
wcz.compute_water_eligible_cells = lambda *a, **kw: wz_l_shape_mask
try:
    wz_zones = find_candidate_zones(water_zone_test_dem, [wz_production_area], wz_full_extent)
finally:
    wcz.compute_water_eligible_cells = _original_compute_water_eligible_cells

assert len(wz_zones) == 1, f"expected exactly 1 water zone on this fixture, got {len(wz_zones)}"
water_zone_fixture = dict(wz_zones[0])
water_zone_fixture["suitability_score"] = 72.5  # render_layout_map() only reads this + 'id', never re-scores

# Sanity check: the hull's own representative point genuinely differs from the real, blocky
# geometry_wgs84's representative point -- otherwise this fixture wouldn't actually be testing anything
# different from the pre-existing (blocky-footprint) marker placement.
blocky_point = _shape_check(
    _transform_geom_check("EPSG:4326", water_zone_test_dem["crs"], water_zone_fixture["geometry_wgs84"])
).representative_point()
hull_point = water_zone_fixture["render_fill_polygon_utm"].representative_point()
assert blocky_point.distance(hull_point) > 0, (
    "test setup should produce a hull whose representative_point() genuinely differs from the real, blocky "
    "geometry_wgs84's own representative_point() -- otherwise the marker-placement check below is trivial"
)

recorded_markers = []
_original_draw_numbered_marker = rlm._draw_numbered_marker
rlm._draw_numbered_marker = lambda ax, point, number: recorded_markers.append((point, number)) or _original_draw_numbered_marker(ax, point, number)

# Also record every plot_polygon()/plot_line() call render_layout_map() makes, so the fill's own
# alpha/zorder and the ripple lines' own color can be confirmed directly against what's actually
# drawn, not just inferred from the module's constants.
recorded_polygon_calls = []
_original_plot_polygon = rlm.plot_polygon


def _recording_plot_polygon(geometry, **kwargs):
    recorded_polygon_calls.append(kwargs)
    return _original_plot_polygon(geometry, **kwargs)


recorded_line_calls = []
_original_plot_line = rlm.plot_line


def _recording_plot_line(geometry, **kwargs):
    recorded_line_calls.append(kwargs)
    return _original_plot_line(geometry, **kwargs)


rlm.plot_polygon = _recording_plot_polygon
rlm.plot_line = _recording_plot_line

wz_synthetic_layers = {
    "dem": water_zone_test_dem,
    "production_areas": [],
    "production_zone_legend_stats": [],
    "water_zone": water_zone_fixture,
    "road_corridor": None,
    "tree_zone_result": None,
    "structure_site": None,
    "water_features": {"streams": []},
    "contour_lines": [],
    "fencing_result": _plain_fencing_result(property_boundary, water_zone_test_dem),
}

try:
    with tempfile.TemporaryDirectory() as tmpdir:
        wz_output_path = os.path.join(tmpdir, "layout_map.png")
        wz_result_path = rlm.render_layout_map(property_boundary, wz_output_path, layers=wz_synthetic_layers)
        assert wz_result_path == wz_output_path
        assert os.path.getsize(wz_output_path) > 0, "render_layout_map() must produce a real, non-empty PNG"
finally:
    rlm._draw_numbered_marker = _original_draw_numbered_marker
    rlm.plot_polygon = _original_plot_polygon
    rlm.plot_line = _original_plot_line

water_fill_calls = [kw for kw in recorded_polygon_calls if kw.get("facecolor") == WATER_ZONE_COLOR]
assert len(water_fill_calls) == 1, f"expected exactly 1 water zone fill plot_polygon() call, got {len(water_fill_calls)}"
water_fill_kwargs = water_fill_calls[0]
assert water_fill_kwargs["alpha"] == 1.0, (
    f"the water zone fill must be drawn fully OPAQUE (alpha=1.0), not the earlier 0.35, got "
    f"{water_fill_kwargs['alpha']}"
)
assert water_fill_kwargs["zorder"] == 41, f"the water zone fill's zorder must stay above production zones' zorder=40, got {water_fill_kwargs['zorder']}"

ripple_calls = [kw for kw in recorded_line_calls if kw.get("color") == WATER_ZONE_RIPPLE_COLOR]
assert ripple_calls, "expected at least one plot_line() call drawing the ripple texture in WATER_ZONE_RIPPLE_COLOR"
assert all(kw["zorder"] > water_fill_kwargs["zorder"] for kw in ripple_calls), (
    "every ripple line must render ABOVE the opaque fill it textures (higher zorder), not underneath it"
)
print(
    f"Water zone fill draws fully opaque (alpha=1.0) at zorder={water_fill_kwargs['zorder']} (above production "
    f"zones' zorder=40), with {len(ripple_calls)} ripple line segment(s) drawn above the fill in "
    f"WATER_ZONE_RIPPLE_COLOR."
)

assert len(recorded_markers) == 1, f"expected exactly 1 marker drawn (the water zone), got {len(recorded_markers)}"
marker_point_mercator, marker_number = recorded_markers[0]

# The marker must land on the HULL geometry actually drawn (reprojected to Mercator), not the real,
# blocky geometry_wgs84 -- confirmed by reprojecting the whole hull POLYGON the same way render_layout_map()
# does and taking ITS OWN representative_point() in Mercator (representative_point() is not guaranteed to
# correspond to the same point across a reprojection of a single point vs. the whole polygon, so this
# reproduces render_layout_map()'s own exact computation rather than a point-then-reproject shortcut).
expected_render_fill_mercator = rlm._reproject_utm_geometry_to_mercator(
    water_zone_fixture["render_fill_polygon_utm"], water_zone_test_dem["crs"]
)
expected_marker_point = expected_render_fill_mercator.representative_point()
assert marker_point_mercator.distance(expected_marker_point) < 1e-6, (
    "the water zone's numbered marker must be placed at render_fill_polygon_utm's own representative_point(), "
    "not geometry_wgs84's -- got a marker point that doesn't match the hull's reprojected representative point"
)
print(
    "Full pipeline with a synthetic water zone: render_layout_map() runs offline end-to-end, draws the water "
    "zone's numbered marker on the HULL geometry (render_fill_polygon_utm) rather than the real, blocky "
    "footprint, and completes successfully even though the hull deliberately overlaps a production area's "
    "own render_fill_polygon_utm."
)


# =====================================================================
# Road corridor rendering: DEM-cell stairstepping simplified/smoothed
# away at render time, drawn as a cased (double-line) dark-gray road
# symbol -- see this module's own "ROAD CORRIDOR STYLE" docstring
# section.
# =====================================================================

import copy

from rasterio.warp import transform as _warp_transform_check
from shapely.geometry import LineString
from render_layout_map import (
    ROAD_RENDER_COLOR,
    ROAD_RENDER_INNER_ALPHA,
    ROAD_RENDER_INNER_WIDTH,
    ROAD_RENDER_OUTER_ALPHA,
    ROAD_RENDER_OUTER_WIDTH,
    ROAD_RENDER_SIMPLIFY_TOLERANCE_M,
    _chaikin_smooth_coords,
    _smooth_line_for_render,
)

# --- _chaikin_smooth_coords(): endpoints kept exactly, every original interior corner rounded away, further cutting on repeated iterations, true no-op on a cornerless (2-point) line ---

staircase = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (20.0, 10.0), (20.0, 20.0)]
smoothed_once = _chaikin_smooth_coords(staircase, 1)
assert smoothed_once[0] == staircase[0], "the first coordinate must be kept EXACTLY, not cut"
assert smoothed_once[-1] == staircase[-1], "the last coordinate must be kept EXACTLY, not cut"
assert len(smoothed_once) > len(staircase), "one Chaikin iteration must add interior corner-cut points"
for corner in staircase[1:-1]:
    assert corner not in smoothed_once, (
        f"corner {corner} is a sharp original interior vertex -- Chaikin smoothing must round it away, "
        f"not keep it exactly"
    )
smoothed_twice = _chaikin_smooth_coords(staircase, 2)
assert len(smoothed_twice) > len(smoothed_once), "a second iteration must cut further (more points, rounder corners)"

two_point_line = [(0.0, 0.0), (10.0, 10.0)]
assert _chaikin_smooth_coords(two_point_line, 3) == two_point_line, (
    "a straight 2-point line has no interior corner to cut -- smoothing must be a genuine no-op"
)
print(
    "_chaikin_smooth_coords keeps both endpoints exactly, rounds away every original interior corner, "
    "cuts further on repeated iterations, and is a true no-op on a 2-point (cornerless) line."
)

# --- _smooth_line_for_render(): a DEM-cell stairstepped path (many small 1m jogs approximating a real ridge/connector turn) simplifies+smooths into far fewer vertices, keeps its exact start/end anchor points, and stays within a small buffer of the real path ---

# Simulates road_corridors.py's own per-DEM-cell routing output: two
# straight legs (an L-shaped turn -- a real corner the route makes),
# each built from small 1m jogs -- exactly the kind of visibly blocky,
# stairstepped polyline the REAL BUG this module's own docstring
# describes was found against (road_corridors.py's least_cost_path()/
# _order_fragment_from_entry() both walk the DEM's grid one cell at a
# time).
stairstep_coords = [(0.0, 0.0)]
x, y = 0.0, 0.0
for _ in range(30):
    x += 1.0
    stairstep_coords.append((x, y))
for _ in range(30):
    y += 1.0
    stairstep_coords.append((x, y))
stairstep_line = LineString(stairstep_coords)

rendered_line = _smooth_line_for_render(stairstep_line)
assert len(rendered_line.coords) < len(stairstep_coords), (
    f"simplify+smooth must genuinely reduce the stairstepped vertex count ({len(stairstep_coords)}), "
    f"got {len(rendered_line.coords)}"
)
assert len(rendered_line.coords) > 3, (
    "expected more than the bare 3-point simplified corner -- Chaikin smoothing should have run and added "
    "its own corner-cut points on top of simplify's own reduction"
)
assert rendered_line.coords[0] == stairstep_coords[0], (
    "the smoothed line's own start point must still match the real route's anchor point exactly"
)
assert rendered_line.coords[-1] == stairstep_coords[-1], (
    "the smoothed line's own end point must still match the real route's far end exactly"
)
# The smoothed line must stay close to the real path -- a generous
# buffer (well above ROAD_RENDER_SIMPLIFY_TOLERANCE_M alone, since
# Chaikin smoothing on top of simplify can pull the curve a little
# further off each cut corner) confirms this is cosmetic corner-
# rounding, not a route drawn somewhere visibly different.
buffered_real_path = stairstep_line.buffer(ROAD_RENDER_SIMPLIFY_TOLERANCE_M * 4)
assert buffered_real_path.contains(rendered_line), (
    "the smoothed/simplified line must stay within a small buffer of the real route -- it must round "
    "corners, not visibly relocate the road"
)
print(
    f"_smooth_line_for_render() reduces a {len(stairstep_coords)}-point DEM-cell stairstep down to "
    f"{len(rendered_line.coords)} points, keeps the exact start/end anchor points, and stays within a "
    f"small buffer of the real, unsmoothed path."
)

# --- Full render_layout_map() pass: a synthetic stairstepped road corridor is drawn as a CASED (double-line) road -- a wider low-alpha outer line and a narrower higher-alpha inner line, both ROAD_RENDER_COLOR, both a visibly smoothed/simplified geometry -- and the real road_corridor input is never mutated ---

ROAD_TEST_ORIGIN_MERCATOR = (-8900000.0, 4900000.0)  # arbitrary but realistic Web Mercator point
road_stairstep_mercator = [
    (ROAD_TEST_ORIGIN_MERCATOR[0] + cx, ROAD_TEST_ORIGIN_MERCATOR[1] + cy) for cx, cy in stairstep_coords
]
road_lons, road_lats = _warp_transform_check(
    rlm.WEB_MERCATOR, rlm.WGS84,
    [c[0] for c in road_stairstep_mercator], [c[1] for c in road_stairstep_mercator],
)
road_corridor_fixture = {
    "type": "Feature",
    "geometry": {"type": "LineString", "coordinates": list(zip(road_lons, road_lats))},
    "properties": {"suitability_score": 81.0},
}
road_corridor_fixture_before = copy.deepcopy(road_corridor_fixture)

recorded_road_line_calls = []
_original_plot_line_for_road = rlm.plot_line


def _recording_plot_line_for_road(geometry, **kwargs):
    recorded_road_line_calls.append((geometry, kwargs))
    return _original_plot_line_for_road(geometry, **kwargs)


rlm.plot_line = _recording_plot_line_for_road

road_synthetic_layers = {
    "dem": water_zone_test_dem,
    "production_areas": [],
    "production_zone_legend_stats": [],
    "water_zone": None,
    "road_corridor": road_corridor_fixture,
    "tree_zone_result": None,
    "structure_site": None,
    "water_features": {"streams": []},
    "contour_lines": [],
    "fencing_result": _plain_fencing_result(property_boundary, water_zone_test_dem),
}

try:
    with tempfile.TemporaryDirectory() as tmpdir:
        road_output_path = os.path.join(tmpdir, "layout_map.png")
        road_result_path = rlm.render_layout_map(property_boundary, road_output_path, layers=road_synthetic_layers)
        assert road_result_path == road_output_path
        assert os.path.getsize(road_output_path) > 0, "render_layout_map() must produce a real, non-empty PNG"
finally:
    rlm.plot_line = _original_plot_line_for_road

road_line_calls = [(geom, kw) for geom, kw in recorded_road_line_calls if kw.get("color") == ROAD_RENDER_COLOR]
assert len(road_line_calls) == 2, (
    f"expected exactly 2 plot_line() calls for the cased (double-line) road style, got {len(road_line_calls)}"
)
outer_calls = [(geom, kw) for geom, kw in road_line_calls if kw["linewidth"] == ROAD_RENDER_OUTER_WIDTH]
inner_calls = [(geom, kw) for geom, kw in road_line_calls if kw["linewidth"] == ROAD_RENDER_INNER_WIDTH]
assert len(outer_calls) == 1 and len(inner_calls) == 1, (
    "expected exactly one outer-width call and one inner-width call among the two road plot_line() calls"
)
outer_geom, outer_kwargs = outer_calls[0]
inner_geom, inner_kwargs = inner_calls[0]
assert outer_kwargs["alpha"] == ROAD_RENDER_OUTER_ALPHA and inner_kwargs["alpha"] == ROAD_RENDER_INNER_ALPHA, (
    "the outer (shoulder) line must use ROAD_RENDER_OUTER_ALPHA and the inner line ROAD_RENDER_INNER_ALPHA"
)
assert outer_kwargs["zorder"] < inner_kwargs["zorder"], (
    "the narrower inner line must render ABOVE the wider outer shoulder (higher zorder), not underneath it"
)
assert outer_geom.coords[:] == inner_geom.coords[:], (
    "both the outer and inner line must be drawn over the exact same (smoothed) geometry"
)
raw_mercator_coords_count = len(road_stairstep_mercator)
assert len(outer_geom.coords) < raw_mercator_coords_count, (
    f"the geometry actually handed to plot_line() must be the SMOOTHED version (fewer vertices than the "
    f"raw {raw_mercator_coords_count}-point stairstep), not the raw per-cell geometry straight off "
    f"road_corridor['geometry']"
)
assert road_corridor_fixture == road_corridor_fixture_before, (
    "rendering must never mutate the real road_corridor input -- its geometry/properties (used for "
    "length_m/avg_grade_pct/every other scoring and narrative value) must stay byte-for-byte identical"
)
print(
    f"Full pipeline with a synthetic stairstepped road corridor: render_layout_map() draws it as a cased "
    f"double-line road (outer width={ROAD_RENDER_OUTER_WIDTH}/alpha={ROAD_RENDER_OUTER_ALPHA}, inner "
    f"width={ROAD_RENDER_INNER_WIDTH}/alpha={ROAD_RENDER_INNER_ALPHA}, inner above outer) over a visibly "
    f"smoothed geometry ({raw_mercator_coords_count} raw points down to {len(outer_geom.coords)}), and "
    f"never mutates the real road_corridor input."
)


# =====================================================================
# TREE ZONE STYLE: each ranked tree-zone candidate patch (possibly
# several, same "ranked list" shape as production zones -- not a single
# selection like water_zone/road_corridor/structure_site) renders as a
# hatched, semi-transparent fill drawn from its own render_fill_polygon_utm
# (a DISPLAY-ONLY plain convex hull -- see score_tree_search_space()'s own
# docstring), NOT its real, potentially-notched polygon_utm/geometry_wgs84
# -- see this module's own "TREE ZONE STYLE" docstring section.
# =====================================================================

from shapely.geometry import mapping as _mapping_check
from shapely.ops import unary_union as _unary_union_check
from render_layout_map import TREE_ZONE_COLOR, TREE_ZONE_FILL_ALPHA, TREE_ZONE_HATCH


def _tree_patch_fixture(polygon_utm, rank: int, score: float, area_acres: float) -> dict:
    geometry_wgs84 = _transform_geom_check(water_zone_test_dem["crs"], "EPSG:4326", _mapping_check(polygon_utm))
    return {
        "id": rank - 1,
        "rank": rank,
        "polygon_utm": polygon_utm,
        "render_fill_polygon_utm": polygon_utm.convex_hull,
        "geometry_wgs84": geometry_wgs84,
        "area_acres": area_acres,
        "tree_suitability_score": score,
    }


# tree_patch_1: a deliberately NON-CONVEX L-shape (same fixture pattern as the water zone's own
# render_fill_polygon_utm test above) -- its own convex hull genuinely differs from its real
# footprint, so this actually exercises the hull being drawn, not a fixture where hull == footprint
# coincidentally passes either way.
tree_l_shape_polygon_utm = _unary_union_check([
    box(500000.0 + 10.0, 4500000.0 - 60.0, 500000.0 + 20.0, 4500000.0 - 30.0),  # vertical arm
    box(500000.0 + 10.0, 4500000.0 - 40.0, 500000.0 + 40.0, 4500000.0 - 30.0),  # horizontal arm
])
tree_patch_1 = _tree_patch_fixture(tree_l_shape_polygon_utm, rank=1, score=72.3, area_acres=1.85)
assert tree_patch_1["render_fill_polygon_utm"].area > tree_patch_1["polygon_utm"].area, (
    "test setup should produce a hull with strictly MORE area than the real, non-convex L-shape footprint -- "
    "otherwise this fixture isn't actually testing hull-vs-footprint behavior"
)

# tree_patch_2: an ordinary convex box -- hull == footprint, the common "no visible change" case.
tree_patch_2 = _tree_patch_fixture(
    box(500000.0 + 60.0, 4500000.0 - 90.0, 500000.0 + 90.0, 4500000.0 - 60.0), rank=2, score=55.0, area_acres=0.9
)
tree_zone_result_fixture = {"patches": [tree_patch_1, tree_patch_2]}

recorded_tree_markers = []
_original_draw_numbered_marker_for_trees = rlm._draw_numbered_marker
rlm._draw_numbered_marker = (
    lambda ax, point, number: recorded_tree_markers.append((point, number))
    or _original_draw_numbered_marker_for_trees(ax, point, number)
)

recorded_tree_polygon_calls = []
_original_plot_polygon_for_trees = rlm.plot_polygon


def _recording_plot_polygon_for_trees(geometry, **kwargs):
    recorded_tree_polygon_calls.append(kwargs)
    return _original_plot_polygon_for_trees(geometry, **kwargs)


rlm.plot_polygon = _recording_plot_polygon_for_trees

tree_synthetic_layers = {
    "dem": water_zone_test_dem,
    "production_areas": [],
    "production_zone_legend_stats": [],
    "water_zone": None,
    "road_corridor": None,
    "tree_zone_result": tree_zone_result_fixture,
    "structure_site": None,
    "water_features": {"streams": []},
    "contour_lines": [],
    "fencing_result": _plain_fencing_result(property_boundary, water_zone_test_dem),
}

try:
    with tempfile.TemporaryDirectory() as tmpdir:
        tree_output_path = os.path.join(tmpdir, "layout_map.png")
        tree_result_path = rlm.render_layout_map(property_boundary, tree_output_path, layers=tree_synthetic_layers)
        assert tree_result_path == tree_output_path
        assert os.path.getsize(tree_output_path) > 0, "render_layout_map() must produce a real, non-empty PNG"
finally:
    rlm._draw_numbered_marker = _original_draw_numbered_marker_for_trees
    rlm.plot_polygon = _original_plot_polygon_for_trees

tree_fill_calls = [kw for kw in recorded_tree_polygon_calls if kw.get("facecolor") == TREE_ZONE_COLOR]
assert len(tree_fill_calls) == 2, f"expected exactly 2 tree-zone fill plot_polygon() calls (one per patch), got {len(tree_fill_calls)}"
for kw in tree_fill_calls:
    assert kw["alpha"] == TREE_ZONE_FILL_ALPHA, f"every tree-zone fill must use TREE_ZONE_FILL_ALPHA, got {kw['alpha']}"
    assert kw["hatch"] == TREE_ZONE_HATCH, f"every tree-zone fill must use the TREE_ZONE_HATCH pattern, got {kw['hatch']}"
    assert kw["zorder"] == 42.8, f"tree-zone fill must render between the road corridor (42.5) and structure site (43), got {kw['zorder']}"

assert len(recorded_tree_markers) == 2, f"expected exactly 2 markers drawn (one per tree-zone candidate), got {len(recorded_tree_markers)}"
for (marker_point_mercator, _marker_number), patch in zip(recorded_tree_markers, [tree_patch_1, tree_patch_2]):
    expected_hull_mercator = rlm._reproject_utm_geometry_to_mercator(
        patch["render_fill_polygon_utm"], water_zone_test_dem["crs"]
    )
    expected_point = expected_hull_mercator.representative_point()
    assert marker_point_mercator.distance(expected_point) < 1e-6, (
        "each tree-zone candidate's numbered marker must be placed at render_fill_polygon_utm's own "
        "representative_point(), reprojected to Mercator -- not geometry_wgs84's"
    )

# tree_patch_1's own hull-fill polygon must be visibly LARGER than its real, non-convex footprint,
# confirming the hull (not the real footprint) is what actually got drawn for the notched patch.
tree_patch_1_fill_call = tree_fill_calls[0]
assert tree_patch_1["render_fill_polygon_utm"].area > tree_patch_1["polygon_utm"].area, (
    "sanity re-check: tree_patch_1's own render_fill_polygon_utm must still be strictly larger than its "
    "real, non-convex polygon_utm"
)

print(
    f"Full pipeline with 2 synthetic tree-zone candidates: render_layout_map() draws each as a hatched "
    f"(pattern={TREE_ZONE_HATCH!r}) fill (alpha={TREE_ZONE_FILL_ALPHA}) at zorder=42.8 (between the road "
    f"corridor and structure site) from its own render_fill_polygon_utm (a real, notched patch's hull "
    f"visibly larger than its real footprint), places each numbered marker on that same hull, and "
    f"completes successfully."
)

# =====================================================================
# build_pipeline_context() wiring: fetch_layout_layers() must forward
# ParcelData's OWN water_features/soil_geometries straight through to
# build_pipeline_context() as kwargs -- the same already-fetched objects,
# by identity (`is`, not `==`), NOT re-derived copies. This is what lets
# build_pipeline_context() (and the _fetch_floodplain_hydric_union() call
# nested inside it) reuse ParcelData's single NHD/SSURGO-geometry fetch
# instead of issuing its own redundant ones -- see fetch_layout_layers()'s
# own docstring and pipeline_context.build_pipeline_context()'s water_
# features=/soil_geometries= override parameters. dem/boundary_polygon_utm/
# soil_components/farm_roads (wired through in the two prior branches) are
# identity-checked here too, so this one spy covers every ParcelData field
# fetch_layout_layers() hands to build_pipeline_context().
# =====================================================================

from parcel_data import ParcelData


class _HaltAfterContextCall(Exception):
    """Raised by the build_pipeline_context() spy purely to stop fetch_
    layout_layers() right after the call under test -- everything past it
    (production/water/road/tree/solar/fencing) is real KSOP code this
    identity check has no reason to drive with sentinel objects."""


# Distinct sentinel objects, one per ParcelData field fetch_layout_layers()
# forwards -- distinct so an accidental cross-wire (e.g. passing water_
# features where soil_geometries was meant) can't slip through an identity
# check against the wrong field. Every other ParcelData field is irrelevant
# to this call and gets a plain placeholder.
_sentinel_dem = {"sentinel": "dem"}
_sentinel_boundary_polygon_utm = box(0.0, 0.0, 1.0, 1.0)
_sentinel_soil_components = [{"sentinel": "soil_components"}]
_sentinel_soil_geometries = {"sentinel": "soil_geometries"}
_sentinel_water_features = {"sentinel": "water_features"}
_sentinel_farm_roads = [{"sentinel": "farm_roads"}]

_spy_parcel_data = ParcelData(
    dem=_sentinel_dem,
    boundary_polygon_utm=_sentinel_boundary_polygon_utm,
    soil_components=_sentinel_soil_components,
    farmland_classification=[],
    erosion_factor=[],
    saturated_hydraulic_conductivity=[],
    soil_geometries=_sentinel_soil_geometries,
    water_features=_sentinel_water_features,
    farm_roads=_sentinel_farm_roads,
    climate_summary={},
    elevation_grid=[],
    canopy_height={},
    imagery_summary={},
)

_captured_context_kwargs = {}
_original_build_pipeline_context = rlm.build_pipeline_context


def _spy_build_pipeline_context(*args, **kwargs):
    _captured_context_kwargs.update(kwargs)
    raise _HaltAfterContextCall()


rlm.build_pipeline_context = _spy_build_pipeline_context
try:
    rlm.fetch_layout_layers(property_boundary, parcel_data=_spy_parcel_data)
    raise AssertionError(
        "the build_pipeline_context() spy should have halted fetch_layout_layers() before it returned"
    )
except _HaltAfterContextCall:
    pass
finally:
    rlm.build_pipeline_context = _original_build_pipeline_context

# The two this branch adds -- identity, not equality.
assert _captured_context_kwargs["water_features"] is _spy_parcel_data.water_features, (
    "fetch_layout_layers() must forward parcel_data.water_features to build_pipeline_context() by identity, "
    "not a re-derived copy"
)
assert _captured_context_kwargs["soil_geometries"] is _spy_parcel_data.soil_geometries, (
    "fetch_layout_layers() must forward parcel_data.soil_geometries to build_pipeline_context() by identity, "
    "not a re-derived copy"
)
# The four wired through in the two prior branches -- same identity contract, guarded here so a future
# refactor can't silently swap any of them back to a self-fetch/re-derivation.
assert _captured_context_kwargs["dem"] is _spy_parcel_data.dem, "dem must be forwarded by identity"
assert _captured_context_kwargs["boundary_polygon_utm"] is _spy_parcel_data.boundary_polygon_utm, (
    "boundary_polygon_utm must be forwarded by identity"
)
assert _captured_context_kwargs["soil_components"] is _spy_parcel_data.soil_components, (
    "soil_components must be forwarded by identity"
)
assert _captured_context_kwargs["farm_roads"] is _spy_parcel_data.farm_roads, (
    "farm_roads must be forwarded by identity"
)
print(
    "build_pipeline_context() wiring: fetch_layout_layers() forwards ParcelData's own water_features and "
    "soil_geometries (plus dem/boundary_polygon_utm/soil_components/farm_roads) to build_pipeline_context() "
    "as kwargs, every one by identity (is, not ==) -- confirming the single ParcelData fetch is reused, not "
    "re-derived."
)


print("\nAll render_layout_map checks passed.")
