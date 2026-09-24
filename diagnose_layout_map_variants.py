"""
diagnose_layout_map_variants.py

BRANCH 16, PHASE 1: the three open questions about section VIII's layout
map, answered on rendered pages rather than argued.

    1. roads and fencing, with and without a casing -- and where a road
       crosses tree shadow
    2. the parcel boundary, dropped and drawn in ink
    3. the graded parcel edge, built from nested buffers (no SVG filter)

Every variant is the whole Design page at its final size, over the
reference parcel's real 2022 NAIP window, built from design_record_
fixture.json's full document -- the same inputs test_design_section.py
renders. The layers are drawn in the interactive map's vocabulary (the
rendering audit's values, as branch 15 amended them): see PHASE1_LAYERS
below. design_section.py itself is not changed by this script; phase 2
moves the settled vocabulary there.

    python diagnose_layout_map_variants.py OUT_DIR

writes OUT_DIR/<variant>.png (the map page at 150 dpi) and
OUT_DIR/<variant>-shadow.png (the road through tree shadow, at 400 dpi),
plus OUT_DIR/<question>.png, the variants of each question side by side.

SCREEN PIXELS TO POINTS. The interactive map's values are CSS pixels, and
a CSS pixel is 0.75 pt -- WeasyPrint's own px. So an 8 px hatch tile is
6 pt, a 1 px rule 0.75 pt, a 2 px line 1.5 pt, the 28 px pin 21 pt.

THE CANDIDATE TOKENS below are the frontend's values (index.css) for the
marks the report has no token for yet, plus the stream blue, which is new.
They live here only until phase 2 moves them into site_report.TOKENS.
"""

import copy
import json
import os
import sys
from datetime import date

import offline_harness

offline_harness.install()

import pymupdf  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from weasyprint import HTML  # noqa: E402

import design_record  # noqa: E402
import design_section as ds  # noqa: E402
import fencing_step_fixture as F  # noqa: E402
import naip_imagery  # noqa: E402
import naip_reference_fixture  # noqa: E402
import report_map  # noqa: E402
import site_report  # noqa: E402
from reference_fixture import BOUNDARY_POLYGON_UTM  # noqa: E402
from shapely.geometry import MultiPolygon, Point, box, mapping  # noqa: E402

PX = 0.75  # points per CSS pixel

CANDIDATE_TOKENS = {
    "halo": "#ffffff",               # --halo
    "tree": "#52a466",               # --tree
    "survey-embankment": "#6da4c6",  # --survey-embankment
    "survey-excavated": "#3d5a6c",   # --survey-excavated
    "stream": "#9fd3f2",             # NEW: lighter than either survey blue
}
TOKENS = {**site_report.TOKENS, **CANDIDATE_TOKENS}

PATTERN_ACTIVE = 0.75   # --pattern-active
TINT_ACTIVE = 0.22      # --tint-active
LINE_PT = 2 * PX        # layers.jsx LINE_WEIGHT
ROAD_CASING_PT = (4 - 2) / 2 * PX       # CASING_WEIGHT 4 under LINE_WEIGHT 2: 1 px each side
FENCE_PT = 1.25 * PX
FENCE_CASING_PT = (2 - 1.25) / 2 * PX   # the fence row's casing 2 under weight 1.25
FENCE_DASH = f"{8 * PX:g} {5 * PX:g}"
CONTOUR_PT = 0.6
STREAM_PT = 1.1

PARCEL = F._build_parcel_data()
NAIP = naip_reference_fixture.naip_block()
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "design_record_fixture.json"), encoding="utf-8") as h:
    DOCUMENT = json.load(h)["full"]["document"]
COVER = {"title": "Site Data Report", "eyebrow": "Keyline Designer", "label": "5614 N Montour Rd",
         "generated_on": "24 September 2026", "meta": "Generated 24 September 2026"}


def _utm(geometry):
    return ds._utm(geometry, PARCEL.dem["crs"])


def _largest_piece_centroid(geometry):
    """The pin's point for a pad: the area-weighted centroid of its largest
    piece (geo.js largestPieceCentroid)."""
    parts = list(geometry.geoms) if isinstance(geometry, MultiPolygon) else [geometry]
    return max(parts, key=lambda g: g.area).centroid


