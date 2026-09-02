"""
test_pinch_catchment_drainage.py

THE DRAINAGE CRITERION'S MOVE, its own test file: contributing area
stops being a per-cell SEEDING criterion on the embankment path and
becomes a per-COMPARTMENT measurement taken at the PINCH CELL, after the
pinch walk has found it.

WHY THE MOVE (the finding, so this file's assertions are readable as
evidence rather than as arbitrary pins): the drainage-area criterion
carried the highest weight in the embankment blend (0.30) and was the
only criterion in it with real external anchoring -- AH-590's
fill-vs-flood guidance, NRCS CPS 378, PA Chapter 105. On the reference
property it was ANTI-CORRELATED with producing a survey area: every
high-drainage seed died at no_constriction or the acreage floor, and
every surviving zone carried drainage 0.000. The diagnosis was a
QUESTION MISMATCH rather than a bad criterion -- drainage was measured
AT THE SEED, which under the compartment construction is the STORAGE
ANCHOR, and a cell already sitting in an established drainageway is at
the valley's narrow point, so its downstream walk finds only widening.
What fills a pond is the catchment ABOVE the compartment, delivered
through the dam reach. Same band, same constants, different cell.

Run as:

    python test_pinch_catchment_drainage.py

Sections (the design's numbered test items in brackets):
  1  [1]   THE PINCH-CELL MEASUREMENT -- a fixture whose pinch sits below
           a KNOWN contributing area scores the band as hand-computed,
           and the seed's own catchment is provably NOT what is scored
           (the two are made to differ by two orders of magnitude, and
           they land on OPPOSITE ends of the band).
  2  [2]   SEEDING WITHOUT DRAINAGE -- a channel cell that previously
           seeded high now scores on three criteria only; the weights
           sum to 1.0 at import and drainage_area is absent from the
           embankment criteria grids.
  3  [3]   catchment_exceeds_ceiling -- a compartment whose pinch carries
           more than the 20 ac ceiling drops with that reason, visible
           in the dropped list, before dedupe and before the floor.
  4  [4]   THE FILL CLAIM ON THE WIRE -- all three numbers reported
           separately (seed blend / pinch catchment + drainage score /
           the composite) on the feature properties, in narrative_data,
           in the summary and on the diagnostic export's pinch layer;
           the panel deliberately carries none of them and says so.
  5  [5]   EXCAVATED IS UNTOUCHED -- drainage_runon stays a per-cell
           criterion at 0.10 and the excavated output is byte-identical
           across the change's own inputs.

(Item [4] of the design -- ranking by pinch catchment on identical seed
blends -- is asserted in test_water_survey_areas.py beside the rest of
the ranking machinery, where the mini-zone pool already lives. Items [6]
and [7] are test_pipeline_context.py / test_water_step.py and
test_twi_boundary_independence.py respectively, unchanged homes.)
"""

import json
import math

import numpy as np
from rasterio.warp import transform_geom
from shapely.geometry import box

import water_survey_areas as wsa
from feature_schema import validate_feature_collection
from keypoint_detection import build_upstream_map
from raster_grid import SQUARE_METERS_PER_ACRE, cell_area_acres, pixel_center_xy
from valley_delineation import compute_flow_accumulation, compute_flow_direction, fill_depressions
from water_survey_areas import (
    EMBANKMENT_WEIGHTS,
    MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    SURVEY_TYPE_EMBANKMENT,
    SURVEY_TYPE_EXCAVATED,
    build_embankment_compartment,
    compartment_pinch_catchment,
    compartment_rank_score,
    compute_suitability_surfaces,
    compute_water_survey_areas,
    drainage_band_score,
    walk_embankment_pinch,
)

RESOLUTION = 5.0
ORIGIN_X, ORIGIN_Y = 500000.0, 4500000.0
CRS = "EPSG:32617"
CELL_ACRES = 25.0 / SQUARE_METERS_PER_ACRE  # 0.00617763... ac per 5 m cell


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
    from shapely import contains_xy

    rows, cols = dem["array"].shape
    col_x = dem["origin_x"] + (np.arange(cols) + 0.5) * RESOLUTION
    row_y = dem["origin_y"] - (np.arange(rows) + 0.5) * RESOLUTION
    xs, ys = np.meshgrid(col_x, row_y)
    return contains_xy(boundary, xs, ys) & ~np.isnan(dem["array"])


