"""
pipeline_context.py

Computes the shared upstream data several KSOP (Keyline Scale of
Permanence) pipeline steps each already fetch or derive independently --
DEM, boundary polygon, valleys, production areas, existing
roads, soil exclusion unions, water-system candidate zones, the selected
water zone, the selected road corridor, the selected structure (solar)
site, and every ranked tree-zone candidate -- exactly ONCE, and hands the
result back as a single PipelineContext object.

This is a pure orchestrator: it calls the REAL, already-existing entry
points in dem_data.py, valley_delineation.py, production_area_ceiling.py,
farm_roads_data.py, road_corridors.py, water_survey_areas.py,
solar_suitability.py, and tree_zone_candidates.py, in the dependency
order those modules already require. It reimplements
none of their logic. It calls road_corridors.
identify_road_corridor_candidates(), solar_suitability.
identify_solar_candidate_zones(), and tree_zone_candidates.
identify_tree_zone_candidates() (three of the identify_*_candidate*()
consumer functions) directly now that prior branches made those entry
points override-capable; it does NOT call report_generator.py, generate_
full_report.py, or fencing.py's own identify_*_candidate*() consumer
function -- that module doesn't have overrides yet, so wiring it in is
later, separate work. See KNOWN LIMITATIONS below for the remaining gaps
this surfaced -- in particular #4, now RESOLVED, a genuine, MEASURED
duplicate-call redundancy this branch's own testing surfaced inside
solar_suitability.identify_solar_candidate_zones() itself, since fixed
directly in that module.

FIELD NOTES

  keypoints is keypoint_detection.detect_keypoints()'s own list of per-
  valley keypoint dicts (the inflection in each primary valley's long
  profile -- see that module's docstring). It is computed right after
  valleys, in dependency order: keypoint detection is pure terrain analysis
  that needs only dem/boundary_polygon_utm/valleys (NO soil, canopy, road,
  or climate -- it is independent of KSOP), and this call forwards this
  context's own already-computed dem/boundary_polygon_utm/valleys straight
  through so delineate_valleys() is NOT run a second time for it. It earns a
  context field under the sizing principle now that it has THREE real
  consumers -- the layout map (a marker per keypoint), the report (the
  keypoint list carried through for narration), and the keypoint
  relationship pass below (each keypoint related to production areas and
  the selected water region). The survey-area water step does NOT
  consume keypoints (it nominates from suitability surfaces -- the old
  level-pool water step's keypoint nomination went with its demotion),
  so this list is no longer forwarded into the water call; keypoints
  remain their own KSOP layer, untouched by that redesign.
  detect_keypoints() self-
  computes its own flow direction/accumulation/filled arrays from dem
  (valley_delineation.py does not expose those, so there is nothing shared
  to forward for them); that is a handful of pure-numpy passes, no network.

  exclusion_zones is exclusion_zones.identify_exclusion_zones()'s own
  full result: the parcel's UNSELECTABLE ground as five per-gate cell
  masks (canopy, slope, hydric, roads, setback), each morphologically
  closed at its own measured radius, plus the closed union, the derived
  eligible geometry, and its own narrative_data. It is the FIRST Layer 2
  computation in this function -- before production areas -- because it
  derives only from Layer 1 products (dem, the canopy root-zone mask,
  the disqualifying-soil union, the road-exclusion union) and waits on
  no other Layer 2 result. It is NOT Layer 1 itself: it fetches no raw
  layer of its own.

  It earns a context field on the sizing principle through the layout
  map (which draws the union as the map's ground layer), the report
  (narrative_data), the frontend (the wire block), AND -- since the
  production integration landed -- the production call below, which
  consumes its five gate masks as exclusion_result= rather than
  computing the same five gates a second time. That is why canopy,
  soil and the slope grid are each computed exactly ONCE across one
  build_pipeline_context() run instead of twice; asserted at exact
  counts in test_exclusion_zones.py. It is a pure de-duplication --
  production's masks are bit-identical either way, asserted from both
  sides (see exclusion_zones.py's own module docstring). The ROAD
  union is computed once too, here: this context's own existing_roads
  (built just above the exclusion call, at the shared farm_roads_data.
  ROAD_EXCLUSION_BUFFER_METERS every consumer reads) is passed into
  exclusion_zones AND into the water-system step, and production reads
  the road layer off the exclusion result -- so all three former
  re-fetches of it are gone.

  production_areas holds production_area_ceiling.
  identify_optimized_production_areas()'s own 'scored_patches' -- the
  ceiling-trimmed, STEP-4-scored per-patch list -- NOT production_area.
  identify_production_areas()'s raw (un-trimmed) patches. scored_patches
  carries every field identify_production_areas() itself would have
  returned (id, area_acres, polygon_utm, render_fill_polygon_utm, cells,
  representative_elevation_m, geometry_wgs84, ...) plus STEP 4's advisory
  scoring fields added on top (production_suitability.
  score_production_areas() only ADDS fields, never removes or renames the
  base ones) -- so this is a genuine drop-in replacement for any consumer
  that expects identify_production_areas()'s own shape.

  existing_roads is farm_roads_data.get_road_exclusion_union_utm()'s own
  output directly (a shapely union of ROAD_LAYERS 30/31/32 road/ROW
  centerline geometry, buffered by its own ROAD_EXCLUSION_BUFFER_METERS
  default -- the single shared "how far off an existing road" definition,
  see that constant's docstring -- or None if no mapped roads were found
  nearby, the common, clean case, not an error). Computed ABOVE the
  exclusion-zones call (it is Layer-1-derived, so that is where it
  belongs) and passed into identify_exclusion_zones() as
  road_exclusion_union_utm=, sparing that module its own second road
  fetch -- including the None case, which its OVERRIDES contract reuses
  as a real "checked, nothing there" answer.

  soil_exclusion_unions is a dict with 'hydric_floodplain_union' (road_
  corridors._fetch_floodplain_hydric_union()'s own real NHD-stream +
  SSURGO-hydric union, clipped to the fetch-context/final-relevance
  buffers that module's own docstring documents), 'hydric_floodplain_
  is_fallback' (that same call's own second return value -- True only if
  BOTH real sources were unreachable and the union fell back to buffering
  the already-computed delineated valley lines; this is the real flag,
  not a hardcoded/defaulted one -- see selected_road_corridor below for
  the one place downstream that depends on it being genuine), and
  'erosion_prone_union' -- see KNOWN LIMITATIONS below for why the latter
  is always None here.

  selected_water_zone is water_survey_areas.identify_water_survey_
  areas()'s own pooled rank-1 SURVEY ZONE (embankment and excavated
  pooled by member-mean suitability, member-acreage tiebreak -- a
  PROVISIONAL selection rule, documented at select_survey_zone()), or
  None if no zone exists because nothing cleared the suitability
  threshold. The same call produces water_zones above, so the water
  step runs ONCE per context build. The zone dict carries the full
  established consumer contract (render_fill_polygon_utm = the zone's
  boundary-clipped closing ENVELOPE, the identity -- the ground one
  survey visit walks; representative_elevation_m from member cells;
  id, rank, served_production_area_ids, ...) so every downstream
  reader -- road exclusion, solar exclusion, tree search-space
  subtraction, fencing, the map's ripple clip, the keypoint
  relationship pass below -- slots it in unchanged. The FIELD keeps None
  for "no region" (context readers -- the map, the report, fencing --
  keep their existing None contract), but the three downstream override
  forwards below (road corridor, solar, tree zones) NEVER pass that
  None through: each consumer treats a None override as "not supplied"
  and re-runs the retired water_suitability pipeline as its self-compute
  fallback -- the SAME trap selected_road_corridor below documents,
  measured at FIVE full water-suitability runs per
  build_pipeline_context() on a no-qualifying-zone parcel before this
  guard. A resolved "nothing" is forwarded as water_suitability.
  NO_WATER_ZONE, the explicit already-ran-and-selected-nothing answer
  those entry points accept (see that constant's own docstring), so even
  an empty selection is a real, explicit answer downstream, not a missing
  one.

  selected_road_corridor is road_corridors.identify_road_corridor_
  candidates()'s own 'road_network' -- build_road_network()'s full
  multi-branch network dict (branches, each carrying its own cells/
  points_xyz/line_utm/geometry_wgs84/cell_footprint_polygon_utm/
  branch_role/branch_index/length_meters/avg_grade_pct/newly_served_
  acres/..., plus network-level total_length_meters/total_served_acres/
  unserved_acres/stop_reason/cells/cell_footprint_polygon_utm), NEVER
  None -- even a network with no branches at all (anchor unreachable,
  no demand, constraint stack cleared nothing) is this SAME shape with
  branches=[], not None (see build_road_network()'s own
  _empty_road_network()). This is deliberately the road_network dict,
  not identify_road_corridor_candidates()'s own 'selected_road_corridor'
  return key (which collapses branches=[] to None) -- every downstream
  consumer below treats None as "not supplied" and reacts by running its
  own full self-compute fallback (several whole-DEM Dijkstra runs, not a
  cheap mask), so an empty network has to be forwarded as a real, explicit
  answer to be reused at all. This call passes this context's own already-
  computed dem/boundary_polygon_utm/valleys/production_areas/selected_
  water_zone through via override params, AND reuses this context's own
  soil_exclusion_unions['hydric_floodplain_union'] (paired with the real
  soil_exclusion_unions['hydric_floodplain_is_fallback'] flag above)
  rather than letting identify_road_corridor_candidates() fetch a second,
  independent floodplain/hydric union -- so _fetch_floodplain_hydric_
  union() also runs only ONCE across the whole of build_pipeline_context().
  PipelineContext does NOT carry any per-branch fields of its own --
  branches live inside this one dict; a consumer that needs one branch's
  geometry reads selected_road_corridor["branches"][i] directly rather
  than this context growing a new field per branch.

  selected_structure_site is solar_suitability.identify_solar_candidate_
  zones()'s own 'selected_structure_site' -- select_optimal_structure_
  site()'s rank-1 answer, or None if no candidate cleared the constraint
  stack. This call passes this context's own already-computed dem/
  boundary_polygon_utm/valleys/production_areas/selected_water_zone/
  selected_road_corridor/soil_exclusion_unions['hydric_floodplain_union']/
  ['hydric_floodplain_is_fallback'] through via override params, same
  reuse pattern as selected_road_corridor above -- nothing it depends on
  is re-derived a second time, at this call's own top level OR one level
  deeper, inside its own internal tree-zone-exclusion step, which now
  forwards this same context's own values into its own nested identify_
  tree_zone_candidates() call rather than self-computing independent
  copies (see KNOWN LIMITATIONS #4 for the redundancy this fixed, and
  test_pipeline_context.py's own call-count assertions for proof).

  tree_zone_patches is tree_zone_candidates.identify_tree_zone_
  candidates()'s own 'patches' -- score_tree_search_space()'s full ranked
  list. Unlike water/road/solar, there is no single "selected" tree zone
  on a property (a farm can have several legitimate tree zones at once),
  so this field holds the complete ranked list, same shape/reuse pattern
  as this context's own production_areas field. This call passes the same
  overrides selected_structure_site above does, so nothing it depends on
  is re-derived a second time by THIS call specifically. Named
  tree_zone_patches (matching the 'patches' key it's sourced from), NOT
  tree_zone_candidates -- an earlier version of this field used that name
  and flagged it as a real ambiguity risk, since it doubled as the base
  name of the module (tree_zone_candidates.py) and function (identify_
  tree_zone_candidates()) that produce it; renamed here to resolve that
  collision rather than leave it flagged.

  water_zones is water_survey_areas.identify_water_survey_areas()'s own
  'zones_geojson' FeatureCollection's 'features' list -- EVERY survey
  zone envelope (survey_zone_embankment / survey_zone_excavated) plus
  every member-region footprint (survey_zone_member_*), flagged not
  filtered, GeoJSON-wrapped (WGS84 geometry built once at each object's
  birth; stored wire forms only).
  The SAME single call also produces selected_water_zone below. It
  passes this context's own already-computed dem/boundary_polygon_utm/
  production_areas/canopy_height/existing_roads straight through via
  that function's own override params, so identify_optimized_
  production_areas() and the road-union fetch genuinely run only ONCE
  each across the whole of build_pipeline_context() -- see
  test_pipeline_context.py's own call-count assertions. valleys are NOT
  an input to the survey-area water step (it derives its own flow/
  slope/TWI screens from the DEM directly, exactly once, internally).

  narrative_data is the report-facing narrative block each producing
  module attaches to its own identify_*() result (the narrative_data
  convention -- pre-digested, FINAL, JSON-serialisable values a
  narrative can quote directly), captured here keyed by module, one
  line per module, off calls this function already makes. It
  deliberately does NOT meet this context's own sizing principle
  (nothing downstream COMPUTES off it -- only report_generator.py's
  formatting functions read it), which is exactly why it is its own
  clearly separate field rather than folded into the KSOP fields above:
  a future reader can tell at a glance which fields are load-bearing
  for computation and which exist purely to feed the narrative. The
  water key is "water_survey_areas" -- water_survey_areas.build_
  narrative_data()'s block (ALL regions, no cap, per-criterion mean
  scores as the narrative-honesty mechanism, and the parcel-relative
  TWI caveat surfaced as twi_is_parcel_relative/twi_note).

KNOWN LIMITATIONS (found while building this, deliberately NOT worked
around here -- flagging per this branch's own instructions rather than
silently patching another module or reimplementing its logic)

  1. The survey-area water step (water_survey_areas.identify_water_
     survey_areas()) accepts road_exclusion_union_utm directly, so this
     context's existing_roads union is now genuinely shared with the
     water step (the missing-parameter blocker the old level-pool water
     call carried is gone with that call). What remains un-deduplicated:
     canopy_root_zone_mask_utm -- the water step still derives its OWN
     root-zone mask from the shared canopy_height dict, at its own
     WATER_ZONE_CANOPY_BUFFER_METERS, because PipelineContext has no
     tree-root-zone-mask field and the per-module canopy buffers are
     genuinely separate constants (unlike the unified road buffer).
     This costs no network fetch (canopy_height is forwarded; only the
     mask derivation repeats) -- flagged here, not added. The water
     step's soil inputs are RESOLVED (tuning pass): hydrologic group now
     rides the existing get_soil_data_for_polygon() query (a column, not
     a second fetch), and the soil trio (ksat rows / component rows /
     map-unit geometry) is forwarded from ParcelData's Layer-1 fetches
     through this function's own saturated_hydraulic_conductivity/
     soil_components/soil_geometries passthroughs -- the water step
     performs no SDA fetch on the pipeline path.

  2. production_area_ceiling.identify_optimized_production_areas() takes
     `boundary_coordinates` + `dem`, not an already-computed
     boundary_polygon_utm -- it re-derives its own internally via the same
     warp_transform-then-Polygon pattern _boundary_polygon_utm() below
     performs. Unlike limitation 1, this is a cheap, pure-geometry
     recomputation with no network cost, so it's noted here but not
     treated as a blocking gap.

  3. soil_exclusion_unions['erosion_prone_union'] is always None: road_
     corridors.py used to fetch one (_fetch_erosion_prone_union()) but
     that function -- and the erosion-avoidance preference it fed -- was
     deliberately REMOVED OUTRIGHT (not relocated), per that module's own
     docstring: this pipeline's KSOP ordering puts Soil at step 8, well
     below Farm Roads at step 4, so scoring a step-4 feature against
     step-8 data inverted that ordering. No other shared, reusable
     exclusion-union-builder for erosion-prone soil currently exists
     anywhere in this codebase -- soil_data.py exposes only the per-mukey
     primitives is_erosion_prone()/get_erosion_factor_for_polygon(), not a
     union-builder standing in the hydric one's shoes. Building one would
     be new logic, which this branch's own instructions say not to write
     here -- so this key is populated with None and flagged, not silently
     reimplemented.

  4. [RESOLVED -- see below] This slot documented a real, MEASURED
     duplicate-call redundancy: identify_solar_candidate_zones()'s own
     internal "TREE-ZONE-CANDIDATE exclusion" step called identify_tree_
     zone_candidates() a second, nested time, forwarding NONE of the
     overrides identify_solar_candidate_zones() itself had just received
     (only boundary_coordinates/dem/anchor_lon_lat) -- so that inner call
     fell back to self-computing production_areas/selected_water_zone/
     selected_road_corridor all over again via its own separate module-
     level bindings, causing identify_optimized_production_areas() and
     identify_road_corridor_candidates() to each run TWICE total (not
     once) across a single build_pipeline_context() call, and identify_
     water_suitability() at least once more the same way. Originally left
     unfixed here because fixing it meant modifying solar_suitability.py,
     out of that branch's own stated scope. A follow-up branch fixed it
     directly in solar_suitability.py's identify_solar_candidate_zones():
     its internal identify_tree_zone_candidates() call now forwards
     boundary_polygon_utm/production_areas/valleys/selected_water_zone/
     selected_road_corridor/hydric_floodplain_union/floodplain_data_is_
     fallback, same as this module's own two calls always did -- which
     also required moving that function's own Tier-1 selected_road_
     corridor self-compute earlier (before the tree-zone-exclusion step,
     which needs it resolved to forward), see that function's own
     docstring/comments for the reordering and why it's safe. See test_
     pipeline_context.py's own call-count assertions (updated alongside
     this fix) for the now-restored "still exactly 1" total across the
     whole build_pipeline_context() run.

  5. [RESOLVED] This branch first added a boundary_polygon_utm override
     (mirroring the dem override an earlier branch added), so a caller
     that already computed one (e.g. parcel_data.fetch_parcel_data()) can
     pass it straight through instead of paying for a second warp_
     transform. It originally could NOT add equivalent overrides for
     existing_roads/soil_exclusion_unions['hydric_floodplain_union'],
     because -- unlike dem/boundary_polygon_utm -- this file never fetches
     that raw data itself; it calls two wrapper functions that do the
     fetching one level down (farm_roads_data.get_road_exclusion_union_
     utm(), road_corridors._fetch_floodplain_hydric_union()), and neither
     exposed a way to skip its own internal fetch.

     A follow-up addendum to this same branch closed most of this: both
     wrapper functions now accept their own override parameter (farm_
     roads= on get_road_exclusion_union_utm(), soil_components= on
     _fetch_floodplain_hydric_union()), same None-falls-back-to-self-fetch
     convention as every other override in this pipeline, and this
     function's own soil_components=/farm_roads= parameters pass straight
     through to them (a pure passthrough, not a self-compute-here-if-
     missing gate the way dem/boundary_polygon_utm are -- this file still
     never fetches this data itself, it just no longer FORCES the two
     wrapper functions to). existing_roads was fully closeable at that
     point: get_road_exclusion_union_utm() had exactly one internal fetch
     (get_farm_roads_for_boundary()), and farm_roads= covered it
     completely.

     soil_exclusion_unions['hydric_floodplain_union'] was only PARTIALLY
     closed at that point. _fetch_floodplain_hydric_union() actually self-
     fetches THREE things, not one:
       - get_soil_data_for_polygon() (SSURGO composition data) -- closed
         via soil_components=, wired through from this function.
       - get_soil_geometries_for_polygon() (SSURGO per-mukey GEOMETRY,
         fetched only when hydric_mukeys is non-empty) -- a SEPARATE call
         from the one soil_components= replaces.
       - get_water_features_for_boundary() (NHD streams/water bodies).
     This branch closed the remaining two: _fetch_floodplain_hydric_
     union() now also accepts water_features=/soil_geometries= override
     parameters (road_corridors.py), and this function's own water_
     features=/soil_geometries= parameters pass straight through to it,
     same pure-passthrough convention as soil_components=/farm_roads=
     above. A caller that already fetched soil geometries and water
     features via parcel_data.fetch_parcel_data() (both are ParcelData
     fields, in the exact shape these two functions expect) now passes
     them through instead of paying for two more redundant fetches.
     soil_exclusion_unions['hydric_floodplain_union'] is now fully
     closeable: all three of _fetch_floodplain_hydric_union()'s internal
     fetches have override parameters, all three are threaded through
     here.

  6. [RESOLVED] exclusion_zones.identify_exclusion_zones()'s hydric gate
     was the LAST place this function still forced a redundant SSURGO
     fetch. It accepted a pre-derived disqualifying_soil_union_utm= but
     no raw-row override, so it self-served production_area._fetch_
     disqualifying_soil_union(), whose own two SDA queries (get_soil_
     data_for_polygon(), then get_soil_geometries_for_polygon() for the
     qualifying mukeys) are the SAME two calls limitation 5 above already
     closed one level over in road_corridors.py -- against data this
     function was already holding in its own soil_components/soil_
     geometries parameters.

     A follow-up branch closed it the same way, in the same shape: _fetch_
     disqualifying_soil_union() gained soil_components=/soil_geometries=
     overrides (None falls back to self-fetch), identify_exclusion_zones()
     gained matching PURE PASSTHROUGH parameters, and this function
     forwards its own two existing parameters into them. No computed value
     moves: the union is derived from the same rows by the same code, it
     just no longer re-fetches rows the caller already has. With this
     closed, every SSURGO fetch on this function's path is served from
     Layer 1 -- the exclusion gate, the floodplain union and the water
     step all read one caller-supplied pair of soil layers.

     This one mattered beyond the two queries it saves. session_cache.
     run_terrain_warm_up() runs the same identify_exclusion_zones() call
     with the same overrides, so leaving the gap open meant a session
     rebuild -- which is supposed to be compute, served entirely by the
     fetch cache -- still went to the network twice. Closing it here and
     there together is what makes the property interactive-design-
     architecture-proposal.md section 2.2 states outright -- an external
     fetch inside a rebuild is served by the fetch cache, so a rebuild is
     compute, not network -- true unconditionally rather than nearly.
"""

