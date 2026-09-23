"""
test_site_report.py

Offline checks for the site data report's rendering foundation
(site_report.py, climate_section.py, templates/report/) -- the phase 2
verification of site-data-report-proposal.md that a test can hold. The
report is built from the real Daymet fixture and the reference parcel;
the network is refused by offline_harness.

  1. THE CLIMATE SECTION'S CONTENT: month-parts, the summary's two
     sentences, the nine key figures, the nine-row table at print
     precision (one decimal, a dash for a true zero, a true minus sign
     for a deficit), the design-storm and severe-weather tables, the
     caption under each figure, the sources footer with one line per
     source, and the methods structure -- read straight off the builder,
     before any template touches it. The section numeral comes from the
     fixed outline, not the render order. A missing frost median reads
     "none recorded" and drops the frost clause rather than inventing a
     date; a degradable layer that is missing leaves a visible statement
     that distinguishes "not covered" from "did not answer".
  2. NO COLOUR LITERAL OUTSIDE TOKENS: a hex-colour grep over the
     stylesheet template, every component and section template,
     site_report.py, the map renderer and the chart renderer hits only
     inside site_report.TOKENS -- nine tokens, water and ochre new.
  3. THE HTML: the climate section composes the components and nothing
     else, on two page blocks; every measured value is inside a
     data-face element; a hostile property label is escaped everywhere it
     appears; without a label the cover shows the centroid; the title is
     "Site Data Report".
  4. THE PDF (skipped, loudly, if WeasyPrint is not importable): renders
     with no network, embeds all three faces, is three pages (the cover
     and the two climate pages), and its monthly table aligns -- right
     edges equal within a column across values of differing digit counts,
     minus signs and dashes included, and the digit advance is uniform
     (tabular figures) -- measured off the laid-out boxes; and NO BOX ON
     ANY PAGE crosses the page's content width (report_layout). The
     sources, the running label and date are in the PDF's text.
  5. THE FAILURE SHAPE: session_report.error_payload() maps a
     ReportDataIncompleteError to failed_layer {type, label, reason} with
     the outage or permanent-gap wording, beside report_failed.
"""

import os
import re
import tempfile
import zlib
from datetime import date

import offline_harness

offline_harness.install()

import requests

import climate_section
import report_chart
import report_layout
import report_map
import session_report
import site_report
from atlas14_data import parse_atlas14_csv
from daymet_data import parse_daymet_csv
from landform_section import ZERO_DASH
from power_wind_data import parse_power_csv
from reference_fixture import REAL_BOUNDARY
from report_data import LAYER_CLIMATE, ReportDataIncompleteError, report_data_from_daily, report_data_from_fixtures

with open("daymet_reference_fixture.csv", encoding="utf-8") as _handle:
    DAILY = parse_daymet_csv(_handle.read())
with open("atlas14_reference_fixture.csv", encoding="utf-8") as _handle:
    ATLAS14 = parse_atlas14_csv(_handle.read())
with open("power_wind_reference_fixture.csv", encoding="utf-8") as _handle:
    POWER = parse_power_csv(_handle.read())
DATA = report_data_from_fixtures(REAL_BOUNDARY, DAILY, atlas14=ATLAS14, power_wind=POWER)
GENERATED_ON = date(2026, 9, 21)
MINUS = "\u2212"

# ======================================================================
# 1. The climate section's content
# ======================================================================
print("1. the climate section's content, off the builder")

section = climate_section.build_climate_section(DATA)
# THE NUMERAL IS THE OUTLINE'S, NOT THE RENDER ORDER'S: Climate is II with
# Site overview unbuilt ahead of it.
assert section["number"] == "II" and section["name"] == "Climate" and section["template"] == "climate.html"
import report_outline
assert report_outline.section_number("Site overview") == "I"
assert report_outline.section_number("Climate") == "II"
assert report_outline.section_number("Soils & geology") == "IX"
assert len(report_outline.SECTION_OUTLINE) == 9
try:
    report_outline.section_number("Weather")
except ValueError:
    pass
