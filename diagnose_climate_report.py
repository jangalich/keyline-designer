"""
diagnose_climate_report.py

PRINTS THE CLIMATE NUMBERS, RAW, FOR THE EYE -- the phase 1 checkpoint of
the site data report (site-data-report-proposal.md), run over the Daymet
reference fixture (daymet_reference_fixture.csv: real single-pixel output
for the reference parcel, 1995-2024). Offline; no fetch.

    python3 diagnose_climate_report.py [path/to/daymet.csv]

What it prints, in order, so a reader can check each derivation against
the method stated in climate_report.py rather than trust the number:

  1. The monthly table and the six key figures.
  2. Frost-free days BOTH ways -- median of per-year gaps (the figure) and
     difference of the two median dates (NOT the figure) -- side by side.
  3. The solar row WITH the dayl correction (the figure) and WITHOUT it
     (srad treated as a 24-hour mean, the unit error) side by side.
  4. Per-year frost dates for every year, so an outlier is visible.
  5. Sanity bands for western Pennsylvania, with each figure marked inside
     or OUTSIDE. Bands, not targets: a figure outside one is reported here
     and nothing is adjusted to fit.

Prints imperial beside metric because the bands are quoted imperial; the
stored block stays metric (climate_report.py).
"""

import sys

from climate_report import celsius_to_fahrenheit, derive_climate
from daymet_data import parse_daymet_csv, summarize_daymet

MM_PER_INCH = 25.4
MONTHS = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"]
MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

# Western Pennsylvania sanity bands (the phase 1 brief). Reference ydays on
# the common calendar: Apr 20 = 110, May 15 = 135, Oct 1 = 274, Oct 15 = 288.
BANDS = {
    "last spring frost": (110, 135, "Apr 20 - May 15"),
    "first fall frost": (274, 288, "Oct 1 - Oct 15"),
    "annual precipitation in": (38.0, 42.0, "38 - 42 in"),
    "annual GDD base 50F": (2800.0, 3300.0, "2,800 - 3,300"),
}


def _date(d):
    return "  --  " if d is None else d.strftime("%b %d")


def _md(entry):
    return "--" if entry is None else f"{MONTH_NAMES[entry['month'] - 1][:3]} {entry['day']:2d}"


