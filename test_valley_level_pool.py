"""
test_valley_level_pool.py

Offline (no-network) checks for valley_level_pool.py: the level-pool
delineation at one anchor cell -- backwater region, dam-axis band,
abutment search, and the per-station cross-section measurements.

Script style (python3 test_valley_level_pool.py, module-level asserts +
prints), same as every other test in this repo. Every fixture is a
synthetic DEM built from round numbers so that EVERY asserted value is
hand-computable from the fixture's own geometry, and each fixture carries
an EXPECTED-OUTPUT comment block deriving those numbers before the code
runs. A fixture whose expected numbers cannot be derived on paper is not a
test of this module, it is a snapshot of it.

What is covered here:
  1. V-valley: dam-band width, both abutment distances, backwater extent,
     and every station's flooded width/area, against hand-computed values.
  2. The DOWNSTREAM GUARANTEE: no zone cell is downstream of the anchor
     (checked by walking the real flow field), and the dam band is the
     zone's downstream edge with the anchor on it.
  3. Flat plain: no abutment within the half-width on either side (both
     flags False, honestly), and a very large backwater.
  4. FILLED-vs-RAW division of labor, in two separately observable
     halves: (a) the elevation tests read RAW -- shown against a reach the
     priority-flood raised by 3 m, where reading filled would more than
     double the reported flooded width; (b) connectivity comes from the
     FILLED flow field -- and its real limit in this repository, where a
     plain (no-epsilon) fill plus a strictly-positive-slope D8 leaves a
     filled pit as an unrouted flat that TRUNCATES the backwater rather
     than being crossed. That limitation is valley_delineation.py's, is
     pre-existing, and is pinned here rather than worked around.
  5. The stem's LOCAL SECANT is continuous, not D8-quantized (what the
     retired straight-line fit was for, without its assumption).
  6. rowcol_for_xy() round-trips pixel_center_xy().
  7. THE CONTRIBUTING-AREA CEILING ON THE DAM BAND: a parallel channel
     below the waterline inside the abutment half-width, carrying more
     contributing area than the ceiling allows, truncates the band on that
     side with crosses_major_drainage -- and does NOT set found=False's
     "no abutment here" story on that side, because those are different
     findings.
  8. THE CEILING IS A NO-OP ON THE BACKWATER BY CONSTRUCTION: no backwater
     cell's contributing area can exceed the anchor's. This is the
     structural guarantee that makes a per-cell runtime check unnecessary,
     so it is asserted here rather than coded there.
  9. CURVED VALLEY: stations sit ON the traced stem past a bend and each
     faces its own local direction -- with the cell, elevation, bearing,
     width and area all hand-derived, and the retired straight axis's
     wrong answer for station 2 computed inline so the difference is a
     fact rather than a claim.
 10. The dam band on that same curved fixture: perpendicular to the stem's
     local direction at the anchor, abutments where the cone geometry puts
     them.
 11. A stem that dies before the last station: UNREACHABLE, with the
     distance actually reached -- never a fabricated 0.0 width. Plus the
     degenerate-window flag.
 12. Contract: every station carries a status; the global straight fit
     (function, constant, eigen-decomposition and output key) exists
     nowhere.

FIXTURE 1 IS THE REGRESSION ANCHOR for this branch's change. On a straight
channel a stem-following station and a fitted-axis station coincide
exactly, so every width, area, abutment and band figure hand-computed for
it before the change must still hold after it, unretuned. They do.
"""

import math

import numpy as np

from keypoint_detection import build_upstream_map
from valley_delineation import (
    compute_flow_accumulation,
    compute_flow_direction,
    fill_depressions,
)
from valley_level_pool import (
    ABUTMENT_SEARCH_HALF_WIDTH_METERS,
    CROSS_SECTION_STATION_SPACING_METERS,
    CROSS_SECTION_STATIONS,
    MAX_BACKWATER_UPSTREAM_METERS,
    POOL_REFERENCE_HEIGHT_METERS,
    STATION_MEASURED,
    STATION_UNREACHABLE_STEM_END,
    STEM_DIRECTION_WINDOW_CELLS,
    bearing_degrees,
    delineate_level_pool,
    local_stem_direction,
    rowcol_for_xy,
)
from raster_grid import pixel_center_xy

CRS = "EPSG:32617"
RESOLUTION = (5.0, 5.0)
ORIGIN_X = 500000.0
ORIGIN_Y = 4500000.0

# The constants every hand-computed number below is derived from. Pinned so
# a retune breaks these tests loudly rather than silently changing what
# they claim to verify.
assert POOL_REFERENCE_HEIGHT_METERS == 2.5
assert ABUTMENT_SEARCH_HALF_WIDTH_METERS == 75.0
assert MAX_BACKWATER_UPSTREAM_METERS == 150.0
assert CROSS_SECTION_STATION_SPACING_METERS == 25.0
assert CROSS_SECTION_STATIONS == 3


