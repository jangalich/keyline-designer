"""
access_section.py

THE ACCESS SECTION'S CONTENT (section V), formatted for the page -- the
one place access_derivations' metres and cell counts become the
imperial, rounded, worded values the template sets, plus the section's
map.

    build_access_section(inputs, tokens) -> the section dict

TWO PAGES, AN INVENTORY. Existing access only: frontage, tracks, and what
the ground allows. No proposed road, no route, no corridor, no cost --
where a new road should go is the roads step's job and appears on the
layout map and in the design record, never here; test_access_section.py
greps the section's words for that language. No engineering quantities
either: cost depends on cut and fill, culverts, surfacing and local
rates, and a figure here would be checked against a contractor's quote
and found wrong. The section gives what a contractor prices from.

THE PAGES, top to bottom: the summary line carrying the three figures
worth featuring (frontage, the drivable boundary, the very-limited soil
share) as a sentence; the access map AT LANDFORM'S EXTENT AND SCALE, so
a reader can compare Landform's contours, Water's streams and this map
as pictures of the same land; its legend and caption (the terrain
constraints, in words); the frontage table. Then, on a continuation
page: the soil road-construction table and the sources. Two pages at
the same scale rather than one page at a different one (the author's
decision, phase 2 review). No key-figures panel: three numbers do not
fill a nine-figure grid.

THE MAP'S PLATE. Roads and tracks are culture, in ink: a mapped road a
solid ink line on a page-coloured casing, a track (the part of a mapped
road that lies on the parcel) dashed in the muted ink on the same
casing. Frontage is a soft ink band under the boundary where a road runs
against it. Undrivable boundary is hachured: short ticks inward, in the
terrain token, along the pieces where the ground inside the boundary is
at or above Landform's class D break. Contours are present but set
back: this map is about edges and lines, not terrain.

DRAWING AND MEASURING ARE DIFFERENT JOBS (the author's decision, phase
1). The drivability figures are the stations as measured; for DRAWING,
a run of fewer than DRAWN_RUN_MIN_STATIONS stations is merged into its
neighbours, because a dotted boundary would imply a precision the
sampling does not have. The caption says so. Same split as Landform's
slope tints: compute on cells, draw what reads.

OVERFLOW. The frontage table is merged by road name and the soil table
is condensed by construction (three classes, plus a row only for a
state that is present); the first page holds the map and up to
FRONTAGE_ROWS_MAX road rows beneath it, measured. With frontage on more
roads than that the rule is a SPILL, stated: the frontage table and its
caption move whole to the continuation page above the soil table, and
the map caption says so. Nothing is condensed or truncated silently.

EVERY ACREAGE TABLE SUMS TO THE COVER'S ACREAGE (landform_section.
allocate_exactly). A dash is a true zero; a nonzero value below display
precision reads "<0.1". Mono only for measurements. One caveat per
figure, at the point of use; one source line per source; full citations
and methods under `methods` for the note Site overview will render.
"""

import math
import re
from datetime import date, datetime
from typing import Optional

from shapely.geometry import LineString, MultiLineString, Point, box
from shapely.ops import unary_union

import access_derivations as ad
import report_map
import soil_road_ratings as srr
import water_derivations as wd
from landform_section import (
    ZERO_DASH,
    _feet,
    _one_decimal,
    _one_decimal_or_dash,
    allocate_exactly,
    format_retrieved_on,
)
from report_outline import section_number

SECTION_NAME = "Access"
SECTION_TEMPLATE = "access.html"

METERS_PER_FOOT = report_map.METERS_PER_FOOT

