#!/usr/bin/env python3
"""
diagnose_climate_sources.py

PRINTS EVERY FIGURE BRANCH 7 ADDED TO THE CLIMATE SECTION, RAW, FOR THE
EYE -- the phase 1 checkpoint. Offline by default: the reference
fixtures (Daymet at the parcel and at the five nearest normals
stations, Atlas 14, POWER) and the two bundles (SPC reports, NCEI
normals), through report_data.report_data_from_fixtures(), which
derives every block exactly as the report job's fetch does.

    python3 diagnose_climate_sources.py           # from the fixtures, no network
    python3 diagnose_climate_sources.py --live    # fetch_report_data() for real

What it prints, in order, so each number can be checked against the
method stated in its module rather than trusted:

  1. The precipitation correction: each station, its normal, Daymet at
     it, the ratio; the median; the parcel's raw and corrected annual.
  2. The monthly table: mean high/low, precipitation (corrected), PET,
     balance, heavy-rain days, GDD, day length, solar.
  3. Annual PET against the published Thornthwaite figure (671 mm,
     Pittsburgh airport) and FAO-56 Penman-Monteith from POWER (716 mm,
     step 0), with the gap. Deficit months and total.
  4. Heavy-rain days per year against the NCEI normals' own count of
     days >= 1.00 in at the same stations.
  5. Day length, driest and wettest year, the largest day.
  6. Design storms as served, and the series.
  7. Wind by season: sector frequencies, mean speed, prevailing.
  8. Severe weather within the radius.

Prints imperial beside metric because the references are quoted
imperial; the stored blocks stay metric.
"""

import os
import sys

MM_PER_INCH = 25.4
MPH_PER_M_S = 2.23694
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

PUBLISHED_THORNTHWAITE_MM = 671.0     # Waltman et al., Soil Climate Regimes of Pennsylvania, Pittsburgh AP, 1961-1990
STEP0_FAO56_MM = 716.0                # FAO-56 Penman-Monteith from NASA POWER at the parcel, 1995-2024 (branch 7 step 0)


def _f(c):
    return c * 9.0 / 5.0 + 32.0


def load_fixtures():
    from daymet_data import parse_daymet_csv
    from atlas14_data import parse_atlas14_csv
    from power_wind_data import parse_power_csv
    from reference_fixture import REAL_BOUNDARY
    from report_data import report_data_from_fixtures
    import precipitation_normals

    with open("daymet_reference_fixture.csv", encoding="utf-8") as handle:
        daily = parse_daymet_csv(handle.read())
    with open("atlas14_reference_fixture.csv", encoding="utf-8") as handle:
        atlas14 = parse_atlas14_csv(handle.read())
    with open("power_wind_reference_fixture.csv", encoding="utf-8") as handle:
        power = parse_power_csv(handle.read())
    centroid = __import__("report_data").boundary_centroid_lat_lon(REAL_BOUNDARY)
    stations = {}
    for station in precipitation_normals.nearest_stations(*centroid):
        path = os.path.join("daymet_station_fixtures", station["station"] + ".csv")
        with open(path, encoding="utf-8") as handle:
            stations[station["station"]] = parse_daymet_csv(handle.read(), required_variables=("prcp",))
    return report_data_from_fixtures(REAL_BOUNDARY, daily, station_daily=stations, atlas14=atlas14, power_wind=power)


