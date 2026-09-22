"""
water_section.py

THE WATER & HYDROLOGY SECTION'S CONTENT (section IV), formatted for the
page -- the one place water_derivations' metres, centimetres and cell
counts become the imperial, rounded, worded values the template sets,
plus the section's two maps.

    build_water_section(inputs, tokens, flow=None) -> the section dict

THIS SECTION DESCRIBES THE WATER THAT IS THERE. Not where anything should
be built: no survey areas, no siting, no storage, no pond language --
those belong to the design step and the layout map. test_water_section.py
greps the section's words for that language, as Landform's test greps
for water. And no engineering quantities: no runoff depth, no curve
numbers, no volumes. The section reports the inputs a consultant would
use and lets them compute; the water standards document's refusal to
compute them is deliberate.

THREE PAGES ON LANDFORM'S RHYTHM. The hydrology map: streams (water,
solid, weight by order, intermittent dashed), waterbodies (water tint
with a firmer edge), wetlands (the marsh convention: short horizontal
tufts, not a flat fill), the 1%-annual-chance flood zone (a light
hatch, the lightest treatment on the map), flow paths (very light,
context only), over contours set lighter than the terrain map's; the
summary line above it, the surface-water table beneath. The wetness
map: a graduated water-blue tint of the wetness index at the pipeline's
own breakpoints, with the depressions the flow model filled; then the
seasonal water table as a twelve-column table with flooding and
ponding as further rows. The numbers: nine key figures, the wet-ground
comparison, catchment and land cover, flood, the sources footer.

THE MAPS ARE AT LANDFORM'S EXTENT AND SCALE -- render_map() fits the
frame to the boundary, so the same parcel gives the same projection --
and context beyond the parcel is clipped to what the frame shows
(report_map.visible_extent_utm), never to the parcel: a stream 250 ft
off the boundary is on the map when the frame reaches it.

THE MARSH AND THE HATCH ARE GEOMETRY, NOT SVG PATTERNS. The tufts are
short horizontal lines on a staggered grid inside each wetland polygon;
the hatch is diagonal lines at a fixed ground spacing clipped to the
zone. Both go through the renderer's ordinary line layer, so they carry
token colours, print as vectors and need nothing the renderer does not
already do.

THE WATER TABLE'S THREE STATES ARE SET DIFFERENTLY (the author's rule).
A depth is a measurement, in the data face. "Deeper than N" is a bound,
not a depth: it is prefixed and set muted (cell kind "bound") so a
column of depths cannot be read across it. "No data" is a word, in the
prose face (cell kind "word"). The table's caption says what each means.

EVERY ACREAGE TABLE SUMS TO THE COVER'S ACREAGE (landform_section.
allocate_exactly over the cell partitions water_derivations builds). A
dash is a true zero; a nonzero value below display precision reads
"<0.1". Mono only for measurements.

ONE CAVEAT PER FIGURE, AT THE POINT OF USE; one source line per source
in the footer; full citations and methods under `methods` for the note
Site overview will render.
"""

import math
import re
from typing import Optional

import contourpy
import numpy as np
from shapely.geometry import LineString, MultiLineString, box
from shapely.ops import unary_union

import landform_derivations
import nfhl_data
import nhdplus_data
import nlcd_landcover_data
import nwi_data
import report_map
import soil_water_table
import water_derivations as wd
from contour_lines import _grid_axes
from climate_section import MONTH_INITIALS, MONTH_NAMES
from landform_section import (
    ZERO_DASH,
    _feet,
    _one_decimal,
    _one_decimal_or_dash,
    _polygonal,
    allocate_exactly,
    format_retrieved_on,
)
from raster_grid import cell_union_footprint
from report_outline import section_number

SECTION_NAME = "Water & hydrology"
SECTION_TEMPLATE = "water.html"

METERS_PER_FOOT = report_map.METERS_PER_FOOT
SQUARE_METERS_PER_ACRE = 4046.8564224
INCHES_PER_CM = 1 / 2.54

# --- the hydrology map's plate ------------------------------------------
# Streams: water, solid, weight by Strahler order where NHDPlus HR carries
# it (the weight for an order the table does not name is the last one);
# intermittent dashed, the topographic convention.
STREAM_STROKE_BY_ORDER_PT = {1: 0.7, 2: 1.0, 3: 1.35, 4: 1.7}
STREAM_STROKE_UNKNOWN_PT = 0.85
INTERMITTENT_DASH = "3 2"
WATERBODY_FILL_OPACITY = 0.28
WATERBODY_STROKE_PT = 0.8
# The marsh convention: short horizontal tufts on a staggered grid.
TUFT_SPACING_M = 9.0
TUFT_LENGTH_M = 5.0
TUFT_STROKE_PT = 0.7
# The flood zone: diagonal hatch, the lightest treatment on the map.
HATCH_SPACING_M = 12.0
HATCH_STROKE_PT = 0.4
HATCH_OPACITY = 0.55
# Flow paths: context only.
FLOW_PATH_STROKE_PT = 0.5
FLOW_PATH_OPACITY = 0.4
# Contours under both maps: the terrain map's weights, set back.
CONTOUR_OPACITY = 0.55
SPRING_STROKE_PT = 0.9

# --- the wetness map's ramp ---------------------------------------------
# Four classes of raw TWI on the water token: below the water step's
# floor breakpoint, two steps between the floor and the full-credit
# breakpoint, and at or above it -- wet ground by terrain. Light to dark,
# the darkest capped so a contour still reads over it.
WETNESS_TINT_OPACITY = (0.10, 0.22, 0.36, 0.52)
DEPRESSION_FILL_OPACITY = 0.85

# --- captions and statements --------------------------------------------
NHD_CAVEAT = ("NHD streams are compiled at 1:24,000 and can sit 100–300 ft from the channel on the ground; seeps and "
              "springs are not reliably mapped, and any on the ground need field verification.")
NWI_CAVEAT = ("Wetlands are mapped from imagery interpretation, not field-delineated, and are not a jurisdictional "
              "determination.")
FEMA_CAVEAT = ("Not mapped is not not at risk: large parts of rural America have no detailed flood study, and Zone X "
               "means only that no 1%-annual-chance floodplain has been mapped there.")
WATER_TABLE_CAVEAT = ("Survey-scale mapping, from the same 1:24,000 product as the soils section. A depth is the top of "
                      "the wettest layer NRCS describes, weighted over a map unit's major soils;")
