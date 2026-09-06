"""
display_outline.py

THE DISPLAY-ONLY SMOOTHED OUTLINE of a cell-union zone -- ONE implementation,
read by the PDF's layout map and shipped, as a rendering hint and nothing else,
on the interactive map's production and tree features.

WHAT THE PROBLEM IS. Production zones and tree zones are unions of 5 m DEM
cells. Their edges are therefore pixel boundaries: an unbroken right-angle
staircase that reads as a raster artefact rather than as a field edge. The PDF
has never had that look, because render_layout_map.py has always run its
production clip mask through raster_grid.angular_smooth_polygon() before
clipping contours to it. The interactive map drew the staircase raw, so the two
maps of the same parcel disagreed about what the same zone looks like.

WATER AND ROADS ARE NOT CELL UNIONS AND ARE NOT SMOOTHED. A water zone is a
clipped envelope and a road is a LineString; neither has a staircase, and
running either through a corner-cutter would move geometry for no reason. This
module is called from exactly two producer sites (production_area.py's clusters
by way of production_zone_payload.py, and tree_zone_candidates.py's patches by
way of step_orchestrator.build_trees_payload()) plus render_layout_map.py, and
nowhere else.

  ***********************************************************************
  *  DISPLAY ONLY. NOTHING MAY COMPUTE FROM WHAT THIS FUNCTION RETURNS. *
  ***********************************************************************

The smoothed outline is a RENDERING OF A SHAPE, NOT THE SHAPE. `polygon_utm`
remains the editable source; `render_fill_polygon_utm` remains the derived
fill; cautions, clamping, commit validation, acreage, the exclusion unions the
downstream steps subtract, and every other consumer keep reading the REAL
geometry. The rule is not stylistic. The frontend clips a drawn zone's cautions
against the crossing grounds it was shipped, and those grounds are real
geometry; if the client DREW a smoothed outline while measuring against an
unsmoothed one, a drawn zone could visually miss a crossing it actually
records -- reintroducing exactly the client/server disagreement the
crossing-grounds agreement test closed.

So the smoothed outline never enters a patch dict, never rides an internal
geometry field, and never comes back inbound: rehydration re-derives forward
from an edited source and has no business reconstructing anything from a
display form. It exists only in feature PROPERTIES, under a name that says so.

SERVER-SIDE, AND THE REASON MATTERS. A JS port of this would be a second
implementation of a geometric operation, and those drift. The one place this
project tolerates two implementations of one question -- zoneGeometry.js's
cautionsFor() against step_orchestrator's exclusion crossings -- exists only
because the client cannot round-trip per vertex while a gesture is in flight,
and it took a six-ring agreement test to prove the two agree. Smoothing has no
such justification: nothing about it has to happen during a gesture, so it
happens once, here, on the server.

A DRAWN ZONE IS NOT SMOOTHED, and that is a decision rather than an omission.
A user-drawn zone has no staircase -- its edge was placed vertex by vertex --
so there is no artefact to remove; smoothing it would move the rendered edge
away from the vertices the user placed, which would read as the map disagreeing
with their own clicks and invite them to drag a vertex to fix a curve that is
not in the data. Nothing here is reachable from the rehydration path, so a
drawn zone ships its own outline unchanged by construction.
"""

from shapely.geometry import MultiPolygon
from shapely.ops import unary_union

from raster_grid import angular_smooth_polygon


# THE FEATURE PROPERTY THE OUTLINE RIDES UNDER, written down once because two
# payload builders put it there and a frontend keys on it -- a second spelling
# would be a field nothing reads, silently.
#
# NAMED FOR ITS STATUS, NOT FOR ITS SHAPE. "display_only" is in the identifier
# because the constraint is the important half of what this is: a rendering of
# the feature's own `geometry`, from which NOTHING MAY COMPUTE. Cautions,
# clamping, acreage, commit validation and every downstream consumer read
# `geometry`, which no producer here touches. A name like "smoothed_geometry"
# would invite exactly the reading this module exists to forbid.
DISPLAY_ONLY_OUTLINE_PROPERTY = "display_only_smoothed_outline"


