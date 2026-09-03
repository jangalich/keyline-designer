"""
test_flat_resolution.py

Offline (no-network) checks for valley_delineation.resolve_flats() --
Garbrecht & Martz flat resolution, the branch that replaced the epsilon
fill's flood-order drainage pattern on level ground with one derived from
the flat's own inlet/outlet geometry.

WHAT THIS PINS, AND WHAT IT DOES NOT. The epsilon fill (test_epsilon_fill.
py, still passing and still correct about its own function) gave every
filled cell a defined flow direction. That fixed a real defect. What it
could NOT do is make the direction MEAN anything on genuinely level
ground: Priority-Flood+epsilon rides outward from whichever cell the
priority queue popped first, so the pattern it lays on a flat is a
property of the heap, not of the land. resolve_flats() derives the
pattern from the flat's outlets (where its water leaves) and its inlets
(the higher ground that feeds it) instead.

THE ONE THING THIS BRANCH MUST NOT DO is reintroduce the unroutable-flat
defect the epsilon fixed. TEST 5 is that guarantee, re-asserted under the
new pass and checked against a deliberately-broken variant so it is
measuring something.

Everything here runs against small hand-built arrays -- checks about the
ALGORITHM, independent of any real DEM fetch. The surrounding pipeline is
covered by test_valley_delineation.py; the epsilon primitive by
test_epsilon_fill.py; boundary stability by
test_twi_boundary_independence.py.

Run:  python test_flat_resolution.py   (no network)
"""

import numpy as np

from valley_delineation import (
    FILL_EPSILON_METERS,
    FLAT_RESOLUTION_INCREMENT_METERS,
    FLAT_RESOLUTION_OUTLET_WEIGHT,
    _bfs_hops,
    compute_flow_accumulation,
    compute_flow_direction,
    fill_and_resolve,
    fill_depressions,
    find_flat_regions,
    resolve_flats,
)
from water_survey_areas import DEPRESSION_NOISE_FLOOR_METERS, compute_depression_depth

RESOLUTION = (5.0, 5.0)
INC = FLAT_RESOLUTION_INCREMENT_METERS
W = FLAT_RESOLUTION_OUTLET_WEIGHT
N = np.nan


def _border_mask(shape):
    mask = np.zeros(shape, dtype=bool)
    mask[0, :] = mask[-1, :] = mask[:, 0] = mask[:, -1] = True
    return mask


def _stranded(surface, raw):
    """Valid, NON-BORDER cells with no downhill neighbour. The border is
    excluded because a grid-edge outlet's -1 is the legitimate sentinel
    compute_flow_direction() documents, not a stranding."""
    flow_to_row, _ = compute_flow_direction(surface, RESOLUTION)
    return int(((flow_to_row < 0) & ~_border_mask(surface.shape) & ~np.isnan(raw)).sum())


def _route(surface):
    flow_to_row, flow_to_col = compute_flow_direction(surface, RESOLUTION)
    return flow_to_row, flow_to_col, compute_flow_accumulation(surface, flow_to_row, flow_to_col)


# =====================================================================
# TEST 0 -- the increment constant sits between the same two bounds
#           FILL_EPSILON_METERS does, re-argued for THIS pass's
#           arithmetic rather than inherited by assertion.
# =====================================================================

assert INC == 0.001, INC
assert W == 2.0, W

# LOWER BOUND. The smallest elevation difference resolve_flats() ever
# creates between a cell and the cell it drains into is (W - 1) * INC --
# see the descent proof in its docstring. At W = 2 that is exactly one
# INC, so this pass's float32 margin IS the epsilon's, unchanged.
_MIN_DESCENT = (W - 1.0) * INC
assert abs(_MIN_DESCENT - FILL_EPSILON_METERS) < 1e-12, (_MIN_DESCENT, FILL_EPSILON_METERS)

# Against the REAL dtype dem_data.get_dem_for_boundary() delivers.
_ULP_346 = float(np.spacing(np.float32(346.0)))
_ULP_4000 = float(np.spacing(np.float32(4000.0)))
assert _ULP_346 < _MIN_DESCENT / 20.0, (_ULP_346, _MIN_DESCENT)
assert _ULP_4000 < _MIN_DESCENT / 2.0, (_ULP_4000, _MIN_DESCENT)
for _z in (-86.0, 0.0, 346.0, 1000.0, 4000.0, 6190.0):
    _lifted = np.float32(np.float32(_z) + np.float32(_MIN_DESCENT))
    assert _lifted > np.float32(_z), (_z, _lifted)

