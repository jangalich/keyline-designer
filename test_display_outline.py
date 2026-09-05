"""
test_display_outline.py

THE DISPLAY-ONLY SMOOTHED OUTLINE, end to end -- the field production and tree
features now carry so the interactive map stops drawing a 5 m cell staircase,
and the rule that it is a rendering and nothing else.

Run as:

    python test_display_outline.py

THE FIXTURE IS test_trees_step.py's, AND IMPORTING IT RUNS THAT FILE'S SUITE.
That is deliberate on both counts. The questions here are about a whole
session -- production zones AND tree candidates AND water zones AND a road
network, all off one parcel, all through the real generates -- and that session
already exists, built on the real 5614 N Montour Rd boundary and the
bench-and-drainage DEM, with the network mocked and nothing else. Rebuilding it
here would be a second fixture that agrees with the first until the first
changes. Its own assertions running first is a feature: every number below is
taken on a session that file has just proved sound. Its output is captured and
reprinted only if it fails.

Sections (the branch's numbered backend tests in brackets):
  1  [1]  WHO CARRIES IT. Production and tree features carry the outline;
          water and road features do not, and the reason they must not is
          that neither is a cell union.
  2  [2]  ONE IMPLEMENTATION, BYTE-IDENTICAL. The shipped outline is exactly
          what render_layout_map.py computes for the same zone -- asserted
          three ways: the function is the same object, the renderer holds no
          smoothing call of its own, and the geometry is WKB-identical to a
          literal transcription of the expression the renderer used to
          evaluate inline.
  3  [3]  THE REAL GEOMETRY IS UNTOUCHED. polygon_utm and
          render_fill_polygon_utm are WKB-identical across a payload build,
          and the feature's own `geometry` is the unsmoothed opening.
  4  [4]  NOTHING DOWNSTREAM READS IT -- grepped across the backend, and
          MEASURED: every rehydrator returns a field-identical internal dict
          whether the property is present or stripped.
  5  [5]  WHAT IT COSTS, per zone and per generate, and what the largest
          deviation from the real geometry is on the reference parcel.
  5b      THE DEGRADATION CONTRACT at the boundaries -- empty in, non-polygonal
          in, and a clip that keeps nothing, none of which may raise over a
          display field.
  6  [6]  Regression is the other test files, run separately.
"""

import io
import re
import subprocess
import sys
import time
from contextlib import redirect_stdout

_captured = io.StringIO()
try:
    with redirect_stdout(_captured):
        import test_trees_step as fixture
except BaseException:
    sys.stdout.write(_captured.getvalue())
    raise

from shapely.geometry import mapping, shape

import display_outline
import production_zone_payload
import render_layout_map
import step_orchestrator
import wire_translation
from display_outline import DISPLAY_ONLY_OUTLINE_PROPERTY
from raster_grid import SQUARE_METERS_PER_ACRE, angular_smooth_polygon

OUTLINE = DISPLAY_ONLY_OUTLINE_PROPERTY


# --- one session, all four steps ---------------------------------------
#
# The generates run in registry order because trees consumes the other three;
# each payload is kept so section 1 can ask all four what their features carry.

with fixture.Harness() as _harness:
    SESSION = fixture.Session()
    LANDFORM_PAYLOAD = SESSION.generate("landform")
    _zones = LANDFORM_PAYLOAD["suggested_zones"]["features"]
    SESSION.commit("landform", _zones, {f["id"]: "generated" for f in _zones})
    WATER_PAYLOAD = SESSION.generate("water")
    SESSION.commit_water(3)
    ROADS_PAYLOAD = SESSION.generate("roads", {"access_point": list(fixture.ACCESS_A)})
    SESSION.commit_roads()
    TREES_PAYLOAD = SESSION.generate("trees")

    CONTEXT = SESSION.context()
    DEM = CONTEXT.dem
    CELL_M = max(DEM["resolution_meters"])
    PRODUCTION_FEATURES = LANDFORM_PAYLOAD["suggested_zones"]["features"]
    TREE_FEATURES = TREES_PAYLOAD["tree_zones"]["features"]
    WATER_FEATURES = WATER_PAYLOAD["survey_zones"]["features"]
    ROAD_FEATURES = ROADS_PAYLOAD["road_corridors"]["features"]
    PRODUCTION_PATCHES = CONTEXT.step_proposals["landform"]["scored_patches"]
    TREE_PATCHES = CONTEXT.step_proposals["trees"]["patches"]
    EXCLUSION = SESSION.assembled("landform")["exclusion_zones"]

