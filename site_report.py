"""
site_report.py

THE SITE DATA REPORT, RENDERED: the cover and every section, from a
ReportData, through Jinja2 and WeasyPrint to a PDF.

    render_site_report_html(report_data, ...)      -> the HTML document
    render_site_report(report_data, ...)           -> (HTML, weasyprint Document),
                                                      laid out and refitted
    generate_site_report_pdf(report_data, path, ...) -> the PDF on disk
    generate_session_site_report_pdf(session_id, store, path, ...)
                                                    -> the PDF for a session

This replaced the narrated document the report job used to produce
(site-data-report-proposal.md); that generator and its Claude call are
retired. It carries all eight sections -- Site
overview, Climate, Landform, Water & hydrology, Access, Trees & forestry,
Soils & geology and Design -- the cover with its contents, the back
matter, and the foundation every section composes: the tokens,
the fonts, the page geometry, and the six reusable components as Jinja
macros under templates/report/components/. A section is a builder that
turns a ReportData into a dict of already-formatted values
(climate_section.build_climate_section) plus a template that composes the
six components and nothing else. NO SECTION TEMPLATE CONTAINS ITS OWN
STYLING: templates/report/report.css is the one stylesheet.

TOKENS, IN ONE PLACE. TOKENS below is the only location in the backend a
colour literal may appear. The stylesheet is a Jinja template that
receives them and declares each as a custom property on :root; every rule
reads var(--name). test_site_report.py greps the stylesheet and this
module for hex literals and expects hits only inside TOKENS. The values
are the frontend design guide's (keyline-designer-frontend/src/index.css)
-- the page is white and stock is used only for small filled areas.

FONTS. assets/fonts/ (vendored from the frontend, see its README) is
injected as absolute file URIs, so the stylesheet carries no path and the
render does not depend on WeasyPrint's base_url.

WEASYPRINT IS IMPORTED INSIDE generate_site_report_pdf(), api.py's reason:
it links libpango at import time, and at module scope one missing system
library would make this module -- and everything that imports it --
unimportable. render_site_report_html() needs only Jinja2 and can be
called, tested and diffed without a PDF engine present. WeasyPrint is
PINNED (requirements.txt): var(), WOFF2 loading and variable-font weights
were each verified on 70.0 and a resolver picking another release could
lose any of them silently.

THE COVER. "Site Data Report", the property label -- or, when none was
sent, the centroid the report data was fetched at, as coordinates -- the
acreage, the county and State (branch 17, when the geocoder answered), the
generation date, and THE CONTENTS: the outline's eight sections and the
back matter with page numbers, resolved by WeasyPrint's target-counter()
in the one render pass (verified at branch 17 step 0: no second pass).
Each section is wrapped in an anchor the contents link to.

A BLOCK PUSHED ONTO A PAGE OF ITS OWN IS REFITTED (branch 24). Every
section's page blocks start on a forced page break, so a block that does
not fit under the one before it takes a page to itself and leaves that
page nearly empty -- whatever the break rules, measured: splitting the
block leaves its tail alone instead, and the pages before branch 21 left
the caption alone. A few blocks are marked in their templates with the
one remedy that fits them back (data-refit, components/caption.html's
`captioned`): the block moved to head the section's next page, where
there is room -- the stream table onto the wetness page, the soil-test
sentence onto the last Soils page. render_site_report() lays the
document out once, finds the marked blocks that did not start on their
page block's first page (pushed_blocks), applies their remedies to the
SAME built sections -- no map is drawn twice -- and lays it out again,
keeping the second layout only if it has fewer pages, or as many and
fewer near-empty ones (a moved block can push a longer one in its place:
the reference parcel's wetness page is 96% full). A report whose blocks
all fit is laid out once and is exactly what it was.

THE BACK MATTER (branch 17, back_matter.py): the vintage table and the
methods note, built after every section from the footers and methods
structures the sections carry. The label and date also run in every section page's
footer beside the page number, through CSS string-set from two hidden
elements, so user input reaches the page margin as escaped text and never
as a CSS string.

THE REPORT JOB PRODUCES THIS. session_report.run_report_job() calls
generate_session_site_report_pdf(); session_report.error_payload() maps
this layer's failure shape (a REQUIRED report layer's failed_layer).
"""

import os
from datetime import date
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

