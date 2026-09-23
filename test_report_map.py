"""
test_report_map.py

Offline checks for report_map.py -- the report's SVG map component -- and
the terrain token it introduced. Geometry is the REAL reference boundary
(reference_fixture) over a synthetic DEM; nothing here reaches the network.

  1. NO COLOUR LITERAL IN THE RENDERER: a hex grep over report_map.py
     finds nothing; a layer naming an unknown token raises; every colour in
     the emitted SVG is a value from the token table passed in.
  2. PROJECTION: the boundary's drawn bbox has the SAME aspect ratio as its
     UTM extent, the drawing fits inside the frame's margin above the
     furniture band, and a tall parcel and a wide parcel both fit.
  3. SCALE BAR: drawn length x metres-per-unit equals the feet it claims,
     the length is a round candidate, and it never exceeds 30% of the frame.
  4. CONTOURS: the interval rule at flat, moderate and steep relief; every
     fifth level is an index level; parcel_contours() clips to the boundary
     and counts lines; a flat DEM yields no lines.
  5. THE SVG: well-formed XML; one user unit is one point; every <text>
     carries font-family, font-size and fill as attributes (WeasyPrint
     applies no document CSS to SVG text); numbers in the data face, words
     in the prose face; the fixed furniture is present; the legend is empty
     with no labelled layer and lists labelled layers in order.
  6. THE MAP MACRO and the token: templates/report/components/map.html
     inlines the SVG; site_report.TOKENS carries `terrain`.
"""

import os
import re
from xml.dom import minidom

import numpy as np
from shapely.affinity import scale as affine_scale
from shapely.geometry import LineString, Point, Polygon, box

import offline_harness

offline_harness.install()

import report_map
import site_report
from reference_fixture import BOUNDARY_POLYGON_UTM, CRS
from report_map import (
    CONTOUR_INTERVALS_FT,
    FRAME,
    FURNITURE_BAND_PT,
    MARGIN_PT,
    METERS_PER_FOOT,
    SCALE_BAR_CANDIDATES_FT,
    SCALE_BAR_MAX_FRACTION,
    contour_interval_ft,
    contour_layers,
    layer,
    parcel_contours,
    render_map,
    scale_bar_feet,
)

TOKENS = site_report.TOKENS
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _synthetic_dem(boundary, slope_m_per_m: float, resolution: float = 5.0, buffer: float = 100.0) -> dict:
    """A plane rising northward at `slope_m_per_m` over the boundary's bbox
    plus a buffer -- the DEM dict shape every module here reads."""
    minx, miny, maxx, maxy = boundary.bounds
    origin_x, origin_y = minx - buffer, maxy + buffer
    cols = int(np.ceil((maxx - minx + 2 * buffer) / resolution))
    rows = int(np.ceil((maxy - miny + 2 * buffer) / resolution))
    r = np.arange(rows)[:, None].astype(np.float64)
    array = 300.0 + slope_m_per_m * resolution * (rows - r) * np.ones((1, cols))
    return {"array": array.astype(np.float32), "resolution_meters": (resolution, resolution),
            "origin_x": origin_x, "origin_y": origin_y, "crs": CRS}


# ======================================================================
# 1. No colour literal in the renderer
# ======================================================================
print("1. the renderer carries no colour literal; layers name tokens")
with open(report_map.__file__, encoding="utf-8") as handle:
    source = handle.read()
assert not HEX.findall(source), HEX.findall(source)
for word in ("white", "black", "grey", "gray", "rgb("):
    assert word not in source.replace("Names and words", ""), word

plain = render_map(BOUNDARY_POLYGON_UTM, [], TOKENS)
emitted = set(HEX.findall(plain["svg"]))
assert emitted <= set(TOKENS.values()), emitted - set(TOKENS.values())
assert TOKENS["ink"] in emitted and TOKENS["rule"] in emitted and TOKENS["page"] in emitted
try:
    render_map(BOUNDARY_POLYGON_UTM, [layer("x", [BOUNDARY_POLYGON_UTM], kind="line", stroke="brown")], TOKENS)
except KeyError as exc:
    assert "brown" in str(exc)
