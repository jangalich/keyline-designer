"""
render_layout_map.py

Renders a single, high-resolution static map image of a property's final
proposed layout -- the visual counterpart to the narrative Scale of
Permanence report, meant to be the last page of the assembled PDF (see
generate_pdf_report.py). This module does not identify, score, or select
anything itself: every layer it draws is the already-computed output of
production_area_ceiling.py (optimized production zones),
water_suitability.py (fetch_and_select_optimal_water_zone -- the top-ranked
candidate), road_corridors.py (identify_road_corridor_candidates -- the
single selected road corridor road_corridors.py's own ridge-line
identification/scoring picks, see that module's own module docstring),
tree_zone_candidates.py (identify_tree_zone_candidates -- every ranked
tree-suitable candidate patch left over once production/water/road's own
claimed geometry is subtracted out, see that module's own module
docstring), solar_suitability.py (fetch_and_select_optimal_structure_site
-- the top-ranked candidate), hydrology_data.py (real NHD streams, for
background context only -- no soil/hydrology POLYGON data is drawn here,
that's covered in the narrative text), fencing.py
(identify_fencing -- the canopy-aware boundary fence line(s) plus the
road/water/tree-zone-exclusion fence loops added below, see that module's
own module docstring), and contour_lines.py (global elevation contour
lines over the full DEM extent).

    boundary --> dem_data (fetched once, shared across every layer below)
             --> production_area_ceiling.identify_optimized_production_areas
             --> water_suitability.fetch_and_select_optimal_water_zone
             --> road_corridors.identify_road_corridor_candidates
             --> tree_zone_candidates.identify_tree_zone_candidates
             --> solar_suitability.fetch_and_select_optimal_structure_site
             --> hydrology_data.get_water_features_for_boundary (streams)
             --> farm_roads_data.get_farm_roads_for_boundary (existing farm roads)
             --> fencing.identify_fencing (boundary + road/water/tree-zone-exclusion fence loops)
             --> contour_lines.compute_contour_lines (global, unclipped)
             --> rendered PNG (basemap + halo + streams + boundary fence +
                 road/water/tree-zone-exclusion fence loops + layout layers +
                 numbered legend box, all one image)

PRODUCTION ZONE STYLE: production zones render as CONTOUR-LINE TEXTURE,
not a filled/outlined shape -- contour_lines.py's global contour lines
(computed once, over the DEM's full extent) are clipped per zone at
render time (real shapely intersection against that zone's own
render_fill_polygon_utm, not a pre-clipped raster), and only the clipped
segments within that zone are drawn. No fill, no boundary stroke for
production zones -- zone identity is conveyed by the numbered marker
alone, same as every other layer. This is a deliberate, scoped styling
split: the boundary fence (below) has its OWN render-only treatment --
the water zone, the road corridor, and the structure site (all below)
have their OWN render-only treatments too.

BOUNDARY FENCE STYLE: the property boundary itself no longer renders as
a plain solid stroke -- it renders as fencing.identify_fencing()'s own
canopy-aware boundary fence line(s) (see that module's own module
docstring), a thin solid hairline (FENCE_COLOR/FENCE_LINEWIDTH; this used
to be a dashed line, see FENCE_LINEWIDTH's own comment) at the same
zorder=30 this layer's old plain stroke previously occupied.
There is always at least one segment -- fencing.find_boundary_fencing()'s
own plain-wrap case when there's no canopy touching the boundary at all --
so there's no fallback path to the old stroke. Before drawing, each segment's
geometry is run through _angular_simplify_closed_ring() -- a shapely
simplify() pass ONLY (FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M), no Chaikin
corner-cutting at all: fence lines render ANGULAR now, not curved (see that
function's own docstring for why it replaced this module's earlier simplify-
then-Chaikin-smooth treatment), same DISPLAY-ONLY "never touches the real
geometry used elsewhere" discipline as the road corridor's own
_smooth_line_for_render() (which stays curved/unchanged -- a DIFFERENT
feature, the road corridor line itself, not a fence line). When canopy
running end-to-end
splits the parcel into more than one fence loop, each loop gets its own
numbered legend line ("Boundary Fencing 1" / "Boundary Fencing 2") --
otherwise a single unnumbered "Boundary Fencing" line, same "no numbered
circle marker" treatment structure_site's own legend line already uses
(there's no single point on a loop-shaped feature for a circle number to
point to).

ROAD/WATER/TREE EXCLUSION FENCE STYLE: fencing.identify_fencing()'s four
non-boundary fence loops (fence_type "road_corridor_exclusion" -- one
closed loop around the single selected road corridor; "existing_farm_
road_exclusion" -- one closed loop per on-parcel mapped-road segment;
"water_zone_exclusion" -- one closed loop around the single selected
water zone; "tree_zone_exclusion" -- one closed loop per tree zone
candidate, since that layer has no selection step, see fencing.py's own
module docstring) all reuse these SAME boundary-fence styling constants
(FENCE_COLOR/FENCE_LINEWIDTH) and the same _angular_simplify_closed_
ring() + drawing helper, at the same zorder=30 -- all four are fully
enclosed closed loops too, so no new styling or simplify logic is needed.
Unlike the boundary fence, all four ARE clipped to the boundary polygon at
render time (real shapely .intersection(), AFTER angular-simplifying the
WHOLE ring -- simplifying before clipping, not after, since _angular_
simplify_closed_ring() re-closes a genuinely closed ring, which would
wrongly force-close an already-clipped, genuinely OPEN arc piece):
find_road_corridor_fencing()'s own dilated-and-inset footprint naturally
follows the road corridor's own path, which can reach right up to (and
technically just past, once buffered) the property edge where a real road
crosses it, and a water/tree zone's own buffered render_fill_polygon_utm
can likewise sit close enough to the boundary for its own buffer to reach
past the edge -- correct as COMPUTED in every case (see fencing.py), this
clip only trims what gets DRAWN. The boundary fence itself is deliberately
NOT clipped this way -- it's already guaranteed to stay inside the
boundary by construction (see find_boundary_fencing()'s own difference-
against-boundary-polygon logic), so clipping it here would be a redundant
no-op. The intersection can come back as a LineString, MultiLineString, or
GeometryCollection (a ring crossing the boundary at two points splits into
pieces) -- _iter_line_parts() already handles all three -- with any
resulting piece under ROAD_FENCE_CLIP_MIN_LENGTH (a degenerate point/
sliver from a tangent crossing) dropped rather than drawn. All four are
INDEPENDENT of the boundary fence and of each other (deliberately not
spliced/gated into anything -- see fencing.py's own module docstring): a
visible overlap between any of them is expected wherever the underlying
real-world geometry meets, not a rendering bug. "Road Corridor Fencing"
and "Water Zone Fencing" each get a single unnumbered legend line (only
ever one of each, regardless of how many pieces their own clip produces);
"Existing Farm Road Fencing" and "Tree Zone Fencing" each get one legend
line per segment/candidate when more than one exists (same, regardless of
per-segment clip pieces) -- "Tree Zone Fencing {rank}" reuses fencing.
tree_zone_fencing_to_geojson()'s own candidate_rank property directly
rather than re-deriving a new index here, so it lines up with the same-
numbered "Tree Zone Candidate {rank}" this file already draws elsewhere.
All four mirror boundary fencing's own 1-vs-2 segment-labeling convention,
and none gets a numbered circle marker, same "no single point to point
to" reasoning as boundary fencing's own legend lines above.

Contours clip against render_fill_polygon_utm rather than polygon_utm
for two separate reasons layered on top of each other, both from
production_area.py's own module docstring:

  1. A waist split (production_area.py's Part 1) can leave two zones
     directly adjacent with ZERO real distance between their reported
     polygon_utm footprints -- erosion's reclaim step reassigns every
     stripped cell back onto whichever resulting piece is nearest, so
     there's nothing left between them in the real geometry.
     render_polygon_utm (built from each piece's PRE-reclaim cells only)
     fixes this by excluding the whole reclaimed strip from BOTH pieces,
     showing a real, visible gap at the pinch.

  2. Genuinely excluded ground (steep slope or hydric soil) can also sit
     as a small, scattered pocket entirely INSIDE an otherwise-solid
     zone, rendering as an unexplained blank gap in the middle of a
     field. render_fill_polygon_utm is render_polygon_utm's own PLAIN
     CONVEX HULL (see production_area.py's own module docstring for why
     two earlier smoothing attempts -- a raster dilate/erode
     implementation, then a vector buffer round-trip -- were both
     replaced with this) -- a hull necessarily covers any real interior
     pocket/notch, whatever its size, since a pocket is by definition a
     concavity the hull fills in.

render_fill_polygon_utm equals render_polygon_utm (which itself equals
polygon_utm) exactly whenever render_polygon_utm is already convex --
e.g. an ordinary, roughly-rectangular zone with no notches or holes --
so this changes nothing for the common case.

WATER ZONE STYLE: the water zone's FILL is drawn from its own
render_fill_polygon_utm too (water_candidate_zones.find_candidate_zones()'s
own convex hull of the zone's real cell-union footprint, re-intersected
with the parcel boundary) rather than its real, often long/winding/concave
geometry_wgs84 -- same reasoning as production's own hull, applied here
for the same "reads as one coherent shape, not a blocky, notched outline"
purpose. This is DISPLAY-ONLY: the zone's real polygon_utm/geometry_wgs84
(used for scoring, eligibility, and the narrative report) are completely
untouched by this. The fill is drawn fully OPAQUE (alpha=1.0, not the
0.35 an earlier version used) specifically so it occludes any production-
zone contour lines beneath it, then a subtle sine-wave ripple texture is
drawn directly over the opaque fill (matplotlib has no built-in wavy
`hatch` character, so this is hand-drawn as clipped Line2D paths -- see
_ripple_lines_for_polygon()) so the shape reads as water at a glance. The
water zone's hull is allowed to overlap a production zone's own hull at
render time -- that's a display-only coincidence between two convex
hulls, not a real siting conflict; the real geometries stay separated by
water_candidate_zones.py's own production-zone eligibility exclusion gate
(WATER_ZONE_PRODUCTION_SETBACK_METERS), unaffected by anything here.

ROAD CORRIDOR STYLE: rendered as a CASED (double-line) road symbol, the
standard cartographic convention for a road -- a wider, low-alpha dark
gray "shoulder" plotted first, then a narrower, higher-alpha dark gray
line on top of it, both the same color (see _draw_road_corridor()).
Before either line is drawn, the route's own Mercator-projected geometry
is run through _smooth_line_for_render(): a shapely simplify() pass
(ROAD_RENDER_SIMPLIFY_TOLERANCE_M, Douglas-Peucker, preserve_topology=
True) removes the DEM's own per-cell stairstepping, then Chaikin corner-
cutting (ROAD_RENDER_SMOOTHING_ITERATIONS iterations, see
_chaikin_smooth_coords()) rounds what's left. Both are REAL BUG fixes,
found live: the raw route geometry is a literal per-DEM-cell polyline
(road_corridors.py's own _order_fragment_from_entry()/least_cost_path()
walk the DEM's grid one cell at a time), which reads as a visibly
blocky, stairstepped line at the map's actual output resolution rather
than a plausible road alignment. This is DISPLAY-ONLY, same pattern as
the water zone's own hull fill above: road_corridor's real
points_xyz/geometry_wgs84 (used for length_m, avg_grade_pct, and every
other scoring/narrative value) are never touched -- only the copy handed
to the plotting calls is simplified/smoothed.

TREE ZONE STYLE: each ranked tree-zone candidate patch (there can be
several, same "possibly-multiple, ranked" shape as production zones, not
a single selection like water/road/structure) renders as a solid,
hatched fill (TREE_ZONE_COLOR, TREE_ZONE_FILL_ALPHA, TREE_ZONE_HATCH),
drawn from its own render_fill_polygon_utm -- a DISPLAY-ONLY plain convex
hull of the patch's real footprint, re-intersected with the parcel
boundary (score_tree_search_space()'s own output, same field/reasoning
production_area.py's/water_candidate_zones.py's own patches/zones already
carry) -- NOT the patch's real, potentially-notched geometry_wgs84 (used
for area_acres/scoring/the narrative report, completely untouched by
this). Same "reads as one coherent shape at render time" purpose as
production/water's own hulls: most directly here, closing over any
interior pocket the CANOPY EXCLUSION GATE carves out of an otherwise-
contiguous candidate (see tree_zone_candidates.py's own module
docstring), rather than rendering as an unexplained blank notch. The
hatch is a deliberate, visible "candidate, not a committed site" cue,
consistent with this layer's own CONFIDENCE_LOW rating and its explicit
"not a definite planting plan" framing (see tree_zone_candidates.py's own
TREE_ZONE_CONFIDENCE_NOTES_TEMPLATE). Rendered after the road corridor
and before the structure site, matching Scale of Permanence step
ordering (Trees is step 5, immediately before Permanent Buildings' step
6) -- and z-ordered accordingly (below the structure site's own pin icon,
zorder=43), so a real, expected overlap between a tree candidate and the
structure site (tree_zone_candidates.py's own search space deliberately
has no awareness of solar/structure siting, see that module's own
docstring) still reads as the structure site's pin sitting visibly on
top, not lost beneath the tree-candidate fill.

STRUCTURE SITE STYLE: the selected structure site renders as a single,
fixed-size MAP-PIN ICON (assets/icons/farm_location_pin.svg, rasterized
once to assets/icons/farm_location_pin.png -- see STRUCTURE_SITE_ICON_PATH/
STRUCTURE_SITE_ICON above) placed at its own representative_point(), not
a filled polygon over its full eligible footprint (up to the module's own
1-acre cap) and not a numbered circle marker like every other layer --
there's exactly one structure site per property
(fetch_and_select_optimal_structure_site()'s own top-ranked candidate,
same "single selection" shape as water_zone/road_corridor), so a precise
point read at a glance, the way an icon on a printed map is meant to be
read, better matches how a real building site actually gets sited and
referenced than a shaded area does. This is DISPLAY-ONLY, same "real
geometry drives placement, display is separate" split this module already
uses for water/tree zones' own render_fill_polygon_utm above:
structure_site's real geometry (used for area_acres/suitability_score/the
narrative report) is completely untouched by this -- only what gets drawn
at its representative_point() changes. Because there's no numbered circle
for this layer, its legend line carries no leading number either (a
number with nothing on the map to point to would be confusing) -- every
other layer keeps its existing numbered-circle treatment unchanged.
Rendered last (after the tree zone candidates), at zorder=43, the same
z-order this layer already held before this change.

Basemap: NAIP aerial imagery via USGS's cached USGSImageryOnly tile
service, fetched and composited with contextily (a well-established
static-basemap library built for exactly this -- fetch+stitch XYZ tiles
into a georeferenced raster behind a matplotlib axes). Tiles are plain
{z}/{y}/{x} ArcGIS REST tiles, assumed (correctly, for this service) to be
in Web Mercator (EPSG:3857) -- see NAIP_TILE_URL_TEMPLATE below.

Output resolution: 300 DPI at US Letter portrait (8.5in x 11in), i.e.
2550x3300px -- print-quality for the full-bleed final page of the PDF
report (see generate_pdf_report.py), and consistent with the page size
the rest of that document renders at.
"""

