"""
fence_display_geometry.py

THE DISPLAY-ONLY FENCE LINE -- ONE implementation of the two passes
render_layout_map.py has always run over a fence ring before drawing it,
read by the PDF's layout map and shipped, as a rendering hint and nothing
else, on the interactive map's fence features.

WHAT THE PROBLEM IS. A fence ring is the boundary of a buffered zone fill,
and that fill is a union of 5 m DEM cells (or, for the boundary fence, a
margined hull of several such unions clipped to the parcel). Its edge
inherits the DEM-resolution stairstep. The PDF has never drawn that: it runs
each ring through a shapely simplify() pass first. And two fence rings can
run ON TOP OF EACH OTHER: find_boundary_fencing() unions each zone's own
buffered polygon into the boundary fence, so along a shared stretch a zone
ring and the boundary ring are data-exact coincident, and two adjacent zones'
own buffered rings run near-parallel a buffer's width apart. Simplified
independently, the same stretch keeps different vertices on each ring and
renders as two visibly separate near-parallel lines. The PDF trims that away
too. The interactive map drew the raw rings, so the two maps of the same
parcel disagreed about what the same fence looks like.

TWO PASSES, AND THEIR ORDER IS THE SPEC.

  PASS 1 -- ANGULAR SIMPLIFY. angular_simplify_closed_ring() at
  FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M, on EVERY ring, boundary and
  zone alike, each on its own. A shapely simplify() pass ONLY: no Chaikin,
  no corner rounding of any kind. Fence lines render ANGULAR, by explicit
  request. The tolerance is deliberately larger than the road corridor's
  2.5 m (ROAD_RENDER_SIMPLIFY_TOLERANCE_M) so the stairstep collapses into
  fewer, longer straight segments rather than merely having its corners
  rounded off.

  PASS 2 -- THE COINCIDENCE TRIM. AFTER every ring is simplified, each ZONE
  ring is trimmed by the union of all the OTHER drawn rings -- the boundary
  ring(s) plus every other zone ring -- buffered by
  ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M. Every zone is compared
  against the others' ORIGINAL (pass 1) rings, never against an
  already-trimmed result, so there is no ordering dependency and no rank.

  THE TRIM IS SYMMETRIC, NOT PRIORITY-BASED. Where two zone rings run close,
  BOTH lose the shared stretch, and the pair renders as two separate line
  pieces with a real, visible GAP between them. That gap is the intended
  result, not a defect to close, and this module must not be "improved"
  into a priority rule that lets one zone keep the stretch: neither zone in
  an adjacent pair outranks the other. The boundary ring is the one
  asymmetry, and it is stated: boundary rings are drawn as-is and are never
  trimmed against anything -- they are only ever an input to the zone rings'
  trim mask, never a target of one.

WHAT IS NOT HERE. render_layout_map.py additionally clips each trimmed zone
ring to the drawn property polygon and drops pieces shorter than
EXCLUSION_FENCE_CLIP_MIN_LENGTH before drawing. Both are render-time
concerns of the printed page (a buffered zone fill can reach past the parcel
edge; a tangent crossing can leave a sliver) and stay in the renderer. The
interactive map carries the off-parcel scrim for the first and draws a
sliver as a sliver for the second.

  ***********************************************************************
  *  DISPLAY ONLY. NOTHING MAY COMPUTE FROM WHAT THESE FUNCTIONS RETURN. *
  ***********************************************************************

The display line is a RENDERING OF A FENCE, NOT THE FENCE. The feature's
own `geometry` remains the real ring, in full; every length -- the per-type
total_length_ft the tabs show, the per-feature length_ft, the narrative
block's figures -- is summed from the REAL UTM geometry in fencing.build_
narrative_data() and never re-measured here or anywhere downstream. So a
trimmed display line and its reported length will LEGITIMATELY DISAGREE:
a tree zone ring that shares half its length with the boundary fence
reports its whole length and draws half of it. That is correct. The commit
body sends `geometry`; the rehydrator reads `geometry` and reproduces the
length from it; the document stores what was sent. The display line never
enters an internal fence dict, never rides an internal geometry field, and
never comes back inbound. It exists only in feature PROPERTIES, under a
name that says so.

SERVER-SIDE, AND THE REASON MATTERS -- the reason display_outline.py gave
for the smoothed production outline, and it holds harder here. A JS port of
these two passes would be a second implementation of a geometric operation,
and those drift; this one is more involved than smoothing (a mutual
difference against a buffered union, with an ordering rule), and nothing
about it has to happen during a gesture. So it happens once, here, on the
server, and both maps read the one answer.

THE CRS. The renderer runs both passes in WEB MERCATOR (EPSG:3857), on the
reprojected rings it is about to draw, with the two tolerances in Mercator
units. The wire side does the same -- reprojects each fence ring from WGS84
to EPSG:3857, runs the identical function, and reprojects the result back --
so the interactive map's display line is the PDF's, not a UTM cousin of it
that would disagree at the vertex level. (Mercator units are metres scaled
by 1/cos(latitude): at the reference parcel's 40.6 N, the 6 m tolerance is
about 4.5 ground metres. That has always been what the PDF drew; agreeing
with it is the point.)
"""


