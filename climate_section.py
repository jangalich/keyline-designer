"""
climate_section.py

THE CLIMATE SECTION'S CONTENT, formatted for the page -- the one place the
climate block (climate_report.derive_climate(), metric) becomes the
imperial, rounded, worded values the template sets.

    build_climate_section(report_data, number) -> the section dict

THE FORMATTING BOUNDARY. Everything upstream is metric and unrounded; every
string this module returns is final. The template composes and styles and
does no arithmetic; this module rounds and converts and does no styling. A
value that reaches the template is either prose or a measurement, and the
two are told apart by shape: a measurement is {"value": ...} and the
component wraps it in the data face. The summary and footer are built as
LISTS OF PARTS for that reason -- a sentence assembled here as one string
would put a number into prose with nothing marking it as measured.

WHAT IS SAID, AND FROM WHAT:

  Summary    "The frost-free season runs about {days} days, from {early/
             mid/late Month} to {early/mid/late Month}. Precipitation
             peaks in {Month}." The month-part is read off the median
             frost date: day 1-10 early, 11-20 mid, 21 on late. Only the
             day count is set as data; a month name is a word.
  Key figures  last spring frost, first fall frost, frost-free days,
             inches of precipitation per year, growing degree days base
             50 F, estimated hardiness zone.
  Table      mean high F, mean low F, precipitation in, GDD base 50 F,
             solar kWh/m2/day, by month. Temperatures and GDD to the
             whole number, precipitation and solar to one decimal --
             the brief's own example row by row.
  Footer     THE CAVEAT FIRST -- 1 km interpolation between stations is
             the local climate rather than the parcel's microclimate;
             cold air pools; simple-average GDD; the zone is an estimate,
             not the USDA map -- then, as its own smaller line, the
             version served, the period, and the citation AS FETCHED
             (never typed here) with the commas Daymet's CSV header
             swapped for semicolons put back (display_citation).

WHEN A MEDIAN IS MISSING. derive_climate() gives None for a frost median
when no year had a frost on that side of July 1. The key figure then reads
"none recorded" and the summary line drops the frost clause rather than
inventing a date -- and the footer says how many years the medians stand
on only when it is fewer than all of them.
"""

import re
from datetime import date

from climate_report import celsius_to_fahrenheit
from report_outline import section_number

MM_PER_INCH = 25.4

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
MONTH_ABBREVIATIONS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTH_INITIALS = ("J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D")

SECTION_NAME = "Climate"
SECTION_TEMPLATE = "climate.html"

_VERSION_RE = re.compile(r"Version\s+\d+(?:\s+R\d+)?", re.IGNORECASE)


def month_part(entry) -> str:
    """'late April' from a {month, day} median: day 1-10 early, 11-20 mid,
    21 on late."""
    day = entry["day"]
    part = "early" if day <= 10 else "mid" if day <= 20 else "late"
    return f"{part} {MONTH_NAMES[entry['month'] - 1]}"


def short_date(entry) -> str:
    """'Apr 27' from a {month, day} median."""
    return f"{MONTH_ABBREVIATIONS[entry['month'] - 1]} {entry['day']}"


def _whole(value: float) -> str:
    return f"{value:,.0f}"


def _one_decimal(value: float) -> str:
    return f"{value:,.1f}"


def _f(celsius: float) -> float:
    return celsius_to_fahrenheit(celsius)


def _inches(mm: float) -> float:
    return mm / MM_PER_INCH


def display_citation(citation: str) -> str:
    """The citation as Daymet's CSV header carries it, with its commas
    restored. The service swaps every comma for a semicolon so the line
    survives inside a CSV ("Thornton; M.M.; R. Shrestha; ..."); rendered
    as-is that reads as broken. The parsed dict keeps the served line
    verbatim (daymet_data); this is display only."""
    return citation.replace(";", ",")


def daymet_version_label(daily: dict) -> str:
    """'Version 4 R1' as the citation names it; the software version
    ('4.0') if the citation does not carry one."""
    match = _VERSION_RE.search(daily.get("citation") or "")
    if match:
        return f"Daymet {match.group(0)}"
    version = daily.get("software_version")
    return f"Daymet Version {version}" if version else "Daymet"


