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

  BOUNDARY FENCING -- the "boundary" fence protects the property's
  DEVELOPED FOOTPRINT (production zones + structure site + road corridor
  path), buffered by a working margin (BOUNDARY_FENCE_MARGIN_METERS) and
  clipped to the drawn property boundary, rather than following the full
  drawn boundary unconditionally -- so fencing is spent on the land the
  farm actually develops, not on unused ground out to the parcel edge.
  Each of those developed features is a real, already-sited/scored
  footprint (production_area_ceiling.py's ceiling-trimmed zones, solar_
  suitability.py's selected structure site, road_corridors.py's own
  undilated path footprint), the same "real, already-computed feature"
  reasoning that justifies STREAM EXCLUSION FENCING's own buffering above.
  Where a water/tree exclusion zone sits at the true outer edge of that
  developed footprint, the boundary fence's own path meets that zone's own
  already-buffered fence edge along that stretch -- a union operation (see
  find_boundary_fencing()), fed the SAME buffered polygon
  (_buffered_zone_polygon()) the zone's own fence is drawn from. After the
  union the developed footprint is reduced to its CONVEX HULL (so the fence
  never dips inward through the gap between two separated developed
  clusters) and then clipped to the drawn boundary. Tree canopy is NOT a
  factor -- a fence running through wooded ground is fine in practice, so
  the boundary fence no longer routes around canopy (earlier passes
  differenced canopy out; that, and the sliver-opening cleanup it required,
  are gone). On a genuinely bare property (no production, structure, or
  road corridor sited) this collapses cleanly back to the drawn-boundary-
  only behavior. No fence
  type/height/material guidance, which is explicitly out of scope (see
  report_generator.py's step 7 framing).

  WATER ZONE EXCLUSION FENCING -- water_candidate_zones.py's own SINGLE
  selected water system candidate (water_suitability.select_optimal_
  water_zone()'s rank-1 answer) already carries a real, already-computed
  fill footprint (render_fill_polygon_utm -- the SAME display-quality
  polygon render_layout_map.py already draws that zone's own fill from),
  not raw DEM cells -- so this fences it in with the SAME "buffer a real
  feature, output its outline" recipe STREAM EXCLUSION FENCING above
  already uses. Buffered outward by a fixed distance (WATER_ZONE_FENCE_
  BUFFER_METERS) so the fence line sits clear of the zone's own edge, not
  flush against or clipping through it. Only ever one selected zone (or
  none) -- no ranking/multiple-loop handling needed here, unlike the
  tree zone layer below.

  TREE ZONE EXCLUSION FENCING -- tree_zone_candidates.py has NO selection
  step (unlike production/water/road, which each narrow down to one
  claimed candidate): identify_tree_zone_candidates()'s own 'patches' is
  the full ranked list, every one of which is a real, already-computed
  scored candidate with its own render_fill_polygon_utm. This fences EVERY
  candidate independently, same buffer-and-outline recipe as WATER ZONE
  EXCLUSION FENCING above (TREE_ZONE_FENCE_BUFFER_METERS, its own separate
  constant), one closed loop per candidate -- no merging of two candidates
  that happen to sit close together into a single combined loop, even if
  their buffers would overlap; that's out of scope for this pass.

Subdivision/rotational fencing is deliberately NOT computed here, and
this module doesn't attempt it -- it keys off a PLANNED grazing/rotation
scheme (how a producer intends to divide and rotate production ground),
not a real, already-sited or already-scored feature footprint the way
every fence type above does, even the candidate-stage water/tree zone
layers (a real, already-computed scored candidate is not the same thing
as a not-yet-decided planning scheme). That gets narrative-only treatment
instead, reasoned by Claude at report time from structured context
already computed by earlier steps (water candidate zones, production
zones, tree zone candidates, valley/ridge delineation, road corridors,
building placement) -- same pattern report_generator.py already uses for
tradeoff narration elsewhere in the report. See report_generator.py's
step 7 system-prompt guidance for exactly how that narrative is framed.
The water and tree zone EXCLUSION fences above stay fully independent
closed loops of their own regardless: where a zone sits inside the
developed footprint with real clearance, its fence is a separate nested
loop the boundary fence never touches; where a zone sits at the true
outer edge, the boundary fence's own path simply coincides with (uses)
that zone's already-buffered fence edge along that stretch and continues
-- a byproduct of the developed-footprint union, not a merge of the two
fence layers (the zone still renders its own full independent loop).
Streams get their own independent exclusion loop as before. The road
corridor's own path footprint is now PROTECTED by the boundary fence
(margined and clipped like every other developed feature) but still gets
no separate rendered fence line of its own (dropped/narrative-only, per
this session's earlier decision).

find_stream_exclusion_fencing(), find_boundary_fencing(), find_water_
zone_fencing(), and find_tree_zone_fencing() are all pure geometric
cores: no network I/O, taking already-fetched geometry (stream features
+ a target UTM CRS; the boundary polygon + the developed-footprint/zone
polygons the boundary fence encloses; the selected water zone's own
render_fill_polygon_utm; the full list of tree zone candidates' own
render_fill_polygon_utm values, respectively) -- same "logic separable
from fetching" split as water_candidate_zones.find_candidate_zones() and
every other *_candidates.py/*_corridors.py module in this codebase.
identify_boundary_fencing() is the fetch-and-wrap entry point that builds
boundary_polygon_utm, passes the developed-footprint/zone inputs straight
through to find_boundary_fencing(), and wraps the result in schema (it no
longer fetches canopy at all -- the boundary fence does not route around
canopy anymore). identify_fencing() is the full pipeline entry point
that additionally feeds find_water_zone_fencing() the selected water
zone's own render_fill_polygon_utm (water_suitability.fetch_and_
select_optimal_water_zone() if not supplied -- no zone sited cleanly
produces zero features), and find_tree_zone_fencing() every tree zone
candidate's own render_fill_polygon_utm (tree_zone_candidates.
identify_tree_zone_candidates() if not supplied -- no candidates
cleanly produces zero features). All four fence types land on the
"perimeter_fencing" layer (aside from stream exclusion's own separate
"exclusion_fencing" layer) -- kept as one shared layer rather than split
per fence type, distinguished by each feature's own "fence_type"
property, not by separate layers. identify_fencing() also still derives
the selected road corridor itself (road_corridors.identify_road_
corridor_candidates(), same no-anchor-point-given-produces-None
handling as before) -- NOT for a fence loop of its own anymore, but
because find_tree_zone_fencing()'s own upstream self-compute fallback
(identify_tree_zone_candidates()) needs it as a real siting exclusion:
tree zone candidates must not overlap the road corridor's own footprint,
independent of whether the corridor gets fenced at all.
"""

from typing import Optional, Union

from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, MultiLineString, Polygon, mapping, shape
from shapely.ops import unary_union

from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, make_feature, make_feature_collection
from hydrology_data import get_water_features_geojson
from raster_grid import SQUARE_METERS_PER_ACRE
from road_corridors import identify_road_corridor_candidates
from tree_zone_candidates import identify_tree_zone_candidates
from water_suitability import fetch_and_select_optimal_water_zone

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

# Extra working clearance (meters) the boundary fence line is pushed OUTWARD past the
# developed footprint's own real edge (production zones + structure site + road corridor
# path), so a farmer has room to actually walk and build a fence line rather than tracing
# flush against the last developed cell. The ONLY job of this constant is that walk/build
# clearance -- it is NOT responsible for root-zone buffering (already baked into WHERE
# production/etc. were sited upstream, never a fencing-level exclusion) or water/tree
# zone-fence buffering (each zone's own WATER_ZONE_FENCE_BUFFER_METERS/TREE_ZONE_FENCE_BUFFER_
# METERS handles that in step 3's zone-fence union) -- each of those is handled by an earlier
# or later step in find_boundary_fencing()'s own sequence, so this margin should not be
# reasoned about as needing to account for them. (Canopy is no longer a factor at all: a fence
# running through wooded canopy is fine in practice, so the boundary fence no longer routes
# around it -- see find_boundary_fencing()'s own docstring.) Its own SEPARATE constant from
# every other buffer in this module, same "constants stay separate even when they might share
# a value" convention. CONFIGURABLE -- tune by eye against the reference property.
BOUNDARY_FENCE_MARGIN_METERS = 5.0

# Area floor below which a fence-loop fragment produced by find_boundary_
# fencing() (e.g. a sliver where the drawn boundary clips a thin corner off
# the developed footprint's convex hull) is
# dropped rather than returned as a spurious extra segment -- its own
# SEPARATE constant from production_area.MIN_PRODUCTION_AREA_ACRES, even
# though both are "drop tiny slivers" floors: one gates a production
# PATCH (real farmable ground), this one gates a FENCE-LOOP FRAGMENT (a
# closed line, not an area to farm) -- different kinds of sliver, same
# "constants stay separate" convention noted above. CONFIGURABLE.
BOUNDARY_FENCE_MIN_SEGMENT_ACRES = 0.05

BOUNDARY_FENCE_CONFIDENCE_NOTES_TEMPLATE = (
    "This fence line encloses the property's DEVELOPED FOOTPRINT (production zones, structure "
    "site, and road corridor path), pushed out by a working margin, unioned with any water/tree "
    "exclusion-zone fences at the developed edge, simplified to its convex hull, and clipped to "
    "the drawn property boundary -- so fencing follows the land the farm actually develops rather "
    "than the full drawn parcel edge. It deliberately does NOT route around tree canopy (a fence "
    "through wooded ground is fine in practice).{split_note} It carries no fence type, height, or "
    "material specification (out of scope), and no legal parcel/ownership-boundary or survey data "
    "backs it -- confirm the exact line against a real survey before building on it."
)

BOUNDARY_FENCE_SPLIT_NOTE_TEMPLATE = (
    " The drawn property boundary clipped the developed footprint's convex hull into {segment_count} "
    "separate fence loops requiring {segment_count} separate physical fence runs, not one continuous "
    "loop."
)

# Buffer (meters) around the selected water zone's own render_fill_polygon_utm
# (an already-computed real fill footprint, not raw DEM cells -- see this
# module's own WATER ZONE EXCLUSION FENCING docstring section) that the water
# zone fence line is drawn at. Purpose here is specifically to guarantee the
# fence line sits truly OUTSIDE render_fill_polygon_utm's own edge, not flush
# against it (which would visually merge the fence with the zone's own fill
# outline) or clipping through it. Deliberately its OWN constant, NOT reused
# from TREE_ZONE_FENCE_BUFFER_METERS below even though both may start at the
# same placeholder value -- same "constants stay separate even when
# numerically identical" convention this module already applies elsewhere
# (see BOUNDARY_FENCE_CANOPY_BUFFER_METERS's own comment). Was 1.5m;
# bumped to 2.5m as a starting point per this session's tuning pass --
# CONFIGURABLE, tune further by eye if still too tight against the zone's
# own edge.
WATER_ZONE_FENCE_BUFFER_METERS = 2.5

# Same purpose/reasoning as WATER_ZONE_FENCE_BUFFER_METERS above, applied to
# each tree zone candidate's own render_fill_polygon_utm instead -- its own
# SEPARATE constant for the same "distinct constants for distinct purposes"
# reason. Was 1.5m; bumped to 2.5m alongside WATER_ZONE_FENCE_BUFFER_METERS
# -- CONFIGURABLE, tune further by eye if still too tight.
TREE_ZONE_FENCE_BUFFER_METERS = 2.5


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
    production_zone_polygons_utm: list[Polygon],
    structure_site_polygon_utm: Optional[Polygon],
    road_corridor_cell_footprint_polygon_utm: Optional[Polygon],
    water_zone_polygon_utm: Optional[Polygon],
    tree_zone_polygons_utm: list[Polygon],
    margin_meters: float = BOUNDARY_FENCE_MARGIN_METERS,
) -> list[LineString]:
    """
    Pure geometric core -- no network I/O, no DEM access. Returns the
    boundary fence line(s) enclosing the property's DEVELOPED FOOTPRINT
    (production zones + structure site + road corridor path), buffered by
    a working margin, simplified to its convex hull, and clipped to the
    drawn boundary, rather than unconditionally following the full drawn
    boundary. Every input polygon is already in the same UTM CRS.

    Canopy is deliberately NOT a factor: a fence running through wooded
    canopy is fine in practice, so this function no longer routes around
    it (previous passes differenced canopy out and then morphologically
    opened away the resulting notch/peninsula artifacts -- both steps, and
    the canopy input itself, are gone). margin_meters is the ONE extra
    working clearance this function adds itself (step 2).

    Algorithm, IN THIS EXACT ORDER -- each step's output feeds the next,
    so the separate constraints (margin, zone buffer, drawn boundary) each
    resolve independently without jointly reasoning about each other:

      1. developed_footprint_union = unary_union of all production zone
         polygons + the structure site (if any) + the road corridor's own
         undilated path footprint (if any). Water/tree zones are
         DELIBERATELY excluded here -- they enter separately in step 3 at
         their OWN buffer distance, not this function's margin.

      2. margined_core = developed_footprint_union.buffer(margin_meters).
         If the developed footprint is empty (a genuinely bare property --
         no production zones, no structure, no road corridor), margined_
         core falls back to boundary_polygon_utm itself, same as this
         module's original boundary-only behavior -- the honest degenerate
         case, not an error.

      3. zone_fences_union = unary_union of the water zone's own buffered
         polygon (at WATER_ZONE_FENCE_BUFFER_METERS) + each tree zone's
         own buffered polygon (at TREE_ZONE_FENCE_BUFFER_METERS), each via
         the SHARED _buffered_zone_polygon() the zones' own fences use.
         protective_core = margined_core.union(zone_fences_union). Because
         the SAME buffered polygon object feeds both this union and the
         zone's own independently-drawn fence, wherever a zone extends past
         margined_core's edge the union's boundary there BECOMES that zone's
         own buffered edge; wherever a zone sits inside margined_core with
         real clearance, the union changes nothing and the zone's own fence
         stays genuinely independent. (The convex hull in step 4 can move
         the boundary fence line off a zone edge that is itself non-convex
         or recessed relative to the overall hull -- the render-time
         coincidence-trim in render_layout_map.py, not this function,
         suppresses any resulting doubled line.)

      4. hulled = protective_core.convex_hull, then clipped =
         hulled.intersection(boundary_polygon_utm). The convex hull is what
         keeps the fence from dipping inward through the gap between two
         separated developed clusters (the shape that motivated this pass).
         The drawn-boundary clip is a hard ceiling that always wins (so the
         road corridor's own path, which can reach a real anchor ON the
         boundary, never produces fence geometry outside the property) --
         and it is now the ONLY step that can reintroduce a concavity or
         split the result into multiple pieces (an oddly-shaped drawn
         boundary intersecting a convex hull can do both). The MultiPolygon-
         aware ring extraction below is kept defensively for that case even
         though it fires far less often than the old canopy-driven split.

      5. Only EXTERIOR rings of `clipped` are kept -- a Polygon contributes
         its one exterior ring, a MultiPolygon one per piece. Interior rings
         (holes) are discarded entirely. Each kept ring is returned as its
         own closed LineString (first coord == last coord), ordered by
         enclosed area LARGEST FIRST (deterministic; the ordering identify_
         boundary_fencing() labels "Boundary Fencing 1 / 2" from). Fragments
         below BOUNDARY_FENCE_MIN_SEGMENT_ACRES are dropped rather than
         returned as a spurious extra segment (that floor is unrelated to
         the removed canopy sliver-opening step and stays).
    """
    # Step 1: developed footprint (production + structure + road corridor path).
    # Water/tree zones are deliberately NOT included here -- they enter in step 3.
    developed_parts = [p for p in production_zone_polygons_utm if p is not None and not p.is_empty]
    if structure_site_polygon_utm is not None and not structure_site_polygon_utm.is_empty:
        developed_parts.append(structure_site_polygon_utm)
    if (
        road_corridor_cell_footprint_polygon_utm is not None
        and not road_corridor_cell_footprint_polygon_utm.is_empty
    ):
        developed_parts.append(road_corridor_cell_footprint_polygon_utm)
    developed_footprint_union = unary_union(developed_parts) if developed_parts else Polygon()

    # Step 2: working margin past the developed edge -- or, on a genuinely bare
    # property (no developed footprint at all), fall back to the drawn boundary,
    # the honest degenerate case that reproduces this module's original behavior.
    if developed_footprint_union.is_empty:
        margined_core = boundary_polygon_utm
    else:
        margined_core = developed_footprint_union.buffer(margin_meters)

    # Step 3: union in each water/tree zone's OWN buffered polygon (the SAME
    # buffered object its own fence is drawn from -- see _buffered_zone_polygon()),
    # so the boundary fence's path meets a zone's own fence edge wherever that zone
    # sits at the true outer edge of the developed footprint.
    zone_parts = []
    if water_zone_polygon_utm is not None and not water_zone_polygon_utm.is_empty:
        zone_parts.append(
            _buffered_zone_polygon(water_zone_polygon_utm, WATER_ZONE_FENCE_BUFFER_METERS, "find_boundary_fencing")
        )
    for tree_polygon_utm in tree_zone_polygons_utm:
        if tree_polygon_utm is None or tree_polygon_utm.is_empty:
            continue
        zone_parts.append(
            _buffered_zone_polygon(tree_polygon_utm, TREE_ZONE_FENCE_BUFFER_METERS, "find_boundary_fencing")
        )
    zone_fences_union = unary_union(zone_parts) if zone_parts else Polygon()
    protective_core = margined_core.union(zone_fences_union)

    # Degenerate boundary-only case (no developed footprint, no zone fences):
    # reproduce this module's original plain-wrap behavior EXACTLY -- the boundary's
    # own exterior ring, with no convex-hull/clip re-noding of its coordinates.
    if developed_footprint_union.is_empty and zone_fences_union.is_empty:
        return [LineString(boundary_polygon_utm.exterior.coords)]

    # Step 4: convex hull (keeps the fence from dipping inward through the gap
    # between separated developed clusters), then clip to the drawn boundary
    # (the hard ceiling, and now the only step that can reintroduce a concavity
    # or split into multiple pieces).
    hulled = protective_core.convex_hull
    clipped = hulled.intersection(boundary_polygon_utm)
    if clipped.is_empty:
        return []

    if clipped.geom_type == "Polygon":
        candidate_polygons = [clipped]
    elif clipped.geom_type == "MultiPolygon":
        candidate_polygons = list(clipped.geoms)
    else:
        # Defensive: a convex-hull-intersect-boundary result is a Polygon or
        # MultiPolygon in the ordinary case, but guard against a degenerate
        # GeometryCollection (e.g. a zero-area Point/LineString boundary touch)
        # by keeping only real Polygon parts, same tolerance-for-topology-noise
        # reasoning raster_grid.cell_union_footprint()'s own buffer(0) cleanup uses.
        candidate_polygons = [
            g for g in getattr(clipped, "geoms", []) if g.geom_type == "Polygon"
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


def boundary_fencing_to_geojson(rings_wgs84: list) -> dict:
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
    confidence_notes = BOUNDARY_FENCE_CONFIDENCE_NOTES_TEMPLATE.format(split_note=split_note)

    features = []
    for i, ring in enumerate(rings_wgs84, start=1):
        geometry_wgs84 = ring if isinstance(ring, dict) else mapping(ring)
        extra_properties = {
            "fence_type": "boundary",
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


WATER_ZONE_FENCE_CONFIDENCE_NOTES_TEMPLATE = (
    "This fence line fully encloses the selected water system candidate zone's own real, "
    "already-computed render footprint, buffered {buffer_meters}m ({buffer_feet}ft) outward so "
    "the line sits clear of the zone's own edge rather than flush against it. It inherits the "
    "water zone's own siting confidence and limitations already documented at that layer "
    "(water_suitability.py/water_candidate_zones.py) -- this fence line is exactly as reliable as "
    "the zone geometry it encloses, no more, no less. It is INDEPENDENT of every other fence type "
    "here -- not spliced or gated into the boundary fence or any other fence loop, and may "
    "visually overlap them near the zone's own edge; that overlap is expected, not a bug."
)

TREE_ZONE_FENCE_CONFIDENCE_NOTES_TEMPLATE = (
    "This fence line fully encloses one tree zone candidate's own real, already-computed render "
    "footprint, buffered {buffer_meters}m ({buffer_feet}ft) outward so the line sits clear of the "
    "candidate's own edge rather than flush against it. Unlike production/water/road zones, there "
    "is NO selection step for tree zone candidates -- EVERY ranked candidate on this property gets "
    "its own separate fence loop here, so a property with multiple tree zone candidates will show "
    "multiple separate tree zone fence loops, not just one. It inherits that candidate's own siting "
    "confidence and limitations already documented at that layer (tree_zone_candidates.py) -- this "
    "fence line is exactly as reliable as the candidate geometry it encloses, no more, no less. It "
    "is INDEPENDENT of every other fence type here -- not spliced or gated into the boundary fence "
    "or any other fence loop, and may visually overlap them (including other tree zone candidates' "
    "own fence loops, where two candidates sit close together); that overlap is expected, not a bug."
)


def _buffered_zone_polygon(
    polygon_utm: Polygon, buffer_meters: float, caller_label: str = "_buffered_zone_polygon"
):
    """
    Shared buffered-zone-POLYGON core -- the single place a water/tree zone's
    own already-computed real fill polygon (water_candidate_zones.py's/tree_
    zone_candidates.py's own render_fill_polygon_utm, NOT raw DEM path cells)
    is buffered outward by its own zone-fence distance. Extracted here so it
    is called from THREE places off ONE definition:

      - find_water_zone_fencing()/find_tree_zone_fencing() (via
        _buffer_fill_polygon_to_fence_line() below), which take this buffered
        polygon's OUTLINE as the zone's own independently-drawn fence line;
      - find_boundary_fencing()'s step 3 zone-fence union, which unions this
        SAME buffered polygon into the boundary fence's protective core.

    Because both computations start from the identical buffered polygon
    (same input polygon + same buffer distance -> same coordinates), the
    boundary fence's "where a zone sits at the developed edge, my path
    BECOMES that zone's own fence edge" behavior is exact BY CONSTRUCTION,
    not by two independent buffer calls that merely happen to share a
    constant and could drift apart under a future retune.

    render_fill_polygon_utm is a bounded render opening (water_candidate_
    zones.py/tree_zone_candidates.py) that can itself be a MultiPolygon when
    the opening severs a narrow pinch into pieces, so a positive buffer can
    legitimately come back as EITHER a single Polygon or a MultiPolygon (two
    pieces too far apart to merge under the buffer). Both are accepted:
    _buffer_fill_polygon_to_fence_line() below outlines each part, and find_
    boundary_fencing()'s step 3 unary_union() folds either shape in unchanged.
    An empty or invalid result still has no sensible fallback, so this raises
    rather than silently dropping the fence.
    """
    buffered = polygon_utm.buffer(buffer_meters)
    if buffered.is_empty or not buffered.is_valid or buffered.geom_type not in ("Polygon", "MultiPolygon"):
        raise RuntimeError(
            f"{caller_label}: buffering a render_fill_polygon_utm by {buffer_meters}m produced an "
            f"unexpected result (geom_type={buffered.geom_type!r}, empty={buffered.is_empty}, "
            f"valid={buffered.is_valid}) -- a positive buffer of a real, already-computed polygon "
            "should never be empty or invalid, and there is no sensible fallback shape here."
        )
    return buffered


def _buffer_fill_polygon_to_fence_line(polygon_utm: Polygon, buffer_meters: float, caller_label: str):
    """
    Shared buffer-and-outline core for find_water_zone_fencing()/find_tree_
    zone_fencing() below -- both fence an already-computed real fill polygon
    by taking the OUTLINE of _buffered_zone_polygon()'s own buffered polygon
    (see that helper's docstring for the recipe, the shared-buffered-polygon
    exactness guarantee, and the "no sensible fallback" reasoning on an
    empty/invalid buffer result). This is the SAME "buffer a real feature,
    output its outline" recipe find_stream_exclusion_fencing() already uses.

    Returns a LineString for a single-piece buffered polygon, or a
    MultiLineString (one closed exterior ring per piece) when render_fill_
    polygon_utm's bounded opening severed the fill into a MultiPolygon -- so a
    multi-piece water/tree zone still draws one fence loop around each piece.
    """
    buffered = _buffered_zone_polygon(polygon_utm, buffer_meters, caller_label)
    if buffered.geom_type == "Polygon":
        return LineString(buffered.exterior.coords)
    return MultiLineString([LineString(part.exterior.coords) for part in buffered.geoms])


def find_water_zone_fencing(
    selected_water_zone_render_fill_polygon_utm: Optional[Polygon],
    buffer_meters: float = WATER_ZONE_FENCE_BUFFER_METERS,
) -> Optional[Union[LineString, MultiLineString]]:
    """
    Pure geometric core -- no network I/O. Takes the selected water zone's
    own already-computed render_fill_polygon_utm (water_candidate_zones.
    find_candidate_zones()'s own real fill footprint, the SAME polygon
    render_layout_map.py already draws that zone's fill from) and returns a
    closed fence line buffer_meters outside its edge (see
    _buffer_fill_polygon_to_fence_line()'s own docstring for the recipe and
    the "no sensible fallback" reasoning on an unexpected buffer result).
    The fill can be a MultiPolygon (the bounded render opening severs a narrow
    pinch), in which case the fence comes back as a MultiLineString -- one
    closed loop per piece.

    None or empty input (no water zone was sited on this property at all)
    returns None -- the existing "honest no-result" case this pipeline
    already handles elsewhere, not an error.
    """
    if selected_water_zone_render_fill_polygon_utm is None or selected_water_zone_render_fill_polygon_utm.is_empty:
        return None
    return _buffer_fill_polygon_to_fence_line(
        selected_water_zone_render_fill_polygon_utm, buffer_meters, "find_water_zone_fencing"
    )


def water_zone_fencing_to_geojson(
    fence_line: Optional[LineString], buffer_meters: float = WATER_ZONE_FENCE_BUFFER_METERS
) -> dict:
    """
    Wraps find_water_zone_fencing()'s output (already reprojected to WGS84
    by the caller -- see identify_fencing()) as a schema-conformant Feature
    on the "perimeter_fencing" layer (fence_type="water_zone_exclusion").

    fence_line may be a shapely LineString or an already-mapped GeoJSON
    geometry dict. None (no water zone at all) returns an empty
    FeatureCollection, not an error.
    """
    if fence_line is None:
        return make_feature_collection([])

    geometry_wgs84 = fence_line if isinstance(fence_line, dict) else mapping(fence_line)
    confidence_notes = WATER_ZONE_FENCE_CONFIDENCE_NOTES_TEMPLATE.format(
        buffer_meters=round(buffer_meters, 3), buffer_feet=round(buffer_meters / METERS_PER_FOOT, 1)
    )
    feature = make_feature(
        feature_id="perimeter-fencing-water-zone",
        geometry=geometry_wgs84,
        layer="perimeter_fencing",
        label="Water zone fencing",
        confidence=CONFIDENCE_HIGH,
        confidence_notes=confidence_notes,
        extra_properties={
            "fence_type": "water_zone_exclusion",
            "buffer_meters": buffer_meters,
        },
    )
    return make_feature_collection([feature])


def find_tree_zone_fencing(
    tree_zone_render_fill_polygons_utm: list[Polygon],
    buffer_meters: float = TREE_ZONE_FENCE_BUFFER_METERS,
) -> list[Union[LineString, MultiLineString]]:
    """
    Pure geometric core -- no network I/O. Takes EVERY tree zone
    candidate's own already-computed render_fill_polygon_utm (tree_zone_
    candidates.py has no selection step -- identify_tree_zone_candidates()'s
    own 'patches' is the full ranked list, not narrowed to one claimed
    candidate the way production/water/road are) and returns one closed
    fence line per candidate, buffer_meters outside its own edge -- same
    recipe as find_water_zone_fencing() above (see
    _buffer_fill_polygon_to_fence_line()'s own docstring), applied to each
    polygon INDEPENDENTLY. No merging of two candidates that happen to sit
    close together into one combined loop, even if their buffers would
    overlap -- that's explicitly out of scope for this pass.

    None/empty entries in the input list are skipped rather than erroring.
    Returns one LineString per valid input polygon, in the SAME order as
    the input list -- this ordering is the basis for "Tree Zone Fencing
    {rank}" labeling downstream (see tree_zone_fencing_to_geojson()).
    """
    fence_lines = []
    for polygon_utm in tree_zone_render_fill_polygons_utm:
        if polygon_utm is None or polygon_utm.is_empty:
            continue
        fence_lines.append(_buffer_fill_polygon_to_fence_line(polygon_utm, buffer_meters, "find_tree_zone_fencing"))
    return fence_lines


def tree_zone_fencing_to_geojson(
    fence_lines: list, buffer_meters: float = TREE_ZONE_FENCE_BUFFER_METERS
) -> dict:
    """
    Wraps find_tree_zone_fencing()'s output (already reprojected to WGS84
    by the caller -- see identify_fencing()) as schema-conformant Features
    on the "perimeter_fencing" layer (fence_type="tree_zone_exclusion") --
    one feature per input LineString, each carrying its own 1-based
    "candidate_rank" property matching the SAME order/ranking tree zone
    candidates already carry elsewhere in this pipeline (tree_zone_
    candidates.py's own 'rank' field), so "Tree Zone Fencing 2" can be
    cross-referenced against the same-numbered "Tree Zone Candidate 2".

    fence_lines entries may be shapely LineString geometries or already-
    mapped GeoJSON geometry dicts.
    """
    confidence_notes = TREE_ZONE_FENCE_CONFIDENCE_NOTES_TEMPLATE.format(
        buffer_meters=round(buffer_meters, 3), buffer_feet=round(buffer_meters / METERS_PER_FOOT, 1)
    )

    features = []
    for i, fence_line in enumerate(fence_lines, start=1):
        geometry_wgs84 = fence_line if isinstance(fence_line, dict) else mapping(fence_line)
        features.append(
            make_feature(
                feature_id=f"perimeter-fencing-tree-zone-{i}",
                geometry=geometry_wgs84,
                layer="perimeter_fencing",
                label=f"Tree zone fencing {i}",
                confidence=CONFIDENCE_HIGH,
                confidence_notes=confidence_notes,
                extra_properties={
                    "fence_type": "tree_zone_exclusion",
                    "candidate_rank": i,
                    "buffer_meters": buffer_meters,
                },
            )
        )
    return make_feature_collection(features)


def identify_boundary_fencing(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    production_zone_polygons_utm: Optional[list] = None,
    structure_site_polygon_utm: Optional[Polygon] = None,
    road_corridor_cell_footprint_polygon_utm: Optional[Polygon] = None,
    water_zone_polygon_utm: Optional[Polygon] = None,
    tree_zone_polygons_utm: Optional[list] = None,
) -> dict:
    """
    Fetch-and-wrap entry point for the developed-footprint boundary fence. Fetches
    dem if not supplied (get_dem_for_boundary(), same pattern every other
    module in this codebase uses) and builds boundary_polygon_utm from
    boundary_coordinates via dem["crs"] (same warp_transform pattern
    road_corridors.identify_road_corridor_candidates() already uses). It no
    longer fetches canopy at all: the boundary fence no longer routes around
    tree canopy (a fence through wooded ground is fine in practice -- see
    find_boundary_fencing()), so there is nothing left for a canopy fetch to
    feed, and the dem is used ONLY to build boundary_polygon_utm / reproject
    the result.

    production_zone_polygons_utm/structure_site_polygon_utm/road_corridor_
    cell_footprint_polygon_utm/water_zone_polygon_utm/tree_zone_polygons_utm
    are the developed-footprint + zone inputs find_boundary_fencing()
    protects (see its own docstring for the full algorithm). They are pure
    PASS-THROUGH here -- this entry point does no siting of its own and
    never self-computes them; a caller (identify_fencing()) supplies
    whatever it already has, each already in dem["crs"]. All default to
    None/empty, in which case find_boundary_fencing() collapses to the
    plain drawn-boundary loop (see its own degenerate fallback), so
    existing callers that pass only boundary_coordinates/dem are unaffected.

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

    fence_rings_utm = find_boundary_fencing(
        boundary_polygon_utm,
        production_zone_polygons_utm or [],
        structure_site_polygon_utm,
        road_corridor_cell_footprint_polygon_utm,
        water_zone_polygon_utm,
        tree_zone_polygons_utm or [],
    )
    segment_count = len(fence_rings_utm)

    rings_wgs84 = [
        shape(transform_geom(dem["crs"], "EPSG:4326", mapping(ring))) for ring in fence_rings_utm
    ]
    fencing_geojson = boundary_fencing_to_geojson(rings_wgs84)

    return {"fencing_geojson": fencing_geojson, "segment_count": segment_count}


def identify_fencing(
    boundary_coordinates: list[tuple[float, float]],
    water_features_geojson: Optional[dict] = None,
    dem: Optional[dict] = None,
    stream_exclusion_buffer_meters: float = STREAM_EXCLUSION_BUFFER_METERS,
    anchor_lon_lat: Optional[tuple[float, float]] = None,
    selected_water_zone_render_fill_polygon_utm: Optional[Polygon] = None,
    tree_zone_render_fill_polygons_utm: Optional[list] = None,
    water_zone_fence_buffer_meters: float = WATER_ZONE_FENCE_BUFFER_METERS,
    tree_zone_fence_buffer_meters: float = TREE_ZONE_FENCE_BUFFER_METERS,
    boundary_polygon_utm: Optional[Polygon] = None,
    production_areas: Optional[list[dict]] = None,
    valleys: Optional[list[dict]] = None,
    selected_road_corridor: Optional[dict] = None,
    selected_water_zone: Optional[dict] = None,
    tree_zone_patches: Optional[list[dict]] = None,
    hydric_floodplain_union=None,
    floodplain_data_is_fallback: Optional[bool] = None,
    canopy_height: Optional[dict] = None,
    production_zone_polygons_utm: Optional[list[Polygon]] = None,
    structure_site_feature: Optional[dict] = None,
    road_corridor_cell_footprint_polygon_utm: Optional[Polygon] = None,
) -> dict:
    """
    Full pipeline entry point for Subdivision Fences' computed geometry
    (stream exclusion, boundary, water zone exclusion, and tree zone
    exclusion fencing -- see module docstring for why everything else in
    that report step is narrative-only, not generated here, and why no
    road -- existing or generated -- gets a fence loop of its own). Every
    fence type past boundary/stream is a fully independent, closed loop
    -- deliberately NOT spliced or gated into the boundary fence or each
    other (see module docstring) -- so an overlap between any two of
    them is expected, not a bug.

    water_features_geojson is hydrology_data.get_water_features_geojson()'s
    output; fetched here if not already supplied (e.g. reused from a
    caller that already fetched it). Same independent-fetch pattern
    every other pipeline module in this codebase uses (each fetches what
    it needs rather than sharing state across pipeline stages) --
    generate_full_report.py's own hydrology fetch for the WATER SUPPLY
    section is untouched by this. dem, likewise, is fetched here if not
    already supplied (get_dem_for_boundary()) -- identify_boundary_
    fencing() needs one to build boundary_polygon_utm and reproject its
    result (see that function's own docstring), same optional-dem pattern
    every other entry point in this codebase already uses. Every UTM geometry this
    function derives itself when a caller doesn't supply one (the water
    zone's render_fill_polygon_utm, tree zone candidates' own render_
    fill_polygon_utm values) is derived from this SAME shared dem, so
    it's always in dem["crs"] -- a caller supplying one of those directly
    instead is responsible for it already being in that same CRS.

    anchor_lon_lat defaults to None, the common "not yet wired up to a
    real anchor point" case -- identify_road_corridor_candidates() (see
    selected_road_corridor below) already returns a clean selected_road_
    corridor=None for it (no network fetch even attempted), which is NOT
    an error.

    selected_water_zone_render_fill_polygon_utm is water_candidate_
    zones.find_candidate_zones()'s own already-computed render_fill_
    polygon_utm for the single selected water zone -- the SAME real fill
    footprint render_layout_map.py already draws that zone's fill from,
    NOT re-derived here. If not supplied, this fetches it itself via
    water_suitability.fetch_and_select_optimal_water_zone(dem=dem) -- NOT
    a re-run of water-zone siting/scoring logic, just this module's own
    copy of the SAME already-computed selection every other caller
    reuses. No water zone sited on this property (None) cleanly produces
    zero water-zone-exclusion features, not an error.

    tree_zone_render_fill_polygons_utm is the full list of EVERY tree
    zone candidate's own already-computed render_fill_polygon_utm --
    tree_zone_candidates.py has no selection step, unlike production/
    water/road, so every ranked candidate gets fenced (see module
    docstring). If not supplied, this fetches it itself via tree_zone_
    candidates.identify_tree_zone_candidates(dem=dem)'s own 'patches'
    list, in that list's own existing rank order. No tree zone
    candidates on this property (empty list) cleanly produces zero
    tree-zone-exclusion features, not an error.

    selected_road_corridor/selected_water_zone/tree_zone_patches are
    pipeline_context.PipelineContext's own HIGH-LEVEL fields (matching
    its field names/shapes exactly). selected_water_zone/tree_zone_
    patches are each an alternative to supplying the LOW-LEVEL selected_
    water_zone_render_fill_polygon_utm/tree_zone_render_fill_polygons_utm
    values directly -- for each pair, the low-level value wins if both
    are supplied (unchanged behavior); otherwise, if the high-level value
    is supplied, the low-level value is derived from it directly (its own
    'render_fill_polygon_utm'/list-of-'render_fill_polygon_utm' fields)
    and fetch_and_select_optimal_water_zone()/identify_tree_zone_
    candidates() are not called at all; otherwise this self-computes via
    those same calls, same as before. selected_road_corridor has no
    low-level counterpart on this function's own signature anymore (see
    module docstring for why) -- when supplied, it's forwarded straight
    into identify_tree_zone_candidates()'s own self-compute fallback (its
    real siting-exclusion input) and identify_road_corridor_candidates()
    is not called at all; otherwise, if identify_tree_zone_candidates()
    itself is about to be self-computed (tree_zone_render_fill_polygons_
    utm/tree_zone_patches BOTH still None at that point), this derives it
    itself via that same call, purely for that same exclusion purpose --
    a caller that already supplies tree_zone_render_fill_polygons_utm/
    tree_zone_patches directly never triggers this derivation at all,
    since nothing downstream would consume it.

    boundary_polygon_utm/production_areas/valleys are PASS-THROUGH-ONLY
    overrides (same "require it, no self-compute fallback" decision as
    solar_suitability.py/tree_zone_candidates.py make for these same
    fields) forwarded into all three self-compute fallback calls above
    so a caller that already has these (e.g. pipeline_context.py) never
    causes those calls to re-derive them independently -- closing the
    same nested, un-deduped-chain gap solar_suitability.py's own
    identify_solar_candidate_zones() was fixed for. boundary_polygon_utm
    is also computed here (once, from boundary_coordinates) if not
    supplied, same warp_transform pattern this function already used
    inline, now shared by all three self-compute calls. hydric_
    floodplain_union/floodplain_data_is_
    fallback are forwarded the same way, straight through to identify_
    road_corridor_candidates()/identify_tree_zone_candidates() (fetch_
    and_select_optimal_water_zone() doesn't take them).

    canopy_height is an optional pre-fetched override (the same dict
    canopy_height_data.get_canopy_height_for_boundary() returns, e.g.
    parcel_data.ParcelData.canopy_height) forwarded into every self-compute
    fallback call above that itself accepts it (identify_road_corridor_
    candidates()/fetch_and_select_optimal_water_zone()/identify_tree_zone_
    candidates()), when the corresponding override isn't itself already
    supplied. Same forwarding pattern as boundary_polygon_utm/production_
    areas/valleys above -- closes the same nested-fetch gap, this time for
    canopy, so a caller supplying canopy_height here never causes any of
    those three self-compute calls to issue its own redundant canopy fetch.
    It is NOT forwarded to identify_boundary_fencing() anymore -- the
    boundary fence no longer uses canopy at all (see find_boundary_
    fencing()).

    production_zone_polygons_utm/structure_site_feature/road_corridor_cell_
    footprint_polygon_utm are this branch's addition -- the developed-
    footprint inputs the rewritten boundary fence now protects (see find_
    boundary_fencing()). They are received as ALREADY-COMPUTED results
    (same pattern as dem above), NOT re-sited here: a caller (render_layout_
    map.fetch_layout_layers()) passes production_area_ceiling.py's ceiling-
    trimmed zone polygons (each zone's render_fill_polygon_utm, in
    dem["crs"]), solar_suitability.py's OWN final structure-site GeoJSON
    Feature (whose geometry is WGS84 and is reprojected into dem["crs"]
    here before use), and road_corridors.py's own undilated cell_footprint_
    polygon_utm (the raw path shape, NOT any dilated/inset fence geometry --
    the road corridor's own fence line stays dropped/narrative-only; only
    its bare path footprint is protected here). The water/tree developed-
    edge inputs are NOT new parameters -- find_boundary_fencing() reuses the
    SAME selected_water_zone_render_fill_polygon_utm/tree_zone_render_fill_
    polygons_utm this function already derives for the water/tree zone
    fences, so the boundary fence meets those zones' own fence edges exactly
    by construction. Any of these left None/empty (no production sited, no
    structure, no road corridor, no water/tree zone) is handled gracefully
    by find_boundary_fencing()'s own degenerate fallbacks -- this function
    just passes through whatever it has, substituting no defaults and
    skipping no call.

    Returns:
        {
            'fencing_geojson': FeatureCollection,   # "exclusion_fencing" (stream) + "perimeter_fencing" (boundary + water zone + tree zone) features
            'segment_count': int,                   # identify_boundary_fencing()'s own segment_count, passed through
        }
    """
    if water_features_geojson is None:
        water_features_geojson = get_water_features_geojson(boundary_coordinates)
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    if boundary_polygon_utm is None:
        boundary_xs, boundary_ys = warp_transform(
            "EPSG:4326",
            dem["crs"],
            [pt[0] for pt in boundary_coordinates],
            [pt[1] for pt in boundary_coordinates],
        )
        boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    stream_features = [
        f for f in water_features_geojson["features"] if f["properties"]["layer"] == "hydrology-streams"
    ]

    utm_crs = _utm_crs_for_boundary(boundary_coordinates)
    stream_entries = find_stream_exclusion_fencing(stream_features, utm_crs, stream_exclusion_buffer_meters)

    if selected_water_zone_render_fill_polygon_utm is None:
        if selected_water_zone is None:
            selected_water_zone = fetch_and_select_optimal_water_zone(
                boundary_coordinates,
                dem=dem,
                boundary_polygon_utm=boundary_polygon_utm,
                valleys=valleys,
                production_areas=production_areas,
                canopy_height=canopy_height,
            )
        selected_water_zone_render_fill_polygon_utm = (
            selected_water_zone["render_fill_polygon_utm"] if selected_water_zone else None
        )

    water_fence_line_utm = find_water_zone_fencing(
        selected_water_zone_render_fill_polygon_utm, buffer_meters=water_zone_fence_buffer_meters
    )
    water_fence_line_wgs84 = (
        shape(transform_geom(dem["crs"], "EPSG:4326", mapping(water_fence_line_utm)))
        if water_fence_line_utm is not None
        else None
    )
    water_zone_geojson = water_zone_fencing_to_geojson(
        water_fence_line_wgs84, buffer_meters=water_zone_fence_buffer_meters
    )

    if tree_zone_render_fill_polygons_utm is None:
        if tree_zone_patches is None:
            if selected_road_corridor is None:
                # Derived purely as a real siting-exclusion input for tree zone
                # candidates below (tree zones must not overlap the road
                # corridor's own footprint) -- no fence loop of its own is
                # built from this anymore (see module docstring for why
                # roads, existing or generated, don't get one). Derived here,
                # not unconditionally at the top of this function, so a
                # caller that already supplies tree_zone_render_fill_
                # polygons_utm/tree_zone_patches directly never pays for this
                # self-compute -- same "don't self-compute what won't be
                # used" discipline the rest of this function already applies.
                road_corridor_candidates = identify_road_corridor_candidates(
                    boundary_coordinates,
                    dem=dem,
                    anchor_lon_lat=anchor_lon_lat,
                    boundary_polygon_utm=boundary_polygon_utm,
                    production_areas=production_areas,
                    valleys=valleys,
                    hydric_floodplain_union=hydric_floodplain_union,
                    floodplain_data_is_fallback=floodplain_data_is_fallback,
                    canopy_height=canopy_height,
                )
                selected_road_corridor = road_corridor_candidates["selected_road_corridor"]
            tree_zone_result = identify_tree_zone_candidates(
                boundary_coordinates,
                dem=dem,
                anchor_lon_lat=anchor_lon_lat,
                boundary_polygon_utm=boundary_polygon_utm,
                production_areas=production_areas,
                valleys=valleys,
                selected_water_zone=selected_water_zone,
                selected_road_corridor=selected_road_corridor,
                hydric_floodplain_union=hydric_floodplain_union,
                floodplain_data_is_fallback=floodplain_data_is_fallback,
                canopy_height=canopy_height,
            )
            tree_zone_patches = tree_zone_result.get("patches", [])
        tree_zone_render_fill_polygons_utm = [
            patch["render_fill_polygon_utm"] for patch in tree_zone_patches
        ]

    tree_fence_lines_utm = find_tree_zone_fencing(
        tree_zone_render_fill_polygons_utm, buffer_meters=tree_zone_fence_buffer_meters
    )
    tree_fence_lines_wgs84 = [
        shape(transform_geom(dem["crs"], "EPSG:4326", mapping(line))) for line in tree_fence_lines_utm
    ]
    tree_zone_geojson = tree_zone_fencing_to_geojson(
        tree_fence_lines_wgs84, buffer_meters=tree_zone_fence_buffer_meters
    )

    # Boundary fencing now protects the DEVELOPED FOOTPRINT (production + structure +
    # road corridor path), buffered by a margin and clipped to the drawn boundary, with
    # water/tree zones unioned in at their own fence distance (see find_boundary_fencing()).
    # The water/tree polygons handed in here are the SAME already-derived render_fill_
    # polygon_utm values the water/tree zone fences above were built from -- reusing them
    # (rather than re-buffering independently) is what makes the boundary fence's path
    # coincide EXACTLY with a zone's own fence edge where that zone sits at the developed
    # edge. structure_site_feature is solar_suitability.py's final GeoJSON Feature, whose
    # geometry is WGS84 (confirmed against that module); it is reprojected into dem["crs"]
    # here so it lands in the same UTM space as every other developed-footprint input.
    # production_zone_polygons_utm/road_corridor_cell_footprint_polygon_utm are pure
    # pass-throughs (already-computed results a caller supplies, per this function's docstring
    # pattern), each already in dem["crs"]; any that a caller leaves None/empty collapses that
    # part of find_boundary_fencing()'s footprint union cleanly, no substitute/default.
    structure_site_polygon_utm = None
    if structure_site_feature is not None and structure_site_feature.get("geometry") is not None:
        structure_site_polygon_utm = shape(
            transform_geom("EPSG:4326", dem["crs"], structure_site_feature["geometry"])
        )

    boundary_result = identify_boundary_fencing(
        boundary_coordinates,
        dem=dem,
        production_zone_polygons_utm=production_zone_polygons_utm or [],
        structure_site_polygon_utm=structure_site_polygon_utm,
        road_corridor_cell_footprint_polygon_utm=road_corridor_cell_footprint_polygon_utm,
        water_zone_polygon_utm=selected_water_zone_render_fill_polygon_utm,
        tree_zone_polygons_utm=tree_zone_render_fill_polygons_utm,
    )

    features = (
        stream_exclusion_fencing_to_geojson(stream_entries, stream_exclusion_buffer_meters)["features"]
        + boundary_result["fencing_geojson"]["features"]
        + water_zone_geojson["features"]
        + tree_zone_geojson["features"]
    )

    return {"fencing_geojson": make_feature_collection(features), "segment_count": boundary_result["segment_count"]}


def summarize_fencing(result: dict) -> str:
    features = result["fencing_geojson"]["features"]
    stream_count = sum(1 for f in features if f["properties"]["layer"] == "exclusion_fencing")
    boundary_features = [f for f in features if f["properties"].get("fence_type") == "boundary"]
    water_zone_count = sum(1 for f in features if f["properties"].get("fence_type") == "water_zone_exclusion")
    tree_zone_count = sum(1 for f in features if f["properties"].get("fence_type") == "tree_zone_exclusion")
    segment_count = result.get("segment_count", len(boundary_features))

    lines = [
        f"Computed fencing features: {stream_count} stream exclusion, "
        f"{segment_count} boundary fencing segment(s), "
        f"{water_zone_count} water zone exclusion, {tree_zone_count} tree zone exclusion"
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
    # road corridor (feeding tree zone candidates' own siting exclusion below,
    # not a fence loop -- see module docstring for why roads don't get one).
    anchor_lon_lat = (-79.98356157031265, 40.64303511679458)

    print(
        "Identifying computed fencing (stream exclusion + developed-footprint boundary + "
        "water zone exclusion + tree zone exclusion) "
        "for property boundary...\n"
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

        fence_types_present = {f["properties"].get("fence_type") for f in result["fencing_geojson"]["features"]}
        print(f"\nWater zone fence produced: {'water_zone_exclusion' in fence_types_present}")
        print(f"Tree zone fence produced: {'tree_zone_exclusion' in fence_types_present}")
        print("\nfencing_geojson is schema-valid.")
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map ImageServer/3DEP lidar HAG services — not a fully sandboxed environment."
        )
