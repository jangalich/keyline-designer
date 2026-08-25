"""
test_production_area.py

Offline (no-network) checks for production_area.py -- the foundation
module of the consolidated production-zone pipeline: STEP 1
(compute_step1_eligible_cells(), the cell-level slope + hydric-soil
eligibility gate and per-cell slope/aspect scoring) and STEP 3
(cluster_and_gate(), connected-component clustering + real cell-union
footprint + area survival gate), plus identify_production_areas() (the
"raw", un-ceiling-trimmed entry point every existing consumer --
water_candidate_zones.py, road_corridors.py, solar_suitability.py --
already reads).

Runs against small synthetic DEMs built by hand, same "pure logic,
independent of real data fetches" philosophy as the rest of this
pipeline's tests. identify_production_areas() defaults to check_soil=True
and check_roads=True (it does its own disqualifying-soil and existing-
road fetches, each gracefully degrading on failure) -- every test below
that isn't specifically about the hydric or road gate passes
check_soil=False, check_roads=False to stay fully offline, same
convention identify_optimized_production_areas()/identify_production_
area_suitability() already established elsewhere in this pipeline.

The woody-vegetation gate, unlike check_soil, is NOT optional (no
check_canopy flag exists -- see production_area.identify_production_
areas()'s own docstring) and a fetch failure is a hard error, not
something to degrade past. pa.get_canopy_height_for_boundary is patched
once, globally, right below to a fixed offline stub returning a real,
checked, tree-free HAG result for whatever DEM it's called with -- every
identify_production_areas() call in this file goes through the real
canopy-gate code path (STEP 1's cell-level exclusion, tree_root_zone_
mask(), etc.) with a controlled, always-clean input, rather than hitting
the network. The gate's own hard-failure behavior (RuntimeError on no
coverage, exceptions propagating unchanged) has its own dedicated tests
in test_canopy_height_data.py.
"""

from unittest.mock import patch as mock_patch

import numpy as np
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

import production_area as pa
from feature_schema import validate_feature_collection
from production_area import (
    cluster_and_gate,
    compute_slope_percent,
    compute_step1_eligible_cells,
    identify_production_areas,
    per_cell_score,
    production_areas_to_geojson,
)
import soil_data
from soil_data import is_disqualifying_soil_condition


def _fake_clean_canopy(boundary_coordinates, dem):
    return {
        "array": np.full(dem["array"].shape, 1.0, dtype=np.float32),  # below threshold everywhere -- no trees
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source_item_id": "offline-test-stub",
    }


pa.get_canopy_height_for_boundary = _fake_clean_canopy

RESOLUTION = (5.0, 5.0)
BASE_DEM = {
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}

# How far past the DEM's own true extent these "full extent" boundary
# fixtures below are padded outward -- compute_step1_eligible_cells() now
# always shrinks the boundary it tests cell centers against by
# PRODUCTION_BOUNDARY_SETBACK_METERS (see the dedicated boundary-setback
# tests in test_canopy_height_data.py for THAT gate specifically). Padding
# these shared fixtures by more than that setback keeps every test below
# that isn't about the setback itself isolated from it, exactly as it was
# isolated from parcel clipping before this gate existed -- the DEM's own
# extent stays fully "on-parcel" after the shrink.
_SETBACK_TEST_PADDING_METERS = pa.PRODUCTION_BOUNDARY_SETBACK_METERS + 1.0

# The DEM's own full extent (origin_y is the upper-left/max-y corner),
# padded outward by _SETBACK_TEST_PADDING_METERS — used wherever a test
# isn't specifically about parcel clipping or the boundary setback, so
# that behavior matches the pre-clipping expectations exactly (100%
# on-parcel, nothing to clip).
FULL_EXTENT_BOUNDARY = box(
    500000.0 - _SETBACK_TEST_PADDING_METERS,
    4500000.0 - 30 * 5.0 - _SETBACK_TEST_PADDING_METERS,
    500000.0 + 30 * 5.0 + _SETBACK_TEST_PADDING_METERS,
    4500000.0 + _SETBACK_TEST_PADDING_METERS,
)


def _dem(array: np.ndarray, origin_y: float = 4500000.0) -> dict:
    return {**BASE_DEM, "array": array, "origin_y": origin_y}


def _full_extent_boundary(dem: dict) -> Polygon:
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    x0, y0 = dem["origin_x"], dem["origin_y"]
    pad = _SETBACK_TEST_PADDING_METERS
    return box(x0 - pad, y0 - rows * py - pad, x0 + cols * px + pad, y0 + pad)


# --- compute_slope_percent: flat ground is ~0%, a real rise is high ---

flat = np.full((5, 5), 100.0, dtype=np.float32)
flat_slope = compute_slope_percent(flat, RESOLUTION)
assert np.nanmax(flat_slope) == 0.0, f"flat DEM should have 0% slope everywhere, got max {np.nanmax(flat_slope)}"
print("compute_slope_percent reads 0% slope on perfectly flat ground.")

steep = np.array(
    [
        [100.0, 100.0, 100.0],
        [110.0, 110.0, 110.0],  # 10m rise over one 5m cell = 200% grade
        [120.0, 120.0, 120.0],
    ],
    dtype=np.float32,
)
steep_slope = compute_slope_percent(steep, RESOLUTION)
assert steep_slope[1, 1] == 200.0, f"expected 200% grade at the middle row, got {steep_slope[1, 1]}"
print("compute_slope_percent correctly reads a steep grade.")


# --- per_cell_score / PER_CELL weights: STEP 1's own scoring, renormalized ratio, no size counterpart ---

assert abs((pa.PER_CELL_SLOPE_WEIGHT + pa.PER_CELL_ASPECT_WEIGHT) - 1.0) < 1e-9
assert abs(
    (pa.PER_CELL_SLOPE_WEIGHT / pa.PER_CELL_ASPECT_WEIGHT)
    - (pa.SLOPE_FACTOR_WEIGHT / pa.ASPECT_FACTOR_WEIGHT)
) < 1e-9, "renormalizing must preserve the EXISTING zone-level slope:aspect ratio, not invent a new one"
assert not hasattr(pa, "PER_CELL_SIZE_WEIGHT"), (
    "size_factor is a cluster-level shape property with no meaning for a single grid cell -- "
    "per-cell scoring must not invent a per-cell size weight"
)
assert per_cell_score(0.0, 180.0) == 1.0, "flat, due-south ground must score the max, 1.0"
worst = per_cell_score(pa.MAX_PRODUCTION_SLOPE_PCT, 0.0)
assert abs(worst - 0.0) < 1e-9, f"max slope + due-north aspect should score ~0.0, got {worst}"
print(
    f"Per-cell weights sum to 1.0 (slope={pa.PER_CELL_SLOPE_WEIGHT:.4f}, aspect={pa.PER_CELL_ASPECT_WEIGHT:.4f}), "
    "preserve the zone-level 0.55:0.15 ratio exactly, and no per-cell size weight exists."
)


# =====================================================================
# _cell_union_footprint(): grid-seam fix -- adjacent cell squares must
# dissolve into ONE clean polygon under unary_union(), not leave razor-
# thin sliver gaps. Confirmed live: this ONLY reproduces with realistic,
# large-magnitude UTM origin values -- this file's usual "nice" round-
# number origin/resolution convention (e.g. 500000.0/5.0, used
# everywhere else below) happens to never trigger the underlying
# floating-point issue, so this test deliberately uses "ugly" values
# representative of a real DEM fetch instead.
# =====================================================================


def _old_buggy_cell_union_footprint(cells, dem):
    """Reimplements the OLD (pre-fix) center-then-half-width approach
    directly (computing each square from pixel_center_xy() +/- half a
    cell width, rather than snapping both neighbors' shared edge to the
    same origin-relative expression) -- proves this test's DEM values
    actually reproduce the grid-seam bug, not just that the NEW
    _cell_union_footprint() happens to produce a clean polygon regardless
    of input."""
    px, py = dem["resolution_meters"]
    squares = []
    for r, c in cells:
        x = dem["origin_x"] + (c + 0.5) * px
        y = dem["origin_y"] - (r + 0.5) * py
        squares.append(box(x - px / 2, y - py / 2, x + px / 2, y + py / 2))
    return unary_union(squares)


grid_seam_dem = {
    "resolution_meters": (0.9144, 0.9144),  # ~3 ft -- a real lidar-derived resolution, not a round number
    "origin_x": 583412.371,
    "origin_y": 4498217.933,
    "crs": "EPSG:32617",
}
grid_seam_cells = [(r, c) for r in range(60) for c in range(60)]  # one large, solid, contiguous 60x60 blob

old_buggy_footprint = _old_buggy_cell_union_footprint(grid_seam_cells, grid_seam_dem)
assert old_buggy_footprint.geom_type == "MultiPolygon" and len(list(old_buggy_footprint.geoms)) > 1, (
    "test setup should genuinely reproduce the old grid-seam bug at these DEM values -- otherwise this "
    "regression test isn't actually testing anything real"
)
print(
    f"(confirmed the OLD center-then-half-width approach genuinely fragments this exact solid blob into "
    f"{len(list(old_buggy_footprint.geoms))} sliver pieces at realistic UTM-scale coordinates)"
)

grid_seam_footprint = pa._cell_union_footprint(grid_seam_cells, grid_seam_dem)

SLIVER_AREA_EPSILON_SQM = 1e-6  # any "part" below this is floating-point noise, not a real disconnected piece
if grid_seam_footprint.geom_type == "MultiPolygon":
    sliver_parts = [g for g in grid_seam_footprint.geoms if g.area < SLIVER_AREA_EPSILON_SQM]
    assert not sliver_parts, (
        f"a single contiguous 60x60 blob must dissolve into ONE clean polygon -- got a MultiPolygon with "
        f"{len(list(grid_seam_footprint.geoms))} parts, {len(sliver_parts)} of them near-zero-area slivers "
        "(the exact grid-seam bug this fix addresses)"
    )

