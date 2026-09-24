"""
test_design_record.py

THE DESIGN RECORD AGAINST THE PANEL, OFFLINE.

design_record_fixture.json holds two fully committed Design Documents made
through the real orchestrator (make_design_record_fixtures.py) and, beside
each, every committed feature's DATA PANEL -- header, tab rows, detail
rows -- read off the generate payloads where the frontend reads them and
formatted by Node's own toFixed. This file builds the record from each
DOCUMENT ALONE and holds every row it prints to the panel's row, in the
panel's order, with the panel's label and string.

    1. toFixed, reproduced -- on the values where JavaScript and Python differ
    2. every row the record prints is the panel's, in order; the rows left out, and why
    3. names and provenance: suggested with a rank, drawn, placed with none; cautions
    4. committed empty, in words, end to end
    5. nothing recomputed: the record reads the document and calls nothing
"""

import copy
import json
import os
import shutil
import subprocess
from unittest.mock import patch

import offline_harness

offline_harness.install()

import design_document  # noqa: E402
import design_record as dr  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "design_record_fixture.json"), encoding="utf-8") as handle:
    FIXTURE = json.load(handle)

print("=" * 72)
print("test_design_record.py -- the design record, from the document, against the panel")
print("=" * 72)

# ======================================================================
# 1. toFixed
# ======================================================================
print("1. toFixed, reproduced -- on the values where JavaScript and Python differ")

# (value, dp, what JavaScript prints). Every one of the TIES below prints
# differently under Python's round-half-to-even, which is asserted, so the
# table cannot pass by testing values on which the two rules agree.
JS_TIES = [
    (2.5, 0, "3"), (0.5, 0, "1"), (4.5, 0, "5"), (2.25, 1, "2.3"), (0.125, 2, "0.13"),
    (-2.5, 0, "-3"), (44.25, 1, "44.3"), (2662.5, 0, "2663"),
]
# Not ties in binary, so both rules agree -- and toFixed must not "fix" them:
# 1.005 is 1.00499999999999989..., 2.65 is 2.64999999999999991...
JS_NON_TIES = [(1.005, 2, "1.00"), (2.65, 1, "2.6"), (0.35, 1, "0.3"), (-0.04, 1, "-0.0"), (-0.0, 1, "0.0"), (None, 1, "—")]
for value, dp, expected in JS_TIES:
    assert dr.to_fixed(value, dp) == expected, (value, dp, dr.to_fixed(value, dp))
    assert f"{value:.{dp}f}" != expected, f"{value} is not a discriminating tie"
for value, dp, expected in JS_NON_TIES:
    assert dr.to_fixed(value, dp) == expected, (value, dp, dr.to_fixed(value, dp))

# A synthetic structure site scoring exactly 2.25: the panel prints "2.3"
# and so must the record; Python's own formatting would print "2.2".
tie_site = {"id": "s", "type": "Feature", "geometry": None,
            "properties": {"rank": 1, "distance_to_road_ft": 2.5, "suitability_score": 2.25,
                           "road_proximity_source": "selected_road_corridor"}}
tie_rows = dr._structures([tie_site], {"s": "generated"})["cards"][0]["rows"]
assert [r["value"] for r in tie_rows[:2]] == ["3", "2.3"], tie_rows

# Where Node is present, a sweep: every 0.005 step from -5 to 105 at 0, 1
# and 2 places -- 66,000 values, the ties among them included. Node builds
# the fixture and is not needed to run this file; without it the sweep is
# skipped and the table above still holds.
swept = 0
if shutil.which("node"):
    values = [round(-5 + i * 0.005, 3) for i in range(22001)]
    script = ("const v = JSON.parse(require('fs').readFileSync(0,'utf8'));"
              "process.stdout.write(JSON.stringify([0,1,2].map(d => v.map(x => x.toFixed(d)))))")
    js = json.loads(subprocess.run(["node", "-e", script], input=json.dumps(values), capture_output=True, text=True, check=True).stdout)
    for dp, strings in enumerate(js):
        for value, text in zip(values, strings):
            assert dr.to_fixed(value, dp) == text, (value, dp, dr.to_fixed(value, dp), text)
            swept += 1
print(f"   {len(JS_TIES)} ties where Python differs, {len(JS_NON_TIES)} non-ties; "
      + (f"{swept:,} values swept against Node" if swept else "Node absent, sweep skipped"))

