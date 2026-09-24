"""
design_record.py

THE DESIGN RECORD: what the user committed at each step, read off the
Design Document and nothing else, as the step's DATA PANEL showed it.

    build_design_record(document) -> {"steps": [one entry per STEP_ORDER step]}
    to_fixed(value, dp)           -> JavaScript's Number.prototype.toFixed

THE PANEL, NOT THE TAB. A step's tab strip shows two headline figures per
feature; the data panel beside it repeats those same rows and then the
step's detail rows beneath a break (the frontend's panelBody(tab, detail):
tab rows, a break, detail rows, a break, caution rows). The record is
built against the PANEL: each committed feature is one card -- the
panel's header name, its provenance, then the panel's rows in the
panel's order, with the panel's labels -- so the tab's figures appear
because the panel carries them, not as a separate source.

THE DOCUMENT IS THE ONLY INPUT (branch 13, decision A). A committed step
carries the FeatureCollection the client sent -- the server's own
proposal Features for what the user selected, the drawn or placed
Features as the client held them -- and the provenance of each. That is
the whole of what this module reads. It takes no session context and no
generate result. A panel row whose value the document does not hold is
left out of the card, and nothing on the page says so: a reader who did
not see the panel has nothing to miss. Which rows those are, step by
step, is PANEL_ROWS_NOT_IN_DOCUMENT below. Saving the panel's rows into
the document at commit is the complete answer and is a Design Document
change for its own branch.

EXACT DERIVATIONS ONLY. Four panel values are not fields on the feature
but are derived from fields that are, by the same function over the same
stored values the server used when it built the panel row:

    water   water delivery     the panel row water_survey_areas builds
                               from primary_production_area_relationship
                               (None -> no service relationship; above
                               the block -> gravity feed; else pump
                               required), its constants imported.
            /100 score         display_scale.to_display_scale over the
                               feature's own 0.0001-rounded mean.
    trees   position           production_area_ceiling._elevation_position
                               over the zone's own elevation percentile.
            marginal benefits  tree_zone_candidates.marginal_benefits over
                               the zone's own 0.001-rounded factors and
                               data-available flags -- the patch the panel
                               row was built from carries exactly these.

A FIGURE IS PRINTED ONLY WHEN IT IS PROVABLY THE PANEL'S. Most rows are
exact: the panel formatted a value the committed Feature carries
unchanged, or carries at the rounding the panel applied before
formatting. Where the document holds a figure more coarsely than the
panel formatted it, the true value lies in an interval the stored
rounding bounds, and the panel's string is a non-decreasing step
function of the value -- so when the string is the same at both ends of
the interval it is the panel's, whatever the true value was
(_determined). When it is not, the row is left out rather than risk a
figure one digit off the panel's:

    roads    acres served  Features carry total_served_acres at 0.001;
                           the panel formatted round(served, 1). The
                           fixture's empty-water road serves 2.650 at
                           0.001: the panel printed "2.7", Python's
                           round(2.65, 1) of the stored figure is 2.6,
                           and the row is left out.
             avg grade %   NEVER PRINTED: a length-weighted mean of the
                           UNROUNDED branch grades, the branches held at
                           0.1, so the band is a full last digit wide.
    fencing  feet          the panel's is _feet(sum of every ring's
                           metres); Features carry each feature's
                           length_ft at 0.1.

JAVASCRIPT'S ROUNDING, NOT PYTHON'S. The panel formats with toFixed,
which picks the nearer of the two neighbours of the EXACT binary value
and, on a true tie, the one further from zero: (2.5).toFixed(0) is "3"
and (2.25).toFixed(1) is "2.3". Python's round() and format() round a tie
to even -- "2" and "2.2" -- so a record formatted with Python would print
a different figure from the panel on exactly the values that tie.
to_fixed() reproduces toFixed on the exact decimal expansion of the
double. Where the SERVER rounded before shipping (Python's round), that
rounding is reproduced too, first, because it is what the panel was
handed.

PROVENANCE, IN TWO WORDS. The document's provenance is "generated" or
"user_added" and nothing else (design_document.PROVENANCE_VALUES). A
generated feature is "Suggested", with its rank when it carries one; a
user-added one is "Drawn" (a landform block, a tree zone) or "Placed" (a
structure site). A user-added feature is never given a rank here: a
drawn zone was never ranked, and the rank the tool computes for a site
the user placed (the panel's header "Placed N · would rank R") is the
tool's judgement of the user's decision, not a decision the user made.

COMMITTED EMPTY IS A DECISION AND IS SAID IN WORDS. Every step is
committed when the record is built -- the report is offered only then --
and a step committed with no features carries `empty: True` and a
sentence, never an empty card.

FENCING HAS NO DATA PANEL (its step definition declares `detail: null`):
the tab row, a type's feet, is everything the user saw, and it is what
the record prints.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

import design_document
import display_scale
import production_area_ceiling
import solar_suitability
import tree_zone_candidates
import water_survey_areas
from fencing import FENCE_TYPE_LABELS

EM_DASH = "—"

PROVENANCE_GENERATED = "generated"
PROVENANCE_USER_ADDED = "user_added"

STEP_TITLES = {
    "landform": "Landform",
    "water": "Water",
    "roads": "Roads",
    "trees": "Trees",
    "structures": "Structures",
    "fencing": "Fencing",
}

# A step committed with nothing in it, in words. The decision is the
# user's and the sentence says so; it names what the step would have
# held in the panel's own terms.
EMPTY_STATEMENTS = {
    "landform": "No production blocks committed. The design sets no ground aside for production.",
    "water": "No water survey areas committed. The design carries no water zone.",
    "roads": "No road committed. The design runs no road on this property.",
    "trees": "No tree zones committed. The design plants no tree crop.",
    "structures": "No structure sites committed. The design sites no structure.",
    "fencing": "No fencing committed. The design fences nothing.",
}

# The panel's decimal places, per figure (keyline-designer-frontend
# src/wizard/stepDefinitions.js: MEASURE_DP, SUITABILITY_DP, LENGTH_DP,
# DISTANCE_DP, FENCE_LENGTH_DP).
MEASURE_DP = 1
WHOLE_DP = 0

# The structures tab's distance label, by the run's road proximity source
# -- the frontend's ROAD_TAB_LABEL, which the Feature's own
# road_proximity_source selects between.
ROAD_DISTANCE_LABELS = {
    "selected_road_corridor": "ft to road",
    "real_mapped_road": "ft to farm road",
    "unavailable": "ft to road",
}

METERS_PER_FOOT = 0.3048


# ======================================================================
# Formatting
# ======================================================================


def to_fixed(value, dp: int) -> str:
    """JavaScript's Number(value).toFixed(dp), or an em dash for None --
    the panel's measure(). The exact binary value is expanded as a
    Decimal and rounded half away from zero, which is toFixed's rule for
    the two representable neighbours of an exact double."""
    if value is None:
        return EM_DASH
    number = float(value)
    if number == 0:
        number = 0.0  # (-0).toFixed() prints no sign
    quantum = Decimal(1).scaleb(-dp)
    return str(Decimal(number).quantize(quantum, rounding=ROUND_HALF_UP))


def _py_round(value, dp: int):
    """The server's own rounding before shipping (round(float(v), dp)),
    None passed through."""
    return None if value is None else round(float(value), dp)


def _determined(format_value, low: float, high: float) -> Optional[str]:
    """The string format_value gives every value in [low, high], or None
    when it is not one string. format_value must be non-decreasing in its
    argument (a rounding followed by toFixed is), so agreement at the two
    ends is agreement across the interval."""
    at_low, at_high = format_value(low), format_value(high)
    return at_low if at_low == at_high else None


def _score_denominator(scales: dict) -> int:
    """The top of a step's published score scale, as the panel's
    scoreDenominator() reads it: Math.round(scales.range[1])."""
    return int(Decimal(float(scales["range"][1])).quantize(Decimal(1), rounding=ROUND_HALF_UP))


LANDFORM_SCORE_TOP = _score_denominator(production_area_ceiling._SCALES)
WATER_SCORE_TOP = display_scale.DISPLAY_SCALE_MAX
TREES_SCORE_TOP = _score_denominator(tree_zone_candidates._SCALES)
STRUCTURES_SCORE_TOP = _score_denominator(solar_suitability._SCALES)


def count_line(count: int, noun: str) -> dict:
    """How many features a step committed, AS A COUNT: {'n': 6, 'text':
    '6 production blocks committed.'}. Never a total of a column -- the
    record prints no acreage sum anywhere (blocks may overlap, and a union
    would be a recomputation), and nothing here should invite a reader to
    add one up."""
    return {"n": count, "noun": noun if count == 1 else noun + "s",
            "text": f"{count} {noun if count == 1 else noun + 's'} committed."}


def provenance_label(provenance: str, rank=None, user_word: str = "Drawn") -> str:
    """'Suggested · rank N', 'Suggested', or the user's word."""
    if provenance == PROVENANCE_USER_ADDED:
        return user_word
    if provenance != PROVENANCE_GENERATED:
        raise ValueError(f"unknown provenance {provenance!r}")
    return f"Suggested · rank {rank}" if rank is not None else "Suggested"