TWI_CAVEAT = "The wetness index is terrain only: it does not know soil or cover, and a flat, well-drained bench scores wet."
CATCHMENT_TRUNCATION = ("The contributing area is measured within the elevation model's window, the parcel plus 100 m, "
                        "and its watershed reaches the window's edge, so it is a lower bound.")


# ======================================================================
# Formatting
# ======================================================================


def _ft(meters: float) -> str:
    return _feet(meters / METERS_PER_FOOT)


def _acres(cells: int, cell_acres: float) -> float:
    return cells * cell_acres


def _whole_or_dash(value: float) -> str:
    return ZERO_DASH if round(value) == 0 and value == 0 else f"{round(value):,}"


def _bound(inches: float) -> dict:
    return {"value": f">{round(inches):,}", "kind": "bound"}


def _word(text: str) -> dict:
    return {"value": text, "kind": "word"}


def map_unit_short_name(muname: Optional[str]) -> str:
    """'Gilpin, Weikert, Culleoka channery silt loams and 25 to 80 percent
    slopes' -> 'Gilpin, Weikert, Culleoka'; 'Rayne silt loam, Conemaugh
    geology, 8 to 15 percent slopes' -> 'Rayne'."""
    if not muname:
        return "Unnamed map unit"
    head = re.split(r"\s+(?:silt|loam|loams|channery|sandy|clay|fine|gravelly|stony|shaly|cobbly|loamy|very|extremely|"
                    r"complex|muck|peat|soils?|\d)", muname, maxsplit=1)[0]
    return head.strip(" ,") or muname


def _permanence_word(label: str) -> str:
    return {"perennial": "perennial", "intermittent": "intermittent", "ephemeral": "ephemeral"}.get(label, "of unknown permanence")


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# ======================================================================
# Map geometry: tufts and hatch
# ======================================================================


def marsh_tufts(polygon, spacing_m: float = TUFT_SPACING_M, length_m: float = TUFT_LENGTH_M):
    """Short horizontal tufts on a staggered grid inside the polygon, as
    one MultiLineString (None when the polygon holds no whole tuft)."""
    if polygon is None or polygon.is_empty:
        return None
    minx, miny, maxx, maxy = polygon.bounds
    lines = []
    row = 0
    y = miny + spacing_m / 2
    while y < maxy:
        x = minx + (spacing_m / 2 if row % 2 else 0.0)
        while x < maxx:
            tuft = LineString([(x - length_m / 2, y), (x + length_m / 2, y)])
            if polygon.contains(tuft):
                lines.append(tuft)
            x += spacing_m
        y += spacing_m
        row += 1
    return MultiLineString(lines) if lines else None


def hatch_lines(polygon, spacing_m: float = HATCH_SPACING_M):
    """Diagonal (45°) hatch lines at a fixed ground spacing, clipped to the
    polygon, as one MultiLineString (None when nothing is inside)."""
    if polygon is None or polygon.is_empty:
        return None
    minx, miny, maxx, maxy = polygon.bounds
    span = (maxx - minx) + (maxy - miny)
    lines = []
    offset = -(maxy - miny)
    while offset < (maxx - minx):
        start = (minx + offset, miny)
        end = (minx + offset + span, miny + span)
        clipped = LineString([start, end]).intersection(polygon)
        parts = [clipped] if isinstance(clipped, LineString) else [g for g in getattr(clipped, "geoms", []) if isinstance(g, LineString)]
        lines.extend(p for p in parts if not p.is_empty)
        offset += spacing_m * math.sqrt(2)
    return MultiLineString(lines) if lines else None


def _clip_lines(geometry, clip):
    if geometry is None:
        return None
    return wd._linear(geometry.intersection(clip))


def _clip_polygon(geometry, clip):
    if geometry is None:
        return None
    return _polygonal(geometry.intersection(clip))


# ======================================================================
# The hydrology map
# ======================================================================


def _stream_stroke(order: Optional[int]) -> float:
    if order is None:
        return STREAM_STROKE_UNKNOWN_PT
    return STREAM_STROKE_BY_ORDER_PT.get(int(order), STREAM_STROKE_BY_ORDER_PT[max(STREAM_STROKE_BY_ORDER_PT)])


def build_hydrology_layers(inputs: wd.WaterInputs, derived: wd.WaterDerived, contours: dict) -> list:
    """Drawing order, lightest context first: contours, the flood hatch,
    wetland tufts, waterbodies, flow paths, streams (dashed intermittent),
    springs."""
    visible = box(*report_map.visible_extent_utm(inputs.boundary_polygon_utm))
    layers = []
    for spec in report_map.contour_layers(contours, legend=["Contours, ", {"value": f"{contours['interval_ft']} ft"}]):
        spec["labels"] = None
        spec["stroke_opacity"] = CONTOUR_OPACITY
        layers.append(spec)
    # The 1%-annual-chance flood zone(s): Special Flood Hazard Areas only.
    # Zone X is "minimal hazard" and is stated in the table, not hatched.
    sfha = [z["geometry_utm"] for z in derived.flood["zones"] if z["sfha"] and z["geometry_utm"] is not None]
    if sfha:
        hatch = hatch_lines(_clip_polygon(unary_union(sfha), visible))
        if hatch is not None:
            layers.append(report_map.layer(
                "flood-zone", [hatch], kind="line", stroke="water", stroke_width=HATCH_STROKE_PT,
                stroke_opacity=HATCH_OPACITY, legend="FEMA flood zone, 1% annual chance",
            ))
    wetlands = [f["geometry_utm"] for f in derived.wetlands["features"] if f["geometry_utm"] is not None]
    if wetlands:
        tufts = marsh_tufts(_clip_polygon(unary_union(wetlands), visible))
        if tufts is not None:
            layers.append(report_map.layer(
                "wetlands", [tufts], kind="line", stroke="water", stroke_width=TUFT_STROKE_PT, legend="Wetlands, NWI",
            ))
    waterbodies = [_clip_polygon(w["geometry_utm"], visible) for w in derived.surface_water["waterbodies"]]
    waterbodies = [w for w in waterbodies if w is not None]
    if waterbodies:
        layers.append(report_map.layer(
            "waterbodies", waterbodies, kind="polygon", fill="water", fill_opacity=WATERBODY_FILL_OPACITY,
            stroke="water", stroke_width=WATERBODY_STROKE_PT, legend="Waterbodies",
        ))
    paths = [p["geometry"] for p in derived.drainage["flow_paths"]]
    if paths:
        layers.append(report_map.layer(
            "flow-paths", paths, kind="line", stroke="water", stroke_width=FLOW_PATH_STROKE_PT,
            stroke_opacity=FLOW_PATH_OPACITY, legend="Flow paths, from the elevation model",
        ))
    for permanence, dash, legend in (("perennial", None, "Perennial streams"), ("intermittent", INTERMITTENT_DASH, "Intermittent streams"),
                                     ("ephemeral", INTERMITTENT_DASH, "Ephemeral streams"), ("unknown", None, "Streams")):
        rows = [s for s in derived.surface_water["streams"] if s["permanence"] == permanence]
        geometries, labels, widths = [], [], []
        for stream in rows:
            clipped = _clip_lines(stream["geometry_utm"], visible)
            if clipped is None:
                continue
            geometries.append(clipped)
            labels.append(stream["name"] if stream["name"] and stream["name"] != "Unnamed stream" else None)
            widths.append(_stream_stroke(stream["stream_order"]))
        if not geometries:
            continue
        # One layer per distinct weight within the permanence, so order shows in the line.
        for width in sorted(set(widths)):
            picked = [i for i, w in enumerate(widths) if w == width]
            layers.append(report_map.layer(
                f"streams-{permanence}-{width:g}", [geometries[i] for i in picked], kind="line", stroke="water",
                stroke_width=width, dash=dash, labels=[labels[i] for i in picked],
                legend=legend if width == min(set(widths)) else None,
            ))
    springs = [s["point_utm"] for s in (derived.surface_water["springs"] or []) if visible.contains(s["point_utm"])]
    if springs:
        layers.append(report_map.layer(
            "springs", springs, kind="point", stroke="water", stroke_width=SPRING_STROKE_PT, marker="dot",
            legend="Springs and seeps, NHD",
        ))
    return layers


