"""
render_layout_map.py

Renders a single, high-resolution static map image of a property's final
proposed layout -- the visual counterpart to the narrative Scale of
Permanence report, meant to be the last page of the assembled PDF (see
generate_pdf_report.py). This module does not identify, score, or select
anything itself: every layer it draws is the already-computed output of
production_area_ceiling.py (optimized production zones),
water_suitability.py (fetch_and_select_optimal_water_zone -- the top-ranked
candidate), road_corridors.py (fetch_and_select_optimal_road_corridor -- the
top-ranked candidate), solar_suitability.py (fetch_and_select_optimal_structure_site
-- the top-ranked candidate), hydrology_data.py (real NHD streams, for
background context only -- no soil/hydrology POLYGON data is drawn here,
that's covered in the narrative text), and contour_lines.py (global
elevation contour lines over the full DEM extent).

    boundary --> dem_data (fetched once, shared across all four layers)
             --> production_area_ceiling.identify_optimized_production_areas
             --> water_suitability.fetch_and_select_optimal_water_zone
             --> road_corridors.fetch_and_select_optimal_road_corridor
             --> solar_suitability.fetch_and_select_optimal_structure_site
             --> hydrology_data.get_water_features_for_boundary (streams)
             --> contour_lines.compute_contour_lines (global, unclipped)
             --> rendered PNG (basemap + halo + streams + boundary +
                 layout layers + numbered legend box, all one image)

PRODUCTION ZONE STYLE: production zones render as CONTOUR-LINE TEXTURE,
not a filled/outlined shape -- contour_lines.py's global contour lines
(computed once, over the DEM's full extent) are clipped per zone at
render time (real shapely intersection against that zone's own
polygon_utm, not a pre-clipped raster), and only the clipped segments
within that zone are drawn. No fill, no boundary stroke for production
zones -- zone identity is conveyed by the numbered marker alone, same as
every other layer. This is a deliberate, scoped styling split: water,
road corridors, structure sites, and the property boundary all keep
their existing solid fill/line rendering exactly as before.

Basemap: NAIP aerial imagery via USGS's cached USGSImageryOnly tile
service, fetched and composited with contextily (a well-established
static-basemap library built for exactly this -- fetch+stitch XYZ tiles
into a georeferenced raster behind a matplotlib axes). Tiles are plain
{z}/{y}/{x} ArcGIS REST tiles, assumed (correctly, for this service) to be
in Web Mercator (EPSG:3857) -- see NAIP_TILE_URL_TEMPLATE below.

Output resolution: 300 DPI at US Letter portrait (8.5in x 11in), i.e.
2550x3300px -- print-quality for the full-bleed final page of the PDF
report (see generate_pdf_report.py), and consistent with the page size
the rest of that document renders at.
"""

import math
from typing import Optional

import contextily as cx
import matplotlib
import mercantile
import requests
import xyzservices

matplotlib.use("Agg")  # headless rendering -- no display server in this pipeline's runtime
import matplotlib.pyplot as plt
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely.plotting import plot_line, plot_points, plot_polygon

from contour_lines import compute_contour_lines
from dem_data import get_dem_for_boundary
from hydrology_data import get_water_features_for_boundary
from production_area_ceiling import identify_optimized_production_areas
from road_corridors import fetch_and_select_optimal_road_corridor
from solar_suitability import fetch_and_select_optimal_structure_site
from water_suitability import fetch_and_select_optimal_water_zone

WGS84 = "EPSG:4326"
WEB_MERCATOR = "EPSG:3857"

# ArcGIS REST tile path is literally .../tile/{z}/{y}/{x} -- contextily
# substitutes the {x}/{y}/{z} tokens wherever they appear in the string,
# so this works regardless of their order in the URL path.
NAIP_TILE_URL_TEMPLATE = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"

