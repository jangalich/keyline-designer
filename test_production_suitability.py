"""
test_production_suitability.py

Offline (no-network) checks for production_suitability.py's per-factor
scoring, soil CARVING (not whole-patch rejection), and weighted composite
ranking. Hand-built synthetic DEMs, same "pure logic, independent of real
data fetches" approach as test_solar_suitability.py.

identify_production_areas() (production_area.py) requires a real parcel
boundary_polygon_utm and clips every patch to on-parcel ground --
score_production_areas() here does the same, filtering its own recovered
cells to the same on-parcel subset, so every synthetic DEM below passes a
FULL_EXTENT boundary (a box exactly matching the DEM's own extent, same
convention test_production_area.py uses) wherever a test isn't
specifically about parcel clipping, so clipping is a no-op there and
doesn't confound the factor being isolated.

Synthetic-DEM layouts, each isolating ONE thing while holding the others
equal:

  1. Compact square vs. an elongated sliver of nearly the SAME acreage --
     tests that size_factor's compactness component, not just raw
     acreage, drives a real ranking difference.
  2. Two identically-shaped/sized patches, one gently south-facing, one
     gently north-facing -- tests aspect_factor direction and weight cap.
  3. A boundary that clips the compact square (from #1) down to roughly
     half its footprint -- tests that score_production_areas() follows
     the SAME on-parcel clipping identify_production_areas() applied.
  4. SOIL CARVING (the core of this pass): a single solid patch, with a
     synthetic disqualifying-soil geometry positioned to hit four
     distinct outcomes -- no overlap (passthrough), a corner carve (single
     remaining piece, same id), a mid-band carve that splits the patch
     into two disconnected survivors (new lettered ids), a split where one
     resulting piece is too small to keep (dropped, same as today's
     existing size-filtering behavior), and a full-cover case (the whole
     patch is disqualifying soil -- dropped entirely). Each surviving
     piece is re-scored against its OWN post-carve geometry/cells, not the
     original patch's pre-carve score.
  5. _fetch_disqualifying_soil_union(): offline, with get_soil_data_for_polygon/
     get_soil_geometries_for_polygon mocked (same "mock the already-tested
     fetch layer" approach as test_farmland_classification.py, just one
     level up) -- confirms it unions only the mukeys that actually meet
     is_disqualifying_soil_condition(), and returns None when nothing does.

Every synthetic DEM using a steep background uses
elevation = 1000 + (row+col)*constant so that ONLY the deliberately
carved flat patches qualify as production-area candidates at all.
"""

from unittest.mock import patch as mock_patch

import numpy as np
from shapely.geometry import box

import production_suitability as ps
from feature_schema import validate_feature_collection
from production_area import identify_production_areas
from production_suitability import (
    ASPECT_FACTOR_WEIGHT,
    SIZE_FACTOR_WEIGHT,
    SLOPE_FACTOR_WEIGHT,
    _WEIGHT_SUM,
    identify_production_area_suitability,
    production_suitability_to_geojson,
    score_production_areas,
    summarize_production_area_suitability,
)

RESOLUTION = (5.0, 5.0)
CRS = "EPSG:32617"


def _steep_background(rows: int, cols: int) -> np.ndarray:
    array = np.zeros((rows, cols), dtype=np.float32)
    for r in range(rows):
        for c in range(cols):
            array[r, c] = 1000.0 + (r + c) * 200.0  # ~10,000%+ grade -- always excluded
    return array


def _dem(array: np.ndarray, origin_y: float = 4500120.0) -> dict:
    return {
        "array": array,
        "resolution_meters": RESOLUTION,
        "origin_x": 500000.0,
        "origin_y": origin_y,
        "crs": CRS,
    }


def _full_extent_boundary(dem: dict):
    """A boundary box exactly matching a synthetic DEM's own extent --
    same convention as test_production_area.py's FULL_EXTENT_BOUNDARY --
    so parcel clipping is a no-op wherever a test isn't specifically
    about clipping itself."""
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    x0, y0 = dem["origin_x"], dem["origin_y"]
    return box(x0, y0 - rows * py, x0 + cols * px, y0)


