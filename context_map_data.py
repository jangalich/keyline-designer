"""
context_map_data.py

THE SITE OVERVIEW'S CONTEXT MAP DATA: the land for about a mile around
the parcel -- elevation, streams and roads -- fetched at report time from
the three services Layer 1 already reaches, at a wider extent.

    get_context_dem_for_boundary(boundary)    -> dem_data's DEM dict
    get_context_water_for_boundary(boundary)  -> hydrology_data's water dict
    get_context_roads_for_boundary(boundary)  -> farm_roads_data's road rows

A REPORT-LAYER FETCH, NEVER LAYER 1. Nothing on the design path needs a
mile of surrounding country, so these are three DEGRADABLE rows in
report_data.REPORT_FETCH_LAYERS, each failing on its own: without the
DEM the map has no contours and the overview no landscape position;
without streams or roads the map draws without them.

THE SAME FETCH FUNCTIONS, A WIDER BUFFER. Each call is Layer 1's own
function with `buffer_meters=CONTEXT_BUFFER_METERS` -- one mile past the
parcel's bounding box on every side, about two miles across for a small
farm. No new host, no new query shape.

THE GRID THE CAP FORCES. dem_data.MAX_GRID_DIMENSION clamps the grid at
300 cells a side, so the resolution is whatever the window divides into:
11.7 x 11.9 m for the reference parcel's 3.5 x 3.56 km window. That is
coarse by design and fine for its job -- contours at 20 ft (6.1 m) with
100 ft index lines, drawn at about 7 m to the point -- and it is why the
map's caption says the contours are not the parcel's terrain (Landform's
5 m grid is).

MEASURED FOR THE REFERENCE PARCEL (branch 17, step 0): six requests, DEM
591 KB, NHD flowlines 103 KB (48) and waterbodies 18 KB (14), roads 28 KB
+ 218 KB over the three road layers (171 segments, every one named); no
service reached its transfer limit.
"""

import dem_data
import farm_roads_data
import hydrology_data

CONTEXT_BUFFER_METERS = 1609.344  # one mile

CONTEXT_CITATION = {
    "dem": "USGS 3DEP elevation, resampled onto a 300-cell grid over the parcel's extent plus one mile.",
    "water": "USGS National Hydrography Dataset, flowlines and waterbodies at 1:24,000, the parcel's extent plus one mile.",
    "roads": "USGS National Map transportation, local road layers (from Census TIGER/Line), the parcel's extent plus one mile.",
}


def get_context_dem_for_boundary(boundary_coordinates: list) -> dict:
    return dem_data.get_dem_for_boundary(boundary_coordinates, buffer_meters=CONTEXT_BUFFER_METERS)


def get_context_water_for_boundary(boundary_coordinates: list) -> dict:
    return hydrology_data.get_water_features_for_boundary(boundary_coordinates, buffer_meters=CONTEXT_BUFFER_METERS)


def get_context_roads_for_boundary(boundary_coordinates: list) -> list:
    return farm_roads_data.get_farm_roads_for_boundary(boundary_coordinates, buffer_meters=CONTEXT_BUFFER_METERS)
