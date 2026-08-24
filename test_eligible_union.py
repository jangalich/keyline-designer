"""
test_eligible_union.py

Offline (no-network) checks for exclusion_zones.build_eligible_union() and the
wire fields identify_exclusion_zones() now ships alongside it, plus the
assertions that belong to diagnose_eligible_union_staircase.py's measurement
pass (the diagnostic itself only prints; what must never silently change is
asserted here).

WHAT THE ELIGIBLE UNION IS. One geometry covering every piece of ground a user
may select in the interactive design flow: STEP 1's gate-eligible cells,
8-connected clustered, clusters under ELIGIBLE_UNION_MIN_CLUSTER_ACRES
dropped, footprints unioned, clipped to the boundary. It is a DISPLAY AND
CLAMPING geometry. The five per-gate exclusion layers are NOT derived from it
and stay exact, because their acreages become user-facing caution figures.

THE MEASUREMENT PASS ASSERTS NOTHING ABOUT WHICH STAIRCASE-REMOVAL OPTION IS
BETTER. That is the reviewer's call. What is asserted here is that the
bounded-error property Option 2 is chosen FOR actually holds -- and, where the
property does not hold as it was originally stated, that the discrepancy is
pinned down rather than left as a claim. See section 7.

All fixtures are synthetic and deterministic; nothing here touches the network
or any real-property number.
"""

import numpy as np
from shapely.geometry import Polygon, box
from unittest.mock import patch as mock_patch

import diagnose_eligible_union_staircase as diag
import exclusion_zones as ez
from exclusion_zones import (
    ELIGIBLE_UNION_CONNECTIVITY,
    ELIGIBLE_UNION_MIN_CLUSTER_ACRES,
    LAYER_ORDER,
    build_eligible_union,
)
from production_area import compute_step1_eligible_cells
from production_area_ceiling import PRODUCTION_CEILING_PCT_OF_PARCEL, trim_to_ceiling
from raster_grid import SQUARE_METERS_PER_ACRE, cell_area_acres

CELL = 5.0
_ACRE = SQUARE_METERS_PER_ACRE


def _dem(rows, cols, array=None):
    if array is None:
        array = np.zeros((rows, cols), dtype=np.float32)
    return {
        "array": array,
        "resolution_meters": (CELL, CELL),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }


def _full_boundary(dem, inset_cells=0):
    rows, cols = dem["array"].shape
    return box(
        dem["origin_x"] + inset_cells * CELL,
        dem["origin_y"] - (rows - inset_cells) * CELL,
        dem["origin_x"] + (cols - inset_cells) * CELL,
        dem["origin_y"] - inset_cells * CELL,
    )


def _mask(shape, cells):
    mask = np.zeros(shape, dtype=bool)
    for r, c in cells:
        mask[r, c] = True
    return mask


def _rect(r0, r1, c0, c1):
    return [(r, c) for r in range(r0, r1) for c in range(c0, c1)]


def _sloped(rows, cols, rise=0.6, seed=4):
    rng = np.random.default_rng(seed)
    array = np.zeros((rows, cols), dtype=np.float32)
    for r in range(rows):
        array[r, :] = 100.0 + r * rise
    array += (rng.standard_normal((rows, cols)).cumsum(axis=1) * 0.05).astype(np.float32)
    return array


def _run_exclusions(dem, boundary, canopy):
    """identify_exclusion_zones() with all three fetch-backed gates stubbed --
    fully offline, and the canopy mask handed in directly."""
    with mock_patch.object(ez, "get_required_tree_root_zone_mask_utm", return_value=canopy), mock_patch.object(
        ez, "_fetch_disqualifying_soil_union", return_value=None
    ), mock_patch.object(ez, "_fetch_road_exclusion_union_utm", return_value=None):
        return ez.identify_exclusion_zones(None, dem=dem, boundary_polygon_utm=boundary)


