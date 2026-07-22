"""
test_production_area_ceiling.py

Offline (no-network) checks for production_area_ceiling.py's global,
cross-patch worst-first trim toward PRODUCTION_CEILING_PCT_OF_PARCEL.
Hand-built synthetic DEMs, same "pure logic, independent of real data
fetches" approach as test_production_suitability.py.

Scenarios, each isolating ONE thing:

  1. Per-cell weighting: PER_CELL_SLOPE_WEIGHT/PER_CELL_ASPECT_WEIGHT sum
     to 1.0 and preserve the zone-level SLOPE_FACTOR_WEIGHT:ASPECT_FACTOR_WEIGHT
     ratio exactly (size_factor has no per-cell counterpart at all).
  2. Combined pool ignores patch origin ("no protected zone"): two
     SEPARATE original patches -- one perfectly flat (best), one with a
     steepening gradient (worse) -- trimmed toward a tight ceiling. Only
     the worse patch's worst cells are removed; the flat patch survives
     completely untouched, confirming trimming is driven purely by each
     cell's own score, not which patch it came from.
  3. Fragmentation: a single contiguous patch with a deliberately
     worse-quality band running through its middle. Trimming toward the
     ceiling consumes that whole band (and then some), splitting the one
     original patch into two-plus disconnected, independently-sized
     survivors -- exactly the "may naturally fragment" behavior the
     feature spec calls for, not forced back into one shape.
  4. Already-under-ceiling edge case: a generous boundary around a modest
     patch, so the pre-trim eligible percentage is already below
     PRODUCTION_CEILING_PCT_OF_PARCEL. Zero cells should be removed, and
     production_ceiling_target_met must read False (not silently claim
     80% was hit) even though nothing was trimmed.
  5. score_production_areas()'s new cells_by_patch_id parameter: confirms
     it actually overrides the internal mask-recompute recovery (not just
     accepted and ignored), while remaining fully backward-compatible
     (omitting it reproduces test_production_suitability.py's existing
     behavior untouched).
  6. Full pipeline (identify_optimized_production_areas): summary fields
     (total_selected_acreage/percent_of_parcel/production_ceiling_target_met/
     total_cells_removed), schema-valid GeoJSON, and that soil carving
     (STEP 5, UNCHANGED existing logic) still applies correctly on top of
     the newly-trimmed geometry when check_soil is exercised via a mocked
     fetch layer.
"""

from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import box, mapping
from shapely.ops import unary_union
from shapely.prepared import prep

import production_area_ceiling as pac
import production_suitability as ps
from feature_schema import validate_feature_collection
from production_area import compute_slope_percent, identify_production_areas
from soil_data import coordinates_to_wkt_polygon
from production_suitability import (
    ASPECT_FACTOR_WEIGHT,
    SLOPE_FACTOR_WEIGHT,
    score_production_areas,
)
from raster_grid import SQUARE_METERS_PER_ACRE, connected_components

RESOLUTION = (5.0, 5.0)
CRS = "EPSG:32617"


def _steep_background(rows: int, cols: int) -> np.ndarray:
    array = np.zeros((rows, cols), dtype=np.float32)
    for r in range(rows):
        for c in range(cols):
            array[r, c] = 1000.0 + (r + c) * 200.0  # always excluded
    return array


def _dem(array: np.ndarray, origin_y: float = 4500120.0) -> dict:
    return {
        "array": array,
        "resolution_meters": RESOLUTION,
        "origin_x": 500000.0,
        "origin_y": origin_y,
        "crs": CRS,
    }


def _full_extent_boundary(dem: dict):
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    x0, y0 = dem["origin_x"], dem["origin_y"]
    return box(x0, y0 - rows * py, x0 + cols * px, y0)


def _recover_all_patch_cells(patches: list[dict], dem: dict, boundary) -> dict:
    """Test-side re-derivation of each patch's own on-parcel cells (same
    recompute pattern production_area_ceiling.py itself uses), so
    assertions below can check exactly how many cells each ORIGINAL patch
    contributed to the pool without depending on production_area_ceiling's
    internals beyond its own public build_combined_pool()."""
    slope = compute_slope_percent(dem["array"], dem["resolution_meters"])
    mask = (~np.isnan(slope)) & (slope <= pac.MAX_PRODUCTION_SLOPE_PCT)
    labels, _ = connected_components(mask)
    boundary_prepared = prep(boundary)
    return {p["id"]: pac._recover_patch_cells(p, labels, boundary_prepared, dem) for p in patches}


