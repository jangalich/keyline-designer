"""
back_matter.py

THE REPORT'S BACK MATTER -- two pages built from what every section has
been accumulating since Climate: the vintage table, and the methods note.

    build_back_matter(sections, report_data, layer1_retrieved_on) -> the dict back_matter.html renders
    footer_lines(section)                                         -> the source lines a section's footer prints
    SOURCES                                                       -> the registry, one entry per source

THE VINTAGE TABLE IS THE PAGE A CONSULTANT USES: every source any section
cites, once, under ONE NAME (3DEP was written four ways across four
sections; here it is "USGS 3D Elevation Program (3DEP)" with the grid in
the version column), with its version, its retrieval date and the terms
it was found under, and the sections that use it.

A ROW EXISTS BECAUSE A FOOTER CITES IT. Each registry entry carries a
matcher over footer lines; the table's rows are the entries some section's
printed footer matches, and a row's "used in" is exactly those sections.
test_back_matter.py asserts the converse mechanically: every line printed
in any section's footer matches at least one entry, and that entry's row
is on the rendered page. A source added to a footer without an entry here
fails that test rather than going missing from the table. Two rows are
always present because the overview states them without a footer line:
broadband, NOT ASSESSED, so the table says what was not looked at as
plainly as what was.

RETRIEVAL DATES ARE DATA, from where each fetch happened:

    session   Layer 1 -- the session's fetch when the Design Document was
              created (every section's `retrieved_on`);
    report    the report layer -- report_data.ReportData.retrieved_on;
    bundle    a class E file in the repository -- its build or source-file
              date, never a retrieval, and the column says "bundled";
    design    the committed design itself -- not a data source; its row
              says "as committed".

TERMS ARE AS FOUND, one short statement per source; the fuller text is in
each source module's TERMS constant. Where two statements conflict (HIFLD
public use, served under Esri's licence) both are given, and where terms
were never confirmed (NAIP) the row says so.

THE METHODS NOTE collects each section's `methods` structure in outline
order -- the method behind every figure and the thresholds each section
said it would record here -- and closes with the two rules the whole
report shares and no one section owns: the largest-remainder allocator
every acreage table sums with, and the display smoothing of the
production block's outline on the layout map. Both are written from the
code's own constants, so the note cannot drift from what ran.
"""

import re
from datetime import date
from typing import Callable, Optional

import display_outline
import livestock_predators
import physiography
import precipitation_normals
import transmission_lines
from climate_section import format_generated_on

# ======================================================================
# The registry
# ======================================================================

SESSION, REPORT, BUNDLE, DESIGN, NOT_ASSESSED = "session", "report", "bundle", "design", "not assessed"

PUBLIC_DOMAIN = "Public domain."
# THE ONE OUTSTANDING TERMS QUESTION (naip_imagery.py: VERIFY BEFORE BUILD):
# Microsoft Planetary Computer's hosting terms. The data it serves are
# federal and public domain; the host lists NAIP's licence as "proprietary"
# and its own terms are unconfirmed. Both collections it serves here --
# the lidar heights and NAIP -- read the same way until that is settled.
PLANETARY_COMPUTER_TERMS = "Public domain; Planetary Computer's terms unconfirmed."


def _text(line) -> str:
    return "".join(p if isinstance(p, str) else str(p["value"]) for p in line)


def _has(*words) -> Callable[[str], bool]:
    patterns = [re.compile(w, re.I) for w in words]
    return lambda line: all(p.search(line) for p in patterns)


def _not(test, *words) -> Callable[[str], bool]:
    patterns = [re.compile(w, re.I) for w in words]
    return lambda line: test(line) and not any(p.search(line) for p in patterns)


_CONTEXT = r"one mile"


def _fmt_iso(value) -> str:
    if isinstance(value, date):
        return format_generated_on(value)
    return format_generated_on(date.fromisoformat(str(value)[:10]))


def _survey_version(ctx) -> str:
    survey = (ctx["data"].soil_survey or {}).get("survey_areas") or []
    parts = [f"{s['areasymbol']}, version of {s['saverest'].split(' ')[0]}" for s in survey if s.get("saverest")]
    return "; ".join(parts) or "survey version not returned"


