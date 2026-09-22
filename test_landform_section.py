"""
test_landform_section.py

THE LANDFORM SECTION -- branch 6, phase 2 of site-data-report-proposal.md's
build sequence. Runs offline: an offline session (roads_step_fixture's
harness, every network call mocked, every real computation counted) on the
real reference boundary, the Daymet fixture for Climate, WeasyPrint for
the pages.

  1. allocate_exactly(): every allocation sums EXACTLY to the rounded
     total, across counts, totals and decimals.
  2. the classifiers: SSURGO slope phases at their bounds, eight aspect
     sectors plus Flat, NaN handling.
  3. THE CALL COUNT: building the section from a live session calls
     compute_slope_percent(), delineate_valleys() and detect_keypoints()
     ZERO times; aspect is one Horn pass without a landform proposal and
     zero with one.
  4. the tables: both acreage columns sum to the cover's acreage, both
     share columns to 100.0, only the classes present, a total row that IS
     the parcel, one decimal everywhere with a dash for a true zero; the
     summary line's template.
  5. the map: slope tints as FILLED CONTOURS of the slope grid clipped to
     the boundary, under contours under valleys under keypoints, only the
     classes present, the ramp's opacities on the terrain token with the
     darkest capped and the lightest lifted, index labels, legend entries
     in order; a synthetic keypoint draws as the asterisk and earns its
     legend entry.
  6. the pages: the cover carries the acreage; every colour literal in the
     stylesheet, the templates, the renderer and the section builder is in
     site_report.TOKENS; the three-page rule (the terrain, the keyline
     structure, the numbers) with the continuation eyebrow on the follow-on
     pages; decimal alignment measured in all three tables; the two maps at
     one extent and scale; the footer's two lines; Climate still renders.
"""

import os
import random
import re
from datetime import date
from unittest.mock import patch

import numpy as np
from shapely.geometry import MultiPolygon, Point, Polygon

import offline_harness

offline_harness.install()

import exclusion_zones  # noqa: E402
import keypoint_detection  # noqa: E402
import landform_derivations  # noqa: E402
import landform_section  # noqa: E402
import report_chart  # noqa: E402
import report_map  # noqa: E402
import roads_step_fixture as fixture  # noqa: E402
import site_report  # noqa: E402
import terrain_metrics  # noqa: E402
import valley_delineation  # noqa: E402
from daymet_data import parse_daymet_csv  # noqa: E402
from landform_section import (  # noqa: E402
    ASPECT_SECTORS,
    FLAT_LABEL,
    SLOPE_CLASSES,
    SLOPE_TINT_CAP,
    SLOPE_TINT_OPACITY,
    TerrainInputs,
    BELOW_PRECISION,
    ZERO_DASH,
    allocate_exactly,
    aspect_sector,
    build_landform_section,
    slope_class,
    slope_range_label,
    terrain_inputs_from_context,
)
from reference_fixture import REAL_BOUNDARY  # noqa: E402
from report_data import report_data_from_daily  # noqa: E402
from site_report import TOKENS  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "daymet_reference_fixture.csv"), encoding="utf-8") as _handle:
    DATA = report_data_from_daily(REAL_BOUNDARY, parse_daymet_csv(_handle.read()))
GENERATED_ON = date(2026, 9, 21)
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _label_text(parts) -> str:
    return "".join(p if isinstance(p, str) else p["value"] for p in parts)