def main(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        daily = parse_daymet_csv(handle.read())
    print(summarize_daymet(daily))
    print()
    climate = derive_climate(daily)
    monthly = climate["monthly"]
    frost = climate["frost"]
    annual = climate["annual"]
    hardiness = climate["hardiness"]

    # 1. THE MONTHLY TABLE
    print("1. MONTHLY TABLE (means over %d years, %d-%d)" % (
        climate["year_count"], climate["period"]["start"], climate["period"]["end"]))
    head = "                    " + "".join(f"{m:>8}" for m in MONTHS)
    print(head)
    rows = [
        ("Mean high F", [celsius_to_fahrenheit(r["tmax_mean_c"]) for r in monthly], "{:8.1f}"),
        ("Mean low F", [celsius_to_fahrenheit(r["tmin_mean_c"]) for r in monthly], "{:8.1f}"),
        ("Mean high C", [r["tmax_mean_c"] for r in monthly], "{:8.1f}"),
        ("Mean low C", [r["tmin_mean_c"] for r in monthly], "{:8.1f}"),
        ("Precip in", [r["prcp_total_mm"] / MM_PER_INCH for r in monthly], "{:8.2f}"),
        ("Precip mm", [r["prcp_total_mm"] for r in monthly], "{:8.1f}"),
        ("GDD base 50F", [r["gdd_f"] for r in monthly], "{:8.0f}"),
        ("Solar kWh/m2/day", [r["solar_kwh_m2_day"] for r in monthly], "{:8.2f}"),
        ("Days counted", [r["day_count"] for r in monthly], "{:8d}"),
    ]
    for label, values, fmt in rows:
        print(f"{label:<20}" + "".join(fmt.format(v) for v in values))
    print()

    print("   KEY FIGURES")
    print(f"   median last spring frost : {_md(frost['last_spring'])}   (from {frost['years_with_spring_frost']} years)")
    print(f"   median first fall frost  : {_md(frost['first_fall'])}   (from {frost['years_with_fall_frost']} years)")
    print(f"   frost-free days          : {frost['frost_free_days']}   (median of {frost['years_with_both']} per-year gaps)")
    print(f"   annual precipitation     : {annual['prcp_total_mm'] / MM_PER_INCH:.1f} in   ({annual['prcp_total_mm']:.1f} mm)")
    print(f"   annual GDD base 50F      : {annual['gdd_f']:,.0f}")
    print(f"   est. hardiness zone      : {hardiness['zone']}   (mean annual min {hardiness['mean_annual_min_f']:.1f} F / {hardiness['mean_annual_min_c']:.1f} C)")
    print(f"   wettest month            : {MONTH_NAMES[annual['wettest_month'] - 1]}")
    print()

    # 2. FROST-FREE DAYS, BOTH WAYS
    print("2. FROST-FREE DAYS, BOTH WAYS")
    print(f"   median of per-year gaps (THE FIGURE)     : {frost['frost_free_days']}")
    print(f"   difference of the two medians (NOT used) : {frost['frost_free_days_from_medians']}")
    print(f"   years without a spring frost: {frost['years_without_spring_frost'] or 'none'}")
    print(f"   years without a fall frost  : {frost['years_without_fall_frost'] or 'none'}")
    print()

    # 3. SOLAR WITH AND WITHOUT THE dayl CORRECTION
    print("3. SOLAR, WITH AND WITHOUT THE dayl CORRECTION (kWh/m2/day)")
    # Without: srad taken as a 24-hour mean flux -> srad * 86400 / 3.6e6.
    by_month_wrong = [0.0] * 13
    count = [0] * 13
    from daymet_data import daymet_date
    for year, yday, srad in zip(daily["year"], daily["yday"], daily["srad"]):
        m = daymet_date(year, yday).month
        by_month_wrong[m] += srad * 86400.0 / 3.6e6
        count[m] += 1
    wrong = [by_month_wrong[m] / count[m] for m in range(1, 13)]
    right = [r["solar_kwh_m2_day"] for r in monthly]
    print("                    " + "".join(f"{m:>8}" for m in MONTHS))
    print(f"{'with dayl (FIGURE)':<20}" + "".join(f"{v:8.2f}" for v in right))
    print(f"{'without dayl (WRONG)':<20}" + "".join(f"{v:8.2f}" for v in wrong))
    print(f"{'ratio wrong/right':<20}" + "".join(f"{w / r:8.2f}" for w, r in zip(wrong, right)))
    print()

    # 4. PER-YEAR FROST DATES
    print("4. PER-YEAR FROST DATES (tmin <= 0 C; spring = last before Jul 1, fall = first on/after Jul 1)")
    print("   year   last spring   first fall   frost-free   annual min C   precip in   GDD")
    for entry in frost["per_year"]:
        y = entry["year"]
        gap = "  --" if entry["frost_free_days"] is None else f"{entry['frost_free_days']:4d}"
        print(
            f"   {y}   {_date(entry['last_spring']):>11}   {_date(entry['first_fall']):>10}   {gap:>10}   "
            f"{hardiness['annual_min_c_by_year'][y]:12.1f}   {annual['prcp_totals_by_year'][y] / MM_PER_INCH:9.1f}   "
            f"{annual['gdd_by_year'][y]:5.0f}"
        )
    print()

    # 5. SANITY BANDS
    print("5. SANITY BANDS (western Pennsylvania; reported, never fitted)")
    checks = [
        ("last spring frost", frost["last_spring"]["reference_yday"] if frost["last_spring"] else None, _md(frost["last_spring"])),
        ("first fall frost", frost["first_fall"]["reference_yday"] if frost["first_fall"] else None, _md(frost["first_fall"])),
        ("annual precipitation in", annual["prcp_total_mm"] / MM_PER_INCH, f"{annual['prcp_total_mm'] / MM_PER_INCH:.1f}"),
        ("annual GDD base 50F", annual["gdd_f"], f"{annual['gdd_f']:,.0f}"),
    ]
    outside = 0
    for name, value, shown in checks:
        low, high, band = BANDS[name]
        ok = value is not None and low <= value <= high
        outside += 0 if ok else 1
        print(f"   {name:<26} {shown:>10}   band {band:<18} {'inside' if ok else 'OUTSIDE'}")
    print(f"\n   {outside} figure(s) outside the bands.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "daymet_reference_fixture.csv"))