def build_summary(climate: dict) -> list:
    frost = climate["frost"]
    wettest = MONTH_NAMES[climate["annual"]["wettest_month"] - 1]
    parts = []
    # ONLY THE COUNT IS A MEASUREMENT. "late April" and "June" are words
    # derived from measurements, and words are prose; setting them in the
    # data face would mark a month name as a number.
    if frost["frost_free_days"] is not None and frost["last_spring"] and frost["first_fall"]:
        parts += [
            "The frost-free season runs about ",
            {"value": _whole(frost["frost_free_days"])},
            f" days, from {month_part(frost['last_spring'])} to {month_part(frost['first_fall'])}. ",
        ]
    parts += [f"Precipitation peaks in {wettest}."]
    return parts


def build_key_figures(climate: dict) -> list:
    frost = climate["frost"]
    annual = climate["annual"]
    return [
        {
            "value": short_date(frost["last_spring"]) if frost["last_spring"] else "none recorded",
            "label": "median last spring frost",
        },
        {
            "value": short_date(frost["first_fall"]) if frost["first_fall"] else "none recorded",
            "label": "median first fall frost",
        },
        {
            "value": _whole(frost["frost_free_days"]) if frost["frost_free_days"] is not None else "—",
            "label": "frost-free days",
        },
        {"value": _one_decimal(_inches(annual["prcp_total_mm"])), "label": "inches precipitation per year"},
        {"value": _whole(annual["gdd_f"]), "label": "growing degree days, base 50°F"},
        {"value": climate["hardiness"]["zone"], "label": "est. hardiness zone"},
    ]


def build_table(climate: dict) -> dict:
    monthly = climate["monthly"]
    return {
        "corner": "",
        "columns": list(MONTH_INITIALS),
        "rows": [
            {"label": "Mean high °F", "cells": [_whole(_f(m["tmax_mean_c"])) for m in monthly]},
            {"label": "Mean low °F", "cells": [_whole(_f(m["tmin_mean_c"])) for m in monthly]},
            {"label": "Precipitation, in", "cells": [_one_decimal(_inches(m["prcp_total_mm"])) for m in monthly]},
            {"label": "GDD, base 50°F", "cells": [_whole(m["gdd_f"]) for m in monthly]},
            {"label": "Solar, kWh/m²/day", "cells": [_one_decimal(m["solar_kwh_m2_day"]) for m in monthly]},
        ],
    }


def build_footer(daily: dict, climate: dict) -> dict:
    """
    {caveat: parts, citation: parts} -- THE CAVEAT FIRST, then the formal
    citation as its own smaller line.

    The caveat is the valuable half: what the numbers are and are not.
    The citation is the record: the version served, the period, and the
    reference as Daymet asks it to be cited (display_citation). Nothing
    in either is a measurement, so neither carries a data part -- a
    product name and a year range are names, not figures -- and the
    part lists exist so a later section can mark one when it has one.
    """
    frost = climate["frost"]
    years = climate["year_count"]
    caveat = [
        "Daymet interpolates between weather stations on a 1 km grid; it describes the local "
        "climate, not the parcel's microclimate. Low ground and valley floors typically frost "
        "later in spring and earlier in fall than these dates. Growing degree days by the "
        "simple-average method. Hardiness zone is estimated from the same data and is not the "
        "official USDA map."
    ]
    for name, count in (
        ("spring", frost["years_with_spring_frost"]),
        ("fall", frost["years_with_fall_frost"]),
    ):
        if count < years:
            caveat.append(
                f" The median {name} frost stands on {count} of the {years} years; the rest "
                f"recorded no {name} frost."
            )
    citation = [
        f"Source: {daymet_version_label(daily)}, {years}-year means "
        f"{climate['period']['start']}–{climate['period']['end']}. {display_citation(daily['citation'])}"
    ]
    return {"caveat": caveat, "citation": citation}


def build_climate_section(report_data) -> dict:
    """
    The section dict `templates/report/sections/climate.html` renders:
    {number, name, template, heading, summary, key_figures, table, footer}.
    `number` is the section's Roman numeral in the fixed outline
    (report_outline.section_number) -- II, whatever else renders.
    """
    climate = report_data.climate
    daily = report_data.daymet_daily
    if climate is None or daily is None:
        raise ValueError("build_climate_section: the report data carries no climate block")
    return {
        "number": section_number(SECTION_NAME),
        "name": SECTION_NAME,
        "template": SECTION_TEMPLATE,
        "heading": SECTION_NAME,
        "summary": build_summary(climate),
        "key_figures": build_key_figures(climate),
        "table": build_table(climate),
        "footer": build_footer(daily, climate),
    }


def format_generated_on(when: date) -> str:
    """'21 September 2026' -- the cover's and the running footer's date."""
    return f"{when.day} {MONTH_NAMES[when.month - 1]} {when.year}"