def format_lon_lat(lon: float, lat: float) -> str:
    """'40.64330° N, 79.98369° W' -- five decimals, about a metre."""
    return (f"{abs(lat):.5f}° {'N' if lat >= 0 else 'S'}, "
            f"{abs(lon):.5f}° {'E' if lon >= 0 else 'W'}")


# ======================================================================
# The steps
# ======================================================================

# THE PANEL ROWS THE DOCUMENT CANNOT SUPPLY, by step and provenance --
# each lives only in the step's generate result (the session cache) and
# is left out of the card. Asserted against the fixture's captured panel
# by test_design_record.py, so this table cannot drift from the code.
PANEL_ROWS_NOT_IN_DOCUMENT = {
    ("landform", PROVENANCE_GENERATED): ("aspect", "position", "median slope %", "soil", "drainage"),
    ("water", PROVENANCE_GENERATED): ("shared ground w/ <a survey area not committed> %",),
    ("roads", PROVENANCE_GENERATED): ("/100 score", "avg grade %", "crosses production block ft", "crosses canopy ft",
                                      "crosses wet ground ft"),
    ("trees", PROVENANCE_GENERATED): ("where in the parcel",),
}


# ======================================================================
# The panel's rows
# ======================================================================
#
# One row is {"kind", "value", "label"}: "measured" (a figure, set in the
# data face and right-aligned), "categorical" (a word), "continuation" (a
# further line of a labelled run, no label of its own), "term" (a line of
# prose under a heading), "break" (the panel's hairline, optionally with a
# heading) and "caution" (a crossing a drawn shape carries). The builders
# below are the frontend's (src/wizard/stepDefinitions.js, shell/
# panelFormat.js, shell/DetailPanel.jsx), row for row.