else:
    raise AssertionError("a name outside the outline must not be numbered")

assert climate_section.month_part({"month": 4, "day": 10}) == "early April"
assert climate_section.month_part({"month": 4, "day": 11}) == "mid April"
assert climate_section.month_part({"month": 4, "day": 20}) == "mid April"
assert climate_section.month_part({"month": 10, "day": 21}) == "late October"

# ONLY THE COUNTS ARE DATA; the month-parts and the month runs are prose.
summary_values = [p["value"] for p in section["summary"] if isinstance(p, dict)]
assert summary_values == ["177", "1.9"], summary_values
prose = "".join(p if isinstance(p, str) else "{}" for p in section["summary"])
assert prose == (
    "The frost-free season runs about {} days, from late April to mid October. "
    "Rainfall exceeds evaporation from September through May; June through August run a deficit of about {} in."
), prose
assert climate_section.month_run([10, 11, 12, 1, 2]) == "October through February"
assert climate_section.month_run([6, 7, 9]) == "June, July and September"
assert climate_section.month_run([7]) == "July" and climate_section.month_run([]) == ""

# NINE KEY FIGURES, in the brief's order, with months in deficit last.
figures = [(f["label"], f["value"]) for f in section["key_figures"]]
assert figures == [
    ("median last spring frost", "Apr 27"),
    ("median first fall frost", "Oct 20"),
    ("frost-free days", "177"),
    ("inches precipitation per year", "43.7"),
    ("inches in the driest year, 1995", "34.5"),
    ("inches in the wettest year, 2018", "60.9"),
    ("growing degree days, base 50°F", "2,933"),
    ("est. hardiness zone", "6b"),
    ("months in deficit, Jun–Aug", "3"),
], figures
assert not any(f.get("word") for f in section["key_figures"])
assert "".join(section["key_figures_caption"]).startswith("Low ground and valley floors typically frost later")

# THE MONTHLY TABLE: nine rows in the brief's order, at print precision.
table = section["table"]
assert table["columns"] == ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"]
rows = {r["label"]: r["cells"] for r in table["rows"]}
assert list(rows) == [
    "Mean high °F", "Mean low °F", "Precipitation, in", "Potential evaporation, in", "Water balance, in",
    "Days over 1 in", "GDD, base 50°F", "Day length, h", "Solar, kWh/m²/day",
], list(rows)
assert all(len(r["cells"]) == 12 for r in table["rows"])
assert rows["Mean high °F"][0] == "36" and rows["Mean high °F"][6] == "83"
assert rows["Precipitation, in"] == ["3.3", "2.8", "3.2", "3.9", "4.3", "4.6", "4.2", "4.2", "3.7", "3.3", "3.2", "3.1"], rows["Precipitation, in"]
# January and February evaporate NOTHING (Thornthwaite at or below 0 °C): dashes, not "0.0". December
# evaporates 0.04 in -- not nothing -- and reads "<0.1", never a dash: the dash is for a true zero only.
assert rows["Potential evaporation, in"][:2] == [ZERO_DASH, ZERO_DASH] and rows["Potential evaporation, in"][11] == "<0.1"
assert rows["Potential evaporation, in"][6] == "5.4"
# The balance row: a deficit month carries a TRUE MINUS SIGN, never a hyphen; a rounded zero is a dash.
balance = rows["Water balance, in"]
assert balance[6] == MINUS + "1.2" and balance[7] == MINUS + "0.6" and balance[5] == MINUS + "0.1", balance
assert not any("-" in cell for cell in balance), "no hyphen-minus in the balance row"
assert balance[0] == "3.3" and balance[11] == "3.1"
# Days over 1 in are the STATIONS' normals (medians), not Daymet's count.
assert rows["Days over 1 in"] == ["0.5", "0.3", "0.5", "0.5", "0.6", "0.9", "1.0", "0.7", "0.8", "0.5", "0.6", "0.5"], rows["Days over 1 in"]
assert rows["GDD, base 50°F"][6] == "686"
assert rows["Day length, h"][5] == "14.9" and rows["Day length, h"][11] == "9.1"
assert rows["Solar, kWh/m²/day"][5] == "5.7" and rows["Solar, kWh/m²/day"][11] == "1.5"
assert all(re.fullmatch(r"-?\d+", v) for v in rows["Mean high °F"] + rows["Mean low °F"] + rows["GDD, base 50°F"])
for label in ("Precipitation, in", "Day length, h", "Solar, kWh/m²/day"):
    assert all(re.fullmatch(r"\d+\.\d", v) for v in rows[label]), label