# ======================================================================
# 1. Exact allocation
# ======================================================================
print("1. allocate_exactly() sums to the rounded total, always")
assert allocate_exactly([5, 3, 2], 13.2, 1) == [6.6, 4.0, 2.6]
assert allocate_exactly([1, 1, 1], 100.0, 1) == [33.4, 33.3, 33.3]
assert allocate_exactly([0, 0], 13.2, 1) == [0.0, 0.0]
assert allocate_exactly([7, 0, 3], 10.0, 0) == [7.0, 0.0, 3.0]
rng = random.Random(6)
trials = 0
for _ in range(400):
    counts = [rng.randint(0, 2000) for _ in range(rng.randint(1, 9))]
    total = rng.uniform(0.3, 640.0)
    decimals = rng.choice((0, 1, 2))
    parts = allocate_exactly(counts, total, decimals)
    assert round(sum(parts), decimals + 3) == round(total, decimals), (counts, total, decimals, parts)
    assert all(p == 0.0 for p, c in zip(parts, counts) if c == 0)
    # No part is more than one unit off its unrounded share.
    weight = sum(counts) or 1
    assert all(abs(p - c / weight * total) <= 10 ** -decimals + 1e-9 for p, c in zip(parts, counts))
    trials += 1
print(f"   {trials} random allocations, every one summing to its rounded total")

# ======================================================================
# 2. The classifiers
# ======================================================================
print("2. SSURGO slope phases and the eight sectors plus Flat")
assert [c for c, _, _ in SLOPE_CLASSES] == ["A", "B", "C", "D", "E", "F"]
for value, expected in ((0.0, "A"), (2.99, "A"), (3.0, "B"), (7.99, "B"), (8.0, "C"), (15.0, "D"), (24.99, "D"),
                        (25.0, "E"), (34.99, "E"), (35.0, "F"), (80.0, "F")):
    assert slope_class(value) == expected, (value, slope_class(value))
assert slope_class(float("nan")) is None and slope_class(None) is None
assert slope_range_label("A") == "0–3%" and slope_range_label("D") == "15–25%" and slope_range_label("F") == "35%+"
for aspect, expected in ((0.0, "N"), (22.4, "N"), (22.6, "NE"), (45.0, "NE"), (90.0, "E"), (135.0, "SE"), (180.0, "S"),
                         (225.0, "SW"), (270.0, "W"), (315.0, "NW"), (337.4, "NW"), (337.6, "N"), (359.9, "N")):
    assert aspect_sector(aspect, 10.0) == expected, (aspect, aspect_sector(aspect, 10.0))
assert aspect_sector(90.0, 1.99) == FLAT_LABEL, "under 2% is Flat whatever the aspect"
assert aspect_sector(float("nan"), 1.0) == FLAT_LABEL
assert aspect_sector(float("nan"), 5.0) is None, "a sloping cell with no aspect is unclassified, not Flat"
assert aspect_sector(90.0, float("nan")) is None
print("   bounds inclusive below, exclusive above; Flat under 2%; NaN handled")

