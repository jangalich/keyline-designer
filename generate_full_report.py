"""
generate_full_report.py

The real end-to-end pipeline: give it a property boundary once, and it
runs soil, elevation, hydrology, climate, and imagery data fetching, then
generates a full Scale of Permanence report — no manual copy-pasting
between scripts.

    boundary --> soil_data (polygon)
             --> elevation_data (grid)
             --> hydrology_data
             --> climate_data
             --> imagery_data (polygon)
             --> water_candidate_zones (DEM/LiDAR valley + gradient/setback zones)
             --> road_corridors (DEM least-cost-path corridors + NHD/SSURGO constraints)
             --> solar_suitability (DEM slope/aspect/shading + road/production constraints,
                                     falling back to road_corridors for proximity if needed)
             --> fencing (real geometry: buffered NHD stream exclusion + property-boundary
                          perimeter; everything else in Subdivision Fences stays narrative-only)
             --> report_generator
             --> printed report

Requires ANTHROPIC_API_KEY to be set in your environment (see
report_generator.py for details).
"""

from soil_data import get_soil_data_for_polygon, coordinates_to_wkt_polygon
from elevation_data import get_elevation_grid
from hydrology_data import get_water_features_for_boundary
from climate_data import get_climate_summary_for_point
from imagery_data import get_imagery_summary_for_boundary
from water_candidate_zones import (
    identify_water_system_candidate_zones,
    summarize_water_system_candidate_zones,
)
from road_corridors import (
    identify_road_corridor_candidates,
    summarize_road_corridor_candidates,
    validate_access_point_on_boundary,
)
from solar_suitability import identify_solar_candidate_zones, summarize_solar_candidate_zones
from fencing import identify_fencing, summarize_fencing
from report_generator import generate_scale_of_permanence_report