from dataclasses import dataclass
from typing import Optional

from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

import dem_data
import canopy_height_data
import exclusion_zones
import farm_roads_data
import keypoint_detection
import production_area_ceiling
import road_corridors
import valley_delineation
import water_survey_areas
from solar_suitability import identify_solar_candidate_zones
from tree_zone_candidates import identify_tree_zone_candidates
# NO_WATER_ZONE stays imported from water_suitability even though that
# module is retired from the pipeline path: the sentinel is the shared
# identity object every downstream override entry point (road corridors,
# solar, tree zones) compares against with `is`, so it must keep living
# in exactly one place -- see its own docstring there.
from water_suitability import NO_WATER_ZONE


@dataclass
class PipelineContext:
    dem: dict
    boundary_polygon_utm: Polygon
    valleys: list[dict]
    keypoints: list[dict]
    # The parcel's unselectable ground -- exclusion_zones.identify_exclusion_
    # zones()' whole result (five per-gate masks as exact cell footprints,
    # their union, the derived eligible geometry, and the wire block the
    # frontend reads). The FIRST Layer 2 computation, before production
    # areas: it depends only on Layer 1 products, so nothing here waits on
    # it -- and production areas, the NEXT Layer 2 step, now depends on IT,
    # which makes that ordering load-bearing rather than incidental. The map
    # draws it, the report narrates it, the frontend intersects a drawn
    # polygon against it, and production consumes its five gate masks
    # instead of computing them again.
    exclusion_zones: dict
    production_areas: list[dict]
    parcel_acres: float
    existing_roads: BaseGeometry | None
    soil_exclusion_unions: dict[str, BaseGeometry | None]
    water_zones: list[dict]
    selected_water_zone: dict | None
    selected_road_corridor: dict
    selected_structure_site: dict | None
    tree_zone_patches: list[dict]
    # Report-facing narrative blocks, keyed by producing module ("production_
    # area_ceiling", "water_survey_areas", "road_corridors",
    # "solar_suitability", "tree_zone_candidates") -- each value is that
    # module's own 'narrative_data' return key (pre-digested, FINAL,
    # JSON-serialisable values; see each module's build_narrative_data()),
    # or None if the producing call didn't attach one. DELIBERATELY its own,
    # clearly separate field rather than folded into the fields above: every
    # other field here is load-bearing for downstream KSOP computation
    # (this context's own sizing principle), while nothing computes off this
    # one -- only report_generator.py reads it. See the narrative_data
    # convention doc for this distinction.
    narrative_data: dict[str, dict | None]


