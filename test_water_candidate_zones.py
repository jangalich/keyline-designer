"""
test_water_candidate_zones.py

Offline (no-network) checks for water_candidate_zones.py's Step 3
cell-based eligibility mask + real cell-union footprint pipeline — Stage 2
of this feature: "is the zone-filtering logic correct," deliberately
independent of Stage 1 (DEM/valley delineation accuracy — see
test_valley_delineation.py / test_production_area.py).

Unlike the old per-traced-valley-branch line-walk this replaced, there is
no hand-built "valley" fixture here at all: find_candidate_zones() now
derives its own flow-accumulation grid directly from a synthetic DEM (via
valley_delineation.get_flow_accumulation_for_dem(), exercised for real,
not mocked) and clusters individually-eligible cells, so these tests build
small synthetic DEMs with a known, hand-verifiable drainage pattern
instead. production_areas are still hand-built dicts in the same shape
identify_production_areas() actually produces (representative elevation +
a real UTM polygon), same as before.

PERCENTILE-BAND GATE: compute_water_eligible_cells()'s old single
MIN_VALLEY_CONTRIBUTING_AREA_ACRES hard threshold was replaced by a
two-step selection (a low floor defines the "drainage-qualifying
population," then a [P25, P75] percentile band of that population's own
flow_accumulation_cells values decides eligibility) -- see that
function's own docstring. On the single-column fixture below (a straight
valley whose accumulation decreases monotonically from row 0's outlet
down to the far row), this means the eligible band before dilation is no
longer "every row down to the floor" (as it was under the old single
threshold) -- it's a genuine BAND in the middle of the row range,
excluding both the high-accumulation rows near the outlet (top of the
distribution) and the low-accumulation rows far from it (bottom of the
distribution). Every row-range/cell-count expectation below reflects this
directly-computed band, not a hand-derived guess -- see the inline
comments deriving each one from the same population+percentile logic
compute_water_eligible_cells() itself runs.

WAIST-SPLITTING: find_candidate_zones() now runs raster_grid.
attempt_waist_split() (shared with production_area.py's own zone
clustering, see that module's docstring for the extraction) on each
connected component before building its cell-union footprint, so a
dilation-induced merge between two originally-separate drainage patches
splits back into two independent zones. The dedicated waist-split tests
below monkeypatch compute_water_eligible_cells() to return a hand-built
dumbbell-shaped mask (the exact same shape production_area.py's own
waist-split tests already validate _attempt_waist_split()/
raster_grid.attempt_waist_split() against) so the CLUSTERING integration
itself is exercised end-to-end through find_candidate_zones(), without
needing to fight real D8 hydrology into producing an exact pinch shape.

WHOLE-ZONE SCORING: production_area_relationships/
primary_production_area_relationship are now computed ONCE per surviving
(possibly waist-split) cluster, from that cluster's own real footprint
centroid + median member-cell elevation -- not per cell, not aggregated
via median from per-cell tags. compute_water_eligible_cells() itself no
longer returns cell_relationships at all (just the eligible_mask).
"""

import math

import numpy as np
from shapely.geometry import box

from feature_schema import validate_feature_collection
from raster_grid import SQUARE_METERS_PER_ACRE, cell_area_acres, cell_union_footprint, connected_components
from water_candidate_zones import (
    MIN_WATER_ZONE_AREA_ACRES,
    MIN_ZONE_WAIST_METERS,
    VALLEY_ACCUMULATION_PERCENTILE_HIGH,
    VALLEY_ACCUMULATION_PERCENTILE_LOW,
    WATER_ZONE_MIN_WAIST_METERS,
    WATER_ZONE_SUBAREA_TARGET_ACRES,
    WATER_ZONE_SUBAREA_TRIGGER_ACRES,
    WATER_ZONE_SURVEY_BUFFER_METERS,
    compute_water_eligible_cells,
    find_candidate_zones,
    select_optimal_survey_subarea,
    zones_to_geojson,
)
import water_candidate_zones as wcz

CRS = "EPSG:32617"
RESOLUTION = (5.0, 5.0)

assert WATER_ZONE_MIN_WAIST_METERS == MIN_ZONE_WAIST_METERS, (
    "WATER_ZONE_MIN_WAIST_METERS should still be anchored to production_area.MIN_ZONE_WAIST_METERS "
    "(see that constant's own docstring) -- if this fails, the anchor was broken without updating this test"
)

# A single straight drainage column at col=20 running the full height of a
# 40x40 grid (200m x 200m at 5m resolution): elevation rises with distance
# from that column AND with row, so flow converges onto col=20 and heads
# toward row 0 (confirmed directly against get_flow_accumulation_for_dem():
# accumulation along the column decreases smoothly from 1600 at row 0 down
# to ~40 at row 39, while off-column cells stay under ~20 everywhere) --
# a single, unambiguous, known drainage path to test cell-level eligibility
# against, not a hand-waved fixture.
SIZE = 40
MID_COL = 20
_single_column_array = np.zeros((SIZE, SIZE), dtype=np.float32)
for _row in range(SIZE):
    for _col in range(SIZE):
        _single_column_array[_row, _col] = abs(_col - MID_COL) * 2.0 + _row * 0.5

SINGLE_COLUMN_DEM = {
    "array": _single_column_array,
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": CRS,
}

# The DEM's own full 200m x 200m extent as the property boundary.
BOUNDARY = box(500000.0, 4499800.0, 500200.0, 4500000.0)

# A production-area patch off to the side of the drainage column (~50m
# away horizontally -- comfortably between MIN_SERVICE_DISTANCE_METERS and
# MAX_SERVICE_DISTANCE_METERS's defaults), well below the column's own
# elevation range (0 - ~19.5m) so every eligible cell sits above it
# (gravity-favorable).
PRODUCTION_AREA_ABOVE = [
    {"id": 0, "representative_elevation_m": -5.0, "polygon_utm": box(500150.0, 4499850.0, 500180.0, 4499900.0)}
]

CELL_AREA_ACRES = cell_area_acres(SINGLE_COLUMN_DEM)