assert grid_seam_footprint.geom_type == "Polygon", (
    f"a single contiguous, solid blob at realistic UTM-scale coordinates must union into a single clean "
    f"Polygon (no grid-seam sliver fragmentation), got {grid_seam_footprint.geom_type}"
)
expected_area = 60 * 60 * grid_seam_dem["resolution_meters"][0] * grid_seam_dem["resolution_meters"][1]
assert abs(grid_seam_footprint.area - expected_area) < 1e-6, (
    f"the dissolved footprint's area must exactly match the real cell-count area (no slivers double-counted "
    f"or lost), got {grid_seam_footprint.area}, expected {expected_area}"
)
print(
    f"_cell_union_footprint() grid-seam fix: the SAME solid 60x60 blob at the SAME realistic UTM coordinates "
    f"dissolves into a single clean Polygon with no sliver gaps, area exactly matching {expected_area} sq m."
)


# --- identify_production_areas: finds the flat bench, not the steep rise ---

size = 30
array = np.zeros((size, size), dtype=np.float32)
for row in range(size):
    for col in range(size):
        array[row, col] = 100.0 if row < 15 else 100.0 + (row - 14) * 5.0

patches = identify_production_areas(_dem(array), FULL_EXTENT_BOUNDARY, check_soil=False, check_roads=False)
assert len(patches) == 1, f"expected exactly 1 production-area patch, got {len(patches)}"
patch = patches[0]
assert patch["representative_elevation_m"] == 100.0, (
    f"the flat bench is uniformly 100m, expected that as the representative elevation, "
    f"got {patch['representative_elevation_m']}"
)
assert patch["polygon_utm"].geom_type == "Polygon"
assert patch["geometry_wgs84"]["type"] == "Polygon"
assert "cells" in patch and len(patch["cells"]) > 0, "STEP 3 output must carry its own constituent cells"
assert "source_patch_id" in patch
print(
    f"identify_production_areas isolates the flat bench "
    f"({patch['area_acres']} acres, {patch['representative_elevation_m']}m) and excludes the steep rise."
)

huge_area_threshold = patch["area_acres"] * 100
assert identify_production_areas(_dem(array), FULL_EXTENT_BOUNDARY, min_area_acres=huge_area_threshold, check_soil=False, check_roads=False) == []
print("Raising min_area_acres above the found patch's size correctly drops it.")


# --- regression: candidates are clipped to the real parcel boundary, not the buffered DEM extent ---

west_half_boundary = box(500000.0, 4500000.0 - 30 * 5.0, 500075.0, 4500000.0)
west_half_patches = identify_production_areas(_dem(array), west_half_boundary, check_soil=False, check_roads=False)
assert len(west_half_patches) == 1, (
    f"expected the bench to still qualify as a (smaller) candidate on the west-half boundary, "
    f"got {len(west_half_patches)} patch(es)"
)
west_patch = west_half_patches[0]
assert west_patch["area_acres"] < patch["area_acres"], (
    f"a boundary covering only half the bench should clip its area down "
    f"(full: {patch['area_acres']} acres, west-half boundary: {west_patch['area_acres']} acres)"
)
on_parcel_fraction = (
    west_patch["polygon_utm"].intersection(west_half_boundary).area / west_patch["polygon_utm"].area
)
assert on_parcel_fraction > 0.999, (
    f"the returned candidate geometry must itself be (effectively) 100% on-parcel after clipping, "
    f"got {on_parcel_fraction * 100:.1f}%"
)
assert west_patch["polygon_utm"].within(west_half_boundary.buffer(1e-6)), (
    "clipped candidate polygon must stay within the real parcel boundary"
)
print(
    f"Parcel clipping: a boundary covering only half the bench correctly shrinks the candidate "
    f"({patch['area_acres']} acres unclipped -> {west_patch['area_acres']} acres clipped), "
    f"and the returned geometry checks out as {on_parcel_fraction * 100:.1f}% on-parcel."
)

disjoint_boundary = box(600000.0, 4600000.0, 600100.0, 4600100.0)
disjoint_patches = identify_production_areas(_dem(array), disjoint_boundary, check_soil=False, check_roads=False)
assert disjoint_patches == [], (
    f"a boundary that doesn't overlap the DEM at all should drop every candidate entirely, "
    f"got {len(disjoint_patches)} patch(es)"
)
print("Parcel clipping: a boundary entirely off the DEM's extent correctly drops every candidate (0% on-parcel).")

sliver_boundary = box(500000.0, 4500000.0 - 30 * 5.0, 500002.0, 4500000.0)  # 2m wide sliver of the bench
sliver_patches = identify_production_areas(_dem(array), sliver_boundary, check_soil=False, check_roads=False)
assert sliver_patches == [], (
    f"a boundary clipping the bench down to a sliver below MIN_PRODUCTION_AREA_ACRES should drop it, "
    f"got {len(sliver_patches)} patch(es)"
)
print("Parcel clipping: a boundary clipping the bench below the minimum area correctly drops it.")


# --- production_areas_to_geojson: schema-valid with the diagnostic layer name ---

geojson = production_areas_to_geojson(patches)
validate_feature_collection(geojson)
assert geojson["features"][0]["properties"]["layer"] == "production_area_candidate"
print("production_areas_to_geojson output is schema-valid with layer='production_area_candidate'.")


# =====================================================================
# STEP 1 cell-level hydric exclusion: computed ONCE, before clustering
# =====================================================================

# A simple, clean case: half the flat bench (rows < 15) is disqualifying
# (hydric) soil, cut along a cell boundary so both approaches (cell-center
# test vs continuous differencing) would agree on the outcome here -- this
# isolates that the HYDRIC GATE ITSELF works correctly, separate from the
# fragmentation-avoidance test below.

hydric_half = box(500000.0, 4500000.0 - 15 * 5.0, 500000.0 + 30 * 5.0, 4500000.0)  # south half of the bench rows

step1_clean = compute_step1_eligible_cells(_dem(array), FULL_EXTENT_BOUNDARY, disqualifying_soil_union_utm=None)
assert step1_clean["soil_data_available"] is True
assert np.array_equal(step1_clean["eligible_mask"], step1_clean["slope_only_mask"]), (
    "a real, checked, but empty (None) disqualifying union must exclude nothing"
)
print("compute_step1_eligible_cells(): a checked-and-clean (None) disqualifying union excludes nothing.")

step1_unchecked = compute_step1_eligible_cells(_dem(array), FULL_EXTENT_BOUNDARY)
assert step1_unchecked["soil_data_available"] is False, "omitting disqualifying_soil_union_utm must read as 'never checked'"
assert np.array_equal(step1_unchecked["eligible_mask"], step1_unchecked["slope_only_mask"])
print("compute_step1_eligible_cells(): omitting the soil union reads as 'never checked' and excludes nothing (degrades gracefully).")

step1_hydric = compute_step1_eligible_cells(_dem(array), FULL_EXTENT_BOUNDARY, disqualifying_soil_union_utm=hydric_half)
assert step1_hydric["soil_data_available"] is True
eligible_before = int(step1_hydric["slope_only_mask"].sum())
eligible_after = int(step1_hydric["eligible_mask"].sum())
assert eligible_after < eligible_before, "hydric exclusion must actually remove cells from the eligible mask"
# Only the flat bench (rows < 15) was ever slope-eligible; the hydric half
# exactly covers those same rows, so ALL slope-eligible cells should be
# excluded here.
assert eligible_after == 0, (
    f"the hydric polygon covers exactly the flat bench's own rows -- every slope-eligible cell should be "
    f"excluded, but {eligible_after} remain"
)
print(
    f"compute_step1_eligible_cells(): a disqualifying union covering the entire slope-eligible bench correctly "
    f"excludes all {eligible_before} of its cells at the cell level, before any clustering happens."
)

# A PARTIAL hydric overlap -- only the western half of the bench -- must
# leave the eastern half eligible, demonstrating this is a partial,
# cell-level exclusion, not a whole-region veto (the real bug the
# pre-consolidation architecture's own module docstring warned against
# reintroducing).
hydric_west_of_bench = box(500000.0, 4500000.0 - 15 * 5.0, 500000.0 + 15 * 5.0, 4500000.0)
step1_partial = compute_step1_eligible_cells(_dem(array), FULL_EXTENT_BOUNDARY, disqualifying_soil_union_utm=hydric_west_of_bench)
partial_eligible = int(step1_partial["eligible_mask"].sum())
assert 0 < partial_eligible < eligible_before, (
    f"a hydric polygon covering only the west half of the bench should exclude some, but not all, of its cells, "
    f"got {partial_eligible} of {eligible_before} remaining eligible"
)
patches_partial = cluster_and_gate(step1_partial["eligible_mask"], _dem(array), FULL_EXTENT_BOUNDARY, step1_partial)
assert len(patches_partial) == 1, "the surviving (eastern) half of the bench should still cluster as one clean patch"
print(
    f"compute_step1_eligible_cells(): a PARTIAL hydric overlap (west half of the bench) excludes only the "
    f"overlapping cells ({eligible_before - partial_eligible} of {eligible_before}), leaving the eastern half "
    "intact as one clean surviving cluster -- a partial wet inclusion doesn't veto the whole region."
)


# =====================================================================
# STEP 1 cell-level existing-road exclusion: same shapely-polygon-union-plus-
# per-cell-.contains() pattern as hydric soil above (NOT canopy's raster
# dilation) -- road_exclusion_union_utm follows disqualifying_soil_union_utm's
# EXACT convention, including the real-None-means-checked-and-clean state.
# =====================================================================

road_full_bench = box(500000.0, 4500000.0 - 15 * 5.0, 500000.0 + 30 * 5.0, 4500000.0)  # same footprint as hydric_half

step1_road_unchecked = compute_step1_eligible_cells(_dem(array), FULL_EXTENT_BOUNDARY)
assert step1_road_unchecked["road_data_available"] is False, "omitting road_exclusion_union_utm must read as 'never checked'"
assert np.array_equal(step1_road_unchecked["eligible_mask"], step1_road_unchecked["slope_only_mask"])
print("compute_step1_eligible_cells(): omitting the road exclusion union reads as 'never checked' and excludes nothing (degrades gracefully).")

step1_road_clean = compute_step1_eligible_cells(_dem(array), FULL_EXTENT_BOUNDARY, road_exclusion_union_utm=None)
assert step1_road_clean["road_data_available"] is True
assert np.array_equal(step1_road_clean["eligible_mask"], step1_road_clean["slope_only_mask"]), (
    "a real, checked, but empty (None) road exclusion union must exclude nothing"
)
print("compute_step1_eligible_cells(): a checked-and-clean (None) road exclusion union excludes nothing.")