def _nfhl_version(ctx) -> str:
    water = ctx["sections"].get("Water & hydrology")
    panel = ((water or {}).get("derived") and water["derived"].flood.get("panel")) or {}
    if panel.get("firm_pan"):
        effective = f", effective {_fmt_iso(panel['effective_on'])}" if panel.get("effective_on") else ""
        return f"FIRM panel {panel['firm_pan']}{effective}"
    return "National Flood Hazard Layer"


def _roads_version(ctx) -> str:
    access = ctx["sections"].get("Access")
    if access and access.get("derived") is not None:
        import access_section

        vintage = access_section._road_vintage(access["derived"].roads)
        if vintage:
            return f"local roads, TIGER/Line of {vintage}"
    return "local roads, TIGER/Line"


def _canopy_version(ctx) -> str:
    trees = ctx["sections"].get("Trees & forestry")
    canopy = trees["derived"].canopy if trees and trees.get("derived") is not None else {}
    item = canopy.get("source_item_id")
    return f"{item}, 2 m" if item else "2 m height above ground"


def _context_grid(ctx) -> str:
    dem = ctx["data"].context_dem
    if dem is None:
        return "context grid"
    return f"{max(dem['resolution_meters']):.0f} m, extent plus a mile"


def _naip_version(ctx) -> str:
    imagery = ctx["data"].naip_imagery or {}
    dates = ", ".join(_fmt_iso(d) for d in imagery.get("acquired") or [])
    return f"acquired {dates}, {imagery.get('gsd', 0.6)} m" if dates else "orthoimagery"


def _structures_version(ctx) -> str:
    block = ctx["data"].structures or {}
    images = ", ".join(_fmt_iso(d) for d in block.get("image_dates") or [])
    produced = ", ".join(_fmt_iso(d) for d in block.get("production_dates") or [])
    return (f"imagery of {images}" if images else "imagery date not stated") + (f", produced {produced}" if produced else "")


def _normals_retrieved(ctx) -> str:
    vintage, _ = precipitation_normals.load_bundle()
    match = re.search(r"_c(\d{4})(\d{2})(\d{2})", vintage.get("annual_archive", ""))
    return f"bundled, files of {short_date(date(int(match.group(1)), int(match.group(2)), int(match.group(3))))}" if match else "bundled"


def _spc_retrieved(ctx) -> str:
    vintage = ((ctx["data"].severe_weather or {}).get("vintage") or {}).get("hail", {}).get("last_modified", "")
    if not vintage:
        return "bundled"
    from email.utils import parsedate_to_datetime

    return f"bundled, files of {short_date(parsedate_to_datetime(vintage).date())}"


def _physio_retrieved(ctx) -> str:
    built = physiography.load_bundle()["header"].get("built_on")
    return f"bundled {short_date(date.fromisoformat(built))}" if built else "bundled"


def _nahms_retrieved(ctx) -> str:
    built = livestock_predators.load_bundle().get("built_on")
    return f"bundled {short_date(date.fromisoformat(built))}" if built else "bundled"