# ======================================================================
# 2. The panel's rows
# ======================================================================
print("2. every row the record prints is the panel's, in order; the rows left out, and why")


def _seq(rows):
    """A card's rows as (kind, label, string), the hairlines dropped and a
    labelled break kept as its heading."""
    out = []
    for row in rows:
        if row["kind"] == "break":
            if row["label"]:
                out.append(("heading", row["label"], None))
            continue
        out.append((row["kind"], row["label"], row.get("text", row.get("value"))))
    return out


# THE PANEL ROWS THE DOCUMENT DOES NOT HOLD. Static by step and provenance
# (design_record.PANEL_ROWS_NOT_IN_DOCUMENT), plus two that depend on the
# design: a shared-ground row whose other survey area was not committed
# (the document cannot name it), and a road's acres served when its
# 0.001-rounded figure straddles the panel's rounding (_served_acres).
def _left_out(step_id, provenance, row, committed_names, served_determined):
    label = row[1]
    static = dr.PANEL_ROWS_NOT_IN_DOCUMENT.get((step_id, provenance), ())
    if label in static:
        return label
    if step_id == "water" and label and label.startswith("shared ground w/ "):
        name = label[len("shared ground w/ "):-2]
        if name not in committed_names:
            return "shared ground w/ <a survey area not committed> %"
    if step_id == "roads" and label == "acres served" and not served_determined:
        return "acres served (undetermined at 0.001)"
    return None


EXPECTED_LEFT_OUT = {
    "full": {
        ("landform", "aspect"): 5, ("landform", "position"): 5, ("landform", "median slope %"): 5, ("landform", "soil"): 5,
        ("landform", "drainage"): 5, ("water", "shared ground w/ <a survey area not committed> %"): 6,
        ("roads", "/100 score"): 1, ("roads", "avg grade %"): 1, ("trees", "where in the parcel"): 1,
    },
}
EXPECTED_LEFT_OUT["empty_water"] = {k: v for k, v in EXPECTED_LEFT_OUT["full"].items() if k[0] != "water"}
EXPECTED_LEFT_OUT["empty_water"][("roads", "acres served (undetermined at 0.001)")] = 1

TABLE = []
for session, case in FIXTURE.items():
    document = case["document"]
    record = {s["step_id"]: s for s in dr.build_design_record(document)["steps"]}
    left_out = {}
    for step_id in design_document.STEP_ORDER:
        panel_cards = {card["id"]: card for card in case["panel"][step_id]}
        step = record[step_id]
        if step["empty"]:
            assert not panel_cards, f"{session}: {step_id} committed empty but the panel had cards"
            continue
        assert [c["id"] for c in step["cards"]] == [c["id"] for c in case["panel"][step_id]], (session, step_id)
        committed_names = {c["name"] for c in step["cards"]}
        provenance = document["steps"][step_id]["provenance"]
        for card in step["cards"]:
            panel = panel_cards[card["id"]]
            kind = "generated" if step_id in ("roads", "fencing") else provenance[card["id"]]
            served_ok = step_id != "roads" or dr._served_acres(document["steps"]["roads"]["features"]["features"]) is not None
            expected, skipping = [], False
            for row in _seq(panel["rows"]):
                if row[0] == "continuation" and skipping:
                    continue   # a labelled run's later lines go with its first
                reason = _left_out(step_id, kind, row, committed_names, served_ok)
                skipping = reason is not None
                if reason:
                    left_out[(step_id, reason)] = left_out.get((step_id, reason), 0) + 1
                    continue
                expected.append(row)
            got = _seq(card["rows"])
            assert got == expected, (session, step_id, card["name"], got, expected)
            for kind_, label, text in got:
                TABLE.append((session, step_id, card["name"], label if kind_ != "term" else "", text))
    assert left_out == EXPECTED_LEFT_OUT[session], (session, left_out)

width = max(len(r[2]) for r in TABLE)
for session, step_id, name, label, text in TABLE:
    if session == "full":
        print(f"   {step_id:<10} {name:<{width}} {'' if text is None else text:>52}  {label}")
print(f"   {len(TABLE)} rows across both sessions, each the panel's row, in the panel's order")
for session, expected in EXPECTED_LEFT_OUT.items():
    print(f"   left out ({session}): " + "; ".join(f"{step} {label} x{n}" for (step, label), n in sorted(expected.items())))

