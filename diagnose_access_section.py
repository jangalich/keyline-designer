"""
diagnose_access_section.py

EVERY ACCESS FIGURE FOR THE REFERENCE PARCEL, tabled -- branch 10 phase
1's verification, printed:

    python3 diagnose_access_section.py          # offline: the captured fixtures
    python3 diagnose_access_section.py --live   # the real services and a real session

  * an offline session on the real DEM with the real SSURGO, NHD and ROAD
    rows (access_reference_fixture.Harness), its terrain warm-up done --
    the same context the report job reads; the report layer's soil road
    ratings from assets/reference/access/;
  * every block of access_derivations.derive(), in the units it holds
    (metres, cells) and converted beside it (feet, acres);
  * the call counts that prove nothing was recomputed or fetched;
  * the tracks-match-exclusion-mask agreement, measured;
  * every acreage partition summed against the cover's parcel acreage.
"""

import sys
from unittest.mock import patch

LIVE = "--live" in sys.argv
if not LIVE:
    import offline_harness

    offline_harness.install()

import access_derivations as ad  # noqa: E402
import access_reference_fixture as fixture  # noqa: E402
import farm_roads_data  # noqa: E402
import production_area  # noqa: E402
import soil_data  # noqa: E402
import valley_delineation  # noqa: E402
import numpy as np  # noqa: E402
from landform_section import allocate_exactly  # noqa: E402
from reference_fixture import REAL_BOUNDARY  # noqa: E402

FT = 1 / 0.3048
ACRES_PER_M2 = 1 / 4046.8564224


