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
# module. render_layout_map.py is the narrated report's and is NOT here:
# it is left exactly as it was until D4 retires it.
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
print("2. the map: its frame, its layers and their marks, the legend naming every one")
# EXEMPT FROM THE SHARED SECTION SCALE: the layout map takes LAYOUT_FRAME
# whole, unfitted -- not FRAME, and not Landform's fitted scale.
assert MAP["frame"] == report_map.LAYOUT_FRAME and report_map.LAYOUT_FRAME[0] == report_map.FRAME_WIDTH_PT
assert report_map.LAYOUT_FRAME[1] > report_map.FRAME_HEIGHT_PT
assert MAP["scale_bar"]["feet"] in report_map.SCALE_BAR_CANDIDATES_FT
# The plate system: each element's mark and token.
expected = {
    "production": ("hatch", None, "oxide"), "water": ("polygon", "water", "water"), "roads": ("line", "ink", None),
    "fencing": ("line", "ink", None), "structures": ("point", "ink", None), "trees": ("screen", None, "field"),
    "contours": ("line", "terrain", None), "streams": ("line", "water", None), "access": ("point", "ochre", None),
}
assert set(LAYERS) == set(expected), sorted(LAYERS)
for layer_id, (kind, stroke, fill) in expected.items():
    spec = LAYERS[layer_id]
    assert (spec["kind"], spec["stroke"], spec["fill"]) == (kind, stroke, fill), (layer_id, spec["kind"], spec["stroke"], spec["fill"])
    # HALO CASING ON EVERYTHING: a line, a tint, a hatch in the page colour beneath; a point marker carries its own.
    assert spec["casing_pt"] > 0 or spec["kind"] == "point", layer_id
assert LAYERS["production"]["stroke"] is None, "the production hatch has no outline"
assert LAYERS["fencing"]["dash"] and not LAYERS["roads"]["dash"], "fencing dashed, the road solid"
assert LAYERS["fencing"]["stroke_width"] < LAYERS["roads"]["stroke_width"], "fencing lighter than the road"
assert LAYERS["structures"]["marker"] == "glyph" and LAYERS["access"]["marker"] == "dot"
assert LAYERS["water"]["stroke_width"] >= 1.0, "the water tint carries a firm edge"
assert 'id="parcel-boundary-casing"' in SVG and 'id="parcel-boundary"' in SVG
for group in ("north-arrow", "scale-bar"):
    assert f'id="{group}"' in SVG
# Every layer drawn, drawn: each has marks in its group.
drawn = set()
for layer_id in LAYERS:
    body = re.search(rf'<g id="layer-{layer_id}">(.*?)</g>', SVG, re.S)
    assert body and ("<path" in body.group(1) or "<circle" in body.group(1)), layer_id
    drawn.add(layer_id)
# THE LEGEND NAMES EVERY ELEMENT DRAWN, and nothing that is not: every layer, and the boundary report_map draws itself.
legend_ids = [entry["id"] for entry in MAP["legend"]]
assert set(legend_ids) == drawn | {"boundary"}, (sorted(legend_ids), sorted(drawn))
assert len(legend_ids) == len(set(legend_ids))
assert legend_ids[:7] == ["production", "water", "roads", "access", "trees", "structures", "fencing"], legend_ids
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
# Committed empty: the empty step's layer is absent, and so is its legend entry.
EMPTY = ds.build_design_section(inputs("empty_water"), TOKENS)
assert "water" not in {s["id"] for s in EMPTY["map"]["layers"]} and "water" not in [e["id"] for e in EMPTY["map"]["legend"]]
print(f"   frame {MAP['frame'][0]:.1f} x {MAP['frame'][1]:.1f} pt unfitted (Landform's {report_map.FRAME_HEIGHT_PT:.0f} pt tall); "
      f"{len(drawn)} layers each cased and in the legend with the boundary; {len(labels)} labels, points clear")

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
# would make the design unreadable. render_layout_map.py draws them as a
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
print("4. contours: present, lighter than Landform's, no elevation labels")
contours = LAYERS["contours"]
landform_layers = report_map.contour_layers(report_map.parcel_contours(PARCEL.dem, BOUNDARY_POLYGON_UTM))
landform_lightest = min(spec["stroke_width"] for spec in landform_layers)
assert contours["geometries"], "contours are drawn"
assert contours["stroke_width"] < landform_lightest, (contours["stroke_width"], landform_lightest)
assert contours["stroke_opacity"] < 1.0 and all(spec["stroke_opacity"] >= contours["stroke_opacity"] for spec in landform_layers)
assert any(spec.get("labels") for spec in landform_layers), "Landform labels its index contours; the comparison is real"
assert contours["labels"] is None
body = re.search(r'<g id="layer-contours">(.*?)</g>', SVG, re.S).group(1)
assert "<text" not in body, "no elevation label in the contour group"
assert not re.search(r">\s*\d{3,4}\s*<", SVG), "no elevation figure anywhere on the map"
interval = MAP["contours"]["interval_ft"]
print(f"   {len(contours['geometries'])} lines at {interval} ft, {contours['stroke_width']} pt at "
      f"{contours['stroke_opacity']} opacity against Landform's lightest {landform_lightest} pt; no <text>")

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

assert offline_harness.refused() == [], offline_harness.refused()
print("\ntest_design_section.py: all sections passed")
