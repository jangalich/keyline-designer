"""
pipeline_context.py

Computes the shared upstream data several KSOP (Keyline Scale of
Permanence) pipeline steps each already fetch or derive independently --
DEM, boundary polygon, valleys, ridge lines, production areas, existing
roads, soil exclusion unions, water-system candidate zones, the selected
water zone, the selected road corridor, the selected structure (solar)
site, and every ranked tree-zone candidate -- exactly ONCE, and hands the
result back as a single PipelineContext object.

This is a pure orchestrator: it calls the REAL, already-existing entry
points in dem_data.py, valley_delineation.py, production_area_ceiling.py,
farm_roads_data.py, road_corridors.py, water_candidate_zones.py,
water_suitability.py, solar_suitability.py, and tree_zone_candidates.py,
in the dependency order those modules already require. It reimplements
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

  ridge_lines vs valleys: both fields are built by running the exact same
  valley_delineation.delineate_valleys() pipeline (fill -> flow direction
  -> flow accumulation -> threshold -> trace) -- valleys against the real
  DEM, ridge_lines against an elevation-INVERTED copy of it (a ridge in
  real terrain is a valley in its negation -- the same standard-GIS
  technique road_corridors.py's own _identify_ridge_cell_mask() already
  uses). ridge_lines is real ridge-crest geometry, not valley geometry,
  even though the function that produced it is literally
  delineate_valleys() -- do not read ridge_lines as "the valleys of the
  inverted DEM" in any downstream naming, docstring, or future GeoJSON
  layer property; it must read as ridge lines, full stop, everywhere it
  appears.

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
  geometry, buffered by its own ROAD_EXCLUSION_BUFFER_METERS default, or
  None if no mapped roads were found nearby -- the common, clean case,
  not an error).

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

  selected_water_zone is water_suitability.fetch_and_select_optimal_
  water_zone()'s own rank-1 answer (or None if no candidate zones cleared
  scoring) -- this call passes this context's own already-computed dem/
  boundary_polygon_utm/valleys/production_areas straight through via that
  function's own override params, same reuse pattern as water_zones
  above, so nothing it depends on is re-derived a second time.

  selected_road_corridor is road_corridors.identify_road_corridor_
  candidates()'s own 'selected_road_corridor' -- select_optimal_road_
  corridor()'s rank-1 answer, or None if no route cleared the constraint
  stack (including the case where anchor_lon_lat itself is unavailable).
  This call passes this context's own already-computed dem/boundary_
  polygon_utm/valleys/production_areas/selected_water_zone through via
  override params, AND reuses this context's own soil_exclusion_unions
  ['hydric_floodplain_union'] (paired with the real soil_exclusion_
  unions['hydric_floodplain_is_fallback'] flag above) rather than letting
  identify_road_corridor_candidates() fetch a second, independent
  floodplain/hydric union -- so _fetch_floodplain_hydric_union() also
  runs only ONCE across the whole of build_pipeline_context().

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

  water_zones is water_candidate_zones.identify_water_system_candidate_
  zones()'s own 'zones_geojson' FeatureCollection's 'features' list --
  identify_water_system_candidate_zones() is the entry point named for
  this field, and it only ever returns GeoJSON-wrapped output (WGS84
  geometry_wgs84, not the raw shapely-UTM zone dicts find_candidate_
  zones() itself builds internally and discards before returning); this
  is the closest real list[dict] that entry point can produce. This call
  passes this context's own already-computed dem/boundary_polygon_utm/
  valleys/production_areas straight through via that function's own
  override params, so delineate_valleys()/identify_production_areas()
  (or identify_optimized_production_areas(), whichever `production_areas`
  above actually came from) genuinely run only ONCE each across the whole
  of build_pipeline_context() -- see test_pipeline_context.py's own
  call-count assertions. A future consumer that needs each zone's real
  UTM polygon_utm/render_fill_polygon_utm (the way road_corridors.
  find_road_routes() needs its own selected_water_zone's shapely
  geometry) still won't get it from this field, though -- that's a
  return-SHAPE limitation of identify_water_system_candidate_zones()
  itself (GeoJSON-only output), independent of and not addressed by the
  override params this branch wired through. See KNOWN LIMITATIONS #1
  for the one piece of this call that still isn't de-duplicated: canopy/
  road exclusion inputs.

KNOWN LIMITATIONS (found while building this, deliberately NOT worked
around here -- flagging per this branch's own instructions rather than
silently patching another module or reimplementing its logic)

  1. water_candidate_zones.identify_water_system_candidate_zones() now
     accepts dem/boundary_polygon_utm/valleys/production_areas as
     overrides (a prior branch added these, mirroring the dem override it
     already had), and the water_zones call above passes this context's
     own already-computed values for all four -- delineate_valleys()/
     identify_production_areas() (or identify_optimized_production_areas(),
     whichever `production_areas` above came from) are NOT called a
     second time for this field anymore. What's still NOT de-duplicated,
     evaluated and deliberately left alone rather than wired through:
       - road_exclusion_union_utm: identify_water_system_candidate_zones()
         does not expose this as a parameter at all -- it always computes
         its own internally (_fetch_road_exclusion_union_utm()) and passes
         it to find_candidate_zones() as an explicit keyword argument; a
         caller-supplied value threaded through **zone_kwargs would
         collide with that explicit kwarg and raise TypeError. Even if the
         parameter existed, this context's own `existing_roads` field
         (farm_roads_data.get_road_exclusion_union_utm(boundary_coordinates,
         dem), the default ROAD_EXCLUSION_BUFFER_METERS = 0.0m buffer)
         isn't the right value to pass anyway -- water_candidate_zones.py's
         own water-zone road exclusion needs a DIFFERENT, deliberately
         separate, independently-tunable buffer
         (WATER_ZONE_ROAD_BUFFER_METERS = 3.048m/10ft; see that module's
         own docstring for why the two buffers are kept as separate
         constants rather than one shared value). Two independent reasons
         this isn't a clean pass-through, not one.
       - canopy_root_zone_mask_utm: same story on the missing-parameter
         side (the canopy gate is unconditionally fetched-or-raised
         inside identify_water_system_candidate_zones(), no override path
         at all) -- and PipelineContext itself has no tree-root-zone-mask
         field to offer in the first place; this context's own field list
         (dem, boundary_polygon_utm, valleys, ridge_lines,
         production_areas, existing_roads, soil_exclusion_unions,
         water_zones, selected_water_zone, selected_road_corridor) never
         included one. Adding one would be real new
         scope (a new PipelineContext field, plus deciding whether/how a
         single shared canopy fetch can be reused across future steps
         that may each want it at a different buffer distance the way
         roads already do) -- flagged here, not added.
     Both would require modifying water_candidate_zones.py itself (adding
     the two missing override params, and resolving the buffer-mismatch/
     new-field questions above) -- out of scope for this branch, which was
     told not to touch that module further.

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
"""

