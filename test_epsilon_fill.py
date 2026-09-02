"""
test_epsilon_fill.py

Offline (no-network) checks for valley_delineation.fill_depressions()'s
EPSILON variant -- the branch that gave filled flats a defined flow
direction.

THE DEFECT THIS PINS THE FIX FOR. fill_depressions() used to be the plain
priority-flood: it raised a pit to EXACTLY its spill elevation, so the
filled cell TIED with the neighbour it should drain to.
compute_flow_direction() requires a STRICTLY positive slope, so every one
of those tied cells got the -1 "no downhill neighbour" sentinel and was
unroutable -- and every consumer that walks the flow field stopped there.
The same defect was reported five different ways across this project's
history: truncated backwaters in the level-pool era, wall walks ending
`flat_tie_sentinel`, `unreachable_stem_end` cross-section stations,
raw-vs-filled contour weave in the keyline exploration, and embankment
seeds terminating `flow_end` with 0-2 stations measured.

Everything here runs against small, hand-built arrays -- these are checks
about the FILL ALGORITHM, deliberately independent of any real DEM fetch.
test_valley_delineation.py covers the surrounding pipeline;
test_valley_level_pool.py's TEST 4b is the tripwire that was pinned to
fail when this branch landed (it did; see its own corrected note).
"""

import numpy as np

from valley_delineation import (
    FILL_EPSILON_METERS,
    compute_flow_accumulation,
    compute_flow_direction,
    fill_depressions,
)
from water_survey_areas import DEPRESSION_NOISE_FLOOR_METERS, compute_depression_depth

RESOLUTION = (5.0, 5.0)
EPS = FILL_EPSILON_METERS


def _drains_strictly_downhill(filled, flow_to_row, flow_to_col):
    """Every routed cell, with the drop to the cell it drains into. A cell
    with the -1 sentinel is not routed and is excluded."""
    drops = []
    rows, cols = filled.shape
    for r in range(rows):
        for c in range(cols):
            tr, tc = int(flow_to_row[r, c]), int(flow_to_col[r, c])
            if tr >= 0:
                drops.append(((r, c), (tr, tc), float(filled[r, c]) - float(filled[tr, tc])))
    return drops


def _border_mask(shape):
    mask = np.zeros(shape, dtype=bool)
    mask[0, :] = mask[-1, :] = mask[:, 0] = mask[:, -1] = True
    return mask


# =====================================================================
# 0. THE CONSTANT'S TWO BOUNDS, CHECKED RATHER THAN ASSUMED.
#
# FILL_EPSILON_METERS claims to sit between two limits: far below the
# DEM's own vertical accuracy (so it cannot manufacture terrain signal)
# and far above float32's resolution at real elevations (so it cannot
# vanish to rounding). Both are checked here against the ACTUAL dtype the
# DEM arrives in -- dem_data.get_dem_for_boundary() returns
# `src.read(1).astype("float32")`, so float32 is the real precision the
# increment is added at, not an assumption.
# =====================================================================
assert EPS == 0.001, EPS
assert EPS < DEPRESSION_NOISE_FLOOR_METERS / 50.0, (
    f"the epsilon must sit far below the depression noise floor "
    f"({DEPRESSION_NOISE_FLOOR_METERS} m) or the fill starts manufacturing wetness signal"
)

# float32's ulp at the reference property's own elevation band (~346 m) and
# at a deliberately extreme one. np.spacing() gives the real ulp for the
# real dtype -- no hand-derived exponent arithmetic to get wrong.
_ULP_346 = float(np.spacing(np.float32(346.0)))
_ULP_4000 = float(np.spacing(np.float32(4000.0)))
assert _ULP_346 < EPS / 20.0, (_ULP_346, EPS)
assert _ULP_4000 < EPS / 2.0, (_ULP_4000, EPS)

