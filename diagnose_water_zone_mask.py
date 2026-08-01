"""
diagnose_water_zone_mask.py

Standalone, read-only diagnostic: reports pre- and post-dilation stats for
compute_water_eligible_cells()'s drainage-only mask (water_candidate_zones.py),
then breaks the post-dilation mask down further by the real service-
distance/boundary-setback gates that function applies -- against the real
reference property boundary (the same coordinates render_layout_map.py's
own __main__ block uses).

This does NOT modify compute_water_eligible_cells() itself, and does not
reimplement any of its gate logic independently -- the pre/post-dilation
section calls the exact same building blocks that function already uses
internally (valley_delineation.get_flow_accumulation_for_dem(),
water_candidate_zones.MIN_VALLEY_CONTRIBUTING_AREA_ACRES,
water_candidate_zones._survey_buffer_radius_cells(),
raster_grid.binary_dilate()) in the same order; the gate-breakdown section
calls compute_water_eligible_cells() itself directly, several times, with
individual gate thresholds swapped for "always pass" values (0/infinity)
to isolate one real gate at a time -- the gate math itself is never
duplicated, only which of the function's own real checks can actually
bind is varied per call.

Every isolation call uses the REAL boundary_polygon_utm, never a
synthetic stand-in -- an earlier version of this section used a
synthetic boundary covering the DEM's entire extent (to make the
on-parcel/setback gate a no-op) for the service-distance isolation
calls specifically. That inadvertently broke the downstream-clearance
gate too: that gate checks for a confirmed exit from the SAME
boundary_polygon_utm argument, and a boundary large enough to contain
the whole DEM makes a confirmed exit structurally impossible for every
cell (a flow path only ever walks within the DEM's own grid). Fixed --
see the gate-breakdown section's own comment for the full regression
note. The on-parcel containment check has no override at all
(boundary_polygon_utm is required, not optional) -- only its SETBACK
distance can be disabled (min_boundary_setback_meters=0.0), so the
service-distance isolation below is "on-parcel required, setback
waived," not a pure isolation from every other gate.

Requires real network access (a real USGS DEM fetch via dem_data.py, plus
production_area.py's own SSURGO/canopy/road fetches) -- this is a live
diagnostic against a real property, not the offline/synthetic-DEM tests
in test_water_candidate_zones.py.

--min-contributing-acres and --buffer-meters override
MIN_VALLEY_CONTRIBUTING_AREA_ACRES / WATER_ZONE_SURVEY_BUFFER_METERS for
this run only (default: the current module constants) -- for
experimentation while tuning either value; the actual module constants
themselves are never changed. The service-distance/boundary-setback
thresholds used in the gate breakdown are always the module's real
current defaults (MAX_SERVICE_DISTANCE_METERS/MIN_SERVICE_DISTANCE_METERS/
MIN_BOUNDARY_SETBACK_METERS), not separately overridable here.
"""

import argparse

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Point, Polygon
from shapely.prepared import prep