# UPPER BOUND. The rise accumulates at W * INC per D8 hop of outlet
# distance, so the hop count a flat needs before it reaches the noise
# floor is the headroom -- and it is HALF the epsilon's, which is the
# honest price of carrying two gradients instead of one.
_HOPS_TO_NOISE_FLOOR = DEPRESSION_NOISE_FLOOR_METERS / (W * INC)
_EPSILON_HOPS = DEPRESSION_NOISE_FLOOR_METERS / FILL_EPSILON_METERS
assert _HOPS_TO_NOISE_FLOOR == 50.0, _HOPS_TO_NOISE_FLOOR
assert _EPSILON_HOPS == 100.0, _EPSILON_HOPS
print(
    f"Constant bounds: FLAT_RESOLUTION_INCREMENT_METERS = {INC} m at outlet weight {W:.0f} "
    f"guarantees a minimum descent of {_MIN_DESCENT} m -- exactly FILL_EPSILON_METERS, so the "
    f"float32 margin is the epsilon's unchanged ({_ULP_346:.2e} m ulp at the reference "
    f"property's ~346 m, {_MIN_DESCENT / _ULP_346:.0f}x under; holds from -86 m to 6190 m). "
    f"Upper bound: the rise reaches the {DEPRESSION_NOISE_FLOOR_METERS} m noise floor only after "
    f"{_HOPS_TO_NOISE_FLOOR:.0f} D8 hops of outlet distance ({_HOPS_TO_NOISE_FLOOR * 5:.0f} m at "
    f"this DEM's 5 m resolution, in one dead-level piece) -- half the epsilon's "
    f"{_EPSILON_HOPS:.0f}-hop headroom, measured not assumed."
)


# =====================================================================
# TEST 1 -- ONE INLET, ONE OUTLET: the drainage direction of every cell
#           on the flat, HAND-DERIVED, cell by cell.
# =====================================================================
#
# A 3x5 flat at 50 m, nodata-walled except for two openings:
#   * ONE INLET  -- the single high cell (1, 1) at 70 m, touching the
#     flat's NW corner, so the flat's inlet cells are (2, 1) and (2, 2).
#   * ONE OUTLET -- the drop to (5, 5) at 40 m, which continues to the
#     grid-border exit (6, 5) at 30 m, so the flat's outlet cells are
#     (4, 4) and (4, 5), the two that touch it.
#
# The exit has to reach the GRID BORDER or the priority flood would never
# get in and would fill the whole flat to 70 m instead -- that is the
# fill's own stated assumption about where water leaves, and building the
# fixture around it is part of what makes this hand-derivable.

FLAT_1 = np.array(
    [
        [N, N, N, N, N, N, N],
        [N, 70.0, N, N, N, N, N],
        [N, 50.0, 50.0, 50.0, 50.0, 50.0, N],
        [N, 50.0, 50.0, 50.0, 50.0, 50.0, N],
        [N, 50.0, 50.0, 50.0, 50.0, 50.0, N],
        [N, N, N, N, N, 40.0, N],
        [N, N, N, N, N, 30.0, N],
    ]
)

_plain_1 = fill_depressions(FLAT_1, epsilon_meters=0.0)
assert np.array_equal(np.nan_to_num(_plain_1, nan=-1.0), np.nan_to_num(FLAT_1, nan=-1.0)), (
    "precondition: this fixture has no depression, so the PLAIN fill must leave it alone -- "
    "everything below is then about flat resolution and nothing else"
)

_labels_1, _regions_1 = find_flat_regions(_plain_1)
_flat_1 = [r for r in _regions_1 if len(r["cells"]) > 1]
assert len(_flat_1) == 1, [len(r["cells"]) for r in _regions_1]
assert len(_flat_1[0]["cells"]) == 15, len(_flat_1[0]["cells"])
assert sorted(_flat_1[0]["outlets"]) == [(4, 4), (4, 5)], sorted(_flat_1[0]["outlets"])
assert sorted(_flat_1[0]["inlets"]) == [(2, 1), (2, 2)], sorted(_flat_1[0]["inlets"])

RESOLVED_1 = fill_and_resolve(FLAT_1)

# HAND-DERIVED INCREMENT, in units of INC. d_outlet is the D8 (Chebyshev)
# hop count to the nearer of (4, 4)/(4, 5); d_inlet the same to the nearer
# of (2, 1)/(2, 2), whose span across this flat is 3; the increment is
# W * d_outlet + (3 - d_inlet) / 3.
#
#   cell    d_out  d_in   increment / INC
#   (2,1)     3      0     2*3 + 3/3 = 7
#   (2,2)     2      0     2*2 + 3/3 = 5
#   (2,3)     2      1     2*2 + 2/3 = 4 + 2/3
#   (2,4)     2      2     2*2 + 1/3 = 4 + 1/3
#   (2,5)     2      3     2*2 + 0   = 4
#   (3,1)     3      1     2*3 + 2/3 = 6 + 2/3
#   (3,2)     2      1     2*2 + 2/3 = 4 + 2/3
#   (3,3)     1      1     2*1 + 2/3 = 2 + 2/3
#   (3,4)     1      2     2*1 + 1/3 = 2 + 1/3
#   (3,5)     1      3     2*1 + 0   = 2
#   (4,1)     3      2     2*3 + 1/3 = 6 + 1/3
#   (4,2)     2      2     2*2 + 1/3 = 4 + 1/3
#   (4,3)     1      2     2*1 + 1/3 = 2 + 1/3
#   (4,4)     0      2     OUTLET -> pinned to 0
#   (4,5)     0      3     OUTLET -> pinned to 0
#
# The two outlets are PINNED AT ZERO rather than carrying their
# away-from-higher term (which would have put (4, 4) at 1/3). That is
# where the flat's water leaves; see resolve_flats() for why lifting an
# outlet is wrong on both kinds -- a spill outlet would gain depression
# depth it did not earn, and a grid-border outlet would gain an IN-GRID
# direction and carry off-window water along the rim instead.
_T = 1.0 / 3.0
EXPECTED_INCREMENT_1 = {
    (2, 1): 7.0, (2, 2): 5.0, (2, 3): 4 + 2 * _T, (2, 4): 4 + _T, (2, 5): 4.0,
    (3, 1): 6 + 2 * _T, (3, 2): 4 + 2 * _T, (3, 3): 2 + 2 * _T, (3, 4): 2 + _T, (3, 5): 2.0,
    (4, 1): 6 + _T, (4, 2): 4 + _T, (4, 3): 2 + _T, (4, 4): 0.0, (4, 5): 0.0,
}
for _cell, _units in EXPECTED_INCREMENT_1.items():
    _got = (RESOLVED_1[_cell] - FLAT_1[_cell]) / INC
    assert abs(_got - _units) < 1e-9, (_cell, _got, _units)

