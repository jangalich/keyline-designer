"""
overview_section.py

SECTION I, SITE OVERVIEW -- ONE PAGE: the summary line, the context map,
the figures, the boundary statement and the sources.

    build_overview_section(inputs, tokens) -> the section dict overview.html renders
    build_context_map(inputs, derived, tokens) -> render_map()'s dict

THE CONTEXT MAP IS DELIBERATELY UNLIKE EVERY OTHER MAP IN THE REPORT. A
line map on paper -- no imagery, no tints -- of about a mile around the
parcel: contours in terrain brown at the context interval (20 ft here,
100 ft index lines labelled, because elevation relative to the
surroundings is the point), NHD streams and water bodies in water blue,
the mapped roads in ink and named, and the parcel outline heavier than
all of it and small on the page. Everything that makes it a topographic
sheet rather than a parcel map is on purpose: a different scale (the
scale bar says so), a different interval, a coarser grid, and a caption
that says the contours are not the parcel's terrain (Landform's are). A
reader who took these contours for the property's would be misled, so
the map must look like what it is.

It is drawn through report_map.render_map() with the CONTEXT EXTENT as the
fitted shape and no boundary line -- the frame, north arrow, scale bar,
labelled lines and legend are the shared component's -- and the parcel
drawn as its own heavy line layer on top. The extent is the context DEM's
window, cropped to the frame's aspect about the parcel's centre, so every
line on the map is drawn from data that covers it.

NO AERIAL PHOTOGRAPH HERE. It would be section VIII's layout map's NAIP,
same parcel, same date, twenty pages apart. The reader's first sight of
the property is therefore a line map; the photograph is the deliverable's.

THE BOUNDARY STATEMENT is printed on the page, plain: the boundary is as
the user drew it and is not a survey. A tax parcel number, if the report
ever carries one, prints as supplied above it; the statement stays
regardless (branch 17 step 0: no such field exists anywhere today).

No colour literal lives here: every colour is a token name resolved
against the `tokens` the caller passes (site_report.TOKENS).
"""

from typing import Optional

from rasterio.warp import transform_geom
from shapely.geometry import LineString, box, shape
from shapely.ops import linemerge, unary_union

import census_geography
import overview_derivations as od
import report_map
import structures_data
import transmission_lines
from climate_section import format_generated_on
from report_outline import section_number

SECTION_NAME = "Site overview"
SECTION_TEMPLATE = "overview.html"

FT = 1 / 0.3048
METERS_PER_MILE = 1609.344

# The context map's frame: near-square, beside the key figures, so it
# shows about a mile of ground in every direction rather than a strip.
CONTEXT_FRAME = (318.0, 300.0)
CONTOUR_PT = 0.3
INDEX_CONTOUR_PT = 1.1
# THE TINT: the ground between index contours, darker as it rises, so the
# reader SEES the parcel below the surrounding high ground the summary
# describes. The lowest band is untinted; each band above it adds this
# much terrain-brown opacity. Light enough that every contour reads over
# the top band.
BAND_OPACITY_STEP = 0.07
STREAM_PT = 0.7
ROAD_PT = 0.6
PARCEL_PT = 1.8
# The roads named on the map: the longest named roads in the extent, so
# the labels do not crowd; every road is drawn.
NAMED_ROADS = 6
ROAD_LABEL_CLEARANCE_M = 60.0

BOUNDARY_STATEMENT = "The boundary is as drawn by the user and is not a survey."


# ======================================================================
# Formatting
# ======================================================================


def _ft(value_ft: float) -> str:
    return f"{round(value_ft):,}"


def _mi(meters: float) -> str:
    return f"{meters / METERS_PER_MILE:.1f} mi"


def _acres(value: float) -> str:
    return f"{value:,.1f}"


def _lat_lon(centroid) -> str:
    lat, lon = centroid
    return f"{abs(lat):.4f}° {'N' if lat >= 0 else 'S'}, {abs(lon):.4f}° {'E' if lon >= 0 else 'W'}"


def relief_position(percentile: float) -> str:
    """Where the parcel's mean elevation sits among the surrounding
    ground's, in words: the plain version of a percentile."""
    if percentile < 20:
        return "low in"
    if percentile < 40:
        return "in the lower-middle of"
    if percentile < 60:
        return "in the middle of"
    if percentile < 80:
        return "in the upper-middle of"
    return "high in"


def _series(words: list) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


# ======================================================================
# The context map
# ======================================================================


