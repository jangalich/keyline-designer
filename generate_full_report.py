"""
generate_full_report.py

The real end-to-end pipeline: give it a property boundary once, and it
fetches every raw data layer, computes the full KSOP context, and
generates a Scale of Permanence report — no manual copy-pasting between
scripts, and no layer fetched or computed more than once.

    boundary --> parcel_data.fetch_parcel_data (every raw KSOP data layer
                     -- soil, elevation, hydrology, climate, imagery, DEM,
                     farm roads, canopy, irradiance -- fetched EXACTLY ONCE,
                     HARD-FAILING on any missing/broken layer)
             --> pipeline_context.build_pipeline_context (every shared
                     derived KSOP input -- valleys, production areas,
                     water/road/solar/tree candidate zones, keypoints --
                     computed EXACTLY ONCE, HARD-FAILING on any failure)
             --> the selected winners + already-computed context values
                     forwarded straight to the report (selected water zone,
                     selected structure site, road network, production-area
                     FeatureCollection, parcel acreage) -- no extra KSOP
                     recompute, no fencing pass
             --> report_generator (Scale of Permanence narrative via Claude)
             --> printed report

Architecture note -- this file no longer self-fetches or self-computes
anything per section. Raw data comes from the single fetch_parcel_data()
call; every derived KSOP layer comes from the single build_pipeline_
context() call. Both HARD-FAIL, uncaught: a report built on incomplete
raw data or a failed KSOP computation should never be generated, the same
standard parcel_data.py already enforces for raw data. There is
deliberately NO per-section try/except graceful degradation here anymore
-- see parcel_data.py's own module docstring for the full contract this
mirrors. This matches the pipeline architecture documented in pipeline-
architecture-guide.md.

Requires ANTHROPIC_API_KEY to be set in your environment (see
report_generator.py for details).
"""

from parcel_data import fetch_parcel_data
from pipeline_context import build_pipeline_context
from production_suitability import production_suitability_to_geojson
from road_corridors import validate_access_point_on_boundary
from report_generator import generate_scale_of_permanence_report