# HAND-DERIVED FLOW DIRECTION. Steepest descent is drop / real ground
# distance, so a cardinal neighbour is compared over 5 m and a diagonal
# one over 7.071 m -- which is why (3, 3) picks the DIAGONAL (4, 4)
# (drop 2 1/3 INC over 7.071 m) over the cardinal (4, 3) (drop 1/3 INC
# over 5 m), and why (2, 2) reaches past its two equal cardinal
# neighbours to the diagonal (3, 3).
EXPECTED_FLOW_1 = {
    (2, 1): (2, 2), (2, 2): (3, 3), (2, 3): (3, 3), (2, 4): (3, 4), (2, 5): (3, 5),
    (3, 1): (3, 2), (3, 2): (3, 3), (3, 3): (4, 4), (3, 4): (4, 4), (3, 5): (4, 5),
    (4, 1): (4, 2), (4, 2): (4, 3), (4, 3): (4, 4), (4, 4): (5, 5), (4, 5): (5, 5),
}
_ftr_1, _ftc_1, _acc_1 = _route(RESOLVED_1)
for _cell, _target in EXPECTED_FLOW_1.items():
    _got = (int(_ftr_1[_cell]), int(_ftc_1[_cell]))
    assert _got == _target, (_cell, _got, _target)

# The whole flat drains through the one outlet: 15 flat cells + the
# single inlet cell (1, 1) + (5, 5) itself = 17 at the spill, 18 at the
# grid-border exit below it.
assert int(_acc_1[5, 5]) == 17, int(_acc_1[5, 5])
assert int(_acc_1[6, 5]) == 18, int(_acc_1[6, 5])
assert _stranded(RESOLVED_1, FLAT_1) == 0

# THE DIRECTION IS INLET-TO-OUTLET, stated as a property rather than read
# off the table: walking downstream from the inlet corner strictly
# decreases the distance to the outlet at every step.
_d_out_1 = _bfs_hops(_flat_1[0]["outlets"], set(_flat_1[0]["cells"]), FLAT_1.shape)
_walk = [(2, 1)]
while _walk[-1] in _d_out_1:
    _r, _c = _walk[-1]
    _nxt = (int(_ftr_1[_r, _c]), int(_ftc_1[_r, _c]))
    if _nxt[0] < 0 or _nxt not in _d_out_1:
        break
    assert _d_out_1[_nxt] <= _d_out_1[(_r, _c)], (_walk[-1], _nxt)
    _walk.append(_nxt)
assert _walk[0] == (2, 1) and _walk[-1] in ((4, 4), (4, 5)), _walk
print(
    f"Test 1 -- one inlet (the 70 m cell at (1,1)), one outlet (the 40 m drop at (5,5)): all 15 "
    f"increments and all 15 flow directions match the hand derivation cell for cell. The walk from "
    f"the inlet corner runs {' -> '.join(str(c) for c in _walk)}, monotonically toward the outlet, "
    f"and the spill carries all 17 upstream cells. No cell on the flat is stranded."
)


# =====================================================================
# TEST 2 -- MULTIPLE OUTLETS: the flat splits between them by distance,
#           which is exactly what a single-source pattern cannot do.
# =====================================================================
#
# A 3x9 flat at 50 m with a drop at EACH end -- (4, 1) to the west and
# (4, 9) to the east, both continuing to the grid border. Neither is
# privileged, so each half of the flat should drain to its own side and
# the divide should fall in the middle.

FLAT_2 = np.full((6, 11), N)
FLAT_2[1, 1:10] = 70.0
FLAT_2[2:5, 1:10] = 50.0
FLAT_2[5, 1] = 40.0
FLAT_2[5, 9] = 40.0
FLAT_2[2:5, 1:10] = 50.0
_FLAT_2_RAW = FLAT_2.copy()

_plain_2 = fill_depressions(FLAT_2, epsilon_meters=0.0)
_labels_2, _regions_2 = find_flat_regions(_plain_2)
_flat_2 = max(_regions_2, key=lambda r: len(r["cells"]))
assert len(_flat_2["cells"]) == 27, len(_flat_2["cells"])
assert sorted(_flat_2["outlets"]) == [(4, 1), (4, 2), (4, 8), (4, 9)], sorted(_flat_2["outlets"])

RESOLVED_2 = fill_and_resolve(FLAT_2)
_ftr_2, _ftc_2, _acc_2 = _route(RESOLVED_2)

# Follow every flat cell downstream and record WHICH of the two exits it
# reaches. A single-source pattern would send all 27 to one of them.
def _exit_of(cell):
    seen = set()
    r, c = cell
    while True:
        if (r, c) in ((5, 1), (5, 9)):
            return (r, c)
        if (r, c) in seen:
            return None
        seen.add((r, c))
        nr, nc = int(_ftr_2[r, c]), int(_ftc_2[r, c])
        if nr < 0:
            return None
        r, c = nr, nc


