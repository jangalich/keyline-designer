"""
test_panel_feature_parity.py

SUGGESTED FEATURES CARRY WHAT THEIR PANEL DISPLAYED, AND NOTHING MOVED.

A drawn block and a drawn tree zone have always carried their panel's
readings on the Feature -- the server spreads the same row builder's output
onto it -- while a suggested one's lived only in the payload's `zones`
table, so a commit could not store them and the Design Document could not
see them. The payload assemblers now copy the displayed values from the
rows the panel reads onto the suggested Features (landform, water, roads,
trees). This file holds that change to four things:

    1. parity: the panel-displayed properties, the same on suggested and drawn features
    2. every added property formats to the string the panel displayed (Node's, from the fixture)
    3. nothing moved: main's payloads, committed geometries and exclusion result, byte for byte
    4. the consumers: commit, the rehydrators, the layers payloads, a reopen's restore
    5. a document committed before this change still validates, opens and rehydrates

Offline: fencing_step_fixture's parcel, DEM and mocked fetches.
panel_parity_fixture.json is built by make_panel_parity_fixture.py from a
capture on main and one here; see it for what each part holds.
"""

import copy
import json
import os
from decimal import ROUND_HALF_UP, Decimal

import offline_harness

offline_harness.install()

import design_document  # noqa: E402
import fencing_step_fixture as F  # noqa: E402
import make_panel_parity_fixture as parity  # noqa: E402
import session_manager  # noqa: E402
import step_orchestrator  # noqa: E402
from production_zone_payload import PANEL_READING_FIELDS  # noqa: E402
from step_orchestrator import TREE_PANEL_READING_FIELDS  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "panel_parity_fixture.json"), encoding="utf-8") as handle:
    FIXTURE = json.load(handle)
EM_DASH = "—"

# The fields the frontend merges onto a DRAWN feature from the server's
# reading (stepDefinitions.js DRAWN_BLOCK_READING_FIELDS and
# DRAWN_TREE_ZONE_READING_FIELDS), the panel-displayed ones among them. Read
# from the frontend's source when the sibling checkout is present, so a
# change there fails here.
FRONTEND = os.path.join(HERE, "..", "keyline-designer-frontend", "src", "wizard", "stepDefinitions.js")


def _frontend_list(name):
    if not os.path.exists(FRONTEND):
        return None
    with open(FRONTEND, encoding="utf-8") as handle:
        source = handle.read()
    block = source[source.index(f"const {name} = Object.freeze(["):]
    block = block[:block.index("])")]
    return [line.strip().strip(",").strip("'") for line in block.splitlines()[1:] if line.strip().startswith("'")]


def to_fixed(value, dp):
    """JavaScript's toFixed on the exact double (ties away from zero)."""
    if value is None:
        return EM_DASH
    number = float(value) or 0.0
    return str(Decimal(number).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP))


print("=" * 72)
print("test_panel_feature_parity.py -- suggested features carry what their panel displayed")
print("=" * 72)

