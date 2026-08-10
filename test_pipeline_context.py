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
  4. soil_exclusion_unions['hydric_floodplain_union'] and water_zones both
     reuse the already-computed dem/boundary_polygon_utm/valleys/
     production_areas instances -- identify_water_system_candidate_zones()
     now accepts all three as overrides (a prior branch), and this branch
     wires them through, so this is the test that actually proves the
     water-zone step's OWN internal delineate_valleys()/
     identify_production_areas() self-compute fallbacks are never
     triggered a second time: identify_water_system_candidate_zones() is
     run for REAL here (not fully mocked away), with its own internal
     fallback functions separately mocked and asserted at zero calls --
     see the "water_zones" section below.
  5. selected_water_zone reuses this context's own boundary_polygon_utm/
     valleys/production_areas instances via fetch_and_select_optimal_
     water_zone()'s own override params (mock+inspect call kwargs, same
     pattern as the water_zones section).
  6. selected_road_corridor reuses those same instances, PLUS this
     context's own selected_water_zone and soil_exclusion_unions
     ['hydric_floodplain_union'], via identify_road_corridor_candidates()'s
     own override params (mock+inspect call kwargs). floodplain_data_
     is_fallback is separately proven to carry the REAL flag -- not a
     hardcoded/defaulted value -- by forcing _fetch_floodplain_hydric_
     union() into its fallback path (return value's second element True)
     in a second, dedicated build_pipeline_context() run and asserting
     that True (not the default-False every other case here would show)
     reaches identify_road_corridor_candidates() and soil_exclusion_
     unions['hydric_floodplain_is_fallback'] alike.
  7. soil_exclusion_unions now carries 3 keys -- 'hydric_floodplain_union',
     'hydric_floodplain_is_fallback', and 'erosion_prone_union' --
     confirming the prior 2-key shape (which silently dropped is_fallback)
     is gone.
  8. selected_structure_site and tree_zone_patches reuse this context's
     own boundary_polygon_utm/valleys/production_areas/selected_water_zone/
     selected_road_corridor/hydric_floodplain_union/floodplain_data_is_
     fallback instances via identify_solar_candidate_zones()'s and
     identify_tree_zone_candidates()'s own override params (mock+inspect
     call kwargs, identity checks, same pattern as sections 5-7 above) --
     AND, unlike every prior section, both are left real/wraps= (not
     canned return_value mocks) specifically so this file can also measure
     -- not assume -- the actual total call counts identify_optimized_
     production_areas()/identify_water_suitability()/identify_road_
     corridor_candidates() reach across the WHOLE build_pipeline_context()
     run. That measurement is THE POINT of this section: it now DOES stay
     at "still exactly 1", same as water_zones/selected_water_zone/
     selected_road_corridor's own dedup in sections 4-7 above --
     identify_solar_candidate_zones() has its own internal, one-level-
     deeper call to identify_tree_zone_candidates() (its own "TREE-ZONE-
     CANDIDATE exclusion" step) which USED TO forward none of the
     overrides identify_solar_candidate_zones() itself received, causing
     that inner call to self-compute production_areas/selected_water_zone/
     selected_road_corridor all over again via its own separate module-
     level bindings -- a real, MEASURED redundancy this branch's own prior
     testing found (see pipeline_context.py's own KNOWN LIMITATIONS #4,
     now marked RESOLVED) and a follow-up branch fixed directly inside
     solar_suitability.py by forwarding those same overrides into that
     nested call. This section's own assertions prove the fix: the nested
     call's own self-compute checks now fire ZERO times too, same as this
     module's own two direct calls always did.

  9. dem is optional -- a caller-supplied dem= skips dem_data.
     get_dem_for_boundary() entirely (call_count == 0, identity-checked on
     both the returned context.dem and one representative downstream
     call), and the pre-existing no-override path (dem= omitted) still
     self-fetches exactly once, unchanged -- see section 12/13 below.

Every real network-touching entry point pipeline_context.py calls
directly is mocked, so this file never touches the network. valley_
delineation.delineate_valleys is left real/spied-on (wraps=), not
replaced -- it's pure numpy with no network dependency, and check #2
above needs its genuine output to mean anything. identify_water_system_
candidate_zones() itself is also left real/spied-on (wraps=) rather than
replaced outright, specifically so check #4 can prove its own internal
self-compute fallbacks (a SEPARATE set of module-level bindings inside
water_candidate_zones.py -- see that section's own comments for why
patching valley_delineation.delineate_valleys doesn't also intercept
water_candidate_zones.py's own internal reference to the same original
function) are never called; its own mandatory canopy fetch and optional
road fetch are separately stubbed so this stays fully offline.

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

from contextlib import ExitStack
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon, box

import pipeline_context as pc
import solar_suitability
import tree_zone_candidates
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
fake_selected_water_zone = {
    "type": "Feature",
    "id": "water-zone-selected",
    # render_fill_polygon_utm is genuinely dereferenced by solar_suitability.
    # identify_solar_candidate_zones()/tree_zone_candidates.identify_tree_
    # zone_candidates() when this context's own selected_water_zone reaches
    # them (both left real/wraps= below, unlike every prior section's fully
    # canned mock) -- a bare placeholder Feature dict without this key would
    # KeyError there.
    "render_fill_polygon_utm": box(0, 0, 10, 10),
}
fake_selected_road_corridor = {
    "type": "Feature",
    "id": "road-corridor-selected",
    # cell_footprint_polygon_utm/cells are genuinely dereferenced the same
    # way -- solar_suitability.py's own Tier-1 road-proximity candidate
    # scoring reads cell_footprint_polygon_utm directly, and tree_zone_
    # candidates.py's own _road_corridor_exclusion_polygon() reads cells.
    "cell_footprint_polygon_utm": box(0, 0, 10, 10),
    "cells": [(0, 0)],
}
fake_road_corridor_result = {
    "zones_geojson": {"type": "FeatureCollection", "features": []},
    "all_scored_candidates": [],
    "selected_road_corridor": fake_selected_road_corridor,
}


def _fake_clean_canopy_mask(boundary_polygon_utm, dem, buffer_meters=None, canopy_height=None):
    """Offline stand-in for production_area.get_required_tree_root_zone_mask_utm() --
    identify_water_system_candidate_zones()'s canopy fetch is MANDATORY (no
    try/except around it), so it must be stubbed for this file to run real/
    unmocked through that function without touching the network. All-False
    (no tree cover anywhere) is a pure no-op against compute_water_eligible_
    cells()'s canopy gate -- this file isn't testing that gate. canopy_height
    mirrors the real function's new optional pre-fetched-canopy override
    parameter (the callers now always forward canopy_height= through, so this
    stand-in must accept it too); it is ignored here -- this stub returns its
    fixed clean mask regardless of where canopy would have come from."""
    return np.zeros(dem["array"].shape, dtype=bool)


# --- run build_pipeline_context with every real fetch entry point mocked ---
#
# identify_solar_candidate_zones()/identify_tree_zone_candidates() (this
# module's own two new direct calls) are left real/wraps= below, same
# "prove the dedup for real" standard identify_water_system_candidate_
# zones() already sets in this file -- see section 8's own comment for why
# that matters here specifically. Everything EACH of those two functions'
# own internal self-compute fallbacks could reach (mandatory canopy fetch,
# and separately-bound identify_optimized_production_areas()/identify_
# water_suitability()/identify_road_corridor_candidates() references) is
# mocked at THEIR OWN module-level bindings (solar_suitability.*, tree_
# zone_candidates.*) -- classic "patch where it's looked up" again, a
# THIRD independent set of bindings from pc's own and water_candidate_
# zones.py's own above. More than 20 context managers in one `with`
# statement hits CPython's static nesting limit, so this uses ExitStack
# instead -- functionally identical to the chained `with` above it.

with ExitStack() as _stack:
    _enter = _stack.enter_context
    mock_get_dem = _enter(mock_patch.object(pc.dem_data, "get_dem_for_boundary", return_value=synthetic_dem))
    mock_delineate = _enter(
        mock_patch.object(pc.valley_delineation, "delineate_valleys", wraps=pc.valley_delineation.delineate_valleys)
    )
    mock_optimize = _enter(
        mock_patch.object(
            pc.production_area_ceiling, "identify_optimized_production_areas", return_value=fake_optimized_result
        )
    )
    mock_roads = _enter(
        mock_patch.object(pc.farm_roads_data, "get_road_exclusion_union_utm", return_value=fake_existing_roads_union)
    )
    mock_floodplain = _enter(
        mock_patch.object(pc.road_corridors, "_fetch_floodplain_hydric_union", return_value=(fake_hydric_union, False))
    )
    mock_water = _enter(
        mock_patch.object(
            pc.water_candidate_zones,
            "identify_water_system_candidate_zones",
            wraps=pc.water_candidate_zones.identify_water_system_candidate_zones,
        )
    )
    mock_water_zone_canopy = _enter(
        mock_patch.object(pc.water_candidate_zones, "get_required_tree_root_zone_mask_utm", side_effect=_fake_clean_canopy_mask)
    )
    mock_water_zone_roads = _enter(
        mock_patch.object(pc.water_candidate_zones, "_fetch_road_exclusion_union_utm", return_value=None)
    )
    # mock_water_zone_delineate/mock_water_zone_identify_pa patch water_candidate_
    # zones.py's OWN module-level `delineate_valleys`/`identify_production_areas`
    # names (bound via `from valley_delineation import delineate_valleys` / `from
    # production_area import (..., identify_production_areas, ...)` at import
    # time) -- a SEPARATE binding from pc.valley_delineation.delineate_valleys/
    # pc.production_area_ceiling.identify_optimized_production_areas above.
    # Patching the latter does NOT intercept the former (classic "patch where
    # it's looked up, not where it's defined" -- water_candidate_zones.py's own
    # `if valleys is None: valleys = delineate_valleys(dem)` self-compute
    # fallback resolves `delineate_valleys` in ITS OWN module namespace, not
    # valley_delineation's). Both must be patched separately to actually prove
    # neither fallback fires when valleys/production_areas are supplied as
    # overrides -- that's the whole point of section 4 below.
    mock_water_zone_delineate = _enter(mock_patch.object(pc.water_candidate_zones, "delineate_valleys"))
    mock_water_zone_identify_pa = _enter(mock_patch.object(pc.water_candidate_zones, "identify_production_areas"))
    mock_select_water_zone = _enter(
        mock_patch.object(pc, "fetch_and_select_optimal_water_zone", return_value=fake_selected_water_zone)
    )
    mock_road_corridor = _enter(
        mock_patch.object(pc.road_corridors, "identify_road_corridor_candidates", return_value=fake_road_corridor_result)
    )

    # --- solar/tree_zone: this module's own two new direct calls, left real ---
    mock_solar = _enter(mock_patch.object(pc, "identify_solar_candidate_zones", wraps=pc.identify_solar_candidate_zones))
    mock_tree_zone = _enter(mock_patch.object(pc, "identify_tree_zone_candidates", wraps=pc.identify_tree_zone_candidates))

    # --- solar_suitability.py's OWN mandatory canopy fetch + module-level
    # bindings of identify_optimized_production_areas/identify_water_
    # suitability/identify_road_corridor_candidates -- a THIRD independent
    # set of bindings, separate from pc's own and water_candidate_zones.py's
    # own above (see this block's own leading comment). mock_solar_optimize/
    # mock_solar_water/mock_solar_road are expected to see ZERO calls --
    # proof that identify_solar_candidate_zones()'s OWN top-level self-
    # compute checks correctly skip when this context's own production_
    # areas/selected_water_zone/selected_road_corridor are supplied. ---
    _enter(mock_patch.object(solar_suitability, "get_required_tree_root_zone_mask_utm", side_effect=_fake_clean_canopy_mask))
    mock_solar_optimize = _enter(
        mock_patch.object(solar_suitability, "identify_optimized_production_areas", return_value=fake_optimized_result)
    )
    mock_solar_water = _enter(
        mock_patch.object(
            solar_suitability, "identify_water_suitability", return_value={"selected_water_zone": fake_selected_water_zone}
        )
    )
    mock_solar_road = _enter(
        mock_patch.object(solar_suitability, "identify_road_corridor_candidates", return_value=fake_road_corridor_result)
    )

    # --- tree_zone_candidates.py's OWN mandatory canopy fetch + module-level
    # bindings of the same three functions -- a FOURTH independent set of
    # bindings. These are reached TWICE over the whole run: once from THIS
    # module's own direct identify_tree_zone_candidates() call above, and
    # once more from INSIDE identify_solar_candidate_zones()'s own internal,
    # separate, nested identify_tree_zone_candidates() call (its own
    # "TREE-ZONE-CANDIDATE exclusion" step) -- BOTH now forward every
    # override correctly (a follow-up branch fixed the nested call, which
    # used to forward none of them -- see pipeline_context.py's own KNOWN
    # LIMITATIONS #4, now RESOLVED), so mock_tz_optimize/mock_tz_water/
    # mock_tz_road are expected to see ZERO calls, same as mock_solar_
    # optimize/mock_solar_water/mock_solar_road above -- section 8 proves
    # this. ---
    _enter(mock_patch.object(tree_zone_candidates, "get_required_tree_root_zone_mask_utm", side_effect=_fake_clean_canopy_mask))
    mock_tz_optimize = _enter(
        mock_patch.object(tree_zone_candidates, "identify_optimized_production_areas", return_value=fake_optimized_result)
    )
    mock_tz_water = _enter(
        mock_patch.object(
            tree_zone_candidates, "identify_water_suitability", return_value={"selected_water_zone": fake_selected_water_zone}
        )
    )
    mock_tz_road = _enter(
        mock_patch.object(tree_zone_candidates, "identify_road_corridor_candidates", return_value=fake_road_corridor_result)
    )

    ctx = pc.build_pipeline_context(boundary_coordinates, anchor_lon_lat)

assert isinstance(ctx, pc.PipelineContext)

# --- 1. every real fetch entry point is called exactly once ---

assert mock_get_dem.call_count == 1, "dem_data.get_dem_for_boundary must be called exactly once"
assert mock_optimize.call_count == 1, "identify_optimized_production_areas must be called exactly once"
assert mock_roads.call_count == 1, "farm_roads_data.get_road_exclusion_union_utm must be called exactly once"
assert mock_floodplain.call_count == 1, "_fetch_floodplain_hydric_union must be called exactly once"
assert mock_water.call_count == 1, "identify_water_system_candidate_zones must be called exactly once"
assert mock_select_water_zone.call_count == 1, "fetch_and_select_optimal_water_zone must be called exactly once"
assert mock_road_corridor.call_count == 1, "identify_road_corridor_candidates must be called exactly once"
print(
    "Every underlying fetch entry point (DEM, optimized production areas, road exclusion, "
    "floodplain/hydric union, water-system candidate zones, selected water zone, selected road "
    "corridor) was called exactly once."
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
    "hydric_floodplain_is_fallback": False,
    "erosion_prone_union": None,
}, (
    "soil_exclusion_unions must carry all 3 keys, with hydric_floodplain_is_fallback holding the "
    "REAL second element _fetch_floodplain_hydric_union() returned (False here, matching the mock's "
    "own return value) -- not silently dropped the way the prior 2-key shape did"
)
roads_call = mock_roads.call_args
assert roads_call.args[0] == boundary_coordinates and roads_call.args[1] is synthetic_dem
assert roads_call.kwargs.get("farm_roads") is None, (
    "farm_roads= was not supplied to build_pipeline_context() in this run -- get_road_exclusion_union_utm() "
    "must receive farm_roads=None (its own self-fetch fallback), not silently defaulted to some other value"
)
floodplain_call = mock_floodplain.call_args
assert floodplain_call.args[0] == boundary_coordinates
assert floodplain_call.args[1] is synthetic_dem
assert floodplain_call.args[2] is ctx.valleys, "must reuse the already-computed valleys, not re-derive them"
assert floodplain_call.args[3] is ctx.boundary_polygon_utm, "must reuse the already-computed boundary polygon"
assert floodplain_call.kwargs.get("soil_components") is None, (
    "soil_components= was not supplied to build_pipeline_context() in this run -- _fetch_floodplain_hydric_"
    "union() must receive soil_components=None (its own self-fetch fallback), not silently defaulted"
)
print(
    "existing_roads and soil_exclusion_unions['hydric_floodplain_union'] carry the real fetched "
    "geometry; the floodplain/hydric fetch reused the same dem/valleys/boundary_polygon_utm "
    "instances already computed above, not re-derived copies. soil_exclusion_unions carries all 3 "
    "keys, with hydric_floodplain_is_fallback holding the real (not dropped) flag. erosion_prone_union "
    "is None -- see pipeline_context.py's own KNOWN LIMITATIONS #3 for why (no shared erosion-prone-"
    "soil union builder currently exists anywhere in this codebase to call)."
)

# --- 5. water_zones reuses dem/boundary_polygon_utm/valleys/production_areas -- ALL four, not just dem ---
#
# identify_water_system_candidate_zones() is run for REAL above (wraps=, not fully mocked away),
# so this is the test that actually proves the redundancy is gone: its own internal self-compute
# fallbacks for valleys/production_areas must NOT fire when both are supplied as overrides.

water_call = mock_water.call_args
assert water_call.args[0] == boundary_coordinates
assert water_call.kwargs["dem"] is synthetic_dem, "water_zones must reuse the already-fetched dem, not fetch a second one"
assert water_call.kwargs["boundary_polygon_utm"] is ctx.boundary_polygon_utm, (
    "water_zones must reuse the already-computed boundary_polygon_utm, not a second self-computed copy"
)
assert water_call.kwargs["valleys"] is ctx.valleys, "water_zones must reuse the already-computed valleys instance"
assert water_call.kwargs["production_areas"] is ctx.production_areas, (
    "water_zones must reuse the already-computed production_areas instance"
)

# The actual proof: water_candidate_zones.py's OWN internal self-compute fallbacks for valleys/
# production_areas (a SEPARATE pair of mocks from mock_delineate/mock_optimize above -- see the
# comment on the `with` block) were never triggered at all.
assert mock_water_zone_delineate.call_count == 0, (
    "delineate_valleys() must NOT be called a second time inside the water-zone step when valleys "
    "was supplied as an override -- this is the redundancy this branch closes"
)
assert mock_water_zone_identify_pa.call_count == 0, (
    "identify_production_areas() must NOT be called inside the water-zone step when production_areas "
    "was supplied as an override -- this is the redundancy this branch closes"
)
# ...and the TOTAL count across the whole build_pipeline_context() call for each: delineate_valleys()
# ran exactly twice (valleys + ridge_lines, both via pc.valley_delineation.delineate_valleys, asserted
# in section 1 above) plus zero more from inside the water-zone step; identify_optimized_production_
# areas() ran exactly once (section 3 above) plus zero calls to the raw identify_production_areas()
# fallback from inside the water-zone step. Neither is "once for context's own fields plus a second
# hidden call inside the water-zone step."
assert mock_delineate.call_count == 2, "delineate_valleys should still total exactly 2 calls overall (valleys + ridge_lines)"
assert mock_optimize.call_count == 1, "identify_optimized_production_areas should still total exactly 1 call overall"

# mock_water_zone_canopy/mock_water_zone_roads confirm the (offline-stubbed) mandatory canopy fetch
# and optional road fetch each ran exactly once too -- real behavior of the real, unmocked function.
assert mock_water_zone_canopy.call_count == 1
assert mock_water_zone_roads.call_count == 1

# fake_patch's polygon_utm (box(0, 0, 10, 10)) is nowhere near this synthetic DEM's real UTM
# coordinates, so no cell clears find_candidate_zones()'s service-distance gate -- water_zones is
# genuinely, correctly empty here. This section isn't testing zone geometry/eligibility correctness
# (test_water_candidate_zones.py and test_water_system_candidate_pipeline.py already cover that with
# real, spatially-coherent fixtures) -- only that the real function runs end-to-end, offline, without
# re-deriving what this context already computed.
assert ctx.water_zones == [], (
    "expected zero water zones: fake_patch's production-area geometry doesn't overlap this DEM's real "
    "coordinates, so nothing clears the service-distance gate -- confirms the real function ran (not a "
    "canned mock reply) without crashing"
)
print(
    "water_zones reuses the already-computed dem/boundary_polygon_utm/valleys/production_areas -- ALL "
    "FOUR, not just dem -- and identify_water_system_candidate_zones()'s own internal delineate_valleys()/"
    "identify_production_areas() self-compute fallbacks were called ZERO times. Total calls across the "
    "whole build_pipeline_context() run: delineate_valleys() x2 (valleys + ridge_lines), "
    "identify_optimized_production_areas() x1 -- not once for this context's own fields plus a second "
    "hidden call inside the water-zone step."
)

# --- 6. selected_water_zone reuses this context's own boundary_polygon_utm/valleys/production_areas ---

assert ctx.selected_water_zone is fake_selected_water_zone
select_water_zone_call = mock_select_water_zone.call_args
assert select_water_zone_call.args[0] == boundary_coordinates
assert select_water_zone_call.kwargs["dem"] is synthetic_dem, (
    "selected_water_zone must reuse the already-fetched dem, not fetch its own"
)
assert select_water_zone_call.kwargs["boundary_polygon_utm"] is ctx.boundary_polygon_utm, (
    "selected_water_zone must reuse the already-computed boundary_polygon_utm, not a second self-computed copy"
)
assert select_water_zone_call.kwargs["valleys"] is ctx.valleys, (
    "selected_water_zone must reuse the already-computed valleys instance"
)
assert select_water_zone_call.kwargs["production_areas"] is ctx.production_areas, (
    "selected_water_zone must reuse the already-computed production_areas instance"
)
print(
    "selected_water_zone reuses this context's own dem/boundary_polygon_utm/valleys/production_areas "
    "instances via fetch_and_select_optimal_water_zone()'s own override params, not self-derived copies."
)

# --- 7. selected_road_corridor reuses dem/boundary_polygon_utm/valleys/production_areas/selected_water_zone, ---
# --- AND this context's own soil_exclusion_unions['hydric_floodplain_union'] rather than a second fetch ---

assert ctx.selected_road_corridor is fake_selected_road_corridor
road_corridor_call = mock_road_corridor.call_args
assert road_corridor_call.args[0] == boundary_coordinates
assert road_corridor_call.kwargs["anchor_lon_lat"] == anchor_lon_lat
assert road_corridor_call.kwargs["dem"] is synthetic_dem, (
    "selected_road_corridor must reuse the already-fetched dem, not fetch its own"
)
assert road_corridor_call.kwargs["boundary_polygon_utm"] is ctx.boundary_polygon_utm, (
    "selected_road_corridor must reuse the already-computed boundary_polygon_utm, not a second self-computed copy"
)
assert road_corridor_call.kwargs["valleys"] is ctx.valleys, (
    "selected_road_corridor must reuse the already-computed valleys instance"
)
assert road_corridor_call.kwargs["production_areas"] is ctx.production_areas, (
    "selected_road_corridor must reuse the already-computed production_areas instance"
)
assert road_corridor_call.kwargs["selected_water_zone"] is ctx.selected_water_zone, (
    "selected_road_corridor must reuse this context's own already-selected water zone, not re-select one"
)
assert road_corridor_call.kwargs["hydric_floodplain_union"] is ctx.soil_exclusion_unions["hydric_floodplain_union"], (
    "identify_road_corridor_candidates() must reuse this context's own already-fetched "
    "hydric_floodplain_union rather than letting road_corridors.py fetch a second one"
)
assert road_corridor_call.kwargs["floodplain_data_is_fallback"] is ctx.soil_exclusion_unions["hydric_floodplain_is_fallback"], (
    "floodplain_data_is_fallback passed to identify_road_corridor_candidates() must match this context's "
    "own real hydric_floodplain_is_fallback flag, not a hardcoded/defaulted value"
)
assert road_corridor_call.kwargs["floodplain_data_is_fallback"] is False, (
    "sanity check for this run's own synthetic scenario: the mocked _fetch_floodplain_hydric_union() "
    "returned (fake_hydric_union, False) above, so False is what should have propagated through here"
)
print(
    "selected_road_corridor reuses this context's own dem/boundary_polygon_utm/valleys/production_areas/"
    "selected_water_zone instances, AND this context's own soil_exclusion_unions['hydric_floodplain_union']"
    " (paired with its real hydric_floodplain_is_fallback flag) rather than letting "
    "identify_road_corridor_candidates() fetch a second, independent floodplain/hydric union."
)

# --- 8. selected_structure_site/tree_zone_patches reuse dem/boundary_polygon_utm/valleys/ ---
# --- production_areas/selected_water_zone/selected_road_corridor/hydric_floodplain_union/ ---
# --- floodplain_data_is_fallback -- THIS module's own two new direct calls, not the nested one ---

solar_call = mock_solar.call_args
assert solar_call.args[0] == boundary_coordinates
assert solar_call.kwargs["anchor_lon_lat"] == anchor_lon_lat
assert solar_call.kwargs["dem"] is synthetic_dem, "selected_structure_site must reuse the already-fetched dem"
assert solar_call.kwargs["boundary_polygon_utm"] is ctx.boundary_polygon_utm
assert solar_call.kwargs["valleys"] is ctx.valleys
assert solar_call.kwargs["production_areas"] is ctx.production_areas
assert solar_call.kwargs["selected_water_zone"] is ctx.selected_water_zone
assert solar_call.kwargs["selected_road_corridor"] is ctx.selected_road_corridor
assert solar_call.kwargs["hydric_floodplain_union"] is ctx.soil_exclusion_unions["hydric_floodplain_union"]
assert solar_call.kwargs["floodplain_data_is_fallback"] is ctx.soil_exclusion_unions["hydric_floodplain_is_fallback"]

tree_zone_call = mock_tree_zone.call_args
assert tree_zone_call.args[0] == boundary_coordinates
assert tree_zone_call.kwargs["anchor_lon_lat"] == anchor_lon_lat
assert tree_zone_call.kwargs["dem"] is synthetic_dem, "tree_zone_candidates must reuse the already-fetched dem"
assert tree_zone_call.kwargs["boundary_polygon_utm"] is ctx.boundary_polygon_utm
assert tree_zone_call.kwargs["valleys"] is ctx.valleys
assert tree_zone_call.kwargs["production_areas"] is ctx.production_areas
assert tree_zone_call.kwargs["selected_water_zone"] is ctx.selected_water_zone
assert tree_zone_call.kwargs["selected_road_corridor"] is ctx.selected_road_corridor
assert tree_zone_call.kwargs["hydric_floodplain_union"] is ctx.soil_exclusion_unions["hydric_floodplain_union"]
assert tree_zone_call.kwargs["floodplain_data_is_fallback"] is ctx.soil_exclusion_unions["hydric_floodplain_is_fallback"]

# mock_solar_optimize/mock_solar_water/mock_solar_road being zero here proves
# identify_solar_candidate_zones()'s OWN top-level self-compute checks
# correctly skipped -- everything it needed was supplied.
assert mock_solar_optimize.call_count == 0, (
    "identify_optimized_production_areas() must NOT be called at identify_solar_candidate_zones()'s own top "
    "level when production_areas was supplied as an override"
)
assert mock_solar_water.call_count == 0, (
    "identify_water_suitability() must NOT be called at identify_solar_candidate_zones()'s own top level "
    "when selected_water_zone was supplied as an override"
)
assert mock_solar_road.call_count == 0, (
    "identify_road_corridor_candidates() must NOT be called at identify_solar_candidate_zones()'s own top "
    "level when selected_road_corridor was supplied as an override"
)
print(
    "selected_structure_site and tree_zone_patches: both of THIS module's own new direct calls "
    "(identify_solar_candidate_zones(), identify_tree_zone_candidates()) reuse this context's own dem/"
    "boundary_polygon_utm/valleys/production_areas/selected_water_zone/selected_road_corridor/"
    "hydric_floodplain_union/floodplain_data_is_fallback instances -- proven both by kwargs identity and by "
    "identify_solar_candidate_zones()'s own top-level identify_optimized_production_areas()/identify_water_"
    "suitability()/identify_road_corridor_candidates() self-compute checks firing ZERO times."
)

# --- 9. THE REAL POINT: total call counts across the WHOLE build_pipeline_context() run ---
#
# Every module-level binding of identify_optimized_production_areas()/identify_water_suitability()/
# identify_road_corridor_candidates() that ANY code reachable from build_pipeline_context() could call is
# mocked and counted above (pc's own direct bindings, water_candidate_zones.py's own, solar_suitability.py's
# own, and tree_zone_candidates.py's own) -- so summing every mock's call_count for a given function is a
# genuine, complete total, not a partial one.
#
# This USED TO measure a real, MEASURED redundancy: identify_solar_candidate_zones()'s own internal, nested
# identify_tree_zone_candidates() call (its own "TREE-ZONE-CANDIDATE exclusion" step) forwarded none of the
# overrides identify_solar_candidate_zones() itself received, so mock_tz_optimize/mock_tz_water/mock_tz_road
# each fired once more than they should have, doubling identify_optimized_production_areas()/identify_road_
# corridor_candidates()'s own total call counts across a single build_pipeline_context() run (see
# pipeline_context.py's own KNOWN LIMITATIONS #4, now marked RESOLVED). A follow-up branch fixed this
# directly inside solar_suitability.py, forwarding boundary_polygon_utm/production_areas/valleys/selected_
# water_zone/selected_road_corridor/hydric_floodplain_union/floodplain_data_is_fallback into that nested
# call -- the assertions below now prove the fix: every one of mock_tz_optimize/mock_tz_water/mock_tz_road
# stays at ZERO, same as mock_solar_optimize/mock_solar_water/mock_solar_road in section 8 above, so the
# totals below are genuinely back to "still exactly 1", same as sections 4-7's own dedup.
total_optimize_calls = mock_optimize.call_count + mock_solar_optimize.call_count + mock_tz_optimize.call_count
total_road_corridor_calls = mock_road_corridor.call_count + mock_solar_road.call_count + mock_tz_road.call_count
assert total_optimize_calls == 1, (
    f"MEASURED total identify_optimized_production_areas() calls across the whole build_pipeline_context() "
    f"run: {total_optimize_calls} (expected 1). mock_tz_optimize == {mock_tz_optimize.call_count} proves "
    f"identify_solar_candidate_zones()'s own internal nested identify_tree_zone_candidates() call no longer "
    f"self-computes production_areas -- if this regresses back to 2, the nested-call override forwarding "
    f"fixed in solar_suitability.py (see pipeline_context.py's own KNOWN LIMITATIONS #4) broke again."
)
assert total_road_corridor_calls == 1, (
    f"MEASURED total identify_road_corridor_candidates() calls across the whole build_pipeline_context() run: "
    f"{total_road_corridor_calls} (expected 1). mock_tz_road == {mock_tz_road.call_count} proves the same "
    f"fix holds for selected_road_corridor -- see the identify_optimized_production_areas() assertion above."
)
# identify_water_suitability(): fetch_and_select_optimal_water_zone (pc's own direct call, section 6 above) is
# a fully canned return_value mock, same as every prior section in this file -- its own internal call to
# identify_water_suitability() is not exercised here, unchanged by this branch, so it contributes 0 to this
# specific count (not because the fix doesn't apply to it, but because this mock never lets the real function
# run at all). mock_solar_water/mock_tz_water measure whether solar/tree_zone's own wiring introduces any
# NEW calls beyond that -- both should now be 0.
assert mock_solar_water.call_count + mock_tz_water.call_count == 0, (
    f"MEASURED new identify_water_suitability() calls introduced by wiring in selected_structure_site/"
    f"tree_zone_patches: {mock_solar_water.call_count + mock_tz_water.call_count} (expected 0, not 1 -- if "
    f"nonzero, the nested-call override-forwarding fix in solar_suitability.py regressed for selected_water_"
    f"zone specifically)."
)
print(
    "MEASURED (not assumed) total call counts across the WHOLE build_pipeline_context() run: "
    f"identify_optimized_production_areas() x{total_optimize_calls}, identify_road_corridor_candidates() "
    f"x{total_road_corridor_calls}, and ZERO new identify_water_suitability() calls introduced by wiring in "
    "selected_structure_site/tree_zone_patches. Adding these two fields stays at 'still exactly 1', same as "
    "sections 4-7's own dedup -- identify_solar_candidate_zones()'s own internal nested identify_tree_zone_"
    "candidates() call now correctly forwards every override it itself received, closing the redundancy "
    "documented (and now marked RESOLVED) in pipeline_context.py's own KNOWN LIMITATIONS #4."
)

# --- boundary_polygon_utm sanity check ---

assert ctx.dem is synthetic_dem
assert isinstance(ctx.boundary_polygon_utm, Polygon)
expected_area = (COLS * RESOLUTION) * (ROWS * RESOLUTION)
assert abs(ctx.boundary_polygon_utm.area - expected_area) < 1.0, "boundary_polygon_utm should match the DEM's own footprint"

# --- 10. floodplain_data_is_fallback threads the REAL flag through, not a hardcoded/defaulted one ---
#
# Every assertion above ran against a synthetic scenario where _fetch_floodplain_hydric_union() happens
# to return is_fallback=False -- on its own, that would never catch a regression that hardcoded/defaulted
# floodplain_data_is_fallback to False regardless of the real flag. This dedicated second
# build_pipeline_context() run forces the floodplain fetch into its fallback path (is_fallback=True) and
# re-proves the same threading with the OPPOSITE value, so a hardcoded-False regression would fail here.

fake_fallback_hydric_union = box(20, 20, 30, 30)
fake_selected_water_zone_fallback_case = {"type": "Feature", "id": "water-zone-fallback-case"}
fake_selected_road_corridor_fallback_case = {"type": "Feature", "id": "road-corridor-fallback-case"}
fake_water_zones_result_fallback_case = {"zones_geojson": {"type": "FeatureCollection", "features": []}}
fake_road_corridor_result_fallback_case = {
    "zones_geojson": {"type": "FeatureCollection", "features": []},
    "all_scored_candidates": [],
    "selected_road_corridor": fake_selected_road_corridor_fallback_case,
}
fake_solar_result_fallback_case = {
    "zones_geojson": {"type": "FeatureCollection", "features": []},
    "all_scored_candidates": [],
    "selected_structure_site": {"type": "Feature", "id": "structure-site-fallback-case"},
}
fake_tree_zone_result_fallback_case = {
    "zones_geojson": {"type": "FeatureCollection", "features": []},
    "search_space_geojson": {"type": "FeatureCollection", "features": []},
    "search_space_acres": 0.0,
    "claimed_acres": 0.0,
    "boundary_acres": 0.0,
    "patches": [],
}

# solar_suitability.identify_solar_candidate_zones()/tree_zone_candidates.identify_tree_zone_candidates()
# are fully mocked (return_value, not wraps=) in THIS second run -- this section is testing only the
# floodplain-fallback flag's own threading through soil_exclusion_unions/identify_road_corridor_candidates,
# same scope as before this branch added these two fields; running them for real here would need the same
# offline canopy/geometry stubbing section 8/9 above already do, for no benefit to what this section checks.
with mock_patch.object(pc.dem_data, "get_dem_for_boundary", return_value=synthetic_dem), \
     mock_patch.object(pc.valley_delineation, "delineate_valleys", wraps=pc.valley_delineation.delineate_valleys), \
     mock_patch.object(
         pc.production_area_ceiling, "identify_optimized_production_areas", return_value=fake_optimized_result
     ), \
     mock_patch.object(pc.farm_roads_data, "get_road_exclusion_union_utm", return_value=fake_existing_roads_union), \
     mock_patch.object(
         pc.road_corridors, "_fetch_floodplain_hydric_union", return_value=(fake_fallback_hydric_union, True)
     ) as mock_floodplain_fallback_case, \
     mock_patch.object(
         pc.water_candidate_zones,
         "identify_water_system_candidate_zones",
         return_value=fake_water_zones_result_fallback_case,
     ), \
     mock_patch.object(
         pc, "fetch_and_select_optimal_water_zone", return_value=fake_selected_water_zone_fallback_case
     ), \
     mock_patch.object(
         pc.road_corridors,
         "identify_road_corridor_candidates",
         return_value=fake_road_corridor_result_fallback_case,
     ) as mock_road_corridor_fallback_case, \
     mock_patch.object(
         pc, "identify_solar_candidate_zones", return_value=fake_solar_result_fallback_case
     ) as mock_solar_fallback_case, \
     mock_patch.object(
         pc, "identify_tree_zone_candidates", return_value=fake_tree_zone_result_fallback_case
     ) as mock_tree_zone_fallback_case:
    ctx_fallback_case = pc.build_pipeline_context(boundary_coordinates, anchor_lon_lat)

assert mock_floodplain_fallback_case.call_count == 1

assert ctx_fallback_case.soil_exclusion_unions == {
    "hydric_floodplain_union": fake_fallback_hydric_union,
    "hydric_floodplain_is_fallback": True,
    "erosion_prone_union": None,
}, "soil_exclusion_unions['hydric_floodplain_is_fallback'] must be True when the floodplain fetch itself fell back"

road_corridor_fallback_call = mock_road_corridor_fallback_case.call_args
assert road_corridor_fallback_call.kwargs["hydric_floodplain_union"] is fake_fallback_hydric_union
assert road_corridor_fallback_call.kwargs["floodplain_data_is_fallback"] is True, (
    "identify_road_corridor_candidates() must receive the REAL is_fallback flag (True, forced here) from "
    "this context's own floodplain fetch -- a hardcoded/defaulted False would fail this assertion even "
    "though every other assertion in this file (built against the is_fallback=False synthetic case) would "
    "still pass, which is exactly the regression this dedicated second run exists to catch"
)
assert ctx_fallback_case.selected_road_corridor is fake_selected_road_corridor_fallback_case

solar_fallback_call = mock_solar_fallback_case.call_args
assert solar_fallback_call.kwargs["hydric_floodplain_union"] is fake_fallback_hydric_union
assert solar_fallback_call.kwargs["floodplain_data_is_fallback"] is True, (
    "identify_solar_candidate_zones() must receive the REAL is_fallback flag too, same as identify_road_"
    "corridor_candidates() above"
)
tree_zone_fallback_call = mock_tree_zone_fallback_case.call_args
assert tree_zone_fallback_call.kwargs["hydric_floodplain_union"] is fake_fallback_hydric_union
assert tree_zone_fallback_call.kwargs["floodplain_data_is_fallback"] is True, (
    "identify_tree_zone_candidates() must receive the REAL is_fallback flag too, same as identify_road_"
    "corridor_candidates() above"
)
assert ctx_fallback_case.selected_structure_site == fake_solar_result_fallback_case["selected_structure_site"]
assert ctx_fallback_case.tree_zone_patches == fake_tree_zone_result_fallback_case["patches"]
print(
    "floodplain_data_is_fallback threads the REAL flag (forced True here, the opposite of every assertion "
    "above) through to identify_road_corridor_candidates(), identify_solar_candidate_zones(), identify_tree_"
    "zone_candidates(), and soil_exclusion_unions alike -- not a hardcoded/defaulted value."
)

# --- 11. selected_structure_site/tree_zone_patches hold real, correctly-shaped values ---
#
# Against section 8/9's own synthetic run (the real, non-mocked identify_solar_candidate_zones()/
# identify_tree_zone_candidates() calls): tree_zone_patches comes back non-empty and correctly shaped.
# selected_structure_site genuinely comes back None on this fixture -- NOT a bug or a swallowed exception
# (traced separately: identify_solar_candidate_zones()'s own all_scored_candidates list is genuinely empty,
# not just its selected_structure_site) -- this synthetic terrain's own column-wise gradient (4.0m per
# 5m RESOLUTION column, engineered steep specifically so delineate_valleys()/its inversion trace real valley/
# ridge geometry -- see this file's own module docstring) is roughly 80% slope almost everywhere, an order of
# magnitude past solar_suitability.MAX_SOLAR_SLOPE_PCT (20%) -- so every DEM cell genuinely fails the slope
# gate, the same "uniform terrain -> no water zone/road corridor" kind of finding sections 5/6/7 above already
# hit for the same underlying reason (this fixture optimizes for valley/ridge tracing, not for producing solar
# or tree-zone candidates everywhere).
assert ctx.selected_structure_site is None, (
    "expected no solar candidate on this synthetic fixture -- see comment above: the terrain built for "
    "delineate_valleys()/ridge_lines tracing is far too steep (~80%) everywhere to clear solar_suitability."
    "MAX_SOLAR_SLOPE_PCT (20%), a genuine, DEM-driven exclusion, not a swallowed error"
)
assert ctx.tree_zone_patches, "expected at least one tree-zone candidate patch on the synthetic fixture"
assert isinstance(ctx.tree_zone_patches, list)
for _patch in ctx.tree_zone_patches:
    assert isinstance(_patch, dict)
    assert "id" in _patch and "polygon_utm" in _patch, (
        "tree_zone_patches entries must carry score_tree_search_space()'s own real per-patch shape"
    )
print(
    f"selected_structure_site is genuinely None on this synthetic fixture (terrain slope ~80%, far past "
    f"MAX_SOLAR_SLOPE_PCT's 20% ceiling -- a real DEM-driven exclusion, not an error). tree_zone_patches "
    f"holds {len(ctx.tree_zone_patches)} real, correctly-shaped patch(es)."
)

# --- 12. dem= override: a caller-supplied dem skips the self-fetch entirely ---
#
# build_pipeline_context() previously had no dem parameter at all -- always called dem_data.
# get_dem_for_boundary() itself, even for a caller (render_layout_map.fetch_layout_layers()) that
# already had one. This dedicated third run supplies dem= directly and proves two things: (a)
# dem_data.get_dem_for_boundary is never called (call_count == 0, not just "still 1"), and (b) the
# EXACT object passed in is what ends up on the returned context AND gets threaded down to a
# downstream call (identify_road_corridor_candidates(), picked arbitrarily -- every call in build_
# pipeline_context() shares the same local `dem` variable, so one representative identity check is
# sufficient). dem_override is a fresh dict (not the same object as the module's own `synthetic_dem`
# used everywhere else in this file, though it carries the identical real array/crs/etc.) specifically
# so an `is` check here cannot pass by accident.
dem_override = dict(synthetic_dem)
fake_selected_road_corridor_dem_case = {
    "type": "Feature",
    "id": "road-corridor-dem-override-case",
    "cell_footprint_polygon_utm": box(0, 0, 10, 10),
    "cells": [(0, 0)],
}
fake_road_corridor_result_dem_case = {
    "zones_geojson": {"type": "FeatureCollection", "features": []},
    "all_scored_candidates": [],
    "selected_road_corridor": fake_selected_road_corridor_dem_case,
}
fake_solar_result_dem_case = {
    "zones_geojson": {"type": "FeatureCollection", "features": []},
    "all_scored_candidates": [],
    "selected_structure_site": None,
}
fake_tree_zone_result_dem_case = {
    "zones_geojson": {"type": "FeatureCollection", "features": []},
    "search_space_geojson": {"type": "FeatureCollection", "features": []},
    "search_space_acres": 0.0,
    "claimed_acres": 0.0,
    "boundary_acres": 0.0,
    "patches": [],
}

with mock_patch.object(
    pc.dem_data, "get_dem_for_boundary", side_effect=AssertionError("must not be called when dem= is supplied")
) as mock_get_dem_dem_case, \
     mock_patch.object(pc.valley_delineation, "delineate_valleys", return_value=[]), \
     mock_patch.object(pc.production_area_ceiling, "identify_optimized_production_areas", return_value=fake_optimized_result), \
     mock_patch.object(pc.farm_roads_data, "get_road_exclusion_union_utm", return_value=fake_existing_roads_union), \
     mock_patch.object(pc.road_corridors, "_fetch_floodplain_hydric_union", return_value=(fake_hydric_union, False)), \
     mock_patch.object(
         pc.water_candidate_zones,
         "identify_water_system_candidate_zones",
         return_value={"zones_geojson": {"type": "FeatureCollection", "features": []}},
     ), \
     mock_patch.object(pc, "fetch_and_select_optimal_water_zone", return_value=fake_selected_water_zone), \
     mock_patch.object(
         pc.road_corridors, "identify_road_corridor_candidates", return_value=fake_road_corridor_result_dem_case
     ) as mock_road_corridor_dem_case, \
     mock_patch.object(pc, "identify_solar_candidate_zones", return_value=fake_solar_result_dem_case), \
     mock_patch.object(pc, "identify_tree_zone_candidates", return_value=fake_tree_zone_result_dem_case):
    ctx_dem_case = pc.build_pipeline_context(boundary_coordinates, anchor_lon_lat, dem=dem_override)

assert mock_get_dem_dem_case.call_count == 0, (
    "dem_data.get_dem_for_boundary must NOT be called at all when a caller supplies dem= -- if this fires, "
    "the side_effect above raises AssertionError instead of returning a fake DEM, so a regression here "
    "fails loudly rather than silently passing with a self-fetched DEM"
)
assert ctx_dem_case.dem is dem_override, (
    "build_pipeline_context()'s returned context.dem must be the EXACT object the caller passed in, not a "
    "copy or a re-fetched one"
)
assert mock_road_corridor_dem_case.call_args.kwargs["dem"] is dem_override, (
    "the caller-supplied dem must thread down to every downstream call the same way a self-fetched one "
    "always did -- checked here via identify_road_corridor_candidates() as one representative call"
)
print("dem= override: get_dem_for_boundary is never called, and the exact caller-supplied object reaches "
      "both context.dem and every downstream call.")

# --- 13. existing no-override behavior is unchanged ---
#
# Section 1's own original run (no dem= argument at all) already re-proves this implicitly -- it still
# calls build_pipeline_context(boundary_coordinates, anchor_lon_lat) with no third argument, and section
# 1's mock_get_dem.call_count == 1 assertion already confirms the default (dem=None) path still self-fetches
# exactly once, unchanged by this addendum. No new run needed here -- flagged explicitly so this file's own
# module docstring accounting stays honest about what section 1 already covers.
print("existing no-override callers (dem= omitted entirely) are unaffected -- see section 1's own "
      "mock_get_dem.call_count == 1 assertion, re-run unchanged by this addendum.")

# --- 14. boundary_polygon_utm= override: a caller-supplied polygon skips the self-compute entirely, ---
# --- AND reaches every internal use site, not just one ---
#
# _boundary_polygon_utm() (this module's own warp_transform+Polygon helper) is stubbed to raise if
# called at all, same "side_effect=AssertionError" trick section 12 uses for dem_data.get_dem_for_
# boundary -- a regression that ignores the override and recomputes anyway fails loudly here rather
# than silently passing. Every direct call build_pipeline_context() makes that takes a
# boundary_polygon_utm kwarg/positional arg is captured and identity-checked below: the floodplain
# fetch (positional), water_zones, selected_water_zone, selected_road_corridor, selected_structure_
# site, and tree_zone_patches -- covering every internal use site listed in pipeline_context.py's own
# field notes, not just the returned context.boundary_polygon_utm.
boundary_polygon_utm_override = box(1000, 1000, 2000, 2000)
fake_selected_road_corridor_bpu_case = {
    "type": "Feature",
    "id": "road-corridor-bpu-override-case",
    "cell_footprint_polygon_utm": box(0, 0, 10, 10),
    "cells": [(0, 0)],
}
fake_road_corridor_result_bpu_case = {
    "zones_geojson": {"type": "FeatureCollection", "features": []},
    "all_scored_candidates": [],
    "selected_road_corridor": fake_selected_road_corridor_bpu_case,
}
fake_solar_result_bpu_case = {
    "zones_geojson": {"type": "FeatureCollection", "features": []},
    "all_scored_candidates": [],
    "selected_structure_site": None,
}
fake_tree_zone_result_bpu_case = {
    "zones_geojson": {"type": "FeatureCollection", "features": []},
    "search_space_geojson": {"type": "FeatureCollection", "features": []},
    "search_space_acres": 0.0,
    "claimed_acres": 0.0,
    "boundary_acres": 0.0,
    "patches": [],
}

with mock_patch.object(pc.dem_data, "get_dem_for_boundary", return_value=synthetic_dem), \
     mock_patch.object(
         pc, "_boundary_polygon_utm", side_effect=AssertionError("must not be called when boundary_polygon_utm= is supplied")
     ) as mock_bpu_helper, \
     mock_patch.object(pc.valley_delineation, "delineate_valleys", return_value=[]), \
     mock_patch.object(pc.production_area_ceiling, "identify_optimized_production_areas", return_value=fake_optimized_result), \
     mock_patch.object(pc.farm_roads_data, "get_road_exclusion_union_utm", return_value=fake_existing_roads_union), \
     mock_patch.object(
         pc.road_corridors, "_fetch_floodplain_hydric_union", return_value=(fake_hydric_union, False)
     ) as mock_floodplain_bpu_case, \
     mock_patch.object(
         pc.water_candidate_zones,
         "identify_water_system_candidate_zones",
         return_value={"zones_geojson": {"type": "FeatureCollection", "features": []}},
     ) as mock_water_bpu_case, \
     mock_patch.object(
         pc, "fetch_and_select_optimal_water_zone", return_value=fake_selected_water_zone
     ) as mock_select_water_zone_bpu_case, \
     mock_patch.object(
         pc.road_corridors, "identify_road_corridor_candidates", return_value=fake_road_corridor_result_bpu_case
     ) as mock_road_corridor_bpu_case, \
     mock_patch.object(
         pc, "identify_solar_candidate_zones", return_value=fake_solar_result_bpu_case
     ) as mock_solar_bpu_case, \
     mock_patch.object(
         pc, "identify_tree_zone_candidates", return_value=fake_tree_zone_result_bpu_case
     ) as mock_tree_zone_bpu_case:
    ctx_bpu_case = pc.build_pipeline_context(
        boundary_coordinates, anchor_lon_lat, boundary_polygon_utm=boundary_polygon_utm_override
    )

assert mock_bpu_helper.call_count == 0, (
    "_boundary_polygon_utm() must NOT be called at all when a caller supplies boundary_polygon_utm= -- if "
    "this fires, the side_effect above raises AssertionError instead of returning a polygon, so a "
    "regression here fails loudly rather than silently passing with a self-computed one"
)
assert ctx_bpu_case.boundary_polygon_utm is boundary_polygon_utm_override, (
    "build_pipeline_context()'s returned context.boundary_polygon_utm must be the EXACT object the caller "
    "passed in, not a copy or a re-derived one"
)

floodplain_bpu_call = mock_floodplain_bpu_case.call_args
assert floodplain_bpu_call.args[3] is boundary_polygon_utm_override, (
    "_fetch_floodplain_hydric_union() must receive the caller-supplied boundary_polygon_utm, not a "
    "self-computed one"
)
assert mock_water_bpu_case.call_args.kwargs["boundary_polygon_utm"] is boundary_polygon_utm_override, (
    "identify_water_system_candidate_zones() must receive the caller-supplied boundary_polygon_utm"
)
assert mock_select_water_zone_bpu_case.call_args.kwargs["boundary_polygon_utm"] is boundary_polygon_utm_override, (
    "fetch_and_select_optimal_water_zone() must receive the caller-supplied boundary_polygon_utm"
)
assert mock_road_corridor_bpu_case.call_args.kwargs["boundary_polygon_utm"] is boundary_polygon_utm_override, (
    "identify_road_corridor_candidates() must receive the caller-supplied boundary_polygon_utm"
)
assert mock_solar_bpu_case.call_args.kwargs["boundary_polygon_utm"] is boundary_polygon_utm_override, (
    "identify_solar_candidate_zones() must receive the caller-supplied boundary_polygon_utm"
)
assert mock_tree_zone_bpu_case.call_args.kwargs["boundary_polygon_utm"] is boundary_polygon_utm_override, (
    "identify_tree_zone_candidates() must receive the caller-supplied boundary_polygon_utm"
)
print(
    "boundary_polygon_utm= override: _boundary_polygon_utm() is never called, and the exact caller-supplied "
    "polygon reaches context.boundary_polygon_utm AND every internal use site (floodplain/hydric fetch, "
    "water_zones, selected_water_zone, selected_road_corridor, selected_structure_site, tree_zone_patches) -- "
    "not just one of them."
)

# --- 15. existing no-override behavior for boundary_polygon_utm is unchanged ---
#
# Section 1's own original run (no boundary_polygon_utm= argument at all) already re-proves this: it still
# calls build_pipeline_context(boundary_coordinates, anchor_lon_lat) with no boundary_polygon_utm argument,
# and ctx.boundary_polygon_utm there is the real, self-computed Polygon checked against the DEM's own
# footprint area (see the "boundary_polygon_utm sanity check" section above) -- unaffected by this addendum.
print("existing no-override callers (boundary_polygon_utm= omitted entirely) are unaffected -- see the "
      "'boundary_polygon_utm sanity check' section above, re-run unchanged by this addendum.")

# --- 16. soil_components=/farm_roads= overrides: caller-supplied values reach ---
# --- get_road_exclusion_union_utm()/_fetch_floodplain_hydric_union() correctly, ---
# --- NOT re-derived -- this function is a pure passthrough for both (it never ---
# --- self-fetches this data itself, see KNOWN LIMITATIONS #5), so this section ---
# --- proves identity through the wiring, not a self-compute-skip on THIS function ---
#
# The real self-compute skip these two overrides ultimately cause (get_farm_roads_for_boundary()/
# get_soil_data_for_polygon() never firing) is proven directly against the two functions that own
# those fetches in test_farm_roads_data.py and test_floodplain_union_scope.py -- this section only
# proves build_pipeline_context() itself threads the caller-supplied values through correctly.
fake_soil_components_override = [{"mukey": "999999", "comppct_r": 90, "hydricrating": "Yes"}]
fake_farm_roads_override = [{"name": "Override Test Rd", "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}}]

with mock_patch.object(pc.dem_data, "get_dem_for_boundary", return_value=synthetic_dem), \
     mock_patch.object(pc.valley_delineation, "delineate_valleys", return_value=[]), \
     mock_patch.object(pc.production_area_ceiling, "identify_optimized_production_areas", return_value=fake_optimized_result), \
     mock_patch.object(
         pc.farm_roads_data, "get_road_exclusion_union_utm", return_value=fake_existing_roads_union
     ) as mock_roads_override_case, \
     mock_patch.object(
         pc.road_corridors, "_fetch_floodplain_hydric_union", return_value=(fake_hydric_union, False)
     ) as mock_floodplain_override_case, \
     mock_patch.object(
         pc.water_candidate_zones,
         "identify_water_system_candidate_zones",
         return_value={"zones_geojson": {"type": "FeatureCollection", "features": []}},
     ), \
     mock_patch.object(pc, "fetch_and_select_optimal_water_zone", return_value=fake_selected_water_zone), \
     mock_patch.object(pc.road_corridors, "identify_road_corridor_candidates", return_value=fake_road_corridor_result), \
     mock_patch.object(pc, "identify_solar_candidate_zones", return_value=fake_solar_result_dem_case), \
     mock_patch.object(pc, "identify_tree_zone_candidates", return_value=fake_tree_zone_result_dem_case):
    ctx_override_case = pc.build_pipeline_context(
        boundary_coordinates,
        anchor_lon_lat,
        soil_components=fake_soil_components_override,
        farm_roads=fake_farm_roads_override,
    )

assert mock_roads_override_case.call_args.kwargs["farm_roads"] is fake_farm_roads_override, (
    "get_road_exclusion_union_utm() must receive the exact caller-supplied farm_roads= object, not a copy "
    "or a re-derived one"
)
assert mock_floodplain_override_case.call_args.kwargs["soil_components"] is fake_soil_components_override, (
    "_fetch_floodplain_hydric_union() must receive the exact caller-supplied soil_components= object, not a "
    "copy or a re-derived one"
)
print(
    "soil_components=/farm_roads= overrides: the exact caller-supplied objects reach get_road_exclusion_"
    "union_utm()/_fetch_floodplain_hydric_union() as kwargs, not re-derived copies -- build_pipeline_"
    "context() itself never self-fetches either (see KNOWN LIMITATIONS #5); the actual self-compute-skip "
    "these overrides cause is proven directly against those two functions in test_farm_roads_data.py and "
    "test_floodplain_union_scope.py."
)

# --- 17. water_features=/soil_geometries= overrides: caller-supplied values reach ---
# --- _fetch_floodplain_hydric_union() correctly, closing the last two pieces of ---
# --- KNOWN LIMITATIONS #5 (now RESOLVED) -- same pure-passthrough identity-check ---
# --- pattern as section 16 above. The actual self-compute-skip these two overrides ---
# --- cause (get_water_features_for_boundary()/get_soil_geometries_for_polygon() never ---
# --- firing) is proven directly against _fetch_floodplain_hydric_union() itself in ---
# --- test_floodplain_union_scope.py -- this section only proves build_pipeline_context() ---
# --- itself threads the caller-supplied values through correctly.
fake_water_features_override = {"streams": [], "water_bodies": []}
fake_soil_geometries_override = {"999999": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}}

with mock_patch.object(pc.dem_data, "get_dem_for_boundary", return_value=synthetic_dem), \
     mock_patch.object(pc.valley_delineation, "delineate_valleys", return_value=[]), \
     mock_patch.object(pc.production_area_ceiling, "identify_optimized_production_areas", return_value=fake_optimized_result), \
     mock_patch.object(pc.farm_roads_data, "get_road_exclusion_union_utm", return_value=fake_existing_roads_union), \
     mock_patch.object(
         pc.road_corridors, "_fetch_floodplain_hydric_union", return_value=(fake_hydric_union, False)
     ) as mock_floodplain_water_soil_geom_case, \
     mock_patch.object(
         pc.water_candidate_zones,
         "identify_water_system_candidate_zones",
         return_value={"zones_geojson": {"type": "FeatureCollection", "features": []}},
     ), \
     mock_patch.object(pc, "fetch_and_select_optimal_water_zone", return_value=fake_selected_water_zone), \
     mock_patch.object(pc.road_corridors, "identify_road_corridor_candidates", return_value=fake_road_corridor_result), \
     mock_patch.object(pc, "identify_solar_candidate_zones", return_value=fake_solar_result_dem_case), \
     mock_patch.object(pc, "identify_tree_zone_candidates", return_value=fake_tree_zone_result_dem_case):
    ctx_water_soil_geom_case = pc.build_pipeline_context(
        boundary_coordinates,
        anchor_lon_lat,
        water_features=fake_water_features_override,
        soil_geometries=fake_soil_geometries_override,
    )

assert mock_floodplain_water_soil_geom_case.call_args.kwargs["water_features"] is fake_water_features_override, (
    "_fetch_floodplain_hydric_union() must receive the exact caller-supplied water_features= object, not a "
    "copy or a re-derived one"
)
assert mock_floodplain_water_soil_geom_case.call_args.kwargs["soil_geometries"] is fake_soil_geometries_override, (
    "_fetch_floodplain_hydric_union() must receive the exact caller-supplied soil_geometries= object, not a "
    "copy or a re-derived one"
)
print(
    "water_features=/soil_geometries= overrides: the exact caller-supplied objects reach "
    "_fetch_floodplain_hydric_union() as kwargs, not re-derived copies -- build_pipeline_context() itself "
    "never self-fetches either (see KNOWN LIMITATIONS #5, now RESOLVED); the actual self-compute-skip these "
    "overrides cause is proven directly against _fetch_floodplain_hydric_union() itself in "
    "test_floodplain_union_scope.py."
)

# --- Regression: canopy_height omitted (the pre-existing call signature) -- every accepting call ---
# --- still receives an explicit None (its own self-fetch default), same as before canopy_height= ---
# --- existed on build_pipeline_context() at all. Reuses the very first (fully offline-mocked) run ---
# --- from section 1 above -- optimize_call/mock_water/mock_select_water_zone/mock_solar/mock_tree_zone ---
# --- were all captured from that SAME run, before this branch added the canopy_height parameter.
assert optimize_call.kwargs.get("canopy_height") is None
assert mock_water.call_args.kwargs.get("canopy_height") is None
assert mock_select_water_zone.call_args.kwargs.get("canopy_height") is None
assert mock_solar.call_args.kwargs.get("canopy_height") is None
assert mock_tree_zone.call_args.kwargs.get("canopy_height") is None
print(
    "Regression: canopy_height omitted reaches identify_optimized_production_areas()/identify_water_system_"
    "candidate_zones()/fetch_and_select_optimal_water_zone()/identify_solar_candidate_zones()/identify_tree_"
    "zone_candidates() as an explicit None -- each one's own pre-existing self-fetch-canopy default -- so a "
    "caller that doesn't supply an override sees zero behavior change."
)

# --- 18. canopy_height= override: reaches every accepting internal call as the exact caller-supplied ---
# --- object (identity-checked, same as section 17's pattern), AND -- run for REAL here, NOT the ---
# --- _fake_clean_canopy_mask stand-in sections 1-16 use elsewhere in this file -- causes production_area. ---
# --- get_canopy_height_for_boundary() to be called ZERO times across the WHOLE build_pipeline_context() ---
# --- run. identify_water_system_candidate_zones()/identify_solar_candidate_zones()/identify_tree_zone_ ---
# --- candidates() are left real/wraps= below specifically so their own internal canopy gates (each ---
# --- resolving to production_area's shared get_required_tree_root_zone_mask_utm()/_fetch_tree_root_zone_ ---
# --- mask_utm(), per that function's own docstring) genuinely run and genuinely honor the override -- a ---
# --- fully-mocked identify_*() call would never reach that gate at all, making a "zero fetches" assertion ---
# --- vacuous. Uses the SAME CanopyOverrideProbe every other canopy-override caller test in this session ---
# --- uses (test_production_area_ceiling.py, test_water_suitability.py, test_solar_suitability.py, etc.) ---
# --- patched once, at production_area's own module bindings, so it observes every path into the shared ---
# --- fetch no matter how deeply nested the reaching caller is. identify_optimized_production_areas()/ ---
# --- fetch_and_select_optimal_water_zone() stay fully mocked here (their own real-run canopy proof ---
# --- already lives in test_production_area_ceiling.py/test_water_suitability.py) -- only identity-checked. ---
# --- road_corridors.identify_road_corridor_candidates() has no canopy gate at all (see Step 0 of this ---
# --- branch's own scoping), so it is excluded from this section entirely -- nothing to forward there.
from _canopy_override_probe import CanopyOverrideProbe, clean_canopy_for  # noqa: E402

canopy_override = clean_canopy_for(synthetic_dem)

with ExitStack() as _stack:
    _enter = _stack.enter_context
    _enter(mock_patch.object(pc.dem_data, "get_dem_for_boundary", return_value=synthetic_dem))
    _enter(mock_patch.object(pc.valley_delineation, "delineate_valleys", return_value=[]))
    mock_optimize_canopy = _enter(
        mock_patch.object(
            pc.production_area_ceiling, "identify_optimized_production_areas", return_value=fake_optimized_result
        )
    )
    _enter(mock_patch.object(pc.farm_roads_data, "get_road_exclusion_union_utm", return_value=fake_existing_roads_union))
    _enter(mock_patch.object(pc.road_corridors, "_fetch_floodplain_hydric_union", return_value=(fake_hydric_union, False)))

    # identify_water_system_candidate_zones: left real/wraps= -- UNLIKE the main run above, its own
    # internal get_required_tree_root_zone_mask_utm binding is NOT stubbed with _fake_clean_canopy_mask,
    # so it genuinely reaches production_area's real gate with the supplied override.
    mock_water_canopy_case = _enter(
        mock_patch.object(
            pc.water_candidate_zones,
            "identify_water_system_candidate_zones",
            wraps=pc.water_candidate_zones.identify_water_system_candidate_zones,
        )
    )
    _enter(mock_patch.object(pc.water_candidate_zones, "_fetch_road_exclusion_union_utm", return_value=None))
    _enter(mock_patch.object(pc.water_candidate_zones, "delineate_valleys"))
    _enter(mock_patch.object(pc.water_candidate_zones, "identify_production_areas"))

    mock_select_water_zone_canopy = _enter(
        mock_patch.object(pc, "fetch_and_select_optimal_water_zone", return_value=fake_selected_water_zone)
    )
    _enter(mock_patch.object(pc.road_corridors, "identify_road_corridor_candidates", return_value=fake_road_corridor_result))

    # solar/tree_zone: left real/wraps= below, same as the main run -- their own canopy gates are NOT
    # stubbed here either, for the same reason as water_candidate_zones above.
    mock_solar_canopy_case = _enter(
        mock_patch.object(pc, "identify_solar_candidate_zones", wraps=pc.identify_solar_candidate_zones)
    )
    mock_tree_zone_canopy_case = _enter(
        mock_patch.object(pc, "identify_tree_zone_candidates", wraps=pc.identify_tree_zone_candidates)
    )

    # solar_suitability.py's/tree_zone_candidates.py's OWN self-compute-fallback bindings for
    # production/water/road -- mocked so they never fire a SEPARATE, un-overridden path (already proven
    # zero calls in section 8 above); their own canopy gate bindings are deliberately left untouched.
    _enter(
        mock_patch.object(solar_suitability, "identify_optimized_production_areas", return_value=fake_optimized_result)
    )
    _enter(
        mock_patch.object(
            solar_suitability, "identify_water_suitability", return_value={"selected_water_zone": fake_selected_water_zone}
        )
    )
    _enter(mock_patch.object(solar_suitability, "identify_road_corridor_candidates", return_value=fake_road_corridor_result))
    _enter(
        mock_patch.object(tree_zone_candidates, "identify_optimized_production_areas", return_value=fake_optimized_result)
    )
    _enter(
        mock_patch.object(
            tree_zone_candidates, "identify_water_suitability", return_value={"selected_water_zone": fake_selected_water_zone}
        )
    )
    _enter(
        mock_patch.object(tree_zone_candidates, "identify_road_corridor_candidates", return_value=fake_road_corridor_result)
    )

    with CanopyOverrideProbe() as probe:
        ctx_canopy_case = pc.build_pipeline_context(
            boundary_coordinates,
            anchor_lon_lat,
            canopy_height=canopy_override,
        )

probe.assert_override_used(canopy_override, "build_pipeline_context()")

assert mock_optimize_canopy.call_args.kwargs["canopy_height"] is canopy_override, (
    "identify_optimized_production_areas() must receive the exact caller-supplied canopy_height= object"
)
assert mock_select_water_zone_canopy.call_args.kwargs["canopy_height"] is canopy_override, (
    "fetch_and_select_optimal_water_zone() must receive the exact caller-supplied canopy_height= object"
)
assert mock_water_canopy_case.call_args.kwargs["canopy_height"] is canopy_override, (
    "identify_water_system_candidate_zones() must receive the exact caller-supplied canopy_height= object"
)
assert mock_solar_canopy_case.call_args.kwargs["canopy_height"] is canopy_override, (
    "identify_solar_candidate_zones() must receive the exact caller-supplied canopy_height= object"
)
assert mock_tree_zone_canopy_case.call_args.kwargs["canopy_height"] is canopy_override, (
    "identify_tree_zone_candidates() must receive the exact caller-supplied canopy_height= object"
)
print(
    f"canopy_height= override: reaches every accepting internal call as the exact caller-supplied object, and "
    f"-- run for REAL (not stubbed away) on the water-system/solar/tree-zone paths -- causes production_area."
    f"get_canopy_height_for_boundary() to be called ZERO times across the whole build_pipeline_context() run "
    f"({len(probe.mask_arrays)} real canopy gate(s) reached, every one computed on the exact supplied override "
    "array, not a re-fetched or copied one). road_corridors.identify_road_corridor_candidates() has no canopy "
    "gate to forward to at all."
)

print("\nAll pipeline_context checks passed.")