# ======================================================================
# 3. Names, provenance and cautions
# ======================================================================
print("3. names and provenance: suggested with a rank, drawn, placed with none; cautions")
record = {s["step_id"]: s for s in dr.build_design_record(FIXTURE["full"]["document"])["steps"]}
document = FIXTURE["full"]["document"]
for step_id in ("landform", "water", "trees", "structures"):
    panel_names = {card["id"]: card["name"] for card in FIXTURE["full"]["panel"][step_id]}
    for card in record[step_id]["cards"]:
        kind = document["steps"][step_id]["provenance"][card["id"]]
        if kind == "generated":
            rank = next(f for f in document["steps"][step_id]["features"]["features"] if f["id"] == card["id"])["properties"]["rank"]
            assert card["source"] == f"Suggested · rank {rank}", card
            assert card["name"] == panel_names[card["id"]], (card["name"], panel_names[card["id"]])
        else:
            # A USER-ADDED FEATURE CARRIES NO RANK ANYWHERE IN THE RECORD. A
            # drawn zone never was ranked; a placed site was given one by the
            # tool (the panel's header "Placed 1 · would rank N"), and the
            # record leaves it out: the tool's view of the user's decision.
            assert card["source"] == ("Placed" if step_id == "structures" else "Drawn"), card
            assert "rank" not in card["name"] and "rank" not in card["source"], card
            assert panel_names[card["id"]].startswith(card["name"]), (card["name"], panel_names[card["id"]])
assert any("would rank" in c["name"] for c in FIXTURE["full"]["panel"]["structures"]), "the fixture must carry a placed site"
assert record["roads"]["cards"][0]["name"] == "Road network" and FIXTURE["full"]["panel"]["roads"][0]["name"] == "Road Network 1"
assert all(c["source"] == "Suggested" for c in record["fencing"]["cards"] + record["roads"]["cards"])
assert record["roads"]["access_point"] == {"text": "40.64330° N, 79.98369° W", "source": "Placed",
                                           "lon_lat": list(document["steps"]["roads"]["features"]["features"][0]["properties"]["access_point"])}
assert [s["step_id"] for s in dr.build_design_record(document)["steps"]] == list(design_document.STEP_ORDER)
# The placed site's broken siting rules, as the panel set them.
placed = next(c for c in record["structures"]["cards"] if c["source"] == "Placed")
assert ("heading", "siting rules broken", None) in _seq(placed["rows"])
# An unknown provenance is a malformed document, not a third kind.
try:
    dr._landform(document["steps"]["landform"]["features"]["features"], {})
    raise AssertionError("a feature with no provenance must raise")
except ValueError:
    pass
# CAUTIONS: the fixture's drawn shapes carry none (the client computes them
# against its own exclusion layers when the ring closes), so the rule is held
# on a drawn block given three: a share, an acreage, and a zero that drops.
drawn = copy.deepcopy(next(f for f in document["steps"]["landform"]["features"]["features"]
                           if document["steps"]["landform"]["provenance"][f["id"]] == "user_added"))
drawn["properties"]["cautions"] = [
    {"type": "hydric", "label": "wet (hydric) soil", "acres": 0.125, "pct": 42.5, "overlapLabel": "wet ground overlap %"},
    {"type": "slope", "label": "slope over the limit", "acres": 0.25},
    {"type": "roads", "label": "existing farm road", "acres": 0},
]
rows = dr._landform([drawn], {drawn["id"]: "user_added"})["cards"][0]["rows"]
assert [(r["kind"], r["value"], r["label"]) for r in rows[-2:]] == [
    ("caution", "43", "wet ground overlap %"), ("caution", "0.3", "acres — slope over the limit")], rows[-2:]
assert not any("farm road" in (r["label"] or "") for r in rows)
print("   generated: 'Suggested · rank N' and the panel's header; drawn: 'Drawn'; placed: 'Placed', no rank; "
      "cautions: share toFixed(0), acres toFixed(1), a zero dropped")

# ======================================================================
# 4. Committed empty
# ======================================================================
print("4. committed empty, in words, end to end")


def record_text(record: dict) -> str:
    """The record as plain text -- a card per feature, as the page sets it."""
    lines = []
    for step in record["steps"]:
        lines.append(step["title"].upper())
        if step["empty"]:
            lines.append(f"  {step['statement']}")
            continue
        if step["step_id"] == "roads":
            lines.append(f"  Access point {step['access_point']['text']}, placed.")
        for card in step["cards"]:
            lines.append(f"  {card['name']}  ({card['source']})")
            for kind, label, text in _seq(card["rows"]):
                if kind == "heading":
                    lines.append(f"      {label}")
                elif kind in ("term", "continuation"):
                    lines.append(f"      {'':>10}  {text}")
                else:
                    lines.append(f"      {text:>10}  {label}")
        lines.append(f"  {step['count']['text']}")
    return "\n".join(lines)


