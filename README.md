# Keyline Designer — Backend

Backend services for the regenerative farm design tool. Fetches public
climate and geospatial data (climate, soil, elevation, hydrology) for a
given property boundary and generates a narrative Scale of Permanence
report using the Claude API.

## What's built and working

- `climate_data.py` — fetches historical wind, rainfall, and temperature
  data from Open-Meteo (free, no API key). Prevailing wind and rainfall
  intensity feed directly into the report's design reasoning; temperature
  is included as reference context.
- `soil_data.py` — fetches SSURGO soil survey data from USDA's Soil Data
  Access API, for either a single point or a full parcel boundary.
- `elevation_data.py` — fetches elevation data from USGS 3DEP for a point
  or as a grid sampled across a boundary (used to gauge slope/relief).
- `hydrology_data.py` — fetches nearby streams and standing water from
  USGS's National Hydrography Dataset, with a buffer zone so features
  just outside the exact drawn boundary are still caught.
- `report_generator.py` — combines all of the above and calls the Claude
  API to generate the narrative Scale of Permanence report.
- `generate_full_report.py` — the full end-to-end pipeline: give it a
  boundary once, it runs all four data-fetching steps and generates the
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
- Real LiDAR-based keypoint detection — downloading actual DEM raster
  data (not just sampled grid points) and running real terrain-analysis
  tools (e.g. WhiteboxTools) to mathematically locate keypoints and
  candidate keylines precisely, rather than having the report estimate
  their general location from a coarse grid. Meaningfully more complex
  to build (raster processing, larger data handling) — a plausible
  candidate for a paid add-on feature once the core free-data pipeline
  is solid and validated.

## Deploying

Once ready, this backend deploys to Render or Railway (connected to this
GitHub repo), giving it a live URL with real internet access to reach
USDA/USGS/Open-Meteo APIs. The frontend (separate repo) deploys to
Vercel.
