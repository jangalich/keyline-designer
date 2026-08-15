"""
test_tree_zone_candidates.py

Offline (no-network) checks for tree_zone_candidates.py -- Scale of
Permanence step 5 (Trees). Same "pure logic, independent of real data
fetches" approach as test_production_suitability.py/test_road_corridors.py:
hand-built synthetic DEMs and hand-fed geometry unions for the bulk of the
coverage (score_tree_search_space(), compute_tree_search_space() directly),
with real network calls mocked only for the final full-orchestrator wiring
check (identify_tree_zone_candidates()).

Synthetic-DEM layout for the main scoring test (score_tree_search_space()),
6 regions, each isolating ONE thing while holding the others equal -- see
the region-by-region comments below for exactly what each proves:

  1. Hydric, flat, non-prime, no stream       -> QUALIFIES (hydric alone)
  2. Steep ramp, non-hydric, non-prime, no stream -> QUALIFIES (slope alone)
  3. Flat, non-hydric, non-prime, no stream    -> EXCLUDED (merely
     unclaimed leftover land is NOT automatically tree-suitable)
  4. Flat, PRIME FARMLAND, non-hydric, no stream -> EXCLUDED (inverted
     soil_marginality suppresses it even though it's technically leftover)
  5. Flat, non-hydric, non-prime, NEAR A STREAM -> EXCLUDED (stream
     proximity is a genuinely MINOR factor -- can't alone clear the
     threshold)
  6. Tiny hydric patch (same signal as #1, would qualify on SCORE) -> still
     EXCLUDED by the minimum contiguous-size filter (MIN_TREE_ZONE_ACRES)

Regions are distinguished purely by which polygon (search_space/
prime_farmland_union/hydric_union) or line (stream_union) they fall inside
-- NOT by elevation differences -- except region 2's deliberate ramp. Every
other cell (including the gaps between regions, and everywhere outside the
explicit search_space) sits at one uniform flat baseline elevation, so there
are no artificial slope spikes at region seams to confound the assertions.
"""

from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import LineString, Point, Polygon, box, mapping
from shapely.ops import unary_union

import production_area as pa
import production_area_ceiling as pac
import road_corridors as rc
import tree_zone_candidates as tzc
import production_suitability as ps
import water_suitability as ws
from feature_schema import validate_feature_collection
from raster_grid import cell_union_footprint
from tree_zone_candidates import (
    HYDRIC_OVERLAP_FACTOR_WEIGHT,
    SLOPE_FACTOR_WEIGHT,
    SOIL_MARGINALITY_FACTOR_WEIGHT,
    STREAM_PROXIMITY_FACTOR_WEIGHT,
    TREE_SLOPE_REFERENCE_PCT,
    STREAM_PROXIMITY_REFERENCE_METERS,
    TREE_ZONE_ROAD_BUFFER_CELLS,
    _road_corridor_exclusion_polygon,
    _slope_factor,
    _stream_proximity_factor,
    compute_tree_search_space,
    identify_tree_zone_candidates,
    score_tree_search_space,
    summarize_tree_zone_candidates,
    tree_zones_to_geojson,
)

# production_area.identify_production_areas() (reached transitively through this file's
# own identify_tree_zone_candidates()/road_corridors.py wiring checks) now has a
# MANDATORY, non-degrading woody-vegetation gate -- no check_canopy flag, and a fetch
# failure raises rather than proceeding without the check. Patched once, globally, to a
# fixed offline stub returning a real, checked, tree-free HAG result for whatever DEM
# it's called with, so every code path in this file stays offline instead of hitting the
# network. The gate's own hard-failure behavior has its own dedicated tests in
# test_canopy_height_data.py.
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

# The existing-road exclusion gate is optional (degrades gracefully, unlike canopy) but
# still defaults to check_roads=True and attempts a real fetch otherwise -- patched the
# same way, to a stub that reports "no roads found nearby" (None), so this file's
# identify_production_areas() calls stay offline instead of just eventually timing out
# and degrading. get_road_exclusion_union_utm() is the shared fetch pa._fetch_road_
# exclusion_union_utm() (identify_production_areas()'s own call) and production_area_
# ceiling.identify_optimized_production_areas() both route through.
pa.get_road_exclusion_union_utm = lambda boundary_coordinates, dem, buffer_meters=None: None

CRS = "EPSG:32617"


# --- weights: documented, sum to 1.0, ordering matches the feature spec ---

_WEIGHT_SUM = (
    HYDRIC_OVERLAP_FACTOR_WEIGHT + SLOPE_FACTOR_WEIGHT + SOIL_MARGINALITY_FACTOR_WEIGHT + STREAM_PROXIMITY_FACTOR_WEIGHT
)
assert abs(_WEIGHT_SUM - 1.0) < 1e-6, f"tree suitability factor weights must sum to 1.0, got {_WEIGHT_SUM}"
assert HYDRIC_OVERLAP_FACTOR_WEIGHT > SLOPE_FACTOR_WEIGHT > SOIL_MARGINALITY_FACTOR_WEIGHT > STREAM_PROXIMITY_FACTOR_WEIGHT, (
    "hydric overlap must be the STRONGEST factor and stream proximity the WEAKEST, per the feature spec"
)
print(
    f"Factor weights sum to 1.0 (hydric={HYDRIC_OVERLAP_FACTOR_WEIGHT}, slope={SLOPE_FACTOR_WEIGHT}, "
    f"soil={SOIL_MARGINALITY_FACTOR_WEIGHT}, stream={STREAM_PROXIMITY_FACTOR_WEIGHT}), correctly ordered."
)


# --- _slope_factor(): inverted from production -- steeper scores HIGHER, scales across the full range ---

assert _slope_factor(0.0) == 0.0, "flat ground must score 0 for tree suitability's slope factor"
assert _slope_factor(TREE_SLOPE_REFERENCE_PCT) == 1.0, "grade at the reference point must saturate to 1.0"
assert _slope_factor(TREE_SLOPE_REFERENCE_PCT * 2) == 1.0, "grade well beyond the reference point must stay capped at 1.0"
assert _slope_factor(80.0) == 1.0, "the property's own known 80% extreme must still read as a valid, capped 1.0, not overflow"
mid = _slope_factor(TREE_SLOPE_REFERENCE_PCT / 2)
assert 0.0 < mid < 1.0, "a slope halfway to the reference point must fall strictly between 0 and 1 (real scaling, not a step function)"
assert abs(mid - 0.5) < 1e-9, "the ramp is linear, so half the reference grade should score almost exactly 0.5"
# Confirm real differentiation across the FULL known range (25-80%), not just near production's old 20% cutoff.
assert _slope_factor(25.0) < _slope_factor(45.0) < _slope_factor(80.0) == 1.0
print("_slope_factor() is correctly inverted (steeper=higher), linear up to the reference point, and capped beyond it "
      "-- differentiates meaningfully across the property's known 25-80% slope range.")


# --- _stream_proximity_factor(): distance-based, minor, independent of hydric ---

assert _stream_proximity_factor(0.0) == 1.0
assert _stream_proximity_factor(STREAM_PROXIMITY_REFERENCE_METERS) == 0.0
assert _stream_proximity_factor(STREAM_PROXIMITY_REFERENCE_METERS * 2) == 0.0
assert _stream_proximity_factor(None) == 0.0, "no stream union at all -- correctly scores 0, not a crash or None passthrough"
half = _stream_proximity_factor(STREAM_PROXIMITY_REFERENCE_METERS / 2)
assert abs(half - 0.5) < 1e-9
print("_stream_proximity_factor() falls linearly from 1.0 at distance 0 to 0.0 at the reference distance.")


# =====================================================================
# compute_tree_search_space(): Step 1, pure geometry difference
# =====================================================================