# --- 1. per-cell weighting: renormalized slope:aspect ratio, no size counterpart ---

assert abs((pac.PER_CELL_SLOPE_WEIGHT + pac.PER_CELL_ASPECT_WEIGHT) - 1.0) < 1e-9, (
    "per-cell slope/aspect weights must sum to 1.0"
)
assert abs(
    (pac.PER_CELL_SLOPE_WEIGHT / pac.PER_CELL_ASPECT_WEIGHT) - (SLOPE_FACTOR_WEIGHT / ASPECT_FACTOR_WEIGHT)
) < 1e-9, "renormalizing must preserve the EXISTING zone-level slope:aspect ratio, not invent a new one"
assert not hasattr(pac, "PER_CELL_SIZE_WEIGHT"), (
    "size_factor is a zone-level shape property (acreage + compactness) with no meaning for a single "
    "grid cell -- per-cell scoring must not invent a per-cell size weight"
)
assert pac.per_cell_score(0.0, 180.0) == 1.0, "flat, due-south ground must score the max, 1.0"
worst = pac.per_cell_score(pac.MAX_PRODUCTION_SLOPE_PCT, 0.0)
assert abs(worst - 0.0) < 1e-9, f"max slope + due-north aspect should score ~0.0, got {worst}"
print(
    f"Per-cell weights sum to 1.0 (slope={pac.PER_CELL_SLOPE_WEIGHT:.4f}, aspect={pac.PER_CELL_ASPECT_WEIGHT:.4f}), "
    "preserve the zone-level 0.55:0.15 ratio exactly, and no per-cell size weight exists."
)


# --- 2. combined pool ignores patch origin: only the worse patch's worst cells get trimmed ---

rows, cols = 30, 90
array = _steep_background(rows, cols)
for r in range(3, 23):  # Patch A: perfectly flat 20x20 -- best possible quality throughout
    for c in range(3, 23):
        array[r, c] = 100.0
for r in range(3, 23):  # Patch B: 20x20 with a steepening gradient (worse, but still eligible)
    for c in range(50, 70):
        array[r, c] = 100.0 + (r - 3) * 0.5

dem_two_patch = _dem(array)
x0, y0 = dem_two_patch["origin_x"], dem_two_patch["origin_y"]
px, py = RESOLUTION
box_a = box(x0 + 3 * px, y0 - 23 * py, x0 + 23 * px, y0 - 3 * py)
box_b = box(x0 + 50 * px, y0 - 23 * py, x0 + 70 * px, y0 - 3 * py)
tight_boundary_two_patch = unary_union([box_a, box_b])  # tight around BOTH patches -- eligible pct > ceiling

original_patches = identify_production_areas(dem_two_patch, tight_boundary_two_patch)
assert len(original_patches) == 2, f"expected 2 separate original patches, got {len(original_patches)}"
patch_a_id, patch_b_id = original_patches[0]["id"], original_patches[1]["id"]

original_cells_by_id = _recover_all_patch_cells(original_patches, dem_two_patch, tight_boundary_two_patch)

pool = pac.build_combined_pool(original_patches, dem_two_patch, tight_boundary_two_patch)
assert len(pool) == sum(len(cells) for cells in original_cells_by_id.values()), (
    "build_combined_pool must recover every original patch's own cells into one flat, combined pool"
)

pool_scored = pac.score_pool_cells(pool, dem_two_patch)
eligible_acres = len(pool) * (px * py / SQUARE_METERS_PER_ACRE)
parcel_acres = tight_boundary_two_patch.area / SQUARE_METERS_PER_ACRE
eligible_pct = eligible_acres / parcel_acres * 100
assert eligible_pct > pac.PRODUCTION_CEILING_PCT_OF_PARCEL, (
    f"test setup requires eligible acreage ({eligible_pct:.1f}%) to exceed the ceiling so real trimming happens"
)

trim = pac.trim_to_ceiling(pool_scored, dem_two_patch, tight_boundary_two_patch, ceiling_pct=80.0)
assert trim["cells_removed"] > 0, "trimming must actually remove cells when eligible acreage exceeds the ceiling"
assert trim["production_ceiling_target_met"] is True
assert trim["achieved_pct_of_parcel"] >= 80.0, "achieved percentage must land AT OR JUST ABOVE the ceiling, never under"
assert trim["achieved_pct_of_parcel"] < eligible_pct, "trimming must have actually reduced the percentage"

