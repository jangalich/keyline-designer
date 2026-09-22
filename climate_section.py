"""
climate_section.py

THE CLIMATE SECTION'S CONTENT, formatted for the page -- the one place the
climate block (climate_report.derive_climate(), metric), the design
storms, the wind block and the severe-weather block become the imperial,
rounded, worded values the template sets.

    build_climate_section(report_data) -> the section dict

THE FORMATTING BOUNDARY. Everything upstream is metric and unrounded; every
string this module returns is final. The template composes and styles and
does no arithmetic; this module rounds and converts and does no styling. A
value that reaches the template is either prose or a measurement, and the
two are told apart by shape: a measurement is {"value": ...} and the
component wraps it in the data face. The summary and the captions are
built as LISTS OF PARTS for that reason.

TWO PAGES, BY THE RHYTHM THE MAP SECTIONS SET (branch 7 phase 2). Page
one is the pictures and the takeaway: eyebrow, heading, summary line,
the water balance diagram, the seasonal wind roses. Page two is the
numbers: nine key figures, the monthly table, design storms, severe
weather, the sources. The split is the template's; this module hands
over both halves.

DISPLAY PRECISION (the author's phase 2 rule). Temperatures and GDD to
the whole number. Precipitation, evaporation, balance, heavy-rain days
and day length to ONE decimal -- phase 1's two were for verification,
not print. A true zero in a one-decimal row is set as a dash, the
Landform rule (landform_section.ZERO_DASH): January's evaporation reads
"–", not "0.0". A deficit in the balance row carries a TRUE MINUS SIGN
(U+2212), not a hyphen. Design-storm depths keep the two decimals Atlas
14 serves: a design figure at the source's own precision.

ONE CAVEAT PER FIGURE, AT THE POINT OF USE. With five sources in one
section the one-caveat rule applies per figure, not per section: each
chart or table carries at most one caption line with the caveat that
could change a decision. The section footer then lists each source on
one line with its identifier and period. Full citations and method
notes -- Hargreaves rejected for Thornthwaite, the Atlas 14 series, the
wind averaging, the precipitation correction -- are recorded under
`methods`, a structure the methods note at the back of the report (built
with Site overview) will read; nothing renders it yet.

WHEN A DEGRADABLE LAYER IS MISSING the figure's place carries a visible
statement, and the statement distinguishes the two causes report_data
records: a source that did not answer (retry later; here is where to
look it up) from a point the source does not cover. Atlas 14's "not
covered" case is real -- Washington, Oregon, Idaho, Montana and Wyoming
have no Atlas 14 volume -- and the statement says so plainly and points
at NOAA Atlas 15, due to cover the whole country.

HEAVY-RAIN DAYS ARE THE STATIONS' (phase 2 decision). The monthly table's
"Days over 1 in" row is the median of the nearest stations' NCEI monthly
normals (precipitation_normals.heavy_rain_normals), because Daymet's
interpolation undercounts heavy days; when fewer than three stations
carry the normal, Daymet's own count is used and the table caption says
so.
"""

import re
from datetime import date

import report_chart
from climate_report import celsius_to_fahrenheit
from landform_section import ZERO_DASH
from report_outline import section_number

MM_PER_INCH = 25.4
MPH_PER_M_S = 2.23694
MINUS = "−"
EN_DASH = "–"

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
MONTH_ABBREVIATIONS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTH_INITIALS = ("J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D")

SECTION_NAME = "Climate"
SECTION_TEMPLATE = "climate.html"

SEASON_TITLES = {"winter": ("Winter", "Dec–Feb"), "summer": ("Summer", "Jun–Aug")}
STORM_DURATION_LABELS = {"60-min": "1-hour, in", "24-hr": "24-hour, in"}
SEVERE_TYPE_LABELS = {"hail": "Hail", "wind": "Damaging wind", "tornado": "Tornado"}

PFDS_URL = "hdsc.nws.noaa.gov/pfds"

_VERSION_RE = re.compile(r"Version\s+\d+(?:\s+R\d+)?", re.IGNORECASE)


# ======================================================================
# Formatting helpers
# ======================================================================


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


def _one_decimal_or_dash(value: float) -> str:
    """The Landform rule: a value that rounds to zero at one decimal is a
    dash, so the row reads 'none' rather than a measurement of nothing."""
    return ZERO_DASH if round(value, 1) == 0 else _one_decimal(value)


