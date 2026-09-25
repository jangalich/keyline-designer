"""
diagnose_whole_report.py

THE WHOLE REPORT FOR THE REFERENCE PARCEL, RENDERED TO PNG, EVERY PAGE --
branch 17 phase 2's deliverable, and the first time all eight sections,
the contents and the back matter exist at once:

    python3 diagnose_whole_report.py [out_dir] [dpi]

Writes site-report.pdf and page-NN.png for every page (PyMuPDF), and
prints each page's first line, the contents' page numbers, the vintage
table's rows and the boxes past the content width. Offline by
construction (offline_harness); the inputs are whole_report_fixture's.
"""

import os
import sys
import tempfile

import offline_harness

offline_harness.install()

import back_matter  # noqa: E402
import report_layout  # noqa: E402
import site_report  # noqa: E402
import whole_report_fixture as w  # noqa: E402


def main(out_dir: str, dpi: int) -> int:
    import pymupdf

    os.makedirs(out_dir, exist_ok=True)
    bundle = w.inputs()
    html, document, pdf = w.render(bundle)
    pdf_path = os.path.join(out_dir, "site-report.pdf")
    with open(pdf_path, "wb") as handle:
        handle.write(pdf)
    opened = pymupdf.open(stream=pdf, filetype="pdf")
    print(f"{pdf_path}: {len(opened)} pages, {len(pdf):,} bytes")
    for index, page in enumerate(opened, start=1):
        path = os.path.join(out_dir, f"page-{index:02d}.png")
        page.get_pixmap(dpi=dpi).save(path)
        first = next((line for line in page.get_text().splitlines() if line.strip()), "")
        print(f"  page {index:>2}  {first[:70]:<70} -> {os.path.basename(path)}")
    sections = site_report.build_sections(bundle["data"], bundle["terrain"], bundle["water"], bundle["access"],
                                          bundle["trees"], bundle["soils"], bundle["design"], bundle["overview"])
    back = back_matter.build_back_matter(sections, bundle["data"], bundle["terrain"].retrieved_on)
    print("\nVINTAGE TABLE")
    for row in back["vintage"]["rows"]:
        print(f"  {row['source']:<42} {row['version'][:44]:<44} {row['retrieved']:<24} {row['used_in']}")
    print(f"  unmatched footer lines: {back['vintage']['unmatched']}")
    print(f"\nboxes past the content width: {report_layout.overflowing_boxes(document)}")
    print(offline_harness.summary())
    return 0


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="whole-report-")
    sys.exit(main(out, int(sys.argv[2]) if len(sys.argv) > 2 else 110))
