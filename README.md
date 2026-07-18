# Keyline Designer — Backend

Backend services for the regenerative farm design tool. Fetches public
climate and geospatial data (climate, soil, elevation, hydrology) for a
given property boundary and generates a narrative Scale of Permanence
report using the Claude API.

## What's built and working

- `feature_schema.py` — the shared GeoJSON FeatureCollection data contract
  every vector-data layer wraps its output in: `make_feature()`,
  `make_feature_collection()`, and `validate_feature_collection()`. Every
  feature requires a `properties.confidence` and a non-empty, plain-language
  `properties.confidence_notes` — no layer gets to skip stating its own data
  quality caveats. `hydrology_data.py` and `soil_data.py` are converted to
  it today (see `get_water_features_geojson()` and `get_soil_data_as_geojson()`
  respectively); other layers convert to it in later passes. See
  `test_feature_schema.py` for an offline (no-network) validation example.
- `climate_data.py` — fetches historical wind, rainfall, and temperature
  data from Open-Meteo (free, no API key). Prevailing wind and rainfall
  intensity feed directly into the report's design reasoning; temperature
  is included as reference context.
- `soil_data.py` — fetches SSURGO soil survey data from USDA's Soil Data
  Access API, for either a single point or a full parcel boundary.
  `get_soil_data_as_geojson()` additionally fetches each map unit's actual
  polygon boundary and returns it as a schema-conformant FeatureCollection.
- `elevation_data.py` — fetches elevation data from USGS 3DEP for a point
  or as a grid sampled across a boundary (used to gauge slope/relief).
- `hydrology_data.py` — fetches nearby streams and standing water from
  USGS's National Hydrography Dataset, with a buffer zone so features
  just outside the exact drawn boundary are still caught.
  `get_water_features_geojson()` returns the same fetch as a
  schema-conformant FeatureCollection.
- `dem_data.py` — fetches a real DEM (digital elevation model) raster grid
  for a property from USGS 3DEP's ImageServer (`exportImage`), reprojected
  to the property's local UTM zone so pixels are true meters. This is the
  raster counterpart of `elevation_data.py`'s point-sampled grid — what
  flow-direction/accumulation terrain analysis actually needs.
- `raster_grid.py` — tiny shared, numpy-only helpers (pixel<->coordinate
  math, 8-connected component labeling) used by every module downstream
  of the DEM fetch, so their core logic stays unit-testable against a
  synthetic DEM dict without touching rasterio or the network.
- `valley_delineation.py` — delineates primary valleys from a DEM via
  standard D8 terrain analysis (priority-flood depression fill -> flow
  direction -> flow accumulation -> threshold -> trace). Outputs a
  schema-conformant `valley` layer.