# The claim that actually matters: adding the epsilon to a float32
# elevation lands on a STRICTLY GREATER float32, at every elevation this
# tool can be pointed at. Death Valley (-86 m) to Denali (6190 m).
for _z in (-86.0, 0.0, 100.0, 346.0, 1000.0, 4000.0, 6190.0):
    _base = np.float32(_z)
    _raised = np.float32(float(_base) + EPS)
    assert _raised > _base, f"epsilon vanished to float32 rounding at {_z} m"
print(
    f"Constant bounds: FILL_EPSILON_METERS = {EPS} m sits {DEPRESSION_NOISE_FLOOR_METERS / EPS:.0f}x below the "
    f"depression noise floor and {EPS / _ULP_346:.0f}x above float32's ulp at the reference property's ~346 m "
    f"({_ULP_346:.2e} m). float32(z) + epsilon > float32(z) holds from -86 m to 6190 m."
)


# =====================================================================
# 1. A HAND-BUILT PIT: EVERY FILLED CELL DRAINS STRICTLY DOWNHILL, EVERY
#    PIT CELL ROUTES, AND THE ACCUMULATED RISE IS HAND ARITHMETIC.
# =====================================================================

# 1a. The classic bowl: a 3x3 pit at 10 m inside a 100 m rim, with ONE
# low border cell at (4, 3) = 20 m as the spill.
PIT = np.array(
    [
        [100.0, 100.0, 100.0, 100.0, 100.0],
        [100.0, 10.0, 10.0, 10.0, 100.0],
        [100.0, 10.0, 10.0, 10.0, 100.0],
        [100.0, 10.0, 10.0, 10.0, 100.0],
        [100.0, 100.0, 100.0, 20.0, 100.0],
    ],
    dtype=np.float64,
)
PIT_RAW_SNAPSHOT = PIT.copy()

pit_filled = fill_depressions(PIT)
pit_ftr, pit_ftc = compute_flow_direction(pit_filled, RESOLUTION)

# The pit is raised to its spill elevation PLUS the epsilon increments --
# never to exactly the spill, which is what used to tie it.
_pit_cells = [(r, c) for r in (1, 2, 3) for c in (1, 2, 3)]
for _cell in _pit_cells:
    assert pit_filled[_cell] > 20.0, (_cell, pit_filled[_cell])
    assert pit_filled[_cell] < 20.0 + 10 * EPS, (_cell, pit_filled[_cell])

# EVERY pit cell has a real flow direction. Under the plain fill every one
# of these was -1: they all tied at exactly 20.0 with the neighbour they
# drained to. This is the assertion the whole branch exists for.
for _cell in _pit_cells:
    assert int(pit_ftr[_cell]) >= 0, f"pit cell {_cell} still has the -1 sentinel"
    assert int(pit_ftc[_cell]) >= 0, _cell

# ...and every routed cell in the grid drains STRICTLY downhill, by at
# least one epsilon on the filled flat.
_pit_drops = _drains_strictly_downhill(pit_filled, pit_ftr, pit_ftc)
assert all(drop > 0.0 for _cell, _target, drop in _pit_drops), [
    d for d in _pit_drops if d[2] <= 0.0
]
_min_flat_drop = min(
    drop for cell, _t, drop in _pit_drops if cell in _pit_cells
)
assert abs(_min_flat_drop - EPS) < 1e-9, _min_flat_drop

# The plain fill, for contrast: the SAME call at epsilon 0.0 reproduces it
# exactly, and it is the tie-and-sentinel behaviour this replaced.
plain_pit_filled = fill_depressions(PIT, epsilon_meters=0.0)
plain_pit_ftr, _plain_pit_ftc = compute_flow_direction(plain_pit_filled, RESOLUTION)
assert np.all(plain_pit_filled[1:4, 1:4] == 20.0), plain_pit_filled
_plain_sentinels = sum(1 for _cell in _pit_cells if int(plain_pit_ftr[_cell]) < 0)
assert _plain_sentinels == len(_pit_cells), _plain_sentinels
print(
    f"Test 1a -- a 3x3 pit inside a 100 m rim spilling at 20.0 m: the PLAIN fill raises all "
    f"{len(_pit_cells)} pit cells to exactly 20.0 and every one of them takes the -1 sentinel "
    f"({_plain_sentinels}/{len(_pit_cells)}). The epsilon fill raises them to "
    f"{pit_filled[1:4, 1:4].min():.4f}-{pit_filled[1:4, 1:4].max():.4f} m, every one routes, and the "
    f"smallest drop anywhere on the flat is exactly one epsilon ({_min_flat_drop:.4f} m)."
)