from shapely.geometry import LineString, MultiLineString
from shapely.ops import unary_union

from raster_grid import angular_simplify_closed_ring


# THE FEATURE PROPERTY THE DISPLAY LINE RIDES UNDER, written down once because
# the wire boundary puts it there and a frontend keys on it -- a second
# spelling would be a field nothing reads, silently.
#
# NAMED FOR ITS STATUS, NOT FOR ITS SHAPE, the way display_outline.DISPLAY_
# ONLY_OUTLINE_PROPERTY is: "display_only" is in the identifier because the
# constraint is the important half of what this is -- a rendering of the
# feature's own `geometry`, from which NOTHING MAY COMPUTE. Lengths, the
# commit body, the narrative and every downstream consumer read `geometry`,
# which no producer here touches. A name like "trimmed_geometry" would invite
# exactly the reading this module exists to forbid.
DISPLAY_ONLY_FENCE_LINE_PROPERTY = "display_only_fence_line"

# The CRS both passes run in, on the wire side as in the renderer -- see THE
# CRS in the module docstring. render_layout_map.WEB_MERCATOR is this same
# string; spelled here so this module does not import the renderer.
DISPLAY_CRS = "EPSG:3857"

# DISPLAY-ONLY simplify tolerance for fence rings (pass 1) -- a shapely
# simplify() pass ONLY, no Chaikin/corner-rounding at all: fence lines render
# ANGULAR, not curved, per explicit request. Meaningfully larger than the road
# corridor's own ROAD_RENDER_SIMPLIFY_TOLERANCE_M (2.5 m) so the DEM-resolution
# stairstep zigzags a fence line inherits from its own underlying cell/canopy
# geometry collapse into fewer, longer straight segments rather than just
# having their corners rounded off. Was render_layout_map.FENCE_RENDER_
# ANGULAR_SIMPLIFY_TOLERANCE_M while the PDF was the only consumer, unchanged
# in value; the renderer imports it from here now so the printed line and the
# shipped one cannot drift apart. CONFIGURABLE -- tune by eye against a real
# property.
FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M = 6.0  # was 4.0

# DISPLAY-ONLY coincidence tolerance (pass 2) for trimming a zone fence ring
# where it runs on top of ANOTHER drawn fence ring -- the boundary fence OR
# another zone's fence. Wide enough to catch NEAR-coincident stretches (where
# independent simplification left the rings a few metres apart), not just
# pixel-exact overlap; note that widening it widens the inter-zone gap the
# symmetric trim leaves too. Was render_layout_map.ZONE_FENCE_BOUNDARY_
# COINCIDENCE_TOLERANCE_M, unchanged in value. CONFIGURABLE.
ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M = 5.0  # was 1.0


def simplified_fence_ring(ring, tolerance: float = FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M):
    """
    PASS 1 for one fence feature's geometry: angular_simplify_closed_ring()
    on each closed ring it holds.

    A LineString is one ring and comes back exactly as angular_simplify_
    closed_ring() returns it -- byte-identical to what render_layout_map.py
    computed inline, which is the only reason this is not simply that call.
    A MultiLineString -- a fence around a zone whose bounded render opening
    severed its fill into several parts (fencing._buffer_fill_polygon_to_
    fence_line) -- is simplified PART BY PART and rebuilt, because the
    simplifier re-closes a ring by its coords and a multi-part geometry has
    none of its own; run whole, it raised. Empty parts are dropped.
    """
    if ring is None or ring.is_empty:
        return LineString()
    if ring.geom_type == "MultiLineString":
        parts = [
            angular_simplify_closed_ring(part, tolerance) for part in ring.geoms if not part.is_empty
        ]
        return MultiLineString(parts) if parts else LineString()
    return angular_simplify_closed_ring(ring, tolerance)


