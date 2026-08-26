"""
test_diagnose_water_zone_mask.py

Offline (no-network) coverage for diagnose_water_zone_mask.py's own
INVOCATION PATH -- main(), called the way __main__ calls it, with only the
network fetches stubbed.

WHY THIS FILE EXISTS. A NameError shipped in diagnose_water_zone_mask.py
that no test caught, and the shape of the gap is worth stating because it
is a general one:

    the suite tested the PIPELINE path (build_pipeline_context ->
    identify_water_suitability) and it tested each helper on the TEST'S
    OWN path (calling _export_candidate_geojson() directly with
    hand-assembled arguments), but nothing ever invoked the module the way
    its real consumer does.

Both of those pass while main() itself is broken, because neither one runs
main(). The bug was a scoping mistake -- code moved across a function
boundary kept reading names that only existed in the scope it left -- and
that is exactly the class of defect that unit-testing helpers in isolation
cannot see, since every helper was individually fine. Two distinct
symptoms came from the one mistake:

    1. _report_confluence_check() read `valleys`, which had been hoisted
       up into main() without being threaded back down. This is the
       NameError that surfaced (as "Request failed: ..." through the
       script's own fetch wrapper).
    2. The whole GeoJSON export block had been appended to the END of
       _report_confluence_check() rather than the end of main(), so it
       read six more of main()'s locals. It never ran at all, and would
       have raised its own NameError the moment (1) was fixed.

So the assertions below are deliberately about main() COMPLETING and
about what it PRODUCED, not about any helper's return value.

The DEM is synthetic but covers the real reference boundary's own extent,
so main()'s boundary reprojection and every downstream call receive
coherent coordinates. Only the network is stubbed: the DEM fetch, the
canopy fetch, the road fetch and the SSURGO calls. main()'s control flow,
the real generation call, the real scoring call and the real export all
run untouched.

Run: python3 test_diagnose_water_zone_mask.py
"""

import json
import os
import tempfile
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform

import dem_data
import diagnose_water_zone_mask as diag
import keypoint_detection as kd
import production_area as pa
import soil_data
import valley_delineation as vd
import water_candidate_zones as wcz
import water_suitability as ws
from dem_data import _utm_epsg_for_lonlat
from feature_schema import validate_feature_collection

# --- a synthetic DEM over the real reference boundary's own extent ------
_LONS = [p[0] for p in diag.PROPERTY_BOUNDARY]
_LATS = [p[1] for p in diag.PROPERTY_BOUNDARY]
_EPSG = _utm_epsg_for_lonlat(sum(_LONS) / len(_LONS), sum(_LATS) / len(_LATS))
CRS = f"EPSG:{_EPSG}"
_xs, _ys = warp_transform("EPSG:4326", CRS, _LONS, _LATS)

RESOLUTION = 10.0
_PAD = 60.0
_ORIGIN_X = min(_xs) - _PAD
_ORIGIN_Y = max(_ys) + _PAD
_WIDTH = int((max(_xs) - min(_xs) + 2 * _PAD) / RESOLUTION) + 1
_HEIGHT = int((max(_ys) - min(_ys) + 2 * _PAD) / RESOLUTION) + 1

