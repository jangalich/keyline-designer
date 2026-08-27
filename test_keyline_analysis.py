"""
test_keyline_analysis.py

Offline (no-network) checks for keyline_analysis.py -- the three pure
measurement primitives the keyline exploration pass is built on.

Script style (python3 test_keyline_analysis.py, module-level asserts +
prints), same as every other test in this repo. Every fixture is built from
round numbers so that EVERY asserted value is hand-computable from the
fixture's own geometry, and each fixture carries an EXPECTED-OUTPUT comment
block deriving those numbers before the code runs. A fixture whose expected
numbers cannot be derived on paper is not a test of this module, it is a
snapshot of it.

The exploration SCRIPT (explore_keyline_water_zones.py) is itself a
diagnostic and is not unit-tested here; its own --synthetic flag runs the
whole path end to end over a hand-built two-valley fixture. The one thing
this file does assert about it is the GEOMETRY CONTRACT (test 4), because
that contract is a property of the module/script pair and can only be
checked across both.

What is covered here:
  1. CONTOUR EXTRACTION on a synthetic cone: the level set at a chosen
     elevation is a circle of a known radius, asserted to within one cell,
     with both wire forms present and consistent.
  2. CROSSINGS on a hand-built two-valley fixture: one crossing per branch
     at the test elevation, on the exact expected cells and at the exact
     expected along-branch distances -- plus a WIGGLE CLUSTER that collapses
     to one crossing with its size noted.
  3. PERIMETER DECOMPOSITION on hand-built pool masks, the three archetypes
     this property exhibits: a fully enclosed pool (enclosure 1.0, zero open
     segments, area-per-wall-metre absent rather than infinite), a V-valley
     pool with one open downstream edge, and a contour-band swale (low
     enclosure, long open perimeter) -- plus the UNDETERMINED class, which
     is neither open nor closed.
  4. THE GEOMETRY CONTRACT: every polyline/point these functions return
     carries its WGS84 form beside its UTM form, and
     explore_keyline_water_zones.build_exploration_geojson() -- the export
     -- contains no reprojection call at all.
"""

import inspect
import math

import numpy as np
from shapely.geometry import LineString

import explore_keyline_water_zones as explorer
import keyline_analysis as ka
from keyline_analysis import (
    EDGE_GROUND_CLOSED,
    EDGE_OPEN,
    EDGE_UNDETERMINED,
    decompose_pool_perimeter,
    extract_level_contour,
    find_stem_crossings,
)
from raster_grid import SQUARE_METERS_PER_ACRE, pixel_center_xy

CRS = "EPSG:32617"
ORIGIN_X = 500000.0
ORIGIN_Y = 4500000.0
RESOLUTION = 5.0


