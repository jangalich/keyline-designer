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

THE PANEL'S STRINGS ARE JAVASCRIPT'S. Each committed feature's DATA PANEL
-- header, tab rows, detail rows -- is read off the generate payload
exactly where the frontend's tabs() and detail() read them
(stepDefinitions.js), and every figure is formatted by Node's own
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
import wire_translation  # noqa: E402

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
#
# Each committed feature's DATA PANEL, as DetailPanel.jsx assembles it:
# the header (headerFor: the tab's name), then panelBody(tab rows, detail
# rows) -- the tab's rows with their denominators, a break, the step's
# detail() rows -- then the caution run. Values are read off the generate
# PAYLOAD (the session's data), never off the committed document; figures
# carry (raw value, dp) and are formatted by Node below. Every label and
# every label template used here is asserted to appear verbatim in the
# frontend's stepDefinitions.js, so a port that drifted from the source
# fails at build time.

FRONTEND_STEPS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "keyline-designer-frontend",
                              "src", "wizard", "stepDefinitions.js")
with open(FRONTEND_STEPS, encoding="utf-8") as _handle:
    _FRONTEND_SOURCE = _handle.read()
EM_DASH = "\u2014"


def _in_source(*literals):
    for literal in literals:
        assert literal in _FRONTEND_SOURCE, f"panel label {literal!r} is not in stepDefinitions.js"


def M(value, label, dp=1):
    return {"kind": "measured", "value": value, "dp": dp, "label": label}


def C(value, label):
    return {"kind": "categorical", "text": value if value is not None else EM_DASH, "label": label}


def T(value):
    return {"kind": "term", "text": value, "label": None}


def B(label=None):
    return {"kind": "break", "label": label}


def drops_at_zero(value, row):
    if value is None:
        return row
    return None if float(value) == 0 else row


def labelled_run(values, label):
    values = list(values or [])
    if not values:
        return [C(EM_DASH, label)]
    return [C(values[0], label)] + [{"kind": "continuation", "text": v, "label": None} for v in values[1:]]


def panel_body(tab, rows):
    rows = [r for r in rows if r is not None]
    joined = tab + ([B()] if tab and rows else []) + rows
    body = []
    for row in joined:
        if row["kind"] != "break":
            body.append(row)
        elif body and body[-1]["kind"] != "break":
            body.append(row)
        elif body and row["label"] and not body[-1]["label"]:
            body[-1] = row
    while body and body[-1]["kind"] == "break":
        body.pop()
    return body


def denominator(scales, quantity=None):
    entry = (scales or {}).get(quantity) if quantity else None
    top = ((scales or {}).get("range") or [None, None])[1]
    if top is None and entry:
        top = (entry.get("range") or [None, None])[1] if entry.get("range") else entry.get("max")
    return None if top is None else int(math.floor(float(top) + 0.5))


def score_label(top):
    return "score" if top is None else f"/{top} score"


def aspect_phrase(reading):
    return f"{reading['dominant_aspect']} facing" if reading.get("aspect_available") and reading.get("dominant_aspect") else EM_DASH


def plural(n):
    return "" if n == 1 else "s"


def production_block_rows(r):
    _in_source("'aspect'", "'position'", "'median slope %'", "'soil'", "'drainage'", "facing`")
    return [C(aspect_phrase(r), "aspect"), C(r.get("elevation_position"), "position"),
            M(r.get("slope_median_pct"), "median slope %"),
            *labelled_run([e["label"] for e in r.get("soil_components") or []], "soil"),
            C(r.get("drainage_class"), "drainage")]


def landform_panel(payload, committed, drawn):
    zones = {z["feature_id"]: z for z in payload["zones"]}
    top = denominator(payload.get("scales"))
    _in_source("`Block ${zone.rank}`", "`Drawn ${index + 1}`", "label: 'acres'", "label: 'score', denominator")
    cards = []
    for f in committed:
        z = zones[f["id"]]
        cards.append({"id": f["id"], "name": f"Block {z['rank']}", "rows": panel_body(
            [M(z["area_acres"], "acres"), M(z["score"], score_label(top))], production_block_rows(z))})
    for index, f in enumerate(drawn):
        p = f["properties"]
        cards.append({"id": f["id"], "name": f"Drawn {index + 1}", "rows": panel_body(
            [M(p["acres"], "acres"), M(p.get("score"), score_label(top))], production_block_rows(p))})
    return cards