PROJECTION = report_map._Projection(BOUNDARY_POLYGON_UTM.bounds, report_map.LAYOUT_FRAME, report_map.MARGIN_PT,
                                    report_map.FURNITURE_BAND_PT)
# The probe lines, in the frame's points: across the tree clump and its
# shadow in the parcel's east half.
PROBE_ROAD = [(262.0, 196.0), (300.0, 214.0), (345.0, 222.0), (372.0, 262.0)]
PROBE_FENCE = [(268.0, 250.0), (310.0, 250.0), (352.0, 290.0)]


def _frame_line(points):
    from shapely.geometry import LineString
    return LineString([((x - PROJECTION.offset_x) / PROJECTION.scale, (PROJECTION.offset_y - y) / PROJECTION.scale)
                       for x, y in points])


def phase1_layers(inputs, record, contours, *, cased_lines: bool):
    """The layers in the interactive map's vocabulary. `cased_lines` puts a
    --halo casing under the road and the fence (question 1); nothing else
    is cased."""
    doc = inputs.document
    names = ds.map_labels(record)
    layers = []
    lines = [level["geometry"] for level in contours["levels"]]
    layers.append(report_map.layer("contours", lines, kind="line", stroke="terrain", stroke_width=CONTOUR_PT,
                                   legend=["Contours, ", {"value": f"{contours['interval_ft']} ft"}]))
    frame = box(*ds.map_extent(inputs))
    streams = []
    for row in inputs.streams:
        if row.get("geometry"):
            streams += ds._linear(_utm(row["geometry"]).intersection(frame))
    layers.append(report_map.layer("streams", streams, kind="line", stroke="stream", stroke_width=STREAM_PT,
                                   legend="Streams"))

    trees = ds._features(doc, "trees")
    layers.append(report_map.layer(
        "trees", [_utm(f["geometry"]) for f in trees], kind="pattern", fill="tree",
        pattern={"type": "hatch", "tile_pt": 8 * PX, "weight_pt": 1 * PX, "rise": "down", "screen": None},
        pattern_opacity=PATTERN_ACTIVE, labels=[names[f["id"]] for f in trees], legend="Tree zones"))

    blocks = ds._features(doc, "landform")
    outlines = []
    for f in blocks:
        outline = f["properties"].get("display_only_smoothed_outline")
        outlines.append(_utm(outline) if outline else _utm(f["geometry"]))
    layers.append(report_map.layer(
        "production", outlines, kind="pattern", fill="oxide",
        pattern={"type": "hatch", "tile_pt": 8 * PX, "weight_pt": 1 * PX, "rise": "up",
                 "screen": {"token": "rule", "opacity": 0.12}},
        pattern_opacity=PATTERN_ACTIVE, labels=[names[f["id"]] for f in blocks], legend="Production blocks"))

    zones = ds._features(doc, "water")
    embankment = [f for f in zones if f["properties"].get("survey_type") == "embankment"]
    excavated = [f for f in zones if f["properties"].get("survey_type") == "excavated"]
    layers.append(report_map.layer(
        "water-embankment", [_utm(f["geometry"]) for f in embankment], kind="polygon", fill="survey-embankment",
        fill_opacity=TINT_ACTIVE, stroke="survey-embankment", stroke_width=LINE_PT, stroke_opacity=PATTERN_ACTIVE,
        labels=[names[f["id"]] for f in embankment], legend="Water survey area, embankment"))
    layers.append(report_map.layer(
        "water-excavated", [_utm(f["geometry"]) for f in excavated], kind="pattern", fill="survey-excavated",
        pattern={"type": "dots", "tile_pt": 64 * PX, "grid": 24, "radius_pt": 1.0 * PX,
                 "screen": {"token": "halo", "opacity": 0.16}},
        pattern_opacity=PATTERN_ACTIVE, stroke="survey-excavated", stroke_width=LINE_PT, stroke_opacity=PATTERN_ACTIVE,
        labels=[names[f["id"]] for f in excavated], legend="Water survey area, excavated"))

    fence_lines = []
    for f in ds._features(doc, "fencing"):
        line = f["properties"].get("display_only_fence_line")
        if line is not None:
            fence_lines += ds._linear(_utm(line))
    layers.append(report_map.layer("fencing", fence_lines, kind="line", stroke="ink", stroke_width=FENCE_PT,
                                   dash=FENCE_DASH, stroke_opacity=PATTERN_ACTIVE,
                                   casing_pt=FENCE_CASING_PT if cased_lines else 0.0, casing_opacity=PATTERN_ACTIVE,
                                   legend="Fencing"))

    roads = ds._features(doc, "roads")
    layers.append(report_map.layer("roads", [_utm(f["geometry"]) for f in roads], kind="line", stroke="ink",
                                   stroke_width=LINE_PT, stroke_opacity=PATTERN_ACTIVE,
                                   casing_pt=ROAD_CASING_PT if cased_lines else 0.0, casing_opacity=PATTERN_ACTIVE,
                                   legend="Road"))
    # THE PROBE: the fixture's road never runs THROUGH tree shadow -- it
    # runs beside the west tree line -- so a road and a fence drawn exactly
    # as the design's are laid across the tree clump inside the parcel's
    # east half. Not part of the design; no legend entry; the crop says so.
    layers.append(report_map.layer("probe-road", [_frame_line(PROBE_ROAD)], kind="line", stroke="ink",
                                   stroke_width=LINE_PT, stroke_opacity=PATTERN_ACTIVE,
                                   casing_pt=ROAD_CASING_PT if cased_lines else 0.0, casing_opacity=PATTERN_ACTIVE))
    layers.append(report_map.layer("probe-fence", [_frame_line(PROBE_FENCE)], kind="line", stroke="ink",
                                   stroke_width=FENCE_PT, dash=FENCE_DASH, stroke_opacity=PATTERN_ACTIVE,
                                   casing_pt=FENCE_CASING_PT if cased_lines else 0.0, casing_opacity=PATTERN_ACTIVE))
    steps = {s["step_id"]: s for s in record["steps"]}
    lon, lat = steps["roads"]["access_point"]["lon_lat"]
    layers.append(report_map.layer("access", [_utm(mapping(Point(lon, lat)))], kind="point", stroke="ochre",
                                   marker="disc", marker_size_pt=18 * PX, marker_halo_pt=2 * PX,
                                   labels=[ds.ACCESS_LABEL], label_halo=True, legend="Access point"))

    sites = ds._features(doc, "structures")
    provenance = doc["steps"]["structures"]["provenance"]

    def pin_point(f):
        geometry = _utm(f["geometry"])
        return geometry if geometry.geom_type == "Point" else _largest_piece_centroid(geometry)

    suggested = [f for f in sites if provenance.get(f["id"]) != "user_added"]
    placed = [f for f in sites if provenance.get(f["id"]) == "user_added"]
    layers.append(report_map.layer("structures", [pin_point(f) for f in suggested], kind="point", stroke="ink",
                                   marker="pin", marker_size_pt=28 * PX, labels=[names[f["id"]] for f in suggested],
                                   label_halo=True, legend="Structure site, suggested"))
    layers.append(report_map.layer("structures-placed", [pin_point(f) for f in placed], kind="point", stroke="ochre",
                                   marker="pin", marker_size_pt=28 * PX, labels=[names[f["id"]] for f in placed],
                                   label_halo=True, legend="Structure site, placed"))
    return [spec for spec in layers if spec["geometries"]]


