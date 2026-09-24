"""
design_record.py

THE DESIGN RECORD'S FIGURES: what the user committed at each step, read
off the Design Document and nothing else, formatted the way the step's
panel formatted them when the user committed.

    build_design_record(document) -> {"steps": [one entry per STEP_ORDER step]}
    to_fixed(value, dp)           -> JavaScript's Number.prototype.toFixed

THE DOCUMENT IS THE ONLY INPUT (branch 13, decision A). A committed step
carries the FeatureCollection the client sent -- the server's own
proposal Features for what the user selected, the drawn or placed
Features as the client held them -- and the provenance of each. That is
the whole of what this module reads. It takes no session context, no
generate result and no DEM: the panel's other rows (landform's aspect
word and soils, the road network's score and crossings, a tree zone's
place in the parcel) live only in the session cache, and a record built
from the cache would fail the day the cache is evicted and could, after
a code change, disagree with what the user agreed to. So the record
prints what the document holds and is silent about the rest -- no
footnote about what is missing: a reader who did not see the panel has
nothing to miss. Saving the panel's rows into the document at commit is
the complete answer and is a Design Document change for its own branch.

A FIGURE IS PRINTED ONLY WHEN IT IS PROVABLY THE PANEL'S. Most rows are
exact: the panel formatted a value the committed Feature carries
unchanged (a structure site's score) or carries at the rounding the
panel applied before formatting (a tree zone's acres, 0.01-rounded on
both). Three are not, because the document holds a figure rounded more
coarsely or per part where the panel formatted the whole:

    roads   acres served   Features carry total_served_acres at 0.001;
                           the panel formatted round(served, 1).
            avg grade %    NEVER PRINTED. The panel's is a length-weighted
                           mean of the UNROUNDED branch grades; Features
                           carry each branch's grade at 0.1, so the true
                           mean is anywhere in a band a full last digit
                           wide and the panel's figure is never provable.
    fencing feet           the panel's is _feet(sum of every ring's
                           metres); Features carry each feature's
                           length_ft at 0.1.

For each, the true value lies in an interval the stored rounding bounds,
and the panel's string is a non-decreasing step function of the value --
so when the string is the same at both ends of the interval it is the
panel's string, whatever the true value was (_determined). When it is
not, the row is left out rather than risk printing a figure one digit
off the panel's. The fixture has one: the empty-water session's road
serves 2.65 ac at 0.001 -- the panel's round(served, 1) printed "2.7", and
Python's round(2.65, 1) of the stored figure is 2.6 -- so its acres
served is not printed.

JAVASCRIPT'S ROUNDING, NOT PYTHON'S. The panel formats with toFixed,
which picks the nearer of the two neighbours of the EXACT binary value
and, on a true tie, the one further from zero: (2.5).toFixed(0) is "3"
and (2.25).toFixed(1) is "2.3". Python's round() and format() round a tie
to even -- "2" and "2.2" -- so a record formatted with Python would print
a different figure from the panel on exactly the values that tie.
to_fixed() reproduces toFixed on the exact decimal expansion of the
double. Where the SERVER rounded before shipping (road_corridors._round1,
tree_zone_candidates._round1 -- Python's round), that Python rounding is
reproduced too, first, because it is what the panel was handed.

PROVENANCE, IN TWO WORDS. The document's provenance is "generated" or
"user_added" and nothing else (design_document.PROVENANCE_VALUES). A
generated feature is "Suggested", with its rank when it carries one; a
user-added one is "Drawn" (a landform block, a tree zone) or "Placed" (a
structure site). A user-added feature is never given a rank here: a
drawn zone was never ranked, and the rank the tool computes for a site
the user placed (the panel's "would rank N") is the tool's judgement of
the user's decision, not a decision the user made.

COMMITTED EMPTY IS A DECISION AND IS SAID IN WORDS. Every step is
committed when the record is built -- the report is offered only then --
and a step committed with no features carries `empty: True` and a
sentence, never an empty table.
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

import design_document
import display_scale
import production_area_ceiling
import solar_suitability
import tree_zone_candidates
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


def _column(key: str, label: str, numeric: bool) -> dict:
    return {"key": key, "label": label, "numeric": numeric}


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


def _landform(features: list, provenance: dict) -> dict:
    generated, drawn = _split(features, provenance)
    rows = []
    for feature in _by_rank(generated):
        p = feature["properties"]
        rows.append({
            "id": feature["id"], "name": f"Block {p['rank']}",
            "source": provenance_label(PROVENANCE_GENERATED, p["rank"]),
            "acres": to_fixed(p["area_acres"], MEASURE_DP),
            "score": to_fixed(p["suitability_score"], MEASURE_DP),
        })
    for index, feature in enumerate(drawn):
        p = feature["properties"]
        rows.append({
            "id": feature["id"], "name": f"Drawn {index + 1}",
            "source": provenance_label(PROVENANCE_USER_ADDED),
            # The client's own measurement of the ring it clamped, which is
            # the figure on the drawn tab; the server never re-measures it.
            "acres": to_fixed(p.get("acres"), MEASURE_DP),
            "score": to_fixed(p.get("score"), MEASURE_DP),
        })
    count = len(rows)
    return {
        "columns": [
            _column("name", "Block", False), _column("source", "Source", False),
            _column("acres", "acres", True), _column("score", f"/{LANDFORM_SCORE_TOP} score", True),
        ],
        "rows": rows,
        "count": f"{count} production block{'s' if count != 1 else ''} committed.",
    }


def _survey_zone_name(properties: dict) -> str:
    """The panel's surveyZoneName(): the survey type capitalised, then the rank."""
    kind = properties.get("survey_type")
    title = kind[:1].upper() + kind[1:] if kind else "Zone"
    rank = properties.get("rank")
    return f"{title} {rank if rank is not None else '?'}"


