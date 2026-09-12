"""
test_tree_zone_panel_data.py

THE THREE THINGS THE TREES DATA PANEL READS, and the gate that keeps one
of them honest. Run as:

    python3 test_tree_zone_panel_data.py

Offline and synthetic throughout -- hand-built DEMs and hand-fed geometry
unions straight into tree_zone_candidates.score_tree_search_space() and
build_narrative_data(), the same "pure logic, independent of real data
fetches" approach test_tree_zone_candidates.py already uses. No network is
mocked here because none is reachable from anything this file calls.

WHAT THIS BRANCH ADDED, and what each section proves:

  MARGINAL BENEFITS -- what a zone's ground is GOOD FOR, as words with no
    values, shipped as a list on each zone rather than as a rule the
    client derives. Three benefits, mapped from the scoring factors:

        erosion control       <- slope_factor
        nutrient deposition   <- soil_marginality_factor OR
                                 hydric_overlap_factor
        stream protection     <- stream_proximity_factor

  elevation_position -- where the zone sits between the parcel's lowest
    and highest ground, in production_area_ceiling.ELEVATION_POSITION_
    BANDS' own words, IMPORTED from production and never redeclared here.

  slope_median_pct -- the median of the same per-cell slope array
    avg_slope_pct is the mean of, under the name production and water
    already publish it by.

THE GATE IS THE REASON THIS FILE EXISTS (sections 1 and 3). A benefit
appears when its factor is above zero AND its availability gate is True,
both, always. soil_marginality_factor, hydric_overlap_factor and
stream_proximity_factor each fall back to tree_zone_candidates._NEUTRAL_
FACTOR_VALUE (0.5) when their data source could not be reached, and 0.5
is above zero -- so a plain above-zero rule would display a benefit the
zone never earned, computed from data that was never fetched, with
nothing in the output to tell a reader that from a measurement. Section 1
asserts the refusal for all three gates independently; section 7 shows it
end to end, on a zone that really qualifies and really earns nothing.

Sections:
  1  GATE CHECK: a neutral 0.5 behind a False gate produces NO benefit,
     asserted for each of the three gates independently.
  2  A factor above zero behind a True gate DOES produce its benefit.
  3  NUTRIENT DEPOSITION's OR: either source alone; neither when both are
     neutral-and-ungated; earned when one is neutral-and-ungated and the
     other is real.
  4  A zone that qualifies for nothing ships an EMPTY LIST.
  5  elevation_position's words are ELEVATION_POSITION_BANDS' own, checked
     against the IMPORTED constant rather than a copy of it.
  6  A None percentile yields a None position -- never a default word.
  7  END TO END through score_tree_search_space(): the median beside the
     mean over the same cells, the percentile against the parcel's own
     range, and both gate outcomes on real scored patches.
"""

import numpy as np
from shapely.geometry import Point, box

import tree_zone_candidates as tzc
from production_area import compute_slope_percent
from production_area_ceiling import ELEVATION_POSITION_BANDS, _on_parcel_cell_mask
from raster_grid import pixel_center_xy
from tree_zone_candidates import (
    MARGINAL_BENEFIT_EROSION_CONTROL,
    MARGINAL_BENEFIT_FACTOR_SOURCES,
    MARGINAL_BENEFIT_NUTRIENT_DEPOSITION,
    MARGINAL_BENEFIT_STREAM_PROTECTION,
    MARGINAL_BENEFITS,
    _NEUTRAL_FACTOR_VALUE,
    build_narrative_data,
    marginal_benefits,
    score_tree_search_space,
)

EROSION = MARGINAL_BENEFIT_EROSION_CONTROL
NUTRIENT = MARGINAL_BENEFIT_NUTRIENT_DEPOSITION
STREAM = MARGINAL_BENEFIT_STREAM_PROTECTION

# The mapping this file asserts against is READ OFF THE MODULE, not
# retyped: a copy here would pass happily while the shipped rule drifted
# underneath it, which is the whole failure mode these words exist to
# prevent elsewhere.
assert MARGINAL_BENEFITS == (EROSION, NUTRIENT, STREAM), MARGINAL_BENEFITS
assert dict(MARGINAL_BENEFIT_FACTOR_SOURCES)[NUTRIENT] == (
    ("soil_marginality_factor", "soil_marginality_data_available"),
    ("hydric_overlap_factor", "hydric_data_available"),
), "nutrient deposition must be the OR of soil marginality and hydric overlap"
assert _NEUTRAL_FACTOR_VALUE > 0.0, (
    "this whole file is only interesting because the neutral fallback is ABOVE ZERO -- "
    f"got {_NEUTRAL_FACTOR_VALUE}"
)