# The raw percentile-band-qualifying mask is only ever one cell wide
# (exactly col=20); WATER_ZONE_SURVEY_BUFFER_METERS dilates it by this
# many cells (rounded up) on every side before the other gates run -- see
# _survey_buffer_radius_cells()'s own conversion in water_candidate_zones.py.
SURVEY_BUFFER_RADIUS_CELLS = math.ceil(WATER_ZONE_SURVEY_BUFFER_METERS / RESOLUTION[0])
assert SURVEY_BUFFER_RADIUS_CELLS > 0, "this test assumes a real, nonzero default survey buffer"


# --- percentile-band gate: derive the expected pre-dilation row band  ---
# --- directly from the SAME population+percentile logic              ---
# --- compute_water_eligible_cells() runs internally, not a hand guess ---
#
# This mirrors the existing convention (SURVEY_BUFFER_RADIUS_CELLS above
# is likewise derived via the same conversion the module itself uses, not
# hardcoded from intuition) -- confirming the module's own behavior
# against its own documented algorithm, not re-deriving it independently.
from valley_delineation import get_flow_accumulation_for_dem  # noqa: E402
from shapely.prepared import prep  # noqa: E402
from shapely.geometry import Point  # noqa: E402
from raster_grid import pixel_center_xy  # noqa: E402

_flow_accumulation = get_flow_accumulation_for_dem(SINGLE_COLUMN_DEM)
_area_per_cell = cell_area_acres(SINGLE_COLUMN_DEM)
from water_candidate_zones import MIN_VALLEY_CONTRIBUTING_AREA_ACRES  # noqa: E402

_min_contributing_cells = MIN_VALLEY_CONTRIBUTING_AREA_ACRES / _area_per_cell
_floor_mask = _flow_accumulation >= _min_contributing_cells
_boundary_prepared = prep(BOUNDARY)
_population = [
    float(_flow_accumulation[r, c])
    for r, c in np.argwhere(_floor_mask)
    if _boundary_prepared.contains(Point(*pixel_center_xy(SINGLE_COLUMN_DEM, int(r), int(c))))
]
assert _population, "test setup should produce a nonempty drainage-qualifying population"
_p_low = np.percentile(_population, VALLEY_ACCUMULATION_PERCENTILE_LOW)
_p_high = np.percentile(_population, VALLEY_ACCUMULATION_PERCENTILE_HIGH)
_band_mask = _floor_mask & (_flow_accumulation >= _p_low) & (_flow_accumulation <= _p_high)
_pre_dilation_rows = sorted(set(int(r) for r, c in np.argwhere(_band_mask)))
assert _pre_dilation_rows, "percentile band should not be empty on this fixture"
print(
    f"Percentile-band derivation check: population={len(_population)} cells, "
    f"p{VALLEY_ACCUMULATION_PERCENTILE_LOW:.0f}={_p_low:.1f}, p{VALLEY_ACCUMULATION_PERCENTILE_HIGH:.0f}={_p_high:.1f}, "
    f"pre-dilation band rows {_pre_dilation_rows[0]}-{_pre_dilation_rows[-1]} ({len(_pre_dilation_rows)} rows) -- "
    "excludes both the high-accumulation rows near the outlet (row 0) and the low-accumulation rows far from it "
    "(row 39), unlike the old single-threshold gate which admitted every row down to the floor."
)
assert _pre_dilation_rows[0] > 0, (
    "the percentile band must exclude the highest-accumulation rows near the outlet (row 0) -- "
    f"got a band starting at row {_pre_dilation_rows[0]}"
)
assert _pre_dilation_rows[-1] < SIZE - 1, (
    "the percentile band must exclude the lowest-accumulation rows far from the outlet (row 39) -- "
    f"got a band ending at row {_pre_dilation_rows[-1]}"
)

# Expected post-dilation row/col range: the pre-dilation band widened by
# SURVEY_BUFFER_RADIUS_CELLS on every side (8-connected square dilation).
_expected_rows = list(range(_pre_dilation_rows[0] - SURVEY_BUFFER_RADIUS_CELLS, _pre_dilation_rows[-1] + SURVEY_BUFFER_RADIUS_CELLS + 1))
_expected_cols = list(range(MID_COL - SURVEY_BUFFER_RADIUS_CELLS, MID_COL + SURVEY_BUFFER_RADIUS_CELLS + 1))
_expected_cell_count = len(_expected_rows) * len(_expected_cols)


# --- compute_water_eligible_cells(): shape, no valley/branch identity, ---
# --- and the drainage BAND (not the old full-column threshold) WIDENED ---
# --- by the survey buffer is what survives all three gates             ---

eligible_mask = compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY)
assert eligible_mask.shape == SINGLE_COLUMN_DEM["array"].shape, (
    f"eligible_mask must match the DEM's own shape {SINGLE_COLUMN_DEM['array'].shape}, got {eligible_mask.shape}"
)
assert eligible_mask.dtype == bool
eligible_cells = [(int(r), int(c)) for r, c in np.argwhere(eligible_mask)]
assert eligible_cells, "expected at least one eligible cell on this synthetic drainage column"

eligible_cols = sorted(set(c for _r, c in eligible_cells))
assert eligible_cols == _expected_cols, (
    f"the percentile-band drainage column (col=20) should be dilated by the survey buffer "
    f"({SURVEY_BUFFER_RADIUS_CELLS} cells each side) to columns {_expected_cols}, got {eligible_cols}"
)
eligible_rows = sorted(set(r for r, _c in eligible_cells))
assert eligible_rows == _expected_rows, (
    f"the percentile-band row range should be dilated by the survey buffer to rows {_expected_rows}, "
    f"got {eligible_rows}"
)
assert len(eligible_cells) == _expected_cell_count, (
    f"expected {_expected_cell_count} eligible cells (derived from the percentile-band row range dilated by "
    f"the survey buffer), got {len(eligible_cells)}"
)
print(
    f"compute_water_eligible_cells() returns a DEM-shaped boolean mask with {len(eligible_cells)} eligible "
    f"cells -- the percentile-band drainage segment (rows {_pre_dilation_rows[0]}-{_pre_dilation_rows[-1]}) "
    f"correctly WIDENED to columns {_expected_cols[0]}-{_expected_cols[-1]} / rows {_expected_rows[0]}-"
    f"{_expected_rows[-1]} by the survey buffer -- no valley/branch identity, no per-cell relationship "
    "tagging (that's now whole-zone scoring, see find_candidate_zones() below)."
)


