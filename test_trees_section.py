"""
test_trees_section.py

THE TREES & FORESTRY SECTION'S PAGES -- branch 11, phase 2 of
site-data-report-proposal.md's build sequence. Runs offline on the real
parcel: the captured 3DEP grid, the parcel's own lidar HAG, the real
SSURGO, NHD and road rows (trees_reference_fixture.py) through an offline
session, the captured forest type group and woodland rows, WeasyPrint for
the pages; the same again on the captured NLCD TCC dict for the fallback
render.

  1. NO COLOUR LITERAL OUTSIDE TOKENS: the stylesheet, every template,
     both renderers, the Trees builder and derivations; the field token
     is the frontend's value.
  2. THE MAP: at Landform's extent, scale and frame -- the same metres
     per unit, drawn bbox and scale bar, measured; the plate in order
     (the three screens light to dark, then the contours at full
     strength, then the boundary); the screens as dotted rows in the
     field token on the shared grid, the darkest capped; every geometry
     within the frame; the legend; the fallback's single tint; the
     empty-frame note on a parcel with no canopy.
  3. THE WORDS AND TABLES: the summary, the captions (source and vintage,
     the roof, the resampling edge, closure labelled as not the
     fallback's cover, the two measurements differing, the survey's list
     not a recommendation, Guernsey's missing rows, the windthrow line to
     Water and Climate), every acreage column summing to the cover, the
     dash for a true zero, six species, one source line per source; the
     degraded layers' statements; a source record whose id carries no
     year produces a stated absence.
  4. NO PLANTING OR SITING LANGUAGE, and no species-identification,
     vigour, invasive, noxious or cultivation language, anywhere in the
     section's words.
  5. THE PAGES: thirteen (cover, Climate 2, Landform 3, Water 3, Access 2,
     Trees 2), the map page and the numbers page with the continuation
     eyebrow, no box past the measure, decimal alignment across every
     table with dashes included; the fallback render at the same page
     count with the height table replaced by its statement; the degraded
     renders likewise.
"""

import os
import re
from datetime import date

import numpy as np
from shapely.geometry import box

import offline_harness

offline_harness.install()

import access_derivations as ad  # noqa: E402
import landform_section  # noqa: E402
import report_chart  # noqa: E402
import report_layout  # noqa: E402
import report_map  # noqa: E402
import site_report  # noqa: E402
import soil_woodland as sw  # noqa: E402
import trees_derivations as td  # noqa: E402
import trees_reference_fixture as fixture  # noqa: E402
import trees_section as tsn  # noqa: E402
import water_derivations as wd  # noqa: E402
from canopy_height_data import CANOPY_SOURCE_LIDAR_HAG, CANOPY_SOURCE_NLCD_TCC  # noqa: E402
from landform_section import ZERO_DASH  # noqa: E402
from site_report import TOKENS  # noqa: E402

GENERATED_ON = date(2026, 9, 23)
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _text(parts) -> str:
    if parts is None:
        return ""
    if isinstance(parts, str):
        return parts
    return "".join(p if isinstance(p, str) else str(p["value"]) for p in parts)


def _cell(cell) -> str:
    return cell["value"] if isinstance(cell, dict) else cell


def _table_words(table) -> str:
    if not table:
        return ""
    return " ".join([table["corner"]] + list(table["columns"]) + [" ".join([_text(r["label"])] + [_cell(c) for c in r["cells"]]) for r in table["rows"]])


# ======================================================================
# 1. Colour literals
# ======================================================================
print("1. no colour literal outside site_report.TOKENS; the field token is the frontend's")
files = [report_map.__file__, report_chart.__file__, tsn.__file__, td.__file__]
for root, _, names in os.walk(site_report.TEMPLATES_DIRECTORY):
    files += [os.path.join(root, n) for n in names]
for path in files:
    with open(path, encoding="utf-8") as handle:
        hits = HEX.findall(handle.read())
    assert not hits, f"{path} carries colour literal(s) {hits}"
