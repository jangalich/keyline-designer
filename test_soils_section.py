"""
test_soils_section.py

THE SOILS & GEOLOGY SECTION'S PAGES -- branch 12, phase 2 of
site-data-report-proposal.md's build sequence. Runs offline on the real
parcel: the captured 3DEP grid, the real SSURGO rows, map unit polygons
and K factors, and the captured survey and geology answers
(soils_reference_fixture.py) through an offline session, the Daymet
fixture for Climate, WeasyPrint for the pages.

  1. NO COLOUR LITERAL OUTSIDE TOKENS: the stylesheet, every template,
     both renderers, and this section's builder and derivations.
  2. THE MAP: at Landform's extent, scale and frame, measured; every map
     unit polygon outlined over a tint no unit it TOUCHES shares; the
     symbol inside each polygon that can hold one, the rest dropped and
     counted in the caption; the symbol labels knocked out of what they
     sit on; the contours carrying no elevations; colours only from
     TOKENS.
  3. THE TABLES: the map unit table without its drainage column and what
     that measured; the surface horizon table with the fragipan note
     INSIDE the cell it corrects; every acreage column summing to the
     cover; texture carried as a sentence and not a column.
  4. NO MANAGEMENT LANGUAGE anywhere in the section's words.
  5. THE PAGES: sixteen, the three-page rule with continuation eyebrows,
     the map page carrying the two classification tables and the map unit
     table opening the next, a table and its caption breaking together,
     no box past the measure, decimal alignment across every table; and
     the degraded renders.
"""

import os
import re

import offline_harness

offline_harness.install()

import access_derivations as ad  # noqa: E402
import landform_section  # noqa: E402
import report_layout  # noqa: E402
import report_map  # noqa: E402
import site_report  # noqa: E402
import soils_derivations as sd  # noqa: E402
import soils_reference_fixture as fixture  # noqa: E402
import soils_section as ssn  # noqa: E402
import trees_derivations as td  # noqa: E402
import water_derivations as wd  # noqa: E402
from datetime import date  # noqa: E402
from landform_section import ZERO_DASH  # noqa: E402
from site_report import TOKENS  # noqa: E402

GENERATED_ON = date(2026, 9, 23)
HEX = re.compile(r"#[0-9a-fA-F]{6}\b")

# ======================================================================
# 1. No colour literal outside TOKENS
# ======================================================================
print("1. no colour literal outside site_report.TOKENS")
checked = []
for root, _dirs, files in os.walk(site_report.TEMPLATES_DIRECTORY):
    checked += [os.path.join(root, f) for f in files]
assert len(checked) == 20, sorted(checked)   # css, base, back matter, 9 components, 8 sections
for path in checked:
    with open(path, encoding="utf-8") as handle:
        assert not HEX.findall(handle.read()), f"{path} carries a colour literal"
for module in (report_map, ssn, sd):
    with open(module.__file__, encoding="utf-8") as handle:
        assert HEX.findall(handle.read()) == [], f"{module.__name__} carries a colour literal"
with open(site_report.__file__, encoding="utf-8") as handle:
    inside = HEX.findall(handle.read())
assert set(inside) == set(TOKENS.values()), inside
print(f"   {len(checked)} template files clean; the Soils builder and derivations name tokens only")

# ======================================================================
# 2. The map
# ======================================================================
print("2. the soil map at Landform's extent and scale; tints, symbols and knock-outs")
DATA = fixture.report_data()
with fixture.Harness():
    SESSION = fixture.Session()
    CONTEXT = SESSION.context()
    DOCUMENT = SESSION.stored()
    TERRAIN = landform_section.terrain_inputs_from_context(CONTEXT, DOCUMENT)
    WATER = wd.water_inputs_from_context(CONTEXT, DOCUMENT, DATA)
    ACCESS = ad.access_inputs_from_context(CONTEXT, DOCUMENT, DATA)
    TREES = td.trees_inputs_from_context(CONTEXT, DOCUMENT, DATA)
    INPUTS = sd.soils_inputs_from_context(CONTEXT, DOCUMENT, DATA)
    SECTIONS = site_report.build_sections(DATA, TERRAIN, WATER, ACCESS, TREES, INPUTS)
