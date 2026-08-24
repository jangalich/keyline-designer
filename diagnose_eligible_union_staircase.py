"""
diagnose_eligible_union_staircase.py

Standing, read-only diagnostic: MEASURES two candidate operations for
removing the 5 m cell staircase from exclusion_zones.build_eligible_union()'s
output, and reports both. It applies neither, recommends neither, and changes
no pipeline module. The reviewer chooses on the numbers.

WHY THIS IS A MEASUREMENT AND NOT A CHANGE
------------------------------------------
The eligible union is the interactive flow's display-and-clamping geometry:
the highlight a user picks production ground out of, and eventually the shape
a drawn polygon is constrained against. Its boundary is a literal per-cell
right-angle staircase, which reads as cells rather than as ground.

Two operations can remove it. They fail in different ways and the choice
between them is a judgement about which failure is acceptable, so this script
produces the figures and stops.

WHY THE DIRECTION OF ERROR IS SAFE HERE, UNLIKE ON THE EXCLUSION UNION
---------------------------------------------------------------------
A previous branch measured and REJECTED smoothing the EXCLUSION union
(test_exclusion_smoothing.py). The argument was geometric, not aesthetic: a
Chaikin pass on a closed ring is net area-reducing, always, because any
simple closed ring turns a net +360 degrees, so the inward convex cuts
necessarily outweigh the outward reflex ones. Smoothing the exclusion
therefore SHRANK the exclusion and GREW eligible ground -- republishing 0.655
acres of gate-excluded, tree-covered land as selectable.

Applied to the ELIGIBLE union, that same theorem points the other way and is
protective: eligible ground shrinks, so some genuinely selectable land simply
is not highlighted. Conservative, and the error lands where it should.

The other objection from that branch has also lapsed. It rejected smoothing
partly because the exclusion and eligible layers would then disagree along a
shared boundary. Under the current plan they are not derived from each other:
the five per-gate layers stay EXACT, for intersection and captioning, and the
eligible union is built independently from the cell mask. A sliver of
disagreement between them is genuinely eligible ground that is merely not
highlighted -- not a rendering fault and not a clamping fault.

THE TWO OPTIONS
---------------
OPTION 1 -- angular simplify + Chaikin (raster_grid's relocated helpers, the
same order and constant pattern the production fill already uses). Direction
is guaranteed inward by the theorem above. MAGNITUDE IS NOT BOUNDED. The
exclusion-union measurement put it at 4.75% at the gentlest supportable
settings, with nothing gentler available. Reported here as three separate
figures -- simplify alone, Chaikin alone, and the two composed -- because
that branch found the passes are NOT independent: each alone moved the union
under 1.3% while composed they moved it 4.75%, since Chaikin's corner cut
scales with edge length and collapsing the staircase is exactly what turns
hundreds of 5 m edges into a few long ones.

OPTION 2 -- vector opening, buffer(-r).buffer(+r), round join. Anti-extensive
by construction. Removes the staircase by construction too, since a round
buffer emits arcs rather than cell corners. Costs genuine convex corners of
the eligible region, and at small r may not fully erase a 5 m staircase --
so r is swept at one, two and three cells.

READ THE MAX-EXCURSION COLUMNS CAREFULLY -- see max_inward_excursion() and
removed_ground_reach() below. The claim that an opening's inward error is
bounded by exactly r is TRUE OF THE GROUND IT REMOVES and FALSE OF THE
BOUNDARY IT MOVES, and this script reports both so the difference is visible
rather than assumed.

All fixtures are synthetic and deterministic. Nothing here touches the
network or any real-property number.
"""

import numpy as np
from shapely.geometry import MultiPolygon, Polygon, box

from exclusion_zones import build_eligible_union
from raster_grid import (
    SQUARE_METERS_PER_ACRE,
    angular_simplify_closed_ring,
    angular_smooth_polygon,
    cell_union_footprint,
    chaikin_smooth_closed_ring,
)
from shapely.geometry import LineString

CELL = 5.0  # the pipeline's own DEM resolution

# Option 1's settings: the gentlest supportable values, matching the pattern
# PRODUCTION_FILL_SIMPLIFY_TOLERANCE_CELLS / PRODUCTION_FILL_CHAIKIN_ITERATIONS
# already use. Expressed in CELLS and multiplied by the DEM's own resolution at
# the point of use, so "one cell" stays one cell at any resolution.
SIMPLIFY_TOLERANCE_CELLS = 1.0
CHAIKIN_ITERATIONS = 1