def _measured(value, label):
    return {"kind": "measured", "value": value, "label": label}


def _categorical(value, label):
    return {"kind": "categorical", "value": value if value is not None else EM_DASH, "label": label}


def _term(value):
    return {"kind": "term", "value": value, "label": None}


def _break(label=None):
    return {"kind": "break", "value": None, "label": label}


def _drops_at_zero(value, row):
    """panelFormat.dropsAtZero: a null is a row (it prints a dash), a
    measured zero is no row at all."""
    if value is None:
        return row
    return None if float(value) == 0 else row


def _labelled_run(values, label):
    """panelFormat.labelledRun: the first value carries the label, the
    rest continue it; none at all is one dashed row."""
    values = list(values or [])
    if not values:
        return [_categorical(EM_DASH, label)]
    return [_categorical(values[0], label)] + [
        {"kind": "continuation", "value": v if v is not None else EM_DASH, "label": None} for v in values[1:]
    ]


def _plural(count: int) -> str:
    return "" if count == 1 else "s"


def _panel_body(tab_rows: list, detail_rows: list) -> list:
    """panelFormat.panelBody: the tab's rows, a break, the detail rows;
    runs of breaks collapse to one (a labelled one wins) and none leads or
    trails."""
    rows = [r for r in detail_rows if r is not None]
    joined = tab_rows + ([_break()] if tab_rows and rows else []) + rows
    body = []
    for row in joined:
        if row["kind"] != "break":
            body.append(row)
            continue
        if not body:
            continue
        if body[-1]["kind"] != "break":
            body.append(row)
        elif row["label"] and not body[-1]["label"]:
            body[-1] = row
    while body and body[-1]["kind"] == "break":
        body.pop()
    return body


