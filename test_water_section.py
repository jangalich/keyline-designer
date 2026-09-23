"""
test_water_section.py

THE WATER & HYDROLOGY SECTION'S PAGES -- branch 9, phase 2 of
site-data-report-proposal.md's build sequence. Runs offline on the real
parcel: the captured 3DEP grid, the real SSURGO and NHD rows and one real
response per Water source (water_reference_fixture.py) through an offline
session, the Daymet fixture for Climate, WeasyPrint for the pages.

  1. NO COLOUR LITERAL OUTSIDE TOKENS: the stylesheet, every template,
     both renderers, the Landform and Water builders and derivations.
  2. THE CALL COUNT through the report assembly: Landform and Water share
     ONE flow pass; the section is numbered from the outline.
  3. THE MAPS: both at Landform's extent and scale, measured; the
     hydrology plate in order (contours, the flood hatch, wetland tufts,
     flow paths, streams weighted by order and dashed for intermittent,
     springs) and clipped to what the frame shows; the marsh tufts and
     the hatch as geometry; the wetness ramp's four classes at the
     pipeline's breakpoints with the depressions; legends; colours only
     from TOKENS.
  4. THE TABLES: the surface-water rows; the water table's three states
     set differently (a depth, a muted bound, "no data" in words) in
     twelve columns, the four-month form when the parcel has more map
     units than the page holds; the comparison, land cover and flood
     tables summing to the cover's acreage; the land cover rows naming
     both extents; nine key figures with words flagged.
  5. NO SITING LANGUAGE anywhere in the section's words -- the reverse of
     Landform's grep.
  6. THE PAGES: nine (cover, Climate 2, Landform 3, Water 3), the three-
     page rule with continuation eyebrows, no box past the measure,
     decimal alignment across every Water table with dashes, "<0.1" and
     bounds included, the captions on the page; and a render with FEMA
     and NWI unavailable carrying the degraded statements at the same
     page count.
"""

import copy
import os
import re
from datetime import date
from unittest.mock import patch

from shapely.geometry import MultiLineString, Polygon, box

import offline_harness

offline_harness.install()

import landform_derivations  # noqa: E402
import landform_section  # noqa: E402
import report_chart  # noqa: E402
import report_layout  # noqa: E402
import report_map  # noqa: E402
import site_report  # noqa: E402
import valley_delineation  # noqa: E402
import water_derivations as wd  # noqa: E402
import water_reference_fixture as fixture  # noqa: E402
import water_section as ws  # noqa: E402
from landform_section import BELOW_PRECISION, ZERO_DASH  # noqa: E402
from site_report import TOKENS  # noqa: E402

GENERATED_ON = date(2026, 9, 22)
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _text(parts) -> str:
    if isinstance(parts, str):
        return parts
    return "".join(p if isinstance(p, str) else str(p["value"]) for p in parts)


def _cell_text(cell) -> str:
    return cell["value"] if isinstance(cell, dict) else cell


# ======================================================================
# 1. Colour literals
# ======================================================================
print("1. no colour literal outside site_report.TOKENS")
files = [report_map.__file__, report_chart.__file__, landform_section.__file__, landform_derivations.__file__,
         ws.__file__, wd.__file__]
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
# 2. The call count through the assembly
# ======================================================================
print("2. the report assembly runs ONE flow pass for Landform and Water together")
DATA = fixture.report_data()
with fixture.Harness():
    SESSION = fixture.Session()
    CONTEXT = SESSION.context()
    DOCUMENT = SESSION.stored()
    TERRAIN = landform_section.terrain_inputs_from_context(CONTEXT, DOCUMENT)
    INPUTS = wd.water_inputs_from_context(CONTEXT, DOCUMENT, DATA)
    with patch.object(valley_delineation, "fill_and_resolve", wraps=valley_delineation.fill_and_resolve) as fills, \
         patch.object(valley_delineation, "compute_flow_accumulation", wraps=valley_delineation.compute_flow_accumulation) as flows, \
         patch.object(valley_delineation, "delineate_valleys", wraps=valley_delineation.delineate_valleys) as valleys:
        SECTIONS = site_report.build_sections(DATA, TERRAIN, INPUTS)
    assert fills.call_count == 1 and flows.call_count == 1 and valleys.call_count == 0
    LANDFORM, SECTION = SECTIONS[1], SECTIONS[2]
    assert SECTION["derived"].flow is LANDFORM["derived"], "Water reads Landform's own flow pass"