def water_panel(payload, committed):
    zones = {z["feature_id"]: z for z in payload["zones"]}
    features = [f for f in payload["survey_zones"]["features"] if f["properties"]["layer"] in wire_translation.LAYER_SURVEY_ZONES]
    by_zone = {f["properties"].get("zone_id"): f for f in features}
    top = denominator(payload.get("scales"), "suitability")
    _in_source("'water delivery'", "'contributing acres at dam site'", "'contributing acres'", "'median slope %'",
               "'binding shoulder height ft'", "'max depth ft'", "'production overlap %'", "'canopy overlap %'",
               "'road overlap %'", "`shared ground w/ ${name} %`", "qualifier: 'survey'", "replace(/_/g, ' ')")

    def name(p):
        t = p.get("survey_type")
        return f"{t[:1].upper() + t[1:] if t else 'Zone'} {p.get('rank', '?')}"

    def overlap(value, label):
        return drops_at_zero(value, M(value, label))

    cards = []
    for f in committed:
        p = f["properties"]
        panel = {r["key"]: r for r in zones[f["id"]]["panel"]}
        emb = p["survey_type"] == "embankment"
        delivery = panel.get("water_delivery", {}).get("value")
        cards.append({"id": f["id"], "name": name(p), "rows": panel_body(
            [M(p["zone_acres"], "survey acres"), M(panel["suitability"]["value"], score_label(top), 0)],
            [C(delivery.replace("_", " ") if isinstance(delivery, str) else EM_DASH, "water delivery"),
             M(p.get("pinch_catchment_acres"), "contributing acres at dam site") if emb
             else M(p.get("contributing_area_acres_at_wettest_cell"), "contributing acres"),
             M(p.get("slope_median_pct"), "median slope %"),
             M(p.get("pinch_binding_height_ft"), "binding shoulder height ft") if emb
             else M(p.get("depression_depth_max_ft"), "max depth ft"),
             B(),
             overlap(p.get("production_overlap_pct"), "production overlap %"),
             overlap(p.get("canopy_overlap_pct"), "canopy overlap %"),
             overlap(p.get("road_overlap_pct"), "road overlap %"),
             *[overlap(e["fraction"] * 100, f"shared ground w/ {name(by_zone[e['zone_id']]['properties']) if e['zone_id'] in by_zone else 'an area not shown'} %")
               for e in p.get("cross_type_overlaps") or []]])})
    return cards


TERRAIN_QUALITY_KEY = "terrain_quality_score"


def roads_panel(payload, network_id):
    network = next(n for n in payload["networks"] if n["network_id"] == network_id)
    index = [n["network_id"] for n in payload["networks"]].index(network_id) + 1
    access, determination, crossings = network["access"], network["determination"], network.get("crossings") or {}
    top = denominator(network.get("scales"), TERRAIN_QUALITY_KEY)
    _in_source("'acres served'", "'length ft'", "'avg grade %'", "'max grade %'", "'crosses production block ft'",
               "'crosses canopy ft'", "'crosses wet ground ft'", "`Road Network ${index}`")
    return [{"id": network_id, "name": f"Road Network {index}", "rows": panel_body(
        [M(access["served_acres"], "acres served"), M((network.get("quality") or {}).get("terrain_quality_score"), score_label(top), 0)],
        [M(access["total_length_ft"], "length ft", 0), M(determination["avg_grade_pct"], "avg grade %"),
         M(determination["max_grade_pct"], "max grade %"), B(),
         drops_at_zero(crossings.get("crosses_block_ft"), M(crossings.get("crosses_block_ft"), "crosses production block ft", 0)),
         drops_at_zero(crossings.get("crosses_canopy_ft"), M(crossings.get("crosses_canopy_ft"), "crosses canopy ft", 0)),
         drops_at_zero(crossings.get("crosses_floodplain_ft"), M(crossings.get("crosses_floodplain_ft"), "crosses wet ground ft", 0))])}]


def tree_zone_rows(r):
    _in_source("'where in the parcel'", "`marginal benefit${plural(earned.length)}`")
    earned = r.get("marginal_benefits") or []
    return [C(r.get("position_in_parcel"), "where in the parcel"), C(r.get("elevation_position"), "position"),
            M(r.get("slope_median_pct"), "median slope %"),
            *([B(f"marginal benefit{plural(len(earned))}")] + [T(b) for b in earned] if earned else [])]