def _boundary_center(boundary_coordinates: list) -> tuple:
    """Rough center point of the boundary, used for the climate lookup
    (climate is regional, not parcel-precise, so one representative point
    is the right level of precision here)."""
    lons = [pt[0] for pt in boundary_coordinates]
    lats = [pt[1] for pt in boundary_coordinates]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def generate_full_report(boundary_coordinates: list, anchor_lon_lat: tuple[float, float]) -> str:
    """
    Runs the full pipeline for a given property boundary (list of
    (longitude, latitude) tuples) and returns the final narrative report.

    anchor_lon_lat is the real, user-picked access point (a single
    (lon, lat) pair, same convention as boundary_coordinates) -- required,
    not optional: it's the point road-corridor routing starts from (see
    road_corridors.py's own module docstring), and there's no reasonable
    default to invent for it (a real anchor is a product decision -- where
    does this property actually connect to the outside world -- not
    something derivable from the boundary alone). Validated up front via
    validate_access_point_on_boundary() -- fails loud with a ValueError
    rather than silently accepting a point that isn't actually on this
    property's own boundary.
    """
    validate_access_point_on_boundary(boundary_coordinates, anchor_lon_lat)

    print("Step 1/10: Fetching climate data (prevailing wind, rainfall)...")
    center_lat, center_lon = _boundary_center(boundary_coordinates)
    climate_summary = get_climate_summary_for_point(center_lat, center_lon)
    print(
        f"  Prevailing wind: {climate_summary['prevailing_wind_direction']}, "
        f"avg annual precip: {climate_summary['avg_annual_precipitation_mm']}mm\n"
    )

    print("Step 2/10: Fetching soil data for the full boundary...")
    wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
    soil_components = get_soil_data_for_polygon(wkt_polygon)
    print(f"  Found {len(soil_components)} soil component(s).\n")

    print("Step 3/10: Fetching elevation grid...")
    elevation_grid = get_elevation_grid(boundary_coordinates, grid_size=6)
    print(f"  Sampled {len(elevation_grid)} elevation points.\n")

    print("Step 4/10: Fetching nearby water features...")
    water_features = get_water_features_for_boundary(boundary_coordinates)
    stream_count = len(water_features["streams"])
    waterbody_count = len(water_features["water_bodies"])
    print(f"  Found {stream_count} stream(s), {waterbody_count} water body/bodies.\n")

    print("Step 5/10: Fetching satellite imagery (NDVI land cover)...")
    try:
        imagery_summary = get_imagery_summary_for_boundary(boundary_coordinates)
    except Exception as e:
        # Imagery is a nice-to-have layer on top of soil/elevation/water/
        # climate, not a hard dependency — a Planetary Computer outage or
        # network hiccup shouldn't take down the whole report.
        print(f"  Imagery fetch failed ({e}), continuing without it.\n")
        imagery_summary = None
    if imagery_summary:
        print(
            f"  Scene date: {imagery_summary['scene_date']} "
            f"({imagery_summary['days_since_scene']} days ago), "
            f"cloud cover: {imagery_summary['cloud_cover_pct']}%\n"
        )
    else:
        print("  No recent low-cloud imagery available for this boundary.\n")

    print("Step 6/10: Identifying valley-based water system candidate zones (DEM/LiDAR)...")
    try:
        water_zone_result = identify_water_system_candidate_zones(boundary_coordinates)
        water_candidate_zones_geojson = water_zone_result["zones_geojson"]
    except Exception as e:
        # Same reasoning as imagery above: a USGS 3DEP outage or network
        # hiccup shouldn't take down the whole report — the WATER SUPPLY
        # section just falls back to reasoning from the coarse elevation
        # grid alone, same as it did before this layer existed.
        print(f"  Water system candidate zone identification failed ({e}), continuing without it.\n")
        water_candidate_zones_geojson = None
    if water_candidate_zones_geojson is not None:
        print(f"  {summarize_water_system_candidate_zones(water_zone_result)}\n")

    print("Step 7/10: Identifying suggested road corridor candidates (DEM least-cost-path routing)...")
    try:
        road_corridor_result = identify_road_corridor_candidates(
            boundary_coordinates, anchor_lon_lat=anchor_lon_lat
        )
        road_corridor_candidates_geojson = road_corridor_result["zones_geojson"]
    except Exception as e:
        # Same reasoning as the other DEM/network-backed layers above — an
        # outage here shouldn't take down the whole report; the FARM
        # ROADS section just falls back to its old prose-inference
        # behavior, same as it did before this layer existed.
        print(f"  Road corridor candidate identification failed ({e}), continuing without it.\n")
        road_corridor_candidates_geojson = None
    if road_corridor_candidates_geojson is not None:
        print(f"  {summarize_road_corridor_candidates(road_corridor_result)}\n")

    print("Step 8/10: Identifying solar infrastructure candidate zones (DEM slope/aspect/shading)...")
    try:
        solar_zone_result = identify_solar_candidate_zones(boundary_coordinates, anchor_lon_lat=anchor_lon_lat)
        solar_candidate_zones_geojson = solar_zone_result["zones_geojson"]
    except Exception as e:
        # Same reasoning as imagery/water candidate zones above: a USGS/
        # SSURGO outage or network hiccup shouldn't take down the whole
        # report — the PERMANENT BUILDINGS section's solar siting
        # discussion just falls back to reasoning without ranked
        # candidates, same as it did before this layer existed.
        print(f"  Solar candidate zone identification failed ({e}), continuing without it.\n")
        solar_candidate_zones_geojson = None
    if solar_candidate_zones_geojson is not None:
        print(f"  {summarize_solar_candidate_zones(solar_zone_result)}\n")

    print("Step 9/10: Identifying fencing geometry (stream exclusion + perimeter)...")
    try:
        fencing_result = identify_fencing(boundary_coordinates, anchor_lon_lat=anchor_lon_lat)
        fencing_geojson = fencing_result["fencing_geojson"]
    except Exception as e:
        # Same reasoning as the other DEM/network-backed layers above — an
        # NHD outage here shouldn't take down the whole report; the
        # SUBDIVISION FENCES section just falls back to narrative-only
        # reasoning for stream exclusion/perimeter too, same as it always
        # has for the rest of that section.
        print(f"  Fencing geometry identification failed ({e}), continuing without it.\n")
        fencing_geojson = None
    if fencing_geojson is not None:
        print(f"  {summarize_fencing(fencing_result)}\n")

    print("Step 10/10: Generating Scale of Permanence report via Claude...\n")
    report = generate_scale_of_permanence_report(
        soil_components,
        elevation_grid,
        water_features,
        climate_summary,
        imagery_summary,
        water_candidate_zones_geojson,
        solar_candidate_zones_geojson,
        road_corridor_candidates_geojson,
        fencing_geojson,
    )

    return report


if __name__ == "__main__":
    # The user's real, drawn property boundary
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    # A real point ON this boundary's own edge (the midpoint of its first
    # segment) -- NOT render_layout_map.py's shared reference-property
    # placeholder, which sits ~10m off this exact boundary and would fail
    # validate_access_point_on_boundary()'s own tolerance check now that
    # this entry point enforces it.
    access_point = (-79.98374275, 40.6443462)

    report = generate_full_report(property_boundary, access_point)

    print("=" * 60)
    print(report)
