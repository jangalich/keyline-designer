"""
water_candidate_zones.py

Step 3 of water-system candidate-zone identification: a purely cell-based
eligibility mask + real cell-union footprint, mirroring the pattern
production_area.py's own pipeline uses (eligibility mask -> connected
components -> per-cell-square union geometry, not a smoothed buffer or
convex hull) -- see compute_water_eligible_cells()'s docstring.

    DEM (dem_data.py)
        --> raw flow-accumulation grid (valley_delineation.
            get_flow_accumulation_for_dem() -- the same grid
            delineate_valleys() thresholds/traces internally)
        --> production areas (production_area.py)
        --> [this module] per-DEM-cell eligibility mask (contributing
            area + service distance + boundary setback)
        --> connected components -> cell-union footprint per cluster
        --> candidate-zone polygons, one per qualifying cluster

This REPLACES the earlier per-traced-valley-branch line-walk entirely:
there is no valley/branch identity carried into a zone anymore. A zone is
now just "a connected cluster of individually-eligible DEM cells" --
exactly the same "cluster's own connectivity defines it" logic
production_area.py's clusters already use, just applied to a different
per-cell eligibility test. Finding one "best" pond/dam site within that
zone is explicitly out of scope here (see the confidence_notes on the
output feature) -- that's future, separate, more detailed work (storage
volume, dam wall geometry).

Elevation relative to the production area(s) a zone could serve is NOT a
generation-time exclusion here -- it used to be (a hard "must clear
MIN_GRAVITY_GRADIENT" gate), but that discarded genuinely well-suited
water-system ground before scoring ever got to weigh it: a site that's
otherwise excellent but sits below its nearest production area (requiring
a pump) is a real, valid candidate -- a pump is a cost/maintenance
tradeoff, not a disqualification. This module instead computes and
attaches the raw elevation-differential/gradient data for every candidate
zone's relationship to each production area it could plausibly serve
(see production_area_relationships below), and leaves turning that into a
"gravity is preferred" SCORE to water_suitability.py -- the same
gate-to-preference move production_suitability.py already made for soil
(see that module's own docstring) and solar_suitability.py already made
for production-zone proximity.

find_candidate_zones() below is deliberately a pure function over an
already-fetched dem plus already-computed production_areas/boundary -- no
DEM fetch, no network. That split is what makes Stage 2 ("is the
zone-filtering logic correct") testable independently of Stage 1 ("is the
DEM/valley delineation accurate") -- see test_water_candidate_zones.py,
and the module docstrings on dem_data.py/valley_delineation.py/
production_area.py for the same reasoning applied to the layers underneath
this one.
"""

import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import Point, Polygon, mapping
from shapely.prepared import prep

from dem_data import get_dem_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from production_area import identify_production_areas, production_areas_to_geojson
from raster_grid import (
    SQUARE_METERS_PER_ACRE,
    binary_dilate,
    cell_area_acres,
    cell_union_footprint,
    connected_components,
    pixel_center_xy,
)
from valley_delineation import delineate_valleys, get_flow_accumulation_for_dem, valleys_to_geojson

# Zones within this distance of the property boundary are excluded even
# if geometrically valid — too close to the property line to realistically
# develop (access, neighbor impact, and likely setback/easement rules this
# pipeline has no data on). CONFIGURABLE.
MIN_BOUNDARY_SETBACK_METERS = 15.0

# How far downhill a candidate cell's elevation advantage is considered
# relevant to a given production-area patch at all. Beyond this, even a
# technically-qualifying gradient isn't a plausible single contour-channel
# run. CONFIGURABLE.
MAX_SERVICE_DISTANCE_METERS = 800.0

# Guards against a candidate cell sitting immediately adjacent to (but
# genuinely OUTSIDE) a production-area patch, where "above by X% grade
# over Y meters" no longer means anything (Y too small to be meaningful).
# Deliberately NOT applied to a cell already INSIDE/touching a patch
# (distance == 0 — see compute_water_eligible_cells()): that guard is
# about rejecting a near-but-separate siting as too close for the
# distance math to mean anything, not about rejecting siting inside the
# production area at all. That distinction is real, not academic — a
# single production-area patch can legitimately cover most of a parcel
# (production_area.py's own slope threshold, confirmed live: ~95% of one
# real reference property), and a strict "distance < 10m is always too
# close" reading would then reject nearly every candidate cell on that
# property outright, since almost everywhere on it genuinely IS inside
# that one patch. Same "gate becomes a genuinely-inapplicable rule at this
# property's real scale, fix it, don't just re-tune the number" pattern as
# road_corridors.py's/production_suitability.py's own earlier softened-
# exclusion fixes documented in README.md. CONFIGURABLE.
MIN_SERVICE_DISTANCE_METERS = 10.0