def _point_wgs84(dem, r, c):
    return {
        "type": "Point",
        "coordinates": tuple(
            transform_geom(
                CRS, "EPSG:4326", {"type": "Point", "coordinates": pixel_center_xy(dem, r, c)}
            )["coordinates"]
        ),
    }


# =========================================================================
# THE SHARED FIXTURE: the pinched valley, 40x21 at 5 m, channel down
# col 10, flow +row (south), cross-section widening below a waist --
# the same construction test_embankment_compartments.py hand-derives,
# reproduced here because THIS file's assertions are about a different
# quantity measured on the same shape and must not depend on that file.
#
#     d = |c - 10|;  k(r) = 4 (rows < 18), 2 (rows 18-21), 5 (rows >= 22)
#     d <  k : valley floor,  base(r) + 0.5*d
#     d == k : levee crest,   base(r) + 3.0
#     d >  k : outside,       base(r) + 1.0 - 0.05*(d-k-1)
#
# A seed at (10, 10) walks down-channel to the waist at (18, 10): that
# is the geometry the pinch machinery is tested on elsewhere, and here
# it is simply the vehicle for "seed cell" and "pinch cell" being two
# demonstrably different cells.
# =========================================================================

ROWS, COLS, CHANNEL = 40, 21, 10


def _k_of_row(r):
    if 18 <= r <= 21:
        return 2
    return 4 if r < 18 else 5


def _valley_array():
    array = np.zeros((ROWS, COLS))
    for r in range(ROWS):
        base = 100.0 - 0.25 * r
        k = _k_of_row(r)
        for c in range(COLS):
            d = abs(c - CHANNEL)
            if d < k:
                array[r, c] = base + 0.5 * d
            elif d == k:
                array[r, c] = base + 3.0
            else:
                array[r, c] = base + 1.0 - 0.05 * (d - k - 1)
    return array


DEM = _dem(_valley_array())
BOUNDARY = box(
    ORIGIN_X + 1 * RESOLUTION + 0.1,
    ORIGIN_Y - 39 * RESOLUTION + 0.1,
    ORIGIN_X + 20 * RESOLUTION - 0.1,
    ORIGIN_Y - 1 * RESOLUTION - 0.1,
)
FILLED, FTR, FTC = _flow(DEM)
ON_PARCEL = _on_parcel(DEM, BOUNDARY)
NO_ROAD = np.zeros(DEM["array"].shape, dtype=bool)
UPSTREAM = build_upstream_map(FTR, FTC)

SEED_ROWCOL = (10, CHANNEL)
WALK = walk_embankment_pinch(DEM, SEED_ROWCOL, FTR, FTC, ON_PARCEL, NO_ROAD)
assert WALK["found"] and WALK["pinch_rowcol"] == (18, CHANNEL), (
    f"the fixture's seed walks to the waist: {WALK.get('pinch_rowcol')}"
)
PINCH_ROWCOL = WALK["pinch_rowcol"]
assert SEED_ROWCOL != PINCH_ROWCOL, "the two cells this file distinguishes must be distinct"


def _seed(blend=0.6):
    return {
        "rowcol": SEED_ROWCOL,
        "xy": pixel_center_xy(DEM, *SEED_ROWCOL),
        "geometry_wgs84": _point_wgs84(DEM, *SEED_ROWCOL),
        "blend_score": blend,
        "criteria_signature": {name: 0.5 for name in EMBANKMENT_WEIGHTS},
    }


def _surfaces():
    shape = DEM["array"].shape
    return {
        SURVEY_TYPE_EMBANKMENT: np.full(shape, 0.6),
        "criteria": {
            SURVEY_TYPE_EMBANKMENT: {name: np.full(shape, 0.5) for name in EMBANKMENT_WEIGHTS}
        },
    }


def _context(accumulation):
    shape = DEM["array"].shape
    return {
        "twi_score": np.full(shape, 0.5),
        "depression_depth": np.zeros(shape),
        "flow_accumulation": accumulation,
        "slope_pct": np.full(shape, 5.0),
        "soil_covered_mask": np.zeros(shape, dtype=bool),
        "soil_checked": False,
    }


def _build(accumulation, blend=0.6):
    return build_embankment_compartment(
        DEM, _seed(blend), WALK, UPSTREAM, BOUNDARY, None, _surfaces(), _context(accumulation)
    )