# key, name, matcher, version (str or ctx -> str), retrieval kind (or ctx -> str for bundles), terms
SOURCES = (
    ("daymet", "Daymet daily weather (ORNL DAAC)", _has(r"\bDaymet\b"),
     lambda c: f"Version 4 R1, 1 km, {c['data'].climate['period']['start']}–{c['data'].climate['period']['end']}",
     REPORT, "No restriction on use; citation required."),
    ("normals", "NOAA NCEI U.S. Climate Normals", _has(r"Climate Normals"), "1991–2020, v1.0.1, five nearest stations",
     _normals_retrieved, PUBLIC_DOMAIN),
    ("atlas14", "NOAA Atlas 14 precipitation frequency", _has(r"Atlas 14"),
     lambda c: (c["data"].atlas14 or {}).get("source_line", "NOAA Atlas 14").replace("NOAA Atlas 14 ", ""),
     REPORT, "Public domain."),
    ("power", "NASA POWER (MERRA-2)", _has(r"NASA POWER"), "0.5° × 0.625° cell, WS10M and WD10M",
     REPORT, "Free and open; acknowledgement requested."),
    ("spc", "NOAA SPC severe weather reports", _has(r"Storm Prediction Center"),
     "hail, damaging wind and tornado reports, 1995–2024", _spc_retrieved, PUBLIC_DOMAIN),
    ("3dep", "USGS 3D Elevation Program (3DEP)", _not(_has(r"3DEP elevation"), _CONTEXT),
     "1/3 arc-second, resampled to 5 m", SESSION, PUBLIC_DOMAIN),
    ("3dep_context", "USGS 3D Elevation Program (3DEP)", _has(r"3DEP elevation", _CONTEXT), _context_grid, REPORT,
     PUBLIC_DOMAIN),
    ("3dep_hag", "USGS 3DEP lidar height above ground", _has(r"lidar height above ground"),
     _canopy_version, SESSION, PLANETARY_COMPUTER_TERMS),
    ("tcc", "USDA Forest Service NLCD Tree Canopy Cover", _has(r"Tree Canopy Cover"), "30 m", SESSION, PUBLIC_DOMAIN),
    ("nhd", "USGS National Hydrography Dataset (NHD)", _not(_has(r"National Hydrography Dataset"), _CONTEXT),
     "1:24,000 flowlines and waterbodies", SESSION, PUBLIC_DOMAIN),
    ("nhd_context", "USGS National Hydrography Dataset (NHD)", _has(r"National Hydrography Dataset", _CONTEXT),
     "extent plus a mile", REPORT, PUBLIC_DOMAIN),
    ("nhdplus", "USGS NHDPlus High Resolution", _has(r"NHDPlus"), "network flowline attributes", REPORT, PUBLIC_DOMAIN),
    ("ssurgo_spatial", "USDA NRCS Soil Survey (SSURGO)", _has(r"SSURGO map unit polygons"),
     "polygons and components", SESSION, PUBLIC_DOMAIN),
    ("ssurgo", "USDA NRCS Soil Survey (SSURGO)", _has(r"SSURGO, soil survey"), _survey_version, REPORT, PUBLIC_DOMAIN),
    ("nwi", "USFWS National Wetlands Inventory", _has(r"National Wetlands Inventory"),
     lambda c: (lambda p: f"{p.get('name')}, {p.get('image_year')} imagery" if p.get("name") else "wetlands")(
         (c["data"].nwi or {}).get("project") or {}),
     REPORT, 'Constraints "None"; USFWS acknowledgement requested.'),
    ("nfhl", "FEMA National Flood Hazard Layer", _has(r"Flood Hazard Layer"), _nfhl_version, REPORT,
     "Federal work; no NFHL use restriction stated."),
    ("nlcd", "USGS Annual NLCD land cover", _not(_has(r"NLCD"), r"Tree Canopy"),
     lambda c: f"Collection 1.1, {(c['data'].nlcd_landcover or {}).get('year', '')}, 30 m", REPORT, PUBLIC_DOMAIN),
    ("bigmap", "USFS FIA BIGMAP forest type group", _has(r"BIGMAP"), "2018, plots 2014–2018, 30 m",
     REPORT, "Public domain; credit USDA Forest Service FIA."),
    ("sgmc", "USGS State Geologic Map Compilation (SGMC)", _has(r"Geologic Map Compilation|SGMC"),
     "Version 1.1, Horton 2017", REPORT, "No constraint on access or use."),
    ("transportation", "USGS National Map transportation", _not(_has(r"National Map transportation"), _CONTEXT),
     _roads_version, SESSION, PUBLIC_DOMAIN),
    ("transportation_context", "USGS National Map transportation", _has(r"National Map transportation", _CONTEXT),
     "extent plus a mile", REPORT, PUBLIC_DOMAIN),
    ("naip", "USDA FSA NAIP aerial imagery", _has(r"National Agriculture Imagery Program"),
     _naip_version, REPORT, PLANETARY_COMPUTER_TERMS),
    ("census", "U.S. Census Bureau geocoder", _has(r"Census Bureau geocoder"), "Public_AR_Current, Current_Current", REPORT,
     PUBLIC_DOMAIN),
    ("structures", "FEMA and ORNL USA Structures", _has(r"USA Structures"), _structures_version, REPORT,
     "CC BY 4.0; attribution printed."),
    ("hifld", "HIFLD transmission lines", _has(r"HIFLD"),
     f"Esri archive, last data update {format_generated_on(date.fromisoformat(transmission_lines.ARCHIVE_DATE))}",
     REPORT, 'HIFLD "None (Public Use)"; served under the Esri Master License Agreement.'),
    ("physiography", "USGS physiographic divisions", _has(r"physiographic divisions"),
     "Fenneman and Johnson 1946, 1:7,000,000", _physio_retrieved, PUBLIC_DOMAIN),
    ("nahms", "USDA APHIS NAHMS death-loss surveys", _has(r"NAHMS"),
     "cattle and calves 2015; sheep and lambs 2014", _nahms_retrieved, PUBLIC_DOMAIN),
    ("design", "The committed design", _has(r"^The design:"),
     "the Design Document's committed steps", DESIGN, "Your own."),
)

