"""
generate_contour_background.py

One-off build-time asset generator: turns a local 3DEP GeoTIFF into the
static contour-linework SVG that sits behind the marketing page.

This is NOT a pipeline module. It fetches nothing, it is never imported by
api.py or main.py, and it adds no layer to the three-layer architecture --
it runs by hand, once, and its output is committed to the frontend repo as
a static asset. It lives in scripts/ for exactly that reason.

Input is the GeoTIFF written by the manual fetch step (see the task notes):
3DEP elevation over the marketing window, already reprojected to UTM by
dem_data.get_dem_for_boundary()'s own imageSR request, elevation in METERS.

Run:
    python3 scripts/generate_contour_background.py [source.tif]

with the source defaulting to ~/keyline-scratch/contour-source.tif.

Everything below is offline. Contour geometry comes from the pipeline's own
contour_lines.compute_contour_lines() rather than a separate matplotlib
pass, so the background asset and the report maps come off one engine.
"""

import math
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import rasterio
from shapely.geometry import LineString, MultiLineString

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contour_lines import compute_contour_lines  # noqa: E402

SOURCE_TIF = Path("~/keyline-scratch/contour-source.tif").expanduser()
OUTPUT_DIR = Path("~/keyline-scratch").expanduser()

METERS_PER_FOOT = 0.3048

# All three in one run -- the right interval is chosen by looking at the
# previews, not by calculating it.
INTERVALS_FEET = (10, 20, 40)

# Douglas-Peucker tolerances, in METERS of ground (the SVG's user units are
# ground meters, so a tolerance in meters is directly a tolerance in user
# units). 0.0 is the unsimplified baseline, kept so the tradeoff table has
# a real "before" column. At the ~1500px this renders at across a 2.1km
# window, 1 meter is roughly 0.7 CSS pixels -- so 10m is ~7px of allowed
# deviation, which genuinely rounds the curves.
SIMPLIFY_TOLERANCES_M = (0.0, 2.0, 5.0, 10.0, 20.0)

# The interval whose simplification tolerances get rendered side by side at
# background conditions, so the "you cannot see the difference" claim is
# something to look at rather than take on trust.
SWEEP_INTERVAL_FEET = 20

# Contour fragments shorter than this are dropped. Aggressive simplification
# collapses short specks into near-degenerate 2-vertex stubs that read as
# dirt rather than terrain, and they are pure file-size cost.
MIN_SEGMENT_LENGTH_M = 25.0

# Coordinate precision in the emitted path data. User units are ground
# meters; at typical render scale 1 unit is under a pixel, so one decimal
# place is already sub-pixel and any more is wasted bytes.
COORD_DECIMALS = 1

# --- Preview conditions -------------------------------------------------
# These colors exist ONLY to render throwaway preview PNGs. None of them
# reach the SVG -- _assert_no_color_literals() enforces that.
PREVIEW_PAGE_HEX = "#f4f1ea"
PREVIEW_LINE_HEX = "#4a4a4a"
PREVIEW_LINE_ALPHA = 0.06
PREVIEW_INK_HEX = "#232019"
PREVIEW_MUTED_HEX = "#6b6355"

# (label, width_px, height_px). The phone crop matters: the background is
# fixed and cover-scaled, so a portrait viewport shows a much narrower
# slice of the artwork than the desktop crop suggests.
PREVIEW_VIEWPORTS = (
    ("desktop", 1440, 900),
    ("phone", 390, 844),
)


# --- DEM loading --------------------------------------------------------

def load_dem(path: Path) -> dict:
    """
    Reads the GeoTIFF into the same plain dict shape
    dem_data.get_dem_for_boundary() returns, so contour_lines.py and
    raster_grid.py work on it unchanged.

    Refuses a geographic (lon/lat) raster outright. Contouring a lon/lat
    grid stretches the terrain horizontally by 1/cos(latitude) -- about 24%
    at this window's 40.6N. Nobody would consciously notice; it would just
    look subtly wrong. The fetch already asks 3DEP for UTM, so a geographic
    raster here means the wrong file was passed, not something to correct
    silently.
    """
    with rasterio.open(path) as src:
        if not src.crs or not src.crs.is_projected:
            raise SystemExit(
                f"{path} is in {src.crs}, which is not a projected CRS. Contours must "
                "be computed in a projected CRS (UTM) or the terrain comes out "
                f"horizontally squashed. Re-run the fetch through "
                "dem_data.get_dem_for_boundary(), which reprojects to UTM server-side."
            )

        array = src.read(1).astype("float32")
        if src.nodata is not None:
            array[np.isclose(array, src.nodata)] = np.nan
        # Same defensive floor dem_data.py applies: resampling blends nodata
        # into real values near a data gap, leaving large negative artifacts.
        array[array <= -1000.0] = np.nan

        transform = src.transform
        return {
            "array": array,
            "resolution_meters": (transform.a, -transform.e),
            "origin_x": transform.c,
            "origin_y": transform.f,
            "crs": src.crs.to_string(),
        }


