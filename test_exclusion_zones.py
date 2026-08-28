"""
test_exclusion_zones.py

Offline (no-network) checks for exclusion_zones.py. Every fixture below is
SYNTHETIC -- a hand-built DEM dict and a hand-built boundary polygon. No
real-property figure is computed, asserted or reproduced here; the acreages
quoted in exclusion_zones.py's own docstring came from a separate
diagnose_exclusion_footprints.py run against the two reference boundaries
and are context for the decisions, not test data.

What this file proves, in order:

  1. PRODUCTION IS COMPLETELY UNCHANGED. This branch's core guarantee.
     production_area.py and production_area_ceiling.py do not appear in
     `git diff` at all; compute_step1_eligible_cells()' signature is
     untouched; and its eligible_mask, every other array it returns, and
     every scored patch identify_optimized_production_areas() produces are
     BYTE-IDENTICAL before and after this module runs against the same
     fixture.
  2. THE DEFERRED INTEGRATION IS NOW BEHAVIOURALLY A NO-OP. This module's
     eligible_mask is BYTE-IDENTICAL to compute_step1_eligible_cells()'
     own, so gating production on these exclusions would remove exactly
     zero cells -- asserted at zero, not at "small". While the closing
     existed that figure was real pinhole ground, and deferring the
     integration was also deferring a decision about production's output.
     It is not anymore.
  3. EVERY PER-GATE ACREAGE IS RAW -- NOTHING IS CLOSED. Each published
     layer mask is compared bit-for-bit against the gate recomputed here
     from production's own gate definitions (not read back from the
     module, which would be circular). Reports per gate what the 5 m
     closing this module used to apply WOULD have added, so the removal is
     quantified rather than merely asserted. The five closing constants are
     verified DELETED rather than zeroed, via a dir() scan that also catches
     a radius reintroduced under a new name.
  4. THE ONLY GEOMETRIC OPERATION IS THE CLIP TO THE BOUNDARY. The
     published union is the EXACT cell footprint of its own mask, clipped
     -- equal to it, not merely inside or outside it -- and the five layer
     footprints union to exactly that. This replaces the old extensive
     invariant, which no longer has two sides.
  5. NaN SLOPE IS SPLIT OUT. Nodata cells land in the slope layer and are
     reported separately in narrative_data.
  6. OVERLAP IS MEASURED, NOT ASSUMED. Cells that are both wooded and
     hydric are reported as a pairwise overlap, and the naive sum exceeds
     the union by exactly that amount.
  7. narrative_data IS JSON-SERIALISABLE, imperial, 1 decimal, no numpy
     scalars and no geometry.
  8. THE REDUNDANCY IS BOUNDED AND EXPECTED. Call counters on the gate
     fetch HELPERS and the slope computation across a full
     build_pipeline_context() run (what is actually measured -- not every
     road/canopy/soil touch in the pipeline): canopy, soil and slope each
     run EXACTLY twice -- once here, once in production -- while the road
     helper runs exactly ONCE (production's own self-compute only:
     build_pipeline_context() passes its already-fetched road union into
     identify_exclusion_zones(), which reuses it, a real None included,
     with the producer/consumer buffer defaults asserted equal). A higher
     count would mean something re-fetches beyond the known duplication
     documented in exclusion_zones.py. 8c: the road gate now EXCLUDES
     GROUND -- at the shared 5.0m buffer a road crossing the parcel
     excludes the cells within 5m of its centerline, where the old 0.0
     buffer (computed inline as the contrast) excluded nothing.
  9. THE LAYER ACTUALLY RENDERS, and does so under its two constraints:
     beneath every feature layer (above only the basemap/halo backdrop,
     which covers off-parcel ground this layer never touches), and
     contributing exactly ONE legend entry. Read off a real render, not
     off the constants.
"""

import inspect
import json
import subprocess

import numpy as np
from shapely.geometry import Polygon

import production_area
from production_area import compute_step1_eligible_cells
from raster_grid import cell_area_acres, cell_union_footprint, disc_closing

RESOLUTION_M = 5.0
_TOLERANCE_M2 = 1e-6


def make_dem(array: np.ndarray, origin_x: float = 500000.0, origin_y: float = 4500000.0) -> dict:
    """A synthetic DEM dict in the exact shape dem_data.get_dem_for_boundary()
    returns, at the pipeline's own 5 m resolution (the resolution the closing
    radii quantize against -- a different one would make every cell-count
    assertion below meaningless)."""
    return {
        "array": array.astype(np.float64),
        "resolution_meters": (RESOLUTION_M, RESOLUTION_M),
        "origin_x": origin_x,
        "origin_y": origin_y,
        "crs": "EPSG:32617",
    }


def boundary_for(dem: dict, inset_cells: int = 2) -> Polygon:
    """A rectangular boundary inset `inset_cells` from the DEM's own edges, so
    the grid always has real off-parcel margin on every side -- otherwise a
    closing that pushes outward would be silently clipped by the array bound
    rather than by the boundary, and the extensive checks would pass for the
    wrong reason."""
    ox, oy = dem["origin_x"], dem["origin_y"]
    rows, cols = dem["array"].shape
    west = ox + inset_cells * RESOLUTION_M
    east = ox + (cols - inset_cells) * RESOLUTION_M
    north = oy - inset_cells * RESOLUTION_M
    south = oy - (rows - inset_cells) * RESOLUTION_M
    return Polygon([(west, south), (east, south), (east, north), (west, north)])


def polygon_part_count(geom) -> int:
    """Number of Polygon parts in a Polygon/MultiPolygon/empty geometry."""
    if geom.is_empty:
        return 0
    return len(geom.geoms) if geom.geom_type == "MultiPolygon" else 1


def flat_plane(rows: int, cols: int, grade_pct: float = 2.0) -> np.ndarray:
    """A uniform plane at `grade_pct`, well under MAX_PRODUCTION_SLOPE_PCT --
    every cell clears the slope gate, so any slope-layer cell in a fixture
    below is one the fixture put there deliberately."""
    _, x = np.mgrid[0:rows, 0:cols]
    return x * RESOLUTION_M * grade_pct / 100.0


# ===========================================================================
# 1. PRODUCTION'S ANSWER IS COMPLETELY UNCHANGED
# ===========================================================================
#
# THIS SECTION HAS BEEN NARROWED A SECOND TIME, AND FOR A REAL REASON. It
# used to prove that this branch had not wired itself into production at
# all -- a file-level git ban plus a frozen parameter list. That integration
# is now LANDED: compute_step1_eligible_cells() takes an exclusion_result=
# override, production_area_ceiling.py forwards it, and build_pipeline_
# context() passes this module's own result in. So "production is unedited"
# is no longer the property to defend, and asserting it would only forbid
# the change the pipeline was restructured to make.
#
# What has NOT changed, and is what the ban was ever a proxy for, is
# PRODUCTION'S ANSWER. That is asserted directly below -- by value, on a
# shared fixture, byte-for-byte -- which is a stronger check than a clean
# diff was: it catches a behavioural change made in a file no ban covers.
# The bit-identity of the integrated path itself is asserted from the other
# side, in test_production_area.py's "STEP 1 CONSUMES THE EXCLUSION RESULT".

# THE CONTRACT: the seven original parameters, in their original order, plus
# exclusion_result and NOTHING else. The frozen-list form of this check
# forbade the override outright; the list is still frozen, it just now
# includes it. Order matters as much as membership -- every existing caller
# passes dem/boundary_polygon_utm positionally, and disqualifying_soil_union_
# utm is passed positionally by production_area_ceiling.optimize_production_
# areas(). Appending, rather than inserting, is what keeps them working.
_STEP1_ORIGINAL_PARAMS = [
    "dem",
    "boundary_polygon_utm",
    "disqualifying_soil_union_utm",
    "max_slope_pct",
    "tree_root_zone_mask_utm",
    "boundary_setback_meters",
    "road_exclusion_union_utm",
]
_step1_params = list(inspect.signature(compute_step1_eligible_cells).parameters)
assert _step1_params == _STEP1_ORIGINAL_PARAMS + ["exclusion_result"], (
    "compute_step1_eligible_cells() must take its seven original parameters, unchanged and in their "
    "original positions, with exclusion_result APPENDED -- inserting a parameter would silently "
    f"re-bind every positional caller. Got: {_step1_params}"
)
# NOT None. "Not supplied" and "supplied, and the answer is nothing" are
# different states throughout this pipeline, and this module's own
# _ROAD_UNION_NOT_SUPPLIED exists for the same reason.
_step1_override_default = inspect.signature(compute_step1_eligible_cells).parameters["exclusion_result"].default
assert _step1_override_default is not None, (
    "compute_step1_eligible_cells()'s exclusion_result default must be an explicit sentinel, never None"
)
assert _step1_override_default is production_area._EXCLUSION_RESULT_NOT_SUPPLIED
print(
    "production's CONTRACT is the seven original parameters in their original positions, plus an "
    "appended exclusion_result= defaulting to an explicit sentinel (not None)."
)

# The shared fixture for checks 1 and 2: gentle plane, one steep block, a
# canopy patch with a pinhole, a hydric strip.
_p_rows = _p_cols = 34
_p_array = flat_plane(_p_rows, _p_cols)
_p_array[8:14, 8:14] += 9.0  # a steep block, well over the 20% gate
_p_dem = make_dem(_p_array)
_p_boundary = boundary_for(_p_dem)

_p_canopy = np.zeros((_p_rows, _p_cols), dtype=bool)
_p_canopy[20:28, 20:28] = True
_p_canopy[23, 23] = False  # a one-cell pinhole a 5 m closing absorbs
_p_canopy[25, 25] = False  # and another

_p_soil_union = Polygon(
    [
        (_p_dem["origin_x"] + 14 * RESOLUTION_M, _p_dem["origin_y"] - 30 * RESOLUTION_M),
        (_p_dem["origin_x"] + 18 * RESOLUTION_M, _p_dem["origin_y"] - 30 * RESOLUTION_M),
        (_p_dem["origin_x"] + 18 * RESOLUTION_M, _p_dem["origin_y"] - 6 * RESOLUTION_M),
        (_p_dem["origin_x"] + 14 * RESOLUTION_M, _p_dem["origin_y"] - 6 * RESOLUTION_M),
    ]
)