for label in ("Potential evaporation, in", "Days over 1 in"):
    assert all(v in (ZERO_DASH, "<0.1") or re.fullmatch(r"\d+\.\d", v) for v in rows[label]), label
assert all(v == ZERO_DASH or re.fullmatch(MINUS + r"?\d+\.\d", v) for v in balance)
caption = section["table_caption"]
assert [p["value"] for p in caption if isinstance(p, dict)] == ["0.96"]
assert "".join(p if isinstance(p, str) else "{}" for p in caption) == (
    "Precipitation scaled by {} to the 5 nearest NOAA station normals; days over 1 in from the same stations."
)

# DESIGN STORMS at the source's own precision; SEVERE WEATHER as reports per year and a peak month.
storms = section["design_storms"]
assert storms["unavailable"] is None
assert storms["table"]["columns"] == ["2-yr", "10-yr", "25-yr", "100-yr"] and storms["table"]["compact"] is True
assert [(r["label"], r["cells"]) for r in storms["table"]["rows"]] == [
    ("1-hour, in", ["1.19", "1.74", "2.07", "2.58"]),
    ("24-hour, in", ["2.38", "3.34", "3.96", "4.98"]),
]
assert storms["caption"] == ["Point estimates from records through 2000; heavier recent storms are not reflected."]
severe = section["severe_weather"]
assert severe["table"]["corner"] == "Within 25 miles" and severe["table"]["columns"] == ["Reports per year", "Peak month"]
severe_rows = severe["table"]["rows"]
assert severe_rows[0]["label"] == ["Hail (largest ", {"value": "2.5 in"}, ")"] and severe_rows[0]["cells"] == ["20.5", "Jun"]
assert severe_rows[1]["label"] == "Damaging wind" and severe_rows[1]["cells"] == ["63.9", "Jun"]
assert severe_rows[2]["label"] == "Tornado" and severe_rows[2]["cells"] == ["1.2", ZERO_DASH], "no tornado month"
assert "cluster near roads and towns" in severe["caption"][0]

# THE CHARTS: the water balance from the corrected months, the roses labelled "wind from".
wb = section["water_balance"]
assert wb["chart"]["svg"].startswith("<svg") and [round(x, 2) for x in wb["chart"]["crossings"]] == [4.92, 7.65], wb["chart"]["crossings"]
assert [b["sign"] for b in wb["chart"]["bands"]] == ["surplus", "deficit", "surplus"]
assert wb["caption"] == ["Potential evaporation is estimated from temperature; actual loss depends on cover and soil."]
roses = section["wind_roses"]
assert roses["unavailable"] is None and roses["chart"]["svg"].count(">wind from</text>") == 2
assert [r["prevailing"] for r in roses["chart"]["roses"]] == ["W", "SW"], "the modal sectors, and the solid wedges"
for rose in roses["chart"]["roses"]:
    assert rose["wedge_radii"][rose["prevailing"]] == max(rose["wedge_radii"].values()), "the solid wedge is the longest"
assert any("Resultant wind" in n and "winter from SW (246°)" in n for n in section["methods"][3]["notes"])
assert "regional estimate" in roses["caption"][0]

