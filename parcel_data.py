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
climate_summary, elevation_grid, canopy_height, and imagery_summary. Data
completeness is a precondition for a trustworthy report, not an optional
enhancement -- a missing/broken layer means nothing downstream should run
against incomplete data. Imagery is included in that list even though the
map might seem optional: the map is essential to the report, so an imagery
outage stops the pipeline the same as a DEM outage does.

There is exactly ONE deliberate exception to the hard-fail contract, and
NO others: the irradiance field. It is optional regional context worth a
single narrative sentence, not a constraint any downstream KSOP step
consumes, so a missing/failed baseline must NOT gate a run. See the
comment on the ParcelData.irradiance field for the full rationale. This
exemption is documented in three places (here, the field comment, and the
fetch site) precisely so nobody "fixes" the apparent inconsistency by
folding irradiance back into the hard-fail behavior.

This is a deliberately STRICTER standard than the individual fetch
functions this module calls. Two of them -- imagery_data.
get_imagery_summary_for_boundary() and canopy_height_data.
get_canopy_height_for_boundary() -- document returning None as a genuine,
non-exceptional "nothing usable found" outcome (persistent cloud cover; no
LiDAR HAG coverage for this area), distinct from a raised exception on an
actual request failure. generate_full_report.py's current per-section
graceful degradation treats that None the same way it treats a caught
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

Standalone module only in this branch -- no wiring into
pipeline_context.py, generate_full_report.py, render_layout_map.py, or
any KSOP module yet; that's later, separate branches.
"""

from dataclasses import dataclass
from typing import Optional

from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon

from canopy_height_data import get_canopy_height_for_boundary
from climate_data import get_climate_summary_for_point
from dem_data import get_dem_for_boundary
from elevation_data import get_elevation_grid
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
    """

    def __init__(self, message: str, layer: Optional[str] = None, label: Optional[str] = None):
        super().__init__(message)
        self.layer = layer
        self.label = label


# The (type, label) pairs the two mandatory-layer raises below report as.
# The canopy pair is production_zone_payload.LAYER_CANOPY's, asserted equal
# in test_roads_step.py rather than imported (production_zone_payload
# imports the whole production pipeline; this module is Layer 1 and must
# stay below it).
LAYER_CANOPY = ("canopy", "tree canopy height")
LAYER_IMAGERY = ("imagery", "satellite imagery")


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
    elevation_grid: list[dict]
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
    """
    dem = get_dem_for_boundary(boundary_coordinates)

    boundary_polygon_utm = _boundary_polygon_utm(boundary_coordinates, dem)

    wkt_polygon = coordinates_to_wkt_polygon(boundary_coordinates)
    soil_components = get_soil_data_for_polygon(wkt_polygon)
    farmland_classification = get_farmland_classification_for_polygon(wkt_polygon)
    erosion_factor = get_erosion_factor_for_polygon(wkt_polygon)
    saturated_hydraulic_conductivity = get_saturated_hydraulic_conductivity_for_polygon(wkt_polygon)
    soil_geometries = get_soil_geometries_for_polygon(wkt_polygon)

    water_features = get_water_features_for_boundary(boundary_coordinates)
    farm_roads = get_farm_roads_for_boundary(boundary_coordinates)

    center_lat, center_lon = _boundary_center(boundary_coordinates)
    climate_summary = get_climate_summary_for_point(center_lat, center_lon)

    elevation_grid = get_elevation_grid(boundary_coordinates, grid_size=6)

    canopy_height = get_canopy_height_for_boundary(boundary_coordinates, dem)
    if canopy_height is None:
        raise ParcelDataIncompleteError(
            "get_canopy_height_for_boundary() found no LiDAR HAG coverage for this "
            "boundary -- canopy_height is a mandatory layer in this module, so a "
            "genuine no-coverage result is a hard failure here, not a value to degrade "
            "gracefully on.",
            *LAYER_CANOPY,
        )

    imagery_summary = get_imagery_summary_for_boundary(boundary_coordinates)
    if imagery_summary is None:
        raise ParcelDataIncompleteError(
            "get_imagery_summary_for_boundary() found no recent low-cloud scene for this "
            "boundary -- imagery_summary is a mandatory layer in this module (the map is "
            "essential to the report), so a genuine no-scene result is a hard failure "
            "here, not a value to degrade gracefully on.",
            *LAYER_IMAGERY,
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
        elevation_grid=elevation_grid,
        canopy_height=canopy_height,
        imagery_summary=imagery_summary,
        irradiance=irradiance,
    )
