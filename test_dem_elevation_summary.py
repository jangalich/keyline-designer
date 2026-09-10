"""
test_dem_elevation_summary.py

Offline (no-network) checks for the branch that DELETED the elevation
lattice fetch: the report's one elevation sentence now comes off the DEM
that fetch_parcel_data() already has in memory, not off 36 sequential EPQS
point requests.

    python test_dem_elevation_summary.py

WHAT THIS FILE IS FOR. Three separate claims, none of which the other test
files cover:

  1. raster_grid.elevation_range_in_polygon() reads the range off the DEM
     over THE PARCEL, not over the fetched window. dem_data.py deliberately
     fetches DEFAULT_BUFFER_METERS of terrain PAST the drawn line, so the
     raw array's min/max (dem_data.summarize_dem()'s numbers) describe a
     bigger piece of ground than the owner's. Masking is not a refinement
     here; without it the report would quote the neighbour's hillside.
     Section 1 puts deliberately extreme values in that margin and asserts
     they do not reach the answer.

  2. The mask is THE PIPELINE'S OWN, not a second one. The in-boundary
     cells are exactly raster_grid.cells_in_polygon()'s -- pixel-center
     containment, the same test production_area.compute_step1_eligible_
     cells() applies for the parcel boundary. Asserted by equality against
     that function in section 2, so a drift shows up here rather than as a
     report quoting an elevation range over different ground than the
     design was computed on.

  3. THE TWO SOURCES, COMPARED OFFLINE. Section 4 builds the 6x6 lattice
     get_elevation_grid() would have built for a boundary, reads the DEM at
     exactly those points, and compares that answer against the masked DEM
     answer over the same boundary. This is NOT the live comparison (that
     needs both real services -- see diagnose_elevation_source_and_fetch_
     cost.py, which runs it against USGS for real); it isolates the two
     mechanisms that make the lattice disagree even when both sources agree
     perfectly on every point:

       * the lattice samples the bounding BOX, not the boundary, so on any
         non-rectangular parcel some of its 36 points stand off-parcel;
       * 36 points cannot resolve ground a few thousand cells can, so a
         real high or low spot between lattice nodes is simply missed.

     Both are demonstrated on a synthetic terrain whose peak and pit sit
     where the lattice does not look.

  4. The report's elevation sentence renders from those figures, and says
     what it is really counting.
"""

import numpy as np
from shapely.geometry import Point, Polygon

import raster_grid
import report_generator
from raster_grid import cells_in_polygon, elevation_range_in_polygon

# --- a DEM in dem_data.get_dem_for_boundary()'s own dict shape ---------
#
# 60x60 cells at 5 m, origin at a realistic UTM magnitude (float behaviour
# at 1e5/1e6 is part of what the grid helpers have to survive -- see
# cell_union_footprint()'s GRID-SEAM FIX). The boundary is an L-shape
# INSIDE that window, so the fixture has both of the things that matter:
# a buffer margin outside the parcel, and a concavity the bounding box
# covers but the parcel does not.

RESOLUTION = 5.0
ORIGIN_X = 585000.0
ORIGIN_Y = 4500000.0
ROWS = COLS = 60


def _dem(array) -> dict:
    return {
        "array": array,
        "resolution_meters": (RESOLUTION, RESOLUTION),
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": "EPSG:32617",
    }


def _xy(row, col):
    return raster_grid.pixel_center_xy(_dem(np.zeros((ROWS, COLS))), row, col)


# The parcel: an L, occupying rows/cols 10..49 minus the 30..49 x 30..49
# quadrant. Everything outside it, out to the grid edge, is the buffer
# margin dem_data.py fetches on purpose.
_x_lo = ORIGIN_X + 10 * RESOLUTION
_x_hi = ORIGIN_X + 50 * RESOLUTION
_y_hi = ORIGIN_Y - 10 * RESOLUTION
_y_lo = ORIGIN_Y - 50 * RESOLUTION
_x_mid = ORIGIN_X + 30 * RESOLUTION
_y_mid = ORIGIN_Y - 30 * RESOLUTION

BOUNDARY = Polygon(
    [
        (_x_lo, _y_lo),
        (_x_mid, _y_lo),
        (_x_mid, _y_mid),
        (_x_hi, _y_mid),
        (_x_hi, _y_hi),
        (_x_lo, _y_hi),
    ]
)


# =====================================================================
# 1. THE BUFFER MARGIN IS EXCLUDED
# =====================================================================
#
# A gentle plane over the whole window, then two absurd values planted in
# the margin -- the kind of thing a neighbouring ridge or quarry would be.
# The raw array's min/max sees them; the parcel's must not.

_plane = np.zeros((ROWS, COLS), dtype="float32")
for _r in range(ROWS):
    _plane[_r, :] = 300.0 + _r * 0.25          # 300.0 .. 314.75

_margin = _plane.copy()
_margin[2, 2] = 90.0        # a hole outside the parcel, at the NW margin
_margin[57, 57] = 900.0     # a peak outside the parcel, at the SE margin