def _dem(array):
    return {
        "array": array,
        "resolution_meters": RESOLUTION,
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def _hydrology(dem):
    """The four D8 arrays plus the upstream map, exactly as
    water_candidate_zones.find_candidate_zones() derives them."""
    filled = fill_depressions(dem["array"])
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    accumulation = compute_flow_accumulation(filled, flow_to_row, flow_to_col)
    return filled, flow_to_row, flow_to_col, accumulation, build_upstream_map(flow_to_row, flow_to_col)


# =====================================================================
# FIXTURE 1 -- SYNTHETIC V-VALLEY
#
# 40x40 grid at 5 m. Channel down column 20, running NORTH -> SOUTH (row
# increases southward). Elevation is exactly:
#
#     z(r, c) = 100.0 - 0.1 * r + 1.0 * |c - 20|
#
# i.e. a 2% longitudinal grade down the channel (0.1 m per 5 m row) and a
# 20% side slope out of it (1.0 m per 5 m column). No pits, so filled ==
# raw everywhere and the flow field is unambiguous.
#
# Anchor: (30, 20). EXPECTED OUTPUT, derived before running:
#
#   anchor elevation  z(30,20) = 100 - 3.0 + 0 = 97.0 m
#   waterline         97.0 + 2.5 = 99.5 m
#
#   ABUTMENTS. The dam axis is the perpendicular to the (due-south) valley
#   axis, i.e. due east/west along row 30, where z(30, 20+k) = 97 + |k|.
#   The first sample at or above 99.5 is |k| = 3 (z = 100.0), at a lateral
#   distance of 3 * 5 = 15.0 m. Both sides are symmetric.
#     -> abutment_found_left/right = True, lateral_distance_m = 15.0 both
#     -> band cells = anchor + 3 east + 3 west = 7
#     -> dam_band_width_m = 15.0 + 15.0 + 5.0 (the anchor's own cell) = 35.0
#
#   BACKWATER. A cell (r, c) is in the pool iff its RAW elevation is below
#   99.5, i.e. 100 - 0.1r + |c-20| < 99.5, i.e. |c - 20| < 0.1r - 0.5, and
#   it drains to the anchor (everything at row <= 30 does; nothing at row
#   > 30 can, which is the point of test 2).
#     r = 6..15  -> |k| < 0.1..1.0    -> k = 0            -> 1 cell each
#     r = 16..25 -> |k| < 1.1..2.0    -> k = -1, 0, 1     -> 3 cells each
#     r = 26..30 -> |k| < 2.1..2.5    -> k = -2..2        -> 5 cells each
#     r <= 5     -> |k| < 0           -> nothing
#     total = 10*1 + 10*3 + 5*5 = 10 + 30 + 25 = 65 cells
#   Farthest cell is (6, 20): 24 rows * 5 m = 120 m along-path, under the
#   150 m reach cap, so backwater_distance_limited is False.
#
#   STATIONS (0, 25 m, 50 m upstream = rows 30, 25, 20). Samples run at
#   5 m spacing; width = sample_count * 5, area = sum(depth) * 5.
#     row 30, z = 97.0 + |k|: below 99.5 for |k| <= 2 -> 5 samples
#        width = 25.0 m; depths 0.5,1.5,2.5,1.5,0.5 -> area = 6.5*5 = 32.5
#     row 25, z = 97.5 + |k|: below 99.5 for |k| <= 1 -> 3 samples
#        width = 15.0 m; depths 1.0,2.0,1.0 -> area = 4.0*5 = 20.0
#     row 20, z = 98.0 + |k|: below 99.5 for |k| <= 1 -> 3 samples
#        width = 15.0 m; depths 0.5,1.5,0.5 -> area = 2.5*5 = 12.5
# =====================================================================
V_SIZE = 40
_v_array = np.zeros((V_SIZE, V_SIZE), dtype=np.float64)
for _r in range(V_SIZE):
    for _c in range(V_SIZE):
        _v_array[_r, _c] = 100.0 - 0.1 * _r + 1.0 * abs(_c - 20)
V_DEM = _dem(_v_array)
V_FILLED, V_FTR, V_FTC, V_ACC, V_UP = _hydrology(V_DEM)
V_ANCHOR = (30, 20)

assert np.allclose(V_FILLED, _v_array), "the V-valley fixture has no pits, so filled must equal raw"

v_pool = delineate_level_pool(V_DEM, V_FILLED, V_FTR, V_FTC, V_ACC, V_UP, V_ANCHOR)

assert v_pool["anchor_elevation_m"] == 97.0, v_pool["anchor_elevation_m"]
assert v_pool["waterline_elevation_m"] == 99.5, v_pool["waterline_elevation_m"]

# The stem's local direction at the anchor on a due-south valley is due
# south: (0, -1) in UTM (+x east, +y north), bearing 180. Sign convention:
# it points DOWNSTREAM.
assert abs(v_pool["anchor_direction_unit"][0]) < 1e-9, v_pool["anchor_direction_unit"]
assert abs(v_pool["anchor_direction_unit"][1] + 1.0) < 1e-9, v_pool["anchor_direction_unit"]
assert v_pool["anchor_bearing_deg"] == 180.0
assert v_pool["stem_direction_degenerate"] is False
assert v_pool["stem_cells"][v_pool["anchor_stem_index"]] == V_ANCHOR

assert v_pool["abutment_found_left"] and v_pool["abutment_found_right"]
assert v_pool["abutments"]["left"]["lateral_distance_m"] == 15.0, v_pool["abutments"]["left"]
assert v_pool["abutments"]["right"]["lateral_distance_m"] == 15.0, v_pool["abutments"]["right"]
assert v_pool["abutments"]["left"]["elevation_m"] == 100.0
assert len(v_pool["band_cells"]) == 7, v_pool["band_cells"]
assert v_pool["dam_band_width_m"] == 35.0, v_pool["dam_band_width_m"]

assert len(v_pool["pool_cells"]) == 65, len(v_pool["pool_cells"])
_v_rows = sorted({r for r, _c in v_pool["pool_cells"]})
assert _v_rows[0] == 6 and _v_rows[-1] == 30, (_v_rows[0], _v_rows[-1])
assert max(abs(c - 20) for _r, c in v_pool["pool_cells"]) == 2
assert v_pool["backwater_distance_limited"] is False
assert max(v_pool["pool_cell_distance_m"].values()) == 120.0

# REGRESSION ANCHOR. These are the numbers hand-derived above, and they
# are UNCHANGED by the move from a fitted straight axis to stem-following
# stations -- on a straight channel the two coincide exactly, which is what
# makes this fixture the regression test for that change. Nothing here was
# retuned.
_v_expected_stations = [
    # offset, width, area, along-stem distance, stem cell, channel elevation
    (0.0, 25.0, 32.5, 0.0, (30, 20), 97.0),
    (25.0, 15.0, 20.0, 25.0, (25, 20), 97.5),
    (50.0, 15.0, 12.5, 50.0, (20, 20), 98.0),
]
assert len(v_pool["stations"]) == CROSS_SECTION_STATIONS
for _station, (_offset, _width, _area, _along, _cell, _z) in zip(v_pool["stations"], _v_expected_stations):
    assert _station["on_grid"] is True
    assert _station["status"] == STATION_MEASURED, _station
    assert _station["offset_upstream_m"] == _offset, _station
    assert _station["flooded_width_m"] == _width, _station
    assert _station["flooded_cross_section_area_m2"] == _area, _station
    # Stem-following adds these: the station sits ON the channel, at the
    # along-stem distance asked for, facing due south like the valley.
    assert _station["along_stem_distance_m"] == _along, _station
    assert _station["stem_rowcol"] == _cell, _station
    assert _station["channel_elevation_m"] == _z, _station
    assert _station["bearing_deg"] == 180.0, _station

print(
    "Test 1 -- synthetic V-valley (2% grade, 20% side slopes), anchor (30, 20) at 97.0 m, waterline "
    f"99.5 m: abutments found both sides at {v_pool['abutments']['left']['lateral_distance_m']} m / "
    f"{v_pool['abutments']['right']['lateral_distance_m']} m, dam band {v_pool['dam_band_width_m']} m wide "
    f"({len(v_pool['band_cells'])} cells), backwater {len(v_pool['pool_cells'])} cells spanning rows "
    f"{_v_rows[0]}-{_v_rows[-1]}, stations "
    + ", ".join(
        f"{s['offset_upstream_m']:.0f} m: {s['flooded_width_m']:.1f} m wide / "
        f"{s['flooded_cross_section_area_m2']:.1f} m^2"
        for s in v_pool["stations"]
    )
    + " -- every value matches the hand-computed expectation above."
)


# =====================================================================
# TEST 2 -- THE DOWNSTREAM GUARANTEE.
#
# No cell downstream of the anchor can enter the backwater, because the
# upstream map is the exact inverse of the flow field and only ever
# reaches cells that drain INTO the anchor. Checked here by walking the
# REAL flow field downstream from the anchor and asserting that set is
# disjoint from the pool -- i.e. verified against the flow field, not
# against the same walk the delineation used.
#
# On this fixture the anchor is at row 30 and the grid runs to row 39, so
# there are 9 genuinely-downstream channel cells to be excluded, and rows
# 31-39 of the whole grid sit below the waterline (z(39,20) = 96.1 < 99.5),
# which is what makes the check meaningful: the elevation test ALONE would
# have flooded them.
# =====================================================================
_downstream = set()
_cur = V_ANCHOR
while True:
    _tr = int(V_FTR[_cur[0], _cur[1]])
    _tc = int(V_FTC[_cur[0], _cur[1]])
    if _tr < 0 or (_tr, _tc) in _downstream:
        break
    _downstream.add((_tr, _tc))
    _cur = (_tr, _tc)
assert len(_downstream) == 9, f"the fixture must have real downstream cells to exclude, got {len(_downstream)}"
assert all(_v_array[r, c] < v_pool["waterline_elevation_m"] for r, c in _downstream), (
    "precondition: every downstream cell is BELOW the waterline, so only the upstream-map "
    "construction (not the elevation test) can be what keeps them out"
)
assert not _downstream.intersection(v_pool["zone_cells"]), (
    f"TEST 2: no zone cell may be downstream of the anchor; found "
    f"{sorted(_downstream.intersection(v_pool['zone_cells']))}"
)
assert V_ANCHOR in v_pool["band_cells"], "the anchor must sit ON the dam band"
# The band IS the downstream edge: every pool cell is at or upstream of the
# band's own row on this north-south fixture.
_band_rows = {r for r, _c in v_pool["band_cells"]}
assert _band_rows == {30}, _band_rows
assert max(r for r, _c in v_pool["zone_cells"]) == 30, (
    "TEST 2: the dam band must form the zone's downstream edge -- no zone cell past it"
)
print(
    f"Test 2 -- downstream guarantee: the {len(_downstream)} cells the real flow field reaches downstream "
    "of the anchor are ALL below the waterline and NONE is in the zone; the dam band is the zone's "
    "downstream edge (row 30) with the anchor on it."
)


# =====================================================================
# FIXTURE 3 -- FLAT PLAIN (a barely-tilted plane, no valley at all).
#
# 60x60 grid at 5 m, z(r, c) = 100.0 - 0.01 * r: a 0.2% grade due south
# and NO cross-slope whatsoever.
#
# EXPECTED OUTPUT:
#   anchor (40, 30) elevation = 100 - 0.4 = 99.6; waterline = 102.1.
#   The whole grid's maximum elevation is 100.0 < 102.1, so terrain NEVER
#   rises to the waterline anywhere -- on either side of the dam axis, at
#   any distance. Both abutment flags must therefore be False (a real
#   finding: this ground cannot cheaply impound), with left_grid False on
#   at least the side that stays on the grid, and NOT an exception.
#   The station cross-sections span the full +/-75 m search: 31 samples
#   at 5 m -> flooded width 155.0 m at every station.
#   The backwater is bounded by the 150 m reach cap ALONE -- every cell on
#   this fixture is below the waterline, so nothing else can stop the walk.
#   D8 on a pure plane routes every cell due south with no ties resolved
#   sideways, so each cell has exactly ONE feeder and the fan-out is a
#   single column: 150 m / 5 m = 30 upstream steps plus the anchor = 31
#   cells, and backwater_distance_limited must be True.
# =====================================================================
F_SIZE = 60
_f_array = np.zeros((F_SIZE, F_SIZE), dtype=np.float64)
for _r in range(F_SIZE):
    _f_array[_r, :] = 100.0 - 0.01 * _r
F_DEM = _dem(_f_array)
F_FILLED, F_FTR, F_FTC, F_ACC, F_UP = _hydrology(F_DEM)
F_ANCHOR = (40, 30)

f_pool = delineate_level_pool(F_DEM, F_FILLED, F_FTR, F_FTC, F_ACC, F_UP, F_ANCHOR)
assert abs(f_pool["anchor_elevation_m"] - 99.6) < 1e-6
assert abs(f_pool["waterline_elevation_m"] - 102.1) < 1e-6
assert float(_f_array.max()) < f_pool["waterline_elevation_m"], (
    "precondition: no cell on this fixture reaches the waterline"
)
assert f_pool["abutment_found_left"] is False, f_pool["abutments"]["left"]
assert f_pool["abutment_found_right"] is False, f_pool["abutments"]["right"]
assert f_pool["abutments"]["left"]["lateral_distance_m"] is None
assert f_pool["abutments"]["right"]["lateral_distance_m"] is None
assert f_pool["backwater_distance_limited"] is True, (
    "on ground this flat the reach cap, not the waterline, is what stops the walk"
)
assert len(f_pool["pool_cells"]) == 31, len(f_pool["pool_cells"])
assert max(f_pool["pool_cell_distance_m"].values()) == 150.0
for _station in f_pool["stations"]:
    assert _station["flooded_width_m"] == 155.0, _station
print(
    f"Test 3 -- flat plain: NEITHER abutment found within the {ABUTMENT_SEARCH_HALF_WIDTH_METERS} m "
    "half-width (both flags False -- a real finding, not an error); the backwater is bounded by the "
    f"{MAX_BACKWATER_UPSTREAM_METERS} m reach cap ({len(f_pool['pool_cells'])} cells) and every station's "
    "flooded width runs the full 155.0 m of the search line."
)


# =====================================================================
# FIXTURE 4 -- FILLED-vs-RAW DIVISION OF LABOR.
#
# Two halves, checked separately, because in this codebase they are
# separately observable.
#
# 4a. THE ELEVATION TESTS READ RAW.
#
# The V-valley of fixture 1 plus a TRANSVERSE RIDGE: row 31, columns
# 14-26, raised to 101.0 -- a natural barrier straight across the valley,
# a few meters downstream of the anchor. The priority-flood dams the whole
# reach behind it: the only escape is back up the channel to the grid
# border at row 0, where the channel floor is z(0, 20) = 100.0, so every
# enclosed cell below 100.0 is raised to exactly 100.0. (Verified below on
# row 30: filled reads 100.0 across columns 17-23 where raw reads
# 100, 99, 98, 97, 98, 99, 100.)
#
# The RAW run (the real one) must be BLIND to all of that -- its answers
# must match fixture 1's exactly, because the raw terrain is unchanged:
#     anchor 97.0, waterline 99.5, abutments 15.0 m both sides, 7 band
#     cells, stations 25.0 m/32.5 m^2, 15.0 m/20.0 m^2, 15.0 m/12.5 m^2.
#
# The CONTRAST run passes a dem whose 'array' IS the filled array -- i.e.
# exactly what this module would report if the elevation test read filled
# -- and must differ on every one of those numbers:
#     anchor reads 100.0, waterline 102.5.
#     Abutment: filled row 30 is 100.0 out to |k| = 3, then raw takes over
#       at 101.0 (k=4) and 102.0 (k=5); the first sample at or above 102.5
#       is k = 6 (z = 103.0) -> 30.0 m each side, 13 band cells.
#     Station 0: below 102.5 for |k| <= 5 -> 11 samples -> 55.0 m wide;
#       depths 2.5 for |k| <= 3 (7 samples), 1.5 at |k| = 4, 0.5 at
#       |k| = 5 -> (2.5*7 + 1.5*2 + 0.5*2) * 5 = 21.5 * 5 = 107.5 m^2.
#
# A pool drawn on the filled array would claim a 55 m wide flooded
# cross-section where the real ground holds 25 m: that is the bug the raw
# elevation test exists to prevent, and it is the same fix
# keypoint_detection.py's own #1 makes for the same reason.
#
# 4b. CONNECTIVITY COMES FROM THE FILLED FLOW FIELD -- AND ITS REAL LIMIT.
#
# The walk runs over the upstream map inverted from the flow direction
# computed on the FILLED array, which is what makes it a well-defined,
# acyclic, terminating walk at all.
#
# What that does NOT buy, in this repository, is crossing a depression.
# valley_delineation.fill_depressions() is the PLAIN priority-flood (no
# epsilon), so it raises a pit to EXACTLY its spill elevation, and
# compute_flow_direction() requires a STRICTLY positive slope -- so every
# filled cell ties with the neighbour it should drain to and gets the -1
# "no downhill neighbour" sentinel. That is the flat-tie limitation
# valley_delineation.py's own module docstring already states, and it
# means a pit sitting ON the channel TRUNCATES the backwater at the pit
# rather than being crossed.
#
# Fixture: the V-valley with (24, 20) dug from 97.6 to 90.0, six rows
# upstream of the anchor. Priority-flood raises it to 97.5 -- the
# elevation of its own lowest downstream neighbour (25, 20) -- an exact
# tie, so (24, 20) becomes a flow sink and the walk from the anchor stops
# at row 25.
#
# This is asserted here rather than worked around: it is a pre-existing
# property of the hydrology layer, not of this module, and pinning it
# means that if valley_delineation.py ever grows an epsilon fill or a
# flat-resolution step, this test fails LOUDLY and the (already correct)
# raw-vs-filled split here starts doing visible work instead of silently
# being untestable.
# =====================================================================
R_SIZE = 40
_r_array = _v_array.copy()
_r_array[31, 14:27] = 101.0
R_DEM = _dem(_r_array)
R_FILLED, R_FTR, R_FTC, R_ACC, R_UP = _hydrology(R_DEM)

assert list(R_FILLED[30, 17:24]) == [100.0] * 7, list(R_FILLED[30, 17:24])
assert list(_r_array[30, 17:24]) == [100.0, 99.0, 98.0, 97.0, 98.0, 99.0, 100.0], list(_r_array[30, 17:24])

r_pool = delineate_level_pool(R_DEM, R_FILLED, R_FTR, R_FTC, R_ACC, R_UP, V_ANCHOR)
assert r_pool["anchor_elevation_m"] == 97.0, r_pool["anchor_elevation_m"]
assert r_pool["waterline_elevation_m"] == 99.5
assert r_pool["abutments"]["left"]["lateral_distance_m"] == 15.0
assert r_pool["abutments"]["right"]["lateral_distance_m"] == 15.0
assert len(r_pool["band_cells"]) == 7
# Station 0 (the anchor) reads exactly the straight-V numbers, because the
# raw terrain at the dam line is unchanged. Stations 1 and 2 come back
# UNREACHABLE, and that is the correct answer rather than a regression:
# the transverse ridge makes the priority-flood raise the whole reach to a
# flat 100.0, every cell ties with its neighbour, and this repo's
# strictly-positive-slope D8 hands them all the -1 sentinel -- so there is
# no traced stem above the anchor at all (stem_upstream_length_m == 0.0).
# Under the retired straight-axis design these two stations reported
# fabricated widths off a fitted direction; they now report the absence of
# a channel to measure. This is valley_delineation.py's flat-tie
# limitation surfacing honestly, FLAGGED not fixed.
assert r_pool["stem_upstream_length_m"] == 0.0, r_pool["stem_upstream_length_m"]
assert r_pool["stations"][0]["status"] == STATION_MEASURED
assert (r_pool["stations"][0]["flooded_width_m"], r_pool["stations"][0]["flooded_cross_section_area_m2"]) == (
    25.0, 32.5
), r_pool["stations"][0]
for _s in r_pool["stations"][1:]:
    assert _s["status"] == STATION_UNREACHABLE_STEM_END, _s
    assert _s["flooded_width_m"] is None and _s["flooded_cross_section_area_m2"] is None, (
        "unreachable must be ABSENT, never a fabricated 0.0 -- see the module docstring"
    )
    assert _s["along_stem_distance_m"] == 0.0, _s

# The contrast: the SAME call with the filled array standing in as the
# elevation source, i.e. what a filled-reading elevation test would say.
_r_dem_filled = dict(R_DEM)
_r_dem_filled["array"] = R_FILLED
r_pool_filled = delineate_level_pool(_r_dem_filled, R_FILLED, R_FTR, R_FTC, R_ACC, R_UP, V_ANCHOR)
assert r_pool_filled["anchor_elevation_m"] == 100.0, r_pool_filled["anchor_elevation_m"]
assert r_pool_filled["waterline_elevation_m"] == 102.5
assert r_pool_filled["abutments"]["left"]["lateral_distance_m"] == 30.0
assert r_pool_filled["abutments"]["right"]["lateral_distance_m"] == 30.0
assert len(r_pool_filled["band_cells"]) == 13
assert (
    r_pool_filled["stations"][0]["flooded_width_m"],
    r_pool_filled["stations"][0]["flooded_cross_section_area_m2"],
) == (55.0, 107.5), r_pool_filled["stations"][0]
print(
    "Test 4a -- the elevation tests read RAW: behind a transverse ridge the priority-flood raises the "
    "whole reach to 100.0 m, but the real run still reports the raw anchor (97.0 m), raw abutments "
    "(15.0 m each side) and a raw 25.0 m station-0 width. Reading the FILLED array instead would claim a "
    "100.0 m anchor, 30.0 m abutments and a 55.0 m flooded width at the dam line -- more than double the "
    "real ground. Stations 1 and 2 correctly report unreachable_stem_end (the flat-filled reach leaves no "
    "traceable stem above the anchor) rather than the fabricated widths the retired straight-axis design "
    "produced there."
)

_p_array = _v_array.copy()
_p_array[24, 20] = 90.0
P_DEM = _dem(_p_array)
P_FILLED, P_FTR, P_FTC, P_ACC, P_UP = _hydrology(P_DEM)

assert P_FILLED[24, 20] > _p_array[24, 20], "precondition: the priority-flood must actually raise the pit"
assert abs(float(P_FILLED[24, 20]) - 97.5) < 1e-6, float(P_FILLED[24, 20])
assert abs(float(P_FILLED[25, 20]) - 97.5) < 1e-6, "the pit is raised to EXACTLY its downstream neighbour"
assert int(P_FTR[24, 20]) == -1, (
    "the filled pit ties with its spill neighbour, so compute_flow_direction() gives it the -1 "
    "no-downhill-neighbour sentinel -- the flat-tie limitation this test pins"
)

p_pool = delineate_level_pool(P_DEM, P_FILLED, P_FTR, P_FTC, P_ACC, P_UP, V_ANCHOR)
_p_rows = sorted({r for r, _c in p_pool["pool_cells"]})
assert _p_rows[0] == 25, (
    f"TEST 4b: a filled pit is an unrouted flat, so the backwater stops at row 25 (one below the pit); "
    f"got {_p_rows[0]}. If this now reads 6, valley_delineation.py grew an epsilon/flat-resolution fill "
    "-- update this expectation, and note the raw-vs-filled elevation split above now does visible work "
    "on the backwater too."
)
assert (24, 20) not in p_pool["pool_cells"], "the sink cell itself is not reachable from downstream"
# Connectivity is EXACTLY what the filled flow field says: every pool cell
# reaches the anchor by walking the filled flow direction arrays.
for _cell in p_pool["pool_cells"]:
    _walk = _cell
    for _ in range(400):
        if _walk == V_ANCHOR:
            break
        _tr, _tc = int(P_FTR[_walk[0], _walk[1]]), int(P_FTC[_walk[0], _walk[1]])
        assert _tr >= 0, f"TEST 4b: pool cell {_cell} does not drain to the anchor under the filled flow field"
        _walk = (_tr, _tc)
    assert _walk == V_ANCHOR, f"TEST 4b: pool cell {_cell} never reaches the anchor"
print(
    f"Test 4b -- connectivity comes from the FILLED flow field: every one of the {len(p_pool['pool_cells'])} "
    "pool cells drains to the anchor under those exact arrays. The pit at (24, 20) fills to its 97.5 m "
    "spill elevation -- an EXACT tie with (25, 20) -- so this repo's strictly-positive-slope D8 marks it a "
    f"sink and the backwater stops at row {_p_rows[0]} rather than crossing it. That is "
    "valley_delineation.py's documented flat-tie limitation, pinned here so an epsilon fill would surface "
    "it loudly."
)


# =====================================================================
# TEST 5 -- THE LOCAL SECANT IS CONTINUOUS, NOT D8-QUANTIZED.
#
# A channel running at a shallow angle -- 1 column east per 3 rows south,
# i.e. atan2(1, 3) = 18.435 degrees east of due south -- forces D8 to
# alternate between S and SE steps. The raw D8 direction at any single
# cell is therefore always a multiple of 45 degrees (here: 0, due south at
# the anchor), while the stem's local SECANT should recover the real
# 18.435.
#
# This is what the retired straight-line fit was for; the secant keeps its
# one genuine virtue (de-quantizing D8) while dropping the assumption that
# cost it -- that the valley is straight over the whole window.
#
# EXPECTED: the secant bearing lands within 2 degrees of 18.435; the raw
# D8 step at the anchor is more than 10 degrees off it. The print below
# also states what that quantization would cost the abutment search --
# sin(18.435 deg) * 75 m of lateral error at the far end of the walk.
# =====================================================================
D_SIZE = 40
_d_array = np.zeros((D_SIZE, D_SIZE), dtype=np.float64)
for _r in range(D_SIZE):
    for _c in range(D_SIZE):
        # Channel center drifts east by 1 column per 3 rows; a 40%
        # longitudinal grade (2.0 m per 5 m row) keeps the channel running
        # genuinely downhill despite that drift.
        _center = 8.0 + _r / 3.0
        _d_array[_r, _c] = 200.0 - 2.0 * _r + 1.0 * abs(_c - _center)
D_DEM = _dem(_d_array)
D_FILLED, D_FTR, D_FTC, D_ACC, D_UP = _hydrology(D_DEM)
D_ANCHOR = (24, 16)  # 8 + 24/3 = 16 exactly -- on the channel center

d_pool = delineate_level_pool(D_DEM, D_FILLED, D_FTR, D_FTC, D_ACC, D_UP, D_ANCHOR)
_fitted_deg = d_pool["anchor_bearing_deg"]
# Downstream here is SOUTH-and-EAST: bearing 180 - 18.435 = 161.565.
_true_deg = 180.0 - math.degrees(math.atan2(1.0, 3.0))
_d8_target = (int(D_FTR[D_ANCHOR]), int(D_FTC[D_ANCHOR]))
_ax, _ay = pixel_center_xy(D_DEM, *D_ANCHOR)
_bx, _by = pixel_center_xy(D_DEM, *_d8_target)
_d8_deg = bearing_degrees((_bx - _ax, _by - _ay))
_d8_error = abs(_d8_deg - _true_deg)
_secant_error = abs(_fitted_deg - _true_deg)
assert abs(_d8_deg % 45.0) < 1e-9, f"precondition: a raw D8 step is always a multiple of 45 deg, got {_d8_deg}"
assert _d8_error > 10.0, (
    f"precondition: the raw D8 step must be badly quantized here, got {_d8_deg:.2f} deg against a real "
    f"{_true_deg:.3f} deg valley"
)
# A +/-2-cell secant cannot resolve a drift whose period is 3 cells
# exactly -- 4 cells of run carry 1 column of D8 drift where the true
# figure is 1.33 -- so a few degrees of residual is inherent to the window
# size, not a defect. What the test pins is that the residual is a small
# FRACTION of the raw D8 error, which is the whole reason a window exists.
assert _secant_error < 5.0, (
    f"the secant must land near the real bearing ({_true_deg:.3f} deg), got {_fitted_deg:.2f} deg"
)
assert _secant_error * 3.0 < _d8_error, (
    f"the secant ({_secant_error:.2f} deg off) must be at least 3x closer than the raw D8 step "
    f"({_d8_error:.2f} deg off)"
)
# What the quantization costs in practice: how far off the abutment search
# would look at the far end of its own half-width.
_lateral_error_m = ABUTMENT_SEARCH_HALF_WIDTH_METERS * math.sin(math.radians(_d8_error))
print(
    f"Test 5 -- the local secant is continuous: on a channel running 18.435 deg east of south (bearing "
    f"{_true_deg:.3f}), the raw D8 step at the anchor reads bearing {_d8_deg:.1f} (always a multiple of 45, "
    f"{_d8_error:.2f} deg off) while the stem's local secant recovers {_fitted_deg:.2f} "
    f"({_secant_error:.2f} deg off) -- the raw quantization alone would misplace the abutment search by "
    f"{_lateral_error_m:.1f} m at the far end of its {ABUTMENT_SEARCH_HALF_WIDTH_METERS:.0f} m half-width."
)


# =====================================================================
# TEST 6 -- rowcol_for_xy() round-trips pixel_center_xy().
# The perpendicular sampling depends on this inverse being exact; a
# half-cell drift would silently sample the neighbouring cell.
# =====================================================================
for _rc in [(0, 0), (7, 3), (39, 39), (30, 20)]:
    assert rowcol_for_xy(V_DEM, *pixel_center_xy(V_DEM, *_rc)) == _rc, _rc
assert rowcol_for_xy(V_DEM, ORIGIN_X - 1.0, ORIGIN_Y - 1.0) is None, "off-grid must be None, not clamped"
print("Test 6 -- rowcol_for_xy() is the exact inverse of pixel_center_xy(), and returns None off-grid.")


# =====================================================================
# FIXTURE 7 -- THE CONTRIBUTING-AREA CEILING ON THE DAM BAND.
#
# The one place the ceiling can genuinely bind on a delineation. The
# lateral abutment search runs perpendicular to the valley, and on real
# terrain it can cross a SECOND channel that happens to sit below the
# waterline within the 75 m half-width. A dam axis spanning that is not a
# longer wall, it is a wall damming two creeks.
#
# Fixture: the fixture-1 V-valley (channel down column 20, anchor (30, 20)
# at 97.0 m, waterline 99.5 m) with a PARALLEL TRENCH cut down column 22 --
# 2 cells (10 m) east of the channel. Two things about that placement are
# deliberate: it is comfortably BELOW the waterline (0.5 m under the
# channel floor), so the raw-terrain search would walk straight through it
# and only the ceiling can stop the walk; and it sits NEARER than the
# natural abutment at |k| = 3 (15 m), so the ceiling binds FIRST. A trench
# placed outside 15 m would never be reached and the test would assert
# nothing. The trench's contributing area is put above the ceiling by
# handing the delineation a synthetic accumulation grid.
#
# EXPECTED:
#   left  (east): crosses_major_drainage_left  True at 10.0 m,
#                 abutment_found_left  False (never reached),
#                 band cells on that side: 1 (the 5 m cell only -- the
#                 tripping cell at 10 m is NOT included)
#   right (west): untouched -- abutment_found_right True at 15.0 m,
#                 crosses_major_drainage_right False
# =====================================================================
_t_array = _v_array.copy()
_t_array[:, 22] = _t_array[:, 20] - 0.5     # a trench 0.5 m below the channel floor
T_DEM = _dem(_t_array)
T_FILLED, T_FTR, T_FTC, T_ACC, T_UP = _hydrology(T_DEM)

# The ceiling in cell-count terms, and a synthetic accumulation grid that
# puts ONLY the trench column above it. Everything else stays at 1 so the
# check cannot fire anywhere unintended.
_T_CEILING_CELLS = 100.0
_t_accum = np.ones((V_SIZE, V_SIZE), dtype=np.float64)
_t_accum[:, 22] = 250.0
assert _t_accum[30, 22] > _T_CEILING_CELLS and _t_accum[30, 21] <= _T_CEILING_CELLS

t_pool = delineate_level_pool(
    T_DEM, T_FILLED, T_FTR, T_FTC, _t_accum, T_UP, V_ANCHOR,
    max_contributing_cells=_T_CEILING_CELLS,
)
assert t_pool["anchor_elevation_m"] == 97.0
assert t_pool["waterline_elevation_m"] == 99.5
assert float(_t_array[30, 22]) < t_pool["waterline_elevation_m"], (
    "precondition: the trench sits BELOW the waterline, so only the ceiling can stop the walk there"
)
_t_left = t_pool["abutments"]["left"]
_t_right = t_pool["abutments"]["right"]
assert t_pool["dam_band_crosses_major_drainage_left"] is True, _t_left
assert _t_left["major_drainage_distance_m"] == 10.0, _t_left
assert _t_left["major_drainage_contributing_cells"] == 250.0
assert t_pool["abutment_found_left"] is False, (
    "TEST 7: the search stopped at the drainage, so no abutment was found on that side -- but this "
    "must be reported as crosses_major_drainage, NOT as the bare 'no shoulder in range' story"
)
assert _t_left["lateral_distance_m"] is None
assert len(_t_left["band_cells"]) == 1, (
    f"TEST 7: only the 5 m cell joins the band; the tripping cell at 10 m does NOT, got "
    f"{_t_left['band_cells']}"
)
assert (30, 22) not in _t_left["band_cells"]
# The far side is untouched -- one side truncating must not disturb the other.
assert t_pool["dam_band_crosses_major_drainage_right"] is False
assert t_pool["abutment_found_right"] is True
assert _t_right["lateral_distance_m"] == 15.0, _t_right
assert _t_right["major_drainage_distance_m"] is None

# And the contrast that proves the ceiling is the cause: the SAME terrain
# with the check disabled walks straight through the trench to the natural
# abutment at 15 m.
t_pool_unchecked = delineate_level_pool(T_DEM, T_FILLED, T_FTR, T_FTC, _t_accum, T_UP, V_ANCHOR)
assert t_pool_unchecked["dam_band_crosses_major_drainage_left"] is False
assert t_pool_unchecked["abutment_found_left"] is True
assert t_pool_unchecked["abutments"]["left"]["lateral_distance_m"] == 15.0
print(
    "Test 7 -- ceiling on the dam band: a parallel drainage 10 m east of the anchor, 0.5 m below the "
    "channel floor and carrying 250 cells against a 100-cell ceiling, truncates the band at 10.0 m with "
    "crosses_major_drainage_left=True and abutment_found_left=False (a DIFFERENT finding, reported "
    "distinctly); the tripping cell is excluded from the band; the right side still finds its abutment at "
    "15.0 m; and with the check disabled the same walk runs through to 15.0 m -- so the ceiling is what "
    "stopped it."
)


# =====================================================================
# TEST 8 -- THE CEILING IS A NO-OP ON THE BACKWATER, BY CONSTRUCTION.
#
# Every backwater cell is upstream of the anchor, and flow accumulation
# decreases strictly upstream (a cell's accumulation is its own upstream
# contributing count, and the anchor collects all of them). So no
# backwater cell can carry more contributing area than an anchor that
# already cleared the ceiling -- which is why find_candidate_zones()
# applies the ceiling to the nomination mask and the dam band, and does
# NOT re-check it per backwater cell at runtime.
#
# Asserted here against the REAL accumulation grid over the fixture-1
# pool, so the guarantee is verified rather than assumed.
# =====================================================================
_anchor_accum = float(V_ACC[V_ANCHOR[0], V_ANCHOR[1]])
_worst = max(float(V_ACC[r, c]) for r, c in v_pool["pool_cells"])
assert _worst <= _anchor_accum, (
    f"TEST 8: a backwater cell carries {_worst} contributing cells against the anchor's "
    f"{_anchor_accum} -- the structural guarantee is broken and the ceiling WOULD need a runtime "
    "per-cell check on the backwater"
)
for _cell in v_pool["pool_cells"]:
    assert float(V_ACC[_cell[0], _cell[1]]) <= _anchor_accum, _cell
# Same guarantee on the flat-plain fixture, where the pool is a long
# single-file column rather than a fan -- a different shape of walk.
_f_anchor_accum = float(F_ACC[F_ANCHOR[0], F_ANCHOR[1]])
assert all(float(F_ACC[r, c]) <= _f_anchor_accum for r, c in f_pool["pool_cells"])
print(
    f"Test 8 -- ceiling is a no-op on the backwater: across the {len(v_pool['pool_cells'])}-cell V-valley "
    f"pool the largest contributing area on any backwater cell is {_worst:.0f} against the anchor's "
    f"{_anchor_accum:.0f}, and the same holds on the flat-plain fixture -- so an anchor that cleared the "
    "ceiling guarantees its whole backwater does, with no per-cell runtime check."
)


# =====================================================================
# FIXTURE 9 -- CURVED VALLEY: stations follow the stem around a bend.
#
# This is what no straight axis can do, and the reason the fitted axis was
# retired. The channel is an L: down column 10 from row 0 to row 20
# (flowing south), then east along row 20 from column 10 to the grid edge.
# Terrain is a cone around that polyline --
#
#     z(r, c) = min over channel cells p of
#               [ 100.0 - 0.1 * along(p) + 1.0 * euclid_cells((r,c), p) ]
#
# where along(p) counts cells from the channel head. So the channel falls
# 0.1 m per cell (2% at 5 m) and the valley walls rise 1.0 m per cell (20%)
# -- the SAME grades as the straight fixture 1, which is what makes the two
# comparable.
#
# ANCHOR (20, 17): 7 cells east of the corner. along = 27, so z = 97.3 and
# the waterline is 99.8.
#
# THE TRACED STEM, upstream from the anchor, runs west along row 20 to
# (20, 11), then cuts the corner DIAGONALLY to (19, 10) and climbs column
# 10. Along-stem distance therefore reads 5, 10, ... 30 m to (20, 11), then
# 37.071 (a sqrt(2) diagonal step), 42.071, 47.071, 52.071 ...
#
# EXPECTED STATIONS -- each derived from the cell the walk actually lands
# on, its own local secant, and the cone formula above:
#
#   station 0, target 0 m   -> (20, 17), along 0.0, z = 97.3
#       bearing 90 (due east: the +/-2 window is entirely on the row-20 leg)
#       section is N-S; z(20+/-k, 17) = 97.3 + k; below 99.8 for |k| <= 2
#       -> 5 samples -> 25.0 m; depths 2.5,1.5,1.5,0.5,0.5 = 6.5 -> 32.5 m^2
#
#   station 1, target 25 m  -> (20, 12), along 25.0, z = 97.8
#       bearing 104.04: the +/-2 window spans (20,14) -> (19,10), i.e. it
#       is ROUNDING THE CORNER, and the secant says so. Vector (+20, -5)
#       -> atan2(20, -5) = 104.04.
#       section is 14 deg off N-S; at +/-5 m it still samples (19,12) and
#       (21,12), at +/-10 m (18,12)/(22,12) which read 99.8 -- NOT below
#       the waterline -> 3 samples -> 15.0 m; depths 2.0,1.0,1.0 = 4.0
#       -> 20.0 m^2
#
#   station 2, target 50 m  -> (16, 10), along 52.071 (the nearest stem
#       cell to 50 m: |47.071-50| = 2.93 vs |52.071-50| = 2.07), z = 98.4
#       bearing 180 (due south: its window is entirely on the column-10 leg)
#       section is E-W; z(16, 10+/-k) = 98.4 + k; below 99.8 for |k| <= 1
#       -> 3 samples -> 15.0 m; depths 1.4,0.4,0.4 = 2.2 -> 11.0 m^2
#
# WHAT A STRAIGHT AXIS WOULD HAVE DONE, stated so the difference is not a
# matter of opinion. The retired fit at this anchor reads due east (its
# whole window sits on the row-20 leg), so it would have placed station 2
# 50 m due WEST of the anchor -- at cell (20, 7), which is 3 cells PAST
# the corner on ground that has no channel at all: z = 101.0, ABOVE the
# 99.8 waterline, so it would have reported a flooded width of 0.0 m and
# cross-sectioned it N-S instead of E-W. Station 1 happens to coincide
# (the first 7 cells upstream are still on the straight leg), which is
# exactly why this failure mode is easy to miss on gentle terrain and
# catastrophic past a bend.
# =====================================================================
L_SIZE = 40
_L_CHANNEL = [(r, 10) for r in range(0, 21)] + [(20, c) for c in range(11, L_SIZE)]
_L_ALONG = {cell: i for i, cell in enumerate(_L_CHANNEL)}
_l_array = np.zeros((L_SIZE, L_SIZE), dtype=np.float64)
for _r in range(L_SIZE):
    for _c in range(L_SIZE):
        _l_array[_r, _c] = min(
            100.0 - 0.1 * _L_ALONG[_p] + 1.0 * math.hypot(_r - _p[0], _c - _p[1]) for _p in _L_CHANNEL
        )
L_DEM = _dem(_l_array)
L_FILLED, L_FTR, L_FTC, L_ACC, L_UP = _hydrology(L_DEM)
L_ANCHOR = (20, 17)

l_pool = delineate_level_pool(L_DEM, L_FILLED, L_FTR, L_FTC, L_ACC, L_UP, L_ANCHOR)
assert l_pool["anchor_elevation_m"] == 97.3, l_pool["anchor_elevation_m"]
assert l_pool["waterline_elevation_m"] == 99.8
assert l_pool["stem_direction_degenerate"] is False

_l_expected = [
    # along, cell, channel z, bearing, width, area
    (0.0, (20, 17), 97.3, 90.0, 25.0, 32.5),
    (25.0, (20, 12), 97.8, 104.04, 15.0, 20.0),
    (52.071, (16, 10), 98.4, 180.0, 15.0, 11.0),
]
for _station, (_along, _cell, _z, _bearing, _width, _area) in zip(l_pool["stations"], _l_expected):
    assert _station["status"] == STATION_MEASURED, _station
    # ON THE STEM: the station's cell is a real channel cell, and its
    # elevation is that cell's own channel-profile value.
    assert _station["stem_rowcol"] == _cell, _station
    assert _station["along_stem_distance_m"] == _along, _station
    assert _station["channel_elevation_m"] == _z, _station
    assert _cell in _L_ALONG, f"station {_station['station_index']} must sit ON the channel: {_cell}"
    # FACING THE LOCAL CHANNEL DIRECTION, which differs per station here.
    assert _station["bearing_deg"] == _bearing, _station
    # And the hand-computed section.
    assert _station["flooded_width_m"] == _width, _station
    assert _station["flooded_cross_section_area_m2"] == _area, _station

# The three stations genuinely face three different ways -- a straight
# axis would have given all three the same bearing.
assert len({s["bearing_deg"] for s in l_pool["stations"]}) == 3

# THE WRONG-AXIS CONTRAST, computed rather than asserted from memory: the
# cell a due-east straight axis would have put station 2 on, and what it
# reads there.
_wrong_xy = pixel_center_xy(L_DEM, *L_ANCHOR)
_wrong_cell = rowcol_for_xy(L_DEM, _wrong_xy[0] - 50.0, _wrong_xy[1])
assert _wrong_cell == (20, 7), _wrong_cell
assert _wrong_cell not in _L_ALONG, "the straight axis lands OFF the channel -- that is the failure"
_wrong_z = round(float(_l_array[_wrong_cell]), 3)
assert _wrong_z == 101.0, _wrong_z
assert _wrong_z > l_pool["waterline_elevation_m"], (
    "the straight axis would have cross-sectioned ground ABOVE the waterline and reported 0.0 m flooded"
)
print(
    f"Test 9 -- curved valley: all three stations sit ON the channel at along-stem "
    f"{[s['along_stem_distance_m'] for s in l_pool['stations']]} m, reading channel elevations "
    f"{[s['channel_elevation_m'] for s in l_pool['stations']]} m, and each faces its OWN local direction "
    f"{[s['bearing_deg'] for s in l_pool['stations']]} (three different bearings -- 90 on the straight "
    "leg, 104.04 rounding the corner, 180 up the other leg). Hand-computed widths "
    f"{[s['flooded_width_m'] for s in l_pool['stations']]} m and areas "
    f"{[s['flooded_cross_section_area_m2'] for s in l_pool['stations']]} m^2 all match. The retired "
    f"straight axis would have put station 2 on cell {_wrong_cell} -- off the channel entirely, at "
    f"{_wrong_z} m, above the {l_pool['waterline_elevation_m']} m waterline, reporting 0.0 m flooded."
)


# =====================================================================
# TEST 10 -- THE DAM BAND ON THE CURVED FIXTURE.
#
# The band's perpendicular comes from the stem's LOCAL direction at the
# anchor, by the same window rule the stations use. At (20, 17) that is due
# east (bearing 90), so the dam axis runs N-S, and the abutment walk reads
# z(20 +/- k, 17) = 97.3 + k against the 99.8 waterline: the first sample at
# or above it is |k| = 3, i.e. 15.0 m out on each side.
#   -> band = anchor + 3 north + 3 south = 7 cells
#   -> dam_band_width = 15.0 + 15.0 + 5.0 (the anchor's own cell) = 35.0 m
# =====================================================================
assert l_pool["anchor_bearing_deg"] == 90.0, l_pool["anchor_bearing_deg"]
# The dam axis is the +90 rotation of the downstream direction: due north.
assert bearing_degrees(l_pool["dam_axis_unit"]) == 0.0, l_pool["dam_axis_unit"]
assert l_pool["abutment_found_left"] and l_pool["abutment_found_right"]
assert l_pool["abutments"]["left"]["lateral_distance_m"] == 15.0, l_pool["abutments"]["left"]
assert l_pool["abutments"]["right"]["lateral_distance_m"] == 15.0, l_pool["abutments"]["right"]
assert len(l_pool["band_cells"]) == 7, l_pool["band_cells"]
assert l_pool["dam_band_width_m"] == 35.0
assert {c for _r, c in l_pool["band_cells"]} == {17}, (
    "a due-east channel puts the whole dam band in one COLUMN -- if the band ran along the channel "
    "instead, this is the assertion that would catch it"
)
print(
    "Test 10 -- dam band on the curved fixture: the anchor's local stem direction is due east (bearing "
    "90.0), so the band runs due north-south through column 17 only, 7 cells wide, with both abutments "
    "at 15.0 m -- exactly where the cone geometry puts them."
)


# =====================================================================
# FIXTURE 11 -- A STEM THAT DIES EARLY: unreachable is not dry.
#
# 60x60 plane at 5 m, z = 100.0 - 0.01 * r: a 0.2% grade due south and NO
# cross-slope, so D8 routes every cell straight south and each cell has
# exactly ONE feeder -- the stem is a single-file column. The anchor sits
# at row 6, so the walk reaches row 0 and STOPS: there is no row above it.
#
# EXPECTED:
#   stem_upstream_length_m = 6 cells * 5 m = 30.0
#   station 0 (target 0 m)  -> measured at (6, 30)
#   station 1 (target 25 m) -> measured at (1, 30)
#   station 2 (target 50 m) -> UNREACHABLE, along_stem_distance_m = 30.0
#       (the distance actually reached), width and area None -- NOT 0.0
#
# The distinction is the point. Every cell on this plane is below the
# waterline, so a 0.0 m width here would be flatly false; but even where it
# would have been arguable, "the ground rises above the waterline" and
# "there is no channel here to measure" are different facts and the scoring
# branch has to be able to tell them apart.
#
# The dam band still gets a valid direction: the anchor has two cells
# downstream, so its +/-2 window is non-degenerate even though the stem
# ends four cells later.
# =====================================================================
E_SIZE = 60
_e_array = np.zeros((E_SIZE, E_SIZE), dtype=np.float64)
for _r in range(E_SIZE):
    _e_array[_r, :] = 100.0 - 0.01 * _r
E_DEM = _dem(_e_array)
E_FILLED, E_FTR, E_FTC, E_ACC, E_UP = _hydrology(E_DEM)
E_ANCHOR = (6, 30)

e_pool = delineate_level_pool(E_DEM, E_FILLED, E_FTR, E_FTC, E_ACC, E_UP, E_ANCHOR)
assert e_pool["stem_upstream_length_m"] == 30.0, e_pool["stem_upstream_length_m"]
assert e_pool["stem_cells"][-1] == (0, 30), e_pool["stem_cells"]
assert e_pool["stem_direction_degenerate"] is False, (
    "the dam band must still get a real direction from the two downstream cells"
)
assert e_pool["anchor_bearing_deg"] == 180.0

_e_stations = e_pool["stations"]
assert _e_stations[0]["status"] == STATION_MEASURED and _e_stations[0]["stem_rowcol"] == (6, 30)
assert _e_stations[1]["status"] == STATION_MEASURED and _e_stations[1]["stem_rowcol"] == (1, 30)
assert _e_stations[1]["along_stem_distance_m"] == 25.0
assert _e_stations[2]["status"] == STATION_UNREACHABLE_STEM_END, _e_stations[2]
assert _e_stations[2]["along_stem_distance_m"] == 30.0, (
    "an unreachable station must report the distance the walk ACTUALLY reached"
)
assert _e_stations[2]["flooded_width_m"] is None, (
    "TEST 11: unreachable must be ABSENT, never 0.0 -- zero width is a measurement, and this is not one"
)
assert _e_stations[2]["flooded_cross_section_area_m2"] is None
assert _e_stations[2]["sample_count"] is None
assert _e_stations[2]["channel_elevation_m"] is None and _e_stations[2]["bearing_deg"] is None
# Contrast, so "None" is not merely the code's default everywhere: on this
# plane the REACHED stations report the full sampling window, a real 155.0.
assert _e_stations[0]["flooded_width_m"] == 155.0 and _e_stations[1]["flooded_width_m"] == 155.0
print(
    f"Test 11 -- unreachable is not dry: on a plane whose single-file stem hits the grid edge after "
    f"{e_pool['stem_upstream_length_m']} m, stations 0 and 1 measure a real 155.0 m flooded width while "
    f"station 2 reports '{_e_stations[2]['status']}' with along_stem_distance_m="
    f"{_e_stations[2]['along_stem_distance_m']} and width/area ABSENT -- never a fabricated 0.0. The dam "
    "band still gets a valid direction from the anchor's downstream cells."
)

# The degenerate-window flag itself, exercised directly rather than by
# contriving a DEM that produces a one-cell stem: no window can separate
# two cells of a single-cell stem, so the flag fires and the caller is
# handed a usable fallback direction rather than a zero vector.
_deg_dir, _deg_flag = local_stem_direction(E_DEM, [(5, 5)], 0, STEM_DIRECTION_WINDOW_CELLS)
assert _deg_flag is True and _deg_dir == (0.0, -1.0)
_ok_dir, _ok_flag = local_stem_direction(E_DEM, [(6, 5), (5, 5), (4, 5)], 1, STEM_DIRECTION_WINDOW_CELLS)
assert _ok_flag is False and bearing_degrees(_ok_dir) == 180.0
print(
    "Test 11b -- degenerate window: a one-cell stem cannot yield a direction, so the flag fires and a "
    "usable fallback is returned instead of a zero vector; a three-cell stem resolves normally."
)


# =====================================================================
# TEST 12 -- CONTRACT: the station status field, and the retired fit.
# =====================================================================
import valley_level_pool as _vlp  # noqa: E402

for _pool, _label in ((v_pool, "V-valley"), (l_pool, "curved"), (e_pool, "early-death"), (f_pool, "flat")):
    for _station in _pool["stations"]:
        assert "status" in _station, f"{_label}: every station entry must carry a status"
        assert _station["status"] in (STATION_MEASURED, STATION_UNREACHABLE_STEM_END), _station
        assert "along_stem_distance_m" in _station and "stem_rowcol" in _station, _station
        if _station["status"] == STATION_UNREACHABLE_STEM_END:
            assert _station["flooded_width_m"] is None, f"{_label}: unreachable must never report a width"
        else:
            assert _station["flooded_width_m"] is not None

# GREP-STYLE: the global straight fit must not survive anywhere.
for _retired in ("fit_valley_axis", "VALLEY_AXIS_WALK_CELLS"):
    assert not hasattr(_vlp, _retired), f"{_retired} is DELETED -- no global straight fit may survive"
_module_source = open(_vlp.__file__).read()
# Token-precise: "eigh" alone is a substring of "height", which this module
# says a great deal about.
for _token in ("np.linalg.eigh", "eigenvectors", "eigenvalues", "polyfit", "scatter matrix"):
    assert _token not in _module_source, (
        f"the total-least-squares machinery must be gone, not merely unreferenced -- found {_token!r}"
    )
assert "valley_axis_unit" not in _module_source, "the fitted-axis output key is retired too"
print(
    "Test 12 -- contract: every station on every fixture carries a status in "
    f"{{{STATION_MEASURED}, {STATION_UNREACHABLE_STEM_END}}} with along_stem_distance_m and stem_rowcol; "
    "unreachable never reports a width. fit_valley_axis, VALLEY_AXIS_WALK_CELLS, the eigen-decomposition "
    "and the valley_axis_unit key exist nowhere in the module."
)


print("\nAll valley_level_pool checks passed.")