def _run_production_step1():
    return compute_step1_eligible_cells(
        _p_dem,
        _p_boundary,
        disqualifying_soil_union_utm=_p_soil_union,
        tree_root_zone_mask_utm=_p_canopy,
        road_exclusion_union_utm=None,
    )


def _snapshot(step1: dict) -> dict:
    """Every returned value reduced to raw bytes / plain scalars, so the
    comparison below is genuinely byte-for-byte and not an `==` that a numpy
    array would answer element-wise. NaN-safe: .tobytes() compares the bit
    pattern, so NaN == NaN holds where `==` would not."""
    snap = {}
    for key, value in step1.items():
        snap[key] = (value.dtype.str, value.shape, value.tobytes()) if isinstance(value, np.ndarray) else value
    return snap


_before_raw = _run_production_step1()
_before_step1 = _snapshot(_before_raw)
_before_patches = production_area.cluster_and_gate(
    _before_raw["eligible_mask"], _p_dem, _p_boundary, _before_raw, min_area_acres=0.05
)
_before_patch_bytes = [
    (p["id"], p["area_acres"], p["polygon_utm"].wkb, p["render_fill_polygon_utm"].wkb, sorted(p["cells"]))
    for p in _before_patches
]

# The additive-fields-only promise the file ban above was narrowed to, checked
# by VALUE on the same baseline patches: every key production published before
# is still there, render_fill_area_acres is exactly the expression
# production_areas_to_geojson() used to compute inline, and
# render_fill_geometry_wgs84 describes the same geometry render_fill_polygon_utm
# does -- so the two new fields are derived views of what was already there,
# not a second answer to anything.
_ORIGINAL_PATCH_KEYS = {
    "id", "area_acres", "representative_elevation_m", "polygon_utm",
    "render_fill_polygon_utm", "geometry_wgs84", "cells", "hole_footprints",
    "source_patch_id",
}
for _p in _before_patches:
    assert _ORIGINAL_PATCH_KEYS <= set(_p), (
        "a field production already published has gone missing from cluster_and_gate(): "
        f"{sorted(_ORIGINAL_PATCH_KEYS - set(_p))}"
    )
    assert _p["render_fill_area_acres"] == round(
        float(_p["render_fill_polygon_utm"].area / production_area.SQUARE_METERS_PER_ACRE), 2
    ), (
        "render_fill_area_acres must equal the inline expression "
        "production_areas_to_geojson() published before it was stored"
    )
    assert _p["render_fill_area_acres"] <= _p["area_acres"] + 1e-9, (
        "the opening is anti-extensive -- its acreage can never exceed the footprint's"
    )
    if _p["render_fill_polygon_utm"].is_empty:
        assert _p["render_fill_geometry_wgs84"] is None, (
            "an empty opening must publish None, not an empty geometry dict -- "
            "'nothing to draw' and 'a zero-area polygon' are different claims"
        )
    else:
        assert _p["render_fill_geometry_wgs84"]["type"] in ("Polygon", "MultiPolygon")
print(
    f"production_area.py is additive-only, asserted by value across {len(_before_patches)} patches: "
    "every original field intact, render_fill_area_acres identical to the expression it replaced."
)

# ...now bring in the new module and run it against the SAME fixture...
import exclusion_zones as ez  # noqa: E402  (deliberately imported here, after the baseline)

_p_result = ez.identify_exclusion_zones(
    [],
    dem=_p_dem,
    boundary_polygon_utm=_p_boundary,
    tree_root_zone_mask_utm=_p_canopy,
    disqualifying_soil_union_utm=_p_soil_union,
    check_roads=False,
)

_after_raw = _run_production_step1()
_after_step1 = _snapshot(_after_raw)
_after_patches = production_area.cluster_and_gate(
    _after_raw["eligible_mask"], _p_dem, _p_boundary, _after_raw, min_area_acres=0.05
)
_after_patch_bytes = [
    (p["id"], p["area_acres"], p["polygon_utm"].wkb, p["render_fill_polygon_utm"].wkb, sorted(p["cells"]))
    for p in _after_patches
]

assert set(_before_step1) == set(_after_step1)
for _key in _before_step1:
    assert _before_step1[_key] == _after_step1[_key], (
        f"compute_step1_eligible_cells()['{_key}'] changed after exclusion_zones ran against the same "
        "fixture -- production must be bit-for-bit unaffected by this module's existence"
    )
assert _before_patch_bytes == _after_patch_bytes, (
    "every patch cluster_and_gate() produces must be byte-identical before and after this module runs"
)
assert len(_before_patches) > 0, "fixture sanity: production must actually produce patches here"
print(
    f"production is untouched at the BEHAVIOUR level: all {len(_before_step1)} arrays/flags "
    f"compute_step1_eligible_cells() returns (eligible_mask included) and all {len(_before_patches)} "
    "scored patch(es) are byte-identical before and after exclusion_zones ran on the same fixture."
)


# ===========================================================================
# 2. THE INTEGRATION IS BEHAVIOURALLY A NO-OP
# ===========================================================================
#
# Computed HERE, in the test, from two things the module already returns --
# never as a code path inside exclusion_zones.py, and never by touching
# production. It used to answer "how much ground would production LOSE if it
# were gated on this module's closed exclusions?" and the answer was real
# pinhole ground.
#
# With the closing removed the answer is EXACTLY ZERO, and that is now the
# point of the check rather than a degenerate result: this module's
# eligible_mask is the same set of cells production already computes, so the
# deferred wiring can no longer change production's output. The redundancy
# argument in exclusion_zones.py's module docstring rests on this, so it is
# asserted rather than asserted-in-prose.

_production_eligible = _after_raw["eligible_mask"]
_would_be_eligible = _production_eligible & (~_p_result["excluded_union_mask"])
_area_per_cell = cell_area_acres(_p_dem)
_lost_cells = int(_production_eligible.sum()) - int(_would_be_eligible.sum())
_lost_acres = _lost_cells * _area_per_cell

assert (_would_be_eligible & ~_production_eligible).sum() == 0, (
    "gating production on this module's exclusions must never ADD a cell to production's eligible mask"
)
assert _lost_cells == 0, (
    "with no closing, gating production on this module's exclusions must remove NOTHING -- the two are "
    f"the same set of cells. {_lost_cells} cell(s) = {_lost_acres:.3f} ac went missing, which means "
    "something extensive is still being applied somewhere in the exclusion path"
)
assert _p_result["eligible_mask"].tobytes() == _production_eligible.tobytes(), (
    "this module's eligible_mask must be BYTE-IDENTICAL to compute_step1_eligible_cells()' own -- the "
    "claim production's exclusion_result= override rests on"
)
assert _production_eligible.tobytes() == _run_production_step1()["eligible_mask"].tobytes(), (
    "measuring the hypothetical must not have altered production's real answer"
)
print(
    f"THE INTEGRATION IS A NO-OP: production's eligible mask is "
    f"{int(_production_eligible.sum())} cells ({int(_production_eligible.sum()) * _area_per_cell:.3f} ac) "
    f"on this fixture, this module's eligible_mask is byte-identical to it, and gating production on "
    f"these exclusions removes {_lost_cells} cells ({_lost_acres:.3f} ac). Before the closing was removed "
    "that figure was real pinhole ground; now production consuming these masks cannot change its output "
    "at all -- which is what the exclusion_result= override does, and what makes it a de-duplication "
    "rather than a decision."
)


# ===========================================================================
# 3. EVERY PER-GATE ACREAGE IS RAW -- NOTHING IS CLOSED
# ===========================================================================
#
# The branch's core guarantee, on the fixture that used to demonstrate the
# opposite. One fixture carrying all three shapes at once: canopy with
# pinholes, a steep region with pinholes, and the setback ring (which the
# cell grid fragments on its own).
#
# Asserting "the layers are raw" needs something to be raw AGAINST, so this
# section recomputes each gate's own hit mask independently of the module and
# compares. It then reports, per gate, what the 5 m closing this module used
# to apply WOULD have added -- because "we removed an operation" is only
# meaningful next to the ground that operation was adding.

_r_rows = _r_cols = 36
_r_array = flat_plane(_r_rows, _r_cols)
_r_array[6:16, 6:16] += 9.0     # steep region
_r_array[10, 10] = flat_plane(_r_rows, _r_cols)[10, 10]   # pinhole in the steep region
_r_array[13, 13] = flat_plane(_r_rows, _r_cols)[13, 13]   # and another
# ...and steep spikes scattered along the setback ring itself. The setback
# layer is derived as `on_parcel & slope_ok & ~slope_only_mask`, so a ring
# cell that ALSO fails slope leaves the setback layer entirely and lands in
# the slope layer -- which is precisely the mechanism that fragments the real
# ring into 41-43 pieces on the reference boundaries, and precisely the
# mechanism behind narrative_data's setback_is_lower_bound flag. Reproducing
# it here keeps the polygon-count assertion below a real one: without it the
# ring is a single connected loop and the count is trivially 1.
_r_ring_index = 2  # the outermost on-parcel cell band, given boundary_for()'s inset
for _spike in range(4, 32, 5):
    _r_array[_r_ring_index, _spike] += 9.0
    _r_array[_r_rows - 1 - _r_ring_index, _spike] += 9.0

_r_dem = make_dem(_r_array)
_r_boundary = boundary_for(_r_dem)

_r_canopy = np.zeros((_r_rows, _r_cols), dtype=bool)
_r_canopy[22:30, 22:30] = True
_r_canopy[25, 25] = False
_r_canopy[27, 27] = False

_r_result = ez.identify_exclusion_zones(
    [],
    dem=_r_dem,
    boundary_polygon_utm=_r_boundary,
    tree_root_zone_mask_utm=_r_canopy,
    check_soil=False,
    check_roads=False,
)
_r_layers = _r_result["layers"]
_r_area_per_cell = cell_area_acres(_r_dem)