_west = [c for c in _flat_2["cells"] if _exit_of(c) == (5, 1)]
_east = [c for c in _flat_2["cells"] if _exit_of(c) == (5, 9)]
assert len(_west) + len(_east) == 27, (len(_west), len(_east))
assert len(_west) > 0 and len(_east) > 0, (len(_west), len(_east))
# Every cell reaches the exit on ITS OWN SIDE of the divide: the flat is
# symmetric about column 5, so a west-draining cell must not sit east of
# a east-draining cell in the same row.
for _r in (2, 3, 4):
    _wc = [c for (rr, c) in _west if rr == _r]
    _ec = [c for (rr, c) in _east if rr == _r]
    assert max(_wc) < min(_ec), (_r, sorted(_wc), sorted(_ec))
assert _stranded(RESOLVED_2, _FLAT_2_RAW) == 0
print(
    f"Test 2 -- multiple outlets: a 27-cell flat with a drop at each end splits {len(_west)}/"
    f"{len(_east)} between them, every cell reaching the exit on its own side of the divide "
    f"(no row has a west-draining cell east of an east-draining one). The outlet set is all four "
    f"cells touching a lower neighbour, not just the one a flood would have entered through."
)


# =====================================================================
# TEST 3 -- AN INLET-LESS BASIN FLOOR: nothing higher touches the flat,
#           so the away-from-higher gradient has nothing to say and the
#           outlet distance alone routes it. Stated, not assumed away.
# =====================================================================
#
# A filled basin whose floor sits at the SAME elevation as its rim after
# filling -- the fill raises the pit to its spill, so no cell of the
# resulting flat has a higher neighbour anywhere.

BASIN_3 = np.full((13, 13), 60.0)
BASIN_3[3:10, 3:10] = 42.0          # the pit, filled to the 60 m rim
BASIN_3[12, 6] = 55.0               # the border exit, below the rim
_BASIN_3_RAW = BASIN_3.copy()

_plain_3 = fill_depressions(BASIN_3, epsilon_meters=0.0)
assert float(_plain_3[6, 6]) == 60.0, float(_plain_3[6, 6])
_labels_3, _regions_3 = find_flat_regions(_plain_3)
_floor_3 = max(_regions_3, key=lambda r: len(r["cells"]))
assert _floor_3["inlets"] == [], _floor_3["inlets"][:5]
assert len(_floor_3["outlets"]) > 0

RESOLVED_3 = resolve_flats(_plain_3)
_d_out_3 = _bfs_hops(_floor_3["outlets"], set(_floor_3["cells"]), BASIN_3.shape)
for _cell in _floor_3["cells"]:
    _units = (RESOLVED_3[_cell] - _plain_3[_cell]) / INC
    assert abs(_units - W * _d_out_3[_cell]) < 1e-9, (_cell, _units, _d_out_3[_cell])
assert _stranded(RESOLVED_3, _BASIN_3_RAW) == 0
print(
    f"Test 3 -- an inlet-less flat ({len(_floor_3['cells'])} cells, {len(_floor_3['outlets'])} "
    f"outlets, nothing higher touching it anywhere): the away-from-higher term is identically zero "
    f"and the increment is exactly {W:.0f} x the outlet distance, cell for cell. That is the "
    f"defensible answer rather than a fallback -- with no higher ground adjacent there is no "
    f"away-from-higher information to be had, so the flat's geometry supplies exactly one fact, "
    f"where its water leaves, and the distance transform from the outlet is the most it supports."
)


# =====================================================================
# TEST 4 -- A SINGLE-CELL FLAT gets an increment of exactly zero, and a
#           flat with NO OUTLET AT ALL is left alone rather than tilted.
# =====================================================================

# 4a. A one-cell flat: the grid's own lowest border cell.
RAMP_4 = np.array(
    [
        [50.0, 49.0, 48.0, 47.0, 46.0],
        [51.0, 50.0, 49.0, 48.0, 47.0],
        [52.0, 51.0, 50.0, 49.0, 48.0],
        [53.0, 52.0, 51.0, 50.0, 49.0],
        [54.0, 53.0, 52.0, 51.0, 50.0],
    ]
)
_plain_4 = fill_depressions(RAMP_4, epsilon_meters=0.0)
_labels_4, _regions_4 = find_flat_regions(_plain_4)
assert len(_regions_4) == 1 and len(_regions_4[0]["cells"]) == 1, [
    len(r["cells"]) for r in _regions_4
]
assert _regions_4[0]["cells"] == [(0, 4)], _regions_4[0]["cells"]
RESOLVED_4 = resolve_flats(_plain_4)
assert RESOLVED_4.tobytes() == _plain_4.tobytes(), "a single-cell flat must move by exactly nothing"

