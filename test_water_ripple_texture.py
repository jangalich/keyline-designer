"""
Tests for the water zone RIPPLE-LINE TEXTURE rendering (see render_layout_map.py's
own WATER ZONE STYLE docstring section).

The water zone no longer renders a filled/outlined polygon -- it renders a family
of horizontal sine waves (_ripple_lines_for_polygon()) clipped to the zone's own
render_fill_polygon_utm, with no fill and no perimeter stroke. These tests exercise
the ripple generator directly (geometry only, no matplotlib) plus one offline
end-to-end render pass confirming the water zone's numbered marker and legend text
are unchanged from before this branch.

This file is intentionally SEPARATE from test_render_layout_map.py: that file is a
script-style module with top-level assertions that already fail to collect on main
(a stale production convex-hull assertion at ~line 472, plus a water-zone section
invalidated by the water rebuild) -- putting these tests there would let that
pre-existing collection error mask them.
"""

import math
import os
import tempfile
from collections import defaultdict

import numpy as np
from shapely.geometry import LineString, MultiPolygon, Polygon, box, mapping, shape
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom

import render_layout_map as rlm
from fencing import boundary_fencing_to_geojson, find_boundary_fencing
from render_layout_map import (
    WATER_RIPPLE_AMPLITUDE,
    WATER_RIPPLE_PHASE_STEP,
    WATER_RIPPLE_SAMPLES_PER_WAVELENGTH,
    WATER_RIPPLE_SPACING,
    WATER_RIPPLE_WAVELENGTH,
    WATER_ZONE_COLOR,
    _ripple_lines_for_polygon,
)

S = WATER_RIPPLE_SPACING
WL = WATER_RIPPLE_WAVELENGTH
A = WATER_RIPPLE_AMPLITUDE
P = WATER_RIPPLE_PHASE_STEP
SPP = WATER_RIPPLE_SAMPLES_PER_WAVELENGTH


def _ripples(geom):
    """The generator, called with the module's own tuned constants."""
    return _ripple_lines_for_polygon(geom, S, WL, A, P, SPP)


def _mean_y(line):
    ys = [c[1] for c in line.coords]
    return sum(ys) / len(ys)


def _y_at(line, x0):
    """The y-value where `line` crosses the vertical x=x0 (a single crossing for a
    full-width wave over this fixture)."""
    hit = LineString([(x0, -1e6), (x0, 1e6)]).intersection(line)
    if hit.geom_type == "Point":
        return hit.y
    return list(hit.geoms)[0].y


# =====================================================================
# Test 1: ripples clip to the polygon (a synthetic square).
# =====================================================================
def test_ripples_clip_to_square_and_count_matches_spacing():
    side = 60.0
    square = box(0.0, 0.0, side, side)
    lines = _ripples(square)

    # Every returned line is contained within the square (small buffer to absorb
    # floating-point rounding on shapely's own clip vertices).
    padded = square.buffer(1e-6)
    for line in lines:
        assert padded.contains(line), "a returned ripple line escaped the clipping polygon"

    # The generator lays down one wave-row baseline every `spacing`, padded by
    # `amplitude` on each side of the shape's y-extent. The bottom padding row
    # only grazes the boundary (empty), and for spacing > 2*amplitude every other
    # baseline yields exactly one full-width line for a full-width square. So the
    # count is floor((height + 2*amplitude) / spacing).
    expected = math.floor((side + 2.0 * A) / S)
    assert len(lines) == expected, (
        f"expected {expected} ripple lines over a {side}m square at spacing={S}/amp={A}, "
        f"got {len(lines)}"
    )
    # Guard the tuning intent: a ~0.5-acre zone must read as several distinct
    # waves, not one or two.
    assert expected >= 4


# =====================================================================
# Test 2: concave shape (a notch) -- rows spanning the notch split into
#         multiple separate pieces; none crosses the notch.
# =====================================================================
def test_ripples_split_across_a_notch():
    # A 60x60 square with a top-middle vertical slot removed (x in [20,40],
    # y in [30,60]). A horizontal wave-row above y=30 must come back as two
    # separate pieces (left arm x<=20, right arm x>=40), never one line
    # bridging the slot.
    slot = box(20.0, 30.0, 40.0, 61.0)
    poly = box(0.0, 0.0, 60.0, 60.0).difference(slot)
    lines = _ripples(poly)
    assert lines, "the notched shape should still produce ripple lines"

    padded = poly.buffer(1e-6)
    for line in lines:
        assert padded.contains(line), "a ripple line escaped the notched polygon"

    # No returned line may enter the notch interior.
    slot_interior = box(20.0 + 1e-3, 30.0 + 1e-3, 40.0 - 1e-3, 60.0)
    for line in lines:
        assert line.intersection(slot_interior).is_empty, "a ripple line crossed into the notch"

    # Group the returned lines back into rows by their (spacing-quantized) mean y.
    rows = defaultdict(list)
    for line in lines:
        rows[round(_mean_y(line) / S)].append(line)

    # Rows whose baseline sits above the notch floor (y > 30) span the slot's
    # x-range, so each must come back as exactly two pieces.
    split_piece_counts = []
    for _, pieces in rows.items():
        row_y = _mean_y(pieces[0])
        if row_y > 30.0:
            split_piece_counts.append(len(pieces))
    assert split_piece_counts, "expected at least one wave-row crossing the notch"
    assert all(n == 2 for n in split_piece_counts), (
        f"every wave-row spanning the notch must split into exactly 2 pieces, got {split_piece_counts}"
    )


