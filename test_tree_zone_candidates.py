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
from shapely.geometry import LineString, Point, box, mapping
from shapely.ops import unary_union

import production_area as pa
import road_corridors as rc
import tree_zone_candidates as tzc
import production_suitability as ps
import water_suitability as ws
from feature_schema import validate_feature_collection
from tree_zone_candidates import (
    HYDRIC_OVERLAP_FACTOR_WEIGHT,
    SLOPE_FACTOR_WEIGHT,
    SOIL_MARGINALITY_FACTOR_WEIGHT,
    STREAM_PROXIMITY_FACTOR_WEIGHT,
    TREE_SLOPE_REFERENCE_PCT,
    STREAM_PROXIMITY_REFERENCE_METERS,
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
road_lines = [LineString([(0, 50), (100, 50)])]  # zero-width

search_space, claimed = compute_tree_search_space(boundary, production_polys, water_polys, road_lines)
assert claimed is not None
assert abs(claimed.area - 1800.0) < 1e-6, "claimed union area should be exactly production+water (roads contribute 0 -- zero width)"
assert search_space is not None and not search_space.is_empty
assert abs(search_space.area - (10000.0 - 1800.0)) < 1e-6, (
    "search space area must equal boundary minus claimed EXACTLY -- the road LineString subtraction has zero "
    "practical effect on area, as documented (a future pass would introduce a real cleared buffer, not this one)"
)
print(f"compute_tree_search_space(): claimed={claimed.area} sqm (roads contributed 0, as expected for zero-width "
      f"line subtraction), search_space={search_space.area} sqm = boundary - claimed exactly.")

# No claims at all -- search_space is the boundary itself, claimed is None (not just empty).
empty_search_space, empty_claimed = compute_tree_search_space(boundary, [], [], [])
assert empty_claimed is None
assert empty_search_space is boundary, "with nothing claimed, the search space should be the boundary object itself"
print("compute_tree_search_space() with nothing claimed returns the boundary unmodified, claimed_union=None.")

# Fully claimed -- search_space is None (not merely empty), a real, reportable "nothing left" outcome.
fully_claimed_search_space, fully_claimed_union = compute_tree_search_space(boundary, [boundary], [], [])
assert fully_claimed_union is not None
assert fully_claimed_search_space is None, "a boundary fully claimed by production/water/roads must report search_space=None"
print("compute_tree_search_space() with the ENTIRE boundary claimed returns search_space=None (a real, reportable outcome).")


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
region6_tiny_hydric = box(ORIGIN_X + 140 * 5.0, ORIGIN_Y - 2 * 5.0, ORIGIN_X + 142 * 5.0, ORIGIN_Y)  # 2x2 cells only

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

# region 6 (tiny hydric patch) -- explicitly re-score in isolation: qualifies on SCORE, dropped by the SIZE filter
region6_only_patches = score_tree_search_space(
    dem, region6_tiny_hydric, boundary_polygon_utm, hydric_union=hydric_union, min_area_acres=0.0,
)
assert len(region6_only_patches) == 1, "with the size filter disabled, region 6 should score high enough to qualify"
assert region6_only_patches[0]["tree_suitability_score"] >= tzc.MIN_TREE_SUITABILITY_SCORE
assert region6_only_patches[0]["area_acres"] < tzc.MIN_TREE_ZONE_ACRES
region6_filtered = score_tree_search_space(dem, region6_tiny_hydric, boundary_polygon_utm, hydric_union=hydric_union)
assert region6_filtered == [], "with the real default size filter applied, the tiny hydric patch must be dropped"
print(f"Region 6 (tiny hydric patch, {region6_only_patches[0]['area_acres']}ac) scores high enough to qualify "
      f"({region6_only_patches[0]['tree_suitability_score']}/100) but is correctly dropped by MIN_TREE_ZONE_ACRES "
      f"({tzc.MIN_TREE_ZONE_ACRES}ac) -- proves the size/shape filter, not just the score threshold, is doing real work.")


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


def _fake_erosion_empty(wkt_polygon):
    return []


def _fake_farm_roads_empty(boundary_coordinates):
    return []


# Same bench-and-rise synthetic DEM as production_area.py's own smoke test:
# a flat, workable bench (rows < 15) production_area.py will claim as a
# production candidate, bordered by a genuinely steep rise (rows >= 15,
# ~100% grade) that production won't touch -- real leftover ground this
# module's own scoring should be able to pick up as a tree candidate via
# slope alone, with every network fetch (production's soil check,
# water_suitability's own per-zone soil/stream check -- reached now via its
# full identify_water_suitability() entry point, since tree_zone_candidates
# subtracts only its SELECTED zone, not the raw pure zone list --
# road_corridors' floodplain/erosion/farm-roads checks, and this module's
# own farmland/hydric/stream checks) mocked to "reachable, found nothing."
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
     mock_patch.object(rc, "get_erosion_factor_for_polygon", _fake_erosion_empty), \
     mock_patch.object(rc, "get_water_features_for_boundary", _fake_water_features_empty), \
     mock_patch.object(rc, "get_farm_roads_for_boundary", _fake_farm_roads_empty), \
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
assert abs(result["search_space_acres"] + result["claimed_acres"] - result["boundary_acres"]) < 0.5, (
    "search_space_acres + claimed_acres should reconstruct boundary_acres almost exactly -- roads contribute "
    "~0 claimed area (zero-width), and production/water polygons are already clipped to the boundary"
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

print("\nAll tree_zone_candidates checks passed.")