VARIANTS = {
    # Question 1: casings, with the boundary in ink and the hard wash edge.
    "q1-uncased": dict(cased_lines=False, boundary="ink", edge=None),
    "q1-cased": dict(cased_lines=True, boundary="ink", edge=None),
    # Question 2: the boundary, uncased lines, hard edge.
    "q2-boundary-none": dict(cased_lines=False, boundary=None, edge=None),
    "q2-boundary-ink": dict(cased_lines=False, boundary="ink", edge=None),
    # Question 3: the graded edge, four steps and three, with and without the ink line.
    "q3-graded4-none": dict(cased_lines=False, boundary=None, edge="graded4"),
    "q3-graded4-ink": dict(cased_lines=False, boundary="ink", edge="graded4"),
    "q3-graded3-ink": dict(cased_lines=False, boundary="ink", edge="graded3"),
    "q3-wide-none": dict(cased_lines=False, boundary=None, edge="wide4"),
    "q3-wide-ink": dict(cased_lines=False, boundary="ink", edge="wide4"),
}

# THE EDGE STEPS, in points outward from the boundary, and each band's
# opacity of --scrim's stand-in, ink.
EDGES = {
    "graded4": {"token": "ink", "steps": [(2.0, 0.18), (3.0, 0.10), (4.0, 0.05), (6.0, 0.02)]},
    "graded3": {"token": "ink", "steps": [(2.5, 0.16), (4.0, 0.07), (6.0, 0.02)]},
    "wide4": {"token": "ink", "steps": [(3.0, 0.18), (5.0, 0.10), (7.0, 0.05), (10.0, 0.02)]},
}


