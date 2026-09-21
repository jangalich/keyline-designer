"""
test_climate_report.py

Offline checks for climate_report.derive_climate() -- each method stated
in that module's docstring, proven on synthetic daily arrays where the
right answer is known by construction, then on the real Daymet fixture
(daymet_reference_fixture.csv, reference parcel, 1995-2024) where the
answer is checked against a published sanity band and REPORTED, never
adjusted.

The three data facts, each with a case that fails if the fact is ignored:
  A. srad x dayl (data fact 2): a day of srad 200 W/m^2 over 12 h of
     daylight is 2.4 kWh/m^2; reading srad as a 24-hour mean gives 4.8.
  B. the 365-day year (data fact 1): a leap year's rows land on Feb 29
     and Dec 30; the December total counts 30 days; February counts 29.
  C. metric storage (data fact 3): the block holds degC and mm; the only
     degF figure is GDD, which is DEFINED on a degF base.

And the derivations:
  D. monthly precipitation is the mean of monthly TOTALS, not the mean
     daily rate x days, and annual is the mean of annual totals.
  E. GDD is simple-average, base 50 degF, floored at zero, no ceiling.
  F. frost dates: last before Jul 1 / first on or after Jul 1; a year
     with no frost on one side contributes no date and is listed.
  G. frost-free days is the MEDIAN OF PER-YEAR GAPS, and a constructed
     case shows it differs from the difference of the medians.
  H. medians of dates use the common reference calendar (a leap year's
     Feb 29 counts as day 60) and round half up.
  I. hardiness zone mapping at the zone edges.
  J. the fixture: every figure printed and checked against the western
     Pennsylvania bands; a figure outside a band is printed as OUTSIDE and
     the test still passes -- the band is a sanity check on the METHOD,
     asserted only where the method itself would produce nonsense.
"""

from datetime import date

import climate_report
from climate_report import (
    daily_gdd_f,
    daily_solar_kwh_m2,
    derive_climate,
    hardiness_zone_from_min_f,
    celsius_to_fahrenheit,
)
from daymet_data import DAYS_PER_DAYMET_YEAR, parse_daymet_csv


def _synthetic(years, tmax_fn, tmin_fn, prcp_fn=lambda y, d: 0.0, srad=200.0, dayl=43200.0):
    """A Daymet-shaped dict: 365 rows per year, values from the callables."""
    daily = {k: [] for k in ("year", "yday", "tmax", "tmin", "prcp", "srad", "dayl")}
    for y in years:
        for d in range(1, DAYS_PER_DAYMET_YEAR + 1):
            daily["year"].append(y)
            daily["yday"].append(d)
            daily["tmax"].append(tmax_fn(y, d))
            daily["tmin"].append(tmin_fn(y, d))
            daily["prcp"].append(prcp_fn(y, d))
            daily["srad"].append(srad)
            daily["dayl"].append(dayl)
    return daily


def _close(a, b, tol=1e-9):
    return abs(a - b) <= tol


# ======================================================================
# A. srad x dayl
# ======================================================================
print("A. srad is a daylight-mean flux: energy = srad x dayl")
assert _close(daily_solar_kwh_m2(200.0, 43200.0), 2.4)
wrong = 200.0 * 86400.0 / 3.6e6
assert _close(wrong, 4.8) and not _close(daily_solar_kwh_m2(200.0, 43200.0), wrong)
flat = _synthetic([2001], lambda y, d: 20.0, lambda y, d: 10.0, srad=200.0, dayl=43200.0)
block = derive_climate(flat)
for row in block["monthly"]:
    assert _close(row["solar_kwh_m2_day"], 2.4), row
print("   200 W/m2 over 12 h = 2.4 kWh/m2/day (24-h reading would say 4.8)")

