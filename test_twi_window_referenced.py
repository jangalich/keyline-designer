"""
test_twi_window_referenced.py

THE TEST FOR THE WINDOW-REFERENCED TWI CURVE: where the two breakpoints
come from, what population they are read off, and what makes that
population stable when the user redraws the boundary.

It is the third file in a three-branch arc and only the third question
is new. test_twi_boundary_independence.py owns the invariant that a
cell's score may not move when the boundary moves; test_water_survey_
areas.py owns the curve's per-cell arithmetic. THIS file owns the
derivation:

    1. THE CURVE, HAND-DERIVED -- a window with a distribution whose
       percentiles are exact integers, so the two breakpoints are
       hand-checkable, and cells scored below / at / between / at / above
       them against hand arithmetic.
    2. SNAP QUANTIZATION -- two windows differing by a small edit snap to
       the IDENTICAL reference rectangle and produce identical scores
       (delta exactly 0.0); one differing by more than a snap produces a
       different rectangle, and the test STATES the resulting delta
       rather than asserting it away. Both outcomes are the design.
    3. THE GATE NEVER ENTERS THE REFERENCE -- a fixture whose gated cells
       are much drier than the window it sits in. The derived
       breakpoints are the WINDOW's percentiles, nowhere near the gated
       cells' own, and twi_reference_window()'s signature cannot reach
       the boundary or the mask even by mistake.
    4. THE WETNESS BLEND IS UN-PINNED -- a cell with real depression AND
       real TWI scores above 0.5, so the max-pinned-at-exactly-0.500
       signature (TWI contributing nothing, depression carrying the
       criterion alone) is gone.

Run:  python test_twi_window_referenced.py   (no network)
"""

import inspect
import math

import numpy as np
from shapely.geometry import box

import dem_data
import water_survey_areas as wsa
from water_survey_areas import (
    SURVEY_TYPE_EXCAVATED,
    compute_topographic_wetness_index,
    compute_water_survey_areas,
    twi_reference_window,
    twi_score,
    twi_window_breakpoints,
)

RESOLUTION = 5.0
ORIGIN_X, ORIGIN_Y = 500000.0, 4500000.0
CRS = "EPSG:32617"


def _dem(array, origin_x=ORIGIN_X, origin_y=ORIGIN_Y):
    return {
        "array": array,
        "resolution_meters": (RESOLUTION, RESOLUTION),
        "origin_x": origin_x,
        "origin_y": origin_y,
        "crs": CRS,
    }


# =========================================================================
# 1. THE CURVE, HAND-DERIVED FROM A KNOWN DISTRIBUTION
# =========================================================================
# 101 values, 0..100, one per cell. numpy's linear-interpolation
# percentile on n values lands on index (n-1)*q, which for n=101 is
# exactly 25 at p25 and exactly 90 at p90 -- chosen so the two
# breakpoints are integers a reader can check without running anything.
KNOWN = np.arange(101, dtype=np.float64).reshape(101, 1)
ALL_CELLS = np.ones(KNOWN.shape, dtype=bool)

curve = twi_window_breakpoints(KNOWN, ALL_CELLS)
assert curve["floor"] == 25.0, f"the floor IS the window's p25 (got {curve['floor']})"
assert curve["full_credit"] == 90.0, f"and full credit its p90 (got {curve['full_credit']})"
assert curve["floor_percentile"] == wsa.TWI_WINDOW_FLOOR_PERCENTILE == 25.0
assert curve["full_credit_percentile"] == wsa.TWI_WINDOW_FULL_CREDIT_PERCENTILE == 90.0
assert curve["curve_fallback"] is None, "a real spread needs no fallback"
assert curve["reference_cell_count"] == curve["measured_cell_count"] == 101
# The reported distribution is the window's own, at every reported
# percentile -- this is what makes a run's scoring reproducible from its
# own output.
assert curve["percentiles"]["p0"] == 0.0 and curve["percentiles"]["p100"] == 100.0
assert curve["percentiles"]["p50"] == 50.0
assert set(curve["percentiles"]) == {f"p{q:g}" for q in wsa.TWI_REPORTED_WINDOW_PERCENTILES}