# Confirmed directly against this service's own metadata
# (https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer?f=json):
# tileInfo.lods only defines levels 0-23 -- level 24+ doesn't exist on this
# service and 404s. contextily's own auto-zoom (zoom='auto') has no way to
# know that on its own when `source` is passed as a bare URL string --
# _process_source() wraps a bare string into a TileProvider with no
# max_zoom set, so contextily's own zoom validation falls back to an
# "unknown max, allow up to 30" default and lets an over-high computed
# zoom through uncapped (confirmed live: a small property's extent
# produced zoom 27, a real 404 against this service). Passing `source` as
# an xyzservices.TileProvider with an explicit max_zoom (below) instead of
# a bare string makes contextily's OWN _validate_zoom() clip any
# auto-computed zoom that exceeds this service's real maximum, the same
# way it already does for providers whose max_zoom ships in their own
# metadata -- the extent-based auto calculation itself (small property ->
# higher zoom, large property -> lower zoom) is untouched; only the
# ceiling is now enforced. This must be a real TileProvider, not a plain
# dict: contextily's tile-fetch path calls provider.build_url(...), which
# only TileProvider (not a plain dict, even one with the right keys)
# implements.
#
# tileInfo.lods' 0-23 range is this service's OVERALL documented
# capability, not what's actually cached for any specific point --
# confirmed live: a real rural property's actual cache tops out at zoom
# 16, with every level 17-23 returning an identical tiny 404 placeholder
# (572 bytes), not real image data. The majority of this service's
# coverage is 1m NAIP, not the 6-inch imagery that reaches deeper zooms
# in some areas, so real cache depth varies by location and can't be
# assumed from the documented max alone. See _probe_max_available_zoom()
# below, which live-probes the actual property's own centroid tile by
# tile, starting from this documented max (or the extent-appropriate
# auto zoom, whichever is lower) and stepping down until a genuine tile
# is found -- NAIP_SERVICE_MAX_ZOOM here remains the correct, necessary
# ceiling that starting point can never exceed.
NAIP_SERVICE_MAX_ZOOM = 23

NAIP_TILE_SOURCE = xyzservices.TileProvider(
    name="USGSImageryOnly",
    url=NAIP_TILE_URL_TEMPLATE,
    max_zoom=NAIP_SERVICE_MAX_ZOOM,
    attribution="",
)

# Real "not found" tiles from this service come back as a tiny, fixed-size
# placeholder (confirmed live: 572 bytes) rather than a clean HTTP error in
# every case -- a genuine NAIP tile is tens of KB or more, so this
# threshold sits comfortably between the two with real margin, not a
# hair-trigger cutoff.
MIN_VALID_TILE_BYTES = 4096

# Floor for the zoom-availability probe below: past this point, an empty
# cache says something else is wrong (wrong tile scheme, wrong host, a
# genuinely uncovered region) rather than "just needs a lower zoom," so
# _probe_max_available_zoom() raises instead of continuing to step down.
MIN_PROBE_ZOOM = 10

TILE_PROBE_TIMEOUT_SECONDS = 10.0


def _extent_based_auto_zoom(west: float, south: float, east: float, north: float) -> int:
    """
    Mirrors contextily's own zoom='auto' calculation exactly (see
    contextily.tile._calculate_zoom) -- reimplemented directly here rather
    than importing that private, underscore-prefixed function, since this
    module needs the raw auto-computed value BEFORE calling
    add_basemap() (as the real-tile-availability probe's starting point/
    ceiling below), not just as an opaque 'auto' string handed to
    contextily itself. west/south/east/north are plain lon/lat degrees.
    """
    lon_length = abs(east - west)
    lat_length = abs(north - south)
    zoom_lon = math.ceil(math.log2(360 * 2.0 / lon_length))
    zoom_lat = math.ceil(math.log2(360 * 2.0 / lat_length))
    return min(zoom_lon, zoom_lat)


def _is_real_tile_response(response: "requests.Response") -> bool:
    """A genuine tile: HTTP 200, real image content-type, and a real
    payload size -- this service's own 404s can otherwise look
    deceptively like a normal response (a small, fixed-size placeholder
    body), so status code alone is not a reliable enough check here."""
    if response.status_code != 200:
        return False
    content_type = response.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        return False
    return len(response.content) >= MIN_VALID_TILE_BYTES