# =====================================================================
# Test 3: MultiPolygon input -- two disjoint squares. Lines in both parts,
#         none bridging the gap.
# =====================================================================
def test_ripples_multipolygon_two_disjoint_squares():
    left = box(0.0, 0.0, 20.0, 60.0)
    right = box(40.0, 0.0, 60.0, 60.0)
    mp = MultiPolygon([left, right])
    assert mp.geom_type == "MultiPolygon"

    lines = _ripples(mp)
    assert lines

    left_lines = [ln for ln in lines if ln.bounds[2] <= 20.0 + 1e-6]
    right_lines = [ln for ln in lines if ln.bounds[0] >= 40.0 - 1e-6]
    assert left_lines, "expected ripple lines in the left square"
    assert right_lines, "expected ripple lines in the right square"

    # No line may bridge the gap (span from the left part into the right part).
    for line in lines:
        minx, _, maxx, _ = line.bounds
        assert not (minx < 20.0 - 1e-6 and maxx > 40.0 + 1e-6), "a ripple line bridged the gap between parts"
        # Every line lives entirely in one part.
        assert maxx <= 20.0 + 1e-6 or minx >= 40.0 - 1e-6


# =====================================================================
# Test 4: phase stagger -- successive rows are not in phase.
# =====================================================================
def test_ripples_rows_are_phase_staggered():
    square = box(0.0, 0.0, 60.0, 60.0)
    lines = sorted(_ripples(square), key=_mean_y)
    assert len(lines) >= 2

    # Two vertically-adjacent full-width rows. If they were in phase, one would
    # be a pure vertical translate of the other, so (y_a(x) - y_b(x)) would be
    # constant across x -- zero variance. A non-zero std proves the phase offset.
    a, b = lines[len(lines) // 2], lines[len(lines) // 2 + 1]
    xs = np.linspace(2.0, 58.0, 40)
    diff = np.array([_y_at(a, x) - _y_at(b, x) for x in xs])
    assert float(np.std(diff)) > 0.1, (
        f"adjacent wave-rows must be phase-staggered, not vertically-translated copies "
        f"(std of y-difference={float(np.std(diff)):.4f})"
    )

    # And sampling the SAME x on the two rows: the deviation from each row's own
    # baseline differs.
    off_a = _y_at(a, 10.0) - _mean_y(a)
    off_b = _y_at(b, 10.0) - _mean_y(b)
    assert abs(off_a - off_b) > 1e-3


# =====================================================================
# Test 5: degenerate input -- empty list, no exception.
# =====================================================================
def test_ripples_degenerate_inputs_return_empty():
    # Empty geometry.
    assert _ripples(Polygon()) == []

    # A shape smaller than one spacing interval (height 2m << spacing 6m): it sits
    # entirely below the first interior wave-row baseline, so no row intersects it.
    assert _ripples(box(0.0, 0.0, 5.0, 2.0)) == []

    # An invalid (self-intersecting) polygon must not raise.
    bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])
    assert not bowtie.is_valid
    assert _ripples(bowtie) == []

    # None is tolerated too (display-only, must never fail a render).
    assert _ripples(None) == []


# ---------------------------------------------------------------------
# Offline render fixture for test 6 (network-free; basemap degrades
# gracefully -- same pattern the existing water-zone render test uses).
# ---------------------------------------------------------------------
_DEM = {
    "array": np.full((20, 20), 100.0, dtype=np.float32),
    "resolution_meters": (5.0, 5.0),
    "origin_x": 500000.0,
    "origin_y": 4500000.0,
    "crs": "EPSG:32617",
}
_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9838154, 40.6448343),
    (-79.9820000, 40.6448343),
    (-79.9820000, 40.6458343),
]