# CELLS SCORED ON IT, hand-computed: below the floor, AT the floor, a
# quarter and half way up the 65-wide ramp, AT full credit, above it.
hand = np.array([[0.0, 10.0, 25.0], [41.25, 57.5, 90.0], [100.0, 1e6, np.nan]])
scored = twi_score(hand, curve["floor"], curve["full_credit"])
assert scored[0, 0] == 0.0 and scored[0, 1] == 0.0, "the window's driest quarter earns nothing"
assert scored[0, 2] == 0.0, "AT the floor is still 0.0 -- the ramp starts here, it does not step"
assert math.isclose(scored[1, 0], 0.25), "(41.25 - 25) / 65 = 0.25"
assert math.isclose(scored[1, 1], 0.5), "(57.5 - 25) / 65 = 0.5"
assert scored[1, 2] == 1.0, "AT full credit scores 1.0"
assert scored[2, 0] == 1.0 and scored[2, 1] == 1.0, "above full credit saturates, never overshoots"
assert np.isnan(scored[2, 2]), "an unmeasured cell stays unmeasured"

# THE CURVE MOVES WITH THE WINDOW, which is the whole point of
# referencing it: the SAME raw value scores differently in a wetter
# window, because the landscape it is being compared against is wetter.
# That is not instability -- the window is a property of the raster, and
# section 2 is what keeps the raster's reference rectangle still.
wetter = twi_window_breakpoints(KNOWN + 40.0, ALL_CELLS)
assert wetter["floor"] == 65.0 and wetter["full_credit"] == 130.0
assert twi_score(np.array([57.5]), curve["floor"], curve["full_credit"])[0] == 0.5
assert twi_score(np.array([57.5]), wetter["floor"], wetter["full_credit"])[0] == 0.0, (
    "a value that is mid-slope in one landscape is the dry end of a wetter one -- the curve is a "
    "statement about the window, and it says so"
)

# AND IT NEVER HAS ZERO WIDTH. Two degeneracies, both reported:
tied_with_tail = np.array([1.0] * 95 + [5.0, 6.0, 7.0, 8.0, 9.0])
tied = twi_window_breakpoints(tied_with_tail, np.ones(100, dtype=bool))
assert tied["curve_fallback"] == "observed_range", (
    "when p25 and p90 tie but the window has a spread, the ramp runs over the observed min..max"
)
assert (tied["floor"], tied["full_credit"]) == (1.0, 9.0)
uniform = twi_window_breakpoints(np.full(50, 7.0), np.ones(50, dtype=bool))
assert uniform["curve_fallback"] == "centred_on_uniform_value", (
    "a window with no gradient at all falls back to the centred curve"
)
assert twi_score(np.array([7.0]), uniform["floor"], uniform["full_credit"])[0] == 0.5, (
    "and every cell in it reads the NEUTRAL 0.5 -- a window that cannot tell wet from dry must not "
    "accuse every cell of dryness"
)
empty = twi_window_breakpoints(np.full(10, np.nan), np.ones(10, dtype=bool))
assert empty["floor"] is None and empty["full_credit"] is None and empty["measured_cell_count"] == 0
assert np.all(np.isnan(twi_score(np.array([5.0, 9.0]), empty["floor"], empty["full_credit"]))), (
    "no measured TWI in the window means NO CURVE -- every cell scores NaN, which the "
    "criteria-completeness signal already knows how to read, rather than an invented number"
)
print(
    f"1. The curve, hand-derived: a 0..100 window yields floor {curve['floor']:.0f} (p25) and full "
    f"credit {curve['full_credit']:.0f} (p90); cells score 0.00/0.00/0.25/0.50/1.00/1.00 below, at, "
    "between, at and above them; a wetter window moves the curve with it; and the two degeneracy "
    "fallbacks plus the no-curve case are each reported rather than silently patched."
)