# 1b. HAND ARITHMETIC, on a corridor whose flood order is forced. nodata
# walls the flat down to ONE row, so the flood can only advance one cell
# at a time from the single valid border cell at (1, 0): the k-th cell
# along the corridor is raised to exactly spill + k epsilons, with no
# branching to make the order ambiguous.
CORRIDOR_LENGTH = 9
# One nodata column past the flat's far end, so the corridor's ONLY valid
# border cell -- and therefore the flood's only seed -- is the spill at
# (1, 0). A valid cell at the far edge would be seeded too and the flood
# would start from both ends at once.
corridor = np.full((3, CORRIDOR_LENGTH + 2), np.nan, dtype=np.float64)
corridor[1, 0] = 5.0  # the spill: the corridor's only valid border cell
corridor[1, 1 : CORRIDOR_LENGTH + 1] = 1.0  # a dead-flat pit floor
corridor_filled = fill_depressions(corridor)

_expected = [5.0] + [5.0 + k * EPS for k in range(1, CORRIDOR_LENGTH + 1)]
_measured = [float(corridor_filled[1, k]) for k in range(CORRIDOR_LENGTH + 1)]
for k, (exp, got) in enumerate(zip(_expected, _measured)):
    assert abs(exp - got) < 1e-9, (k, exp, got)

_accumulated_rise = _measured[-1] - _measured[0]
assert abs(_accumulated_rise - CORRIDOR_LENGTH * EPS) < 1e-9, _accumulated_rise
assert np.isnan(corridor_filled[0]).all() and np.isnan(corridor_filled[2]).all(), (
    "nodata is a barrier, not something the fill writes into"
)
print(
    f"Test 1b -- hand arithmetic on a nodata-walled {CORRIDOR_LENGTH}-cell flat spilling at 5.0 m: cell k "
    f"fills to exactly 5.0 + k x {EPS} m ({_measured[0]:.4f} -> {_measured[-1]:.4f}), so the accumulated rise "
    f"across the whole flat is {_accumulated_rise:.4f} m = {CORRIDOR_LENGTH} x epsilon, to within 1e-9."
)


