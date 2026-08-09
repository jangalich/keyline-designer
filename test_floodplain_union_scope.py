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

Bug 3 (missing post-buffer bound, found after bugs 1 and 2 were both
fixed): bug 1's fix clips each fetched NHD feature's RAW geometry to a
context region around the parcel BEFORE buffering it by
FLOODPLAIN_STREAM_BUFFER_METERS -- but nothing bounded the result AFTER
buffering, so the buffer stroke (and however much stream length survived
the looser fetch-context clip) could still produce a final exclusion
piece extending well past any distance actually relevant to the parcel.
Confirmed live and visually (plotted GeoJSON): an 11.2-acre union of which
only 0.077 acres (0.6%) actually overlapped the real parcel -- a long
buffered band along Montour Run, entirely on the far side of N Montour Rd
from the field. Fixed by intersecting the final unioned/buffered result
against boundary_polygon_utm.buffer(FLOODPLAIN_FINAL_RELEVANCE_BUFFER_METERS)
(75m -- meaningfully smaller than the 200m fetch-context buffer, but
meaningfully larger than the 30m stream buffer alone, so genuine near-
parcel floodplain risk on a differently-shaped property isn't clipped
away just because its own buffer stroke doesn't quite reach the
boundary).

(This file used to also carry a secondary check confirming the module's
now-removed erosion-prone-soil fetch, _fetch_erosion_prone_union(), was
NOT subject to the same over-broad-union bug. That fetch -- and the
erosion-avoidance preference it fed -- has been removed outright, not
merely relocated; see road_corridors.py's own module docstring for why.
The floodplain/hydric regression checks below are unaffected.)
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


# --- bug 3: nothing bounded the final buffered union to a region actually near the parcel ---
#
# Two synthetic streams, both fully inside FLOODPLAIN_FETCH_CONTEXT_BUFFER_METERS
# (200m) so bug 1's fix doesn't clip either one away at fetch time:
#   - a DISTANT one, offset well beyond FLOODPLAIN_FINAL_RELEVANCE_BUFFER_METERS
#     (75m) from the parcel -- mirrors the real Montour Run/N Montour Rd
#     finding (real, mapped, but too far from the field to matter) -- must
#     be fully excluded from the final union by the new post-buffer clip.
#   - a NEAR one, close enough that its buffer stroke reaches the parcel --
#     genuine near-parcel floodplain risk -- must NOT be clipped away.
DISTANT_OFFSET_M = rc.FLOODPLAIN_FINAL_RELEVANCE_BUFFER_METERS + 50.0  # well past the final-relevance bound
NEAR_OFFSET_M = 10.0  # well within it

parcel_north_edge_y = boundary_polygon_utm.bounds[3]

DISTANT_STREAM_GEOMETRY = {
    "type": "LineString",
    "coordinates": [
        [center_x - 3000, parcel_north_edge_y + DISTANT_OFFSET_M, 0],
        [center_x + 3000, parcel_north_edge_y + DISTANT_OFFSET_M, 0],
    ],
}
NEAR_STREAM_GEOMETRY = {
    "type": "LineString",
    "coordinates": [
        [center_x - 100, parcel_north_edge_y + NEAR_OFFSET_M, 0],
        [center_x + 100, parcel_north_edge_y + NEAR_OFFSET_M, 0],
    ],
}


def fake_two_streams(boundary_coordinates):
    return {
        "streams": [
            {"geometry": DISTANT_STREAM_GEOMETRY, "name": "Distant Regional Creek (far side of a road)"},
            {"geometry": NEAR_STREAM_GEOMETRY, "name": "Near Stream (genuine floodplain risk)"},
        ],
        "water_bodies": [],
    }


with patch.object(rc, "get_water_features_for_boundary", fake_two_streams), \
     patch.object(rc, "transform_geom", fake_transform_geom), \
     patch.object(rc, "get_soil_data_for_polygon", fake_get_soil_data_for_polygon):
    bug3_union, bug3_is_fallback = rc._fetch_floodplain_hydric_union(
        BOUNDARY, DEM, valleys=[], boundary_polygon_utm=boundary_polygon_utm
    )

assert bug3_union is not None and not bug3_is_fallback

# The distant stream's buffered corridor must be fully clipped away --
# nowhere in the final union should reach out to it.
distant_stream_buffered = shape(DISTANT_STREAM_GEOMETRY).buffer(rc.FLOODPLAIN_STREAM_BUFFER_METERS)
assert not bug3_union.intersects(distant_stream_buffered), (
    "the distant stream (beyond FLOODPLAIN_FINAL_RELEVANCE_BUFFER_METERS from the parcel) must be fully "
    "excluded from the final union -- this is exactly the live Montour Run/N Montour Rd finding "
    "(an 11.2-acre union of which only 0.077 acres actually overlapped the real parcel)"
)

