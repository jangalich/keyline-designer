"""
test_access_section.py

THE ACCESS SECTION'S PAGE -- branch 10, phase 2 of
site-data-report-proposal.md's build sequence. Runs offline on the real
parcel: the captured 3DEP grid, the real SSURGO, NHD and road rows
(access_reference_fixture.py) through an offline session, the captured
soil road ratings, WeasyPrint for the pages.

  1. NO COLOUR LITERAL OUTSIDE TOKENS: the stylesheet, every template,
     both renderers, the Access builder and derivations.
  2. THE MAP: at Landform's extent, scale and frame -- the same metres
     per unit, drawn bbox and scale bar, measured -- so the sections'
     maps compare as pictures of the same land; the plate in order (contours set back, the
     frontage band, the hachures, road casing under roads, track casing
     under the dashed track); every geometry within the frame; the
     hachures as ticks inward; drawn runs merge nothing longer than two
     stations and leave the measured figures alone; colours only from
     TOKENS; the empty-frame note on a parcel with no road.
  3. THE WORDS AND TABLES: the summary's three figures, the frontage
     table (every road, a dash for a true zero, no total row), the soil
     table's three rows with the limiting features as a prose column and
     the acreage column summing to the cover, the captions carrying the
     drawing rule, the slope clause and the water-table line, one source
     line per source; the degraded soil layer's statement; the landlocked
     parcel's sentence.
  4. NO PROPOSED-ROAD LANGUAGE anywhere in the section's words: corridor,
     route, cost, propose, recommend, should, build, candidate, suitable.
  5. THE PAGES: eleven (cover, Climate 2, Landform 3, Water 3, Access 2),
     the map page and the numbers page with the continuation eyebrow, no
     box past the measure, decimal alignment across both tables with
     dashes included; the SPILL RULE measured -- four road rows sit under
     the map, five move the frontage table whole to the numbers page,
     nine likewise with every road listed; the degraded render at the
     same page count.
"""

import copy
import os
import re
from datetime import date

import numpy as np
from shapely.geometry import LineString, MultiLineString, box
from shapely.ops import substring
from rasterio.warp import transform as warp_transform

import offline_harness

offline_harness.install()

import access_derivations as ad  # noqa: E402
import access_reference_fixture as fixture  # noqa: E402
import access_section as acs  # noqa: E402
import landform_derivations  # noqa: E402
import landform_section  # noqa: E402
import report_chart  # noqa: E402
import report_layout  # noqa: E402
import report_map  # noqa: E402
import site_report  # noqa: E402
import water_derivations as wd  # noqa: E402
import water_section  # noqa: E402
from landform_section import ZERO_DASH  # noqa: E402
from site_report import TOKENS  # noqa: E402

GENERATED_ON = date(2026, 9, 23)
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _text(parts) -> str:
    if isinstance(parts, str):
        return parts
    return "".join(p if isinstance(p, str) else str(p["value"]) for p in parts)


def _cell(cell) -> str:
    return cell["value"] if isinstance(cell, dict) else cell


# ======================================================================
# 1. Colour literals
# ======================================================================
print("1. no colour literal outside site_report.TOKENS")
files = [report_map.__file__, report_chart.__file__, acs.__file__, ad.__file__]
for root, _, names in os.walk(site_report.TEMPLATES_DIRECTORY):
    files += [os.path.join(root, n) for n in names]
for path in files:
    with open(path, encoding="utf-8") as handle:
        hits = HEX.findall(handle.read())
    assert not hits, f"{path} carries colour literal(s) {hits}"
with open(site_report.__file__, encoding="utf-8") as handle:
    assert sorted(HEX.findall(handle.read())) == sorted(TOKENS.values())
assert set(HEX.findall(site_report.render_stylesheet())) == set(TOKENS.values())
print(f"   {len(files) + 1} files checked")