# --- the access map's plate ---------------------------------------------
# LANDFORM'S FRAME, EXTENT AND SCALE: report_map.render_map() fits the
# same frame to the same boundary, so the map is the other sections' map
# of this parcel with different layers on it. test_access_section.py
# measures the scale, the drawn bbox and the frame against Landform's.
ROAD_STROKE_PT = 1.3
ROAD_CASING_PT = 3.0
TRACK_STROKE_PT = 1.1
TRACK_DASH = "4 2.5"
FRONTAGE_BAND_PT = 5.0
FRONTAGE_BAND_OPACITY = 0.22
# Hachures along undrivable boundary: short ticks inward, in the terrain
# token, sized in POINTS so they read the same at any scale (converted to
# ground metres through the frame's projection when drawn).
TICK_SPACING_PT = 3.2
TICK_LENGTH_PT = 3.4
TICK_STROKE_PT = 0.55
CONTOUR_OPACITY = 0.4
# A run of fewer stations than this is merged into its neighbours for drawing.
DRAWN_RUN_MIN_STATIONS = 2
# The first page holds the map and this many road rows beneath it
# (measured on the reference parcel); beyond it the frontage table moves
# whole to the continuation page.
FRONTAGE_ROWS_MAX = 4
# A limiting feature is named in the table when it affects at least this
# share of its class's ground; the rest are in the methods note.
FEATURE_NAME_SHARE = 0.25
FEATURE_NAME_MAX = 6

ROADS_CAVEAT = "The source does not say whether a road is public, and shows no track it never surveyed."
SOIL_SLOPE_CLAUSE = "its slope feature is the survey's slope phase, not the elevation model's grid above"


# ======================================================================
# Formatting
# ======================================================================


def _ft(meters: float) -> str:
    return _feet(meters / METERS_PER_FOOT)


def _pct(value: float) -> str:
    return f"{value:.0f}%"


def _ft_or_dash(meters: float) -> str:
    return ZERO_DASH if meters <= 0 else _ft(meters)


def _lower(name: str) -> str:
    """NRCS's feature name in running text: 'Depth to saturated zone' ->
    'depth to saturated zone'; 'Shrink-swell' stays."""
    return name[:1].lower() + name[1:] if name else name


def _list(words: list) -> str:
    if not words:
        return ""
    if len(words) == 1:
        return words[0]
    return ", ".join(words[:-1]) + " and " + words[-1]


def _side(parcel, geometry) -> Optional[str]:
    """The compass side of the parcel a geometry lies on, in words."""
    if geometry is None or geometry.is_empty:
        return None
    sector = ad.compass_sector(ad._bearing_deg((parcel.centroid.x, parcel.centroid.y), (geometry.centroid.x, geometry.centroid.y)))
    return {"N": "north", "NE": "north-east", "E": "east", "SE": "south-east", "S": "south", "SW": "south-west",
            "W": "west", "NW": "north-west"}[sector]


# ======================================================================
# The map
# ======================================================================


def drawn_runs(boundary: dict, minimum: int = DRAWN_RUN_MIN_STATIONS) -> list:
    """The boundary's runs for DRAWING: a run shorter than `minimum`
    stations takes the state of the longer of its neighbours (the one
    before it when equal), then adjacent equal runs merge. The measured
    runs and lengths are untouched. Returns [{'state', 'start_m', 'end_m'}]."""
    step = boundary["step_m"]
    runs = [{"state": r["state"], "start_m": r["start_m"], "end_m": r["end_m"]} for r in boundary["runs"]]
    changed = True
    while changed and len(runs) > 1:
        changed = False
        for index, run in enumerate(runs):
            stations = (run["end_m"] - run["start_m"]) / step
            if stations + 1e-9 >= minimum:
                continue
            before = runs[index - 1] if index > 0 else None
            after = runs[index + 1] if index + 1 < len(runs) else None
            neighbour = before if after is None or (before is not None and (before["end_m"] - before["start_m"]) >= (after["end_m"] - after["start_m"])) else after
            run["state"] = neighbour["state"]
            changed = True
            break
        merged = []
        for run in runs:
            if merged and merged[-1]["state"] == run["state"]:
                merged[-1]["end_m"] = run["end_m"]
            else:
                merged.append(dict(run))
        runs = merged
    return runs


