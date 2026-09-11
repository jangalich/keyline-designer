"""
parcel_data.py

Fetches every raw, network-backed data layer for a property boundary
EXACTLY ONCE, upfront, before any KSOP computation begins.

This module has no awareness of valleys, production zones, water
suitability, or any other derived KSOP concept -- it only fetches raw
layers and returns them bundled in one dataclass. See pipeline_context.py
for the derived-computation layer built on top of raw data like this.

HARD-FAIL CONTRACT: fetch_parcel_data() raises (uncached, uncaught) on
ANY failure among ANY of the HARD-FAIL layers it fetches -- dem,
soil_components, farmland_classification, erosion_factor, saturated_
hydraulic_conductivity, soil_geometries, water_features, farm_roads,
climate_summary, canopy_height, and imagery_summary. Data completeness is
a precondition for a trustworthy report, not an optional enhancement -- a
missing/broken layer means nothing downstream should run against
incomplete data. Imagery is included in that list even though the
map might seem optional: the map is essential to the report, so an imagery
outage stops the pipeline the same as a DEM outage does.

THE TWO CARVE-OUTS, AND HOW THEY DIFFER. There are exactly TWO named
departures from the contract above and NO others, and they are NOT the
same shape -- reading one as the other is the mistake this paragraph
exists to prevent:

  1. irradiance IS OPTIONAL. The layer can be missing and the run
     continues. It is optional regional context worth a single narrative
     sentence, not a constraint any downstream KSOP step consumes, so a
     missing/failed baseline must NOT gate a run. See the comment on the
     ParcelData.irradiance field for the full rationale. Documented in
     three places (here, the field comment, and the fetch site) precisely
     so nobody "fixes" the apparent inconsistency by folding irradiance
     back into the hard-fail behavior.

  2. canopy_height IS NOT OPTIONAL -- IT HAS A FALLBACK. It did not
     become optional and must not be made optional. What changed is that
     there are now TWO sources for it: USGS 3DEP lidar height-above-
     ground, and, ONLY where HAG is absent for the parcel, NLCD Tree
     Canopy Cover (canopy_cover_data.py). canopy_height_data.get_canopy_
     height_for_boundary() tries the fallback itself before it returns
     None, so a None reaching the check below means BOTH sources have
     nothing for this land. The layer then HARD-FAILS exactly as before.
     Documented in three places (here, the LAYER_CANOPY constant, and the
     raise site) for the same reason irradiance is.

The difference stated plainly: irradiance can be ABSENT and the run
proceeds; canopy can be absent from ONE SOURCE and the run proceeds on
the other, but absent from BOTH still stops the run. A fallback is not an
exemption.

WHICH SOURCE A PARCEL RAN ON IS RECORDED, not inferred. ParcelData.
canopy_height carries its own 'source' key ('lidar_hag' | 'nlcd_tcc'),
read via canopy_height_data.canopy_source(), and it surfaces downstream
as the `canopy_data_source` flag. A TCC parcel is being analysed by a
coarser rule -- 30 m percent cover, where any nonzero value counts as
canopy -- so some ground on it is marked wooded that a walk would show as
two trees in a field. That is not expressible as an availability flag,
because the check genuinely ran.

This is a deliberately STRICTER standard than the individual fetch
functions this module calls. Two of them -- imagery_data.
get_imagery_summary_for_boundary() and canopy_height_data.
get_canopy_height_for_boundary() -- document returning None as a genuine,
non-exceptional "nothing usable found" outcome (persistent cloud cover;
no canopy coverage for this area from EITHER canopy source), distinct
from a raised exception on an actual request failure. generate_full_
report.py's current per-section graceful degradation treats that None the same way it treats a caught
exception: skip the section, keep going. This module does not -- since
ParcelData.imagery_summary and ParcelData.canopy_height are required
dict fields (no Optional, no None default), a None from either fetch is
converted into a raised ParcelDataIncompleteError here, same as any other
hard failure. Once this module is wired into generate_full_report.py (a
later, separate branch), that module's own imagery try/except becomes
dead code -- this function will already have raised before that point is
ever reached. Flagging here so it isn't mistaken for still-active
graceful degradation once that wiring happens. generate_full_report.py's
own broader redesign is out of scope for this branch.

irradiance IS included, as the single deliberately non-hard-failing field
(see the HARD-FAIL CONTRACT carve-out above and the comment on the
dataclass field). It is fetched here exactly once, at Layer 1, from one
representative point (the parcel centroid in WGS84), so no downstream
consumer needs to re-fetch it. Unlike every other field, a missing or
failed irradiance baseline does NOT gate this function:
get_regional_irradiance_baseline() never raises and always returns a
populated dict, so the field is always present and always a dict -- its
'status' key carries whether the numbers are real.

NO ELEVATION-POINT LAYER, DELIBERATELY. Until this branch there was a
thirteenth fetch here: elevation_data.get_elevation_grid(boundary,
grid_size=6), a 6x6 lattice of 36 SEQUENTIAL EPQS point requests with a
time.sleep(0.3) between each. Three timed cold creations put it at 42.8 s,
31.8 s and 118.8 s -- 65%, 65% and 90% of the whole fetch wait, the single
largest cost in a session creation by a wide margin. What consumed it was
ONE SENTENCE in the report: report_generator._format_elevation_summary()
printed its min, max and point count. No KSOP module read it; it appears
in no step registry entry's `consumes`. Meanwhile the dem layer above --
the FIRST fetch, 1-3 s -- already covers the same boundary at ~5 m
resolution, and min/max over that array (raster_grid.elevation_range_in_
polygon(), masked to the boundary by this pipeline's own pixel-center
convention) is microseconds of numpy over thousands of cells rather than
minutes of waiting for 36. The lattice was not merely the slow way to get
those two numbers: it was the WORSE one. It sampled the bounding BOX
rather than the boundary (its own docstring says so), so on a
non-rectangular parcel some of its points stood off-parcel; 36 samples can
miss the real high and low ground a raster resolves; and it came off a
DIFFERENT USGS service (EPQS points) than the design is computed on (the
3DEPElevation ImageServer raster), so the report could quote an elevation
nothing downstream was run against. Do NOT re-add a point-sampled
elevation layer here; the DEM is the source. elevation_data.py itself is
left in place (see its own module docstring).

MEASURED, NOT CHANGED. Every one of the twelve fetches below sits
inside a run_diagnostics.time_layer() block, so a session creation with
KEYLINE_RUN_DIAGNOSTICS set records how long each layer took, in fetch
order, in that session's diagnostic record -- and, for the nine layers
whose module retries, HOW MANY ATTEMPTS that took and how much of the
wait was the pause between them, published by the loops themselves
(fetch_attempts.py) and read off the module here. Those blocks read a
clock on either side of a call that was already there: nothing about
what runs, in what order, how often, or how it retries is different with
them than without. Off (the default) each one costs a thread-local lookup and a
do-nothing singleton. The names are FETCH_LAYERS below, which is also
what run_diagnostics.self_check() cross-checks the compiled function
against, so a thirteenth layer added without a timer is reported rather
than silently missing. See run_diagnostics.py's Group 5.

Standalone module only in this branch -- no wiring into
pipeline_context.py, generate_full_report.py, render_layout_map.py, or
any KSOP module yet; that's later, separate branches.
"""