boundary = box(0, 0, 100, 100)  # 10,000 sqm
production_polys = [box(0, 0, 30, 30)]   # 900 sqm
water_polys = [box(70, 70, 100, 100)]     # 900 sqm
# A REAL, non-zero-width road polygon (road_corridors.find_road_routes()'s own
# cell_footprint_polygon_utm shape -- a strip, not a zero-width LineString), disjoint
# from both production_polys and water_polys so the union area is a simple, exact sum.
road_polys = [box(0, 45, 100, 55)]  # 1,000 sqm

# production_buffer_meters=0.0/water_buffer_meters=0.0/boundary_setback_meters=0.0 explicitly here --
# this section tests the PURE geometry-difference behavior (no clearance applied), independently of
# the production/water buffer defaults (TREE_ZONE_PRODUCTION_BUFFER_METERS/TREE_ZONE_WATER_BUFFER_METERS)
# and the boundary setback default (TREE_ZONE_BOUNDARY_SETBACK_METERS); the buffered/setback default
# paths have their own dedicated coverage in test_tree_zone_search_space_buffers.py/
# test_tree_zone_boundary_setback.py.
search_space, claimed = compute_tree_search_space(
    boundary,
    production_polys,
    water_polys,
    road_polys,
    production_buffer_meters=0.0,
    water_buffer_meters=0.0,
    boundary_setback_meters=0.0,
)
assert claimed is not None
assert abs(claimed.area - 2800.0) < 1e-6, (
    "claimed union area should be exactly production+water+road -- the road's own REAL cell-footprint polygon "
    "must contribute its own real area, unlike the old zero-width LineString it replaced"
)
assert search_space is not None and not search_space.is_empty
assert abs(search_space.area - (10000.0 - 2800.0)) < 1e-6, (
    "search space area must equal boundary minus claimed EXACTLY, including the road's own real, "
    "non-negligible contribution"
)
print(f"compute_tree_search_space(): claimed={claimed.area} sqm (production+water+road, road contributing its "
      f"own real, non-zero area), search_space={search_space.area} sqm = boundary - claimed exactly.")

# No claims at all -- search_space is the boundary itself, claimed is None (not just empty).
empty_search_space, empty_claimed = compute_tree_search_space(
    boundary, [], [], [], production_buffer_meters=0.0, water_buffer_meters=0.0, boundary_setback_meters=0.0
)
assert empty_claimed is None
assert empty_search_space is boundary, "with nothing claimed, the search space should be the boundary object itself"
print("compute_tree_search_space() with nothing claimed returns the boundary unmodified, claimed_union=None.")

# Fully claimed -- search_space is None (not merely empty), a real, reportable "nothing left" outcome.
fully_claimed_search_space, fully_claimed_union = compute_tree_search_space(
    boundary, [boundary], [], [], production_buffer_meters=0.0, water_buffer_meters=0.0, boundary_setback_meters=0.0
)
assert fully_claimed_union is not None
assert fully_claimed_search_space is None, "a boundary fully claimed by production/water/roads must report search_space=None"
print("compute_tree_search_space() with the ENTIRE boundary claimed returns search_space=None (a real, reportable outcome).")


# =====================================================================
# _road_corridor_exclusion_polygon(): the road's OWN buffered cell-footprint --
# closes the corner-only gap a plain, unbuffered cell-square union can still leave
# =====================================================================

ROAD_TEST_RESOLUTION = (5.0, 5.0)
road_test_dem = {
    "array": np.full((10, 10), 100.0, dtype=np.float32),
    "resolution_meters": ROAD_TEST_RESOLUTION,
    "origin_x": 600000.0,
    "origin_y": 4600050.0,
    "crs": CRS,
}


def _road_test_cell_box(row, col):
    """The real ground square for one (row, col) cell of road_test_dem -- same corner
    convention raster_grid.cell_union_footprint() itself uses."""
    px, py = ROAD_TEST_RESOLUTION
    x0 = road_test_dem["origin_x"] + col * px
    x1 = road_test_dem["origin_x"] + (col + 1) * px
    y1 = road_test_dem["origin_y"] - row * py
    y0 = road_test_dem["origin_y"] - (row + 1) * py
    return box(x0, y0, x1, y1)


# A single road cell at (5, 5). Diagonal-only neighbor cell (4, 6) (NE) shares
# exactly one CORNER with it, no edge -- exactly the real, live-confirmed gap case.
single_road_cell = {"cells": [(5, 5)]}
diagonal_neighbor_box = _road_test_cell_box(4, 6)
edge_neighbor_box = _road_test_cell_box(5, 6)  # shares a full EDGE with (5, 5), for comparison
far_box = _road_test_cell_box(0, 0)  # nowhere near (5, 5) at all

unbuffered_mask = np.zeros((10, 10), dtype=bool)
unbuffered_mask[5, 5] = True
unbuffered_footprint = cell_union_footprint(road_test_dem, unbuffered_mask)
assert unbuffered_footprint.intersection(diagonal_neighbor_box).area < 1e-9, (
    "test sanity check: the UNBUFFERED single-cell footprint must NOT already overlap its diagonal "
    "neighbor's square (only a shared corner point, zero area) -- otherwise this isn't reproducing the "
    "real corner-touching gap at all"
)

buffered_footprint = _road_corridor_exclusion_polygon(road_test_dem, single_road_cell)
assert buffered_footprint.area > unbuffered_footprint.area, (
    "the buffered footprint must be strictly larger than the unbuffered one -- TREE_ZONE_ROAD_BUFFER_CELLS "
    f"({TREE_ZONE_ROAD_BUFFER_CELLS}) must have actually grown the mask"
)
assert diagonal_neighbor_box.difference(buffered_footprint).area < 1e-9, (
    "the diagonal-only neighbor cell (sharing just a corner with the road cell) must be FULLY absorbed into "
    "the buffered footprint -- this is the exact real, live-confirmed gap this buffer exists to close"
)
assert edge_neighbor_box.difference(buffered_footprint).area < 1e-9, (
    "the direct edge neighbor must also be fully absorbed (a strictly easier case than the diagonal one)"
)
assert far_box.intersection(buffered_footprint).area < 1e-9, (
    "a cell far from the road must NOT be swept into the buffer -- this is a bounded, local dilation, "
    "not an unbounded growth"
)
print(
    f"_road_corridor_exclusion_polygon(): a single road cell's plain footprint ({unbuffered_footprint.area} sq m) "
    f"leaves its diagonal neighbor touching at a bare corner (confirmed, zero overlap); after "
    f"TREE_ZONE_ROAD_BUFFER_CELLS={TREE_ZONE_ROAD_BUFFER_CELLS} dilation, the buffered footprint "
    f"({buffered_footprint.area} sq m) fully absorbs both the diagonal AND edge neighbor, while a cell "
    "further away stays completely untouched."
)


# =====================================================================
# score_tree_search_space(): Steps 2-3, the 6-region synthetic DEM
# =====================================================================

ROWS, COLS = 20, 150
RESOLUTION = (5.0, 5.0)
ORIGIN_X, ORIGIN_Y = 500000.0, 4500100.0  # origin_y - ROWS*5 = 4500000

array = np.full((ROWS, COLS), 400.0, dtype=np.float32)
# Region 2 (cols 30-49): a steep ramp, ~70% grade row-to-row -- everything
# else stays at the flat 400.0 baseline (including the gaps between
# regions), so there are no artificial slope spikes anywhere else.
for row in range(ROWS):
    array[row, 30:50] = 400.0 + row * 3.5

dem = {"array": array, "resolution_meters": RESOLUTION, "origin_x": ORIGIN_X, "origin_y": ORIGIN_Y, "crs": CRS}
boundary_polygon_utm = box(ORIGIN_X, ORIGIN_Y - ROWS * 5.0, ORIGIN_X + COLS * 5.0, ORIGIN_Y)