def context_extent(context_dem: dict, parcel, frame: tuple = CONTEXT_FRAME) -> tuple:
    """The ground render_map fits to the drawable area, chosen so that
    everything the frame SHOWS -- margins included, the furniture band
    excepted -- lies inside the context window: the largest scale at which
    the whole visible frame is covered ground, centred on the parcel as
    far as the window allows. The map is filled edge to edge."""
    rows, cols = context_dem["array"].shape
    px, py = context_dem["resolution_meters"]
    minx, maxy = context_dem["origin_x"], context_dem["origin_y"]
    maxx, miny = minx + cols * px, maxy - rows * py
    # Inset one cell so no edge of the view is interpolated nodata.
    minx, maxx, miny, maxy = minx + px, maxx - px, miny + py, maxy - py
    width, height = frame
    margin, band = report_map.MARGIN_PT, report_map.FURNITURE_BAND_PT
    meters_per_pt = min((maxx - minx) / width, (maxy - miny) / (height - band))
    view_w, view_h = width * meters_per_pt, (height - band) * meters_per_pt
    cx, cy = parcel.centroid.x, parcel.centroid.y
    cx = min(max(cx, minx + view_w / 2), maxx - view_w / 2)
    # The visible ground runs from the top edge down to the band; its centre
    # sits `band / 2` points above the drawable area's.
    cy = min(max(cy, miny + view_h / 2), maxy - view_h / 2)
    ground_w = (width - 2 * margin) * meters_per_pt
    ground_h = (height - 2 * margin - band) * meters_per_pt
    return (cx - ground_w / 2, cy - ground_h / 2, cx + ground_w / 2, cy + ground_h / 2)


def _utm(geometry: dict, crs: str):
    return shape(transform_geom("EPSG:4326", crs, geometry))


def _label_point(geometry):
    parts = list(getattr(geometry, "geoms", [geometry]))
    longest = max(parts, key=lambda g: g.length)
    return longest.interpolate(0.5, normalized=True)


def _under_furniture(geometry, furniture) -> bool:
    return any(box_.buffer(0).contains(_label_point(geometry)) for box_ in furniture)


def _road_layers(roads: list, crs: str, visible, parcel, furniture: tuple = ()) -> list:
    by_name = {}
    for road in roads or []:
        geometry = _utm(road["geometry"], crs).intersection(visible)
        if geometry.is_empty:
            continue
        by_name.setdefault(road["name"], []).append(geometry)
    merged = []
    for name, parts in by_name.items():
        union = unary_union(parts)
        if union.geom_type == "MultiLineString":
            union = linemerge(union)
        merged.append((name, union))
    # A road along the boundary is not named: its label would sit on the
    # parcel outline, which is the one mark on this map that must read.
    # Nor is one whose label would land on the north arrow or the scale bar.
    named = sorted((m for m in merged if m[0] != "Unnamed road" and m[1].distance(parcel) > ROAD_LABEL_CLEARANCE_M
                    and not _under_furniture(m[1], furniture)), key=lambda m: -m[1].length)[:NAMED_ROADS]
    labels = {name for name, _ in named}
    geometries = [g for _, g in merged]
    return [report_map.layer("context-roads", geometries, kind="line", stroke="ink", stroke_width=ROAD_PT,
                             labels=[name if name in labels else None for name, _ in merged], legend="Mapped roads",
                             label_face="prose")]