# =========================================================================
# 2. SNAP QUANTIZATION: THE SMALL EDIT PRODUCES THE IDENTICAL WINDOW
# =========================================================================
# ONE MASTER TERRAIN, THREE FETCH WINDOWS OF IT. A real re-fetch under a
# redrawn boundary returns a different raster window over the same
# ground, which is exactly the residual boundary-dependence the snap
# exists to quantize away -- so the fixture builds that situation
# directly: three sub-windows of one master array, differing only in how
# far east/south they extend.
MASTER_ROWS, MASTER_COLS = 200, 200
CHANNEL_COL = 40


def _master_array():
    """A PARABOLIC valley draining south, channel at CHANNEL_COL, easing
    into a gentler eastern flat past col 100. Parabolic rather than
    V-shaped deliberately: a constant-grade V gives every side cell the
    identical slope, which collapses the window's TWI distribution onto
    a handful of values and lands the curve on a degeneracy fallback. A
    parabola's grade rises with distance from the channel, so the window
    carries a genuine spread and this section exercises the NORMAL
    p25/p90 path -- which is what it is here to test."""
    array = np.zeros((MASTER_ROWS, MASTER_COLS), dtype=np.float64)
    for r in range(MASTER_ROWS):
        for c in range(MASTER_COLS):
            if c < 100:
                array[r, c] = 200.0 - 0.30 * r + 0.004 * (c - CHANNEL_COL) ** 2
            else:
                array[r, c] = (
                    200.0
                    - 0.30 * r
                    + 0.004 * (100 - CHANNEL_COL) ** 2
                    - 0.15 * (c - 100)
                    + 0.0008 * (c - 100) ** 2
                )
    return array


MASTER = _master_array()
# Accumulation as a supplied override (the established fixture pattern):
# convergence falling away from the channel, rising again across the
# eastern flat, so extending a window eastward genuinely adds WET ground
# to the reference population rather than more of the same.
MASTER_ACCUMULATION = np.ones((MASTER_ROWS, MASTER_COLS), dtype=np.float64)
for _r in range(MASTER_ROWS):
    for _c in range(MASTER_COLS):
        MASTER_ACCUMULATION[_r, _c] = (
            1.0 + 3.0 * max(0, 60 - abs(_c - CHANNEL_COL)) + (0.5 * (_c - 100) if _c >= 100 else 0.0)
        )
MASTER_ACCUMULATION[:, CHANNEL_COL] = 400.0

# The probe: an interior mid-slope cell inside every window below, so
# its slope neighborhood and its supplied accumulation are identical in
# all three and the ONLY thing that can move its score is the curve.
# MID-SLOPE ON PURPOSE: a channel cell saturates at 1.0 under every
# curve this fixture produces, so it could not show a moved curve even
# when the curve moved.
PROBE = (60, 78)


def _sub_window(cols, rows=140):
    """A fetch window over the master terrain: the same origin, the same
    cells, a different eastern/southern extent -- the shape a re-fetch
    under a slightly redrawn boundary takes."""
    dem = _dem(MASTER[:rows, :cols])
    accumulation = MASTER_ACCUMULATION[:rows, :cols]
    reference = twi_reference_window(dem)
    raw = compute_topographic_wetness_index(
        dem, accumulation, wsa.compute_slope_percent(dem["array"], dem["resolution_meters"])
    )
    breakpoints = twi_window_breakpoints(raw, reference["mask"])
    scored = twi_score(raw, breakpoints["floor"], breakpoints["full_credit"])
    return reference, breakpoints, raw, scored