# ======================================================================
# The wetness map
# ======================================================================


def wetness_breaks(wetness: dict) -> list:
    """The four classes' lower bounds: -inf, the floor, the midpoint, the
    threshold (the water step's full-credit breakpoint)."""
    floor, threshold = wetness["breakpoints"]["floor"], wetness["threshold"]
    if floor is None or threshold is None:
        return []
    return [float("-inf"), floor, (floor + threshold) / 2.0, threshold]


def wetness_class_geometries(inputs: wd.WaterInputs, wetness: dict) -> list:
    """[(lower, upper, geometry)] -- filled contours of raw TWI between the
    breaks, clipped to the boundary, for the classes with any on-parcel
    cell; the tables never read these."""
    breaks = wetness_breaks(wetness)
    if not breaks:
        return []
    twi = np.ma.masked_invalid(wetness["twi_raw"].astype(float))
    if twi.count() == 0:
        return []
    x, y = _grid_axes(inputs.dem)
    generator = contourpy.contour_generator(x=x, y=y, z=twi, fill_type=contourpy.FillType.OuterOffset)
    low_edge = float(twi.min()) - 1.0
    top = float(twi.max()) + 1.0
    out = []
    from landform_section import _filled_polygons

    for i, lower in enumerate(breaks):
        upper = breaks[i + 1] if i + 1 < len(breaks) else top
        polygons = _filled_polygons(*generator.filled(low_edge if lower == float("-inf") else lower, upper))
        if not polygons:
            continue
        clipped = _polygonal(unary_union(polygons).intersection(inputs.boundary_polygon_utm))
        if clipped is not None:
            out.append((lower, upper if i + 1 < len(breaks) else None, clipped))
    return out


def build_wetness_layers(inputs: wd.WaterInputs, derived: wd.WaterDerived, contours: dict) -> list:
    """Tints under the depressions under lighter contours; the legend
    names the TWI value of every break."""
    layers = []
    classes = wetness_class_geometries(inputs, derived.wetness)
    breaks = wetness_breaks(derived.wetness)
    for index, (lower, upper, geometry) in enumerate(classes):
        position = breaks.index(lower)
        if position == 0:
            legend = ["Wetness index below ", {"value": f"{breaks[1]:.1f}"}]
        elif position == len(breaks) - 1:
            legend = [{"value": f"{lower:.1f}"}, " and above: wet ground by terrain"]
        else:
            legend = [{"value": f"{lower:.1f}"}, "–", {"value": f"{upper:.1f}"}]
        layers.append(report_map.layer(
            f"wetness-{position}", [geometry], kind="polygon", fill="water", fill_opacity=WETNESS_TINT_OPACITY[position],
            stroke=None, legend=legend,
        ))
    depressions = derived.wetness["depression_mask"]
    if depressions.any():
        footprint = cell_union_footprint(inputs.dem, depressions)
        if footprint is not None and not footprint.is_empty:
            layers.append(report_map.layer(
                "depressions", [footprint], kind="polygon", fill="water", fill_opacity=DEPRESSION_FILL_OPACITY, stroke=None,
                legend="Depressions the flow model filled",
            ))
    for spec in report_map.contour_layers(contours, legend=["Contours, ", {"value": f"{contours['interval_ft']} ft"}]):
        spec["labels"] = None
        spec["stroke_opacity"] = CONTOUR_OPACITY
        layers.append(spec)
    return layers


# ======================================================================
# Words
# ======================================================================


def _stream_sentence(surface: dict) -> list:
    streams = [s for s in surface["streams"] if s["length_in_window_m"] > 0]
    if not streams:
        return ["No mapped stream lies within ", {"value": f"{_ft(wd.ADJACENCY_BUFFER_METERS)} ft"}, " of the boundary."]
    on_parcel = [s for s in streams if s["length_on_parcel_m"] > 0]
    if on_parcel:
        longest = max(on_parcel, key=lambda s: s["length_on_parcel_m"])
        parts = [longest["name"] or "An unnamed stream", f", {_permanence_word(longest['permanence'])}"]
        if longest["stream_order"] is not None:
            parts += [" and order ", {"value": str(longest["stream_order"])}]
        parts += [", crosses the parcel for ", {"value": f"{_ft(longest['length_on_parcel_m'])} ft"}, "."]
        return parts
    nearest = min(streams, key=lambda s: s["distance_m"])
    parts = ["No mapped stream crosses the parcel; ", nearest["name"] or "an unnamed stream", f", {_permanence_word(nearest['permanence'])}"]
    if nearest["stream_order"] is not None:
        parts += [" and order ", {"value": str(nearest["stream_order"])}]
    parts += [", runs ", {"value": f"{_ft(nearest['distance_m'])} ft"}, " beyond the boundary."]
    return parts