# ======================================================================
# 2. The map
# ======================================================================
print("2. the map at Landform's extent, scale and frame; the plate in order")
DATA = fixture.report_data()
with fixture.Harness():
    SESSION = fixture.Session()
    CONTEXT = SESSION.context()
    DOCUMENT = SESSION.stored()
    TERRAIN = landform_section.terrain_inputs_from_context(CONTEXT, DOCUMENT)
    WATER = wd.water_inputs_from_context(CONTEXT, DOCUMENT, DATA)
    INPUTS = ad.access_inputs_from_context(CONTEXT, DOCUMENT, DATA)
    SECTIONS = site_report.build_sections(DATA, TERRAIN, WATER, INPUTS)
assert [s["number"] for s in SECTIONS] == ["II", "III", "IV", "V"]
LANDFORM, SECTION = SECTIONS[1], SECTIONS[3]
assert SECTION["name"] == "Access" and SECTION["template"] == "access.html" and SECTION["heading"] == "Access"
DERIVED = SECTION["derived"]
m = SECTION["map"]
# IDENTICAL TO LANDFORM'S, MEASURED: the same metres per unit, the same drawn bbox, the same scale bar, the same
# fitted frame -- the other sections' map of this parcel with different layers on it.
assert m["extent_utm"] == LANDFORM["map"]["extent_utm"] and m["meters_per_unit"] == LANDFORM["map"]["meters_per_unit"]
assert m["drawn_bbox"] == LANDFORM["map"]["drawn_bbox"] and m["scale_bar"] == LANDFORM["map"]["scale_bar"] and m["frame"] == LANDFORM["map"]["frame"]
assert m["frame"] == (SECTIONS[2]["map"]["frame"]) and m["meters_per_unit"] == SECTIONS[2]["map"]["meters_per_unit"], "and Water's"
parcel = INPUTS.boundary_polygon_utm
minx, miny, maxx, maxy = parcel.bounds
access_visible = report_map.visible_extent_utm(parcel)
assert access_visible == report_map.visible_extent_utm(parcel)
colours = set(re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{6})"', m["svg"]))
assert colours <= set(TOKENS.values()), colours - set(TOKENS.values())
svg = m["svg"]
order = ["layer-contours", "layer-frontage", "layer-undrivable", "layer-road-casing", "layer-roads", "layer-track-casing", "layer-tracks", "parcel-boundary"]
positions = [svg.index(f'id="{i}"') for i in order]
assert positions == sorted(positions), "the plate draws in order"
assert 'stroke-opacity="0.40"' in svg and f'stroke-opacity="{acs.FRONTAGE_BAND_OPACITY:.2f}"' in svg
layers = acs.build_map_layers(INPUTS, DERIVED, report_map.parcel_contours(INPUTS.dem, parcel))
by_id = {l["id"]: l for l in layers}
assert by_id["road-casing"]["stroke"] == "page" and by_id["road-casing"]["stroke_width"] > by_id["roads"]["stroke_width"]
assert by_id["roads"]["stroke"] == "ink" and by_id["roads"]["dash"] is None and by_id["tracks"]["stroke"] == "ink-muted" and by_id["tracks"]["dash"]
assert by_id["track-casing"]["stroke"] == "page" and by_id["frontage"]["stroke"] == "oxide" and by_id["undrivable"]["stroke"] == "terrain"
road_labels = by_id["roads"]["labels"]
assert {l for l in road_labels if l} == {"N Montour Rd", "N Montour Dr"} and road_labels.count(None) == 1, "the unnamed road carries no label"
assert len(road_labels) == len(by_id["roads"]["geometries"]) == 4, "the far N Montour Rd segment is clipped away"
assert m["labels_placed"]["roads"].count(True) >= 1, "at least one road label is set along its line"
visible = box(*access_visible)
for spec in layers:
    for geometry in spec["geometries"]:
        assert geometry.within(visible.buffer(0.01)), f"{spec['id']} draws outside the frame"
# Hachures: ticks of one length, each starting on the boundary and pointing inward.
ticks = by_id["undrivable"]["geometries"][0]
assert isinstance(ticks, MultiLineString) and len(ticks.geoms) == 159, len(ticks.geoms)
tick_length = acs.TICK_LENGTH_PT * m["meters_per_unit"]
for tick in list(ticks.geoms)[::7]:
    (x0, y0), (x1, y1) = tick.coords
    assert abs(tick.length - tick_length) < 1e-6 and parcel.exterior.distance(tick.interpolate(0)) < 0.01
    assert parcel.contains(tick.interpolate(0.5, normalized=True)), "ticks point inward"
