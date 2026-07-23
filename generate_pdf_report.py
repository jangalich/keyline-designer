"""
generate_pdf_report.py

Assembles the final client-facing deliverable: the existing narrative
Scale of Permanence report (generate_full_report.py, completely
unchanged -- same prompt, same content, same eight sections) followed by
the new static layout map (render_layout_map.py) as a single, full-bleed
final page -- one PDF file, not two separate outputs.

    boundary --> generate_full_report.generate_full_report() (narrative, unchanged)
             --> render_layout_map.render_layout_map() (final-page image)
             --> markdown (narrative markdown -> HTML)
             --> weasyprint (HTML/CSS -> PDF)
             --> PDF file on disk

Uses `markdown` (a well-established markdown-to-HTML library) to convert
the narrative report's own markdown structure directly to HTML rather
than reformatting it by hand, and `weasyprint` (a well-established HTML/
CSS-to-PDF renderer) rather than manual low-level PDF layout. CSS Paged
Media's named-page mechanism (see MAP_PAGE_NAME below) gives the map page
its own zero-margin page box while every narrative page keeps normal
print margins -- both page kinds are declared in the same stylesheet, no
separate document/merge step needed.

This does NOT wire the frontend's "Generate Report" button to PDF output
-- see this module's __main__ block and api.py's /api/generate-report-pdf
endpoint for how to call it directly instead.
"""

import os
import tempfile
from typing import Optional

import markdown as markdown_lib
from weasyprint import HTML

from dem_data import get_dem_for_boundary
from generate_full_report import generate_full_report
from render_layout_map import fetch_layout_layers, render_layout_map

MAP_PAGE_NAME = "layoutMapPage"

REPORT_CSS = f"""
@page {{
    size: Letter;
    margin: 0.9in 0.85in 0.8in 0.85in;
    @bottom-center {{
        content: counter(page);
        font-family: Arial, Helvetica, sans-serif;
        font-size: 9pt;
        color: #777;
    }}
}}
@page {MAP_PAGE_NAME} {{
    size: Letter;
    margin: 0;
    @bottom-center {{ content: ""; }}
}}
body {{
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 11pt;
    line-height: 1.55;
    color: #1a1a1a;
}}
h1, h2, h3, h4 {{
    font-family: Arial, Helvetica, sans-serif;
    color: #14401d;
}}
h1 {{ font-size: 19pt; border-bottom: 2px solid #14401d; padding-bottom: 6px; margin-top: 1.4em; }}
h2 {{ font-size: 15pt; margin-top: 1.3em; }}
h3 {{ font-size: 12.5pt; }}
p {{ margin: 0.6em 0; text-align: justify; }}
ul, ol {{ margin: 0.5em 0; padding-left: 1.4em; }}
.cover {{
    text-align: center;
    margin-top: 2.2in;
}}
.cover h1 {{
    border: none;
    font-size: 27pt;
    margin: 0;
}}
.cover .subtitle {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 12.5pt;
    color: #555;
    margin-top: 0.4em;
}}
.cover .tagline {{
    font-family: Arial, Helvetica, sans-serif;
    font-size: 10.5pt;
    color: #888;
    margin-top: 1.6in;
}}
.narrative {{
    break-before: page;
}}
.map-page {{
    page: {MAP_PAGE_NAME};
    break-before: page;
    margin: 0;
    padding: 0;
}}
.map-page img {{
    width: 8.5in;
    height: 11in;
    display: block;
}}
"""


def _narrative_html(report_markdown: str) -> str:
    """Converts the narrative report's own markdown structure straight to
    HTML -- no reformatting of its content."""
    return markdown_lib.markdown(report_markdown, extensions=["extra", "sane_lists"])


def _build_html_document(report_markdown: str, map_image_path: str, property_label: str) -> str:
    narrative_body = _narrative_html(report_markdown)
    # file:// URI so weasyprint can load the local PNG directly, no HTTP
    # round-trip for an image this module just wrote to disk itself.
    map_image_uri = "file://" + os.path.abspath(map_image_path)

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>{REPORT_CSS}</style>
</head>
<body>
<div class="cover">
    <h1>Scale of Permanence Report</h1>
    <div class="subtitle">{property_label}</div>
    <div class="tagline">Regenerative land-use analysis and proposed layout</div>
</div>
<div class="narrative">
{narrative_body}
</div>
<div class="map-page">
    <img src="{map_image_uri}" alt="Proposed farm layout map">
</div>
</body>
</html>"""


def generate_full_report_pdf(
    boundary_coordinates: list[tuple[float, float]],
    output_path: str,
    property_label: str = "Property Design Report",
    map_image_path: Optional[str] = None,
) -> str:
    """
    Runs the full pipeline for a given property boundary and writes the
    assembled PDF (narrative report + final full-page layout map) to
    output_path. Returns output_path.

    map_image_path (optional): where to write the intermediate map PNG.
    Defaults to a temp file next to output_path's directory; pass a real
    path if you want to keep/inspect the image separately from the PDF.
    """
    if map_image_path is None:
        map_image_path = os.path.join(
            tempfile.gettempdir(), f"{os.path.splitext(os.path.basename(output_path))[0]}_layout_map.png"
        )

    print("Generating narrative Scale of Permanence report...")
    report_markdown = generate_full_report(boundary_coordinates)
    print("  Narrative report generated.\n")

    print("Fetching layout layers and rendering the final static map page...")
    dem = get_dem_for_boundary(boundary_coordinates)
    layers = fetch_layout_layers(boundary_coordinates, dem=dem)
    render_layout_map(boundary_coordinates, map_image_path, layers=layers)
    print(f"  Map image written to {map_image_path}\n")

    print("Assembling final PDF...")
    html_document = _build_html_document(report_markdown, map_image_path, property_label)
    HTML(string=html_document, base_url=os.path.dirname(os.path.abspath(output_path)) or ".").write_pdf(output_path)
    print(f"  PDF written to {output_path}\n")

    return output_path


if __name__ == "__main__":
    # The user's real, drawn property boundary -- same one every other
    # module's own __main__ block uses.
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    try:
        output = generate_full_report_pdf(
            property_boundary,
            "scale_of_permanence_report.pdf",
            property_label="5614 N Montour Rd, Gibsonia, PA 15044",
        )
        print(f"Done: {output}")
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS/USDA services and "
            "the USGSImageryOnly tile service, plus ANTHROPIC_API_KEY for the "
            "narrative report step -- not a fully sandboxed environment."
        )
