"""
solar_suitability.py

Solar suitability data layer for permanent building placement (Scale of
Permanence step 6): ranks candidate zones for solar infrastructure. This
produces RANKED CANDIDATES, not a single placement decision — Claude
narrates the tradeoffs between them in the report (see report_generator.py
step 6). Finding "the one best spot" is explicitly not this module's job.

Three-part constraint stack, computed as real geometry:

    (production zones, buffered + inverted)   -- exclusion: stay off
        ∩ (farm road proximity buffer)         -- proximity: stay reachable
        ∩ (slope + aspect + shading score)      -- suitability: rank what's left

    DEM (dem_data.py, already in main)
        --> slope/aspect/shading (terrain_metrics.py)
        --> production zones (production_area.py, already in main) -- exclusion
        --> farm roads (farm_roads_data.py) -- proximity
        --> [this module] constraint stack + scoring + ranking
        --> ranked candidate zone polygons (layer="solar_infrastructure")

find_candidate_solar_zones() is the geometric/scoring core: it takes an
already-fetched DEM dict, production areas, and road geometries (all in
the DEM's own projected CRS) and does no network I/O itself — same reason
as water_candidate_zones.py's find_candidate_zones(): so the constraint-
stack logic is unit-testable against a synthetic DEM independent of
whether any of the three real data fetches (DEM, roads, SSURGO) are
working. flag_prime_farmland_conflicts() is a second, separate pure
function for exactly the same reason, applied to the SSURGO farmland
lookup specifically.
"""

import math
from typing import Optional

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import LineString, MultiPoint, Point
from shapely.ops import unary_union
from shapely.prepared import prep

from dem_data import get_dem_for_boundary
from farm_roads_data import get_farm_roads_for_boundary
from feature_schema import CONFIDENCE_LOW, make_feature, make_feature_collection
from production_area import identify_production_areas
from raster_grid import cell_area_acres, connected_components, pixel_center_xy
from soil_data import coordinates_to_wkt_polygon, get_farmland_classification_for_polygon, is_prime_farmland
from terrain_metrics import aspect_score, aspect_to_compass_label, compute_shading_score, compute_slope_and_aspect

METERS_PER_FOOT = 0.3048

# Ground-mount solar racking can tolerate more grade than row-crop
# production land (production_area.py's own MAX_PRODUCTION_SLOPE_PCT is
# 15%), but not arbitrarily much — beyond this, site prep (grading,
# custom racking) starts dominating cost. Cells steeper than this are
# hard-excluded, not just scored down. Deliberately higher than
# production's threshold: land too steep to farm but still workable for
# racking is exactly the kind of area this layer should be able to
# surface as a candidate, not one that gets silently swallowed by the
# production-zone exclusion because both thresholds happened to match.
# CONFIGURABLE.
MAX_SOLAR_SLOPE_PCT = 20.0

# Weights for the combined 0-1 suitability score (must sum to 1.0).
# CONFIGURABLE — tune against your own property once real production data
# is available to check the ranking against.
SLOPE_SCORE_WEIGHT = 0.4
ASPECT_SCORE_WEIGHT = 0.3
SHADING_SCORE_WEIGHT = 0.3

# Below this combined score (0-1 scale), a cell isn't worth surfacing as a
# candidate at all, even if it technically clears every hard constraint.
# CONFIGURABLE.
MIN_SUITABILITY_SCORE = 0.4

# How far outside a production zone's own footprint to also exclude —
# the "buffered, inverted" exclusion from the constraint-stack formula.
# Keeps solar infrastructure from crowding right up against the edge of
# workable production land. CONFIGURABLE.
PRODUCTION_ZONE_EXCLUSION_BUFFER_METERS = 15.0

# Candidates must be within this distance of a mapped road to be
# considered reachable/wireable at all — a hard constraint per the
# "∩ road proximity buffer" formula, not just a scoring input.
# CONFIGURABLE.
ROAD_PROXIMITY_BUFFER_METERS = 150.0