def _dem(array) -> dict:
    return {
        "array": np.asarray(array, dtype=np.float64),
        "resolution_meters": (RESOLUTION, RESOLUTION),
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


# ==========================================================================
# TEST 1 -- contour extraction on a synthetic cone
#
# FIXTURE: a 41x41 grid at 5 m resolution. Elevation falls 0.5 m per CELL of
# radial distance from the apex at cell (20, 20):
#
#     z(r, c) = 100.0 - 0.5 * hypot(r - 20, c - 20)
#
# EXPECTED OUTPUT, derived before the code runs:
#   * The level set at z = 95.0 is where hypot(r - 20, c - 20) = 10 cells,
#     i.e. a circle of radius 10 * 5 m = 50.0 m about the apex cell's
#     CENTER (contourpy interpolates between cell centers, so the apex cell
#     center is the exact circle center).
#   * It is a single CLOSED polyline: the circle lies wholly inside the
#     grid (radius 10 cells, 20 cells to the nearest edge).
#   * Its length is 2*pi*50 = 314.159 m for the true circle; the returned
#     polyline is inscribed in it, so its length must be slightly SHORT of
#     that and within a percent or so.
#   * Every vertex's radius must equal 50.0 m to within one cell (5.0 m) --
#     in practice contourpy's linear interpolation puts them at 49.955 to
#     50.000 m, far inside that.
#   * The WGS84 form must have exactly as many vertices as the UTM form and
#     must be a plausible lon/lat pair (this DEM's UTM 17N origin is in
#     western Pennsylvania).
# ==========================================================================

_cone = np.zeros((41, 41), dtype=np.float64)
for _r in range(41):
    for _c in range(41):
        _cone[_r, _c] = 100.0 - 0.5 * math.hypot(_r - 20, _c - 20)
cone_dem = _dem(_cone)

cone_contour = extract_level_contour(cone_dem, 95.0)
assert len(cone_contour) == 1, f"one closed circle expected, got {len(cone_contour)} polyline(s)"
_ring = cone_contour[0]
assert _ring["closed"], "the level set of a cone strictly inside the grid is a closed ring"
assert _ring["elevation_m"] == 95.0

_apex_x, _apex_y = pixel_center_xy(cone_dem, 20, 20)
_radii = [math.hypot(x - _apex_x, y - _apex_y) for x, y in _ring["points_utm"]]
assert all(abs(radius - 50.0) <= RESOLUTION for radius in _radii), (
    f"every vertex must sit within one cell of the 50.0 m radius; got "
    f"{min(_radii):.3f}..{max(_radii):.3f}"
)
assert 0.98 * 2 * math.pi * 50.0 <= _ring["length_m"] <= 2 * math.pi * 50.0, (
    f"an inscribed polygon is slightly shorter than its circle: got {_ring['length_m']} m "
    f"vs {2 * math.pi * 50.0:.3f} m"
)
assert len(_ring["points_wgs84"]) == len(_ring["points_utm"]), "both wire forms, same vertex count"
assert _ring["geometry_wgs84"]["coordinates"] == _ring["points_wgs84"], (
    "geometry_wgs84 must BE the stored WGS84 vertices, not a second derivation of them"
)
_lon, _lat = _ring["points_wgs84"][0]
assert -81.5 < _lon < -78.5 and 40.0 < _lat < 41.5, (
    f"UTM 17N ({ORIGIN_X}, {ORIGIN_Y}) is in western Pennsylvania; got ({_lon}, {_lat})"
)

# The honest empty answers: a level above the cone's apex, and a level below
# its rim, both return [] rather than a degenerate line.
assert extract_level_contour(cone_dem, 200.0) == []
assert extract_level_contour(_dem(np.full((5, 5), np.nan)), 95.0) == []

print(
    f"Test 1 -- contour extraction: the 95.0 m level set of a cone falling 0.5 m/cell from cell "
    f"(20, 20) is ONE closed ring of {_ring['length_m']} m (true circle 314.159 m), every vertex "
    f"{min(_radii):.3f}-{max(_radii):.3f} m from the apex against a hand-derived 50.0 m, well "
    f"inside one {RESOLUTION} m cell. Both wire forms are present with {len(_ring['points_utm'])} "
    "vertices each, and geometry_wgs84 IS the stored WGS84 list. A level off the surface returns "
    "[], as does a fully-nodata DEM."
)


# ==========================================================================
# TEST 2 -- crossings on a hand-built two-valley fixture
#
# FIXTURE: a 20x20 grid at 5 m resolution over a plane rising 1.0 m per row
# southward: z(r, c) = 100.0 + r. Cell (r, c)'s center is at
#     x = 500000 + (c + 0.5) * 5,  y = 4500000 - (r + 0.5) * 5
#
# The valleys are HAND-PLACED rather than delineated, so every number below
# is fixture arithmetic and nothing depends on the D8 hydrology being right
# (that is test_valley_delineation.py's job). Two straight north-flowing
# branches, head at row 15 and outlet at row 3, one at col 5 and one at
# col 14, with each vertex carrying the plane's elevation there:
#     valley 0, branch 0: (15, 5) ... (3, 5)     x = 500027.5
#     valley 1, branch 0: (15, 14) ... (3, 14)   x = 500072.5
#
# EXPECTED OUTPUT at level 110.0, derived before the code runs:
#   * z = 110.0 at row 10 exactly, and contourpy interpolates between cell
#     CENTERS, so the level set is the horizontal line
#     y = 4500000 - 10.5 * 5 = 4499947.5.
#   * It crosses each vertical branch once, at that branch's own x:
#     (500027.5, 4499947.5) and (500072.5, 4499947.5).
#   * Containing cell of (500027.5, 4499947.5):
#       col = floor((500027.5 - 500000) / 5) = floor(5.5) = 5
#       row = floor((4500000 - 4499947.5) / 5) = floor(10.5) = 10   -> (10, 5)
#     and likewise (10, 14). Both are real branch cells, so branch_rowcol
#     equals rowcol here.
#   * along_branch_m: head (15, 5) is at y = 4499922.5; the crossing is at
#     4499947.5, i.e. 25.0 m along (5 rows x 5 m). Same for the other.
#   * channel_elevation_m: the branch vertices carry the plane's elevation,
#     so interpolation at the midpoint of the (10,5)-(11,5) span... no --
#     the crossing lands exactly ON vertex (10, 5), whose elevation is
#     100 + 10 = 110.0. level_residual_m = 0.0.
#   * cluster_size = 1 on both.
# ==========================================================================

_plane = np.zeros((20, 20), dtype=np.float64)
for _r in range(20):
    _plane[_r, :] = 100.0 + _r
plane_dem = _dem(_plane)


def _straight_branch(col: int):
    cells = [(r, col) for r in range(15, 2, -1)]
    utm = [(*pixel_center_xy(plane_dem, r, c), 100.0 + r) for r, c in cells]
    return cells, utm


_cells_a, _utm_a = _straight_branch(5)
_cells_b, _utm_b = _straight_branch(14)
two_valleys = [
    {"id": 0, "branches_rowcol": [_cells_a], "branches_utm": [_utm_a]},
    {"id": 1, "branches_rowcol": [_cells_b], "branches_utm": [_utm_b]},
]

plane_contour = extract_level_contour(plane_dem, 110.0)
assert plane_contour, "a plane's 110.0 m level set is a single horizontal line"
crossings = find_stem_crossings(plane_contour, two_valleys, plane_dem)

assert len(crossings) == 2, f"one crossing per branch expected, got {len(crossings)}"
_expected = {0: (10, 5), 1: (10, 14)}
for crossing in crossings:
    valley_id = crossing["valley_id"]
    assert crossing["branch_index"] == 0
    assert crossing["rowcol"] == _expected[valley_id], (
        f"valley {valley_id}: expected containing cell {_expected[valley_id]}, "
        f"got {crossing['rowcol']}"
    )
    assert crossing["branch_rowcol"] == _expected[valley_id]
    assert abs(crossing["along_branch_m"] - 25.0) < 1e-6, (
        f"valley {valley_id}: expected 25.0 m along the branch, got {crossing['along_branch_m']}"
    )
    assert abs(crossing["channel_elevation_m"] - 110.0) < 1e-6
    assert abs(crossing["level_residual_m"]) < 1e-6
    assert crossing["cluster_size"] == 1
    assert not crossing["collinear_overlap"]
    assert len(crossing["point_wgs84"]) == 2
    assert crossing["geometry_wgs84"]["coordinates"] == crossing["point_wgs84"]

_x_a, _y_a = crossings[0]["point_utm"]
assert abs(_x_a - 500027.5) < 1e-6 and abs(_y_a - 4499947.5) < 1e-6, (
    f"hand-derived crossing (500027.5, 4499947.5), got ({_x_a}, {_y_a})"
)

# --- the wiggle cluster ---------------------------------------------------
# A hand-built contour that zig-zags across the col-5 branch (x = 500027.5)
# three times, at y = 4499945.0, 4499947.5 and 4499950.0. The branch's
# elevation is linear in y: z = 99.5 + (4500000 - y) / 5, so those three
# crossings sit at 110.5, 110.0 and 109.5 m of channel elevation. Against a
# declared level of 110.0 the middle one is the branch's own elevation match
# and is the one that must survive; cluster_size must read 3, and every
# member's along-branch distance (22.5, 25.0, 27.5 m from the head at
# y = 4499922.5) must be reported.
_wiggle = {
    "elevation_m": 110.0,
    "line_utm": LineString(
        [
            (500020.0, 4499945.0),
            (500035.0, 4499945.0),
            (500035.0, 4499947.5),
            (500020.0, 4499947.5),
            (500020.0, 4499950.0),
            (500035.0, 4499950.0),
        ]
    ),
}
wiggle_crossings = find_stem_crossings([_wiggle], [two_valleys[0]], plane_dem)
assert len(wiggle_crossings) == 1, (
    f"a cluster must collapse to ONE crossing, got {len(wiggle_crossings)}"
)
_kept = wiggle_crossings[0]
assert _kept["cluster_size"] == 3, f"the cluster size must be noted; got {_kept['cluster_size']}"
assert _kept["cluster_along_branch_m"] == [22.5, 25.0, 27.5], _kept["cluster_along_branch_m"]
assert abs(_kept["along_branch_m"] - 25.0) < 1e-6, (
    "the survivor is the member closest to the branch's own elevation match at 110.0 m, "
    f"i.e. 25.0 m along; got {_kept['along_branch_m']}"
)
assert abs(_kept["channel_elevation_m"] - 110.0) < 1e-6
assert _kept["rowcol"] == (10, 5)

# The zig-zag never reaches x = 500072.5, so valley 1 has no crossing at all
# -- an honest absence, not a fabricated one.
assert find_stem_crossings([_wiggle], [two_valleys[1]], plane_dem) == []

print(
    "Test 2 -- crossings: on a plane rising 1 m/row with two hand-placed north-flowing branches at "
    "cols 5 and 14, the 110.0 m keyline crosses each exactly once, at hand-derived cells (10, 5) "
    "and (10, 14), 25.0 m along each branch, at a channel elevation of 110.0 m with zero residual. "
    "A hand-built zig-zag crossing one branch three times (at 110.5/110.0/109.5 m) collapses to the "
    "single 110.0 m member with cluster_size=3 and every member's position reported; a branch the "
    "zig-zag never reaches returns no crossing rather than a nearest one."
)


# ==========================================================================
# TEST 3 -- perimeter decomposition, the three archetypes
#
# Every fixture below is at 5 m resolution, so ONE CELL EDGE IS 5.0 m and
# one cell is 25.0 m^2. The waterline is 100.0 m throughout.
# ==========================================================================

WATERLINE = 100.0
EDGE = RESOLUTION            # 5.0 m per cell edge
CELL_AREA = RESOLUTION ** 2  # 25.0 m^2 per cell

# --- 3a. FULLY ENCLOSED ---------------------------------------------------
# A 7x7 grid at 105.0 m (above the waterline) with a 3x3 pool of 95.0 m cut
# into it at rows 2-4, cols 2-4. The pool mask is exactly that 3x3.
#
# EXPECTED, hand-derived:
#   exterior edges of a 3x3 block = 4 sides x 3 = 12, all facing on-grid
#   neighbours at 105.0 >= 100.0 -> all GROUND-CLOSED.
#     ground_closed = 12 * 5.0 = 60.0 m ; open = 0.0 ; undetermined = 0.0
#     total = 60.0 m ; enclosure = 60/60 = 1.0
#     open_segment_count = 0
#     area = 9 * 25.0 = 225.0 m^2 = 225/4046.8564224 = 0.0556 ac
#     pool_area_per_open_meter_m2 = None  (ABSENT, never infinity)
_enclosed = np.full((7, 7), 105.0)
_enclosed[2:5, 2:5] = 95.0
_enclosed_mask = np.zeros((7, 7), dtype=bool)
_enclosed_mask[2:5, 2:5] = True
enclosed = decompose_pool_perimeter(_enclosed_mask, _dem(_enclosed), WATERLINE)

assert enclosed["ground_closed_length_m"] == 12 * EDGE == 60.0
assert enclosed["open_length_m"] == 0.0
assert enclosed["undetermined_length_m"] == 0.0
assert enclosed["total_perimeter_m"] == 60.0
assert enclosed["enclosure_fraction"] == 1.0
assert enclosed["open_segment_count"] == 0 and enclosed["open_segments"] == []
assert enclosed["pool_cell_count"] == 9
assert enclosed["pool_area_m2"] == 9 * CELL_AREA == 225.0
assert abs(enclosed["pool_area_acres"] - 225.0 / SQUARE_METERS_PER_ACRE) < 1e-4
assert enclosed["pool_area_per_open_meter_m2"] is None, (
    "with no open perimeter there is no area-per-wall-metre to quote -- None, never infinity"
)
assert enclosed["edge_counts"] == {EDGE_GROUND_CLOSED: 12, EDGE_OPEN: 0, EDGE_UNDETERMINED: 0}

# --- 3b. V-VALLEY POOL, one open downstream edge --------------------------
# The same 7x7 / 3x3 fixture, but the three cells immediately DOWNSTREAM
# (row 5, cols 2-4) drop to 90.0 m -- below the waterline.
#
# EXPECTED, hand-derived:
#   the 3 south edges of the row-4 pool cells now face 90.0 < 100.0 -> OPEN
#     open = 3 * 5.0 = 15.0 m ; ground_closed = 9 * 5.0 = 45.0 m
#     total = 60.0 m ; enclosure = 45/60 = 0.75
#   those 3 edges are collinear and share endpoints -> ONE segment of 15.0 m
#     with 4 vertices, not closed
#   pool_area_per_open_meter_m2 = 225.0 / 15.0 = 15.0 m^2 per metre of wall
_vvalley = np.full((7, 7), 105.0)
_vvalley[2:5, 2:5] = 95.0
_vvalley[5, 2:5] = 90.0
vvalley = decompose_pool_perimeter(_enclosed_mask, _dem(_vvalley), WATERLINE)

assert vvalley["open_length_m"] == 3 * EDGE == 15.0
assert vvalley["ground_closed_length_m"] == 9 * EDGE == 45.0
assert vvalley["undetermined_length_m"] == 0.0
assert vvalley["total_perimeter_m"] == 60.0
assert vvalley["enclosure_fraction"] == 0.75
assert vvalley["open_segment_count"] == 1, (
    "three collinear open edges sharing endpoints are ONE proposed wall line, not three"
)
_wall = vvalley["open_segments"][0]
assert _wall["length_m"] == 15.0
assert len(_wall["points_utm"]) == 4, f"3 edges chain into 4 vertices, got {len(_wall['points_utm'])}"
assert not _wall["closed"]
assert vvalley["pool_area_per_open_meter_m2"] == 15.0, (
    f"225.0 m^2 of pool over 15.0 m of wall = 15.0; got {vvalley['pool_area_per_open_meter_m2']}"
)
assert vvalley["edge_counts"] == {EDGE_GROUND_CLOSED: 9, EDGE_OPEN: 3, EDGE_UNDETERMINED: 0}
# The wall runs along the pool's southern face: every vertex sits on the
# row-4/row-5 grid line, y = origin_y - 5 * 5 = 4499975.0.
assert all(abs(y - (ORIGIN_Y - 5 * RESOLUTION)) < 1e-9 for _x, y in _wall["points_utm"]), (
    f"the proposed wall must lie on the row-4/row-5 seam; got {_wall['points_utm']}"
)

# --- 3c. CONTOUR-BAND SWALE ----------------------------------------------
# A 5x12 grid. The pool is a 1-cell-tall, 10-cell-wide band across a slope
# at row 2, cols 1-10. Uphill (row 1) is 105.0 m; downhill (row 3) is
# 90.0 m; the two cells capping the band's ends (row 2, cols 0 and 11) are
# 90.0 m too, so the band is open at both ends as well as downhill.
#
# EXPECTED, hand-derived:
#   exterior edges of a 1x10 strip = 10 north + 10 south + 1 west + 1 east
#     north 10 face 105.0 >= 100.0 -> GROUND-CLOSED  = 10 * 5.0 = 50.0 m
#     south 10 face  90.0 <  100.0 -> OPEN           = 10 * 5.0 = 50.0 m
#     west + east    90.0 <  100.0 -> OPEN           =  2 * 5.0 = 10.0 m
#     open = 60.0 m ; ground_closed = 50.0 m ; total = 110.0 m
#     enclosure = 50 / 110 = 0.4545
#   THE OPEN EDGES CHAIN INTO ONE POLYLINE: the west edge shares its bottom
#   corner with the south run's west end, and the east edge shares its
#   bottom corner with the south run's east end, so the proposed wall is a
#   single line up-along-up of 5.0 + 50.0 + 5.0 = 60.0 m with 13 vertices.
#   area = 10 * 25.0 = 250.0 m^2 -> 250 / 60 = 4.167 m^2 per metre of wall,
#   against archetype 3b's 15.0: the swale holds a QUARTER as much water per
#   metre of wall, which is the whole point of reporting the ratio.
_swale_grid = np.full((5, 12), 95.0)
_swale_grid[1, :] = 105.0
_swale_grid[3, :] = 90.0
_swale_grid[2, 0] = 90.0
_swale_grid[2, 11] = 90.0
_swale_mask = np.zeros((5, 12), dtype=bool)
_swale_mask[2, 1:11] = True
swale = decompose_pool_perimeter(_swale_mask, _dem(_swale_grid), WATERLINE)

assert swale["ground_closed_length_m"] == 10 * EDGE == 50.0
assert swale["open_length_m"] == 12 * EDGE == 60.0
assert swale["undetermined_length_m"] == 0.0
assert swale["total_perimeter_m"] == 110.0
assert swale["enclosure_fraction"] == round(50.0 / 110.0, 4) == 0.4545
assert swale["open_segment_count"] == 1
assert swale["open_segments"][0]["length_m"] == 60.0
assert len(swale["open_segments"][0]["points_utm"]) == 13
assert swale["pool_area_m2"] == 250.0
assert swale["pool_area_per_open_meter_m2"] == round(250.0 / 60.0, 3) == 4.167
assert swale["enclosure_fraction"] < vvalley["enclosure_fraction"] < enclosed["enclosure_fraction"]
assert (
    swale["pool_area_per_open_meter_m2"] < vvalley["pool_area_per_open_meter_m2"]
), "the swale must read as far worse value per metre of wall than the V-valley pool"

# --- 3d. UNDETERMINED IS NEITHER OPEN NOR CLOSED --------------------------
# A 4x4 grid whose pool is the single corner cell (0, 0). Its north and west
# edges face OFF THE GRID; its south neighbour (1, 0) is nodata; its east
# neighbour (0, 1) is 105.0 m.
#
# EXPECTED, hand-derived:
#   undetermined = 3 * 5.0 = 15.0 m (2 off-grid + 1 nodata)
#   ground_closed = 1 * 5.0 = 5.0 m ; open = 0.0 m ; total = 20.0 m
#   enclosure = 5 / 20 = 0.25   <- NOT 1.0: the undetermined edges are in
#                                  the denominator, so an unmeasurable pool
#                                  cannot report itself fully enclosed
#   open_segment_count = 0 and pool_area_per_open_meter_m2 = None
_corner = np.full((4, 4), 105.0)
_corner[0, 0] = 95.0
_corner[1, 0] = np.nan
_corner_mask = np.zeros((4, 4), dtype=bool)
_corner_mask[0, 0] = True
corner = decompose_pool_perimeter(_corner_mask, _dem(_corner), WATERLINE)

assert corner["undetermined_length_m"] == 3 * EDGE == 15.0
assert corner["ground_closed_length_m"] == 5.0
assert corner["open_length_m"] == 0.0
assert corner["total_perimeter_m"] == 20.0
assert corner["enclosure_fraction"] == 0.25, (
    "undetermined edges belong in the denominator -- an unmeasurable pool must not read as "
    f"fully enclosed; got {corner['enclosure_fraction']}"
)
assert corner["open_segment_count"] == 0
assert corner["pool_area_per_open_meter_m2"] is None
assert corner["edge_counts"] == {EDGE_GROUND_CLOSED: 1, EDGE_OPEN: 0, EDGE_UNDETERMINED: 3}

# The empty mask: zeros throughout, and no division by zero.
empty = decompose_pool_perimeter(np.zeros((4, 4), dtype=bool), _dem(_corner), WATERLINE)
assert empty["total_perimeter_m"] == 0.0
assert empty["enclosure_fraction"] == 0.0
assert empty["pool_area_per_open_meter_m2"] is None
assert empty["open_segments"] == []

print(
    "Test 3 -- perimeter decomposition, three archetypes at 5 m cells against hand-derived numbers: "
    "(a) a 3x3 fully enclosed pool -> 60.0 m all ground-closed, enclosure 1.00, 0 open segments, "
    "area-per-wall-metre ABSENT; (b) the same pool with its downstream face over 90 m ground -> "
    "45.0 m closed + 15.0 m open in ONE 4-vertex wall line on the row-4/row-5 seam, enclosure 0.75, "
    "15.0 m2 of pool per metre of wall; (c) a 1x10 contour-band swale -> 50.0 m closed + 60.0 m "
    "open chained into ONE 13-vertex wall, enclosure 0.4545, 4.167 m2 per metre -- a quarter the "
    "value of (b). Plus: off-grid and nodata neighbours read UNDETERMINED, stay out of the wall "
    "lines and stay IN the denominator (enclosure 0.25, not 1.0), and an empty mask returns zeros "
    "rather than dividing by one."
)


# ==========================================================================
# TEST 4 -- the geometry contract
#
# (i)  Every polyline/point these functions return carries its WGS84 form
#      beside its UTM form, with the same vertex count, and geometry_wgs84
#      IS the stored WGS84 list rather than a second derivation of it.
# (ii) explore_keyline_water_zones.build_exploration_geojson() -- THE EXPORT
#      -- contains NO reprojection call. WGS84 is built where the geometry
#      is born; the export reads it. This is a grep-style assertion over the
#      function's own source, so adding a reprojection there fails loudly.
# ==========================================================================

_geometry_producers = [
    ("extract_level_contour", cone_contour),
    ("decompose_pool_perimeter open_segments", vvalley["open_segments"] + swale["open_segments"]),
]
for _label, _polylines in _geometry_producers:
    assert _polylines, f"{_label} produced nothing to check the contract against"
    for _p in _polylines:
        assert "points_utm" in _p and "points_wgs84" in _p, f"{_label}: both wire forms required"
        assert len(_p["points_utm"]) == len(_p["points_wgs84"]), f"{_label}: vertex counts differ"
        assert _p["geometry_wgs84"]["type"] == "LineString"
        assert _p["geometry_wgs84"]["coordinates"] == _p["points_wgs84"], (
            f"{_label}: geometry_wgs84 must BE the stored vertices"
        )

for _crossing in crossings:
    assert "point_utm" in _crossing and "point_wgs84" in _crossing
    assert _crossing["geometry_wgs84"]["coordinates"] == _crossing["point_wgs84"]

_REPROJECTION_TOKENS = (
    "warp_transform",
    "transform_geom",
    "rasterio.warp",
    "pyproj",
    "Transformer",
    "EPSG:4326",
    "to_crs",
)
# The DOCSTRING is stripped before grepping: it names transform_geom()
# precisely to say where the reprojection does and does not belong, and an
# assertion that forbade the word would forbid documenting the contract it
# is enforcing. What must contain none of these tokens is the CODE.
_export_source = inspect.getsource(explorer.build_exploration_geojson).replace(
    explorer.build_exploration_geojson.__doc__, ""
)
for _token in _REPROJECTION_TOKENS:
    assert _token not in _export_source, (
        f"the export must reproject nothing -- found {_token!r} in "
        "build_exploration_geojson(). Build the WGS84 form where the geometry is created."
    )

# The same assertion turned around: the module's own geometry builders are
# where the reprojection lives, so it must be present THERE. A contract that
# only says "not here" could be satisfied by emitting no WGS84 at all.
_builder_source = inspect.getsource(ka._polyline) + inspect.getsource(ka._point_pair)
assert "warp_transform" in _builder_source, (
    "the WGS84 form has to be built somewhere -- keyline_analysis._polyline()/._point_pair() "
    "are that somewhere"
)

print(
    "Test 4 -- geometry contract: every polyline from extract_level_contour() and every proposed "
    "wall line from decompose_pool_perimeter(), and every point from find_stem_crossings(), carries "
    "points_wgs84 beside points_utm with matching vertex counts, and geometry_wgs84 IS the stored "
    "WGS84 list. build_exploration_geojson()'s CODE -- the export -- contains none of "
    f"{list(_REPROJECTION_TOKENS)} (its docstring is stripped first, since naming them is how the "
    "contract gets documented), while keyline_analysis._polyline()/._point_pair() do the one "
    "reprojection, at build time, where the geometry is born."
)


print("\nAll keyline_analysis checks passed.")
