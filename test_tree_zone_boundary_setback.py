"""
test_tree_zone_boundary_setback.py

Offline (no-network) checks for compute_tree_search_space()'s boundary
setback (TREE_ZONE_BOUNDARY_SETBACK_METERS/boundary_setback_meters) -- a
plain shapely negative buffer applied to the parcel boundary itself before
Step 1's search space is built, same mechanism/reasoning as production_area.py's
own PRODUCTION_BOUNDARY_SETBACK_METERS/compute_step1_eligible_cells() (see
test_canopy_height_data.py's own "Boundary setback" section for that
module's analogous coverage).

Round-number, hand-verifiable geometry throughout -- same "synthetic fixture
a human can check by hand" standard as test_tree_zone_search_space_buffers.py,
kept in its own dedicated file for the same "narrowly-scoped coverage for a
single function's new keyword argument" reasoning that file's own docstring
gives.

Inward (negative) buffering of a convex rectangle needs no corner rounding --
each edge simply shifts inward by the setback distance and the (still
right-angle) corners are exactly where the shifted edges meet -- so a
100x100 square shrunk by boundary_setback_meters on every side becomes
EXACTLY a (100 - 2*boundary_setback_meters) square, not merely a bounded
approximation the way the OUTWARD production/water buffers in
test_tree_zone_search_space_buffers.py are.
"""

from shapely.geometry import box

from raster_grid import SQUARE_METERS_PER_ACRE
from tree_zone_candidates import TREE_ZONE_BOUNDARY_SETBACK_METERS, compute_tree_search_space

assert TREE_ZONE_BOUNDARY_SETBACK_METERS == 5.0

boundary = box(0, 0, 100, 100)  # 10,000 sqm
production_square = box(40, 40, 60, 60)  # 20x20, 400 sqm, well clear of the boundary edge


# =====================================================================
# boundary_setback_meters=0.0: reproduces the pre-setback behavior EXACTLY --
# search_space is the boundary object itself when nothing else is claimed.
# =====================================================================

search_space_no_setback, claimed_no_setback = compute_tree_search_space(
    boundary, [], [], [], boundary_setback_meters=0.0
)
assert claimed_no_setback is None
assert search_space_no_setback is boundary, (
    "boundary_setback_meters=0.0 with nothing else claimed must return the boundary object itself, unmodified"
)
print("boundary_setback_meters=0.0 reproduces the pre-setback behavior exactly: search_space is the boundary itself.")


# =====================================================================
# Default boundary_setback_meters (TREE_ZONE_BOUNDARY_SETBACK_METERS, 5.0m),
# nothing else claimed: search_space is EXACTLY a (100 - 2*5)x(100 - 2*5) =
# 90x90 square, 8,100 sqm -- see module docstring for why inward buffering
# of a convex rectangle needs no rounding, unlike the outward production/
# water buffers.
# =====================================================================

search_space_default, claimed_default = compute_tree_search_space(boundary, [], [], [])
assert claimed_default is None, "boundary setback alone (nothing production/water/road-claimed) must leave claimed_union None"
expected_side = 100.0 - 2 * TREE_ZONE_BOUNDARY_SETBACK_METERS
expected_area = expected_side**2
assert search_space_default is not None and not search_space_default.is_empty
assert abs(search_space_default.area - expected_area) < 1e-6, (
    f"default boundary_setback_meters={TREE_ZONE_BOUNDARY_SETBACK_METERS}m should shrink a 100x100 boundary to "
    f"EXACTLY a {expected_side}x{expected_side} square ({expected_area} sqm), got {search_space_default.area} sqm"
)
print(
    f"Default boundary_setback_meters={TREE_ZONE_BOUNDARY_SETBACK_METERS}m shrinks a 100x100 boundary to exactly "
    f"a {expected_side}x{expected_side} square ({search_space_default.area} sqm), no rounding needed."
)


# =====================================================================
# Setback composes with an interior production claim: the setback shrinks
# the OUTER bound first, then the (buffer-free, production_buffer_meters=0.0)
# production square is subtracted from what's left -- same order Step 1's
# own module docstring describes (boundary shrunk first, then claimed
# geometry subtracted).
# =====================================================================

search_space_with_claim, claimed_with_claim = compute_tree_search_space(
    boundary, [production_square], [], [], production_buffer_meters=0.0
)
assert abs(claimed_with_claim.area - 400.0) < 1e-9, "the production square's own claimed area is unaffected by the boundary setback"
expected_area_with_claim = expected_area - 400.0
assert abs(search_space_with_claim.area - expected_area_with_claim) < 1e-6, (
    f"search space should be the setback-shrunk 90x90 boundary ({expected_area} sqm) minus the interior "
    f"20x20 production square (400 sqm) = {expected_area_with_claim} sqm, got {search_space_with_claim.area} sqm"
)
print(
    f"Boundary setback composes correctly with an interior production claim: search_space="
    f"{search_space_with_claim.area} sqm (setback-shrunk boundary minus the claimed square)."
)


# =====================================================================
# claimed_union is None iff production/water/road are ALL empty, unaffected
# by the boundary setback -- the setback changes what's ELIGIBLE, not
# whether anything has been "claimed" by an upstream layer.
# =====================================================================

assert claimed_no_setback is None and claimed_default is None, (
    "claimed_union must stay None when nothing is production/water/road-claimed, regardless of the boundary setback"
)
print("claimed_union stays None (not merely a smaller polygon) when nothing upstream has actually been claimed.")

print(
    f"\nAll compute_tree_search_space() boundary-setback checks passed "
    f"({(search_space_default.area / SQUARE_METERS_PER_ACRE):.4f} acres search space in the round-number "
    "no-other-claims fixture, for reference)."
)