with F.Harness():
    RUN = parity.run_design(own_harness=False)
    SESSION = RUN["session"]
    PAYLOADS = RUN["payloads"]
    DOCUMENT = RUN["document"]

    # ==================================================================
    # 1. Parity
    # ==================================================================
    print("1. parity: the panel-displayed properties, the same on suggested and drawn features")
    assert tuple(PANEL_READING_FIELDS) == parity.ADDED_PROPERTIES["landform"]
    assert tuple(TREE_PANEL_READING_FIELDS) == parity.ADDED_PROPERTIES["trees"]
    suggested = {
        "landform": PAYLOADS["landform"]["suggested_zones"]["features"],
        "water": [f for f in PAYLOADS["water"]["survey_zones"]["features"]
                  if not f["properties"]["layer"].startswith("survey_zone_member_")],
        "roads": PAYLOADS["roads"]["road_corridors"]["features"],
        "trees": PAYLOADS["trees"]["tree_zones"]["features"],
    }
    drawn = {"landform": PAYLOADS["drawn_block"], "trees": PAYLOADS["drawn_tree"]}
    frontend_merge = {"landform": _frontend_list("DRAWN_BLOCK_READING_FIELDS"),
                      "trees": _frontend_list("DRAWN_TREE_ZONE_READING_FIELDS")}
    for step, fields in parity.ADDED_PROPERTIES.items():
        assert suggested[step], step
        for feature in suggested[step]:
            missing = set(fields) - set(feature["properties"])
            assert not missing, (step, feature["id"], missing)
        if step in drawn:
            # THE SAME PANEL-DISPLAYED SET on the two kinds of feature: the
            # server's drawn Feature carries them, and so does what the
            # frontend keeps of it (its reading-field merge list).
            shown_suggested = {frozenset(set(fields) & set(f["properties"])) for f in suggested[step]}
            shown_drawn = frozenset(set(fields) & set(drawn[step]["properties"]))
            assert shown_suggested == {shown_drawn} == {frozenset(fields)}, (step, shown_suggested, shown_drawn)
            if frontend_merge[step] is not None:
                assert set(fields) <= set(frontend_merge[step]), (step, set(fields) - set(frontend_merge[step]))
    # A suggested feature carries no cautions: it cannot cross an exclusion.
    assert not any("cautions" in f["properties"] for f in suggested["landform"] + suggested["trees"])
    # And the displayed water score is named and shaped apart from the stored 0-1 mean.
    for feature in suggested["water"]:
        display, mean = feature["properties"]["suitability_display"], feature["properties"]["mean_suitability"]
        assert set(display) == {"value", "unit"} and display["unit"] == "/100" and isinstance(display["value"], int)
        assert 0.0 <= mean <= 1.0 and not isinstance(mean, int)
    print("   landform 6, water 2 (+ partner identity), roads 1, trees 3: on every suggested feature; landform and trees "
          "the same set as the drawn feature" + ("" if frontend_merge["landform"] else " (frontend list not checked: no sibling checkout)"))

    # ==================================================================
    # 2. The panel's strings
    # ==================================================================
    print("2. every added property formats to the string the panel displayed")
    checked = 0
    panel = FIXTURE["panel"]
    for feature in suggested["landform"]:
        p, shown = feature["properties"], panel["landform"][feature["id"]]
        mine = {
            "aspect": f"{p['dominant_aspect']} facing" if p["aspect_available"] and p["dominant_aspect"] else EM_DASH,
            "position": p["elevation_position"] or EM_DASH,
            "median slope %": to_fixed(p["slope_median_pct"], 1),
            "soil": [entry["label"] for entry in p["soil_components"] or []] or [EM_DASH],
            "drainage": p["drainage_class"] or EM_DASH,
        }
        assert mine == shown, (feature["id"], mine, shown)
        checked += len(mine)
    names_checked = 0
    for feature in suggested["water"]:
        p, shown = feature["properties"], panel["water"][feature["id"]]
        assert to_fixed(p["suitability_display"]["value"], 0) == shown["score"], (feature["id"], p["suitability_display"], shown)
        assert f"{p['suitability_display']['unit']} score" == shown["score_label"]
        assert p["water_delivery"].replace("_", " ") == shown["water delivery"]
        names = [f"{e['survey_type'][:1].upper() + e['survey_type'][1:]} {e['rank']}" if e["presented"] else "an area not shown"
                 for e in p["cross_type_overlaps"]]
        assert names == shown["shared ground names"], (feature["id"], names, shown["shared ground names"])
        checked += 3
        names_checked += len(names)
    for feature in suggested["roads"]:
        shown = panel["roads"][feature["properties"]["network_id"]]
        assert to_fixed(feature["properties"]["terrain_quality_score"], 0) == shown["score"]
        checked += 1
    for feature in suggested["trees"]:
        p, shown = feature["properties"], panel["trees"][feature["id"]]
        mine = {"where in the parcel": p["position_in_parcel"] or EM_DASH, "position": p["elevation_position"] or EM_DASH,
                "marginal benefits": list(p["marginal_benefits"])}
        assert mine == shown, (feature["id"], mine, shown)
        checked += len(mine)
    unnamed = sum(1 for f in suggested["water"] for e in f["properties"]["cross_type_overlaps"] if not e["presented"])
    assert unnamed, "the fixture must carry a partner the panel could not name"
    print(f"   {checked} rows and {names_checked} partner names, each the panel's string; {unnamed} partners 'an area not shown'")
    # Copies, not derivations: each is the value on the row the panel reads.
    for feature in suggested["landform"]:
        row = next(z for z in PAYLOADS["landform"]["zones"] if z["feature_id"] == feature["id"])
        assert all(feature["properties"][k] == row[k] for k in PANEL_READING_FIELDS)
    for feature in suggested["trees"]:
        row = next(z for z in PAYLOADS["trees"]["zones"] if z["feature_id"] == feature["id"])
        assert all(feature["properties"][k] == row[k] for k in TREE_PANEL_READING_FIELDS)
    for feature in suggested["water"]:
        row = {r["key"]: r for r in next(z for z in PAYLOADS["water"]["zones"] if z["feature_id"] == feature["id"])["panel"]}
        assert feature["properties"]["suitability_display"] == {"value": row["suitability"]["value"], "unit": row["suitability"]["unit"]}
        assert feature["properties"]["water_delivery"] == row["water_delivery"]["value"]

    # ==================================================================
    # 3. Nothing moved
    # ==================================================================
    print("3. nothing moved: main's payloads, committed geometries and exclusion result, byte for byte")
    main = FIXTURE["main_hashes"]
    for key, payload in PAYLOADS.items():
        assert parity.sha(parity.strip_added(payload, parity.PAYLOAD_STEP[key])) == main["payloads"][key], key
    assert parity.sha(parity.committed_geometries(json.loads(parity.canonical(DOCUMENT)))) == main["committed_geometries"]
    assert RUN["exclusion_hashes"] == main["exclusion"], "the exclusion result moved"
    print(f"   {len(PAYLOADS)} payloads equal main's once the added values are removed; "
          f"{len(parity.committed_geometries(DOCUMENT))} committed geometries and {len(main['exclusion'])} exclusion "
          "arrays and geometries identical")

    # ==================================================================
    # 4. The consumers
    # ==================================================================
    print("4. the consumers: commit, the rehydrators, the layers payloads, a reopen's restore")
    # COMMIT stored the richer features verbatim: the document holds what was sent.
    for step, fields in parity.ADDED_PROPERTIES.items():
        committed = DOCUMENT["steps"][step]["features"]["features"]
        assert committed and all(set(fields) <= set(f["properties"]) for f in committed), step
    context = SESSION.context()
    # THE REHYDRATORS read their own field lists and ignore the rest.
    for step in ("landform", "water", "roads", "trees", "structures"):
        assert step_orchestrator.committed_internal_value(context, SESSION.stored(), step) is not None, step
    # A REOPEN restores the selection out of the regenerated proposals -- the
    # committed ids still match -- and THE LAYERS PAYLOAD the map draws while
    # a step is open is the generate payload, the added properties on it. (A
    # committed step's map draws the document's own features, stored above.)
    reopened = SESSION.reopen("trees")
    restored = json.dumps(reopened, default=str)
    assert '"missing_feature_ids": []' in restored or "missing_feature_ids" not in restored, restored[:400]
    layers = SESSION.layers("trees")
    assert all(set(TREE_PANEL_READING_FIELDS) <= set(f["properties"]) for f in layers["tree_zones"]["features"])
    print("   4 steps committed with the added properties stored verbatim; 5 rehydrated; trees reopened with nothing "
          "missing, its layers payload carrying the additions")

    # ==================================================================
    # 5. A pre-change document
    # ==================================================================
    print("5. a document committed before this change still validates, opens and rehydrates")
    old = copy.deepcopy(FIXTURE["pre_parity_document"])
    design_document.validate_document(old)
    for step, fields in parity.ADDED_PROPERTIES.items():
        for feature in old["steps"][step]["features"]["features"]:
            assert not (set(fields) & set(feature["properties"])), (step, "the fixture is not pre-change")
            for entry in feature["properties"].get("cross_type_overlaps") or []:
                assert "presented" not in entry
    store = F._fresh_store()
    store.put(old)
    fetch_cache, cache = F._fresh_caches()
    old_context = session_manager.get_session_context(old["session_id"], store, fetch_cache=fetch_cache, cache=cache)
    for step in design_document.STEP_ORDER:
        if old["steps"][step]["status"] == design_document.STATUS_COMMITTED:
            assert step_orchestrator.committed_internal_value(old_context, old, step) is not None, step
    print("   main's committed document: valid, none of the new properties, opened and rehydrated step by step")

print("\ntest_panel_feature_parity.py: all sections passed")
print(offline_harness.summary())
