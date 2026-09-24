"""
design_section.py

SECTION VIII, DESIGN: the layout map and the design record -- the design
on its land, then what the user committed at each step, in STEP_ORDER.

    design_inputs_from_context(context, document, report_data) -> DesignInputs
    build_map_layers(inputs, record)                           -> [report_map layer, ...]
    build_design_section(inputs, tokens)                       -> the section dict

THE LAYOUT MAP IS REBUILT, NOT ADOPTED. render_layout_map.py (matplotlib,
Web Mercator, fourteen colour literals) stays exactly as it is for the
narrated report until D4 retires it. This map is report_map's vector SVG
in the pipeline's UTM zone, every colour a token, over NAIP photography.

THE PLATE SYSTEM, APPLIED TO THE DESIGN. One mark per element, each cased
in the page colour: over photography no single ink wins against every
patch of ground, and a mark with a halo is legible because one of the two
always separates from what is under it.

    production blocks   oxide hatch, no outline
    water survey areas  water tint, a firmer water edge
    road                ink, solid
    fencing             ink, dashed and lighter
    structure sites     ink building glyph
    tree zones          field dot screen
    contours            terrain, LIGHTER THAN LANDFORM'S, NO LABELS
    streams             water line (NHD)
    access point        ochre -- the user placed it; ink is what the tool sited
    boundary            ink (report_map's own, cased)

CONTOURS ARE HERE because they are the only thing on the page that says
why the design sits where it does: photography shows cover, not slope, so
without them a road following a grade or a block stopping at a break
looks arbitrary. They are SUBORDINATE to Landform's: one hairline weight,
set back, and no elevation labels -- Landform is where an elevation is
read, and labels here would compete with the design's own. The same call
the soil map made.

STREAMS ARE HERE by the same rule: a survey area sited on a drainage, or
a road crossing one, is only legible with the stream shown.

WHAT THIS MAP MUST NOT DRAW -- see test_design_section.py, which asserts
both absent and says why:

    keypoints        keypoint detection is independent of the water step,
                     so nothing on this map was sited relative to one.
                     Landform reports them, beside the keyline that gives
                     them meaning.
    exclusion zones  canopy, slope, hydric, roads, setback. They constrain
                     DRAWING; on the interactive map they tell the user
                     where they may work, and at commit that job ended.
                     They are also the five heaviest layers in the set.
                     render_layout_map.py draws them as a leftover; this
                     map does not. A RENDERING exclusion only -- they stay
                     in the payload and the pipeline still needs them.

THE RULE FOR ANY LAYER CONSIDERED LATER: a terrain layer earns its place
on this page only if it explains a design decision. That is why
DesignInputs carries no keypoints and no exclusion result at all: the map
cannot draw what it is never handed.

THIS MAP IS EXEMPT FROM THE SHARED SECTION SCALE (report_map.LAYOUT_FRAME,
drawn with fit=False). It is the deliverable and takes the page; its own
scale bar states its scale.

LABELS NAME ELEMENTS -- "Block 1", "Excavated 1", "Site 1", "Access" --
the record's own names. Rationale belongs in the record, not on the map,
which has to stay readable at arm's length.

THE RECORD is design_record.build_design_record(document): the document
alone, as each step's DATA PANEL showed it, no footnote about what the
document does not hold. A card per committed feature -- the panel's
header, its provenance, the panel's rows in the panel's order -- in
STEP_ORDER; a step committed empty said in words; a count, never a sum.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from rasterio.warp import transform_geom
from shapely.geometry import LineString, MultiLineString, Point, box, mapping, shape

import design_record
import naip_imagery
import report_map
from fence_display_geometry import fence_display_lines
from landform_section import format_retrieved_on
from report_outline import section_number

SECTION_NAME = "Design"
SECTION_TEMPLATE = "design.html"
MAP_HEADING = "The layout"
RECORD_HEADING = "The design record"

# THE OFF-PARCEL WASH: the neighbours' land set back, lighter than the old
# layout map's 0.55 so the photography still reads as context.
WASH = {"token": "page", "opacity": 0.35}

# Weights, in points. Contours are one hairline weight, BELOW Landform's
# lightest (report_map.CONTOUR_STROKE_PT, 0.45) and set back further.
CONTOUR_STROKE_PT = 0.35
CONTOUR_OPACITY = 0.9
STREAM_STROKE_PT = 0.9
ROAD_STROKE_PT = 1.5
FENCE_STROKE_PT = 0.7
FENCE_DASH = "3 2"
WATER_EDGE_PT = 1.0
WATER_TINT_OPACITY = 0.28
HATCH_STROKE_PT = 0.75
TREE_DOT_PT = 1.5
CASING_PT = 0.9          # the page-coloured halo each side of a line or a dot
HATCH_CASING_PT = 0.3    # a hatch's, thinner: one per line, and a thick one bleaches the tint
CONTOUR_CASING_PT = 0.3  # the contours' own, lighter and set back -- they are context, not design
CONTOUR_CASING_OPACITY = 0.35

ACCESS_LABEL = "Access"

LEGEND = {
    "production": "Production blocks",
    "water": "Water survey areas",
    "roads": "Road",
    "access": "Access point",
    "trees": "Tree zones",
    "structures": "Structure sites",
    "fencing": "Fencing",
    "streams": "Streams",
    "boundary": "Parcel boundary",
}


@dataclass
class DesignInputs:
    document: dict                  # the Design Document, every step committed
    dem: dict                       # the session's DEM -- contours, and the CRS every shape is drawn in
    boundary_polygon_utm: object
    streams: list                   # ParcelData's NHD stream rows, geometry in WGS84
    imagery: Optional[dict]         # naip_imagery.parse_naip's block, None when it degraded
    imagery_unavailable: Optional[dict]
    retrieved_on: date


def _created_on(document: dict) -> date:
    stamp = (document or {}).get("created_at")
    if not stamp:
        return date.today()
    return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).date()


def design_inputs_from_context(context, document: dict, report_data) -> DesignInputs:
    """The session's reads and the report data's imagery. Takes the DEM,
    the boundary and the streams off the context -- nothing a step
    computed. Keypoints and the exclusion result are deliberately not
    read: see the module docstring."""
    parcel = context.parcel_data
    water_features = getattr(parcel, "water_features", None) or {}
    return DesignInputs(
        document=document,
        dem=context.dem,
        boundary_polygon_utm=context.boundary_polygon_utm,
        streams=list(water_features.get("streams") or []),
        imagery=getattr(report_data, "naip_imagery", None),
        imagery_unavailable=(getattr(report_data, "unavailable", None) or {}).get("naip_imagery"),
        retrieved_on=_created_on(document),
    )


# ======================================================================
# Geometry
# ======================================================================


def _utm(geometry_wgs84: dict, crs: str):
    return shape(transform_geom("EPSG:4326", crs, geometry_wgs84))


def _features(document: dict, step_id: str) -> list:
    return list(document["steps"][step_id]["features"].get("features") or [])


def _linear(geometry) -> list:
    if geometry is None or geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    if isinstance(geometry, MultiLineString):
        return list(geometry.geoms)
    return [g for part in getattr(geometry, "geoms", []) for g in _linear(part)]


def map_extent(inputs: DesignInputs) -> tuple:
    """The ground the layout map's whole frame covers -- what the
    photograph fills and what context beyond the parcel is clipped to."""
    return report_map.frame_extent_utm(inputs.boundary_polygon_utm, report_map.LAYOUT_FRAME, fit=False)


# ON THE MAP, A DRAWN SHAPE SAYS WHAT IT IS. The record's "Drawn 1" sits
# under its step's heading, where it is unambiguous; on the map a drawn
# block and a drawn tree zone would both read "Drawn 1", told apart only
# by colour. Every other name is the record's own.
DRAWN_MAP_NOUNS = {"landform": "block", "trees": "zone"}


def map_labels(record: dict) -> dict:
    """{feature id: the label the map sets on it} -- the record card's
    name, a drawn shape's qualified."""
    labels = {}
    for step in record["steps"]:
        for card in step.get("cards") or []:
            name = card["name"]
            noun = DRAWN_MAP_NOUNS.get(step["step_id"])
            if noun and card["source"] == "Drawn":
                name = name.replace("Drawn ", f"Drawn {noun} ", 1)
            labels[card["id"]] = name
    return labels