with open(site_report.__file__, encoding="utf-8") as handle:
    assert sorted(HEX.findall(handle.read())) == sorted(TOKENS.values())
assert set(HEX.findall(site_report.render_stylesheet())) == set(TOKENS.values())
assert TOKENS["field"] == "#4a5f3a" and tsn.SCREEN_TOKEN == "field"
print(f"   {len(files) + 1} files checked")

# ======================================================================
# 2. The map
# ======================================================================
print("2. the map at Landform's extent, scale and frame; the screens light to dark under the contours")
DATA = fixture.report_data()
with fixture.Harness():
    SESSION = fixture.Session()
    CONTEXT = SESSION.context()
    DOCUMENT = SESSION.stored()
    TERRAIN = landform_section.terrain_inputs_from_context(CONTEXT, DOCUMENT)
    WATER = wd.water_inputs_from_context(CONTEXT, DOCUMENT, DATA)
    ACCESS = ad.access_inputs_from_context(CONTEXT, DOCUMENT, DATA)
    INPUTS = td.trees_inputs_from_context(CONTEXT, DOCUMENT, DATA)
    SECTIONS = site_report.build_sections(DATA, TERRAIN, WATER, ACCESS, INPUTS)
assert [s["number"] for s in SECTIONS] == ["II", "III", "IV", "V", "VI"]
LANDFORM, SECTION = SECTIONS[1], SECTIONS[4]
assert SECTION["name"] == "Trees & forestry" and SECTION["template"] == "trees.html" and SECTION["heading"] == "Trees & forestry"
DERIVED = SECTION["derived"]
m = SECTION["map"]
assert m["extent_utm"] == LANDFORM["map"]["extent_utm"] and m["meters_per_unit"] == LANDFORM["map"]["meters_per_unit"]
assert m["drawn_bbox"] == LANDFORM["map"]["drawn_bbox"] and m["scale_bar"] == LANDFORM["map"]["scale_bar"] and m["frame"] == LANDFORM["map"]["frame"]
assert m["frame"] == SECTIONS[3]["map"]["frame"] and m["meters_per_unit"] == SECTIONS[3]["map"]["meters_per_unit"], "and Access's"
parcel = INPUTS.boundary_polygon_utm
colours = set(re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{6})"', m["svg"]))
assert colours <= set(TOKENS.values()), colours - set(TOKENS.values())
assert TOKENS["field"] in colours and TOKENS["terrain"] in colours
svg = m["svg"]
order = ["layer-canopy-15-30", "layer-canopy-30-50", "layer-canopy-50+", "layer-contours", "layer-index-contours", "parcel-boundary"]
positions = [svg.index(f'id="{i}"') for i in order]
assert positions == sorted(positions), "the plate draws in order: screens light to dark, contours, boundary"
layers = tsn.build_map_layers(INPUTS, DERIVED, report_map.parcel_contours(INPUTS.dem, parcel))
by_id = {l["id"]: l for l in layers}
screens = [by_id[f"canopy-{n}"] for n in td.CANOPY_CLASSES]
assert all(l["kind"] == "screen" and l["fill"] == "field" and l["stroke"] is None for l in screens)
dots = [l["screen_dot_pt"] for l in screens]
assert dots == [0.9, 1.3, 1.7] and dots == sorted(dots) and max(dots) <= tsn.SCREEN_DOT_CAP_PT, "light to dark, the darkest capped"
assert all(3.14159 * (d / 2) ** 2 / report_map.SCREEN_SPACING_PT ** 2 < 0.26 for d in dots), "at most a quarter of the ground inked"
# The screen is dotted rows: zero-length dashes on the shared grid, round caps, in the field token, at the class's dot size.
screen_paths = re.findall(r'<path d="M [^"]+" fill="none" stroke="(#[0-9a-f]{6})" stroke-width="([0-9.]+)" stroke-linecap="round" '
                          r'stroke-dasharray="0 ([0-9.]+)" stroke-dashoffset="([0-9.]+)"/>', svg)