# Drawn runs: nothing shorter than two stations survives, the measured runs and lengths are untouched.
drawn = acs.drawn_runs(DERIVED.boundary)
step = DERIVED.boundary["step_m"]
assert all((r["end_m"] - r["start_m"]) / step + 1e-9 >= acs.DRAWN_RUN_MIN_STATIONS for r in drawn), drawn
assert abs(sum(r["end_m"] - r["start_m"] for r in drawn) - DERIVED.boundary["perimeter_m"]) < 1e-6
assert len(drawn) < len(DERIVED.boundary["runs"]) and any(r["length_m"] <= step for r in DERIVED.boundary["runs"])
assert round(DERIVED.boundary["lengths_m"][ad.UNDRIVABLE], 1) == 600.1, "the figures are as measured"
drawn_undrivable = sum(r["end_m"] - r["start_m"] for r in drawn if r["state"] == ad.UNDRIVABLE)
assert len(drawn) == 13 and abs(drawn_undrivable - DERIVED.boundary["lengths_m"][ad.UNDRIVABLE] + 10.0) < 1e-6, \
    "drawing moves 10 m of undrivable boundary into its drivable neighbours and nothing else"
legend = [_text(e["parts"]) for e in m["legend"]]
assert legend == ["Contours, 10 ft", "Frontage, a mapped road within 49 ft", "Steep boundary, 15% and over", "Mapped roads",
                  "Track, a mapped road on the parcel"], legend
assert "map-note" not in svg
# A parcel with no road: the note on the map, no road layers.
none_inputs = ad.AccessInputs(**{**INPUTS.__dict__, "farm_roads": []})
none_section = acs.build_access_section(none_inputs, TOKENS)
assert "map-note" in none_section["map"]["svg"] and "No mapped road within</text>" in none_section["map"]["svg"]
assert "layer-roads" not in none_section["map"]["svg"] and "layer-frontage" not in none_section["map"]["svg"]
print(f"   m/unit {m['meters_per_unit']:.4f} = 2 x {LANDFORM['map']['meters_per_unit']:.4f}; frame {m['frame'][0]:.0f} x {m['frame'][1]:.0f} pt; "
      f"{len(ticks.geoms)} ticks; {len(DERIVED.boundary['runs'])} measured runs drawn as {len(drawn)}")

# ======================================================================
# 3. The words and the tables
# ======================================================================
print("3. the summary, the tables, the captions, the sources; the degraded and landlocked cases")
COVER = round(INPUTS.parcel_acres, 1)
summary = _text(SECTION["summary"])
assert summary == ("Mapped roads front 1,521 ft of the 3,265 ft boundary, N Montour Rd on the west and an unnamed road on the north. "
                   "1,296 ft of the boundary is under 15% slope, 583 ft of it on that frontage; the edges from east round to north are "
                   "steeper. Soil rated very limited for a local road covers 65% of the parcel."), summary
assert [p["value"] for p in SECTION["summary"] if isinstance(p, dict)] == ["1,521 ft", "3,265 ft", "1,296 ft", "15%", "583 ft", "65%"]
assert DERIVED.frontage["total_m"] < DERIVED.frontage["perimeter_m"] and round(DERIVED.frontage["total_m"] / DERIVED.frontage["perimeter_m"], 3) == 0.466
frontage = SECTION["frontage_table"]
assert frontage["columns"] == ["Along the boundary, ft", "Under 15%, ft"] and frontage["compact"] and frontage["spill"] is False
assert [(r["label"], r["cells"]) for r in frontage["rows"]] == [("N Montour Rd, local road", ["1,145", "583"]),
                                                                ("Unnamed road, local road", ["441", ZERO_DASH])]