else:
    raise AssertionError("an unknown token name must raise, not fall through to a value")
try:
    layer("x", [], kind="blob")
except ValueError:
    pass
else:
    raise AssertionError("an unknown layer kind must raise")
print(f"   0 literals in source; SVG colours {sorted(emitted)} all from the token table")

# ======================================================================
# 2. Projection
# ======================================================================
print("2. the drawn boundary keeps its UTM aspect ratio and fits the frame")
minx, miny, maxx, maxy = plain["extent_utm"]
x0, y0, x1, y1 = plain["drawn_bbox"]
true_ratio = (maxx - minx) / (maxy - miny)
drawn_ratio = (x1 - x0) / (y1 - y0)
assert abs(true_ratio - drawn_ratio) < 1e-9, (true_ratio, drawn_ratio)
width, height = FRAME
assert x0 >= MARGIN_PT - 1e-6 and x1 <= width - MARGIN_PT + 1e-6
assert y0 >= MARGIN_PT - 1e-6 and y1 <= height - MARGIN_PT - FURNITURE_BAND_PT + 1e-6
# It fills the tighter dimension exactly.
assert abs((x1 - x0) - (width - 2 * MARGIN_PT)) < 1e-6 or abs((y1 - y0) - (height - 2 * MARGIN_PT - FURNITURE_BAND_PT)) < 1e-6
# The metres-per-unit is the inverse of the uniform scale on both axes.
assert abs((maxx - minx) / (x1 - x0) - plain["meters_per_unit"]) < 1e-9
assert abs((maxy - miny) / (y1 - y0) - plain["meters_per_unit"]) < 1e-9
# A very tall parcel and a very wide one both fit, both keep their ratio.
for sx, sy in ((1.0, 4.0), (4.0, 1.0)):
    shaped = affine_scale(BOUNDARY_POLYGON_UTM, xfact=sx, yfact=sy)
    r = render_map(shaped, [], TOKENS)
    bx0, by0, bx1, by1 = r["drawn_bbox"]
    ex = r["extent_utm"]
    assert abs((ex[2] - ex[0]) / (ex[3] - ex[1]) - (bx1 - bx0) / (by1 - by0)) < 1e-9
    assert bx1 <= width - MARGIN_PT + 1e-6 and by1 <= height - MARGIN_PT - FURNITURE_BAND_PT + 1e-6
# A geographic (degree) polygon drawn here would be ~24% off; the renderer
# takes UTM metres, and the reference boundary's UTM extent is what it draws.
assert 250 < (maxx - minx) < 400 and 300 < (maxy - miny) < 400, plain["extent_utm"]
print(f"   ratio {true_ratio:.5f} both ways; {plain['meters_per_unit']:.4f} m per unit")

# ======================================================================
# 3. Scale bar
# ======================================================================
print("3. the scale bar is a round length and measures what it claims")
bar = plain["scale_bar"]
assert bar["feet"] in SCALE_BAR_CANDIDATES_FT
implied_ft = bar["units"] * plain["meters_per_unit"] / METERS_PER_FOOT
assert abs(implied_ft - bar["feet"]) < 1e-9, (implied_ft, bar["feet"])
assert bar["units"] <= width * SCALE_BAR_MAX_FRACTION + 1e-9
assert f">{bar['feet']:,} ft<" in plain["svg"]
for mpu, expected in ((0.1, 20), (0.5, 200), (1.0, 250), (2.0, 500), (10.0, 2000), (100.0, 5000), (0.001, 20)):
    got = scale_bar_feet(mpu, width)
    assert got == expected, (mpu, got, expected)
    assert got * METERS_PER_FOOT / mpu <= width * SCALE_BAR_MAX_FRACTION + 1e-9 or got == SCALE_BAR_CANDIDATES_FT[0]
print(f"   {bar['feet']} ft drawn as {bar['units']:.2f} units -> {implied_ft:.6f} ft")