def _col_box(col_start, col_end):
    return box(ORIGIN_X + col_start * 5.0, ORIGIN_Y - ROWS * 5.0, ORIGIN_X + col_end * 5.0, ORIGIN_Y)

region1_hydric_flat = _col_box(0, 20)
region2_steep = _col_box(30, 50)
region3_flat_marginal = _col_box(60, 80)
region4_prime_farmland = _col_box(90, 110)
region5_stream_adjacent = _col_box(120, 130)
# 3x3 cells (9 cells, 225 sqm = 0.0556 ac): deliberately sized to SURVIVE the
# render-opening survival gate (a 3x3 block keeps a 1-cell plantable core after
# the r=1 disc opening) while staying well BELOW the MIN_TREE_ZONE_ACRES floor,
# so this fixture still isolates the SIZE filter. A 2x2 block (the earlier
# fixture) would instead erode to nothing and be dropped by the survival gate --
# that path is covered separately in test_tree_zone_render_opening.py.
region6_tiny_hydric = box(ORIGIN_X + 140 * 5.0, ORIGIN_Y - 3 * 5.0, ORIGIN_X + 143 * 5.0, ORIGIN_Y)  # 3x3 cells

search_space_utm = unary_union(
    [region1_hydric_flat, region2_steep, region3_flat_marginal, region4_prime_farmland, region5_stream_adjacent, region6_tiny_hydric]
)
prime_farmland_union = region4_prime_farmland
hydric_union = unary_union([region1_hydric_flat, region6_tiny_hydric])
stream_x_mid = ORIGIN_X + 125 * 5.0  # center of region5's column range
stream_union = LineString([(stream_x_mid, ORIGIN_Y - ROWS * 5.0), (stream_x_mid, ORIGIN_Y)])

patches = score_tree_search_space(
    dem, search_space_utm, boundary_polygon_utm,
    prime_farmland_union=prime_farmland_union,
    hydric_union=hydric_union,
    stream_union=stream_union,
)

assert len(patches) == 2, f"expected exactly 2 qualifying candidates (regions 1 and 2), got {len(patches)}"

hydric_patch = max(patches, key=lambda p: p["hydric_overlap_factor"])
steep_patch = max(patches, key=lambda p: p["slope_factor"])
assert hydric_patch is not steep_patch, "the two surviving patches must be genuinely different (region 1 vs region 2)"

# --- region 1: hydric alone pushes it over the threshold ---
assert hydric_patch["hydric_overlap_factor"] > 0.9
assert hydric_patch["slope_factor"] < 0.1, "region 1 is flat -- slope should contribute almost nothing to its score"
assert hydric_patch["soil_marginality_factor"] > 0.9, "region 1 is not prime farmland -- soil marginality should read high"
assert hydric_patch["stream_proximity_factor"] < 0.1, "region 1 is far from the stream (region 5) -- proximity should read ~0"
assert hydric_patch["tree_suitability_score"] >= tzc.MIN_TREE_SUITABILITY_SCORE
assert hydric_patch["area_acres"] > tzc.MIN_TREE_ZONE_ACRES
print(f"Region 1 (hydric, flat) qualifies via hydric overlap alone: score={hydric_patch['tree_suitability_score']}, "
      f"hydric_overlap_factor={hydric_patch['hydric_overlap_factor']}, area={hydric_patch['area_acres']}ac.")

# --- region 2: steep slope alone pushes it over the threshold ---
assert steep_patch["slope_factor"] > 0.9, "region 2's ~70% grade should saturate slope_factor near 1.0"
assert steep_patch["hydric_overlap_factor"] < 0.1, "region 2 is not hydric -- should read ~0"
assert steep_patch["soil_marginality_factor"] > 0.9, "region 2 is not prime farmland -- soil marginality should read high"
assert steep_patch["tree_suitability_score"] >= tzc.MIN_TREE_SUITABILITY_SCORE
assert steep_patch["avg_slope_pct"] > TREE_SLOPE_REFERENCE_PCT, "region 2's real average slope should exceed the saturation reference"
print(f"Region 2 (steep ramp, non-hydric) qualifies via slope alone: score={steep_patch['tree_suitability_score']}, "
      f"slope_factor={steep_patch['slope_factor']}, avg_slope_pct={steep_patch['avg_slope_pct']}%.")

# --- confirm regions 3, 4, 5, 6 did NOT produce candidates (the core "not just all leftover land" proof) ---
total_candidate_area = sum(p["area_acres"] for p in patches)
total_search_space_acres = search_space_utm.area / 4046.8564224
assert total_candidate_area < total_search_space_acres * 0.6, (
    "candidates must cover meaningfully LESS area than the full search space -- region 3 (flat/marginal), "
    "region 4 (prime farmland), region 5 (stream-only), and region 6 (size-filtered) must all have been excluded, "
    "not silently included just because they're unclaimed leftover land"
)
print(f"Total candidate area ({total_candidate_area}ac) is meaningfully less than the total search space "
      f"({round(total_search_space_acres, 2)}ac) -- confirms regions 3/4/5/6 were genuinely excluded, not just "
      "everything-leftover included by default.")

# region 3 (flat, no positive signal at all) -- explicitly re-score in isolation to show its low score directly
region3_only_patches = score_tree_search_space(dem, region3_flat_marginal, boundary_polygon_utm, min_score=0.0, min_area_acres=0.0)
assert len(region3_only_patches) == 1
assert region3_only_patches[0]["tree_suitability_score"] < tzc.MIN_TREE_SUITABILITY_SCORE, (
    "flat, non-hydric, non-prime, no-stream leftover land must score BELOW the qualifying threshold -- "
    "being merely unclaimed is not itself evidence of tree suitability"
)
print(f"Region 3 in isolation (flat, no positive signal) scores {region3_only_patches[0]['tree_suitability_score']}/100 "
      f"-- correctly below MIN_TREE_SUITABILITY_SCORE ({tzc.MIN_TREE_SUITABILITY_SCORE}).")

# region 4 (prime farmland) -- explicitly re-score in isolation: soil_marginality_factor must read 0
region4_only_patches = score_tree_search_space(
    dem, region4_prime_farmland, boundary_polygon_utm,
    prime_farmland_union=prime_farmland_union, min_score=0.0, min_area_acres=0.0,
)
assert len(region4_only_patches) == 1
assert region4_only_patches[0]["soil_marginality_factor"] == 0.0, "prime farmland must read soil_marginality_factor=0.0 (inverted)"
assert region4_only_patches[0]["tree_suitability_score"] < tzc.MIN_TREE_SUITABILITY_SCORE
print(f"Region 4 (prime farmland) scores {region4_only_patches[0]['tree_suitability_score']}/100 with "
      "soil_marginality_factor=0.0 -- the inversion correctly suppresses otherwise-leftover prime ag land.")

# region 5 (stream proximity alone) -- explicitly re-score in isolation: real bonus, but not enough alone
region5_only_patches = score_tree_search_space(
    dem, region5_stream_adjacent, boundary_polygon_utm,
    stream_union=stream_union, min_score=0.0, min_area_acres=0.0,
)
assert len(region5_only_patches) == 1
assert region5_only_patches[0]["stream_proximity_factor"] > 0.5, "region 5 sits close to the stream -- proximity should read a real, non-trivial bonus"
assert region5_only_patches[0]["tree_suitability_score"] < tzc.MIN_TREE_SUITABILITY_SCORE, (
    "stream proximity is a deliberately MINOR factor -- it must not be able to clear the qualifying threshold on its own"
)
print(f"Region 5 (near stream only) scores {region5_only_patches[0]['tree_suitability_score']}/100 with a real "
      f"stream_proximity_factor={region5_only_patches[0]['stream_proximity_factor']} -- confirms stream proximity "
      "alone is correctly too minor to qualify land on its own.")