def build_summary(derived: wd.WaterDerived) -> list:
    cells = derived.cells
    parts = _stream_sentence(derived.surface_water)
    hydric_acres = _acres(derived.hydric["counts"][wd.HYDRIC_PREDOMINANT], cells["cell_acres"])
    terrain_acres = _acres(derived.wetness["wet_cells"], cells["cell_acres"])
    parts += [" The soil survey maps ", {"value": _one_decimal(hydric_acres)}, " acres as hydric and terrain wetness marks ",
              {"value": _one_decimal(terrain_acres)}, " acres"]
    flood = derived.flood
    if flood["fetched"] and flood["available"]:
        counts = flood["counts"]
        sfha = [z for z in flood["zones"] if z["sfha"] and z["cells_on_parcel"] > 0]
        if sfha:
            acres = _acres(sum(z["cells_on_parcel"] for z in sfha), cells["cell_acres"])
            parts += ["; ", {"value": _one_decimal(acres)}, " acres lie in a FEMA 1%-annual-chance flood zone."]
        else:
            dominant = max(counts, key=counts.get)
            parts += [f"; the whole parcel lies in FEMA {dominant.split(',')[0]}."]
    elif flood["fetched"]:
        parts += ["; no digital flood map covers the parcel."]
    else:
        parts += ["."]
    return parts


# ======================================================================
# Tables
# ======================================================================


def build_surface_water_table(derived: wd.WaterDerived) -> Optional[dict]:
    """One row per mapped stream within 150 m: permanence, order, length
    on the parcel, length within 150 m, distance from the boundary. None
    when no stream is within reach (the caption says so)."""
    streams = [s for s in derived.surface_water["streams"] if s["length_in_window_m"] > 0]
    if not streams:
        return None
    streams.sort(key=lambda s: (s["distance_m"], -s["length_in_window_m"]))
    rows = []
    for stream in streams:
        rows.append({
            "label": stream["name"] or "Unnamed stream",
            "cells": [
                _permanence_word(stream["permanence"]) if stream["permanence"] != "unknown" else "unknown",
                str(stream["stream_order"]) if stream["stream_order"] is not None else ZERO_DASH,
                _whole_or_dash(stream["length_on_parcel_m"] / METERS_PER_FOOT),
                _whole_or_dash(stream["length_in_window_m"] / METERS_PER_FOOT),
                _whole_or_dash(stream["distance_m"] / METERS_PER_FOOT),
            ],
        })
    return {"corner": "Stream", "columns": ["Permanence", "Order", "On the parcel, ft", "Within 500 ft, ft", "Distance, ft"],
            "rows": rows}


def build_surface_water_caption(derived: wd.WaterDerived, inputs: wd.WaterInputs) -> list:
    surface = derived.surface_water
    parts = []
    waterbodies = surface["waterbodies"]
    if waterbodies:
        on = sum(1 for w in waterbodies if w["area_on_parcel_m2"] > 0)
        parts.append(f"{_plural(len(waterbodies), 'mapped waterbody')} within 500 ft, {on} on the parcel. ")
    else:
        parts.append("No mapped waterbody lies within 500 ft. ")
    if surface["springs"] is None:
        parts.append("NHD's springs and seeps could not be checked when this report was generated. ")
    elif surface["springs"]:
        on = sum(1 for s in surface["springs"] if s["on_parcel"])
        parts.append(f"{_plural(len(surface['springs']), 'spring or seep')} mapped within 500 ft, {on} on the parcel. ")
    else:
        parts.append("No spring or seep is mapped within 500 ft. ")
    if not surface["order_available"] and surface["streams"]:
        parts.append("Stream order could not be read when this report was generated. ")
    parts.append(NHD_CAVEAT)
    return parts


def build_map_caption(derived: wd.WaterDerived, inputs: wd.WaterInputs) -> list:
    wetlands = derived.wetlands
    if not wetlands["fetched"]:
        reason = inputs.unavailable.get("nwi", {}).get("reason")
        if reason == "no_data_for_parcel":
            return ["The National Wetlands Inventory has no mapping for this parcel; no wetland is drawn."]
        return ["The National Wetlands Inventory did not answer when this report was generated; no wetland is drawn. "
                "Look the parcel up on the USFWS Wetlands Mapper."]
    project = wetlands["project"] or {}
    window_acres = sum(wetlands["window_area_by_type_m2"].values()) / SQUARE_METERS_PER_ACRE
    parts = []
    if wetlands["on_parcel_cells"] == 0 and window_acres <= 0.05:
        parts.append("No NWI wetland is mapped on the parcel or within 500 ft. ")
    elif wetlands["on_parcel_cells"] == 0:
        nearest = min((f["distance_m"] for f in wetlands["features"] if f["distance_m"] is not None), default=None)
        parts += ["No NWI wetland is mapped on the parcel; ", {"value": _one_decimal(window_acres)}, " acres lie within 500 ft"]
        if nearest is not None:
            parts += [", the nearest ", {"value": f"{_ft(nearest)} ft"}, " from the boundary"]
        parts.append(". ")
    else:
        acres = _acres(wetlands["on_parcel_cells"], derived.cells["cell_acres"])
        parts += ["NWI maps ", {"value": _one_decimal(acres)}, " acres of wetland on the parcel. "]
    if project.get("image_year"):
        parts += [f"Mapped by the {project.get('name')} project from ", {"value": str(project["image_year"])}, " imagery. "]
    parts.append(NWI_CAVEAT)
    return parts


# The wetness page holds the map, its legend and caption, and the water
# table under them. Measured on the reference parcel: seven map-unit rows
# plus the four parcel rows fill the page in the twelve-column form. A
# parcel with more map units takes the four-month form (January, April,
# July, October) so the page stays one page; the caption says which.
WATER_TABLE_TWELVE_MONTH_MAX_UNITS = 7
REPRESENTATIVE_MONTHS = (1, 4, 7, 10)


def water_table_months(derived: wd.WaterDerived) -> tuple:
    table = derived.water_table
    if not table["fetched"] or len(table["map_units"]) <= WATER_TABLE_TWELVE_MONTH_MAX_UNITS:
        return tuple(range(1, 13))
    return REPRESENTATIVE_MONTHS