# Option 2's sweep, in cells.
OPENING_RADII_CELLS = (1.0, 2.0, 3.0)

# How finely the true boundary is sampled when measuring excursions. A tenth of
# a cell: fine enough that the measured maximum is not an artifact of sampling,
# coarse enough to stay fast on a few thousand vertices.
BOUNDARY_SAMPLE_SPACING_M = CELL / 10.0


# ---------------------------------------------------------------------------
# METRICS
# ---------------------------------------------------------------------------


def _parts(geometry) -> list:
    if geometry.is_empty:
        return []
    return [geometry] if geometry.geom_type == "Polygon" else list(geometry.geoms)


def acres(square_meters: float) -> float:
    return square_meters / SQUARE_METERS_PER_ACRE


def exterior_vertices(geometry) -> int:
    return sum(len(part.exterior.coords) for part in _parts(geometry))


def polygon_count(geometry) -> int:
    return len(_parts(geometry))


def interior_ring_count(geometry) -> int:
    return sum(len(part.interiors) for part in _parts(geometry))


def one_cell_axis_aligned_segments(geometry, cell: float = CELL, tolerance: float = 1e-6) -> int:
    """
    The count of boundary segments that are BOTH axis-aligned AND exactly one
    cell long -- an objective proxy for "this still looks like cells". A raw
    cell-union boundary is made of nothing else; an operation that has really
    removed the staircase drives this to zero or near it.

    Counts every ring, exterior and interior, of every part. Deliberately
    strict about length: a two-cell run that survived as one straight segment
    is not a staircase step, it is a real edge.
    """
    total = 0
    for part in _parts(geometry):
        for ring in [part.exterior, *part.interiors]:
            coords = list(ring.coords)
            for (x0, y0), (x1, y1) in zip(coords, coords[1:]):
                dx, dy = abs(x1 - x0), abs(y1 - y0)
                axis_aligned = dx < tolerance or dy < tolerance
                if axis_aligned and abs((dx + dy) - cell) < tolerance:
                    total += 1
    return total


def max_inward_excursion(original, result, spacing: float = BOUNDARY_SAMPLE_SPACING_M) -> float:
    """
    THE BRIEF'S DEFINITION: the furthest any point of the TRUE BOUNDARY sits
    outside the result. Every ring of `original` is densified to `spacing` and
    each sample's distance to `result` is taken; a sample still covered by the
    result contributes 0.

    THIS IS NOT BOUNDED BY r FOR AN OPENING, and the reason is structural
    rather than a measurement artifact. An opening deletes any protrusion too
    thin to contain a radius-r disc -- a one-cell-wide finger vanishes
    ENTIRELY, however long it is, so its tip's distance to the result is the
    finger's whole length, not r. The bounded quantity is a different one; see
    removed_ground_reach() below.
    """
    worst = 0.0
    for part in _parts(original):
        for ring in [part.exterior, *part.interiors]:
            for x, y in LineString(ring.coords).segmentize(spacing).coords:
                from shapely.geometry import Point

                worst = max(worst, Point(x, y).distance(result))
    return worst


def removed_ground_reach(original, result, spacing: float = BOUNDARY_SAMPLE_SPACING_M) -> float:
    """
    THE QUANTITY AN OPENING ACTUALLY BOUNDS: how far the ground it removed
    reaches from the nearest INELIGIBLE ground. Formally, the maximum over
    removed points p of dist(p, complement of original).

    Why this one is bounded by r, exactly: a point p survives an opening iff
    some radius-r disc containing p fits inside the original. If the disc
    B(p, r) itself fits, p survives. So every REMOVED p has B(p, r) not
    contained in the original, which means some ineligible point lies within r
    of p. Every square metre an opening takes away is therefore within r of
    ground that was never eligible anyway -- which is the guarantee that
    matters for a highlight, and it holds however long and thin the removed
    protrusion is.

    Measured on the boundary of the removed region, which is where the maximum
    of a distance-to-complement is attained on a closed set.
    """
    removed = original.difference(result)
    if removed.is_empty:
        return 0.0
    original_boundary = original.boundary
    worst = 0.0
    for part in _parts(removed):
        for ring in [part.exterior, *part.interiors]:
            for x, y in LineString(ring.coords).segmentize(spacing).coords:
                from shapely.geometry import Point

                point = Point(x, y)
                # Only interior-of-original samples are informative: a sample
                # lying ON the original's own boundary is trivially at 0.
                worst = max(worst, point.distance(original_boundary))
    return worst