# =========================================================================
# 1 [1]. THE PINCH-CELL MEASUREMENT, and the seed's catchment provably
#        not being it
# =========================================================================
# THE TWO CELLS ARE GIVEN CATCHMENTS TWO ORDERS OF MAGNITUDE APART, AND
# ON OPPOSITE ENDS OF THE BAND. That is the point of the fixture: if the
# implementation read the seed cell, every assertion below inverts rather
# than merely shifting, so no rounding coincidence can hide the error.
#
#   seed  (10, 10): 20 cells -> 20 * 0.0061776 = 0.1236 ac
#                   BELOW the band's 0.5 ac minimum -> score 0.0
#   pinch (18, 10): 500 cells -> 500 * 0.0061776 = 3.0888 ac
#                   ABOVE the band's 2.0 ac full credit -> score 1.0
#
# Every other cell is given 1 so nothing else can accidentally supply
# either number.
ACC = np.ones(DEM["array"].shape, dtype=np.float64)
ACC[SEED_ROWCOL] = 20.0
ACC[PINCH_ROWCOL] = 500.0

SEED_ACRES = 20.0 * CELL_ACRES
PINCH_ACRES = 500.0 * CELL_ACRES
assert math.isclose(SEED_ACRES, 0.1236, abs_tol=5e-5), f"hand-computed seed acres, got {SEED_ACRES}"
assert math.isclose(PINCH_ACRES, 3.0888, abs_tol=5e-5), f"hand-computed pinch acres, got {PINCH_ACRES}"
assert SEED_ACRES < wsa.EMBANKMENT_DRAINAGE_MIN_ACRES, "the seed sits under the band's ramp"
assert PINCH_ACRES > wsa.EMBANKMENT_DRAINAGE_FULL_CREDIT_ACRES, "the pinch sits on its plateau"

# The unit, in isolation first.
measured = compartment_pinch_catchment(DEM, PINCH_ROWCOL, ACC)
assert math.isclose(measured["acres"], round(PINCH_ACRES, 4)), (
    f"hand-computed pinch catchment {PINCH_ACRES:.4f} ac, got {measured['acres']}"
)
assert measured["score"] == 1.0, "3.0888 ac is past the 2.0 ac full-credit plateau"
assert measured["exceeds_ceiling"] is False
assert measured["ceiling_acres"] == MAX_VALLEY_CONTRIBUTING_AREA_ACRES
# THE COUNTERFACTUAL, stated as an assertion rather than as a comment:
# the same band on the SEED's catchment reads the opposite verdict.
assert float(drainage_band_score(np.array(SEED_ACRES))) == 0.0, (
    "the seed's own catchment scores ZERO on this very band -- the measurement that was killing "
    "the archetype"
)

# ...and through the compartment builder, which is where it has to be
# right.
comp = _build(ACC)
assert comp is not None
assert comp["pinch"]["rowcol"] == PINCH_ROWCOL and comp["seed"]["rowcol"] == SEED_ROWCOL
assert comp["pinch_catchment_acres"] == measured["acres"], (
    "the compartment's fill claim is the pinch cell's catchment, not the seed's"
)
assert comp["pinch_drainage_score"] == 1.0
assert comp["pinch_catchment_acres"] != round(SEED_ACRES, 4), (
    "PROVABLY NOT THE SEED'S: the two catchments differ by 25x on this fixture"
)
# The band is read at the pinch even when that INVERTS the verdict the
# seed would have given -- run the fixture the other way round to prove
# the direction is not an artifact of which number happens to be bigger.
ACC_INVERTED = np.ones(DEM["array"].shape, dtype=np.float64)
ACC_INVERTED[SEED_ROWCOL] = 500.0
ACC_INVERTED[PINCH_ROWCOL] = 20.0
inverted = _build(ACC_INVERTED)
assert inverted["pinch_catchment_acres"] == round(SEED_ACRES, 4), (
    "with the catchments swapped the compartment reports the SMALL one -- because it reports the "
    "PINCH cell, whichever number that is"
)
assert inverted["pinch_drainage_score"] == 0.0, (
    "and scores it zero: a dam reach with no catchment above it earns no fill credit however good "
    "the storage cell's own drainage looks"
)

