"""
production_zone_payload.py

Assembles the single JSON payload the interactive frontend's production-zone
step reads: the eligible-ground highlight, the five per-gate exclusion layers,
the ranked suggested zones, and the summary figures that caption them.

WHY THIS IS A MODULE AND NOT A FLASK ROUTE BODY
-----------------------------------------------
Two things happen here that are real logic rather than HTTP plumbing: the
UPSTREAM FETCH ORDER (which layer is fetched once and shared, and which
failure is allowed to name itself) and the PAYLOAD CONTRACT (what the wire
carries, and at what coordinate precision). api.py stays what it already is
for every other endpoint -- request validation, status-code mapping, and
nothing else.

THE SHARED FETCHES, AND THE TWO THAT ARE STILL DOUBLED
------------------------------------------------------
This payload needs BOTH exclusion_zones.identify_exclusion_zones() (for the
per-gate geometries and the eligible union -- production does not return
either) and production_area_ceiling.identify_optimized_production_areas()
(for the ranked zones and the summary figures). Called naively that is two
of every upstream fetch.

DEM and CANOPY are fetched here, ONCE, and passed into both -- they are the
two expensive ones (a 3DEP raster fetch and a Planetary Computer STAC search
plus HAG read), and both entry points already accept the standard
None-falls-back-to-self-fetch override, so this is a pure pass-through with
no behavior change.

SOIL and ROADS are STILL FETCHED TWICE, and that is a known limitation rather
than an oversight. identify_exclusion_zones() accepts
disqualifying_soil_union_utm= / road_exclusion_union_utm= overrides;
identify_optimized_production_areas() does NOT -- it owns its own fetch and
exposes only check_soil / check_roads toggles. Pre-fetching them here would
move one of the two calls rather than remove it. Closing this properly means
adding those two overrides to production_area_ceiling.py's entry point, which
is a change to a pipeline module that render_layout_map.py and
tree_zone_candidates.py also call, and so is deliberately NOT bundled into
the branch that first exposes this endpoint.

WHAT A FAILURE IS ALLOWED TO SAY
--------------------------------
Only two layers can hard-fail this request, and the frontend's error state
exists to name them:

  elevation -- no DEM, no slope, no gates, no answer.
  canopy    -- MANDATORY by pipeline design (see exclusion_zones.py's
               GRACEFUL DEGRADATION note). A missing or too-sparse HAG layer
               raises rather than degrading, because "no trees here" and "we
               could not look" are different claims about someone's land.

SOIL and ROADS DO NOT HARD-FAIL. They degrade to an empty layer with
`data_available: false`, and the ground they would have excluded is then
reported as ELIGIBLE. That is the dangerous case: a payload that arrives
looking complete while the highlight overstates available ground. Nothing is
hidden here -- every layer ships its own `data_available` flag, and a
consumer that renders the highlight without reading them is showing a
measurement where it should be showing an unknown.

WHICH PRODUCTION GEOMETRY THE WIRE CARRIES
------------------------------------------
THE OPENING, NOT THE FOOTPRINT. Every production patch carries two shapes
and they are not interchangeable:

  polygon_utm / area_acres -- the REAL per-cell union footprint. Every
    5 m cell that cleared every gate, with the cell staircase intact and
    every one-cell finger still attached. It is the honest answer to "what
    ground qualified".

  render_fill_polygon_utm / render_fill_area_acres -- a bounded, asymmetric
    DISC OPENING of that footprint at production_area.
    RENDER_OPENING_RADIUS_METERS (12 m, so a 24 m disc) with a
    RENDER_LEAD_ERODE_CELLS lead erode. Anti-extensive by construction and
    asserted so: it severs features narrower than 2*(r + lead), restores
    only r, and insets every edge by the lead. It is the answer to "what
    shape would you actually work".

The footprint is the wrong thing to draw. It reports a suggested acreage
within a couple of percent of the eligible acreage while its outline is an
unbroken 5 m staircase hung with one-cell fingers -- ground nobody would
farm as drawn, presented as a recommendation. render_layout_map.py has
always clipped the PDF's production texture to the opening for this reason;
this endpoint shipping the footprint was the interactive map disagreeing
with the printed one.

EVERY ACREAGE MOVES WITH THE GEOMETRY. Drawing the opening while captioning
the footprint's acreage would be worse than either alone, so the per-zone
figure and both summary totals below all come from render_fill_area_acres.

WHAT IS DELIBERATELY NOT CHANGED. The ceiling trim in
production_area_ceiling.py still measures itself against FOOTPRINT acreage,
and narrative_data still reports footprint acreage to the report. Both are
left alone on purpose: the trim is an algorithm whose behaviour would change
if its input did, and the narrative is the PDF's contract, not this one.
The consequence is real and is reported rather than hidden -- see this
module's own note in build_production_zone_payload().

COORDINATE PRECISION
--------------------
Geometry arrives from rasterio's transform_geom at full float repr -- 14 to
15 decimal places, roughly a nanometre. Measured on the reference parcel, the
full payload is 61,845 B raw / 17,817 B gzipped at that precision and
37,914 B / 7,688 B at six decimal places: 39% and 57% off, for coordinates
that are still resolved to about 11 cm.

Six is not an arbitrary round number. The eligible union has already been
through one Douglas-Peucker pass at one DEM cell (exclusion_zones.
ELIGIBLE_UNION_SIMPLIFY_TOLERANCE_CELLS -- 5 m), and the exclusion layers are
exact 5 m cell footprints. A tolerance of 11 cm is more than an order of
magnitude finer than the smallest feature any of this geometry can express,
so the rounding discards no information the geometry actually carries.

Applied to COORDINATES ONLY, never to the payload wholesale: scores,
acreages and factors have already been rounded by the pipeline at their own
documented boundary (production_area_ceiling._round1), and a second blind
pass over every float in the tree would be a second, undocumented rounding
of numbers that are contractually FINAL.
"""