# 120 cells = 600 m wide; 124 cells = 620 m. Both snap INWARD to
# 500000..500600 on a 100 m grid, so the reference rectangle is
# byte-identical and so is everything downstream of it.
_ref_a, _bp_a, _raw_a, _scored_a = _sub_window(120)
_ref_b, _bp_b, _raw_b, _scored_b = _sub_window(124)
assert _ref_a["snapped"] and _ref_b["snapped"], "both windows span whole snap cells"
assert _ref_a["bounds"] == _ref_b["bounds"] == (500000.0, 4499300.0, 500600.0, 4500000.0), (
    f"a 20 m edit must not move the snapped reference rectangle: {_ref_a['bounds']} vs "
    f"{_ref_b['bounds']}"
)
assert _ref_a["cell_count"] == _ref_b["cell_count"], "same rectangle, same cells"
assert (_bp_a["floor"], _bp_a["full_credit"]) == (_bp_b["floor"], _bp_b["full_credit"]), (
    f"identical reference window -> identical curve, got {_bp_a['floor']}/{_bp_a['full_credit']} "
    f"vs {_bp_b['floor']}/{_bp_b['full_credit']}"
)
assert _bp_a["curve_fallback"] is None, "this fixture exercises the normal p25/p90 path"
assert _raw_a[PROBE] == _raw_b[PROBE], "the probe's own terrain is identical in both windows"
_small_edit_delta = abs(float(_scored_a[PROBE]) - float(_scored_b[PROBE]))
assert _small_edit_delta == 0.0, (
    f"THE POINT OF THE SNAP: a boundary edit that does not cross a snap line must produce the "
    f"IDENTICAL score, got a delta of {_small_edit_delta}"
)

# ...AND AN EDIT THAT DOES CROSS ONE MOVES THE WINDOW, which the snap
# bounds rather than abolishes. 144 cells = 720 m snaps to
# 500000..500700 -- a different rectangle, a different population, a
# different curve. THE DELTA IS STATED, NOT ASSERTED AWAY.
_ref_c, _bp_c, _raw_c, _scored_c = _sub_window(144)
assert _ref_c["bounds"] == (500000.0, 4499300.0, 500700.0, 4500000.0), _ref_c["bounds"]
assert _ref_c["bounds"] != _ref_a["bounds"], "crossing a snap line DOES move the reference window"
assert (_bp_c["floor"], _bp_c["full_credit"]) != (_bp_a["floor"], _bp_a["full_credit"])
assert _raw_c[PROBE] == _raw_a[PROBE], "the probe's terrain is still identical -- only the curve moved"
_crossed_snap_delta = abs(float(_scored_a[PROBE]) - float(_scored_c[PROBE]))
assert _crossed_snap_delta > 0.0, (
    "a genuinely different reference population must produce a genuinely different score -- a zero "
    "here would mean the window is not being referenced at all"
)
assert _crossed_snap_delta < 0.20, (
    f"and it must stay SMALL: the window is several times the parcel, so one added strip is a small "
    f"share of the reference population rather than the top of it (got {_crossed_snap_delta:.4f}). "
    "This is the number the snap bounds; the retired percentile moved the same kind of cell 0.6356."
)
print(
    f"2. Snap quantization: a 20 m edit snaps to the IDENTICAL reference rectangle "
    f"{_ref_a['bounds']} and moves the probe's score by exactly {_small_edit_delta:.4f}; a 120 m "
    f"edit crosses a snap line to {_ref_c['bounds']} and moves it by {_crossed_snap_delta:.4f} "
    f"(curve {_bp_a['floor']:.3f}/{_bp_a['full_credit']:.3f} -> "
    f"{_bp_c['floor']:.3f}/{_bp_c['full_credit']:.3f}) -- stated, not asserted away."
)

# THE SAME QUANTIZATION AT THE FETCH GEOMETRY, on real lon/lat, using
# dem_data's own request-sizing arithmetic (no network: dem_window_bounds
# is everything get_dem_for_boundary() computes before it asks for
# pixels). A vertex moved a few metres reuses the snapped rectangle; a
# vertex moved past a snap line does not.
REFERENCE_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]
NUDGED_BOUNDARY = [(lon + 0.00002, lat) for lon, lat in REFERENCE_BOUNDARY]  # ~1.7 m east
STRETCHED_BOUNDARY = REFERENCE_BOUNDARY + [(-79.9784741, 40.6445667)]  # ~250 m further east


