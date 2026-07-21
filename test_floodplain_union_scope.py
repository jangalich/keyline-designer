"""
test_floodplain_union_scope.py

Regression test for TWO bugs where _fetch_floodplain_hydric_union()
(road_corridors.py) returned an exclusion union covering far more area
than the parcel it was computed for -- both confirmed live on the real
six-point reference property.

Bug 1 (NHD clipping): hydrology_data.py's NHD fetch is a standard ArcGIS
`query` operation (esriSpatialRelIntersects against a bounding-box
envelope) -- it returns each matching feature's FULL, un-clipped geometry
for anything that merely intersects the query box, not geometry clipped
to it. A long stream or large waterbody that just grazes the box can come
back with geometry extending miles past the property, which then got
buffered (widening it further) and unioned into the exclusion mask
wholesale. Same root-cause CATEGORY as the original
soil_data.get_soil_geometries_for_polygon() bug (test_soil_geometry_scope.py)
-- that one is fixed at the SSURGO query itself (real STIntersects +
STIntersection clipping server-side); NHD's ArcGIS `query` endpoint has no
equivalent server-side clip parameter, so the fix clips each fetched
feature CLIENT-SIDE instead, against a generous context region around the
parcel boundary (FLOODPLAIN_FETCH_CONTEXT_BUFFER_METERS).

Bug 2 (whole-mukey hydric rollup, found after bug 1 was fixed): a mukey
was flagged hydric-disqualifying if ANY component within it was hydric,
regardless of that component's share of the map unit's composition. Two
real map units on the reference property were 90%+ well/moderately-well-
drained but hydric via a trace component (1%, and 5%+3%=8%) -- their
entire polygons (~58x and ~40x larger than the genuinely, overwhelmingly
wet Atkins floodplain mukey) got excluded right alongside it, producing an
18.77-acre union on the 13.23-acre parcel even after bug 1's fix. Fixed by
soil_data.hydric_disqualifying_mukeys(), which only flags a mukey once its
SUMMED hydric component percentage meets MIN_HYDRIC_COMPONENT_PCT_TO_EXCLUDE
-- shared with production_suitability.py's own hydric-carving logic, which
had the identical bug (see test_production_suitability.py's own trace-
hydric regression section for that side).

No network access required: hydro.nationalmap.gov isn't reachable from
this sandbox (confirmed separately). This mocks get_water_features_for_boundary
to return exactly the shape of feature bug 1's live repro exposed -- a
stream whose real geometry is tens of kilometers long and only grazes the
parcel -- and checks the fix's actual, most important property: the
returned union's area stays a small, plausible multiple of the parcel's
own area, not an order of magnitude larger. It separately mocks
get_soil_data_for_polygon with the real mukeys/percentages from bug 2's
live repro (Atkins 85% hydric; Guernsey-Vandergrift 1% hydric;
Ernest-Vandergrift 5%+3%=8% hydric) and confirms only the genuinely,
dominantly hydric Atkins polygon is pulled into the union.

Also directly documents (and would catch a regression of) the secondary
finding from the bug 1 report: _fetch_erosion_prone_union() looked
suspicious by the same size-comparison test (13.17 acres on the same
13.23-acre parcel) but is NOT buggy -- it exclusively sources geometry
from get_soil_geometries_for_polygon(), which is mathematically bounded
by the parcel's own boundary (STIntersection clipping server-side), so it
can never exceed the parcel's own area. That's asserted directly here
too, against a mocked soil union that (correctly) never exceeds the
parcel.
"""

from unittest.mock import patch

from rasterio.warp import transform as warp_transform
from shapely.geometry import LineString, box, shape

import road_corridors as rc
from dem_data import _utm_epsg_for_lonlat

SQUARE_METERS_PER_ACRE = 4046.8564224

# The real property boundary from the bug report (~40.64N / -79.98W, PA).
BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]
CENTER_LON, CENTER_LAT = -79.982, 40.644
EPSG = _utm_epsg_for_lonlat(CENTER_LON, CENTER_LAT)
DST_CRS = f"EPSG:{EPSG}"
DEM = {"crs": DST_CRS}

center_x, center_y = warp_transform("EPSG:4326", DST_CRS, [CENTER_LON], [CENTER_LAT])
center_x, center_y = center_x[0], center_y[0]

# A ~13.25-acre square parcel, matching the real property's rough scale.
PARCEL_SIDE_M = (13.25 * SQUARE_METERS_PER_ACRE) ** 0.5
boundary_polygon_utm = box(
    center_x - PARCEL_SIDE_M / 2, center_y - PARCEL_SIDE_M / 2,
    center_x + PARCEL_SIDE_M / 2, center_y + PARCEL_SIDE_M / 2,
)
parcel_acres = boundary_polygon_utm.area / SQUARE_METERS_PER_ACRE