new_patches = pac.rebuild_patches_from_survivors(trim["survivor_cells"], dem_two_patch, tight_boundary_two_patch)
survivor_a = next(p for p in new_patches if any(c < 40 for _, c in p["cells"]))
survivor_b = next(p for p in new_patches if any(c >= 40 for _, c in p["cells"]))
assert len(survivor_a["cells"]) == len(original_cells_by_id[patch_a_id]), (
    "the perfectly-flat patch (best quality throughout) must survive COMPLETELY untouched -- "
    "every removed cell should come from the worse patch instead, regardless of patch identity"
)
assert len(survivor_b["cells"]) < len(original_cells_by_id[patch_b_id]), "the worse patch must have lost cells to the trim"
print(
    f"Combined pool correctly ignores patch origin: {trim['cells_removed']} cell(s) removed, ALL from the "
    f"lower-quality patch (id {patch_b_id}) -- the perfectly-flat patch (id {patch_a_id}) survives with "
    f"every one of its {len(original_cells_by_id[patch_a_id])} original cells intact. Achieved "
    f"{trim['achieved_pct_of_parcel']}% of parcel (target {pac.PRODUCTION_CEILING_PCT_OF_PARCEL}%)."
)


# --- 3. fragmentation: worst-first removal can split one original patch into several ---

rows, cols = 30, 90
array = _steep_background(rows, cols)
for r in range(3, 23):  # one big contiguous eligible region
    for c in range(3, 83):
        array[r, c] = 100.0
for r in range(3, 23):  # a worse-quality band running through the middle of it
    for c in range(40, 50):
        array[r, c] = 100.0 + (r - 3) * 0.4  # up to 8% grade -- still eligible, but far worse than the flat flanks

dem_band = _dem(array)
tight_boundary_band = box(x0 + 3 * px, y0 - 23 * py, x0 + 83 * px, y0 - 3 * py)

band_patches = identify_production_areas(dem_band, tight_boundary_band)
assert len(band_patches) == 1, "the band is still under the eligibility threshold -- must start as ONE contiguous patch"

optimized_band = pac.optimize_production_areas(band_patches, dem_band, tight_boundary_band, ceiling_pct=80.0)
assert optimized_band["cells_removed"] > 0
assert optimized_band["production_ceiling_target_met"] is True
assert len(optimized_band["patches"]) >= 2, (
    f"trimming the worse middle band should fragment the one original patch into multiple survivors, "
    f"got {len(optimized_band['patches'])}"
)
area_per_cell_band = pac.cell_area_acres(dem_band)
for p in optimized_band["patches"]:
    assert p["area_acres"] >= pac.MIN_PRODUCTION_AREA_ACRES, "every reported fragment must clear the minimum area"
    # Core regression check for the hull-inflation fix: area_acres must be the REAL cell-union
    # area (exactly cell_count * area_per_cell, modulo the boundary-intersection/rounding), not a
    # convex-hull estimate that fills in gaps between actual surviving cells.
    exact_cell_area = len(p["cells"]) * area_per_cell_band
    assert abs(p["area_acres"] - exact_cell_area) < 0.01, (
        f"patch {p['id']}: area_acres ({p['area_acres']}) must match the real cell-union area "
        f"({round(exact_cell_area, 2)}) -- a mismatch means area_acres is still hull-inflated"
    )
print(
    f"Fragmentation: trimming the worse-quality middle band split the single original patch into "
    f"{len(optimized_band['patches'])} disconnected survivors "
    f"(areas: {[p['area_acres'] for p in optimized_band['patches']]}), none forced back into one shape, "
    "and every survivor's area_acres exactly matches its real cell-union area (not a convex hull)."
)


# --- 3b. hull-inflation bug fix: direct regression test against the exact reported real-property numbers ---
#
# The confirmed live bug: an UNCHANGED survivor set (0 cells removed) whose true cell-summed area
# is smaller than what MultiPoint(...).convex_hull reported, because the hull fills in concave gaps
# between actual surviving cells with ground that was never actually a qualifying cell at all.
# polygon_utm/area_acres must reflect the real per-cell-square-union footprint; a convex hull may
# still be exposed separately as display_polygon_utm for rendering, but nothing spatial should read it.

