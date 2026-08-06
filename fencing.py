"""
fencing.py

Subdivision Fences data layer (Scale of Permanence step 7). Four fencing
types get real computed geometry here:

  STREAM EXCLUSION FENCING -- streams (USGS NHD, hydrology_data.py) are
  real line geometry today, not a candidate/planning zone, so it's safe
  to buffer them directly. Each stream's centerline is buffered by a
  fixed livestock-exclusion distance, and the BOUNDARY of that buffer
  (the outline, not the filled area) is output as the fence-line
  geometry -- a fence is a line, not an area, so this deliberately
  outputs a LineString/MultiLineString, not the buffered polygon itself.

  BOUNDARY FENCING -- the property boundary is already known (the user
  drew it), but real existing tree canopy (USGS 3DEP lidar HAG, same
  source production_area.py's own woody-vegetation gate uses) that
  overlaps the boundary edge itself is a real, already-sited feature --
  not a candidate/planning zone -- so it's safe to route the fence
  INWARD around it, the same "real, already-sited feature" reasoning
  that justifies STREAM EXCLUSION FENCING's own buffering above. Where
  canopy never touches the boundary edge (it sits elsewhere on the
  property), the boundary line is unaffected. No fence type/height/
  material guidance, which is explicitly out of scope (see report_
  generator.py's step 7 framing).

  ROAD CORRIDOR EXCLUSION FENCING -- road_corridors.py's own single
  generated/selected road corridor (find_road_routes()'s real, walk-
  ordered path cells, not a rounded continuous-geometry route) is real,
  already-computed geometry, the same "real, already-sited feature"
  standing STREAM EXCLUSION FENCING's own buffering above relies on -- so
  it's safe to fence it in directly. Mirrors tree_zone_candidates.py's
  own _road_corridor_exclusion_polygon() construction exactly (see
  ROAD_CORRIDOR_FENCE_DILATION_CELLS below): the corridor's own path
  cells are dilated by a whole DEM cell in every direction (raster_grid.
  binary_dilate(), 8-connected) into a real per-cell-square union
  footprint (raster_grid.cell_union_footprint()), then inset a short,
  fixed distance (ROAD_FENCE_LINE_INSET_METERS) so the drawn fence line
  reads tight to the road rather than sitting a full clearance-cell out.
  Fully INDEPENDENT of the boundary fence -- deliberately NOT spliced or
  gated into it; the two loops are allowed to overlap where the corridor
  meets the boundary edge (e.g. near the corridor's own anchor point),
  which is expected, not a bug -- a gate/access point is implied there
  but not computed by this layer. No corridor at all (no anchor point
  given to road_corridors.py yet, the common not-yet-wired-up case)
  produces zero features here, not an error.

  EXISTING FARM ROAD EXCLUSION FENCING -- real, already-mapped public
  road geometry that falls on-parcel (farm_roads_data.
  get_farm_roads_for_boundary(), USGS National Map Transportation data --
  the SAME real, already-clean vector-line data get_road_exclusion_
  union_utm() in that module already buffers directly rather than
  rasterizing, for the same reason) is a second real, already-sited
  feature, buffered by a fixed distance (EXISTING_FARM_ROAD_FENCE_BUFFER_
  METERS) with the BOUNDARY of that buffer output as the fence line --
  same "buffer a real line feature, output the outline" recipe STREAM
  EXCLUSION FENCING's own buffering above already uses, not the cell-
  dilation approach ROAD CORRIDOR EXCLUSION FENCING above uses, since
  this is already-clean vector data, not DEM-derived. Only the on-parcel
  portion of each mapped road matters here -- each road's geometry is
  clipped to the boundary first, so a road that merely fronts the
  property without crossing into it produces nothing. Also fully
  INDEPENDENT of the boundary fence, same overlap-is-expected reasoning
  as above.

Everything else this step covers -- pond/water zone exclusion fencing,
tree crop/windbreak exclusion fencing, and subdivision/rotational
fencing -- is deliberately NOT computed here, and this module doesn't
attempt it. Those all key off CANDIDATE/planning geometry (a water
system candidate zone from water_candidate_zones.py, a proposed
windbreak, a production zone) rather than a real, already-sited feature
footprint. Buffering a candidate zone and presenting the result as fence
geometry would draw a specific-looking fence line around ground that
isn't a confirmed feature yet -- exactly the kind of misleading
precision this pipeline's confidence_notes convention exists to prevent
(see feature_schema.py). Those get narrative-only treatment instead,
reasoned by Claude at report time from structured context already
computed by earlier steps (water candidate zones, production zones,
valley/ridge delineation, road corridors, building placement) -- same
pattern report_generator.py already uses for tradeoff narration
elsewhere in the report. See report_generator.py's step 7 system-prompt
guidance for exactly how that narrative is framed. This also does NOT
walk around any OTHER real, already-sited feature (streams, roads, water
zones, production zones) even where those cross the boundary -- canopy
is the one exception, not a precedent for routing the boundary fence
around everything else too.

find_stream_exclusion_fencing(), find_boundary_fencing(), find_road_
corridor_fencing(), and find_existing_farm_road_fencing() are all pure
geometric cores: no network I/O, taking already-fetched geometry (stream
features + a target UTM CRS; a boundary polygon + an already-fetched/
already-buffered canopy footprint; a DEM + the selected road corridor's
own path cells; farm road features + a boundary polygon, respectively)
-- same "logic separable from fetching" split as water_candidate_zones.
find_candidate_zones() and every other *_candidates.py/*_corridors.py
module in this codebase. identify_boundary_fencing() is the fetch-and-
wrap entry point that feeds find_boundary_fencing() real canopy data
(production_area.get_required_tree_root_zone_mask_utm(), the SAME
mandatory canopy gate production_area.py/tree_zone_candidates.py already
use) and wraps the result in schema. identify_fencing() is the full
pipeline entry point that additionally feeds find_road_corridor_fencing()
the selected road corridor's own path cells (deriving them itself via
road_corridors.identify_road_corridor_candidates() if not supplied -- a
no-anchor-point-given corridor cleanly produces zero features, not an
error) and find_existing_farm_road_fencing() real on-parcel farm road
geometry (farm_roads_data.get_farm_roads_for_boundary(), degrading
gracefully on fetch failure -- this is NOT canopy, same independent-
degrade pattern every other non-canopy network fetch in this codebase
uses). All four fence types land on the "perimeter_fencing" layer (aside
from stream exclusion's own separate "exclusion_fencing" layer) -- kept
as one shared layer rather than split per fence type (water/tree-crop
exclusion fencing will land here too in later passes), distinguished by
each feature's own "fence_type" property, not by separate layers.
"""