def dem_extent(dem: dict) -> tuple[float, float, float, float]:
    """(min_x, min_y, max_x, max_y) of the DEM's real ground footprint."""
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    return (
        dem["origin_x"],
        dem["origin_y"] - rows * py,
        dem["origin_x"] + cols * px,
        dem["origin_y"],
    )


# --- Geometry -----------------------------------------------------------

def _explode(geometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, MultiLineString):
        return [g for g in geometry.geoms if not g.is_empty]
    return [geometry]


def contour_linestrings(dem: dict, interval_meters: float) -> list[LineString]:
    """Every contour segment at this interval, flattened across all levels."""
    lines = []
    for level in compute_contour_lines(dem, interval_meters=interval_meters):
        lines.extend(_explode(level["lines_utm"]))
    return lines


def simplify_lines(lines: list[LineString], tolerance_m: float) -> list[LineString]:
    """
    Douglas-Peucker at `tolerance_m`, then drop the specks.

    preserve_topology=False is deliberate: it is real Douglas-Peucker and
    simplifies considerably harder. The topology-preserving variant exists
    to stop a polygon collapsing through itself, which is not a risk for
    open contour lines drawn at 6% opacity.
    """
    out = []
    for line in lines:
        simplified = line if tolerance_m <= 0 else line.simplify(tolerance_m, preserve_topology=False)
        if simplified.is_empty or len(simplified.coords) < 2:
            continue
        if simplified.length < MIN_SEGMENT_LENGTH_M:
            continue
        out.append(simplified)
    return out


def count_vertices(lines: list[LineString]) -> int:
    return sum(len(line.coords) for line in lines)


# --- SVG ----------------------------------------------------------------

_COLOR_NAMES = (
    "black", "white", "grey", "gray", "silver", "red", "green", "blue",
    "brown", "tan", "beige", "olive", "navy", "teal", "slate", "stone",
)
_COLOR_PATTERN = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|\b(?:" + "|".join(_COLOR_NAMES) + r")\b",
    re.IGNORECASE,
)


def _assert_no_color_literals(svg: str) -> None:
    """
    Hard gate: no color may reach the SVG. Color and opacity are CSS
    token decisions, and a literal committed into a static asset is
    invisible to the token system and to the standing no-literals check --
    it would be the first one to escape it.
    """
    found = _COLOR_PATTERN.findall(svg)
    if found:
        raise SystemExit(f"Color literal(s) leaked into the SVG: {sorted(set(found))}")


def build_svg(lines: list[LineString], extent: tuple[float, float, float, float]) -> str:
    """
    Paths only. No <title>, no <metadata>, no editor namespaces, no color,
    no stroke-width -- CSS owns all of that.

    User units are ground meters and the viewBox is the projected extent,
    so the aspect ratio is the terrain's true aspect ratio by construction.
    preserveAspectRatio="xMidYMid slice" makes the file behave like
    background-size: cover, which is how it gets used.

    Path data uses SVG's implicit-lineto form: after "M x y", every
    following coordinate pair is an implicit L. Same geometry as spelling
    out each L, meaningfully fewer bytes.
    """
    min_x, min_y, max_x, max_y = extent
    width_m = max_x - min_x
    height_m = max_y - min_y

    subpaths = []
    for line in lines:
        coords = [
            f"{x - min_x:.{COORD_DECIMALS}f} {max_y - y:.{COORD_DECIMALS}f}"
            for x, y in line.coords
        ]
        subpaths.append("M" + " ".join(coords))

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {width_m:.{COORD_DECIMALS}f} {height_m:.{COORD_DECIMALS}f}" '
        f'preserveAspectRatio="xMidYMid slice">'
        f'<path fill="none" stroke="currentColor" d="{"".join(subpaths)}"/>'
        f"</svg>"
    )


def write_svg(lines: list[LineString], extent, path: Path) -> int:
    svg = build_svg(lines, extent)
    _assert_no_color_literals(svg)
    path.write_text(svg, encoding="utf-8")
    return len(svg.encode("utf-8"))


# --- Preview ------------------------------------------------------------

_PREVIEW_HEADLINE = "Design your land around the water it already carries."

_PREVIEW_BODY = (
    "Keyline design reads a property's own terrain before it proposes anything. "
    "The ridges, the valleys, the way water already moves across the ground when "
    "it rains -- those come first, and the fields, roads, dams and tree lines "
    "follow from them.",
    "Upload a boundary and the analysis walks the same sequence a keyline "
    "consultant would: elevation, slope, flow accumulation, the keypoints where "
    "a valley's grade breaks. Every recommendation traces back to something "
    "measured on your parcel.",
)