# Minimum upstream contributing area for a DEM cell to count as sitting on
# a genuine drainage feature at all, replacing the old "is this cell near
# a traced valley branch LINE" test. Mirrors valley_delineation.py's own
# MIN_STREAM_CONTRIBUTING_AREA_ACRES stream threshold (same reasoning:
# concentrated flow, not diffuse sheet flow off a slope) but kept as a
# separate, independently-tunable constant since this module's use case
# (water-system siting) doesn't have to move in lockstep with
# valley_delineation.py's own general-purpose valley threshold.
#
# TUNED live against the real reference property via
# diagnose_water_zone_mask.py's threshold sweep (0.5/1.0/2.0/2.5/3.0
# acres, all at the 10m buffer below): pre-dilation connected-component
# count dropped from 20 at the old 0.5-acre default (scattered terrain
# noise, not real channels) down to 4, stabilizing at 3.0 acres -- 2.5
# acres still let one pair of components merge, 3.0 acres reports the
# same 4 components both before AND after dilation (no further merging).
# 4 also matches the original, pre-rearchitecture line-based pipeline's
# own zone count on this property, as an independent sanity check.
# CONFIGURABLE — re-tune with diagnose_water_zone_mask.py against your
# own property.
MIN_VALLEY_CONTRIBUTING_AREA_ACRES = 3.0

# Drop tiny, noise-sized eligible-cell clusters below this real cell-union
# footprint area. A small first-pass default, deliberately NOT yet
# validated against a real property the way production_area.py's own
# MIN_PRODUCTION_AREA_ACRES has been — tune once ground-truthed.
# CONFIGURABLE.
MIN_WATER_ZONE_AREA_ACRES = 0.1

# The raw flow-accumulation-qualifying mask is only ever one cell wide
# along the exact drainage path (a single line of cells clearing
# MIN_VALLEY_CONTRIBUTING_AREA_ACRES) -- confirmed live: without widening
# it, real zones came back as thin, one-cell-wide traces rather than a
# surveyable area, and most separate drainage segments never cleared
# MIN_WATER_ZONE_AREA_ACRES at all. This dilates the drainage-only mask by
# this many meters (converted to a cell radius, see
# _survey_buffer_radius_cells()) BEFORE the service-distance/on-parcel/
# boundary-setback tests run, so a genuinely qualifying drainage cell
# reads as a walkable-width band, not a hairline.
#
# TUNED live against the real reference property alongside
# MIN_VALLEY_CONTRIBUTING_AREA_ACRES above via diagnose_water_zone_mask.py:
# at 10m, the 3.0-acre contributing-area threshold's 4 connected
# components stay 4 components after dilation too -- no extra merging
# from widening at this buffer size. The original 20.0m value (reused
# from the pre-rearchitecture ZONE_BUFFER_METERS line-buffer half-width)
# was tuned before the contributing-area threshold itself was fixed, and
# turned out wider than this property's real, separate drainage segments
# needed once 3.0 acres stopped conflating them. CONFIGURABLE -- re-tune
# with diagnose_water_zone_mask.py against your own property.
WATER_ZONE_SURVEY_BUFFER_METERS = 10.0

