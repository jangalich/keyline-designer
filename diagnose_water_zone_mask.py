"""
diagnose_water_zone_mask.py

Standalone, read-only diagnostic: reports the contributing-area ceiling
mask for compute_water_eligible_cells() (water_candidate_zones.py), then
breaks it down further by the real service-distance/boundary-setback gates
that function applies -- against the real reference property boundary (the
same coordinates render_layout_map.py's own __main__ block uses).

This does NOT modify compute_water_eligible_cells() itself, and does not
reimplement any of its gate logic independently -- the ceiling-mask section
calls the exact same building blocks that function already uses internally
(valley_delineation.get_flow_accumulation_for_dem(),
water_candidate_zones.MAX_VALLEY_CONTRIBUTING_AREA_ACRES); the
gate-breakdown section calls compute_water_eligible_cells() itself
directly, several times, with individual gate thresholds swapped for
"always pass" values (0/infinity) to isolate one real gate at a time --
the gate math itself is never duplicated, only which of the function's
own real checks can actually bind is varied per call. This covers all of
compute_water_eligible_cells()'s real gates: the absolute contributing-
area ceiling, on-parcel/boundary-setback, service-distance, canopy
(woody-vegetation root zone), and existing-road right-of-way. The real
canopy_root_zone_mask_utm/road_exclusion_union_utm this run actually
fetches (see below) are held REAL across every isolation call except the
ones specifically isolating canopy or road themselves, the same way the
contributing-area ceiling is already held real throughout.

THE PRODUCTION-ZONE EXCLUSION GATE IS GONE and this script no longer
isolates it: water-zone cells inside a production area's render fill are
eligible now, and whether the two uses may share ground is the designer's
call rather than a generation-time rule (see water_candidate_zones.py's
own module docstring for the full gate-to-preference reasoning). What
replaced its diagnostic value is the per-candidate canopy_overlap_pct /
road_overlap_pct and the production_area_relationships every candidate
already carries.

Every isolation call uses the REAL boundary_polygon_utm, never a
synthetic stand-in -- an earlier version of this section used a
synthetic boundary covering the DEM's entire extent (to make the
on-parcel/setback gate a no-op) for the service-distance isolation calls
specifically. The on-parcel containment check has no override at all
(boundary_polygon_utm is required, not optional) -- only its SETBACK
distance can be disabled (min_boundary_setback_meters=0.0), so the
service-distance isolation below is "on-parcel required, setback
waived," not a pure isolation from every other gate.

The zone section below (elevation ranges, confluence check) calls
water_candidate_zones.find_candidate_zones() directly -- the real, full
pipeline entry point, including its keypoint/accumulation nomination ->
level-pool delineation -> bounded-opening wiring -- rather than
reimplementing nomination/delineation independently a second time.
find_candidate_zones() returns EVERY surviving candidate (generation is uncapped).

IT ALSO WRITES water_candidates.geojson, beside its terminal output: every
candidate, casualty, keypoint marker, wall anchor (failed walks included)
and traced stem walk in one feature_schema-compliant file, for VISUAL
REVIEW over aerial imagery. See _export_candidate_geojson() for the layer
list and for why a dropped candidate is drawn as a point.

THAT EXPORT IS A DIAGNOSTIC CONSUMER AND THE WIRING IS FINISHED.
render_layout_map.py and every batch consumer are untouched and are meant
to stay that way: the batch map draws the ONE selected rank-1 zone by
design, because a printed plan carrying nine overlapping candidate
polygons is not a plan. Multi-candidate display belongs to the
interactive wizard, where a user can toggle and compare. Nobody should
"finish the wiring" by pointing the batch renderer at this file.

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

--max-contributing-acres overrides MAX_VALLEY_CONTRIBUTING_AREA_ACRES for
this run only (default: the current module constant) -- for experimentation
while tuning the ceiling; the actual module constant itself is never
changed. The service-distance/boundary-setback thresholds used in the gate
breakdown are always the module's real current defaults
(MAX_SERVICE_DISTANCE_METERS/MIN_BOUNDARY_SETBACK_METERS -- the last now
0.0), not separately overridable here. There is no minimum-service-distance
gate any more (the former 10 m "too close to production" rule was removed),
so the gate breakdown reports "too far" but no "too close".
"""

import argparse
import json

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, Point, Polygon, mapping
from shapely.prepared import prep

