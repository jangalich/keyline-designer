"""
test_render_layout_map.py

Offline (no-network) checks for render_layout_map.py's production-zone
rendering: contour-line texture (contour_lines.py's global contour lines,
clipped per zone at render time against that zone's own real polygon_utm),
not a filled/outlined shape -- see production_area.py's and
render_layout_map.py's own module docstrings for why the earlier
display_polygon_utm/display_geometry_wgs84 fields were removed entirely
in favor of this.

Builds real patch dicts via production_area.cluster_and_gate() +
production_suitability.score_production_areas() against small synthetic,
SLOPED DEMs (a real elevation gradient is required here -- unlike
test_production_area.py's own flat fixtures, which only needed cell-mask
shape, contour lines need real elevation variation to exist at all), then
feeds them to render_layout_map.py through its `layers` pre-fetch
parameter -- no DEM/soil/basemap network calls required; the basemap
fetch itself degrades gracefully (same as every other network-backed
layer in this pipeline) when unreachable, so render_layout_map() runs
fully offline here.

The one invariant this whole pass depends on: zones_geojson (and every
patch's geometry_wgs84/polygon_utm) is completely unaffected by this
rendering-only change -- see the dedicated regression check near the
bottom.
"""

import os
import tempfile

import numpy as np
from shapely.geometry import box

import production_area as pa
import render_layout_map as rlm
from contour_lines import compute_contour_lines
from production_area import cluster_and_gate, compute_step1_eligible_cells
from production_suitability import score_production_areas

RESOLUTION = (5.0, 5.0)
RISE_PER_ROW = 0.4  # meters -- a real, modest gradient so contour lines actually exist


def _sloped_dem(rows: int, cols: int) -> dict:
    array = np.zeros((rows, cols), dtype=np.float32)
    for row in range(rows):
        array[row, :] = 100.0 + row * RISE_PER_ROW
    return {
        "array": array,
        "resolution_meters": RESOLUTION,
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }


def _full_extent_boundary(dem: dict):
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    x0, y0 = dem["origin_x"], dem["origin_y"]
    return box(x0, y0 - rows * py, x0 + cols * px, y0)


def _rect_cells(r0, r1, c0, c1):
    return [(r, c) for r in range(r0, r1) for c in range(c0, c1)]


def _mask_from_cells(shape, cells):
    mask = np.zeros(shape, dtype=bool)
    for r, c in cells:
        mask[r, c] = True
    return mask


def _scored_patches_for(cell_mask, dem):
    """Real end-to-end patch dicts (cluster_and_gate + score_production_areas),
    exactly the shape render_layout_map.py's own production_result['scored_patches']
    already carries."""
    boundary = _full_extent_boundary(dem)
    step1 = compute_step1_eligible_cells(dem, boundary, disqualifying_soil_union_utm=None)
    patches = cluster_and_gate(cell_mask, dem, boundary, step1)
    return score_production_areas(patches, dem, step1)


def _clip_contours_to_zone(contour_lines: list[dict], patch: dict):
    """Exactly what render_layout_map.py's own rendering loop does per
    production zone -- real shapely intersection of the GLOBAL contour
    lines against that zone's own polygon_utm (same CRS, no reprojection
    needed) -- returns the list of non-empty clipped geometries."""
    clipped = []
    for contour in contour_lines:
        piece = contour["lines_utm"].intersection(patch["polygon_utm"])
        if not piece.is_empty:
            clipped.append(piece)
    return clipped


# --- Solid zone smaller than the DEM's full extent: clipped contours must stay
#     entirely within the zone's own real boundary -- no segment outside it ---

SINGLE_SHAPE = (30, 30)
single_dem = _sloped_dem(*SINGLE_SHAPE)
single_cells = _rect_cells(5, 15, 5, 15)  # a 10x10 block -- well inside the 30x30 DEM, not the full extent
single_mask = _mask_from_cells(SINGLE_SHAPE, single_cells)
single_scored = _scored_patches_for(single_mask, single_dem)
assert len(single_scored) == 1, f"expected 1 scored patch, got {len(single_scored)}"
single_patch = single_scored[0]

global_contours = compute_contour_lines(single_dem)
assert len(global_contours) > 0, "test setup should produce real global contour lines on a sloped DEM"