step1_road_full = compute_step1_eligible_cells(_dem(array), FULL_EXTENT_BOUNDARY, road_exclusion_union_utm=road_full_bench)
assert step1_road_full["road_data_available"] is True
road_eligible_before = int(step1_road_full["slope_only_mask"].sum())
road_eligible_after = int(step1_road_full["eligible_mask"].sum())
assert road_eligible_after == 0, (
    f"the road exclusion polygon covers exactly the flat bench's own rows -- every slope-eligible cell should be "
    f"excluded, but {road_eligible_after} remain"
)
assert int(step1_road_full["road_hit"].sum()) == road_eligible_before, "road_hit must mark every excluded cell"
print(
    f"compute_step1_eligible_cells(): a road exclusion union covering the entire slope-eligible bench correctly "
    f"excludes all {road_eligible_before} of its cells at the cell level, before any clustering happens."
)

road_west_of_bench = box(500000.0, 4500000.0 - 15 * 5.0, 500000.0 + 15 * 5.0, 4500000.0)
step1_road_partial = compute_step1_eligible_cells(_dem(array), FULL_EXTENT_BOUNDARY, road_exclusion_union_utm=road_west_of_bench)
road_partial_eligible = int(step1_road_partial["eligible_mask"].sum())
assert 0 < road_partial_eligible < road_eligible_before, (
    f"a road exclusion polygon covering only the west half of the bench should exclude some, but not all, of its "
    f"cells, got {road_partial_eligible} of {road_eligible_before} remaining eligible"
)
print(
    f"compute_step1_eligible_cells(): a PARTIAL road exclusion overlap (west half of the bench) excludes only the "
    f"overlapping cells ({road_eligible_before - road_partial_eligible} of {road_eligible_before}), leaving the "
    "eastern half eligible -- a partial road overlap doesn't veto the whole region."
)

# --- combined gate stack: hydric AND road exclusion both apply simultaneously, disjoint halves ---
step1_combined = compute_step1_eligible_cells(
    _dem(array),
    FULL_EXTENT_BOUNDARY,
    disqualifying_soil_union_utm=hydric_west_of_bench,
    road_exclusion_union_utm=road_west_of_bench,
)
assert int(step1_combined["eligible_mask"].sum()) == road_partial_eligible, (
    "hydric and road exclusion covering the exact same west-half footprint should exclude the same cells whether "
    "applied by one gate or both together -- combining them must not double-exclude or under-exclude"
)
print("compute_step1_eligible_cells(): hydric-soil and road-exclusion gates combine correctly (both applied, no double-counting).")


# =====================================================================
# Regression test: cell-level exclusion avoids the sliver-fragmentation
# that continuous-geometry hull/footprint-vs-soil-polygon differencing
# caused (the real bug found live: ~65% of genuinely usable ground lost
# to spurious sub-minimum-area fragments on a real property boundary).
#
# Construction: a single large, solid, flat eligible region (a strip),
# and a "jagged" disqualifying-soil geometry built from many thin teeth,
# each centered exactly on a CELL-COLUMN BOUNDARY (not a cell center) and
# narrower than the distance to either neighboring cell's own center.
#
#   - Cell-CENTER testing (this pipeline's STEP 1, post-consolidation):
#     no cell center falls inside any tooth (they're too thin and
#     positioned between cells) -- nothing is excluded, and the region
#     survives clustering as ONE clean patch.
#   - Continuous-geometry differencing (the OLD, pre-consolidation
#     approach superseded by this pass): each tooth still overlaps real
#     portions of its two neighboring cells' own ground SQUARES (a
#     square's edge sits right where the tooth is, even though its
#     CENTER doesn't), so .difference() slices the real footprint into
#     one thin, sub-minimum-area sliver PER COLUMN -- every one of them
#     dropped by the old per-piece area filter, losing the ENTIRE region
#     to fragmentation that never should have existed.
# =====================================================================

strip_rows, strip_cols = 10, 100
strip_array = np.full((strip_rows, strip_cols), 100.0, dtype=np.float32)  # flat, fully slope-eligible
strip_dem = _dem(strip_array, origin_y=4500100.0)
strip_boundary = _full_extent_boundary(strip_dem)

px, py = RESOLUTION
tooth_half_width = 0.25  # meters -- far narrower than a 5m cell, and centered on a cell boundary
teeth = []
for k in range(1, strip_cols):  # one tooth on every internal column boundary
    boundary_x = strip_dem["origin_x"] + k * px
    teeth.append(
        box(
            boundary_x - tooth_half_width,
            strip_dem["origin_y"] - strip_rows * py,
            boundary_x + tooth_half_width,
            strip_dem["origin_y"],
        )
    )
jagged_hydric_union = unary_union(teeth)

# --- what the OLD (superseded) continuous-geometry approach would have done ---
real_footprint = pa._cell_union_footprint(
    [(r, c) for r in range(strip_rows) for c in range(strip_cols)], strip_dem
)
old_style_remainder = real_footprint.difference(jagged_hydric_union)
old_style_pieces = list(old_style_remainder.geoms) if old_style_remainder.geom_type == "MultiPolygon" else [old_style_remainder]
MIN_AREA_SQM = pa.MIN_PRODUCTION_AREA_ACRES * 4046.8564224
old_style_surviving_pieces = [p for p in old_style_pieces if p.area >= MIN_AREA_SQM]
assert len(old_style_pieces) > 50, (
    f"test setup should genuinely fragment under continuous differencing (one sliver per column boundary), "
    f"got {len(old_style_pieces)} piece(s)"
)
assert old_style_surviving_pieces == [], (
    "every old-style sliver should be a thin, sub-minimum-area column strip -- none should survive the old "
    "per-piece area filter, demonstrating the real ground lost to fragmentation that never should have existed"
)
print(
    f"OLD-style continuous-geometry differencing: the jagged soil polygon fragments the real footprint into "
    f"{len(old_style_pieces)} disconnected pieces, ALL below MIN_PRODUCTION_AREA_ACRES -- 100% of this region "
    "would have been lost to spurious fragmentation."
)

# --- what the NEW, consolidated cell-level pipeline actually does ---
step1_jagged = compute_step1_eligible_cells(strip_dem, strip_boundary, disqualifying_soil_union_utm=jagged_hydric_union)
assert int(step1_jagged["slope_only_mask"].sum()) == strip_rows * strip_cols
assert int(step1_jagged["eligible_mask"].sum()) == strip_rows * strip_cols, (
    "no cell CENTER should fall inside any tooth (each is centered on a column boundary, well clear of every "
    "cell's own center) -- cell-level testing must exclude nothing here"
)
jagged_patches = cluster_and_gate(step1_jagged["eligible_mask"], strip_dem, strip_boundary, step1_jagged)
assert len(jagged_patches) == 1, (
    f"the consolidated pipeline should report ONE clean surviving cluster for the whole strip, got "
    f"{len(jagged_patches)}"
)
recovered_acres = jagged_patches[0]["area_acres"]
full_strip_acres = real_footprint.area / 4046.8564224
assert abs(recovered_acres - round(full_strip_acres, 2)) < 0.01, (
    f"the full strip's real acreage ({round(full_strip_acres, 2)}) should survive essentially intact, "
    f"got {recovered_acres}"
)
print(
    f"NEW cell-level pipeline: the SAME jagged soil polygon excludes ZERO cells (no cell center falls inside "
    f"any tooth) and clusters cleanly into 1 patch covering the full {recovered_acres} acres -- confirming "
    "cell-level exclusion avoids the sliver-fragmentation the old continuous-geometry approach caused, by "
    "construction."
)

# Also confirm identify_production_areas() end-to-end (with the soil fetch mocked) reaches the same conclusion.
# The module-level pa.get_canopy_height_for_boundary stub keeps this offline for the (now-mandatory)
# canopy gate too -- this test is specifically about the hydric-gate integration (soil fetch mocked
# above), not the canopy gate, which has its own dedicated tests/mocking in test_canopy_height_data.py.
with mock_patch.object(pa, "_fetch_disqualifying_soil_union", lambda wkt, dem: jagged_hydric_union):
    end_to_end_patches = identify_production_areas(strip_dem, strip_boundary, check_soil=True, check_roads=False)
assert len(end_to_end_patches) == 1
assert abs(end_to_end_patches[0]["area_acres"] - recovered_acres) < 0.01
print("identify_production_areas() end-to-end (soil fetch mocked) reaches the identical, non-fragmented result.")


# =====================================================================
# identify_production_areas(): check_roads works end-to-end, and -- unlike the
# mandatory canopy gate -- degrades GRACEFULLY on fetch failure rather than raising.
# =====================================================================

with mock_patch.object(pa, "get_road_exclusion_union_utm", lambda boundary_coordinates, dem, buffer_meters=None: road_full_bench):
    end_to_end_road_patches = identify_production_areas(_dem(array), FULL_EXTENT_BOUNDARY, check_soil=False, check_roads=True)
assert end_to_end_road_patches == [], (
    "identify_production_areas() end-to-end: a mocked road exclusion covering the entire slope-eligible bench "
    "should leave zero surviving patches, same conclusion as the direct compute_step1_eligible_cells() check above"
)
print("identify_production_areas(): a real (mocked) road exclusion union is correctly wired through end-to-end.")


def _raise_road_network_failure(boundary_coordinates, dem, buffer_meters=None):
    raise RuntimeError("simulated: road fetch retries exhausted")


with mock_patch.object(pa, "get_road_exclusion_union_utm", _raise_road_network_failure):
    degraded_patches = identify_production_areas(_dem(array), FULL_EXTENT_BOUNDARY, check_soil=False, check_roads=True)
assert len(degraded_patches) == 1, (
    "unlike the canopy gate, a road fetch failure must degrade GRACEFULLY (slope-only eligibility), not raise or "
    "crash identify_production_areas()"
)
print(
    "identify_production_areas(): a road fetch failure degrades gracefully (unlike the mandatory canopy gate) -- "
    "no exception, proceeds without the road exclusion."
)


# =====================================================================
# is_disqualifying_soil_condition() / _fetch_disqualifying_soil_union():
# moved here from test_production_suitability.py -- the fetch now lives in
# production_area.py (STEP 1 needs it before any patch/cluster exists).
# =====================================================================