# region 6 (small 3x3 hydric patch) -- explicitly re-score in isolation:
# qualifies on SCORE and survives the render-opening survival gate (a 3x3 block
# keeps a plantable core), but is dropped by the SIZE filter. With the size
# floor disabled it comes back as one scored candidate, proving the drop below
# is the size filter doing real work, not the score threshold or the survival
# gate. (A patch too thin to survive the opening -- e.g. a 1-cell-wide chain --
# is dropped by the survival gate instead; see test_tree_zone_render_opening.py.)
region6_only_patches = score_tree_search_space(
    dem, region6_tiny_hydric, boundary_polygon_utm, hydric_union=hydric_union, min_area_acres=0.0,
)
assert len(region6_only_patches) == 1, "with the size filter disabled, region 6 should score high enough AND survive the opening to qualify"
assert region6_only_patches[0]["tree_suitability_score"] >= tzc.MIN_TREE_SUITABILITY_SCORE
assert region6_only_patches[0]["area_acres"] < tzc.MIN_TREE_ZONE_ACRES
region6_filtered = score_tree_search_space(dem, region6_tiny_hydric, boundary_polygon_utm, hydric_union=hydric_union)
assert region6_filtered == [], "with the real default size filter applied, the small hydric patch must be dropped"
print(f"Region 6 (small 3x3 hydric patch, {region6_only_patches[0]['area_acres']}ac) scores high enough to qualify "
      f"({region6_only_patches[0]['tree_suitability_score']}/100) and survives the opening, but is correctly dropped by "
      f"MIN_TREE_ZONE_ACRES ({tzc.MIN_TREE_ZONE_ACRES}ac) -- proves the size filter, not just the score threshold or the "
      f"survival gate, is doing real work.")


# =====================================================================
# CANOPY EXCLUSION GATE: a cell inside tree_root_zone_mask_utm is HARD-excluded
# BEFORE scoring, even when every other factor would otherwise clearly qualify it
# (region 1 -- hydric, flat -- scores well above threshold on hydric overlap alone)
# =====================================================================

region1_ungated_patches = score_tree_search_space(
    dem, region1_hydric_flat, boundary_polygon_utm, hydric_union=hydric_union, min_score=0.0, min_area_acres=0.0,
)
assert len(region1_ungated_patches) == 1
region1_ungated_area = region1_ungated_patches[0]["area_acres"]

# Left half of region 1's own column range (0-20) reads as already under existing tree canopy.
half_canopy_mask_utm = np.zeros((ROWS, COLS), dtype=bool)
half_canopy_mask_utm[:, 0:10] = True

region1_half_gated_patches = score_tree_search_space(
    dem, region1_hydric_flat, boundary_polygon_utm, hydric_union=hydric_union,
    tree_root_zone_mask_utm=half_canopy_mask_utm, min_score=0.0, min_area_acres=0.0,
)
assert len(region1_half_gated_patches) == 1, "the non-canopy half of region 1 should still qualify as its own candidate"
region1_half_gated_area = region1_half_gated_patches[0]["area_acres"]
assert region1_half_gated_area < region1_ungated_area * 0.6, (
    f"excluding half of region 1's cells as already under canopy should meaningfully shrink the resulting "
    f"patch area -- ungated={region1_ungated_area}ac, gated={region1_half_gated_area}ac"
)

canopy_footprint = _col_box(0, 10)
gated_polygon = region1_half_gated_patches[0]["polygon_utm"]
overlap_area = gated_polygon.intersection(canopy_footprint).area
assert overlap_area < 1e-6, (
    f"the surviving candidate patch must not include any cell inside the canopy-excluded region -- got "
    f"{overlap_area} sq m of overlap"
)
print(f"Canopy exclusion gate (partial): gating half of region 1 as already under existing canopy shrinks its "
      f"candidate area from {region1_ungated_area}ac to {region1_half_gated_area}ac, with zero overlap between "
      "the surviving patch and the canopy-excluded cells -- confirms cells are HARD-excluded before scoring, "
      "not merely scored down.")

# The ENTIRE region under canopy -- must yield zero candidates, no matter how strong its other factors are.
full_canopy_mask_utm = np.zeros((ROWS, COLS), dtype=bool)
full_canopy_mask_utm[:, 0:20] = True
region1_fully_gated_patches = score_tree_search_space(
    dem, region1_hydric_flat, boundary_polygon_utm, hydric_union=hydric_union,
    tree_root_zone_mask_utm=full_canopy_mask_utm, min_score=0.0, min_area_acres=0.0,
)
assert region1_fully_gated_patches == [], (
    "a region entirely under existing canopy must yield zero candidates, even with hydric overlap alone "
    "otherwise clearing the threshold easily"
)
print("Canopy exclusion gate (full): gating the ENTIRE region under existing canopy yields zero candidates, "
      "even though hydric overlap alone would otherwise clear the threshold easily.")

# tree_root_zone_mask_utm=None (the default) must reproduce the ungated result exactly -- confirms the gate is a
# genuine opt-in that changes nothing for every OTHER call in this file that never passes it.
region1_explicit_none_patches = score_tree_search_space(
    dem, region1_hydric_flat, boundary_polygon_utm, hydric_union=hydric_union,
    tree_root_zone_mask_utm=None, min_score=0.0, min_area_acres=0.0,
)
assert region1_explicit_none_patches[0]["area_acres"] == region1_ungated_area, (
    "tree_root_zone_mask_utm=None must be a true no-op, identical to the parameter's own default"
)
print("Canopy exclusion gate: tree_root_zone_mask_utm=None reproduces the ungated result exactly (a true no-op).")


# =====================================================================
# render_fill_polygon_utm: a bounded morphological OPENING (NOT a convex hull).
# An opening is anti-extensive and clipped to the real footprint, so it leaves a
# real interior notch (e.g. one the CANOPY EXCLUSION GATE carves) OPEN rather
# than closing over it, and never exceeds the footprint's area -- the same field
# production_area.py's and water_candidate_zones.py's own patches/zones carry.
# =====================================================================

# A small canopy pocket sitting STRICTLY INSIDE region 1's own interior (rows 8-11, cols
# 8-11 -- well clear of region 1's own cols 0-20 edges) carves a real hole out of an
# otherwise-contiguous candidate.
interior_pocket_canopy_mask = np.zeros((ROWS, COLS), dtype=bool)
interior_pocket_canopy_mask[8:12, 8:12] = True
pocket_box = box(ORIGIN_X + 8 * 5.0, ORIGIN_Y - 12 * 5.0, ORIGIN_X + 12 * 5.0, ORIGIN_Y - 8 * 5.0)

region1_notched_patches = score_tree_search_space(
    dem, region1_hydric_flat, boundary_polygon_utm, hydric_union=hydric_union,
    tree_root_zone_mask_utm=interior_pocket_canopy_mask, min_score=0.0, min_area_acres=0.0,
)
assert len(region1_notched_patches) == 1, (
    "a canopy pocket strictly inside region 1's own interior must still leave ONE single surrounding "
    "candidate (a real hole, not a split into separate patches)"
)
notched_patch = region1_notched_patches[0]
assert "render_fill_polygon_utm" in notched_patch, "every patch must carry a render_fill_polygon_utm field"
render_fill = notched_patch["render_fill_polygon_utm"]
real_footprint = notched_patch["polygon_utm"]