def _plain_fencing_result(boundary_coordinates, dem):
    """Network-free stand-in for identify_boundary_fencing(): the plain,
    no-canopy boundary loop, built from the pure find_boundary_fencing() +
    boundary_fencing_to_geojson() helpers (same approach test_render_layout_map.py
    uses for its own synthetic layers)."""
    xs, ys = warp_transform(
        "EPSG:4326", dem["crs"], [pt[0] for pt in boundary_coordinates], [pt[1] for pt in boundary_coordinates]
    )
    boundary_polygon_utm = Polygon(zip(xs, ys))
    # Plain no-canopy, no-developed-footprint case: every developed-footprint/zone input empty/None,
    # so find_boundary_fencing() collapses to the boundary's own ring (see step 2's degenerate fallback).
    rings_utm = find_boundary_fencing(
        boundary_polygon_utm,
        None,
        production_zone_polygons_utm=[],
        structure_site_polygon_utm=None,
        road_corridor_cell_footprint_polygon_utm=None,
        water_zone_polygon_utm=None,
        tree_zone_polygons_utm=[],
    )
    rings_wgs84 = [shape(transform_geom(dem["crs"], "EPSG:4326", mapping(ring))) for ring in rings_utm]
    return {
        "fencing_geojson": boundary_fencing_to_geojson(rings_wgs84),
        "segment_count": len(rings_utm),
    }


def _water_zone_fixture():
    """A minimal water zone dict shaped exactly as render_layout_map() reads it:
    a render_fill_polygon_utm (the render opening, in the DEM's UTM CRS), an id,
    and a suitability_score. The opening is large enough that ripple rows land
    inside it."""
    return {
        "id": 3,
        "suitability_score": 72.5,
        "render_fill_polygon_utm": box(500010.0, 4499910.0, 500090.0, 4499990.0),
    }


# =====================================================================
# Test 6: nothing else moved -- the water zone's marker position and
#         legend text are unchanged from before this branch (the ripple
#         change only swaps the fill/outline for ripple lines).
# =====================================================================
def test_water_marker_and_legend_unchanged():
    water_zone = _water_zone_fixture()
    layers = {
        "dem": _DEM,
        "production_areas": [],
        "production_zone_legend_stats": [],
        "water_zone": water_zone,
        "road_corridor": [],
        "tree_zone_result": None,
        "structure_site": None,
        "water_features": {"streams": []},
        "contour_lines": [],
        "fencing_result": _plain_fencing_result(_BOUNDARY, _DEM),
    }

    # Spy on the numbered-marker placement.
    recorded_markers = []
    original_marker = rlm._draw_numbered_marker
    rlm._draw_numbered_marker = (
        lambda ax, point, number: recorded_markers.append((point, number)) or original_marker(ax, point, number)
    )

    # Spy on legend text (ax.text renders the joined legend block).
    import matplotlib.axes as maxes

    recorded_text = []
    original_text = maxes.Axes.text

    def _recording_text(self, x, y, s, *a, **kw):
        recorded_text.append(s)
        return original_text(self, x, y, s, *a, **kw)

    maxes.Axes.text = _recording_text

    # Spy on plot_polygon: the water zone must draw NO fill polygon anymore.
    recorded_polygon_kwargs = []
    original_plot_polygon = rlm.plot_polygon

    def _recording_plot_polygon(geometry, **kwargs):
        recorded_polygon_kwargs.append(kwargs)
        return original_plot_polygon(geometry, **kwargs)

    rlm.plot_polygon = _recording_plot_polygon

    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "layout.png")
            result = rlm.render_layout_map(_BOUNDARY, out, layers=layers)
            assert result == out
            assert os.path.getsize(out) > 0, "render_layout_map() must produce a real, non-empty PNG"
    finally:
        rlm._draw_numbered_marker = original_marker
        maxes.Axes.text = original_text
        rlm.plot_polygon = original_plot_polygon

    # Marker: exactly one (the water zone), placed at the reprojected render
    # opening's own representative_point() -- unchanged placement.
    assert len(recorded_markers) == 1, f"expected exactly 1 numbered marker, got {len(recorded_markers)}"
    marker_point, marker_number = recorded_markers[0]
    expected_geom = rlm._reproject_utm_geometry_to_mercator(water_zone["render_fill_polygon_utm"], _DEM["crs"])
    expected_point = expected_geom.representative_point()
    assert marker_point.distance(expected_point) < 1e-6, (
        "the water zone marker must still sit at render_fill_polygon_utm's own representative_point()"
    )

    # Legend text: unchanged f-string, exactly as before this branch.
    legend_blocks = [s for s in recorded_text if "Water System" in s]
    assert len(legend_blocks) == 1, "expected exactly one legend block mentioning the water zone"
    expected_line = (
        f"{marker_number} — Water System, Zone {water_zone['id']}, "
        f"score {water_zone['suitability_score']}"
    )
    assert expected_line in legend_blocks[0], (
        f"the water zone legend text changed. expected to find:\n  {expected_line!r}\n"
        f"in:\n  {legend_blocks[0]!r}"
    )

    # No fill polygon is drawn for the water zone anymore (no plot_polygon call
    # carrying the water blue as its facecolor).
    water_fill_calls = [kw for kw in recorded_polygon_kwargs if kw.get("facecolor") == WATER_ZONE_COLOR]
    assert not water_fill_calls, (
        f"the water zone must no longer draw a filled polygon, found {len(water_fill_calls)} fill call(s)"
    )