assert [s["number"] for s in SECTIONS] == ["II", "III", "IV"] and SECTION["name"] == "Water & hydrology"
assert SECTION["template"] == "water.html" and SECTION["heading"] == "Water & hydrology"
print("   fill_and_resolve 1, compute_flow_accumulation 1, delineate_valleys 0; sections II, III, IV")

# ======================================================================
# 3. The maps
# ======================================================================
print("3. both maps at Landform's extent and scale; the plate in order; tufts and hatch as geometry")
DERIVED = SECTION["derived"]
for name in ("map", "wetness_map"):
    m = SECTION[name]
    assert m["meters_per_unit"] == LANDFORM["map"]["meters_per_unit"], name
    assert m["drawn_bbox"] == LANDFORM["map"]["drawn_bbox"] and m["scale_bar"] == LANDFORM["map"]["scale_bar"], name
    assert m["frame"] == LANDFORM["map"]["frame"]
    colours = set(re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{6})"', m["svg"]))
    assert colours <= set(TOKENS.values()), colours - set(TOKENS.values())
svg = SECTION["map"]["svg"]
order = [i for i in ("layer-contours", "layer-flood-zone", "layer-wetlands", "layer-flow-paths", "layer-streams-perennial",
                     "parcel-boundary") if f'id="{i}' in svg]
assert order == sorted(order, key=svg.index) and "layer-flood-zone" in svg and "layer-wetlands" in svg
assert svg.index('id="layer-contours"') < svg.index('id="layer-flood-zone"') < svg.index('id="layer-wetlands"') \
    < svg.index('id="layer-flow-paths"') < svg.index('id="layer-streams-perennial') < svg.index('id="parcel-boundary"')
assert "layer-streams-intermittent" not in svg, "the intermittent tributary lies beyond the frame"
# THE FRAME IS FITTED: no more than 50 m of ground beyond the bbox either side (report_map.MAX_CONTEXT_MARGIN_M), so
# Montour Run at 78 m is off the frame but for a stub at a corner, and the empty parcel says so ON the map.
assert SECTION["map"]["frame"][0] < report_map.FRAME_WIDTH_PT and SECTION["map"]["frame"] == LANDFORM["map"]["frame"]
assert '<g id="map-note">' in svg and "No mapped stream, waterbody, wetland" in svg and "or 1%-annual-chance flood zone on the parcel." in svg
note = ws.empty_parcel_note(DERIVED, INPUTS.boundary_polygon_utm)
assert note["lines"] == ["No mapped stream, waterbody, wetland", "or 1%-annual-chance flood zone on the parcel."]
assert INPUTS.boundary_polygon_utm.contains(note["point"])
# A parcel with a stream on it gets no note; one where NWI and FEMA were not read names only what was checked.
with_stream = copy.deepcopy(DERIVED.surface_water)
with_stream["streams"][0]["length_on_parcel_m"] = 12.0
assert ws.empty_parcel_note(wd.WaterDerived(**{**DERIVED.__dict__, "surface_water": with_stream}), INPUTS.boundary_polygon_utm) is None
assert "map-note" not in SECTION["wetness_map"]["svg"]
assert 'stroke-opacity="0.55"' in svg, "the contours are set back"
layers = ws.build_hydrology_layers(INPUTS, DERIVED, report_map.parcel_contours(INPUTS.dem, INPUTS.boundary_polygon_utm))
by_id = {l["id"]: l for l in layers}
assert by_id["streams-perennial-1"]["stroke_width"] == ws.STREAM_STROKE_BY_ORDER_PT[2] and by_id["streams-perennial-1"]["dash"] is None
assert by_id["streams-perennial-1"]["stroke"] == "water" and by_id["flood-zone"]["stroke"] == "water" and by_id["wetlands"]["stroke"] == "water"
assert by_id["flood-zone"]["stroke_opacity"] == ws.HATCH_OPACITY < 1 and by_id["flow-paths"]["stroke_opacity"] == ws.FLOW_PATH_OPACITY
# The hatch is the lightest thing on the map: fainter and wider-spaced than the tufts, which sit on top of it.
assert ws.HATCH_OPACITY <= 0.3 and ws.HATCH_SPACING_M >= 2 * ws.TUFT_SPACING_M and ws.HATCH_STROKE_PT < ws.TUFT_STROKE_PT
assert by_id["wetlands"]["stroke_opacity"] == 1.0
hatch_swatch = next(e["swatch"] for e in SECTION["map"]["legend"] if e["id"] == "flood-zone")
assert f'stroke-opacity="{ws.HATCH_OPACITY:.2f}"' in hatch_swatch, "the legend swatch is as faint as the hatch"
assert ws._stream_stroke(1) < ws._stream_stroke(2) < ws._stream_stroke(3) and ws._stream_stroke(None) == ws.STREAM_STROKE_UNKNOWN_PT
visible = box(*report_map.visible_extent_utm(INPUTS.boundary_polygon_utm))
for spec in layers:
    for geometry in spec["geometries"]:
        assert geometry.within(visible.buffer(0.01)), f"{spec['id']} draws outside the frame"
