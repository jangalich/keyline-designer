"""
make_design_record_fixtures.py

BUILDS design_record_fixture.json: two fully committed Design Documents,
each beside the strings its step panels showed, for test_design_record.py
to hold the record against offline.

    python make_design_record_fixtures.py

Run through the real orchestrator on the fencing step's fixture (the
reference parcel, its DEM and every mocked fetch boundary -- offline,
fencing_step_fixture.Harness), committing every step of STEP_ORDER:

    full          landform: every suggested block plus one DRAWN block;
                  water: two survey areas; roads: access point A;
                  trees: the top zone plus one DRAWN zone; structures:
                  the top site plus one PLACED site; fencing: every
                  candidate type.
    empty_water   the same, except water is COMMITTED EMPTY -- the
                  decision the record must state in words -- and so the
                  water-area fence type never exists.

A drawn block and a drawn tree zone are built the way the frontend builds
them: the ring, the client's own acreage (a port of geo.js
multiPolygonAreaAcres, which is what `properties.acres` holds on a drawn
tab), and the reading fields the server's score_placed_feature returns
merged into properties. A placed site is the verb's own Feature.

THE PANEL'S STRINGS ARE JAVASCRIPT'S. Each committed feature's panel rows
are read off the generate payload exactly where the frontend's tabs() and
detail() read them (stepDefinitions.js), and formatted by Node's own
Number.prototype.toFixed -- not by design_record.to_fixed, which is the
thing under test. Node is needed to BUILD the fixture, never to run the
test.
"""

import json
import math
import os
import subprocess
import sys

import offline_harness

offline_harness.install()

import design_document  # noqa: E402
import fencing_step_fixture as F  # noqa: E402
import step_orchestrator  # noqa: E402

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "design_record_fixture.json")

# Inside the parcel, clear of its edges: one drawn landform block, one
# drawn tree zone, one placed structure site.
DRAWN_BLOCK_RING = [(-79.98303, 40.64342), (-79.98291, 40.64342), (-79.98291, 40.64390), (-79.98303, 40.64390)]
DRAWN_TREE_RING = [(-79.98350, 40.64420), (-79.98325, 40.64420), (-79.98325, 40.64445), (-79.98350, 40.64445)]
PLACED_SITE = (-79.98280, 40.64500)

# geo.js
METRES_PER_DEGREE_LATITUDE = 111132.0
METRES_PER_DEGREE_LONGITUDE_AT_EQUATOR = 111320.0
SQUARE_METRES_PER_ACRE = 4046.8564224

# stepDefinitions.js DRAWN_BLOCK_READING_FIELDS, and the trees step's equivalent
DRAWN_BLOCK_READING_FIELDS = (
    "block_origin", "score", "factors", "slope_min_pct", "slope_max_pct", "slope_median_pct", "avg_slope_pct",
    "dominant_aspect", "aspect_consistency_pct", "aspect_available", "position_in_parcel", "soil_components",
    "drainage_class", "soil_available", "elevation_position", "percent_of_parcel",
)


def client_ring_acres(ring_lon_lat) -> float:
    """geo.js multiPolygonAreaAcres() for one ring, [lng, lat] order."""
    ring = [list(p) for p in ring_lon_lat]
    scale = math.cos(sum(lat for _, lat in ring) / len(ring) * math.pi / 180)
    double_area = 0.0
    for i in range(len(ring)):
        lng1, lat1 = ring[i]
        lng2, lat2 = ring[(i + 1) % len(ring)]
        double_area += lat1 * (lng2 * scale) - lat2 * (lng1 * scale)
    square_metres = abs(double_area) / 2 * METRES_PER_DEGREE_LATITUDE * METRES_PER_DEGREE_LONGITUDE_AT_EQUATOR
    return square_metres / SQUARE_METRES_PER_ACRE


def drawn_feature(session, step_id: str, feature_id: str, ring, reading_fields=None) -> dict:
    """A drawn zone as the frontend commits it: the ring, the client's
    acreage, and the server's reading merged into properties."""
    scored = step_orchestrator.score_placed_feature(
        session.id, step_id, session.store, params={"ring": [list(p) for p in ring]},
        fetch_cache=session.fetch_cache, cache=session.cache,
    )
    reading = scored["properties"]
    if reading_fields is not None:
        reading = {k: reading[k] for k in reading_fields if k in reading}
    closed = [list(p) for p in ring] + [list(ring[0])]
    return {
        "id": feature_id,
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [closed]},
        "properties": {**scored["properties"], **reading, "acres": client_ring_acres(ring)},
    }


# --- the panel, read where the frontend reads it -------------------------


def _row(label, value, dp):
    return {"label": label, "value": value, "dp": dp}


def landform_panel(payload, committed, drawn):
    zones = {z["feature_id"]: z for z in payload["zones"]}
    tabs = []
    for f in committed:
        z = zones[f["id"]]
        tabs.append({"id": f["id"], "name": f"Block {z['rank']}",
                     "rows": [_row("acres", z["area_acres"], 1), _row("score", z["score"], 1)]})
    for index, f in enumerate(drawn):
        tabs.append({"id": f["id"], "name": f"Drawn {index + 1}",
                     "rows": [_row("acres", f["properties"]["acres"], 1), _row("score", f["properties"]["score"], 1)]})
    return tabs


def water_panel(payload, committed):
    zones = {z["feature_id"]: z for z in payload["zones"]}
    tabs = []
    for f in committed:
        p = f["properties"]
        suitability = next(r for r in zones[f["id"]]["panel"] if r["key"] == "suitability")
        name = f"{p['survey_type'][:1].upper()}{p['survey_type'][1:]} {p['rank']}"
        tabs.append({"id": f["id"], "name": name,
                     "rows": [_row("acres", p["zone_acres"], 1), _row("score", suitability["value"], 0)]})
    return tabs


