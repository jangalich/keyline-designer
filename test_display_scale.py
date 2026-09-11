"""
Offline checks for the KSOP DISPLAY SCALE -- display_scale.py and the
water step's use of it. No network, no fetch; a small synthetic DEM for
the end-to-end half.

WHAT THIS BRANCH DID AND DID NOT DO, which is what these checks are
about. Water's suitability is SHOWN on 0-100 (so every KSOP step can
grade zones on one scale and a reader moving between panels does not
re-learn the units) and is COMPUTED, STORED, RANKED AND THRESHOLDED on
0-1 exactly as before. The failure mode a display scale invites is
therefore not a wrong number -- it is TWO scales: a stored field quietly
rescaled, a second multiplier growing at another call site, or a value
shown without saying which scale it is on. Every section below is aimed
at one of those three.

Sections:
  1. THE HELPER -- the conversion and its stated rounding rule.
  2. THE PANEL -- the converted row, its unit, and no other row touched.
  3. STORED VALUES UNCHANGED -- the zone dict, the tabular block and the
     GeoJSON feature properties, asserted explicitly. THIS IS THE
     REGRESSION THAT MATTERS.
  4. ONE CONVERSION POINT -- AST-level: no site outside the helper
     multiplies a suitability by 100.
  5. PANEL / REPORT / DIAGNOSTIC AGREE on the same fixture.
  6. FULL-CONTEXT SYNTHETIC -- the consumer contract and
     selected_water_zone unchanged end to end.
"""

import ast
import inspect
import json

import numpy as np
from shapely.geometry import box

import display_scale
import report_generator
import water_survey_areas as wsa
from display_scale import (
    DISPLAY_SCALE_MAX,
    DISPLAY_SCALE_MIN,
    DISPLAY_SCALE_UNIT,
    to_display_scale,
)
from water_survey_areas import (
    SURVEY_TYPE_EMBANKMENT,
    SURVEY_TYPE_EXCAVATED,
    SURVEY_TYPES,
    build_narrative_data,
    build_scales,
    build_zone_panel,
    compute_water_survey_areas,
    summarize_water_survey_areas,
)

# ======================================================================
# 1. THE HELPER
# ======================================================================

assert (DISPLAY_SCALE_MIN, DISPLAY_SCALE_MAX) == (0, 100)
assert DISPLAY_SCALE_UNIT == "/100"

# The four stated conversions, one of which is the reference parcel's own
# excavated ceiling.
for _internal, _display in ((0.0, 0), (0.4406, 44), (0.8719, 87), (1.0, 100)):
    assert to_display_scale(_internal) == _display, (
        f"{_internal} must read {_display} on the display scale, got {to_display_scale(_internal)}"
    )
    assert isinstance(to_display_scale(_internal), int), (
        "a 0-100 grade is a WHOLE number -- 44.06 is the fraction with the decimal point moved"
    )

# ROUNDING AT .5 IS STATED, NOT DISCOVERED: round(value * 100), which is
# Python's round-half-to-EVEN on the product. Asserted on values that are
# exactly representable in binary (0.125, 0.375) and on two that land
# exactly on .5 after the multiply, so the rule is pinned in both
# directions rather than by one example that could be either rule.
assert to_display_scale(0.125) == 12, "12.5 rounds DOWN to the even 12"
assert to_display_scale(0.375) == 38, "37.5 rounds UP to the even 38"
assert to_display_scale(0.445) == 44, "44.5 rounds DOWN to the even 44"
assert to_display_scale(0.455) == 46, "45.5 rounds UP to the even 46"

# None PASSES THROUGH. "Not measured" is a value this pipeline carries
# honestly end to end and a converted 0 would report an unmeasured thing
# as a measured floor -- the same coercion the overlap sentinels exist to
# prevent.
assert to_display_scale(None) is None

# NO CLAMP. A value outside 0-1 is a scoring defect and must surface as a
# visibly wrong grade rather than be folded to an endpoint by the display
# layer, where nobody would ever see it.
assert to_display_scale(1.4) == 140 and to_display_scale(-0.2) == -20

print(
    "1. HELPER: 0.0 -> 0, 0.4406 -> 44, 0.8719 -> 87, 1.0 -> 100, whole numbers throughout; "
    "round-half-to-even stated and pinned in both directions (0.125 -> 12, 0.375 -> 38); None "
    "passes through; nothing is clamped."
)


# ======================================================================
# 2. THE PANEL
# ======================================================================
# Hand-built zones, so the assertion is about the row set rather than
# about whatever a synthetic DEM happened to score.

