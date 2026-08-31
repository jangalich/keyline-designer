"""
wire_translation.py

THE TRANSLATION BOUNDARY between the pipeline's internal step results and
the wire (interactive-design-architecture-proposal.md section 2.4). One
adapter layer, deliberately OUTSIDE the KSOP modules, sitting at the edge
between the session orchestrator and the frontend.

OUTBOUND: internal step result -> feature_schema.py GeoJSON
FeatureCollection, one function per layer the frontend displays or edits.

INBOUND: committed GeoJSON -> the internal per-feature dict shape the
downstream override parameters expect ("rehydration"), so a user-authored
feature travels down the same override params as a computer-authored one.
PRODUCTION ZONES ONLY so far -- tree zones and structure sites follow once
this pattern is proven. See the INBOUND section header near the bottom of
this file for the governing rule and what is derived versus inherited.

BOTH DIRECTIONS LIVE IN THIS ONE MODULE ON PURPOSE, and a later branch must
not split them. The proposal's reason: the modules stay agnostic of the
wire, the wire stays agnostic of shapely/numpy, and shape drift between
outbound and inbound has exactly ONE place it can be caught. An outbound
function that emits a field inbound cannot reconstruct is a bug you can see
by reading two adjacent functions in one file; split across two modules it
is invisible.

TWO CONSUMERS, NOT ONE. Every function here is written for both:

  1. The interactive session's per-layer endpoints (later branches) -- one
     layer at a time, on demand, for map display and editing.
  2. render_layout_map.fetch_layout_layers() (exists today) -- the batch
     layout-map path.

So each function takes ONE layer's already-computed internal value(s) and
nothing else: no PipelineContext, no ParcelData, no fetch, no orchestration.
A function that reached for the whole context could not serve consumer 1,
which hands out one layer at a time.

NEVER None, NEVER a raised error, on an empty input. Every function returns
a valid FeatureCollection -- an EMPTY one when the layer computed and found
nothing. That distinction is load-bearing: an empty FeatureCollection is how
"computed, nothing there" reaches the frontend, and it must be
distinguishable from a failure. `{"type": "FeatureCollection", "features":
[]}` says the step ran; a None or an exception says it did not.

NO REPROJECTION WHERE A WGS84 FORM ALREADY EXISTS. Most internal objects in
this pipeline already carry `geometry_wgs84`, built ONCE at the object's own
birth against the DEM's CRS. This module WRAPS that stored form in the
feature_schema envelope -- it does not rebuild it. Reprojecting a second
time is wasted work AND a genuine source of drift from the geometry
render_layout_map.py draws (transform_geom is not exactly idempotent across
a round trip). The three places that DO reproject here are the three where
no stored WGS84 form exists at all, each flagged at its own function:

  - exclusion_zones' per-gate footprints already ship WGS84 on the module's
    own `wire` block, so even those are wrapped, not rebuilt.
  - the parcel boundary is ALREADY lon/lat (it is the pipeline's own input),
    so it is ring-closed, never transformed.

  ...which leaves ZERO reprojections in this module. See the branch report's
  provenance table: every layer in the inventory carries a stored WGS84 form
  or is natively WGS84.

CONSOLIDATION, NOT REIMPLEMENTATION. The bodies below were MOVED here from
the KSOP modules that used to each carry their own *_to_geojson() helper
(valley_delineation, keypoint_detection, production_area,
production_suitability, water_survey_areas, road_corridors,
solar_suitability, tree_zone_candidates). Those modules now import from
here and keep their old public names bound as aliases, so every existing
caller, test and diagnostic keeps working and there is exactly one
implementation of each conversion. NOTHING about what any of them computes
changed -- these are wrapping steps, and a byte-identical output is the
whole point (see test_wire_translation.py's PARITY section).

IMPORT DIRECTION, AND WHY THE KSOP IMPORTS BELOW ARE FUNCTION-LOCAL. The
static dependency runs module -> boundary: road_corridors.py,
solar_suitability.py, water_survey_areas.py et al. import THIS module at
their top level to build the `zones_geojson` key their own return contracts
have always carried. This module needs a handful of values back from them
(a confidence-notes template, a threshold quoted in those notes, a property-
set builder), which would close an import cycle if taken at module level.
They are therefore imported INSIDE the functions that need them -- deferred
to call time, cached by Python after the first call, and cheap. This is the
one concession the boundary makes to the fact that a KSOP module still
publishes its own wire form; it is not a pattern to copy elsewhere.

CONFIDENCE. Every feature carries feature_schema's required confidence +
confidence_notes. Where a layer already had a *_to_geojson() helper, its
convention is preserved EXACTLY (per-feature confidence read off the object
for keypoints and water survey zones; CONFIDENCE_LOW plus a module-owned
notes template for production, roads, solar and tree zones;
CONFIDENCE_MEDIUM for valleys). The three layers that never had one -- the
parcel boundary and the two exclusion-zone layers -- follow the same
established shape (CONFIDENCE_LOW plus a real, specific caveat) rather than
inventing a fourth convention; their notes are declared in this module
because exclusion_zones.py has never been a wire producer and owns no
confidence vocabulary of its own.

COORDINATE ORDER is [lon, lat], WGS84 (EPSG:4326), everywhere -- inherited
from the stored geometry_wgs84 forms, which transform_geom already produces
in that order, and enforced on the one hand-built geometry (the boundary
ring) by construction.
"""

from typing import Any, Optional

from feature_schema import (
    CONFIDENCE_LOW,
    CONFIDENCE_MEDIUM,
    make_feature,
    make_feature_collection,
)

METERS_PER_FOOT = 0.3048


# ======================================================================
# Layer names -- the stable identifiers the frontend branches on
# ======================================================================
# Every name a *_to_geojson() helper already published is preserved
# VERBATIM. Renaming one would break the frontend and would break the
# parity guarantee this branch is built around. The three new names at the
# bottom are the layers that never had a helper.

LAYER_PARCEL_BOUNDARY = "parcel_boundary"
LAYER_VALLEY = "valley"
LAYER_KEYPOINT = "keypoint"
LAYER_PRODUCTION_AREA = "production_area_candidate"
LAYER_ROAD_CORRIDOR = "suggested_road_corridor"
LAYER_SOLAR = "solar_infrastructure"
LAYER_TREE_ZONE = "tree_zone_candidate"
LAYER_EXCLUSION_UNION = "exclusion_union"
LAYER_ELIGIBLE_GROUND = "eligible_ground"
LAYER_EXCLUSION_GATE = "exclusion_gate"
# Water survey layers are computed per zone ("survey_zone_<type>",
# "survey_zone_member_<type>", "survey_zone_dropped") -- see
# water_survey_zones_to_feature_collection(). The two ZONE layers are named
# here as well, because the water step's commit contract has to declare the
# layers a committed feature may carry and a contract built out of f-strings
# would be a second spelling of a name this module owns. The member and
# dropped layers are deliberately NOT named here: neither is committable
# (a member is a sub-feature of a zone; a dropped zone is below the floor
# and not selectable), so a constant for them would invite a contract to
# accept one.
LAYER_SURVEY_ZONE_EMBANKMENT = "survey_zone_embankment"
LAYER_SURVEY_ZONE_EXCAVATED = "survey_zone_excavated"
LAYER_SURVEY_ZONES = (LAYER_SURVEY_ZONE_EMBANKMENT, LAYER_SURVEY_ZONE_EXCAVATED)

# The wire id prefix water_survey_zones_to_feature_collection() mints for a
# zone. Written down once, here, for the same reason _PRODUCTION_FEATURE_ID_
# PREFIX is: outbound builds it and inbound parses it, and a second spelling
# in the commit path would be a second answer waiting to disagree.
_WATER_SURVEY_ZONE_FEATURE_ID_PREFIX = "water-survey-zone-"


PARCEL_BOUNDARY_CONFIDENCE_NOTES = (
    "The parcel boundary exactly as it was supplied to this pipeline -- the lon/lat ring every "
    "layer below it was clipped and measured against. It is an INPUT, not a measurement: this "
    "pipeline neither sources nor verifies it against a county parcel record, so any error in "
    "the drawn or imported boundary propagates into every acreage on this map."
)

EXCLUSION_UNION_CONFIDENCE_NOTES = (
    "Ground this pipeline will not site anything on: the union of five gates (existing canopy "
    "root zone, slope ceiling, hydric/floodplain soil, existing farm road right-of-way, boundary "
    "setback), each published as its exact DEM-cell footprint clipped to the parcel and unioned "
    "with no closing, smoothing or buffering applied. Cell-resolution geometry over public "
    "SSURGO soil, lidar canopy and DEM slope data, so the edge is a cell staircase rather than a "
    "surveyed line, and a gate whose source data was unreachable this run excludes nothing -- "
    "check each gate's own data_available flag on the exclusion_gate layer before reading empty "
    "as clear."
)

ELIGIBLE_GROUND_CONFIDENCE_NOTES = (
    "Selectable ground: gate-eligible DEM cells, 8-connected clustered, clusters under the "
    "minimum-cluster floor dropped, footprints unioned, clipped to the parcel and simplified by "
    "one Douglas-Peucker pass at one DEM cell. This is the DISPLAY AND CLAMPING geometry -- it "
    "is deliberately NOT the exact complement of the exclusion union (that complement keeps "
    "stranded single-cell specks and the exact cell staircase; this drops the specks and carries "
    "up to a one-cell simplify tolerance on its ring), so a drawn polygon constrained against it "
    "is constrained slightly conservatively, not exactly."
)

EXCLUSION_GATE_CONFIDENCE_NOTES = (
    "One exclusion gate's own exact DEM-cell footprint, clipped to the parcel, with nothing "
    "closed, smoothed or buffered -- the per-gate detail behind the exclusion union, so a "
    "drawing that crosses excluded ground can be captioned with WHICH gate it crossed. "
    "properties.data_available is NOT decoration: a gate that was never checked (an unreachable "
    "soil endpoint, no lidar coverage) and a gate that genuinely excludes nothing both produce "
    "no geometry, and they do not mean the same thing."
)