assert len(screen_paths) > 100 and {p[0] for p in screen_paths} == {TOKENS["field"]}
assert {float(p[1]) for p in screen_paths} == set(dots) and {float(p[2]) for p in screen_paths} == {report_map.SCREEN_SPACING_PT}
assert all(0 <= float(p[3]) < report_map.SCREEN_SPACING_PT for p in screen_paths), "dash offsets within one spacing: one grid"
# The contours are Landform's, at full strength, above the screens.
assert by_id["contours"]["stroke"] == "terrain" and by_id["contours"]["stroke_opacity"] == 1.0
assert SECTION["contours"]["interval_ft"] == LANDFORM["contours"]["interval_ft"]
visible = box(*report_map.visible_extent_utm(parcel))
for spec in layers:
    for geometry in spec["geometries"]:
        assert geometry.within(visible.buffer(0.01)), f"{spec['id']} draws outside the frame"
# The screens' ground is the height classes' cells, clipped to the parcel: the acres they cover are the table's.
masks = tsn.height_class_masks(INPUTS, DERIVED)
assert {n: int(mk.sum()) for n, mk in masks.items()} == {"15-30": 53, "30-50": 89, "50+": 116}
for name, spec in zip(td.CANOPY_CLASSES, screens):
    area = sum(g.area for g in spec["geometries"])
    assert abs(area - int(masks[name].sum()) * 24.96) < 60, (name, area)
legend = [_text(e["parts"]) for e in m["legend"]]
assert legend == ["Canopy 15–30 ft", "30–50 ft", "50 ft and over", "Contours, 10 ft"], legend
assert all('stroke-dasharray="0 3.00"' in e["swatch"] for e in m["legend"][:3]), "the swatch is drawn with the same dots"
assert "map-note" not in svg
# A parcel with no canopy: the note on the map, no screen.
bare = dict(INPUTS.canopy, array=np.zeros_like(INPUTS.canopy["array"]))
none_section = tsn.build_trees_section(td.TreesInputs(**{**INPUTS.__dict__, "canopy": bare}), TOKENS)
assert "map-note" in none_section["map"]["svg"] and "No canopy at 15 ft</text>" in none_section["map"]["svg"]
assert "layer-canopy" not in none_section["map"]["svg"] and none_section["height_table"] is not None
assert [_cell(c) for c in none_section["extent_table"]["rows"][0]["cells"]] == [ZERO_DASH, ZERO_DASH]
print(f"   m/unit {m['meters_per_unit']:.4f} = Landform's; frame {m['frame'][0]:.0f} x {m['frame'][1]:.0f} pt; dots {dots} pt on a "
      f"{report_map.SCREEN_SPACING_PT:.0f} pt grid; {len(screen_paths)} dotted rows")

# ======================================================================
# 3. The words and the tables
# ======================================================================
print("3. the summary, the tables, the captions, the sources; the degraded cases; the year parsed defensively")
COVER = round(INPUTS.parcel_acres, 1)
summary = _text(SECTION["summary"])
assert summary == ("Lidar of 2019 measures 1.6 ac of canopy at 15 ft and over, 12% of the parcel, 38% closed at a 30 m grain; the tallest "
                   "cell is 86 ft. The forest type model calls 0.6 ac of it forest, oak/hickory. The soil survey rates windthrow hazard "
                   "moderate on 10.0 ac."), summary
assert [p["value"] for p in SECTION["summary"] if isinstance(p, dict)] == ["1.6 ac", "12%", "38%", "86 ft", "0.6 ac", "10.0 ac"]
map_caption = _text(SECTION["map_caption"])
assert map_caption == ("Lidar first-return height above ground, 2 m, acquired 2019, resampled to the 5 m grid; canopy is a cell at or above "
                       "the design's 15 ft threshold, so a roof reads as canopy too. Contours at Landform's interval."), map_caption