from dem_data import get_dem_for_boundary
from production_area import (
    _fetch_road_exclusion_union_utm,
    get_required_tree_root_zone_mask_utm,
    identify_production_areas,
)
from raster_grid import (
    SQUARE_METERS_PER_ACRE,
    cell_area_acres,
    connected_components,
    pixel_center_xy,
)
from valley_delineation import (
    compute_flow_accumulation,
    compute_flow_direction,
    delineate_valleys,
    fill_depressions,
    get_flow_accumulation_for_dem,
    get_flow_direction_for_dem,
)
from keypoint_detection import detect_keypoints
from feature_schema import (
    CONFIDENCE_LOW,
    make_feature,
    make_feature_collection,
    validate_feature_collection,
)
from water_suitability import score_water_zones, summarize_water_suitability
from valley_level_pool import POOL_REFERENCE_HEIGHT_METERS, STEM_DIRECTION_WINDOW_CELLS
# The road gate reads the SINGLE shared buffer definition (water's former
# separate per-module road-buffer constant was deleted -- see the shared
# constant's own docstring in farm_roads_data.py).
from farm_roads_data import ROAD_EXCLUSION_BUFFER_METERS
from water_candidate_zones import (
    MAX_SERVICE_DISTANCE_METERS,
    MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
    MAX_WALL_SEARCH_DOWNSTREAM_METERS,
    WATER_ACCUMULATION_SEED_BUDGET,
    MIN_BOUNDARY_SETBACK_METERS,
    MIN_WATER_ZONE_AREA_ACRES,
    WATER_ZONE_CANOPY_BUFFER_METERS,
    _CANOPY_CHECK_UNCHECKED,
    _ROAD_CHECK_UNCHECKED,
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
    max_contributing_acres: float = MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
) -> None:
    print("diagnose_water_zone_mask.py -- contributing-area ceiling mask + gate breakdown\n")
    print(f"Property: real reference boundary, {len(PROPERTY_BOUNDARY)} vertices")
    print(f"Boundary coordinates (lon, lat): {PROPERTY_BOUNDARY}\n")
    print(f"max_contributing_acres (this run)        = {max_contributing_acres} acres "
          f"(module default: {MAX_VALLEY_CONTRIBUTING_AREA_ACRES}) -- the ABSOLUTE ceiling; a cell is "
          "eligible iff its own contributing area is AT OR BELOW this, with NO lower bound")
    print(f"boundary setback (module default, not overridable this run) = "
          f"{MIN_BOUNDARY_SETBACK_METERS}m (zeroed -- inert)")
    print("production-overlap exclusion: DELETED (gate, constant and parameter) -- a water-zone "
          "cell inside a production area's render fill is eligible now; see water_candidate_zones.py's "
          "module docstring\n")

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
            PROPERTY_BOUNDARY, dem, buffer_meters=ROAD_EXCLUSION_BUFFER_METERS
        )
        if road_exclusion_union_utm is None:
            print("Road exclusion fetch succeeded: no roads found nearby (checked, genuinely none -- not the "
                  "same as unchecked).\n")
        else:
            print(f"Road exclusion union fetched (shared ROAD_EXCLUSION_BUFFER_METERS={ROAD_EXCLUSION_BUFFER_METERS}m).\n")
    except Exception as e:
        print(
            f"Road exclusion fetch failed ({e}) -- proceeding with the road gate UNCHECKED for this diagnostic "
            "run (this gate degrades gracefully in the real pipeline too, same behavior here).\n"
        )
        road_exclusion_union_utm = _ROAD_CHECK_UNCHECKED

    # The same absolute contributing-area ceiling compute_water_eligible_
    # cells() applies internally (a cell is eligible iff its own
    # contributing area is AT OR BELOW the ceiling, with no lower bound),
    # computed here directly and reported before any spatial gate. Uses this
    # run's max_contributing_acres (the module constant unless overridden
    # via --max-contributing-acres).
    flow_accumulation_cells = get_flow_accumulation_for_dem(dem)
    area_per_cell = cell_area_acres(dem)
    max_contributing_cells = max_contributing_acres / area_per_cell
    ceiling_mask = flow_accumulation_cells <= max_contributing_cells

    print(f"(max_contributing_acres = {max_contributing_acres} acres -> "
          f"{max_contributing_cells:.2f} contributing cells at this DEM's resolution -- the CEILING)\n")

    _report_mask_stats(
        "CEILING-qualifying mask (contributing area <= ceiling, before any spatial gate)", ceiling_mask, dem
    )

    # --- direct on-parcel vs boundary-setback split, over the ceiling  ---
    # --- mask (unconditional, no dependence on production areas). The   ---
    # --- gate-breakdown section further down only reports the COMBINED  ---
    # --- on-parcel-and-setback result; this checks each of the two      ---
    # --- tests separately, directly against boundary_polygon_utm, so a  ---
    # --- "0 combined" result can't hide which of the two is responsible.
    print("=== On-parcel vs boundary-setback (direct, separated check) ===\n")

    boundary_prepared = prep(boundary_polygon_utm)
    boundary_line = boundary_polygon_utm.boundary

    on_parcel_mask = np.zeros(ceiling_mask.shape, dtype=bool)
    setback_survivor_mask = np.zeros(ceiling_mask.shape, dtype=bool)

    for r, c in np.argwhere(ceiling_mask):
        r, c = int(r), int(c)
        x, y = pixel_center_xy(dem, r, c)
        point = Point(x, y)
        if boundary_prepared.contains(point):
            on_parcel_mask[r, c] = True
            if boundary_line.distance(point) >= MIN_BOUNDARY_SETBACK_METERS:
                setback_survivor_mask[r, c] = True

    # 1. On-parcel alone, no setback applied.
    _report_mask_stats(
        "1. On-parcel cells within the ceiling mask (NO setback applied)",
        on_parcel_mask, dem,
    )

    # 2. Of those on-parcel cells, how many ALSO clear the real setback.
    _report_mask_stats(
        f"2. Of those, cells ALSO clearing MIN_BOUNDARY_SETBACK_METERS={MIN_BOUNDARY_SETBACK_METERS}m "
        "from the boundary",
        setback_survivor_mask, dem,
    )
    print(
        f"   (MIN_BOUNDARY_SETBACK_METERS is {MIN_BOUNDARY_SETBACK_METERS}m -- zeroed and therefore inert: "
        "every on-parcel cell clears a 0m setback, so (1) and (2) are identical. The on-parcel containment "
        "test is a SEPARATE, still-active gate, unaffected by the zeroed setback.)\n"
    )

    if not production_areas_available:
        print(
            "Skipping the service-distance/boundary-setback gate breakdown below -- it needs real "
            "production areas, which failed to fetch above.\n"
        )
        return

    # =================================================================
    # THE MASK IS THREE GATES NOW: ceiling, on-parcel, inert setback.
    # Canopy, existing roads and max-service-distance are no longer gates
    # at all -- they are MEASUREMENTS on a delineated candidate. Their
    # figures are still printed below, because "how much of this parcel's
    # drainage sits under canopy" remains the useful context it always
    # was, but they are labelled as CONTEXT/OVERLAP rather than as gates,
    # and they no longer pretend to be exclusions.
    #
    # A LABELLING BUG IS FIXED HERE, and it is worth stating because the
    # numbers it produced looked plausible. The previous version derived
    # each "excluded by X" figure by calling compute_water_eligible_cells()
    # with X real and the OTHER gates relaxed, then complementing against
    # on_parcel_mask. That is correct only if the other gates are actually
    # relaxed -- and for the TOO FAR line they were not: canopy and road
    # were held REAL in that call, so
    #
    #     too_far_excluded  ==  on_parcel & (canopy | road | too_far)
    #
    # i.e. the "excluded as TOO FAR" line printed the UNION of all three
    # exclusions under a distance label. On the reference property, where
    # the road union excluded nothing and every on-parcel cell was within
    # service distance, that union collapses to exactly the canopy set --
    # which is why the canopy and TOO FAR lines both read 465 cells /
    # 2.868 acres. THE CANOPY FIGURE WAS THE REAL ONE; the TOO FAR figure
    # was the canopy set wearing the wrong name. The old "internal
    # consistency check" could never have caught it, because both of its
    # sides carried the same contamination -- so it is deleted along with
    # the calls it validated.
    #
    # Every figure below is now computed DIRECTLY from its own layer --
    # not by complementing a relaxed call -- so no line can silently be
    # another line's set. The pairwise intersections are printed
    # underneath to make that provable rather than asserted.
    # =================================================================
    print("=== Nomination mask: the THREE gates that decide where an anchor may sit ===\n")

    three_gate_mask = compute_water_eligible_cells(
        dem, boundary_polygon_utm,
        max_valley_contributing_area_acres=max_contributing_acres,
        min_boundary_setback_meters=MIN_BOUNDARY_SETBACK_METERS,
    )
    _report_mask_stats(
        f"NOMINATION MASK (ceiling <= {max_contributing_acres} ac AND on-parcel AND setback "
        f"{MIN_BOUNDARY_SETBACK_METERS}m [inert])",
        three_gate_mask, dem,
    )

    canopy_covered_mask = (
        three_gate_mask & np.asarray(canopy_root_zone_mask_utm, dtype=bool)
        if canopy_available
        else np.zeros_like(three_gate_mask)
    )
    road_prepared = prep(road_exclusion_union_utm) if road_exclusion_union_utm is not None else None
    road_covered_mask = np.zeros_like(three_gate_mask)
    beyond_service_mask = np.zeros_like(three_gate_mask)
    for r, c in np.argwhere(three_gate_mask):
        r, c = int(r), int(c)
        point = Point(*pixel_center_xy(dem, r, c))
        if road_prepared is not None and road_prepared.contains(point):
            road_covered_mask[r, c] = True
        if all(
            point.distance(patch["polygon_utm"]) > MAX_SERVICE_DISTANCE_METERS
            for patch in production_areas
        ):
            beyond_service_mask[r, c] = True

    # THE HEADLINE BEFORE/AFTER NUMBER FOR THE MASK SHRINK.
    old_gate_mask = three_gate_mask & ~canopy_covered_mask & ~road_covered_mask & ~beyond_service_mask
    _gained = int(three_gate_mask.sum()) - int(old_gate_mask.sum())
    print("--- BEFORE/AFTER: what the deleted canopy/road/service gates used to remove ---")
    print(f"  nomination mask BEFORE (6 gates):  {int(old_gate_mask.sum())} cells, "
          f"{int(old_gate_mask.sum()) * cell_area_acres(dem):.3f} acres")
    print(f"  nomination mask NOW    (3 gates):  {int(three_gate_mask.sum())} cells, "
          f"{int(three_gate_mask.sum()) * cell_area_acres(dem):.3f} acres")
    print(f"  GAINED by the shrink:              {_gained} cells, "
          f"{_gained * cell_area_acres(dem):.3f} acres -- ground on which an anchor may now be "
          "nominated and could not before\n")

    print("=== CONTEXT / OVERLAP measurements (NOT gates -- nothing below excludes a cell) ===\n")
    _report_cell_count(
        f"Under the woody-vegetation root zone (WATER_ZONE_CANOPY_BUFFER_METERS="
        f"{WATER_ZONE_CANOPY_BUFFER_METERS}m)"
        + ("" if canopy_available else " [canopy fetch FAILED above -- reads 0, NOT a real result]"),
        canopy_covered_mask, dem,
    )
    _report_cell_count(
        f"Inside an existing road right-of-way (shared ROAD_EXCLUSION_BUFFER_METERS="
        f"{ROAD_EXCLUSION_BUFFER_METERS}m)"
        + ("" if road_exclusion_union_utm is not None else " [no mapped road nearby -- a real 0]"),
        road_covered_mask, dem,
    )
    _report_cell_count(
        f"Beyond MAX_SERVICE_DISTANCE_METERS={MAX_SERVICE_DISTANCE_METERS}m from EVERY production area",
        beyond_service_mask, dem,
    )
    print()

    # PROVABLY DISTINCT: three independent layers cannot be validated by
    # their totals alone -- that is exactly how the old bug hid -- so print
    # the pairwise intersections. Two lines reporting the same count with a
    # full-overlap intersection are the same set wearing two names.
    print("  Pairwise overlaps (so no two lines above can silently be the same set):")
    for _label_a, _mask_a, _label_b, _mask_b in (
        ("canopy", canopy_covered_mask, "road", road_covered_mask),
        ("canopy", canopy_covered_mask, "beyond-service", beyond_service_mask),
        ("road", road_covered_mask, "beyond-service", beyond_service_mask),
    ):
        _both = int((_mask_a & _mask_b).sum())
        _a, _b = int(_mask_a.sum()), int(_mask_b.sum())
        _note = ""
        if _a and _a == _b == _both:
            _note = "  <-- IDENTICAL SETS: check the labelling before trusting either figure"
        print(f"    {_label_a} & {_label_b}: {_both} cells (of {_a} / {_b}){_note}")
    print()

    # find_candidate_zones() returns EVERY surviving candidate now -- there
    # is no generation cap. Keypoints are attempted in catchment order and
    # family 2 then adds up to WATER_ACCUMULATION_SEED_BUDGET survivors of
    # its own. The nomination record -- one outcome per keypoint (with its
    # distance_outside_boundary_m, which is what makes an off-parcel
    # anchor's outcome legible), one entry per family-2 seed, each with its
    # reason code -- is what explains a short or empty list.
    #
    # The D8 field and the keypoint list are derived HERE and handed down,
    # rather than left to find_candidate_zones() to self-compute: the
    # GeoJSON export below needs both (it draws every keypoint marker and
    # walks the flow field to trace each stem), and detecting keypoints a
    # second time for the export would be a second answer to the same
    # question. Same forward-what-you-already-have pattern
    # pipeline_context.py uses, and it keeps detect_keypoints() to exactly
    # one run in this script.
    # Delineated once, here, and reused by PART B below -- it used to be
    # delineated there, which was fine while nothing above needed it.
    valleys = delineate_valleys(dem)
    _filled = fill_depressions(dem["array"])
    flow_to_row, flow_to_col = compute_flow_direction(_filled, dem["resolution_meters"])
    flow_accumulation_grid = compute_flow_accumulation(_filled, flow_to_row, flow_to_col)
    keypoints = detect_keypoints(
        dem,
        boundary_polygon_utm,
        flow_to_row=flow_to_row,
        flow_to_col=flow_to_col,
        flow_accumulation=flow_accumulation_grid,
        filled=_filled,
        valleys=valleys,
    )

    nomination_diagnostics: dict = {}
    zones = find_candidate_zones(
        dem, production_areas, boundary_polygon_utm,
        max_valley_contributing_area_acres=max_contributing_acres,
        canopy_root_zone_mask_utm=canopy_root_zone_mask_utm,
        road_exclusion_union_utm=road_exclusion_union_utm,
        keypoints=keypoints,
        valleys=valleys,
        filled=_filled,
        flow_to_row=flow_to_row,
        flow_to_col=flow_to_col,
        flow_accumulation=flow_accumulation_grid,
        diagnostics=nomination_diagnostics,
    )
    print(f"find_candidate_zones() returned {len(zones)} candidate(s) -- generation is UNCAPPED; family 2 "
          f"is bounded at {WATER_ACCUMULATION_SEED_BUDGET} survivor(s).")
    for zone in zones:
        left, right = zone["abutments"]["left"], zone["abutments"]["right"]

        def _side(side_result, crosses):
            if crosses:
                return f"TRUNCATED at a major drainage {side_result['major_drainage_distance_m']}m out"
            if side_result["found"]:
                return f"{side_result['lateral_distance_m']}m"
            return "NOT FOUND"

        print(
            f"  - zone {zone['id']}: nominated_by={zone['nominated_by']} "
            f"keypoint_id={zone['keypoint_id']} valley_id={zone['valley_id']} "
            f"anchor={zone['anchor_rowcol']} "
            f"off_parcel={zone['anchor_off_parcel']}"
            + (f" ({zone['anchor_distance_outside_boundary_m']}m outside)"
               if zone["anchor_off_parcel"] else "")
            + f" cells={len(zone['cells'])} "
            f"acres={zone['polygon_utm'].area / SQUARE_METERS_PER_ACRE:.3f} "
            f"abutments=(L {_side(left, zone['dam_band_crosses_major_drainage_left'])}, "
            f"R {_side(right, zone['dam_band_crosses_major_drainage_right'])}) "
            f"canopy={zone['canopy_overlap_pct']}% road={zone['road_overlap_pct']}% "
            f"flags={zone['flags']}"
        )
        pool = zone["level_pool"]
        print(
            f"      stem traced {pool['stem_upstream_length_m']}m upstream of the anchor; local stem "
            f"bearing at the anchor {pool['anchor_bearing_deg']} deg"
            + ("  [DEGENERATE -- no window separated two stem cells]"
               if pool["stem_direction_degenerate"] else "")
        )
        for station in pool["stations"]:
            if station["status"] != "measured":
                print(
                    f"      station {station['station_index']} target {station['offset_upstream_m']}m: "
                    f"{station['status'].upper()} -- the traced stem ends at "
                    f"{station['along_stem_distance_m']}m. NOT a dry cross-section: there is no channel "
                    "here to measure. (Short stems are valley_delineation.py's flat-tie limitation "
                    "surfacing -- flagged, not fixed here.)"
                )
                continue
            print(
                f"      station {station['station_index']} target {station['offset_upstream_m']}m -> "
                f"stem cell {station['stem_rowcol']} at along-stem {station['along_stem_distance_m']}m, "
                f"channel {station['channel_elevation_m']}m, local bearing {station['bearing_deg']} deg: "
                f"flooded width {station['flooded_width_m']}m, flooded cross-section "
                f"{station['flooded_cross_section_area_m2']}m^2"
            )
    print(
        "\nPer-keypoint nomination outcomes (reason codes, off-parcel distance, and THE WALL WALK "
        f"-- downstream to the first cell {POOL_REFERENCE_HEIGHT_METERS}m below the keypoint, "
        f"searching at most {MAX_WALL_SEARCH_DOWNSTREAM_METERS}m):"
    )
    for outcome in nomination_diagnostics.get("keypoint_outcomes", []):
        print(
            f"  - keypoint {outcome['keypoint_id']} (valley {outcome['valley_id']}, "
            f"{outcome['contributing_acres']:.2f} ac, on_parcel={outcome['on_parcel']}, "
            f"{outcome['distance_outside_boundary_m']}m outside): {outcome['outcome']} "
            f"-> candidate {outcome['candidate_id']}"
            + (f"  flags={outcome['flags']}" if outcome["flags"] else "")
        )
        # THE WALL WALK. The keypoint is the pool's TAIL; the wall sits a
        # full POOL_REFERENCE_HEIGHT_METERS below it, found by walking
        # downstream. Both positions and both elevations are printed
        # because the whole point of this change is that they are DIFFERENT
        # places -- reading only the anchor would hide the walk entirely.
        if outcome["wall_walk_end_reason"] is None:
            print(
                "      wall walk: NOT ATTEMPTED -- rejected before the walk "
                f"({outcome['outcome']})"
            )
        elif outcome["wall_offset_downstream_m"] is not None:
            print(
                f"      wall walk: keypoint {tuple(outcome['keypoint_rowcol'])} at "
                f"{outcome['keypoint_elevation_m']}m -> wall {tuple(outcome['anchor_rowcol'])} at "
                f"{outcome['anchor_elevation_m']}m: {outcome['wall_offset_downstream_m']}m downstream, "
                f"{outcome['wall_drop_m']}m of drop ({outcome['wall_walk_end_reason']})"
            )
        else:
            # A FAILED walk. No partial-height fallback exists, so the walk
            # reports where it died and why rather than quietly anchoring
            # somewhere shallower than the design height.
            print(
                f"      wall walk: FAILED -- keypoint {tuple(outcome['keypoint_rowcol'])} at "
                f"{outcome['keypoint_elevation_m']}m walked to {outcome['wall_walk_end_rowcol']} and "
                f"found only {outcome['wall_drop_m']}m of the required "
                f"{POOL_REFERENCE_HEIGHT_METERS}m before "
                f"'{outcome['wall_walk_end_reason']}'"
                + ("  (flat_tie_sentinel is valley_delineation.py's flat-tie limitation surfacing -- "
                   "a filled depression is unroutable under strict-slope D8; flagged, not fixed here)"
                   if outcome["wall_walk_end_reason"] == "flat_tie_sentinel" else "")
            )
    print(
        f"Family-2 (accumulation) seed log -- {nomination_diagnostics.get('accumulation_survivors')} "
        f"survivor(s) from {len(nomination_diagnostics.get('accumulation_seeds', []))} attempt(s)"
        + ("  [ATTEMPT LIMIT REACHED]"
           if nomination_diagnostics.get("accumulation_attempt_limit_reached") else "")
        + ":"
    )
    for seed in nomination_diagnostics.get("accumulation_seeds", []):
        print(
            f"  - anchor {seed['anchor_rowcol']} (accum {seed['flow_accumulation_cells']:.0f} cells): "
            f"{seed['outcome']} -> candidate {seed['candidate_id']}"
        )
    print()

    # The real, full pipeline entry point itself -- not a re-implementation
    # of clustering/connected-growth/whole-zone scoring. zones carries every
    # field find_candidate_zones() itself produces (cells, polygon_utm,
    # primary_production_area_relationship, ...) so the sections below report
    # on the REAL thing, not a diagnostic-only approximation of it.
    ranked_zones = _report_zone_elevation_ranges(dem, boundary_polygon_utm, zones)

    # --- GeoJSON export, for review over aerial imagery ---
    #
    # Scored here so the export can carry ranks. score_water_zones() is
    # PURE -- no network, no DEM re-read -- so this costs nothing but the
    # arithmetic, and it runs with no soil fetch, which the export's own
    # confidence_notes state plainly rather than letting a neutral soil
    # default pass for a measurement.
    scored_zones = score_water_zones(zones, dem, production_areas=production_areas)
    print("=== GeoJSON export for visual review ===\n")
    print(summarize_water_suitability(scored_zones))
    export = _export_candidate_geojson(
        dem, scored_zones, nomination_diagnostics, keypoints, flow_to_row, flow_to_col,
        # Passed EXPLICITLY rather than left to the parameter default:
        # a module constant used as a default argument is bound once at
        # import, so it stops being configurable the moment anything
        # wants to change it (a test redirecting the write, a future
        # --output flag). Reading it here reads it at call time.
        path=WATER_CANDIDATES_GEOJSON_PATH,
    )
    print(
        f"\nWrote {export['feature_count']} feature(s) to {export['path']} "
        "(feature_schema-validated). Layer/status breakdown:"
    )
    for (layer, status), count in sorted(export["by_layer_status"].items()):
        print(f"  {layer} [{status}]: {count}")
    print()

    if not ranked_zones:
        print("No surviving zone to run the confluence check against -- skipping.\n")
        return

    top_zone = ranked_zones[0]
    # valleys is THREADED IN, not re-delineated. This function used to call
    # delineate_valleys() itself; main() now delineates once at the top and
    # forwards the one list into keypoint detection, generation and here.
    # It is a required parameter rather than an optional self-computing one
    # deliberately: this is a private helper with exactly one caller, so a
    # missing argument should be a loud TypeError at the call site, not a
    # silent second delineation of a DEM that was already traced.
    _report_confluence_check(dem, boundary_polygon_utm, top_zone["cells"], valleys)


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
    valleys: list[dict],
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
    # `valleys` arrives as a PARAMETER. main() delineates once at the top
    # and forwards the one list into keypoint detection, generation and
    # this check, so there is exactly one delineate_valleys() call site in
    # this script -- asserted by call count in
    # test_diagnose_water_zone_mask.py, not assumed.
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