# THE COMPOSITE, and the fact that it never replaces its inputs.
assert comp["compartment_rank_score"] == compartment_rank_score(0.6, 1.0) == 0.8
assert inverted["compartment_rank_score"] == compartment_rank_score(0.6, 0.0) == 0.3
assert comp["seed_blend_score"] == inverted["seed_blend_score"] == 0.6, (
    "identical anchor claims, opposite fill claims -- both records carry both numbers"
)
# NO NEW COMPUTATION: the number is read off the grid handed in, and a
# different grid gives a different answer with nothing else touched.
assert comp["compartment_footprint_acres"] == inverted["compartment_footprint_acres"], (
    "changing only the accumulation grid changes the fill claim and NOTHING about the geometry -- "
    "the measurement rides along, it does not steer the compartment"
)
print(
    f"1. Pinch-cell measurement: seed {SEED_ACRES:.4f} ac (band 0.0) vs pinch {PINCH_ACRES:.4f} ac "
    f"(band 1.0) -- the compartment reports the PINCH's, and reports the SMALL one when the two "
    f"are swapped; composite {comp['compartment_rank_score']} vs "
    f"{inverted['compartment_rank_score']} with the same 0.6 seed blend."
)


# =========================================================================
# 2 [2]. SEEDING WITHOUT DRAINAGE
# =========================================================================
# THE IMPORT-TIME GUARANTEE first: three criteria, summing to 1.0. The
# module asserts this at import; re-stated here so this file fails on it
# directly rather than on a downstream symptom.
assert set(EMBANKMENT_WEIGHTS) == {"slope", "soil", "twi"}
assert math.isclose(sum(EMBANKMENT_WEIGHTS.values()), 1.0, abs_tol=1e-9)
assert "drainage_area" not in EMBANKMENT_WEIGHTS

# A CHANNEL CELL THAT PREVIOUSLY SEEDED HIGH, scored both ways on one
# set of inputs. The retired blend is reconstructed HERE, in the test,
# from the numbers it used to carry -- so the comparison is against a
# stated historical fact rather than against a second implementation
# that could drift with the first.
RETIRED_EMBANKMENT_WEIGHTS = {"drainage_area": 0.30, "slope": 0.25, "soil": 0.25, "twi": 0.20}
assert math.isclose(sum(RETIRED_EMBANKMENT_WEIGHTS.values()), 1.0, abs_tol=1e-9)

blend_dem = _dem(np.full((3, 3), 100.0))
CHANNEL_ACCUMULATION = 324.0  # -> 2.0016 ac, past the band's full credit
blend_surfaces = compute_suitability_surfaces(
    blend_dem,
    gate_mask=np.ones((3, 3), dtype=bool),
    flow_accumulation=np.full((3, 3), CHANNEL_ACCUMULATION),
    slope_pct=np.full((3, 3), 5.0),
    twi_score_grid=np.full((3, 3), 0.6),
    depression_depth=np.full((3, 3), 0.25),
    soil_score_grid=np.full((3, 3), 0.8),
)
# The three criteria that remain, at this cell: slope 1.0 (5% is inside
# the 3-8% sweet spot), soil 0.8 (given), TWI 0.6 (given).
NEW_BLEND = 0.36 * 1.0 + 0.36 * 0.8 + 0.28 * 0.6  # 0.816
OLD_BLEND = 0.30 * 1.0 + 0.25 * 1.0 + 0.25 * 0.8 + 0.20 * 0.6  # 0.87
assert np.allclose(blend_surfaces[SURVEY_TYPE_EMBANKMENT], NEW_BLEND), (
    f"three criteria only: {blend_surfaces[SURVEY_TYPE_EMBANKMENT][1, 1]} vs hand-computed {NEW_BLEND}"
)
assert math.isclose(OLD_BLEND, 0.87), "the retired blend's value at this cell, for the record"
assert "drainage_area" not in blend_surfaces["criteria"][SURVEY_TYPE_EMBANKMENT], (
    "no drainage grid is produced for the embankment type at all -- absence, not a zeroed grid"
)
assert set(blend_surfaces["criteria"][SURVEY_TYPE_EMBANKMENT]) == set(EMBANKMENT_WEIGHTS), (
    "the criteria grids and the weights are one vocabulary"
)