from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, Polygon, mapping, shape

from dem_data import get_dem_for_boundary
from farm_roads_data import get_farm_roads_for_boundary
from feature_schema import CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, make_feature, make_feature_collection
from hydrology_data import get_water_features_geojson
from production_area import get_required_tree_root_zone_mask_utm
from raster_grid import SQUARE_METERS_PER_ACRE, binary_dilate, cell_union_footprint
from road_corridors import identify_road_corridor_candidates

# Buffer distance (meters) used to turn a stream's real NHD centerline
# into a livestock-exclusion fence-line recommendation: the fence runs
# along the outline of this buffer, keeping livestock off the streambank
# and out of the immediate riparian corridor. 10.7m (~35ft) is a
# commonly-cited MINIMUM livestock-exclusion setback in NRCS conservation
# practice guidance (e.g. livestock exclusion / riparian buffer
# standards) -- narrower buffers focused specifically on water-quality/
# habitat benefit commonly go wider (up to 30m/100ft or more). This is a
# rule-of-thumb minimum, not a site-specific regulatory or cost-share
# program setback -- stated plainly since this environment has no live
# network access to verify a specific citation. Deliberately a SEPARATE
# constant from road_corridors.POND_ZONE_EXCLUSION_BUFFER_METERS, which
# serves an unrelated purpose (keeping OTHER infrastructure off a
# candidate pond/dam zone), not livestock exclusion from a real stream.
# CONFIGURABLE -- tune against your own property/program requirements.
STREAM_EXCLUSION_BUFFER_METERS = 10.7

STREAM_EXCLUSION_CONFIDENCE_NOTES_TEMPLATE = (
    "This is a SUGGESTED livestock-exclusion boundary, not a surveyed fence "
    "line: it's generated by buffering the mapped USGS NHD stream centerline "
    "by a fixed {buffer_meters}m ({buffer_feet}ft) distance and outputting the "
    "outline of that buffer. It inherits NHD's own scale/currency limitations "
    "(see hydrology_data.py's confidence_notes) -- actual streambank position, "
    "channel meander, and small/seasonal drainages not captured in NHD may not "
    "match what's on the ground. Walk this line and adjust before building "
    "fence on it."
)

METERS_PER_FOOT = 0.3048

# How far a boundary fence line gets routed INWARD around real existing tree
# canopy that overlaps the boundary edge itself -- deliberately its OWN
# constant, NOT a reuse of production_area.TREE_ROOT_ZONE_BUFFER_METERS
# (10ft, canopy_height_data.TREE_ROOT_ZONE_BUFFER_METERS) or any other
# canopy buffer already in this codebase (e.g. water_candidate_zones.
# WATER_ZONE_CANOPY_BUFFER_METERS), even though a similar number would
# work for all of them -- same "constants stay separate even when
# numerically identical" convention this pipeline already applies
# elsewhere (see water_candidate_zones.py's own WATER_ZONE_CANOPY_BUFFER_
# METERS comment for the fullest statement of this), so a future retune
# of one buffer doesn't silently couple to another that happens to serve
# a different purpose (production eligibility vs water-zone eligibility
# vs, here, fence routing). 0ft: the fence line hugs the real canopy
# footprint directly, no extra working-clearance margin beyond it.
# CONFIGURABLE.
BOUNDARY_FENCE_CANOPY_BUFFER_METERS = 0 * METERS_PER_FOOT  # 0m

# Area floor below which a fence-loop fragment produced by find_boundary_
# fencing() (e.g. a sliver where canopy just grazes a boundary corner) is
# dropped rather than returned as a spurious extra segment -- its own
# SEPARATE constant from production_area.MIN_PRODUCTION_AREA_ACRES, even
# though both are "drop tiny slivers" floors: one gates a production
# PATCH (real farmable ground), this one gates a FENCE-LOOP FRAGMENT (a
# closed line, not an area to farm) -- different kinds of sliver, same
# "constants stay separate" convention noted above. CONFIGURABLE.
BOUNDARY_FENCE_MIN_SEGMENT_ACRES = 0.05

# Minimum width (meters) a strip of kept land between two canopy notches must
# clear to survive find_boundary_fencing()'s own morphological OPENING step
# (erode then dilate by this same radius -- see that function's own
# docstring) -- narrower than this, a "peninsula" of land between two close
# canopy notches gets snapped off into the excluded/canopy side entirely,
# rather than surviving as a real usable strip the fence line then traces
# pointless fine detail around. This is a DIFFERENT kind of sliver from
# BOUNDARY_FENCE_MIN_SEGMENT_ACRES above (a whole fence-loop FRAGMENT dropped
# by area) -- this one snaps off a narrow PROTRUSION within a single
# fence_polygon before ring extraction even happens, same "constants stay
# separate even when both about slivers" convention this module already
# applies elsewhere. ~3ft. CONFIGURABLE -- tune by eye against the reference
# property.
BOUNDARY_FENCE_MIN_SLIVER_WIDTH_METERS = 1.0

BOUNDARY_FENCE_CONFIDENCE_NOTES_TEMPLATE = (
    "This fence line follows the drawn property boundary, inset inward around real "
    "existing tree canopy (USGS 3DEP lidar HAG, buffered {buffer_meters}m/{buffer_feet}ft) "
    "wherever canopy overlaps the boundary edge itself -- canopy elsewhere on the property "
    "does not affect this line. It does NOT walk around any other feature (streams, roads, "
    "water zones, production zones) even where those cross the boundary.{split_note} It "
    "carries no fence type, height, or material specification (out of scope), and no legal "
    "parcel/ownership-boundary or survey data backs it -- confirm the exact line against a "
    "real survey before building on it."
)