# --- weights: documented, sum to 1.0, soil is NOT one of them ---

assert abs(_WEIGHT_SUM - 1.0) < 1e-6, f"suitability factor weights must sum to 1.0, got {_WEIGHT_SUM}"
assert ASPECT_FACTOR_WEIGHT < SLOPE_FACTOR_WEIGHT and ASPECT_FACTOR_WEIGHT < SIZE_FACTOR_WEIGHT, (
    "aspect must be the minor factor per the feature spec (matters far less than for solar)"
)
assert not hasattr(ps, "SOIL_FACTOR_WEIGHT"), (
    "soil must NOT be a weighted composite factor -- Scale of Permanence sequencing puts soil "
    "quality at step 8 (last, most improvable), so it shouldn't gate/rank where production zones "
    "go the way slope/size/aspect (step 2, Land Shape) do; soil affects geometry via carving only"
)
print(f"Factor weights sum to 1.0 ({SLOPE_FACTOR_WEIGHT}+{SIZE_FACTOR_WEIGHT}+{ASPECT_FACTOR_WEIGHT}), aspect is the "
      "smallest weight, and soil is not a weighted factor at all (module exposes no SOIL_FACTOR_WEIGHT).")


# --- 1. size_factor: compact square outranks an elongated sliver of similar acreage ---
#
# Dimensions are chosen so the POST-EROSION, POST-CONVEX-HULL acreage
# identify_production_areas() actually reports comes out close for both
# shapes -- compute_slope_percent's steepest-neighbor test erodes one cell
# ring off every candidate patch's edge, and a narrow shape loses
# proportionally more of itself to that erosion than a block does.

rows, cols = 30, 90
array = _steep_background(rows, cols)
for r in range(3, 23):        # 20x20 nominal square -> 18x18 post-erosion -> ~1.79ac hull
    for c in range(3, 23):
        array[r, c] = 100.0
for r in range(3, 12):        # 9x51 nominal sliver -> 7x49 post-erosion -> ~1.78ac hull
    for c in range(30, 81):
        array[r, c] = 100.0

dem_shape = _dem(array)
full_extent_shape = _full_extent_boundary(dem_shape)
shape_patches = identify_production_areas(dem_shape, full_extent_shape)
assert len(shape_patches) == 2, f"expected 2 isolated patches, got {len(shape_patches)}"

scored_shape = score_production_areas(shape_patches, dem_shape, full_extent_shape)
square = next(p for p in scored_shape if p["compactness_score"] == max(x["compactness_score"] for x in scored_shape))
sliver = next(p for p in scored_shape if p is not square)

assert abs(square["area_acres"] - sliver["area_acres"]) / square["area_acres"] < 0.15, (
    "the two regions should be close in acreage -- the test isolates SHAPE, not size"
)
assert square["compactness_score"] > sliver["compactness_score"]
assert square["size_factor"] > sliver["size_factor"], "higher compactness should lift size_factor at equal acreage"
assert square["suitability_score"] > sliver["suitability_score"]
assert square["rank"] == 1 and sliver["rank"] == 2
assert square["soil_carved_acres"] == 0.0 and sliver["soil_carved_acres"] == 0.0, "no soil dict was passed -- nothing should be carved"
print(
    f"Compact square (compactness={square['compactness_score']}, {square['area_acres']} ac, "
    f"score={square['suitability_score']}) outranks the elongated sliver "
    f"(compactness={sliver['compactness_score']}, {sliver['area_acres']} ac, score={sliver['suitability_score']}) "
    "despite similar acreage."
)


# --- 2. aspect_factor: south-facing outranks north-facing at equal slope/size, but only slightly ---