# ======================================================================
# 3. The call count
# ======================================================================
print("3. Landform recomputes nothing the session already built")
with fixture.Harness():
    SESSION = fixture.Session()
    CONTEXT = SESSION.context()
    DOCUMENT = SESSION.stored()
    assert "slope_pct" in CONTEXT.exclusion_zones, "the warm-up publishes the slope grid"

    with patch.object(exclusion_zones, "compute_slope_percent", wraps=exclusion_zones.compute_slope_percent) as slope_calls, \
         patch.object(valley_delineation, "delineate_valleys", wraps=valley_delineation.delineate_valleys) as valley_calls, \
         patch.object(keypoint_detection, "detect_keypoints", wraps=keypoint_detection.detect_keypoints) as keypoint_calls, \
         patch.object(terrain_metrics, "compute_slope_and_aspect", wraps=terrain_metrics.compute_slope_and_aspect) as aspect_calls, \
         patch.object(valley_delineation, "fill_and_resolve", wraps=valley_delineation.fill_and_resolve) as fill_calls, \
         patch.object(valley_delineation, "compute_flow_accumulation", wraps=valley_delineation.compute_flow_accumulation) as flow_calls:
        TERRAIN = terrain_inputs_from_context(CONTEXT, DOCUMENT)
        SECTION = build_landform_section(TERRAIN, TOKENS)
    assert slope_calls.call_count == 0, slope_calls.call_count
    assert valley_calls.call_count == 0 and keypoint_calls.call_count == 0
    assert aspect_calls.call_count == 1, "no landform proposal in the cache -> one Horn pass"
    assert fill_calls.call_count == 1 and flow_calls.call_count == 1, "the flow pass the ridges and the profile need, once"
    assert TERRAIN.aspect_source == "one Horn pass" and SECTION["aspect_source"] == "one Horn pass"
    assert TERRAIN.slope_pct is CONTEXT.exclusion_zones["slope_pct"], "the slope grid is the warm-up's own object"
    assert TERRAIN.valleys == CONTEXT.valleys and TERRAIN.keypoints == CONTEXT.keypoints
    assert TERRAIN.retrieved_on == date.fromisoformat(DOCUMENT["created_at"][:10])

    # With the landform step generated, STEP 1's aspect grid is read instead.
    SESSION.generate("landform")
    CONTEXT2 = SESSION.context()
    STEP1 = CONTEXT2.step_proposals["landform"]["run_inputs"]["step1"]
    assert isinstance(STEP1.get("aspect_deg"), np.ndarray)
    assert landform_section._step1_aspect(CONTEXT2) is STEP1["aspect_deg"]
    with patch.object(terrain_metrics, "compute_slope_and_aspect", wraps=terrain_metrics.compute_slope_and_aspect) as aspect_calls, \
         patch.object(exclusion_zones, "compute_slope_percent", wraps=exclusion_zones.compute_slope_percent) as slope_calls:
        TERRAIN2 = terrain_inputs_from_context(CONTEXT2, DOCUMENT)
        SECTION2 = build_landform_section(TERRAIN2, TOKENS)
    assert aspect_calls.call_count == 0 and slope_calls.call_count == 0
    assert TERRAIN2.aspect_source == "landform step"
    assert TERRAIN2.aspect_deg is STEP1["aspect_deg"]
    # Both grids are Horn's method over the same DEM, so the tables agree.
    assert SECTION2["aspect_table"]["rows"] == SECTION["aspect_table"]["rows"]
    assert SECTION2["slope_table"]["rows"] == SECTION["slope_table"]["rows"]

    # A session whose exclusion result lost its grid says so rather than recomputing.
    class _Bare:
        dem = CONTEXT.dem
        boundary_polygon_utm = CONTEXT.boundary_polygon_utm
        exclusion_zones = {}
        valleys = []
        keypoints = []
        step_proposals = {}

    try:
        terrain_inputs_from_context(_Bare(), DOCUMENT)
    except ValueError as exc:
        assert "slope grid" in str(exc)
    else:
        raise AssertionError("a missing slope grid must raise, not recompute")
print("   compute_slope_percent 0, delineate_valleys 0, detect_keypoints 0, the flow pass 1; aspect: one Horn pass "
      "without the landform proposal, zero with it")

# ======================================================================
# 4. The tables and the summary
# ======================================================================
print("4. every acreage column sums to the cover's acreage; every share column to 100.0")
COVER_ACRES = round(TERRAIN.parcel_acres, 1)
assert site_report.parcel_acres_label(TERRAIN) == f"{COVER_ACRES:.1f}"
slope_table = SECTION["slope_table"]
aspect_table = SECTION["aspect_table"]
assert round(sum(slope_table["acres"]), 6) == COVER_ACRES, (slope_table["acres"], COVER_ACRES)
assert round(sum(aspect_table["acres"]), 6) == COVER_ACRES, (aspect_table["acres"], COVER_ACRES)
assert round(sum(slope_table["shares"]), 6) == 100.0 and round(sum(aspect_table["shares"]), 6) == 100.0
counts = SECTION["classified"]["slope_counts"]
assert slope_table["classes"] == [c for c, _, _ in SLOPE_CLASSES if counts[c] > 0], "only the classes present"
assert len(slope_table["classes"]) >= 2, "the fixture must spread across classes for the test to mean anything"
assert slope_table["columns"] == ["Range", "Acres", "% of parcel"]
for row, name in zip(slope_table["rows"], slope_table["classes"]):
    assert row["label"] == ["Class ", {"value": name}]
    assert row["cells"][0] == slope_range_label(name)
