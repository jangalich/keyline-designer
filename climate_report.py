"""
climate_report.py

The site data report's CLIMATE NUMBERS, derived from Daymet daily arrays
(daymet_data.parse_daymet_csv()'s dict). Pure functions, no network, no
numpy -- ten thousand rows of arithmetic.

    derive_climate(daily)  -> the climate block (see the return contract)

EVERY FIGURE IS DETERMINISTIC AND METRIC. The block stores degC, mm, kWh
and degree-days exactly as computed; converting to degF and inches is the
formatting layer's job (report_generator.py's rule, kept). The one
exception is growing degree days, which are DEFINED on a Fahrenheit base
(50 degF) and are stored as degF-days -- a unit, not a conversion.

THE METHODS, each stated here because each can be subtly wrong while
looking plausible (site-data-report-proposal.md, phase 1):

  Monthly mean high / low     mean of daily tmax / tmin over every day of
                              that calendar month across all years.
  Monthly precipitation       mean over YEARS of each month's TOTAL -- not
                              the mean daily rate. In a leap year Daymet's
                              December has 30 days (Dec 31 is dropped), so
                              that December total is one day short; with
                              8 leap years in 30 the December mean is
                              understated by roughly 0.9%. Reported, not
                              corrected: the day genuinely is not in the data.
  Monthly solar               mean of daily srad x dayl / 3.6e6 (kWh/m^2/day)
                              over the month -- DATA FACT 2: srad is the
                              daylight-mean flux in W/m^2, dayl the daylight
                              seconds.
  Monthly GDD, base 50 degF   per day max(0, (tmax_f + tmin_f)/2 - 50),
                              summed per month, mean over years. SIMPLE
                              AVERAGE method: no ceiling on tmax and no floor
                              on tmin, and the footer says so.
  Annual precipitation        mean of the annual totals.
  Annual GDD                  mean of the annual sums.
  Last spring frost           per year, the LAST day before July 1 with
                              tmin <= 0 degC. Median across years.
  First fall frost            per year, the FIRST day on or after July 1
                              with tmin <= 0 degC. Median across years.
  Frost-free days             per year, first fall frost minus last spring
                              frost, in days. MEDIAN OF THOSE GAPS -- not the
                              difference of the two median dates, which is a
                              different number; both are on the block so the
                              difference can be seen.
  Est. hardiness zone         per year, the annual minimum tmin; mean of
                              those minimums in degF; mapped to USDA
                              half-zones (10 degF zones from -60 degF, split
                              at 5 degF). Always "est." -- it is an estimate
                              from a 1 km reanalysis-style product over the
                              years fetched, not the official USDA map, which
                              is built from PRISM over 1991-2020.

A YEAR WITHOUT A FROST ON ONE SIDE OF JULY 1 IS POSSIBLE and is handled
explicitly: that year contributes no date to that median and no gap to the
frost-free median, and its year is listed under `years_without_spring_frost`
/ `years_without_fall_frost` so the report can say how many years the
median stands on. It is NOT treated as a frost on January 1 or December 31,
which would fabricate a date, and it is not dropped silently.

MEDIAN DATES. Each year's frost date is reduced to a day-of-year on a
COMMON 365-day reference calendar so leap years do not shift the median by
a day: yday as Daymet gives it in a common year; in a leap year, yday 60
(Feb 29) counts as day 60 (Mar 1 of the reference year) and every later
yday counts as yday - 1 (see _reference_yday). The median of those integers
is taken with statistics.median and rounded HALF UP to a whole day
(_round_half_up), then read back as (month, day) on the reference year.
Half-up rather than Python's banker's rounding so two runs cannot round
the same .5 two ways.
"""

import math
import statistics
from datetime import date, timedelta

from daymet_data import daymet_date

GDD_BASE_F = 50.0
FROST_THRESHOLD_C = 0.0
# The split between "spring" and "fall" frost searches: July 1.
MIDYEAR_MONTH, MIDYEAR_DAY = 7, 1
JOULES_PER_KWH = 3.6e6
DAYS_PER_YEAR = 365

# A common (non-leap) year to read a reference day-of-year back as a
# calendar date. 2001 is a common year; any would do.
REFERENCE_YEAR = 2001

# USDA hardiness zones: zone n spans [-60 + 10(n-1), -60 + 10n) degF, split
# into 'a' (lower 5 degF) and 'b' (upper 5 degF). Zone 1a is <= -55 degF's
# band and zone 13b tops out at 70 degF; values outside are clamped.
_ZONE_FLOOR_F = -60.0
_ZONE_MIN, _ZONE_MAX = 1, 13