# Drop clusters smaller than this — likely noise, not a real usable pad.
# CONFIGURABLE.
MIN_CANDIDATE_AREA_ACRES = 0.25

# How many top-ranked candidates to return. Deliberately more than 1 —
# per this feature's framing, ties/close calls should surface as multiple
# candidates for Claude to compare, not get silently collapsed into one.
# CONFIGURABLE.
MAX_CANDIDATES = 5

SOLAR_CONFIDENCE_NOTES_TEMPLATE = (
    "This identifies a ranked CANDIDATE ZONE for solar infrastructure, not "
    "a final placement decision — see the report's Permanent Buildings "
    "section for tradeoffs against other ranked candidates. Slope and "
    "aspect are computed directly from the DEM (real geometry). Shading is "
    "{shading_caveat} It also inherits the limitations of production_area.py "
    "(a slope-only production-zone heuristic) and farm_roads_data.py "
    "(public road/right-of-way data only — may miss private farm tracks). "
    "{farmland_note}Treat this as a starting shortlist to walk and "
    "ground-truth, not a final site plan."
)

SHADING_CAVEAT_HORIZON_ONLY = (
    "estimated from a DEM-only horizon/terrain-shading proxy (terrain_metrics.py) — "
    "this has no way to see vegetation or tree canopy, since no canopy height model "
    "(DSM) or NDVI data was available/used for this run. A real canopy height model "
    "would be a meaningfully more accurate shading signal than this."
)


def _slope_score(slope_pct: float) -> float:
    return max(0.0, 1.0 - slope_pct / MAX_SOLAR_SLOPE_PCT)


def _circular_mean_aspect_deg(aspect_values_deg: list[float]) -> Optional[float]:
    """Mean compass bearing via vector averaging (a plain arithmetic mean
    of e.g. 350 deg and 10 deg would wrongly give 180 instead of 0).
    Returns None if every input is undefined (an all-flat candidate)."""
    valid = [a for a in aspect_values_deg if not math.isnan(a)]
    if not valid:
        return None
    sin_sum = sum(math.sin(math.radians(a)) for a in valid)
    cos_sum = sum(math.cos(math.radians(a)) for a in valid)
    return math.degrees(math.atan2(sin_sum, cos_sum)) % 360


