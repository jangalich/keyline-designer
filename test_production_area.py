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
(it does its own disqualifying-soil fetch, gracefully degrading on
failure) -- every test below that isn't specifically about the hydric
gate passes check_soil=False to stay fully offline, same convention
identify_optimized_production_areas()/identify_production_area_
suitability() already established elsewhere in this pipeline.
"""

from unittest.mock import patch as mock_patch

import numpy as np
from shapely.geometry import MultiPoint, Polygon, box
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

RESOLUTION = (5.0, 5.0)
BASE_DEM = {
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}

# The DEM's own full extent (origin_y is the upper-left/max-y corner) —
# used wherever a test isn't specifically about parcel clipping, so that
# behavior matches the pre-clipping expectations exactly (100% on-parcel,
# nothing to clip).
FULL_EXTENT_BOUNDARY = box(500000.0, 4500000.0 - 30 * 5.0, 500000.0 + 30 * 5.0, 4500000.0)


def _dem(array: np.ndarray, origin_y: float = 4500000.0) -> dict:
    return {**BASE_DEM, "array": array, "origin_y": origin_y}


def _full_extent_boundary(dem: dict) -> Polygon:
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    x0, y0 = dem["origin_x"], dem["origin_y"]
    return box(x0, y0 - rows * py, x0 + cols * px, y0)


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


# --- identify_production_areas: finds the flat bench, not the steep rise ---

size = 30
array = np.zeros((size, size), dtype=np.float32)
for row in range(size):
    for col in range(size):
        array[row, col] = 100.0 if row < 15 else 100.0 + (row - 14) * 5.0

patches = identify_production_areas(_dem(array), FULL_EXTENT_BOUNDARY, check_soil=False)
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
assert identify_production_areas(_dem(array), FULL_EXTENT_BOUNDARY, min_area_acres=huge_area_threshold, check_soil=False) == []
print("Raising min_area_acres above the found patch's size correctly drops it.")


# --- regression: candidates are clipped to the real parcel boundary, not the buffered DEM extent ---

west_half_boundary = box(500000.0, 4500000.0 - 30 * 5.0, 500075.0, 4500000.0)
west_half_patches = identify_production_areas(_dem(array), west_half_boundary, check_soil=False)
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
disjoint_patches = identify_production_areas(_dem(array), disjoint_boundary, check_soil=False)
assert disjoint_patches == [], (
    f"a boundary that doesn't overlap the DEM at all should drop every candidate entirely, "
    f"got {len(disjoint_patches)} patch(es)"
)
print("Parcel clipping: a boundary entirely off the DEM's extent correctly drops every candidate (0% on-parcel).")

sliver_boundary = box(500000.0, 4500000.0 - 30 * 5.0, 500002.0, 4500000.0)  # 2m wide sliver of the bench
sliver_patches = identify_production_areas(_dem(array), sliver_boundary, check_soil=False)
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
with mock_patch.object(pa, "_fetch_disqualifying_soil_union", lambda wkt, dem: jagged_hydric_union):
    end_to_end_patches = identify_production_areas(strip_dem, strip_boundary, check_soil=True)
assert len(end_to_end_patches) == 1
assert abs(end_to_end_patches[0]["area_acres"] - recovered_acres) < 0.01
print("identify_production_areas() end-to-end (soil fetch mocked) reaches the identical, non-fragmented result.")


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
# Part 1/2/3: waist detection/splitting, true-hole detection, and
# display_polygon_utm hole-punching -- offline synthetic cell-mask tests.
#
# These feed a hand-built boolean cell_mask directly into cluster_and_gate()
# (bypassing compute_step1_eligible_cells()'s own slope/soil derivation
# entirely -- cluster_and_gate() accepts "whatever cell mask a caller
# passes in", per its own docstring), so each shape below tests ONLY the
# raster morphology (erosion-based waist splitting / edge-anchored flood-
# fill hole detection), independent of slope or hydric-soil semantics.
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

small_lobe = _rect_cells(0, 6, 0, 6)     # 6x6 -- small, but its eroded interior still survives on its own
big_lobe = _rect_cells(0, 10, 10, 20)    # 10x10
tiny_strip = _rect_cells(2, 4, 6, 10)    # 2 rows tall -- narrow, same as the real-split case above
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


# --- Ring/donut (true hole): must NOT split, hole_footprints populated, display_polygon_utm has an interior ring ---

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

display = ring_patch["display_polygon_utm"]
assert display.geom_type == "Polygon", "display_polygon_utm must stay a single Polygon even with a hole punched out"
assert len(display.interiors) == 1, (
    f"display_polygon_utm must carry exactly one interior ring for the one true hole, got {len(display.interiors)}"
)
interior_ring_polygon = Polygon(display.interiors[0])
interior_diff = interior_ring_polygon.symmetric_difference(real_hole_footprint).area
assert interior_diff < 1e-6, (
    f"display_polygon_utm's interior ring must match the real hole footprint, symmetric difference area {interior_diff}"
)
print(
    f"True hole: a ring/donut mask correctly does NOT split (1 cluster), hole_footprints holds the real "
    f"{round(real_hole_footprint.area / pa.SQUARE_METERS_PER_ACRE, 3)}-acre enclosed region, and "
    "display_polygon_utm carries a matching interior ring."
)


# --- Solid square (neither waist nor hole): passes through completely unchanged ---

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
plain_hull = MultiPoint([pa.pixel_center_xy(square_dem, r, c) for r, c in square_cells]).convex_hull
plain_hull = plain_hull.intersection(_full_extent_boundary(square_dem))
assert square_patch["display_polygon_utm"].symmetric_difference(plain_hull).area < 1e-6, (
    "with no holes, display_polygon_utm must be exactly the plain convex hull, unchanged from before this feature"
)
assert len(square_patch["display_polygon_utm"].interiors) == 0, "a plain hull must carry no interior rings"
print(
    "Idempotence: a solid, roughly-square mask with neither a waist nor a hole passes through completely "
    "unchanged -- 1 cluster, hole_footprints=[], and a plain convex hull display_polygon_utm."
)


# --- Combined: a waist split AND a separate, unrelated true hole in one of the two resulting lobes ---

COMBINED_SHAPE = (16, 30)
combined_dem = _waist_dem(*COMBINED_SHAPE)
combined_step1 = _step1_for(combined_dem)

combined_lobe_a = set(_rect_cells(0, 10, 0, 10))     # 10x10, no hole
combined_lobe_b = set(_rect_cells(0, 16, 14, 30))    # 16x16, WITH a hole below
# 6x6 hole, walled by a 5-cell margin on every side of lobe B -- same
# thickness the standalone ring/donut test above uses, so it survives
# Part 1's erosion as its own thin ring rather than eroding away to
# nothing (a hole positioned too close to its own cluster's edge would
# vanish under erosion along with the outer boundary, which would just
# make lobe B smaller, not preserve a real hole to detect).
combined_hole = set(_rect_cells(5, 11, 19, 25))
combined_lobe_b -= combined_hole
combined_strip = set(_rect_cells(4, 6, 10, 14))      # 2 rows tall -- narrow, same as the split case above

combined_cells = list(combined_lobe_a | combined_lobe_b | combined_strip)
combined_mask = _mask_from_cells(COMBINED_SHAPE, combined_cells)

combined_patches = _gate(combined_mask, combined_dem, combined_step1)
assert len(combined_patches) == 2, (
    f"the waist must still split into 2 clusters even with an unrelated hole present, got {len(combined_patches)}"
)

# Identify which resulting sub-cluster is the "lobe B" side (its cells sit at column >= 14).
lobe_b_patch = next(p for p in combined_patches if any(c >= 14 for _, c in p["cells"]))
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
assert len(lobe_b_patch["display_polygon_utm"].interiors) == 1, (
    "lobe B's display_polygon_utm must carry an interior ring for its own hole"
)
assert len(lobe_a_patch["display_polygon_utm"].interiors) == 0, (
    "lobe A's display_polygon_utm must carry no interior ring -- the hole belongs to lobe B only"
)
print(
    f"Combined: a dumbbell with BOTH a waist and a separate hole in one lobe correctly splits into "
    f"{len(combined_patches)} clusters, with the hole preserved on the correct resulting sub-cluster only."
)


print("\nAll production_area checks passed.")
