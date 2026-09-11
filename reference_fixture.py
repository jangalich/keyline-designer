"""
reference_fixture.py

THE ONE REAL PARCEL EVERY STEP TEST RUNS ON, DEFINED ONCE.

5614 N Montour Rd, Gibsonia, PA -- ~13.2 acres, UTM zone 17N -- the drawn
boundary from generate_full_report.py, which test_session_manager.py,
test_step_orchestrator.py, test_step_commit.py, test_session_api.py and
every step test since (water, roads, trees, structures, fencing, the run
diagnostics) carried as their own verbatim copy, each followed by the
same six lines projecting it to UTM. Ten copies of one fact is nine
chances for it to drift; this module is the fact.

What lives here is only what was byte-identical across those files: the
boundary, its UTM CRS, the projected polygon and its acreage. Each test
file keeps its OWN DEM (bench-and-drainage, V-valley-with-knee, ...),
its own mocked soil/roads/streams and its own Harness, because those
differ on purpose -- a test's terrain is part of what it asserts.

The session build itself (session_manager.create_session() on this
boundary, ~0.5 s of real terrain warm-up) is deliberately NOT cached
here. Tests count the mocked fetches that build makes and assert on the
count; a session cloned from a template would make none, and the caches
behind it hold locks and cannot be copied. That cost is real work the
tests mean to run, not overhead.
"""

from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon

from dem_data import _utm_epsg_for_lonlat
from raster_grid import SQUARE_METERS_PER_ACRE

# (lon, lat) pairs, GeoJSON order, closed ring: the last vertex repeats the
# first to within ~0.9 m, which is the shape a browser map hands back.
REAL_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

_mean_lon = sum(lon for lon, _ in REAL_BOUNDARY) / len(REAL_BOUNDARY)
_mean_lat = sum(lat for _, lat in REAL_BOUNDARY) / len(REAL_BOUNDARY)
CRS = f"EPSG:{_utm_epsg_for_lonlat(_mean_lon, _mean_lat)}"
_xs, _ys = warp_transform(
    "EPSG:4326", CRS, [lon for lon, _ in REAL_BOUNDARY], [lat for _, lat in REAL_BOUNDARY]
)
BOUNDARY_POLYGON_UTM = Polygon(zip(_xs, _ys))
PARCEL_ACRES = BOUNDARY_POLYGON_UTM.area / SQUARE_METERS_PER_ACRE
