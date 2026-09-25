"""
test_whole_report.py

THE WHOLE REPORT, RENDERED ONCE -- branch 17 phase 2's verification, the
first time all eight sections, the cover's contents and the back matter
exist together (whole_report_fixture):

  1. no colour literal outside site_report.TOKENS, across every template
     and every module the report renders through; every colour the
     context map's SVG draws is a token's value;
  2. every source line printed in any section's footer matches a source
     in back_matter.SOURCES, and that source's row is on the rendered
     vintage page -- asserted from the rendered HTML, not from the
     builders' own lists;
  3. the contents' page numbers are the pages the sections start on in
     the rendered PDF;
  4. the context map is drawn at its own scale, several times coarser
     than Landform's, with no imagery, and its caption says it is not the
     parcel's terrain; the boundary statement is on the page;
  5. Site overview is one page; the back matter follows Design;
  6. no box past the content width, on any page.

Offline (offline_harness). Set WHOLE_REPORT_OUT to a directory to write
the PDF there as well.
"""

import os
import re
import html as html_module

import offline_harness

offline_harness.install()

import pymupdf  # noqa: E402

import back_matter  # noqa: E402
import report_layout  # noqa: E402
import site_report  # noqa: E402
import whole_report_fixture as w  # noqa: E402
from report_outline import SECTION_OUTLINE, section_number  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")

print("=" * 72)
print("test_whole_report.py -- all eight sections, the contents and the back matter")
print("=" * 72)

BUNDLE = w.inputs()
SECTIONS = site_report.build_sections(BUNDLE["data"], BUNDLE["terrain"], BUNDLE["water"], BUNDLE["access"],
                                      BUNDLE["trees"], BUNDLE["soils"], BUNDLE["design"], BUNDLE["overview"])
HTML_TEXT, DOCUMENT, PDF = w.render(BUNDLE)
if os.environ.get("WHOLE_REPORT_OUT"):
    os.makedirs(os.environ["WHOLE_REPORT_OUT"], exist_ok=True)
    with open(os.path.join(os.environ["WHOLE_REPORT_OUT"], "site-report.pdf"), "wb") as handle:
        handle.write(PDF)
OPENED = pymupdf.open(stream=PDF, filetype="pdf")
PAGE_TEXT = [page.get_text() for page in OPENED]

# ======================================================================
print("1. colour literals live only in site_report.TOKENS")
templates = []
for root, _, files in os.walk(site_report.TEMPLATES_DIRECTORY):
    templates += [os.path.join(root, f) for f in files if f.endswith((".html", ".css"))]
modules = ["overview_section.py", "overview_derivations.py", "back_matter.py", "context_map_data.py", "census_geography.py",
           "structures_data.py", "transmission_lines.py", "physiography.py", "livestock_predators.py",
           "whole_report_fixture.py", "report_map.py", "report_chart.py", "design_section.py", "landform_section.py",
           "water_section.py", "access_section.py", "trees_section.py", "soils_section.py", "climate_section.py"]
for path in templates + [os.path.join(HERE, m) for m in modules]:
    with open(path, encoding="utf-8") as handle:
        found = HEX.findall(handle.read())
    assert not found, (path, found)
with open(os.path.join(HERE, "site_report.py"), encoding="utf-8") as handle:
    source = handle.read()
tokens_block = source[source.index("TOKENS = {"):source.index("}", source.index("TOKENS = {")) + 1]
assert not HEX.findall(source.replace(tokens_block, "")), "a colour literal outside TOKENS in site_report.py"
OVERVIEW = SECTIONS[0]
svg_colours = set(c.lower() for c in HEX.findall(OVERVIEW["map"]["svg"]))
assert svg_colours <= {v.lower() for v in site_report.TOKENS.values()}, svg_colours - set(site_report.TOKENS.values())
print(f"   {len(templates)} templates and {len(modules) + 1} modules clean; the context map draws "
      f"{len(svg_colours)} colours, every one a token")