def build_map_layers(inputs: DesignInputs, record: dict, contours: dict) -> list:
    """The layers, in draw order, each only when it has something to draw.
    Every legend entry is a layer's own, so the legend names exactly what
    was drawn."""
    crs = inputs.dem["crs"]
    parcel = inputs.boundary_polygon_utm
    document = inputs.document
    steps = {s["step_id"]: s for s in record["steps"]}
    names = map_labels(record)
    layers = []

    lines = [level["geometry"] for level in contours["levels"]]
    if lines:
        layers.append(report_map.layer(
            "contours", lines, kind="line", stroke="terrain", stroke_width=CONTOUR_STROKE_PT,
            stroke_opacity=CONTOUR_OPACITY, casing_pt=CONTOUR_CASING_PT, casing_opacity=CONTOUR_CASING_OPACITY,
            legend=["Contours, ", {"value": f"{contours['interval_ft']} ft"}],
        ))

    frame = box(*map_extent(inputs))
    streams = []
    for row in inputs.streams:
        if row.get("geometry"):
            streams += _linear(_utm(row["geometry"], crs).intersection(frame))
    if streams:
        layers.append(report_map.layer("streams", streams, kind="line", stroke="water", stroke_width=STREAM_STROKE_PT,
                                       casing_pt=CASING_PT, legend=LEGEND["streams"]))

    trees = _features(document, "trees")
    if trees:
        layers.append(report_map.layer(
            "trees", [_utm(f["geometry"], crs) for f in trees], kind="screen", fill="field", screen_dot_pt=TREE_DOT_PT,
            casing_pt=CASING_PT / 2, labels=[names[f["id"]] for f in trees], legend=LEGEND["trees"],
        ))

    blocks = _features(document, "landform")
    if blocks:
        layers.append(report_map.layer(
            "production", [_utm(f["geometry"], crs) for f in blocks], kind="hatch", fill="oxide",
            stroke_width=HATCH_STROKE_PT, casing_pt=HATCH_CASING_PT, labels=[names[f["id"]] for f in blocks],
            legend=LEGEND["production"],
        ))

    zones = _features(document, "water")
    if zones:
        layers.append(report_map.layer(
            "water", [_utm(f["geometry"], crs) for f in zones], kind="polygon", fill="water", fill_opacity=WATER_TINT_OPACITY,
            stroke="water", stroke_width=WATER_EDGE_PT, casing_pt=CASING_PT,
            labels=[names[f["id"]] for f in zones], legend=LEGEND["water"],
        ))

    fences = _features(document, "fencing")
    if fences:
        boundary_rings = [_utm(f["geometry"], crs) for f in fences if f["properties"]["fence_type"] == "boundary"]
        zone_rings = [_utm(f["geometry"], crs) for f in fences if f["properties"]["fence_type"] != "boundary"]
        drawn_boundary, drawn_zones = fence_display_lines(boundary_rings, zone_rings)
        fence_lines = [g for g in drawn_boundary if g is not None and not g.is_empty]
        for ring in drawn_zones:
            fence_lines += _linear(ring.intersection(parcel)) if ring is not None else []
        if fence_lines:
            layers.append(report_map.layer("fencing", fence_lines, kind="line", stroke="ink", stroke_width=FENCE_STROKE_PT,
                                           dash=FENCE_DASH, casing_pt=CASING_PT, legend=LEGEND["fencing"]))

    branches = _features(document, "roads")
    if branches:
        layers.append(report_map.layer("roads", [_utm(f["geometry"], crs) for f in branches], kind="line", stroke="ink",
                                       stroke_width=ROAD_STROKE_PT, casing_pt=CASING_PT, legend=LEGEND["roads"]))
        lon, lat = steps["roads"]["access_point"]["lon_lat"]
        layers.append(report_map.layer("access", [_utm(mapping(Point(lon, lat)), crs)], kind="point", stroke="ochre",
                                       marker="dot", labels=[ACCESS_LABEL], label_halo=True, legend=LEGEND["access"]))

    sites = _features(document, "structures")
    if sites:
        layers.append(report_map.layer(
            "structures", [_utm(f["geometry"], crs).representative_point() for f in sites], kind="point", stroke="ink",
            marker="glyph", labels=[names[f["id"]] for f in sites], label_halo=True, legend=LEGEND["structures"],
        ))
    return layers


