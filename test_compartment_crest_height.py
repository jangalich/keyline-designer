"""
test_compartment_crest_height.py

CREST HEIGHT ABOVE CHANNEL -- the compartment's DEPTH-OF-ENCLOSURE
measurement, the vertical companion to the crest-to-crest widths it
already carried. The outward-and-up walk has always computed crest
ELEVATIONS (it cannot declare a crest without them); this branch keeps
the vertical difference instead of discarding it.

WHY THE MEASUREMENT EXISTS. A compartment reports how WIDE the valley is
at its dam reach and says nothing about how DEEP the enclosure is, and
those are independent facts. A 65 ft pinch between shoulders standing
20 ft above the channel and the same 65 ft pinch between shoulders
standing 3 ft up are different sites -- the second spills around its
abutments at any useful pool height, and the width figure alone would
never say so.

Hand-derived fixtures throughout: every asserted height is computed from
the fixture's own stated side slopes and channel profile in the comment
above the assertion, before the code asserts it. Run as:

    python test_compartment_crest_height.py

Sections (the design's numbered test items in brackets):
  1  [1]  THE V-VALLEY -- crest heights at both transects, both sides,
          from the fixture's known rises; _min_ picks the lower side;
          the two stations are differenced against their OWN channel
          cells, not a common datum.
  2  [2]  ASYMMETRY -- a 3 m shoulder against a 12 m one: _min_ is 3,
          not the 7.5 mean and not the 12 max.
  3  [3]  ABSENT IS NOT ZERO -- a side that runs out the half-width
          bound stores None; _min_ falls to the other side; both absent
          -> None, and 0.0 appears nowhere on those records. With the
          converse: a MEASURED 0.0 (a crest confirmed at the station
          itself -- no shoulder at all) is a real reading, must not
          collapse into the absent sentinel, and wins the reduction.
  4  [4]  RAW, NOT CONDITIONED -- a filled pit at the pinch's channel
          cell: the heights come out of the RAW DEM and differ from
          what the conditioned surface would have produced.
  5  [5]  REUSE -- AST: the crest walk is defined exactly once
          repo-wide, and the height helpers this branch added contain
          no search of their own.
  6  [6]  CONTRACT -- the wire property set gains exactly these six
          keys and loses none; the consumer access patterns and the
          pooled selection are untouched. (The build_pipeline_context()
          half of this item rides in test_pipeline_context.py, beside
          the compartment-as-rank-1 case it already runs.)
  7       THE DIAGNOSTIC LINE -- both transects' left/right/min with
          units, the prominence threshold that produced them, and
          "no crest within bound" spelled out for an absent side.
"""

import ast
import inspect
import json
import pathlib

import numpy as np
from rasterio.warp import transform_geom
from shapely import contains_xy
from shapely.geometry import box

import water_survey_areas as wsa
from diagnose_water_survey_areas import summarize_survey_zones_table
from keypoint_detection import build_upstream_map
from raster_grid import pixel_center_xy
from valley_delineation import (
    compute_flow_accumulation,
    compute_flow_direction,
    fill_and_resolve,
    fill_depressions,
)
from water_survey_areas import (
    EMBANKMENT_WEIGHTS,
    RIDGE_PROMINENCE_METERS,
    SURVEY_TYPE_EMBANKMENT,
    _zone_feature_properties,
    build_embankment_compartment,
    compute_water_survey_areas,
    crest_height_above_channel,
    lower_crest_height,
    walk_embankment_pinch,
)

RESOLUTION = 5.0
ORIGIN_X, ORIGIN_Y = 500000.0, 4500000.0
CRS = "EPSG:32617"