def render_variant(name, *, cased_lines, boundary, edge, out_dir):
    inputs = ds.DesignInputs(document=copy.deepcopy(DOCUMENT), dem=PARCEL.dem, boundary_polygon_utm=BOUNDARY_POLYGON_UTM,
                             streams=PARCEL.water_features.get("streams", []), imagery=NAIP, imagery_unavailable=None,
                             retrieved_on=date(2026, 9, 24))
    section = ds.build_design_section(inputs, site_report.TOKENS)
    record = design_record.build_design_record(inputs.document)
    contours = report_map.parcel_contours(inputs.dem, inputs.boundary_polygon_utm)
    layers = phase1_layers(inputs, record, contours, cased_lines=cased_lines)
    extent = ds.map_extent(inputs)
    underlay = naip_imagery.underlay(NAIP, inputs.dem["crs"], extent, (extent[2] - extent[0]) / report_map.LAYOUT_FRAME[0],
                                     page_rgb=naip_imagery.page_rgb(TOKENS))
    rendered = report_map.render_map(
        BOUNDARY_POLYGON_UTM, layers, TOKENS, report_map.LAYOUT_FRAME, fit=False, underlay=underlay, wash=ds.WASH,
        halo=True, labels_on_top=True,
        boundary_style={"stroke": boundary, "width": LINE_PT, "casing": False},
        edge=EDGES.get(edge),
    )
    rendered["legend"] = report_map.legend_entries(layers, TOKENS) + (
        [report_map.legend_entries([report_map.layer("boundary", [], kind="line", stroke="ink", stroke_width=LINE_PT,
                                                     legend="Parcel boundary")], TOKENS)[0]] if boundary else [])
    rendered["layers"], rendered["underlay"] = layers, underlay
    section["map"] = rendered
    env = site_report.jinja_environment()
    stylesheet = site_report.render_stylesheet(env)
    html = env.get_template("base.html").render(stylesheet=stylesheet, cover=COVER, sections=[section])
    pdf = HTML(string=html, base_url=site_report.TEMPLATES_DIRECTORY).write_pdf()
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    page = doc[1]
    page.get_pixmap(dpi=150).save(os.path.join(out_dir, f"{name}.png"))
    # The road through tree shadow: the road's run along the west edge,
    # under the canopy line, where an uncased line has to hold on dark ground.
    image_rect = page.get_image_rects(page.get_images()[0][0])[0]
    projection = report_map._Projection(BOUNDARY_POLYGON_UTM.bounds, report_map.LAYOUT_FRAME, report_map.MARGIN_PT,
                                        report_map.FURNITURE_BAND_PT)
    crops = {}
    for label, (x0, y0, x1, y1) in SHADOW_WINDOWS.items():
        clip = pymupdf.Rect(image_rect.x0 + x0, image_rect.y0 + y0, image_rect.x0 + x1, image_rect.y0 + y1)
        path = os.path.join(out_dir, f"{name}-{label}.png")
        page.get_pixmap(dpi=400, clip=clip).save(path)
        crops[label] = path
    return os.path.join(out_dir, f"{name}.png"), crops, pdf