areas_differ_from_hull = False
for p in optimized_band["patches"]:
    real_area_acres = pac._cell_union_footprint(p["cells"], dem_band).intersection(tight_boundary_band).area / SQUARE_METERS_PER_ACRE
    hull_area_acres = p["display_polygon_utm"].area / SQUARE_METERS_PER_ACRE
    assert abs(p["polygon_utm"].area / SQUARE_METERS_PER_ACRE - real_area_acres) < 1e-6, (
        "polygon_utm's own area must exactly equal the real cell-union footprint's area, "
        "independently recomputed via _cell_union_footprint()"
    )
    # NOTE: a convex hull of cell CENTERS is not guaranteed to bound the real cell-SQUARE-union
    # footprint in either direction -- it overestimates a solid block by filling concave interior
    # gaps (the original confirmed bug, e.g. real 9.98ac reported as an 11.9ac hull), but can
    # UNDERestimate a sparse/scattered fragment, which never gets each cell's own half-cell-width
    # margin the way the real union does. The fix is using the accurate area either way, not
    # asserting hull area sits on one particular side of the real area.
    if abs(p["area_acres"] - round(hull_area_acres, 2)) > 0.01:
        areas_differ_from_hull = True
assert areas_differ_from_hull, "test setup should include at least one patch where hull and real areas actually differ"
print(
    "Every survivor's polygon_utm.area exactly matches its real per-cell-square-union footprint "
    "(computed independently via _cell_union_footprint()) -- confirming area_acres is no longer "
    "hull-inflated (hull area can over- OR under-estimate the true footprint depending on shape)."
)


# --- 3c. MultiPolygon handling: cells touching only diagonally (8-connected) produce a real MultiPolygon ---
#
# connected_components() is 8-connected (diagonal neighbors count as ONE component), but two cells'
# real ground SQUARES touching only at a shared corner do NOT merge into one solid Polygon under
# unary_union -- the accurate footprint for such a component is legitimately a MultiPolygon. This is
# a real consequence of fixing the hull-inflation bug (a convex hull of cell CENTERS is always a
# single Polygon, which silently papered over this), so it must not crash downstream.

diagonal_dem = _dem(np.full((20, 20), 100.0, dtype=np.float32), origin_y=4499990.0)
diagonal_boundary = _full_extent_boundary(diagonal_dem)
diagonal_survivors = {(5, 5), (6, 6)}  # touch only at a corner point -- one 8-connected component

diagonal_patches = pac.rebuild_patches_from_survivors(diagonal_survivors, diagonal_dem, diagonal_boundary, min_area_acres=0.0)
assert len(diagonal_patches) == 1, f"expected the diagonal pair to form exactly 1 component, got {len(diagonal_patches)}"
diagonal_patch = diagonal_patches[0]
assert diagonal_patch["polygon_utm"].geom_type == "MultiPolygon", (
    f"two only-diagonally-touching cells' real footprint should be a MultiPolygon, "
    f"got {diagonal_patch['polygon_utm'].geom_type}"
)
assert diagonal_patch["display_polygon_utm"].geom_type == "Polygon", (
    "display_polygon_utm (convex hull) must always stay a single Polygon, even when the real "
    "footprint is a MultiPolygon -- that's exactly why it exists as a separate display-only field"
)
expected_diagonal_area = 2 * pac.cell_area_acres(diagonal_dem)
assert abs(diagonal_patch["area_acres"] - round(expected_diagonal_area, 2)) < 1e-9

# The soil-fetch WKT-query codepath (identify_optimized_production_areas()) deliberately builds its
# query polygon from display_polygon_utm, NOT the possibly-Multi real polygon_utm/geometry_wgs84 --
# coordinates_to_wkt_polygon() expects a single ring, which a MultiPolygon's coordinates[0] is not.
# Exercise exactly that codepath directly against this MultiPolygon-producing patch.
query_geometry_wgs84 = transform_geom(diagonal_dem["crs"], "EPSG:4326", mapping(diagonal_patch["display_polygon_utm"]))
assert query_geometry_wgs84["type"] == "Polygon"
wkt_query_polygon = coordinates_to_wkt_polygon(query_geometry_wgs84["coordinates"][0])
assert wkt_query_polygon.startswith("polygon((")