# Two drainage columns falling south, so generation has real channels to
# nominate on and the confluence check has real branches to trace.
_array = np.zeros((_HEIGHT, _WIDTH), dtype=np.float64)
for _r in range(_HEIGHT):
    for _c in range(_WIDTH):
        _array[_r, _c] = 100.0 - 0.1 * _r + 1.0 * min(
            abs(_c - _WIDTH // 4), abs(_c - 3 * _WIDTH // 4)
        )
SYNTHETIC_DEM = {
    "array": _array,
    "resolution_meters": (RESOLUTION, RESOLUTION),
    "origin_x": _ORIGIN_X,
    "origin_y": _ORIGIN_Y,
    "crs": CRS,
}


def _fake_canopy(boundary_coordinates, dem):
    """Below the threshold everywhere -- no trees. The canopy gate on this
    path is fetch-or-RAISE by design, so it has to return something real."""
    return {
        "array": np.full(dem["array"].shape, 1.0, dtype=np.float32),
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source_item_id": "offline-test-stub",
    }


def _no_roads(boundary_coordinates, dem, buffer_meters=None):
    return None


def _no_soil(*args, **kwargs):
    return []


# delineate_valleys is looked up in FOUR modules on this path (the
# diagnostic's own import, plus keypoint_detection's, water_candidate_
# zones's and water_suitability's). Every one is counted, because a
# forward that fixes only some of them fixes nothing -- and because the
# whole point of threading `valleys` through main() rather than letting
# each callee self-compute is that the total stays at one.
_LOOKUP_SITES = (diag, kd, wcz, ws)
_valley_calls = {"n": 0}
_real_delineate = vd.delineate_valleys


def _counting_delineate(dem):
    _valley_calls["n"] += 1
    return _real_delineate(dem)


_export_dir = tempfile.mkdtemp()
_export_path = os.path.join(_export_dir, "water_candidates.geojson")

_patches = [
    mock_patch.object(dem_data, "get_dem_for_boundary", return_value=SYNTHETIC_DEM),
    mock_patch.object(diag, "get_dem_for_boundary", return_value=SYNTHETIC_DEM),
    mock_patch.object(pa, "get_canopy_height_for_boundary", _fake_canopy),
    mock_patch.object(pa, "get_road_exclusion_union_utm", _no_roads),
    mock_patch.object(soil_data, "get_soil_data_for_polygon", _no_soil),
    mock_patch.object(soil_data, "get_soil_geometries_for_polygon", _no_soil),
    mock_patch.object(pa, "get_soil_data_for_polygon", _no_soil),
    mock_patch.object(pa, "get_soil_geometries_for_polygon", _no_soil),
    # Write the export somewhere disposable instead of the working
    # directory -- the module constant is what main() passes, so patching
    # it is patching the real default rather than bypassing it.
    mock_patch.object(diag, "WATER_CANDIDATES_GEOJSON_PATH", _export_path),
] + [mock_patch.object(m, "delineate_valleys", side_effect=_counting_delineate) for m in _LOOKUP_SITES]

for _p in _patches:
    _p.start()
try:
    # EXACTLY how __main__ invokes it: same entry point, same kwarg shape.
    # No try/except around it -- a raise here IS the failure, and the
    # traceback is the report.
    diag.main(max_contributing_acres=diag.MAX_VALLEY_CONTRIBUTING_AREA_ACRES)
finally:
    for _p in reversed(_patches):
        _p.stop()

print(
    "diagnose_water_zone_mask.main() runs to completion through its OWN invocation path "
    "(same entry point and kwarg shape as __main__), with only the network stubbed."
)


# --- the call-count instrument, on the DIAGNOSTIC's standalone path -----
#
# EXACTLY ONE, not two. main() delineates once at the top and forwards the
# one list into detect_keypoints(), find_candidate_zones() and
# _report_confluence_check(). Before the fix _report_confluence_check()
# delineated its own second copy; the hoist that removed that call is what
# left the NameError behind, so this count and that bug are two faces of
# the same change and belong in one assertion.
assert _valley_calls["n"] == 1, (
    f"delineate_valleys() must run EXACTLY ONCE across the diagnostic's whole standalone path -- "
    f"counted across all {len(_LOOKUP_SITES)} module lookup sites. Saw {_valley_calls['n']}: more than "
    "one means a callee is self-computing instead of reading the forwarded list; zero means the "
    "counter is not patched where the call actually happens."
)
print(
    f"delineate_valleys() runs exactly {_valley_calls['n']} time on the diagnostic's standalone path, "
    f"counted across all {len(_LOOKUP_SITES)} module lookup sites "
    f"({', '.join(m.__name__ for m in _LOOKUP_SITES)})."
)


# --- main() actually PRODUCED the export ---------------------------------
#
# The second half of the bug: the export block sat in the wrong function
# and never ran. Asserting that main() did not raise would NOT have caught
# that on its own -- the block was unreachable dead code inside a function
# whose earlier line raised first. So the file it writes is asserted here
# as a product of the run, not as a helper's return value.
assert os.path.exists(_export_path), (
    "main() must WRITE the GeoJSON export -- a run that completes without producing it is the second "
    "half of the bug this file exists for, and it would pass a raises-nothing assertion"
)
with open(_export_path, encoding="utf-8") as _handle:
    _collection = json.load(_handle)
validate_feature_collection(_collection)
assert _collection["features"], "the export must carry real features on a run that produced candidates"

_layers = {f["properties"]["layer"] for f in _collection["features"]}
assert "water_candidate_zone" in _layers, _layers
for _feature in _collection["features"]:
    assert _feature["properties"]["status"] in ("nominated", "dropped"), _feature["id"]

_survivors = [f for f in _collection["features"] if f["properties"]["layer"] == "water_candidate_zone"]
assert _survivors, "expected at least one surviving candidate on this synthetic parcel"
for _feature in _survivors:
    for _key in ("rank", "suitability_score", "basin_shape_factor", "production_overlap_pct", "stations"):
        assert _key in _feature["properties"], f"{_feature['id']} is missing {_key}"
assert {f["properties"]["rank"] for f in _survivors} == set(range(1, len(_survivors) + 1))
print(
    f"main() wrote a feature_schema-valid export: {len(_collection['features'])} feature(s) across "
    f"layers {sorted(_layers)}, with {len(_survivors)} ranked survivor(s) carrying scores, basin "
    "sub-scores, overlaps and station tables."
)


# --- the scoping regression, asserted structurally as well as behaviourally ---
#
# Behavioural coverage above is the real protection. This adds a cheap
# static guard for the SAME class of defect: a top-level function that
# reads a name it never binds, which is neither a parameter, a local, nor
# a module global. Both symptoms of this bug were exactly that, and the
# check costs nothing.
import ast  # noqa: E402
import builtins  # noqa: E402

_tree = ast.parse(open(diag.__file__, encoding="utf-8").read())
_module_names = set(dir(builtins))
for _node in _tree.body:
    if isinstance(_node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        _module_names.add(_node.name)
    elif isinstance(_node, (ast.Import, ast.ImportFrom)):
        for _alias in _node.names:
            _module_names.add((_alias.asname or _alias.name).split(".")[0])
    elif isinstance(_node, (ast.Assign, ast.AnnAssign)):
        _targets = [_node.target] if isinstance(_node, ast.AnnAssign) else _node.targets
        for _target in _targets:
            for _n in ast.walk(_target):
                if isinstance(_n, ast.Name):
                    _module_names.add(_n.id)

_free_by_function = {}
for _fn in [n for n in _tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
    _bound = {a.arg for a in _fn.args.args + _fn.args.kwonlyargs + _fn.args.posonlyargs}
    if _fn.args.vararg:
        _bound.add(_fn.args.vararg.arg)
    if _fn.args.kwarg:
        _bound.add(_fn.args.kwarg.arg)
    _loaded = []
    for _n in ast.walk(_fn):
        if isinstance(_n, ast.Name):
            if isinstance(_n.ctx, (ast.Store, ast.Del)):
                _bound.add(_n.id)
            else:
                _loaded.append(_n.id)
        elif isinstance(_n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _n is not _fn:
            _bound.add(_n.name)
            _bound.update(a.arg for a in _n.args.args + _n.args.kwonlyargs + _n.args.posonlyargs)
        elif isinstance(_n, ast.Lambda):
            _bound.update(a.arg for a in _n.args.args + _n.args.kwonlyargs + _n.args.posonlyargs)
        elif isinstance(_n, ast.ClassDef):
            _bound.add(_n.name)
        elif isinstance(_n, ast.ExceptHandler) and _n.name:
            _bound.add(_n.name)
        elif isinstance(_n, (ast.Import, ast.ImportFrom)):
            for _alias in _n.names:
                _bound.add((_alias.asname or _alias.name).split(".")[0])
        elif isinstance(_n, ast.comprehension):
            for _t in ast.walk(_n.target):
                if isinstance(_t, ast.Name):
                    _bound.add(_t.id)
    _free = sorted(set(_loaded) - _bound - _module_names)
    if _free:
        _free_by_function[f"{_fn.name}():{_fn.lineno}"] = _free

assert not _free_by_function, (
    "a function reads a name it never binds and that is not a module global -- this is the exact "
    f"shape of the bug this file exists for: {_free_by_function}"
)
print(
    f"Static scope guard: none of diagnose_water_zone_mask.py's "
    f"{len([n for n in _tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))])} top-level "
    "functions reads an unbound name -- the same check would have caught both symptoms of this bug at "
    "import time."
)

print("\nAll diagnose_water_zone_mask checks passed.")