def fence_display_lines(
    boundary_rings: list,
    zone_rings: list,
    simplify_tolerance: float = FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M,
    coincidence_tolerance: float = ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M,
) -> tuple:
    """
    Both passes, over every fence ring the map is about to draw, in ONE
    PROJECTED CRS (the caller's -- the renderer and the wire both use
    DISPLAY_CRS). Returns (boundary_display, zone_display): two lists in the
    input order, one geometry per input ring.

    `boundary_rings` are the boundary fence feature geometries (one or more
    closed rings -- the drawn boundary can split the developed footprint's
    fence into several); `zone_rings` are the water-zone and tree-zone
    exclusion fence geometries, every one a closed loop (or a MultiLineString
    of them). Any (Multi)LineString shapely geometry, in the projected CRS.

    PASS 1 simplifies every ring independently (simplified_fence_ring()).
    Boundary rings are then DONE: drawn as-is, never trimmed.

    PASS 2 trims each zone ring by the union of the boundary ring(s) and
    every OTHER zone ring, all taken from pass 1's untouched list, buffered
    by the coincidence tolerance. Deliberately two passes rather than
    trimming inline: the trim is SYMMETRIC and has no ordering, priority or
    rank dependency, so a trimmed result must never be fed into a later
    zone's comparison (that would make zone A's drawn line depend on where
    zone B happened to sit in the list). A zone with no near neighbour and
    no near boundary stretch loses nothing and keeps its full loop; a zone
    that shares its whole length with its neighbours can come back EMPTY,
    and empty is a real answer (the boundary ring is drawing that line).

    Each zone's result is its own geometry -- never merged or unioned with a
    neighbour's into one continuous outline. The difference can come back as
    a LineString, a MultiLineString (a stretch removed from the middle
    splits the loop into pieces) or empty; every caller iterates line parts.

    NOTHING MAY COMPUTE FROM THE RESULT. See the module docstring.
    """
    boundary_display = [simplified_fence_ring(ring, simplify_tolerance) for ring in boundary_rings]
    zone_simplified = [simplified_fence_ring(ring, simplify_tolerance) for ring in zone_rings]

    zone_display = []
    for index, render_ring in enumerate(zone_simplified):
        other_rings = boundary_display + [
            ring for other_index, ring in enumerate(zone_simplified) if other_index != index
        ]
        if other_rings:
            other_rings_union = unary_union(other_rings)
            trimmed_ring = render_ring.difference(other_rings_union.buffer(coincidence_tolerance))
        else:
            trimmed_ring = render_ring
        zone_display.append(trimmed_ring)
    return boundary_display, zone_display


def _line_parts(geometry) -> list:
    """Every non-empty LineString part of a (Multi)LineString or
    GeometryCollection -- the pieces a line consumer can draw. A degenerate
    Point touch is not one."""
    if geometry is None or geometry.is_empty:
        return []
    if geometry.geom_type == "LineString":
        return [geometry]
    if geometry.geom_type in ("MultiLineString", "GeometryCollection"):
        parts = []
        for part in geometry.geoms:
            parts.extend(_line_parts(part))
        return parts
    return []


def display_only_fence_lines_wgs84(features: list, zone_fence_types: tuple) -> list:
    """
    THE WIRE SIDE: for a list of fence-line Features in WGS84 (fencing.py's
    own perimeter_fencing features, in their collection order), the
    DISPLAY-ONLY line of each, as a WGS84 GeoJSON geometry dict rounded to
    the payload's coordinate precision -- or None where the trim left
    nothing to draw. One entry per input feature, in order.

    A feature whose `fence_type` is in `zone_fence_types` is a zone ring and
    is trimmed; every other feature is a boundary ring and is only
    simplified. Both passes run in DISPLAY_CRS on the reprojected rings --
    the renderer's own CRS -- through the ONE fence_display_lines(), so the
    map and the PDF draw the same line.

    ROUNDED LIKE EVERY OTHER COORDINATE ON THE PAYLOAD (production_zone_
    payload.COORDINATE_PRECISION_DP -- 11 cm, an order of magnitude finer
    than the 5 m cell the line is a simplification of). A display field is
    the last thing that should ship at nanometre precision.

    NOTHING MAY COMPUTE FROM THE RESULT.
    """
    from rasterio.warp import transform_geom
    from shapely.geometry import mapping, shape

    from production_zone_payload import _round_geometry

    def to_display_crs(feature):
        geometry = feature.get("geometry")
        if not geometry:
            return LineString()
        return shape(transform_geom("EPSG:4326", DISPLAY_CRS, geometry))

    boundary_index = [i for i, f in enumerate(features) if f["properties"].get("fence_type") not in zone_fence_types]
    zone_index = [i for i, f in enumerate(features) if f["properties"].get("fence_type") in zone_fence_types]
    boundary_display, zone_display = fence_display_lines(
        [to_display_crs(features[i]) for i in boundary_index],
        [to_display_crs(features[i]) for i in zone_index],
    )

    out: list = [None] * len(features)
    for i, display in list(zip(boundary_index, boundary_display)) + list(zip(zone_index, zone_display)):
        parts = _line_parts(display)
        if not parts:
            out[i] = None
            continue
        line = parts[0] if len(parts) == 1 else MultiLineString(parts)
        out[i] = _round_geometry(transform_geom(DISPLAY_CRS, "EPSG:4326", mapping(line)))
    return out
