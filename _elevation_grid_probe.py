"""
_elevation_grid_probe.py

A LIVE PROBE, NOT A REGRESSION TEST. Fetches a 6x6 lattice of USGS EPQS
point elevations for one real, user-drawn property boundary (from
geojson.io) and prints the summary:

    python3 _elevation_grid_probe.py

This used to be test_elevation_grid.py, and it sat in the regression set
while being everything a regression test is not: 36 sequential network
requests to a service the offline suite cannot reach (20 s of failing
quietly, every run), no assertion, and a module elevation_data.py's own
docstring marks as demoted from the pipeline. Renamed with the same
leading-underscore convention as _canopy_override_probe.py so
run_tests.py's test_*.py discovery leaves it alone. Run it by hand when
the question is the EPQS service itself.
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