def _ring_piece(parcel, start_m: float, end_m: float):
    from shapely.ops import substring

    ring = LineString(parcel.exterior.coords)
    piece = substring(ring, start_m, end_m)
    return piece if isinstance(piece, LineString) and piece.length > 0 else None


def hachures(parcel, pieces: list, spacing_m: float, length_m: float):
    """Short ticks inward from the boundary along `pieces` (ring
    LineStrings), one MultiLineString or None."""
    ticks = []
    for piece in pieces:
        if piece is None or piece.length <= 0:
            continue
        d = spacing_m / 2
        while d < piece.length:
            here = piece.interpolate(d)
            ahead = piece.interpolate(min(piece.length, d + 0.5))
            behind = piece.interpolate(max(0.0, d - 0.5))
            dx, dy = ahead.x - behind.x, ahead.y - behind.y
            norm = math.hypot(dx, dy) or 1.0
            nx, ny = -dy / norm, dx / norm
            inward = Point(here.x + nx * length_m, here.y + ny * length_m)
            if not parcel.contains(Point(here.x + nx * 0.5, here.y + ny * 0.5)):
                inward = Point(here.x - nx * length_m, here.y - ny * length_m)
            ticks.append(LineString([(here.x, here.y), (inward.x, inward.y)]))
            d += spacing_m
    return MultiLineString(ticks) if ticks else None


def _clip(geometry, clip):
    if geometry is None:
        return None
    return wd._linear(geometry.intersection(clip))


def build_map_layers(inputs: ad.AccessInputs, derived: ad.AccessDerived, contours: dict) -> list:
    """Drawing order, lightest context first: contours set back, the
    frontage band, the undrivable hachures, road casings, roads (named),
    track casings, tracks."""
    parcel = inputs.boundary_polygon_utm
    visible = box(*report_map.visible_extent_utm(parcel))
    meters_per_unit = report_map._Projection(
        parcel.bounds, report_map.fitted_frame(parcel), report_map.MARGIN_PT, report_map.FURNITURE_BAND_PT
    ).meters_per_unit
    layers = []
    for spec in report_map.contour_layers(contours, legend=["Contours, ", {"value": f"{contours['interval_ft']} ft"}]):
        spec["labels"] = None
        spec["stroke_opacity"] = CONTOUR_OPACITY
        layers.append(spec)
    frontage = derived.frontage["geometry"]
    if frontage is not None:
        layers.append(report_map.layer(
            "frontage", [frontage], kind="line", stroke="ink", stroke_width=FRONTAGE_BAND_PT, stroke_opacity=FRONTAGE_BAND_OPACITY,
            legend=["Frontage, a mapped road within ", {"value": f"{_ft(derived.frontage['tolerance_m'])} ft"}],
        ))
    undrivable = [_ring_piece(parcel, r["start_m"], r["end_m"]) for r in drawn_runs(derived.boundary) if r["state"] == ad.UNDRIVABLE]
    ticks = hachures(parcel, undrivable, TICK_SPACING_PT * meters_per_unit, TICK_LENGTH_PT * meters_per_unit)
    if ticks is not None:
        layers.append(report_map.layer(
            "undrivable", [ticks], kind="line", stroke="terrain", stroke_width=TICK_STROKE_PT,
            legend=["Steep boundary, ", {"value": f"{_pct(derived.boundary['threshold_pct'])}"}, " and over"],
        ))
    roads, labels = [], []
    for road in derived.roads:
        clipped = _clip(road["geometry_utm"], visible)
        if clipped is None:
            continue
        roads.append(clipped)
        labels.append(road["name"] if road["name"] != "Unnamed road" else None)
    if roads:
        layers.append(report_map.layer("road-casing", roads, kind="line", stroke="page", stroke_width=ROAD_CASING_PT))
        layers.append(report_map.layer("roads", roads, kind="line", stroke="ink", stroke_width=ROAD_STROKE_PT, labels=labels,
                                       legend="Mapped roads"))
    tracks = [_clip(t["geometry"], visible) for t in derived.tracks["tracks"]]
    tracks = [t for t in tracks if t is not None]
    if tracks:
        layers.append(report_map.layer("track-casing", tracks, kind="line", stroke="page", stroke_width=ROAD_CASING_PT))
        layers.append(report_map.layer("tracks", tracks, kind="line", stroke="ink-muted", stroke_width=TRACK_STROKE_PT, dash=TRACK_DASH,
                                       legend="Track, a mapped road on the parcel"))
    return layers