extent = SECTION["extent_table"]
assert extent["columns"] == ["Acres", "% of parcel"] and extent["compact"]
assert [(r["label"], r["cells"]) for r in extent["rows"]] == [("Canopy, 15 ft and over", ["1.6", "12.0"]), ("Open, under 15 ft", ["11.5", "87.4"]),
                                                             ("No value", ["0.1", "0.6"]), ("Total", ["13.2", "100.0"])]
assert round(sum(extent["acres"]), 6) == COVER and round(sum(extent["shares"]), 6) == 100.0
extent_caption = _text(SECTION["extent_caption"])
assert extent_caption == ("Within the 30 m blocks that hold canopy, 38% of the ground is canopy: closure at a 30 m grain, derived from the "
                          "height threshold, not the fallback product's percent cover; 6 of 27 blocks are over three-quarters closed. "
                          "The 13 cells with no value are the edge of the resampling, not a gap in coverage."), extent_caption
heights = SECTION["height_table"]
assert [(r["label"], r["cells"]) for r in heights["rows"]] == [("Under 15 ft, not canopy", ["11.5", "87.4"]), ("15–30 ft", ["0.3", "2.5"]),
                                                              ("30–50 ft", ["0.6", "4.1"]), ("50 ft and over", ["0.7", "5.4"]),
                                                              ("No value", ["0.1", "0.6"]), ("Total", ["13.2", "100.0"])]
assert round(sum(heights["acres"]), 6) == COVER and round(sum(heights["shares"]), 6) == 100.0
assert _text(SECTION["height_caption"]) == ("Classed at the design's 15 ft threshold and at 30 and 50 ft, the height of a cell's tallest "
                                            "return; the tallest cell is 86 ft, the canopy's median 47 ft.")
assert SECTION["cover_table"] is None
assert _text(SECTION["forest_type"]) == "The forest type model calls 0.6 ac of the parcel forest, 5%, all oak/hickory."
forest_caption = _text(SECTION["forest_type_caption"])
assert forest_caption == ("FIA BIGMAP, plots of 2014–2018, a model imputing inventory plots to 30 m pixels, the type of forest occupying "
                          "an area, not what stands on any acre. The lidar sees 1.6 ac of canopy where the model calls 0.6 ac forest: "
                          "height returns at 5 m against a classification of stands at 30 m."), forest_caption
species = SECTION["species_table"]
assert species["columns"] == ["Acres rated", "Site index, ft", "Growth, cu ft/ac/yr"] and len(species["rows"]) == 6 == tsn.SPECIES_ROWS_MAX
assert [r["label"] for r in species["rows"]] == ["northern red oak", "yellow-poplar", "sugar maple", "white ash", "Virginia pine", "eastern white pine"]
assert [r["cells"] for r in species["rows"]][:2] == [["10.4", "78 (55–81)", "57"], ["10.4", "89 (55–95)", "86"]]
assert species["rows"][5]["cells"] == ["2.2", "90", "143"] and species["species"] == ["QURU", "LITU", "ACSA3", "FRAM2", "PIVI2", "PIST"]
acres_rated = [float(r["cells"][0]) for r in species["rows"]]
assert acres_rated == sorted(acres_rated, reverse=True) and min(acres_rated) >= tsn.SPECIES_MIN_SHARE * COVER
species_caption = _text(SECTION["species_caption"])
assert species_caption == ("The survey's list for these map units, the 6 species rated on the most ground of 15, not a recommendation; site "
                           "index is height in feet at the base age of the survey's curve. Guernsey, 55% of the Guernsey-Vandergrift unit "
                           "(4.5 ac), carries no rows, so that unit rests on Vandergrift. "), species_caption
