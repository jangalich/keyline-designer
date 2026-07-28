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
with mock_patch.object(pa, "get_road_exclusion_union_utm", lambda boundary_coordinates, dem: road_gate_south_half_union):
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


def _raise_road_ceiling_failure(boundary_coordinates, dem):
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


print("\nAll production_area_ceiling checks passed.")