def empty_map_note(derived: ad.AccessDerived, parcel) -> Optional[dict]:
    """The quiet statement on a map with no road in the frame."""
    visible = box(*report_map.visible_extent_utm(parcel))
    if any(road["geometry_utm"].intersects(visible) for road in derived.roads):
        return None
    if derived.roads:
        nearest = derived.frontage["nearest"]
        lines = ["No mapped road within the frame;", f"the nearest lies {_ft(nearest['distance_m'])} ft to the {nearest['sector']}."] if nearest \
            else ["No mapped road within the frame."]
    else:
        lines = ["No mapped road within", f"{_ft(wd.ADJACENCY_BUFFER_METERS)} ft of the boundary."]
    return {"lines": lines, "point": parcel.representative_point()}


# ======================================================================
# Words
# ======================================================================


def build_summary(derived: ad.AccessDerived, parcel) -> list:
    frontage = derived.frontage
    boundary = derived.boundary
    perimeter = boundary["perimeter_m"]
    parts = []
    if frontage["roads"]:
        sides = []
        for road in frontage["roads"][:2]:
            side = _side(parcel, road["geometry"])
            name = "an unnamed road" if road["name"] == "Unnamed road" else road["name"]
            sides.append(f"{name} on the {side}" if side else name)
        parts += ["Mapped roads front ", {"value": f"{_ft(frontage['total_m'])} ft"}, " of the ", {"value": f"{_ft(perimeter)} ft"},
                  f" boundary, {_list(sides)}. "]
    elif frontage["nearest"]:
        nearest = frontage["nearest"]
        parts += ["No mapped road touches the boundary: the nearest, ", nearest["name"], ", lies ",
                  {"value": f"{_ft(nearest['distance_m'])} ft"}, f" to the {nearest['sector']}. "]
    else:
        parts += ["No mapped public road lies within ", {"value": f"{_ft(wd.ADJACENCY_BUFFER_METERS)} ft"}, " of the boundary. "]
    drivable = boundary["lengths_m"][ad.DRIVABLE]
    parts += [{"value": f"{_ft(drivable)} ft"}, " of the boundary is under ", {"value": _pct(boundary["threshold_pct"])}, " slope"]
    if frontage["roads"]:
        on_frontage = boundary["frontage_lengths_m"][ad.DRIVABLE]
        parts += [", ", {"value": f"{_ft(on_frontage)} ft"}, " of it on that frontage"]
    steep_sides = [e for e in boundary["edges"] if e["undrivable_share"] is not None and e["undrivable_share"] >= 0.75 and e["length_m"] > 20]
    if steep_sides:
        names = []
        for edge in steep_sides:
            side = {"N": "north", "NE": "north-east", "E": "east", "SE": "south-east", "S": "south", "SW": "south-west", "W": "west",
                    "NW": "north-west"}[ad.compass_sector(edge["midpoint_bearing_deg"])]
            if side not in names:
                names.append(side)
        if len(names) >= 3:
            parts += [f"; the edges from {names[0]} round to {names[-1]} are steeper. "]
        else:
            parts += [f"; the {_list(names)} {'edges are' if len(names) > 1 else 'edge is'} steeper. "]
    else:
        parts += [". "]
    soil = derived.soil
    if soil["fetched"]:
        cells = derived.cells
        very = soil["counts"][srr.VERY_LIMITED] / cells["on_parcel_count"] * 100.0 if cells["on_parcel_count"] else 0.0
        parts += ["Soil rated very limited for a local road covers ", {"value": f"{very:.0f}%"}, " of the parcel."]
    return parts