def _probe_max_available_zoom(
    centroid_lon: float,
    centroid_lat: float,
    ceiling_zoom: int,
    tile_url_template: str = NAIP_TILE_URL_TEMPLATE,
    min_zoom: int = MIN_PROBE_ZOOM,
) -> int:
    """
    Live-probes this specific property's own real tile cache depth,
    starting at ceiling_zoom (the lower of this service's documented max
    and the extent-appropriate auto zoom -- this function only ever
    steps DOWN from that ceiling, never above it) and fetching one real
    tile at a time at the property's own centroid until a genuine tile is
    found (see _is_real_tile_response()) or min_zoom is reached.

    Raises RuntimeError if no real tile is found down to min_zoom --
    at that point something else is wrong (wrong host/scheme, a
    genuinely uncovered region), not "just needs a lower zoom," and
    should surface as a real failure rather than loop indefinitely. The
    caller (render_layout_map()) already wraps basemap fetching in a
    try/except that degrades gracefully, same as every other
    network-backed layer in this pipeline.
    """
    zoom = ceiling_zoom
    last_zoom_tried = None
    last_status = None
    last_size = None
    while zoom >= min_zoom:
        tile = mercantile.tile(centroid_lon, centroid_lat, zoom)
        url = tile_url_template.format(z=tile.z, y=tile.y, x=tile.x)
        response = requests.get(url, timeout=TILE_PROBE_TIMEOUT_SECONDS)
        if _is_real_tile_response(response):
            return zoom
        last_zoom_tried, last_status, last_size = zoom, response.status_code, len(response.content)
        zoom -= 1
    raise RuntimeError(
        f"No real NAIP tile found for this property down to zoom {min_zoom} "
        f"(last attempt: zoom {last_zoom_tried}, status {last_status}, {last_size} bytes)"
    )

# How far past the property boundary's own bounding box the halo/context
# view extends, as a fraction of the boundary's larger dimension -- gives
# the muted "outside the property" band room to actually read as a band,
# and leaves a bit of surrounding imagery for geographic context.
CONTEXT_PADDING_FRACTION = 0.35

HALO_COLOR = "white"
HALO_ALPHA = 0.55

FIGURE_SIZE_INCHES = (8.5, 11)  # US Letter, portrait -- matches the report's page size
OUTPUT_DPI = 300

STREAM_COLOR = "#3B82C4"
BOUNDARY_COLOR = "#1A1A1A"
PRODUCTION_ZONE_COLOR = "#4C9A2A"
PRODUCTION_ZONE_CONTOUR_LINEWIDTH = 0.7
WATER_ZONE_COLOR = "#1F6FB2"
ROAD_CORRIDOR_COLOR = "#B5651D"
STRUCTURE_SITE_COLOR = "#D64545"

MARKER_FACE_COLOR = "#1A1A1A"
MARKER_TEXT_COLOR = "white"
MARKER_RADIUS_POINTS = 11


def _reproject_geometry_to_mercator(geometry_wgs84: dict):
    """geometry_wgs84 is a GeoJSON geometry dict in WGS84 (the shape every
    layer module in this pipeline already produces) -- reprojects it to
    Web Mercator (matching the basemap tiles' own CRS) and returns it as a
    shapely geometry, ready to draw directly on the map axes."""
    geometry_3857 = transform_geom(WGS84, WEB_MERCATOR, geometry_wgs84)
    return shape(geometry_3857)


def _reproject_utm_geometry_to_mercator(geometry_utm, source_crs: str):
    """geometry_utm is a shapely geometry already in `source_crs` (a
    DEM's own UTM zone, e.g. contour_lines.py's lines_utm clipped against
    a production zone's own polygon_utm -- both already share that same
    CRS, so no WGS84 round-trip is needed first) -- reprojects directly
    to Web Mercator (one hop) and returns it as a shapely geometry, ready
    to draw."""
    geometry_3857 = transform_geom(source_crs, WEB_MERCATOR, mapping(geometry_utm))
    return shape(geometry_3857)


def _iter_line_parts(geometry):
    """Yields real LineString parts out of a LineString/MultiLineString/
    GeometryCollection -- shapely's intersection() of a (Multi)LineString
    against a (Multi)Polygon can legitimately come back as any of those,
    or mix in degenerate Point touches where a line only grazes the
    polygon's boundary; only real line segments are worth drawing."""
    if geometry.is_empty:
        return
    if geometry.geom_type == "LineString":
        yield geometry
    elif geometry.geom_type == "MultiLineString":
        yield from geometry.geoms
    elif geometry.geom_type == "GeometryCollection":
        for part in geometry.geoms:
            yield from _iter_line_parts(part)


def _boundary_polygon_mercator(boundary_coordinates: list[tuple[float, float]]) -> Polygon:
    lons = [pt[0] for pt in boundary_coordinates]
    lats = [pt[1] for pt in boundary_coordinates]
    xs, ys = warp_transform(WGS84, WEB_MERCATOR, lons, lats)
    return Polygon(zip(xs, ys))


def _draw_numbered_marker(ax, point, number: int) -> None:
    ax.annotate(
        str(number),
        xy=(point.x, point.y),
        xycoords="data",
        ha="center",
        va="center",
        fontsize=10,
        fontweight="bold",
        color=MARKER_TEXT_COLOR,
        zorder=50,
        bbox=dict(boxstyle="circle,pad=0.35", facecolor=MARKER_FACE_COLOR, edgecolor="white", linewidth=1.2),
    )