assert PRODUCTION_FEATURES and TREE_FEATURES and WATER_FEATURES and ROAD_FEATURES, (
    "the fixture must produce all four layers, or every assertion below is vacuous"
)


# --- 1 [test 1]. WHO CARRIES THE OUTLINE, AND WHO MUST NOT --------------
#
# Production and tree zones are unions of 5 m DEM cells; their edges ARE pixel
# boundaries and that is the whole defect. A water survey zone is a clipped
# envelope and a road corridor is a LineString -- neither is a cell union,
# neither has a staircase, and smoothing either would move geometry for no
# reason at all. So this is two assertions, not one: the field is present where
# the staircase is, and ABSENT where it is not.

for feature in PRODUCTION_FEATURES:
    assert OUTLINE in feature["properties"], f"{feature['id']} carries no display outline"
    outline = feature["properties"][OUTLINE]
    assert outline is not None and outline["type"] in ("Polygon", "MultiPolygon"), outline

for feature in TREE_FEATURES:
    assert OUTLINE in feature["properties"], f"{feature['id']} carries no display outline"
    outline = feature["properties"][OUTLINE]
    assert outline is not None and outline["type"] in ("Polygon", "MultiPolygon"), outline

for feature in WATER_FEATURES + ROAD_FEATURES:
    assert OUTLINE not in feature["properties"], (
        f"{feature['id']} carries a display outline: water zones are clipped envelopes and "
        f"road corridors are LineStrings -- neither is a cell union and neither may be smoothed"
    )

# THE CONTROL THAT MAKES THE ABSENCE A MEASUREMENT. Water and road features
# carry properties at all, and plenty of them, so "no such key" above is a
# statement about this key rather than about an empty properties dict.
assert all(len(f["properties"]) > 3 for f in WATER_FEATURES + ROAD_FEATURES)

print(
    f"1 [test 1]. WHO CARRIES IT: {len(PRODUCTION_FEATURES)} production and {len(TREE_FEATURES)} "
    f"tree feature(s) carry '{OUTLINE}' as a Polygon/MultiPolygon; "
    f"{len(WATER_FEATURES)} water and {len(ROAD_FEATURES)} road feature(s) carry no such key "
    f"(they average {sum(len(f['properties']) for f in WATER_FEATURES + ROAD_FEATURES) // (len(WATER_FEATURES) + len(ROAD_FEATURES))} "
    f"properties each, so the absence is about this key). Water zones are clipped envelopes and "
    f"roads are LineStrings -- neither is a cell union."
)


# --- 2 [test 2]. ONE IMPLEMENTATION, BYTE-IDENTICAL --------------------
#
# THE CLAIM: the outline on the wire is the geometry render_layout_map.py
# smooths for the PDF, not a second answer that happens to look like it.
# Asserted three ways, because "one implementation" is a claim about the code
# and "byte-identical" is a claim about the output, and neither implies the
# other.

# (a) THE SAME FUNCTION OBJECT. Not two functions that agree today.
assert render_layout_map.smoothed_display_outline is display_outline.smoothed_display_outline
assert step_orchestrator.smoothed_display_outline is display_outline.smoothed_display_outline
assert (
    production_zone_payload.smoothed_display_outline is display_outline.smoothed_display_outline
)