# ======================================================================
# OUTBOUND -- one function per layer
# ======================================================================
#
# Every function: takes one layer's internal value(s), returns a
# feature_schema FeatureCollection, tolerates None/empty by returning an
# EMPTY FeatureCollection, and never reprojects a geometry that already
# carries a stored WGS84 form.


def boundary_to_feature_collection(
    boundary_coordinates: Optional[list[tuple[float, float]]],
) -> dict:
    """
    The parcel boundary as a single Polygon Feature.

    boundary_coordinates is this pipeline's OWN INPUT and is already
    (lon, lat) WGS84 -- there is nothing to reproject and nothing to look
    up, so this only closes the ring (GeoJSON requires first == last;
    every internal consumer of boundary_coordinates works off the open
    ring, so closing here must not be pushed back upstream) and wraps it.
    Same treatment diagnose_water_survey_areas.py's own context-boundary
    feature already gives it.

    An empty or absent boundary yields an empty FeatureCollection rather
    than a degenerate polygon.
    """
    if not boundary_coordinates:
        return make_feature_collection([])

    ring = [[float(point[0]), float(point[1])] for point in boundary_coordinates]
    if ring[0] != ring[-1]:
        ring.append(list(ring[0]))
    if len(ring) < 4:
        # Fewer than three distinct corners is not a polygon. Report the
        # layer as computed-and-empty rather than emitting geometry that
        # would fail feature_schema's own validation downstream.
        return make_feature_collection([])

    return make_feature_collection(
        [
            make_feature(
                feature_id="parcel-boundary",
                geometry={"type": "Polygon", "coordinates": [ring]},
                layer=LAYER_PARCEL_BOUNDARY,
                label="Parcel boundary",
                confidence=CONFIDENCE_LOW,
                confidence_notes=PARCEL_BOUNDARY_CONFIDENCE_NOTES,
                extra_properties={"vertex_count": len(ring) - 1},
            )
        ]
    )


def valleys_to_feature_collection(valleys: Optional[list[dict]]) -> dict:
    """
    valley_delineation.delineate_valleys() output, one LineString Feature
    per valley (layer="valley").

    MOVED here verbatim from valley_delineation.valleys_to_geojson(), which
    is now an alias for this function. Geometry is each valley's stored
    geometry_wgs84 -- built at delineation time, not rebuilt here.
    """
    from valley_delineation import VALLEY_CONFIDENCE_NOTES

    features = [
        make_feature(
            feature_id=f"valley-{v['id']}",
            geometry=v["geometry_wgs84"],
            layer=LAYER_VALLEY,
            label=f"Valley {v['id']}",
            confidence=CONFIDENCE_MEDIUM,
            confidence_notes=VALLEY_CONFIDENCE_NOTES,
            extra_properties={
                "max_contributing_area_acres": v["max_contributing_area_acres"],
                "branch_count": len(v["branches_rowcol"]),
            },
        )
        for v in (valleys or [])
    ]
    return make_feature_collection(features)


def keypoints_to_feature_collection(keypoints: Optional[list[dict]]) -> dict:
    """
    keypoint_detection.detect_keypoints() output, one Point Feature per
    keypoint (layer="keypoint").

    MOVED here verbatim from keypoint_detection.keypoints_to_geojson(),
    which is now an alias for this function. Confidence and
    confidence_notes are read PER FEATURE off the keypoint dict -- that
    layer measures its own per-keypoint reliability, so a layer-wide
    constant would throw the measurement away.

    Note this deliberately does NOT emit 'feature_relationships' (the
    distance/elevation block pipeline_context._attach_keypoint_feature_
    relationships() attaches). That block is measured against OTHER
    layers' render_fill_polygon_utm and would go stale the moment a user
    edits one of those layers; it is a derived cross-layer read, not this
    layer's own geometry, and the original helper never emitted it.
    """
    features = [
        make_feature(
            feature_id=f"keypoint-{k['id']}",
            geometry=k["geometry_wgs84"],
            layer=LAYER_KEYPOINT,
            label=f"Keypoint {k['id']}",
            confidence=k["confidence"],
            confidence_notes=k["confidence_notes"],
            extra_properties={
                "valley_id": k["valley_id"],
                "elevation_m": k["elevation_m"],
                "contributing_acres": k["contributing_acres"],
                "slope_above_pct": k["slope_above_pct"],
                "slope_below_pct": k["slope_below_pct"],
                "slope_drop_pct": k["slope_drop_pct"],
                "stem_length_cells": k["stem_length_cells"],
                "position_along_stem": k["position_along_stem"],
                "on_parcel": k["on_parcel"],
                "distance_outside_boundary_m": k["distance_outside_boundary_m"],
            },
        )
        for k in (keypoints or [])
    ]
    return make_feature_collection(features)


def exclusion_union_to_feature_collection(exclusion_result: Optional[dict]) -> dict:
    """
    The parcel's unselectable ground as ONE Feature (layer=
    "exclusion_union") -- exclusion_zones.identify_exclusion_zones()'s
    'geometry_wgs84', which is excluded_union_utm already reprojected at
    that call's own return. Not rebuilt from excluded_union_utm here.

    That module publishes 'render_fill_polygon_utm' as the SAME geometry
    as excluded_union_utm, deliberately (there is no display-only
    reduction to apply to an exact cell footprint), so the geometry drawn
    and the geometry on the wire are the same object -- unlike production
    areas, where they are not. See the branch report's provenance table.

    'geometry_wgs84' is None when the union is empty (nothing excluded on
    this parcel), which is a real "computed, nothing there" answer and
    yields an empty FeatureCollection, never an error.
    """
    if not exclusion_result:
        return make_feature_collection([])

    geometry = exclusion_result.get("geometry_wgs84")
    if geometry is None:
        return make_feature_collection([])

    narrative = exclusion_result.get("narrative_data") or {}
    parcel_block = narrative.get("parcel") or {}

    return make_feature_collection(
        [
            make_feature(
                feature_id="exclusion-union",
                geometry=geometry,
                layer=LAYER_EXCLUSION_UNION,
                label="Excluded ground (all gates)",
                confidence=CONFIDENCE_LOW,
                confidence_notes=EXCLUSION_UNION_CONFIDENCE_NOTES,
                extra_properties={
                    "parcel_acres": exclusion_result.get("parcel_acres"),
                    "excluded_acres": parcel_block.get("excluded_acres"),
                    "excluded_pct_of_parcel": parcel_block.get("excluded_pct_of_parcel"),
                    "gate_types": [
                        layer["type"] for layer in _wire_block_layers(exclusion_result)
                    ],
                },
            )
        ]
    )


def eligible_ground_to_feature_collection(exclusion_result: Optional[dict]) -> dict:
    """
    The DERIVED eligible geometry as ONE Feature (layer="eligible_ground")
    -- exclusion_zones' 'eligible_union_wgs84', the display-and-clamping
    highlight, already reprojected at that call's own return.

    A SEPARATE LAYER FROM THE UNION ABOVE, not its inverse. Three things in
    that module are named "eligible" and only this one is the clamping
    geometry: 'eligible_polygon_utm' is the exact geometric complement
    (specks and cell staircase intact) and 'eligible_mask' is the cell
    grid. Emitting the complement instead would highlight confetti a user
    cannot plant; emitting both would ship two layers a frontend cannot
    tell apart. See ELIGIBLE_GROUND_CONFIDENCE_NOTES for what the
    difference costs a consumer that clamps against this.

    None when nothing is eligible -- an empty FeatureCollection, never an
    error.
    """
    if not exclusion_result:
        return make_feature_collection([])

    geometry = exclusion_result.get("eligible_union_wgs84")
    if geometry is None:
        return make_feature_collection([])

    narrative = exclusion_result.get("narrative_data") or {}
    parcel_block = narrative.get("parcel") or {}
    wire = exclusion_result.get("wire") or {}

    return make_feature_collection(
        [
            make_feature(
                feature_id="eligible-ground",
                geometry=geometry,
                layer=LAYER_ELIGIBLE_GROUND,
                label="Selectable ground",
                confidence=CONFIDENCE_LOW,
                confidence_notes=ELIGIBLE_GROUND_CONFIDENCE_NOTES,
                extra_properties={
                    "parcel_acres": exclusion_result.get("parcel_acres"),
                    "eligible_acres": parcel_block.get("eligible_acres"),
                    # BOTH DEM cell dimensions, in metres -- resolution is
                    # not square (the reference DEMs are 4.99 x 5.00 and
                    # 5.00 x 4.99), and a frontend computing acreage from
                    # an intersection needs both. Carried straight off the
                    # module's own wire block.
                    "cell_size_meters": wire.get("cell_size_meters"),
                },
            )
        ]
    )


def exclusion_gate_layers_to_feature_collection(exclusion_result: Optional[dict]) -> dict:
    """
    One Feature per exclusion gate, in the module's own LAYER_ORDER
    (layer="exclusion_gate") -- the per-gate detail that lets a drawing
    which crosses excluded ground be captioned with WHICH gate it crossed.

    Geometry comes straight off exclusion_zones' own `wire` block, where
    each gate's footprint was ALREADY reprojected to WGS84 once at that
    call's return. Nothing is reprojected here.

    A gate whose geometry is None is SKIPPED as a feature but its
    data_available flag still rides properties.gate_availability on every
    emitted feature, so a consumer can tell "this gate excluded nothing"
    from "this gate was never checked" without a second request. Both the
    stable `type` identifier and the display `label` are carried, exactly
    as the wire block splits them.
    """
    wire_layers = _wire_block_layers(exclusion_result)
    if not wire_layers:
        return make_feature_collection([])

    availability = {layer["type"]: bool(layer["data_available"]) for layer in wire_layers}

    features = []
    for layer in wire_layers:
        if layer.get("geometry_wgs84") is None:
            continue
        features.append(
            make_feature(
                feature_id=f"exclusion-gate-{layer['type']}",
                geometry=layer["geometry_wgs84"],
                layer=LAYER_EXCLUSION_GATE,
                label=layer["label"],
                confidence=CONFIDENCE_LOW,
                confidence_notes=EXCLUSION_GATE_CONFIDENCE_NOTES,
                extra_properties={
                    "gate_type": layer["type"],
                    "data_available": bool(layer["data_available"]),
                    "gate_availability": availability,
                },
            )
        )
    return make_feature_collection(features)