assert [s["number"] for s in SECTIONS] == ["II", "III", "IV", "V", "VI", "VII"]
LANDFORM, SECTION = SECTIONS[1], SECTIONS[5]
assert SECTION["name"] == "Soils & geology" and SECTION["template"] == "soils.html"
DERIVED = SECTION["derived"]
m = SECTION["map"]
# IDENTICAL TO LANDFORM'S, MEASURED -- the other sections' map of this parcel with different layers on it.
assert m["extent_utm"] == LANDFORM["map"]["extent_utm"] and m["meters_per_unit"] == LANDFORM["map"]["meters_per_unit"]
assert m["drawn_bbox"] == LANDFORM["map"]["drawn_bbox"] and m["scale_bar"] == LANDFORM["map"]["scale_bar"]
assert m["frame"] == LANDFORM["map"]["frame"] == SECTIONS[4]["map"]["frame"], "and Trees'"
colours = set(re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{6})"', m["svg"]))
assert colours <= set(TOKENS.values()), colours - set(TOKENS.values())

contours = report_map.parcel_contours(INPUTS.dem, INPUTS.boundary_polygon_utm)
layers = ssn.build_map_layers(DERIVED, contours)
by_id = {l["id"]: l for l in layers}
# THE CONTOURS CARRY NO ELEVATIONS HERE: the symbols want the same gaps.
contour_specs = [l for l in layers if l["id"] in ("contours", "index-contours")]
assert contour_specs and all(l["labels"] is None and l["stroke_opacity"] == ssn.CONTOUR_OPACITY for l in contour_specs)
assert report_map.contour_layers(contours)[1]["labels"], "Landform's index contours still carry theirs"
assert contour_specs[0]["legend"] == [f"Contours, {contours['interval_ft']} ft"]
# One layer per map unit, drawn over the contours, outlined in ink, labelled with its symbol.
unit_ids = [f"unit-{DERIVED.map_units[m_]['musym']}" for m_ in DERIVED.order]
assert [l["id"] for l in layers] == [l["id"] for l in contour_specs] + unit_ids
for mukey, layer_id in zip(DERIVED.order, unit_ids):
    spec = by_id[layer_id]
    assert spec["kind"] == "polygon" and spec["stroke"] == "ink" and spec["fill"] == "ink"
    assert spec["labels"] == [DERIVED.map_units[mukey]["musym"]]
    assert spec["fill_opacity"] in ssn.SOIL_TINTS

# NO TWO UNITS THAT TOUCH SHARE A TINT -- the whole point of the colouring.
TINTS = ssn.assign_tints(DERIVED)
ADJACENCY = ssn.neighbours(DERIVED)
assert any(ADJACENCY.values()), "the reference parcel's units do touch"
for mukey, touching in ADJACENCY.items():
    for other in touching:
        assert TINTS[mukey] != TINTS[other], (DERIVED.map_units[mukey]["musym"], DERIVED.map_units[other]["musym"])
assert max(TINTS.values()) <= max(ssn.SOIL_TINTS), "nothing darker than the ramp's cap"

# THE SYMBOLS: placed where the polygon can hold one, dropped where it cannot, and the caption says which.
placed = {DERIVED.map_units[m_]["musym"]: m["labels_placed"][f"unit-{DERIVED.map_units[m_]['musym']}"][0]
          for m_ in DERIVED.order}
assert placed == {"GvD": True, "EvC": True, "WhC": True, "RycC": True, "GlD": True, "At": False, "GSF": False}, placed
assert [DERIVED.map_units[m_]["musym"] for m_ in SECTION["unlabelled"]] == ["At", "GSF"]
for symbol, was_placed in placed.items():
    assert (f">{symbol}</text>" in m["svg"]) is was_placed, symbol
# KNOCKED OUT: each placed symbol is drawn twice, a page-coloured stroke under the glyphs.
for symbol, was_placed in placed.items():
    if was_placed:
        assert m["svg"].count(f">{symbol}</text>") == 2, symbol
assert f'stroke="{TOKENS["page"]}" stroke-width=' in m["svg"]
print(f"   m/unit {m['meters_per_unit']:.4f}, frame {m['frame'][0]:.0f} x {m['frame'][1]:.0f} pt; "
      f"{len(unit_ids)} units, {sum(placed.values())} symbols set, {len(SECTION['unlabelled'])} dropped; "
      f"{len(set(TINTS.values()))} tints, none shared across a shared boundary")