def _caution_rows(cautions) -> list:
    """DetailPanel's caution run: every caution a drawn shape carries whose
    acres are not zero, as a share when it names one, else as acres."""
    rows = []
    for caution in cautions or []:
        if float(caution.get("acres") or 0) == 0:
            continue
        if caution.get("overlapLabel") is not None and caution.get("pct") is not None:
            rows.append({"kind": "caution", "value": to_fixed(caution["pct"], 0), "label": caution["overlapLabel"]})
        else:
            rows.append({"kind": "caution", "value": to_fixed(caution["acres"], 1), "label": f"acres — {caution.get('label')}"})
    return rows


def _card(feature_id, name, source, tab_rows, detail_rows, cautions=None) -> dict:
    return {"id": feature_id, "name": name, "source": source,
            "rows": _panel_body(tab_rows, detail_rows) + _caution_rows(cautions)}


def _aspect_phrase(reading: dict) -> str:
    """stepDefinitions.aspectPhrase."""
    if not reading.get("aspect_available") or not reading.get("dominant_aspect"):
        return EM_DASH
    return f"{reading['dominant_aspect']} facing"


# ======================================================================
# The steps
# ======================================================================


def _split(features: list, provenance: dict) -> tuple:
    """(generated, user_added), each in commit order. A feature whose
    provenance is missing is a malformed document and raises."""
    generated, added = [], []
    for feature in features:
        kind = provenance.get(feature.get("id"))
        if kind == PROVENANCE_GENERATED:
            generated.append(feature)
        elif kind == PROVENANCE_USER_ADDED:
            added.append(feature)
        else:
            raise ValueError(f"feature {feature.get('id')!r} carries no provenance in the document")
    return generated, added


def _by_rank(features: list) -> list:
    return sorted(features, key=lambda f: (f["properties"].get("rank") is None, f["properties"].get("rank") or 0))


def _score_label(top: int) -> str:
    return f"/{top} score"


def _production_block_rows(reading: dict) -> list:
    """stepDefinitions.productionBlockRows: the ground under a block."""
    return [
        _categorical(_aspect_phrase(reading), "aspect"),
        _categorical(reading.get("elevation_position"), "position"),
        _measured(to_fixed(reading.get("slope_median_pct"), MEASURE_DP), "median slope %"),
        *_labelled_run([entry.get("label") for entry in reading.get("soil_components") or []], "soil"),
        _categorical(reading.get("drainage_class"), "drainage"),
    ]