from dem_data import get_dem_for_boundary
from production_area import identify_production_areas
from raster_grid import (
    binary_dilate,
    cell_area_acres,
    cell_union_footprint,
    connected_components,
    pixel_center_xy,
)
from valley_delineation import get_flow_accumulation_for_dem
from water_candidate_zones import (
    MAX_SERVICE_DISTANCE_METERS,
    MIN_BOUNDARY_SETBACK_METERS,
    MIN_DOWNSTREAM_CLEARANCE_METERS,
    MIN_SERVICE_DISTANCE_METERS,
    MIN_VALLEY_CONTRIBUTING_AREA_ACRES,
    WATER_ZONE_SURVEY_BUFFER_METERS,
    _survey_buffer_radius_cells,
    compute_water_eligible_cells,
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


def _report_cell_count(label: str, mask, dem: dict) -> None:
    """Same numbers as _report_mask_stats() minus the connected-component
    count -- used for the gate-isolation breakdown below, where component
    count isn't the useful signal (an isolated gate's survivor mask is a
    diagnostic intermediate, not a candidate zone shape)."""
    cell_count = int(mask.sum())
    acres = cell_count * cell_area_acres(dem)
    print(f"  {label}: {cell_count} cells, {acres:.3f} acres")


def main(
    min_contributing_acres: float = MIN_VALLEY_CONTRIBUTING_AREA_ACRES,
    buffer_meters: float = WATER_ZONE_SURVEY_BUFFER_METERS,
    downstream_clearance_meters: float = MIN_DOWNSTREAM_CLEARANCE_METERS,
    sweep_downstream: bool = False,
) -> None:
    print("diagnose_water_zone_mask.py -- pre/post survey-buffer-dilation mask stats\n")
    print(f"Property: real reference boundary, {len(PROPERTY_BOUNDARY)} vertices")
    print(f"Boundary coordinates (lon, lat): {PROPERTY_BOUNDARY}\n")
    print(f"min_contributing_acres (this run)      = {min_contributing_acres} acres "
          f"(module default: {MIN_VALLEY_CONTRIBUTING_AREA_ACRES})")
    print(f"buffer_meters (this run)               = {buffer_meters}m "
          f"(module default: {WATER_ZONE_SURVEY_BUFFER_METERS})")
    print(f"downstream_clearance_meters (this run) = {downstream_clearance_meters}m "
          f"(module default: {MIN_DOWNSTREAM_CLEARANCE_METERS}) -- ignored if --sweep-downstream is set\n")

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

    production_areas: list = []
    production_areas_available = False
    try:
        production_areas = identify_production_areas(dem, boundary_polygon_utm)
        production_areas_available = True
        print(f"Production areas found: {len(production_areas)} (the pre/post-dilation mask stats "
              "below don't depend on this, but the gate-breakdown section further down does)\n")
    except Exception as e:
        print(
            f"Production-area fetch failed ({e}) -- proceeding anyway for the pre/post-dilation "
            "mask stats (which only depend on the DEM's own flow accumulation), but the "
            "gate-breakdown section further down needs real production areas and will be skipped.\n"
        )

    print(f"Boundary polygon extent (UTM, {dem['crs']}): {boundary_polygon_utm.bounds}")
    if production_areas_available:
        for patch in production_areas:
            centroid = patch["polygon_utm"].centroid
            print(f"  Production area id={patch['id']}: centroid=({centroid.x:.1f}, {centroid.y:.1f})")
    print()

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

    # --- direct on-parcel vs boundary-setback split (unconditional, no ---
    # --- dependence on production areas at all) -- the gate-breakdown  ---
    # --- section further down only reports the COMBINED on-parcel-    ---
    # --- and-setback result; this checks each of those two tests      ---
    # --- separately, directly against boundary_polygon_utm itself, so ---
    # --- a "0 combined" result can't hide which of the two is actually ---
    # --- responsible.
    print("=== On-parcel vs boundary-setback (direct, separated check) ===\n")

    boundary_prepared = prep(boundary_polygon_utm)
    boundary_line = boundary_polygon_utm.boundary

    on_parcel_mask = np.zeros(drainage_mask_after.shape, dtype=bool)
    setback_survivor_mask = np.zeros(drainage_mask_after.shape, dtype=bool)

    for r, c in np.argwhere(drainage_mask_after):
        r, c = int(r), int(c)
        x, y = pixel_center_xy(dem, r, c)
        point = Point(x, y)
        if boundary_prepared.contains(point):
            on_parcel_mask[r, c] = True
            if boundary_line.distance(point) >= MIN_BOUNDARY_SETBACK_METERS:
                setback_survivor_mask[r, c] = True

    # 1. On-parcel alone, no setback applied.
    _report_mask_stats(
        "1. On-parcel cells within the dilated drainage mask (NO setback applied)",
        on_parcel_mask, dem,
    )

    # 2. Of those on-parcel cells, how many ALSO clear the real setback.
    _report_mask_stats(
        f"2. Of those, cells ALSO clearing MIN_BOUNDARY_SETBACK_METERS={MIN_BOUNDARY_SETBACK_METERS}m "
        "from the boundary",
        setback_survivor_mask, dem,
    )

    # 3. If on-parcel cells exist but NONE clear setback, show real
    #    boundary.distance() numbers instead of just a pass/fail count --
    #    5 sample cells, first 5 in raster order (np.argwhere()'s own
    #    row-major order).
    #
    #    BUG FIXED HERE: this used to sample from drainage_mask_after (the
    #    FULL dilated mask, on-parcel or not) instead of on_parcel_mask
    #    (the actual candidate pool setback_survivor_mask is drawn from).
    #    Since on_parcel_mask is typically a small subset of
    #    drainage_mask_after, the "first 5" cells in raster order were
    #    almost always OFF-parcel cells that were never even tested for
    #    setback at all -- their boundary.distance() values (measured to
    #    the boundary LINE, which says nothing about inside-vs-outside)
    #    could easily be large despite having nothing to do with why
    #    setback_survivor_mask came back empty, producing exactly the
    #    "large real distance, 0 survivors" contradiction this section
    #    exists to explain. Sampling on_parcel_mask directly means every
    #    printed cell is one setback_survivor_mask actually evaluated.
    if on_parcel_mask.any() and not setback_survivor_mask.any():
        print(
            "3. setback_survivor_mask is empty despite on-parcel cells existing -- "
            "sample boundary.distance() values (first 5 ON-PARCEL cells, raster order):"
        )
        sample_cells = np.argwhere(on_parcel_mask)[:5]
        for r, c in sample_cells:
            r, c = int(r), int(c)
            x, y = pixel_center_xy(dem, r, c)
            distance_to_boundary = boundary_line.distance(Point(x, y))
            print(
                f"     (row={r}, col={c}) UTM=({x:.2f}, {y:.2f}) "
                f"boundary_polygon_utm.boundary.distance() = {distance_to_boundary:.3f}m"
            )
        print()

    # 4. Direct negative-buffer sanity check -- runs UNCONDITIONALLY,
    #    regardless of what 1-3 found: if the boundary shrunk inward by
    #    15m is already empty, the parcel is narrower than 2x that setback
    #    everywhere, and NO cell could ever clear
    #    MIN_BOUNDARY_SETBACK_METERS=15.0m from this boundary at all --
    #    the cleanest possible explanation for a setback_survivor_mask
    #    that's empty even though on-parcel cells exist.
    shrunk_boundary = boundary_polygon_utm.buffer(-15.0)
    print(
        f"4. boundary_polygon_utm.buffer(-15.0).is_empty = {shrunk_boundary.is_empty}, "
        f".area = {shrunk_boundary.area:.3f} sq m"
    )
    print()

    if not production_areas_available:
        print(
            "Skipping the service-distance/boundary-setback gate breakdown below -- it needs real "
            "production areas, which failed to fetch above.\n"
        )
        return

    # Gate breakdown: every call below is the REAL compute_water_eligible_cells()
    # itself, just with individual gate thresholds swapped for "always pass"
    # values (0 / infinity) to isolate one real gate at a time. No gate logic
    # is reimplemented here.
    #
    # REGRESSION FIXED HERE: items 2/2a/2b used to pass a synthetic
    # "_full_extent_boundary()" (the DEM's own extent plus a 10,000m margin)
    # in place of boundary_polygon_utm, specifically to make the on-parcel/
    # setback gate (#3) a trivial no-op. That inadvertently ALSO broke the
    # downstream-clearance gate (#4) added since -- #4 checks for a confirmed
    # exit from that SAME boundary_polygon_utm argument, and a boundary large
    # enough to contain the entire DEM makes a confirmed exit structurally
    # IMPOSSIBLE for every single cell (a flow path only ever walks within the
    # DEM's own grid, which is always smaller than a 10,000m-margin box) --
    # min_downstream_clearance_meters=0.0 only relaxes gate #4's DISTANCE
    # threshold, not its separate "must actually confirm an exit" requirement,
    # so it could never rescue this. The result: service_distance_mask was
    # unconditionally empty regardless of real service distance, which made
    # BOTH too_far_mask and too_close_mask unconditionally empty too --
    # reporting "excluded as TOO FAR" and "excluded as TOO CLOSE" as the same
    # full drainage_mask_after count simultaneously, which cannot be true for
    # a real per-cell distance test (confirmed directly: the same candidate
    # cell that never registers a confirmed exit against the huge boundary
    # exits cleanly, with real distance, against the REAL boundary_polygon_utm).
    #
    # Fix: items 2/2a/2b now use the REAL boundary_polygon_utm (same as items
    # 1 and 3) with min_boundary_setback_meters=0.0 -- this disables gate #3's
    # SETBACK distance specifically (its only overridable knob) while leaving
    # on-parcel containment intact, since on-parcel-ness can't be waived
    # without breaking gate #4 the same way the old synthetic boundary did.
    # These three items are therefore "service-distance + downstream-
    # clearance, on-parcel required, setback waived" rather than pure
    # service-distance isolation -- the closest isolation achievable without
    # the same structural conflict. Items 1/2/2a/2b all still pass
    # min_downstream_clearance_meters=0.0 to keep gate #4's THRESHOLD out of
    # these numbers as much as possible; its "must confirm an exit"
    # requirement remains (unavoidable via any parameter), so a real property
    # where a candidate's flow path never confirms an exit at all would still
    # depress these numbers somewhat -- a real, bounded caveat now, not the
    # unconditional 100% failure the old synthetic boundary caused. Item 3
    # ("ALL FOUR gates combined") uses the real downstream_clearance_meters
    # value, since it's meant to match find_candidate_zones()'s own real,
    # complete output.
    print("=== Service-distance / boundary-setback gate breakdown (post-dilation mask) ===\n")

    # 1. On-parcel + boundary-setback gate ALONE (service-distance disabled
    #    via max=infinity/min=0, using the REAL property boundary/setback).
    parcel_setback_mask, _ = compute_water_eligible_cells(
        dem, production_areas, boundary_polygon_utm,
        min_valley_contributing_area_acres=min_contributing_acres,
        max_service_distance_meters=float("inf"),
        min_service_distance_meters=0.0,
        min_boundary_setback_meters=MIN_BOUNDARY_SETBACK_METERS,
        survey_buffer_meters=buffer_meters,
        min_downstream_clearance_meters=0.0,
    )
    _report_cell_count(
        f"On-parcel + boundary-setback gate alone (MIN_BOUNDARY_SETBACK_METERS={MIN_BOUNDARY_SETBACK_METERS}m, "
        "service-distance disabled)",
        parcel_setback_mask, dem,
    )

    # 2. Service-distance gate (boundary-SETBACK disabled via
    #    min_boundary_setback_meters=0, using the REAL boundary_polygon_utm
    #    -- on-parcel containment still required, using the REAL
    #    MAX/MIN_SERVICE_DISTANCE_METERS).
    service_distance_mask, _ = compute_water_eligible_cells(
        dem, production_areas, boundary_polygon_utm,
        min_valley_contributing_area_acres=min_contributing_acres,
        max_service_distance_meters=MAX_SERVICE_DISTANCE_METERS,
        min_service_distance_meters=MIN_SERVICE_DISTANCE_METERS,
        min_boundary_setback_meters=0.0,
        survey_buffer_meters=buffer_meters,
        min_downstream_clearance_meters=0.0,
    )
    _report_cell_count(
        f"Service-distance gate (MAX={MAX_SERVICE_DISTANCE_METERS}m, "
        f"MIN={MIN_SERVICE_DISTANCE_METERS}m, setback disabled, on-parcel still required)",
        service_distance_mask, dem,
    )

    # 2a. "Too far" only: max-distance real, min-distance disabled (0) --
    #     a cell excluded here failed to find ANY production area within
    #     MAX_SERVICE_DISTANCE_METERS.
    too_far_mask, _ = compute_water_eligible_cells(
        dem, production_areas, boundary_polygon_utm,
        min_valley_contributing_area_acres=min_contributing_acres,
        max_service_distance_meters=MAX_SERVICE_DISTANCE_METERS,
        min_service_distance_meters=0.0,
        min_boundary_setback_meters=0.0,
        survey_buffer_meters=buffer_meters,
        min_downstream_clearance_meters=0.0,
    )
    _report_cell_count(
        f"  -> excluded as TOO FAR (exceeds MAX_SERVICE_DISTANCE_METERS={MAX_SERVICE_DISTANCE_METERS}m "
        "from every production area)",
        drainage_mask_after & ~too_far_mask, dem,
    )

    # 2b. "Too close" only: min-distance real, max-distance disabled (inf) --
    #     a cell excluded here sits within (0, MIN_SERVICE_DISTANCE_METERS)
    #     of EVERY production area (genuinely near but not touching any of
    #     them) -- the opposite problem from "too far," needing the
    #     opposite fix (a smaller MIN_SERVICE_DISTANCE_METERS, not a
    #     larger MAX_SERVICE_DISTANCE_METERS).
    too_close_mask, _ = compute_water_eligible_cells(
        dem, production_areas, boundary_polygon_utm,
        min_valley_contributing_area_acres=min_contributing_acres,
        max_service_distance_meters=float("inf"),
        min_service_distance_meters=MIN_SERVICE_DISTANCE_METERS,
        min_boundary_setback_meters=0.0,
        survey_buffer_meters=buffer_meters,
        min_downstream_clearance_meters=0.0,
    )
    _report_cell_count(
        f"  -> excluded as TOO CLOSE (within MIN_SERVICE_DISTANCE_METERS={MIN_SERVICE_DISTANCE_METERS}m "
        "of every production area, but not touching any of them)",
        drainage_mask_after & ~too_close_mask, dem,
    )
    print()

    # 3. ALL FOUR gates combined -- this is compute_water_eligible_cells()'s
    #    own real output at this run's actual parameters, i.e. exactly what
    #    find_candidate_zones() (the real pipeline) works from.
    combined_mask, _ = compute_water_eligible_cells(
        dem, production_areas, boundary_polygon_utm,
        min_valley_contributing_area_acres=min_contributing_acres,
        survey_buffer_meters=buffer_meters,
        min_downstream_clearance_meters=downstream_clearance_meters,
    )
    _report_mask_stats(
        "ALL FOUR gates combined (real pipeline output -- matches find_candidate_zones()'s own eligible_mask)",
        combined_mask, dem,
    )

    if sweep_downstream:
        _report_downstream_sweep(dem, production_areas, boundary_polygon_utm, min_contributing_acres, buffer_meters)


def _report_downstream_sweep(
    dem: dict,
    production_areas: list[dict],
    boundary_polygon_utm: Polygon,
    min_contributing_acres: float,
    buffer_meters: float,
) -> None:
    """
    Runs the REAL, full pipeline (compute_water_eligible_cells() -- all
    four gates, unmodified) at MIN_DOWNSTREAM_CLEARANCE_METERS =
    15/25/40m, at whatever min_contributing_acres/buffer_meters this run
    otherwise used, so a threshold sweep can show whether the surviving
    cell set genuinely shifts from outlet-hugging (clustered near the
    boundary, where flow structurally exits soonest) toward a real
    interior band (further from the boundary, needing a longer confirmed
    downstream run) as the clearance requirement tightens -- not just
    shrinking in place.

    Per value, reports: qualifying cell count, acreage, connected-
    component count, and each surviving component's own centroid distance
    to the nearest boundary edge (via cell_union_footprint()'s real
    footprint, not a hull or a cell-count average) -- a sweep where that
    distance grows as clearance tightens is exactly the "outlet-hugging
    -> interior band" shift this gate exists to produce.
    """
    print("=== Downstream-clearance sweep (all four gates, real pipeline) ===\n")
    boundary_line = boundary_polygon_utm.boundary

    for clearance_meters in (15.0, 25.0, 40.0):
        eligible_mask, _ = compute_water_eligible_cells(
            dem, production_areas, boundary_polygon_utm,
            min_valley_contributing_area_acres=min_contributing_acres,
            survey_buffer_meters=buffer_meters,
            min_downstream_clearance_meters=clearance_meters,
        )
        cell_count = int(eligible_mask.sum())
        acres = cell_count * cell_area_acres(dem)
        labels, num_components = connected_components(eligible_mask)

        print(f"--- MIN_DOWNSTREAM_CLEARANCE_METERS = {clearance_meters}m ---")
        print(f"  Qualifying cell count: {cell_count}")
        print(f"  Acreage:               {acres:.3f} acres")
        print(f"  Connected components:  {num_components}")
        for component_id in range(num_components):
            component_mask = labels == component_id
            footprint = cell_union_footprint(dem, component_mask)
            centroid = footprint.centroid
            distance_to_boundary = boundary_line.distance(centroid)
            print(
                f"    component {component_id}: centroid=({centroid.x:.1f}, {centroid.y:.1f}), "
                f"distance to nearest boundary edge = {distance_to_boundary:.2f}m"
            )
        print()


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
    parser.add_argument(
        "--downstream-clearance-meters",
        type=float,
        default=MIN_DOWNSTREAM_CLEARANCE_METERS,
        help=f"Override MIN_DOWNSTREAM_CLEARANCE_METERS for this run only "
        f"(default: the current module constant, {MIN_DOWNSTREAM_CLEARANCE_METERS}). Ignored if "
        "--sweep-downstream is set.",
    )
    parser.add_argument(
        "--sweep-downstream",
        action="store_true",
        help="Run the full pipeline (all four gates) at MIN_DOWNSTREAM_CLEARANCE_METERS = 15/25/40m, "
        "at whatever --min-contributing-acres/--buffer-meters were otherwise passed, reporting "
        "per-value cell count/acreage/component count/component centroid distance to the boundary.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        main(
            min_contributing_acres=args.min_contributing_acres,
            buffer_meters=args.buffer_meters,
            downstream_clearance_meters=args.downstream_clearance_meters,
            sweep_downstream=args.sweep_downstream,
        )
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map ImageServer (DEM fetch) and production_area.py's own "
            "SSURGO/canopy/road data sources -- not a fully sandboxed environment."
        )