# ======================================================================
# 4. Contours
# ======================================================================
print("4. the interval rule, index levels, clipping and counts")
assert CONTOUR_INTERVALS_FT == (2, 5, 10, 20)
assert contour_interval_ft(10) == 2 and contour_interval_ft(30) == 2
assert contour_interval_ft(31) == 5 and contour_interval_ft(75) == 5
assert contour_interval_ft(76) == 10 and contour_interval_ft(150) == 10
assert contour_interval_ft(151) == 20 and contour_interval_ft(1000) == 20
assert report_map._is_index_level(1050.0, 10) and not report_map._is_index_level(1040.0, 10)
assert report_map._is_index_level(1000.0, 2) and not report_map._is_index_level(1002.0, 2)

# A 4% plane over the reference parcel: relief ~ 4% x ~330 m of northing
# ~ 13 m ~ 43 ft -> 5 ft interval -> about 8 lines.
moderate = parcel_contours(_synthetic_dem(BOUNDARY_POLYGON_UTM, 0.04), BOUNDARY_POLYGON_UTM)
assert 35 < moderate["relief_ft"] < 55, moderate["relief_ft"]
assert moderate["interval_ft"] == 5
assert 7 <= moderate["line_count"] <= 11, moderate["line_count"]
assert moderate["index_count"] == sum(1 for lv in moderate["levels"] if round(lv["elevation_ft"]) % 25 == 0)
for level in moderate["levels"]:
    assert level["geometry"].within(BOUNDARY_POLYGON_UTM.buffer(0.01)), "contours are clipped to the boundary"
# Flatter and steeper synthetic parcels.
flat = parcel_contours(_synthetic_dem(BOUNDARY_POLYGON_UTM, 0.01), BOUNDARY_POLYGON_UTM)
steep = parcel_contours(_synthetic_dem(BOUNDARY_POLYGON_UTM, 0.20), BOUNDARY_POLYGON_UTM)
assert flat["interval_ft"] == 2 and flat["line_count"] >= 4, (flat["relief_ft"], flat["line_count"])
assert steep["interval_ft"] in (10, 20) and steep["line_count"] <= 15, (steep["relief_ft"], steep["interval_ft"], steep["line_count"])
# A level DEM has no contour and says so.
level_dem = _synthetic_dem(BOUNDARY_POLYGON_UTM, 0.0)
assert parcel_contours(level_dem, BOUNDARY_POLYGON_UTM)["line_count"] == 0
# The two contour layers split intermediate from index and share one legend.
layers = contour_layers(moderate, legend="Contours")
assert [l["id"] for l in layers] == ["contours", "index-contours"]
assert len(layers[0]["geometries"]) + len(layers[1]["geometries"]) == moderate["line_count"]
assert layers[0]["legend"] == "Contours" and layers[1]["legend"] is None
assert layers[1]["stroke_width"] > layers[0]["stroke_width"]
print(
    f"   4% plane: relief {moderate['relief_ft']:.1f} ft -> {moderate['interval_ft']} ft, {moderate['line_count']} lines; "
    f"1%: {flat['relief_ft']:.1f} ft -> {flat['interval_ft']} ft, {flat['line_count']} lines; "
    f"20%: {steep['relief_ft']:.1f} ft -> {steep['interval_ft']} ft, {steep['line_count']} lines"
)