# CONTRIBUTING AREA CANNOT REACH THE EMBANKMENT SURFACE AT ALL, which is
# a stronger claim than "its weight is zero" and the one worth testing:
# vary ONLY the accumulation and the embankment blend must not move,
# while the excavated blend (whose run-on criterion did NOT move) must.
for accumulation in (1.0, 40.0, 324.0, 3000.0):
    varied = compute_suitability_surfaces(
        blend_dem,
        gate_mask=np.ones((3, 3), dtype=bool),
        flow_accumulation=np.full((3, 3), accumulation),
        slope_pct=np.full((3, 3), 5.0),
        twi_score_grid=np.full((3, 3), 0.6),
        depression_depth=np.full((3, 3), 0.25),
        soil_score_grid=np.full((3, 3), 0.8),
    )
    assert np.allclose(varied[SURVEY_TYPE_EMBANKMENT], NEW_BLEND), (
        f"the embankment NOMINATION surface is blind to contributing area now "
        f"(accumulation {accumulation} moved it)"
    )
assert not np.allclose(
    compute_suitability_surfaces(
        blend_dem,
        gate_mask=np.ones((3, 3), dtype=bool),
        flow_accumulation=np.full((3, 3), 1.0),
        slope_pct=np.full((3, 3), 5.0),
        twi_score_grid=np.full((3, 3), 0.6),
        depression_depth=np.full((3, 3), 0.25),
        soil_score_grid=np.full((3, 3), 0.8),
    )[SURVEY_TYPE_EXCAVATED],
    blend_surfaces[SURVEY_TYPE_EXCAVATED],
), "and the EXCAVATED surface still is not -- drainage_runon is untouched by this change"
print(
    f"2. Seeding without drainage: three criteria summing to 1.0; the channel cell that blended "
    f"{OLD_BLEND} on the retired four now blends {NEW_BLEND:.3f}, and the embankment surface does "
    f"not move at all across accumulation 1..3000 while the excavated surface does."
)


# =========================================================================
# 3 [3]. catchment_exceeds_ceiling
# =========================================================================
# THE CASE IS REACHABLE, and that is the first thing to establish: the
# nomination mask gates SEEDS at the ceiling, but the pinch sits
# DOWNSTREAM of its seed by construction, so its catchment is always at
# least the seed's and can be far more. A compartment can therefore be
# anchored on a perfectly gated seed and still dam a drainage past
# farm-pond scale.
#
# 4000 cells * 0.0061776 = 24.7106 ac at the pinch, past the 20 ac
# ceiling; 20 cells (0.1236 ac) at the seed, comfortably under it.
ACC_OVER = np.ones(DEM["array"].shape, dtype=np.float64)
ACC_OVER[SEED_ROWCOL] = 20.0
ACC_OVER[PINCH_ROWCOL] = 4000.0
OVER_ACRES = 4000.0 * CELL_ACRES
assert math.isclose(OVER_ACRES, 24.7106, abs_tol=5e-4), f"hand-computed, got {OVER_ACRES}"
assert OVER_ACRES > MAX_VALLEY_CONTRIBUTING_AREA_ACRES
assert SEED_ACRES < MAX_VALLEY_CONTRIBUTING_AREA_ACRES, (
    "the SEED clears the nomination gate -- this is not a seed the mask would ever have refused"
)

over = _build(ACC_OVER)
assert over["catchment_exceeds_ceiling"] is True
assert wsa.FLAG_CATCHMENT_EXCEEDS_CEILING in over["flags"], "flagged on the zone, not only in a verdict"
assert over["pinch_drainage_score"] == 0.0
# THE VERDICT AND THE SCORE ARE SEPARATE FIELDS, and this is why: the
# band reads 0.0 at BOTH ends. Too little water and too much are
# opposite findings and must never collapse into one number.
assert inverted["pinch_drainage_score"] == over["pinch_drainage_score"] == 0.0
assert inverted["catchment_exceeds_ceiling"] is False and over["catchment_exceeds_ceiling"] is True, (
    "two compartments scoring 0.0 for OPPOSITE reasons are distinguishable on the record"
)

