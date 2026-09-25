"""
test_design_section.py

SECTION VIII, DESIGN: the layout map and the design record, rendered.

The design is design_record_fixture.json's two committed documents (the
real orchestrator, offline); the DEM and the stream are the fencing
fixture's, the same ones those documents were designed on; the
photograph is the reference parcel's real 2022 NAIP window
(naip_reference_fixture, fetched once and committed). Offline throughout.

    1. no colour literal outside site_report.TOKENS
    2. the map: its frame, its layers and their marks, the legend naming every one
    3. what this map must NOT draw: keypoints and the five exclusion zones
    4. contours: present, lighter than Landform's, no elevation labels
    5. the imagery: its year printed; the no-imagery render
    6. the pages: two, no overflow, decimal alignment, the record's words
    7. vector over raster, read back from the PDF
    8. the page decisions: the edge only over a photograph, the record's
       empty rows and shared ground, the record heading a part of VIII
"""

import copy
import json
import os
import re
from datetime import date

import offline_harness

offline_harness.install()

import pymupdf  # noqa: E402
from weasyprint import HTML  # noqa: E402

import design_record  # noqa: E402
import design_section as ds  # noqa: E402
import fencing_step_fixture as F  # noqa: E402
import naip_imagery  # noqa: E402
import naip_reference_fixture  # noqa: E402
import report_layout  # noqa: E402
from rasterio.warp import transform_geom  # noqa: E402
from shapely.geometry import shape  # noqa: E402
import report_map  # noqa: E402
import site_report  # noqa: E402
from reference_fixture import BOUNDARY_POLYGON_UTM  # noqa: E402
from site_report import TOKENS  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
OUT = os.environ.get("DESIGN_SECTION_OUT")  # a directory: write the PDFs and page PNGs there

with open(os.path.join(HERE, "design_record_fixture.json"), encoding="utf-8") as handle:
    FIXTURE = json.load(handle)
PARCEL = F._build_parcel_data()
NAIP = naip_reference_fixture.naip_block()
RETRIEVED = date(2026, 9, 24)
COVER = {"title": "Site Data Report", "eyebrow": "Keyline Designer", "label": "5614 N Montour Rd",
         "generated_on": "24 September 2026", "meta": "Generated 24 September 2026"}

print("=" * 72)
print("test_design_section.py -- VIII Design: the layout map and the design record")
print("=" * 72)


def inputs(case="full", imagery=True):
    return ds.DesignInputs(
        document=copy.deepcopy(FIXTURE[case]["document"]), dem=PARCEL.dem, boundary_polygon_utm=BOUNDARY_POLYGON_UTM,
        streams=PARCEL.water_features.get("streams", []), imagery=NAIP if imagery else None,
        imagery_unavailable=None if imagery else {"label": "aerial imagery", "reason": "source_unavailable", "error": "refused"},
        retrieved_on=RETRIEVED,
    )


def render(section, name):
    env = site_report.jinja_environment()
    html = env.get_template("base.html").render(stylesheet=site_report.render_stylesheet(env), cover=COVER, sections=[section])
    document = HTML(string=html, base_url=site_report.TEMPLATES_DIRECTORY).render()
    pdf = document.write_pdf()
    if OUT:
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, f"{name}.pdf"), "wb") as handle:
            handle.write(pdf)
        opened = pymupdf.open(stream=pdf, filetype="pdf")
        for index in range(1, len(opened)):
            opened[index].get_pixmap(dpi=150).save(os.path.join(OUT, f"{name}-page{index}.png"))
    return html, document, pdf


FULL = ds.build_design_section(inputs(), TOKENS)
MAP = FULL["map"]
SVG = MAP["svg"]
LAYERS = {spec["id"]: spec for spec in MAP["layers"]}

# ======================================================================
# 1. No colour literal outside TOKENS
# ======================================================================
print("1. no colour literal outside site_report.TOKENS")
# The stylesheet, every report template, the report's renderers (the map
# and the chart), this section's builder, the record and the imagery
# module. The matplotlib layout map was the narrated report's and is NOT
# here: it was left exactly as it was until D4 retired it.
checked = [os.path.join(HERE, "templates", "report", "report.css")]
for root, _, files in os.walk(os.path.join(HERE, "templates", "report")):
    checked += [os.path.join(root, f) for f in files if f.endswith(".html")]
checked += [os.path.join(HERE, f) for f in ("report_map.py", "report_chart.py", "design_section.py", "design_record.py",
                                             "naip_imagery.py", "report_outline.py")]
for path in checked:
    with open(path, encoding="utf-8") as handle:
        hits = HEX.findall(handle.read())
    assert not hits, (path, hits)
with open(site_report.__file__, encoding="utf-8") as handle:
    assert sorted(HEX.findall(handle.read())) == sorted(TOKENS.values()), "site_report.py: hex only inside TOKENS"
