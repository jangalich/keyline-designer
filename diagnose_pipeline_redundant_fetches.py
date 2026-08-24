"""
diagnose_pipeline_redundant_fetches.py

Permanent, standalone diagnostic: measures REAL call counts for every
expensive entry point pipeline_context.py's own build_pipeline_context()
calls directly, across ONE real run, and fails loudly if any of them
exceeds its documented expected count. This is the same "permanent tool,
not throwaway script" convention diagnose_water_zone_mask.py already
established -- not a unit test (test_pipeline_context.py already owns
that job, with mock+kwargs-identity assertions proving *which* upstream
instance gets reused), but a standing regression check that measures
counts against the REAL, unmocked functions actually running end to end,
the way the pipeline_context.py solar/tree fields branch caught its own
nested-call redundancy (KNOWN LIMITATIONS #4, now RESOLVED) by hand.

--- What this wraps, and where ---

Confirmed via `grep -n "delineate_valleys\|identify_optimized_production_areas\|
identify_water_suitability\|fetch_and_select_optimal_water_zone\|
identify_road_corridor_candidates\|identify_solar_candidate_zones\|
identify_tree_zone_candidates" pipeline_context.py` -- pipeline_context.py
calls water_suitability.fetch_and_select_optimal_water_zone() directly (NOT
identify_water_suitability() -- that's one level further in, only reached
via fetch_and_select_optimal_water_zone()'s own body), and calls
identify_solar_candidate_zones()/identify_tree_zone_candidates() directly
for selected_structure_site/tree_zone_patches, same as every other entry
point here. Each of the 7 is wrapped with mock_patch.object(..., wraps=
real_function) -- NOT a bare/canned mock -- at the exact module attribute
pipeline_context.py's own imports bind (classic "patch where it's looked
up, not where it's defined", same lesson test_pipeline_context.py's own
delineate_valleys/identify_production_areas bindings already established):

    dem_data.get_dem_for_boundary            -> pc.dem_data.get_dem_for_boundary
    valley_delineation.delineate_valleys     -> pc.valley_delineation.delineate_valleys
    production_area_ceiling.
      identify_optimized_production_areas    -> pc.production_area_ceiling.identify_optimized_production_areas
    water_suitability.
      fetch_and_select_optimal_water_zone    -> pc.fetch_and_select_optimal_water_zone
        (bound via `from water_suitability import fetch_and_select_optimal_water_zone`)
    road_corridors.
      identify_road_corridor_candidates      -> pc.road_corridors.identify_road_corridor_candidates
    solar_suitability.
      identify_solar_candidate_zones         -> pc.identify_solar_candidate_zones
        (bound via `from solar_suitability import identify_solar_candidate_zones`)
    tree_zone_candidates.
      identify_tree_zone_candidates          -> pc.identify_tree_zone_candidates
        (bound via `from tree_zone_candidates import identify_tree_zone_candidates`)

The real pipeline runs for real and produces real output -- only call
counts are observed. Wrapping in wraps= mode still requires every OTHER
network-touching fetch pipeline_context.py's own call graph reaches to
either succeed for real (network mode) or be stubbed offline (synthetic
mode) -- otherwise the real run itself can't complete. See "Two run
modes" below for how each is handled; this script does not modify
pipeline_context.py or any of the 6 dependency modules, and is read-only.

--- Two run modes ---

NETWORK MODE (preferred): a quick, short-timeout probe against dem_data.
DEM_EXPORT_ENDPOINT decides whether real network access is available in
whatever environment this runs in. If so, this runs against a real
reference property boundary (the same one every other module's own
__main__ block already uses) with NO other mocking at all -- every real
fetch (DEM, soil, canopy, roads, hydrology) succeeds for real, and the 7
wraps= mocks above are the only patches active.

SYNTHETIC MODE (fallback, and the one this environment actually exercises
-- outbound network here is proxy-blocked): reuses test_pipeline_context.
py's own synthetic 60x60 DEM fixture verbatim (same two-landform terrain,
same real-centroid UTM math) rather than building a new one. dem_data.
get_dem_for_boundary is the ONE exception to "wraps=real_function" -- the
real function needs network this environment doesn't have, so it's a
return_value=synthetic_dem mock instead (call count is still tracked,
same as any other mock); get_dem_for_boundary's own expected count is
still simply 1. Every other non-target fetch pipeline_context.py's own
call graph reaches is stubbed with the SAME offline fixture test_
pipeline_context.py already proved works end to end for a real,
unmocked run (farm_roads_data.get_road_exclusion_union_utm, road_
corridors._fetch_floodplain_hydric_union, and each module's own
mandatory-canopy fetch -- water_candidate_zones.py's/production_area_
ceiling.py's/water_suitability.py's/solar_suitability.py's/tree_zone_
candidates.py's own separate get_required_tree_root_zone_mask_utm
bindings, all fetch-or-raise, none of them optional). Two of these
(production_area_ceiling.py's and water_suitability.py's own canopy
bindings) are NEW here -- test_pipeline_context.py never needed them
because it never let identify_optimized_production_areas()/fetch_and_
select_optimal_water_zone() run for real (both were canned return_value
mocks there, since proving call-count dedup via kwargs identity doesn't
need the real computation). This script's whole point is the opposite:
it needs the real computation, so it needs the real computation's own
real (mandatory) dependencies stubbed, not skipped. Every OTHER real
fetch each of the 7 targets reaches degrades gracefully on its own
(soil/road/stream fetches are already wrapped in try/except inside
production_area.py/water_suitability.py/tree_zone_candidates.py --
confirmed by reading each, not assumed -- so a network failure there
self-degrades to "unchecked" rather than raising) and needs no stub here.

--- Supplementary, INFORMATIONAL-ONLY section: nested self-compute call
--- counts (expect ZERO, but does not gate exit code -- see caveat below) ---

Beyond the 7-row primary table (each function's own top-level pipeline_
context.py-bound call count, which alone decides PASS/FAIL and this
script's exit code), this script ALSO instruments the exact nested-call
regression class KNOWN LIMITATIONS #4 documented and a follow-up branch
fixed: solar_suitability.identify_solar_candidate_zones() has its own
internal, nested identify_tree_zone_candidates() call (its "TREE-ZONE-
CANDIDATE exclusion" step), and both identify_solar_candidate_zones() and
that nested call each have their OWN separate module-level bindings of
identify_optimized_production_areas()/identify_water_suitability()/
identify_road_corridor_candidates() (solar_suitability.py's own and tree_
zone_candidates.py's own -- THIRD and FOURTH independent bindings,
separate from pipeline_context.py's own and water_candidate_zones.py's
own). When boundary_polygon_utm/production_areas/valleys/selected_water_
zone/selected_road_corridor/hydric_floodplain_union/floodplain_data_is_
fallback are all correctly forwarded into every nested call (the fix),
every one of these SIX bindings (three in solar_suitability.py, three in
tree_zone_candidates.py) should see exactly ZERO calls -- each one firing
means a self-compute fallback fired when an override was already known.

CAVEAT, confirmed live while building this script, since RESOLVED for the
water-zone rows: this section is informational only, NOT part of the
exit-code gate, originally because test_pipeline_context.py's own
synthetic terrain (reused verbatim here, per this script's own mandate
not to build a new fixture) was built ONLY to exercise delineate_
valleys()'s ridge/valley tracing -- its slope is far too steep everywhere
(~80%, vs. production_area_ceiling.py's own MAX_PRODUCTION_SLOPE_PCT) to
clear identify_optimized_production_areas()'s own STEP 1 eligibility
gate. Every prior use of this fixture (test_pipeline_context.py itself)
canned identify_optimized_production_areas()/fetch_and_select_optimal_
water_zone() with fixed return values, so this never surfaced; this
script is the first to run them for REAL against it. Real production_
areas comes back empty here, so the real selected_water_zone legitimately
resolves to None -- and a None selected_water_zone override USED TO be
"indistinguishable from not supplied" to every receiving entry point (the
ambiguous-sentinel convention), which made the water-zone nested bindings
fire their self-compute fallback -- FIVE full identify_water_
suitability() runs per build_pipeline_context() on such a parcel,
measured. That ambiguity is now closed the same way selected_road_
corridor's always was: pipeline_context.py forwards a resolved "nothing"
as water_suitability.NO_WATER_ZONE (the real, explicit already-ran-and-
selected-nothing answer, see that constant's own docstring), never a bare
None, and identify_road_corridor_candidates()/identify_solar_candidate_
zones()/identify_tree_zone_candidates() each accept it -- so the
water-zone nested bindings below are now expected to gate at zero even on
this no-qualifying-zone fixture, exactly like the road-corridor ones
(whose guard forwards build_road_network()'s full network dict,
branches=[] and all, never None -- see that field's own notes in
pipeline_context.py). A nonzero count on ANY nested binding below is now
a real, reintroduced redundancy signal on this fixture, not an excusable
ambiguity artifact.

--- What this does NOT do ---

Does not modify pipeline_context.py or any of the 6 dependency modules --
read-only. Does not wire into generate_full_report.py -- that entry point
doesn't share pipeline_context.py's overrides yet (see pipeline_context.
py's own module docstring), so a generate_full_report.py variant of this
script is separate, later work once that wiring exists.

Exits non-zero (a real assert, not a printed warning) if ANY PRIMARY
count exceeds its expected value, so this is callable from a shell
script or CI-style loop, not just human-read. The supplementary section
is printed for visibility but never fails the run on its own -- see its
own caveat above for why.
"""

