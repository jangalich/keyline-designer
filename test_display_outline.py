"""
test_display_outline.py

THE DISPLAY-ONLY SMOOTHED OUTLINE, end to end -- the field PRODUCTION features
carry so the interactive map stops drawing a 5 m cell staircase, and the rule
that it is a rendering and nothing else.

TREE FEATURES NO LONGER CARRY IT, and section 1 asserts the absence beside
water's and roads'. A tree zone IS a cell union, so it is the one layer where
the staircase argument applied and the answer is still no: render_layout_map.py
draws the tree hatch from the cell-union footprint verbatim, so smoothing a
tree feature made the two maps disagree rather than agree -- and the smooth is
anti-extensive, measured here at 19.56% of a 0.32 ac candidate with 255.7 m^2
removed and NOTHING added, which is the thin-arm deletion the tree layer
refuses a morphological opening in order to prevent. The measurement itself
lives in test_tree_zone_geometry_validity.py, where it is the justification for
the removal; what is asserted here is that no tree feature carries the field.

Run as:

    python test_display_outline.py

THE FIXTURE IS test_trees_step.py's, IMPORTED FROM trees_step_fixture.py --
its Harness and Session without that file's tests running first (importing
test_trees_step itself cost ~15 s of its suite before this file's first
section; the two now run side by side under run_tests.py). Reusing the
fixture rather than rebuilding it is deliberate. The questions here are about a whole
session -- production zones AND tree candidates AND water zones AND a road
network, all off one parcel, all through the real generates -- and that session
already exists, built on the real 5614 N Montour Rd boundary and the
bench-and-drainage DEM, with the network mocked and nothing else. Rebuilding it
here would be a second fixture that agrees with the first until the first
changes. test_trees_step.py proves that session sound in its own run; every number
below is taken on the same fixture. The fixture's import output is captured and
reprinted only if it fails.

Sections (the branch's numbered backend tests in brackets):
  1  [1]  WHO CARRIES IT. Production features carry the outline; tree,
          water and road features do not -- water and roads because neither
          is a cell union, trees because the layout map does not smooth
          them and the smooth ate their thin arms.
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
          whether the property is present or stripped. A tree feature, which
          no longer carries it, is INJECTED with one so the claim stays a
          measurement rather than a vacuous truth.
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
        import trees_step_fixture as fixture
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
    # Section 3 rebuilds the trees payload; assembling its consumed values
    # needs the harness's mocks, so it happens here rather than below.
    SESSION_ASSEMBLED = SESSION.assembled("trees")

assert PRODUCTION_FEATURES and TREE_FEATURES and WATER_FEATURES and ROAD_FEATURES, (
    "the fixture must produce all four layers, or every assertion below is vacuous"
)


# --- 1 [test 1]. WHO CARRIES THE OUTLINE, AND WHO MUST NOT --------------
#
# A production zone is a union of 5 m DEM cells; its edge IS a pixel boundary
# and that is the whole defect. A water survey zone is a clipped envelope and a
# road corridor is a LineString -- neither is a cell union, neither has a
# staircase, and smoothing either would move geometry for no reason at all.
#
# A TREE ZONE IS A CELL UNION AND STILL MUST NOT CARRY IT, which is why it is
# asserted here beside water and roads rather than beside production. The
# staircase is real on a tree candidate; the smooth is wrong anyway, for two
# reasons that are separate and each sufficient. render_layout_map.py does not
# smooth tree zones -- it draws the tree hatch from the cell-union footprint
# verbatim and smooths only the production fill, which its contour clip runs
# against -- so the field made the interactive map disagree with the printed
# one, which is the opposite of what it exists for. And the smooth is
# ANTI-EXTENSIVE: on this same parcel it removed 255.7 m^2 from a 0.32 ac
# candidate and added nothing, 19.56% of the zone, off the thin arms the tree
# layer refuses an opening in order to keep. See display_outline.py and
# test_tree_zone_geometry_validity.py, which measures it.

for feature in PRODUCTION_FEATURES:
    assert OUTLINE in feature["properties"], f"{feature['id']} carries no display outline"
    outline = feature["properties"][OUTLINE]
    assert outline is not None and outline["type"] in ("Polygon", "MultiPolygon"), outline

for feature in TREE_FEATURES:
    assert OUTLINE not in feature["properties"], (
        f"{feature['id']} carries a display outline: the layout map draws tree zones "
        f"unsmoothed, and the smooth is anti-extensive on exactly their thin arms"
    )

for feature in WATER_FEATURES + ROAD_FEATURES:
    assert OUTLINE not in feature["properties"], (
        f"{feature['id']} carries a display outline: water zones are clipped envelopes and "
        f"road corridors are LineStrings -- neither is a cell union and neither may be smoothed"
    )

# THE CONTROL THAT MAKES THE ABSENCE A MEASUREMENT. The features that carry no
# outline carry properties at all, and plenty of them, so "no such key" above is
# a statement about this key rather than about an empty properties dict.
_WITHOUT = TREE_FEATURES + WATER_FEATURES + ROAD_FEATURES
assert all(len(f["properties"]) > 3 for f in _WITHOUT)

print(
    f"1 [test 1]. WHO CARRIES IT: {len(PRODUCTION_FEATURES)} production feature(s) carry "
    f"'{OUTLINE}' as a Polygon/MultiPolygon; {len(TREE_FEATURES)} tree, {len(WATER_FEATURES)} "
    f"water and {len(ROAD_FEATURES)} road feature(s) carry no such key (they average "
    f"{sum(len(f['properties']) for f in _WITHOUT) // len(_WITHOUT)} properties each, so the "
    f"absence is about this key). Water zones are clipped envelopes and roads are LineStrings -- "
    f"neither is a cell union; a tree zone is one, and the layout map draws it unsmoothed anyway."
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
assert (
    production_zone_payload.smoothed_display_outline is display_outline.smoothed_display_outline
)
# AND step_orchestrator IMPORTS IT NO LONGER. The trees payload builder was the
# second producer site; removing the call without removing the import would
# leave a name that reads as a live consumer.
assert not hasattr(step_orchestrator, "smoothed_display_outline"), (
    "step_orchestrator still imports the smoother -- trees does not smooth"
)
assert not hasattr(step_orchestrator, "_with_display_only_outlines"), (
    "step_orchestrator still holds the tree outline builder"
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

print(
    f"2 [test 2]. ONE IMPLEMENTATION: render_layout_map.smoothed_display_outline IS "
    f"display_outline.smoothed_display_outline (and so is the one the payload builder calls, "
    f"the only one left); "
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
# The trees payload builder is rebuilt whole -- there is no outline step left
# to isolate, and rebuilding the builder is the stronger statement anyway: it
# says a re-read returns the generate's own answer, outline or no outline.
_rebuilt_trees = step_orchestrator.build_trees_payload(
    CONTEXT.step_proposals["trees"], SESSION_ASSEMBLED
)["tree_zones"]

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
# A tree feature's geometry is its own footprint, and it is the ONLY geometry
# the feature carries -- there is no second version of it in properties.
for feature in TREE_FEATURES:
    patch = _tree_by_rank[feature["properties"]["rank"]]
    assert feature["geometry"] == patch["geometry_wgs84"], feature["id"]
    assert OUTLINE not in feature["properties"], feature["id"]

# The rebuilt payloads agree with the shipped ones, which is what says a
# re-read (step_payload) returns the generate's own answer.
assert _rebuilt_landform["suggested_zones"]["features"] == PRODUCTION_FEATURES
assert _rebuilt_trees["features"] == TREE_FEATURES

print(
    f"3 [test 3]. REAL GEOMETRY UNTOUCHED: polygon_utm and render_fill_polygon_utm are "
    f"WKB-identical across a payload rebuild on all {len(PRODUCTION_PATCHES)} production and "
    f"{len(TREE_PATCHES)} tree patch(es); every production feature's own `geometry` is still the "
    f"unsmoothed opening and differs from its outline on every zone; every tree feature's is its "
    f"own footprint and carries no outline at all; and a rebuild returns the shipped payload "
    f"byte for byte."
)


# --- 4 [test 4]. NOTHING DOWNSTREAM READS IT ---------------------------
#
# Two proofs, because a grep can miss a computed key and a behavioural test can
# miss a reader on a path this fixture does not walk.

# (a) GREPPED. Every backend file that mentions the property at all, by its
#     literal name or through the one constant that spells it.
_producers = {
    "display_outline.py",          # the rule, and the property's one spelling
    "production_zone_payload.py",  # production's own use -- the ONE producer
    "step_orchestrator.py",        # a docstring naming what trees stopped doing
}
# THE THIRD ONE IS PROSE, NOT CODE, and the difference is asserted rather than
# assumed: step_orchestrator.py names the property only in build_trees_payload()'s
# docstring, explaining why a tree feature no longer carries it. A grep that
# could not tell those apart would go on passing if the call came back.
_orchestrator_lines = [
    line for line in open("step_orchestrator.py").read().splitlines()
    if OUTLINE in line or "DISPLAY_ONLY_OUTLINE_PROPERTY" in line
]
assert _orchestrator_lines and all(
    not line.strip().startswith(("from ", "import ")) and "=" not in line and "(" not in line
    for line in _orchestrator_lines
), _orchestrator_lines
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


# A TREE FEATURE NO LONGER CARRIES THE FIELD, so stripping one proves nothing.
# It is INJECTED instead: a feature with the property added and the same feature
# without it must rehydrate identically. That keeps the claim a measurement --
# the rehydrator ignores this key whether or not anything ever ships it -- and
# it is the assertion that would catch a rehydrator learning to read it back.


def _injected(feature):
    return {
        **feature,
        "properties": {**feature["properties"], OUTLINE: feature["geometry"]},
    }


_rehydrations = 0
for feature in TREE_FEATURES:
    with_field = wire_translation.rehydrate_tree_zone(_injected(feature), DEM)
    without = wire_translation.rehydrate_tree_zone(feature, DEM)
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
    f"{len(_producers)} non-test backend files ({', '.join(sorted(_producers))}) -- the one that "
    f"puts it on the wire, the one that owns the rule, and {len(_orchestrator_lines)} line(s) of "
    f"docstring prose in step_orchestrator.py saying why trees stopped. And {_rehydrations} "
    f"rehydration(s) return a field-identical internal dict with the property present and with it "
    f"absent (injected on a tree feature, which ships none), so no consumer downstream of the "
    f"wire can be reading it."
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
_per_production = _production_ms / len(PRODUCTION_PATCHES)

# THE DEVIATION. How far the drawn edge moves from the real one, per zone --
# the worst Hausdorff distance (the furthest any point on one boundary is from
# the other, in metres), and the area the outline ADDS and REMOVES against the
# shape it is a rendering of. Read in the DEM's own projected metres, where a
# metre is a metre. Printed in full rather than reduced to one number: the
# deviation is a function of how ragged and how small a zone is, and a single
# worst case says nothing about whether that is one zone or all of them.
_rows = []
for label, patches in (("production", PRODUCTION_PATCHES),):
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
for label, patches in (("production", PRODUCTION_PATCHES),):
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
    f"{_per_production:.2f} ms per production zone "
    f"({_production_ms:.2f} ms for {len(PRODUCTION_PATCHES)} production zones, mean of "
    f"{_REPEATS} runs) -- so twelve zones would cost about {_per_production * 12:.1f} ms. "
    f"LARGEST DEVIATION on this parcel: "
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
    "test_roads_step.py, test_render_layout_map.py, test_tree_zone_geometry_validity.py."
)

print("\nAll display outline checks passed.")