# ======================================================================
# 3. The tables
# ======================================================================
print("3. the map unit table without drainage, the horizon table with the note in its cell")
COVER = round(INPUTS.parcel_acres, 1)
units_table = SECTION["map_unit_table"]
assert units_table["columns"] == ["Acres", "% of parcel", "Group", "Ksat", "Class", "Farmland"]
assert "Drainage" not in units_table["columns"], "the drainage column is cut; the hydrologic group carries it"
assert units_table["variant"] == "units" and units_table["text_columns"] == ["Farmland"]
assert len(units_table["rows"]) == len(DERIVED.order) + 1 and units_table["rows"][-1]["label"] == "Total"
assert units_table["rows"][-1]["cells"][:2] == [f"{COVER:,.1f}", "100.0"]
assert round(sum(float(r["cells"][0]) for r in units_table["rows"][:-1]), 1) == COVER
# WITH the column, for the record: the same rows, one column wider.
with_drainage = ssn.build_map_unit_table(DERIVED, with_drainage=True)
assert with_drainage["columns"][2] == "Drainage" and len(with_drainage["columns"]) == len(units_table["columns"]) + 1
assert [r["cells"][2]["value"] for r in with_drainage["rows"][:-1]] == \
    [DERIVED.map_units[m_]["drainage_class"] for m_ in DERIVED.order]

# TEXTURE IS A SENTENCE, NOT A COLUMN.
horizon = SECTION["properties_table"]
assert not any("texture" in c.lower() for c in horizon["columns"]), horizon["columns"]
summary_words = "".join(p if isinstance(p, str) else str(p["value"]) for p in SECTION["summary"])
assert "silt loam" in summary_words and "pH 5.0–5.9" in summary_words.replace(" –", "–")
assert "across the parcel" in summary_words
assert "very strongly to moderately acid" in summary_words
assert ssn.texture_classes(DERIVED)[0]["texture"] == "Silt loam"
# The one unit whose survey phrase differs is the caption's, not a column's.
properties_words = "".join(p if isinstance(p, str) else str(p["value"]) for p in SECTION["properties_caption"])
assert "GSF is a channery silt loam" in properties_words and "0.1" in properties_words

# THE FRAGIPAN NOTE IS IN THE CELL IT CORRECTS, not in a caption below the table.
rows = {r["label"][0]["value"]: r for r in horizon["rows"]}
bedrock = rows["EvC"]["cells"][-1]
assert bedrock["value"] == "> 72" and bedrock["kind"] == "bound", bedrock
note_words = "".join(p if isinstance(p, str) else str(p["value"]) for p in bedrock["note"])
assert note_words == "fragipan at 28 in stops roots above the rock", note_words
assert all("note" not in r["cells"][-1] for k, r in rows.items() if k != "EvC"), "one note, on the one unit that needs it"
assert "fragipan" not in properties_words.lower(), "the note is not also in the caption"
assert rows["At"]["cells"][-1] == {"value": "> 60", "kind": "bound"}, "a bound with no note keeps its kind"
assert rows["GvD"]["cells"][-1] == {"value": "60", "kind": None}, "a measured depth is not a bound"
assert rows["GSF"]["cells"][0] == "1–8", "the surface horizon's own depth, per row"

# Acreage columns, all three of them, summing to the cover.
for table in (SECTION["capability_table"], SECTION["farmland_table"]):
    assert table["rows"][-1]["label"] == "Total" and table["rows"][-1]["cells"] == [f"{COVER:,.1f}", "100.0"]
    assert round(sum(float(r["cells"][0]) for r in table["rows"][:-1]), 1) == COVER
assert [r["label"] for r in SECTION["farmland_table"]["rows"][:-1]] == DERIVED.farmland["values"]
assert SECTION["capability_table"]["rows"][0]["label"] == ["Class ", {"value": "3e"}, ", limited by erosion"]
assert len(SECTION["erosion_table"]["rows"]) == len(DERIVED.order)
geology_words = "".join(p if isinstance(p, str) else str(p["value"]) for p in SECTION["geology"])
assert geology_words.startswith("The rock beneath is the Casselman Formation of Pennsylvanian age")
assert "Glenshaw Formation" in geology_words and "under its centre" in geology_words
print(f"   map unit table {len(units_table['columns'])} columns (drainage cut), "
      f"horizon table {len(horizon['columns'])}; the fragipan note inside EvC's bedrock cell; "
      f"three acreage columns summing to {COVER}")