from dataclasses import dataclass
from typing import Optional

from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon

import run_diagnostics
from canopy_height_data import get_canopy_height_for_boundary
from climate_data import get_climate_summary_for_point
from dem_data import get_dem_for_boundary
from farm_roads_data import get_farm_roads_for_boundary
from hydrology_data import get_water_features_for_boundary
from imagery_data import get_imagery_summary_for_boundary
from irradiance_data import get_regional_irradiance_baseline
from soil_data import (
    coordinates_to_wkt_polygon,
    get_erosion_factor_for_polygon,
    get_farmland_classification_for_polygon,
    get_saturated_hydraulic_conductivity_for_polygon,
    get_soil_data_for_polygon,
    get_soil_geometries_for_polygon,
)


class ParcelDataIncompleteError(RuntimeError):
    """
    Raised when a fetch function returned its own documented "nothing
    usable found" sentinel (None) for a layer this module treats as
    mandatory -- imagery and canopy height both use that convention (see
    module docstring) for a genuine data-gap outcome that isn't a raised
    exception on its own. ParcelData has no Optional fields, so that
    sentinel is converted into a hard failure here instead of being
    passed through as None.

    `layer` / `label` NAME THE LAYER, the same two-field split
    production_zone_payload.LayerFetchError carries: a stable type to
    branch on and display prose to print. Added so a failed session
    creation (session_api.py's POST /api/sessions, which reaches
    fetch_parcel_data() through the fetch cache) can put `failed_layer
    {type, label}` on the wire the way a failed generate job does, instead
    of a 500 that cannot say which source did not answer. Both are None on
    a raise site that predates them, and the API then reports the generic
    error rather than inventing a layer.

    `reason` SAYS WHICH KIND OF FAILURE IT WAS, and exists because the two
    read identically today and must not. A user whose parcel simply has no
    coverage was told "the tree canopy height could not be retrieved" --
    the wording of a transient outage, which invites a retry that can never
    succeed. REASON_SOURCE_UNAVAILABLE is "this source is down" (a retry
    may help); REASON_NO_DATA_FOR_PARCEL is "this source has no data for
    your land" (permanent for this boundary; no retry helps). None on a
    raise site that does not know, and the API then falls back to the
    generic wording rather than guessing which it was.
    """

    # Stable identifiers a consumer branches on, not display prose -- the
    # same split as `layer`/`label` above.
    REASON_SOURCE_UNAVAILABLE = "source_unavailable"
    REASON_NO_DATA_FOR_PARCEL = "no_data_for_parcel"

    def __init__(
        self,
        message: str,
        layer: Optional[str] = None,
        label: Optional[str] = None,
        reason: Optional[str] = None,
    ):
        super().__init__(message)
        self.layer = layer
        self.label = label
        self.reason = reason


