"""
test_render_layout_map.py

Offline (no-network) checks for render_layout_map.py's production-zone
rendering: contour-line texture (contour_lines.py's global contour lines,
clipped per zone at render time against that zone's own render_fill_
polygon_utm), not a filled/outlined shape -- see production_area.py's and
render_layout_map.py's own module docstrings for why the earlier
display_polygon_utm/display_geometry_wgs84 fields were removed entirely
in favor of this, and for render_fill_polygon_utm's own role: a bounded
morphological OPENING of the cluster's cell mask clipped to polygon_utm,
which severs a waist-split pinch (drawing the visual gap) and, being
anti-extensive, leaves any excluded interior pocket open rather than
closing over it.

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

import json
import os
import tempfile
from contextlib import ExitStack
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, Polygon, box, mapping, shape
from shapely.ops import unary_union

import production_area as pa
import render_layout_map as rlm
from contour_lines import compute_contour_lines
from fencing import (
    boundary_fencing_to_geojson,
    find_boundary_fencing,
    tree_zone_fencing_to_geojson,
    water_zone_fencing_to_geojson,
)
from production_area import cluster_and_gate, compute_step1_eligible_cells
from production_suitability import score_production_areas

RESOLUTION = (5.0, 5.0)
RISE_PER_ROW = 0.4  # meters -- a real, modest gradient so contour lines actually exist


def _capture_legend_labels(layers: dict, boundary_coordinates: list) -> list:
    """Runs a full render_layout_map() pass and returns the ordered list of
    legend labels the icon legend was actually built with -- read off the real
    matplotlib Legend the render creates, not inferred. Empty list when the map
    drew nothing (no legend, no frame). Replaces the old ax.text-capture helpers:
    the legend is now a real ax.legend(), so we intercept Axes.legend(), delegate
    to the real implementation, and read the resulting Legend's own text labels."""
    captured: list = []
    _orig_legend = rlm.plt.Axes.legend

    def _recording_legend(self, *args, **kwargs):
        legend = _orig_legend(self, *args, **kwargs)
        captured[:] = [text.get_text() for text in legend.get_texts()]
        return legend

    with mock_patch.object(rlm.plt.Axes, "legend", _recording_legend):
        with tempfile.TemporaryDirectory() as tmpdir:
            rlm.render_layout_map(boundary_coordinates, os.path.join(tmpdir, "layout_map.png"), layers=layers)
    return captured