# The standard fixture every section below reuses: a real sloped DEM with a
# nodata patch, a boundary inset from the DEM edge, and two canopy stands (one
# carrying a deliberate pinhole).
_ROWS = _COLS = 60
_DEM = _dem(_ROWS, _COLS, _sloped(_ROWS, _COLS))
_DEM["array"][10:14, 20:26] = np.nan
_BOUNDARY = _full_boundary(_DEM, inset_cells=3)
_CANOPY = np.zeros((_ROWS, _COLS), dtype=bool)
_CANOPY[18:34, 8:28] = True
_CANOPY[24, 16] = False
_CANOPY[40:47, 36:50] = True
_RESULT = _run_exclusions(_DEM, _BOUNDARY, _CANOPY)


# ===========================================================================
# 0. THE UNION IS BUILT FROM STEP 1's OWN GATE OUTPUT
# ===========================================================================
#
# identify_exclusion_zones() feeds build_eligible_union() its own UNCLOSED
# gate complement rather than calling compute_step1_eligible_cells() a sixth
# time. That shortcut is only legitimate if the two masks are the same mask,
# so it is checked against a real call rather than argued from the docstring.

_step1 = compute_step1_eligible_cells(
    _DEM,
    _BOUNDARY,
    disqualifying_soil_union_utm=None,
    tree_root_zone_mask_utm=_CANOPY,
    road_exclusion_union_utm=None,
)
_raw_union = np.zeros((_ROWS, _COLS), dtype=bool)
for _name in LAYER_ORDER:
    _raw_union |= _RESULT["layers"][_name]["raw_mask"]
_on_parcel = ez._on_parcel_mask(_DEM, _BOUNDARY)
_derived_step1 = _on_parcel & ~(_raw_union & _on_parcel)

assert np.array_equal(_derived_step1, _step1["eligible_mask"]), (
    "identify_exclusion_zones()' UNCLOSED gate complement must be byte-identical to "
    "compute_step1_eligible_cells()' own eligible_mask -- it is used in place of one"
)
# And it must NOT be the CLOSED complement, which is smaller by the pinholes.
assert int(_RESULT["eligible_mask"].sum()) < int(_step1["eligible_mask"].sum()), (
    "fixture sanity: the closing must actually absorb pinholes here, otherwise this "
    "fixture cannot tell the closed and unclosed complements apart"
)
print(
    f"0. STEP 1 SOURCE: the unclosed gate complement is byte-identical to compute_step1_eligible_cells()' "
    f"own eligible_mask ({int(_step1['eligible_mask'].sum())} cells). The CLOSED complement is "
    f"{int(_step1['eligible_mask'].sum()) - int(_RESULT['eligible_mask'].sum())} cells smaller and is "
    f"deliberately NOT what the union is built from."
)


# ===========================================================================
# 2. THE UNION IS BUILT PRE-CEILING
# ===========================================================================
#
# The branch's most consequential design decision, demonstrated on a fixture
# where the ceiling actually fires rather than asserted in a comment.

_FLAT_ROWS = _FLAT_COLS = 50
_FLAT_DEM = _dem(_FLAT_ROWS, _FLAT_COLS)  # perfectly flat: every cell clears the slope gate
_FLAT_BOUNDARY = _full_boundary(_FLAT_DEM, inset_cells=1)
_FLAT_CANOPY = np.zeros((_FLAT_ROWS, _FLAT_COLS), dtype=bool)
_FLAT_RESULT = _run_exclusions(_FLAT_DEM, _FLAT_BOUNDARY, _FLAT_CANOPY)

_flat_step1 = compute_step1_eligible_cells(
    _FLAT_DEM,
    _FLAT_BOUNDARY,
    disqualifying_soil_union_utm=None,
    tree_root_zone_mask_utm=_FLAT_CANOPY,
    road_exclusion_union_utm=None,
)
_trim = trim_to_ceiling(_flat_step1, _FLAT_DEM, _FLAT_BOUNDARY)