# 4b. A nodata-walled island: a flat with no outlet of either kind.
#     resolve_flats() must leave it EXACTLY alone -- imposing a pattern on
#     ground whose water has nowhere to go would manufacture the very
#     artifact this branch removes, and these cells carry the -1 sentinel
#     today under the epsilon fill for the same reason (the flood never
#     reaches them, so the epsilon never applies to them either).
ISLAND_4 = np.full((7, 7), N)
ISLAND_4[0, 0] = 20.0                 # a real border exit, elsewhere
ISLAND_4[2:5, 2:5] = 30.0             # the walled-off island, dead level
_plain_4b = fill_depressions(ISLAND_4, epsilon_meters=0.0)
_eps_4b = fill_depressions(ISLAND_4)
RESOLVED_4B = resolve_flats(_plain_4b)
_labels_4b, _regions_4b = find_flat_regions(_plain_4b)
_island = [r for r in _regions_4b if len(r["cells"]) == 9]
assert len(_island) == 1 and _island[0]["outlets"] == [], _island[0]["outlets"]
assert np.array_equal(
    np.nan_to_num(RESOLVED_4B, nan=-1.0), np.nan_to_num(_plain_4b, nan=-1.0)
), "an outlet-less flat must be left exactly as the fill left it"
_eps_island_sentinels = int(
    (compute_flow_direction(_eps_4b, RESOLUTION)[0][2:5, 2:5] < 0).sum()
)
_res_island_sentinels = int(
    (compute_flow_direction(RESOLVED_4B, RESOLUTION)[0][2:5, 2:5] < 0).sum()
)
assert _eps_island_sentinels == _res_island_sentinels == 9, (
    _eps_island_sentinels,
    _res_island_sentinels,
)
print(
    f"Test 4 -- the two degenerate regions, decided rather than assumed away: a single-cell flat "
    f"({_regions_4[0]['cells'][0]}) moves by exactly nothing (bitwise), and a "
    f"{len(_island[0]['cells'])}-cell nodata-walled island with no outlet of either kind is left "
    f"exactly as the fill left it. Its cells keep the -1 sentinel -- all "
    f"{_res_island_sentinels} of them, the SAME {_eps_island_sentinels} the epsilon fill leaves "
    f"stranded there today, so this is not a regression but the unchanged and correct answer for "
    f"ground whose water has nowhere to go."
)


# =====================================================================
# TEST 4c -- AN OUTLET IS PINNED AT ZERO, and a rim flat's water LEAVES
#            the grid rather than running along the rim.
# =====================================================================
#
# FOUND BY MEASUREMENT, not by design. An earlier revision of this pass
# gave outlet cells d_outlet = 0 but still let them carry the
# away-from-higher term. On a level strip running off the DEM's north
# edge with higher ground to its east, that tilted the strip westward --
# so its BORDER cells acquired an in-grid flow direction and water that
# should have left the window ran along the rim instead, piling up 3348
# cells of accumulation at one corner and merging two drainage networks
# the DEM's arbitrary crop had no business joining. Pinning outlets to
# exactly zero fixes it: the rim keeps the -1 sentinel that means "water
# leaves here", exactly as it does under the epsilon fill.

RIM_4C = np.zeros((10, 24))
_rr4c, _cc4c = np.mgrid[0:10, 0:24]
RIM_4C = 340.0 + 0.20 * _rr4c + 0.05 * _cc4c     # falls to the north-west
RIM_4C[0:4, 6:20] = float(RIM_4C[3, 6])          # the level strip on the rim

_plain_4c = fill_depressions(RIM_4C, epsilon_meters=0.0)
_res_4c = resolve_flats(_plain_4c)
_eps_4c = fill_depressions(RIM_4C)
_labels_4c, _regions_4c = find_flat_regions(_plain_4c)
_rim = max(_regions_4c, key=lambda r: len(r["cells"]))
_rim_border = [c for c in _rim["outlets"] if c[0] == 0]
assert len(_rim_border) > 1, _rim_border

# Every outlet gets exactly zero, so the whole rim row stays level and no
# cell of it acquires an in-grid direction the epsilon would not give it.
for _cell in _rim["outlets"]:
    assert float(_res_4c[_cell] - _plain_4c[_cell]) == 0.0, (_cell, _res_4c[_cell])

_ftr_4c, _, _acc_4c = _route(_res_4c)
_ftr_e4c, _, _acc_e4c = _route(_eps_4c)
_rim_routed_res = sum(1 for _cell in _rim_border if int(_ftr_4c[_cell]) >= 0)
_rim_routed_eps = sum(1 for _cell in _rim_border if int(_ftr_e4c[_cell]) >= 0)
assert _rim_routed_res == _rim_routed_eps, (_rim_routed_res, _rim_routed_eps)
assert int(_acc_4c.max()) == int(_acc_e4c.max()), (int(_acc_4c.max()), int(_acc_e4c.max()))
# and the flat's INTERIOR is still resolved -- pinning the rim does not
# strand the ground above it.
assert _stranded(_res_4c, RIM_4C) == 0
print(
    f"Test 4c -- an outlet is pinned at exactly zero: on a {len(_rim['cells'])}-cell level strip "
    f"running off the DEM's north edge with higher ground east of it, all {len(_rim['outlets'])} "
    f"outlet cells move by 0.0000 m, the {len(_rim_border)} rim cells keep exactly the routing the "
    f"epsilon gives them ({_rim_routed_res} routed either way), and peak accumulation is identical "
    f"({int(_acc_4c.max())}). Water leaves the window instead of running along an arbitrary crop "
    f"edge -- while the strip's interior is still fully resolved, 0 cells stranded."
)