- `production_area.py` — a simple slope-threshold heuristic that
  identifies candidate production/cultivation area(s) from the DEM, as a
  structured elevation reference for the water-zone logic below (a
  narrower, purpose-built stand-in for the same judgment
  `report_generator.py`'s Land Shape narrative section already makes in
  prose — it doesn't modify that step). Outputs a schema-conformant
  `production_area_candidate` layer.
- `water_candidate_zones.py` — the actual water-system candidate-zone
  feature: for each primary valley, flags the portion sitting above a
  candidate production area by at least a configurable minimum gravity
  gradient, outside a configurable property-boundary setback, and
  buffers the qualifying segment(s) into a zone polygon (not a point).
  Outputs the schema-conformant `water_system_candidate` layer that
  `generate_full_report.py`/`report_generator.py` consume. The core
  filtering logic (`find_candidate_zones()`) is a pure function over
  already-computed valleys/production areas — deliberately separable from
  DEM fetching and valley delineation, so "is the terrain data right" and
  "is the zone logic right" can be debugged independently (see
  `test_valley_delineation.py`, `test_production_area.py`,
  `test_water_candidate_zones.py`, and the end-to-end
  `test_water_system_candidate_pipeline.py`).
- `terrain_metrics.py` — DEM-derived slope, aspect (Horn's method), and a
  horizon-based shading proxy (no vegetation/canopy signal — see its
  docstring), feeding `solar_suitability.py`'s scoring. Numpy-only, no
  network, unit-tested against synthetic terrain
  (`test_terrain_metrics.py`).
- `farm_roads_data.py` — fetches real, existing road geometry near a
  property from USGS National Map's Transportation dataset (same
  ArcGIS-`query` pattern as `hydrology_data.py`, different theme).
  Public road/right-of-way data only — a private farm track or driveway
  not captured in that dataset won't appear; see its confidence_notes.
- `soil_data.py` additionally has `get_farmland_classification_for_polygon()`
  and `is_prime_farmland()` — SSURGO's official Farmland Classification
  (`farmlndcl`), used to flag (never exclude) solar candidates that
  overlap prime agricultural soil (`test_farmland_classification.py`).
- `irradiance_data.py` — a regional (not per-candidate) solar production
  baseline from NREL's PVWatts API, for a rough "expect about X kWh/kW/
  year here" report note. Optional (needs a free `NREL_API_KEY`);
  degrades to `None` without one, same as the other optional layers.
- `solar_suitability.py` — the solar suitability data layer for Scale of
  Permanence step 6 (Permanent Buildings): a three-part constraint stack
  — excluded inside/near production zones (`production_area.py`, already
  in main, buffered), required within a configurable proximity buffer of
  a mapped farm road (`farm_roads_data.py`), scored by DEM slope + aspect
  + shading (`terrain_metrics.py`) — producing RANKED candidate zones
  (layer `solar_infrastructure`), not one forced placement. Flags (never
  excludes) SSURGO prime-farmland overlap as a tradeoff note. Same
  pure-core-logic-vs-network-fetch split as `water_candidate_zones.py`
  (`find_candidate_solar_zones()` / `flag_prime_farmland_conflicts()` are
  both network-free and unit-tested against synthetic input —
  `test_solar_suitability.py`, end-to-end wiring in
  `test_solar_suitability_pipeline.py`).
- `soil_data.py` additionally has `get_erosion_factor_for_polygon()` and
  `is_erosion_prone()` (SSURGO K-factor, `kwfact`) and `is_hydric()`
  (SSURGO `hydricrating`) — the erosion-prone-soil and hydric/floodplain
  exclusion signals `road_corridors.py` uses
  (`test_erosion_hydric_soil.py`).
- `road_corridors.py` — computes suggested road corridor geometry from
  the DEM, replacing having Claude infer a plausible-sounding corridor in
  prose (the old Farm Roads behavior) for properties with no existing
  road/access data. Two independently-generated, jointly-ranked corridor
  types (no hardcoded type preference): CONTOUR-BAND (elevation-band
  slicing + PCA/binned-median centerline extraction) and RIDGE-TOP
  (reuses `valley_delineation.py`'s own flow-routing algorithm against an
  *inverted* DEM — a ridge in real terrain is a valley in its negation).
  Excludes production zones (`production_area.py`) and pond/water-system
  zones (`water_candidate_zones.py`, both already in main, reused not
  modified) with their own buffers, plus floodplain/hydric ground (real
  NHD stream/water-body buffers + SSURGO hydric soil — falls back to
  buffered valley lines, flagged in confidence_notes, only if neither
  reachable) and erosion-prone soil (SSURGO K-factor). Grade-capped at a
  pinned, documented `MAX_ROAD_GRADE_PCT` (see the module for the
  rationale). Anchors each candidate to the property boundary — via a
  real mapped road's frontage point where one exists, otherwise an
  explicitly-flagged arbitrary nearest point. Outputs the
  `suggested_road_corridor` layer. Same pure-core-logic-vs-network-fetch
  split as the other candidate-zone features
  (`find_candidate_road_corridors()` is network-free and unit-tested
  against synthetic terrain — `test_road_corridors.py`, end-to-end wiring
  in `test_road_corridors_pipeline.py`).
- `solar_suitability.py`'s road-proximity scoring falls back to the
  top-ranked `suggested_road_corridor` when no existing-road data is
  reachable (real road data always wins when available) — see
  `_suggested_corridor_as_road_fallback()` and
  `test_solar_road_fallback.py`. This is the only change made to
  `solar_suitability.py` in that pass; its scoring logic itself is
  untouched.
- `report_generator.py` — combines all of the above and calls the Claude
  API to generate the narrative Scale of Permanence report.
- `generate_full_report.py` — the full end-to-end pipeline: give it a
  boundary once, it runs every data-fetching step and generates the
  final report. This is the main script to run for a real test.
- `parcel_boundary.py` — fetches legal parcel boundaries from Allegheny
  County's GIS system specifically. Superseded by manual boundary drawing
  (see below) as the actual product approach, but kept as a working
  reference.

## Running it yourself

Needs internet access (won't run in a fully offline sandbox). Setup:

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
export NREL_API_KEY="..."  # optional -- only needed for irradiance_data.py's regional baseline note
python3 generate_full_report.py
```

Individual modules can also be run standalone (`python3 climate_data.py`,
etc.) to test just one data layer without triggering a full report /
Claude API call — useful when only testing a new or changed module.

## Key product decision: manual boundary drawing

Rather than trying to auto-fetch legal parcel boundaries (which vary by
county/state with no unified nationwide API), the tool has users draw
their intended farm area directly on a map. This also correctly handles
the common case where someone wants to design only part of their land
(e.g. 50 of 100 acres). See the frontend project for the actual drawing
tool (built with Leaflet).

## Roadmap / next pieces

**Near-term:**
- Connect the frontend's drawn boundary to this backend (currently two
  separate, unconnected pieces — frontend has the map, backend has the
  pipeline)
- Imagery/land cover data as an additional layer (NAIP/Sentinel-2)
- Uploaded soil test result parsing (PDF/photo → structured data →
  incorporated into the report, alongside SSURGO data)
- Live address autocomplete — tried using Photon (free, OSM-based), but
  its coverage is thin for rural addresses specifically, which is most
  of this tool's actual target audience. Reverted to a simple "type
  full address, hit Enter" search using the Census geocoder, which is
  reliable for rural addresses. Worth revisiting with a paid service
  (e.g. Mapbox, better rural coverage) during a future polish pass if
  live suggestions turn out to be worth the added cost/complexity.

**Future / potential premium tier:**
- Real LiDAR-based *keypoint* detection specifically — a single precise
  pond/dam siting point (storage volume, dam wall geometry) within a
  water system candidate zone. The DEM/raster foundation and
  valley-based *zone* identification this would build on now exists
  (`dem_data.py`, `valley_delineation.py`, `water_candidate_zones.py`) —
  deliberately a zone, not a point, in this pass. Precise keypoint/keyline
  siting on top of it is meaningfully more work (still a plausible paid
  add-on candidate) and is explicitly out of scope for the current layer.
- Validate `valley_delineation.py`/`water_candidate_zones.py` against a
  real property with known ground truth, and tune
  `MIN_STREAM_CONTRIBUTING_AREA_ACRES` / `MIN_PRIMARY_VALLEY_CONTRIBUTING_AREA_ACRES`
  (`valley_delineation.py`), `MAX_PRODUCTION_SLOPE_PCT`
  (`production_area.py`), and `MIN_GRAVITY_GRADIENT` /
  `MIN_BOUNDARY_SETBACK_METERS` (`water_candidate_zones.py`) accordingly —
  all deliberately exposed as module-level constants for exactly this.
- Same ground-truth validation pass for `solar_suitability.py`: check that
  top-ranked candidates are actually outside production zones, near a
  real road, low-slope, and south-facing on a known property, and tune
  `MAX_SOLAR_SLOPE_PCT`, the score weights (`SLOPE_SCORE_WEIGHT` /
  `ASPECT_SCORE_WEIGHT` / `SHADING_SCORE_WEIGHT`), `MIN_SUITABILITY_SCORE`,
  `PRODUCTION_ZONE_EXCLUSION_BUFFER_METERS`, and
  `ROAD_PROXIMITY_BUFFER_METERS` accordingly.
- `terrain_metrics.py`'s shading proxy is DEM-only horizon/terrain
  shading — it has no vegetation/canopy signal at all (no DSM-derived
  canopy height model exists in this pipeline yet). A real canopy height
  model, or a per-pixel NDVI overlay reprojected onto the DEM grid (using
  `imagery_data.py`'s already-merged Sentinel-2 fetch — NOT the separate,
  still-unmerged NLCD/NDVI branch), would be a meaningfully better shading
  signal and is a reasonable next step once the DEM-only version is
  validated against real tree cover on the ground.
- `irradiance_data.py` targets `developer.nlr.gov` per the current
  guidance this was built against (NREL's API domain migrating off
  `developer.nrel.gov`). That's the one constant
  (`NLR_PVWATTS_ENDPOINT`) to double-check/update if that domain changes
  again — this module was written and unit-tested without live network
  access to that endpoint in this environment, so it's unverified against
  a real request.
- `farm_roads_data.py` targets USGS National Map's Transportation
  MapServer (`carto.nationalmap.gov`), following the same
  service-catalog convention as `hydrology_data.py`'s NHD endpoint — also
  unverified against a live request in this environment (no route to
  reach it from here). Confirm the layer ID/response shape against a real
  request before relying on it.
- Ground-truth validation pass for `road_corridors.py` against a real
  no-existing-road property (the explicit target case for this layer):
  confirm both contour-band and ridge-top candidates actually show up
  where terrain supports them, that they route around real production/
  pond zones and real floodplain/erosion-prone soil rather than the
  DEM-only fallback, and tune `MAX_ROAD_GRADE_PCT` (see the module for
  the current rationale/sourcing caveat), `CONTOUR_BAND_WIDTH_METERS`,
  the ridge `RIDGE_MIN_AREA_ACRES` / `RIDGE_MIN_PRIMARY_AREA_ACRES`
  thresholds, the exclusion buffers
  (`PRODUCTION_ZONE_EXCLUSION_BUFFER_METERS`,
  `POND_ZONE_EXCLUSION_BUFFER_METERS`,
  `FLOODPLAIN_STREAM_BUFFER_METERS`), `soil_data.DEFAULT_EROSION_KWFACT_THRESHOLD`,
  and the scoring weights (`GRADE_SCORE_WEIGHT` / `EXCLUSION_MARGIN_WEIGHT`
  / `LENGTH_SCORE_WEIGHT`) accordingly.
- `road_corridors.py`'s PCA-based centerline extraction (contour-band
  candidates) is a simple, explainable thinning heuristic, not a proper
  skeletonization algorithm — on an oddly-shaped or branching low-slope
  patch it can produce a less-than-ideal path through the middle of it.
  Reasonable to revisit with a real raster skeletonization approach if
  ground-truthing shows this matters in practice.

## Deploying

Once ready, this backend deploys to Render or Railway (connected to this
GitHub repo), giving it a live URL with real internet access to reach
USDA/USGS/Open-Meteo APIs. The frontend (separate repo) deploys to
Vercel.