def roads_panel(payload, network_id):
    network = next(n for n in payload["networks"] if n["network_id"] == network_id)
    return [{"id": network_id, "name": "network", "rows": [
        _row("length ft", network["access"]["total_length_ft"], 0),
        _row("avg grade %", network["determination"]["avg_grade_pct"], 1),
        _row("max grade %", network["determination"]["max_grade_pct"], 1),
        _row("acres served", network["access"]["served_acres"], 1),
    ]}]


def trees_panel(payload, committed, drawn):
    zones = {z["feature_id"]: z for z in payload["zones"]}
    tabs = []
    for f in committed:
        z = zones[f["id"]]
        tabs.append({"id": f["id"], "name": f"Zone {z['rank']}",
                     "rows": [_row("acres", z["area_acres"], 1), _row("score", z["score"], 1)]})
    for index, f in enumerate(drawn):
        tabs.append({"id": f["id"], "name": f"Drawn {index + 1}",
                     "rows": [_row("acres", f["properties"]["acres"], 1), _row("score", f["properties"].get("score"), 1)]})
    return tabs


def structures_panel(payload, committed, placed):
    tabs = []
    for f in committed:
        p = f["properties"]
        tabs.append({"id": f["id"], "name": f"Site {p['rank']}",
                     "rows": [_row("distance", p["distance_to_road_ft"], 0), _row("score", p["suitability_score"], 1)]})
    for index, f in enumerate(placed):
        p = f["properties"]
        tabs.append({"id": f["id"], "name": f"Placed {index + 1} · would rank {p['rank']}",
                     "rows": [_row("distance", p["distance_to_road_ft"], 0), _row("score", p["suitability_score"], 1)]})
    return tabs, payload["summary"]["road_proximity_source"]


def fencing_panel(payload, types):
    return [{"id": block["fence_type"], "name": block["label"], "rows": [_row("feet", block["total_length_ft"], 0)]}
            for block in payload["fence_types"] if block["candidate"] and block["fence_type"] in types]


def js_format(panel: dict) -> dict:
    """Every row's string through Node's toFixed (the panel's measure())."""
    rows = [row for tabs in panel.values() if isinstance(tabs, list) for tab in tabs for row in tab["rows"]]
    script = ("const rows = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
              "process.stdout.write(JSON.stringify(rows.map(([v, dp]) => v == null ? '\\u2014' : Number(v).toFixed(dp))))")
    out = subprocess.run(["node", "-e", script], input=json.dumps([[r["value"], r["dp"]] for r in rows]),
                         capture_output=True, text=True, check=True).stdout
    for row, text in zip(rows, json.loads(out)):
        row["text"] = text
    return panel


# --- the two sessions ----------------------------------------------------


def build(empty_water: bool) -> dict:
    s = F.Session()
    panel = {}

    payload = s.generate("landform")
    suggested = payload["suggested_zones"]["features"]
    block = drawn_feature(s, "landform", "drawn-block-1", DRAWN_BLOCK_RING, DRAWN_BLOCK_READING_FIELDS)
    s.commit("landform", suggested + [block],
             {**{f["id"]: "generated" for f in suggested}, block["id"]: "user_added"})
    panel["landform"] = landform_panel(payload, suggested, [block])

    zones = [] if empty_water else s.water_zones()[:2]
    payload = s.generate("water") if not empty_water else None
    s.commit("water", zones, {f["id"]: "generated" for f in zones})
    panel["water"] = water_panel(payload, zones) if zones else []

    payload = s.generate("roads", {"access_point": list(F.ACCESS_A)})
    network = payload["networks"][0]
    branches = [f for f in payload["road_corridors"]["features"] if f["properties"]["network_id"] == network["network_id"]]
    s.commit("roads", branches, {f["id"]: "generated" for f in branches}, inputs={"access_points": [list(F.ACCESS_A)]})
    panel["roads"] = roads_panel(payload, network["network_id"])

    payload = s.generate("trees")
    top = payload["tree_zones"]["features"][:1]
    tree = drawn_feature(s, "trees", "drawn-tree-zone-1", DRAWN_TREE_RING)
    s.commit("trees", top + [tree], {**{f["id"]: "generated" for f in top}, tree["id"]: "user_added"})
    panel["trees"] = trees_panel(payload, top, [tree])

    payload = s.generate("structures")
    site = payload["structure_sites"]["features"][:1]
    placed = s.score(PLACED_SITE)
    s.commit("structures", site + [placed], {**{f["id"]: "generated" for f in site}, placed["id"]: "user_added"})
    panel["structures"], panel["road_proximity_source"] = structures_panel(payload, site, [placed])

    payload = s.generate("fencing")
    types = [b["fence_type"] for b in payload["fence_types"] if b["candidate"]]
    fences = [f for f in payload["fence_lines"]["features"] if f["properties"]["fence_type"] in types]
    s.commit("fencing", fences, {f["id"]: "generated" for f in fences})
    panel["fencing"] = fencing_panel(payload, types)

    document = s.stored()
    assert all(document["steps"][step]["status"] == "committed" for step in design_document.STEP_ORDER)
    return {"document": document, "panel": js_format(panel)}


def main():
    with F.Harness():
        fixture = {"full": build(empty_water=False), "empty_water": build(empty_water=True)}
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        json.dump(fixture, handle, indent=1, sort_keys=True)
        handle.write("\n")
    print(f"wrote {OUTPUT} ({os.path.getsize(OUTPUT):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