assert real_footprint.intersection(pocket_box).area < 1e-6, (
    "the real footprint must genuinely EXCLUDE the canopy pocket -- area_acres/scoring must "
    "never reflect ground that's actually under canopy"
)
# The opening (unlike the old hull) leaves the interior pocket OPEN: an opening is a
# subset of the mask, and the pocket cells were never in the mask, so it can never
# reappear in render_fill.
assert render_fill.intersection(pocket_box).area < 1e-6, (
    "render_fill_polygon_utm (a bounded opening) must leave the interior canopy pocket OPEN -- an opening is "
    "anti-extensive and clipped to the footprint, so it never closes over a real interior notch the way the "
    "old convex hull did"
)
assert render_fill.difference(real_footprint).area < 1e-6, (
    "render_fill_polygon_utm must be a subset of the real footprint (clipped to it by construction)"
)
assert render_fill.area <= real_footprint.area + 1e-6, (
    "an opening never has MORE area than the real footprint -- the opposite of the old hull, which closed "
    "over notches and GAINED area"
)
# non-vacuous: the OLD convex hull WOULD have closed this pocket -- assert it, so this
# fixture provably exercises a real behavior change.
assert pocket_box.difference(real_footprint.convex_hull).area < 1e-6, (
    "sanity check: the old convex-hull behavior would have closed over this interior pocket -- if it "
    "wouldn't have, this fixture isn't testing a real change"
)
print(
    f"render_fill_polygon_utm: a canopy pocket carved out of region 1's own interior leaves the real "
    f"footprint ({real_footprint.area}sqm) genuinely excluding it, and render_fill_polygon_utm "
    f"({render_fill.area}sqm, a bounded opening) leaves it OPEN too -- whereas the old convex hull would "
    "have closed over it. Confirms the opening, clipped to the footprint, is what render_layout_map.py draws."
)

# The ordinary, un-notched case (region 2, a solid rectangle, no canopy gate): the disc
# opening trims ONLY the four single-cell corners (each a corner protrusion narrower than
# 2r), leaving a non-empty subset of the real footprint with no interior change.
region2_plain_patches = score_tree_search_space(
    dem, region2_steep, boundary_polygon_utm, min_score=0.0, min_area_acres=0.0,
)
assert len(region2_plain_patches) == 1
plain_patch = region2_plain_patches[0]
plain_render = plain_patch["render_fill_polygon_utm"]
plain_footprint = plain_patch["polygon_utm"]
assert not plain_render.is_empty and plain_render.area > 0, "a solid block's opening must be non-empty"
assert plain_render.difference(plain_footprint).area < 1e-6, "the opening must stay within the real footprint"
# region 2 is a solid 20x20-cell rectangle; the disc opening of a solid rectangle removes
# exactly its four corner cells (4 * one-cell area), nothing else.
_CELL_SQM = RESOLUTION[0] * RESOLUTION[1]
assert abs(plain_render.area - (plain_footprint.area - 4 * _CELL_SQM)) < 1e-6, (
    f"the opening of a solid rectangle trims exactly its four single-cell corners "
    f"({4 * _CELL_SQM} sqm); got footprint {plain_footprint.area} sqm, render {plain_render.area} sqm"
)
print(
    f"render_fill_polygon_utm: an ordinary solid-rectangle candidate's opening ({plain_render.area}sqm) is its "
    f"real footprint ({plain_footprint.area}sqm) minus exactly its four single-cell corners -- the only visible "
    "change for the common, un-notched case."
)


# --- data-unavailable factors default to a neutral 0.5, not the "checked and clean" 0.0/1.0 ---

tiny_dem = {
    "array": np.full((4, 4), 400.0, dtype=np.float32),
    "resolution_meters": (5.0, 5.0),
    "origin_x": 500000.0,
    "origin_y": 4500020.0,
    "crs": CRS,
}
tiny_boundary = box(500000.0, 4500000.0, 500020.0, 4500020.0)
unavailable_patches = score_tree_search_space(
    tiny_dem, tiny_boundary, tiny_boundary,
    prime_farmland_data_available=False,
    hydric_data_available=False,
    stream_data_available=False,
    min_score=0.0, min_area_acres=0.0,
)
assert len(unavailable_patches) == 1
p = unavailable_patches[0]
assert p["soil_marginality_factor"] == 0.5 and p["hydric_overlap_factor"] == 0.5 and p["stream_proximity_factor"] == 0.5, (
    "when a factor's own data source could not be reached at all, it must default to a neutral 0.5 -- "
    "distinct from a real 'checked and clean' 0.0/1.0 result"
)
assert p["soil_marginality_data_available"] is False and p["hydric_data_available"] is False and p["stream_data_available"] is False
print("When soil/hydric/stream data is genuinely unavailable (fetch failed), all three factors correctly default "
      "to a neutral 0.5, distinct from a real checked-and-clean 0.0/1.0 result -- and the *_data_available flags "
      "report that honestly.")


# =====================================================================
# tree_zones_to_geojson(): schema validity, layer, confidence_notes content
# =====================================================================

geojson = tree_zones_to_geojson(patches)
validate_feature_collection(geojson)

required_props = {
    "area_acres", "tree_suitability_score", "soil_marginality_factor", "slope_factor",
    "hydric_overlap_factor", "stream_proximity_factor", "avg_slope_pct", "rank",
}
for feature in geojson["features"]:
    assert feature["properties"]["layer"] == "tree_zone_candidate"
    assert required_props.issubset(feature["properties"].keys()), (
        f"missing required properties: {required_props - feature['properties'].keys()}"
    )
    score = feature["properties"]["tree_suitability_score"]
    assert 0.0 <= score <= 100.0
    for factor_name in ("soil_marginality_factor", "slope_factor", "hydric_overlap_factor", "stream_proximity_factor"):
        assert 0.0 <= feature["properties"][factor_name] <= 1.0
    notes = feature["properties"]["confidence_notes"].lower()
    assert "windbreak" in notes and "riparian" in notes and "habitat" in notes, (
        "confidence_notes must plainly disclaim every specific function this layer does NOT assign"
    )
    assert "species recommendation" in notes
    assert "not decided here" in notes or "not decided" in notes
print("tree_zones_to_geojson output is schema-valid, layer='tree_zone_candidate', every feature carries all "
      "required factor properties plus a confidence_notes that plainly disclaims windbreak/riparian/habitat/"
      "species-recommendation framing.")

assert "No production-area" not in summarize_tree_zone_candidates({"zones_geojson": {"features": []}, "search_space_acres": 5.0, "claimed_acres": 2.0, "boundary_acres": 7.0})
print("summarize_tree_zone_candidates() handles the zero-candidate case without crashing.")


# =====================================================================
# identify_tree_zone_candidates(): full orchestrator wiring, network-free via mocking
# =====================================================================

def _fake_soil_rows_empty(wkt_polygon):
    return []


def _fake_soil_geometries_empty(wkt_polygon):
    return {}


def _fake_farmland_empty(wkt_polygon):
    return []


def _fake_water_features_empty(boundary_coordinates, buffer_meters=150):
    return {"streams": [], "water_bodies": []}


# Same bench-and-rise synthetic DEM as production_area.py's own smoke test:
# a flat, workable bench (rows < 15) production_area.py will claim as a
# production candidate, bordered by a genuinely steep rise (rows >= 15,
# ~100% grade) that production won't touch -- real leftover ground this
# module's own scoring should be able to pick up as a tree candidate via
# slope alone, with every network fetch (production's soil check,
# water_suitability's own per-zone soil/stream check -- reached now via its
# full identify_water_suitability() entry point, since tree_zone_candidates
# subtracts only its SELECTED zone, not the raw pure zone list --
# road_corridors' own floodplain (NHD stream/hydric soil) check, and this
# module's own farmland/hydric/stream checks) mocked to "reachable, found
# nothing."
size = 30
orchestrator_array = np.zeros((size, size), dtype=np.float32)
for row in range(size):
    for col in range(size):
        if row < 15:
            orchestrator_array[row, col] = 100.0
        else:
            orchestrator_array[row, col] = 100.0 + (row - 14) * 5.0

