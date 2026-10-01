"""
test_report_refit.py

A BLOCK PUSHED ONTO A PAGE OF ITS OWN IS FITTED BACK -- branch 24
(site_report.lay_out). A live 33.3-acre, ten-map-unit parcel lost five
pages this way: the stream table, the canopy extent table, the farmland
table and the soil-test sentence each fell short of fitting by 1 to
30 pt and took a page to itself. The canopy and farmland tables fit now
that every legend strip is as tall as its rows (report.css); the stream
table and the soil-test sentence are the two blocks with a remedy. The
reference parcel fits, so the test makes the shortfall itself: a spacer
of an exact height set ahead of each refittable block, found by
bisection as the least that pushes it -- the tightest miss a parcel can
produce -- and larger ones past it.

Branch 28 adds the source footers: every section's footer is marked
("<section>.sources"), pushed when a page holds nothing but its lines --
its tail after a split the orphans rule allowed -- and its remedy moves
it whole (source-footer--whole), where the break-before binding gives it
the block above for company. The reference parcel's own footers are too
short to split (five lines or fewer under orphans/widows 3), so the
design case below lengthens one to a live parcel's shape. The last case
is the Overview's headroom: the failing live shape -- the streams layer
degraded, its note printed -- fits the one page whole.

For each of the two refittable blocks, offline, one section rendered at
a time from sections built once (whole_report_fixture):

  1. with no spacer, nothing is pushed and nothing is refitted: the
     reference parcel lays out exactly as before;
  2. at the least spacer that pushes it, one layout pass leaves the block
     on a page of its own, and the refitted layout fits it back: the
     block on its page block's first page, the remedy the block's own,
     no page left near-empty, and the section at its unpushed page count
     -- except the stream table, whose remedy moves it onto the wetness
     page: that page is 96% full on the reference parcel, so the water
     table goes over in its place, a half page of table rather than a
     page holding four rows;
  3. at a spacer beyond what the remedy can give back, the refitted
     layout never costs more than the one pass: no more pages, and no
     more near-empty ones at the same count;
  4. in every layout, every caption on the page its block ends on
     (report_layout.captions_apart).
"""

import copy
import dataclasses

import offline_harness

offline_harness.install()

from weasyprint import HTML  # noqa: E402

import overview_section  # noqa: E402
import report_layout  # noqa: E402
import site_report  # noqa: E402
import whole_report_fixture as w  # noqa: E402

print("=" * 72)
print("test_report_refit.py -- a pushed block fitted back")
print("=" * 72)

BUNDLE = w.inputs()
SECTIONS, _, _ = site_report._built_report(
    BUNDLE["data"], None, w.GENERATED_ON, None, site_report.FONTS_DIRECTORY, BUNDLE["terrain"], BUNDLE["water"],
    BUNDLE["access"], BUNDLE["trees"], BUNDLE["soils"], BUNDLE["design"], None, False)
ENV = site_report.jinja_environment()
BY_NAME = {s["template"].split(".")[0]: s for s in SECTIONS}

# (the block's data-refit, its section, the page block the spacer opens, and
# the text the spacer goes after -- the element above the block on its page)
CASES = [
    ("water.surface_water", "water", '<div class="section__figures">'),
    ("soils.soil_test", "soils", '<div class="section__detail">'),
]


def html_for(sections, anchor, spacer):
    html = site_report._render_document(ENV, site_report.FONTS_DIRECTORY, BUNDLE["data"], None, w.GENERATED_ON,
                                        BUNDLE["terrain"], sections, False)
    assert html.count(anchor) == 1, anchor
    return html.replace(anchor, anchor + f'<div style="height: {spacer}px"></div>') if spacer else html


def one_pass(name, anchor, spacer):
    sections = [copy.deepcopy(BY_NAME[name])]
    return HTML(string=html_for(sections, anchor, spacer), base_url=site_report.TEMPLATES_DIRECTORY).render()