def _zone(**overrides):
    zone = {
        "id": 0,
        "survey_type": SURVEY_TYPE_EXCAVATED,
        "rank": 1,
        "zone_acres": 1.2345,
        "mean_suitability": 0.4406,
        "confidence": wsa.CONFIDENCE_HIGH,
        "primary_production_area_relationship": None,
        "canopy_overlap_pct": 0.0,
        "road_overlap_pct": 0.0,
        "production_overlap_pct": 0.0,
        "cross_type_overlaps": [],
        "truncated_by_boundary": False,
        "truncated_by_road": False,
        "sparse_anchor": False,
        "below_min_area": False,
        "pinch_terminal": None,
        "still_narrowing_at_termination": False,
    }
    zone.update(overrides)
    return zone


_clean = _zone()
_rows = build_zone_panel(_clean, True, {})
_by_key = {row["key"]: row for row in _rows}

assert _by_key["suitability"]["value"] == 44, (
    f"the suitability row is the converted reading: {_by_key['suitability']['value']!r}"
)
assert _by_key["suitability"]["unit"] == DISPLAY_SCALE_UNIT, (
    "AMBIGUITY GUARD: a bare 44 could be read as either scale, which is worse than either alone "
    "-- the row says which at its own point of use"
)
assert _by_key["suitability"]["label"] == "suitability", (
    "the scale rides the UNIT field, not the label -- a label that spelled it too would render "
    "as 'suitability /100 (/100)'"
)

# NO OTHER ROW CHANGED. Every other panel value is the zone's own number
# in the unit it was measured in, asserted against the zone dict itself
# rather than against a remembered literal.
assert _by_key["zone_acres"]["value"] == round(_clean["zone_acres"], 1) == 1.2
assert _by_key["zone_acres"]["unit"] == "acres"
assert _by_key["survey_type"]["value"] == _clean["survey_type"]
assert _by_key["rank"]["value"] == _clean["rank"] == 1
assert _by_key["water_delivery"]["value"] == wsa.WATER_DELIVERY_NONE
assert [row["key"] for row in _rows] == list(wsa.PANEL_ALWAYS_ROWS)

# The widest panel a zone can produce: exactly ONE row carries the
# display unit, and it is the suitability row. A second converted row
# appearing without a test change is the drift this asserts against.
_wide = build_zone_panel(
    _zone(
        survey_type=SURVEY_TYPE_EMBANKMENT,
        confidence=wsa.CONFIDENCE_LOW,
        canopy_overlap_pct=12.5,
        road_overlap_pct=None,
        production_overlap_pct=2.0,
        pinch_terminal="boundary",
        still_narrowing_at_termination=True,
        truncated_by_boundary=True,
        truncated_by_road=True,
        sparse_anchor=True,
        below_min_area=True,
        cross_type_overlaps=[{"zone_id": 7, "fraction": 0.61}],
        primary_production_area_relationship={
            "above_production_area": True,
            "elevation_differential_m": 6.096,
            "production_area_id": 3,
        },
    ),
    False,
    {7: "excavated 1"},
)
assert [row["key"] for row in _wide if row["unit"] == DISPLAY_SCALE_UNIT] == ["suitability"], (
    "exactly one row on the widest panel carries the display unit"
)
# The overlap percentages are NOT suitabilities and are NOT converted --
# they are already percentages of an area and were computed as such.
_wide_by_key = {row["key"]: row for row in _wide}
assert _wide_by_key["canopy_overlap_pct"]["value"] == 12.5
assert _wide_by_key["canopy_overlap_pct"]["unit"] == "percent"
assert _wide_by_key["road_overlap_pct"]["value"] is None, "the never-checked sentinel survives"

print(
    "2. PANEL: the suitability row reads 44 with unit '/100' off a stored 0.4406; the label does "
    "not double-spell the scale; no other always-row or caution row changed, and exactly one row "
    "on the widest possible panel carries the display unit."
)


# ======================================================================
# 3. STORED VALUES UNCHANGED -- THE REGRESSION THAT MATTERS
# ======================================================================
# A display scale's characteristic failure is a stored field quietly
# following the display. These assertions are made on a REAL result --
# the zone dict, the narrative block and the GeoJSON feature properties
# -- not on a hand-built fixture, because the hand-built one cannot
# catch a conversion introduced in the measurement path.

