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

DOWNSTREAM-CLEARANCE GATE NOTE: every synthetic DEM below now extends
ROW_MARGIN rows NORTH of the property boundary itself (the boundary polygon
is unchanged from before this gate existed) -- a small buffer margin of
real, valid DEM cells sitting just outside the parcel, mirroring
dem_data.py's own real fetch-with-buffer behavior. Without this margin, a
synthetic DEM whose boundary exactly matches its own full grid extent has
nowhere for a flow path to ever "exit" into (every cell's downhill walk
either reaches the grid's true edge -- a structural dead end, not a
confirmed boundary exit -- or loops), which would fail the new downstream-
clearance gate for every cell regardless of the other three gates' outcome.
The margin is a pure coordinate shift (same world positions, just re-
indexed) -- see the ROW_MARGIN comment below for the exact transform.
"""

import math

import numpy as np
from shapely.geometry import box

from feature_schema import validate_feature_collection
from raster_grid import SQUARE_METERS_PER_ACRE, cell_area_acres, cell_union_footprint
from water_candidate_zones import (
    MIN_DOWNSTREAM_CLEARANCE_METERS,
    MIN_WATER_ZONE_AREA_ACRES,
    WATER_ZONE_SURVEY_BUFFER_METERS,
    _downstream_clearance_meters,
    compute_water_eligible_cells,
    find_candidate_zones,
    zones_to_geojson,
)

CRS = "EPSG:32617"
RESOLUTION = (5.0, 5.0)

# A single straight drainage column at col=20 running most of the height of
# a 40x40 grid (200m x 200m at 5m resolution): elevation rises with distance
# from that column AND with row, so flow converges onto col=20 and heads
# toward row 0 (confirmed directly against get_flow_accumulation_for_dem():
# accumulation along the column decreases smoothly from 1600 at row 0 down
# to ~40 at row 39, while off-column cells stay under ~20 everywhere) --
# a single, unambiguous, known drainage path to test cell-level eligibility
# against, not a hand-waved fixture.
#
# ROW_MARGIN: the property boundary below is fixed at the ORIGINAL 200m x
# 200m extent (500000-500200, 4499800-4500000), but the DEM grid itself now
# has ROW_MARGIN extra rows north of that boundary's own north edge -- real,
# valid DEM cells (continuing the same downhill slope) that sit just off-
# parcel, giving the downstream-clearance gate somewhere to register a real
# exit. This is a pure coordinate shift: origin_y is moved up by
# ROW_MARGIN * resolution, and every cell's elevation uses (row - ROW_MARGIN)
# in place of row, so a cell at NEW row (OLD_ROW + ROW_MARGIN) sits at
# EXACTLY the same world position/elevation as OLD_ROW did before this
# margin existed -- confirmed directly: every eligible-cell count, zone
# area, and column range below is IDENTICAL to this fixture's own pre-
# downstream-clearance-gate numbers, just at row indices shifted by
# +ROW_MARGIN. Any row number referenced below is the NEW (post-shift) one.
SIZE = 40
MID_COL = 20
ROW_MARGIN = 6
ROWS = SIZE + ROW_MARGIN

_single_column_array = np.zeros((ROWS, SIZE), dtype=np.float32)
for _row in range(ROWS):
    for _col in range(SIZE):
        _single_column_array[_row, _col] = abs(_col - MID_COL) * 2.0 + (_row - ROW_MARGIN) * 0.5

SINGLE_COLUMN_DEM = {
    "array": _single_column_array,
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500000.0 + ROW_MARGIN * RESOLUTION[1],
    "crs": CRS,
}

# The property boundary -- UNCHANGED from before the downstream-clearance
# gate existed; the DEM above is simply taller than this now (see
# ROW_MARGIN above).
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

# The raw flow-accumulation-qualifying mask is only ever one cell wide
# (exactly col=20); WATER_ZONE_SURVEY_BUFFER_METERS dilates it by this
# many cells (rounded up) on every side before the other gates run -- see
# _survey_buffer_radius_cells()'s own conversion in water_candidate_zones.py.
SURVEY_BUFFER_RADIUS_CELLS = math.ceil(WATER_ZONE_SURVEY_BUFFER_METERS / RESOLUTION[0])
assert SURVEY_BUFFER_RADIUS_CELLS > 0, "this test assumes a real, nonzero default survey buffer"


# --- compute_water_eligible_cells(): shape, no valley/branch identity, ---
# --- and the drainage column WIDENED by the survey buffer (not just a  ---
# --- one-cell-wide trace) is what survives all four gates              ---

eligible_mask, cell_relationships = compute_water_eligible_cells(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY
)
assert eligible_mask.shape == SINGLE_COLUMN_DEM["array"].shape, (
    f"eligible_mask must match the DEM's own shape {SINGLE_COLUMN_DEM['array'].shape}, got {eligible_mask.shape}"
)
assert eligible_mask.dtype == bool
eligible_cells = [(int(r), int(c)) for r, c in np.argwhere(eligible_mask)]
assert eligible_cells, "expected at least one eligible cell on this synthetic drainage column"

eligible_cols = sorted(set(c for _r, c in eligible_cells))
expected_cols = list(range(MID_COL - SURVEY_BUFFER_RADIUS_CELLS, MID_COL + SURVEY_BUFFER_RADIUS_CELLS + 1))
assert eligible_cols == expected_cols, (
    f"the raw one-cell-wide drainage column (col=20) should be dilated by the survey buffer "
    f"({SURVEY_BUFFER_RADIUS_CELLS} cells each side) to columns {expected_cols}, got {eligible_cols}"
)
assert len(eligible_cells) == 135, (
    f"this fixture's known eligible-cell count (confirmed directly, identical to its own count before the "
    f"downstream-clearance gate existed) is 135, got {len(eligible_cells)} -- the margin should not have "
    "changed which cells qualify, only their row index"
)
assert set(cell_relationships.keys()) == set(eligible_cells), (
    "cell_relationships should have exactly one entry per eligible cell, no more, no less"
)
for cell, relationship in cell_relationships.items():
    assert relationship["id"] == 0
    assert relationship["above_production_area"] if "above_production_area" in relationship else True
    assert relationship["elevation_differential_m"] > 0, "every column cell sits above this low patch"
print(
    f"compute_water_eligible_cells() returns a DEM-shaped boolean mask with {len(eligible_cells)} eligible "
    f"cells, correctly WIDENED from the raw one-cell-wide drainage column to columns {expected_cols[0]}-"
    f"{expected_cols[-1]} by the survey buffer, each tagged with its own per-cell production-area "
    "relationship -- no valley/branch identity involved. All four gates (including the new downstream-"
    "clearance one) pass for this fixture's known-good drainage cells."
)


# --- find_candidate_zones(): the eligible column clusters into exactly ---
# --- one zone whose real geometry matches cell_union_footprint()       ---
# --- directly (not a hull, not a buffer)                               ---

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
print(
    f"find_candidate_zones() clusters the eligible drainage-column cells into exactly 1 zone whose real "
    f"geometry (area={zone['polygon_utm'].area:.2f} sq m) matches cell_union_footprint() directly, "
    f"above_production_area=True, elevation_differential_m={primary['elevation_differential_m']}, "
    f"gradient_pct={primary['gradient_pct']}."
)


# --- the survey buffer produces a genuinely WIDER zone -- checked via ---
# --- real area, not visual inspection: with the buffer disabled       ---
# --- (survey_buffer_meters=0), the same drainage column collapses     ---
# --- back to its old one-cell-wide trace, an order of magnitude       ---
# --- smaller than the buffered zone above                             ---

zone_area_acres = zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert zone_area_acres >= 0.5, (
    f"the buffered zone should be a genuinely surveyable, real-acreage area, got {zone_area_acres:.3f} acres"
)

unbuffered_zones = find_candidate_zones(SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, survey_buffer_meters=0.0)
assert len(unbuffered_zones) == 1
unbuffered_area_acres = unbuffered_zones[0]["polygon_utm"].area / SQUARE_METERS_PER_ACRE
assert zone_area_acres > unbuffered_area_acres * 3, (
    f"the survey-buffered zone ({zone_area_acres:.3f} acres) should be dramatically larger than the same "
    f"drainage column with survey_buffer_meters=0 ({unbuffered_area_acres:.3f} acres) -- otherwise the "
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
#
# Tighter than even the CLOSEST widened cell's real distance to the patch
# (the survey-buffered column's nearest edge, col=22, sits ~43.7m away --
# closer than the original single column's ~52.6m, since widening brings
# some cells nearer) -- so this must still exclude every cell, widened or not.

too_far_zones = find_candidate_zones(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, max_service_distance_meters=30.0
)
assert too_far_zones == [], (
    "a max_service_distance_meters tighter than even the widened column's closest cell to the patch "
    "should exclude every cell -- service-distance bounds are unchanged real filters, not preferences"
)
print("Max service distance is still a real, enforced generation-time filter (checked against the widened mask).")


# --- min service distance floor rejects a near-but-SEPARATE patch, ---
# --- but the distance==0 carve-out still protects cells already    ---
# --- inside/touching a patch -- both in the SAME fixture            ---
#
# TOUCHING straddles rows ~16-25 of the drainage column (distance == 0,
# inside the patch) with an ~7.5m buffer band on either side (rows 14-15 and
# 26-27: genuinely outside the patch, but closer than
# MIN_SERVICE_DISTANCE_METERS=10.0 -- must be excluded).
TOUCHING = [
    {"id": 1, "representative_elevation_m": 100.0, "polygon_utm": box(500095.0, 4499900.0, 500110.0, 4499950.0)}
]
touching_mask, touching_relationships = compute_water_eligible_cells(SINGLE_COLUMN_DEM, TOUCHING, BOUNDARY)
# Filtered to the original col=20 slice specifically -- the survey buffer
# now widens the mask to columns 16-24 (see above), but this check is
# about the service-distance/carve-out behavior at a single, known column,
# same as touching_relationships[(r, MID_COL)]'s own lookup below.
touching_rows = sorted(int(r) for r, c in np.argwhere(touching_mask) if c == MID_COL)

inside_rows = [r for r in range(16, 26) if r in touching_rows]
assert inside_rows == list(range(16, 26)), (
    f"rows 16-25 sit inside/touching the patch (distance==0) and must all be eligible, got {touching_rows}"
)
for r in inside_rows:
    assert touching_relationships[(r, MID_COL)]["distance_m"] == 0.0

near_but_separate_rows = [14, 15, 26, 27]
assert not any(r in touching_rows for r in near_but_separate_rows), (
    f"rows 14-15 and 26-27 sit genuinely OUTSIDE the patch but within MIN_SERVICE_DISTANCE_METERS of it -- "
    f"must be excluded, got eligible rows {touching_rows}"
)
print(
    "Same fixture, both behaviors at once: cells already inside/touching a patch (distance==0, rows 16-25) "
    "are correctly kept eligible, while genuinely-outside-but-too-close cells (rows 14-15/26-27) are correctly "
    "rejected by the min-service-distance floor -- the distance==0 carve-out doesn't weaken the floor for a "
    "real near-but-separate siting."
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
# --- MIN_BOUNDARY_SETBACK_METERS, not approximately -- found live via  ---
# --- diagnose_water_zone_mask.py: a diagnostic sampling bug there once ---
# --- made a genuinely-passing on-parcel cell's real 68-80m distance    ---
# --- look contradictory against a 0-survivor setback mask. That bug    ---
# --- turned out to be in the diagnostic's own sample selection, not    ---
# --- this gate -- but this test pins the real gate's exact behavior    ---
# --- directly, with the flow-accumulation and service-distance checks  ---
# --- bypassed (min_valley_contributing_area_acres=0.0, survey_buffer_  ---
# --- meters=0.0, a distant production area) at an exactly-controlled   ---
# --- distance from the boundary's own edge.
#
# This probe also needs its own downstream-clearance margin (COL_MARGIN,
# same coordinate-preserving-shift idea as ROW_MARGIN above, just along
# columns/x instead of rows/y): the probe DEM slopes gently toward
# decreasing x (the same west edge the setback distance is measured
# against), with COL_MARGIN extra off-parcel columns west of x=0 for that
# flow to actually exit into. Because the flow happens to head toward the
# SAME edge the setback test measures against, both the boundary-setback
# gate and the downstream-clearance gate agree here (a cell far from the
# edge clears both; a cell close to the edge fails both) -- this no longer
# isolates the setback gate in perfect purity, but it's still a real,
# meaningful regression pinning the combined spatial-gate behavior at an
# exact, controlled distance.
_COL_MARGIN = 20
_setback_probe_cols = 30 + _COL_MARGIN
_setback_probe_array = np.zeros((3, _setback_probe_cols), dtype=np.float32)
for _row in range(3):
    for _new_col in range(_setback_probe_cols):
        _setback_probe_array[_row, _new_col] = _new_col - _COL_MARGIN  # decreases toward the west edge (x=0)

SETBACK_PROBE_DEM = {
    "array": _setback_probe_array,
    "resolution_meters": (1.0, 1.0),
    "origin_x": -0.5 - _COL_MARGIN,
    "origin_y": 100.5,
    "crs": CRS,
}
# Cell (row=0, new_col)'s center sits at x = new_col - COL_MARGIN exactly
# (origin_x = -0.5 - COL_MARGIN, so x = -0.5 - COL_MARGIN + (new_col + 0.5)
# = new_col - COL_MARGIN) and y=100 -- 100m from the north/south edges of
# the box(0,0,200,200) boundary below (comfortably irrelevant), so
# boundary.distance() reduces to exactly x (the nearest edge, x=0) for any
# on-parcel cell. The two probe columns below (old col 20 -> new col 40,
# old col 10 -> new col 30) sit at exactly x=20 and x=10, unchanged from
# before this margin existed.
SETBACK_PROBE_BOUNDARY = box(0.0, 0.0, 200.0, 200.0)
SETBACK_PROBE_PRODUCTION_AREAS = [
    {"id": 0, "representative_elevation_m": -5.0, "polygon_utm": box(495.0, 95.0, 505.0, 105.0)}
]

setback_probe_mask, _ = compute_water_eligible_cells(
    SETBACK_PROBE_DEM, SETBACK_PROBE_PRODUCTION_AREAS, SETBACK_PROBE_BOUNDARY,
    min_valley_contributing_area_acres=0.0,  # bypass the flow-accumulation threshold entirely
    survey_buffer_meters=0.0,  # no dilation -- exact 1:1 cell correspondence
)
assert bool(setback_probe_mask[0, 20 + _COL_MARGIN]), (
    "a cell exactly 20m from the boundary (> MIN_BOUNDARY_SETBACK_METERS=15.0m) must pass the setback gate"
)
assert not bool(setback_probe_mask[0, 10 + _COL_MARGIN]), (
    "a cell exactly 10m from the boundary (< MIN_BOUNDARY_SETBACK_METERS=15.0m) must fail the setback gate"
)
print(
    "Regression: the boundary-setback gate flips exactly at MIN_BOUNDARY_SETBACK_METERS -- a cell at "
    "exactly 20m (> 15m) passes, a cell at exactly 10m (< 15m) fails, with the flow-accumulation and "
    "service-distance checks bypassed (downstream-clearance agrees here too, since flow heads toward the "
    "same edge being measured)."
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
# grid (same ROW_MARGIN buffer as SINGLE_COLUMN_DEM above), both served by
# one production area sitting between them.

_two_column_array = np.zeros((ROWS, SIZE), dtype=np.float32)
for _row in range(ROWS):
    for _col in range(SIZE):
        _two_column_array[_row, _col] = min(abs(_col - 8), abs(_col - 32)) * 2.0 + (_row - ROW_MARGIN) * 0.5

TWO_COLUMN_DEM = {
    "array": _two_column_array,
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500000.0 + ROW_MARGIN * RESOLUTION[1],
    "crs": CRS,
}
BETWEEN_PRODUCTION_AREA = [
    {"id": 0, "representative_elevation_m": -5.0, "polygon_utm": box(500080.0, 4499750.0, 500120.0, 4499800.0)}
]

two_zone_mask, _ = compute_water_eligible_cells(TWO_COLUMN_DEM, BETWEEN_PRODUCTION_AREA, BOUNDARY)
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
print(
    "zones_to_geojson output is schema-valid, layer='water_system_candidate', polygon geometry, carries "
    "elevation-relationship data, feature id keyed off the new sequential zone id."
)

below_geojson = zones_to_geojson(below_zones)
below_feature = below_geojson["features"][0]
assert below_feature["properties"]["primary_production_area_relationship"]["above_production_area"] is False
print("Below-elevation zone's GeoJSON feature reports above_production_area=False, not omitted or excluded.")


# =====================================================================
# _downstream_clearance_meters(): dedicated, hand-built flow_direction_
# cells arrays -- exact, hand-computable path lengths, not derived from
# a real elevation surface, so every accumulated_distance/exited result
# below is independently verifiable by construction.
# =====================================================================

# A trivial (1, 1.0m)-resolution DEM: cell (0, col)'s center sits at
# x=col exactly (origin_x=-0.5), y=0 (origin_y=0.5). A hand-built
# flow_direction_cells walks straight east, cell by cell, from col=0 to
# col=9, where it dead-ends (-1, -1); a synthetic boundary at x < 4.5
# means the path exits after crossing from col=4 (x=4, inside) to col=5
# (x=5, outside).
_PROBE_DEM = {
    "array": np.zeros((1, 10), dtype=np.float32),
    "resolution_meters": (1.0, 1.0),
    "origin_x": -0.5,
    "origin_y": 0.5,
    "crs": CRS,
}
_PROBE_BOUNDARY = box(-100.0, -100.0, 4.5, 100.0)

_straight_flow = np.full((1, 10, 2), -1, dtype=np.int32)
for _col in range(9):
    _straight_flow[0, _col] = (0, _col + 1)
# col=9 keeps its default (-1, -1) -- a real dead end.

# --- known path length to the boundary, exited=True ---

distance, exited = _downstream_clearance_meters(_PROBE_DEM, _straight_flow, _PROBE_BOUNDARY, 0, 0)
assert exited is True, "the straight path from col=0 should cross the boundary at x=4.5 and register a real exit"
assert abs(distance - 5.0) < 1e-9, (
    f"the path crosses into the boundary at col=5 (5 steps of 1m each from col=0) -- expected exactly 5.0m, "
    f"got {distance}"
)
print(
    f"_downstream_clearance_meters(): a hand-built straight path with a known 5-step crossing correctly "
    f"reports (distance={distance}, exited={exited})."
)

# --- (-1, -1)-terminated dead end, exited=False, distance still real ---
#
# Uses a huge boundary that comfortably contains the ENTIRE grid (unlike
# _PROBE_BOUNDARY above, which the path would cross well before reaching
# its dead end) -- the only way this path can stop here is the real
# (-1, -1) dead end at col=9, not a boundary crossing.
_no_exit_boundary = box(-100.0, -100.0, 100.0, 100.0)

distance, exited = _downstream_clearance_meters(_PROBE_DEM, _straight_flow, _no_exit_boundary, 0, 7)
assert exited is False, "starting at col=7, the path dead-ends at col=9 (-1, -1) before ever crossing the boundary"
assert abs(distance - 2.0) < 1e-9, (
    f"col=7 -> col=8 -> col=9 is 2 real steps (2.0m) before the dead end -- got {distance}"
)
print(
    f"_downstream_clearance_meters(): a synthetic (-1, -1)-terminated dead end correctly reports "
    f"exited=False (distance={distance} still real and non-zero, not discarded)."
)

# --- cycle guard: a path that loops back on itself stops immediately, ---
# --- exited=False, not a false clearance                              ---

_cycle_flow = np.full((1, 3, 2), -1, dtype=np.int32)
_cycle_flow[0, 0] = (0, 1)
_cycle_flow[0, 1] = (0, 2)
_cycle_flow[0, 2] = (0, 0)  # cycles back to the start
_cycle_boundary = box(-100.0, -100.0, 100.0, 100.0)  # comfortably contains all 3 cells -- no real exit possible here

distance, exited = _downstream_clearance_meters(_PROBE_DEM, _cycle_flow, _cycle_boundary, 0, 0)
assert exited is False, "a cycle must never be reported as a confirmed boundary exit"
assert abs(distance - 2.0) < 1e-9, (
    f"col=0 -> col=1 -> col=2 (2 real steps, 2.0m) before col=2 -> col=0 repeats a visited cell -- got {distance}"
)
print(
    f"_downstream_clearance_meters(): a cycle (col 0->1->2->0) is caught by the visited-set guard and "
    f"correctly stops with exited=False (distance={distance}), not a false clearance value."
)

# --- max_steps guard: a genuinely long path that never reaches its    ---
# --- real exit within the step budget also reports exited=False       ---

distance, exited = _downstream_clearance_meters(_PROBE_DEM, _straight_flow, _PROBE_BOUNDARY, 0, 0, max_steps=3)
assert exited is False, "capped at 3 steps, the walk never reaches its real exit at step 5"
assert abs(distance - 3.0) < 1e-9, f"3 real steps (3.0m) should accumulate before the step cap stops it, got {distance}"
print(
    f"_downstream_clearance_meters(): capped at max_steps=3 (short of the real 5-step exit), correctly "
    f"reports exited=False (distance={distance}) rather than continuing past the budget."
)


# --- the new gate correctly excludes/admits cells relative to threshold ---
# --- and exited status, on a real DEM run through compute_water_       ---
# --- eligible_cells() end-to-end (not the hand-built probes above)      ---

for _clearance, _expected_count in ((5.0, 135), (MIN_DOWNSTREAM_CLEARANCE_METERS, 135), (100.0, 61), (200.0, 0)):
    _mask, _ = compute_water_eligible_cells(
        SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, min_downstream_clearance_meters=_clearance
    )
    _count = int(_mask.sum())
    assert _count == _expected_count, (
        f"min_downstream_clearance_meters={_clearance} should yield exactly {_expected_count} eligible cells "
        f"on this fixture, got {_count}"
    )
print(
    "The downstream-clearance gate correctly excludes/admits cells relative to its threshold on a real DEM: "
    "135 cells pass at both a lenient (5m) and the default (15m) clearance, dropping to 61 at 100m and 0 at "
    "200m -- cells further from the boundary along their own flow path are excluded first, as expected."
)

print("\nAll water_candidate_zones checks passed.")