assert set(HEX.findall(SVG)) <= set(TOKENS.values()), set(HEX.findall(SVG)) - set(TOKENS.values())
print(f"   {len(checked) + 1} files checked; the rendered map's colours are all tokens")

# ======================================================================
# 2. The map
# ======================================================================
print("2. the map: its frame, each layer's treatment, its geometry, the legend naming every one")
# EXEMPT FROM THE SHARED SECTION SCALE: the layout map takes LAYOUT_FRAME
# whole, unfitted -- not FRAME, and not Landform's fitted scale.
assert MAP["frame"] == report_map.LAYOUT_FRAME and report_map.LAYOUT_FRAME[0] == report_map.FRAME_WIDTH_PT
assert report_map.LAYOUT_FRAME[1] > report_map.FRAME_HEIGHT_PT
assert MAP["scale_bar"]["feet"] in report_map.SCALE_BAR_CANDIDATES_FT
PX = 0.75  # a CSS pixel, in points: the interactive map's values carry over at WeasyPrint's px


def group(layer_id, svg=None):
    body = re.search(rf'<g id="layer-{layer_id}">(.*?)</g>(?=<g id=|<path id=|<g id="labels")', svg or SVG, re.S)
    assert body, layer_id
    return body.group(1)


# THE INTERACTIVE MAP'S ACTIVE STATE, EVERY LAYER AT ONCE: (kind, stroke, fill, width pt, stroke opacity).
expected = {
    "production": ("pattern", None, "oxide", None, None),
    "trees": ("pattern", None, "tree", None, None),
    "water-embankment": ("polygon", "survey-embankment", "survey-embankment", 2 * PX, 0.75),
    "water-excavated": ("pattern", "survey-excavated", "survey-excavated", 2 * PX, 0.75),
    "roads": ("line", "ink", None, 2 * PX, 0.75),
    "fencing": ("line", "ink", None, 1.25 * PX, 0.75),
    "structures": ("point", "ink", None, None, None),
    "structures-placed": ("point", "ochre", None, None, None),
    "access": ("point", "ochre", None, None, None),
    "contours": ("line", "terrain", None, 0.6, 1.0),
    "streams": ("line", "stream", None, 1.1, 1.0),
}
assert set(LAYERS) == set(expected), sorted(LAYERS)
for layer_id, (kind, stroke, fill, width, opacity) in expected.items():
    spec = LAYERS[layer_id]
    assert (spec["kind"], spec["stroke"], spec["fill"]) == (kind, stroke, fill), (layer_id, spec["kind"], spec["stroke"], spec["fill"])
    if width is not None:
        assert abs(spec["stroke_width"] - width) < 1e-9 and abs(spec["stroke_opacity"] - opacity) < 1e-9, layer_id
# NO CASING ON ANY FILL -- a page-coloured casing under a hatch or a wash is
# what fogged the parcel -- and none on the fence or the terrain. THE ROAD
# ALONE IS CASED: half the screen's width, as a rim at the line's level.
for layer_id, spec in LAYERS.items():
    if layer_id != "roads":
        assert spec["casing_pt"] == 0, layer_id
roads = LAYERS["roads"]
assert abs(roads["casing_pt"] - PX / 2) < 1e-9 and roads["casing_rim"] and roads["casing_opacity"] == 0.75
for layer_id in ("production", "trees", "water-embankment", "water-excavated", "fencing", "contours", "streams"):
    assert f'stroke="{TOKENS["page"]}"' not in group(layer_id), f"{layer_id} carries a page-coloured casing"
road_body = group("roads")
assert road_body.count(f'fill="{TOKENS["page"]}" fill-opacity="0.75" fill-rule="evenodd"') == 1, "the road's rim, once"
assert f'stroke="{TOKENS["page"]}"' not in road_body, "a rim, not a stroked casing under the line"
# THE TILES: the audit's values in points, userSpaceOnUse, and the screens only where the audit has them.
production_tile = re.search(r'<pattern id="pattern-production" patternUnits="userSpaceOnUse" x="0" y="0" width="6.00" '
                            r'height="6.00">(.*?)</pattern>', SVG, re.S).group(1)
assert f'fill="{TOKENS["rule"]}" fill-opacity="0.12"' in production_tile, "production's --rule screen at 0.12"
assert f'stroke="{TOKENS["oxide"]}" stroke-width="0.75" stroke-linecap="square"' in production_tile
assert production_tile.count("<path") == 1 and "M0 6.00 L6.00 0.00" in production_tile, "rising: /"
tree_tile = re.search(r'<pattern id="pattern-trees" patternUnits="userSpaceOnUse" x="0" y="0" width="6.00" '
                      r'height="6.00">(.*?)</pattern>', SVG, re.S).group(1)
assert "<rect" not in tree_tile, "trees carry no screen"
assert f'stroke="{TOKENS["tree"]}" stroke-width="0.75"' in tree_tile and "M0 0.00 L6.00 6.00" in tree_tile, "falling: \\"
excavated_tile = re.search(r'<pattern id="pattern-water-excavated" patternUnits="userSpaceOnUse" x="0" y="0" '
                           r'width="48.00" height="48.00">(.*?)</pattern>', SVG, re.S).group(1)