def _signed_one_decimal(value: float) -> str:
    """One decimal with a TRUE MINUS SIGN for a negative; a value that
    rounds to zero is a dash, neither signed nor measured."""
    rounded = round(value, 1)
    if rounded == 0:
        return ZERO_DASH
    text = _one_decimal(abs(rounded))
    return MINUS + text if rounded < 0 else text


def _f(celsius: float) -> float:
    return celsius_to_fahrenheit(celsius)


def _inches(mm: float) -> float:
    return mm / MM_PER_INCH


def display_citation(citation: str) -> str:
    """The citation as Daymet's CSV header carries it, with its commas
    restored. The service swaps every comma for a semicolon so the line
    survives inside a CSV; rendered as-is that reads as broken."""
    return citation.replace(";", ",")


def daymet_version_label(daily: dict) -> str:
    """'Daymet Version 4 R1' as the citation names it; the software
    version ('4.0') if the citation does not carry one."""
    match = _VERSION_RE.search(daily.get("citation") or "")
    if match:
        return f"Daymet {match.group(0)}"
    version = daily.get("software_version")
    return f"Daymet Version {version}" if version else "Daymet"


def month_run(months: list) -> str:
    """'June through August' for a contiguous run (wrapping the year:
    [10, 11, 12, 1, 2] is 'October through February'); 'June, July and
    September' when the months are not contiguous; '' for none."""
    if not months:
        return ""
    names = [MONTH_NAMES[m - 1] for m in months]
    if len(months) == 1:
        return names[0]
    if len(months) == 12:
        return "every month"
    ordered = sorted(months)
    # Find the start of the run: the month whose predecessor is absent.
    present = set(ordered)
    starts = [m for m in ordered if ((m - 2) % 12) + 1 not in present]
    if len(starts) == 1:
        start = starts[0]
        run = [((start - 1 + k) % 12) + 1 for k in range(len(months))]
        if set(run) == present:
            return f"{MONTH_NAMES[run[0] - 1]} through {MONTH_NAMES[run[-1] - 1]}"
    return ", ".join(names[:-1]) + " and " + names[-1]


# ======================================================================
# Page one: summary, water balance, wind roses
# ======================================================================


def build_summary(climate: dict) -> list:
    frost = climate["frost"]
    annual = climate["annual"]
    parts = []
    # ONLY THE COUNTS ARE MEASUREMENTS. "late April" and "June through
    # August" are words derived from measurements, and words are prose.
    if frost["frost_free_days"] is not None and frost["last_spring"] and frost["first_fall"]:
        parts += [
            "The frost-free season runs about ",
            {"value": _whole(frost["frost_free_days"])},
            f" days, from {month_part(frost['last_spring'])} to {month_part(frost['first_fall'])}. ",
        ]
    deficit, surplus = annual["deficit_months"], annual["surplus_months"]
    if not deficit:
        parts += ["Rainfall exceeds evaporation in every month."]
    elif not surplus:
        parts += ["Evaporation exceeds rainfall in every month; the year runs a deficit of about ",
                  {"value": _one_decimal(_inches(annual["deficit_mm"]))}, " in."]
    else:
        parts += [
            f"Rainfall exceeds evaporation {'from ' if len(surplus) > 1 else 'in '}{month_run(surplus)}; "
            f"{month_run(deficit)} {'run' if len(deficit) > 1 else 'runs'} a deficit of about ",
            {"value": _one_decimal(_inches(annual["deficit_mm"]))},
            " in.",
        ]
    return parts


def build_water_balance(climate: dict, tokens: dict) -> dict:
    monthly = climate["monthly"]
    chart = report_chart.render_water_balance(
        list(MONTH_INITIALS),
        [_inches(m["prcp_total_mm"]) for m in monthly],
        [_inches(m["pet_mm"]) for m in monthly],
        "in",
        tokens,
    )
    return {
        "chart": chart,
        "caption": ["Potential evaporation is estimated from temperature; actual loss depends on cover and soil."],
    }


def build_wind_roses(wind, unavailable: dict, tokens: dict) -> dict:
    if wind is None:
        record = unavailable.get("power_wind") or {}
        cause = record.get("error", "no answer")
        return {
            "chart": None,
            "unavailable": [
                "Wind roses are unavailable: NASA POWER, the regional wind record, did not answer when this "
                f"report was generated ({cause}). A later report on the same boundary will carry them."
            ],
            "caption": [],
        }
    seasons = []
    for name, (title, months) in SEASON_TITLES.items():
        season = wind["seasons"][name]
        speed = season["mean_speed_m_s"]
        seasons.append(
            {
                "title": title,
                "months": months,
                "sectors": wind["sectors"],
                "frequency": season["sector_frequency"],
                "prevailing": season["prevailing_sector"],
                "speed_label": f"{speed * MPH_PER_M_S:.1f} mph" if speed is not None else None,
            }
        )
    return {
        "chart": report_chart.render_wind_roses(seasons, tokens),
        "unavailable": None,
        "caption": ["A regional estimate from a 55 km reanalysis cell; valleys and ridges channel local wind."],
    }