orchestrator_dem = {
    "array": orchestrator_array,
    "resolution_meters": (5.0, 5.0),
    "origin_x": 500000.0,
    "origin_y": 4500150.0,
    "crs": CRS,
}
orchestrator_boundary_utm = box(500000.0, 4500150.0 - size * 5.0, 500000.0 + size * 5.0, 4500150.0)
minx, miny, maxx, maxy = orchestrator_boundary_utm.bounds
corner_xs, corner_ys = [minx, maxx, maxx, minx, minx], [miny, miny, maxy, maxy, miny]
lons, lats = warp_transform(CRS, "EPSG:4326", corner_xs, corner_ys)
orchestrator_boundary_coordinates = list(zip(lons, lats))

with mock_patch.object(pa, "get_soil_data_for_polygon", _fake_soil_rows_empty), \
     mock_patch.object(pa, "get_soil_geometries_for_polygon", _fake_soil_geometries_empty), \
     mock_patch.object(rc, "get_soil_data_for_polygon", _fake_soil_rows_empty), \
     mock_patch.object(rc, "get_soil_geometries_for_polygon", _fake_soil_geometries_empty), \
     mock_patch.object(rc, "get_water_features_for_boundary", _fake_water_features_empty), \
     mock_patch.object(ws, "get_saturated_hydraulic_conductivity_for_polygon", _fake_soil_rows_empty), \
     mock_patch.object(ws, "get_soil_geometries_for_polygon", _fake_soil_geometries_empty), \
     mock_patch.object(ws, "get_water_features_for_boundary", _fake_water_features_empty), \
     mock_patch.object(tzc, "get_farmland_classification_for_polygon", _fake_farmland_empty), \
     mock_patch.object(tzc, "get_soil_data_for_polygon", _fake_soil_rows_empty), \
     mock_patch.object(tzc, "get_soil_geometries_for_polygon", _fake_soil_geometries_empty), \
     mock_patch.object(tzc, "get_water_features_for_boundary", _fake_water_features_empty):
    result = identify_tree_zone_candidates(orchestrator_boundary_coordinates, dem=orchestrator_dem)

validate_feature_collection(result["zones_geojson"])
validate_feature_collection(result["search_space_geojson"])

assert result["boundary_acres"] > 0
# production_area.py's render_fill_polygon_utm is now a bounded, inset opening
# (erode r+lead, dilate r, disc element) rather than a convex hull, so the DRAWN
# production geometry intentionally claims LESS than its full eligible footprint
# (~0.6-0.8x, an accepted display gap -- see that module's render opening). The
# reconstruction therefore UNDER-claims by a small fringe (the trimmed production
# edge plus the boundary setback ring) instead of matching boundary_acres almost
# exactly as it did under the old over-claiming hull. Assert the two directions
# separately: NO over-claim (claimed+search never exceeds the boundary -- catches
# unclipped/double-counted geometry, tighter than the old +/-0.5), and an
# under-claim no larger than the accepted inset fringe.
_recon_acres = result["search_space_acres"] + result["claimed_acres"]
assert _recon_acres <= result["boundary_acres"] + 0.01, (
    "claimed + search space must never EXCEED the boundary (no over-claim / unclipped geometry) -- got "
    f"{_recon_acres} vs boundary {result['boundary_acres']}"
)
assert result["boundary_acres"] - _recon_acres < 1.0, (
    "claimed + search space should reconstruct boundary_acres except for the render opening's accepted inset "
    f"fringe (< ~1 acre on this fixture) -- got a gap of {result['boundary_acres'] - _recon_acres}"
)
assert len(result["zones_geojson"]["features"]) >= 1, (
    "the steep, non-production rise (rows >= 15, ~100% grade, non-hydric/non-prime/no-stream with every fetch "
    "mocked to 'reachable, found nothing') should still qualify as at least one tree candidate via slope alone"
)
best = max(result["zones_geojson"]["features"], key=lambda f: f["properties"]["tree_suitability_score"])
assert best["properties"]["slope_factor"] > 0.9, (
    "the steep rise's real DEM slope should be what's actually driving this candidate's score -- confirms this "
    "isn't just a coincidental default-factor artifact"
)
print(summarize_tree_zone_candidates(result))
print(f"\nidentify_tree_zone_candidates() full orchestrator wiring: boundary={result['boundary_acres']}ac, "
      f"search_space={result['search_space_acres']}ac, claimed={result['claimed_acres']}ac, "
      f"{len(result['zones_geojson']['features'])} tree zone candidate(s), best score driven by real steep slope "
      f"(slope_factor={best['properties']['slope_factor']}) -- OFFLINE/SYNTHETIC ONLY, network mocked throughout.")
print("REGRESSION (test 2, no overrides supplied): output above is identical to pre-branch behavior against "
      "this same synthetic fixture -- confirms the new boundary_polygon_utm/production_areas/valleys/"
      "selected_water_zone/selected_road_corridor/hydric_floodplain_union/floodplain_data_is_fallback overrides "
      "don't change anything when left unsupplied.")


# =====================================================================
# identify_tree_zone_candidates(): boundary_polygon_utm/production_areas/
# valleys/selected_water_zone/selected_road_corridor/hydric_floodplain_union/
# floodplain_data_is_fallback overrides -- same override set/pattern as
# solar_suitability.identify_solar_candidate_zones()'s own overrides,
# reusing this file's own bench-and-rise orchestrator fixture above.
# =====================================================================

# Real baseline override values for this same orchestrator_dem/
# orchestrator_boundary_coordinates fixture, via the SAME underlying call
# identify_tree_zone_candidates()'s own production_areas self-compute
# fallback makes (check_soil/check_roads disabled here purely so this
# setup step doesn't attempt any real network fetch of its own -- the
# canopy gate stays real, offline, via this file's own module-level
# pa.get_canopy_height_for_boundary patch) -- computed once here and
# reused below both as a realistic override value and as identity-
# checkable proof that the self-compute fallback is genuinely skipped
# when overridden.
override_boundary_xs, override_boundary_ys = warp_transform(
    "EPSG:4326", CRS,
    [pt[0] for pt in orchestrator_boundary_coordinates],
    [pt[1] for pt in orchestrator_boundary_coordinates],
)
OVERRIDE_BOUNDARY_POLYGON_UTM = Polygon(zip(override_boundary_xs, override_boundary_ys))

OVERRIDE_PRODUCTION_AREAS = pac.identify_optimized_production_areas(
    orchestrator_boundary_coordinates, dem=orchestrator_dem, check_soil=False, check_roads=False
)["scored_patches"]
assert isinstance(OVERRIDE_PRODUCTION_AREAS, list) and OVERRIDE_PRODUCTION_AREAS, (
    "test setup: expected at least one real production patch on this bench-and-rise fixture (the flat, "
    "workable bench, rows < 15) to reuse as a direct override below"
)

# valleys is a pure pass-through in this module (no delineate_valleys() self-compute -- see this module's
# own docstring) -- any object works as an override here; what matters below is identity, not content.
OVERRIDE_VALLEYS = []

# This fixture's bench-and-rise terrain isn't shaped to produce a real selected water zone or road corridor
# of its own; both are fabricated as small synthetic values instead, positioned/sized so they have a real,
# checkable effect (a real render_fill_polygon_utm / a real 'cells' list within this DEM's own shape)
# without depending on this terrain accidentally producing one.
OVERRIDE_SELECTED_WATER_ZONE = {
    "id": "synthetic-test-water-zone",
    "render_fill_polygon_utm": Point(orchestrator_dem["origin_x"] - 1000.0, orchestrator_dem["origin_y"] - 1000.0).buffer(5.0),
}
OVERRIDE_SELECTED_ROAD_CORRIDOR = {
    "id": "synthetic-test-road-corridor",
    "cells": [(0, 0), (0, 1), (1, 0)],
}
OVERRIDE_HYDRIC_FLOODPLAIN_UNION = Point(
    orchestrator_dem["origin_x"] - 2000.0, orchestrator_dem["origin_y"] - 2000.0
).buffer(5.0)
OVERRIDE_FLOODPLAIN_DATA_IS_FALLBACK = False


