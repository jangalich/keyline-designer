"""
test_canopy_mask_override.py

Offline (no-network) checks for the OPTIONAL pre-fetched-canopy override
threaded through production_area._fetch_tree_root_zone_mask_utm() and
production_area.get_required_tree_root_zone_mask_utm() -- the shared
"fetch canopy, or fail hard" building block behind the mandatory woody-
vegetation gate every entry point in this pipeline applies.

The override lets a context-aware caller that already holds the parcel's
canopy (e.g. parcel_data.ParcelData.canopy_height, the SAME dict
canopy_height_data.get_canopy_height_for_boundary() returns) pass it
straight through instead of every one of these functions independently
re-fetching canopy from the Planetary Computer. This file proves, at the
shared core, the two properties the whole branch rests on:

  1. canopy_height SUPPLIED  -> get_canopy_height_for_boundary() is called
     ZERO times, and the exact array object supplied is what reaches the
     mask computation (identity-checked, not just equal-valued).
  2. canopy_height OMITTED   -> unchanged from before this branch: canopy
     IS fetched, and the resulting mask is byte-for-byte identical to the
     supplied-override case built from that same fetched canopy.

Per-caller forwarding (identify_production_areas(), identify_optimized_
production_areas(), identify_tree_zone_candidates(), identify_water_
suitability(), identify_water_system_candidate_zones(), identify_solar_
candidate_zones(), identify_boundary_fencing()) is proven in each of
those functions' own existing test files, grafted onto their existing
offline fixtures. This file owns the shared core the forwarding all lands
on.

Pure logic against a small synthetic DEM built by hand, same "independent
of real data fetches" philosophy as the rest of this pipeline's tests.
"""

import numpy as np
from shapely.geometry import box

import production_area as pa
from canopy_height_data import CANOPY_HEIGHT_THRESHOLD_METERS
from production_area import (
    _fetch_tree_root_zone_mask_utm,
    get_required_tree_root_zone_mask_utm,
)

# --- Synthetic DEM + boundary, matching the conventions the other
# production_area tests already use (real UTM crs so the real WGS84
# reprojection inside get_required_tree_root_zone_mask_utm() runs
# offline, exactly as in production). ---
RESOLUTION = (5.0, 5.0)
_ROWS, _COLS = 20, 20
_DEM_ARRAY = np.zeros((_ROWS, _COLS), dtype=np.float32)
DEM = {
    "array": _DEM_ARRAY,
    "resolution_meters": RESOLUTION,
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}
BOUNDARY_UTM = box(
    500000.0,
    4500000.0 - _ROWS * 5.0,
    500000.0 + _COLS * 5.0,
    4500000.0,
)


def _canopy_dict(hag_array):
    """A canopy dict shaped exactly like get_canopy_height_for_boundary()'s
    own return value (and parcel_data.ParcelData.canopy_height) -- only
    'array'/'resolution_meters' are read downstream, the rest mirror the
    real shape so this is a faithful stand-in, not a minimal stub."""
    return {
        "array": hag_array,
        "resolution_meters": DEM["resolution_meters"],
        "origin_x": DEM["origin_x"],
        "origin_y": DEM["origin_y"],
        "crs": DEM["crs"],
        "source_item_id": "offline-test-stub",
    }