# =====================================================================
# TEST 5 -- THE GUARANTEE, re-asserted: no cell on filled ground is left
#           without a flow direction. Checked against a deliberately
#           weaker weighting so the assertion measures something.
# =====================================================================
#
# WHY THIS TEST EXISTS AND WHAT IT COST. Garbrecht & Martz as PUBLISHED
# weight the away-from-higher gradient MORE heavily than the toward-lower
# one and then repair, in correction passes, the cells that combination
# leaves without a descent (Barnes, Lehman & Mulla 2014 document exactly
# that repair burden). This module inverts the weights so the descent is
# structural instead of repaired. That is a real divergence from the
# literature and it is justified HERE, by measurement, rather than by
# assertion in a docstring.


def _resolve_gm97_published(filled, away_weight=2.0, toward_weight=1.0):
    """GM97's published combination: the away-from-higher gradient stepping
    one unit per hop and weighted 2x, the toward-lower gradient added at
    1x. Present ONLY as the measured contrast for the weighting this
    module actually ships -- nothing in the pipeline calls it."""
    out = filled.copy()
    _lab, regions = find_flat_regions(filled)
    for region in regions:
        if not region["outlets"]:
            continue
        member = set(region["cells"])
        d_out = _bfs_hops(region["outlets"], member, filled.shape)
        d_in = _bfs_hops(region["inlets"], member, filled.shape) if region["inlets"] else {}
        span = max(d_in.values()) if d_in else 0
        for cell in region["cells"]:
            hops = d_out.get(cell)
            if hops is None:
                continue
            g_away = (span - d_in.get(cell, span)) if d_in else 0.0
            out[cell] = filled[cell] + INC * (away_weight * g_away + toward_weight * hops)
    return out


_rng = np.random.default_rng(20260903)
_cases = {}
_cases["one inlet, one outlet"] = FLAT_1
_cases["multiple outlets"] = _FLAT_2_RAW
_cases["inlet-less basin floor"] = _BASIN_3_RAW
_cases["dead flat 20x20"] = np.full((20, 20), 100.0)
for _i in range(40):
    # Quantised terrain: coarse elevation steps make genuine plateaus,
    # many with several spills and an irregular inlet edge -- the shape
    # real flats have and the shape a single-source pattern gets wrong.
    _b = _rng.normal(0.0, 3.0, size=(18, 18))
    for _ in range(3):
        _p = np.pad(_b, 1, mode="edge")
        _b = sum(_p[_a : _a + 18, _d : _d + 18] / 9.0 for _a in range(3) for _d in range(3))
    _cases[f"quantised terrain #{_i}"] = np.round(_b * 2.0) / 2.0 + 340.0

_stranded_ours = _stranded_published = _stranded_epsilon = 0
_flat_cells_total = 0
for _name, _raw in _cases.items():
    _plain = fill_depressions(_raw, epsilon_meters=0.0)
    _flat_cells_total += sum(len(r["cells"]) for r in find_flat_regions(_plain)[1])
    _stranded_ours += _stranded(resolve_flats(_plain), _raw)
    _stranded_published += _stranded(_resolve_gm97_published(_plain), _raw)
    _stranded_epsilon += _stranded(fill_depressions(_raw), _raw)

assert _stranded_ours == 0, _stranded_ours
assert _stranded_epsilon == 0, _stranded_epsilon
assert _stranded_published > 0, (
    "precondition: GM97's published weighting must actually strand cells on this corpus, or this "
    "test is not measuring the thing it claims to measure"
)
print(
    f"Test 5 -- the epsilon branch's guarantee, re-asserted under the new pass: across "
    f"{len(_cases)} DEMs carrying {_flat_cells_total} flat cells in total, resolve_flats() leaves "
    f"{_stranded_ours} non-border cells without a flow direction, exactly matching the epsilon "
    f"fill's own {_stranded_epsilon}. GM97's PUBLISHED weighting (away-from-higher at 2x, "
    f"toward-lower at 1x) strands {_stranded_published} on the same corpus -- which is why this "
    f"module inverts the weights and takes the descent by construction. The divergence from the "
    f"literature is measured here, not asserted in a docstring."
)


# =====================================================================
# TEST 6 -- the RAW DEM is untouched by the whole fill+resolve path, and
#           terrain that is neither depression nor flat is BITWISE
#           unchanged by it.
# =====================================================================

_RAW_SNAPSHOT = FLAT_1.copy()
_ = fill_and_resolve(FLAT_1)
assert np.array_equal(
    np.nan_to_num(FLAT_1, nan=-1.0), np.nan_to_num(_RAW_SNAPSHOT, nan=-1.0)
), "fill_and_resolve() mutated its input array"
assert FLAT_1.dtype == _RAW_SNAPSHOT.dtype

# The float32 dtype dem_data.get_dem_for_boundary() actually delivers, at
# a real reference-property elevation.
_f32 = (FLAT_1.astype(np.float32) + np.float32(296.0)).astype(np.float32)
_f32_snapshot = _f32.copy()
_f32_resolved = fill_and_resolve(_f32)
assert _f32_resolved is not _f32
assert _f32.tobytes() == _f32_snapshot.tobytes(), "fill_and_resolve() mutated a float32 input"
assert _f32_resolved.dtype == np.float32, _f32_resolved.dtype
assert _stranded(_f32_resolved, _f32) == 0, "the float32 path must route as well as float64 does"

