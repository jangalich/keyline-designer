"""
test_fence_display_geometry.py

THE DISPLAY-ONLY FENCE LINE: the two passes render_layout_map.py has always
run over a fence ring before drawing it -- an angular simplify of every ring
and a SYMMETRIC coincidence trim of the zone rings -- moved into one module,
fence_display_geometry.py, and shipped on the wire from that same function
object, so the interactive map and the PDF draw one line. Run as:

    python test_fence_display_geometry.py

THE FIXTURE IS test_fencing_step.py, IMPORTED. Its module-level assertions
run first (~40 s) and its Harness / Session are reused, so every number below
is taken on a session that file has just proved sound -- the same arrangement
test_display_outline.py makes with test_trees_step.py. Its output is captured
and released only if it fails.

Sections (the branch's numbered tests in brackets):
  1  [1]  WHO CARRIES IT: every fence feature carries the field; no
          production, water, road, tree or structure feature does.
  2  [2]  ONE IMPLEMENTATION: render_layout_map.fence_display_lines IS
          fence_display_geometry.fence_display_lines, the wire calls the same
          object, and the renderer holds no simplify, no union and no trim of
          its own.
  3  [3]  BYTE-IDENTICAL: the layout map's PNG, and every geometry it hands
          its fence-drawing helper, are identical to the inline code the
          shared function replaced -- asserted against a LITERAL
          TRANSCRIPTION of that code, not a second call to the code under
          test. And the wire's lines are that transcription's output too.
  4  [4]  THE TRIM IS SYMMETRIC: where two zone rings run close BOTH lose the
          shared stretch, with a real gap between them; the boundary ring is
          never trimmed; a lone ring keeps its whole loop.
  5  [5]  LENGTHS ARE UNCHANGED AND READ FROM THE REAL GEOMETRY: every
          length_ft is the real UTM ring's; the display line's length differs
          where the trim bit; no length anywhere reads the display field, and
          the rehydrated commit never sees it.
  6  [6]  GENERATE TIMING, before and after, measured in one process.
  7  [7]  Regression is the other test files, run separately.
"""

import hashlib
import io
import os
import re
import sys
import tempfile
import time
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch as mock_patch

_captured = io.StringIO()
try:
    with redirect_stdout(_captured):
        import test_fencing_step as fixture
except BaseException:
    sys.stdout.write(_captured.getvalue())
    raise

import numpy as np
from rasterio.warp import transform_geom
from shapely.geometry import LineString, MultiLineString, Polygon, mapping, shape
from shapely.ops import unary_union

import fence_display_geometry
import fencing
import render_layout_map as rlm
import step_orchestrator
import wire_translation
from fence_display_geometry import (
    DISPLAY_CRS,
    DISPLAY_ONLY_FENCE_LINE_PROPERTY,
    FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M,
    ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M,
)
from fencing import boundary_fencing_to_geojson, tree_zone_fencing_to_geojson, water_zone_fencing_to_geojson
from raster_grid import angular_simplify_closed_ring

FIELD = DISPLAY_ONLY_FENCE_LINE_PROPERTY
ZONE_TYPES = ("water_zone_exclusion", "tree_zone_exclusion")
FEET = 0.3048


def _rings(line):
    return list(line.geoms) if line.geom_type == "MultiLineString" else [line]


def _to_display(geometry_wgs84):
    return shape(transform_geom("EPSG:4326", DISPLAY_CRS, geometry_wgs84))


def _utm(geometry_wgs84):
    return shape(transform_geom("EPSG:4326", fixture.CRS, geometry_wgs84))


# --- one session, all six steps ----------------------------------------
#
# The five upstream commits, then the fencing generate -- every step's payload
# kept, so section 1 can ask all of them what their features carry.

with fixture.Harness() as _harness:
    SESSION = fixture.Session()
    SESSION.upstream()
    _t0 = time.perf_counter()
    FENCING_PAYLOAD = SESSION.fencing()
    GENERATE_SECONDS_AFTER = time.perf_counter() - _t0
    RESULT = SESSION.result("fencing")
    # A step's layers exist only while it is generated; every upstream step is
    # committed here, so their features are read off the DOCUMENT -- the same
    # features the generate shipped, as committed by the fixture.
    STORED_STEPS = SESSION.stored()["steps"]