# ======================================================================
# Page two: key figures, tables, sources
# ======================================================================


def build_key_figures(climate: dict) -> list:
    frost = climate["frost"]
    annual = climate["annual"]
    deficit = annual["deficit_months"]
    deficit_label = "months in deficit"
    if deficit:
        deficit_label += f", {MONTH_ABBREVIATIONS[deficit[0] - 1]}–{MONTH_ABBREVIATIONS[deficit[-1] - 1]}" if len(deficit) > 1 else f", {MONTH_ABBREVIATIONS[deficit[0] - 1]}"
    return [
        {
            "value": short_date(frost["last_spring"]) if frost["last_spring"] else "none recorded",
            "label": "median last spring frost",
            "word": frost["last_spring"] is None,
        },
        {
            "value": short_date(frost["first_fall"]) if frost["first_fall"] else "none recorded",
            "label": "median first fall frost",
            "word": frost["first_fall"] is None,
        },
        {
            "value": _whole(frost["frost_free_days"]) if frost["frost_free_days"] is not None else ZERO_DASH,
            "label": "frost-free days",
        },
        {"value": _one_decimal(_inches(annual["prcp_total_mm"])), "label": "inches precipitation per year"},
        {
            "value": _one_decimal(_inches(annual["driest_year"]["prcp_total_mm"])),
            "label": f"inches in the driest year, {annual['driest_year']['year']}",
        },
        {
            "value": _one_decimal(_inches(annual["wettest_year"]["prcp_total_mm"])),
            "label": f"inches in the wettest year, {annual['wettest_year']['year']}",
        },
        {"value": _whole(annual["gdd_f"]), "label": "growing degree days, base 50°F"},
        {"value": climate["hardiness"]["zone"], "label": "est. hardiness zone"},
        {"value": str(len(deficit)), "label": deficit_label},
    ]


def build_table(climate: dict, heavy_rain) -> dict:
    """The monthly table. `heavy_rain` is precipitation_normals.
    heavy_rain_normals()'s block, or None; the stations' monthly medians
    when applied, Daymet's own count otherwise."""
    monthly = climate["monthly"]
    if heavy_rain and heavy_rain["applied"]:
        heavy_cells = [_one_decimal_or_dash(v) for v in heavy_rain["monthly"]]
    else:
        heavy_cells = [_one_decimal_or_dash(m["heavy_rain_days"]) for m in monthly]
    return {
        "corner": "",
        "columns": list(MONTH_INITIALS),
        "rows": [
            {"label": "Mean high °F", "cells": [_whole(_f(m["tmax_mean_c"])) for m in monthly]},
            {"label": "Mean low °F", "cells": [_whole(_f(m["tmin_mean_c"])) for m in monthly]},
            {"label": "Precipitation, in", "cells": [_one_decimal(_inches(m["prcp_total_mm"])) for m in monthly]},
            {"label": "Potential evaporation, in", "cells": [_one_decimal_or_dash(_inches(m["pet_mm"])) for m in monthly]},
            {"label": "Water balance, in", "cells": [_signed_one_decimal(_inches(m["balance_mm"])) for m in monthly]},
            {"label": "Days over 1 in", "cells": heavy_cells},
            {"label": "GDD, base 50°F", "cells": [_whole(m["gdd_f"]) for m in monthly]},
            {"label": "Day length, h", "cells": [_one_decimal(m["day_length_h"]) for m in monthly]},
            {"label": "Solar, kWh/m²/day", "cells": [_one_decimal(m["solar_kwh_m2_day"]) for m in monthly]},
        ],
    }


def build_table_caption(correction, heavy_rain) -> list:
    parts = []
    if correction and correction["applied"]:
        parts += [
            "Precipitation scaled by ",
            {"value": f"{correction['factor']:.2f}"},
            f" to the {correction['station_count']} nearest NOAA station normals; ",
        ]
    else:
        parts += ["Precipitation is Daymet's, uncorrected (too few station normals nearby); "]
    if heavy_rain and heavy_rain["applied"]:
        parts += ["days over 1 in from the same stations."]
    else:
        parts += ["days over 1 in are Daymet's, which understates them."]
    return parts


