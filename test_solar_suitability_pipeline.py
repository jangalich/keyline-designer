"""
test_solar_suitability_pipeline.py

End-to-end offline check that identify_solar_candidate_zones()'s wiring
(DEM -> production areas -> roads -> SSURGO -> point-candidate scoring ->
ranked GeoJSON) fits together, using a hand-built synthetic DEM passed
via `dem=` (same approach as test_water_system_candidate_pipeline.py).

The farm-roads and SSURGO fetches inside identify_solar_candidate_zones()
are real network calls this sandbox can't reach — that's fine and by
design: both are wrapped in their own try/except and degrade gracefully
(no road data and no selected road corridor -> the road-proximity
constraint is disabled entirely; no SSURGO data -> no
prime_farmland_conflict property), the same "a network layer fetch
failing doesn't take down the whole feature" pattern the rest of this
pipeline already uses. So this test doubles as a live check of that
graceful-degradation path, not just the DEM/production-area wiring.

The mandatory canopy exclusion fetch (production_area.
get_required_tree_root_zone_mask_utm(), called unconditionally by
identify_solar_candidate_zones() -- see that module's own docstring for
why this one specifically does NOT degrade) is a real network call this
sandbox can't reach either, but does NOT degrade -- an unmocked run would
hard-fail before producing any candidates at all. It's mocked here with a
synthetic "no trees anywhere" canopy result, same convention
test_road_corridors_pipeline.py / test_production_area_ceiling.py /
test_water_suitability.py already use for this exact mandatory gate.

Terrain is deliberately simple here: a uniform gentle south-facing slope
across the whole grid. Under the OLD eligible-area/exclusion model this
would have been a poor test fixture (the whole grid would register as
ONE production zone and hard-exclude the whole thing, leaving nothing
for solar) — that's exactly the live bug the point-candidate redesign
fixes (see solar_suitability.py's module docstring), so this simple
layout now deliberately doubles as an end-to-end confirmation that
candidates form on/near production land instead of being swallowed by
it, through the real identify_production_areas()/identify_solar_candidate_zones()
wiring, not just the pure function test_solar_suitability.py already
covers directly.

This same uniform, gentle, non-ridge-shaped terrain never registers any
delineated valleys and never gives find_road_routes() a ridge to route
along, so the real (non-overridden) selected_water_zone and
selected_road_corridor on this fixture are always None -- same situation
test_road_corridors_pipeline.py's own narrow-ridge fixture hit for its
water zone. The override sections below fabricate small synthetic
zone/corridor dicts positioned off in open space near this fixture's own
DEM extent (not overlapping any real candidate) purely to have a real,
non-None value to prove the self-compute fallbacks are skipped -- same
pattern test_road_corridors_pipeline.py already established.

This same uniform terrain ALSO hits the exact "whole grid reads as one
zone" failure mode tree_zone_candidates.py's own docstring warns about
(same shape production_area.py's zone model used to have, before the
point-candidate redesign this module's own docstring describes): on this
fixture, identify_tree_zone_candidates() claims essentially the entire
parcel as a single ranked tree-zone candidate, and solar_suitability.py's
own (unchanged, out-of-scope-for-this-branch -- see solar_suitability.py's
own module docstring) hard exclusion against that union then wipes out
every solar candidate outright. That exclusion is real and correct
production behavior, just not what THIS pipeline test exists to exercise
(that's test_tree_zone_candidates.py's job) -- identify_tree_zone_
candidates() is mocked out below to a real, empty 'patches' result, the
same "neutralize an orthogonal exclusion this fixture wasn't designed to
account for" reasoning already applied to the mandatory canopy mock above.
"""

from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Point, Polygon, box

import production_area
import production_area_ceiling
import solar_suitability
from dem_data import _utm_epsg_for_lonlat
from feature_schema import validate_feature_collection
from solar_suitability import identify_solar_candidate_zones


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
SIZE = 60  # 300m x 300m, big enough for CANDIDATE_POINT_SPACING_METERS (75m) to sample several points
HALF_EXTENT = SIZE * RESOLUTION / 2
origin_x = center_x - HALF_EXTENT
origin_y = center_y + HALF_EXTENT

utm_corners_x = [origin_x, origin_x + SIZE * RESOLUTION, origin_x + SIZE * RESOLUTION, origin_x, origin_x]
utm_corners_y = [origin_y, origin_y, origin_y - SIZE * RESOLUTION, origin_y - SIZE * RESOLUTION, origin_y]
lons, lats = warp_transform(DST_CRS, "EPSG:4326", utm_corners_x, utm_corners_y)
boundary_coordinates = list(zip(lons, lats))