import access_derivations
import access_section
import back_matter
import census_geography
import climate_section
import design_section
import landform_section
import overview_derivations
import overview_section
import parcel_data
import report_data as report_data_module
import report_progress
from report_progress import STAGE_PAGES, STAGE_REBUILD, STAGE_RECORDS, STAGE_TERRAIN
from report_outline import SECTION_OUTLINE
import session_cache
import session_manager
import soils_derivations
import soils_section
import trees_derivations
import trees_section
import water_derivations
import water_section
from report_outline import section_number

# --- tokens ------------------------------------------------------------
#
# THE ONE PLACE A COLOUR LITERAL LIVES. Names are the frontend's, values
# are the frontend's (src/index.css :root), plus `page`: the report's
# pages are white, with stock used only for small filled areas.
TOKENS = {
    "page": "#ffffff",
    "stock": "#f4f1ea",
    "rule": "#ddd6c8",
    "ink": "#2b2b26",
    "ink-muted": "#8a8477",
    "oxide": "#9c4a2f",
    # NEW ON BRANCH 6, and the first terrain colour in the palette: the
    # report map's contour lines and slope tints (report_map.py). A muted,
    # warm brown between ink and oxide in tone -- a printed contour line,
    # not a cartographic tan. Judged rendered on the Landform proof map.
    "terrain": "#7a5c3a",
    # NEW ON BRANCH 7, the report's first water colour: the water balance
    # diagram's precipitation line and surplus fill (report_chart.py). The
    # plate system reserves blue for water. Desaturated and mid-dark, a
    # tonal sibling of terrain brown (about the same lightness and
    # saturation, the hue turned to blue) -- a printed hydrology-bulletin
    # blue, not a cartographic cyan. Judged rendered on the Climate proof.
    "water": "#3f5d75",
    # NEW ON BRANCH 7, carried over from the frontend palette
    # (src/index.css --ochre, where it marks a live point): the water
    # balance's evaporation line and deficit fill. The frontend's value,
    # unchanged.
    "ochre": "#c99a2e",
    # NEW ON BRANCH 11, carried over from the frontend palette
    # (src/index.css --field, where it draws the boundary ring and the
    # map's geometry): the Trees section's canopy screen and its height
    # ramp. Trees are green in the plate system; this token is MAP-ONLY
    # here as it is there -- never a control, a rule or a tint on the
    # page. The frontend's value, unchanged.
    "field": "#4a5f3a",
    # NEW ON BRANCH 16, carried over from the frontend palette for the
    # layout map, which draws the interactive map's marks (design_section):
    # --halo, the pure white a map mark's halo and the excavated screen
    # are drawn in -- MAP-ONLY, as there; --tree, the tree zones' hatch;
    # and the two survey blues, embankment's wash and excavated's dots.
    # The frontend's values, unchanged.
    "halo": "#ffffff",
    "tree": "#52a466",
    "survey-embankment": "#6da4c6",
    "survey-excavated": "#3d5a6c",
    # NEW ON BRANCH 16, and the report's own: the layout map's NHD streams.
    # An existing feature rather than a design element, so in the palette's
    # desaturated register, not a cartographic cyan; lighter than both
    # survey blues so a stream is never read as a survey edge.
    # hsl(205, 30%, 74%): judged rendered against the embankment tint
    # (diagnose_layout_map_variants.py) over hsl(203, 28%, 70%), which sat on the
    # embankment's own edge, and hsl(203, 22%, 76%), which greyed out
    # over pasture.
    "stream": "#a9c0d1",
}

# --- fonts -------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
FONTS_DIRECTORY = os.path.join(_HERE, "assets", "fonts")
TEMPLATES_DIRECTORY = os.path.join(_HERE, "templates", "report")

# (family, weight declaration, file). Variable files declare their whole
# axis; the two Plex Mono statics declare one weight each.
FONT_FACES = (
    ("Bitter", "100 900", "bitter-latin-wght-normal.woff2"),
    ("Source Serif 4", "200 900", "source-serif-4-latin-wght-normal.woff2"),
    ("IBM Plex Mono", "400", "ibm-plex-mono-latin-400-normal.woff2"),
    ("IBM Plex Mono", "500", "ibm-plex-mono-latin-500-normal.woff2"),
)

COVER_TITLE = "Site Data Report"
COVER_EYEBROW = "Keyline Designer"