# ======================================================================
# 5. The SVG
# ======================================================================
print("5. the SVG: well-formed, point-for-unit, fonts as attributes, furniture present")
full = render_map(
    BOUNDARY_POLYGON_UTM,
    contour_layers(moderate, legend="Contours, 5 ft")
    + [layer("valleys", [LineString([(minx + 50, miny + 50), (maxx - 50, maxy - 50)])], kind="line", stroke="ink", stroke_width=0.6, legend="Valleys"),
       layer("keypoints", [Point((minx + maxx) / 2, (miny + maxy) / 2)], kind="point", stroke="ink", stroke_width=0.8, legend="Keypoints")],
    TOKENS,
)
svg = full["svg"]
document = minidom.parseString(svg)
root = document.documentElement
# THE FRAME IS FITTED TO THE PARCEL (fitted_frame): the tall reference boundary in the wide frame would leave
# 126 m of empty ground either side, so the width is cut to MAX_CONTEXT_MARGIN_M beyond the bbox; the height, and
# so the scale, is the full frame's. A wide parcel is width-limited and keeps the full frame.
from report_map import MAX_CONTEXT_MARGIN_M, fitted_frame, render_map as _render, visible_extent_utm  # noqa: E402
fitted_w, fitted_h = full["frame"]
assert fitted_h == height and fitted_w < width and fitted_frame(BOUNDARY_POLYGON_UTM) == (fitted_w, fitted_h)
assert root.getAttribute("width") == f"{fitted_w:.2f}pt" and root.getAttribute("viewBox") == f"0 0 {fitted_w:.2f} {fitted_h:.2f}"
assert full["meters_per_unit"] == plain["meters_per_unit"], "the cut changes no scale"
fx0, _, fx1, _ = full["drawn_bbox"]
assert abs((fx0 - MARGIN_PT) * full["meters_per_unit"] - MAX_CONTEXT_MARGIN_M) < 1e-6
assert abs((fitted_w - MARGIN_PT - fx1) * full["meters_per_unit"] - MAX_CONTEXT_MARGIN_M) < 1e-6
vx0, vy0, vx1, vy1 = visible_extent_utm(BOUNDARY_POLYGON_UTM)
assert abs((minx - vx0) - MAX_CONTEXT_MARGIN_M - MARGIN_PT * full["meters_per_unit"]) < 1e-6 and vx1 > maxx and vy1 > maxy > miny > vy0
wide = affine_scale(BOUNDARY_POLYGON_UTM, xfact=4.0, yfact=1.0)
assert fitted_frame(wide) == FRAME, "a width-limited parcel keeps the whole frame"
# A NOTE ON THE MAP: prose face, muted ink, centred on the point, between the layers and the boundary.
noted = _render(BOUNDARY_POLYGON_UTM, [], TOKENS, note={"lines": ["Nothing mapped", "on the parcel."], "point": BOUNDARY_POLYGON_UTM.representative_point()})
assert '<g id="map-note">' in noted["svg"] and noted["svg"].index("map-note") < noted["svg"].index("parcel-boundary")
note_texts = [t for t in minidom.parseString(noted["svg"]).getElementsByTagName("text") if t.firstChild.data in ("Nothing mapped", "on the parcel.")]
assert len(note_texts) == 2 and all(t.getAttribute("fill") == TOKENS["ink-muted"] and t.getAttribute("font-family") == "Source Serif 4" for t in note_texts)
assert "map-note" not in plain["svg"]
texts = root.getElementsByTagName("text")
assert texts, "the map has no text"
for text in texts:
    for attr in ("font-family", "font-size", "fill"):
        assert text.getAttribute(attr), f"<text> lacks {attr}: {text.toxml()}"
by_content = {t.firstChild.data: t.getAttribute("font-family") for t in texts}
assert by_content["N"] == "Source Serif 4"
assert by_content[f"{full['scale_bar']['feet']:,} ft"] == "IBM Plex Mono", by_content
# The legend is NOT in the SVG: no legend text, no legend group. It comes
# back as entries -- swatch plus label parts -- for the macro to set below
# the frame, in layer order; the empty case is an empty list.
assert "Contours, 5 ft" not in by_content and "Valleys" not in by_content
ids = {g.getAttribute("id") for g in root.getElementsByTagName("g")} | {p.getAttribute("id") for p in root.getElementsByTagName("path")}
for required in ("parcel-boundary", "north-arrow", "scale-bar", "layer-contours", "layer-index-contours", "layer-valleys", "layer-keypoints"):
    assert required in ids, required
assert "legend" not in ids
legend_labels = ["".join(p if isinstance(p, str) else p["value"] for p in e["parts"]) for e in full["legend"]]
assert legend_labels == ["Contours, 5 ft", "Valleys", "Keypoints"], legend_labels
assert [e["id"] for e in full["legend"]] == ["contours", "valleys", "keypoints"]
for entry in full["legend"]:
    minidom.parseString(entry["swatch"])
    assert 'class="report-map__swatch"' in entry["swatch"]