assert is_disqualifying_soil_condition("Yes") is not None
assert is_disqualifying_soil_condition("Partially hydric") is not None
assert is_disqualifying_soil_condition("No") is None
assert is_disqualifying_soil_condition(None) is None
assert is_disqualifying_soil_condition("") is None
print("is_disqualifying_soil_condition() correctly flags hydric/partially-hydric and nothing else.")


def _fake_soil_rows_with_hydric(wkt_polygon):
    return [
        {"mukey": "1", "muname": "Rayne silt loam", "compname": "Rayne", "comppct_r": 100, "drainagecl": "Well drained", "hydricrating": "No"},
        {"mukey": "2", "muname": "Wet inclusion", "compname": "Atkins", "comppct_r": 90, "drainagecl": "Poorly drained", "hydricrating": "Yes"},
        {"mukey": "2", "muname": "Wet inclusion", "compname": "Upland part", "comppct_r": 10, "drainagecl": "Well drained", "hydricrating": "No"},
        {"mukey": "3", "muname": "Droughty-but-dry non-wetland", "compname": "Ernest", "comppct_r": 100, "drainagecl": "Very poorly drained", "hydricrating": "No"},
    ]


def _fake_soil_geometries(wkt_polygon):
    return {
        "2": {
            "type": "Polygon",
            "coordinates": [[[-79.984, 40.645], [-79.983, 40.645], [-79.983, 40.646], [-79.984, 40.646], [-79.984, 40.645]]],
        },
        "3": {
            "type": "Polygon",
            "coordinates": [[[-79.982, 40.645], [-79.981, 40.645], [-79.981, 40.646], [-79.982, 40.646], [-79.982, 40.645]]],
        },
    }


with mock_patch.object(pa, "get_soil_data_for_polygon", _fake_soil_rows_with_hydric), \
     mock_patch.object(pa, "get_soil_geometries_for_polygon", _fake_soil_geometries):
    union = pa._fetch_disqualifying_soil_union(
        "polygon((-79.99 40.64, -79.98 40.64, -79.98 40.65, -79.99 40.65, -79.99 40.64))", _dem(array)
    )

from shapely.geometry import shape as _shape

assert union is not None and union.geom_type in ("Polygon", "MultiPolygon")
mukey_3_geom = _shape(_fake_soil_geometries(None)["3"])
assert not union.covers(mukey_3_geom), (
    "a non-hydric 'very poorly drained' mukey must NOT be part of the disqualifying union -- "
    "only hydric rating disqualifies"
)
print("_fetch_disqualifying_soil_union() unions only the majority-hydric mukey -- a non-hydric 'very "
      "poorly drained' mukey is correctly left out (drainage class alone doesn't disqualify).")


def _fake_soil_rows_all_clean(wkt_polygon):
    return [{"mukey": "1", "muname": "Rayne silt loam", "compname": "Rayne", "comppct_r": 100, "drainagecl": "Well drained", "hydricrating": "No"}]


with mock_patch.object(pa, "get_soil_data_for_polygon", _fake_soil_rows_all_clean):
    clean_union = pa._fetch_disqualifying_soil_union(
        "polygon((-79.99 40.64, -79.98 40.64, -79.98 40.65, -79.99 40.65, -79.99 40.64))", _dem(array)
    )

assert clean_union is None, "no disqualifying components at all must return None, not an empty geometry"
print("_fetch_disqualifying_soil_union() returns None when nothing in the footprint is hydric.")


# Regression: a trace hydric component must NOT carve out an entire, mostly well-drained map unit's
# polygon. Real live numbers from the original bug report: mukey 541700 (Guernsey-Vandergrift) is
# hydric via a component that's only 1% of composition; mukey 541683 (Ernest-Vandergrift) is hydric
# via components totaling 5%+3%=8%. Both were previously excluded in full despite being 90%+
# well/moderately-well-drained -- this must not regress under the consolidated pipeline either.

def _fake_soil_rows_trace_hydric(wkt_polygon):
    return [
        {"mukey": "541658", "muname": "Atkins silt loam, frequently flooded", "compname": "Atkins", "comppct_r": 85, "hydricrating": "Yes"},
        {"mukey": "541658", "muname": "Atkins silt loam, frequently flooded", "compname": "Atkins channery part", "comppct_r": 10, "hydricrating": "No"},
        {"mukey": "541700", "muname": "Guernsey-Vandergrift silt loams", "compname": "Guernsey", "comppct_r": 55, "hydricrating": "No"},
        {"mukey": "541700", "muname": "Guernsey-Vandergrift silt loams", "compname": "Vandergrift", "comppct_r": 44, "hydricrating": "No"},
        {"mukey": "541700", "muname": "Guernsey-Vandergrift silt loams", "compname": "Wet inclusion", "comppct_r": 1, "hydricrating": "Yes"},
        {"mukey": "541683", "muname": "Ernest-Vandergrift silt loams", "compname": "Ernest", "comppct_r": 50, "hydricrating": "No"},
        {"mukey": "541683", "muname": "Ernest-Vandergrift silt loams", "compname": "Vandergrift", "comppct_r": 42, "hydricrating": "No"},
        {"mukey": "541683", "muname": "Ernest-Vandergrift silt loams", "compname": "Wet inclusion A", "comppct_r": 5, "hydricrating": "Yes"},
        {"mukey": "541683", "muname": "Ernest-Vandergrift silt loams", "compname": "Wet inclusion B", "comppct_r": 3, "hydricrating": "Yes"},
    ]


def _fake_soil_geometries_trace_hydric(wkt_polygon):
    return {
        "541658": {
            "type": "Polygon",
            "coordinates": [[[-79.984, 40.645], [-79.9838, 40.645], [-79.9838, 40.6452], [-79.984, 40.6452], [-79.984, 40.645]]],
        },
        "541700": {
            "type": "Polygon",
            "coordinates": [[[-79.99, 40.63], [-79.96, 40.63], [-79.96, 40.66], [-79.99, 40.66], [-79.99, 40.63]]],
        },
        "541683": {
            "type": "Polygon",
            "coordinates": [[[-79.98, 40.60], [-79.95, 40.60], [-79.95, 40.63], [-79.98, 40.63], [-79.98, 40.60]]],
        },
    }


assert soil_data.hydric_disqualifying_mukeys(_fake_soil_rows_trace_hydric(None)) == {"541658"}, (
    "only the 85%-dominant-hydric Atkins mukey should meet the disqualifying threshold -- "
    "the 1% and 8% trace inclusions must not"
)

with mock_patch.object(pa, "get_soil_data_for_polygon", _fake_soil_rows_trace_hydric), \
     mock_patch.object(pa, "get_soil_geometries_for_polygon", _fake_soil_geometries_trace_hydric):
    trace_union = pa._fetch_disqualifying_soil_union(
        "polygon((-79.99 40.62, -79.95 40.62, -79.95 40.66, -79.99 40.66, -79.99 40.62))", _dem(array)
    )

assert trace_union is not None
guernsey_geom = _shape(_fake_soil_geometries_trace_hydric(None)["541700"])
ernest_geom = _shape(_fake_soil_geometries_trace_hydric(None)["541683"])
assert not trace_union.intersects(guernsey_geom), (
    "the 1%-hydric Guernsey-Vandergrift mukey's large, mostly well-drained polygon must NOT be carved out"
)
assert not trace_union.intersects(ernest_geom), (
    "the 8%-hydric Ernest-Vandergrift mukey's large, mostly well-drained polygon must NOT be carved out"
)
print("_fetch_disqualifying_soil_union() carves only the genuinely, dominantly hydric Atkins mukey -- "
      "the two large, mostly well-drained mukeys with only trace hydric inclusions are correctly left whole.")


# =====================================================================
# Part 1/2: waist detection/splitting and true-hole detection -- offline
# synthetic cell-mask tests.
#
# These feed a hand-built boolean cell_mask directly into cluster_and_gate()
# (bypassing compute_step1_eligible_cells()'s own slope/soil derivation
# entirely -- cluster_and_gate() accepts "whatever cell mask a caller
# passes in", per its own docstring), so each shape below tests ONLY the
# raster morphology (erosion-based waist splitting / edge-anchored flood-
# fill hole detection), independent of slope or hydric-soil semantics.
# hole_footprints is narrative metadata only -- see production_area.py's
# own module docstring -- so these tests check ITS geometry directly, not
# any display/rendering field (there isn't one anymore).
# =====================================================================

WAIST_RESOLUTION = (5.0, 5.0)
WAIST_DEM_SHAPE = (12, 24)
WAIST_CELL_AREA_ACRES = (WAIST_RESOLUTION[0] * WAIST_RESOLUTION[1]) / pa.SQUARE_METERS_PER_ACRE


def _waist_dem(rows: int, cols: int) -> dict:
    # Perfectly flat -- compute_step1_eligible_cells() on this is only used
    # to hand cluster_and_gate() a well-formed step1 dict (it reads
    # step1['slope_source_labels'] for source_patch_id bookkeeping); the
    # actual cell_mask under test is built and passed in by hand below.
    array = np.full((rows, cols), 100.0, dtype=np.float32)
    return {
        "array": array,
        "resolution_meters": WAIST_RESOLUTION,
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }


def _mask_from_cells(shape: tuple[int, int], cells) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    for r, c in cells:
        mask[r, c] = True
    return mask


def _rect_cells(r0: int, r1: int, c0: int, c1: int) -> list[tuple[int, int]]:
    """All (row, col) cells in [r0, r1) x [c0, c1)."""
    return [(r, c) for r in range(r0, r1) for c in range(c0, c1)]


def _step1_for(dem: dict) -> dict:
    boundary = _full_extent_boundary(dem)
    return compute_step1_eligible_cells(dem, boundary, disqualifying_soil_union_utm=None)


def _gate(cell_mask: np.ndarray, dem: dict, step1: dict) -> list[dict]:
    boundary = _full_extent_boundary(dem)
    return cluster_and_gate(cell_mask, dem, boundary, step1)


# --- Dumbbell with a strip NARROWER than MIN_ZONE_WAIST_METERS: must split ---

dumbbell_dem = _waist_dem(*WAIST_DEM_SHAPE)
dumbbell_step1 = _step1_for(dumbbell_dem)

lobe_a = _rect_cells(0, 10, 0, 10)   # 10x10
lobe_b = _rect_cells(0, 10, 14, 24)  # 10x10
narrow_strip = _rect_cells(4, 6, 10, 14)  # 2 rows tall -- narrower than the 12m (~2-cell radius) erosion
dumbbell_cells = lobe_a + lobe_b + narrow_strip
dumbbell_mask = _mask_from_cells(WAIST_DEM_SHAPE, dumbbell_cells)