# A uniform ~4% south-facing grade across the entire grid -- flat/gentle
# enough that production_area.py claims essentially the whole parcel as
# one production zone.
array = np.zeros((SIZE, SIZE), dtype=np.float32)
for row in range(SIZE):
    array[row, :] = 100.0 - row * 0.2

synthetic_dem = {
    "array": array,
    "resolution_meters": (RESOLUTION, RESOLUTION),
    "origin_x": origin_x,
    "origin_y": origin_y,
    "crs": DST_CRS,
}

# identify_solar_candidate_zones() calls production_area.identify_production_areas()
# internally (unchanged wiring), which -- post-consolidation -- now does its own
# disqualifying-soil fetch by default (check_soil=True), gracefully degrading on
# failure same as every other optional network layer already exercised below. Mocked
# here purely to keep this "offline" pipeline test fast and deterministic instead of
# depending on how quickly a given environment's proxy rejects the call. The mandatory
# canopy fetch is mocked too -- see this file's own docstring for why that one MUST be
# mocked rather than left to degrade. identify_tree_zone_candidates() is mocked to a
# real, empty result too -- on this uniform fixture it otherwise claims the whole
# parcel as one tree-zone candidate and its exclusion buffer wipes out every solar
# candidate, an orthogonal exclusion this pipeline test isn't meant to exercise (see
# this file's own docstring).
with mock_patch.object(production_area, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area_ceiling, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy), \
     mock_patch.object(solar_suitability, "identify_tree_zone_candidates", return_value={"patches": []}):
    result = identify_solar_candidate_zones(boundary_coordinates, dem=synthetic_dem)

assert "zones_geojson" in result
validate_feature_collection(result["zones_geojson"])

features = result["zones_geojson"]["features"]
print(f"Pipeline ran end-to-end with unreachable road/SSURGO data: {len(features)} solar candidate(s).")
assert len(features) >= 1, (
    "expected at least one solar candidate: this gentle, uniform terrain should clear the slope/"
    "aspect/shading suitability floor everywhere sampled"
)

relationships_seen = {f["properties"]["production_zone_relationship"] for f in features}
assert "inside" in relationships_seen, (
    "expected at least one candidate classified 'inside' the (whole-parcel) production zone here -- "
    "this is the exact scenario the point-candidate redesign exists to enable; under the old "
    "exclusion model this synthetic property would have produced ZERO solar candidates"
)
print(f"production_zone_relationship values observed: {sorted(relationships_seen)} -- "
      f"candidates correctly form inside/near production land instead of being excluded by it.")

for feature in features:
    props = feature["properties"]
    assert props["layer"] == "solar_infrastructure"
    assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert props["footprint_area_acres"] > 0
    assert "prime_farmland_conflict" not in props, (
        "with no reachable SSURGO data, candidates shouldn't carry a fabricated farmland conflict flag"
    )

# No anchor_lon_lat was supplied -> Tier 1 (identify_road_corridor_candidates())
# degrades immediately to selected_road_corridor=None with no network call at all
# (see that function's own docstring) -> Tier 1 produces zero candidates -> Tier 2's
# own farm-roads fetch is attempted for real and fails in this sandbox (unreachable
# network) -> the road-proximity constraint is disabled entirely for the final pass.
road_sources_seen = {f["properties"]["road_proximity_source"] for f in features}
assert road_sources_seen == {"unavailable"}, (
    f"expected every candidate's road_proximity_source to be 'unavailable' (no anchor -> no selected "
    f"corridor, and real road data is unreachable in this sandbox), got {road_sources_seen}"
)
road_distances = {f["properties"]["distance_to_road_ft"] for f in features}
assert road_distances == {None}, (
    f"expected distance_to_road_ft to be null for every candidate with the road constraint fully "
    f"disabled, got {road_distances}"
)
for feature in features:
    notes = feature["properties"]["confidence_notes"]
    assert "disabled entirely" in notes, (
        "confidence_notes should flag that the road-proximity constraint was disabled entirely "
        "when neither a selected corridor nor real mapped road data was available"
    )
print(f"With no anchor_lon_lat and no reachable existing-road data, distance_to_road_ft correctly "
      f"comes back null for every candidate, flagged in confidence_notes as the road-proximity "
      f"constraint being disabled entirely (road_proximity_source={sorted(road_sources_seen)}).")
print("REGRESSION (test 2, no overrides supplied): output above is identical to pre-branch behavior "
      "against this same synthetic fixture (once the mandatory canopy gate is mocked, which is required "
      "for ANY run of this fixture to complete in a network-restricted sandbox, override work or not).")