FENCE_FEATURES = FENCING_PAYLOAD["fence_lines"]["features"]
assert FENCE_FEATURES, "the fixture must produce fence lines, or every assertion below is vacuous"
BLOCKS = {block["fence_type"]: block for block in FENCING_PAYLOAD["fence_types"]}
assert FENCING_PAYLOAD["candidate_fence_types"] == ["boundary", "water_zone_exclusion", "tree_zone_exclusion"], (
    "the fixture must produce all three candidate types, or the trim has nothing to trim"
)

OTHER_FEATURES = {
    step: STORED_STEPS[step]["features"]["features"]
    for step in ("landform", "water", "roads", "trees", "structures")
}
assert all(OTHER_FEATURES.values()), {k: len(v) for k, v in OTHER_FEATURES.items()}


# --- 1 [test 1]. WHO CARRIES IT ----------------------------------------

for feature in FENCE_FEATURES:
    assert FIELD in feature["properties"], f"{feature['id']} carries no display line"
    line = feature["properties"][FIELD]
    assert line is None or line["type"] in ("LineString", "MultiLineString"), (feature["id"], line)
_drawn = [f for f in FENCE_FEATURES if f["properties"][FIELD] is not None]
assert _drawn, "at least one fence feature must have a drawable display line"
for step, features in OTHER_FEATURES.items():
    for feature in features:
        assert FIELD not in feature["properties"], f"{step}: {feature['id']} carries a fence display line"
        # THE CONTROL: these features carry plenty of properties, so "no such
        # key" is a statement about this key rather than an empty dict.
        assert len(feature["properties"]) > 3, (step, feature["id"])
# AND THE NARRATIVE BLOCK DOES NOT CARRY IT EITHER: it is a feature property
# and nothing else, so no digest can quote it.
assert FIELD not in repr(FENCING_PAYLOAD["fence_types"]) and FIELD not in repr(FENCING_PAYLOAD["summary"])

print(
    f"1 [test 1]. WHO CARRIES IT: all {len(FENCE_FEATURES)} fence feature(s) carry '{FIELD}' "
    f"({len(_drawn)} as a LineString/MultiLineString, {len(FENCE_FEATURES) - len(_drawn)} as None -- trimmed "
    f"away entirely); "
    + ", ".join(f"{len(v)} {k}" for k, v in OTHER_FEATURES.items())
    + " feature(s) carry no such key. The narrative block and summary do not quote it."
)


# --- 2 [test 2]. ONE IMPLEMENTATION -------------------------------------

# (a) THE SAME FUNCTION OBJECT, on both callers. Not two functions that agree
#     today.
assert rlm.fence_display_lines is fence_display_geometry.fence_display_lines
assert wire_translation.display_only_fence_lines_wgs84 is fence_display_geometry.display_only_fence_lines_wgs84
# AND THE WIRE WRAPPER CALLS THAT SAME OBJECT: patch it, run the wire path,
# and the patch is what ran.
_calls = []
_real = fence_display_geometry.fence_display_lines


def _recording(boundary_rings, zone_rings, **kwargs):
    _calls.append((len(boundary_rings), len(zone_rings)))
    return _real(boundary_rings, zone_rings, **kwargs)


with mock_patch.object(fence_display_geometry, "fence_display_lines", _recording):
    wire_translation.fence_lines_to_feature_collection(RESULT)
assert len(_calls) == 1, "the wire runs both passes ONCE for the whole collection (the trim is mutual)"
assert _calls[0] == (BLOCKS["boundary"]["feature_count"], len(FENCE_FEATURES) - BLOCKS["boundary"]["feature_count"])