unsplit_footprint_acres = pa._cell_union_footprint(dumbbell_cells, dumbbell_dem).area / pa.SQUARE_METERS_PER_ACRE

dumbbell_patches = _gate(dumbbell_mask, dumbbell_dem, dumbbell_step1)
assert len(dumbbell_patches) == 2, (
    f"a dumbbell with a strip narrower than MIN_ZONE_WAIST_METERS must split into 2 clusters, "
    f"got {len(dumbbell_patches)}"
)
for p in dumbbell_patches:
    assert p["area_acres"] >= pa.MIN_PRODUCTION_AREA_ACRES, "every split sub-cluster must clear the area floor"
split_total = sum(p["area_acres"] for p in dumbbell_patches)
assert abs(split_total - round(unsplit_footprint_acres, 2)) < 0.02, (
    f"the two split sub-clusters' reclaimed acreage ({split_total}) should sum to ~the original unsplit "
    f"footprint ({round(unsplit_footprint_acres, 2)}) -- erosion only decides the split, it must not "
    "permanently lose real ground"
)
print(
    f"Waist split: a dumbbell joined by a strip narrower than MIN_ZONE_WAIST_METERS correctly splits into "
    f"{len(dumbbell_patches)} clusters (areas {[p['area_acres'] for p in dumbbell_patches]}), summing to "
    f"~{split_total} acres vs {round(unsplit_footprint_acres, 2)} unsplit."
)


# --- Reclaim adjacency + reported-geometry invariant: the reclaim step reassigns every eroded-away cell
#     to whichever resulting piece is nearest, so two split zones on a real boundary end up directly
#     adjacent -- polygon_utm.distance() == 0.0 between them -- while polygon_utm/area_acres continue to
#     reflect the full, post-reclaim footprint. The visual gap at the pinch is drawn from each piece's own
#     render_fill_polygon_utm opening instead (see test_render_layout_map.py), not from the reported
#     geometry. ---

p1, p2 = dumbbell_patches
polygon_distance = p1["polygon_utm"].distance(p2["polygon_utm"])
assert polygon_distance < 1e-9, (
    f"the two split zones' real, reported footprints (polygon_utm) must be directly adjacent with ZERO "
    f"distance after reclaim, got {polygon_distance}"
)

for p in dumbbell_patches:
    assert p["area_acres"] == round(p["polygon_utm"].area / pa.SQUARE_METERS_PER_ACRE, 2), (
        "area_acres must continue to reflect the full, POST-reclaim polygon_utm"
    )
print(
    "Reclaim adjacency: the two split zones' polygon_utm are directly adjacent (distance "
    f"{round(polygon_distance, 6)}m) and area_acres for both reflects the full, post-reclaim polygon_utm."
)


# --- render_fill_polygon_utm: a BOUNDED morphological opening (erode by r, dilate survivors back by r),
#     clipped to polygon_utm. Confirm (a) it never exceeds the real footprint, (b) it does NOT fill a WIDE
#     concavity the way the old convex hull did (an "L"'s re-entrant corner), and (c) it does NOT close a
#     pocket wider than r -- the two unbounded behaviors the convex hull got wrong and the reason it was
#     removed (three-plus modules consume this field as production EXCLUSION geometry). ---

hull_dem = _waist_dem(40, 40)
hull_step1 = _step1_for(hull_dem)
l_shape_cells = list(set(_rect_cells(5, 35, 5, 20)) | set(_rect_cells(20, 35, 5, 35)))  # a real "L", concave at its inner corner
l_shape_mask = _mask_from_cells((40, 40), l_shape_cells)
l_shape_patches = _gate(l_shape_mask, hull_dem, hull_step1)
assert len(l_shape_patches) == 1, f"a single L-shaped mask must stay one cluster, got {len(l_shape_patches)} cluster(s)"
l_patch = l_shape_patches[0]

assert not l_patch["polygon_utm"].equals(l_patch["polygon_utm"].convex_hull), (
    "test sanity check: this L-shaped fixture's own polygon_utm must genuinely be non-convex, "
    "otherwise the concavity check below isn't exercising real behavior"
)
# BOUNDED: the clipped opening never exceeds the real footprint -- the whole point of replacing the hull.
assert l_patch["render_fill_polygon_utm"].area <= l_patch["polygon_utm"].area + 1e-6, (
    "render_fill_polygon_utm (a clipped opening) must never exceed polygon_utm's area"
)
# The L's inner corner is a WIDE re-entrant, far wider than the opening radius, so the opening leaves it
# OPEN -- unlike the convex hull, which filled it in. The opening's area is therefore strictly BELOW the
# hull's on this fixture.
l_hull_area = l_patch["polygon_utm"].convex_hull.intersection(_full_extent_boundary(hull_dem)).area
assert l_patch["render_fill_polygon_utm"].area < l_hull_area, (
    "the opening must NOT fill the L's wide concave corner the way the convex hull did -- its area must be "
    f"strictly below the hull's ({l_patch['render_fill_polygon_utm'].area} vs {l_hull_area} sq m)"
)
print(
    f"Opening (bounded): render_fill_polygon_utm for a non-convex ('L') cluster is "
    f"{round(l_patch['render_fill_polygon_utm'].area)} sq m -- within polygon_utm "
    f"({round(l_patch['polygon_utm'].area)}) and strictly below the old hull ({round(l_hull_area)}), leaving "
    "the wide inner corner open instead of over-claiming it."
)

# A pocket WIDER than the opening radius stays OPEN -- the convex hull closed pockets of any size, exactly
# the unbounded over-claim being removed. 60x60m pocket, opening radius ~10m: must NOT be recovered.
big_pocket_dem = _waist_dem(60, 60)
big_pocket_step1 = _step1_for(big_pocket_dem)
big_pocket_solid = set(_rect_cells(2, 58, 2, 58))
big_pocket_hole = set(_rect_cells(20, 32, 20, 32))  # 12x12 cells = 60x60m, far wider than the opening radius
big_pocket_cells = list(big_pocket_solid - big_pocket_hole)
big_pocket_mask = _mask_from_cells((60, 60), big_pocket_cells)
big_pocket_patches = _gate(big_pocket_mask, big_pocket_dem, big_pocket_step1)
assert len(big_pocket_patches) == 1, f"a solid block with one large interior pocket must stay one cluster, got {len(big_pocket_patches)}"
big_pocket_patch = big_pocket_patches[0]
big_pocket_footprint = pa._cell_union_footprint(list(big_pocket_hole), big_pocket_dem)
assert big_pocket_patch["polygon_utm"].intersection(big_pocket_footprint).area < 1e-6, (
    "test sanity check: the pocket must genuinely be excluded ground"
)
big_recovered = big_pocket_patch["render_fill_polygon_utm"].intersection(big_pocket_footprint).area
assert big_recovered < 1e-6, (
    f"a bounded opening must NOT close a pocket wider than its radius (60x60m here) -- the convex hull did, "
    f"and that unbounded over-claim is the reason it was removed; recovered {big_recovered} of "
    f"{big_pocket_footprint.area} sq m (expected ~0)"
)
print("Opening: a 60x60m excluded pocket (far wider than the opening radius) stays OPEN -- not closed over, "
      "unlike the old unbounded convex hull.")


# --- render_fill_polygon_utm: two committed-split pieces' fills cannot overlap -- each is a bounded opening
#     clipped to its OWN polygon_utm, and the two footprints are disjoint, so overlap is impossible by
#     construction (the convex hull could, in principle, bulge one piece's fill across the pinch into the
#     other's). Checked on both the convex-lobe dumbbell above and a deliberately non-convex, notch-facing
#     pair. ---

assert p1["render_fill_polygon_utm"].intersection(p2["render_fill_polygon_utm"]).area < 1e-9, (
    "render_fill_polygon_utm for the dumbbell's two split zones must not overlap (each bounded by its own footprint)"
)
for _p in (p1, p2):
    assert _p["render_fill_polygon_utm"].area <= _p["polygon_utm"].area + 1e-6, (
        "each split piece's opening must stay within its own footprint"
    )
print(
    f"Opening (convex-lobe split): the dumbbell's two zones' fills stay "
    f"{p1['render_fill_polygon_utm'].distance(p2['render_fill_polygon_utm'])}m apart with zero overlap, each "
    "bounded by its own footprint."
)

NOTCH_SHAPE = (44, 100)
notch_dem = _waist_dem(*NOTCH_SHAPE)
notch_step1 = _step1_for(notch_dem)
notch_lobe_a = set(_rect_cells(0, 40, 0, 40)) - set(_rect_cells(10, 30, 10, 40))  # "C" opening toward lobe B
notch_lobe_b = set(_rect_cells(0, 40, 60, 100)) - set(_rect_cells(10, 30, 60, 90))  # "C" opening toward lobe A
notch_strip = set(_rect_cells(40, 42, 40, 60))  # narrow connector, well clear of both notches
notch_cells = list(notch_lobe_a | notch_lobe_b | notch_strip)
notch_mask = _mask_from_cells(NOTCH_SHAPE, notch_cells)
notch_patches = _gate(notch_mask, notch_dem, notch_step1)
assert len(notch_patches) == 2, f"expected the notched dumbbell to split into 2 clusters, got {len(notch_patches)}"
n1, n2 = notch_patches
for _n in (n1, n2):
    assert _n["render_fill_polygon_utm"].area <= _n["polygon_utm"].area + 1e-6, (
        "each notch-facing split piece's opening must stay within its own footprint"
    )
notch_overlap = n1["render_fill_polygon_utm"].intersection(n2["render_fill_polygon_utm"]).area
assert notch_overlap < 1e-9, (
    f"two bounded openings cannot overlap even for notch-facing non-convex lobes -- overlap {notch_overlap} sq m "
    "(the convex hull's theoretical bulge-overlap risk is removed by the bounded-by-footprint construction)"
)
print(
    f"Opening (non-convex split): notch-facing lobes' fills each stay within their own footprint and do not "
    f"overlap (overlap={notch_overlap} sq m) -- the hull's bulge-overlap risk is removed by construction."
)


# --- Same dumbbell, but the connecting strip is WIDER than MIN_ZONE_WAIST_METERS: must NOT split ---

