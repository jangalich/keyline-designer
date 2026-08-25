"""
_canopy_override_probe.py

Shared OFFLINE test probe for the pre-fetched-canopy override threaded
through production_area.get_required_tree_root_zone_mask_utm() and its
inner _fetch_tree_root_zone_mask_utm(). Imported by each caller's own test
file (test_production_area.py, test_production_area_ceiling.py, test_water_
candidate_zones.py, test_water_suitability.py, test_solar_suitability.py,
test_tree_zone_candidates.py, test_fencing.py) so the same forwarding
assertion is expressed identically everywhere instead of re-derived per
file.

The core mechanics (that a supplied canopy is used verbatim with zero
network fetches, and that the no-override path is byte-for-byte unchanged)
live in test_canopy_mask_override.py. THIS probe proves the caller layer:
that an entry point, handed canopy_height=<override>, forwards that exact
object all the way down to EVERY canopy consumer it reaches, so none of
them re-fetch canopy from the Planetary Computer.

Both patches are installed at production_area's OWN module bindings --
get_required_tree_root_zone_mask_utm() and _fetch_tree_root_zone_mask_utm()
look up get_canopy_height_for_boundary() and tree_root_zone_mask() there,
so a single probe observes every path into the shared fetch no matter how
deeply nested the caller's own entry point is (e.g. solar_suitability's
four independent canopy gates, water_candidate_zones' two).

ONE MORE BINDING, ADDED WHEN A SECOND PATH TO THE SAME LEAF APPEARED.
pipeline_context.build_pipeline_context() now fetches canopy itself when
its caller supplies none, and it reaches the leaf as a MODULE ATTRIBUTE
(canopy_height_data.get_canopy_height_for_boundary) rather than through
production_area's `from canopy_height_data import ...` copy. Those are two
independent names for one function and patching either alone leaves the
other invisible. Both are patched here, over one shared counter, so this
probe keeps the promise its name makes: it observes EVERY path into the
canopy fetch, and `fetch_calls` stays the number of real Planetary
Computer round-trips rather than the number that happened to go through
one module.
"""

import numpy as np

import canopy_height_data as chd
import production_area as pa


def clean_canopy_for(dem):
    """A clean (no-tree, all-below-threshold) canopy dict shaped exactly
    like canopy_height_data.get_canopy_height_for_boundary()'s own return
    value / parcel_data.ParcelData.canopy_height, sized to dem's grid.
    Clean so the caller's real pipeline behaves normally (no canopy
    exclusion wiping out its candidates) while it runs fully offline."""
    return {
        "array": np.full(dem["array"].shape, 1.0, dtype=np.float32),
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source_item_id": "offline-override-probe",
    }


class CanopyOverrideProbe:
    """Context manager. While active it (1) counts every get_canopy_height_
    for_boundary() call -- which MUST stay 0 when an override is forwarded,
    since a fetch means some path lost the override and fell back to the
    network -- and (2) records the array object handed to tree_root_zone_
    mask(), so a test can identity-check that the SUPPLIED override array,
    not a re-fetched or copied one, is what every reached canopy gate
    actually computed on. The real tree_root_zone_mask() still runs, so the
    caller's downstream behavior is genuinely exercised, not stubbed away.
    """

    def __init__(self):
        self.fetch_calls = 0
        self.mask_arrays = []
        self._orig_fetch = None
        self._orig_leaf_fetch = None
        self._orig_mask = None

    def _counting_fetch(self, boundary_coordinates, dem, *args, **kwargs):
        self.fetch_calls += 1
        return clean_canopy_for(dem)

    def __enter__(self):
        self._orig_fetch = pa.get_canopy_height_for_boundary
        self._orig_leaf_fetch = chd.get_canopy_height_for_boundary
        self._orig_mask = pa.tree_root_zone_mask
        real_mask = self._orig_mask
        recorder = self.mask_arrays

        def _capturing_mask(hag_array, resolution_meters, *args, **kwargs):
            recorder.append(hag_array)
            return real_mask(hag_array, resolution_meters, *args, **kwargs)

        pa.get_canopy_height_for_boundary = self._counting_fetch
        # The SAME counter on the module-attribute binding pipeline_context.py
        # reaches -- see this module's own docstring for why one is not enough.
        chd.get_canopy_height_for_boundary = self._counting_fetch
        pa.tree_root_zone_mask = _capturing_mask
        return self

    def __exit__(self, *exc):
        pa.get_canopy_height_for_boundary = self._orig_fetch
        chd.get_canopy_height_for_boundary = self._orig_leaf_fetch
        pa.tree_root_zone_mask = self._orig_mask
        return False

    def assert_override_used(self, override, label):
        """Assert the forwarded override was honored end-to-end: zero canopy
        fetches, at least one canopy gate actually ran (so the identity check
        isn't vacuous), and EVERY gate that ran computed on the exact supplied
        override array object."""
        assert self.fetch_calls == 0, (
            f"{label}: with canopy_height supplied, get_canopy_height_for_boundary() must be "
            f"called ZERO times (every canopy consumer must use the forwarded override), "
            f"but it was fetched {self.fetch_calls} time(s)"
        )
        assert len(self.mask_arrays) >= 1, (
            f"{label}: expected the shared canopy gate to run at least once so the identity "
            f"check is meaningful, but tree_root_zone_mask() was never reached"
        )
        assert all(arr is override["array"] for arr in self.mask_arrays), (
            f"{label}: every canopy gate reached ({len(self.mask_arrays)} total) must compute on "
            f"the EXACT supplied override array object, not a re-fetched or copied one"
        )