assert not any(r["label"] == "Total" for r in frontage["rows"]), "the summary carries the totals"
soil = SECTION["soil_table"]
assert soil["columns"] == ["Acres", "% of parcel", "Limiting features"] and soil["text_columns"] == ["Limiting features"]
assert [r["label"] for r in soil["rows"]] == ["Not limited", "Somewhat limited", "Very limited", "Total"]
assert round(sum(soil["acres"]), 6) == COVER and round(sum(soil["shares"]), 6) == 100.0
assert [_cell(c) for c in soil["rows"][0]["cells"]] == [ZERO_DASH, ZERO_DASH, ZERO_DASH]
assert [_cell(c) for c in soil["rows"][1]["cells"]] == ["4.7", "35.2", "slope, frost action, low strength and depth to saturated zone"]
assert [_cell(c) for c in soil["rows"][2]["cells"]] == ["8.5", "64.8", "frost action, slope, depth to saturated zone, low strength and shrink-swell"]
assert soil["rows"][2]["cells"][2]["kind"] == "text" and [_cell(c) for c in soil["rows"][3]["cells"]] == ["13.2", "100.0", ""]
map_caption = _text(SECTION["map_caption"])
assert map_caption == ("Steep boundary: slope 16 ft inside the line of 15% or more; runs under two samples are merged for drawing only. "
                       "The mapped lane climbs 14% over 126 ft, 19% at the steepest step, entering on 25–27% ground; no stream crosses the parcel."), map_caption
frontage_caption = _text(SECTION["frontage_caption"])
assert frontage_caption.startswith("Frontage is the boundary within 49 ft of a mapped road; the second column is its length under 15%. ")
assert "both frontage and track and may be a private lane" in frontage_caption and frontage_caption.endswith(acs.ROADS_CAVEAT[:1].lower() + acs.ROADS_CAVEAT[1:])
soil_caption = _text(SECTION["soil_caption"])
assert soil_caption == ("NRCS's rating by dominant components; its slope feature is the survey's slope phase, not the elevation model's grid "
                        "above. Atkins is very limited partly for a shallow water table, as the Water section shows; roadfill is poor on "
                        "every map unit."), soil_caption
sources = [_text(line) for line in SECTION["sources"]]
assert len(sources) == 3 and sources[0].startswith("USGS National Map transportation") and "2016" in sources[0]
assert sources[1] == "USDA NRCS SSURGO, soil survey PA003, version of 9/5/2025: cointerp, local roads and streets, roadfill."
assert [m_["source"] for m_ in SECTION["methods"]] == ["USGS National Map transportation", "USDA NRCS SSURGO", "USGS 3DEP"]
assert "THE TOLERANCE IS A JUDGMENT" in SECTION["methods"][0]["method"] and all(m_["terms"] for m_ in SECTION["methods"])
assert SECTION["spill"] is False
# The degraded soil layer: the statement, no table, the roads untouched.
degraded_data = fixture.report_data(soil_road_ratings_rows=None, unavailable={
    "soil_road_ratings": {"label": "soil road-construction ratings", "reason": "source_unavailable", "error": "down"}})
degraded_inputs = ad.access_inputs_from_context(CONTEXT, DOCUMENT, degraded_data)
degraded = acs.build_access_section(degraded_inputs, TOKENS)
assert degraded["soil_table"] is None and _text(degraded["soil_unavailable"]).startswith("SSURGO's road-construction ratings did not answer")
assert _text(degraded["summary"]).endswith("the edges from east round to north are steeper. ") and len(degraded["sources"]) == 2
assert degraded["frontage_table"]["rows"] == frontage["rows"]
# A landlocked parcel: one road 40 m east, no frontage, the nearest road's distance and bearing in the summary.
xs, ys = warp_transform(INPUTS.dem["crs"], "EPSG:4326", [maxx + 40.0, maxx + 40.0], [miny, maxy])
far_row = {"name": "Far Rd", "geometry": {"type": "LineString", "coordinates": [[xs[0], ys[0]], [xs[1], ys[1]]]},
           "properties": {"layer": 31, "layer_name": "Local connecting road"}}
far = acs.build_access_section(ad.AccessInputs(**{**INPUTS.__dict__, "farm_roads": [far_row]}), TOKENS)
assert _text(far["summary"]).startswith("No mapped road touches the boundary: the nearest, Far Rd, lies 131 ft to the E. ")
assert far["frontage_table"] is None and _text(far["frontage_caption"]).startswith("No mapped road runs within 49 ft of the boundary.")
assert "layer-frontage" not in far["map"]["svg"] and "map-note" not in far["map"]["svg"], "the road is in the frame, the parcel has no frontage"
assert _text(far["map_caption"]).startswith("Steep boundary") and "No mapped lane lies on the parcel" in _text(far["map_caption"])
print(f"   summary {len(summary.split())} words; soil acres {soil['acres']} -> {sum(soil['acres'])}; landlocked: 131 ft to the E")

