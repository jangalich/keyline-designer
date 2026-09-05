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

TWO CALLERS, ONE ASSEMBLER. The two are split:

  build_production_zone_payload()      fetch, then assemble  -- the
                                       /api/production-zones path
  assemble_production_zone_payload()   assemble only, pure   -- also the
                                       interactive session's landform step

The session path (step_orchestrator.build_landform_payload()) already holds
both upstream results -- the exclusion result came from the terrain warm-up
at session creation, the production result from the step's own generate --
so it needs the contract without the fetching. Splitting it out means the
endpoint and the session return the SAME payload by construction; two
assemblers would be two contracts that agree until the first edit, with a
working frontend on the far side of the difference.

THE TWO REPRESENTATIONS OF ONE ZONE SET, AND THE ID THAT JOINS THEM.
`zones` is tabular (rank, score, slope range, aspect) for the panel's list;
`suggested_zones.features` is GeoJSON for the map. Deliberately not
collapsed: neither consumer can use the other's shape. Every tabular row
therefore carries `feature_id` -- its own feature's wire id -- alongside the
bare integer `id`, so the panel joins on a value the payload gave it rather
than on one it rebuilt with a format string.

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

THE DISPLAY-ONLY SMOOTHED OUTLINE
---------------------------------
Every suggested-zone feature also carries
`properties.display_only_smoothed_outline`: the same opening its `geometry`
carries, run through display_outline.smoothed_display_outline() -- the function
render_layout_map.py uses for the PDF's contour clip.

WHY IT IS ON THE WIRE AT ALL. A production zone is a union of 5 m DEM cells, so
its outline is a right-angle staircase. The PDF has never shown that staircase;
the interactive map did, which made the two maps of one parcel disagree about
what one zone looks like. Computing it here rather than porting the smoother to
JS keeps ONE implementation of a geometric operation -- see display_outline.py
for why that rule is worth a wire field.

NOTHING MAY COMPUTE FROM IT, AND THE NAME SAYS SO. `geometry` is still the
shape; cautions, clamping, acreage, commit validation and every downstream
consumer read that and not this. A client that measured against the real
geometry while DRAWING the smoothed one could show a zone visually missing a
crossing it records, which is precisely the client/server disagreement the
crossing-grounds tests closed.

It is None for a patch with no drawn shape, matching `geometry`'s own
convention, and it is rounded like every other coordinate here.

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
from rasterio.warp import transform_geom
from shapely.geometry import Polygon, mapping

import dem_data
from canopy_height_data import get_canopy_height_for_boundary
from display_outline import DISPLAY_ONLY_OUTLINE_PROPERTY, smoothed_display_outline
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