def _landform(features: list, provenance: dict) -> dict:
    generated, drawn = _split(features, provenance)
    cards = []
    for feature in _by_rank(generated):
        p = feature["properties"]
        # A SUGGESTED BLOCK'S CARD IS ITS TAB ROWS ALONE. Its panel read the
        # ground rows (aspect, position, median slope, soil, drainage) off
        # the generate's `zones` table, and the committed Feature carries
        # none of them -- only aspect_deg and avg_slope_pct, which are
        # different quantities. Left out, not recomputed.
        cards.append(_card(feature["id"], f"Block {p['rank']}", provenance_label(PROVENANCE_GENERATED, p["rank"]), [
            _measured(to_fixed(p["area_acres"], MEASURE_DP), "acres"),
            _measured(to_fixed(p["suitability_score"], MEASURE_DP), _score_label(LANDFORM_SCORE_TOP)),
        ], []))
    # "DRAWN N" IN COMMIT ORDER, WHICH IS NOT ALWAYS THE PANEL'S N. The
    # panel numbers drawn tabs across every block the user drew, ticked or
    # not; the document keeps only the committed ones, so a block the user
    # drew second after unticking the first is "Drawn 2" on the panel and
    # "Drawn 1" here. The panel's numbering is not recoverable from the
    # document, the name is a label rather than a measurement, and every
    # figure beside it is the panel's. Not a bug -- see branch 13's review.
    for index, feature in enumerate(drawn):
        p = feature["properties"]
        # A drawn block's ground rows ARE on its Feature: the server's
        # reading, merged into properties before the commit, under the
        # suggestion's own field names -- and so is the client's acreage
        # and its cautions.
        cards.append(_card(feature["id"], f"Drawn {index + 1}", provenance_label(PROVENANCE_USER_ADDED), [
            _measured(to_fixed(p.get("acres"), MEASURE_DP), "acres"),
            _measured(to_fixed(p.get("score"), MEASURE_DP), _score_label(LANDFORM_SCORE_TOP)),
        ], _production_block_rows(p), p.get("cautions")))
    return {"cards": cards, "count": count_line(len(cards), "production block")}


def _survey_zone_name(properties: dict) -> str:
    """The panel's surveyZoneName(): the survey type capitalised, then the rank."""
    kind = properties.get("survey_type")
    title = kind[:1].upper() + kind[1:] if kind else "Zone"
    rank = properties.get("rank")
    return f"{title} {rank if rank is not None else '?'}"


def _water_delivery(properties: dict) -> str:
    """The panel's water-delivery answer, as water_survey_areas builds the
    row from the same field, underscores read as spaces (waterDeliveryPhrase)."""
    primary = properties.get("primary_production_area_relationship")
    if primary is None:
        value = water_survey_areas.WATER_DELIVERY_NONE
    elif primary["above_production_area"]:
        value = water_survey_areas.WATER_DELIVERY_GRAVITY
    else:
        value = water_survey_areas.WATER_DELIVERY_PUMP
    return value.replace("_", " ")


def _overlap_row(value, label):
    return _drops_at_zero(value, _measured(to_fixed(value, MEASURE_DP), label))