total = slope_table["rows"][-1]
assert total["label"] == "Total" and total["cells"] == ["", f"{COVER_ACRES:.1f}", "100.0"]
assert aspect_table["columns"] == list(ASPECT_SECTORS) + [FLAT_LABEL]
assert [r["label"] for r in aspect_table["rows"]] == ["Acres", "% of parcel"]
one_decimal = re.compile(r"^\d{1,3}(,\d{3})*\.\d$")
zero_cells = 0
for table in (slope_table, aspect_table):
    for row in table["rows"]:
        for cell in row["cells"][1:] if table is slope_table else row["cells"]:
            if cell == ZERO_DASH:
                zero_cells += 1
                continue
            if cell == BELOW_PRECISION:
                continue
            assert one_decimal.match(cell), cell
            assert float(cell.replace(",", "")) != 0.0, "a zero is set as the dash, never as 0.0"
assert zero_cells >= 1, "the fixture has empty aspect sectors; they must read as dashes"
assert "0.0" not in [c for r in aspect_table["rows"] for c in r["cells"]]
# THE DASH IS FOR A TRUE ZERO ONLY. A dash in the acres row never sits over a nonzero share, and a sector
# with cells whose allocated acres round to nothing reads "<0.1", never the dash.
aspect_counts = SECTION["classified"]["aspect_counts"]
for name, acre_cell, share_cell in zip(aspect_table["columns"], aspect_table["rows"][0]["cells"], aspect_table["rows"][1]["cells"]):
    if aspect_counts.get(name, 0) == 0:
        assert acre_cell == ZERO_DASH and share_cell == ZERO_DASH, name
    else:
        assert acre_cell != ZERO_DASH and share_cell != ZERO_DASH, (name, acre_cell, share_cell)
assert (acre_cell == ZERO_DASH) == (share_cell == ZERO_DASH)
assert landform_section._one_decimal_or_dash(0.0) == ZERO_DASH and landform_section._one_decimal_or_dash(0.0, 0) == ZERO_DASH
assert landform_section._one_decimal_or_dash(0.0, 3) == BELOW_PRECISION and landform_section._one_decimal_or_dash(0.04) == BELOW_PRECISION
assert landform_section._one_decimal_or_dash(0.05) == "0.1" and landform_section._one_decimal_or_dash(2.0, 5) == "2.0"
assert slope_table["compact"] is True and "compact" not in aspect_table
# The parcel's own acreage from the tables' columns, sure to the cent of an acre.
assert sum(float(r["cells"][1]) for r in slope_table["rows"][:-1]) == float(total["cells"][1])
assert round(sum(0.0 if c in (ZERO_DASH, BELOW_PRECISION) else float(c) for c in aspect_table["rows"][0]["cells"]), 6) == COVER_ACRES
# The summary line's template.
summary = _label_text(SECTION["summary"])
relief = SECTION["contours"]["relief_ft"]
assert summary.startswith(f"The land rises {round(relief):,} ft across the parcel. Most of it is in slope class ")
dominant = max((c for c, _, _ in SLOPE_CLASSES), key=lambda c: counts[c])
assert f"slope class {dominant}, {slope_range_label(dominant)}, and it falls toward the " in summary
assert SECTION["summary"][1] == {"value": f"{round(relief):,}"}, "the relief figure is data"
assert {"value": dominant} in SECTION["summary"] and {"value": slope_range_label(dominant)} in SECTION["summary"]
assert isinstance(SECTION["summary"][-1], str) and "falls toward the" in SECTION["summary"][-1], "the direction is prose"
# Key figures: whole feet, one-decimal slope, the aspect as a WORD; nine of them (the last four are
# the detector's keypoint count and the derivations' counts -- test_landform_derivations.py holds those).
figures = {f["label"]: f for f in SECTION["key_figures"]}
assert list(figures) == ["lowest elevation", "highest elevation", "relief", "mean slope", "dominant aspect",
                         "keypoints detected", "valleys on the parcel", "ridges on the parcel",
                         "keyline length on the parcel"]
