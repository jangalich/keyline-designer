"""
diagnose_water_section.py

EVERY WATER & HYDROLOGY FIGURE FOR THE REFERENCE PARCEL, tabled -- branch
9 phase 1's verification, printed:

    python3 diagnose_water_section.py          # offline: the captured fixtures
    python3 diagnose_water_section.py --live   # the real services and a real session

  * an offline session on the real DEM with the real SSURGO and NHD rows
    (water_reference_fixture.Harness), its terrain warm-up done -- the
    same context the report job reads; the report layer's six Water
    blocks from assets/reference/water/;
  * the landform derivations' flow pass, run ONCE and handed to the water
    derivations -- the call counts printed prove it;
  * every block of water_derivations.derive(), in the units it holds
    (metres, cm, cells) and converted beside it (feet, inches, acres);
  * every acreage partition summed against the cover's parcel acreage.
"""

import sys
from unittest.mock import patch

LIVE = "--live" in sys.argv
_positional = [a for a in sys.argv[1:] if not a.startswith("--")]
OUT_DIR = _positional[0] if _positional else None
if not LIVE:
    import offline_harness

    offline_harness.install()

import landform_derivations  # noqa: E402
import landform_section  # noqa: E402
import valley_delineation  # noqa: E402
import water_derivations as wd  # noqa: E402
import water_reference_fixture as fixture  # noqa: E402
import water_survey_areas  # noqa: E402
from landform_section import allocate_exactly  # noqa: E402
from reference_fixture import REAL_BOUNDARY  # noqa: E402

FT = 1 / 0.3048
ACRES_PER_M2 = 1 / 4046.8564224
IN_PER_CM = 1 / 2.54
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _acres_row(label, counts: dict, parcel_acres: float):
    names = list(counts)
    acres = allocate_exactly([counts[n] for n in names], parcel_acres, 1)
    shares = allocate_exactly([counts[n] for n in names], 100.0, 1)
    print(f"  {label}")
    for name, count, acre, share in zip(names, (counts[n] for n in names), acres, shares):
        print(f"    {name:42s} {count:6d} cells {acre:7.1f} ac {share:6.1f} %")
    print(f"    {'sum':42s} {sum(counts.values()):6d} cells {sum(acres):7.1f} ac {sum(shares):6.1f} %")
    return acres