# (b) THE RENDERER HOLDS NO SMOOTHING CALL OF ITS OWN. A source read rather
#     than an import check: an import it does not use would pass the check
#     above while a second inline angular_smooth_polygon() sat below it.
_renderer_source = open("render_layout_map.py").read()
_code_lines = [
    line for line in _renderer_source.splitlines()
    if "angular_smooth_polygon" in line and not line.lstrip().startswith("#")
]
# The only surviving mention is inside the module docstring's own prose.
assert all(
    "(" not in line.split("angular_smooth_polygon")[1][:1] for line in _code_lines
), _code_lines

# (c) BYTE-IDENTICAL OUTPUT, against a LITERAL TRANSCRIPTION of the expression
#     render_layout_map.py used to evaluate inline before the shared helper
#     existed:
#
#         angular_smooth_polygon(
#             patch["render_fill_polygon_utm"],
#             PRODUCTION_FILL_SIMPLIFY_TOLERANCE_CELLS * max(dem["resolution_meters"]),
#             PRODUCTION_FILL_CHAIKIN_ITERATIONS,
#         ).intersection(patch["polygon_utm"])
#
#     Written out here rather than called, so this test is an independent
#     statement of what the PDF draws rather than a second call to the code
#     under test.


def _renderer_expression(patch):
    return angular_smooth_polygon(
        patch["render_fill_polygon_utm"],
        display_outline.DISPLAY_OUTLINE_SIMPLIFY_TOLERANCE_CELLS * CELL_M,
        display_outline.DISPLAY_OUTLINE_CHAIKIN_ITERATIONS,
    ).intersection(patch["polygon_utm"])


def _patch_by_feature_id(patches, feature_id, mint):
    for patch in patches:
        if mint(patch) == feature_id:
            return patch
    raise AssertionError(f"no patch behind {feature_id}")


_production_by_id = {
    f"production-area-{patch['id']}": patch for patch in PRODUCTION_PATCHES
}
_tree_by_rank = {patch["rank"]: patch for patch in TREE_PATCHES}

_utm_identical = 0
for feature in PRODUCTION_FEATURES:
    patch = _production_by_id[feature["id"]]
    expected_utm = _renderer_expression(patch)
    shipped_utm = display_outline.smoothed_display_outline(
        patch["render_fill_polygon_utm"], patch["polygon_utm"], CELL_M
    )
    assert shipped_utm.wkb == expected_utm.wkb, f"{feature['id']}: UTM geometry differs"
    # AND THE WIRE CARRIES THAT GEOMETRY, through this payload's own documented
    # reprojection and 6-dp rounding and nothing else.
    from rasterio.warp import transform_geom

    expected_wire = production_zone_payload._round_geometry(
        transform_geom(EXCLUSION["wire"]["crs"], "EPSG:4326", mapping(expected_utm))
    )
    assert feature["properties"][OUTLINE] == expected_wire, feature["id"]
    _utm_identical += 1

for feature in TREE_FEATURES:
    patch = _tree_by_rank[feature["properties"]["rank"]]
    expected_utm = _renderer_expression(patch)
    shipped_utm = display_outline.smoothed_display_outline(
        patch["render_fill_polygon_utm"], patch["polygon_utm"], CELL_M
    )
    assert shipped_utm.wkb == expected_utm.wkb, f"{feature['id']}: UTM geometry differs"
    from rasterio.warp import transform_geom

    expected_wire = transform_geom(DEM["crs"], "EPSG:4326", mapping(expected_utm))
    assert feature["properties"][OUTLINE] == expected_wire, feature["id"]
    _utm_identical += 1