def _boundary_polygon_utm(boundary_coordinates: list[tuple[float, float]], dem: dict) -> Polygon:
    """
    Reprojects boundary_coordinates (WGS84 lon/lat) into dem['crs'] and
    builds the resulting UTM-meters Polygon -- the same warp_transform +
    Polygon(...) pattern water_candidate_zones.
    identify_water_system_candidate_zones(), production_area_ceiling.
    identify_optimized_production_areas(), and road_corridors.
    identify_road_corridor_candidates() each already duplicate inline.
    Extracted here as this module's own shared helper since this context
    needs it directly (see PipelineContext.boundary_polygon_utm) rather
    than copy-pasting the block a fourth time.
    """
    boundary_xs, boundary_ys = warp_transform(
        "EPSG:4326",
        dem["crs"],
        [pt[0] for pt in boundary_coordinates],
        [pt[1] for pt in boundary_coordinates],
    )
    return Polygon(zip(boundary_xs, boundary_ys))


def _attach_keypoint_feature_relationships(
    keypoints: list[dict],
    production_areas: list[dict],
    selected_water_zone: dict | None,
) -> None:
    """Layer-2 relationship computation, in place on each keypoint dict.

    keypoints, production_areas, and selected_water_zone are all already in
    memory in build_pipeline_context() (all computed above from the same
    DEM/boundary/valleys pass), so this needs NO new fetch and NO DEM
    re-derivation. Each keypoint gains a 'feature_relationships' key --
    always present, never None -- with two sub-dicts:

      'nearest_production_area' and 'water_zone', each carrying a 'status':
        - "computed": also carries 'distance_m' (keypoint point to that
          feature's DRAWN geometry) and 'elevation_differential_m' (the
          keypoint's elevation_m minus the feature's representative
          elevation, SIGNED so positive = keypoint sits ABOVE the feature).
        - "no_feature": no production areas / no selected water zone exists
          on this property -- no distance or differential keys.

    Distances use render_fill_polygon_utm for BOTH features -- the same
    geometry render_layout_map.py actually draws (production contour texture
    is clipped to it; the water ripple texture is drawn from it), NOT the
    scoring/eligibility geometry_wgs84 -- matching the render_fill_area_acres
    discipline production_areas_to_geojson() now uses. All geometry here is
    UTM meters (keypoint 'point_utm' and each feature's
    render_fill_polygon_utm share the DEM's own CRS), so shapely .distance()
    returns meters directly.
    """
    for kp in keypoints:
        kp_point = kp["point_utm"]
        kp_elev = kp["elevation_m"]

        if not production_areas:
            production_rel = {"status": "no_feature"}
        else:
            distance, nearest = min(
                (
                    (kp_point.distance(patch["render_fill_polygon_utm"]), patch)
                    for patch in production_areas
                ),
                key=lambda pair: pair[0],
            )
            production_rel = {
                "status": "computed",
                "distance_m": round(distance, 2),
                "elevation_differential_m": round(
                    kp_elev - nearest["representative_elevation_m"], 2
                ),
            }

        if selected_water_zone is None:
            water_rel = {"status": "no_feature"}
        else:
            water_rel = {
                "status": "computed",
                "distance_m": round(
                    kp_point.distance(selected_water_zone["render_fill_polygon_utm"]), 2
                ),
                "elevation_differential_m": round(
                    kp_elev - selected_water_zone["representative_elevation_m"], 2
                ),
            }

        kp["feature_relationships"] = {
            "nearest_production_area": production_rel,
            "water_zone": water_rel,
        }


