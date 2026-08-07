"""
test_pipeline_context.py

Offline (no-network) checks for pipeline_context.py's own orchestration
contract:

  1. Every real fetch entry point it calls directly (dem_data.
     get_dem_for_boundary, production_area_ceiling.
     identify_optimized_production_areas, farm_roads_data.
     get_road_exclusion_union_utm, road_corridors.
     _fetch_floodplain_hydric_union, water_candidate_zones.
     identify_water_system_candidate_zones) is called exactly ONCE -- that
     de-duplication is the entire point of this module.
  2. ridge_lines and valleys are genuinely different results, computed
     from two different DEM arrays (the real one, and a real, non-
     mutating elevation inversion of it), not the same computation twice.
  3. production_areas matches identify_optimized_production_areas()'s
     real scored_patches shape (STEP 4 fields present), not identify_
     production_areas()'s raw, unscored shape.
  4. soil_exclusion_unions['hydric_floodplain_union'] and water_zones
     reuse the already-computed dem/valleys/boundary_polygon_utm
     instances where the underlying entry point actually supports doing
     so -- and this file also demonstrates, rather than hides, the one
     real gap pipeline_context.py's own KNOWN LIMITATIONS section flags:
     identify_water_system_candidate_zones() has no override for valleys/
     production_areas/boundary_polygon_utm, so those three specifically
     are NOT (and, given that function's real signature, cannot be) reused
     for water_zones.

Every real network-touching entry point pipeline_context.py calls
directly is mocked, so this file never touches the network. valley_
delineation.delineate_valleys is left real/spied-on (wraps=), not
replaced -- it's pure numpy with no network dependency, and check #2
above needs its genuine output to mean anything.

Synthetic terrain: a 60x60 DEM built from two independent, non-
overlapping landforms so valleys and ridge_lines land on predictably
disjoint halves of the grid:
  - Left half (cols 0-29): a real V-shaped valley trough at col 12,
    exiting the grid at row 0 -- delineate_valleys() against the REAL
    dem should trace it.
  - Right half (cols 30-59): a real ridge crest at col 45, exiting the
    grid at row 59 -- a local elevation MAXIMUM, so delineate_valleys()
    against the real dem finds nothing there, but against an elevation-
    inverted copy (a ridge in real terrain is a valley in its negation)
    it traces cleanly.
The right half is built uniformly higher than the left half (no
elevation overlap between them) specifically so neither landform's flow
can cross into the other's half in either the real or inverted DEM --
keeping the two features cleanly, deterministically separated.
"""

from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon, box

import pipeline_context as pc
from dem_data import _utm_epsg_for_lonlat

# --- synthetic DEM: real-world centroid so the UTM zone/CRS math is genuine ---

CENTER_LON, CENTER_LAT = -79.98, 40.64
EPSG = _utm_epsg_for_lonlat(CENTER_LON, CENTER_LAT)
DST_CRS = f"EPSG:{EPSG}"
center_x, center_y = warp_transform("EPSG:4326", DST_CRS, [CENTER_LON], [CENTER_LAT])
center_x, center_y = center_x[0], center_y[0]

RESOLUTION = 5.0
ROWS = COLS = 60
VALLEY_COL = 12  # left-half trough
RIDGE_COL = 45  # right-half crest

origin_x = center_x - COLS * RESOLUTION / 2
origin_y = center_y + ROWS * RESOLUTION / 2

# The synthetic property boundary is the exact rectangle this DEM covers,
# reprojected back to WGS84 -- same "boundary and DEM genuinely line up"
# construction as test_water_system_candidate_pipeline.py.
utm_corners_x = [origin_x, origin_x + COLS * RESOLUTION, origin_x + COLS * RESOLUTION, origin_x, origin_x]
utm_corners_y = [origin_y, origin_y, origin_y - ROWS * RESOLUTION, origin_y - ROWS * RESOLUTION, origin_y]
lons, lats = warp_transform(DST_CRS, "EPSG:4326", utm_corners_x, utm_corners_y)
boundary_coordinates = list(zip(lons, lats))
anchor_lon_lat = boundary_coordinates[0]