def _storm_unavailable(record: dict, centroid) -> list:
    error = record.get("error", "")
    if "not within a project area" in error:
        return [
            "Design storm depths are unavailable: NOAA Atlas 14 does not cover this location. Washington, "
            "Oregon, Idaho, Montana and Wyoming have no Atlas 14 volume and remain on NOAA Atlas 2 of 1973. "
            "NOAA Atlas 15, due to cover the whole country with published estimates in 2027, will fill the gap."
        ]
    lat, lon = centroid
    return [
        "Design storm depths are unavailable: the NOAA Precipitation Frequency Data Server did not answer when "
        f"this report was generated ({error or 'no answer'}). The point estimates can be read at {PFDS_URL} for ",
        {"value": f"{lat:.4f}, {lon:.4f}"},
        ".",
    ]


def build_design_storms(atlas14, storms, unavailable: dict, centroid) -> dict:
    if storms is None:
        return {"table": None, "unavailable": _storm_unavailable(unavailable.get("atlas14") or {}, centroid), "caption": []}
    return {
        "table": {
            "corner": "Return period",
            "columns": [f"{ari}-yr" for ari in storms["aris"]],
            "rows": [
                {"label": STORM_DURATION_LABELS.get(row["duration"], row["duration"]),
                 "cells": [f"{row['depths'][ari]:.2f}" for ari in storms["aris"]]}
                for row in storms["rows"]
            ],
            "compact": True,
        },
        "unavailable": None,
        "caption": [f"Point estimates from records through {atlas14['record_ends']}; heavier recent storms are not reflected."],
    }


def build_severe_weather(severe: dict) -> dict:
    rows = []
    for kind in ("hail", "wind", "tornado"):
        label = SEVERE_TYPE_LABELS[kind]
        if kind == "hail" and severe.get("largest_hail_in") is not None:
            label = ["Hail (largest ", {"value": f"{severe['largest_hail_in']:.1f} in"}, ")"]
        peak = severe.get(f"{kind}_peak_month")
        rows.append(
            {
                "label": label,
                "cells": [_one_decimal(severe["per_year"][kind]), MONTH_ABBREVIATIONS[peak - 1] if peak else ZERO_DASH],
            }
        )
    return {
        "table": {"corner": f"Within {severe['radius_miles']:.0f} miles", "columns": ["Reports per year", "Peak month"], "rows": rows, "compact": True},
        "caption": ["Reports, not measurements: they cluster near roads and towns, so fewer reports does not mean fewer storms."],
    }


def build_key_figures_caption(climate: dict) -> list:
    frost = climate["frost"]
    years = climate["year_count"]
    parts = ["Low ground and valley floors typically frost later in spring and earlier in fall than these dates."]
    for name, count in (("spring", frost["years_with_spring_frost"]), ("fall", frost["years_with_fall_frost"])):
        if count < years:
            parts.append(f" The median {name} frost stands on {count} of the {years} years; the rest recorded no {name} frost.")
    return parts


def build_sources(report_data) -> list:
    """One line per source, in the order the section uses them: the
    identifier and the period. Names, not measurements."""
    climate = report_data.climate
    daily = report_data.daymet_daily
    lines = [[f"{daymet_version_label(daily)}, 1 km daily grid, {climate['year_count']}-year means "
              f"{climate['period']['start']}–{climate['period']['end']}."]]
    correction = report_data.precipitation_correction
    if correction and correction["stations"]:
        farthest = max(s["distance_miles"] for s in correction["stations"])
        lines.append([f"NCEI U.S. Climate Normals {correction['normals_period'].replace('-', EN_DASH)}, "
                      f"{correction['station_count']} stations within {farthest:.0f} miles."])
    if report_data.atlas14 is not None:
        atlas = report_data.atlas14
        lines.append([f"{atlas['source_line']} ({atlas['project_area']}), partial-duration series, "
                      f"records through {atlas['record_ends']}."])
    if report_data.wind is not None:
        wind = report_data.wind
        lines.append([f"NASA POWER (MERRA-2), 0.5° × 0.625° cell, {wind['period']['start']}–{wind['period']['end']}."])
    severe = report_data.severe_weather
    if severe is not None:
        vintage = severe.get("vintage", {}).get("hail", {}).get("last_modified", "")
        dated = f", files of {vintage[5:16].strip()}" if vintage else ""
        lines.append([f"NOAA Storm Prediction Center severe weather reports {severe['window']['start']}–{severe['window']['end']}{dated}."])
    return lines