print(
    f"2 [test 2]. ONE IMPLEMENTATION: render_layout_map.smoothed_display_outline IS "
    f"display_outline.smoothed_display_outline (and so is the one both payload builders call); "
    f"render_layout_map.py holds no angular_smooth_polygon() call of its own; and all "
    f"{_utm_identical} zone outline(s) are WKB-IDENTICAL to a literal transcription of the "
    f"expression that renderer used to evaluate inline -- "
    f"angular_smooth_polygon(render_fill_polygon_utm, {display_outline.DISPLAY_OUTLINE_SIMPLIFY_TOLERANCE_CELLS} "
    f"cell x {CELL_M:.2f} m, {display_outline.DISPLAY_OUTLINE_CHAIKIN_ITERATIONS} Chaikin pass)"
    f".intersection(polygon_utm) -- with the wire carrying it through each payload's own "
    f"documented reprojection and nothing else."
)


# --- 3 [test 3]. THE REAL GEOMETRY IS UNTOUCHED ------------------------
#
# The load-bearing constraint, asserted at the two names that carry it. Both
# payload builders are pure, so the proof is a WKB snapshot either side of a
# rebuild: if smoothing had reached the source or the fill, one of these moves.

_before = {
    patch["id"]: (patch["polygon_utm"].wkb, patch["render_fill_polygon_utm"].wkb)
    for patch in PRODUCTION_PATCHES
}
_tree_before = {
    patch["id"]: (patch["polygon_utm"].wkb, patch["render_fill_polygon_utm"].wkb)
    for patch in TREE_PATCHES
}

_rebuilt_landform = production_zone_payload.assemble_production_zone_payload(
    EXCLUSION, CONTEXT.step_proposals["landform"]
)
_rebuilt_trees = step_orchestrator._with_display_only_outlines(
    CONTEXT.step_proposals["trees"]["zones_geojson"], TREE_PATCHES, DEM
)

for patch in PRODUCTION_PATCHES:
    assert _before[patch["id"]] == (
        patch["polygon_utm"].wkb,
        patch["render_fill_polygon_utm"].wkb,
    ), f"production patch {patch['id']}: the real geometry moved"
for patch in TREE_PATCHES:
    assert _tree_before[patch["id"]] == (
        patch["polygon_utm"].wkb,
        patch["render_fill_polygon_utm"].wkb,
    ), f"tree patch {patch['id']}: the real geometry moved"

# AND THE FEATURE'S OWN `geometry` IS STILL THE UNSMOOTHED SHAPE, which is what
# every consumer of the wire reads. Production's is the opening; a tree's is
# its footprint. Neither equals its own outline -- if they did, this test would
# be asserting that smoothing does nothing.
for feature in PRODUCTION_FEATURES:
    patch = _production_by_id[feature["id"]]
    from rasterio.warp import transform_geom

    assert feature["geometry"] == production_zone_payload._round_geometry(
        transform_geom(
            EXCLUSION["wire"]["crs"], "EPSG:4326", mapping(patch["render_fill_polygon_utm"])
        )
    ), feature["id"]
    assert feature["geometry"] != feature["properties"][OUTLINE], (
        f"{feature['id']}: the outline equals the geometry -- the smooth did nothing"
    )
for feature in TREE_FEATURES:
    patch = _tree_by_rank[feature["properties"]["rank"]]
    assert feature["geometry"] == patch["geometry_wgs84"], feature["id"]
    assert feature["geometry"] != feature["properties"][OUTLINE], feature["id"]

# The rebuilt payloads agree with the shipped ones, which is what says a
# re-read (step_payload) returns the generate's own answer.
assert _rebuilt_landform["suggested_zones"]["features"] == PRODUCTION_FEATURES
assert _rebuilt_trees["features"] == TREE_FEATURES

print(
    f"3 [test 3]. REAL GEOMETRY UNTOUCHED: polygon_utm and render_fill_polygon_utm are "
    f"WKB-identical across a payload rebuild on all {len(PRODUCTION_PATCHES)} production and "
    f"{len(TREE_PATCHES)} tree patch(es); every feature's own `geometry` is still the unsmoothed "
    f"shape (production's opening, a tree's footprint) and differs from its outline on every "
    f"zone; and a rebuild returns the shipped payload byte for byte."
)