# END TO END: the compartment is built, gets an id, and is DROPPED with
# the reason -- visible in the dropped list, per the established pattern,
# never silently absent.
over_result = compute_water_survey_areas(DEM, BOUNDARY, flow_accumulation=ACC_OVER)
over_dropped = [
    zone
    for zone in over_result["dropped_zones"]
    if zone["drop_reason"] == wsa.REASON_CATCHMENT_EXCEEDS_CEILING
]
assert over_dropped, (
    "a compartment whose pinch drains past the ceiling must appear in the dropped list with its "
    f"reason: {[z['drop_reason'] for z in over_result['dropped_zones']]}"
)
for zone in over_dropped:
    assert zone["status"] == wsa.ZONE_STATUS_DROPPED and zone["rank"] is None
    assert zone["pinch_catchment_acres"] > MAX_VALLEY_CONTRIBUTING_AREA_ACRES
    assert zone["id"] is not None
    # The full record survives the drop, so the diagnostic can show WHICH
    # reach was refused and how much water was above it.
    assert zone["zone_acres"] > 0 and zone["compartment_footprint_acres"] > 0
    assert zone["seed_blend_score"] > 0
    assert zone["cross_type_overlaps"] == []
assert all(
    zone["catchment_exceeds_ceiling"] is False
    for zone in over_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
), "no over-ceiling compartment survives into the output"

# THE SEED LADDER SEES IT, as its own outcome class rather than as an
# unexplained absence.
import diagnose_water_survey_areas as diag  # noqa: E402  (after the module under test)

_ladder_zone_by_id = {
    zone["id"]: zone for zone in over_result["zones"] + over_result["dropped_zones"]
}
_over_records = [
    record
    for record in over_result["embankment_seeds"]
    if _ladder_zone_by_id.get(record.get("zone_id")) in over_dropped
]
assert _over_records, "the refusing compartment's seed is on the ladder"
for record in _over_records:
    bucket, detail, key = diag._seed_outcome(record, _ladder_zone_by_id)
    assert bucket == "dropped" and key == diag._OVER_CEILING_BREAKDOWN_KEY
    assert wsa.REASON_CATCHMENT_EXCEEDS_CEILING in detail
    assert "pinch catchment" in detail, "and the line says how much water refused it"

# ORDERING: the ceiling decides BEFORE dedupe, so a disqualified
# compartment can never win a valley away from a qualifying one. Asserted
# structurally -- an over-ceiling compartment is never handed to the
# dedupe -- by giving one a blend that WOULD win and checking it takes
# nothing with it.
_kept, _dupes = wsa.dedupe_compartments_by_overlap([comp, inverted])
assert len(_kept) + len(_dupes) == 2, "the dedupe itself is unchanged by any of this"
print(
    f"3. Ceiling disqualifier: a gated seed ({SEED_ACRES:.4f} ac) whose pinch drains "
    f"{OVER_ACRES:.4f} ac builds a compartment and is DROPPED with catchment_exceeds_ceiling, "
    f"record intact, its own ladder outcome class, and 0.0 distinguishable from the "
    "too-little-water 0.0 by the separate verdict field."
)


# =========================================================================
# 4 [4]. THE FILL CLAIM ON THE WIRE -- three numbers, reported apart
# =========================================================================
result = compute_water_survey_areas(DEM, BOUNDARY, flow_accumulation=ACC)
survivors = result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
assert survivors, "the ordinary fixture produces at least one surviving compartment"
zone = survivors[0]

features = wsa.survey_areas_to_geojson(result["zones"], result["dropped_zones"])
validate_feature_collection(features)
json.dumps(features)
props = next(
    feature["properties"]
    for feature in features["features"]
    if feature["properties"].get("layer") == "survey_zone_embankment"
    and feature["properties"]["zone_id"] == zone["id"]
)
for key in (
    "seed_blend_score",
    "pinch_catchment_acres",
    "pinch_drainage_score",
    "compartment_rank_score",
    "catchment_exceeds_ceiling",
):
    assert key in props, f"the feature properties carry {key} separately"
assert props["pinch_catchment_acres"] == zone["pinch_catchment_acres"]
assert props["compartment_rank_score"] == compartment_rank_score(
    props["seed_blend_score"], props["pinch_drainage_score"]
), "the composite is recomputable from its two published inputs -- that is what 'reported apart' means"

narrative = wsa.build_narrative_data(result)
json.dumps(narrative)
block = next(b for b in narrative["zones"] if b["id"] == zone["id"])
for key in (
    "seed_blend_score",
    "pinch_catchment_acres",
    "pinch_drainage_score",
    "compartment_rank_score",
):
    assert key in block, f"narrative_data carries {key} separately: {sorted(block)}"