assert plain["legend"] == []
# A legend given as parts keeps them: the macro sets the mapping as data.
parts_layer = layer("x", [], kind="line", stroke="ink", legend=["Contours, ", {"value": "5 ft"}])
assert report_map.legend_entries([parts_layer], TOKENS)[0]["parts"] == ["Contours, ", {"value": "5 ft"}]
# INDEX LABELS: the index layer carries one whole-feet label per level,
# the label is set in the data face along the line, and the line is
# BROKEN behind it -- the labelled level draws as more subpaths than the
# same level unlabelled.
index_layer = contour_layers(moderate)[1]
assert index_layer["labels"] == [f"{round(lv['elevation_ft']):,}" for lv in moderate["levels"] if lv["index"]]
labels = [t for t in texts if t.getAttribute("font-family") == "IBM Plex Mono" and t.getAttribute("transform")]
assert labels, "no index label was set"
assert all(t.getAttribute("transform").startswith("rotate(") for t in labels)
assert all(t.getAttribute("fill") == TOKENS["terrain"] for t in labels)
assert {t.firstChild.data for t in labels} <= set(index_layer["labels"]), [t.firstChild.data for t in labels]
unlabelled = dict(index_layer, labels=None)
with_label = render_map(BOUNDARY_POLYGON_UTM, [index_layer], TOKENS)["svg"]
without_label = render_map(BOUNDARY_POLYGON_UTM, [unlabelled], TOKENS)["svg"]
assert with_label.count("M") > without_label.count("M"), "the labelled line was not broken"
assert "rotate(" not in without_label
# The angle keeps the label upright: within (-90, 90].
for t in labels:
    angle = float(t.getAttribute("transform")[len("rotate("):].split()[0])
    assert -90 < angle <= 90, angle
# A label on a tint, or a count that does not match, is refused. A label on a POLYGON is not:
# branch 12 made it a capability (the soil map's symbols; Site overview may want it too).
for bad in (dict(kind="screen", labels=["x"], fill="stock"), dict(kind="line", labels=["a", "b"])):
    try:
        layer("bad", [Point(0, 0)], stroke="ink", **bad)
    except ValueError:
        pass
    else:
        raise AssertionError(bad)
layer("fine", [Polygon([(0, 0), (1, 0), (1, 1)])], kind="polygon", stroke="ink", labels=["x"])

# --- POLYGON LABELS (branch 12): the pole of inaccessibility, and the drop rule --------------
# The pole is inside the polygon and further from every edge than the centroid is -- on an L the
# centroid falls OUTSIDE the shape entirely, which is the case a centroid-placed label gets wrong.
ell = Polygon([(0, 0), (100, 0), (100, 20), (20, 20), (20, 100), (0, 100)])
px, py, radius = report_map.polygon_pole(ell)
assert not ell.contains(ell.centroid), "the L's centroid is outside it, which is why the pole is used"
assert ell.contains(Point(px, py)) and abs(radius - 10.0) < 1e-6, (px, py, radius)
assert abs(ell.boundary.distance(Point(px, py)) - radius) < 1e-6, "the radius is the clearance it claims"
# A hole is an edge: the pole of a donut sits in the ring, not in the hole.
donut = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)], [[(20, 20), (80, 20), (80, 80), (20, 80)]])
dx, dy, dradius = report_map.polygon_pole(donut)
assert donut.contains(Point(dx, dy)) and abs(dradius - 10.0) < 1e-6, (dx, dy, dradius)
# A MultiPolygon's symbol goes in the largest part.
scattered = box(0, 0, 4, 4).union(box(200, 200, 260, 260))
mx, my, _ = report_map.polygon_pole(scattered)
assert 200 <= mx <= 260 and 200 <= my <= 260, (mx, my)
assert report_map.polygon_pole(Polygon()) is None
# The drop rule: a polygon that cannot hold its box is left unlabelled, and render_map says so.
big = box(minx + 40, miny + 40, minx + 160, miny + 160)
sliver = box(maxx - 60, maxy - 66, maxx - 54, maxy - 60)
units = layer("units", [big, sliver], kind="polygon", stroke="ink", fill="stock", labels=["GvD", "At"])
rendered = render_map(BOUNDARY_POLYGON_UTM, [units], TOKENS)
assert rendered["labels_placed"]["units"] == [True, False], rendered["labels_placed"]
# Twice: the knock-out stroke under the glyphs, then the glyphs.
assert rendered["svg"].count(">GvD</text>") == 2 and ">At</text>" not in rendered["svg"]
assert f'fill="none" stroke="{TOKENS["page"]}" stroke-width="{report_map.LINE_LABEL_SIZE_PT * report_map.HALO_WIDTH_EM:.2f}"' \
    in rendered["svg"], "the polygon label is knocked out of what it sits on"