import math
import os
from typing import Optional

import contextily as cx
import matplotlib
import mercantile
import numpy as np
import requests
import xyzservices

matplotlib.use("Agg")  # headless rendering -- no display server in this pipeline's runtime
import matplotlib.pyplot as plt
from matplotlib.offsetbox import AnnotationBbox, OffsetImage
from PIL import Image
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely.plotting import plot_line, plot_points, plot_polygon

from contour_lines import compute_contour_lines
from dem_data import get_dem_for_boundary
from farm_roads_data import get_farm_roads_for_boundary
from fencing import identify_fencing
from hydrology_data import get_water_features_for_boundary
from production_area_ceiling import identify_optimized_production_areas
from road_corridors import identify_road_corridor_candidates
from solar_suitability import fetch_and_select_optimal_structure_site
from tree_zone_candidates import identify_tree_zone_candidates
from water_suitability import fetch_and_select_optimal_water_zone

# TEMPORARY: hardcoded to Jordan's reference property until frontend
# anchor-point selection ships. NOT a real default — do not reuse for
# any other property. Remove once callers supply a real user-picked point.
_PLACEHOLDER_REFERENCE_PROPERTY_ANCHOR_LON_LAT = (-79.98356157031265, 40.64303511679458)

WGS84 = "EPSG:4326"
WEB_MERCATOR = "EPSG:3857"

# ArcGIS REST tile path is literally .../tile/{z}/{y}/{x} -- contextily
# substitutes the {x}/{y}/{z} tokens wherever they appear in the string,
# so this works regardless of their order in the URL path.
NAIP_TILE_URL_TEMPLATE = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"