def font_faces(directory: str = FONTS_DIRECTORY) -> list:
    """The @font-face rows the stylesheet renders, each with an absolute
    file URI. Raises if a file is missing: a report that silently fell
    back to Liberation Serif would look finished and be wrong."""
    faces = []
    for family, weight, filename in FONT_FACES:
        path = os.path.join(directory, filename)
        if not os.path.exists(path):
            raise FileNotFoundError(f"report font missing: {path}")
        faces.append({"family": family, "weight": weight, "uri": "file://" + path})
    return faces


def jinja_environment(templates_directory: str = TEMPLATES_DIRECTORY) -> Environment:
    """AUTOESCAPE ON, for every template. The property label is user
    input; nothing rendered from it may reach the page unescaped."""
    return Environment(
        loader=FileSystemLoader(templates_directory),
        # HTML templates escape; the stylesheet template does not -- its
        # quotes are CSS, and its only interpolations are this module's
        # own tokens and font paths. base.html marks the rendered
        # stylesheet safe for that reason and for nothing else.
        autoescape=select_autoescape(
            enabled_extensions=("html", "htm", "xml"),
            disabled_extensions=("css",),
            default=True,
            default_for_string=True,
        ),
        trim_blocks=False,
        lstrip_blocks=False,
    )


def render_stylesheet(env: Optional[Environment] = None, fonts_directory: str = FONTS_DIRECTORY) -> str:
    env = env or jinja_environment()
    return env.get_template("report.css").render(tokens=TOKENS, fonts=font_faces(fonts_directory))


def cover_label(report_data, property_label: Optional[str]) -> str:
    """The label under the title: the property label the client sent, or
    the centroid the report data was fetched at when it sent none."""
    if property_label and property_label.strip():
        return property_label.strip()
    lat, lon = report_data.centroid
    return f"{abs(lat):.4f}° {'N' if lat >= 0 else 'S'}, {abs(lon):.4f}° {'E' if lon >= 0 else 'W'}"


def section_anchor(number: str) -> str:
    return f"section-{number.lower()}"


def build_sections(report_data, terrain=None, water=None, access=None, trees=None, soils=None, design=None,
                   overview=None) -> list:
    """Every section the report renders, in outline order. Climate from
    the report data; Landform from the session's terrain reads
    (landform_section.TerrainInputs) when the caller has a session to read
    -- the report-data-only path renders without it; Water (branch 9)
    from the session's water reads (water_derivations.WaterInputs) and the
    report data's Water blocks, handed Landform's flow pass so the report
    runs it ONCE; Access (branch 10) from the session's access reads
    (access_derivations.AccessInputs) and the report data's soil road
    ratings; Trees & forestry (branch 11) from the session's canopy reads
    (trees_derivations.TreesInputs) and the report data's forest type and
    woodland blocks; Soils & geology (branch 12) from the session's Layer
    1 soil reads (soils_derivations.SoilsInputs) and the report data's
    survey and geology blocks; Design (branch 13) from the session's
    committed Design Document and the report data's NAIP imagery
    (design_section.DesignInputs) -- the layout map and the design
    record. Site overview (branch 17) from its own inputs
    (overview_derivations.OverviewInputs) -- first, being I. Each section
    carries its own numeral from report_outline, so adding one renumbers
    nothing, and an anchor the cover's contents link to."""
    # Each builder is one progress unit (report_progress.plan_session_
    # report's "section.<name>"); a no-op outside a report job.
    def built(name, build, *args, **kwargs):
        with report_progress.unit(STAGE_TERRAIN, f"section.{name}"):
            section = build(*args, **kwargs)
        sections.append(section)
        return section

    sections = []
    if overview is not None:
        built("overview", overview_section.build_overview_section, overview, TOKENS)
    built("climate", climate_section.build_climate_section, report_data)
    if terrain is not None:
        landform = built("landform", landform_section.build_landform_section, terrain, TOKENS)
        if water is not None:
            built("water", water_section.build_water_section, water, TOKENS, flow=landform["derived"])
        if access is not None:
            built("access", access_section.build_access_section, access, TOKENS)
        if trees is not None:
            built("trees", trees_section.build_trees_section, trees, TOKENS)
        if soils is not None:
            built("soils", soils_section.build_soils_section, soils, TOKENS)
        if design is not None:
            built("design", design_section.build_design_section, design, TOKENS)
    for section in sections:
        section["anchor"] = section_anchor(section["number"])
    return sections