# --- 4 [test 4]. NOTHING DOWNSTREAM READS IT ---------------------------
#
# Two proofs, because a grep can miss a computed key and a behavioural test can
# miss a reader on a path this fixture does not walk.

# (a) GREPPED. Every backend file that mentions the property at all, by its
#     literal name or through the one constant that spells it.
_producers = {
    "display_outline.py",          # the rule, and the property's one spelling
    "production_zone_payload.py",  # production's own use
    "step_orchestrator.py",        # trees' own use
}
_mentions = subprocess.run(
    ["grep", "-rl", "-e", OUTLINE, "-e", "DISPLAY_ONLY_OUTLINE_PROPERTY", "--include=*.py", "."],
    capture_output=True, text=True, check=False,
).stdout.split()
_mentions = {name.removeprefix("./") for name in _mentions}
_non_test = {name for name in _mentions if not name.startswith("test_")}
assert _non_test == _producers, (
    f"the display-only outline is mentioned outside its producers: {sorted(_non_test - _producers)}"
)

# (b) MEASURED, WHICH IS THE ONE THAT MATTERS. A feature carrying the property
#     and the same feature with it stripped must rehydrate to the SAME internal
#     dict, field for field and geometry for geometry. That is the whole claim:
#     the commit path, the four consumer modules and everything they feed do
#     not see this field at all.


def _stripped(feature):
    return {
        **feature,
        "properties": {k: v for k, v in feature["properties"].items() if k != OUTLINE},
    }


def _comparable(patch):
    return {
        key: (value.wkb if hasattr(value, "wkb") else value)
        for key, value in patch.items()
    }


_rehydrations = 0
for feature in TREE_FEATURES:
    with_field = wire_translation.rehydrate_tree_zone(feature, DEM)
    without = wire_translation.rehydrate_tree_zone(_stripped(feature), DEM)
    assert _comparable(with_field) == _comparable(without), feature["id"]
    assert OUTLINE not in with_field, "the rehydrator inherited a display field"
    _rehydrations += 1
for feature in PRODUCTION_FEATURES:
    with_field = wire_translation.rehydrate_production_zone(feature, DEM)
    without = wire_translation.rehydrate_production_zone(_stripped(feature), DEM)
    assert _comparable(with_field) == _comparable(without), feature["id"]
    assert OUTLINE not in with_field, "the rehydrator inherited a display field"
    _rehydrations += 1

print(
    f"4 [test 4]. NOTHING READS IT: the property is named in exactly "
    f"{len(_producers)} non-test backend files ({', '.join(sorted(_producers))}) -- the two that "
    f"put it on the wire and the one that owns the rule. And {_rehydrations} rehydration(s) "
    f"return a field-identical internal dict with the property present and with it stripped, so "
    f"no consumer downstream of the wire can be reading it."
)


# --- 5 [test 5]. WHAT IT COSTS, AND HOW FAR IT MOVES THE EDGE ----------
#
# THE COST, isolated: the smoothing pass alone, over the geometry a generate
# already holds. The generate-level before/after is measured outside this file
# (the same fixture, timed with and without the change); what is timed here is
# the thing that was added, per zone, so the per-generate figure can be read
# against the number of zones any parcel produces.

_REPEATS = 20


def _time_outlines(patches):
    start = time.perf_counter()
    for _ in range(_REPEATS):
        for patch in patches:
            display_outline.smoothed_display_outline(
                patch["render_fill_polygon_utm"], patch["polygon_utm"], CELL_M
            )
    return (time.perf_counter() - start) * 1000.0 / _REPEATS


_production_ms = _time_outlines(PRODUCTION_PATCHES)
_tree_ms = _time_outlines(TREE_PATCHES)
_per_production = _production_ms / len(PRODUCTION_PATCHES)
_per_tree = _tree_ms / len(TREE_PATCHES)