def celsius_to_fahrenheit(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def daily_solar_kwh_m2(srad_w_m2: float, dayl_s: float) -> float:
    """DATA FACT 2: srad (daylight-mean W/m^2) x dayl (daylight seconds) is
    joules per m^2 for the day; / 3.6e6 is kWh/m^2/day."""
    return srad_w_m2 * dayl_s / JOULES_PER_KWH


def daily_gdd_f(tmax_c: float, tmin_c: float, base_f: float = GDD_BASE_F) -> float:
    """Simple-average growing degree days for one day, in degF-days."""
    mean_f = (celsius_to_fahrenheit(tmax_c) + celsius_to_fahrenheit(tmin_c)) / 2.0
    return max(0.0, mean_f - base_f)


def hardiness_zone_from_min_f(mean_annual_min_f: float) -> str:
    """USDA half-zone label ('6a') for a mean annual extreme minimum in degF."""
    offset = mean_annual_min_f - _ZONE_FLOOR_F
    zone = int(math.floor(offset / 10.0)) + 1
    zone = max(_ZONE_MIN, min(_ZONE_MAX, zone))
    within = offset - (zone - 1) * 10.0
    half = "a" if within < 5.0 else "b"
    return f"{zone}{half}"


def _round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _reference_yday(year: int, yday: int) -> int:
    """A Daymet (year, yday) as a day-of-year on the common 365-day
    reference calendar -- see the module docstring's MEDIAN DATES."""
    if _is_leap(year) and yday > 60:
        return yday - 1
    return yday


def _reference_date(reference_yday: int) -> date:
    return date(REFERENCE_YEAR, 1, 1) + timedelta(days=reference_yday - 1)


def _median_reference_date(reference_ydays: list):
    """(month, day) of the half-up-rounded median of reference ydays, or
    None when there are none."""
    if not reference_ydays:
        return None
    reference = _round_half_up(statistics.median(reference_ydays))
    reference = max(1, min(DAYS_PER_YEAR, reference))
    d = _reference_date(reference)
    return {"month": d.month, "day": d.day, "reference_yday": reference}


def _rows_by_year(daily: dict) -> dict:
    """{year: [(yday, tmax, tmin, prcp, srad, dayl), ...] in yday order}."""
    by_year = {}
    for year, yday, tmax, tmin, prcp, srad, dayl in zip(
        daily["year"], daily["yday"], daily["tmax"], daily["tmin"],
        daily["prcp"], daily["srad"], daily["dayl"],
    ):
        by_year.setdefault(year, []).append((yday, tmax, tmin, prcp, srad, dayl))
    for rows in by_year.values():
        rows.sort(key=lambda r: r[0])
    return by_year


def derive_climate(daily: dict, gdd_base_f: float = GDD_BASE_F, frost_threshold_c: float = FROST_THRESHOLD_C) -> dict:
    """
    The climate block:

        {
          'years': [...], 'year_count': int, 'period': {'start', 'end'},
          'gdd_base_f': 50.0, 'frost_threshold_c': 0.0,
          'monthly': [ {'month': 1..12,
                        'tmax_mean_c', 'tmin_mean_c',
                        'prcp_total_mm',        # mean of monthly totals
                        'solar_kwh_m2_day',     # mean of daily srad*dayl/3.6e6
                        'gdd_f',                # mean of monthly sums
                        'day_count'}, ... ],    # rows contributing, all years
          'annual': {'prcp_total_mm', 'gdd_f',   # means of annual totals
                     'prcp_totals_by_year': {year: mm},
                     'gdd_by_year': {year: gdd},
                     'wettest_month': 1..12},
          'frost': {
              'per_year': [ {'year', 'last_spring': date|None,
                             'first_fall': date|None,
                             'frost_free_days': int|None}, ... ],
              'last_spring': {'month','day','reference_yday'} | None,
              'first_fall':  {'month','day','reference_yday'} | None,
              'frost_free_days': int|None,           # MEDIAN OF PER-YEAR GAPS
              'frost_free_days_from_medians': int|None,  # diagnostic: the
                                                         # other, wrong number
              'years_with_spring_frost': int, 'years_with_fall_frost': int,
              'years_with_both': int,
              'years_without_spring_frost': [...],
              'years_without_fall_frost': [...],
          },
          'hardiness': {'annual_min_c_by_year': {year: c},
                        'mean_annual_min_c', 'mean_annual_min_f',
                        'zone': '6a'},
        }

    Values are unrounded floats; the formatter rounds.
    """
    by_year = _rows_by_year(daily)
    years = sorted(by_year)
    if not years:
        raise ValueError("derive_climate: no daily rows")

    # --- monthly accumulators, per (year, month) so totals are per year ---
    tmax_sum = [0.0] * 13
    tmin_sum = [0.0] * 13
    solar_sum = [0.0] * 13
    day_count = [0] * 13
    prcp_month_totals = {m: [] for m in range(1, 13)}   # per-year totals
    gdd_month_totals = {m: [] for m in range(1, 13)}
    prcp_year_total = {}
    gdd_year_total = {}
    annual_min_c = {}
    frost_per_year = []

    for year in years:
        rows = by_year[year]
        month_prcp = [0.0] * 13
        month_gdd = [0.0] * 13
        year_min = None
        last_spring = None
        first_fall = None
        midyear = date(year, MIDYEAR_MONTH, MIDYEAR_DAY)
        for yday, tmax, tmin, prcp, srad, dayl in rows:
            d = daymet_date(year, yday)
            m = d.month
            tmax_sum[m] += tmax
            tmin_sum[m] += tmin
            solar_sum[m] += daily_solar_kwh_m2(srad, dayl)
            day_count[m] += 1
            month_prcp[m] += prcp
            month_gdd[m] += daily_gdd_f(tmax, tmin, gdd_base_f)
            year_min = tmin if year_min is None else min(year_min, tmin)
            if tmin <= frost_threshold_c:
                if d < midyear:
                    last_spring = d          # keeps overwriting: the LAST one
                elif first_fall is None:
                    first_fall = d           # set once: the FIRST one
        for m in range(1, 13):
            prcp_month_totals[m].append(month_prcp[m])
            gdd_month_totals[m].append(month_gdd[m])
        prcp_year_total[year] = sum(month_prcp[1:])
        gdd_year_total[year] = sum(month_gdd[1:])
        annual_min_c[year] = year_min
        gap = (first_fall - last_spring).days if (last_spring and first_fall) else None
        frost_per_year.append(
            {"year": year, "last_spring": last_spring, "first_fall": first_fall, "frost_free_days": gap}
        )

    monthly = []
    for m in range(1, 13):
        n = day_count[m]
        monthly.append(
            {
                "month": m,
                "tmax_mean_c": tmax_sum[m] / n if n else None,
                "tmin_mean_c": tmin_sum[m] / n if n else None,
                "prcp_total_mm": statistics.fmean(prcp_month_totals[m]),
                "solar_kwh_m2_day": solar_sum[m] / n if n else None,
                "gdd_f": statistics.fmean(gdd_month_totals[m]),
                "day_count": n,
            }
        )

    # --- frost medians ---
    spring_ref = [
        _reference_yday(e["year"], (e["last_spring"] - date(e["year"], 1, 1)).days + 1)
        for e in frost_per_year if e["last_spring"] is not None
    ]
    fall_ref = [
        _reference_yday(e["year"], (e["first_fall"] - date(e["year"], 1, 1)).days + 1)
        for e in frost_per_year if e["first_fall"] is not None
    ]
    gaps = [e["frost_free_days"] for e in frost_per_year if e["frost_free_days"] is not None]
    last_spring = _median_reference_date(spring_ref)
    first_fall = _median_reference_date(fall_ref)
    frost_free_days = _round_half_up(statistics.median(gaps)) if gaps else None
    from_medians = (
        first_fall["reference_yday"] - last_spring["reference_yday"]
        if last_spring and first_fall else None
    )

    mean_min_c = statistics.fmean(annual_min_c.values())
    mean_min_f = celsius_to_fahrenheit(mean_min_c)

    annual_prcp = statistics.fmean(prcp_year_total.values())
    annual_gdd = statistics.fmean(gdd_year_total.values())
    wettest = max(monthly, key=lambda row: row["prcp_total_mm"])["month"]

    return {
        "years": years,
        "year_count": len(years),
        "period": {"start": years[0], "end": years[-1]},
        "gdd_base_f": gdd_base_f,
        "frost_threshold_c": frost_threshold_c,
        "monthly": monthly,
        "annual": {
            "prcp_total_mm": annual_prcp,
            "gdd_f": annual_gdd,
            "prcp_totals_by_year": prcp_year_total,
            "gdd_by_year": gdd_year_total,
            "wettest_month": wettest,
        },
        "frost": {
            "per_year": frost_per_year,
            "last_spring": last_spring,
            "first_fall": first_fall,
            "frost_free_days": frost_free_days,
            "frost_free_days_from_medians": from_medians,
            "years_with_spring_frost": len(spring_ref),
            "years_with_fall_frost": len(fall_ref),
            "years_with_both": len(gaps),
            "years_without_spring_frost": [e["year"] for e in frost_per_year if e["last_spring"] is None],
            "years_without_fall_frost": [e["year"] for e in frost_per_year if e["first_fall"] is None],
        },
        "hardiness": {
            "annual_min_c_by_year": annual_min_c,
            "mean_annual_min_c": mean_min_c,
            "mean_annual_min_f": mean_min_f,
            "zone": hardiness_zone_from_min_f(mean_min_f),
        },
    }