def _wire_block_layers(exclusion_result: Optional[dict]) -> list[dict]:
    """exclusion_zones' own wire['layers'] list, or [] when absent."""
    if not exclusion_result:
        return []
    wire = exclusion_result.get("wire") or {}
    return list(wire.get("layers") or [])


def production_areas_to_feature_collection(patches: Optional[list[dict]]) -> dict:
    """
    production_area.identify_production_areas() output -- the RAW,
    un-trimmed, UNSCORED patches (layer="production_area_candidate").

    MOVED here verbatim from production_area.production_areas_to_geojson(),
    which is now an alias for this function.

    THIS IS NOT THE PIPELINE'S PRODUCTION LAYER. PipelineContext.
    production_areas holds production_area_ceiling's SCORED patches; use
    scored_production_areas_to_feature_collection() for those. This one
    stays because it is a real diagnostic entry point (checking the slope
    heuristic against a known property, independent of the ceiling and the
    scoring built on top of it), and because dropping it would delete a
    conversion this branch was asked to consolidate, not remove.

    Geometry is each patch's stored geometry_wgs84 -- which is
    polygon_utm reprojected, NOT render_fill_polygon_utm. The map draws
    render_fill. See the branch report's provenance table; that split is
    the single most important thing on it.
    """
    from production_area import PRODUCTION_AREA_CONFIDENCE_NOTES

    features = [
        make_feature(
            feature_id=f"production-area-{p['id']}",
            geometry=p["geometry_wgs84"],
            layer=LAYER_PRODUCTION_AREA,
            label=f"Production area candidate {p['id']}",
            confidence=CONFIDENCE_LOW,
            confidence_notes=PRODUCTION_AREA_CONFIDENCE_NOTES,
            extra_properties={
                "area_acres": p["area_acres"],
                "representative_elevation_m": round(p["representative_elevation_m"], 1),
                # Acreage of the geometry the MAP actually draws (the bounded
                # morphological opening render_layout_map.py clips production
                # contour texture to), NOT the full cell-union footprint
                # area_acres reports. render_fill_polygon_utm is always a
                # subset of polygon_utm, so this is <= area_acres for every
                # patch (see cluster_and_gate()'s containment assertion).
                "render_fill_area_acres": p["render_fill_area_acres"],
            },
        )
        for p in (patches or [])
    ]
    return make_feature_collection(features)


def scored_production_areas_to_feature_collection(
    scored_patches: Optional[list[dict]],
) -> dict:
    """
    production_suitability.score_production_areas() output -- the SCORED
    patches, which is what PipelineContext.production_areas holds
    (ceiling-trimmed, STEP-4-scored). Same layer name as the unscored
    diagnostic above ("production_area_candidate"), by that helper's own
    established choice.

    MOVED here verbatim from production_suitability.
    production_suitability_to_geojson(), which is now an alias for this
    function.

    Geometry is each patch's stored geometry_wgs84 (polygon_utm
    reprojected at cluster_and_gate() time). confidence_notes is read PER
    PATCH -- scoring attaches its own per-patch caveat, so a layer-wide
    constant would drop it.
    """
    features = []
    for patch in (scored_patches or []):
        confidence_notes = patch["confidence_notes"]

        label = f"Production area candidate {patch['id']} (suitability rank {patch['rank']})"

        features.append(
            make_feature(
                feature_id=f"production-area-{patch['id']}",
                geometry=patch["geometry_wgs84"],
                layer=LAYER_PRODUCTION_AREA,
                label=label,
                confidence=CONFIDENCE_LOW,
                confidence_notes=confidence_notes,
                extra_properties={
                    "area_acres": patch["area_acres"],
                    "representative_elevation_m": round(patch["representative_elevation_m"], 1),
                    "rank": patch["rank"],
                    "suitability_score": patch["suitability_score"],
                    "slope_factor": patch["slope_factor"],
                    "size_factor": patch["size_factor"],
                    "aspect_factor": patch["aspect_factor"],
                    "avg_slope_pct": patch["avg_slope_pct"],
                    "aspect_deg": patch["aspect_deg"],
                    "soil_carved_acres": patch["soil_carved_acres"],
                    "soil_carved_pct": patch["soil_carved_pct"],
                    "soil_data_available": patch["soil_data_available"],
                    "source_patch_id": patch["source_patch_id"],
                },
            )
        )
    return make_feature_collection(features)


def water_survey_zones_to_feature_collection(
    zones: Optional[list[dict]],
    dropped_zones: Optional[list[dict]] = None,
) -> dict:
    """
    water_survey_areas.identify_water_survey_areas()'s survey zones: every
    surviving zone envelope on survey_zone_<type> plus every member-region
    footprint on survey_zone_member_<type>, and -- when dropped_zones is
    supplied (the diagnostic export path only) -- every floor-dropped zone
    on survey_zone_dropped.

    MOVED here verbatim from water_survey_areas.survey_areas_to_geojson(),
    which is now an alias for this function.

    STORED WIRE FORMS ONLY. Every geometry is the object's own
    geometry_wgs84, built at its birth. For a survey zone that WGS84 form
    is the clipped closing envelope, which is ALSO its
    render_fill_polygon_utm -- so unlike production, the geometry on the
    wire and the geometry the map draws are the same. Confidence and (for
    zones) confidence_notes are per-object.

    Note that PipelineContext.water_zones is ALREADY this function's own
    features list; a caller holding that should use
    water_zone_features_to_feature_collection() rather than re-running
    this over the raw zones, which would be a second conversion of the
    same objects.
    """
    from water_survey_areas import (
        MIN_SURVEY_REGION_AREA_ACRES,
        _DROPPED_ZONE_NOTE,
        _MEMBER_FEATURE_NOTE,
        _member_feature_properties,
        _zone_feature_properties,
    )

    features = []
    for zone in (zones or []):
        features.append(
            make_feature(
                feature_id=f"water-survey-zone-{zone['id']}",
                geometry=zone["geometry_wgs84"],
                layer=f"survey_zone_{zone['survey_type']}",
                label=(
                    f"Survey zone {zone['id']} ({zone['survey_type']}-type, rank {zone['rank']}): "
                    f"{zone['zone_acres']} ac to survey, anchored by {zone['member_acres']} ac of "
                    f"high-suitability ground ({zone['member_count']} member(s))"
                ),
                confidence=zone["confidence"],
                confidence_notes=zone["confidence_notes"],
                extra_properties=_zone_feature_properties(zone),
            )
        )
        for member in zone["members"]:
            features.append(
                make_feature(
                    feature_id=f"water-survey-zone-member-{member['id']}",
                    geometry=member["geometry_wgs84"],
                    layer=f"survey_zone_member_{member['survey_type']}",
                    label=(
                        f"Member region {member['id']} of survey zone {zone['id']} "
                        f"({member['area_acres']} ac)"
                    ),
                    confidence=member["confidence"],
                    confidence_notes=_MEMBER_FEATURE_NOTE,
                    extra_properties=_member_feature_properties(member),
                )
            )
    for zone in dropped_zones or []:
        features.append(
            make_feature(
                feature_id=f"water-survey-zone-dropped-{zone['id']}",
                geometry=zone["geometry_wgs84"],
                layer="survey_zone_dropped",
                label=(
                    f"DROPPED survey zone {zone['id']} ({zone['survey_type']}-type): member ground "
                    f"{zone['member_acres']} ac under the {MIN_SURVEY_REGION_AREA_ACRES} ac floor"
                ),
                confidence=zone["confidence"],
                confidence_notes=_DROPPED_ZONE_NOTE,
                extra_properties=_zone_feature_properties(zone),
            )
        )
    return make_feature_collection(features)


def water_zone_features_to_feature_collection(
    water_zone_features: Optional[list[dict]],
) -> dict:
    """
    PipelineContext.water_zones -> a FeatureCollection.

    THIS IS A WRAP, NOT A CONVERSION, AND THAT IS THE POINT.
    pipeline_context.py's water_zones field IS identify_water_survey_
    areas()'s own zones_geojson['features'] list -- already
    schema-conformant Features whose WGS84 geometry was built once at each
    object's birth. There is nothing left to translate: re-deriving these
    from the raw zone dicts would be a second conversion of the same
    objects and a place for the two to drift.

    The list is COPIED (shallow) so a caller mutating the returned
    collection's `features` cannot reach back into the context's own
    field; the Feature dicts themselves are shared, same as every other
    function here.
    """
    return make_feature_collection(list(water_zone_features or []))


def selected_water_zone_to_feature_collection(
    selected_water_zone: Optional[dict],
) -> dict:
    """
    The rank-1 survey zone as its own layer -- a SEPARATE layer from
    water_zones, because the frontend draws the selection differently from
    the candidate set and needs to fetch or refresh one without the other.

    Runs the SAME builder water_survey_zones_to_feature_collection() uses,
    over a one-zone list, so the selected zone's Feature is byte-identical
    to its entry in the candidate collection (same id, same layer name,
    same properties) -- which is what lets a frontend match the two up.

    None ("no zone cleared the suitability threshold" -- a real, computed
    answer, see pipeline_context.py's own None contract for this field)
    yields an empty FeatureCollection.
    """
    if selected_water_zone is None:
        return make_feature_collection([])
    return water_survey_zones_to_feature_collection([selected_water_zone])


