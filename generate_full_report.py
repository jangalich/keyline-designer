"""
generate_full_report.py

The real end-to-end pipeline: give it a property boundary once, and it
runs soil, elevation, and hydrology data fetching, then generates a full
Scale of Permanence report — no manual copy-pasting between scripts.

    boundary --> soil_data (polygon)
             --> elevation_data (grid)
             --> hydrology_data
             --> report_generator
             --> printed report

Requires ANTHROPIC_API_KEY to be set in your environment (see
report_generator.py for details).
"""

from soil_data import get_soil_data_for_polygon, coordinates_to_wkt_polygon
from elevation_data import get_elevation_grid
from hydrology_data import get_water_features_for_boundary
from report_generator import generate_scale_of_permanence_report


def generate_full_report(boundary_coordinates: list) -> str:
    """
    Runs the full pipeline for a given property boundary (list of
    (longitude, latitude) tuples) and returns the final narrative report.
    """
    print("Step 1/4: Fetching soil data for the full boundary...")
    wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
    soil_components = get_soil_data_for_polygon(wkt_polygon)
    print(f"  Found {len(soil_components)} soil component(s).\n")

    print("Step 2/4: Fetching elevation grid...")
    elevation_grid = get_elevation_grid(boundary_coordinates, grid_size=6)
    print(f"  Sampled {len(elevation_grid)} elevation points.\n")

    print("Step 3/4: Fetching nearby water features...")
    water_features = get_water_features_for_boundary(boundary_coordinates)
    stream_count = len(water_features["streams"])
    waterbody_count = len(water_features["water_bodies"])
    print(f"  Found {stream_count} stream(s), {waterbody_count} water body/bodies.\n")

    print("Step 4/4: Generating Scale of Permanence report via Claude...\n")
    report = generate_scale_of_permanence_report(
        soil_components, elevation_grid, water_features
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