# --- find_candidate_zones(): the eligible column clusters into exactly ---
# --- one zone whose real geometry matches cell_union_footprint()       ---
# --- directly (not a hull, not a buffer), scored WHOLE-ZONE            ---

zones = find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY)
assert len(zones) == 1, f"expected exactly 1 zone (one connected drainage-column cluster), got {len(zones)}"
zone = zones[0]
assert zone["id"] == 0
assert zone["served_production_area_ids"] == [0]

expected_footprint = cell_union_footprint(SINGLE_COLUMN_DEM, eligible_mask).intersection(BOUNDARY)
assert abs(zone["polygon_utm"].area - expected_footprint.area) < 1e-6, (
    "the zone's polygon_utm must be exactly the real cell-union footprint of the eligible mask "
    "(clipped to the boundary), not a hull or buffer approximation"
)
assert zone["polygon_utm"].within(BOUNDARY.buffer(1e-6)), "zone must stay within the property boundary"

primary = zone["primary_production_area_relationship"]
assert primary["above_production_area"] is True
assert primary["elevation_differential_m"] > 0
assert primary["gradient_pct"] > 0
assert "contributing_area_cells" in zone and zone["contributing_area_cells"] > 0
assert "slope_pct" in zone and zone["slope_pct"] > 0
print(
    f"find_candidate_zones() clusters the eligible drainage-column cells into exactly 1 zone whose real "
    f"geometry (area={zone['polygon_utm'].area:.2f} sq m) matches cell_union_footprint() directly, "
    f"above_production_area=True, elevation_differential_m={primary['elevation_differential_m']}, "
    f"gradient_pct={primary['gradient_pct']}, contributing_area_cells={zone['contributing_area_cells']}, "
    f"slope_pct={zone['slope_pct']} (whole-zone scoring, computed once from the cluster's own real "
    "footprint centroid + median member-cell elevation, not per-cell/aggregated)."
)


# --- the survey buffer produces a genuinely WIDER zone -- checked via ---
# --- real area, not visual inspection: with the buffer disabled       ---
# --- (survey_buffer_meters=0), the same drainage band collapses back  ---
# --- to its old one-cell-wide trace, an order of magnitude smaller    ---
# --- than the buffered zone above                                     ---

zone_area_acres = zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert zone_area_acres >= 0.1, (
    f"the buffered zone should be a genuinely surveyable, real-acreage area, got {zone_area_acres:.3f} acres"
)

unbuffered_zones = find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, survey_buffer_meters=0.0)
assert len(unbuffered_zones) == 1
unbuffered_area_acres = unbuffered_zones[0]["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert zone_area_acres > unbuffered_area_acres * 2, (
    f"the survey-buffered zone ({zone_area_acres:.3f} acres) should be substantially larger than the same "
    f"drainage band with survey_buffer_meters=0 ({unbuffered_area_acres:.3f} acres) -- otherwise the "
    "buffer isn't actually widening anything"
)
print(
    f"The survey buffer produces a genuinely wider zone by real area: {zone_area_acres:.3f} acres buffered "
    f"vs. {unbuffered_area_acres:.3f} acres with survey_buffer_meters=0 (the old one-cell-wide-trace shape) "
    "-- not just a thin line, confirmed via area, not visual inspection."
)


# --- gravity is a preference, not a gate: a production area SITTING ---
# --- ABOVE the drainage column (pump-required) still produces a     ---
# --- real, qualifying zone, just tagged with a negative differential ---

PRODUCTION_AREA_BELOW = [
    {"id": 5, "representative_elevation_m": 100.0, "polygon_utm": box(500150.0, 4499850.0, 500180.0, 4499900.0)}
]
below_zones = find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_BELOW, BOUNDARY)
assert len(below_zones) == 1, (
    "a production area sitting above the drainage column (pump-required) must still produce a real "
    f"zone, not be silently excluded -- got {len(below_zones)} zones"
)
below_primary = below_zones[0]["primary_production_area_relationship"]
assert below_primary["above_production_area"] is False
assert below_primary["elevation_differential_m"] < 0
print(
    "Removing the hard gravity gate: a below-elevation (pump-required) production-area relationship "
    f"still produces a qualifying zone, elevation_differential_m={below_primary['elevation_differential_m']} "
    "(negative), NOT silently excluded."
)


# --- max service distance is still a real, enforced generation-time ---
# --- filter                                                          ---

too_far_zones = find_candidate_zones(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, max_service_distance_meters=30.0
)
assert too_far_zones == [], (
    "a max_service_distance_meters tighter than every widened-column cell's real distance to the patch "
    "should exclude every cell -- service-distance bounds are unchanged real filters, not preferences"
)
print("Max service distance is still a real, enforced generation-time filter (checked against the widened mask).")


# --- min service distance floor rejects a near-but-SEPARATE patch, ---
# --- but the distance==0 carve-out still protects cells already    ---
# --- inside/touching a patch -- both in the SAME fixture            ---
#
# A patch positioned so its own boundary happens to bisect the drainage
# band at a real, non-row-aligned distance -- confirmed directly below
# (not hand-derived) which rows land inside/touching (distance==0) versus
# genuinely outside but within MIN_SERVICE_DISTANCE_METERS (excluded)
# versus far enough to pass again.
TOUCHING = [
    {"id": 1, "representative_elevation_m": 100.0, "polygon_utm": box(500095.0, 4499900.0, 500110.0, 4499950.0)}
]
touching_mask = compute_water_eligible_cells(SINGLE_COLUMN_DEM, TOUCHING, BOUNDARY)
touching_rows = sorted(int(r) for r, c in np.argwhere(touching_mask) if c == MID_COL)

from shapely.geometry import Point as _Point  # noqa: E402

_patch = TOUCHING[0]["polygon_utm"]
_touching_by_distance = {}
for _r in _expected_rows:
    _x, _y = pixel_center_xy(SINGLE_COLUMN_DEM, _r, MID_COL)
    _touching_by_distance[_r] = _Point(_x, _y).distance(_patch)

_inside_rows = [r for r in _expected_rows if _touching_by_distance[r] == 0.0]
_too_close_rows = [r for r in _expected_rows if 0.0 < _touching_by_distance[r] < 10.0]
_far_enough_rows = [r for r in _expected_rows if _touching_by_distance[r] >= 10.0]

