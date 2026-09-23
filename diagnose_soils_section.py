"""
diagnose_soils_section.py

EVERY SOILS & GEOLOGY FIGURE FOR THE REFERENCE PARCEL, TABLED, and the
section's three pages rendered -- branch 12's verification, printed:

    python3 diagnose_soils_section.py [out_dir]

  * an offline session on the real DEM with the parcel's real SSURGO
    rows, map unit polygons and K factors (soils_reference_fixture), its
    terrain warm-up done -- the same context the report job reads; the
    report layer's survey rows and geology answer from
    assets/reference/soils/;
  * the map unit table, every column, with the acreage and percent
    columns allocated exactly and summed against the cover;
  * the physical properties, in the survey's centimetres and in inches
    beside them, the surface horizon's depth stated per row;
  * the classification partitions -- capability and farmland -- each
    summed against the cover;
  * the erosion factors, the geology, and the survey version;
  * the call count: what the derivations fetched and what they recomputed;
  * the map's label fit, polygon by polygon: which symbols will be set
    and which are too small to hold one, and the tint each unit took
    beside the tints of the units it touches;
  * the whole report rendered, the Soils pages rasterised to PNG
    (PyMuPDF), each page's slack measured and the spill rule's break
    point rendered on both sides.

Offline by construction: the network is refused (offline_harness).
"""

import os
import sys
from datetime import date

import offline_harness

offline_harness.install()

import access_derivations as ad  # noqa: E402
import landform_section  # noqa: E402
import report_layout  # noqa: E402
import report_map  # noqa: E402
import site_report  # noqa: E402
import soils_section as ssn  # noqa: E402
import trees_derivations as td  # noqa: E402
import soil_survey as ss  # noqa: E402
import soils_derivations as sd  # noqa: E402
import soils_reference_fixture as fixture  # noqa: E402
import water_derivations as wd  # noqa: E402
from landform_section import allocate_exactly  # noqa: E402

CM_PER_IN = 2.54


def _partition(label, counts: dict, keys: list, parcel_acres: float):
    acres = allocate_exactly([counts[k] for k in keys], parcel_acres, 1)
    shares = allocate_exactly([counts[k] for k in keys], 100.0, 1)
    print(f"  {label}")
    for key, acre, share in zip(keys, acres, shares):
        print(f"    {str(key):44s} {counts[key]:6d} cells {acre:7.1f} ac {share:6.1f} %")
    print(f"    {'sum':44s} {sum(counts[k] for k in keys):6d} cells {sum(acres):7.1f} ac {sum(shares):6.1f} %")
    return acres


GENERATED_ON = date(2026, 9, 23)


def _slack(rendered, first_page: int) -> list:
    rows = []
    for number, page in enumerate(rendered.pages, start=1):
        if number < first_page:
            continue
        page_box = page._page_box
        bottom = page_box.content_box_y() + page_box.height
        lowest = 0.0
        for box in report_layout._walk(page_box):
            element = getattr(box, "element", None)
            cls = (element.get("class") or "") if element is not None else ""
            if hasattr(box, "position_y") and "section" not in cls and any(
                c in cls for c in ("caption", "data-table", "source-footer", "summary", "report-map",
                                   "unavailable", "heading", "eyebrow")
            ):
                lowest = max(lowest, box.position_y + box.height)
        rows.append((number, round(bottom - lowest, 1)))
    return rows