assert _trim["cells_removed"] > 0, (
    f"fixture sanity: this fixture must actually FIRE the {PRODUCTION_CEILING_PCT_OF_PARCEL}% ceiling, "
    f"otherwise it demonstrates nothing -- got {_trim['cells_removed']} cells removed"
)

_area_per_cell = cell_area_acres(_FLAT_DEM)
_trimmed_away = _flat_step1["eligible_mask"].copy()
for _r, _c in _trim["survivor_cells"]:
    _trimmed_away[_r, _c] = False
_trimmed_away_acres = int(_trimmed_away.sum()) * _area_per_cell

# The trimmed ground must be INSIDE the eligible union: it passed every
# physical gate and was dropped by an advisory design judgement.
_flat_union = _FLAT_RESULT["eligible_union_utm"]
_trimmed_footprint = ez._mask_polygon(_FLAT_DEM, _trimmed_away, _FLAT_BOUNDARY)
_uncovered = _trimmed_footprint.difference(_flat_union).area

assert _uncovered < 1e-6, (
    f"every cell the ceiling trim removed must still be inside the eligible union -- the ceiling is "
    f"advisory and must not narrow the highlight; {_uncovered:.6f} m2 was left out"
)
# And the survivors-only union really is smaller, so this is a live difference.
_survivor_mask = _mask((_FLAT_ROWS, _FLAT_COLS), _trim["survivor_cells"])
_survivor_union = build_eligible_union(_FLAT_DEM, _survivor_mask, _FLAT_BOUNDARY)
_ceiling_gap_acres = (_flat_union.area - _survivor_union.area) / _ACRE
assert _ceiling_gap_acres > 0.0, "the pre-ceiling union must be strictly larger than a post-ceiling one"

print(
    f"2. PRE-CEILING: on a ceiling-firing fixture ({_trim['pre_trim_acres']} ac eligible on a "
    f"{_trim['parcel_acres']} ac parcel, ceiling {PRODUCTION_CEILING_PCT_OF_PARCEL}%), the trim removes "
    f"{_trim['cells_removed']} cells = {_trimmed_away_acres:.3f} ac. ALL of it stays inside the eligible "
    f"union (0 m2 left out). Building post-ceiling instead would shrink the highlight by "
    f"{_ceiling_gap_acres:.3f} ac -- ground that passed every physical gate."
)


# ===========================================================================
# 3. THE CLUSTER FLOOR DROPS WHAT IT SHOULD
# ===========================================================================

_SPECK_SHAPE = (40, 40)
_SPECK_DEM = _dem(*_SPECK_SHAPE)
_SPECK_BOUNDARY = _full_boundary(_SPECK_DEM)
_speck_cells = _rect(5, 7, 5, 7)  # 4 cells -- 0.025 ac at 5 m, under the floor
_pocket_cells = _rect(20, 24, 20, 25)  # 20 cells -- 0.124 ac, over it
_speck_mask = _mask(_SPECK_SHAPE, _speck_cells + _pocket_cells)

_speck_area = len(_speck_cells) * cell_area_acres(_SPECK_DEM)
_pocket_area = len(_pocket_cells) * cell_area_acres(_SPECK_DEM)
assert _speck_area < ELIGIBLE_UNION_MIN_CLUSTER_ACRES < _pocket_area, (
    f"fixture sanity: the speck ({_speck_area:.4f} ac) must sit below and the pocket "
    f"({_pocket_area:.4f} ac) above the {ELIGIBLE_UNION_MIN_CLUSTER_ACRES} ac floor"
)

_floored = build_eligible_union(_SPECK_DEM, _speck_mask, _SPECK_BOUNDARY)
_speck_footprint = ez._mask_polygon(_SPECK_DEM, _mask(_SPECK_SHAPE, _speck_cells), _SPECK_BOUNDARY)
_pocket_footprint = ez._mask_polygon(_SPECK_DEM, _mask(_SPECK_SHAPE, _pocket_cells), _SPECK_BOUNDARY)