# --- boundary_polygon_utm/production_areas/valleys/selected_water_zone/ --
# --- selected_road_corridor overrides -------------------------------------
#
# Real baseline override values for this same synthetic_dem/boundary_coordinates,
# via the SAME underlying calls identify_solar_candidate_zones()'s own
# self-compute fallbacks make (mocks already applied above for the mandatory
# canopy gate / disqualifying-soil fetch) -- computed once here and reused
# below both as realistic override values and as identity-checkable proof
# that the self-compute fallbacks are genuinely skipped when overridden.
import valley_delineation  # noqa: E402
import water_suitability  # noqa: E402

override_boundary_xs, override_boundary_ys = warp_transform(
    "EPSG:4326", DST_CRS, [pt[0] for pt in boundary_coordinates], [pt[1] for pt in boundary_coordinates],
)
OVERRIDE_BOUNDARY_POLYGON_UTM = Polygon(zip(override_boundary_xs, override_boundary_ys))

with mock_patch.object(production_area, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area_ceiling, "_fetch_disqualifying_soil_union", return_value=None), \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy):
    OVERRIDE_PRODUCTION_AREAS = production_area_ceiling.identify_optimized_production_areas(
        boundary_coordinates, dem=synthetic_dem
    )["scored_patches"]
    OVERRIDE_VALLEYS = valley_delineation.delineate_valleys(synthetic_dem)
    _real_selected_water_zone = water_suitability.identify_water_suitability(
        boundary_coordinates,
        dem=synthetic_dem,
        boundary_polygon_utm=OVERRIDE_BOUNDARY_POLYGON_UTM,
        valleys=OVERRIDE_VALLEYS,
        production_areas=OVERRIDE_PRODUCTION_AREAS,
    )["selected_water_zone"]

assert isinstance(OVERRIDE_PRODUCTION_AREAS, list) and OVERRIDE_PRODUCTION_AREAS, (
    "test setup: expected at least one real production patch on this uniform gentle-slope fixture "
    "to reuse as a direct override below"
)
# This fixture's uniform, non-ridge-shaped terrain never delineates a real valley and never gives
# find_road_routes() a ridge to route along, so the real, honestly-computed selected water zone and
# selected road corridor on THIS fixture are always None -- neither can be used to prove a call was
# skipped (None is exactly the sentinel this branch's own None-falls-back-to-self-compute pattern
# treats as "not overridden"). Both are fabricated as small synthetic values below instead, positioned
# well outside this fixture's own DEM/boundary extent (or, for the road corridor, deliberately spanning
# it) so they have a real, checkable effect without depending on this terrain accidentally producing one.
assert _real_selected_water_zone is None, (
    "test setup: this uniform gentle-slope fixture is expected to produce no real candidate water zone "
    "-- if this now finds one, OVERRIDE_SELECTED_WATER_ZONE below should probably use it directly instead"
)
OVERRIDE_SELECTED_WATER_ZONE = {
    "id": "synthetic-test-water-zone",
    "render_fill_polygon_utm": Point(origin_x - 1000.0, origin_y - 1000.0).buffer(5.0),
}

# A thin strip crossing the full width of the DEM at mid-height -- close enough to real
# candidate footprints for several of them to clear Tier 1's ROAD_CORRIDOR_PROXIMITY_METERS
# (15m) gate, so tests below that supply this override don't spuriously fall through to
# Tier 2's own real (network-backed) farm-roads fetch.
OVERRIDE_SELECTED_ROAD_CORRIDOR = {
    "id": "synthetic-test-road-corridor",
    "cell_footprint_polygon_utm": box(origin_x, center_y - 2.0, origin_x + SIZE * RESOLUTION, center_y + 2.0),
}


# --- 1. override behavior: all five new overrides supplied -> the -------
# --- corresponding self-compute fallbacks are never called ---------------

