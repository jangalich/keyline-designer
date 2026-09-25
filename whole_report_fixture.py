"""
whole_report_fixture.py

THE WHOLE REPORT, OFFLINE: every section's inputs for the reference
parcel, from the fixture chain, and the render.

    inputs()          -> {'data', 'terrain', 'water', 'access', 'trees',
                          'soils', 'design', 'overview', 'context', 'document'}
    render_html(**kw) -> the document's HTML
    render(**kw)      -> (html, weasyprint Document, pdf bytes)

Branch 17 is the first time all eight sections and the back matter exist
at once; this is the one place that assembles them, so the whole-report
test and the whole-report diagnostic render the same document.

THE SESSION is overview_reference_fixture's (the soils chain: the real DEM,
canopy, SSURGO rows, streams and roads), and its report data carries every
report layer. THE DESIGN is design_record_fixture.json's committed "full"
document, read the way the report job reads it
(design_section.design_inputs_from_context) against that same session --
the reference session's own document has no committed design, and the
report job only renders Design for a finished one.
"""

import json
import os
from datetime import date

import access_derivations as ad
import design_section as ds
import landform_section
import overview_derivations as od
import overview_reference_fixture as fixture
import site_report
import soils_derivations as sd
import trees_derivations as td
import water_derivations as wd

HERE = os.path.dirname(os.path.abspath(__file__))
GENERATED_ON = date(2026, 9, 25)
PROPERTY_LABEL = "5614 N Montour Rd"


def design_document() -> dict:
    with open(os.path.join(HERE, "design_record_fixture.json"), encoding="utf-8") as handle:
        return json.load(handle)["full"]["document"]


def inputs(**report_overrides) -> dict:
    data = fixture.report_data(**report_overrides)
    with fixture.Harness():
        session = fixture.Session()
        context = session.context()
        document = session.stored()
        result = {
            "data": data,
            "context": context,
            "document": document,
            "terrain": landform_section.terrain_inputs_from_context(context, document),
            "water": wd.water_inputs_from_context(context, document, data),
            "access": ad.access_inputs_from_context(context, document, data),
            "trees": td.trees_inputs_from_context(context, document, data),
            "soils": sd.soils_inputs_from_context(context, document, data),
            "overview": od.overview_inputs_from_context(context, document, data),
        }
    result["design"] = ds.design_inputs_from_context(context, design_document(), data)
    return result


def render_html(bundle: dict = None, property_label: str = PROPERTY_LABEL) -> str:
    bundle = bundle or inputs()
    return site_report.render_site_report_html(
        bundle["data"], property_label=property_label, generated_on=GENERATED_ON, terrain=bundle["terrain"],
        water=bundle["water"], access=bundle["access"], trees=bundle["trees"], soils=bundle["soils"],
        design=bundle["design"], overview=bundle["overview"], complete=True,
    )


def render(bundle: dict = None, property_label: str = PROPERTY_LABEL) -> tuple:
    from weasyprint import HTML

    html = render_html(bundle, property_label)
    document = HTML(string=html, base_url=site_report.TEMPLATES_DIRECTORY).render()
    return html, document, document.write_pdf()
