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
from report_generator import generate_scale_of_permanence_report


def _boundary_center(boundary_coordinates: list) -> tuple:
    """Rough center point of the boundary, used for the climate lookup
    (climate is regional, not parcel-precise, so one representative point
    is the right level of precision here)."""
    lons = [pt[0] for pt in boundary_coordinates]
    lats = [pt[1] for pt in boundary_coordinates]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def generate_full_report(boundary_coordinates: list) -> str:
    """
    Runs the full pipeline for a given property boundary (list of
    (longitude, latitude) tuples) and returns the final narrative report.
    """
    print("Step 1/7: Fetching climate data (prevailing wind, rainfall)...")
    center_lat, center_lon = _boundary_center(boundary_coordinates)
    climate_summary = get_climate_summary_for_point(center_lat, center_lon)
    print(
        f"  Prevailing wind: {climate_summary['prevailing_wind_direction']}, "
        f"avg annual precip: {climate_summary['avg_annual_precipitation_mm']}mm\n"
    )

    print("Step 2/7: Fetching soil data for the full boundary...")
    wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
    soil_components = get_soil_data_for_polygon(wkt_polygon)
    print(f"  Found {len(soil_components)} soil component(s).\n")

    print("Step 3/7: Fetching elevation grid...")
    elevation_grid = get_elevation_grid(boundary_coordinates, grid_size=6)
    print(f"  Sampled {len(elevation_grid)} elevation points.\n")

    print("Step 4/7: Fetching nearby water features...")
    water_features = get_water_features_for_boundary(boundary_coordinates)
    stream_count = len(water_features["streams"])
    waterbody_count = len(water_features["water_bodies"])
    print(f"  Found {stream_count} stream(s), {waterbody_count} water body/bodies.\n")

    print("Step 5/7: Fetching satellite imagery (NDVI land cover)...")
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

    print("Step 6/7: Identifying valley-based water system candidate zones (DEM/LiDAR)...")
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

    print("Step 7/7: Generating Scale of Permanence report via Claude...\n")
    report = generate_scale_of_permanence_report(
        soil_components,
        elevation_grid,
        water_features,
        climate_summary,
        imagery_summary,
        water_candidate_zones_geojson,
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

    report = generate_full_report(property_boundary)

    print("=" * 60)
    print(report)