def road_network_to_feature_collection(
    road_network: Optional[dict],
    floodplain_data_is_fallback: bool = False,
) -> dict:
    """
    road_corridors.build_road_network() output -- ONE LineString Feature
    PER BRANCH, trunk and spur alike, never one feature for the whole
    network (layer="suggested_road_corridor").

    MOVED here verbatim from road_corridors.corridors_to_geojson(), which
    is now an alias for this function.

    Geometry is each branch's stored geometry_wgs84, built at branch
    construction from its own points_xyz. The network dict is NEVER None
    in this pipeline -- an unreachable anchor or no demand produces the
    same shape with branches=[] (see _empty_road_network()) -- and that
    empty network correctly yields an empty FeatureCollection here. A None
    is tolerated anyway for the interactive path, where a step may not
    have run yet.

    floodplain_data_is_fallback is NOT decoration and NOT derivable from
    the network: it says the hydric/floodplain union those branches were
    routed around came from the DEM-only fallback rather than real
    NHD/SSURGO data, and it changes every branch's confidence_notes. A
    caller that has it (PipelineContext.soil_exclusion_unions
    ['hydric_floodplain_is_fallback']) must pass it; the default False
    matches the original helper's default exactly.
    """
    from road_corridors import _confidence_notes_for_route

    if not road_network:
        return make_feature_collection([])

    features = []
    for branch in road_network["branches"]:
        features.append(
            make_feature(
                feature_id=f"road-corridor-{branch['branch_index'] + 1}",
                geometry=branch["geometry_wgs84"],
                layer=LAYER_ROAD_CORRIDOR,
                label="Suggested road corridor",
                confidence=CONFIDENCE_LOW,
                confidence_notes=_confidence_notes_for_route(
                    floodplain_data_is_fallback,
                    branch["avg_grade_pct"],
                    branch["crosses_floodplain"],
                    branch["crosses_production_zone"],
                ),
                extra_properties={
                    "branch_index": branch["branch_index"],
                    "branch_role": branch["branch_role"],
                    "joins_branch_index": branch["joins_branch_index"],
                    "length_ft": round(branch["length_meters"] / METERS_PER_FOOT, 1),
                    "avg_grade_pct": round(branch["avg_grade_pct"], 1),
                    # Cell-level steep-section metrics (see _cell_steep_stats()),
                    # deliberately distinct from the centerline avg_grade_pct
                    # above: max_grade_pct is the steepest single cell, steep_ft
                    # the length of cells above the steep threshold.
                    "max_grade_pct": round(branch["max_grade_pct"], 1),
                    "steep_ft": round(branch["steep_meters"] / METERS_PER_FOOT, 1),
                    "newly_served_acres": round(branch["newly_served_acres"], 3),
                    "crosses_floodplain": branch["crosses_floodplain"],
                    "crosses_production_zone": branch["crosses_production_zone"],
                    # Network-level fields, identical across every feature
                    # in this FeatureCollection -- a caller narrating the
                    # whole network doesn't have to sum branch lengths
                    # back up itself.
                    "total_length_ft": round(road_network["total_length_meters"] / METERS_PER_FOOT, 1),
                    "total_served_acres": round(road_network["total_served_acres"], 3),
                    "stop_reason": road_network["stop_reason"],
                    # Grade is a genuine HARD ceiling now (impassable_
                    # grade_pct, see MAX_ROAD_GRADE_PCT), so unlike before
                    # this branch it's a real guarantee every branch
                    # satisfies, not merely an unbounded soft penalty.
                    # Production is NOT listed here -- it's a soft,
                    # proportionally-costlier traversal term a branch may
                    # or may not have crossed (see crosses_production_zone
                    # above), so claiming it "satisfied" would be
                    # misleading. Floodplain stays excluded for the same
                    # reason.
                    "constraints_satisfied": ["outside_pond_zone", "grade_within_max"],
                },
            )
        )

    return make_feature_collection(features)


def structure_sites_to_feature_collection(
    candidates: Optional[list[dict]],
    shading_is_rough_proxy: bool = True,
    road_proximity_source: str = "unavailable",
    tree_zone_exclusion_available: bool = True,
    spacing_meters: Optional[float] = None,
    max_structure_footprint_acres: Optional[float] = None,
) -> dict:
    """
    solar_suitability.find_candidate_solar_zones() (+ optionally
    flag_prime_farmland_conflicts()) output, one Feature per candidate
    (layer="solar_infrastructure").

    MOVED here verbatim from solar_suitability.candidates_to_geojson(),
    which is now an alias for this function. The two default-None
    parameters take solar_suitability's own module defaults at call time
    (they cannot be evaluated in this signature without a module-level
    import that would close an import cycle -- see the module docstring).

    FOUR RUN-LEVEL FLAGS ARE INPUTS, NOT DERIVABLE FROM A CANDIDATE.
    shading_is_rough_proxy, road_proximity_source,
    tree_zone_exclusion_available, and the two threshold parameters all
    feed the SHARED confidence_notes string every candidate carries. None
    of them lives on a candidate dict, on PipelineContext, or on
    identify_solar_candidate_zones()'s return dict -- they exist only
    inside that function's own local scope and only reach the wire baked
    into the notes. A caller that holds a candidate but not those flags
    CANNOT reproduce this layer's confidence_notes; it gets the defaults,
    which are a different (and possibly wrong) statement about the run.
    This is a real gap in what the pipeline carries forward, flagged in
    the branch report rather than papered over here.
    """
    from solar_suitability import (
        CANDIDATE_POINT_SPACING_METERS,
        MAX_SOLAR_SLOPE_PCT,
        MAX_STRUCTURE_FOOTPRINT_ACRES,
        MIN_SUITABILITY_SCORE,
        ROAD_PROXIMITY_NOTE_BY_SOURCE,
        SHADING_CAVEAT_HORIZON_ONLY,
        SOLAR_CONFIDENCE_NOTES_TEMPLATE,
        TREE_ROOT_ZONE_BUFFER_METERS,
        TREE_ZONE_EXCLUSION_UNAVAILABLE_NOTE,
        TREE_ZONE_STRUCTURE_EXCLUSION_BUFFER_METERS,
        _footprint_side_meters,
    )

    candidates = candidates or []
    if spacing_meters is None:
        spacing_meters = CANDIDATE_POINT_SPACING_METERS
    if max_structure_footprint_acres is None:
        max_structure_footprint_acres = MAX_STRUCTURE_FOOTPRINT_ACRES

    farmland_note = ""
    if candidates and "prime_farmland_conflict" in candidates[0]:
        farmland_note = (
            "SSURGO prime-farmland overlap was checked and is reported per-candidate "
            "in properties.prime_farmland_conflict — see properties.prime_farmland_note. "
        )

    confidence_notes = SOLAR_CONFIDENCE_NOTES_TEMPLATE.format(
        spacing_m=spacing_meters,
        max_footprint_acres=max_structure_footprint_acres,
        footprint_side_m=_footprint_side_meters(max_structure_footprint_acres),
        shading_caveat=SHADING_CAVEAT_HORIZON_ONLY if shading_is_rough_proxy else "computed from a real canopy height model (DSM-derived), not a rough proxy.",
        canopy_buffer_ft=TREE_ROOT_ZONE_BUFFER_METERS / METERS_PER_FOOT,
        tree_zone_buffer_ft=TREE_ZONE_STRUCTURE_EXCLUSION_BUFFER_METERS / METERS_PER_FOOT,
        tree_zone_availability_note="" if tree_zone_exclusion_available else TREE_ZONE_EXCLUSION_UNAVAILABLE_NOTE,
        road_proximity_note=ROAD_PROXIMITY_NOTE_BY_SOURCE[road_proximity_source],
        farmland_note=farmland_note,
    )

    features = []
    for candidate in candidates:
        constraints_satisfied = [
            "outside_water_candidate_zone",
            "outside_existing_canopy",
            f"max_slope<={MAX_SOLAR_SLOPE_PCT:.0f}pct",
            f"suitability_score>={MIN_SUITABILITY_SCORE * 100:.0f}",
        ]
        if tree_zone_exclusion_available:
            constraints_satisfied.append("outside_tree_zone_candidate_buffer")
        if candidate.get("distance_to_road_m") is not None:
            constraints_satisfied.append("within_road_proximity_buffer")

        distance_to_road_ft = (
            round(candidate["distance_to_road_m"] / METERS_PER_FOOT, 1)
            if candidate.get("distance_to_road_m") is not None
            else None
        )
        distance_to_production_zone_ft = (
            round(candidate["distance_to_production_zone_m"] / METERS_PER_FOOT, 1)
            if candidate.get("distance_to_production_zone_m") is not None
            else None
        )
        distance_to_water_zone_ft = (
            round(candidate["distance_to_water_zone_m"] / METERS_PER_FOOT, 1)
            if candidate.get("distance_to_water_zone_m") is not None
            else None
        )

        extra_properties = {
            "rank": candidate["rank"],
            "suitability_score": candidate["suitability_score"],
            "avg_slope_pct": candidate["avg_slope_pct"],
            "aspect": candidate["aspect_label"],
            "aspect_degrees": candidate["aspect_deg"],
            "footprint_area_acres": candidate["footprint_area_acres"],
            "distance_to_road_ft": distance_to_road_ft,
            "road_proximity_source": road_proximity_source,
            "distance_to_production_zone_ft": distance_to_production_zone_ft,
            "production_zone_relationship": candidate["production_zone_relationship"],
            "distance_to_water_zone_ft": distance_to_water_zone_ft,
            "constraints_satisfied": constraints_satisfied,
        }
        if "prime_farmland_conflict" in candidate:
            extra_properties["prime_farmland_conflict"] = candidate["prime_farmland_conflict"]
            extra_properties["prime_farmland_note"] = candidate["prime_farmland_note"]

        # Confidence reflects geometric/data-quality reliability (this
        # layer stacks a slope-only production heuristic, a DEM-only
        # shading proxy, and public-only road data), NOT site
        # desirability — a prime-farmland conflict, or sitting inside a
        # production zone, doesn't make the geometry itself any less
        # trustworthy, so neither is folded into confidence.
        features.append(
            make_feature(
                feature_id=f"solar-candidate-{candidate['rank']}",
                geometry=candidate["geometry_wgs84"],
                layer=LAYER_SOLAR,
                label=f"Solar structure candidate (rank {candidate['rank']})",
                confidence=CONFIDENCE_LOW,
                confidence_notes=confidence_notes,
                extra_properties=extra_properties,
            )
        )

    return make_feature_collection(features)


