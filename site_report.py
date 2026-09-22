"""
site_report.py

THE SITE DATA REPORT, RENDERED: the cover and every section, from a
ReportData, through Jinja2 and WeasyPrint to a PDF.

    render_site_report_html(report_data, ...)      -> the HTML document
    generate_site_report_pdf(report_data, path, ...) -> the PDF on disk
    generate_session_site_report_pdf(session_id, store, path, ...)
                                                    -> the PDF for a session

This is the replacement for generate_pdf_report.py's narrated document
(site-data-report-proposal.md). It carries Climate, Landform and Water &
hydrology, and the foundation every later section composes: the tokens,
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
sent, the centroid the report data was fetched at, as coordinates -- and
the generation date. The label and date also run in every section page's
footer beside the page number, through CSS string-set from two hidden
elements, so user input reaches the page margin as escaped text and never
as a CSS string.

NOT YET WIRED INTO THE REPORT JOB. session_report.run_report_job() still
produces generate_pdf_report.py's narrated document; this module's
generate_session_site_report_pdf() is the Python-callable entry the job
switches to when the report has enough sections to replace it (see
site-data-report-proposal.md's build sequence). session_report.
error_payload() already knows this layer's failure shape.
"""

import os
from datetime import date
from typing import Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

import climate_section
import landform_section
import report_data as report_data_module
import session_manager
import water_derivations
import water_section

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


def build_sections(report_data, terrain=None, water=None) -> list:
    """Every section the report renders, in outline order. Climate from
    the report data; Landform from the session's terrain reads
    (landform_section.TerrainInputs) when the caller has a session to read
    -- the report-data-only path renders without it; Water (branch 9)
    from the session's water reads (water_derivations.WaterInputs) and the
    report data's Water blocks, handed Landform's flow pass so the report
    runs it ONCE. Each section carries its own numeral from
    report_outline, so adding one renumbers nothing."""
    sections = [climate_section.build_climate_section(report_data)]
    if terrain is not None:
        landform = landform_section.build_landform_section(terrain, TOKENS)
        sections.append(landform)
        if water is not None:
            sections.append(water_section.build_water_section(water, TOKENS, flow=landform["derived"]))
    return sections


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
) -> str:
    """The whole document as HTML, stylesheet inlined. `terrain` is the
    session's landform_section.TerrainInputs, or None for a report built
    from report data alone; `water` the session's water_derivations.
    WaterInputs, rendered only beside `terrain`."""
    env = env or jinja_environment()
    generated_on = generated_on or date.today()
    cover = {
        "title": COVER_TITLE,
        "eyebrow": COVER_EYEBROW,
        "label": cover_label(report_data, property_label),
        "acres": parcel_acres_label(terrain),
        "generated_on": climate_section.format_generated_on(generated_on),
        "meta": f"Generated {climate_section.format_generated_on(generated_on)}",
    }
    return env.get_template("base.html").render(
        stylesheet=render_stylesheet(env, fonts_directory),
        cover=cover,
        sections=build_sections(report_data, terrain, water),
    )


def generate_site_report_pdf(
    report_data,
    output_path: str,
    property_label: Optional[str] = None,
    generated_on: Optional[date] = None,
    terrain=None,
    water=None,
) -> str:
    """HTML -> PDF on disk. Returns output_path. No network: the fonts are
    local files and the data is already in hand."""
    from weasyprint import HTML

    html = render_site_report_html(
        report_data, property_label=property_label, generated_on=generated_on, terrain=terrain, water=water
    )
    HTML(string=html, base_url=TEMPLATES_DIRECTORY).write_pdf(output_path)
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

    Reads the document and the context only -- the site inventory
    describes the property, not the design, so no step's commit state is
    consulted here. The design record (site-data-report-proposal.md,
    section 3 of the report) will add that read when it is built. Raises
    report_data.ReportDataIncompleteError for a REQUIRED layer that fails,
    which session_report.error_payload() maps to the failed_layer shape.
    """
    document = store.get(session_id)
    if report_fetch_cache is None:
        report_fetch_cache = report_data_module.default_report_fetch_cache()
    data = report_fetch_cache.get_or_fetch(document["boundary"])
    context = session_manager.get_session_context(session_id, store, fetch_cache=fetch_cache, cache=cache)
    terrain = landform_section.terrain_inputs_from_context(context, document)
    water = water_derivations.water_inputs_from_context(context, document, data)
    return generate_site_report_pdf(
        data, output_path, property_label=property_label, generated_on=generated_on, terrain=terrain, water=water
    )