def _water(features: list, provenance: dict) -> dict:
    generated, added = _split(features, provenance)
    if added:
        raise ValueError("the water step draws nothing; a user_added water feature is a malformed document")
    by_zone = {f["properties"].get("zone_id"): f for f in generated}
    cards = []
    for feature in _by_rank(generated):
        p = feature["properties"]
        embankment = p.get("survey_type") == "embankment"
        # SHARED GROUND IS NAMED BY THE OTHER AREA -- the panel names it off
        # the generate's full proposal set. When the other area is committed
        # too, its type and rank are in the document and the row is the
        # panel's; when it is not, the document cannot name it and the row
        # is left out.
        shared = []
        for entry in p.get("cross_type_overlaps") or []:
            other = by_zone.get(entry.get("zone_id"))
            if other is not None:
                shared.append(_overlap_row(float(entry["fraction"]) * 100,
                                           f"shared ground w/ {_survey_zone_name(other['properties'])} %"))
        cards.append(_card(feature["id"], _survey_zone_name(p), provenance_label(PROVENANCE_GENERATED, p.get("rank")), [
            _measured(to_fixed(p["zone_acres"], MEASURE_DP), "survey acres"),
            # THE ONE CONVERSION POINT (display_scale.py), on the same
            # 0.0001-rounded mean the panel row was converted from.
            _measured(to_fixed(display_scale.to_display_scale(p["mean_suitability"]), WHOLE_DP), _score_label(WATER_SCORE_TOP)),
        ], [
            _categorical(_water_delivery(p), "water delivery"),
            _measured(to_fixed(p.get("pinch_catchment_acres"), MEASURE_DP), "contributing acres at dam site") if embankment
            else _measured(to_fixed(p.get("contributing_area_acres_at_wettest_cell"), MEASURE_DP), "contributing acres"),
            _measured(to_fixed(p.get("slope_median_pct"), MEASURE_DP), "median slope %"),
            _measured(to_fixed(p.get("pinch_binding_height_ft"), MEASURE_DP), "binding shoulder height ft") if embankment
            else _measured(to_fixed(p.get("depression_depth_max_ft"), MEASURE_DP), "max depth ft"),
            _break(),
            _overlap_row(p.get("production_overlap_pct"), "production overlap %"),
            _overlap_row(p.get("canopy_overlap_pct"), "canopy overlap %"),
            _overlap_row(p.get("road_overlap_pct"), "road overlap %"),
            *shared,
        ]))
    return {"cards": cards, "count": count_line(len(cards), "water survey area")}


def _served_acres(branches: list) -> Optional[str]:
    """The panel's measure(round(served, 1)) from total_served_acres at 0.001."""
    stored = float(branches[0]["properties"]["total_served_acres"])
    return _determined(lambda v: to_fixed(_py_round(v, 1), MEASURE_DP), stored - 0.0005, stored + 0.0005)


def _roads(features: list, provenance: dict) -> dict:
    generated, added = _split(features, provenance)
    if added:
        raise ValueError("the roads step draws nothing; a user_added road feature is a malformed document")
    networks = {f["properties"].get("network_id") for f in generated}
    if len(networks) != 1:
        raise ValueError(f"a committed road is one network; the document holds {len(networks)}")
    branches = sorted(generated, key=lambda f: f["properties"]["branch_index"])
    first = branches[0]["properties"]
    lon, lat = first["access_point"]
    served = _served_acres(branches)
    # "Road network", not the panel's "Road Network N": N is the network's
    # place among every access point the user tried, and the document does
    # not reliably hold that order. A label, not a figure.
    card = _card(first["network_id"], "Road network", provenance_label(PROVENANCE_GENERATED), [
        *([_measured(served, "acres served")] if served is not None else []),
    ], [
        # EXACT: total_length_ft is road_corridors._feet(total metres), the
        # panel's access.total_length_ft by the same function.
        _measured(to_fixed(first["total_length_ft"], WHOLE_DP), "length ft"),
        # EXACT: rounding is monotone, so the max of the branches' 0.1-rounded
        # maxima is the 0.1-rounded max the panel formatted.
        _measured(to_fixed(max(float(b["properties"]["max_grade_pct"]) for b in branches), MEASURE_DP), "max grade %"),
    ])
    return {
        "cards": [card],
        "access_point": {"text": format_lon_lat(lon, lat), "source": "Placed", "lon_lat": [lon, lat]},
        "count": count_line(1, "road network"),
    }


def _benefit_rows(benefits) -> list:
    """stepDefinitions.marginalBenefitRows: a heading and one term per
    benefit, nothing at all for none."""
    earned = list(benefits or [])
    if not earned:
        return []
    return [_break(f"marginal benefit{_plural(len(earned))}")] + [_term(b) for b in earned]