minx, miny, maxx, maxy = INPUTS.boundary_polygon_utm.bounds
assert visible.bounds[0] < minx and visible.bounds[2] > maxx, "the frame shows ground either side of the parcel"
# The tufts: short horizontal lines inside the polygon; the hatch: 45-degree lines inside it.
square = box(0, 0, 100, 100)
tufts = ws.marsh_tufts(square)
assert isinstance(tufts, MultiLineString) and len(tufts.geoms) > 50
for tuft in tufts.geoms:
    (x0, y0), (x1, y1) = tuft.coords
    assert y0 == y1 and abs(abs(x1 - x0) - ws.TUFT_LENGTH_M) < 1e-9 and square.contains(tuft)
rows = sorted({round(t.coords[0][1], 6) for t in tufts.geoms})
assert all(abs((b - a) - ws.TUFT_SPACING_M) < 1e-6 for a, b in zip(rows, rows[1:])), "rows at the spacing"
hatch = ws.hatch_lines(square)
assert isinstance(hatch, MultiLineString) and len(hatch.geoms) > 5
for line in hatch.geoms:
    (x0, y0), (x1, y1) = line.coords[0], line.coords[-1]
    assert abs((y1 - y0) - (x1 - x0)) < 1e-6 and square.buffer(1e-6).contains(line)
assert ws.marsh_tufts(None) is None and ws.hatch_lines(Polygon()) is None
# The wetness map: four classes at the breaks, the depressions, then the contours.
wet_svg = SECTION["wetness_map"]["svg"]
classes = [f"layer-wetness-{i}" for i in range(4)]
assert all(f'id="{c}"' in wet_svg for c in classes) and 'id="layer-depressions"' in wet_svg
assert wet_svg.index('id="layer-wetness-3"') < wet_svg.index('id="layer-depressions"') < wet_svg.index('id="layer-contours"')
breaks = ws.wetness_breaks(DERIVED.wetness)
assert breaks[1] == DERIVED.wetness["breakpoints"]["floor"] and breaks[3] == DERIVED.wetness["threshold"]
legend = [_text(e["parts"]) for e in SECTION["wetness_map"]["legend"]]
assert legend[0] == f"Wetness index below {breaks[1]:.1f}" and legend[3] == f"{breaks[3]:.1f} and above: wet ground by terrain"
assert legend[4] == "Depressions the flow model filled" and legend[5].startswith("Contours, ")
assert list(ws.WETNESS_TINT_OPACITY) == sorted(ws.WETNESS_TINT_OPACITY) and max(ws.WETNESS_TINT_OPACITY) < 0.6
hydrology_legend = [_text(e["parts"]) for e in SECTION["map"]["legend"]]
assert hydrology_legend == ["Contours, 10 ft", "FEMA flood zone, 1% annual chance", "Wetlands, NWI",
                            "Flow paths, from the elevation model", "Perennial streams"], hydrology_legend