from dataclasses import dataclass

from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

import dem_data
import farm_roads_data
import production_area_ceiling
import road_corridors
import valley_delineation
import water_candidate_zones
from solar_suitability import identify_solar_candidate_zones
from tree_zone_candidates import identify_tree_zone_candidates
from water_suitability import fetch_and_select_optimal_water_zone


@dataclass
class PipelineContext:
    dem: dict
    boundary_polygon_utm: Polygon
    valleys: list[dict]
    ridge_lines: list[dict]
    production_areas: list[dict]
    existing_roads: BaseGeometry | None
    soil_exclusion_unions: dict[str, BaseGeometry | None]
    water_zones: list[dict]
    selected_water_zone: dict | None
    selected_road_corridor: dict | None
    selected_structure_site: dict | None
    tree_zone_patches: list[dict]


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


def build_pipeline_context(
    boundary_coordinates: list[tuple[float, float]],
    anchor_lon_lat: tuple[float, float],
) -> PipelineContext:
    """
    Computes every shared upstream input multiple KSOP pipeline steps
    need, exactly once. Does not call report_generator.py, generate_full_
    report.py, or fencing.py's own identify_*_candidate*() consumer
    function -- that module doesn't have overrides yet, so wiring it in is
    later, separate work.

    anchor_lon_lat is the real, chosen access point road routing starts
    from -- it's passed straight through to identify_road_corridor_
    candidates(), identify_solar_candidate_zones(), and identify_tree_
    zone_candidates() below (see selected_road_corridor, selected_
    structure_site, tree_zone_patches).
    """
    dem = dem_data.get_dem_for_boundary(boundary_coordinates)
    boundary_polygon_utm = _boundary_polygon_utm(boundary_coordinates, dem)

    valleys = valley_delineation.delineate_valleys(dem)
    ridge_lines = valley_delineation.delineate_valleys(road_corridors._invert_dem(dem))

    optimized_production = production_area_ceiling.identify_optimized_production_areas(
        boundary_coordinates, dem=dem
    )
    production_areas = optimized_production["scored_patches"]

    existing_roads = farm_roads_data.get_road_exclusion_union_utm(boundary_coordinates, dem)

    hydric_floodplain_union, hydric_floodplain_is_fallback = road_corridors._fetch_floodplain_hydric_union(
        boundary_coordinates, dem, valleys, boundary_polygon_utm
    )
    soil_exclusion_unions = {
        "hydric_floodplain_union": hydric_floodplain_union,
        "hydric_floodplain_is_fallback": hydric_floodplain_is_fallback,
        # See module docstring, KNOWN LIMITATIONS #3 -- no shared
        # union-builder for erosion-prone soil currently exists to call.
        "erosion_prone_union": None,
    }

    water_system_result = water_candidate_zones.identify_water_system_candidate_zones(
        boundary_coordinates,
        dem=dem,
        boundary_polygon_utm=boundary_polygon_utm,
        valleys=valleys,
        production_areas=production_areas,
    )
    water_zones = water_system_result["zones_geojson"]["features"]

    selected_water_zone = fetch_and_select_optimal_water_zone(
        boundary_coordinates,
        dem=dem,
        boundary_polygon_utm=boundary_polygon_utm,
        valleys=valleys,
        production_areas=production_areas,
    )

    road_corridor_result = road_corridors.identify_road_corridor_candidates(
        boundary_coordinates,
        anchor_lon_lat=anchor_lon_lat,
        dem=dem,
        boundary_polygon_utm=boundary_polygon_utm,
        production_areas=production_areas,
        valleys=valleys,
        selected_water_zone=selected_water_zone,
        hydric_floodplain_union=soil_exclusion_unions["hydric_floodplain_union"],
        floodplain_data_is_fallback=soil_exclusion_unions["hydric_floodplain_is_fallback"],
    )
    selected_road_corridor = road_corridor_result["selected_road_corridor"]

    solar_result = identify_solar_candidate_zones(
        boundary_coordinates,
        dem=dem,
        anchor_lon_lat=anchor_lon_lat,
        boundary_polygon_utm=boundary_polygon_utm,
        production_areas=production_areas,
        valleys=valleys,
        selected_water_zone=selected_water_zone,
        selected_road_corridor=selected_road_corridor,
        hydric_floodplain_union=soil_exclusion_unions["hydric_floodplain_union"],
        floodplain_data_is_fallback=soil_exclusion_unions["hydric_floodplain_is_fallback"],
    )
    selected_structure_site = solar_result["selected_structure_site"]

    tree_zone_result = identify_tree_zone_candidates(
        boundary_coordinates,
        dem=dem,
        anchor_lon_lat=anchor_lon_lat,
        boundary_polygon_utm=boundary_polygon_utm,
        production_areas=production_areas,
        valleys=valleys,
        selected_water_zone=selected_water_zone,
        selected_road_corridor=selected_road_corridor,
        hydric_floodplain_union=soil_exclusion_unions["hydric_floodplain_union"],
        floodplain_data_is_fallback=soil_exclusion_unions["hydric_floodplain_is_fallback"],
    )
    tree_zone_patches = tree_zone_result["patches"]

    return PipelineContext(
        dem=dem,
        boundary_polygon_utm=boundary_polygon_utm,
        valleys=valleys,
        ridge_lines=ridge_lines,
        production_areas=production_areas,
        existing_roads=existing_roads,
        soil_exclusion_unions=soil_exclusion_unions,
        water_zones=water_zones,
        selected_water_zone=selected_water_zone,
        selected_road_corridor=selected_road_corridor,
        selected_structure_site=selected_structure_site,
        tree_zone_patches=tree_zone_patches,
    )