def _patch(**overrides) -> dict:
    """A scored patch's benefit-relevant fields: nothing earns anything
    until an override says so. Every factor at 0.0 and every gate True,
    so a test that flips ONE factor and ONE gate is testing exactly
    that pair and nothing else."""
    patch = {
        "slope_factor": 0.0,
        "soil_marginality_factor": 0.0,
        "hydric_overlap_factor": 0.0,
        "stream_proximity_factor": 0.0,
        "soil_marginality_data_available": True,
        "hydric_data_available": True,
        "stream_data_available": True,
    }
    patch.update(overrides)
    return patch


# =====================================================================
# 1. GATE CHECK -- a neutral 0.5 behind a False gate earns NOTHING
# =====================================================================
#
# THE BRANCH'S REASON TO EXIST. Each of the three gated factors is set to
# exactly the value it defaults to when its fetch never ran, its own gate
# is turned off, and the benefit it would otherwise have produced must be
# absent. Asserted one gate at a time, so a rule that happened to check
# some OTHER gate could not pass any of the three.

_gate_check = []
for factor_key, gate_key, benefit in (
    ("soil_marginality_factor", "soil_marginality_data_available", NUTRIENT),
    ("hydric_overlap_factor", "hydric_data_available", NUTRIENT),
    ("stream_proximity_factor", "stream_data_available", STREAM),
):
    ungated = _patch(**{factor_key: _NEUTRAL_FACTOR_VALUE, gate_key: False})
    earned = marginal_benefits(ungated)
    assert benefit not in earned, (
        f"{factor_key} at the neutral {_NEUTRAL_FACTOR_VALUE} with {gate_key}=False must NOT "
        f"produce {benefit!r} -- the value is a fallback, not a measurement. Got {earned}"
    )
    # THE CONTROL, and it is what makes the line above a measurement
    # rather than an unreachable path: the identical patch with only the
    # gate flipped back to True DOES earn it. So the refusal above is the
    # gate doing its job, not the factor being too small to count.
    control = _patch(**{factor_key: _NEUTRAL_FACTOR_VALUE, gate_key: True})
    assert benefit in marginal_benefits(control), (
        f"control: {factor_key} at {_NEUTRAL_FACTOR_VALUE} with {gate_key}=True must produce "
        f"{benefit!r}, or the assertion above proves nothing"
    )
    _gate_check.append((gate_key, benefit))

# ALL THREE AT ONCE, which is the shape a total SSURGO+NHD outage
# actually produces: every gated factor sitting at the neutral value,
# every gate False. A plain above-zero rule would claim BOTH gated
# benefits here.
_all_ungated = _patch(
    soil_marginality_factor=_NEUTRAL_FACTOR_VALUE,
    hydric_overlap_factor=_NEUTRAL_FACTOR_VALUE,
    stream_proximity_factor=_NEUTRAL_FACTOR_VALUE,
    soil_marginality_data_available=False,
    hydric_data_available=False,
    stream_data_available=False,
)
assert marginal_benefits(_all_ungated) == [], marginal_benefits(_all_ungated)
assert marginal_benefits(
    _patch(
        soil_marginality_factor=_NEUTRAL_FACTOR_VALUE,
        hydric_overlap_factor=_NEUTRAL_FACTOR_VALUE,
        stream_proximity_factor=_NEUTRAL_FACTOR_VALUE,
    )
) == [NUTRIENT, STREAM], "the same three values with every gate True earn both gated benefits"

print(
    f"1 [test 1]. GATE CHECK: each of the three gated factors, set to the neutral "
    f"{_NEUTRAL_FACTOR_VALUE} with ITS OWN gate False, produces no benefit -- "
    f"{[f'{g} -> no {b!r}' for g, b in _gate_check]} -- and each one's control (the same "
    f"value, gate True) does produce it. With all three ungated at once the list is EMPTY; "
    f"with all three gated True the same three values earn ['{NUTRIENT}', '{STREAM}']. "
    f"A plain above-zero rule would have claimed both on unfetched data."
)