# ======================================================================
# 4. No proposed-road language
# ======================================================================
print("4. the section describes the access that exists: no corridor, route, cost or proposal")
tables = [t for t in (frontage, soil) if t]
words = " ".join([
    summary, map_caption, frontage_caption, soil_caption, _text(SECTION["soil_unavailable"]), " ".join(legend), " ".join(sources),
    SECTION["heading"], _text(far["summary"]), _text(far["frontage_caption"]), _text(none_section["summary"]),
    " ".join(none_section["map"]["svg"].split("<text")[1:]),
    " ".join(" ".join([_text(r["label"])] + [_cell(c) for c in r["cells"]]) for t in tables for r in t["rows"]),
    " ".join(" ".join(t["columns"]) + " " + t["corner"] for t in tables),
]).lower()
for banned in (r"\bcorridor", r"\broute", r"\bcost", r"\bpropos", r"\brecommend", r"\bshould\b", r"\bbuild", r"\bcandidate",
               r"\bsuitab", r"\bsit(e|es|ing|ed)\b", r"\balignment", r"\bculvert", r"\bnew road", r"\bdesign"):
    assert not re.search(banned, words), (banned, re.search(banned, words).group(0))
print("   14 patterns, none found")

# ======================================================================
# 5. The pages
# ======================================================================
print("5. eleven pages, no overflow, decimal alignment; the spill rule at four, five and nine roads; the degraded render")
from weasyprint import HTML  # noqa: E402

html = site_report.render_site_report_html(DATA, generated_on=GENERATED_ON, terrain=TERRAIN, water=WATER, access=INPUTS)
assert 'class="section section--access section--map"' in html and html.count("Access, continued") == 1 and html.count("V</span> · Access") == 2
assert '<td class="text">' in html and '<th class="text">Limiting features</th>' in html
assert set(HEX.findall(html)) == set(TOKENS.values())
document = HTML(string=html, base_url=site_report.TEMPLATES_DIRECTORY).render()
pages = document.pages
assert len(pages) == 11, len(pages)
assert report_layout.overflowing_boxes(document) == [], report_layout.overflowing_boxes(document)


def _walk(b):
    yield b
    for child in getattr(b, "children", []) or []:
        yield from _walk(child)


def _classes_on(page):
    return {(b.element.get("class") or "").split()[0]
            for b in _walk(page._page_box) if getattr(b, "element", None) is not None and b.element.get("class")}


def _tables(page):
    return [b for b in _walk(page._page_box) if type(b).__name__ == "TableBox"]


def _numeric_cells(table_box):
    cells = []
    for child in _walk(table_box):
        if type(child).__name__ == "TableCellBox" and child.element_tag == "td" and "num" in (child.element.get("class") or ""):
            text = "".join(child.element.itertext()).strip()
            if text:
                cells.append((round(child.position_x + child.width, 3), text, child))
    return cells


page, numbers = pages[9], pages[10]
assert {"report-map", "summary", "heading", "eyebrow", "data-table", "caption"} <= _classes_on(page)
assert not ({"key-figures", "source-footer"} & _classes_on(page)) and len(_tables(page)) == 1
assert {"eyebrow", "data-table", "caption", "source-footer"} <= _classes_on(numbers) and "report-map" not in _classes_on(numbers)
assert len(_tables(numbers)) == 1
advances = set()
for table_box, columns in zip(_tables(page) + _tables(numbers), (2, 2)):
    cells = _numeric_cells(table_box)
    edges = {right for right, _, _ in cells}
    assert len(edges) == columns, (columns, sorted(edges))
    for right, text, cell in cells:
        if text == ZERO_DASH:
            continue
        text_boxes = [b for b in _walk(cell) if type(b).__name__ == "TextBox"]
        advances.add(round(sum(b.width for b in text_boxes) / len(text), 2))
        if "." in text:
            assert len(text) - text.index(".") == 2, text