RADIUS = 8  # column-distance over which the row-wise gradient tapers to 0


def _row_weight(col: int, target_col: int) -> float:
    """
    Tapers the row-wise (north-south) gradient down to exactly 0 beyond
    RADIUS columns from target_col, instead of applying it uniformly
    across the whole half -- a uniform row-wise gradient reaching all the
    way to a column-range boundary (a true grid edge, or the other
    landform's half) creates a spurious single-column drainage CHANNEL
    there (confirmed live while building this fixture: the column-wise
    gradient alone can't carry flow further once no lower sideways
    neighbor remains, so a residual row-wise gradient chains flow
    straight down that one edge column instead, registering as an
    unintended extra valley/ridge far from the intended landform). Taper
    is gradual (not a hard cutoff) so it never inverts the column-wise
    elevation ordering between adjacent columns at any row -- the max
    per-column row-term change (0.3/RADIUS per column) stays well under
    the 4.0-per-column lateral term this is added to.
    """
    return 0.3 * max(0.0, 1.0 - abs(col - target_col) / RADIUS)


array = np.zeros((ROWS, COLS), dtype=np.float32)
for r in range(ROWS):
    for c in range(COLS):
        if c < 30:
            # Left-half valley: a real V-shaped trough at col 12, exiting
            # the grid at the north (row 0) edge.
            array[r, c] = 1000.0 + abs(c - VALLEY_COL) * 4.0 + r * _row_weight(c, VALLEY_COL)
        else:
            # Right-half ridge: a real crest at col 45 -- a local
            # elevation MAXIMUM, descending away from it in both
            # directions, exiting (as a trough, once inverted) at the
            # south (row 59) edge. Uniformly higher than the whole left
            # half (1122-1200 vs 1000-1086) so flow never crosses between
            # the two halves either direction.
            array[r, c] = 1200.0 - abs(c - RIDGE_COL) * 4.0 - r * _row_weight(c, RIDGE_COL)

synthetic_dem = {
    "array": array,
    "resolution_meters": (RESOLUTION, RESOLUTION),
    "origin_x": origin_x,
    "origin_y": origin_y,
    "crs": DST_CRS,
}
original_array_snapshot = array.copy()

# --- fake return values for every mocked entry point ---

fake_patch = {
    "id": 0,
    "area_acres": 1.23,
    "representative_elevation_m": 1000.0,
    "polygon_utm": box(0, 0, 10, 10),
    "render_polygon_utm": box(0, 0, 10, 10),
    "render_fill_polygon_utm": box(0, 0, 10, 10),
    "geometry_wgs84": {"type": "Polygon", "coordinates": [[[0.0, 0.0]]]},
    "cells": [(0, 0)],
    "hole_footprints": [],
    "source_patch_id": 0,
    # STEP-4-only advisory fields identify_production_areas()'s own RAW
    # patches would never carry -- present here specifically so the
    # assertions below can confirm production_areas holds the optimized/
    # scored shape, not the raw one.
    "suitability_score": 87.5,
    "rank": 1,
    "slope_factor": 0.9,
    "size_factor": 0.8,
    "aspect_factor": 0.7,
}
fake_optimized_result = {
    "zones_geojson": {"type": "FeatureCollection", "features": []},
    "scored_patches": [fake_patch],
    "total_selected_acreage": 1.23,
    "percent_of_parcel": 5.0,
    "production_ceiling_target_met": True,
    "total_cells_removed": 0,
}

fake_existing_roads_union = box(100, 100, 200, 200)
fake_hydric_union = box(50, 50, 60, 60)