wide_strip = _rect_cells(0, 10, 10, 14)  # full lobe height -- no pinch at all
wide_dumbbell_cells = lobe_a + lobe_b + wide_strip
wide_dumbbell_mask = _mask_from_cells(WAIST_DEM_SHAPE, wide_dumbbell_cells)

wide_dumbbell_patches = _gate(wide_dumbbell_mask, dumbbell_dem, dumbbell_step1)
assert len(wide_dumbbell_patches) == 1, (
    f"a dumbbell whose connecting strip is wider than MIN_ZONE_WAIST_METERS must NOT split, "
    f"got {len(wide_dumbbell_patches)} cluster(s)"
)
assert len(wide_dumbbell_patches[0]["cells"]) == len(wide_dumbbell_cells), (
    "an un-split cluster must pass through with every one of its original cells intact"
)
print(
    "Waist split: the same dumbbell shape with a WIDE connecting strip correctly stays as 1 unsplit cluster, "
    "unchanged from before this feature."
)


# --- Dumbbell where erosion produces 2 components, but one sub-cluster would fall below
#     MIN_PRODUCTION_AREA_ACRES after reclaiming -- must NOT split (step 2c) ---

small_lobe = _rect_cells(0, 7, 0, 7)     # 7x7 = 49 cells (~0.30 ac, sub-floor) -- big enough that its
                                         # center still survives the now-3-cell (24m) erosion on its own
big_lobe = _rect_cells(0, 10, 12, 22)    # 10x10 = 100 cells
tiny_strip = _rect_cells(3, 6, 7, 12)    # 3 rows tall -- narrow, erodes away at the 3-cell radius
undersized_split_cells = small_lobe + big_lobe + tiny_strip
undersized_split_mask = _mask_from_cells(WAIST_DEM_SHAPE, undersized_split_cells)
undersized_dem = _waist_dem(*WAIST_DEM_SHAPE)
undersized_step1 = _step1_for(undersized_dem)

# Confirm the test setup actually exercises step 2c: erosion alone (before
# reclaiming/gating) must find 2+ components here, so the "no split"
# outcome below comes from the area gate, not from erosion failing to
# separate the shape at all.
radius_cells = pa._waist_erosion_radius_cells(undersized_dem, pa.MIN_ZONE_WAIST_METERS)
from raster_grid import binary_erode as _binary_erode, connected_components as _connected_components

eroded_probe = _binary_erode(undersized_split_mask, radius_cells)
_, num_eroded_probe = _connected_components(eroded_probe)
assert num_eroded_probe >= 2, (
    f"test setup should genuinely erode into 2+ components (isolating step 2c's area gate specifically), "
    f"got {num_eroded_probe}"
)

undersized_patches = _gate(undersized_split_mask, undersized_dem, undersized_step1)
assert len(undersized_patches) == 1, (
    f"erosion producing 2+ components must NOT commit a split if any resulting sub-cluster would fall "
    f"below MIN_PRODUCTION_AREA_ACRES after reclaiming, got {len(undersized_patches)} cluster(s)"
)
assert len(undersized_patches[0]["cells"]) == len(undersized_split_cells), (
    "a rejected split must keep the ORIGINAL, unsplit cluster exactly as cluster_and_gate would have "
    "produced it without this feature -- not a partial/one-sided split"
)
print(
    "Waist split: a dumbbell whose erosion technically yields 2 components, but one sub-cluster would fall "
    "below MIN_PRODUCTION_AREA_ACRES after reclaiming, correctly does NOT split (step 2c) -- the original "
    "cluster passes through unchanged."
)


# --- Ring/donut (true hole): must NOT split, hole_footprints (narrative metadata) populated with the
#     real enclosed region -- and polygon_utm (the real footprint, never affected by this) naturally
#     already carries a matching interior ring of its own, same as it always has. ---

RING_SHAPE = (18, 18)
ring_dem = _waist_dem(*RING_SHAPE)
ring_step1 = _step1_for(ring_dem)

outer = set(_rect_cells(1, 17, 1, 17))     # 16x16 solid block
hole = set(_rect_cells(6, 12, 6, 12))      # 6x6 hole, well clear of the outer edge (5-cell wall all around)
ring_cells = list(outer - hole)
ring_mask = _mask_from_cells(RING_SHAPE, ring_cells)

ring_patches = _gate(ring_mask, ring_dem, ring_step1)
assert len(ring_patches) == 1, f"a true hole must NOT trigger a split -- got {len(ring_patches)} cluster(s)"
ring_patch = ring_patches[0]
assert len(ring_patch["cells"]) == len(ring_cells), "the ring's own cells must pass through unchanged (no split)"

assert len(ring_patch["hole_footprints"]) == 1, (
    f"expected exactly 1 detected hole, got {len(ring_patch['hole_footprints'])}"
)
detected_hole = ring_patch["hole_footprints"][0]
real_hole_footprint = pa._cell_union_footprint(list(hole), ring_dem)
hole_area_diff = detected_hole.symmetric_difference(real_hole_footprint).area
assert hole_area_diff < 1e-6, (
    f"detected hole_footprints must match the real enclosed region's own cell-union footprint exactly, "
    f"symmetric difference area {hole_area_diff}"
)

polygon_utm = ring_patch["polygon_utm"]
assert polygon_utm.geom_type == "Polygon"
assert len(polygon_utm.interiors) == 1, (
    f"polygon_utm (the real footprint -- the union of the ring's own cells, which simply never covers the "
    f"hole area) must carry exactly one interior ring for the one true hole, got {len(polygon_utm.interiors)}"
)
interior_ring_polygon = Polygon(polygon_utm.interiors[0])
interior_diff = interior_ring_polygon.symmetric_difference(real_hole_footprint).area
assert interior_diff < 1e-6, (
    f"polygon_utm's interior ring must match the real hole footprint, symmetric difference area {interior_diff}"
)
print(
    f"True hole: a ring/donut mask correctly does NOT split (1 cluster), hole_footprints holds the real "
    f"{round(real_hole_footprint.area / pa.SQUARE_METERS_PER_ACRE, 3)}-acre enclosed region as narrative "
    "metadata, and polygon_utm (the real footprint) naturally carries a matching interior ring on its own."
)


# --- Solid square (neither waist nor hole): passes through completely unchanged. ---

SQUARE_SHAPE = (12, 12)
square_dem = _waist_dem(*SQUARE_SHAPE)
square_step1 = _step1_for(square_dem)
square_cells = _rect_cells(1, 11, 1, 11)  # solid 10x10, well clear of the DEM edge
square_mask = _mask_from_cells(SQUARE_SHAPE, square_cells)

square_patches = _gate(square_mask, square_dem, square_step1)
assert len(square_patches) == 1, f"a solid, roughly-square mask must not split, got {len(square_patches)} cluster(s)"
square_patch = square_patches[0]
assert len(square_patch["cells"]) == len(square_cells), "a normal field must pass through with every cell intact"
assert square_patch["hole_footprints"] == [], "a solid mask with no enclosed gap must report hole_footprints=[]"
assert len(square_patch["polygon_utm"].interiors) == 0, "a clean solid mask's real footprint must carry no interior rings"
# render_fill_polygon_utm is a bounded, ASYMMETRIC, DISC opening (erode r+lead, dilate r) clipped to
# polygon_utm. The lead erode insets every straight edge by exactly one cell, and the disc rounds the
# corners, so a solid square is drawn strictly INSIDE its own footprint (no longer identical to polygon_utm,
# as it was before the lead erode was folded in).
assert square_patch["render_fill_polygon_utm"].area <= square_patch["polygon_utm"].area + 1e-6, (
    "the opening is bounded above by the footprint"
)
assert square_patch["render_fill_polygon_utm"].area < square_patch["polygon_utm"].area, (
    "the asymmetric opening's lead erode insets every edge, so a solid square's fill is strictly smaller "
    "than its footprint"
)
assert not square_patch["render_fill_polygon_utm"].is_empty
_poly_b = square_patch["polygon_utm"].bounds
_fill_b = square_patch["render_fill_polygon_utm"].bounds
_inset_x = ((_fill_b[0] - _poly_b[0]) + (_poly_b[2] - _fill_b[2])) / 2
_inset_y = ((_fill_b[1] - _poly_b[1]) + (_poly_b[3] - _fill_b[3])) / 2
assert abs(_inset_x - WAIST_RESOLUTION[0]) < 1e-6 and abs(_inset_y - WAIST_RESOLUTION[1]) < 1e-6, (
    f"the lead erode must inset each straight edge by exactly one cell ({WAIST_RESOLUTION[0]}m); "
    f"measured inset ({_inset_x}, {_inset_y})"
)
print(
    "Idempotence: a solid, roughly-square mask passes through unchanged for REPORTING (1 cluster, "
    "hole_footprints=[]) while render_fill_polygon_utm is its bounded asymmetric opening -- straight edges "
    f"inset by exactly one cell ({WAIST_RESOLUTION[0]}m), corners rounded by the disc element."
)


# --- Combined: a waist split AND a separate, unrelated true hole in one of the two resulting lobes ---

COMBINED_SHAPE = (24, 44)
combined_dem = _waist_dem(*COMBINED_SHAPE)
combined_step1 = _step1_for(combined_dem)

combined_lobe_a = set(_rect_cells(0, 10, 0, 10))     # 10x10, no hole
combined_lobe_b = set(_rect_cells(0, 22, 16, 38))    # 22x22, WITH a hole
# 6x6 hole, walled by an 8-cell margin on every side of lobe B -- thick
# enough that the wall survives the now-3-cell (24m) Part 1 erosion from
# BOTH the outer boundary and the hole edge (8 - 3 - 3 > 0), so lobe B
# keeps a surviving eroded ring rather than eroding away to nothing (a
# too-thin wall would vanish under the wider erosion, leaving only lobe A
# and thus no split to test).
combined_hole = set(_rect_cells(8, 14, 24, 30))
combined_lobe_b -= combined_hole
combined_strip = set(_rect_cells(4, 7, 10, 16))      # 3 rows tall -- narrow, erodes away at the 3-cell radius

combined_cells = list(combined_lobe_a | combined_lobe_b | combined_strip)
combined_mask = _mask_from_cells(COMBINED_SHAPE, combined_cells)

