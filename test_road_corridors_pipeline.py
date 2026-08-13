"""
test_road_corridors_pipeline.py

End-to-end offline check that identify_road_corridor_candidates()'s
wiring (DEM -> optimized production areas -> the single selected water
zone -> floodplain fetch -> coverage-greedy network routing -> per-branch
GeoJSON) fits together, using a hand-built synthetic DEM passed via `dem=`
(same approach as test_water_system_candidate_pipeline.py /
test_solar_suitability_pipeline.py).

production_areas comes from production_area_ceiling.identify_optimized_
production_areas() (the OPTIMIZED/final, ceiling-trimmed patch shape,
scored against render_fill_polygon_utm) rather than production_area.
identify_production_areas()'s raw pre-optimization patches, and the
water-zone hard exclusion comes from water_suitability.fetch_and_
select_optimal_water_zone() -- the single rank-1 SELECTED zone -- rather
than every unscored candidate zone water_candidate_zones.py generates.
Water stays a HARD exclusion on the ridge itself; production is a HARD
exclusion for the anchor-to-ridge connector only, but a SOFT, cell-based
scoring term against the ridge fragment (an earlier version of this
module treated production as hard on the ridge too). Floodplain stays a
soft cost penalty on the connector. The
erosion-prone-soil preference this module used to carry has been removed
outright (KSOP: Soil is step 8, below Farm Roads at step 4), so this test
no longer exercises or mocks anything erosion-related.

Both the production-area and water-zone pipelines share production_area.
py's mandatory woody-vegetation (canopy) gate -- a real network fetch
this sandbox can't reach, and one that deliberately does NOT degrade
gracefully (see production_area.identify_production_areas()'s own
docstring), so it's mocked here with a synthetic "no trees anywhere"
canopy result, same convention test_production_area_ceiling.py /
test_water_suitability.py already use. The disqualifying-soil fetch (used
internally by both production_area.py and production_area_ceiling.py) is
ALSO mocked here purely to keep this "offline" test fast and deterministic
rather than depending on how quickly a given environment's proxy rejects
the call -- it already degrades gracefully on its own if left unmocked.

The NHD and SSURGO fetches inside identify_road_corridor_candidates()
(and inside water_suitability.py's own per-zone soil/stream scoring) are
real network calls this sandbox can't reach — by design, each is wrapped
in its own try/except and degrades gracefully (no NHD/SSURGO data ->
floodplain cost-penalty union falls back to buffered valley lines,
flagged in confidence_notes). This test doubles as a live check of that
degradation path. anchor_lon_lat is a real, required input now (see
road_corridors.py's own module docstring) -- there's no farm-roads fetch
or named-road anchoring/connector search left in this module at all, so
unlike an earlier version of this test, no road-data mocking is needed
here.
"""

from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Point

import production_area
import production_area_ceiling
from dem_data import _utm_epsg_for_lonlat
from feature_schema import validate_feature_collection
from road_corridors import MAX_ROAD_GRADE_PCT, identify_road_corridor_candidates


def _fake_clean_canopy(boundary_coordinates, dem):
    return {
        "array": np.full(dem["array"].shape, 1.0, dtype=np.float32),  # below threshold everywhere -- no trees
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source_item_id": "offline-test-stub",
    }

CENTER_LON, CENTER_LAT = -79.98, 40.64
EPSG = _utm_epsg_for_lonlat(CENTER_LON, CENTER_LAT)
DST_CRS = f"EPSG:{EPSG}"

center_x, center_y = warp_transform("EPSG:4326", DST_CRS, [CENTER_LON], [CENTER_LAT])
center_x, center_y = center_x[0], center_y[0]

RESOLUTION = 5.0
ROWS = COLS = 40
origin_x = center_x - COLS * RESOLUTION / 2
origin_y = center_y + ROWS * RESOLUTION / 2