# =====================================================================
# 2. A factor above zero behind a True gate DOES produce its benefit
# =====================================================================

_earned_by = {}
for factor_key, benefit in (
    ("slope_factor", EROSION),
    ("soil_marginality_factor", NUTRIENT),
    ("hydric_overlap_factor", NUTRIENT),
    ("stream_proximity_factor", STREAM),
):
    # A deliberately SMALL positive value: the rule is above zero, not
    # above some unstated significance threshold.
    earned = marginal_benefits(_patch(**{factor_key: 0.001}))
    assert earned == [benefit], (f"{factor_key}=0.001 with its gate True must earn exactly [{benefit!r}], got {earned}")
    # And exactly zero earns nothing -- the boundary is strict.
    assert marginal_benefits(_patch(**{factor_key: 0.0})) == [], factor_key
    _earned_by[factor_key] = benefit

# EVERY factor positive and every gate True -> every benefit, in
# MARGINAL_BENEFIT_FACTOR_SOURCES' declared order, which is a contract: a
# panel renders the list as given, and two zones must list their shared
# benefits in the same order.
_all_earned = marginal_benefits(
    _patch(slope_factor=0.4, soil_marginality_factor=1.0, hydric_overlap_factor=0.8, stream_proximity_factor=0.2)
)
assert _all_earned == [EROSION, NUTRIENT, STREAM], _all_earned
assert _all_earned == list(MARGINAL_BENEFITS), "emission order must be the declared order"
# NO VALUES ON A BENEFIT -- plain strings, nothing to read a number off.
assert all(isinstance(b, str) for b in _all_earned)
# And no duplicate when BOTH of nutrient deposition's sources fire.
assert _all_earned.count(NUTRIENT) == 1, "two qualifying sources must still name the benefit once"

print(
    f"2 [test 2]. EARNED: each factor at a deliberately tiny 0.001 with its gate True earns "
    f"exactly its own benefit ({ {k: v for k, v in _earned_by.items()} }), and exactly 0.0 "
    f"earns nothing -- the boundary is strictly above zero. All four positive earns "
    f"{_all_earned}, in the declared order, plain strings with no values attached, and "
    f"nutrient deposition appears ONCE even though both its sources fired."
)


# =====================================================================
# 3. NUTRIENT DEPOSITION -- the OR, and the gate inside it
# =====================================================================
#
# The one benefit with two sources. Each is independently sufficient, each
# carries its OWN gate, and the interesting case is the mixed one: a
# factor that is neutral-and-ungated sitting beside one that is real.

_soil_only = _patch(soil_marginality_factor=1.0, hydric_overlap_factor=0.0)
_hydric_only = _patch(soil_marginality_factor=0.0, hydric_overlap_factor=1.0)
assert marginal_benefits(_soil_only) == [NUTRIENT], "soil marginality alone earns it"
assert marginal_benefits(_hydric_only) == [NUTRIENT], "hydric overlap alone earns it"

# BOTH NEUTRAL AND UNGATED -> NOT EARNED. Two fallbacks are not evidence,
# however many of them there are.
_both_neutral_ungated = _patch(
    soil_marginality_factor=_NEUTRAL_FACTOR_VALUE,
    hydric_overlap_factor=_NEUTRAL_FACTOR_VALUE,
    soil_marginality_data_available=False,
    hydric_data_available=False,
)
assert marginal_benefits(_both_neutral_ungated) == [], marginal_benefits(_both_neutral_ungated)

# ONE NEUTRAL-AND-UNGATED, THE OTHER REAL -> EARNED, from the real one.
# Asserted in both directions, so neither source can be the one the rule
# happens to look at.
_soil_dead_hydric_real = _patch(
    soil_marginality_factor=_NEUTRAL_FACTOR_VALUE,
    soil_marginality_data_available=False,
    hydric_overlap_factor=1.0,
    hydric_data_available=True,
)
_hydric_dead_soil_real = _patch(
    hydric_overlap_factor=_NEUTRAL_FACTOR_VALUE,
    hydric_data_available=False,
    soil_marginality_factor=1.0,
    soil_marginality_data_available=True,
)
assert marginal_benefits(_soil_dead_hydric_real) == [NUTRIENT], (
    "a neutral, ungated soil value beside a REAL hydric signal still earns nutrient deposition"
)
assert marginal_benefits(_hydric_dead_soil_real) == [NUTRIENT], (
    "a neutral, ungated hydric value beside a REAL soil signal still earns nutrient deposition"
)