# (b) THE RENDERER HOLDS NO PASS OF ITS OWN. A source read rather than an
#     import check: an import it does not use would pass (a) while a second
#     inline simplify or trim sat below it.
_renderer_source = open("render_layout_map.py").read()
_code_lines = [line for line in _renderer_source.splitlines() if not line.lstrip().startswith("#")]
for forbidden in ("angular_simplify_closed_ring(", "unary_union(", ".buffer(ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M"):
    hits = [line for line in _code_lines if forbidden in line and not line.lstrip().startswith(('"""', "*", "("))]
    # The module docstring mentions the simplifier by name in prose; a CALL is
    # what is forbidden, and a docstring line naming "angular_simplify_closed_
    # ring() --" is prose, so require the opening paren to be followed by an
    # argument rather than a closing paren or a dash.
    hits = [line for line in hits if not re.search(re.escape(forbidden) + r"\)", line)]
    assert not hits, (forbidden, hits)
# AND THE TWO TOLERANCES ARE ONE NUMBER EACH: the renderer's names are the
# module's values, re-exported, not a second declaration.
assert rlm.FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M == FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M == 6.0
assert rlm.ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M == ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M == 5.0
assert not re.search(r"^FENCE_RENDER_ANGULAR_SIMPLIFY_TOLERANCE_M\s*=", _renderer_source, re.M)
assert not re.search(r"^ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M\s*=", _renderer_source, re.M)

print(
    "2 [test 2]. ONE IMPLEMENTATION: render_layout_map.fence_display_lines IS "
    "fence_display_geometry.fence_display_lines; wire_translation.display_only_fence_lines_wgs84 is the "
    f"module's own and ran the shared function exactly once over ({_calls[0][0]} boundary + {_calls[0][1]} zone) "
    "rings; render_layout_map.py holds no simplify(), union or buffer-trim call of its own and declares "
    "neither tolerance (6.0 m / 5.0 m are read from the module)."
)


# --- 3 [test 3]. BYTE-IDENTICAL ----------------------------------------
#
# A LITERAL TRANSCRIPTION of the inline code render_layout_map.py evaluated
# before the shared function existed -- its two passes, written out here
# rather than called, so this section is an independent statement of what
# the PDF drew rather than a second call to the code under test:
#
#     boundary_fence_render_rings = [angular_simplify_closed_ring(g, 6.0) for g in boundary]
#     zone_fence_render_rings = [angular_simplify_closed_ring(g, 6.0) for g in zones]
#     for index, render_ring in enumerate(zone_fence_render_rings):
#         other_rings = boundary_fence_render_rings + [r for i, r in enumerate(zone_fence_render_rings) if i != index]
#         if other_rings:
#             trimmed_ring = render_ring.difference(unary_union(other_rings).buffer(5.0))
#         else:
#             trimmed_ring = render_ring


#
# ONE EXTENSION, STATED: the inline code called angular_simplify_closed_ring()
# on the feature's whole geometry, and that RAISES on a MultiLineString (the
# simplifier re-closes a ring by its coords, which a multi-part geometry does
# not have) -- so the PDF could not have drawn a fence around a severed zone,
# or around the union of several committed water zones, at all. The real
# parcel below has exactly such a fence. The transcription simplifies a
# multi-part ring PART BY PART, which is the one behaviour the shared function
# adds; on every single-ring input (the whole PNG fixture) it is the inline
# expression verbatim.


def _simplify_literal(geometry, tolerance):
    if geometry.geom_type == "MultiLineString":
        return MultiLineString([angular_simplify_closed_ring(part, tolerance) for part in geometry.geoms])
    return angular_simplify_closed_ring(geometry, tolerance)


def _transcription(boundary_rings, zone_rings, simplify_tolerance=6.0, coincidence_tolerance=5.0):
    boundary_fence_render_rings = [_simplify_literal(g, simplify_tolerance) for g in boundary_rings]
    zone_fence_render_rings = [_simplify_literal(g, simplify_tolerance) for g in zone_rings]
    trimmed = []
    for index, render_ring in enumerate(zone_fence_render_rings):
        other_rings = boundary_fence_render_rings + [
            ring for other_index, ring in enumerate(zone_fence_render_rings) if other_index != index
        ]
        if other_rings:
            trimmed.append(render_ring.difference(unary_union(other_rings).buffer(coincidence_tolerance)))
        else:
            trimmed.append(render_ring)
    return boundary_fence_render_rings, trimmed