print(f"   m/unit {SECTION['map']['meters_per_unit']:.4f} on all three maps; hydrology legend {hydrology_legend}")

# ======================================================================
# 4. The tables and the figures
# ======================================================================
print("4. the tables: three states in the water table; every acreage table sums to the cover")
COVER = round(INPUTS.parcel_acres, 1)
surface = SECTION["surface_water_table"]
assert surface["columns"] == ["Permanence", "Order", "On the parcel, ft", "Within 500 ft, ft", "Distance, ft"]
# Two rows: the reaches within 500 ft of the boundary. The tributary at 873 ft is not in a table headed "within 500 ft".
assert [r["label"] for r in surface["rows"]] == ["Montour Run", "Montour Run"]
assert surface["rows"][0]["cells"] == ["perennial", "2", ZERO_DASH, "785", "255"] and surface["rows"][1]["cells"][-1] == "490"
assert all(int(r["cells"][-1]) <= 500 for r in surface["rows"])
table = SECTION["water_table"]
assert table["monthly"] is True and len(table["columns"]) == 12 and len(table["rows"]) == 7 + 4
labels = [_text(r["label"]) for r in table["rows"]]
assert labels[0] == "Guernsey-Vandergrift, 4.4 ac" and labels[-4:] == ["Parcel, weighted, in", "With a water table, %",
                                                                        "Flooding, % of parcel", "Ponding, % of parcel"]
assert round(sum(table["unit_acres"]), 6) == COVER, "the map units partition the parcel"
guernsey = table["rows"][0]["cells"]
assert guernsey[0] == "19" and guernsey[6] == {"value": ">56", "kind": "bound"}, guernsey
assert all(isinstance(c, str) for c in table["rows"][5]["cells"]) and table["rows"][5]["cells"] == ["8"] * 12, "Atkins: a depth every month"
gilpin = table["rows"][4]["cells"]
assert all(c == {"value": ">30", "kind": "bound"} for c in gilpin), "Gilpin: deeper than 76 cm every month, a bound never a depth"
parcel_depth = table["rows"][7]["cells"]
assert parcel_depth[0] == "18" and parcel_depth[6] == "8", parcel_depth
assert table["rows"][8]["cells"][0] == "76.1" and table["rows"][8]["cells"][6] == "0.7"
assert table["rows"][9]["cells"][0] == "1.3" and table["rows"][9]["cells"][6] == ZERO_DASH
assert table["rows"][10]["cells"][0] == ZERO_DASH and table["rows"][10]["cells"][4] == "0.6"
# A map unit with no month rows reads "no data" in words, distinct from the bound.
stripped = copy.deepcopy(DATA.soil_water_table)
for cokey in stripped["map_units"]["541690"]["components"]:
    stripped["components"][cokey]["months"] = {}
    stripped["components"][cokey]["has_rows"] = False
no_rows = wd.derive(wd.WaterInputs(**{**INPUTS.__dict__, "soil_water_table": stripped}), DERIVED.flow)
no_rows_table = ws.build_water_table(no_rows)
gilpin_row = next(r for r in no_rows_table["rows"] if _text(r["label"]).startswith("Gilpin, 0.9"))
assert all(c == {"value": "no data", "kind": "word"} for c in gilpin_row["cells"])
# More map units than the page holds -> the four representative months, said in the caption.
with patch.object(ws, "WATER_TABLE_TWELVE_MONTH_MAX_UNITS", 3):
    four = ws.build_water_table(DERIVED)
    four_caption = _text(ws.build_water_table_caption(DERIVED))
