"""
test_production_area_ceiling.py

Offline (no-network) checks for production_area_ceiling.py -- STEP 2 of
the consolidated production-zone pipeline (trim_to_ceiling(), a global,
cross-cluster worst-first trim toward PRODUCTION_CEILING_PCT_OF_PARCEL,
operating directly on STEP 1's own eligible_mask/per_cell_score, already
hydric-excluded) -- plus this pipeline's full orchestration entry point,
identify_optimized_production_areas().

Scenarios, each isolating ONE thing:

  1. Combined pool ignores original-region origin ("no protected zone"):
     two SEPARATE slope-only-eligible regions -- one perfectly flat
     (best), one with a steepening gradient (worse) -- trimmed toward a
     tight ceiling. Only the worse region's worst cells are removed; the
     flat region survives completely untouched, confirming trimming is
     driven purely by each cell's own STEP 1 score, not which contiguous
     region it came from.
  2. Fragmentation: a single contiguous eligible region with a
     deliberately worse-quality band running through its middle. Trimming
     toward the ceiling consumes that whole band (and then some),
     splitting the one original region into two-plus disconnected,
     independently-sized survivors via STEP 3's cluster_and_gate() -- the
     "may naturally fragment" behavior the feature spec calls for, not
     forced back into one shape.
  3. Already-under-ceiling edge case: a generous boundary around a modest
     region, so the pre-trim eligible percentage is already below
     PRODUCTION_CEILING_PCT_OF_PARCEL. Zero cells should be removed, and
     production_ceiling_target_met must read False (not silently claim
     80% was hit) even though nothing was trimmed.
  4. Full pipeline (identify_optimized_production_areas): summary fields,
     schema-valid GeoJSON, and that STEP 1's hydric exclusion (now
     happening BEFORE the ceiling trim, unlike the pre-consolidation
     architecture) still applies correctly when check_soil is exercised
     via a mocked fetch layer.
"""

import functools
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import box
from shapely.ops import unary_union

import production_area as pa
import production_area_ceiling as pac
from feature_schema import validate_feature_collection
from production_area import compute_step1_eligible_cells, cluster_and_gate

# This file is entirely about STEP 2 (trim_to_ceiling()'s worst-first trim algorithm) --
# not about the hard, always-on boundary-setback gate compute_step1_eligible_cells() also
# applies (that has its own dedicated tests in test_canopy_height_data.py). Every scenario
# below deliberately uses TIGHT boundaries sized exactly to its own eligible region for
# precise area comparisons; left as-is, PRODUCTION_BOUNDARY_SETBACK_METERS would shave an
# unpredictable ring off those tight fixtures and break assertions that have nothing to do
# with the setback itself. Patched both as this file's own name AND inside
# production_area_ceiling.py's own module namespace (optimize_production_areas()/
# identify_optimized_production_areas() call their own internal reference), so every path
# through STEP 1 in this file consistently sees boundary_setback_meters=0 -- same "isolate
# from a concern this file isn't testing" reasoning as check_soil=False elsewhere in this
# pipeline's tests.
compute_step1_eligible_cells = functools.partial(compute_step1_eligible_cells, boundary_setback_meters=0.0)
pac.compute_step1_eligible_cells = compute_step1_eligible_cells

# identify_optimized_production_areas() (scenario 4 below) now fetches a REQUIRED
# tree-root-zone mask via production_area.get_required_tree_root_zone_mask_utm() -- the
# woody-vegetation gate is mandatory there too, same as production_area.identify_
# production_areas(), and a fetch failure raises rather than degrading. Patched here
# (production_area's own module-level name, which is what that shared helper actually
# looks up) to a fixed offline stub so this file's full-pipeline scenario stays fully
# offline; the gate's own hard-failure behavior has its own dedicated tests in
# test_canopy_height_data.py. optimize_production_areas() itself (scenarios 1/2, called
# directly) never reaches this fetch at all -- it stays "pure logic, no network I/O" per
# its own docstring, taking tree_root_zone_mask_utm as an already-fetched, optional input.
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


# --- 1. combined pool ignores region origin: only the worse region's worst cells get trimmed ---

rows, cols = 30, 90
array = _steep_background(rows, cols)
for r in range(3, 23):  # Region A: perfectly flat 20x20 -- best possible quality throughout
    for c in range(3, 23):
        array[r, c] = 100.0
for r in range(3, 23):  # Region B: 20x20 with a steepening gradient (worse, but still eligible)
    for c in range(50, 70):
        array[r, c] = 100.0 + (r - 3) * 0.5

dem_two_region = _dem(array)
x0, y0 = dem_two_region["origin_x"], dem_two_region["origin_y"]
px, py = RESOLUTION
box_a = box(x0 + 3 * px, y0 - 23 * py, x0 + 23 * px, y0 - 3 * py)
box_b = box(x0 + 50 * px, y0 - 23 * py, x0 + 70 * px, y0 - 3 * py)
tight_boundary_two_region = unary_union([box_a, box_b])  # tight around BOTH regions -- eligible pct > ceiling

from raster_grid import cell_area_acres

step1_two_region = compute_step1_eligible_cells(dem_two_region, tight_boundary_two_region)
eligible_count = int(step1_two_region["eligible_mask"].sum())
parcel_acres = tight_boundary_two_region.area / 4046.8564224
eligible_acres = eligible_count * cell_area_acres(dem_two_region)
eligible_pct = eligible_acres / parcel_acres * 100
assert eligible_pct > pac.PRODUCTION_CEILING_PCT_OF_PARCEL, (
    f"test setup requires eligible acreage ({eligible_pct:.1f}%) to exceed the ceiling so real trimming happens"
)

trim = pac.trim_to_ceiling(step1_two_region, dem_two_region, tight_boundary_two_region, ceiling_pct=80.0)
assert trim["cells_removed"] > 0, "trimming must actually remove cells when eligible acreage exceeds the ceiling"
assert trim["production_ceiling_target_met"] is True
assert trim["achieved_pct_of_parcel"] >= 80.0, "achieved percentage must land AT OR JUST ABOVE the ceiling, never under"
assert trim["achieved_pct_of_parcel"] < eligible_pct, "trimming must have actually reduced the percentage"

survivor_mask = np.zeros(array.shape, dtype=bool)
for r, c in trim["survivor_cells"]:
    survivor_mask[r, c] = True
new_patches = cluster_and_gate(survivor_mask, dem_two_region, tight_boundary_two_region, step1_two_region)
survivor_a = next(p for p in new_patches if any(c < 40 for _, c in p["cells"]))
survivor_b = next(p for p in new_patches if any(c >= 40 for _, c in p["cells"]))

original_a_cells = int((step1_two_region["slope_source_labels"] == step1_two_region["slope_source_labels"][12, 12]).sum())
original_b_cells = int((step1_two_region["slope_source_labels"] == step1_two_region["slope_source_labels"][12, 60]).sum())

assert len(survivor_a["cells"]) == original_a_cells, (
    "the perfectly-flat region (best quality throughout) must survive COMPLETELY untouched -- "
    "every removed cell should come from the worse region instead, regardless of region identity"
)
assert len(survivor_b["cells"]) < original_b_cells, "the worse region must have lost cells to the trim"
print(
    f"STEP 2 correctly ignores region origin: {trim['cells_removed']} cell(s) removed, ALL from the "
    f"lower-quality region -- the perfectly-flat region survives with every one of its {original_a_cells} "
    f"original cells intact. Achieved {trim['achieved_pct_of_parcel']}% of parcel "
    f"(target {pac.PRODUCTION_CEILING_PCT_OF_PARCEL}%)."
)


# --- 2. fragmentation: worst-first removal can split one original region into several ---

rows, cols = 30, 90
array = _steep_background(rows, cols)
for r in range(3, 23):  # one big contiguous eligible region
    for c in range(3, 83):
        array[r, c] = 100.0
for r in range(3, 23):  # a worse-quality band running through the middle of it
    for c in range(40, 50):
        # A gentle gradient (~1.1% max internal grade) -- worse than the
        # perfectly flat flanks (0%), but deliberately gentle enough that
        # the ELEVATION JUMP AT THE SEAM (this band column vs its flat
        # neighbor at the SAME row) never approaches MAX_PRODUCTION_SLOPE_PCT
        # either. A steeper gradient here (previously up to 8%) makes that
        # transverse seam jump exceed 20% at the higher rows, which
        # slope-excludes a thin real neck along the low-row end of the
        # band and gets legitimately caught by production_area.py's own
        # waist detection (MIN_ZONE_WAIST_METERS) -- a real pinch, just
        # not the one this specific scenario is isolating (STEP 2's
        # worst-first trim fragmentation, not STEP 3's waist split). This
        # value keeps the raw eligible region a single, un-waisted
        # component (matching the assertion right below), same as this
        # scenario always intended.
        array[r, c] = 100.0 + (r - 3) * 0.03

dem_band = _dem(array)
tight_boundary_band = box(x0 + 3 * px, y0 - 23 * py, x0 + 83 * px, y0 - 3 * py)

step1_band = compute_step1_eligible_cells(dem_band, tight_boundary_band)
band_patches = cluster_and_gate(step1_band["eligible_mask"], dem_band, tight_boundary_band, step1_band)
assert len(band_patches) == 1, "the band is still under the eligibility threshold -- must start as ONE contiguous region"