def selected_structure_site_to_feature_collection(
    selected_structure_site: Optional[dict],
    **run_flags: Any,
) -> dict:
    """
    PipelineContext.selected_structure_site (the rank-1 scored candidate,
    or None when nothing cleared the constraint stack) as its own layer.

    Runs the SAME builder structure_sites_to_feature_collection() uses,
    over a one-candidate list, so the emitted Feature matches that
    candidate's entry in the full candidate collection -- PROVIDED the
    same run flags are passed. **run_flags forwards them.

    THE CAVEAT THAT MATTERS, AND WHY THIS FUNCTION EXISTS RATHER THAN
    BEING WIRED INTO fetch_layout_layers(): the run flags are not carried
    on the site dict, on PipelineContext, or on identify_solar_candidate_
    zones()'s return dict. Called without them, this emits a
    geometrically-correct Feature whose confidence_notes describe the
    DEFAULT run, not the one that produced this site. render_layout_map.
    fetch_layout_layers() therefore still reads its structure_site Feature
    off identify_solar_candidate_zones()'s own zones_geojson, which is the
    only place those flags survive -- see that function's own comment.
    """
    if selected_structure_site is None:
        return make_feature_collection([])
    return structure_sites_to_feature_collection([selected_structure_site], **run_flags)


def tree_zones_to_feature_collection(patches: Optional[list[dict]]) -> dict:
    """
    tree_zone_candidates.score_tree_search_space() output -- the full
    ranked patch list PipelineContext.tree_zone_patches holds, one Feature
    per patch (layer="tree_zone_candidate").

    MOVED here verbatim from tree_zone_candidates.tree_zones_to_geojson(),
    which is now an alias for this function.

    Geometry is each patch's stored geometry_wgs84. For a tree patch,
    polygon_utm and render_fill_polygon_utm are the SAME object (that
    module records the real cell-union footprint under both names,
    deliberately), so the geometry on the wire is the geometry the map
    draws -- unlike production.

    Unlike water/road/solar there is no single "selected" tree zone (a
    farm can legitimately carry several at once), so there is no
    selected_* companion to this function.
    """
    from tree_zone_candidates import (
        HYDRIC_OVERLAP_FACTOR_WEIGHT,
        SLOPE_FACTOR_WEIGHT,
        SOIL_MARGINALITY_FACTOR_WEIGHT,
        STREAM_PROXIMITY_FACTOR_WEIGHT,
        TREE_ZONE_BOUNDARY_SETBACK_METERS,
        TREE_ZONE_CONFIDENCE_NOTES_TEMPLATE,
        TREE_ZONE_PRODUCTION_BUFFER_METERS,
        TREE_ZONE_WATER_BUFFER_METERS,
        _data_availability_note,
    )

    features = []
    for patch in (patches or []):
        confidence_notes = TREE_ZONE_CONFIDENCE_NOTES_TEMPLATE.format(
            hydric_weight=HYDRIC_OVERLAP_FACTOR_WEIGHT,
            slope_weight=SLOPE_FACTOR_WEIGHT,
            soil_weight=SOIL_MARGINALITY_FACTOR_WEIGHT,
            stream_weight=STREAM_PROXIMITY_FACTOR_WEIGHT,
            production_buffer_meters=TREE_ZONE_PRODUCTION_BUFFER_METERS,
            water_buffer_meters=TREE_ZONE_WATER_BUFFER_METERS,
            boundary_setback_meters=TREE_ZONE_BOUNDARY_SETBACK_METERS,
            data_availability_note=_data_availability_note(
                patch["soil_marginality_data_available"], patch["hydric_data_available"], patch["stream_data_available"]
            ),
        )

        features.append(
            make_feature(
                feature_id=f"tree-zone-candidate-{patch['id']}",
                geometry=patch["geometry_wgs84"],
                layer=LAYER_TREE_ZONE,
                label=f"Tree zone candidate {patch['id']} (rank {patch['rank']})",
                confidence=CONFIDENCE_LOW,
                confidence_notes=confidence_notes,
                extra_properties={
                    "area_acres": patch["area_acres"],
                    "tree_suitability_score": patch["tree_suitability_score"],
                    "soil_marginality_factor": patch["soil_marginality_factor"],
                    "slope_factor": patch["slope_factor"],
                    "hydric_overlap_factor": patch["hydric_overlap_factor"],
                    "stream_proximity_factor": patch["stream_proximity_factor"],
                    "avg_slope_pct": patch["avg_slope_pct"],
                    "rank": patch["rank"],
                    "soil_marginality_data_available": patch["soil_marginality_data_available"],
                    "hydric_data_available": patch["hydric_data_available"],
                    "stream_data_available": patch["stream_data_available"],
                },
            )
        )

    return make_feature_collection(features)


# ======================================================================
# INBOUND -- committed GeoJSON -> the internal per-feature dict shape
# ======================================================================
#
# "Rehydration" (proposal section 2.4). A user-drawn or user-adjusted
# feature has to travel down the SAME override parameters a computer-
# authored one does -- `production_areas=` on water_survey_areas.
# identify_water_survey_areas(), road_corridors.build_road_network(),
# solar_suitability.find_candidate_solar_zones(), tree_zone_candidates.
# identify_tree_zone_candidates(). That is the whole trick, and the only
# way it works is if what comes back off the wire is turned into a dict
# indistinguishable, to those consumers, from one cluster_and_gate() built.
#
# THE GOVERNING RULE: REHYDRATION RE-DERIVES FORWARD FROM AN EDITED
# SOURCE. IT NEVER RECONSTRUCTS A SOURCE FROM A DERIVED FORM.
#
# Concretely: production `render_fill_polygon_utm` is NOT invertible. It is
# an ASYMMETRIC disc opening (erode r + lead, dilate r) -- it SEVERS necks
# narrower than 2*(r + lead) and dilation regrows only the survivors, so the
# severed neck is gone, not shrunk. No inverse exists; two different
# footprints open to the same fill. Rehydration therefore never tries to
# recover `polygon_utm` from `render_fill_polygon_utm`.
#
# The wire carries `polygon_utm` (as its stored WGS84 reprojection,
# `geometry_wgs84` -- see scored_production_areas_to_feature_collection()
# above, which emits exactly that and NOT the render fill). Rehydration
# takes the user's version of THAT ONE editable source and re-runs the same
# forward derivations the pipeline runs on it, producing every other
# representation. Every derived field below is computed, never read off the
# wire; every advisory field is read off the wire, never computed.
#
# NO NETWORK, EVER. Every derivation here is pure and local against the
# already-cached DEM: reprojection against dem['crs'], pixel-center
# rasterization onto dem['array'], elevation sampling out of dem['array'],
# the render opening over the resulting cell mask. A rehydration that
# fetches is a bug -- test_wire_translation.py asserts it with a socket
# counter, not a stopwatch.
#
# WHY THE IMPORTS BELOW ARE FUNCTION-LOCAL. Same reason the outbound half's
# are (see the module docstring's IMPORT DIRECTION note): production_area.py
# imports THIS module for its own `production_areas_to_geojson` alias, so a
# module-level import back into it closes a cycle. It also keeps the
# outbound half's dependency surface exactly where it was -- a caller that
# imports this module only to emit GeoJSON still pulls in nothing but
# feature_schema. The inbound half unavoidably deals in shapely geometry
# (that is the shape the override parameters take), so the module docstring's
# "the wire stays agnostic of shapely/numpy" holds for outbound only.


# The STEP-4 advisory block production_suitability.score_production_areas()
# ADDS on top of cluster_and_gate()'s own patch shape, restricted to the
# fields scored_production_areas_to_feature_collection() actually puts on
# the wire. Read back verbatim, NEVER recomputed -- scoring needs STEP 1's
# per-cell factor arrays, which are not derivable from an edited polygon.
#
# ALL-OR-NOTHING, gated on 'suitability_score'. A feature carrying the block
# is a suggested zone coming home unchanged; a feature without it is a zone
# a human authored, and it gets NO advisory field at all -- not a zero, not
# a None, ABSENT. That distinction is the point: 0.0 is a legible
# suitability score meaning "worst possible ground", and a drawn zone has
# not been scored rather than scored badly. The shipped frontend already
# treats the two as different kinds of object (drawn zones list acres and
# cautions only -- no rank, score, slope range or band), and no consumer of
# the `production_areas=` override reads any of these fields.
#
# 'confidence_notes' rides in the block rather than beside it deliberately:
# every feature_schema Feature carries one (the schema requires it), so
# taking it unconditionally would import a client-authored display string
# onto a drawn zone's internal dict as though scoring had produced it.
_ADVISORY_WIRE_FIELDS = (
    "rank",
    "suitability_score",
    "slope_factor",
    "size_factor",
    "aspect_factor",
    "avg_slope_pct",
    "aspect_deg",
    "soil_carved_acres",
    "soil_carved_pct",
    "soil_data_available",
    "source_patch_id",
    "confidence_notes",
)

# Present on a scored patch INTERNALLY but never emitted by
# scored_production_areas_to_feature_collection(), so nothing inbound can
# recover them. Named here rather than left implicit because this is exactly
# the outbound/inbound asymmetry the proposal puts both directions in one
# module to make visible -- see the branch report. All three are advisory
# sub-scores read only by production_area_ceiling._patch_narrative_data()
# (the report path, which runs inside the GENERATOR and never sees a
# rehydrated patch); no consumer of the `production_areas=` override touches
# any of them.
_ADVISORY_FIELDS_NOT_ON_THE_WIRE = ("area_score", "compactness_score", "aspect_available")