assert four["columns"] == ["January", "April", "July", "October"] and four["monthly"] is False and four["compact"] is True
assert four["rows"][0]["cells"] == [guernsey[0], guernsey[3], guernsey[6], guernsey[9]]
assert "Four representative months" in four_caption and "Four representative months" not in _text(SECTION["water_table_caption"])
caption = _text(SECTION["water_table_caption"])
assert "is not a depth" in caption and "no data" in caption and "1:24,000" in caption
# The comparison, land cover and flood tables partition the parcel.
comparison = SECTION["comparison_table"]
assert [r["label"] for r in comparison["rows"]] == ["Terrain wetness only", "Hydric soil only", "Mapped wetland only",
                                                    "Two or more indicators", "None of the three", "Total"]
assert round(sum(comparison["acres"]), 6) == COVER and round(sum(comparison["shares"]), 6) == 100.0
assert comparison["rows"][-1]["cells"] == [f"{COVER:.1f}", "100.0"] and comparison["rows"][2]["cells"] == [ZERO_DASH, ZERO_DASH]
assert comparison["rows"][3]["cells"][0] == BELOW_PRECISION, "one cell of two indicators reads <0.1, never a dash"
land = SECTION["land_cover_table"]
assert land["columns"] == ["Forest", "Pasture and hay", "Developed"]
assert _text(land["rows"][0]["label"]) == "Contributing area within the window, 27.3 ac, %"
assert _text(land["rows"][1]["label"]) == "The parcel, 13.2 ac, %" and land["rows"][2]["label"] == "The parcel, acres"
assert round(sum(land["parcel_acres"]), 6) == COVER and round(sum(land["catchment_shares"]), 6) == 100.0
assert land["rows"][0]["cells"] == ["45.9", "48.8", "5.3"] and land["rows"][2]["cells"] == ["0.4", "11.8", "1.0"]
# A parcel wholly in one zone gets a sentence, not a two-row table saying the same thing twice.
assert SECTION["flood_table"] is None
flood_statement = _text(SECTION["flood_statement"])
assert flood_statement == ("The whole parcel lies in FEMA Zone X, an area of minimal flood hazard, on FIRM panel 42003C0065H "
                           "effective 26 September 2014.")
# Two zones on the parcel -> the table, a partition summing to the cover.
split = copy.deepcopy(DATA.fema_nfhl)
sx0, sy0, sx1, sy1 = INPUTS.boundary_polygon_utm.bounds
split["zones"].append({"zone": "AE", "subtype": None, "sfha": True, "study_type": "NP", "static_bfe": 1010.0, "dfirm_id": "42003C",
                       "fld_ar_id": "42003C_test", "geometry_utm": box(sx0, sy0, sx0 + (sx1 - sx0) / 2, sy1)})
two_zones = ws.build_water_section(wd.WaterInputs(**{**INPUTS.__dict__, "fema_nfhl": split}), TOKENS, flow=LANDFORM["derived"])
flood = two_zones["flood_table"]
assert two_zones["flood_statement"] is None and [r["label"] for r in flood["rows"]] == ["Zone AE", "Zone X, area of minimal flood hazard", "Total"]
assert round(sum(flood["acres"]), 6) == COVER and flood["rows"][-1]["cells"] == [f"{COVER:.1f}", "100.0"] and all(a > 0 for a in flood["acres"])
assert "layer-flood-zone" in two_zones["map"]["svg"] and "map-note" not in two_zones["map"]["svg"], "a zone on the parcel is hatched, and the parcel is not empty"
figures = SECTION["key_figures"]
assert len(figures) == 9 and [f["label"] for f in figures] == [
    "to the nearest mapped stream", "hydric soil, predominantly", "mapped wetland on the parcel", "wet ground by terrain",
    "with a water table in January", "stream catchment at the reach", "contributing area in the window, at least",
    "of it beyond the parcel", "FEMA flood zone, whole parcel"]
