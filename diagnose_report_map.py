"""
diagnose_report_map.py

THE REPORT MAP'S PROOF RENDER, judged as an image -- branch 6, phase 1 of
site-data-report-proposal.md's build sequence:

    python3 diagnose_report_map.py [out_dir]

  * the map on its own -- the boundary, contours from a DEM, north arrow,
    scale bar, an empty legend slot -- as SVG, and as PNG via WeasyPrint;
  * the same map embedded in a test page with the report's stylesheet,
    a heading and a paragraph, as PDF and PNG;
  * the measurements: the boundary's drawn aspect ratio against its true
    UTM extent; the scale bar's drawn length x metres-per-unit against the
    feet it claims; the fonts the PDF embeds; that the PDF holds no raster
    image; the relief, the contour interval chosen and the line count;
    the interval rule's answer for a flatter and a steeper parcel;
  * a swatch row of terrain-brown candidates beside the proposed token,
    for the eye. Those candidates are literals IN THIS SCRIPT ONLY -- the
    renderer carries none, and test_report_map.py holds it to that.

THE DEM. No real 3DEP raster is checked in and the sandbox has no network,
so the default terrain is roads_step_fixture's synthetic DEM over the REAL
reference boundary (a 4% bench with an incised drainage and flanking
levees, 5 m cells). The contours are real contourpy output over that
array; they are not the parcel's real contours. Pass a saved DEM as
`--dem path.npz` (array, resolution_meters, origin_x, origin_y, crs) to
render real terrain.
"""

import os
import sys
import tempfile
from xml.dom import minidom

import numpy as np

import offline_harness

offline_harness.install()

import site_report
from reference_fixture import BOUNDARY_POLYGON_UTM
from report_map import METERS_PER_FOOT, contour_interval_ft, contour_layers, parcel_contours, render_map

# Candidates for the eye, beside the proposed token. LITERALS HERE ONLY.
BROWN_CANDIDATES = (
    ("layout map's contour brown", "#6B4423"),
    ("proposed --terrain", site_report.TOKENS["terrain"]),
    ("lighter sibling", "#8c6b4a"),
    ("greyer sibling", "#726250"),
)


def load_dem(path):
    if path:
        data = np.load(path, allow_pickle=True)
        return {
            "array": data["array"].astype(np.float32),
            "resolution_meters": tuple(float(v) for v in data["resolution_meters"]),
            "origin_x": float(data["origin_x"]),
            "origin_y": float(data["origin_y"]),
            "crs": str(data["crs"]),
        }
    import roads_step_fixture

    return roads_step_fixture._build_dem()


