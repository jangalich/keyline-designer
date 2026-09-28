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
(report_map.visible_extent_utm), never to the parcel: a stream off the
boundary is on the map when the frame reaches it, and the frame reaches
at most 50 m beyond the parcel's bbox (report_map.MAX_CONTEXT_MARGIN_M).
A PARCEL WITH NOTHING TO DRAW SAYS SO ON THE MAP: a quiet line in its
middle names what is absent (empty_parcel_note), so an empty shape reads
as a finding, not a failure. "Within 500 ft" is a true distance from the
boundary (water_derivations.adjacency_window), not the fetch box.

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
import report_text as rt
import water_survey_areas
from hydrology_data import NHD_CHANNEL_OFFSET_FT, NHD_COMPILATION_SCALE
from soil_data import SSURGO_COMPILATION_SCALE
import soil_water_table
import water_derivations as wd
from contour_lines import _grid_axes
from climate_section import MONTH_INITIALS, MONTH_NAMES
from landform_section import (
    ZERO_DASH,
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
# The flood zone: diagonal hatch, the lightest treatment on the map --
# wide-spaced and faint, so the wetland tufts, which sit inside the zone
# where a stream has both, read on top of it rather than into it.
HATCH_SPACING_M = 22.0
HATCH_STROKE_PT = 0.35
HATCH_OPACITY = 0.3
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
# THE ADJACENCY BUFFER, SAID ONE WAY. Every "within" on these pages is
# adjacency_window(): the boundary buffered by ADJACENCY_BUFFER_METERS.
# It was typed as "500 ft" in the captions while the summary derived
# 492 ft from the same 150 m; every mention now comes from here.
WITHIN = rt.feet(wd.ADJACENCY_BUFFER_METERS)
NHD_CAVEAT = rt.clause("Mapped streams can sit ", rt.feet_range(*NHD_CHANNEL_OFFSET_FT), " from the real channel, and springs and "
                       "seeps are often missed; check both on the ground.")
NWI_CAVEAT = "not checked on the ground, and not a legal wetland determination."
FEMA_CAVEAT = ("Unmapped does not mean safe: much of rural America has no detailed flood study, and Zone X means only that no "
               "1%-annual-chance floodplain has been mapped there.")
MAP_UNIT_GLOSS = rt.MAP_UNIT
HYDRIC_GLOSS = "formed under saturation"
TWI_CAVEAT = "The index reads only the land's shape, not soil or cover, so a flat, well-drained bench can score wet."
CATCHMENT_TRUNCATION = ("The contributing area is measured within the elevation model's window, the parcel plus 100 m, "
                        "and its watershed reaches the window's edge, so it is a lower bound.")
# Wetland acreage within the buffer below this reads as none.
NO_WETLAND_ACRES = 0.05


# ======================================================================
# Formatting
# ======================================================================


_ft = rt.feet_text


def _acres(cells: int, cell_acres: float) -> float:
    return cells * cell_acres


def _whole_or_dash(value: float) -> str:
    return ZERO_DASH if round(value) == 0 and value == 0 else f"{round(value):,}"


def _bound(inches: float) -> dict:
    return rt.bound(f">{round(inches):,}")


_word = rt.word


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


def empty_parcel_note(derived: wd.WaterDerived, boundary_polygon_utm) -> Optional[dict]:
    """The quiet statement for a parcel with nothing to draw (set at the
    head of the map's caption): no mapped stream, waterbody, wetland or 1%-annual-chance flood
    zone on it -- each named only when its source was read, so the note
    never claims an absence nobody checked. None when anything is drawn."""
    surface = derived.surface_water
    if any(s["length_on_parcel_m"] > 0 for s in surface["streams"]) or any(w["area_on_parcel_m2"] > 0 for w in surface["waterbodies"]):
        return None
    if any(s["on_parcel"] for s in (surface["springs"] or [])):
        return None
    if derived.wetlands["fetched"] and derived.wetlands["on_parcel_cells"] > 0:
        return None
    if derived.flood["fetched"] and derived.flood["sfha_cells"] > 0:
        return None
    absent = ["stream", "open water"]
    if derived.wetlands["fetched"]:
        absent.append("wetland")
    if derived.flood["fetched"] and derived.flood["available"]:
        absent.append("1%-annual-chance (100-year) flood zone")
    if len(absent) > 2:
        first, last = ", ".join(absent[:-1]), absent[-1]
    else:
        first, last = absent[0], absent[1]
    return {"lines": [f"No mapped {first}", f"or {last} on the parcel."], "point": boundary_polygon_utm.representative_point()}


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


def _stream_named(stream: dict, fallback: str) -> list:
    """'Montour Run, perennial and of stream order 2 (counted up from the
    smallest headwaters)': the order glossed where it is first read."""
    parts = [stream["name"] or fallback, f", {_permanence_word(stream['permanence'])}"]
    if stream["stream_order"] is not None:
        parts += [" and of stream order ", rt.number(stream["stream_order"]), " (counted up from the smallest headwaters)"]
    return rt.clause(*parts)


def _stream_sentence(surface: dict) -> list:
    streams = [s for s in surface["streams"] if s["length_in_window_m"] > 0]
    if not streams:
        return rt.clause("No mapped stream lies within ", WITHIN, " of the boundary.")
    on_parcel = [s for s in streams if s["length_on_parcel_m"] > 0]
    if on_parcel:
        longest = max(on_parcel, key=lambda s: s["length_on_parcel_m"])
        return rt.clause(_stream_named(longest, "An unnamed stream"), ", crosses the parcel for ", rt.feet(longest["length_on_parcel_m"]), ".")
    nearest = min(streams, key=lambda s: s["distance_m"])
    return rt.clause("No mapped stream crosses the parcel; ", _stream_named(nearest, "an unnamed stream"), ", runs ",
                     rt.feet(nearest["distance_m"]), " beyond the boundary.")


def _wet_ground_sentence(derived: wd.WaterDerived) -> list:
    cells = derived.cells
    hydric = rt.acres(_acres(derived.hydric["counts"][wd.HYDRIC_PREDOMINANT], cells["cell_acres"]))
    terrain = rt.acres(_acres(derived.wetness["wet_cells"], cells["cell_acres"]))
    lead = rt.clause("The soil survey maps ", hydric, f" as hydric — {HYDRIC_GLOSS} — and the lie of the land marks ", terrain,
                     " as likely wet")
    flood = derived.flood
    if flood["fetched"] and flood["available"]:
        sfha = [z for z in flood["zones"] if z["sfha"] and z["cells_on_parcel"] > 0]
        if sfha:
            acres = rt.acres(_acres(sum(z["cells_on_parcel"] for z in sfha), cells["cell_acres"]))
            return rt.clause(lead, "; ", acres, " lie in a FEMA 1%-annual-chance (100-year) flood zone.")
        # The zone's own description is on the flood page; here the zone is enough.
        dominant = max(flood["counts"], key=flood["counts"].get)
        return rt.clause(lead, f"; the whole parcel lies in FEMA {dominant.split(',')[0]}.")
    if flood["fetched"]:
        return rt.clause(lead, "; no digital flood map covers the parcel.")
    return rt.clause(lead, ".")


def build_summary(derived: wd.WaterDerived) -> list:
    return rt.sentences(_stream_sentence(derived.surface_water), _wet_ground_sentence(derived))


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
    return {"corner": "Stream", "columns": ["Permanence", "Order", "On the parcel, ft", f"Within {WITHIN['value']}, ft", "Distance, ft"],
            "rows": rows}


def _mapped_within(features: list, on_parcel: int, singular: str, plural_form: str) -> list:
    return rt.clause(rt.count(len(features), singular, plural_form), " mapped within ", WITHIN, ", ", rt.number(on_parcel), " on the parcel.")


def _open_water_and_springs(surface: dict) -> list:
    waterbodies, springs = surface["waterbodies"], surface["springs"]
    if not waterbodies and springs == []:
        return rt.clause("No lake, open water, spring or seep is mapped within ", WITHIN, ".")
    open_water = (_mapped_within(waterbodies, sum(1 for w in waterbodies if w["area_on_parcel_m2"] > 0), "lake or open water", "lakes or open waters")
             if waterbodies else rt.clause("No lake or open water is mapped within ", WITHIN, "."))
    if springs is None:
        seeps = ["Springs and seeps could not be checked when this report was generated."]
    elif springs:
        seeps = _mapped_within(springs, sum(1 for x in springs if x["on_parcel"]), "spring or seep", "springs or seeps")
    else:
        seeps = rt.clause("No spring or seep is mapped within ", WITHIN, ".")
    return rt.sentences(open_water, seeps)


def build_surface_water_caption(derived: wd.WaterDerived, inputs: wd.WaterInputs) -> list:
    surface = derived.surface_water
    order = (["Stream order could not be read when this report was generated."]
             if not surface["order_available"] and surface["streams"] else None)
    return rt.sentences(_open_water_and_springs(surface), order, NHD_CAVEAT)


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
    photographs = (rt.clause(", traced from ", rt.year(project["image_year"]), " aerial photographs") if project.get("image_year")
                   else [", traced from aerial photographs"])
    if wetlands["on_parcel_cells"] == 0 and window_acres <= NO_WETLAND_ACRES:
        return rt.clause("The National Wetlands Inventory maps no wetland on the parcel or within ", WITHIN, photographs, ": ", NWI_CAVEAT)
    if wetlands["on_parcel_cells"] == 0:
        nearest = min((f["distance_m"] for f in wetlands["features"] if f["distance_m"] is not None), default=None)
        where = rt.clause(", the nearest ", rt.feet(nearest), " from the boundary") if nearest is not None else None
        return rt.clause("The National Wetlands Inventory maps ", rt.acres(window_acres), " of wetland within ", WITHIN, where,
                         photographs, ": ", NWI_CAVEAT)
    acres = _acres(wetlands["on_parcel_cells"], derived.cells["cell_acres"])
    return rt.clause("The National Wetlands Inventory maps ", rt.acres(acres), " of wetland on the parcel", photographs, ": ", NWI_CAVEAT)


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


def _four_months_clause(derived: wd.WaterDerived) -> list:
    months = water_table_months(derived)
    if len(months) == len(MONTH_NAMES):
        return None
    return rt.clause("Only ", rt.series_text([MONTH_NAMES[m - 1] for m in months]), " are shown: the parcel's ",
                     rt.count(len(derived.water_table["map_units"]), "map unit"), " need the room.")


def _bound_clause(derived: wd.WaterDerived) -> list:
    """The table's own bound, read back from it: '“>72” means none within
    the 72 in the survey describes'. None when no cell is a bound."""
    table = build_water_table(derived)
    bounds = [c for r in (table or {}).get("rows", []) for c in r["cells"] if isinstance(c, dict) and c.get("kind") == "bound"]
    if not bounds:
        return ["“No data” is a month the survey does not describe."]
    mark = bounds[0]["value"]
    return rt.clause("“", rt.bound(mark), "” means none within the ", rt.inches_of(float(mark.lstrip(">").replace(",", "")), places=0),
                     " the survey describes; “no data”, a month it does not describe.")


def build_water_table_caption(derived: wd.WaterDerived) -> list:
    """What the table's rows and marks mean. How a depth is weighted is in
    the methods note."""
    return rt.sentences(
        [f"Depth to the seasonal water table by month, from the soil survey; each row is a map unit, {MAP_UNIT_GLOSS}."],
        _bound_clause(derived),
        ["The parcel rows average the ground that has a water table; flooding and ponding give the share rated to flood or to hold "
         "standing water at all."],
        _four_months_clause(derived))


def build_wetness_caption(derived: wd.WaterDerived) -> list:
    """The one thing that could change a decision: what the index cannot
    see. Its threshold and the percentile it is read at are in the methods
    note."""
    wetness = derived.wetness
    cells = derived.cells
    wet = rt.clause(rt.acres(_acres(wetness["wet_cells"], cells["cell_acres"])), " rank among the wettest ground around the parcel "
                    "on the wetness index — how much land drains to a spot against how fast it sheds water.")
    if wetness["depression_cells"]:
        hollows = rt.clause(rt.acres(_acres(wetness["depression_cells"], cells["cell_acres"])), " sit in closed hollows, the deepest ",
                            rt.feet_of(wetness["depression_max_m"] / METERS_PER_FOOT, places=1), ".")
    else:
        hollows = ["No closed hollow is deep enough to hold water."]
    return rt.sentences(wet, hollows, [TWI_CAVEAT])


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


WETTEST_WHERE = {wd.HYDRIC_PREDOMINANT: "predominantly hydric", wd.HYDRIC_PARTIAL: "partly hydric",
                 wd.HYDRIC_NONE: "non-hydric", wd.HYDRIC_NO_POLYGON: None}


def _hydric_clause(derived: wd.WaterDerived) -> list:
    h = derived.hydric
    partial = h["counts"][wd.HYDRIC_PARTIAL]
    counts = rt.clause(f"Hydric soil ({HYDRIC_GLOSS}) counts where at least ", rt.percent(h["threshold_pct"]),
                       f" of a map unit — {MAP_UNIT_GLOSS} — is hydric")
    if not partial:
        return rt.clause(counts, "; none of the parcel is partly hydric.")
    return rt.clause(counts, "; ", rt.acres(_acres(partial, derived.cells["cell_acres"])), " more are partly hydric.")


def _wettest_clause(derived: wd.WaterDerived) -> list:
    wettest = derived.comparison["wettest_cell"]
    if wettest is None:
        return None
    kind = WETTEST_WHERE[wettest["hydric_class"]]
    if kind is None:
        return ["The wettest spot by the land's shape is on ground the soil survey does not cover."]
    name = f" {map_unit_short_name(wettest['muname'])}" if wettest["muname"] else " ground"
    return [f"The wettest spot by the land's shape is on {kind}{name}, too small for the soil map to resolve."]


def build_comparison_caption(derived: wd.WaterDerived) -> list:
    unread = None if derived.wetlands["fetched"] else ["Mapped wetland could not be read when this report was generated."]
    return rt.sentences(_hydric_clause(derived), unread, _wettest_clause(derived))


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


def _two_areas_clause(derived: wd.WaterDerived) -> list:
    at_least = " — at least this much, since it runs past the edge of the elevation data" if derived.catchment["truncated"] else ""
    return [f"The first row is the land that drains onto the parcel{at_least}; the rows describe two areas, not the parcel "
            "against its surroundings."]


def _stream_catchment_clause(derived: wd.WaterDerived) -> list:
    catchment = derived.catchment
    if catchment["stream_catchment_acres"] is None:
        return None
    reach = max((r for r in catchment["reaches"] if r["in_window"]), key=lambda r: r["total_drainage_acres"])
    return rt.clause(f"{reach['name'] or 'The stream'} drains ", rt.acres(reach["total_drainage_acres"], places=0),
                     " where it passes the parcel.")


def _land_cover_source_clause(derived: wd.WaterDerived) -> list:
    land = derived.land_cover
    if not land["fetched"]:
        return None
    return rt.clause("Cover from NLCD ", rt.year(land["year"]), ", a national satellite map; what grows upstream and how much soil "
                     "washes down are not modelled.")


def build_land_cover_caption(derived: wd.WaterDerived) -> list:
    return rt.sentences(_two_areas_clause(derived), _stream_catchment_clause(derived), _land_cover_source_clause(derived))


def build_land_cover_unavailable(derived: wd.WaterDerived, inputs: wd.WaterInputs) -> list:
    return rt.sentences(build_land_cover_caption(derived),
                        ["NLCD land cover did not answer when this report was generated; look it up on the MRLC viewer."])


def single_flood_zone(derived: wd.WaterDerived) -> Optional[str]:
    """The one zone label covering every on-parcel cell, or None."""
    flood = derived.flood
    if not flood["fetched"] or not flood["available"]:
        return None
    present = [label for label, n in flood["counts"].items() if n > 0]
    if len(present) == 1 and present[0] != flood["unmapped_label"]:
        return present[0]
    return None


def build_flood_statement(derived: wd.WaterDerived) -> Optional[list]:
    """A parcel that lies wholly in one zone gets a sentence, not a
    two-row table saying the same thing twice."""
    label = single_flood_zone(derived)
    if label is None:
        return None
    zone, _, subtype = label.partition(", ")
    parts = [f"The whole parcel lies in FEMA {zone}"]
    if subtype:
        parts.append(f", {'an ' if subtype[0] in 'aeiou' else 'a '}{subtype}")
    panel = derived.flood["panel"]
    if panel and panel.get("firm_pan"):
        # The panel and its date are labels, set in prose; "flood insurance rate map" is what FIRM stands for.
        parts += [", on flood insurance rate map panel ", rt.word(panel["firm_pan"])]
        if panel.get("effective_on"):
            parts += [" effective ", rt.word(format_retrieved_on(panel["effective_on"]))]
    parts.append(".")
    return rt.clause(*parts)


def build_flood_table(derived: wd.WaterDerived) -> Optional[dict]:
    """Flood zone by designation, a partition of the parcel; None when the
    layer did not answer, no digital flood map covers the parcel, or the
    parcel lies wholly in one zone (build_flood_statement)."""
    flood = derived.flood
    if not flood["fetched"] or not flood["available"] or single_flood_zone(derived) is not None:
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


def _adjacent_floodplain_clause(derived: wd.WaterDerived) -> list:
    adjacent = [z for z in derived.flood["zones"] if z["sfha"] and z["cells_on_parcel"] == 0 and z["area_in_window_m2"] > 0]
    if not adjacent:
        return None
    labels = rt.series_text(sorted({z["label"] for z in adjacent}))
    return rt.clause(f"{labels}, the 1%-annual-chance (100-year) floodplain, lies within ", WITHIN, " of the boundary (",
                     rt.acres_of_m2(sum(z["area_in_window_m2"] for z in adjacent)), ") but nowhere on the parcel.")


def build_flood_caption(derived: wd.WaterDerived) -> list:
    return rt.sentences(_adjacent_floodplain_clause(derived), [FEMA_CAVEAT])


def build_flood_unavailable(derived: wd.WaterDerived, inputs: wd.WaterInputs) -> list:
    flood = derived.flood
    if flood["fetched"] and not flood["available"]:
        return ["No digital flood map covers this parcel: FEMA's National Flood Hazard Layer has no study here, so the flood zone "
                "must be read from the county's paper map or a site study. " + FEMA_CAVEAT]
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
        figures.append({"value": "None", "label": f"mapped stream within {WITHIN['value']}", "word": True})
    figures.append({"value": f"{_one_decimal(_acres(derived.hydric['counts'][wd.HYDRIC_PREDOMINANT], cells['cell_acres']))} ac",
                    "label": "hydric soil, predominantly"})
    if derived.wetlands["fetched"]:
        wet = derived.wetlands["on_parcel_cells"]
        figures.append({"value": f"{_one_decimal(_acres(wet, cells['cell_acres']))} ac" if wet else "None",
                        "label": "mapped wetland on the parcel", "word": not wet})
    else:
        figures.append({"value": "Unavailable", "label": "mapped wetland on the parcel", "word": True})
    figures.append({"value": f"{_one_decimal(_acres(derived.wetness['wet_cells'], cells['cell_acres']))} ac", "label": "wet ground by terrain"})
    table = derived.water_table
    if table["fetched"] and table["parcel"]:
        month = max(range(1, 13), key=lambda m: table["parcel"][m]["wet_share"])
        share = table["parcel"][month]["wet_share"]
        figures.append({"value": f"{share * 100:.0f}%", "label": f"with a water table in {MONTH_NAMES[month - 1]}"})
    else:
        figures.append({"value": "Unavailable", "label": "seasonal water table", "word": True})
    catchment = derived.catchment
    if catchment["stream_catchment_acres"] is not None:
        figures.append({"value": f"{round(catchment['stream_catchment_acres']):,} ac", "label": "stream catchment at the reach"})
    else:
        figures.append({"value": "Unavailable", "label": "stream catchment at the reach", "word": True})
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
        figures.append({"value": "Unavailable", "label": "FEMA flood zone", "word": True})
    return figures


# ======================================================================
# Sources and methods
# ======================================================================


def build_sources(inputs: wd.WaterInputs, derived: wd.WaterDerived) -> list:
    retrieved = format_retrieved_on(inputs.retrieved_on)
    lines = [
        [f"USGS 3DEP elevation, 1/3 arc-second, resampled to 5 m, retrieved {retrieved}."],
        [f"USGS National Hydrography Dataset, flowlines and waterbodies at 1:{NHD_COMPILATION_SCALE:,}, retrieved {retrieved}."],
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
                   f"{wd.ADJACENCY_BUFFER_METERS:g} m ({WITHIN['value']}) of the boundary by intersection in the parcel's UTM zone; distance "
                   "from the boundary to the nearest flowline; springs and seeps from the Point layer (FCode 45800)."},
        {"source": "USGS NHDPlus HR", "identifier": "NetworkNHDFlowline value-added attributes joined by permanent_identifier",
         "period": retrieved, "citation": nhdplus_data.NHDPLUS_CITATION, "terms": "U.S. federal work; public domain.",
         "method": "Strahler order (streamorde) and total upstream drainage area (totdasqkm, converted to acres) per reach; the "
                   f"largest reach figure within {wd.ADJACENCY_BUFFER_METERS:g} m is the stream's catchment at the parcel."},
        {"source": "USDA NRCS SSURGO", "identifier": "comonth, cosoilmoist, muaggatt, sacatalog over the parcel's map units",
         "period": retrieved, "citation": soil_water_table.SSURGO_CITATION, "terms": "U.S. federal work; public domain.",
         "method": "Depth to water table in a month is the top of the shallowest cosoilmoist layer with status Wet, weighted by "
                   "comppct over each map unit's major components; a month without a Wet layer is deeper than the component's "
                   "deepest described layer; a component with no comonth rows is no data. Map units weighted by their cells on "
                   f"the parcel. The survey is compiled at 1:{SSURGO_COMPILATION_SCALE:,}, so the monthly table is as coarse as the soil "
                   f"map. Hydric: a map unit whose hydric components sum to {derived.hydric['threshold_pct']:.0f}% or more is predominantly "
                   "hydric." + (f" The wettest cell by terrain has a raw TWI of {derived.comparison['wettest_cell']['twi']:.1f}."
                                if derived.comparison["wettest_cell"] else "")},
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
                   "raw TWI = ln(specific catchment area / tan slope) on the slope grid Landform classes; wet ground by terrain at "
                   f"or above the {water_survey_areas.TWI_WINDOW_FULL_CREDIT_PERCENTILE:.0f}th percentile of the window's raw TWI "
                   f"({derived.wetness['threshold']:.1f} here); "
                   "depression depth = filled minus raw above a 0.1 m noise floor; the contributing area is the union of the "
                   "parcel outlets' watersheds within the window, with cells on the window's rim counted as evidence of truncation"
                   + (f" ({derived.catchment['rim_cells']:,} here, so it is a lower bound)." if derived.catchment["truncated"] else ".")},
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
    # COMPUTED AGAIN HERE, KNOWINGLY -- NOT A BUG, AND NOT FREE TO REMOVE.
    # parcel_contours() runs once per section map: Site overview, Landform,
    # Water, Access, Trees, Soils and Design, seven times per report over the
    # same DEM and interval. About 0.06 s a time and no network
    # (diagnose_report_generation_time.py), so the repeat costs under half a
    # second and cannot fail on a flaky source. Sharing one result means
    # threading it through every section's builder; worth doing only if the
    # report's compute ever matters beside its fetches.
    contours = report_map.parcel_contours(inputs.dem, inputs.boundary_polygon_utm)
    # THE EMPTY-PARCEL STATEMENT LEADS THE CAPTION (branch 17 review): set on
    # the map it sat across the parcel's contours, and there is no clear
    # ground on a parcel-fitted frame to move it to.
    hydrology = report_map.render_map(inputs.boundary_polygon_utm, build_hydrology_layers(inputs, derived, contours), tokens)
    note = empty_parcel_note(derived, inputs.boundary_polygon_utm)
    wetness = report_map.render_map(inputs.boundary_polygon_utm, build_wetness_layers(inputs, derived, contours), tokens)
    water_table = build_water_table(derived)
    return {
        "number": section_number(SECTION_NAME),
        "name": SECTION_NAME,
        "template": SECTION_TEMPLATE,
        "heading": SECTION_NAME,
        "summary": build_summary(derived),
        "map": hydrology,
        "map_caption": rt.sentences([" ".join(note["lines"])] if note else None, build_map_caption(derived, inputs)),
        "surface_water_table": build_surface_water_table(derived),
        "surface_water_caption": build_surface_water_caption(derived, inputs),
        "wetness_map": wetness,
        "wetness_caption": build_wetness_caption(derived),
        "water_table": water_table,
        "water_table_caption": build_water_table_caption(derived),
        "water_table_unavailable": ["The soil survey's seasonal water table did not answer when this report was generated, so the "
                                    "monthly table is not reported; the Soils section's map units still stand."],
        "key_figures": build_key_figures(derived),
        "comparison_table": build_comparison_table(derived),
        "comparison_caption": build_comparison_caption(derived),
        "land_cover_table": build_land_cover_table(derived),
        "land_cover_caption": build_land_cover_caption(derived),
        "land_cover_unavailable": build_land_cover_unavailable(derived, inputs),
        "flood_table": build_flood_table(derived),
        "flood_statement": build_flood_statement(derived),
        "flood_caption": build_flood_caption(derived),
        "flood_unavailable": build_flood_unavailable(derived, inputs),
        "sources": build_sources(inputs, derived),
        "methods": build_methods(inputs, derived),
        "derived": derived,
        "contours": {k: v for k, v in contours.items() if k != "levels"},
    }
