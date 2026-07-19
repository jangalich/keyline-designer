"""
test_fencing.py

Offline (no-network) checks for fencing.py's Step 1 (stream exclusion)
and Step 2 (perimeter) geometry -- the two fencing types that get real
computed geometry (see fencing.py's module docstring for why everything
else in Subdivision Fences is narrative-only and deliberately NOT
generated here).

Stream features here are hand-built schema Features in the same shape
hydrology_data.get_water_features_geojson() actually produces (real
WGS84 line geometry, wrapped with make_feature()), not fetched from NHD
at all -- this tests find_stream_exclusion_fencing()'s buffer/geometry
logic directly, independent of whether the live NHD service is reachable
(it isn't from every environment -- see other test_*.py files' notes
on the same limitation for their own network-backed layers).
"""

import math

from shapely.geometry import Point, shape

from feature_schema import make_feature, validate_feature_collection
from fencing import (
    STREAM_EXCLUSION_BUFFER_METERS,
    find_stream_exclusion_fencing,
    identify_fencing,
    perimeter_fencing_to_geojson,
    stream_exclusion_fencing_to_geojson,
)

UTM_CRS = "EPSG:32617"

# The user's real property boundary (same one every other module's
# __main__ block tests against).
PROPERTY_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

STREAM_FEATURE = make_feature(
    feature_id="nhd-streams-12345",
    geometry={
        "type": "LineString",
        "coordinates": [(-79.9838154, 40.6458343), (-79.9827466, 40.6428581), (-79.9813665, 40.6440549)],
    },
    layer="hydrology-streams",
    label="Montour Run",
    confidence="medium",
    confidence_notes="test fixture, not real NHD data",
)

STREAM_WITH_NO_GEOMETRY = make_feature(
    feature_id="nhd-streams-99999",
    geometry={"type": "LineString", "coordinates": [(0, 0), (0, 1)]},  # placeholder, overwritten below
    layer="hydrology-streams",
    label="Broken stream",
    confidence="medium",
    confidence_notes="test fixture, not real NHD data",
)
STREAM_WITH_NO_GEOMETRY["geometry"] = {"type": "LineString", "coordinates": []}


# --- find_stream_exclusion_fencing: buffer distance is geometrically correct ---

entries = find_stream_exclusion_fencing([STREAM_FEATURE], UTM_CRS, buffer_meters=STREAM_EXCLUSION_BUFFER_METERS)
assert len(entries) == 1, f"expected exactly 1 fencing entry, got {len(entries)}"

entry = entries[0]
assert entry["source_feature_id"] == "nhd-streams-12345"
assert entry["source_label"] == "Montour Run"
assert entry["geometry_wgs84"]["type"] in ("LineString", "MultiLineString")

# The output is the OUTLINE of the buffer, not the filled polygon -- confirm
# every point on that outline sits close to STREAM_EXCLUSION_BUFFER_METERS
# from the original stream centerline (within a couple meters of tolerance
# for buffer-corner/projection rounding), and none of them sit ON the
# centerline itself (which a filled-polygon bug would produce, since a
# polygon's boundary can pass through the source line at self-intersections
# -- this stream is a simple open curve, so that shouldn't happen here).
from rasterio.warp import transform_geom
from shapely.geometry import mapping

original_line_utm = shape(transform_geom("EPSG:4326", UTM_CRS, STREAM_FEATURE["geometry"]))
fence_line_utm = shape(transform_geom("EPSG:4326", UTM_CRS, entry["geometry_wgs84"]))

sample_points = list(fence_line_utm.coords) if fence_line_utm.geom_type == "LineString" else [
    pt for part in fence_line_utm.geoms for pt in part.coords
]
assert len(sample_points) > 0, "buffered fence line should have vertices"

distances = [Point(pt).distance(original_line_utm) for pt in sample_points]
assert all(abs(d - STREAM_EXCLUSION_BUFFER_METERS) < 2.0 for d in distances), (
    f"fence-line vertices should sit ~{STREAM_EXCLUSION_BUFFER_METERS}m from the stream centerline, "
    f"got distances ranging {min(distances):.2f}-{max(distances):.2f}m"
)
print(
    f"Stream exclusion fence line sits ~{STREAM_EXCLUSION_BUFFER_METERS}m from the stream "
    "centerline (buffer OUTLINE, not a filled zone)."
)


# --- a custom buffer distance is honored ---