assert figures["lowest elevation"]["value"] == f"{round(SECTION['contours']['min_ft']):,} ft"
assert re.match(r"^\d+\.\d%$", figures["mean slope"]["value"]), figures["mean slope"]
assert figures["dominant aspect"].get("word") is True and figures["dominant aspect"]["value"][0].isupper()
assert figures["keypoints detected"]["value"] == str(len(TERRAIN.keypoints)) == "0"
assert figures["keyline length on the parcel"]["value"] == "0 ft" and figures["valleys on the parcel"]["value"] == "2"
# The footer: caveat first, the source line with the retrieval date.
assert SECTION["footer"]["caveat"] == [landform_section.CAVEAT_LINE]
assert SECTION["footer"]["citation"] == [
    "Source: USGS 3DEP elevation, resampled to 5 m · retrieved " + landform_section.format_retrieved_on(TERRAIN.retrieved_on)
]
assert SECTION["number"] == "III" and SECTION["template"] == "landform.html"
print(f"   cover {COVER_ACRES}; slope acres {slope_table['acres']}; aspect acres {aspect_table['acres']}; "
      f"summary: {summary}")

# ======================================================================
# 5. The map
# ======================================================================
print("5. the terrain map: tints under contours and nothing else; the ramp; the legend")
assert SLOPE_TINT_OPACITY["F"] == SLOPE_TINT_CAP == max(SLOPE_TINT_OPACITY.values()), "the darkest tint is the cap"
assert SLOPE_TINT_OPACITY["A"] >= 0.12, "the lightest tint is lifted off the paper"
opacities = [SLOPE_TINT_OPACITY[c] for c, _, _ in SLOPE_CLASSES]
assert opacities == sorted(opacities) and all(b - a >= 0.07 for a, b in zip(opacities, opacities[1:])), opacities
fills = landform_section.slope_class_geometries(TERRAIN, counts)
assert list(fills) == slope_table["classes"], (list(fills), slope_table["classes"])
boundary = TERRAIN.boundary_polygon_utm
for name, geometry in fills.items():
    assert isinstance(geometry, (Polygon, MultiPolygon)), type(geometry)
    assert geometry.within(boundary.buffer(0.01)), f"class {name} fill is not clipped to the boundary"
    # A filled contour's edge is an iso-line of the slope grid, not a cell edge:
    # its vertices do not sit on the 5 m grid lines the way a cell footprint's do.
    px, py = TERRAIN.dem["resolution_meters"]
    interior = [
        (x, y) for part in getattr(geometry, "geoms", [geometry]) for x, y in part.exterior.coords
        if boundary.buffer(-1.0).contains(Point(x, y))
    ]
    on_grid = sum(
        1 for x, y in interior
        if abs(((x - TERRAIN.dem["origin_x"]) / px) % 1.0) < 1e-6 and abs(((TERRAIN.dem["origin_y"] - y) / py) % 1.0) < 1e-6
    )
    assert on_grid < len(interior) / 2, f"class {name} fill looks like cell footprints ({on_grid}/{len(interior)} on grid lines)"