# THE SOURCES: one line per source, names and periods only, no data part.
sources = ["".join(line) for line in section["sources"]]
assert all(isinstance(p, str) for line in section["sources"] for p in line)
assert sources == [
    "Daymet Version 4 R1, 1 km daily grid, 30-year means 1995–2024.",
    "NCEI U.S. Climate Normals 1991–2020, 5 stations within 14 miles.",
    "NOAA Atlas 14 Volume 2 Version 3 (Ohio River Basin), partial-duration series, records through 2000.",
    "NASA POWER (MERRA-2), 0.5° × 0.625° cell, 1995–2024.",
    "NOAA Storm Prediction Center severe weather reports 1995–2024, files of 13 May 2025.",
], sources
# THE METHODS structure for the note at the back: one entry per source, full citations, not rendered.
methods = section["methods"]
assert [m["source"] for m in methods] == ["Daymet", "NCEI U.S. Climate Normals", "NOAA Atlas 14", "NASA POWER", "NOAA Storm Prediction Center"]
assert methods[0]["citation"] == climate_section.display_citation(DAILY["citation"]) and ";" not in methods[0]["citation"]
assert "Thornthwaite" in methods[0]["method"] and any("Hargreaves" in n for n in methods[0]["notes"])
assert any("0.960" in n for n in methods[1]["notes"]) and any("Atlas 15" in n for n in methods[2]["notes"])
assert methods[3]["citation"].startswith("The data was obtained from the National Aeronautics and Space Administration")
assert climate_section.daymet_version_label({"citation": "no version here", "software_version": "4.0"}) == "Daymet Version 4.0"
assert climate_section.display_citation(DAILY["citation"]) == DAILY["citation"].replace(";", ",")

# A missing median: no date invented, the clause dropped, the shortfall said in the key-figures caption.
import copy
short = copy.deepcopy(DATA)
short.climate["frost"]["first_fall"] = None
short.climate["frost"]["frost_free_days"] = None
short.climate["frost"]["years_with_fall_frost"] = 12
short_section = climate_section.build_climate_section(short)
assert [p["value"] for p in short_section["summary"] if isinstance(p, dict)] == ["1.9"]
assert "".join(p if isinstance(p, str) else "{}" for p in short_section["summary"]).startswith("Rainfall exceeds evaporation")
short_figures = {f["label"]: f for f in short_section["key_figures"]}
assert short_figures["median first fall frost"]["value"] == "none recorded" and short_figures["median first fall frost"]["word"] is True
assert "The median fall frost stands on 12 of the 30 years" in "".join(short_section["key_figures_caption"])

# DEGRADED LAYERS leave a visible statement: "not covered" is told from "did not answer".
uncovered = report_data_from_fixtures(
    REAL_BOUNDARY, DAILY, atlas14=None, power_wind=None,
    unavailable={
        "atlas14": {"label": "design storm depths", "reason": "no_data_for_parcel",
                    "error": "Atlas 14 server: Error 3.0: Selected location is not within a project area"},
        "power_wind": {"label": "wind records", "reason": "source_unavailable", "error": "timed out"},
    },
)
degraded_section = climate_section.build_climate_section(uncovered)
statement = "".join(p if isinstance(p, str) else p["value"] for p in degraded_section["design_storms"]["unavailable"])
assert degraded_section["design_storms"]["table"] is None
assert statement.startswith("Design storm depths are unavailable: NOAA Atlas 14 does not cover this location.")
assert "Atlas 15" in statement and "2027" in statement and "Washington, Oregon, Idaho, Montana and Wyoming" in statement
wind_statement = "".join(degraded_section["wind_roses"]["unavailable"])
assert degraded_section["wind_roses"]["chart"] is None and "did not answer" in wind_statement and "timed out" in wind_statement
down = report_data_from_fixtures(
    REAL_BOUNDARY, DAILY, atlas14=None, power_wind=POWER,
    unavailable={"atlas14": {"label": "design storm depths", "reason": "source_unavailable", "error": "503"}},
)
down_statement = climate_section.build_climate_section(down)["design_storms"]["unavailable"]
assert "did not answer" in down_statement[0] and "503" in down_statement[0] and down_statement[1]["value"] == "40.6446, -79.9826"
assert [s[:4] for s in ["".join(l) for l in degraded_section["sources"]]] == ["Daym", "NCEI", "NOAA"], "only the sources used are listed"
print("   two summary sentences, nine key figures, nine table rows at print precision, two tables, five source lines, methods; the missing-median and degraded paths")

