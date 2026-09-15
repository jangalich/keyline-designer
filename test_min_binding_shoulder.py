"""
test_min_binding_shoulder.py

THE ENCLOSURE GATE: a dam site must be able to impound.

The dam-site objective relocates toward the best available shoulder, but
nothing refused a site whose best available shoulder cannot hold water.
The reference run showed the consequence -- eight embankment survivors,
several of them half-acre compartments whose binding shoulders read
0.22-0.46 m: compartments drawn around dam sites that physically cannot
impound.

MIN_BINDING_SHOULDER_METERS = 0.91 is NRCS Conservation Practice
Standard 378's 3 ft, the smallest impoundment the standard recognises as
an embankment pond. It is EXTERNALLY ANCHORED, not tuned.

RIDGE_PROMINENCE_METERS IS NOT THIS LEVER and does not move: it is a
crest DETECTOR ("local high point, or LiDAR speckle?") sitting well
clear of 3DEP's ~10 cm vertical accuracy. Detection was working;
QUALIFICATION was missing, and that is what this gate is.

Run as:

    python test_min_binding_shoulder.py

Sections (the design's numbered test items in brackets):
  1  [1]  THE THRESHOLD -- 0.90 m drops with the new code and its
          measured height; 0.92 m survives.
  2  [2]  THE BINDING SIDE -- the gate reads the LOWER shoulder, not
          the mean and not the deeper one; asymmetric, hand-derived.
  3  [3]  THREE DISTINCT FAILURES -- absent, degenerate-seed and
          too-low keep their own codes and are distinguishable.
  4  [4]  ORDERING -- before the floor, and before dedupe: a sub-gate
          compartment reports the SHOULDER reason even when it is also
          tiny, and cannot take a valid neighbour with it.
  5  [5]  THE SENSITIVITY LADDER's arithmetic on a mixed population.
  6  [6]  CONTRACT -- consumer fields intact; selected_water_zone
          movement REPORTED, including the legitimate case where an
          emptied embankment class hands selection to excavated.
"""

import numpy as np
from shapely import contains_xy
from shapely.geometry import box