assert _floored.intersection(_speck_footprint).area < 1e-6, "the 4-cell speck must be DROPPED"
assert _pocket_footprint.difference(_floored).area < 1e-6, "the 20-cell pocket must SURVIVE intact"
assert abs(_floored.area - _pocket_footprint.area) < 1e-6, "nothing but the pocket may survive"
print(
    f"3. CLUSTER FLOOR: a 4-cell speck ({_speck_area:.4f} ac) is dropped and a 20-cell pocket "
    f"({_pocket_area:.4f} ac) survives intact at the {ELIGIBLE_UNION_MIN_CLUSTER_ACRES} ac floor."
)


# ===========================================================================
# 4. 4-CONNECTED VERSUS 8-CONNECTED, AT THREE FLOORS
# ===========================================================================
#
# Reported for all six combinations rather than asserted, so the reversal
# recorded in ELIGIBLE_UNION_CONNECTIVITY's docstring is a measured choice.

_CONN_SHAPE = (60, 60)
_CONN_DEM = _dem(*_CONN_SHAPE)
_CONN_BOUNDARY = _full_boundary(_CONN_DEM)
# Ground with genuine diagonal structure: two blocks joined only corner-to-
# corner, plus a diagonal chain of single cells -- the case the two
# connectivities actually disagree about.
_conn_cells = _rect(10, 18, 10, 18) + _rect(18, 26, 18, 26)
_conn_cells += [(30 + i, 30 + i) for i in range(12)]
_conn_cells += _rect(40, 44, 8, 12)
_conn_mask = _mask(_CONN_SHAPE, _conn_cells)

from raster_grid import connected_components  # noqa: E402  (used only for the table below)

def _connectivity_table(dem, mask, boundary):
    """cluster count, surviving-cluster count and surviving acres for both
    connectivities at all three floors -- the six-way comparison."""
    rows = []
    for conn in (4, 8):
        labels, count = connected_components(mask, connectivity=conn)
        for floor in (0.0, 0.05, 0.1):
            union = build_eligible_union(
                dem, mask, boundary, min_cluster_acres=floor, connectivity=conn
            )
            min_cells = 0 if floor <= 0 else int(np.ceil(floor / cell_area_acres(dem)))
            surviving = sum(1 for lab in range(count) if int((labels == lab).sum()) >= min_cells)
            rows.append((conn, floor, count, surviving, union.area / _ACRE))
    return rows


def _print_connectivity_table(title, rows):
    print(f"   {title}")
    print(f"     {'conn':>4s}  {'floor ac':>8s}  {'clusters':>8s}  {'surviving':>9s}  {'acres':>9s}")
    for conn, floor, count, surviving, ac in rows:
        print(f"     {conn:>4d}  {floor:>8.2f}  {count:>8d}  {surviving:>9d}  {ac:>9.4f}")


# TWO fixtures on purpose. The first is built to MAKE the two connectivities
# disagree (corner-touching blocks, a single-cell diagonal chain) and shows the
# largest difference the choice can produce. The second is the realistic gate
# output from the standard fixture above, and answers the question that
# actually matters: does the choice matter on ground that looks like ground?
_designed_rows = _connectivity_table(_CONN_DEM, _conn_mask, _CONN_BOUNDARY)
_realistic_rows = _connectivity_table(_DEM, _step1["eligible_mask"], _BOUNDARY)

print("4. CONNECTIVITY x FLOOR -- six combinations, two fixtures:")
_print_connectivity_table("(a) DESIGNED to disagree: corner-touching blocks + a 1-cell diagonal chain", _designed_rows)
_print_connectivity_table("(b) REALISTIC: the standard fixture's own STEP 1 gate output", _realistic_rows)

for _rows, _which in ((_designed_rows, "designed"), (_realistic_rows, "realistic")):
    _keyed = {(c, f): (cl, sv, ac) for c, f, cl, sv, ac in _rows}
    assert abs(_keyed[(8, 0.0)][2] - _keyed[(4, 0.0)][2]) < 1e-9, (
        f"{_which}: at a zero floor the two connectivities must union to exactly the same ground -- "
        "they differ in how cells are GROUPED, not in which cells are eligible"
    )