# ======================================================================
# 2. No colour literal outside TOKENS
# ======================================================================
print("2. colour literals live only in site_report.TOKENS")

HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
templates_dir = site_report.TEMPLATES_DIRECTORY
template_files = []
for root, _, files in os.walk(templates_dir):
    template_files += [os.path.join(root, f) for f in files]
assert len(template_files) == 16, sorted(template_files)   # css, base, 9 components, 5 sections
for path in template_files:
    with open(path, encoding="utf-8") as handle:
        hits = HEX.findall(handle.read())
    assert not hits, f"{path} carries colour literal(s) {hits}"
for renderer in (report_map, report_chart):
    with open(renderer.__file__, encoding="utf-8") as handle:
        assert HEX.findall(handle.read()) == [], f"{renderer.__name__} carries a colour literal"
with open(site_report.__file__, encoding="utf-8") as handle:
    module_source = handle.read()
tokens_start = module_source.index("TOKENS = {")
tokens_end = module_source.index("}", tokens_start)
outside = HEX.findall(module_source[:tokens_start] + module_source[tokens_end:])
assert not outside, f"site_report.py carries colour literal(s) outside TOKENS: {outside}"
inside = HEX.findall(module_source[tokens_start:tokens_end])
assert sorted(inside) == sorted(site_report.TOKENS.values()), inside
assert site_report.TOKENS == {
    "page": "#ffffff", "stock": "#f4f1ea", "rule": "#ddd6c8",
    "ink": "#2b2b26", "ink-muted": "#8a8477", "oxide": "#9c4a2f",
    "terrain": "#7a5c3a",
    "water": "#3f5d75", "ochre": "#c99a2e",
    # Branch 11: the frontend's map-geometry green, the Trees section's canopy screen.
    "field": "#4a5f3a",
}
# Ochre and field are the frontend's own values; water is new to the plate system.
assert site_report.TOKENS["ochre"] == "#c99a2e" and site_report.TOKENS["field"] == "#4a5f3a"
# The rendered stylesheet declares each token once and reads colours only
# through var().
css = site_report.render_stylesheet()
for name, value in site_report.TOKENS.items():
    assert f"--{name}: {value};" in css, name
assert "tabular-nums" in css
assert css.count("@font-face") == 4 and "assets/fonts/bitter-latin-wght-normal.woff2" in css
print(f"   {len(template_files)} template files clean; {len(inside)} literals, all inside TOKENS")

# ======================================================================
# 3. The HTML
# ======================================================================
print("3. the HTML: components composed, values in the data face, label escaped, centroid fallback")

html = site_report.render_site_report_html(DATA, generated_on=GENERATED_ON)
assert "<title>Site Data Report — 40.6446° N, 79.9826° W</title>" in html
assert '<h1 class="cover__title">Site Data Report</h1>' in html
assert "Generated 21 September 2026" in html
for marker in ('class="eyebrow"', 'class="heading"', 'class="summary"', 'class="key-figures"',
               'class="source-footer"', 'report-chart--water-balance', 'report-chart--wind-roses'):
    assert html.count(marker) == 1, marker
assert html.count('class="data-table') == 3 and html.count('class="caption"') == 6
assert '<span class="eyebrow__number">II</span> · Climate' in html
assert '<span class="data">177</span>' in html and '<span class="data">1.9</span>' in html
assert 'from late April to mid October' in html and '<span class="data">late April</span>' not in html
# The data spans on the page: the summary's two, the table caption's factor, the hail label's size.
assert html.count('<span class="data">') == 4, html.count('<span class="data">')
assert html.count('class="key-figure"') == 9
assert html.count('<td class="num">') == 9 * 12 + 2 * 4 + 3 * 2 and html.count('<th class="num">') == 12 + 4 + 2
assert html.count('class="source-footer__citation source-footer__line"') == 5 and 'class="source-footer__caveat"' not in html
assert html.index('class="section__figures"') < html.index('class="section__detail"')
assert html.index('report-chart--water-balance') < html.index('report-chart--wind-roses') < html.index('class="section__detail"')
assert MINUS + "1.2" in html and "-1.2" not in html
assert html.count("<svg xmlns") == 2 + 4, "two charts and the four legend swatches"
# Section templates carry no styling of their own.
with open(os.path.join(templates_dir, "sections", "climate.html"), encoding="utf-8") as handle:
    section_template = handle.read()