def build_map_caption(derived: ad.AccessDerived) -> list:
    boundary = derived.boundary
    parts = ["Steep boundary: slope ", {"value": f"{_ft(boundary['inset_m'])} ft"}, " inside the line of ",
             {"value": _pct(boundary["threshold_pct"])}, " or more; runs under two samples are merged for drawing only. "]
    tracks = derived.tracks["tracks"]
    if tracks:
        track = tracks[0]
        profile = track["profile"]
        entries = [s for s in track["entry_slopes_pct"] if s is not None]
        parts += ["The mapped lane climbs " if len(tracks) == 1 else "The longest mapped lane climbs ",
                  {"value": _pct(profile["end_to_end_grade_pct"])}, " over ", {"value": f"{_ft(track['length_m'])} ft"},
                  ", ", {"value": _pct(profile["max_grade_pct"])}, " at the steepest step"]
        if entries:
            low, high = min(entries), max(entries)
            parts += [", entering on ", {"value": _pct(low) if round(low) == round(high) else f"{low:.0f}–{high:.0f}%"}, " ground"]
        parts += ["; "]
    else:
        parts += ["No mapped lane lies on the parcel; "]
    crossings = derived.crossings
    if not crossings["streams_on_parcel"]:
        parts += ["no stream crosses the parcel."]
    elif crossings["crossings_needed"] == 0:
        parts += ["a stream crosses the parcel, but every part of it touches the frontage side."]
    else:
        acres = crossings["beyond_crossing_m2"] / wd.SQUARE_METERS_PER_ACRE
        parts += [{"value": _one_decimal(acres)}, " acres lie across a mapped stream from every frontage."]
    frontage = derived.frontage
    if len(frontage["roads"]) > FRONTAGE_ROWS_MAX:
        parts += [f" Frontage on {len(frontage['roads'])} roads: the frontage table is on the next page."]
    return parts


# ======================================================================
# Tables
# ======================================================================


def _drivable_frontage_m(road: dict, boundary: dict) -> float:
    """The road's frontage that is drivable: its intervals along the ring
    against the drivable runs', exact."""
    drivable = [(run["start_m"], run["end_m"]) for run in boundary["runs"] if run["state"] == ad.DRIVABLE]
    return ad.intervals_overlap_m(road["intervals"], drivable)


def build_frontage_table(derived: ad.AccessDerived) -> Optional[dict]:
    """One row per road with frontage, longest first, every road listed
    (the section spills to a second page rather than condense). No
    total row: the summary line carries both totals. None without
    frontage."""
    frontage = derived.frontage
    if not frontage["roads"]:
        return None
    boundary = derived.boundary
    rows = []
    for road in frontage["roads"]:
        label = road["name"] if road["class"] is None else f"{road['name']}, {road['class'].lower()}"
        if road["route"]:
            label += f" {road['route']}"
        rows.append({"label": label, "cells": [_ft(road["length_m"]), _ft_or_dash(_drivable_frontage_m(road, boundary))]})
    return {"corner": "Frontage", "columns": ["Along the boundary, ft", f"Under {_pct(boundary['threshold_pct'])}, ft"], "rows": rows,
            "compact": True, "spill": len(rows) > FRONTAGE_ROWS_MAX}