# =====================================================================
# 2. A LONG SYNTHETIC FLAT: HOW MUCH RISE ACCUMULATES, AND AGAINST WHAT
#    TOLERANCE.
#
# THE THING THAT COULD GO WRONG. Epsilon accumulates along a flat's own
# DRAINAGE PATH, so a long enough flat could accumulate enough rise to
# distort the depression depth (filled - raw) the excavated wetness
# criterion reads -- water_survey_areas.compute_depression_depth(), whose
# noise floor is DEPRESSION_NOISE_FLOOR_METERS.
#
# THE TOLERANCE, and why it is this one: the accumulated rise across the
# longest filled flat must stay BELOW DEPRESSION_NOISE_FLOOR_METERS
# (0.1 m). Below that, every cell the epsilon alone lifted still reads
# 0.0 depth -- the fill's own increment cannot invent depression signal,
# only ride along on depth that the plain fill already reported.
#
# WHAT ACCUMULATION IS ACTUALLY PROPORTIONAL TO -- worth stating, because
# the intuition ("a big flat accumulates a lot") is wrong. The flood
# spreads outward from the spill in D8 steps, so a cell's increment count
# is its CHEBYSHEV DISTANCE from the spill point, not the flat's cell
# COUNT. A 40x40 flat is 1600 cells and accumulates at most ~40 epsilons,
# not 1600. To exceed the noise floor a flat would have to be 100 D8 hops
# = 500 m across at this DEM's 5 m resolution, in one dead-level piece.
# =====================================================================
FLAT_SIDE = 40
plateau = np.full((FLAT_SIDE + 2, FLAT_SIDE + 2), 100.0, dtype=np.float64)
plateau[1:-1, 1:-1] = 90.0  # a dead-level basin, 40x40
plateau[-1, FLAT_SIDE // 2] = 95.0  # one low border cell: the spill

flat_filled = fill_depressions(plateau)
flat_plain = fill_depressions(plateau, epsilon_meters=0.0)
flat_ftr, flat_ftc = compute_flow_direction(flat_filled, RESOLUTION)

_interior = np.zeros(plateau.shape, dtype=bool)
_interior[1:-1, 1:-1] = True
_rise = flat_filled[_interior] - flat_plain[_interior]
_max_rise = float(_rise.max())
_max_hops = int(round(_max_rise / EPS))

assert _max_rise < DEPRESSION_NOISE_FLOOR_METERS, (
    f"accumulated rise {_max_rise} m across a {FLAT_SIDE}x{FLAT_SIDE} flat reached the "
    f"{DEPRESSION_NOISE_FLOOR_METERS} m depression noise floor -- that is a FINDING, not a number to "
    "retune: the epsilon would be manufacturing wetness signal on flat ground"
)
# The rise tracks the flat's DIAMETER in D8 hops, not its cell count.
assert _max_hops <= 2 * FLAT_SIDE, (_max_hops, FLAT_SIDE)
assert _max_hops >= FLAT_SIDE // 2, (_max_hops, FLAT_SIDE)

# Every interior cell of the flat routes; none is left on the sentinel.
assert int((flat_ftr[_interior] < 0).sum()) == 0, int((flat_ftr[_interior] < 0).sum())

# DEPRESSION DEPTH, the excavated wetness input, epsilon vs plain, read
# through the real scorer's own entry point rather than a reimplementation.
_depth_eps = compute_depression_depth(plateau, flat_filled)
_depth_plain = compute_depression_depth(plateau, flat_plain)
_depth_delta = float(np.nanmax(np.abs(_depth_eps - _depth_plain)))
assert _depth_delta <= _max_rise + 1e-12, (_depth_delta, _max_rise)
# The FLOORED depths -- what the criterion actually scores -- are
# unchanged: the basin is 5 m deep, so both fills clear the floor
# everywhere and the epsilon rides on top of an already-real depth.
assert np.nanmax(_depth_plain[_interior]) >= 5.0, np.nanmax(_depth_plain[_interior])
_cells_the_epsilon_alone_lifted = int(
    ((_depth_plain == 0.0) & (_depth_eps > 0.0)).sum()
)
assert _cells_the_epsilon_alone_lifted == 0, (
    "the epsilon alone must never lift a cell over the noise floor into nonzero "
    f"depression depth; {_cells_the_epsilon_alone_lifted} cells did"
)
# THE ABSOLUTE WORST CASE, stated rather than left to be inferred: the
# accumulated rise can never exceed (grid diameter in D8 hops) x epsilon,
# because the flood reaches every cell in at most that many steps from the
# nearest seed. On the reference property's own DEM window (96 x 108 cells
# at 5 m) that ceiling is 108 x 0.001 = 0.108 m -- i.e. only a DEM whose
# entire 480 x 540 m window is one dead-level basin could reach the noise
# floor, and such a window has no terrain to survey in the first place.
# A real flat is a small fraction of the window; the 40x40 basin here is
# already an implausibly large one and lands 2.5x under.
_GRID_DIAMETER_CEILING_M = max(plateau.shape) * EPS
assert _max_rise <= _GRID_DIAMETER_CEILING_M, (_max_rise, _GRID_DIAMETER_CEILING_M)

print(
    f"Test 2 -- a {FLAT_SIDE}x{FLAT_SIDE} dead-level basin ({FLAT_SIDE * FLAT_SIDE} cells) spilling at one "
    f"border cell: the accumulated rise peaks at {_max_rise:.4f} m = {_max_hops} epsilons, i.e. the flat's "
    f"D8 DIAMETER, not its cell count. Tolerance: that must stay under the "
    f"{DEPRESSION_NOISE_FLOOR_METERS} m depression noise floor, and it does, by "
    f"{DEPRESSION_NOISE_FLOOR_METERS / _max_rise:.1f}x. Depression depth moves by at most {_depth_delta:.4f} m "
    f"and NO cell is lifted from 0.0 depth into nonzero by the epsilon alone."
)


# =====================================================================
# 3. THE RAW DEM IS UNTOUCHED -- BITWISE.
#
# The raw/filled division of labour is a hard architectural boundary in
# this repo (connectivity from the filled field, elevation truth from the
# raw -- see keypoint_detection.py's fix #1 and valley_level_pool.py's
# TEST 4a). The fill returning a raised COPY is what makes that boundary
# enforceable at all.
# =====================================================================
assert np.array_equal(PIT, PIT_RAW_SNAPSHOT), "fill_depressions() mutated its input array"
assert PIT.dtype == PIT_RAW_SNAPSHOT.dtype

_f32 = np.array(
    [
        [346.0, 346.0, 346.0, 346.0],
        [346.0, 340.0, 340.0, 346.0],
        [346.0, 340.0, 340.0, 346.0],
        [346.0, 346.0, 344.0, 346.0],
    ],
    dtype=np.float32,
)
_f32_snapshot = _f32.copy()
_f32_filled = fill_depressions(_f32)
assert _f32_filled is not _f32
assert _f32.tobytes() == _f32_snapshot.tobytes(), (
    "the raw float32 DEM must be BITWISE unchanged by the fill call"
)
assert _f32_filled.dtype == np.float32, _f32_filled.dtype
# ...and at float32, at a real reference-property elevation, the increment
# still lands strictly above: the pit routes rather than tying.
_f32_ftr, _ = compute_flow_direction(_f32_filled, RESOLUTION)
for _cell in ((1, 1), (1, 2), (2, 1), (2, 2)):
    assert int(_f32_ftr[_cell]) >= 0, f"float32 pit cell {_cell} took the sentinel"
    assert float(_f32_filled[_cell]) > 344.0, (_cell, float(_f32_filled[_cell]))
print(
    "Test 3 -- the raw DEM is untouched: fill_depressions() returns a raised COPY, the input array is "
    "bitwise identical afterwards (checked on float64 and on the float32 dtype dem_data.py actually "
    "delivers), and the float32 pit still routes at a real ~346 m elevation."
)


# =====================================================================
# 4. THE SENTINEL: STILL PRODUCED WHERE IT IS CORRECT, GONE FROM INTERIOR
#    FILLED FLATS.
#
# -1 is NOT deleted by this branch and downstream code that treats it as
# "no direction here" is still right. What changed is WHERE it occurs:
# the flood SEEDS the grid's border rather than raising it, so a border
# cell that is its own neighbourhood's minimum still has nowhere to go;
# and a valid cell nodata walls off from every border is never reached by
# the flood at all. Interior filled flats -- the old dominant source --
# no longer produce it.
# =====================================================================

# 4a. Grid edges. The bowl above: the spill cell (4, 3) is the grid's
# lowest cell and sits ON the border, so it is the outlet and keeps -1.
_pit_sentinel = (pit_ftr < 0)
assert bool(_pit_sentinel[4, 3]), "the grid-edge outlet must still report the -1 sentinel"
assert int((_pit_sentinel & ~_border_mask(PIT.shape)).sum()) == 0, (
    "no INTERIOR cell may keep the sentinel on a fully-connected grid"
)

# 4b. nodata. A valid cell walled off from every border by nodata is
# unreachable by the flood, so its local minimum keeps the sentinel --
# correctly: there is genuinely no path off the grid from there.
walled = np.array(
    [
        [50.0, 50.0, 50.0, 50.0, 50.0],
        [50.0, np.nan, np.nan, np.nan, 50.0],
        [50.0, np.nan, 30.0, np.nan, 50.0],
        [50.0, np.nan, np.nan, np.nan, 50.0],
        [50.0, 50.0, 50.0, 50.0, 40.0],
    ],
    dtype=np.float64,
)
walled_filled = fill_depressions(walled)
walled_ftr, _walled_ftc = compute_flow_direction(walled_filled, RESOLUTION)
assert float(walled_filled[2, 2]) == 30.0, (
    "an island the flood cannot reach is not raised -- it has no spill path to raise it to"
)
assert int(walled_ftr[2, 2]) == -1, "the nodata-walled island keeps the sentinel, correctly"
assert int(walled_ftr[4, 4]) == -1, "the grid's lowest border cell is the outlet"

# 4c. The interior flat. This is the case that used to fill the flow field
# with sentinels: a 6x6 dead-level marsh sitting on a hillside, entirely
# interior, spilling downslope.
marsh = np.zeros((12, 12), dtype=np.float64)
for _r in range(12):
    for _c in range(12):
        marsh[_r, _c] = 200.0 - _r * 1.0
marsh[3:9, 3:9] = float(marsh[8, 3])  # dead-level, at the flat's own downhill edge
marsh_filled = fill_depressions(marsh)
marsh_plain = fill_depressions(marsh, epsilon_meters=0.0)
marsh_ftr, marsh_ftc = compute_flow_direction(marsh_filled, RESOLUTION)
marsh_plain_ftr, marsh_plain_ftc = compute_flow_direction(marsh_plain, RESOLUTION)

_marsh_cells = [(r, c) for r in range(3, 9) for c in range(3, 9)]
_plain_flat_sentinels = sum(1 for _cell in _marsh_cells if int(marsh_plain_ftr[_cell]) < 0)
_eps_flat_sentinels = sum(1 for _cell in _marsh_cells if int(marsh_ftr[_cell]) < 0)
assert _plain_flat_sentinels > 0, "precondition: the plain fill must strand cells on this flat"
assert _eps_flat_sentinels == 0, (
    f"{_eps_flat_sentinels} interior flat cell(s) still carry the sentinel after the epsilon fill"
)

# Flow accumulation now actually reaches the outlet through the flat --
# the direct consequence for every consumer that walks the field.
marsh_acc = compute_flow_accumulation(marsh_filled, marsh_ftr, marsh_ftc)
marsh_plain_acc = compute_flow_accumulation(marsh_plain, marsh_plain_ftr, marsh_plain_ftc)
_through_flat_eps = int(marsh_acc[3:9, 3:9].max())
_through_flat_plain = int(marsh_plain_acc[3:9, 3:9].max())
assert _through_flat_eps > _through_flat_plain, (_through_flat_eps, _through_flat_plain)
print(
    f"Test 4 -- the sentinel, relocated not deleted: it still marks the grid-edge outlet and a "
    f"nodata-walled island, and NO interior cell keeps it on a connected grid. On a 6x6 interior "
    f"marsh flat the plain fill stranded {_plain_flat_sentinels}/{len(_marsh_cells)} cells; the epsilon fill "
    f"strands {_eps_flat_sentinels}, and peak flow accumulation routed through the flat rises "
    f"{_through_flat_plain} -> {_through_flat_eps} cells."
)


# =====================================================================
# 5. epsilon_meters=0.0 REPRODUCES THE PLAIN VARIANT EXACTLY.
#
# Not a curiosity: it is what makes the before/after of this branch
# MEASURABLE on any DEM, rather than asserted. Nothing in the pipeline
# passes it.
# =====================================================================
# The PIT fixture from section 1a: 9 cells the plain fill raises to ONE
# elevation, which is exactly the tie that stranded them.
_plain_pit = fill_depressions(PIT, epsilon_meters=0.0)
_raised = _plain_pit > PIT
assert int(_raised.sum()) == 9, int(_raised.sum())
assert len(set(np.round(_plain_pit[_raised], 9))) == 1, sorted(set(_plain_pit[_raised]))
# ...and under the epsilon fill the same 9 cells occupy 3 distinct levels
# -- one per D8 hop from the spill. NOTE the claim that actually matters is
# not "all distinct" (two siblings raised off the same predecessor are
# legitimately level with each other, and never drain into each other);
# it is that no cell ties with the neighbour it DRAINS TO, asserted at
# section 1a's _min_flat_drop.
_eps_levels = sorted(set(np.round(pit_filled[_raised], 9)))
assert len(_eps_levels) == 3, _eps_levels
assert all(
    abs((_eps_levels[i + 1] - _eps_levels[i]) - EPS) < 1e-9 for i in range(len(_eps_levels) - 1)
), _eps_levels
# The plain result is also reproducible EXACTLY, not approximately: an
# epsilon-0 run is bitwise the old algorithm.
assert np.array_equal(_plain_pit, plain_pit_filled)
print(
    f"Test 5 -- epsilon_meters=0.0 reproduces the plain priority-flood exactly: its {int(_raised.sum())} "
    f"raised cells all sit at ONE elevation ({float(_plain_pit[_raised][0])} m -- dead level, the tie); the "
    f"epsilon fill spreads those same {int(_raised.sum())} cells over {len(_eps_levels)} levels one epsilon "
    f"apart ({_eps_levels[0]:.4f}/{_eps_levels[1]:.4f}/{_eps_levels[2]:.4f} m), one per D8 hop from the spill."
)



# =====================================================================
# 6. NOTHING OUTSIDE THE DEPRESSIONS MOVES -- BITWISE.
#
# This is what bounds the blast radius of a pipeline-wide change. Every
# KSOP step reads this module, and several of them read elevations that
# came off the FILLED array rather than the raw one (delineate_valleys()
# builds branches_utm's z from filled[r, c]; water_suitability.py reads
# those z values as a valley gradient). Those crossings are pre-existing
# and documented -- see keypoint_detection.py's fix #1 -- and this branch
# does not widen them: on a DEM with nothing to fill, the epsilon fill
# returns the raw array BITWISE, so every one of those consumers reads
# exactly what it read before. The epsilon can only ride on ground the
# plain fill was already raising.
# =====================================================================
_rows = np.arange(60)[:, None].astype(np.float32)
_cols = np.arange(60)[None, :].astype(np.float32)
_bench = (300.0 + 0.20 * _rows + 0.05 * _cols).astype(np.float32)
_bench -= (9.0 * np.exp(-((_cols - 30) ** 2) / (2 * 3.0 ** 2))).astype(np.float32)
_bench_filled = fill_depressions(_bench)
assert _bench_filled.tobytes() == _bench.tobytes(), (
    "a 4% bench with one incised drainage has no pit and no flat, so the epsilon fill must return the "
    f"raw array bitwise; {int((_bench_filled != _bench).sum())} cell(s) differ"
)
_bench_ftr, _ = compute_flow_direction(_bench_filled, RESOLUTION)
_bench_border = _border_mask(_bench.shape)
assert int(((_bench_ftr < 0) & ~_bench_border).sum()) == 0
print(
    "Test 6 -- nothing outside the depressions moves: on a 60x60 4% bench with one incised drainage "
    "(no pit, no flat) the epsilon fill returns the raw array BITWISE, so every consumer that reads a "
    "filled elevation reads exactly what it read before. The epsilon only ever rides on ground the "
    "plain fill was already raising."
)

print("\ntest_epsilon_fill.py: all checks passed.")