def build_water_table(derived: wd.WaterDerived) -> Optional[dict]:
    """The monthly table: one row per map unit (acres in the label), the
    parcel's area-weighted depth, the share of the parcel with a water
    table, flooding and ponding as the share of the parcel with any
    frequency stated. Twelve columns, or the four representative months
    when the parcel has more map units than the page holds."""
    table = derived.water_table
    if not table["fetched"]:
        return None
    cells = derived.cells
    months = water_table_months(derived)
    twelve = len(months) == 12
    units = sorted(table["map_units"].items(), key=lambda kv: -kv[1]["cells"])
    unit_acres = allocate_exactly([u["cells"] for _, u in units], cells["on_parcel_count"] * cells["cell_acres"], 1)
    rows = []
    for (mukey, unit), acres in zip(units, unit_acres):
        label = [map_unit_short_name(unit["muname"]), ", ", {"value": _one_decimal_or_dash(acres, unit["cells"])}, " ac"]
        if not unit["has_data"]:
            rows.append({"label": label, "cells": [_word("no data") for _ in months]})
            continue
        row = []
        for m in months:
            month = unit["months"][m]
            if month["state"] == wd.WT_WET:
                row.append(f"{round(month['depth_cm'] * INCHES_PER_CM):,}")
            elif month["state"] == wd.WT_DEEPER and month["deeper_than_cm"] is not None:
                row.append(_bound(month["deeper_than_cm"] * INCHES_PER_CM))
            elif month["state"] == wd.WT_DEEPER:
                row.append(_word("deeper"))
            else:
                row.append(_word("no data"))
        rows.append({"label": label, "cells": row})
    parcel = table["parcel"]
    depth_row, share_row, flood_row, pond_row = [], [], [], []
    for m in months:
        p = parcel.get(m)
        if not p or p["data_share"] == 0:
            depth_row.append(_word("no data"))
            share_row.append(_word("no data"))
            flood_row.append(_word("no data"))
            pond_row.append(_word("no data"))
            continue
        depth_row.append(f"{round(p['depth_cm'] * INCHES_PER_CM):,}" if p["depth_cm"] is not None else ZERO_DASH)
        share_row.append(_one_decimal_or_dash(p["wet_share"] * 100.0))
        flood_row.append(_one_decimal_or_dash(sum(v for k, v in p["flood"].items() if k not in ("None", "not stated")) * 100.0))
        pond_row.append(_one_decimal_or_dash(sum(v for k, v in p["pond"].items() if k not in ("None", "not stated")) * 100.0))
    rows.append({"label": "Parcel, weighted, in", "cells": depth_row})
    rows.append({"label": "With a water table, %", "cells": share_row})
    rows.append({"label": "Flooding, % of parcel", "cells": flood_row})
    rows.append({"label": "Ponding, % of parcel", "cells": pond_row})
    columns = list(MONTH_INITIALS) if twelve else [MONTH_NAMES[m - 1] for m in months]
    return {"corner": "Water table, in", "columns": columns, "rows": rows, "monthly": twelve, "compact": not twelve,
            "months": months, "unit_acres": unit_acres}


def build_water_table_caption(derived: wd.WaterDerived) -> list:
    table = derived.water_table
    parts = [WATER_TABLE_CAVEAT, " “", {"value": ">72"}, "” is not a depth: no water table within the ", {"value": "72"},
             " in described; “no data” means no month described. The parcel depth is over the share of the parcel with a "
             "water table, beneath it; flooding and ponding are the share rated at any frequency."]
    if len(water_table_months(derived)) < 12:
        parts.append(f" Four representative months: the parcel's {len(table['map_units'])} map units are more than the "
                     "twelve-month form holds on one page.")
    return parts


def build_wetness_caption(derived: wd.WaterDerived) -> list:
    wetness = derived.wetness
    cells = derived.cells
    parts = ["Wet ground by terrain: ", {"value": _one_decimal(_acres(wetness["wet_cells"], cells["cell_acres"]))},
             " acres at or above a wetness index of ", {"value": f"{wetness['threshold']:.1f}"},
             ", the pipeline's own threshold, the 90th percentile of the elevation model's window. "]
    if wetness["depression_cells"]:
        parts += [{"value": _one_decimal_or_dash(_acres(wetness["depression_cells"], cells["cell_acres"]), wetness["depression_cells"])},
                  " acres of closed depressions, the deepest ", {"value": f"{wetness['depression_max_m'] / METERS_PER_FOOT:.1f} ft"}, ". "]
    else:
        parts.append("No closed depression deeper than the noise floor. ")
    parts.append(TWI_CAVEAT)
    return parts


def build_comparison_table(derived: wd.WaterDerived) -> dict:
    """A partition of the parcel by how many indicators call a cell wet:
    terrain only, hydric soil only, wetland only, two or more, none."""
    c = derived.comparison
    cells = derived.cells
    two_or_more = c["any"] - c["hydric_only"] - c["wetland_only"] - c["terrain_only"]
    names = [("Terrain wetness only", c["terrain_only"]), ("Hydric soil only", c["hydric_only"])]
    if derived.wetlands["fetched"]:
        names.append(("Mapped wetland only", c["wetland_only"]))
    names += [("Two or more indicators", two_or_more), ("None of the three" if derived.wetlands["fetched"] else "Neither", c["none"])]
    counts = [n for _, n in names]
    acres = allocate_exactly(counts, cells["on_parcel_count"] * cells["cell_acres"], 1)
    shares = allocate_exactly(counts, 100.0, 1)
    rows = [{"label": name, "cells": [_one_decimal_or_dash(a, n), _one_decimal_or_dash(s, n)]}
            for (name, n), a, s in zip(names, acres, shares)]
    rows.append({"label": "Total", "cells": [_one_decimal(round(cells["on_parcel_count"] * cells["cell_acres"], 1)), _one_decimal(100.0)]})
    return {"corner": "Wet ground", "columns": ["Acres", "% of parcel"], "rows": rows, "compact": True, "acres": acres, "shares": shares}