def _tree_zone_rows(reading: dict, position_in_parcel: bool) -> list:
    """stepDefinitions.treeZoneRows, less `where in the parcel` when the
    document does not hold it."""
    rows = []
    if position_in_parcel:
        rows.append(_categorical(reading.get("position_in_parcel"), "where in the parcel"))
    rows += [
        _categorical(reading.get("elevation_position"), "position"),
        _measured(to_fixed(_py_round(reading.get("slope_median_pct"), 1), MEASURE_DP), "median slope %"),
        *_benefit_rows(reading.get("marginal_benefits")),
    ]
    return rows


def _trees(features: list, provenance: dict) -> dict:
    generated, drawn = _split(features, provenance)
    cards = []
    for feature in _by_rank(generated):
        p = feature["properties"]
        # A SUGGESTED ZONE'S PANEL READINGS, FROM ITS OWN FIELDS: the word
        # for its elevation and its earned benefits by the server's own
        # functions over the stored percentile, factors and flags -- the
        # values the panel's row was built from. Its place in the parcel is
        # not stored and is left out.
        reading = {
            "elevation_position": production_area_ceiling._elevation_position(p.get("elevation_percentile_of_parcel")),
            "slope_median_pct": p.get("slope_median_pct"),
            "marginal_benefits": tree_zone_candidates.marginal_benefits(p),
        }
        cards.append(_card(feature["id"], f"Zone {p['rank']}", provenance_label(PROVENANCE_GENERATED, p["rank"]), [
            # The panel's zone.area_acres and zone.score are the server's
            # _round1() of these same two fields.
            _measured(to_fixed(_py_round(p["area_acres"], 1), MEASURE_DP), "acres"),
            _measured(to_fixed(_py_round(p["tree_suitability_score"], 1), MEASURE_DP), _score_label(TREES_SCORE_TOP)),
        ], _tree_zone_rows(reading, position_in_parcel=False)))
    for index, feature in enumerate(drawn):  # commit order: see _landform's note on "Drawn N"
        p = feature["properties"]
        cards.append(_card(feature["id"], f"Drawn {index + 1}", provenance_label(PROVENANCE_USER_ADDED), [
            _measured(to_fixed(p.get("acres"), MEASURE_DP), "acres"),
            _measured(to_fixed(p.get("score"), MEASURE_DP), _score_label(TREES_SCORE_TOP)),
        ], _tree_zone_rows(p, position_in_parcel=True), p.get("cautions")))
    return {"cards": cards, "count": count_line(len(cards), "tree zone")}


# stepDefinitions.GATE_STATEMENTS: a placed site's broken siting rules in the
# user's terms, the threshold read out of the rule's wire name.
_GATE_STATEMENTS = (
    (r"^outside_existing_canopy$", lambda: "sits under existing tree canopy"),
    (r"^outside_water_candidate_zone$", lambda: "sits on the committed water ground"),
    (r"^outside_tree_zone_candidate_buffer$", lambda: "sits inside a committed tree zone’s clearance"),
    (r"^within_road_proximity_buffer$", lambda: "is farther from a road than the siting rule allows"),
    (r"^outside_hydric_soil$", lambda: "sits on wet (hydric) soil, which drains badly"),
    (r"^outside_floodplain$", lambda: "sits in the mapped floodplain"),
    (r"^max_slope<=(\d+(?:\.\d+)?)pct$", lambda pct: f"averages more than {pct}% slope"),
    (r"^suitability_score>=(\d+(?:\.\d+)?)$", lambda floor: f"scores below the floor of {floor}"),
)


def gate_statement(name: str) -> str:
    import re

    for pattern, words in _GATE_STATEMENTS:
        match = re.match(pattern, str(name))
        if match:
            return words(*match.groups())
    return f"fails the rule the server calls {name}"