def main(out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    data = fixture.report_data()
    with fixture.Harness():
        session = fixture.Session()
        context = session.context()
        document = session.stored()
        inputs = sd.soils_inputs_from_context(context, document, data)
        water_inputs = wd.water_inputs_from_context(context, document, data)
        terrain = landform_section.terrain_inputs_from_context(context, document)
        access = ad.access_inputs_from_context(context, document, data)
        trees = td.trees_inputs_from_context(context, document, data)
        derived = sd.derive(inputs)
        html = site_report.render_site_report_html(data, generated_on=GENERATED_ON, terrain=terrain,
                                                   water=water_inputs, access=access, trees=trees, soils=inputs)

    cells = derived.cells
    parcel_acres = cells["on_parcel_count"] * cells["cell_acres"]
    survey = derived.survey_areas[0] if derived.survey_areas else {}
    print("=" * 108)
    print(f"SOILS & GEOLOGY (VII) -- {len(derived.map_units)} map units over {cells['on_parcel_count']} on-parcel cells; "
          f"cover {parcel_acres:.2f} ac; survey {survey.get('areasymbol')} version {survey.get('saverest')}")
    print("=" * 108)

    # --- the partition is Water's -------------------------------------
    water_cells = wd.grid_bookkeeping(water_inputs)
    water_hydric = wd.derive_hydric(water_inputs, water_cells)
    same = all(derived.map_units[m]["cells"] == u["cells"] for m, u in water_hydric["map_units"].items()) \
        and set(derived.map_units) == set(water_hydric["map_units"])
    print(f"  map-unit partition identical to Water's: {same}")
    print()

    # --- the map unit table -------------------------------------------
    order = derived.order
    counts = [derived.map_units[m]["cells"] for m in order]
    acres = allocate_exactly(counts, parcel_acres, 1)
    shares = allocate_exactly(counts, 100.0, 1)
    print("  MAP UNIT TABLE")
    print(f"    {'sym':>5} {'acres':>6} {'%':>6} {'drainage':<26} {'hyd':>4} {'Ksat':>6} {'cap':>5} {'farmland':<32} name")
    for mukey, acre, share in zip(order, acres, shares):
        unit = derived.map_units[mukey]
        print(f"    {unit['musym'] or '-':>5} {acre:6.1f} {share:6.1f} {str(unit['drainage_class']):<26} "
              f"{str(unit['hydrologic_group']):>4} {unit['ksat_um_s']:6.2f} "
              f"{sd.capability_label(unit['capability_class'], unit['capability_subclass']):>5} "
              f"{str(unit['farmland']):<32} {unit['muname']}")
    print(f"    {'total':>5} {sum(acres):6.1f} {sum(shares):6.1f}     (cover {round(parcel_acres, 1)})")
    print()

    # --- physical properties ------------------------------------------
    print("  PHYSICAL PROPERTIES -- the dominant major component's surface horizon, depth stated per row")
    print(f"    {'sym':>5} {'component':<12} {'%':>4} {'horizon':>8} {'depth cm':>10} {'depth in':>10} "
          f"{'texture':<20} {'sand':>5} {'silt':>5} {'clay':>5} {'awc':>5} {'om %':>5} {'pH':>4} "
          f"{'bedrock':>12} {'profile aws':>12}")
    for mukey in order:
        unit, block = derived.map_units[mukey], derived.properties.get(mukey)
        if block is None:
            print(f"    {unit['musym'] or '-':>5} (no survey)")
            continue
        depth_cm = f"{block['top_cm']:.0f}-{block['bottom_cm']:.0f}"
        depth_in = f"{block['top_cm'] / CM_PER_IN:.0f}-{block['bottom_cm'] / CM_PER_IN:.0f}"
        bedrock = f"{'>' if block['bedrock_is_bound'] else ''}{block['bedrock_cm'] / CM_PER_IN:.0f} in"
        print(f"    {unit['musym']:>5} {block['compname']:<12} {block['comppct']:4.0f} {block['hzname']:>8} "
              f"{depth_cm:>10} {depth_in:>10} {block['texture']:<20} "
              f"{block['sand_pct']:5.1f} {block['silt_pct']:5.1f} {block['clay_pct']:5.1f} "
              f"{block['awc']:5.2f} {block['om_pct']:5.1f} {block['ph']:4.1f} {bedrock:>12} "
              f"{block['aws0150_cm'] / CM_PER_IN:9.1f} in")
    headline = sd.texture_headline(derived.properties, derived.map_units)
    print(f"    texture headline: {headline['texture']} on {headline['units']} of {len(order)} units, "
          f"{headline['cells']} of {cells['on_parcel_count']} cells")
    for note in sd.restriction_notes(derived.properties, derived.map_units):
        bedrock = f"{'deeper than ' if note['bedrock_is_bound'] else ''}{note['bedrock_cm'] / CM_PER_IN:.0f} in"
        print(f"    RESTRICTION THAT IS NOT BEDROCK: {note['musym']} ({note['compname']}) {note['kind'].lower()} at "
              f"{note['depth_cm'] / CM_PER_IN:.0f} in, where the bedrock column reads {bedrock}")
    print()

    # --- classification ------------------------------------------------
    print("  CLASSIFICATION, by acres")
    _partition("land capability (non-irrigated)", derived.capability["counts"], derived.capability["labels"], parcel_acres)
    _partition("farmland classification", derived.farmland["counts"], derived.farmland["values"], parcel_acres)
    print("  EROSION FACTORS")
    print(f"    {'sym':>5} {'K':>6} {'T':>4}")
    for mukey in order:
        erosion = derived.erosion[mukey]
        print(f"    {derived.map_units[mukey]['musym']:>5} {erosion['kwfact']:6.2f} {erosion['tfact']:4.0f}")
    print()

    # --- geology --------------------------------------------------------
    print("  GEOLOGY")
    geology = derived.geology
    print(f"    straddles more than one unit: {geology['straddles']}")
    for unit in geology["units"]:
        where = "under the parcel's centre" if unit["at_centroid"] else "also mapped across the parcel's extent"
        print(f"    {unit['label']:>5} {unit['name']} -- {unit['age']} ({unit['age_max_ma']}-{unit['age_min_ma']} Ma), "
              f"{unit['province']}; {', '.join(unit['lithologies'])}; {where}")
        print(f"          {unit['description']}")
    print()

    # --- the map's labels ------------------------------------------------
    print("  MAP LABELS -- the symbol set inside each polygon at its pole of inaccessibility")
    parcel = inputs.boundary_polygon_utm
    rendered = report_map.render_map(parcel, [], site_report.TOKENS)
    mpu = rendered["meters_per_unit"]
    print(f"    {'sym':>5} {'acres':>6} {'pole r pt':>10} {'needs pt':>9}  fits")
    dropped_acres = 0.0
    placed = 0
    for mukey, acre in zip(order, acres):
        unit = derived.map_units[mukey]
        symbol = unit["musym"] or "-"
        pole = report_map.polygon_pole(unit["geometry_utm"])
        radius = pole[2] / mpu if pole else 0.0
        needed = report_map.polygon_label_radius_pt(symbol)
        fits = radius >= needed
        placed += int(fits)
        dropped_acres += 0.0 if fits else acre
        print(f"    {symbol:>5} {acre:6.1f} {radius:10.2f} {needed:9.2f}  {'yes' if fits else 'NO -- too small'}")
    print(f"    {placed} of {len(order)} symbols will be set; {len(order) - placed} dropped, "
          f"{dropped_acres:.1f} ac in all -- the map unit table carries every symbol")
    print(f"    map scale {mpu:.4f} m per pt, frame {rendered['frame'][0]:.0f} x {rendered['frame'][1]:.0f} pt, "
          f"scale bar {rendered['scale_bar']['feet']} ft")
    tints = ssn.assign_tints(derived)
    adjacency = ssn.neighbours(derived)
    print(f"    {'sym':>5} {'tint':>5}  touches (tint)")
    for mukey in order:
        unit = derived.map_units[mukey]
        touching = ", ".join(f"{derived.map_units[n]['musym']} ({tints[n]:.2f})" for n in sorted(
            adjacency[mukey], key=lambda k: -derived.map_units[k]["cells"]))
        print(f"    {unit['musym']:>5} {tints[mukey]:5.2f}  {touching or '(none)'}")
    clashes = [(a, b) for a, ns in adjacency.items() for b in ns if tints[a] == tints[b]]
    print(f"    {len(set(tints.values()))} of {len(ssn.SOIL_TINTS)} tints used; "
          f"{len(clashes)} touching pair(s) share one")
    print()

    # --- the pages --------------------------------------------------------
    print("  THE PAGES")
    print(f"    spill: {ssn.spills(derived)} ({len(order)} units, the map page holds "
          f"{ssn.MAP_UNIT_ROWS_MAX})")
    pdf_path = os.path.join(out_dir, "site-report.pdf")
    site_report.generate_site_report_pdf(data, pdf_path, generated_on=GENERATED_ON, terrain=terrain,
                                         water=water_inputs, access=access, trees=trees, soils=inputs)
    from weasyprint import HTML

    document_ = HTML(string=html, base_url=site_report.TEMPLATES_DIRECTORY).render()
    overflow = report_layout.overflowing_boxes(document_)
    first = len(document_.pages) - 2
    print(f"    {len(document_.pages)} pages; {len(overflow)} box(es) past the measure")
    for number, slack in _slack(document_, first):
        print(f"      page {number}: {slack:6.1f} pt of slack")
    try:
        import pymupdf
    except ImportError:
        print("    (PyMuPDF not installed: no PNGs)")
    else:
        opened = pymupdf.open(pdf_path)
        for index, name in zip(range(first - 1, len(document_.pages)),
                               ("soils-1-map", "soils-2-properties", "soils-3-classification")):
            path = os.path.join(out_dir, f"{name}.png")
            opened[index].get_pixmap(dpi=140).save(path)
            print(f"      wrote {path}")
    print()

    print("  SOURCES READ")
    print(f"    Layer 1 (already in the session, not fetched again): {len(inputs.soil_components)} component rows, "
          f"{len(inputs.soil_geometries)} map unit polygons, {len(inputs.farmland_classification)} farmland rows, "
          f"{len(inputs.erosion_factor)} K rows, {len(inputs.saturated_hydraulic_conductivity)} Ksat rows")
    print(f"    report layer: soil_survey {len((inputs.soil_survey or {}).get('components') or {})} component rows in "
          f"1 query; bedrock_geology {len((inputs.bedrock_geology or {}).get('units') or [])} unit(s)")
    print(f"    {ss.SSURGO_CITATION}")
    print(offline_harness.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "_proof"))