utm_corners_x = [origin_x, origin_x + COLS * RESOLUTION, origin_x + COLS * RESOLUTION, origin_x, origin_x]
utm_corners_y = [origin_y, origin_y, origin_y - ROWS * RESOLUTION, origin_y - ROWS * RESOLUTION, origin_y]
lons, lats = warp_transform(DST_CRS, "EPSG:4326", utm_corners_x, utm_corners_y)
boundary_coordinates = list(zip(lons, lats))

# A gentle, uniform ~3% south-facing grade across the WHOLE grid -- same
# "gentle uniform slope claims essentially the whole parcel as one real
# production zone" fixture style test_solar_suitability_pipeline.py already
# established (see that file's own module docstring), deliberately NOT the
# narrow, production-starved ridge this test used before this branch: a
# fixture with no real production demand only ever exercises route_road_
# network()'s "no_demand" empty-network path (see the dedicated no-
# production test below), never a genuine multi-branch network. Anchored at
# one corner so a real, substantial slice of this production ground sits
# beyond the anchor's own PRODUCTION_SERVICE_RADIUS_METERS baseline
# coverage -- the router has to actually grow branches to reach it, not
# just report everything already served.
array = np.zeros((ROWS, COLS), dtype=np.float32)
for row in range(ROWS):
    array[row, :] = 100.0 - row * 0.15

synthetic_dem = {
    "array": array,
    "resolution_meters": (RESOLUTION, RESOLUTION),
    "origin_x": origin_x,
    "origin_y": origin_y,
    "crs": DST_CRS,
}

# A real anchor point at the grid's own north-west corner (row=0, col=0) --
# routing grows outward from here toward the real production demand this
# fixture's own gentle slope produces across the rest of the parcel.
anchor_row, anchor_col = 0, 0
anchor_x = origin_x + (anchor_col + 0.5) * RESOLUTION
anchor_y = origin_y - (anchor_row + 0.5) * RESOLUTION
anchor_lons, anchor_lats = warp_transform(DST_CRS, "EPSG:4326", [anchor_x], [anchor_y])
anchor_lon_lat = (anchor_lons[0], anchor_lats[0])

# identify_road_corridor_candidates() now calls production_area_ceiling.
# identify_optimized_production_areas() and water_suitability.fetch_and_
# select_optimal_water_zone() -- both of which, via production_area.py's
# shared helpers, do their own disqualifying-soil fetch (gracefully
# degrading on failure) and MANDATORY canopy fetch (hard-fails on failure,
# see this file's own docstring). The soil fetch is mocked here purely to
# keep this "offline" pipeline test fast and deterministic rather than
# depending on how quickly a given environment's proxy rejects the call;
# the canopy fetch MUST be mocked, since an unreachable network would
# otherwise raise instead of degrading.
with mock_patch.object(production_area, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area_ceiling, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy):
    result = identify_road_corridor_candidates(boundary_coordinates, dem=synthetic_dem, anchor_lon_lat=anchor_lon_lat)

assert "zones_geojson" in result
assert "road_network" in result
validate_feature_collection(result["zones_geojson"])

network = result["road_network"]
branches = network["branches"]
features = result["zones_geojson"]["features"]
print(
    f"Pipeline ran end-to-end with unreachable NHD/SSURGO data: {len(branches)} branch(es), "
    f"stop_reason={network['stop_reason']!r}."
)
assert branches, (
    "expected at least one branch: this fixture's gentle uniform slope gives production_area_"
    "ceiling.py real, substantial production demand, and a meaningful slice of it sits beyond the "
    "anchor's own baseline PRODUCTION_SERVICE_RADIUS_METERS coverage for the router to grow toward"
)
assert len(features) == len(branches), "one GeoJSON feature must exist per branch, no more, no fewer"

assert branches[0]["branch_role"] == "trunk", "the first branch (branch_index 0) must be the trunk"
assert branches[0]["joins_branch_index"] is None, "the trunk joins nothing -- it's the network's own root"
for branch in branches[1:]:
    assert branch["branch_role"] in ("spur", "water_spur"), (
        f"every branch after the trunk must be a spur or water_spur, got {branch['branch_role']!r}"
    )
    assert branch["joins_branch_index"] is not None, (
        f"branch {branch['branch_index']} is not the trunk, so it must join an earlier branch"
    )
    assert branch["joins_branch_index"] < branch["branch_index"], (
        f"branch {branch['branch_index']}'s own joins_branch_index ({branch['joins_branch_index']}) must "
        f"point at an EARLIER branch, not itself or a later one"
    )