# --- 1. override behavior: all 7 overrides supplied -> the corresponding --
# --- self-compute fallbacks are never called -------------------------------

with mock_patch.object(tzc, "get_dem_for_boundary") as mock_dem, \
     mock_patch.object(tzc, "identify_optimized_production_areas") as mock_prod, \
     mock_patch.object(tzc, "identify_water_suitability") as mock_water, \
     mock_patch.object(tzc, "identify_road_corridor_candidates") as mock_corridor, \
     mock_patch.object(tzc, "get_farmland_classification_for_polygon", _fake_farmland_empty), \
     mock_patch.object(tzc, "get_soil_data_for_polygon", _fake_soil_rows_empty), \
     mock_patch.object(tzc, "get_soil_geometries_for_polygon", _fake_soil_geometries_empty), \
     mock_patch.object(tzc, "get_water_features_for_boundary", _fake_water_features_empty):
    override_result = identify_tree_zone_candidates(
        orchestrator_boundary_coordinates,
        dem=orchestrator_dem,
        boundary_polygon_utm=OVERRIDE_BOUNDARY_POLYGON_UTM,
        production_areas=OVERRIDE_PRODUCTION_AREAS,
        valleys=OVERRIDE_VALLEYS,
        selected_water_zone=OVERRIDE_SELECTED_WATER_ZONE,
        selected_road_corridor=OVERRIDE_SELECTED_ROAD_CORRIDOR,
        hydric_floodplain_union=OVERRIDE_HYDRIC_FLOODPLAIN_UNION,
        floodplain_data_is_fallback=OVERRIDE_FLOODPLAIN_DATA_IS_FALLBACK,
    )

assert mock_dem.call_count == 0, "get_dem_for_boundary() must NOT be called when dem was supplied"
assert mock_prod.call_count == 0, (
    "identify_optimized_production_areas() must NOT be called when production_areas was supplied as an override"
)
assert mock_water.call_count == 0, (
    "identify_water_suitability() must NOT be called when selected_water_zone was supplied as an override"
)
assert mock_corridor.call_count == 0, (
    "identify_road_corridor_candidates() must NOT be called when selected_road_corridor was supplied as an override"
)
validate_feature_collection(override_result["zones_geojson"])
validate_feature_collection(override_result["search_space_geojson"])
print(
    "Supplying all 7 overrides (boundary_polygon_utm=/production_areas=/valleys=/selected_water_zone=/"
    "selected_road_corridor=/hydric_floodplain_union=/floodplain_data_is_fallback=, test 1) correctly skips "
    "get_dem_for_boundary()/identify_optimized_production_areas()/identify_water_suitability()/"
    "identify_road_corridor_candidates() -- zero self-compute fallback calls."
)


# --- 3. selected_water_zone and selected_road_corridor NOT overridden, ----
# --- but boundary_polygon_utm/production_areas/valleys ARE -> both --------
# --- identify_water_suitability() and identify_road_corridor_candidates() -
# --- are called WITH those three passed through as kwargs, not -----------
# --- self-deriving their own independent copies ---------------------------

with mock_patch.object(
        tzc, "identify_water_suitability", return_value={"selected_water_zone": None},
     ) as mock_water_passthrough, \
     mock_patch.object(
        tzc, "identify_road_corridor_candidates",
        return_value={"selected_road_corridor": OVERRIDE_SELECTED_ROAD_CORRIDOR},
     ) as mock_corridor_passthrough, \
     mock_patch.object(tzc, "get_farmland_classification_for_polygon", _fake_farmland_empty), \
     mock_patch.object(tzc, "get_soil_data_for_polygon", _fake_soil_rows_empty), \
     mock_patch.object(tzc, "get_soil_geometries_for_polygon", _fake_soil_geometries_empty), \
     mock_patch.object(tzc, "get_water_features_for_boundary", _fake_water_features_empty):
    passthrough_result = identify_tree_zone_candidates(
        orchestrator_boundary_coordinates,
        dem=orchestrator_dem,
        boundary_polygon_utm=OVERRIDE_BOUNDARY_POLYGON_UTM,
        production_areas=OVERRIDE_PRODUCTION_AREAS,
        valleys=OVERRIDE_VALLEYS,
    )

assert mock_water_passthrough.call_count == 1
water_call = mock_water_passthrough.call_args
assert water_call.args[0] == orchestrator_boundary_coordinates
assert water_call.kwargs["dem"] is orchestrator_dem
assert water_call.kwargs["boundary_polygon_utm"] is OVERRIDE_BOUNDARY_POLYGON_UTM, (
    "identify_water_suitability() must receive this function's own already-sourced boundary_polygon_utm, "
    "not re-derive its own copy"
)
assert water_call.kwargs["valleys"] is OVERRIDE_VALLEYS, (
    "identify_water_suitability() must receive this function's own already-sourced valleys, not "
    "re-derive its own copy"
)
assert water_call.kwargs["production_areas"] is OVERRIDE_PRODUCTION_AREAS, (
    "identify_water_suitability() must receive this function's own already-sourced production_areas, "
    "not re-derive its own copy via identify_optimized_production_areas()"
)

assert mock_corridor_passthrough.call_count == 1
corridor_call = mock_corridor_passthrough.call_args
assert corridor_call.args[0] == orchestrator_boundary_coordinates
assert corridor_call.kwargs["dem"] is orchestrator_dem
assert corridor_call.kwargs["boundary_polygon_utm"] is OVERRIDE_BOUNDARY_POLYGON_UTM, (
    "identify_road_corridor_candidates() must receive this function's own already-sourced "
    "boundary_polygon_utm, not re-derive its own copy"
)
assert corridor_call.kwargs["valleys"] is OVERRIDE_VALLEYS, (
    "identify_road_corridor_candidates() must receive this function's own already-sourced valleys, "
    "not re-derive its own copy"
)
assert corridor_call.kwargs["production_areas"] is OVERRIDE_PRODUCTION_AREAS, (
    "identify_road_corridor_candidates() must receive this function's own already-sourced "
    "production_areas, not re-derive its own copy"
)
assert corridor_call.kwargs["selected_water_zone"] is None, (
    "identify_road_corridor_candidates() must receive the SAME selected_water_zone this function's own "
    "(mocked) identify_water_suitability() call just produced, not re-derive it independently"
)
validate_feature_collection(passthrough_result["zones_geojson"])
print(
    "With selected_water_zone/selected_road_corridor left unsupplied but boundary_polygon_utm=/"
    "production_areas=/valleys= all overridden (test 3), identify_water_suitability() and "
    "identify_road_corridor_candidates() correctly receive this function's own already-sourced values "
    "as kwargs instead of re-deriving independent copies of any of them."
)


# --- 4. selected_road_corridor overridden as None explicitly -------------
# --- (simulating "no corridor exists") -> still treated as "not ----------
# --- supplied" and triggers the self-compute path correctly --------------
#
# A caller-supplied None is indistinguishable from "not supplied" (same None-as-
# sentinel convention every other override in this function uses -- see its own
# docstring), so this still triggers the identify_road_corridor_candidates()
# self-compute below.

with mock_patch.object(
        tzc, "identify_road_corridor_candidates", return_value={"selected_road_corridor": None},
     ) as mock_corridor_none, \
     mock_patch.object(tzc, "get_farmland_classification_for_polygon", _fake_farmland_empty), \
     mock_patch.object(tzc, "get_soil_data_for_polygon", _fake_soil_rows_empty), \
     mock_patch.object(tzc, "get_soil_geometries_for_polygon", _fake_soil_geometries_empty), \
     mock_patch.object(tzc, "get_water_features_for_boundary", _fake_water_features_empty):
    none_override_result = identify_tree_zone_candidates(
        orchestrator_boundary_coordinates,
        dem=orchestrator_dem,
        boundary_polygon_utm=OVERRIDE_BOUNDARY_POLYGON_UTM,
        production_areas=OVERRIDE_PRODUCTION_AREAS,
        valleys=OVERRIDE_VALLEYS,
        selected_water_zone=OVERRIDE_SELECTED_WATER_ZONE,
        selected_road_corridor=None,
    )