# A canopy array with a real, checkable patch of trees in it (not all-clean)
# so the derived mask is non-trivial -- an all-below-threshold array would
# make every mask identically all-False and hide a bug that silently
# swapped which array was used. Half the grid is well above threshold.
_TREE_HAG = np.full((_ROWS, _COLS), 1.0, dtype=np.float32)
_TREE_HAG[: _ROWS // 2, :] = CANOPY_HEIGHT_THRESHOLD_METERS + 10.0
OVERRIDE_CANOPY = _canopy_dict(_TREE_HAG)


class _CountingCanopyFetch:
    """Stands in for get_canopy_height_for_boundary(): counts calls and
    returns a copy-shaped clean canopy so the no-override path stays fully
    offline. Distinct object identity from any override lets the identity
    assertions below actually mean something."""

    def __init__(self, hag_array):
        self.calls = 0
        self._canopy = _canopy_dict(hag_array)

    def __call__(self, boundary_coordinates, dem, *args, **kwargs):
        self.calls += 1
        return self._canopy


class _CapturingMask:
    """Wraps the real tree_root_zone_mask() and records the array object it
    was handed, so tests can identity-check WHICH canopy array actually
    reached the mask computation -- while still returning the real mask so
    downstream behavior is genuinely unchanged."""

    def __init__(self, real):
        self._real = real
        self.arrays = []

    def __call__(self, hag_array, resolution_meters, *args, **kwargs):
        self.arrays.append(hag_array)
        return self._real(hag_array, resolution_meters, *args, **kwargs)


# ===========================================================================
# _fetch_tree_root_zone_mask_utm() -- the underlying fetch/threshold/dilate
# ===========================================================================
# Reproject the boundary the same way get_required_tree_root_zone_mask_utm()
# does, so the inner function gets the lon/lat list it actually expects.
from rasterio.warp import transform as _warp_transform  # noqa: E402

_xs, _ys = BOUNDARY_UTM.exterior.coords.xy
_lons, _lats = _warp_transform(DEM["crs"], "EPSG:4326", list(_xs), list(_ys))
BOUNDARY_COORDS = list(zip(_lons, _lats))

# 1a. Override supplied -> zero fetches, supplied array is what's used.
_orig_fetch = pa.get_canopy_height_for_boundary
_orig_mask = pa.tree_root_zone_mask
spy_fetch = _CountingCanopyFetch(np.full((_ROWS, _COLS), 1.0, dtype=np.float32))
cap_mask = _CapturingMask(_orig_mask)
pa.get_canopy_height_for_boundary = spy_fetch
pa.tree_root_zone_mask = cap_mask
try:
    mask_supplied = _fetch_tree_root_zone_mask_utm(
        BOUNDARY_COORDS, DEM, canopy_height=OVERRIDE_CANOPY
    )
finally:
    pa.get_canopy_height_for_boundary = _orig_fetch
    pa.tree_root_zone_mask = _orig_mask

assert spy_fetch.calls == 0, (
    "_fetch_tree_root_zone_mask_utm(): with canopy_height supplied, "
    f"get_canopy_height_for_boundary() must be called ZERO times, was called {spy_fetch.calls}"
)
assert len(cap_mask.arrays) == 1 and cap_mask.arrays[0] is OVERRIDE_CANOPY["array"], (
    "_fetch_tree_root_zone_mask_utm(): the SUPPLIED canopy['array'] object itself must be "
    "what reaches tree_root_zone_mask(), not a re-fetched or copied array"
)
print(
    "_fetch_tree_root_zone_mask_utm(): canopy_height override -> 0 fetches, and the exact "
    "supplied array object (identity-checked) is what the mask computation ran on."
)

# 1b. No override -> exactly one fetch, and the mask equals the one built
#     directly from that fetched canopy (regression: sourcing is the only
#     thing that changed).
spy_fetch2 = _CountingCanopyFetch(_TREE_HAG.copy())
pa.get_canopy_height_for_boundary = spy_fetch2
try:
    mask_fetched = _fetch_tree_root_zone_mask_utm(BOUNDARY_COORDS, DEM)
finally:
    pa.get_canopy_height_for_boundary = _orig_fetch

assert spy_fetch2.calls == 1, (
    "_fetch_tree_root_zone_mask_utm(): with NO override, get_canopy_height_for_boundary() "
    f"must be fetched exactly once, was called {spy_fetch2.calls}"
)
# The fetched canopy (spy_fetch2) carries the SAME tree HAG values as
# OVERRIDE_CANOPY, so the derived masks must be byte-for-byte identical --
# proving the override changes only WHERE canopy comes from, never what the
# fetch/threshold/dilate logic does with it.
assert np.array_equal(mask_supplied, mask_fetched), (
    "_fetch_tree_root_zone_mask_utm(): the override path and the fetch path must produce an "
    "IDENTICAL mask when fed the same canopy values -- the override must not alter the logic"
)
print(
    "_fetch_tree_root_zone_mask_utm(): no override -> exactly 1 fetch, and the resulting mask is "
    "byte-for-byte identical to the supplied-override mask built from the same canopy."
)

# 1c. A canopy dict that is genuinely present but supplied as the override
#     still yields a real non-trivial mask (the tree half dilates outward) --
#     guards against the "override silently produced an all-False no-op" trap.
assert mask_supplied.any() and not mask_supplied.all(), (
    "the tree-half override must yield a real, partial root-zone mask (some True, some False), "
    "confirming the supplied canopy was actually thresholded/dilated, not ignored"
)
print(
    "_fetch_tree_root_zone_mask_utm(): the supplied tree-half canopy yields a real partial mask "
    "(some cells True, some False) -- the override is genuinely computed on, not no-op'd."
)


# ===========================================================================
# get_required_tree_root_zone_mask_utm() -- the shared fetch-or-raise wrapper
# ===========================================================================
# 2a. Override supplied -> zero fetches, supplied array reaches the mask,
#     buffer_meters still honored independently of the override.
spy_fetch3 = _CountingCanopyFetch(np.full((_ROWS, _COLS), 1.0, dtype=np.float32))
cap_mask3 = _CapturingMask(_orig_mask)
pa.get_canopy_height_for_boundary = spy_fetch3
pa.tree_root_zone_mask = cap_mask3
try:
    required_supplied = get_required_tree_root_zone_mask_utm(
        BOUNDARY_UTM, DEM, buffer_meters=12.0, canopy_height=OVERRIDE_CANOPY
    )
finally:
    pa.get_canopy_height_for_boundary = _orig_fetch
    pa.tree_root_zone_mask = _orig_mask

assert spy_fetch3.calls == 0, (
    "get_required_tree_root_zone_mask_utm(): with canopy_height supplied, "
    f"get_canopy_height_for_boundary() must be called ZERO times, was called {spy_fetch3.calls}"
)
assert len(cap_mask3.arrays) == 1 and cap_mask3.arrays[0] is OVERRIDE_CANOPY["array"], (
    "get_required_tree_root_zone_mask_utm(): the SUPPLIED canopy['array'] object itself must "
    "reach tree_root_zone_mask() through the wrapper, unchanged"
)
assert required_supplied is not None
print(
    "get_required_tree_root_zone_mask_utm(): canopy_height override -> 0 fetches, exact supplied "
    "array object reaches the mask computation, and a real mask is returned (no RuntimeError)."
)

# 2b. No override -> exactly one fetch; identical mask to the override path
#     at the SAME buffer_meters (regression against pre-branch behavior).
spy_fetch4 = _CountingCanopyFetch(_TREE_HAG.copy())
pa.get_canopy_height_for_boundary = spy_fetch4
try:
    required_fetched = get_required_tree_root_zone_mask_utm(BOUNDARY_UTM, DEM, buffer_meters=12.0)
finally:
    pa.get_canopy_height_for_boundary = _orig_fetch

assert spy_fetch4.calls == 1, (
    "get_required_tree_root_zone_mask_utm(): with NO override, canopy must be fetched exactly "
    f"once, was called {spy_fetch4.calls}"
)
assert np.array_equal(required_supplied, required_fetched), (
    "get_required_tree_root_zone_mask_utm(): override and fetch paths must produce an IDENTICAL "
    "mask when fed the same canopy values and the same buffer_meters"
)
print(
    "get_required_tree_root_zone_mask_utm(): no override -> exactly 1 fetch, mask byte-for-byte "
    "identical to the supplied-override mask at the same buffer_meters."
)

# 2c. The hard-fail contract is unchanged when NO override is supplied and
#     the fetch genuinely returns no coverage -> RuntimeError, exactly as
#     before this branch (the override adds a capability, it does not soften
#     the mandatory gate).
def _no_coverage_fetch(boundary_coordinates, dem, *args, **kwargs):
    return None


pa.get_canopy_height_for_boundary = _no_coverage_fetch
try:
    raised = False
    try:
        get_required_tree_root_zone_mask_utm(BOUNDARY_UTM, DEM)
    except RuntimeError:
        raised = True
finally:
    pa.get_canopy_height_for_boundary = _orig_fetch
assert raised, (
    "get_required_tree_root_zone_mask_utm(): with no override and no canopy coverage, the "
    "mandatory gate must still raise RuntimeError -- the override must not weaken hard-fail"
)
print(
    "get_required_tree_root_zone_mask_utm(): no override + no coverage still raises RuntimeError "
    "-- the mandatory woody-vegetation gate is unchanged by the new capability."
)

print("\nAll canopy_mask_override checks passed.")