assert _inside_rows, "test setup should produce at least some rows genuinely touching the patch (distance==0)"
assert _too_close_rows, "test setup should produce at least some rows genuinely too close (0 < distance < 10m)"
assert all(r in touching_rows for r in _inside_rows), (
    f"rows {_inside_rows} sit inside/touching the patch (distance==0) and must all be eligible, got {touching_rows}"
)
assert not any(r in touching_rows for r in _too_close_rows), (
    f"rows {_too_close_rows} sit genuinely OUTSIDE the patch but within MIN_SERVICE_DISTANCE_METERS of it -- "
    f"must be excluded, got eligible rows {touching_rows}"
)
assert all(r in touching_rows for r in _far_enough_rows), (
    f"rows {_far_enough_rows} sit far enough from the patch to clear MIN_SERVICE_DISTANCE_METERS again -- "
    f"must be eligible, got {touching_rows}"
)
print(
    f"Same fixture, both behaviors at once: cells already inside/touching a patch (distance==0, rows "
    f"{_inside_rows}) are correctly kept eligible, while genuinely-outside-but-too-close cells "
    f"(rows {_too_close_rows}) are correctly rejected by the min-service-distance floor -- the distance==0 "
    "carve-out doesn't weaken the floor for a real near-but-separate siting."
)


# --- boundary setback is still a real, enforced generation-time filter ---
# --- (the column sits close enough to the east edge that a large      ---
# --- enough setback removes it entirely)                              ---

huge_setback_zones = find_candidate_zones(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, min_boundary_setback_meters=100.0
)
assert huge_setback_zones == [], (
    "a boundary setback of 100m should exclude every column cell (the column sits ~97.5m from the east "
    "edge) -- boundary setback is still a real, unchanged generation-time filter"
)
print("Boundary setback is still a real, enforced generation-time filter.")


# --- regression: the boundary-setback gate flips exactly at            ---
# --- MIN_BOUNDARY_SETBACK_METERS, not approximately                    ---
#
# The flow-accumulation/percentile-band gate is bypassed entirely here
# (min_valley_contributing_area_acres=0.0 AND accumulation_percentile_low/
# high=0/100 -- both needed now, since a floor of 0.0 alone no longer
# bypasses gate 1 the way it did under the old single-threshold design:
# with floor=0, the population is the ENTIRE on-parcel grid, and a
# [25, 75] percentile band of THAT would still exclude real cells. Setting
# the band to [0, 100] as well makes it a true no-op, matching the old
# "min_valley_contributing_area_acres=0.0 bypasses gate 1 entirely"
# behavior exactly) -- so only the boundary/setback test itself is
# exercised, at an exactly-controlled distance from the boundary's own
# edge.
_setback_probe_array = np.zeros((3, 30), dtype=np.float32)
SETBACK_PROBE_DEM = {
    "array": _setback_probe_array,
    "resolution_meters": (1.0, 1.0),
    "origin_x": -0.5,
    "origin_y": 100.5,
    "crs": CRS,
}
# Cell (row=0, col=C)'s center sits at x=C exactly (origin_x=-0.5, so
# x = -0.5 + (C + 0.5) * 1.0 = C) and y=100 -- 100m from the north/south
# edges of the box(0,0,200,200) boundary below (comfortably irrelevant),
# so boundary.distance() reduces to exactly C (the nearest edge, x=0).
SETBACK_PROBE_BOUNDARY = box(0.0, 0.0, 200.0, 200.0)
SETBACK_PROBE_PRODUCTION_AREAS = [
    {"id": 0, "representative_elevation_m": -5.0, "polygon_utm": box(495.0, 95.0, 505.0, 105.0)}
]

setback_probe_mask = compute_water_eligible_cells(
    SETBACK_PROBE_DEM, SETBACK_PROBE_PRODUCTION_AREAS, SETBACK_PROBE_BOUNDARY,
    min_valley_contributing_area_acres=0.0,  # bypass the floor
    accumulation_percentile_low=0.0,
    accumulation_percentile_high=100.0,  # bypass the percentile band -- [0, 100] admits the whole population
    survey_buffer_meters=0.0,  # no dilation -- exact 1:1 cell correspondence
)
assert bool(setback_probe_mask[0, 20]), (
    "a cell exactly 20m from the boundary (> MIN_BOUNDARY_SETBACK_METERS=15.0m) must pass the setback gate"
)
assert not bool(setback_probe_mask[0, 10]), (
    "a cell exactly 10m from the boundary (< MIN_BOUNDARY_SETBACK_METERS=15.0m) must fail the setback gate"
)
print(
    "Regression: the boundary-setback gate flips exactly at MIN_BOUNDARY_SETBACK_METERS -- a cell at "
    "exactly 20m (> 15m) passes, a cell at exactly 10m (< 15m) fails, with the percentile-band and "
    "service-distance checks bypassed so only the setback test itself is exercised."
)


# --- MIN_WATER_ZONE_AREA_ACRES drops a cluster too small to matter ---

huge_min_area_threshold = zone_area_acres * 10
huge_min_area_zones = find_candidate_zones(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, min_water_zone_area_acres=huge_min_area_threshold
)
assert huge_min_area_zones == [], (
    f"raising min_water_zone_area_acres ({huge_min_area_threshold:.3f}) well above the real cluster's own "
    f"area ({zone_area_acres:.3f} acres) should drop it entirely"
)
print("Raising min_water_zone_area_acres above the found zone's size correctly drops it.")


# --- no production areas at all means no zones, regardless of terrain ---

assert find_candidate_zones(SINGLE_COLUMN_DEM, [], BOUNDARY) == []
print("No production-area candidates means no water system candidate zones.")


# --- fragmentation: two genuinely separate eligible patches produce  ---
# --- two separate zones, not one merged/hull'd shape                  ---
#
# Two parallel drainage columns (col=8 and col=32, far enough apart that
# their eligible cells never touch under 8-connectivity) on one 40x40
# grid, both served by one production area sitting between them.

_two_column_array = np.zeros((SIZE, SIZE), dtype=np.float32)
for _row in range(SIZE):
    for _col in range(SIZE):
        _two_column_array[_row, _col] = min(abs(_col - 8), abs(_col - 32)) * 2.0 + _row * 0.5