assert f'fill="{TOKENS["halo"]}" fill-opacity="0.16"' in excavated_tile, "excavated's --halo screen at 0.16"
dots = re.findall(r'<circle cx="([\d.]+)" cy="([\d.]+)" r="0.75" fill="' + TOKENS["survey-excavated"] + '"/>', excavated_tile)
assert len(dots) == 24 * 24 and abs(float(dots[1][0]) - float(dots[0][0]) - 2.0) < 0.011, "24 x 24, pitch 2 pt (2.67 px)"
for layer_id in ("production", "trees", "water-excavated"):
    assert f'fill="url(#pattern-{layer_id})" fill-opacity="0.75"' in group(layer_id), f"{layer_id} at --pattern-active"
assert 'stroke="none"' in group("production") and "stroke-width" not in group("production").split("</defs>")[1], \
    "the production hatch has no edge"
assert f'fill="{TOKENS["survey-embankment"]}" fill-opacity="0.22"' in group("water-embankment"), "--tint-active"
assert f'stroke="{TOKENS["survey-excavated"]}" stroke-width="1.50" stroke-linejoin="round" stroke-opacity="0.75"' in \
    group("water-excavated"), "excavated's edge, weight 2 px"
assert 'stroke-dasharray="6 3.75"' in group("fencing") and LAYERS["fencing"]["dash"] == "6 3.75", "dash 8,5 px"
assert LAYERS["contours"]["labels"] is None and LAYERS["contours"]["stroke_width"] >= 0.6, "the 0.6 pt floor"
# PINS BY PROVENANCE, the 28 px teardrop on a --halo halo; the access point an 18 px ochre disc ringed 2 px.
assert LAYERS["structures"]["marker"] == LAYERS["structures-placed"]["marker"] == "pin"
assert LAYERS["structures"]["marker_size_pt"] == 28 * PX
assert LAYERS["access"]["marker"] == "disc" and LAYERS["access"]["marker_size_pt"] == 18 * PX
assert LAYERS["access"]["marker_halo_pt"] == 2 * PX
assert f'<path d="{report_map.PIN_PATH}" fill="{TOKENS["ink"]}"' in group("structures")
assert f'<path d="{report_map.PIN_PATH}" fill="{TOKENS["ochre"]}"' in group("structures-placed")
# THE HALO CARRIES THE PIN ALONE -- the screen's drop shadow is a CSS filter
# and does not print -- so it is the screen's full 4 units, in --halo.
assert f'stroke="{TOKENS["halo"]}" stroke-width="4.00"' in group("structures") and "filter" not in SVG
# THE BOUNDARY: ink, 2 px, uncased, over a 25 pt graded edge of disjoint rings.
assert 'id="parcel-boundary-casing"' not in SVG
assert '<path id="parcel-boundary" d="' in SVG and re.search(
    rf'<path id="parcel-boundary" d="[^"]+" fill="none" stroke="{TOKENS["ink"]}" stroke-width="1.50"', SVG)
edge = re.search(r'<g id="parcel-edge">(.*?)</g>', SVG, re.S).group(1)
assert re.findall(r'fill-opacity="([\d.]+)"', edge) == ["0.18", "0.10", "0.05", "0.02"]
assert sum(width for width, _ in ds.EDGE["steps"]) == 25.0
assert SVG.index('id="off-parcel-wash"') < SVG.index('id="parcel-edge"') < SVG.index('<g id="layer-')
for group_id in ("north-arrow", "scale-bar"):
    assert f'id="{group_id}"' in SVG

# THE GEOMETRY IS THE SERVER'S DISPLAY FIELDS, AS SENT.
projection = report_map._Projection(BOUNDARY_POLYGON_UTM.bounds, report_map.LAYOUT_FRAME, report_map.MARGIN_PT,
                                    report_map.FURNITURE_BAND_PT)
DOC = FIXTURE["full"]["document"]


def drawn(geometry_wgs84):
    return report_map._geometry_path(ds._utm(geometry_wgs84, PARCEL.dem["crs"]), projection)


blocks = DOC["steps"]["landform"]["features"]["features"]
suggested = [f for f in blocks if f["properties"].get("display_only_smoothed_outline")]
drawn_blocks = [f for f in blocks if not f["properties"].get("display_only_smoothed_outline")]
assert suggested and drawn_blocks
for f in suggested:  # the display outline, never re-smoothed and never the cell union
    assert drawn(f["properties"]["display_only_smoothed_outline"]) in group("production")
    assert drawn(f["geometry"]) not in group("production")
for f in drawn_blocks:  # as stored
    assert drawn(f["geometry"]) in group("production")
