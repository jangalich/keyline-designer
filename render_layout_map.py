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
full road NETWORK road_corridors.py's own coverage-greedy router grows
outward from the property's real, chosen access point -- one Feature per
branch, trunk and spur(s) alike, every branch drawn here, see that
module's own module docstring),
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
                 icon legend box, all one image)

LEGEND: the map carries a single icon legend (a real matplotlib ax.legend(),
not a plain-text box) in the lower-left corner. Its handles are DRAWN symbols
that match what each layer draws on the map -- an asterisk for keypoints,
nested brown contour arcs for production, a blue wave train for the water
survey area, a grey cased road line, a green hatch for tree crops, the
barn/silo glyph for the building site, and a yellow octagon outline for
fencing -- so a reader can match legend to map without a caption. The order is
FIXED and KSOP-shaped (Keypoints, Production, Water, Road, Tree, Structure,
Fencing -- Scale of Permanence order, roughly), assembled ONCE after all
drawing rather than inline in draw order. There is exactly ONE entry per
feature regardless of how many zones/branches/segments it drew: three
production zones, four tree candidates, every fence type, and every road
branch each collapse to a single entry. A feature that drew nothing
contributes no entry (an empty map produces an empty legend and no frame).
NO legend label carries any per-feature data (acreage, score, rank, id) --
that reporting data lives on each feature's own upstream source, read by the
report generator, never derived or stored in this module. See the LEGEND
sections of the individual layer notes below, and the icon/handler constants
grouped under "ICON LEGEND" further down.

There are NO numbered circle markers on the map anymore. Every layer used to
carry a numbered black circle (1, 2, 3, ...) tying its on-map position to a
numbered legend line; that convention is gone entirely -- no layer gets one.
LOSING PER-ZONE IDENTITY ON THE MAP IS INTENTIONAL AND CONFIRMED: with the
numbers removed, a property with three production zones (or several tree
candidates) has NO on-map way to tell rank 1 from rank 3 apart -- they share
one "Production Areas"/"Tree Crop Areas" entry and one texture. This is a
deliberate simplification, not an oversight; do not reintroduce any
substitute per-zone labelling scheme. Two on-map symbols survive as the
genuine symbol for their layer and are NOT numbered markers: the keypoint
asterisks and the structure site's map-pin icon.

