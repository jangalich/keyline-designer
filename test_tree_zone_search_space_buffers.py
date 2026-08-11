"""
test_tree_zone_search_space_buffers.py

Offline (no-network) checks for compute_tree_search_space()'s production/water
exclusion buffers (TREE_ZONE_PRODUCTION_BUFFER_METERS/TREE_ZONE_WATER_BUFFER_METERS),
added alongside hatch-only tree-zone rendering (render_layout_map.py, no test
coverage needed there beyond the existing test_render_layout_map.py plot_polygon()
kwarg assertions).

Round-number, hand-verifiable geometry throughout -- same "synthetic fixture a
human can check by hand" standard as test_tree_zone_candidates.py's own
compute_tree_search_space() coverage, kept in this dedicated file rather than
added to that already-large file since this is new, narrowly-scoped coverage
for a single function's new keyword arguments.

Every call below passes boundary_setback_meters=0.0 explicitly -- this file is
about the production/water exclusion buffers specifically, and the boundary
setback (TREE_ZONE_BOUNDARY_SETBACK_METERS, its own separate default) has its
own dedicated coverage in test_tree_zone_boundary_setback.py; isolating it
here keeps this file's own hand-computed areas (some of which use fixtures,
e.g. water_square, sitting right at the boundary's own corner) exact and
unaffected by that unrelated default, same "isolate tests that aren't about a
given gate from that gate" convention test_production_area_ceiling.py's own
boundary_setback_meters=0.0 partial already establishes for production_area.py's
analogous PRODUCTION_BOUNDARY_SETBACK_METERS.
"""

from shapely.geometry import box

from raster_grid import SQUARE_METERS_PER_ACRE
from tree_zone_candidates import (
    TREE_ZONE_PRODUCTION_BUFFER_METERS,
    TREE_ZONE_WATER_BUFFER_METERS,
    compute_tree_search_space,
)

assert TREE_ZONE_PRODUCTION_BUFFER_METERS == 5.0
assert TREE_ZONE_WATER_BUFFER_METERS == 5.0

# 100m x 100m boundary (10,000 sqm), a single 20m x 20m production square
# (400 sqm) centered inside it, well clear of the boundary edge so the
# buffer never clips against the boundary itself.
boundary = box(0, 0, 100, 100)
production_square = box(40, 40, 60, 60)  # 20x20, 400 sqm


# =====================================================================
# production_buffer_meters=0.0: reproduces the pre-buffer behavior EXACTLY
# =====================================================================

search_space_unbuffered, claimed_unbuffered = compute_tree_search_space(
    boundary,
    [production_square],
    [],
    [],
    production_buffer_meters=0.0,
    water_buffer_meters=0.0,
    boundary_setback_meters=0.0,
)
assert abs(claimed_unbuffered.area - 400.0) < 1e-9, (
    f"production_buffer_meters=0.0 must claim EXACTLY the raw 20x20 square (400 sqm), got {claimed_unbuffered.area}"
)
assert abs(search_space_unbuffered.area - (10000.0 - 400.0)) < 1e-9, (
    f"production_buffer_meters=0.0 search space must be EXACTLY boundary minus the raw square, "
    f"got {search_space_unbuffered.area}"
)
print(
    f"production_buffer_meters=0.0 reproduces the old, unbuffered behavior exactly: "
    f"claimed={claimed_unbuffered.area} sqm, search_space={search_space_unbuffered.area} sqm."
)


# =====================================================================
# Default production_buffer_meters (TREE_ZONE_PRODUCTION_BUFFER_METERS, 5.0m):
# a 20x20 square buffered by 5m is bounded by, but strictly SMALLER than, the
# naive 30x30 square (900 sqm) a caller might expect -- shapely's default
# buffer() rounds the four corners with a quarter-circle arc (radius 5m)
# rather than squaring them off, so the buffered shape's area is exactly
# 900 - 4*(25 - pi*6.25) = 900 - (100 - 25*pi) = 800 + 25*pi ~= 878.5 sqm,
# hand-computable via the buffer(quad_segs=8, the shapely default)-approximated
# quarter-circle corners: perimeter*r + pi*r^2 added to the original area
# (400 + 80*5 + pi*25 ~= 878.5), same number from the other direction.
# =====================================================================

search_space_default, claimed_default = compute_tree_search_space(
    boundary, [production_square], [], [], boundary_setback_meters=0.0
)

expected_buffered_area = 400.0 + (4 * 20) * TREE_ZONE_PRODUCTION_BUFFER_METERS + 3.14159265 * TREE_ZONE_PRODUCTION_BUFFER_METERS**2
naive_30x30_area = 900.0