def refitted(name, anchor, spacer):
    sections = [copy.deepcopy(BY_NAME[name])]
    _, document = site_report.lay_out(html_for(sections, anchor, spacer), sections,
                                      lambda: html_for(sections, anchor, spacer))
    return document, sections[0].get("refit", [])


for refit, name, anchor in CASES:
    print(f"{refit}")
    base = one_pass(name, anchor, 0)
    # 1. The reference parcel: nothing pushed, nothing refitted.
    assert site_report.pushed_blocks(base) == [], site_report.pushed_blocks(base)
    document, applied = refitted(name, anchor, 0)
    assert applied == [] and len(document.pages) == len(base.pages)
    assert report_layout.captions_apart(base) == []
    pages = len(base.pages)

    # 2. The least spacer that pushes the block, by bisection.
    low, high = 0, 400
    assert refit in site_report.pushed_blocks(one_pass(name, anchor, high)), (refit, "not pushed at", high)
    while high - low > 1:
        middle = (low + high) // 2
        if refit in site_report.pushed_blocks(one_pass(name, anchor, middle)):
            high = middle
        else:
            low = middle
    pushed = one_pass(name, anchor, high)
    assert len(pushed.pages) == pages + 1, (refit, len(pushed.pages), pages)
    assert report_layout.captions_apart(pushed) == []
    document, applied = refitted(name, anchor, high)
    assert applied == [refit.split(".")[1]], (refit, applied)
    assert refit not in site_report.pushed_blocks(document), refit
    # The cover of a partial render carries no contents; the section's pages are what is measured.
    sparse = [c for c in site_report.page_characters(document)[1:] if c < site_report.SPARSE_PAGE_CHARS]
    assert sparse == [], (refit, site_report.page_characters(document))
    expected = pages + 1 if refit == "water.surface_water" else pages
    assert len(document.pages) == expected, (refit, len(document.pages), expected)
    assert report_layout.captions_apart(document) == [], report_layout.captions_apart(document)
    print(f"   pushed at a {high} px spacer ({pages} -> {len(pushed.pages)} pages, characters "
          f"{site_report.page_characters(pushed)}); refitted: {len(document.pages)} pages, characters "
          f"{site_report.page_characters(document)}, every caption with its block")

    # 3. Beyond the remedy: never longer than one pass.
    for spacer in (high + 40, high + 120):
        once = one_pass(name, anchor, spacer)
        document, _ = refitted(name, anchor, spacer)
        assert site_report._page_cost(document) <= site_report._page_cost(once), (refit, spacer)
        assert report_layout.captions_apart(document) == []
    print(f"   at {high + 40} and {high + 120} px: never costlier than one pass")

# ======================================================================
# THE SOURCE FOOTER (branch 28, "design.sources"). The footer binds to the
# block above it and its lines split only three or more to a side
# (report.css), so on the reference parcel no spacer strands it: the break
# falls between the record's rows instead, and the footer keeps company.
# A longer footer -- a live parcel prints more sources -- CAN split, and
# the split's tail is then a page of nothing but source lines: the refit
# moves the footer whole, and, bound above, it takes the record's tail
# along. The pushed window is not a half-line (a larger spacer moves the
# whole footer, with company, instead of splitting it), so this scans
# rather than bisects.
# ======================================================================
print("design.sources")
DESIGN = copy.deepcopy(BY_NAME["design"])
DETAIL = '<div class="section__detail">'
# Two more lines, the shape of a live parcel's longer footer.
LONGER = DESIGN["sources"] + [
    ["USDA NRCS Soil Survey Geographic Database (SSURGO), polygons and components over the parcel, "
     "version of 9/5/2025, retrieved 25 September 2026."],
    ["USFS Forest Inventory and Analysis BIGMAP forest type groups, 2018, plots 2014 through 2018, "
     "at 30 m, retrieved 25 September 2026."],
]

def design_section(sources):
    section = copy.deepcopy(DESIGN)
    section["sources"] = copy.deepcopy(sources)
    return section

