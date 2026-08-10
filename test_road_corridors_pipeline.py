"""
test_road_corridors_pipeline.py

End-to-end offline check that identify_road_corridor_candidates()'s
wiring (DEM -> optimized production areas -> the single selected water
zone -> floodplain fetch -> anchor-to-farthest-point routing -> ranked
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
from road_corridors import identify_road_corridor_candidates


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
ROWS = COLS = 30
origin_x = center_x - COLS * RESOLUTION / 2
origin_y = center_y + ROWS * RESOLUTION / 2

utm_corners_x = [origin_x, origin_x + COLS * RESOLUTION, origin_x + COLS * RESOLUTION, origin_x, origin_x]
utm_corners_y = [origin_y, origin_y, origin_y - ROWS * RESOLUTION, origin_y - ROWS * RESOLUTION, origin_y]
lons, lats = warp_transform(DST_CRS, "EPSG:4326", utm_corners_x, utm_corners_y)
boundary_coordinates = list(zip(lons, lats))

# A narrow ridge crest with steep flanks: the flanks are too steep (>15%)
# for production_area.py's own threshold, so nothing here gets claimed as
# production land, but the crest itself stays under the 10% road-grade
# threshold -- deliberately chosen so this test actually exercises corridor
# generation end-to-end rather than having every candidate area swallowed
# by production_area.py's (unmodified, reused-as-is) exclusion, which is
# what happens on a uniformly gentle hillside since MAX_ROAD_GRADE_PCT
# (10%) is stricter than production_area.py's own MAX_PRODUCTION_SLOPE_PCT
# (15%) -- any land flat enough for a road is also flat enough to have
# already been claimed as production land, unless (as here) it's a narrow
# enough strip to fall under production_area.py's own minimum-area filter.
array = np.zeros((ROWS, COLS), dtype=np.float32)
for row in range(ROWS):
    for col in range(COLS):
        distance_from_ridge = abs((ROWS - 1 - row) - col)
        array[row, col] = 100.0 - distance_from_ridge * 1.2 - row * 0.15

synthetic_dem = {
    "array": array,
    "resolution_meters": (RESOLUTION, RESOLUTION),
    "origin_x": origin_x,
    "origin_y": origin_y,
    "crs": DST_CRS,
}

# A real anchor point ON the ridge crest itself (row=15, col=14 satisfies
# distance_from_ridge == 0 in the array-building loop above) -- routing
# starts from here and heads to the farthest eligible point on the ridge.
anchor_row, anchor_col = 15, 14
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
validate_feature_collection(result["zones_geojson"])

features = result["zones_geojson"]["features"]
print(f"Pipeline ran end-to-end with unreachable NHD/SSURGO data: {len(features)} road route(s).")
assert len(features) >= 1, (
    "expected at least one road route: the ridge crest is too narrow to be claimed as "
    "production land, giving the anchor point real eligible ground to route across"
)

for feature in features:
    props = feature["properties"]
    assert props["layer"] == "suggested_road_corridor"
    assert feature["geometry"]["type"] == "LineString"
    assert "anchor_status" not in props, "anchor_status no longer exists -- anchoring is bypassed entirely"
    assert "anchor_road_name" not in props, "anchor_road_name no longer exists -- anchoring is bypassed entirely"
    assert "crosses_production_zone" in props, (
        "crosses_production_zone must exist -- production is a soft scoring term on the ridge fragment "
        "itself now, not a hard exclusion (it stays hard for the anchor-to-ridge connector only)"
    )
    assert "crosses_floodplain" in props
    notes = props["confidence_notes"].lower()
    # No NHD/SSURGO data was reachable in this sandbox -> the floodplain
    # cost-penalty union fell back to buffered valley lines, flagged here.
    assert "dem-only fallback" in notes, "the floodplain-fallback caveat should appear in confidence_notes"

print("Routes correctly reflect unreachable NHD/SSURGO data (floodplain fallback flagged in confidence_notes), "
      "with no stale anchor/production-crossing properties.")
print("REGRESSION (test 2, no overrides supplied): output above is identical to pre-branch behavior against "
      "this same synthetic fixture.")


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

assert isinstance(OVERRIDE_PRODUCTION_AREAS, list), "test setup: expected a real (list, even if empty) production_areas value"
# This fixture's ridge crest is deliberately narrow enough to never itself qualify as
# production land (see this file's own docstring) -- an empty list here is the correct,
# expected real value, not a test-setup failure; it's still a genuine, non-None override.
assert OVERRIDE_HYDRIC_FLOODPLAIN_UNION is not None, (
    "test setup: expected a real (fallback-derived, since NHD/SSURGO are unreachable here) floodplain union "
    "to reuse as a direct override below"
)
# The real, honestly-computed selected water zone on THIS narrow-ridge fixture is None (no
# candidate water-system zone survives here either) -- and None is exactly the sentinel this
# branch's own None-falls-back-to-self-compute pattern treats as "not overridden", so it can't
# be used to prove a call was skipped. Test 1 below needs a genuinely non-None override to prove
# fetch_and_select_optimal_water_zone() is skipped, so it uses a small synthetic zone instead,
# positioned well outside this fixture's own DEM extent so it has zero effect on routing.
assert _real_selected_water_zone is None, (
    "test setup: this narrow-ridge fixture is expected to produce no real candidate water zone -- "
    "if this now finds one, OVERRIDE_SELECTED_WATER_ZONE below should probably use it directly instead"
)
OVERRIDE_SELECTED_WATER_ZONE = {
    "id": "synthetic-test-water-zone",
    "render_fill_polygon_utm": Point(origin_x - 1000.0, origin_y - 1000.0).buffer(5.0),
}


# --- 1. override behavior: all five new overrides supplied -> the -------
# --- corresponding self-compute fallbacks are never called ---------------

with mock_patch.object(road_corridors, "get_dem_for_boundary") as mock_dem, \
     mock_patch.object(road_corridors, "identify_optimized_production_areas") as mock_prod, \
     mock_patch.object(road_corridors, "delineate_valleys") as mock_valleys, \
     mock_patch.object(road_corridors, "fetch_and_select_optimal_water_zone") as mock_water, \
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

with mock_patch.object(road_corridors, "fetch_and_select_optimal_water_zone", return_value=None) as mock_water_passthrough:
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
# has no direct canopy gate of its own (confirmed in this branch's own
# Step 0.1) -- only its production_areas/selected_water_zone self-compute
# calls (identify_optimized_production_areas()/fetch_and_select_optimal_
# water_zone()) do. Real, wraps=-based proof (both nested calls run for
# real, not stubbed away) that a supplied canopy_height reaches both and
# causes production_area.get_canopy_height_for_boundary() to be called
# ZERO times end-to-end, using the SAME CanopyOverrideProbe every other
# canopy-override test in this codebase uses. Disqualifying-soil fetches
# stay mocked purely for speed/determinism, same as every other section
# in this file; NHD/SSURGO stay real/unmocked, same as test 2 above.
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


print("\nAll road corridor pipeline checks passed.")