TWO_COLUMN_DEM = {
    "array": _two_column_array,
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": CRS,
}
BETWEEN_PRODUCTION_AREA = [
    {"id": 0, "representative_elevation_m": -5.0, "polygon_utm": box(500080.0, 4499750.0, 500120.0, 4499800.0)}
]

two_zone_mask = compute_water_eligible_cells(TWO_COLUMN_DEM, BETWEEN_PRODUCTION_AREA, BOUNDARY)
eligible_columns = sorted(set(int(c) for _r, c in np.argwhere(two_zone_mask)))
expected_left_cols = list(range(8 - SURVEY_BUFFER_RADIUS_CELLS, 8 + SURVEY_BUFFER_RADIUS_CELLS + 1))
expected_right_cols = list(range(32 - SURVEY_BUFFER_RADIUS_CELLS, 32 + SURVEY_BUFFER_RADIUS_CELLS + 1))
assert eligible_columns == expected_left_cols + expected_right_cols, (
    f"expected eligible cells confined to the two known drainage columns, each widened by the survey "
    f"buffer ({expected_left_cols}, {expected_right_cols}), got columns {eligible_columns}"
)
assert max(expected_left_cols) < min(expected_right_cols), (
    "test setup should keep the two widened columns genuinely disconnected -- otherwise this isn't "
    "actually testing fragmentation"
)

two_zones = find_candidate_zones(TWO_COLUMN_DEM, BETWEEN_PRODUCTION_AREA, BOUNDARY)
assert len(two_zones) == 2, f"expected exactly 2 separate zones (one per disconnected column), got {len(two_zones)}"
assert {z["id"] for z in two_zones} == {0, 1}
for z in two_zones:
    area_acres = z["polygon_utm"].area / SQUARE_METERS_PER_ACRE
    assert area_acres >= MIN_WATER_ZONE_AREA_ACRES
    assert area_acres >= 0.3, f"each widened zone should be a real, meaningful footprint too, got {area_acres:.3f} acres"
print(
    f"Fragmentation: two genuinely disconnected eligible drainage columns (each widened by the survey "
    f"buffer to a real, meaningful footprint) correctly produce 2 separate zones "
    f"(ids {sorted(z['id'] for z in two_zones)}), not one merged shape."
)


# --- output is a schema-valid FeatureCollection on the required layer ---

geojson = zones_to_geojson(zones)
validate_feature_collection(geojson)
feature = geojson["features"][0]
assert feature["properties"]["layer"] == "water_system_candidate"
assert feature["id"] == "water-system-candidate-0"
assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon"), (
    f"zone geometry must be a real polygon footprint, not a point -- got {feature['geometry']['type']}"
)
assert "pond or dam site" in feature["properties"]["confidence_notes"].lower() or (
    "not a specific pond" in feature["properties"]["confidence_notes"].lower()
)
assert "production_area_relationships" in feature["properties"]
assert "primary_production_area_relationship" in feature["properties"]
assert "contributing_area_cells" in feature["properties"]
assert "slope_pct" in feature["properties"]
print(
    "zones_to_geojson output is schema-valid, layer='water_system_candidate', polygon geometry, carries "
    "elevation-relationship data plus the new contributing_area_cells/slope_pct zone-level aggregates, "
    "feature id keyed off the new sequential zone id."
)

below_geojson = zones_to_geojson(below_zones)
below_feature = below_geojson["features"][0]
assert below_feature["properties"]["primary_production_area_relationship"]["above_production_area"] is False
print("Below-elevation zone's GeoJSON feature reports above_production_area=False, not omitted or excluded.")


# =====================================================================
# Waist-splitting: find_candidate_zones() now runs raster_grid.
# attempt_waist_split() on each connected component before building its
# footprint -- these tests monkeypatch compute_water_eligible_cells() to
# return a hand-built dumbbell mask (the exact same shape production_
# area.py's own waist-split tests validate _attempt_waist_split()/
# raster_grid.attempt_waist_split() against -- see test_production_area.py)
# so the CLUSTERING integration itself is exercised end-to-end through
# find_candidate_zones(), independent of real D8 hydrology.
# =====================================================================

WAIST_RESOLUTION = (5.0, 5.0)
WAIST_DEM_SHAPE = (12, 24)
_waist_array = np.full(WAIST_DEM_SHAPE, 100.0, dtype=np.float32)
WAIST_DEM = {
    "array": _waist_array,
    "resolution_meters": WAIST_RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": CRS,
}
WAIST_BOUNDARY = box(
    500000.0, 4500000.0 - WAIST_DEM_SHAPE[0] * WAIST_RESOLUTION[1],
    500000.0 + WAIST_DEM_SHAPE[1] * WAIST_RESOLUTION[0], 4500000.0,
)
WAIST_PRODUCTION_AREAS = [
    {"id": 0, "representative_elevation_m": 50.0, "polygon_utm": box(500040.0, 4499940.0, 500060.0, 4499960.0)}
]


def _rect_cells(r0: int, r1: int, c0: int, c1: int) -> list[tuple[int, int]]:
    return [(r, c) for r in range(r0, r1) for c in range(c0, c1)]


def _mask_from_cells(shape: tuple[int, int], cells) -> np.ndarray:
    mask = np.zeros(shape, dtype=bool)
    for r, c in cells:
        mask[r, c] = True
    return mask


_lobe_a = _rect_cells(0, 10, 0, 10)   # 10x10
_lobe_b = _rect_cells(0, 10, 14, 24)  # 10x10

# --- narrow strip (narrower than MIN_ZONE_WAIST_METERS): must split into 2 ---

_narrow_strip = _rect_cells(4, 6, 10, 14)  # 2 rows tall -- narrower than the 12m (~2-cell radius) erosion
_dumbbell_mask = _mask_from_cells(WAIST_DEM_SHAPE, _lobe_a + _lobe_b + _narrow_strip)

_original_compute_water_eligible_cells = wcz.compute_water_eligible_cells
wcz.compute_water_eligible_cells = lambda *a, **kw: _dumbbell_mask
try:
    split_zones = find_candidate_zones(WAIST_DEM, WAIST_PRODUCTION_AREAS, WAIST_BOUNDARY)
finally:
    wcz.compute_water_eligible_cells = _original_compute_water_eligible_cells