def build_pipeline_context(
    boundary_coordinates: list[tuple[float, float]],
    anchor_lon_lat: tuple[float, float],
    dem: Optional[dict] = None,
    boundary_polygon_utm: Optional[Polygon] = None,
    soil_components: Optional[list[dict]] = None,
    farm_roads: Optional[list[dict]] = None,
    water_features: Optional[dict] = None,
    soil_geometries: Optional[dict] = None,
    canopy_height: Optional[dict] = None,
    saturated_hydraulic_conductivity: Optional[list[dict]] = None,
) -> PipelineContext:
    """
    Computes every shared upstream input multiple KSOP pipeline steps
    need, exactly once. Does not call report_generator.py, generate_full_
    report.py, or fencing.py's own identify_*_candidate*() consumer
    function -- that module doesn't have overrides yet, so wiring it in is
    later, separate work.

    canopy_height is an optional pre-fetched override AND, when it is not
    supplied, a self-compute-here gate -- the same None-falls-back-to-
    self-fetch shape dem/boundary_polygon_utm/existing_roads already have,
    which it did NOT have before: this file used to forward the caller's
    canopy_height verbatim and never fetch canopy itself. It is the SAME
    dict canopy_height_data.get_canopy_height_for_boundary() returns (e.g.
    parcel_data.ParcelData.canopy_height).

    Forwarded to every internal call below whose entry point accepts a
    canopy_height override -- exclusion_zones.identify_exclusion_zones(),
    production_area_ceiling.identify_optimized_production_areas(),
    water_survey_areas.identify_water_survey_areas(), road_corridors.
    identify_road_corridor_candidates(), identify_solar_candidate_
    zones(), and identify_tree_zone_candidates().

    WHY IT IS FETCHED HERE NOW. Forwarding None is not neutral: None is
    every one of those consumers' "fetch it yourself" value, so leaving it
    None meant SEVEN independent Planetary Computer round-trips for one
    parcel's HAG coverage on every run -- measured at exactly seven (on
    the pre-redesign pipeline, whose water step reached canopy twice), on
    a full run with nothing mocked away. Fetching it once here and letting
    the existing forwards carry it takes that to ONE, and changes no mask:
    each consumer still derives its own root-zone mask from this dict at
    its own buffer. See the fetch site itself for the per-buffer detail
    and for why a real None is left as None.

    anchor_lon_lat is the real, chosen access point road routing starts
    from -- it's passed straight through to identify_road_corridor_
    candidates(), identify_solar_candidate_zones(), and identify_tree_
    zone_candidates() below (see selected_road_corridor, selected_
    structure_site, tree_zone_patches).

    dem is optional -- same None-falls-back-to-self-fetch convention every
    other override in this pipeline uses (see e.g. production_area_
    ceiling.identify_optimized_production_areas()). A caller that already
    fetched a DEM for this exact boundary (e.g. render_layout_map.
    fetch_layout_layers(), which accepts its own dem= for the same reason)
    passes it through here instead of paying for a second, redundant
    fetch.

    boundary_polygon_utm is optional the same way -- a caller that already
    computed it (e.g. parcel_data.fetch_parcel_data(), which derives it
    identically via the same warp_transform-then-Polygon pattern
    _boundary_polygon_utm() below performs) passes it through here instead
    of paying for a second, redundant reprojection.

    soil_components, farm_roads, water_features, soil_geometries, and
    saturated_hydraulic_conductivity are optional too, but unlike
    dem/boundary_polygon_utm above, this function never self-fetches any
    of them -- it passes them straight through, unconditionally, to the
    functions that actually own the corresponding self-fetch
    (road_corridors._fetch_floodplain_hydric_union() for soil_components/
    water_features/soil_geometries, production_area._fetch_disqualifying_
    soil_union() -- reached via exclusion_zones.identify_exclusion_zones()'s
    own passthrough -- for soil_components/soil_geometries again, farm_
    roads_data.get_road_exclusion_union_utm() for farm_roads, and the water
    step below for the soil trio); each of those has its own fall-back-to-
    self-fetch path, so leaving any argument here as None reproduces the
    exact pre-existing behavior. A caller that already fetched them for
    this exact boundary (e.g. parcel_data.fetch_parcel_data()) passes them
    through here instead of paying for second, redundant fetches. See
    KNOWN LIMITATIONS #5 and #6 (both now RESOLVED) for the history of
    closing this.

    soil_components/soil_geometries therefore reach THREE consumers, not
    one: the exclusion gate, the floodplain union, and the water step.
    That is the point -- one Layer 1 pair of SSURGO fetches, read three
    times, rather than three independent pairs of the same two queries.

    THE WATER STEP'S SOIL TRIO rides three of these: when soil_components
    AND soil_geometries AND saturated_hydraulic_conductivity are all
    supplied (the ParcelData path -- Layer 1, fetched once, hard-fail
    governed), they are assembled into water_survey_areas'
    soil_inputs override, so the water step's soil criterion consumes
    the SAME rows ParcelData already fetched and performs no SDA fetch
    of its own (hydrologic group rides soil_components since the hydgrp
    query change -- see soil_data.get_soil_data_for_polygon()). When any
    of the three is missing, the override is simply not passed and the
    water step keeps its own documented standalone posture
    (fetch-or-degrade to never-checked).
    """
    if dem is None:
        dem = dem_data.get_dem_for_boundary(boundary_coordinates)
    if boundary_polygon_utm is None:
        boundary_polygon_utm = _boundary_polygon_utm(boundary_coordinates, dem)

    valleys = valley_delineation.delineate_valleys(dem)

    # Keypoints: pure terrain analysis, dependent only on dem/boundary/valleys
    # (all already computed above), forwarded so delineate_valleys() is not
    # rerun. Independent of every KSOP layer below -- computed here, in
    # dependency order, right after the valleys it profiles.
    keypoints = keypoint_detection.detect_keypoints(
        dem, boundary_polygon_utm, valleys=valleys
    )

    # Layer 2, FIRST STEP -- before production areas, deliberately. This
    # derives entirely from Layer 1 products (dem, the canopy root-zone mask,
    # the disqualifying-soil union, the road-exclusion union) and depends on
    # no other Layer 2 result, so nothing about the ordering below constrains
    # it. Placing it first is now LOAD-BEARING rather than merely tidy: the
    # production call below CONSUMES this result, so it has to exist by then.
    # It was placed here before that was true, which is what made the
    # integration a one-line change when it landed.
    #
    # existing_roads is computed HERE, above the exclusion-zones call --
    # it is Layer-1-derived (a reprojected, buffered union of the raw
    # farm-roads fetch, nothing more), so this is arguably where it always
    # belonged -- specifically so the call below can reuse it instead of
    # re-fetching. Legitimately None on any parcel with no mapped road
    # nearby (the common, clean case).
    existing_roads = farm_roads_data.get_road_exclusion_union_utm(boundary_coordinates, dem, farm_roads=farm_roads)

    # canopy_height is fetched HERE when the caller did not supply one --
    # same None-falls-back-to-self-fetch shape dem and existing_roads above
    # already use, and the reason this function exists at all.
    #
    # WHAT THIS CLOSES. Every canopy consumer below already accepts a
    # canopy_height= override and this function already forwards it to all
    # of them -- but with nothing to forward it forwarded None, and None
    # means "fetch it yourself". SEVEN consumers did, on every run of the
    # pre-redesign pipeline: exclusion_zones, the two old water calls
    # (since replaced by the single water_survey_areas call below),
    # road_corridors, solar_suitability, and identify_tree_zone_
    # candidates TWICE (once nested inside solar's own tree-zone-
    # exclusion step, once at this function's own call below). Seven
    # Planetary Computer round-trips for one parcel's HAG coverage,
    # measured at exactly that on a full run. Fetching once here takes
    # it to ONE.
    #
    # THIS CHANGES NO MASK. Each consumer still derives its OWN root-zone
    # mask from this dict at its OWN buffer -- production/exclusion/solar
    # at TREE_ROOT_ZONE_BUFFER_METERS, water at WATER_ZONE_CANOPY_BUFFER_
    # METERS, tree zones at TREE_ZONE_CANOPY_BUFFER_METERS, road corridors
    # at 0.0m. Only the SOURCE of the HAG array moves; the seven dilations
    # still run, and a consumer handed this dict computes byte-identically
    # to one that fetched it itself (production_area._fetch_tree_root_zone_
    # mask_utm()'s own contract, asserted in test_canopy_mask_override.py).
    #
    # A REAL None IS LEFT AS None, DELIBERATELY. get_canopy_height_for_
    # boundary() returns None for "no HAG coverage for this boundary at
    # all", and None is also this parameter's "not supplied" value. They
    # are not distinguished here ON PURPOSE: forwarding that None makes
    # each consumer take exactly the path it takes today, which for the
    # mandatory gates is get_required_tree_root_zone_mask_utm() raising
    # RuntimeError at the first one reached. Inventing a sentinel to skip
    # those re-fetches would change nothing about the outcome -- the run
    # fails either way -- while adding a state no consumer knows how to
    # read. The one cost is a second fetch on a boundary with no coverage,
    # in a run that is about to fail.
    #
    # Any OTHER fetch failure (retries exhausted, CanopyCoverageIncomplete
    # Error) propagates UNCAUGHT, unchanged: it propagated out of this
    # function before too, from whichever consumer reached canopy first.
    # That consumer is identify_exclusion_zones() immediately below, so
    # the failure point barely moves.
    if canopy_height is None:
        canopy_height = canopy_height_data.get_canopy_height_for_boundary(boundary_coordinates, dem)

    # PASSED INTO THE PRODUCTION CALL BELOW as exclusion_result=. Production
    # no longer computes its own five gates: it consumes these. That closes
    # the duplication this comment used to document -- the canopy fetch, the
    # soil fetch, the road union and the slope grid each now happen ONCE
    # across this function instead of twice, asserted at exact counts in
    # test_exclusion_zones.py.
    #
    # It is a PURE de-duplication, not a behaviour change. It was deferred
    # while this module closed its canopy and slope layers (closing is
    # extensive, so production consuming them would have gained the pinhole
    # cells the closing absorbed -- a real difference in what gets planted).
    # The closing was removed, and these layers are now raw cell footprints,
    # exact, because the frontend intersects a drawn polygon against them and
    # captions the acreage it crossed. Raw and exact is also precisely what
    # production computes, so the masks are bit-identical either way --
    # asserted, not argued: test_eligible_union.py section 0 from this
    # module's side, test_production_area.py's "STEP 1 CONSUMES THE
    # EXCLUSION RESULT" section from production's.
    #
    # road_exclusion_union_utm= IS passed, though: this context's own
    # existing_roads above, built by farm_roads_data.get_road_exclusion_
    # union_utm() at its default buffer -- which is the SAME farm_roads_
    # data.ROAD_EXCLUSION_BUFFER_METERS the exclusion module's own
    # self-compute (production_area._fetch_road_exclusion_union_utm()'s
    # default) would have used, by shared definition, not coincidence;
    # test_exclusion_zones.py asserts the two defaults agree so a future
    # divergence fails loudly instead of silently substituting a
    # wrong-buffer union here. identify_exclusion_zones() reuses a real
    # None too ("checked, genuinely no roads nearby" -- see its OVERRIDES
    # docstring section), so no second road fetch happens either way.
    # Fetches #3 (production's own road gate) and #4 (the water step's)
    # are both closed now: #3 by the exclusion_result= pass-through below,
    # #4 by passing this same union into identify_water_survey_areas().
    # One road union per run, built here, consumed three times.
    # soil_components=/soil_geometries= are this function's own existing
    # parameters, forwarded here for the SAME reason they are forwarded
    # into _fetch_floodplain_hydric_union() below: identify_exclusion_
    # zones()'s hydric gate self-served get_soil_data_for_polygon() and
    # get_soil_geometries_for_polygon() through production_area._fetch_
    # disqualifying_soil_union(), which had no raw-row override until a
    # follow-up branch added one. Both are pure passthroughs (this file
    # never fetches SSURGO rows itself), so a ParcelData-backed caller now
    # pays ZERO SDA queries on this path instead of two -- and session_
    # cache.run_terrain_warm_up() forwards the same two fields into the
    # same parameters, so the batch path and the session path stay one
    # computation reached two ways rather than two routes to the same
    # answer.
    exclusion_result = exclusion_zones.identify_exclusion_zones(
        boundary_coordinates,
        dem=dem,
        boundary_polygon_utm=boundary_polygon_utm,
        canopy_height=canopy_height,
        soil_components=soil_components,
        soil_geometries=soil_geometries,
        road_exclusion_union_utm=existing_roads,
    )

    # canopy_height is still forwarded even though the exclusion result makes
    # production's own canopy fetch unreachable: identify_optimized_
    # production_areas() keeps its full self-fetch path for every caller that
    # does NOT supply an exclusion result, and dropping the override here
    # would silently arm a redundant fetch the day this pass-through is
    # removed or made conditional.
    optimized_production = production_area_ceiling.identify_optimized_production_areas(
        boundary_coordinates,
        dem=dem,
        canopy_height=canopy_height,
        exclusion_result=exclusion_result,
    )
    production_areas = optimized_production["scored_patches"]
    # Total parcel acreage the ceiling optimizer already computed
    # (boundary_polygon_utm.area / SQUARE_METERS_PER_ACRE, production_area_
    # ceiling.py) -- surfaced here so downstream consumers (the report)
    # get it without recomputing. Previously discarded with the rest of
    # the ceiling optimizer's return.
    parcel_acres = optimized_production["parcel_acres"]

    hydric_floodplain_union, hydric_floodplain_is_fallback = road_corridors._fetch_floodplain_hydric_union(
        boundary_coordinates,
        dem,
        valleys,
        boundary_polygon_utm,
        soil_components=soil_components,
        water_features=water_features,
        soil_geometries=soil_geometries,
    )
    soil_exclusion_unions = {
        "hydric_floodplain_union": hydric_floodplain_union,
        "hydric_floodplain_is_fallback": hydric_floodplain_is_fallback,
        # See module docstring, KNOWN LIMITATIONS #3 -- no shared
        # union-builder for erosion-prone soil currently exists to call.
        "erosion_prone_union": None,
    }

    # THE WATER STEP: water_survey_areas.identify_water_survey_areas() --
    # typed survey areas from weighted-overlay suitability surfaces (see
    # that module's docstring for the deliverable's full definition and
    # why it replaced the demoted level-pool arc). ONE call now produces
    # both this context's water fields: the full flagged-not-filtered
    # region FeatureCollection (water_zones) and the pooled rank-1 region
    # (selected_water_zone).
    #
    # road_exclusion_union_utm= closes the road fetch: this context's own
    # existing_roads, the same union already handed to identify_exclusion_
    # zones() (and through it, to production) above -- both built at the
    # single shared farm_roads_data.ROAD_EXCLUSION_BUFFER_METERS. A real
    # None is reused as "checked, genuinely no roads nearby" rather than
    # re-fetched, same convention as the exclusion call.
    #
    # keypoints are NOT forwarded anymore: the survey-area water step
    # nominates from suitability surfaces, not keypoints. detect_
    # keypoints() above still runs exactly once -- keypoints remain their
    # own KSOP layer (relationship pass below, map, report), untouched by
    # this water-step redesign.
    # The soil trio: assembled into the water step's soil_inputs override
    # only when ALL THREE pieces were supplied (the ParcelData path) --
    # partial data is not forwarded, because the water scorer's
    # all-or-nothing soil posture must not receive a mix of real and
    # missing pieces dressed up as one checked answer. When not
    # assembled, the kwarg is simply omitted and the water step keeps its
    # own standalone fetch-or-degrade posture.
    water_step_kwargs = {}
    if (
        soil_components is not None
        and soil_geometries is not None
        and saturated_hydraulic_conductivity is not None
    ):
        water_step_kwargs["soil_inputs"] = {
            "ksat_rows": saturated_hydraulic_conductivity,
            "components": soil_components,
            "geometries_by_mukey": soil_geometries,
        }

    water_system_result = water_survey_areas.identify_water_survey_areas(
        boundary_coordinates,
        dem=dem,
        boundary_polygon_utm=boundary_polygon_utm,
        production_areas=production_areas,
        canopy_height=canopy_height,
        road_exclusion_union_utm=existing_roads,
        **water_step_kwargs,
    )
    water_zones = water_system_result["zones_geojson"]["features"]
    selected_water_zone = water_system_result["selected_water_zone"]
    # SAME GUARD as selected_road_corridor below, applied to the water zone:
    # every downstream consumer of a selected_water_zone override
    # (identify_road_corridor_candidates(), identify_solar_candidate_
    # zones(), identify_tree_zone_candidates()) treats None as "not
    # supplied" and reacts by re-running the ENTIRE identify_water_
    # suitability() pipeline as its self-compute fallback -- measured at
    # FIVE full water-suitability runs (this call plus one per consumer,
    # plus one more through solar's nested tree-zone-exclusion call) across
    # a single build_pipeline_context() run whenever no water zone
    # qualifies. Roads dodge this by forwarding build_road_network()'s full
    # dict (branches=[] and all, never None); water has no non-None dict
    # shape for "nothing," so water_suitability.NO_WATER_ZONE (see that
    # constant's own docstring) IS the real, explicit "already ran the
    # selection, nothing qualified" answer forwarded instead. The context
    # FIELD below still carries the plain None -- context readers (the
    # map, the report, fencing) keep their existing None contract; only
    # the three override forwards use the explicit form.
    water_zone_answer = selected_water_zone if selected_water_zone is not None else NO_WATER_ZONE

    # Layer 2: keypoints, production_areas, and selected_water_zone are now
    # all co-resident in memory (computed above from one DEM/boundary/valleys
    # pass). Relate each keypoint to the nearest production area and to the
    # selected water zone here, in place, with no new fetch. See the helper's
    # own docstring for the geometry/sign conventions.
    _attach_keypoint_feature_relationships(keypoints, production_areas, selected_water_zone)

    road_corridor_result = road_corridors.identify_road_corridor_candidates(
        boundary_coordinates,
        anchor_lon_lat=anchor_lon_lat,
        dem=dem,
        boundary_polygon_utm=boundary_polygon_utm,
        production_areas=production_areas,
        valleys=valleys,
        selected_water_zone=water_zone_answer,
        hydric_floodplain_union=soil_exclusion_unions["hydric_floodplain_union"],
        floodplain_data_is_fallback=soil_exclusion_unions["hydric_floodplain_is_fallback"],
        canopy_height=canopy_height,
    )
    # road_corridor_result["selected_road_corridor"] collapses to None when
    # the network has no branches -- passing that None straight through
    # would make every downstream consumer below (each of which treats
    # None as "not supplied") trigger its own full self-compute fallback
    # (several whole-DEM Dijkstra runs now, not a cheap ridge mask). This
    # context's own field always holds road_network's full shape instead
    # (branches=[] and all, never None) so an empty network is still a
    # real, explicit answer downstream, not a missing one.
    selected_road_corridor = road_corridor_result["road_network"]

    solar_result = identify_solar_candidate_zones(
        boundary_coordinates,
        dem=dem,
        anchor_lon_lat=anchor_lon_lat,
        boundary_polygon_utm=boundary_polygon_utm,
        production_areas=production_areas,
        valleys=valleys,
        selected_water_zone=water_zone_answer,
        selected_road_corridor=selected_road_corridor,
        hydric_floodplain_union=soil_exclusion_unions["hydric_floodplain_union"],
        floodplain_data_is_fallback=soil_exclusion_unions["hydric_floodplain_is_fallback"],
        canopy_height=canopy_height,
    )
    selected_structure_site = solar_result["selected_structure_site"]

    tree_zone_result = identify_tree_zone_candidates(
        boundary_coordinates,
        dem=dem,
        anchor_lon_lat=anchor_lon_lat,
        boundary_polygon_utm=boundary_polygon_utm,
        production_areas=production_areas,
        valleys=valleys,
        selected_water_zone=water_zone_answer,
        selected_road_corridor=selected_road_corridor,
        hydric_floodplain_union=soil_exclusion_unions["hydric_floodplain_union"],
        floodplain_data_is_fallback=soil_exclusion_unions["hydric_floodplain_is_fallback"],
        canopy_height=canopy_height,
    )
    tree_zone_patches = tree_zone_result["patches"]

    return PipelineContext(
        dem=dem,
        boundary_polygon_utm=boundary_polygon_utm,
        valleys=valleys,
        keypoints=keypoints,
        exclusion_zones=exclusion_result,
        production_areas=production_areas,
        parcel_acres=parcel_acres,
        existing_roads=existing_roads,
        soil_exclusion_unions=soil_exclusion_unions,
        water_zones=water_zones,
        selected_water_zone=selected_water_zone,
        selected_road_corridor=selected_road_corridor,
        selected_structure_site=selected_structure_site,
        tree_zone_patches=tree_zone_patches,
        # One line per module, read off calls this function already makes --
        # narrative support for the report only, never a KSOP dependency
        # (see the field's own comment on PipelineContext). .get(): a result
        # from before a module's narrative_data existed simply carries None.
        narrative_data={
            "exclusion_zones": exclusion_result.get("narrative_data"),
            "production_area_ceiling": optimized_production.get("narrative_data"),
            "water_survey_areas": water_system_result.get("narrative_data"),
            "road_corridors": road_corridor_result.get("narrative_data"),
            "solar_suitability": solar_result.get("narrative_data"),
            "tree_zone_candidates": tree_zone_result.get("narrative_data"),
        },
    )