def trees_panel(payload, committed, drawn):
    zones = {z["feature_id"]: z for z in payload["zones"]}
    top = denominator(payload.get("scales"))
    _in_source("`Zone ${zone.rank}`")
    cards = []
    for f in committed:
        z = zones[f["id"]]
        cards.append({"id": f["id"], "name": f"Zone {z['rank']}", "rows": panel_body(
            [M(z["area_acres"], "acres"), M(z["score"], score_label(top))], tree_zone_rows(z))})
    for index, f in enumerate(drawn):
        p = f["properties"]
        cards.append({"id": f["id"], "name": f"Drawn {index + 1}", "rows": panel_body(
            [M(p["acres"], "acres"), M(p.get("score"), score_label(top))], tree_zone_rows(p))})
    return cards


# stepDefinitions.GATE_STATEMENTS, each sentence asserted present in the source.
_GATES = (
    (r"^outside_existing_canopy$", lambda: "sits under existing tree canopy"),
    (r"^outside_water_candidate_zone$", lambda: "sits on the committed water ground"),
    (r"^outside_tree_zone_candidate_buffer$", lambda: "sits inside a committed tree zone’s clearance"),
    (r"^within_road_proximity_buffer$", lambda: "is farther from a road than the siting rule allows"),
    (r"^outside_hydric_soil$", lambda: "sits on wet (hydric) soil, which drains badly"),
    (r"^outside_floodplain$", lambda: "sits in the mapped floodplain"),
    (r"^max_slope<=(\d+(?:\.\d+)?)pct$", lambda pct: f"averages more than {pct}% slope"),
    (r"^suitability_score>=(\d+(?:\.\d+)?)$", lambda floor: f"scores below the floor of {floor}"),
)


def gate_statement(name):
    import re
    for pattern, words in _GATES:
        match = re.match(pattern, str(name))
        if match:
            sentence = words(*match.groups())
            _in_source(sentence.split(" ")[0] + " " + sentence.split(" ")[1])
            return sentence
    _in_source("fails the rule the server calls ${name}")
    return f"fails the rule the server calls {name}"


def structures_panel(payload, committed, placed):
    top = denominator(payload["summary"].get("scales"))
    road_label = {"selected_road_corridor": "ft to road", "real_mapped_road": "ft to farm road"}.get(
        payload["summary"].get("road_proximity_source"), "ft to road")
    _in_source("'avg slope %'", "'solar rating'", "`siting rule${plural(broken.length)} broken`", "would rank ${rank",
               "selected_road_corridor: 'ft to road'", "real_mapped_road: 'ft to farm road'")

    def rows(p):
        broken = p.get("constraints_violated") or []
        return panel_body([M(p.get("distance_to_road_ft"), road_label, 0), M(p.get("suitability_score"), score_label(top))], [
            C(aspect_phrase(p), "aspect"), C(p.get("elevation_position"), "position"), M(p.get("avg_slope_pct"), "avg slope %"),
            C(p.get("solar_rating"), "solar rating"),
            *([B(f"siting rule{plural(len(broken))} broken")] + [T(gate_statement(n)) for n in broken] if broken else [])])

    cards = [{"id": f["id"], "name": f"Site {f['properties']['rank']}", "rows": rows(f["properties"])} for f in committed]
    cards += [{"id": f["id"], "name": f"Placed {i + 1} · would rank {f['properties']['rank']}", "rows": rows(f["properties"]),
               "gates_raw": f["properties"].get("constraints_violated") or []} for i, f in enumerate(placed)]
    return cards


def fencing_panel(payload, types):
    # FENCING HAS NO DATA PANEL (detail: null); its tab row is all it shows.
    _in_source("detail: null", "label: 'feet'")
    return [{"id": b["fence_type"], "name": b["label"], "rows": [M(b["total_length_ft"], "feet", 0)]}
            for b in payload["fence_types"] if b["candidate"] and b["fence_type"] in types]


def js_format(panel: dict) -> dict:
    """Every measured row's string through Node's toFixed (the panel's measure())."""
    rows = [row for cards in panel.values() if isinstance(cards, list) for card in cards for row in card["rows"]
            if row["kind"] == "measured"]
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
    panel["structures"] = structures_panel(payload, site, [placed])

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