def main(argv) -> int:
    if "--live" in argv:
        from reference_fixture import REAL_BOUNDARY
        from report_data import fetch_report_data

        data = fetch_report_data(REAL_BOUNDARY)
        print("LIVE fetch; unavailable:", data.unavailable or "none")
    else:
        import offline_harness

        offline_harness.install()
        data = load_fixtures()
    climate = data.climate
    annual = climate["annual"]
    import precipitation_normals

    print(f"\nPoint {data.centroid[0]:.4f}, {data.centroid[1]:.4f}; Daymet {climate['period']['start']}-{climate['period']['end']}\n")

    print("1. PRECIPITATION CORRECTION (normals 1991-2020 / Daymet 1991-2020 at each station)")
    correction = data.precipitation_correction
    for row in correction["stations"]:
        print(f"   {row['name']:<36} {row['distance_miles']:5.1f} mi  normal {row['normal_in']:6.2f} in  "
              f"Daymet {row['daymet_mm'] / MM_PER_INCH:6.2f} in  ratio {row['ratio']:.3f}  ({row['flag']}, {row['years']} yr)")
    raw_annual = annual["prcp_total_mm"] / climate["prcp_factor"]
    print(f"   factor {correction['factor']:.3f} (median of {correction['station_count']}); applied: {correction['applied']}")
    print(f"   parcel annual: raw {raw_annual / MM_PER_INCH:.1f} in -> corrected {annual['prcp_total_mm'] / MM_PER_INCH:.1f} in\n")

    print("2. MONTHLY TABLE (corrected precipitation; PET Thornthwaite)")
    print(f"   {'':4}{'hi F':>6}{'lo F':>6}{'P in':>7}{'PET in':>8}{'bal in':>8}{'>=1in':>7}{'GDD':>6}{'day h':>7}{'kWh':>6}")
    for m in climate["monthly"]:
        print(f"   {MONTHS[m['month'] - 1]:4}{_f(m['tmax_mean_c']):6.0f}{_f(m['tmin_mean_c']):6.0f}"
              f"{m['prcp_total_mm'] / MM_PER_INCH:7.2f}{m['pet_mm'] / MM_PER_INCH:8.2f}{m['balance_mm'] / MM_PER_INCH:8.2f}"
              f"{m['heavy_rain_days']:7.2f}{m['gdd_f']:6.0f}{m['day_length_h']:7.1f}{m['solar_kwh_m2_day']:6.1f}")
    print()

    print("3. ANNUAL PET AND THE BALANCE")
    pet = annual["pet_mm"]
    print(f"   PET {pet:.0f} mm ({pet / MM_PER_INCH:.1f} in); published Thornthwaite {PUBLISHED_THORNTHWAITE_MM:.0f} mm "
          f"({(pet - PUBLISHED_THORNTHWAITE_MM) / PUBLISHED_THORNTHWAITE_MM * 100:+.1f}%); "
          f"FAO-56 PM from POWER {STEP0_FAO56_MM:.0f} mm ({(pet - STEP0_FAO56_MM) / STEP0_FAO56_MM * 100:+.1f}%)")
    print(f"   heat index {climate['pet']['heat_index']:.2f}, exponent {climate['pet']['exponent']:.3f}")
    print(f"   annual balance {annual['balance_mm'] / MM_PER_INCH:+.1f} in; deficit months {[MONTHS[m - 1] for m in annual['deficit_months']]}, "
          f"total deficit {annual['deficit_mm'] / MM_PER_INCH:.2f} in ({annual['deficit_mm']:.0f} mm)\n")

    print("4. HEAVY-RAIN DAYS (>= 25.4 mm)")
    print(f"   parcel: {annual['heavy_rain_days']:.2f} days/yr (corrected series); by month "
          f"{[round(m['heavy_rain_days'], 2) for m in climate['monthly']]}")
    _, stations = precipitation_normals.load_bundle()
    by_id = {s["station"]: s for s in stations}
    for row in correction["stations"]:
        print(f"   NCEI normal days >= 1.00 in at {row['name'].split(',')[0]}: {by_id[row['station']]['days_ge_1in']}")
    print()

    print("5. DAY LENGTH, VARIABILITY")
    dl = climate["day_length"]
    print(f"   longest {dl['longest']['hours']:.2f} h on {MONTHS[dl['longest']['month'] - 1]} {dl['longest']['day']}; "
          f"shortest {dl['shortest']['hours']:.2f} h on {MONTHS[dl['shortest']['month'] - 1]} {dl['shortest']['day']}")
    print(f"   driest {annual['driest_year']['year']}: {annual['driest_year']['prcp_total_mm'] / MM_PER_INCH:.1f} in; "
          f"wettest {annual['wettest_year']['year']}: {annual['wettest_year']['prcp_total_mm'] / MM_PER_INCH:.1f} in; "
          f"largest day {annual['largest_day']['prcp_mm'] / MM_PER_INCH:.2f} in on {annual['largest_day']['date']}\n")

    print("6. DESIGN STORMS")
    if data.design_storms:
        a = data.atlas14
        print(f"   {a['source_line']} ({a['project_area']}), {a['series']} series, records through {a['record_ends']}, {a['units']}")
        for row in data.design_storms["rows"]:
            print(f"   {row['duration']:>7}: " + "  ".join(f"{ari}-yr {row['depths'][ari]:.2f}" for ari in data.design_storms["aris"]))
    else:
        print("   unavailable:", data.unavailable.get("atlas14"))
    print()

    print("7. WIND (direction FROM; POWER MERRA-2 cell)")
    if data.wind:
        for name, season in data.wind["seasons"].items():
            freq = "  ".join(f"{s} {season['sector_frequency'][s] * 100:4.1f}%" for s in data.wind["sectors"])
            print(f"   {name:6} {season['days']} days  mean {season['mean_speed_m_s']:.2f} m/s ({season['mean_speed_m_s'] * MPH_PER_M_S:.1f} mph)  "
                  f"prevailing {season['prevailing_sector']} ({season['prevailing_degrees']:.0f} deg)")
            print(f"          {freq}")
        print(f"   fill days dropped: {data.wind['fill_days']}")
    else:
        print("   unavailable:", data.unavailable.get("power_wind"))
    print()

    print("8. SEVERE WEATHER (SPC reports)")
    sw = data.severe_weather
    print(f"   within {sw['radius_miles']:.0f} mi, {sw['window']['start']}-{sw['window']['end']}: "
          + ", ".join(f"{k} {sw['counts'][k]} ({sw['per_year'][k]:.1f}/yr)" for k in sw["counts"]))
    for kind in sw["by_month"]:
        print(f"   {kind} by month: {sw['by_month'][kind]} (peak {MONTHS[sw[kind + '_peak_month'] - 1]})")
    print(f"   largest hail {sw['largest_hail_in']} in; vintage {sw['vintage']['hail']['last_modified']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