empty_record = dr.build_design_record(FIXTURE["empty_water"]["document"])
water = empty_record["steps"][1]
entry = FIXTURE["empty_water"]["document"]["steps"]["water"]
assert entry["status"] == "committed" and entry["features"]["features"] == [], entry
assert water == {"step_id": "water", "title": "Water", "empty": True, "statement": dr.EMPTY_STATEMENTS["water"]}, water
text = record_text(empty_record)
assert "No water survey areas committed. The design carries no water zone." in text
print("\n".join("   | " + line for line in text.splitlines()))

# Every step, committed empty: six statements, no card. And "not committed"
# is not "committed empty": an unfinished design raises rather than render.
all_empty = copy.deepcopy(FIXTURE["full"]["document"])
for step_id in design_document.STEP_ORDER:
    all_empty["steps"][step_id].update({"features": {"type": "FeatureCollection", "features": []}, "provenance": {}})
statements = [s.get("statement") for s in dr.build_design_record(all_empty)["steps"]]
assert statements == [dr.EMPTY_STATEMENTS[s] for s in design_document.STEP_ORDER] and all(statements)
unfinished = copy.deepcopy(FIXTURE["full"]["document"])
unfinished["steps"]["fencing"] = {"status": "not_started"}
try:
    dr.build_design_record(unfinished)
    raise AssertionError("an uncommitted step must raise")
except ValueError as error:
    assert "fencing" in str(error)
print("   all six empty: six statements; an uncommitted step raises")

# ======================================================================
# 5. Nothing recomputed
# ======================================================================
print("5. nothing recomputed: the record reads the document and calls nothing")
import production_area_ceiling  # noqa: E402
import road_corridors  # noqa: E402
import session_design  # noqa: E402
import session_manager  # noqa: E402
import solar_suitability  # noqa: E402
import step_orchestrator  # noqa: E402
import tree_zone_candidates  # noqa: E402
import water_survey_areas  # noqa: E402
import wire_translation  # noqa: E402
import fencing  # noqa: E402

# Every way the record could reach for what the document already holds: the
# session and its cache, a generate, a rehydration, each KSOP module's own
# entry point. Patched to count; the record must call none of them.
WATCHED = [
    (session_manager, "get_session_context"), (step_orchestrator, "generate_step"),
    (step_orchestrator, "committed_internal_value"), (step_orchestrator, "score_placed_feature"),
    (session_design, "build_session_design"),
    (production_area_ceiling, "build_narrative_data"), (water_survey_areas, "build_narrative_data"),
    (road_corridors, "build_narrative_data"), (road_corridors, "identify_road_corridor_candidates"),
    (tree_zone_candidates, "identify_tree_zone_candidates"), (solar_suitability, "identify_solar_candidate_zones"),
    (fencing, "identify_fencing"), (fencing, "build_narrative_data"),
]
calls = {}
patches = []
for module, name in WATCHED:
    if not hasattr(module, name):
        raise AssertionError(f"{module.__name__}.{name} is gone; update the watch list")
    patcher = patch.object(module, name, side_effect=AssertionError(f"the record called {module.__name__}.{name}"))
    calls[f"{module.__name__}.{name}"] = patcher.start()
    patches.append(patcher)
rehydrators = [n for n in dir(wire_translation) if n.startswith("rehydrate_")]
for name in rehydrators:
    patcher = patch.object(wire_translation, name, side_effect=AssertionError(f"the record called wire_translation.{name}"))
    calls[f"wire_translation.{name}"] = patcher.start()
    patches.append(patcher)
try:
    for case in FIXTURE.values():
        dr.build_design_record(case["document"])
finally:
    for patcher in patches:
        patcher.stop()
total = sum(mock.call_count for mock in calls.values())
assert total == 0, {k: m.call_count for k, m in calls.items() if m.call_count}
assert offline_harness.refused() == [], offline_harness.refused()
print(f"   {len(calls)} entry points watched (session, cache, generate, {len(rehydrators)} rehydrators, KSOP modules): "
      f"{total} calls; {len(offline_harness.refused())} network requests")

print("\ntest_design_record.py: all sections passed")