optimized_band = pac.optimize_production_areas(dem_band, tight_boundary_band, ceiling_pct=80.0)
assert optimized_band["cells_removed"] > 0
assert optimized_band["production_ceiling_target_met"] is True
assert len(optimized_band["patches"]) >= 2, (
    f"trimming the worse middle band should fragment the one original region into multiple survivors, "
    f"got {len(optimized_band['patches'])}"
)
area_per_cell_band = cell_area_acres(dem_band)
for p in optimized_band["patches"]:
    assert p["area_acres"] >= pac.MIN_PRODUCTION_AREA_ACRES, "every reported fragment must clear the minimum area"
    exact_cell_area = len(p["cells"]) * area_per_cell_band
    assert abs(p["area_acres"] - exact_cell_area) < 0.01, (
        f"patch {p['id']}: area_acres ({p['area_acres']}) must match the real cell-union area "
        f"({round(exact_cell_area, 2)}) -- a mismatch means area_acres is hull-inflated"
    )
print(
    f"Fragmentation: trimming the worse-quality middle band split the single original region into "
    f"{len(optimized_band['patches'])} disconnected survivors "
    f"(areas: {[p['area_acres'] for p in optimized_band['patches']]}), none forced back into one shape, "
    "and every survivor's area_acres exactly matches its real cell-union area."
)


# --- 3. already-under-ceiling edge case: nothing to trim, target_met must read False honestly ---

full_extent_band = _full_extent_boundary(dem_band)  # generous boundary -- eligible acreage is a modest fraction
optimized_under = pac.optimize_production_areas(dem_band, full_extent_band, ceiling_pct=80.0)

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


# --- 4. full pipeline: identify_optimized_production_areas summary fields + schema + STEP 1 hydric wiring ---

minx, miny, maxx, maxy = tight_boundary_band.bounds
corner_xs, corner_ys = [minx, maxx, maxx, minx, minx], [miny, miny, maxy, maxy, miny]
lons, lats = warp_transform(CRS, "EPSG:4326", corner_xs, corner_ys)
boundary_coords = list(zip(lons, lats))

result = pac.identify_optimized_production_areas(boundary_coords, dem=dem_band, check_soil=False, check_roads=False)
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
        "STEP 4 scoring must still run on the newly-trimmed geometry"
    )
    assert p["soil_data_available"] is False, "check_soil=False must skip the disqualifying-soil fetch entirely"
validate_feature_collection(result["zones_geojson"])
print(
    f"Full pipeline: {result['total_cells_removed']} cells removed globally, "
    f"{result['total_selected_acreage']} acres selected ({result['percent_of_parcel']}% of parcel), "
    f"production_ceiling_target_met={result['production_ceiling_target_met']}, schema-valid GeoJSON, "
    "and every survivor still carries suitability scoring fields."
)

# STEP 1's hydric exclusion (now happening BEFORE the ceiling trim, not after it) still applies correctly.


def _fake_soil_rows(wkt_polygon):
    return [{"mukey": "1", "muname": "Wet spot", "compname": "Atkins", "comppct_r": 100, "hydricrating": "Yes"}]


# Split point sits at the MIDPOINT of the first survivor's OWN longitude range (not the whole parcel's) so
# the mocked hydric union covers exactly its western half -- a real PARTIAL overlap that excludes part of
# one survivor's own source region, rather than happening to fall in the gap between survivors.
_first_survivor_coords = result["scored_patches"][0]["geometry_wgs84"]["coordinates"][0]
_survivor_lons = [c[0] for c in _first_survivor_coords]
_survivor_lats = [c[1] for c in _first_survivor_coords]
_wgs84_pad = 0.01
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


with mock_patch.object(pa, "get_soil_data_for_polygon", _fake_soil_rows), \
     mock_patch.object(pa, "get_soil_geometries_for_polygon", _fake_soil_geometries):
    carved_result = pac.identify_optimized_production_areas(boundary_coords, dem=dem_band, check_soil=True, check_roads=False)

assert carved_result["scored_patches"], "expected survivors after a mocked partial hydric union"
total_carved = sum(p["soil_carved_acres"] for p in carved_result["scored_patches"])
assert total_carved > 0, (
    "STEP 1's hydric exclusion must still apply against the geometry the ceiling trim operates on -- "
    "a mocked hydric union covering part of the area should carve something out"
)
assert all(p["soil_data_available"] for p in carved_result["scored_patches"])
print(
    f"STEP 1 hydric exclusion still applies correctly (now BEFORE the ceiling trim): "
    f"{total_carved} acres carved (bookkeeping), final total_selected_acreage "
    f"{carved_result['total_selected_acreage']} (vs {result['total_selected_acreage']} without the mocked union)."
)

# =====================================================================
# Regression: identify_optimized_production_areas()/optimize_production_areas() must
# apply the SAME woody-vegetation gate production_area.identify_production_areas() does.
# The real bug this wiring fixes: optimize_production_areas()'s own STEP 1 call used to
# pass compute_step1_eligible_cells() only 4 positional arguments, leaving
# tree_root_zone_mask_utm on its "skip this gate" sentinel default -- so canopy exclusion
# was NEVER active on this entry point (the one render_layout_map.py/tree_zone_
# candidates.py actually use), even after the gate was hardened everywhere else.
# =====================================================================

canopy_gate_grid_size = 20  # 20x20 @ 5m = 100x100m, comfortably above MIN_PRODUCTION_AREA_ACRES
canopy_gate_dem = {
    "array": np.full((canopy_gate_grid_size, canopy_gate_grid_size), 100.0, dtype=np.float32),  # flat
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500000.0 + canopy_gate_grid_size * RESOLUTION[1],
    "crs": CRS,
}
# Padded generously past the boundary setback -- this section is about the canopy gate
# specifically, not incidentally about the setback (which is disabled file-wide anyway).
canopy_gate_padding = pa.PRODUCTION_BOUNDARY_SETBACK_METERS + 50.0
canopy_gate_extent = canopy_gate_grid_size * RESOLUTION[0]
canopy_gate_boundary_utm = box(
    500000.0 - canopy_gate_padding,
    4500000.0 - canopy_gate_padding,
    500000.0 + canopy_gate_extent + canopy_gate_padding,
    4500000.0 + canopy_gate_extent + canopy_gate_padding,
)
canopy_gate_lons, canopy_gate_lats = warp_transform(CRS, "EPSG:4326", *canopy_gate_boundary_utm.exterior.coords.xy)
canopy_gate_boundary_coords = list(zip(canopy_gate_lons, canopy_gate_lats))