for branch in branches:
    assert branch["avg_grade_pct"] <= MAX_ROAD_GRADE_PCT + 1e-9, (
        f"branch {branch['branch_index']}'s own avg_grade_pct ({branch['avg_grade_pct']}) exceeds "
        f"road_corridors.MAX_ROAD_GRADE_PCT ({MAX_ROAD_GRADE_PCT}) -- grade is a genuine hard ceiling"
    )

for feature in features:
    props = feature["properties"]
    assert props["layer"] == "suggested_road_corridor"
    assert feature["geometry"]["type"] == "LineString"
    for required_key in ("branch_role", "branch_index", "length_ft"):
        assert required_key in props, f"every feature must carry {required_key!r}"
    assert not any(key.startswith("ridge_") for key in props), (
        f"no ridge_* property should exist on the new network-shaped schema, found: "
        f"{[k for k in props if k.startswith('ridge_')]}"
    )
    for removed_key in ("rank", "suitability_score", "weighted_score", "anchor_status", "anchor_road_name"):
        assert removed_key not in props, f"{removed_key!r} no longer exists on the new network-shaped schema"
    assert "crosses_production_zone" in props
    assert "crosses_floodplain" in props
    notes = props["confidence_notes"].lower()
    # No NHD/SSURGO data was reachable in this sandbox -> the floodplain
    # cost-penalty union fell back to buffered valley lines, flagged here.
    assert "dem-only fallback" in notes, "the floodplain-fallback caveat should appear in confidence_notes"

print(
    f"Routes correctly form a real, connected multi-branch network (trunk branch_index 0 with "
    f"joins_branch_index None, every spur's own joins_branch_index pointing at an earlier branch, "
    f"every branch's avg_grade_pct at or under MAX_ROAD_GRADE_PCT={MAX_ROAD_GRADE_PCT}), reflect "
    f"unreachable NHD/SSURGO data (floodplain fallback flagged in confidence_notes), and carry no "
    f"ridge_*/rank/suitability_score/weighted_score/anchor_* properties from the old schema."
)


# --- boundary_polygon_utm/production_areas/valleys/selected_water_zone/ --
# --- hydric_floodplain_union overrides ------------------------------------
#
# Real baseline override values for this same synthetic_dem/boundary_coordinates,
# via the SAME underlying calls identify_road_corridor_candidates()'s own
# self-compute fallbacks make (mocks already applied above for the mandatory
# canopy gate / disqualifying-soil fetch) -- computed once here and reused
# below both as realistic override values and as identity-checkable proof
# that the self-compute fallbacks are genuinely skipped when overridden.
import road_corridors  # noqa: E402
import valley_delineation  # noqa: E402
import water_suitability  # noqa: E402
from rasterio.warp import transform as _override_warp_transform  # noqa: E402
from shapely.geometry import Polygon as _OverridePolygon  # noqa: E402

override_boundary_xs, override_boundary_ys = _override_warp_transform(
    "EPSG:4326", synthetic_dem["crs"],
    [pt[0] for pt in boundary_coordinates], [pt[1] for pt in boundary_coordinates],
)
OVERRIDE_BOUNDARY_POLYGON_UTM = _OverridePolygon(zip(override_boundary_xs, override_boundary_ys))

with mock_patch.object(production_area, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area_ceiling, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy):
    OVERRIDE_PRODUCTION_AREAS = production_area_ceiling.identify_optimized_production_areas(
        boundary_coordinates, dem=synthetic_dem
    )["scored_patches"]
    OVERRIDE_VALLEYS = valley_delineation.delineate_valleys(synthetic_dem)
    _real_selected_water_zone = water_suitability.fetch_and_select_optimal_water_zone(
        boundary_coordinates,
        dem=synthetic_dem,
        boundary_polygon_utm=OVERRIDE_BOUNDARY_POLYGON_UTM,
        valleys=OVERRIDE_VALLEYS,
        production_areas=OVERRIDE_PRODUCTION_AREAS,
    )
    OVERRIDE_HYDRIC_FLOODPLAIN_UNION, _override_is_fallback = road_corridors._fetch_floodplain_hydric_union(
        boundary_coordinates, synthetic_dem, OVERRIDE_VALLEYS, OVERRIDE_BOUNDARY_POLYGON_UTM
    )