_masked = elevation_range_in_polygon(_dem(_margin), BOUNDARY)

_raw_valid = _margin[~np.isnan(_margin)]
assert _raw_valid.min() == 90.0 and _raw_valid.max() == 900.0, "fixture: the margin holds the extremes"

assert _masked["min_meters"] > 90.0, _masked
assert _masked["max_meters"] < 900.0, _masked
# The parcel spans rows 10..49 -> 302.5 .. 312.25 on the plane.
assert abs(_masked["min_meters"] - 302.5) < 1e-4, _masked
assert abs(_masked["max_meters"] - 312.25) < 1e-4, _masked
assert abs(_masked["relief_meters"] - 9.75) < 1e-4, _masked
assert _masked["resolution_meters"] == RESOLUTION

print(
    f"1. THE BUFFER MARGIN IS EXCLUDED: the fetched window's own range is "
    f"{_raw_valid.min():.1f}m to {_raw_valid.max():.1f}m (dem_data.summarize_dem()'s numbers, "
    f"including {raster_grid.__name__}-external ground dem_data.py buffers in on purpose); the "
    f"PARCEL's is {_masked['min_meters']:.2f}m to {_masked['max_meters']:.2f}m over "
    f"{_masked['cell_count']} in-boundary cells. Neither planted extreme reached the answer."
)


# =====================================================================
# 2. THE MASK IS THE PIPELINE'S OWN, NOT A SECOND ONE
# =====================================================================

_cells = cells_in_polygon(_dem(_margin), BOUNDARY)
_values = np.array([_margin[r, c] for r, c in _cells])
assert _masked["cell_count"] == len(_cells), (_masked["cell_count"], len(_cells))
assert abs(_masked["min_meters"] - float(_values.min())) < 1e-9
assert abs(_masked["max_meters"] - float(_values.max())) < 1e-9

# The L's own cell count, computed independently of both: 40x40 minus the
# 20x20 quadrant the L excludes.
assert len(_cells) == 40 * 40 - 20 * 20 == 1200, len(_cells)

print(
    f"2. THE MASK IS THE PIPELINE'S OWN: the {_masked['cell_count']} cells the range was taken "
    f"over are EXACTLY raster_grid.cells_in_polygon()'s -- pixel-center containment, the same test "
    f"production_area STEP 1 applies to the parcel boundary -- and match the L's independently "
    f"computed 40x40 - 20x20 = 1200 cells. No second masking pass exists to drift from it."
)


# =====================================================================
# 3. NODATA, AND THE TWO EMPTY OUTCOMES
# =====================================================================

_holed = _plane.copy()
_holed[10:50, 10:50] = np.nan       # every in-boundary cell but one
_holed[12, 12] = 305.0
_one = elevation_range_in_polygon(_dem(_holed), BOUNDARY)
assert _one["cell_count"] == 1 and _one["min_meters"] == _one["max_meters"] == 305.0, _one
assert _one["relief_meters"] == 0.0, _one

_all_nan = _plane.copy()
_all_nan[:, :] = np.nan
assert elevation_range_in_polygon(_dem(_all_nan), BOUNDARY) == {}, "all-nodata must read as no data"

# A polygon placed on real ground but too small for any cell center, and an
# empty one: both are the "nothing to say" outcome, not an error.
_sliver = Polygon([(_x_lo + 0.1, _y_lo + 0.1), (_x_lo + 0.2, _y_lo + 0.1), (_x_lo + 0.2, _y_lo + 0.2)])
assert elevation_range_in_polygon(_dem(_plane), _sliver) == {}
assert elevation_range_in_polygon(_dem(_plane), Polygon()) == {}

print(
    "3. NODATA AND THE EMPTY OUTCOMES: NaN cells are dropped (1 real cell among 1199 nodata ones "
    "still answers, with relief 0.0); an all-nodata parcel, a sub-cell sliver and an empty polygon "
    "each return {} -- the 'nothing to say' outcome, never an exception."
)


# =====================================================================
# 4. THE TWO SOURCES, COMPARED OFFLINE
# =====================================================================
#
# ONE TERRAIN, TWO READINGS. The DEM below carries a peak and a pit placed
# deliberately BETWEEN lattice nodes, and the L-shaped parcel leaves a
# quadrant of the bounding box off-parcel. Both readings are taken from THIS
# SAME ARRAY -- there is no second elevation service here and no vertical
# datum question -- so every difference below is the LATTICE's geometry, not
# a disagreement between USGS products. The live version of this comparison
# is diagnose_elevation_source_and_fetch_cost.py.

_terrain = np.zeros((ROWS, COLS), dtype="float32")
for _r in range(ROWS):
    for _c in range(COLS):
        _terrain[_r, _c] = 300.0 + _r * 0.25 + _c * 0.05

# A pit and a peak ON the parcel, each one cell wide, between lattice nodes.
_terrain[23, 17] = 288.0
_terrain[41, 23] = 325.0
# A tower OFF the parcel, inside the L's missing quadrant -- and ON a
# lattice node, so the bounding-box lattice reports a parcel high point
# that stands on ground the owner does not own.
_terrain[42, 42] = 400.0