# A gate True over a genuinely MEASURED zero earns nothing either -- the
# gate is not a second way in.
assert marginal_benefits(_patch(soil_marginality_factor=0.0, hydric_overlap_factor=0.0)) == []

print(
    f"3 [test 3]. NUTRIENT DEPOSITION: earned from soil marginality alone and from hydric "
    f"overlap alone; NOT earned when both sit at the neutral {_NEUTRAL_FACTOR_VALUE} with both "
    f"gates False; earned in BOTH mixed directions -- a neutral-and-ungated soil value beside a "
    f"real hydric signal, and a neutral-and-ungated hydric value beside a real soil signal. "
    f"Two measured zeros behind True gates earn nothing, so the gate is not a second way in."
)


# =====================================================================
# 4. A zone that qualifies for nothing ships an EMPTY LIST
# =====================================================================

assert marginal_benefits(_patch()) == [], "every factor at zero earns nothing"
assert marginal_benefits(_patch()) is not None, "a zone with no benefits ships [], never None"
assert isinstance(marginal_benefits(_patch()), list)

# Through the narrative block, which is where a panel actually reads it.
_nothing_boundary = box(500000.0, 4499800.0, 500200.0, 4500000.0)
_nothing_row = build_narrative_data(
    [
        {
            "rank": 1,
            "area_acres": 0.5,
            "tree_suitability_score": 35.0,
            "avg_slope_pct": 0.0,
            "slope_median_pct": 0.0,
            "elevation_percentile_of_parcel": 12.0,
            "render_fill_polygon_utm": box(500010.0, 4499810.0, 500040.0, 4499840.0),
            **_patch(),
        }
    ],
    boundary_polygon_utm=_nothing_boundary,
    boundary_acres=10.0,
    claimed_acres=4.0,
    search_space_acres=6.0,
    soil_marginality_data_available=True,
    hydric_data_available=True,
    stream_data_available=True,
    existing_canopy_excluded=True,
)["zones"][0]
assert _nothing_row["marginal_benefits"] == [], _nothing_row["marginal_benefits"]
assert "marginal_benefits" in _nothing_row, "the key is always present; it is the LIST that is empty"

print(
    "4 [test 4]. NOTHING EARNED: a zone whose factors qualify it for no benefit at all ships "
    "an EMPTY LIST, not None and not a missing key -- the key is always there, so a panel "
    "renders no benefits section rather than having to distinguish absent from empty."
)


# =====================================================================
# 5. elevation_position's words are PRODUCTION's bands, imported
# =====================================================================
#
# The assertion is against the IMPORTED constant. A copy of the three
# words here would keep passing while production retuned its cuts
# underneath, which is exactly the drift production_area_ceiling.py's
# module docstring forbids a second definition in order to prevent.

_band_boundary = box(500000.0, 4499800.0, 500200.0, 4500000.0)


def _rows_for(percentiles):
    """One narrative zone row per percentile, everything else fixed."""
    return build_narrative_data(
        [
            {
                "rank": rank,
                "area_acres": 0.5,
                "tree_suitability_score": 50.0,
                "avg_slope_pct": 10.0,
                "slope_median_pct": 9.0,
                "elevation_percentile_of_parcel": percentile,
                "render_fill_polygon_utm": box(500010.0, 4499810.0, 500040.0, 4499840.0),
                **_patch(slope_factor=0.5),
            }
            for rank, percentile in enumerate(percentiles, start=1)
        ],
        boundary_polygon_utm=_band_boundary,
        boundary_acres=10.0,
        claimed_acres=4.0,
        search_space_acres=6.0,
        soil_marginality_data_available=True,
        hydric_data_available=True,
        stream_data_available=True,
        existing_canopy_excluded=True,
    )["zones"]


# Every band's own bounds, read off the constant: its low edge (inclusive),
# a point inside it, and the value just under its high edge (exclusive).
_sweep = []
for word, (low, high) in ELEVATION_POSITION_BANDS.items():
    _sweep.extend([(low, word), ((low + high) / 2.0, word), (round(high - 0.1, 1), word)])