# The legend's order: the design in step order, then the terrain beneath it,
# then the boundary -- not the draw order, which puts tints under lines.
LEGEND_ORDER = ("production", "water", "roads", "access", "trees", "structures", "fencing", "contours", "streams")


def boundary_legend_spec() -> dict:
    """The boundary's legend entry, drawn by report_map itself rather than
    as a layer."""
    return report_map.layer("boundary", [], kind="line", stroke="ink", stroke_width=report_map.BOUNDARY_STROKE_PT,
                            legend=LEGEND["boundary"])


def build_map(inputs: DesignInputs, record: dict, tokens: dict) -> dict:
    contours = report_map.parcel_contours(inputs.dem, inputs.boundary_polygon_utm)
    layers = build_map_layers(inputs, record, contours)
    extent = map_extent(inputs)
    meters_per_pt = (extent[2] - extent[0]) / report_map.LAYOUT_FRAME[0]
    underlay = None
    if inputs.imagery is not None:
        underlay = naip_imagery.underlay(inputs.imagery, inputs.dem["crs"], extent, meters_per_pt,
                                         page_rgb=naip_imagery.page_rgb(tokens))
    rendered = report_map.render_map(
        inputs.boundary_polygon_utm, layers, tokens, report_map.LAYOUT_FRAME,
        fit=False, underlay=underlay, wash=WASH if underlay else None, halo=True, labels_on_top=True,
    )
    by_id = {spec["id"]: spec for spec in layers}
    ordered = [by_id[i] for i in LEGEND_ORDER if i in by_id] + [boundary_legend_spec()]
    rendered["legend"] = report_map.legend_entries(ordered, tokens)
    rendered["layers"] = layers
    rendered["underlay"] = underlay
    rendered["contours"] = {k: v for k, v in contours.items() if k != "levels"}
    return rendered