assert isinstance(OVERRIDE_PRODUCTION_AREAS, list) and OVERRIDE_PRODUCTION_AREAS, (
    "test setup: this fixture's gentle uniform slope should produce a real, non-empty "
    "production_areas value -- see this file's own module docstring"
)
# This fixture's uniform, gentle slope never registers a real delineated valley (no local
# drainage convergence on a flat tilt -- same reasoning test_solar_suitability_pipeline.py's own
# module docstring gives for why its own uniform terrain never does either), so _fetch_
# floodplain_hydric_union()'s own fallback (buffering delineated valley lines when NHD/SSURGO
# are unreachable) has nothing to buffer and honestly returns None here -- not a test-setup
# failure, just nothing real to reuse as a direct override below. A small synthetic union,
# positioned well outside this fixture's own DEM extent so it has zero effect on routing, stands
# in instead -- same "off-extent synthetic stand-in" pattern OVERRIDE_SELECTED_WATER_ZONE below
# already uses for the same reason.
assert OVERRIDE_VALLEYS == [] and OVERRIDE_HYDRIC_FLOODPLAIN_UNION is None, (
    "test setup: this fixture's uniform slope is expected to produce no real delineated valleys "
    "and therefore no real fallback floodplain union -- if this now finds either, "
    "OVERRIDE_HYDRIC_FLOODPLAIN_UNION below should probably use the real one directly instead"
)
OVERRIDE_HYDRIC_FLOODPLAIN_UNION = Point(origin_x - 1000.0, origin_y - 2000.0).buffer(5.0)
# The real, honestly-computed selected water zone on THIS uniform-slope fixture is None (no
# candidate water-system zone survives here either, same non-ridge-shaped-terrain reasoning as
# above) -- and None is exactly the sentinel this branch's own None-falls-back-to-self-compute
# pattern treats as "not overridden", so it can't be used to prove a call was skipped. Test 1
# below needs a genuinely non-None override to prove fetch_and_select_optimal_water_zone() is
# skipped, so it uses a small synthetic zone instead, positioned well outside this fixture's own
# DEM extent so it has zero effect on routing.
assert _real_selected_water_zone is None, (
    "test setup: this uniform-slope fixture is expected to produce no real candidate water zone -- "
    "if this now finds one, OVERRIDE_SELECTED_WATER_ZONE below should probably use it directly instead"
)
OVERRIDE_SELECTED_WATER_ZONE = {
    "id": "synthetic-test-water-zone",
    "render_fill_polygon_utm": Point(origin_x - 1000.0, origin_y - 1000.0).buffer(5.0),
}


# --- 1. override behavior: all five new overrides supplied -> the -------
# --- corresponding self-compute fallbacks are never called ---------------

# The canopy mask for the SOFT road-cost term is a NEW self-fetch this
# function makes (production_area.get_required_tree_root_zone_mask_utm(),
# which internally calls production_area.get_canopy_height_for_boundary())
# that no override supplied here short-circuits -- it degrades gracefully on
# failure, but a real network attempt would make this "offline" test slow
# and nondeterministic, so it's mocked clean here purely for offline-ness
# (its own dedicated override-forwarding proof is test 6 below).
with mock_patch.object(road_corridors, "get_dem_for_boundary") as mock_dem, \
     mock_patch.object(road_corridors, "identify_optimized_production_areas") as mock_prod, \
     mock_patch.object(road_corridors, "delineate_valleys") as mock_valleys, \
     mock_patch.object(road_corridors, "fetch_and_select_optimal_water_zone") as mock_water, \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy), \
     mock_patch.object(road_corridors, "_fetch_floodplain_hydric_union") as mock_flood:
    override_result = road_corridors.identify_road_corridor_candidates(
        boundary_coordinates,
        anchor_lon_lat=anchor_lon_lat,
        dem=synthetic_dem,
        boundary_polygon_utm=OVERRIDE_BOUNDARY_POLYGON_UTM,
        production_areas=OVERRIDE_PRODUCTION_AREAS,
        valleys=OVERRIDE_VALLEYS,
        selected_water_zone=OVERRIDE_SELECTED_WATER_ZONE,
        hydric_floodplain_union=OVERRIDE_HYDRIC_FLOODPLAIN_UNION,
    )