limits = SECTION["limitations_table"]
assert limits["columns"] == ["Acres", "% of parcel", "Limiting features"] and limits["text_columns"] == ["Limiting features"]
rows = [(r["label"], [_cell(c) for c in r["cells"]]) for r in limits["rows"]]
assert len(rows) == 11 and rows[0] == ("Harvest equipment operability, well suited", ["13.1", "98.9", ZERO_DASH])
assert rows[1] == ("Harvest equipment operability, poorly suited", ["0.1", "1.1", "low strength, wetness and slope"])
assert rows[6] == ("Windthrow hazard, moderate", ["10.0", "76.1", "hillslope position and water table depth"])
assert rows[8] == ("Erosion hazard off-road, moderate", ["7.7", "58.4", "erodibility, slope and rainfall"])
for group in limits["groups"]:
    assert round(sum(group["acres"]), 6) == COVER and round(sum(group["shares"]), 6) == 100.0, group["interpretation"]
assert [g["interpretation"] for g in limits["groups"]] == list(sw.INTERPRETATIONS)
limits_caption = _text(SECTION["limitations_caption"])
assert limits_caption == ("NRCS's rating by dominant components, each interpretation partitioning the parcel: the soil's capacity, the same "
                          "under a field as under a stand. Windthrow hazard is moderate on 10.0 ac, water table depth the feature on 9.9 ac "
                          "of them, the shallow seasonal water table the Water section maps; Climate's winter wind prevails from the west."), limits_caption
sources = [_text(line) for line in SECTION["sources"]]
assert len(sources) == 4 and sources[0] == ("USGS 3DEP lidar height above ground, PA_WesternPA_2_2019-hag-2m-5-4, 2019, 2 m resampled to "
                                            "5 m, via Microsoft Planetary Computer, retrieved 23 September 2026.")
assert sources[1] == "USDA Forest Service FIA BIGMAP forest type group 2018, plots 2014–2018, 30 m."
assert sources[2] == "USDA NRCS SSURGO, soil survey PA003, version of 9/5/2025: coforprod; cointerp, the four woodland interpretations tabled."
assert sources[3].startswith("USGS 3DEP elevation")
assert [m_["source"] for m_ in SECTION["methods"]] == ["USGS 3DEP lidar height above ground", "USDA Forest Service FIA BIGMAP forest type group",
                                                       "USDA NRCS SSURGO", "USGS 3DEP"]
assert all(m_["terms"] and m_["citation"] for m_ in SECTION["methods"])
assert "Site index base curves: " in SECTION["methods"][2]["notes"][0] and "Schnur 1937 (820)" in SECTION["methods"][2]["notes"][0]
# The acquisition year, parsed defensively: a record without one produces a stated absence.
assert tsn.hag_acquisition_year("PA_WesternPA_2_2019-hag-2m-5-4") == 2019 and tsn.hag_acquisition_year("USGS_LPC_TX_2m_2018") == 2018
assert tsn.hag_acquisition_year("hag-2m-5-4") is None and tsn.hag_acquisition_year(None) is None and tsn.hag_acquisition_year("x_1850_y") is None
assert tsn.hag_acquisition_year("tile_12019") is None, "five digits are not a year"
odd = tsn.build_trees_section(td.TreesInputs(**{**INPUTS.__dict__, "canopy": dict(INPUTS.canopy, source_item_id="hag-tile-7")}), TOKENS)
assert _text(odd["summary"]).startswith("Lidar measures 1.6 ac") and "acquisition year not stated by the source record" in _text(odd["map_caption"])
assert "acquisition year not stated" in _text(odd["sources"][0])
# The degraded layers: the statements, the canopy untouched.
degraded_data = fixture.report_data(forest_type_group=None, soil_woodland_rows=None, unavailable={
    "forest_type_group": {"label": "forest type group", "reason": "source_unavailable", "error": "down"},
    "soil_woodland": {"label": "soil woodland ratings", "reason": "source_unavailable", "error": "down"}})