# The (type, label) pairs the two mandatory-layer raises below report as.
#
# CANOPY IS MANDATORY AND STAYS MANDATORY (the second of the module
# docstring's two carve-outs). It has a FALLBACK, not an exemption: the
# fetch tries NLCD Tree Canopy Cover wherever lidar HAG is absent and only
# returns None when BOTH sources have nothing for this land. The raise
# below is unconditional on that None -- do not soften it into a
# degrade-and-continue path because "canopy now has a fallback". The
# fallback is what runs BEFORE this point, not instead of it.
# The canopy pair is production_zone_payload.LAYER_CANOPY's, asserted equal
# in test_roads_step.py rather than imported (production_zone_payload
# imports the whole production pipeline; this module is Layer 1 and must
# stay below it).
LAYER_CANOPY = ("canopy", "tree canopy height")
LAYER_IMAGERY = ("imagery", "satellite imagery")


# THE TWELVE LAYERS fetch_parcel_data() FETCHES, IN THE ORDER IT
# FETCHES THEM. Sequential -- no threading, no async -- so this order is
# real: each layer's wait is added to the one before it, the per-layer
# times sum toward the total, and "which layer is this run on" is a
# question with an answer rather than a guess.
#
# WHY THIS ORDER IS WHAT IT IS. Exactly one edge is a genuine dependency:
# canopy_height needs the dem, and is fetched after it for that reason
# and no other (see the entry point's docstring). Everything from
# soil_components through imagery_summary is independent of everything
# beside it and runs sequentially because that is how it was written, not
# because anything requires it. Stated here as an observation for the
# record to be read against; changing it is not this module's business
# today.
#
# NAMES ARE THE ParcelData FIELD NAMES, so a row in a diagnostic record
# points at the field the fetch filled and not at a label invented for
# the record.
#
# DECLARED HERE AND CROSS-CHECKED AGAINST THE COMPILED FUNCTION.
# run_diagnostics._fetch_hook_sites() reads the LOADED fetch_parcel_
# data()'s own constants and reports how many of these names appear in
# it, so a thirteenth layer added without a timer shows up in
# self_check() as "12 of 13" rather than as a row that quietly never
# appears in any record.
FETCH_LAYERS = (
    "dem",
    "soil_components",
    "farmland_classification",
    "erosion_factor",
    "saturated_hydraulic_conductivity",
    "soil_geometries",
    "water_features",
    "farm_roads",
    "climate_summary",
    "canopy_height",
    "imagery_summary",
    "irradiance",
)