assert "style" not in section_template.lower() and "<style" not in section_template
for name in ("eyebrow", "heading", "summary", "key_figures", "data_table", "source_footer", "chart", "caption"):
    assert f'components/{name}.html' in section_template, name

hostile = '<script>alert(1)</script> & "Farm" \'Lane\''
escaped = site_report.render_site_report_html(DATA, property_label=hostile, generated_on=GENERATED_ON)
assert "<script>" not in escaped
assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &#34;Farm&#34; &#39;Lane&#39;" in escaped
assert escaped.count("&lt;script&gt;") == 3, "title, running label, cover label"
# The stylesheet, by contrast, is not escaped: its quotes are CSS.
assert '@font-face {\n  font-family: "Bitter";' in escaped
print("   components composed on two page blocks, 122 numeric cells, hostile label escaped three times, stylesheet unescaped")

# ======================================================================
# 4. The PDF
# ======================================================================
print("4. the PDF: offline, three faces embedded, three pages, decimal alignment measured with minus signs and dashes")

import importlib.util

if importlib.util.find_spec("weasyprint") is None:
    print("   SKIPPED: weasyprint not installed here; the render is verified where it is installed")
else:
    out_dir = tempfile.mkdtemp(prefix="site-report-test-")
    pdf_path = site_report.generate_site_report_pdf(DATA, os.path.join(out_dir, "report.pdf"), generated_on=GENERATED_ON)
    with open(pdf_path, "rb") as handle:
        pdf = handle.read()
    assert pdf.startswith(b"%PDF") and len(pdf) > 10000
    assert len(offline_harness.refused()) == 0, "the render must reach no network"

    # Fonts, off the PDF's font resource names (the pdffonts read). The
    # font dictionaries sit inside compressed object streams, so every
    # Flate stream is inflated and searched beside the raw bytes.
    inflated = [pdf]
    for raw in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", pdf, re.S):
        try:
            inflated.append(zlib.decompress(raw))
        except zlib.error:
            continue
    names = set()
    for blob in inflated:
        names |= set(re.findall(rb"/BaseFont\s*/([A-Za-z0-9+\-,]+)", blob))
    families = {n.split(b"+", 1)[-1] for n in names}
    assert any(f.startswith(b"Bitter") for f in families), families
    assert any(f.startswith(b"Source-Serif-4") for f in families), families
    assert any(f.startswith(b"IBM-Plex-Mono") for f in families), families
    assert not any(b"DejaVu" in f or b"Liberation" in f for f in families), families

    # DECIMAL ALIGNMENT, measured. Every numeric cell is right-aligned, so
    # within one month column the right edges must agree across rows whose
    # values have different digit counts (36 above 3.4 above 683), and with
    # tabular figures every glyph must advance identically, so a decimal
    # point sits the same distance in from the edge for every value with
    # the same count of digits after it.
    # Measured on the LAID-OUT boxes: WeasyPrint's rendered document exposes
    # every box's position and width, which is exactly the geometry the
    # PDF was drawn from, without a PDF parser.
    from weasyprint import HTML

    document = HTML(string=site_report.render_site_report_html(DATA, generated_on=GENERATED_ON)).render()
    page = document.pages[2]      # the numbers page

    def _walk(box):
        yield box
        for child in getattr(box, "children", []) or []:
            yield from _walk(child)

    cells = []
    for box in _walk(page._page_box):
        # The cell box itself -- a td also owns the inline wrappers beneath
        # it, which report the same element and must not count again. The
        # monthly table is the first data table on the page: its 108 cells
        # come first in document order.
        if type(box).__name__ == "TableCellBox" and box.element_tag == "td" and "num" in (box.element.get("class") or ""):
            text = "".join(box.element.itertext()).strip()
            cells.append((round(box.position_x + box.width, 3), text, box))
    assert len(cells) == 122, len(cells)
    monthly = cells[:108]
    columns = {}
    for right, text, box in monthly:
        columns.setdefault(right, []).append(text)
    assert len(columns) == 12, f"expected 12 right edges, got {sorted(columns)}"
    for right, values in columns.items():
        widths = {len(v) for v in values}
        assert len(widths) >= 2, f"column at {right} has no digit-width variety: {values}"
    # A minus sign and a dash share the column's right edge with the plain figures.
    assert any(text.startswith(MINUS) for _, text, _ in monthly) and any(text == ZERO_DASH for _, text, _ in monthly)
    # Tabular figures: every glyph in a numeric cell advances the same.
    # The text box inside each cell reports its own width; width / glyph
    # count must be one constant across the table -- the minus sign and
    # the dash included, since the data face carries both glyphs.
    advances = set()
    for right, text, box in monthly:
        line_boxes = [b for b in _walk(box) if type(b).__name__ == "TextBox"]
        assert line_boxes, text
        width = sum(b.width for b in line_boxes)
        advances.add(round(width / len(text), 2))
    assert len(advances) == 1, f"glyph advances differ across numeric cells: {sorted(advances)}"
    print(f"   {len(monthly)} monthly cells in 12 columns, every column mixing digit widths, one glyph advance {advances.pop()} pt")

    # The rendered text carries the citation, the running label and date.
    flat = "".join(
        "".join(b.text for b in _walk(p._page_box) if type(b).__name__ == "TextBox")
        for p in document.pages
    )
    # Line wraps drop the space at a break, so compare with whitespace removed.
    for line in section["sources"]:
        assert "".join("".join(line).split()) in "".join(flat.split()), line
    assert "".join("40.6446° N, 79.9826° W".split()) in "".join(flat.split())
    assert "".join("21 September 2026".split()) in "".join(flat.split())
    assert len(document.pages) == 3, len(document.pages)
    # NO BOX PAST THE MEASURE, on any page: the check the review asked for
    # after the severe-weather table sat 51 pt past the right margin.
    overruns = report_layout.overflowing_boxes(document)
    assert overruns == [], overruns[:5]
    print(f"   {len(document.pages)} pages, no box past the content width on any of them")
    print(f"   {len(document.pages)} pages; families {sorted(f.decode() for f in families)}")