# Confirmed directly against this service's own metadata
# (https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer?f=json):
# tileInfo.lods only defines levels 0-23 -- level 24+ doesn't exist on this
# service and 404s. contextily's own auto-zoom (zoom='auto') has no way to
# know that on its own when `source` is passed as a bare URL string --
# _process_source() wraps a bare string into a TileProvider with no
# max_zoom set, so contextily's own zoom validation falls back to an
# "unknown max, allow up to 30" default and lets an over-high computed
# zoom through uncapped (confirmed live: a small property's extent
# produced zoom 27, a real 404 against this service). Passing `source` as
# an xyzservices.TileProvider with an explicit max_zoom (below) instead of
# a bare string makes contextily's OWN _validate_zoom() clip any
# auto-computed zoom that exceeds this service's real maximum, the same
# way it already does for providers whose max_zoom ships in their own
# metadata -- the extent-based auto calculation itself (small property ->
# higher zoom, large property -> lower zoom) is untouched; only the
# ceiling is now enforced. This must be a real TileProvider, not a plain
# dict: contextily's tile-fetch path calls provider.build_url(...), which
# only TileProvider (not a plain dict, even one with the right keys)
# implements.
#
# tileInfo.lods' 0-23 range is this service's OVERALL documented
# capability, not what's actually cached for any specific point --
# confirmed live: a real rural property's actual cache tops out at zoom
# 16, with every level 17-23 returning an identical tiny 404 placeholder
# (572 bytes), not real image data. The majority of this service's
# coverage is 1m NAIP, not the 6-inch imagery that reaches deeper zooms
# in some areas, so real cache depth varies by location and can't be
# assumed from the documented max alone. See _probe_max_available_zoom()
# below, which live-probes the actual property's own centroid tile by
# tile, starting from this documented max (or the extent-appropriate
# auto zoom, whichever is lower) and stepping down until a genuine tile
# is found -- NAIP_SERVICE_MAX_ZOOM here remains the correct, necessary
# ceiling that starting point can never exceed.
NAIP_SERVICE_MAX_ZOOM = 23

NAIP_TILE_SOURCE = xyzservices.TileProvider(
    name="USGSImageryOnly",
    url=NAIP_TILE_URL_TEMPLATE,
    max_zoom=NAIP_SERVICE_MAX_ZOOM,
    attribution="",
)

# Real "not found" tiles from this service come back as a tiny, fixed-size
# placeholder (confirmed live: 572 bytes) rather than a clean HTTP error in
# every case -- a genuine NAIP tile is tens of KB or more, so this
# threshold sits comfortably between the two with real margin, not a
# hair-trigger cutoff.
MIN_VALID_TILE_BYTES = 4096

# Floor for the zoom-availability probe below: past this point, an empty
# cache says something else is wrong (wrong tile scheme, wrong host, a
# genuinely uncovered region) rather than "just needs a lower zoom," so
# _probe_max_available_zoom() raises instead of continuing to step down.
MIN_PROBE_ZOOM = 10

TILE_PROBE_TIMEOUT_SECONDS = 10.0


def _extent_based_auto_zoom(west: float, south: float, east: float, north: float) -> int:
    """
    Mirrors contextily's own zoom='auto' calculation exactly (see
    contextily.tile._calculate_zoom) -- reimplemented directly here rather
    than importing that private, underscore-prefixed function, since this
    module needs the raw auto-computed value BEFORE calling
    add_basemap() (as the real-tile-availability probe's starting point/
    ceiling below), not just as an opaque 'auto' string handed to
    contextily itself. west/south/east/north are plain lon/lat degrees.
    """
    lon_length = abs(east - west)
    lat_length = abs(north - south)
    zoom_lon = math.ceil(math.log2(360 * 2.0 / lon_length))
    zoom_lat = math.ceil(math.log2(360 * 2.0 / lat_length))
    return min(zoom_lon, zoom_lat)


def _is_real_tile_response(response: "requests.Response") -> bool:
    """A genuine tile: HTTP 200, real image content-type, and a real
    payload size -- this service's own 404s can otherwise look
    deceptively like a normal response (a small, fixed-size placeholder
    body), so status code alone is not a reliable enough check here."""
    if response.status_code != 200:
        return False
    content_type = response.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        return False
    return len(response.content) >= MIN_VALID_TILE_BYTES


def _probe_max_available_zoom(
    centroid_lon: float,
    centroid_lat: float,
    ceiling_zoom: int,
    tile_url_template: str = NAIP_TILE_URL_TEMPLATE,
    min_zoom: int = MIN_PROBE_ZOOM,
) -> int:
    """
    Live-probes this specific property's own real tile cache depth,
    starting at ceiling_zoom (the lower of this service's documented max
    and the extent-appropriate auto zoom -- this function only ever
    steps DOWN from that ceiling, never above it) and fetching one real
    tile at a time at the property's own centroid until a genuine tile is
    found (see _is_real_tile_response()) or min_zoom is reached.

    Raises RuntimeError if no real tile is found down to min_zoom --
    at that point something else is wrong (wrong host/scheme, a
    genuinely uncovered region), not "just needs a lower zoom," and
    should surface as a real failure rather than loop indefinitely. The
    caller (render_layout_map()) already wraps basemap fetching in a
    try/except that degrades gracefully, same as every other
    network-backed layer in this pipeline.
    """
    zoom = ceiling_zoom
    last_zoom_tried = None
    last_status = None
    last_size = None
    while zoom >= min_zoom:
        tile = mercantile.tile(centroid_lon, centroid_lat, zoom)
        url = tile_url_template.format(z=tile.z, y=tile.y, x=tile.x)
        response = requests.get(url, timeout=TILE_PROBE_TIMEOUT_SECONDS)
        if _is_real_tile_response(response):
            return zoom
        last_zoom_tried, last_status, last_size = zoom, response.status_code, len(response.content)
        zoom -= 1
    raise RuntimeError(
        f"No real NAIP tile found for this property down to zoom {min_zoom} "
        f"(last attempt: zoom {last_zoom_tried}, status {last_status}, {last_size} bytes)"
    )

# How far past the property boundary's own bounding box the halo/context
# view extends, as a fraction of the boundary's larger dimension -- gives
# the muted "outside the property" band room to actually read as a band,
# and leaves a bit of surrounding imagery for geographic context.
CONTEXT_PADDING_FRACTION = 0.35

HALO_COLOR = "white"
HALO_ALPHA = 0.55

FIGURE_SIZE_INCHES = (8.5, 11)  # US Letter, portrait -- matches the report's page size
OUTPUT_DPI = 300

STREAM_COLOR = "#3B82C4"
BOUNDARY_COLOR = "#1A1A1A"
PRODUCTION_ZONE_COLOR = "#4C9A2A"
PRODUCTION_ZONE_CONTOUR_LINEWIDTH = 0.7
CONTOUR_LINE_COLOR = "#6B4423"  # muted brown -- traditional topo-map
# contour color, chosen for contrast against green/tan aerial imagery
# and to stay visually distinct from every other layer color already
# in use (production green, water blue, road dark gray, structure red)
WATER_ZONE_COLOR = "#1F6FB2"
# A lighter tint of WATER_ZONE_COLOR, for the ripple texture drawn over
# the opaque fill below -- see _ripple_lines_for_polygon()'s own
# docstring. Distinct from WATER_ZONE_COLOR so the ripples read as a
# texture ON the water fill, not a second, competing shape.
WATER_ZONE_RIPPLE_COLOR = "#7EC1E8"
STRUCTURE_SITE_COLOR = "#D64545"

# Structure site renders as a single fixed-size map-pin icon (see this
# module's own STRUCTURE SITE STYLE docstring section), not a filled
# polygon -- source-of-truth vector asset at assets/icons/farm_location_pin.svg,
# rasterized ONCE to a PNG build artifact (assets/icons/farm_location_pin.png,
# tightly cropped to the drawn shape's own bounds -- not the full 24x24
# viewBox, which this asset's own tip doesn't reach -- so the bottom edge
# of the raster lines up with the pin's visual tip; checked into the repo
# alongside the SVG) rather than re-rasterized on every render call.
# Loaded via PIL and wrapped in an OffsetImage here, at module level, so
# repeated render_layout_map() calls in the same process never hit disk
# for it more than once.
STRUCTURE_SITE_ICON_PATH = os.path.join(os.path.dirname(__file__), "assets", "icons", "farm_location_pin.png")
# Empirically tuned against this module's own real output (300 DPI,
# 8.5in x 11in figure -- see FIGURE_SIZE_INCHES/OUTPUT_DPI above): a zoom
# of ~0.035 rendered this icon ~36px tall at that resolution (the original
# 30-40px legibility target); this is that baseline x3, per explicit
# request, for a more prominent on-map pin. CONFIGURABLE.
STRUCTURE_SITE_ICON_ZOOM = 0.105
STRUCTURE_SITE_ICON = OffsetImage(Image.open(STRUCTURE_SITE_ICON_PATH), zoom=STRUCTURE_SITE_ICON_ZOOM)