BOUNDARY_FENCE_SPLIT_NOTE_TEMPLATE = (
    " Canopy running end-to-end across the boundary split the perimeter into {segment_count} "
    "separate fence loops requiring {segment_count} separate physical fence runs, not one "
    "continuous loop."
)

# Whole-cell dilation radius applied to the selected road corridor's own
# path cells (road_corridors.find_road_routes()'s own 'cells') before
# building the real cell-footprint polygon this fence line encloses --
# the SAME whole-cell dilation radius as tree_zone_candidates.
# TREE_ZONE_ROAD_BUFFER_CELLS, reused here for the SAME underlying reason
# (see that constant's own comment): without it, two grid-aligned squares
# that only share a CORNER, not an edge, leave zero real gap between
# them -- a real, cell-accurate clearance envelope around the route's own
# path cells, not a rounded continuous-geometry buffer. Deliberately its
# OWN constant here, NOT imported from tree_zone_candidates.py -- the two
# modules use this same starting value for DIFFERENT downstream purposes
# (an exclusion-zone search space there, a fence line's own clearance
# envelope here) and should stay independently tunable even though they
# start at the same number, same "constants stay separate even when
# numerically identical" convention BOUNDARY_FENCE_CANOPY_BUFFER_METERS's
# own comment already states for this module. CONFIGURABLE.
ROAD_CORRIDOR_FENCE_DILATION_CELLS = 1

# Pulls the actual fence LINE inward from the dilated footprint's outer
# edge (see ROAD_CORRIDOR_FENCE_DILATION_CELLS above), so the fence reads
# tight to the road rather than sitting a full clearance-cell out.
# Deliberately SEPARATE from the dilation above: the dilation establishes
# the real clearance envelope (how much ground is actually kept clear
# around the road), this inset only affects where the drawn line sits
# WITHIN that already-established envelope -- two different concerns,
# not one number doing double duty. Was 0.3048m (1ft); widened to 2.0m
# after eyeballing against the reference property. CONFIGURABLE -- tune
# by eye against the reference property.
ROAD_FENCE_LINE_INSET_METERS = 2.0

# Buffer (meters) around real, already-clean vector road geometry
# (farm_roads_data.get_farm_roads_for_boundary(), USGS National Map
# Transportation data) that falls on-parcel -- a real distance, not a
# whole-cell dilation, same "don't rasterize already-clean vector data"
# reasoning farm_roads_data.get_road_exclusion_union_utm()'s own
# docstring already gives for why IT buffers this same data directly
# rather than going through a raster-dilation step. Deliberately its OWN
# constant, NOT reused from STREAM_EXCLUSION_BUFFER_METERS (a livestock-
# exclusion setback, not a fence-line buffer around a road) or
# farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS (a hard production-zone
# cell exclusion, not a fence line) -- a similar number might work for
# all three, but each serves a genuinely different purpose, same
# "constants stay separate even when numerically identical" convention
# this module already applies elsewhere (see BOUNDARY_FENCE_CANOPY_
# BUFFER_METERS's own comment). Placeholder value -- CONFIGURABLE, tune
# against the reference property.
EXISTING_FARM_ROAD_FENCE_BUFFER_METERS = 3.0