# THE FIXTURE: test_render_layout_map.py's own trim fixture, a 600 m square
# parcel with a boundary ring, a water ring, two adjacent tree rings, a lone
# tree ring and one beside the boundary -- every pairing the trim has a case
# for. Rendered through the real render_layout_map(), twice.
_CRS = "EPSG:32617"
_OX, _OY, _SIZE = 500000.0, 4500000.0, 600.0


def _square(x0, y0, x1, y1):
    ax, ay = _OX + x0, _OY - _SIZE + y0
    bx, by = _OX + x1, _OY - _SIZE + y1
    return LineString([(ax, ay), (bx, ay), (bx, by), (ax, by), (ax, ay)])


def _wgs(geometry_utm):
    return shape(transform_geom(_CRS, "EPSG:4326", mapping(geometry_utm)))


_boundary_utm = _square(2, 2, _SIZE - 2, _SIZE - 2)
_tree1_utm, _tree2_utm = _square(100, 100, 200, 200), _square(203, 100, 303, 200)
_water_utm, _tree3_utm, _tree4_utm = _square(203, 203, 303, 303), _square(420, 420, 520, 520), _square(5, 400, 105, 500)
_fixture_coordinates = list(_wgs(Polygon(_boundary_utm.coords)).exterior.coords)
_fixture_geojson = {
    "type": "FeatureCollection",
    "features": (
        boundary_fencing_to_geojson([_wgs(_boundary_utm)])["features"]
        + water_zone_fencing_to_geojson(_wgs(_water_utm))["features"]
        + tree_zone_fencing_to_geojson([_wgs(_tree1_utm), _wgs(_tree2_utm), _wgs(_tree3_utm), _wgs(_tree4_utm)])["features"]
    ),
}
_fixture_layers = {
    "dem": {"array": np.zeros((10, 10), dtype=np.float32), "resolution_meters": (5.0, 5.0),
            "origin_x": _OX, "origin_y": _OY, "crs": _CRS},
    "production_areas": [], "water_zone": None, "road_corridor": [], "tree_zone_result": None,
    "structure_site": None, "water_features": {"streams": []}, "contour_lines": [],
    "fencing_result": {"fencing_geojson": _fixture_geojson, "segment_count": 1},
}


def _render(display_function=None):
    """One full render; returns (png bytes, [(wkb, zorder) handed to the fence helper])."""
    drawn = []
    real_draw = rlm._draw_boundary_fence

    def recording_draw(ax, ring, zorder=rlm.FENCE_ZORDER):
        drawn.append((ring.wkb, zorder))
        return real_draw(ax, ring, zorder=zorder)

    with ExitStack() as stack:
        stack.enter_context(mock_patch.object(rlm, "_draw_boundary_fence", recording_draw))
        if display_function is not None:
            stack.enter_context(mock_patch.object(rlm, "fence_display_lines", display_function))
        tmpdir = stack.enter_context(tempfile.TemporaryDirectory())
        path = os.path.join(tmpdir, "layout_map.png")
        rlm.render_layout_map(_fixture_coordinates, path, layers=_fixture_layers)
        png = open(path, "rb").read()
    return png, drawn


_png_shared, _drawn_shared = _render()
_png_inline, _drawn_inline = _render(display_function=_transcription)
assert _png_shared == _png_inline, "the layout map PNG differs from the inline code's"
assert _drawn_shared == _drawn_inline and len(_drawn_shared) >= 6, "a drawn fence geometry differs (WKB)"
# DETERMINISM IS WHAT MAKES THE ABOVE A MEASUREMENT: rendering twice with the
# same function yields the same bytes, so equal bytes above means equal
# input geometry rather than a lucky raster.
_png_again, _ = _render()
assert _png_again == _png_shared

# AND THE WIRE CARRIES THE TRANSCRIPTION'S OUTPUT, on the REAL parcel: each
# feature's display line is the transcription evaluated on the reprojected
# real rings, through the payload's own documented reprojection and rounding
# and nothing else.
_real_boundary = [f for f in FENCE_FEATURES if f["properties"]["fence_type"] == "boundary"]
_real_zones = [f for f in FENCE_FEATURES if f["properties"]["fence_type"] in ZONE_TYPES]
assert [f["id"] for f in FENCE_FEATURES] == [f["id"] for f in _real_boundary + _real_zones], "collection order"
_exp_boundary, _exp_zones = _transcription(
    [_to_display(f["geometry"]) for f in _real_boundary], [_to_display(f["geometry"]) for f in _real_zones]
)
from production_zone_payload import _round_geometry  # noqa: E402