for f in DOC["steps"]["trees"]["features"]["features"]:  # the raw cell union, not smoothed
    assert drawn(f["geometry"]) in group("trees")
for f in DOC["steps"]["water"]["features"]["features"]:  # the envelope as sent; member features never draw
    assert drawn(f["geometry"]) in group(f"water-{f['properties']['survey_type']}")
for f in DOC["steps"]["roads"]["features"]["features"]:  # the routed LineString as sent, every vertex: no simplify
    assert drawn(f["geometry"]) in road_body
def drawn_parts(geometry_wgs84):
    return [report_map._geometry_path(part, projection) for part in ds._linear(ds._utm(geometry_wgs84, PARCEL.dem["crs"]))]


for f in DOC["steps"]["fencing"]["features"]["features"]:  # the trimmed display line, part by part, never the ring
    assert all(d in group("fencing") for d in drawn_parts(f["properties"]["display_only_fence_line"])), f["id"]
    assert f["properties"]["display_only_fence_line"] == f["geometry"] or \
        not all(d in group("fencing") for d in drawn_parts(f["geometry"])), f["id"]
# A NULL DISPLAY LINE DRAWS NOTHING -- not the ring it trimmed away.
nulled = copy.deepcopy(DOC)
gone = nulled["steps"]["fencing"]["features"]["features"][1]
gone["properties"]["display_only_fence_line"] = None
case = inputs()
case.document = nulled
nulled_svg = ds.build_design_section(case, TOKENS)["map"]["svg"]
assert not any(d in group("fencing", nulled_svg) for d in drawn_parts(gone["geometry"]))
assert len(group("fencing", nulled_svg)) < len(group("fencing"))
# THE PAD IS NOT DRAWN; IT PLACES THE PIN at its largest piece's area-weighted centroid. A placed site is its point.
sites = DOC["steps"]["structures"]["features"]["features"]
for f in sites:
    geometry = ds._utm(f["geometry"], PARCEL.dem["crs"])
    if geometry.geom_type != "Point":
        assert report_map._geometry_path(geometry, projection) not in SVG, "a pad was drawn"
        piece = max(getattr(geometry, "geoms", [geometry]), key=lambda g: g.area)
        x, y = projection.xy(piece.centroid.x, piece.centroid.y)
    else:
        x, y = projection.xy(geometry.x, geometry.y)
    k = LAYERS["structures"]["marker_size_pt"] / report_map.PIN_VIEWBOX
    assert f'translate({x - 12 * k:.2f} {y - 22 * k:.2f})' in SVG, f["id"]

# THE LEGEND NAMES EVERY ELEMENT DRAWN, and nothing that is not; water has two entries, each its own swatch.
legend_ids = [entry["id"] for entry in MAP["legend"]]
assert set(legend_ids) == set(LAYERS) | {"boundary"}, (sorted(legend_ids), sorted(LAYERS))
assert len(legend_ids) == len(set(legend_ids))
assert legend_ids[:8] == ["production", "water-embankment", "water-excavated", "roads", "access", "trees", "structures",
                          "structures-placed"], legend_ids
swatches = {entry["id"]: entry["swatch"] for entry in MAP["legend"]}
assert f'fill="{TOKENS["survey-embankment"]}" fill-opacity="0.22"' in swatches["water-embankment"]
assert "url(#swatch-pattern-water-excavated)" in swatches["water-excavated"]
assert swatches["water-embankment"] != swatches["water-excavated"]
assert "swatch-pattern-production" in swatches["production"] and "swatch-pattern-trees" in swatches["trees"]
assert TOKENS["ochre"] in swatches["structures-placed"] and TOKENS["ink"] in swatches["structures"]
# Short labels name elements -- the record's names -- and no rationale sits on the map.
labels = [text for text, _ in MAP["label_boxes"]]
for name in ("Block 1", "Block 5", "Drawn block 1", "Embankment 1", "Excavated 1", "Zone 1", "Drawn zone 1", "Site 1",
             "Placed 1", "Access"):
    assert name in labels, (name, labels)
assert all(len(text) <= 14 for text in labels), labels
# No point label overlaps another label.
boxes = MAP["label_boxes"]
point_labels = {"Site 1", "Placed 1", "Access"}
for i, (a, box_a) in enumerate(boxes):
    for b, box_b in boxes[i + 1:]:
        if a in point_labels or b in point_labels:
            assert not report_map._overlaps(box_a, box_b), (a, b)
# Committed empty: the empty step's layers are absent, and so are their legend entries.
EMPTY = ds.build_design_section(inputs("empty_water"), TOKENS)
empty_ids = {s["id"] for s in EMPTY["map"]["layers"]} | {e["id"] for e in EMPTY["map"]["legend"]}
assert not {"water-embankment", "water-excavated"} & empty_ids
print(f"   frame {MAP['frame'][0]:.1f} x {MAP['frame'][1]:.1f} pt unfitted; {len(LAYERS)} layers at the active levels, "
      f"fills uncased, the road alone on a {roads['casing_pt']} pt rim; tiles, pins, boundary and 25 pt edge as audited; "
      f"geometry the display fields; {len(legend_ids)} legend entries, water two; {len(labels)} labels, points clear")