# The top band closes AT 100.0, which the exclusive upper bound cannot
# match -- so it is asserted separately, against the constant's own
# highest band rather than against the string "upper field".
_top_word = max(ELEVATION_POSITION_BANDS.items(), key=lambda kv: kv[1][0])[0]
_sweep.append((100.0, _top_word))

for row, (percentile, expected_word) in zip(_rows_for([p for p, _ in _sweep]), _sweep):
    assert row["elevation_percentile_of_parcel"] == percentile
    assert row["elevation_position"] == expected_word, (
        f"percentile {percentile} must read as {expected_word!r} per the IMPORTED bands, got "
        f"{row['elevation_position']!r}"
    )
    # And the word's own band really does contain the number it came
    # from -- checked against the constant, not against the expectation.
    band_low, band_high = ELEVATION_POSITION_BANDS[row["elevation_position"]]
    assert band_low <= percentile <= band_high, (percentile, row["elevation_position"])

# No word this module can emit is outside production's vocabulary.
_words = {row["elevation_position"] for row in _rows_for([p for p, _ in _sweep])}
assert _words <= set(ELEVATION_POSITION_BANDS), _words
# The bands really are production's object, not an equal-looking copy
# that happens to be in this module's namespace today.
assert ELEVATION_POSITION_BANDS is tzc.ELEVATION_POSITION_BANDS, (
    "trees must IMPORT production's bands, never declare its own"
)

print(
    f"5 [test 5]. ELEVATION POSITION: {len(_sweep)} percentiles -- each band's inclusive low "
    f"edge, its midpoint and the value just under its exclusive high edge, plus 100.0 on the "
    f"closing top band -- all read as the word production_area_ceiling.ELEVATION_POSITION_BANDS "
    f"gives them ({sorted(_words)}), asserted against the IMPORTED constant and confirmed to be "
    f"the same object trees holds. No word outside production's vocabulary is reachable."
)


# =====================================================================
# 6. A None percentile yields a None position -- never a default word
# =====================================================================

_none_row = _rows_for([None])[0]
assert _none_row["elevation_percentile_of_parcel"] is None
assert _none_row["elevation_position"] is None, (
    f"a None percentile must never be given a band word -- got {_none_row['elevation_position']!r}"
)
assert _none_row["elevation_position"] not in ELEVATION_POSITION_BANDS

print(
    "6 [test 6]. NULL, NOT A DEFAULT: a None percentile (a parcel with no elevation relief at "
    "all, where 'upper' and 'lower' name nothing) yields a None position -- never the lowest "
    "band, never a middle default, never any word at all."
)


# =====================================================================
# 7. END TO END through score_tree_search_space()
# =====================================================================
#
# Everything above is the rule in isolation. This section runs the real
# scorer over hand-built DEMs and asserts the same three things on the
# patches it actually emits.

ROWS = COLS = 44
RESOLUTION_METERS = 5.0
ORIGIN_X, ORIGIN_Y = 500000.0, 4500220.0
_BOUNDARY = box(
    ORIGIN_X + 10.0,
    ORIGIN_Y - ROWS * RESOLUTION_METERS + 10.0,
    ORIGIN_X + COLS * RESOLUTION_METERS - 10.0,
    ORIGIN_Y - 10.0,
)
_SEARCH_SPACE = box(
    ORIGIN_X + 30.0,
    ORIGIN_Y - 40 * RESOLUTION_METERS,
    ORIGIN_X + 30.0 + 24 * RESOLUTION_METERS,
    ORIGIN_Y - 4 * RESOLUTION_METERS,
)


def _dem_from(array):
    return {
        "array": array.astype(np.float32),
        "resolution_meters": (RESOLUTION_METERS, RESOLUTION_METERS),
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": "EPSG:32617",
    }


def _cells_of(patch, dem):
    """The DEM cells the patch is made of, recovered from its footprint by
    the SAME cell-center convention the scorer used to build it. Nothing
    in the emitted patch carries its cell list, so this is how a test
    gets at the array the scorer averaged -- and the mean coming back
    exactly is what proves it is that array and not a similar one."""
    return [
        (r, c)
        for r in range(ROWS)
        for c in range(COLS)
        if patch["polygon_utm"].contains(Point(pixel_center_xy(dem, r, c)))
    ]


