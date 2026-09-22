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


# ======================================================================
# K. The heavy-rain threshold, at exactly 25.4 mm
# ======================================================================
print("K. a heavy-rain day is >= 25.4 mm: exactly 25.4 counts, 25.39 does not")
from climate_report import HEAVY_RAIN_MM, is_heavy_rain_day
assert HEAVY_RAIN_MM == 25.4 and is_heavy_rain_day(25.4) and not is_heavy_rain_day(25.39) and is_heavy_rain_day(25.41)
assert is_heavy_rain_day(1.0 * 25.4), "one inch converted is the threshold itself"
heavy = derive_climate(
    _synthetic([2001, 2002], lambda y, d: 20.0, lambda y, d: 10.0,
               prcp_fn=lambda y, d: 25.4 if (y, d) == (2001, 10) else 25.39 if (y, d) == (2001, 11) else 40.0 if (y, d) == (2002, 200) else 0.0)
)
assert heavy["annual"]["heavy_rain_days_by_year"] == {2001: 1, 2002: 1}
assert _close(heavy["annual"]["heavy_rain_days"], 1.0)
assert _close(heavy["monthly"][0]["heavy_rain_days"], 0.5)          # one January day in one of two years
assert _close(heavy["monthly"][6]["heavy_rain_days"], 0.5)          # yday 200 is Jul 19
assert heavy["annual"]["largest_day"] == {"date": date(2002, 7, 19), "prcp_mm": 40.0}
print("   25.4 counts, 25.39 does not; largest day Jul 19 2002 at 40 mm")

# ======================================================================
# L. Thornthwaite PET against a hand-computed case
# ======================================================================
print("L. Thornthwaite: a constant 10 C, 12 h, 30-day month evaporates 48.9 mm; zero at or below 0 C")
from climate_report import (
    thornthwaite_exponent, thornthwaite_heat_index, thornthwaite_monthly_pet_mm, PET_METHOD,
)
# By hand: 2^1.514 = e^(1.514 x 0.693147) = e^1.04942 = 2.8560, so I = 12 x 2.8560 = 34.272;
# a = 6.75e-7 I^3 - 7.71e-5 I^2 + 1.792e-2 I + 0.49239 = 0.02717 - 0.09056 + 0.61415 + 0.49239 = 1.0432;
# PET = 16 x (100 / 34.272)^1.0432 = 16 x e^(1.0432 x ln 2.9178) = 16 x e^1.1172 = 16 x 3.0563 = 48.90 mm.
I = thornthwaite_heat_index([10.0] * 12)
a = thornthwaite_exponent(I)
assert abs(I - 34.272) < 0.001 and abs(a - 1.0432) < 0.0005, (I, a)
pet = thornthwaite_monthly_pet_mm(10.0, I, a, 12.0, 30.0)
assert abs(pet - 48.89) < 0.05, pet
assert _close(thornthwaite_monthly_pet_mm(10.0, I, a, 24.0, 30.0), 2 * pet), "day length scales linearly"
assert _close(thornthwaite_monthly_pet_mm(10.0, I, a, 12.0, 15.0), pet / 2), "days scale linearly"
assert thornthwaite_monthly_pet_mm(0.0, I, a, 12.0, 30.0) == 0.0 and thornthwaite_monthly_pet_mm(-5.0, I, a, 12.0, 30.0) == 0.0
assert thornthwaite_heat_index([-3.0] * 12) == 0.0 and thornthwaite_monthly_pet_mm(5.0, 0.0, 0.5, 12.0, 30.0) == 0.0
# Above 26.5 C the Willmott polynomial: -415.85 + 32.24 x 30 - 0.43 x 900 = 164.35 mm at 12 h / 30 d.
assert abs(thornthwaite_monthly_pet_mm(30.0, I, a, 12.0, 30.0) - 164.35) < 0.01
# Through derive_climate: constant 15/5 C (mean 10 C), 12 h days, the
# block's monthly PET is the hand figure scaled by each month's days.
flat10 = derive_climate(_synthetic([2001], lambda y, d: 15.0, lambda y, d: 5.0, dayl=43200.0))
assert abs(flat10["pet"]["heat_index"] - I) < 1e-9 and flat10["pet"]["method"] == PET_METHOD
assert abs(flat10["monthly"][0]["pet_mm"] - pet * 31 / 30) < 1e-6 and abs(flat10["monthly"][1]["pet_mm"] - pet * 28 / 30) < 1e-6
assert abs(flat10["annual"]["pet_mm"] - pet * 365 / 30) < 1e-6
print(f"   I {I:.3f}, a {a:.4f}, PET {pet:.2f} mm; 30 C -> 164.35 mm by the high-temperature polynomial")