wide_entries = find_stream_exclusion_fencing([STREAM_FEATURE], UTM_CRS, buffer_meters=25.0)
wide_fence_line_utm = shape(transform_geom("EPSG:4326", UTM_CRS, wide_entries[0]["geometry_wgs84"]))
wide_points = list(wide_fence_line_utm.coords) if wide_fence_line_utm.geom_type == "LineString" else [
    pt for part in wide_fence_line_utm.geoms for pt in part.coords
]
wide_distances = [Point(pt).distance(original_line_utm) for pt in wide_points]
assert all(abs(d - 25.0) < 2.0 for d in wide_distances), "a custom buffer_meters should be honored exactly"
print("Custom exclusion_buffer_meters is honored in the output geometry.")


# --- streams with empty/missing geometry are skipped, not raised on ---

skip_entries = find_stream_exclusion_fencing([STREAM_FEATURE, STREAM_WITH_NO_GEOMETRY], UTM_CRS)
assert len(skip_entries) == 1, "a stream with empty coordinates should be skipped, not crash or produce bad geometry"
print("Stream features with empty/missing geometry are skipped without raising.")


# --- stream_exclusion_fencing_to_geojson: schema-valid, correct layer/properties ---

stream_geojson = stream_exclusion_fencing_to_geojson(entries)
validate_feature_collection(stream_geojson)
stream_feature_out = stream_geojson["features"][0]
assert stream_feature_out["properties"]["layer"] == "exclusion_fencing"
assert stream_feature_out["properties"]["source_feature_id"] == "nhd-streams-12345"
assert stream_feature_out["properties"]["exclusion_buffer_meters"] == STREAM_EXCLUSION_BUFFER_METERS
notes = stream_feature_out["properties"]["confidence_notes"].lower()
assert "suggested" in notes and "not a surveyed fence line" in notes, (
    "stream exclusion confidence_notes must flag this as a suggested boundary, not a surveyed fence line"
)
print("stream_exclusion_fencing_to_geojson output is schema-valid, layer='exclusion_fencing'.")


# --- perimeter_fencing_to_geojson: wraps the boundary unmodified, as a line ---

perimeter_geojson = perimeter_fencing_to_geojson(PROPERTY_BOUNDARY)
validate_feature_collection(perimeter_geojson)
assert len(perimeter_geojson["features"]) == 1
perimeter_feature = perimeter_geojson["features"][0]
assert perimeter_feature["properties"]["layer"] == "perimeter_fencing"
assert perimeter_feature["geometry"]["type"] == "LineString", (
    "perimeter fencing must be a LineString (a fence line), not a Polygon (a filled zone)"
)

out_coords = perimeter_feature["geometry"]["coordinates"]
in_coords = [tuple(pt) for pt in PROPERTY_BOUNDARY]
assert out_coords[:len(in_coords)] == in_coords, "perimeter geometry must be the boundary itself, unmodified"
assert out_coords[0] == out_coords[-1], "perimeter fence line should be a closed ring"
print("perimeter_fencing_to_geojson wraps the property boundary unmodified as a closed LineString.")

assert "no fence type, height, or material" in perimeter_feature["properties"]["confidence_notes"].lower(), (
    "perimeter fencing confidence_notes must explicitly state fence type/height/material is out of scope"
)
print("Perimeter fencing confidence_notes explicitly excludes fence type/height/material guidance.")


# --- identify_fencing: combines both layers, reusing a pre-fetched water_features_geojson ---

from feature_schema import make_feature_collection

prefetched_water = make_feature_collection([STREAM_FEATURE])
result = identify_fencing(PROPERTY_BOUNDARY, water_features_geojson=prefetched_water)
validate_feature_collection(result["fencing_geojson"])

layers = sorted(f["properties"]["layer"] for f in result["fencing_geojson"]["features"])
assert layers == ["exclusion_fencing", "perimeter_fencing"], f"expected both fencing layers, got {layers}"
print("identify_fencing() combines exclusion_fencing + perimeter_fencing into one schema-valid FeatureCollection.")


# --- identify_fencing: a water_features_geojson with no streams still produces perimeter fencing ---

no_stream_water = make_feature_collection([])
no_stream_result = identify_fencing(PROPERTY_BOUNDARY, water_features_geojson=no_stream_water)
no_stream_layers = [f["properties"]["layer"] for f in no_stream_result["fencing_geojson"]["features"]]
assert no_stream_layers == ["perimeter_fencing"], (
    f"with no streams, only perimeter_fencing should be produced, got {no_stream_layers}"
)
print("identify_fencing() still produces perimeter fencing when no streams are present.")


print("\nAll fencing checks passed.")
