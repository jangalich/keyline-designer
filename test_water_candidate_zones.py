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
"""

import numpy as np
from shapely.geometry import box

from feature_schema import validate_feature_collection
from raster_grid import cell_area_acres, cell_union_footprint
from water_candidate_zones import (
    MIN_WATER_ZONE_AREA_ACRES,
    compute_water_eligible_cells,
    find_candidate_zones,
    zones_to_geojson,
)

CRS = "EPSG:32617"
RESOLUTION = (5.0, 5.0)

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


# --- compute_water_eligible_cells(): shape, no valley/branch identity, ---
# --- and the drainage column (not the off-column noise) is what        ---
# --- survives all three gates                                          ---

eligible_mask, cell_relationships = compute_water_eligible_cells(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY
)
assert eligible_mask.shape == SINGLE_COLUMN_DEM["array"].shape, (
    f"eligible_mask must match the DEM's own shape {SINGLE_COLUMN_DEM['array'].shape}, got {eligible_mask.shape}"
)
assert eligible_mask.dtype == bool
eligible_cells = [(int(r), int(c)) for r, c in np.argwhere(eligible_mask)]
assert eligible_cells, "expected at least one eligible cell on this synthetic drainage column"
assert all(c == MID_COL for _r, c in eligible_cells), (
    "every eligible cell on this synthetic DEM should sit on the drainage column (col=20) -- "
    f"off-column noise cells should never clear MIN_VALLEY_CONTRIBUTING_AREA_ACRES, got columns "
    f"{sorted(set(c for _r, c in eligible_cells))}"
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
    "cells, all on the real drainage column, each tagged with its own per-cell production-area relationship "
    "-- no valley/branch identity involved."
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
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, max_service_distance_meters=40.0
)
assert too_far_zones == [], (
    "a max_service_distance_meters tighter than the column's actual (~50m) distance to the patch should "
    "exclude every cell -- service-distance bounds are unchanged real filters, not preferences"
)
print("Max service distance is still a real, enforced generation-time filter.")


# --- min service distance floor rejects a near-but-SEPARATE patch, ---
# --- but the distance==0 carve-out still protects cells already    ---
# --- inside/touching a patch -- both in the SAME fixture            ---
#
# TOUCHING straddles rows ~10-19 of the drainage column (distance == 0,
# inside the patch) with an ~7.5m buffer band on either side (rows 8-9 and
# 20-21: genuinely outside the patch, but closer than
# MIN_SERVICE_DISTANCE_METERS=10.0 -- must be excluded).
TOUCHING = [
    {"id": 1, "representative_elevation_m": 100.0, "polygon_utm": box(500095.0, 4499900.0, 500110.0, 4499950.0)}
]
touching_mask, touching_relationships = compute_water_eligible_cells(SINGLE_COLUMN_DEM, TOUCHING, BOUNDARY)
touching_rows = sorted(int(r) for r, c in np.argwhere(touching_mask))

inside_rows = [r for r in range(10, 20) if r in touching_rows]
assert inside_rows == list(range(10, 20)), (
    f"rows 10-19 sit inside/touching the patch (distance==0) and must all be eligible, got {touching_rows}"
)
for r in inside_rows:
    assert touching_relationships[(r, MID_COL)]["distance_m"] == 0.0

near_but_separate_rows = [8, 9, 20, 21]
assert not any(r in touching_rows for r in near_but_separate_rows), (
    f"rows 8-9 and 20-21 sit genuinely OUTSIDE the patch but within MIN_SERVICE_DISTANCE_METERS of it -- "
    f"must be excluded, got eligible rows {touching_rows}"
)
print(
    "Same fixture, both behaviors at once: cells already inside/touching a patch (distance==0, rows 10-19) "
    "are correctly kept eligible, while genuinely-outside-but-too-close cells (rows 8-9/20-21) are correctly "
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


# --- MIN_WATER_ZONE_AREA_ACRES drops a cluster too small to matter ---

huge_min_area_zones = find_candidate_zones(
    SINGLE_COLUMN_DEM, PRODUCTION_AREA_ABOVE, BOUNDARY, min_water_zone_area_acres=1.0
)
assert huge_min_area_zones == [], (
    "raising min_water_zone_area_acres above the real cluster's own area "
    f"({zone['polygon_utm'].area / 4046.8564224:.3f} acres) should drop it entirely"
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

two_zone_mask, _ = compute_water_eligible_cells(TWO_COLUMN_DEM, BETWEEN_PRODUCTION_AREA, BOUNDARY)
eligible_columns = sorted(set(int(c) for _r, c in np.argwhere(two_zone_mask)))
assert eligible_columns == [8, 32], (
    f"expected eligible cells confined to the two known drainage columns (8, 32), got columns {eligible_columns}"
)

two_zones = find_candidate_zones(TWO_COLUMN_DEM, BETWEEN_PRODUCTION_AREA, BOUNDARY)
assert len(two_zones) == 2, f"expected exactly 2 separate zones (one per disconnected column), got {len(two_zones)}"
assert {z["id"] for z in two_zones} == {0, 1}
for z in two_zones:
    assert z["polygon_utm"].area / 4046.8564224 >= MIN_WATER_ZONE_AREA_ACRES
print(
    f"Fragmentation: two genuinely disconnected eligible drainage columns correctly produce 2 separate "
    f"zones (ids {sorted(z['id'] for z in two_zones)}), not one merged shape."
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

print("\nAll water_candidate_zones checks passed.")