_wire_identical = 0
for feature, expected in zip(_real_boundary + _real_zones, _exp_boundary + _exp_zones):
    parts = list(fence_display_geometry._line_parts(expected))
    if not parts:
        assert feature["properties"][FIELD] is None, feature["id"]
        continue
    expected_line = parts[0] if len(parts) == 1 else MultiLineString(parts)
    expected_wire = _round_geometry(transform_geom(DISPLAY_CRS, "EPSG:4326", mapping(expected_line)))
    assert feature["properties"][FIELD] == expected_wire, feature["id"]
    _wire_identical += 1

print(
    f"3 [test 3]. BYTE-IDENTICAL: the layout map PNG ({len(_png_shared)} bytes, sha256 "
    f"{hashlib.sha256(_png_shared).hexdigest()[:16]}...) rendered through the shared function is byte-for-byte "
    f"the PNG rendered through a literal transcription of the inline code it replaced, and all "
    f"{len(_drawn_shared)} fence geometries handed to the drawing helper are WKB-identical; on the real parcel, "
    f"{_wire_identical} of {len(FENCE_FEATURES)} wire display lines equal that transcription's output "
    f"(reprojected to WGS84 and rounded, nothing else) and the rest are None where the transcription was empty."
)


# --- 4 [test 4]. THE TRIM IS SYMMETRIC ------------------------------------
#
# Asked of the WIRE on the fixture: fence_lines_to_feature_collection() over
# a result whose rings are the square fixture's, read back in the display CRS.

_fixture_result = {
    "fencing_geojson": _fixture_geojson,
    "segment_count": 1,
    "narrative_data": {"fence_types": []},
}
_fixture_wire = {
    f["id"]: f for f in wire_translation.fence_lines_to_feature_collection(_fixture_result)["features"]
}
_by_type = {}
for feature in _fixture_wire.values():
    _by_type.setdefault(feature["properties"]["fence_type"], []).append(feature)
_water_wire = _by_type["water_zone_exclusion"][0]
_trees_wire = _by_type["tree_zone_exclusion"]  # tree1, tree2, tree3, tree4 in order
_boundary_wire = _by_type["boundary"][0]


def _display_length(feature):
    line = feature["properties"][FIELD]
    return 0.0 if line is None else _to_display(line).length


def _display_geom(feature):
    return _to_display(feature["properties"][FIELD])


def _simplified_length(ring_utm):
    return angular_simplify_closed_ring(_to_display(mapping(_wgs(ring_utm))), 6.0).length


_kept = {
    "tree1": _display_length(_trees_wire[0]) / _simplified_length(_tree1_utm),
    "tree2": _display_length(_trees_wire[1]) / _simplified_length(_tree2_utm),
    "water": _display_length(_water_wire) / _simplified_length(_water_utm),
    "tree3": _display_length(_trees_wire[2]) / _simplified_length(_tree3_utm),
    "tree4": _display_length(_trees_wire[3]) / _simplified_length(_tree4_utm),
}
# BOTH SIDES OF EACH ADJACENT PAIR LOSE THEIR SHARED STRETCH -- symmetric,
# not one keeping it -- and only that stretch.
for name in ("tree1", "tree2", "water"):
    assert 0.3 < _kept[name] < 0.95, (name, _kept[name])
assert _kept["tree2"] < _kept["tree1"], "tree2 borders two neighbours and loses more than tree1, which borders one"
for a, b in ((_trees_wire[0], _trees_wire[1]), (_trees_wire[1], _water_wire)):
    assert not _display_geom(a).intersects(_display_geom(b))
    assert _display_geom(a).distance(_display_geom(b)) > ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M, "a real gap"