import water_survey_areas as wsa
from diagnose_pinch_bearing_and_bound import (
    SHOULDER_SENSITIVITY_METERS,
    summarize_shoulder_gate,
)
from valley_delineation import compute_flow_direction, fill_depressions
from valley_level_pool import POOL_REFERENCE_HEIGHT_METERS
from water_survey_areas import (
    FLAG_SHOULDER_BELOW_MINIMUM,
    MIN_BINDING_SHOULDER_METERS,
    REASON_BEST_SITE_AT_SEED,
    REASON_NO_MEASURABLE_SHOULDER,
    REASON_SHOULDER_BELOW_MINIMUM,
    RIDGE_PROMINENCE_METERS,
    SURVEY_TYPE_EMBANKMENT,
    compute_water_survey_areas,
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


def _boundary_for(dem):
    rows, cols = dem["array"].shape
    return box(
        ORIGIN_X + 1 * RESOLUTION + 0.1,
        ORIGIN_Y - (rows - 2) * RESOLUTION + 0.1,
        ORIGIN_X + (cols - 1) * RESOLUTION - 0.1,
        ORIGIN_Y - 1 * RESOLUTION - 0.1,
    )


# =========================================================================
# THE GATE FIXTURE: one channel, one waist, a settable shoulder
# =========================================================================
# 40x31 at 5 m, channel down col 15, base(r) = 100 - 0.25r. A gentle
# 0.05 m/cell floor so the SHOULDER is genuinely the highest thing on
# every ray (at a steeper floor the crest walk declares the inner floor
# edge instead, and the fixture would be measuring the wrong thing).
#
#   rows  0-13   k = 7, rise 0.30 m   the seed's own reach: wide and
#                                     shallow, so it never wins
#   rows 14-25   k = 2, WAIST, rise set per fixture -- the dam site
#   rows 26-39   k = 7, rise 0.30 m
#
# HAND-DERIVED: width = 10k - 2.5, so the waist is 17.5 m and the rest
# 67.5 m. The binding height IS the rise, because the shoulder sits at
# base + rise and the channel at base, and base cancels.
#
# At waist rise h the objective scores h**2 / 17.5 against
# 0.09 / 67.5 = 0.00133 for the seed reach, so any waist rise above
# about 0.15 m wins the selection outright -- which is what makes the
# fixture able to put a sub-gate site in the chosen position at all.
GATE_ROWS, GATE_COLS, GATE_CHANNEL = 40, 31, 15
GATE_SEED = (4, GATE_CHANNEL)
GATE_WAIST = (14, GATE_CHANNEL)


def _gate_array(waist_left_rise, waist_right_rise=None):
    """The fixture, with the waist's two shoulders settable
    independently -- section 2 needs them to differ."""
    if waist_right_rise is None:
        waist_right_rise = waist_left_rise
    array = np.zeros((GATE_ROWS, GATE_COLS))
    for r in range(GATE_ROWS):
        base = 100.0 - 0.25 * r
        waist = 14 <= r <= 25
        k = 2 if waist else 7
        for c in range(GATE_COLS):
            d = abs(c - GATE_CHANNEL)
            rise = (
                (waist_left_rise if c > GATE_CHANNEL else waist_right_rise)
                if waist
                else 0.30
            )
            if d < k:
                array[r, c] = base + 0.05 * d
            elif d == k:
                array[r, c] = base + rise
            else:
                array[r, c] = base + rise - 2.6 - 0.05 * (d - k - 1)
    return array


def _walk_gate(dem):
    rows, cols = dem["array"].shape
    filled = fill_depressions(dem["array"])
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    col_x = ORIGIN_X + (np.arange(cols) + 0.5) * RESOLUTION
    row_y = ORIGIN_Y - (np.arange(rows) + 0.5) * RESOLUTION
    xs, ys = np.meshgrid(col_x, row_y)
    return walk_embankment_pinch(
        dem,
        GATE_SEED,
        flow_to_row,
        flow_to_col,
        contains_xy(_boundary_for(dem), xs, ys),
        np.zeros(dem["array"].shape, dtype=bool),
        max_walk_meters=200.0,
    )


def _zones_for(waist_rise, right=None):
    dem = _dem(_gate_array(waist_rise, right))
    result = compute_water_survey_areas(dem, _boundary_for(dem))
    return dem, result


# =========================================================================
# 1 [1]. THE THRESHOLD
# =========================================================================
# 0.90 m is a centimetre under CPS 378's 3 ft; 0.92 m is a centimetre
# over. Same fixture, same chosen station, same width -- only the
# shoulder differs, so nothing but the gate can explain the difference.
_below_walk = _walk_gate(_dem(_gate_array(0.90)))
_above_walk = _walk_gate(_dem(_gate_array(0.92)))
assert _below_walk["pinch_rowcol"] == _above_walk["pinch_rowcol"] == GATE_WAIST, (
    "both fixtures must choose the SAME station, or the comparison is not about the gate: "
    f"{_below_walk['pinch_rowcol']} vs {_above_walk['pinch_rowcol']}"
)
assert _below_walk["pinch_width_m"] == _above_walk["pinch_width_m"] == 17.5
assert _below_walk["pinch_binding_height_m"] == 0.90
assert _above_walk["pinch_binding_height_m"] == 0.92
assert 0.90 < MIN_BINDING_SHOULDER_METERS < 0.92, (
    f"the fixture must straddle the gate: {MIN_BINDING_SHOULDER_METERS}"
)

# THROUGH THE WHOLE PIPELINE, and the rises differ from the walk's
# because compute_water_survey_areas() picks its OWN seeds. Its best
# ones here sit one column off the channel, where the waist floor stands
# 0.05 m above the channel bed, so a station's binding shoulder reads
# rise - 0.05. The straddle above is the controlled comparison -- same
# seed, same station, one centimetre apart; these two runs are the
# mechanism through the pipeline, chosen so the gate plainly fires on
# one and plainly does not on the other.
_below_dem, _below_result = _zones_for(0.90)   # sites measure 0.85 -- under
_above_dem, _above_result = _zones_for(1.20)   # sites measure 1.15 -- over

_below_refused = [
    zone
    for zone in _below_result["dropped_zones"]
    if zone["drop_reason"] == REASON_SHOULDER_BELOW_MINIMUM
]
assert _below_refused, (
    "a 0.90 m shoulder cannot hold CPS 378's 3 ft and must be refused: "
    f"{[z['drop_reason'] for z in _below_result['dropped_zones']]}"
)
for _zone in _below_refused:
    assert _zone["status"] == wsa.ZONE_STATUS_DROPPED and _zone["rank"] is None
    assert _zone["presented"] is False and _zone["presentation_order"] is None
    # THE MEASUREMENT RIDES THE REFUSAL. "Refused" without "by how much"
    # is not a measurement, and the whole point of gating after
    # selection rather than during it is that the reach can still say
    # what it offers.
    assert _zone["pinch_binding_height_m"] < MIN_BINDING_SHOULDER_METERS, (
        f"refused for being under the bar, and the measured height says by how much: "
        f"{_zone['pinch_binding_height_m']}"
    )
    assert _zone["pinch_binding_height_m"] == 0.85, (
        "hand-derived: the 0.90 m waist shoulder measured from a station one column off the "
        "channel, where the floor stands 0.05 m up"
    )
    assert _zone["min_binding_shoulder_m"] == MIN_BINDING_SHOULDER_METERS
    assert _zone["shoulder_below_minimum"] is True
    assert FLAG_SHOULDER_BELOW_MINIMUM in _zone["flags"]
    # And the reach itself survives on the record: the chosen station,
    # its width and its catchment are all still readable.
    assert _zone["pinch"]["rowcol"][0] == GATE_WAIST[0], (
        "the chosen station is still on the record -- the waist row, whichever column the seed "
        f"walked down: {_zone['pinch']['rowcol']}"
    )
    assert _zone["pinch"]["width_m"] == 17.5
    assert _zone["zone_acres"] > 0

_above_survivors = _above_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
assert _above_survivors, "a shoulder over the bar clears the gate and survives"
assert not [
    zone
    for zone in _above_result["dropped_zones"]
    if zone["drop_reason"] == REASON_SHOULDER_BELOW_MINIMUM
], "and nothing on this fixture is refused by the gate at all"
for _zone in _above_survivors:
    assert _zone["shoulder_below_minimum"] is False
    assert FLAG_SHOULDER_BELOW_MINIMUM not in _zone["flags"]
    assert _zone["pinch_binding_height_m"] >= MIN_BINDING_SHOULDER_METERS

# THE PROMINENCE DETECTOR IS UNTOUCHED, asserted here because the whole
# framing of this branch is that it is NOT the lever.
assert RIDGE_PROMINENCE_METERS == 1.0, (
    "the crest DETECTOR does not move: lowering it declares crests on LiDAR speckle, raising it "
    "misses real small shoulders. Qualification is a separate question and a separate constant."
)
assert MIN_BINDING_SHOULDER_METERS != RIDGE_PROMINENCE_METERS, (
    "and the two are different numbers doing different jobs -- a coincidence of value would invite "
    "exactly the confusion this branch exists to prevent"
)

print(
    f"1. The threshold: on one controlled walk (same seed, same station {GATE_WAIST}, 17.5 m wide) "
    f"0.90 m and 0.92 m straddle the {MIN_BINDING_SHOULDER_METERS} m bar; through the pipeline the "
    f"under-bar fixture yields {len(_below_refused)} refusal(s) carrying "
    f"{REASON_SHOULDER_BELOW_MINIMUM}, their measured 0.85 m and the bar, while the over-bar one "
    f"yields none. RIDGE_PROMINENCE_METERS stays {RIDGE_PROMINENCE_METERS} m."
)


# =========================================================================
# 2 [2]. THE GATE READS THE BINDING SIDE
# =========================================================================
# An asymmetric waist: 0.60 m on one shoulder, 4.00 m on the other.
#   the BINDING (lower) side  0.60 m  -> below the gate, REFUSED
#   the mean of the two       2.30 m  -> would clear it
#   the deeper side           4.00 m  -> would clear it easily
# Water rising in this compartment spills over the 0.60 m side a little
# past 0.60 m; the 4.00 m side is irrelevant to that limit. So the gate
# must refuse, and it must refuse on 0.60.
_asym_walk = _walk_gate(_dem(_gate_array(0.60, 4.00)))
assert _asym_walk["pinch_rowcol"] == GATE_WAIST
_asym_station = next(
    s for s in _asym_walk["stations"] if tuple(s["rowcol"]) == GATE_WAIST
)
assert (_asym_station["crest_height_left_m"], _asym_station["crest_height_right_m"]) == (0.60, 4.00)
assert _asym_walk["pinch_binding_height_m"] == 0.60, (
    "the BINDING side, hand-derived: min(0.60, 4.00)"
)
assert _asym_walk["pinch_binding_height_m"] != 2.30, "not the mean"
assert _asym_walk["pinch_binding_height_m"] != 4.00, "not the deeper side"

_asym_dem, _asym_result = _zones_for(0.60, 4.00)
# THROUGH THE PIPELINE, on its own seeds: at least one compartment in
# the asymmetric waist is refused. Its exact station and height depend
# on which cell the seeder picked (the hand-derivation above is the
# controlled one), so what is asserted here is the property: a
# compartment whose waist has a 4.00 m shoulder on one side is STILL
# refused, because the other side is 0.60 m.
_asym_refused = [
    zone
    for zone in _asym_result["dropped_zones"]
    if zone["drop_reason"] == REASON_SHOULDER_BELOW_MINIMUM
]
assert _asym_refused, (
    "a 0.60 m binding shoulder is refused however deep the other side is -- if the mean or the max "
    "were read, this compartment would have survived"
)
for _zone in _asym_refused:
    assert _zone["pinch_binding_height_m"] < MIN_BINDING_SHOULDER_METERS
    _station = next(
        s for s in _zone["walk_stations"]
        if tuple(s["rowcol"]) == tuple(_zone["pinch"]["rowcol"])
    )
    _sides = (_station["crest_height_left_m"], _station["crest_height_right_m"])
    assert max(h for h in _sides if h is not None) > MIN_BINDING_SHOULDER_METERS, (
        f"the refused station's DEEPER side clears the bar ({_sides}) -- so the refusal can only "
        "be the binding side being read"
    )
    assert _zone["pinch_binding_height_m"] == min(h for h in _sides if h is not None)
assert 2.30 > MIN_BINDING_SHOULDER_METERS and 4.00 > MIN_BINDING_SHOULDER_METERS, (
    "both wrong answers would have cleared the gate, which is what makes this fixture decisive"
)

print(
    "2. The binding side: a waist with 0.60 m and 4.00 m shoulders is refused on 0.60 -- the mean "
    "(2.30) and the deeper side (4.00) would both have cleared, so reading either would be visible "
    "here."
)


# =========================================================================
# 3 [3]. THREE DISTINCT FAILURES
# =========================================================================
# "No shoulder was measurable", "the seed is its own best site" and "the
# measured shoulder is too low" are three different findings about three
# different pieces of ground. Merging any two would tell a reader the
# wrong thing about their land.
#
# ABSENT: both flanks climb to the grid edge, so no station has a
# shoulder at all. Still REASON_NO_MEASURABLE_SHOULDER, and NOT the new
# gate -- an unmeasured shoulder is not a low one.
def _absent_array():
    array = np.zeros((GATE_ROWS, GATE_COLS))
    for r in range(GATE_ROWS):
        base = 100.0 - 0.25 * r
        for c in range(GATE_COLS):
            array[r, c] = base + 0.6 * abs(c - GATE_CHANNEL)
    return array


_absent_walk = _walk_gate(_dem(_absent_array()))
assert _absent_walk["found"] is False
assert _absent_walk["reason_code"] == REASON_NO_MEASURABLE_SHOULDER
assert _absent_walk["reason_code"] != REASON_SHOULDER_BELOW_MINIMUM, (
    "absent is not low: this ground was never measured for depth, and the gate has nothing to "
    "refuse"
)

# DEGENERATE SEED: the seed's own reach is the best site, so the walk
# fails before there is a site to gate. Takes precedence over the gate
# even though the seed's shoulder is itself sub-gate.
def _seed_best_array():
    array = np.zeros((GATE_ROWS, GATE_COLS))
    for r in range(GATE_ROWS):
        base = 100.0 - 0.25 * r
        k, rise = (2, 0.50) if r < 8 else (7, 0.30)
        for c in range(GATE_COLS):
            d = abs(c - GATE_CHANNEL)
            if d < k:
                array[r, c] = base + 0.05 * d
            elif d == k:
                array[r, c] = base + rise
            else:
                array[r, c] = base + rise - 2.6 - 0.05 * (d - k - 1)
    return array


_seed_best_walk = _walk_gate(_dem(_seed_best_array()))
assert _seed_best_walk["found"] is False
assert _seed_best_walk["reason_code"] == REASON_BEST_SITE_AT_SEED, (
    f"the degenerate baseline fails first, before the gate sees a site: "
    f"{_seed_best_walk['reason_code']}"
)
assert _seed_best_walk["stations"][0]["binding_height_m"] == 0.50, (
    "and that seed station's own shoulder IS sub-gate -- so precedence is being tested, not "
    "coincidence: if the gate ran first this would carry the wrong code"
)
assert 0.50 < MIN_BINDING_SHOULDER_METERS

# TOO LOW: section 1's fixture. Three codes, three fixtures, no overlap.
_three = {
    _absent_walk["reason_code"],
    _seed_best_walk["reason_code"],
    _below_refused[0]["drop_reason"],
}
assert _three == {
    REASON_NO_MEASURABLE_SHOULDER, REASON_BEST_SITE_AT_SEED, REASON_SHOULDER_BELOW_MINIMUM
}, f"three distinguishable failures: {_three}"
assert len(_three) == 3

print(
    f"3. Three failures, distinguishable: absent shoulders -> {REASON_NO_MEASURABLE_SHOULDER} (a "
    f"walk-level failure, no site to gate), the seed its own best site -> "
    f"{REASON_BEST_SITE_AT_SEED} (takes precedence even though that seed's own 0.50 m shoulder is "
    f"itself sub-gate), and a measured-but-low site -> {REASON_SHOULDER_BELOW_MINIMUM}."
)


# =========================================================================
# 4 [4]. ORDERING: BEFORE THE FLOOR, BEFORE DEDUPE
# =========================================================================
# BEFORE THE FLOOR. A sub-gate compartment is frequently ALSO tiny -- a
# site with no shoulder tends to draw a thin compartment -- and whichever
# rule is applied first is the reason a reader sees. The enclosure
# reason is the informative one: "this cannot impound" tells an owner
# something about their land; "this is small" does not.
# A 0.70 m waist: sub-gate at every station, and the compartment the
# seeder builds around it is 0.068 ac -- under the 0.1 ac floor too. It
# is therefore refusable by BOTH rules, which is exactly what makes the
# ordering testable rather than assumed.
_tiny_dem, _tiny_result = _zones_for(0.70)
_tiny_refused = [
    zone
    for zone in _tiny_result["dropped_zones"]
    if zone["drop_reason"] == REASON_SHOULDER_BELOW_MINIMUM
]
assert _tiny_refused, "the 0.30 m fixture must produce gate refusals"
_under_floor_and_sub_gate = [
    zone for zone in _tiny_refused if zone["zone_acres"] < wsa.MIN_SURVEY_REGION_AREA_ACRES
]
assert _under_floor_and_sub_gate, (
    "the ordering claim needs a compartment that is BOTH sub-gate and under the acreage floor, or "
    "it is untested"
)
for _zone in _under_floor_and_sub_gate:
    assert _zone["drop_reason"] == REASON_SHOULDER_BELOW_MINIMUM, (
        "a compartment that is both must report the SHOULDER reason -- the gate runs first"
    )
    assert _zone["drop_reason"] != wsa.FLAG_BELOW_MIN_AREA

# BEFORE DEDUPE, the sharper half: a compartment that cannot hold water
# must not be able to take a valid neighbour out of the run as its
# duplicate. Asserted structurally, at the source, because constructing
# a fixture where a sub-gate compartment would OUT-OVERLAP a qualifying
# one is a coincidence to hunt rather than a property to state -- and
# the property is that the gate's partition happens before the dedupe
# population is formed at all.
import inspect

_compute_source = inspect.getsource(wsa.compute_water_survey_areas)
_partition_at = _compute_source.index("below_shoulder_compartments = [")
_dedupe_at = _compute_source.index("dedupe_compartments_by_overlap(")
assert _partition_at < _dedupe_at, (
    "the sub-gate partition must be taken BEFORE the dedupe call, or a compartment that cannot "
    "impound could collapse one that can"
)
assert "dedupe_compartments_by_overlap(\n        qualified_compartments\n    )" in _compute_source, (
    "and the dedupe must run over the QUALIFIED population by name, not over every compartment"
)
# The floor's own loop runs later still, over survivors only.
_floor_at = _compute_source.index("below_min_area")
assert _dedupe_at < _floor_at

print(
    f"4. Ordering: a compartment that is both sub-gate and under the "
    f"{wsa.MIN_SURVEY_REGION_AREA_ACRES} ac floor reports "
    f"{REASON_SHOULDER_BELOW_MINIMUM} ({len(_under_floor_and_sub_gate)} of them here), and the "
    "sub-gate partition is taken before dedupe_compartments_by_overlap() is even called -- so a "
    "site that cannot impound can never collapse one that can."
)


# =========================================================================
# 5 [5]. THE SENSITIVITY LADDER
# =========================================================================
# The ladder reports how many selected dam sites clear each candidate
# bar. It chooses nothing -- the same curve-then-choose discipline the
# half-width sweep used -- but its arithmetic has to be right or the
# curve misleads.
assert SHOULDER_SENSITIVITY_METERS == (MIN_BINDING_SHOULDER_METERS, 1.5, POOL_REFERENCE_HEIGHT_METERS)
assert POOL_REFERENCE_HEIGHT_METERS == 2.5
assert SHOULDER_SENSITIVITY_METERS[0] < SHOULDER_SENSITIVITY_METERS[-1], (
    "the ladder must span the bracket the constant documents: CPS 378's floor to the level-pool "
    "arc's measuring stick"
)

# A MIXED POPULATION, hand-counted: the 0.60/4.00 asymmetric fixture
# refuses its waist compartment at 0.60 while the wide reaches sit at
# 0.30. Every built compartment's binding height is known, so the
# ladder's three counts are arithmetic on a list this test can check.
_gate_report = summarize_shoulder_gate(_asym_result)
_built = [
    zone
    for zone in _asym_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
    + _asym_result["dropped_zones"]
    if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT
    and zone["pinch_binding_height_m"] is not None
]
assert _built, "the fixture must build compartments for the ladder to count"
_heights = [zone["pinch_binding_height_m"] for zone in _built]
for _bar in SHOULDER_SENSITIVITY_METERS:
    _expected = sum(1 for _h in _heights if _h >= _bar)
    assert f"{_bar:>5.2f} m: {_expected} of {len(_heights)} selected site(s) clear it" in _gate_report, (
        f"the ladder's count at {_bar} m must be the hand-count {_expected} of {len(_heights)}:\n"
        f"{_gate_report}"
    )
# The counts are monotone non-increasing in the bar, by construction.
_counts = [sum(1 for _h in _heights if _h >= _bar) for _bar in SHOULDER_SENSITIVITY_METERS]
assert _counts == sorted(_counts, reverse=True), f"a higher bar cannot pass more sites: {_counts}"
assert "CHOOSES NOTHING" in _gate_report
assert "SHIPPED" in _gate_report and "measuring stick" in _gate_report

# THE DISTRIBUTION is reported over every site the run FOUND, not only
# survivors -- the question is how far the land sits from the threshold.
assert "EVERY SELECTED DAM SITE THE RUN FOUND" in _gate_report
assert f"DISTRIBUTION across {len(_heights)} selected dam site(s)" in _gate_report

print(
    f"5. Sensitivity: the ladder's counts at {SHOULDER_SENSITIVITY_METERS} match a hand-count over "
    f"{len(_heights)} built site(s) ({_counts}), are monotone in the bar, and the line says it "
    "chooses nothing. The distribution covers every site the run found, not only survivors."
)


# =========================================================================
# 6 [6]. CONTRACT
# =========================================================================
# The gate changes WHICH compartments survive, so counts move. What may
# not move is the shape of the record or the set of fields a consumer
# reads -- and a REFUSED compartment must carry the full set too, since
# it rides the dropped list into the diagnostic and the export.
for _zone in (
    _above_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
    + [z for z in _above_result["dropped_zones"] if z["survey_type"] == SURVEY_TYPE_EMBANKMENT]
    + _below_refused
):
    for _field in (
        "zone_acres", "compartment_footprint_acres", "mean_suitability", "seed_blend_score",
        "pinch_catchment_acres", "pinch_drainage_score", "representative_elevation_m",
        "render_fill_polygon_utm", "polygon_utm", "rank", "id", "presented", "flags",
        "seed_crest_height_min_m", "pinch_crest_height_min_m", "half_width_bound_hit",
        "shoulder_below_minimum", "pinch_binding_height_m", "min_binding_shoulder_m",
    ):
        assert _field in _zone, f"the consumer contract lost {_field} on a {_zone['status']} zone"
    assert _zone["render_fill_polygon_utm"] is _zone["polygon_utm"]

# THE GATE REACHES THE WIRE, on survivors and refusals alike.
_properties = wsa._zone_feature_properties(_below_refused[0])
assert _properties["shoulder_below_minimum"] is True
assert _properties["pinch_binding_height_m"] == _below_refused[0]["pinch_binding_height_m"] == 0.85
assert _properties["min_binding_shoulder_m"] == MIN_BINDING_SHOULDER_METERS
assert _properties["drop_reason"] == REASON_SHOULDER_BELOW_MINIMUM

# SELECTED_WATER_ZONE: REPORTED, NEVER ASSERTED STABLE -- and this
# branch adds a specific legitimate way for it to move. If the gate
# empties the embankment class, the pool has only excavated zones left
# to choose from, and selection becoming excavated is the correct
# consequence rather than a regression.
def _identity(result):
    zone = result["selected_water_zone"]
    return None if zone is None else (zone["survey_type"], round(zone["polygon_utm"].area, 3))


# The pair is the over-bar fixture against the 0.70 m one, which empties
# the embankment class entirely -- the case this branch specifically
# adds, and the one the reference property is expected to land in.
_before_identity = _identity(_above_result)
_after_identity = _identity(_tiny_result)
_before_counts = {t: len(v) for t, v in _above_result["zones_by_type"].items()}
_after_counts = {t: len(v) for t, v in _tiny_result["zones_by_type"].items()}
assert _before_counts[SURVEY_TYPE_EMBANKMENT] > 0
assert _after_counts[SURVEY_TYPE_EMBANKMENT] == 0, (
    "the 0.70 m fixture must empty the embankment class, or the emptied-class case is untested"
)
assert [
    zone for zone in _tiny_result["dropped_zones"]
    if zone["drop_reason"] == REASON_SHOULDER_BELOW_MINIMUM
], "and the gate must be part of why"
# WITH THE CLASS EMPTIED, selection legitimately becomes excavated. This
# is reported as the correct consequence, not asserted away as a
# regression: a parcel whose every dam site fails to impound has no
# embankment candidate for the pool to crown.
if _after_identity is not None:
    assert _after_identity[0] != SURVEY_TYPE_EMBANKMENT, (
        "with the embankment class emptied the pool cannot crown an embankment zone"
    )
assert _before_identity != _after_identity or _before_identity is None, (
    "and the selection identity is reported either way -- this line only records that the two runs "
    "were actually different"
)

print(
    f"6. Contract: every field intact on survivors AND refusals ({len(_below_refused)} refused "
    f"records carry the full set and reach the wire). Survivors {_before_counts} -> "
    f"{_after_counts}; selected_water_zone {_before_identity} -> {_after_identity} "
    f"({'MOVED' if _before_identity != _after_identity else 'unchanged'}) -- REPORTED, not asserted."
)

print("\nAll minimum-binding-shoulder checks passed.")