# The one place the outbound feature-id spelling is written down, so inbound
# parses exactly what outbound builds. Both
# production_areas_to_feature_collection() and
# scored_production_areas_to_feature_collection() emit
# f"production-area-{patch['id']}".
_PRODUCTION_FEATURE_ID_PREFIX = "production-area-"


# The dimensionless degeneracy floor _polygonal_shape_from_wire() rejects a
# ring below: enclosed area over the square of the ring's own extent. See the
# check itself for why it is a ratio and not a square-metre floor.
_DEGENERATE_SLIVER_RATIO = 1e-9


class InboundGeometryError(ValueError):
    """
    A committed geometry that cannot be rehydrated into a valid internal
    patch: wrong geometry type, a ring with fewer than 3 distinct vertices,
    a self-intersecting ring, a zero-area sliver, a zone that covers no DEM
    cell center, or one whose elevations are all nodata.

    FAILS LOUDLY AND SPECIFICALLY, never repairs. buffer(0) would silently
    turn a bowtie into two lobes of the drawer's ground and hand downstream
    a patch nobody drew; a zero-area sliver would rehydrate into a patch
    with no cells, no elevation and no fill. Rejecting these AT COMMIT, with
    the offending feature identified to the user, is the commit-validation
    branch's job (proposal section 2.5); failing cleanly and legibly here is
    this boundary's.
    """


def _polygonal_shape_from_wire(geometry: dict, dem: dict, where: str):
    """
    One inbound GeoJSON geometry -> a shapely Polygon/MultiPolygon in
    dem['crs'] meters, with every structural check applied on the way.

    Checks run in this order on purpose, cheapest and most specific first,
    so the error names the actual defect rather than a downstream symptom of
    it: type, then ring vertex counts (a 2-vertex ring cannot even be
    constructed), then shapely validity (self-intersection), then area.
    """
    import math

    from rasterio.warp import transform_geom
    from shapely.geometry import shape as shapely_shape
    from shapely.validation import explain_validity

    if not isinstance(geometry, dict):
        raise InboundGeometryError(f"{where}: geometry must be a GeoJSON geometry dict, got {type(geometry).__name__}")

    geometry_type = geometry.get("type")
    if geometry_type not in ("Polygon", "MultiPolygon"):
        raise InboundGeometryError(
            f"{where}: a committed zone must be a Polygon or MultiPolygon, got {geometry_type!r}. "
            "A zone is an area of ground; a Point or LineString has no acreage to attribute "
            "and no cells to sample."
        )

    coordinates = geometry.get("coordinates")
    if not coordinates:
        raise InboundGeometryError(f"{where}: geometry has no coordinates")

    # A GeoJSON linear ring closes, so a triangle is 4 positions. Count
    # DISTINCT ones instead of raw length: [a, b, a, a] is 4 positions and
    # still not a ring. Checked before shapely sees it because shapely
    # raises its own opaque error on a 2-point ring.
    parts = coordinates if geometry_type == "MultiPolygon" else [coordinates]
    for part_index, part in enumerate(parts):
        if not part:
            raise InboundGeometryError(f"{where}: part {part_index} has no rings")
        for ring_index, ring in enumerate(part):
            distinct = {(float(position[0]), float(position[1])) for position in ring}
            if len(distinct) < 3:
                kind = "exterior ring" if ring_index == 0 else f"interior ring {ring_index}"
                raise InboundGeometryError(
                    f"{where}: part {part_index}'s {kind} has {len(distinct)} distinct vertex/vertices; "
                    "a ring needs at least 3 to enclose any ground."
                )

    # ONE reprojection, WGS84 -> the DEM's own CRS. The DEM's CRS is the
    # frame every internal geometry in this pipeline lives in, and the frame
    # geometry_wgs84 was originally projected OUT of -- so this is the exact
    # inverse hop, not a new choice of projection.
    utm_geometry = shapely_shape(transform_geom("EPSG:4326", dem["crs"], geometry))

    if not utm_geometry.is_valid:
        raise InboundGeometryError(
            f"{where}: geometry is not valid -- {explain_validity(utm_geometry)}. "
            "Rehydration does not repair geometry: buffer(0) on a self-intersecting ring silently "
            "returns lobes nobody drew, and the acreage attributed to them would be fiction."
        )
    if utm_geometry.is_empty:
        raise InboundGeometryError(f"{where}: geometry is empty after reprojection into {dem['crs']}")
    if utm_geometry.geom_type not in ("Polygon", "MultiPolygon"):
        raise InboundGeometryError(
            f"{where}: reprojection produced {utm_geometry.geom_type}, not a polygonal geometry"
        )
    # ZERO AREA, AND EFFECTIVELY-ZERO AREA. The exact test alone is not
    # enough: a ring of collinear UTM vertices has area exactly 0.0, but it
    # does not ARRIVE in UTM -- it arrives as the WGS84 form of itself, where
    # those vertices are no longer exactly collinear, and reprojecting it back
    # yields a sliver of around 1e-19 m^2 rather than a clean zero. So the
    # thinness test below is what actually catches a degenerate ring off the
    # wire; the exact one only catches a locally-constructed geometry.
    #
    # Measured as area over the SQUARE OF THE GEOMETRY'S OWN EXTENT, which is
    # dimensionless and therefore scale-free: it says "how much of the ground
    # this ring spans does it actually enclose", and it means the same thing
    # for a 10 m draw as for a 500 m one. An absolute square-metre floor
    # cannot do that -- one small enough to clear a genuinely thin zone is too
    # small to catch a sliver spanning hundreds of metres.
    #
    # 1e-9 is a degeneracy floor, not a shape preference. A real zone one
    # MILLIMETRE wide and 100 m long still scores about 1e-5, four orders
    # above it; reaching 1e-9 over that span takes a width measured in
    # nanometres. Rejecting weak-but-real shapes is not this function's
    # business -- that is commit validation's (proposal section 2.5).
    minx, miny, maxx, maxy = utm_geometry.bounds
    extent_squared = (maxx - minx) ** 2 + (maxy - miny) ** 2
    if utm_geometry.area <= 0 or extent_squared <= 0 or (
        utm_geometry.area / extent_squared < _DEGENERATE_SLIVER_RATIO
    ):
        raise InboundGeometryError(
            f"{where}: geometry encloses effectively zero area ({utm_geometry.area:.6g} m^2 across a "
            f"{math.sqrt(extent_squared):.2f} m extent) -- a collinear or degenerate ring, not a zone."
        )

    return utm_geometry


