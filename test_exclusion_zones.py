"""
test_exclusion_zones.py

Offline (no-network) checks for exclusion_zones.py. Every fixture below is
SYNTHETIC -- a hand-built DEM dict and a hand-built boundary polygon. No
real-property figure is computed, asserted or reproduced here; the acreages
quoted in exclusion_zones.py's own docstring came from a separate
diagnose_exclusion_footprints.py run against the two reference boundaries
and are context for the radii, not test data.

What this file proves, in order:

  1. PRODUCTION IS COMPLETELY UNCHANGED. This branch's core guarantee.
     production_area.py and production_area_ceiling.py do not appear in
     `git diff` at all; compute_step1_eligible_cells()' signature is
     untouched; and its eligible_mask, every other array it returns, and
     every scored patch identify_optimized_production_areas() produces are
     BYTE-IDENTICAL before and after this module runs against the same
     fixture.
  2. THE DEFERRED INTEGRATION IS MEASURABLE BUT NOT APPLIED. Computes what
     production's eligible mask WOULD be if it were gated on this module's
     closed exclusions, and reports the acreage difference -- in the test,
     not as a code path in the module. Input for the later decision.
  3. PER-GATE RADII ARE APPLIED INDEPENDENTLY. Canopy and slope close;
     the setback does not, and its polygon count is unchanged -- while the
     same ring closed at the canopy/slope radius demonstrably WOULD change,
     so the difference is the per-gate radius and not a ring that happens
     to be immune.
  4. THE EXTENSIVE INVARIANT, BOTH DIRECTIONS. render_fill_polygon_utm
     CONTAINS the raw union (the direction a production-style assertion
     gets backwards) and is CONTAINED BY boundary_polygon_utm.
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
# 1. PRODUCTION IS COMPLETELY UNCHANGED
# ===========================================================================
#
# Three independent checks, because "unchanged" can fail three different
# ways: the file could be edited (git), the entry point's contract could be
# widened without editing behaviour (signature), or a shared import could
# mutate state production reads (byte-comparison across this module's run).

_git_diff = subprocess.run(
    ["git", "diff", "--stat", "HEAD", "--", "production_area.py", "production_area_ceiling.py"],
    capture_output=True,
    text=True,
    cwd=".",
)
_git_diff_all = subprocess.run(
    ["git", "diff", "--stat"], capture_output=True, text=True, cwd="."
)
if _git_diff.returncode == 0:
    assert _git_diff.stdout.strip() == "", (
        "production_area.py / production_area_ceiling.py must not be modified by this branch at all -- "
        f"`git diff --stat HEAD --` reported:\n{_git_diff.stdout}"
    )
    assert "production_area.py" not in _git_diff_all.stdout, (
        "production_area.py must not appear in `git diff --stat`:\n" + _git_diff_all.stdout
    )
    assert "production_area_ceiling.py" not in _git_diff_all.stdout, (
        "production_area_ceiling.py must not appear in `git diff --stat`:\n" + _git_diff_all.stdout
    )
    print(
        "production is untouched at the FILE level: neither production_area.py nor "
        "production_area_ceiling.py appears in `git diff --stat`."
    )
else:
    print("(git unavailable in this environment -- file-level diff check skipped)")

assert list(inspect.signature(compute_step1_eligible_cells).parameters) == [
    "dem",
    "boundary_polygon_utm",
    "disqualifying_soil_union_utm",
    "max_slope_pct",
    "tree_root_zone_mask_utm",
    "boundary_setback_meters",
    "road_exclusion_union_utm",
], (
    "compute_step1_eligible_cells()'s parameter list must be UNCHANGED -- no eligible-mask override, no "
    "exclusion-result parameter. The integration is deferred; see exclusion_zones.py's DELIBERATE "
    f"REDUNDANCY section. Got: {list(inspect.signature(compute_step1_eligible_cells).parameters)}"
)
print(
    "production is untouched at the CONTRACT level: compute_step1_eligible_cells() still takes exactly "
    "its seven original parameters -- no override was added."
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
# 2. THE DEFERRED INTEGRATION IS MEASURABLE BUT NOT APPLIED
# ===========================================================================
#
# Computed HERE, in the test, from two things the module already returns --
# never as a code path inside exclusion_zones.py, and never by touching
# production. This is the number the later integration decision needs: how
# much ground production can currently claim that the closed exclusions
# would take away from it.

_production_eligible = _after_raw["eligible_mask"]
_would_be_eligible = _production_eligible & (~_p_result["excluded_union_mask"])
_area_per_cell = cell_area_acres(_p_dem)
_lost_cells = int(_production_eligible.sum()) - int(_would_be_eligible.sum())
_lost_acres = _lost_cells * _area_per_cell

assert (_would_be_eligible & ~_production_eligible).sum() == 0, (
    "gating production on the CLOSED exclusions can only ever REMOVE cells, never add any -- a closing "
    "is extensive, so this direction must be empty"
)
assert _lost_cells >= 0
assert _production_eligible.tobytes() == _run_production_step1()["eligible_mask"].tobytes(), (
    "measuring the hypothetical must not have altered production's real answer"
)
print(
    f"DEFERRED INTEGRATION, MEASURED NOT APPLIED: production's eligible mask is "
    f"{int(_production_eligible.sum())} cells ({int(_production_eligible.sum()) * _area_per_cell:.3f} ac) "
    f"on this fixture. Gated on this module's closed exclusions it would be "
    f"{int(_would_be_eligible.sum())} cells ({int(_would_be_eligible.sum()) * _area_per_cell:.3f} ac) -- a "
    f"loss of {_lost_cells} cell(s) = {_lost_acres:.3f} ac of pinhole ground. production_area.py is NOT "
    "modified; this figure is computed in the test as input to the integration decision."
)


# ===========================================================================
# 3. PER-GATE RADII ARE APPLIED INDEPENDENTLY
# ===========================================================================
#
# One fixture carrying all three shapes at once: canopy with pinholes, a
# steep region with pinholes, and the setback ring (which the cell grid
# fragments on its own). The point is not merely that the setback is
# unchanged -- it is that the SAME ring closed at the canopy/slope radius
# demonstrably WOULD change. That is what makes this a test of per-gate
# radii rather than a test of a ring that happens to be closing-proof.

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
# it here makes the "polygon count unchanged" assertion below a real one:
# without it the ring is a single connected loop and the count is trivially 1.
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

for _closing_gate in ("canopy", "slope"):
    _raw = _r_layers[_closing_gate]["raw_mask"]
    _closed = _r_layers[_closing_gate]["mask"]
    assert int(_closed.sum()) > int(_raw.sum()), (
        f"the {_closing_gate} layer is configured at "
        f"{ez.CLOSING_RADIUS_METERS_BY_LAYER[_closing_gate]} m and this fixture gives it pinholes to "
        f"absorb, so its closed mask must be strictly larger -- got {int(_raw.sum())} -> {int(_closed.sum())}"
    )

_setback_raw = _r_layers["setback"]["raw_mask"]
_setback_closed = _r_layers["setback"]["mask"]
assert _setback_raw.tobytes() == _setback_closed.tobytes(), (
    "the setback layer is configured at 0.0 m -- a MEASURED decision, not an untuned placeholder (closing "
    "a ring over-merges across the parcel; see SETBACK_EXCLUSION_CLOSING_RADIUS_METERS) -- so its closed "
    "mask must be bit-for-bit its raw mask"
)
_setback_raw_parts = polygon_part_count(cell_union_footprint(_r_dem, _setback_raw))
assert _setback_raw_parts > 1, (
    "fixture sanity: the setback ring must be genuinely FRAGMENTED here (steep spikes along it move those "
    f"cells into the slope layer), otherwise the polygon-count assertion is trivial -- got "
    f"{_setback_raw_parts} part(s)"
)
_setback_closed_parts = polygon_part_count(_r_layers["setback"]["polygon_utm"])
assert _setback_raw_parts == _setback_closed_parts, (
    f"the setback's polygon count must be unchanged by this module -- {_setback_raw_parts} -> "
    f"{_setback_closed_parts}"
)

# ...and the same ring at the canopy/slope radius WOULD change, which is what
# makes the assertion above about the RADIUS rather than about the ring.
_setback_if_closed = disc_closing(_setback_raw, 1)
assert int(_setback_if_closed.sum()) > int(_setback_raw.sum()), (
    "fixture sanity: this setback ring is not closing-proof -- at the canopy/slope radius it WOULD gain "
    "cells. The assertion above therefore proves the per-gate radius is applied, not that the ring is inert"
)
assert ez.CLOSING_RADIUS_METERS_BY_LAYER == {
    "canopy": 5.0,
    "slope": 5.0,
    "hydric": 0.0,
    "roads": 0.0,
    "setback": 0.0,
}, "the five radii are separate, per-gate constants -- not one shared value"
print(
    f"PER-GATE RADII APPLIED INDEPENDENTLY on one fixture: canopy closed "
    f"({int(_r_layers['canopy']['raw_mask'].sum())} -> {int(_r_layers['canopy']['mask'].sum())} cells), "
    f"slope closed ({int(_r_layers['slope']['raw_mask'].sum())} -> {int(_r_layers['slope']['mask'].sum())}), "
    f"setback UNCHANGED ({int(_setback_raw.sum())} cells, {_setback_raw_parts} polygon(s) before and "
    f"after) -- while that same ring closed at 5 m would have gained "
    f"{int(_setback_if_closed.sum()) - int(_setback_raw.sum())} cells."
)


# ===========================================================================
# 4. THE EXTENSIVE INVARIANT, BOTH DIRECTIONS
# ===========================================================================
#
# The production-style assertion (render_fill.area <= polygon_utm.area) is
# BACKWARDS here and would fail: a closing is extensive. Both directions are
# asserted so neither can be dropped later as "obviously true".

for _label, _res, _bnd in (("pinhole fixture", _p_result, _p_boundary), ("radii fixture", _r_result, _r_boundary)):
    _render = _res["render_fill_polygon_utm"]
    _raw_union = _res["raw_excluded_union_utm"]
    assert not _render.is_empty, f"{_label}: fixture sanity -- something must be excluded"
    assert _render.contains(_raw_union) or _raw_union.difference(_render).area < _TOLERANCE_M2, (
        f"{_label}: render_fill_polygon_utm must CONTAIN the raw union. This is the direction a "
        "production-style containment assertion gets backwards -- a closing only ever adds ground, so "
        "there is no smaller footprint to clip back to"
    )
    assert _render.difference(_bnd).area < _TOLERANCE_M2, (
        f"{_label}: render_fill_polygon_utm must be within boundary_polygon_utm -- the clip to the drawn "
        "boundary is the ONLY clip that applies to this layer"
    )
    assert _render.area >= _raw_union.area - _TOLERANCE_M2, f"{_label}: the closed union cannot be smaller"
    assert _res["render_fill_polygon_utm"] is _res["excluded_union_utm"], (
        f"{_label}: render_fill_polygon_utm IS excluded_union_utm here, deliberately -- there is no "
        "display-only opening to apply to a closing"
    )
_p_gain = _p_result["render_fill_polygon_utm"].area - _p_result["raw_excluded_union_utm"].area
print(
    "EXTENSIVE INVARIANT, BOTH DIRECTIONS: raw_union ⊆ render_fill_polygon_utm ⊆ boundary_polygon_utm on "
    f"both fixtures. The closing GREW the union by {_p_gain:.1f} m² on the pinhole fixture -- the "
    "production-style `render_fill.area <= polygon_utm.area` assertion would fail here, which is why it "
    "is deliberately absent."
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
_n_slope_raw = _n_result["layers"]["slope"]["raw_mask"]
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
        assert "closing_radius_ft" in _entry and "effective_closing_radius_ft" in _entry, (
            f"{_nd_label}: the closing radius applied per gate must be reported, in feet (imperial) and "
            "with the effective (quantized) value alongside the requested one"
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
    "float/None, every float rounded to 1 decimal, radii in FEET, no numpy scalars and no geometry."
)


# ===========================================================================
# 8. THE REDUNDANCY IS BOUNDED AND EXPECTED -- EXACT COUNTS PER GATE HELPER
# ===========================================================================
#
# The concrete, measured cost of deferring the production integration --
# counted at the shared GATE-HELPER bindings, which is what these counters
# actually measure (NOT every road/canopy/soil touch in the pipeline;
# build_pipeline_context()'s own separate existing_roads fetch, mocked below,
# is outside these counts). Because production still self-computes its own
# five gates, one full build_pipeline_context() run reaches the canopy and
# soil helpers TWICE and the slope grid TWICE -- once for exclusion_zones,
# once for production. The ROAD helper is the exception: build_pipeline_
# context() now passes its own already-fetched existing_roads union into
# identify_exclusion_zones() as road_exclusion_union_utm= (reused even when
# it is a real None -- "checked, genuinely no roads nearby"), so exclusion_
# zones' own road self-fetch never fires and the helper runs exactly ONCE
# (production's untouched self-compute). One more of ANY count would mean
# something re-fetches beyond the known duplication, which is exactly the
# failure the override pattern exists to prevent, so this asserts equality
# and not an upper bound.
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
_counts = {"canopy": 0, "soil": 0, "road": 0, "slope": 0}


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
    # the two canopy bindings
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
    _enter(mock_patch.object(pc.water_candidate_zones, "identify_water_system_candidate_zones",
                             return_value={"zones_geojson": _empty_fc}))
    _enter(mock_patch.object(pc, "fetch_and_select_optimal_water_zone", return_value=None))
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

_expected_gate_counts = {"canopy": 2, "soil": 2, "slope": 2, "road": 1}
for _gate, _expected in _expected_gate_counts.items():
    assert _counts[_gate] == _expected, (
        f"the {_gate} gate helper must run EXACTLY {_expected}x across one build_pipeline_context() run "
        f"(canopy/soil/slope: once for exclusion_zones, once for production -- the known and deliberate "
        f"duplication this module accepts, see exclusion_zones.py's DELIBERATE REDUNDANCY section; road: "
        f"once, production's own self-compute only, since build_pipeline_context() passes its own "
        f"existing_roads union into identify_exclusion_zones() and that module reuses it -- a real None "
        f"included). Got {_counts[_gate]}. One more means something is re-fetching beyond the known "
        "duplication and the override pattern is failing somewhere it should be preventing it; one fewer "
        "on canopy/soil/slope would mean production stopped self-computing, which this module does not do, "
        "and a road count of 0 would mean production's own gate stopped running."
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
    "REDUNDANCY BOUNDED AND EXPECTED: across one full build_pipeline_context() run the measured gate-helper "
    f"call counts are canopy={_counts['canopy']}, soil={_counts['soil']}, slope={_counts['slope']} (2x each: "
    f"once for exclusion_zones, once for production) and road={_counts['road']} (1x: production's own "
    "self-compute only -- exclusion_zones reuses the union build_pipeline_context() passes in, a real None "
    "included). That is the concrete, measured cost of deferring the production integration."
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
    f"RENDERED: the closed union draws as flat {rlm.EXCLUSION_ZONE_COLOR} fill at alpha "
    f"{rlm.EXCLUSION_ZONE_ALPHA}, no edge stroke, zorder {rlm.EXCLUSION_ZONE_ZORDER} -- below the streams "
    f"(20) and every KSOP layer above them -- contributing exactly one legend entry "
    f"({rlm.LEGEND_LABEL_EXCLUSION!r}). A layers dict without the key renders with no exclusion layer and "
    "no legend entry."
)


print("\nAll exclusion_zones checks passed.")
