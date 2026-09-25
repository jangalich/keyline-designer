"""
diagnose_layout_map_variants.py

BRANCH 16: section VIII's layout map, judged rendered rather than argued.
Every render is the whole Design page at its final size, over the
reference parcel's real 2022 NAIP window, built from design_record_
fixture.json's full document by design_section.build_map_layers() itself
-- the page the report prints -- with PROBES added where the fixture's
design does not reach the ground a question is about.

PHASE 1 settled three questions on these renders (the settled values are
design_section's; its docstring records the reasoning):

    1. casings on roads and fencing -- the uncased road vanished in canopy
       shadow, the fully cased one read as a pale line with a grey core;
       the road takes a HALF-WIDTH casing (phase 2: as a rim), the fence
       none
    2. the boundary -- drawn in ink; without it the edge went soft against
       same-toned woods
    3. the graded edge -- 25 pt of disjoint rings; 15 pt read only in
       close-up. WeasyPrint ignores feGaussianBlur and feDropShadow.

PHASE 2 renders what those choices left open:

    road      the probe road across the canopy clump: uncased, the
              half and full casings stroked under the line, and the half
              casing as a RIM, which ships -- a stroked casing of any
              width left the 0.75 ink reading as a faded line
    pin       a probe pin on the clump's darkest shadow, where the halo
              carries the separation alone (the drop shadow does not print)
    stream    the stream token's candidates, each with a probe stream laid
              across the embankment survey area

    python diagnose_layout_map_variants.py OUT_DIR

THE PROBES ARE NOT THE DESIGN. They are drawn as the design's own marks,
added to the layer list here and nowhere else, with no legend entry, and
every crop that shows one says so. The fixture's road never crosses tree
shadow and its stream never meets a survey area.
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
from shapely.geometry import LineString, Point  # noqa: E402
from weasyprint import HTML  # noqa: E402

import design_section as ds  # noqa: E402
import fencing_step_fixture as F  # noqa: E402
import naip_reference_fixture  # noqa: E402
import report_map  # noqa: E402
import site_report  # noqa: E402
from reference_fixture import BOUNDARY_POLYGON_UTM  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PARCEL = F._build_parcel_data()
NAIP = naip_reference_fixture.naip_block()
with open(os.path.join(HERE, "design_record_fixture.json"), encoding="utf-8") as handle:
    DOCUMENT = json.load(handle)["full"]["document"]
COVER = {"title": "Site Data Report", "eyebrow": "Keyline Designer", "label": "5614 N Montour Rd",
         "generated_on": "24 September 2026", "meta": "Generated 24 September 2026"}
PROJECTION = report_map._Projection(BOUNDARY_POLYGON_UTM.bounds, report_map.LAYOUT_FRAME, report_map.MARGIN_PT,
                                    report_map.FURNITURE_BAND_PT)

# The probes, in the frame's points. The road and fence cross the tree
# clump in the parcel's east half and its shadow; the pin sits in that
# shadow; the stream runs through Embankment 1 and past its edge.
PROBE_ROAD = [(262.0, 196.0), (300.0, 214.0), (345.0, 222.0), (372.0, 262.0)]
PROBE_FENCE = [(268.0, 250.0), (310.0, 250.0), (352.0, 290.0)]
PROBE_PIN = (356.0, 232.0)
PROBE_STREAM = [(200.0, 60.0), (232.0, 95.0), (262.0, 112.0), (300.0, 150.0)]

# THE STREAM CANDIDATES: the survey blues' hue, their saturation, lighter
# than both. The rejected phase-1 placeholder is kept for comparison.
STREAM_CANDIDATES = {
    "A #9db7c8": "#9db7c8",   # hsl(203, 28%, 70%) -- excavated's saturation
    "B #a9c0d1": "#a9c0d1",   # hsl(205, 30%, 74%)
    "C #b4c5cf": "#b4c5cf",   # hsl(203, 22%, 76%)
    "rejected #9fd3f2": "#9fd3f2",  # hsl(202, 76%, 79%) -- the phase-1 placeholder, a cyan
}

# Windows on the frame, in points.
WINDOWS = {
    "canopy": (240.0, 170.0, 400.0, 310.0),
    "water": (180.0, 40.0, 320.0, 170.0),
    "stream": (240.0, 330.0, 420.0, 470.0),
}


def _frame_line(points):
    return LineString([_frame_point(x, y).coords[0] for x, y in points])


def _frame_point(x, y):
    return Point((x - PROJECTION.offset_x) / PROJECTION.scale, (PROJECTION.offset_y - y) / PROJECTION.scale)


def probes(road_casing_pt: float, rim: bool) -> list:
    """The probe marks, each drawn exactly as design_section draws the real
    one, without a legend entry."""
    return [
        report_map.layer("probe-stream", [_frame_line(PROBE_STREAM)], kind="line", stroke="stream",
                         stroke_width=ds.STREAM_STROKE_PT),
        report_map.layer("probe-fence", [_frame_line(PROBE_FENCE)], kind="line", stroke="ink", stroke_width=ds.FENCE_PT,
                         dash=ds.FENCE_DASH, stroke_opacity=ds.PATTERN_ACTIVE),
        report_map.layer("probe-road", [_frame_line(PROBE_ROAD)], kind="line", stroke="ink", stroke_width=ds.LINE_PT,
                         stroke_opacity=ds.PATTERN_ACTIVE, casing_pt=road_casing_pt, casing_opacity=ds.PATTERN_ACTIVE,
                         casing_rim=rim),
        report_map.layer("probe-pin", [_frame_point(*PROBE_PIN)], kind="point", stroke="ink", marker="pin",
                         marker_size_pt=ds.PIN_PT),
    ]


def render(name, out_dir, *, tokens=site_report.TOKENS, road_casing_pt=ds.ROAD_CASING_PT, rim=True):
    """The Design page with the probes; the page PNG and each window's crop."""
    inputs = ds.DesignInputs(document=copy.deepcopy(DOCUMENT), dem=PARCEL.dem, boundary_polygon_utm=BOUNDARY_POLYGON_UTM,
                             streams=PARCEL.water_features.get("streams", []), imagery=NAIP, imagery_unavailable=None,
                             retrieved_on=date(2026, 9, 24))
    build_map_layers = ds.build_map_layers
    original = ds.ROAD_CASING_PT, ds.ROAD_CASING_RIM
    try:
        ds.ROAD_CASING_PT, ds.ROAD_CASING_RIM = road_casing_pt, rim
        ds.build_map_layers = lambda *a: build_map_layers(*a) + probes(road_casing_pt, rim)
        section = ds.build_design_section(inputs, tokens)
    finally:
        ds.build_map_layers = build_map_layers
        ds.ROAD_CASING_PT, ds.ROAD_CASING_RIM = original
    env = site_report.jinja_environment()
    stylesheet = site_report.render_stylesheet(env).replace(site_report.TOKENS["stream"], tokens["stream"])
    html = env.get_template("base.html").render(stylesheet=stylesheet, cover=COVER, sections=[section])
    pdf = HTML(string=html, base_url=site_report.TEMPLATES_DIRECTORY).write_pdf()
    page = pymupdf.open(stream=pdf, filetype="pdf")[1]
    page_png = os.path.join(out_dir, f"{name}.png")
    page.get_pixmap(dpi=150).save(page_png)
    frame = page.get_image_rects(page.get_images()[0][0])[0]
    crops = {}
    for window, (x0, y0, x1, y1) in WINDOWS.items():
        crops[window] = os.path.join(out_dir, f"{name}-{window}.png")
        clip = pymupdf.Rect(frame.x0 + x0, frame.y0 + y0, frame.x0 + x1, frame.y0 + y1)
        page.get_pixmap(dpi=400, clip=clip).save(crops[window])
    return page_png, crops