def main() -> int:
    if LIVE:
        import report_data as report_data_module
        from diagnose_landform_section import _LiveSession, _NoHarness

        data = report_data_module.default_report_fetch_cache().get_or_fetch(REAL_BOUNDARY)
        harness, make_session = _NoHarness(), _LiveSession
    else:
        data = fixture.report_data()
        harness, make_session = fixture.Harness(), fixture.Session
    with harness:
        session = make_session()
        context = session.context()
        document = session.stored()
        terrain = landform_section.terrain_inputs_from_context(context, document)
        with patch.object(valley_delineation, "fill_and_resolve", wraps=valley_delineation.fill_and_resolve) as fills, \
             patch.object(valley_delineation, "compute_flow_accumulation", wraps=valley_delineation.compute_flow_accumulation) as flows, \
             patch.object(valley_delineation, "delineate_valleys", wraps=valley_delineation.delineate_valleys) as valleys, \
             patch.object(water_survey_areas, "compute_water_survey_areas", wraps=water_survey_areas.compute_water_survey_areas) as water_step:
            flow = landform_derivations.derive(terrain)
            inputs = wd.water_inputs_from_context(context, document, data)
            derived = wd.derive(inputs, flow)
        print(f"call counts: fill_and_resolve {fills.call_count}, compute_flow_accumulation {flows.call_count}, "
              f"delineate_valleys {valleys.call_count}, compute_water_survey_areas {water_step.call_count}")
    parcel_acres = inputs.parcel_acres
    cells = derived.cells
    print(f"parcel {parcel_acres:.4f} ac -> cover {round(parcel_acres, 1):.1f}; on-parcel cells {cells['on_parcel_count']} "
          f"x {cells['cell_acres']:.5f} ac = {cells['on_parcel_count'] * cells['cell_acres']:.3f} ac")
    print("unavailable layers:", inputs.unavailable or "none")

    s = derived.surface_water
    print("\nSURFACE WATER (NHD, bbox + 150 m)")
    for st in s["streams"]:
        print(f"  {st['name'] or 'unnamed':14s} fcode {st['fcode']} {st['permanence']:12s} order {st['stream_order']} "
              f"drains {st['total_drainage_acres'] and round(st['total_drainage_acres'])} ac; on parcel {st['length_on_parcel_m']:.1f} m "
              f"({st['length_on_parcel_m'] * FT:.0f} ft); in window {st['length_in_window_m']:.1f} m; distance {st['distance_m']:.1f} m")
    print(f"  waterbodies: {len(s['waterbodies'])}; springs fetched {s['springs_fetched']}: {s['springs']}")
    print(f"  nearest stream {s['nearest_stream_distance_m']} m; length on parcel by permanence {s['length_on_parcel_by_permanence_m']}; "
          f"streams in window {s['streams_in_window_by_permanence']}")

    print("\nHYDRIC SOIL (SSURGO)")
    for mukey, unit in derived.hydric["map_units"].items():
        print(f"  {mukey} {unit['muname'][:52]:52s} hydric {unit['hydric_pct']:5.1f}% {unit['class']:14s} {unit['cells']:5d} cells "
              f"{unit['cells'] * cells['cell_acres']:.2f} ac")
    _acres_row("hydric partition", derived.hydric["counts"], parcel_acres)

    w = derived.wetlands
    print(f"\nWETLANDS (NWI) fetched {w['fetched']}; project {w['project']}")
    for f in w["features"]:
        print(f"  {f['code']:6s} {f['wetland_type']:36s} {f['acres_mapped']:9.2f} ac mapped; {f['geometry_detail']}; "
              f"distance {f['distance_m']:.1f} m; in window {f['area_in_window_m2'] * ACRES_PER_M2:.2f} ac; on parcel {f['cells_on_parcel']} cells")
    print(f"  on parcel by type: {w['counts_by_type']} ({w['on_parcel_cells']} cells); window acres by type "
          f"{ {k: round(v * ACRES_PER_M2, 2) for k, v in w['window_area_by_type_m2'].items()} }")

    t = derived.wetness
    print(f"\nTERRAIN WETNESS: threshold TWI {t['threshold']:.3f} (window p{t['breakpoints']['full_credit_percentile']:g}; "
          f"floor {t['breakpoints']['floor']:.3f}); wet cells {t['wet_cells']} = {t['wet_cells'] * cells['cell_acres']:.2f} ac; "
          f"on-parcel percentiles {t['percentiles_on_parcel']}")
    print(f"  depressions: {t['depression_cells']} cells = {t['depression_cells'] * cells['cell_acres']:.2f} ac, max depth {t['depression_max_m']:.2f} m")
    _acres_row("terrain wetness partition", {"at or above threshold": t["wet_cells"], "below": cells["on_parcel_count"] - t["wet_cells"]}, parcel_acres)

    c = derived.comparison
    print("\nWET GROUND THREE WAYS (cells / acres)")
    for key in ("hydric", "wetland", "terrain", "hydric_and_wetland", "hydric_and_terrain", "wetland_and_terrain", "all_three",
                "any", "none", "hydric_only", "wetland_only", "terrain_only", "terrain_on_partially_hydric"):
        print(f"  {key:28s} {c[key]:5d} {c[key] * cells['cell_acres']:6.2f} ac")
    print(f"  wettest cell: {c['wettest_cell']}")

    wt = derived.water_table
    print(f"\nSEASONAL WATER TABLE fetched {wt['fetched']}; survey {wt['survey_areas']}; weighted cells {wt['weighted_cells']}")
    for mukey, unit in wt["map_units"].items():
        if not unit["has_data"]:
            print(f"  {unit['muname'][:40]:40s} NO DATA")
            continue
        row = []
        for m in range(1, 13):
            v = unit["months"][m]
            row.append(f"{v['depth_cm'] * IN_PER_CM:4.0f}" if v["state"] == wd.WT_WET else (f">{v['deeper_than_cm'] * IN_PER_CM:3.0f}" if v["deeper_than_cm"] else " n/d"))
        print(f"  {unit['muname'][:40]:40s} {unit['cells']:5d} cells majors {unit['major_pct']:.0f}% | " + " ".join(row) + "  (in)")
    p = wt["parcel"]
    print("  parcel depth in (wet share):  " + " ".join(f"{(p[m]['depth_cm'] or 0) * IN_PER_CM:4.0f}({p[m]['wet_share'] * 100:2.0f}%)" for m in range(1, 13)))
    print("  parcel deeper-than in:        " + " ".join(f"{(p[m]['deeper_than_cm'] or 0) * IN_PER_CM:8.0f}" for m in range(1, 13)))
    print("  flooding share by class:      " + " | ".join(f"{MONTHS[m - 1]} " + ", ".join(f"{k} {v * 100:.0f}%" for k, v in p[m]["flood"].items()) for m in range(1, 13)))
    print("  ponding share by class:       " + " | ".join(f"{MONTHS[m - 1]} " + ", ".join(f"{k} {v * 100:.0f}%" for k, v in p[m]["pond"].items()) for m in range(1, 13)))

    d = derived.drainage
    print(f"\nDRAINAGE: {len(d['flow_paths'])} flow paths on the parcel from {d['valleys']} valleys, {d['length_on_parcel_m']:.0f} m "
          f"({d['length_on_parcel_m'] * FT:.0f} ft)")

    ca = derived.catchment
    print("\nCATCHMENT")
    for r in ca["reaches"]:
        print(f"  reach {r['name'] or 'unnamed':12s} order {r['stream_order']} total drainage {r['total_drainage_acres']:.0f} ac; in window {r['in_window']}; on parcel {r['on_parcel']}")
    print(f"  stream catchment (NHDPlus HR, largest reach in window): {ca['stream_catchment_acres'] and round(ca['stream_catchment_acres'])} ac")
    print(f"  window-derived: {ca['outlets']} outlets; watershed {ca['watershed_cells']} cells = {ca['watershed_cells'] * cells['cell_acres']:.2f} ac "
          f"(on parcel {ca['on_parcel_cells'] * cells['cell_acres']:.2f}, off {ca['off_parcel_cells'] * cells['cell_acres']:.2f}); "
          f"rim cells {ca['rim_cells']}; truncated {ca['truncated']}; largest outlet {ca['largest_outlet']}")

    lc = derived.land_cover
    print(f"\nLAND COVER (NLCD {lc.get('year')}, {lc.get('collection')}) fetched {lc['fetched']}")
    if lc["fetched"]:
        for scope in ("catchment", "parcel"):
            groups = lc[scope]["groups"]
            total = sum(groups.values()) + lc[scope]["nodata"]
            print(f"  {scope}: " + ", ".join(f"{k} {v} ({v / total * 100:.1f}%)" for k, v in groups.items()) + f"; nodata {lc[scope]['nodata']}")
        _acres_row("parcel land cover partition", dict(lc["parcel"]["groups"], **({"no data": lc["parcel"]["nodata"]} if lc["parcel"]["nodata"] else {})), parcel_acres)

    fl = derived.flood
    print(f"\nFLOOD (FEMA NFHL) fetched {fl['fetched']}; available {fl['available']} {fl['study_ids']}; panel {fl['panel']}")
    for z in fl["zones"]:
        print(f"  {z['label']:40s} sfha {z['sfha']} study {z['study_type']} bfe {z['static_bfe']}; on parcel {z['cells_on_parcel']} cells; "
              f"in window {z['area_in_window_m2'] * ACRES_PER_M2:.2f} ac")
    _acres_row("flood partition", fl["counts"], parcel_acres)

    # ------------------------------------------------------------------
    # PHASE 2: the pages. The whole report -- cover, Climate, Landform,
    # Water -- as PDF and every page as PNG, then a second render with
    # FEMA and NWI unavailable for the degraded statements.
    # ------------------------------------------------------------------
    import os
    import tempfile
    from datetime import date

    import pymupdf

    import site_report
    import water_section

    out_dir = OUT_DIR or tempfile.mkdtemp(prefix="water-")
    os.makedirs(out_dir, exist_ok=True)
    section = water_section.build_water_section(inputs, site_report.TOKENS, flow=flow)
    print("\nsummary:", "".join(p if isinstance(p, str) else p["value"] for p in section["summary"]))
    print("key figures:", [(f["value"], f["label"]) for f in section["key_figures"]])
    for name in ("map", "wetness_map"):
        m = section[name]
        print(f"{name}: legend", [("".join(p if isinstance(p, str) else p["value"] for p in e["parts"])) for e in m["legend"]],
              f"m/unit {m['meters_per_unit']:.4f} bbox {tuple(round(v, 1) for v in m['drawn_bbox'])} scale bar {m['scale_bar']}")
    if section["water_table"]:
        for row in section["water_table"]["rows"]:
            label = row["label"] if isinstance(row["label"], str) else "".join(p if isinstance(p, str) else p["value"] for p in row["label"])
            print(f"  {label:44s}", " ".join(f"{(c['value'] if isinstance(c, dict) else c):>7s}" for c in row["cells"]))
    for name in ("surface_water_table", "comparison_table", "land_cover_table", "flood_table"):
        table = section[name]
        if not table:
            print(f"  {name}: none"); continue
        print(f"  {name}: {table['corner']} | {table['columns']}")
        for row in table["rows"]:
            label = row["label"] if isinstance(row["label"], str) else "".join(p if isinstance(p, str) else p["value"] for p in row["label"])
            print(f"    {label:48s}", " ".join(f"{(c['value'] if isinstance(c, dict) else c):>9s}" for c in row["cells"]))
    for name in ("map_caption", "surface_water_caption", "wetness_caption", "water_table_caption", "comparison_caption",
                 "land_cover_caption", "flood_caption"):
        print(f"  {name}: " + "".join(p if isinstance(p, str) else p["value"] for p in section[name]))
    print("  sources:", ["".join(l) for l in section["sources"]])

    generated_on = date.today()
    html = site_report.render_site_report_html(data, generated_on=generated_on, terrain=terrain, water=inputs)
    with open(os.path.join(out_dir, "site-report.html"), "w", encoding="utf-8") as handle:
        handle.write(html)
    pdf_path = os.path.join(out_dir, "site-report.pdf")
    site_report.generate_site_report_pdf(data, pdf_path, generated_on=generated_on, terrain=terrain, water=inputs)
    doc = pymupdf.open(pdf_path)
    for index, page in enumerate(doc, start=1):
        page.get_pixmap(dpi=110).save(os.path.join(out_dir, f"page-{index}.png"))
    print(f"{len(doc)} pages -> {out_dir}/page-N.png")
    # The degraded render: FEMA and NWI unavailable.
    if not LIVE:
        degraded = fixture.report_data(nwi=None, fema_nfhl=None, unavailable={
            "nwi": {"label": "mapped wetlands", "reason": "source_unavailable", "error": "down"},
            "fema_nfhl": {"label": "flood hazard zones", "reason": "source_unavailable", "error": "down"},
        })
        degraded_inputs = wd.water_inputs_from_context(context, document, degraded)
        degraded_pdf = os.path.join(out_dir, "site-report-degraded.pdf")
        site_report.generate_site_report_pdf(degraded, degraded_pdf, generated_on=generated_on, terrain=terrain, water=degraded_inputs)
        ddoc = pymupdf.open(degraded_pdf)
        for index, page in enumerate(ddoc, start=1):
            if index >= 7:
                page.get_pixmap(dpi=110).save(os.path.join(out_dir, f"degraded-page-{index}.png"))
        print(f"degraded: {len(ddoc)} pages -> {out_dir}/degraded-page-N.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