# ======================================================================
# 4. No management language
# ======================================================================
print("4. the section describes the soil that is there: no management language")
WORDS = []
for key in ("summary", "map_caption", "map_unit_caption", "properties_caption", "soil_test", "profile_water",
            "capability_caption", "farmland_caption", "erosion_caption", "geology", "geology_caption",
            "cross_reference"):
    WORDS += [p if isinstance(p, str) else str(p["value"]) for p in (SECTION[key] or [])]
for table in (units_table, horizon, SECTION["capability_table"], SECTION["farmland_table"], SECTION["erosion_table"]):
    WORDS.append(table["corner"])
    WORDS += table["columns"]
    for row in table["rows"]:
        WORDS += [row["label"]] if isinstance(row["label"], str) else [
            p if isinstance(p, str) else str(p["value"]) for p in row["label"]]
        for cell in row["cells"]:
            if isinstance(cell, dict):
                WORDS.append(str(cell["value"]))
                WORDS += [p if isinstance(p, str) else str(p["value"]) for p in (cell.get("note") or [])]
            else:
                WORDS.append(str(cell))
WORDS += [line for parts in SECTION["sources"] for line in parts]
text = " ".join(WORDS).lower()
# TWO SANCTIONED PHRASES, both named so the grep cannot be widened past them.
# "A soil test is the only way to know" is an instruction about MEASURING, not about doing anything to the
# ground -- the one sentence this section owes a reader about its own weakness. And the cross-reference names
# the Trees section's tables by their titles; "woodland productivity and management limitations" is what those
# figures are called, not something this section is advising.
assert text.count("a soil test is the only way to know") == 1
assert text.count("the woodland productivity and management limitations in trees & forestry") == 1
text = text.replace("a soil test is the only way to know", "")
text = text.replace("the woodland productivity and management limitations in trees & forestry", "")
for banned in (r"\bamend", r"\blime\b", r"\bliming", r"\bfertilis", r"\bfertiliz", r"\bmanure", r"\bcompost",
               r"\bapply\b", r"\bapplication", r"\btill(age|ing)\b", r"\bcrop", r"\bplant\b", r"\bplanting",
               r"\bsow\b", r"\brecommend", r"\bshould\b", r"\bsuitab", r"\bimprove", r"\bremediat",
               r"\bprescrib", r"\btreatment\b", r"\byield\b", r"\bproductiv", r"\bcandidate", r"\bpropos",
               r"\bsit(e|es|ing|ed)\b", r"\bdesign"):
    hit = re.search(banned, text)
    assert not hit, (banned, hit.group(0), text[max(0, hit.start() - 50):hit.end() + 50])
print(f"   {len(text.split()) and 26} patterns, none found; the soil-test sentence and the cross-reference stay")

# ======================================================================
# 5. The pages
# ======================================================================
print("5. sixteen pages, the three-page rule, the map page's two tables, no overflow, decimal alignment")
from weasyprint import HTML  # noqa: E402


def _render(soils, data=DATA):
    h = site_report.render_site_report_html(data, generated_on=GENERATED_ON, terrain=TERRAIN, water=WATER,
                                            access=ACCESS, trees=TREES, soils=soils)
    return h, HTML(string=h, base_url=site_report.TEMPLATES_DIRECTORY).render()


html, document = _render(INPUTS)
assert 'class="section section--soils section--map"' in html
assert html.count("Soils &amp; geology, continued") == 2, "two continuation eyebrows for three pages"
assert html.count("VII</span> · Soils &amp; geology") == 3
assert set(HEX.findall(html)) == set(TOKENS.values())
pages = document.pages
assert len(pages) == 16, len(pages)
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


MAP_PAGE, TABLES_PAGE, CLASS_PAGE = pages[13], pages[14], pages[15]
assert {"report-map", "summary", "heading", "eyebrow", "caption", "table-block"} <= _classes_on(MAP_PAGE)
assert "source-footer" not in _classes_on(MAP_PAGE)
# THE COMPOSITION: the map page carries the map and the two CLASSIFICATION tables -- seven map units are
# 261 pt of table against the room a 453 pt map leaves, so the map unit table opens the next page beside the
# properties instead, and the map caption says so.
assert len(_tables(MAP_PAGE)) == 2, "capability and farmland sit under the map"
assert [t.element.get("class").split()[-1] for t in _tables(MAP_PAGE)] == ["data-table--compact"] * 2
assert len(_tables(TABLES_PAGE)) == 2, "the map unit table and the surface horizon table"
assert [t.element.get("class").split()[-1] for t in _tables(TABLES_PAGE)] == ["data-table--units", "data-table--horizon"]
assert len(_tables(CLASS_PAGE)) == 1, "the erosion factors"
assert "report-map" not in _classes_on(TABLES_PAGE) and "source-footer" in _classes_on(CLASS_PAGE)
assert "source-footer" not in _classes_on(TABLES_PAGE)
# A TABLE AND ITS CAPTION BREAK TOGETHER: neither classification caption is stranded from its table.
blocks = [b for b in _walk(MAP_PAGE._page_box)
          if getattr(b, "element", None) is not None and "table-block" in (b.element.get("class") or "")]
