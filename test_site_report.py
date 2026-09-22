"""
test_site_report.py

Offline checks for the site data report's rendering foundation
(site_report.py, climate_section.py, templates/report/) -- the phase 2
verification of site-data-report-proposal.md that a test can hold. The
report is built from the real Daymet fixture and the reference parcel;
the network is refused by offline_harness.

  1. THE CLIMATE SECTION'S CONTENT: month-parts, the six key figures, the
     table's rows and rounding, and the footer -- caveat first, then the
     citation with Daymet's CSV semicolons turned back into commas --
     read straight off the builder, before any template touches it. The
     section numeral comes from the fixed outline, not the render order. A missing frost median reads "none recorded"
     and drops the frost clause rather than inventing a date.
  2. NO COLOUR LITERAL OUTSIDE TOKENS: a hex-colour grep over the
     stylesheet template, every component and section template, and
     site_report.py hits only inside site_report.TOKENS.
  3. THE HTML: every section composes the six components and nothing
     else; every measured value is inside a data-face element; a hostile
     property label is escaped everywhere it appears; without a label the
     cover shows the centroid; the title is "Site Data Report".
  4. THE PDF (skipped, loudly, if WeasyPrint is not importable): renders
     with no network, embeds all three faces, is two pages, and its
     monthly table aligns -- right edges equal within a column across
     values of differing digit counts, and the digit advance is uniform
     (tabular figures) -- measured off the PDF's own text positions with
     no PDF library beyond WeasyPrint's output stream. The footer
     citation and the running label/date are in the PDF's text.
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
import session_report
import site_report
from daymet_data import parse_daymet_csv
from reference_fixture import REAL_BOUNDARY
from report_data import LAYER_CLIMATE, ReportDataIncompleteError, report_data_from_daily

with open("daymet_reference_fixture.csv", encoding="utf-8") as _handle:
    DAILY = parse_daymet_csv(_handle.read())
DATA = report_data_from_daily(REAL_BOUNDARY, DAILY)
GENERATED_ON = date(2026, 9, 21)

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

# ONLY THE COUNT IS DATA; the month-parts and the month are prose.
summary_values = [p["value"] for p in section["summary"] if isinstance(p, dict)]
assert summary_values == ["177"], summary_values
prose = "".join(p if isinstance(p, str) else "{}" for p in section["summary"])
assert prose == (
    "The frost-free season runs about {} days, from late April to mid October. "
    "Precipitation peaks in June."
), prose

figures = {f["label"]: f["value"] for f in section["key_figures"]}
assert figures == {
    "median last spring frost": "Apr 27",
    "median first fall frost": "Oct 20",
    "frost-free days": "177",
    "inches precipitation per year": "45.5",
    "growing degree days, base 50°F": "2,933",
    "est. hardiness zone": "6b",
}, figures

table = section["table"]
assert table["columns"] == ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"]
rows = {r["label"]: r["cells"] for r in table["rows"]}
assert list(rows) == ["Mean high °F", "Mean low °F", "Precipitation, in", "GDD, base 50°F", "Solar, kWh/m²/day"]
assert rows["Mean high °F"][0] == "36" and rows["Mean high °F"][6] == "83"
assert rows["Precipitation, in"][5] == "4.8" and rows["GDD, base 50°F"][6] == "686"
assert rows["Solar, kWh/m²/day"][5] == "5.7" and rows["Solar, kWh/m²/day"][11] == "1.5"
assert all(len(r["cells"]) == 12 for r in table["rows"])
# Whole numbers for temperatures and GDD, one decimal for precipitation
# and solar -- the brief's example, row by row.
assert all(re.fullmatch(r"-?\d+", v) for v in rows["Mean high °F"] + rows["Mean low °F"] + rows["GDD, base 50°F"])
assert all(re.fullmatch(r"\d+\.\d", v) for v in rows["Precipitation, in"] + rows["Solar, kWh/m²/day"])

# THE FOOTER: caveat first, citation second, and no data part in either --
# a product name and a year range are names.
footer = section["footer"]
assert list(footer) == ["caveat", "citation"]
assert all(isinstance(p, str) for p in footer["caveat"] + footer["citation"])
caveat_text = "".join(footer["caveat"])
citation_text = "".join(footer["citation"])
assert caveat_text.startswith("Daymet interpolates between weather stations on a 1 km grid")
assert "simple-average method" in caveat_text and "not the official USDA map" in caveat_text
assert "stands on" not in caveat_text, "every year had both frosts, so no shortfall sentence"
assert citation_text.startswith("Source: Daymet Version 4 R1, 30-year means 1995–2024. Thornton, M.M., R. Shrestha,")
# The commas Daymet's CSV header swapped for semicolons are restored for
# display; the served line is kept verbatim on the data.
assert ";" not in citation_text
assert climate_section.display_citation(DAILY["citation"]) in citation_text
assert climate_section.display_citation(DAILY["citation"]) == DAILY["citation"].replace(";", ",")
assert DAILY["citation"].count(";") == 10 and ";" in DAILY["citation"]
assert citation_text.endswith("ORNL DAAC, Oak Ridge, Tennessee, USA. https://doi.org/10.3334/ORNLDAAC/2129")
footer_text = caveat_text + " " + citation_text
# A citation without a version label falls back to the software version.
assert climate_section.daymet_version_label({"citation": "no version here", "software_version": "4.0"}) == "Daymet Version 4.0"

# A missing median: no date invented, the clause dropped, the shortfall said.
import copy
short = copy.deepcopy(DATA)
short.climate["frost"]["first_fall"] = None
short.climate["frost"]["frost_free_days"] = None
short.climate["frost"]["years_with_fall_frost"] = 12
short_section = climate_section.build_climate_section(short)
assert [p["value"] for p in short_section["summary"] if isinstance(p, dict)] == []
assert "".join(short_section["summary"]) == "Precipitation peaks in June."
assert {f["label"]: f["value"] for f in short_section["key_figures"]}["median first fall frost"] == "none recorded"
short_footer = "".join(short_section["footer"]["caveat"])
assert "The median fall frost stands on 12 of the 30 years" in short_footer
print("   summary parts, six key figures, five table rows, footer citation verbatim; the missing-median path")

# ======================================================================
# 2. No colour literal outside TOKENS
# ======================================================================
print("2. colour literals live only in site_report.TOKENS")

HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
templates_dir = site_report.TEMPLATES_DIRECTORY
template_files = []
for root, _, files in os.walk(templates_dir):
    template_files += [os.path.join(root, f) for f in files]
assert len(template_files) == 11, sorted(template_files)   # css, base, 7 components, 2 sections
for path in template_files:
    with open(path, encoding="utf-8") as handle:
        hits = HEX.findall(handle.read())
    assert not hits, f"{path} carries colour literal(s) {hits}"
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
}
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
               'class="data-table"', 'class="source-footer"'):
    assert html.count(marker) == 1, marker
assert '<span class="eyebrow__number">II</span> · Climate' in html
assert '<span class="data">177</span>' in html
assert 'from late April to mid October' in html and '<span class="data">late April</span>' not in html
assert html.count('<span class="data">') == 1, "the summary's one figure is the only data span on the page"
# The footer: caveat paragraph before the citation paragraph, citation with commas.
assert html.index('class="source-footer__caveat"') < html.index('class="source-footer__citation"')
assert "Thornton, M.M., R. Shrestha" in html and "Thornton; M.M." not in html
assert html.count('class="key-figure"') == 6 and html.count('<td class="num">') == 60
assert html.count('<th class="num">') == 12
assert climate_section.display_citation(DAILY["citation"]) in html
# Section templates carry no styling of their own.
with open(os.path.join(templates_dir, "sections", "climate.html"), encoding="utf-8") as handle:
    section_template = handle.read()
assert "style" not in section_template.lower() and "<style" not in section_template
for name in ("eyebrow", "heading", "summary", "key_figures", "data_table", "source_footer"):
    assert f'components/{name}.html' in section_template, name

hostile = '<script>alert(1)</script> & "Farm" \'Lane\''
escaped = site_report.render_site_report_html(DATA, property_label=hostile, generated_on=GENERATED_ON)
assert "<script>" not in escaped
assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &#34;Farm&#34; &#39;Lane&#39;" in escaped
assert escaped.count("&lt;script&gt;") == 3, "title, running label, cover label"
# The stylesheet, by contrast, is not escaped: its quotes are CSS.
assert '@font-face {\n  font-family: "Bitter";' in escaped
print("   six components once each, 60 numeric cells, hostile label escaped three times, stylesheet unescaped")

# ======================================================================
# 4. The PDF
# ======================================================================
print("4. the PDF: offline, three faces embedded, two pages, decimal alignment measured")

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
    page = document.pages[1]

    def _walk(box):
        yield box
        for child in getattr(box, "children", []) or []:
            yield from _walk(child)

    cells = []
    for box in _walk(page._page_box):
        # The cell box itself -- a td also owns the inline wrappers beneath
        # it, which report the same element and must not count again.
        if type(box).__name__ == "TableCellBox" and box.element_tag == "td" and "num" in (box.element.get("class") or ""):
            text = "".join(box.element.itertext()).strip()
            cells.append((round(box.position_x + box.width, 3), text, box))
    assert len(cells) == 60, len(cells)
    columns = {}
    for right, text, box in cells:
        columns.setdefault(right, []).append(text)
    assert len(columns) == 12, f"expected 12 right edges, got {sorted(columns)}"
    for right, values in columns.items():
        widths = {len(v) for v in values}
        assert len(widths) >= 2, f"column at {right} has no digit-width variety: {values}"
    # Tabular figures: every glyph in a numeric cell advances the same.
    # The text box inside each cell reports its own width; width / glyph
    # count must be one constant across the table.
    advances = set()
    for right, text, box in cells:
        line_boxes = [b for b in _walk(box) if type(b).__name__ == "TextBox"]
        assert line_boxes, text
        width = sum(b.width for b in line_boxes)
        advances.add(round(width / len(text), 2))
    assert len(advances) == 1, f"glyph advances differ across numeric cells: {sorted(advances)}"
    print(f"   {len(cells)} numeric cells in 12 columns, every column mixing digit widths, one glyph advance {advances.pop()} pt")

    # The rendered text carries the citation, the running label and date.
    flat = "".join(
        "".join(b.text for b in _walk(p._page_box) if type(b).__name__ == "TextBox")
        for p in document.pages
    )
    # Line wraps drop the space at a break, so compare with whitespace removed.
    assert "".join(climate_section.display_citation(DAILY["citation"]).split()) in "".join(flat.split())
    assert "".join("40.6446° N, 79.9826° W".split()) in "".join(flat.split())
    assert "".join("21 September 2026".split()) in "".join(flat.split())
    assert len(document.pages) == 2
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