def build_frontage_caption(derived: ad.AccessDerived, table: Optional[dict]) -> list:
    frontage = derived.frontage
    parts = []
    if table is None:
        if frontage["nearest"]:
            parts += ["No mapped road runs within ", {"value": f"{_ft(frontage['tolerance_m'])} ft"}, " of the boundary. "]
        else:
            parts += ["No mapped road lies within ", {"value": f"{_ft(wd.ADJACENCY_BUFFER_METERS)} ft"}, " of the boundary. "]
    else:
        parts += ["Frontage is the boundary within ", {"value": f"{_ft(frontage['tolerance_m'])} ft"},
                  " of a mapped road; the second column is its length under ", {"value": _pct(derived.boundary["threshold_pct"])}, ". "]
        if table["spill"]:
            parts += [f"Frontage on more than {FRONTAGE_ROWS_MAX} roads, so the table is on this page rather than under the map. "]
    tracks = derived.tracks["tracks"]
    unnamed = [r for r in frontage["roads"] if r["name"] == "Unnamed road"]
    if unnamed and any(t["name"] == "Unnamed road" for t in tracks):
        parts.append("The unnamed road is both frontage and track and may be a private lane: ")
    elif unnamed:
        parts.append("The unnamed road may be a private lane: ")
    parts.append(ROADS_CAVEAT if not unnamed else ROADS_CAVEAT[:1].lower() + ROADS_CAVEAT[1:])
    return parts


def named_features(features: dict, class_cells: int, share: float = FEATURE_NAME_SHARE, limit: int = FEATURE_NAME_MAX) -> list:
    """The features affecting at least `share` of the class's ground, most
    ground first, at most `limit`."""
    if class_cells <= 0:
        return []
    picked = [name for name, cells in features.items() if cells / class_cells >= share]
    return picked[:limit]


def build_soil_table(derived: ad.AccessDerived) -> Optional[dict]:
    """Three class rows, then a row for each state that is present (not
    rated, no data, not surveyed), then the total; acres, the share, and
    the limiting features named. None when the layer did not answer."""
    soil = derived.soil
    if not soil["fetched"]:
        return None
    cells = derived.cells
    counts = soil["counts"]
    names = list(srr.LIMITATION_CLASSES) + [n for n in (srr.NOT_RATED, ad.SOIL_NO_DATA, wd.HYDRIC_NO_POLYGON) if counts.get(n)]
    acres = allocate_exactly([counts[n] for n in names], cells["on_parcel_count"] * cells["cell_acres"], 1)
    shares = allocate_exactly([counts[n] for n in names], 100.0, 1)
    rows = []
    for name, acre, share in zip(names, acres, shares):
        if name in srr.LIMITATION_CLASSES:
            features = named_features(soil["features"][name], counts[name])
            text = _list([_lower(f) for f in features]) if features else ZERO_DASH
        else:
            text = {srr.NOT_RATED: "not rated by the survey", ad.SOIL_NO_DATA: "no rating returned",
                    wd.HYDRIC_NO_POLYGON: "no survey polygon"}[name]
        label = {wd.HYDRIC_NO_POLYGON: "Not surveyed", ad.SOIL_NO_DATA: "No data"}.get(name, name)
        rows.append({"label": label, "cells": [_one_decimal_or_dash(acre, counts[name]), _one_decimal_or_dash(share, counts[name]),
                                               {"value": text, "kind": "text"}]})
    rows.append({"label": "Total", "cells": [_one_decimal(round(cells["on_parcel_count"] * cells["cell_acres"], 1)), _one_decimal(100.0),
                                             {"value": "", "kind": "text"}]})
    return {"corner": "Local road rating", "columns": ["Acres", "% of parcel", "Limiting features"], "rows": rows,
            "text_columns": ["Limiting features"], "acres": acres, "shares": shares, "classes": names}