_designed = {(c, f): (cl, sv, ac) for c, f, cl, sv, ac in _designed_rows}
_realistic = {(c, f): (cl, sv, ac) for c, f, cl, sv, ac in _realistic_rows}
_designed_delta = _designed[(8, 0.05)][2] - _designed[(4, 0.05)][2]
_realistic_delta = _realistic[(8, 0.05)][2] - _realistic[(4, 0.05)][2]

print(
    f"   At a 0.0 floor both connectivities give identical acreage on both fixtures -- grouping only\n"
    f"   matters once a floor is applied.\n"
    f"   At the {ELIGIBLE_UNION_MIN_CLUSTER_ACRES} ac floor: on the DESIGNED fixture 8-connectivity keeps\n"
    f"   {_designed_delta:+.4f} ac more ({_designed[(4, 0.05)][0]} clusters collapsing to "
    f"{_designed[(8, 0.05)][0]}, {_designed[(4, 0.05)][1]} vs {_designed[(8, 0.05)][1]} of them surviving\n"
    f"   the floor). On the REALISTIC fixture the difference is {_realistic_delta:+.4f} ac "
    f"({_realistic[(4, 0.05)][0]} vs {_realistic[(8, 0.05)][0]} clusters).\n"
    f"   Read that second number as the honest one: the connectivity choice is worth having reasoned\n"
    f"   about, but on ground that looks like ground it is close to immaterial at this floor."
)


# ===========================================================================
# 5. eligible_union_utm AND eligible_polygon_utm ARE DISTINGUISHABLE
# ===========================================================================

_eu = _RESULT["eligible_union_utm"]
_ep = _RESULT["eligible_polygon_utm"]
assert not _eu.equals(_ep), "the two eligible geometries must not be the same shape"
assert abs(_eu.area - _ep.area) > 1.0, (
    f"the two eligible geometries must differ measurably, not by float noise -- "
    f"{_eu.area:.3f} vs {_ep.area:.3f} m2"
)

# Each field's own docstring must name the other and say what separates them,
# so the difference survives a reader who only opens one of them.
_fn_doc = build_eligible_union.__doc__
_ret_doc = ez.identify_exclusion_zones.__doc__
for _needle in ("eligible_polygon_utm", "eligible_union_utm", "eligible_mask"):
    assert _needle in _ret_doc, f"identify_exclusion_zones()'s docstring must name {_needle}"
assert "CLUSTER FLOOR" in _ret_doc and "CLOSING" in _ret_doc, (
    "the return docstring must state BOTH ways the two eligible geometries differ"
)
assert "ceiling" in _fn_doc.lower() and "advisory" in _fn_doc.lower(), (
    "build_eligible_union()'s docstring must record the pre-ceiling decision"
)
print(
    f"5. DISTINGUISHABLE: eligible_union_utm ({_eu.area / _ACRE:.3f} ac) and eligible_polygon_utm "
    f"({_ep.area / _ACRE:.3f} ac) differ by {abs(_eu.area - _ep.area) / _ACRE:.3f} ac, and both are "
    f"documented against each other in identify_exclusion_zones()'s own return docstring."
)


# ===========================================================================
# 6. THE WIRE FIELDS
# ===========================================================================

_wire = _RESULT["wire"]
_types = [layer["type"] for layer in _wire["layers"]]
_labels = [layer["label"] for layer in _wire["layers"]]

assert _types == list(LAYER_ORDER), f"wire type identifiers must be LAYER_ORDER exactly, got {_types}"
assert len(set(_types)) == len(_types), "type identifiers must be distinct"
for _t, _l in zip(_types, _labels):
    assert _t != _l, f"the type identifier and the display label must not be the same string ({_t!r})"
assert all(isinstance(_l, str) and _l for _l in _labels), "every layer needs a non-empty display label"

