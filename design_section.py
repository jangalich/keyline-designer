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

THE INTERACTIVE MAP'S VOCABULARY, IN ITS ACTIVE STATE, FOR EVERY LAYER AT
ONCE (branch 16). The page reproduces the marks the user worked with, at
the levels the step in hand draws at: --pattern-active 0.75 for a mark,
--tint-active 0.22 for a wash. On the plate nothing was decided more
recently than anything else, so nothing recedes -- no committed state, no
eligible tint, no focus. Values are the rendering audit's as branch 15
amended them, CSS pixels at 0.75 pt each (WeasyPrint's px):

    production    8 px tile, rising "/" hatch, oxide, 1 px, square caps, on a
                  --rule screen at 0.12 in the tile; no edge -- the edge is
                  where the hatching stops
    trees         the same tile falling "\", --tree, no screen, no edge
    embankment    --survey-embankment wash at 0.22, its own colour edge 2 px
    excavated     64 px tile, 24 x 24 dot lattice, r 1 px, --survey-excavated,
                  on a --halo screen at 0.16; its own colour edge 2 px
    road          ink, 2 px, at 0.75 -- on a HALF-WIDTH --halo rim
    fencing       ink, 1.25 px, dashed 8,5, at 0.75, uncased
    structure     the 28 px teardrop pin on a --halo halo: ink where the tool
      sites       sited it, ochre where the user placed it
    access point  an 18 px ochre disc ringed 2 px in --halo
    boundary      ink, 2 px, uncased, over a graded edge
    contours      terrain, 0.6 pt, uncased, no labels
    streams       --stream, lighter than either survey blue

FILLS ARE NOT CASED. A white casing under a hatch or a wash is what fogged
the parcel and made the interior read hazier than the neighbouring land.
The only screens are the two the interactive map's tiles carry: production
on --rule, excavated on --halo. Trees and embankment carry none.

THE ROAD IS THE ONE CASED LINE, AND ITS CASING IS HALF THE SCREEN'S.
Rendered over the reference NAIP (diagnose_layout_map_variants.py): an
uncased ink road at 0.75 disappears where it crosses the canopy shadow,
and the screen's full casing turns it into a pale line with a grey core --
the look branch 15 rejected for the fence. Half a pixel each side is the
middle -- AND IT IS A RIM, not a stroke under the line (report_map's
casing_rim): a stroked casing of any width lies under the line's whole
width, and the 0.75 ink over it still read as a faded line. The rim
leaves the core over the ground, as dark as the uncased road, and lifts
only its edges off the shadow. THE FENCE STAYS UNCASED: its dashes fade over canopy shadow too,
but a fence through shadow is rare and a cased dash reads as white beads
everywhere else.

THE BOUNDARY IS DRAWN, IN INK, AND THE PARCEL IS LIFTED. The off-parcel
wash here is 0.35 against the interactive map's 0.55 scrim, and rendered
without a line the parcel edge goes soft wherever it meets woods of the
same tone -- and it is the line someone in the field traces. Outside it,
a GRADED EDGE: four disjoint rings of ink stepping 0.18, 0.10, 0.05, 0.02
across 25 pt (report_map's `edge`), so the parcel reads as lifted off its
neighbours. Built with shapely buffers, not an SVG filter -- WeasyPrint
ignores feGaussianBlur and feDropShadow -- and disjoint rather than
stacked, so each band prints at exactly its stated opacity. At 15 pt the
lift read only in close-up.

THE PIN'S DROP SHADOW DOES NOT PRINT (it is a CSS filter on screen), so
the --halo stroke carries the separation alone; test_design_section.py
holds it over dark canopy.

GEOMETRY IS THE SERVER'S DISPLAY FIELDS, NEVER REIMPLEMENTED. A suggested
production block draws its display_only_smoothed_outline, never
re-smoothed; a drawn block as stored. Trees the raw cell union -- smoothing
removed 19.6% of a 0.32 ac candidate. Water the envelope as sent, never
the member features. Roads the routed LineString as sent, unsimplified.
Fencing its display_only_fence_line, which already carries the 8 m
coincidence trim against the boundary and the zone edges; a null value
draws nothing. A structure pad is not drawn: it places the pin at the
area-weighted centroid of its largest piece.

CONTOURS ARE HERE because they are the only thing on the page that says
why the design sits where it does: photography shows cover, not slope, so
without them a road following a grade or a block stopping at a break
looks arbitrary. With no casing under them they are drawn HEAVIER than
the section maps' lightest (0.6 pt against Landform's 0.45) -- 0.6 is the
floor, below which they drop out over the hatches; do not thin them. No
elevation labels: Landform is where an elevation is read.

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
from display_outline import DISPLAY_ONLY_OUTLINE_PROPERTY
from fence_display_geometry import DISPLAY_ONLY_FENCE_LINE_PROPERTY
from landform_section import format_retrieved_on
from report_outline import section_number

SECTION_NAME = "Design"
SECTION_TEMPLATE = "design.html"
MAP_HEADING = "The layout"
RECORD_HEADING = "The design record"

# THE OFF-PARCEL WASH: the neighbours' land set back, lighter than the
# interactive map's 0.55 scrim so the photography still reads as context.
WASH = {"token": "page", "opacity": 0.35}

# THE GRADED EDGE outside the boundary: (band width pt, opacity) outward,
# disjoint rings, 25 pt in all. See the module docstring.
EDGE = {"token": "ink", "steps": [(3.0, 0.18), (5.0, 0.10), (7.0, 0.05), (10.0, 0.02)]}

# THE INTERACTIVE MAP'S VALUES, in CSS pixels, and the pixel in points.
PX = 0.75
PATTERN_ACTIVE = 0.75   # --pattern-active: every mark and outline
TINT_ACTIVE = 0.22      # --tint-active: the embankment wash
LINE_PT = 2 * PX        # layers.jsx LINE_WEIGHT: road, boundary, the survey edges
# The road's casing: HALF the screen's CASING_WEIGHT 4 under LINE_WEIGHT 2,
# so half a pixel each side rather than one. See the module docstring.
ROAD_CASING_PT = (4 - 2) / 2 * PX / 2
ROAD_CASING_RIM = True
FENCE_PT = 1.25 * PX
FENCE_DASH = f"{8 * PX:g} {5 * PX:g}"
HATCH_TILE = {"tile_pt": 8 * PX, "weight_pt": 1 * PX}
PRODUCTION_TILE = {"type": "hatch", **HATCH_TILE, "rise": "up", "screen": {"token": "rule", "opacity": 0.12}}
TREE_TILE = {"type": "hatch", **HATCH_TILE, "rise": "down", "screen": None}
EXCAVATED_TILE = {"type": "dots", "tile_pt": 64 * PX, "grid": 24, "radius_pt": 1.0 * PX,
                  "screen": {"token": "halo", "opacity": 0.16}}
PIN_PT = 28 * PX
ACCESS_PT = 18 * PX
ACCESS_RING_PT = 2 * PX
# CONTOURS: 0.6 pt, uncased -- THE FLOOR. Heavier than Landform's 0.45
# because nothing is cased here; any thinner and they drop out over the
# hatches (rendered, branch 16). Do not thin them.
CONTOUR_STROKE_PT = 0.6
STREAM_STROKE_PT = 1.1

ACCESS_LABEL = "Access"

LEGEND = {
    "production": "Production blocks",
    "water-embankment": "Water survey area, embankment",
    "water-excavated": "Water survey area, excavated",
    "roads": "Road",
    "access": "Access point",
    "trees": "Tree zones",
    "structures": "Structure site, suggested",
    "structures-placed": "Structure site, placed",
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


def _pin_point(geometry):
    """Where a structure's pin points: a placed site's own coordinate, or
    the area-weighted centroid of its pad's largest piece (the interactive
    map's largestPieceCentroid). The pad itself is never drawn."""
    if geometry.geom_type == "Point":
        return geometry
    parts = list(geometry.geoms) if geometry.geom_type == "MultiPolygon" else [geometry]
    return max(parts, key=lambda g: g.area).centroid


def build_map_layers(inputs: DesignInputs, record: dict, contours: dict) -> list:
    """The layers, in draw order, each only when it has something to draw.
    Every legend entry is a layer's own, so the legend names exactly what
    was drawn. The geometry is the server's display fields, as sent; see
    the module docstring."""
    crs = inputs.dem["crs"]
    document = inputs.document
    steps = {s["step_id"]: s for s in record["steps"]}
    names = map_labels(record)
    layers = []

    def add(spec):
        if spec["geometries"]:
            layers.append(spec)

    add(report_map.layer("contours", [level["geometry"] for level in contours["levels"]], kind="line", stroke="terrain",
                         stroke_width=CONTOUR_STROKE_PT, legend=["Contours, ", {"value": f"{contours['interval_ft']} ft"}]))

    frame = box(*map_extent(inputs))
    streams = []
    for row in inputs.streams:
        if row.get("geometry"):
            streams += _linear(_utm(row["geometry"], crs).intersection(frame))
    add(report_map.layer("streams", streams, kind="line", stroke="stream", stroke_width=STREAM_STROKE_PT,
                         legend=LEGEND["streams"]))

    # Trees: the raw cell union, never smoothed.
    trees = _features(document, "trees")
    add(report_map.layer("trees", [_utm(f["geometry"], crs) for f in trees], kind="pattern", fill="tree",
                         pattern=TREE_TILE, pattern_opacity=PATTERN_ACTIVE, labels=[names[f["id"]] for f in trees],
                         legend=LEGEND["trees"]))

    # Production: a suggestion's display outline as sent, a drawn block as stored.
    blocks = _features(document, "landform")
    add(report_map.layer(
        "production",
        [_utm(f["properties"].get(DISPLAY_ONLY_OUTLINE_PROPERTY) or f["geometry"], crs) for f in blocks],
        kind="pattern", fill="oxide", pattern=PRODUCTION_TILE, pattern_opacity=PATTERN_ACTIVE,
        labels=[names[f["id"]] for f in blocks], legend=LEGEND["production"]))

    # Water: two marks, the envelope as sent.
    zones = _features(document, "water")
    embankment = [f for f in zones if f["properties"].get("survey_type") == "embankment"]
    excavated = [f for f in zones if f["properties"].get("survey_type") == "excavated"]
    add(report_map.layer(
        "water-embankment", [_utm(f["geometry"], crs) for f in embankment], kind="polygon", fill="survey-embankment",
        fill_opacity=TINT_ACTIVE, stroke="survey-embankment", stroke_width=LINE_PT, stroke_opacity=PATTERN_ACTIVE,
        labels=[names[f["id"]] for f in embankment], legend=LEGEND["water-embankment"]))
    add(report_map.layer(
        "water-excavated", [_utm(f["geometry"], crs) for f in excavated], kind="pattern", fill="survey-excavated",
        pattern=EXCAVATED_TILE, pattern_opacity=PATTERN_ACTIVE, stroke="survey-excavated", stroke_width=LINE_PT,
        stroke_opacity=PATTERN_ACTIVE, labels=[names[f["id"]] for f in excavated], legend=LEGEND["water-excavated"]))

    # Fencing: the display line, trimmed server-side; a null one draws nothing.
    fence_lines = []
    for f in _features(document, "fencing"):
        line = f["properties"].get(DISPLAY_ONLY_FENCE_LINE_PROPERTY)
        if line is not None:
            fence_lines += _linear(_utm(line, crs))
    add(report_map.layer("fencing", fence_lines, kind="line", stroke="ink", stroke_width=FENCE_PT, dash=FENCE_DASH,
                         stroke_opacity=PATTERN_ACTIVE, legend=LEGEND["fencing"]))

    # Roads: the routed LineString as sent.
    branches = _features(document, "roads")
    add(report_map.layer("roads", [_utm(f["geometry"], crs) for f in branches], kind="line", stroke="ink",
                         stroke_width=LINE_PT, stroke_opacity=PATTERN_ACTIVE, casing_pt=ROAD_CASING_PT,
                         casing_opacity=PATTERN_ACTIVE, casing_rim=ROAD_CASING_RIM, legend=LEGEND["roads"]))
    if branches:
        lon, lat = steps["roads"]["access_point"]["lon_lat"]
        add(report_map.layer("access", [_utm(mapping(Point(lon, lat)), crs)], kind="point", stroke="ochre",
                             marker="disc", marker_size_pt=ACCESS_PT, marker_halo_pt=ACCESS_RING_PT,
                             labels=[ACCESS_LABEL], label_halo=True, legend=LEGEND["access"]))

    # Structure sites: pins by provenance -- ink the tool's, ochre the user's.
    sites = _features(document, "structures")
    provenance = document["steps"]["structures"].get("provenance") or {}
    for layer_id, token, placed in (("structures", "ink", False), ("structures-placed", "ochre", True)):
        chosen = [f for f in sites if (provenance.get(f["id"]) == "user_added") == placed]
        add(report_map.layer(layer_id, [_pin_point(_utm(f["geometry"], crs)) for f in chosen], kind="point",
                             stroke=token, marker="pin", marker_size_pt=PIN_PT, labels=[names[f["id"]] for f in chosen],
                             label_halo=True, legend=LEGEND[layer_id]))
    return layers


# The legend's order: the design in step order, then the terrain beneath it,
# then the boundary -- not the draw order, which puts tints under lines.
LEGEND_ORDER = ("production", "water-embankment", "water-excavated", "roads", "access", "trees", "structures",
                "structures-placed", "fencing", "contours", "streams")

BOUNDARY_STYLE = {"stroke": "ink", "width": LINE_PT, "casing": False}


def boundary_legend_spec() -> dict:
    """The boundary's legend entry, drawn by report_map itself rather than
    as a layer."""
    return report_map.layer("boundary", [], kind="line", stroke="ink", stroke_width=LINE_PT, legend=LEGEND["boundary"])


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
        boundary_style=BOUNDARY_STYLE, edge=EDGE,
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