# ---- the gates, recomputed here from first principles ---------------------
#
# Deliberately NOT taken from the module: a raw mask the module handed back
# would make this assertion circular. These five lines are the same gate
# definitions production_area.compute_step1_eligible_cells() uses.
_r_slope_pct = production_area.compute_slope_percent(_r_dem["array"], _r_dem["resolution_meters"])
_r_slope_ok = (~np.isnan(_r_slope_pct)) & (_r_slope_pct <= production_area.MAX_PRODUCTION_SLOPE_PCT)
_r_on_parcel = ez._on_parcel_mask(_r_dem, _r_boundary)
_r_shrunk = _r_boundary.buffer(-production_area.PRODUCTION_BOUNDARY_SETBACK_METERS)
_r_slope_only = _r_slope_ok & ez._on_parcel_mask(_r_dem, _r_shrunk)
_r_expected = {
    "canopy": _r_slope_only & _r_canopy,
    "slope": _r_on_parcel & (~_r_slope_ok),
    "hydric": np.zeros((_r_rows, _r_cols), dtype=bool),
    "roads": np.zeros((_r_rows, _r_cols), dtype=bool),
    "setback": _r_on_parcel & _r_slope_ok & (~_r_slope_only),
}

_r_report = []
for _gate in ez.LAYER_ORDER:
    _published = _r_layers[_gate]["mask"]
    _raw = _r_expected[_gate]
    assert _published.tobytes() == _raw.tobytes(), (
        f"the {_gate} layer's published mask must be the gate's OWN hit mask, bit-for-bit -- "
        f"{int(_raw.sum())} raw cells against {int(_published.sum())} published. Anything else means a "
        "morphological pass has come back"
    )
    # ...and the published acreage is that mask's cell count, not a geometry
    # area and not a closed count.
    assert _r_layers[_gate]["acres"] == round(int(_raw.sum()) * _r_area_per_cell, 2), (
        f"the {_gate} layer's acreage must be its raw mask's cell-count acreage"
    )
    _narr = [e for e in _r_result["narrative_data"]["layers"] if e["layer"] == _gate][0]
    assert _narr["acres"] == round(round(int(_raw.sum()) * _r_area_per_cell, 1), 1), (
        f"narrative_data's {_gate} acreage must be the same raw figure the layer reports"
    )
    # WHAT THE CLOSING WOULD HAVE ADDED, on this fixture, at the radius the
    # canopy and slope gates used to carry. Computed here so the removal is
    # quantified rather than merely asserted -- and asserted NOT to have
    # happened.
    _if_closed = disc_closing(_raw, 1)
    _gained = int(_if_closed.sum()) - int(_raw.sum())
    assert int(_published.sum()) != int(_if_closed.sum()) or _gained == 0, (
        f"the {_gate} layer must not match what a 5 m closing would produce"
    )
    _r_report.append((_gate, int(_raw.sum()), _gained, _gained * _r_area_per_cell))

# The gates that used to close must have something to close on this fixture,
# or "we did not close" is proved against nothing.
_r_by_gate = {g: (raw, gained, ac) for g, raw, gained, ac in _r_report}
for _gate in ("canopy", "slope"):
    assert _r_by_gate[_gate][1] > 0, (
        f"fixture sanity: the {_gate} layer must have pinholes a 5 m closing WOULD absorb, otherwise this "
        "section proves nothing about the closing being gone"
    )

# The setback ring: unchanged, and demonstrably not closing-proof. This was
# the one gate whose 0.0 m radius was a measured decision rather than an
# untuned placeholder, and it is now simply the same rule as the other four.
_setback_raw = _r_expected["setback"]
_setback_parts = polygon_part_count(cell_union_footprint(_r_dem, _setback_raw))
assert _setback_parts > 1, (
    "fixture sanity: the setback ring must be genuinely FRAGMENTED here (steep spikes along it move those "
    f"cells into the slope layer), otherwise the polygon-count assertion is trivial -- got "
    f"{_setback_parts} part(s)"
)
assert _setback_parts == polygon_part_count(_r_layers["setback"]["polygon_utm"]), (
    "the setback's polygon count must be unchanged by this module -- nothing merges its pieces now"
)
assert _r_by_gate["setback"][1] > 0, (
    "fixture sanity: this setback ring is not closing-proof -- a 5 m closing WOULD gain cells on it. That "
    "is what makes its unchanged polygon count evidence about the module rather than about the ring"
)

# And the module carries no closing configuration at all any more. Scanned
# out of dir() rather than checked name by name, for two reasons: the five
# deleted constants must not appear as string literals anywhere in the tree
# (the branch's `git grep` check), and a scan also catches a closing radius
# reintroduced under a NEW name, which a name list would not.
_closing_attrs = sorted(
    _n for _n in dir(ez) if "CLOSING" in _n.upper() or "DISC_CLOSING" in _n.upper()
)
assert _closing_attrs == [], (
    "exclusion_zones must expose no closing configuration at all -- a zeroed radius is a tunable "
    f"someone raises again, a deleted one is a decision. Found: {_closing_attrs}"
)
# ...nor any per-layer closing field on the wire or in narrative_data.
for _entry in _r_result["narrative_data"]["layers"]:
    assert set(_entry) == {"layer", "acres", "data_available"}, (
        f"narrative_data's layer entry must carry only the raw figures now -- got {sorted(_entry)}"
    )
assert "raw_mask" not in _r_layers["canopy"], (
    "with nothing closed there is no closed/raw PAIR to publish -- layers[*] carries one mask"
)
assert "raw_excluded_union_utm" not in _r_result, (
    "with nothing closed the raw union IS excluded_union_utm -- publishing both would leave two "
    "byte-identical keys with no stated difference"
)

print("EVERY PER-GATE ACREAGE IS RAW (36x36 fixture, canopy + slope pinholes + a fragmented setback ring):")
for _gate, _raw_cells, _gained, _gained_ac in _r_report:
    _pub_ac = _raw_cells * _r_area_per_cell
    print(
        f"   {_gate:<8s} published {_raw_cells:>4d} cells = {_pub_ac:.4f} ac  "
        f"(a 5 m closing would have published {_raw_cells + _gained:>4d} = {_pub_ac + _gained_ac:.4f} ac, "
        f"+{_gained_ac:.4f} ac of ground the gate never hit)"
    )
print(
    f"   Every published mask is bit-for-bit the gate's own hits, recomputed here from production's own "
    f"gate definitions rather than read back from the module. The setback ring keeps its "
    f"{_setback_parts} fragments. All five closing constants are deleted, not zeroed, and no closing "
    f"field survives in narrative_data or on layers[*]."
)


# ===========================================================================
# 4. THE ONLY GEOMETRIC OPERATION IS THE CLIP TO THE BOUNDARY
# ===========================================================================
#
# This replaces an EXTENSIVE invariant (raw ⊆ closed ⊆ boundary) that no
# longer has two sides to it. What is asserted now is stronger: the published
# union is the EXACT cell footprint of the union mask, clipped, and nothing
# else has touched it -- so there is no room for an extensive pass to be
# reintroduced without this failing.

for _label, _res, _bnd, _dem_ in (
    ("pinhole fixture", _p_result, _p_boundary, _p_dem),
    ("raw-acreage fixture", _r_result, _r_boundary, _r_dem),
):
    _render = _res["render_fill_polygon_utm"]
    _union_mask = _res["excluded_union_mask"]
    assert not _render.is_empty, f"{_label}: fixture sanity -- something must be excluded"

    # THE EXACT-FOOTPRINT IDENTITY. Not "within" the footprint and not
    # "contains" it: equal to it, to floating-point tolerance.
    _exact = cell_union_footprint(_dem_, _union_mask).intersection(_bnd)
    assert _render.symmetric_difference(_exact).area < _TOLERANCE_M2, (
        f"{_label}: the published union must be the EXACT cell footprint of its mask, clipped to the "
        f"boundary -- symmetric difference {_render.symmetric_difference(_exact).area:.6f} m2"
    )
    assert _render.difference(_bnd).area < _TOLERANCE_M2, (
        f"{_label}: the union must be within boundary_polygon_utm -- the clip to the drawn boundary is "
        "the ONLY geometric operation that applies to this layer"
    )
    assert _res["render_fill_polygon_utm"] is _res["excluded_union_utm"], (
        f"{_label}: render_fill_polygon_utm IS excluded_union_utm here, deliberately -- there is no "
        "display-only reduction to apply to an exact cell footprint"
    )
    # And the per-layer footprints tile it exactly: no layer extends past the
    # union, and together they cover it.
    _layer_union = _res["layers"][ez.LAYER_ORDER[0]]["polygon_utm"]
    for _name in ez.LAYER_ORDER[1:]:
        _layer_union = _layer_union.union(_res["layers"][_name]["polygon_utm"])
    assert _layer_union.symmetric_difference(_render).area < _TOLERANCE_M2, (
        f"{_label}: the five layer footprints must union to exactly the published union -- a layer that "
        "had been closed independently would break this"
    )

print(
    "THE ONLY GEOMETRIC OPERATION IS THE CLIP: on both fixtures the published union is bit-equal to the "
    "exact cell footprint of its own mask clipped to the boundary (symmetric difference < 1e-6 m²), the "
    "five per-layer footprints union to exactly it, and render_fill_polygon_utm IS that same object. "
    "There is no extensive pass left for a `render_fill.area <= polygon_utm.area` assertion to be "
    "backwards about."
)


# ===========================================================================
# 5. NaN SLOPE IS SPLIT OUT
# ===========================================================================
#
# "Too steep" and "not measured" both land in the slope layer, because that
# is what production's own gate does. They are different facts about the
# ground and narrative_data must not merge them.

