"""
test_elevation_grid.py

Quick test of elevation_data.py's grid function using a real, user-drawn
property boundary (from geojson.io).

This is a throwaway test script, not part of the core pipeline — once the
real frontend exists, boundaries will come in through that instead of
being pasted in here by hand.
"""

from elevation_data import get_elevation_grid, summarize_elevation_grid

# Boundary coordinates as (longitude, latitude) tuples — this is the same
# order GeoJSON uses, so they're pasted straight from geojson.io with no
# reordering needed.
property_boundary = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

print("Fetching elevation grid for your property boundary...")
print("(this makes multiple API calls, so it'll take a few seconds)\n")

grid = get_elevation_grid(property_boundary, grid_size=6)

print(summarize_elevation_grid(grid))
print("\nSample points:")
for point in grid[:5]:
    print(
        f"  ({point['latitude']:.5f}, {point['longitude']:.5f}) -> "
        f"{point['elevation']:.1f}m"
    )