with mock_patch.object(solar_suitability, "get_dem_for_boundary") as mock_dem, \
     mock_patch.object(solar_suitability, "identify_optimized_production_areas") as mock_prod, \
     mock_patch.object(solar_suitability, "identify_water_suitability") as mock_water, \
     mock_patch.object(solar_suitability, "identify_road_corridor_candidates") as mock_corridor, \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy), \
     mock_patch.object(solar_suitability, "identify_tree_zone_candidates", return_value={"patches": []}):
    override_result = solar_suitability.identify_solar_candidate_zones(
        boundary_coordinates,
        dem=synthetic_dem,
        boundary_polygon_utm=OVERRIDE_BOUNDARY_POLYGON_UTM,
        production_areas=OVERRIDE_PRODUCTION_AREAS,
        valleys=OVERRIDE_VALLEYS,
        selected_water_zone=OVERRIDE_SELECTED_WATER_ZONE,
        selected_road_corridor=OVERRIDE_SELECTED_ROAD_CORRIDOR,
        check_prime_farmland=False,
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
override_features = override_result["zones_geojson"]["features"]
assert override_features, "expected at least one solar candidate using the synthetic road-corridor override"
for feature in override_features:
    assert feature["properties"]["road_proximity_source"] == "selected_road_corridor"
print(
    "Supplying boundary_polygon_utm=/production_areas=/valleys=/selected_water_zone=/selected_road_corridor= "
    "all five as overrides (test 1) correctly skips get_dem_for_boundary()/identify_optimized_production_areas()/"
    "identify_water_suitability()/identify_road_corridor_candidates() -- zero self-compute fallback calls."
)


# --- 3. selected_water_zone and selected_road_corridor NOT overridden, ---
# --- but boundary_polygon_utm/production_areas/valleys ARE -> both -------
# --- identify_water_suitability() and identify_road_corridor_candidates() -
# --- are called WITH those three passed through as kwargs, not -----------
# --- self-deriving their own independent copies ---------------------------

with mock_patch.object(
        solar_suitability, "identify_water_suitability", return_value={"selected_water_zone": None},
     ) as mock_water_passthrough, \
     mock_patch.object(
        solar_suitability, "identify_road_corridor_candidates",
        return_value={"selected_road_corridor": OVERRIDE_SELECTED_ROAD_CORRIDOR},
     ) as mock_corridor_passthrough, \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy), \
     mock_patch.object(solar_suitability, "identify_tree_zone_candidates", return_value={"patches": []}):
    passthrough_result = solar_suitability.identify_solar_candidate_zones(
        boundary_coordinates,
        dem=synthetic_dem,
        boundary_polygon_utm=OVERRIDE_BOUNDARY_POLYGON_UTM,
        production_areas=OVERRIDE_PRODUCTION_AREAS,
        valleys=OVERRIDE_VALLEYS,
        check_prime_farmland=False,
    )

assert mock_water_passthrough.call_count == 1
water_call = mock_water_passthrough.call_args
assert water_call.args[0] == boundary_coordinates
assert water_call.kwargs["dem"] is synthetic_dem
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
assert corridor_call.args[0] == boundary_coordinates
assert corridor_call.kwargs["dem"] is synthetic_dem
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
# --- (simulating "no corridor exists") -> Tier 1/Tier 2 fallback logic ---
# --- still fires correctly, unaffected by the override mechanism ---------
#
# A caller-supplied None is indistinguishable from "not supplied" (same None-as-
# sentinel convention every override in this function uses -- see its own
# docstring), so this still triggers the self-compute call below; what this test
# actually proves is that the UNCHANGED Tier 1/Tier 2 branching immediately after
# it keeps working correctly regardless of whether the None it's branching on
# came from an override argument or a genuine self-compute result.

with mock_patch.object(
        solar_suitability, "identify_road_corridor_candidates", return_value={"selected_road_corridor": None},
     ) as mock_corridor_none, \
     mock_patch.object(
        solar_suitability, "get_farm_roads_for_boundary",
        return_value=[{"name": "Synthetic Rd", "geometry": {
            "type": "LineString",
            "coordinates": [[lons[0], lats[0]], [lons[2], lats[2]]],
        }}],
     ) as mock_farm_roads, \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy), \
     mock_patch.object(solar_suitability, "identify_tree_zone_candidates", return_value={"patches": []}):
    tier2_result = solar_suitability.identify_solar_candidate_zones(
        boundary_coordinates,
        dem=synthetic_dem,
        boundary_polygon_utm=OVERRIDE_BOUNDARY_POLYGON_UTM,
        production_areas=OVERRIDE_PRODUCTION_AREAS,
        valleys=OVERRIDE_VALLEYS,
        selected_water_zone=OVERRIDE_SELECTED_WATER_ZONE,
        selected_road_corridor=None,
        check_prime_farmland=False,
    )