def _dem(array):
    return {
        "array": np.asarray(array, dtype=np.float64),
        "resolution_meters": (RESOLUTION, RESOLUTION),
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def _flow(dem):
    filled = fill_depressions(dem["array"])
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    return filled, flow_to_row, flow_to_col


def _on_parcel(dem, boundary):
    rows, cols = dem["array"].shape
    col_x = dem["origin_x"] + (np.arange(cols) + 0.5) * RESOLUTION
    row_y = dem["origin_y"] - (np.arange(rows) + 0.5) * RESOLUTION
    xs, ys = np.meshgrid(col_x, row_y)
    return contains_xy(boundary, xs, ys) & ~np.isnan(dem["array"])


# =========================================================================
# THE V-VALLEY FIXTURE. 30x21 at 5 m, channel down col 10, flow +row
# (south; the channel falls 0.25 m/row, so base(r) = 100 - 0.25r).
#
# Cross-section per row, with d = |c - 10| and a half-width k(r):
#
#     k(r) = 4 (rows < 14), 2 (rows 14-17), 5 (rows >= 18)
#     d <  k : valley floor,  base(r) + 0.5*d      (10% cross-slope in)
#     d == k : SHOULDER CREST, base(r) + rise(r, side)
#     d >  k : outside,       base(r) + rise - 2.6 - 0.05*(d - k - 1)
#
# THE 2.6 m STEP OFF THE CREST is what lets the walk declare a crest at
# all: it exceeds RIDGE_PROMINENCE_METERS (1.0), so the sample one cell
# beyond the shoulder confirms the shoulder as the crest, and everything
# further out keeps falling and can never take the running maximum back.
#
# THE CREST HEIGHT IS THEREFORE rise(r, side) EXACTLY, at every station:
# the crest cell reads base(r) + rise and the station's channel cell
# reads base(r), and base(r) cancels. That is the whole hand-derivation,
# and it holds per station rather than against any common datum -- which
# is what section 1's two-station assertion is for, since base(5) and
# base(14) differ by 2.25 m.
#
# SIDES. The baseline runs seed -> pinch (due south), so
# perpendicular = (-dy, dx) = (+x, 0): the LEFT ray is +x, i.e. the
# columns ABOVE the channel, and the RIGHT ray is -x, the columns below
# it. (The same convention the width fixtures state.)
# =========================================================================

ROWS, COLS, CHANNEL = 30, 21, 10
SEED_ROW, PINCH_ROW = 5, 14


def _k_of_row(r):
    if 14 <= r <= 17:
        return 2
    return 4 if r < 14 else 5


def _v_array(left_rise, right_rise, pit=None):
    array = np.zeros((ROWS, COLS))
    for r in range(ROWS):
        base = 100.0 - 0.25 * r
        k = _k_of_row(r)
        for c in range(COLS):
            d = abs(c - CHANNEL)
            rise = left_rise(r) if c > CHANNEL else right_rise(r)
            if d < k:
                array[r, c] = base + 0.5 * d
            elif d == k:
                array[r, c] = base + rise
            else:
                array[r, c] = base + rise - 2.6 - 0.05 * (d - k - 1)
    if pit is not None:
        (pit_row, pit_col), depth = pit
        array[pit_row, pit_col] -= depth
    return array


BOUNDARY = box(
    ORIGIN_X + 1 * RESOLUTION + 0.1,
    ORIGIN_Y - 29 * RESOLUTION + 0.1,
    ORIGIN_X + 20 * RESOLUTION - 0.1,
    ORIGIN_Y - 1 * RESOLUTION - 0.1,
)
NO_ROAD = np.zeros((ROWS, COLS), dtype=bool)
_SHAPE = (ROWS, COLS)
_SURFACES = {
    SURVEY_TYPE_EMBANKMENT: np.full(_SHAPE, 0.6),
    "criteria": {
        SURVEY_TYPE_EMBANKMENT: {name: np.full(_SHAPE, 0.5) for name in EMBANKMENT_WEIGHTS}
    },
}
HEIGHT_FIELDS = (
    "seed_crest_height_left_m",
    "seed_crest_height_right_m",
    "seed_crest_height_min_m",
    "pinch_crest_height_left_m",
    "pinch_crest_height_right_m",
    "pinch_crest_height_min_m",
)


def _compartment(dem, **kwargs):
    """Seed at (5, 10), walked and assembled on one fixture -- the same
    hand-made seed/screens/surfaces the compartment tests use, so the
    only thing varying between sections is the TERRAIN."""
    filled, flow_to_row, flow_to_col = _flow(dem)
    on_parcel = _on_parcel(dem, BOUNDARY)
    walk = walk_embankment_pinch(
        dem, (SEED_ROW, CHANNEL), flow_to_row, flow_to_col, on_parcel, NO_ROAD
    )
    assert walk["found"] is True, f"fixture must pinch: {walk.get('reason_code')}"
    assert walk["pinch_rowcol"] == (PINCH_ROW, CHANNEL), (
        f"the fixture's waist is row {PINCH_ROW}, got {walk['pinch_rowcol']}"
    )
    seed_xy = pixel_center_xy(dem, SEED_ROW, CHANNEL)
    seed = {
        "rowcol": (SEED_ROW, CHANNEL),
        "xy": seed_xy,
        "geometry_wgs84": {
            "type": "Point",
            "coordinates": tuple(
                transform_geom(CRS, "EPSG:4326", {"type": "Point", "coordinates": seed_xy})[
                    "coordinates"
                ]
            ),
        },
        "blend_score": 0.8,
        "criteria_signature": {name: 0.5 for name in EMBANKMENT_WEIGHTS},
    }
    screens = {
        "twi_score": np.full(_SHAPE, 0.5),
        "depression_depth": np.zeros(_SHAPE),
        "flow_accumulation": compute_flow_accumulation(filled, flow_to_row, flow_to_col),
        "slope_pct": np.full(_SHAPE, 5.0),
        "soil_covered_mask": np.zeros(_SHAPE, dtype=bool),
        "soil_checked": False,
    }
    compartment = build_embankment_compartment(
        dem,
        seed,
        walk,
        build_upstream_map(flow_to_row, flow_to_col),
        BOUNDARY,
        None,
        _SURFACES,
        screens,
        **kwargs,
    )
    assert compartment is not None
    return compartment


# =========================================================================
# 1 [1]. THE V-VALLEY: both transects, both sides, hand-derived
# =========================================================================
# Rises chosen so all four shoulders differ and nothing can pass by
# coincidence:  LEFT 4.0 above the seed station / 6.0 above the pinch;
# RIGHT 5.0 / 7.0.
#
# THE WAIST IS THE DEEPER REACH, and that is not decoration: since the
# dam site chooses on width AND height (dam_site_score()), a waist whose
# shoulders were SHALLOWER than the seed reach's would not be chosen as
# the dam cell at all -- the objective would take the seed reach and the
# walk would fail best_site_at_seed. The fixture was built when only
# width mattered; the rises below make its waist a site the objective
# agrees with, which is also the physically sensible shape.
#
# HAND-DERIVED, per the cross-section above (crest height = rise):
#   seed station, row 5   (k=4, base 98.75): crest L = 98.75 + 4.0 =
#       102.75, crest R = 103.75, channel = 98.75 -> L 4.0, R 5.0
#   pinch station, row 14 (k=2, base 96.50): crest L = 99.00,
#       crest R = 103.50, channel = 96.50               -> L 6.0, R 7.0
#   _min_ = the LOWER shoulder: 4.0 at the seed, 6.0 at the pinch.
#
# THE TWO STATIONS' CHANNEL CELLS DIFFER BY 2.25 m (98.75 vs 96.50), so
# a single-datum implementation would have to miss one of these pairs.

V_DEM = _dem(_v_array(lambda r: 4.0 if r < 14 else 6.0, lambda r: 5.0 if r < 14 else 7.0))
v_compartment = _compartment(V_DEM)

assert V_DEM["array"][SEED_ROW, CHANNEL] == 98.75
assert V_DEM["array"][PINCH_ROW, CHANNEL] == 96.50
assert v_compartment["seed_crest_height_left_m"] == 4.0
assert v_compartment["seed_crest_height_right_m"] == 5.0
assert v_compartment["pinch_crest_height_left_m"] == 6.0
assert v_compartment["pinch_crest_height_right_m"] == 7.0
assert v_compartment["seed_crest_height_min_m"] == 4.0, (
    "the seed station's binding shoulder is its LOWER side (4.0), not the right one (5.0)"
)
assert v_compartment["pinch_crest_height_min_m"] == 6.0, (
    "the pinch station's binding shoulder is its LOWER side (6.0), not the right one (7.0)"
)

# The DEPTH is independent of the WIDTH, which this same fixture also
# reports: the pinch is the NARROWER station (17.5 m against 37.5 m) and
# also the SHALLOWER one. Two measurements, both on the record.
by_end = {transect["end"]: transect for transect in v_compartment["transects"]}
assert by_end["seed"]["width_m"] == 37.5 and by_end["pinch"]["width_m"] == 17.5
assert by_end["seed"]["crest_height_min_m"] == 4.0
assert by_end["pinch"]["crest_height_min_m"] == 6.0

print(
    "1. V-valley: shoulder heights 4.0/5.0 m at the seed station and 6.0/7.0 m at the pinch -- each "
    "differenced against its OWN channel cell (98.75 vs 96.50 m, 2.25 m apart) -- and _min_ takes "
    "the lower side at both (4.0, 6.0), beside the widths 37.5 / 17.5 m."
)


# =========================================================================
# 2 [2]. ASYMMETRY: 3 m against 12 m -- the LOWER, never the mean
# =========================================================================
# One shoulder 3 m above the channel, the other 12 m, at every row. The
# mean is 7.5 m and the max is 12 m, and NEITHER describes a constraint
# that exists on this ground: water rising in this compartment spills
# over the 3 m side a little past 3 m, with the 12 m side irrelevant to
# that limit. _min_ must read 3.0.

ASYM_DEM = _dem(_v_array(lambda r: 3.0, lambda r: 12.0))
asym = _compartment(ASYM_DEM)

for end in ("seed", "pinch"):
    assert asym[f"{end}_crest_height_left_m"] == 3.0
    assert asym[f"{end}_crest_height_right_m"] == 12.0
    assert asym[f"{end}_crest_height_min_m"] == 3.0, (
        f"{end}: the binding shoulder is the 3 m one -- got "
        f"{asym[f'{end}_crest_height_min_m']}"
    )
    assert asym[f"{end}_crest_height_min_m"] != 7.5, "the mean of the two sides is not the answer"
    assert asym[f"{end}_crest_height_min_m"] != 12.0, "the higher shoulder is not the answer"

# The reduction itself, exercised directly and both ways round, so the
# result cannot be an artifact of which side happened to be the lower.
assert lower_crest_height(3.0, 12.0) == 3.0
assert lower_crest_height(12.0, 3.0) == 3.0

print(
    "2. Asymmetry: a 3 m shoulder against a 12 m one reads _min_ = 3.0 at both transects -- not the "
    "7.5 m mean, not the 12 m max; the reduction is order-independent."
)


# =========================================================================
# 3 [3]. ABSENT IS NOT ZERO
# =========================================================================
# THE SENTINEL, STATED. A walk that runs out RIDGE_WALK_MAX_HALF_WIDTH_
# METERS (or leaves the grid) without the prominence fall never declared
# a crest -- ridge_crest_walk() returns the walk's END point with
# bound_hit True, which is an unconfirmed running point and not a
# shoulder. There is no height to report. None is the honest answer and
# 0.0 would be a claim the instrument never made ("the shoulder is level
# with the channel"), the same discipline road_overlap_pct and
# unreachable_stem_end keep.
#
# THE CONVERSE IS ALSO ASSERTED (3d): a MEASURED 0.0 -- a crest the walk
# confirmed at the station itself, meaning no shoulder above the channel
# at all -- is a real reading and must never collapse into the absent
# sentinel, in either direction.

# --- 3a. ONE SIDE ABSENT: the left flank rises to the grid edge ---
# Left of the channel (c > 10) the ground climbs 0.6 m/cell all the way
# to col 20 and never falls, so the +x walk leaves the grid at
# (20 - 10) * 5 + 2.5 = 52.5 m with no crest: absent. The right flank is
# the ordinary V with a 3.0 m shoulder, so the right side measures.
# The valley still PINCHES (the right half-width tracks k(r): 20 -> 10
# -> 25 m), which is what keeps the walk and the compartment real.


def _one_sided_array():
    array = np.zeros((ROWS, COLS))
    for r in range(ROWS):
        base = 100.0 - 0.25 * r
        k = _k_of_row(r)
        for c in range(COLS):
            if c > CHANNEL:
                array[r, c] = base + 0.6 * (c - CHANNEL)
                continue
            d = CHANNEL - c
            if d < k:
                array[r, c] = base + 0.5 * d
            elif d == k:
                array[r, c] = base + 3.0
            else:
                array[r, c] = base + 3.0 - 2.6 - 0.05 * (d - k - 1)
    return array


one_sided = _compartment(_dem(_one_sided_array()))
one_sided_transects = {t["end"]: t for t in one_sided["transects"]}
for end in ("seed", "pinch"):
    assert one_sided[f"{end}_crest_height_left_m"] is None, (
        f"{end}: the endlessly-rising flank declared no crest -- the height is ABSENT, got "
        f"{one_sided[f'{end}_crest_height_left_m']!r}"
    )
    assert one_sided_transects[end]["left"]["bound_hit"] is True, (
        "the walk itself must be the thing reporting the absence"
    )
    assert one_sided[f"{end}_crest_height_right_m"] == 3.0
    assert one_sided[f"{end}_crest_height_min_m"] == 3.0, (
        "_min_ falls to the side that MEASURED -- a missing side is unmeasured, not short, and can "
        "never win this comparison"
    )
    # The absence is disclosed by the existing bound flag, not invented
    # here: the same walk that found no crest also floors the width.
    assert one_sided_transects[end]["left"]["half_width_m"] == 52.5

assert one_sided["half_width_bound_hit"] is True, (
    "a compartment with an unresolved flank carries the existing bound flag"
)

# --- 3b. BOTH SIDES ABSENT ---
# The same V-valley of section 1, walked under a 6.0 m half-width bound.
# Its nearest shoulder is the pinch's left one, first sampled at
# 5*2 - 2.5 = 7.5 m; every other is further (10.0, 17.5, 20.0 m). All
# four walks run out the bound on monotonically rising floor, so all
# four sides are absent -- and _min_ has nothing to reduce.
both_absent = _compartment(V_DEM, max_half_width_meters=6.0)
for field in HEIGHT_FIELDS:
    assert both_absent[field] is None, (
        f"{field}: both flanks unresolved -> the min is None, got {both_absent[field]!r}"
    )

# --- 3c. 0.0 APPEARS NOWHERE ---
# Not on the zone dict, not on the wire. Checked by identity as well as
# equality, because False == 0.0 in Python and a boolean slipping into
# one of these fields would be exactly the kind of silent zero this
# section exists to forbid.
for record, label in (
    (one_sided, "one flank absent"),
    (both_absent, "both flanks absent"),
):
    for field in HEIGHT_FIELDS:
        value = record[field]
        assert value is None or (isinstance(value, float) and value != 0.0), (
            f"{label}: {field} is {value!r} -- an absent shoulder is None, never 0.0"
        )

# --- 3d. AND A MEASURED 0.0 IS NOT ABSENT ---
# THE OTHER HALF OF THE RULE, and the one that bites: ridge_crest_walk()
# seeds its running maximum with the START cell, so ground that falls the
# full prominence IMMEDIATELY off the station declares the crest AT the
# station -- half_width_m 0.0, crest_rowcol the channel cell, bound_hit
# False. That is a CONFIRMED crest, and the height it implies is 0.0:
# there is no shoulder above the channel on that side at all. It is the
# most alarming enclosure reading the instrument can produce and it is
# measured, not missing, so the two must stay distinguishable -- "we
# could not find the shoulder" and "there is no shoulder" are opposite
# findings, and the reference run produces both.
#
# TESTED ON A HAND-BUILT STRIP, off a real walk, the way the prominence
# guard itself is tested: a valley whose flank drops a metre at the first
# cell also drops its own D8 flow over that edge, so there is no
# southbound channel left to seed a compartment walk from. The mechanism
# under test is the walk's declaration and the subtraction over it, and
# both are exercised exactly as build_embankment_compartment() exercises
# them -- one ridge_crest_walk(), one crest_height_above_channel() on its
# result.
#
# The strip's middle row, walking +x from col 2: 20.0 (the station),
# 18.5 (-1.5, past the 1.0 m prominence at the FIRST sample), then
# falling away. Walking -x from the same cell: 20.0, 21.0, 23.0 (a real
# shoulder), 21.5 -- a 1.5 m fall that declares it.
CLIFF_STRIP = _dem(
    np.tile(
        np.array([21.5, 23.0, 21.0, 20.0, 18.5, 18.4, 18.3, 18.2], dtype=float),
        (3, 1),
    )
)
_station_rowcol = (1, 3)
_station_xy = pixel_center_xy(CLIFF_STRIP, *_station_rowcol)
assert CLIFF_STRIP["array"][_station_rowcol] == 20.0

_over_the_edge = wsa.ridge_crest_walk(CLIFF_STRIP, _station_xy, (1.0, 0.0))
assert _over_the_edge["bound_hit"] is False, "the 1.5 m drop CONFIRMS a crest -- not a bounded walk"
assert _over_the_edge["half_width_m"] == 0.0, "the crest is declared at the station itself"
assert _over_the_edge["crest_rowcol"] == _station_rowcol
assert crest_height_above_channel(CLIFF_STRIP, _over_the_edge, _station_rowcol) == 0.0, (
    "a station on the lip of a drop has NO shoulder above it: 0.0 is the MEASURED answer, and it "
    "is not the absent sentinel"
)
assert crest_height_above_channel(CLIFF_STRIP, _over_the_edge, _station_rowcol) is not None

# The other side of the same station is an ordinary 3.0 m shoulder
# (23.0 - 20.0), so the pair is exactly the case the reduction must get
# right.
_real_shoulder = wsa.ridge_crest_walk(CLIFF_STRIP, _station_xy, (-1.0, 0.0))
assert _real_shoulder["bound_hit"] is False and _real_shoulder["crest_rowcol"] == (1, 1)
assert crest_height_above_channel(CLIFF_STRIP, _real_shoulder, _station_rowcol) == 3.0

# AND THE 0.0 WINS THE REDUCTION. A shoulder level with the channel is
# the hardest binding constraint there is; a truthiness filter would drop
# it and hand the verdict to the 3.0 m side, reporting an unenclosed
# station as an enclosed one.
assert lower_crest_height(0.0, 3.0) == 0.0 and lower_crest_height(3.0, 0.0) == 0.0
assert lower_crest_height(0.0, None) == 0.0, (
    "a measured 0.0 beside an absent side is 0.0 -- not None, and not the absent side"
)

# The helper's own two refusals, direct: a bounded walk has no height
# whatever elevation it stopped at, and neither does an empty reduction.
assert crest_height_above_channel(
    V_DEM, {"bound_hit": True, "crest_elevation_m": 120.0}, (SEED_ROW, CHANNEL)
) is None, "a bound-hit walk's END elevation is not a crest and yields no height"
assert lower_crest_height(None, None) is None
assert lower_crest_height(None, 2.0) == 2.0 and lower_crest_height(2.0, None) == 2.0

print(
    "3. Absent is not zero: a flank that rises past the bound stores None at both transects (its "
    "walk bound-hit at 52.5 m) and _min_ falls to the 3.0 m side; under a 6.0 m bound all four "
    "flanks go absent and all six fields are None; 0.0 appears on neither record. And the converse "
    "holds -- a station on the lip of a drop measures 0.0 (a crest confirmed at distance 0, NOT an "
    "absence) and that 0.0 wins the binding-side reduction against a 3.0 m shoulder."
)


# =========================================================================
# 4 [4]. RAW, NOT CONDITIONED
# =========================================================================
# THE POINT. fill_and_resolve()'s output carries depression fill and
# flat-resolution epsilon increments -- hydrological bookkeeping, not
# terrain truth. Reading a channel elevation off it would raise the
# floor of the subtraction and SHRINK every height measured there.
#
# THE FIXTURE: section 1's V-valley with a 1.5 m pit gouged into the
# PINCH's own channel cell. Raw, that cell reads 96.50 - 1.5 = 95.00,
# and the pinch shoulders stand at 99.00 (left) and 100.00 (right), so
# the hand-derived RAW heights are 7.5 and 8.5 -- the fixture's 6.0/7.0
# rises PLUS the 1.5 m the channel was dropped. The conditioned surface
# fills that pit back to ~96.25 and would report ~2.75 / ~3.75 instead.
# The seed station, untouched by the pit, must not move at all.

PIT_DEPTH = 1.5
PIT_DEM = _dem(
    _v_array(
        lambda r: 4.0 if r < 14 else 6.0,
        lambda r: 5.0 if r < 14 else 7.0,
        pit=((PINCH_ROW, CHANNEL), PIT_DEPTH),
    )
)
conditioned = fill_and_resolve(PIT_DEM["array"])
raw_channel = float(PIT_DEM["array"][PINCH_ROW, CHANNEL])
conditioned_channel = float(conditioned[PINCH_ROW, CHANNEL])
assert raw_channel == 95.00
assert conditioned_channel > raw_channel + 1.0, (
    "the fixture must actually be filled, or this section proves nothing: raw "
    f"{raw_channel} vs conditioned {conditioned_channel}"
)

pit = _compartment(PIT_DEM)
assert pit["pinch_crest_height_left_m"] == 6.0 + PIT_DEPTH == 7.5
assert pit["pinch_crest_height_right_m"] == 7.0 + PIT_DEPTH == 8.5
assert pit["pinch_crest_height_min_m"] == 7.5

# And the value the conditioned surface WOULD have produced is different
# -- asserted, not assumed, so this cannot pass by the two agreeing.
# The pinch shoulder sits at base(14) + 6.0 = 102.50.
conditioned_left = round(102.50 - conditioned_channel, 2)
assert abs(pit["pinch_crest_height_left_m"] - conditioned_left) > 1.0, (
    f"raw {pit['pinch_crest_height_left_m']} m and conditioned {conditioned_left} m must differ, or "
    "the fixture does not discriminate between the two surfaces"
)

# The unfilled station is untouched: the fill increment is local to the
# pit and does not leak up-channel through the measurement.
assert pit["seed_crest_height_left_m"] == 4.0
assert pit["seed_crest_height_right_m"] == 5.0
assert pit["seed_crest_height_min_m"] == v_compartment["seed_crest_height_min_m"]

print(
    f"4. Raw not conditioned: a {PIT_DEPTH} m pit at the pinch's channel cell (raw {raw_channel} m, "
    f"conditioned {conditioned_channel:.3f} m) gives raw heights 7.5 / 8.5 m -- the conditioned "
    f"surface would have said {conditioned_left} m -- and the unfilled seed station does not move."
)


# =========================================================================
# 5 [5]. REUSE: ONE CREST WALK, REPO-WIDE
# =========================================================================
# This branch measures depth off the SAME two walks that already measured
# width. The pin is structural: the walk is defined exactly once across
# every module in the repository, and the helpers this branch added are
# arithmetic over a walk's RESULT, not searches of their own.

_REPO = pathlib.Path(__file__).resolve().parent
_definitions = {"ridge_crest_walk": [], "measure_valley_width": []}
for _path in sorted(_REPO.rglob("*.py")):
    _tree = ast.parse(_path.read_text())
    for _node in ast.walk(_tree):
        if isinstance(_node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _node.name in _definitions:
            _definitions[_node.name].append(f"{_path.name}:{_node.lineno}")
for _name, _sites in _definitions.items():
    assert len(_sites) == 1, (
        f"{_name} must be defined exactly once repo-wide -- a second crest-finding implementation "
        f"is the thing this branch may not add: {_sites}"
    )

# The two helpers: no marching, no scanning, no prominence rule of their
# own. A loop or a comprehension inside either would be the signature of
# a second search, so both are forbidden outright.
for _helper in (crest_height_above_channel, lower_crest_height):
    _body = ast.parse(inspect.getsource(_helper))
    _loops = [
        type(_node).__name__
        for _node in ast.walk(_body)
        if isinstance(_node, (ast.For, ast.AsyncFor, ast.While))
    ]
    assert not _loops, (
        f"{_helper.__name__} must not search -- it reads a completed walk's result: {_loops}"
    )
    assert "prominence" not in inspect.getsource(_helper).split('"""')[2], (
        f"{_helper.__name__}'s CODE must not re-implement the prominence rule (its docstring may "
        "and does explain it)"
    )

# And the compartment builder still calls the one walk exactly twice per
# transect -- the same four calls that produced the widths.
_builder = ast.parse(inspect.getsource(build_embankment_compartment))
_walk_calls = [
    _node
    for _node in ast.walk(_builder)
    if isinstance(_node, ast.Call)
    and isinstance(_node.func, ast.Name)
    and _node.func.id == "ridge_crest_walk"
]
assert len(_walk_calls) == 2, (
    "the two ridge_crest_walk() calls in the transect loop (one per side) are the ONLY crest "
    f"finding on this path; found {len(_walk_calls)}"
)

print(
    f"5. Reuse: ridge_crest_walk and measure_valley_width are each defined exactly once repo-wide "
    f"({_definitions['ridge_crest_walk'][0]}, {_definitions['measure_valley_width'][0]}); the "
    "compartment's transect loop still makes the same two walk calls, and both height helpers are "
    "loop-free arithmetic over a walk's result."
)


# =========================================================================
# 6 [6]. THE CONTRACT: SIX KEYS ADDED, NONE REMOVED
# =========================================================================
# The wire property set as merged main produced it, listed literally so
# a removal or a rename is a test failure rather than a silent contract
# break downstream. (Extracted from 8d43bb0's _zone_feature_properties;
# both type branches are here because the set is type-dispatched.)
MAIN_PROPERTY_KEYS = frozenset(
    {
        "baseline_length_m",
        "below_min_area",
        "boundary_adjacency_fraction",
        "canopy_overlap_pct",
        "catchment_ceiling_acres",
        "catchment_exceeds_ceiling",
        "cell_count",
        "compartment_footprint_acres",
        "contributing_area_acres_at_wettest_cell",
        "criteria_complete",
        "criterion_contributions",
        "cross_type_overlaps",
        "depression_depth_max_ft",
        "depression_depth_mean_m",
        "drop_reason",
        "flags",
        "half_width_bound_hit",
        "has_service_relationship",
        "max_elevation_m",
        "max_suitability",
        "mean_suitability",
        "nominated_by",
        "pinch_catchment_acres",
        "pinch_drainage_score",
        "pinch_rowcol",
        "pinch_terminal",
        "pinch_walk_distance_m",
        "pinch_width_m",
        "presentation_order",
        "presented",
        "primary_production_area_relationship",
        "production_area_relationships",
        "production_overlap_pct",
        "rank",
        "representative_elevation_m",
        "road_overlap_pct",
        "seed_blend_score",
        "seed_criteria_signature",
        "seed_rowcol",
        "served_production_area_ids",
        "slope_median_pct",
        "soil_coverage_fraction",
        "sparse_anchor",
        "status",
        "still_narrowing_at_termination",
        "survey_type",
        "truncated_by_boundary",
        "truncated_by_road",
        "twi_score_max",
        "twi_score_mean",
        "width_profile_max_m",
        "width_profile_min_m",
        "zone_acres",
        "zone_id",
    }
)

_wire_zone = dict(v_compartment)
_wire_zone.update(
    {
        "id": 1,
        "status": "survived",
        "drop_reason": None,
        "rank": 1,
        "presented": True,
        "presentation_order": 1,
        "cross_type_overlaps": [],
        "canopy_overlap_pct": None,
        "road_overlap_pct": None,
        "production_overlap_pct": None,
        "primary_production_area_relationship": None,
        "production_area_relationships": [],
        "has_service_relationship": False,
        "served_production_area_ids": [],
    }
)
# Keys added to the wire by branches that landed AFTER this file's six.
# Named literally, with the branch that added each, so a later addition is
# a deliberate line here rather than a quietly relaxed assertion. The
# equality below still fails on an unrecorded key.
LATER_BRANCH_PROPERTY_KEYS = frozenset(
    {
        # All three from "Minimum binding shoulder -- the dam site must
        # be able to impound": the gate's verdict, the binding shoulder
        # measured at the CHOSEN dam site (a walk field since "The dam
        # site chooses on width AND height", promoted to the wire by the
        # gate) and the threshold it was held to. A refusal has to be
        # readable off the feature together with how far it missed by.
        "shoulder_below_minimum",
        "pinch_binding_height_m",
        "min_binding_shoulder_m",
        # From "the binding shoulder ships in feet too": the map's zone
        # panel prints this measurement, so the conversion moved to the
        # wire beside the metric original -- the rule
        # depression_depth_max_ft already follows. The metres stay
        # because min_binding_shoulder_m above is the bar this is
        # compared against, and a refusal has to be readable in one unit.
        "pinch_binding_height_ft",
    }
)

_properties = _zone_feature_properties(_wire_zone)
_added = set(_properties) - MAIN_PROPERTY_KEYS
assert _added == set(HEIGHT_FIELDS) | LATER_BRANCH_PROPERTY_KEYS, (
    "the wire gains exactly the six crest-height keys plus the keys later branches recorded in "
    f"LATER_BRANCH_PROPERTY_KEYS and nothing else; added {sorted(_added)}"
)
assert set(HEIGHT_FIELDS).isdisjoint(LATER_BRANCH_PROPERTY_KEYS), (
    "a later branch may not launder one of this branch's six keys through the allowance list"
)
_removed = MAIN_PROPERTY_KEYS - set(_properties) - {"member_acres", "member_count", "member_ids"}
assert not _removed, f"no existing consumer-read property may disappear: {sorted(_removed)}"
for _field in HEIGHT_FIELDS:
    assert _properties[_field] == _wire_zone[_field], (
        f"{_field} rides the wire as the stored metric value, unconverted and unrounded again"
    )

# The heights reach the wire as METRES and are JSON-clean (None, never
# NaN, never a numpy scalar that json refuses).
json.dumps({field: _properties[field] for field in HEIGHT_FIELDS})

# THE POOLED SELECTION IS UNTOUCHED. Nothing added here scores, ranks or
# gates, so a full compute over a real fixture must still crown the zone
# it crowned before. (The build_pipeline_context() end of this item runs
# in test_pipeline_context.py, beside the compartment-as-rank-1 case
# that file already carries.)
_result = compute_water_survey_areas(V_DEM, BOUNDARY)
_selected = _result["selected_water_zone"]
assert _selected is not None and _selected in _result["zones"]
_selected_compartments = [
    zone for zone in _result["zones_by_type"][SURVEY_TYPE_EMBANKMENT] if zone["rank"] == 1
]
for _zone in _result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]:
    for _field in HEIGHT_FIELDS:
        assert _field in _zone, f"every compartment carries {_field}"
        assert _zone[_field] is None or isinstance(_zone[_field], float)
    # The consumer access patterns, replayed on a compartment that now
    # carries six more fields than it did.
    assert _zone["render_fill_polygon_utm"] is _zone["polygon_utm"]
    assert isinstance(_zone["representative_elevation_m"], float)
    _ = _zone["render_fill_polygon_utm"].buffer(6.096)

print(
    f"6. Contract: the wire property set gains the six crest-height keys plus "
    f"{len(LATER_BRANCH_PROPERTY_KEYS)} keys from later branches, and loses none "
    f"({len(_properties)} properties on a compartment feature); the full compute still selects zone "
    f"{_selected['id']} ({_selected['survey_type']}) and every compartment's consumer access "
    "patterns are intact."
)


# =========================================================================
# 7. THE DIAGNOSTIC: WIDTH AND DEPTH READ TOGETHER
# =========================================================================
# A reader looking at a compartment's width line must be able to see, on
# the next line, how deep the enclosure producing that width is -- with
# the prominence threshold that decided every one of those heights, and
# with an absent flank spelled out rather than blank.

_table = summarize_survey_zones_table(_result)
assert "enclosure depth above channel" in _table
assert f"{RIDGE_PROMINENCE_METERS} m prominence" in _table, (
    "the threshold that produced the heights travels with them -- a reader cannot judge a lower "
    "bound without knowing what declared the crest"
)
assert "LOWER BOUND" in _table and "binding" in _table

# ONE LINE PER SURVIVING COMPARTMENT, each carrying both transects'
# three figures -- matched line-by-line against the zone lines they sit
# under, so a line that went missing or doubled cannot hide behind a
# sibling that rendered correctly.
_lines = _table.splitlines()
_depth_lines = [line for line in _lines if "enclosure depth above channel" in line]
_survivors = _result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
assert len(_depth_lines) == len(_survivors), (
    f"one depth line per SURVIVING compartment: {len(_depth_lines)} lines for "
    f"{len(_survivors)} survivors"
)
for _index, _zone in enumerate(_survivors):
    # The depth line is the one IMMEDIATELY under this zone's own width
    # line -- that adjacency is the point of the change.
    _zone_line_index = next(
        i for i, line in enumerate(_lines) if f"zone {_zone['id']}:" in line and "pinch " in line
    )
    _depth_line = _lines[_zone_line_index + 1]
    assert "enclosure depth above channel" in _depth_line, (
        f"zone {_zone['id']}'s depth line must sit directly under its width line, so the two read "
        f"together; found {_depth_line!r}"
    )
    for _end in ("seed", "pinch"):
        for _side, _field in (
            ("L", f"{_end}_crest_height_left_m"),
            ("R", f"{_end}_crest_height_right_m"),
            ("min", f"{_end}_crest_height_min_m"),
        ):
            _value = _zone[_field]
            _rendered = "no crest within bound" if _value is None else f"{_value:.2f} m"
            assert f"{_side} {_rendered}" in _depth_line, (
                f"zone {_zone['id']}: the line must print {_end} {_side} as {_rendered!r} -- "
                f"{_depth_line!r}"
            )

# An absent flank is words, not a blank: the one-sided fixture's table
# says so in full.
_one_sided_result = compute_water_survey_areas(_dem(_one_sided_array()), BOUNDARY)
_one_sided_table = summarize_survey_zones_table(_one_sided_result)
if _one_sided_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]:
    assert "no crest within bound" in _one_sided_table, (
        "an unresolved flank is stated on the diagnostic, never left blank or printed as 0.00 m"
    )
    _absent_line = next(
        line for line in _one_sided_table.splitlines() if "no crest within bound" in line
    )
    assert "0.00 m" not in _absent_line

print(
    "7. Diagnostic: every surviving compartment gets one enclosure-depth line under its width line "
    f"-- both transects' L/R/min in metres, the {RIDGE_PROMINENCE_METERS} m prominence that "
    "declared the crests, and 'no crest within bound' written out for an unresolved flank."
)

print("\nAll crest-height checks passed.")