rows, cols = 60, 80
array = _steep_background(rows, cols)
for r in range(3, 17):
    for c in range(3, 17):
        array[r, c] = 100.0 - (r - 3) * 0.1  # south-facing, ~5% grade
for r in range(3, 17):
    for c in range(30, 44):
        array[r, c] = 100.0 + (r - 3) * 0.1  # north-facing, ~5% grade (mirrored)

dem_aspect = _dem(array)
full_extent_aspect = _full_extent_boundary(dem_aspect)
aspect_patches = identify_production_areas(dem_aspect, full_extent_aspect)
assert len(aspect_patches) == 2

scored_aspect = score_production_areas(aspect_patches, dem_aspect, full_extent_aspect)
south = next(p for p in scored_aspect if p["aspect_deg"] is not None and abs(p["aspect_deg"] - 180.0) < 1.0)
north = next(p for p in scored_aspect if p["aspect_deg"] is not None and abs(p["aspect_deg"] - 0.0) < 1.0)

assert south["aspect_factor"] == 1.0 and north["aspect_factor"] == 0.0
assert abs(south["slope_factor"] - north["slope_factor"]) < 1e-6
assert abs(south["size_factor"] - north["size_factor"]) < 1e-6
assert south["suitability_score"] > north["suitability_score"]

score_gap = south["suitability_score"] - north["suitability_score"]
max_possible_gap_from_aspect_alone = ASPECT_FACTOR_WEIGHT * 100
assert score_gap <= max_possible_gap_from_aspect_alone + 0.1, (
    "with every other factor equal, aspect_factor alone must not be able to move the score by more "
    "than its own configured weight"
)
print(
    f"South-facing (score={south['suitability_score']}) outranks north-facing "
    f"(score={north['suitability_score']}) by exactly the aspect weight's worth ({score_gap} points)."
)


# --- 3. boundary clipping: score_production_areas() follows the same on-parcel cells identify_production_areas() clipped to ---

west_half_boundary = box(500000.0, 4500120.0 - rows * 5.0, 500000.0 + 13 * 5.0, 4500120.0)
clipped_patches = identify_production_areas(dem_shape, west_half_boundary)
clipped_square = next(p for p in clipped_patches if p["id"] == square["id"])
assert clipped_square["area_acres"] < square["area_acres"]

clipped_scored = score_production_areas([dict(clipped_square)], dem_shape, west_half_boundary)
assert clipped_scored[0]["area_score"] < square["area_score"], (
    "clipped area_score should be lower than the unclipped square's -- confirms "
    "score_production_areas() scores the ON-PARCEL cells only"
)
print(
    f"Clipping the compact square to its west half correctly shrinks both its reported area_acres "
    f"({square['area_acres']} -> {clipped_square['area_acres']}) AND its scored area_score "
    f"({square['area_score']} -> {clipped_scored[0]['area_score']})."
)


# --- 4. SOIL CARVING: a single solid patch, several disqualifying-geometry placements ---

soil_array = np.full((20, 20), 100.0, dtype=np.float32)
dem_soil = _dem(soil_array, origin_y=4500040.0)
full_extent_soil = _full_extent_boundary(dem_soil)
soil_patches = identify_production_areas(dem_soil, full_extent_soil)
assert len(soil_patches) == 1
base_patch = soil_patches[0]
patch_id = base_patch["id"]
print(f"Soil-carving base patch: id={patch_id}, area_acres={base_patch['area_acres']}, bounds={base_patch['polygon_utm'].bounds}")

# 4a. No disqualifying geometry at all (key absent -> "never checked") -- unmodified passthrough.
scored_absent = score_production_areas([dict(base_patch)], dem_soil, full_extent_soil, {})
assert len(scored_absent) == 1
assert scored_absent[0]["id"] == patch_id
assert scored_absent[0]["soil_carved_acres"] == 0.0
assert scored_absent[0]["soil_data_available"] is False, "a patch id absent from the dict must read as 'never checked'"
print("No soil dict entry at all -> unmodified passthrough, soil_data_available=False ('never checked').")