def build_methods(report_data) -> list:
    """The methods note's inputs -- full citations and the method behind
    each figure -- for the note the Site overview branch builds. Not
    rendered by this section."""
    climate = report_data.climate
    daily = report_data.daymet_daily
    correction = report_data.precipitation_correction
    heavy = getattr(report_data, "heavy_rain_normals", None)
    methods = [
        {
            "source": "Daymet",
            "identifier": daymet_version_label(daily),
            "period": f"{climate['period']['start']}–{climate['period']['end']}",
            "citation": display_citation(daily["citation"]),
            "doi": daily.get("doi"),
            "method": climate["pet"]["method"],
            "notes": [
                "Potential evapotranspiration by Thornthwaite (1948) on climatological monthly means of daily "
                "(tmax + tmin)/2, the month's mean day length from Daymet dayl, and the mean day count; Willmott "
                "et al. (1985) above 26.5 °C. Chosen over Hargreaves–Samani, which ran 55% above the published "
                "Thornthwaite figure for Pittsburgh (Waltman et al., Soil Climate Regimes of Pennsylvania) and 44% "
                "above FAO-56 Penman–Monteith from NASA POWER.",
                "The water balance is precipitation minus potential evapotranspiration by month: a climatic "
                "balance with no soil storage, runoff or snow term.",
                "Growing degree days by the simple-average method, base 50 °F, no cap. Frost dates: last day with "
                "tmin ≤ 0 °C before July 1 and first on or after, medians over the years. Hardiness zone from the "
                "mean annual extreme minimum, an estimate rather than the USDA map.",
                "Daymet interpolates between stations on a 1 km grid and describes the local climate, not the "
                "parcel's microclimate; its daily maxima are smoothed, so heavy-rain days are taken from station "
                "normals where available.",
            ],
        }
    ]
    if correction is not None:
        methods.append(
            {
                "source": "NCEI U.S. Climate Normals",
                "identifier": f"{correction['vintage'].get('annual_archive', '')}; {correction['vintage'].get('monthly_archive', '')}",
                "period": correction["normals_period"],
                "citation": "NOAA National Centers for Environmental Information, U.S. Climate Normals 1991–2020, "
                            "annual/seasonal and monthly by-station archives, v1.0.1 (2023).",
                "method": correction["method"],
                "notes": [
                    ("Precipitation factor " + f"{correction['factor']:.3f}, the median of station normal / Daymet at the "
                     "station over: " + "; ".join(
                         f"{s['name']} ({s['distance_miles']:.1f} mi, normal {s['normal_in']:.2f} in, Daymet "
                         f"{s['daymet_mm'] / MM_PER_INCH:.2f} in, ratio {s['ratio']:.3f})" for s in correction["stations"]
                     ) + ".") if correction["applied"] else f"Precipitation not corrected: {correction['reason']}.",
                    ("Days with at least 1.00 in of precipitation: median of the same stations' monthly normals "
                     "(MLY-PRCP-AVGNDS-GE100HI)." if heavy and heavy["applied"] else
                     "Days with at least 1.00 in of precipitation: Daymet's count, which understates them."),
                    "Daymet at each station: the 1991–2020 mean annual total at the station's coordinates, "
                    f"bundled with the normals (Daymet versions {correction['vintage'].get('daymet', {}).get('versions', {})}).",
                ],
            }
        )
    if report_data.atlas14 is not None:
        atlas = report_data.atlas14
        methods.append(
            {
                "source": "NOAA Atlas 14",
                "identifier": atlas["source_line"],
                "period": f"records through {atlas['record_ends']}",
                "citation": "Bonnin, G.M., D. Martin, B. Lin, T. Parzybok, M. Yekta, and D. Riley, 2006: NOAA Atlas 14 "
                            "Volume 2 Version 3, Precipitation-Frequency Atlas of the United States, Ohio River Basin "
                            "and Surrounding States. NOAA, National Weather Service, Silver Spring, MD.",
                "method": "Partial-duration series point estimates at the parcel centroid, as served by the "
                          "Precipitation Frequency Data Server.",
                "notes": [
                    "Partial-duration rather than annual-maximum series, following NRCS National Engineering "
                    "Handbook Part 630 Chapter 4: engineering projects are subject to all storms, not only the "
                    "largest each year; the two series differ only at the 2- and 5-year return periods.",
                    "NOAA Atlas 15 is scheduled to supersede Atlas 14 with published estimates for the whole "
                    "country in 2027 and will account for trends in the record.",
                ],
            }
        )
    if report_data.wind is not None:
        wind = report_data.wind
        methods.append(
            {
                "source": "NASA POWER",
                "identifier": "NASA POWER daily meteorology, MERRA-2, WS10M and WD10M",
                "period": f"{wind['period']['start']}–{wind['period']['end']}",
                "citation": "The data was obtained from the National Aeronautics and Space Administration (NASA) "
                            "Langley Research Center (LaRC) Prediction of Worldwide Energy Resource (POWER) Project "
                            "funded through the NASA Earth Science/Applied Science Program.",
                "method": "Eight-sector frequencies by winter (Dec–Feb) and summer (Jun–Aug); direction the wind "
                          "comes from; the prevailing sector is the most frequent (modal) sector.",
                "notes": [
                    "POWER's daily direction was verified to be the speed-weighted vector mean of its hourly "
                    "directions, so a day is sectored by its served direction.",
                    "Resultant wind (speed-weighted vector mean over days, a different quantity from the "
                    "prevailing sector): " + "; ".join(
                        f"{name} from {s['resultant_sector']} ({s['resultant_degrees']:.0f}°)"
                        for name, s in wind["seasons"].items() if s["resultant_degrees"] is not None
                    ) + ".",
                    "A 0.5° × 0.625° reanalysis cell, about 55 km: regional wind, not the parcel's.",
                ],
            }
        )
    severe = report_data.severe_weather
    if severe is not None:
        methods.append(
            {
                "source": "NOAA Storm Prediction Center",
                "identifier": "SPC severe weather database, hail, damaging wind and tornado reports",
                "period": f"{severe['window']['start']}–{severe['window']['end']}",
                "citation": f"NOAA/NWS Storm Prediction Center, severe weather database files ({severe.get('source_url')}), "
                            + ", ".join(f"{v['file']} of {v['last_modified']}" for v in severe.get("vintage", {}).values()) + ".",
                "method": f"Reports with a start point within {severe['radius_miles']:.0f} miles of the parcel centroid; "
                          "tornadoes counted once per track.",
                "notes": ["Reports are for warning verification and cluster near roads and towns; tornadoes are a count "
                          "only, too few for a monthly pattern. No direction is reported."],
            }
        )
    return methods


