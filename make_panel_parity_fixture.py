"""
make_panel_parity_fixture.py

BUILDS panel_parity_fixture.json for test_panel_feature_parity.py: what
the step panels displayed for every property this branch brings onto
suggested features, and what main's code produced before it did.

    python make_panel_parity_fixture.py capture OUT.json
        One offline design run on THIS checkout (fencing_step_fixture's
        parcel, DEM and mocked fetches): every generate payload, a drawn
        block and a drawn tree zone, the Design Document with all six steps
        committed, and a hash of every array and geometry in the context's
        exclusion result.

    python make_panel_parity_fixture.py build MAIN.json BRANCH.json
        The fixture, from a capture made in a worktree of main (the code
        before this branch) and one made here.

What the fixture holds:

  panel          per step, per feature id, the STRING the panel displays
                 for each row this branch stores on the feature -- read off
                 the payload where the frontend reads it (the `zones` rows,
                 the water panel rows, the road network's quality block,
                 the presented survey-zone set) and formatted as the
                 frontend formats it, every figure by NODE's own toFixed.
  main_hashes    sha256 of main's payloads, of main's committed geometries
                 and of main's exclusion result -- so the test proves this
                 branch moved nothing but the properties it adds, against
                 main's own output rather than against itself.
  pre_parity_document
                 the Design Document main's code committed: features
                 WITHOUT the new properties, the document every saved
                 session holds today. The record rebuild degrades against
                 it; this branch's test holds that it still validates and
                 rehydrates.
"""

import hashlib
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(HERE, "panel_parity_fixture.json")
EM_DASH = "—"

# What this branch adds, by step. Kept here, beside the strip, so the test
# and the builder cannot disagree about what "only the added properties" is.
ADDED_PROPERTIES = {
    "landform": ("dominant_aspect", "aspect_available", "elevation_position", "slope_median_pct", "soil_components",
                 "drainage_class"),
    "water": ("suitability_display", "water_delivery"),
    "roads": ("terrain_quality_score",),
    "trees": ("position_in_parcel", "elevation_position", "marginal_benefits"),
}
ADDED_OVERLAP_KEYS = ("presented", "survey_type", "rank")

BLOCK_RING = [(-79.98303, 40.64342), (-79.98291, 40.64342), (-79.98291, 40.64390), (-79.98303, 40.64390)]
TREE_RING = [(-79.98350, 40.64420), (-79.98325, 40.64420), (-79.98325, 40.64445), (-79.98350, 40.64445)]