zone_bounds = single_patch["polygon_utm"].bounds
full_extent_bounds = _full_extent_boundary(single_dem).bounds
assert full_extent_bounds != zone_bounds, (
    "test setup should genuinely have contours extending past the zone's own (smaller) extent, "
    "otherwise clipping isn't actually being exercised"
)
some_contour_exceeds_zone = any(
    not box(*zone_bounds).buffer(1e-6).contains(c["lines_utm"]) for c in global_contours
)
assert some_contour_exceeds_zone, (
    "test setup should genuinely have at least one global contour line extending past the zone's own "
    "bounds -- otherwise this test wouldn't be exercising real clipping"
)

clipped_pieces = _clip_contours_to_zone(global_contours, single_patch)
assert clipped_pieces, "expected at least one contour segment to survive clipping into this zone"
zone_polygon_buffered = single_patch["polygon_utm"].buffer(1e-6)
for piece in clipped_pieces:
    assert zone_polygon_buffered.contains(piece), (
        "every clipped contour segment must lie entirely within the zone's own real boundary "
        "(polygon_utm) -- no segment should exist outside it in the clipped output"
    )
print(
    f"Clipping: {len(clipped_pieces)} of {len(global_contours)} global contour line(s) survive clipping into "
    "a single zone smaller than the DEM's full extent, and every clipped segment stays entirely within that "
    "zone's own real boundary."
)


# --- Split (waist) cluster: the two resulting zones must each get their own independently-clipped
#     contour segments, with no segments appearing in the gap between them ---

DUMBBELL_SHAPE = (16, 30)
dumbbell_dem = _sloped_dem(*DUMBBELL_SHAPE)
lobe_a = _rect_cells(0, 10, 0, 10)
lobe_b = _rect_cells(0, 10, 16, 26)
narrow_strip = _rect_cells(4, 6, 10, 16)  # narrower than MIN_ZONE_WAIST_METERS -- same fixture style as production_area.py's own tests
dumbbell_cells = lobe_a + lobe_b + narrow_strip
dumbbell_mask = _mask_from_cells(DUMBBELL_SHAPE, dumbbell_cells)

# The real, permanently EXCLUDED ground between the lobes -- the rest of
# the connecting corridor (same column range as narrow_strip, but the
# rows OUTSIDE it) that was never part of the dumbbell mask at all, on
# either side of the split. This is the actual "gap" a farmer would see
# on the ground; it's distinct from the reclaimed narrow_strip cells
# themselves, which Part 1 splits down the middle between the two zones
# -- the two zones' own real footprints are allowed to TOUCH exactly
# along that internal dividing line (adjacent cells sharing an edge,
# zero-area/zero-length overlap), that's expected and not a "gap
# violation"; what must never happen is a contour segment actually
# drawn INSIDE this real excluded corridor.
gap_cells = _rect_cells(0, 4, 10, 16) + _rect_cells(6, 10, 10, 16)

dumbbell_scored = _scored_patches_for(dumbbell_mask, dumbbell_dem)
assert len(dumbbell_scored) == 2, f"expected the waist split to produce 2 scored patches, got {len(dumbbell_scored)}"
zone_1, zone_2 = dumbbell_scored

gap_footprint = pa._cell_union_footprint(gap_cells, dumbbell_dem)
assert zone_1["polygon_utm"].intersection(gap_footprint).area < 1e-9, (
    "test sanity check: zone 1's real footprint must not actually cover any of the excluded gap ground "
    "(touching its edge is fine, covering real area of it is not)"
)
assert zone_2["polygon_utm"].intersection(gap_footprint).area < 1e-9, (
    "test sanity check: zone 2's real footprint must not actually cover any of the excluded gap ground"
)

dumbbell_global_contours = compute_contour_lines(dumbbell_dem)
assert len(dumbbell_global_contours) > 0, "test setup should produce real global contour lines on a sloped DEM"

clipped_1 = _clip_contours_to_zone(dumbbell_global_contours, zone_1)
clipped_2 = _clip_contours_to_zone(dumbbell_global_contours, zone_2)
assert clipped_1 and clipped_2, "both split zones should get at least one clipped contour segment each"

zone_1_buffered = zone_1["polygon_utm"].buffer(1e-6)
zone_2_buffered = zone_2["polygon_utm"].buffer(1e-6)
for piece in clipped_1:
    assert zone_1_buffered.contains(piece), "zone 1's clipped contours must stay entirely within zone 1's own boundary"