def build_context_map(inputs: od.OverviewInputs, derived: od.OverviewDerived, tokens: dict,
                      frame: tuple = CONTEXT_FRAME) -> Optional[dict]:
    """The line map, or None when the context DEM did not answer (there is
    no extent to draw without it)."""
    if inputs.context_dem is None or derived.context_contours is None:
        return None
    parcel = inputs.boundary_polygon_utm
    crs = inputs.context_dem["crs"]
    extent = box(*context_extent(inputs.context_dem, parcel, frame))
    visible = box(*report_map.visible_extent_utm(extent, frame, fit=False))
    contours = derived.context_contours
    interval, index_ft = contours["interval_ft"], contours["interval_ft"] * contours["index_every"]
    plain = [lv["geometry"].intersection(visible) for lv in contours["levels"] if not lv["index"]]
    index = [(lv["geometry"].intersection(visible), lv["elevation_ft"]) for lv in contours["levels"] if lv["index"]]
    bands = contours.get("bands") or []
    layers = []
    for i, band in enumerate(bands):
        if i == 0 or band["geometry"].is_empty:
            continue
        top = i == len(bands) - 1
        layers.append(report_map.layer(
            f"context-band-{i}", [band["geometry"].intersection(visible)], kind="polygon", fill="terrain",
            fill_opacity=BAND_OPACITY_STEP * i,
            legend=("Higher ground darker" if top else None)))
    layers += [
        report_map.layer("context-contours", [g for g in plain if not g.is_empty], kind="line", stroke="terrain",
                         stroke_width=CONTOUR_PT, legend=["Contours, ", {"value": f"{interval} ft"}]),
        report_map.layer("context-index-contours", [g for g, _ in index if not g.is_empty], kind="line", stroke="terrain",
                         stroke_width=INDEX_CONTOUR_PT, labels=[f"{e:,}" for g, e in index if not g.is_empty],
                         legend=["Index, ", {"value": f"{index_ft} ft"}]),
    ]
    water = inputs.context_water
    if water is not None:
        bodies = [_utm(b["geometry"], crs).intersection(visible) for b in water.get("water_bodies", [])]
        streams = [_utm(s["geometry"], crs).intersection(visible) for s in water.get("streams", [])]
        layers.append(report_map.layer("context-water-bodies", [b for b in bodies if not b.is_empty], kind="polygon",
                                       stroke="water", stroke_width=0.4, fill="water", fill_opacity=0.18))
        layers.append(report_map.layer("context-streams", [s for s in streams if not s.is_empty], kind="line",
                                       stroke="water", stroke_width=STREAM_PT, legend="Streams and water bodies"))
    if inputs.context_roads is not None:
        # The corners the north arrow (top right) and the scale bar (bottom
        # right) are drawn in, in ground metres, with a label's reach.
        mpu = (extent.bounds[2] - extent.bounds[0]) / (frame[0] - 2 * report_map.MARGIN_PT)
        vx0, vy0, vx1, vy1 = visible.bounds
        furniture = (box(vx1 - 70 * mpu, vy1 - 60 * mpu, vx1, vy1), box(vx1 - 170 * mpu, vy0, vx1, vy0 + 40 * mpu))
        layers += _road_layers(inputs.context_roads, crs, visible, parcel, furniture)
    layers.append(report_map.layer("context-parcel", [LineString(parcel.exterior.coords)], kind="line", stroke="ink",
                                   stroke_width=PARCEL_PT, legend="The parcel"))
    # halo: the north arrow and the scale bar are cased in the page colour --
    # they sit on contour linework edge to edge, not on an empty margin.
    return report_map.render_map(extent, layers, tokens, frame=frame, fit=False, boundary_style={"stroke": None},
                                 halo=True)


# ======================================================================
# The page's words and figures
# ======================================================================


def build_summary(derived: od.OverviewDerived) -> list:
    """Two sentences: the drainage fact first when more ground drains onto
    the parcel than it covers -- the one fact about its setting a reader
    would act on -- then where it sits, in words, and where it is."""
    parts = []
    acres = round(derived.acres, 1)
    landscape = derived.landscape
    place = []
    if derived.county_state:
        place.append(census_geography.county_state_label(derived.county_state))
    if derived.physiography:
        place.append(f"on the {derived.physiography['province']} of the {derived.physiography['division']}")
    where = (", in " + ", ".join(place)) if place else ""
    if landscape:
        inflow = landscape["inflow"]
        bound = "at least " if inflow["truncated"] else ""
        position = relief_position(landscape["percentile_mean"])
        if inflow["acres"] > derived.acres:
            parts += [f"{bound.capitalize()}", {"value": f"{_acres(inflow['acres'])} acres"},
                      " of higher ground drain onto this ", {"value": f"{_acres(acres)}-acre"},
                      " parcel, more than its own area. "]
        else:
            parts += ["This ", {"value": f"{_acres(acres)}-acre"}, " parcel takes the drainage of ", bound,
                      {"value": f"{_acres(inflow['acres'])} acres"}, " of higher ground. "]
        parts += [f"It sits on a {landscape['class']} {position} the surrounding terrain{where}."]
    else:
        parts += ["A ", {"value": f"{_acres(acres)}-acre"}, f" parcel{where}."]
    return [p for p in parts if p != ""]