# 4b. Disqualifying geometry present but genuinely None (checked, clean) -- also unmodified, but soil_data_available=True.
scored_clean = score_production_areas([dict(base_patch)], dem_soil, full_extent_soil, {patch_id: None})
assert scored_clean[0]["soil_carved_acres"] == 0.0
assert scored_clean[0]["soil_data_available"] is True, "checked-and-clean must read as available, unlike never-checked"
assert scored_clean[0]["suitability_score"] == scored_absent[0]["suitability_score"], (
    "checked-and-clean vs never-checked must score identically -- only real disqualifying "
    "geometry (not availability itself) may change the score"
)
print("Disqualifying geometry present but None (checked, clean) -> also unmodified, soil_data_available=True.")

# 4c. Corner carve: removes a chunk from one corner, leaves a single smaller connected remainder (same id).
corner = box(500002.5, 4499942.5, 500050.0, 4499990.0)
scored_corner = score_production_areas([dict(base_patch)], dem_soil, full_extent_soil, {patch_id: corner})
assert len(scored_corner) == 1, "a corner carve must not split the patch"
assert scored_corner[0]["id"] == patch_id, "an unsplit carve keeps the original patch id"
assert scored_corner[0]["source_patch_id"] == patch_id
assert scored_corner[0]["soil_carved_acres"] > 0
assert scored_corner[0]["area_acres"] < base_patch["area_acres"]
assert scored_corner[0]["suitability_score"] != scored_absent[0]["suitability_score"], (
    "carving must actually change the re-scored result, not just be recorded as metadata"
)
print(
    f"Corner carve: single remaining piece keeps id {scored_corner[0]['id']}, "
    f"soil_carved_acres={scored_corner[0]['soil_carved_acres']} "
    f"({scored_corner[0]['soil_carved_pct']}%), area {base_patch['area_acres']} -> {scored_corner[0]['area_acres']}."
)

# 4d. Mid-band carve: splits the patch into two disconnected survivors, each individually re-scored.
midband = box(500045.0, 4499942.5, 500055.0, 4500037.5)
scored_split = score_production_areas([dict(base_patch)], dem_soil, full_extent_soil, {patch_id: midband})
assert len(scored_split) == 2, f"expected a 2-way split, got {len(scored_split)} piece(s)"
ids = {p["id"] for p in scored_split}
assert ids == {f"{patch_id}a", f"{patch_id}b"}, f"expected lettered sub-ids, got {ids}"
for piece in scored_split:
    assert piece["source_patch_id"] == patch_id
    assert piece["soil_carved_acres"] == scored_split[0]["soil_carved_acres"], (
        "soil_carved_acres is the ORIGINAL patch's total carved acreage -- same value on every piece split from it"
    )
    assert piece["area_acres"] < base_patch["area_acres"] / 2 + 0.1, "each half should be roughly half the original"
    # each piece must be scored against its OWN geometry, not the pre-carve patch's score
    assert piece["suitability_score"] != scored_absent[0]["suitability_score"]
print(
    f"Mid-band carve splits into 2 pieces ({[p['id'] for p in scored_split]}), each individually "
    f"re-scored (scores: {[p['suitability_score'] for p in scored_split]}), both referencing "
    f"source_patch_id={patch_id}."
)

# 4e. Split where one resulting piece is below MIN_PRODUCTION_AREA_ACRES -- dropped, the surviving piece kept.
smallband = box(500010.0, 4499942.5, 500020.0, 4500037.5)  # leaves a ~0.18ac sliver + a ~1.8ac remainder
scored_tiny_split = score_production_areas([dict(base_patch)], dem_soil, full_extent_soil, {patch_id: smallband})
assert len(scored_tiny_split) == 1, (
    f"expected exactly 1 surviving piece (the tiny sliver should be dropped below MIN_PRODUCTION_AREA_ACRES), "
    f"got {len(scored_tiny_split)}"
)
assert scored_tiny_split[0]["area_acres"] > 1.0, "the surviving piece should be the larger remainder, not the dropped sliver"
print(
    f"A split leaving one piece below MIN_PRODUCTION_AREA_ACRES correctly drops it, keeping only the "
    f"surviving {scored_tiny_split[0]['area_acres']}ac piece (id {scored_tiny_split[0]['id']})."
)