def _water(features: list, provenance: dict) -> dict:
    generated, added = _split(features, provenance)
    if added:
        raise ValueError("the water step draws nothing; a user_added water feature is a malformed document")
    rows = []
    for feature in _by_rank(generated):
        p = feature["properties"]
        rows.append({
            "id": feature["id"], "name": _survey_zone_name(p),
            "source": provenance_label(PROVENANCE_GENERATED, p.get("rank")),
            "acres": to_fixed(p["zone_acres"], MEASURE_DP),
            # THE ONE CONVERSION POINT (display_scale.py), on the same
            # 0.0001-rounded mean the panel row was converted from.
            "score": to_fixed(display_scale.to_display_scale(p["mean_suitability"]), WHOLE_DP),
        })
    count = len(rows)
    return {
        "columns": [
            _column("name", "Survey area", False), _column("source", "Source", False),
            _column("acres", "survey acres", True), _column("score", f"/{WATER_SCORE_TOP} score", True),
        ],
        "rows": rows,
        "count": f"{count} water survey area{'s' if count != 1 else ''} committed.",
    }


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
    rows = [
        # EXACT: total_length_ft is road_corridors._feet(total metres), the
        # panel's access.total_length_ft by the same function.
        {"label": "length ft", "value": to_fixed(first["total_length_ft"], WHOLE_DP)},
        # EXACT: rounding is monotone, so the max of the branches' 0.1-rounded
        # maxima is the 0.1-rounded max the panel formatted.
        {"label": "max grade %", "value": to_fixed(max(float(b["properties"]["max_grade_pct"]) for b in branches), MEASURE_DP)},
        {"label": "acres served", "value": _served_acres(branches)},
    ]
    return {
        "access_point": {"text": format_lon_lat(lon, lat), "source": "Placed", "lon_lat": [lon, lat]},
        "network_source": provenance_label(PROVENANCE_GENERATED),
        "rows": [row for row in rows if row["value"] is not None],
        "branch_count": len(branches),
    }


def _trees(features: list, provenance: dict) -> dict:
    generated, drawn = _split(features, provenance)
    rows = []
    for feature in _by_rank(generated):
        p = feature["properties"]
        rows.append({
            "id": feature["id"], "name": f"Zone {p['rank']}",
            "source": provenance_label(PROVENANCE_GENERATED, p["rank"]),
            # The panel's zone.area_acres and zone.score are the server's
            # _round1() of these same two fields.
            "acres": to_fixed(_py_round(p["area_acres"], 1), MEASURE_DP),
            "score": to_fixed(_py_round(p["tree_suitability_score"], 1), MEASURE_DP),
        })
    for index, feature in enumerate(drawn):
        p = feature["properties"]
        rows.append({
            "id": feature["id"], "name": f"Drawn {index + 1}",
            "source": provenance_label(PROVENANCE_USER_ADDED),
            "acres": to_fixed(p.get("acres"), MEASURE_DP),
            "score": to_fixed(p.get("score"), MEASURE_DP),
        })
    count = len(rows)
    return {
        "columns": [
            _column("name", "Zone", False), _column("source", "Source", False),
            _column("acres", "acres", True), _column("score", f"/{TREES_SCORE_TOP} score", True),
        ],
        "rows": rows,
        "count": f"{count} tree zone{'s' if count != 1 else ''} committed.",
    }


def _structures(features: list, provenance: dict) -> dict:
    generated, placed = _split(features, provenance)
    sources = {f["properties"].get("road_proximity_source") for f in features}
    distance_label = ROAD_DISTANCE_LABELS.get(sources.pop(), "ft to road") if len(sources) == 1 else "ft to road"
    rows = []
    for feature in _by_rank(generated):
        p = feature["properties"]
        rows.append({
            "id": feature["id"], "name": f"Site {p['rank']}",
            "source": provenance_label(PROVENANCE_GENERATED, p["rank"]),
            "distance": to_fixed(p.get("distance_to_road_ft"), WHOLE_DP),
            "score": to_fixed(p.get("suitability_score"), MEASURE_DP),
        })
    for index, feature in enumerate(placed):
        p = feature["properties"]
        # "Placed N" and NOT the panel's "Placed N · would rank R": the rank
        # the tool gave a site the user chose is not the user's decision.
        rows.append({
            "id": feature["id"], "name": f"Placed {index + 1}",
            "source": provenance_label(PROVENANCE_USER_ADDED, user_word="Placed"),
            "distance": to_fixed(p.get("distance_to_road_ft"), WHOLE_DP),
            "score": to_fixed(p.get("suitability_score"), MEASURE_DP),
        })
    count = len(rows)
    return {
        "columns": [
            _column("name", "Site", False), _column("source", "Source", False),
            _column("distance", distance_label, True), _column("score", f"/{STRUCTURES_SCORE_TOP} score", True),
        ],
        "rows": rows,
        "count": f"{count} structure site{'s' if count != 1 else ''} committed.",
    }


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
    rows = []
    for fence_type in FENCE_TYPE_LABELS:  # fencing.py's own order of types
        members = by_type.pop(fence_type, None)
        if not members:
            continue
        rows.append({
            "id": fence_type, "name": FENCE_TYPE_LABELS[fence_type],
            "source": provenance_label(PROVENANCE_GENERATED),
            "feet": _fence_feet(members) or EM_DASH,
        })
    if by_type:
        raise ValueError(f"unknown fence types in the document: {sorted(by_type)}")
    return {
        "columns": [
            _column("name", "Fencing", False), _column("source", "Source", False), _column("feet", "feet", True),
        ],
        "rows": rows,
        "count": f"{len(rows)} fence type{'s' if len(rows) != 1 else ''} committed.",
    }


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