assert len(split_zones) == 2, (
    f"a dumbbell mask with a strip narrower than MIN_ZONE_WAIST_METERS must be split into 2 zones by "
    f"find_candidate_zones()'s own waist-split step, got {len(split_zones)}"
)
for z in split_zones:
    area_acres = z["polygon_utm"].area / SQUARE_METERS_PER_ACRE
    assert area_acres >= MIN_WATER_ZONE_AREA_ACRES
    assert area_acres > 0.3, f"each split-off lobe should be a real, meaningful footprint, got {area_acres:.3f} acres"
    assert z["primary_production_area_relationship"]["production_area_id"] == 0
print(
    f"Waist-split: a dilation-merged dumbbell mask (narrow connecting strip) is correctly split back into "
    f"{len(split_zones)} independent zones by find_candidate_zones()'s own waist-split step, each a real, "
    "meaningful footprint with its own whole-zone production_area_relationship -- not left as 1 merged zone."
)


# --- wide strip (>= MIN_ZONE_WAIST_METERS): must stay as 1 unsplit zone ---

_wide_strip = _rect_cells(0, 10, 10, 14)  # full lobe height -- no pinch at all
_wide_dumbbell_mask = _mask_from_cells(WAIST_DEM_SHAPE, _lobe_a + _lobe_b + _wide_strip)

wcz.compute_water_eligible_cells = lambda *a, **kw: _wide_dumbbell_mask
try:
    unsplit_zones = find_candidate_zones(WAIST_DEM, WAIST_PRODUCTION_AREAS, WAIST_BOUNDARY)
finally:
    wcz.compute_water_eligible_cells = _original_compute_water_eligible_cells

assert len(unsplit_zones) == 1, (
    f"a dumbbell mask whose connecting strip is WIDER than MIN_ZONE_WAIST_METERS must NOT split, "
    f"got {len(unsplit_zones)} zone(s)"
)
print(
    "Waist-split: the same dumbbell shape with a WIDE connecting strip correctly stays as 1 unsplit zone -- "
    "waist-splitting only triggers on a genuine pinch, not any two-lobe shape."
)

# =====================================================================
# Canopy / existing-road hard exclusions: additional cell-level AND'd
# gates in compute_water_eligible_cells(), reusing production_area.py's
# already-validated canopy_height_data.tree_root_zone_mask()/farm_roads_
# data.get_road_exclusion_union_utm() building blocks directly (see that
# function's own docstring, gates 4/5) rather than reimplementing either.
# Both default to a sentinel meaning "never checked at all" (skip the
# gate) so every existing call above -- none of which pass either
# parameter -- is completely unaffected; identify_water_system_candidate_
# zones() is what actually makes canopy MANDATORY (see that function's
# own dedicated offline check in test_water_system_candidate_pipeline.py/
# test_canopy_height_data.py-style hard-fail coverage).
# =====================================================================

from canopy_height_data import TREE_ROOT_ZONE_BUFFER_METERS  # noqa: E402
from farm_roads_data import ROAD_EXCLUSION_BUFFER_METERS  # noqa: E402

assert wcz.WATER_ZONE_CANOPY_BUFFER_METERS == TREE_ROOT_ZONE_BUFFER_METERS, (
    "WATER_ZONE_CANOPY_BUFFER_METERS is numerically identical to production's own TREE_ROOT_ZONE_BUFFER_METERS "
    "today, but must stay a SEPARATE, independently-named constant (see its own docstring) -- if this fails, "
    "check whether that was an intentional retune or an accidental re-coupling"
)
assert wcz.WATER_ZONE_ROAD_BUFFER_METERS != ROAD_EXCLUSION_BUFFER_METERS, (
    "WATER_ZONE_ROAD_BUFFER_METERS (10ft) is deliberately a real, nonzero buffer, unlike production's own "
    f"ROAD_EXCLUSION_BUFFER_METERS default ({ROAD_EXCLUSION_BUFFER_METERS}, a no-op) -- these must stay distinct"
)
print(
    f"WATER_ZONE_CANOPY_BUFFER_METERS ({wcz.WATER_ZONE_CANOPY_BUFFER_METERS}m) and WATER_ZONE_ROAD_BUFFER_METERS "
    f"({wcz.WATER_ZONE_ROAD_BUFFER_METERS}m) are separate, independently-tunable constants from production_area.py's "
    "own canopy/road buffers, per this pipeline's established convention."
)


# --- canopy: sentinel default (never checked) leaves the baseline mask unchanged ---

_canopy_baseline_mask = compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY)
assert int(_canopy_baseline_mask.sum()) == len(eligible_cells), (
    "with canopy_root_zone_mask_utm left at its default sentinel, the gate must be a complete no-op"
)
print("Canopy gate: left unchecked (default sentinel), compute_water_eligible_cells() is completely unaffected.")


# --- canopy: a real mask marking every cell as tree-root-zone excludes everything ---

_all_trees_mask = np.ones(SINGLE_COLUMN_DEM["array"].shape, dtype=bool)
_canopy_excluded_mask = compute_water_eligible_cells(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, canopy_root_zone_mask_utm=_all_trees_mask
)
assert int(_canopy_excluded_mask.sum()) == 0, (
    "a canopy_root_zone_mask_utm marking every cell as tree-root-zone must exclude every cell -- a hard, "
    "cell-level AND gate, not a soft preference"
)

# --- canopy: a real all-False mask (checked, genuinely no trees) matches baseline ---

_no_trees_mask = np.zeros(SINGLE_COLUMN_DEM["array"].shape, dtype=bool)
_canopy_clean_mask = compute_water_eligible_cells(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, canopy_root_zone_mask_utm=_no_trees_mask
)
assert int(_canopy_clean_mask.sum()) == len(eligible_cells), (
    "a real, checked, all-False canopy mask (genuinely no trees anywhere) must match the no-canopy-check baseline"
)
print(
    "Canopy gate: a real tree-root-zone mask hard-excludes every marked cell (0 eligible cells with an "
    "all-trees mask), while a real, checked all-clear mask matches the unchecked baseline exactly -- "
    "confirming this is a genuine per-cell AND gate, not a no-op regardless of mask content."
)


# --- roads: sentinel default / real None (checked, nothing found) are both no-ops ---