# --- GeoJSON export for visual review -----------------------------------

# Where the export lands, beside this script's terminal output.
WATER_CANDIDATES_GEOJSON_PATH = "water_candidates.geojson"

# Every feature carries one of these, so a viewer can style survivors and
# casualties differently without parsing reason codes.
EXPORT_STATUS_NOMINATED = "nominated"
EXPORT_STATUS_DROPPED = "dropped"


def _wgs84(dem: dict, geometry) -> dict:
    """Shapely geometry in dem['crs'] -> a WGS84 GeoJSON geometry dict,
    which is the only thing feature_schema.make_feature() accepts."""
    return transform_geom(dem["crs"], "EPSG:4326", mapping(geometry))


def _point_wgs84(dem: dict, rowcol) -> dict:
    return _wgs84(dem, Point(*pixel_center_xy(dem, int(rowcol[0]), int(rowcol[1]))))


def _station_table(zone: dict) -> list[dict]:
    """A compact per-station row set: what the cross-section sampler
    measured, at what offset, and -- crucially -- its STATUS, so a viewer
    can tell an unmeasured station from a dry one. Widths and areas are
    None on an unreachable station by valley_level_pool.py's own contract,
    and they are carried through as None rather than zeroed."""
    return [
        {
            "station_index": st["station_index"],
            "offset_upstream_m": st["offset_upstream_m"],
            "status": st["status"],
            "flooded_width_m": st["flooded_width_m"],
            "flooded_cross_section_area_m2": st["flooded_cross_section_area_m2"],
        }
        for st in zone["level_pool"]["stations"]
    ]