def _production_zone_legend_stats(production_result: dict) -> list[tuple[float, str]]:
    """Returns [(area_acres, stat_line), ...] -- one per surviving
    production patch, in the same order production_result['scored_patches']
    already ranks them. parcel_acres is back-derived from the pipeline's
    own reported total_selected_acreage / percent_of_parcel (both already
    computed by production_area_ceiling.py) rather than recomputed here,
    so this stays purely a display-formatting step."""
    scored_patches = production_result["scored_patches"]
    percent_of_parcel = production_result.get("percent_of_parcel") or 0.0
    total_selected_acreage = production_result.get("total_selected_acreage") or 0.0
    parcel_acres = (total_selected_acreage / (percent_of_parcel / 100.0)) if percent_of_parcel else None

    stats = []
    for patch in scored_patches:
        pct_note = (
            f" ({round(patch['area_acres'] / parcel_acres * 100)}% of parcel)"
            if parcel_acres
            else ""
        )
        stats.append((patch["area_acres"], f"{patch['area_acres']} ac{pct_note}"))
    return stats


def fetch_layout_layers(boundary_coordinates: list[tuple[float, float]], dem: Optional[dict] = None) -> dict:
    """
    Fetches/derives every layer render_layout_map() draws. Fetches the DEM
    once (unless one is passed in) and shares it across all four
    optimize/select calls below, rather than letting each independently
    re-fetch the same DEM -- a pure efficiency choice, doesn't change any
    of their identification/scoring/selection logic.

    water_zone reuses identify_water_suitability()'s own selected_water_zone
    field directly (see fetch_and_select_optimal_water_zone()) -- that field
    and this function's return value are the exact same scored-zone dict
    shape, so there was real duplicate "pick the best" logic to remove.
    road_corridor/structure_site, by contrast, are read here as GeoJSON
    Features (via identify_road_corridor_candidates()'s/
    identify_solar_candidate_zones()'s own zones_geojson, same as every
    other GeoJSON-shaped input this module reprojects with
    _reproject_geometry_to_mercator()) rather than their identify_*()'s
    newer selected_road_corridor/selected_structure_site fields -- those
    are a DIFFERENT shape (raw scored dicts carrying UTM shapely geometry:
    line_utm/polygon_utm, un-reprojected, 0-1 scores), so using them here
    would mean adding one-off reprojection/property-translation code for
    each, for zero fetch-count benefit (both paths still call the same
    identify_*() exactly once). Left as GeoJSON for now; worth
    reconsidering only if a future caller needs the raw UTM geometry too.

    contour_lines is contour_lines.compute_contour_lines()'s own output --
    GLOBAL elevation contour lines over the DEM's full extent, computed
    ONCE here and shared across every production zone at render time
    (each zone clips its own segments out of this same list via real
    shapely intersection against its own polygon_utm -- see
    render_layout_map()'s own docstring), rather than recomputing contour
    lines per zone.
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    production_result = identify_optimized_production_areas(boundary_coordinates, dem=dem)
    water_zone = fetch_and_select_optimal_water_zone(boundary_coordinates, dem=dem)
    road_corridor = fetch_and_select_optimal_road_corridor(boundary_coordinates, dem=dem)
    structure_site = fetch_and_select_optimal_structure_site(boundary_coordinates, dem=dem)
    water_features = get_water_features_for_boundary(boundary_coordinates)
    contour_lines = compute_contour_lines(dem)

    return {
        "dem": dem,
        "production_result": production_result,
        "water_zone": water_zone,
        "road_corridor": road_corridor,
        "structure_site": structure_site,
        "water_features": water_features,
        "contour_lines": contour_lines,
    }


def render_layout_map(
    boundary_coordinates: list[tuple[float, float]],
    output_path: str,
    dem: Optional[dict] = None,
    layers: Optional[dict] = None,
) -> str:
    """
    Renders the final proposed layout as a single high-resolution PNG at
    output_path and returns that same path.

    layers (optional): a pre-fetched fetch_layout_layers() result, so a
    caller that already fetched the DEM/layers for other purposes (e.g.
    generate_pdf_report.py, which also needs them alongside the narrative
    report's own data fetches) doesn't pay for a second, redundant fetch.
    Omit it (default) to have this function fetch everything itself.
    """
    if layers is None:
        layers = fetch_layout_layers(boundary_coordinates, dem=dem)

    dem = layers["dem"]
    production_result = layers["production_result"]
    water_zone = layers["water_zone"]
    road_corridor = layers["road_corridor"]
    structure_site = layers["structure_site"]
    water_features = layers["water_features"]
    contour_lines = layers["contour_lines"]

    boundary_polygon = _boundary_polygon_mercator(boundary_coordinates)
    minx, miny, maxx, maxy = boundary_polygon.bounds
    pad = CONTEXT_PADDING_FRACTION * max(maxx - minx, maxy - miny)
    context_box = box(minx - pad, miny - pad, maxx + pad, maxy + pad)

    # The halo mask itself: the real geometric difference between the
    # padded context box and the boundary polygon (shapely.difference(),
    # the same operation the rest of this pipeline already uses for
    # exclusion geometry) -- not a crop or a vignette.
    halo_mask = context_box.difference(boundary_polygon)

    fig, ax = plt.subplots(figsize=FIGURE_SIZE_INCHES)
    ax.set_xlim(minx - pad, maxx + pad)
    ax.set_ylim(miny - pad, maxy + pad)
    ax.set_aspect("equal")
    ax.set_axis_off()

    try:
        corner_lons, corner_lats = warp_transform(
            WEB_MERCATOR, WGS84, [minx - pad, maxx + pad], [miny - pad, maxy + pad]
        )
        west, east = corner_lons
        south, north = corner_lats
        auto_zoom = _extent_based_auto_zoom(west, south, east, north)
        ceiling_zoom = min(auto_zoom, NAIP_SERVICE_MAX_ZOOM)

        centroid = Polygon(boundary_coordinates).representative_point()
        resolved_zoom = _probe_max_available_zoom(centroid.x, centroid.y, ceiling_zoom)
        print(f"  Basemap zoom: extent-appropriate ceiling {ceiling_zoom}, real available zoom {resolved_zoom}")

        cx.add_basemap(ax, source=NAIP_TILE_SOURCE, zoom=resolved_zoom, crs=WEB_MERCATOR, attribution=False)
        basemap_note = None
    except Exception as e:
        # A NAIP tile-service outage or network restriction shouldn't
        # crash PDF generation -- same fetch-degrades-gracefully
        # reasoning as every other network-backed layer in this pipeline
        # (imagery_data.py, water_candidate_zones.py, etc). Falls back to
        # a neutral fill (drawn as an explicit patch, not ax.set_facecolor()
        # -- ax.set_axis_off() below suppresses the axes' own background
        # patch) so every other layer, including the halo overlay, still
        # renders with real visible contrast against something.
        plot_polygon(context_box, ax=ax, add_points=False, facecolor="#DCD8CE", edgecolor="none", zorder=1)
        basemap_note = f"basemap unavailable ({e})"

    # z-order, back to front, per spec: halo mask, streams, perimeter
    # boundary, production zone(s), water zone, road corridor, structure site.
    if not halo_mask.is_empty:
        plot_polygon(halo_mask, ax=ax, add_points=False, facecolor=HALO_COLOR, edgecolor="none", alpha=HALO_ALPHA, zorder=10)

    for stream in water_features.get("streams", []):
        if not stream.get("geometry"):
            continue
        stream_geom = _reproject_geometry_to_mercator(stream["geometry"])
        if stream_geom.geom_type == "LineString":
            plot_line(stream_geom, ax=ax, add_points=False, color=STREAM_COLOR, linewidth=1.0, alpha=0.8, zorder=20)
        elif stream_geom.geom_type == "MultiLineString":
            for line in stream_geom.geoms:
                plot_line(line, ax=ax, add_points=False, color=STREAM_COLOR, linewidth=1.0, alpha=0.8, zorder=20)

    plot_polygon(
        boundary_polygon, ax=ax, add_points=False, facecolor="none", edgecolor=BOUNDARY_COLOR, linewidth=2.2, zorder=30
    )

    legend_entries: list[str] = []
    marker_number = 1

    scored_patches = production_result.get("scored_patches", []) if production_result else []
    zone_stats = _production_zone_legend_stats(production_result) if production_result else []
    multiple_zones = len(scored_patches) > 1
    for patch, (_, stat_line) in zip(scored_patches, zone_stats):
        # geometry_wgs84 -- the real, grid-bug-fixed cell-union footprint
        # (see production_area.py's own module docstring) -- used here
        # only for label placement; no fill, no boundary stroke drawn for
        # production zones (see this module's own docstring). Real zone
        # geometry is what clips the contour lines below, too
        # (patch["polygon_utm"], same CRS as contour_lines' lines_utm --
        # no reprojection needed before intersecting).
        geom = _reproject_geometry_to_mercator(patch["geometry_wgs84"])

        for contour in contour_lines:
            clipped = contour["lines_utm"].intersection(patch["polygon_utm"])
            if clipped.is_empty:
                continue
            for line in _iter_line_parts(_reproject_utm_geometry_to_mercator(clipped, dem["crs"])):
                plot_line(
                    line,
                    ax=ax,
                    add_points=False,
                    color=PRODUCTION_ZONE_COLOR,
                    linewidth=PRODUCTION_ZONE_CONTOUR_LINEWIDTH,
                    alpha=0.85,
                    zorder=40,
                )

        label = f"Production Zone {patch['rank']}" if multiple_zones else "Production Zone"
        _draw_numbered_marker(ax, geom.representative_point(), marker_number)
        legend_entries.append(f"{marker_number} — {label}, {stat_line}")
        marker_number += 1

    if water_zone is not None:
        geom = _reproject_geometry_to_mercator(water_zone["geometry_wgs84"])
        polygons = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        for polygon in polygons:
            plot_polygon(
                polygon,
                ax=ax,
                add_points=False,
                facecolor=WATER_ZONE_COLOR,
                edgecolor=WATER_ZONE_COLOR,
                alpha=0.35,
                linewidth=1.5,
                zorder=41,
            )
        _draw_numbered_marker(ax, geom.representative_point(), marker_number)
        legend_entries.append(
            f"{marker_number} — Water System, Valley {water_zone['valley_id']}, "
            f"score {water_zone['suitability_score']}"
        )
        marker_number += 1

    if road_corridor is not None:
        geom = _reproject_geometry_to_mercator(road_corridor["geometry"])
        props = road_corridor["properties"]
        plot_line(geom, ax=ax, add_points=False, color=ROAD_CORRIDOR_COLOR, linewidth=3.0, zorder=42)
        _draw_numbered_marker(ax, geom.interpolate(0.5, normalized=True), marker_number)
        if props.get("connection_point_is_arbitrary"):
            anchor_note = "arbitrary boundary anchor"
        elif props.get("anchor_road_name"):
            anchor_note = f"anchored to {props['anchor_road_name']}"
        else:
            anchor_note = "anchored to a real road"
        legend_entries.append(
            f"{marker_number} — Road Corridor, {props['corridor_type']}, {anchor_note}"
        )
        marker_number += 1

    if structure_site is not None:
        geom = _reproject_geometry_to_mercator(structure_site["geometry"])
        props = structure_site["properties"]
        plot_polygon(
            geom,
            ax=ax,
            add_points=False,
            facecolor=STRUCTURE_SITE_COLOR,
            edgecolor=STRUCTURE_SITE_COLOR,
            alpha=0.55,
            linewidth=1.5,
            zorder=43,
        )
        _draw_numbered_marker(ax, geom.representative_point(), marker_number)
        legend_entries.append(
            f"{marker_number} — Structure Site, score {props['suitability_score']}"
        )
        marker_number += 1

    ax.set_title("Proposed Farm Layout", fontsize=16, fontweight="bold", pad=14)

    if legend_entries:
        legend_text = "\n".join(legend_entries)
        ax.text(
            0.02,
            0.02,
            legend_text,
            transform=ax.transAxes,
            fontsize=8.5,
            va="bottom",
            ha="left",
            zorder=60,
            bbox=dict(boxstyle="round,pad=0.6", facecolor="white", edgecolor="#333333", alpha=0.9),
        )

    if basemap_note:
        ax.text(
            0.02,
            0.98,
            "Aerial basemap unavailable for this render",
            transform=ax.transAxes,
            fontsize=7,
            va="top",
            ha="left",
            color="#883333",
            zorder=60,
            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor="#883333", alpha=0.85),
        )

    fig.tight_layout()
    fig.savefig(output_path, dpi=OUTPUT_DPI)
    plt.close(fig)

    return output_path


if __name__ == "__main__":
    # The user's real, drawn property boundary -- same one every other
    # module's own __main__ block uses.
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    print("Rendering static layout map for property boundary...\n")

    try:
        path = render_layout_map(property_boundary, "layout_map.png")
        print(f"Wrote {path}")
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map services (DEM/NHD) and the USGSImageryOnly tile service -- "
            "not a fully sandboxed environment."
        )