# Douglas-Peucker tolerance for the display outline, in DEM CELLS -- multiplied
# by the DEM's own cell size at the point of use, so it stays "one cell" at any
# resolution instead of hardcoding ~5 m.
#
# A cell union's boundary is a right-angle staircase; simplifying at one cell
# collapses each staircase's collinear run down to the shape's real turns, so
# the Chaikin pass below has actual corners to round rather than hundreds of
# individual cell steps.
#
# ONE VALUE FOR EVERY CELL-UNION LAYER, deliberately. This was
# render_layout_map.PRODUCTION_FILL_SIMPLIFY_TOLERANCE_CELLS while the
# production contour clip was the only consumer. Production zones and tree
# zones are the same kind of geometry gridded at the same resolution, and
# giving each its own tolerance would be two numbers describing one staircase.
# CONFIGURABLE.
DISPLAY_OUTLINE_SIMPLIFY_TOLERANCE_CELLS = 1.0

# Post-simplify Chaikin softening. Kept small deliberately: this geometry
# decides where the PDF's contour lines terminate and what extent the
# interactive map appears to claim, so over-smoothing moves both. Was
# render_layout_map.PRODUCTION_FILL_CHAIKIN_ITERATIONS, unchanged in value.
# CONFIGURABLE -- start light.
DISPLAY_OUTLINE_CHAIKIN_ITERATIONS = 1


def smoothed_display_outline(cell_union_polygon_utm, clip_polygon_utm, cell_size_meters: float):
    """
    The DISPLAY-ONLY smoothed outline of one cell-union zone, in the DEM's own
    UTM CRS. Nothing may compute from the result -- see this module's docstring.

    `cell_union_polygon_utm` is the shape actually drawn for the zone (the
    production opening's render_fill_polygon_utm; for a tree patch, its
    footprint, which that module records under polygon_utm and
    render_fill_polygon_utm alike). `clip_polygon_utm` is the zone's REAL
    footprint.

    RE-CLIPPED TO THE REAL FOOTPRINT, always. Chaikin can push outward at a
    reflex vertex, and a display shape that covered ground the cell gate
    excluded would be the map overstating what qualified. Production's lead
    erode already leaves the fill a full cell inside polygon_utm, so a light
    smooth stays within that slack anyway -- the clip is there to make the
    invariant hard rather than probable. For a tree patch the two arguments are
    the same object and the clip is a no-op on the same geometry.

    POLYGONAL PARTS ONLY. An intersection can in principle return a
    GeometryCollection where the smoothed ring touches the clip tangentially;
    a line or a point is not an outline, contributes nothing to the contour
    clip the PDF uses this for, and is not a shape a wire consumer can draw.
    Dropping the non-polygonal parts here rather than at either call site is
    what keeps the two callers' answers byte-identical.

    DEGRADES, NEVER RAISES, because angular_smooth_polygon() does: a smooth
    that comes back empty or invalid falls back to the unsmoothed input, which
    is then clipped like any other. An empty input comes back empty, and a
    caller decides what "no outline" means on the wire -- both do, by shipping
    None.
    """
    smoothed = angular_smooth_polygon(
        cell_union_polygon_utm,
        DISPLAY_OUTLINE_SIMPLIFY_TOLERANCE_CELLS * cell_size_meters,
        DISPLAY_OUTLINE_CHAIKIN_ITERATIONS,
    )
    return _polygonal_parts(smoothed.intersection(clip_polygon_utm))


def _polygonal_parts(geometry):
    """`geometry` with anything that is not polygonal dropped.

    A Polygon or MultiPolygon passes through AS ITSELF, and the identity is the
    point: it is what makes this guard invisible in the common case, so the
    result is byte-identical to the unguarded intersection render_layout_map.py
    used to compute inline. A GeometryCollection is rebuilt from its polygonal
    members alone (an empty MultiPolygon when it has none). Anything else -- a
    bare LineString from a purely tangential touch, an empty geometry -- has no
    polygonal content at all and comes back as an empty MultiPolygon, which
    angular_smooth_polygon()'s own degradation contract already treats as
    "nothing to draw" rather than as an error.
    """
    if geometry.geom_type in ("Polygon", "MultiPolygon"):
        return geometry
    if geometry.geom_type == "GeometryCollection":
        parts = [part for part in geometry.geoms if part.geom_type in ("Polygon", "MultiPolygon")]
        if parts:
            return unary_union(parts)
    return MultiPolygon()