import sys
from contextlib import ExitStack
from unittest.mock import patch as mock_patch

import numpy as np
import requests
from rasterio.warp import transform as warp_transform
from shapely.geometry import box

import pipeline_context as pc
import solar_suitability
import tree_zone_candidates
import water_suitability
from _canopy_override_probe import CanopyOverrideProbe, clean_canopy_for
from dem_data import DEM_EXPORT_ENDPOINT, _utm_epsg_for_lonlat

# --- real reference property (network mode only) -- same boundary/anchor
# every other module's own __main__ block (diagnose_water_zone_mask.py,
# road_corridors.py, water_suitability.py, ...) already uses. ---

REAL_PROPERTY_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]
REAL_ANCHOR_LON_LAT = (-79.98356157031265, 40.64303511679458)


# --- synthetic DEM fixture (fallback mode) -- reused verbatim from
# test_pipeline_context.py, not rebuilt -- see that file's own module
# docstring for the two-landform terrain reasoning. ---

CENTER_LON, CENTER_LAT = -79.98, 40.64
EPSG = _utm_epsg_for_lonlat(CENTER_LON, CENTER_LAT)
DST_CRS = f"EPSG:{EPSG}"
_center_x, _center_y = warp_transform("EPSG:4326", DST_CRS, [CENTER_LON], [CENTER_LAT])
center_x, center_y = _center_x[0], _center_y[0]