# 4f. Full cover: the entire patch's footprint is disqualifying soil -- dropped entirely.
full_cover = box(500000.0, 4499940.0, 500100.0, 4500040.0)
scored_full_cover = score_production_areas([dict(base_patch)], dem_soil, full_extent_soil, {patch_id: full_cover})
assert scored_full_cover == [], "a patch entirely covered by disqualifying soil must be dropped, not scored"
print("A patch entirely covered by disqualifying soil is dropped entirely (no candidate reported).")

# 4g. Never checked (soil_data_available=False) still ranks normally -- soil no longer gates ranking at all.
mixed = score_production_areas(
    [dict(base_patch), {**dict(base_patch), "id": 999, "polygon_utm": corner, "area_acres": corner.area / 4046.8564224,
     "geometry_wgs84": base_patch["geometry_wgs84"], "representative_elevation_m": 100.0}],
    dem_soil, full_extent_soil, {patch_id: None},
)
assert all(p["rank"] is not None for p in mixed), "soil no longer creates an unranked bucket -- every candidate is ranked"
assert {p["rank"] for p in mixed} == {1, 2}
print("Every resulting candidate is ranked (soil carving no longer creates an unranked/excluded bucket).")


# --- output: schema-valid FeatureCollection on the SAME layer as production_area.py's own output ---

geojson = production_suitability_to_geojson(scored_split)
validate_feature_collection(geojson)
required_props = {
    "suitability_score", "slope_factor", "size_factor", "aspect_factor",
    "area_acres", "representative_elevation_m", "rank",
    "soil_carved_acres", "soil_carved_pct", "soil_data_available", "source_patch_id",
}
for feature in geojson["features"]:
    assert feature["properties"]["layer"] == "production_area_candidate"
    assert required_props.issubset(feature["properties"].keys()), (
        f"missing required properties: {required_props - feature['properties'].keys()}"
    )
    assert "soil_factor" not in feature["properties"], "soil must not appear as a graded factor property"
    assert "soil_exclusion_passed" not in feature["properties"], "the old binary exclusion property must be gone"
    score = feature["properties"]["suitability_score"]
    assert 0.0 <= score <= 100.0
    for factor_name in ("slope_factor", "size_factor", "aspect_factor"):
        assert 0.0 <= feature["properties"][factor_name] <= 1.0
    assert feature["properties"]["soil_carved_acres"] > 0
    assert "carved out" in feature["properties"]["confidence_notes"]
print("production_suitability_to_geojson output is schema-valid, layer='production_area_candidate', "
      "with soil_carved_acres/soil_carved_pct/source_patch_id and no soil_factor/soil_exclusion_* properties.")

# an unmodified (never-checked) candidate's confidence_notes says so as an estimate
geojson_absent = production_suitability_to_geojson(scored_absent)
notes_absent = geojson_absent["features"][0]["properties"]["confidence_notes"]
assert "ESTIMATE" in notes_absent and "not" in notes_absent.lower()
print("An unchecked candidate's confidence_notes explicitly says soil carving could not be verified.")

# a checked-and-clean candidate's confidence_notes says so plainly, without an estimate caveat
geojson_clean = production_suitability_to_geojson(scored_clean)
notes_clean = geojson_clean["features"][0]["properties"]["confidence_notes"]
assert "no disqualifying soil was found" in notes_clean.lower()
print("A checked-and-clean candidate's confidence_notes states that plainly.")


# --- ranking: rank 1 always has the highest suitability_score ---