DEGRADED_INPUTS = td.trees_inputs_from_context(CONTEXT, DOCUMENT, degraded_data)
degraded = tsn.build_trees_section(DEGRADED_INPUTS, TOKENS)
assert degraded["forest_type"] is None and _text(degraded["forest_type_unavailable"]).startswith("The forest type group service did not answer")
assert degraded["species_table"] is None and degraded["limitations_table"] is None
assert _text(degraded["species_unavailable"]).startswith("SSURGO's woodland ratings did not answer") and len(degraded["sources"]) == 2
assert _text(degraded["summary"]).endswith("the tallest cell is 86 ft. ") and degraded["extent_table"]["rows"] == extent["rows"]
no_pixel = fixture.report_data(forest_type_group=None, unavailable={"forest_type_group": {"label": "x", "reason": "no_data_for_parcel", "error": ""}})
assert _text(tsn.build_trees_section(td.trees_inputs_from_context(CONTEXT, DOCUMENT, no_pixel), TOKENS)["forest_type_unavailable"]) == \
    "The forest type group model carries no pixel for this parcel."
# Without Climate's wind the windthrow line stops at the water table.
no_wind = tsn.build_trees_section(td.TreesInputs(**{**INPUTS.__dict__, "wind": None}), TOKENS)
assert _text(no_wind["limitations_caption"]).endswith("the shallow seasonal water table the Water section maps.")
print(f"   summary {len(summary.split())} words; extent {extent['acres']}; heights {heights['acres']}; {len(species['rows'])} species; "
      f"{len(rows)} limitation rows in {len(limits['groups'])} partitions")

# ======================================================================
# 4. No planting, siting, identification, vigour, invasive or cultivation language
# ======================================================================
print("4. the section is an inventory: no planting, siting, identification, vigour, invasive or cultivation language")
tables = [t for t in (extent, heights, species, limits) if t]
words = " ".join([
    summary, map_caption, extent_caption, _text(SECTION["height_caption"]), _text(SECTION["height_unavailable"]), _text(SECTION["forest_type"]),
    forest_caption, _text(SECTION["forest_type_unavailable"]), species_caption, _text(SECTION["species_unavailable"]), limits_caption,
    _text(SECTION["cover_caption"]), " ".join(legend), " ".join(sources), SECTION["heading"], _text(none_section["summary"]),
    " ".join(none_section["map"]["svg"].split("<text")[1:]), _text(degraded["forest_type_unavailable"]), _text(degraded["species_unavailable"]),
    " ".join(_table_words(t) for t in tables),
]).lower()
# THE ONE SANCTIONED PHRASE: the species caption says the list is the survey's, "not a recommendation" -- the author's
# instruction, stated once. It is removed before the grep, which then holds the rest of the words to the rule.
assert words.count("not a recommendation") == 1, words.count("not a recommendation")
words = words.replace("not a recommendation", "")
for banned in (r"\bplant", r"\bwindbreak", r"\bshelterbelt", r"\bsit(ing|ed)\b", r"\bsite\b(?! index)", r"\brecommend", r"\bshould\b",
               r"\bpropos", r"\bdesign(?!'s)", r"\bcandidate", r"\bsuitab", r"\bcorridor", r"\bidentif", r"\bvigou?r", r"\binvasive",
               r"\bnoxious", r"\bcultivat", r"\bguild", r"\bcomposition", r"\bcoppice", r"\borchard", r"\bestablish", r"\bcrop\b",
               r"\bthinning", r"\bharvest(?! equipment)", r"\bgrow(?!th)"):
    hit = re.search(banned, words)
    assert not hit, (banned, hit.group(0), words[max(0, hit.start() - 40):hit.end() + 40])
print("   26 patterns, none found")

# ======================================================================
# 5. The pages
# ======================================================================
print("5. thirteen pages, no overflow, decimal alignment; the fallback render; the degraded renders")
from weasyprint import HTML  # noqa: E402