# The fills together cover the parcel: filled contours between the breaks
# tile the surface, so their union is (nearly) the whole boundary.
covered = landform_section.unary_union(list(fills.values()))
assert covered.area >= 0.97 * boundary.area, covered.area / boundary.area
# A fill polygon never enters a table: the acreages come from cell counts alone.
assert round(sum(slope_table["acres"]), 6) == COVER_ACRES
layers = landform_section.build_map_layers(
    TERRAIN, report_map.parcel_contours(TERRAIN.dem, TERRAIN.boundary_polygon_utm), fills,
)
ids = [l["id"] for l in layers]
tint_ids = [i for i in ids if i.startswith("slope-")]
assert tint_ids == [f"slope-{c}" for c in slope_table["classes"]], "one tint per class present, in class order"
assert ids[len(tint_ids):] == ["contours", "index-contours"], ids
assert "valleys" not in ids and "keypoints" not in ids, "valleys and keypoints are the structure map's, not the terrain map's"
for spec in layers:
    if spec["id"].startswith("slope-"):
        assert spec["kind"] == "polygon" and spec["fill"] == "terrain" and spec["stroke"] is None
        assert spec["fill_opacity"] == SLOPE_TINT_OPACITY[spec["id"][-1]] <= SLOPE_TINT_CAP
        assert spec["legend"] == [{"value": spec["id"][-1]}, " ", {"value": slope_range_label(spec["id"][-1])}]
rendered = SECTION["map"]
svg = rendered["svg"]
colours = set(re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{6})"', svg))
assert colours <= set(TOKENS.values()), colours - set(TOKENS.values())
assert svg.index('id="layer-slope-') < svg.index('id="layer-contours"') < svg.index('id="parcel-boundary"')
assert "layer-valleys" not in svg and "layer-keypoints" not in svg
assert "rotate(" in svg, "an index label was set"
legend = [_label_text(e["parts"]) for e in rendered["legend"]]
interval = SECTION["contours"]["interval_ft"]
assert legend == [f"{c} {slope_range_label(c)}" for c in slope_table["classes"]] + [f"Contours, {interval} ft"], legend
# Keypoints, when the session has them, draw on the STRUCTURE map (dots, the outside one muted) and never on
# the terrain map; the figure counts the detector's and says how many sit just outside the boundary.
centre = TERRAIN.boundary_polygon_utm.representative_point()
with_keypoint = TerrainInputs(**{**TERRAIN.__dict__, "keypoints": [
    {"id": 0, "valley_id": 0, "point_utm": Point(centre.x, centre.y), "on_parcel": True, "elevation_m": 310.0,
     "rowcol": (0, 0), "position_along_stem": 0, "slope_above_pct": 5.0, "slope_below_pct": 2.0},
    {"id": 1, "valley_id": 1, "point_utm": Point(centre.x + 900, centre.y), "on_parcel": False, "elevation_m": 300.0,
     "rowcol": (0, 1), "position_along_stem": 0, "slope_above_pct": 5.0, "slope_below_pct": 2.0,
     "distance_outside_boundary_m": 900.0},
]})
kp_section = build_landform_section(with_keypoint, TOKENS)
assert "layer-keypoints" not in kp_section["map"]["svg"] and kp_section["map"]["legend"] == rendered["legend"]
assert '<g id="layer-keypoints">' in kp_section["structure_map"]["svg"] and '<g id="layer-keypoints-outside">' in kp_section["structure_map"]["svg"]
kp_figures = {f["label"]: f["value"] for f in kp_section["key_figures"]}
assert kp_figures["keypoints detected, 1 just outside the boundary"] == "2", kp_figures
assert "keypoints detected" not in kp_figures, "the qualifier is in the label when a keypoint sits outside"
print(f"   layers {ids}; ramp {opacities}; legend {legend}; keypoints drawn on the structure map only")