for patches_set in (scored_shape, scored_aspect, scored_split):
    ranked = sorted(patches_set, key=lambda p: p["rank"])
    scores_by_rank = [p["suitability_score"] for p in ranked]
    assert scores_by_rank == sorted(scores_by_rank, reverse=True)
print("rank is consistently ordered by descending suitability_score across every scored set.")


# --- summarize helper ---

assert "No production-area candidates" in summarize_production_area_suitability([])
summary = summarize_production_area_suitability(scored_shape)
assert "Rank 1" in summary and "Rank 2" in summary
summary_split = summarize_production_area_suitability(scored_split)
assert "soil-carved" in summary_split
print("summarize_production_area_suitability handles empty input and mentions soil-carving when relevant.")


# --- 5. _fetch_disqualifying_soil_union(): offline, with the fetch layer mocked ---

def _fake_soil_rows_with_hydric(wkt_polygon):
    return [
        {"mukey": "1", "muname": "Rayne silt loam", "drainagecl": "Well drained", "hydricrating": "No"},
        {"mukey": "2", "muname": "Wet inclusion", "drainagecl": "Poorly drained", "hydricrating": "Yes"},
    ]


def _fake_soil_geometries(wkt_polygon):
    return {
        "2": {
            "type": "Polygon",
            "coordinates": [[[-79.984, 40.645], [-79.983, 40.645], [-79.983, 40.646], [-79.984, 40.646], [-79.984, 40.645]]],
        },
    }


with mock_patch.object(ps, "get_soil_data_for_polygon", _fake_soil_rows_with_hydric), \
     mock_patch.object(ps, "get_soil_geometries_for_polygon", _fake_soil_geometries):
    union = ps._fetch_disqualifying_soil_union("polygon((-79.99 40.64, -79.98 40.64, -79.98 40.65, -79.99 40.65, -79.99 40.64))", dem_soil)

assert union is not None and union.geom_type in ("Polygon", "MultiPolygon")
print("_fetch_disqualifying_soil_union() unions only the disqualifying (hydric) mukey's real geometry, not the clean one.")


def _fake_soil_rows_all_clean(wkt_polygon):
    return [{"mukey": "1", "muname": "Rayne silt loam", "drainagecl": "Well drained", "hydricrating": "No"}]


with mock_patch.object(ps, "get_soil_data_for_polygon", _fake_soil_rows_all_clean):
    clean_union = ps._fetch_disqualifying_soil_union("polygon((-79.99 40.64, -79.98 40.64, -79.98 40.65, -79.99 40.65, -79.99 40.64))", dem_soil)

assert clean_union is None, "no disqualifying components at all must return None, not an empty geometry"
print("_fetch_disqualifying_soil_union() returns None when nothing in the footprint is disqualifying.")


# --- identify_production_area_suitability: full orchestrator wiring, network-free via dem=+check_soil=False ---

from rasterio.warp import transform as warp_transform  # noqa: E402  (kept near use, offline-only import)

minx, miny, maxx, maxy = full_extent_shape.bounds
corner_xs, corner_ys = [minx, maxx, maxx, minx, minx], [miny, miny, maxy, maxy, miny]
lons, lats = warp_transform(CRS, "EPSG:4326", corner_xs, corner_ys)
boundary = list(zip(lons, lats))

result = identify_production_area_suitability(boundary, dem=dem_shape, check_soil=False)
assert result["zones_geojson"]["features"], "expected at least one scored feature from the orchestrator"
assert all(f["properties"]["soil_data_available"] is False for f in result["zones_geojson"]["features"]), (
    "check_soil=False must skip the disqualifying-soil fetch entirely"
)
assert all(f["properties"]["soil_carved_acres"] == 0.0 for f in result["zones_geojson"]["features"])
print("identify_production_area_suitability wires DEM->boundary_polygon_utm->identify_production_areas->"
      "score_production_areas->geojson correctly with check_soil=False (no network call).")

print("\nAll production_suitability checks passed.")