# ======================================================================
# M. The climatic water balance
# ======================================================================
print("M. balance is precipitation minus PET by month; deficit months and total follow; no soil term exists")
balance = derive_climate(
    _synthetic([2001], lambda y, d: 15.0, lambda y, d: 5.0, prcp_fn=lambda y, d: 3.0 if d <= 181 else 0.5, dayl=43200.0)
)
for row in balance["monthly"]:
    assert _close(row["balance_mm"], row["prcp_total_mm"] - row["pet_mm"]), row
# Jan-Jun: 3 mm/day against ~1.63 mm/day PET -> surplus; Jul-Dec: 0.5 mm/day -> deficit.
assert balance["annual"]["surplus_months"] == [1, 2, 3, 4, 5, 6] and balance["annual"]["deficit_months"] == [7, 8, 9, 10, 11, 12]
assert _close(balance["annual"]["deficit_mm"], -sum(r["balance_mm"] for r in balance["monthly"] if r["balance_mm"] < 0))
assert _close(balance["annual"]["balance_mm"], balance["annual"]["prcp_total_mm"] - balance["annual"]["pet_mm"])
assert not any("soil" in key for key in balance["annual"]) and not any("soil" in key for key in balance["monthly"][0])
print(f"   six surplus months, six deficit months, deficit {balance['annual']['deficit_mm']:.1f} mm")

# ======================================================================
# N. The precipitation factor
# ======================================================================
print("N. prcp_factor scales every precipitation figure and nothing else")
half = derive_climate(two_years := _synthetic([2001, 2002], lambda y, d: 20.0, lambda y, d: 10.0,
                                              prcp_fn=lambda y, d: 30.0 if d == 100 else 2.0), prcp_factor=0.5)
full = derive_climate(two_years)
assert half["prcp_factor"] == 0.5 and full["prcp_factor"] == 1.0
assert _close(half["annual"]["prcp_total_mm"], full["annual"]["prcp_total_mm"] / 2)
assert all(_close(h["prcp_total_mm"], f["prcp_total_mm"] / 2) for h, f in zip(half["monthly"], full["monthly"]))
assert all(h["pet_mm"] == f["pet_mm"] and h["tmax_mean_c"] == f["tmax_mean_c"] for h, f in zip(half["monthly"], full["monthly"]))
assert _close(half["annual"]["largest_day"]["prcp_mm"], 15.0) and full["annual"]["largest_day"]["prcp_mm"] == 30.0
assert full["annual"]["heavy_rain_days"] == 1.0 and half["annual"]["heavy_rain_days"] == 0.0, "a 30 mm day at 0.5 is 15 mm, not heavy"
assert _close(half["annual"]["driest_year"]["prcp_total_mm"], full["annual"]["driest_year"]["prcp_total_mm"] / 2)
print("   totals, largest day and driest year halve; heavy days recount on the scaled series; PET and temperature untouched")

# ======================================================================
# O. Day length, and the driest and wettest year
# ======================================================================
print("O. day length is dayl/3600 by month; the longest and shortest day carry their usual dates; years ranked")
import math as _math
def _dayl(y, d):
    # Peak on yday 172 (Jun 21 in a common year), trough exactly on yday 355 (Dec 21): a 366-day period puts the
    # trough 183 days after the peak, on one row rather than between two.
    return 43200.0 + 10800.0 * _math.cos(2 * _math.pi * (d - 172) / 366)