# A LONE RING KEEPS ITS WHOLE LOOP, closed.
# (Within 1e-3: the wire line went out to WGS84 at six decimals and back -- measured at 1.5e-4 on a 528 m ring.)
assert abs(_kept["tree3"] - 1.0) < 1e-3 and _display_geom(_trees_wire[2]).is_closed
# A RING BESIDE THE BOUNDARY IS TRIMMED BY IT...
assert _kept["tree4"] < 0.95
assert _display_geom(_trees_wire[3]).distance(_display_geom(_boundary_wire)) >= 5.0 - 1e-6
# ...AND THE BOUNDARY RING IS NEVER TRIMMED: its display line is exactly its
# simplified self, closed and whole.
assert abs(_display_length(_boundary_wire) / _simplified_length(_boundary_utm) - 1.0) < 1e-3
assert _display_geom(_boundary_wire).is_closed
# NO ORDER DEPENDENCE: the zones fed in reverse draw the same union of lines.
_reversed_geojson = {
    "type": "FeatureCollection",
    "features": [f for f in _fixture_geojson["features"] if f["properties"]["fence_type"] == "boundary"]
    + list(reversed([f for f in _fixture_geojson["features"] if f["properties"]["fence_type"] != "boundary"])),
}
_reversed_wire = wire_translation.fence_lines_to_feature_collection({**_fixture_result, "fencing_geojson": _reversed_geojson})
_forward_union = unary_union([_display_geom(f) for f in list(_trees_wire) + [_water_wire]])
_reversed_union = unary_union(
    [_display_geom(f) for f in _reversed_wire["features"] if f["properties"]["fence_type"] != "boundary"]
)
assert _forward_union.symmetric_difference(_reversed_union).length < 1e-6

# AND ON THE REAL PARCEL, at least one zone ring was actually bitten.


def _simplified_real_length(feature):
    return sum(angular_simplify_closed_ring(r, 6.0).length for r in _rings(_to_display(feature["geometry"])))


_real_bitten = [
    f["id"] for f in _real_zones
    if _display_length(f) < 0.99 * _simplified_real_length(f)
]
assert _real_bitten, "on the real parcel some zone ring must share a stretch with another ring"

print(
    "4 [test 4]. SYMMETRIC: on the fixture, tree1 keeps "
    f"{_kept['tree1']:.0%}, tree2 (two neighbours) {_kept['tree2']:.0%}, water {_kept['water']:.0%} of its "
    f"simplified ring -- BOTH sides of each adjacent pair lose the shared stretch, and the drawn pieces sit more "
    f"than {ZONE_FENCE_BOUNDARY_COINCIDENCE_TOLERANCE_M:.0f} m apart; the lone tree3 keeps 100% as one closed loop; "
    f"tree4 beside the boundary keeps {_kept['tree4']:.0%} and clears the boundary line by the tolerance; the "
    f"boundary ring is drawn whole and untrimmed; reversing the zone order draws the same lines. On the real "
    f"parcel {len(_real_bitten)} of {len(_real_zones)} zone ring(s) lost a stretch: {_real_bitten}."
)


# --- 5 [test 5]. LENGTHS ARE UNCHANGED AND READ FROM THE REAL GEOMETRY ----

_disagreements = []
for feature in FENCE_FEATURES:
    real_utm = _utm(feature["geometry"])
    real_ft = sum(r.length for r in _rings(real_utm)) / FEET
    assert abs(real_ft - feature["properties"]["length_ft"]) < 0.5, (feature["id"], real_ft)
    line = feature["properties"][FIELD]
    display_ft = 0.0 if line is None else sum(r.length for r in _rings(_utm(line))) / FEET
    if display_ft < 0.99 * real_ft:
        _disagreements.append((feature["id"], round(real_ft, 1), round(display_ft, 1)))
assert _disagreements, "a trimmed display line and its reported length must legitimately disagree somewhere"
for block in FENCING_PAYLOAD["fence_types"]:
    assert abs(sum(row["length_ft"] for row in block["features"]) - block["total_length_ft"]) < 0.1 * len(block["features"]) + 1e-6