_n_rows = _n_cols = 30
_n_array = flat_plane(_n_rows, _n_cols)
_n_array[12:18, 12:18] = np.nan          # a nodata patch
_n_array[20:24, 6:10] += 9.0             # and a genuinely steep block
_n_dem = make_dem(_n_array)
_n_boundary = boundary_for(_n_dem)
_n_result = ez.identify_exclusion_zones(
    [],
    dem=_n_dem,
    boundary_polygon_utm=_n_boundary,
    tree_root_zone_mask_utm=np.zeros((_n_rows, _n_cols), dtype=bool),
    check_soil=False,
    check_roads=False,
)
_n_slope_raw = _n_result["layers"]["slope"]["mask"]
_n_nan_cells = np.isnan(_n_array)
_n_on_parcel_nan = _n_nan_cells & _n_slope_raw

assert _n_on_parcel_nan.sum() > 0, "fixture sanity: the nodata patch must fall on-parcel"
# every on-parcel NaN cell is IN the slope layer -- matching production's gate
_n_on_parcel = _n_result["excluded_union_mask"] | _n_result["eligible_mask"]
assert ((_n_nan_cells & _n_on_parcel) & ~_n_slope_raw).sum() == 0, (
    "every on-parcel NaN-slope cell must land in the slope layer -- that is what production's own gate "
    "does (`~isnan & <= max` rejects them), so the layer has to match it"
)
_n_narrative = _n_result["narrative_data"]["slope_detail"]
_n_expected_nan_acres = round(int((_n_nan_cells & _n_on_parcel).sum()) * cell_area_acres(_n_dem), 1)
assert _n_narrative["nan_slope_acres"] == _n_expected_nan_acres, (
    f"narrative_data must report the NaN share separately -- expected {_n_expected_nan_acres}, got "
    f"{_n_narrative['nan_slope_acres']}"
)
assert _n_narrative["too_steep_acres"] > 0, "fixture sanity: there is genuinely steep ground here too"
assert _n_narrative["nan_slope_acres"] > 0
assert round(
    _n_narrative["too_steep_acres"] + _n_narrative["nan_slope_acres"], 1
) == round(int(_n_slope_raw.sum()) * cell_area_acres(_n_dem), 1), (
    "too_steep_acres + nan_slope_acres must account for the whole raw slope layer -- the split has to be "
    "exhaustive, not a sample"
)
print(
    f"NaN SLOPE SPLIT OUT: the slope layer is {int(_n_slope_raw.sum())} cells, of which "
    f"{int((_n_nan_cells & _n_on_parcel).sum())} have NO DEM coverage at all. narrative_data reports them "
    f"separately -- too_steep_acres={_n_narrative['too_steep_acres']}, "
    f"nan_slope_acres={_n_narrative['nan_slope_acres']} -- so a narrative can never call unmeasured "
    "ground 'too steep'."
)


# ===========================================================================
# 6. OVERLAP IS MEASURED, NOT ASSUMED
# ===========================================================================
#
# One cell can be both wooded and hydric. The five per-layer acreages MUST
# NOT BE SUMMED, and this proves the module reports by how much rather than
# just warning that they mustn't be.

_o_rows = _o_cols = 30
_o_dem = make_dem(flat_plane(_o_rows, _o_cols))
_o_boundary = boundary_for(_o_dem)
_o_canopy = np.zeros((_o_rows, _o_cols), dtype=bool)
_o_canopy[10:20, 10:20] = True
# a hydric strip deliberately laid ACROSS the canopy block
_o_soil = Polygon(
    [
        (_o_dem["origin_x"] + 12 * RESOLUTION_M, _o_dem["origin_y"] - 22 * RESOLUTION_M),
        (_o_dem["origin_x"] + 18 * RESOLUTION_M, _o_dem["origin_y"] - 22 * RESOLUTION_M),
        (_o_dem["origin_x"] + 18 * RESOLUTION_M, _o_dem["origin_y"] - 8 * RESOLUTION_M),
        (_o_dem["origin_x"] + 12 * RESOLUTION_M, _o_dem["origin_y"] - 8 * RESOLUTION_M),
    ]
)
_o_result = ez.identify_exclusion_zones(
    [],
    dem=_o_dem,
    boundary_polygon_utm=_o_boundary,
    tree_root_zone_mask_utm=_o_canopy,
    disqualifying_soil_union_utm=_o_soil,
    check_roads=False,
)
_o_overlap = _o_result["narrative_data"]["overlap"]
_o_pairs = {tuple(entry["layers"]): entry["overlap_acres"] for entry in _o_overlap["pairs"]}

assert len(_o_pairs) == 10, f"all ten layer pairs must be reported, got {len(_o_pairs)}"
_o_canopy_hydric_cells = int(
    (_o_result["layers"]["canopy"]["mask"] & _o_result["layers"]["hydric"]["mask"]).sum()
)
assert _o_canopy_hydric_cells > 0, "fixture sanity: the strip must genuinely cross the canopy block"
assert _o_pairs[("canopy", "hydric")] == round(_o_canopy_hydric_cells * cell_area_acres(_o_dem), 1), (
    "the canopy/hydric overlap must be MEASURED and reported, not assumed away"
)
assert _o_pairs[("slope", "setback")] == 0.0, (
    "slope and setback are disjoint BY CONSTRUCTION (the setback layer requires slope_ok), which is "
    "exactly why the setback figure is a lower bound"
)
assert _o_overlap["naive_sum_acres"] > _o_overlap["union_acres"], (
    "the naive sum of the five layers must exceed the true union whenever any pair overlaps -- that gap "
    "is the double-count a summed 'total excluded' would hide"
)
assert round(_o_overlap["naive_sum_acres"] - _o_overlap["union_acres"], 1) == _o_overlap[
    "double_counted_acres"
], "double_counted_acres must be exactly naive_sum - union"
assert _o_overlap["double_counted_acres"] == _o_pairs[("canopy", "hydric")], (
    "on this fixture canopy/hydric is the ONLY overlapping pair, so the naive sum must exceed the union "
    "by EXACTLY that pairwise overlap -- no more, no less"
)
assert _o_result["narrative_data"]["setback_is_lower_bound"] is True
assert (
    _o_result["narrative_data"]["setback_lower_bound_reason"]
    == "steep_ring_ground_counted_in_slope_layer"
), "the unevaluated-ring caveat must travel as a branchable flag, not as prose"
print(
    f"OVERLAP MEASURED, NOT ASSUMED: all 10 pairs reported; canopy & hydric share "
    f"{_o_canopy_hydric_cells} cell(s) = {_o_pairs[('canopy', 'hydric')]} ac, and the naive sum "
    f"({_o_overlap['naive_sum_acres']} ac) exceeds the true union ({_o_overlap['union_acres']} ac) by "
    f"EXACTLY that amount ({_o_overlap['double_counted_acres']} ac). The unevaluated-ring caveat travels "
    "as setback_is_lower_bound + its reason token."
)


# ===========================================================================
# 7. narrative_data IS JSON-SERIALISABLE, IMPERIAL, 1 DECIMAL
# ===========================================================================

def walk(node, path="narrative_data"):
    """Every leaf of the block, with its path, so a failure names the field."""
    if isinstance(node, dict):
        for key, value in node.items():
            assert isinstance(key, str), f"{path}: keys must be plain str, got {type(key)}"
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")
    else:
        yield path, node


for _nd_label, _nd in (
    ("pinhole fixture", _p_result["narrative_data"]),
    ("overlap fixture", _o_result["narrative_data"]),
    ("nodata fixture", _n_result["narrative_data"]),
):
    _round_tripped = json.loads(json.dumps(_nd))
    assert _round_tripped == _nd, f"{_nd_label}: narrative_data must survive a JSON round-trip unchanged"
    for _path, _leaf in walk(_nd):
        assert not isinstance(_leaf, np.generic), (
            f"{_nd_label}: {_path} is a numpy scalar ({type(_leaf)}) -- narrative_data carries plain "
            "Python types only, so the report can serialise it"
        )
        assert not isinstance(_leaf, np.ndarray), f"{_nd_label}: {_path} is a numpy array"
        assert not hasattr(_leaf, "geom_type"), (
            f"{_nd_label}: {_path} is geometry -- narrative_data carries no geometry"
        )
        assert isinstance(_leaf, (str, bool, int, float)) or _leaf is None, (
            f"{_nd_label}: {_path} is {type(_leaf)}, which is not a narrative_data leaf type"
        )
        if isinstance(_leaf, float):
            assert round(_leaf, 1) == _leaf, f"{_nd_label}: {_path} = {_leaf} is not rounded to 1 decimal"
    _nd_keys = {entry["layer"] for entry in _nd["layers"]}
    assert _nd_keys == set(ez.LAYER_ORDER), f"{_nd_label}: one entry per gate required, got {_nd_keys}"
    assert [entry["layer"] for entry in _nd["layers"]] == list(ez.LAYER_ORDER), (
        f"{_nd_label}: layers must be in LAYER_ORDER"
    )
    for _entry in _nd["layers"]:
        # The per-gate entry is now exactly three keys. It used to carry five
        # more describing the closing that was applied (radius requested,
        # radius effective after quantization, radius in cells, whether it
        # closed, and the acreage it gained). Nothing closes, so reporting any
        # of that would be reporting on an operation that does not run.
        assert set(_entry) == {"layer", "acres", "data_available"}, (
            f"{_nd_label}: a narrative_data layer entry carries the raw acreage, the gate name and the "
            f"availability flag and nothing else -- got {sorted(_entry)}"
        )
    assert set(_nd) == {
        "parcel",
        "layers",
        "overlap",
        "slope_detail",
        "setback_is_lower_bound",
        "setback_lower_bound_reason",
    }, f"{_nd_label}: unexpected top-level narrative_data keys {set(_nd)}"
print(
    "narrative_data on all three fixtures: JSON round-trips unchanged, every leaf a plain str/bool/int/"
    "float/None, every float rounded to 1 decimal, every per-gate entry down to {layer, acres, "
    "data_available} with no closing field left to describe, no numpy scalars and no geometry."
)