def _snapped_fetch_bounds(boundary):
    snap = wsa.TWI_REFERENCE_WINDOW_SNAP_METERS
    min_x, min_y, max_x, max_y = dem_data.dem_window_bounds(boundary)["bbox"]
    return (
        math.ceil(min_x / snap) * snap,
        math.ceil(min_y / snap) * snap,
        math.floor(max_x / snap) * snap,
        math.floor(max_y / snap) * snap,
    )


assert _snapped_fetch_bounds(REFERENCE_BOUNDARY) == _snapped_fetch_bounds(NUDGED_BOUNDARY), (
    "a ~1.7 m vertex correction -- the shape of a real boundary edit -- must reuse the identical "
    "snapped reference window, which is what makes its scores identical"
)
assert _snapped_fetch_bounds(REFERENCE_BOUNDARY) != _snapped_fetch_bounds(STRETCHED_BOUNDARY), (
    "and a ~250 m extension crosses a snap line, which the instrument reports rather than hides"
)
print(
    f"   ...and at the fetch geometry on the reference property: a 1.7 m edit reuses "
    f"{_snapped_fetch_bounds(REFERENCE_BOUNDARY)}; a 250 m extension moves to "
    f"{_snapped_fetch_bounds(STRETCHED_BOUNDARY)}."
)


# =========================================================================
# 3. THE GATED MASK NEVER ENTERS THE REFERENCE
# =========================================================================
# THE STRUCTURAL HALF FIRST, because it is the one that cannot rot: the
# reference builder takes the dem and a snap size. There is no boundary
# argument and no mask argument, so no future edit can quietly reference
# the parcel without changing this signature and failing here.
_reference_params = list(inspect.signature(twi_reference_window).parameters)
assert _reference_params == ["dem", "snap_meters"], (
    f"twi_reference_window() must not be able to see the boundary or the gate mask -- its signature "
    f"is the guarantee, and it now reads {_reference_params}"
)

# THE MEASURED HALF: a window whose PARCEL is its driest ground. The
# boundary encloses the dry western hillslope only; the wet eastern flat
# is inside the DEM window and outside the parcel. If the reference were
# the gate, the curve would be read off the dry cells and the parcel
# would flatter itself.
DRY_ROWS, DRY_COLS = 120, 160
dry_array = np.zeros((DRY_ROWS, DRY_COLS), dtype=np.float64)
for r in range(DRY_ROWS):
    for c in range(DRY_COLS):
        if c < 60:
            # West: a steep hillslope whose grade rises westward, so the
            # PARCEL's own TWI has a real spread. A uniformly dry parcel
            # would prove less: a curve read off it would hit the
            # degeneracy fallback, which is a different failure from the
            # one this section is about (a plausible-looking curve fitted
            # to the parcel instead of the landscape).
            dry_array[r, c] = 400.0 - 0.20 * r + 0.9 * (60 - c) + 0.004 * (60 - c) ** 2
        else:
            # East: the gentle wet flat the parcel does not contain.
            dry_array[r, c] = 400.0 - 0.20 * r - 0.06 * (c - 60) + 0.0004 * (c - 60) ** 2
dry_accumulation = np.ones((DRY_ROWS, DRY_COLS), dtype=np.float64)
for _r in range(DRY_ROWS):
    for _c in range(DRY_COLS):
        dry_accumulation[_r, _c] = (
            1.0 + 0.4 * max(0, _c - 10) + (4.0 * (_c - 60) if _c >= 60 else 0.0)
        )
DRY_DEM = _dem(dry_array)
# The parcel: the western hillslope only, well clear of the wet east.
DRY_BOUNDARY = box(
    ORIGIN_X + 5 * RESOLUTION,
    ORIGIN_Y - 110 * RESOLUTION,
    ORIGIN_X + 50 * RESOLUTION,
    ORIGIN_Y - 10 * RESOLUTION,
)
dry_result = compute_water_survey_areas(DRY_DEM, DRY_BOUNDARY, flow_accumulation=dry_accumulation)
dry_curve = dry_result["twi_breakpoints"]
dry_reference = dry_result["twi_reference_window"]
dry_raw = dry_result["screens"]["twi_raw"]
dry_gated = dry_result["gate_mask"]