# The placed label sits at the pole, in the data face, and label_placements agrees with the render.
bx, by, _ = report_map.polygon_pole(big)
projection = report_map._Projection(
    BOUNDARY_POLYGON_UTM.bounds, report_map.fitted_frame(BOUNDARY_POLYGON_UTM, report_map.FRAME),
    report_map.MARGIN_PT, report_map.FURNITURE_BAND_PT)
ex, ey = projection.xy(bx, by)
placed_text = re.search(r'<text x="([\d.-]+)" y="([\d.-]+)"[^>]*font-family="IBM Plex Mono"[^>]*>GvD</text>', rendered["svg"])
assert placed_text, rendered["svg"][:400]
assert abs(float(placed_text.group(1)) - ex) < 0.01 and abs(float(placed_text.group(2)) - ey) < report_map.LINE_LABEL_SIZE_PT
assert report_map.label_placements(BOUNDARY_POLYGON_UTM, units) == [True, False]
# The label's box fits inside the circle the pole claims, and the sliver's does not.
mpu = rendered["meters_per_unit"]
assert report_map.polygon_pole(big)[2] / mpu >= report_map.polygon_label_radius_pt("GvD")
assert report_map.polygon_pole(sliver)[2] / mpu < report_map.polygon_label_radius_pt("At")
# An unlabelled polygon layer renders exactly as it did before labels existed: the ONE placed label costs
# two elements, the knock-out and the glyphs.
assert render_map(BOUNDARY_POLYGON_UTM, [dict(units, labels=None)], TOKENS)["svg"].count("<text") \
    == rendered["svg"].count("<text") - 2
print(f"   polygon labels: pole at {radius:.0f} m clearance on the L, {report_map.polygon_label_radius_pt('GvD'):.1f} pt "
      f"needed for GvD; 1 of 2 placed, the sliver dropped")
# The keypoint is the asterisk convention: three strokes through one point.
keypoint_group = [g for g in root.getElementsByTagName("g") if g.getAttribute("id") == "layer-keypoints"][0]
assert len(keypoint_group.getElementsByTagName("line")) == 3
# The dot marker: one filled circle in the stroke token, on the map and in the swatch; a set-back line
# carries stroke-opacity; an unknown marker is refused.
dotted = render_map(
    BOUNDARY_POLYGON_UTM,
    [layer("dots", [Point((minx + maxx) / 2, (miny + maxy) / 2)], kind="point", stroke="ink-muted", marker="dot", legend="Dots"),
     layer("faint", [LineString([(minx + 50, miny + 50), (maxx - 50, maxy - 50)])], kind="line", stroke="terrain", stroke_opacity=0.55)],
    TOKENS,
)
dot_group = [g for g in minidom.parseString(dotted["svg"]).documentElement.getElementsByTagName("g") if g.getAttribute("id") == "layer-dots"][0]
circles = dot_group.getElementsByTagName("circle")
# A halo in the page colour under the dot, so it reads as a point on a line rather than a thickening of it.
assert len(circles) == 2 and not dot_group.getElementsByTagName("line")
assert circles[0].getAttribute("fill") == TOKENS["page"] and float(circles[0].getAttribute("r")) == report_map.DOT_RADIUS_PT + report_map.DOT_HALO_PT
assert circles[1].getAttribute("fill") == TOKENS["ink-muted"] and float(circles[1].getAttribute("r")) == report_map.DOT_RADIUS_PT
assert "<circle" in dotted["legend"][0]["swatch"] and "<line" not in dotted["legend"][0]["swatch"]
# A point layer's labels are set beside the marker, in the data face, in the layer's own token; None sets nothing.
labelled_points = render_map(
    BOUNDARY_POLYGON_UTM,
    [layer("pts", [Point(minx + 100, miny + 100), Point(maxx - 100, maxy - 100)], kind="point", stroke="ink", marker="dot", labels=["1,173", None])],
    TOKENS,
)
pts_texts = [t for t in minidom.parseString(labelled_points["svg"]).documentElement.getElementsByTagName("text")
             if t.parentNode.getAttribute("id") == "layer-pts"]