def build_soil_caption(derived: ad.AccessDerived) -> list:
    soil = derived.soil
    parts = ["NRCS's rating by dominant components; " + SOIL_SLOPE_CLAUSE + ". "]
    cells = derived.cells
    wet = [u for u in soil["map_units"].values() if u["paved"] and u["paved"]["class"] == srr.VERY_LIMITED
           and u["features"].get("Depth to saturated zone", 0.0) >= 0.99]
    if wet:
        from water_section import map_unit_short_name

        names = _list(sorted({map_unit_short_name(u["muname"]) for u in wet}))
        parts += [f"{names} {'is' if len(wet) == 1 else 'are'} very limited partly for a shallow water table, as the Water "
                  "section shows; "]
    fill = soil["roadfill_counts"]
    total = cells["on_parcel_count"]
    if total and fill.get("Poor", 0) == total:
        parts.append(("roadfill" if wet else "Roadfill") + " is poor on every map unit.")
    elif total and fill.get("Poor", 0):
        parts += [("roadfill" if wet else "Roadfill") + " is poor on ", {"value": _one_decimal(fill["Poor"] * cells["cell_acres"])}, " acres."]
    if soil["unpaved_differs"]:
        parts.append(f" The unpaved-road rating differs on {len(soil['unpaved_differs'])} map unit(s).")
    return parts


def build_soil_unavailable(inputs: ad.AccessInputs) -> list:
    reason = inputs.unavailable.get("soil_road_ratings", {}).get("reason")
    if reason == "no_data_for_parcel":
        return ["SSURGO carries no road-construction rating for this parcel's map units."]
    return ["SSURGO's road-construction ratings did not answer when this report was generated; the soil table is not reported. "
            "Look the map units up in Web Soil Survey, Local Roads and Streets."]


# ======================================================================
# Sources and methods
# ======================================================================


def _road_vintage(roads: list) -> Optional[str]:
    years = set()
    for road in roads:
        text = str(road["properties"].get("source_datadesc") or "")
        match = re.search(r"(19|20)\d{2}", text)
        if match:
            years.add(match.group(0))
    if not years:
        return None
    return min(years) if len(years) == 1 else f"{min(years)}–{max(years)}"


def build_sources(inputs: ad.AccessInputs, derived: ad.AccessDerived) -> list:
    retrieved = format_retrieved_on(inputs.retrieved_on)
    vintage = _road_vintage(derived.roads)
    lines = [[f"USGS National Map transportation, local road layers, from Census TIGER/Line"
              + (f" of {vintage}" if vintage else "") + f", retrieved {retrieved}."]]
    if inputs.soil_road_ratings is not None:
        surveys = derived.soil["survey_areas"]
        if surveys:
            version = (surveys[0].get("saverest") or "").split(" ")[0]
            lines.append([f"USDA NRCS SSURGO, soil survey {surveys[0]['areasymbol']}, version of {version}: cointerp, local roads "
                          f"and streets, roadfill."])
        else:
            lines.append(["USDA NRCS SSURGO: cointerp, local roads and streets, roadfill."])
    lines.append([f"USGS 3DEP elevation, 1/3 arc-second, resampled to 5 m, retrieved {retrieved}."])
    return lines


