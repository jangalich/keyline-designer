"""
render_layout_map.py

Renders a single, high-resolution static map image of a property's final
proposed layout -- the visual counterpart to the narrative Scale of
Permanence report, meant to be the last page of the assembled PDF (see
generate_pdf_report.py). This module does not identify, score, or select
anything itself: every layer it draws is the already-computed output of
production_area_ceiling.py (optimized production zones),
water_suitability.py (fetch_and_select_optimal_water_zone's own selected_
water_zone -- the top-ranked candidate, reused directly off pipeline_
context.PipelineContext rather than re-fetched, see fetch_layout_layers()'s
own docstring), road_corridors.py (identify_road_corridor_candidates -- the
single selected road corridor road_corridors.py's own ridge-line
identification/scoring picks, see that module's own module docstring),
tree_zone_candidates.py (identify_tree_zone_candidates -- every ranked
tree-suitable candidate patch left over once production/water/road's own
claimed geometry is subtracted out, see that module's own module
docstring), solar_suitability.py (identify_solar_candidate_zones -- the
top-ranked candidate), hydrology_data.py (real NHD streams, for
background context only -- no soil/hydrology POLYGON data is drawn here,
that's covered in the narrative text -- sourced via parcel_data.ParcelData
now, see below, not fetched directly by this module anymore), fencing.py
(identify_fencing -- the canopy-aware boundary fence line(s) plus the
water/tree-zone-exclusion fence loops added below, see that module's
own module docstring), and contour_lines.py (global elevation contour
lines over the full DEM extent).

    boundary --> parcel_data.fetch_parcel_data (every raw KSOP data layer,
                 fetched once, hard-fail gate -- see fetch_layout_layers()'s
                 own docstring)
             --> pipeline_context.build_pipeline_context (dem, boundary_
                 polygon_utm, soil_components, farm_roads all sourced off
                 ParcelData above; valleys, production_areas, selected_
                 water_zone, selected_road_corridor, tree_zone_patches, soil
                 exclusion unions -- every shared upstream input below,
                 computed exactly once; see fetch_layout_layers()'s own
                 docstring for what's still one additional call past this)
             --> road_corridors.identify_road_corridor_candidates
             --> solar_suitability.identify_solar_candidate_zones
             --> fencing.identify_fencing (boundary + water/tree-zone-exclusion fence loops)
             --> contour_lines.compute_contour_lines (global, unclipped)
             --> rendered PNG (basemap + halo + streams + boundary fence +
                 water/tree-zone-exclusion fence loops + layout layers +
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
to be a dashed line, see FENCE_LINEWIDTH's own comment) at FENCE_ZORDER
(see that constant's own comment for why this is now 60, above the
numbered circle markers). There is always at least one segment --
fencing.find_boundary_fencing()'s own plain-wrap case when there's no
canopy touching the boundary at all -- so there's no fallback path to the
old stroke. Before drawing, each segment's geometry is run through
_angular_then_smooth_closed_ring() -- a shapely simplify() pass
(FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M, no Chaikin) followed by a
post-angular Chaikin softening pass (FENCE_RENDER_CHAIKIN_ITERATIONS, 1
iteration) that rounds off the angular corners the simplify pass leaves
behind slightly, without fully re-curving the line -- the SAME treatment
this module previously reserved for road-corridor-exclusion fencing
before that fence loop was removed outright (see fencing.py's own module
docstring for why no road, existing or generated, gets a fence line
anymore). Same DISPLAY-ONLY "never touches the real geometry used
elsewhere" discipline as the road corridor's own _smooth_line_for_render()
(which stays curved/unchanged -- a DIFFERENT feature, the road corridor
line itself, not a fence line). When canopy running end-to-end splits the
parcel into more than one fence loop, each loop gets its own numbered
legend line ("Boundary Fencing 1" / "Boundary Fencing 2") -- otherwise a
single unnumbered "Boundary Fencing" line, same "no numbered circle
marker" treatment structure_site's own legend line already uses (there's
no single point on a loop-shaped feature for a circle number to point
to).

WATER/TREE EXCLUSION FENCE STYLE: fencing.identify_fencing()'s two
non-boundary fence loops ("water_zone_exclusion" -- one closed loop
around the single selected water zone; "tree_zone_exclusion" -- one
closed loop per tree zone candidate, since that layer has no selection
step, see fencing.py's own module docstring) reuse these SAME
boundary-fence styling constants (FENCE_COLOR/FENCE_LINEWIDTH) and the
same _angular_simplify_closed_ring() + drawing helper -- both are fully
enclosed closed loops too, so no new styling or simplify logic is
needed; unlike the boundary fence, neither gets the post-angular Chaikin
softening pass -- they stay purely angular. Unlike the boundary fence
(drawn at FENCE_ZORDER, see that constant's own comment), these two
render at EXCLUSION_FENCE_ZORDER instead -- its own separate, unchanged
constant, deliberately NOT bumped alongside the boundary fence's own
zorder (see EXCLUSION_FENCE_ZORDER's own comment). Unlike the boundary
fence, both ARE clipped to the boundary polygon at render time (real
shapely .intersection(), AFTER angular-simplifying the WHOLE ring --
simplifying before clipping, not after, since _angular_simplify_closed_
ring() re-closes a genuinely closed ring, which would wrongly force-close
an already-clipped, genuinely OPEN arc piece): a water/tree zone's own
buffered render_fill_polygon_utm can sit close enough to the boundary for
its own buffer to reach past the edge -- correct as COMPUTED in every
case (see fencing.py), this clip only trims what gets DRAWN. The
boundary fence itself is deliberately NOT clipped this way -- it's
already guaranteed to stay inside the boundary by construction (see
find_boundary_fencing()'s own difference-against-boundary-polygon
logic), so clipping it here would be a redundant no-op. The intersection
can come back as a LineString, MultiLineString, or GeometryCollection (a
ring crossing the boundary at two points splits into pieces) --
_iter_line_parts() already handles all three -- with any resulting piece
under EXCLUSION_FENCE_CLIP_MIN_LENGTH (a degenerate point/sliver from a
tangent crossing) dropped rather than drawn. Both are INDEPENDENT of the
boundary fence and of each other (deliberately not spliced/gated into
anything -- see fencing.py's own module docstring): a visible overlap
between either of them and the boundary fence is expected wherever the
underlying real-world geometry meets, not a rendering bug. "Water Zone
Fencing" gets a single unnumbered legend line (only ever one, regardless
of how many pieces its own clip produces); "Tree Zone Fencing" gets one
legend line per candidate when more than one exists (same, regardless of
per-segment clip pieces) -- "Tree Zone Fencing {rank}" reuses fencing.
tree_zone_fencing_to_geojson()'s own candidate_rank property directly
rather than re-deriving a new index here, so it lines up with the
same-numbered "Tree Zone Candidate {rank}" this file already draws
elsewhere. Both mirror boundary fencing's own 1-vs-2 segment-labeling
convention, and neither gets a numbered circle marker, same "no single
point to point to" reasoning as boundary fencing's own legend lines
above. No road -- existing or generated -- gets a fence loop of its own
at all anymore (see fencing.py's own module docstring for why).

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
zone contour lines beneath it. The water zone's hull is allowed to
overlap a production zone's own hull at render time -- that's a
display-only coincidence between two convex
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
a single selection like water/road/structure) renders as HATCH-ONLY --
diagonal hatch strokes (TREE_ZONE_COLOR, TREE_ZONE_HATCH_ALPHA,
TREE_ZONE_HATCH) with NO fill and NO perimeter stroke (facecolor="none",
linewidth=0 -- see this module's own tree-zone plot_polygon() call for
why that specific combination, not a cleared edgecolor, is what achieves
this: a patch's hatch color follows its edgecolor, and a patch's hatch
stroke width comes from matplotlib's own rcParams["hatch.linewidth"],
independent of the patch's own linewidth). The geometry drawn is still
its own render_fill_polygon_utm -- a DISPLAY-ONLY plain convex hull of
the patch's real footprint, re-intersected with the parcel boundary
(score_tree_search_space()'s own output, same field/reasoning
production_area.py's/water_candidate_zones.py's own patches/zones already
carry) -- NOT the patch's real, potentially-notched geometry_wgs84 (used
for area_acres/scoring/the narrative report, completely untouched by
this). Same "reads as one coherent shape at render time" purpose as
production/water's own hulls: most directly here, closing over any
interior pocket the CANOPY EXCLUSION GATE carves out of an otherwise-
contiguous candidate (see tree_zone_candidates.py's own module
docstring), rather than rendering as an unexplained blank notch. Zone
identity is still carried by the numbered marker placed on that same
hull and by the corresponding legend line -- the hatch by itself doesn't
need to enclose a filled area to read as a distinct zone on the map. The
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
top, not lost beneath the tree-candidate hatch.

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
from fencing import identify_fencing
from parcel_data import ParcelData, fetch_parcel_data
from pipeline_context import build_pipeline_context
from raster_grid import SQUARE_METERS_PER_ACRE
from road_corridors import identify_road_corridor_candidates
from solar_suitability import identify_solar_candidate_zones
from tree_zone_candidates import identify_tree_zone_candidates

# Reference-property fixture for manual/__main__ testing ONLY -- now that
# the frontend supplies a real access point (api.py's /api/generate-report
# and /api/generate-report-pdf both require it), no production code path
# imports this anymore. Every production entry point (fetch_layout_layers(),
# identify_solar_candidate_zones(), identify_tree_zone_candidates(),
# generate_full_report()) takes a real anchor_lon_lat parameter instead.
# Kept here, at module level, only so the several __main__ blocks across
# this codebase that still import it for manual testing keep working — do
# not reuse this for any other property, and do not reintroduce it into
# any production call path.
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
# already in use. A patch's hatch color follows its edgecolor, not a
# separate hatch-color setting -- this is why TREE_ZONE_COLOR stays set as
# BOTH edgecolor and the hatch's own visible color, even though there's no
# fill and no perimeter stroke drawn from it anymore. See this module's own
# TREE ZONE STYLE docstring section for why this renders as hatch-only
# rather than a flat/solid fill, unlike structure_site's own.
TREE_ZONE_COLOR = "#2D5A27"
TREE_ZONE_HATCH_ALPHA = 0.45
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
# green #2D5A27). The water-zone/tree-zone exclusion fence loops reuse this SAME
# color/linewidth -- no new styling constants needed, see this module's own
# WATER/TREE EXCLUSION FENCE STYLE docstring section. CONFIGURABLE.
FENCE_COLOR = "#D4A017"
FENCE_LINEWIDTH = 0.6  # a hairline -- was 1.2 (a dashed line before that)

# The BOUNDARY fence only renders at this zorder -- explicitly bumped to sit
# above the numbered circle markers/structure site pin (zorder=43/50) as well
# as every zone-fill-style layer beneath it, per explicit request (was 50,
# bumped again to 60 per a later explicit request). Deliberately NOT applied
# to the water-zone/tree-zone exclusion fence loops, nor to roads, tree zone,
# water zones, or streams -- those keep rendering at EXCLUSION_FENCE_ZORDER
# (water/tree exclusion fencing) or their own existing zorder (tree zone hatch,
# water zone fill, stream lines) below, unchanged. CONFIGURABLE.
FENCE_ZORDER = 60

# Zorder for the two non-boundary exclusion fence loops (water_zone_exclusion,
# tree_zone_exclusion) -- this is the value FENCE_ZORDER itself used to hold
# before being bumped up for the boundary fence alone (see that constant's own
# comment). Kept here, unchanged, so these two fence loops still render above
# every zone-fill-style layer (production contour zorder=40, water zone fill
# zorder=41, road corridor cased line zorder=42/42.5, tree zone candidate hatch
# zorder=42.8 -- the true current ceiling, not 40) without being bumped above
# the numbered circle markers/structure site pin the way the boundary fence
# now is.
EXCLUSION_FENCE_ZORDER = 42.9

# Below this length (Web Mercator units, ~meters), a water/tree-zone-exclusion
# fence piece produced by clipping to the boundary polygon at render time (see
# WATER/TREE EXCLUSION FENCE STYLE above) is a degenerate point/sliver from a
# tangent crossing, not a real segment worth drawing.
EXCLUSION_FENCE_CLIP_MIN_LENGTH = 0.5

# DISPLAY-ONLY simplify tolerance for fence rings (see _angular_simplify_closed_ring()) --
# a shapely simplify() pass ONLY, no Chaikin/corner-rounding at all: fence lines render
# ANGULAR now, not curved, per explicit request. Meaningfully larger than the road
# corridor's own ROAD_RENDER_SIMPLIFY_TOLERANCE_M (2.5m) so the DEM-resolution stairstep
# zigzags a fence line inherits from its own underlying cell/canopy geometry collapse into
# fewer, longer straight segments rather than just having their corners rounded off.
# CONFIGURABLE -- tune by eye against a real property.
FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M = 6.0  # was 4.0

# Post-angular Chaikin pass for the BOUNDARY fence only (see
# _angular_then_smooth_closed_ring()) -- applies ONLY to fence_type 'boundary',
# not to water_zone_exclusion/tree_zone_exclusion (those stay pure angular,
# unchanged). Previously this same treatment (and this same 1-iteration value)
# applied to road-corridor-exclusion fencing instead, before that fence loop
# was removed outright (see fencing.py's own module docstring); moved here per
# explicit request, unchanged in value. 1 pass: meant to soften the angular
# corners _angular_simplify_closed_ring() leaves behind slightly, not fully
# re-curve the line back to how it looked before the angular-only change.
# CONFIGURABLE -- start light.
FENCE_RENDER_CHAIKIN_ITERATIONS = 1


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
    shoulder (42.5 vs 42).
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
    DISPLAY-ONLY transform for a fence loop's own LineString --
    water-zone-exclusion and tree-zone-exclusion fencing use this one
    helper directly and stay purely angular; boundary fencing routes
    through _angular_then_smooth_closed_ring() below instead, which calls
    this same function first before adding its own post-angular Chaikin
    pass on top.
    Replaces this module's earlier simplify-then-Chaikin-smooth treatment:
    fence lines now render ANGULAR by default, not curved, per explicit
    request -- so this is shapely .simplify(tolerance, preserve_topology=
    True) on the ring ONLY, no Chaikin call, zero smoothing iterations.
    Deliberately simpler than the helper it replaced, not a curved-then-
    de-curved round trip.

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


def _chaikin_smooth_closed_ring(coords: list[tuple[float, float]], iterations: int) -> list[tuple[float, float]]:
    """
    Same Chaikin corner-cutting construction as _chaikin_smooth_coords()
    above, but CYCLIC -- for a closed fence loop, not an open route. A
    closed ring has no meaningful start/end point to pin the way the open-
    line version pins its first/last coordinate, so pinning one here would
    leave a real, visible unsmoothed sharp seam at whatever index the
    coordinate array happens to start at -- a correctness issue, not just
    cosmetic, since that seam's location is an arbitrary artifact of how
    the ring's coordinates happen to be ordered, not a real corner of the
    property.

    Concretely: the duplicated closing coordinate (coords[0] == coords[-1],
    every ring this module receives is already closed) is dropped before
    smoothing, every edge is cut INCLUDING the wraparound edge from the
    last point back to the first (`zip(coords, coords[1:] + coords[:1])`,
    not `zip(coords, coords[1:])`), and the result is re-closed by
    appending its own first point back onto the end.

    A no-op below 3 points -- there's no real ring geometry to smooth
    below a triangle.
    """
    if coords[0] == coords[-1]:
        coords = coords[:-1]

    for _ in range(iterations):
        if len(coords) < 3:
            break
        smoothed = []
        for (x0, y0), (x1, y1) in zip(coords, coords[1:] + coords[:1]):
            smoothed.append((0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1))
            smoothed.append((0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1))
        coords = smoothed

    return coords + [coords[0]]


def _angular_then_smooth_closed_ring(ring: LineString, simplify_tolerance: float, chaikin_iterations: int) -> LineString:
    """
    Boundary-fence-specific render treatment -- fence_type 'boundary' ONLY,
    not water_zone_exclusion/tree_zone_exclusion (those stay purely
    angular via _angular_simplify_closed_ring() alone). Previously this
    same treatment applied to road-corridor-exclusion fencing instead,
    before that fence loop was removed outright (see fencing.py's own
    module docstring). New plumbing chaining two existing helpers, not new
    smoothing math: runs the ring through _angular_simplify_closed_ring()
    first (unchanged), then feeds the result through _chaikin_smooth_
    closed_ring() (cyclic, closed-ring-aware -- see that function's own
    docstring) for chaikin_iterations passes. Meant to soften the angular
    corners simplify() leaves behind slightly, not fully re-curve the line
    back to how it looked before the angular-only change -- see FENCE_
    RENDER_CHAIKIN_ITERATIONS's own comment for why it starts at just 1
    pass.
    """
    angular_ring = _angular_simplify_closed_ring(ring, simplify_tolerance)
    smoothed_coords = _chaikin_smooth_closed_ring(list(angular_ring.coords), chaikin_iterations)
    return LineString(smoothed_coords)


def _draw_boundary_fence(ax, ring: LineString, zorder: float = FENCE_ZORDER) -> None:
    """Draws `ring` (already simplified -- see _angular_simplify_closed_ring()/
    _angular_then_smooth_closed_ring()) as a single solid hairline fence line,
    FENCE_COLOR/FENCE_LINEWIDTH -- no dash pattern (this used to be dashed; now
    a fine, subtle solid line). Defaults to FENCE_ZORDER (see that constant's
    own comment) -- the boundary fence's own caller relies on this default;
    the water/tree exclusion fence loops' caller passes zorder=
    EXCLUSION_FENCE_ZORDER explicitly instead (see that constant's own
    comment for why they don't share FENCE_ZORDER)."""
    plot_line(
        ring,
        ax=ax,
        add_points=False,
        color=FENCE_COLOR,
        linewidth=FENCE_LINEWIDTH,
        linestyle="solid",
        zorder=zorder,
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


def _production_zone_legend_stats(scored_patches: list[dict], parcel_acres: float) -> list[tuple[float, str]]:
    """Returns [(area_acres, stat_line), ...] -- one per surviving
    production patch, in the same order scored_patches already ranks
    them. parcel_acres is the property boundary's own real area, passed
    directly by the caller rather than back-derived from a second
    production-zone computation -- this function is purely a
    display-formatting step, same as before."""
    stats = []
    for patch in scored_patches:
        pct_note = (
            f" ({round(patch['area_acres'] / parcel_acres * 100)}% of parcel)"
            if parcel_acres
            else ""
        )
        stats.append((patch["area_acres"], f"{patch['area_acres']} ac{pct_note}"))
    return stats


def fetch_layout_layers(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    anchor_lon_lat: Optional[tuple[float, float]] = None,
    parcel_data: Optional[ParcelData] = None,
) -> dict:
    """
    Fetches/derives every layer render_layout_map() draws.

    parcel_data (the parameter): a pre-fetched parcel_data.ParcelData, so a
    caller that already fetched one for other purposes (e.g. the narrative
    report's own data needs) doesn't pay for a second, redundant raw-layer
    fetch. Omit it (default None) and this function calls parcel_data.
    fetch_parcel_data() itself, ONCE, right here -- which HARD-FAILS,
    uncached, on any gap among ANY of its 12 mandatory raw layers
    (including farm_roads: a render with incomplete data is worse than no
    render at all, confirmed intentional). That hard fail happens before
    any KSOP computation below runs at all -- production/water/road/tree/
    solar/fencing never even start on a boundary whose raw data is
    incomplete.

    dem (the parameter): kept in the signature so an existing caller that
    passes dem= (generate_pdf_report.py's own pre-fetched-DEM call) doesn't
    break, but it is NO LONGER USED in this function's body -- every dem
    value used below now comes from parcel_data.dem instead, since that's
    the one ParcelData is guaranteed to carry regardless of whether the
    caller passed anything in. A caller passing dem= without also passing
    parcel_data= gets a real, working render, just without the redundant-
    fetch avoidance dem= used to buy it (parcel_data.fetch_parcel_data()
    always re-fetches its own DEM) -- migrating that caller to build and
    pass its own parcel_data= instead is flagged as separate, future work.

    Builds a single pipeline_context.PipelineContext ONCE (build_pipeline_
    context()) and reuses its fields across every call below, instead of
    each of production/water/road/tree/solar/fencing independently
    recomputing the same DEM fetch, boundary_polygon_utm reprojection,
    production areas, valleys, and selected water zone the way this
    function used to let them -- see diagnose_fetch_layout_layers_
    redundant_fetches.py (a parallel diagnostic to diagnose_pipeline_
    redundant_fetches.py, targeting this function directly) for the real,
    measured before/after call counts. build_pipeline_context() itself now
    receives dem/boundary_polygon_utm/soil_components/farm_roads straight
    off parcel_data, so its own internal DEM fetch and boundary_polygon_utm
    reprojection never run either -- the raw-network-fetch layer for this
    whole function now runs exactly once, inside parcel_data.
    fetch_parcel_data() above, not once per consuming layer.

    canopy_height is now parcel_data.canopy_height passed straight into
    build_pipeline_context()'s own canopy_height= override, closing the
    canopy fetch the same way dem/boundary_polygon_utm/soil_components/
    farm_roads/water_features/soil_geometries already were -- every
    canopy-gated call build_pipeline_context() makes internally (optimized
    production areas, water system candidate zones, the selected water
    zone, solar candidate zones, tree zone candidates) now reuses this
    SAME already-fetched canopy instead of independently re-fetching it.
    road_corridors.identify_road_corridor_candidates() has no canopy gate
    at all, so it's unaffected either way. The identify_solar_candidate_
    zones() call below (a second, necessary call -- see structure_site
    below) also receives parcel_data.canopy_height directly, for the same
    reason. fencing.identify_fencing() does NOT yet expose a canopy_
    height override on its own signature (only the identify_boundary_
    fencing() entry point one level down inside fencing.py does) -- out of
    scope here, unchanged, still its own independent canopy fetch; see
    that module's own backlog.

    production zone legend stats: context.production_areas already IS
    identify_optimized_production_areas()'s own scored_patches list (see
    pipeline_context.py's own field notes) -- no additional call needed
    at all now. percent_of_parcel is no longer read off a second
    identify_optimized_production_areas() call's own return dict; it's
    computed directly from context.boundary_polygon_utm.area (the
    property boundary's own real, already-computed footprint) instead --
    the exact same value that second call's own percent_of_parcel field
    was itself computed FROM, just reached without re-running production-
    zone identification a second time. See _production_zone_legend_
    stats()'s own docstring.

    water_zone is context.selected_water_zone directly -- water_
    suitability.fetch_and_select_optimal_water_zone()'s own return value
    and this field are the exact same scored-zone dict shape (see
    pipeline_context.py's own field notes), so this is fully free: no
    additional call at all.

    road_corridor_candidates: full return dict still needed for its
    zones_geojson (the GeoJSON Feature this function renders, unlike
    context.selected_road_corridor's raw-UTM-with-cells shape) -- one
    additional identify_road_corridor_candidates() call, but now passing
    every override that entry point supports (dem/boundary_polygon_utm/
    production_areas/valleys/selected_water_zone/hydric_floodplain_union/
    floodplain_data_is_fallback, all straight from context) so this call's
    own internal production/water/floodplain-union derivation is fully
    eliminated, not just its DEM fetch. selected_road_corridor is read
    from context directly rather than re-extracted from this call's own
    result -- same overrides in, so the same deterministic selection
    comes out; reusing avoids a needless "trust this matches" moment.

    tree_zone_result: identify_tree_zone_candidates()'s own return dict is
    used ONLY for its 'patches' key everywhere downstream (confirmed: the
    one other read site, inside render_layout_map() itself, is also
    tree_zone_result.get("patches", [])) -- so wrapping context.tree_zone_
    patches (the exact same list, already fully computed) in a matching
    {"patches": [...]} dict is a drop-in replacement with zero additional
    calls.

    structure_site is read here as a GeoJSON Feature (identify_solar_
    candidate_zones()'s own zones_geojson features[0], the same top-ranked
    candidate select_optimal_structure_site() picks) rather than context.
    selected_structure_site -- that field is a DIFFERENT shape (a raw
    scored dict carrying UTM shapely geometry, un-reprojected), so reusing
    it here would need one-off reprojection/property-translation code for
    zero fetch-count benefit; this module's own prior docstring already
    flagged this shape mismatch before this branch existed. One
    additional identify_solar_candidate_zones() call (replacing the old
    fetch_and_select_optimal_structure_site() wrapper, which only accepted
    dem/anchor_lon_lat), now passing every override it supports so this
    call's own internal production/water/road/tree-zone re-derivation is
    fully eliminated too, not just its DEM fetch.

    contour_lines is contour_lines.compute_contour_lines()'s own output --
    GLOBAL elevation contour lines over the DEM's full extent, computed
    ONCE here (off context.dem) and shared across every production zone
    at render time (each zone clips its own segments out of this same
    list via real shapely intersection against its own render_fill_
    polygon_utm -- see render_layout_map()'s own docstring), rather than
    recomputing contour lines per zone.

    fencing_result is fencing.identify_fencing()'s own output -- the
    canopy-aware boundary fence line(s) that replace this module's former
    plain boundary stroke, PLUS the water-zone-exclusion and tree-zone-
    exclusion fence loops (see render_layout_map()'s own docstring; no
    road -- existing or generated -- gets a fence loop of its own, see
    fencing.py's own module docstring for why). NOT wrapped in try/except
    itself: identify_fencing()'s own boundary-fence path still carries its
    MANDATORY canopy fetch (same hard-fail-on-missing-coverage design as
    build_pipeline_context()'s own production/tree-zone canopy gates), and
    a failure there should propagate up and fail this whole render rather
    than silently omitting the fence layer. Fed the HIGH-LEVEL selected_
    road_corridor/selected_water_zone/tree_zone_patches context fields
    directly (selected_road_corridor purely as a real siting-exclusion
    input for identify_fencing()'s own tree-zone self-compute fallback,
    not for a fence loop of its own anymore; selected_water_zone/tree_
    zone_patches each let identify_fencing() derive their own low-level
    render_fill_polygon_utm equivalent directly when supplied -- see that
    function's own docstring), plus boundary_polygon_utm/production_areas/
    valleys/hydric_floodplain_union/floodplain_data_is_fallback, so this
    call's own three internal self-compute fallbacks (road corridor, water
    zone, tree zone) never run at all.

    water_features is parcel_data.water_features directly, same reasoning
    -- no longer this function's own hydrology_data.get_water_features_
    for_boundary() call.
    """
    if parcel_data is None:
        parcel_data = fetch_parcel_data(boundary_coordinates)

    context = build_pipeline_context(
        boundary_coordinates,
        anchor_lon_lat,
        dem=parcel_data.dem,
        boundary_polygon_utm=parcel_data.boundary_polygon_utm,
        soil_components=parcel_data.soil_components,
        farm_roads=parcel_data.farm_roads,
        water_features=parcel_data.water_features,
        soil_geometries=parcel_data.soil_geometries,
        canopy_height=parcel_data.canopy_height,
    )

    production_zone_legend_stats = _production_zone_legend_stats(
        context.production_areas, context.boundary_polygon_utm.area / SQUARE_METERS_PER_ACRE
    )

    water_zone = context.selected_water_zone

    road_corridor_candidates = identify_road_corridor_candidates(
        boundary_coordinates,
        dem=context.dem,
        anchor_lon_lat=anchor_lon_lat,
        boundary_polygon_utm=context.boundary_polygon_utm,
        production_areas=context.production_areas,
        valleys=context.valleys,
        selected_water_zone=context.selected_water_zone,
        hydric_floodplain_union=context.soil_exclusion_unions["hydric_floodplain_union"],
        floodplain_data_is_fallback=context.soil_exclusion_unions["hydric_floodplain_is_fallback"],
        canopy_height=parcel_data.canopy_height,
    )
    road_corridor_features = road_corridor_candidates["zones_geojson"]["features"]
    road_corridor = road_corridor_features[0] if road_corridor_features else None
    selected_road_corridor = context.selected_road_corridor

    tree_zone_result = {"patches": context.tree_zone_patches}

    solar_result = identify_solar_candidate_zones(
        boundary_coordinates,
        dem=context.dem,
        anchor_lon_lat=anchor_lon_lat,
        boundary_polygon_utm=context.boundary_polygon_utm,
        production_areas=context.production_areas,
        valleys=context.valleys,
        selected_water_zone=context.selected_water_zone,
        selected_road_corridor=selected_road_corridor,
        hydric_floodplain_union=context.soil_exclusion_unions["hydric_floodplain_union"],
        floodplain_data_is_fallback=context.soil_exclusion_unions["hydric_floodplain_is_fallback"],
        canopy_height=parcel_data.canopy_height,
    )
    solar_features = solar_result["zones_geojson"]["features"]
    structure_site = solar_features[0] if solar_features else None

    water_features = parcel_data.water_features
    contour_lines = compute_contour_lines(context.dem)

    fencing_result = identify_fencing(
        boundary_coordinates,
        dem=context.dem,
        anchor_lon_lat=anchor_lon_lat,
        boundary_polygon_utm=context.boundary_polygon_utm,
        production_areas=context.production_areas,
        valleys=context.valleys,
        selected_road_corridor=selected_road_corridor,
        selected_water_zone=context.selected_water_zone,
        tree_zone_patches=context.tree_zone_patches,
        hydric_floodplain_union=context.soil_exclusion_unions["hydric_floodplain_union"],
        floodplain_data_is_fallback=context.soil_exclusion_unions["hydric_floodplain_is_fallback"],
        canopy_height=parcel_data.canopy_height,
    )

    return {
        "dem": context.dem,
        "production_areas": context.production_areas,
        "production_zone_legend_stats": production_zone_legend_stats,
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
    anchor_lon_lat: Optional[tuple[float, float]] = None,
) -> str:
    """
    Renders the final proposed layout as a single high-resolution PNG at
    output_path and returns that same path.

    layers (optional): a pre-fetched fetch_layout_layers() result, so a
    caller that already fetched the DEM/layers for other purposes (e.g.
    generate_pdf_report.py, which also needs them alongside the narrative
    report's own data fetches) doesn't pay for a second, redundant fetch.
    Omit it (default) to have this function fetch everything itself.

    anchor_lon_lat: the real, user-picked access point -- only used when
    layers isn't already supplied (this function's own internal
    fetch_layout_layers() call needs it for road-corridor routing);
    ignored otherwise, since a pre-fetched layers dict already baked in
    whatever anchor its own caller used.
    """
    if layers is None:
        layers = fetch_layout_layers(boundary_coordinates, dem=dem, anchor_lon_lat=anchor_lon_lat)

    dem = layers["dem"]
    scored_patches = layers["production_areas"]
    zone_stats = layers["production_zone_legend_stats"]
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

    # z-order, back to front: halo mask, streams, production zone contours,
    # water zone fill, road corridor line, tree zone hatch, the water/tree
    # exclusion fence loops (EXCLUSION_FENCE_ZORDER, above every zone
    # fill -- see that constant's own comment), structure site pin, numbered
    # markers, the boundary fence (FENCE_ZORDER, above the numbered markers --
    # see that constant's own comment), legend/basemap note.
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
    # own docstring for the no-canopy case). DISPLAY-ONLY angular simplify PLUS a
    # post-angular Chaikin softening pass (_angular_then_smooth_closed_ring(), see that
    # function's own docstring -- same "never touches the real geometry used elsewhere"
    # discipline as the road corridor's own _smooth_line_for_render()) --
    # fencing_result['fencing_geojson'] (used for the narrative report) is untouched.
    fencing_features = fencing_result["fencing_geojson"]["features"]
    boundary_fence_features = [f for f in fencing_features if f["properties"].get("fence_type") == "boundary"]
    segment_count = fencing_result["segment_count"]
    multiple_fence_segments = segment_count > 1
    for i, feature in enumerate(boundary_fence_features, start=1):
        fence_geom = _reproject_geometry_to_mercator(feature["geometry"])
        render_ring = _angular_then_smooth_closed_ring(
            fence_geom, FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M, FENCE_RENDER_CHAIKIN_ITERATIONS
        )
        _draw_boundary_fence(ax, render_ring)
        label = f"Boundary Fencing {i}" if multiple_fence_segments else "Boundary Fencing"
        legend_entries.append(label)

    # Everything-else fencing (fencing.identify_fencing()'s own "water_zone_exclusion" /
    # "tree_zone_exclusion" fence_types, same "perimeter_fencing" layer -- no road fence
    # loop exists anymore, see fencing.py's own module docstring) -- both are fully
    # enclosed closed loops (per their own spec), so the same drawing helper as boundary
    # fencing applies, at EXCLUSION_FENCE_ZORDER rather than boundary fencing's own
    # FENCE_ZORDER (see that constant's own comment for why they don't share a zorder)
    # -- see this module's own WATER/TREE EXCLUSION FENCE STYLE docstring section. Both
    # stay purely angular (_angular_simplify_closed_ring(), unlike the boundary fence's
    # own Chaikin-softened treatment above). Looped over generically rather than as two
    # near-duplicate blocks; only the legend label differs per fence_type. INDEPENDENT of
    # the boundary fence and of each other -- an overlap between either of them and the
    # boundary fence is expected, not a bug. Unlike the boundary fence, each simplified
    # ring is clipped to boundary_polygon (render-only -- see this module's own WATER/TREE
    # EXCLUSION FENCE STYLE docstring section for why) AFTER simplifying it, same order as
    # the prior render-polish pass -- the simplify helper re-closes a genuinely closed
    # ring, which would be WRONG if applied to an already-clipped, genuinely OPEN arc
    # piece (it would force-close a real arc into a bogus loop), so the whole ring is
    # simplified first, while it's still guaranteed closed, and only THEN clipped into
    # however many open/closed pieces the boundary crossing produces. The clip can split
    # one ring into several line pieces, all drawn, but the legend still gets exactly one
    # line per FEATURE regardless.
    extra_fence_features = [
        f for f in fencing_features if f["properties"].get("fence_type") in ("water_zone_exclusion", "tree_zone_exclusion")
    ]
    multiple_tree_zone_fences = (
        sum(1 for f in extra_fence_features if f["properties"]["fence_type"] == "tree_zone_exclusion") > 1
    )
    for feature in extra_fence_features:
        fence_geom = _reproject_geometry_to_mercator(feature["geometry"])
        fence_type = feature["properties"]["fence_type"]
        render_ring = _angular_simplify_closed_ring(fence_geom, FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M)
        clipped_ring = render_ring.intersection(boundary_polygon)
        for line in _iter_line_parts(clipped_ring):
            if line.length > EXCLUSION_FENCE_CLIP_MIN_LENGTH:
                _draw_boundary_fence(ax, line, zorder=EXCLUSION_FENCE_ZORDER)
        if fence_type == "water_zone_exclusion":
            legend_entries.append("Water Zone Fencing")
        else:  # tree_zone_exclusion -- fencing.tree_zone_fencing_to_geojson()'s own
            # 1-based candidate_rank property, reused directly rather than re-deriving
            # a new index here, so "Tree Zone Fencing N" lines up with the same-numbered
            # "Tree Zone Candidate N" this file already draws elsewhere.
            rank = feature["properties"]["candidate_rank"]
            label = f"Tree Zone Fencing {rank}" if multiple_tree_zone_fences else "Tree Zone Fencing"
            legend_entries.append(label)

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
            # zorder=40).
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
    # own TREE ZONE STYLE docstring section for the hatch-only styling
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
                facecolor="none",
                edgecolor=TREE_ZONE_COLOR,
                alpha=TREE_ZONE_HATCH_ALPHA,
                linewidth=0,
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
        path = render_layout_map(
            property_boundary, "layout_map.png", anchor_lon_lat=_PLACEHOLDER_REFERENCE_PROPERTY_ANCHOR_LON_LAT
        )
        print(f"Wrote {path}")
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services (DEM/NHD) and the USGSImageryOnly tile service -- "
            "not a fully sandboxed environment."
        )