# ======================================================================
# 3. What this map must not draw
# ======================================================================
print("3. what this map must NOT draw: keypoints and the five exclusion zones")
# KEYPOINTS. Keypoint detection is independent of the water step: no zone,
# road or site on this map was sited relative to a keypoint, so an asterisk
# here would explain none of the design -- decoration on a page already
# carrying eight design layers over photography. Landform reports them,
# with the keyline that gives them meaning.
#
# EXCLUSION ZONES -- canopy, slope, hydric, roads, setback. They exist to
# constrain DRAWING: on the interactive map they tell the user where they
# may work, and at commit there is nothing left to draw. They are also the
# five heaviest layers in the set; five textures under eight design layers
# would make the design unreadable. The retired layout map drew them as a
# leftover from the interactive work; this map does not. It is a RENDERING
# exclusion, not a data one: the exclusions stay in the payload and the
# pipeline still needs them.
#
# Whoever wires a future layer in: these two are omitted on purpose. A
# terrain layer earns this page only by explaining a design decision.
with F.Harness():
    SESSION = F.Session()
    CONTEXT = SESSION.context()
# The fixture's DEM yields no keypoint, so three are GIVEN to the context --
# on the parcel, where an asterisk would land if anything drew one -- and
# the whole section is built from that context. Their absence then means
# something: the section was handed keypoints and drew none.
from shapely.geometry import mapping  # noqa: E402

CONTEXT = copy.copy(CONTEXT)
if not CONTEXT.keypoints:
    centre = BOUNDARY_POLYGON_UTM.representative_point()
    CONTEXT.keypoints = [
        {"id": index, "geometry_wgs84": transform_geom(PARCEL.dem["crs"], "EPSG:4326", mapping(point))}
        for index, point in enumerate((centre, BOUNDARY_POLYGON_UTM.centroid, BOUNDARY_POLYGON_UTM.buffer(-40).representative_point()))
    ]
KEYPOINTS = CONTEXT.keypoints
EXCLUSION = CONTEXT.exclusion_zones or {}
assert KEYPOINTS, "keypoints must be present for their absence to mean anything"
assert EXCLUSION.get("render_fill_polygon_utm") is not None and not EXCLUSION["render_fill_polygon_utm"].is_empty