# Windows on the map, in the frame's points: where the road runs through
# the west edge's tree shadow, and the access corner below it.
SHADOW_WINDOWS = {
    "shadow": (60.0, 170.0, 190.0, 330.0),
    "canopy": (240.0, 170.0, 400.0, 310.0),
    "edge-se": (190.0, 300.0, 330.0, 420.0),
    "edge-ne": (240.0, 50.0, 380.0, 170.0),
    "access": (40.0, 330.0, 170.0, 450.0),
}


def side_by_side(paths, captions, out_path, width=None):
    images = [Image.open(p).convert("RGB") for p in paths]
    if width:
        images = [im.resize((width, int(im.height * width / im.width))) for im in images]
    gap, band = 24, 44
    total_w = sum(im.width for im in images) + gap * (len(images) - 1)
    height = max(im.height for im in images) + band
    sheet = Image.new("RGB", (total_w, height), "white")
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    x = 0
    for im, caption in zip(images, captions):
        sheet.paste(im, (x, band))
        draw.text((x + 6, 10), caption, fill="black", font=font)
        x += im.width + gap
    sheet.save(out_path)


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    pages, crops = {}, {}
    for name, options in VARIANTS.items():
        pages[name], crops[name], _ = render_variant(name, out_dir=out_dir, **options)
        print(f"  {name}: {pages[name]}")
    side_by_side([pages["q1-uncased"], pages["q1-cased"]], ["1a. road and fence uncased", "1b. cased in --halo"],
                 os.path.join(out_dir, "question1-pages.png"), width=1100)
    side_by_side([crops["q1-uncased"]["shadow"], crops["q1-cased"]["shadow"]],
                 ["1a. uncased: road in tree shadow", "1b. cased"], os.path.join(out_dir, "question1-shadow.png"))
    side_by_side([crops["q1-uncased"]["canopy"], crops["q1-cased"]["canopy"]],
                 ["1a. uncased: PROBE road + fence over canopy", "1b. cased"], os.path.join(out_dir, "question1-canopy.png"))
    side_by_side([crops["q1-uncased"]["access"], crops["q1-cased"]["access"]],
                 ["1a. uncased: access corner", "1b. cased"], os.path.join(out_dir, "question1-access.png"))
    side_by_side([pages["q2-boundary-none"], pages["q2-boundary-ink"]], ["2a. no boundary line", "2b. boundary in --ink"],
                 os.path.join(out_dir, "question2-pages.png"), width=1100)
    side_by_side([crops["q2-boundary-none"]["shadow"], crops["q2-boundary-ink"]["shadow"]],
                 ["2a. no boundary line", "2b. ink"], os.path.join(out_dir, "question2-edge.png"))
    side_by_side([pages["q2-boundary-ink"], pages["q3-graded4-ink"], pages["q3-wide-none"], pages["q3-wide-ink"]],
                 ["3o. hard wash edge, ink line", "3a. graded 15pt, ink line", "3d. graded 25pt, no line",
                  "3e. graded 25pt, ink line"],
                 os.path.join(out_dir, "question3-pages.png"), width=900)
    side_by_side([crops["q2-boundary-ink"]["shadow"], crops["q3-graded4-ink"]["shadow"], crops["q3-graded3-ink"]["shadow"],
                  crops["q3-graded4-none"]["shadow"]],
                 ["3o. hard edge", "3a. 4 steps + ink", "3c. 3 steps + ink", "3b. 4 steps, no line"],
                 os.path.join(out_dir, "question3-edge.png"))
    for window in ("edge-se", "edge-ne"):
        side_by_side([crops[v][window] for v in ("q2-boundary-none", "q2-boundary-ink", "q3-graded4-none", "q3-graded4-ink",
                                                 "q3-graded3-ink", "q3-wide-none", "q3-wide-ink")],
                     ["2a. hard, no line", "2b. hard, ink", "3b. 15pt, no line", "3a. 15pt, ink", "3c. 3 steps, ink",
                      "3d. 25pt, no line", "3e. 25pt, ink"],
                     os.path.join(out_dir, f"question23-{window}.png"))
    assert offline_harness.refused() == [], offline_harness.refused()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "layout_map_variants")