def build_key_figures(derived: od.OverviewDerived) -> list:
    figures = [
        {"value": _acres(round(derived.acres, 1)), "label": "acres"},
        {"value": f"{_ft(derived.perimeter_m * FT)} ft", "label": "perimeter"},
        {"value": f"{_ft(derived.elevation['min_ft'])}–{_ft(derived.elevation['max_ft'])}", "label": "elevation range, ft"},
    ]
    if derived.landscape:
        figures.append({"value": derived.landscape["class"], "word": True,
                        "label": f"landscape position, {derived.landscape['radius_m']:.0f} m radius"})
    else:
        figures.append({"value": "not assessed", "word": True, "label": "landscape position"})
    if derived.buildings is not None:
        count = derived.buildings["count"]
        figures.append({"value": "none" if count == 0 else str(count), "word": count == 0,
                        "label": "mapped buildings on the parcel"})
    else:
        figures.append({"value": "not assessed", "word": True, "label": "buildings on the parcel"})
    if derived.transmission is not None and derived.transmission["nearest"]:
        figures.append({"value": _mi(derived.transmission["nearest"]["distance_m"]),
                        "label": "to the nearest transmission line"})
    else:
        figures.append({"value": "over 5 mi" if derived.transmission is not None else "not assessed",
                        "word": derived.transmission is None, "label": "to the nearest transmission line"})
    return figures


def wildlife_parts(wildlife: Optional[dict]) -> Optional[list]:
    """Two sentences: the finding, then the caveat. None where the State
    was not surveyed separately."""
    if not wildlife or not wildlife["surveyed"]:
        return None
    leads = {x for x in ((wildlife["cattle"] or {}).get("calf_lead"), (wildlife["cattle"] or {}).get("cattle_lead"),
                         (wildlife["sheep"] or {}).get("lead")) if x}
    lead = _series(sorted(leads))
    shares = [v for v in ((wildlife["cattle"] or {}).get("calf_pct"), (wildlife["cattle"] or {}).get("cattle_pct"),
                          (wildlife["sheep"] or {}).get("pct")) if v is not None]
    extent = "the large majority of" if shares and min(shares) > 50 else "the largest share of"
    parts = [f"{lead.capitalize()} account for {extent} livestock predator losses in {wildlife['state']}"]
    if wildlife["also"]:
        parts.append(f", with {_series(wildlife['also'])} also reported")
    parts.append(". " + wildlife["caveat"])
    return ["".join(parts)]


def build_facts_table(derived: od.OverviewDerived) -> dict:
    rows = []
    transmission = derived.transmission
    if transmission is not None:
        nearest, known = transmission["nearest"], transmission["nearest_known_voltage"]
        if nearest is None:
            text = ["No mapped transmission line within ", {"value": "5 mi"}, "."]
        else:
            text = [{"value": _mi(nearest["distance_m"])}, " to the nearest mapped line, "]
            if nearest["voltage_kv"] is not None:
                text += [{"value": f"{nearest['voltage_kv']:.0f} kV"}, "."]
            else:
                text += ["voltage not recorded"]
                if known is not None:
                    text += ["; the nearest of known voltage is ", {"value": f"{known['voltage_kv']:.0f} kV"}, " at ",
                             {"value": _mi(known["distance_m"])}]
                text += ["."]
        text += [f" HIFLD, {transmission_lines.ARCHIVE_LABEL}."]
        rows.append({"label": "Transmission", "cells": [{"kind": "text", "value": text}]})
    # Broadband is not assessed: the vintage table carries that row, where
    # a reader looks for what each source is and is not.
    wildlife = wildlife_parts(derived.wildlife)
    if wildlife:
        rows.append({"label": "Livestock predators", "cells": [{"kind": "text", "value": wildlife}]})
    return {"corner": "", "columns": [""], "text_columns": [""], "rows": rows, "variant": "facts"}


def build_boundary_statement(derived: od.OverviewDerived) -> list:
    """The statement, and the centroid every point-based figure was read at."""
    return [BOUNDARY_STATEMENT + " Its centre, where the point-based sources were read: ",
            {"value": _lat_lon(derived.centroid)}, "."]


def build_map_caption(derived: od.OverviewDerived, rendered: dict, inputs: od.OverviewInputs) -> list:
    if rendered is None:
        return []
    cell = max(inputs.context_dem["resolution_meters"])
    return ["About a mile around the parcel, from coarser data: contours every ",
            {"value": f"{derived.context_contours['interval_ft']} ft"}, " from a ", {"value": f"{cell:.0f} m"},
            " elevation grid, the ground tinted darker every ", {"value": f"{derived.context_contours['interval_ft'] * derived.context_contours['index_every']} ft"},
            " higher. This map shows where the parcel sits, not its ground; the parcel's terrain is in Landform."]