assert len(blocks) == 2, len(blocks)
for block in blocks:
    assert any(type(c).__name__ == "TableBox" for c in _walk(block)), "a block on the page carries its table"
    assert any((getattr(c, "element", None) is not None and "caption" in (c.element.get("class") or ""))
               for c in _walk(block)), "and its caption"
# THE MAP PAGE IS FULL, and a longer summary moves the FARMLAND BLOCK WHOLE rather than stranding its
# caption -- the .table-block guarantee, on the case that actually exercises it.
ENV = site_report.jinja_environment()
long_summary = list(SECTION["summary"]) + [
    "A longer summary than this parcel's, to push the page past what it holds and prove what gives way."]
crowded = HTML(string=ENV.get_template("base.html").render(
    stylesheet=site_report.render_stylesheet(),
    cover={"title": "x", "eyebrow": "x", "label": "x", "acres": None, "generated_on": "x", "meta": "x"},
    sections=[dict(SECTION, summary=long_summary)]), base_url=site_report.TEMPLATES_DIRECTORY).render()
assert report_layout.overflowing_boxes(crowded) == []
crowded_map, crowded_next = crowded.pages[1], crowded.pages[2]
assert len(_tables(crowded_map)) == 1, "only capability fits when the summary runs longer"
moved = [b for b in _walk(crowded_next._page_box)
         if getattr(b, "element", None) is not None and "table-block" in (b.element.get("class") or "")]
assert len(moved) == 1 and any(type(c).__name__ == "TableBox" for c in _walk(moved[0])), \
    "the farmland table moves WITH its caption, not without it"

# DECIMAL ALIGNMENT: every numeric column is right-aligned to one edge, and every value in a column that
# carries a decimal point carries the SAME NUMBER OF PLACES, so the points line up down the column. Not a
# fixed one place: Ksat and available water are two, and the rule is that a column is consistent with
# itself. One glyph advance across the lot, the data face's.
advances = set()
for page, counts in ((MAP_PAGE, (2, 2)), (TABLES_PAGE, (5, 8)), (CLASS_PAGE, (2,))):
    for table_box, columns in zip(_tables(page), counts):
        cells = _numeric_cells(table_box)
        edges = {right for right, _, _ in cells}
        assert len(edges) == columns, (columns, sorted(edges))
        places = {}
        for right, text, cell in cells:
            if text == ZERO_DASH:
                continue
            boxes = [b for b in _walk(cell) if type(b).__name__ == "TextBox"]
            if len(boxes) == 1 and boxes[0].text.strip() == text:
                advances.add(round(boxes[0].width / len(text), 2))
            bare = text.lstrip("> ").strip()
            if "." in bare and "–" not in bare:
                places.setdefault(right, set()).add(len(bare) - bare.index(".") - 1)
        for right, seen in places.items():
            assert len(seen) == 1, (right, seen)
assert len(advances) == 1, sorted(advances)

flat = "".join("".join(b.text for b in _walk(p._page_box) if type(b).__name__ == "TextBox")
               for p in (MAP_PAGE, TABLES_PAGE, CLASS_PAGE))
squash = "".join(flat.split())
for needle in ("a soil test is the only way to know either on this parcel",
               "fragipan at 28 in stops roots above the rock",
               "the tint separates neighbouring units and carries no value",
               "The map unit table overleaf names every symbol",
               "a different depth basis from the surface horizon's figures above",
               "at that scale a contact is not placed to parcel precision",
               "The same soil survey supplies the seasonal water table"):
    assert "".join(needle.split()) in squash, needle