from typing import Optional

from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon

import dem_data
from canopy_height_data import get_canopy_height_for_boundary
from exclusion_zones import identify_exclusion_zones
from production_area_ceiling import identify_optimized_production_areas


# See this module's COORDINATE PRECISION note. ~11 cm at these latitudes,
# against geometry whose finest real feature is a 5 m DEM cell.
COORDINATE_PRECISION_DP = 6


class LayerFetchError(Exception):
    """
    A hard failure of one named upstream layer.

    `layer` is the STABLE identifier a consumer branches on and must never
    change; `label` is display prose that will be reworded. Same two-field
    split, for the same reason, as exclusion_zones._wire_layers() -- a
    frontend that keys on the label instead is broken by the first copy edit.

    Deliberately carries NO exception text. The underlying error is logged
    server-side; what crosses the wire is the layer's identity and nothing
    else, because a raw rasterio or STAC traceback in a user-facing panel
    tells the reader nothing they can act on.
    """

    def __init__(self, layer: str, label: str):
        super().__init__(f"{layer} layer unavailable")
        self.layer = layer
        self.label = label


def _round_geometry(geometry: Optional[dict]) -> Optional[dict]:
    """
    A GeoJSON geometry dict with every coordinate rounded to
    COORDINATE_PRECISION_DP. None passes straight through -- an absent layer
    stays absent.

    Handles tuples as well as lists: transform_geom emits coordinate PAIRS as
    tuples, and a walker that only recurses into lists silently returns them
    unrounded (measured: a tuple-blind version of this function saved 24
    bytes on a 61 KB payload instead of 24,000).
    """
    if geometry is None:
        return None

    def walk(node):
        if isinstance(node, float):
            return round(node, COORDINATE_PRECISION_DP)
        if isinstance(node, int):
            return node
        if isinstance(node, (list, tuple)):
            return [walk(item) for item in node]
        return node

    return {**geometry, "coordinates": walk(geometry["coordinates"])}


def _feature_patch_id(feature: dict) -> int:
    """The patch id behind a suggested-zone feature.

    production_suitability_to_geojson() mints the feature id as
    "production-area-<patch id>" and does not carry the bare integer in
    properties, so the id is read back off that string rather than assumed to
    exist as its own field."""
    return int(str(feature["id"]).rsplit("-", 1)[-1])


def _boundary_polygon_utm(boundary_coordinates, dem: dict) -> Polygon:
    """The same warp_transform-then-Polygon pattern every other module in
    this pipeline uses. Computed here so identify_exclusion_zones() is handed
    one rather than deriving its own identical copy."""
    xs, ys = warp_transform(
        "EPSG:4326",
        dem["crs"],
        [point[0] for point in boundary_coordinates],
        [point[1] for point in boundary_coordinates],
    )
    return Polygon(zip(xs, ys))