# ======================================================================
# B. The 365-day year inside the derivation
# ======================================================================
print("B. leap-year rows land on Feb 29 and Dec 30; month day-counts follow")
leap = derive_climate(_synthetic([2000], lambda y, d: 20.0, lambda y, d: 10.0, prcp_fn=lambda y, d: 1.0))
common = derive_climate(_synthetic([2001], lambda y, d: 20.0, lambda y, d: 10.0, prcp_fn=lambda y, d: 1.0))
assert leap["monthly"][1]["day_count"] == 29 and common["monthly"][1]["day_count"] == 28
assert leap["monthly"][11]["day_count"] == 30 and common["monthly"][11]["day_count"] == 31
# 1 mm every day: December's TOTAL is 30 mm in the leap year, 31 otherwise.
assert _close(leap["monthly"][11]["prcp_total_mm"], 30.0)
assert _close(common["monthly"][11]["prcp_total_mm"], 31.0)
assert _close(leap["annual"]["prcp_total_mm"], 365.0) and _close(common["annual"]["prcp_total_mm"], 365.0)
print("   Feb 29 days, Dec 30 days in 2000; 28 and 31 in 2001; annual total 365 mm either way")

# ======================================================================
# C. Metric storage
# ======================================================================
print("C. the block stores degC and mm; GDD alone is on a degF base by definition")
assert _close(common["monthly"][0]["tmax_mean_c"], 20.0) and _close(common["monthly"][0]["tmin_mean_c"], 10.0)
# (20 + 10)/2 = 15 C = 59 F -> 9 GDD per day.
assert _close(daily_gdd_f(20.0, 10.0), 9.0)
assert _close(common["monthly"][0]["gdd_f"], 9.0 * 31)
assert _close(celsius_to_fahrenheit(0.0), 32.0) and _close(celsius_to_fahrenheit(100.0), 212.0)
print("   January mean high 20.0 C stored as 20.0; GDD 9/day at a 15 C mean")

# ======================================================================
# D. Monthly precipitation: mean of totals
# ======================================================================
print("D. monthly precipitation is the mean over years of monthly TOTALS")
# Year 1: 2 mm every day in January (62 mm); year 2: 0 mm in January.
two = derive_climate(
    _synthetic([2001, 2002], lambda y, d: 20.0, lambda y, d: 10.0,
               prcp_fn=lambda y, d: 2.0 if (y == 2001 and d <= 31) else 0.0)
)
assert _close(two["monthly"][0]["prcp_total_mm"], 31.0), two["monthly"][0]   # (62 + 0) / 2
assert _close(two["annual"]["prcp_total_mm"], 31.0)
assert two["annual"]["prcp_totals_by_year"] == {2001: 62.0, 2002: 0.0}
assert two["annual"]["wettest_month"] == 1
print("   (62 mm + 0 mm) / 2 years = 31.0 mm January mean")

# ======================================================================
# E. GDD: simple average, base 50, floor 0, no ceiling
# ======================================================================
print("E. GDD is simple-average base 50 degF, floored at 0, uncapped")
assert _close(daily_gdd_f(0.0, -10.0), 0.0)             # cold day: 0, never negative
assert _close(daily_gdd_f(40.0, 30.0), (104.0 + 86.0) / 2 - 50.0)   # 45 -- no 86 F cap applied
assert _close(daily_gdd_f(10.0, 10.0), 0.0)             # exactly 50 F mean: 0
print("   a 35 C-mean day scores 45 GDD (an 86 F-capped method would say 36)")

# ======================================================================
# F. Frost dates, and a year with no frost on one side
# ======================================================================
print("F. last frost before Jul 1, first frost on/after Jul 1; a frostless side is listed")


def _tmin_with_frosts(spring_ydays, fall_ydays):
    """tmin is 5 C except on the named ydays, where it is -1 C."""
    return lambda y, d: -1.0 if d in spring_ydays.get(y, ()) or d in fall_ydays.get(y, ()) else 5.0