# Stated on the overview without a footer line, and always in the table.
ALWAYS = (
    ("broadband", "FCC National Broadband Map", "no location lookup for a report", NOT_ASSESSED, "—"),
)


def footer_lines(section: dict) -> list:
    """The source lines a section's footer prints, as text: Landform's
    single citation (its `footer`), every other section's `sources`."""
    if section.get("footer"):
        footer = section["footer"]
        lines = [footer["citation"]] if footer.get("citation") else []
        return [_text(line) for line in lines + list(footer.get("lines") or [])]
    return [_text(line) for line in section.get("sources") or []]


def matching_sources(line: str) -> list:
    return [entry[0] for entry in SOURCES if entry[2](line)]


def short_date(when: date) -> str:
    """'25 Sep 2026': the table's date, short enough to hold one line."""
    return f"{when.day} {when.strftime('%b')} {when.year}"


_OUTLINE_NUMERALS = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII")


def numeral_ranges(numbers: list) -> str:
    """['III', 'IV', 'V', 'VII'] -> 'III–V, VII'."""
    order = sorted(numbers, key=_OUTLINE_NUMERALS.index)
    runs, run = [], []
    for number in order:
        if run and _OUTLINE_NUMERALS.index(number) != _OUTLINE_NUMERALS.index(run[-1]) + 1:
            runs.append(run)
            run = []
        run.append(number)
    if run:
        runs.append(run)
    return ", ".join(r[0] if len(r) == 1 else f"{r[0]}–{r[-1]}" if len(r) > 2 else f"{r[0]}, {r[1]}" for r in runs)


def _retrieved(kind, ctx) -> str:
    if callable(kind):
        return kind(ctx)
    if kind == SESSION:
        return short_date(ctx["layer1_retrieved_on"])
    if kind == REPORT:
        return short_date(ctx["data"].retrieved_on) if ctx["data"].retrieved_on else "at report time"
    if kind == DESIGN:
        return "as committed"
    return kind


def build_vintage_table(sections: list, report_data, layer1_retrieved_on: date) -> dict:
    """{'rows': [{'keys', 'source', 'version', 'retrieved', 'used_in', 'terms'}], 'unmatched': [(section, line)]}"""
    ctx = {"data": report_data, "layer1_retrieved_on": layer1_retrieved_on,
           "sections": {s["name"]: s for s in sections}}
    used = {}
    unmatched = []
    for section in sections:
        for line in footer_lines(section):
            keys = matching_sources(line)
            if not keys:
                unmatched.append((section["name"], line))
            for key in keys:
                used.setdefault(key, [])
                if section["number"] not in used[key]:
                    used[key].append(section["number"])
    # ONE ROW PER SOURCE NAME: entries that share a name (3DEP at 5 m for the
    # parcel and at the context grid for the overview) merge, each part's
    # version followed by the sections it serves, so 3DEP reads once.
    rows, by_name = [], {}
    for key, name, _, version, kind, terms in SOURCES:
        if key not in used:
            continue
        part = {"key": key, "version": version(ctx) if callable(version) else version, "retrieved": _retrieved(kind, ctx),
                "used": used[key]}
        if name in by_name:
            by_name[name]["parts"].append(part)
        else:
            by_name[name] = {"source": name, "terms": terms, "parts": [part]}
            rows.append(by_name[name])
    for row in rows:
        parts = row.pop("parts")
        row["keys"] = [p["key"] for p in parts]
        if len(parts) == 1:
            row["version"] = parts[0]["version"]
        else:
            row["version"] = "; ".join(f"{p['version']} ({numeral_ranges(p['used'])})" for p in parts)
        row["retrieved"] = ", ".join(dict.fromkeys(p["retrieved"] for p in parts))
        row["used_in"] = numeral_ranges(sorted({n for p in parts for n in p["used"]}, key=_OUTLINE_NUMERALS.index))
    for key, name, version, kind, terms in ALWAYS:
        rows.append({"keys": [key], "source": name, "version": version, "retrieved": kind, "used_in": "I", "terms": terms})
    return {"rows": rows, "unmatched": unmatched}