assert claimed_default.area < naive_30x30_area, (
    "a round-cornered buffer() must claim LESS area than a naive squared-off 30x30 exclusion -- "
    f"got claimed={claimed_default.area} sqm, naive square={naive_30x30_area} sqm"
)
assert abs(claimed_default.area - expected_buffered_area) < 5.0, (
    f"buffered claimed area should match the hand-computed round-cornered 30x30-ish exclusion "
    f"(~{expected_buffered_area:.2f} sqm) to within shapely's own polygonal buffer approximation, "
    f"got {claimed_default.area}"
)
assert abs(search_space_default.area - (10000.0 - expected_buffered_area)) < 5.0, (
    f"search space should be boundary minus the hand-computed buffered exclusion to within shapely's "
    f"own buffer approximation, got {search_space_default.area}"
)
# Sanity: buffering strictly GREW the claimed area relative to the raw square.
assert claimed_default.area > claimed_unbuffered.area, "the default buffer must strictly grow the claimed exclusion"
print(
    f"Default production_buffer_meters={TREE_ZONE_PRODUCTION_BUFFER_METERS}m: claimed={claimed_default.area:.2f} sqm "
    f"(hand-computed ~{expected_buffered_area:.2f} sqm, strictly less than a naive squared-off {naive_30x30_area} sqm "
    f"30x30 exclusion, since shapely's default buffer() rounds corners), search_space={search_space_default.area:.2f} sqm."
)


# =====================================================================
# Same buffer/no-buffer contrast for the WATER exclusion, its own separate
# constant/kwarg (TREE_ZONE_WATER_BUFFER_METERS/water_buffer_meters) --
# placed in a different corner of the boundary, far enough from the
# production square that the two buffers never interact.
# =====================================================================

water_square = box(0, 0, 20, 20)  # 20x20, 400 sqm, far corner from production_square

search_space_water_unbuffered, claimed_water_unbuffered = compute_tree_search_space(
    boundary,
    [],
    [water_square],
    [],
    production_buffer_meters=0.0,
    water_buffer_meters=0.0,
    boundary_setback_meters=0.0,
)
assert abs(claimed_water_unbuffered.area - 400.0) < 1e-9, "water_buffer_meters=0.0 must claim exactly the raw square"

search_space_water_default, claimed_water_default = compute_tree_search_space(
    boundary, [], [water_square], [], boundary_setback_meters=0.0
)
expected_water_buffered_area = 400.0 + (4 * 20) * TREE_ZONE_WATER_BUFFER_METERS + 3.14159265 * TREE_ZONE_WATER_BUFFER_METERS**2
assert abs(claimed_water_default.area - expected_water_buffered_area) < 5.0, (
    f"default water buffer should match the same hand-computed round-cornered exclusion math as production, "
    f"got {claimed_water_default.area} vs expected ~{expected_water_buffered_area:.2f}"
)
print(
    f"Default water_buffer_meters={TREE_ZONE_WATER_BUFFER_METERS}m behaves identically to production's own "
    f"buffer math (its own separate constant/kwarg): claimed={claimed_water_default.area:.2f} sqm "
    f"(hand-computed ~{expected_water_buffered_area:.2f} sqm)."
)


# =====================================================================
# road_polygons_utm is NEVER buffered by this function -- it arrives
# already-buffered from _road_corridor_exclusion_polygon() and must not be
# double-buffered. A disjoint road strip's claimed contribution must equal
# its own raw area exactly, regardless of production_buffer_meters/
# water_buffer_meters.
# =====================================================================

road_strip = box(0, 45, 100, 46)  # 100m x 1m, 100 sqm, disjoint from both squares above
_, claimed_with_road = compute_tree_search_space(boundary, [], [], [road_strip], boundary_setback_meters=0.0)
assert abs(claimed_with_road.area - 100.0) < 1e-9, (
    f"road_polygons_utm must be claimed at EXACTLY its own real area (100 sqm) -- never buffered by this "
    f"function, got {claimed_with_road.area}"
)
print("road_polygons_utm is never buffered by compute_tree_search_space() -- claimed at exactly its own real area.")


# =====================================================================
# claimed_union is None iff all three lists are empty -- unaffected by the
# new buffer kwargs (buffering changes extent, not whether a union exists).
# =====================================================================

empty_search_space, empty_claimed = compute_tree_search_space(boundary, [], [], [], boundary_setback_meters=0.0)
assert empty_claimed is None and empty_search_space is boundary, (
    "with nothing claimed at all, claimed_union must still be None and search_space must still be the "
    "boundary object itself, unaffected by the new default buffer kwargs"
)
print("claimed_union is still None iff all three input lists are empty, unaffected by the new buffer defaults.")

print(f"\nAll compute_tree_search_space() buffer checks passed ({(search_space_default.area / SQUARE_METERS_PER_ACRE):.4f} "
      "acres search space in the round-number production-buffer fixture, for reference).")