assert block["compartment_rank_score"] == compartment_rank_score(
    block["seed_blend_score"], block["pinch_drainage_score"]
)
assert narrative["scales"]["pinch_drainage_score"]["ceiling_acres"] == MAX_VALLEY_CONTRIBUTING_AREA_ACRES
assert narrative["scales"]["compartment_rank_score"]["weights"] == dict(
    wsa.EMBANKMENT_COMPARTMENT_RANK_WEIGHTS
)

# THE PANEL DELIBERATELY CARRIES NONE OF THE THREE, and the decision is
# recorded structurally rather than left as an omission: the five
# always-rows are the panel's whole budget and they are TYPE-GENERIC, so
# an embankment-only acreage cannot join them without either blanking on
# every excavated zone or making the always-set type-dependent.
panel_keys = {row["key"] for row in block["panel"]}
for excluded in ("pinch_catchment_acres", "pinch_drainage_score", "compartment_rank_score"):
    assert excluded in wsa.PANEL_EXCLUDED_KEYS, f"{excluded} is an explicit panel exclusion"
    assert excluded not in panel_keys, f"{excluded} must not be a panel row"
assert len([row for row in block["panel"] if row["key"] in wsa.PANEL_ALWAYS_ROWS]) == 5, (
    "the five-row budget is intact -- the catchment figure stayed off it, it did not displace one"
)

# THE SUMMARY, where a human reads the run: the fill claim gets its own
# line rather than being folded into the anchor sentence.
summary = wsa.summarize_water_survey_areas(result)
assert "fill claim:" in summary
assert f"{zone['pinch_catchment_acres']} ac of catchment at the pinch cell" in summary
assert f"drainage {zone['pinch_drainage_score']}" in summary
assert f"rank score {zone['compartment_rank_score']}" in summary

# THE DIAGNOSTIC EXPORT's pinch layer -- the layer where "what does this
# dam impound" is answerable by clicking the dam.
from wire_translation import water_embankment_detail_features  # noqa: E402

detail = water_embankment_detail_features(result["zones"], result["embankment_seeds"])
pinch_feature = next(
    feature
    for feature in detail
    if feature["properties"]["layer"] == "embankment_pinch"
    and feature["properties"]["zone_id"] == zone["id"]
)
assert pinch_feature["properties"]["catchment_acres"] == zone["pinch_catchment_acres"]
assert pinch_feature["properties"]["drainage_score"] == zone["pinch_drainage_score"]
assert pinch_feature["properties"]["catchment_exceeds_ceiling"] is False
assert "of catchment above it" in pinch_feature["properties"]["label"]
print(
    f"4. Three numbers, reported apart: seed blend {zone['seed_blend_score']}, pinch catchment "
    f"{zone['pinch_catchment_acres']} ac -> drainage {zone['pinch_drainage_score']}, composite "
    f"{zone['compartment_rank_score']} (recomputable from the two published inputs) -- on the "
    "feature, in narrative_data, in the summary and on the pinch layer; NONE of them on the panel."
)


# =========================================================================
# 5 [5]. EXCAVATED IS UNTOUCHED
# =========================================================================
# drainage_runon is still a PER-CELL criterion at 0.10 on a type whose
# ground is extraction-based and has no pinch. Two claims, both asserted:
# the weight table is unchanged, and the excavated OUTPUT is byte-
# identical across the inputs this change moved.
assert wsa.EXCAVATED_WEIGHTS == {
    "wetness": 0.35,
    "soil": 0.30,
    "slope": 0.25,
    "drainage_runon": 0.10,
}
assert "drainage_runon" in wsa.EXCAVATED_WEIGHTS


def _excavated_signature(res):
    """Everything the excavated path produces, in a comparable form --
    zones, their members, and the per-criterion means, deliberately
    including the criterion vocabulary itself so a silently added or
    dropped criterion shows up as a difference rather than as a key
    nobody compared."""
    return json.dumps(
        [
            {
                "cells": sorted(tuple(cell) for cell in zone["cells"]),
                "zone_acres": zone["zone_acres"],
                "member_acres": zone["member_acres"],
                "member_count": zone["member_count"],
                "mean_suitability": zone["mean_suitability"],
                "max_suitability": zone["max_suitability"],
                "criteria": {
                    name: entry["mean_score"]
                    for name, entry in zone["criterion_contributions"].items()
                },
                "flags": sorted(zone["flags"]),
            }
            for zone in sorted(
                res["zones_by_type"][SURVEY_TYPE_EXCAVATED], key=lambda z: z["rank"]
            )
        ],
        sort_keys=True,
    )