RESOLUTION = 5.0
ROWS = COLS = 60
VALLEY_COL = 12
RIDGE_COL = 45

origin_x = center_x - COLS * RESOLUTION / 2
origin_y = center_y + ROWS * RESOLUTION / 2

_utm_corners_x = [origin_x, origin_x + COLS * RESOLUTION, origin_x + COLS * RESOLUTION, origin_x, origin_x]
_utm_corners_y = [origin_y, origin_y, origin_y - ROWS * RESOLUTION, origin_y - ROWS * RESOLUTION, origin_y]
_lons, _lats = warp_transform(DST_CRS, "EPSG:4326", _utm_corners_x, _utm_corners_y)
SYNTHETIC_BOUNDARY_COORDINATES = list(zip(_lons, _lats))
SYNTHETIC_ANCHOR_LON_LAT = SYNTHETIC_BOUNDARY_COORDINATES[0]

RADIUS = 8


def _row_weight(col: int, target_col: int) -> float:
    return 0.3 * max(0.0, 1.0 - abs(col - target_col) / RADIUS)


_array = np.zeros((ROWS, COLS), dtype=np.float32)
for _r in range(ROWS):
    for _c in range(COLS):
        if _c < 30:
            _array[_r, _c] = 1000.0 + abs(_c - VALLEY_COL) * 4.0 + _r * _row_weight(_c, VALLEY_COL)
        else:
            _array[_r, _c] = 1200.0 - abs(_c - RIDGE_COL) * 4.0 - _r * _row_weight(_c, RIDGE_COL)

SYNTHETIC_DEM = {
    "array": _array,
    "resolution_meters": (RESOLUTION, RESOLUTION),
    "origin_x": origin_x,
    "origin_y": origin_y,
    "crs": DST_CRS,
}

# --- fake return values for the non-target fetches this run must stub
# offline -- same fixture data test_pipeline_context.py already uses. ---

_FAKE_PATCH = {
    "id": 0,
    "area_acres": 1.23,
    "representative_elevation_m": 1000.0,
    "polygon_utm": box(0, 0, 10, 10),
    "render_fill_polygon_utm": box(0, 0, 10, 10),
    "geometry_wgs84": {"type": "Polygon", "coordinates": [[[0.0, 0.0]]]},
    "cells": [(0, 0)],
    "hole_footprints": [],
    "source_patch_id": 0,
    "suitability_score": 87.5,
    "rank": 1,
    "slope_factor": 0.9,
    "size_factor": 0.8,
    "aspect_factor": 0.7,
}
_FAKE_OPTIMIZED_RESULT = {
    "zones_geojson": {"type": "FeatureCollection", "features": []},
    "scored_patches": [_FAKE_PATCH],
    "total_selected_acreage": 1.23,
    "percent_of_parcel": 5.0,
    # Surfaced by identify_optimized_production_areas() and now read by
    # build_pipeline_context() (PipelineContext.parcel_acres); 24.6 keeps this
    # fixture internally consistent (1.23 / 24.6 * 100 == 5.0).
    "parcel_acres": 24.6,
    "production_ceiling_target_met": True,
    "total_cells_removed": 0,
}
_FAKE_EXISTING_ROADS_UNION = box(100, 100, 200, 200)
_FAKE_HYDRIC_UNION = box(50, 50, 60, 60)
_FAKE_SELECTED_WATER_ZONE = {
    "type": "Feature",
    "id": "water-zone-selected",
    "render_fill_polygon_utm": box(0, 0, 10, 10),
    # Read by build_pipeline_context()'s _attach_keypoint_feature_relationships()
    # (keypoint<->water-zone elevation differential); the real selected_water_zone
    # carries it now.
    "representative_elevation_m": 995.0,
}
_FAKE_SELECTED_ROAD_CORRIDOR = {
    "type": "Feature",
    "id": "road-corridor-selected",
    "cell_footprint_polygon_utm": box(0, 0, 10, 10),
    "cells": [(0, 0)],
}
_FAKE_ROAD_CORRIDOR_RESULT = {
    "zones_geojson": {"type": "FeatureCollection", "features": []},
    "all_scored_candidates": [],
    # "road_network" is what pipeline_context.py's own build_pipeline_
    # context() reads (never None, even with no branches) -- "selected_
    # road_corridor" is what solar_suitability.py's/tree_zone_candidates.
    # py's own internal self-compute fallbacks read off this same return
    # shape when THEY call identify_road_corridor_candidates() directly.
    # Both keys point at the same fake dict here since neither consumer in
    # this script cares about the full real road_network shape (branches/
    # total_length_meters/stop_reason/...), only cell_footprint_polygon_utm/
    # cells.
    "road_network": _FAKE_SELECTED_ROAD_CORRIDOR,
    "selected_road_corridor": _FAKE_SELECTED_ROAD_CORRIDOR,
}


