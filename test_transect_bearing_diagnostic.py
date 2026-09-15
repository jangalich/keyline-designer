"""
test_transect_bearing_diagnostic.py

THE BEARING A/B INSTRUMENT (diagnose_transect_bearing.py): the
diagnostic that asks whether the compartment crest-height field's
shallow readings are a fact about the ground or an artifact of the
transects running perpendicular to the SEED->PINCH BASELINE rather than
to the LOCAL channel.

An instrument that returns a verdict has to be shown capable of
returning BOTH verdicts, so the two central fixtures here are built to
force one each:

  * a STRAIGHT valley, where baseline and local channel coincide and the
    instrument must say the bearing is not the problem;
  * a CURVED valley whose bend sits just downstream of the seed, where
    the baseline is a chord across that bend and method A demonstrably
    samples ALONG the channel -- the artifact, reproduced from a known
    cause so we know the detector detects it.

Run as:

    python test_transect_bearing_diagnostic.py

Sections (the design's numbered test items in brackets):
  1  [1]  STRAIGHT VALLEY -- A and B agree within tolerance, angle ~0.
  2  [2]  CURVED VALLEY -- at the seed, A finds one flank absent and the
          other 0.00 m while B finds the hand-derived 3.0 m shoulder;
          at the pinch, down the straight leg, the two agree. Both
          numbers stated in the fixture comment.
  3  [3]  THE ANGLE -- hand-checked on that known bend, and on unit
          vectors whose angle is arithmetic.
  4  [4]  ABSENT SEMANTICS IN B -- None not 0.0; a measured 0.0 still a
          real reading that wins the binding reduction.
  5  [5]  DIAGNOSTIC-ONLY -- AST: no production module imports the
          instrument, and the instrument defines neither a crest walk
          nor a direction estimator of its own. The two pins that made
          the SECANT diagnostic-only are RETIRED here, deliberately:
          it has shipped onto the pinch walk. The TRANSECT bearing
          stays pinned.
  6       THE VERDICT RULE -- pinned thresholds applied to synthetic
          classification sets, including the "do not force a verdict"
          mixed case.
  7  [4]  THE -0.00 READING -- the zero's mechanism confirmed (crest AT
          the station), and the MINUS SIGN traced to a separate
          double-rounding defect in the field itself, reproduced from a
          hand-built fixture and left unfixed per the brief.
"""

import ast
import math
import pathlib

import numpy as np
from rasterio.warp import transform_geom
from shapely import contains_xy
from shapely.geometry import box

