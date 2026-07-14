# Farm Design Tool — Backend

Backend services for the regenerative farm design tool. Fetches public
geospatial data (soil, elevation, imagery, hydrology) for a given property
and eventually feeds it into a Scale of Permanence / keyline design report.

## What's here so far

- `soil_data.py` — fetches SSURGO soil survey data from USDA's Soil Data
  Access API for a given point or parcel boundary. No API key needed.

## Running it yourself

This needs internet access to reach USDA's servers, so it won't run inside
a fully offline/sandboxed environment. To test it:

1. Install Python 3.10+ if you don't have it
2. From this folder, install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Run the test script:
   ```
   python soil_data.py
   ```

This will fetch real soil data for the test coordinates in the file (your
property in Richland Township, PA) and print a plain-language summary.

## Next pieces to build

- `elevation_data.py` — pull DEM/elevation data from USGS 3DEP
- `hydrology_data.py` — pull stream/water features from USGS NHD
- `report_generator.py` — combine all data layers and call the Claude API
  to generate the narrative Scale of Permanence report
- A simple web frontend (React) for entering an address and viewing results

## Deploying

Once there's more built out, this backend gets deployed to Render or
Railway (connected to the GitHub repo), which gives it a live URL and
real internet access to reach USDA/USGS APIs.