def metrics(original, result, label: str) -> dict:
    return {
        "label": label,
        "acres_before": acres(original.area),
        "acres_after": acres(result.area),
        "area_ratio": (result.area / original.area) if original.area > 0 else float("nan"),
        "vertices_before": exterior_vertices(original),
        "vertices_after": exterior_vertices(result),
        "polygons_before": polygon_count(original),
        "polygons_after": polygon_count(result),
        "holes_before": interior_ring_count(original),
        "holes_after": interior_ring_count(result),
        "one_cell_segments_before": one_cell_axis_aligned_segments(original),
        "one_cell_segments_after": one_cell_axis_aligned_segments(result),
        "max_inward_excursion_m": max_inward_excursion(original, result),
        "removed_ground_reach_m": removed_ground_reach(original, result),
        "valid": bool(result.is_valid),
    }


# ---------------------------------------------------------------------------
# THE TWO OPTIONS
# ---------------------------------------------------------------------------


def _per_ring(geometry, ring_fn):
    """Applies ring_fn to the exterior and every interior ring of every part --
    the same polygon-level lift angular_smooth_polygon() performs, reproduced
    here so each HALF of Option 1 can be measured on its own."""
    def one(poly):
        return Polygon(ring_fn(poly.exterior), [ring_fn(interior) for interior in poly.interiors])

    parts = [one(part) for part in _parts(geometry)]
    if not parts:
        return geometry
    out = parts[0] if len(parts) == 1 else MultiPolygon(parts)
    return out if out.is_valid else out.buffer(0)


def option1_simplify_only(geometry, tolerance_m: float):
    return _per_ring(
        geometry,
        lambda ring: list(angular_simplify_closed_ring(LineString(ring.coords), tolerance_m).coords),
    )


def option1_chaikin_only(geometry, iterations: int):
    return _per_ring(geometry, lambda ring: chaikin_smooth_closed_ring(list(ring.coords), iterations))


def option1_composed(geometry, tolerance_m: float, iterations: int):
    return angular_smooth_polygon(geometry, tolerance_m, iterations)


def option2_opening(geometry, radius_m: float):
    """
    buffer(-r).buffer(+r) with ROUND joins -- the vector morphological opening.
    Round is not a style preference: a mitre or bevel join would put corners
    back into a result whose whole purpose is to have none.
    """
    return geometry.buffer(-radius_m, join_style="round").buffer(radius_m, join_style="round")


# ---------------------------------------------------------------------------
# FIXTURES -- two boundary-SHAPED synthetic parcels
# ---------------------------------------------------------------------------


def _dem(rows: int, cols: int, array: np.ndarray) -> dict:
    return {
        "array": array,
        "resolution_meters": (CELL, CELL),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }


def _boundary(dem: dict):
    rows, cols = dem["array"].shape
    return box(
        dem["origin_x"] + 2 * CELL,
        dem["origin_y"] - (rows - 2) * CELL,
        dem["origin_x"] + (cols - 2) * CELL,
        dem["origin_y"] - 2 * CELL,
    )