def _fake_clean_canopy_mask(boundary_polygon_utm, dem, buffer_meters=None, canopy_height=None):
    """Offline stand-in for get_required_tree_root_zone_mask_utm() at every
    one of its several independent module-level bindings -- each of those
    fetches is MANDATORY (fetch-or-raise, no try/except around it) in the
    real function it backs, so it must be stubbed for a real, offline run
    to complete at all. All-False (no tree cover anywhere) is a pure no-op
    against every canopy-exclusion gate downstream -- this script isn't
    testing canopy gate correctness, only real call counts. canopy_height
    mirrors the real function's own pre-fetched-canopy override parameter
    (this branch's callers now always forward canopy_height= through, so
    this stand-in must accept it too); ignored here -- this stub returns
    its fixed clean mask regardless of where canopy would have come from."""
    return np.zeros(dem["array"].shape, dtype=bool)


def _network_available() -> bool:
    """Short-timeout probe against the same USGS ImageServer endpoint
    dem_data.get_dem_for_boundary() itself queries -- cheap way to decide
    which of the two run modes applies without attempting (and possibly
    hanging on) a real DEM fetch first."""
    try:
        requests.get(DEM_EXPORT_ENDPOINT, timeout=5)
        return True
    except requests.exceptions.RequestException:
        return False


def run_network_mode() -> tuple[dict, dict, dict]:
    """
    Runs build_pipeline_context() against the real reference property with
    NO mocking beyond the 7 wraps= call-count wrappers themselves -- every
    real fetch (DEM, soil, canopy, roads, hydrology) succeeds for real.
    Nested self-compute bindings (the "supplementary" table) are wrapped
    with wraps=real here too, since there's no offline reason to avoid
    running them for real if a regression makes them fire.
    """
    with ExitStack() as stack:
        enter = stack.enter_context

        mock_dem = enter(mock_patch.object(pc.dem_data, "get_dem_for_boundary", wraps=pc.dem_data.get_dem_for_boundary))
        mock_delineate = enter(
            mock_patch.object(pc.valley_delineation, "delineate_valleys", wraps=pc.valley_delineation.delineate_valleys)
        )
        mock_optimize = enter(
            mock_patch.object(
                pc.production_area_ceiling,
                "identify_optimized_production_areas",
                wraps=pc.production_area_ceiling.identify_optimized_production_areas,
            )
        )
        mock_water = enter(
            mock_patch.object(pc, "fetch_and_select_optimal_water_zone", wraps=pc.fetch_and_select_optimal_water_zone)
        )
        mock_road_corridor = enter(
            mock_patch.object(
                pc.road_corridors, "identify_road_corridor_candidates", wraps=pc.road_corridors.identify_road_corridor_candidates
            )
        )
        mock_solar = enter(mock_patch.object(pc, "identify_solar_candidate_zones", wraps=pc.identify_solar_candidate_zones))
        mock_tree_zone = enter(mock_patch.object(pc, "identify_tree_zone_candidates", wraps=pc.identify_tree_zone_candidates))

        mock_solar_optimize = enter(
            mock_patch.object(
                solar_suitability, "identify_optimized_production_areas", wraps=solar_suitability.identify_optimized_production_areas
            )
        )
        mock_solar_water = enter(
            mock_patch.object(solar_suitability, "identify_water_suitability", wraps=solar_suitability.identify_water_suitability)
        )
        mock_solar_road = enter(
            mock_patch.object(
                solar_suitability, "identify_road_corridor_candidates", wraps=solar_suitability.identify_road_corridor_candidates
            )
        )
        mock_tz_optimize = enter(
            mock_patch.object(
                tree_zone_candidates,
                "identify_optimized_production_areas",
                wraps=tree_zone_candidates.identify_optimized_production_areas,
            )
        )
        mock_tz_water = enter(
            mock_patch.object(
                tree_zone_candidates, "identify_water_suitability", wraps=tree_zone_candidates.identify_water_suitability
            )
        )
        mock_tz_road = enter(
            mock_patch.object(
                tree_zone_candidates, "identify_road_corridor_candidates", wraps=tree_zone_candidates.identify_road_corridor_candidates
            )
        )
        mock_wcz_delineate = enter(
            mock_patch.object(pc.water_candidate_zones, "delineate_valleys", wraps=pc.water_candidate_zones.delineate_valleys)
        )
        mock_wcz_identify_pa = enter(
            mock_patch.object(
                pc.water_candidate_zones, "identify_production_areas", wraps=pc.water_candidate_zones.identify_production_areas
            )
        )

        ctx = pc.build_pipeline_context(REAL_PROPERTY_BOUNDARY, REAL_ANCHOR_LON_LAT)

    assert isinstance(ctx, pc.PipelineContext)

    primary_counts = {
        "get_dem_for_boundary": mock_dem.call_count,
        "delineate_valleys": mock_delineate.call_count,
        "identify_optimized_production_areas": mock_optimize.call_count,
        "fetch_and_select_optimal_water_zone": mock_water.call_count,
        "identify_road_corridor_candidates": mock_road_corridor.call_count,
        "identify_solar_candidate_zones": mock_solar.call_count,
        "identify_tree_zone_candidates": mock_tree_zone.call_count,
    }
    nested_counts = {
        "solar_suitability.identify_optimized_production_areas (nested)": mock_solar_optimize.call_count,
        "solar_suitability.identify_water_suitability (nested)": mock_solar_water.call_count,
        "solar_suitability.identify_road_corridor_candidates (nested)": mock_solar_road.call_count,
        "tree_zone_candidates.identify_optimized_production_areas (nested)": mock_tz_optimize.call_count,
        "tree_zone_candidates.identify_water_suitability (nested)": mock_tz_water.call_count,
        "tree_zone_candidates.identify_road_corridor_candidates (nested)": mock_tz_road.call_count,
        "water_candidate_zones.delineate_valleys (self-compute fallback)": mock_wcz_delineate.call_count,
        "water_candidate_zones.identify_production_areas (self-compute fallback)": mock_wcz_identify_pa.call_count,
    }
    notes = {
        "production_areas_count": len(ctx.production_areas),
        "selected_water_zone_is_none": ctx.selected_water_zone is None,
    }
    return primary_counts, nested_counts, notes