def build_comparison_caption(derived: wd.WaterDerived) -> list:
    c = derived.comparison
    h = derived.hydric
    cells = derived.cells
    parts = ["Hydric soil is a map unit whose hydric components reach ", {"value": f"{h['threshold_pct']:.0f}%"}, "; another ",
             {"value": _one_decimal_or_dash(_acres(h["counts"][wd.HYDRIC_PARTIAL], cells["cell_acres"]), h["counts"][wd.HYDRIC_PARTIAL])},
             " acres are partially hydric. "]
    if not derived.wetlands["fetched"]:
        parts += ["Mapped wetland could not be read when this report was generated. "]
    wettest = c["wettest_cell"]
    if wettest is not None:
        cls = wettest["hydric_class"]
        where = {wd.HYDRIC_PREDOMINANT: "a predominantly hydric map unit", wd.HYDRIC_PARTIAL: "a partially hydric map unit",
                 wd.HYDRIC_NONE: "a map unit the survey maps as non-hydric", wd.HYDRIC_NO_POLYGON: "ground the survey does not cover"}[cls]
        parts += ["The wettest ground by terrain (index ", {"value": f"{wettest['twi']:.1f}"}, f") is on {where}"]
        if wettest["muname"]:
            parts += [f", {map_unit_short_name(wettest['muname'])}"]
        parts += [": survey-scale mapping does not resolve ground this size."]
    return parts


def build_land_cover_table(derived: wd.WaterDerived) -> Optional[dict]:
    """Land cover of the contributing area within the analysis window and
    of the parcel, as shares, and the parcel's in acres -- two extents,
    named in the row labels."""
    land = derived.land_cover
    if not land["fetched"]:
        return None
    cells = derived.cells
    catchment = derived.catchment
    groups = [g for g in nlcd_landcover_data.NLCD_GROUP_ORDER
              if land["catchment"]["groups"].get(g) or land["parcel"]["groups"].get(g)]
    columns = [nlcd_landcover_data.NLCD_GROUP_LABELS[g] for g in groups]
    nodata = land["catchment"]["nodata"] or land["parcel"]["nodata"]
    if nodata:
        columns.append("No data")
    def _counts(scope):
        out = [land[scope]["groups"].get(g, 0) for g in groups]
        if nodata:
            out.append(land[scope]["nodata"])
        return out
    catchment_counts, parcel_counts = _counts("catchment"), _counts("parcel")
    catchment_acres = catchment["watershed_cells"] * cells["cell_acres"]
    parcel_acres = cells["on_parcel_count"] * cells["cell_acres"]
    catchment_shares = allocate_exactly(catchment_counts, 100.0, 1)
    parcel_shares = allocate_exactly(parcel_counts, 100.0, 1)
    parcel_acre_cells = allocate_exactly(parcel_counts, parcel_acres, 1)
    rows = [
        {"label": ["Contributing area within the window, ", {"value": _one_decimal(catchment_acres)}, " ac, %"],
         "cells": [_one_decimal_or_dash(v, n) for v, n in zip(catchment_shares, catchment_counts)]},
        {"label": ["The parcel, ", {"value": _one_decimal(round(parcel_acres, 1))}, " ac, %"],
         "cells": [_one_decimal_or_dash(v, n) for v, n in zip(parcel_shares, parcel_counts)]},
        {"label": "The parcel, acres", "cells": [_one_decimal_or_dash(v, n) for v, n in zip(parcel_acre_cells, parcel_counts)]},
    ]
    return {"corner": "Land cover", "columns": columns, "rows": rows, "parcel_acres": parcel_acre_cells,
            "catchment_shares": catchment_shares, "parcel_shares": parcel_shares}


def build_land_cover_caption(derived: wd.WaterDerived) -> list:
    catchment = derived.catchment
    land = derived.land_cover
    parts = ["Two extents, two questions: the contributing area within the elevation model's window"]
    if catchment["truncated"]:
        parts += [" (a lower bound: ", {"value": f"{catchment['rim_cells']:,}"}, " of its cells lie on the window's edge)"]
    parts += [" and the parcel; the rows are not a comparison of the parcel with its surroundings. "]
    if catchment["stream_catchment_acres"] is not None:
        reach = max((r for r in catchment["reaches"] if r["in_window"]), key=lambda r: r["total_drainage_acres"])
        parts += [f"{reach['name'] or 'The stream'}'s catchment at the reach beside the parcel is ",
                  {"value": f"{round(reach['total_drainage_acres']):,}"}, " acres (NHDPlus HR). "]
    if land["fetched"]:
        parts += ["NLCD ", {"value": str(land["year"])}, " at 30 m; catchment vegetation and sediment condition remain unmodelled."]
    return parts


def build_land_cover_unavailable(derived: wd.WaterDerived, inputs: wd.WaterInputs) -> list:
    parts = build_land_cover_caption(derived)
    parts.append(" NLCD land cover did not answer when this report was generated; look it up on the MRLC viewer.")
    return parts


def build_flood_table(derived: wd.WaterDerived) -> Optional[dict]:
    """Flood zone by designation, a partition of the parcel; None when the
    layer did not answer or no digital flood map covers the parcel."""
    flood = derived.flood
    if not flood["fetched"] or not flood["available"]:
        return None
    cells = derived.cells
    names = [(label, n) for label, n in flood["counts"].items() if n > 0 or label != flood["unmapped_label"]]
    counts = [n for _, n in names]
    acres = allocate_exactly(counts, cells["on_parcel_count"] * cells["cell_acres"], 1)
    shares = allocate_exactly(counts, 100.0, 1)
    rows = [{"label": label if label != wd.FLOOD_NOT_IN_ANY_ZONE else "Outside every drawn zone",
             "cells": [_one_decimal_or_dash(a, n), _one_decimal_or_dash(s, n)]}
            for (label, n), a, s in zip(names, acres, shares)]
    rows.append({"label": "Total", "cells": [_one_decimal(round(cells["on_parcel_count"] * cells["cell_acres"], 1)), _one_decimal(100.0)]})
    return {"corner": "Flood zone", "columns": ["Acres", "% of parcel"], "rows": rows, "compact": True, "acres": acres, "shares": shares}


def build_flood_caption(derived: wd.WaterDerived) -> list:
    flood = derived.flood
    parts = []
    adjacent = [z for z in flood["zones"] if z["sfha"] and z["cells_on_parcel"] == 0 and z["area_in_window_m2"] > 0]
    if adjacent:
        labels = sorted({z["label"] for z in adjacent})
        acres = sum(z["area_in_window_m2"] for z in adjacent) / SQUARE_METERS_PER_ACRE
        parts += [f"{' and '.join(labels)}, the 1%-annual-chance floodplain, lies within 500 ft of the boundary (",
                  {"value": _one_decimal(acres)}, " acres) and nowhere on the parcel. "]
    parts.append(FEMA_CAVEAT)
    return parts