_lattice_size = 6
_min_x, _min_y, _max_x, _max_y = BOUNDARY.bounds


def _lattice_points(grid_size):
    """The points elevation_data.get_elevation_grid() would sample: an
    evenly spaced grid_size x grid_size lattice over the boundary's BOUNDING
    BOX, exactly as that function builds it (its own docstring says it
    samples the box, not the shape)."""
    points = []
    for i in range(grid_size):
        for j in range(grid_size):
            x = _min_x + (_max_x - _min_x) * (i / (grid_size - 1))
            y = _min_y + (_max_y - _min_y) * (j / (grid_size - 1))
            points.append((x, y))
    return points


def _sample(dem, x, y):
    """Nearest-cell read of `dem` at a real-world point, clamped to the
    grid -- stands in for a point elevation service that agrees with this
    DEM exactly."""
    px, py = dem["resolution_meters"]
    col = min(COLS - 1, max(0, int((x - dem["origin_x"]) // px)))
    row = min(ROWS - 1, max(0, int((dem["origin_y"] - y) // py)))
    return float(dem["array"][row, col])


_terrain_dem = _dem(_terrain)
_lattice = [_sample(_terrain_dem, x, y) for x, y in _lattice_points(_lattice_size)]
_lattice_min, _lattice_max = min(_lattice), max(_lattice)

_dem_answer = elevation_range_in_polygon(_terrain_dem, BOUNDARY)

_FEET = 1.0 / 0.3048
_min_gap_m = _dem_answer["min_meters"] - _lattice_min
_max_gap_m = _dem_answer["max_meters"] - _lattice_max

# The lattice misses the parcel's real low point (the pit sits between its
# nodes) and overstates its high point (the off-parcel tower is inside the
# bounding box it samples).
assert _lattice_min > _dem_answer["min_meters"], (_lattice_min, _dem_answer["min_meters"])
assert _lattice_max > _dem_answer["max_meters"], (_lattice_max, _dem_answer["max_meters"])
assert abs(_dem_answer["min_meters"] - 288.0) < 1e-4, _dem_answer
assert abs(_dem_answer["max_meters"] - 325.0) < 1e-4, _dem_answer

# How many of the 36 lattice points are not even on the parcel. covers(),
# not contains(): a node landing exactly on the boundary ring IS on the
# parcel, and counting those as off-parcel would overstate the case.
_off_parcel = sum(
    1 for x, y in _lattice_points(_lattice_size) if not BOUNDARY.covers(Point(x, y))
)
assert _off_parcel == 9, _off_parcel

print(
    f"4. THE TWO SOURCES, COMPARED OFFLINE (one array, two readings -- no second service, no datum "
    f"question):\n"
    f"     36-point lattice over the bounding box: {_lattice_min:.1f}m to {_lattice_max:.1f}m "
    f"(relief {_lattice_max - _lattice_min:.1f}m)\n"
    f"     DEM masked to the parcel ({_dem_answer['cell_count']} cells): "
    f"{_dem_answer['min_meters']:.1f}m to {_dem_answer['max_meters']:.1f}m "
    f"(relief {_dem_answer['relief_meters']:.1f}m)\n"
    f"     difference: min {_min_gap_m:+.1f}m ({_min_gap_m * _FEET:+.0f}ft), "
    f"max {_max_gap_m:+.1f}m ({_max_gap_m * _FEET:+.0f}ft)\n"
    f"     {_off_parcel} of the lattice's 36 points do not stand on the parcel at all; the "
    f"parcel's real low point sits between its nodes."
)


# =====================================================================
# 5. THE REPORT'S SENTENCE
# =====================================================================
#
# WHAT IT SAYS NOW. "across 36 sample points" was true of a lattice and is
# false of a DEM read: nothing is sampled, and the count is cells inside
# the boundary. A sentence that kept the old wording over the new source
# would be a lie the report tells about its own basis.

_sentence = report_generator._format_elevation_summary(_dem_answer)

assert "sample point" not in _sentence, _sentence
assert "DEM cells inside the boundary" in _sentence, _sentence
assert f"across {_dem_answer['cell_count']} DEM cells" in _sentence, _sentence
assert "~5m resolution" in _sentence, _sentence
assert "945ft to 1066ft" in _sentence, _sentence          # 288.0 m / 325.0 m
assert "total relief: 121ft" in _sentence, _sentence
assert " m " not in _sentence and "m to " not in _sentence, "the sentence reads in feet"

# The empty outcome renders as no-data, exactly as an empty grid used to.
assert report_generator._format_elevation_summary({}) == "No elevation data available."
assert report_generator._format_elevation_summary(None) == "No elevation data available."

print(f"5. THE REPORT'S ELEVATION SENTENCE, from the DEM:\n     {_sentence}")

print("\nAll DEM elevation summary checks passed.")