# The V-VALLEY FIXTURE, the same shape test_water_survey_areas.py uses
# for extraction geometry: a uniform side grade into one channel column,
# with the channel's accumulation supplied so the run does not depend on
# how the epsilon fill resolves a synthetic flat. It qualifies end to end
# and produces BOTH survey types, which is what section 3 needs -- the
# embankment-only fields are exactly the ones a careless conversion pass
# would take with it.
_RESOLUTION = 5.0
_ORIGIN_X, _ORIGIN_Y = 500000.0, 4500000.0
_ROWS, _COLS, _CHANNEL = 40, 21, 10
_array = np.zeros((_ROWS, _COLS))
for _r in range(_ROWS):
    for _c in range(_COLS):
        _array[_r, _c] = 100.0 + abs(_c - _CHANNEL) * 0.30 - _r * 0.25
_dem = {
    "array": _array.astype(np.float64),
    "resolution_meters": (_RESOLUTION, _RESOLUTION),
    "origin_x": _ORIGIN_X,
    "origin_y": _ORIGIN_Y,
    "crs": "EPSG:32617",
}
_boundary_utm = box(
    _ORIGIN_X + 2 * _RESOLUTION + 0.1,
    _ORIGIN_Y - 38 * _RESOLUTION + 0.1,
    _ORIGIN_X + 19 * _RESOLUTION - 0.1,
    _ORIGIN_Y - 2 * _RESOLUTION - 0.1,
)
_accumulation = np.ones((_ROWS, _COLS))
for _r in range(_ROWS):
    _accumulation[_r, _CHANNEL] = 60 * (_r + 1)

_result = compute_water_survey_areas(_dem, _boundary_utm, flow_accumulation=_accumulation)
assert _result["zones"], "the synthetic bowl must produce at least one zone to assert against"

_narrative = build_narrative_data(_result)
_collection = wsa.survey_areas_to_geojson(_result["zones"], _result["dropped_zones"])
_ZONE_LAYERS = {f"survey_zone_{survey_type}" for survey_type in SURVEY_TYPES}
_features_by_id = {
    feature["properties"]["zone_id"]: feature
    for feature in _collection["features"]
    if feature["properties"]["layer"] in _ZONE_LAYERS
}
assert len(_features_by_id) == len(_result["zones"]), (
    "one zone feature per surviving zone -- the member layers are not zones"
)
_blocks_by_id = {block["id"]: block for block in _narrative["zones"]}

_ZERO_ONE_ZONE_FIELDS = ("mean_suitability", "max_suitability", "twi_score_mean", "twi_score_max")

for _zone_record in _result["zones"]:
    _zid = _zone_record["id"]
    _feature = _features_by_id[_zid]
    _block = _blocks_by_id[_zid]

    for _field in _ZERO_ONE_ZONE_FIELDS:
        assert 0.0 <= _zone_record[_field] <= 1.0, (
            f"zone {_zid}: {_field} left the 0-1 scale ({_zone_record[_field]!r}) -- the display "
            f"scale is a READING and must never reach a stored value"
        )
        assert _feature["properties"][_field] == _zone_record[_field], (
            f"zone {_zid}: the GeoJSON feature's {_field} must be the zone's own stored value"
        )
    assert _block["mean_suitability"] == _zone_record["mean_suitability"]
    assert _block["max_suitability"] == _zone_record["max_suitability"]

    # EVERY CRITERION'S mean_score, on the zone, on the feature and in
    # the narrative block. This is the field a careless "convert the
    # scores" pass takes with it.
    for _name, _entry in _zone_record["criterion_contributions"].items():
        assert 0.0 <= _entry["mean_score"] <= 1.0, (
            f"zone {_zid}: criterion {_name} mean_score {_entry['mean_score']!r} left 0-1"
        )
        assert (
            _feature["properties"]["criterion_contributions"][_name]["mean_score"]
            == _entry["mean_score"]
        )
        assert _block["criteria"][_name]["mean_score"] == _entry["mean_score"]
        assert _block["criteria"][_name]["weight"] == _entry["weight"]

    if _zone_record["survey_type"] == SURVEY_TYPE_EMBANKMENT:
        for _field in ("seed_blend_score", "pinch_drainage_score", "compartment_rank_score"):
            assert 0.0 <= _zone_record[_field] <= 1.0, (
                f"zone {_zid}: {_field} left 0-1 -- the seed minimum, the drainage band and the "
                f"ranking composite are INTERNAL and this branch changed none of them"
            )
            assert _feature["properties"][_field] == _zone_record[_field]
            assert _block[_field] == _zone_record[_field]

    # AND THE PANEL, built from this same zone, shows the converted
    # reading of the value the zone still holds.
    _panel_suitability = next(
        row for row in _block["panel"] if row["key"] == "suitability"
    )
    assert _panel_suitability["value"] == to_display_scale(_zone_record["mean_suitability"])
    assert _panel_suitability["unit"] == DISPLAY_SCALE_UNIT