# A dark forest green -- deliberately distinct from PRODUCTION_ZONE_COLOR's
# lighter, more saturated green (production is active farm ground; trees
# are a candidate, not-yet-decided layer) and from every other layer color
# already in use. See this module's own TREE ZONE STYLE docstring section
# for why this fill is hatched rather than flat, unlike structure_site's own.
TREE_ZONE_COLOR = "#2D5A27"
TREE_ZONE_FILL_ALPHA = 0.45
TREE_ZONE_HATCH = "///"

MARKER_FACE_COLOR = "#1A1A1A"
MARKER_TEXT_COLOR = "white"
MARKER_RADIUS_POINTS = 11

# Rendering-only geometry cleanup for the road corridor's own LineString
# (see _smooth_line_for_render()) -- never applied to road_corridors.py's
# real points_xyz/geometry_wgs84 (used for length_m/avg_grade_pct/every
# other scoring value), only to the copy handed to the plotting calls
# below. Deliberately small/few: over-simplifying or over-smoothing risks
# cutting a real turn in the route, not just its per-cell stairsteps.
# CONFIGURABLE.
ROAD_RENDER_SIMPLIFY_TOLERANCE_M = 2.5
ROAD_RENDER_SMOOTHING_ITERATIONS = 2

# Cased (double-line) road style -- a wider, low-alpha "shoulder" drawn
# first, then a narrower, higher-alpha line on top of it, both this same
# dark-gray color (see _draw_road_corridor()). CONFIGURABLE.
ROAD_RENDER_COLOR = "#3A3A3A"
ROAD_RENDER_OUTER_WIDTH = 3.0
ROAD_RENDER_INNER_WIDTH = 1.5
ROAD_RENDER_OUTER_ALPHA = 0.35
ROAD_RENDER_INNER_ALPHA = 0.7

# Boundary fencing (fencing.identify_fencing()'s own "perimeter_fencing" layer,
# fence_type="boundary") renders as a solid hairline -- a fine, subtle fence line,
# not the heavy solid cartographic boundary stroke this layer replaces (see
# render_layout_map()'s own docstring for why that stroke was removed outright, not
# kept as a fallback). A mustard yellow, chosen to read clearly against both green
# production-zone contours and tan/green aerial imagery, and distinct from every
# other layer color already in use (production green #4C9A2A, water blue #1F6FB2,
# road dark gray #3A3A3A, structure red #D64545, contour brown #6B4423, tree dark
# green #2D5A27). The two road-exclusion fence loops (fence_type
# "road_corridor_exclusion" / "existing_farm_road_exclusion") reuse this SAME
# color/linewidth -- no new styling constants needed, see this module's own ROAD
# EXCLUSION FENCE STYLE docstring section. CONFIGURABLE.
FENCE_COLOR = "#D4A017"
FENCE_LINEWIDTH = 0.6  # a hairline -- was 1.2 (a dashed line before that)

# Below this length (Web Mercator units, ~meters), a road-exclusion fence piece
# produced by clipping to the boundary polygon at render time (see ROAD EXCLUSION
# FENCE STYLE above) is a degenerate point/sliver from a tangent crossing, not a
# real segment worth drawing.
ROAD_FENCE_CLIP_MIN_LENGTH = 0.5

# DISPLAY-ONLY simplify tolerance for fence rings (see _angular_simplify_closed_ring()) --
# a shapely simplify() pass ONLY, no Chaikin/corner-rounding at all: fence lines render
# ANGULAR now, not curved, per explicit request. Meaningfully larger than the road
# corridor's own ROAD_RENDER_SIMPLIFY_TOLERANCE_M (2.5m) so the DEM-resolution stairstep
# zigzags a fence line inherits from its own underlying cell/canopy geometry collapse into
# fewer, longer straight segments rather than just having their corners rounded off.
# CONFIGURABLE -- tune by eye against a real property.
FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M = 4.0


def _reproject_geometry_to_mercator(geometry_wgs84: dict):
    """geometry_wgs84 is a GeoJSON geometry dict in WGS84 (the shape every
    layer module in this pipeline already produces) -- reprojects it to
    Web Mercator (matching the basemap tiles' own CRS) and returns it as a
    shapely geometry, ready to draw directly on the map axes."""
    geometry_3857 = transform_geom(WGS84, WEB_MERCATOR, geometry_wgs84)
    return shape(geometry_3857)


def _reproject_utm_geometry_to_mercator(geometry_utm, source_crs: str):
    """geometry_utm is a shapely geometry already in `source_crs` (a
    DEM's own UTM zone, e.g. contour_lines.py's lines_utm clipped against
    a production zone's own render_fill_polygon_utm -- both already share
    that same CRS, so no WGS84 round-trip is needed first) -- reprojects
    directly to Web Mercator (one hop) and returns it as a shapely
    geometry, ready to draw."""
    geometry_3857 = transform_geom(source_crs, WEB_MERCATOR, mapping(geometry_utm))
    return shape(geometry_3857)


def _iter_line_parts(geometry):
    """Yields real LineString parts out of a LineString/MultiLineString/
    GeometryCollection -- shapely's intersection() of a (Multi)LineString
    against a (Multi)Polygon can legitimately come back as any of those,
    or mix in degenerate Point touches where a line only grazes the
    polygon's boundary; only real line segments are worth drawing."""
    if geometry.is_empty:
        return
    if geometry.geom_type == "LineString":
        yield geometry
    elif geometry.geom_type == "MultiLineString":
        yield from geometry.geoms
    elif geometry.geom_type == "GeometryCollection":
        for part in geometry.geoms:
            yield from _iter_line_parts(part)


# Ripple-texture tuning for the water zone's opaque fill -- deliberately
# subtle (few waves, low amplitude) so this reads as "this is water" at a
# glance rather than a decorative pattern competing with the numbered
# marker drawn on top of it (zorder=50, well above the ripples).
WATER_ZONE_RIPPLE_COUNT = 4
WATER_ZONE_RIPPLE_AMPLITUDE_FRACTION = 0.06  # fraction of the polygon's own bounding-box height
WATER_ZONE_RIPPLE_WAVELENGTH_FRACTION = 0.5  # fraction of the polygon's own bounding-box width, per full sine cycle
WATER_ZONE_RIPPLE_LINEWIDTH = 1.0
WATER_ZONE_RIPPLE_ALPHA = 0.6
WATER_ZONE_RIPPLE_SAMPLES = 200  # points sampled along each wave before clipping to the polygon


def _ripple_lines_for_polygon(polygon) -> list:
    """
    Generates WATER_ZONE_RIPPLE_COUNT evenly-spaced horizontal sine-wave
    lines across `polygon`'s own bounding box, each clipped to the real
    polygon shape (shapely intersection, not a rectangular crop) -- a
    lightweight water-texture hatch drawn over the solid opaque fill,
    not a real second geometry. Matplotlib has no built-in wavy `hatch`
    character, so this draws the waves directly as clipped LineStrings
    instead.

    Returns a list of clipped (Multi)LineString/GeometryCollection
    geometries (possibly empty if the polygon is degenerate) -- callers
    should iterate real line parts out of each via _iter_line_parts()
    before drawing, same as every other clipped-line consumer in this
    module (e.g. the production-zone contour clipping above).
    """
    minx, miny, maxx, maxy = polygon.bounds
    height = maxy - miny
    width = maxx - minx
    if height <= 0 or width <= 0:
        return []

    amplitude = height * WATER_ZONE_RIPPLE_AMPLITUDE_FRACTION
    wavelength = width * WATER_ZONE_RIPPLE_WAVELENGTH_FRACTION
    if wavelength <= 0:
        return []

    xs = np.linspace(minx, maxx, WATER_ZONE_RIPPLE_SAMPLES)
    clipped_lines = []
    for i in range(WATER_ZONE_RIPPLE_COUNT):
        # Evenly spaced across the polygon's own vertical extent (not
        # hugging the top/bottom edge) -- (i + 1) / (count + 1) puts
        # WATER_ZONE_RIPPLE_COUNT waves at fractions 1/(n+1) .. n/(n+1)
        # of the bounding-box height.
        y_center = miny + height * (i + 1) / (WATER_ZONE_RIPPLE_COUNT + 1)
        ys = y_center + amplitude * np.sin(2 * np.pi * (xs - minx) / wavelength)
        wave_line = LineString(zip(xs, ys))
        clipped = wave_line.intersection(polygon)
        if not clipped.is_empty:
            clipped_lines.append(clipped)
    return clipped_lines


