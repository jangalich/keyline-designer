"""
diagnose_site_overview.py

EVERY SITE OVERVIEW FIGURE FOR THE REFERENCE PARCEL, tabled -- branch 17
phase 1's verification, printed:

    python3 diagnose_site_overview.py

  * every figure section I states, with its source and how it was read;
  * the context DEM at the wider extent: its grid, the resolution the
    300-cell cap forces, the relief and the contour interval the rule
    picks, and the index levels;
  * the landscape position at the printed radius and at two others;
  * acreage and perimeter against the cover and Access;
  * the call count and payload of every report-layer fetch the overview
    added, from the capture record.

Offline by construction: the network is refused (offline_harness).
"""

import sys

import offline_harness

offline_harness.install()

import access_derivations as ad  # noqa: E402
import census_geography  # noqa: E402
import landform_section  # noqa: E402
import livestock_predators as lp  # noqa: E402
import overview_derivations as od  # noqa: E402
import overview_reference_fixture as fixture  # noqa: E402
import site_report  # noqa: E402

FT = 1 / 0.3048
MI = 1609.344


def main() -> int:
    data = fixture.report_data()
    with fixture.Harness():
        session = fixture.Session()
        context = session.context()
        document = session.stored()
        inputs = od.overview_inputs_from_context(context, document, data)
        terrain = landform_section.terrain_inputs_from_context(context, document)
        access = ad.access_inputs_from_context(context, document, data)
    d = od.derive(inputs)
    frontage = ad.derive_frontage(access, ad.derive_roads(access))

    rows = []
    rows.append(("acreage", f"{d.acres:.2f} ac -> {round(d.acres, 1):.1f} ac", "boundary polygon, UTM 17N"))
    rows.append(("perimeter", f"{d.perimeter_m:,.1f} m = {d.perimeter_m * FT:,.0f} ft", "boundary ring, UTM 17N"))
    rows.append(("county, State", census_geography.county_state_label(d.county_state) or "-",
                 f"Census geocoder, FIPS {d.county_state['county_fips']}" if d.county_state else "degraded"))
    rows.append(("centroid", f"{d.centroid[0]:.5f} N, {abs(d.centroid[1]):.5f} W", "polygon centroid, WGS84"))
    rows.append(("elevation range", f"{d.elevation['min_ft']:,.0f} to {d.elevation['max_ft']:,.0f} ft "
                 f"({d.elevation['relief_ft']:.0f} ft relief)", "session 5 m DEM, Landform's figures"))
    L = d.landscape
    if L:
        rows.append(("landscape position", f"{L['class']} (z {L['z']:+.2f} at {L['radius_m']:.0f} m; median slope "
                     f"{L['median_slope_deg']:.1f} deg)", "context DEM, TPI standardised over the window"))
        rows.append(("  relief position", f"lowest ground at the {L['percentile_low']:.0f}th percentile, highest at the "
                     f"{L['percentile_high']:.0f}th, of {L['window_relief_ft']:.0f} ft", "context DEM"))
        rows.append(("  drains in from above", f"{L['inflow']['acres']:.1f} ac"
                     + (" (at least: reaches the window edge)" if L['inflow']['truncated'] else ""), "D8 over the context DEM"))
    if d.physiography:
        rows.append(("physiographic province", f"{d.physiography['province']} province, {d.physiography['division']}",
                     "USGS Fenneman & Johnson 1946, bundled"))
    B = d.buildings
    if B:
        nearest = (f"; nearest {B['nearest_outside_m']:.0f} m outside ({B['nearest_outside']['occupancy']}, "
                   f"{B['nearest_outside']['sqft']:,.0f} sq ft)") if B["nearest_outside"] else ""
        rows.append(("buildings on the parcel", f"{B['count']}{nearest}",
                     f"FEMA USA Structures, imagery {', '.join(B['image_dates'])}, CC BY 4.0"))
    T = d.transmission
    if T:
        n, k = T["nearest"], T["nearest_known_voltage"]
        rows.append(("nearest transmission line", f"{n['distance_m'] / MI:.2f} mi ({n['distance_m']:,.0f} m), voltage "
                     + (f"{n['voltage_kv']:.0f} kV" if n["voltage_kv"] else "not recorded") if n else "none within 5 mi",
                     "HIFLD, archived September 2024"))
        if k and k is not n:
            rows.append(("  nearest with known voltage", f"{k['voltage_kv']:.0f} kV at {k['distance_m'] / MI:.2f} mi "
                         f"({k['distance_m']:,.0f} m){', ' + k['owner'] if k['owner'] else ''}", "same"))
    rows.append(("broadband", "not assessed", "dropped from v1 (branch 17 step 0)"))
    W = d.wildlife
    if W and W["surveyed"]:
        c, s = W["cattle"], W["sheep"]
        rows.append(("wildlife line", f"coyotes: {c['calf_pct']}% of calf, {c['cattle_pct']}% of cattle predator deaths "
                     f"({c['year']}); {s['pct']:.0f}% of sheep and lamb ({s['year']}); also {', '.join(W['also'])}",
                     "USDA APHIS NAHMS death-loss surveys, bundled"))

    print("=" * 110)
    print("I SITE OVERVIEW -- every figure, reference parcel")
    print("=" * 110)
    for name, value, source in rows:
        print(f"  {name:<30} {value:<62} {source}")
    print()

    cc = d.context_contours
    cdem = data.context_dem
    print("THE CONTEXT DEM AT THE WIDER EXTENT")
    print(f"  {cdem['array'].shape[1]} x {cdem['array'].shape[0]} cells, "
          f"{cdem['resolution_meters'][0]:.2f} x {cdem['resolution_meters'][1]:.2f} m (the 300-cell cap), "
          f"{cdem['array'].shape[1] * cdem['resolution_meters'][0] / 1000:.2f} x "
          f"{cdem['array'].shape[0] * cdem['resolution_meters'][1] / 1000:.2f} km, {cdem['crs']}")
    print(f"  {cc['min_ft']:,.0f} to {cc['max_ft']:,.0f} ft, relief {cc['relief_ft']:.0f} ft -> "
          f"{cc['interval_ft']} ft interval, index every {cc['index_every']} "
          f"({cc['interval_ft'] * cc['index_every']} ft): {len(cc['levels'])} levels, index "
          f"{[lv['elevation_ft'] for lv in cc['levels'] if lv['index']]}")
    for relief in (100, 200, 317, 400, 800):
        print(f"    relief {relief:>4} ft -> {od.context_contour_interval_ft(relief)} ft")
    print()

    print("LANDSCAPE POSITION AT THREE RADII (the page prints 500 m)")
    saved = od.LANDSCAPE_TPI_RADIUS_M
    try:
        for radius in (300.0, 500.0, 800.0):
            od.LANDSCAPE_TPI_RADIUS_M = radius
            other = od.derive_landscape(cdem, inputs.boundary_polygon_utm)
            print(f"  {radius:>5.0f} m  z {other['z']:+.2f}  sd {other['tpi_sd_m']:.1f} m  -> {other['class']}")
    finally:
        od.LANDSCAPE_TPI_RADIUS_M = saved
    print()

    print("AGREEMENT")
    print(f"  cover acreage {site_report.parcel_acres_label(terrain)} ac; overview {round(d.acres, 1):.1f} ac; "
          f"equal: {site_report.parcel_acres_label(terrain) == f'{round(d.acres, 1):,.1f}'}")
    print(f"  Access perimeter {frontage['perimeter_m']:.3f} m; overview {d.perimeter_m:.3f} m; "
          f"equal: {abs(frontage['perimeter_m'] - d.perimeter_m) < 1e-9}")
    print()

    record = fixture.capture_record()
    print(f"CALLS -- the overview's report-layer fetches, captured {record['captured_on']}")
    total_requests = total_bytes = 0
    for name, entry in record["files"].items():
        total_requests += entry["requests"]
        total_bytes += entry["response_bytes"]
        print(f"  {name:<20} {entry['requests']} request(s) {entry['response_bytes']:>9,} B  {entry['seconds']:>4.1f} s  "
              f"{entry['describes']}")
    print(f"  {'total':<20} {total_requests} requests    {total_bytes:>9,} B; bundled (no call): physiography, "
          f"NAHMS predator losses ({len(lp.load_bundle()['states'])} States)")
    print()
    print(offline_harness.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main())