# The threshold and the seed minimum are CONSTANTS on the internal scale
# and are not display values at all.
assert 0.0 < wsa.SUITABILITY_THRESHOLD <= 1.0
assert 0.0 < wsa.EMBANKMENT_SEED_MIN_SCORE <= 1.0
assert _narrative["suitability_threshold"] == _result["threshold"] <= 1.0, (
    "the threshold on the wire is the internal one -- a converted threshold beside converted "
    "values is how a display scale becomes a scoring change"
)

json.dumps(_narrative)

print(
    f"3. STORED VALUES: across {len(_result['zones'])} real zone(s), mean/max suitability, every "
    f"criterion mean_score, and the embankment seed blend / drainage / rank composite are 0-1 and "
    f"IDENTICAL on the zone dict, the narrative block and the GeoJSON feature; the panel's "
    f"converted row sits beside them without touching them; the threshold on the wire is still "
    f"internal."
)


# ======================================================================
# 4. ONE CONVERSION POINT
# ======================================================================
# TWO INDEPENDENT MULTIPLIERS IS HOW A DISPLAY SCALE SILENTLY BECOMES
# TWO DIFFERENT SCALES -- one site gains a round(), another a clamp, and
# the panel and the report disagree about the same zone while both look
# right in isolation. So: outside display_scale.py, no audited module may
# multiply anything suitability-shaped by 100.
#
# AST-LEVEL rather than grep, per house pattern: a comment or a docstring
# arguing ABOUT the conversion must stay legal, and only real arithmetic
# counts.

_AUDITED = (wsa, report_generator)
_SCORE_WORDS = (
    "suitability",
    "mean_score",
    "blend_score",
    "rank_score",
    "drainage_score",
    "twi_score",
    "parcel_observed_max",
    "display_scale",
)


def _mentions_a_score(node) -> bool:
    """Does this expression name anything score-shaped? Reads NAMES and
    STRING SUBSCRIPTS -- zone["mean_suitability"] is the spelling every
    call site in this pipeline actually uses."""
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and any(w in child.id for w in _SCORE_WORDS):
            return True
        if isinstance(child, ast.Attribute) and any(w in child.attr for w in _SCORE_WORDS):
            return True
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if any(w in child.value for w in _SCORE_WORDS):
                return True
    return False


_multiplications = []
for _module in _AUDITED:
    _tree = ast.parse(inspect.getsource(_module))
    for _node in ast.walk(_tree):
        if not isinstance(_node, ast.BinOp) or not isinstance(_node.op, ast.Mult):
            continue
        _operands = (_node.left, _node.right)
        _hundred = [
            side
            for side in _operands
            if isinstance(side, ast.Constant) and side.value in (100, 100.0)
        ]
        if not _hundred:
            # A multiply by the display constant by any other name is the
            # same multiply.
            _hundred = [
                side
                for side in _operands
                if isinstance(side, ast.Name) and side.id == "DISPLAY_SCALE_MAX"
            ]
        if not _hundred:
            continue
        _multiplications.append((_module.__name__, _node.lineno))
        assert not _mentions_a_score(_node), (
            f"{_module.__name__}:{_node.lineno} multiplies a score by 100 outside the helper -- "
            f"the display conversion has ONE call site (display_scale.to_display_scale) and a "
            f"second multiplier is how one scale becomes two"
        )

# The multiplications that ARE here are fraction -> percent on areas, and
# they are unrelated: a percentage of an area is not a grade.
assert _multiplications, (
    "the audit found no multiplications at all, which means it is not looking where it thinks -- "
    "water_survey_areas converts overlap and adjacency fractions to percent"
)

# AND THE HELPER IS THE ONE PLACE THE CONSTANT IS APPLIED. Asserted on
# display_scale.py's own source so the module cannot grow a second,
# differently-rounded entry point.
_helper_tree = ast.parse(inspect.getsource(display_scale))
_helper_multiplies = [
    node
    for node in ast.walk(_helper_tree)
    if isinstance(node, ast.BinOp)
    and isinstance(node.op, ast.Mult)
    and any(
        isinstance(side, ast.Name) and side.id == "DISPLAY_SCALE_MAX"
        for side in (node.left, node.right)
    )
]
assert len(_helper_multiplies) == 1, (
    f"the helper module applies the display constant exactly once, found {len(_helper_multiplies)}"
)
_helper_functions = [
    node.name
    for node in ast.walk(_helper_tree)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
]
assert _helper_functions == ["to_display_scale"], (
    f"one helper, one conversion: {_helper_functions}"
)

