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

import offline_harness

offline_harness.install()

from weasyprint import HTML  # noqa: E402

import report_layout  # noqa: E402
import site_report  # noqa: E402
import whole_report_fixture as w  # noqa: E402

print("=" * 72)
print("test_report_refit.py -- a pushed block fitted back")
print("=" * 72)

BUNDLE = w.inputs()
SECTIONS, _, _ = site_report._built_report(
    BUNDLE["data"], None, w.GENERATED_ON, None, site_report.FONTS_DIRECTORY, BUNDLE["terrain"], BUNDLE["water"],
    BUNDLE["access"], BUNDLE["trees"], BUNDLE["soils"], None, None, False)
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

print("\ntest_report_refit.py: all sections passed")
print(offline_harness.summary())