WATER_SYSTEM_CANDIDATE_CONFIDENCE_NOTES = (
    "This identifies a general candidate zone for water-system "
    "infrastructure (keyline plowing patterns, pond/dam potential, ram "
    "pump routing) — a connected cluster of DEM cells, each individually "
    "on a genuine drainage feature and within plausible service distance "
    "of a candidate production area, outside the boundary setback. "
    "Elevation relative to that production area is NOT a generation-time "
    "filter here: a candidate sitting BELOW its nearest production area "
    "(which would need a pump to deliver water uphill) is still reported, "
    "same as one sitting comfortably above it (which could gravity-feed) "
    "— see properties.production_area_relationships for the real "
    "elevation differential/gradient this candidate was measured against, "
    "and water_suitability.py for how that's turned into a real, weighted "
    "preference score rather than a pass/fail gate. This is NOT a "
    "specific pond or dam site: actual siting requires separate, more "
    "detailed analysis (storage volume, dam wall geometry, spillway "
    "design) not covered here. It also inherits the limitations of the "
    "layers it's built on — DEM-derived flow accumulation and a "
    "slope-only production-area heuristic — so treat this as a starting "
    "area to walk and ground-truth, not a final answer."
)


def _survey_buffer_radius_cells(dem: dict, buffer_meters: float) -> int:
    """
    Converts WATER_ZONE_SURVEY_BUFFER_METERS (a real-world distance) into a
    cell-count dilation radius using the DEM's own resolution_meters --
    same meters-to-cell-units conversion pattern production_area.py's own
    _waist_erosion_radius_cells() already uses (average of the two axis
    resolutions, in case they ever differ). Unlike that function, this is
    a direct radius (not a width being halved into one), so it's rounded
    UP (via ceil) with no further halving -- the buffer is never narrower
    than requested. buffer_meters <= 0 correctly yields 0 (no dilation at
    all), since ceil(0 / cell_size) == 0.
    """
    px, py = dem["resolution_meters"]
    cell_size = (px + py) / 2.0
    return math.ceil(buffer_meters / cell_size)