def build_unavailable(derived: od.OverviewDerived) -> list:
    """One statement per report-layer source that did not answer."""
    return [["The ", entry["label"], " did not answer; ", {"context_dem": "the context map is not drawn.",
                                                           "context_water": "streams are not drawn on the map.",
                                                           "context_roads": "roads are not drawn on the map.",
                                                           "county_state": "the county is not named.",
                                                           "structures": "buildings are not assessed.",
                                                           "transmission_lines": "transmission is not assessed."}[layer]]
            for layer, entry in derived.unavailable.items()]


def build_sources(inputs: od.OverviewInputs, derived: od.OverviewDerived) -> list:
    """Three lines: the context map's three sources; the report-time
    lookups; the two bundled references."""
    retrieved = format_generated_on(inputs.report_retrieved_on) if inputs.report_retrieved_on else None
    on = f", retrieved {retrieved}" if retrieved else ""
    lines = []
    if inputs.context_dem is not None:
        lines.append([f"USGS 3DEP elevation, USGS National Hydrography Dataset and USGS National Map transportation, "
                      f"over the parcel's extent plus one mile{on}."])
    lookups = []
    if derived.county_state:
        lookups.append("U.S. Census Bureau geocoder")
    if derived.buildings is not None:
        dates = ", ".join(format_generated_on_iso(d) for d in derived.buildings["image_dates"])
        lookups.append(f"{structures_data.ATTRIBUTION}" + (f", imagery of {dates}" if dates else ""))
    if derived.transmission is not None:
        lookups.append(f"HIFLD transmission lines, {transmission_lines.ARCHIVE_LABEL}")
    if lookups:
        lines.append(["; ".join(lookups) + f"{on}."])
    bundled = []
    if derived.physiography:
        bundled.append("USGS physiographic divisions, Fenneman and Johnson 1946")
    if derived.wildlife and derived.wildlife["surveyed"]:
        bundled.append("USDA APHIS NAHMS death-loss surveys, cattle 2015 and sheep 2014")
    if bundled:
        lines.append(["; ".join(bundled) + "."])
    return lines


def format_generated_on_iso(iso: str) -> str:
    from datetime import date

    return format_generated_on(date.fromisoformat(iso))


def build_overview_section(inputs: od.OverviewInputs, tokens: dict, derived: Optional[od.OverviewDerived] = None) -> dict:
    derived = derived or od.derive(inputs)
    rendered = build_context_map(inputs, derived, tokens)
    return {
        "number": section_number(SECTION_NAME),
        "name": SECTION_NAME,
        "template": SECTION_TEMPLATE,
        "heading": SECTION_NAME,
        "summary": build_summary(derived),
        "map": rendered,
        "map_caption": build_map_caption(derived, rendered, inputs),
        "key_figures": build_key_figures(derived),
        "facts": build_facts_table(derived),
        "unavailable": build_unavailable(derived),
        "boundary_statement": build_boundary_statement(derived),
        "sources": build_sources(inputs, derived),
        "methods": build_methods(inputs, derived),
        "derived": derived,
    }


def build_methods(inputs: od.OverviewInputs, derived: od.OverviewDerived) -> list:
    """The methods note's entries for this section."""
    methods = []
    if derived.landscape:
        methods.append({
            "source": "USGS 3DEP (context grid)",
            "method": (f"Landscape position: Weiss's slope position over a topographic position index -- each cell's "
                       f"elevation minus the mean within {od.LANDSCAPE_TPI_RADIUS_M:.0f} m, standardised by the index's "
                       "spread over the context window. Ridge above +1, upper slope above +0.5, mid slope or bench "
                       f"within 0.5 (bench at or under {od.LANDSCAPE_BENCH_MAX_SLOPE_DEG:.0f} degrees of median slope), "
                       "valley floor below -0.5. Drainage from above: the ground outside the parcel whose D8 flow "
                       "paths enter it. Context contours: the smallest of 10, 20, 40 or 80 ft giving at most "
                       f"{od.CONTEXT_CONTOUR_MAX_LINES} levels across the window, an index every {od.CONTEXT_INDEX_EVERY}."),
            "notes": [],
        })
    methods.append({
        "source": "Site overview",
        "method": ("Buildings: FEMA USA Structures footprints intersecting the boundary; structures under 450 sq ft "
                   "are not inventoried. Transmission: the nearest HIFLD line within five miles of the boundary, "
                   "measured in the parcel's UTM zone; the source's unknown voltages are reported as not recorded. "
                   "Province at the centroid, from the 1:7,000,000 map; its sections are not printed."),
        "notes": [],
    })
    return methods