def build_methods(inputs: ad.AccessInputs, derived: ad.AccessDerived) -> list:
    retrieved = format_retrieved_on(inputs.retrieved_on)
    tolerance = derived.frontage["tolerance_m"]
    boundary = derived.boundary
    return [
        {"source": "USGS National Map transportation",
         "identifier": "Transportation map service layers 30, 31 and 32 (secondary highways, local connecting roads, local roads), bbox + 150 m",
         "period": retrieved,
         "citation": "U.S. Geological Survey, The National Map, transportation theme, from U.S. Census Bureau TIGER/Line data, "
                     "served by the National Map transportation map service.",
         "terms": "U.S. federal work; USGS states its data are in the public domain.",
         "method": f"Frontage is the boundary ring within {tolerance:.0f} m of a road centreline, merged by road name. THE TOLERANCE IS A "
                   f"JUDGMENT: TIGER states a positional accuracy of about 7.6 m and a township road's right-of-way is 33 to 60 ft, "
                   f"so 15 m sits just above the source's error and below a half right-of-way plus that error; a road the tolerance "
                   f"decides is reported as such. Public status and surface are not attributes of the source. A track is the part of "
                   f"a mapped segment on the parcel; its grade is the DEM sampled every {ad.TRACK_SAMPLE_STEP_M:.0f} m along it, a "
                   f"final step shorter than a station folded into the one before. The same rows, buffered by "
                   f"{ad.ROAD_EXCLUSION_BUFFER_METERS:.0f} m, are the design step's existing-road exclusion.",
         "notes": [
             f"Boundary drivability: the exclusion result's slope grid sampled {boundary['inset_m']:.0f} m inside the ring every "
             f"{boundary['step_m']:.0f} m; a station at or above {boundary['threshold_pct']:.0f}% (Landform's class D break) is undrivable. "
             f"Runs of stations partition the perimeter exactly; for drawing, runs shorter than {DRAWN_RUN_MIN_STATIONS} stations are "
             "merged into their neighbours.",
             "Stream crossings: the parcel split by its on-parcel NHD flowlines; a piece that touches no frontage lies beyond a crossing.",
         ]},
        {"source": "USDA NRCS SSURGO", "identifier": "cointerp joined to component over the parcel's map units; ENG - Local Roads and Streets, "
                                                       "ENG - Unpaved Local Roads and Streets, ENG - Construction Materials; Roadfill",
         "period": retrieved, "citation": srr.SSURGO_CITATION, "terms": "U.S. federal work; public domain.",
         "method": "A map unit's class is NRCS's dominant condition: component percentages summed by class over every rated component, "
                   "the largest total winning and a tie going to the more limiting class. Limiting features are the depth-1 rules of the "
                   "components in the winning class with a nonzero degree, weighted by the component's share of the class and the unit's "
                   f"cells on the parcel; the table names those affecting at least {FEATURE_NAME_SHARE:.0%} of the class's ground. The "
                   "rating's slope feature is the survey's slope phase, independent of the elevation model. Acres by the same map-unit "
                   "cell grid the Water section uses, allocated exactly to the parcel's area."},
        {"source": "USGS 3DEP", "identifier": f"3DEP 1/3 arc-second DEM, resampled to {max(inputs.dem['resolution_meters']):.0f} m, {inputs.dem['crs']}",
         "period": retrieved, "citation": "U.S. Geological Survey, 3D Elevation Program seamless DEM, served by The National Map elevation service.",
         "terms": "U.S. federal work; public domain.", "method": "Contours at Landform's interval; the slope grid is production_area."
                                                                 "compute_slope_percent's, the exclusion result's own."},
    ]


# ======================================================================
# The section
# ======================================================================


def build_access_section(inputs: ad.AccessInputs, tokens: Optional[dict] = None) -> dict:
    """The section dict access.html sets. `tokens` defaults to site_report.TOKENS."""
    if tokens is None:
        import site_report

        tokens = site_report.TOKENS
    derived = ad.derive(inputs)
    parcel = inputs.boundary_polygon_utm
    contours = report_map.parcel_contours(inputs.dem, parcel)
    rendered = report_map.render_map(parcel, build_map_layers(inputs, derived, contours), tokens, note=empty_map_note(derived, parcel))
    frontage_table = build_frontage_table(derived)
    spill = bool(frontage_table and frontage_table["spill"])
    return {
        "number": section_number(SECTION_NAME),
        "name": SECTION_NAME,
        "template": SECTION_TEMPLATE,
        "heading": SECTION_NAME,
        "summary": build_summary(derived, parcel),
        "map": rendered,
        "map_caption": build_map_caption(derived),
        "frontage_table": frontage_table,
        "frontage_caption": build_frontage_caption(derived, frontage_table),
        # THE SPILL RULE: two pages when the frontage table would not fit one (access.html splits on it).
        "spill": spill,
        "soil_table": build_soil_table(derived),
        "soil_caption": build_soil_caption(derived),
        "soil_unavailable": build_soil_unavailable(inputs),
        "sources": build_sources(inputs, derived),
        "methods": build_methods(inputs, derived),
        "derived": derived,
        "contours": {k: v for k, v in contours.items() if k != "levels"},
    }
