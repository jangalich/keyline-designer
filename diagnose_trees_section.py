"""
diagnose_trees_section.py

EVERY TREES & FORESTRY FIGURE FOR THE REFERENCE PARCEL, tabled, and the
section's two pages rendered -- branch 11's verification, printed:

    python3 diagnose_trees_section.py [out_dir]

  * an offline session on the real DEM with the parcel's own lidar HAG
    (trees_reference_fixture.Harness), its terrain warm-up done -- the
    same context the report job reads; the report layer's forest type
    group and woodland rows from assets/reference/trees/;
  * every block of trees_derivations.derive(), in cells and converted
    beside it (acres, feet), every partition summed against the cover;
  * the canopy-input agreement with the exclusion gate, measured;
  * the same session again on the captured NLCD TCC dict: the fallback
    path, computed end to end with the heights absent;
  * the thirteen-page report rendered on both sessions, the Trees pages
    rasterised to PNG (PyMuPDF) and each page's slack measured.

Offline by construction: the network is refused (offline_harness).
"""

import os
import sys
import tempfile
from datetime import date

import offline_harness

offline_harness.install()

import numpy as np  # noqa: E402

import access_derivations as ad  # noqa: E402
import forest_type_data as ftd  # noqa: E402
import landform_section  # noqa: E402
import report_layout  # noqa: E402
import site_report  # noqa: E402
import soil_woodland as sw  # noqa: E402
import trees_derivations as td  # noqa: E402
import trees_reference_fixture as fixture  # noqa: E402
import trees_section as tsn  # noqa: E402
import water_derivations as wd  # noqa: E402
from landform_section import allocate_exactly  # noqa: E402
from raster_grid import binary_dilate  # noqa: E402

FT = 1 / 0.3048
GENERATED_ON = date(2026, 9, 23)


def _text(parts) -> str:
    if parts is None:
        return ""
    if isinstance(parts, str):
        return parts
    return "".join(p if isinstance(p, str) else str(p["value"]) for p in parts)


def _partition(label, counts: dict, parcel_acres: float):
    names = list(counts)
    acres = allocate_exactly([counts[n] for n in names], parcel_acres, 1)
    shares = allocate_exactly([counts[n] for n in names], 100.0, 1)
    print(f"  {label}")
    for name, acre, share in zip(names, acres, shares):
        print(f"    {str(name):40s} {counts[name]:6d} cells {acre:7.1f} ac {share:6.1f} %")
    print(f"    {'sum':40s} {sum(counts.values()):6d} cells {sum(acres):7.1f} ac {sum(shares):6.1f} %")


def _slack(document, first_page: int) -> list:
    rows = []
    for number, page in enumerate(document.pages, start=1):
        if number < first_page:
            continue
        page_box = page._page_box
        bottom = page_box.content_box_y() + page_box.height
        lowest = 0.0
        for box in report_layout._walk(page_box):
            element = getattr(box, "element", None)
            cls = (element.get("class") or "") if element is not None else ""
            if hasattr(box, "position_y") and "section" not in cls and any(
                c in cls for c in ("caption", "data-table", "source-footer", "summary", "report-map", "unavailable", "heading", "eyebrow")
            ):
                lowest = max(lowest, box.position_y + box.height)
        rows.append((number, round(bottom - lowest, 1)))
    return rows