def _render(trees, data=DATA):
    h = site_report.render_site_report_html(data, generated_on=GENERATED_ON, terrain=TERRAIN, water=WATER, access=ACCESS, trees=trees)
    return h, HTML(string=h, base_url=site_report.TEMPLATES_DIRECTORY).render()


html, document = _render(INPUTS)
assert 'class="section section--trees section--map"' in html and html.count("Trees &amp; forestry, continued") == 1
assert html.count("VI</span> · Trees &amp; forestry") == 2
assert set(HEX.findall(html)) == set(TOKENS.values())
pages = document.pages
assert len(pages) == 13, len(pages)
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


def _check_alignment(pages_, columns_per_table):
    advances = set()
    table_boxes = [t for p in pages_ for t in _tables(p)]
    assert len(table_boxes) == len(columns_per_table), len(table_boxes)
    for table_box, columns in zip(table_boxes, columns_per_table):
        cells = _numeric_cells(table_box)
        edges = {right for right, _, _ in cells}
        assert len(edges) == columns, (columns, sorted(edges))
        for right, text, cell in cells:
            if text == ZERO_DASH:
                continue
            text_boxes = [b for b in _walk(cell) if type(b).__name__ == "TextBox"]
            advances.add(round(sum(b.width for b in text_boxes) / len(text), 2))
            if "." in text and "(" not in text:
                assert len(text) - text.index(".") == 2, text
    assert len(advances) == 1, sorted(advances)
    return advances.pop()


page, numbers = pages[11], pages[12]
assert {"report-map", "summary", "heading", "eyebrow", "data-table", "caption"} <= _classes_on(page)
assert not ({"key-figures", "source-footer"} & _classes_on(page)) and len(_tables(page)) == 1
assert {"eyebrow", "data-table", "caption", "source-footer", "summary"} <= _classes_on(numbers) and "report-map" not in _classes_on(numbers)
assert len(_tables(numbers)) == 3
advance = _check_alignment((page, numbers), (2, 2, 3, 2))
flat = "".join("".join(b.text for b in _walk(p._page_box) if type(b).__name__ == "TextBox") for p in (page, numbers))
squash = "".join(flat.split())
for needle in ("a roof reads as canopy too", "the edge of the resampling, not a gap in coverage", "not the fallback product's percent cover",
               "not what stands on any acre", "not a recommendation", "carries no rows", "Climate's winter wind prevails from the west",
               "the tallest cell is 86 ft"):
    assert "".join(needle.split()) in squash, needle
assert "VI·TREES&FORESTRY" in squash.upper() and "VI·TREES&FORESTRY,CONTINUED" in squash.upper()
# Access and the maps ahead of it intact.
assert 'class="section section--access section--map"' in html and len(_numeric_cells(pages[2]._page_box)) == 108 + 6

# THE FALLBACK RENDER: the TCC session, the height table replaced by its statement, the cover classes, one tint, thirteen pages.
with fixture.Harness(canopy="tcc"):
    TCC_SESSION = fixture.Session()
    TCC_CONTEXT = TCC_SESSION.context()
    TCC_DOCUMENT = TCC_SESSION.stored()
    TCC_INPUTS = td.trees_inputs_from_context(TCC_CONTEXT, TCC_DOCUMENT, DATA)
tcc = tsn.build_trees_section(TCC_INPUTS, TOKENS)
assert tcc["derived"].canopy["source"] == CANOPY_SOURCE_NLCD_TCC and tcc["height_table"] is None and tcc["cover_table"] is not None
assert _text(tcc["summary"]) == ("NLCD Tree Canopy Cover of 2025 marks 1.2 ac of canopy, 9% of the parcel, 29% mean cover within it; no lidar "
                                 "height is available here. The forest type model calls 0.6 ac of it forest, oak/hickory. The soil survey rates "
                                 "windthrow hazard moderate on 10.0 ac.")