@dataclass
class ParcelData:
    dem: dict
    boundary_polygon_utm: Polygon
    soil_components: list[dict]
    farmland_classification: list[dict]
    erosion_factor: list[dict]
    saturated_hydraulic_conductivity: list[dict]
    soil_geometries: dict
    water_features: dict
    farm_roads: list[dict]
    climate_summary: dict
    canopy_height: dict
    imagery_summary: dict
    # THE ONE DELIBERATELY NON-HARD-FAILING LAYER 1 FIELD. Every field above
    # is mandatory: a missing/broken value raises and stops the pipeline
    # (see the module docstring's HARD-FAIL CONTRACT). irradiance is the
    # single exception -- it is optional regional context worth one
    # narrative sentence, NOT a constraint any downstream KSOP step
    # consumes, so a missing baseline must not fail a run. It is ALWAYS
    # present and ALWAYS a dict (never None, never Optional):
    # get_regional_irradiance_baseline() guarantees a populated dict whose
    # 'status' key ("ok"/"no_api_key"/"fetch_failed"/"validation_failed")
    # says whether the numbers are real. Do NOT "fix" this inconsistency by
    # adding it to a hard-fail gate, a completeness check, or an
    # Optional/None default -- the non-hard-failing behavior is the point.
    irradiance: dict


def _boundary_center(boundary_coordinates: list) -> tuple:
    """Rough center point of the boundary, used for the climate lookup
    (climate is regional, not parcel-precise, so one representative point
    is the right level of precision here). Duplicated from generate_full_
    report.py's own helper of the same name -- a 4-line pure function, not
    worth a cross-import."""
    lons = [pt[0] for pt in boundary_coordinates]
    lats = [pt[1] for pt in boundary_coordinates]
    return sum(lats) / len(lats), sum(lons) / len(lons)


def _boundary_polygon_utm(boundary_coordinates: list[tuple[float, float]], dem: dict) -> Polygon:
    """Reprojects boundary_coordinates (WGS84 lon/lat) into dem['crs'] and
    builds the resulting UTM-meters Polygon -- the same warp_transform +
    Polygon(...) pattern pipeline_context.py and nearly every KSOP module
    already duplicates independently. INTENTIONALLY duplicated with
    pipeline_context.py's own _boundary_polygon_utm() for now -- this
    module is standalone in this branch; consolidating the two is a later
    wiring branch's job, not this one's."""
    boundary_xs, boundary_ys = warp_transform(
        "EPSG:4326",
        dem["crs"],
        [pt[0] for pt in boundary_coordinates],
        [pt[1] for pt in boundary_coordinates],
    )
    return Polygon(zip(boundary_xs, boundary_ys))