# THE DEVIATION. How far the drawn edge moves from the real one, per zone --
# the worst Hausdorff distance (the furthest any point on one boundary is from
# the other, in metres), and the area the outline ADDS and REMOVES against the
# shape it is a rendering of. Read in the DEM's own projected metres, where a
# metre is a metre. Printed in full rather than reduced to one number: the
# deviation is a function of how ragged and how small a zone is, and a single
# worst case says nothing about whether that is one zone or all of them.
_rows = []
for label, patches in (("production", PRODUCTION_PATCHES), ("tree", TREE_PATCHES)):
    for patch in patches:
        real = patch["render_fill_polygon_utm"]
        outline = display_outline.smoothed_display_outline(real, patch["polygon_utm"], CELL_M)
        _rows.append(
            {
                "label": f"{label} {patch['id']}",
                "acres": real.area / SQUARE_METERS_PER_ACRE,
                "hausdorff_m": real.hausdorff_distance(outline),
                "added_m2": outline.difference(real).area,
                "removed_m2": real.difference(outline).area,
                "difference_pct": real.symmetric_difference(outline).area / real.area * 100.0,
            }
        )
_worst = max(_rows, key=lambda row: row["hausdorff_m"])
for row in sorted(_rows, key=lambda row: -row["hausdorff_m"]):
    print(
        f"    deviation  {row['label']:<14} {row['acres']:6.2f} ac   "
        f"Hausdorff {row['hausdorff_m']:5.2f} m   "
        f"added {row['added_m2']:7.1f} m^2  removed {row['removed_m2']:7.1f} m^2  "
        f"= {row['difference_pct']:5.2f}% of the zone"
    )

# THE SMOOTH NEVER CLAIMS GROUND THE GATE EXCLUDED. The re-clip to polygon_utm
# is what makes this hard rather than probable -- see smoothed_display_outline().
for label, patches in (("production", PRODUCTION_PATCHES), ("tree", TREE_PATCHES)):
    for patch in patches:
        outline = display_outline.smoothed_display_outline(
            patch["render_fill_polygon_utm"], patch["polygon_utm"], CELL_M
        )
        assert outline.difference(patch["polygon_utm"]).area < 1e-6, (
            f"{label} {patch['id']}: the outline reaches outside the real footprint"
        )

# THE BOUND, AND WHY IT IS TWO CELLS RATHER THAN ONE.
#
# The transform is a Douglas-Peucker pass at ONE cell followed by ONE Chaikin
# pass. Simplify can move the boundary by up to its own tolerance; Chaikin then
# cuts each surviving corner, and a corner cut on a short segment moves the
# edge again. So one cell is the wrong ceiling -- it is the budget for the
# first of two operations -- and two cells is the honest one.
#
# WHAT IT IS NOT. It is not a claim that the deviation is negligible. On this
# parcel the worst case is a small, ragged zone whose edge moves by more than
# a cell and whose area moves by a tenth, and that number is PRINTED above
# rather than buried: on a half-acre zone the smoothing is a real change to
# apparent extent, and it grows quieter the larger and straighter the zone is.
# What makes it acceptable is that it is the SAME smoothing the printed map has
# always applied to the same zones -- the interactive map was the one
# disagreeing -- and that no consumer computes from it. If it ever needs to be
# gentler, DISPLAY_OUTLINE_SIMPLIFY_TOLERANCE_CELLS is the one number to turn.
DEVIATION_BOUND_CELLS = 2.0
assert _worst["hausdorff_m"] < DEVIATION_BOUND_CELLS * CELL_M, (_worst, CELL_M)