PRODUCTION ZONE STYLE: production zones render as CONTOUR-LINE TEXTURE,
not a filled/outlined shape -- contour_lines.py's global contour lines
(computed once, over the DEM's full extent) are clipped per zone at
render time (real shapely intersection against that zone's own
render_fill_polygon_utm -- a bounded opening, display-smoothed at render
time, see the DISPLAY SMOOTHING note below -- not a pre-clipped raster),
and only the clipped segments within that zone are drawn. No fill, no
boundary stroke, and no on-map label for production zones -- every
production zone shares one "Production Areas" legend entry (the contour
texture, echoed by the legend's nested-arc icon), with no per-zone identity
on the map (intentional, see LEGEND above). This is a deliberate, scoped
styling split: the boundary fence (below) has its OWN render-only treatment
-- the water zone, the road corridor, and the structure site (all below)
have their OWN render-only treatments too.

BOUNDARY FENCE STYLE: the property boundary itself no longer renders as
a plain solid stroke -- it renders as fencing.identify_fencing()'s own
developed-footprint boundary fence line(s) (see that module's own module
docstring), a thin solid hairline (FENCE_COLOR/FENCE_LINEWIDTH; this used
to be a dashed line, see FENCE_LINEWIDTH's own comment) at FENCE_ZORDER
(see that constant's own comment for why this is now 60, above the
structure site pin). There is always at least one segment --
fencing.find_boundary_fencing()'s own plain-wrap case on a bare property
(no developed footprint) -- so there's no fallback path to the old stroke.
Before drawing, each segment's geometry is run through
_angular_simplify_closed_ring() -- a shapely simplify() pass
(FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M) ONLY, no Chaikin: the boundary
fence renders pure-angular, the same treatment as every other fence type
(an earlier session's boundary-specific post-angular Chaikin softening
pass has been removed). Same DISPLAY-ONLY "never touches the real geometry
used elsewhere" discipline as the road corridor's own _smooth_line_for_
render() (which stays curved/unchanged -- a DIFFERENT feature, the road
corridor line itself, not a fence line). However many fence loops the drawn
boundary clips into, they carry NO per-segment legend line: boundary fencing,
the water-zone-exclusion loop, and the tree-zone-exclusion loops all collapse
into a SINGLE "Fencing" legend entry (drawn in yellow, echoed by the legend's
octagon-outline icon), regardless of segment count or fence_type.

WATER/TREE EXCLUSION FENCE STYLE: fencing.identify_fencing()'s two
non-boundary fence loops ("water_zone_exclusion" -- one closed loop
around the single selected water zone; "tree_zone_exclusion" -- one
closed loop per tree zone candidate, since that layer has no selection
step, see fencing.py's own module docstring) reuse these SAME
boundary-fence styling constants (FENCE_COLOR/FENCE_LINEWIDTH) and the
same _angular_simplify_closed_ring() + drawing helper -- both are fully
enclosed closed loops too, so no new styling or simplify logic is
needed (both stay purely angular, same as the boundary fence). Unlike the
boundary fence (drawn at FENCE_ZORDER, see that constant's own comment),
these two render at EXCLUSION_FENCE_ZORDER instead -- its own separate,
unchanged constant, deliberately NOT bumped alongside the boundary fence's
own zorder (see EXCLUSION_FENCE_ZORDER's own comment). Each simplified ring
is first trimmed by the union of every OTHER simplified ring about to be
drawn -- the boundary fence ring(s) PLUS every other zone ring (the water
zone and every other tree zone candidate), buffered by ZONE_FENCE_BOUNDARY_
COINCIDENCE_TOLERANCE_M -- to suppress the doubled line where find_boundary_
fencing()'s union made a zone and the boundary coincident and, equally, where
two adjacent zones' own buffered rings run near-parallel to each other. That
mutual trim is SYMMETRIC, not priority-based: neither zone in an adjacent pair
outranks the other, so BOTH lose their near-shared stretch and the pair renders
as two separate line pieces with a real visible GAP between them -- the
intended result, not a defect to close. Each zone is compared against the
other zones' ORIGINAL (pre-trim) rings, never against an already-trimmed
result, so there is no ordering dependency and no rank is needed: every zone's
trim is computed independently of every other's. Each zone's own trimmed ring
is then drawn on its OWN -- never merged/unioned with a neighbour's into one
continuous outline. A zone with no near neighbour still renders its full,
untrimmed loop. Each ring is then clipped to
the boundary polygon at render time (real shapely .difference() then
.intersection(), AFTER angular-simplifying the WHOLE ring -- simplifying
before either op, not after, since _angular_simplify_closed_ring()
re-closes a genuinely closed ring, which would wrongly force-close an
already-open arc piece): a water/tree zone's own
buffered render_fill_polygon_utm can sit close enough to the boundary for
its own buffer to reach past the edge -- correct as COMPUTED in every
case (see fencing.py), this clip only trims what gets DRAWN. The
boundary fence itself is deliberately NOT clipped this way -- it's
already guaranteed to stay inside the boundary by construction (see
find_boundary_fencing()'s own intersection-with-boundary-polygon clip),
so clipping it here would be a redundant no-op. The trim/clip results can
come back as a LineString, MultiLineString, or GeometryCollection (a ring
crossing the boundary at two points, or a coincident stretch removed from
the middle, splits into pieces) -- _iter_line_parts() already handles all
three -- with any resulting piece under EXCLUSION_FENCE_CLIP_MIN_LENGTH (a
degenerate point/sliver from a tangent crossing) dropped rather than drawn.
Both are INDEPENDENT of the boundary fence and of each other in the DATA
(deliberately not spliced/gated into anything -- see fencing.py's own
module docstring); fencing_geojson is completely unaffected by any of
this -- every zone's full, untrimmed ring stays exactly as find_water_
zone_fencing()/find_tree_zone_fencing() computed it, and the mutual trim
above only changes what gets DRAWN. It suppresses just the doubled line
where two drawn rings run on top of each other, but a genuine crossing
overlap elsewhere is still expected wherever the underlying geometry
meets, not a bug. The boundary fence ring(s) themselves are drawn as-is
and are never trimmed against anything -- they only ever act as a trim
mask for the zone rings, never the other way round. Legend: every fence
type -- boundary, water_zone_exclusion, and tree_zone_exclusion alike --
collapses into ONE shared "Fencing" entry regardless of zone count or clip
pieces (the mutual trim never merges two zones into one rendered feature, it
just takes a bite out of each; but the legend no longer distinguishes them
at all). There are no more per-zone "Water Zone Fencing"/"Tree Zone Fencing
{rank}" lines and no numbered circle markers on any fence. No road --
existing or generated -- gets a fence loop of its own at all anymore (see
fencing.py's own module docstring for why).

Contours clip against render_fill_polygon_utm rather than polygon_utm
because render_fill_polygon_utm is a bounded morphological OPENING of the
zone's cell-union footprint (production_area.py's own module docstring):
erode by RENDER_OPENING_RADIUS_METERS + RENDER_LEAD_ERODE_CELLS, dilate
back by the opening radius, then clip to polygon_utm. Two consequences
matter for the contour clip:

  1. A waist split (production_area.py's Part 1) can leave two zones
     directly adjacent with ZERO real distance between their reported
     polygon_utm footprints -- erosion's reclaim step reassigns every
     stripped cell back onto whichever resulting piece is nearest, so
     there's nothing left between them in the real geometry. The opening's
     own erosion severs any neck narrower than its radius before the
     dilation regrows only the survivors, so a narrow pinch renders as a
     real, visible gap between the two pieces rather than a seamless join.

  2. The opening is ANTI-EXTENSIVE (contained within polygon_utm) and
     insets every edge by the lead erode, so it is a clean, bounded shape
     that never claims ground the cell gate excluded -- unlike the convex
     hull this replaced, which had no upper bound and covered interior
     pockets/notches whatever their size. Interior excluded ground (steep/
     hydric pockets) is therefore NOT closed over; those pockets read as
     the real gaps they are.

DISPLAY SMOOTHING: the fill is a true cell-union boundary, a 5m right-angle
staircase, so clipping contours straight against it lets each 5m step run
one contour slightly longer than its neighbour -- the contour ends read as
a frayed comb rather than a field edge. At render time the clip mask is
therefore angular-simplified + Chaikin-softened (PRODUCTION_FILL_SIMPLIFY_
TOLERANCE_CELLS / PRODUCTION_FILL_CHAIKIN_ITERATIONS, then re-clipped to
polygon_utm) via _smooth_production_fill_for_render(), so contours
terminate along a clean curve. This is a Layer-3 display transform only:
the stored render_fill_polygon_utm the four consumer modules read as
production exclusion geometry is NOT smoothed.

WATER ZONE STYLE: the water zone renders as RIPPLE-LINE TEXTURE, not a
filled/outlined shape. A family of horizontal sine waves
(_ripple_lines_for_polygon(), see WATER_RIPPLE_* constants) is clipped per
zone at render time (a real shapely intersection against that zone's own
render_fill_polygon_utm, the bounded render opening -- generated in the
same Web Mercator space the geometry is drawn in so the clip lands
correctly), and only the surviving wave segments within that zone are
drawn. The water zone NO LONGER renders a fill or a perimeter stroke: no
facecolor, no edge stroke -- exactly the same styling discipline as
production zones' contour-line texture and tree zones' hatch. There is no
on-map label; the single "Water System Survey Area" legend entry (echoed by
the legend's blue wave-train icon) carries the water layer, with no numbered
marker. The ripple stroke reuses WATER_ZONE_COLOR (the water blue) at the
water zone's existing zorder (41, above production contours' zorder=40).
This is DISPLAY-ONLY: the zone's real polygon_utm/geometry_wgs84 (used for
scoring, eligibility, and the narrative report) are completely untouched
by this, and render_fill_polygon_utm is read but not modified. If the
render opening is too small for even one wave row to intersect, nothing is
drawn -- there is no marker to convey a zone that small anymore, and no
fallback to the old fill (the legend entry still lists, since the layer is
present in the data). The ripple texture is allowed to cross a production
zone's own rendered texture at render time -- that's a display-only
coincidence, not a real siting conflict; the real geometries stay
separated by water_candidate_zones.py's own production-zone eligibility
exclusion gate (WATER_ZONE_PRODUCTION_SETBACK_METERS), unaffected by
anything here.

ROAD CORRIDOR STYLE: the road network can have several branches (a trunk
plus spur(s) growing off it or off each other, see road_corridors.py's own
module docstring) -- EVERY branch is drawn, each one independently as a
CASED (double-line) road symbol, the standard cartographic convention for
a road -- a wider, low-alpha dark gray "shoulder" plotted first, then a
narrower, higher-alpha dark gray line on top of it, both the same color
(see _draw_road_corridor()). All branches share the exact same styling --
nothing here visually distinguishes a trunk from a spur, and no branch
gets its own label on the map. Every branch, trunk and spur alike, collapses
into ONE "Suggested Road Corridor" legend entry (echoed by the legend's grey
cased-line icon); branch_role/branch_index/length/grade are for the narrative
report to read off each branch's own Feature, never a map or legend label
here. Before
either line of a branch is drawn, THAT branch's own Mercator-projected
geometry is run through _smooth_line_for_render(), independently of every
other branch: a shapely simplify() pass (ROAD_RENDER_SIMPLIFY_TOLERANCE_M,
Douglas-Peucker, preserve_topology=True) removes the DEM's own per-cell
stairstepping, then Chaikin corner-cutting (ROAD_RENDER_SMOOTHING_
ITERATIONS iterations, see _chaikin_smooth_coords()) rounds what's left.
Both are REAL BUG fixes, found live: the raw route geometry is a literal
per-DEM-cell polyline (road_cost_path.cost_distance_field()/
backtrace_route(), which road_network_router.route_road_network() walks
one cell at a time), which reads as a visibly blocky, stairstepped line at
the map's actual output resolution rather than a plausible road alignment.
Smoothing per branch, not on some pre-merged whole-network line, matters
for a spur specifically: _chaikin_smooth_coords() keeps a line's own first
and last coordinates exactly (see that function's own docstring), and a
spur's own first cell IS its join point on its parent branch (build_road_
network()'s own "cells" ordering, joint-cell first) -- smoothing each
branch independently keeps that join point exact, so the rendered network
still reads as connected rather than a spur floating just off the trunk.
This is DISPLAY-ONLY, same pattern as the water zone's own ripple texture
above: each branch's real points_xyz/geometry_wgs84 (used for length_m,
avg_grade_pct, and every other scoring/narrative value) are never
touched -- only the copy handed to the plotting calls is
simplified/smoothed.

TREE ZONE STYLE: each ranked tree-zone candidate patch (there can be
several, same "possibly-multiple, ranked" shape as production zones -- and
now the road corridor's own branches, though unlike this ranked-candidate
list, branches aren't ranked, just drawn -- not a single selection like
water/structure) renders as HATCH-ONLY --
diagonal hatch strokes (TREE_ZONE_COLOR, TREE_ZONE_HATCH_ALPHA,
TREE_ZONE_HATCH) with NO fill and NO perimeter stroke (facecolor="none",
linewidth=0 -- see this module's own tree-zone plot_polygon() call for
why that specific combination, not a cleared edgecolor, is what achieves
this: a patch's hatch color follows its edgecolor, and a patch's hatch
stroke width comes from matplotlib's own rcParams["hatch.linewidth"],
independent of the patch's own linewidth). The geometry drawn is still
its own render_fill_polygon_utm -- which for tree zones is the patch's real
cell-union footprint, UNMODIFIED (identical to polygon_utm): no hull, no
opening, no smoothing of any kind. This DIVERGES from production_area.py's
and water_candidate_zones.py's own render fills, which DO open/hull their
geometry -- deliberately, because tree candidates are the thin, branching
leftover ground threading between production/water/road, and those narrow
arms and interior pockets are exactly the geometry this layer exists to
find (a windbreak row, a riparian strip, a narrow edge planting). An
opening would delete them; a thin arm is unworkable as a cultivation block
or a pond but is a genuinely useful tree feature. So the hatch follows the
real cell-union shape verbatim -- thin arms and all -- and interior canopy
pockets stay OPEN as real holes, which is correct: the zone genuinely wraps
around existing canopy. render_fill_polygon_utm may still be a MultiPolygon
whenever the real footprint is (a candidate split across search-space gaps).
It is NOT the patch's real geometry_wgs84 field, but geometrically equal to
polygon_utm. There is no on-map label and no per-zone identity: however many
tree candidates there are, they share ONE "Tree Crop Areas" legend entry
(echoed by the legend's green hatch icon), and the hatch texture alone reads
them as candidate ground (intentional, see LEGEND above). The hatch by
itself doesn't need to enclose a filled area to read as a distinct zone on
the map. The hatch is a deliberate, visible "candidate, not a committed
site" cue,
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
structure_site_icon() below) placed at its own representative_point(), not
a filled polygon over its full eligible footprint (up to the module's own
1-acre cap) -- there's exactly one structure site per property
(fetch_and_select_optimal_structure_site()'s own top-ranked candidate,
same "single selection" shape water_zone has -- road_corridor is now a
possibly-multi-branch NETWORK instead, see this module's own ROAD
CORRIDOR STYLE section), so a precise
point read at a glance, the way an icon on a printed map is meant to be
read, better matches how a real building site actually gets sited and
referenced than a shaded area does. This is DISPLAY-ONLY, same "real
geometry drives placement, display is separate" split this module already
uses for water/tree zones' own render_fill_polygon_utm above:
structure_site's real geometry (used for area_acres/suitability_score/the
narrative report) is completely untouched by this -- only what gets drawn
at its representative_point() changes. Its single "Permanent Building Site"
legend entry uses the barn/silo glyph lifted out of this same pin (the
legend's own building icon), carrying no data. The map-pin icon is NOT a
numbered marker (it never was one) -- it and the keypoint asterisks are the
only on-map point symbols that survive now that the numbered circle markers
are gone (see LEGEND above). Rendered last (after the tree zone candidates),
at zorder=43, the same z-order this layer already held before this change.

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
from matplotlib.legend_handler import HandlerBase
from matplotlib.lines import Line2D
from matplotlib.offsetbox import AnnotationBbox, OffsetImage
from matplotlib.patches import Patch, Polygon as MplPolygon
from matplotlib.image import BboxImage
from matplotlib.transforms import Bbox, TransformedBbox
from PIL import Image
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, MultiPolygon, Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely.plotting import plot_line, plot_points, plot_polygon

from contour_lines import compute_contour_lines
from fencing import identify_fencing
from parcel_data import ParcelData, fetch_parcel_data
from pipeline_context import build_pipeline_context
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

# EXCLUSION ZONES (exclusion_zones.identify_exclusion_zones()'s closed union
# of all five per-gate masks -- canopy, slope, hydric, roads, setback).
#
# NEUTRAL, DELIBERATELY NOT A HUE. On the two reference boundaries this
# covers 4.7 and 7.2 acres of a 13-16 acre parcel, which makes it the single
# largest thing on the map. Every other layer already owns a colour that
# means something (production green, water blue, road dark gray, structure
# red, tree green, contour brown), and a sixth hue this size would read as
# another FEATURE competing with them. A desaturated warm gray reads as
# ABSENCE instead -- ground the plan is not proposing anything on -- and
# leaves every real feature's colour uncontested. Flat fill, no edge stroke:
# a stroke would draw the eye to the cell-union staircase, which is a
# raster artifact and not a surveyed line.
EXCLUSION_ZONE_COLOR = "#6E6A63"          # CONFIGURABLE
EXCLUSION_ZONE_ALPHA = 0.42               # CONFIGURABLE
# BELOW EVERY OTHER LAYER. Production contours (40), water ripples (41),
# road corridor (42/42.5), tree hatch (42.8), exclusion fences
# (EXCLUSION_FENCE_ZORDER), keypoints, the structure pin and the boundary
# fence all sit on top; so do the streams (20). It sits just ABOVE the halo
# mask (10) only because the halo covers OFF-parcel ground and this covers
# ON-parcel ground -- the two never compete for the same pixels, and
# ordering it below the halo would mean the halo's white wash was painted
# over ground it is not meant to touch.
EXCLUSION_ZONE_ZORDER = 15                # CONFIGURABLE

# Water zones render as RIPPLE-LINE TEXTURE -- a family of horizontal sine
# waves clipped to the zone's own render_fill_polygon_utm -- rather than a
# filled/outlined polygon. Same styling discipline as production zones'
# contour-line texture and tree zones' hatch: no fill, no perimeter stroke,
# no on-map label -- the single "Water System Survey Area" legend entry (and
# its wave-train icon) carries the layer. The ripple stroke reuses
# WATER_ZONE_COLOR above (the water blue #1F6FB2, see FENCE_COLOR's own
# comment) rather than introducing a second blue.
#
# Spacing/amplitude/wavelength are in the SAME units as the drawing space
# (Mercator metres, see _reproject_utm_geometry_to_mercator()) -- not raw
# UTM metres, and not points. Tuned so a ~0.5-acre zone (roughly 45m across)
# reads as several distinct waves rather than one or two.
WATER_RIPPLE_SPACING = 6.0        # vertical gap between successive wave rows
WATER_RIPPLE_WAVELENGTH = 14.0    # horizontal period of each wave
WATER_RIPPLE_AMPLITUDE = 1.6      # peak deviation from the row's own baseline
WATER_RIPPLE_PHASE_STEP = 0.9     # radians of phase offset per successive row,
                                  # so waves stagger instead of stacking into
                                  # vertical columns
WATER_RIPPLE_SAMPLES_PER_WAVELENGTH = 16   # polyline resolution per period
WATER_RIPPLE_LINEWIDTH = 0.7
WATER_RIPPLE_ALPHA = 0.85

STRUCTURE_SITE_COLOR = "#D64545"

# Structure site renders as a single fixed-size map-pin icon (see this
# module's own STRUCTURE SITE STYLE docstring section), not a filled
# polygon -- source-of-truth vector asset at assets/icons/farm_location_pin.svg,
# rasterized ONCE to a PNG build artifact (assets/icons/farm_location_pin.png,
# tightly cropped to the drawn shape's own bounds -- not the full 24x24
# viewBox, which this asset's own tip doesn't reach -- so the bottom edge
# of the raster lines up with the pin's visual tip; checked into the repo
# alongside the SVG) rather than re-rasterized on every render call.
# Loaded via PIL into a decoded RGBA pixel array here, at module level, so
# repeated render_layout_map() calls in the same process never hit disk for
# it more than once. The array is inert data and is safe to share; the
# OffsetImage that wraps it is NOT -- an OffsetImage is a matplotlib Artist,
# which binds to the first figure it is added to and raises RuntimeError
# ("Can not put single artist in more than one figure") on the second. So the
# artist must never live at module scope; it is built fresh per render by the
# structure_site_icon() factory below.
STRUCTURE_SITE_ICON_PATH = os.path.join(os.path.dirname(__file__), "assets", "icons", "farm_location_pin.png")
# Empirically tuned against this module's own real output (300 DPI,
# 8.5in x 11in figure -- see FIGURE_SIZE_INCHES/OUTPUT_DPI above): a zoom
# of ~0.035 rendered this icon ~36px tall at that resolution (the original
# 30-40px legibility target); this is that baseline x3, per explicit
# request, for a more prominent on-map pin. CONFIGURABLE.
STRUCTURE_SITE_ICON_ZOOM = 0.105
_STRUCTURE_SITE_ICON_ARRAY = np.asarray(
    Image.open(STRUCTURE_SITE_ICON_PATH).convert("RGBA")
)


def structure_site_icon():
    """Fresh OffsetImage per figure. An OffsetImage binds to the first figure
    it is added to and raises RuntimeError on the second -- it must never be
    module-level state."""
    return OffsetImage(_STRUCTURE_SITE_ICON_ARRAY, zoom=STRUCTURE_SITE_ICON_ZOOM)

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

# Keypoints render as a plain black asterisk -- no fill variation by index, and
# identical for on-parcel and off-parcel. They are a set of like features (one
# per primary valley), not individually narrated elements, so they get a single
# collapsed legend entry ("Keypoint Candidates") regardless of count -- the same
# one-entry-per-feature treatment every other layer now gets in the icon legend
# (see this module's own LEGEND docstring section). The asterisk IS the on-map
# symbol for this layer, drawn directly; the legend just mirrors it (a stock
# Line2D handle with the same (6, 2, 0) marker tuple, see LEGEND_HANDLES below).
KEYPOINT_COLOR = "#000000"

# Keypoint asterisk marker size (matplotlib markersize, points) -- a fixed size,
# tuned by eye against this module's own 300 DPI output.
KEYPOINT_MARKER_SIZE_POINTS = 10.0
# The asterisk's spoke line width -- a hairline heavy enough to read at the size
# above without turning into a blob.
KEYPOINT_MARKER_LINEWIDTH = 1.4

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
# above the structure site pin (zorder=43) as well as every zone-fill-style
# layer beneath it, per explicit request (was 50, bumped again to 60 per a
# later explicit request; it once had to clear the numbered circle markers at
# zorder=50 too, but those markers no longer exist -- see this module's own
# LEGEND docstring section). Deliberately NOT applied to the water-zone/tree-
# zone exclusion fence loops, nor to roads, tree zone, water zones, or streams
# -- those keep rendering at EXCLUSION_FENCE_ZORDER (water/tree exclusion
# fencing) or their own existing zorder (tree zone hatch, water zone ripples,
# stream lines) below, unchanged. The icon legend sits at this same zorder and,
# being drawn last, stays on top of the boundary fence (see LEGEND FRAME below).
# CONFIGURABLE.
FENCE_ZORDER = 60

# Zorder for the two non-boundary exclusion fence loops (water_zone_exclusion,
# tree_zone_exclusion) -- this is the value FENCE_ZORDER itself used to hold
# before being bumped up for the boundary fence alone (see that constant's own
# comment). Kept here, unchanged, so these two fence loops still render above
# every zone-fill-style layer (production contour zorder=40, water zone ripples
# zorder=41, road corridor cased line zorder=42/42.5, tree zone candidate hatch
# zorder=42.8 -- the true current ceiling, not 40) without being bumped above
# the structure site pin the way the boundary fence now is (there are no longer
# any numbered circle markers to clear -- see this module's own LEGEND section).
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

# DISPLAY-ONLY coincidence tolerance (meters) for trimming a water/tree zone
# fence ring where it runs on top of ANOTHER drawn fence ring -- the boundary
# fence OR another zone's fence. find_boundary_fencing() unions each zone's OWN
# buffered polygon (via the shared _buffered_zone_polygon()) into the boundary
# fence, so along a shared stretch the two rings are data-exact coincident -- but
# each ring is angular-simplified INDEPENDENTLY before drawing, and independent
# simplification of the same stretch (in the context of each ring's own different
# overall shape) can keep different vertices, rendering as two visibly separate
# near-parallel lines. Two ADJACENT zones produce the same visual mess for the same
# reason: each zone's fence is its own zone polygon buffered by the same amount, so
# where the zones neighbour each other the two rings run near-parallel a buffer's
# width apart. AFTER every ring is simplified, each zone ring is trimmed by the
# union of all the OTHER drawn rings buffered by this tolerance, so the redundant
# (doubled) portion of the RENDERED line is suppressed. That trim is SYMMETRIC, not
# priority-based: where two zones run close BOTH rings lose the near-shared stretch,
# leaving a real visible gap between them -- the intended result, not a defect to
# close. Wide enough to catch NEAR-coincident stretches (where independent
# simplification left the rings a few meters apart), not just pixel-exact overlap;
# note that widening it widens that inter-zone gap too. Render-only -- every zone's
# full, untrimmed ring is still written to fencing_geojson by fencing.py.
# CONFIGURABLE.
ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M = 5.0  # was 1.0

# DISPLAY-ONLY simplify tolerance for the production zone fill used as the
# contour clip mask -- expressed in DEM CELLS, multiplied by the DEM's own cell
# size at the point of use (dem["resolution_meters"] is in scope in
# render_layout_map()), so it stays "one cell" at any resolution instead of
# hardcoding ~5m. The production fill is a true cell-union boundary (a 5m
# right-angle staircase after the bounded opening); simplifying collapses that
# staircase's collinear runs down to the shape's real turns, so the Chaikin pass
# below has actual corners to round rather than hundreds of individual cell
# steps. Separate constant from the fence/road render tolerances even though it
# starts near them -- three different geometries with three different retuning
# pressures, per the standing convention. CONFIGURABLE.
PRODUCTION_FILL_SIMPLIFY_TOLERANCE_CELLS = 1.0

# Post-simplify Chaikin softening for the production fill clip mask. Kept small
# deliberately: this geometry is a clip mask for contour lines, so over-
# smoothing moves where every contour terminates. Separate from the fence/road
# Chaikin iteration counts by the same standing convention. CONFIGURABLE --
# start light.
PRODUCTION_FILL_CHAIKIN_ITERATIONS = 1


# ===========================================================================
# ICON LEGEND
#
# The legend is a real ax.legend() whose handles are DRAWN symbols matching
# what each layer draws on the map (see this module's own LEGEND docstring
# section) -- not the old plain-text ax.text box. Every icon reuses its
# layer's own COLOUR constant (a legend symbol in a different colour from its
# map symbol would defeat the point of the legend), but its SIZE/LINEWIDTH/
# geometry are the legend's OWN independent constants below: a legend handle
# is drawn in handle-box space at a fixed small size, while a map stroke is
# tuned against the 300 DPI 8.5x11 output, and the two retune under different
# pressures. Each constant is defined independently even where it starts out
# numerically identical to a map constant. FENCE_LINEWIDTH in particular is a
# 0.6 hairline that would be invisible at legend scale, so the legend octagon
# gets LEGEND_FENCE_LINEWIDTH instead. Every value here is CONFIGURABLE.
# ===========================================================================

# Keypoint Candidates -- a stock Line2D handle with the SAME (6, 2, 0) asterisk
# marker tuple _draw_keypoint_marker() uses, sized for the handle box.
LEGEND_KEYPOINT_MARKERSIZE = 9.0          # CONFIGURABLE
LEGEND_KEYPOINT_MARKEREDGEWIDTH = 1.3     # CONFIGURABLE

# Production Areas -- several stacked, near-parallel curved arcs (concentric,
# increasing radius) in CONTOUR_LINE_COLOR: reads as CONTOUR LINES, not ripples.
LEGEND_PRODUCTION_ARC_COUNT = 3           # CONFIGURABLE
LEGEND_PRODUCTION_LINEWIDTH = 1.0         # CONFIGURABLE
LEGEND_PRODUCTION_ARC_SAMPLES = 48        # polyline resolution per arc; CONFIGURABLE

# Water System Survey Area -- parallel sine waves in WATER_ZONE_COLOR, mirroring
# _ripple_lines_for_polygon()'s staggered wave train (distinguishable from the
# production arcs at legend scale: production is irregular nested arcs, water is
# a regular staggered wave train).
LEGEND_WATER_WAVE_ROWS = 3                # CONFIGURABLE
LEGEND_WATER_LINEWIDTH = 0.9              # CONFIGURABLE
LEGEND_WATER_WAVE_CYCLES = 2.0            # full sine periods across the handle width; CONFIGURABLE
LEGEND_WATER_WAVE_AMPLITUDE_FRAC = 0.14   # amplitude as a fraction of box height; CONFIGURABLE
LEGEND_WATER_WAVE_PHASE_STEP = 0.9        # radians of phase stagger per row (mirrors WATER_RIPPLE_PHASE_STEP); CONFIGURABLE
LEGEND_WATER_WAVE_SAMPLES = 40            # polyline resolution per wave row; CONFIGURABLE

# Suggested Road Corridor -- the cased (double-line) road symbol: outer shoulder
# then inner line, reusing ROAD_RENDER_COLOR / ROAD_RENDER_*_ALPHA, but its own
# wider handle-scale widths (the map's 3.0/1.5 read as hairlines in the box).
LEGEND_ROAD_OUTER_WIDTH = 5.0             # CONFIGURABLE
LEGEND_ROAD_INNER_WIDTH = 2.2             # CONFIGURABLE

# Permanent Building Site -- the barn/silo glyph alone (not the full teardrop
# pin), drawn via a BboxImage sized to the handle box. The glyph is a checked-in
# build artifact (assets/icons/farm_building_glyph.png), rasterized ONCE from
# its own source SVG (assets/icons/farm_building_glyph.svg -- the inner farm
# icon lifted out of farm_location_pin.svg's teardrop, recoloured to the
# structure red) the same way farm_location_pin.png is produced from its SVG.
# LEGEND_BUILDING_GLYPH_ZOOM is the glyph height as a fraction of the handle-box
# height (aspect ratio preserved); the pixel array loads at module level (inert,
# shareable) and the BboxImage artist is built fresh per render inside the
# handler, the same discipline structure_site_icon()'s docstring explains for
# OffsetImage (a matplotlib Artist binds to its first figure).
LEGEND_BUILDING_GLYPH_ZOOM = 1.15         # CONFIGURABLE
LEGEND_BUILDING_GLYPH_PATH = os.path.join(os.path.dirname(__file__), "assets", "icons", "farm_building_glyph.png")
_LEGEND_BUILDING_GLYPH_ARRAY = np.asarray(Image.open(LEGEND_BUILDING_GLYPH_PATH).convert("RGBA"))

# Fencing -- a regular octagon outline in FENCE_COLOR, no fill.
LEGEND_FENCE_LINEWIDTH = 1.4              # NOT FENCE_LINEWIDTH (0.6 -- invisible here); CONFIGURABLE
LEGEND_FENCE_OCTAGON_RADIUS_FRAC = 0.46   # octagon radius as a fraction of min(box w, box h); CONFIGURABLE

# Fixed KSOP legend labels -- exactly these strings, in exactly this order (see
# this module's own LEGEND docstring section). No numbering, no per-feature data.
LEGEND_LABEL_EXCLUSION = "Unsuitable Ground"
LEGEND_LABEL_KEYPOINTS = "Keypoint Candidates"
LEGEND_LABEL_PRODUCTION = "Production Areas"
LEGEND_LABEL_WATER = "Water System Survey Area"
LEGEND_LABEL_ROAD = "Suggested Road Corridor"
LEGEND_LABEL_TREE = "Tree Crop Areas"
LEGEND_LABEL_STRUCTURE = "Permanent Building Site"
LEGEND_LABEL_FENCING = "Fencing"


class _ProductionLegendHandle:
    """Proxy handle for the Production Areas legend entry -- drawn by
    _HandlerContourArcs (mapped in _LEGEND_HANDLER_MAP)."""


class _WaterLegendHandle:
    """Proxy handle for the Water System Survey Area entry -- drawn by
    _HandlerWaterWaves."""


class _RoadLegendHandle:
    """Proxy handle for the Suggested Road Corridor entry -- drawn by
    _HandlerCasedRoad."""


class _BuildingLegendHandle:
    """Proxy handle for the Permanent Building Site entry -- drawn by
    _HandlerBuildingGlyph."""


class _FenceLegendHandle:
    """Proxy handle for the Fencing entry -- drawn by _HandlerOctagon."""


class _HandlerContourArcs(HandlerBase):
    """Draws several stacked, near-parallel curved arcs (concentric, increasing
    radius) in CONTOUR_LINE_COLOR -- the legend symbol for production zones'
    contour-line texture. Nested arcs, NOT a uniform sine train: this must read
    as contour lines, visually distinct from the water icon's regular wave
    train."""

    def create_artists(self, legend, orig_handle, xdescent, ydescent, width, height, fontsize, trans):
        x0 = -xdescent
        cx = x0 + width / 2.0
        samples = LEGEND_PRODUCTION_ARC_SAMPLES
        xs = [x0 + width * k / samples for k in range(samples + 1)]
        # A circle centre below the box gives gentle upward-bowing arcs; stepping
        # the radius up per arc nests them, so they read as offset contour lines.
        base_radius = 1.3 * height
        center_y = -ydescent - base_radius
        artists = []
        for i in range(LEGEND_PRODUCTION_ARC_COUNT):
            radius = base_radius + height * (i + 0.5) / LEGEND_PRODUCTION_ARC_COUNT
            ys = []
            for x in xs:
                dx = x - cx
                inside = radius * radius - dx * dx
                ys.append(center_y + math.sqrt(inside) if inside > 0 else center_y)
            line = Line2D(
                xs, ys, color=CONTOUR_LINE_COLOR, linewidth=LEGEND_PRODUCTION_LINEWIDTH,
                solid_capstyle="round",
            )
            line.set_transform(trans)
            artists.append(line)
        return artists


class _HandlerWaterWaves(HandlerBase):
    """Draws parallel, phase-staggered sine waves in WATER_ZONE_COLOR, mirroring
    _ripple_lines_for_polygon()'s output -- the legend symbol for the water
    zone's ripple texture. A regular staggered wave train, distinguishable from
    the production icon's irregular nested arcs."""

    def create_artists(self, legend, orig_handle, xdescent, ydescent, width, height, fontsize, trans):
        x0 = -xdescent
        samples = LEGEND_WATER_WAVE_SAMPLES
        xs = [x0 + width * k / samples for k in range(samples + 1)]
        amplitude = LEGEND_WATER_WAVE_AMPLITUDE_FRAC * height
        rows = LEGEND_WATER_WAVE_ROWS
        angular = 2.0 * math.pi * LEGEND_WATER_WAVE_CYCLES / width
        artists = []
        for i in range(rows):
            baseline = -ydescent + height * (i + 1) / (rows + 1)
            phase = i * LEGEND_WATER_WAVE_PHASE_STEP
            ys = [baseline + amplitude * math.sin(angular * (x - x0) + phase) for x in xs]
            line = Line2D(
                xs, ys, color=WATER_ZONE_COLOR, linewidth=LEGEND_WATER_LINEWIDTH,
                solid_capstyle="round",
            )
            line.set_transform(trans)
            artists.append(line)
        return artists


class _HandlerCasedRoad(HandlerBase):
    """Draws the cased (double-line) road symbol: a wider low-alpha outer
    shoulder, then a narrower higher-alpha inner line on top -- same outer-then-
    inner order and same ROAD_RENDER_COLOR/alpha constants as
    _draw_road_corridor(), at the legend's own handle-scale widths."""

    def create_artists(self, legend, orig_handle, xdescent, ydescent, width, height, fontsize, trans):
        y = -ydescent + height / 2.0
        xs = [-xdescent, -xdescent + width]
        outer = Line2D(
            xs, [y, y], color=ROAD_RENDER_COLOR, linewidth=LEGEND_ROAD_OUTER_WIDTH,
            alpha=ROAD_RENDER_OUTER_ALPHA, solid_capstyle="butt",
        )
        inner = Line2D(
            xs, [y, y], color=ROAD_RENDER_COLOR, linewidth=LEGEND_ROAD_INNER_WIDTH,
            alpha=ROAD_RENDER_INNER_ALPHA, solid_capstyle="butt",
        )
        outer.set_transform(trans)
        inner.set_transform(trans)
        return [outer, inner]  # outer first, inner on top -- same order as the map


class _HandlerBuildingGlyph(HandlerBase):
    """Draws the barn/silo glyph (assets/icons/farm_building_glyph.png) via a
    BboxImage sized to a centred sub-rectangle of the handle box. The pixel
    array is module-level (inert, shareable); the BboxImage Artist is built
    fresh here per render (a matplotlib Artist binds to its first figure -- same
    reason structure_site_icon() rebuilds its OffsetImage per figure)."""

    def create_artists(self, legend, orig_handle, xdescent, ydescent, width, height, fontsize, trans):
        img = _LEGEND_BUILDING_GLYPH_ARRAY
        img_h, img_w = img.shape[0], img.shape[1]
        aspect = img_w / img_h
        target_h = LEGEND_BUILDING_GLYPH_ZOOM * height
        target_w = target_h * aspect
        cx = -xdescent + width / 2.0
        cy = -ydescent + height / 2.0
        bbox = Bbox.from_bounds(cx - target_w / 2.0, cy - target_h / 2.0, target_w, target_h)
        image = BboxImage(TransformedBbox(bbox, trans), interpolation="antialiased")
        image.set_data(img)
        return [image]


class _HandlerOctagon(HandlerBase):
    """Draws a regular octagon outline in FENCE_COLOR, no fill -- the legend
    symbol for the fence layers."""

    def create_artists(self, legend, orig_handle, xdescent, ydescent, width, height, fontsize, trans):
        cx = -xdescent + width / 2.0
        cy = -ydescent + height / 2.0
        radius = LEGEND_FENCE_OCTAGON_RADIUS_FRAC * min(width, height)
        # pi/8 offset -> a flat-topped octagon (stop-sign orientation).
        verts = [
            (cx + radius * math.cos(math.pi / 8 + k * math.pi / 4),
             cy + radius * math.sin(math.pi / 8 + k * math.pi / 4))
            for k in range(8)
        ]
        octagon = MplPolygon(
            verts, closed=True, facecolor="none", edgecolor=FENCE_COLOR,
            linewidth=LEGEND_FENCE_LINEWIDTH,
        )
        octagon.set_transform(trans)
        return [octagon]


# Maps each proxy handle CLASS to its handler instance. Handles for the two
# stock icons (Keypoint Candidates -> Line2D, Tree Crop Areas -> Patch) are not
# here: matplotlib's own default handlers draw them.
_LEGEND_HANDLER_MAP = {
    _ProductionLegendHandle: _HandlerContourArcs(),
    _WaterLegendHandle: _HandlerWaterWaves(),
    _RoadLegendHandle: _HandlerCasedRoad(),
    _BuildingLegendHandle: _HandlerBuildingGlyph(),
    _FenceLegendHandle: _HandlerOctagon(),
}


def _legend_handle_for(feature: str):
    """Builds a fresh (handle, label) pair for one present feature. Stock
    handles (keypoints, tree) are real Line2D/Patch proxies carrying their own
    style; the five custom icons return a lightweight proxy handle whose drawing
    is delegated through _LEGEND_HANDLER_MAP. Handles are built per render rather
    than cached at module scope, the same Artist-per-figure discipline
    structure_site_icon() follows."""
    if feature == "keypoints":
        return (
            Line2D(
                [], [], marker=(6, 2, 0), linestyle="None", color=KEYPOINT_COLOR,
                markersize=LEGEND_KEYPOINT_MARKERSIZE, markeredgewidth=LEGEND_KEYPOINT_MARKEREDGEWIDTH,
            ),
            LEGEND_LABEL_KEYPOINTS,
        )
    if feature == "exclusion":
        # A plain filled swatch in the map's own fill colour and alpha --
        # the layer draws as flat fill with no edge, so the swatch does too.
        return (
            Patch(facecolor=EXCLUSION_ZONE_COLOR, edgecolor="none", alpha=EXCLUSION_ZONE_ALPHA),
            LEGEND_LABEL_EXCLUSION,
        )
    if feature == "production":
        return (_ProductionLegendHandle(), LEGEND_LABEL_PRODUCTION)
    if feature == "water":
        return (_WaterLegendHandle(), LEGEND_LABEL_WATER)
    if feature == "road":
        return (_RoadLegendHandle(), LEGEND_LABEL_ROAD)
    if feature == "tree":
        return (
            Patch(
                facecolor="none", edgecolor=TREE_ZONE_COLOR, hatch=TREE_ZONE_HATCH,
                linewidth=0, alpha=TREE_ZONE_HATCH_ALPHA,
            ),
            LEGEND_LABEL_TREE,
        )
    if feature == "structure":
        return (_BuildingLegendHandle(), LEGEND_LABEL_STRUCTURE)
    if feature == "fencing":
        return (_FenceLegendHandle(), LEGEND_LABEL_FENCING)
    raise ValueError(f"unknown legend feature {feature!r}")


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


def _ripple_lines_for_polygon(geom, spacing, wavelength, amplitude,
                              phase_step, samples_per_wavelength):
    """
    Horizontal sine-wave polylines clipped to `geom`, for water zone ripple
    texture. Returns a list of drawable LineStrings.

    Mirrors how production zones are drawn: generate a family of lines over
    the shape's own bounds, clip each with a real shapely intersection, and
    draw only the surviving segments. No fill, no perimeter stroke.

    Successive rows are phase-offset by `phase_step` so the wave crests
    stagger rather than aligning into vertical columns.

    `geom` may be a MultiPolygon -- the water zone's render fill is a bounded
    opening and can be severed at a narrow pinch. shapely's intersection
    handles that directly; do not iterate .geoms and do not assume a single
    exterior ring.
    """
    # Display-only, must never fail a render: bail to an empty list on any
    # empty/invalid input rather than raising.
    if geom is None or geom.is_empty or not geom.is_valid:
        return []
    if spacing <= 0 or wavelength <= 0:
        return []
    minx, miny, maxx, maxy = geom.bounds
    if not all(math.isfinite(v) for v in (minx, miny, maxx, maxy)):
        return []

    # Pad the x-range by one full wavelength on each side and the y-range by
    # `amplitude` on each side, so each wave enters and leaves the shape from
    # outside (clipped at the shape edge) rather than starting or ending on a
    # polyline vertex mid-shape.
    x_start = minx - wavelength
    x_end = maxx + wavelength
    y_start = miny - amplitude
    y_end = maxy + amplitude

    # Polyline resolution: `samples_per_wavelength` points per period, so the
    # sampled sine reads as a smooth curve rather than a coarse zigzag.
    step = wavelength / max(1, int(samples_per_wavelength))
    n_x = int(math.ceil((x_end - x_start) / step))
    xs = [x_start + i * step for i in range(n_x + 1)]
    if xs[-1] < x_end:
        xs.append(x_end)

    angular_freq = 2.0 * math.pi / wavelength
    lines = []
    row = 0
    while True:
        y = y_start + row * spacing
        if y > y_end + 1e-9:
            break
        phase = row * phase_step
        coords = [(x, y + amplitude * math.sin(angular_freq * x + phase)) for x in xs]
        wave = LineString(coords)
        try:
            clipped = wave.intersection(geom)
        except Exception:
            # A malformed row must not sink the whole render -- skip it.
            row += 1
            continue
        # intersection() can return LineString / MultiLineString /
        # GeometryCollection / empty (and stray Point touches); keep only the
        # line-like pieces, same as the production-zone contour clip above.
        lines.extend(_iter_line_parts(clipped))
        row += 1
    return lines




def _chaikin_smooth_coords(coords: list[tuple[float, float]], iterations: int) -> list[tuple[float, float]]:
    """
    Chaikin's corner-cutting subdivision, run `iterations` times, over an
    OPEN polyline (a road corridor, not a closed ring) -- simple enough
    to not need a new spline/smoothing dependency for what's purely a
    cosmetic rendering touch-up (see _smooth_line_for_render()).

    The first and last coordinates are always kept EXACTLY as given, so a
    smoothed branch still starts/ends at the same real endpoint -- the
    anchor for a trunk, or the join-on-the-parent-branch cell for a spur
    (build_road_network()'s own "cells" ordering puts a spur's join point
    first) -- only the interior gets rounded. This is exactly why
    _smooth_line_for_render() must be called once PER BRANCH rather than
    on some pre-merged whole-network line: smoothing each branch
    independently is what keeps a spur's own join point exact, so the
    rendered network still reads as connected. Each iteration replaces
    every edge
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
    Rendering-only transform for a SINGLE road branch's own LineString --
    the caller (render_layout_map()) calls this once PER BRANCH, never on
    a pre-merged whole-network line (see this module's own ROAD CORRIDOR
    STYLE docstring section for why: a spur's own first coordinate is its
    real join point on the parent branch, and only per-branch smoothing
    keeps that exact). REAL BUG, FOUND LIVE: road_network_router.py's own
    route geometry is a literal per-DEM-cell polyline (road_cost_path.
    cost_distance_field()/backtrace_route() walk the DEM's grid one cell
    at a time), which reads as a visibly blocky, stairstepped line at this
    map's actual output resolution rather than a plausible road alignment.

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


def _smooth_production_fill_for_render(fill, simplify_tolerance: float, chaikin_iterations: int):
    """
    DISPLAY-ONLY smoothing of a production zone's render_fill_polygon_utm for
    use as the contour-clip mask -- softens the 5m cell-union staircase into a
    clean curve so contour lines terminate along a field edge rather than a
    frayed comb of 5m steps. This does NOT touch the stored
    render_fill_polygon_utm the four consumer modules (water_candidate_zones.py,
    road_corridors.py, tree_zone_candidates.py, solar_suitability.py) read as
    production EXCLUSION geometry -- the caller passes the field in and clips the
    smoothed result to polygon_utm before use; the stored field is unchanged.

    The existing ring smoothers (_angular_then_smooth_closed_ring() and the
    simplify/Chaikin pair it chains) operate on a SINGLE closed ring (a fence
    loop). A production fill is routinely a MultiPolygon after the opening and
    may carry interior rings, so this wraps that same helper at the POLYGON
    level: it smooths the exterior ring AND every interior ring of every part
    and reassembles into a Polygon/MultiPolygon.

    Display-only degradation: if smoothing yields empty or invalid geometry
    (e.g. a Chaikin pass self-intersecting a very thin sliver), this returns the
    INPUT geometry unchanged rather than raising -- a bad smooth must fall back
    to the unsmoothed shape, never fail the render.
    """
    def _smooth_ring_coords(ring):
        return list(
            _angular_then_smooth_closed_ring(
                LineString(ring.coords), simplify_tolerance, chaikin_iterations
            ).coords
        )

    def _smooth_polygon(poly):
        exterior = _smooth_ring_coords(poly.exterior)
        interiors = [_smooth_ring_coords(interior) for interior in poly.interiors]
        return Polygon(exterior, interiors)

    try:
        if fill.geom_type == "MultiPolygon":
            smoothed = MultiPolygon([_smooth_polygon(part) for part in fill.geoms])
        elif fill.geom_type == "Polygon":
            smoothed = _smooth_polygon(fill)
        else:
            return fill
        if not smoothed.is_valid:
            smoothed = smoothed.buffer(0)  # heal minor ring self-touch from corner-cutting
        if smoothed.is_empty or not smoothed.is_valid:
            return fill
        return smoothed
    except (ValueError, TypeError):
        # A degenerate ring (too few coordinates after simplify, etc.) can make
        # Polygon()/MultiPolygon() raise -- degrade to the unsmoothed fill.
        return fill


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


def _draw_keypoint_marker(ax, point) -> None:
    """A plain black asterisk at a keypoint -- IDENTICAL for on-parcel and
    off-parcel keypoints (no fill, size, or colour variation). Keypoints are a
    set of like features, not individually narrated elements (see KEYPOINT_
    COLOR's own comment); they collapse to a single "Keypoint Candidates" legend
    entry regardless of count.

    The marker is matplotlib's true asterisk -- the polygon marker (6, 2, 0):
    six spokes, style 2 (the asterisk style), angle 0 -- NOT "*" (a
    five-pointed star in matplotlib) and not a mathtext "$*$" (which renders
    small and sits high off-centre at this size). The legend's own keypoint
    handle reuses this exact (6, 2, 0) tuple. Drawn at the keypoint layer's
    existing zorder (50)."""
    ax.plot(
        point.x,
        point.y,
        marker=(6, 2, 0),
        markersize=KEYPOINT_MARKER_SIZE_POINTS,
        markeredgecolor=KEYPOINT_COLOR,
        markerfacecolor=KEYPOINT_COLOR,
        markeredgewidth=KEYPOINT_MARKER_LINEWIDTH,
        linestyle="None",
        zorder=50,
    )


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

    No per-feature legend/reporting data is derived here anymore. The icon
    legend render_layout_map() now draws carries NO data in any label (see
    that function's own LEGEND docstring section), so this function no longer
    computes production acreage/percent-of-parcel display strings, and the
    return dict below carries no per-feature legend-stats key (or any
    replacement). Everything the report generator needs stays on its own
    upstream source -- production acreage/rank on context.production_areas,
    water id/score on context.selected_water_zone, tree rank/score/acres on
    context.tree_zone_patches, keypoints on context.keypoints, road branch
    figures on the road_corridor Features below, structure score on the
    structure_site Feature -- and wiring the report generator to those sources
    is separate, out-of-scope work.

    water_zone is context.selected_water_zone directly -- water_
    suitability.fetch_and_select_optimal_water_zone()'s own return value
    and this field are the exact same scored-zone dict shape (see
    pipeline_context.py's own field notes), so this is fully free: no
    additional call at all.

    road_corridor_candidates: full return dict still needed for its
    zones_geojson (one GeoJSON Feature PER BRANCH this function renders --
    every branch, not just the trunk -- unlike context.selected_road_
    corridor's raw-UTM-with-cells network shape) -- one additional
    identify_road_corridor_candidates() call, but now passing every
    override that entry point supports (dem/boundary_polygon_utm/
    production_areas/valleys/selected_water_zone/hydric_floodplain_union/
    floodplain_data_is_fallback, all straight from context) so this call's
    own internal production/water/floodplain-union derivation is fully
    eliminated, not just its DEM fetch. selected_road_corridor is read
    from context directly rather than re-extracted from this call's own
    result -- same overrides in, so the same deterministic network comes
    out; reusing avoids a needless "trust this matches" moment.

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
    # Every branch's own Feature, trunk and spur(s) alike -- NOT just
    # branch_index 0 -- see this module's own ROAD CORRIDOR STYLE
    # docstring section for why taking only the trunk would silently omit
    # real, built geometry.
    road_corridor_features = road_corridor_candidates["zones_geojson"]["features"]
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

    # Developed-footprint inputs for the rewritten boundary fence (fencing.find_boundary_
    # fencing()): the boundary fence now protects production + structure + road corridor
    # path, not the full drawn boundary. All of these are ALREADY in memory here from this
    # function's own upstream fetches -- no new network call, just extract each layer's own
    # already-computed UTM geometry and thread it through. production_zone_polygons_utm =
    # each ceiling-trimmed production zone's own render_fill_polygon_utm (production_area_
    # ceiling.py's scored_patches, carried on context.production_areas); structure_site_
    # feature = solar_suitability.py's own final rank-1 GeoJSON Feature (structure_site,
    # reprojected inside identify_fencing()); road_corridor_cell_footprint_polygon_utm =
    # the corridor's own undilated path footprint (road_corridors.py's top-level cell_
    # footprint_polygon_utm), None when no corridor was selected. Water/tree developed-edge
    # inputs are NOT threaded here -- identify_fencing() reuses the same render_fill_polygon_
    # utm values it already derives from selected_water_zone/tree_zone_patches (already
    # passed below), which is what makes the boundary fence meet those zones' fences exactly.
    production_zone_polygons_utm = [
        patch["render_fill_polygon_utm"]
        for patch in (context.production_areas or [])
        if patch.get("render_fill_polygon_utm") is not None
    ]
    road_corridor_cell_footprint_polygon_utm = (
        selected_road_corridor["cell_footprint_polygon_utm"] if selected_road_corridor else None
    )

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
        production_zone_polygons_utm=production_zone_polygons_utm,
        structure_site_feature=structure_site,
        road_corridor_cell_footprint_polygon_utm=road_corridor_cell_footprint_polygon_utm,
    )

    return {
        "dem": context.dem,
        # The parcel's unselectable ground, straight off the context (its
        # FIRST Layer 2 computation -- see pipeline_context.py). Nothing is
        # computed here: build_pipeline_context() above already produced it
        # and no other layer in this dict depends on it.
        "exclusion_zones": context.exclusion_zones,
        "production_areas": context.production_areas,
        "water_zone": water_zone,
        "road_corridor": road_corridor_features,
        "tree_zone_result": tree_zone_result,
        "structure_site": structure_site,
        # context.keypoints IS keypoint_detection.detect_keypoints()'s own
        # per-valley list (see pipeline_context.py's field notes) -- carried
        # through to render_layout_map() for an asterisk per keypoint, and to the
        # report generator for narration. No second computation here.
        "keypoints": context.keypoints,
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
    water_zone = layers["water_zone"]
    road_corridor_features = layers["road_corridor"]
    tree_zone_result = layers["tree_zone_result"]
    structure_site = layers["structure_site"]
    # .get() so a pre-fetched layers dict built before keypoints existed (e.g.
    # the synthetic fixtures in test_render_layout_map.py) still renders --
    # missing key means simply "no keypoint layer to draw", not an error.
    keypoints = layers.get("keypoints", [])
    # .get() for the same reason keypoints uses it -- a layers dict built
    # before this layer existed (the synthetic fixtures in
    # test_render_layout_map.py) still renders, with simply no exclusion
    # layer to draw.
    exclusion_result = layers.get("exclusion_zones") or {}
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

    # z-order, back to front: halo mask, exclusion zones, streams, production zone contours,
    # water zone ripples, road corridor line, tree zone hatch, the water/tree
    # exclusion fence loops (EXCLUSION_FENCE_ZORDER, above every zone fill --
    # see that constant's own comment), keypoint asterisks, structure site pin,
    # the boundary fence (FENCE_ZORDER, above the structure pin -- see that
    # constant's own comment), then the icon legend and basemap note on top.
    #
    # Each layer sets a presence flag as it draws; the legend is assembled ONCE
    # after all drawing completes, in a fixed KSOP order that is deliberately
    # NOT the draw order (see this module's own LEGEND docstring section). One
    # entry per feature regardless of how many zones/branches it drew; a feature
    # that drew nothing contributes none.
    drew_keypoints = False
    drew_exclusion = False
    drew_production = False
    drew_water = False
    drew_road = False
    drew_tree = False
    drew_structure = False
    drew_fencing = False

    if not halo_mask.is_empty:
        plot_polygon(halo_mask, ax=ax, add_points=False, facecolor=HALO_COLOR, edgecolor="none", alpha=HALO_ALPHA, zorder=10)

    # EXCLUSION ZONES -- the map's ground layer, drawn first among the KSOP
    # layers and beneath all of them (EXCLUSION_ZONE_ZORDER; see that
    # constant for the full ordering and why it sits just above the halo).
    #
    # render_fill_polygon_utm here is the CLOSED union already clipped to the
    # parcel boundary, and unlike every other layer's render_fill it is the
    # same geometry as its polygon_utm equivalent -- a closing is extensive
    # and there is no display-only opening to apply (see exclusion_zones.py).
    # No smoothing either: the production fill is smoothed because contour
    # clipping against a 5 m staircase shows, but this draws as flat fill
    # with no edge stroke, where the staircase is invisible at map scale.
    exclusion_fill_utm = exclusion_result.get("render_fill_polygon_utm")
    if exclusion_fill_utm is not None and not exclusion_fill_utm.is_empty:
        exclusion_fill = _reproject_utm_geometry_to_mercator(exclusion_fill_utm, dem["crs"])
        for part in (
            exclusion_fill.geoms if exclusion_fill.geom_type == "MultiPolygon" else [exclusion_fill]
        ):
            if part.is_empty:
                continue
            plot_polygon(
                part,
                ax=ax,
                add_points=False,
                facecolor=EXCLUSION_ZONE_COLOR,
                edgecolor="none",
                alpha=EXCLUSION_ZONE_ALPHA,
                zorder=EXCLUSION_ZONE_ZORDER,
            )
            drew_exclusion = True

    for stream in water_features.get("streams", []):
        if not stream.get("geometry"):
            continue
        stream_geom = _reproject_geometry_to_mercator(stream["geometry"])
        if stream_geom.geom_type == "LineString":
            plot_line(stream_geom, ax=ax, add_points=False, color=STREAM_COLOR, linewidth=1.0, alpha=0.8, zorder=20)
        elif stream_geom.geom_type == "MultiLineString":
            for line in stream_geom.geoms:
                plot_line(line, ax=ax, add_points=False, color=STREAM_COLOR, linewidth=1.0, alpha=0.8, zorder=20)

    # Boundary fencing (fencing.identify_fencing()'s own "perimeter_fencing" layer,
    # fence_type="boundary") replaces this module's former plain boundary stroke
    # outright -- no fallback to the old solid line, since this layer always returns
    # at least the plain-boundary-equivalent loop (see fencing.find_boundary_fencing()'s
    # own docstring for the bare-property case). DISPLAY-ONLY angular simplify only --
    # pure _angular_simplify_closed_ring(), same treatment as every other fence type at
    # this point in the session (the earlier boundary-specific post-angular Chaikin
    # softening pass has been removed). fencing_result['fencing_geojson'] (used for the
    # narrative report) is untouched. Each simplified boundary ring is collected so the
    # water/tree zone fences below can be trimmed where they coincide with it. The
    # boundary ring itself is drawn AS-IS and is never trimmed against anything -- not
    # against a zone ring, not against another boundary ring: it is only ever an input to
    # the zone rings' trim mask below, never a target of one. Every fence type (boundary,
    # water_zone_exclusion, tree_zone_exclusion) collapses into ONE "Fencing" legend
    # entry -- drew_fencing flips true as soon as any fence line is drawn, regardless of
    # segment count.
    fencing_features = fencing_result["fencing_geojson"]["features"]
    boundary_fence_features = [f for f in fencing_features if f["properties"].get("fence_type") == "boundary"]
    boundary_fence_render_rings = []
    for feature in boundary_fence_features:
        fence_geom = _reproject_geometry_to_mercator(feature["geometry"])
        render_ring = _angular_simplify_closed_ring(fence_geom, FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M)
        boundary_fence_render_rings.append(render_ring)
        _draw_boundary_fence(ax, render_ring)
        drew_fencing = True

    # Everything-else fencing (fencing.identify_fencing()'s own "water_zone_exclusion" /
    # "tree_zone_exclusion" fence_types, same "perimeter_fencing" layer -- no road fence
    # loop exists anymore, see fencing.py's own module docstring) -- both are fully
    # enclosed closed loops (per their own spec), so the same drawing helper as boundary
    # fencing applies, at EXCLUSION_FENCE_ZORDER rather than boundary fencing's own
    # FENCE_ZORDER (see that constant's own comment for why they don't share a zorder)
    # -- see this module's own WATER/TREE EXCLUSION FENCE STYLE docstring section. Both
    # stay purely angular (_angular_simplify_closed_ring(), same as the boundary fence
    # above). Looped over generically rather than as two near-duplicate blocks; only the
    # legend label differs per fence_type. INDEPENDENT of the boundary fence and of each
    # other in the DATA -- an overlap between either of them and the boundary fence is
    # expected there, not a bug; the mutual trim below is purely about what gets DRAWN.
    # Each simplified ring is (a) trimmed by the buffered union of the boundary fence
    # ring(s) AND every OTHER zone ring (see the two-pass structure below), dropping the
    # stretch it shares with a neighbour, then (b) clipped to boundary_polygon (render-only
    # -- see this module's own WATER/TREE EXCLUSION FENCE STYLE docstring section for why),
    # both AFTER simplifying: the simplify helper re-closes a genuinely closed ring, which
    # would be WRONG on an already-open arc piece (it would force-close a real arc into a
    # bogus loop), so the whole ring is simplified first, while still guaranteed closed,
    # and only THEN differenced/clipped into however many open/closed pieces result. Either
    # op can split one ring into several line pieces, all drawn -- but they contribute to
    # the SAME single collapsed "Fencing" legend entry as the boundary fence above,
    # regardless of fence_type or piece count.
    extra_fence_features = [
        f for f in fencing_features if f["properties"].get("fence_type") in ("water_zone_exclusion", "tree_zone_exclusion")
    ]

    # PASS 1 -- simplify every zone ring FIRST, before any of them is trimmed, so pass 2
    # can compare each ring against the others' ORIGINAL (pre-trim) geometry. Deliberately
    # two passes rather than trimming inline: the mutual trim is SYMMETRIC and has no
    # ordering/priority/rank dependency at all, so a trimmed result must never be fed into
    # a later zone's comparison (that would make zone A's drawn line depend on where zone B
    # happened to sit in this list). What's compared is exactly what's about to be drawn --
    # each ring is already angular-simplified here.
    zone_fence_render_rings = [
        _angular_simplify_closed_ring(
            _reproject_geometry_to_mercator(feature["geometry"]), FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M
        )
        for feature in extra_fence_features
    ]

    # PASS 2 -- trim, clip and draw each zone ring on its own. For zone i the trim mask is
    # the buffered union of the boundary fence ring(s) plus every OTHER zone ring (water +
    # every other tree zone), all taken from pass 1's untouched list. Where two zones run
    # within ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M of each other BOTH lose that
    # stretch, so the pair renders as two separate line pieces with a real gap between them
    # -- that gap is the intended result, not a defect (see that constant's own comment).
    # Each zone's own trimmed ring is drawn SEPARATELY -- never merged/unioned with a
    # neighbour's into one continuous outline -- so every zone stays its own rendered
    # feature and keeps its own legend entry below. A zone with no near neighbour and no
    # near boundary stretch loses nothing and still renders its full loop. Render-only
    # throughout: fencing_geojson keeps every zone's full, untrimmed ring exactly as
    # find_water_zone_fencing()/find_tree_zone_fencing() computed it.
    for index, (feature, render_ring) in enumerate(zip(extra_fence_features, zone_fence_render_rings)):
        other_rings = boundary_fence_render_rings + [
            ring for other_index, ring in enumerate(zone_fence_render_rings) if other_index != index
        ]
        # (a) trim every stretch near the boundary fence or near another zone's ring
        if other_rings:
            other_rings_union = unary_union(other_rings)
            trimmed_ring = render_ring.difference(
                other_rings_union.buffer(ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M)
            )
        else:
            trimmed_ring = render_ring
        # (b) clip to the drawn property boundary, same render-only reason as before
        clipped_ring = trimmed_ring.intersection(boundary_polygon)
        for line in _iter_line_parts(clipped_ring):
            if line.length > EXCLUSION_FENCE_CLIP_MIN_LENGTH:
                _draw_boundary_fence(ax, line, zorder=EXCLUSION_FENCE_ZORDER)
                drew_fencing = True

    # Production zones render as CONTOUR-LINE TEXTURE only -- no fill, no boundary
    # stroke, no on-map label (there is no numbered marker anymore; several
    # production zones collapse into one "Production Areas" legend entry with no
    # per-zone identity on the map, intentionally -- see this module's own
    # docstring). Every production patch that draws contributes to the single
    # drew_production flag regardless of rank or count.
    for patch in scored_patches:
        # The contour lines below clip against render_fill_polygon_utm (same CRS
        # as contour_lines' lines_utm -- no reprojection needed before
        # intersecting). render_fill_polygon_utm is a bounded morphological
        # opening of the cluster's cell mask: it severs a waist-split pinch (so a
        # reclaimed waist renders as a blank strip) and, being anti-extensive,
        # leaves an excluded interior (steep/hydric) pocket OPEN rather than
        # closing over it.

        # DISPLAY-ONLY: smooth the production fill's 5m cell-union staircase into
        # a clean curve for contour clipping, then re-clip to the stored
        # polygon_utm so the smoothed mask can never cover ground the cell gate
        # excluded (Chaikin can push outward at reflex vertices; the lead erode
        # leaves the fill a full cell inside polygon_utm, so a light smooth stays
        # within that slack -- but clip anyway to keep the invariant hard).
        # Computed ONCE per patch, not per contour line. The stored
        # render_fill_polygon_utm is untouched.
        production_fill_clip = _smooth_production_fill_for_render(
            patch["render_fill_polygon_utm"],
            PRODUCTION_FILL_SIMPLIFY_TOLERANCE_CELLS * max(dem["resolution_meters"]),
            PRODUCTION_FILL_CHAIKIN_ITERATIONS,
        ).intersection(patch["polygon_utm"])

        for contour in contour_lines:
            clipped = contour["lines_utm"].intersection(production_fill_clip)
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

        drew_production = True

    if water_zone is not None:
        # DISPLAY-ONLY ripple geometry: render_fill_polygon_utm is the zone's
        # bounded render opening (see water_candidate_zones.find_candidate_
        # zones()'s own docstring), already in the DEM's own UTM CRS --
        # reprojected in one hop (_reproject_utm_geometry_to_mercator(), same
        # pattern the production-zone contour clipping above already uses),
        # never the real geometry_wgs84 used for scoring/eligibility/the
        # narrative report. The water zone renders as RIPPLE-LINE TEXTURE (a
        # family of horizontal sine waves clipped to this geometry) with NO
        # fill and NO perimeter stroke -- see this module's own WATER ZONE
        # STYLE docstring section. The ripples are generated in the SAME
        # Mercator space the geometry is drawn in, so they clip correctly.
        # Crossing over a production zone's own rendered contour texture is
        # expected and fine here -- that overlap is a display-only
        # coincidence, not a real siting conflict (the real, unsmoothed
        # geometries stay clear of each other via the production-zone
        # eligibility exclusion gate).
        render_fill_geom = _reproject_utm_geometry_to_mercator(water_zone["render_fill_polygon_utm"], dem["crs"])
        ripple_lines = _ripple_lines_for_polygon(
            render_fill_geom,
            WATER_RIPPLE_SPACING,
            WATER_RIPPLE_WAVELENGTH,
            WATER_RIPPLE_AMPLITUDE,
            WATER_RIPPLE_PHASE_STEP,
            WATER_RIPPLE_SAMPLES_PER_WAVELENGTH,
        )
        if not ripple_lines:
            # A zone too small for even one wave row to intersect: draw nothing
            # rather than falling back to the old fill. There is no marker to
            # convey a zone this small anymore -- a zone that draws no ripple at
            # all simply doesn't appear on the map (the "Water System Survey
            # Area" legend entry still lists, since the layer is present in the
            # data). Logged once.
            print(
                f"  Water zone {water_zone['id']}: render opening too small for any ripple "
                "line row to intersect -- no ripple texture drawn."
            )
        for line in ripple_lines:
            # No fill, no face colour, no boundary stroke -- ripple lines only,
            # at the water zone's existing zorder (41, above production
            # contours' zorder=40).
            plot_line(
                line,
                ax=ax,
                add_points=False,
                color=WATER_ZONE_COLOR,
                linewidth=WATER_RIPPLE_LINEWIDTH,
                alpha=WATER_RIPPLE_ALPHA,
                zorder=41,
            )
        drew_water = True

    # Every branch, trunk and spur(s) alike -- NOT just road_corridor_
    # features[0] -- drawn with IDENTICAL styling (see this module's own
    # ROAD CORRIDOR STYLE docstring section for why no branch is visually
    # distinguished on the map). Every branch collapses into ONE "Suggested
    # Road Corridor" legend entry, trunk and spur alike. Each branch's own
    # geometry is smoothed INDEPENDENTLY (_smooth_line_for_render() preserves a
    # line's own first/last coordinate exactly, which for a spur is its real
    # join point on the parent branch) -- road_corridor_features' real
    # geometry/properties (length_m, avg_grade_pct, everything the narrative
    # report uses) are completely untouched by this, only the copy handed to
    # the plotting calls is simplified/smoothed.
    for road_corridor in road_corridor_features:
        geom = _reproject_geometry_to_mercator(road_corridor["geometry"])
        render_geom = _smooth_line_for_render(geom)
        _draw_road_corridor(ax, render_geom)
        drew_road = True

    # Tree zone candidates: possibly several, ranked (same "possibly-
    # multiple" shape as production zones and the road network's own
    # branches above, not a single selection like water_zone/structure_
    # site) -- see this module's own TREE ZONE STYLE docstring section
    # for the hatch-only styling rationale. Fill geometry:
    # render_fill_polygon_utm is the patch's real cell-union footprint,
    # UNMODIFIED -- no hull, no opening, no smoothing (unlike production/water,
    # this layer draws thin arms and interior pockets verbatim; see
    # score_tree_search_space()'s own docstring) -- may be a MultiPolygon,
    # which the loop below handles the same way the water zone's own fill
    # does -- already in the DEM's
    # own UTM CRS -- reprojected in one hop
    # (_reproject_utm_geometry_to_mercator(), same pattern the water
    # zone's own fill above already uses), never the real geometry_wgs84
    # used for scoring/eligibility/the narrative report.
    # Every tree candidate collapses into ONE "Tree Crop Areas" legend entry
    # regardless of count -- no numbered marker, no per-zone identity on the map
    # (intentional -- see this module's own docstring).
    tree_zone_patches = tree_zone_result.get("patches", []) if tree_zone_result else []
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
        drew_tree = True

    # Keypoints: the inflection in each primary valley's long profile
    # (keypoint_detection.py). ASTERISK ONLY -- the keyline contour is
    # deliberately out of scope here, not drawn. A plain black asterisk per
    # keypoint (_draw_keypoint_marker()), IDENTICAL for on-parcel and off-parcel
    # (on_parcel/distance_outside_boundary_m stay on the dicts for the report,
    # but the map draws no distinction). Each geometry is a GeoJSON Point
    # (geometry_wgs84) reprojected to Mercator in one hop, the same reprojection
    # path every other point layer uses. Drawn after the tree-zone hatch so the
    # asterisks sit on top of the zone fills.
    #
    # Keypoints contribute exactly ONE "Keypoint Candidates" legend entry
    # regardless of how many exist -- and none when there are none. The asterisk
    # IS the on-map symbol; the legend's own keypoint handle mirrors it (the same
    # (6, 2, 0) marker tuple). This mirrors every other layer's one-entry-per-
    # feature treatment.
    for keypoint in keypoints:
        point = _reproject_geometry_to_mercator(keypoint["geometry_wgs84"])
        _draw_keypoint_marker(ax, point)
        drew_keypoints = True

    if structure_site is not None:
        # Real, scored footprint still drives placement -- only what gets
        # drawn at its representative_point() changes (see this module's
        # own STRUCTURE SITE STYLE docstring section): a single fixed-size
        # map-pin icon, not a filled polygon. This is NOT a numbered marker (it
        # never was); the barn/silo glyph inside the pin is also the legend's
        # own "Permanent Building Site" icon.
        geom = _reproject_geometry_to_mercator(structure_site["geometry"])
        anchor_point = geom.representative_point()
        pin = AnnotationBbox(
            structure_site_icon(),
            (anchor_point.x, anchor_point.y),
            frameon=False,
            box_alignment=(0.5, 0.0),
            zorder=43,
        )
        ax.add_artist(pin)
        drew_structure = True

    ax.set_title("Proposed Farm Layout", fontsize=16, fontweight="bold", pad=14)

    # Icon legend: assembled ONCE, here, after all drawing -- in the fixed KSOP
    # order below, which is deliberately NOT the draw order (fences -> production
    # -> water -> road -> tree -> keypoints -> structure; exclusion leads, being
    # the ground layer everything else sits on). One entry per feature
    # that drew anything; a feature that drew nothing contributes none, so an
    # empty map produces an empty legend and no frame (the same guard the old
    # plain-text box used). Each handle is a DRAWN symbol matching the map (see
    # _legend_handle_for()); no label carries any per-feature data.
    legend_features = [
        ("exclusion", drew_exclusion),
        ("keypoints", drew_keypoints),
        ("production", drew_production),
        ("water", drew_water),
        ("road", drew_road),
        ("tree", drew_tree),
        ("structure", drew_structure),
        ("fencing", drew_fencing),
    ]
    legend_pairs = [_legend_handle_for(name) for name, present in legend_features if present]
    if legend_pairs:
        handles = [handle for handle, _label in legend_pairs]
        labels = [label for _handle, label in legend_pairs]
        # Map the old ax.text box's appearance onto ax.legend(): lower-left at
        # axes (0.02, 0.02), fontsize 8.5, white facecolor, #333333 edge,
        # framealpha 0.9, borderpad 0.6. Same zorder as before (60) so it stays
        # above the boundary fence at FENCE_ZORDER (drawn last, it wins the tie).
        legend = ax.legend(
            handles,
            labels,
            handler_map=_LEGEND_HANDLER_MAP,
            loc="lower left",
            bbox_to_anchor=(0.02, 0.02),
            fontsize=8.5,
            facecolor="white",
            edgecolor="#333333",
            framealpha=0.9,
            borderpad=0.6,
        )
        legend.set_zorder(60)
        legend.get_frame().set_boxstyle("round,pad=0.6")  # rounded corners, matching the old box

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