def vintage_data_table(vintage: dict) -> dict:
    """The rows in data_table's shape: the source as the row label, the
    other four as prose columns (versions and terms are words; dates are
    dates, set as data)."""
    rows = []
    for row in vintage["rows"]:
        rows.append({"label": row["source"], "cells": [
            {"kind": "text", "value": row["version"]},
            {"kind": "text", "value": [{"value": row["retrieved"]}] if row["retrieved"][:1].isdigit() else row["retrieved"]},  # noqa: E501
            {"kind": "text", "value": [{"value": row["used_in"]}]},
            {"kind": "text", "value": row["terms"]},
        ]})
    return {"corner": "Source", "columns": ["Version", "Retrieved", "Used in", "Terms"],
            "text_columns": ["Version", "Retrieved", "Used in", "Terms"], "rows": rows, "variant": "vintage"}


# ======================================================================
# The methods note
# ======================================================================


def _passes(count: int) -> str:
    return {1: "one pass", 2: "two passes", 3: "three passes"}.get(count, f"{count} passes")


def report_wide_methods() -> list:
    """The two rules the whole report shares, from the code's own constants."""
    return [
        {"source": "Acreage allocation",
         "method": ("Every acreage table is cell counts on the 5 m grid scaled to the boundary polygon's area and rounded by "
                    "the largest-remainder method: each class takes the floor of its exact "
                    "share at one decimal, and the tenths left over go to the classes with the largest remainders, ties to "
                    "the larger class. The rows therefore sum exactly to the cover's acreage, and a percentage column to "
                    "100.0.")},
        {"source": "Display smoothing",
         "method": ("A suggested production block is a union of grid cells, so its edge is a staircase. On the layout map "
                    "only, its outline is simplified by Douglas-Peucker at a tolerance of "
                    f"{display_outline.DISPLAY_OUTLINE_SIMPLIFY_TOLERANCE_CELLS:g} grid cell and rounded by "
                    f"{_passes(display_outline.DISPLAY_OUTLINE_CHAIKIN_ITERATIONS)} of Chaikin's corner cutting, clipped to the "
                    "block's own footprint. Nothing is computed from the smoothed outline: acreage, cautions and every "
                    "downstream step read the cells. A drawn block and the tree zones are never smoothed.")},
    ]


def _sentence(text: str) -> str:
    """A methods fragment set as a sentence: the ASCII dash made an em
    dash, a closing full stop added where the fragment has none (a
    section's `method` is often a phrase -- "Thornthwaite (1948), monthly"
    -- and its notes follow it in the same paragraph)."""
    text = text.strip().replace(" -- ", " — ")
    return text if text[-1:] in ".!?" else text + "."


def build_methods_note(sections: list) -> list:
    """[{'number', 'name', 'entries': [{'source', 'text': [str, ...]}]}] in
    outline order, then the report-wide rules under the back matter's own
    heading."""
    groups = []
    for section in sections:
        entries = []
        for method in section.get("methods") or []:
            text = [method["method"]] + list(method.get("notes") or [])
            entries.append({"source": method["source"], "text": [_sentence(t) for t in text if t]})
        if entries:
            groups.append({"number": section["number"], "name": section["name"], "entries": entries})
    groups.append({"number": None, "name": "Across the report",
                   "entries": [{"source": m["source"], "text": [m["method"]]} for m in report_wide_methods()]})
    return groups


def build_back_matter(sections: list, report_data, layer1_retrieved_on: date) -> dict:
    vintage = build_vintage_table(sections, report_data, layer1_retrieved_on)
    return {
        "template": "back_matter.html",
        "vintage": vintage,
        "vintage_table": vintage_data_table(vintage),
        "vintage_caption": ["Retrieved is when this report's data were fetched; a bundled source ships with the software."],
        "methods": build_methods_note(sections),
    }
