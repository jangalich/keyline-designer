"""
test_design_record.py

THE DESIGN RECORD AGAINST THE PANEL, OFFLINE.

design_record_fixture.json holds two fully committed Design Documents made
through the real orchestrator (make_design_record_fixtures.py) and, beside
each, the strings the step panels showed -- read off the generate payloads
where the frontend reads them and formatted by Node's own toFixed. This
file builds the record from each DOCUMENT ALONE and holds every figure it
prints to the panel's string for the same feature and row.

    1. toFixed, reproduced -- on the values where JavaScript and Python differ
    2. every printed figure equals the panel's; the ones left out, and why
    3. names and provenance: suggested with a rank, drawn, placed with none
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
tie_row = dr._structures([tie_site], {"s": "generated"})["rows"][0]
assert (tie_row["score"], tie_row["distance"]) == ("2.3", "3"), tie_row

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
# 2. Every printed figure is the panel's
# ======================================================================
print("2. every printed figure equals the panel's; the ones left out, and why")

# Record column key -> the panel row's label, per step.
ROW_KEYS = {
    "landform": {"acres": "acres", "score": "score"},
    "water": {"acres": "acres", "score": "score"},
    "trees": {"acres": "acres", "score": "score"},
    "structures": {"distance": "distance", "score": "score"},
    "fencing": {"feet": "feet"},
}
# THE ROWS THE RECORD DOES NOT PRINT, and each is a reason in design_record:
# the road network's average grade is never provable from 0.1-rounded
# branches, and the empty-water session's acres served is stored as 2.650 --
# a tie at the panel's one decimal, where the panel printed 2.7 and the
# stored figure cannot say which way the true value fell.
EXPECTED_OMITTED = {
    "full": {("roads", "avg grade %")},
    "empty_water": {("roads", "avg grade %"), ("roads", "acres served")},
}

table = []
for session, case in FIXTURE.items():
    record = dr.build_design_record(case["document"])
    steps = {s["step_id"]: s for s in record["steps"]}
    omitted = set()
    for step_id, keys in ROW_KEYS.items():
        panel_tabs = {tab["id"]: tab for tab in case["panel"][step_id]}
        step = steps[step_id]
        if step["empty"]:
            assert not panel_tabs, f"{session}: {step_id} committed empty but the panel had tabs"
            continue
        assert {row["id"] for row in step["rows"]} == set(panel_tabs), (session, step_id)
        for row in step["rows"]:
            panel = {r["label"]: r["text"] for r in panel_tabs[row["id"]]["rows"]}
            for key, label in keys.items():
                assert row[key] == panel[label], (session, step_id, row["id"], key, row[key], panel[label])
                table.append((session, step_id, row["name"], label, row[key], panel[label]))
    # Roads: label-keyed rows, some provably absent.
    roads_panel = {r["label"]: r["text"] for r in case["panel"]["roads"][0]["rows"]}
    printed = {row["label"]: row["value"] for row in steps["roads"]["rows"]}
    for label, text in roads_panel.items():
        if label in printed:
            assert printed[label] == text, (session, label, printed[label], text)
            table.append((session, "roads", "network", label, printed[label], text))
        else:
            omitted.add(("roads", label))
    assert omitted == EXPECTED_OMITTED[session], (session, omitted)
    stored = case["document"]["steps"]["roads"]["features"]["features"][0]["properties"]["total_served_acres"]
    if ("roads", "acres served") in omitted:
        assert dr._served_acres(case["document"]["steps"]["roads"]["features"]["features"]) is None
        assert round(stored, 1) != float(roads_panel["acres served"]), "the omitted figure is one a naive record gets wrong"

# THE TABLE: every figure the record prints, beside the panel's.
width = max(len(r[2]) for r in table)
for session, step_id, name, label, ours, theirs in table:
    print(f"   {session:<11} {step_id:<10} {name:<{width}} {label:<12} record {ours:>6}   panel {theirs:>6}")
print(f"   {len(table)} figures, every one the panel's; omitted: "
      + "; ".join(f"{s}: {', '.join(sorted(l for _, l in EXPECTED_OMITTED[s]))}" for s in EXPECTED_OMITTED))

# ======================================================================
# 3. Names and provenance
# ======================================================================
print("3. names and provenance: suggested with a rank, drawn, placed with none")
record = dr.build_design_record(FIXTURE["full"]["document"])
steps = {s["step_id"]: s for s in record["steps"]}
provenance = {step_id: FIXTURE["full"]["document"]["steps"][step_id]["provenance"] for step_id in design_document.STEP_ORDER}
for step_id in ("landform", "water", "trees", "structures"):
    panel_names = {tab["id"]: tab["name"] for tab in FIXTURE["full"]["panel"][step_id]}
    for row in steps[step_id]["rows"]:
        kind = provenance[step_id][row["id"]]
        if kind == "generated":
            rank = next(f for f in FIXTURE["full"]["document"]["steps"][step_id]["features"]["features"]
                        if f["id"] == row["id"])["properties"]["rank"]
            assert row["source"] == f"Suggested · rank {rank}", row
            assert row["name"] == panel_names[row["id"]], (row["name"], panel_names[row["id"]])
        else:
            # A USER-ADDED FEATURE CARRIES NO RANK ANYWHERE IN THE RECORD. A
            # drawn zone never was ranked; a placed site was given one by the
            # tool ("would rank N" on the panel), and the record leaves it out:
            # it is the tool's view of the user's decision, not the decision.
            assert row["source"] == ("Placed" if step_id == "structures" else "Drawn"), row
            assert "rank" not in row["name"] and "rank" not in row["source"], row
            assert panel_names[row["id"]].startswith(row["name"]), (row["name"], panel_names[row["id"]])
assert any("would rank" in tab["name"] for tab in FIXTURE["full"]["panel"]["structures"]), "the fixture must carry a placed site"
assert all(r["source"] == "Suggested" for r in steps["fencing"]["rows"]) and steps["roads"]["network_source"] == "Suggested"
assert steps["roads"]["access_point"]["source"] == "Placed"
assert steps["roads"]["access_point"]["text"] == "40.64330° N, 79.98369° W"
assert [s["step_id"] for s in record["steps"]] == list(design_document.STEP_ORDER)
assert steps["structures"]["columns"][2]["label"] == "ft to road"
# An unknown provenance is a malformed document, not a third kind.
try:
    dr._landform(steps and FIXTURE["full"]["document"]["steps"]["landform"]["features"]["features"], {})
    raise AssertionError("a feature with no provenance must raise")
except ValueError:
    pass
print("   generated: 'Suggested · rank N' and the panel's name; drawn: 'Drawn'; placed: 'Placed', no rank; "
      f"order {', '.join(s['title'] for s in record['steps'])}")

# ======================================================================
# 4. Committed empty
# ======================================================================
print("4. committed empty, in words, end to end")


def record_text(record: dict) -> str:
    """The record as plain text -- phase 1's rendering, the page's content."""
    lines = []
    for step in record["steps"]:
        lines.append(step["title"].upper())
        if step["empty"]:
            lines.append(f"  {step['statement']}")
        elif step["step_id"] == "roads":
            lines.append(f"  Access point  {step['access_point']['text']}  ({step['access_point']['source']})")
            lines.append(f"  Road network  ({step['network_source']})")
            lines.extend(f"    {row['label']:<14}{row['value']:>8}" for row in step["rows"])
        else:
            columns = step["columns"]
            widths = [max(len(c["label"]), *(len(r[c["key"]]) for r in step["rows"])) for c in columns]
            lines.append("  " + "  ".join(c["label"].rjust(w) if c["numeric"] else c["label"].ljust(w) for c, w in zip(columns, widths)))
            for row in step["rows"]:
                lines.append("  " + "  ".join(row[c["key"]].rjust(w) if c["numeric"] else row[c["key"]].ljust(w)
                                              for c, w in zip(columns, widths)))
            lines.append(f"  {step['count']}")
    return "\n".join(lines)


empty_record = dr.build_design_record(FIXTURE["empty_water"]["document"])
water = empty_record["steps"][1]
entry = FIXTURE["empty_water"]["document"]["steps"]["water"]
assert entry["status"] == "committed" and entry["features"]["features"] == [], entry
assert water == {"step_id": "water", "title": "Water", "empty": True, "statement": dr.EMPTY_STATEMENTS["water"]}, water
assert "rows" not in water and "columns" not in water
text = record_text(empty_record)
assert "No water survey areas committed. The design carries no water zone." in text
print("\n".join("   | " + line for line in text.splitlines()))

# Every step, committed empty: six statements, no table. And "not committed"
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
