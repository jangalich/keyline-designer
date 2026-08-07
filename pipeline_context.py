"""
pipeline_context.py

Computes the shared upstream data several KSOP (Keyline Scale of
Permanence) pipeline steps each already fetch or derive independently --
DEM, boundary polygon, valleys, ridge lines, production areas, existing
roads, soil exclusion unions, and water-system candidate zones -- exactly
ONCE, and hands the result back as a single PipelineContext object.

This is a pure orchestrator: it calls the REAL, already-existing entry
points in dem_data.py, valley_delineation.py, production_area_ceiling.py,
farm_roads_data.py, road_corridors.py, and water_candidate_zones.py, in
the dependency order those modules already require. It reimplements none
of their logic. It does NOT call report_generator.py, generate_full_
report.py, or any of the identify_*_candidate*() consumer functions in
road_corridors.py, solar_suitability.py, or fencing.py -- this module is
not wired into any of those in this pass; that wiring is later, separate
work. See KNOWN LIMITATIONS below for the remaining gaps this surfaced.

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
  buffers that module's own docstring documents) and 'erosion_prone_union'
  -- see KNOWN LIMITATIONS below for why the latter is always None here.

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
         water_zones) never included one. Adding one would be real new
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
    need, exactly once. Does not call report_generator.py or any of the
    identify_*_candidate*() consumer functions in road_corridors.py,
    solar_suitability.py, or fencing.py -- those are wired in later
    branches.

    anchor_lon_lat is accepted (matching the eventual road/fencing
    consumers' own required real access-point input) but not used by
    anything computed here yet -- none of this module's own fields
    (DEM, valleys/ridge lines, production areas, existing roads, soil
    exclusion unions, water zones) depend on it; it exists on this
    function's signature now so later branches that wire in anchor-
    dependent steps (road_corridors.py) don't need to change this
    function's call sites to add it.
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

    hydric_floodplain_union, _hydric_floodplain_is_fallback = road_corridors._fetch_floodplain_hydric_union(
        boundary_coordinates, dem, valleys, boundary_polygon_utm
    )
    soil_exclusion_unions = {
        "hydric_floodplain_union": hydric_floodplain_union,
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

    return PipelineContext(
        dem=dem,
        boundary_polygon_utm=boundary_polygon_utm,
        valleys=valleys,
        ridge_lines=ridge_lines,
        production_areas=production_areas,
        existing_roads=existing_roads,
        soil_exclusion_unions=soil_exclusion_unions,
        water_zones=water_zones,
    )