# The two layers allowed to hard-fail this payload, each as the exact
# (type, label) pair the wire carries -- see WHAT A FAILURE IS ALLOWED TO SAY.
#
# NAMED CONSTANTS RATHER THAN LITERALS AT THE RAISE SITES because they are no
# longer read in one place. step_registry.py declares the same pairs as the
# landform step's failure layers, so a session-path generate that dies on
# canopy reports what /api/production-zones reports for the same failure. Two
# copies of "tree canopy height" would drift on the first copy edit, and the
# frontend's upstream-failure state branches on `type` -- so the drift would
# be silent on the side that matters.
LAYER_ELEVATION = ("elevation", "elevation data")
LAYER_CANOPY = ("canopy", "tree canopy height")


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
            'zones':             [ per-zone readout dicts, rank order, each
                                   carrying BOTH `id` (the patch's bare
                                   integer) and `feature_id` (the matching
                                   suggested_zones feature's wire id) ],
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

    THE ASSEMBLY ITSELF is assemble_production_zone_payload() below; this
    function is the fetch-then-assemble path /api/production-zones takes. The
    interactive session's landform generate (step_orchestrator.build_landform_
    payload()) reaches the same assembler with an exclusion result and a
    production result it already has, so the two paths return one shape by
    construction rather than by agreement.
    """
    if dem is None:
        try:
            dem = dem_data.get_dem_for_boundary(boundary_coordinates)
        except Exception as exc:
            raise LayerFetchError(*LAYER_ELEVATION) from exc

    boundary_polygon_utm = _boundary_polygon_utm(boundary_coordinates, dem)

    if canopy_height is None:
        try:
            canopy_height = get_canopy_height_for_boundary(boundary_coordinates, dem)
        except Exception as exc:
            raise LayerFetchError(*LAYER_CANOPY) from exc
        if canopy_height is None:
            # No HAG coverage at all for this boundary. A genuine no-data
            # outcome, and a hard failure by the same pipeline rule that
            # makes the canopy gate mandatory -- not something to degrade on.
            raise LayerFetchError(*LAYER_CANOPY)

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

    return assemble_production_zone_payload(exclusion, production)


def assemble_production_zone_payload(exclusion: dict, production: dict) -> dict:
    """
    THE PAYLOAD CONTRACT ITSELF, with the fetching lifted off it: an
    exclusion_zones.identify_exclusion_zones() result and a production_area_
    ceiling.identify_optimized_production_areas() result in, the wire payload
    out. Pure -- no network, no geospatial derivation, no ordering decisions;
    every one of those lives in the caller above (or, for the session path, in
    step_orchestrator.py, which assembles the same two results from a warmed
    session cache instead of from two fetches).

    SPLIT OUT SO THE TWO PATHS CANNOT DRIFT. /api/production-zones and the
    interactive session's landform generate must return the SAME shape -- a
    working frontend consumes it today and the session path is meant to slot
    in underneath that frontend unchanged. Two assemblers would be two
    contracts that happen to agree until the first edit; test_step_
    orchestrator.py asserts the session payload against THIS function's own
    output rather than against a hand-written expectation, which is only a
    meaningful assertion because there is one implementation to assert
    against.
    """
    wire = exclusion["wire"]
    narrative = production["narrative_data"]
    parcel_acres = float(narrative["parcel"]["total_acres"])

    # The drawn shape and its acreage, per patch id. Both were computed by
    # cluster_and_gate() and are read here, never recomputed -- the opening is
    # a raster morphological operation and a second implementation of it in a
    # serialisation layer would be a second answer to the same question.
    #
    # THE THIRD ENTRY IS NOT A THIRD GEOMETRY. `outline` is the DISPLAY-ONLY
    # smoothed rendering of the SAME opening -- computed here, inside the
    # generate, rather than lazily at a layers fetch, so a payload is a payload
    # by the time anything reads it. It is display_outline.py's own function,
    # the one render_layout_map.py calls for the PDF's contour clip, so the
    # interactive map and the printed map smooth by one implementation. Nothing
    # computes from it; see display_outline.DISPLAY_ONLY_OUTLINE_PROPERTY.
    drawn = {
        int(patch["id"]): {
            "geometry": patch["render_fill_geometry_wgs84"],
            "acres": patch["render_fill_area_acres"],
            "outline": _display_only_outline_wgs84(patch, wire),
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
                    # Through _round_geometry() like every other coordinate on
                    # this payload -- 11 cm, an order of magnitude finer than
                    # the 5 m cell the outline is a smoothing OF. A display
                    # field is the last thing that should ship at nanometre
                    # precision.
                    DISPLAY_ONLY_OUTLINE_PROPERTY: _round_geometry(
                        drawn[patch_id]["outline"]
                    ),
                },
            }
        )

    # THE WIRE FEATURE ID, CARRIED RATHER THAN REBUILT. `id` is the patch's
    # bare integer; `feature_id` is the identity the map's own features are
    # keyed by -- f"production-area-{id}", minted by production_suitability_
    # to_geojson() and written down once in wire_translation._PRODUCTION_
    # FEATURE_ID_PREFIX.
    #
    # The panel used to reconstruct it with a format string
    # (`production-area-${zone.id}`) while the map filtered on feature.id
    # directly: one identity with two sources of truth, joined by a template
    # literal that nothing checks. Renaming the prefix would have broken
    # selection silently -- the rows would simply stop matching, with no error
    # anywhere. Carrying the real value makes the join a lookup instead.
    #
    # `id` STAYS. The frontend still keys its list rows on the bare integer,
    # and this is an addition, not a migration.
    feature_ids = {
        _feature_patch_id(feature): feature["id"] for feature in features
    }

    zones = [
        {
            **zone,
            "feature_id": feature_ids[int(zone["id"])],
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


def _display_only_outline_wgs84(patch: dict, wire: dict) -> Optional[dict]:
    """
    One production patch's DISPLAY-ONLY smoothed outline, as a WGS84 GeoJSON
    geometry -- or None when the patch has no drawn shape at all.

    NOTHING MAY COMPUTE FROM THE RESULT. It is a rendering of
    render_fill_polygon_utm, not a second version of it; see
    display_outline.py, which owns both the rule and the smoothing.

    THE SAME SHAPE THE FEATURE'S OWN GEOMETRY IS. The wire geometry for a
    production zone is the opening (render_fill_geometry_wgs84), so the outline
    smooths render_fill_polygon_utm and re-clips to polygon_utm -- exactly what
    render_layout_map.py hands the same function for the PDF's contour clip.
    Smoothing anything else would ship an outline of a shape the map does not
    draw.

    THE CRS AND THE CELL SIZE COME OFF THE EXCLUSION WIRE BLOCK, which is
    already this payload's published answer to "what projection are these
    metres in" and "how big is a cell" -- the same two values a frontend
    computing an acreage from an intersection is told to use. Reading them here
    rather than re-deriving them from a DEM this function does not have is what
    keeps the endpoint path and the session path identical.

    EMPTY IS A REAL OUTCOME and returns None, matching
    render_fill_geometry_wgs84's own convention for a patch whose opening came
    back empty -- such a patch is dropped from the payload entirely, so this is
    belt-and-braces rather than a case a consumer sees.
    """
    render_fill_polygon_utm = patch["render_fill_polygon_utm"]
    if render_fill_polygon_utm.is_empty:
        return None
    outline_utm = smoothed_display_outline(
        render_fill_polygon_utm,
        patch["polygon_utm"],
        max(wire["cell_size_meters"]),
    )
    if outline_utm.is_empty:
        return None
    return transform_geom(wire["crs"], "EPSG:4326", mapping(outline_utm))