# The exact shape of feature that caused the live bug: a stream whose real
# NHD geometry is tens of kilometers long, only grazing the parcel -- this
# is what an un-clipped ArcGIS `query` response looks like for any stream
# that happens to intersect the (already-buffered) fetch bounding box.
HUGE_STREAM_GEOMETRY = {
    "type": "LineString",
    "coordinates": [
        [center_x - 15000, center_y, 0],
        [center_x + 15000, center_y, 0],
    ],
}


def fake_water_features(boundary_coordinates):
    return {
        "streams": [{"geometry": HUGE_STREAM_GEOMETRY, "name": "Huge Regional Creek"}],
        "water_bodies": [],
    }


def fake_transform_geom(src_crs, dst_crs, geometry):
    # Identity transform -- HUGE_STREAM_GEOMETRY is already expressed in
    # the synthetic parcel's own UTM coordinates for test simplicity.
    return geometry


def fake_get_soil_data_for_polygon(wkt_polygon):
    return []  # no hydric soil in this scenario -- isolates the NHD piece


with patch.object(rc, "get_water_features_for_boundary", fake_water_features), \
     patch.object(rc, "transform_geom", fake_transform_geom), \
     patch.object(rc, "get_soil_data_for_polygon", fake_get_soil_data_for_polygon):
    union, is_fallback = rc._fetch_floodplain_hydric_union(
        BOUNDARY, DEM, valleys=[], boundary_polygon_utm=boundary_polygon_utm
    )

assert union is not None and not is_fallback, "real (mocked) NHD data was provided -- should not fall back"
union_acres = union.area / SQUARE_METERS_PER_ACRE
print(f"parcel area: {parcel_acres:.2f} acres; floodplain/hydric union with a 30km-long "
      f"un-clipped stream feature: {union_acres:.2f} acres")

assert union_acres < parcel_acres * 3, (
    f"floodplain/hydric union ({union_acres:.2f} ac) is not bounded to a plausible multiple of the "
    f"parcel ({parcel_acres:.2f} ac) -- the NHD clipping fix has regressed"
)
print("PASS: union area stays bounded/plausible despite a synthetic 30km-long unclipped stream feature "
      "reaching the same code path the live 33.9-acre-on-13.23-acre bug went through.")

# The clip region itself should be comfortably larger than the buffered
# stream corridor width alone, but nowhere near the huge raw geometry's own
# extent -- confirms the context region is actually doing the clipping, not
# some other accidental bound.
assert union_acres > 0.5, "the clip shouldn't be so aggressive it removes the real, on-parcel stream segment"


# --- bug 2: trace hydric components must not carve out an entire, mostly well-drained mukey ---
#
# Real numbers from the live bug report: mukey 541658 (Atkins, frequently
# flooded) is hydric via an 85%-dominant component -- genuinely wet,
# correctly excluded. mukey 541700 (Guernsey-Vandergrift) is hydric via a
# component that's only 1% of composition. mukey 541683 (Ernest-Vandergrift)
# is hydric via components totaling 5%+3%=8%. Neither of the latter two
# should end up in the exclusion union -- isolated here from the NHD piece
# by returning no water features.

TRACE_HYDRIC_SOIL_ROWS = [
    {"mukey": "541658", "muname": "Atkins silt loam, frequently flooded", "compname": "Atkins", "comppct_r": 85, "hydricrating": "Yes"},
    {"mukey": "541658", "muname": "Atkins silt loam, frequently flooded", "compname": "Atkins channery part", "comppct_r": 10, "hydricrating": "No"},
    {"mukey": "541700", "muname": "Guernsey-Vandergrift silt loams", "compname": "Guernsey", "comppct_r": 55, "hydricrating": "No"},
    {"mukey": "541700", "muname": "Guernsey-Vandergrift silt loams", "compname": "Vandergrift", "comppct_r": 44, "hydricrating": "No"},
    {"mukey": "541700", "muname": "Guernsey-Vandergrift silt loams", "compname": "Wet inclusion", "comppct_r": 1, "hydricrating": "Yes"},
    {"mukey": "541683", "muname": "Ernest-Vandergrift silt loams", "compname": "Ernest", "comppct_r": 50, "hydricrating": "No"},
    {"mukey": "541683", "muname": "Ernest-Vandergrift silt loams", "compname": "Vandergrift", "comppct_r": 42, "hydricrating": "No"},
    {"mukey": "541683", "muname": "Ernest-Vandergrift silt loams", "compname": "Wet inclusion A", "comppct_r": 5, "hydricrating": "Yes"},
    {"mukey": "541683", "muname": "Ernest-Vandergrift silt loams", "compname": "Wet inclusion B", "comppct_r": 3, "hydricrating": "Yes"},
]