def fetch_parcel_data(boundary_coordinates: list[tuple[float, float]]) -> ParcelData:
    """
    Fetches every raw data layer needed anywhere downstream in the
    pipeline, exactly once, agnostic of which KSOP step will use it.

    HARD FAILS (raises, uncached, does not degrade) on ANY failure among
    ANY fetched layer -- see module docstring for the full contract,
    including why imagery and canopy height each get an explicit None
    check here despite neither ParcelData field being Optional.

    canopy_height is fetched AFTER dem (real ordering dependency --
    get_canopy_height_for_boundary() requires the DEM as an input, unlike
    every other layer here).

    EVERY LAYER IS TIMED, AND ONLY TIMED. Each fetch below sits inside a
    run_diagnostics.time_layer() block naming the FETCH_LAYERS entry it
    fills and the callable it calls. Those blocks change nothing about
    what runs, in what order, or how often -- they read a clock on either
    side of a call that was already there. With diagnostics off (the
    default) each one is a thread-local lookup and a do-nothing
    singleton; with them on, and only inside a session creation that
    opened a probe, each appends one timing row to that session's
    record. Nothing here reads a returned value, retries anything, or
    decides anything: see run_diagnostics.py's Group 5.
    """
    with run_diagnostics.time_layer("dem", get_dem_for_boundary):
        dem = get_dem_for_boundary(boundary_coordinates)

    boundary_polygon_utm = _boundary_polygon_utm(boundary_coordinates, dem)

    wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
    # THE FIVE SDA CALLS, one after another against the same service. The
    # block to watch when a creation is slow: soil_data._run_sda_query()
    # retries twice with a longer timeout and a 2-second pause each time.
    # That used to be invisible to this caller, so these five rows carried
    # the wait without being able to say how much of it was retry; the
    # loop now publishes both, and each row records its own attempts and
    # its own slept milliseconds beside its elapsed time.
    with run_diagnostics.time_layer("soil_components", get_soil_data_for_polygon):
        soil_components = get_soil_data_for_polygon(wkt_polygon)
    with run_diagnostics.time_layer(
        "farmland_classification", get_farmland_classification_for_polygon
    ):
        farmland_classification = get_farmland_classification_for_polygon(wkt_polygon)
    with run_diagnostics.time_layer("erosion_factor", get_erosion_factor_for_polygon):
        erosion_factor = get_erosion_factor_for_polygon(wkt_polygon)
    with run_diagnostics.time_layer(
        "saturated_hydraulic_conductivity", get_saturated_hydraulic_conductivity_for_polygon
    ):
        saturated_hydraulic_conductivity = get_saturated_hydraulic_conductivity_for_polygon(
            wkt_polygon
        )
    with run_diagnostics.time_layer("soil_geometries", get_soil_geometries_for_polygon):
        soil_geometries = get_soil_geometries_for_polygon(wkt_polygon)

    with run_diagnostics.time_layer("water_features", get_water_features_for_boundary):
        water_features = get_water_features_for_boundary(boundary_coordinates)
    with run_diagnostics.time_layer("farm_roads", get_farm_roads_for_boundary):
        farm_roads = get_farm_roads_for_boundary(boundary_coordinates)

    center_lat, center_lon = _boundary_center(boundary_coordinates)
    with run_diagnostics.time_layer("climate_summary", get_climate_summary_for_point):
        climate_summary = get_climate_summary_for_point(center_lat, center_lon)

    with run_diagnostics.time_layer("canopy_height", get_canopy_height_for_boundary):
        canopy_height = get_canopy_height_for_boundary(boundary_coordinates, dem)
    # THE None CHECK IS OUTSIDE THE TIMER, deliberately. The call itself
    # SUCCEEDED -- it ran, it returned, and its row says "ok" with its
    # real elapsed time; what fails is this module's mandatory-layer rule
    # applied to the sentinel it returned. Putting the raise inside the
    # block would record the fetch as having raised, which it did not,
    # and would attribute the sentinel's cost to the network. The record
    # reports this shape exactly: the layer row is "ok" and the fetch
    # event's outcome names the failed layer.
    if canopy_height is None:
        # BOTH CANOPY SOURCES ARE OUT. get_canopy_height_for_boundary() has
        # already tried the NLCD TCC fallback by the time it returns None
        # (canopy_height_data._tree_canopy_cover_fallback()), so None here
        # means neither lidar HAG nor TCC has data for this boundary -- not
        # that a service failed to answer, which would have RAISED instead.
        # The message says ABSENT, not unresponsive: this is permanent for
        # this parcel and no retry will change it.
        raise ParcelDataIncompleteError(
            "No canopy data exists for this boundary: USGS 3DEP lidar height-above-ground "
            "has no coverage here, and the NLCD Tree Canopy Cover fallback has none either. "
            "This is a permanent gap in the data for this land, not a service outage -- "
            "retrying will not help. canopy_height is a mandatory layer in this module, so "
            "a genuine no-coverage result is a hard failure here, not a value to degrade "
            "gracefully on.",
            *LAYER_CANOPY,
            reason=ParcelDataIncompleteError.REASON_NO_DATA_FOR_PARCEL,
        )

    with run_diagnostics.time_layer("imagery_summary", get_imagery_summary_for_boundary):
        imagery_summary = get_imagery_summary_for_boundary(boundary_coordinates)
    # Outside the timer for canopy_height's reason above, exactly.
    if imagery_summary is None:
        raise ParcelDataIncompleteError(
            "get_imagery_summary_for_boundary() found no recent low-cloud scene for this "
            "boundary -- imagery_summary is a mandatory layer in this module (the map is "
            "essential to the report), so a genuine no-scene result is a hard failure "
            "here, not a value to degrade gracefully on.",
            *LAYER_IMAGERY,
            # Also an absence, not an outage: the search RAN and matched no
            # scene. A failed request would have raised out of the fetch.
            reason=ParcelDataIncompleteError.REASON_NO_DATA_FOR_PARCEL,
        )

    # irradiance: the ONE non-hard-failing field (see the dataclass comment
    # and the module docstring's HARD-FAIL CONTRACT carve-out). Fetched here
    # once, at Layer 1, so nothing downstream re-fetches it. The
    # representative point is the parcel centroid in WGS84: take the shapely
    # centroid of the already-built UTM boundary polygon and warp it back to
    # EPSG:4326 with the same warp_transform helper that built that polygon
    # -- preferred over averaging raw lon/lat, which skews toward wherever
    # the boundary has denser vertices. get_regional_irradiance_baseline()
    # never raises and always returns a populated dict, so there is
    # deliberately NO try/except and NO None check here (unlike
    # canopy_height/imagery above), and this field is intentionally NOT part
    # of any hard-fail gate.
    centroid_utm = boundary_polygon_utm.centroid
    centroid_lons, centroid_lats = warp_transform(
        dem["crs"], "EPSG:4326", [centroid_utm.x], [centroid_utm.y]
    )
    # TIMED LIKE THE OTHER ELEVEN, JUDGED LIKE NONE OF THEM. The timer
    # measures the call, which is all it ever does; it cannot record this
    # layer as a failure because this layer cannot fail -- the function
    # never raises. What says whether the numbers are real is the
    # returned dict's own 'status', which the record reads off
    # ParcelData.irradiance rather than from anything here. The centroid
    # warp above is deliberately outside the block: it is arithmetic, not
    # a fetch.
    with run_diagnostics.time_layer("irradiance", get_regional_irradiance_baseline):
        irradiance = get_regional_irradiance_baseline(centroid_lats[0], centroid_lons[0])

    return ParcelData(
        dem=dem,
        boundary_polygon_utm=boundary_polygon_utm,
        soil_components=soil_components,
        farmland_classification=farmland_classification,
        erosion_factor=erosion_factor,
        saturated_hydraulic_conductivity=saturated_hydraulic_conductivity,
        soil_geometries=soil_geometries,
        water_features=water_features,
        farm_roads=farm_roads,
        climate_summary=climate_summary,
        canopy_height=canopy_height,
        imagery_summary=imagery_summary,
        irradiance=irradiance,
    )