print(
    f"5 [test 5]. COST AND DEVIATION: the smoothing pass costs "
    f"{_per_production:.2f} ms per production zone and {_per_tree:.2f} ms per tree candidate "
    f"({_production_ms:.2f} ms for {len(PRODUCTION_PATCHES)} production zones, {_tree_ms:.2f} ms "
    f"for {len(TREE_PATCHES)} tree candidates, mean of {_REPEATS} runs) -- so twelve tree "
    f"candidates would cost about {_per_tree * 12:.1f} ms. LARGEST DEVIATION on this parcel: "
    f"{_worst['label']} ({_worst['acres']:.2f} ac), Hausdorff {_worst['hausdorff_m']:.2f} m "
    f"against a {DEVIATION_BOUND_CELLS:.0f}-cell ({DEVIATION_BOUND_CELLS * CELL_M:.0f} m) bound "
    f"-- symmetric difference {_worst['difference_pct']:.2f}% of that zone, "
    f"{_worst['added_m2']:.0f} m^2 added and {_worst['removed_m2']:.0f} m^2 removed. The median "
    f"zone moves {sorted(row['hausdorff_m'] for row in _rows)[len(_rows) // 2]:.2f} m. No outline "
    f"reaches outside its own polygon_utm."
)


# --- 5b. THE DEGRADATION CONTRACT, at the boundaries -------------------
#
# smoothed_display_outline() sits in front of a function documented to DEGRADE
# RATHER THAN RAISE, and it adds a polygonal-parts guard of its own. Both are
# asserted here rather than assumed, because the payload builders treat an
# empty result as "no outline" and anything that raised instead would fail a
# generate over a display field.

from shapely.geometry import GeometryCollection, LineString, Point, Polygon, box

_unit = box(0.0, 0.0, 40.0, 40.0)

# An EMPTY input comes back empty -- not an exception, and not a shape.
assert display_outline.smoothed_display_outline(Polygon(), Polygon(), 5.0).is_empty

# A NON-POLYGONAL input is returned by angular_smooth_polygon() unchanged, and
# the clip of a line against a disjoint box is empty -- still no raise.
_line = LineString([(0.0, 0.0), (10.0, 10.0)])
assert display_outline.smoothed_display_outline(_line, box(100.0, 100.0, 140.0, 140.0), 5.0).is_empty

# A CLIP THAT KEEPS NOTHING is empty rather than an error.
assert display_outline.smoothed_display_outline(_unit, box(500.0, 500.0, 540.0, 540.0), 5.0).is_empty

# THE POLYGONAL-PARTS GUARD, exercised directly: a collection loses its line
# and its point and keeps its polygon; a Polygon passes through AS ITSELF,
# which is what makes the guard invisible in the common case.
_collection = GeometryCollection([_unit, _line, Point(1.0, 1.0)])
assert display_outline._polygonal_parts(_collection).equals(_unit)
assert display_outline._polygonal_parts(_unit) is _unit
assert display_outline._polygonal_parts(_line).is_empty

# AND THE COMMON CASE IS UNGUARDED IN EFFECT: for a real zone the guard returns
# the intersection object itself, so the shared function's output is what an
# inline `.intersection()` would have produced, byte for byte.
_patch = PRODUCTION_PATCHES[0]
_raw = _renderer_expression(_patch)
assert display_outline._polygonal_parts(_raw) is _raw

print(
    "5b. DEGRADATION: an empty input, a non-polygonal input and a clip that keeps nothing all "
    "return empty rather than raising; the polygonal-parts guard drops a collection's line and "
    "point, and returns a real zone's intersection object ITSELF -- so the guard is invisible "
    "wherever it is not needed."
)


# --- 6 [test 6]. REGRESSION -------------------------------------------

print(
    "\n6 [test 6]. REGRESSION: run the other test files separately -- test_trees_step.py "
    "(run above, as this file's fixture), test_production_fill_smoothing.py, "
    "test_production_zone_payload.py, test_step_orchestrator.py, test_step_commit.py, "
    "test_wire_translation.py, test_wire_translation_inbound.py, test_water_step.py, "
    "test_roads_step.py, test_render_layout_map.py."
)

print("\nAll display outline checks passed.")