def run_synthetic_mode() -> tuple[dict, dict, dict]:
    """
    Runs build_pipeline_context() against test_pipeline_context.py's own
    synthetic DEM fixture, with every non-target real fetch stubbed
    offline (see module docstring's "SYNTHETIC MODE" section for exactly
    which, and why each is safe to stub rather than skip). The 7 target
    entry points, plus the 6 nested self-compute bindings the
    supplementary table checks, are wraps=real -- the real pipeline still
    runs and produces real output.
    """
    with ExitStack() as stack:
        enter = stack.enter_context

        mock_dem = enter(mock_patch.object(pc.dem_data, "get_dem_for_boundary", return_value=SYNTHETIC_DEM))
        mock_delineate = enter(
            mock_patch.object(pc.valley_delineation, "delineate_valleys", wraps=pc.valley_delineation.delineate_valleys)
        )
        # exclusion_zones.identify_exclusion_zones() -- added to build_
        # pipeline_context() by a later branch than this script, runs REAL
        # here (it's not one of the 7 targets, but the run can't complete
        # without it): its canopy gate is MANDATORY (fetch-or-raise, no
        # try/except -- an unstubbed run raises straight out of synthetic
        # mode, found live once that step landed), and its soil/road
        # fetches, while gracefully degrading, would each burn real retry
        # time against a proxy that rejects every attempt.
        enter(mock_patch.object(pc.exclusion_zones, "get_required_tree_root_zone_mask_utm", side_effect=_fake_clean_canopy_mask))
        enter(mock_patch.object(pc.exclusion_zones, "_fetch_disqualifying_soil_union", return_value=None))
        enter(mock_patch.object(pc.exclusion_zones, "_fetch_road_exclusion_union_utm", return_value=None))
        mock_optimize = enter(
            mock_patch.object(
                pc.production_area_ceiling,
                "identify_optimized_production_areas",
                wraps=pc.production_area_ceiling.identify_optimized_production_areas,
            )
        )
        # production_area_ceiling.py's own mandatory canopy binding -- NEW
        # here vs. test_pipeline_context.py (which never let this function
        # run for real). Soil/road fetches inside identify_optimized_
        # production_areas() are each wrapped in their own try/except and
        # self-degrade to "unchecked" on failure (confirmed by reading
        # production_area_ceiling.py directly) -- no stub needed for those.
        enter(
            mock_patch.object(pc.production_area_ceiling, "get_required_tree_root_zone_mask_utm", side_effect=_fake_clean_canopy_mask)
        )

        mock_roads = enter(
            mock_patch.object(pc.farm_roads_data, "get_road_exclusion_union_utm", return_value=_FAKE_EXISTING_ROADS_UNION)
        )
        enter(
            mock_patch.object(pc.road_corridors, "_fetch_floodplain_hydric_union", return_value=(_FAKE_HYDRIC_UNION, False))
        )

        enter(
            mock_patch.object(
                pc.water_candidate_zones,
                "identify_water_system_candidate_zones",
                wraps=pc.water_candidate_zones.identify_water_system_candidate_zones,
            )
        )
        enter(
            mock_patch.object(pc.water_candidate_zones, "get_required_tree_root_zone_mask_utm", side_effect=_fake_clean_canopy_mask)
        )
        enter(mock_patch.object(pc.water_candidate_zones, "_fetch_road_exclusion_union_utm", return_value=None))
        mock_wcz_delineate = enter(mock_patch.object(pc.water_candidate_zones, "delineate_valleys"))
        mock_wcz_identify_pa = enter(mock_patch.object(pc.water_candidate_zones, "identify_production_areas"))

        mock_water = enter(
            mock_patch.object(pc, "fetch_and_select_optimal_water_zone", wraps=pc.fetch_and_select_optimal_water_zone)
        )
        # water_suitability.py's own mandatory canopy binding -- NEW here
        # vs. test_pipeline_context.py, same reasoning as production_area_
        # ceiling.py's above. Per-zone soil fetch and the whole-boundary
        # NHD stream fetch inside identify_water_suitability() are each
        # wrapped in their own try/except (confirmed by reading water_
        # suitability.py directly) -- no stub needed for those either.
        enter(mock_patch.object(water_suitability, "get_required_tree_root_zone_mask_utm", side_effect=_fake_clean_canopy_mask))

        mock_road_corridor = enter(
            mock_patch.object(
                pc.road_corridors, "identify_road_corridor_candidates", wraps=pc.road_corridors.identify_road_corridor_candidates
            )
        )

        mock_solar = enter(mock_patch.object(pc, "identify_solar_candidate_zones", wraps=pc.identify_solar_candidate_zones))
        mock_tree_zone = enter(mock_patch.object(pc, "identify_tree_zone_candidates", wraps=pc.identify_tree_zone_candidates))

        enter(mock_patch.object(solar_suitability, "get_required_tree_root_zone_mask_utm", side_effect=_fake_clean_canopy_mask))
        mock_solar_optimize = enter(
            mock_patch.object(solar_suitability, "identify_optimized_production_areas", return_value=_FAKE_OPTIMIZED_RESULT)
        )
        mock_solar_water = enter(
            mock_patch.object(
                solar_suitability, "identify_water_suitability", return_value={"selected_water_zone": _FAKE_SELECTED_WATER_ZONE}
            )
        )
        mock_solar_road = enter(
            mock_patch.object(solar_suitability, "identify_road_corridor_candidates", return_value=_FAKE_ROAD_CORRIDOR_RESULT)
        )

        enter(mock_patch.object(tree_zone_candidates, "get_required_tree_root_zone_mask_utm", side_effect=_fake_clean_canopy_mask))
        mock_tz_optimize = enter(
            mock_patch.object(tree_zone_candidates, "identify_optimized_production_areas", return_value=_FAKE_OPTIMIZED_RESULT)
        )
        mock_tz_water = enter(
            mock_patch.object(
                tree_zone_candidates, "identify_water_suitability", return_value={"selected_water_zone": _FAKE_SELECTED_WATER_ZONE}
            )
        )
        mock_tz_road = enter(
            mock_patch.object(tree_zone_candidates, "identify_road_corridor_candidates", return_value=_FAKE_ROAD_CORRIDOR_RESULT)
        )

        ctx = pc.build_pipeline_context(SYNTHETIC_BOUNDARY_COORDINATES, SYNTHETIC_ANCHOR_LON_LAT)

    assert isinstance(ctx, pc.PipelineContext)

    primary_counts = {
        "get_dem_for_boundary": mock_dem.call_count,
        "delineate_valleys": mock_delineate.call_count,
        "identify_optimized_production_areas": mock_optimize.call_count,
        "fetch_and_select_optimal_water_zone": mock_water.call_count,
        "identify_road_corridor_candidates": mock_road_corridor.call_count,
        "identify_solar_candidate_zones": mock_solar.call_count,
        "identify_tree_zone_candidates": mock_tree_zone.call_count,
    }
    nested_counts = {
        "solar_suitability.identify_optimized_production_areas (nested)": mock_solar_optimize.call_count,
        "solar_suitability.identify_water_suitability (nested)": mock_solar_water.call_count,
        "solar_suitability.identify_road_corridor_candidates (nested)": mock_solar_road.call_count,
        "tree_zone_candidates.identify_optimized_production_areas (nested)": mock_tz_optimize.call_count,
        "tree_zone_candidates.identify_water_suitability (nested)": mock_tz_water.call_count,
        "tree_zone_candidates.identify_road_corridor_candidates (nested)": mock_tz_road.call_count,
        "water_candidate_zones.delineate_valleys (self-compute fallback)": mock_wcz_delineate.call_count,
        "water_candidate_zones.identify_production_areas (self-compute fallback)": mock_wcz_identify_pa.call_count,
    }
    # mock_roads isn't part of either table (not one of this session's 7
    # target entry points), but its own call count is a cheap extra sanity
    # check that the offline stub was actually exercised once, not zero
    # or several times.
    assert mock_roads.call_count == 1, f"farm_roads_data.get_road_exclusion_union_utm stub called {mock_roads.call_count}x, expected 1"
    notes = {
        "production_areas_count": len(ctx.production_areas),
        "selected_water_zone_is_none": ctx.selected_water_zone is None,
    }
    return primary_counts, nested_counts, notes