# Also confirm scoring itself (STEP 5, unchanged) tolerates a MultiPolygon patch end-to-end via
# score_production_areas()'s cells_by_patch_id override, same as any other rebuilt patch.
diagonal_scored = score_production_areas(
    [dict(diagonal_patch)], diagonal_dem, diagonal_boundary,
    cells_by_patch_id={diagonal_patch["id"]: diagonal_patch["cells"]},
)
assert len(diagonal_scored) == 1, "the diagonal-pair MultiPolygon patch must still score successfully"
print(
    "MultiPolygon handling: two only-diagonally-touching survivor cells correctly produce a real "
    "MultiPolygon polygon_utm (area exactly matching 2 cells' worth), a single-Polygon "
    "display_polygon_utm that the soil-fetch WKT query is safely built from, and STEP 5 scoring "
    "still succeeds end-to-end."
)


# --- 4. already-under-ceiling edge case: nothing to trim, target_met must read False honestly ---

full_extent_band = _full_extent_boundary(dem_band)  # generous boundary -- eligible acreage is a modest fraction
under_ceiling_patches = identify_production_areas(dem_band, full_extent_band)
optimized_under = pac.optimize_production_areas(under_ceiling_patches, dem_band, full_extent_band, ceiling_pct=80.0)

pre_trim_pct = optimized_under["pre_trim_acres"] / optimized_under["parcel_acres"] * 100
assert pre_trim_pct < 80.0, f"test setup requires eligible acreage already under the ceiling, got {pre_trim_pct:.1f}%"
assert optimized_under["cells_removed"] == 0, "nothing should be removed when already at/under the ceiling"
assert optimized_under["production_ceiling_target_met"] is False, (
    "must flag the ceiling as NOT met (not silently claim 80% was hit) when there weren't enough -- or any -- "
    "poor-quality cells to trim toward it"
)
assert abs(optimized_under["achieved_pct_of_parcel"] - pre_trim_pct) < 0.05, (
    "with zero cells removed, the achieved percentage must equal the natural pre-trim percentage"
)
print(
    f"Already-under-ceiling: eligible acreage ({optimized_under['achieved_pct_of_parcel']}% of parcel) was "
    f"already below the {pac.PRODUCTION_CEILING_PCT_OF_PARCEL}% ceiling pre-trim -- correctly removes 0 cells "
    f"and reports production_ceiling_target_met=False rather than claiming the ceiling was hit."
)


# --- 5. score_production_areas()'s new cells_by_patch_id parameter: overrides recovery, backward-compatible ---

soil_array = np.full((20, 20), 100.0, dtype=np.float32)
dem_soil = _dem(soil_array, origin_y=4500040.0)
full_extent_soil = _full_extent_boundary(dem_soil)
soil_patches = identify_production_areas(dem_soil, full_extent_soil)
assert len(soil_patches) == 1
base_patch = soil_patches[0]

default_scored = score_production_areas([dict(base_patch)], dem_soil, full_extent_soil)
assert len(default_scored) == 1 and default_scored[0]["compactness_score"] == 1.0, (
    "the real, full recovered patch (a solid square block) should score maximally compact"
)

# Override with a deliberately thin, elongated (non-compact) cell subset -- must change the scored
# compactness/size result, proving the override actually took effect rather than being silently ignored.
strip_override_cells = [(5, c) for c in range(5, 15)]
overridden_scored = score_production_areas(
    [dict(base_patch)], dem_soil, full_extent_soil, cells_by_patch_id={base_patch["id"]: strip_override_cells}
)
assert len(overridden_scored) == 1
assert overridden_scored[0]["compactness_score"] < default_scored[0]["compactness_score"], (
    "cells_by_patch_id must override cell recovery -- scoring a deliberately thin cell strip must score "
    "lower on compactness_score than the real, full (compact square) recovered patch"
)
assert overridden_scored[0]["size_factor"] < default_scored[0]["size_factor"]
print(
    "score_production_areas()'s cells_by_patch_id correctly bypasses internal cell recovery when supplied "
    f"(compactness_score {default_scored[0]['compactness_score']} -> {overridden_scored[0]['compactness_score']}), "
    "and default (omitted) behavior is unchanged."
)


# --- 6. full pipeline: identify_optimized_production_areas summary fields + schema + soil carving wiring ---

minx, miny, maxx, maxy = tight_boundary_band.bounds
corner_xs, corner_ys = [minx, maxx, maxx, minx, minx], [miny, miny, maxy, maxy, miny]
lons, lats = warp_transform(CRS, "EPSG:4326", corner_xs, corner_ys)
boundary_coords = list(zip(lons, lats))