import diagnose_transect_bearing as dtb
import water_survey_areas as wsa
from diagnose_transect_bearing import (
    COMPARABLE,
    DEEPER_UNDER_A,
    DEEPER_UNDER_B,
    METHOD_BASELINE,
    METHOD_LOCAL,
    NEITHER_MEASURED,
    VERDICT_BEARING_ARTIFACT,
    VERDICT_MARGIN_METERS,
    VERDICT_MIXED,
    VERDICT_TERRAIN,
    _angle_between_degrees,
    compare_compartment_bearings,
    decide_verdict,
)
from keypoint_detection import build_upstream_map
from raster_grid import pixel_center_xy
from valley_delineation import compute_flow_accumulation, compute_flow_direction, fill_depressions
from water_survey_areas import (
    EMBANKMENT_WEIGHTS,
    RIDGE_PROMINENCE_METERS,
    SURVEY_TYPE_EMBANKMENT,
    build_embankment_compartment,
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


def _build(dem, seed_rowcol, max_walk_meters):
    """One compartment on one fixture, through the real production path
    -- same hand-made seed/screens/surfaces the compartment tests use,
    so the only thing varying between sections is the TERRAIN."""
    rows, cols = dem["array"].shape
    boundary = box(
        ORIGIN_X + 1 * RESOLUTION + 0.1,
        ORIGIN_Y - (rows - 2) * RESOLUTION + 0.1,
        ORIGIN_X + (cols - 1) * RESOLUTION - 0.1,
        ORIGIN_Y - 1 * RESOLUTION - 0.1,
    )
    filled = fill_depressions(dem["array"])
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    col_x = ORIGIN_X + (np.arange(cols) + 0.5) * RESOLUTION
    row_y = ORIGIN_Y - (np.arange(rows) + 0.5) * RESOLUTION
    xs, ys = np.meshgrid(col_x, row_y)
    on_parcel = contains_xy(boundary, xs, ys)
    no_road = np.zeros(dem["array"].shape, dtype=bool)
    walk = walk_embankment_pinch(
        dem,
        seed_rowcol,
        flow_to_row,
        flow_to_col,
        on_parcel,
        no_road,
        max_walk_meters=max_walk_meters,
    )
    assert walk["found"] is True, f"fixture must pinch: {walk.get('reason_code')}"
    seed_xy = pixel_center_xy(dem, *seed_rowcol)
    seed = {
        "rowcol": seed_rowcol,
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
    shape = dem["array"].shape
    screens = {
        "twi_score": np.full(shape, 0.5),
        "depression_depth": np.zeros(shape),
        "flow_accumulation": compute_flow_accumulation(filled, flow_to_row, flow_to_col),
        "slope_pct": np.full(shape, 5.0),
        "soil_covered_mask": np.zeros(shape, dtype=bool),
        "soil_checked": False,
    }
    surfaces = {
        SURVEY_TYPE_EMBANKMENT: np.full(shape, 0.6),
        "criteria": {
            SURVEY_TYPE_EMBANKMENT: {name: np.full(shape, 0.5) for name in EMBANKMENT_WEIGHTS}
        },
    }
    compartment = build_embankment_compartment(
        dem,
        seed,
        walk,
        build_upstream_map(flow_to_row, flow_to_col),
        boundary,
        None,
        surfaces,
        screens,
    )
    assert compartment is not None
    compartment["id"] = 0
    return compartment, walk


# =========================================================================
# 1 [1]. THE STRAIGHT VALLEY: baseline IS the channel
# =========================================================================
# 30x21 at 5 m, channel down col 10 flowing south, base(r) = 100 - 0.25r,
# floor base + 0.5d, shoulder base + 3.0 at d = k(r), then a 2.6 m step
# off the crest (well past the 1.0 m prominence) and a gentle fall.
# k(r) = 4 (rows < 14), 2 (rows 14-17), 5 (rows >= 18), so the waist is
# row 14 and the seed at row 5 walks straight down to it.
#
# THE BASELINE AND THE CHANNEL ARE THE SAME LINE HERE, so B has nothing
# to correct: both bearings read due south, the angle is 0, and every
# height must match exactly -- not merely within tolerance. Anything
# else would mean the instrument perturbs what it measures.

STRAIGHT_ROWS, STRAIGHT_COLS, CHANNEL = 30, 21, 10


def _k_of_row(r):
    if 14 <= r <= 17:
        return 2
    return 4 if r < 14 else 5


def _straight_array():
    array = np.zeros((STRAIGHT_ROWS, STRAIGHT_COLS))
    for r in range(STRAIGHT_ROWS):
        base = 100.0 - 0.25 * r
        k = _k_of_row(r)
        for c in range(STRAIGHT_COLS):
            d = abs(c - CHANNEL)
            if d < k:
                array[r, c] = base + 0.5 * d
            elif d == k:
                array[r, c] = base + 3.0
            else:
                array[r, c] = base + 3.0 - 2.6 - 0.05 * (d - k - 1)
    return array


STRAIGHT_DEM = _dem(_straight_array())
straight_compartment, _straight_walk = _build(STRAIGHT_DEM, (5, CHANNEL), 200.0)
straight = compare_compartment_bearings(STRAIGHT_DEM, straight_compartment)

assert straight["baseline_bearing_deg"] == 180.0, (
    f"seed -> pinch runs due south on this fixture: {straight['baseline_bearing_deg']}"
)
for station in straight["stations"]:
    assert station["reproduces_record"], (
        f"{station['end']}: method A must reproduce the stored heights exactly, else the comparison "
        f"is not like-for-like -- stored {station['stored_heights']}"
    )
    assert station[METHOD_LOCAL]["bearing_deg"] == 180.0, (
        f"{station['end']}: the local channel also runs due south here, got "
        f"{station[METHOD_LOCAL]['bearing_deg']}"
    )
    assert station["angle_deg"] == 0.0, (
        f"{station['end']}: baseline and local channel coincide, so the angle is 0 -- got "
        f"{station['angle_deg']}"
    )
    for side in ("left_height_m", "right_height_m", "min_height_m"):
        assert station[METHOD_BASELINE][side] == station[METHOD_LOCAL][side], (
            f"{station['end']} {side}: identical bearings must give identical readings -- "
            f"A {station[METHOD_BASELINE][side]} vs B {station[METHOD_LOCAL][side]}"
        )
    assert station["classification"] == COMPARABLE
    # The hand-derived shoulder: every crest height on this fixture is
    # the 3.0 m rise, because base(r) cancels out of the subtraction.
    assert station[METHOD_LOCAL]["min_height_m"] == 3.0

straight_verdict = decide_verdict([straight])
assert straight_verdict["verdict"] == VERDICT_TERRAIN, (
    f"a straight valley must exonerate the bearing: {straight_verdict}"
)

print(
    f"1. Straight valley: baseline bearing {straight['baseline_bearing_deg']:.1f} deg equals the "
    f"local channel bearing at both stations, angle 0.00 deg, and A and B return the identical "
    f"3.00 m shoulder -- verdict {straight_verdict['verdict']}."
)


# =========================================================================
# 2 [2]. THE CURVED VALLEY: the artifact, reproduced from a known cause
# =========================================================================
# THE CHANNEL IS AN L, and the bend sits immediately downstream of the
# seed: south down col 8 from row 4 to row 10, then EAST along row 10
# from col 9 to col 37. Terrain is a V-valley wrapped around that
# polyline -- for each cell, the nearest channel cell p gives the
# along-channel base 100 - 0.30 * along(p), and the cross-profile is the
# straight fixture's: floor base + 0.5d, SHOULDER base + 3.0 at
# d = k(along(p)), then the same 2.6 m step off the crest. k is 4
# everywhere except along 26..29 (cols 28..31), the waist.
#
# THE SEED IS AT (4, 8) -- top of the SHORT south leg -- and the pinch
# lands at (10, 28), far down the LONG east leg. The baseline is
# therefore a chord across the bend:
#
#   seed -> pinch = (+20 cells east, -6 rows south) = (+100 m, -30 m)
#   bearing = atan2(100, -30) = 106.70 deg
#   local channel AT THE SEED = due south = 180.00 deg
#   ANGLE = 180.00 - 106.70 = 73.30 deg
#
# A 73 degree angle means A's transect at the seed runs at bearing
# 16.70 -- almost ALONG the north-south channel it is supposed to be
# crossing. The two numbers this fixture exists to state:
#
#   METHOD A at the seed:  left ABSENT (the ray walks UP-channel, where
#                          ground rises with the channel's own gradient
#                          and never falls back a full prominence, so it
#                          runs out the half-width bound), right 0.00 m
#                          (the ray walks DOWN-channel, ground falls
#                          immediately, and the crest is declared at the
#                          station itself).  MIN = 0.00 m.
#   METHOD B at the seed:  left 3.00 m, right 3.00 m -- the fixture's
#                          own hand-derived shoulder rise, recovered
#                          because the transect now crosses the valley.
#                          MIN = 3.00 m.
#
# AND AT THE PINCH, 100 m down the straight east leg, the angle falls to
# 16.70 deg and the two methods agree (A min 2.70 m vs B min 3.00 m,
# inside the 1.0 m margin). That half matters as much as the first: the
# instrument must NOT cry artifact wherever a baseline is used.
# =========================================================================

CURVED_SIZE = 44
_CURVED_CHANNEL = [(r, 8) for r in range(4, 11)] + [(10, c) for c in range(9, 38)]
_CURVED_ALONG = {cell: index for index, cell in enumerate(_CURVED_CHANNEL)}


def _curved_k(along):
    return 2 if 26 <= along <= 29 else 4


def _curved_array():
    array = np.zeros((CURVED_SIZE, CURVED_SIZE))
    for r in range(CURVED_SIZE):
        for c in range(CURVED_SIZE):
            distance, nearest = min(
                (math.hypot(r - p[0], c - p[1]), p) for p in _CURVED_CHANNEL
            )
            base = 100.0 - 0.30 * _CURVED_ALONG[nearest]
            k = _curved_k(_CURVED_ALONG[nearest])
            if distance <= k - 0.5:
                array[r, c] = base + 0.5 * distance
            elif distance <= k + 0.5:
                array[r, c] = base + 3.0
            else:
                array[r, c] = base + 3.0 - 2.6 - 0.05 * (distance - k - 1)
    return array


CURVED_DEM = _dem(_curved_array())
curved_compartment, _curved_walk = _build(CURVED_DEM, (4, 8), 138.0)
assert curved_compartment["pinch"]["rowcol"] == (10, 28), (
    f"the waist is at (10, 28): got {curved_compartment['pinch']['rowcol']}"
)
curved = compare_compartment_bearings(CURVED_DEM, curved_compartment)
curved_by_end = {station["end"]: station for station in curved["stations"]}

assert curved["baseline_bearing_deg"] == 106.7, (
    f"hand-derived chord bearing atan2(100, -30) = 106.70 deg, got "
    f"{curved['baseline_bearing_deg']}"
)

seed_station = curved_by_end["seed"]
assert seed_station["reproduces_record"], "method A must reproduce the stored heights"
assert seed_station[METHOD_LOCAL]["bearing_deg"] == 180.0, (
    "the local channel at the seed runs due south -- the secant must say so despite the bend "
    f"downstream: got {seed_station[METHOD_LOCAL]['bearing_deg']}"
)
assert seed_station["angle_deg"] == 73.3, (
    f"hand-derived 180.00 - 106.70 = 73.30 deg, got {seed_station['angle_deg']}"
)

# METHOD A: one flank absent, the other a crest declared at the station.
assert seed_station[METHOD_BASELINE]["left_height_m"] is None, (
    "A's up-channel ray climbs the channel's own gradient and never falls a full prominence -- "
    f"absent, got {seed_station[METHOD_BASELINE]['left_height_m']}"
)
assert seed_station[METHOD_BASELINE]["left"]["bound_hit"] is True
assert seed_station[METHOD_BASELINE]["right_height_m"] == 0.0, (
    "A's down-channel ray falls immediately, so the crest is the station itself -- 0.00 m, got "
    f"{seed_station[METHOD_BASELINE]['right_height_m']}"
)
assert tuple(seed_station[METHOD_BASELINE]["right"]["crest_rowcol"]) == (4, 8), (
    "and that 0.00 m must be the station cell, which is what makes it a reading rather than a bug"
)
assert seed_station[METHOD_BASELINE]["min_height_m"] == 0.0

# METHOD B: the hand-derived shoulder, both sides, and the absent flank
# RECOVERED.
assert seed_station[METHOD_LOCAL]["left_height_m"] == 3.0
assert seed_station[METHOD_LOCAL]["right_height_m"] == 3.0
assert seed_station[METHOD_LOCAL]["min_height_m"] == 3.0, (
    "crossing the valley instead of running down it recovers the fixture's own 3.0 m shoulder"
)
assert seed_station[METHOD_LOCAL]["left"]["bound_hit"] is False
assert seed_station["classification"] == DEEPER_UNDER_B

# AND THE PINCH, down the straight leg: the instrument does not cry
# artifact where the baseline is locally fine.
pinch_station = curved_by_end["pinch"]
assert pinch_station["angle_deg"] == 16.7, (
    f"hand-derived 106.70 - 90.00 = 16.70 deg, got {pinch_station['angle_deg']}"
)
assert pinch_station[METHOD_LOCAL]["bearing_deg"] == 90.0, "the east leg runs due east"
assert pinch_station[METHOD_LOCAL]["min_height_m"] == 3.0
assert pinch_station[METHOD_BASELINE]["min_height_m"] == 2.7
assert (
    abs(pinch_station[METHOD_LOCAL]["min_height_m"] - pinch_station[METHOD_BASELINE]["min_height_m"])
    <= VERDICT_MARGIN_METERS
)
assert pinch_station["classification"] == COMPARABLE, (
    "a 0.30 m difference is inside the pinned margin -- reporting that as an artifact would make "
    "the instrument useless"
)

print(
    f"2. Curved valley: at the seed the baseline runs {curved['baseline_bearing_deg']:.2f} deg "
    f"against a channel bearing {seed_station[METHOD_LOCAL]['bearing_deg']:.2f} deg -- ANGLE "
    f"{seed_station['angle_deg']:.2f} deg -- and A reads (absent, 0.00 m) where B reads "
    f"(3.00, 3.00) m: the artifact, reproduced from a known cause. At the pinch (angle "
    f"{pinch_station['angle_deg']:.2f} deg) the two agree, 2.70 vs 3.00 m."
)


# =========================================================================
# 3 [3]. THE ANGLE, HAND-CHECKED
# =========================================================================
# On unit vectors whose angle is arithmetic, and on the fixture's own
# bend above (where it is the difference of two compass bearings).
SOUTH, EAST, NORTH, WEST = (0.0, -1.0), (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0)
assert _angle_between_degrees(SOUTH, SOUTH) == 0.0
assert _angle_between_degrees(SOUTH, EAST) == 90.0
assert _angle_between_degrees(SOUTH, NORTH) == 180.0
assert _angle_between_degrees(EAST, WEST) == 180.0
# Symmetric, and unsigned: the instrument reports HOW FAR APART, not
# which way round, because a transect is a line and not a ray.
assert _angle_between_degrees(EAST, SOUTH) == _angle_between_degrees(SOUTH, EAST)
# A 3-4-5 triangle: atan2(4, 3) = 53.13 deg off due east.
_three_four_five = (3.0 / 5.0, 4.0 / 5.0)
assert _angle_between_degrees(EAST, _three_four_five) == 53.13
# And the fixture's chord, re-derived independently of the module.
_chord = (100.0, -30.0)
_chord_unit = (_chord[0] / math.hypot(*_chord), _chord[1] / math.hypot(*_chord))
assert _angle_between_degrees(_chord_unit, SOUTH) == 73.3
assert round(math.degrees(math.atan2(100.0, 30.0)), 2) == 73.3, (
    "the same 73.30 deg from plain trigonometry, so the assertion above is not circular"
)

print(
    "3. Angle: 0 / 90 / 180 deg on cardinal pairs, 53.13 deg on a 3-4-5, symmetric and unsigned, "
    "and the curved fixture's 73.30 deg chord angle re-derived from plain trigonometry."
)


# =========================================================================
# 4 [4]. ABSENT SEMANTICS SURVIVE METHOD B
# =========================================================================
# Method B routes through the SAME crest_height_above_channel() and
# lower_crest_height() the field uses, so the sentinel rules must hold
# unchanged on its readings: a bound-hit flank is None and never 0.0,
# and a measured 0.0 -- a crest confirmed at the station itself -- is a
# real reading that WINS the binding reduction against a taller side.
_all_stations = list(straight["stations"]) + list(curved["stations"])
_absent = 0
_measured_zero = 0
for _station in _all_stations:
    for _method in (METHOD_BASELINE, METHOD_LOCAL):
        _data = _station[_method]
        for _side in ("left", "right"):
            _height = _data[f"{_side}_height_m"]
            _walk = _data[_side]
            if _walk["bound_hit"]:
                assert _height is None, (
                    f"a bound-hit flank has no crest height -- None, never {_height!r}"
                )
                _absent += 1
            else:
                assert isinstance(_height, float), "a confirmed crest yields a float"
                if _height == 0.0:
                    _measured_zero += 1
                    assert tuple(_walk["crest_rowcol"]) == _station["rowcol"], (
                        "a 0.0 must be a crest at the station cell, or it is not this mechanism"
                    )
        # The binding side is the LOWER of the MEASURED sides, never a
        # missing one -- and a measured 0.0 competes and wins.
        _measured = [
            _data[f"{_s}_height_m"] for _s in ("left", "right") if _data[f"{_s}_height_m"] is not None
        ]
        assert _data["min_height_m"] == (min(_measured) if _measured else None)

assert _absent > 0 and _measured_zero > 0, (
    f"both sentinels must actually be exercised here: {_absent} absent, {_measured_zero} measured zero"
)
# The headline case, restated on the fixture that produces it: absent
# beside a measured 0.0, and the 0.0 is the binding side.
assert curved_by_end["seed"][METHOD_BASELINE]["left_height_m"] is None
assert curved_by_end["seed"][METHOD_BASELINE]["min_height_m"] == 0.0

print(
    f"4. Absent semantics in B: across {len(_all_stations)} station(s) x 2 methods x 2 sides, every "
    f"bound-hit flank is None ({_absent} of them) and every measured 0.0 ({_measured_zero}) is a "
    "crest at its own station cell that wins the binding reduction."
)


# =========================================================================
# 5 [5]. DIAGNOSTIC-ONLY
# =========================================================================
# THE PIN. Method B must not leak into a production path: no production
# module may import this instrument, and none may call the secant it
# borrows. "Production" here is every module that is neither a
# diagnose_* instrument nor a test.
_REPO = pathlib.Path(__file__).resolve().parent
_PRODUCTION = [
    path
    for path in sorted(_REPO.rglob("*.py"))
    if not path.name.startswith(("diagnose_", "test_", "_"))
]
assert len(_PRODUCTION) > 20, f"the production sweep must actually find modules: {len(_PRODUCTION)}"

_importers = []
for _path in _PRODUCTION:
    _tree = ast.parse(_path.read_text())
    for _node in ast.walk(_tree):
        if isinstance(_node, ast.Import):
            for _alias in _node.names:
                if _alias.name == "diagnose_transect_bearing":
                    _importers.append(_path.name)
        elif isinstance(_node, ast.ImportFrom):
            if _node.module == "diagnose_transect_bearing":
                _importers.append(_path.name)

assert not _importers, (
    f"diagnose_transect_bearing is diagnostic-only -- imported by production module(s) {_importers}"
)

# TWO ASSERTIONS ARE RETIRED HERE, DELIBERATELY AND NOT SILENTLY.
#
# This section used to assert two more things: that no production module
# imports local_stem_direction, and that the string "local_stem_direction"
# appears nowhere in water_survey_areas.py. Both were pinning the secant
# as a DIAGNOSTIC-ONLY path, which was the correct pin while the only
# thing that had been measured with it was this module's method B.
#
# THE SECANT HAS SINCE SHIPPED, on the PINCH WALK: walk_embankment_pinch()
# measures every width perpendicular to local_stem_direction()'s
# de-quantized secant instead of the raw D8 step. So water_survey_areas
# imports it, and those two assertions would now fail on a change that is
# correct. Retiring them is the branch's decision, recorded here so a
# reader does not have to reconstruct it from a diff.
#
# WHAT IS NOT RETIRED, and why the distinction matters: the TRANSECT
# bearing is untouched. build_embankment_compartment() still takes its
# crest walks perpendicular to the seed->pinch BASELINE, which is what
# this whole module's A/B compares, and the assertion above -- that no
# production module imports this INSTRUMENT -- still holds and still
# means what it meant. The pinch walk's bearing and the transects'
# bearing are two different questions; only the first has been answered.
_wsa_source = (_REPO / "water_survey_areas.py").read_text()
assert "local_stem_direction" in _wsa_source, (
    "the secant has shipped onto the pinch walk -- if this fails, either the bearing change was "
    "reverted (in which case restore the diagnostic-only pins above) or the reuse was replaced by "
    "a local reimplementation"
)
# And it is REUSED, not copied: still defined exactly once, in the module
# that owns it.
_secant_definitions = [
    f"{_path.name}:{_node.lineno}"
    for _path in sorted(_REPO.rglob("*.py"))
    for _node in ast.walk(ast.parse(_path.read_text()))
    if isinstance(_node, (ast.FunctionDef, ast.AsyncFunctionDef))
    and _node.name == "local_stem_direction"
]
assert _secant_definitions == ["valley_level_pool.py:436"] or len(_secant_definitions) == 1, (
    f"local_stem_direction must stay defined exactly once repo-wide: {_secant_definitions}"
)

# THE TRANSECT bearing itself, pinned directly rather than by proxy: the
# compartment's transects are still built off the baseline perpendicular.
assert "perpendicular = (-baseline_unit[1], baseline_unit[0])" in _wsa_source, (
    "the production TRANSECT bearing is unchanged -- changing it is a different branch, and this "
    "module's A/B is what would justify it"
)

# THE REUSE PIN, the same shape the crest-height branch uses: the
# instrument computes no crest walk and no direction estimator of its
# own. Every function it defines is a comparison, a formatter, or the
# verdict rule.
_instrument = ast.parse((_REPO / "diagnose_transect_bearing.py").read_text())
_defined = {
    node.name
    for node in ast.walk(_instrument)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}
for _forbidden in ("ridge_crest_walk", "measure_valley_width", "local_stem_direction",
                   "crest_height_above_channel", "lower_crest_height", "bearing_degrees"):
    assert _forbidden not in _defined, (
        f"the instrument must REUSE {_forbidden}, never redefine it: {sorted(_defined)}"
    )
# And it does in fact call the borrowed ones.
_called = {
    node.func.id
    for node in ast.walk(_instrument)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
}
for _required in ("ridge_crest_walk", "local_stem_direction", "crest_height_above_channel",
                  "lower_crest_height", "bearing_degrees"):
    assert _required in _called, f"the instrument must actually call {_required}"

print(
    f"5. Diagnostic-only: across {len(_PRODUCTION)} production module(s), none imports the "
    "INSTRUMENT, and it redefines none of the six borrowed functions while calling five of them. "
    "The two pins that made the SECANT diagnostic-only are RETIRED here (it has shipped onto the "
    f"pinch walk, defined once at {_secant_definitions[0]}); the TRANSECT bearing stays pinned to "
    "the baseline perpendicular."
)


# =========================================================================
# 6. THE VERDICT RULE, PINNED
# =========================================================================
# Applied to synthetic classification sets so the thresholds themselves
# are tested rather than only the one outcome a fixture happens to give.


def _fake(classifications):
    return [
        {
            "zone_id": 0,
            "stations": [
                {"end": f"s{i}", "classification": value} for i, value in enumerate(classifications)
            ],
        }
    ]


# A strict MAJORITY is required, so a tie is not enough.
assert decide_verdict(_fake([DEEPER_UNDER_B] * 3 + [COMPARABLE]))["verdict"] == VERDICT_BEARING_ARTIFACT
assert decide_verdict(_fake([DEEPER_UNDER_B, DEEPER_UNDER_B, COMPARABLE, COMPARABLE]))["verdict"] == (
    VERDICT_MIXED
), "2 of 4 is not a majority either way -- mixed, not forced"
assert decide_verdict(_fake([COMPARABLE] * 3 + [DEEPER_UNDER_B]))["verdict"] == VERDICT_TERRAIN
assert decide_verdict(
    _fake([DEEPER_UNDER_B, DEEPER_UNDER_A, COMPARABLE, NEITHER_MEASURED])
)["verdict"] == VERDICT_MIXED
assert decide_verdict([])["verdict"] == VERDICT_MIXED

# The margin is the prominence, and it is exclusive: a difference of
# exactly the threshold is NOT material.
assert dtb._classify(1.0, 1.0 + VERDICT_MARGIN_METERS) == COMPARABLE
assert dtb._classify(1.0, 1.0 + VERDICT_MARGIN_METERS + 0.01) == DEEPER_UNDER_B
assert dtb._classify(1.0 + VERDICT_MARGIN_METERS + 0.01, 1.0) == DEEPER_UNDER_A
assert VERDICT_MARGIN_METERS == RIDGE_PROMINENCE_METERS, (
    "the margin is deliberately the crest walk's own prominence, not a new tunable"
)
# Absence is a result: recovered by B counts for B, lost under B counts
# against, neither measured is set aside.
assert dtb._classify(None, 0.2) == DEEPER_UNDER_B, (
    "B measuring where A found nothing is the strongest single sign of the artifact, however small "
    "the recovered depth"
)
assert dtb._classify(0.2, None) == DEEPER_UNDER_A
assert dtb._classify(None, None) == NEITHER_MEASURED

print(
    f"6. Verdict rule: strict majority (2 of 4 reports MIXED, not a forced call), margin pinned to "
    f"RIDGE_PROMINENCE_METERS = {VERDICT_MARGIN_METERS} m and exclusive at the boundary, and "
    "absence counts as a result for whichever method resolved it."
)

# =========================================================================
# 7. THE -0.00 READING: MECHANISM CONFIRMED, AND A DEFECT BEHIND IT
# =========================================================================
# The reference run reports a binding depth of -0.00 m. Two questions,
# and the answers are different: WHY IS THERE A ZERO, and WHY IS IT
# NEGATIVE.
#
# THE ZERO IS CORRECT. ridge_crest_walk() seeds its running maximum with
# the START cell, so ground falling a full prominence immediately off
# the station declares the crest AT the station -- crest_rowcol EQUALS
# the station's rowcol -- and the height is genuinely zero: no shoulder
# above the channel on that side at all.
#
# THE MINUS SIGN IS A DEFECT, in the field this branch's parent commit
# added. crest_height_above_channel() subtracts the UNROUNDED raw
# channel elevation from ridge_crest_walk()'s ALREADY-ROUNDED
# crest_elevation_m. Where the crest IS the station, both come from one
# raw value e, and the field computes round(round(e, 2) - e, 2) instead
# of 0 -- up to +/-0.005 m, signed either way. The same +/-5 mm rides
# EVERY crest height the field reports.
#
# THE FIXTURE, hand-built to produce it: the cliff strip, with the
# station cell moved off a 2-decimal value. Walking +x from col 3:
# 20.0049 (the station), 18.5 (a 1.5 m drop, past the 1.0 m prominence
# at the FIRST sample), then falling. round(20.0049, 2) = 20.0, and
# round(20.0 - 20.0049, 2) = -0.0.
_ROUNDING_STRIP = _dem(
    np.tile(np.array([21.5, 23.0, 21.0, 20.0049, 18.5, 18.4, 18.3, 18.2]), (3, 1))
)
_station = (1, 3)
_station_xy = pixel_center_xy(_ROUNDING_STRIP, *_station)
_over_the_edge = wsa.ridge_crest_walk(_ROUNDING_STRIP, _station_xy, (1.0, 0.0))

# The zero's mechanism, confirmed: the crest IS the station cell.
assert _over_the_edge["bound_hit"] is False
assert _over_the_edge["half_width_m"] == 0.0
assert tuple(_over_the_edge["crest_rowcol"]) == _station, (
    "the crest must be the station itself -- that is what makes the zero a reading and not a bug"
)

_stored = wsa.crest_height_above_channel(_ROUNDING_STRIP, _over_the_edge, _station)
_raw = float(_ROUNDING_STRIP["array"][tuple(_over_the_edge["crest_rowcol"])]) - float(
    _ROUNDING_STRIP["array"][_station]
)
assert _raw == 0.0, "the honest difference of a cell with itself is exactly zero"

# SINCE FIXED, ON THE BOUND BRANCH, and this section is kept as the
# defect's record rather than rewritten away. What it used to assert
# here was that the field REPRODUCED the reference run's -0.00:
#
#     assert dtb._negative_zero(_stored)
#     assert repr(_stored) == "-0.0"
#
# crest_height_above_channel() now re-reads the crest elevation raw at
# crest_rowcol instead of building on ridge_crest_walk()'s already
# rounded crest_elevation_m, so the subtraction rounds once and a
# coincident crest is exactly 0.0. The diagnosis above is unchanged and
# still worth reading; only the last line of it has moved from "is" to
# "was". (The fix and its own regression guard live in
# test_crest_bound_150.py section 5.)
assert _stored == 0.0
assert not dtb._negative_zero(_stored), (
    f"the sign is gone: a coincident crest reads exactly 0.0, not -0.0 -- got {_stored!r}"
)
assert repr(_stored) == "0.0", f"fixed: {_stored!r}"
assert _stored == _raw, "and the stored value IS the raw difference, rounded once"

# THE OLD ARITHMETIC, reproduced explicitly, so the magnitude this
# section describes stays checkable and the audit below still has
# something to catch.
_old_arithmetic = round(
    _over_the_edge["crest_elevation_m"] - float(_ROUNDING_STRIP["array"][_station]), 2
)
assert dtb._negative_zero(_old_arithmetic), (
    f"the defect, as it was: {_old_arithmetic!r} where the truth is {_raw!r}"
)
# Its magnitude: a half-centimetre, which is why it changed no verdict --
# the comparison margin is 1.0 m.
assert abs(_old_arithmetic - _raw) < 0.005
assert VERDICT_MARGIN_METERS > 100 * abs(_old_arithmetic - _raw)

# And the audit must still catch it. (It cannot be caught by an
# equality test: -0.0 == 0.0 is True, which is the trap.)
assert (-0.0) == 0.0, "the trap itself, stated so the next reader does not re-lay it"
_stored = _old_arithmetic  # the audit below is fed the DEFECT, deliberately
_fake_comparison = [
    {
        "zone_id": 0,
        "stations": [
            {
                "end": "pinch",
                "rowcol": _station,
                METHOD_BASELINE: {
                    "left": _over_the_edge,
                    "right": {"crest_rowcol": None, "bound_hit": True},
                    "left_height_m": _stored,
                    "right_height_m": None,
                },
                METHOD_LOCAL: {
                    "left": _over_the_edge,
                    "right": {"crest_rowcol": None, "bound_hit": True},
                    "left_height_m": _stored,
                    "right_height_m": None,
                },
            }
        ],
    }
]
_audit = dtb._rounding_audit(_ROUNDING_STRIP, _fake_comparison)
assert _audit["measured"] == 2 and _audit["mismatched"] == 2, (
    f"the audit must flag both readings as disagreeing with the raw difference: {_audit}"
)
assert _audit["negative_zeros"] == 2, f"and must name them as negative zeros: {_audit}"

# THE CONTROL: a station whose elevation lands ON a 2-decimal value
# produces the same zero WITHOUT the sign, so the audit is detecting the
# rounding and not merely the coincident crest.
_CLEAN_STRIP = _dem(np.tile(np.array([21.5, 23.0, 21.0, 20.0, 18.5, 18.4, 18.3, 18.2]), (3, 1)))
_clean_walk = wsa.ridge_crest_walk(_CLEAN_STRIP, pixel_center_xy(_CLEAN_STRIP, *_station), (1.0, 0.0))
_clean = wsa.crest_height_above_channel(_CLEAN_STRIP, _clean_walk, _station)
assert tuple(_clean_walk["crest_rowcol"]) == _station and _clean == 0.0
assert not dtb._negative_zero(_clean), "a clean elevation gives a clean +0.0"
_clean_comparison = [
    {
        "zone_id": 0,
        "stations": [
            {
                "end": "pinch",
                "rowcol": _station,
                METHOD_BASELINE: {
                    "left": _clean_walk,
                    "right": {"crest_rowcol": None, "bound_hit": True},
                    "left_height_m": _clean,
                    "right_height_m": None,
                },
                METHOD_LOCAL: {
                    "left": _clean_walk,
                    "right": {"crest_rowcol": None, "bound_hit": True},
                    "left_height_m": _clean,
                    "right_height_m": None,
                },
            }
        ],
    }
]
assert dtb._rounding_audit(_CLEAN_STRIP, _clean_comparison)["mismatched"] == 0, (
    "the audit must stay silent where there is nothing wrong, or it is not evidence of anything"
)

# AND THE FIELD ITSELF IS FIXED, asserted at the source so this section
# cannot go back to passing because the defect returned. The old form
# subtracted from the walk's rounded crest_elevation_m; the new one
# re-reads the crest cell raw.
_WSA_SOURCE = (_REPO / "water_survey_areas.py").read_text()
assert "round(crest_elevation - channel_elevation, 2)" in _WSA_SOURCE, (
    "the subtraction still rounds once, at the end"
)
assert 'crest_elevation = float(dem["array"][crest_rowcol[0], crest_rowcol[1]])' in _WSA_SOURCE, (
    "and the crest elevation is read RAW at crest_rowcol -- if this line goes, the double "
    "rounding is back"
)

print(
    f"7. The -0.00: mechanism CONFIRMED (crest_rowcol {tuple(_over_the_edge['crest_rowcol'])} == "
    f"station {_station}, so the zero correctly means no shoulder at all), and the MINUS SIGN it "
    f"used to carry is GONE -- the field now reads {_over_the_edge['crest_rowcol']} raw and returns "
    f"exactly 0.0. The old arithmetic is reproduced here as {_old_arithmetic!r} so the audit still "
    "has a defect to catch, and it catches it."
)

print("\nAll transect-bearing diagnostic checks passed.")