def _plain_fencing_result(boundary_coordinates: list, dem: dict) -> dict:
    """
    A network-free stand-in for fencing.identify_boundary_fencing(), used only to
    populate the synthetic `layers` dicts below -- render_layout_map() now always
    expects a 'fencing_result' key (see fetch_layout_layers()'s own docstring), and
    these fixtures aren't about exercising fencing.py's own developed-footprint logic
    (that has its own dedicated coverage in test_fencing.py). Builds the plain,
    bare-property case directly via find_boundary_fencing() with empty inputs +
    boundary_fencing_to_geojson() -- both pure, no network -- reprojecting UTM back
    to WGS84 the same way identify_boundary_fencing() itself does.
    """
    xs, ys = warp_transform(
        "EPSG:4326", dem["crs"], [pt[0] for pt in boundary_coordinates], [pt[1] for pt in boundary_coordinates]
    )
    boundary_polygon_utm = Polygon(zip(xs, ys))
    # Bare-property case: every developed-footprint/zone input empty/None, so find_boundary_fencing()
    # collapses to the boundary's own ring (its degenerate fallback).
    rings_utm = find_boundary_fencing(
        boundary_polygon_utm,
        production_zone_polygons_utm=[],
        structure_site_polygon_utm=None,
        road_corridor_cell_footprint_polygon_utm=None,
        water_zone_polygon_utm=None,
        tree_zone_polygons_utm=[],
    )
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
    real production behavior); pass "polygon_utm" to reproduce an earlier
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
# render_fill_polygon_utm is now a bounded morphological OPENING clipped to polygon_utm (production's
# disc opening replaced the old convex hull), so it is CONTAINED WITHIN polygon_utm rather than equal to
# any hull -- it may be smaller (the opening trims/rounds) but never claims ground outside polygon_utm.
assert single_patch["polygon_utm"].buffer(1e-6).contains(single_patch["render_fill_polygon_utm"]), (
    "render_fill_polygon_utm (a bounded opening clipped to polygon_utm) must be contained within polygon_utm"
)
assert single_patch["render_fill_polygon_utm"].area <= single_patch["polygon_utm"].area * (1 + 1e-9) + 1e-6, (
    "the opening can never exceed polygon_utm's area"
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
#     above. render_fill_polygon_utm (each piece's own bounded opening) must NOT bridge the waist either;
#     the opening operates on the survivors of its own erosion, so a neck severed by the erosion stays
#     severed (see test_production_area.py's own deliberately non-convex, notch-facing fixture for the
#     empirical check against a genuinely non-convex pair). ---

assert zone_1["polygon_utm"].distance(zone_2["polygon_utm"]) < 1e-9, (
    "test setup should reproduce the confirmed-live bug: polygon_utm for the two split zones must be "
    "directly adjacent with ZERO distance -- reclaim leaves nothing between them"
)
assert zone_1["render_fill_polygon_utm"].distance(zone_2["render_fill_polygon_utm"]) > 0, (
    "render_fill_polygon_utm (each piece's own bounded opening) for the two split zones must have a real "
    "gap -- an opening cannot bridge the severed waist"
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

# No per-feature legend-stats key anymore: the icon legend carries no
# per-feature data, so render_layout_map() neither reads nor is passed any
# display-stat derivation (see render_layout_map.py's own LEGEND docstring
# section and fetch_layout_layers()'s docstring).
synthetic_layers = {
    "dem": dumbbell_dem,
    "production_areas": dumbbell_scored,
    "water_zone": None,
    "road_corridor": [],
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

# The two split production zones collapse into ONE "Production Areas" legend
# entry (no per-zone identity, no numbering); the plain boundary fence adds the
# single "Fencing" entry. KSOP order puts production before fencing.
_split_legend_labels = _capture_legend_labels(synthetic_layers, property_boundary)
assert _split_legend_labels == [rlm.LEGEND_LABEL_PRODUCTION, rlm.LEGEND_LABEL_FENCING], (
    f"two production zones + a boundary fence must yield exactly one Production Areas entry and one "
    f"Fencing entry, in KSOP order -- got {_split_legend_labels}"
)

print(
    "Full pipeline: render_layout_map() runs offline end-to-end (basemap fetch degrades gracefully) with a "
    "split production_areas, drawing clipped contour-line texture for each zone, collapsing both zones into a "
    "single 'Production Areas' legend entry, and producing a real, non-empty PNG."
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
# POST-reclaim polygon_utm -- render_fill_polygon_utm plays no role in any
# reported number or geometry (it feeds only rendering and downstream
# candidate exclusion, never this patch's own reported area/geometry/score).
# Same invariant every prior rendering-only pass in this pipeline has needed
# (see the zones_geojson invariant directly above).
# =====================================================================

from rasterio.warp import transform_geom as _transform_geom_check
from shapely.geometry import shape as _shape_check

for zone in (zone_1, zone_2):
    assert zone["render_fill_polygon_utm"].area < zone["polygon_utm"].area, (
        "test sanity check: this is a real split fixture, the opening's render_fill_polygon_utm must "
        "genuinely be smaller than the full polygon_utm"
    )
    assert zone["area_acres"] == round(zone["polygon_utm"].area / pa.SQUARE_METERS_PER_ACRE, 2), (
        "area_acres must be computed from the full, POST-reclaim polygon_utm, not render_fill_polygon_utm"
    )
    geometry_utm_reprojected = _shape_check(
        _transform_geom_check("EPSG:4326", dumbbell_dem["crs"], zone["geometry_wgs84"])
    )
    reprojection_diff = geometry_utm_reprojected.symmetric_difference(zone["polygon_utm"]).area
    assert reprojection_diff < 1e-6, (
        f"geometry_wgs84 must reproject back to the full polygon_utm, not the narrower "
        f"render_fill_polygon_utm (symmetric difference area {reprojection_diff})"
    )

# Direct proof that render_fill_polygon_utm plays no role in suitability scoring: rebuild the same split
# from scratch, strip the render-only fill field off each raw patch entirely before scoring, and confirm
# score_production_areas() produces byte-identical numeric results either way.
fresh_step1 = compute_step1_eligible_cells(dumbbell_dem, _full_extent_boundary(dumbbell_dem), disqualifying_soil_union_utm=None)
fresh_patches = cluster_and_gate(dumbbell_mask, dumbbell_dem, _full_extent_boundary(dumbbell_dem), fresh_step1)
for p in fresh_patches:
    del p["render_fill_polygon_utm"]
stripped_scored = {p["id"]: p for p in score_production_areas(fresh_patches, dumbbell_dem, fresh_step1)}


def _same_score(a: float, b: float) -> bool:
    return a == b or (a != a and b != b)  # NaN != NaN, but both-NaN counts as "identical" here


for zone in (zone_1, zone_2):
    stripped = stripped_scored[zone["id"]]
    assert _same_score(stripped["suitability_score"], zone["suitability_score"]), (
        "suitability_score must be identical whether or not render_fill_polygon_utm is present on the "
        "patch dict"
    )
    assert _same_score(stripped["area_score"], zone["area_score"]) and _same_score(
        stripped["compactness_score"], zone["compactness_score"]
    ), "size_factor's sub-scores must be identical whether or not the render-only fill field is present"

print(
    "render_fill_polygon_utm invariant: area_acres, geometry_wgs84 (zones_geojson), and suitability "
    "scoring for both split zones all continue to reflect the full, post-reclaim polygon_utm -- "
    "byte-identical whether or not the render-only fill field is even present on the patch dict."
)


# =====================================================================
# Bounded opening -- an interior pocket stays OPEN: a small excluded (steep/hydric)
# pocket entirely inside an otherwise-solid zone must NOT be covered by render_fill_
# polygon_utm. render_fill_polygon_utm is a bounded morphological OPENING of the
# cluster's own cell mask clipped to polygon_utm (production's disc opening replaced
# the old convex hull). An opening is anti-extensive (opening(A) is a subset of A) and
# CANNOT fill a hole -- filling a notch/pocket is a CLOSING (dilate-then-erode), the
# opposite operation, deliberately NOT used here. So the pocket the cell gate excluded
# from polygon_utm stays excluded from render_fill_polygon_utm too, and contour lines
# clipped against render_fill_polygon_utm leave the pocket as a real blank gap rather
# than drawing through it. This is exactly the property the opening work exists to
# guarantee: the drawn fill can never claim ground the cell gate excluded.
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
# render_fill_polygon_utm is a bounded opening clipped to polygon_utm: contained within
# polygon_utm, never larger, and -- being anti-extensive -- it does NOT close over the pocket.
assert pocket_patch["polygon_utm"].buffer(1e-6).contains(pocket_patch["render_fill_polygon_utm"]), (
    "render_fill_polygon_utm (a bounded opening clipped to polygon_utm) must be contained within polygon_utm"
)
assert pocket_patch["render_fill_polygon_utm"].area <= pocket_patch["polygon_utm"].area * (1 + 1e-9) + 1e-6, (
    "the opening can never exceed polygon_utm's area"
)
fill_pocket_area = pocket_patch["render_fill_polygon_utm"].intersection(pocket_footprint).area
assert fill_pocket_area < 1e-6, (
    f"the excluded interior pocket must stay OPEN -- an opening cannot fill a hole, so render_fill_polygon_utm "
    f"must not cover the small (20x20m) excluded pocket (covered {fill_pocket_area} of {pocket_footprint.area} sq m)"
)

pocket_global_contours = compute_contour_lines(pocket_dem)
pocket_clipped = _clip_contours_to_zone(pocket_global_contours, pocket_patch)  # render_fill_polygon_utm
pocket_overlap = sum(p.intersection(pocket_footprint).length for p in pocket_clipped)
assert pocket_overlap < 1e-9, (
    f"clipping against render_fill_polygon_utm must leave the excluded interior pocket as a real blank gap -- "
    f"an opening does not close over it, so no contour line may run through it (got {pocket_overlap}m of overlap)"
)
print(
    "Bounded opening: a small (20x20m) excluded pocket entirely inside a zone stays OPEN in "
    "render_fill_polygon_utm (an opening is anti-extensive and cannot fill a hole) -- contour lines clip "
    "around it, leaving a real blank gap rather than drawing through it."
)


# --- Severed waist stays severed, worst case: the tightest real waist-split gap this pipeline's own
#     erosion math can produce (a single-pixel-wide, single-row-long throat, at dem_data.py's fixed 5m
#     production DEM resolution) -- confirmed empirically, not assumed, that render_fill_polygon_utm (each
#     piece's own bounded opening) stays SEVERED here too. An opening operates on the survivors of its OWN
#     erosion, so a neck this narrow is cut before the dilation and cannot be re-joined -- the two lobes'
#     fills stay on their own sides of the pinch and never bridge the gap the waist split created. ---

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
tight_fill_gap = tight_1["render_fill_polygon_utm"].distance(tight_2["render_fill_polygon_utm"])
assert tight_fill_gap > 0, (
    "render_fill_polygon_utm (each piece's own bounded opening) must stay SEVERED at the tightest real "
    "waist-split gap this pipeline's own erosion math can produce -- an opening cannot bridge a neck it "
    "just cut, so the two zones must not touch or overlap and silently defeat the waist split"
)
assert tight_1["render_fill_polygon_utm"].intersection(tight_2["render_fill_polygon_utm"]).area < 1e-9, (
    "render_fill_polygon_utm for the two split zones must not overlap at the tightest possible real waist"
)
print(
    f"Severed waist stays severed: even the tightest real waist-split gap this pipeline's erosion math can "
    f"produce survives as a real gap between the two pieces' render_fill_polygon_utm openings "
    f"(gap {tight_fill_gap}m) -- no touch, no overlap; an opening cannot bridge a neck it severed."
)


# =====================================================================
# WATER ZONE STYLE: the water zone renders as RIPPLE-LINE TEXTURE -- a family
# of horizontal sine waves clipped to its own render_fill_polygon_utm, drawn
# with NO fill and NO perimeter stroke (see render_layout_map.py's own WATER
# ZONE STYLE docstring section, and the ripple-texture branch that replaced the
# earlier filled/outlined polygon). render_fill_polygon_utm is the water zone's
# bounded render opening (a disc opening of its own cell mask clipped to
# polygon_utm -- see water_candidate_zones.find_candidate_zones()'s own
# docstring), NOT a convex hull: it is contained within polygon_utm and never
# exceeds it, and may be a MultiPolygon where the opening severs a narrow pinch.
# =====================================================================

import water_candidate_zones as wcz
from water_candidate_zones import find_candidate_zones
from render_layout_map import (
    WATER_ZONE_COLOR,
    WATER_RIPPLE_ALPHA,
    WATER_RIPPLE_LINEWIDTH,
)


# --- Full render_layout_map() pass: a synthetic water zone whose render_fill_polygon_utm is a bounded
#     render opening (contained within its real polygon_utm footprint, smaller than the blocky shape) and
#     DELIBERATELY overlaps a production zone's own render_fill_polygon_utm -- confirms the whole pipeline
#     still renders correctly, the water zone draws as ripple-line texture (no fill) clipped to render_fill_
#     polygon_utm, its numbered marker lands on that render opening, and the display overlap is allowed. ---

WATER_ZONE_TEST_SIZE = (20, 20)
water_zone_test_dem = {
    "array": np.full(WATER_ZONE_TEST_SIZE, 100.0, dtype=np.float32),
    "resolution_meters": (5.0, 5.0),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}

# Same L-shaped (non-convex) fixture pattern as test_water_candidate_zones.py's own render_fill_polygon_utm
# tests -- a real drainage band winding around a corner, so the bounded opening genuinely differs from
# (is smaller than) the blocky footprint.
wz_vertical_arm = _rect_cells(0, 15, 0, 5)
wz_horizontal_arm = _rect_cells(10, 15, 0, 15)
wz_l_shape_mask = _mask_from_cells(WATER_ZONE_TEST_SIZE, list(set(wz_vertical_arm + wz_horizontal_arm)))

wz_full_extent = box(500000.0, 4500000.0 - 100.0, 500000.0 + 100.0, 4500000.0)
wz_production_area = {
    "id": 0,
    "representative_elevation_m": 50.0,
    "polygon_utm": box(500000.0, 4500000.0 - 130.0, 500000.0 + 20.0, 4500000.0 - 100.0),
    # Deliberately overlaps the water zone's own render opening -- see the corresponding
    # test_water_candidate_zones.py check for why this is the expected, allowed outcome
    # (the ripple texture is allowed to cross a production zone's rendered contour texture).
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

# Current semantics: render_fill_polygon_utm is a BOUNDED render opening, not a convex hull -- it is
# contained within the zone's own polygon_utm and never exceeds it (the water rebuild replaced the old
# hull, which had no upper bound, with a disc opening clipped to polygon_utm). It may be a Polygon or a
# MultiPolygon (a narrow pinch severs into pieces); every assertion here tolerates both. It is genuinely
# smaller than the real, blocky footprint on this L-shaped fixture, so the marker-placement check below --
# marker on the render opening, computed the same way render_layout_map() does -- stays non-trivial.
_wz_render_fill = water_zone_fixture["render_fill_polygon_utm"]
_wz_polygon = water_zone_fixture["polygon_utm"]
assert _wz_render_fill.geom_type in ("Polygon", "MultiPolygon"), (
    f"render_fill_polygon_utm must be a (Multi)Polygon, got {_wz_render_fill.geom_type}"
)
assert _wz_polygon.buffer(1e-6).contains(_wz_render_fill), (
    "render_fill_polygon_utm (a bounded render opening) must be contained within the zone's own polygon_utm"
)
assert _wz_render_fill.area <= _wz_polygon.area * (1 + 1e-9) + 1e-6, (
    "render_fill_polygon_utm must never exceed polygon_utm's area -- bounded by construction, not a hull"
)
assert _wz_render_fill.area < _wz_polygon.area, (
    "test setup: on this L-shaped fixture the render opening must be strictly smaller than the blocky "
    "footprint, so the render geometry genuinely differs from the real footprint"
)

# Record every plot_polygon()/plot_line() call (GEOMETRY + kwargs) render_layout_map() makes, so the ripple
# lines' own color/alpha/zorder/linewidth (and the ABSENCE of any water-zone fill polygon) can be confirmed
# directly against what's drawn -- and, with no numbered marker to carry it anymore, so the "drawn geometry
# is the render opening, not geometry_wgs84" invariant can be ported onto the ripple LINE geometry itself
# (the marker used to be that invariant's vehicle).
recorded_polygon_calls = []
_original_plot_polygon = rlm.plot_polygon


def _recording_plot_polygon(geometry, **kwargs):
    recorded_polygon_calls.append((geometry, kwargs))
    return _original_plot_polygon(geometry, **kwargs)


recorded_line_calls = []
_original_plot_line = rlm.plot_line


def _recording_plot_line(geometry, **kwargs):
    recorded_line_calls.append((geometry, kwargs))
    return _original_plot_line(geometry, **kwargs)


rlm.plot_polygon = _recording_plot_polygon
rlm.plot_line = _recording_plot_line

wz_synthetic_layers = {
    "dem": water_zone_test_dem,
    "production_areas": [],
    "water_zone": water_zone_fixture,
    "road_corridor": [],
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
    rlm.plot_polygon = _original_plot_polygon
    rlm.plot_line = _original_plot_line

# The water zone renders as ripple-line TEXTURE, not a filled polygon: there must be ZERO water-zone fill
# plot_polygon() calls (no facecolor == WATER_ZONE_COLOR), and one or more ripple LINE calls, every one
# using WATER_ZONE_COLOR, the ripple alpha/linewidth, and the water zone's zorder=41 (above production
# zones' zorder=40).
water_fill_calls = [kw for _geom, kw in recorded_polygon_calls if kw.get("facecolor") == WATER_ZONE_COLOR]
assert len(water_fill_calls) == 0, (
    f"the water zone must NOT be drawn as a filled polygon anymore (ripple-line texture replaced the fill) "
    f"-- got {len(water_fill_calls)} plot_polygon() call(s) with facecolor == WATER_ZONE_COLOR"
)
water_ripple_calls = [(geom, kw) for geom, kw in recorded_line_calls if kw.get("color") == WATER_ZONE_COLOR]
assert len(water_ripple_calls) >= 1, (
    f"expected at least one water-zone ripple plot_line() call (color == WATER_ZONE_COLOR), got "
    f"{len(water_ripple_calls)}"
)
assert {kw["zorder"] for _geom, kw in water_ripple_calls} == {41}, (
    f"every water-zone ripple line must use zorder=41 (above production zones' zorder=40), got "
    f"{sorted({kw['zorder'] for _geom, kw in water_ripple_calls})}"
)
assert {kw["alpha"] for _geom, kw in water_ripple_calls} == {WATER_RIPPLE_ALPHA}, (
    f"every water-zone ripple line must use WATER_RIPPLE_ALPHA, got "
    f"{sorted({kw['alpha'] for _geom, kw in water_ripple_calls})}"
)
assert {kw["linewidth"] for _geom, kw in water_ripple_calls} == {WATER_RIPPLE_LINEWIDTH}, (
    f"every water-zone ripple line must use WATER_RIPPLE_LINEWIDTH, got "
    f"{sorted({kw['linewidth'] for _geom, kw in water_ripple_calls})}"
)

print(
    f"Water zone draws as ripple-line texture: {len(water_ripple_calls)} ripple line(s) in WATER_ZONE_COLOR "
    f"at zorder=41 (alpha={WATER_RIPPLE_ALPHA}, linewidth={WATER_RIPPLE_LINEWIDTH}), and ZERO filled-polygon "
    "calls for the water zone."
)

# Ported invariant (previously carried by the numbered marker's placement): the geometry actually DRAWN for
# the water zone is clipped to render_fill_polygon_utm (the render opening), NOT to the real, blocky
# geometry_wgs84. The old assertion proved the marker sat on the render opening's own representative_point();
# with no marker anymore, we prove the SAME thing directly on the drawn ripple LINES -- every ripple line
# must lie within the reprojected render_fill_polygon_utm.
expected_render_fill_mercator = rlm._reproject_utm_geometry_to_mercator(
    water_zone_fixture["render_fill_polygon_utm"], water_zone_test_dem["crs"]
)
expected_footprint_mercator = rlm._reproject_geometry_to_mercator(water_zone_fixture["geometry_wgs84"])
# The render opening is strictly smaller than the reprojected geometry_wgs84 footprint on this L-shaped
# fixture, so ripples clipped to geometry_wgs84 instead would escape render_fill -- this makes the
# containment check below a real discriminator, not a tautology.
assert expected_render_fill_mercator.area < expected_footprint_mercator.area, (
    "test setup: the reprojected render opening must be strictly smaller than the reprojected geometry_wgs84 "
    "footprint, otherwise the containment check below wouldn't distinguish render_fill from geometry_wgs84"
)
_render_fill_padded = expected_render_fill_mercator.buffer(1e-6)
for geom, _kw in water_ripple_calls:
    assert _render_fill_padded.contains(geom), (
        "every water-zone ripple line must lie within render_fill_polygon_utm (the render opening actually "
        "drawn), not geometry_wgs84 -- a ripple escaping the opening would mean the clip used the wrong "
        "geometry"
    )

# The water zone contributes exactly one "Water System Survey Area" legend entry (before the boundary
# fence's single "Fencing" entry, KSOP order); no numbering, no data.
_wz_legend_labels = _capture_legend_labels(wz_synthetic_layers, property_boundary)
assert _wz_legend_labels == [rlm.LEGEND_LABEL_WATER, rlm.LEGEND_LABEL_FENCING], (
    f"a water zone + boundary fence must yield exactly [Water System Survey Area, Fencing] in KSOP order, "
    f"got {_wz_legend_labels}"
)

print(
    "Full pipeline with a synthetic water zone: render_layout_map() runs offline end-to-end, draws the water "
    "zone as ripple-line texture clipped to render_fill_polygon_utm (the bounded render opening, not the real "
    "blocky geometry_wgs84 -- proven directly on the drawn ripple lines now that no marker carries it), emits "
    "a single 'Water System Survey Area' legend entry, and completes successfully even though the render "
    "opening deliberately overlaps a production area's own render_fill_polygon_utm."
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

# --- Full render_layout_map() pass: a synthetic MULTI-BRANCH road network (trunk + spur) is drawn ---
# --- as a CASED (double-line) road per branch -- a wider low-alpha outer line and a narrower ---
# --- higher-alpha inner line, both ROAD_RENDER_COLOR, both a visibly smoothed/simplified geometry, ---
# --- IDENTICAL styling on every branch -- and the real road_corridor input is never mutated. Also ---
# --- proves the branch_role/length_ft/avg_grade_pct/newly_served_acres shape (not suitability_score, ---
# --- which no longer exists on this Feature -- see this module's own module docstring for why) is ---
# --- what render_layout_map() actually reads, and that no branch gets its own numbered map marker. ---

ROAD_TEST_ORIGIN_MERCATOR = (-8900000.0, 4900000.0)  # arbitrary but realistic Web Mercator point
road_stairstep_mercator = [
    (ROAD_TEST_ORIGIN_MERCATOR[0] + cx, ROAD_TEST_ORIGIN_MERCATOR[1] + cy) for cx, cy in stairstep_coords
]
road_lons, road_lats = _warp_transform_check(
    rlm.WEB_MERCATOR, rlm.WGS84,
    [c[0] for c in road_stairstep_mercator], [c[1] for c in road_stairstep_mercator],
)
trunk_branch_fixture = {
    "type": "Feature",
    "geometry": {"type": "LineString", "coordinates": list(zip(road_lons, road_lats))},
    "properties": {
        "branch_index": 0,
        "branch_role": "trunk",
        "length_ft": 810.5,
        "avg_grade_pct": 4.2,
        "newly_served_acres": 0.85,
    },
}
# A short spur off the trunk -- a straight 2-point line is enough to prove every branch (not just
# branch_index 0) gets drawn; it deliberately has its OWN distinct geometry so a "only drew the
# trunk" regression is unambiguous (a spur silently omitted would leave this geometry never plotted).
spur_lons, spur_lats = _warp_transform_check(
    rlm.WEB_MERCATOR, rlm.WGS84,
    [ROAD_TEST_ORIGIN_MERCATOR[0] + 5.0, ROAD_TEST_ORIGIN_MERCATOR[0] + 5.0],
    [ROAD_TEST_ORIGIN_MERCATOR[1] + 5.0, ROAD_TEST_ORIGIN_MERCATOR[1] + 25.0],
)
spur_branch_fixture = {
    "type": "Feature",
    "geometry": {"type": "LineString", "coordinates": list(zip(spur_lons, spur_lats))},
    "properties": {
        "branch_index": 1,
        "branch_role": "spur",
        "length_ft": 16.4,
        "avg_grade_pct": 6.1,
        "newly_served_acres": 0.10,
    },
}
road_corridor_fixture = [trunk_branch_fixture, spur_branch_fixture]
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
assert len(road_line_calls) == 4, (
    f"expected exactly 4 plot_line() calls (2 per branch -- cased double-line -- across 2 branches), "
    f"got {len(road_line_calls)}. EVERY branch must be drawn, not just road_corridor_features[0]"
)
outer_calls = [(geom, kw) for geom, kw in road_line_calls if kw["linewidth"] == ROAD_RENDER_OUTER_WIDTH]
inner_calls = [(geom, kw) for geom, kw in road_line_calls if kw["linewidth"] == ROAD_RENDER_INNER_WIDTH]
assert len(outer_calls) == 2 and len(inner_calls) == 2, (
    "expected exactly one outer-width and one inner-width call PER branch (2 branches here)"
)
# All 4 calls share the exact same styling regardless of branch_role -- nothing here distinguishes
# the trunk from the spur.
assert {kw["alpha"] for _geom, kw in outer_calls} == {ROAD_RENDER_OUTER_ALPHA}, (
    "every outer-shoulder call (trunk and spur alike) must use the SAME ROAD_RENDER_OUTER_ALPHA -- "
    "branches must not be visually distinguished"
)
assert {kw["alpha"] for _geom, kw in inner_calls} == {ROAD_RENDER_INNER_ALPHA}, (
    "every inner call (trunk and spur alike) must use the SAME ROAD_RENDER_INNER_ALPHA -- branches "
    "must not be visually distinguished"
)
for _geom, kw in road_line_calls:
    assert kw["zorder"] in (42, 42.5), "every branch must share the same two zorders (42/42.5), not per-branch values"
assert all(kw["zorder"] == 42 for _geom, kw in outer_calls), "every outer (shoulder) call must use zorder=42"
assert all(kw["zorder"] == 42.5 for _geom, kw in inner_calls), (
    "every inner call must use zorder=42.5, ABOVE the outer shoulder's 42 -- same for every branch"
)

# Two genuinely distinct geometries were plotted -- proof the spur is real, separately-drawn geometry,
# not the trunk's line plotted twice.
outer_geoms = [geom for geom, kw in outer_calls]
assert outer_geoms[0].coords[:] != outer_geoms[1].coords[:], (
    "the trunk and spur must be drawn as two DIFFERENT geometries -- got identical coordinates, as if "
    "only one branch (or the same branch twice) were actually plotted"
)

# The trunk's own outer/inner pair share its exact smoothed geometry (same "cased" pairing test the
# single-branch version of this check used), matched by vertex count against the DEM-cell-stairstepped
# fixture (the trunk is the one built from stairstep_coords; the spur is a plain 2-point line with
# nothing to smooth away).
trunk_calls = [(geom, kw) for geom, kw in road_line_calls if len(geom.coords) > 2]
assert len(trunk_calls) == 2, "expected the trunk's own outer+inner pair to be identifiable by vertex count"
trunk_outer_geom = [geom for geom, kw in trunk_calls if kw["linewidth"] == ROAD_RENDER_OUTER_WIDTH][0]
trunk_inner_geom = [geom for geom, kw in trunk_calls if kw["linewidth"] == ROAD_RENDER_INNER_WIDTH][0]
assert trunk_outer_geom.coords[:] == trunk_inner_geom.coords[:], (
    "the trunk's own outer and inner line must be drawn over the exact same (smoothed) geometry"
)
raw_mercator_coords_count = len(road_stairstep_mercator)
assert len(trunk_outer_geom.coords) < raw_mercator_coords_count, (
    f"the trunk geometry actually handed to plot_line() must be the SMOOTHED version (fewer vertices "
    f"than the raw {raw_mercator_coords_count}-point stairstep), not the raw per-cell geometry straight "
    f"off road_corridor['geometry']"
)

# Every branch, trunk and spur alike, collapses into ONE "Suggested Road Corridor" legend entry -- no
# per-branch label and no numbered map marker (branch_role/branch_index are for the narrative report, not
# a map label -- see this module's own ROAD CORRIDOR STYLE docstring section). The plain boundary fence
# adds the single "Fencing" entry; KSOP order puts road before fencing.
_road_legend_labels = _capture_legend_labels(road_synthetic_layers, property_boundary)
assert _road_legend_labels == [rlm.LEGEND_LABEL_ROAD, rlm.LEGEND_LABEL_FENCING], (
    f"two road branches + a boundary fence must yield exactly one Suggested Road Corridor entry and one "
    f"Fencing entry (branches collapse to one), got {_road_legend_labels}"
)

assert road_corridor_fixture == road_corridor_fixture_before, (
    "rendering must never mutate the real road_corridor input -- its geometry/properties (used for "
    "length_m/avg_grade_pct/every other scoring and narrative value) must stay byte-for-byte identical"
)
print(
    f"Full pipeline with a synthetic 2-branch (trunk + spur) road network: render_layout_map() draws "
    f"EVERY branch, each as a cased double-line road (outer width={ROAD_RENDER_OUTER_WIDTH}/"
    f"alpha={ROAD_RENDER_OUTER_ALPHA}, inner width={ROAD_RENDER_INNER_WIDTH}/alpha={ROAD_RENDER_INNER_ALPHA}, "
    f"inner above outer), with IDENTICAL styling regardless of branch_role and NO numbered map marker per "
    f"branch, over a visibly smoothed trunk geometry ({raw_mercator_coords_count} raw points down to "
    f"{len(trunk_outer_geom.coords)}), and never mutates the real road_corridor input."
)


# =====================================================================
# TREE ZONE STYLE: each ranked tree-zone candidate patch (possibly
# several, same "ranked list" shape as production zones -- not a single
# selection like water_zone/road_corridor/structure_site) renders as
# HATCH-ONLY -- diagonal hatch strokes, no fill, no perimeter stroke --
# drawn from its own render_fill_polygon_utm (a DISPLAY-ONLY plain convex
# hull -- see score_tree_search_space()'s own docstring), NOT its real,
# potentially-notched polygon_utm/geometry_wgs84 -- see this module's own
# "TREE ZONE STYLE" docstring section.
# =====================================================================

from shapely.geometry import mapping as _mapping_check
from shapely.ops import unary_union as _unary_union_check
from render_layout_map import TREE_ZONE_COLOR, TREE_ZONE_HATCH_ALPHA, TREE_ZONE_HATCH


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

# Record the GEOMETRY (plus kwargs) of every plot_polygon() call, so the "drawn geometry is render_fill_
# polygon_utm, not geometry_wgs84" invariant can be ported directly onto the hatch polygons -- with no
# numbered marker anymore, the drawn polygon itself is the vehicle for that invariant.
recorded_tree_polygon_calls = []
_original_plot_polygon_for_trees = rlm.plot_polygon


def _recording_plot_polygon_for_trees(geometry, **kwargs):
    recorded_tree_polygon_calls.append((geometry, kwargs))
    return _original_plot_polygon_for_trees(geometry, **kwargs)


rlm.plot_polygon = _recording_plot_polygon_for_trees

tree_synthetic_layers = {
    "dem": water_zone_test_dem,
    "production_areas": [],
    "water_zone": None,
    "road_corridor": [],
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
    rlm.plot_polygon = _original_plot_polygon_for_trees

tree_fill_calls = [(geom, kw) for geom, kw in recorded_tree_polygon_calls if kw.get("edgecolor") == TREE_ZONE_COLOR]
assert len(tree_fill_calls) == 2, f"expected exactly 2 tree-zone hatch plot_polygon() calls (one per patch), got {len(tree_fill_calls)}"
for _geom, kw in tree_fill_calls:
    assert kw["facecolor"] == "none", f"tree-zone patches must render with NO fill, got facecolor={kw['facecolor']!r}"
    assert kw["linewidth"] == 0, f"tree-zone patches must render with NO perimeter stroke, got linewidth={kw['linewidth']}"
    assert kw["alpha"] == TREE_ZONE_HATCH_ALPHA, f"every tree-zone hatch must use TREE_ZONE_HATCH_ALPHA, got {kw['alpha']}"
    assert kw["hatch"] == TREE_ZONE_HATCH, f"every tree-zone hatch must use the TREE_ZONE_HATCH pattern, got {kw['hatch']}"
    assert kw["zorder"] == 42.8, f"tree-zone hatch must render between the road corridor (42.5) and structure site (43), got {kw['zorder']}"

# Ported invariant (previously carried by each tree candidate's numbered marker placement): each hatch
# polygon actually DRAWN is the reprojected render_fill_polygon_utm (the display hull), NOT the real,
# potentially-notched geometry_wgs84. Both fixtures here have single-Polygon hulls, so there's exactly one
# drawn polygon per patch, in patch order. For the notched patch its hull is strictly larger than its real
# footprint, so a drawn geometry matching geometry_wgs84 instead would be visibly smaller -- a real
# discriminator, not a tautology.
tree_drawn_geoms = [geom for geom, _kw in tree_fill_calls]
for drawn_geom, patch in zip(tree_drawn_geoms, [tree_patch_1, tree_patch_2]):
    expected_hull_mercator = rlm._reproject_utm_geometry_to_mercator(
        patch["render_fill_polygon_utm"], water_zone_test_dem["crs"]
    )
    assert drawn_geom.symmetric_difference(expected_hull_mercator).area < 1e-6, (
        "each tree-zone candidate's drawn hatch polygon must be its reprojected render_fill_polygon_utm "
        "(the display hull), not geometry_wgs84 -- the drawn geometry did not match the render fill"
    )

# Discriminator: for the NOTCHED patch (tree_patch_1) the drawn geometry must NOT match the reprojected
# geometry_wgs84 -- the hull genuinely differs from (is larger than) the real, non-convex footprint, so a
# drawn geometry_wgs84 would be a visibly different, smaller shape. This is what makes the
# "drawn == render_fill" checks above discriminating rather than tautological.
assert tree_patch_1["render_fill_polygon_utm"].area > tree_patch_1["polygon_utm"].area, (
    "sanity re-check: tree_patch_1's own render_fill_polygon_utm must still be strictly larger than its "
    "real, non-convex polygon_utm"
)
_tree1_drawn = tree_drawn_geoms[0]
_tree1_footprint_mercator = rlm._reproject_geometry_to_mercator(tree_patch_1["geometry_wgs84"])
assert _tree1_drawn.symmetric_difference(_tree1_footprint_mercator).area > 1e-3, (
    "the notched tree candidate's drawn hatch polygon must be the render_fill hull, NOT the reprojected "
    "geometry_wgs84 footprint -- the two must be visibly different shapes (they were not)"
)
assert _tree1_drawn.area > _tree1_footprint_mercator.area, (
    "the drawn hull must be strictly larger than the reprojected geometry_wgs84 footprint for the notched patch"
)

# Two tree candidates collapse into ONE "Tree Crop Areas" legend entry; the boundary fence adds the single
# "Fencing" entry. KSOP order puts tree before fencing.
_tree_legend_labels = _capture_legend_labels(tree_synthetic_layers, property_boundary)
assert _tree_legend_labels == [rlm.LEGEND_LABEL_TREE, rlm.LEGEND_LABEL_FENCING], (
    f"two tree candidates + a boundary fence must yield exactly [Tree Crop Areas, Fencing], got {_tree_legend_labels}"
)

print(
    f"Full pipeline with 2 synthetic tree-zone candidates: render_layout_map() draws each as hatch-only "
    f"(pattern={TREE_ZONE_HATCH!r}, alpha={TREE_ZONE_HATCH_ALPHA}, no fill, no perimeter stroke) at zorder=42.8 "
    f"(between the road corridor and structure site) from its own render_fill_polygon_utm (a real, notched patch's hull "
    f"visibly larger than its real footprint -- proven directly on the drawn polygon now), collapses both into a "
    f"single 'Tree Crop Areas' legend entry, and completes successfully."
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
_sentinel_canopy_height = {"sentinel": "canopy_height"}

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
    canopy_height=_sentinel_canopy_height,
    imagery_summary={},
    # irradiance -- irrelevant to this build_pipeline_context() forwarding check (not one of the
    # fields fetch_layout_layers() forwards), so it gets a plain placeholder like every other
    # irrelevant ParcelData field above. It became a required ParcelData field on the irradiance-
    # integration branch (see parcel_data.py's own irradiance field notes).
    irradiance={},
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
# canopy_height -- this branch's own addition, same identity contract as every field above.
assert _captured_context_kwargs["canopy_height"] is _spy_parcel_data.canopy_height, (
    "fetch_layout_layers() must forward parcel_data.canopy_height to build_pipeline_context() by identity, "
    "not a re-derived copy"
)
print(
    "build_pipeline_context() wiring: fetch_layout_layers() forwards ParcelData's own water_features, "
    "soil_geometries, and canopy_height (plus dem/boundary_polygon_utm/soil_components/farm_roads) to "
    "build_pipeline_context() as kwargs, every one by identity (is, not ==) -- confirming the single "
    "ParcelData fetch is reused, not re-derived."
)


# =====================================================================
# canopy_height= override: real, wraps=-based call-count proof (not just
# mocked) that fetch_layout_layers()'s OWN direct identify_solar_
# candidate_zones() call -- the one net-new canopy-accepting call the
# render-layout-map-context-wiring branch added inside fetch_layout_
# layers() itself -- genuinely never triggers a real canopy fetch when
# parcel_data.canopy_height is supplied. build_pipeline_context() is
# mocked here to a cheap, fully-formed fake PipelineContext (its own
# real, whole-run canopy-fetch-closure proof already lives in test_
# pipeline_context.py -- no value re-deriving that same heavy synthetic-
# terrain machinery a second time in THIS file); identify_road_corridor_
# candidates() and identify_fencing() are ALSO mocked here (return_
# value=, identity-checked below rather than left real) -- both now
# accept canopy_height (road_corridors.py/fencing.py gained the
# parameter on the canopy-height-road-corridors branch, and THIS branch,
# canopy-mask-wiring-road-fencing, wires fetch_layout_layers()'s own
# direct calls to both into it), but a fully-real run of either would
# also drag in identify_road_corridor_candidates()'s/identify_fencing()'s
# own separate NHD/SSURGO/floodplain network calls, which have nothing
# to do with canopy forwarding -- the real, wraps=-based zero-additional-
# canopy-fetch proof for those two call sites specifically lives in the
# dedicated call-count-measurement section below instead, which reuses
# diagnose_fetch_layout_layers_redundant_fetches.py's own real (non-
# mocked) registry. Uses the SAME CanopyOverrideProbe every other real
# canopy-override test in this session uses, patched once at production_
# area's own module bindings so it observes the fetch no matter how
# deeply nested identify_solar_candidate_zones()'s own reach into it is
# (its own top-level gate, PLUS its own nested identify_tree_zone_
# candidates() call's gate).
# =====================================================================

import pipeline_context as pc
from _canopy_override_probe import CanopyOverrideProbe, clean_canopy_for

_canopy_dem = _sloped_dem(24, 24)
_canopy_boundary_utm = _full_extent_boundary(_canopy_dem)
_canopy_lons, _canopy_lats = warp_transform(
    _canopy_dem["crs"], "EPSG:4326", *_canopy_boundary_utm.exterior.coords.xy
)
_canopy_boundary_coordinates = list(zip(_canopy_lons, _canopy_lats))
_canopy_override = clean_canopy_for(_canopy_dem)

# The real "no road network" shape (road_corridors._empty_road_network()) -- PipelineContext.
# selected_road_corridor is never None (see pipeline_context.py's own field notes), even when no
# branches exist, so a fixture standing in for it must be this same shape, not None. identify_
# solar_candidate_zones() is run for REAL (wraps=) below and genuinely dereferences cell_footprint_
# polygon_utm, so this needs a real (if empty) Polygon, not a placeholder.
_EMPTY_ROAD_NETWORK = {
    "branches": [],
    "total_length_meters": 0.0,
    "total_served_acres": 0.0,
    "unserved_acres": 0.0,
    "stop_reason": "no_anchor_given",
    "cells": [],
    "cell_footprint_polygon_utm": Polygon(),
}

_fake_context_for_canopy_case = pc.PipelineContext(
    dem=_canopy_dem,
    boundary_polygon_utm=_canopy_boundary_utm,
    valleys=[],
    keypoints=[],
    production_areas=[],
    parcel_acres=0.0,
    existing_roads=None,
    soil_exclusion_unions={"hydric_floodplain_union": None, "hydric_floodplain_is_fallback": False, "erosion_prone_union": None},
    water_zones=[],
    selected_water_zone=None,
    selected_road_corridor=_EMPTY_ROAD_NETWORK,
    selected_structure_site=None,
    tree_zone_patches=[],
    narrative_data={},
)

_canopy_case_parcel_data = ParcelData(
    dem=_canopy_dem,
    boundary_polygon_utm=_canopy_boundary_utm,
    soil_components=[],
    farmland_classification=[],
    erosion_factor=[],
    saturated_hydraulic_conductivity=[],
    soil_geometries={},
    water_features={"streams": [], "water_bodies": []},
    farm_roads=[],
    climate_summary={},
    elevation_grid=[],
    canopy_height=_canopy_override,
    imagery_summary={},
    # irradiance -- not consumed anywhere on this canopy-override path (build_pipeline_context() is
    # mocked below; nothing in fetch_layout_layers()'s own reach reads it), so a plain placeholder is
    # enough. Required ParcelData field since the irradiance-integration branch (see parcel_data.py).
    irradiance={},
)

with ExitStack() as _canopy_stack:
    _enter = _canopy_stack.enter_context
    mock_context_canopy_case = _enter(
        mock_patch.object(rlm, "build_pipeline_context", return_value=_fake_context_for_canopy_case)
    )
    mock_road_corridor_canopy_case = _enter(
        mock_patch.object(
            rlm,
            "identify_road_corridor_candidates",
            return_value={"zones_geojson": {"type": "FeatureCollection", "features": []}, "selected_road_corridor": None},
        )
    )
    mock_solar_canopy_case = _enter(
        mock_patch.object(rlm, "identify_solar_candidate_zones", wraps=rlm.identify_solar_candidate_zones)
    )
    mock_fencing_canopy_case = _enter(
        mock_patch.object(
            rlm, "identify_fencing", return_value={"fencing_geojson": {"type": "FeatureCollection", "features": []}, "segment_count": 0}
        )
    )

    with CanopyOverrideProbe() as canopy_probe:
        _canopy_case_result = rlm.fetch_layout_layers(_canopy_boundary_coordinates, parcel_data=_canopy_case_parcel_data)

canopy_probe.assert_override_used(_canopy_override, "fetch_layout_layers()")
assert mock_context_canopy_case.call_args.kwargs["canopy_height"] is _canopy_override, (
    "fetch_layout_layers() must pass parcel_data.canopy_height to build_pipeline_context() by identity"
)
assert mock_solar_canopy_case.call_args.kwargs["canopy_height"] is _canopy_override, (
    "fetch_layout_layers()'s own direct identify_solar_candidate_zones() call must receive parcel_data."
    "canopy_height by identity"
)
# canopy_height -- this branch's (canopy-mask-wiring-road-fencing) own addition: fetch_layout_layers()'s
# own direct identify_road_corridor_candidates()/identify_fencing() calls must also forward parcel_data.
# canopy_height by identity, same as identify_solar_candidate_zones() above.
assert mock_road_corridor_canopy_case.call_args.kwargs["canopy_height"] is _canopy_override, (
    "fetch_layout_layers()'s own direct identify_road_corridor_candidates() call must receive parcel_data."
    "canopy_height by identity"
)
assert mock_fencing_canopy_case.call_args.kwargs["canopy_height"] is _canopy_override, (
    "fetch_layout_layers()'s own direct identify_fencing() call must receive parcel_data.canopy_height by "
    "identity"
)
print(
    f"canopy_height= override: fetch_layout_layers()'s OWN direct identify_solar_candidate_zones()/identify_"
    f"road_corridor_candidates()/identify_fencing() calls -- identify_solar_candidate_zones() run for REAL "
    f"here, not stubbed away -- receive the exact caller-supplied override (identity-checked for all three) "
    f"and cause production_area.get_canopy_height_for_boundary() to be called ZERO times "
    f"({len(canopy_probe.mask_arrays)} real canopy gate(s) reached, every one computed on the exact supplied "
    "override array). build_pipeline_context()'s own equivalent real-run proof lives in test_pipeline_"
    "context.py; identify_road_corridor_candidates()/identify_fencing() are return_value-mocked here purely "
    "to keep this section fast/offline (their own separate NHD/SSURGO/floodplain fetches have nothing to do "
    "with canopy) -- their own real, wraps=-based zero-additional-canopy-fetch proof is in the dedicated "
    "call-count-measurement section below."
)

# =====================================================================
# canopy_height= wiring (canopy-mask-wiring-road-fencing branch): real,
# wraps=-based call-count proof that fetch_layout_layers()'s own direct
# identify_road_corridor_candidates()/identify_fencing() calls, and
# pipeline_context.build_pipeline_context()'s own identify_road_corridor_
# candidates() call, now forward parcel_data.canopy_height all the way
# down to fencing.py's/road_corridors.py's own internal gates -- not just
# identity-checked against a mock (the section above), but a genuinely
# full, un-mocked fetch_layout_layers() run, reusing diagnose_fetch_
# layout_layers_redundant_fetches.py's own registry (WRAPS_SITES/
# STUB_SITES/COUNTED_STUB_SITES) rather than re-building the same heavy
# offline-stub apparatus a second time in this file. That module is a
# permanent, standalone diagnostic (no assertions of its own beyond a
# sanity isinstance() check) -- this section is what turns its real
# measurement into an enforced regression guard.
#
# get_canopy_height_for_boundary() is called ONCE for ParcelData's own
# upstream fetch (parcel_data.fetch_parcel_data(), ONE call regardless of
# how many downstream consumers reuse it), and now ZERO more from the
# compute layer. tree_zone_candidates.py's and solar_suitability.py's own
# DIRECT identify_road_corridor_candidates() call sites USED TO contribute
# a residual here: on this flat synthetic terrain, road_corridors.py's own
# network router finds no branches at all, and the canopy-mask-wiring-
# road-fencing branch's own pipeline_context.py still let that collapse to
# None (identify_road_corridor_candidates()'s own 'selected_road_corridor'
# key, which folds branches=[] to None) -- an ambiguous None every
# downstream consumer reads as "not supplied" and reacts to by running its
# own full self-compute (a real canopy fetch included). This branch closes
# that: pipeline_context.py's own selected_road_corridor field now always
# holds road_corridors.build_road_network()'s full network dict, branches
# =[] and all, NEVER None (see pipeline_context.py's own field notes) --
# so tree_zone_candidates.py's/solar_suitability.py's own self-compute
# checks (`if selected_road_corridor is None:`) no longer fire on THIS
# call graph, and their own identify_road_corridor_candidates() call sites
# are never reached at all anymore. Both of this branch's own wired call
# sites, pipeline_context.py's build_pipeline_context() and render_layout_
# map.py's own fetch_layout_layers(), are confirmed BELOW to still be
# genuinely exercised (count == 1, not skipped), not just present with a
# zero contribution by accident.
# =====================================================================

import diagnose_fetch_layout_layers_redundant_fetches as diag_fetch_layout_layers

_diag_counts, _diag_call_site_logs = diag_fetch_layout_layers.run()

_diag_canopy_sites = _diag_call_site_logs["get_canopy_height_for_boundary"]
# The call-site key is basename:lineno of the get_canopy_height_for_boundary() call inside
# parcel_data.fetch_parcel_data() (see diagnose_fetch_layout_layers_redundant_fetches.py's own
# _call_site_label()). That line moved to 192 when the irradiance-baseline branch added its own
# fetch above it, so this pinned lineno is updated to match the current source.
_diag_canopy_from_parcel_data = _diag_canopy_sites.get("parcel_data.py:192 in fetch_parcel_data", 0)
_diag_canopy_total = _diag_counts["get_canopy_height_for_boundary"]
_diag_canopy_from_compute_layer = _diag_canopy_total - _diag_canopy_from_parcel_data

assert _diag_canopy_from_parcel_data == 1, (
    f"parcel_data.fetch_parcel_data()'s own single upstream get_canopy_height_for_boundary() fetch must run "
    f"exactly once per fetch_layout_layers() call, got {_diag_canopy_from_parcel_data}"
)

# The two call sites THIS branch wired that are genuinely reached on this fixture:
# pipeline_context.py's own identify_road_corridor_candidates() call, and render_layout_map.py's own direct
# identify_road_corridor_candidates() call. fencing.py's own identify_fencing() no longer contributes a third
# call site here: it only derives a road corridor at all when its own tree-zone self-compute fallback is
# about to run (a real siting-exclusion input, not a fence loop of its own anymore -- see fencing.py's own
# module docstring for why no road gets a fence line), and fetch_layout_layers() already supplies
# tree_zone_patches=context.tree_zone_patches directly, so that fallback -- and therefore fencing.py's own
# nested identify_road_corridor_candidates() call -- correctly never fires on this real call graph.
_diag_road_corridor_sites = _diag_call_site_logs["identify_road_corridor_candidates"]
_WIRED_SITE_PREFIXES = ("pipeline_context.py:", "render_layout_map.py:")
_diag_wired_site_counts = {
    site: count for site, count in _diag_road_corridor_sites.items() if site.startswith(_WIRED_SITE_PREFIXES)
}
_diag_unwired_site_counts = {
    site: count for site, count in _diag_road_corridor_sites.items() if not site.startswith(_WIRED_SITE_PREFIXES)
}
assert len(_diag_wired_site_counts) == 2, (
    f"expected exactly 2 of this branch's own wired identify_road_corridor_candidates() call sites "
    f"(pipeline_context.py/render_layout_map.py) to be reached, got {_diag_wired_site_counts}"
)
assert all(count == 1 for count in _diag_wired_site_counts.values()), (
    f"each of this branch's own wired identify_road_corridor_candidates() call sites must run exactly once, "
    f"got {_diag_wired_site_counts}"
)
# road_corridors.geometry-migration branch: pipeline_context.py's own selected_road_corridor field
# now always holds the full network dict (branches=[] and all), never None, so tree_zone_candidates.py's/
# solar_suitability.py's own `if selected_road_corridor is None:` self-compute checks no longer fire on
# this real call graph -- their own identify_road_corridor_candidates() call sites are unreached, not
# just present-with-zero-contribution. len(...) == 0 is the direct proof; the (still correct, now
# vacuous) startswith check is kept as a tripwire in case either file's own self-compute regresses back.
assert len(_diag_unwired_site_counts) == 0, (
    f"expected ZERO identify_road_corridor_candidates() call sites beyond this branch's own 2 wired ones -- "
    f"tree_zone_candidates.py's/solar_suitability.py's own self-compute fallbacks must not fire now that "
    f"pipeline_context.py's selected_road_corridor is never None (see that field's own notes) -- got "
    f"unexpected site(s): {_diag_unwired_site_counts}"
)
assert all(
    site.startswith(("tree_zone_candidates.py:", "solar_suitability.py:")) for site in _diag_unwired_site_counts
), (
    f"every identify_road_corridor_candidates() call site OTHER than this branch's own 2 wired ones must "
    f"trace to tree_zone_candidates.py or solar_suitability.py (the two files explicitly out of scope for "
    f"this branch) -- got unexpected site(s): {_diag_unwired_site_counts}"
)

# Regression guard on the actual residual: the road-corridors-geometry-migration branch closed this
# outright (pipeline_context.py's own selected_road_corridor is never None anymore -- see the comment
# above) -- the compute-layer residual is now genuinely ZERO, down from the canopy-mask-wiring-road-
# fencing branch's own pinned 5 (itself already an improvement over the earlier baseline of 10). Pinned
# to the current real, measured value so any regression fails loudly here rather than silently drifting.
assert _diag_canopy_from_compute_layer == 0, (
    f"expected ZERO residual compute-layer get_canopy_height_for_boundary() calls on this fixture -- "
    f"pipeline_context.py's selected_road_corridor is never None now, so tree_zone_candidates.py's/"
    f"solar_suitability.py's own unwired identify_road_corridor_candidates() self-compute (and its own real "
    f"canopy fetch) should never fire -- got {_diag_canopy_from_compute_layer}. If this rose above 0, "
    f"either this branch's own road_network passthrough regressed back to None on empty networks, or a new "
    f"canopy-fetch call site was introduced."
)

print(
    f"canopy_height= wiring, real full fetch_layout_layers() run (diagnose_fetch_layout_layers_redundant_"
    f"fetches.py's own registry, not a re-built one): get_canopy_height_for_boundary() called "
    f"{_diag_canopy_total} total ({_diag_canopy_from_parcel_data} from parcel_data.fetch_parcel_data()'s own "
    f"single upstream fetch, {_diag_canopy_from_compute_layer} from the compute layer). This branch's own 3 "
    f"wired call sites (pipeline_context.py's build_pipeline_context(), render_layout_map.py's own fetch_"
    f"layout_layers(), fencing.py's own identify_fencing()) are each confirmed exercised exactly once with "
    f"ZERO canopy-fetch contribution; the compute-layer residual is now ZERO too -- the road-corridors-"
    f"geometry-migration branch's own selected_road_corridor-is-never-None fix means tree_zone_candidates.py's/"
    f"solar_suitability.py's own identify_road_corridor_candidates() call sites are unreached on this real "
    f"call graph, not just present with zero canopy contribution."
)

# =====================================================================
# Reentrancy: render_layout_map() must survive being called more than
# once in a single process. render_layout_map.py used to build the
# structure-site map-pin OffsetImage at module scope; a matplotlib Artist
# binds to the first figure it is added to, so the SECOND render in the
# same process raised RuntimeError ("Can not put single artist in more
# than one figure"). This only surfaces when a structure_site is present
# (that is the only code path that adds the pin artist to the axes), which
# every other fixture in this file leaves as None -- so this reuses the
# existing offline synthetic_layers/property_boundary fixture and just
# supplies the minimal structure_site input that exercises the pin path.
# =====================================================================

_reentrancy_structure_site = {
    # A small WGS84 footprint inside property_boundary above; render_layout_
    # map() only reads its representative_point() + suitability_score.
    "geometry": mapping(box(-79.9825, 40.6445, -79.9820, 40.6450)),
    "properties": {"suitability_score": 88.0},
}
_reentrancy_layers = dict(synthetic_layers)
_reentrancy_layers["structure_site"] = _reentrancy_structure_site


def test_render_is_reentrant():
    """Module-level matplotlib artists bind to the first figure and raise
    'Can not put single artist in more than one figure' on the second render.
    A single-call test passes against the broken code -- this must run twice."""
    with tempfile.TemporaryDirectory() as tmpdir:
        first_path = os.path.join(tmpdir, "layout_map_reentrant_1.png")
        second_path = os.path.join(tmpdir, "layout_map_reentrant_2.png")
        rlm.render_layout_map(property_boundary, first_path, layers=_reentrancy_layers)
        # The second call is the whole point: on the broken code it raises
        # RuntimeError before it can write anything.
        rlm.render_layout_map(property_boundary, second_path, layers=_reentrancy_layers)
        assert os.path.getsize(first_path) > 0, "first render must produce a real, non-empty PNG"
        assert os.path.getsize(second_path) > 0, "second render must produce a real, non-empty PNG"


test_render_is_reentrant()
print(
    "Reentrancy: render_layout_map() with a structure_site renders twice in one process (fresh pin artist "
    "per figure) -- no 'Can not put single artist in more than one figure' RuntimeError."
)


# --- Symmetric zone-fence mutual trim: every water/tree zone fence ring is trimmed
#     against the boundary fence ring(s) AND every OTHER zone ring, so adjacent zones
#     render with a real visible gap between them rather than two near-parallel doubled
#     lines -- see render_layout_map.py's own WATER/TREE EXCLUSION FENCE STYLE docstring
#     section and ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M's own comment. Purely
#     render-time: fencing_geojson keeps every zone's full, untrimmed ring. ---

_TRIM_CRS = "EPSG:32617"
_TRIM_ORIGIN_X, _TRIM_ORIGIN_Y = 500000.0, 4500000.0
_TRIM_SIZE = 600.0  # property square side, meters


def _trim_square(x0: float, y0: float, x1: float, y1: float) -> LineString:
    """A closed square ring, positioned by meters-from-the-property's-SW-corner so the
    fixture below reads in plain local coordinates rather than raw UTM eastings."""
    ax, ay = _TRIM_ORIGIN_X + x0, _TRIM_ORIGIN_Y - _TRIM_SIZE + y0
    bx, by = _TRIM_ORIGIN_X + x1, _TRIM_ORIGIN_Y - _TRIM_SIZE + y1
    return LineString([(ax, ay), (bx, ay), (bx, by), (ax, by), (ax, ay)])


def _trim_to_wgs84(geometry_utm):
    return shape(transform_geom(_TRIM_CRS, "EPSG:4326", mapping(geometry_utm)))


# Gaps of 3m between neighbours: inside ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M, so
# every one of these pairings is a genuine "these two run close" case for the trim.
_trim_boundary_ring_utm = _trim_square(2, 2, _TRIM_SIZE - 2, _TRIM_SIZE - 2)
_trim_tree1_utm = _trim_square(100, 100, 200, 200)  # adjacent to tree 2
_trim_tree2_utm = _trim_square(203, 100, 303, 200)  # adjacent to tree 1 AND to the water zone
_trim_water_utm = _trim_square(203, 203, 303, 303)  # adjacent to tree 2's top edge
_trim_tree3_utm = _trim_square(420, 420, 520, 520)  # LONE -- near nothing at all
_trim_tree4_utm = _trim_square(5, 400, 105, 500)  # near the BOUNDARY ring only

_trim_boundary_coordinates = list(_trim_to_wgs84(Polygon(_trim_boundary_ring_utm.coords)).exterior.coords)
_trim_fencing_geojson = {
    "type": "FeatureCollection",
    "features": (
        boundary_fencing_to_geojson([_trim_to_wgs84(_trim_boundary_ring_utm)])["features"]
        + water_zone_fencing_to_geojson(_trim_to_wgs84(_trim_water_utm))["features"]
        + tree_zone_fencing_to_geojson(
            [
                _trim_to_wgs84(_trim_tree1_utm),
                _trim_to_wgs84(_trim_tree2_utm),
                _trim_to_wgs84(_trim_tree3_utm),
                _trim_to_wgs84(_trim_tree4_utm),
            ]
        )["features"]
    ),
}
_trim_fencing_geojson_before = json.dumps(_trim_fencing_geojson, sort_keys=True)

_trim_dem = {
    "array": np.zeros((10, 10), dtype=np.float32),
    "resolution_meters": RESOLUTION,
    "origin_x": _TRIM_ORIGIN_X,
    "origin_y": _TRIM_ORIGIN_Y,
    "crs": _TRIM_CRS,
}
_trim_layers = {
    "dem": _trim_dem,
    "production_areas": [],
    "water_zone": None,
    "road_corridor": [],
    "tree_zone_result": None,
    "structure_site": None,
    "water_features": {"streams": []},
    "contour_lines": [],
    "fencing_result": {"fencing_geojson": _trim_fencing_geojson, "segment_count": 1},
}


def _render_capturing_fences(layers: dict, legend_sink: list) -> list:
    """Runs a full render_layout_map() pass, returning every (geometry, zorder) pair
    _draw_boundary_fence() was actually handed -- i.e. exactly what got DRAWN, after the
    trim/clip -- and extending legend_sink with the icon legend's own label list (read
    off the real Legend the render builds, since the legend is now ax.legend(), not
    ax.text)."""
    captured: list = []
    real_draw = rlm._draw_boundary_fence

    def recording_draw(ax, ring, zorder=rlm.FENCE_ZORDER):
        captured.append((ring, zorder))
        return real_draw(ax, ring, zorder=zorder)

    _orig_legend = rlm.plt.Axes.legend

    def recording_legend(self, *args, **kwargs):
        legend = _orig_legend(self, *args, **kwargs)
        legend_sink.extend(text.get_text() for text in legend.get_texts())
        return legend

    with mock_patch.object(rlm, "_draw_boundary_fence", recording_draw):
        with mock_patch.object(rlm.plt.Axes, "legend", recording_legend):
            with tempfile.TemporaryDirectory() as tmpdir:
                rlm.render_layout_map(
                    _trim_boundary_coordinates, os.path.join(tmpdir, "layout_map.png"), layers=layers
                )
    return captured


def _trim_render_ring(geometry_utm):
    """The same simplified Mercator ring the renderer itself builds for a fence feature --
    the untrimmed reference each drawn piece is measured against below."""
    return rlm._angular_simplify_closed_ring(
        rlm._reproject_geometry_to_mercator(mapping(_trim_to_wgs84(geometry_utm))),
        rlm.FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M,
    )


_trim_legend_texts: list = []
_trim_drawn = _render_capturing_fences(_trim_layers, _trim_legend_texts)

_trim_rings = {
    "boundary": _trim_render_ring(_trim_boundary_ring_utm),
    "water": _trim_render_ring(_trim_water_utm),
    "tree1": _trim_render_ring(_trim_tree1_utm),
    "tree2": _trim_render_ring(_trim_tree2_utm),
    "tree3": _trim_render_ring(_trim_tree3_utm),
    "tree4": _trim_render_ring(_trim_tree4_utm),
}
_trim_boundary_drawn = [g for g, z in _trim_drawn if z == rlm.FENCE_ZORDER]
_trim_zone_drawn = [g for g, z in _trim_drawn if z == rlm.EXCLUSION_FENCE_ZORDER]

# Every drawn zone piece must trace exactly ONE zone's own ring: a merged/unioned outline
# would produce a piece no single ring covers (or one that straddles two).
_trim_by_zone: dict = {name: [] for name in ("water", "tree1", "tree2", "tree3", "tree4")}
for _piece in _trim_zone_drawn:
    _owners = [name for name in _trim_by_zone if _piece.distance(_trim_rings[name]) < 1e-6]
    assert len(_owners) == 1, (
        f"each drawn fence piece must trace exactly one zone's own ring (no merged outline) -- got {_owners}"
    )
    _trim_by_zone[_owners[0]].append(_piece)


def _trim_kept_fraction(name: str) -> float:
    return sum(p.length for p in _trim_by_zone[name]) / _trim_rings[name].length


# 1. Each zone with a near neighbour loses its near-shared stretch -- and only that.
#    tree2 neighbours BOTH tree1 and the water zone, so it loses two edges, not one.
for _name in ("tree1", "tree2", "water"):
    _kept = _trim_kept_fraction(_name)
    assert _kept < 0.95, f"{_name} borders a neighbour but kept {_kept:.3f} of its ring -- no bite was taken"
    assert _kept > 0.3, f"{_name} lost far more of its ring than its shared stretch ({_kept:.3f} kept)"
assert _trim_kept_fraction("tree2") < _trim_kept_fraction("tree1"), (
    "tree2 borders two neighbours and must lose more of its ring than tree1, which borders one"
)

# 2. SYMMETRY: both rings of an adjacent pair lose their shared stretch, leaving a REAL
#    visible gap between the two drawn lines -- not touching, not overlapping. The gap is
#    the intended result of the trim, not a defect.
for _a, _b in (("tree1", "tree2"), ("tree2", "water")):
    _a_drawn, _b_drawn = unary_union(_trim_by_zone[_a]), unary_union(_trim_by_zone[_b])
    assert not _a_drawn.intersects(_b_drawn), f"{_a}/{_b} drawn fence lines must not touch"
    assert _a_drawn.distance(_b_drawn) > rlm.ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M, (
        f"{_a}/{_b} drawn fence lines must be separated by a real, visible gap"
    )

# 3. A lone zone bordering nothing still renders its FULL, closed, untrimmed loop.
assert abs(_trim_kept_fraction("tree3") - 1.0) < 1e-9, "a zone with no near neighbour must keep its whole ring"
assert len(_trim_by_zone["tree3"]) == 1 and _trim_by_zone["tree3"][0].is_closed, (
    "a zone with no near neighbour must still render as one unbroken closed loop"
)

# 4. The pre-existing zone-vs-boundary trim is unchanged: tree4 borders only the boundary
#    fence and loses exactly that stretch, clearing the boundary line by the tolerance.
assert _trim_kept_fraction("tree4") < 0.95, "a zone running alongside the boundary fence must still be trimmed"
assert unary_union(_trim_by_zone["tree4"]).distance(_trim_rings["boundary"]) >= (
    rlm.ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M - 1e-6
), "the trimmed zone ring must clear the boundary fence line by the coincidence tolerance"

# 5. The boundary fence itself is never trimmed against anything -- drawn exactly as
#    simplified, still one closed loop.
assert len(_trim_boundary_drawn) == 1, "the single boundary fence ring must be drawn as exactly one piece"
assert abs(_trim_boundary_drawn[0].length - _trim_rings["boundary"].length) < 1e-9, (
    "the boundary fence must be drawn untrimmed, at its full simplified length"
)
assert _trim_boundary_drawn[0].is_closed, "the boundary fence must stay a closed loop"

# 6. Legend: every fence type/zone now collapses into ONE shared "Fencing" entry -- the
#    six per-zone labels ("Boundary Fencing"/"Water Zone Fencing"/"Tree Zone Fencing N")
#    are gone; the mutual trim still draws each zone as its own separate rendered pieces
#    (asserted above), but the legend no longer distinguishes them at all. This layers dict
#    has ONLY fencing present, so the whole legend is exactly one entry.
assert _trim_legend_texts == [rlm.LEGEND_LABEL_FENCING], (
    f"boundary + water + four tree fences must collapse into a single 'Fencing' legend entry, "
    f"got {_trim_legend_texts}"
)

# 7. No ordering dependency: the trim is symmetric and every zone is compared against the
#    OTHER zones' ORIGINAL (pre-trim) rings, never against an already-trimmed result -- so
#    feeding the same zones in reverse order must draw byte-identical geometry.
_trim_reversed_features = [f for f in _trim_fencing_geojson["features"] if f["properties"]["fence_type"] == "boundary"]
_trim_reversed_features += list(
    reversed([f for f in _trim_fencing_geojson["features"] if f["properties"]["fence_type"] != "boundary"])
)
_trim_reversed_layers = dict(_trim_layers)
_trim_reversed_layers["fencing_result"] = {
    "fencing_geojson": {"type": "FeatureCollection", "features": _trim_reversed_features},
    "segment_count": 1,
}
_trim_reversed_drawn = _render_capturing_fences(_trim_reversed_layers, [])
_trim_forward_union = unary_union(_trim_zone_drawn)
_trim_reversed_union = unary_union([g for g, z in _trim_reversed_drawn if z == rlm.EXCLUSION_FENCE_ZORDER])
assert _trim_forward_union.symmetric_difference(_trim_reversed_union).length < 1e-9, (
    "the mutual trim must be symmetric -- reversing the zone order must not change what gets drawn"
)

# 8. fencing_geojson (the narrative report's own data) is completely unaffected: every
#    zone's full, untrimmed ring survives the render byte-for-byte.
assert json.dumps(_trim_fencing_geojson, sort_keys=True) == _trim_fencing_geojson_before, (
    "fencing_geojson must be completely unaffected by this render-only trim"
)

print(
    "Symmetric zone-fence mutual trim: each of two adjacent tree zones, and a water zone adjacent to a tree "
    f"zone, loses its near-shared stretch (tree1 keeps {_trim_kept_fraction('tree1'):.0%}, tree2 -- which "
    f"borders two neighbours -- keeps {_trim_kept_fraction('tree2'):.0%}, water keeps "
    f"{_trim_kept_fraction('water'):.0%}), leaving a real gap between each pair's separately-drawn lines "
    "(no touching, no overlap, no merged outline); a lone non-adjacent zone still renders its full closed "
    "loop, the boundary fence is drawn untrimmed, reversing the zone order changes nothing (symmetric, no "
    "rank), every fence collapses into a single 'Fencing' legend entry, and fencing_geojson is byte-for-byte "
    "unchanged."
)


# =====================================================================
# KEYPOINT ASTERISKS: a plain black asterisk per keypoint, identical
# on/off parcel, exactly one collapsed "Keypoint Candidates" legend entry
# regardless of count, and adding/removing keypoints never changes any
# other layer's legend entry (keypoints lead the KSOP order).
# =====================================================================

_kp_dem = {
    "array": np.full((10, 10), 100.0, dtype=np.float32),
    "resolution_meters": (5.0, 5.0),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}
_kp_cx = sum(p[0] for p in property_boundary) / len(property_boundary)
_kp_cy = sum(p[1] for p in property_boundary) / len(property_boundary)


def _make_keypoints(n: int) -> list:
    """n keypoints alternating on-parcel / off-parcel, each at a distinct
    point near the boundary centroid."""
    kps = []
    for i in range(n):
        on = i % 2 == 0
        kps.append(
            {
                "id": i,
                "valley_id": i,
                "geometry_wgs84": {"type": "Point", "coordinates": (_kp_cx + i * 1.0e-4, _kp_cy + i * 1.0e-4)},
                "elevation_m": 346.0 + i,
                "contributing_acres": 6.0 + i,
                "on_parcel": on,
                "distance_outside_boundary_m": 0.0 if on else 10.0 + i,
            }
        )
    return kps


def _kp_layers(keypoints: list, water_zone=None) -> dict:
    return {
        "dem": _kp_dem,
        "production_areas": [],
        "water_zone": water_zone,
        "road_corridor": [],
        "tree_zone_result": {"patches": []},
        "structure_site": None,
        "keypoints": keypoints,
        "water_features": {"streams": []},
        "contour_lines": [],
        "fencing_result": {"fencing_geojson": {"type": "FeatureCollection", "features": []}, "segment_count": 0},
    }


_orig_axes_plot_kp = rlm.plt.Axes.plot
_orig_legend_kp = rlm.plt.Axes.legend


def _capture_kp_render(layers: dict):
    """Renders `layers` and returns (asterisk_plot_calls, legend_labels):
    asterisk_plot_calls is the kwargs of every ax.plot() call drawing the
    (6, 2, 0) asterisk marker -- the ON-MAP keypoint symbol; legend_labels is
    the ordered list of icon-legend labels the render actually built (read off
    the real Legend, empty when nothing drew). The legend's own keypoint HANDLE
    is a Line2D built via its constructor and drawn through the stock handler,
    not via ax.plot(), so it never inflates the asterisk_plot_calls count."""
    plot_calls: list = []
    legend_labels: list = []

    def _rec_plot(self, *args, **kwargs):
        if kwargs.get("marker") == (6, 2, 0):
            plot_calls.append(kwargs)
        return _orig_axes_plot_kp(self, *args, **kwargs)

    def _rec_legend(self, *args, **kwargs):
        legend = _orig_legend_kp(self, *args, **kwargs)
        legend_labels[:] = [text.get_text() for text in legend.get_texts()]
        return legend

    with mock_patch.object(rlm.plt.Axes, "plot", autospec=True, side_effect=_rec_plot):
        with mock_patch.object(rlm.plt.Axes, "legend", _rec_legend):
            with tempfile.TemporaryDirectory() as tmpdir:
                rlm.render_layout_map(property_boundary, os.path.join(tmpdir, "m.png"), layers=layers)
    return plot_calls, legend_labels


def _kp_legend_lines(legend_labels: list) -> list:
    return [ln for ln in legend_labels if ln == rlm.LEGEND_LABEL_KEYPOINTS]


# 1. Exactly one keypoint legend entry for 1, 3, and 8 keypoints, identical text each time,
#    and one asterisk drawn per keypoint.
_kp_legend_variants = {}
for _n in (1, 3, 8):
    _plots_n, _legend_n = _capture_kp_render(_kp_layers(_make_keypoints(_n)))
    _kp_lines_n = _kp_legend_lines(_legend_n)
    assert len(_kp_lines_n) == 1, f"expected exactly one keypoint legend entry for {_n} keypoints, got {_kp_lines_n}"
    assert len(_plots_n) == _n, f"expected {_n} asterisk markers drawn, got {len(_plots_n)}"
    _kp_legend_variants[_n] = _kp_lines_n[0]
assert _kp_legend_variants[1] == _kp_legend_variants[3] == _kp_legend_variants[8] == rlm.LEGEND_LABEL_KEYPOINTS, (
    f"the single keypoint legend entry must be identical regardless of count/on-off mix: {_kp_legend_variants}"
)
print(
    f"Keypoint legend: exactly one entry ('{_kp_legend_variants[8]}') for 1, 3, and 8 keypoints "
    "(mixed on/off parcel) -- identical text, one asterisk marker drawn per keypoint."
)

# 2. Empty keypoint list: no keypoint legend entry, no asterisk, no exception (and since this
#    fixture has nothing else present either, an empty legend overall).
_plots_0, _legend_0 = _capture_kp_render(_kp_layers([]))
assert _kp_legend_lines(_legend_0) == [], "an empty keypoint list must emit no keypoint legend entry"
assert _legend_0 == [], "a fixture with nothing drawn at all must produce an empty legend (no frame)"
assert _plots_0 == [], "an empty keypoint list must draw no asterisk"
print("Empty keypoint list: no legend entry, no asterisk drawn, empty overall legend, no exception.")

# 3. One entry per feature, and keypoints don't perturb other layers: a water zone plus 8
#    keypoints vs the same water zone plus 0 keypoints must produce IDENTICAL non-keypoint
#    legend entries. Keypoints lead the KSOP order, so with both present the legend is exactly
#    [Keypoint Candidates, Water System Survey Area]; with 0 keypoints it drops to just
#    [Water System Survey Area] -- nothing else moves.
_wz_for_kp = dict(water_zone_fixture)
_plots_8, _legend_8 = _capture_kp_render(_kp_layers(_make_keypoints(8), water_zone=_wz_for_kp))
_plots_0b, _legend_0b = _capture_kp_render(_kp_layers([], water_zone=_wz_for_kp))

assert _legend_8 == [rlm.LEGEND_LABEL_KEYPOINTS, rlm.LEGEND_LABEL_WATER], (
    f"with 8 keypoints + a water zone, the legend must be [Keypoint Candidates, Water System Survey Area] "
    f"in KSOP order, got {_legend_8}"
)
assert _legend_0b == [rlm.LEGEND_LABEL_WATER], (
    f"with 0 keypoints + a water zone, the legend must be just [Water System Survey Area], got {_legend_0b}"
)
_non_kp_legend_8 = [ln for ln in _legend_8 if ln != rlm.LEGEND_LABEL_KEYPOINTS]
_non_kp_legend_0 = [ln for ln in _legend_0b if ln != rlm.LEGEND_LABEL_KEYPOINTS]
assert _non_kp_legend_8 == _non_kp_legend_0 == [rlm.LEGEND_LABEL_WATER], (
    f"non-keypoint legend entries must be identical with 8 vs 0 keypoints:\n8-kp: {_non_kp_legend_8}\n"
    f"0-kp: {_non_kp_legend_0}"
)
assert len(_plots_8) == 8 and _plots_0b == [], "8 keypoints must draw 8 asterisks, 0 keypoints draws none"
print(
    "One-entry-per-feature + no perturbation: with a water zone present, 8-keypoint and 0-keypoint renders "
    "produce identical non-keypoint legend entries, and the keypoint entry simply appears/disappears at the "
    "head of the KSOP order without moving anything else."
)

# 4. On-parcel and off-parcel keypoints draw with an IDENTICAL marker specification (no
#    variation), and the layer still contributes exactly the single collapsed keypoint entry
#    (no per-keypoint entry, no number).
_mixed = [_make_keypoints(2)[0], _make_keypoints(2)[1]]  # [0]=on-parcel, [1]=off-parcel
assert _mixed[0]["on_parcel"] is True and _mixed[1]["on_parcel"] is False
_plots_mixed, _legend_mixed = _capture_kp_render(_kp_layers(_mixed))
assert len(_plots_mixed) == 2
_spec_keys = ("marker", "markersize", "markeredgecolor", "markerfacecolor", "markeredgewidth", "linestyle", "zorder")
_spec_on = {k: _plots_mixed[0].get(k) for k in _spec_keys}
_spec_off = {k: _plots_mixed[1].get(k) for k in _spec_keys}
assert _spec_on == _spec_off, f"on-parcel and off-parcel keypoints must draw identically: {_spec_on} vs {_spec_off}"
assert _spec_on["marker"] == (6, 2, 0), "keypoints must use the true asterisk polygon marker (6, 2, 0)"
assert _spec_on["markeredgecolor"] == rlm.KEYPOINT_COLOR == "#000000", "keypoint asterisk must be black"
assert _kp_legend_lines(_legend_mixed) == [rlm.LEGEND_LABEL_KEYPOINTS], (
    f"a mixed on/off keypoint set must still collapse to one 'Keypoint Candidates' entry, got {_legend_mixed}"
)
print(
    "On-parcel and off-parcel keypoints draw with an identical marker spec "
    f"(marker={_spec_on['marker']}, size={_spec_on['markersize']}, colour={_spec_on['markeredgecolor']}); "
    "one collapsed keypoint legend entry, no number."
)

# 5. The legend's own keypoint handle reuses the SAME (6, 2, 0) asterisk marker tuple as the
#    on-map symbol, so legend and map read as the same thing (relationship asserted against the
#    module's real handle-builder, not a literal).
_kp_handle, _kp_label = rlm._legend_handle_for("keypoints")
assert _kp_label == rlm.LEGEND_LABEL_KEYPOINTS
assert _kp_handle.get_marker() == (6, 2, 0), (
    f"the keypoint legend handle must use the same (6, 2, 0) asterisk marker the map draws, got "
    f"{_kp_handle.get_marker()}"
)
assert _kp_handle.get_markeredgecolor() == rlm.KEYPOINT_COLOR, "the keypoint legend handle must be KEYPOINT_COLOR"
print(
    "Keypoint legend handle reuses the map's own (6, 2, 0) asterisk marker in KEYPOINT_COLOR -- legend and "
    "map symbol match."
)


print("\nAll render_layout_map checks passed.")