assert mock_dem.call_count == 0, "get_dem_for_boundary() must NOT be called when dem was supplied"
assert mock_prod.call_count == 0, (
    "identify_optimized_production_areas() must NOT be called when production_areas was supplied as an override"
)
assert mock_valleys.call_count == 0, "delineate_valleys() must NOT be called when valleys was supplied as an override"
assert mock_water.call_count == 0, (
    "fetch_and_select_optimal_water_zone() must NOT be called when selected_water_zone was supplied as an override"
)
assert mock_flood.call_count == 0, (
    "_fetch_floodplain_hydric_union() must NOT be called when hydric_floodplain_union was supplied as an override"
)
validate_feature_collection(override_result["zones_geojson"])
print(
    "Supplying boundary_polygon_utm=/production_areas=/valleys=/selected_water_zone=/hydric_floodplain_union= "
    "all five as overrides (test 1) correctly skips get_dem_for_boundary()/identify_optimized_production_areas()/"
    "delineate_valleys()/fetch_and_select_optimal_water_zone()/_fetch_floodplain_hydric_union() -- zero "
    "self-compute fallback calls."
)


# --- 3. selected_water_zone NOT overridden, but boundary_polygon_utm/ ---
# --- valleys/production_areas ARE -> fetch_and_select_optimal_water_ -----
# --- zone() is called WITH those three passed through as kwargs, not -----
# --- self-deriving its own copies a third, independent time (mirrors -----
# --- test_water_suitability.py's own section 4 for this exact function) --

with mock_patch.object(road_corridors, "fetch_and_select_optimal_water_zone", return_value=None) as mock_water_passthrough, \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy):  # new soft-canopy self-fetch, mocked clean for offline-ness (see test 1)
    passthrough_result = road_corridors.identify_road_corridor_candidates(
        boundary_coordinates,
        anchor_lon_lat=anchor_lon_lat,
        dem=synthetic_dem,
        boundary_polygon_utm=OVERRIDE_BOUNDARY_POLYGON_UTM,
        valleys=OVERRIDE_VALLEYS,
        production_areas=OVERRIDE_PRODUCTION_AREAS,
    )

assert mock_water_passthrough.call_count == 1
water_call = mock_water_passthrough.call_args
assert water_call.args[0] == boundary_coordinates
assert water_call.kwargs["dem"] is synthetic_dem
assert water_call.kwargs["boundary_polygon_utm"] is OVERRIDE_BOUNDARY_POLYGON_UTM, (
    "fetch_and_select_optimal_water_zone() must receive this function's own already-sourced "
    "boundary_polygon_utm, not re-derive its own copy"
)
assert water_call.kwargs["valleys"] is OVERRIDE_VALLEYS, (
    "fetch_and_select_optimal_water_zone() must receive this function's own already-sourced valleys, "
    "not re-derive its own copy via delineate_valleys()"
)
assert water_call.kwargs["production_areas"] is OVERRIDE_PRODUCTION_AREAS, (
    "fetch_and_select_optimal_water_zone() must receive this function's own already-sourced "
    "production_areas, not re-derive its own copy via identify_optimized_production_areas()"
)
validate_feature_collection(passthrough_result["zones_geojson"])
print(
    "With selected_water_zone left unsupplied but boundary_polygon_utm=/valleys=/production_areas= all "
    "overridden (test 3), fetch_and_select_optimal_water_zone() correctly receives this function's own "
    "already-sourced values as kwargs instead of re-deriving a third, independent copy of any of them."
)