cycle = {k: [] for k in ("year", "yday", "tmax", "tmin", "prcp", "srad", "dayl")}
for y in (2001, 2002, 2003):
    for d in range(1, DAYS_PER_DAYMET_YEAR + 1):
        cycle["year"].append(y); cycle["yday"].append(d); cycle["tmax"].append(20.0); cycle["tmin"].append(10.0)
        cycle["prcp"].append(float(y - 2000)); cycle["srad"].append(200.0); cycle["dayl"].append(_dayl(y, d))
cyc = derive_climate(cycle)
assert _close(cyc["day_length"]["longest"]["hours"], 15.0) and (cyc["day_length"]["longest"]["month"], cyc["day_length"]["longest"]["day"]) == (6, 21)
assert _close(cyc["day_length"]["shortest"]["hours"], 9.0) and (cyc["day_length"]["shortest"]["month"], cyc["day_length"]["shortest"]["day"]) == (12, 21)
assert abs(cyc["monthly"][5]["day_length_h"] - 14.9) < 0.1 and abs(cyc["monthly"][11]["day_length_h"] - 9.1) < 0.1
assert cyc["annual"]["driest_year"] == {"year": 2001, "prcp_total_mm": 365.0}
assert cyc["annual"]["wettest_year"] == {"year": 2003, "prcp_total_mm": 3 * 365.0}
assert two["annual"]["driest_year"]["year"] == 2002 and two["annual"]["wettest_year"]["year"] == 2001   # section D's case
print("   longest Jun 21 at 15.0 h, shortest Dec 21 at 9.0 h; driest 2001, wettest 2003")

# ======================================================================
# P. The fixture's water figures against the published references
# ======================================================================
print("P. the fixture: Thornthwaite against the Penn State atlas figure, and the rest reported")
pet_mm = fixture["annual"]["pet_mm"]
published_mm = 671.0   # Pittsburgh airport, Thornthwaite on 1961-1990 normals (Waltman et al., Soil Climate Regimes of Pennsylvania)
assert abs(pet_mm - published_mm) / published_mm < 0.03, f"{pet_mm:.0f} mm vs {published_mm} mm published"
# UNCORRECTED precipitation here (factor 1.0): June's balance is +2 mm, so the deficit is July and August.
# With the 0.96 factor the report applies (test_precipitation_normals.py) June joins them.
assert fixture["annual"]["deficit_months"] == [7, 8], fixture["annual"]["deficit_months"]
assert (fixture["day_length"]["longest"]["month"], fixture["day_length"]["longest"]["day"]) == (6, 21)
assert (fixture["day_length"]["shortest"]["month"], fixture["day_length"]["shortest"]["day"]) == (12, 21)
assert 14.5 < fixture["day_length"]["longest"]["hours"] < 15.5 and 8.5 < fixture["day_length"]["shortest"]["hours"] < 9.5
assert fixture["annual"]["driest_year"]["year"] == 1995 and fixture["annual"]["wettest_year"]["year"] == 2018
assert fixture["annual"]["largest_day"]["date"] == date(2004, 9, 17)   # the remnants of Hurricane Ivan
print(f"   PET {pet_mm:.0f} mm vs 671 mm published ({(pet_mm - published_mm) / published_mm * 100:+.1f}%); "
      f"deficit months {fixture['annual']['deficit_months']}, {fixture['annual']['deficit_mm'] / 25.4:.2f} in uncorrected; "
      f"heavy days {fixture['annual']['heavy_rain_days']:.2f}/yr; largest day {fixture['annual']['largest_day']['prcp_mm']:.1f} mm on {fixture['annual']['largest_day']['date']}")

print("\ntest_climate_report.py: all sections passed")
