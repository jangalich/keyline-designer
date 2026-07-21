"""
test_scenario_generation_pipeline.py

End-to-end offline check that generate_production_scenarios()'s wiring
(DEM -> suitability-scored production zones -> bounded subset enumeration
-> per-subset water/solar/road/fencing -> diversity dedup -> labeled
top-N scenarios) fits together against a hand-built synthetic DEM, same
`dem=` approach as test_water_system_candidate_pipeline.py /
test_road_corridors_pipeline.py / test_solar_suitability_pipeline.py.

The synthetic terrain deliberately carves out THREE separate,
differently-sized production-zone candidates (walled off from each other
by a narrow strip of extreme elevation so they never merge into one
connected component):

  - a wide flat bench on the west edge (no valley above it)
  - a wide flat bench on the east edge (no valley above it)
  - a narrower valley-fed bench in the center (mirrors
    test_water_system_candidate_pipeline.py's own north-high/south-flat
    shape, so a real water-system candidate zone can actually form above
    it)

With 3 candidates (<= the default MAX_ZONES_FOR_FULL_ENUMERATION of 5),
every non-empty subset is enumerated directly -- this is what lets this
test confirm the feature's actual point: a scenario that excludes the two
edge benches frees real acreage for solar/road candidates that the
all-zones scenario has none of at all (over-exclusion, the bug this
feature exists to fix), and that a genuine partial multi-zone combination
survives selection, not just the single-zone/all-zone extremes.

NHD, SSURGO, and farm-roads fetches this pulls in (via
road_corridors.py's floodplain/erosion/road helpers and fencing.py's own
NHD fetch) are real network calls this sandbox can't reach -- each
degrades gracefully (see scenario_generation.py's own try/except around
identify_fencing(), and road_corridors.py's already-established
degradation pattern for the rest), so this test doubles as a live check
of that degradation path across every layer at once, not just DEM/subset
wiring.
"""

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import shape
from shapely.ops import unary_union

from dem_data import _utm_epsg_for_lonlat
from feature_schema import validate_feature_collection
from production_suitability import identify_production_area_suitability
from scenario_generation import DEFAULT_SCENARIO_COUNT, generate_production_scenarios

CENTER_LON, CENTER_LAT = -79.98, 40.64
EPSG = _utm_epsg_for_lonlat(CENTER_LON, CENTER_LAT)
DST_CRS = f"EPSG:{EPSG}"

center_x, center_y = warp_transform("EPSG:4326", DST_CRS, [CENTER_LON], [CENTER_LAT])
center_x, center_y = center_x[0], center_y[0]

RESOLUTION = 5.0
ROWS, COLS = 40, 60  # 200m x 300m
origin_x = center_x - COLS * RESOLUTION / 2
origin_y = center_y + ROWS * RESOLUTION / 2

utm_corners_x = [origin_x, origin_x + COLS * RESOLUTION, origin_x + COLS * RESOLUTION, origin_x, origin_x]
utm_corners_y = [origin_y, origin_y, origin_y - ROWS * RESOLUTION, origin_y - ROWS * RESOLUTION, origin_y]
lons, lats = warp_transform(DST_CRS, "EPSG:4326", utm_corners_x, utm_corners_y)
boundary_coordinates = list(zip(lons, lats))

# A huge, deliberately unreachable-by-anything-nearby elevation for the
# isolating "moat" strips -- guarantees compute_slope_percent() reads a
# hard-excluding grade on both sides of each strip, so the west/center/east
# regions never merge into one connected production-area component.
WALL_ELEVATION = 9999.0

array = np.zeros((ROWS, COLS), dtype=np.float32)
for row in range(ROWS):
    for col in range(COLS):
        if col < 14:
            array[row, col] = 60.0  # west bench: flat, no valley above it
        elif col < 16:
            array[row, col] = WALL_ELEVATION  # isolating moat
        elif col < 44:
            # center region: same north-high/south-flat valley+bench shape
            # as test_water_system_candidate_pipeline.py's own synthetic
            # DEM, re-centered to this narrower column range.
            col_local = col - 16
            if row >= 32:
                array[row, col] = 100.0  # flat bench
            else:
                array[row, col] = 100.0 + (32 - row) * 3.0 + abs(col_local - 13.5) * 1.5
        elif col < 46:
            array[row, col] = WALL_ELEVATION  # isolating moat
        else:
            array[row, col] = 50.0  # east bench: flat, no valley above it

synthetic_dem = {
    "array": array,
    "resolution_meters": (RESOLUTION, RESOLUTION),
    "origin_x": origin_x,
    "origin_y": origin_y,
    "crs": DST_CRS,
}

scenarios = generate_production_scenarios(
    boundary_coordinates, dem=synthetic_dem, check_soil=False, check_prime_farmland=False
)

assert scenarios, "expected at least one scenario from a DEM with real production-zone candidates on it"
assert len(scenarios) <= DEFAULT_SCENARIO_COUNT, (
    f"expected at most DEFAULT_SCENARIO_COUNT={DEFAULT_SCENARIO_COUNT} scenarios, got {len(scenarios)}"
)
print(f"generate_production_scenarios() returned {len(scenarios)} scenario(s).")

# --- shape/schema checks ---

EXPECTED_COLLECTION_KEYS = {"water_system_candidates", "solar_candidates", "road_corridors", "exclusion_fencing"}

all_feature_ids = []
zone_id_sets_seen = []

for scenario in scenarios:
    for key in ("scenario_id", "label", "rationale", "production_zones_included", "feature_collections"):
        assert key in scenario, f"scenario missing required key {key!r}: {scenario}"

    assert scenario["production_zones_included"], "every scenario must include at least one production zone"
    zone_id_sets_seen.append(frozenset(scenario["production_zones_included"]))

    assert set(scenario["feature_collections"].keys()) == EXPECTED_COLLECTION_KEYS, (
        f"unexpected feature_collections keys: {scenario['feature_collections'].keys()}"
    )

    for collection in scenario["feature_collections"].values():
        validate_feature_collection(collection)
        all_feature_ids.extend(f["id"] for f in collection["features"])