fake_water_zone_feature = {
    "type": "Feature",
    "id": "water-system-candidate-0",
    "geometry": {"type": "Polygon", "coordinates": [[[0.0, 0.0]]]},
    "properties": {"layer": "water_system_candidate"},
}
fake_water_system_result = {
    "zones_geojson": {"type": "FeatureCollection", "features": [fake_water_zone_feature]},
    "valleys_geojson": {"type": "FeatureCollection", "features": []},
    "production_areas_geojson": {"type": "FeatureCollection", "features": []},
}

# --- run build_pipeline_context with every real fetch entry point mocked ---

with mock_patch.object(pc.dem_data, "get_dem_for_boundary", return_value=synthetic_dem) as mock_get_dem, \
     mock_patch.object(
         pc.valley_delineation, "delineate_valleys", wraps=pc.valley_delineation.delineate_valleys
     ) as mock_delineate, \
     mock_patch.object(
         pc.production_area_ceiling, "identify_optimized_production_areas", return_value=fake_optimized_result
     ) as mock_optimize, \
     mock_patch.object(
         pc.farm_roads_data, "get_road_exclusion_union_utm", return_value=fake_existing_roads_union
     ) as mock_roads, \
     mock_patch.object(
         pc.road_corridors, "_fetch_floodplain_hydric_union", return_value=(fake_hydric_union, False)
     ) as mock_floodplain, \
     mock_patch.object(
         pc.water_candidate_zones,
         "identify_water_system_candidate_zones",
         return_value=fake_water_system_result,
     ) as mock_water:
    ctx = pc.build_pipeline_context(boundary_coordinates, anchor_lon_lat)

assert isinstance(ctx, pc.PipelineContext)

# --- 1. every real fetch entry point is called exactly once ---

assert mock_get_dem.call_count == 1, "dem_data.get_dem_for_boundary must be called exactly once"
assert mock_optimize.call_count == 1, "identify_optimized_production_areas must be called exactly once"
assert mock_roads.call_count == 1, "farm_roads_data.get_road_exclusion_union_utm must be called exactly once"
assert mock_floodplain.call_count == 1, "_fetch_floodplain_hydric_union must be called exactly once"
assert mock_water.call_count == 1, "identify_water_system_candidate_zones must be called exactly once"
print(
    "Every underlying fetch entry point (DEM, optimized production areas, road exclusion, "
    "floodplain/hydric union, water-system candidate zones) was called exactly once."
)

# valley_delineation.delineate_valleys is called exactly twice -- once for valleys, once for
# ridge_lines -- against two genuinely different DEM arrays (real, then inverted), not the same
# dem/computation applied twice. The original dem's own array must be untouched afterward.
assert mock_delineate.call_count == 2, "delineate_valleys should run exactly twice: once for valleys, once for ridge_lines"
first_call_dem = mock_delineate.call_args_list[0].args[0]
second_call_dem = mock_delineate.call_args_list[1].args[0]
assert first_call_dem is synthetic_dem, "the valleys pass must run against the real, unmodified dem"
assert second_call_dem is not synthetic_dem, "the ridge_lines pass must run against a SEPARATE (inverted) dem dict"
np.testing.assert_array_equal(second_call_dem["array"], -synthetic_dem["array"])
np.testing.assert_array_equal(
    synthetic_dem["array"], original_array_snapshot
), "inverting the DEM for ridge_lines must not mutate the original dem['array']"
print("delineate_valleys ran exactly twice, against the real dem and a separate, non-mutating inverted copy.")

# --- 2. ridge_lines and valleys are genuinely different results ---

assert ctx.valleys, "expected at least one delineated valley on the synthetic left-half trough"
assert ctx.ridge_lines, "expected at least one delineated ridge line on the synthetic right-half crest"