# --- 7a. THE SKEWED RAMP: median beside mean, over the same cells -----
#
# Two grades, deliberately unequal in extent (13 rows at 30%, the rest at
# 100%) so the mean and the median come out DIFFERENT. A fixture where
# they agreed would pass against a slope_median_pct that was secretly the
# mean.

_ramp = np.zeros((ROWS, COLS), dtype=np.float64)
_elevation = 300.0
for _row in range(ROWS):
    _ramp[_row, :] = _elevation
    _elevation += (0.30 if _row < 34 else 1.00) * RESOLUTION_METERS
RAMP_DEM = _dem_from(_ramp)

_ramp_patches = score_tree_search_space(RAMP_DEM, _SEARCH_SPACE, _BOUNDARY)
assert len(_ramp_patches) == 1, f"the ramp fixture must produce exactly one patch, got {len(_ramp_patches)}"
RAMP = _ramp_patches[0]

_slope_grid = compute_slope_percent(_ramp, (RESOLUTION_METERS, RESOLUTION_METERS))
_ramp_cells = _cells_of(RAMP, RAMP_DEM)
assert _ramp_cells, "the footprint must cover DEM cell centers or this section proves nothing"
_ramp_slopes = [
    float(_slope_grid[r, c]) if not np.isnan(_slope_grid[r, c]) else 0.0 for r, c in _ramp_cells
]

# THE MEAN COMES BACK EXACTLY -- which is the proof that the array
# reconstructed here IS the array the scorer used.
assert RAMP["avg_slope_pct"] == round(float(np.mean(_ramp_slopes)), 1), (
    RAMP["avg_slope_pct"], float(np.mean(_ramp_slopes))
)
# AND THE MEDIAN IS THE MEDIAN OF THAT SAME ARRAY.
assert RAMP["slope_median_pct"] == round(float(np.median(_ramp_slopes)), 1), (
    RAMP["slope_median_pct"], float(np.median(_ramp_slopes))
)
# NON-VACUOUS: on this fixture they genuinely differ, so the assertion
# above could not be satisfied by publishing the mean twice.
assert RAMP["slope_median_pct"] != RAMP["avg_slope_pct"], (
    "the fixture must skew the slope distribution, or 'the median is the median' is untestable"
)
# THE MEAN STAYS. The report reads it; slope_factor was computed from it.
assert "avg_slope_pct" in RAMP and "slope_median_pct" in RAMP

# THE PERCENTILE, against the parcel's OWN low-to-high range -- measured
# over the full boundary, not over the search space.
_on_parcel = _ramp[_on_parcel_cell_mask(RAMP_DEM, _BOUNDARY)]
_low, _high = float(_on_parcel.min()), float(_on_parcel.max())
_mean_elevation = float(np.mean([float(_ramp[r, c]) for r, c in _ramp_cells]))
assert RAMP["elevation_percentile_of_parcel"] == round(
    max(0.0, min(100.0, (_mean_elevation - _low) / (_high - _low) * 100.0)), 1
), (RAMP["elevation_percentile_of_parcel"], _mean_elevation, _low, _high)

# Steep, non-hydric, non-prime, no stream, every gate True -> erosion
# control and nutrient deposition (the soil is measured non-prime, which
# is a real 1.0), and NO stream protection off a measured 0.0.
assert marginal_benefits(RAMP) == [EROSION, NUTRIENT], marginal_benefits(RAMP)
assert RAMP["stream_proximity_factor"] == 0.0 and RAMP["stream_data_available"] is True

# --- 7b. FLAT AND HYDRIC: a None percentile off a real parcel ---------
#
# A parcel with no relief at all. Its zone still qualifies (hydric alone
# clears the floor with no slope signal whatsoever), so this is a real
# scored patch whose percentile is genuinely None -- not a hand-built one.