def build_contents(sections: list, back: Optional[dict]) -> list:
    """The outline's eight sections in order, each with its anchor when
    the report carries it, then the back matter."""
    present = {section["name"]: section["anchor"] for section in sections}
    contents = [{"number": section_number(name), "name": name, "anchor": present.get(name)} for name in SECTION_OUTLINE]
    if back is not None:
        contents.append({"number": None, "name": "Sources and methods", "anchor": "back-matter"})
    return contents


def parcel_acres_label(terrain) -> Optional[str]:
    """The cover's acreage, to one decimal -- the figure every acreage
    table sums to exactly. None without a session to read it from."""
    if terrain is None:
        return None
    return f"{round(terrain.parcel_acres, 1):,.1f}"


def render_site_report_html(
    report_data,
    property_label: Optional[str] = None,
    generated_on: Optional[date] = None,
    env: Optional[Environment] = None,
    fonts_directory: str = FONTS_DIRECTORY,
    terrain=None,
    water=None,
    access=None,
    trees=None,
    soils=None,
    design=None,
    overview=None,
    complete: bool = False,
) -> str:
    """The whole document as HTML, stylesheet inlined. `terrain` is the
    session's landform_section.TerrainInputs, or None for a report built
    from report data alone; `water` the session's water_derivations.
    WaterInputs, `access` its access_derivations.AccessInputs, `trees`
    its trees_derivations.TreesInputs and `soils` its
    soils_derivations.SoilsInputs, each rendered only beside
    `terrain`. `complete` is the whole report the job produces: the
    contents on the cover and the back matter after the last section --
    off for a render of some sections, which neither describes."""
    return _built_report(report_data, property_label, generated_on, env, fonts_directory, terrain, water, access, trees,
                         soils, design, overview, complete)[1]


def _built_report(report_data, property_label, generated_on, env, fonts_directory, terrain, water, access, trees, soils,
                  design, overview, complete) -> tuple:
    """(sections, the HTML, a function that renders the HTML again from the
    same sections) -- the sections built once."""
    env = env or jinja_environment()
    generated_on = generated_on or date.today()
    with report_progress.stage(STAGE_TERRAIN):
        sections = build_sections(report_data, terrain, water, access, trees, soils, design, overview)

    def html_for() -> str:
        return _render_document(env, fonts_directory, report_data, property_label, generated_on, terrain, sections,
                                complete)

    with report_progress.stage(STAGE_PAGES), report_progress.unit(STAGE_PAGES, "html"):
        return sections, html_for(), html_for


# The page blocks a section's template splits on: each starts a page.
PAGE_BLOCKS = ("section__figures", "section__structure", "section__detail", "section__classification")


def _boxes(box):
    yield box
    for child in getattr(box, "children", []) or []:
        yield from _boxes(child)


def pushed_blocks(document) -> list:
    """Every data-refit block that did not start on its page block's first
    page, as its data-refit value ("<section>.<remedy>"), in document order:
    the page before it ran out, and it was carried to a page of its own."""
    first_page = {}
    for number, page in enumerate(document.pages, start=1):
        for box in _boxes(page._page_box):
            element = getattr(box, "element", None)
            if element is not None and element not in first_page:
                first_page[element] = number
    root = next((e for e in first_page if e.tag == "html"), None)
    if root is None:
        return []
    pushed = []

    def visit(element, block):
        classes = (element.get("class") or "").split()
        if any(name in classes for name in PAGE_BLOCKS):
            block = element
        refit = element.get("data-refit")
        if refit and block is not None and element in first_page and block in first_page \
                and first_page[element] > first_page[block]:
            pushed.append(refit)
        for child in element:
            visit(child, block)

    visit(root, None)
    return pushed


def refit_sections(sections: list, pushed: list) -> list:
    """Apply each pushed block's remedy to its section, in place: a remedy
    is a word in section["refit"] the template reads. Returns the remedies
    applied."""
    applied = []
    for refit in pushed:
        name, _, remedy = refit.partition(".")
        section = next((s for s in sections if s.get("template", "").split(".")[0] == name), None)
        if section is None or remedy in section.get("refit", ()):
            continue
        section["refit"] = list(section.get("refit", ())) + [remedy]
        applied.append(refit)
    return applied