assert [t.firstChild.data for t in pts_texts] == ["1,173"] and pts_texts[0].getAttribute("font-family") == "IBM Plex Mono"
assert pts_texts[0].getAttribute("fill") == TOKENS["ink"] and pts_texts[0].getAttribute("text-anchor") == "start"
assert labelled_points["labels_placed"] == {}, "point labels always fit; only line layers report placements"
# A labelled line layer reports which labels were set, by the same rule the renderer applies.
assert full["labels_placed"] == {"index-contours": report_map.label_placements(BOUNDARY_POLYGON_UTM, index_layer)}
assert any(full["labels_placed"]["index-contours"]) and len(full["labels_placed"]["index-contours"]) == len(index_layer["labels"])
short = layer("short", [LineString([(minx + 10, miny + 10), (minx + 12, miny + 12)])], kind="line", stroke="ink", labels=["1,000"])
assert report_map.label_placements(BOUNDARY_POLYGON_UTM, short) == [False], "a part too short to carry its label drops it"
faint_group = dotted["svg"].split('<g id="layer-faint">', 1)[1].split("</g>", 1)[0]
assert 'stroke-opacity="0.55"' in faint_group and "stroke-opacity" not in svg, "opacity only when set back"
try:
    layer("bad", [Point(0, 0)], kind="point", stroke="ink", marker="star")
except ValueError:
    pass
else:
    raise AssertionError("an unknown marker must be refused")
# The boundary is in ink at the boundary weight; contours in terrain.
boundary_path = [p for p in root.getElementsByTagName("path") if p.getAttribute("id") == "parcel-boundary"][0]
assert boundary_path.getAttribute("stroke") == TOKENS["ink"]
contour_group = [g for g in root.getElementsByTagName("g") if g.getAttribute("id") == "layer-contours"][0]
assert all(p.getAttribute("stroke") == TOKENS["terrain"] for p in contour_group.getElementsByTagName("path"))
print(f"   {len(texts)} text elements, all attributed; {len(labels)} index label(s) {[t.firstChild.data for t in labels]}; legend {legend_labels}")

# ======================================================================
# 6. The macro and the token
# ======================================================================
print("6. the map macro inlines the SVG; the terrain token exists")
assert TOKENS["terrain"] == "#7a5c3a"
macro_path = os.path.join(site_report.TEMPLATES_DIRECTORY, "components", "map.html")
with open(macro_path, encoding="utf-8") as handle:
    macro = handle.read()
assert "map.svg | safe" in macro and 'class="report-map"' in macro
assert not HEX.findall(macro)
env = site_report.jinja_environment()
rendered = env.from_string('{% import "components/map.html" as m %}{{ m.map(map) }}').render(map=full)
assert rendered.startswith('<figure class="report-map">') and rendered.rstrip().endswith("</figure>")
assert '<div class="report-map__frame"><svg' in rendered
assert rendered.count('<li class="report-map__entry">') == 3
assert '<span class="data">5 ft</span>' not in rendered  # this legend was given as strings
parts_map = dict(full, legend=report_map.legend_entries([parts_layer], TOKENS))
parts_rendered = env.from_string('{% import "components/map.html" as m %}{{ m.map(map) }}').render(map=parts_map)
assert 'Contours, <span class="data">5 ft</span>' in parts_rendered
css = site_report.render_stylesheet()
assert ".report-map__frame svg" in css and ".report-map__legend" in css and "--terrain: #7a5c3a;" in css
print("   macro renders the SVG inline with the legend strip below; stylesheet declares --terrain")

print("\ntest_report_map.py: all sections passed")
print(offline_harness.summary())