def build_production_zone_payload(
    boundary_coordinates: list[tuple[float, float]],
    dem: Optional[dict] = None,
    canopy_height: Optional[dict] = None,
) -> dict:
    """
    Returns:
        {
            'eligible_union':    GeoJSON MultiPolygon | None,
            'exclusion_layers':  [ {type, label, data_available,
                                    geometry_wgs84}, ... ]  -- five, in
                                 exclusion_zones.LAYER_ORDER,
            'suggested_zones':   GeoJSON FeatureCollection,
            'zones':             [ per-zone readout dicts, rank order ],
            'summary':           {total_acres, slope_passing_acres,
                                  eligible_acres, selected_acres,
                                  selected_pct_of_parcel},
            'scales':            how to read every score,
            'wire':              {cell_size_meters, crs, max_slope_pct,
                                  boundary_setback_meters, ...},
        }

    dem and canopy_height are optional pre-fetched overrides in the same
    pass-through family the pipeline modules already use -- supplied, no
    network fetch happens for that layer. They exist so this function can be
    exercised end to end against a synthetic grid with no network at all,
    which is the only way it can be tested in a sandboxed environment.

    Raises LayerFetchError for a hard failure of elevation or canopy. Soil
    and road failures do NOT raise -- see this module's docstring.

    'summary' is read from production's narrative_data.parcel block rather
    than from the top-level total_selected_acreage / percent_of_parcel /
    parcel_acres fields. Both describe the same four figures, but the
    narrative block's are already through _round1 at the pipeline's own
    single rounding boundary and are contractually FINAL, while
    parcel_acres is raw (13.234178531035083 on the reference parcel).
    Rounding it here would be a second rounding boundary for one field.
    """
    if dem is None:
        try:
            dem = dem_data.get_dem_for_boundary(boundary_coordinates)
        except Exception as exc:
            raise LayerFetchError("elevation", "elevation data") from exc

    boundary_polygon_utm = _boundary_polygon_utm(boundary_coordinates, dem)

    if canopy_height is None:
        try:
            canopy_height = get_canopy_height_for_boundary(boundary_coordinates, dem)
        except Exception as exc:
            raise LayerFetchError("canopy", "tree canopy height") from exc
        if canopy_height is None:
            # No HAG coverage at all for this boundary. A genuine no-data
            # outcome, and a hard failure by the same pipeline rule that
            # makes the canopy gate mandatory -- not something to degrade on.
            raise LayerFetchError("canopy", "tree canopy height")

    exclusion = identify_exclusion_zones(
        boundary_coordinates,
        dem=dem,
        boundary_polygon_utm=boundary_polygon_utm,
        canopy_height=canopy_height,
    )
    production = identify_optimized_production_areas(
        boundary_coordinates,
        dem=dem,
        canopy_height=canopy_height,
    )

    wire = exclusion["wire"]
    narrative = production["narrative_data"]
    parcel_acres = float(narrative["parcel"]["total_acres"])

    # The drawn shape and its acreage, per patch id. Both were computed by
    # cluster_and_gate() and are read here, never recomputed -- the opening is
    # a raster morphological operation and a second implementation of it in a
    # serialisation layer would be a second answer to the same question.
    drawn = {
        int(patch["id"]): {
            "geometry": patch["render_fill_geometry_wgs84"],
            "acres": patch["render_fill_area_acres"],
        }
        for patch in production["scored_patches"]
    }

    # A patch whose opening came back empty has no drawn shape at all (a
    # cluster thinner than the opening radius throughout). It is dropped from
    # BOTH the map and the list rather than from one of them: a zone in the
    # readout at 0.0 acres with nothing under it on the map is a contradiction
    # the reader has no way to resolve. Reported by the endpoint's caller as a
    # count, not silently absorbed.
    drawable = {pid for pid, d in drawn.items() if d["geometry"] is not None}

    features = []
    for feature in production["zones_geojson"]["features"]:
        patch_id = _feature_patch_id(feature)
        if patch_id not in drawable:
            continue
        features.append(
            {
                **feature,
                "geometry": _round_geometry(drawn[patch_id]["geometry"]),
                "properties": {
                    **feature["properties"],
                    "area_acres": drawn[patch_id]["acres"],
                },
            }
        )

    zones = [
        {
            **zone,
            "area_acres": drawn[int(zone["id"])]["acres"],
            "percent_of_parcel": (
                round(drawn[int(zone["id"])]["acres"] / parcel_acres * 100.0, 1)
                if parcel_acres > 0
                else None
            ),
        }
        for zone in narrative["patches"]
        if int(zone["id"]) in drawable
    ]

    # Both totals are SUMS of the per-zone figures above, not a separate
    # measurement of a separate geometry -- so the list adds up to the total
    # the reader is shown, which it would not if one came from the footprint.
    suggested_acres = round(sum(zone["area_acres"] for zone in zones), 1)

    return {
        "eligible_union": _round_geometry(exclusion["eligible_union_wgs84"]),
        "exclusion_layers": [
            {**layer, "geometry_wgs84": _round_geometry(layer["geometry_wgs84"])}
            for layer in wire["layers"]
        ],
        "suggested_zones": {**production["zones_geojson"], "features": features},
        "zones": zones,
        "summary": {
            **narrative["parcel"],
            "selected_acres": suggested_acres,
            "selected_pct_of_parcel": (
                round(suggested_acres / parcel_acres * 100.0, 1) if parcel_acres > 0 else None
            ),
        },
        "scales": narrative["scales"],
        "wire": {key: value for key, value in wire.items() if key != "layers"},
        "zones_without_drawn_shape": len(drawn) - len(drawable),
    }