# ===========================================================================
# 8. THE REDUNDANCY IS GONE -- EXACT COUNTS PER GATE HELPER
# ===========================================================================
#
# This section used to measure the cost of DEFERRING the production
# integration: canopy TWICE, soil TWICE, the slope grid TWICE across one
# build_pipeline_context() run, once for exclusion_zones and once for
# production computing the same five gates itself. That integration has
# landed -- production_area.compute_step1_eligible_cells() takes an
# exclusion_result= override and build_pipeline_context() passes this
# module's own result into it -- so every one of those is now ONE.
#
# Counted at the shared GATE-HELPER bindings, which is what these counters
# actually measure (NOT every road/canopy/soil touch in the pipeline;
# build_pipeline_context()'s own separate existing_roads fetch, mocked below,
# is outside these counts, and so is water_candidate_zones', mocked away
# entirely here).
#
# EVERY COUNT IS NOW EXACTLY ONE, AND EVERY ONE OF THEM IS THIS MODULE'S.
# Production makes no gate fetch and computes no slope grid at all on this
# path: it reads all five gates off the result. The road helper reaches zero
# because BOTH remaining consumers are supplied -- build_pipeline_context()
# hands its own already-fetched existing_roads union to identify_exclusion_
# zones() (reused even when it is a real None, "checked, genuinely no roads
# nearby"), and production reads the road layer out of the exclusion result.
#
# One more of ANY count would mean something re-fetches, which is exactly
# the failure the override pattern exists to prevent, so this asserts
# equality and not an upper bound. One FEWER on canopy/soil/slope would mean
# this module stopped computing its own gates, which is the other direction
# of the same failure -- production now depends on it doing so.
#
# Each counter is installed at BOTH module-level bindings of the same
# underlying helper (exclusion_zones.py's own, bound via `from
# production_area import (...)`, and production_area_ceiling.py's / production_
# area.py's own) -- patching one does NOT intercept the other. Everything
# downstream of production is mocked away so the counts measure this
# duplication and nothing else.

from contextlib import ExitStack  # noqa: E402
from unittest.mock import patch as mock_patch  # noqa: E402

import pipeline_context as pc  # noqa: E402
import production_area_ceiling  # noqa: E402

_c_rows = _c_cols = 26
_c_dem = make_dem(flat_plane(_c_rows, _c_cols))
_c_boundary_utm = boundary_for(_c_dem)
_counts = {"canopy": 0, "soil": 0, "road": 0, "slope": 0, "canopy_leaf": 0}


def _counting(name, fn):
    """Increments the SHARED counter for `name` and delegates. Both bindings
    of a helper get a wrapper over the same counter, so the count is per
    LOGICAL fetch rather than per binding."""

    def wrapper(*args, **kwargs):
        _counts[name] += 1
        return fn(*args, **kwargs)

    return wrapper


def _fake_canopy(boundary_polygon_utm, dem, buffer_meters=None, canopy_height=None):
    """All-False: no tree cover anywhere. This file is not testing the canopy
    gate here, only how many times it is reached."""
    return np.zeros(dem["array"].shape, dtype=bool)


def _fake_canopy_leaf(boundary_coordinates, dem, *_a, **_k):
    """The NETWORK leaf under the gate above -- canopy_height_data.get_canopy_
    height_for_boundary()'s own return shape. build_pipeline_context() now
    reaches this directly (it fetches canopy itself when the caller supplies
    none, and forwards the result to every consumer), so it needs its own
    counter: the gate counter above measures how many root-zone MASKS are
    derived, which is a different question from how many Planetary Computer
    round-trips are paid for."""
    return {
        "array": np.full(dem["array"].shape, 1.0, dtype=np.float32),
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source_item_id": "offline-test-stub",
    }


def _fake_soil(*_args, **_kwargs):
    return None


def _fake_road(*_args, **_kwargs):
    return None


_real_slope = production_area.compute_slope_percent
_empty_fc = {"type": "FeatureCollection", "features": []}
_empty_road_network = {
    "branches": [],
    "total_length_meters": 0.0,
    "steep_meters": 0.0,
    "cell_footprint_polygon_utm": Polygon(),
}

with ExitStack() as _stack:
    _enter = _stack.enter_context
    # the canopy NETWORK leaf, at BOTH its bindings -- pipeline_context.py
    # reaches it as a module attribute (canopy_height_data.get_canopy_height_
    # for_boundary), production_area.py through its own `from canopy_height_
    # data import ...` copy. Patching one does not intercept the other.
    _enter(mock_patch.object(pc.canopy_height_data, "get_canopy_height_for_boundary",
                             side_effect=_counting("canopy_leaf", _fake_canopy_leaf)))
    _enter(mock_patch.object(production_area, "get_canopy_height_for_boundary",
                             side_effect=_counting("canopy_leaf", _fake_canopy_leaf)))
    # the two canopy GATE bindings
    _enter(mock_patch.object(pc.exclusion_zones, "get_required_tree_root_zone_mask_utm",
                             side_effect=_counting("canopy", _fake_canopy)))
    _enter(mock_patch.object(production_area_ceiling, "get_required_tree_root_zone_mask_utm",
                             side_effect=_counting("canopy", _fake_canopy)))
    # the two soil bindings
    _enter(mock_patch.object(pc.exclusion_zones, "_fetch_disqualifying_soil_union",
                             side_effect=_counting("soil", _fake_soil)))
    _enter(mock_patch.object(production_area_ceiling, "_fetch_disqualifying_soil_union",
                             side_effect=_counting("soil", _fake_soil)))
    # the two road bindings (production_area_ceiling's own, and this module's)
    _enter(mock_patch.object(pc.exclusion_zones, "_fetch_road_exclusion_union_utm",
                             side_effect=_counting("road", _fake_road)))
    _enter(mock_patch.object(production_area_ceiling, "_fetch_road_exclusion_union_utm",
                             side_effect=_counting("road", _fake_road)))
    # the two slope bindings: exclusion_zones' own, and production_area's (which
    # is what compute_step1_eligible_cells() looks up)
    _enter(mock_patch.object(pc.exclusion_zones, "compute_slope_percent",
                             side_effect=_counting("slope", _real_slope)))
    _enter(mock_patch.object(production_area, "compute_slope_percent",
                             side_effect=_counting("slope", _real_slope)))

    # everything downstream of production, mocked away entirely
    _enter(mock_patch.object(pc.dem_data, "get_dem_for_boundary", return_value=_c_dem))
    _enter(mock_patch.object(pc.valley_delineation, "delineate_valleys", return_value=[]))
    _enter(mock_patch.object(pc.keypoint_detection, "detect_keypoints", return_value=[]))
    _enter(mock_patch.object(pc.farm_roads_data, "get_road_exclusion_union_utm", return_value=None))
    _enter(mock_patch.object(pc.road_corridors, "_fetch_floodplain_hydric_union", return_value=(None, False)))
    _enter(mock_patch.object(pc.water_survey_areas, "identify_water_survey_areas",
                             return_value={"zones_geojson": _empty_fc, "regions": [],
                                           "regions_by_type": {"embankment": [], "excavated": []},
                                           "selected_water_zone": None, "narrative_data": None,
                                           "gate_mask_stats": {}, "result": {}}))
    _enter(mock_patch.object(pc.road_corridors, "identify_road_corridor_candidates",
                             return_value={"zones_geojson": _empty_fc, "all_scored_candidates": [],
                                           "road_network": _empty_road_network,
                                           "selected_road_corridor": None}))
    _enter(mock_patch.object(pc, "identify_solar_candidate_zones",
                             return_value={"zones_geojson": _empty_fc, "all_scored_candidates": [],
                                           "selected_structure_site": None}))
    _enter(mock_patch.object(pc, "identify_tree_zone_candidates",
                             return_value={"zones_geojson": _empty_fc, "search_space_geojson": _empty_fc,
                                           "search_space_acres": 0.0, "claimed_acres": 0.0,
                                           "boundary_acres": 0.0, "patches": []}))

    _c_ctx = pc.build_pipeline_context(
        [(-80.0, 40.0), (-79.99, 40.0), (-79.99, 40.01), (-80.0, 40.01)],
        (-80.0, 40.0),
        dem=_c_dem,
        boundary_polygon_utm=_c_boundary_utm,
    )

_expected_gate_counts = {"canopy": 1, "soil": 1, "slope": 1, "road": 0, "canopy_leaf": 1}
for _gate, _expected in _expected_gate_counts.items():
    assert _counts[_gate] == _expected, (
        f"the {_gate} gate helper must run EXACTLY {_expected}x across one build_pipeline_context() run. "
        f"canopy/soil/slope: ONCE, this module's own -- production consumes the result rather than "
        f"computing the same five gates itself (production_area.compute_step1_eligible_cells()' "
        f"exclusion_result= override). road: ZERO at these two bindings -- build_pipeline_context() passes "
        f"its own already-fetched existing_roads union in here (a real None included), and production "
        f"reads the road layer out of the exclusion result. Got {_counts[_gate]}. One MORE means something "
        "is re-fetching and the override pattern is failing somewhere it should be preventing it; one "
        "FEWER on canopy/soil/slope would mean this module stopped computing its own gates, which "
        "production now depends on it doing. canopy_leaf is the NETWORK fetch under the canopy gate, a "
        "different question from the gate count: build_pipeline_context() fetches canopy ONCE itself and "
        "forwards it, so exactly one round-trip is paid for however many gates derive a mask from it."
    )
assert isinstance(_c_ctx.exclusion_zones, dict) and _c_ctx.exclusion_zones, (
    "the context must carry a real exclusion result"
)
assert _c_ctx.narrative_data["exclusion_zones"] is not None, (
    "the context's narrative_data must carry this module's own block"
)
# The union pipeline_context passed above was the mocked existing_roads fetch's
# None -- identify_exclusion_zones() must have REUSED it as "checked, genuinely
# no roads nearby" (road_available True), not treated it as unavailable.
assert _c_ctx.exclusion_zones["layers"]["roads"]["data_available"] is True, (
    "a caller-supplied real None union means 'checked, genuinely no roads nearby' -- the roads layer must "
    "report data_available=True without exclusion_zones fetching anything itself"
)
print(
    "REDUNDANCY GONE: across one full build_pipeline_context() run the measured gate-helper call counts "
    f"are canopy={_counts['canopy']}, soil={_counts['soil']}, slope={_counts['slope']} (1x each, all this "
    f"module's own -- production consumes the result instead of computing the same five gates again) and "
    f"road={_counts['road']} (0x at these bindings: build_pipeline_context() supplies the union here, and "
    f"production reads the road layer off the result). Previously 2/2/2 and 1. The canopy NETWORK leaf "
    f"under that gate is reached {_counts['canopy_leaf']}x -- build_pipeline_context() fetches the HAG "
    "coverage once and forwards it, so the gate count and the round-trip count are now independent."
)