combined_patches = _gate(combined_mask, combined_dem, combined_step1)
assert len(combined_patches) == 2, (
    f"the waist must still split into 2 clusters even with an unrelated hole present, got {len(combined_patches)}"
)

# Identify which resulting sub-cluster is the "lobe B" side (its cells sit at column >= 16;
# lobe A's cells never reach past the neck, so this uniquely picks lobe B).
lobe_b_patch = next(p for p in combined_patches if any(c >= 16 for _, c in p["cells"]))
lobe_a_patch = next(p for p in combined_patches if p is not lobe_b_patch)

assert lobe_a_patch["hole_footprints"] == [], "lobe A (no hole) must report hole_footprints=[]"
assert len(lobe_b_patch["hole_footprints"]) == 1, (
    f"lobe B's own hole must be preserved on the correct resulting sub-cluster, got "
    f"{len(lobe_b_patch['hole_footprints'])} hole(s)"
)
real_combined_hole_footprint = pa._cell_union_footprint(list(combined_hole), combined_dem)
combined_hole_diff = lobe_b_patch["hole_footprints"][0].symmetric_difference(real_combined_hole_footprint).area
assert combined_hole_diff < 1e-6, (
    f"lobe B's detected hole must match the real hole geometry, symmetric difference area {combined_hole_diff}"
)
assert len(lobe_b_patch["polygon_utm"].interiors) == 1, (
    "lobe B's real footprint (polygon_utm) must carry an interior ring for its own hole"
)
assert len(lobe_a_patch["polygon_utm"].interiors) == 0, (
    "lobe A's real footprint (polygon_utm) must carry no interior ring -- the hole belongs to lobe B only"
)
print(
    f"Combined: a dumbbell with BOTH a waist and a separate hole in one lobe correctly splits into "
    f"{len(combined_patches)} clusters, with the hole preserved on the correct resulting sub-cluster only."
)


# --- canopy_height override forwarding ---
#
# identify_production_areas()'s mandatory woody-vegetation gate now accepts
# a pre-fetched canopy_height override (the SAME dict get_canopy_height_for_
# boundary()/parcel_data.ParcelData.canopy_height carry). When supplied it
# must be forwarded to get_required_tree_root_zone_mask_utm() so NO network
# canopy fetch happens and the exact supplied array is what the gate runs on.
# The shared-core behavior (fetch-or-use, no-override regression, hard-fail
# unchanged) is proven in test_canopy_mask_override.py; this proves THIS
# entry point actually forwards it. Reuses the flat-bench fixture above.
from _canopy_override_probe import CanopyOverrideProbe, clean_canopy_for  # noqa: E402

_ov_dem = _dem(array)
_ov_override = clean_canopy_for(_ov_dem)
with CanopyOverrideProbe() as _ov_probe:
    _ov_patches = identify_production_areas(
        _ov_dem, FULL_EXTENT_BOUNDARY, check_soil=False, check_roads=False, canopy_height=_ov_override
    )
_ov_probe.assert_override_used(_ov_override, "identify_production_areas()")
# Regression: the override path yields the same candidate geometry the
# self-fetched clean-canopy path above did (canopy sourcing is all that changed).
assert len(_ov_patches) == 1 and _ov_patches[0]["area_acres"] == patch["area_acres"], (
    "identify_production_areas(): supplying a clean canopy_height override must yield the SAME "
    "flat-bench candidate the self-fetched clean-canopy path produced"
)
print(
    "identify_production_areas(): a supplied canopy_height override is forwarded to the mandatory "
    "woody-vegetation gate -- 0 canopy fetches, exact override array used, identical candidate output."
)


# ===========================================================================
# STEP 1 CONSUMES THE EXCLUSION RESULT -- BIT-IDENTICAL, AND OPTIONAL
# ===========================================================================
#
# compute_step1_eligible_cells() now takes an exclusion_result= override: an
# exclusion_zones.identify_exclusion_zones() result whose five gate masks it
# consumes instead of computing the same five gates a second time.
#
# THE WHOLE CLAIM OF THAT CHANGE IS THAT IT CHANGES NOTHING. exclusion_zones.
# py computes the identical gates from the identical helpers (it imports them
# from production_area.py) at the identical thresholds (imported from here
# too), over the identical universe, with nothing closed, smoothed or
# buffered anywhere between the gates and the masks it publishes. So the
# override must produce a BIT-IDENTICAL step1 dict, not a similar one -- and
# that is checked array-by-array below rather than by comparing acreages,
# which would hide a handful of moved cells.
#
# The fixture deliberately fires all five gates at once, with real overlap
# between them (hydric crossing canopy, a road strip crossing both), NaN
# nodata inside the parcel, and a one-cell canopy pinhole -- the exact
# feature a closing used to absorb, and therefore the single most sensitive
# probe for a re-introduced extensive pass on either side.

import exclusion_zones as _ez  # noqa: E402

_X_ROWS = _X_COLS = 56
_X_RES = (5.0, 4.99)  # deliberately non-square, as both reference DEMs are
_X_OX, _X_OY = 500000.0, 4500000.0

_x_array = np.zeros((_X_ROWS, _X_COLS), dtype=np.float64)
for _r in range(_X_ROWS):
    for _c in range(_X_COLS):
        # a near-flat bench that clears the slope gate, then a ramp that does not
        _x_array[_r, _c] = 100.0 + 0.02 * _c + (0.0 if _r < 34 else (_r - 33) * 1.4)
_x_array[12:16, 22:28] = np.nan  # a real nodata hole inside the parcel

_X_DEM = {
    "array": _x_array,
    "resolution_meters": _X_RES,
    "origin_x": _X_OX,
    "origin_y": _X_OY,
    "crs": "EPSG:32617",
}
_X_BOUNDARY = box(
    _X_OX + 3 * _X_RES[0],
    _X_OY - (_X_ROWS - 3) * _X_RES[1],
    _X_OX + (_X_COLS - 3) * _X_RES[0],
    _X_OY - 3 * _X_RES[1],
)

_X_CANOPY = np.zeros((_X_ROWS, _X_COLS), dtype=bool)
_X_CANOPY[18:30, 8:26] = True
_X_CANOPY[24, 16] = False  # the pinhole a closing would absorb
_X_CANOPY[6:11, 34:48] = True
_X_HYDRIC = box(
    _X_OX + 30 * _X_RES[0], _X_OY - 30 * _X_RES[1],
    _X_OX + 44 * _X_RES[0], _X_OY - 18 * _X_RES[1],
)
_X_ROADS = box(
    _X_OX + 5 * _X_RES[0], _X_OY - 21.4 * _X_RES[1],
    _X_OX + 50 * _X_RES[0], _X_OY - 19.6 * _X_RES[1],
)

with mock_patch.object(_ez, "get_required_tree_root_zone_mask_utm", return_value=_X_CANOPY), mock_patch.object(
    _ez, "_fetch_disqualifying_soil_union", return_value=_X_HYDRIC
), mock_patch.object(_ez, "_fetch_road_exclusion_union_utm", return_value=_X_ROADS):
    _X_EXCLUSION = _ez.identify_exclusion_zones(None, dem=_X_DEM, boundary_polygon_utm=_X_BOUNDARY)

_x_self_computed = compute_step1_eligible_cells(
    _X_DEM,
    _X_BOUNDARY,
    disqualifying_soil_union_utm=_X_HYDRIC,
    tree_root_zone_mask_utm=_X_CANOPY,
    road_exclusion_union_utm=_X_ROADS,
)
_x_consumed = compute_step1_eligible_cells(
    _X_DEM,
    _X_BOUNDARY,
    exclusion_result=_X_EXCLUSION,
)

# --- the fixture actually exercises every gate ---
assert int(_X_EXCLUSION["layers"]["canopy"]["mask"].sum()) > 0
assert int(_X_EXCLUSION["layers"]["hydric"]["mask"].sum()) > 0
assert int(_X_EXCLUSION["layers"]["roads"]["mask"].sum()) > 0
assert int(_X_EXCLUSION["layers"]["slope"]["mask"].sum()) > 0
assert int(_X_EXCLUSION["layers"]["setback"]["mask"].sum()) > 0, (
    "every one of the five gates must reject real ground on this fixture, or a bit-identity check over it "
    "proves nothing about the gate that never fired"
)
# The pinhole is a real hole in the canopy layer, so an extensive pass on
# either side would show up as a difference rather than being invisible.
assert not _X_EXCLUSION["layers"]["canopy"]["mask"][24, 16], (
    "the deliberate one-cell canopy pinhole must survive into the published canopy layer -- it is the most "
    "sensitive probe there is for a re-introduced closing"
)
assert _x_self_computed["eligible_mask"][24, 16], (
    "and production's own self-computed gate must find that same pinhole cell eligible"
)

# --- ARRAY-BY-ARRAY, not acreage-by-acreage ---
_X_ARRAY_KEYS = (
    "eligible_mask",
    "slope_only_mask",
    "slope_source_labels",
    "slope_pct",
    "aspect_deg",
    "per_cell_slope_factor",
    "per_cell_aspect_factor",
    "per_cell_score",
    "soil_carved_acres_by_cell",
    "soil_carved_pct_by_cell",
    "hydric_hit",
    "tree_root_zone_hit",
    "road_hit",
)
_X_FLAG_KEYS = ("soil_data_available", "canopy_data_available", "road_data_available")

assert set(_x_consumed) == set(_x_self_computed), (
    "the override path must return the SAME KEYS -- a missing key is a downstream KeyError several frames away"
)
assert set(_x_consumed) == set(_X_ARRAY_KEYS) | set(_X_FLAG_KEYS), (
    "this check enumerates STEP 1's whole return contract on purpose: a new key added to compute_step1_"
    "eligible_cells() without being compared here would go unverified across the override"
)

for _key in _X_ARRAY_KEYS:
    _a, _b = _x_consumed[_key], _x_self_computed[_key]
    assert _a.dtype == _b.dtype, f"{_key}: dtype {_a.dtype} vs {_b.dtype}"
    assert _a.shape == _b.shape, f"{_key}: shape {_a.shape} vs {_b.shape}"
    # NaN-aware, and BIT-level: equal_nan makes NaN==NaN, and the arrays that
    # carry no NaN (the masks, the labels) are compared exactly either way.
    assert np.array_equal(_a, _b, equal_nan=not np.issubdtype(_a.dtype, np.bool_)), (
        f"STEP 1's '{_key}' differs between the exclusion-consumed and self-computed paths -- the two "
        f"modules disagree about a gate, which is a defect in one of them, not a rounding difference "
        f"({int((_a != _b).sum())} differing cells)"
    )