assert _text(tcc["map_caption"]).startswith("NLCD Tree Canopy Cover 2025, percent cover per 30 m pixel") and "one tint" in _text(tcc["map_caption"])
assert _text(tcc["height_unavailable"]) == ("No canopy height is reported: this parcel's canopy is NLCD Tree Canopy Cover 2025, percent cover "
                                            "per 30 m pixel; no lidar height product covers it, so height classes are not available.")
assert [(r["label"], r["cells"]) for r in tcc["cover_table"]["rows"]] == [("1–25% cover", ["0.6", "52.9"]), ("26–50% cover", ["0.3", "23.8"]),
                                                                          ("51–75% cover", ["0.1", "9.8"]), ("76–100% cover", ["0.2", "13.5"]),
                                                                          ("All canopy", ["1.2", "100.0"])]
assert [(r["label"], r["cells"]) for r in tcc["extent_table"]["rows"]] == [("Canopy", ["1.2", "9.0"]), ("Open, no canopy", ["12.0", "91.0"]),
                                                                           ("Total", ["13.2", "100.0"])]
assert _text(tcc["extent_caption"]) == "Mean cover within the canopy pixels is 29%, the product's own percent cover. "
assert "The cover product sees 1.2 ac of canopy where the model calls 0.6 ac forest" in _text(tcc["forest_type_caption"])
assert [_text(e["parts"]) for e in tcc["map"]["legend"]] == ["Canopy, NLCD cover 2025", "Contours, 10 ft"]
tcc_layers = tsn.build_map_layers(TCC_INPUTS, tcc["derived"], report_map.parcel_contours(TCC_INPUTS.dem, parcel))
assert [l["id"] for l in tcc_layers][:1] == ["canopy"] and tcc_layers[0]["screen_dot_pt"] == tsn.SCREEN_DOT_SINGLE_PT == 1.3
assert _text(tcc["sources"][0]) == ("USDA Forest Service NLCD Tree Canopy Cover v2025-6, 2025, 30 m, via the IIPP image service, retrieved "
                                    "23 September 2026.")
assert tcc["methods"][0]["source"] == "USDA Forest Service NLCD Tree Canopy Cover" and "year pinned" in tcc["methods"][0]["method"]
assert tcc["map"]["meters_per_unit"] == m["meters_per_unit"] and tcc["map"]["frame"] == m["frame"]
tcc_html, tcc_document = _render(TCC_INPUTS)
assert len(tcc_document.pages) == 13 and report_layout.overflowing_boxes(tcc_document) == []
tcc_numbers = tcc_document.pages[12]
assert "unavailable" in _classes_on(tcc_numbers) and len(_tables(tcc_numbers)) == 3
_check_alignment((tcc_document.pages[11], tcc_numbers), (2, 2, 3, 2))
tcc_squash = "".join("".join(b.text for b in _walk(tcc_numbers._page_box) if type(b).__name__ == "TextBox").split())
assert "Nocanopyheightisreported" in tcc_squash and "Canopyheight" not in tcc_squash
# The degraded renders: the statements where the tables would be, still thirteen pages.
for degraded_inputs in (DEGRADED_INPUTS, td.trees_inputs_from_context(CONTEXT, DOCUMENT, no_pixel)):
    _, degraded_document = _render(degraded_inputs)
    assert len(degraded_document.pages) == 13 and report_layout.overflowing_boxes(degraded_document) == []
assert "unavailable" in _classes_on(_render(DEGRADED_INPUTS)[1].pages[12]) and len(_tables(_render(DEGRADED_INPUTS)[1].pages[12])) == 1
print(f"   13 pages, four tables aligned at one advance {advance} pt; the fallback 13 with the statement; both degraded renders 13")

print("\ntest_trees_section.py: all sections passed")
print(offline_harness.summary())