# ======================================================================
# 5. The failure shape
# ======================================================================
print("5. error_payload() maps a report-layer failure to failed_layer")

down = ReportDataIncompleteError(
    "x", LAYER_CLIMATE[0], LAYER_CLIMATE[1], ReportDataIncompleteError.REASON_SOURCE_UNAVAILABLE
)
payload = session_report.error_payload(down)
assert payload["failed_layer"] == {"type": "climate", "label": "climate records", "reason": "source_unavailable"}
assert payload["report_failed"] == {"actionable": True}
assert "climate records could not be retrieved" in payload["error"] and "unharmed" in payload["error"]
assert "session_expired" not in payload

gap = ReportDataIncompleteError("x", *LAYER_CLIMATE, reason=ReportDataIncompleteError.REASON_NO_DATA_FOR_PARCEL)
payload = session_report.error_payload(gap)
assert payload["failed_layer"]["reason"] == "no_data_for_parcel"
assert payload["report_failed"] == {"actionable": False}
assert "permanent gap" in payload["error"] and "retrying will not help" in payload["error"]

# The two shapes that were there before are untouched.
other = session_report.error_payload(requests.exceptions.ConnectionError("tiles"))
assert other == {"error": session_report.GENERATION_FAILED, "report_failed": {"actionable": False}}
print("   outage and permanent-gap wordings, report_failed beside failed_layer, the old shapes unchanged")

print("\ntest_site_report.py: all sections passed")
print(offline_harness.summary())
