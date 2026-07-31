"""
diagnose_water_zone_mask.py

Standalone, read-only diagnostic: reports pre- and post-dilation stats for
compute_water_eligible_cells()'s drainage-only mask (water_candidate_zones.py)
against the real reference property boundary -- the same coordinates
render_layout_map.py's own __main__ block uses.

This does NOT modify compute_water_eligible_cells() itself, and does not
reimplement its flow-accumulation-threshold/dilation logic independently
-- it calls the exact same building blocks that function already uses
internally (valley_delineation.get_flow_accumulation_for_dem(),
water_candidate_zones.MIN_VALLEY_CONTRIBUTING_AREA_ACRES,
water_candidate_zones._survey_buffer_radius_cells(),
raster_grid.binary_dilate()) in the same order, just stopping to report
stats BEFORE and AFTER the dilation step rather than continuing on into
the service-distance/boundary-setback tests and full eligibility mask.

Requires real network access (a real USGS DEM fetch via dem_data.py, plus
production_area.py's own SSURGO/canopy/road fetches) -- this is a live
diagnostic against a real property, not the offline/synthetic-DEM tests
in test_water_candidate_zones.py.

--min-contributing-acres and --buffer-meters override
MIN_VALLEY_CONTRIBUTING_AREA_ACRES / WATER_ZONE_SURVEY_BUFFER_METERS for
this run only (default: the current module constants) -- for
experimentation while tuning either value; the actual module constants
themselves are never changed.
"""

import argparse

from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon

from dem_data import get_dem_for_boundary
from production_area import identify_production_areas
from raster_grid import binary_dilate, cell_area_acres, connected_components
from valley_delineation import get_flow_accumulation_for_dem
from water_candidate_zones import (
    MIN_VALLEY_CONTRIBUTING_AREA_ACRES,
    WATER_ZONE_SURVEY_BUFFER_METERS,
    _survey_buffer_radius_cells,
)

# The user's real, drawn property boundary -- same one render_layout_map.py's
# own __main__ block (and every other module's own __main__ block) uses.
PROPERTY_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]


def _report_mask_stats(label: str, mask, dem: dict) -> None:
    cell_count = int(mask.sum())
    acres = cell_count * cell_area_acres(dem)
    _, num_components = connected_components(mask)
    print(f"--- {label} ---")
    print(f"  Qualifying cell count: {cell_count}")
    print(f"  Acreage:               {acres:.3f} acres")
    print(f"  Connected components:  {num_components}")
    print()


def main(
    min_contributing_acres: float = MIN_VALLEY_CONTRIBUTING_AREA_ACRES,
    buffer_meters: float = WATER_ZONE_SURVEY_BUFFER_METERS,
) -> None:
    print("diagnose_water_zone_mask.py -- pre/post survey-buffer-dilation mask stats\n")
    print(f"Property: real reference boundary, {len(PROPERTY_BOUNDARY)} vertices")
    print(f"Boundary coordinates (lon, lat): {PROPERTY_BOUNDARY}\n")
    print(f"min_contributing_acres (this run) = {min_contributing_acres} acres "
          f"(module default: {MIN_VALLEY_CONTRIBUTING_AREA_ACRES})")
    print(f"buffer_meters (this run)          = {buffer_meters}m "
          f"(module default: {WATER_ZONE_SURVEY_BUFFER_METERS})\n")

    dem = get_dem_for_boundary(PROPERTY_BOUNDARY)
    print(f"DEM fetched: {dem['array'].shape[0]}x{dem['array'].shape[1]} cells, "
          f"{dem['resolution_meters'][0]}m resolution, crs={dem['crs']}\n")

    boundary_xs, boundary_ys = warp_transform(
        "EPSG:4326",
        dem["crs"],
        [pt[0] for pt in PROPERTY_BOUNDARY],
        [pt[1] for pt in PROPERTY_BOUNDARY],
    )
    boundary_polygon_utm = Polygon(zip(boundary_xs, boundary_ys))

    try:
        production_areas = identify_production_areas(dem, boundary_polygon_utm)
        print(f"Production areas found: {len(production_areas)} (context only -- the mask stats "
              "below don't depend on this)\n")
    except Exception as e:
        print(
            f"Production-area fetch failed ({e}) -- proceeding anyway, since the drainage-mask "
            "stats below only depend on the DEM's own flow accumulation, not production areas.\n"
        )

    # Same computation compute_water_eligible_cells() does internally, just
    # stopped short of the service-distance/boundary-setback loop so the
    # BEFORE/AFTER dilation stats can be reported directly. Uses this run's
    # min_contributing_acres/buffer_meters (module constants unless
    # overridden via --min-contributing-acres/--buffer-meters).
    flow_accumulation_cells = get_flow_accumulation_for_dem(dem)
    area_per_cell = cell_area_acres(dem)
    min_contributing_cells = min_contributing_acres / area_per_cell
    drainage_mask_before = flow_accumulation_cells >= min_contributing_cells

    print(f"(min_contributing_acres = {min_contributing_acres} acres -> "
          f"{min_contributing_cells:.2f} cells at this DEM's resolution)\n")

    _report_mask_stats("BEFORE dilation (raw flow-accumulation-qualifying mask)", drainage_mask_before, dem)

    radius_cells = _survey_buffer_radius_cells(dem, buffer_meters)
    print(f"(buffer_meters = {buffer_meters}m -> dilation radius = {radius_cells} cell(s))\n")

    drainage_mask_after = binary_dilate(drainage_mask_before, radius_cells)
    _report_mask_stats("AFTER dilation (survey-buffered drainage mask)", drainage_mask_after, dem)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report pre/post survey-buffer-dilation drainage mask stats for the real reference property."
    )
    parser.add_argument(
        "--min-contributing-acres",
        type=float,
        default=MIN_VALLEY_CONTRIBUTING_AREA_ACRES,
        help=f"Override MIN_VALLEY_CONTRIBUTING_AREA_ACRES for this run only "
        f"(default: the current module constant, {MIN_VALLEY_CONTRIBUTING_AREA_ACRES}).",
    )
    parser.add_argument(
        "--buffer-meters",
        type=float,
        default=WATER_ZONE_SURVEY_BUFFER_METERS,
        help=f"Override WATER_ZONE_SURVEY_BUFFER_METERS for this run only "
        f"(default: the current module constant, {WATER_ZONE_SURVEY_BUFFER_METERS}).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        main(min_contributing_acres=args.min_contributing_acres, buffer_meters=args.buffer_meters)
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map ImageServer (DEM fetch) and production_area.py's own "
            "SSURGO/canopy/road data sources -- not a fully sandboxed environment."
        )