# Labels state the TEST, not the layer name: the two thresholds carry their
# real numbers, taken from the run rather than hardcoded.
_label_by_type = dict(zip(_types, _labels))
assert "20" in _label_by_type["slope"] and "%" in _label_by_type["slope"], (
    f"the slope label must state the threshold it tested, got {_label_by_type['slope']!r}"
)
assert "ft" in _label_by_type["setback"], (
    f"the setback label must state the distance it tested, got {_label_by_type['setback']!r}"
)
# ...and they track the parameters actually used, rather than being constants.
_retuned = _run_exclusions(_DEM, _BOUNDARY, _CANOPY)
with mock_patch.object(ez, "get_required_tree_root_zone_mask_utm", return_value=_CANOPY), mock_patch.object(
    ez, "_fetch_disqualifying_soil_union", return_value=None
), mock_patch.object(ez, "_fetch_road_exclusion_union_utm", return_value=None):
    _retuned = ez.identify_exclusion_zones(
        None, dem=_DEM, boundary_polygon_utm=_BOUNDARY, max_slope_pct=12.5
    )
_retuned_slope_label = [l["label"] for l in _retuned["wire"]["layers"] if l["type"] == "slope"][0]
assert "12.5" in _retuned_slope_label, (
    f"the slope label must reflect the max_slope_pct this run actually used, got {_retuned_slope_label!r}"
)

# Cell dimensions: both, in metres.
_cell_size = _wire["cell_size_meters"]
assert isinstance(_cell_size, list) and len(_cell_size) == 2, (
    f"cell_size_meters must ship BOTH dimensions -- DEM resolution is not square (the two reference "
    f"DEMs are 4.99 x 5.00 and 5.00 x 4.99) -- got {_cell_size!r}"
)
assert all(isinstance(_v, float) for _v in _cell_size), "cell dimensions must be plain floats for JSON"
assert _cell_size == [_DEM["resolution_meters"][0], _DEM["resolution_meters"][1]], "x then y, in that order"

# The setback caveat, machine-readable.
assert _wire["setback_is_lower_bound"] is True, "the setback caveat flag must be carried to the wire"
assert _wire["setback_lower_bound_reason"] == "steep_ring_ground_counted_in_slope_layer", (
    "the caveat's reason CODE must be carried, not just the flag"
)
assert _wire["setback_lower_bound_reason"] == _RESULT["narrative_data"]["setback_lower_bound_reason"], (
    "the wire's reason code and narrative_data's must not be allowed to drift apart"
)

# Off-parcel is NOT shipped: the frontend has the boundary and derives the scrim.
_wire_text = repr(_wire).lower()
for _forbidden in ("off_parcel", "offparcel", "scrim", "halo"):
    assert _forbidden not in _wire_text, f"the wire must not ship off-parcel geometry ({_forbidden})"
assert "eligible_union_wgs84" in _RESULT and _RESULT["eligible_union_wgs84"]["type"] in (
    "Polygon",
    "MultiPolygon",
), "the eligible union must ship as GeoJSON for the wire"
print(
    f"6. WIRE: 5 distinct stable type identifiers {tuple(_types)}, each with a separate display label "
    f"stating its test ({_label_by_type['slope']!r}, {_label_by_type['setback']!r}); labels track the run's "
    f"real thresholds; cell_size_meters ships both dimensions {_cell_size}; the setback caveat flag and "
    f"reason code are carried and agree with narrative_data; no off-parcel geometry is shipped."
)


# ===========================================================================
# 7. THE MEASUREMENT PASS -- WHAT IT MAY AND MAY NOT ASSERT
# ===========================================================================
#
# The diagnostic reports both options and recommends neither. What IS asserted
# is the bounded-error property Option 2 would be chosen for, because that is a
# guarantee rather than a preference -- and the check below is what establishes
# that the guarantee is TRUE OF ONE QUANTITY AND FALSE OF ANOTHER.