def compute_water_eligible_cells(
    dem: dict,
    production_areas: list[dict],
    boundary_polygon_utm: Polygon,
    min_valley_contributing_area_acres: float = MIN_VALLEY_CONTRIBUTING_AREA_ACRES,
    max_service_distance_meters: float = MAX_SERVICE_DISTANCE_METERS,
    min_service_distance_meters: float = MIN_SERVICE_DISTANCE_METERS,
    min_boundary_setback_meters: float = MIN_BOUNDARY_SETBACK_METERS,
    survey_buffer_meters: float = WATER_ZONE_SURVEY_BUFFER_METERS,
) -> tuple[np.ndarray, dict[tuple[int, int], dict]]:
    """
    Cell-based STEP 1/2: computes the raw flow-accumulation grid directly
    from `dem` (valley_delineation.get_flow_accumulation_for_dem() — the
    same contributing-cell-count grid delineate_valleys() thresholds/
    traces internally, recomputed here rather than reusing a traced
    branch) and gates each cell on THREE independent checks, ALL of which
    must pass for a cell to be eligible:

      1. Contributing area at that cell — converted from the raw
         cell-count grid to acres via cell_area_acres(dem), since
         get_flow_accumulation_for_dem() returns a cell-count grid, not an
         area — meets min_valley_contributing_area_acres. This replaces
         the old "is this cell near a traced valley branch LINE" test
         with "is this cell genuinely part of a drainage feature," with
         no valley/branch identity involved at all: a cell qualifies (or
         doesn't) purely on its own local flow accumulation.

         This raw per-cell test only ever qualifies a thin, one-cell-wide
         trace along the exact drainage path -- before checks 2/3 below
         run at all, this drainage-only mask is WIDENED by dilating it
         (raster_grid.binary_dilate()) by survey_buffer_meters (converted
         to a cell radius via _survey_buffer_radius_cells()), so a real
         zone reads as a walkable-width band, not a hairline. Dilation
         happens on this drainage-only mask specifically, NOT on the
         final combined eligible_mask below -- dilating the final mask
         would let a cell that fails the service-distance/setback tests
         qualify just by sitting next to one that passes, which isn't the
         intent; every dilated drainage cell must still independently
         clear checks 2/3 on its own.

      2. Within max_service_distance_meters of at least one production
         area's polygon_utm, and NOT within min_service_distance_meters of
         it UNLESS the cell is already inside/touching that patch
         (distance == 0). Real bug, found live and fixed for the old
         per-branch-point version of this same check: with a single
         production-area patch covering ~95% of a real reference
         property, "distance < min_service_distance is too close" rejected
         every point on that property outright, since a point genuinely
         inside a patch that large has nowhere else to be relative to it.
         min_service_distance_meters exists to reject a near-but-SEPARATE
         siting (where "above by X% grade over Y meters" stops meaning
         anything for Y too small) — it was never meant to reject siting
         INSIDE the production area entirely, and shouldn't, per this
         whole feature's "elevation/proximity is a preference, not a
         gate" direction (see module docstring).

      3. On-parcel (boundary_polygon_utm.contains(cell center)) AND at
         least min_boundary_setback_meters from boundary_polygon_utm's own
         boundary.

    Elevation/gradient is deliberately NOT a gate here (see module
    docstring's "gravity is a preference, not a gate" framing) — do not
    add a min-gradient or elevation-band exclusion; a cell otherwise
    eligible is never excluded for sitting below its best-matching
    production area.

    While gating, each eligible cell is also tagged with its own best
    (most gravity-favorable) production-area relationship — "best" = the
    largest elevation_differential_m among production areas within the
    service-distance window, whether or not it's actually above the
    production area. This is a real measurement, not a second
    qualification check: no minimum gradient is required to be tagged.

    Returns (eligible_mask, cell_relationships):
        eligible_mask: np.ndarray[bool], same shape as dem['array'].
        cell_relationships: dict mapping each eligible cell's (row, col)
            to its tagged relationship
            ({'id', 'elevation_differential_m', 'distance_m'}), so
            find_candidate_zones() can aggregate it per cluster without
            recomputing anything after connected-component labeling.
    """
    flow_accumulation_cells = get_flow_accumulation_for_dem(dem)
    area_per_cell = cell_area_acres(dem)
    min_contributing_cells = min_valley_contributing_area_acres / area_per_cell
    valley_mask = flow_accumulation_cells >= min_contributing_cells

    survey_buffer_radius_cells = _survey_buffer_radius_cells(dem, survey_buffer_meters)
    valley_mask = binary_dilate(valley_mask, survey_buffer_radius_cells)

    rows, cols = dem["array"].shape
    eligible_mask = np.zeros((rows, cols), dtype=bool)
    cell_relationships: dict[tuple[int, int], dict] = {}

    boundary_prepared = prep(boundary_polygon_utm)
    boundary_line = boundary_polygon_utm.boundary
    array = dem["array"]

    for r, c in np.argwhere(valley_mask):
        r, c = int(r), int(c)
        elevation = float(array[r, c])
        if np.isnan(elevation):
            continue

        x, y = pixel_center_xy(dem, r, c)
        point = Point(x, y)

        if not boundary_prepared.contains(point):
            continue
        if point.distance(boundary_line) < min_boundary_setback_meters:
            continue

        best = None
        for patch in production_areas:
            distance = point.distance(patch["polygon_utm"])
            if distance > max_service_distance_meters:
                continue
            if 0 < distance < min_service_distance_meters:
                continue
            elevation_differential_m = elevation - patch["representative_elevation_m"]
            if best is None or elevation_differential_m > best["elevation_differential_m"]:
                best = {
                    "id": patch["id"],
                    "elevation_differential_m": elevation_differential_m,
                    "distance_m": distance,
                }

        if best is None:
            continue

        eligible_mask[r, c] = True
        cell_relationships[(r, c)] = best

    return eligible_mask, cell_relationships


def _aggregate_production_area_relationships(relationships: list[dict]) -> list[dict]:
    """
    Rolls up the per-cell elevation relationships collected across every
    cell contributing to one zone's cluster into one entry per served
    production area — the MEDIAN elevation differential/distance across
    every cell that picked that production area as its best match
    (median, not mean, for the same "resist a single outlier cell skewing
    the reported number" reasoning as production_area.py's own
    representative-elevation choice).

    Returns a list of:
        {
            'production_area_id': int,
            'elevation_differential_m': float,  # + = zone sits above the
                                                  # production area
                                                  # (gravity-favorable);
                                                  # - = below (would need a
                                                  # pump)
            'distance_m': float,
            'gradient_pct': float,               # elevation_differential_m
                                                  # / distance_m * 100 —
                                                  # can be negative
            'above_production_area': bool,
        }
    sorted by elevation_differential_m descending (most gravity-favorable
    first), so callers that just want "the best one" can take index 0.
    """
    points_by_id: dict = {}
    for r in relationships:
        points_by_id.setdefault(r["id"], []).append(r)

    aggregated = []
    for production_area_id, points in points_by_id.items():
        differential = float(np.median([p["elevation_differential_m"] for p in points]))
        distance = float(np.median([p["distance_m"] for p in points]))
        gradient_pct = (differential / distance * 100) if distance > 0 else 0.0
        aggregated.append(
            {
                "production_area_id": production_area_id,
                "elevation_differential_m": round(differential, 2),
                "distance_m": round(distance, 1),
                "gradient_pct": round(gradient_pct, 2),
                "above_production_area": differential > 0,
            }
        )

    aggregated.sort(key=lambda r: -r["elevation_differential_m"])
    return aggregated