def lay_out(html: str, sections: list, html_for):
    """(HTML, Document): laid out once, and again with the pushed blocks'
    remedies applied if any block was pushed -- the second layout kept only
    if it costs less (_page_cost)."""
    from weasyprint import HTML

    document = HTML(string=html, base_url=TEMPLATES_DIRECTORY).render()
    before = [(section, dict(section)) for section in sections]
    if not refit_sections(sections, pushed_blocks(document)):
        return html, document
    refitted_html = html_for()
    refitted = HTML(string=refitted_html, base_url=TEMPLATES_DIRECTORY).render()
    if _page_cost(refitted) < _page_cost(document):
        return refitted_html, refitted
    # Not kept: the sections go back to what the first layout rendered.
    for section, fields in before:
        section.clear()
        section.update(fields)
    return html, document


# A page with less text than this is a block alone on it
# (test_whole_report's standing check holds the report to the same figure).
SPARSE_PAGE_CHARS = 600


def page_characters(document) -> list:
    """The characters each laid-out page sets -- its text boxes, not its
    running footer."""
    counts = []
    for page in document.pages:
        body = [c for c in page._page_box.children if type(c).__name__ != "MarginBox"]
        counts.append(sum(len(getattr(b, "text", "") or "") for c in body for b in _boxes(c)
                          if type(b).__name__ == "TextBox"))
    return counts


def _page_cost(document) -> tuple:
    """Fewer pages first, then fewer sparse ones: a refit that moves a
    block and pushes a longer one in its place keeps the count and trades
    a near-empty page for a fuller one."""
    return len(document.pages), sum(1 for c in page_characters(document) if c < SPARSE_PAGE_CHARS)


def render_site_report(
    report_data,
    property_label: Optional[str] = None,
    generated_on: Optional[date] = None,
    env: Optional[Environment] = None,
    fonts_directory: str = FONTS_DIRECTORY,
    terrain=None,
    water=None,
    access=None,
    trees=None,
    soils=None,
    design=None,
    overview=None,
    complete: bool = False,
) -> tuple:
    """(HTML, weasyprint Document): the document laid out, with any block
    pushed onto a page of its own refitted (lay_out). The layout is the
    pages stage's "layout" unit, both passes of it."""
    sections, html, html_for = _built_report(report_data, property_label, generated_on, env, fonts_directory, terrain,
                                             water, access, trees, soils, design, overview, complete)
    with report_progress.stage(STAGE_PAGES), report_progress.unit(STAGE_PAGES, "layout"):
        return lay_out(html, sections, html_for)


def _render_document(env, fonts_directory, report_data, property_label, generated_on, terrain, sections,
                     complete) -> str:
    """The cover, the back matter and the template, around sections
    already built -- the "html" unit of the pages stage."""
    # The back matter needs a Layer 1 retrieval date, which only a session has.
    back = (back_matter.build_back_matter(sections, report_data, terrain.retrieved_on)
            if complete and terrain is not None else None)
    cover = {
        "title": COVER_TITLE,
        "eyebrow": COVER_EYEBROW,
        "label": cover_label(report_data, property_label),
        "acres": parcel_acres_label(terrain),
        "generated_on": climate_section.format_generated_on(generated_on),
        "meta": f"Generated {climate_section.format_generated_on(generated_on)}",
        "county": census_geography.county_state_label(report_data.county_state),
        "contents": build_contents(sections, back) if complete else None,
    }
    return env.get_template("base.html").render(
        stylesheet=render_stylesheet(env, fonts_directory),
        cover=cover,
        sections=sections,
        back=back,
    )


def generate_site_report_pdf(
    report_data,
    output_path: str,
    property_label: Optional[str] = None,
    generated_on: Optional[date] = None,
    terrain=None,
    water=None,
    access=None,
    trees=None,
    soils=None,
    design=None,
    overview=None,
    complete: bool = False,
) -> str:
    """HTML -> PDF on disk. Returns output_path. No network: the fonts are
    local files and the data is already in hand."""
    # HTML.write_pdf() is render() then Document.write_pdf() (weasyprint
    # 70); called as its halves so layout and the write are each a
    # progress unit -- the layout one pass, or two when a block is refitted.
    _, document = render_site_report(
        report_data, property_label=property_label, generated_on=generated_on, terrain=terrain, water=water,
        access=access, trees=trees, soils=soils, design=design, overview=overview, complete=complete,
    )
    with report_progress.stage(STAGE_PAGES), report_progress.unit(STAGE_PAGES, "write"):
        document.write_pdf(output_path)
    return output_path