def _run_pipeline_context_for_canopy_measurement(canopy_height) -> int:
    """
    A SEPARATE synthetic-mode run, dedicated to measuring real production_
    area.get_canopy_height_for_boundary() call counts -- the shared leaf
    every one of this session's canopy gates ultimately calls (see
    _canopy_override_probe.py's own docstring for why patching there alone
    observes every path in, no matter how deeply nested the reaching
    caller is). run_synthetic_mode() above stubs get_required_tree_root_
    zone_mask_utm() itself (a level ABOVE this leaf) with _fake_clean_
    canopy_mask for speed, which would make a leaf-level call count always
    read 0 trivially -- vacuous for what THIS branch needs to prove. This
    function instead leaves every get_required_tree_root_zone_mask_utm()
    binding genuinely real, and patches ONLY the true leaf, via the same
    CanopyOverrideProbe every other real canopy-override test in this
    session uses. Every non-canopy fetch is stubbed exactly the same way
    run_synthetic_mode() already does.
    """
    with ExitStack() as stack:
        enter = stack.enter_context
        enter(mock_patch.object(pc.dem_data, "get_dem_for_boundary", return_value=SYNTHETIC_DEM))
        enter(mock_patch.object(pc.valley_delineation, "delineate_valleys", return_value=[]))
        # exclusion_zones.identify_exclusion_zones() runs real here -- its
        # canopy gate is deliberately left genuine (the probe patches the
        # true leaf), but its gracefully-degrading soil/road fetches are
        # stubbed so they don't burn real retry time against the proxy
        # (same reasoning as run_synthetic_mode()'s own stubs for them).
        enter(mock_patch.object(pc.exclusion_zones, "_fetch_disqualifying_soil_union", return_value=None))
        enter(mock_patch.object(pc.exclusion_zones, "_fetch_road_exclusion_union_utm", return_value=None))
        enter(
            mock_patch.object(
                pc.production_area_ceiling, "identify_optimized_production_areas", return_value=_FAKE_OPTIMIZED_RESULT
            )
        )
        enter(mock_patch.object(pc.farm_roads_data, "get_road_exclusion_union_utm", return_value=_FAKE_EXISTING_ROADS_UNION))
        enter(mock_patch.object(pc.road_corridors, "_fetch_floodplain_hydric_union", return_value=(_FAKE_HYDRIC_UNION, False)))

        # identify_water_system_candidate_zones/identify_solar_candidate_zones/
        # identify_tree_zone_candidates left real below -- same three this
        # session's other real canopy-override runs always leave real, since
        # each owns its own genuine (unstubbed-at-the-leaf) canopy gate.
        enter(
            mock_patch.object(
                pc.water_candidate_zones,
                "identify_water_system_candidate_zones",
                wraps=pc.water_candidate_zones.identify_water_system_candidate_zones,
            )
        )
        enter(mock_patch.object(pc.water_candidate_zones, "_fetch_road_exclusion_union_utm", return_value=None))
        enter(mock_patch.object(pc.water_candidate_zones, "delineate_valleys"))
        enter(mock_patch.object(pc.water_candidate_zones, "identify_production_areas"))

        enter(mock_patch.object(pc, "fetch_and_select_optimal_water_zone", return_value=_FAKE_SELECTED_WATER_ZONE))
        enter(mock_patch.object(pc.road_corridors, "identify_road_corridor_candidates", return_value=_FAKE_ROAD_CORRIDOR_RESULT))

        enter(mock_patch.object(pc, "identify_solar_candidate_zones", wraps=pc.identify_solar_candidate_zones))
        enter(mock_patch.object(pc, "identify_tree_zone_candidates", wraps=pc.identify_tree_zone_candidates))

        enter(mock_patch.object(solar_suitability, "identify_optimized_production_areas", return_value=_FAKE_OPTIMIZED_RESULT))
        enter(
            mock_patch.object(
                solar_suitability, "identify_water_suitability", return_value={"selected_water_zone": _FAKE_SELECTED_WATER_ZONE}
            )
        )
        enter(mock_patch.object(solar_suitability, "identify_road_corridor_candidates", return_value=_FAKE_ROAD_CORRIDOR_RESULT))
        enter(mock_patch.object(tree_zone_candidates, "identify_optimized_production_areas", return_value=_FAKE_OPTIMIZED_RESULT))
        enter(
            mock_patch.object(
                tree_zone_candidates, "identify_water_suitability", return_value={"selected_water_zone": _FAKE_SELECTED_WATER_ZONE}
            )
        )
        enter(mock_patch.object(tree_zone_candidates, "identify_road_corridor_candidates", return_value=_FAKE_ROAD_CORRIDOR_RESULT))

        with CanopyOverrideProbe() as probe:
            pc.build_pipeline_context(SYNTHETIC_BOUNDARY_COORDINATES, SYNTHETIC_ANCHOR_LON_LAT, canopy_height=canopy_height)

    return probe.fetch_calls