def find_candidate_solar_zones(
    dem: dict,
    production_areas: list[dict],
    road_geometries_utm: Optional[list[LineString]],
    max_solar_slope_pct: float = MAX_SOLAR_SLOPE_PCT,
    min_suitability_score: float = MIN_SUITABILITY_SCORE,
    production_zone_exclusion_buffer_meters: float = PRODUCTION_ZONE_EXCLUSION_BUFFER_METERS,
    road_proximity_buffer_meters: float = ROAD_PROXIMITY_BUFFER_METERS,
    min_candidate_area_acres: float = MIN_CANDIDATE_AREA_ACRES,
    max_candidates: int = MAX_CANDIDATES,
) -> list[dict]:
    """
    Pure constraint-stack + scoring logic — see module docstring for why
    this takes already-computed inputs rather than fetching anything.

    road_geometries_utm=None means "road data unavailable" (the fetch
    itself failed) and disables the road-proximity constraint entirely
    (with that noted by the caller); an empty list [] means "fetched
    successfully, no roads found nearby" and is treated as a real,
    binding constraint (nothing will qualify) — see module docstring.

    Returns up to max_candidates entries, ranked best-first:
        {
            'rank': int,
            'suitability_score': float,        # 0-100
            'avg_slope_pct': float,
            'aspect_deg': Optional[float],      # None if the candidate is essentially flat
            'aspect_label': str,
            'distance_to_road_m': Optional[float],
            'distance_to_production_zone_m': Optional[float],
            'polygon_utm': shapely Polygon,
            'geometry_wgs84': GeoJSON geometry dict,
        }
    """
    array = dem["array"]
    resolution = dem["resolution_meters"]
    valid = ~np.isnan(array)

    slope_pct, aspect_deg = compute_slope_and_aspect(array, resolution)
    shading = compute_shading_score(array, resolution)

    rows, cols = array.shape
    suitability = np.full((rows, cols), np.nan, dtype=np.float32)
    eligible = np.zeros((rows, cols), dtype=bool)

    excluded_union = None
    if production_areas:
        excluded_union = unary_union(
            [p["polygon_utm"].buffer(production_zone_exclusion_buffer_meters) for p in production_areas]
        )
        excluded_prepared = prep(excluded_union)
    else:
        excluded_prepared = None

    road_union = unary_union(road_geometries_utm) if road_geometries_utm else None
    apply_road_constraint = road_geometries_utm is not None  # None = data unavailable, don't apply

    for r in range(rows):
        for c in range(cols):
            if not valid[r, c] or np.isnan(slope_pct[r, c]):
                continue
            if slope_pct[r, c] > max_solar_slope_pct:
                continue

            x, y = pixel_center_xy(dem, r, c)
            point = Point(x, y)

            if excluded_prepared is not None and excluded_prepared.contains(point):
                continue

            if apply_road_constraint:
                if road_union is None or point.distance(road_union) > road_proximity_buffer_meters:
                    continue

            a_score = aspect_score(float(aspect_deg[r, c]))
            s_score = _slope_score(float(slope_pct[r, c]))
            sh_score = float(shading[r, c]) if not math.isnan(shading[r, c]) else 0.5

            combined = (
                SLOPE_SCORE_WEIGHT * s_score
                + ASPECT_SCORE_WEIGHT * a_score
                + SHADING_SCORE_WEIGHT * sh_score
            )
            suitability[r, c] = combined
            if combined >= min_suitability_score:
                eligible[r, c] = True

    labels, num_components = connected_components(eligible)
    area_per_cell = cell_area_acres(dem)

    candidates = []
    for component_id in range(num_components):
        cells = [(int(r), int(c)) for r, c in np.argwhere(labels == component_id)]
        area_acres = len(cells) * area_per_cell
        if area_acres < min_candidate_area_acres:
            continue

        cell_slopes = [float(slope_pct[r, c]) for r, c in cells]
        cell_aspects = [float(aspect_deg[r, c]) for r, c in cells]
        cell_scores = [float(suitability[r, c]) for r, c in cells]

        utm_points = [pixel_center_xy(dem, r, c) for r, c in cells]
        polygon_utm = MultiPoint(utm_points).convex_hull
        if polygon_utm.geom_type != "Polygon":
            px, py = resolution
            polygon_utm = polygon_utm.buffer(max(px, py) / 2)

        centroid = polygon_utm.centroid
        distance_to_production_zone_m = (
            centroid.distance(excluded_union) if excluded_union is not None else None
        )
        distance_to_road_m = centroid.distance(road_union) if road_union is not None else None

        mean_aspect = _circular_mean_aspect_deg(cell_aspects)

        xs, ys = zip(*polygon_utm.exterior.coords)
        lons, lats = warp_transform(dem["crs"], "EPSG:4326", list(xs), list(ys))
        geometry_wgs84 = {"type": "Polygon", "coordinates": [list(zip(lons, lats))]}

        candidates.append(
            {
                "suitability_score": round(float(np.mean(cell_scores)) * 100, 1),
                "avg_slope_pct": round(float(np.mean(cell_slopes)), 1),
                "aspect_deg": round(mean_aspect, 1) if mean_aspect is not None else None,
                "aspect_label": aspect_to_compass_label(mean_aspect) if mean_aspect is not None else "flat",
                "distance_to_road_m": (
                    round(float(distance_to_road_m), 1) if distance_to_road_m is not None else None
                ),
                "distance_to_production_zone_m": (
                    round(float(distance_to_production_zone_m), 1)
                    if distance_to_production_zone_m is not None
                    else None
                ),
                "polygon_utm": polygon_utm,
                "geometry_wgs84": geometry_wgs84,
            }
        )

    candidates.sort(key=lambda cand: -cand["suitability_score"])
    candidates = candidates[:max_candidates]
    for rank, candidate in enumerate(candidates, start=1):
        candidate["rank"] = rank

    return candidates