def generate_session_site_report_pdf(
    session_id: str,
    store,
    output_path: str,
    property_label: Optional[str] = None,
    report_fetch_cache=None,
    generated_on: Optional[date] = None,
    fetch_cache=None,
    cache=None,
) -> str:
    """
    The site data report for ONE SESSION: the boundary off the Design
    Document, the report data through the report fetch cache (fetched
    exactly once per boundary), the terrain off the session context (the
    warm-up's own products -- session_manager.get_session_context, a cache
    hit or a rebuild, never a recompute here), the PDF at output_path.

    Reads the document and the context only. The seven inventory sections
    describe the property and consult no step's commit state; the Design
    section (VIII) reads the committed steps off the DOCUMENT -- never the
    session cache -- so an evicted cache cannot fail it
    (design_record.py). The report is offered only once every step is
    committed; design_record raises for a step that is not. Raises
    report_data.ReportDataIncompleteError for a REQUIRED layer that fails,
    which session_report.error_payload() maps to the failed_layer shape.
    """
    document = store.get(session_id)
    if report_fetch_cache is None:
        report_fetch_cache = report_data_module.default_report_fetch_cache()
    # The same defaults session_manager.get_session_context() resolves, so
    # the plan below asks the caches that call will actually read.
    if fetch_cache is None:
        fetch_cache = session_cache.DEFAULT_FETCH_CACHE
    if cache is None:
        cache = session_cache.DEFAULT_SESSION_CACHE
    # DESIGN ONLY FOR A FINISHED DESIGN. The report is offered once every
    # step is committed (the job's precondition, session_report); called
    # before that, this still renders the inventory and leaves the design
    # out, rather than a record with an unfinished step in it.
    finished = all(document["steps"][step]["status"] == "committed" for step in document["steps"])
    boundary = document["boundary"]
    context_cached = session_id in cache

    # THE PLAN, FIXED BEFORE THE FIRST FETCH (report_progress's docstring):
    # what the three caches hold now decides whether the report fetches and
    # a rebuild are in the total at all.
    progress = report_progress.current()
    if progress is not None and not progress.planned:
        progress.set_plan(report_progress.plan_session_report(
            report_data_module.REPORT_FETCH_LAYERS,
            parcel_data.FETCH_LAYERS,
            report_cached=_holds(report_fetch_cache, boundary),
            context_cached=context_cached,
            layer1_cached=_holds(fetch_cache, boundary),
            design=finished,
        ))

    with report_progress.stage(STAGE_RECORDS):
        data = report_fetch_cache.get_or_fetch(boundary)
    report_progress.settle(STAGE_RECORDS)
    with report_progress.stage(STAGE_TERRAIN if context_cached else STAGE_REBUILD):
        context = session_manager.get_session_context(session_id, store, fetch_cache=fetch_cache, cache=cache)
    report_progress.settle(STAGE_REBUILD)

    def derived(name, derive, *args):
        with report_progress.unit(STAGE_TERRAIN, f"inputs.{name}"):
            return derive(*args)

    with report_progress.stage(STAGE_TERRAIN):
        terrain = derived("terrain", landform_section.terrain_inputs_from_context, context, document)
        water = derived("water", water_derivations.water_inputs_from_context, context, document, data)
        access = derived("access", access_derivations.access_inputs_from_context, context, document, data)
        trees = derived("trees", trees_derivations.trees_inputs_from_context, context, document, data)
        soils = derived("soils", soils_derivations.soils_inputs_from_context, context, document, data)
        overview = derived("overview", overview_derivations.overview_inputs_from_context, context, document, data)
        design = (derived("design", design_section.design_inputs_from_context, context, document, data)
                  if finished else None)
    return generate_site_report_pdf(
        data, output_path, property_label=property_label, generated_on=generated_on, terrain=terrain, water=water, access=access,
        trees=trees, soils=soils, design=design, overview=overview, complete=True,
    )


def _holds(cache, boundary) -> bool:
    """Does this fetch cache already hold `boundary`? A cache without a
    contains() (a test's stand-in) is asked nothing and planned as a miss:
    a planned fetch that never ticks is settled when the stage returns,
    so the bar is late rather than wrong."""
    contains = getattr(cache, "contains", None)
    return bool(contains(boundary)) if callable(contains) else False