assert figures[0]["value"] == "255 ft" and figures[2] == {"value": "None", "label": "mapped wetland on the parcel", "word": True}
assert figures[5]["value"] == "532 ac" and figures[6]["value"] == "27.3 ac" and figures[8]["value"] == "Zone X" and figures[8]["word"]
assert all(re.match(r"^[\d,.]+( ft| ac|%)$", f["value"]) for f in figures if not f.get("word")), "measurements in the data face only"
summary = _text(SECTION["summary"])
assert summary == ("No mapped stream crosses the parcel; Montour Run, perennial and order 2, runs 255 ft beyond the boundary. "
                   "The soil survey maps 0.1 acres as hydric and terrain wetness marks 1.1 acres; the whole parcel lies in FEMA Zone X.")
assert {"value": "255 ft"} in SECTION["summary"] and {"value": "0.1"} in SECTION["summary"]
assert _text(SECTION["map_caption"]).endswith(ws.NWI_CAVEAT) and "2023" in _text(SECTION["map_caption"])
assert _text(SECTION["surface_water_caption"]).endswith(ws.NHD_CAVEAT) and "No spring or seep is mapped" in _text(SECTION["surface_water_caption"])
assert _text(SECTION["flood_caption"]).endswith(ws.FEMA_CAVEAT) and "Zone A" in _text(SECTION["flood_caption"])
assert _text(SECTION["wetness_caption"]).endswith(ws.TWI_CAVEAT) and "7.2" in _text(SECTION["wetness_caption"])
land_caption = _text(SECTION["land_cover_caption"])
assert "not a comparison of the parcel with its surroundings" in land_caption and "lower bound" in land_caption and "532" in land_caption
assert "does not resolve ground this size" in _text(SECTION["comparison_caption"])
assert len(SECTION["sources"]) == 7 and all(len(line) == 1 for line in SECTION["sources"])
assert [m["source"] for m in SECTION["methods"]] == ["USGS NHD", "USGS NHDPlus HR", "USDA NRCS SSURGO", "USFWS NWI", "FEMA NFHL",
                                                     "USGS Annual NLCD", "USGS 3DEP"]
assert all(m["terms"] for m in SECTION["methods"]) and "Use_Constraints" in SECTION["methods"][3]["terms"]
print(f"   water table {len(table['rows'])} rows x 12; comparison {comparison['acres']}; land cover parcel {land['parcel_acres']}; "
      f"flood: a sentence for one zone, a table {flood['acres']} for two")

# ======================================================================
# 5. No siting language
# ======================================================================
print("5. the section describes the water that is there: no siting language")
words = " ".join([
    summary, _text(SECTION["map_caption"]), _text(SECTION["surface_water_caption"]), _text(SECTION["wetness_caption"]),
    _text(SECTION["water_table_caption"]), _text(SECTION["comparison_caption"]), _text(SECTION["land_cover_caption"]),
    _text(SECTION["flood_caption"]), _text(SECTION["flood_unavailable"]), _text(SECTION["land_cover_unavailable"]),
    _text(SECTION["water_table_unavailable"]),
    " ".join(f["label"] + " " + f["value"] for f in figures),
    " ".join(_text(e["parts"]) for e in SECTION["map"]["legend"] + SECTION["wetness_map"]["legend"]),
    " ".join(" ".join([_text(r["label"])] + [_cell_text(c) for c in r["cells"]]) for t in
             (surface, table, comparison, land, flood) for r in t["rows"]),
    " ".join(" ".join(t["columns"]) + " " + t["corner"] for t in (surface, table, comparison, land, flood)),
    " ".join(_text(line) for line in SECTION["sources"]), SECTION["heading"], flood_statement,
    " ".join(note["lines"]),
]).lower()
for banned in (r"\bsit(e|es|ing|ed)\b", r"\bsurvey (area|zone)s?\b", r"\bponds?\b", r"\bdams?\b", r"\bstorage\b", r"\bembankment",
               r"\bexcavat", r"\bcandidate", r"\bsuitab", r"\brecommend", r"\bshould\b", r"\bbuild", r"\bpropos", r"\bkeyline",
               r"\brunoff\b", r"\bcurve number", r"\bvolume", r"\bacre-f(oo|ee)t", r"\breservoir"):
    assert not re.search(banned, words), (banned, re.search(banned, words).group(0))