valley_cells = {cell for v in ctx.valleys for branch in v["branches_rowcol"] for cell in branch}
ridge_cells = {cell for v in ctx.ridge_lines for branch in v["branches_rowcol"] for cell in branch}
assert valley_cells, "valleys carries no member cells"
assert ridge_cells, "ridge_lines carries no member cells"
assert valley_cells.isdisjoint(ridge_cells), "valleys and ridge_lines must not share any cell on this synthetic terrain"
assert all(col < 30 for _row, col in valley_cells), "every valley cell should fall on the synthetic left-half trough"
assert all(col >= 30 for _row, col in ridge_cells), "every ridge cell should fall on the synthetic right-half crest"
print(
    f"valleys ({len(valley_cells)} cell(s), left half) and ridge_lines ({len(ridge_cells)} cell(s), "
    "right half) are genuinely distinct, non-overlapping results, not the same computation twice."
)

# --- 3. production_areas matches identify_optimized_production_areas()'s real per-patch shape ---

assert ctx.production_areas == [fake_patch]
assert "suitability_score" in ctx.production_areas[0] and "rank" in ctx.production_areas[0], (
    "production_areas must carry STEP 4's scoring fields -- proof this is identify_optimized_"
    "production_areas()'s scored_patches, not identify_production_areas()'s raw (unscored) shape"
)
optimize_call = mock_optimize.call_args
assert optimize_call.args[0] == boundary_coordinates
assert optimize_call.kwargs["dem"] is synthetic_dem, "must reuse the already-fetched dem, not fetch its own"
print(
    "production_areas matches identify_optimized_production_areas()'s scored_patches shape "
    "(STEP 4 fields present) and reuses the already-fetched DEM rather than fetching its own."
)

# --- 4. existing_roads / soil_exclusion_unions carry the real fetched unions ---

assert ctx.existing_roads is fake_existing_roads_union
assert ctx.soil_exclusion_unions == {
    "hydric_floodplain_union": fake_hydric_union,
    "erosion_prone_union": None,
}
roads_call = mock_roads.call_args
assert roads_call.args[0] == boundary_coordinates and roads_call.args[1] is synthetic_dem
floodplain_call = mock_floodplain.call_args
assert floodplain_call.args[0] == boundary_coordinates
assert floodplain_call.args[1] is synthetic_dem
assert floodplain_call.args[2] is ctx.valleys, "must reuse the already-computed valleys, not re-derive them"
assert floodplain_call.args[3] is ctx.boundary_polygon_utm, "must reuse the already-computed boundary polygon"
print(
    "existing_roads and soil_exclusion_unions['hydric_floodplain_union'] carry the real fetched "
    "geometry; the floodplain/hydric fetch reused the same dem/valleys/boundary_polygon_utm "
    "instances already computed above, not re-derived copies. erosion_prone_union is None -- see "
    "pipeline_context.py's own KNOWN LIMITATIONS #3 for why (no shared erosion-prone-soil union "
    "builder currently exists anywhere in this codebase to call)."
)

# --- 5. water_zones reuses the already-fetched dem -- the one override that entry point supports ---

water_call = mock_water.call_args
assert water_call.args[0] == boundary_coordinates
assert water_call.kwargs["dem"] is synthetic_dem, "water_zones must reuse the already-fetched dem, not fetch a second one"
assert ctx.water_zones == [fake_water_zone_feature]
print(
    "water_zones reuses the already-fetched dem (the only override identify_water_system_"
    "candidate_zones() currently accepts) rather than triggering a second DEM fetch. It does NOT "
    "(and, given that function's real signature, currently cannot) reuse this context's own "
    "valleys/production_areas/boundary_polygon_utm -- see pipeline_context.py's own KNOWN "
    "LIMITATIONS #1 for the flagged gap this demonstrates rather than hides."
)

# --- boundary_polygon_utm sanity check ---

assert ctx.dem is synthetic_dem
assert isinstance(ctx.boundary_polygon_utm, Polygon)
expected_area = (COLS * RESOLUTION) * (ROWS * RESOLUTION)
assert abs(ctx.boundary_polygon_utm.area - expected_area) < 1.0, "boundary_polygon_utm should match the DEM's own footprint"

print("\nAll pipeline_context checks passed.")