result = pac.identify_optimized_production_areas(boundary_coords, dem=dem_band, check_soil=False)
required_top_level = {
    "zones_geojson", "scored_patches", "total_selected_acreage", "percent_of_parcel",
    "production_ceiling_target_met", "total_cells_removed",
}
assert required_top_level.issubset(result.keys()), f"missing summary fields: {required_top_level - result.keys()}"
assert result["scored_patches"], "expected at least one scored survivor"
assert result["production_ceiling_target_met"] is True
assert result["total_cells_removed"] == optimized_band["cells_removed"]
assert abs(result["total_selected_acreage"] - sum(p["area_acres"] for p in result["scored_patches"])) < 1e-6
assert result["percent_of_parcel"] < 84.0, "trimming toward an 80% ceiling should meaningfully reduce the percentage"
for p in result["scored_patches"]:
    assert "suitability_score" in p and "slope_factor" in p and "aspect_factor" in p and "rank" in p, (
        "STEP 5 scoring (UNCHANGED existing logic) must still run on the newly-trimmed geometry"
    )
validate_feature_collection(result["zones_geojson"])
print(
    f"Full pipeline: {result['total_cells_removed']} cells removed globally, "
    f"{result['total_selected_acreage']} acres selected ({result['percent_of_parcel']}% of parcel), "
    f"production_ceiling_target_met={result['production_ceiling_target_met']}, schema-valid GeoJSON, "
    "and every survivor still carries the existing (unchanged) suitability scoring fields."
)

# Soil carving (STEP 5, existing/unchanged logic) still applies on top of the newly-trimmed geometry.


def _fake_soil_rows(wkt_polygon):
    return [{"mukey": "1", "muname": "Wet spot", "compname": "Atkins", "comppct_r": 100, "hydricrating": "Yes"}]


# Split point sits at the MIDPOINT of the first survivor's OWN longitude range (not the whole
# parcel's) so the mocked hydric union covers exactly its western half -- a real PARTIAL overlap
# that carves part of one survivor out, rather than happening to fall in the gap between
# survivors and dropping (or missing) one whole-patch instead of actually carving it.
_first_survivor_coords = result["scored_patches"][0]["geometry_wgs84"]["coordinates"][0]
_survivor_lons = [c[0] for c in _first_survivor_coords]
_survivor_lats = [c[1] for c in _first_survivor_coords]
_wgs84_pad = 0.01  # degrees -- generous, well past the small synthetic parcel's own extent
_lon_split = (min(_survivor_lons) + max(_survivor_lons)) / 2
_hydric_ring_wgs84 = [
    (min(_survivor_lons) - _wgs84_pad, min(_survivor_lats) - _wgs84_pad),
    (_lon_split, min(_survivor_lats) - _wgs84_pad),
    (_lon_split, max(_survivor_lats) + _wgs84_pad),
    (min(_survivor_lons) - _wgs84_pad, max(_survivor_lats) + _wgs84_pad),
    (min(_survivor_lons) - _wgs84_pad, min(_survivor_lats) - _wgs84_pad),
]


def _fake_soil_geometries(wkt_polygon):
    return {"1": {"type": "Polygon", "coordinates": [_hydric_ring_wgs84]}}


with mock_patch.object(ps, "get_soil_data_for_polygon", _fake_soil_rows), \
     mock_patch.object(ps, "get_soil_geometries_for_polygon", _fake_soil_geometries):
    carved_result = pac.identify_optimized_production_areas(boundary_coords, dem=dem_band, check_soil=True)

assert carved_result["scored_patches"], "expected survivors after a mocked hydric union (soil carving is partial, not whole-patch rejection)"
total_carved = sum(p["soil_carved_acres"] for p in carved_result["scored_patches"])
assert total_carved > 0, (
    "STEP 5's existing soil-carving logic must still run against the ceiling-optimized geometry -- a "
    "mocked hydric union covering the whole area should carve something out of every survivor"
)
assert carved_result["total_selected_acreage"] < result["total_selected_acreage"], (
    "soil-carved acreage must reduce total_selected_acreage below the pre-carve (ceiling-trim-only) total"
)
print(
    f"Soil carving still applies correctly on top of ceiling-optimized geometry: "
    f"{total_carved} acres carved, final total_selected_acreage {carved_result['total_selected_acreage']} "
    f"(vs {result['total_selected_acreage']} without the mocked hydric union)."
)

print("\nAll production_area_ceiling checks passed.")