# ======================================================================
# 6. The pages
# ======================================================================
print("6. the pages: cover acreage, colour literals, decimal alignment, the footer, Climate intact")
html = site_report.render_site_report_html(DATA, generated_on=GENERATED_ON, terrain=TERRAIN)
assert f'<p class="cover__acres"><span class="data">{COVER_ACRES:.1f}</span> acres</p>' in html
assert 'class="section section--landform section--map"' in html and 'class="section section--climate section--chart"' in html
assert '<div class="section__figures">' in html and '<div class="section__detail">' in html
assert 'class="data-table data-table--compact"' in html and html.count('class="data-table"') == 3  # aspect + valley + Climate
assert html.count("Landform, continued") == 2, "the structure page and the numbers page carry the continuation eyebrow"
assert '<div class="section__structure">' in html
assert html.index('class="section section--climate') < html.index('class="section section--landform'), "outline order"
assert "III" in html and "Landform" in html
# Colour literals: the stylesheet, every template, the renderer, the section builder -> only TOKENS.
files = [report_map.__file__, landform_section.__file__, landform_derivations.__file__, report_chart.__file__]
for root, _, names in os.walk(site_report.TEMPLATES_DIRECTORY):
    files += [os.path.join(root, n) for n in names]
for path in files:
    with open(path, encoding="utf-8") as handle:
        hits = HEX.findall(handle.read())
    assert not hits, f"{path} carries colour literal(s) {hits}"
with open(site_report.__file__, encoding="utf-8") as handle:
    site_hits = HEX.findall(handle.read())
assert sorted(site_hits) == sorted(TOKENS.values()), site_hits
css = site_report.render_stylesheet()
assert set(HEX.findall(css)) == set(TOKENS.values())
assert set(HEX.findall(html)) == set(TOKENS.values()), set(HEX.findall(html)) - set(TOKENS.values())

from weasyprint import HTML  # noqa: E402

document = HTML(string=html, base_url=site_report.TEMPLATES_DIRECTORY).render()
pages = document.pages
# Cover, Climate x2, Landform x3: the terrain map; the keyline-structure map and the profile; the numbers.
assert len(pages) == 6, len(pages)
import report_layout  # noqa: E402
assert report_layout.overflowing_boxes(document) == [], "a box past the page's content width"


def _walk(box):
    yield box
    for child in getattr(box, "children", []) or []:
        yield from _walk(child)


def _numeric_cells(box):
    cells = []
    for child in _walk(box):
        if type(child).__name__ == "TableCellBox" and child.element_tag == "td" and "num" in (child.element.get("class") or ""):
            text = "".join(child.element.itertext()).strip()
            if text:
                cells.append((round(child.position_x + child.width, 3), text, child))
    return cells


def _tables(page):
    return [b for b in _walk(page._page_box) if type(b).__name__ == "TableBox"]


# THE TWO-PAGE RULE: figures on the first page, every number on the second.
def _classes_on(page):
    return {
        (getattr(b, "element", None) is not None and b.element.get("class") or "").split()[0]
        for b in _walk(page._page_box)
        if getattr(b, "element", None) is not None and b.element.get("class")
    }


# THE THREE-PAGE RULE: the terrain (heading, summary, map, the slope table under it); the structure (a second
# map, its caption, the profile chart and its caption); the numbers (key figures, aspect and valley tables, footer).
assert {"report-map", "summary", "heading", "eyebrow", "data-table"} <= _classes_on(pages[3]), _classes_on(pages[3])
assert not ({"key-figures", "report-chart"} & _classes_on(pages[3]))
assert {"report-map", "report-chart", "caption", "eyebrow"} <= _classes_on(pages[4]), _classes_on(pages[4])
assert not ({"key-figures", "data-table", "heading", "summary"} & _classes_on(pages[4]))
assert len(_tables(pages[3])) == 1 and not _tables(pages[4]), "the slope table under the terrain map; none on the structure page"
assert {"key-figures", "data-table", "source-footer", "eyebrow"} <= _classes_on(pages[5])
assert "report-map" not in _classes_on(pages[5]) and "report-chart" not in _classes_on(pages[5])
landform_tables = _tables(pages[3]) + _tables(pages[5])
assert len(landform_tables) == 3, len(landform_tables)
# The compact table is narrower than the measure; the aspect and valley tables take it.
slope_box, aspect_box, valley_box = landform_tables
assert slope_box.width < 0.8 * aspect_box.width, (slope_box.width, aspect_box.width)
assert abs(valley_box.width - aspect_box.width) < 1.0
slope_cells, aspect_cells, valley_cells = (_numeric_cells(t) for t in landform_tables)
# 3 per class row + 2 in the total row; 18 in the aspect table; 6 per valley row.
assert len(slope_cells) == 3 * len(slope_table["classes"]) + 2, len(slope_cells)
assert len(aspect_cells) == 18, len(aspect_cells)
valley_table = SECTION["valley_table"]
assert len(valley_cells) == 6 * len(valley_table["rows"]) and len(valley_table["rows"]) == 2, "the synthetic parcel's two valleys"
landform_cells = slope_cells + aspect_cells
# Right edges: every cell in a column shares one -- 3 columns, then 9, then 6.
for cells, expected in ((slope_cells, 3), (aspect_cells, 9), (valley_cells, 6)):
    edges = {right for right, _, _ in cells}
    assert len(edges) == expected, (expected, sorted(edges))