def main(out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    data = fixture.report_data()
    for mode in ("hag", "tcc"):
        print("=" * 72)
        print(f"THE {mode.upper()} SESSION")
        print("=" * 72)
        with fixture.Harness(canopy=mode):
            session = fixture.Session()
            context = session.context()
            document = session.stored()
            terrain = landform_section.terrain_inputs_from_context(context, document)
            water = wd.water_inputs_from_context(context, document, data)
            access = ad.access_inputs_from_context(context, document, data)
            inputs = td.trees_inputs_from_context(context, document, data)
            section = tsn.build_trees_section(inputs)
            html = site_report.render_site_report_html(data, generated_on=GENERATED_ON, terrain=terrain, water=water, access=access, trees=inputs)
        derived = section["derived"]
        cells = derived.cells
        parcel_acres = cells["on_parcel_count"] * cells["cell_acres"]
        canopy = derived.canopy
        print(f"  canopy source {canopy['source']} (recorded {inputs.canopy_source_recorded}); item {canopy['source_item_id']}; "
              f"year {canopy['year']}; on-parcel cells {cells['on_parcel_count']}; cover {parcel_acres:.1f} ac")
        exclusion = context.exclusion_zones
        rebuilt = binary_dilate(canopy["grid_mask"], td.root_zone_radius_cells(inputs.dem)) & exclusion["slope_only_mask"]
        agrees = rebuilt.tobytes() == exclusion["layers"]["canopy"]["mask"].tobytes()
        print(f"  canopy cells dilated over the slope-only universe == exclusion canopy mask: {agrees} "
              f"({int(exclusion['layers']['canopy']['mask'].sum())} cells)")
        _partition("canopy extent", canopy["counts"], parcel_acres)
        if derived.heights:
            _partition("height classes", derived.heights["counts"], parcel_acres)
            print(f"    tallest {derived.heights['max_m'] * FT:.0f} ft, canopy median {derived.heights['canopy_median_m'] * FT:.0f} ft")
            closure = derived.closure
            by_class = ", ".join(f"{k}: {v['blocks']}" for k, v in closure["classes"].items())
            print(f"  closure at {closure['block_m']:.0f} m: {closure['mean_pct']:.1f}% over {closure['blocks']} blocks; by class {{{by_class}}}")
        else:
            print("  heights: ABSENT on this path (percent cover, not height)")
            print(f"  cover classes over canopy cells: {derived.cover['classes']}, mean {derived.cover['mean_pct']:.1f}%")
        forest = derived.forest_type
        _partition("forest type group", {ftd.FOREST_TYPE_GROUPS[c]: n for c, n in forest["counts"].items()}, parcel_acres)
        print("  productivity, the survey's species by ground rated:")
        for species in derived.productivity["species"]:
            print(f"    {species['common']:22s} {species['cells'] * cells['cell_acres']:6.1f} ac  SI {species['site_index_mean']:5.0f} "
                  f"[{species['site_index_min']:.0f}-{species['site_index_max']:.0f}]  {'; '.join(species['bases'])}")
        for mukey, unit in derived.productivity["units"].items():
            if unit["unrated_major"]:
                print(f"    unrated major component in {mukey} ({unit['muname'][:36]}): {unit['unrated_major']}")
        for name in sw.INTERPRETATIONS:
            _partition(sw.INTERPRETATION_LABELS[name], derived.limitations["interpretations"][name]["counts"], parcel_acres)
        print("  the words:")
        for key in ("summary", "map_caption", "extent_caption", "height_caption", "height_unavailable", "forest_type", "forest_type_caption",
                    "species_caption", "limitations_caption"):
            print(f"    {key}: {_text(section[key])}")
        for line in section["sources"]:
            print(f"    source: {_text(line)}")
        from weasyprint import HTML

        rendered = HTML(string=html, base_url=site_report.TEMPLATES_DIRECTORY).render()
        print(f"  pages {len(rendered.pages)}; overflowing boxes {report_layout.overflowing_boxes(rendered)}; "
              f"slack {_slack(rendered, 12)}")
        pdf_path = os.path.join(out_dir, f"site-data-report-{mode}.pdf")
        rendered.write_pdf(pdf_path)
        print(f"  PDF: {pdf_path}")
        try:
            import pymupdf
        except ImportError:
            print("  PyMuPDF not importable -- pages not rasterised.")
            continue
        pdf = pymupdf.open(pdf_path)
        for index in range(11, len(pdf)):
            png = os.path.join(out_dir, f"trees-{mode}-page-{index + 1}.png")
            pdf[index].get_pixmap(dpi=110).save(png)
            print(f"  rasterised page {index + 1} -> {png}")
    print(f"network requests refused: {len(offline_harness.refused())}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="trees-section-")))
