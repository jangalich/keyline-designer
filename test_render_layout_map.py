"""
test_render_layout_map.py

Offline (no-network) checks for render_layout_map.py's production-zone
drawing, specifically the switch from geometry_wgs84 (the real cell-union
footprint) to display_geometry_wgs84 (production_area.py's smoothed,
hole-punched convex hull -- see cluster_and_gate()'s own docstring).

Builds real patch dicts via production_area.cluster_and_gate() +
production_suitability.score_production_areas() against small synthetic
DEMs (the same dumbbell/ring fixtures test_production_area.py's own Part
1/2/3 tests use), then feeds them to render_layout_map.py through its
`layers` pre-fetch parameter -- no DEM/soil/basemap network calls
required; the basemap fetch itself degrades gracefully (same as every
other network-backed layer in this pipeline) when unreachable, so
render_layout_map() runs fully offline here.

The one invariant this whole pass depends on: zones_geojson (and every
patch's geometry_wgs84/polygon_utm) must stay byte-for-byte the real
footprint, never the cosmetic display geometry -- see the dedicated
regression check at the bottom.
"""

import os
import tempfile

import numpy as np
from matplotlib.path import Path
from shapely.geometry import Polygon
from shapely.plotting import patch_from_polygon

import production_area as pa
import render_layout_map as rlm
from production_area import cluster_and_gate, compute_step1_eligible_cells
from production_area_ceiling import identify_optimized_production_areas  # noqa: F401  (import-shape sanity check)
from production_suitability import score_production_areas

RESOLUTION = (5.0, 5.0)


def _dem(rows: int, cols: int) -> dict:
    array = np.full((rows, cols), 100.0, dtype=np.float32)
    return {
        "array": array,
        "resolution_meters": RESOLUTION,
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }


def _full_extent_boundary(dem: dict):
    from shapely.geometry import box

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
    already carries -- same 'rank'/'area_acres'/'display_geometry_wgs84' fields."""
    boundary = _full_extent_boundary(dem)
    step1 = compute_step1_eligible_cells(dem, boundary, disqualifying_soil_union_utm=None)
    patches = cluster_and_gate(cell_mask, dem, boundary, step1)
    return score_production_areas(patches, dem, step1)


# --- Ring/donut (true hole): display_geometry_wgs84 must carry an interior ring,
#     and shapely.plotting's own patch construction must draw it as a real
#     compound Matplotlib path (2 sub-paths), not silently drop it ---

RING_SHAPE = (18, 18)
ring_dem = _dem(*RING_SHAPE)
outer = set(_rect_cells(1, 17, 1, 17))
hole = set(_rect_cells(6, 12, 6, 12))
ring_cells = list(outer - hole)
ring_mask = _mask_from_cells(RING_SHAPE, ring_cells)

ring_scored = _scored_patches_for(ring_mask, ring_dem)
assert len(ring_scored) == 1, f"expected 1 scored patch for the ring fixture, got {len(ring_scored)}"
ring_patch = ring_scored[0]
assert len(ring_patch["hole_footprints"]) == 1

# The exact reprojection render_layout_map.py itself performs for
# production zones -- reusing the real function, not a reimplementation.
ring_geom_mercator = rlm._reproject_geometry_to_mercator(ring_patch["display_geometry_wgs84"])
assert ring_geom_mercator.geom_type == "Polygon"
assert len(ring_geom_mercator.interiors) == 1, (
    f"display_geometry_wgs84 (reprojected via render_layout_map.py's own helper) must carry exactly one "
    f"interior ring for the one true hole, got {len(ring_geom_mercator.interiors)}"
)

# shapely.plotting.plot_polygon draws via patch_from_polygon() internally
# (see plot_polygon's own source) -- calling it directly on the exact
# geometry render_layout_map.py hands to plot_polygon() confirms the real
# code path, not just the abstract claim that shapely "supports holes".
mpl_patch = patch_from_polygon(ring_geom_mercator)
path = mpl_patch.get_path()
num_subpaths = int((path.codes == Path.MOVETO).sum())
assert num_subpaths == 2, (
    f"a Polygon with 1 interior ring must compile to a 2-subpath compound Matplotlib path "
    f"(1 exterior MOVETO + 1 interior MOVETO), got {num_subpaths}"
)
print(
    "Hole rendering: display_geometry_wgs84 for a ring/donut cluster reprojects (via render_layout_map.py's "
    "own _reproject_geometry_to_mercator) to a Polygon with 1 interior ring, and shapely.plotting's real "
    "patch_from_polygon() compiles it to a genuine 2-subpath compound Matplotlib path -- the hole is drawn, "
    "not smoothed over."
)


# --- Solid square (no hole): confirm the same real code path draws a single-subpath patch ---

SQUARE_SHAPE = (12, 12)
square_dem = _dem(*SQUARE_SHAPE)
square_cells = _rect_cells(1, 11, 1, 11)
square_mask = _mask_from_cells(SQUARE_SHAPE, square_cells)
square_scored = _scored_patches_for(square_mask, square_dem)
assert len(square_scored) == 1
square_geom_mercator = rlm._reproject_geometry_to_mercator(square_scored[0]["display_geometry_wgs84"])
assert len(square_geom_mercator.interiors) == 0
square_mpl_patch = patch_from_polygon(square_geom_mercator)
square_num_subpaths = int((square_mpl_patch.get_path().codes == Path.MOVETO).sum())
assert square_num_subpaths == 1, "a hole-free cluster must compile to a single-subpath Matplotlib path"
print("Hole rendering: a solid, hole-free cluster correctly compiles to a single-subpath Matplotlib path.")


# --- Split cluster (waist split): the two resulting display geometries must render as
#     independent, non-overlapping shapes with a real gap -- not touching, not overlapping ---

DUMBBELL_SHAPE = (12, 24)
dumbbell_dem = _dem(*DUMBBELL_SHAPE)
lobe_a = _rect_cells(0, 10, 0, 10)
lobe_b = _rect_cells(0, 10, 14, 24)
narrow_strip = _rect_cells(4, 6, 10, 14)  # narrower than MIN_ZONE_WAIST_METERS -- same fixture as production_area.py's own test
dumbbell_cells = lobe_a + lobe_b + narrow_strip
dumbbell_mask = _mask_from_cells(DUMBBELL_SHAPE, dumbbell_cells)

dumbbell_scored = _scored_patches_for(dumbbell_mask, dumbbell_dem)
assert len(dumbbell_scored) == 2, f"expected the waist split to produce 2 scored patches, got {len(dumbbell_scored)}"

geom_1 = rlm._reproject_geometry_to_mercator(dumbbell_scored[0]["display_geometry_wgs84"])
geom_2 = rlm._reproject_geometry_to_mercator(dumbbell_scored[1]["display_geometry_wgs84"])

assert not geom_1.intersects(geom_2), (
    "the two split production zones' display geometries must be genuinely disjoint (no touching, no "
    "overlap) -- the excluded waist ground between them must not be filled in by either hull"
)
gap_distance = geom_1.distance(geom_2)
assert gap_distance > 0, f"expected a real, positive gap between the two split zones, got {gap_distance}"
print(
    f"Split rendering: the two waist-split production zones' display geometries are genuinely disjoint, "
    f"with a real {round(gap_distance, 2)}m gap between them -- neither hull swallows the excluded ground."
)


# --- Full render_layout_map() pass, offline: confirms the swapped field flows through the
#     entire rendering pipeline (basemap fallback, halo, boundary, legend, PNG assembly)
#     without crashing, for a production_result carrying BOTH a hole and a split ---

property_boundary = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

combined_scored = ring_scored + dumbbell_scored
for rank, patch in enumerate(sorted(combined_scored, key=lambda p: -p["suitability_score"]), start=1):
    patch["rank"] = rank

synthetic_layers = {
    "dem": None,
    "production_result": {
        "scored_patches": combined_scored,
        "total_selected_acreage": round(sum(p["area_acres"] for p in combined_scored), 2),
        "percent_of_parcel": 42.0,
    },
    "water_zone": None,
    "road_corridor": None,
    "structure_site": None,
    "water_features": {"streams": []},
}

with tempfile.TemporaryDirectory() as tmpdir:
    output_path = os.path.join(tmpdir, "layout_map.png")
    result_path = rlm.render_layout_map(property_boundary, output_path, layers=synthetic_layers)
    assert result_path == output_path
    assert os.path.exists(output_path)
    assert os.path.getsize(output_path) > 0, "render_layout_map() must produce a real, non-empty PNG"

print(
    "Full pipeline: render_layout_map() runs offline end-to-end (basemap fetch degrades gracefully) with a "
    "production_result carrying both a hole and a split, producing a real, non-empty PNG."
)


# =====================================================================
# Invariant: zones_geojson (and every patch's geometry_wgs84/polygon_utm)
# stays the real cell-union footprint, byte-for-byte, regardless of this
# rendering-only display_geometry_wgs84 addition. This is the specific
# thing this whole pass -- and any future edit to render_layout_map.py --
# must never break.
# =====================================================================

from production_area import production_areas_to_geojson
from production_suitability import production_suitability_to_geojson

# The ring fixture is the most telling case here: display_geometry_wgs84
# (hull, hole punched out) and geometry_wgs84 (real footprint) are
# GENUINELY different geometries for it (different point sets), so this
# also proves the geojson wasn't accidentally switched to the display
# field, not just that no crash occurred.
ring_geojson = production_suitability_to_geojson([dict(ring_patch)])
ring_feature_geometry = ring_geojson["features"][0]["geometry"]
assert ring_feature_geometry == ring_patch["geometry_wgs84"], (
    "production_suitability_to_geojson() must embed geometry_wgs84 (the real footprint) exactly -- "
    "byte-for-byte -- unaffected by the new display_geometry_wgs84 field"
)
assert ring_feature_geometry != ring_patch["display_geometry_wgs84"], (
    "test sanity check: for a cluster with a real hole, geometry_wgs84 and display_geometry_wgs84 must "
    "actually differ, otherwise the assertion above wouldn't be a meaningful regression guard"
)

raw_ring_patches = cluster_and_gate(
    ring_mask, ring_dem, _full_extent_boundary(ring_dem),
    compute_step1_eligible_cells(ring_dem, _full_extent_boundary(ring_dem), disqualifying_soil_union_utm=None),
)
raw_geojson = production_areas_to_geojson(raw_ring_patches)
assert raw_geojson["features"][0]["geometry"] == raw_ring_patches[0]["geometry_wgs84"], (
    "production_areas_to_geojson() must also embed geometry_wgs84 exactly, unaffected by display_geometry_wgs84"
)

print(
    "zones_geojson invariant: both production_areas_to_geojson() and production_suitability_to_geojson() "
    "still embed geometry_wgs84 (the real cell-union footprint) exactly, byte-for-byte -- unaffected by the "
    "new rendering-only display_geometry_wgs84 field, and genuinely different from it where a hole exists."
)


print("\nAll render_layout_map checks passed.")