# ======================================================================
print("2. every source printed in any footer is in the vintage table")
# The footers AS PRINTED: each section's source-footer block in the HTML, citation lines only (a caveat is not a source).
printed = {}
for chunk in re.split(r'<section class="section section--', HTML_TEXT)[1:]:
    name = chunk.split('"')[0].split()[0]
    for block in re.findall(r'<div class="source-footer">(.*?)</div>', chunk, re.S):
        for line in re.findall(r'<p class="source-footer__citation[^"]*">(.*?)</p>', block, re.S):
            printed.setdefault(name, []).append(" ".join(html_module.unescape(re.sub(r"<[^>]+>", "", line)).split()))
assert set(printed) == {"overview", "climate", "landform", "water", "access", "trees", "soils", "design"}, sorted(printed)
# The builder reads the same lines the page prints.
for section in SECTIONS:
    key = section["template"].split(".")[0]
    assert [" ".join(line.split()) for line in back_matter.footer_lines(section)] == printed[key], key
back = back_matter.build_back_matter(SECTIONS, BUNDLE["data"], BUNDLE["terrain"].retrieved_on)
assert back["vintage"]["unmatched"] == [], back["vintage"]["unmatched"]
vintage_page = [i for i, text in enumerate(PAGE_TEXT) if "Sources and data vintage" in text and i > 0]
assert len(vintage_page) == 1, vintage_page
vintage_text = " ".join(PAGE_TEXT[vintage_page[0]].split())
row_of = {key: row for row in back["vintage"]["rows"] for key in row["keys"]}
count = 0
for key, lines in printed.items():
    for line in lines:
        matches = back_matter.matching_sources(line)
        assert matches, (key, line)
        for match in matches:
            assert match in row_of, (key, line, match)
            assert " ".join(row_of[match]["source"].split()) in vintage_text, (match, row_of[match]["source"])
        count += 1
# The rows the table always carries, whatever the footers print.
assert "FCC National Broadband Map" in vintage_text and "not assessed" in vintage_text
assert "Esri Master License Agreement" in vintage_text, "the HIFLD row states the Esri licence as found"
# ONE HOST, ONE ANSWER: both Planetary Computer collections carry the same unresolved terms until they are settled.
assert row_of["3dep_hag"]["terms"] == row_of["naip"]["terms"] == back_matter.PLANETARY_COMPUTER_TERMS
# 3DEP under ONE name: one row, both grids in it.
threedep = [row for row in back["vintage"]["rows"] if row["source"] == "USGS 3D Elevation Program (3DEP)"]
assert len(threedep) == 1 and set(threedep[0]["keys"]) == {"3dep", "3dep_context"}, threedep
# Retrieval dates are data: every report-layer row carries the report data's date, every Layer 1 row the document's.
report_day = back_matter.short_date(BUNDLE["data"].retrieved_on)
for row in back["vintage"]["rows"]:
    assert row["retrieved"], row
assert row_of["nwi"]["retrieved"] == report_day and row_of["sgmc"]["retrieved"] == report_day
assert row_of["nhdplus"]["retrieved"] == report_day and row_of["daymet"]["retrieved"] == report_day
# Design's footer dates NAIP by the report's fetch, and its 3DEP and NHD by Layer 1's -- the same dates the table carries.
assert f"retrieved {site_report.climate_section.format_generated_on(BUNDLE['data'].retrieved_on)}" in printed["design"][1], printed["design"]
assert row_of["naip"]["retrieved"] == report_day
print(f"   {count} footer lines across {len(printed)} sections, each matched; {len(back['vintage']['rows'])} rows on "
      f"page {vintage_page[0] + 1}")

# ======================================================================
print("3. the contents' page numbers are where the sections start")
cover_lines = [line.strip() for line in PAGE_TEXT[0].splitlines() if line.strip()]


def contents_number(name: str) -> int:
    """The number set after `name` on the cover, past its dotted leader."""
    index = cover_lines.index(name)
    for line in cover_lines[index + 1:]:
        if line.strip(". "):
            assert line.isdigit(), (name, line)
            return int(line)
    raise AssertionError(f"{name}: no page number in the contents")