_gated_raw = dry_raw[dry_gated]
_gated_raw = _gated_raw[~np.isnan(_gated_raw)]
_window_raw = dry_raw[dry_reference["mask"]]
_window_raw = _window_raw[~np.isnan(_window_raw)]
assert _gated_raw.size and _window_raw.size
assert _window_raw.size > _gated_raw.size * 2, (
    "the fixture must actually have much more window than parcel, or it proves nothing"
)
_gated_p90 = float(np.percentile(_gated_raw, 90.0))
_window_p90 = float(np.percentile(_window_raw, 90.0))
assert _window_p90 > _gated_p90 + 1.0, (
    f"the fixture's window must be materially wetter than its parcel (window p90 {_window_p90:.2f} "
    f"vs gated p90 {_gated_p90:.2f}), or the two populations are indistinguishable"
)
assert np.unique(_gated_raw).size > 20 and np.unique(_window_raw).size > 20, (
    "BOTH populations must carry a real spread -- a uniform parcel would only prove that a "
    "gate-referenced curve hits the degeneracy fallback, which is not the failure being ruled out"
)
assert dry_curve["curve_fallback"] is None, "the derived curve is on the normal p25/p90 path"
# THE ASSERTION: the derived breakpoints are the WINDOW's percentiles,
# to full precision, and NOT the gated cells'.
assert dry_curve["full_credit"] == _window_p90, (
    f"full credit must BE the window's p90 ({_window_p90}), got {dry_curve['full_credit']}"
)
assert dry_curve["floor"] == float(np.percentile(_window_raw, 25.0))
assert dry_curve["full_credit"] != _gated_p90, (
    "and it must not be the gated cells' p90 -- the parcel does not get to set its own curve"
)
assert dry_curve["floor"] != float(np.percentile(_gated_raw, 25.0)), (
    "nor the gated cells' p25 -- BOTH breakpoints come from the window"
)
# WHAT A GATE-REFERENCED CURVE WOULD HAVE DONE, measured rather than
# imagined: the parcel's own wettest ground would have been declared
# full-credit wet, when against its own landscape it is the dry end.
_gate_referenced = twi_window_breakpoints(dry_raw, dry_gated)
_wettest_gated = float(np.max(_gated_raw))
assert (
    twi_score(np.array([_wettest_gated]), _gate_referenced["floor"], _gate_referenced["full_credit"])[0]
    == 1.0
), "a gate-referenced curve gives this parcel's driest-landscape ground full wetness credit"
assert (
    twi_score(np.array([_wettest_gated]), dry_curve["floor"], dry_curve["full_credit"])[0] < 0.5
), "the window-referenced curve reads the same cell for what it is -- below half"
# The reference mask genuinely reaches outside the parcel:
_on_parcel = dry_result["on_parcel_mask"]
assert np.any(dry_reference["mask"] & ~_on_parcel), (
    "the reference window must contain ground the parcel does not -- that IS the landscape the "
    "curve is referenced to"
)
assert int(np.count_nonzero(dry_reference["mask"] & ~dry_gated)) > 0
print(
    f"3. The gate never enters the reference: twi_reference_window(dem, snap_meters) cannot reach "
    f"the boundary or the mask; on a fixture whose parcel is its driest ground the derived curve is "
    f"the WINDOW's p25/p90 ({dry_curve['floor']:.3f}/{dry_curve['full_credit']:.3f}) and not the "
    f"gated cells' (whose own p90 is {_gated_p90:.3f}), over "
    f"{dry_reference['cell_count']} reference cells against {int(np.count_nonzero(dry_gated))} gated."
)