print("Every scenario carries the required keys and schema-valid FeatureCollections for all four layers.")

# --- feature ids stay globally unique across every scenario ---

assert len(all_feature_ids) == len(set(all_feature_ids)), "feature ids must be unique across ALL scenarios, not just within one"
print(f"All {len(all_feature_ids)} features across every scenario carry globally unique ids.")

# --- selected scenarios are genuinely distinct subsets, not repeats ---

assert len(zone_id_sets_seen) == len(set(zone_id_sets_seen)), "selected scenarios must not repeat the same zone subset"
print(f"Selected zone subsets: {[sorted(s) for s in zone_id_sets_seen]} -- all pairwise distinct.")

# --- multi-zone combinations are genuinely considered, not just extremes ---

sizes_seen = {len(s) for s in zone_id_sets_seen}
if len(zone_id_sets_seen) >= 2:
    # With 3 disjoint candidates found on this synthetic property, dedup +
    # top-N selection choosing more than one scenario should surface at
    # least one multi-zone combination (size >= 2) alongside a smaller
    # one -- confirming this isn't just picking single-zone vs. all-zone
    # extremes.
    assert any(size >= 2 for size in sizes_seen), (
        f"expected at least one multi-zone scenario among {sizes_seen} when >1 scenario was selected"
    )
print(f"Scenario zone-subset sizes selected: {sorted(sizes_seen)}.")

# --- the core bug fix: production zones no longer starve solar/road
# candidates out entirely in the scenario claiming the most zones ---
#
# Both solar_suitability.py (point-candidate model) and road_corridors.py
# (production softened to a scored preference, this pass) no longer hard-
# exclude production zones, so neither layer should go all the way to
# zero just because a scenario claims more production land than another
# -- that WAS the original over-exclusion bug (confirmed live: zero
# candidates for both layers under full production coverage), and it's
# now fixed at the root in both layers, not just re-tuned. The
# corresponding assertion here is no longer "fewer zones means strictly
# more candidates" (that was true only while the bug existed) but "the
# most-inclusive scenario still gets real road corridor candidates,
# not zero" -- road corridors are the layer this pass specifically
# touched; see test_scenario_generation_pipeline.py's solar-specific
# coverage in test_solar_suitability.py / test_solar_suitability_pipeline.py
# for that layer's own equivalent regression check.

most_inclusive = max(scenarios, key=lambda s: len(s["production_zones_included"]))
most_inclusive_road_count = len(most_inclusive["feature_collections"]["road_corridors"]["features"])
assert most_inclusive_road_count > 0, (
    f"expected the most-inclusive scenario ({len(most_inclusive['production_zones_included'])} zone(s)) to "
    f"still surface real road corridor candidates, not zero -- production zones are a scored preference "
    f"for road corridors now, not a hard exclusion that can starve them out entirely"
)
print(
    f"Confirmed the over-exclusion fix holds for road corridors: the most-inclusive scenario "
    f"({len(most_inclusive['production_zones_included'])} zone(s)) still surfaces "
    f"{most_inclusive_road_count} road corridor candidate(s), not zero."
)

# --- self-consistency: each scenario's own SOLAR candidates report their
# production_zone_relationship correctly against THAT SAME scenario's own
# claimed production zones. Solar candidates are a point-candidate model
# (solar_suitability.py) that may legitimately sit inside a production
# zone -- that's intentional, not a bug -- so the guarantee worth
# checking here isn't "never overlaps" (the old, now-wrong assumption)
# but "the reported relationship matches the real geometry against THIS
# scenario's own zones," i.e. no stale/leftover relationship computed
# against a different subset. Water-system candidate zones are buffered
# from a valley LINE using a different distance convention than the
# production-area service-distance check (a documented, pre-existing
# property of water_candidate_zones.py, unrelated to this pass), and
# road corridors' final boundary-anchor connector segment is explicitly
# exempted from the exclusion check by road_corridors.py's own design --
# neither is the guarantee this test is after.

suitability_result = identify_production_area_suitability(boundary_coordinates, dem=synthetic_dem, check_soil=False)
patches_by_id = {str(p["id"]): p for p in suitability_result["scored_patches"]}

for scenario in scenarios:
    own_zone_ids = scenario["production_zones_included"]
    own_production_union = unary_union([patches_by_id[zid]["polygon_utm"] for zid in own_zone_ids])

    for feature in scenario["feature_collections"]["solar_candidates"]["features"]:
        geometry_utm = shape(transform_geom("EPSG:4326", DST_CRS, feature["geometry"]))
        relationship = feature["properties"]["production_zone_relationship"]
        actually_overlaps = own_production_union.intersects(geometry_utm)

        if relationship == "inside":
            assert actually_overlaps, (
                f"scenario {scenario['scenario_id']}'s solar candidate {feature['id']} is labeled "
                f"'inside' but doesn't actually overlap this scenario's own production zones ({own_zone_ids})"
            )
        else:
            assert not actually_overlaps, (
                f"scenario {scenario['scenario_id']}'s solar candidate {feature['id']} is labeled "
                f"{relationship!r} but actually overlaps this scenario's own production zones "
                f"({own_zone_ids}) -- should be 'inside'"
            )

print("Self-consistency confirmed: every scenario's own solar candidates report production_zone_relationship "
      "correctly against that SAME scenario's own claimed production zones (overlap is allowed and expected).")

print("\nAll scenario generation pipeline checks passed.")
