"""
fencing.py

Subdivision Fences data layer (Scale of Permanence step 7). Two fencing
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

find_stream_exclusion_fencing() and find_boundary_fencing() are both pure
geometric cores: no network I/O, taking already-fetched geometry (stream
features + a target UTM CRS; a boundary polygon + an already-fetched/
already-buffered canopy footprint, respectively) -- same "logic separable
from fetching" split as water_candidate_zones.find_candidate_zones() and
every other *_candidates.py/*_corridors.py module in this codebase.
identify_boundary_fencing() is the fetch-and-wrap entry point that feeds
find_boundary_fencing() real canopy data (production_area.
get_required_tree_root_zone_mask_utm(), the SAME mandatory canopy gate
production_area.py/tree_zone_candidates.py already use) and wraps the
result in schema, both on the "perimeter_fencing" layer -- kept as one
layer (not split per fence type) since road/water/tree-crop/stream-
exclusion fencing will all land here too in later passes, distinguished
by each feature's own "fence_type" property, not by separate layers.
"""

from typing import Optional

from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, Polygon, mapping, shape

from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, make_feature, make_feature_collection
from hydrology_data import get_water_features_geojson
from production_area import get_required_tree_root_zone_mask_utm
from raster_grid import SQUARE_METERS_PER_ACRE, cell_union_footprint

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
# vs, here, fence routing). A narrower buffer than the 10ft production/
# water gates -- 5ft is enough working clearance for a fence builder to
# walk the line without brushing branches, not a full root-zone
# protection radius. CONFIGURABLE.
BOUNDARY_FENCE_CANOPY_BUFFER_METERS = 5 * METERS_PER_FOOT  # ~1.524m

# Area floor below which a fence-loop fragment produced by find_boundary_
# fencing() (e.g. a sliver where canopy just grazes a boundary corner) is
# dropped rather than returned as a spurious extra segment -- its own
# SEPARATE constant from production_area.MIN_PRODUCTION_AREA_ACRES, even
# though both are "drop tiny slivers" floors: one gates a production
# PATCH (real farmable ground), this one gates a FENCE-LOOP FRAGMENT (a
# closed line, not an area to farm) -- different kinds of sliver, same
# "constants stay separate" convention noted above. CONFIGURABLE.
BOUNDARY_FENCE_MIN_SEGMENT_ACRES = 0.05

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

    Otherwise: fence_polygon = boundary_polygon_utm.difference(canopy_union_utm).
    Only EXTERIOR rings of the result are kept -- a Polygon result
    contributes its one exterior ring, a MultiPolygon result contributes
    one exterior ring per piece. Interior rings (holes) are discarded
    entirely: canopy that sits entirely inside the boundary, never
    touching the edge, carves a hole out of fence_polygon, not a change
    to the fence line itself, so it must be silently ignored rather than
    surfaced as a spurious third fence line.

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

    if fence_polygon_utm.geom_type == "Polygon":
        candidate_polygons = [fence_polygon_utm]
    elif fence_polygon_utm.geom_type == "MultiPolygon":
        candidate_polygons = list(fence_polygon_utm.geoms)
    else:
        # Defensive: a Polygon-minus-Polygon difference shouldn't produce
        # anything else, but guard against a degenerate GeometryCollection
        # (e.g. a zero-area Point/LineString touch) by keeping only real
        # Polygon parts, same tolerance-for-topology-noise reasoning
        # raster_grid.cell_union_footprint()'s own buffer(0) cleanup uses.
        candidate_polygons = [g for g in getattr(fence_polygon_utm, "geoms", []) if g.geom_type == "Polygon"]

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
) -> dict:
    """
    Full pipeline entry point for Subdivision Fences' computed geometry
    (stream exclusion + boundary fencing only -- see module docstring for
    why everything else in that report step is narrative-only, not
    generated here).

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

    Returns:
        {
            'fencing_geojson': FeatureCollection,   # "exclusion_fencing" (stream) + "perimeter_fencing" (boundary) features -- the deliverable
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

    features = (
        stream_exclusion_fencing_to_geojson(stream_entries, stream_exclusion_buffer_meters)["features"]
        + boundary_result["fencing_geojson"]["features"]
    )

    return {"fencing_geojson": make_feature_collection(features), "segment_count": boundary_result["segment_count"]}


def summarize_fencing(result: dict) -> str:
    features = result["fencing_geojson"]["features"]
    stream_count = sum(1 for f in features if f["properties"]["layer"] == "exclusion_fencing")
    boundary_features = [f for f in features if f["properties"]["layer"] == "perimeter_fencing"]
    segment_count = result.get("segment_count", len(boundary_features))

    lines = [
        f"Computed fencing features: {stream_count} stream exclusion, "
        f"{segment_count} boundary fencing segment(s)"
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

    print("Identifying computed fencing (stream exclusion + canopy-aware boundary fencing) for property boundary...\n")

    try:
        from feature_schema import validate_feature_collection

        dem = get_dem_for_boundary(property_boundary)

        boundary_only = identify_boundary_fencing(property_boundary, dem=dem)
        validate_feature_collection(boundary_only["fencing_geojson"])
        print(f"identify_boundary_fencing(): {boundary_only['segment_count']} segment(s), schema-valid.\n")

        result = identify_fencing(property_boundary, dem=dem)
        validate_feature_collection(result["fencing_geojson"])
        print(summarize_fencing(result))
        print("\nfencing_geojson is schema-valid.")
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map ImageServer/3DEP lidar HAG services — not a fully sandboxed environment."
        )