def generate_full_report(boundary_coordinates: list, anchor_lon_lat: tuple[float, float]) -> str:
    """
    Runs the full pipeline for a given property boundary (list of
    (longitude, latitude) tuples) and returns the final narrative report.

    anchor_lon_lat is the real, user-picked access point (a single
    (lon, lat) pair, same convention as boundary_coordinates) -- required,
    not optional: it's the point road-corridor routing starts from (see
    road_corridors.py's own module docstring), and there's no reasonable
    default to invent for it (a real anchor is a product decision -- where
    does this property actually connect to the outside world -- not
    something derivable from the boundary alone). Validated up front via
    validate_access_point_on_boundary() -- fails loud with a ValueError
    rather than silently accepting a point that isn't actually on this
    property's own boundary.
    """
    validate_access_point_on_boundary(boundary_coordinates, anchor_lon_lat)

    print("Fetching all raw parcel data (soil, elevation, hydrology, climate, imagery, DEM, roads, canopy)...")
    # HARD FAILS here, uncaught -- same contract as parcel_data.py's own
    # module docstring. A raw-data failure stops the report before any KSOP
    # computation begins; there is no per-layer try/except here anymore.
    parcel_data = fetch_parcel_data(boundary_coordinates)
    stream_count = len(parcel_data.water_features["streams"])
    waterbody_count = len(parcel_data.water_features["water_bodies"])
    print(
        f"  {len(parcel_data.soil_components)} soil component(s), "
        f"{len(parcel_data.elevation_grid)} elevation point(s), "
        f"{stream_count} stream(s)/{waterbody_count} water body/bodies, "
        f"imagery scene {parcel_data.imagery_summary['scene_date']}.\n"
    )

    print("Building KSOP pipeline context (valleys, production, water/road/solar/tree zones, keypoints)...")
    # ALSO HARD FAILS here, uncaught (Option A): a derived-computation
    # failure anywhere in the KSOP chain (water/road/solar/tree zones and
    # keypoints are ALL computed inside this one call) stops the whole
    # report. This single upstream hard-fail replaces the old per-section
    # graceful degradation entirely -- that behavior is gone by design, not
    # an oversight.
    context = build_pipeline_context(
        boundary_coordinates,
        anchor_lon_lat,
        dem=parcel_data.dem,
        boundary_polygon_utm=parcel_data.boundary_polygon_utm,
        soil_components=parcel_data.soil_components,
        farm_roads=parcel_data.farm_roads,
        water_features=parcel_data.water_features,
        soil_geometries=parcel_data.soil_geometries,
        canopy_height=parcel_data.canopy_height,
        # The water step's soil trio completes with this: alongside
        # soil_components/soil_geometries above, it lets build_pipeline_
        # context() hand the water step its soil_inputs from ParcelData's
        # own Layer-1 fetches (fetched once, hard-fail governed) instead
        # of the step re-fetching SDA inline.
        saturated_hydraulic_conductivity=parcel_data.saturated_hydraulic_conductivity,
    )
    print("  KSOP context built.\n")

    # water: the report now narrates only the SELECTED water zone (the winner
    # already chosen inside build_pipeline_context()), not the full ranked
    # candidate list -- alternatives are out of scope for the narrative.
    # context.selected_water_zone is a raw candidate dict carrying Shapely
    # geometry (not a GeoJSON feature); it is stored and unused by
    # report_generator.py -- the WATER SUPPLY data block is formatted from
    # context.narrative_data["water_candidate_zones"] instead.
    selected_water_zone = context.selected_water_zone

    # road_network: context.selected_road_corridor already IS build_road_
    # network()'s full network dict (NEVER None -- an empty network is
    # branches=[], not None; see pipeline_context.py's own comment). Stored
    # through to the report generator for compatibility, but the FARM ROADS
    # data block is formatted from context.narrative_data["road_corridors"]
    # now (the pre-digested narrative block built off this same network),
    # not from this raw dict. No second identify_road_corridor_candidates()
    # call.
    road_network = context.selected_road_corridor

    # keypoints: already computed inside build_pipeline_context() and carried
    # on the context (keypoint_detection.detect_keypoints()'s own list). No
    # separate identify_keypoints() call here anymore -- that redundant call
    # is deleted.
    keypoints = context.keypoints

    # solar: the report narrates only the SELECTED structure site (the winner
    # already chosen inside build_pipeline_context()), not the full ranked
    # list -- alternatives are out of scope for the narrative. The second
    # identify_solar_candidate_zones() call this file used to make (for the
    # full ranked list, an accepted bounded-2x recompute) is therefore gone.
    # context.selected_structure_site is a raw candidate dict carrying Shapely
    # geometry (not a GeoJSON feature); stored and unused this branch.
    selected_structure_site = context.selected_structure_site

    # production_areas_geojson: the "production_area_candidate" FeatureCollection
    # for the ceiling-trimmed scored patches already on the context -- the same
    # production areas the map draws -- wrapped from values already in memory
    # (no new fetch/compute). Stored and unused by report_generator.py this
    # branch. (Fencing is no longer computed here at all; it still renders on
    # the map via render_layout_map.py's own identify_fencing() call.)
    production_areas_geojson = production_suitability_to_geojson(context.production_areas)

    print("Generating Scale of Permanence report via Claude...\n")
    report = generate_scale_of_permanence_report(
        parcel_data.soil_components,
        parcel_data.elevation_grid,
        parcel_data.water_features,
        parcel_data.climate_summary,
        parcel_data.imagery_summary,
        selected_water_zone,
        selected_structure_site,
        road_network,
        keypoints=keypoints,
        irradiance=parcel_data.irradiance,
        parcel_acres=context.parcel_acres,
        production_areas_geojson=production_areas_geojson,
        # The per-module narrative blocks captured on the context -- what the
        # production/water/road/tree/solar data blocks in the report prompt
        # are actually formatted from now (see report_generator.py's own
        # docstring); the raw winner dicts above stay forwarded for
        # compatibility but no longer feed any formatter.
        narrative_data=context.narrative_data,
        # Enables each keypoint's cardinal position on the map
        # (report_generator._locative_descriptor()) -- the same boundary
        # polygon every keypoint's own point_utm shares a CRS with.
        boundary_polygon_utm=context.boundary_polygon_utm,
    )

    return report


if __name__ == "__main__":
    # The user's real, drawn property boundary
    property_boundary = [
        (-79.9838154, 40.6458343),
        (-79.9836701, 40.6428581),
        (-79.9813665, 40.6440549),
        (-79.9804741, 40.6445667),
        (-79.9827466, 40.6458894),
        (-79.9838258, 40.6458343),
    ]

    # A real point ON this boundary's own edge (the midpoint of its first
    # segment) -- NOT render_layout_map.py's shared reference-property
    # placeholder, which sits ~10m off this exact boundary and would fail
    # validate_access_point_on_boundary()'s own tolerance check now that
    # this entry point enforces it.
    access_point = (-79.98374275, 40.6443462)

    report = generate_full_report(property_boundary, access_point)

    print("=" * 60)
    print(report)