# --- expected counts, with reasoning for anything not simply 1 ---

EXPECTED_PRIMARY = {
    "get_dem_for_boundary": 1,
    # valleys only -- ridge_lines (a second delineate_valleys() pass against
    # an elevation-inverted DEM, feeding output nothing read) was deleted
    # outright; see pipeline_context.py's own module docstring.
    "delineate_valleys": 1,
    "identify_optimized_production_areas": 1,
    "fetch_and_select_optimal_water_zone": 1,
    "identify_road_corridor_candidates": 1,
    "identify_solar_candidate_zones": 1,
    "identify_tree_zone_candidates": 1,
}

# Every nested/self-compute binding below should be called ZERO times --
# each one firing means a self-compute fallback ran when pipeline_context.
# py had already supplied that value as an override, i.e. a real,
# rediscovered instance of the same redundancy class KNOWN LIMITATIONS #4
# documented and a follow-up branch fixed.
EXPECTED_NESTED = {
    "solar_suitability.identify_optimized_production_areas (nested)": 0,
    "solar_suitability.identify_water_suitability (nested)": 0,
    "solar_suitability.identify_road_corridor_candidates (nested)": 0,
    "tree_zone_candidates.identify_optimized_production_areas (nested)": 0,
    "tree_zone_candidates.identify_water_suitability (nested)": 0,
    "tree_zone_candidates.identify_road_corridor_candidates (nested)": 0,
    "water_candidate_zones.delineate_valleys (self-compute fallback)": 0,
    "water_candidate_zones.identify_production_areas (self-compute fallback)": 0,
}


def _print_table(title: str, counts: dict, expected: dict) -> bool:
    print(f"\n{title}")
    print(f"{'function':<62} {'actual':>8} {'expected':>8}  result")
    print("-" * 92)
    all_pass = True
    for name, actual in counts.items():
        exp = expected[name]
        passed = actual <= exp
        all_pass = all_pass and passed
        print(f"{name:<62} {actual:>8} {exp:>8}  {'PASS' if passed else 'FAIL'}")
    return all_pass