assert "ponding" in words, "the SSURGO term survives the grep: ponding is a rating, not a pond"
print("   19 patterns, none found; 'ponding' stays")

# ======================================================================
# 6. The pages
# ======================================================================
print("6. the pages: nine, the three-page rule, no overflow, decimal alignment, the degraded render")
from weasyprint import HTML  # noqa: E402

html = site_report.render_site_report_html(DATA, generated_on=GENERATED_ON, terrain=TERRAIN, water=INPUTS)
assert html.count("Water &amp; hydrology, continued") == 2 and 'class="section section--water section--map"' in html
assert html.index('class="section section--landform') < html.index('class="section section--water')
assert set(HEX.findall(html)) == set(TOKENS.values())
assert 'class="num num--bound">&gt;56<' in html and 'class="data-table data-table--monthly"' in html
document = HTML(string=html, base_url=site_report.TEMPLATES_DIRECTORY).render()
pages = document.pages
assert len(pages) == 9, len(pages)
assert report_layout.overflowing_boxes(document) == [], report_layout.overflowing_boxes(document)


def _walk(box):
    yield box
    for child in getattr(box, "children", []) or []:
        yield from _walk(child)


def _classes_on(page):
    return {
        (b.element.get("class") or "").split()[0]
        for b in _walk(page._page_box) if getattr(b, "element", None) is not None and b.element.get("class")
    }


def _tables(page):
    return [b for b in _walk(page._page_box) if type(b).__name__ == "TableBox"]


def _numeric_cells(box):
    cells = []
    for child in _walk(box):
        if type(child).__name__ == "TableCellBox" and child.element_tag == "td" and "num" in (child.element.get("class") or ""):
            text = "".join(child.element.itertext()).strip()
            if text:
                cells.append((round(child.position_x + child.width, 3), text, child))
    return cells


# The hydrology page: heading, summary, the map, the surface-water table; the wetness page: a second map, the
# monthly table, no figures; the numbers: the panel, three tables, the footer, no map.
assert {"report-map", "summary", "heading", "eyebrow", "data-table", "caption"} <= _classes_on(pages[6])
assert not ({"key-figures", "source-footer"} & _classes_on(pages[6]))
assert {"report-map", "data-table", "eyebrow", "caption"} <= _classes_on(pages[7]) and not ({"key-figures", "heading", "summary"} & _classes_on(pages[7]))
assert {"key-figures", "data-table", "source-footer", "eyebrow", "caption"} <= _classes_on(pages[8]) and "report-map" not in _classes_on(pages[8])
assert len(_tables(pages[6])) == 1 and len(_tables(pages[7])) == 1 and len(_tables(pages[8])) == 2
assert "unavailable" in _classes_on(pages[8]), "the one-zone flood sentence stands where the table would"
water_tables = _tables(pages[6]) + _tables(pages[7]) + _tables(pages[8])
expected_columns = (5, 12, 2, 3)
advances = set()
for table_box, columns in zip(water_tables, expected_columns):
    cells = _numeric_cells(table_box)
    edges = {right for right, _, _ in cells}
    assert len(edges) == columns, (columns, sorted(edges))
    for right, text, cell in cells:
        if text in (ZERO_DASH, BELOW_PRECISION) or text.startswith(">") or text[0].isalpha():
            continue
        text_boxes = [b for b in _walk(cell) if type(b).__name__ == "TextBox"]
        advances.add(round(sum(b.width for b in text_boxes) / len(text), 2))
        if "." in text:
            assert len(text) - text.index(".") == 2, text
assert len(advances) == 1, sorted(advances)
monthly_cells = _numeric_cells(water_tables[1])
assert sum(1 for _, text, _ in monthly_cells if text.startswith(">")) == 12 * 3 + 5 + 6 + 4, "the bounds on the page"
assert any(text == ZERO_DASH for _, text, _ in monthly_cells)
bound_cells = [cell for _, text, cell in monthly_cells if text.startswith(">")]
assert all("num--bound" in (c.element.get("class") or "") for c in bound_cells)
flat = "".join("".join(b.text for b in _walk(p._page_box) if type(b).__name__ == "TextBox") for p in pages[6:9])
squash = "".join(flat.split())
for caption in (ws.NWI_CAVEAT, ws.NHD_CAVEAT, ws.TWI_CAVEAT, ws.FEMA_CAVEAT):
    assert "".join(caption.split()) in squash, caption[:40]