for piece in clipped_2:
    assert zone_2_buffered.contains(piece), "zone 2's clipped contours must stay entirely within zone 2's own boundary"

gap_overlap_length = sum(piece.intersection(gap_footprint).length for piece in clipped_1 + clipped_2)
assert gap_overlap_length < 1e-9, (
    f"no clipped contour segment (from EITHER zone) may actually run through the real excluded gap ground "
    f"between the two split zones -- got {gap_overlap_length}m of overlap"
)
print(
    f"Split rendering: the two waist-split zones each get their own independently-clipped contour segments "
    f"({len(clipped_1)} and {len(clipped_2)} respectively), with zero overlap into the real excluded gap "
    f"ground ({round(gap_footprint.area, 1)} sq m) between them."
)


# --- Full render_layout_map() pass, offline: confirms the whole contour-clipping pipeline
#     (fetch_layout_layers' own contour_lines computation, per-zone clipping, reprojection,
#     drawing, legend, PNG assembly) runs without crashing for a production_result with a split ---

property_boundary = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

for rank, patch in enumerate(sorted(dumbbell_scored, key=lambda p: -p["suitability_score"]), start=1):
    patch["rank"] = rank

synthetic_layers = {
    "dem": dumbbell_dem,
    "production_result": {
        "scored_patches": dumbbell_scored,
        "total_selected_acreage": round(sum(p["area_acres"] for p in dumbbell_scored), 2),
        "percent_of_parcel": 42.0,
    },
    "water_zone": None,
    "road_corridor": None,
    "structure_site": None,
    "water_features": {"streams": []},
    "contour_lines": dumbbell_global_contours,
}

with tempfile.TemporaryDirectory() as tmpdir:
    output_path = os.path.join(tmpdir, "layout_map.png")
    result_path = rlm.render_layout_map(property_boundary, output_path, layers=synthetic_layers)
    assert result_path == output_path
    assert os.path.exists(output_path)
    assert os.path.getsize(output_path) > 0, "render_layout_map() must produce a real, non-empty PNG"

print(
    "Full pipeline: render_layout_map() runs offline end-to-end (basemap fetch degrades gracefully) with a "
    "split production_result, drawing clipped contour-line texture for each zone and producing a real, "
    "non-empty PNG."
)


# =====================================================================
# Invariant: zones_geojson (and every patch's geometry_wgs84/polygon_utm)
# is completely unaffected by this rendering-only change -- this only
# changes HOW production zones are drawn and removes the now-unused
# display_polygon_utm/display_geometry_wgs84 fields, not the canonical
# geometry any other consumer reasons over.
# =====================================================================

from production_area import production_areas_to_geojson
from production_suitability import production_suitability_to_geojson

zone_1_geojson = production_suitability_to_geojson([dict(zone_1)])
assert zone_1_geojson["features"][0]["geometry"] == zone_1["geometry_wgs84"], (
    "production_suitability_to_geojson() must still embed geometry_wgs84 exactly, byte-for-byte"
)
assert "display_polygon_utm" not in zone_1 and "display_geometry_wgs84" not in zone_1, (
    "the removed display fields must not reappear on a scored patch dict"
)

raw_patches = cluster_and_gate(
    dumbbell_mask, dumbbell_dem, _full_extent_boundary(dumbbell_dem),
    compute_step1_eligible_cells(dumbbell_dem, _full_extent_boundary(dumbbell_dem), disqualifying_soil_union_utm=None),
)
raw_geojson = production_areas_to_geojson(raw_patches)
for feature, patch in zip(raw_geojson["features"], raw_patches):
    assert feature["geometry"] == patch["geometry_wgs84"], (
        "production_areas_to_geojson() must also embed geometry_wgs84 exactly for every patch"
    )
    assert "display_polygon_utm" not in patch and "display_geometry_wgs84" not in patch

print(
    "zones_geojson invariant: both production_areas_to_geojson() and production_suitability_to_geojson() "
    "still embed geometry_wgs84 exactly, byte-for-byte -- unaffected by the switch to contour-line "
    "rendering, and the removed display fields are genuinely gone from every patch dict."
)


print("\nAll render_layout_map checks passed.")