def rehydrate_production_zone(feature: dict, dem: dict, zone_id: Optional[int] = None) -> dict:
    """
    ONE committed production-zone Feature -> the internal patch dict the
    `production_areas=` override parameters expect. The INBOUND half of the
    translation boundary; the exact counterpart of
    scored_production_areas_to_feature_collection() above.

    `feature` is a feature_schema Feature whose geometry is the zone's
    editable source in WGS84 -- for a suggested zone coming home unchanged
    that is the very `geometry_wgs84` the outbound function emitted; for a
    user-drawn zone it is whatever the drawing tool clamped to the parcel.
    `dem` is the cached ParcelData's own DEM dict. `zone_id` overrides the
    integer id; when None it is parsed off the feature's own
    "production-area-<n>" id.

    ONE FEATURE IS ONE PATCH, EVEN WHEN THE CLAMP SPLIT IT. The shipped
    frontend clamps a drawn ring to the parcel with a polygon-clipping
    intersection, which routinely returns SEVERAL pieces, each with its own
    interior rings -- and it keeps the result as ONE zone with one id, one
    acreage and one caution list. Rehydration preserves that: a multi-part
    geometry becomes one patch whose polygon_utm is a MultiPolygon. Splitting
    it into several patches would invent ids the user never saw, and would
    scatter one drawn zone's acreage across several rows of a report that is
    supposed to say what the user drew. It also costs nothing to support:
    cluster_and_gate()'s own polygon_utm is `footprint.intersection(
    boundary_polygon_utm)` and its render fill is a cell-union of a possibly
    disconnected opened mask, so BOTH are routinely MultiPolygons already --
    every consumer has always handled them.

    WHAT IS DERIVED AND WHAT IS INHERITED. Everything cluster_and_gate()
    computes is RE-DERIVED here from the geometry plus the cached DEM, in the
    pipeline's own order and by the pipeline's own code:

        polygon_utm                 the wire geometry, reprojected into
                                    dem['crs'] -- the editable source
        cells                       raster_grid.cells_in_polygon(): pixel-
                                    center containment, STEP 1's own
                                    rasterization convention
        representative_elevation_m  median of dem['array'] over those cells,
                                    exactly as cluster_and_gate() takes it
        area_acres                  polygon_utm.area / SQUARE_METERS_PER_ACRE
        render_fill_polygon_utm     production_area.render_fill_polygon_for_
                                    cluster() -- the pipeline's OWN opening,
                                    called, not reimplemented
        render_fill_area_acres      \\
        render_fill_geometry_wgs84   > straight off the two polygons above,
        geometry_wgs84              /  by the same expressions
        hole_footprints             production_area._detect_hole_footprints()

    ...and the STEP-4 advisory block is INHERITED verbatim from the feature's
    properties when present, or absent entirely when it is not (see
    _ADVISORY_WIRE_FIELDS). Nothing is both.

    NOT CLIPPED TO THE PARCEL, DELIBERATELY. cluster_and_gate() clips its
    footprint to boundary_polygon_utm because it is building a zone out of
    raw cells; rehydration is handed a zone that was already clamped
    client-side, and re-clipping it here would (a) perturb an unedited
    suggested zone's polygon by the intersection's own floating-point noise,
    breaking the round-trip identity this boundary exists to guarantee, and
    (b) quietly repair an off-parcel commit that the SERVER-AUTHORITATIVE
    commit validation of proposal section 2.5 is supposed to REJECT and
    report. Clamping is UX, validation is the commit path's; neither is this
    function's.

    Raises InboundGeometryError, with the defect named, on anything that
    cannot become a valid patch. It never returns a half-built one.
    """
    import math

    import numpy as np
    from rasterio.warp import transform_geom
    from shapely.geometry import mapping

    from production_area import (
        SQUARE_METERS_PER_ACRE,
        _detect_hole_footprints,
        render_fill_polygon_for_cluster,
    )
    from raster_grid import cells_in_polygon

    if not isinstance(feature, dict):
        raise InboundGeometryError(f"a production zone must be a GeoJSON Feature dict, got {type(feature).__name__}")

    feature_id = feature.get("id")
    where = f"production zone {feature_id!r}" if feature_id is not None else "production zone"

    if zone_id is None:
        zone_id = _zone_id_from_feature_id(feature_id, where)

    polygon_utm = _polygonal_shape_from_wire(feature.get("geometry"), dem, where)

    # STEP 1's rasterization convention, so this zone's cells are the same
    # cells STEP 1 would have called it. Empty is a REAL possibility (a zone
    # thinner than the gap between cell centers) and it is fatal, not
    # tolerable: representative_elevation_m has nothing to take a median
    # over, and the render opening has no mask to open.
    cells = cells_in_polygon(dem, polygon_utm)
    if not cells:
        raise InboundGeometryError(
            f"{where}: covers no DEM cell center ({polygon_utm.area:.2f} m^2 at "
            f"{dem['resolution_meters'][0]:.1f}x{dem['resolution_meters'][1]:.1f} m resolution), so it has no "
            "cells to sample an elevation from and no mask to open. A zone has to be at least about one "
            "cell across to be a zone."
        )

    # cluster_and_gate()'s own expression, verbatim. A generated cluster can
    # never carry a nodata cell (STEP 1's slope gate drops them with
    # ~np.isnan), but a hand-drawn ring can sit over one -- and a NaN
    # representative elevation would travel silently into every gravity-feed
    # differential water_candidate_zones.py computes off this patch. Fail
    # instead.
    elevations = [float(dem["array"][r, c]) for r, c in cells]
    representative_elevation_m = float(np.median(elevations))
    if math.isnan(representative_elevation_m):
        nodata_count = sum(1 for value in elevations if math.isnan(value))
        raise InboundGeometryError(
            f"{where}: {nodata_count} of its {len(cells)} DEM cells are nodata, leaving no median "
            "elevation. Every downstream gravity-feed differential is computed against this value, and a "
            "NaN would propagate into all of them without raising anything."
        )

    # THE PIPELINE'S OWN OPENING, CALLED. Not a second implementation of it
    # -- see production_area.render_fill_polygon_for_cluster()'s docstring
    # for why it is a function at all. mask_shape is the full DEM grid, the
    # same grid cluster_and_gate() passes its cell_mask's shape for.
    render_fill_polygon_utm = render_fill_polygon_for_cluster(
        cells, dem["array"].shape, dem, polygon_utm
    )

    patch = {
        "id": zone_id,
        "area_acres": round(float(polygon_utm.area / SQUARE_METERS_PER_ACRE), 2),
        "representative_elevation_m": representative_elevation_m,
        "polygon_utm": polygon_utm,
        "render_fill_polygon_utm": render_fill_polygon_utm,
        "render_fill_area_acres": round(float(render_fill_polygon_utm.area / SQUARE_METERS_PER_ACRE), 2),
        "render_fill_geometry_wgs84": (
            transform_geom(dem["crs"], "EPSG:4326", mapping(render_fill_polygon_utm))
            if not render_fill_polygon_utm.is_empty
            else None
        ),
        "geometry_wgs84": transform_geom(dem["crs"], "EPSG:4326", mapping(polygon_utm)),
        "cells": cells,
        "hole_footprints": _detect_hole_footprints(cells, dem),
    }

    # The advisory block, all-or-nothing (see _ADVISORY_WIRE_FIELDS). A
    # suggested zone coming home carries it; a drawn zone gets no key at all.
    properties = feature.get("properties") or {}
    if "suitability_score" in properties:
        for field in _ADVISORY_WIRE_FIELDS:
            if field in properties:
                patch[field] = properties[field]

    return patch


def internal_zone_id(feature_id: Any) -> Optional[int]:
    """
    The integer patch id behind an outbound feature id, or None when this
    module's outbound half did not build that id.

    THE PUBLIC HALF of _zone_id_from_feature_id() below, and the question the
    COMMIT PATH has to ask before it can rehydrate anything: "does this
    committed feature carry a pipeline id, or is it a shape somebody drew?"
    A drawn zone must be given an internal id the commit path allocates (see
    step_registry.CommitContract.internal_id_parameter), and asking by
    catching InboundGeometryError off the private function would be using an
    exception to answer a question that has a plain answer. None is that
    answer -- it means "not one of ours", never "malformed".

    Spelling the id is this module's job and nowhere else's: outbound builds
    f"production-area-{patch['id']}" and inbound parses exactly that, and a
    second parser in the commit path would be a second spelling waiting to
    disagree with this one.
    """
    if isinstance(feature_id, int) and not isinstance(feature_id, bool):
        return feature_id
    if isinstance(feature_id, str) and feature_id.startswith(_PRODUCTION_FEATURE_ID_PREFIX):
        tail = feature_id[len(_PRODUCTION_FEATURE_ID_PREFIX):]
        if tail.isdigit():
            return int(tail)
    return None


def _zone_id_from_feature_id(feature_id: Any, where: str) -> int:
    """
    The integer patch id behind an outbound feature id.

    scored_production_areas_to_feature_collection() builds
    f"production-area-{patch['id']}", so an unedited suggested zone comes
    home carrying its own id and keeps it -- which is what makes a rehydrated
    patch line up with the water zone that already recorded it in
    served_production_area_ids.

    A drawn zone has no such id, so its caller passes zone_id= explicitly.
    Guessing one here (a running counter, a hash) would risk colliding with a
    suggested zone's id in the same commit, and a collision would silently
    merge two zones' served-area accounting. Raise instead.
    """
    zone_id = internal_zone_id(feature_id)
    if zone_id is not None:
        return zone_id
    raise InboundGeometryError(
        f"{where}: cannot determine an integer zone id from feature id {feature_id!r}. A suggested zone "
        "carries \"production-area-<n>\"; a user-drawn zone has no pipeline id, so its caller must pass "
        "zone_id= explicitly rather than have one invented here (an invented id can collide with a "
        "suggested zone's in the same commit, silently merging their served-area accounting)."
    )


def rehydrate_production_zones(
    collection: Optional[dict],
    dem: dict,
    zone_ids: Optional[list[int]] = None,
) -> list[dict]:
    """
    A whole committed production-zone FeatureCollection -> the list the
    `production_areas=` override parameters take.

    EMPTY IN, EMPTY OUT -- None, a collection with no features, or a bare
    empty list all give []. That mirrors the outbound half's own contract
    (an empty FeatureCollection means "computed, nothing there") and it is a
    meaningful value downstream, not a degenerate one: every consumer treats
    an empty production list as "checked, no production ground" and keeps
    running, while None means "never checked" (see water_survey_areas.
    _production_overlap_pct()'s sentinel semantics).

    `zone_ids`, when given, must be one id per feature, in order -- for a
    commit whose drawn zones carry no pipeline id. Ids are NOT deduplicated
    or renumbered here; a commit that reuses one is a commit-validation
    failure (proposal section 2.5), not something to paper over.

    A single bad feature fails the WHOLE call. A partial list would hand
    downstream a commit the user did not make, with the rejected zone's
    acreage silently missing from every total computed off it.
    """
    features = (collection or {}).get("features") if isinstance(collection, dict) else collection
    features = list(features or [])

    if zone_ids is not None and len(zone_ids) != len(features):
        raise InboundGeometryError(
            f"zone_ids has {len(zone_ids)} entries for {len(features)} features -- one id per feature, "
            "in order, or None to parse each feature's own id."
        )

    return [
        rehydrate_production_zone(feature, dem, zone_id=None if zone_ids is None else zone_ids[index])
        for index, feature in enumerate(features)
    ]


# ======================================================================
# INBOUND -- the water step's survey zones
# ======================================================================
#
# WHY THIS IS SHORTER THAN THE PRODUCTION HALF ABOVE, AND WHY THAT IS NOT
# AN OMISSION. The water step is SELECT-ONLY: there is no drawing tool and
# no editing, so every committed feature is one this pipeline generated and
# handed to the client, coming home with its geometry untouched. That
# removes at a stroke everything rehydrate_production_zone() has to do for
# a drawn ring -- there is no id to allocate (see internal_water_survey_
# zone_id()), no clamped multi-part shape to reassemble, and no morphology
# to re-derive, because a survey zone's render fill IS its own envelope
# (water_survey_areas.build_survey_zones() sets the two to the same object).
#
# The structural checks still run in full. Select-only makes an invalid
# geometry unlikely, not impossible -- a client can send anything -- and a
# gate that is only correct while the client behaves is not a gate.


def internal_water_survey_zone_id(feature_id: Any) -> Optional[int]:
    """
    The integer zone id behind an outbound water-survey-zone feature id, or
    None when this module's outbound half did not build that id.

    THE MEMBER AND DROPPED IDS DO NOT PARSE, WHICH IS THE POINT. All three
    of "water-survey-zone-7", "water-survey-zone-member-7" and
    "water-survey-zone-dropped-7" share this prefix, and only the first
    names a committable zone. The tail is required to be all digits, so the
    other two return None and a commit carrying one is rejected by name
    rather than silently rehydrated as zone 7 -- which would attribute a
    member's footprint, or a zone the acreage floor dropped, to a selection
    the user never made.
    """
    if isinstance(feature_id, int) and not isinstance(feature_id, bool):
        return feature_id
    if isinstance(feature_id, str) and feature_id.startswith(
        _WATER_SURVEY_ZONE_FEATURE_ID_PREFIX
    ):
        tail = feature_id[len(_WATER_SURVEY_ZONE_FEATURE_ID_PREFIX):]
        if tail.isdigit():
            return int(tail)
    return None


