"""
diagnose_site_report_render.py

RENDERS THE SITE DATA REPORT FROM THE FIXTURE, OFFLINE, AND SHOWS ITS WORK
-- the phase 2 verification of site-data-report-proposal.md:

    python3 diagnose_site_report_render.py [out_dir]

  * the HTML and the PDF, from daymet_reference_fixture.csv and the
    reference parcel, with no network (offline_harness is installed);
  * every page rasterised to PNG (PyMuPDF, if importable -- it is not a
    project dependency; without it this prints the PDF's size and stops);
  * the fonts the PDF embeds, by name -- `pdffonts` without poppler;
  * DECIMAL ALIGNMENT in the monthly table, measured: for every month
    column, the right edge of every numeric cell, and the x of every
    decimal point among the cells that carry one, across rows whose values
    differ in digit count (36 beside 3.4 beside 683);
  * the footer's citation against the fixture header's, verbatim.

Prints what it measured. Nothing here is a test; test_site_report.py
asserts the parts of this that a test can hold.
"""

import os
import sys
import tempfile
from datetime import date

import offline_harness

offline_harness.install()

from daymet_data import parse_daymet_csv
from reference_fixture import REAL_BOUNDARY
from report_data import report_data_from_daily
from site_report import generate_site_report_pdf, render_site_report_html

FIXTURE = "daymet_reference_fixture.csv"


def main(out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    with open(FIXTURE, encoding="utf-8") as handle:
        daily = parse_daymet_csv(handle.read())
    data = report_data_from_daily(REAL_BOUNDARY, daily)
    generated_on = date(2026, 9, 21)

    html_path = os.path.join(out_dir, "site-data-report.html")
    with open(html_path, "w", encoding="utf-8") as handle:
        handle.write(render_site_report_html(data, generated_on=generated_on))
    pdf_path = generate_site_report_pdf(data, os.path.join(out_dir, "site-data-report.pdf"), generated_on=generated_on)
    print(f"HTML: {html_path}")
    print(f"PDF:  {pdf_path}  ({os.path.getsize(pdf_path):,} bytes)")
    print(f"network requests refused: {len(offline_harness.refused())}")

    try:
        import pymupdf
    except ImportError:
        print("PyMuPDF not importable -- pages not rasterised, fonts not listed, alignment not measured.")
        return 0

    doc = pymupdf.open(pdf_path)
    print(f"pages: {len(doc)}")

    fonts = {}
    for page in doc:
        for entry in page.get_fonts(full=True):
            fonts[entry[3]] = entry[2]
    print("embedded fonts (pdffonts equivalent):")
    for name in sorted(fonts):
        print(f"  {name:<40} {fonts[name]}")

    for index, page in enumerate(doc, start=1):
        png = os.path.join(out_dir, f"page-{index}.png")
        page.get_pixmap(dpi=110).save(png)
        print(f"rasterised page {index} -> {png}")

    # DECIMAL ALIGNMENT. Every numeric cell is a span in the data face.
    # Group the spans of the section page by their column (right edge, to a
    # tolerance) and report per column: the spread of right edges, and the
    # spread of decimal-point x positions among the cells that have one.
    section = doc[1]
    spans = []
    for block in section.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = span["text"].strip()
                if "Plex" in span["font"] and text and text[0].isdigit():
                    spans.append((span["bbox"], text, span["size"]))
    # The table's own size only: key figures are larger, the eyebrow and the
    # footer's data spans smaller (report.css's --text-table).
    table_spans = [s for s in spans if abs(s[2] - 8.75) < 0.1]
    columns = {}
    for bbox, text, _ in table_spans:
        right = round(bbox[2], 0)
        key = min(columns, key=lambda k: abs(k - right)) if columns and min(abs(k - right) for k in columns) < 2.0 else right
        columns.setdefault(key, []).append((bbox, text))
    print(f"table numeric cells: {len(table_spans)} in {len(columns)} columns")
    worst_right = 0.0
    worst_decimal = 0.0
    for key in sorted(columns):
        cells = columns[key]
        rights = [b[2] for b, _ in cells]
        decimals = []
        for bbox, text in cells:
            if "." in text:
                # Mono: every glyph the same advance, so the decimal's x is
                # the right edge minus the glyphs after it, plus half a glyph.
                advance = (bbox[2] - bbox[0]) / len(text)
                after = len(text) - text.index(".") - 1
                decimals.append(bbox[2] - after * advance - advance / 2)
        spread_r = max(rights) - min(rights)
        spread_d = (max(decimals) - min(decimals)) if len(decimals) > 1 else 0.0
        worst_right = max(worst_right, spread_r)
        worst_decimal = max(worst_decimal, spread_d)
        widths = sorted({len(t) for _, t in cells})
        print(
            f"  column @x={key:6.1f}: {len(cells)} cells, digit widths {widths}, "
            f"right-edge spread {spread_r:.2f} pt, decimal spread {spread_d:.2f} pt"
        )
    print(f"worst right-edge spread {worst_right:.2f} pt; worst decimal spread {worst_decimal:.2f} pt")

    text = "".join(page.get_text() for page in doc)
    citation = daily["citation"]
    normalised = " ".join(text.split())
    print(f"footer carries the fixture's citation verbatim: {' '.join(citation.split()) in normalised}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="site-report-")))