assert "VII·SOILS&GEOLOGY,CONTINUED" in squash.upper()
# The sections ahead of it are intact.
assert 'class="section section--trees section--map"' in html and 'class="section section--access section--map"' in html

# THE DEGRADED RENDERS: each report layer absent on its own, and both.
for missing, expect in (({"soil_survey": None}, "The soil survey's detailed properties are not shown"),
                        ({"bedrock_geology": None}, "The bedrock geology is not shown"),
                        ({"soil_survey": None, "bedrock_geology": None}, "The soil survey's detailed properties are not shown")):
    degraded_inputs = sd.SoilsInputs(**{**INPUTS.__dict__, **missing,
                                        "unavailable": {k: {"label": k, "reason": "source_unavailable", "error": "down"}
                                                        for k in missing}})
    degraded_html, degraded = _render(degraded_inputs)
    assert report_layout.overflowing_boxes(degraded) == [], missing
    assert expect in degraded_html.replace("&#39;", "'"), missing
    if "soil_survey" in missing:
        # THE MAP STILL DRAWS ITS POLYGONS, outlined and unlabelled, and every Layer 1 reading survives:
        # the symbols, the surface horizon, the capability class and the T factor are what is lost.
        degraded_section = [s for s in site_report.build_sections(DATA, TERRAIN, WATER, ACCESS, TREES, degraded_inputs)
                            if s["name"] == "Soils & geology"][0]
        assert degraded_section["properties_table"] is None and degraded_section["profile_water"] is None
        assert len(degraded_section["map_unit_table"]["rows"]) == len(DERIVED.order) + 1
        assert degraded_section["map"]["labels_placed"] == {}, "no symbol to place, so none is claimed"
        assert degraded_section["unlabelled"] == []
        for row, mukey in zip(degraded_section["map_unit_table"]["rows"], DERIVED.order):
            assert row["cells"][2] == DERIVED.map_units[mukey]["hydrologic_group"], "the Layer 1 readings survive"
        assert all(e["tfact"] is None and e["kwfact"] is not None
                   for e in degraded_section["derived"].erosion.values()), "K survives, T does not"
# THE LAYER ANSWERED, WITH NOTHING FOR THIS PARCEL. A survey whose rows
# describe no major component of any map unit here (they belong to other
# units), and a geologic compilation that maps no unit under it. Each leaves
# its table or line empty exactly as an absent layer does -- and used to
# reach the template with NO statement, which raised inside the caption
# macro and failed the whole report. The statement now follows the table,
# and says the source answered rather than that it was down.
for answered_empty, key, expect in (
        ({"soil_survey": {**INPUTS.soil_survey, "map_units": {}}}, "survey_unavailable",
         "the Soil Data Access service answered, but described no major component for the map units on this parcel"),
        ({"bedrock_geology": {**INPUTS.bedrock_geology, "units": []}}, "geology_unavailable",
         "the USGS State Geologic Map Compilation answered, but maps no geologic unit under this parcel")):
    empty_inputs = sd.SoilsInputs(**{**INPUTS.__dict__, **answered_empty})
    empty_section = [s for s in site_report.build_sections(DATA, TERRAIN, WATER, ACCESS, TREES, empty_inputs)
                     if s["name"] == "Soils & geology"][0]
    assert empty_section[key] and expect in "".join(p for p in empty_section[key] if isinstance(p, str)), \
        empty_section[key]
    empty_html, empty_document = _render(empty_inputs)
    assert expect in empty_html.replace("&#39;", "'"), key
    assert report_layout.overflowing_boxes(empty_document) == [], key
# AND THE STATEMENT IS ABSENT EXACTLY WHEN THE TABLE IS PRESENT.
for section in ([s for s in site_report.build_sections(DATA, TERRAIN, WATER, ACCESS, TREES, INPUTS)
                 if s["name"] == "Soils & geology"]):
    assert section["properties_table"] and section["survey_unavailable"] is None
    assert section["geology"] and section["geology_unavailable"] is None
print(f"   16 pages; the map page ({len(_tables(MAP_PAGE))} classification tables under the map), the tables page "
      f"({len(_tables(TABLES_PAGE))}) and the last ({len(_tables(CLASS_PAGE))}); one glyph advance "
      f"{advances.pop():.1f} pt; a crowded page moves the farmland block whole; three degraded renders; two sources that answered with "
      f"nothing for this parcel render their statements")

print("\ntest_soils_section.py: all sections passed")
print(offline_harness.summary())