def _stem_path_cells(flow_to_row, flow_to_col, start, end, max_steps: int = 200):
    """The traced flow path from a keypoint down to its wall anchor, as
    grid cells. Walks the SAME D8 field _find_wall_site() walked, so the
    line drawn on the map is the line the walk actually took -- not a
    straight segment between the two endpoints, which would imply a
    channel that is not there.

    Returns [] when the walk cannot reach `end` (a failed walk has no
    path to draw; its own dead-end position is exported as a wall-anchor
    point instead)."""
    path = [tuple(start)]
    current = tuple(start)
    for _ in range(max_steps):
        if current == tuple(end):
            return path
        tr = int(flow_to_row[current[0], current[1]])
        tc = int(flow_to_col[current[0], current[1]])
        if tr < 0:
            return []
        current = (tr, tc)
        path.append(current)
    return []


def _export_candidate_geojson(
    dem: dict,
    scored_zones: list[dict],
    nomination_diagnostics: dict,
    keypoints: list[dict],
    flow_to_row,
    flow_to_col,
    path: str = WATER_CANDIDATES_GEOJSON_PATH,
) -> dict:
    """
    Writes every candidate, every casualty and every piece of the
    nomination machinery to one feature_schema-compliant GeoJSON file, for
    VISUAL REVIEW over aerial imagery. This is the merge gate for the
    multi-candidate work: a reader opens the file on top of the property
    and sees, in one picture, which valleys became candidates, which
    keypoints produced nothing and why, and where each wall would stand.

    THIS IS A DIAGNOSTIC CONSUMER, DELIBERATELY, AND THE WIRING IS
    FINISHED. render_layout_map.py and every batch consumer are untouched
    and are meant to stay that way: the batch map draws the ONE selected
    rank-1 zone by design, because a printed plan with nine overlapping
    candidate polygons on it is not a plan. Multi-candidate display is the
    interactive wizard's job, where a user can toggle and compare. Nobody
    should "finish the wiring" by pointing the batch renderer at this
    file.

    Six layers, every feature carrying `status` (nominated | dropped) and,
    where dropped, the reason code that stopped it:

      water_candidate_zone      -- surviving zone polygons, with rank,
                                   score, factors, basin sub-scores,
                                   confidence, overlaps and a station table
      water_candidate_dropped   -- casualties (see the geometry note below)
      water_keypoint            -- every detected keypoint, survivor or not
      water_wall_anchor         -- wall sites, INCLUDING failed walks at
                                   the position where the walk died
      water_stem_walk           -- the traced keypoint -> wall path
      water_accumulation_seed   -- dropped family-2 seeds

    GEOMETRY IS NOT RETAINED ON A DROP, and this export does not add
    retention to fix that. find_candidate_zones() builds a candidate's
    polygon and then returns (None, reason, flags) on every drop path, so
    by the time the outcome reaches a diagnostic the geometry is gone --
    including for the boundary-clipped slivers that are the most
    interesting casualties on the reference property. A dropped candidate
    is therefore exported as its ANCHOR POINT with its reason code, and
    the feature says so in its own confidence_notes. Retaining drop-time
    geometry is a generation-side change with its own consequences for the
    zone contract, and it belongs in its own branch rather than being
    smuggled in behind an export.
    """
    features: list[dict] = []
    keypoints_by_id = {int(k["id"]): k for k in keypoints}
    outcomes = nomination_diagnostics.get("keypoint_outcomes", [])
    outcome_by_keypoint_id = {o["keypoint_id"]: o for o in outcomes}

    scoring_note = (
        "Scores here come from water_suitability.score_water_zones() run WITHOUT a soil fetch, so "
        "soil_water_holding_factor sits at its neutral unavailable default for every candidate and "
        "confidence is capped accordingly. Gravity and basin shape are real. This is a visual-review "
        "export from a read-only diagnostic, not the pipeline's own scored output."
    )

    # --- layer 1: surviving candidates ---
    for zone in scored_zones:
        features.append(
            make_feature(
                feature_id=f"water-candidate-zone-{zone['id']}",
                geometry=zone["geometry_wgs84"],
                layer="water_candidate_zone",
                label=f"Candidate {zone['id']} (rank {zone['rank']}, {zone['suitability_score']}/100)",
                confidence=zone["confidence"],
                confidence_notes=scoring_note + " " + zone["confidence_notes"],
                extra_properties={
                    "status": EXPORT_STATUS_NOMINATED,
                    "zone_id": zone["id"],
                    "rank": zone["rank"],
                    "suitability_score": zone["suitability_score"],
                    "gravity_feed_factor": zone["gravity_feed_factor"],
                    "soil_water_holding_factor": zone["soil_water_holding_factor"],
                    "basin_shape_factor": zone["basin_shape_factor"],
                    "basin_enclosure_score": zone["basin_enclosure_score"],
                    "basin_persistence_score": zone["basin_persistence_score"],
                    "basin_persistence_ratio": zone["basin_persistence_ratio"],
                    "basin_wall_economy_score": zone["basin_wall_economy_score"],
                    "nominated_by": zone["nominated_by"],
                    "keypoint_id": zone["keypoint_id"],
                    "valley_id": zone["valley_id"],
                    "area_acres": round(zone["polygon_utm"].area / SQUARE_METERS_PER_ACRE, 4),
                    "flags": list(zone["flags"]),
                    "abutment_found_left": zone["abutment_found_left"],
                    "abutment_found_right": zone["abutment_found_right"],
                    "dam_band_crosses_major_drainage_left": zone["dam_band_crosses_major_drainage_left"],
                    "dam_band_crosses_major_drainage_right": zone["dam_band_crosses_major_drainage_right"],
                    "dam_band_width_m": zone["level_pool"]["dam_band_width_m"],
                    "canopy_overlap_pct": zone["canopy_overlap_pct"],
                    "road_overlap_pct": zone["road_overlap_pct"],
                    "production_overlap_pct": zone["production_overlap_pct"],
                    "has_service_relationship": zone["has_service_relationship"],
                    "wall_offset_downstream_m": zone["wall_offset_downstream_m"],
                    "anchor_rowcol": list(zone["anchor_rowcol"]),
                    "keypoint_rowcol": list(zone["keypoint_rowcol"]) if zone["keypoint_rowcol"] else None,
                    "stations": _station_table(zone),
                },
            )
        )

    # --- layer 2: dropped candidates (anchor points, see the docstring) ---
    dropped_note = (
        "GEOMETRY NOT RETAINED. find_candidate_zones() discards a candidate's polygon on every drop "
        "path, so this casualty is drawn at its ANCHOR CELL rather than as the footprint it would "
        "have had -- including where the drop was a boundary clip that left only a sliver. The "
        "reason code says what stopped it; the shape it would have covered is not recoverable from "
        "this module's output and this export deliberately does not add retention to make it so."
    )
    for outcome in outcomes:
        if outcome["candidate_id"] is not None or outcome["anchor_rowcol"] is None:
            continue
        features.append(
            make_feature(
                feature_id=f"water-candidate-dropped-keypoint-{outcome['keypoint_id']}",
                geometry=_point_wgs84(dem, outcome["anchor_rowcol"]),
                layer="water_candidate_dropped",
                label=f"Dropped candidate from keypoint {outcome['keypoint_id']} ({outcome['outcome']})",
                confidence=CONFIDENCE_LOW,
                confidence_notes=dropped_note,
                extra_properties={
                    "status": EXPORT_STATUS_DROPPED,
                    "outcome": outcome["outcome"],
                    "keypoint_id": outcome["keypoint_id"],
                    "valley_id": outcome["valley_id"],
                    "flags": list(outcome["flags"]),
                    "anchor_rowcol": list(outcome["anchor_rowcol"]),
                    "geometry_is_anchor_point_only": True,
                },
            )
        )

    # --- layer 3: every keypoint marker ---
    for keypoint in keypoints:
        outcome = outcome_by_keypoint_id.get(int(keypoint["id"]))
        features.append(
            make_feature(
                feature_id=f"water-keypoint-{int(keypoint['id'])}",
                geometry=_wgs84(dem, keypoint["point_utm"]),
                layer="water_keypoint",
                label=f"Keypoint {int(keypoint['id'])} (valley {keypoint['valley_id']})",
                confidence=CONFIDENCE_LOW,
                confidence_notes=(
                    "A keypoint is the POOL'S TAIL, not its wall -- the wall sits downstream, at the "
                    "matching water_wall_anchor feature. DEM-derived, not surveyed."
                ),
                extra_properties={
                    "status": (
                        EXPORT_STATUS_NOMINATED
                        if outcome is not None and outcome["candidate_id"] is not None
                        else EXPORT_STATUS_DROPPED
                    ),
                    "keypoint_id": int(keypoint["id"]),
                    "valley_id": keypoint["valley_id"],
                    "contributing_acres": float(keypoint["contributing_acres"]),
                    "on_parcel": bool(keypoint.get("on_parcel", True)),
                    "distance_outside_boundary_m": float(keypoint.get("distance_outside_boundary_m", 0.0)),
                    "outcome": outcome["outcome"] if outcome is not None else None,
                    "candidate_id": outcome["candidate_id"] if outcome is not None else None,
                },
            )
        )

    # --- layers 4 and 5: wall anchors (incl. failed walks) + stem walks ---
    for outcome in outcomes:
        keypoint = keypoints_by_id.get(outcome["keypoint_id"])
        if keypoint is None:
            continue
        walked = outcome["wall_walk_end_reason"] is not None
        if not walked:
            continue
        found = outcome["anchor_rowcol"] is not None
        end_cell = outcome["anchor_rowcol"] if found else outcome["wall_walk_end_rowcol"]
        if end_cell is None:
            continue
        # STATUS TRACKS THE CANDIDATE, NOT THE WALK. A wall site can be
        # found perfectly well on a nomination that is then rejected for
        # separation or dropped at the area floor; colouring that anchor
        # as a survivor would put a wall on the map where no candidate
        # exists. wall_site_found carries the walk's own outcome separately.
        nomination_status = (
            EXPORT_STATUS_NOMINATED if outcome["candidate_id"] is not None else EXPORT_STATUS_DROPPED
        )
        features.append(
            make_feature(
                feature_id=f"water-wall-anchor-{outcome['keypoint_id']}",
                geometry=_point_wgs84(dem, end_cell),
                layer="water_wall_anchor",
                label=(
                    f"Wall site for keypoint {outcome['keypoint_id']}"
                    if found
                    else f"FAILED wall walk from keypoint {outcome['keypoint_id']}"
                ),
                confidence=CONFIDENCE_LOW,
                confidence_notes=(
                    "Where a dam wall would stand: the first cell downstream of the keypoint a full "
                    f"{POOL_REFERENCE_HEIGHT_METERS}m below it. A FAILED walk is drawn where it died, "
                    "with the reason -- there is no partial-height fallback, so a failed walk means "
                    "the keypoint nominated nothing."
                ),
                extra_properties={
                    "status": nomination_status,
                    "keypoint_id": outcome["keypoint_id"],
                    "outcome": outcome["outcome"],
                    "wall_offset_downstream_m": outcome["wall_offset_downstream_m"],
                    "wall_drop_m": outcome["wall_drop_m"],
                    "wall_walk_end_reason": outcome["wall_walk_end_reason"],
                    "keypoint_elevation_m": outcome["keypoint_elevation_m"],
                    "anchor_elevation_m": outcome["anchor_elevation_m"],
                    "wall_site_found": found,
                },
            )
        )

        path_cells = _stem_path_cells(
            flow_to_row, flow_to_col, tuple(outcome["keypoint_rowcol"]), tuple(end_cell)
        )
        if len(path_cells) >= 2:
            line = LineString([pixel_center_xy(dem, r, c) for r, c in path_cells])
            features.append(
                make_feature(
                    feature_id=f"water-stem-walk-{outcome['keypoint_id']}",
                    geometry=_wgs84(dem, line),
                    layer="water_stem_walk",
                    label=f"Stem walk, keypoint {outcome['keypoint_id']} to its wall site",
                    confidence=CONFIDENCE_LOW,
                    confidence_notes=(
                        "The traced D8 flow path the wall-site walk actually took, cell by cell -- not a "
                        "straight line between the endpoints, which would imply a channel that is not "
                        "there."
                    ),
                    extra_properties={
                        "status": nomination_status,
                        "keypoint_id": outcome["keypoint_id"],
                        "outcome": outcome["outcome"],
                        "cell_count": len(path_cells),
                        "wall_offset_downstream_m": outcome["wall_offset_downstream_m"],
                    },
                )
            )

    # --- layer 6: dropped family-2 seeds ---
    for index, seed in enumerate(nomination_diagnostics.get("accumulation_seeds", [])):
        if seed["candidate_id"] is not None:
            continue
        features.append(
            make_feature(
                feature_id=f"water-accumulation-seed-{index}",
                geometry=_point_wgs84(dem, seed["anchor_rowcol"]),
                layer="water_accumulation_seed",
                label=f"Dropped accumulation seed at {tuple(seed['anchor_rowcol'])} ({seed['outcome']})",
                confidence=CONFIDENCE_LOW,
                confidence_notes=(
                    "A family-2 (highest-remaining-flow-accumulation) seed that delineated but did not "
                    "survive. Drawn at its anchor cell; see the dropped-candidate note for why no "
                    "footprint is available."
                ),
                extra_properties={
                    "status": EXPORT_STATUS_DROPPED,
                    "outcome": seed["outcome"],
                    "flow_accumulation_cells": seed["flow_accumulation_cells"],
                    "flags": list(seed["flags"]),
                    "anchor_rowcol": list(seed["anchor_rowcol"]),
                    "geometry_is_anchor_point_only": True,
                },
            )
        )

    collection = make_feature_collection(features)
    validate_feature_collection(collection)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(collection, handle, indent=2)

    by_layer: dict = {}
    for feature in features:
        key = (feature["properties"]["layer"], feature["properties"]["status"])
        by_layer[key] = by_layer.get(key, 0) + 1
    return {"path": path, "feature_count": len(features), "by_layer_status": by_layer}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report the contributing-area ceiling mask + gate breakdown for the real reference property."
    )
    parser.add_argument(
        "--max-contributing-acres",
        type=float,
        default=MAX_VALLEY_CONTRIBUTING_AREA_ACRES,
        help=f"Override MAX_VALLEY_CONTRIBUTING_AREA_ACRES (the absolute contributing-area ceiling) for this "
        f"run only (default: the current module constant, {MAX_VALLEY_CONTRIBUTING_AREA_ACRES}).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    try:
        main(
            max_contributing_acres=args.max_contributing_acres,
        )
    except Exception as e:
        print(f"Request failed: {e}")
        print(
            "\nNote: this requires internet access to reach USGS's National "
            "Map ImageServer (DEM fetch) and production_area.py's own "
            "SSURGO/canopy/road data sources -- not a fully sandboxed environment."
        )