# 1. The reference parcel's own footer is never stranded, whatever the
#    shortfall: no spacer pushes it, and no refit pass runs.
for spacer in range(0, 401, 50):
    document = one_pass("design", DETAIL, spacer)
    assert site_report.pushed_blocks(document) == [], (spacer, site_report.pushed_blocks(document))
print("   the reference footer: no spacer strands it (0-400 px)")

# 2. The longer footer: somewhere in the sweep the one-pass split strands
#    its tail on a page of its own, and the refit fixes it back.
window = []
for spacer in range(0, 401, 20):
    sections = [design_section(LONGER)]
    document = HTML(string=html_for(sections, DETAIL, spacer), base_url=site_report.TEMPLATES_DIRECTORY).render()
    pushed = site_report.pushed_blocks(document)
    assert pushed in ([], ["design.sources"]), (spacer, pushed)
    if pushed:
        window.append((spacer, document))
assert window, "no spacer strands the longer footer; the case no longer exercises the remedy"
for spacer, once in window:
    sections = [design_section(LONGER)]
    _, document = site_report.lay_out(html_for(sections, DETAIL, spacer), sections,
                                      lambda: html_for(sections, DETAIL, spacer))
    assert sections[0].get("refit") == ["sources"], (spacer, sections[0].get("refit"))
    assert site_report.pushed_blocks(document) == [], (spacer, site_report.pushed_blocks(document))
    assert len(document.pages) <= len(once.pages), (spacer, len(document.pages), len(once.pages))
    assert site_report._page_cost(document) < site_report._page_cost(once), (spacer, site_report._page_cost(document))
    # The cover and the layout-map page are short by design; every page of
    # the record carries at least the sparse floor once the footer is back
    # with company.
    sparse = [c for c in site_report.page_characters(document)[2:] if c < site_report.SPARSE_PAGE_CHARS]
    assert sparse == [], (spacer, site_report.page_characters(document))
    assert report_layout.captions_apart(document) == [], (spacer, report_layout.captions_apart(document))
print(f"   the longer footer: stranded at {[s for s, _ in window]} px; refitted whole, with company, "
      f"no sparse record page, every caption with its block")

# ======================================================================
# THE OVERVIEW'S HEADROOM (branch 28). The live defect: the Site overview
# sits at the edge of one page, and a failed-source note tipped its last
# two source lines onto a page of their own. The context map's frame gave
# back the margin (overview_section.CONTEXT_FRAME); the failing shape --
# the surrounding-streams layer degraded, its note printed -- must fit
# the one page whole, nothing pushed.
# ======================================================================
print("overview.sources")
degraded = dataclasses.replace(
    BUNDLE["overview"], context_water=None,
    unavailable=dict(BUNDLE["overview"].unavailable,
                     context_water={"label": "surrounding streams", "reason": "did not answer",
                                    "error": "constructed offline (branch 28)"}))
section = overview_section.build_overview_section(degraded, site_report.TOKENS)
section["anchor"] = site_report.section_anchor(section["number"])
overview_html = site_report._render_document(ENV, site_report.FONTS_DIRECTORY, BUNDLE["data"], None, w.GENERATED_ON,
                                             BUNDLE["terrain"], [section], False)
document = HTML(string=overview_html, base_url=site_report.TEMPLATES_DIRECTORY).render()
assert len(document.pages) == 2, (len(document.pages), site_report.page_characters(document))
assert site_report.pushed_blocks(document) == [], site_report.pushed_blocks(document)
assert report_layout.captions_apart(document) == [], report_layout.captions_apart(document)
overview_chars = site_report.page_characters(document)[1]
assert overview_chars >= site_report.SPARSE_PAGE_CHARS, overview_chars
print(f"   streams degraded, the note printed: the cover and ONE overview page ({overview_chars} characters), "
      f"nothing pushed, every caption with its block")

print("\ntest_report_refit.py: all sections passed")
print(offline_harness.summary())