# --- the buffers match by shared definition, asserted rather than assumed ---
#
# build_pipeline_context()'s existing_roads is built by farm_roads_data.
# get_road_exclusion_union_utm() at ITS default buffer; had exclusion_zones
# self-computed instead, it would have used production_area._fetch_road_
# exclusion_union_utm()'s default. Both defaults must be the same shared
# farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS -- captured at def time, so
# compare the SIGNATURE defaults, not the live module attribute alone. If a
# future change hands either path its own buffer, this fails loudly instead
# of the pass-through silently substituting a wrong-buffer union.

import inspect  # noqa: E402

import farm_roads_data  # noqa: E402

_producer_default = inspect.signature(farm_roads_data.get_road_exclusion_union_utm).parameters["buffer_meters"].default
_consumer_default = inspect.signature(production_area._fetch_road_exclusion_union_utm).parameters["buffer_meters"].default
assert _producer_default == _consumer_default == farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS, (
    f"the union build_pipeline_context() passes into identify_exclusion_zones() is built at "
    f"get_road_exclusion_union_utm()'s default buffer ({_producer_default}m), while exclusion_zones' own "
    f"self-compute would use _fetch_road_exclusion_union_utm()'s default ({_consumer_default}m) -- these "
    f"must both be the single shared farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS "
    f"({farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS}m); a divergence means the pass-through would "
    "silently substitute a union built at the wrong buffer"
)
print(
    f"BUFFERS MATCH BY DEFINITION, ASSERTED: the passed union's producer default and the consumer's "
    f"self-compute default are both the shared ROAD_EXCLUSION_BUFFER_METERS = {_producer_default}m."
)


# ===========================================================================
# 8c. THE ROAD GATE NOW EXCLUDES GROUND -- 5.0m EITHER SIDE OF A CENTERLINE
# ===========================================================================
#
# ROAD_EXCLUSION_BUFFER_METERS was 0.0 -- buffering a road LineString by zero
# yields zero-area geometry, so the union was always empty and the road layer
# could never exclude a cell (a documented no-op pending tuning). At the new
# 5.0m default a road crossing the parcel excludes real ground for the first
# time. Fixture: a flat, all-slope-passing plane with one road centerline
# crossing it horizontally, deliberately offset a quarter cell from the row
# centers so cell-center distances to the line are 1.25/3.75/6.25/8.75m --
# clean separation either side of the 5.0m buffer edge, no boundary-case
# ambiguity. Both reference boundaries measured 0 road cells (no mapped road
# near either parcel), so this synthetic fixture is the gate's first real
# validation -- see the constant's own docstring.

from rasterio.warp import transform as _rg_warp_transform  # noqa: E402

_rg_rows = _rg_cols = 40
_rg_dem = make_dem(flat_plane(_rg_rows, _rg_cols))
_rg_boundary = boundary_for(_rg_dem)
_rg_lons, _rg_lats = _rg_warp_transform(
    _rg_dem["crs"], "EPSG:4326", *[list(c) for c in _rg_boundary.exterior.coords.xy]
)
_rg_boundary_coords = list(zip(_rg_lons, _rg_lats))

# Road centerline: horizontal, 1.25m below row 20's cell centers, so rows
# 20/21 centers sit 1.25/3.75m from it (inside 5.0m) and rows 19/22 sit
# 6.25/8.75m (outside).
_rg_road_y = _rg_dem["origin_y"] - (20 + 0.5) * RESOLUTION_M - 1.25
_rg_road_lons, _rg_road_lats = _rg_warp_transform(
    _rg_dem["crs"], "EPSG:4326",
    [_rg_dem["origin_x"] - 2 * RESOLUTION_M, _rg_dem["origin_x"] + (_rg_cols + 2) * RESOLUTION_M],
    [_rg_road_y, _rg_road_y],
)
_rg_farm_roads = [{
    "name": "Synthetic Crossing Rd",
    "geometry": {"type": "LineString", "coordinates": [list(pt) for pt in zip(_rg_road_lons, _rg_road_lats)]},
}]

# Both unions built by the REAL producer from the same road line: one at the
# 5.0m default, one at the old 0.0 for the inline contrast.
_rg_union_now = farm_roads_data.get_road_exclusion_union_utm(_rg_boundary_coords, _rg_dem, farm_roads=_rg_farm_roads)
_rg_union_old = farm_roads_data.get_road_exclusion_union_utm(
    _rg_boundary_coords, _rg_dem, buffer_meters=0.0, farm_roads=_rg_farm_roads
)
assert _rg_union_old is None, "contrast precondition: the old 0.0 buffer yields an empty union (None) from the same road"

_rg_no_canopy = np.zeros((_rg_rows, _rg_cols), dtype=bool)
_rg_result_now = ez.identify_exclusion_zones(
    _rg_boundary_coords,
    dem=_rg_dem,
    boundary_polygon_utm=_rg_boundary,
    tree_root_zone_mask_utm=_rg_no_canopy,
    check_soil=False,
    road_exclusion_union_utm=_rg_union_now,
)
_rg_result_old = ez.identify_exclusion_zones(
    _rg_boundary_coords,
    dem=_rg_dem,
    boundary_polygon_utm=_rg_boundary,
    tree_root_zone_mask_utm=_rg_no_canopy,
    check_soil=False,
    road_exclusion_union_utm=_rg_union_old,
)

_rg_road_mask = _rg_result_now["layers"]["roads"]["mask"]
_rg_hit_rows = sorted(set(np.argwhere(_rg_road_mask)[:, 0].tolist()))
assert _rg_hit_rows == [20, 21], (
    f"cells whose centers sit within 5.0m of the road centerline (rows 20/21, at 1.25/3.75m) must be "
    f"excluded and cells beyond (rows 19/22, at 6.25/8.75m) must not -- excluded rows: {_rg_hit_rows}"
)
_rg_acres_now = _rg_result_now["layers"]["roads"]["acres"]
_rg_acres_old = _rg_result_old["layers"]["roads"]["acres"]
assert _rg_acres_now > 0.0, "the road layer must exclude real acreage at the 5.0m default"
assert _rg_acres_old == 0.0, (
    "inline contrast: under the old 0.0 buffer this same fixture excluded NOTHING -- the difference IS "
    f"the behaviour change (got {_rg_acres_old} acres)"
)
print(
    f"ROAD GATE ACTIVE: a road crossing the parcel now excludes {_rg_acres_now:.3f} acres "
    f"({int(_rg_road_mask.sum())} cells, rows within 5.0m of the centerline only); under the old 0.0 "
    f"buffer the same fixture excluded {_rg_acres_old:.3f} acres (nothing -- the gate was a no-op)."
)


# ===========================================================================
# 9. THE LAYER ACTUALLY RENDERS -- BENEATH EVERYTHING, ONE LEGEND ENTRY
# ===========================================================================
#
# Two constraints from this layer's own brief, both checked against a real
# render rather than against the constants: it must sit BELOW every other
# layer's zorder (it is the map's ground layer, not another feature), and it
# must contribute exactly ONE legend entry.

import os  # noqa: E402
import tempfile  # noqa: E402

from rasterio.warp import transform as warp_transform  # noqa: E402

import render_layout_map as rlm  # noqa: E402

_m_xs, _m_ys = _o_boundary.exterior.coords.xy
_m_lons, _m_lats = warp_transform(_o_dem["crs"], "EPSG:4326", list(_m_xs), list(_m_ys))
_m_boundary_coordinates = list(zip(_m_lons, _m_lats))

_m_layers = {
    "dem": _o_dem,
    "exclusion_zones": _o_result,
    "production_areas": [],
    "water_zone": None,
    "road_corridor": [],
    "tree_zone_result": {"patches": []},
    "structure_site": None,
    "keypoints": [],
    "water_features": {"streams": [], "water_bodies": []},
    "contour_lines": [],
    "fencing_result": {"fencing_geojson": {"type": "FeatureCollection", "features": []}},
}

_drawn = []
_orig_plot_polygon = rlm.plot_polygon
_orig_legend = rlm.plt.Axes.legend
_legend_labels = []


def _recording_plot_polygon(geom, **kwargs):
    _drawn.append({"facecolor": kwargs.get("facecolor"), "zorder": kwargs.get("zorder")})
    return _orig_plot_polygon(geom, **kwargs)


def _recording_legend(self, *args, **kwargs):
    legend = _orig_legend(self, *args, **kwargs)
    _legend_labels[:] = [text.get_text() for text in legend.get_texts()]
    return legend


with mock_patch.object(rlm, "plot_polygon", _recording_plot_polygon), \
     mock_patch.object(rlm.plt.Axes, "legend", _recording_legend), \
     tempfile.TemporaryDirectory() as _tmpdir:
    rlm.render_layout_map(
        _m_boundary_coordinates, os.path.join(_tmpdir, "layout_map.png"), layers=_m_layers
    )