# NO LENGTH READS THE FIELD, by source: neither the property's identifier nor
# the wrapper that computes it is named in any code line of fencing.py or
# step_orchestrator.py (nor is the wire string), and wire_translation.py names
# them on exactly three lines -- the import, the one call, and the stamp.
_IDENTIFIERS = re.compile(r"\bDISPLAY_ONLY_FENCE_LINE_PROPERTY\b|\bdisplay_only_fence_lines_wgs84\b")
for module_name, allowed in (("fencing.py", 0), ("step_orchestrator.py", 0), ("wire_translation.py", 3)):
    code = [l for l in open(module_name).read().splitlines() if not l.lstrip().startswith("#")]
    hits = [l for l in code if _IDENTIFIERS.search(l)]
    assert len(hits) == allowed, (module_name, hits)
    if allowed == 0:
        assert not any('"display_only_fence_line"' in l or "'display_only_fence_line'" in l for l in code), module_name

# AND THE REHYDRATED COMMIT NEVER SEES IT: commit every type, and the internal
# fence dicts carry no display key and a length_meters that is the real ring's.
with fixture.Harness():
    s = fixture.Session()
    s.upstream()
    payload = s.fencing()
    features = payload["fence_lines"]["features"]
    s.commit("fencing", features, {f["id"]: "generated" for f in features})
    committed = s.committed("fencing")
    stored = s.stored()["steps"]["fencing"]["features"]["features"]
assert len(committed) == len(features)
for fence, feature in zip(committed, features):
    assert not any("display" in key for key in fence), sorted(fence)
    assert abs(fence["length_meters"] / FEET - feature["properties"]["length_ft"]) < 0.5
    assert fence["line_utm"].equals_exact(_utm(feature["geometry"]), 1e-6), "the real ring, not the display line"
# The document stores the feature as sent (display field and all); it is
# inert there -- a stored property the rehydrator does not read.
assert all(FIELD in f["properties"] for f in stored)

print(
    f"5 [test 5]. LENGTHS: every length_ft is the real UTM ring's length (within 0.5 ft) and each type's "
    f"total is the sum of its features'; the display line is SHORTER than the reported length on "
    f"{len(_disagreements)} feature(s) -- (id, real ft, display ft): {_disagreements} -- which is the "
    f"legitimate disagreement, not a defect; the property name appears in no code line of fencing.py or "
    f"step_orchestrator.py and only at its import and stamp in wire_translation.py; committing all three types "
    f"rehydrates {len(committed)} fence(s) with no display key, each length_meters off the real ring."
)


# --- 6 [test 6]. GENERATE TIMING, before and after ---------------------------
#
# In one process: the fencing generate with the display pass patched out
# (BEFORE -- fence_lines_to_feature_collection stamping None, which is the
# payload builder as it was, minus one key) against the generate as shipped
# (AFTER). The wire pass alone is timed too, so the delta can be read directly.

with fixture.Harness():
    s = fixture.Session()
    s.upstream()
    with mock_patch.object(wire_translation, "display_only_fence_lines_wgs84", lambda features, zone_fence_types: [None] * len(features)):
        t0 = time.perf_counter()
        s.fencing()
        BEFORE = time.perf_counter() - t0
    t0 = time.perf_counter()
    s.fencing()
    AFTER = time.perf_counter() - t0
    result = s.result("fencing")
    t0 = time.perf_counter()
    for _ in range(5):
        wire_translation.fence_lines_to_feature_collection(result)
    WIRE_PASS = (time.perf_counter() - t0) / 5

print(
    f"6 [test 6]. TIMING: fencing generate BEFORE (display pass patched out) {BEFORE:.3f} s, AFTER "
    f"{AFTER:.3f} s (first generate on the session under test: {GENERATE_SECONDS_AFTER:.3f} s); the wire pass "
    f"alone -- reproject, both passes, reproject back, round -- {WIRE_PASS * 1000:.1f} ms over "
    f"{len(FENCE_FEATURES)} feature(s)."
)

print(
    "7 [test 7]. Regression is the other test files, run separately: test_fencing_step.py (this file's "
    "fixture, already run above), test_render_layout_map.py, test_display_outline.py, test_fencing.py, "
    "test_wire_translation.py, test_step_commit.py."
)
print("\nAll fence display geometry checks passed.")