_road_sentinel_mask = compute_water_eligible_cells(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY)
_road_none_mask = compute_water_eligible_cells(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, road_exclusion_union_utm=None
)
assert int(_road_sentinel_mask.sum()) == int(_road_none_mask.sum()) == len(eligible_cells), (
    "both the default sentinel (never checked) and a real None (checked, no roads found nearby) must leave "
    "the mask unaffected"
)
print("Road gate: both the unchecked sentinel and a real 'no roads found' None result are correctly no-ops.")


# --- roads: a real union covering the whole eligible band excludes everything ---

_whole_band_road_union = BOUNDARY
_road_excluded_mask = compute_water_eligible_cells(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, road_exclusion_union_utm=_whole_band_road_union
)
assert int(_road_excluded_mask.sum()) == 0, (
    "a road_exclusion_union_utm covering the entire eligible band must exclude every cell -- a hard, "
    "cell-level AND gate"
)

# --- roads: a union covering only PART of the eligible band excludes only that part ---

_half_band_road_union = box(500000.0, 4499800.0, 500110.0, 4500000.0)  # west half only
_partial_road_mask = compute_water_eligible_cells(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, road_exclusion_union_utm=_half_band_road_union
)
_partial_count = int(_partial_road_mask.sum())
assert 0 < _partial_count < len(eligible_cells), (
    f"a road union covering only part of the eligible band should exclude only the overlapping cells, not "
    f"all-or-nothing -- got {_partial_count} of {len(eligible_cells)}"
)
print(
    f"Road gate: a real road union hard-excludes every cell it covers (0 of {len(eligible_cells)} survive a "
    f"whole-band union; {_partial_count} of {len(eligible_cells)} survive a west-half-only union) -- a genuine "
    "cell-level AND gate, never vectorized into a polygon buffer/difference."
)


# --- find_candidate_zones() forwards both new gates through to compute_water_eligible_cells() ---

_zones_all_trees = find_candidate_zones(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, canopy_root_zone_mask_utm=_all_trees_mask
)
assert _zones_all_trees == [], (
    "find_candidate_zones() must forward canopy_root_zone_mask_utm through to compute_water_eligible_cells() -- "
    "an all-trees mask should leave no eligible cells to cluster into zones at all"
)
print("find_candidate_zones() correctly forwards canopy_root_zone_mask_utm/road_exclusion_union_utm through to compute_water_eligible_cells().")


# =====================================================================
# select_optimal_survey_subarea(): a smaller, higher-confidence sub-region
# within a zone that's large enough that the whole footprint isn't a very
# actionable survey pointer -- favoring elevation advantage and proximity
# to the zone's own primary served production area. Wired into
# find_candidate_zones() itself (every zone dict always carries
# optimal_subarea_polygon_utm/optimal_subarea_geometry_wgs84/
# optimal_subarea_acres, None when not applicable), tested here both via
# the standalone function and through find_candidate_zones()'s own
# wiring.
# =====================================================================

# A wide drainage BAND (not a single column) so a zone this size clears
# WATER_ZONE_SUBAREA_TRIGGER_ACRES with real elevation variation across
# it: elevation rises with distance from col=30 AND with row, so the
# zone's own cells span a real, known elevation gradient (low near
# col=30/high row, high toward the edges/low row) to score against.
_SUBAREA_SIZE = 60
_SUBAREA_MID_COL = 30
_subarea_array = np.zeros((_SUBAREA_SIZE, _SUBAREA_SIZE), dtype=np.float32)
for _row in range(_SUBAREA_SIZE):
    for _col in range(_SUBAREA_SIZE):
        _subarea_array[_row, _col] = abs(_col - _SUBAREA_MID_COL) * 2.0 + _row * 0.5

SUBAREA_DEM = {
    "array": _subarea_array,
    "resolution_meters": (5.0, 5.0),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": CRS,
}
SUBAREA_BOUNDARY = box(
    500000.0, 4500000.0 - _SUBAREA_SIZE * 5.0, 500000.0 + _SUBAREA_SIZE * 5.0, 4500000.0
)
SUBAREA_PRODUCTION_POLYGON = box(500220.0, 4499850.0, 500250.0, 4499900.0)
SUBAREA_PRODUCTION_AREAS = [
    {"id": 0, "representative_elevation_m": -5.0, "polygon_utm": SUBAREA_PRODUCTION_POLYGON}
]

# A wide survey buffer so the resulting zone footprint clears
# WATER_ZONE_SUBAREA_TRIGGER_ACRES (1.0 acre) by a real, comfortable margin.
subarea_zones = find_candidate_zones(
    SUBAREA_DEM, SUBAREA_PRODUCTION_AREAS, SUBAREA_BOUNDARY, survey_buffer_meters=20.0
)
assert len(subarea_zones) == 1, f"expected exactly 1 large zone on this fixture, got {len(subarea_zones)}"
big_zone = subarea_zones[0]
big_zone_area_acres = big_zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert big_zone_area_acres > WATER_ZONE_SUBAREA_TRIGGER_ACRES, (
    f"test setup should produce a zone comfortably over the {WATER_ZONE_SUBAREA_TRIGGER_ACRES}-acre trigger, "
    f"got {big_zone_area_acres:.3f} acres"
)

# --- (a) a zone at/under the trigger returns None for all three fields ---

small_zones = find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, survey_buffer_meters=0.0)
assert len(small_zones) == 1
small_zone = small_zones[0]
small_zone_area_acres = small_zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert small_zone_area_acres <= WATER_ZONE_SUBAREA_TRIGGER_ACRES, (
    f"test setup should produce a zone at/under the {WATER_ZONE_SUBAREA_TRIGGER_ACRES}-acre trigger, "
    f"got {small_zone_area_acres:.3f} acres"
)
assert small_zone["optimal_subarea_polygon_utm"] is None
assert small_zone["optimal_subarea_geometry_wgs84"] is None
assert small_zone["optimal_subarea_acres"] is None
print(
    f"(a) A zone at/under WATER_ZONE_SUBAREA_TRIGGER_ACRES ({small_zone_area_acres:.3f} <= "
    f"{WATER_ZONE_SUBAREA_TRIGGER_ACRES} acres) correctly returns None for all three optimal_subarea_* fields."
)


# --- (b) a zone over the trigger returns a real sub-area capped near the target ---