def flag_prime_farmland_conflicts(
    candidates: list[dict], farmland_classifications: list[dict]
) -> list[dict]:
    """
    Pure post-processing step (Step 3): checks each candidate's polygon
    against SSURGO farmland classification and adds
    'prime_farmland_conflict' (bool) and 'prime_farmland_note' (str) to
    each candidate dict, in place, and returns it. Does NOT exclude or
    re-rank anything — a technically great solar site on prime farmland is
    still flagged, not dropped, per the Scale of Permanence tension
    between competing land uses this feature is explicitly meant to
    surface, not resolve.

    farmland_classifications is soil_data.get_farmland_classification_for_
    polygon()'s output — this function takes it pre-fetched (no network
    here) so it's unit-testable with a synthetic list.

    This function only ever sets prime_farmland_conflict based on whether
    ANY prime-farmland map unit was found intersecting the boundary this
    classification data was fetched for — it doesn't have per-candidate
    soil geometry to check individually (SSURGO map unit polygons, not
    just their farmland-class attribute, would be needed for that), so a
    "yes" here means "somewhere in the area this data covers," not
    necessarily "exactly under this polygon." That's stated in the note
    added to each flagged candidate, not left implicit.
    """
    any_prime = any(is_prime_farmland(c.get("farmland_classification")) for c in farmland_classifications)

    for candidate in candidates:
        candidate["prime_farmland_conflict"] = any_prime
        if any_prime:
            candidate["prime_farmland_note"] = (
                "Prime (or conditionally prime) farmland soil was found in this area per "
                "SSURGO — this candidate may sit on or near land better suited to production "
                "than solar infrastructure. This is a tradeoff to weigh, not an exclusion."
            )
        else:
            candidate["prime_farmland_note"] = "No prime farmland classification found in this area per SSURGO."

    return candidates


def candidates_to_geojson(candidates: list[dict], shading_is_rough_proxy: bool = True) -> dict:
    """Wraps find_candidate_solar_zones() (+ optionally
    flag_prime_farmland_conflicts()) output as the schema-conformant
    GeoJSON FeatureCollection this feature delivers
    (layer="solar_infrastructure")."""
    farmland_note = ""
    if candidates and "prime_farmland_conflict" in candidates[0]:
        farmland_note = (
            "SSURGO prime-farmland overlap was checked and is reported per-candidate "
            "in properties.prime_farmland_conflict — see properties.prime_farmland_note. "
        )

    confidence_notes = SOLAR_CONFIDENCE_NOTES_TEMPLATE.format(
        shading_caveat=SHADING_CAVEAT_HORIZON_ONLY if shading_is_rough_proxy else "computed from a real canopy height model (DSM-derived), not a rough proxy.",
        farmland_note=farmland_note,
    )

    features = []
    for candidate in candidates:
        constraints_satisfied = ["outside_production_zone", f"suitability_score>={MIN_SUITABILITY_SCORE * 100:.0f}"]
        if candidate.get("distance_to_road_m") is not None:
            constraints_satisfied.append("within_road_proximity_buffer")

        distance_to_road_ft = (
            round(candidate["distance_to_road_m"] / METERS_PER_FOOT, 1)
            if candidate.get("distance_to_road_m") is not None
            else None
        )
        distance_to_production_zone_ft = (
            round(candidate["distance_to_production_zone_m"] / METERS_PER_FOOT, 1)
            if candidate.get("distance_to_production_zone_m") is not None
            else None
        )

        extra_properties = {
            "rank": candidate["rank"],
            "suitability_score": candidate["suitability_score"],
            "avg_slope_pct": candidate["avg_slope_pct"],
            "aspect": candidate["aspect_label"],
            "aspect_degrees": candidate["aspect_deg"],
            "distance_to_road_ft": distance_to_road_ft,
            "distance_to_production_zone_ft": distance_to_production_zone_ft,
            "constraints_satisfied": constraints_satisfied,
        }
        if "prime_farmland_conflict" in candidate:
            extra_properties["prime_farmland_conflict"] = candidate["prime_farmland_conflict"]
            extra_properties["prime_farmland_note"] = candidate["prime_farmland_note"]

        # Confidence reflects geometric/data-quality reliability (this
        # layer stacks a slope-only production heuristic, a DEM-only
        # shading proxy, and public-only road data), NOT site
        # desirability — a prime-farmland conflict doesn't make the
        # geometry itself any less trustworthy, so it's surfaced only via
        # prime_farmland_conflict/prime_farmland_note above, not folded
        # into confidence.
        features.append(
            make_feature(
                feature_id=f"solar-candidate-{candidate['rank']}",
                geometry=candidate["geometry_wgs84"],
                layer="solar_infrastructure",
                label=f"Solar candidate zone (rank {candidate['rank']})",
                confidence=CONFIDENCE_LOW,
                confidence_notes=confidence_notes,
                extra_properties=extra_properties,
            )
        )

    return make_feature_collection(features)