f = derive_climate(
    _synthetic(
        [2001, 2002, 2003],
        lambda y, d: 20.0,
        _tmin_with_frosts(
            spring_ydays={2001: (10, 100), 2002: (120,), 2003: ()},        # 2003: no spring frost
            fall_ydays={2001: (290, 300), 2002: (), 2003: (280,)},          # 2002: no fall frost
        ),
    )
)
per = {e["year"]: e for e in f["frost"]["per_year"]}
assert per[2001]["last_spring"] == date(2001, 4, 10) and per[2001]["first_fall"] == date(2001, 10, 17)
assert per[2001]["frost_free_days"] == 190
assert per[2002]["last_spring"] == date(2002, 4, 30) and per[2002]["first_fall"] is None
assert per[2002]["frost_free_days"] is None
assert per[2003]["last_spring"] is None and per[2003]["first_fall"] == date(2003, 10, 7)
assert f["frost"]["years_without_spring_frost"] == [2003]
assert f["frost"]["years_without_fall_frost"] == [2002]
assert f["frost"]["years_with_spring_frost"] == 2 and f["frost"]["years_with_fall_frost"] == 2
assert f["frost"]["years_with_both"] == 1
assert f["frost"]["frost_free_days"] == 190          # the one year with both
# A frost ON Jul 1 is a fall frost, and a frost on Jun 30 a spring one.
edge = derive_climate(
    _synthetic([2001], lambda y, d: 20.0, _tmin_with_frosts({2001: (181,)}, {2001: (182,)}))
)
assert edge["frost"]["per_year"][0]["last_spring"] == date(2001, 6, 30)
assert edge["frost"]["per_year"][0]["first_fall"] == date(2001, 7, 1)
assert edge["frost"]["per_year"][0]["frost_free_days"] == 1
print("   2002 has no fall frost and 2003 no spring frost; both listed, neither fabricated")

# ======================================================================
# G. Frost-free days: median of gaps, not difference of medians
# ======================================================================
print("G. frost-free days is the median of per-year gaps, which differs from the difference of medians")
# Three years: (spring 100, fall 280) gap 180; (spring 120, fall 300) gap 180;
# (spring 110, fall 260) gap 150. Median spring 110, median fall 280 ->
# difference 170. Median of gaps: 180. Different numbers.
g = derive_climate(
    _synthetic(
        [2001, 2002, 2003],
        lambda y, d: 20.0,
        _tmin_with_frosts({2001: (100,), 2002: (120,), 2003: (110,)}, {2001: (280,), 2002: (300,), 2003: (260,)}),
    )
)
assert g["frost"]["frost_free_days"] == 180, g["frost"]
assert g["frost"]["frost_free_days_from_medians"] == 170, g["frost"]
assert g["frost"]["last_spring"] == {"month": 4, "day": 20, "reference_yday": 110}
assert g["frost"]["first_fall"] == {"month": 10, "day": 7, "reference_yday": 280}
print("   median of gaps 180 vs difference of medians 170 on the constructed case")

# ======================================================================
# H. Median dates on the reference calendar, half-up rounding
# ======================================================================
print("H. a leap year's late-season yday maps to the common calendar; .5 medians round half up")
# Same calendar date (Apr 20) in a leap year and a common year must give
# the same reference yday: Apr 20 is yday 111 in 2000 and 110 in 2001.
assert climate_report._reference_yday(2000, 111) == 110 == climate_report._reference_yday(2001, 110)
assert climate_report._reference_yday(2000, 60) == 60      # Feb 29 counts as day 60 (Mar 1 reference)
assert climate_report._reference_yday(2000, 59) == 59
h = derive_climate(
    _synthetic([2000, 2001], lambda y, d: 20.0, _tmin_with_frosts({2000: (111,), 2001: (110,)}, {2000: (281,), 2001: (280,)}))
)
assert h["frost"]["last_spring"]["reference_yday"] == 110 and h["frost"]["first_fall"]["reference_yday"] == 280
assert h["frost"]["last_spring"] == {"month": 4, "day": 20, "reference_yday": 110}
# Two years, ydays 110 and 111 -> median 110.5 -> half up -> 111.
h2 = derive_climate(
    _synthetic([2001, 2002], lambda y, d: 20.0, _tmin_with_frosts({2001: (110,), 2002: (111,)}, {2001: (280,), 2002: (280,)}))
)
assert h2["frost"]["last_spring"]["reference_yday"] == 111
assert climate_report._round_half_up(2.5) == 3 and climate_report._round_half_up(3.5) == 4
print("   Apr 20 in 2000 and 2001 agree at reference day 110; 110.5 rounds to 111")