# ======================================================================
# The section
# ======================================================================


def build_climate_section(report_data, tokens=None) -> dict:
    """
    The section dict `templates/report/sections/climate.html` renders:
    {number, name, template, heading, summary, water_balance, wind_roses,
    key_figures, key_figures_caption, table, table_caption, design_storms,
    severe_weather, sources, methods}. `number` is the section's Roman
    numeral in the fixed outline (report_outline.section_number) -- II,
    whatever else renders. `tokens` defaults to site_report.TOKENS.
    """
    if tokens is None:
        import site_report

        tokens = site_report.TOKENS
    climate = report_data.climate
    daily = report_data.daymet_daily
    if climate is None or daily is None:
        raise ValueError("build_climate_section: the report data carries no climate block")
    unavailable = report_data.unavailable or {}
    heavy = getattr(report_data, "heavy_rain_normals", None)
    return {
        "number": section_number(SECTION_NAME),
        "name": SECTION_NAME,
        "template": SECTION_TEMPLATE,
        "heading": SECTION_NAME,
        "summary": build_summary(climate),
        "water_balance": build_water_balance(climate, tokens),
        "wind_roses": build_wind_roses(report_data.wind, unavailable, tokens),
        "key_figures": build_key_figures(climate),
        "key_figures_caption": build_key_figures_caption(climate),
        "table": build_table(climate, heavy),
        "table_caption": build_table_caption(report_data.precipitation_correction, heavy),
        "design_storms": build_design_storms(report_data.atlas14, report_data.design_storms, unavailable, report_data.centroid),
        "severe_weather": build_severe_weather(report_data.severe_weather) if report_data.severe_weather else None,
        "sources": build_sources(report_data),
        "methods": build_methods(report_data),
    }


def format_generated_on(when: date) -> str:
    """'21 September 2026' -- the cover's and the running footer's date."""
    return f"{when.day} {MONTH_NAMES[when.month - 1]} {when.year}"