def identify_solar_candidate_zones(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    check_prime_farmland: bool = True,
    **zone_kwargs,
) -> dict:
    """
    Full pipeline entry point: fetches the DEM (unless one is passed in),
    production areas, and farm roads; runs the constraint stack; checks
    the SSURGO prime-farmland conflict; and returns the
    "solar_infrastructure" GeoJSON FeatureCollection.

    Each real-data fetch degrades independently and gracefully — a farm
    roads or SSURGO outage shouldn't block solar candidates from being
    identified at all, same reasoning as every other optional layer in
    this pipeline (imagery, water candidate zones' own DEM fetch, etc.).
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    production_areas = identify_production_areas(dem)

    try:
        roads = get_farm_roads_for_boundary(boundary_coordinates)
        road_lines_wgs84 = [g["geometry"] for g in roads]
    except Exception:
        road_lines_wgs84 = None  # fetch failed -- disables the road constraint, see find_candidate_solar_zones

    road_geometries_utm = None
    if road_lines_wgs84 is not None:
        road_geometries_utm = []
        for geometry in road_lines_wgs84:
            coords = geometry["coordinates"]
            line_lists = coords if geometry["type"] == "MultiLineString" else [coords]
            for line in line_lists:
                xs, ys = warp_transform("EPSG:4326", dem["crs"], [p[0] for p in line], [p[1] for p in line])
                road_geometries_utm.append(LineString(zip(xs, ys)))

    candidates = find_candidate_solar_zones(dem, production_areas, road_geometries_utm, **zone_kwargs)

    if check_prime_farmland and candidates:
        try:
            wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
            farmland_classifications = get_farmland_classification_for_polygon(wkt_polygon)
            candidates = flag_prime_farmland_conflicts(candidates, farmland_classifications)
        except Exception:
            pass  # SSURGO outage -- candidates just won't carry a prime_farmland_conflict flag this run

    return {"zones_geojson": candidates_to_geojson(candidates)}


def summarize_solar_candidate_zones(result: dict) -> str:
    features = result["zones_geojson"]["features"]
    if not features:
        return "No solar candidate zones identified (nothing cleared the constraint stack)."

    lines = [f"Solar candidate zones: {len(features)}"]
    for feature in features:
        props = feature["properties"]
        conflict = " [prime farmland conflict]" if props.get("prime_farmland_conflict") else ""
        lines.append(
            f"  - Rank {props['rank']}: score {props['suitability_score']}/100, "
            f"{props['avg_slope_pct']}% slope, {props['aspect']}-facing, "
            f"{props['distance_to_road_ft']}ft to road{conflict}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    print("Identifying solar candidate zones for property boundary...\n")

    try:
        result = identify_solar_candidate_zones(property_boundary)
        print(summarize_solar_candidate_zones(result))
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services and USDA's Soil Data Access — not a fully "
            "sandboxed environment."
        )