def build_imagery_line(inputs: DesignInputs, underlay: Optional[dict]) -> list:
    """Beneath the map: what the photograph is and, above all, WHEN it
    was taken -- a reader years from now must know what year they are
    looking at. Without imagery, the statement that stands in its place."""
    if inputs.imagery is None or underlay is None:
        return ["Aerial imagery unavailable: the NAIP imagery service did not answer for this parcel, and the map is "
                "drawn without it."]
    acquired = naip_imagery.format_acquired(inputs.imagery)
    return ["Aerial imagery: USDA NAIP, acquired ", {"value": acquired}, ", ",
            {"value": f"{inputs.imagery['gsd']:g} m"}, " ground resolution. Outside the parcel the photograph is set back."]


# ======================================================================
# The record
# ======================================================================


def _card(card: dict) -> dict:
    """One committed feature as the page sets it: the panel's header, the
    provenance, and the panel's rows -- a figure in the data face,
    right-aligned, before its label; a word the same way in the prose
    face; a heading or a hairline where the panel breaks."""
    rows = []
    for row in card["rows"]:
        kind = row["kind"]
        if kind == "break":
            rows.append({"kind": "heading" if row["label"] else "rule", "label": row["label"]})
        elif kind == "term":
            rows.append({"kind": "term", "value": row["value"]})
        else:
            rows.append({"kind": kind, "value": row["value"], "label": row["label"] or ""})
    return {"name": card["name"], "source": card["source"], "rows": rows}


def build_record(record: dict) -> list:
    """One block per step, in STEP_ORDER: a heading, then a card per
    committed feature and the step's count, or the sentence a step
    committed empty carries. The road carries its access point."""
    blocks = []
    for step in record["steps"]:
        block = {"step_id": step["step_id"], "title": step["title"], "empty": step["empty"]}
        if step["empty"]:
            block["statement"] = step["statement"]
        else:
            block["cards"] = [_card(card) for card in step["cards"]]
            if step["step_id"] == "roads":
                block["note"] = ["Access point ", {"value": step["access_point"]["text"]}, ", placed."]
            else:
                block["note"] = [{"value": str(step["count"]["n"])}, f" {step['count']['noun']} committed."]
        blocks.append(block)
    return blocks


def build_sources(inputs: DesignInputs, contours: dict) -> list:
    retrieved = format_retrieved_on(inputs.retrieved_on)
    lines = [["The design: the shapes committed at each step, with the figures each step's panel showed; production "
              "acreage is the drawn block, and the eligible-cell footprint is larger."]]
    if inputs.imagery is not None:
        lines.append([f"USDA Farm Service Agency, National Agriculture Imagery Program, acquired "
                      f"{naip_imagery.format_acquired(inputs.imagery)}, {inputs.imagery['gsd']:g} m, "
                      f"via Microsoft Planetary Computer, retrieved {retrieved}."])
    terrain = []
    if contours.get("interval_ft"):
        terrain.append(f"USGS 3DEP elevation resampled to 5 m, contoured at {contours['interval_ft']} ft")
    if inputs.streams:
        terrain.append("USGS National Hydrography Dataset flowlines at 1:24,000")
    if terrain:
        lines.append(["; ".join(terrain) + f"; retrieved {retrieved}."])
    return lines


def build_design_section(inputs: DesignInputs, tokens: dict) -> dict:
    record = design_record.build_design_record(inputs.document)
    rendered = build_map(inputs, record, tokens)
    return {
        "number": section_number(SECTION_NAME),
        "name": SECTION_NAME,
        "template": SECTION_TEMPLATE,
        "heading": MAP_HEADING,
        "record_heading": RECORD_HEADING,
        "map": rendered,
        "imagery_line": build_imagery_line(inputs, rendered["underlay"]),
        "imagery_available": rendered["underlay"] is not None,
        "record": build_record(record),
        "sources": build_sources(inputs, rendered["contours"]),
        "design_record": record,
    }