def _structure_rows(p: dict) -> list:
    broken = list(p.get("constraints_violated") or [])
    return [
        _categorical(_aspect_phrase(p), "aspect"),
        _categorical(p.get("elevation_position"), "position"),
        _measured(to_fixed(p.get("avg_slope_pct"), MEASURE_DP), "avg slope %"),
        _categorical(p.get("solar_rating"), "solar rating"),
        *([_break(f"siting rule{_plural(len(broken))} broken")] + [_term(gate_statement(n)) for n in broken] if broken else []),
    ]


def _structures(features: list, provenance: dict) -> dict:
    generated, placed = _split(features, provenance)
    cards = []
    for feature in _by_rank(generated) + placed:
        p = feature["properties"]
        is_placed = provenance[feature["id"]] == PROVENANCE_USER_ADDED
        label = ROAD_DISTANCE_LABELS.get(p.get("road_proximity_source"), "ft to road")
        # "Placed N" and NOT the panel's "Placed N · would rank R": the rank
        # the tool gave a site the user chose is not the user's decision.
        # Commit order, as "Drawn N" on landform.
        name = f"Placed {placed.index(feature) + 1}" if is_placed else f"Site {p['rank']}"
        source = provenance_label(PROVENANCE_USER_ADDED, user_word="Placed") if is_placed \
            else provenance_label(PROVENANCE_GENERATED, p["rank"])
        cards.append(_card(feature["id"], name, source, [
            _measured(to_fixed(p.get("distance_to_road_ft"), WHOLE_DP), label),
            _measured(to_fixed(p.get("suitability_score"), MEASURE_DP), _score_label(STRUCTURES_SCORE_TOP)),
        ], _structure_rows(p)))
    return {"cards": cards, "count": count_line(len(cards), "structure site")}


def _fence_feet(features: list) -> Optional[str]:
    """The panel's measure(_feet(sum of ring metres), 0), bounded from
    each feature's length_ft at 0.1."""
    stored = sum(float(f["properties"]["length_ft"]) for f in features)
    slack = 0.05 * len(features)
    return _determined(lambda v: to_fixed(_py_round(v, 1), WHOLE_DP), stored - slack, stored + slack)


def _fencing(features: list, provenance: dict) -> dict:
    generated, added = _split(features, provenance)
    if added:
        raise ValueError("the fencing step draws nothing; a user_added fence is a malformed document")
    by_type = {}
    for feature in generated:
        by_type.setdefault(feature["properties"]["fence_type"], []).append(feature)
    cards = []
    for fence_type in FENCE_TYPE_LABELS:  # fencing.py's own order of types
        members = by_type.pop(fence_type, None)
        if not members:
            continue
        feet = _fence_feet(members)
        cards.append(_card(fence_type, FENCE_TYPE_LABELS[fence_type], provenance_label(PROVENANCE_GENERATED),
                           [_measured(feet, "feet")] if feet is not None else [], []))
    if by_type:
        raise ValueError(f"unknown fence types in the document: {sorted(by_type)}")
    return {"cards": cards, "count": count_line(len(cards), "fence type")}


_BUILDERS = {
    "landform": _landform,
    "water": _water,
    "roads": _roads,
    "trees": _trees,
    "structures": _structures,
    "fencing": _fencing,
}


def build_design_record(document: dict) -> dict:
    """The record, in STEP_ORDER. Raises for a step that is not committed:
    the report is offered only once every step is, so an uncommitted step
    here is a caller's mistake, not a record to render with a hole in it."""
    design_document.validate_document(document)
    steps = []
    for step_id in design_document.STEP_ORDER:
        entry = document["steps"][step_id]
        if entry["status"] != design_document.STATUS_COMMITTED:
            raise ValueError(f"step {step_id!r} is {entry['status']}; the design record needs every step committed")
        features = list(entry["features"].get("features") or [])
        record = {"step_id": step_id, "title": STEP_TITLES[step_id], "empty": not features}
        if features:
            record.update(_BUILDERS[step_id](features, entry["provenance"]))
        else:
            record["statement"] = EMPTY_STATEMENTS[step_id]
        steps.append(record)
    return {"steps": steps}
