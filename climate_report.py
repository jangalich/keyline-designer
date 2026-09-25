"""
climate_report.py

The site data report's CLIMATE NUMBERS, derived from Daymet daily arrays
(daymet_data.parse_daymet_csv()'s dict). Pure functions, no network, no
numpy -- ten thousand rows of arithmetic.

    derive_climate(daily)  -> the climate block (see the return contract)

EVERY FIGURE IS DETERMINISTIC AND METRIC. The block stores degC, mm, kWh
and degree-days exactly as computed; converting to degF and inches is the
formatting layer's job (the retired narrated report's rule, kept). The one
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

BRANCH 7 ADDED THE WATER FIGURES, each stated the same way:

  Precipitation factor        `prcp_factor` (default 1.0) multiplies EVERY
                              daily prcp before anything is summed, so the
                              monthly and annual totals, the driest and
                              wettest year, the heavy-rain days and the
                              largest day all carry the same correction
                              (precipitation_normals.py says where it comes
                              from). The factor is on the block.
  Potential evapotranspiration  THORNTHWAITE (1948), monthly, from the
                              CLIMATOLOGICAL monthly mean temperature (mean
                              of daily (tmax+tmin)/2 over every day of that
                              month, all years), the heat index I summed
                              over the twelve monthly means, and the month's
                              mean day length from `dayl` and mean day count:
                                PET = 16 (10 T / I)^a x (L / 12) x (N / 30)
                              with T in degC (0 when T <= 0), L in hours, N
                              in days; a the cubic in I. Above 26.5 degC the
                              Willmott et al. (1985) polynomial replaces the
                              power term. Chosen in branch 7 step 0 over
                              Hargreaves-Samani, which ran 55% above the
                              published Thornthwaite figure for Pittsburgh
                              (Penn State soil-climate atlas, 671 mm on
                              1961-1990 normals; this method gives 666 mm on
                              the fixture) and 44% above FAO-56
                              Penman-Monteith from NASA POWER (716 mm). It is
                              the PET behind the classic bulletin water
                              balance, and it uses only figures the required
                              source already carries.
  Water balance               precipitation minus PET, by month, in mm. A
                              CLIMATIC balance: no soil storage term, no
                              runoff, no snow. It says which months run a
                              surplus and which a deficit, and how big; it
                              is NOT a soil water balance and must never be
                              labelled as one.
  Heavy-rain days             days with prcp >= 25.4 mm (1.00 in, at or
                              above), counted per (year, month), mean over
                              years; the annual figure is the mean of annual
                              counts. Also the largest single-day total in
                              the record with its date.
  Day length                  mean of dayl / 3600 over the month, all years;
                              the longest and shortest day as the mean over
                              years of each year's maximum and minimum, with
                              the calendar date that maximum or minimum most
                              often falls on.
  Driest and wettest year     the annual totals already computed, ranked.

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
# A heavy-rain day: at least one inch. The comparison is >=, so a day of
# exactly 25.4 mm counts (test_climate_report.py holds this boundary).
HEAVY_RAIN_MM = 25.4
SECONDS_PER_HOUR = 3600.0
# Thornthwaite's formula changes form above this monthly mean (Willmott
# et al. 1985); below it the power law applies.
THORNTHWAITE_HIGH_TEMP_C = 26.5
PET_METHOD = "Thornthwaite (1948), monthly, on climatological monthly means"
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


def is_heavy_rain_day(prcp_mm: float, threshold_mm: float = HEAVY_RAIN_MM) -> bool:
    """At or above the threshold: a day of exactly 25.4 mm is a heavy day."""
    return prcp_mm >= threshold_mm


def thornthwaite_heat_index(monthly_mean_c) -> float:
    """I = sum over the twelve months of (T/5)^1.514, months at or below
    0 degC contributing nothing."""
    return sum((max(t, 0.0) / 5.0) ** 1.514 for t in monthly_mean_c)


def thornthwaite_exponent(heat_index: float) -> float:
    """a = 6.75e-7 I^3 - 7.71e-5 I^2 + 1.792e-2 I + 0.49239."""
    i = heat_index
    return 6.75e-7 * i**3 - 7.71e-5 * i**2 + 1.792e-2 * i + 0.49239


def thornthwaite_monthly_pet_mm(
    mean_c: float, heat_index: float, exponent: float, day_length_h: float, days_in_month: float
) -> float:
    """
    One month's potential evapotranspiration in mm: the unadjusted
    12-hour, 30-day figure -- 16 (10 T / I)^a, or Willmott's polynomial
    above 26.5 degC -- scaled by day length / 12 and days / 30. Zero at
    or below 0 degC, and zero when the heat index is zero (a place with
    no month above freezing).
    """
    if mean_c <= 0.0 or heat_index <= 0.0:
        return 0.0
    if mean_c > THORNTHWAITE_HIGH_TEMP_C:
        unadjusted = -415.85 + 32.24 * mean_c - 0.43 * mean_c**2
    else:
        unadjusted = 16.0 * (10.0 * mean_c / heat_index) ** exponent
    return max(unadjusted, 0.0) * (day_length_h / 12.0) * (days_in_month / 30.0)


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


def derive_climate(
    daily: dict,
    gdd_base_f: float = GDD_BASE_F,
    frost_threshold_c: float = FROST_THRESHOLD_C,
    prcp_factor: float = 1.0,
) -> dict:
    """
    The climate block:

        {
          'years': [...], 'year_count': int, 'period': {'start', 'end'},
          'gdd_base_f': 50.0, 'frost_threshold_c': 0.0,
          'prcp_factor': 1.0,                    # applied to every daily prcp
          'monthly': [ {'month': 1..12,
                        'tmax_mean_c', 'tmin_mean_c',
                        'tmean_c',              # mean of daily (tmax+tmin)/2
                        'prcp_total_mm',        # mean of monthly totals (scaled)
                        'pet_mm',               # Thornthwaite, this month
                        'balance_mm',           # prcp_total_mm - pet_mm
                        'heavy_rain_days',      # mean per year of days >= 25.4 mm
                        'day_length_h',         # mean dayl / 3600
                        'solar_kwh_m2_day',     # mean of daily srad*dayl/3.6e6
                        'gdd_f',                # mean of monthly sums
                        'day_count'}, ... ],    # rows contributing, all years
          'annual': {'prcp_total_mm', 'gdd_f',   # means of annual totals
                     'pet_mm',                   # sum of monthly PET
                     'balance_mm',               # prcp_total_mm - pet_mm
                     'deficit_mm',               # sum of the negative monthly balances
                     'deficit_months': [m, ...], 'surplus_months': [m, ...],
                     'heavy_rain_days',          # mean of annual counts
                     'heavy_rain_days_by_year': {year: n},
                     'prcp_totals_by_year': {year: mm},
                     'gdd_by_year': {year: gdd},
                     'driest_year': {'year', 'prcp_total_mm'},
                     'wettest_year': {'year', 'prcp_total_mm'},
                     'largest_day': {'date': date, 'prcp_mm'},
                     'wettest_month': 1..12},
          'pet': {'method', 'heat_index', 'exponent',
                  'monthly_mean_c': [12], 'day_length_h': [12], 'days': [12]},
          'day_length': {'longest': {'hours', 'month', 'day'},
                         'shortest': {'hours', 'month', 'day'}},
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
    dayl_sum = [0.0] * 13
    day_count = [0] * 13
    prcp_month_totals = {m: [] for m in range(1, 13)}   # per-year totals
    gdd_month_totals = {m: [] for m in range(1, 13)}
    heavy_month_counts = {m: [] for m in range(1, 13)}  # per-year counts
    prcp_year_total = {}
    gdd_year_total = {}
    heavy_year_count = {}
    annual_min_c = {}
    frost_per_year = []
    largest_day = None                                  # (prcp, date)
    longest_days = []                                   # per year (hours, date)
    shortest_days = []

    for year in years:
        rows = by_year[year]
        month_prcp = [0.0] * 13
        month_gdd = [0.0] * 13
        month_heavy = [0] * 13
        year_min = None
        last_spring = None
        first_fall = None
        year_longest = None
        year_shortest = None
        midyear = date(year, MIDYEAR_MONTH, MIDYEAR_DAY)
        for yday, tmax, tmin, prcp, srad, dayl in rows:
            d = daymet_date(year, yday)
            m = d.month
            prcp = prcp * prcp_factor
            tmax_sum[m] += tmax
            tmin_sum[m] += tmin
            solar_sum[m] += daily_solar_kwh_m2(srad, dayl)
            dayl_sum[m] += dayl
            day_count[m] += 1
            month_prcp[m] += prcp
            month_gdd[m] += daily_gdd_f(tmax, tmin, gdd_base_f)
            if is_heavy_rain_day(prcp):
                month_heavy[m] += 1
            if largest_day is None or prcp > largest_day[0]:
                largest_day = (prcp, d)
            if year_longest is None or dayl > year_longest[0]:
                year_longest = (dayl, d)
            if year_shortest is None or dayl < year_shortest[0]:
                year_shortest = (dayl, d)
            year_min = tmin if year_min is None else min(year_min, tmin)
            if tmin <= frost_threshold_c:
                if d < midyear:
                    last_spring = d          # keeps overwriting: the LAST one
                elif first_fall is None:
                    first_fall = d           # set once: the FIRST one
        for m in range(1, 13):
            prcp_month_totals[m].append(month_prcp[m])
            gdd_month_totals[m].append(month_gdd[m])
            heavy_month_counts[m].append(month_heavy[m])
        prcp_year_total[year] = sum(month_prcp[1:])
        gdd_year_total[year] = sum(month_gdd[1:])
        heavy_year_count[year] = sum(month_heavy[1:])
        annual_min_c[year] = year_min
        longest_days.append(year_longest)
        shortest_days.append(year_shortest)
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
                "tmean_c": (tmax_sum[m] + tmin_sum[m]) / (2 * n) if n else None,
                "prcp_total_mm": statistics.fmean(prcp_month_totals[m]),
                "heavy_rain_days": statistics.fmean(heavy_month_counts[m]),
                "day_length_h": dayl_sum[m] / n / SECONDS_PER_HOUR if n else None,
                "solar_kwh_m2_day": solar_sum[m] / n if n else None,
                "gdd_f": statistics.fmean(gdd_month_totals[m]),
                "day_count": n,
            }
        )

    # --- Thornthwaite PET and the climatic water balance ---
    monthly_means = [row["tmean_c"] if row["tmean_c"] is not None else 0.0 for row in monthly]
    heat_index = thornthwaite_heat_index(monthly_means)
    exponent = thornthwaite_exponent(heat_index)
    pet_days = [row["day_count"] / len(years) for row in monthly]
    pet_day_length = [row["day_length_h"] if row["day_length_h"] is not None else 0.0 for row in monthly]
    for row, t, L, n in zip(monthly, monthly_means, pet_day_length, pet_days):
        row["pet_mm"] = thornthwaite_monthly_pet_mm(t, heat_index, exponent, L, n)
        row["balance_mm"] = row["prcp_total_mm"] - row["pet_mm"]
    annual_pet = sum(row["pet_mm"] for row in monthly)
    deficit_months = [row["month"] for row in monthly if row["balance_mm"] < 0]
    surplus_months = [row["month"] for row in monthly if row["balance_mm"] >= 0]
    deficit_mm = -sum(row["balance_mm"] for row in monthly if row["balance_mm"] < 0)

    # --- day length extremes: mean hours, and the date most years hit them ---
    def _extreme(entries):
        hours = statistics.fmean(dayl for dayl, _ in entries) / SECONDS_PER_HOUR
        dates = [(d.month, d.day) for _, d in entries]
        month, day = max(set(dates), key=lambda md: (dates.count(md), -md[0], -md[1]))
        return {"hours": hours, "month": month, "day": day}

    day_length = {"longest": _extreme(longest_days), "shortest": _extreme(shortest_days)}

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
    driest_year = min(prcp_year_total, key=lambda y: (prcp_year_total[y], y))
    wettest_year = max(prcp_year_total, key=lambda y: (prcp_year_total[y], -y))

    return {
        "years": years,
        "year_count": len(years),
        "period": {"start": years[0], "end": years[-1]},
        "gdd_base_f": gdd_base_f,
        "frost_threshold_c": frost_threshold_c,
        "prcp_factor": prcp_factor,
        "monthly": monthly,
        "annual": {
            "prcp_total_mm": annual_prcp,
            "gdd_f": annual_gdd,
            "pet_mm": annual_pet,
            "balance_mm": annual_prcp - annual_pet,
            "deficit_mm": deficit_mm,
            "deficit_months": deficit_months,
            "surplus_months": surplus_months,
            "heavy_rain_days": statistics.fmean(heavy_year_count.values()),
            "heavy_rain_days_by_year": heavy_year_count,
            "prcp_totals_by_year": prcp_year_total,
            "gdd_by_year": gdd_year_total,
            "driest_year": {"year": driest_year, "prcp_total_mm": prcp_year_total[driest_year]},
            "wettest_year": {"year": wettest_year, "prcp_total_mm": prcp_year_total[wettest_year]},
            "largest_day": {"date": largest_day[1], "prcp_mm": largest_day[0]},
            "wettest_month": wettest,
        },
        "pet": {
            "method": PET_METHOD,
            "heat_index": heat_index,
            "exponent": exponent,
            "monthly_mean_c": monthly_means,
            "day_length_h": pet_day_length,
            "days": pet_days,
        },
        "day_length": day_length,
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