def build_flood_unavailable(derived: wd.WaterDerived, inputs: wd.WaterInputs) -> list:
    flood = derived.flood
    if flood["fetched"] and not flood["available"]:
        return ["No digital flood map covers this parcel: FEMA's National Flood Hazard Layer has no study here. Not mapped does "
                "not mean not at risk -- large parts of rural America have no detailed flood study -- and the flood zone must be "
                "read from the county's paper map or a site study. " + FEMA_CAVEAT]
    return ["FEMA's National Flood Hazard Layer did not answer when this report was generated, so the flood zone is not "
            "reported. Look the parcel up at the FEMA Flood Map Service Center. " + FEMA_CAVEAT]


# ======================================================================
# Key figures
# ======================================================================


def build_key_figures(derived: wd.WaterDerived) -> list:
    cells = derived.cells
    surface = derived.surface_water
    within = [s for s in surface["streams"] if s["length_in_window_m"] > 0]
    figures = []
    if within:
        nearest = min(within, key=lambda s: s["distance_m"])
        if nearest["distance_m"] <= 0:
            figures.append({"value": f"{_ft(sum(s['length_on_parcel_m'] for s in within))} ft", "label": "mapped stream on the parcel"})
        else:
            figures.append({"value": f"{_ft(nearest['distance_m'])} ft", "label": "to the nearest mapped stream"})
    else:
        figures.append({"value": "None", "label": "mapped stream within 500 ft", "word": True})
    figures.append({"value": f"{_one_decimal(_acres(derived.hydric['counts'][wd.HYDRIC_PREDOMINANT], cells['cell_acres']))} ac",
                    "label": "hydric soil, predominantly"})
    if derived.wetlands["fetched"]:
        wet = derived.wetlands["on_parcel_cells"]
        figures.append({"value": f"{_one_decimal(_acres(wet, cells['cell_acres']))} ac" if wet else "None",
                        "label": "mapped wetland on the parcel", "word": not wet})
    else:
        figures.append({"value": "Not read", "label": "mapped wetland on the parcel", "word": True})
    figures.append({"value": f"{_one_decimal(_acres(derived.wetness['wet_cells'], cells['cell_acres']))} ac", "label": "wet ground by terrain"})
    table = derived.water_table
    if table["fetched"] and table["parcel"]:
        month = max(range(1, 13), key=lambda m: table["parcel"][m]["wet_share"])
        share = table["parcel"][month]["wet_share"]
        figures.append({"value": f"{share * 100:.0f}%", "label": f"with a water table in {MONTH_NAMES[month - 1]}"})
    else:
        figures.append({"value": "Not read", "label": "seasonal water table", "word": True})
    catchment = derived.catchment
    if catchment["stream_catchment_acres"] is not None:
        figures.append({"value": f"{round(catchment['stream_catchment_acres']):,} ac", "label": "stream catchment at the reach"})
    else:
        figures.append({"value": "Not read", "label": "stream catchment at the reach", "word": True})
    figures.append({"value": f"{_one_decimal(catchment['watershed_cells'] * cells['cell_acres'])} ac",
                    "label": "contributing area in the window" + (", at least" if catchment["truncated"] else "")})
    figures.append({"value": f"{_one_decimal(catchment['off_parcel_cells'] * cells['cell_acres'])} ac", "label": "of it beyond the parcel"})
    flood = derived.flood
    if flood["fetched"] and flood["available"]:
        sfha_acres = _acres(flood["sfha_cells"], cells["cell_acres"])
        if flood["sfha_cells"]:
            figures.append({"value": f"{_one_decimal(sfha_acres)} ac", "label": "in a 1%-annual-chance flood zone"})
        else:
            dominant = max(flood["counts"], key=flood["counts"].get)
            figures.append({"value": dominant.split(",")[0], "label": "FEMA flood zone, whole parcel", "word": True})
    elif flood["fetched"]:
        figures.append({"value": "Not mapped", "label": "FEMA flood zone", "word": True})
    else:
        figures.append({"value": "Not read", "label": "FEMA flood zone", "word": True})
    return figures


# ======================================================================
# Sources and methods
# ======================================================================


def build_sources(inputs: wd.WaterInputs, derived: wd.WaterDerived) -> list:
    retrieved = format_retrieved_on(inputs.retrieved_on)
    lines = [
        [f"USGS 3DEP elevation, 1/3 arc-second, resampled to 5 m, retrieved {retrieved}."],
        [f"USGS National Hydrography Dataset, flowlines and waterbodies at 1:24,000, retrieved {retrieved}."],
    ]
    if inputs.nhdplus_hr is not None:
        lines.append(["USGS NHDPlus High Resolution, network flowline attributes (stream order, total drainage area)."])
    if inputs.soil_water_table is not None:
        surveys = inputs.soil_water_table.get("survey_areas") or []
        if surveys:
            version = (surveys[0].get("saverest") or "").split(" ")[0]
            lines.append([f"USDA NRCS SSURGO, soil survey {surveys[0]['areasymbol']}, version of {version}: comonth, cosoilmoist, muaggatt."])
        else:
            lines.append(["USDA NRCS SSURGO: comonth, cosoilmoist, muaggatt."])
    if inputs.nwi is not None:
        project = inputs.nwi.get("project") or {}
        detail = f", {project['name']} project, {project['image_year']} imagery" if project.get("image_year") else ""
        lines.append([f"USFWS National Wetlands Inventory{detail}."])
    if inputs.fema_nfhl is not None:
        panel = derived.flood.get("panel")
        detail = ""
        if panel and panel.get("firm_pan"):
            detail = f", FIRM panel {panel['firm_pan']}" + (f" effective {format_retrieved_on(panel['effective_on'])}" if panel.get("effective_on") else "")
        lines.append([f"FEMA National Flood Hazard Layer{detail}."])
    if inputs.nlcd_landcover is not None:
        lines.append([f"USGS {inputs.nlcd_landcover['collection']} land cover, {inputs.nlcd_landcover['year']}, 30 m."])
    return lines