def find_candidate_zones(
    dem: dict,
    production_areas: list[dict],
    boundary_polygon_utm: Polygon,
    min_valley_contributing_area_acres: float = MIN_VALLEY_CONTRIBUTING_AREA_ACRES,
    min_boundary_setback_meters: float = MIN_BOUNDARY_SETBACK_METERS,
    max_service_distance_meters: float = MAX_SERVICE_DISTANCE_METERS,
    min_service_distance_meters: float = MIN_SERVICE_DISTANCE_METERS,
    min_water_zone_area_acres: float = MIN_WATER_ZONE_AREA_ACRES,
    survey_buffer_meters: float = WATER_ZONE_SURVEY_BUFFER_METERS,
) -> list[dict]:
    """
    Cell-based zone-filtering logic (Step 3) — see module docstring for
    why this takes the already-fetched `dem` (to derive its own flow-
    accumulation grid directly) plus already-computed production_areas
    rather than a list of pre-traced valley branches, and for why
    elevation/gradient is not one of the filters applied here
    (min_gravity_gradient is not part of this signature at all — it's
    water_suitability.py's scoring concern, not a generation-time
    parameter).

    Builds the per-cell eligibility mask (compute_water_eligible_cells() —
    including its own survey_buffer_meters dilation of the raw drainage-
    only mask, see that function's docstring for why a zone needs to be
    wider than a one-cell-wide drainage trace), clusters it via
    raster_grid.connected_components() — exactly the same
    "cluster's own connectivity defines a zone" pattern
    production_area.py's own patches use, with no valley identity carried
    into this pass at all — and builds each surviving cluster's REAL
    cell-union footprint (raster_grid.cell_union_footprint()), not a hull
    or a line buffer. Clusters below min_water_zone_area_acres (after
    clipping to boundary_polygon_utm) are dropped as noise.

    Returns one entry per qualifying cell cluster:
        {
            'id': int,
            'served_production_area_ids': [int, ...],
            'polygon_utm': shapely Polygon/MultiPolygon,
            'geometry_wgs84': GeoJSON geometry dict,
            'production_area_relationships': [...],   # see
                _aggregate_production_area_relationships()'s docstring —
                one entry per served production area, sorted most-
                gravity-favorable first
            'primary_production_area_relationship': dict,  # same shape as
                one production_area_relationships entry — the single most
                gravity-favorable one, for callers that just want one
                headline number
        }
    'id' is assigned sequentially across the surviving cluster list, same
    convention production_area.py's own patches use — there is no more
    stable "valley identity" to key a zone off of, since a zone's own
    cell-cluster connectivity is what defines it now.
    """
    if not production_areas:
        return []

    eligible_mask, cell_relationships = compute_water_eligible_cells(
        dem,
        production_areas,
        boundary_polygon_utm,
        min_valley_contributing_area_acres,
        max_service_distance_meters,
        min_service_distance_meters,
        min_boundary_setback_meters,
        survey_buffer_meters,
    )

    labels, num_components = connected_components(eligible_mask)

    zones = []
    next_id = 0
    for component_id in range(num_components):
        cluster_mask = labels == component_id
        cluster_cells = [(int(r), int(c)) for r, c in np.argwhere(cluster_mask)]
        if not cluster_cells:
            continue

        footprint = cell_union_footprint(dem, cluster_mask)
        polygon_utm = footprint.intersection(boundary_polygon_utm)
        if polygon_utm.is_empty:
            continue

        area_acres = polygon_utm.area / SQUARE_METERS_PER_ACRE
        if area_acres < min_water_zone_area_acres:
            continue

        relationships = [cell_relationships[cell] for cell in cluster_cells]
        production_area_relationships = _aggregate_production_area_relationships(relationships)

        geometry_wgs84 = transform_geom(dem["crs"], "EPSG:4326", mapping(polygon_utm))

        zones.append(
            {
                "id": next_id,
                "served_production_area_ids": sorted(
                    r["production_area_id"] for r in production_area_relationships
                ),
                "polygon_utm": polygon_utm,
                "geometry_wgs84": geometry_wgs84,
                "production_area_relationships": production_area_relationships,
                "primary_production_area_relationship": production_area_relationships[0],
            }
        )
        next_id += 1

    return zones