def side_by_side(paths, captions, out_path):
    images = [Image.open(p).convert("RGB") for p in paths]
    gap, band = 24, 44
    sheet = Image.new("RGB", (sum(im.width for im in images) + gap * (len(images) - 1), max(im.height for im in images) + band),
                      "white")
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
    full = (4 - 2) / 2 * ds.PX
    half = (4 - 2) / 2 * ds.PX / 2
    roads = {label: render(f"road-{label}", out_dir, road_casing_pt=casing, rim=rim)
             for label, casing, rim in (("uncased", 0.0, False), ("half", half, False), ("full", full, False),
                                        ("half-rim", half, True))}
    side_by_side([roads[k][1]["canopy"] for k in ("uncased", "half", "full", "half-rim")],
                 ["PROBES. road uncased", f"half casing {half:g} pt, stroked", f"full casing {full:g} pt, stroked",
                  f"half casing {half:g} pt, rim (ships)"],
                 os.path.join(out_dir, "road-casing-canopy.png"))
    streams = {}
    for label, value in STREAM_CANDIDATES.items():
        streams[label] = render(f"stream-{label.split()[0]}", out_dir, tokens={**site_report.TOKENS, "stream": value})
    for window in ("water", "stream"):
        side_by_side([streams[k][1][window] for k in STREAM_CANDIDATES],
                     [("PROBE stream " if window == "water" else "NHD stream ") + k for k in STREAM_CANDIDATES],
                     os.path.join(out_dir, f"stream-candidates-{window}.png"))
    assert offline_harness.refused() == [], offline_harness.refused()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "layout_map_variants")