def build_methods(inputs: wd.WaterInputs, derived: wd.WaterDerived) -> list:
    """Full citations, terms as found, and the method behind each figure,
    for the methods note Site overview builds. Not rendered here."""
    retrieved = format_retrieved_on(inputs.retrieved_on)
    methods = [
        {"source": "USGS NHD", "identifier": "National Hydrography Dataset, flowline and waterbody large-scale layers, bbox + 150 m",
         "period": retrieved, "citation": "U.S. Geological Survey, National Hydrography Dataset, served by The National Map's NHD map service.",
         "terms": "U.S. federal work; USGS states its data are in the public domain.",
         "method": "Permanence from FCode (46006 perennial, 46003 intermittent, 46007 ephemeral); lengths on the parcel and within "
                   "150 m by intersection in the parcel's UTM zone; distance from the boundary to the nearest flowline; springs and "
                   "seeps from the Point layer (FCode 45800)."},
        {"source": "USGS NHDPlus HR", "identifier": "NetworkNHDFlowline value-added attributes joined by permanent_identifier",
         "period": retrieved, "citation": nhdplus_data.NHDPLUS_CITATION, "terms": "U.S. federal work; public domain.",
         "method": "Strahler order (streamorde) and total upstream drainage area (totdasqkm, converted to acres) per reach; the "
                   "largest reach figure within 150 m is the stream's catchment at the parcel."},
        {"source": "USDA NRCS SSURGO", "identifier": "comonth, cosoilmoist, muaggatt, sacatalog over the parcel's map units",
         "period": retrieved, "citation": soil_water_table.SSURGO_CITATION, "terms": "U.S. federal work; public domain.",
         "method": "Depth to water table in a month is the top of the shallowest cosoilmoist layer with status Wet, weighted by "
                   "comppct over each map unit's major components; a month without a Wet layer is deeper than the component's "
                   "deepest described layer; a component with no comonth rows is no data. Map units weighted by their cells on "
                   "the parcel. Hydric: a map unit whose hydric components sum to 50% or more is predominantly hydric."},
        {"source": "USFWS NWI", "identifier": "Wetlands map service, two-stage fetch; Data_Source for the mapping project",
         "period": retrieved, "citation": nwi_data.NWI_CITATION,
         "terms": f"FGDC metadata: Access_Constraints \"{nwi_data.NWI_ACCESS_CONSTRAINTS}\"; Use_Constraints \"{nwi_data.NWI_USE_CONSTRAINTS}\".",
         "method": "Polygons intersecting the parcel's bbox + 150 m; geometry at 1 m generalisation for features under 1,000 acres "
                   "and 10 m for larger; acreage on the parcel by cell-centre containment, within 150 m by polygon area."},
        {"source": "FEMA NFHL", "identifier": "NFHL map service layers 0 (availability), 28 (flood hazard zones), 3 (FIRM panels)",
         "period": retrieved, "citation": nfhl_data.NFHL_CITATION, "terms": nfhl_data.NFHL_TERMS_BASIS,
         "method": "Zones intersecting the parcel's bbox + 150 m, generalised at 5 m in the parcel's UTM zone; acreage by "
                   "cell-centre containment, Special Flood Hazard Areas first where zones overlap; no availability polygon at the "
                   "parcel reads as no digital flood map."},
        {"source": "USGS Annual NLCD", "identifier": f"{nlcd_landcover_data.NLCD_COLLECTION} land cover, {nlcd_landcover_data.NLCD_YEAR}, on the DEM grid",
         "period": retrieved, "citation": nlcd_landcover_data.NLCD_CITATION,
         "terms": "U.S. federal work; USGS states its data are in the public domain.",
         "method": "Nearest-neighbour sample of the 30 m grid onto the 5 m DEM grid, the year pinned by a mosaic rule; shares by "
                   "cell count over the window-derived contributing area and over the parcel."},
        {"source": "USGS 3DEP", "identifier": f"3DEP 1/3 arc-second DEM, resampled to {max(inputs.dem['resolution_meters']):.0f} m, {inputs.dem['crs']}",
         "period": retrieved, "citation": "U.S. Geological Survey, 3D Elevation Program seamless DEM, served by The National Map elevation service.",
         "terms": "U.S. federal work; public domain.",
         "method": "One priority-flood fill with flat resolution, D8 flow direction and accumulation (the landform derivations' pass); "
                   "raw TWI = ln(specific catchment area / tan slope) on the exclusion result's slope grid; wet ground by terrain at "
                   "or above the water step's window-referenced full-credit breakpoint (90th percentile of the window's raw TWI); "
                   "depression depth = filled minus raw above a 0.1 m noise floor; the contributing area is the union of the "
                   "parcel outlets' watersheds within the window, with cells on the window's rim counted as evidence of truncation."},
    ]
    return methods


# ======================================================================
# The section
# ======================================================================


def build_water_section(inputs: wd.WaterInputs, tokens: Optional[dict] = None,
                        flow: Optional[landform_derivations.TerrainDerived] = None) -> dict:
    """The section dict water.html sets. `flow` is Landform's derived
    object (the shared flow pass); `tokens` defaults to site_report.TOKENS."""
    if tokens is None:
        import site_report

        tokens = site_report.TOKENS
    derived = wd.derive(inputs, flow)
    contours = report_map.parcel_contours(inputs.dem, inputs.boundary_polygon_utm)
    hydrology = report_map.render_map(inputs.boundary_polygon_utm, build_hydrology_layers(inputs, derived, contours), tokens)
    wetness = report_map.render_map(inputs.boundary_polygon_utm, build_wetness_layers(inputs, derived, contours), tokens)
    water_table = build_water_table(derived)
    return {
        "number": section_number(SECTION_NAME),
        "name": SECTION_NAME,
        "template": SECTION_TEMPLATE,
        "heading": SECTION_NAME,
        "summary": build_summary(derived),
        "map": hydrology,
        "map_caption": build_map_caption(derived, inputs),
        "surface_water_table": build_surface_water_table(derived),
        "surface_water_caption": build_surface_water_caption(derived, inputs),
        "wetness_map": wetness,
        "wetness_caption": build_wetness_caption(derived),
        "water_table": water_table,
        "water_table_caption": build_water_table_caption(derived),
        "water_table_unavailable": ["SSURGO's seasonal water table did not answer when this report was generated; the monthly "
                                    "table is not reported. The soils section's map units still stand."],
        "key_figures": build_key_figures(derived),
        "comparison_table": build_comparison_table(derived),
        "comparison_caption": build_comparison_caption(derived),
        "land_cover_table": build_land_cover_table(derived),
        "land_cover_caption": build_land_cover_caption(derived),
        "land_cover_unavailable": build_land_cover_unavailable(derived, inputs),
        "flood_table": build_flood_table(derived),
        "flood_caption": build_flood_caption(derived),
        "flood_unavailable": build_flood_unavailable(derived, inputs),
        "sources": build_sources(inputs, derived),
        "methods": build_methods(inputs, derived),
        "derived": derived,
        "contours": {k: v for k, v in contours.items() if k != "levels"},
    }