def zones_to_geojson(zones: list[dict]) -> dict:
    """Wraps find_candidate_zones() output as the schema-conformant
    GeoJSON FeatureCollection this feature actually delivers
    (layer="water_system_candidate"). This is the UNSCORED diagnostic
    layer — confidence stays CONFIDENCE_LOW/flat here, same as
    production_area.py's own production_areas_to_geojson() before
    production_suitability.py enriches it; water_suitability.py is where
    real, differentiated confidence/suitability_score get added, on this
    same layer, following that exact precedent."""
    features = [
        make_feature(
            feature_id=f"water-system-candidate-{z['id']}",
            geometry=z["geometry_wgs84"],
            layer="water_system_candidate",
            label=f"Water system candidate zone {z['id']}",
            confidence=CONFIDENCE_LOW,
            confidence_notes=WATER_SYSTEM_CANDIDATE_CONFIDENCE_NOTES,
            extra_properties={
                "served_production_area_ids": z["served_production_area_ids"],
                "production_area_relationships": z["production_area_relationships"],
                "primary_production_area_relationship": z["primary_production_area_relationship"],
            },
        )
        for z in zones
    ]
    return make_feature_collection(features)


def identify_water_system_candidate_zones(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    **zone_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in —
    e.g. reused from generate_full_report.py already fetching it, or
    supplied directly in a test), identifies production-area candidates,
    and returns:

        {
            'zones_geojson': FeatureCollection,             # layer="water_system_candidate" — the deliverable
            'valleys_geojson': FeatureCollection,            # layer="valley" — diagnostic (Stage 1)
            'production_areas_geojson': FeatureCollection,   # layer="production_area_candidate" — diagnostic
        }

    valleys_geojson is still produced via valley_delineation.
    delineate_valleys() purely as diagnostic output (unchanged, own
    traced-branch geometry, own thresholds) — useful for inspecting "did
    we find the right valleys" independently of "is the zone logic
    right," per this feature's stated debugging goal — but
    find_candidate_zones() itself no longer consumes delineate_valleys()'s
    traced branches at all; it derives its own flow-accumulation grid
    directly from `dem` (see find_candidate_zones()'s own docstring).
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

    valleys = delineate_valleys(dem)
    production_areas = identify_production_areas(dem, boundary_polygon_utm)

    zones = find_candidate_zones(dem, production_areas, boundary_polygon_utm, **zone_kwargs)

    return {
        "zones_geojson": zones_to_geojson(zones),
        "valleys_geojson": valleys_to_geojson(valleys),
        "production_areas_geojson": production_areas_to_geojson(production_areas),
    }


def summarize_water_system_candidate_zones(result: dict) -> str:
    zone_count = len(result["zones_geojson"]["features"])
    valley_count = len(result["valleys_geojson"]["features"])
    production_area_count = len(result["production_areas_geojson"]["features"])

    if zone_count == 0:
        return (
            f"{valley_count} primary valley(s) and {production_area_count} "
            "production-area candidate(s) found, but no drainage cell falls "
            "within the service-distance/boundary-setback thresholds — no "
            "water system candidate zones identified."
        )

    return (
        f"Water system candidate zones: {zone_count} "
        f"(from {valley_count} primary valley(s) and "
        f"{production_area_count} production-area candidate(s))"
    )


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

    print("Identifying water system candidate zones for property boundary...\n")

    try:
        result = identify_water_system_candidate_zones(property_boundary)
        print(summarize_water_system_candidate_zones(result))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map ImageServer — not a fully sandboxed environment."
        )