def rehydrate_water_survey_zone(feature: dict, dem: dict) -> dict:
    """
    ONE committed water survey-zone Feature -> the internal zone dict.

    WHAT IT CARRIES, AND WHY SO LITTLE. Identity (`id`, `survey_type`) and
    geometry, and nothing else. A survey zone's full measurement set --
    suitability, dual acreage, the three overlap sentinels, the gravity
    block, cross_type_overlaps -- was computed at GENERATE time against the
    whole parcel's surfaces and is already recorded twice: in the payload
    the user selected from, and in this very feature's own `properties`,
    which the Design Document stores verbatim. Copying a subset of it onto
    a second object here would be a second copy of the same measurements
    with nothing keeping the two in step, and re-DERIVING any of it is not
    possible from a polygon and a DEM: the numbers are functions of the
    suitability surfaces, not of the shape.

    So this returns what a consumer of a committed selection can use --
    where the ground is -- and refers everything else to the record.

    THE RENDER FILL IS THE ENVELOPE, BY IDENTITY. build_survey_zones() sets
    render_fill_polygon_utm to the clipped hull object itself and states
    that no further morphology is ever applied to it; the outbound half
    puts that same geometry on the wire. So the inverse hop reconstructs
    ONE polygon and both names point at it -- unlike a production patch,
    where the wire geometry is the editable source and the render fill is a
    morphological opening of it.

    `zone_acres` IS re-derived (polygon area over the parcel-clipped hull --
    build_survey_zones()'s own expression), because it is a function of the
    geometry alone and a consumer holding this dict should not have to reach
    back into the wire properties for the one number that survives the trip
    honestly.

    Raises InboundGeometryError, with the defect named, on anything that
    cannot become a valid zone.
    """
    from raster_grid import SQUARE_METERS_PER_ACRE

    if not isinstance(feature, dict):
        raise InboundGeometryError(
            f"a water survey zone must be a GeoJSON Feature dict, got {type(feature).__name__}"
        )

    feature_id = feature.get("id")
    where = (
        f"water survey zone {feature_id!r}" if feature_id is not None else "water survey zone"
    )

    zone_id = internal_water_survey_zone_id(feature_id)
    if zone_id is None:
        # NO ALLOCATION FALLBACK, deliberately -- the opposite of the
        # production path's. There is no drawing tool at this step, so a
        # feature without a pipeline id is not a shape the user made; it is a
        # feature that did not come from this step's proposals (a member
        # footprint, a dropped zone, a hand-assembled request). Allocating an
        # id for it would manufacture a zone that no suitability surface ever
        # nominated and attribute a survey recommendation to it.
        raise InboundGeometryError(
            f"{where}: cannot determine a survey-zone id from feature id {feature_id!r}. Every "
            f"committable zone carries \"{_WATER_SURVEY_ZONE_FEATURE_ID_PREFIX}<n>\" -- this step is "
            "select-only, so a feature with no pipeline id did not come from its proposals and no id "
            "is invented for it."
        )

    layer = (feature.get("properties") or {}).get("layer")
    if layer not in LAYER_SURVEY_ZONES:
        raise InboundGeometryError(
            f"{where}: carries layer {layer!r}; a committable survey zone is on one of "
            f"{list(LAYER_SURVEY_ZONES)}. The layer is where the zone's TYPE lives, and a zone with "
            "no type cannot be told apart from the other instrument's answer for the same ground."
        )

    polygon_utm = _polygonal_shape_from_wire(feature.get("geometry"), dem, where)

    return {
        "id": zone_id,
        # READ OFF THE LAYER, not off properties.survey_type. The layer is
        # the field the commit contract gated on, so it is the one the commit
        # path has actually established; a properties value disagreeing with
        # it would be believed here and refused there.
        "survey_type": layer[len("survey_zone_"):],
        "zone_acres": round(float(polygon_utm.area / SQUARE_METERS_PER_ACRE), 4),
        "polygon_utm": polygon_utm,
        "render_fill_polygon_utm": polygon_utm,
        # OFF THE WIRE, NOT REPROJECTED BACK. The feature's geometry IS the
        # zone's stored geometry_wgs84 -- built once at the zone's birth and
        # never edited, because this step has no editor -- so round-tripping
        # it through two transform_geom() hops would replace an exact value
        # with a numerically perturbed copy of itself.
        "geometry_wgs84": feature.get("geometry"),
        "render_fill_geometry_wgs84": feature.get("geometry"),
    }


def rehydrate_water_survey_zones(
    collection: Optional[dict],
    dem: dict,
) -> list[dict]:
    """
    A whole committed water survey-zone FeatureCollection -> the list of
    internal zone dicts.

    EMPTY IN, EMPTY OUT, AND [] IS NOT THE ANSWER A CONSUMER GETS. Every
    consumer of `selected_water_zone=` takes ONE zone or the explicit
    water_suitability.NO_WATER_ZONE sentinel; none of them takes a list. So
    an empty commit does not travel as this function's [] -- the registry's
    `empty_commit` declaration intercepts it first and forwards the sentinel
    (step_registry.Consumed's EMPTY IS AN ANSWER note). [] here means only
    "no features to rehydrate", and the one caller that can see it is the
    commit gate, which is checking a collection it already counted.

    NO `zone_ids` PARAMETER, unlike the production rehydrator. That
    parameter exists there so the COMMIT PATH can allocate an id for a drawn
    zone; there is no drawing at this step, so there is no id to allocate
    and the water commit contract declares no internal_id_parameter.

    A single bad feature fails the WHOLE call, for the production
    rehydrator's reason: a partial list is a selection nobody made.
    """
    features = (collection or {}).get("features") if isinstance(collection, dict) else collection
    return [rehydrate_water_survey_zone(feature, dem) for feature in list(features or [])]


def water_zone_union(zones: list[dict]) -> dict:
    """
    MANY selected survey zones -> the ONE zone-shaped value every
    `selected_water_zone=` override takes.

    THE UNION IS THE WHOLE OF WHAT MULTI-SELECT MEANS DOWNSTREAM. The water
    step lets a user select any number of zones, across both survey types;
    downstream, all of them are claimed ground. Every consumer of a selected
    water zone reads exactly one field off it -- render_fill_polygon_utm --
    and applies its OWN buffer to it (road_corridors' pond exclusion,
    tree_zone_candidates' water polygons, solar_suitability's water
    exclusion, fencing, the layout map), so a single geometry carrying the
    union of the selection is a complete answer for all of them and NO
    consumer signature changes. Buffering is not this boundary's concern and
    must not become it.

    WHAT THIS DICT DELIBERATELY DOES NOT CARRY, which is the more important
    half. There is no `id`, no `survey_type`, no `rank`, no
    `mean_suitability`, no `zone_acres`, no `representative_elevation_m`.
    The union is not a zone: no suitability surface nominated it, no
    surveyor would rope it off as one claim, and every one of those fields
    would be a fabricated measurement of an object that does not exist.
    Leaving them absent means a consumer that reaches for one gets a
    KeyError naming the field -- which is the loud, immediate failure this
    boundary owes it -- rather than a plausible number that reads as
    measured.

    ONE CONSUMER TODAY IS IN EXACTLY THAT POSITION and it is reported rather
    than papered over: pipeline_context._attach_keypoint_feature_
    relationships() reads `representative_elevation_m` off the selected
    water zone to compute each keypoint's elevation differential. A union of
    three zones has no single representative elevation -- the honest answer
    is per-keypoint, against the NEAREST selected zone, which is a change to
    that function's signature and to what the batch pipeline means by the
    keypoint water relationship. So this dict does not answer it, and the
    water registry entry does NOT declare the keypoint post-commit hook: the
    hook keeps running on the landform commit alone, and the water half of
    every keypoint relationship keeps reading "no_feature" exactly as it
    does today. That is a known gap, not a silent one.

    Raises ValueError on an empty selection rather than inventing a value
    for it. An empty commit is a real decision and it has a real
    representation -- water_suitability.NO_WATER_ZONE, declared as the water
    consumes edge's `empty_commit` -- and reaching this function with nothing
    means that declaration was bypassed.
    """
    from shapely.ops import unary_union

    if not zones:
        raise ValueError(
            "water_zone_union() was called with no zones. An empty water selection is a DECISION "
            "and travels as water_suitability.NO_WATER_ZONE (the water consumes edge's empty_commit "
            "declaration), never as a union of nothing and never as None -- every downstream "
            "consumer reads None as 'not supplied' and re-runs the whole water pipeline."
        )

    union = unary_union([zone["render_fill_polygon_utm"] for zone in zones])
    return {
        # THE ONE FIELD EVERY CONSUMER READS, and the reason this shape works
        # at all. See the docstring for what is absent and why.
        "render_fill_polygon_utm": union,
        # The same geometry under the survey zone's other name for it. A
        # survey zone's envelope and its render fill are one object
        # (build_survey_zones()), so the union of one is the union of the
        # other; carried so a consumer written against `polygon_utm` sees
        # the identical ground rather than a KeyError that would be
        # misleading -- this field is not missing, it is the same answer.
        "polygon_utm": union,
        # PROVENANCE OF THE UNION, so a reader holding this dict can say
        # which zones it was built from without going back to the document.
        # Not an identity: `zone_ids` is a list precisely so it cannot be
        # mistaken for the `id` of a zone.
        "zone_ids": [zone["id"] for zone in zones],
        "survey_types": sorted({zone["survey_type"] for zone in zones}),
    }