def _chaikin_smooth_coords(coords: list[tuple[float, float]], iterations: int) -> list[tuple[float, float]]:
    """
    Chaikin's corner-cutting subdivision, run `iterations` times, over an
    OPEN polyline (a road corridor, not a closed ring) -- simple enough
    to not need a new spline/smoothing dependency for what's purely a
    cosmetic rendering touch-up (see _smooth_line_for_render()).

    The first and last coordinates are always kept EXACTLY as given, so
    a smoothed route still starts/ends at the same anchor/ridge-end point
    -- only the interior gets rounded. Each iteration replaces every edge
    (Pi, Pi+1) with two points at 1/4 and 3/4 along it (the standard
    Chaikin construction), which is what visually rounds a sharp corner:
    each cut moves the curve a little further off the original corner and
    a little closer to a smooth arc through it.

    A no-op below 3 points, or once an iteration's own input drops below
    3 points -- there's no interior corner left to cut on a 2-point
    (straight) line.
    """
    for _ in range(iterations):
        if len(coords) < 3:
            break
        smoothed = [coords[0]]
        for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
            smoothed.append((0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1))
            smoothed.append((0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1))
        smoothed.append(coords[-1])
        coords = smoothed
    return coords


def _smooth_line_for_render(line: LineString) -> LineString:
    """
    Rendering-only transform for the road corridor's own LineString --
    REAL BUG, FOUND LIVE: road_corridors.py's route geometry is a literal
    per-DEM-cell polyline (_order_fragment_from_entry()/least_cost_path()
    both walk the DEM's grid one cell at a time), which reads as a
    visibly blocky, stairstepped line at this map's actual output
    resolution rather than a plausible road alignment.

    Two passes, both deliberately small/subtle (see
    ROAD_RENDER_SIMPLIFY_TOLERANCE_M/ROAD_RENDER_SMOOTHING_ITERATIONS'
    own comments -- over-doing either risks cutting a real turn in the
    route, not just its per-cell stairsteps):
      1. shapely simplify() (Douglas-Peucker, preserve_topology=True so
         it can't collapse the line into something degenerate) removes
         the redundant near-collinear stairstep vertices first.
      2. _chaikin_smooth_coords() then rounds what corners are left.

    This is DISPLAY-ONLY -- the caller passes in a geometry ALREADY
    reprojected to Web Mercator for plotting (see
    _reproject_geometry_to_mercator()), never road_corridors.py's own
    points_xyz/geometry_wgs84 (used for length_m/avg_grade_pct/every
    other scoring/narrative value), and this function's own return value
    is used for drawing only, never fed back into anything else.
    """
    simplified = line.simplify(ROAD_RENDER_SIMPLIFY_TOLERANCE_M, preserve_topology=True)
    smoothed_coords = _chaikin_smooth_coords(list(simplified.coords), ROAD_RENDER_SMOOTHING_ITERATIONS)
    return LineString(smoothed_coords)


def _draw_road_corridor(ax, line: LineString) -> None:
    """
    Draws `line` (already smoothed -- see _smooth_line_for_render()) as a
    CASED (double-line) road symbol, the standard cartographic convention
    for a road: a wider, low-alpha dark-gray "shoulder" underneath, then a
    narrower, higher-alpha dark-gray line on top of it, both the same
    ROAD_RENDER_COLOR -- two plot calls over the same geometry, not one
    styled line, so the road reads as a real symbol rather than a flat
    highlighted path. zorder puts the inner line just above the outer
    shoulder, same +0.5 pattern the water zone's ripple texture (zorder
    41 fill / 41.5 ripple) already uses.
    """
    plot_line(
        line,
        ax=ax,
        add_points=False,
        color=ROAD_RENDER_COLOR,
        linewidth=ROAD_RENDER_OUTER_WIDTH,
        alpha=ROAD_RENDER_OUTER_ALPHA,
        zorder=42,
    )
    plot_line(
        line,
        ax=ax,
        add_points=False,
        color=ROAD_RENDER_COLOR,
        linewidth=ROAD_RENDER_INNER_WIDTH,
        alpha=ROAD_RENDER_INNER_ALPHA,
        zorder=42.5,
    )


def _angular_simplify_closed_ring(ring: LineString, tolerance: float) -> LineString:
    """
    DISPLAY-ONLY transform for a fence loop's own LineString (boundary,
    road-corridor-exclusion, or existing-farm-road-exclusion -- all three
    fence types share this one helper). Replaces this module's earlier
    simplify-then-Chaikin-smooth treatment: fence lines now render
    ANGULAR, not curved, per explicit request -- so this is shapely
    .simplify(tolerance, preserve_topology=True) on the ring ONLY, no
    Chaikin call, zero smoothing iterations. Deliberately simpler than
    the helper it replaces, not a curved-then-de-curved round trip.

    simplify() can drop the duplicate closing coordinate (coords[0] ==
    coords[-1]) that every ring this module receives arrives with -- the
    same closure bookkeeping the helper this replaces already had to do,
    re-applied here: re-close the ring afterward if simplify() dropped it.

    Never touches the real geometry used elsewhere (fencing.py's own
    fencing_geojson, used for the narrative report) -- the caller passes
    in a copy already reprojected to Web Mercator for plotting, and this
    function's return value is used for drawing only.
    """
    simplified = ring.simplify(tolerance, preserve_topology=True)
    coords = list(simplified.coords)
    if coords[0] != coords[-1]:
        coords.append(coords[0])
    return LineString(coords)


def _draw_boundary_fence(ax, ring: LineString) -> None:
    """Draws `ring` (already simplified -- see _angular_simplify_closed_ring())
    as a single solid hairline fence line, FENCE_COLOR/FENCE_LINEWIDTH -- no
    dash pattern (this used to be dashed; now a fine, subtle solid line)."""
    plot_line(
        ring,
        ax=ax,
        add_points=False,
        color=FENCE_COLOR,
        linewidth=FENCE_LINEWIDTH,
        linestyle="solid",
        zorder=30,
    )


def _boundary_polygon_mercator(boundary_coordinates: list[tuple[float, float]]) -> Polygon:
    lons = [pt[0] for pt in boundary_coordinates]
    lats = [pt[1] for pt in boundary_coordinates]
    xs, ys = warp_transform(WGS84, WEB_MERCATOR, lons, lats)
    return Polygon(zip(xs, ys))


def _draw_numbered_marker(ax, point, number: int) -> None:
    ax.annotate(
        str(number),
        xy=(point.x, point.y),
        xycoords="data",
        ha="center",
        va="center",
        fontsize=10,
        fontweight="bold",
        color=MARKER_TEXT_COLOR,
        zorder=50,
        bbox=dict(boxstyle="circle,pad=0.35", facecolor=MARKER_FACE_COLOR, edgecolor="white", linewidth=1.2),
    )


def _production_zone_legend_stats(production_result: dict) -> list[tuple[float, str]]:
    """Returns [(area_acres, stat_line), ...] -- one per surviving
    production patch, in the same order production_result['scored_patches']
    already ranks them. parcel_acres is back-derived from the pipeline's
    own reported total_selected_acreage / percent_of_parcel (both already
    computed by production_area_ceiling.py) rather than recomputed here,
    so this stays purely a display-formatting step."""
    scored_patches = production_result["scored_patches"]
    percent_of_parcel = production_result.get("percent_of_parcel") or 0.0
    total_selected_acreage = production_result.get("total_selected_acreage") or 0.0
    parcel_acres = (total_selected_acreage / (percent_of_parcel / 100.0)) if percent_of_parcel else None

    stats = []
    for patch in scored_patches:
        pct_note = (
            f" ({round(patch['area_acres'] / parcel_acres * 100)}% of parcel)"
            if parcel_acres
            else ""
        )
        stats.append((patch["area_acres"], f"{patch['area_acres']} ac{pct_note}"))
    return stats