# --- 4. hydric_floodplain_union overridden directly, floodplain_data_is_ --
# --- fallback NOT specified -> defaults to False (assume real) rather ----
# --- than crashing or silently mislabeling a genuine fallback union -------
#
# Deliberately reuses OVERRIDE_HYDRIC_FLOODPLAIN_UNION -- a union that IS
# actually fallback-derived (NHD/SSURGO are unreachable in this sandbox) --
# to prove the function applies the False default purely because the caller
# supplied the union directly and stayed silent on floodplain_data_is_
# fallback, not because it re-derives or otherwise knows the union's real
# provenance.

with mock_patch.object(production_area, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area_ceiling, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy), \
     mock_patch.object(road_corridors, "_fetch_floodplain_hydric_union") as mock_flood_direct:
    direct_result = road_corridors.identify_road_corridor_candidates(
        boundary_coordinates,
        anchor_lon_lat=anchor_lon_lat,
        dem=synthetic_dem,
        hydric_floodplain_union=OVERRIDE_HYDRIC_FLOODPLAIN_UNION,
    )

assert mock_flood_direct.call_count == 0, (
    "_fetch_floodplain_hydric_union() must NOT be called when hydric_floodplain_union was supplied directly"
)
assert direct_result["zones_geojson"]["features"], "expected at least one route in this direct-override run"
for feature in direct_result["zones_geojson"]["features"]:
    notes = feature["properties"]["confidence_notes"].lower()
    assert "dem-only fallback" not in notes, (
        "floodplain_data_is_fallback must default to False when hydric_floodplain_union is supplied directly "
        "without an explicit floodplain_data_is_fallback= -- a caller-supplied union must not be silently "
        "mislabeled as the degraded valley-line fallback"
    )
print(
    "Supplying hydric_floodplain_union= directly without floodplain_data_is_fallback= (test 4) correctly "
    "defaults floodplain_data_is_fallback to False (assume real data) rather than crashing or mislabeling it "
    "as the DEM-only fallback."
)


# --- 5. REGRESSION GUARD -- floodplain_data_is_fallback=True paired with -
# --- an EXPLICIT hydric_floodplain_union= override --------------------
#
# This item was opened as "confidence_notes never gets the DEM-only-
# fallback caveat appended when the floodplain fetch degrades." Re-
# investigation (this branch's own Step 0.2) found the mechanism already
# correctly wired end-to-end -- _confidence_notes_for_route() unconditionally
# appends the caveat whenever floodplain_data_is_fallback is True,
# identify_road_corridor_candidates() already threads it through on both
# the self-compute path (test 2 above, genuinely exercised against
# unreachable NHD/SSURGO in this sandbox) and the "override supplied,
# floodplain_data_is_fallback left unset -> defaults to False" path (test 4
# above). The one combination neither of those covered -- an EXPLICIT
# hydric_floodplain_union= override paired with an EXPLICIT
# floodplain_data_is_fallback=True -- is covered here.
#
# THIS ITEM IS CLOSED, NOT OPEN: there is no production-code fix for it in
# this branch. This test exists purely as a regression guard so that if
# this specific combination ever DOES regress, it fails loudly here rather
# than silently passing by accident -- do not read the presence of this
# test as evidence the bug is still live; read a FAILURE here as evidence
# it came back.

with mock_patch.object(production_area, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area_ceiling, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy), \
     mock_patch.object(road_corridors, "_fetch_floodplain_hydric_union") as mock_flood_explicit_true:
    explicit_true_result = road_corridors.identify_road_corridor_candidates(
        boundary_coordinates,
        anchor_lon_lat=anchor_lon_lat,
        dem=synthetic_dem,
        hydric_floodplain_union=OVERRIDE_HYDRIC_FLOODPLAIN_UNION,
        floodplain_data_is_fallback=True,
    )