# The near stream's buffered corridor (genuine near-parcel floodplain risk)
# must survive -- specifically, it must still overlap the parcel itself,
# since it was placed close enough that its 30m buffer reaches the boundary.
assert bug3_union.intersects(boundary_polygon_utm), (
    "the near stream's buffered corridor was placed close enough to genuinely reach the parcel -- it must "
    "NOT be clipped away by the final-relevance bound (that bound must catch distant geometry without "
    "removing real near-parcel floodplain risk)"
)
bug3_union_acres = bug3_union.area / SQUARE_METERS_PER_ACRE
print(f"Post-buffer relevance clip: final union is {bug3_union_acres:.2f} acres, correctly excludes the "
      f"distant stream (>{rc.FLOODPLAIN_FINAL_RELEVANCE_BUFFER_METERS:.0f}m away, mirroring the real Montour "
      f"Run/N Montour Rd spillover) while keeping the near stream's genuine on-parcel-adjacent floodplain risk.")

# --- soil_components= override: a caller-supplied soil_components list skips ---
# --- get_soil_data_for_polygon() entirely, while get_soil_geometries_for_polygon() ---
# --- (a separate, still self-fetched call) still runs and produces the same ---
# --- real hydric-exclusion result as the fully self-fetched path above ---
#
# get_soil_data_for_polygon is stubbed to raise if called at all -- a regression that
# ignores the override and fetches anyway fails loudly here rather than silently
# passing with a self-fetched list. Reuses the same trace-hydric scenario (bug 2
# above) so the override path is proven to produce the IDENTICAL real result the
# self-fetch path already does, not just "doesn't crash."


def _raise_if_soil_data_fetched(wkt_polygon):
    raise AssertionError("get_soil_data_for_polygon must not be called when soil_components= is supplied")


with patch.object(rc, "get_water_features_for_boundary", fake_no_water_features), \
     patch.object(rc, "get_soil_data_for_polygon", _raise_if_soil_data_fetched), \
     patch.object(rc, "get_soil_geometries_for_polygon", side_effect=fake_trace_hydric_geometries) as mock_soil_geometries, \
     patch.object(rc, "transform_geom", fake_transform_geom):
    override_union, override_is_fallback = rc._fetch_floodplain_hydric_union(
        BOUNDARY, DEM, valleys=[], boundary_polygon_utm=boundary_polygon_utm, soil_components=TRACE_HYDRIC_SOIL_ROWS
    )

assert override_union is not None and not override_is_fallback
assert override_union.equals(trace_union), (
    "soil_components= override must produce the exact same union bug 2's self-fetched run above did -- "
    "same input data, just sourced differently (skipping get_soil_data_for_polygon(), NOT get_soil_"
    "geometries_for_polygon(), which is a separate, still self-fetched call -- confirmed it still ran below)"
)
assert mock_soil_geometries.call_count == 1, (
    "get_soil_geometries_for_polygon() is a SEPARATE fetch from the one soil_components= replaces (mukey "
    "GEOMETRY, not composition data) -- it must still run exactly once even with soil_components= supplied, "
    "confirming this override only closes get_soil_data_for_polygon(), not the whole function's self-fetch "
    "surface (see pipeline_context.py's own KNOWN LIMITATIONS #5 for why the geometry fetch remains open)"
)
print(
    "_fetch_floodplain_hydric_union(): a caller-supplied soil_components= skips get_soil_data_for_polygon() "
    "entirely (call would raise if it fired) and produces the exact same real hydric-exclusion union as the "
    "self-fetched path, while get_soil_geometries_for_polygon() (a separate fetch) still runs unchanged."
)

# --- regression: soil_components= omitted (None default) still self-fetches, unchanged ---
with patch.object(rc, "get_water_features_for_boundary", fake_no_water_features), \
     patch.object(rc, "get_soil_data_for_polygon", fake_trace_hydric_soil_rows), \
     patch.object(rc, "get_soil_geometries_for_polygon", fake_trace_hydric_geometries), \
     patch.object(rc, "transform_geom", fake_transform_geom):
    no_override_union, no_override_is_fallback = rc._fetch_floodplain_hydric_union(
        BOUNDARY, DEM, valleys=[], boundary_polygon_utm=boundary_polygon_utm
    )
assert no_override_union is not None and not no_override_is_fallback
assert no_override_union.equals(trace_union), (
    "omitting soil_components= entirely must still self-fetch via get_soil_data_for_polygon(), unchanged "
    "from before this override was added"
)
print("_fetch_floodplain_hydric_union(): omitting soil_components= entirely still self-fetches, unchanged.")

print("\nAll floodplain union scope checks passed.")
