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

## Deploying

Once ready, this backend deploys to Render or Railway (connected to this
GitHub repo), giving it a live URL with real internet access to reach
USDA/USGS/Open-Meteo APIs. The frontend (separate repo) deploys to
Vercel.