assert "".join("not a comparison of the parcel with its surroundings".split()) in squash
assert "IV·WATER&HYDROLOGY,CONTINUED" in squash.upper().replace(" ", "")
# Landform and Climate intact ahead of it: six pages before Water, the Landform maps at the same scale.
assert 'class="section section--landform section--map"' in html and len(_numeric_cells(pages[2]._page_box)) == 108 + 6
# THE DEGRADED RENDER: FEMA and NWI unavailable -> the statements, no hatch, no tufts, the same page count.
degraded_data = fixture.report_data(nwi=None, fema_nfhl=None, unavailable={
    "nwi": {"label": "mapped wetlands", "reason": "source_unavailable", "error": "down"},
    "fema_nfhl": {"label": "flood hazard zones", "reason": "source_unavailable", "error": "down"},
})
degraded_inputs = wd.water_inputs_from_context(CONTEXT, DOCUMENT, degraded_data)
degraded_section = ws.build_water_section(degraded_inputs, TOKENS, flow=LANDFORM["derived"])
assert degraded_section["flood_table"] is None and "did not answer" in _text(degraded_section["flood_unavailable"])
assert "layer-flood-zone" not in degraded_section["map"]["svg"] and "layer-wetlands" not in degraded_section["map"]["svg"]
assert _text(degraded_section["map_caption"]).startswith("The National Wetlands Inventory did not answer")
assert [r["label"] for r in degraded_section["comparison_table"]["rows"]] == ["Terrain wetness only", "Hydric soil only",
                                                                               "Two or more indicators", "Neither", "Total"]
assert round(sum(degraded_section["comparison_table"]["acres"]), 6) == COVER
assert degraded_section["key_figures"][2] == {"value": "Unavailable", "label": "mapped wetland on the parcel", "word": True}
assert degraded_section["key_figures"][8] == {"value": "Unavailable", "label": "FEMA flood zone", "word": True}
assert "map-note" in degraded_section["map"]["svg"] and "No mapped stream</text>" in degraded_section["map"]["svg"]
assert "wetland" not in ws.empty_parcel_note(degraded_section["derived"], INPUTS.boundary_polygon_utm)["lines"][0]
assert _text(degraded_section["summary"]).endswith("terrain wetness marks 1.1 acres.")
assert len(degraded_section["sources"]) == 5
degraded_html = site_report.render_site_report_html(degraded_data, generated_on=GENERATED_ON, terrain=TERRAIN, water=degraded_inputs)
degraded_document = HTML(string=degraded_html, base_url=site_report.TEMPLATES_DIRECTORY).render()
assert len(degraded_document.pages) == 9 and report_layout.overflowing_boxes(degraded_document) == []
assert "unavailable" in _classes_on(degraded_document.pages[8])
# A parcel with no digital flood map: the statement in those words.
unstudied = copy.deepcopy(DATA.fema_nfhl)
unstudied.update({"available": False, "study_ids": [], "zones": [], "panels": []})
no_map = ws.build_water_section(wd.WaterInputs(**{**INPUTS.__dict__, "fema_nfhl": unstudied}), TOKENS, flow=LANDFORM["derived"])
assert no_map["flood_table"] is None and _text(no_map["flood_unavailable"]).startswith("No digital flood map covers this parcel")
assert no_map["key_figures"][8] == {"value": "Not mapped", "label": "FEMA flood zone", "word": True}
assert _text(no_map["summary"]).endswith("no digital flood map covers the parcel.")
print(f"   9 pages; Water tables {expected_columns} columns aligned, one advance {advances.pop()} pt; degraded 9 pages, no overflow")

print("\ntest_water_section.py: all sections passed")
print(offline_harness.summary())