print(
    f"4. ONE CONVERSION POINT: {len(_multiplications)} multiplication(s) by 100 across "
    f"{', '.join(m.__name__ for m in _AUDITED)}, AST-checked -- every one of them a "
    f"fraction-to-percent on an area, none of them a score; display_scale.py applies the constant "
    f"exactly once, in its single function."
)


# ======================================================================
# 5. PANEL / REPORT / DIAGNOSTIC AGREE
# ======================================================================
# Three renderings of one zone, off one fixture. They may word it
# differently; they may not disagree about the number.

_summary = summarize_water_survey_areas(_result)
_prose = report_generator._format_water_survey_areas_summary(_narrative)

for _zone_record in _result["zones"]:
    _shown = to_display_scale(_zone_record["mean_suitability"])
    _block = _blocks_by_id[_zone_record["id"]]
    _panel_value = next(row for row in _block["panel"] if row["key"] == "suitability")["value"]
    assert _panel_value == _shown

    if _zone_record["survey_type"] == SURVEY_TYPE_EMBANKMENT:
        _diagnostic_phrase = f"compartment mean {_shown}{DISPLAY_SCALE_UNIT}"
        _prose_phrase = (
            f"compartment mean {_shown}{DISPLAY_SCALE_UNIT}, max "
            f"{to_display_scale(_zone_record['max_suitability'])}{DISPLAY_SCALE_UNIT}"
        )
    else:
        _diagnostic_phrase = f"mean {_shown}{DISPLAY_SCALE_UNIT}"
        _prose_phrase = (
            f"member-cell mean suitability {_shown}{DISPLAY_SCALE_UNIT} (max "
            f"{to_display_scale(_zone_record['max_suitability'])}{DISPLAY_SCALE_UNIT})"
        )
    assert _diagnostic_phrase in _summary, (
        f"the diagnostic table must show the panel's own figure: {_diagnostic_phrase!r} not in\n"
        f"{_summary}"
    )
    assert _prose_phrase in _prose, (
        f"the report prose must show the panel's own figure: {_prose_phrase!r}"
    )

# The scale a reader reads the panel value against is converted through
# the same helper, so value and denominator are never on two scales.
_scales = build_scales(_result)
for _type in SURVEY_TYPES:
    assert _scales["suitability"]["parcel_observed_max"][_type] == to_display_scale(
        float(np.max(_result["surfaces"][_type]))
    )
assert (_scales["suitability"]["min"], _scales["suitability"]["max"]) == (
    DISPLAY_SCALE_MIN,
    DISPLAY_SCALE_MAX,
)

print(
    f"5. AGREEMENT: on one fixture, the panel row, the diagnostic table and the report prose print "
    f"the same converted figure for every one of {len(_result['zones'])} zone(s), and the scales "
    f"block's endpoints and per-type ceilings "
    f"({_scales['suitability']['parcel_observed_max']}) are the same conversion."
)


# ======================================================================
# 6. FULL-CONTEXT SYNTHETIC: THE CONSUMER CONTRACT
# ======================================================================
# A presentation change must be invisible to everything downstream of the
# measurement. selected_water_zone is the sharpest statement available
# here: it is chosen by pooling an embankment's compartment_rank_score
# against an excavated zone's mean_suitability, so if any of those had
# followed the display, the pooled comparison would pick differently.

_selected = _result["selected_water_zone"]
assert _selected is not None, "the fixture selects a zone, or this section proves nothing"
assert 0.0 <= _selected["mean_suitability"] <= 1.0
_reselected = wsa.select_survey_zone(_result["zones"])
assert _reselected["id"] == _selected["id"], (
    "selection is a function of the stored scores and nothing else"
)
assert _narrative["selection"]["selected_zone_id"] == _selected["id"]
assert _narrative["selection"]["selected_survey_type"] == _selected["survey_type"]

# The consumer fields downstream steps read off the selected zone, still
# there and still in their own units.
for _field in (
    "id",
    "rank",
    "survey_type",
    "zone_acres",
    "mean_suitability",
    "representative_elevation_m",
    "render_fill_polygon_utm",
):
    assert _field in _selected, f"the consumer contract lost {_field}"

# Ranking is untouched: per type, contiguous from 1, ordered by the
# instrument each type ranks on.
for _type in SURVEY_TYPES:
    _of_type = sorted(_result["zones_by_type"][_type], key=lambda z: z["rank"])
    assert [z["rank"] for z in _of_type] == list(range(1, len(_of_type) + 1))

print(
    f"6. FULL CONTEXT: selected_water_zone is zone {_selected['id']} "
    f"({_selected['survey_type']}), re-derivable from the stored scores alone; the consumer "
    f"fields are intact in their own units and per-type ranking is contiguous."
)

print("\nAll display-scale checks passed.")