def main(out_dir: str, dem_path=None) -> int:
    os.makedirs(out_dir, exist_ok=True)
    dem = load_dem(dem_path)
    tokens = site_report.TOKENS

    contours = parcel_contours(dem, BOUNDARY_POLYGON_UTM)
    print(
        f"relief {contours['relief_ft']:.1f} ft ({contours['min_ft']:.0f}-{contours['max_ft']:.0f} ft); "
        f"interval {contours['interval_ft']} ft; {contours['line_count']} lines, {contours['index_count']} index"
    )
    print("interval rule:", ", ".join(f"relief {r} ft -> {contour_interval_ft(r)} ft" for r in (10, 25, 40, 75, 150, 300, 600)))

    result = render_map(BOUNDARY_POLYGON_UTM, contour_layers(contours), tokens)
    svg = result["svg"]
    minidom.parseString(svg)  # well-formed
    svg_path = os.path.join(out_dir, "report-map.svg")
    with open(svg_path, "w", encoding="utf-8") as handle:
        handle.write(svg)
    print(f"SVG: {svg_path} ({len(svg):,} bytes)")

    # ASPECT RATIO: drawn bbox against UTM extent.
    minx, miny, maxx, maxy = result["extent_utm"]
    x0, y0, x1, y1 = result["drawn_bbox"]
    true_ratio = (maxx - minx) / (maxy - miny)
    drawn_ratio = (x1 - x0) / (y1 - y0)
    print(f"aspect ratio: UTM extent {true_ratio:.5f}, drawn {drawn_ratio:.5f}, difference {abs(true_ratio - drawn_ratio):.2e}")
    print(f"metres per unit {result['meters_per_unit']:.4f}; extent {maxx - minx:.1f} x {maxy - miny:.1f} m")

    # SCALE BAR: drawn units x metres per unit, in feet, against the claim.
    bar = result["scale_bar"]
    implied_ft = bar["units"] * result["meters_per_unit"] / METERS_PER_FOOT
    print(f"scale bar: claims {bar['feet']} ft, drawn {bar['units']:.2f} units -> {implied_ft:.4f} ft")

    # THE PDFs. WeasyPrint and PyMuPDF are both present where this runs;
    # pymupdf is not a project dependency and is imported here only.
    from weasyprint import HTML

    stylesheet = site_report.render_stylesheet()
    width, height = result["frame"]
    alone_html = (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{stylesheet}'
        f"@page {{ size: {width}pt {height}pt; margin: 0; @bottom-left{{content:none}} "
        f"@bottom-center{{content:none}} @bottom-right{{content:none}} }}"
        f"body {{ margin: 0; }}</style></head><body>{svg}</body></html>"
    )
    alone_pdf = os.path.join(out_dir, "report-map-alone.pdf")
    HTML(string=alone_html).write_pdf(alone_pdf)

    swatches = "".join(
        f'<div style="display:inline-block;width:1.2in;margin-right:8pt">'
        f'<div style="height:22pt;background:{value}"></div>'
        f'<div style="font-size:7.5pt;color:#8a8477">{name}<br>{value}</div></div>'
        for name, value in BROWN_CANDIDATES
    )
    page_html = (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{stylesheet}</style></head><body>'
        f'<section class="section" style="break-before:auto">'
        f'<p class="eyebrow"><span class="eyebrow__number">III</span> · Landform</p>'
        f'<h2 class="heading">Landform</h2>'
        f'<p class="summary">Proof render: the parcel boundary, <span class="data">{contours["interval_ft"]}</span> ft contours '
        f'from a synthetic DEM, north arrow, scale bar and an empty legend slot.</p>'
        f'<figure class="report-map">{svg}</figure>'
        f'<p class="source-footer__caveat">Terrain brown candidates, for the eye:</p>{swatches}'
        f"</section></body></html>"
    )
    page_pdf = os.path.join(out_dir, "report-map-page.pdf")
    HTML(string=page_html).write_pdf(page_pdf)

    import pymupdf

    for label, path in (("alone", alone_pdf), ("page", page_pdf)):
        doc = pymupdf.open(path)
        fonts = sorted({f[3] for f in doc[0].get_fonts(full=True)})
        images = sum(len(p.get_images()) for p in doc)
        drawings = len(doc[0].get_drawings())
        png = os.path.join(out_dir, f"report-map-{label}.png")
        doc[0].get_pixmap(dpi=150 if label == "alone" else 110).save(png)
        print(f"{label}: {len(doc)} page(s), raster images {images}, vector drawing items {drawings}, fonts {fonts} -> {png}")
        spans = [
            (s["text"], s["font"], round(s["size"], 1))
            for b in doc[0].get_text("dict")["blocks"] for l in b.get("lines", []) for s in l["spans"]
            if s["text"].strip()
        ]
        map_spans = [s for s in spans if s[0].strip() in ("N",) or "ft" in s[0]]
        print(f"   map text spans: {map_spans}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    dem_path = None
    if "--dem" in args:
        dem_path = args[args.index("--dem") + 1]
        args = [a for a in args if a not in ("--dem", dem_path)]
    sys.exit(main(args[0] if args else tempfile.mkdtemp(prefix="report-map-"), dem_path))
