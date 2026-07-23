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
-- the top-ranked candidate), and hydrology_data.py (real NHD streams, for
background context only -- no soil/hydrology POLYGON data is drawn here,
that's covered in the narrative text).

    boundary --> dem_data (fetched once, shared across all four layers)
             --> production_area_ceiling.identify_optimized_production_areas
             --> water_suitability.fetch_and_select_optimal_water_zone
             --> road_corridors.fetch_and_select_optimal_road_corridor
             --> solar_suitability.fetch_and_select_optimal_structure_site
             --> hydrology_data.get_water_features_for_boundary (streams)
             --> rendered PNG (basemap + halo + streams + boundary +
                 layout layers + numbered legend box, all one image)

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

matplotlib.use("Agg")  # headless rendering -- no display server in this pipeline's runtime
import matplotlib.pyplot as plt
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import Polygon, box, shape
from shapely.ops import unary_union
from shapely.plotting import plot_line, plot_points, plot_polygon

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
    """
    if dem is None:
        dem = get_dem_for_boundary(boundary_coordinates)

    production_result = identify_optimized_production_areas(boundary_coordinates, dem=dem)
    water_zone = fetch_and_select_optimal_water_zone(boundary_coordinates, dem=dem)
    road_corridor = fetch_and_select_optimal_road_corridor(boundary_coordinates, dem=dem)
    structure_site = fetch_and_select_optimal_structure_site(boundary_coordinates, dem=dem)
    water_features = get_water_features_for_boundary(boundary_coordinates)

    return {
        "dem": dem,
        "production_result": production_result,
        "water_zone": water_zone,
        "road_corridor": road_corridor,
        "structure_site": structure_site,
        "water_features": water_features,
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

    production_result = layers["production_result"]
    water_zone = layers["water_zone"]
    road_corridor = layers["road_corridor"]
    structure_site = layers["structure_site"]
    water_features = layers["water_features"]

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
        cx.add_basemap(ax, source=NAIP_TILE_URL_TEMPLATE, crs=WEB_MERCATOR, attribution=False)
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
        geom = _reproject_geometry_to_mercator(patch["geometry_wgs84"])
        polygons = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        for polygon in polygons:
            plot_polygon(
                polygon,
                ax=ax,
                add_points=False,
                facecolor=PRODUCTION_ZONE_COLOR,
                edgecolor=PRODUCTION_ZONE_COLOR,
                alpha=0.35,
                linewidth=1.5,
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