def _fake_half_tree_canopy(boundary_coordinates, dem):
    array = np.full(dem["array"].shape, 1.0, dtype=np.float32)  # below threshold everywhere...
    array[:, : dem["array"].shape[1] // 2] = 20.0  # ...except the west half: real trees
    return {
        "array": array,
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source_item_id": "offline-test-half-tree-stub",
    }


# --- STEP 1 cross-check: fed the IDENTICAL tree-root-zone mask, optimize_production_areas()
#     and compute_step1_eligible_cells() (as identify_production_areas() itself calls it)
#     must produce the exact same eligible-cell geometry -- not two different gate stacks. ---
with mock_patch.object(pa, "get_canopy_height_for_boundary", _fake_half_tree_canopy):
    shared_tree_mask = pa.get_required_tree_root_zone_mask_utm(canopy_gate_boundary_utm, canopy_gate_dem)

assert shared_tree_mask.any() and not shared_tree_mask.all(), (
    "test setup should produce a genuine partial tree mask (some cells treed, some not)"
)

step1_via_direct_call = pa.compute_step1_eligible_cells(
    canopy_gate_dem, canopy_gate_boundary_utm, disqualifying_soil_union_utm=None, tree_root_zone_mask_utm=shared_tree_mask
)
optimized_via_ceiling = pac.optimize_production_areas(
    canopy_gate_dem,
    canopy_gate_boundary_utm,
    disqualifying_soil_union_utm=None,
    ceiling_pct=100.0,  # no trim -- isolates the canopy gate from STEP 2's own removal
    tree_root_zone_mask_utm=shared_tree_mask,
)
assert np.array_equal(step1_via_direct_call["eligible_mask"], optimized_via_ceiling["step1"]["eligible_mask"]), (
    "production_area_ceiling.optimize_production_areas() must produce the SAME STEP 1 eligible-cell mask as "
    "production_area.compute_step1_eligible_cells() when fed the identical tree-root-zone mask"
)
assert int(optimized_via_ceiling["step1"]["tree_root_zone_hit"].sum()) > 0, (
    "the canopy gate must have actually excluded some cells here, not just been present and inert"
)
print(
    "Regression: production_area_ceiling.optimize_production_areas(), fed the same tree-root-zone mask, "
    "produces the IDENTICAL STEP 1 eligible-cell mask production_area.compute_step1_eligible_cells() does -- "
    f"{int(optimized_via_ceiling['step1']['tree_root_zone_hit'].sum())} cells genuinely excluded by the canopy gate."
)

# --- Full entry point: identify_optimized_production_areas() with real trees present
#     selects meaningfully less acreage than with the (file-wide, tree-free) default stub. ---
tree_free_result = pac.identify_optimized_production_areas(
    canopy_gate_boundary_coords, dem=canopy_gate_dem, check_soil=False, check_roads=False, ceiling_pct=100.0
)
with mock_patch.object(pa, "get_canopy_height_for_boundary", _fake_half_tree_canopy):
    half_tree_result = pac.identify_optimized_production_areas(
        canopy_gate_boundary_coords, dem=canopy_gate_dem, check_soil=False, check_roads=False, ceiling_pct=100.0
    )
assert half_tree_result["total_selected_acreage"] < tree_free_result["total_selected_acreage"], (
    f"a boundary with real tree cover over its west half must select LESS acreage than the same boundary "
    f"read as tree-free ({half_tree_result['total_selected_acreage']} vs {tree_free_result['total_selected_acreage']}) "
    "-- if these are equal, the canopy gate isn't actually wired into this entry point"
)
print(
    f"Regression: identify_optimized_production_areas() selects meaningfully less acreage with real tree "
    f"cover present ({half_tree_result['total_selected_acreage']} ac) than the tree-free case "
    f"({tree_free_result['total_selected_acreage']} ac) -- the canopy gate is genuinely active on this entry point."
)


# =====================================================================
# identify_optimized_production_areas(): the woody-vegetation gate is mandatory here too
# (no check_canopy flag) -- a fetch failure must propagate as a hard error, not be caught
# and degraded into a second, more lenient behavior for this entry point.
# =====================================================================

# --- no HAG coverage at all (None) -> RuntimeError ---
with mock_patch.object(pa, "get_canopy_height_for_boundary", lambda boundary_coordinates, dem: None):
    try:
        pac.identify_optimized_production_areas(canopy_gate_boundary_coords, dem=canopy_gate_dem, check_soil=False, check_roads=False)
        raised_none = None
    except Exception as e:
        raised_none = e
assert isinstance(raised_none, RuntimeError), f"no HAG coverage must raise RuntimeError, got {type(raised_none)}"
assert "Canopy height data unavailable" in str(raised_none)
print("identify_optimized_production_areas(): no HAG coverage for the boundary raises RuntimeError, same as identify_production_areas().")


class _FakeCeilingCanopyFailure(Exception):
    """Stand-in for a real network exception (e.g. retries exhausted) from the fetch."""


def _raise_ceiling_network_failure(boundary_coordinates, dem):
    raise _FakeCeilingCanopyFailure("simulated: retries exhausted")


# --- a fetch exception propagates UNCHANGED -- not swallowed, not converted, not softened ---
with mock_patch.object(pa, "get_canopy_height_for_boundary", _raise_ceiling_network_failure):
    try:
        pac.identify_optimized_production_areas(canopy_gate_boundary_coords, dem=canopy_gate_dem, check_soil=False, check_roads=False)
        raised_network = None
    except Exception as e:
        raised_network = e
assert isinstance(raised_network, _FakeCeilingCanopyFailure), (
    f"a canopy fetch exception must propagate unchanged through identify_optimized_production_areas(), "
    f"got {type(raised_network)}"
)
print(
    "identify_optimized_production_areas(): a canopy fetch exception propagates unchanged rather than being "
    "caught or softened into a second, more lenient behavior for this entry point."
)


# =====================================================================
# Regression: identify_optimized_production_areas()/optimize_production_areas() must
# apply the SAME existing-road exclusion gate production_area.identify_production_areas()
# does -- the same "confirm production_area_ceiling.py isn't left calling the shared
# function with this parameter omitted" concern the canopy/boundary-setback wiring above
# already covers, now for road_exclusion_union_utm. Both entry points route through
# production_area._fetch_road_exclusion_union_utm() -> production_area.get_road_exclusion_
# union_utm() (imported from farm_roads_data), so a single mock on pa.get_road_exclusion_
# union_utm covers both -- same reasoning as the canopy mock above.
# =====================================================================

road_gate_south_half_union = box(
    500000.0, 4500000.0, 500000.0 + canopy_gate_extent, 4500000.0 + canopy_gate_extent / 2
)  # south half of canopy_gate_dem's own footprint

step1_road_via_direct_call = pa.compute_step1_eligible_cells(
    canopy_gate_dem, canopy_gate_boundary_utm, disqualifying_soil_union_utm=None, road_exclusion_union_utm=road_gate_south_half_union
)
optimized_road_via_ceiling = pac.optimize_production_areas(
    canopy_gate_dem,
    canopy_gate_boundary_utm,
    disqualifying_soil_union_utm=None,
    ceiling_pct=100.0,
    road_exclusion_union_utm=road_gate_south_half_union,
)
assert np.array_equal(step1_road_via_direct_call["eligible_mask"], optimized_road_via_ceiling["step1"]["eligible_mask"]), (
    "production_area_ceiling.optimize_production_areas() must produce the SAME STEP 1 eligible-cell mask as "
    "production_area.compute_step1_eligible_cells() when fed the identical road exclusion union"
)
assert int(optimized_road_via_ceiling["step1"]["road_hit"].sum()) > 0, (
    "the road exclusion gate must have actually excluded some cells here, not just been present and inert"
)
print(
    "Regression: production_area_ceiling.optimize_production_areas(), fed the same road exclusion union, produces "
    f"the IDENTICAL STEP 1 eligible-cell mask production_area.compute_step1_eligible_cells() does -- "
    f"{int(optimized_road_via_ceiling['step1']['road_hit'].sum())} cells genuinely excluded by the road gate."
)

# --- full entry point: identify_optimized_production_areas() applies real road exclusion too ---
with mock_patch.object(pa, "get_road_exclusion_union_utm", lambda boundary_coordinates, dem, buffer_meters=None: road_gate_south_half_union):
    road_excluded_result = pac.identify_optimized_production_areas(
        canopy_gate_boundary_coords, dem=canopy_gate_dem, check_soil=False, check_roads=True, ceiling_pct=100.0
    )
assert road_excluded_result["total_selected_acreage"] < tree_free_result["total_selected_acreage"], (
    f"a boundary with real road exclusion present must select LESS acreage than the same boundary with roads "
    f"unchecked ({road_excluded_result['total_selected_acreage']} vs {tree_free_result['total_selected_acreage']}) "
    "-- if these are equal, the road exclusion gate isn't actually wired into this entry point"
)
print(
    f"Regression: identify_optimized_production_areas() selects meaningfully less acreage with real road "
    f"exclusion present ({road_excluded_result['total_selected_acreage']} ac) than without it "
    f"({tree_free_result['total_selected_acreage']} ac) -- the road gate is genuinely active on this entry point."
)


def _raise_road_ceiling_failure(boundary_coordinates, dem, buffer_meters=None):
    raise RuntimeError("simulated: road fetch retries exhausted")


# --- road fetch failure degrades gracefully here too (unlike canopy) -- no exception ---
with mock_patch.object(pa, "get_road_exclusion_union_utm", _raise_road_ceiling_failure):
    road_degraded_result = pac.identify_optimized_production_areas(
        canopy_gate_boundary_coords, dem=canopy_gate_dem, check_soil=False, check_roads=True, ceiling_pct=100.0
    )
assert road_degraded_result["total_selected_acreage"] == tree_free_result["total_selected_acreage"], (
    "unlike the canopy gate, a road fetch failure must degrade GRACEFULLY (same result as check_roads=False), "
    "not raise or crash identify_optimized_production_areas()"
)
print(
    "identify_optimized_production_areas(): a road fetch failure degrades gracefully (unlike the mandatory "
    "canopy gate) -- no exception, proceeds without the road exclusion."
)


# --- canopy_height override forwarding ---
#
# identify_optimized_production_areas()'s mandatory canopy gate (production_
# area.get_required_tree_root_zone_mask_utm(), the SAME shared helper) now
# accepts a pre-fetched canopy_height override. When supplied it must be
# forwarded so no network canopy fetch happens and the exact supplied array
# reaches the gate. Shared-core behavior is proven in test_canopy_mask_
# override.py; this proves THIS entry point forwards it. Reuses the offline
# dem_band/boundary_coords fixture the check_soil=False path above already uses.
from _canopy_override_probe import CanopyOverrideProbe, clean_canopy_for  # noqa: E402

_ov_override = clean_canopy_for(dem_band)
with CanopyOverrideProbe() as _ov_probe:
    pac.identify_optimized_production_areas(
        boundary_coords, dem=dem_band, check_soil=False, check_roads=False, canopy_height=_ov_override
    )
_ov_probe.assert_override_used(_ov_override, "identify_optimized_production_areas()")
print(
    "identify_optimized_production_areas(): a supplied canopy_height override is forwarded to its "
    "mandatory canopy gate -- 0 canopy fetches, exact override array used."
)

# =====================================================================
# narrative_data: the report-facing, FINAL, JSON-serialisable block
# identify_optimized_production_areas() now attaches. Everything below
# checks the block's own contract -- that it is purely additive, that its
# numbers are internally consistent, that overlapping gate exclusions are
# represented in a way a narrative cannot misread, and that unavailable
# data reads as None rather than as a measured zero.
#
# Every fixture here is synthetic. Nothing in this section reaches the
# network, and no figure below is taken from (or tuned against) a real
# property.
# =====================================================================

import json  # noqa: E402
import math  # noqa: E402

from production_suitability import score_production_areas  # noqa: E402

# The exact top-level key set identify_optimized_production_areas()
# returned BEFORE narrative_data existed. Hard-coded, not derived, so a
# future change that drops or renames one of them fails here loudly --
# this branch's core guarantee is that it added a key and touched nothing
# else.
_PRE_NARRATIVE_RESULT_KEYS = {
    "zones_geojson",
    "scored_patches",
    "total_selected_acreage",
    "percent_of_parcel",
    "parcel_acres",
    "production_ceiling_target_met",
    "total_cells_removed",
}

# Same idea for one scored patch: the fields STEP 3/STEP 4 attached before
# this branch. narrative_data must not have added, removed, or renamed any
# of them -- it reads them, it does not write them.
_PRE_NARRATIVE_PATCH_KEYS = {
    "id",
    "area_acres",
    "representative_elevation_m",
    "polygon_utm",
    "render_fill_polygon_utm",
    # Added by cluster_and_gate() alongside render_fill_polygon_utm when the
    # production-zone endpoint needed the drawn shape's own acreage and its
    # WGS84 form on the wire. Listed here for the same reason every other key
    # is: this set is STEP 3/STEP 4's field list, and the assertion below is
    # about narrative_data not WRITING to a patch, not about STEP 3 never
    # gaining a field. It stays an equality check, so narrative_data adding or
    # dropping anything still fails it.
    "render_fill_area_acres",
    "render_fill_geometry_wgs84",
    "geometry_wgs84",
    "cells",
    "hole_footprints",
    "source_patch_id",
    "suitability_score",
    "slope_factor",
    "size_factor",
    "aspect_factor",
    "avg_slope_pct",
    "aspect_deg",
    "area_score",
    "compactness_score",
    "aspect_available",
    "soil_carved_acres",
    "soil_carved_pct",
    "soil_data_available",
    "confidence_notes",
    "rank",
}


def _nd_dem(array: np.ndarray, origin_y: float = 4500600.0) -> dict:
    return {
        "array": array,
        "resolution_meters": RESOLUTION,
        "origin_x": 500000.0,
        "origin_y": origin_y,
        "crs": CRS,
    }


def _nd_boundary(dem: dict):
    """Full-grid-extent parcel boundary, in UTM and in WGS84 -- every cell
    center in the grid sits inside it."""
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    boundary_utm = box(
        dem["origin_x"], dem["origin_y"] - rows * py, dem["origin_x"] + cols * px, dem["origin_y"]
    )
    lons, lats = warp_transform(CRS, "EPSG:4326", *boundary_utm.exterior.coords.xy)
    return boundary_utm, list(zip(lons, lats))


def _nd_row_band_union(dem: dict, first_row: int, last_row: int):
    """UTM box covering the CENTERS of every cell in rows first_row..last_row
    (inclusive), full grid width -- the shape a mocked hydric-soil or
    road-exclusion union takes in these fixtures. Inset half a cell on each
    side so the cell-center containment test compute_step1_eligible_cells()
    runs is unambiguous at the band's own edges."""
    _, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    top = dem["origin_y"] - (first_row + 0.25) * py
    bottom = dem["origin_y"] - (last_row + 0.75) * py
    return box(dem["origin_x"] - px, bottom, dem["origin_x"] + (cols + 1) * px, top)


def _nd_run(
    dem,
    boundary_coords,
    canopy_mask=None,
    hydric_union=None,
    road_union=None,
    roads_checked=None,
    ceiling_pct=100.0,
):
    """Runs the real entry point offline, with each of its three optional
    network layers replaced by an exact synthetic input (or left at its own
    'not checked' path). Patches the names production_area_ceiling.py
    itself calls, so the fixture controls precisely which cells each gate
    rejects.

    roads_checked separates the two road states the pipeline genuinely
    distinguishes: left None it follows road_union (a union means the
    check ran), but passing True with road_union=None models "the road
    check RAN and found nothing" -- a measured zero, not the unchecked
    case."""
    roads_checked = (road_union is not None) if roads_checked is None else roads_checked
    patches = []
    if canopy_mask is not None:
        patches.append(
            mock_patch.object(
                pac,
                "get_required_tree_root_zone_mask_utm",
                lambda boundary_polygon_utm, dem_arg, canopy_height=None: canopy_mask,
            )
        )
    if hydric_union is not None:
        patches.append(
            mock_patch.object(pac, "_fetch_disqualifying_soil_union", lambda wkt_polygon, dem_arg: hydric_union)
        )
    if roads_checked:
        patches.append(
            mock_patch.object(
                pac, "_fetch_road_exclusion_union_utm", lambda coords, dem_arg: road_union
            )
        )
    stack = []
    try:
        for p in patches:
            p.start()
            stack.append(p)
        return pac.identify_optimized_production_areas(
            boundary_coords,
            dem=dem,
            check_soil=hydric_union is not None,
            check_roads=roads_checked,
            ceiling_pct=ceiling_pct,
        )
    finally:
        for p in reversed(stack):
            p.stop()


def _assert_one_decimal(value, path: str) -> None:
    """Every acreage and percentage this block emits is rounded to 1
    decimal place -- the precision emitted is the precision narrated.
    (The 'scales' block's own band edges are declarations, not
    measurements, and are whole numbers regardless.)"""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        assert round(float(value), 1) == float(value), f"{path} = {value!r} is not rounded to 1 decimal place"
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _assert_one_decimal(v, f"{path}.{k}")
        return
    if isinstance(value, list):
        for i, v in enumerate(value):
            _assert_one_decimal(v, f"{path}[{i}]")
        return
    raise AssertionError(f"{path} holds a non-JSON type: {type(value)!r}")


# --- N1. purely additive, JSON-serialisable, and rounded --------------
# A single flat block of eligible ground, no gates firing at all -- the
# simplest fixture that produces a real patch, used to check the shape of
# the addition rather than any particular number.

nd_flat = np.full((40, 40), 300.0, dtype=np.float32)
nd_flat_dem = _nd_dem(nd_flat)
nd_flat_boundary_utm, nd_flat_coords = _nd_boundary(nd_flat_dem)
nd_flat_result = _nd_run(nd_flat_dem, nd_flat_coords)

assert _PRE_NARRATIVE_RESULT_KEYS <= set(nd_flat_result), (
    "narrative_data must be PURELY ADDITIVE: every key identify_optimized_production_areas() "
    f"returned before it must still be there. Missing: {_PRE_NARRATIVE_RESULT_KEYS - set(nd_flat_result)}"
)
assert set(nd_flat_result) - _PRE_NARRATIVE_RESULT_KEYS == {"narrative_data"}, (
    "narrative_data must be the ONLY new top-level key -- got "
    f"{set(nd_flat_result) - _PRE_NARRATIVE_RESULT_KEYS}"
)
assert nd_flat_result["scored_patches"], "expected the flat fixture to produce at least one patch"
for _p in nd_flat_result["scored_patches"]:
    assert set(_p) == _PRE_NARRATIVE_PATCH_KEYS, (
        "narrative_data must not add, drop, or rename any field on a scored patch -- diff: "
        f"{set(_p) ^ _PRE_NARRATIVE_PATCH_KEYS}"
    )

nd_flat_json = json.dumps(nd_flat_result["narrative_data"])
assert json.loads(nd_flat_json) == nd_flat_result["narrative_data"], (
    "narrative_data must survive a plain json.dumps()/json.loads() round trip unchanged -- no numpy "
    "scalars, no arrays, no geometry"
)
assert set(nd_flat_result["narrative_data"]) == {"scales", "parcel", "ceiling", "gates", "patches"}
_assert_one_decimal(nd_flat_result["narrative_data"], "narrative_data")
# Every patch entry carries a locative position_in_parcel ("center" or an
# 8-point compass word) -- the map legend labels feature classes, not
# individual features, so this is what lets a narrative tell two
# Production Areas apart on the map.
_ALLOWED_POSITIONS = {"center", "north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"}
for _nd_patch_entry in nd_flat_result["narrative_data"]["patches"]:
    assert _nd_patch_entry["position_in_parcel"] in _ALLOWED_POSITIONS, (
        f"patch narrative entry must carry a valid position_in_parcel, got "
        f"{_nd_patch_entry.get('position_in_parcel')!r}"
    )
print(
    "narrative_data: purely additive (every pre-existing top-level key and every scored-patch field "
    f"unchanged), json.dumps()-clean with no custom encoder ({len(nd_flat_json)} chars), and every "
    "numeric value rounded to 1 decimal place."
)


# --- N2. gate acreages are internally consistent (no overlap) ---------
# 40x40 flat cells, all of them slope-passing. Three gates each reject
# their own disjoint 5-row band (200 cells apiece), so every excluded cell
# is rejected by exactly one gate and the '*_only_excluded' figures must
# close the books exactly: slope_passing - sum(only) == eligible.

nd_gate_dem = _nd_dem(np.full((40, 40), 300.0, dtype=np.float32))
nd_gate_boundary_utm, nd_gate_coords = _nd_boundary(nd_gate_dem)

nd_canopy_mask = np.zeros((40, 40), dtype=bool)
nd_canopy_mask[0:5, :] = True                                   # rows 0-4
nd_hydric_union = _nd_row_band_union(nd_gate_dem, 10, 14)       # rows 10-14
nd_road_union = _nd_row_band_union(nd_gate_dem, 20, 24)         # rows 20-24

nd_gate_result = _nd_run(
    nd_gate_dem,
    nd_gate_coords,
    canopy_mask=nd_canopy_mask,
    hydric_union=nd_hydric_union,
    road_union=nd_road_union,
)
nd_gates = nd_gate_result["narrative_data"]["gates"]
nd_parcel = nd_gate_result["narrative_data"]["parcel"]

_cell_acres = cell_area_acres(nd_gate_dem)
_band_acres = round(200 * _cell_acres, 1)
assert nd_parcel["slope_passing_acres"] == round(1600 * _cell_acres, 1)
assert nd_parcel["eligible_acres"] == round(1000 * _cell_acres, 1)
for _gate in ("canopy", "hydric", "farm_roads"):
    assert nd_gates[f"{_gate}_excluded_acres"] == _band_acres, _gate
    assert nd_gates[f"{_gate}_only_excluded_acres"] == _band_acres, _gate
    assert nd_gates[f"{_gate}_excluded_acres"] >= nd_gates[f"{_gate}_only_excluded_acres"], _gate

_only_sum = round(
    sum(nd_gates[f"{g}_only_excluded_acres"] for g in ("canopy", "hydric", "farm_roads")), 1
)
# Every emitted acreage is independently rounded to 1 decimal place, on
# purpose: the precision emitted is the precision narrated. That caps how
# exactly figures can reconcile against each other -- five independently
# rounded terms here, so up to 0.05 ac of slack apiece. The identity is
# asserted to the precision the block actually publishes, not to a
# precision it deliberately does not.
_ROUNDING_SLACK_ACRES = 0.05 * 5
assert abs((nd_parcel["slope_passing_acres"] - _only_sum) - nd_parcel["eligible_acres"]) <= _ROUNDING_SLACK_ACRES, (
    "with no cell rejected by two gates at once, the '*_only_excluded_acres' figures must account for "
    f"the whole gap between slope-passing and eligible ground: {nd_parcel['slope_passing_acres']} - "
    f"{_only_sum} != {nd_parcel['eligible_acres']}"
)
print(
    f"narrative_data gates (no overlap): {nd_parcel['slope_passing_acres']} ac slope-passing - "
    f"{_only_sum} ac summed '_only' exclusions == {nd_parcel['eligible_acres']} ac eligible; each gate "
    f"excludes {_band_acres} ac, all of it its own."
)


# --- N3. overlapping exclusions are represented correctly -------------
# The failure mode this guards: a cell that is BOTH hydric and under
# canopy. It must be counted by both gates' '*_excluded_acres' (each
# answers "how much of this ground carries canopy / is hydric") and by
# NEITHER gate's '*_only_excluded_acres' (which answers "how much would
# come back if this gate did not apply" -- and that ground would not come
# back, the other gate still rejects it). Canopy takes rows 0-9, hydric
# rows 5-14: rows 5-9 are hit by both.

nd_overlap_dem = _nd_dem(np.full((40, 40), 300.0, dtype=np.float32))
nd_overlap_boundary_utm, nd_overlap_coords = _nd_boundary(nd_overlap_dem)

nd_overlap_canopy = np.zeros((40, 40), dtype=bool)
nd_overlap_canopy[0:10, :] = True                                  # rows 0-9
nd_overlap_hydric = _nd_row_band_union(nd_overlap_dem, 5, 14)      # rows 5-14

nd_overlap_result = _nd_run(
    nd_overlap_dem, nd_overlap_coords, canopy_mask=nd_overlap_canopy, hydric_union=nd_overlap_hydric
)
nd_o_gates = nd_overlap_result["narrative_data"]["gates"]
nd_o_parcel = nd_overlap_result["narrative_data"]["parcel"]

_overlap_acres = round(200 * _cell_acres, 1)      # rows 5-9, rejected twice
_both_bands_acres = round(400 * _cell_acres, 1)   # each gate's own full 10-row band
_own_only_acres = round(200 * _cell_acres, 1)     # rows 0-4 (canopy alone) / 10-14 (hydric alone)

assert nd_o_gates["canopy_excluded_acres"] == _both_bands_acres
assert nd_o_gates["hydric_excluded_acres"] == _both_bands_acres
assert nd_o_gates["canopy_only_excluded_acres"] == _own_only_acres
assert nd_o_gates["hydric_only_excluded_acres"] == _own_only_acres
assert nd_o_parcel["eligible_acres"] == round(1000 * _cell_acres, 1)

# The doubly-rejected ground is counted by both '_excluded' figures...
assert abs(
    (nd_o_gates["canopy_excluded_acres"] + nd_o_gates["hydric_excluded_acres"])
    - (nd_o_parcel["slope_passing_acres"] - nd_o_parcel["eligible_acres"] + _overlap_acres)
) <= _ROUNDING_SLACK_ACRES, (
    "summing '*_excluded_acres' must over-state the real loss by exactly the doubly-rejected acreage -- "
    "which is why the docstring forbids summing them"
)
# ...and by neither '_only' figure, so those still never double-count.
_o_only_sum = round(nd_o_gates["canopy_only_excluded_acres"] + nd_o_gates["hydric_only_excluded_acres"], 1)
_o_gap = round(nd_o_parcel["slope_passing_acres"] - nd_o_parcel["eligible_acres"], 1)
assert _o_only_sum <= _o_gap
assert abs((_o_only_sum + _overlap_acres) - _o_gap) <= _ROUNDING_SLACK_ACRES, (
    "the '*_only' figures plus the doubly-rejected acreage must account for the whole "
    f"slope-passing-to-eligible gap: {_o_only_sum} + {_overlap_acres} != {_o_gap}"
)
print(
    f"narrative_data gates (overlapping): rows rejected by BOTH canopy and hydric ({_overlap_acres} ac) "
    f"are counted in each gate's '_excluded_acres' ({_both_bands_acres} ac each, summing to "
    f"{round(nd_o_gates['canopy_excluded_acres'] + nd_o_gates['hydric_excluded_acres'], 1)} ac against a "
    f"real {_o_gap} ac loss) and in NEITHER gate's '_only_excluded_acres' ({_own_only_acres} ac each); "
    f"the '_only' figures sum to {_o_only_sum} ac and never over-state."
)


# --- N4. unavailable data is None, never 0.0 --------------------------
# hydric_pct: 0.0 would tell a narrative "no hydric soils here". When the
# soil check never ran, the truth is "unknown" -- every soil-derived field
# must be None. Same for the road layer, which this fixture also leaves
# unchecked.

nd_unavailable_result = _nd_run(nd_flat_dem, nd_flat_coords, canopy_mask=np.zeros((40, 40), dtype=bool))
nd_u = nd_unavailable_result["narrative_data"]

assert nd_u["gates"]["soil_data_available"] is False
assert nd_u["gates"]["road_data_available"] is False
assert nd_u["gates"]["canopy_data_available"] is True
for _field in ("hydric_excluded_acres", "hydric_only_excluded_acres"):
    assert nd_u["gates"][_field] is None, f"{_field} must be None when the soil check never ran, not 0.0"
for _field in ("farm_roads_excluded_acres", "farm_roads_only_excluded_acres"):
    assert nd_u["gates"][_field] is None, f"{_field} must be None when the road check never ran, not 0.0"
assert nd_u["patches"], "expected the unavailable-data fixture to still produce patches"
for _p in nd_u["patches"]:
    for _field in ("source_region_hydric_pct", "soil_components", "drainage_class"):
        assert _p[_field] is None, (
            f"patch {_p['id']}'s {_field} must be None when no SSURGO data reached this pipeline -- "
            f"got {_p[_field]!r}, which a narrative would read as a measurement"
        )
# ...and the canopy gate, which DID run and genuinely found nothing, still
# reports a real measured 0.0 rather than collapsing to None.
assert nd_u["gates"]["canopy_excluded_acres"] == 0.0
assert nd_u["gates"]["canopy_only_excluded_acres"] == 0.0
print(
    "narrative_data: with soil and road unchecked, every soil- and road-derived field is None (not 0.0) "
    "-- while the canopy gate, which ran and found nothing, still reports a measured 0.0."
)


# --- N5. factors are directly comparable, higher is better ------------
# Three patches on one parcel, each isolated by canopy-excluded ground so
# the elevation surface itself stays smooth:
#   A -- 20x20 block, 3% grade falling SOUTH   (good slope, good aspect, good size)
#   B -- 20x20 block, 18% grade falling NORTH  (bad slope, bad aspect, same size)
#   C -- 20x5 sliver, same ground as A         (same slope/aspect, worse size)
# Every factor is emitted on a 0-100 scale where higher is better, so the
# narrative can compare them to each other and to `score` with no
# conversion.

nd_fac_rows, nd_fac_cols = 52, 40
nd_fac_array = np.zeros((nd_fac_rows, nd_fac_cols), dtype=np.float32)
_elev = 400.0
for _r in range(nd_fac_rows):
    if 2 <= _r <= 21:
        _step = -0.15   # 3% grade at 5m cells, falling southward (increasing row)
    elif 30 <= _r <= 49:
        _step = 0.90    # 18% grade, rising southward -- i.e. falling NORTH
    else:
        _step = 0.0
    _elev += _step
    nd_fac_array[_r, :] = _elev

nd_fac_dem = _nd_dem(nd_fac_array, origin_y=4500900.0)
nd_fac_boundary_utm, nd_fac_coords = _nd_boundary(nd_fac_dem)

nd_fac_canopy = np.ones((nd_fac_rows, nd_fac_cols), dtype=bool)
nd_fac_canopy[2:22, 2:22] = False    # A
nd_fac_canopy[2:22, 30:35] = False   # C
nd_fac_canopy[30:50, 2:22] = False   # B

nd_fac_result = _nd_run(nd_fac_dem, nd_fac_coords, canopy_mask=nd_fac_canopy)
nd_fac_patches = nd_fac_result["narrative_data"]["patches"]
assert len(nd_fac_patches) == 3, f"expected exactly 3 patches from this fixture, got {len(nd_fac_patches)}"

for _p in nd_fac_patches:
    for _name, _value in _p["factors"].items():
        assert 0.0 <= _value <= 100.0, f"patch {_p['id']} factor {_name} = {_value} is outside 0-100"

_by_area = sorted(nd_fac_patches, key=lambda p: -p["area_acres"])
_nd_A = min((p for p in nd_fac_patches if p["slope_median_pct"] < 10.0), key=lambda p: -p["area_acres"])
_nd_C = min((p for p in nd_fac_patches if p["slope_median_pct"] < 10.0), key=lambda p: p["area_acres"])
_nd_B = max(nd_fac_patches, key=lambda p: p["slope_median_pct"])
assert _nd_A["id"] != _nd_C["id"] != _nd_B["id"] != _nd_A["id"]

assert _nd_A["factors"]["slope_factor"] > _nd_B["factors"]["slope_factor"], "gentler ground must score higher"
assert _nd_A["factors"]["aspect_factor"] > _nd_B["factors"]["aspect_factor"], "south-facing must score higher"
assert _nd_A["factors"]["size_factor"] > _nd_C["factors"]["size_factor"], "the bigger, blockier patch must score higher"
assert _nd_A["score"] > _nd_B["score"] and _nd_A["score"] > _nd_C["score"]
assert _nd_A["rank"] == 1
assert _nd_A["dominant_aspect"] == "south" and _nd_B["dominant_aspect"] == "north"
print(
    "narrative_data factors (all 0-100, higher = better): "
    f"good ground {_nd_A['factors']} score {_nd_A['score']} rank {_nd_A['rank']}; "
    f"steep north-facing {_nd_B['factors']} score {_nd_B['score']} rank {_nd_B['rank']}; "
    f"same ground as a sliver {_nd_C['factors']} score {_nd_C['score']} rank {_nd_C['rank']}."
)


# --- N6. aspect_consistency_pct separates a bench from a spur ---------
# The whole reason this field exists: two patches can report the SAME
# dominant aspect and mean completely different things on the ground. Here
# both come back "southeast" --
#   * a uniform plane, every cell falling southeast; and
#   * an annular sector of a cone (a spur nose), whose cells fan across a
#     270-degree arc and merely AVERAGE to southeast.
# Without this field they are indistinguishable, and a narrative would
# describe the spur as if it were the bench.

nd_asp_rows, nd_asp_cols = 60, 60
nd_asp_array = np.full((nd_asp_rows, nd_asp_cols), 500.0, dtype=np.float32)
for _r in range(0, 26):
    for _c in range(0, 26):
        nd_asp_array[_r, _c] = 600.0 - 0.25 * _r - 0.25 * _c  # plane falling south AND east
_spur_r0, _spur_c0 = 42.0, 30.0
for _r in range(28, nd_asp_rows):
    for _c in range(nd_asp_cols):
        _d = math.hypot((_r - _spur_r0) * RESOLUTION[1], (_c - _spur_c0) * RESOLUTION[0])
        nd_asp_array[_r, _c] = 400.0 - 0.05 * _d  # cone: every flank falls away from the apex

nd_asp_dem = _nd_dem(nd_asp_array, origin_y=4501200.0)
nd_asp_boundary_utm, nd_asp_coords = _nd_boundary(nd_asp_dem)

nd_asp_canopy = np.ones((nd_asp_rows, nd_asp_cols), dtype=bool)
nd_asp_canopy[2:24, 2:24] = False  # the uniform bench
for _r in range(28, nd_asp_rows):
    for _c in range(nd_asp_cols):
        _d = math.hypot((_r - _spur_r0) * RESOLUTION[1], (_c - _spur_c0) * RESOLUTION[0])
        _bearing = math.degrees(math.atan2(_c - _spur_c0, -(_r - _spur_r0))) % 360.0
        if 25.0 <= _d <= 80.0 and _bearing <= 270.0:
            nd_asp_canopy[_r, _c] = False  # the spur: a 270-degree arc of the cone's flanks

nd_asp_result = _nd_run(nd_asp_dem, nd_asp_coords, canopy_mask=nd_asp_canopy)
nd_asp_patches = nd_asp_result["narrative_data"]["patches"]
assert len(nd_asp_patches) == 2, f"expected exactly 2 patches from this fixture, got {len(nd_asp_patches)}"

_bench = max(nd_asp_patches, key=lambda p: p["aspect_consistency_pct"])
_spur = min(nd_asp_patches, key=lambda p: p["aspect_consistency_pct"])
assert _bench["dominant_aspect"] == "southeast" and _spur["dominant_aspect"] == "southeast", (
    "both fixtures are built to report the same dominant aspect -- that is the point of the check: "
    f"{_bench['dominant_aspect']} / {_spur['dominant_aspect']}"
)
assert _bench["aspect_consistency_pct"] == 100
assert _spur["aspect_consistency_pct"] < 50, (
    "a patch wrapping a spur must NOT read as consistently oriented -- got "
    f"{_spur['aspect_consistency_pct']}%"
)
assert _bench["aspect_consistency_pct"] - _spur["aspect_consistency_pct"] >= 50
print(
    f"narrative_data aspect_consistency_pct: the uniform bench and the spur BOTH report dominant_aspect "
    f"'southeast', but read {_bench['aspect_consistency_pct']}% and {_spur['aspect_consistency_pct']}% "
    "consistent respectively -- the one figure that tells them apart."
)


# --- N7. the ceiling reports whether it actually bound ----------------
# "Production is limited by terrain" and "production is limited by design"
# are materially different sentences. The ceiling block has to say which.

nd_ceiling_bound = _nd_run(nd_flat_dem, nd_flat_coords, ceiling_pct=40.0)["narrative_data"]["ceiling"]
nd_ceiling_slack = _nd_run(nd_flat_dem, nd_flat_coords, ceiling_pct=100.0)["narrative_data"]["ceiling"]
assert nd_ceiling_bound["cap_pct_of_parcel"] == 40.0 and nd_ceiling_slack["cap_pct_of_parcel"] == 100.0
assert nd_ceiling_bound["bound"] is True and nd_ceiling_bound["acres_trimmed"] > 0.0
assert nd_ceiling_slack["bound"] is False and nd_ceiling_slack["acres_trimmed"] == 0.0
print(
    f"narrative_data ceiling: a 40% cap on this fixture bound and trimmed "
    f"{nd_ceiling_bound['acres_trimmed']} ac; a 100% cap did not bind and reports 0.0 ac trimmed."
)


# --- N8. no recomputation ---------------------------------------------
# The architecture this block sits in exists to compute each step ONCE.
# Reporting on a gate by re-running it would reintroduce exactly the
# redundant work the consolidation removed. Counted here per pipeline run:
# one slope pass, one aspect pass, one STEP 1, one STEP 2, one STEP 3, one
# STEP 4 -- the same counts this entry point had before narrative_data
# existed.

_nd_call_counts: dict[str, int] = {}


def _nd_counting(module, name):
    original = getattr(module, name)

    def wrapper(*args, **kwargs):
        _nd_call_counts[name] = _nd_call_counts.get(name, 0) + 1
        return original(*args, **kwargs)

    return mock_patch.object(module, name, wrapper)


import terrain_metrics as _tm  # noqa: E402

_nd_counters = [
    _nd_counting(pa, "compute_slope_percent"),
    _nd_counting(pa, "compute_slope_and_aspect"),
    _nd_counting(pac, "compute_step1_eligible_cells"),
    _nd_counting(pac, "trim_to_ceiling"),
    _nd_counting(pac, "cluster_and_gate"),
    _nd_counting(pac, "score_production_areas"),
]
for _counter in _nd_counters:
    _counter.start()
try:
    _nd_run(nd_gate_dem, nd_gate_coords, canopy_mask=nd_canopy_mask, hydric_union=nd_hydric_union, road_union=nd_road_union)
finally:
    for _counter in reversed(_nd_counters):
        _counter.stop()

_NO_RECOMPUTE_EXPECTED = {
    "compute_slope_percent": 1,
    "compute_slope_and_aspect": 1,
    "compute_step1_eligible_cells": 1,
    "trim_to_ceiling": 1,
    "cluster_and_gate": 1,
    "score_production_areas": 1,
}
assert _nd_call_counts == _NO_RECOMPUTE_EXPECTED, (
    "narrative_data must be DERIVED from what STEP 1-4 already computed, never recomputed: expected "
    f"{_NO_RECOMPUTE_EXPECTED}, got {_nd_call_counts}"
)
print(f"narrative_data: one full pipeline run still makes exactly these calls -- {_nd_call_counts}.")


# --- N9. the boundary setback's own cost is reported ------------------
# Every other scenario in this file runs with boundary_setback_meters=0
# (see the file-header note). This one restores the real gate so the
# setback figure has something to report, and confirms it reads 0.0 when
# the setback is off -- proving the figure tracks the setback actually
# applied rather than assuming the module constant.

_nd_patched_step1 = pac.compute_step1_eligible_cells
try:
    pac.compute_step1_eligible_cells = pa.compute_step1_eligible_cells  # real setback, this scenario only
    nd_setback_on = _nd_run(nd_flat_dem, nd_flat_coords)["narrative_data"]["gates"]
finally:
    pac.compute_step1_eligible_cells = _nd_patched_step1
nd_setback_off = nd_flat_result["narrative_data"]["gates"]

assert nd_setback_on["boundary_setback_excluded_acres"] > 0.0
assert nd_setback_off["boundary_setback_excluded_acres"] == 0.0
# The constraint names itself in feet regardless of what it cost -- and
# PRODUCTION_BOUNDARY_SETBACK_METERS is defined AS 10 * METERS_PER_FOOT, so
# this is the constant's native unit rather than a lossy conversion. (Under
# this file's global setback=0 patch the cost reads 0.0 while the constant
# still reports 10.0 ft -- that patch swaps the FUNCTION, not the constant,
# and identify_optimized_production_areas() never overrides the setback on
# the real path.)
assert nd_setback_on["boundary_setback_feet"] == 10.0
assert nd_setback_off["boundary_setback_feet"] == 10.0
assert nd_setback_on["boundary_setback_only_excluded_acres"] is None, (
    "what the setback costs NET of the canopy/hydric/road gates is genuinely unknown -- those gates are "
    "never evaluated inside the setback ring -- so this must be None, not 0.0"
)
print(
    f"narrative_data: the real {pa.PRODUCTION_BOUNDARY_SETBACK_METERS:.1f}m boundary setback costs "
    f"{nd_setback_on['boundary_setback_excluded_acres']} ac of otherwise slope-passing ground on this "
    f"fixture, and reads {nd_setback_off['boundary_setback_excluded_acres']} ac with the setback disabled."
)


# --- N10. the whole block, on one fixture that exercises every field ---
# A parcel with real relief, three surviving patches (two of them the two
# halves of a waist split), a hydric band that sits entirely under canopy
# -- so it demonstrates a gate whose exclusions are ALL shared with
# another gate -- and a road layer that ran and found nothing. Printed in
# full: this is the exact JSON a narrative layer would be handed.

nd_show_rows, nd_show_cols = 70, 60
nd_show_array = np.zeros((nd_show_rows, nd_show_cols), dtype=np.float32)
for _r in range(nd_show_rows):
    for _c in range(nd_show_cols):
        nd_show_array[_r, _c] = 500.0 - 0.2 * _r - 0.1 * _c  # falls south (4%) and east (2%)

nd_show_dem = _nd_dem(nd_show_array, origin_y=4501500.0)
nd_show_boundary_utm, nd_show_coords = _nd_boundary(nd_show_dem)

nd_show_canopy = np.ones((nd_show_rows, nd_show_cols), dtype=bool)
nd_show_canopy[5:25, 5:25] = False    # north lobe    \  joined by a 20m neck, narrower than
nd_show_canopy[25:28, 13:17] = False  # the neck      /  MIN_ZONE_WAIST_METERS -- STEP 3 splits it
nd_show_canopy[28:48, 5:25] = False   # south lobe
nd_show_canopy[5:25, 35:55] = False   # a separate, unsplit block to the east
nd_show_hydric = _nd_row_band_union(nd_show_dem, 60, 69)  # wet ground along the bottom, all of it wooded

nd_show_result = _nd_run(
    nd_show_dem,
    nd_show_coords,
    canopy_mask=nd_show_canopy,
    hydric_union=nd_show_hydric,
    road_union=None,
    roads_checked=True,  # the road check RAN and genuinely found nothing
    ceiling_pct=pac.PRODUCTION_CEILING_PCT_OF_PARCEL,
)
nd_show = nd_show_result["narrative_data"]

assert len(nd_show["patches"]) == 3
assert sum(1 for p in nd_show["patches"] if p["from_waist_split"]) == 2, (
    "the two lobes either side of the neck must both be flagged as coming out of a waist split -- got "
    f"{[(p['id'], p['from_waist_split']) for p in nd_show['patches']]}"
)
assert sum(1 for p in nd_show["patches"] if not p["from_waist_split"]) == 1
assert [p["rank"] for p in nd_show["patches"]] == [1, 2, 3], "patches must be emitted in rank order"
# Every hydric cell here is also under canopy, so the hydric gate rejects
# real ground and yet nothing at all would come back if it stopped
# applying. Both facts have to be readable, and they are.
assert nd_show["gates"]["hydric_excluded_acres"] > 0.0
assert nd_show["gates"]["hydric_only_excluded_acres"] == 0.0
assert all(
    p["source_region_hydric_pct"] is not None and p["source_region_hydric_pct"] > 0.0
    for p in nd_show["patches"]
)
# The road check ran against a genuinely clean parcel: available, measured zero.
assert nd_show["gates"]["road_data_available"] is True
assert nd_show["gates"]["farm_roads_excluded_acres"] == 0.0
_assert_one_decimal(nd_show, "narrative_data")
json.dumps(nd_show)

print("\nnarrative_data, in full, on a synthetic three-patch fixture:")
print(json.dumps(nd_show, indent=2))

# --- N11. area_score / compactness_score resolve the size ambiguity ---
# size_factor blends acreage and shape, so a single number cannot say
# WHICH one is holding a patch back. The N5 fixture already carries the
# exact pair that makes the point: a 20x20 block and a 20x5 sliver on
# identical ground. Splitting size_factor into its two halves is what lets
# a narrative say "small" or "awkwardly shaped" instead of guessing.

for _p in nd_fac_patches:
    for _name in ("area_score", "compactness_score"):
        assert 0.0 <= _p[_name] <= 100.0, f"patch {_p['id']} {_name} = {_p[_name]} is outside 0-100"

# The 20x20 block and the 20x20 steep block are the same size and shape --
# their size halves must match exactly, isolating the comparison below.
assert _nd_A["area_score"] == _nd_B["area_score"]
assert _nd_A["compactness_score"] == _nd_B["compactness_score"]
# Against the sliver: both halves are worse, and compactness is what
# collapses -- the sliver is not merely smaller, it is a bad shape.
assert _nd_A["area_score"] > _nd_C["area_score"]
assert _nd_A["compactness_score"] > _nd_C["compactness_score"]
assert (_nd_A["compactness_score"] - _nd_C["compactness_score"]) >= 20.0, (
    "a same-ground sliver must read as substantially less compact than a solid block -- that is the "
    f"ambiguity these two fields exist to resolve: {_nd_A['compactness_score']} vs "
    f"{_nd_C['compactness_score']}"
)
print(
    f"narrative_data size decomposition: the 20x20 block reads area {_nd_A['area_score']} / compactness "
    f"{_nd_A['compactness_score']} and the same-ground sliver area {_nd_C['area_score']} / compactness "
    f"{_nd_C['compactness_score']} -- both fold into size_factor {_nd_A['factors']['size_factor']} vs "
    f"{_nd_C['factors']['size_factor']}, which alone could not tell 'small' from 'a sliver'."
)


# --- N12. aspect_available pins the flat-ground default ---------------
# STEP 4 defaults aspect_factor to a neutral 1.0 on ground too flat for a
# downhill direction. On a 0-100 scale that arrives as a perfect 100.0 --
# indistinguishable from a genuinely ideal southern aspect unless this
# flag says otherwise.

nd_availability = nd_flat_result["narrative_data"]["patches"]
assert nd_availability, "expected the dead-flat fixture to produce a patch"
for _p in nd_availability:
    assert _p["aspect_available"] is False, (
        "dead-flat ground has no measurable aspect -- aspect_available must say so"
    )
    assert _p["factors"]["aspect_factor"] == 100.0, (
        "STEP 4's neutral flat-ground default must still surface as 100.0 -- the point is that the flag, "
        "not the value, is what tells a reader it was defaulted"
    )
    assert _p["dominant_aspect"] is None and _p["aspect_consistency_pct"] is None
# ...and the check has teeth: the sloped fixtures report it True.
assert all(p["aspect_available"] is True for p in nd_fac_patches)
print(
    "narrative_data aspect_available: dead-flat ground reports False alongside the neutral "
    "aspect_factor 100.0 it was defaulted to (and dominant_aspect None), while every sloped patch "
    "reports True -- the two cases are no longer indistinguishable."
)


# --- N13. narrative_data is self-sufficient ---------------------------
# The wiring session reads this block and nothing else. Strip every other
# key off the result and confirm the block still answers "what makes the
# selected area(s) suitable for production?" on its own -- parcel totals,
# gate contrast, ceiling outcome, and per-patch acreage/score/factors all
# reachable without touching scored_patches or any top-level summary field.

nd_isolated = nd_show_result["narrative_data"]  # the ONLY thing carried forward
_stripped = {"narrative_data": nd_isolated}
assert set(_stripped) == {"narrative_data"}

# Everything the six pre-existing top-level keys would have supplied, and
# what answers it from inside the block alone.
assert _stripped["narrative_data"]["parcel"]["total_acres"] > 0                    # parcel_acres
assert _stripped["narrative_data"]["parcel"]["selected_acres"] > 0                 # total_selected_acreage
assert _stripped["narrative_data"]["parcel"]["selected_pct_of_parcel"] > 0         # percent_of_parcel
assert isinstance(_stripped["narrative_data"]["ceiling"]["bound"], bool)           # ceiling outcome
assert _stripped["narrative_data"]["ceiling"]["acres_trimmed"] >= 0.0              # total_cells_removed, in acres
assert len(_stripped["narrative_data"]["patches"]) == 3                            # scored_patches, count
for _p in _stripped["narrative_data"]["patches"]:
    for _needed in (
        "area_acres", "percent_of_parcel", "score", "factors", "rank",
        "area_score", "compactness_score", "avg_slope_pct", "aspect_available",
        "dominant_aspect", "aspect_consistency_pct", "source_region_hydric_pct",
        "elevation_percentile_of_parcel", "hole_count", "hole_acres",
        "from_waist_split", "source_patch_id",
    ):
        assert _needed in _p, f"patch entry cannot answer for {_needed} without reaching outside the block"
# The comparative half -- "suitable compared to what was rejected".
for _needed in (
    "canopy_excluded_acres", "canopy_only_excluded_acres",
    "hydric_excluded_acres", "hydric_only_excluded_acres",
    "farm_roads_excluded_acres", "farm_roads_only_excluded_acres",
    "boundary_setback_excluded_acres", "boundary_setback_feet",
    "universe", "soil_data_available", "canopy_data_available", "road_data_available",
):
    assert _needed in _stripped["narrative_data"]["gates"], _needed
# And the scale every one of those scores is on.
assert _stripped["narrative_data"]["scales"]["direction"] == "higher_is_better"
print(
    "narrative_data self-sufficiency: with every other result key deleted, the block still supplies "
    "parcel totals, the ceiling outcome, the full gate contrast, and per-patch acreage/score/factors -- "
    "nothing reaches back into scored_patches or the top-level summary fields."
)


# --- N14. the declared bands cover the range, and every score lands ---
# The bands surface close to verbatim in the narrative ("good slope
# suitability"), so a score falling between two bands would leave the
# sentence with no adjective at all. Bounds are lower-inclusive /
# upper-exclusive with the top band closing at 100, which is what makes
# them gapless over the continuous, 1-decimal-place values this block
# actually emits -- closed integer bands would strand 39.5.

_bands = nd_show["scales"]["bands"]
_ordered_bands = sorted(_bands.items(), key=lambda kv: kv[1][0])
assert nd_show["scales"]["range"] == [0.0, 100.0]
assert nd_show["scales"]["band_bounds"] == "lower_inclusive_upper_exclusive_last_band_inclusive"
assert _ordered_bands[0][1][0] == 0.0 and _ordered_bands[-1][1][1] == 100.0, "bands must span the whole range"
for _left, _right in zip(_ordered_bands, _ordered_bands[1:]):
    assert _left[1][1] == _right[1][0], (
        f"bands must be gapless and non-overlapping: {_left[0]} ends at {_left[1][1]}, "
        f"{_right[0]} starts at {_right[1][0]}"
    )


def _band_of(value: float) -> str:
    for _name, (_low, _high) in _ordered_bands:
        if _low <= value < _high or (_high == 100.0 and value == 100.0):
            return _name
    raise AssertionError(f"{value} falls in no declared band")


_banded = []
for _p in nd_fac_patches:
    for _label, _value in [("score", _p["score"])] + sorted(_p["factors"].items()) + [
        ("area_score", _p["area_score"]),
        ("compactness_score", _p["compactness_score"]),
    ]:
        _banded.append((_p["id"], _label, _value, _band_of(_value)))
assert len(_banded) == len(nd_fac_patches) * 6
print("narrative_data score bands -- every emitted score and factor on the good/steep/sliver fixture:")
for _pid, _label, _value, _band in _banded:
    print(f"    patch {_pid}  {_label:<18} {_value:>6} -> {_band}")


# ===========================================================================
# THE EXCLUSION-RESULT OVERRIDE IS FORWARDED THROUGH BOTH HOPS
# ===========================================================================
#
# build_pipeline_context() -> identify_optimized_production_areas() ->
# optimize_production_areas() -> compute_step1_eligible_cells(). The override
# has to survive BOTH hops: a parameter that exists on the entry point but is
# dropped one frame down looks wired and is not, and every fetch-count
# assertion built on it would then be measuring a path nothing takes.
#
# Checked three ways, because "forwarded" fails three ways: the parameter can
# be missing from a hop (signature), it can be accepted and dropped (the value
# STEP 1 actually receives), and it can be forwarded while the entry point
# still makes the fetches the override exists to remove (fetch counts).

import inspect as _e_inspect  # noqa: E402

import exclusion_zones as _e_ez  # noqa: E402

for _e_fn, _e_name in (
    (pac.optimize_production_areas, "optimize_production_areas"),
    (pac.identify_optimized_production_areas, "identify_optimized_production_areas"),
):
    _e_params = _e_inspect.signature(_e_fn).parameters
    assert "exclusion_result" in _e_params, (
        f"{_e_name}() must take exclusion_result -- without it the override cannot reach STEP 1 from "
        "build_pipeline_context(), and the parameter on the entry point above it is dead wiring"
    )
    _e_default = _e_params["exclusion_result"].default
    assert _e_default is pa._EXCLUSION_RESULT_NOT_SUPPLIED, (
        f"{_e_name}()'s exclusion_result must default to production_area's SHARED sentinel -- a fresh "
        f"object() per module would make 'not supplied' unrecognisable one frame down. Got {_e_default!r}"
    )
    assert _e_default is not None, f"{_e_name}()'s exclusion_result default must not be None"
print(
    "both hops take exclusion_result, both defaulting to production_area's own shared "
    "_EXCLUSION_RESULT_NOT_SUPPLIED sentinel (not None, not a per-module object())."
)

# --- the value actually ARRIVES at STEP 1, through both hops -------------

_e_rows = _e_cols = 30
_e_array = np.zeros((_e_rows, _e_cols), dtype=np.float64)
for _r in range(_e_rows):
    _e_array[_r, :] = 100.0 + (0.0 if _r < 20 else (_r - 19) * 1.6)
_e_dem = {
    "array": _e_array,
    "resolution_meters": (5.0, 5.0),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}
_e_boundary = box(500000.0 + 10.0, 4500000.0 - 140.0, 500000.0 + 140.0, 4500000.0 - 10.0)
_e_canopy = np.zeros((_e_rows, _e_cols), dtype=bool)
_e_canopy[4:9, 4:12] = True

# boundary_setback_meters=0.0 to match the file-wide STEP 1 partial at the top
# of this file, NOT to dodge the setback gate: consuming an exclusion result
# computed at a different setback than STEP 1 was asked for raises (asserted
# in test_production_area.py), and that guard fires here for real if this
# argument is dropped. The two thresholds have to be the same threshold.
with mock_patch.object(_e_ez, "get_required_tree_root_zone_mask_utm", return_value=_e_canopy), mock_patch.object(
    _e_ez, "_fetch_disqualifying_soil_union", return_value=None
), mock_patch.object(_e_ez, "_fetch_road_exclusion_union_utm", return_value=None):
    _e_exclusion = _e_ez.identify_exclusion_zones(
        None, dem=_e_dem, boundary_polygon_utm=_e_boundary, boundary_setback_meters=0.0
    )

_e_seen = []
_e_real_step1 = pac.compute_step1_eligible_cells


def _e_spy(*args, **kwargs):
    _e_seen.append(kwargs.get("exclusion_result", "NOT PASSED"))
    return _e_real_step1(*args, **kwargs)


with mock_patch.object(pac, "compute_step1_eligible_cells", side_effect=_e_spy):
    pac.optimize_production_areas(_e_dem, _e_boundary, exclusion_result=_e_exclusion)
assert _e_seen == [_e_exclusion], (
    f"optimize_production_areas() must forward the EXACT exclusion result object to STEP 1, got {_e_seen!r}"
)

# --- and the entry point stops fetching entirely on that path ------------

_e_fetches = {"canopy": 0, "soil": 0, "road": 0}


def _e_count(name, value):
    def _fn(*_a, **_k):
        _e_fetches[name] += 1
        return value

    return _fn


def _e_run(**kwargs):
    _e_fetches.update({"canopy": 0, "soil": 0, "road": 0})
    _e_seen.clear()
    with mock_patch.object(pac, "get_required_tree_root_zone_mask_utm", side_effect=_e_count("canopy", _e_canopy)), \
         mock_patch.object(pac, "_fetch_disqualifying_soil_union", side_effect=_e_count("soil", None)), \
         mock_patch.object(pac, "_fetch_road_exclusion_union_utm", side_effect=_e_count("road", None)), \
         mock_patch.object(pac, "get_dem_for_boundary", return_value=_e_dem), \
         mock_patch.object(pac, "warp_transform",
                           side_effect=lambda *a, **k: ([p[0] for p in _e_boundary.exterior.coords],
                                                        [p[1] for p in _e_boundary.exterior.coords])), \
         mock_patch.object(pac, "compute_step1_eligible_cells", side_effect=_e_spy):
        result = pac.identify_optimized_production_areas(
            [(-80.0, 40.0), (-79.99, 40.0), (-79.99, 40.01)], dem=_e_dem, **kwargs
        )
    return result, dict(_e_fetches), list(_e_seen)


_e_res_self, _e_n_self, _e_seen_self = _e_run()
_e_res_ovr, _e_n_ovr, _e_seen_ovr = _e_run(exclusion_result=_e_exclusion)

assert _e_n_self == {"canopy": 1, "soil": 1, "road": 1}, (
    f"the no-override path must still make all three fetches, got {_e_n_self}"
)
assert _e_seen_self == [pa._EXCLUSION_RESULT_NOT_SUPPLIED], (
    "with nothing supplied, STEP 1 must receive the sentinel -- not None, and not a stray value"
)
assert _e_n_ovr == {"canopy": 0, "soil": 0, "road": 0}, (
    f"identify_optimized_production_areas() must make NO gate fetch when an exclusion result is supplied "
    f"-- that skip IS the de-duplication. Got {_e_n_ovr}"
)
assert _e_seen_ovr == [_e_exclusion], (
    "the exact exclusion result object must reach STEP 1 through BOTH hops"
)

# an explicit None self-computes, exactly as omitting it does
_e_res_none, _e_n_none, _e_seen_none = _e_run(exclusion_result=None)
assert _e_n_none == {"canopy": 1, "soil": 1, "road": 1}, (
    f"an explicit exclusion_result=None must SELF-COMPUTE, fetches and all, got {_e_n_none}"
)

# --- and the answer is the same one, through both paths ------------------

assert len(_e_res_self["scored_patches"]) > 0, "fixture sanity: the comparison must not be vacuous"
for _e_key in ("total_selected_acreage", "percent_of_parcel", "parcel_acres",
               "production_ceiling_target_met", "total_cells_removed"):
    assert _e_res_self[_e_key] == _e_res_ovr[_e_key] == _e_res_none[_e_key], (
        f"'{_e_key}' differs across the override -- the exclusion result must be a pure de-duplication"
    )
assert _e_res_self["narrative_data"] == _e_res_ovr["narrative_data"] == _e_res_none["narrative_data"], (
    "narrative_data must be identical across the override -- it is report-facing and derived entirely "
    "from STEP 1 through STEP 4"
)
assert len(_e_res_self["scored_patches"]) == len(_e_res_ovr["scored_patches"])
for _e_a, _e_b in zip(_e_res_self["scored_patches"], _e_res_ovr["scored_patches"]):
    for _e_field in ("id", "rank", "area_acres", "suitability_score", "slope_factor", "size_factor",
                     "aspect_factor", "representative_elevation_m", "render_fill_area_acres",
                     "source_patch_id", "cells", "geometry_wgs84"):
        assert _e_a[_e_field] == _e_b[_e_field], f"scored patch field '{_e_field}' differs across the override"
    assert _e_a["polygon_utm"].wkb == _e_b["polygon_utm"].wkb
    assert _e_a["render_fill_polygon_utm"].wkb == _e_b["render_fill_polygon_utm"].wkb
print(
    f"identify_optimized_production_areas(): the override reaches STEP 1 through both hops and takes the "
    f"entry point's gate fetches from canopy/soil/road 1/1/1 to 0/0/0, while all "
    f"{len(_e_res_self['scored_patches'])} scored patch(es) -- id, rank, area, geometry, score, every "
    f"factor -- and every narrative_data field stay identical. An explicit None self-computes (1/1/1)."
)


print("\nAll production_area_ceiling checks passed.")