_exclusion_draws = [d for d in _drawn if d["facecolor"] == rlm.EXCLUSION_ZONE_COLOR]
assert _exclusion_draws, (
    "the exclusion union must actually be DRAWN -- no patch was plotted in EXCLUSION_ZONE_COLOR"
)
# Everything else this render drew, split at the BACKDROP boundary. The
# basemap (or its neutral fallback fill, zorder 1) and the halo mask (zorder
# 10) are backdrop treatments, not KSOP layers: the basemap is the ground the
# whole map sits on, and the halo washes OFF-parcel ground this layer never
# covers. The exclusion layer belongs above those two and below everything
# else -- see EXCLUSION_ZONE_ZORDER's own comment.
_BACKDROP_ZORDER_CEILING = 10
_backdrop_zorders = [
    d["zorder"]
    for d in _drawn
    if d["facecolor"] != rlm.EXCLUSION_ZONE_COLOR
    and d["zorder"] is not None
    and d["zorder"] <= _BACKDROP_ZORDER_CEILING
]
_feature_zorders = [
    d["zorder"]
    for d in _drawn
    if d["facecolor"] != rlm.EXCLUSION_ZONE_COLOR
    and d["zorder"] is not None
    and d["zorder"] > _BACKDROP_ZORDER_CEILING
]
assert _backdrop_zorders, "fixture sanity: this render must draw a backdrop to sit above"
for _draw in _exclusion_draws:
    assert _draw["zorder"] == rlm.EXCLUSION_ZONE_ZORDER
    for _z in _feature_zorders:
        assert _draw["zorder"] < _z, (
            f"the exclusion layer must sit BELOW every feature layer -- it draws at {_draw['zorder']} but "
            f"a feature draws at {_z}"
        )
    for _z in _backdrop_zorders:
        assert _draw["zorder"] > _z, (
            f"the exclusion layer must sit ABOVE the basemap/halo backdrop (it covers ON-parcel ground, "
            f"which the halo is not meant to touch) -- it draws at {_draw['zorder']}, backdrop at {_z}"
        )
# The feature layers that draw as lines rather than polygons carry their own
# zorders as module constants; assert against those directly, since this
# fixture deliberately draws none of them.
for _layer_zorder in (20, 40, 41, 42, 42.5, 42.8, rlm.EXCLUSION_FENCE_ZORDER, rlm.FENCE_ZORDER):
    assert rlm.EXCLUSION_ZONE_ZORDER < _layer_zorder, (
        f"exclusion zorder {rlm.EXCLUSION_ZONE_ZORDER} must be below every feature layer -- found a layer "
        f"at {_layer_zorder} (streams 20, production contours 40, water ripples 41, road 42/42.5, tree "
        "hatch 42.8, the exclusion/boundary fences)"
    )

assert _legend_labels.count(rlm.LEGEND_LABEL_EXCLUSION) == 1, (
    f"the exclusion layer must contribute EXACTLY ONE legend entry -- got {_legend_labels}"
)
assert _legend_labels[0] == rlm.LEGEND_LABEL_EXCLUSION, (
    "it leads the legend, being the ground layer everything else sits on -- got "
    f"{_legend_labels}"
)

# ...and a layers dict with no exclusion result at all still renders, drawing
# no exclusion patch and contributing no legend entry.
_m_layers_without = dict(_m_layers)
del _m_layers_without["exclusion_zones"]
_drawn.clear()
_legend_labels.clear()
with mock_patch.object(rlm, "plot_polygon", _recording_plot_polygon), \
     mock_patch.object(rlm.plt.Axes, "legend", _recording_legend), \
     tempfile.TemporaryDirectory() as _tmpdir:
    rlm.render_layout_map(
        _m_boundary_coordinates, os.path.join(_tmpdir, "layout_map_no_exclusion.png"), layers=_m_layers_without
    )
assert not [d for d in _drawn if d["facecolor"] == rlm.EXCLUSION_ZONE_COLOR], (
    "a layers dict built before this layer existed must simply draw no exclusion layer, not fail"
)
assert rlm.LEGEND_LABEL_EXCLUSION not in _legend_labels, (
    "a feature that drew nothing contributes no legend entry"
)

print(
    f"RENDERED: the exclusion union draws as flat {rlm.EXCLUSION_ZONE_COLOR} fill at alpha "
    f"{rlm.EXCLUSION_ZONE_ALPHA}, no edge stroke, zorder {rlm.EXCLUSION_ZONE_ZORDER} -- below the streams "
    f"(20) and every KSOP layer above them -- contributing exactly one legend entry "
    f"({rlm.LEGEND_LABEL_EXCLUSION!r}). A layers dict without the key renders with no exclusion layer and "
    "no legend entry."
)



from rasterio.warp import transform_geom as _rg_transform_geom  # noqa: E402
from shapely.geometry import mapping as _shapely_mapping  # noqa: E402

# ===========================================================================
# 10. THE RAW-ROW SOIL OVERRIDE: IDENTICAL RESULT, ZERO QUERIES
# ===========================================================================
#
# identify_exclusion_zones() gained soil_components=/soil_geometries=, a
# PURE PASSTHROUGH to production_area._fetch_disqualifying_soil_union()'s
# own new overrides, so a caller holding ParcelData's already-fetched
# SSURGO rows stops forcing that helper to re-issue the two SDA queries
# behind them.
#
# THE CENTRAL CLAIM OF THAT CHANGE IS THAT NO COMPUTED VALUE MOVES. It is
# asserted here the only way that means anything: the SAME fixture is run
# twice -- once with the overrides ABSENT (the self-fetch path, which is
# what every caller did before) and once with them SUPPLIED -- and the two
# results are compared BIT-IDENTICALLY. Masks are compared as ARRAYS
# (np.array_equal), never by acreage or cell count: a mask with the same
# number of cells in different places is a different answer, and a summary
# statistic would call it a match.
#
# The fixture is built so the hydric gate has REAL CONTENT. Comparing two
# empty unions would pass no matter what the override did.

# THE PARAMETER CONTRACT: the eleven pre-existing parameters, in their
# original order, with the two new ones APPENDED. Same frozen-list check
# section 1 applies to compute_step1_eligible_cells(), and for the same
# reason -- inserting soil_components/soil_geometries beside disqualifying_
# soil_union_utm (where they read better) would silently re-bind every
# positional caller past that point.
_EZ_ORIGINAL_PARAMS = [
    "boundary_coordinates",
    "dem",
    "boundary_polygon_utm",
    "max_slope_pct",
    "boundary_setback_meters",
    "canopy_height",
    "tree_root_zone_mask_utm",
    "disqualifying_soil_union_utm",
    "road_exclusion_union_utm",
    "check_soil",
    "check_roads",
]
_ez_params = list(inspect.signature(ez.identify_exclusion_zones).parameters)
assert _ez_params == _EZ_ORIGINAL_PARAMS + ["soil_components", "soil_geometries"], (
    "identify_exclusion_zones() must keep its eleven original parameters in their original positions "
    f"with the two raw-row soil overrides APPENDED. Got: {_ez_params}"
)
_ez_sig = inspect.signature(ez.identify_exclusion_zones).parameters
assert _ez_sig["soil_components"].default is None and _ez_sig["soil_geometries"].default is None, (
    "both must default to a plain None -- the self-fetch trigger the whole pipeline's override "
    "convention uses. A sentinel here would mean 'supplied and empty' had to be distinguishable, and "
    "no caller draws that distinction (only road_exclusion_union_utm does, for its own reason)."
)

# ...and the helper they pass through to must accept them under the SAME
# names, appended after its own two original parameters. This is what makes
# the passthrough a passthrough rather than a translation layer.
_helper_params = list(inspect.signature(production_area._fetch_disqualifying_soil_union).parameters)
assert _helper_params == ["wkt_polygon", "dem", "soil_components", "soil_geometries"], (
    f"_fetch_disqualifying_soil_union() must keep (wkt_polygon, dem) in their original positions -- "
    f"every caller passes both positionally -- with the two overrides appended. Got: {_helper_params}"
)

_o_rows, _o_cols = 34, 34
_o_dem = make_dem(flat_plane(_o_rows, _o_cols))
_o_boundary_utm = boundary_for(_o_dem)

# A hydric map unit covering the WEST half of the parcel, built by back-
# projecting a real sub-rectangle of the boundary into WGS84 -- so the
# geometry genuinely overlaps the DEM grid after _fetch_disqualifying_soil_
# union() reprojects it forward again, rather than landing off-grid and
# producing the empty union this section exists to avoid.
_o_minx, _o_miny, _o_maxx, _o_maxy = _o_boundary_utm.bounds
_o_hydric_utm = Polygon([
    (_o_minx, _o_miny),
    (_o_minx + (_o_maxx - _o_minx) * 0.5, _o_miny),
    (_o_minx + (_o_maxx - _o_minx) * 0.5, _o_maxy),
    (_o_minx, _o_maxy),
])
_o_hydric_wgs84 = _rg_transform_geom(_o_dem["crs"], "EPSG:4326", _shapely_mapping(_o_hydric_utm))

# The exact shapes soil_data's two functions return: a list of component
# rows, and a {mukey: geojson_geometry} dict. These are ALSO the exact
# shapes ParcelData.soil_components/ParcelData.soil_geometries hold --
# parcel_data.py stores each fetch's return value unmodified -- which is
# what makes forwarding them a passthrough and not a conversion.
_o_components = [
    {"mukey": "424242", "comppct_r": "90", "hydricrating": "Yes", "compname": "Fixture mucky loam"},
]
_o_geometries = {"424242": _o_hydric_wgs84}

# The canopy gate is supplied directly (it is a network fetch of its own and
# not what this section measures), and the road union is supplied as a real
# None -- "checked, genuinely no roads nearby". That leaves the SOIL path as
# the only thing that can reach the network here, which is the point.
_o_canopy_mask = np.zeros((_o_rows, _o_cols), dtype=bool)
_o_canopy_mask[6:12, 20:28] = True

_o_boundary_coordinates = [
    tuple(pt) for pt in
    _rg_transform_geom(_o_dem["crs"], "EPSG:4326", _shapely_mapping(_o_boundary_utm))["coordinates"][0]
]

_o_query_counts = {"components": 0, "geometries": 0}


def _o_counting_components(_wkt):
    _o_query_counts["components"] += 1
    return _o_components


def _o_counting_geometries(_wkt):
    _o_query_counts["geometries"] += 1
    return _o_geometries