def main() -> None:
    print("diagnose_pipeline_redundant_fetches.py -- real call-count regression check for build_pipeline_context()\n")

    if _network_available():
        print(f"Network available (reached {DEM_EXPORT_ENDPOINT.split('?')[0]}) -- running NETWORK MODE against the real "
              f"reference property boundary ({len(REAL_PROPERTY_BOUNDARY)} vertices).")
        primary_counts, nested_counts, notes = run_network_mode()
    else:
        print("Network unavailable -- falling back to SYNTHETIC MODE (test_pipeline_context.py's own synthetic 60x60 DEM "
              "fixture, every other non-target fetch stubbed offline).")
        primary_counts, nested_counts, notes = run_synthetic_mode()

    primary_ok = _print_table("Primary entry points (pipeline_context.py's own top-level bindings) -- GATES exit code", primary_counts, EXPECTED_PRIMARY)
    _print_table(
        "Supplementary, INFORMATIONAL ONLY: nested self-compute bindings (regression class KNOWN LIMITATIONS #4) "
        "-- does NOT gate exit code, see module docstring caveat",
        nested_counts, EXPECTED_NESTED,
    )
    if notes["production_areas_count"] == 0 or notes["selected_water_zone_is_none"]:
        print(
            f"\nNOTE: this run's real production_areas came back empty (count={notes['production_areas_count']}) and "
            f"selected_water_zone is None ({notes['selected_water_zone_is_none']}) -- expected in SYNTHETIC MODE "
            "(the reused terrain is too steep for any real production area). Since build_pipeline_context() now "
            "forwards a resolved no-qualifying-water-zone answer as water_suitability.NO_WATER_ZONE (never a bare "
            "None -- see module docstring caveat, RESOLVED), the supplementary table's water-zone rows are "
            "expected at ZERO even on this fixture; any nonzero row above is a real, reintroduced-redundancy "
            "signal, no longer an excusable ambiguity artifact."
        )

    print()
    if primary_ok:
        print("PRIMARY TABLE: ALL PASS -- every measured count for this session's 7 target entry points is at or "
              "under its expected value.")
    else:
        print("PRIMARY TABLE: FAILURE -- at least one measured call count exceeded its expected value (see FAIL "
              "rows above). This is a real, undiscovered redundant fetch -- investigate before assuming the "
              "expected-count table needs updating.")

    for name, actual in primary_counts.items():
        assert actual <= EXPECTED_PRIMARY[name], f"{name}: {actual} calls exceeds expected {EXPECTED_PRIMARY[name]}"

    # --- canopy_height: dedicated leaf-level measurement (always offline/synthetic, independent of the
    # --- network-vs-synthetic mode picked above -- see _run_pipeline_context_for_canopy_measurement()'s
    # --- own docstring for why this needs its OWN run rather than reusing run_synthetic_mode()'s counts.
    print(
        "\nCanopy fetch count (production_area.get_canopy_height_for_boundary(), the single shared leaf "
        "EVERY canopy gate this session touched -- production/optimized-production, water-system-candidate, "
        "water-suitability, solar, tree-zone, all ultimately call -- resolves to):"
    )
    canopy_calls_no_override = _run_pipeline_context_for_canopy_measurement(None)
    print(
        f"  canopy_height omitted (pre-existing self-fetch behavior, unchanged by this branch): "
        f"{canopy_calls_no_override} independent fetch(es)"
    )
    _simulated_parcel_data_canopy = clean_canopy_for(SYNTHETIC_DEM)
    canopy_calls_with_override = _run_pipeline_context_for_canopy_measurement(_simulated_parcel_data_canopy)
    print(
        f"  canopy_height=<parcel_data's own single upstream fetch> supplied: {canopy_calls_with_override} "
        "additional fetch(es) from inside build_pipeline_context() (expected 0)"
    )
    if canopy_calls_with_override == 0:
        print(
            "CANOPY: PASS -- with the override supplied, build_pipeline_context() contributes ZERO additional "
            "canopy fetches; the ONE real fetch a full run performs is parcel_data.fetch_parcel_data()'s own "
            "single upstream call, reused everywhere downstream instead of the 3+ independent re-fetches "
            "confirmed earlier this session."
        )
    else:
        print(
            f"CANOPY: FAILURE -- {canopy_calls_with_override} canopy fetch(es) still happened inside "
            "build_pipeline_context() even with canopy_height supplied -- at least one accepting entry point "
            "lost the override somewhere along its call chain."
        )
    assert canopy_calls_with_override == 0, (
        f"canopy_height override supplied but get_canopy_height_for_boundary() was still called "
        f"{canopy_calls_with_override} time(s) inside build_pipeline_context() -- the redundancy is NOT closed"
    )


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"\nASSERTION FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nRequest failed: {e}")
        print(
            "\nNote: NETWORK MODE requires internet access to reach USGS's National Map ImageServer "
            "(DEM fetch) and every dependency module's own SSURGO/canopy/road/NHD data sources. "
            "SYNTHETIC MODE should never reach the network at all -- an exception there is a bug in "
            "this script's own offline stubbing, not a network issue."
        )
        sys.exit(1)