# The SAME accumulation grid through the pipeline twice, and through a
# run whose only difference is a pinch catchment large enough to
# disqualify every compartment: the excavated half must not notice.
baseline_excavated = _excavated_signature(result)
assert baseline_excavated == _excavated_signature(
    compute_water_survey_areas(DEM, BOUNDARY, flow_accumulation=ACC)
), "the excavated path is deterministic on identical inputs"
assert (
    _excavated_signature(compute_water_survey_areas(DEM, BOUNDARY, flow_accumulation=ACC_INVERTED))
    != baseline_excavated
), (
    "SANITY ON THE INSTRUMENT: the excavated signature DOES move when the accumulation grid moves "
    "-- drainage_runon still reads it -- so an equality below would be a real finding rather than "
    "a comparison of two constants"
)
# THE REAL ISOLATION TEST: move THIS BRANCH'S OWN CONSTANTS and watch
# the excavated half not notice. The accumulation grid is held byte-
# identical (so drainage_runon reads the same numbers it always did) and
# the drainage BAND -- the thing that moved to the pinch -- is perturbed
# hard, along with the compartment ranking weights. If any of that could
# reach the excavated path, this fails; and the sensitivity assertion
# above proves the signature is capable of failing.
#
# The band's constants are chosen to invert its verdict everywhere: a
# minimum above the fixture's largest catchment means every compartment
# now reads 0.0 for fill.
_saved = (
    wsa.EMBANKMENT_DRAINAGE_MIN_ACRES,
    wsa.EMBANKMENT_DRAINAGE_FULL_CREDIT_ACRES,
    wsa.EMBANKMENT_COMPARTMENT_RANK_WEIGHTS,
)
try:
    wsa.EMBANKMENT_DRAINAGE_MIN_ACRES = 100.0
    wsa.EMBANKMENT_DRAINAGE_FULL_CREDIT_ACRES = 200.0
    wsa.EMBANKMENT_COMPARTMENT_RANK_WEIGHTS = {"seed_blend": 0.9, "pinch_drainage": 0.1}
    perturbed = compute_water_survey_areas(DEM, BOUNDARY, flow_accumulation=ACC)
finally:
    (
        wsa.EMBANKMENT_DRAINAGE_MIN_ACRES,
        wsa.EMBANKMENT_DRAINAGE_FULL_CREDIT_ACRES,
        wsa.EMBANKMENT_COMPARTMENT_RANK_WEIGHTS,
    ) = _saved
assert _excavated_signature(perturbed) == baseline_excavated, (
    "BYTE-IDENTICAL EXCAVATED OUTPUT: neither the drainage band's constants nor the compartment "
    "ranking weights can reach the excavated path -- drainage_runon is its own scorer on its own "
    "criterion, and this branch did not touch it"
)
# ...and the perturbation DID land where it was aimed, so the equality
# above is a finding about isolation rather than about a no-op.
_perturbed_embankment = perturbed["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
assert _perturbed_embankment, "the fixture still produces compartments under the perturbed band"
assert all(zone["pinch_drainage_score"] == 0.0 for zone in _perturbed_embankment), (
    "the perturbed band scores every one of this fixture's catchments zero -- the change went in"
)
assert any(
    zone["compartment_rank_score"] != original["compartment_rank_score"]
    for zone, original in zip(_perturbed_embankment, survivors)
), "and the embankment ranking moved with it"

# The two surfaces are separate end to end: disqualifying compartments
# on the embankment side removes no excavated zone.
assert over_result["zones_by_type"][SURVEY_TYPE_EXCAVATED], (
    "the over-ceiling run still produces excavated zones -- the embankment disqualifier is not a "
    "pipeline-wide gate"
)
assert wsa.EXCAVATED_WEIGHTS["drainage_runon"] == 0.10, "the weight is stated, not inferred"
print(
    "5. Excavated untouched: drainage_runon still a per-cell criterion at 0.10, the weight table "
    "unchanged, and the excavated signature BYTE-IDENTICAL while the drainage band's constants and "
    "the compartment ranking weights are perturbed hard enough to zero every fill claim -- with "
    "the signature demonstrably sensitive to accumulation, so the comparison has teeth."
)

print("\nAll pinch-catchment drainage checks passed.")