assert mock_flood_explicit_true.call_count == 0, (
    "_fetch_floodplain_hydric_union() must NOT be called when hydric_floodplain_union was supplied directly"
)
assert explicit_true_result["zones_geojson"]["features"], "expected at least one route in this direct-override run"
for feature in explicit_true_result["zones_geojson"]["features"]:
    notes = feature["properties"]["confidence_notes"].lower()
    assert "dem-only fallback" in notes, (
        "REGRESSION: confidence_notes must carry the DEM-only fallback caveat whenever "
        "floodplain_data_is_fallback=True, including when it arrives via an EXPLICIT override (not just the "
        "self-compute path covered by test 2) -- if this fails, the previously-suspected bug is back"
    )
print(
    "REGRESSION GUARD (test 5): confidence_notes correctly carries the DEM-only fallback caveat when "
    "floodplain_data_is_fallback=True arrives via an explicit override paired with an explicit "
    "hydric_floodplain_union= -- closing out a bug suspected earlier this session that reinvestigation found "
    "was already handled correctly."
)


# =====================================================================
# 6. canopy_height= override forwarding: identify_road_corridor_candidates()
# forwards a supplied canopy_height to THREE canopy consumers -- its
# production_areas/selected_water_zone self-compute calls (identify_
# optimized_production_areas()/fetch_and_select_optimal_water_zone()) AND
# its own direct soft-canopy road-cost consumer (_fetch_canopy_soft_cost_
# mask() -> production_area.get_required_tree_root_zone_mask_utm(), added
# this branch). Real, wraps=-based proof (the nested calls run for real,
# not stubbed away) that a supplied canopy_height reaches every one of them
# and causes production_area.get_canopy_height_for_boundary() to be called
# ZERO times end-to-end, using the SAME CanopyOverrideProbe every other
# canopy-override test in this codebase uses -- the probe sees the direct
# soft-canopy gate too and confirms it also computes on the exact supplied
# override array, not a re-fetch. Disqualifying-soil fetches stay mocked
# purely for speed/determinism, same as every other section in this file;
# NHD/SSURGO stay real/unmocked, same as test 2 above.
# =====================================================================

from _canopy_override_probe import CanopyOverrideProbe, clean_canopy_for  # noqa: E402

CANOPY_HEIGHT_OVERRIDE = clean_canopy_for(synthetic_dem)