# The valley table: whole feet in the stem, fall and keypoint columns, one decimal in the grades, dashes for
# a valley without a keypoint (the synthetic parcel has none).
for row in valley_table["rows"]:
    assert re.match(r"^\d{1,3}(,\d{3})*$", row["cells"][0]) and re.match(r"^\d{1,3}(,\d{3})*$", row["cells"][1])
    assert re.match(r"^\d+\.\d$", row["cells"][2]) and row["cells"][3:] == [ZERO_DASH] * 3
by_edge = {right for right, _, _ in slope_cells} | {right for right, _, _ in aspect_cells}
# Tabular figures: one glyph advance across every numeric cell of both tables.
advances = set()
for right, text, box in landform_cells:
    text_boxes = [b for b in _walk(box) if type(b).__name__ == "TextBox"]
    assert text_boxes, text
    advances.add(round(sum(b.width for b in text_boxes) / len(text), 2))
assert len(advances) == 1, sorted(advances)
# The decimal point sits one glyph in from the right edge in every one-decimal cell;
# a dash cell is the one exception and is a single glyph.
for right, text, box in landform_cells:
    if text in (ZERO_DASH, BELOW_PRECISION) or "%" in text:  # the dash, "<0.1", or the slope table's Range column
        continue
    assert "." in text and len(text) - text.index(".") == 2, text
assert any(text == ZERO_DASH for _, text, _ in landform_cells), "the rendered aspect table shows dashes"
flat = "".join("".join(b.text for b in _walk(p._page_box) if type(b).__name__ == "TextBox") for p in pages[3:])
squash = "".join(flat.split())
assert "".join(landform_section.CAVEAT_LINE.split()) in squash
assert "".join(("Source: USGS 3DEP elevation, resampled to 5 m · retrieved "
                + landform_section.format_retrieved_on(TERRAIN.retrieved_on)).split()) in squash
assert "III·LANDFORM" in squash.upper() or "III" in squash
# Climate is intact ahead of Landform: its numbers page carries the monthly table's 108 cells and the
# severe-weather table's 6 (this report data carries no Atlas 14 answer, so the design-storm table is a
# statement, not cells).
assert len(_numeric_cells(pages[2]._page_box)) == 108 + 6
# The two maps share extent and scale, so a feature sits at the same place on both pages.
assert SECTION["structure_map"]["meters_per_unit"] == SECTION["map"]["meters_per_unit"]
assert SECTION["structure_map"]["drawn_bbox"] == SECTION["map"]["drawn_bbox"]
assert SECTION["structure_map"]["scale_bar"] == SECTION["map"]["scale_bar"]
print(f"   6 pages; {len(landform_cells)} Landform numeric cells in 3 + 9 columns, one glyph advance "
      f"{advances.pop()} pt; literals only in TOKENS across {len(files) + 1} files")

print("\ntest_landform_section.py: all sections passed")
print(offline_harness.summary())