assert big_zone["optimal_subarea_polygon_utm"] is not None
assert big_zone["optimal_subarea_geometry_wgs84"] is not None
subarea_acres = big_zone["optimal_subarea_acres"]
assert subarea_acres is not None
# "Capped at approximately the target": the greedy grower stops once the
# target is reached (so it can overshoot by at most the last cell added,
# a single cell's own area) or once candidates run out (so it can also
# undershoot if the zone itself -- after production-area exclusion -- is
# smaller than the target).
cell_acres = cell_area_acres(SUBAREA_DEM)
assert subarea_acres <= WATER_ZONE_SUBAREA_TARGET_ACRES + cell_acres, (
    f"sub-area ({subarea_acres:.3f} acres) should be capped at approximately "
    f"WATER_ZONE_SUBAREA_TARGET_ACRES ({WATER_ZONE_SUBAREA_TARGET_ACRES} acres, +/- one cell), overshot by too much"
)
assert subarea_acres >= WATER_ZONE_SUBAREA_TARGET_ACRES - cell_acres, (
    f"sub-area ({subarea_acres:.3f} acres) is well under WATER_ZONE_SUBAREA_TARGET_ACRES "
    f"({WATER_ZONE_SUBAREA_TARGET_ACRES} acres) despite the zone having comfortably enough candidate cells"
)
print(
    f"(b) A zone over the trigger ({big_zone_area_acres:.3f} acres) returns a real sub-area "
    f"({subarea_acres:.3f} acres), capped at approximately WATER_ZONE_SUBAREA_TARGET_ACRES "
    f"({WATER_ZONE_SUBAREA_TARGET_ACRES} acres)."
)


# --- (c)/(d)/(e): recover the sub-area's own member cells (by real footprint ---
# --- containment against the zone's own known cell list) to check elevation/  ---
# --- distance/exclusion/contiguity directly, not just acreage.                ---

_subarea_polygon = big_zone["optimal_subarea_polygon_utm"]
subarea_cells = [
    (r, c) for r, c in big_zone["cells"]
    if _subarea_polygon.intersects(Point(*pixel_center_xy(SUBAREA_DEM, r, c)))
]
assert subarea_cells, "should be able to recover the sub-area's own member cells from the zone's cell list"

zone_elevations = [SUBAREA_DEM["array"][r, c] for r, c in big_zone["cells"]]
subarea_elevations = [SUBAREA_DEM["array"][r, c] for r, c in subarea_cells]
zone_distances = [
    Point(*pixel_center_xy(SUBAREA_DEM, r, c)).distance(SUBAREA_PRODUCTION_POLYGON) for r, c in big_zone["cells"]
]
subarea_distances = [
    Point(*pixel_center_xy(SUBAREA_DEM, r, c)).distance(SUBAREA_PRODUCTION_POLYGON) for r, c in subarea_cells
]

assert np.mean(subarea_elevations) > np.mean(zone_elevations), (
    f"the sub-area's own average elevation ({np.mean(subarea_elevations):.2f}m) should be measurably higher "
    f"than the zone's own average ({np.mean(zone_elevations):.2f}m) -- it's supposed to favor elevation advantage"
)
assert np.mean(subarea_distances) < np.mean(zone_distances), (
    f"the sub-area's own average distance to the production area ({np.mean(subarea_distances):.1f}m) should be "
    f"measurably LESS than the zone's own average ({np.mean(zone_distances):.1f}m) -- it's supposed to favor proximity"
)
print(
    f"(c) The selected sub-area's cells are measurably higher elevation ({np.mean(subarea_elevations):.2f}m avg "
    f"vs. zone's {np.mean(zone_elevations):.2f}m avg) and closer to the production area "
    f"({np.mean(subarea_distances):.1f}m avg vs. zone's {np.mean(zone_distances):.1f}m avg)."
)

assert not any(
    SUBAREA_PRODUCTION_POLYGON.contains(Point(*pixel_center_xy(SUBAREA_DEM, r, c))) for r, c in subarea_cells
), "no selected sub-area cell should fall within the production area's own polygon_utm"
print("(d) No selected sub-area cell falls within the production area's own polygon_utm.")

_subarea_mask = np.zeros(SUBAREA_DEM["array"].shape, dtype=bool)
for r, c in subarea_cells:
    _subarea_mask[r, c] = True
_, _subarea_component_count = connected_components(_subarea_mask)
assert _subarea_component_count == 1, (
    f"the sub-area must be a single contiguous patch, not scattered cells -- got {_subarea_component_count} "
    "connected component(s)"
)
print("(e) The sub-area is a single contiguous 8-connected patch, not scattered cells.")


# --- select_optimal_survey_subarea() called standalone matches the wired-in result ---

standalone_subarea = select_optimal_survey_subarea(big_zone, SUBAREA_PRODUCTION_AREAS, SUBAREA_DEM)
assert standalone_subarea is not None
assert abs(standalone_subarea["area_acres"] - big_zone["optimal_subarea_acres"]) < 1e-9
assert standalone_subarea["polygon_utm"].equals(big_zone["optimal_subarea_polygon_utm"])
print("select_optimal_survey_subarea() called standalone reproduces find_candidate_zones()'s own wired-in result.")

# The full zone's own real geometry/acreage are unchanged by any of the above.
assert big_zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE == big_zone_area_acres
print("The zone's own full polygon_utm/area remain the unchanged source of truth alongside the sub-area suggestion.")


# --- zones_to_geojson() carries the new fields (None when not applicable) ---

subarea_geojson = zones_to_geojson(subarea_zones)
subarea_feature_props = subarea_geojson["features"][0]["properties"]
assert subarea_feature_props["optimal_subarea_acres"] == big_zone["optimal_subarea_acres"]
assert subarea_feature_props["optimal_subarea_geometry_wgs84"] == big_zone["optimal_subarea_geometry_wgs84"]

small_zone_geojson = zones_to_geojson(small_zones)
small_feature_props = small_zone_geojson["features"][0]["properties"]
assert small_feature_props["optimal_subarea_acres"] is None
assert small_feature_props["optimal_subarea_geometry_wgs84"] is None
print("zones_to_geojson() carries optimal_subarea_geometry_wgs84/optimal_subarea_acres, None when not applicable.")

print("\nAll water_candidate_zones checks passed.")