def _utm_epsg_for_lonlat(longitude: float, latitude: float) -> int:
    """
    Same formula as dem_data.py's private helper of the same name,
    duplicated here rather than imported: stream-exclusion buffering
    needs a projected meters-based CRS the same way DEM analysis does,
    but doesn't need (and shouldn't require) fetching an entire DEM
    raster just to get one EPSG code.
    """
    zone = int((longitude + 180) // 6) + 1
    return (32600 if latitude >= 0 else 32700) + zone


def _utm_crs_for_boundary(boundary_coordinates: list[tuple[float, float]]) -> str:
    lons = [pt[0] for pt in boundary_coordinates]
    lats = [pt[1] for pt in boundary_coordinates]
    center_lon = (min(lons) + max(lons)) / 2
    center_lat = (min(lats) + max(lats)) / 2
    return f"EPSG:{_utm_epsg_for_lonlat(center_lon, center_lat)}"


def find_stream_exclusion_fencing(
    stream_features: list[dict],
    utm_crs: str,
    buffer_meters: float = STREAM_EXCLUSION_BUFFER_METERS,
) -> list[dict]:
    """
    Pure geometric core (Step 1) -- no network I/O. Takes already-fetched
    NHD stream features (hydrology_data.get_water_features_geojson()'s
    "hydrology-streams" features -- real WGS84 line geometry, schema-
    wrapped) and a target UTM CRS, buffers each stream by buffer_meters,
    and returns the BOUNDARY of that buffer (a line, not a filled area)
    as fence-line geometry.

    Features with missing/empty geometry are skipped, not raised on --
    same tolerance-for-partial-data reasoning the rest of this pipeline
    uses (see e.g. road_corridors.py's per-source try/except fetches).

    Returns one entry per usable stream feature:
        {
            'source_feature_id': str,   # the input feature's own schema id
            'source_label': str,
            'geometry_wgs84': GeoJSON LineString/MultiLineString,
        }
    """
    results = []
    for feature in stream_features:
        geometry = feature.get("geometry")
        if not geometry or not geometry.get("coordinates"):
            continue

        geometry_utm = shape(transform_geom("EPSG:4326", utm_crs, geometry))
        buffered_polygon_utm = geometry_utm.buffer(buffer_meters)
        if buffered_polygon_utm.is_empty:
            continue

        fence_line_utm = buffered_polygon_utm.boundary
        geometry_wgs84 = transform_geom(utm_crs, "EPSG:4326", mapping(fence_line_utm))

        results.append(
            {
                "source_feature_id": feature["id"],
                "source_label": feature["properties"].get("label") or "Unnamed stream",
                "geometry_wgs84": geometry_wgs84,
            }
        )

    return results


def stream_exclusion_fencing_to_geojson(
    fencing_entries: list[dict], buffer_meters: float = STREAM_EXCLUSION_BUFFER_METERS
) -> dict:
    """Wraps find_stream_exclusion_fencing() output as schema-conformant
    Features on the "exclusion_fencing" layer."""
    confidence_notes = STREAM_EXCLUSION_CONFIDENCE_NOTES_TEMPLATE.format(
        buffer_meters=buffer_meters, buffer_feet=round(buffer_meters / METERS_PER_FOOT, 1)
    )

    features = [
        make_feature(
            feature_id=f"exclusion-fencing-stream-{entry['source_feature_id']}",
            geometry=entry["geometry_wgs84"],
            layer="exclusion_fencing",
            label=f"Stream exclusion fencing ({entry['source_label']})",
            confidence=CONFIDENCE_MEDIUM,
            confidence_notes=confidence_notes,
            extra_properties={
                "source_feature_id": entry["source_feature_id"],
                "exclusion_buffer_meters": buffer_meters,
            },
        )
        for entry in fencing_entries
    ]
    return make_feature_collection(features)


def find_boundary_fencing(
    boundary_polygon_utm: Polygon,
    canopy_union_utm: Optional[object],
    buffer_meters: float = BOUNDARY_FENCE_CANOPY_BUFFER_METERS,
) -> list[LineString]:
    """
    Pure geometric core -- no network I/O, no DEM access. Takes the
    property boundary and an already-fetched, already-buffered canopy
    footprint (canopy_union_utm -- a shapely (Multi)Polygon, or None/
    empty if there's no canopy on the property at all), both already in
    the same UTM CRS, and returns the boundary fence line(s) routed
    inward around any canopy that overlaps the boundary edge itself.
    buffer_meters is carried through only for documentation/parity with
    identify_boundary_fencing()'s own default -- the buffering itself
    already happened in canopy_union_utm before this function ever sees
    it (see identify_boundary_fencing()), so this function does no
    buffering of its own.

    No canopy at all (canopy_union_utm is None or empty): returns the
    boundary's own exterior ring, unmodified, as a single closed
    LineString -- the plain-wrap case, unchanged from this module's
    earlier behavior.

    Otherwise: fence_polygon = boundary_polygon_utm.difference(canopy_union_utm),
    then a standard morphological OPENING (erode by BOUNDARY_FENCE_MIN_
    SLIVER_WIDTH_METERS, then dilate back by that same radius) is applied
    to fence_polygon BEFORE ring extraction -- opened = fence_polygon.
    buffer(-BOUNDARY_FENCE_MIN_SLIVER_WIDTH_METERS).buffer(BOUNDARY_FENCE_
    MIN_SLIVER_WIDTH_METERS). Two close canopy notches can leave a narrow
    "peninsula" of kept land between them -- too narrow to be a real
    usable strip, and it makes the fence line trace pointless fine detail
    into and back out of it. The opening snaps off any such protrusion
    narrower than BOUNDARY_FENCE_MIN_SLIVER_WIDTH_METERS (merging it into
    the excluded/canopy side) while leaving genuinely large land areas
    untouched. This can itself occasionally merge what would have been
    two legitimate separate loops back into one, or vice versa, in
    unusual geometry -- expected, correct behavior of an opening
    operation, not a bug. Everything downstream (ring extraction, the
    MultiPolygon-split-into-multiple-loops handling, the min-segment-area
    filter below) operates on `opened`, not the raw difference result.

    Only EXTERIOR rings of the (opened) result are kept -- a Polygon
    result contributes its one exterior ring, a MultiPolygon result
    contributes one exterior ring per piece. Interior rings (holes) are
    discarded entirely: canopy that sits entirely inside the boundary,
    never touching the edge, carves a hole out of the polygon, not a
    change to the fence line itself, so it must be silently ignored
    rather than surfaced as a spurious third fence line.

    Each kept ring is returned as its own closed LineString (first coord
    == last coord), ordered by the ring's own enclosed area, LARGEST
    FIRST -- deterministic run to run given the same input, and this
    becomes the ordering identify_boundary_fencing() labels "Boundary
    Fencing 1 / 2" from. Ring fragments below BOUNDARY_FENCE_MIN_SEGMENT_
    ACRES (e.g. a sliver where canopy just grazes a boundary corner) are
    dropped rather than returned as a spurious extra segment.
    """
    if canopy_union_utm is None or canopy_union_utm.is_empty:
        return [LineString(boundary_polygon_utm.exterior.coords)]

    fence_polygon_utm = boundary_polygon_utm.difference(canopy_union_utm)
    if fence_polygon_utm.is_empty:
        return []

    opened_fence_polygon_utm = fence_polygon_utm.buffer(-BOUNDARY_FENCE_MIN_SLIVER_WIDTH_METERS).buffer(
        BOUNDARY_FENCE_MIN_SLIVER_WIDTH_METERS
    )
    if opened_fence_polygon_utm.is_empty:
        return []

    if opened_fence_polygon_utm.geom_type == "Polygon":
        candidate_polygons = [opened_fence_polygon_utm]
    elif opened_fence_polygon_utm.geom_type == "MultiPolygon":
        candidate_polygons = list(opened_fence_polygon_utm.geoms)
    else:
        # Defensive: a Polygon-minus-Polygon difference (opened by a
        # symmetric erode-then-dilate) shouldn't produce anything else,
        # but guard against a degenerate GeometryCollection (e.g. a
        # zero-area Point/LineString touch) by keeping only real Polygon
        # parts, same tolerance-for-topology-noise reasoning raster_grid.
        # cell_union_footprint()'s own buffer(0) cleanup uses.
        candidate_polygons = [
            g for g in getattr(opened_fence_polygon_utm, "geoms", []) if g.geom_type == "Polygon"
        ]

    rings_by_area = []
    for polygon in candidate_polygons:
        if polygon.is_empty:
            continue
        exterior_ring_polygon = Polygon(polygon.exterior)
        area_acres = exterior_ring_polygon.area / SQUARE_METERS_PER_ACRE
        if area_acres < BOUNDARY_FENCE_MIN_SEGMENT_ACRES:
            continue
        rings_by_area.append((exterior_ring_polygon.area, LineString(polygon.exterior.coords)))

    rings_by_area.sort(key=lambda pair: pair[0], reverse=True)
    return [ring for _, ring in rings_by_area]


def boundary_fencing_to_geojson(
    rings_wgs84: list, buffer_meters: float = BOUNDARY_FENCE_CANOPY_BUFFER_METERS
) -> dict:
    """
    Wraps find_boundary_fencing()'s output (already reprojected to WGS84
    by the caller -- see identify_boundary_fencing()) as schema-conformant
    Features on the "perimeter_fencing" layer -- kept as one shared layer
    for every future fence type (road/water/tree-crop/stream-exclusion
    fencing will all land here too), distinguished by each feature's own
    "fence_type" property rather than by separate layers.

    rings_wgs84 entries may be shapely LineString geometries or already-
    mapped GeoJSON geometry dicts -- either is accepted so callers can
    pass shapely objects directly without a redundant mapping() round
    trip. segment_index is only set (and the label/confidence_notes only
    mention a split) when more than one ring was returned -- a single
    ring is exactly today's plain-boundary case, not a "segment 1 of 1".
    """
    segment_count = len(rings_wgs84)
    split_note = (
        BOUNDARY_FENCE_SPLIT_NOTE_TEMPLATE.format(segment_count=segment_count) if segment_count > 1 else ""
    )
    confidence_notes = BOUNDARY_FENCE_CONFIDENCE_NOTES_TEMPLATE.format(
        buffer_meters=round(buffer_meters, 3),
        buffer_feet=round(buffer_meters / METERS_PER_FOOT, 1),
        split_note=split_note,
    )

    features = []
    for i, ring in enumerate(rings_wgs84, start=1):
        geometry_wgs84 = ring if isinstance(ring, dict) else mapping(ring)
        extra_properties = {
            "fence_type": "boundary",
            "canopy_buffer_meters": buffer_meters,
        }
        label = "Boundary fencing"
        if segment_count > 1:
            extra_properties["segment_index"] = i
            label = f"Boundary fencing {i}"

        features.append(
            make_feature(
                feature_id=f"perimeter-fencing-boundary-{i}",
                geometry=geometry_wgs84,
                layer="perimeter_fencing",
                label=label,
                confidence=CONFIDENCE_HIGH,
                confidence_notes=confidence_notes,
                extra_properties=extra_properties,
            )
        )
    return make_feature_collection(features)


ROAD_CORRIDOR_FENCE_CONFIDENCE_NOTES_TEMPLATE = (
    "This fence line encloses road_corridors.py's own single generated/selected road corridor, "
    "buffered {dilation_cells} DEM cell(s) in every direction for a real clearance envelope around "
    "the route's own path cells, then inset {line_inset_meters}m ({line_inset_feet}ft) inward for "
    "the drawn line itself. It is INDEPENDENT of the boundary fence -- the two loops are not "
    "spliced or gated together, and may visually overlap it near the corridor's own anchor point "
    "(a gate/access point is implied there but not computed by this layer); that overlap is "
    "expected, not a bug. It inherits road_corridors.py's own limitations: the route itself is a "
    "planning suggestion, not a surveyed or built road -- confirm the actual alignment on the "
    "ground before building fence on it."
)

EXISTING_FARM_ROAD_FENCE_CONFIDENCE_NOTES_TEMPLATE = (
    "This fence line encloses the on-parcel portion of a real, already-existing mapped road (USGS "
    "National Map Transportation data -- see farm_roads_data.py's own confidence_notes: private "
    "farm tracks, driveways, or internal access lanes not captured in that public dataset will not "
    "appear here either), buffered {buffer_meters}m ({buffer_feet}ft). It is INDEPENDENT of the "
    "boundary fence -- the two loops are not spliced or gated together, and may visually overlap "
    "it where the road crosses the boundary (a gate/access point is implied there but not "
    "computed by this layer); that overlap is expected, not a bug."
)


def find_road_corridor_fencing(
    dem: dict,
    selected_road_corridor_cells: Optional[list],
    dilation_cells: int = ROAD_CORRIDOR_FENCE_DILATION_CELLS,
    line_inset_meters: float = ROAD_FENCE_LINE_INSET_METERS,
) -> Optional[LineString]:
    """
    Pure geometric core -- no network I/O. Mirrors tree_zone_candidates.
    _road_corridor_exclusion_polygon() construction exactly (see
    ROAD_CORRIDOR_FENCE_DILATION_CELLS's own comment): builds a fresh
    boolean cell mask from selected_road_corridor_cells (road_corridors.
    find_road_routes()'s own real path cells, in walk order), dilates it
    by dilation_cells in every direction (raster_grid.binary_dilate(),
    8-connected), and turns the dilated mask into a real per-cell-square
    union polygon (raster_grid.cell_union_footprint()) -- the SAME real
    footprint every other cell-clustering consumer in this pipeline
    builds from a boolean mask, not a hull or a smoothed buffer.

    selected_road_corridor_cells is None or empty whenever no corridor
    was generated/selected at all -- the common "no anchor point given"
    case road_corridors.fetch_and_select_optimal_road_corridor() (and
    identify_road_corridor_candidates()) already return None for. Returns
    None immediately in that case -- not an error, and not an empty
    LineString.

    The fence LINE itself is then inset line_inset_meters inward from
    that dilated footprint's own outer edge (fence_polygon =
    dilated_footprint.buffer(-line_inset_meters)) so it reads tight to
    the road rather than sitting a full clearance-cell out. On a narrow/
    pinched stretch of a snaking corridor, that negative buffer can come
    back empty or invalid -- a real risk, not a hypothetical -- in which
    case this falls back to the dilated footprint's own outer edge
    unbuffered (fence_polygon = dilated_footprint) rather than silently
    dropping the segment or returning broken geometry. That fallback is
    printed so it's visible if it ever actually triggers on a real
    property.

    Returns fence_polygon's exterior ring as a closed LineString (first
    coord == last coord).
    """
    if not selected_road_corridor_cells:
        return None

    road_cell_mask = np.zeros(dem["array"].shape, dtype=bool)
    for cell_r, cell_c in selected_road_corridor_cells:
        road_cell_mask[cell_r, cell_c] = True
    dilated_mask = binary_dilate(road_cell_mask, dilation_cells)
    dilated_footprint = cell_union_footprint(dem, dilated_mask)

    fence_polygon = dilated_footprint.buffer(-line_inset_meters)
    if fence_polygon.is_empty or not fence_polygon.is_valid or fence_polygon.geom_type != "Polygon":
        print(
            f"  find_road_corridor_fencing: {line_inset_meters}m inset produced an empty/invalid "
            "result on a narrow/pinched stretch of the corridor -- falling back to the dilated "
            "footprint's own outer edge (unbuffered) for the fence line."
        )
        fence_polygon = dilated_footprint

    return LineString(fence_polygon.exterior.coords)


def road_corridor_fencing_to_geojson(
    fence_line: Optional[LineString],
    dilation_cells: int = ROAD_CORRIDOR_FENCE_DILATION_CELLS,
    line_inset_meters: float = ROAD_FENCE_LINE_INSET_METERS,
) -> dict:
    """
    Wraps find_road_corridor_fencing()'s output (already reprojected to
    WGS84 by the caller -- see identify_fencing()) as a schema-conformant
    Feature on the "perimeter_fencing" layer (fence_type=
    "road_corridor_exclusion") -- the same shared layer boundary fencing
    uses, distinguished by fence_type rather than a separate layer.

    fence_line may be a shapely LineString or an already-mapped GeoJSON
    geometry dict. None (no corridor at all -- see find_road_corridor_
    fencing()'s own docstring) returns an empty FeatureCollection, not an
    error.
    """
    if fence_line is None:
        return make_feature_collection([])

    geometry_wgs84 = fence_line if isinstance(fence_line, dict) else mapping(fence_line)
    confidence_notes = ROAD_CORRIDOR_FENCE_CONFIDENCE_NOTES_TEMPLATE.format(
        dilation_cells=dilation_cells,
        line_inset_meters=round(line_inset_meters, 3),
        line_inset_feet=round(line_inset_meters / METERS_PER_FOOT, 1),
    )
    feature = make_feature(
        feature_id="perimeter-fencing-road-corridor",
        geometry=geometry_wgs84,
        layer="perimeter_fencing",
        label="Road corridor fencing",
        confidence=CONFIDENCE_HIGH,
        confidence_notes=confidence_notes,
        extra_properties={
            "fence_type": "road_corridor_exclusion",
            "dilation_cells": dilation_cells,
            "line_inset_meters": line_inset_meters,
        },
    )
    return make_feature_collection([feature])


def find_existing_farm_road_fencing(
    farm_road_features: list[dict],
    boundary_polygon_utm: Polygon,
    utm_crs: str,
    buffer_meters: float = EXISTING_FARM_ROAD_FENCE_BUFFER_METERS,
) -> list[dict]:
    """
    Pure geometric core (mirrors find_stream_exclusion_fencing()'s own
    shape -- see that function's docstring) -- no network I/O. Takes
    already-fetched farm road features (farm_roads_data.
    get_farm_roads_for_boundary()'s own {'name', 'geometry'} shape, real
    WGS84 LineString/MultiLineString geometry) and the property boundary
    (already in utm_crs), and returns a fence line around the ON-PARCEL
    portion of each road.

    Each road's geometry is reprojected into utm_crs, then CLIPPED to
    boundary_polygon_utm.intersection(...) FIRST -- only the on-parcel
    portion matters; a road that merely fronts the boundary without ever
    crossing into it clips to empty (or a zero-length touch) and
    correctly produces nothing. The clipped on-parcel portion is then
    buffered by buffer_meters, and the BOUNDARY of that buffer (the
    outline, not the filled area) is returned as fence-line geometry --
    same "buffer a real line feature, output the outline" recipe find_
    stream_exclusion_fencing() already uses, not the dilation/inset
    approach find_road_corridor_fencing() above uses, since this is
    already-clean vector data, not DEM-derived.

    Returns one entry per usable on-parcel road segment:
        {
            'source_feature_id': str,
            'source_label': str,
            'geometry_wgs84': GeoJSON LineString/MultiLineString,
        }
    """
    results = []
    for i, feature in enumerate(farm_road_features):
        geometry = feature.get("geometry")
        if not geometry or not geometry.get("coordinates"):
            continue

        geometry_utm = shape(transform_geom("EPSG:4326", utm_crs, geometry))
        on_parcel_geometry_utm = boundary_polygon_utm.intersection(geometry_utm)
        if on_parcel_geometry_utm.is_empty or on_parcel_geometry_utm.length == 0:
            continue

        buffered_polygon_utm = on_parcel_geometry_utm.buffer(buffer_meters)
        if buffered_polygon_utm.is_empty:
            continue

        fence_line_utm = buffered_polygon_utm.boundary
        geometry_wgs84 = transform_geom(utm_crs, "EPSG:4326", mapping(fence_line_utm))

        results.append(
            {
                "source_feature_id": f"farm-road-{i}",
                "source_label": feature.get("name") or "Unnamed road",
                "geometry_wgs84": geometry_wgs84,
            }
        )

    return results


def existing_farm_road_fencing_to_geojson(
    fencing_entries: list[dict], buffer_meters: float = EXISTING_FARM_ROAD_FENCE_BUFFER_METERS
) -> dict:
    """Wraps find_existing_farm_road_fencing() output as schema-conformant
    Features on the "perimeter_fencing" layer (fence_type=
    "existing_farm_road_exclusion") -- same shared layer/distinguish-by-
    fence_type convention as road_corridor_fencing_to_geojson()/
    boundary_fencing_to_geojson()."""
    confidence_notes = EXISTING_FARM_ROAD_FENCE_CONFIDENCE_NOTES_TEMPLATE.format(
        buffer_meters=round(buffer_meters, 3), buffer_feet=round(buffer_meters / METERS_PER_FOOT, 1)
    )

    features = [
        make_feature(
            feature_id=f"perimeter-fencing-existing-farm-road-{i}",
            geometry=entry["geometry_wgs84"],
            layer="perimeter_fencing",
            label=f"Existing farm road fencing ({entry['source_label']})",
            confidence=CONFIDENCE_MEDIUM,
            confidence_notes=confidence_notes,
            extra_properties={
                "fence_type": "existing_farm_road_exclusion",
                "source_feature_id": entry["source_feature_id"],
                "buffer_meters": buffer_meters,
            },
        )
        for i, entry in enumerate(fencing_entries, start=1)
    ]
    return make_feature_collection(features)


def identify_boundary_fencing(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
) -> dict:
    """
    Fetch-and-wrap entry point for canopy-aware boundary fencing. Fetches
    dem if not supplied (get_dem_for_boundary(), same pattern every other
    module in this codebase uses), builds boundary_polygon_utm from
    boundary_coordinates via dem["crs"] (same warp_transform pattern
    road_corridors.identify_road_corridor_candidates() already uses), and
    fetches a REQUIRED tree-root-zone mask (production_area.
    get_required_tree_root_zone_mask_utm(), at this module's OWN
    BOUNDARY_FENCE_CANOPY_BUFFER_METERS, not production_area's own
    default) -- the SAME mandatory canopy gate production_area.py/tree_
    zone_candidates.py already apply. A fetch failure (no HAG coverage
    for this property at all) is left to raise UNCAUGHT here, same hard-
    fail behavior as those callers -- "can't verify what canopy actually
    overlaps the boundary" is treated as a hard failure, not a lower-
    confidence result to hand back with a caveat.

    The mask is converted to a real cell-union footprint polygon via
    raster_grid.cell_union_footprint() -- the SAME real per-cell-square
    union production_area_ceiling.py/tree_zone_candidates.py already use,
    NOT a convex hull, so the fence line notches around canopy's real,
    possibly-jagged shape rather than a smoothed approximation.

    find_boundary_fencing() then does the actual routing (pure geometry,
    see that function's own docstring), and each returned UTM ring is
    reprojected back to WGS84 before boundary_fencing_to_geojson() wraps
    it in schema.

    Returns:
        {
            'fencing_geojson': FeatureCollection,   # 1 or more "perimeter_fencing" features
            'segment_count': int,
        }
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    boundary_xs, boundary_ys = warp_transform(
        "EPSG:4326",
        dem["crs"],
        [pt[0] for pt in boundary_coordinates],
        [pt[1] for pt in boundary_coordinates],
    )
    boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    tree_root_zone_mask_utm = get_required_tree_root_zone_mask_utm(
        boundary_polygon_utm, dem, buffer_meters=BOUNDARY_FENCE_CANOPY_BUFFER_METERS
    )
    canopy_union_utm = cell_union_footprint(dem, tree_root_zone_mask_utm)

    fence_rings_utm = find_boundary_fencing(
        boundary_polygon_utm, canopy_union_utm, buffer_meters=BOUNDARY_FENCE_CANOPY_BUFFER_METERS
    )
    segment_count = len(fence_rings_utm)

    rings_wgs84 = [
        shape(transform_geom(dem["crs"], "EPSG:4326", mapping(ring))) for ring in fence_rings_utm
    ]
    fencing_geojson = boundary_fencing_to_geojson(rings_wgs84, buffer_meters=BOUNDARY_FENCE_CANOPY_BUFFER_METERS)

    return {"fencing_geojson": fencing_geojson, "segment_count": segment_count}


def identify_fencing(
    boundary_coordinates: list[tuple[float, float]],
    water_features_geojson: Optional[dict] = None,
    dem: Optional[dict] = None,
    stream_exclusion_buffer_meters: float = STREAM_EXCLUSION_BUFFER_METERS,
    selected_road_corridor_cells: Optional[list] = None,
    anchor_lon_lat: Optional[tuple[float, float]] = None,
    farm_road_features: Optional[list[dict]] = None,
    road_corridor_dilation_cells: int = ROAD_CORRIDOR_FENCE_DILATION_CELLS,
    road_fence_line_inset_meters: float = ROAD_FENCE_LINE_INSET_METERS,
    existing_farm_road_buffer_meters: float = EXISTING_FARM_ROAD_FENCE_BUFFER_METERS,
) -> dict:
    """
    Full pipeline entry point for Subdivision Fences' computed geometry
    (stream exclusion, boundary, road corridor exclusion, and existing
    farm road exclusion fencing -- see module docstring for why
    everything else in that report step is narrative-only, not generated
    here). The two road fence types are each fully independent, closed
    loops -- deliberately NOT spliced or gated into the boundary fence
    (see module docstring) -- so an overlap between either of them and
    the boundary fence, or between the two of them, is expected, not a
    bug.

    water_features_geojson is hydrology_data.get_water_features_geojson()'s
    output; fetched here if not already supplied (e.g. reused from a
    caller that already fetched it). Same independent-fetch pattern
    every other pipeline module in this codebase uses (each fetches what
    it needs rather than sharing state across pipeline stages) --
    generate_full_report.py's own hydrology fetch for the WATER SUPPLY
    section is untouched by this. dem, likewise, is fetched here if not
    already supplied (get_dem_for_boundary()) -- identify_boundary_
    fencing() now needs one for its own mandatory canopy fetch (see that
    function's own docstring), same optional-dem pattern every other
    entry point in this codebase already uses.

    selected_road_corridor_cells is road_corridors.find_road_routes()'s
    own real path cells for the single selected corridor. If not
    supplied, this derives them itself via road_corridors.
    identify_road_corridor_candidates(dem=dem, anchor_lon_lat=
    anchor_lon_lat) -- the SAME optional-derive-if-not-supplied pattern
    dem itself already uses above. anchor_lon_lat defaults to None, the
    common "not yet wired up to a real anchor point" case -- that alone
    is NOT an error: identify_road_corridor_candidates() already returns
    a clean selected_road_corridor=None for it (no network fetch even
    attempted), and find_road_corridor_fencing() already returns None
    cleanly for that, producing zero road-corridor-exclusion features,
    same as today's "no road routes" handling elsewhere in this
    pipeline.

    farm_road_features is farm_roads_data.get_farm_roads_for_boundary()'s
    own output. If not supplied, fetched here -- but unlike the
    boundary fence's own MANDATORY canopy gate, this is NOT canopy, so a
    fetch failure degrades gracefully (same independent-degrade pattern
    every other non-canopy network fetch in this codebase already uses):
    road-corridor-exclusion fencing still computes normally, and only
    the existing-farm-road fencing is omitted.

    Returns:
        {
            'fencing_geojson': FeatureCollection,   # "exclusion_fencing" (stream) + "perimeter_fencing" (boundary + road corridor + existing farm road) features
            'segment_count': int,                   # identify_boundary_fencing()'s own segment_count, passed through
        }
    """
    if water_features_geojson is None:
        water_features_geojson = get_water_features_geojson(boundary_coordinates)
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    stream_features = [
        f for f in water_features_geojson["features"] if f["properties"]["layer"] == "hydrology-streams"
    ]

    utm_crs = _utm_crs_for_boundary(boundary_coordinates)
    stream_entries = find_stream_exclusion_fencing(stream_features, utm_crs, stream_exclusion_buffer_meters)

    boundary_result = identify_boundary_fencing(boundary_coordinates, dem=dem)

    if selected_road_corridor_cells is None:
        road_corridor_candidates = identify_road_corridor_candidates(
            boundary_coordinates, dem=dem, anchor_lon_lat=anchor_lon_lat
        )
        selected_road_corridor = road_corridor_candidates["selected_road_corridor"]
        selected_road_corridor_cells = selected_road_corridor["cells"] if selected_road_corridor else None

    road_fence_line_utm = find_road_corridor_fencing(
        dem,
        selected_road_corridor_cells,
        dilation_cells=road_corridor_dilation_cells,
        line_inset_meters=road_fence_line_inset_meters,
    )
    road_fence_line_wgs84 = (
        shape(transform_geom(dem["crs"], "EPSG:4326", mapping(road_fence_line_utm)))
        if road_fence_line_utm is not None
        else None
    )
    road_corridor_geojson = road_corridor_fencing_to_geojson(
        road_fence_line_wgs84,
        dilation_cells=road_corridor_dilation_cells,
        line_inset_meters=road_fence_line_inset_meters,
    )

    if farm_road_features is None:
        try:
            farm_road_features = get_farm_roads_for_boundary(boundary_coordinates)
        except Exception as e:
            # Same fetch-degrades-independently reasoning every other
            # non-canopy network-backed layer in this pipeline already
            # uses -- an outage here shouldn't take down road-corridor-
            # exclusion fencing (already computed above) or the rest of
            # this pipeline's fencing output.
            print(
                f"  identify_fencing: farm road fetch failed ({e}), continuing without "
                "existing-farm-road fencing."
            )
            farm_road_features = []

    boundary_xs, boundary_ys = warp_transform(
        "EPSG:4326",
        dem["crs"],
        [pt[0] for pt in boundary_coordinates],
        [pt[1] for pt in boundary_coordinates],
    )
    boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    farm_road_entries = find_existing_farm_road_fencing(
        farm_road_features, boundary_polygon_utm, dem["crs"], buffer_meters=existing_farm_road_buffer_meters
    )
    farm_road_geojson = existing_farm_road_fencing_to_geojson(
        farm_road_entries, buffer_meters=existing_farm_road_buffer_meters
    )

    features = (
        stream_exclusion_fencing_to_geojson(stream_entries, stream_exclusion_buffer_meters)["features"]
        + boundary_result["fencing_geojson"]["features"]
        + road_corridor_geojson["features"]
        + farm_road_geojson["features"]
    )

    return {"fencing_geojson": make_feature_collection(features), "segment_count": boundary_result["segment_count"]}


def summarize_fencing(result: dict) -> str:
    features = result["fencing_geojson"]["features"]
    stream_count = sum(1 for f in features if f["properties"]["layer"] == "exclusion_fencing")
    boundary_features = [f for f in features if f["properties"].get("fence_type") == "boundary"]
    road_corridor_count = sum(1 for f in features if f["properties"].get("fence_type") == "road_corridor_exclusion")
    farm_road_count = sum(1 for f in features if f["properties"].get("fence_type") == "existing_farm_road_exclusion")
    segment_count = result.get("segment_count", len(boundary_features))

    lines = [
        f"Computed fencing features: {stream_count} stream exclusion, "
        f"{segment_count} boundary fencing segment(s), {road_corridor_count} road corridor exclusion, "
        f"{farm_road_count} existing farm road exclusion"
    ]
    for feature in features:
        props = feature["properties"]
        if props["layer"] == "exclusion_fencing":
            lines.append(f"  - {props['label']} ({props['exclusion_buffer_meters']}m buffer)")
        else:
            lines.append(f"  - {props['label']} (fence_type={props['fence_type']})")
    return "\n".join(lines)


if __name__ == "__main__":
    # Test case: the user's own drawn property boundary.
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]
    # Same real anchor point road_corridors.py's own __main__ block uses for
    # this reference property -- needed to actually exercise a real, selected
    # road corridor (and therefore find_road_corridor_fencing()) below; with
    # no anchor at all, road-corridor-exclusion fencing cleanly produces zero
    # features (see identify_fencing()'s own docstring).
    anchor_lon_lat = (-79.98356157031265, 40.64303511679458)

    print(
        "Identifying computed fencing (stream exclusion + canopy-aware boundary + road corridor "
        "exclusion + existing farm road exclusion) for property boundary...\n"
    )

    try:
        from feature_schema import validate_feature_collection

        dem = get_dem_for_boundary(property_boundary)

        boundary_only = identify_boundary_fencing(property_boundary, dem=dem)
        validate_feature_collection(boundary_only["fencing_geojson"])
        print(f"identify_boundary_fencing(): {boundary_only['segment_count']} segment(s), schema-valid.\n")

        result = identify_fencing(property_boundary, dem=dem, anchor_lon_lat=anchor_lon_lat)
        validate_feature_collection(result["fencing_geojson"])
        print(summarize_fencing(result))

        road_corridor_fenced = any(
            f["properties"].get("fence_type") == "road_corridor_exclusion" for f in result["fencing_geojson"]["features"]
        )
        farm_road_fenced = any(
            f["properties"].get("fence_type") == "existing_farm_road_exclusion" for f in result["fencing_geojson"]["features"]
        )
        print(f"\nRoad corridor fence produced: {road_corridor_fenced}")
        print(f"Existing farm road fence produced: {farm_road_fenced}")
        print("\nfencing_geojson is schema-valid.")
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map ImageServer/3DEP lidar HAG services — not a fully sandboxed environment."
        )