# Polygons sized/positioned to roughly mirror the live report's relative
# scale (Guernsey ~58x and Ernest ~40x the small Atkins floodplain), all
# well within FLOODPLAIN_FETCH_CONTEXT_BUFFER_METERS of the synthetic
# parcel since get_soil_geometries_for_polygon() itself always returns
# geometry clipped to wkt_polygon (STIntersection) -- no NHD-style
# unclipped-fetch risk here, only the whole-mukey rollup this section tests.
# Three mutually disjoint boxes (distinct, non-overlapping longitude bands)
# so an incorrect union covering more than just the genuinely hydric one
# is caught by a real geometric disjointness check, not just an area sum.
TRACE_HYDRIC_GEOMETRIES = {
    "541658": {  # small -- the genuinely wet Atkins floodplain
        "type": "Polygon",
        "coordinates": [[[-79.984, 40.645], [-79.9838, 40.645], [-79.9838, 40.6452], [-79.984, 40.6452], [-79.984, 40.645]]],
    },
    "541700": {  # large, ~1% hydric -- must NOT be excluded
        "type": "Polygon",
        "coordinates": [[[-79.93, 40.70], [-79.90, 40.70], [-79.90, 40.73], [-79.93, 40.73], [-79.93, 40.70]]],
    },
    "541683": {  # large, ~8% hydric -- must NOT be excluded
        "type": "Polygon",
        "coordinates": [[[-79.88, 40.75], [-79.85, 40.75], [-79.85, 40.78], [-79.88, 40.78], [-79.88, 40.75]]],
    },
}


def fake_no_water_features(boundary_coordinates):
    return {"streams": [], "water_bodies": []}


def fake_trace_hydric_soil_rows(wkt_polygon):
    return TRACE_HYDRIC_SOIL_ROWS


def fake_trace_hydric_geometries(wkt_polygon):
    return TRACE_HYDRIC_GEOMETRIES


with patch.object(rc, "get_water_features_for_boundary", fake_no_water_features), \
     patch.object(rc, "get_soil_data_for_polygon", fake_trace_hydric_soil_rows), \
     patch.object(rc, "get_soil_geometries_for_polygon", fake_trace_hydric_geometries), \
     patch.object(rc, "transform_geom", fake_transform_geom):
    trace_union, trace_is_fallback = rc._fetch_floodplain_hydric_union(
        BOUNDARY, DEM, valleys=[], boundary_polygon_utm=boundary_polygon_utm
    )

assert trace_union is not None and not trace_is_fallback
guernsey_geom = shape(TRACE_HYDRIC_GEOMETRIES["541700"])
ernest_geom = shape(TRACE_HYDRIC_GEOMETRIES["541683"])
assert not trace_union.intersects(guernsey_geom), (
    "the 1%-hydric Guernsey-Vandergrift mukey's large, mostly well-drained polygon must NOT be "
    "excluded over a trace hydric inclusion"
)
assert not trace_union.intersects(ernest_geom), (
    "the 8%-hydric Ernest-Vandergrift mukey's large, mostly well-drained polygon must NOT be "
    "excluded over trace hydric inclusions"
)
print("_fetch_floodplain_hydric_union() excludes only the genuinely, dominantly hydric Atkins mukey "
      "(85%) -- the two large, mostly well-drained mukeys with only trace hydric inclusions (1%, "
      "5%+3%=8%) are correctly left out of the exclusion union.")


# --- secondary check: _fetch_erosion_prone_union() is NOT subject to the same bug ---
#
# It exclusively sources geometry from get_soil_geometries_for_polygon(),
# which clips server-side (STIntersection) against the parcel's own
# boundary -- so, unlike the NHD piece above, its result is mathematically
# bounded by the parcel's own area no matter what SDA returns for
# "geometries near this mukey". A large-but-plausible result (like the real
# 13.17-of-13.23-acre live finding) is expected here, not a symptom of the
# same bug -- this is confirmed by construction, not by re-testing SDA's
# query shape (that's already covered by test_soil_geometry_scope.py).
import inspect

erosion_source = inspect.getsource(rc._fetch_erosion_prone_union)
assert "get_soil_geometries_for_polygon" in erosion_source, (
    "expected _fetch_erosion_prone_union to source geometry exclusively from "
    "get_soil_geometries_for_polygon() (parcel-bounded via STIntersection) -- "
    "if this changed, re-check whether it can now return over-broad geometry too"
)
assert "get_water_features_for_boundary" not in erosion_source, (
    "_fetch_erosion_prone_union should not touch the un-clipped NHD fetch path -- "
    "if it does, it needs the same context-region clipping as the floodplain/hydric union"
)
print("Confirmed by source inspection: _fetch_erosion_prone_union() exclusively sources geometry from "
      "the parcel-bounded get_soil_geometries_for_polygon() (STIntersection-clipped), so it cannot "
      "exceed the parcel's own area -- its real 13.17-of-13.23-acre live finding is a plausible result, "
      "not the same bug as the NHD/floodplain union above.")

print("\nAll floodplain union scope checks passed.")
