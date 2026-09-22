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
     the parcel, one decimal everywhere; the summary line's template.
  5. the map: slope tints under contours under valleys under keypoints,
     only the classes present, the ramp's opacities on the terrain token
     with the darkest capped, index labels, legend entries in order; a
     synthetic keypoint draws as the asterisk and earns its legend entry.
  6. the pages: the cover carries the acreage; every colour literal in the
     stylesheet, the templates, the renderer and the section builder is in
     site_report.TOKENS; decimal alignment measured in both tables; the
     footer's two lines; Climate still renders.
"""

import os
import random
import re
from datetime import date
from unittest.mock import patch

import numpy as np
from shapely.geometry import Point

import offline_harness

offline_harness.install()

import exclusion_zones  # noqa: E402
import keypoint_detection  # noqa: E402
import landform_section  # noqa: E402
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
         patch.object(terrain_metrics, "compute_slope_and_aspect", wraps=terrain_metrics.compute_slope_and_aspect) as aspect_calls:
        TERRAIN = terrain_inputs_from_context(CONTEXT, DOCUMENT)
        SECTION = build_landform_section(TERRAIN, TOKENS)
    assert slope_calls.call_count == 0, slope_calls.call_count
    assert valley_calls.call_count == 0 and keypoint_calls.call_count == 0
    assert aspect_calls.call_count == 1, "no landform proposal in the cache -> one Horn pass"
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
print("   compute_slope_percent 0, delineate_valleys 0, detect_keypoints 0; aspect: one Horn pass without the "
      "landform proposal, zero with it")

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
for table in (slope_table, aspect_table):
    for row in table["rows"]:
        for cell in row["cells"][1:] if table is slope_table else row["cells"]:
            assert one_decimal.match(cell), cell
# The parcel's own acreage from the tables' columns, sure to the cent of an acre.
assert sum(float(r["cells"][1]) for r in slope_table["rows"][:-1]) == float(total["cells"][1])
assert round(sum(float(c) for c in aspect_table["rows"][0]["cells"]), 6) == COVER_ACRES
# The summary line's template.
summary = _label_text(SECTION["summary"])
relief = SECTION["contours"]["relief_ft"]
assert summary.startswith(f"Elevation ranges {round(relief):,} ft across the parcel. Most of it is in slope class ")
dominant = max((c for c, _, _ in SLOPE_CLASSES), key=lambda c: counts[c])
assert f"slope class {dominant}, {slope_range_label(dominant)}, and it falls toward the " in summary
assert SECTION["summary"][1] == {"value": f"{round(relief):,}"}, "the relief figure is data"
assert {"value": dominant} in SECTION["summary"] and {"value": slope_range_label(dominant)} in SECTION["summary"]
assert isinstance(SECTION["summary"][-1], str) and "falls toward the" in SECTION["summary"][-1], "the direction is prose"
# Key figures: whole feet, one-decimal slope, the aspect as a WORD.
figures = {f["label"]: f for f in SECTION["key_figures"]}
assert set(figures) == {"lowest elevation", "highest elevation", "relief", "mean slope", "dominant aspect", "keypoints detected"}
assert figures["lowest elevation"]["value"] == f"{round(SECTION['contours']['min_ft']):,} ft"
assert re.match(r"^\d+\.\d%$", figures["mean slope"]["value"]), figures["mean slope"]
assert figures["dominant aspect"].get("word") is True and figures["dominant aspect"]["value"][0].isupper()
assert figures["keypoints detected"]["value"] == str(len(landform_section.parcel_keypoints(TERRAIN)))
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
print("5. the map: tints under contours under valleys under keypoints; the ramp; the legend")
assert SLOPE_TINT_OPACITY["F"] == SLOPE_TINT_CAP == max(SLOPE_TINT_OPACITY.values()), "the darkest tint is the cap"
opacities = [SLOPE_TINT_OPACITY[c] for c, _, _ in SLOPE_CLASSES]
assert opacities == sorted(opacities) and all(b - a >= 0.08 for a, b in zip(opacities, opacities[1:])), opacities
layers = landform_section.build_map_layers(
    TERRAIN, report_map.parcel_contours(TERRAIN.dem, TERRAIN.boundary_polygon_utm),
    landform_section.slope_class_geometries(TERRAIN, landform_section.classify_cells(TERRAIN)["slope_masks"]),
)
ids = [l["id"] for l in layers]
tint_ids = [i for i in ids if i.startswith("slope-")]
assert tint_ids == [f"slope-{c}" for c in slope_table["classes"]], "one tint per class present, in class order"
assert ids[len(tint_ids):len(tint_ids) + 2] == ["contours", "index-contours"], ids
assert "valleys" in ids and ids.index("valleys") > ids.index("index-contours")
assert "keypoints" not in ids, "the fixture terrain has no on-parcel keypoint; nothing is invented"
for spec in layers:
    if spec["id"].startswith("slope-"):
        assert spec["kind"] == "polygon" and spec["fill"] == "terrain" and spec["stroke"] is None
        assert spec["fill_opacity"] == SLOPE_TINT_OPACITY[spec["id"][-1]] <= SLOPE_TINT_CAP
        assert spec["legend"] == [{"value": spec["id"][-1]}, " ", {"value": slope_range_label(spec["id"][-1])}]
valleys = [l for l in layers if l["id"] == "valleys"][0]
assert valleys["kind"] == "line" and valleys["stroke"] == "ink-muted" and valleys["dash"]
assert all(g.within(TERRAIN.boundary_polygon_utm.buffer(0.01)) for g in valleys["geometries"]), "valleys are clipped"
rendered = SECTION["map"]
svg = rendered["svg"]
colours = set(re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{6})"', svg))
assert colours <= set(TOKENS.values()), colours - set(TOKENS.values())
assert svg.index('id="layer-slope-') < svg.index('id="layer-contours"') < svg.index('id="layer-valleys"') < svg.index('id="parcel-boundary"')
assert "rotate(" in svg, "an index label was set"
legend = [_label_text(e["parts"]) for e in rendered["legend"]]
interval = SECTION["contours"]["interval_ft"]
assert legend == [f"{c} {slope_range_label(c)}" for c in slope_table["classes"]] + [f"Contours, {interval} ft", "Valleys"], legend
# A keypoint, when the session has one on the parcel, draws as the asterisk and earns its entry.
centre = TERRAIN.boundary_polygon_utm.representative_point()
with_keypoint = TerrainInputs(**{**TERRAIN.__dict__, "keypoints": [
    {"point_utm": Point(centre.x, centre.y), "on_parcel": True, "elevation_m": 310.0},
    {"point_utm": Point(centre.x + 900, centre.y), "on_parcel": False, "elevation_m": 300.0},
]})
kp_section = build_landform_section(with_keypoint, TOKENS)
assert '<g id="layer-keypoints">' in kp_section["map"]["svg"]
assert kp_section["map"]["svg"].count('id="layer-keypoints"') == 1
kp_group = kp_section["map"]["svg"].split('<g id="layer-keypoints">', 1)[1].split("</g>", 1)[0]
assert kp_group.count("<line") == 3, "one on-parcel keypoint -> one asterisk of three strokes"
assert [_label_text(e["parts"]) for e in kp_section["map"]["legend"]][-1] == "Keypoints"
assert {f["label"]: f["value"] for f in kp_section["key_figures"]}["keypoints detected"] == "1"
print(f"   layers {ids}; ramp {opacities}; legend {legend}; keypoint asterisk verified")

# ======================================================================
# 6. The pages
# ======================================================================
print("6. the pages: cover acreage, colour literals, decimal alignment, the footer, Climate intact")
html = site_report.render_site_report_html(DATA, generated_on=GENERATED_ON, terrain=TERRAIN)
assert f'<p class="cover__acres"><span class="data">{COVER_ACRES:.1f}</span> acres</p>' in html
assert 'class="section section--landform"' in html and 'class="section section--climate"' in html
assert html.index("section--climate") < html.index("section--landform"), "outline order"
assert "III" in html and "Landform" in html
# Colour literals: the stylesheet, every template, the renderer, the section builder -> only TOKENS.
files = [report_map.__file__, landform_section.__file__]
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
assert len(pages) == 4, len(pages)


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


landform_tables = _tables(pages[2]) + _tables(pages[3])
assert len(landform_tables) == 2, len(landform_tables)
slope_cells, aspect_cells = (_numeric_cells(t) for t in landform_tables)
# 3 per class row + 2 in the total row; 18 in the aspect table.
assert len(slope_cells) == 3 * len(slope_table["classes"]) + 2, len(slope_cells)
assert len(aspect_cells) == 18, len(aspect_cells)
landform_cells = slope_cells + aspect_cells
# Right edges: every cell in a column shares one -- 3 columns, then 9.
for cells, expected in ((slope_cells, 3), (aspect_cells, 9)):
    edges = {right for right, _, _ in cells}
    assert len(edges) == expected, (expected, sorted(edges))
by_edge = {right for right, _, _ in slope_cells} | {right for right, _, _ in aspect_cells}
# Tabular figures: one glyph advance across every numeric cell of both tables.
advances = set()
for right, text, box in landform_cells:
    text_boxes = [b for b in _walk(box) if type(b).__name__ == "TextBox"]
    assert text_boxes, text
    advances.add(round(sum(b.width for b in text_boxes) / len(text), 2))
assert len(advances) == 1, sorted(advances)
# The decimal point sits one glyph in from the right edge in every one-decimal cell.
for right, text, box in landform_cells:
    if "." in text:
        assert len(text) - text.index(".") == 2, text
flat = "".join("".join(b.text for b in _walk(p._page_box) if type(b).__name__ == "TextBox") for p in pages[2:])
squash = "".join(flat.split())
assert "".join(landform_section.CAVEAT_LINE.split()) in squash
assert "".join(("Source: USGS 3DEP elevation, resampled to 5 m · retrieved "
                + landform_section.format_retrieved_on(TERRAIN.retrieved_on)).split()) in squash
assert "III·LANDFORM" in squash.upper() or "III" in squash
# Climate is untouched: its page still carries its 60 numeric cells.
assert len(_numeric_cells(pages[1]._page_box)) == 60
print(f"   4 pages; {len(landform_cells)} Landform numeric cells in 3 + 9 columns, one glyph advance "
      f"{advances.pop()} pt; literals only in TOKENS across {len(files) + 1} files")

print("\ntest_landform_section.py: all sections passed")
print(offline_harness.summary())