for _key in _X_FLAG_KEYS:
    assert _x_consumed[_key] == _x_self_computed[_key], f"{_key}: {_x_consumed[_key]} vs {_x_self_computed[_key]}"

# The three masks whose bytes are what everything downstream is built from,
# compared as BYTES rather than as values -- an equal-but-recomputed array
# would pass array_equal; this also catches a silently copied/re-derived one
# having different memory layout.
for _key in ("eligible_mask", "slope_only_mask", "hydric_hit", "tree_root_zone_hit", "road_hit"):
    assert _x_consumed[_key].tobytes() == _x_self_computed[_key].tobytes(), f"{_key} not byte-identical"

print(
    f"STEP 1 CONSUMES THE EXCLUSION RESULT: on a {_X_ROWS}x{_X_COLS} fixture firing all five gates "
    f"(canopy {int(_X_EXCLUSION['layers']['canopy']['mask'].sum())} cells, "
    f"slope {int(_X_EXCLUSION['layers']['slope']['mask'].sum())}, "
    f"hydric {int(_X_EXCLUSION['layers']['hydric']['mask'].sum())}, "
    f"roads {int(_X_EXCLUSION['layers']['roads']['mask'].sum())}, "
    f"setback {int(_X_EXCLUSION['layers']['setback']['mask'].sum())}), all "
    f"{len(_X_ARRAY_KEYS)} returned arrays and all {len(_X_FLAG_KEYS)} availability flags are identical to "
    f"the self-computed path; eligible_mask is byte-identical at "
    f"{int(_x_consumed['eligible_mask'].sum())} cells."
)


# --- the gates are consumed, not merely agreed with -----------------------
#
# Bit-identity alone cannot distinguish "read the override" from "ignored the
# override and computed the same answer". A DELIBERATELY WRONG exclusion
# result, one whose masks nothing on this DEM would ever produce, must
# therefore change STEP 1's answer -- if it doesn't, the override is dead
# wiring and every count assertion built on it is measuring nothing.

_x_tampered = dict(_X_EXCLUSION)
_x_tampered_eligible = _X_EXCLUSION["eligible_mask"].copy()
_x_tampered_eligible[:] = False
_x_tampered["eligible_mask"] = _x_tampered_eligible
_x_tampered_step1 = compute_step1_eligible_cells(_X_DEM, _X_BOUNDARY, exclusion_result=_x_tampered)
assert int(_x_tampered_step1["eligible_mask"].sum()) == 0, (
    "a supplied exclusion result with an all-False eligible_mask must yield an all-False STEP 1 eligible "
    "mask -- if STEP 1 still finds eligible cells it is not reading the override at all"
)
print(
    "  ... and genuinely READ: a tampered all-False exclusion eligible_mask yields an all-False STEP 1 mask, "
    "so the agreement above is consumption, not coincidence."
)


# --- the thresholds are checked, not assumed ------------------------------
#
# Both modules default to the same MAX_PRODUCTION_SLOPE_PCT / PRODUCTION_
# BOUNDARY_SETBACK_METERS, but both also take them as real parameters. An
# exclusion result computed at one threshold and consumed at another would
# silently answer a different question, and nothing downstream would catch it.

for _bad_kwargs, _needle in (
    ({"max_slope_pct": pa.MAX_PRODUCTION_SLOPE_PCT + 5.0}, "max_slope_pct"),
    ({"boundary_setback_meters": pa.PRODUCTION_BOUNDARY_SETBACK_METERS + 5.0}, "boundary_setback_meters"),
):
    try:
        compute_step1_eligible_cells(_X_DEM, _X_BOUNDARY, exclusion_result=_X_EXCLUSION, **_bad_kwargs)
    except ValueError as _exc:
        assert _needle in str(_exc), f"the {_needle} mismatch must be named in the error, got: {_exc}"
    else:
        raise AssertionError(
            f"consuming an exclusion result at a different {_needle} than it was computed at must raise, "
            f"not silently combine two different questions' answers"
        )

# A different GRID is the other way to supply the wrong masks.
try:
    compute_step1_eligible_cells(
        _dem(np.zeros((10, 10), dtype=np.float64)), _X_BOUNDARY, exclusion_result=_X_EXCLUSION
    )
except ValueError as _exc:
    assert "shape" in str(_exc)
else:
    raise AssertionError("an exclusion result computed against a different grid must raise, not broadcast")
print(
    "  ... and the thresholds and grid are CHECKED: a slope-ceiling, setback or DEM-shape mismatch between "
    "the exclusion result and the STEP 1 call raises rather than combining two different questions."
)


# --- the override is genuinely OPTIONAL, and the sentinel is not None ------
#
# Every existing caller passes nothing. Those callers must be untouched --
# and a caller that passes an explicit None (e.g. `exclusion_result=ctx.
# exclusion_zones` on a context where that field is not populated yet) must
# self-compute too, exactly as if it had omitted the parameter. None as "not
# supplied" is the documented trap in this codebase; the sentinel is an
# object() precisely so a real None can never be mistaken for it.

assert pa._EXCLUSION_RESULT_NOT_SUPPLIED is not None, (
    "the 'not supplied' sentinel must NOT be None -- that collapse is the repo-wide trap this convention "
    "exists to avoid (see _CANOPY_CHECK_UNCHECKED / _ROAD_CHECK_UNCHECKED)"
)
import inspect as _x_inspect  # noqa: E402

for _fn, _owner in (
    (compute_step1_eligible_cells, "production_area.compute_step1_eligible_cells"),
    (identify_production_areas, "production_area.identify_production_areas"),
):
    _default = _x_inspect.signature(_fn).parameters["exclusion_result"].default
    assert _default is pa._EXCLUSION_RESULT_NOT_SUPPLIED, (
        f"{_owner}'s exclusion_result default must be the shared sentinel, not None and not a fresh object()"
    )

_x_explicit_none = compute_step1_eligible_cells(
    _X_DEM,
    _X_BOUNDARY,
    disqualifying_soil_union_utm=_X_HYDRIC,
    tree_root_zone_mask_utm=_X_CANOPY,
    road_exclusion_union_utm=_X_ROADS,
    exclusion_result=None,
)
for _key in _X_ARRAY_KEYS:
    _a, _b = _x_explicit_none[_key], _x_self_computed[_key]
    assert np.array_equal(_a, _b, equal_nan=not np.issubdtype(_a.dtype, np.bool_)), (
        f"an explicit exclusion_result=None must SELF-COMPUTE exactly as an omitted one does, but '{_key}' "
        f"differs -- None has been mistaken for the sentinel somewhere"
    )
assert _x_explicit_none["eligible_mask"].tobytes() == _x_self_computed["eligible_mask"].tobytes()
print(
    "  ... and the override is OPTIONAL: the sentinel is an object() (not None), both entry points default "
    "to that same shared sentinel, and an explicit exclusion_result=None self-computes bit-identically to "
    "an omitted one."
)


# --- identify_production_areas() forwards it, and stops fetching ----------
#
# The second of compute_step1_eligible_cells()' two production callers (the
# other is production_area_ceiling.optimize_production_areas(), covered in
# test_production_area_ceiling.py). It must forward the override AND skip all
# three of its own fetches when one is supplied -- forwarding it while still
# fetching would leave the duplication in place while looking like it was
# fixed.

_x_fetches = {"canopy": 0, "soil": 0, "road": 0}


def _x_count_canopy(*_a, **_k):
    _x_fetches["canopy"] += 1
    return _X_CANOPY


def _x_count_soil(*_a, **_k):
    _x_fetches["soil"] += 1
    return _X_HYDRIC


def _x_count_road(*_a, **_k):
    _x_fetches["road"] += 1
    return _X_ROADS


with mock_patch.object(pa, "get_required_tree_root_zone_mask_utm", side_effect=_x_count_canopy), mock_patch.object(
    pa, "_fetch_disqualifying_soil_union", side_effect=_x_count_soil
), mock_patch.object(pa, "_fetch_road_exclusion_union_utm", side_effect=_x_count_road):
    _x_patches_self = identify_production_areas(_X_DEM, _X_BOUNDARY, min_area_acres=0.1)
    _x_fetches_self = dict(_x_fetches)
    _x_fetches.update({"canopy": 0, "soil": 0, "road": 0})
    _x_patches_consumed = identify_production_areas(
        _X_DEM, _X_BOUNDARY, min_area_acres=0.1, exclusion_result=_X_EXCLUSION
    )
    _x_fetches_consumed = dict(_x_fetches)

assert _x_fetches_self == {"canopy": 1, "soil": 1, "road": 1}, (
    f"the no-override path must still make all three of its own fetches, got {_x_fetches_self}"
)
assert _x_fetches_consumed == {"canopy": 0, "soil": 0, "road": 0}, (
    f"identify_production_areas() must make NO gate fetch at all when an exclusion result is supplied -- "
    f"it already carries every gate's mask and data_available flag. Got {_x_fetches_consumed}"
)
assert len(_x_patches_self) == len(_x_patches_consumed) and len(_x_patches_self) > 0, (
    "the fixture must produce real patches on both paths, or this comparison is vacuous"
)
for _p_self, _p_consumed in zip(_x_patches_self, _x_patches_consumed):
    for _field in ("id", "area_acres", "representative_elevation_m", "render_fill_area_acres",
                   "source_patch_id", "cells", "hole_footprints"):
        assert _p_self[_field] == _p_consumed[_field], f"patch field '{_field}' differs across the override"
    assert _p_self["polygon_utm"].equals(_p_consumed["polygon_utm"])
    assert _p_self["render_fill_polygon_utm"].equals(_p_consumed["render_fill_polygon_utm"])
    assert _p_self["geometry_wgs84"] == _p_consumed["geometry_wgs84"]
print(
    f"identify_production_areas(): forwards the override and makes 0 gate fetches on that path (vs "
    f"{_x_fetches_self['canopy']}/{_x_fetches_self['soil']}/{_x_fetches_self['road']} canopy/soil/road "
    f"without it), producing the identical {len(_x_patches_self)} patch(es)."
)


print("\nAll production_area checks passed.")