def first_page_of(eyebrow: str) -> int:
    for number, text in enumerate(PAGE_TEXT, start=1):
        if any(line.strip() == eyebrow for line in text.splitlines()):
            return number
    raise AssertionError(f"no page carries {eyebrow!r}")


for name in SECTION_OUTLINE:
    eyebrow = f"{section_number(name)} · {name.upper()}"
    assert contents_number(name) == first_page_of(eyebrow), (name, contents_number(name), first_page_of(eyebrow))
assert contents_number("Sources and methods") == first_page_of("BACK MATTER")
print("   " + "; ".join(f"{section_number(n)} p{contents_number(n)}" for n in SECTION_OUTLINE)
      + f"; back matter p{contents_number('Sources and methods')}")

# ======================================================================
print("4. the context map is not a parcel map")
landform = next(s for s in SECTIONS if s["name"] == "Landform")
context_mpu, landform_mpu = OVERVIEW["map"]["meters_per_unit"], landform["map"]["meters_per_unit"]
assert context_mpu > 5 * landform_mpu, (context_mpu, landform_mpu)
assert OVERVIEW["map"]["scale_bar"]["feet"] > landform["map"]["scale_bar"]["feet"]
assert "<image" not in OVERVIEW["map"]["svg"], "no imagery on the context map"
caption = "".join(p if isinstance(p, str) else p["value"] for p in OVERVIEW["map_caption"])
assert "not its ground" in caption and "20 ft" in caption and "tinted darker every 100 ft higher" in caption
# HIGH AND LOW, SEEN: the ground between index contours is tinted, darker as it rises, the lowest band untinted.
assert "layer-context-band-1" in OVERVIEW["map"]["svg"] and "layer-context-band-0" not in OVERVIEW["map"]["svg"]
overview_text = " ".join(PAGE_TEXT[1].split())
assert "The boundary is as drawn by the user and is not a survey." in overview_text
assert "Coyotes account for the large majority of livestock predator losses in Pennsylvania" in overview_text
assert "voltage not recorded" in overview_text and "138 kV" in overview_text
print(f"   {context_mpu:.2f} m/pt against Landform's {landform_mpu:.2f} ({context_mpu / landform_mpu:.0f}x); scale bar "
      f"{OVERVIEW['map']['scale_bar']['feet']:,} ft; caption and boundary statement on the page")

# ======================================================================
print("5. one overview page; the back matter after Design")
assert first_page_of("I · SITE OVERVIEW") == 2 and first_page_of("II · CLIMATE") == 3
back_start = first_page_of("BACK MATTER")
assert back_start > first_page_of("VIII · DESIGN")
assert first_page_of("BACK MATTER, CONTINUED") == back_start + 1, "the vintage table holds one page"
assert len(PAGE_TEXT) - back_start + 1 == 3, "vintage table, then the methods note on two pages"
# A hyphen that ends a line in the PDF text is joined back to its word.
methods_text = re.sub(r"-\s+", "-", " ".join(" ".join(PAGE_TEXT[back_start:]).split()))
for phrase in ("Thornthwaite", "Hargreaves", "Precipitation factor", "Resultant wind", "largest-remainder",
               "Chaikin", "THE TOLERANCE IS A JUDGMENT", "Weiss", "Keypoints: where a valley's long profile",
               "priority-flood fill", "map-unit cell grid"):
    assert phrase in methods_text, phrase
# THE DESIGN'S METHODS ARE NOT HERE (branch 17 review): how the pipeline decides what to propose belongs to the
# design methods document, not the site data note.
for phrase in ("least-squares", "detector", "existing-road exclusion", "design's own threshold", "full-credit breakpoint",
               "the water step", "canopy dict", "No keypoint, no keyline"):
    assert phrase not in methods_text, phrase
print(f"   {len(PAGE_TEXT)} pages; overview p2; back matter p{back_start}-{len(PAGE_TEXT)}")

# ======================================================================
print("6. no box past the content width")
overruns = report_layout.overflowing_boxes(DOCUMENT)
assert overruns == [], overruns[:5]
print(f"   {len(DOCUMENT.pages)} pages, none")

print("\ntest_whole_report.py: all sections passed")
print(offline_harness.summary())