# ======================================================================
# I. Hardiness zone edges
# ======================================================================
print("I. USDA half-zones from the mean annual extreme minimum")
assert hardiness_zone_from_min_f(-8.0) == "6a"
assert hardiness_zone_from_min_f(-3.0) == "6b"
assert hardiness_zone_from_min_f(-10.0) == "6a"      # the zone floor belongs to the zone
assert hardiness_zone_from_min_f(-5.0) == "6b"       # the half boundary belongs to 'b'
assert hardiness_zone_from_min_f(0.0) == "7a"
assert hardiness_zone_from_min_f(-10.01) == "5b"
assert hardiness_zone_from_min_f(-70.0) == "1a" and hardiness_zone_from_min_f(80.0) == "13b"
# Per-year minimum, then the mean: two years at -10 C and -20 C -> -15 C = 5 F -> 7b.
i = derive_climate(_synthetic([2001, 2002], lambda y, d: 20.0, lambda y, d: -10.0 if y == 2001 else -20.0))
assert i["hardiness"]["annual_min_c_by_year"] == {2001: -10.0, 2002: -20.0}
assert _close(i["hardiness"]["mean_annual_min_f"], 5.0) and i["hardiness"]["zone"] == "7b"
print("   -8 F -> 6a, -3 F -> 6b, boundaries at -10 and -5 F; mean of per-year minimums")

# ======================================================================
# J. The fixture, against the western Pennsylvania bands
# ======================================================================
print("J. the real fixture, against the sanity bands (reported, not fitted)")
with open("daymet_reference_fixture.csv", encoding="utf-8") as handle:
    fixture = derive_climate(parse_daymet_csv(handle.read()))
assert fixture["year_count"] == 30 and fixture["period"] == {"start": 1995, "end": 2024}
frost = fixture["frost"]
assert frost["years_with_both"] == 30 and not frost["years_without_spring_frost"] and not frost["years_without_fall_frost"]
mm_per_inch = 25.4
figures = {
    "last spring frost (ref yday)": (frost["last_spring"]["reference_yday"], 110, 135),
    "first fall frost (ref yday)": (frost["first_fall"]["reference_yday"], 274, 288),
    "annual precipitation (in)": (fixture["annual"]["prcp_total_mm"] / mm_per_inch, 38.0, 42.0),
    "annual GDD base 50F": (fixture["annual"]["gdd_f"], 2800.0, 3300.0),
}
for name, (value, low, high) in figures.items():
    inside = low <= value <= high
    print(f"   {name:<30} {value:8.1f}   band {low:6.1f}-{high:6.1f}   {'inside' if inside else 'OUTSIDE'}")
# What the METHOD guarantees on real data, asserted: the medians fall in
# the spring and fall halves, the gap is positive and equals what the
# per-year table implies, and the zone is a real label.
assert 1 <= frost["last_spring"]["reference_yday"] < 182 <= frost["first_fall"]["reference_yday"] <= 365
assert 0 < frost["frost_free_days"] < 365
gaps = sorted(e["frost_free_days"] for e in frost["per_year"])
assert frost["frost_free_days"] == climate_report._round_half_up((gaps[14] + gaps[15]) / 2)
assert fixture["hardiness"]["zone"] in {"5a", "5b", "6a", "6b", "7a", "7b"}, fixture["hardiness"]
assert fixture["annual"]["wettest_month"] in range(1, 13)
assert all(1.0 < row["solar_kwh_m2_day"] < 7.0 for row in fixture["monthly"])
assert all(-15.0 < row["tmin_mean_c"] < 20.0 and -5.0 < row["tmax_mean_c"] < 35.0 for row in fixture["monthly"])
print(f"   frost-free {frost['frost_free_days']} d (gaps median), zone {fixture['hardiness']['zone']}, wettest month {fixture['annual']['wettest_month']}")

print("\ntest_climate_report.py: all sections passed")