FLAT_DEM = _dem_from(np.full((ROWS, COLS), 300.0, dtype=np.float64))
_flat_patches = score_tree_search_space(
    FLAT_DEM, _SEARCH_SPACE, _BOUNDARY, hydric_union=_SEARCH_SPACE, hydric_data_available=True
)
assert len(_flat_patches) == 1
FLAT_HYDRIC = _flat_patches[0]
assert FLAT_HYDRIC["elevation_percentile_of_parcel"] is None, (
    "a parcel with no elevation relief has no low-to-high range to position a zone within"
)
assert FLAT_HYDRIC["slope_median_pct"] == 0.0 and FLAT_HYDRIC["avg_slope_pct"] == 0.0
assert FLAT_HYDRIC["slope_factor"] == 0.0
# Flat ground earns NO erosion control -- a measured zero, not a gate.
assert marginal_benefits(FLAT_HYDRIC) == [NUTRIENT], marginal_benefits(FLAT_HYDRIC)

# --- 7c. THE OUTAGE: a real zone that earns nothing -------------------
#
# THE FAILURE THIS BRANCH PREVENTS, end to end. The same flat parcel with
# all three data sources unreachable: every gated factor falls back to the
# neutral 0.5, the composite clears the floor on those fallbacks alone,
# and the zone is emitted. Under a plain above-zero rule this zone would
# display nutrient deposition AND stream protection -- both computed from
# data nobody ever fetched.

_outage_patches = score_tree_search_space(
    FLAT_DEM,
    _SEARCH_SPACE,
    _BOUNDARY,
    prime_farmland_data_available=False,
    hydric_data_available=False,
    stream_data_available=False,
)
assert len(_outage_patches) == 1
OUTAGE = _outage_patches[0]
assert OUTAGE["tree_suitability_score"] >= tzc.MIN_TREE_SUITABILITY_SCORE, (
    "the outage zone must really qualify, or it is not the case this test is about"
)
for _factor in ("soil_marginality_factor", "hydric_overlap_factor", "stream_proximity_factor"):
    assert OUTAGE[_factor] == _NEUTRAL_FACTOR_VALUE, (_factor, OUTAGE[_factor])
assert marginal_benefits(OUTAGE) == [], (
    f"a zone scored entirely on neutral fallbacks must claim NO benefit, got {marginal_benefits(OUTAGE)}"
)

# The narrative block a panel reads carries all of it, on real patches.
_rows = build_narrative_data(
    [RAMP],
    boundary_polygon_utm=_BOUNDARY,
    boundary_acres=_BOUNDARY.area / 4046.8564224,
    claimed_acres=0.0,
    search_space_acres=_SEARCH_SPACE.area / 4046.8564224,
    soil_marginality_data_available=True,
    hydric_data_available=True,
    stream_data_available=True,
    existing_canopy_excluded=True,
)["zones"]
assert _rows[0]["slope_median_pct"] == RAMP["slope_median_pct"]
assert _rows[0]["elevation_percentile_of_parcel"] == RAMP["elevation_percentile_of_parcel"]
assert _rows[0]["elevation_position"] in ELEVATION_POSITION_BANDS
assert _rows[0]["marginal_benefits"] == [EROSION, NUTRIENT]

print(
    f"7 [test 7]. END TO END through score_tree_search_space(): on a deliberately SKEWED ramp "
    f"the emitted patch's avg_slope_pct ({RAMP['avg_slope_pct']}%) is the mean and "
    f"slope_median_pct ({RAMP['slope_median_pct']}%) is the median OF THE SAME reconstructed "
    f"per-cell array over {len(_ramp_cells)} cells -- the mean matching exactly is what proves "
    f"it is that array, and the two differing is what makes the median assertion real. Its "
    f"elevation_percentile_of_parcel ({RAMP['elevation_percentile_of_parcel']}) matches the "
    f"parcel's own {_low:.0f}-{_high:.0f} m range, reading as "
    f"{_rows[0]['elevation_position']!r}. A flat hydric parcel gives a real scored zone with a "
    f"None percentile and {marginal_benefits(FLAT_HYDRIC)}; THE SAME FLAT PARCEL WITH ALL THREE "
    f"SOURCES DOWN gives a zone that really qualifies ({OUTAGE['tree_suitability_score']}/100, "
    f"every gated factor at {_NEUTRAL_FACTOR_VALUE}) and claims NOTHING -- where an above-zero "
    f"rule would have displayed '{NUTRIENT}' and '{STREAM}' off data that was never fetched."
)


print("\nAll tree zone panel-data checks passed.")