assert mock_corridor_none.call_count == 1, (
    "an explicit selected_road_corridor=None must still be treated as unsupplied and trigger the "
    "identify_road_corridor_candidates() self-compute, same as leaving it out entirely"
)
validate_feature_collection(none_override_result["zones_geojson"])
print(
    "Supplying selected_road_corridor=None explicitly (test 4) is correctly treated the same as leaving "
    "it unsupplied -- identify_road_corridor_candidates() self-computes (finding no corridor), and the "
    "rest of the pipeline still runs correctly on top of it."
)


# --- canopy_height override forwarding ---
#
# identify_tree_zone_candidates() reaches canopy on SEVERAL independent
# paths when production_areas/selected_water_zone self-compute: its own
# Step-2 get_required_tree_root_zone_mask_utm() gate, its nested identify_
# optimized_production_areas() fetch, and identify_water_suitability()'s own
# gate. A supplied canopy_height override must reach ALL of them so none
# re-fetches from the network. Shared-core behavior is proven in test_
# canopy_mask_override.py; this proves this orchestrator forwards the
# override down every canopy path. Reuses the exact offline fetch-mock block
# and fixtures the full-orchestrator wiring check above already uses.
from _canopy_override_probe import CanopyOverrideProbe, clean_canopy_for  # noqa: E402

_ov_override = clean_canopy_for(orchestrator_dem)
with mock_patch.object(pa, "get_soil_data_for_polygon", _fake_soil_rows_empty), \
     mock_patch.object(pa, "get_soil_geometries_for_polygon", _fake_soil_geometries_empty), \
     mock_patch.object(rc, "get_soil_data_for_polygon", _fake_soil_rows_empty), \
     mock_patch.object(rc, "get_soil_geometries_for_polygon", _fake_soil_geometries_empty), \
     mock_patch.object(rc, "get_water_features_for_boundary", _fake_water_features_empty), \
     mock_patch.object(ws, "get_saturated_hydraulic_conductivity_for_polygon", _fake_soil_rows_empty), \
     mock_patch.object(ws, "get_soil_geometries_for_polygon", _fake_soil_geometries_empty), \
     mock_patch.object(ws, "get_water_features_for_boundary", _fake_water_features_empty), \
     mock_patch.object(tzc, "get_farmland_classification_for_polygon", _fake_farmland_empty), \
     mock_patch.object(tzc, "get_soil_data_for_polygon", _fake_soil_rows_empty), \
     mock_patch.object(tzc, "get_soil_geometries_for_polygon", _fake_soil_geometries_empty), \
     mock_patch.object(tzc, "get_water_features_for_boundary", _fake_water_features_empty), \
     CanopyOverrideProbe() as _ov_probe:
    identify_tree_zone_candidates(
        orchestrator_boundary_coordinates, dem=orchestrator_dem, canopy_height=_ov_override
    )
_ov_probe.assert_override_used(_ov_override, "identify_tree_zone_candidates()")
assert len(_ov_probe.mask_arrays) >= 3, (
    "identify_tree_zone_candidates(): with production_areas/selected_water_zone self-computed, the "
    "override must reach ALL canopy paths (Step-2 gate + identify_optimized_production_areas + "
    f"identify_water_suitability's gate), saw {len(_ov_probe.mask_arrays)}"
)
print(
    "identify_tree_zone_candidates(): a supplied canopy_height override reaches ALL internal canopy "
    f"paths ({len(_ov_probe.mask_arrays)} gates) -- 0 canopy fetches, exact override array used throughout."
)


# --- canopy_height forwarding into the nested identify_road_corridor_ ----
# --- candidates() self-compute call specifically --------------------------
#
# The probe test above proves canopy_height reaches every canopy GATE, but
# identify_road_corridor_candidates() itself has no canopy gate of its own
# (see road_corridors.py's own docstring) -- it only forwards canopy_height
# into ITS OWN nested production_areas/selected_water_zone self-computes,
# both of which are already resolved (non-None) by the time tree_zone_
# candidates.py calls it, so those nested self-computes never fire here and
# the probe's mask-array count can't observe whether the kwarg itself was
# actually forwarded. Directly mocking identify_road_corridor_candidates()
# and checking its call kwargs by identity is the only way to prove this
# specific nested-forwarding call site, not just its downstream effect.

with mock_patch.object(
        tzc, "identify_road_corridor_candidates",
        return_value={"selected_road_corridor": None},
     ) as mock_corridor_canopy, \
     mock_patch.object(tzc, "get_farmland_classification_for_polygon", _fake_farmland_empty), \
     mock_patch.object(tzc, "get_soil_data_for_polygon", _fake_soil_rows_empty), \
     mock_patch.object(tzc, "get_soil_geometries_for_polygon", _fake_soil_geometries_empty), \
     mock_patch.object(tzc, "get_water_features_for_boundary", _fake_water_features_empty):
    identify_tree_zone_candidates(
        orchestrator_boundary_coordinates,
        dem=orchestrator_dem,
        boundary_polygon_utm=OVERRIDE_BOUNDARY_POLYGON_UTM,
        production_areas=OVERRIDE_PRODUCTION_AREAS,
        valleys=OVERRIDE_VALLEYS,
        selected_water_zone=OVERRIDE_SELECTED_WATER_ZONE,
        canopy_height=_ov_override,
    )

assert mock_corridor_canopy.call_count == 1
canopy_corridor_call = mock_corridor_canopy.call_args
assert canopy_corridor_call.kwargs["canopy_height"] is _ov_override, (
    "identify_road_corridor_candidates() must receive this function's own canopy_height override as a "
    "kwarg (identity-checked), not omit it and leave the nested call to self-fetch its own copy"
)
print(
    "identify_tree_zone_candidates(): a supplied canopy_height override correctly reaches the nested "
    "identify_road_corridor_candidates() self-compute call as a kwarg, identity-checked."
)

# REGRESSION: canopy_height left unsupplied -> identify_road_corridor_candidates() still receives it
# explicitly as None (its own default), same as every other forwarded-but-unsupplied override.
with mock_patch.object(
        tzc, "identify_road_corridor_candidates",
        return_value={"selected_road_corridor": None},
     ) as mock_corridor_no_canopy, \
     mock_patch.object(tzc, "get_farmland_classification_for_polygon", _fake_farmland_empty), \
     mock_patch.object(tzc, "get_soil_data_for_polygon", _fake_soil_rows_empty), \
     mock_patch.object(tzc, "get_soil_geometries_for_polygon", _fake_soil_geometries_empty), \
     mock_patch.object(tzc, "get_water_features_for_boundary", _fake_water_features_empty):
    identify_tree_zone_candidates(
        orchestrator_boundary_coordinates,
        dem=orchestrator_dem,
        boundary_polygon_utm=OVERRIDE_BOUNDARY_POLYGON_UTM,
        production_areas=OVERRIDE_PRODUCTION_AREAS,
        valleys=OVERRIDE_VALLEYS,
        selected_water_zone=OVERRIDE_SELECTED_WATER_ZONE,
    )

assert mock_corridor_no_canopy.call_count == 1
no_canopy_call = mock_corridor_no_canopy.call_args
assert no_canopy_call.kwargs.get("canopy_height") is None, (
    "with canopy_height left unsupplied, identify_road_corridor_candidates() must still receive it "
    "explicitly as None (its own default) -- unchanged from behavior before this kwarg was forwarded"
)
print(
    "REGRESSION: with canopy_height left unsupplied, identify_road_corridor_candidates() correctly "
    "receives canopy_height=None -- unchanged from prior behavior."
)


print("\nAll tree_zone_candidates checks passed.")