def _correlated(shape, seed: int, passes: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    field = rng.standard_normal(shape)
    for _ in range(passes):
        field = (
            field
            + np.roll(field, 1, 0)
            + np.roll(field, -1, 0)
            + np.roll(field, 1, 1)
            + np.roll(field, -1, 1)
        ) / 5.0
    return (field - field.mean()) / field.std()


def rolling_fixture():
    """Fixture A -- rolling ground: a broad eligible field with scattered
    excluded stands punched through it. The common case."""
    rows = cols = 90
    elevation = np.zeros((rows, cols), np.float32)
    for r in range(rows):
        elevation[r, :] = 100.0 + r * 0.35
    dem = _dem(rows, cols, elevation)
    boundary = _boundary(dem)
    excluded = _correlated((rows, cols), seed=11, passes=22) > 0.85
    eligible = ~excluded
    return "A: rolling ground", dem, boundary, eligible


def ridge_fixture():
    """Fixture B -- a ridge with fingers: eligible ground reaching out in
    narrow runs between excluded draws. The case an opening treats worst, and
    the reason the excursion columns are reported separately."""
    rows = cols = 90
    elevation = np.zeros((rows, cols), np.float32)
    for r in range(rows):
        elevation[r, :] = 100.0 + r * 0.35
    dem = _dem(rows, cols, elevation)
    boundary = _boundary(dem)
    field = _correlated((rows, cols), seed=5, passes=9)
    eligible = field > -0.15
    # Deliberate one- and two-cell-wide fingers of eligible ground reaching
    # into excluded terrain: exactly the structure a vector opening deletes.
    eligible[30:40, :] = False
    for offset, width in ((10, 1), (24, 1), (38, 2), (54, 2), (70, 1)):
        eligible[30:40, offset : offset + width] = True
    return "B: ridge with fingers", dem, boundary, eligible


FIXTURES = (rolling_fixture, ridge_fixture)


def fixture_union(builder):
    name, dem, boundary, eligible = builder()
    union = build_eligible_union(dem, eligible, boundary)
    return name, dem, boundary, union


# ---------------------------------------------------------------------------
# REPORT
# ---------------------------------------------------------------------------

_COLUMNS = (
    ("operation", 30, "{:<30s}"),
    ("acres", 8, "{:>8.3f}"),
    ("ratio", 7, "{:>7.4f}"),
    ("verts", 7, "{:>7d}"),
    ("polys", 6, "{:>6d}"),
    ("holes", 6, "{:>6d}"),
    ("1-cell seg", 11, "{:>11d}"),
    ("max excur m", 12, "{:>12.2f}"),
    ("rmvd reach m", 13, "{:>13.2f}"),
)


def _header() -> str:
    return "  ".join(f"{name:<{width}s}" if i == 0 else f"{name:>{width}s}"
                     for i, (name, width, _) in enumerate(_COLUMNS))


def _row(m: dict) -> str:
    values = (
        m["label"],
        m["acres_after"],
        m["area_ratio"],
        m["vertices_after"],
        m["polygons_after"],
        m["holes_after"],
        m["one_cell_segments_after"],
        m["max_inward_excursion_m"],
        m["removed_ground_reach_m"],
    )
    return "  ".join(fmt.format(v) for (_, _, fmt), v in zip(_COLUMNS, values))


def measure_fixture(builder) -> tuple[str, list[dict]]:
    name, dem, boundary, union = fixture_union(builder)
    tolerance_m = SIMPLIFY_TOLERANCE_CELLS * max(dem["resolution_meters"])

    rows = [metrics(union, union, "raw eligible union (baseline)")]
    rows.append(metrics(union, option1_simplify_only(union, tolerance_m), "opt1: simplify only (1 cell)"))
    rows.append(metrics(union, option1_chaikin_only(union, CHAIKIN_ITERATIONS), "opt1: Chaikin only (1 iter)"))
    rows.append(
        metrics(union, option1_composed(union, tolerance_m, CHAIKIN_ITERATIONS), "opt1: composed (simplify+Chaikin)")
    )
    for radius_cells in OPENING_RADII_CELLS:
        radius_m = radius_cells * max(dem["resolution_meters"])
        rows.append(
            metrics(union, option2_opening(union, radius_m), f"opt2: opening r={radius_cells:g} cell ({radius_m:g} m)")
        )
    return name, rows


def main() -> None:
    print("=" * 118)
    print("ELIGIBLE UNION -- STAIRCASE REMOVAL MEASUREMENT (no operation is applied; this only measures)")
    print("=" * 118)
    for builder in FIXTURES:
        name, rows = measure_fixture(builder)
        baseline = rows[0]
        print(f"\nFIXTURE {name}")
        print(
            f"  baseline: {baseline['acres_before']:.3f} ac, {baseline['vertices_before']} exterior vertices, "
            f"{baseline['polygons_before']} polygons, {baseline['holes_before']} interior rings, "
            f"{baseline['one_cell_segments_before']} one-cell axis-aligned segments"
        )
        print()
        print("  " + _header())
        print("  " + "-" * 116)
        for row in rows:
            print("  " + _row(row))
    print(
        "\nCOLUMN NOTES\n"
        "  1-cell seg    axis-aligned boundary segments exactly one cell long -- the objective\n"
        "                'does it still look like cells?' proxy. The baseline is made of nothing else.\n"
        "  max excur m   furthest any point of the TRUE boundary sits outside the result. For an\n"
        "                opening this is NOT bounded by r: a protrusion too thin to hold a radius-r\n"
        "                disc is deleted entirely, so its tip's distance is the protrusion's whole\n"
        "                length. See max_inward_excursion().\n"
        "  rmvd reach m  furthest the removed ground reaches from ineligible ground. THIS is the\n"
        "                quantity an opening bounds by exactly r, and it holds however thin the\n"
        "                removed protrusion is. See removed_ground_reach().\n"
        "\nNo recommendation is made here. Both options are reported; the choice is the reviewer's."
    )


if __name__ == "__main__":
    main()