with mock_patch.object(production_area, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area_ceiling, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(
         road_corridors, "identify_optimized_production_areas",
         wraps=road_corridors.identify_optimized_production_areas,
     ) as mock_prod_canopy, \
     mock_patch.object(
         road_corridors, "fetch_and_select_optimal_water_zone",
         wraps=road_corridors.fetch_and_select_optimal_water_zone,
     ) as mock_water_canopy:
    with CanopyOverrideProbe() as canopy_probe:
        canopy_result = road_corridors.identify_road_corridor_candidates(
            boundary_coordinates,
            anchor_lon_lat=anchor_lon_lat,
            dem=synthetic_dem,
            canopy_height=CANOPY_HEIGHT_OVERRIDE,
        )

canopy_probe.assert_override_used(CANOPY_HEIGHT_OVERRIDE, "identify_road_corridor_candidates()")
assert mock_prod_canopy.call_args.kwargs["canopy_height"] is CANOPY_HEIGHT_OVERRIDE, (
    "identify_road_corridor_candidates() must forward canopy_height to its own "
    "identify_optimized_production_areas() self-compute call by identity"
)
assert mock_water_canopy.call_args.kwargs["canopy_height"] is CANOPY_HEIGHT_OVERRIDE, (
    "identify_road_corridor_candidates() must forward canopy_height to its own "
    "fetch_and_select_optimal_water_zone() self-compute call by identity"
)
validate_feature_collection(canopy_result["zones_geojson"])
print(
    f"canopy_height= override: identify_road_corridor_candidates()'s own production_areas/selected_water_zone "
    f"self-compute calls -- run for REAL here, not stubbed away -- receive the exact caller-supplied override "
    f"and cause production_area.get_canopy_height_for_boundary() to be called ZERO times "
    f"({len(canopy_probe.mask_arrays)} real canopy gate(s) reached, every one computed on the exact supplied "
    "override array)."
)


# --- 7. REGRESSION: no canopy_height supplied -> identify_optimized_ -----
# --- production_areas()/fetch_and_select_optimal_water_zone() still ------
# --- receive canopy_height=None (the same as never having the kwarg at ---
# --- all pre-branch) -- adding the parameter must be a pure no-op when ---
# --- the caller doesn't use it. Test 2 above (identical fixture, no ------
# --- canopy_height=) already proves the actual OUTPUT is unaffected; -----
# --- this proves the WIRING itself defaults correctly too. ---------------

with mock_patch.object(production_area, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area_ceiling, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy), \
     mock_patch.object(
         road_corridors, "identify_optimized_production_areas",
         wraps=road_corridors.identify_optimized_production_areas,
     ) as mock_prod_default, \
     mock_patch.object(
         road_corridors, "fetch_and_select_optimal_water_zone",
         wraps=road_corridors.fetch_and_select_optimal_water_zone,
     ) as mock_water_default:
    road_corridors.identify_road_corridor_candidates(
        boundary_coordinates, dem=synthetic_dem, anchor_lon_lat=anchor_lon_lat,
    )

assert mock_prod_default.call_args.kwargs.get("canopy_height") is None, (
    "identify_road_corridor_candidates() with no canopy_height= supplied must still pass canopy_height=None "
    "through to identify_optimized_production_areas() -- a pure no-op default, not a behavior change"
)
assert mock_water_default.call_args.kwargs.get("canopy_height") is None, (
    "identify_road_corridor_candidates() with no canopy_height= supplied must still pass canopy_height=None "
    "through to fetch_and_select_optimal_water_zone() -- a pure no-op default, not a behavior change"
)
print(
    "REGRESSION (test 7): with no canopy_height= supplied, both self-compute calls still receive "
    "canopy_height=None -- adding the parameter is a pure no-op for existing callers."
)


# =====================================================================
# 8. No production area at all -> route_road_network()'s own "no_demand"
# path: zero branches, stop_reason="no_demand", no exception. This is a
# SEPARATE, deliberate scenario, not a bad fixture -- a parcel with no
# identified production zone has no farming and nothing this tool can
# recommend a road for, so "no production, no road" is the correct,
# honest answer, not a bug to route around. Reuses this file's own real
# synthetic_dem/boundary_coordinates/anchor_lon_lat, with production_
# areas=[] passed directly (bypassing identify_optimized_production_
# areas() entirely, same "force zero demand regardless of what the real
# terrain would otherwise produce" technique test_road_corridors.py's own
# "no production areas at all" case uses against build_road_network()
# directly) rather than needing a second synthetic terrain.
# =====================================================================

with mock_patch.object(production_area, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area_ceiling, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy):
    no_production_result = road_corridors.identify_road_corridor_candidates(
        boundary_coordinates,
        anchor_lon_lat=anchor_lon_lat,
        dem=synthetic_dem,
        production_areas=[],
    )

no_production_network = no_production_result["road_network"]
assert no_production_network["branches"] == [], "zero production demand must produce zero branches"
assert no_production_network["stop_reason"] == "no_demand", (
    f"expected stop_reason='no_demand' with zero production demand, got "
    f"{no_production_network['stop_reason']!r}"
)
assert no_production_network["total_length_meters"] == 0.0
assert no_production_network["total_served_acres"] == 0.0
assert no_production_result["selected_road_corridor"] is None, (
    "identify_road_corridor_candidates()'s own 'selected_road_corridor' key collapses an empty "
    "network to None (see that function's own docstring) -- distinct from 'road_network', which "
    "stays the real empty-network shape"
)
validate_feature_collection(no_production_result["zones_geojson"])
assert no_production_result["zones_geojson"]["features"] == []
print(
    "No production area at all (test 8) correctly returns zero branches with stop_reason='no_demand', "
    "a valid empty (not malformed) zones_geojson FeatureCollection, and raises nothing."
)


print("\nAll road corridor pipeline checks passed.")