# ======================================================================
# Shared with the test
# ======================================================================


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def sha(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def iter_features(tree):
    if isinstance(tree, dict):
        if tree.get("type") == "Feature":
            yield tree
        for value in tree.values():
            yield from iter_features(value)
    elif isinstance(tree, list):
        for value in tree:
            yield from iter_features(value)


PAYLOAD_STEP = {"landform": "landform", "water": "water", "roads": "roads", "trees": "trees",
                "drawn_block": None, "drawn_tree": None}


def strip_added(tree, step):
    """A deep copy of one step's payload (or committed FeatureCollection)
    with exactly this branch's additions for THAT step removed -- its
    ADDED_PROPERTIES and, for water, the three keys added to each
    cross_type_overlaps entry. Per step, because a name this branch adds to
    one step's features can be a field another step's features always
    carried (water's slope_median_pct). What is left must be main's output
    byte for byte. A drawn feature's payload (step None) is untouched: it
    carried its readings before this branch and still does."""
    tree = json.loads(canonical(tree))
    if step is None:
        return tree
    for feature in iter_features(tree):
        properties = feature["properties"]
        for name in ADDED_PROPERTIES[step]:
            properties.pop(name, None)
        if step == "water":
            for entry in properties.get("cross_type_overlaps") or []:
                for key in ADDED_OVERLAP_KEYS:
                    entry.pop(key, None)
    return tree


def committed_geometries(document) -> list:
    return [[f["id"], f["geometry"]] for f in iter_features(document)]


# ======================================================================
# Capture
# ======================================================================


def capture(out_path):
    data = run_design()
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump({k: v for k, v in data.items() if k != "session"}, handle, sort_keys=True, default=str)
    print(f"captured {out_path}")


def run_design(own_harness: bool = True) -> dict:
    """The offline design run capture() writes, returned: {payloads,
    document, exclusion_hashes, session}. `session` is the live
    fencing_step_fixture.Session, for a caller that goes on to read it --
    which passes own_harness=False and holds fencing_step_fixture.Harness
    open itself, so the session's fetch boundaries stay patched."""
    import contextlib
    sys.path.insert(0, HERE)
    import offline_harness

    offline_harness.install()
    import numpy as np

    import fencing_step_fixture as F
    import step_orchestrator

    def score(session, step, ring):
        return step_orchestrator.score_placed_feature(session.id, step, session.store, params={"ring": [list(p) for p in ring]},
                                                      fetch_cache=session.fetch_cache, cache=session.cache)

    def hashes(obj, prefix=""):
        out = {}
        if isinstance(obj, np.ndarray):
            out[prefix] = hashlib.sha256(obj.tobytes()).hexdigest() + str(obj.shape)
        elif hasattr(obj, "wkb"):
            out[prefix] = hashlib.sha256(obj.wkb).hexdigest()
        elif isinstance(obj, dict):
            for key, value in obj.items():
                out.update(hashes(value, f"{prefix}.{key}"))
        elif isinstance(obj, (list, tuple)):
            for index, value in enumerate(obj):
                out.update(hashes(value, f"{prefix}[{index}]"))
        return out

    with (F.Harness() if own_harness else contextlib.nullcontext()):
        s = F.Session()
        payloads = {"landform": s.generate("landform"), "drawn_block": score(s, "landform", BLOCK_RING)}
        s.commit_landform()
        payloads["water"] = s.generate("water")
        s.commit_water(2)
        payloads["roads"] = s.generate("roads", {"access_point": list(F.ACCESS_A)})
        s.commit_roads()
        payloads["trees"] = s.generate("trees")
        payloads["drawn_tree"] = score(s, "trees", TREE_RING)
        s.commit_trees(1)
        s.commit_structures(1)
        # Fencing too, every candidate type, so the document is a FINISHED
        # design -- the only kind a design record is built from.
        fencing = s.generate("fencing")
        types = {b["fence_type"] for b in fencing["fence_types"] if b["candidate"]}
        fences = [f for f in fencing["fence_lines"]["features"] if f["properties"]["fence_type"] in types]
        s.commit("fencing", fences, {f["id"]: "generated" for f in fences})
        context = s.context()
        return {"payloads": payloads, "document": s.stored(),
                "exclusion_hashes": hashes(context.exclusion_zones, "exclusion"), "session": s}


# ======================================================================
# The panel's strings, from where the panel reads them
# ======================================================================


def _node_to_fixed(pairs):
    script = ("const rows = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              "process.stdout.write(JSON.stringify(rows.map(([v, dp]) => v == null ? '\\u2014' : Number(v).toFixed(dp))))")
    out = subprocess.run(["node", "-e", script], input=json.dumps(pairs), capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def panel_strings(payloads) -> dict:
    """{step: {feature id: {row label: string or [strings]}}} for every row
    this branch stores -- the frontend's productionBlockRows, the water
    tab's score and detail rows, the road tab's score, treeZoneRows."""
    numbers = []   # (step, feature id, label, value, dp), formatted by Node in one call
    panel = {"landform": {}, "water": {}, "roads": {}, "trees": {}}

    for row in payloads["landform"]["zones"]:
        rows = panel["landform"][row["feature_id"]] = {}
        rows["aspect"] = (f"{row['dominant_aspect']} facing" if row.get("aspect_available") and row.get("dominant_aspect")
                          else EM_DASH)
        rows["position"] = row.get("elevation_position") or EM_DASH
        numbers.append(("landform", row["feature_id"], "median slope %", row.get("slope_median_pct"), 1))
        rows["soil"] = [entry["label"] for entry in row.get("soil_components") or []] or [EM_DASH]
        rows["drainage"] = row.get("drainage_class") or EM_DASH

    water = payloads["water"]
    envelopes = {f["properties"]["zone_id"]: f for f in water["survey_zones"]["features"]
                 if not f["properties"]["layer"].startswith("survey_zone_member_")}
    top = water["scales"]["suitability"].get("max") or water["scales"]["suitability"]["range"][1]
    for row in water["zones"]:
        feature = envelopes[row["id"]]
        panel_rows = {entry["key"]: entry for entry in row["panel"]}
        rows = panel["water"][row["feature_id"]] = {"score_label": f"/{round(top)} score"}
        numbers.append(("water", row["feature_id"], "score", panel_rows["suitability"]["value"], 0))
        value = panel_rows["water_delivery"]["value"]
        rows["water delivery"] = value.replace("_", " ") if isinstance(value, str) else EM_DASH
        names = []
        for entry in feature["properties"].get("cross_type_overlaps") or []:
            partner = envelopes.get(entry["zone_id"])
            if partner is None:
                names.append("an area not shown")
            else:
                kind = partner["properties"]["survey_type"]
                names.append(f"{kind[:1].upper() + kind[1:]} {partner['properties']['rank']}")
        rows["shared ground names"] = names

    for network in payloads["roads"]["networks"]:
        panel["roads"][network["network_id"]] = {}
        numbers.append(("roads", network["network_id"], "score", network["quality"]["terrain_quality_score"], 0))

    for row in payloads["trees"]["zones"]:
        panel["trees"][row["feature_id"]] = {
            "where in the parcel": row.get("position_in_parcel") or EM_DASH,
            "position": row.get("elevation_position") or EM_DASH,
            "marginal benefits": list(row.get("marginal_benefits") or []),
        }

    for (step, feature_id, label, _, _), text in zip(numbers, _node_to_fixed([[n[3], n[4]] for n in numbers])):
        panel[step][feature_id][label] = text
    return panel


def build(main_path, branch_path):
    with open(main_path, encoding="utf-8") as handle:
        main = json.load(handle)
    with open(branch_path, encoding="utf-8") as handle:
        branch = json.load(handle)
    fixture = {
        "panel": panel_strings(branch["payloads"]),
        "main_hashes": {
            "payloads": {key: sha(value) for key, value in main["payloads"].items()},
            "committed_geometries": sha(committed_geometries(main["document"])),
            "exclusion": main["exclusion_hashes"],
        },
        "pre_parity_document": main["document"],
    }
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        json.dump(fixture, handle, indent=1, sort_keys=True)
        handle.write("\n")
    print(f"wrote {OUTPUT} ({os.path.getsize(OUTPUT):,} bytes)")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "capture" and len(sys.argv) == 3:
        capture(sys.argv[2])
    elif command == "build" and len(sys.argv) == 4:
        build(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