# A 60x60 4% bench with one incised drainage: no pit, no flat, so nothing
# for either the fill or the resolution pass to raise.
_rr, _cc = np.mgrid[0:60, 0:60]
BENCH_6 = 340.0 + (59 - _rr) * 0.20
BENCH_6 = BENCH_6 - np.exp(-(((_cc - 30) / 3.0) ** 2)) * 2.0
BENCH_6 = BENCH_6.astype(np.float32)
_bench_snapshot = BENCH_6.copy()
BENCH_6_RESOLVED = fill_and_resolve(BENCH_6)
assert BENCH_6_RESOLVED.tobytes() == _bench_snapshot.tobytes(), (
    "on terrain with no depression and no flat, fill_and_resolve() must return the raw array "
    "BITWISE -- every consumer that reads a filled elevation must read exactly what it read before"
)
assert _stranded(BENCH_6_RESOLVED, BENCH_6) == 0
print(
    "Test 6 -- the raw DEM is untouched: fill_and_resolve() returns a raised COPY, the input is "
    "bitwise identical afterwards (checked on float64 and on the float32 dtype dem_data.py "
    "actually delivers), and on a 60x60 4% bench with one incised drainage -- no pit, no flat -- "
    "the whole fill+resolve path returns the raw array BITWISE. The increment only ever rides on "
    "ground that is genuinely level."
)


# =====================================================================
# TEST 7 -- THE DEAD-FLAT ARTIFACT MEASUREMENT (Finding 2, reproduced).
#           The expected resolved pattern is stated HERE, in the fixture,
#           and derived from the flat's geometry -- not read off the
#           algorithm's own output.
# =====================================================================
#
# A 20x20 dead-flat DEM. Its geometry says exactly one thing: the ground
# is level, nothing feeds it, and the only place its water can leave is
# the grid rim -- which is the depression fill's own stated assumption
# about the fetched DEM's outer edge, and the reason the rim cells are
# this flat's outlet set.
#
# SO THE RESOLVED PATTERN IS PREDICTABLE IN CLOSED FORM, and this is the
# prediction, written before looking: with no inlets the away-from-higher
# term vanishes, so the increment must be exactly
#
#     W * INC * (Chebyshev distance from the grid rim)
#
# a square cone rising from 0 at the rim to its apex in the middle.

DEAD_FLAT = np.full((20, 20), 100.0)
_rr7, _cc7 = np.mgrid[0:20, 0:20]
CHEBYSHEV_FROM_RIM = np.minimum(
    np.minimum(_rr7, 19 - _rr7), np.minimum(_cc7, 19 - _cc7)
)
PREDICTED_RESOLVED = DEAD_FLAT + W * INC * CHEBYSHEV_FROM_RIM

_eps_7 = fill_depressions(DEAD_FLAT)
_res_7 = fill_and_resolve(DEAD_FLAT)
assert np.allclose(_res_7, PREDICTED_RESOLVED, atol=1e-12), np.abs(
    _res_7 - PREDICTED_RESOLVED
).max()

_ftr_e7, _ftc_e7, _acc_e7 = _route(_eps_7)
_ftr_r7, _ftc_r7, _acc_r7 = _route(_res_7)


def _twi_spread(accumulation):
    """The TWI spread this flat manufactures, on compute_topographic_
    wetness_index()'s own formula with slope at the flat floor: TWI =
    ln(a / tan beta), so a uniform tan beta makes the spread exactly the
    ln-range of the specific catchment area."""
    a = accumulation.astype(np.float64)
    return float(np.log(a.max()) - np.log(a.min()))


_eps_range = (int(_acc_e7.min()), int(_acc_e7.max()))
_res_range = (int(_acc_r7.min()), int(_acc_r7.max()))

# THE FINDING, pinned as an assertion rather than left as prose: on THIS
# fixture the epsilon's pattern is NOT queue-order artifact. Its rise
# field is the SAME Chebyshev cone, at half the scale -- because a flood
# seeded from the whole rim advances in rim-distance order, so flood order
# and outlet distance COINCIDE here. The routing is therefore identical.
assert np.allclose(_eps_7 - DEAD_FLAT, FILL_EPSILON_METERS * CHEBYSHEV_FROM_RIM, atol=1e-12)
assert np.array_equal(_ftr_e7, _ftr_r7) and np.array_equal(_ftc_e7, _ftc_r7)
assert _eps_range == _res_range == (1, 10), (_eps_range, _res_range)
assert abs(_twi_spread(_acc_e7) - _twi_spread(_acc_r7)) < 1e-12

# What the flat DOES manufacture, and it is not nothing: a level plate
# that can only drain at its rim really does concentrate flow, and D8
# gives every cell exactly one exit. That residual is geometry, not queue
# order, and it survives ANY flat-resolution algorithm.
assert _twi_spread(_acc_r7) > 2.0, _twi_spread(_acc_r7)
print(
    f"Test 7 -- the Finding 2 fixture, 20x20 dead flat. Resolved increment matches the "
    f"closed-form prediction exactly: {W:.0f} x {INC} m x Chebyshev-distance-from-rim, a square "
    f"cone with no away-from-higher term because the flat has no inlets. Accumulation "
    f"{_res_range[0]}-{_res_range[1]}, TWI spread {_twi_spread(_acc_r7):.2f} ln units. "
    f"THE EPSILON GIVES THE IDENTICAL ROUTING -- same range, same directions, TWI spread equal to "
    f"1e-12 -- because its rise field is the SAME cone at half scale ({FILL_EPSILON_METERS} m/hop "
    f"vs {W * INC} m/hop): a flood seeded from the whole rim advances in rim-distance order, so "
    f"here flood order and outlet distance COINCIDE. On this fixture the 1-10 spread is NOT queue "
    f"artifact; it is what D8 single-flow-direction does to a level plate that can only drain at "
    f"its rim, and it survives any flat-resolution algorithm."
)