assert mock_corridor_none.call_count == 1, (
    "an explicit selected_road_corridor=None must still be treated as unsupplied and trigger the "
    "identify_road_corridor_candidates() self-compute, same as leaving it out entirely"
)
assert mock_farm_roads.call_count == 1, (
    "Tier 2's own farm-roads fallback must still fire when Tier 1's (self-computed) "
    "selected_road_corridor comes back None -- unaffected by the override mechanism"
)
tier2_features = tier2_result["zones_geojson"]["features"]
assert tier2_features, "expected at least one candidate via the Tier 2 real-mapped-road fallback"
for feature in tier2_features:
    assert feature["properties"]["road_proximity_source"] == "real_mapped_road"
print(
    "Supplying selected_road_corridor=None explicitly (test 4) is correctly treated the same as leaving "
    "it unsupplied -- Tier 1 self-computes (finding no corridor), and the unchanged Tier 2 real-mapped-"
    "road fallback logic still fires correctly on top of it."
)


# --- canopy_height forwarding into the nested identify_road_corridor_ ----
# --- candidates() self-compute call specifically --------------------------
#
# identify_solar_candidate_zones() already forwards its own canopy_height
# override into its own mandatory Step gate / identify_optimized_production_
# areas()/identify_water_suitability() (unchanged, out of scope here); this
# proves the one remaining nested call -- identify_road_corridor_candidates(),
# fired here (Tier 1) when selected_road_corridor isn't itself supplied --
# also receives it as a kwarg, not silently omitting it. Mocking the nested
# call directly and checking its call kwargs by identity is required here:
# identify_road_corridor_candidates() has no canopy gate of its own (see
# road_corridors.py's own docstring), it only forwards canopy_height into ITS
# OWN nested production_areas/selected_water_zone self-computes -- both
# already resolved (non-None, via the OVERRIDE_* fixtures below) by the time
# this call fires, so those nested self-computes never run and no downstream
# effect could otherwise reveal whether the kwarg itself was forwarded.

OVERRIDE_CANOPY_HEIGHT = {
    "array": np.full(synthetic_dem["array"].shape, 1.0, dtype=np.float32),
    "resolution_meters": synthetic_dem["resolution_meters"],
    "origin_x": synthetic_dem["origin_x"],
    "origin_y": synthetic_dem["origin_y"],
    "crs": synthetic_dem["crs"],
    "source_item_id": "offline-canopy-forwarding-probe",
}

with mock_patch.object(
        solar_suitability, "identify_road_corridor_candidates", return_value={"selected_road_corridor": None},
     ) as mock_corridor_canopy, \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy), \
     mock_patch.object(solar_suitability, "identify_tree_zone_candidates", return_value={"patches": []}):
    solar_suitability.identify_solar_candidate_zones(
        boundary_coordinates,
        dem=synthetic_dem,
        boundary_polygon_utm=OVERRIDE_BOUNDARY_POLYGON_UTM,
        production_areas=OVERRIDE_PRODUCTION_AREAS,
        valleys=OVERRIDE_VALLEYS,
        selected_water_zone=OVERRIDE_SELECTED_WATER_ZONE,
        canopy_height=OVERRIDE_CANOPY_HEIGHT,
        check_prime_farmland=False,
    )

assert mock_corridor_canopy.call_count == 1
canopy_corridor_call = mock_corridor_canopy.call_args
assert canopy_corridor_call.kwargs["canopy_height"] is OVERRIDE_CANOPY_HEIGHT, (
    "identify_road_corridor_candidates() must receive this function's own canopy_height override as a "
    "kwarg (identity-checked), not omit it and leave the nested call to self-fetch its own copy"
)
print(
    "identify_solar_candidate_zones(): a supplied canopy_height override correctly reaches the nested "
    "identify_road_corridor_candidates() self-compute call as a kwarg, identity-checked."
)

# REGRESSION: canopy_height left unsupplied -> identify_road_corridor_candidates() still receives it
# explicitly as None (its own default), same as every other forwarded-but-unsupplied override.
with mock_patch.object(
        solar_suitability, "identify_road_corridor_candidates", return_value={"selected_road_corridor": None},
     ) as mock_corridor_no_canopy, \
     mock_patch.object(production_area, "get_canopy_height_for_boundary", _fake_clean_canopy), \
     mock_patch.object(solar_suitability, "identify_tree_zone_candidates", return_value={"patches": []}):
    solar_suitability.identify_solar_candidate_zones(
        boundary_coordinates,
        dem=synthetic_dem,
        boundary_polygon_utm=OVERRIDE_BOUNDARY_POLYGON_UTM,
        production_areas=OVERRIDE_PRODUCTION_AREAS,
        valleys=OVERRIDE_VALLEYS,
        selected_water_zone=OVERRIDE_SELECTED_WATER_ZONE,
        check_prime_farmland=False,
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


print("\nAll solar suitability pipeline checks passed.")