_name_a, _rows_a = diag.measure_fixture(diag.rolling_fixture)
_name_b, _rows_b = diag.measure_fixture(diag.ridge_fixture)
assert _rows_a and _rows_b, "the measurement pass must produce rows for both fixtures"

_TOLERANCE_M = 1e-6

for _fixture_name, _rows in ((_name_a, _rows_a), (_name_b, _rows_b)):
    _baseline = _rows[0]
    assert _baseline["one_cell_segments_before"] > 0, (
        f"{_fixture_name}: fixture sanity -- the raw union must actually BE a cell staircase"
    )
    for _row in _rows:
        if not _row["label"].startswith("opt2:"):
            continue
        _radius_m = float(_row["label"].split("(")[1].split(" m")[0])

        # THE GUARANTEE THAT HOLDS. Every square metre an opening removes lies
        # within r of ground that was never eligible: a point survives iff some
        # radius-r disc containing it fits inside the shape, so a removed point
        # cannot have a full radius-r disc around it inside the shape either.
        assert _row["removed_ground_reach_m"] <= _radius_m + _TOLERANCE_M, (
            f"{_fixture_name}: an opening's removed ground must stay within r={_radius_m} m of "
            f"ineligible ground, got {_row['removed_ground_reach_m']:.3f} m"
        )
        # The result is anti-extensive, by construction.
        assert _row["area_ratio"] <= 1.0 + 1e-9, f"{_fixture_name}: an opening cannot grow the union"

# THE GUARANTEE THAT DOES NOT HOLD, pinned as an assertion so it cannot quietly
# start being believed again. "Maximum inward excursion equals r" is FALSE for
# an opening measured against the true boundary: a protrusion too thin to hold
# a radius-r disc is deleted in full, so the distance from its tip to the
# result is the protrusion's whole length, not r.
_ridge_r1 = [r for r in _rows_b if r["label"].startswith("opt2: opening r=1")][0]
assert _ridge_r1["max_inward_excursion_m"] > CELL * 2, (
    "FINDING NO LONGER HOLDS: an opening's boundary excursion is now within r on the finger fixture. "
    "The measurement pass was written because it is NOT -- re-measure and revisit."
)

# Option 1's simplify half, by contrast, IS bounded by its own tolerance:
# Douglas-Peucker never moves the ring further than the tolerance it was given.
for _fixture_name, _rows in ((_name_a, _rows_a), (_name_b, _rows_b)):
    _simplify_row = [r for r in _rows if r["label"].startswith("opt1: simplify only")][0]
    _tolerance = diag.SIMPLIFY_TOLERANCE_CELLS * CELL
    assert _simplify_row["max_inward_excursion_m"] <= _tolerance + 1e-6, (
        f"{_fixture_name}: Douglas-Peucker must not move the ring further than its own tolerance, got "
        f"{_simplify_row['max_inward_excursion_m']:.3f} m against {_tolerance} m"
    )

print(
    f"7. MEASUREMENT PASS: runs on both fixtures and asserts nothing about which option is better.\n"
    f"   Option 2's BOUNDED guarantee HOLDS: removed ground stays within r of ineligible ground on every\n"
    f"   radius on both fixtures (max observed "
    f"{max(r['removed_ground_reach_m'] for r in _rows_a + _rows_b if r['label'].startswith('opt2:')):.2f} m\n"
    f"   against radii of 5/10/15 m).\n"
    f"   Option 2's guarantee AS ORIGINALLY STATED -- 'maximum inward excursion equals r' -- DOES NOT HOLD:\n"
    f"   on the finger fixture at r=5 m the true boundary moves {_ridge_r1['max_inward_excursion_m']:.2f} m,\n"
    f"   because an opening deletes any protrusion too thin to hold a radius-r disc, however long it is.\n"
    f"   Option 1's simplify half IS bounded by its tolerance (5.00 m against a 5 m tolerance, both fixtures)."
)


print("\nAll eligible union checks passed.")