def fetch_layout_layers(boundary_coordinates: list[tuple[float, float]], dem: Optional[dict] = None) -> dict:
    """
    Fetches/derives every layer render_layout_map() draws. Fetches the DEM
    once (unless one is passed in) and shares it across all four
    optimize/select calls below, rather than letting each independently
    re-fetch the same DEM -- a pure efficiency choice, doesn't change any
    of their identification/scoring/selection logic.

    water_zone reuses identify_water_suitability()'s own selected_water_zone
    field directly (see fetch_and_select_optimal_water_zone()) -- that field
    and this function's return value are the exact same scored-zone dict
    shape, so there was real duplicate "pick the best" logic to remove.
    structure_site, by contrast, is read here as a GeoJSON Feature (via
    identify_solar_candidate_zones()'s own zones_geojson, same as every
    other GeoJSON-shaped input this module reprojects with
    _reproject_geometry_to_mercator()) rather than its identify_*()'s
    newer selected_structure_site field -- that's a DIFFERENT shape (a raw
    scored dict carrying UTM shapely geometry: polygon_utm, un-reprojected,
    0-1 score), so using it here would mean adding one-off reprojection/
    property-translation code for zero fetch-count benefit (both paths
    still call the same identify_*() exactly once). Left as GeoJSON for
    now; worth reconsidering only if a future caller needs the raw UTM
    geometry too.

    road_corridor, like water_zone/structure_site, is read here as a
    GeoJSON Feature (via road_corridors.identify_road_corridor_candidates()'s
    own zones_geojson features[0]) -- the single road corridor road_
    corridors.py's own ridge-line identification and scoring selects, not
    a list of alternates: that module's current design produces exactly
    one candidate per property (see its own module docstring), so there's
    nothing left to rank/discard here the way an earlier fan-based version
    of this module needed to. Called directly (identify_road_corridor_
    candidates(), not the fetch_and_select_optimal_road_corridor()
    convenience wrapper this function used before) so this same call can
    also pull the winning candidate's own RAW 'cells' field (road_
    corridors.find_road_routes()'s own real, walk-ordered path cells) for
    fencing.identify_fencing()'s road-corridor-exclusion fence below --
    that convenience wrapper only ever returns the schema-wrapped GeoJSON
    Feature, which doesn't carry 'cells'. No new fetch: this is the exact
    same underlying computation the old wrapper call already made.

    tree_zone_result reuses identify_tree_zone_candidates()'s own full
    return dict directly (like production_result above, not narrowed down
    to a single selection -- there can be several ranked tree-zone
    candidates, same shape as production). This call independently
    recomputes production/water/road's own candidate geometry a second
    time internally (identify_tree_zone_candidates()'s own Step 1 needs
    it to build its search space) -- the same "each optimize/select call
    below re-derives its own upstream dependencies, only the DEM fetch
    itself is shared" pattern every other call in this function already
    follows (e.g. fetch_and_select_optimal_road_corridor() already
    re-runs identify_optimized_production_areas() and
    fetch_and_select_optimal_water_zone() internally); not a new
    inefficiency this call introduces.

    contour_lines is contour_lines.compute_contour_lines()'s own output --
    GLOBAL elevation contour lines over the DEM's full extent, computed
    ONCE here and shared across every production zone at render time
    (each zone clips its own segments out of this same list via real
    shapely intersection against its own render_fill_polygon_utm -- see
    render_layout_map()'s own docstring), rather than recomputing contour
    lines per zone.

    fencing_result is fencing.identify_fencing()'s own output -- the
    canopy-aware boundary fence line(s) that replace this module's former
    plain boundary stroke, PLUS the road-exclusion, water-zone-exclusion,
    and tree-zone-exclusion fence loops (see render_layout_map()'s own
    docstring). NOT wrapped in try/except itself: identify_fencing()'s own
    boundary-fence path still carries its MANDATORY canopy fetch (same
    hard-fail-on-missing-coverage design as production_result/tree_zone_
    result's own canopy gates above), and a failure there should
    propagate up and fail this whole render rather than silently omitting
    the fence layer. Fed selected_road_corridor_cells (the winning road
    corridor's own real path cells, already pulled above -- no second
    fetch), farm_road_features (farm_roads_data.get_farm_roads_for_
    boundary(), fetched just below), and the water_zone's/tree_zone_
    result's own already-fetched render_fill_polygon_utm value(s) (both
    already sitting in memory from this function's own water/tree zone
    fetches above -- again no second fetch, and NOT a re-run of either
    layer's own siting/scoring logic). identify_fencing() degrades
    gracefully on its own if the farm-road fetch failed, so this function
    only needs to protect that ONE fetch, not the whole identify_fencing()
    call.
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    production_result = identify_optimized_production_areas(boundary_coordinates, dem=dem)
    water_zone = fetch_and_select_optimal_water_zone(boundary_coordinates, dem=dem)
    road_corridor_candidates = identify_road_corridor_candidates(
        boundary_coordinates, dem=dem, anchor_lon_lat=_PLACEHOLDER_REFERENCE_PROPERTY_ANCHOR_LON_LAT
    )
    road_corridor_features = road_corridor_candidates["zones_geojson"]["features"]
    road_corridor = road_corridor_features[0] if road_corridor_features else None
    selected_road_corridor = road_corridor_candidates["selected_road_corridor"]
    selected_road_corridor_cells = selected_road_corridor["cells"] if selected_road_corridor else None
    tree_zone_result = identify_tree_zone_candidates(boundary_coordinates, dem=dem)
    structure_site = fetch_and_select_optimal_structure_site(boundary_coordinates, dem=dem)
    water_features = get_water_features_for_boundary(boundary_coordinates)
    contour_lines = compute_contour_lines(dem)

    try:
        # Same graceful-degrade pattern every other non-canopy network fetch
        # in this file already uses -- an existing-farm-road outage shouldn't
        # take down the whole render; identify_fencing() below still produces
        # road-corridor-exclusion fencing normally, just omitting the
        # existing-farm-road loops.
        farm_road_features = get_farm_roads_for_boundary(boundary_coordinates)
    except Exception as e:
        print(f"  fetch_layout_layers: farm road fetch failed ({e}), continuing without existing-farm-road fencing.")
        farm_road_features = []

    selected_water_zone_render_fill_polygon_utm = water_zone["render_fill_polygon_utm"] if water_zone else None
    tree_zone_render_fill_polygons_utm = [
        patch["render_fill_polygon_utm"] for patch in tree_zone_result.get("patches", [])
    ]

    fencing_result = identify_fencing(
        boundary_coordinates,
        dem=dem,
        selected_road_corridor_cells=selected_road_corridor_cells,
        farm_road_features=farm_road_features,
        selected_water_zone_render_fill_polygon_utm=selected_water_zone_render_fill_polygon_utm,
        tree_zone_render_fill_polygons_utm=tree_zone_render_fill_polygons_utm,
    )

    return {
        "dem": dem,
        "production_result": production_result,
        "water_zone": water_zone,
        "road_corridor": road_corridor,
        "tree_zone_result": tree_zone_result,
        "structure_site": structure_site,
        "water_features": water_features,
        "contour_lines": contour_lines,
        "fencing_result": fencing_result,
    }


def render_layout_map(
    boundary_coordinates: list[tuple[float, float]],
    output_path: str,
    dem: Optional[dict] = None,
    layers: Optional[dict] = None,
) -> str:
    """
    Renders the final proposed layout as a single high-resolution PNG at
    output_path and returns that same path.

    layers (optional): a pre-fetched fetch_layout_layers() result, so a
    caller that already fetched the DEM/layers for other purposes (e.g.
    generate_pdf_report.py, which also needs them alongside the narrative
    report's own data fetches) doesn't pay for a second, redundant fetch.
    Omit it (default) to have this function fetch everything itself.
    """
    if layers is None:
        layers = fetch_layout_layers(boundary_coordinates, dem=dem)

    dem = layers["dem"]
    production_result = layers["production_result"]
    water_zone = layers["water_zone"]
    road_corridor = layers["road_corridor"]
    tree_zone_result = layers["tree_zone_result"]
    structure_site = layers["structure_site"]
    water_features = layers["water_features"]
    contour_lines = layers["contour_lines"]
    fencing_result = layers["fencing_result"]

    boundary_polygon = _boundary_polygon_mercator(boundary_coordinates)
    minx, miny, maxx, maxy = boundary_polygon.bounds
    pad = CONTEXT_PADDING_FRACTION * max(maxx - minx, maxy - miny)
    context_box = box(minx - pad, miny - pad, maxx + pad, maxy + pad)

    # The halo mask itself: the real geometric difference between the
    # padded context box and the boundary polygon (shapely.difference(),
    # the same operation the rest of this pipeline already uses for
    # exclusion geometry) -- not a crop or a vignette.
    halo_mask = context_box.difference(boundary_polygon)

    fig, ax = plt.subplots(figsize=FIGURE_SIZE_INCHES)
    ax.set_xlim(minx - pad, maxx + pad)
    ax.set_ylim(miny - pad, maxy + pad)
    ax.set_aspect("equal")
    ax.set_axis_off()

    try:
        corner_lons, corner_lats = warp_transform(
            WEB_MERCATOR, WGS84, [minx - pad, maxx + pad], [miny - pad, maxy + pad]
        )
        west, east = corner_lons
        south, north = corner_lats
        auto_zoom = _extent_based_auto_zoom(west, south, east, north)
        ceiling_zoom = min(auto_zoom, NAIP_SERVICE_MAX_ZOOM)

        centroid = Polygon(boundary_coordinates).representative_point()
        resolved_zoom = _probe_max_available_zoom(centroid.x, centroid.y, ceiling_zoom)
        print(f"  Basemap zoom: extent-appropriate ceiling {ceiling_zoom}, real available zoom {resolved_zoom}")

        cx.add_basemap(ax, source=NAIP_TILE_SOURCE, zoom=resolved_zoom, crs=WEB_MERCATOR, attribution=False)
        basemap_note = None
    except Exception as e:
        # A NAIP tile-service outage or network restriction shouldn't
        # crash PDF generation -- same fetch-degrades-gracefully
        # reasoning as every other network-backed layer in this pipeline
        # (imagery_data.py, water_candidate_zones.py, etc). Falls back to
        # a neutral fill (drawn as an explicit patch, not ax.set_facecolor()
        # -- ax.set_axis_off() below suppresses the axes' own background
        # patch) so every other layer, including the halo overlay, still
        # renders with real visible contrast against something.
        plot_polygon(context_box, ax=ax, add_points=False, facecolor="#DCD8CE", edgecolor="none", zorder=1)
        basemap_note = f"basemap unavailable ({e})"

    # z-order, back to front, per spec: halo mask, streams, boundary fence,
    # production zone(s), water zone, road corridor, structure site.
    if not halo_mask.is_empty:
        plot_polygon(halo_mask, ax=ax, add_points=False, facecolor=HALO_COLOR, edgecolor="none", alpha=HALO_ALPHA, zorder=10)

    for stream in water_features.get("streams", []):
        if not stream.get("geometry"):
            continue
        stream_geom = _reproject_geometry_to_mercator(stream["geometry"])
        if stream_geom.geom_type == "LineString":
            plot_line(stream_geom, ax=ax, add_points=False, color=STREAM_COLOR, linewidth=1.0, alpha=0.8, zorder=20)
        elif stream_geom.geom_type == "MultiLineString":
            for line in stream_geom.geoms:
                plot_line(line, ax=ax, add_points=False, color=STREAM_COLOR, linewidth=1.0, alpha=0.8, zorder=20)

    legend_entries: list[str] = []
    marker_number = 1

    # Boundary fencing (fencing.identify_fencing()'s own "perimeter_fencing" layer,
    # fence_type="boundary") replaces this module's former plain boundary stroke
    # outright -- no fallback to the old solid line, since this layer always returns
    # at least the plain-boundary-equivalent loop (see fencing.find_boundary_fencing()'s
    # own docstring for the no-canopy case). DISPLAY-ONLY angular simplify
    # (_angular_simplify_closed_ring(), same "never touches the real geometry used
    # elsewhere" discipline as the road corridor's own _smooth_line_for_render()) --
    # fencing_result['fencing_geojson'] (used for the narrative report) is untouched.
    fencing_features = fencing_result["fencing_geojson"]["features"]
    boundary_fence_features = [f for f in fencing_features if f["properties"].get("fence_type") == "boundary"]
    segment_count = fencing_result["segment_count"]
    multiple_fence_segments = segment_count > 1
    for i, feature in enumerate(boundary_fence_features, start=1):
        fence_geom = _reproject_geometry_to_mercator(feature["geometry"])
        render_ring = _angular_simplify_closed_ring(fence_geom, FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M)
        _draw_boundary_fence(ax, render_ring)
        label = f"Boundary Fencing {i}" if multiple_fence_segments else "Boundary Fencing"
        legend_entries.append(label)

    # Everything-else fencing (fencing.identify_fencing()'s own "road_corridor_exclusion" /
    # "existing_farm_road_exclusion" / "water_zone_exclusion" / "tree_zone_exclusion"
    # fence_types, same "perimeter_fencing" layer) -- all four are fully enclosed closed
    # loops (per their own spec), so the exact same angular-simplify + drawing helper as
    # boundary fencing applies unchanged (see this module's own ROAD EXCLUSION FENCE STYLE
    # docstring section -- water/tree zone fencing reuses that same treatment: water/tree
    # zones can sit close enough to the boundary that a clip may matter, same reasoning as
    # roads, so it's applied uniformly here rather than special-cased out). Looped over
    # generically rather than as four near-duplicate blocks; only the legend label differs
    # per fence_type. INDEPENDENT of the boundary fence and of each other -- an overlap
    # between any of them is expected, not a bug. Unlike the boundary fence, each
    # simplified ring is clipped to boundary_polygon (render-only -- see this module's own
    # ROAD EXCLUSION FENCE STYLE docstring section for why) AFTER angular-simplifying it,
    # same order as the prior render-polish pass -- _angular_simplify_closed_ring()
    # re-closes a genuinely closed ring, which would be WRONG if applied to an already-
    # clipped, genuinely OPEN arc piece (it would force-close a real arc into a bogus
    # loop), so the whole ring is simplified first, while it's still guaranteed closed,
    # and only THEN clipped into however many open/closed pieces the boundary crossing
    # produces. The clip can split one ring into several line pieces, all drawn, but the
    # legend still gets exactly one line per FEATURE regardless.
    extra_fence_features = [
        f
        for f in fencing_features
        if f["properties"].get("fence_type")
        in ("road_corridor_exclusion", "existing_farm_road_exclusion", "water_zone_exclusion", "tree_zone_exclusion")
    ]
    multiple_farm_road_segments = (
        sum(1 for f in extra_fence_features if f["properties"]["fence_type"] == "existing_farm_road_exclusion") > 1
    )
    multiple_tree_zone_fences = (
        sum(1 for f in extra_fence_features if f["properties"]["fence_type"] == "tree_zone_exclusion") > 1
    )
    farm_road_segment_index = 0
    for feature in extra_fence_features:
        fence_geom = _reproject_geometry_to_mercator(feature["geometry"])
        render_ring = _angular_simplify_closed_ring(fence_geom, FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M)
        clipped_ring = render_ring.intersection(boundary_polygon)
        for line in _iter_line_parts(clipped_ring):
            if line.length > ROAD_FENCE_CLIP_MIN_LENGTH:
                _draw_boundary_fence(ax, line)
        fence_type = feature["properties"]["fence_type"]
        if fence_type == "road_corridor_exclusion":
            legend_entries.append("Road Corridor Fencing")
        elif fence_type == "existing_farm_road_exclusion":
            farm_road_segment_index += 1
            label = (
                f"Existing Farm Road Fencing {farm_road_segment_index}"
                if multiple_farm_road_segments
                else "Existing Farm Road Fencing"
            )
            legend_entries.append(label)
        elif fence_type == "water_zone_exclusion":
            legend_entries.append("Water Zone Fencing")
        else:  # tree_zone_exclusion -- fencing.tree_zone_fencing_to_geojson()'s own
            # 1-based candidate_rank property, reused directly rather than re-deriving
            # a new index here, so "Tree Zone Fencing N" lines up with the same-numbered
            # "Tree Zone Candidate N" this file already draws elsewhere.
            rank = feature["properties"]["candidate_rank"]
            label = f"Tree Zone Fencing {rank}" if multiple_tree_zone_fences else "Tree Zone Fencing"
            legend_entries.append(label)

    scored_patches = production_result.get("scored_patches", []) if production_result else []
    zone_stats = _production_zone_legend_stats(production_result) if production_result else []
    multiple_zones = len(scored_patches) > 1
    for patch, (_, stat_line) in zip(scored_patches, zone_stats):
        # geometry_wgs84 -- the real, grid-bug-fixed cell-union footprint
        # (see production_area.py's own module docstring) -- used here
        # only for label placement; no fill, no boundary stroke drawn for
        # production zones (see this module's own docstring). The contour
        # lines below clip against render_fill_polygon_utm instead (same
        # CRS as contour_lines' lines_utm -- no reprojection needed before
        # intersecting) -- excludes the reclaimed cells at a waist-split
        # pinch (like render_polygon_utm) AND closes over small excluded
        # (steep/hydric) pockets inside the zone, so both kinds of gap are
        # handled by clipping against a single geometry.
        geom = _reproject_geometry_to_mercator(patch["geometry_wgs84"])

        for contour in contour_lines:
            clipped = contour["lines_utm"].intersection(patch["render_fill_polygon_utm"])
            if clipped.is_empty:
                continue
            for line in _iter_line_parts(_reproject_utm_geometry_to_mercator(clipped, dem["crs"])):
                plot_line(
                    line,
                    ax=ax,
                    add_points=False,
                    color=CONTOUR_LINE_COLOR,
                    linewidth=PRODUCTION_ZONE_CONTOUR_LINEWIDTH,
                    alpha=0.85,
                    zorder=40,
                )

        label = f"Production Zone {patch['rank']}" if multiple_zones else "Production Zone"
        _draw_numbered_marker(ax, geom.representative_point(), marker_number)
        legend_entries.append(f"{marker_number} — {label}, {stat_line}")
        marker_number += 1

    if water_zone is not None:
        # DISPLAY-ONLY fill geometry: render_fill_polygon_utm is a plain
        # convex hull of the zone's real cell-union footprint (see
        # water_candidate_zones.find_candidate_zones()'s own docstring),
        # already in the DEM's own UTM CRS -- reprojected in one hop
        # (_reproject_utm_geometry_to_mercator(), same pattern the
        # production-zone contour clipping above already uses), never the
        # real geometry_wgs84 used for scoring/eligibility/the narrative
        # report. Crossing over a production zone's own rendered fill is
        # expected and fine here -- that overlap is a display-only
        # coincidence between two convex hulls, not a real siting
        # conflict (the real, unsmoothed geometries stay clear of each
        # other via the production-zone eligibility exclusion gate).
        render_fill_geom = _reproject_utm_geometry_to_mercator(water_zone["render_fill_polygon_utm"], dem["crs"])
        polygons = render_fill_geom.geoms if render_fill_geom.geom_type == "MultiPolygon" else [render_fill_geom]
        for polygon in polygons:
            # Opaque fill (alpha=1.0) so it fully occludes any production-
            # zone contour lines beneath it (zorder=41 > those zones'
            # zorder=40) -- then a subtle sine-wave ripple texture drawn
            # directly over it, so this reads as water at a glance.
            plot_polygon(
                polygon,
                ax=ax,
                add_points=False,
                facecolor=WATER_ZONE_COLOR,
                edgecolor=WATER_ZONE_COLOR,
                alpha=1.0,
                linewidth=1.5,
                zorder=41,
            )
            for ripple in _ripple_lines_for_polygon(polygon):
                for line in _iter_line_parts(ripple):
                    plot_line(
                        line,
                        ax=ax,
                        add_points=False,
                        color=WATER_ZONE_RIPPLE_COLOR,
                        linewidth=WATER_ZONE_RIPPLE_LINEWIDTH,
                        alpha=WATER_ZONE_RIPPLE_ALPHA,
                        zorder=41.5,
                    )
        # The marker sits on the geometry actually drawn above (the hull,
        # not the real blocky footprint) -- representative_point() is
        # guaranteed by shapely to fall within its own geometry, so this
        # can never land outside the visible fill even though the hull's
        # own representative point can differ from geometry_wgs84's.
        marker_point = render_fill_geom.representative_point()
        assert render_fill_geom.contains(marker_point) or render_fill_geom.intersects(marker_point), (
            "water zone marker point must fall on the geometry actually drawn"
        )
        _draw_numbered_marker(ax, marker_point, marker_number)
        legend_entries.append(
            f"{marker_number} — Water System, Zone {water_zone['id']}, "
            f"score {water_zone['suitability_score']}"
        )
        marker_number += 1

    if road_corridor is not None:
        # DISPLAY-ONLY geometry: _smooth_line_for_render() simplifies away
        # the DEM's own per-cell stairstepping and rounds what corners are
        # left (see that function's own docstring and this module's ROAD
        # CORRIDOR STYLE docstring section) -- road_corridor's real
        # geometry (properties, length_m, avg_grade_pct, everything the
        # narrative report uses) is completely untouched by this.
        geom = _reproject_geometry_to_mercator(road_corridor["geometry"])
        props = road_corridor["properties"]
        render_geom = _smooth_line_for_render(geom)
        _draw_road_corridor(ax, render_geom)
        # The marker sits on the geometry actually drawn (the smoothed
        # line, same "marker matches the visible shape" reasoning the
        # water zone's own marker placement above already uses).
        _draw_numbered_marker(ax, render_geom.interpolate(0.5, normalized=True), marker_number)
        legend_entries.append(f"{marker_number} — Road Corridor, score {props['suitability_score']}")
        marker_number += 1

    # Tree zone candidates: possibly several, ranked (same "possibly-
    # multiple" shape as production zones above, not a single selection
    # like water_zone/road_corridor/structure_site) -- see this module's
    # own TREE ZONE STYLE docstring section for the hatched-fill styling
    # rationale. DISPLAY-ONLY fill geometry: render_fill_polygon_utm is a
    # plain convex hull of the patch's own real footprint (see
    # score_tree_search_space()'s own docstring), already in the DEM's
    # own UTM CRS -- reprojected in one hop
    # (_reproject_utm_geometry_to_mercator(), same pattern the water
    # zone's own fill above already uses), never the real geometry_wgs84
    # used for scoring/eligibility/the narrative report.
    tree_zone_patches = tree_zone_result.get("patches", []) if tree_zone_result else []
    multiple_tree_zones = len(tree_zone_patches) > 1
    for patch in tree_zone_patches:
        render_fill_geom = _reproject_utm_geometry_to_mercator(patch["render_fill_polygon_utm"], dem["crs"])
        polygons = render_fill_geom.geoms if render_fill_geom.geom_type == "MultiPolygon" else [render_fill_geom]
        for polygon in polygons:
            plot_polygon(
                polygon,
                ax=ax,
                add_points=False,
                facecolor=TREE_ZONE_COLOR,
                edgecolor=TREE_ZONE_COLOR,
                alpha=TREE_ZONE_FILL_ALPHA,
                linewidth=1.0,
                hatch=TREE_ZONE_HATCH,
                zorder=42.8,
            )
        label = f"Tree Zone Candidate {patch['rank']}" if multiple_tree_zones else "Tree Zone Candidate"
        # The marker sits on the geometry actually drawn above (the hull,
        # not the real footprint) -- same "marker matches the visible
        # shape" reasoning the water zone's own marker placement already
        # uses.
        _draw_numbered_marker(ax, render_fill_geom.representative_point(), marker_number)
        legend_entries.append(
            f"{marker_number} — {label}, score {patch['tree_suitability_score']}/100, {patch['area_acres']} ac"
        )
        marker_number += 1

    if structure_site is not None:
        # Real, scored footprint still drives placement -- only what gets
        # drawn at its representative_point() changes (see this module's
        # own STRUCTURE SITE STYLE docstring section): a single fixed-size
        # map-pin icon, not a filled polygon, and no numbered circle
        # marker for this zone (there's nothing else on the map for its
        # legend number to point to).
        geom = _reproject_geometry_to_mercator(structure_site["geometry"])
        props = structure_site["properties"]
        anchor_point = geom.representative_point()
        pin = AnnotationBbox(
            STRUCTURE_SITE_ICON,
            (anchor_point.x, anchor_point.y),
            frameon=False,
            box_alignment=(0.5, 0.0),
            zorder=43,
        )
        ax.add_artist(pin)
        legend_entries.append(f"Structure Site, score {props['suitability_score']}")

    ax.set_title("Proposed Farm Layout", fontsize=16, fontweight="bold", pad=14)

    if legend_entries:
        legend_text = "\n".join(legend_entries)
        ax.text(
            0.02,
            0.02,
            legend_text,
            transform=ax.transAxes,
            fontsize=8.5,
            va="bottom",
            ha="left",
            zorder=60,
            bbox=dict(boxstyle="round,pad=0.6", facecolor="white", edgecolor="#333333", alpha=0.9),
        )

    if basemap_note:
        ax.text(
            0.02,
            0.98,
            "Aerial basemap unavailable for this render",
            transform=ax.transAxes,
            fontsize=7,
            va="top",
            ha="left",
            color="#883333",
            zorder=60,
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor="#883333", alpha=0.85),
        )

    fig.tight_layout()
    fig.savefig(output_path, dpi=OUTPUT_DPI)
    plt.close(fig)

    return output_path


if __name__ == "__main__":
    # The user's real, drawn property boundary -- same one every other
    # module's own __main__ block uses.
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    print("Rendering static layout map for property boundary...\n")

    try:
        path = render_layout_map(property_boundary, "layout_map.png")
        print(f"Wrote {path}")
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services (DEM/NHD) and the USGSImageryOnly tile service -- "
            "not a fully sandboxed environment."
        )