assert len(advances) == 1, sorted(advances)
flat = "".join("".join(b.text for b in _walk(p._page_box) if type(b).__name__ == "TextBox") for p in (page, numbers))
squash = "".join(flat.split())
for needle in ("merged for drawing only", "shallow water table", "may be a private lane", "not the elevation model's grid above"):
    assert "".join(needle.split()) in squash, needle
assert "V·ACCESS" in squash.upper() and "V·ACCESS,CONTINUED" in squash.upper()
# The landform maps and Water intact ahead of it.
assert len(_numeric_cells(pages[2]._page_box)) == 108 + 6 and 'class="section section--water section--map"' in html


def _roads_around(count: int) -> list:
    """`count` named roads laid 6 m outside the boundary, one per equal arc."""
    ring = LineString(parcel.exterior.coords)
    outward = ring.offset_curve(-6.0)
    if parcel.contains(outward.interpolate(0.5, normalized=True)):
        outward = ring.offset_curve(6.0)
    rows = []
    for k in range(count):
        piece = substring(outward, k * outward.length / count, (k + 1) * outward.length / count)
        lon, lat = warp_transform(INPUTS.dem["crs"], "EPSG:4326", [c[0] for c in piece.coords], [c[1] for c in piece.coords])
        rows.append({"name": f"Road {k + 1}", "geometry": {"type": "LineString", "coordinates": [[x, y] for x, y in zip(lon, lat)]},
                     "properties": {"layer": 32, "layer_name": "Local road"}})
    return rows


def _render(inputs):
    h = site_report.render_site_report_html(DATA, generated_on=GENERATED_ON, terrain=TERRAIN, water=WATER, access=inputs)
    return h, HTML(string=h, base_url=site_report.TEMPLATES_DIRECTORY).render()


# THE SPILL RULE, measured: four road rows sit under the map; five move the frontage table whole to the numbers page,
# nine likewise, every road listed and the total the perimeter, not the sum. Eleven pages every time.
assert acs.FRONTAGE_ROWS_MAX == 4
for count, spill in ((4, False), (5, True), (9, True)):
    many = ad.AccessInputs(**{**INPUTS.__dict__, "farm_roads": _roads_around(count)})
    section = acs.build_access_section(many, TOKENS)
    assert section["spill"] is spill and len(section["frontage_table"]["rows"]) == count, (count, section["spill"])
    assert section["derived"].frontage["total_m"] <= section["derived"].frontage["perimeter_m"] + 1e-6
    assert section["derived"].frontage["sum_of_roads_m"] > section["derived"].frontage["total_m"], "overlapping bands, counted once"
    many_html, many_document = _render(many)
    assert len(many_document.pages) == 11, (count, len(many_document.pages))
    assert report_layout.overflowing_boxes(many_document) == []
    map_page, numbers_page = many_document.pages[9], many_document.pages[10]
    assert ("the frontage table is on the next page" in _text(section["map_caption"])) is spill
    assert ("on this page rather than under the map" in _text(section["frontage_caption"])) is spill
    assert len(_tables(map_page)) == (0 if spill else 1) and len(_tables(numbers_page)) == (2 if spill else 1)
    assert "source-footer" in _classes_on(numbers_page) and "report-map" not in _classes_on(numbers_page)
nine = acs.build_access_section(ad.AccessInputs(**{**INPUTS.__dict__, "farm_roads": _roads_around(9)}), TOKENS)
assert _text(nine["summary"]).startswith("Mapped roads front 3,265 ft of the 3,265 ft boundary")
# The degraded render: the statement where the soil table would be, still eleven pages.
degraded_html, degraded_document = _render(degraded_inputs)
assert len(degraded_document.pages) == 11 and report_layout.overflowing_boxes(degraded_document) == []
assert "unavailable" in _classes_on(degraded_document.pages[10]) and len(_tables(degraded_document.pages[10])) == 0
print(f"   11 pages, both tables aligned at one advance {advances.pop()} pt; 4 roads under the map, 5 and 9 spill; degraded 11")

print("\ntest_access_section.py: all sections passed")
print(offline_harness.summary())