# =========================================================================
# 4. THE WETNESS BLEND IS UN-PINNED
# =========================================================================
# THE SIGNATURE BEING KILLED: on the reference property the excavated
# WETNESS criterion read mean 0.041 with its MAX AT EXACTLY 0.500 -- the
# arithmetic tell of TWI contributing nothing at all, since wetness is
# 0.5*twi + 0.5*depression and a depression half saturated at 1.0 with a
# twi half pinned at 0.0 lands on exactly 0.500 and can never exceed it.
# A cell with real ponding AND real convergence must be able to clear
# that.
assert wsa.WETNESS_TWI_SUBWEIGHT == 0.5 and wsa.WETNESS_DEPRESSION_SUBWEIGHT == 0.5, (
    "the 0.500 ceiling is arithmetic from these two sub-weights -- if they move, so does the "
    "signature this section is about"
)

BASIN_ROWS, BASIN_COLS = 100, 100
basin_array = np.zeros((BASIN_ROWS, BASIN_COLS), dtype=np.float64)
for r in range(BASIN_ROWS):
    for c in range(BASIN_COLS):
        basin_array[r, c] = 150.0 - 0.30 * r + 0.35 * abs(c - 50)
# A real closed basin on the valley floor: 0.8 m deep, well over
# DEPRESSION_NOISE_FLOOR_METERS and over DEPRESSION_FULL_CREDIT_METERS.
for r in range(60, 68):
    for c in range(44, 56):
        basin_array[r, c] -= 0.8
BASIN_DEM = _dem(basin_array)
basin_accumulation = np.ones((BASIN_ROWS, BASIN_COLS), dtype=np.float64)
basin_accumulation[:, 48:53] = 250.0
BASIN_BOUNDARY = box(
    ORIGIN_X + 5 * RESOLUTION,
    ORIGIN_Y - 95 * RESOLUTION,
    ORIGIN_X + 95 * RESOLUTION,
    ORIGIN_Y - 5 * RESOLUTION,
)
basin_result = compute_water_survey_areas(
    BASIN_DEM, BASIN_BOUNDARY, flow_accumulation=basin_accumulation
)
wetness = basin_result["surfaces"]["criteria"][SURVEY_TYPE_EXCAVATED]["wetness"]
gated = basin_result["gate_mask"]
wetness_gated = wetness[gated]
assert wetness_gated.size, "the fixture must gate cells"
_max_wetness = float(np.max(wetness_gated))
assert _max_wetness > 0.5, (
    f"a cell with real depression AND real TWI must clear 0.5 for wetness -- the max-pinned-at-"
    f"exactly-0.500 signature means TWI is contributing nothing (got {_max_wetness})"
)
assert not math.isclose(_max_wetness, 0.500, abs_tol=1e-9), "and specifically it is not pinned AT 0.500"

# The cell that does it carries BOTH halves, not one of them twice:
_best = np.unravel_index(int(np.argmax(np.where(gated, wetness, -1.0))), wetness.shape)
_best_twi = float(basin_result["screens"]["twi_score"][_best])
_best_depth = float(basin_result["screens"]["depression_depth"][_best])
_best_depression = min(max(_best_depth / wsa.DEPRESSION_FULL_CREDIT_METERS, 0.0), 1.0)
assert _best_twi > 0.0, "the TWI half is genuinely contributing at the wettest cell"
assert _best_depression > 0.0, "and so is the depression half -- this is a blend, not one signal"
assert math.isclose(
    _max_wetness,
    wsa.WETNESS_TWI_SUBWEIGHT * _best_twi + wsa.WETNESS_DEPRESSION_SUBWEIGHT * _best_depression,
    abs_tol=1e-9,
), "and the criterion IS the stated 50/50 blend of the two, hand-recomputed"
print(
    f"4. The wetness blend is un-pinned: the wettest gated cell scores {_max_wetness:.4f} from a "
    f"TWI half of {_best_twi:.4f} and a depression half of {_best_depression:.4f} ({_best_depth:.3f} m "
    "of fill) -- above 0.5 and not pinned at it, so the signature of TWI contributing nothing is gone."
)

print("\nAll TWI window-referencing checks passed.")