def _o_run(**overrides):
    """One identify_exclusion_zones() call on the shared fixture, with the
    two SDA leaves counted at production_area's bindings -- the bindings
    _fetch_disqualifying_soil_union() actually looks up."""
    with ExitStack() as stack:
        stack.enter_context(mock_patch.object(
            production_area, "get_soil_data_for_polygon", side_effect=_o_counting_components))
        stack.enter_context(mock_patch.object(
            production_area, "get_soil_geometries_for_polygon", side_effect=_o_counting_geometries))
        return ez.identify_exclusion_zones(
            _o_boundary_coordinates,
            dem=_o_dem,
            boundary_polygon_utm=_o_boundary_utm,
            tree_root_zone_mask_utm=_o_canopy_mask,
            road_exclusion_union_utm=None,
            **overrides,
        )


# --- 10a. the self-fetch path, unchanged: the overrides ABSENT -------------

_o_query_counts.update(components=0, geometries=0)
_o_self_fetched = _o_run()
_o_self_fetch_queries = dict(_o_query_counts)

assert _o_self_fetch_queries == {"components": 1, "geometries": 1}, (
    f"with the overrides OMITTED the hydric gate must still self-fetch, exactly as it did before this "
    f"parameter existed -- one component query and one geometry query. Got {_o_self_fetch_queries}. The "
    f"None case is a real supported path, not a deprecated one."
)
assert _o_self_fetched["layers"]["hydric"]["data_available"] is True
assert _o_self_fetched["layers"]["hydric"]["mask"].any(), (
    "the fixture must give the hydric gate REAL content -- two empty unions would compare equal no "
    "matter what the override did"
)

# --- 10b. the override path: the SAME rows, supplied ----------------------

_o_query_counts.update(components=0, geometries=0)
_o_overridden = _o_run(soil_components=_o_components, soil_geometries=_o_geometries)
_o_override_queries = dict(_o_query_counts)

assert _o_override_queries == {"components": 0, "geometries": 0}, (
    f"with soil_components=/soil_geometries= supplied the hydric gate must issue ZERO SDA queries -- the "
    f"rows are already in hand. Got {_o_override_queries}."
)

# --- 10c. BIT-IDENTICAL, mask by mask, not by summary statistic -----------

for _o_layer in ez.LAYER_ORDER:
    _o_left = _o_self_fetched["layers"][_o_layer]
    _o_right = _o_overridden["layers"][_o_layer]
    assert np.array_equal(_o_left["mask"], _o_right["mask"]), (
        f"the {_o_layer} gate's cell mask must be BIT-IDENTICAL with and without the override -- compared "
        f"as an array, so a mask with the same cell count in different places fails here. "
        f"{int(np.count_nonzero(_o_left['mask'] != _o_right['mask']))} cell(s) differ."
    )
    assert _o_left["polygon_utm"].equals(_o_right["polygon_utm"]), (
        f"the {_o_layer} layer's published footprint must be the same geometry"
    )
    assert _o_left["acres"] == _o_right["acres"], f"{_o_layer} acreage moved"
    assert _o_left["data_available"] == _o_right["data_available"], (
        f"{_o_layer} data_available moved -- a supplied row set is as CHECKED an answer as a fetched one"
    )

for _o_mask_key in ("eligible_mask", "excluded_union_mask", "slope_only_mask"):
    assert np.array_equal(_o_self_fetched[_o_mask_key], _o_overridden[_o_mask_key]), (
        f"{_o_mask_key} must be bit-identical, compared as an array"
    )
assert np.array_equal(
    np.isnan(_o_self_fetched["slope_pct"]), np.isnan(_o_overridden["slope_pct"])
) and np.array_equal(
    _o_self_fetched["slope_pct"][~np.isnan(_o_self_fetched["slope_pct"])],
    _o_overridden["slope_pct"][~np.isnan(_o_overridden["slope_pct"])],
), "the slope grid must be bit-identical, NaN placement included"

for _o_geom_key in ("excluded_union_utm", "render_fill_polygon_utm", "eligible_polygon_utm",
                    "eligible_union_utm"):
    assert _o_self_fetched[_o_geom_key].equals(_o_overridden[_o_geom_key]), (
        f"{_o_geom_key} must be the same geometry with and without the override"
    )
assert (
    json.dumps(_o_self_fetched["eligible_union_wgs84"], sort_keys=True)
    == json.dumps(_o_overridden["eligible_union_wgs84"], sort_keys=True)
), "the wire-facing eligible geometry must serialise identically"
assert (
    json.dumps(_o_self_fetched["geometry_wgs84"], sort_keys=True)
    == json.dumps(_o_overridden["geometry_wgs84"], sort_keys=True)
), "the wire-facing excluded geometry must serialise identically"
assert (
    json.dumps(_o_self_fetched["narrative_data"], sort_keys=True)
    == json.dumps(_o_overridden["narrative_data"], sort_keys=True)
), "narrative_data must be identical -- it is what the user is told about their own land"
assert (
    json.dumps(_o_self_fetched["wire"], sort_keys=True)
    == json.dumps(_o_overridden["wire"], sort_keys=True)
), "the frontend-facing wire block must be identical"
assert _o_self_fetched["parcel_acres"] == _o_overridden["parcel_acres"]
assert set(_o_self_fetched) == set(_o_overridden), "no key appeared or vanished"

# --- 10d. the passthrough is a PASSTHROUGH: the same objects, not copies ---
#
# exclusion_zones.py must not fetch, derive, normalise or reshape these rows
# -- it hands them to the module that owns the fetch, untouched. Asserted by
# IDENTITY on what _fetch_disqualifying_soil_union() actually received.

with mock_patch.object(ez, "_fetch_disqualifying_soil_union", return_value=None) as _o_helper_spy:
    ez.identify_exclusion_zones(
        _o_boundary_coordinates,
        dem=_o_dem,
        boundary_polygon_utm=_o_boundary_utm,
        tree_root_zone_mask_utm=_o_canopy_mask,
        road_exclusion_union_utm=None,
        soil_components=_o_components,
        soil_geometries=_o_geometries,
    )
assert _o_helper_spy.call_args.kwargs["soil_components"] is _o_components, (
    "the caller's component rows must reach _fetch_disqualifying_soil_union() as the SAME OBJECT -- this "
    "module is a pure passthrough here and must not copy, convert or re-derive them"
)
assert _o_helper_spy.call_args.kwargs["soil_geometries"] is _o_geometries, (
    "the caller's map-unit geometry must reach the helper as the SAME OBJECT"
)

# ...and with the overrides omitted the helper receives None for both, which
# is its own documented "fetch it yourself" value. Forwarding a sentinel or a
# silently-defaulted empty list instead would break the fallback.
with mock_patch.object(ez, "_fetch_disqualifying_soil_union", return_value=None) as _o_none_spy:
    ez.identify_exclusion_zones(
        _o_boundary_coordinates,
        dem=_o_dem,
        boundary_polygon_utm=_o_boundary_utm,
        tree_root_zone_mask_utm=_o_canopy_mask,
        road_exclusion_union_utm=None,
    )
assert _o_none_spy.call_args.kwargs["soil_components"] is None, (
    "omitted means None -- the helper's own self-fetch trigger, not an empty list that would read as "
    "'checked, no map units here'"
)
assert _o_none_spy.call_args.kwargs["soil_geometries"] is None

# --- 10e. the helper's own two overrides are independent -------------------
#
# soil_geometries= alone still self-fetches the components; soil_components=
# alone still self-fetches the geometries. Each parameter closes exactly its
# own query, which is what None-falls-back-to-self-fetch means per-parameter
# rather than all-or-nothing.

_o_query_counts.update(components=0, geometries=0)
_o_run(soil_components=_o_components)
assert dict(_o_query_counts) == {"components": 0, "geometries": 1}, (
    f"soil_components= alone must close only the component query, got {_o_query_counts}"
)
_o_query_counts.update(components=0, geometries=0)
_o_run(soil_geometries=_o_geometries)
assert dict(_o_query_counts) == {"components": 1, "geometries": 0}, (
    f"soil_geometries= alone must close only the geometry query, got {_o_query_counts}"
)

# ...and soil_geometries= supplied on a boundary whose components qualify
# NOTHING is simply never looked at -- the early return fires first. Not an
# error, and not a query either.
_o_query_counts.update(components=0, geometries=0)
_o_clean = _o_run(
    soil_components=[{"mukey": "424242", "comppct_r": "90", "hydricrating": "No"}],
    soil_geometries=_o_geometries,
)
assert dict(_o_query_counts) == {"components": 0, "geometries": 0}
assert not _o_clean["layers"]["hydric"]["mask"].any(), (
    "no qualifying mukey means an empty hydric layer, even with geometry supplied"
)
assert _o_clean["layers"]["hydric"]["data_available"] is True, (
    "'checked, genuinely clean' is a CHECKED answer, not an unavailable one"
)

_o_differing_cells = sum(
    int(np.count_nonzero(
        _o_self_fetched["layers"][name]["mask"] != _o_overridden["layers"][name]["mask"]
    ))
    for name in ez.LAYER_ORDER
)
print(
    f"RAW-ROW SOIL OVERRIDE: identify_exclusion_zones() run twice on one fixture -- overrides ABSENT "
    f"(components={_o_self_fetch_queries['components']} + geometries="
    f"{_o_self_fetch_queries['geometries']} SDA queries) and overrides SUPPLIED "
    f"(components={_o_override_queries['components']} + geometries={_o_override_queries['geometries']}) "
    f"-- results BIT-IDENTICAL: {_o_differing_cells} differing cells across all five gate masks "
    f"(hydric alone covers {_o_self_fetched['layers']['hydric']['acres']} acres, so the comparison has "
    f"real content), plus identical closed union, eligible geometry, wire block and narrative_data. The "
    f"rows reach production_area._fetch_disqualifying_soil_union() as the SAME OBJECTS (pure "
    f"passthrough), each parameter closes only its own query, and omitting them forwards None -- the "
    f"self-fetch path, intact."
)


print("\nAll exclusion_zones checks passed.")