# =====================================================================
# TEST 8 -- ACCUMULATED RISE on a large synthetic flat, and the
#           depression-depth guard the excavated criterion depends on.
# =====================================================================

FLAT_SIDE = 40
BASIN_8 = np.full((FLAT_SIDE + 2, FLAT_SIDE + 2), 100.0)
BASIN_8[1:-1, 1:-1] = 90.0
BASIN_8[0, 1] = 80.0                 # the one border spill
_BASIN_8_RAW = BASIN_8.copy()

_plain_8 = fill_depressions(BASIN_8, epsilon_meters=0.0)
_res_8 = resolve_flats(_plain_8)
_eps_8 = fill_depressions(BASIN_8)

_rise_res = float(np.nanmax(_res_8 - _plain_8))
_labels_8, _regions_8 = find_flat_regions(_plain_8)
_big_8 = max(_regions_8, key=lambda r: len(r["cells"]))
_d_out_8 = _bfs_hops(_big_8["outlets"], set(_big_8["cells"]), BASIN_8.shape)
_max_hops = max(_d_out_8.values())

# The bound the constant's upper argument claims: W * INC per hop of
# outlet distance, plus at most one sub-increment of away-from-higher.
assert _rise_res <= INC * (W * _max_hops + 1.0) + 1e-12, (_rise_res, _max_hops)
assert _rise_res < DEPRESSION_NOISE_FLOOR_METERS, (_rise_res, DEPRESSION_NOISE_FLOOR_METERS)
assert _max_hops < _HOPS_TO_NOISE_FLOOR, (_max_hops, _HOPS_TO_NOISE_FLOOR)

# HOW MANY CELLS CROSS THE 0.1 m NOISE FLOOR ON THE INCREMENT ALONE.
# Expected zero; a nonzero count is a finding to report, not a number to
# retune the floor around.
_crossing = int((np.nan_to_num(_res_8 - _plain_8) >= DEPRESSION_NOISE_FLOOR_METERS).sum())
assert _crossing == 0, _crossing

# HOW MANY CELLS ARE LIFTED FROM ZERO DEPRESSION DEPTH INTO NONZERO BY THE
# INCREMENT ALONE. compute_depression_depth() reads depths below the noise
# floor as 0.0, so this is the count that actually reaches the excavated
# wetness criterion. Expected zero.
_depth_plain = compute_depression_depth(_BASIN_8_RAW, _plain_8)
_depth_res = compute_depression_depth(_BASIN_8_RAW, _res_8)
_depth_eps = compute_depression_depth(_BASIN_8_RAW, _eps_8)
_lifted_res = int(((_depth_plain == 0.0) & (_depth_res > 0.0)).sum())
_lifted_eps = int(((_depth_plain == 0.0) & (_depth_eps > 0.0)).sum())
assert _lifted_res == 0, _lifted_res
assert _lifted_eps == 0, _lifted_eps

# And on a flat that is NOT a depression -- a natural level bench, raised
# by nothing -- the same guard, because that is where a lift would come
# from if it came from anywhere.
BENCH_8 = np.full((FLAT_SIDE + 2, FLAT_SIDE + 2), 90.0)
BENCH_8[0, :] = BENCH_8[-1, :] = BENCH_8[:, 0] = BENCH_8[:, -1] = 89.0
_plain_bench = fill_depressions(BENCH_8, epsilon_meters=0.0)
_res_bench = resolve_flats(_plain_bench)
_lifted_bench = int(
    (
        (compute_depression_depth(BENCH_8, _plain_bench) == 0.0)
        & (compute_depression_depth(BENCH_8, _res_bench) > 0.0)
    ).sum()
)
assert _lifted_bench == 0, _lifted_bench
assert _stranded(_res_8, _BASIN_8_RAW) == 0
print(
    f"Test 8 -- a {FLAT_SIDE}x{FLAT_SIDE} dead-level basin ({FLAT_SIDE * FLAT_SIDE} cells) "
    f"spilling at one border cell: the outlet distance peaks at {_max_hops} D8 hops -- the flat's "
    f"DIAMETER, not its cell count -- so the accumulated rise peaks at {_rise_res:.4f} m, "
    f"{DEPRESSION_NOISE_FLOOR_METERS / _rise_res:.1f}x under the "
    f"{DEPRESSION_NOISE_FLOOR_METERS} m depression noise floor. Cells crossing that floor on the "
    f"increment alone: {_crossing}. Cells lifted from 0.0 depression depth into nonzero by the "
    f"increment alone: {_lifted_res} on the basin and {_lifted_bench} on a natural level bench "
    f"that the fill raises by nothing at all (the epsilon's own count on the same basin is "
    f"{_lifted_eps}). The excavated wetness criterion's input is unmoved in the only sense it "
    f"scores."
)


print("\ntest_flat_resolution.py: all checks passed.")
