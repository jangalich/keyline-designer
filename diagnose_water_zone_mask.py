"""
diagnose_water_zone_mask.py

Standalone, read-only diagnostic: reports pre- and post-dilation stats for
compute_water_eligible_cells()'s percentile-band mask (water_candidate_zones.py),
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
raster_grid.binary_dilate(), numpy.percentile()) in the same order; the
gate-breakdown section calls compute_water_eligible_cells() itself
directly, several times, with individual gate thresholds swapped for
"always pass" values (0/infinity) to isolate one real gate at a time --
the gate math itself is never duplicated, only which of the function's
own real checks can actually bind is varied per call. This covers all
SIX of compute_water_eligible_cells()'s real gates: percentile-band,
on-parcel/boundary-setback, service-distance, canopy (woody-vegetation
root zone), existing-road right-of-way, and the production-zone
exclusion (any cell inside the UNION of every production area's own
render_fill_polygon_utm is hard-excluded, buffered by WATER_ZONE_
PRODUCTION_SETBACK_METERS -- see that constant's own docstring in
water_candidate_zones.py) -- the real canopy_root_zone_mask_utm/
road_exclusion_union_utm this run actually fetches (see below) are held
REAL across every isolation call except the ones specifically isolating
canopy or road themselves, the same way the percentile-band gate is
already held real throughout. Unlike canopy/road, the production-zone
exclusion has no "unchecked" sentinel to disable for isolation -- it's
always built directly from whatever production_areas this run already
fetched, so its own isolation call below relaxes every OTHER gate
instead, the same technique the canopy-alone/road-alone isolation calls
already use.

Every isolation call uses the REAL boundary_polygon_utm, never a
synthetic stand-in -- an earlier version of this section used a
synthetic boundary covering the DEM's entire extent (to make the
on-parcel/setback gate a no-op) for the service-distance isolation calls
specifically. The on-parcel containment check has no override at all
(boundary_polygon_utm is required, not optional) -- only its SETBACK
distance can be disabled (min_boundary_setback_meters=0.0), so the
service-distance isolation below is "on-parcel required, setback
waived," not a pure isolation from every other gate.

The post-waist-split zone section below (elevation ranges, optimal
survey sub-area, confluence check) calls water_candidate_zones.
find_candidate_zones() directly -- the real, full pipeline entry point,
including its own waist-splitting and select_optimal_survey_subarea()
wiring -- rather than reimplementing clustering/scoring independently a
second time.

Requires real network access (a real USGS DEM fetch via dem_data.py, plus
production_area.py's own SSURGO/canopy/road fetches, plus this script's
own canopy/road fetches for the gate-breakdown section) -- this is a live
diagnostic against a real property, not the offline/synthetic-DEM tests
in test_water_candidate_zones.py. Canopy fetch failure degrades
gracefully HERE (unlike the real pipeline's own hard-fail via
get_required_tree_root_zone_mask_utm() -- see identify_water_system_
candidate_zones()'s own docstring): this diagnostic proceeds with the
canopy gate unchecked and says so, rather than aborting the whole run,
since a debugging tool that can't run at all when one optional-to-this-
script fetch fails is less useful than one that reports what it can and
flags the gap plainly.

--min-contributing-acres and --buffer-meters override
MIN_VALLEY_CONTRIBUTING_AREA_ACRES / WATER_ZONE_SURVEY_BUFFER_METERS for
this run only (default: the current module constants) -- for
experimentation while tuning either value; the actual module constants
themselves are never changed. --accumulation-percentile-low/-high and
--zone-min-waist-meters do the same for VALLEY_ACCUMULATION_PERCENTILE_LOW/
HIGH and WATER_ZONE_MIN_WAIST_METERS. The service-distance/boundary-setback
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
from production_area import (
    _fetch_road_exclusion_union_utm,
    get_required_tree_root_zone_mask_utm,
    identify_production_areas,
)
from raster_grid import (
    SQUARE_METERS_PER_ACRE,
    binary_dilate,
    cell_area_acres,
    connected_components,
    pixel_center_xy,
)
from valley_delineation import delineate_valleys, get_flow_accumulation_for_dem, get_flow_direction_for_dem
from water_candidate_zones import (
    MAX_SERVICE_DISTANCE_METERS,
    MIN_BOUNDARY_SETBACK_METERS,
    MIN_SERVICE_DISTANCE_METERS,
    MIN_VALLEY_CONTRIBUTING_AREA_ACRES,
    VALLEY_ACCUMULATION_PERCENTILE_HIGH,
    VALLEY_ACCUMULATION_PERCENTILE_LOW,
    WATER_ZONE_CANOPY_BUFFER_METERS,
    WATER_ZONE_MIN_WAIST_METERS,
    WATER_ZONE_PRODUCTION_SETBACK_METERS,
    WATER_ZONE_ROAD_BUFFER_METERS,
    WATER_ZONE_SUBAREA_TARGET_ACRES,
    WATER_ZONE_SUBAREA_TRIGGER_ACRES,
    WATER_ZONE_SURVEY_BUFFER_METERS,
    _CANOPY_CHECK_UNCHECKED,
    _ROAD_CHECK_UNCHECKED,
    _survey_buffer_radius_cells,
    compute_water_eligible_cells,
    find_candidate_zones,
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
    accumulation_percentile_low: float = VALLEY_ACCUMULATION_PERCENTILE_LOW,
    accumulation_percentile_high: float = VALLEY_ACCUMULATION_PERCENTILE_HIGH,
    zone_min_waist_meters: float = WATER_ZONE_MIN_WAIST_METERS,
) -> None:
    print("diagnose_water_zone_mask.py -- pre/post survey-buffer-dilation mask stats\n")
    print(f"Property: real reference boundary, {len(PROPERTY_BOUNDARY)} vertices")
    print(f"Boundary coordinates (lon, lat): {PROPERTY_BOUNDARY}\n")
    print(f"min_contributing_acres (this run)        = {min_contributing_acres} acres "
          f"(module default: {MIN_VALLEY_CONTRIBUTING_AREA_ACRES}) -- now a FLOOR for the "
          "percentile population, not the gate itself")
    print(f"buffer_meters (this run)                 = {buffer_meters}m "
          f"(module default: {WATER_ZONE_SURVEY_BUFFER_METERS})")
    print(f"accumulation_percentile_low/high (run)    = {accumulation_percentile_low}/{accumulation_percentile_high} "
          f"(module default: {VALLEY_ACCUMULATION_PERCENTILE_LOW}/{VALLEY_ACCUMULATION_PERCENTILE_HIGH})")
    print(f"zone_min_waist_meters (this run)          = {zone_min_waist_meters}m "
          f"(module default: {WATER_ZONE_MIN_WAIST_METERS})")
    print(f"production_setback_meters (module default, not overridable this run) = "
          f"{WATER_ZONE_PRODUCTION_SETBACK_METERS}m\n")

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

    # Fetches the same two real, cell-level hard-exclusion inputs
    # compute_water_eligible_cells()'s gates 4/5 apply -- reused directly
    # (production_area.get_required_tree_root_zone_mask_utm()/
    # _fetch_road_exclusion_union_utm(), the exact same functions
    # identify_water_system_candidate_zones() calls for the real
    # pipeline), not refetched or reimplemented independently. Unlike that
    # real entry point, canopy fetch failure degrades GRACEFULLY here
    # (this is a debugging tool, not the production pipeline) -- the gate
    # is left unchecked and every downstream section says so plainly,
    # rather than the whole diagnostic aborting.
    canopy_root_zone_mask_utm = _CANOPY_CHECK_UNCHECKED
    canopy_available = False
    try:
        canopy_root_zone_mask_utm = get_required_tree_root_zone_mask_utm(
            boundary_polygon_utm, dem, buffer_meters=WATER_ZONE_CANOPY_BUFFER_METERS
        )
        canopy_available = True
        print(
            f"Canopy root-zone mask fetched (WATER_ZONE_CANOPY_BUFFER_METERS={WATER_ZONE_CANOPY_BUFFER_METERS}m): "
            f"{int(canopy_root_zone_mask_utm.sum())} tree-root-zone cell(s) on this DEM's own grid\n"
        )
    except Exception as e:
        print(
            f"Canopy fetch failed ({e}) -- proceeding with the canopy gate UNCHECKED for this diagnostic run. "
            "The real pipeline (identify_water_system_candidate_zones()) would hard-fail here instead -- see "
            "get_required_tree_root_zone_mask_utm()'s own docstring -- so every number below that depends on "
            "canopy exclusion is a LOWER BOUND on what the real pipeline would actually exclude.\n"
        )

    road_exclusion_union_utm = _ROAD_CHECK_UNCHECKED
    try:
        road_exclusion_union_utm = _fetch_road_exclusion_union_utm(
            PROPERTY_BOUNDARY, dem, buffer_meters=WATER_ZONE_ROAD_BUFFER_METERS
        )
        if road_exclusion_union_utm is None:
            print("Road exclusion fetch succeeded: no roads found nearby (checked, genuinely none -- not the "
                  "same as unchecked).\n")
        else:
            print(f"Road exclusion union fetched (WATER_ZONE_ROAD_BUFFER_METERS={WATER_ZONE_ROAD_BUFFER_METERS}m).\n")
    except Exception as e:
        print(
            f"Road exclusion fetch failed ({e}) -- proceeding with the road gate UNCHECKED for this diagnostic "
            "run (this gate degrades gracefully in the real pipeline too, same behavior here).\n"
        )
        road_exclusion_union_utm = _ROAD_CHECK_UNCHECKED

    # Same two-step computation compute_water_eligible_cells() does
    # internally, just stopped short of the service-distance/boundary-
    # setback loop so the BEFORE/AFTER dilation stats can be reported
    # directly. Uses this run's min_contributing_acres/buffer_meters/
    # accumulation_percentile_low/high (module constants unless overridden
    # via the matching --flags).
    flow_accumulation_cells = get_flow_accumulation_for_dem(dem)
    area_per_cell = cell_area_acres(dem)
    min_contributing_cells = min_contributing_acres / area_per_cell
    floor_mask = flow_accumulation_cells >= min_contributing_cells

    print(f"(min_contributing_acres = {min_contributing_acres} acres -> "
          f"{min_contributing_cells:.2f} cells at this DEM's resolution -- the FLOOR, not the gate)\n")

    _report_mask_stats("FLOOR-qualifying mask (before percentile band, before on-parcel filtering)", floor_mask, dem)

    # The percentile band is computed over the ON-PARCEL population only
    # (see compute_water_eligible_cells()'s own docstring) -- reproduced
    # here the same way, not as a second, independent definition.
    boundary_prepared_for_population = prep(boundary_polygon_utm)
    population_values = [
        float(flow_accumulation_cells[r, c])
        for r, c in np.argwhere(floor_mask)
        if boundary_prepared_for_population.contains(Point(*pixel_center_xy(dem, int(r), int(c))))
    ]
    print(f"Drainage-qualifying population (on-parcel, floor-passing cells): {len(population_values)} cell(s)\n")

    if not population_values:
        print("Population is empty -- no percentile band can be computed; every downstream section "
              "below will report an empty mask.\n")
        drainage_mask_before = np.zeros(floor_mask.shape, dtype=bool)
    else:
        p_low = np.percentile(population_values, accumulation_percentile_low)
        p_high = np.percentile(population_values, accumulation_percentile_high)
        print(f"p{accumulation_percentile_low:.0f} = {p_low:.2f} cells, "
              f"p{accumulation_percentile_high:.0f} = {p_high:.2f} cells\n")
        drainage_mask_before = floor_mask & (flow_accumulation_cells >= p_low) & (flow_accumulation_cells <= p_high)

    _report_mask_stats("BEFORE dilation (raw percentile-band-qualifying mask)", drainage_mask_before, dem)

    radius_cells = _survey_buffer_radius_cells(dem, buffer_meters)
    print(f"(buffer_meters = {buffer_meters}m -> dilation radius = {radius_cells} cell(s))\n")

    drainage_mask_after = binary_dilate(drainage_mask_before, radius_cells)
    _report_mask_stats("AFTER dilation (survey-buffered percentile-band mask)", drainage_mask_after, dem)

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
        "1. On-parcel cells within the dilated percentile-band mask (NO setback applied)",
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
    #    row-major order). Sampled from on_parcel_mask directly so every
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
    # is reimplemented here. Items 1/2/2a/2b use the REAL boundary_polygon_utm
    # (same as the on-parcel-vs-setback section above) with
    # min_boundary_setback_meters=0.0 for the service-distance isolation --
    # this disables gate 2's SETBACK distance specifically (its only
    # overridable knob) while leaving on-parcel containment intact. These
    # items are therefore "service-distance, on-parcel required, setback
    # waived" rather than pure service-distance isolation.
    print("=== Service-distance / boundary-setback gate breakdown (post-dilation mask) ===\n")

    # 1. On-parcel + boundary-setback gate ALONE (service-distance disabled
    #    via max=infinity/min=0, using the REAL property boundary/setback).
    #    Canopy/road held REAL (this isn't what's being isolated here) --
    #    same as the percentile-band gate already being held real.
    parcel_setback_mask = compute_water_eligible_cells(
        dem, production_areas, boundary_polygon_utm,
        min_valley_contributing_area_acres=min_contributing_acres,
        accumulation_percentile_low=accumulation_percentile_low,
        accumulation_percentile_high=accumulation_percentile_high,
        max_service_distance_meters=float("inf"),
        min_service_distance_meters=0.0,
        min_boundary_setback_meters=MIN_BOUNDARY_SETBACK_METERS,
        survey_buffer_meters=buffer_meters,
        canopy_root_zone_mask_utm=canopy_root_zone_mask_utm,
        road_exclusion_union_utm=road_exclusion_union_utm,
    )
    _report_cell_count(
        f"On-parcel + boundary-setback gate alone (MIN_BOUNDARY_SETBACK_METERS={MIN_BOUNDARY_SETBACK_METERS}m, "
        "service-distance disabled)",
        parcel_setback_mask, dem,
    )

    # 2. Service-distance gate (boundary-SETBACK disabled via
    #    min_boundary_setback_meters=0, using the REAL boundary_polygon_utm
    #    -- on-parcel containment still required, using the REAL
    #    MAX/MIN_SERVICE_DISTANCE_METERS). Canopy/road held REAL.
    service_distance_mask = compute_water_eligible_cells(
        dem, production_areas, boundary_polygon_utm,
        min_valley_contributing_area_acres=min_contributing_acres,
        accumulation_percentile_low=accumulation_percentile_low,
        accumulation_percentile_high=accumulation_percentile_high,
        max_service_distance_meters=MAX_SERVICE_DISTANCE_METERS,
        min_service_distance_meters=MIN_SERVICE_DISTANCE_METERS,
        min_boundary_setback_meters=0.0,
        survey_buffer_meters=buffer_meters,
        canopy_root_zone_mask_utm=canopy_root_zone_mask_utm,
        road_exclusion_union_utm=road_exclusion_union_utm,
    )
    _report_cell_count(
        f"Service-distance gate (MAX={MAX_SERVICE_DISTANCE_METERS}m, "
        f"MIN={MIN_SERVICE_DISTANCE_METERS}m, setback disabled, on-parcel still required)",
        service_distance_mask, dem,
    )

    # 2a. "Too far" only: max-distance real, min-distance disabled (0) --
    #     a cell excluded here failed to find ANY production area within
    #     MAX_SERVICE_DISTANCE_METERS. Canopy/road held REAL.
    too_far_mask = compute_water_eligible_cells(
        dem, production_areas, boundary_polygon_utm,
        min_valley_contributing_area_acres=min_contributing_acres,
        accumulation_percentile_low=accumulation_percentile_low,
        accumulation_percentile_high=accumulation_percentile_high,
        max_service_distance_meters=MAX_SERVICE_DISTANCE_METERS,
        min_service_distance_meters=0.0,
        min_boundary_setback_meters=0.0,
        survey_buffer_meters=buffer_meters,
        canopy_root_zone_mask_utm=canopy_root_zone_mask_utm,
        road_exclusion_union_utm=road_exclusion_union_utm,
    )
    # too_far_mask is on-parcel-required (compute_water_eligible_cells()'s
    # on-parcel gate has no override), so it can only ever contain a
    # subset of on_parcel_mask -- the complement below is against
    # on_parcel_mask (item 1 above), not the larger drainage_mask_after,
    # so an off-parcel cell never gets spuriously counted as "excluded as
    # too far" (see the internal consistency check below for why this
    # matters).
    too_far_excluded_mask = on_parcel_mask & ~too_far_mask
    _report_cell_count(
        f"  -> excluded as TOO FAR (exceeds MAX_SERVICE_DISTANCE_METERS={MAX_SERVICE_DISTANCE_METERS}m "
        "from every production area)",
        too_far_excluded_mask, dem,
    )

    # 2b. "Too close" only: min-distance real, max-distance disabled (inf) --
    #     a cell excluded here sits within (0, MIN_SERVICE_DISTANCE_METERS)
    #     of EVERY production area (genuinely near but not touching any of
    #     them) -- the opposite problem from "too far," needing the
    #     opposite fix (a smaller MIN_SERVICE_DISTANCE_METERS, not a
    #     larger MAX_SERVICE_DISTANCE_METERS). Canopy/road held REAL.
    too_close_mask = compute_water_eligible_cells(
        dem, production_areas, boundary_polygon_utm,
        min_valley_contributing_area_acres=min_contributing_acres,
        accumulation_percentile_low=accumulation_percentile_low,
        accumulation_percentile_high=accumulation_percentile_high,
        max_service_distance_meters=float("inf"),
        min_service_distance_meters=MIN_SERVICE_DISTANCE_METERS,
        min_boundary_setback_meters=0.0,
        survey_buffer_meters=buffer_meters,
        canopy_root_zone_mask_utm=canopy_root_zone_mask_utm,
        road_exclusion_union_utm=road_exclusion_union_utm,
    )
    too_close_excluded_mask = on_parcel_mask & ~too_close_mask
    _report_cell_count(
        f"  -> excluded as TOO CLOSE (within MIN_SERVICE_DISTANCE_METERS={MIN_SERVICE_DISTANCE_METERS}m "
        "of every production area, but not touching any of them)",
        too_close_excluded_mask, dem,
    )
    print()

    # 2c. Canopy gate ALONE: service-distance/setback disabled (max=inf/
    #     min=0/setback=0), road skipped -- isolates canopy specifically.
    canopy_alone_mask = compute_water_eligible_cells(
        dem, production_areas, boundary_polygon_utm,
        min_valley_contributing_area_acres=min_contributing_acres,
        accumulation_percentile_low=accumulation_percentile_low,
        accumulation_percentile_high=accumulation_percentile_high,
        max_service_distance_meters=float("inf"),
        min_service_distance_meters=0.0,
        min_boundary_setback_meters=0.0,
        survey_buffer_meters=buffer_meters,
        canopy_root_zone_mask_utm=canopy_root_zone_mask_utm,
        road_exclusion_union_utm=_ROAD_CHECK_UNCHECKED,
    )
    canopy_excluded_mask = on_parcel_mask & ~canopy_alone_mask
    _report_cell_count(
        f"Canopy gate alone (woody-vegetation root zone, WATER_ZONE_CANOPY_BUFFER_METERS="
        f"{WATER_ZONE_CANOPY_BUFFER_METERS}m)"
        + ("" if canopy_available else " [canopy fetch FAILED above -- unchecked, reads 0, not a real result]"),
        canopy_excluded_mask, dem,
    )

    # 2d. Road gate ALONE: service-distance/setback disabled, canopy
    #     skipped -- isolates existing-road right-of-way specifically.
    road_alone_mask = compute_water_eligible_cells(
        dem, production_areas, boundary_polygon_utm,
        min_valley_contributing_area_acres=min_contributing_acres,
        accumulation_percentile_low=accumulation_percentile_low,
        accumulation_percentile_high=accumulation_percentile_high,
        max_service_distance_meters=float("inf"),
        min_service_distance_meters=0.0,
        min_boundary_setback_meters=0.0,
        survey_buffer_meters=buffer_meters,
        canopy_root_zone_mask_utm=_CANOPY_CHECK_UNCHECKED,
        road_exclusion_union_utm=road_exclusion_union_utm,
    )
    road_excluded_mask = on_parcel_mask & ~road_alone_mask
    _report_cell_count(
        f"Road gate alone (existing right-of-way, WATER_ZONE_ROAD_BUFFER_METERS={WATER_ZONE_ROAD_BUFFER_METERS}m)",
        road_excluded_mask, dem,
    )

    # 2e. Production-zone exclusion gate ALONE: service-distance/setback
    #     disabled, canopy/road skipped -- isolates the hard exclusion
    #     against every production area's own render_fill_polygon_utm
    #     specifically. Unlike canopy/road, this gate has no "unchecked"
    #     sentinel of its own to disable directly (it's always built from
    #     whichever production_areas this run already fetched) -- so
    #     isolating it means relaxing every OTHER gate instead, same
    #     technique 2c/2d above use.
    production_alone_mask = compute_water_eligible_cells(
        dem, production_areas, boundary_polygon_utm,
        min_valley_contributing_area_acres=min_contributing_acres,
        accumulation_percentile_low=accumulation_percentile_low,
        accumulation_percentile_high=accumulation_percentile_high,
        max_service_distance_meters=float("inf"),
        min_service_distance_meters=0.0,
        min_boundary_setback_meters=0.0,
        survey_buffer_meters=buffer_meters,
        canopy_root_zone_mask_utm=_CANOPY_CHECK_UNCHECKED,
        road_exclusion_union_utm=_ROAD_CHECK_UNCHECKED,
    )
    production_excluded_mask = on_parcel_mask & ~production_alone_mask
    _report_cell_count(
        "Production-zone exclusion gate alone (any cell inside the union of every production area's own "
        f"render_fill_polygon_utm, WATER_ZONE_PRODUCTION_SETBACK_METERS={WATER_ZONE_PRODUCTION_SETBACK_METERS}m)",
        production_excluded_mask, dem,
    )
    print()

    # --- Internal consistency check -----------------------------------
    # By construction, too_far_mask = {on-parcel cells passing the
    # max-distance test} and too_close_mask = {on-parcel cells passing the
    # min-distance test}, both restricted to on_parcel_mask and sharing
    # the same gate-1 (percentile band) conditions as service_distance_mask
    # (which requires BOTH distance tests). So (on_parcel_mask \
    # too_far_mask) union (on_parcel_mask \ too_close_mask) ==
    # on_parcel_mask \ service_distance_mask exactly, and by
    # inclusion-exclusion:
    #   too_far_excluded_count + too_close_excluded_count - overlap_count
    #   == on_parcel_count - service_distance_pass_count
    # A mismatch here means the isolation calls above no longer share the
    # same universe/gate conditions as service_distance_mask -- exactly
    # the class of regression this check exists to catch automatically.
    on_parcel_count = int(on_parcel_mask.sum())
    service_distance_pass_count = int(service_distance_mask.sum())
    too_far_excluded_count = int(too_far_excluded_mask.sum())
    too_close_excluded_count = int(too_close_excluded_mask.sum())
    overlap_count = int((too_far_excluded_mask & too_close_excluded_mask).sum())

    lhs = too_far_excluded_count + too_close_excluded_count - overlap_count
    rhs = on_parcel_count - service_distance_pass_count
    print("Internal consistency check: (too_far_excluded + too_close_excluded - overlap) "
          "== (on_parcel_count - service_distance_pass_count)")
    print(f"  too_far_excluded_count={too_far_excluded_count}, too_close_excluded_count={too_close_excluded_count}, "
          f"overlap_count={overlap_count} -> lhs={lhs}")
    print(f"  on_parcel_count={on_parcel_count}, service_distance_pass_count={service_distance_pass_count} -> rhs={rhs}")
    if lhs == rhs:
        print(f"  PASS: {lhs} == {rhs}\n")
    else:
        print(f"  WARNING: consistency check FAILED ({lhs} != {rhs}) -- the gate-breakdown "
              "isolation calls above may no longer share the same universe/gate conditions as "
              "service_distance_mask. Treat the TOO FAR/TOO CLOSE numbers above as unreliable "
              "until this is investigated.\n")

    # 3. ALL SIX gates combined -- this is compute_water_eligible_cells()'s
    #    own real output at this run's actual parameters, i.e. exactly what
    #    find_candidate_zones() (the real pipeline) works from (BEFORE
    #    waist-splitting/clustering -- see the zone section below for the
    #    post-waist-split numbers). production_setback_meters is left at
    #    its module default (WATER_ZONE_PRODUCTION_SETBACK_METERS) --
    #    not overridable by this script, same as the service-distance/
    #    boundary-setback thresholds (see module docstring).
    combined_mask = compute_water_eligible_cells(
        dem, production_areas, boundary_polygon_utm,
        min_valley_contributing_area_acres=min_contributing_acres,
        accumulation_percentile_low=accumulation_percentile_low,
        accumulation_percentile_high=accumulation_percentile_high,
        survey_buffer_meters=buffer_meters,
        canopy_root_zone_mask_utm=canopy_root_zone_mask_utm,
        road_exclusion_union_utm=road_exclusion_union_utm,
    )
    _report_mask_stats(
        "ALL SIX gates combined (real pipeline output -- matches find_candidate_zones()'s own eligible_mask, "
        "pre-waist-split)",
        combined_mask, dem,
    )

    # The real, full pipeline entry point itself -- not a re-implementation
    # of clustering/waist-splitting/whole-zone scoring/sub-area selection.
    # zones carries every field find_candidate_zones() itself produces
    # (cells, polygon_utm, primary_production_area_relationship,
    # optimal_subarea_polygon_utm/geometry_wgs84/acres, ...) so the
    # sections below report on the REAL thing, not a diagnostic-only
    # approximation of it.
    zones = find_candidate_zones(
        dem, production_areas, boundary_polygon_utm,
        min_valley_contributing_area_acres=min_contributing_acres,
        accumulation_percentile_low=accumulation_percentile_low,
        accumulation_percentile_high=accumulation_percentile_high,
        survey_buffer_meters=buffer_meters,
        min_zone_waist_meters=zone_min_waist_meters,
        canopy_root_zone_mask_utm=canopy_root_zone_mask_utm,
        road_exclusion_union_utm=road_exclusion_union_utm,
    )

    _report_waist_split(combined_mask, zones, dem)

    ranked_zones = _report_zone_elevation_ranges(dem, boundary_polygon_utm, zones)

    if not ranked_zones:
        print("No surviving post-waist-split zone to run the sub-area/confluence checks against -- skipping.\n")
        return

    top_zone = ranked_zones[0]
    _report_optimal_subarea(dem, production_areas, top_zone)
    _report_confluence_check(dem, boundary_polygon_utm, top_zone["cells"])


def _report_waist_split(eligible_mask, zones: list[dict], dem: dict) -> None:
    """
    Reports how find_candidate_zones()'s own waist-splitting step changes
    the cell/acreage/component-count numbers relative to the pre-split
    connected-component labeling of `eligible_mask` (the "ALL FIVE gates
    combined" mask reported by the caller above) -- a dilation-induced
    merge between two originally-separate drainage patches should show up
    here as component count ticking UP after the split, with total cell
    count and acreage roughly unchanged (waist-splitting only re-labels
    which cells belong to which cluster; it can discard cells only if a
    resulting sub-cluster would fall below MIN_WATER_ZONE_AREA_ACRES, in
    which case the whole split is rejected and the original, unsplit
    cluster passes through instead -- see raster_grid.attempt_waist_
    split()'s own docstring).

    `zones` is find_candidate_zones()'s own real output (already waist-
    split, already clipped to boundary_polygon_utm, already gated on
    min_water_zone_area_acres) -- POST numbers are read directly off it,
    not recomputed independently.
    """
    print("=== Waist-split before/after (post-dilation, all-five-gates mask) ===\n")

    cell_count_before = int(eligible_mask.sum())
    area_per_cell = cell_area_acres(dem)
    acres_before = cell_count_before * area_per_cell
    _, num_components_before = connected_components(eligible_mask)

    print(f"PRE-waist-split:  {cell_count_before} cells, {acres_before:.3f} acres, "
          f"{num_components_before} connected component(s)")

    total_cells_after = sum(len(zone["cells"]) for zone in zones)
    num_components_after = len(zones)
    acres_after = total_cells_after * area_per_cell
    print(f"POST-waist-split: {total_cells_after} cells, {acres_after:.3f} acres, "
          f"{num_components_after} connected component(s)")
    if num_components_after > num_components_before:
        print(
            f"  -> component count ticked UP ({num_components_before} -> {num_components_after}): "
            "waist-splitting caught at least one dilation-induced merge."
        )
    elif num_components_after == num_components_before:
        print("  -> component count unchanged: no waist was detected (or none survived the area gate).")
    print()


def _report_zone_elevation_ranges(
    dem: dict,
    boundary_polygon_utm: Polygon,
    zones: list[dict],
) -> list[dict]:
    """
    For each real zone find_candidate_zones() returns, reports its member
    cells' real elevation range (min/max, straight from dem['array']),
    alongside its real footprint acreage (zone['polygon_utm'].area, not a
    cell-count approximation), and the property's own overall ON-PARCEL
    elevation range (every cell whose center falls inside
    boundary_polygon_utm, NaN cells excluded) for direct comparison --
    this is what actually tests whether a zone spans most of the parcel's
    real elevation range ("ridge to valley bottom") or just a modest
    slice of it, rather than eyeballing acreage alone.

    Zones are reported ranked by real footprint acreage descending -- a
    simple, self-contained proxy for "top-ranked" that doesn't require
    fetching soil/stream data or running the full water_suitability.py
    scoring pipeline (this diagnostic's own established scope: real
    generation-time gates and clustering, not full suitability scoring).
    Explicitly NOT the same ranking identify_water_suitability()'s
    score_water_zones() would produce.

    Returns the same zones, re-sorted largest-first, so the caller can
    pick the top-ranked one for the sub-area/confluence checks below
    without re-deriving the ranking a second time.
    """
    array = dem["array"]

    boundary_prepared = prep(boundary_polygon_utm)
    on_parcel_elevations = [
        float(array[r, c])
        for r in range(array.shape[0])
        for c in range(array.shape[1])
        if not np.isnan(array[r, c]) and boundary_prepared.contains(Point(*pixel_center_xy(dem, r, c)))
    ]

    print("=== Zone elevation ranges (post-waist-split, ranked by acreage) ===\n")

    if not on_parcel_elevations:
        print("No on-parcel DEM cells at all -- cannot report an overall elevation range.\n")
        overall_span = None
    else:
        overall_min = min(on_parcel_elevations)
        overall_max = max(on_parcel_elevations)
        overall_span = overall_max - overall_min
        print(
            f"Property's overall on-parcel elevation range: {overall_min:.2f}m - {overall_max:.2f}m "
            f"(span {overall_span:.2f}m, over {len(on_parcel_elevations)} on-parcel cell(s))\n"
        )

    if not zones:
        print("No surviving post-waist-split zones.\n")
        return []

    ranked = sorted(zones, key=lambda z: z["polygon_utm"].area, reverse=True)

    for rank, zone in enumerate(ranked):
        cells = zone["cells"]
        elevations = [float(array[r, c]) for r, c in cells if not np.isnan(array[r, c])]
        acres = zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
        label = f"Zone rank {rank} (largest, id={zone['id']})" if rank == 0 else f"Zone rank {rank} (id={zone['id']})"
        if not elevations:
            print(f"--- {label}: {len(cells)} cells, {acres:.3f} acres -- all-NaN, no elevation data ---")
            continue

        zone_min, zone_max = min(elevations), max(elevations)
        zone_span = zone_max - zone_min
        print(f"--- {label}: {len(cells)} cells, {acres:.3f} acres ---")
        print(f"  Elevation range: {zone_min:.2f}m - {zone_max:.2f}m (span {zone_span:.2f}m)")
        if overall_span is not None and overall_span > 0:
            coverage_pct = zone_span / overall_span * 100
            print(
                f"  Spans {coverage_pct:.1f}% of the property's overall on-parcel elevation range "
                f"({overall_span:.2f}m) -- "
                + (
                    "a broad span consistent with a 'ridge to valley bottom' read across most of the parcel."
                    if coverage_pct >= 50
                    else "a modest slice of the parcel's full elevation range, not most of it."
                )
            )
        print()

    return ranked


def _report_optimal_subarea(dem: dict, production_areas: list[dict], zone: dict) -> None:
    """
    Reports water_candidate_zones.select_optimal_survey_subarea()'s own
    real output for the top-ranked zone -- already computed and attached
    by find_candidate_zones() itself (zone['optimal_subarea_polygon_utm']/
    'optimal_subarea_geometry_wgs84'/'optimal_subarea_acres'), not
    recomputed here. Reports the sub-area's own acreage, centroid (UTM and
    lon/lat, same style as the production-area centroid line printed near
    the top of this script) and real bounding extent, plus its member
    cells' elevation range and distance-to-primary-production-area range/
    average, directly against the full zone's own equivalents, so a
    "smaller, higher-confidence pointer" claim can actually be checked
    (higher elevation, closer to the production area it serves) rather
    than taken on faith.
    """
    print("=== Optimal survey sub-area (top-ranked zone) ===\n")

    zone_area_acres = zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE
    print(f"Zone's own full footprint: {zone_area_acres:.3f} acres ({len(zone['cells'])} cells)")

    if zone_area_acres <= WATER_ZONE_SUBAREA_TRIGGER_ACRES:
        print(
            f"At/under WATER_ZONE_SUBAREA_TRIGGER_ACRES ({WATER_ZONE_SUBAREA_TRIGGER_ACRES} acres) -- "
            "select_optimal_survey_subarea() correctly returns None here: the full zone footprint is "
            "already a reasonable survey pointer at this size.\n"
        )
        return

    subarea_acres = zone["optimal_subarea_acres"]
    if subarea_acres is None or zone["optimal_subarea_polygon_utm"] is None:
        print(
            "No valid sub-area could be selected -- every zone cell fell inside the production area it "
            "serves, or its primary production area couldn't be resolved (see select_optimal_survey_"
            "subarea()'s own docstring).\n"
        )
        return

    primary_production_area_id = zone["primary_production_area_relationship"]["production_area_id"]
    primary_patch = next((p for p in production_areas if p["id"] == primary_production_area_id), None)
    if primary_patch is None:
        print(
            f"Sub-area reports {subarea_acres:.3f} acres, but the primary production area id="
            f"{primary_production_area_id} is no longer in this run's production_areas list -- can't report "
            "elevation/distance comparisons.\n"
        )
        return

    production_polygon = primary_patch["polygon_utm"]
    subarea_polygon = zone["optimal_subarea_polygon_utm"]
    subarea_cells = [
        (r, c) for r, c in zone["cells"]
        if subarea_polygon.intersects(Point(*pixel_center_xy(dem, r, c)))
    ]

    array = dem["array"]
    zone_elevations = [float(array[r, c]) for r, c in zone["cells"] if not np.isnan(array[r, c])]
    subarea_elevations = [float(array[r, c]) for r, c in subarea_cells if not np.isnan(array[r, c])]
    zone_distances = [
        Point(*pixel_center_xy(dem, r, c)).distance(production_polygon) for r, c in zone["cells"]
    ]
    subarea_distances = [
        Point(*pixel_center_xy(dem, r, c)).distance(production_polygon) for r, c in subarea_cells
    ]

    print(
        f"Optimal sub-area: {subarea_acres:.3f} acres ({len(subarea_cells)} of {len(zone['cells'])} zone "
        f"cells), capped near WATER_ZONE_SUBAREA_TARGET_ACRES ({WATER_ZONE_SUBAREA_TARGET_ACRES} acres)"
    )

    # Centroid/extent -- same style as the production-area centroid line
    # printed near the top of this script (centroid=(x, y)), plus the
    # lon/lat reprojection (same warp_transform() call every other
    # UTM->WGS84 conversion in this file already uses) and the polygon's
    # own real bounding box (same style boundary_polygon_utm.bounds is
    # already printed in).
    subarea_centroid = subarea_polygon.centroid
    subarea_lons, subarea_lats = warp_transform(
        dem["crs"], "EPSG:4326", [subarea_centroid.x], [subarea_centroid.y]
    )
    print(
        f"  Centroid: UTM=({subarea_centroid.x:.1f}, {subarea_centroid.y:.1f}), "
        f"lon/lat=({subarea_lons[0]:.6f}, {subarea_lats[0]:.6f})"
    )
    print(f"  Bounding extent (UTM, {dem['crs']}): {subarea_polygon.bounds}")

    if zone_elevations and subarea_elevations:
        print(
            f"  Elevation:  sub-area avg={np.mean(subarea_elevations):.2f}m "
            f"(range {min(subarea_elevations):.2f}-{max(subarea_elevations):.2f}m) vs. full zone avg="
            f"{np.mean(zone_elevations):.2f}m (range {min(zone_elevations):.2f}-{max(zone_elevations):.2f}m)"
        )
    print(
        f"  Distance to primary production area (id={primary_production_area_id}): "
        f"sub-area avg={np.mean(subarea_distances):.1f}m vs. full zone avg={np.mean(zone_distances):.1f}m"
    )
    print()



def _trace_flow_path_cells(
    flow_direction_cells: np.ndarray,
    start_row: int,
    start_col: int,
    boundary_prepared,
    dem: dict,
    max_steps: int = 500,
) -> tuple[list[tuple[int, int]], bool]:
    """
    Simplified re-implementation of the walking logic
    water_candidate_zones._downstream_clearance_meters() used before the
    downstream-clearance gate was removed: walks a cell's D8 flow path
    (flow_direction_cells, valley_delineation.get_flow_direction_for_dem()'s
    own (rows, cols, 2) array -- each [row, col] holds the absolute
    (target_row, target_col) of that cell's downhill neighbor, or (-1, -1)
    if there is none) forward, cell by cell, until it either exits
    boundary_polygon_utm (via boundary_prepared, a shapely prepared
    geometry) or hits a dead end/cycle/the max_steps budget.

    Unlike the original gate, this does NOT accumulate real ground
    distance or compare against any threshold -- it exists purely to
    return the actual SEQUENCE of (row, col) cells visited, so callers
    can check whether two independently-traced paths ever share a cell
    (a real confluence) rather than measuring how far a single path
    travels.

    Returns (path_cells, exited): path_cells includes the starting cell
    itself as its first entry. exited is True only if the walk left
    boundary_polygon_utm before hitting a dead end, a cycle, or max_steps.
    """
    path_cells = [(start_row, start_col)]
    visited = {(start_row, start_col)}
    row, col = start_row, start_col

    for _ in range(max_steps):
        target_row, target_col = flow_direction_cells[row, col]
        target_row, target_col = int(target_row), int(target_col)

        if target_row < 0:
            return path_cells, False

        target_cell = (target_row, target_col)
        if target_cell in visited:
            return path_cells, False

        path_cells.append(target_cell)
        x, y = pixel_center_xy(dem, target_row, target_col)
        if not boundary_prepared.contains(Point(x, y)):
            return path_cells, True

        visited.add(target_cell)
        row, col = target_row, target_col

    return path_cells, False


def _report_confluence_check(
    dem: dict,
    boundary_polygon_utm: Polygon,
    top_zone_cells: list[tuple[int, int]],
) -> None:
    """
    Tests whether the top-ranked zone's own eligible cells -- and the
    real, coarser-threshold valley branches (valley_delineation.
    delineate_valleys()'s own output) that reach it -- actually converge
    on a shared downstream cell (a true confluence) before exiting the
    parcel, versus running roughly parallel without ever merging (which
    would point toward the "one broad hillside, no real confluence"
    explanation instead of "ridge to valley bottom, channels converging").

    PART A -- the zone's own member cells: traces every one of
    top_zone_cells's own D8 flow paths forward (_trace_flow_path_cells())
    and checks whether any two of those paths ever share a cell before
    either exits. Since the zone's member cells are already one
    8-connected cluster by construction, a high merge rate here is
    expected and confirms the zone itself reads as one confluent band,
    not proof on its own of a real valley-bottom confluence -- PART B is
    the more meaningful test.

    PART B -- real valley branches: for every branch of every valley
    delineate_valleys() finds (a SEPARATE, coarser-threshold trace than
    this zone's own per-cell eligibility -- see water_candidate_zones.py's
    own module docstring), checks whether it has any cell inside
    top_zone_cells ("within") OR its own outlet cell's real D8 flow target
    (flow_direction_cells, one step PAST wherever delineate_valleys()'s own
    contributing-area threshold stopped tracing it -- not just the
    branch's own recorded cell list, which would make this redundant with
    "within") lands inside top_zone_cells ("immediately upstream") --
    reported as the count of qualifying branches out of the total found.
    For each qualifying branch, continues its OWN flow trace forward from
    its own outlet cell (past wherever delineate_valleys()'s own
    contributing-area threshold stopped tracing it) and checks whether any
    two qualifying branches' continued traces ever share a cell before
    exiting the parcel -- a real, literal confluence -- versus never
    sharing one at all (parallel, non-converging flow paths).
    """
    print("=== Confluence check (top-ranked zone) ===\n")

    flow_direction_cells = get_flow_direction_for_dem(dem)
    boundary_prepared = prep(boundary_polygon_utm)

    # --- PART A: the zone's own member cells ---
    zone_cells_set = set(top_zone_cells)
    zone_traces = [
        {"start": cell, "path": _trace_flow_path_cells(flow_direction_cells, cell[0], cell[1], boundary_prepared, dem)[0]}
        for cell in top_zone_cells
    ]

    first_owner: dict = {}
    merged_starts: set = set()
    for trace in zone_traces:
        for cell in trace["path"]:
            owner = first_owner.get(cell)
            if owner is None:
                first_owner[cell] = trace["start"]
            elif owner != trace["start"]:
                merged_starts.add(trace["start"])
                merged_starts.add(owner)

    print(
        f"PART A -- zone's own {len(top_zone_cells)} member cell(s): {len(merged_starts)} of them "
        f"({len(merged_starts) / len(top_zone_cells) * 100:.1f}%) have a flow path that shares at least one "
        "downstream cell with another member cell's flow path before exiting -- "
        + (
            "the large majority converge onto a shared downstream path, consistent with one confluent band."
            if len(merged_starts) >= 0.5 * len(top_zone_cells)
            else "less than half converge -- this zone's own cells may not all funnel through one shared channel."
        )
    )
    print()

    # --- PART B: real valley branches (delineate_valleys(), coarser threshold) ---
    valleys = delineate_valleys(dem)
    all_branches = [
        (valley["id"], branch_index, branch)
        for valley in valleys
        for branch_index, branch in enumerate(valley["branches_rowcol"])
    ]
    total_branch_count = len(all_branches)

    qualifying = []
    for valley_id, branch_index, branch in all_branches:
        touches_zone = any(cell in zone_cells_set for cell in branch)

        # "Immediately upstream": delineate_valleys() traces each branch
        # only down to ITS OWN (coarser) contributing-area threshold, which
        # can stop just short of ever reaching the zone -- so this can't be
        # answered from the branch's own recorded cell list alone (any cell
        # of the branch landing in the zone is already "touches_zone" by
        # construction, making a same-list check redundant). Instead, take
        # ONE more real D8 step (flow_direction_cells, not the branch's own
        # truncated trace) past the branch's own outlet (its last recorded
        # cell) and check whether THAT step lands in the zone.
        immediately_upstream = False
        if not touches_zone:
            outlet_row, outlet_col = branch[-1]
            next_row, next_col = flow_direction_cells[outlet_row, outlet_col]
            next_row, next_col = int(next_row), int(next_col)
            immediately_upstream = next_row >= 0 and (next_row, next_col) in zone_cells_set

        if touches_zone or immediately_upstream:
            qualifying.append(
                {
                    "valley_id": valley_id,
                    "branch_index": branch_index,
                    "branch": branch,
                    "touches_zone": touches_zone,
                    "immediately_upstream": immediately_upstream,
                }
            )

    print(
        f"PART B -- delineate_valleys() found {len(valleys)} primary valley(s), {total_branch_count} branch(es) "
        f"total. {len(qualifying)} of those branch(es) have a cell within (or immediately upstream of) the "
        "top-ranked zone's footprint:"
    )
    for q in qualifying:
        outlet = q["branch"][-1]
        print(
            f"    valley id={q['valley_id']}, branch #{q['branch_index']}: "
            f"{'passes through' if q['touches_zone'] else 'flows directly into'} the zone, "
            f"own outlet cell=(row={outlet[0]}, col={outlet[1]})"
        )
    print()

    if len(qualifying) < 2:
        print(
            "Fewer than 2 qualifying branches -- a confluence needs at least 2 converging paths, so this "
            "check can't distinguish confluence from broad-hillside with only 0 or 1 branch reaching the zone.\n"
        )
        return

    branch_traces = []
    for q in qualifying:
        outlet_row, outlet_col = q["branch"][-1]
        path_cells, exited = _trace_flow_path_cells(flow_direction_cells, outlet_row, outlet_col, boundary_prepared, dem)
        branch_traces.append({"valley_id": q["valley_id"], "branch_index": q["branch_index"], "path": path_cells, "exited": exited})

    first_owner = {}
    confluence_events = []
    for trace in branch_traces:
        key = (trace["valley_id"], trace["branch_index"])
        for cell in trace["path"]:
            owner = first_owner.get(cell)
            if owner is None:
                first_owner[cell] = key
            elif owner != key:
                confluence_events.append((cell, owner, key))

    if confluence_events:
        example_cell, owner_a, owner_b = confluence_events[0]
        merged_branch_ids = {e[1] for e in confluence_events} | {e[2] for e in confluence_events}
        print(
            f"TRUE CONFLUENCE detected: {len(merged_branch_ids)} of {len(qualifying)} qualifying branches' "
            f"continued flow paths converge on a shared cell before exiting the parcel (e.g. valley "
            f"{owner_a} and valley {owner_b} both reach row={example_cell[0]}, col={example_cell[1]}) -- "
            "this supports the 'ridge to valley bottom, channels converging' read."
        )
    else:
        print(
            f"NO CONFLUENCE: none of the {len(qualifying)} qualifying branches' continued flow paths ever "
            "share a cell before exiting the parcel -- they run roughly parallel without merging, which "
            "points back toward the broad-hillside explanation instead of a true valley confluence."
        )
    print()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report pre/post survey-buffer-dilation percentile-band mask stats for the real reference property."
    )
    parser.add_argument(
        "--min-contributing-acres",
        type=float,
        default=MIN_VALLEY_CONTRIBUTING_AREA_ACRES,
        help=f"Override MIN_VALLEY_CONTRIBUTING_AREA_ACRES (the percentile-population FLOOR) for this run only "
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
        "--accumulation-percentile-low",
        type=float,
        default=VALLEY_ACCUMULATION_PERCENTILE_LOW,
        help=f"Override VALLEY_ACCUMULATION_PERCENTILE_LOW for this run only "
        f"(default: the current module constant, {VALLEY_ACCUMULATION_PERCENTILE_LOW}).",
    )
    parser.add_argument(
        "--accumulation-percentile-high",
        type=float,
        default=VALLEY_ACCUMULATION_PERCENTILE_HIGH,
        help=f"Override VALLEY_ACCUMULATION_PERCENTILE_HIGH for this run only "
        f"(default: the current module constant, {VALLEY_ACCUMULATION_PERCENTILE_HIGH}).",
    )
    parser.add_argument(
        "--zone-min-waist-meters",
        type=float,
        default=WATER_ZONE_MIN_WAIST_METERS,
        help=f"Override WATER_ZONE_MIN_WAIST_METERS for this run only "
        f"(default: the current module constant, {WATER_ZONE_MIN_WAIST_METERS}).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        main(
            min_contributing_acres=args.min_contributing_acres,
            buffer_meters=args.buffer_meters,
            accumulation_percentile_low=args.accumulation_percentile_low,
            accumulation_percentile_high=args.accumulation_percentile_high,
            zone_min_waist_meters=args.zone_min_waist_meters,
        )
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map ImageServer (DEM fetch) and production_area.py's own "
            "SSURGO/canopy/road data sources -- not a fully sandboxed environment."
        )