def _cover_window(extent, viewport_px: tuple[int, int]):
    """
    The slice of the artwork a fixed, cover-scaled background actually shows
    in a viewport of this size -- background-size: cover, centered.

    Scale to whichever axis needs the most magnification to fill the
    viewport, then keep the centered viewport-sized window of what that
    produces. For a portrait phone against this tall-but-not-that-tall
    artwork, the height fills first and the visible window is a narrow
    vertical strip -- far narrower than the desktop crop suggests.
    """
    min_x, min_y, max_x, max_y = extent
    width_m, height_m = max_x - min_x, max_y - min_y
    vw, vh = viewport_px

    px_per_m = max(vw / width_m, vh / height_m)
    visible_w = vw / px_per_m
    visible_h = vh / px_per_m

    cx, cy = (min_x + max_x) / 2, (min_y + max_y) / 2
    return (cx - visible_w / 2, cy - visible_h / 2, cx + visible_w / 2, cy + visible_h / 2)


def render_preview(lines, extent, viewport, out_path: Path, caption: str) -> None:
    """
    The only preview that answers anything: linework at real background
    conditions -- mid grey at ~6% opacity over the page cream -- with real
    body text on top. Bare contours on white always look good and tell you
    nothing about whether text stays readable or the terrain reads as
    terrain.

    Drawn from the same simplified geometry write_svg() serializes, so what
    this shows is what the SVG contains.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.collections import LineCollection

    label, vw, vh = viewport
    dpi = 200
    fig = plt.figure(figsize=(vw / 100, vh / 100), dpi=dpi)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_facecolor(PREVIEW_PAGE_HEX)
    fig.patch.set_facecolor(PREVIEW_PAGE_HEX)

    win_min_x, win_min_y, win_max_x, win_max_y = _cover_window(extent, (vw, vh))
    ax.set_xlim(win_min_x, win_max_x)
    ax.set_ylim(win_min_y, win_max_y)
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)

    # Stroke width in points that matches ~1 CSS px at this viewport.
    stroke_pt = 0.75 * (100 / dpi) * (dpi / 72) * 0.72
    ax.add_collection(
        LineCollection(
            [np.asarray(line.coords) for line in lines],
            colors=PREVIEW_LINE_HEX,
            alpha=PREVIEW_LINE_ALPHA,
            linewidths=stroke_pt,
        )
    )

    is_phone = vw < 700
    head_size = 15 if is_phone else 26
    body_size = 7.5 if is_phone else 11
    left = 0.08 if is_phone else 0.10
    right = 0.92 if is_phone else 0.62
    wrap_width = right - left

    ax.text(
        left, 0.88 if is_phone else 0.80, _PREVIEW_HEADLINE,
        transform=ax.transAxes, fontsize=head_size, color=PREVIEW_INK_HEX,
        va="top", ha="left", wrap=True, linespacing=1.2,
        fontweight="regular",
    ).set_wrap(True)

    y = 0.72 if is_phone else 0.62
    for paragraph in _PREVIEW_BODY:
        wrapped = _wrap(paragraph, 44 if is_phone else 66)
        ax.text(
            left, y, wrapped, transform=ax.transAxes, fontsize=body_size,
            color=PREVIEW_INK_HEX, va="top", ha="left", linespacing=1.65,
        )
        y -= 0.055 * len(wrapped.split("\n")) * (1.35 if is_phone else 1.0)

    ax.text(
        left, 0.04, caption, transform=ax.transAxes, fontsize=6 if is_phone else 8,
        color=PREVIEW_MUTED_HEX, va="bottom", ha="left",
    )

    fig.savefig(out_path, dpi=dpi, facecolor=PREVIEW_PAGE_HEX)
    plt.close(fig)


def _wrap(text: str, width: int) -> str:
    import textwrap
    return "\n".join(textwrap.wrap(text, width=width))


# --- Main ---------------------------------------------------------------

def main() -> None:
    source = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else SOURCE_TIF
    if not source.exists():
        raise SystemExit(
            f"{source} not found. Run the manual 3DEP fetch first -- this "
            "script is fully offline and does not fetch anything itself."
        )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    dem = load_dem(source)
    extent = dem_extent(dem)
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]

    valid = dem["array"][~np.isnan(dem["array"])]
    if valid.size == 0:
        raise SystemExit("DEM has no valid elevation cells.")

    min_m, max_m = float(valid.min()), float(valid.max())
    relief_m = max_m - min_m

    print("=" * 78)
    print("SOURCE")
    print("=" * 78)
    print(f"  file        {source}")
    print(f"  grid        {cols} x {rows} cells at {px:.2f} x {py:.2f} m")
    print(f"  crs         {dem['crs']}")
    print(f"  extent      {extent[2] - extent[0]:.0f} x {extent[3] - extent[1]:.0f} m "
          f"(aspect {(extent[2] - extent[0]) / (extent[3] - extent[1]):.4f})")
    print(f"  nodata      {int(np.isnan(dem['array']).sum())} / {dem['array'].size} cells")
    print()
    print(f"  RELIEF      min {min_m:.1f} m ({min_m / METERS_PER_FOOT:.0f} ft)")
    print(f"              max {max_m:.1f} m ({max_m / METERS_PER_FOOT:.0f} ft)")
    print(f"              drop {relief_m:.1f} m ({relief_m / METERS_PER_FOOT:.0f} ft)")
    print()

    # Which tolerance the per-interval previews are rendered at. 5m is the
    # aggressive-but-honest setting: it takes roughly 88% of the bytes out
    # and is indistinguishable from unsimplified at real viewing size, while
    # 20m visibly turns small closed contours into triangles. See the
    # tolerance sweep this script also writes.
    recommended_tolerance = 5.0
    previews = []
    sweeps = []

    for interval_ft in INTERVALS_FEET:
        interval_m = interval_ft * METERS_PER_FOOT
        started = time.perf_counter()
        raw = contour_linestrings(dem, interval_m)
        elapsed = time.perf_counter() - started

        levels = math.floor(relief_m / interval_m)
        print("=" * 78)
        print(f"{interval_ft} FT INTERVAL  ({interval_m:.3f} m)")
        print("=" * 78)
        print(f"  levels crossing the relief range   {levels}")
        print(f"  contour lines, before any filter   {len(raw)}")
        print(f"  vertices, before any filter        {count_vertices(raw):,}")
        print(f"  contour compute                    {elapsed:.1f}s")
        print()
        print(f"  Rows below all have the <{MIN_SEGMENT_LENGTH_M:.0f}m speck filter applied; the 0m row is")
        print("  filter-only (no simplification) and is the baseline the percentages use.")
        print()
        print(f"  {'tolerance':>10}  {'lines':>7}  {'vertices':>10}  {'vs 0m':>7}  "
              f"{'svg bytes':>11}  {'vs 0m':>7}")
        print("  " + "-" * 62)

        baseline_bytes = None
        for tolerance in SIMPLIFY_TOLERANCES_M:
            lines = simplify_lines(raw, tolerance)
            vertices = count_vertices(lines)
            name = f"contours-{interval_ft}ft-t{tolerance:g}.svg".replace(".0", "")
            svg_path = OUTPUT_DIR / name
            size = write_svg(lines, extent, svg_path)

            if baseline_bytes is None:
                baseline_vertices, baseline_bytes = vertices, size
            print(
                f"  {tolerance:>8.0f}m  {len(lines):>7}  {vertices:>10,}  "
                f"{100 * vertices / baseline_vertices:>6.1f}%  "
                f"{size:>10,}  {100 * size / baseline_bytes:>6.1f}%"
            )

            caption = (
                f"{interval_ft} ft contours, {tolerance:g} m simplify tolerance, "
                f"{len(lines)} lines, {size / 1024:.0f} KB"
            )

            if tolerance == recommended_tolerance:
                for viewport in PREVIEW_VIEWPORTS:
                    out = OUTPUT_DIR / f"preview-{interval_ft}ft-{viewport[0]}.png"
                    render_preview(lines, extent, viewport, out, caption)
                    previews.append(out)

            # The whole simplification argument is that the tradeoff is
            # invisible at background opacity. Tabulating bytes doesn't
            # demonstrate that; rendering every tolerance at real conditions
            # does. Only for the interval under consideration -- one sweep,
            # not three.
            if interval_ft == SWEEP_INTERVAL_FEET:
                out = OUTPUT_DIR / f"sweep-{interval_ft}ft-t{tolerance:g}-desktop.png"
                render_preview(lines, extent, PREVIEW_VIEWPORTS[0], out, caption)
                sweeps.append(out)
        print()

    print("=" * 78)
    print("PREVIEWS")
    print("=" * 78)
    for path in previews:
        print(f"  {path}  ({os.path.getsize(path) / 1024:.0f} KB)")
    if sweeps:
        print()
        print(f"  tolerance sweep at {SWEEP_INTERVAL_FEET} ft, desktop crop:")
        for path in sweeps:
            print(f"    {path}  ({os.path.getsize(path) / 1024:.0f} KB)")
    print()
    print(f"All output in {OUTPUT_DIR}. Nothing committed.")


if __name__ == "__main__":
    main()