def _acres_rows(label, counts: dict, parcel_acres: float):
    names = list(counts)
    acres = allocate_exactly([counts[n] for n in names], parcel_acres, 1)
    shares = allocate_exactly([counts[n] for n in names], 100.0, 1)
    print(f"  {label}")
    for name, acre, share in zip(names, acres, shares):
        print(f"    {name:34s} {counts[name]:6d} cells {acre:7.1f} ac {share:6.1f} %")
    print(f"    {'sum':34s} {sum(counts.values()):6d} cells {sum(acres):7.1f} ac {sum(shares):6.1f} %")
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
        with patch.object(production_area, "compute_slope_percent", wraps=production_area.compute_slope_percent) as slopes, \
             patch.object(valley_delineation, "delineate_valleys", wraps=valley_delineation.delineate_valleys) as valleys, \
             patch.object(farm_roads_data, "get_farm_roads_for_boundary", wraps=farm_roads_data.get_farm_roads_for_boundary) as road_fetch, \
             patch.object(farm_roads_data, "get_road_exclusion_union_utm", wraps=farm_roads_data.get_road_exclusion_union_utm) as road_union, \
             patch.object(soil_data, "_run_sda_query", wraps=soil_data._run_sda_query) as sda:
            inputs = ad.access_inputs_from_context(context, document, data)
            derived = ad.derive(inputs)
        print(f"call counts: compute_slope_percent {slopes.call_count}, delineate_valleys {valleys.call_count}, "
              f"get_farm_roads_for_boundary {road_fetch.call_count}, get_road_exclusion_union_utm {road_union.call_count}, "
              f"_run_sda_query {sda.call_count}")
        roads_layer = context.exclusion_zones["layers"]["roads"]
        existing = context.existing_roads
    parcel_acres = inputs.parcel_acres
    cells = derived.cells
    print(f"parcel {parcel_acres:.4f} ac -> cover {round(parcel_acres, 1):.1f}; on-parcel cells {cells['on_parcel_count']} "
          f"x {cells['cell_acres']:.5f} ac; perimeter {derived.boundary['perimeter_m']:.1f} m ({derived.boundary['perimeter_m'] * FT:.0f} ft)")
    print("unavailable layers:", inputs.unavailable or "none")

    print("\nMAPPED ROADS (National Map layers 30/31/32, bbox + 150 m)")
    for r in derived.roads:
        print(f"  {r['name']:14s} {str(r['class']):22s} route {str(r['route']):5s} len {r['length_m']:7.1f} m  distance {r['distance_m']:6.1f} m  "
              f"on parcel {r['length_on_parcel_m']:5.1f} m  frontage {r['frontage_m']:6.1f} m  in 150 m {r['length_in_window_m']:6.1f} m  "
              f"{r['properties'].get('mtfcc_code')} {r['properties'].get('source_datadesc')}")

    f = derived.frontage
    print(f"\nFRONTAGE (ring within {f['tolerance_m']:.0f} m of a centreline), perimeter {f['perimeter_m']:.1f} m")
    for road in f["roads"]:
        print(f"  {road['name']:14s} {str(road['class']):22s} {road['segments']} segment(s) {road['length_m']:7.1f} m ({road['length_m'] * FT:6.0f} ft)")
    print(f"  total {f['total_m']:.1f} m ({f['total_m'] * FT:.0f} ft) = {f['total_m'] / f['perimeter_m'] * 100:.1f} % of the perimeter; "
          f"mapped roads {f['mapped_roads']}; nearest-road fallback: {f['nearest']}")

    t = derived.tracks
    print(f"\nTRACKS ON THE PARCEL: {t['count']}, {t['total_m']:.1f} m ({t['total_m'] * FT:.0f} ft)")
    for track in t["tracks"]:
        p = track["profile"]
        print(f"  {track['name']:14s} {track['length_m']:5.1f} m; elevation {min(p['elevations_m']):.1f}-{max(p['elevations_m']):.1f} m; "
              f"grade mean {p['mean_grade_pct']:.1f}% max {p['max_grade_pct']:.1f}% end-to-end {p['end_to_end_grade_pct']:.1f}%; "
              f"entry slope {[None if s is None else round(s, 1) for s in track['entry_slopes_pct']]}%")

    b = derived.boundary
    print(f"\nBOUNDARY DRIVABILITY: slope {b['inset_m']:.0f} m inside every {b['step_m']:.0f} m, undrivable at >= {b['threshold_pct']:.0f}%")
    for state, m in b["lengths_m"].items():
        print(f"  {state:11s} {m:7.1f} m ({m * FT:6.0f} ft) {m / b['perimeter_m'] * 100:5.1f} % of the perimeter")
    print(f"  sum {sum(b['lengths_m'].values()):.1f} m = perimeter {b['perimeter_m']:.1f} m")
    for state, m in b["frontage_lengths_m"].items():
        print(f"  frontage {state:11s} {m:7.1f} m ({m * FT:6.0f} ft)")
    print(f"  runs: {len(b['runs'])} -> {[(r['state'][0], round(r['length_m'])) for r in b['runs']]}")
    for e in b["edges"]:
        print(f"  edge {e['index']} {ad.compass_sector(e['midpoint_bearing_deg']):2s} {e['length_m']:6.1f} m  mean {e['mean_slope_pct']:5.1f}% "
              f"max {e['max_slope_pct']:5.1f}%  undrivable {e['undrivable_share'] * 100:5.1f}%  frontage {e['frontage_m']:6.1f} m")

    c = derived.crossings
    print(f"\nSTREAM CROSSINGS: {len(c['streams_on_parcel'])} stream(s) on the parcel, {c['crossings_needed']} piece(s) beyond a crossing, "
          f"{c['beyond_crossing_m2'] * ACRES_PER_M2:.2f} ac")

    s = derived.soil
    print(f"\nSOIL ROAD RATINGS (SSURGO cointerp) fetched {s['fetched']}; survey {s['survey_areas']}")
    for mukey, unit in s["map_units"].items():
        paved = unit["paved"]
        print(f"  {mukey} {str(unit['muname'])[:40]:40s} {unit['cells']:4d} cells  paved {str(paved and paved['class']):17s} "
              f"unpaved {str(unit['unpaved'] and unit['unpaved']['class']):17s} roadfill {str(unit['roadfill'] and unit['roadfill']['class']):5s} "
              f"weights {paved and {k: round(v) for k, v in paved['weights'].items()}}")
        print(f"      features {{{', '.join(f'{k}: {v:.2f}' for k, v in unit['features'].items())}}}")
    acres = _acres_rows("partition by paved class", s["counts"], parcel_acres)
    for name in ad.srr.LIMITATION_CLASSES:
        if s["features"][name]:
            print(f"  features, {name}: " + ", ".join(f"{k} {v * cells['cell_acres']:.1f} ac" for k, v in s["features"][name].items()))
    _acres_rows("partition by roadfill class", s["roadfill_counts"], parcel_acres)
    print(f"  unpaved class differs from paved on: {s['unpaved_differs'] or 'no map unit'}")

    print("\nTRACKS MATCH THE EXCLUSION MASK")
    print(f"  section's rows buffered {farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS} m equals context.existing_roads: "
          f"{derived.exclusion_union.equals(existing) if derived.exclusion_union is not None else existing is None}")
    print(f"  exclusion roads layer: {int(roads_layer['mask'].sum())} cells, {roads_layer['acres']} ac, data_available {roads_layer['data_available']}; "
          f"union on parcel {derived.exclusion_union.intersection(inputs.boundary_polygon_utm).area:.1f} m2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