def _geometries(value):
    if hasattr(value, "geom_type"):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _geometries(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _geometries(v)


exclusion_geometries = [g for g in _geometries(EXCLUSION) if g.geom_type in ("Polygon", "MultiPolygon") and not g.is_empty]
assert exclusion_geometries, "the fixture's exclusion result carries polygons"
# NOT IN THE INPUTS: built from a context that carries both, the section is handed neither.
class _Data:
    naip_imagery = NAIP
    unavailable = {}


wired = ds.design_inputs_from_context(CONTEXT, FIXTURE["full"]["document"], _Data())
assert not {"keypoints", "exclusion_zones", "exclusion", "valleys"} & set(vars(wired)), sorted(vars(wired))
assert wired.dem is CONTEXT.dem and wired.boundary_polygon_utm is CONTEXT.boundary_polygon_utm and wired.imagery is NAIP
SVG = ds.build_design_section(wired, TOKENS)["map"]["svg"]
# NOT IN THE GEOMETRY: the exact path each exclusion polygon and the exact asterisk each keypoint would draw, absent.
projection = report_map._Projection(BOUNDARY_POLYGON_UTM.bounds, report_map.LAYOUT_FRAME, report_map.MARGIN_PT,
                                    report_map.FURNITURE_BAND_PT)
for geometry in exclusion_geometries:
    d = report_map._geometry_path(geometry, projection)
    assert d and d not in SVG, "an exclusion polygon was drawn"
for keypoint in KEYPOINTS:
    point = shape(transform_geom("EPSG:4326", PARCEL.dem["crs"], keypoint["geometry_wgs84"]))
    x, y = projection.xy(point.x, point.y)
    asterisk = report_map._asterisk(x, y, report_map.ASTERISK_RADIUS_PT, TOKENS["ink"], 0.75)
    assert asterisk.split("/>")[0] not in SVG, "a keypoint asterisk was drawn"
assert "<line" not in re.sub(r'<g id="(north-arrow|scale-bar)">.*?</g>', "", SVG, flags=re.S), "no asterisk strokes anywhere"
# NOT IN THE LEGEND.
banned = re.compile(r"keypoint|exclu|unsuitable|canopy|slope|hydric|setback|valley|ridge", re.I)
legend_text = [" ".join(p if isinstance(p, str) else p["value"] for p in entry["parts"]) for entry in MAP["legend"]]
assert not any(banned.search(t) for t in legend_text), legend_text
assert not any(banned.search(i) for i in LAYERS), sorted(LAYERS)
print(f"   {len(KEYPOINTS)} keypoints and {len(exclusion_geometries)} exclusion polygons in the session; none in the inputs, "
      "the SVG or the legend")

# ======================================================================
# 4. Contours
# ======================================================================
print("4. contours: present, heavier than Landform's lightest, uncased, no elevation labels")
contours = LAYERS["contours"]
landform_layers = report_map.contour_layers(report_map.parcel_contours(PARCEL.dem, BOUNDARY_POLYGON_UTM))
landform_lightest = min(spec["stroke_width"] for spec in landform_layers)
assert contours["geometries"], "contours are drawn"
# NOTHING IS CASED HERE, so the contours carry themselves: heavier than the
# section maps' lightest, and 0.6 pt is the floor below which they drop out
# over the hatches (rendered, branch 16).
assert contours["stroke_width"] > landform_lightest and contours["stroke_width"] >= 0.6
assert contours["casing_pt"] == 0 and contours["stroke_opacity"] == 1.0
assert any(spec.get("labels") for spec in landform_layers), "Landform labels its index contours; the comparison is real"
assert contours["labels"] is None
body = group("contours")
assert "<text" not in body, "no elevation label in the contour group"
assert not re.search(r">\s*\d{3,4}\s*<", SVG), "no elevation figure anywhere on the map"
interval = MAP["contours"]["interval_ft"]
print(f"   {len(contours['geometries'])} lines at {interval} ft, {contours['stroke_width']} pt uncased against "
      f"Landform's lightest {landform_lightest} pt; no <text>")

# ======================================================================
# 5. The imagery
# ======================================================================
print("5. the imagery: its year printed; the no-imagery render")
underlay = MAP["underlay"]
assert underlay["coverage"] == 1.0 and underlay["href"].startswith("data:image/jpeg;base64,")
assert underlay["bytes"] < 200_000, underlay["bytes"]
dpi = underlay["pixels"][0] / (report_map.LAYOUT_FRAME[0] / 72)
assert dpi <= naip_imagery.MAX_UNDERLAY_DPI + 1e-6 and underlay["meters_per_pixel"] >= NAIP["gsd"]
assert '<image id="underlay"' in SVG and 'id="off-parcel-wash"' in SVG
assert SVG.index('<image id="underlay"') < SVG.index('id="off-parcel-wash"') < SVG.index('<g id="layer-')
line = "".join(p if isinstance(p, str) else p["value"] for p in FULL["imagery_line"])
assert "USDA NAIP" in line and "21 June 2022" in line and FULL["imagery_available"], line
assert naip_imagery.format_acquired({"acquired": ["2022-06-21", "2022-07-02"]}) == "21 June and 2 July 2022"
assert naip_imagery.format_acquired({"acquired": ["2021-09-30", "2022-06-21"]}) == "30 September 2021 and 21 June 2022"
BARE = ds.build_design_section(inputs(imagery=False), TOKENS)
assert BARE["map"]["underlay"] is None and "<image" not in BARE["map"]["svg"] and "off-parcel-wash" not in BARE["map"]["svg"]
# THE GRADED EDGE ONLY OVER A PHOTOGRAPH. The lift means something against
# imagery; on white its four bands read as a drawn soft border, so the bare
# map leaves the boundary line to carry the parcel alone.
assert 'id="parcel-edge"' in SVG and 'id="parcel-edge"' not in BARE["map"]["svg"]
assert 'id="parcel-boundary"' in BARE["map"]["svg"]
assert not BARE["imagery_available"] and "unavailable" in BARE["imagery_line"][0]
# The same design either way: every layer and the legend unchanged.
assert [s["id"] for s in BARE["map"]["layers"]] == [s["id"] for s in MAP["layers"]]
assert [e["id"] for e in BARE["map"]["legend"]] == legend_ids
print(f"   {underlay['pixels'][0]} x {underlay['pixels'][1]} px JPEG, {underlay['bytes']:,} bytes, {dpi:.0f} dpi on the page, "
      f"{underlay['meters_per_pixel']} m/px; printed: '{line[:60]}...'")

# ======================================================================
# 6. The pages
# ======================================================================
print("6. the pages: the map and the record, no overflow, decimal alignment, the record's words")


def _walk(box):
    yield box
    for child in getattr(box, "children", []) or []:
        yield from _walk(child)


def _pages_text(document, pages):
    return "".join("".join(b.text for b in _walk(document.pages[p]._page_box) if type(b).__name__ == "TextBox") for p in pages)


RENDERS = {}
for name, section in (("design", FULL), ("design-no-imagery", BARE), ("design-committed-empty", EMPTY)):
    html, document, pdf = render(section, name)
    RENDERS[name] = (html, document, pdf)
    # The cover, the map page, then the record: a card per committed
    # feature, the step's data panel, which runs to two pages on this design.
    assert len(document.pages) == 4, (name, len(document.pages))
    assert report_layout.overflowing_boxes(document) == [], (name, report_layout.overflowing_boxes(document))
    assert set(HEX.findall(html)) <= set(TOKENS.values())
    assert html.count("VIII</span> · Design") == 2
    record_text = _pages_text(document, range(2, len(document.pages)))
    squashed = "".join(record_text.split()).lower()
    assert "total" not in squashed, "no total row, no summed column"
    for step in design_record.STEP_TITLES.values():
        assert step in record_text, (name, step)

# Committed empty, rendered: the sentence stands where the table would.
empty_text = _pages_text(RENDERS["design-committed-empty"][1], range(2, 4))
assert "No water survey areas committed. The design carries no water zone." in empty_text
full_text = _pages_text(RENDERS["design"][1], range(2, 4))
_squash = lambda text: "".join(text.split())  # a label may wrap inside its card
for panel_only in ("Embankment 1", "water delivery", "binding shoulder height ft"):
    assert _squash(panel_only) not in _squash(empty_text) and _squash(panel_only) in _squash(full_text), panel_only
# Provenance in the record's words; the placed site carries no rank; the panel's own labels and headings.
assert "Suggested · rank 1" in full_text and "Drawn" in full_text and "Placed" in full_text
assert "would rank" not in full_text
assert "6 production blocks committed." in full_text and "Access point 40.64330° N, 79.98369° W, placed." in full_text
for label in ("/100 score", "median slope %", "marginal benefits", "siting rules broken", "survey acres", "max grade %"):
    assert _squash(label) in _squash(full_text), label


def _numeric_cells(table_box):
    for child in _walk(table_box):
        if type(child).__name__ == "TableCellBox" and child.element_tag == "td" and "num" in (child.element.get("class") or ""):
            text = "".join(child.element.itertext()).strip()
            glyphs = [b for b in _walk(child) if type(b).__name__ == "TextBox"]
            yield child, text, glyphs


# DECIMAL ALIGNMENT, AS THE PANEL SETS IT: in every card the figures share
# one right edge, in the data face at one advance; a figure carries the
# same decimals under the same label in every card of a step (water's
# score is whole, the panel's SUITABILITY_DP; the others' one place); and
# the cards' figure
# columns fall on three edges across the page, one per column of cards.
advances, edges, decimals_by_label = set(), {}, {}
document = RENDERS["design"][1]
cards = 0
def _step_tables(page_box):
    """(step id, table box) for every record card's table on the page."""
    for box in _walk(page_box):
        classes = (getattr(box, "element", None) is not None and box.element.get("class") or "").split()
        if "record-step" in classes and type(box).__name__ == "BlockBox":
            step = next(c[len("record-step--"):] for c in classes if c.startswith("record-step--"))
            for table in (b for b in _walk(box) if type(b).__name__ == "TableBox"):
                yield step, table


for page_index in range(2, len(document.pages)):
    for step, table in _step_tables(document.pages[page_index]._page_box):
        rights = set()
        for row in (b for b in _walk(table) if type(b).__name__ == "TableRowBox"):
            tds = [c for c in _walk(row) if type(c).__name__ == "TableCellBox"]
            if len(tds) != 2 or "num" not in (tds[0].element.get("class") or ""):
                continue
            text = "".join(tds[0].element.itertext()).strip()
            glyphs = [b for b in _walk(tds[0]) if type(b).__name__ == "TextBox"]
            rights.add(round(max(g.position_x + g.width for g in glyphs), 2))
            advances.add(round(sum(g.width for g in glyphs) / len(text), 2))
            label = "".join(tds[1].element.itertext()).strip()
            decimals_by_label.setdefault((step, label), set()).add(len(text) - text.index(".") - 1 if "." in text else 0)
        if rights:
            assert len(rights) == 1, rights       # one figure edge per card
            edges.setdefault(page_index, set()).add(rights.pop())
            cards += 1
assert cards == 16, cards
assert len(advances) == 1, advances
assert all(len(d) == 1 for d in decimals_by_label.values()), {k: v for k, v in decimals_by_label.items() if len(v) > 1}
assert all(len(e) <= 3 for e in edges.values()), edges
print(f"   3 renders x 3 pages, no overflow; {cards} cards, one advance {advances.pop()} pt, one figure edge per card, "
      f"{len(decimals_by_label)} labels each at one decimal count, three card columns; no total")

# ======================================================================
# 7. Vector over raster, from the PDF
# ======================================================================
print("7. vector over raster, read back from the PDF")
pdf = pymupdf.open(stream=RENDERS["design"][2], filetype="pdf")
page = pdf[1]
images = page.get_images(full=True)
assert len(images) == 1, images  # the photograph, once -- nothing rasterised the SVG
xref = images[0][0]
image_rect = page.get_image_rects(xref)[0]
assert (images[0][2], images[0][3]) == underlay["pixels"], images[0]
words = page.get_text("words")
on_image = [w[4] for w in words if image_rect.contains(pymupdf.Rect(w[:4]))]
for needle in ("Block", "Excavated", "Site", "Access", "250", "N"):
    assert needle in on_image, (needle, on_image)
paths = [d for d in page.get_drawings() if image_rect.intersects(d["rect"])]
assert len(paths) >= 2 * len(LAYERS), len(paths)
# DRAWN AFTER THE IMAGE, in the page's own content stream: the photograph
# is painted once, first, and every stroke, fill and glyph over it comes
# later -- vector over raster, not a raster with the vector baked in.
log = page.get_bboxlog()
image_ops = [i for i, (op, rect) in enumerate(log) if op == "fill-image"]
assert len(image_ops) == 1, image_ops
# Inside the photograph's rectangle, and not a background: the page, the
# body and the SVG's own page-colour frame fill cover the whole image and
# are painted before it by design.
inside = image_rect + (-0.5, -0.5, 0.5, 0.5)
over = [(i, op) for i, (op, rect) in enumerate(log)
        if op in ("stroke-path", "fill-path", "fill-text") and inside.contains(pymupdf.Rect(rect))
        and not pymupdf.Rect(rect).contains(image_rect + (1, 1, -1, -1))]
assert over and all(i > image_ops[0] for i, _ in over), over[:5]
assert {"stroke-path", "fill-text"} <= {op for _, op in over}
bare = pymupdf.open(stream=RENDERS["design-no-imagery"][2], filetype="pdf")
assert not bare[1].get_images(), "the no-imagery render embeds no image"
print(f"   one image object ({images[0][2]} x {images[0][3]}), painted first; {len(over)} vector operations and "
      f"{len(on_image)} words after it, over it; "
      f"PDF {len(RENDERS['design'][2]):,} bytes, {len(RENDERS['design-no-imagery'][2]):,} without imagery")

# ======================================================================
# 8. The page decisions
# ======================================================================
print("8. the page decisions: the record's empty rows and shared ground, the part heading")
record_blocks = {block["step_id"]: block for block in FULL["record"]}
cards = {card["name"]: card for card in record_blocks["landform"]["cards"]}
# A ROW EMPTY ON EVERY SUGGESTED CARD IS DROPPED where it is empty: the
# fixture's suggested blocks have no soil data, and six dash pairs read as
# a failed report. The drawn block's soil has a value and keeps its row.
raw = {card["name"]: card for step in design_record.build_design_record(FIXTURE["full"]["document"])["steps"]
       if step["step_id"] == "landform" for card in step["cards"]}
assert all(any(r["label"] == "soil" and r["value"] == design_record.EM_DASH for r in raw[f"Block {n}"]["rows"])
           for n in range(1, 6)), "the fixture really has no soil under the suggested blocks"
for name, card in cards.items():
    assert not any(row.get("value") == design_record.EM_DASH for row in card["rows"]), name
    assert not any(row.get("label") == "drainage" for row in card["rows"]), name
assert [(r["value"], r["label"]) for r in cards["Drawn 1"]["rows"] if r.get("label") == "soil"] == \
    [("68% Fixture silt loam", "soil")], "the drawn block's soil row stays"
# SHARED GROUND NAMED ONLY FOR COMMITTED PARTNERS; the rest one line, a
# count and a range -- never a sum: the partners' geometry is not in the
# document, and two of them may overlap each other.
water_cards = {card["name"]: card for card in record_blocks["water"]["cards"]}
shared = lambda name: [(r["value"], r["label"]) for r in water_cards[name]["rows"]
                       if str(r.get("label") or "").startswith(ds.SHARED_PREFIX)]
assert shared("Excavated 1") == [("6.9", "shared ground w/ Embankment 1 %"),
                                 ("3.4–30.1", "shared ground w/ 6 areas not shown, % each")], shared("Excavated 1")
assert shared("Embankment 1") == [("14.4", "shared ground w/ Excavated 1 %")], "a committed partner's row is unchanged"
assert not any("not shown %" in label and value != "3.4–30.1" for value, label in shared("Excavated 1"))
# THE RECORD HEADING IS A PART OF VIII: the only continued page with a
# heading, so set a step below the section's and above the steps' own.
sizes = {}
for index in (1, 2):
    for block in pdf[index].get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                sizes.setdefault(span["text"].strip(), round(span["size"], 2))
assert sizes["The layout"] > sizes["The design record"] > sizes["Landform"] > sizes["Block 1"], sizes
print(f"   no edge on the bare map; suggested blocks' empty soil and drainage dropped, the drawn block's soil kept; "
      f"Excavated 1's shared ground {len(shared('Excavated 1'))} rows from 8; headings "
      f"{sizes['The layout']} > {sizes['The design record']} > {sizes['Landform']} > {sizes['Block 1']} pt")

assert offline_harness.refused() == [], offline_harness.refused()
print("\ntest_design_section.py: all sections passed")
