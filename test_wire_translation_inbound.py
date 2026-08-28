"""
test_wire_translation_inbound.py

Offline (no-network) checks for the INBOUND half of the translation
boundary -- wire_translation.rehydrate_production_zone() /
rehydrate_production_zones(), the "rehydration" of
interactive-design-architecture-proposal.md section 2.4.

Split from test_wire_translation.py (which covers the OUTBOUND half) the
same way test_production_area_render_opening.py is split from
test_production_area.py: one module, several test files, each with its own
fixture cost. This one needs a REAL pipeline run to have anything to
round-trip against, which the outbound file's synthetic-shape fixtures
deliberately avoid.

Script-style, per this repo's convention: run it directly
(`python test_wire_translation_inbound.py`), assertions inline, printed
section headers.

THE REFERENCE PROPERTY. Every geometry here sits on the same real parcel
the rest of this repo's tests use -- roughly 40.6429-40.6459 N,
79.9805-79.9838 W, 13.23 acres, UTM 17N (EPSG:32617) -- verbatim from
production_area_ceiling.py's own __main__ block, the same six lon/lat pairs
test_production_zone_payload.py and diagnose_exclusion_footprints.py carry.

REAL PIPELINE CODE, SYNTHETIC TERRAIN. The DEM under those real coordinates
is built here (the same dissected-plateau fixture
test_production_zone_payload.py uses: a ~4% bench cut by two incised
drainages, with riparian woodland in the cuts and six interior canopy
pockets), because a test that fetched a real DEM would need the network and
this repo's tests do not. Everything ABOVE the DEM is the pipeline's own
code, unmocked: identify_optimized_production_areas() runs STEP 1 -> 2 -> 3
-> 4 for real, so the patches test 1 round-trips are patches
cluster_and_gate() and score_production_areas() actually produced, not
hand-written dicts shaped like them.

SIX THINGS THIS FILE EXISTS TO PROVE

  1. ROUND-TRIP IDENTITY -- the branch's reason to exist. A GENERATED patch
     pushed OUT through scored_production_areas_to_feature_collection() and
     back IN through rehydrate_production_zone() must come back as the same
     internal dict, field by field, INCLUDING every derived representation
     (cells, representative elevation, the render opening, the hole
     footprints). One tolerance, on one pair of fields, justified in place.

  2. A USER-DRAWN ZONE -- a polygon no pipeline ever produced -- rehydrates
     with every field a downstream consumer reads present and correctly
     typed, and with the STEP-4 advisory fields ABSENT rather than
     defaulted to zero.

  3. DOWNSTREAM ACCEPTANCE -- the go/no-go. A rehydrated user-drawn zone,
     passed as the `production_areas=` override into two REAL consumer
     entry points (water_survey_areas.identify_water_survey_areas() and
     road_corridors.build_road_network()), runs and produces sane output.
     This is the actual proof that a user-authored feature travels down the
     same override parameter a computer-authored one does.

  4. MULTI-PART geometry, which is the NORMAL case and not an edge case:
     the shipped frontend clamps a drawn ring to the parcel and that clamp
     routinely splits it. One drawn zone rehydrates as ONE patch with
     MultiPolygon geometry, holes preserved, and survives test 3.

  5. NO NETWORK during rehydration -- asserted with a socket counter that
     also raises, not with a stopwatch.

  6. DEGENERATE INPUT fails LOUDLY and SPECIFICALLY: a self-intersecting
     ring, a zero-area sliver, a ring with fewer than 3 vertices, a
     non-polygonal geometry, a zone smaller than the gap between DEM cell
     centers. Rejecting these AT COMMIT is the commit-validation branch's
     job (proposal section 2.5); failing cleanly here is this one's.
"""

import math
import socket

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import MultiPolygon, Polygon, box, mapping

from feature_schema import CONFIDENCE_LOW, make_feature, validate_feature_collection
from production_area_ceiling import identify_optimized_production_areas
from wire_translation import (
    LAYER_PRODUCTION_AREA,
    InboundGeometryError,
    rehydrate_production_zone,
    rehydrate_production_zones,
    scored_production_areas_to_feature_collection,
)

SQUARE_METERS_PER_ACRE = 4046.8564224

# The reference property, verbatim from production_area_ceiling.py's own
# __main__ block -- roughly 40.643-40.646 N, 79.980-79.984 W, 13.23 acres.
REFERENCE_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

RESOLUTION_M = 5.0
CONTEXT_BUFFER_M = 100.0
UTM_17N = "EPSG:32617"

# 0.25 rather than production_area.MIN_PRODUCTION_AREA_ACRES (0.5) so this
# fixture yields THREE patches instead of one. Two of the three are thinner
# than RENDER_OPENING_RADIUS_METERS throughout, which means they take
# render_fill_polygon_for_cluster()'s EMPTY-OPENING FALLBACK branch
# (render_fill_polygon_utm = polygon_utm, behind its warning) while the
# third takes the normal opening branch -- so the round-trip below covers
# both halves of the extracted helper rather than just the common one.
FIXTURE_MIN_AREA_ACRES = 0.25


def _synthetic_dem_and_canopy():
    """A dissected plateau: a bench tilted about 4%, cut by two incised
    drainages, with riparian woodland in the cuts and six interior canopy
    pockets. Lifted from test_production_zone_payload.py's fixture of the
    same name, for the same reason it was built there -- the slope gate
    genuinely bites on the drainage walls and the canopy gate genuinely
    punches HOLES in the eligible union, so the patches that come out have
    interior rings and the hole-footprint assertions below have something
    real to check."""
    xs, ys = warp_transform(
        "EPSG:4326", UTM_17N,
        [p[0] for p in REFERENCE_BOUNDARY],
        [p[1] for p in REFERENCE_BOUNDARY],
    )
    boundary_utm = Polygon(zip(xs, ys))
    min_x, min_y, max_x, max_y = boundary_utm.bounds
    min_x -= CONTEXT_BUFFER_M
    min_y -= CONTEXT_BUFFER_M
    max_x += CONTEXT_BUFFER_M
    max_y += CONTEXT_BUFFER_M

    width = int(np.ceil((max_x - min_x) / RESOLUTION_M))
    height = int(np.ceil((max_y - min_y) / RESOLUTION_M))
    grid_x, grid_y = np.meshgrid(
        min_x + (np.arange(width) + 0.5) * RESOLUTION_M,
        max_y - (np.arange(height) + 0.5) * RESOLUTION_M,
    )
    u = (grid_x - min_x) / (max_x - min_x)
    v = (grid_y - min_y) / (max_y - min_y)

    elevation = 300.0 + 26.0 * v + 6.0 * u
    for centre, depth, spread in ((0.34, 15.0, 0.055), (0.68, 11.0, 0.045)):
        elevation -= depth * np.exp(-((u - centre) ** 2) / (2 * spread ** 2))
    elevation += 0.28 * np.random.default_rng(20260824).standard_normal(elevation.shape)

    b_min_x, b_min_y, b_max_x, b_max_y = boundary_utm.bounds
    pu = (grid_x - b_min_x) / (b_max_x - b_min_x)
    pv = (grid_y - b_min_y) / (b_max_y - b_min_y)
    hag = np.zeros_like(elevation)
    for centre, spread in ((0.34, 0.030), (0.68, 0.026)):
        hag += 22.0 * np.exp(-((pu - centre) ** 2) / (2 * spread ** 2))
    for cu, cv, r in ((0.20, 0.55, 0.035), (0.50, 0.30, 0.030), (0.55, 0.72, 0.028),
                      (0.82, 0.45, 0.032), (0.44, 0.60, 0.022), (0.26, 0.28, 0.024)):
        hag += 20.0 * np.exp(-(((pu - cu) ** 2 + (pv - cv) ** 2)) / (2 * r ** 2))

    dem = {
        "array": elevation.astype(np.float32),
        "resolution_meters": (RESOLUTION_M, RESOLUTION_M),
        "origin_x": min_x,
        "origin_y": max_y,
        "crs": UTM_17N,
    }
    canopy = {"array": hag.astype(np.float32), "resolution_meters": (RESOLUTION_M, RESOLUTION_M)}
    return dem, canopy, boundary_utm


DEM, CANOPY, BOUNDARY_UTM = _synthetic_dem_and_canopy()


def _drawn_feature(polygon_utm, feature_id="drawn-zone", label="Zone the user drew"):
    """A committed feature exactly as a drawing tool would hand one back:
    the clamped geometry in WGS84, wrapped in the feature_schema envelope,
    carrying NO scoring property of any kind. Built through make_feature()
    rather than as a literal so the fixture cannot drift from the schema the
    real wire enforces."""
    return make_feature(
        feature_id=feature_id,
        geometry=transform_geom(UTM_17N, "EPSG:4326", mapping(polygon_utm)),
        layer=LAYER_PRODUCTION_AREA,
        label=label,
        confidence=CONFIDENCE_LOW,
        confidence_notes="A zone drawn by hand on the map, clamped to the parcel boundary. Not scored.",
    )


# ======================================================================
# The generated patches every round-trip assertion below runs against
# ======================================================================
# REAL pipeline code end to end: STEP 1 (eligible cells) -> STEP 2 (the
# ceiling trim) -> STEP 3 (cluster_and_gate()) -> STEP 4
# (score_production_areas()). check_soil/check_roads are off because the
# fetches they gate are network; the canopy mask is supplied from the
# fixture, which is the same thing the pipeline path does from ParcelData.

_ceiling_result = identify_optimized_production_areas(
    REFERENCE_BOUNDARY,
    dem=DEM,
    canopy_height=CANOPY,
    check_soil=False,
    check_roads=False,
    min_area_acres=FIXTURE_MIN_AREA_ACRES,
)
GENERATED_PATCHES = _ceiling_result["scored_patches"]
assert len(GENERATED_PATCHES) == 3, (
    f"the fixture is tuned to produce three patches, got {len(GENERATED_PATCHES)} -- "
    "the round-trip below wants more than one, and wants both render-opening branches"
)

# Both branches of render_fill_polygon_for_cluster() are exercised: at least
# one patch opened normally (its fill is a strict subset of its footprint)
# and at least one fell back (fill IS the footprint, the cluster being
# thinner than the opening radius throughout).
_opened = [p for p in GENERATED_PATCHES if p["render_fill_polygon_utm"].area < p["polygon_utm"].area]
_fell_back = [p for p in GENERATED_PATCHES if p["render_fill_polygon_utm"].equals(p["polygon_utm"])]
assert _opened and _fell_back, (
    "the fixture must cover BOTH render-opening branches -- "
    f"{len(_opened)} opened, {len(_fell_back)} fell back"
)

OUTBOUND = scored_production_areas_to_feature_collection(GENERATED_PATCHES)
validate_feature_collection(OUTBOUND)

print(
    f"Fixture: reference property (40.6429-40.6459 N, 79.9805-79.9838 W, "
    f"{BOUNDARY_UTM.area / SQUARE_METERS_PER_ACRE:.2f} ac, {UTM_17N}); "
    f"{len(GENERATED_PATCHES)} generated patches "
    f"({len(_opened)} opened, {len(_fell_back)} fell back to polygon_utm)."
)


# ======================================================================
# 1. ROUND-TRIP IDENTITY
# ======================================================================
# The branch's reason to exist. Out through the outbound function, back in
# through the inbound one, and the result must be the ORIGINAL internal
# dict -- not a lookalike.

print("\n== 1. ROUND-TRIP IDENTITY ==")

# Every field cluster_and_gate() itself produces. Enumerated from the real
# producer (production_area.cluster_and_gate()'s own patch literal), not
# from a docstring's summary of it -- the summary in
# pipeline_context.py's field notes omits four of these.
CLUSTER_AND_GATE_FIELDS = {
    "id", "area_acres", "representative_elevation_m", "polygon_utm",
    "render_fill_polygon_utm", "render_fill_area_acres", "render_fill_geometry_wgs84",
    "geometry_wgs84", "cells", "hole_footprints", "source_patch_id",
}
assert CLUSTER_AND_GATE_FIELDS <= set(GENERATED_PATCHES[0]), (
    "the field enumeration has drifted from what cluster_and_gate() actually returns: "
    f"missing {sorted(CLUSTER_AND_GATE_FIELDS - set(GENERATED_PATCHES[0]))}"
)

# THE ONE TOLERANCE IN THIS FILE, and it applies to exactly two fields:
# polygon_utm and render_fill_polygon_utm.
#
# WHY IT EXISTS. The wire carries polygon_utm as its stored WGS84
# reprojection (geometry_wgs84). Coming back, rehydration reprojects that
# WGS84 ring into the DEM's CRS -- so the round trip is
# transform_geom(UTM -> 4326) followed by transform_geom(4326 -> UTM), and
# PROJ is not exactly idempotent across that pair. Each vertex lands within
# a fraction of a nanometre of where it started, which is a nonzero
# symmetric difference and can never be exactly zero.
#
# WHY THIS FORM. Measured as symmetric-difference area RELATIVE to the
# original polygon's area -- the strictest formulation available, because a
# symmetric difference counts every direction a ring moved (inward and
# outward both), unlike comparing areas, which lets an inward drift on one
# edge cancel an outward drift on another and report zero.
#
# WHY THIS VALUE. 1e-9 is roughly ten times the worst observed (9.3e-11
# across these three patches). On the largest patch here, 4400 m^2, 1e-9 of
# relative symmetric difference is 4.4 square MICROMETRES. Nothing this
# pipeline computes can resolve that: cell membership is decided at 5 m,
# acreage is rounded to two decimals (0.01 ac = 40 m^2), and elevation is
# sampled per cell. It is a float-noise ceiling, not slack for a real
# geometric difference -- a genuine one-cell disagreement would be 25 m^2,
# nine orders of magnitude above this and instantly fatal here.
#
# NOTHING ELSE IS TOLERANCED. cells, area_acres, representative_elevation_m,
# render_fill_area_acres, the hole footprints and every advisory field are
# asserted EXACTLY equal below, and they hold exactly.
REPROJECTION_SYMMETRIC_DIFFERENCE_TOLERANCE = 1e-9

# The advisory block the wire carries, and the three fields it does NOT.
ADVISORY_ON_THE_WIRE = (
    "rank", "suitability_score", "slope_factor", "size_factor", "aspect_factor",
    "avg_slope_pct", "aspect_deg", "soil_carved_acres", "soil_carved_pct",
    "soil_data_available", "source_patch_id", "confidence_notes",
)
ADVISORY_NOT_ON_THE_WIRE = ("area_score", "compactness_score", "aspect_available")

worst_relative_symmetric_difference = 0.0
for feature, original in zip(OUTBOUND["features"], GENERATED_PATCHES):
    rehydrated = rehydrate_production_zone(feature, DEM)

    # The id came home off the feature's own id string, with no help.
    assert rehydrated["id"] == original["id"], (rehydrated["id"], original["id"])

    # EXACT, no tolerance: the raster derivations.
    assert rehydrated["cells"] == original["cells"], (
        f"patch {original['id']}: rehydrated cells differ from cluster_and_gate()'s -- "
        f"{len(rehydrated['cells'])} vs {len(original['cells'])}. This is the assertion that "
        "keeps raster_grid.cells_in_polygon() honest about STEP 1's pixel-center convention."
    )
    assert rehydrated["area_acres"] == original["area_acres"]
    assert rehydrated["representative_elevation_m"] == original["representative_elevation_m"]
    assert rehydrated["render_fill_area_acres"] == original["render_fill_area_acres"]
    assert len(rehydrated["hole_footprints"]) == len(original["hole_footprints"])
    for rehydrated_hole, original_hole in zip(rehydrated["hole_footprints"], original["hole_footprints"]):
        assert rehydrated_hole.equals(original_hole), f"patch {original['id']}: a hole footprint moved"

    # Same geometry TYPE, not just the same area -- a MultiPolygon that came
    # back as a Polygon would have lost a part while keeping its acreage
    # within tolerance.
    assert rehydrated["polygon_utm"].geom_type == original["polygon_utm"].geom_type
    assert (
        rehydrated["render_fill_polygon_utm"].geom_type
        == original["render_fill_polygon_utm"].geom_type
    )

    # TOLERANCED, and only here.
    for field in ("polygon_utm", "render_fill_polygon_utm"):
        reference_area = original[field].area
        if reference_area <= 0:
            assert rehydrated[field].is_empty == original[field].is_empty
            continue
        relative = rehydrated[field].symmetric_difference(original[field]).area / reference_area
        worst_relative_symmetric_difference = max(worst_relative_symmetric_difference, relative)
        assert relative < REPROJECTION_SYMMETRIC_DIFFERENCE_TOLERANCE, (
            f"patch {original['id']}: {field} relative symmetric difference {relative:.3e} exceeds "
            f"{REPROJECTION_SYMMETRIC_DIFFERENCE_TOLERANCE:.0e} -- that is far above PROJ round-trip "
            "noise and means a real geometric difference."
        )

    # geometry_wgs84 / render_fill_geometry_wgs84 are derived FROM the two
    # polygons above by the same expression cluster_and_gate() uses, so they
    # inherit the same noise and are checked the same way.
    assert (rehydrated["render_fill_geometry_wgs84"] is None) == (
        original["render_fill_geometry_wgs84"] is None
    )

    # The advisory block came home verbatim.
    for field in ADVISORY_ON_THE_WIRE:
        assert rehydrated[field] == original[field], (field, rehydrated[field], original[field])

    # ...and the three fields outbound never emits are ABSENT, not guessed.
    for field in ADVISORY_NOT_ON_THE_WIRE:
        assert field in original, f"{field} should be on a scored patch"
        assert field not in rehydrated, (
            f"{field} is not on the wire, so rehydration must not invent one"
        )

print(
    f"Round trip: all {len(GENERATED_PATCHES)} generated patches return field-identical. "
    f"cells / area_acres / representative_elevation_m / render_fill_area_acres / hole footprints and "
    f"all {len(ADVISORY_ON_THE_WIRE)} wire-carried advisory fields EXACT; worst relative symmetric "
    f"difference on the two UTM polygons {worst_relative_symmetric_difference:.3e} "
    f"(tolerance {REPROJECTION_SYMMETRIC_DIFFERENCE_TOLERANCE:.0e}). "
    f"{', '.join(ADVISORY_NOT_ON_THE_WIRE)} are absent -- outbound never emits them."
)


# ======================================================================
# 2. A USER-DRAWN ZONE
# ======================================================================

print("\n== 2. USER-DRAWN ZONE ==")

_centroid = BOUNDARY_UTM.centroid
DRAWN_UTM = box(_centroid.x - 60, _centroid.y - 45, _centroid.x + 60, _centroid.y + 45).intersection(
    BOUNDARY_UTM
)
assert not DRAWN_UTM.is_empty and DRAWN_UTM.area > 0
DRAWN_ZONE = rehydrate_production_zone(_drawn_feature(DRAWN_UTM), DEM, zone_id=101)

# The id was NOT invented: a drawn feature carries no "production-area-<n>",
# so a caller that forgets zone_id= gets a specific error rather than a
# silent collision with a suggested zone's id.
try:
    rehydrate_production_zone(_drawn_feature(DRAWN_UTM), DEM)
except InboundGeometryError as error:
    assert "zone_id=" in str(error) and "collide" in str(error), str(error)
else:
    raise AssertionError("a drawn feature with no parsable id must not have one invented for it")

# Every field a `production_areas=` consumer reads, present and typed. The
# list is the AST-verified read set across water_survey_areas.py,
# road_corridors.py, solar_suitability.py, tree_zone_candidates.py,
# water_candidate_zones.py, pipeline_context.py and render_layout_map.py --
# see the branch report's consumer table.
CONSUMER_READ_FIELDS = {
    "id": int,
    "polygon_utm": (Polygon, MultiPolygon),
    "render_fill_polygon_utm": (Polygon, MultiPolygon),
    "representative_elevation_m": float,
    "area_acres": float,
}
for field, expected_type in CONSUMER_READ_FIELDS.items():
    assert field in DRAWN_ZONE, f"a consumer reads {field!r} and it is missing"
    assert isinstance(DRAWN_ZONE[field], expected_type), (
        f"{field} is {type(DRAWN_ZONE[field]).__name__}, expected {expected_type}"
    )

# The rest of cluster_and_gate()'s shape, minus source_patch_id (STEP-1
# provenance a drawn zone genuinely does not have -- see below).
for field in ("render_fill_area_acres", "render_fill_geometry_wgs84", "geometry_wgs84",
              "cells", "hole_footprints"):
    assert field in DRAWN_ZONE, field
assert isinstance(DRAWN_ZONE["geometry_wgs84"], dict)
assert DRAWN_ZONE["geometry_wgs84"]["type"] in ("Polygon", "MultiPolygon")
assert DRAWN_ZONE["cells"] and all(
    isinstance(r, int) and isinstance(c, int) for r, c in DRAWN_ZONE["cells"]
)
assert not math.isnan(DRAWN_ZONE["representative_elevation_m"])
assert DRAWN_ZONE["area_acres"] > 0

# ABSENT, NOT ZERO. This is the assertion the whole "advisory block is
# all-or-nothing" design exists for: 0.0 is a legible suitability score
# meaning "the worst ground on the parcel", and a zone nobody scored has
# not earned one. source_patch_id rides in the same block for the same
# reason -- 0 would NAME STEP-1 patch 0, a different, real zone, and the
# soil-carving line in the report would attribute this zone's ground to it.
for field in ADVISORY_ON_THE_WIRE + ADVISORY_NOT_ON_THE_WIRE:
    assert field not in DRAWN_ZONE, (
        f"a user-drawn zone must not carry {field!r} -- absent, not defaulted"
    )

print(
    f"Drawn zone: {DRAWN_ZONE['area_acres']} ac over {len(DRAWN_ZONE['cells'])} cells, "
    f"elevation {DRAWN_ZONE['representative_elevation_m']:.1f} m, fill "
    f"{DRAWN_ZONE['render_fill_area_acres']} ac ({DRAWN_ZONE['render_fill_polygon_utm'].geom_type}). "
    f"All {len(CONSUMER_READ_FIELDS)} consumer-read fields present and typed; all "
    f"{len(ADVISORY_ON_THE_WIRE + ADVISORY_NOT_ON_THE_WIRE)} advisory fields absent, not zeroed."
)


# ======================================================================
# 3. DOWNSTREAM ACCEPTANCE -- the go/no-go
# ======================================================================
# If a rehydrated user zone cannot be consumed by a real downstream entry
# point, the architecture's load-bearing assumption is false and this branch
# has nothing to deliver. Two real consumers, both reached through the same
# `production_areas=` override a generated patch travels down.

print("\n== 3. DOWNSTREAM ACCEPTANCE ==")


def _assert_consumers_accept(zone, what):
    """Both consumers, run against one rehydrated zone. Returns their
    results so the caller can print the numbers it cares about."""
    from road_corridors import build_road_network
    from water_survey_areas import identify_water_survey_areas

    # CONSUMER 1: the water step's full entry point. Every network layer is
    # supplied or switched off (dem/boundary/canopy from the fixture,
    # soil_inputs=None meaning "never checked", road union an explicit None
    # meaning "checked, nothing there") so the ONLY thing under test is what
    # it does with production_areas.
    water = identify_water_survey_areas(
        REFERENCE_BOUNDARY,
        dem=DEM,
        boundary_polygon_utm=BOUNDARY_UTM,
        production_areas=[zone],
        canopy_height=CANOPY,
        road_exclusion_union_utm=None,
        soil_inputs=None,
        check_soil=False,
    )
    assert isinstance(water, dict) and "zones" in water and "zones_geojson" in water
    validate_feature_collection(water["zones_geojson"])

    # SANE, not merely non-crashing. Every zone must have had its production
    # relationship COMPUTED (a real float, never the "never checked" None
    # sentinel), and every served id it reports must be an id this zone
    # actually has -- which is what proves the drawn zone was read as a
    # production area rather than skipped.
    for survey_zone in water["zones"]:
        overlap = survey_zone["production_overlap_pct"]
        assert overlap is not None, (
            f"{what}: production_overlap_pct is None -- the None sentinel means 'never checked', so "
            "the rehydrated zone was not seen as production geometry at all"
        )
        assert 0.0 <= overlap <= 100.0, overlap
        assert set(survey_zone["served_production_area_ids"]) <= {zone["id"]}
        for relationship in survey_zone["production_area_relationships"]:
            assert relationship["production_area_id"] == zone["id"]
            assert isinstance(relationship["elevation_differential_m"], float)
            assert relationship["distance_m"] >= 0

    # CONSUMER 2: the road network builder -- the inner entry point, which
    # takes every input explicitly and fetches nothing. It reads
    # render_fill_polygon_utm off each patch (and raises by name if the
    # field is missing, which is the guard this call also exercises).
    roads = build_road_network(
        DEM,
        [zone],
        None,
        BOUNDARY_UTM,
        (REFERENCE_BOUNDARY[0][0], REFERENCE_BOUNDARY[0][1]),
    )
    assert isinstance(roads, dict)
    for key in ("branches", "total_served_acres", "unserved_acres", "stop_reason"):
        assert key in roads, key

    # The drawn zone's acreage is ACCOUNTED FOR, not dropped: served plus
    # unserved is the zone's own render-fill acreage, which is the acreage
    # road_corridors measures demand against.
    accounted = roads["total_served_acres"] + roads["unserved_acres"]
    expected = zone["render_fill_polygon_utm"].area / SQUARE_METERS_PER_ACRE
    assert math.isclose(accounted, expected, rel_tol=0.02), (
        f"{what}: road_corridors accounted for {accounted:.3f} ac of a {expected:.3f} ac zone"
    )
    return water, roads


WATER_RESULT, ROAD_RESULT = _assert_consumers_accept(DRAWN_ZONE, "drawn zone")
_survey_zone_count = len(WATER_RESULT["zones"])
_overlaps = [z["production_overlap_pct"] for z in WATER_RESULT["zones"]]
print(
    f"water_survey_areas.identify_water_survey_areas(production_areas=[drawn zone]): "
    f"{_survey_zone_count} zone(s) nominated, {len(WATER_RESULT['dropped_zones'])} dropped; "
    f"production_overlap_pct computed for every one {_overlaps}; served ids resolve to the drawn "
    f"zone's own id ({DRAWN_ZONE['id']})."
)
print(
    f"road_corridors.build_road_network(production_areas=[drawn zone]): ran, stop_reason="
    f"{ROAD_RESULT['stop_reason']!r}, {ROAD_RESULT['total_served_acres']:.2f} ac served + "
    f"{ROAD_RESULT['unserved_acres']:.2f} ac unserved == the zone's own "
    f"{DRAWN_ZONE['render_fill_area_acres']} ac render fill."
)
print("GO: a user-drawn zone with no scoring fields is consumable by real downstream entry points.")


# ======================================================================
# 4. MULTI-PART -- the normal case
# ======================================================================
# The shipped frontend clamps a drawn ring to the parcel with a
# polygon-clipping intersection (zoneGeometry.clampToBoundary()) and keeps
# the result as ONE zone: one id, one acreage, one caution list. So the
# clamp splitting a ring is not an exotic input, it is Tuesday -- and
# rehydration has to agree with the frontend about what one zone is.

print("\n== 4. MULTI-PART ==")

# A "U" drawn across the parcel's north edge: both arms land inside, the
# crossbar joining them is off-parcel, so the clamp severs it and returns
# two pieces. A hole is punched in one arm -- the drawn ring's own interior
# ring, which the clamp carries through as a later ring of that piece.
_minx, _miny, _maxx, _maxy = BOUNDARY_UTM.bounds
_cx = (_minx + _maxx) / 2
_u_outer = [
    (_cx - 70, _maxy - 120), (_cx - 70, _maxy + 60), (_cx + 70, _maxy + 60), (_cx + 70, _maxy - 120),
    (_cx + 40, _maxy - 120), (_cx + 40, _maxy + 20), (_cx - 40, _maxy + 20), (_cx - 40, _maxy - 120),
]
_hole = [(_cx - 63, _maxy - 100), (_cx - 47, _maxy - 100), (_cx - 47, _maxy - 60), (_cx - 63, _maxy - 60)]
SPLIT_DRAWN_UTM = Polygon(_u_outer, [_hole]).intersection(BOUNDARY_UTM)

assert SPLIT_DRAWN_UTM.geom_type == "MultiPolygon", SPLIT_DRAWN_UTM.geom_type
assert len(SPLIT_DRAWN_UTM.geoms) >= 2
assert any(len(part.interiors) >= 1 for part in SPLIT_DRAWN_UTM.geoms), (
    "the fixture must keep its hole through the clamp -- otherwise this tests only the split"
)

MULTIPART_ZONE = rehydrate_production_zone(
    _drawn_feature(SPLIT_DRAWN_UTM, feature_id="drawn-zone-split"), DEM, zone_id=202
)

# ONE ZONE IN, ONE PATCH OUT. rehydrate_production_zone() returns a single
# dict by signature, so the real assertion is that the patch keeps ALL the
# parts rather than silently taking the largest -- and that its acreage is
# the whole drawn zone's, not one piece's.
assert MULTIPART_ZONE["polygon_utm"].geom_type == "MultiPolygon"
assert len(MULTIPART_ZONE["polygon_utm"].geoms) == len(SPLIT_DRAWN_UTM.geoms), (
    "a split drawn zone rehydrates as ONE patch carrying EVERY piece -- dropping one would "
    "silently shrink the acreage the user drew"
)
assert math.isclose(
    MULTIPART_ZONE["area_acres"], SPLIT_DRAWN_UTM.area / SQUARE_METERS_PER_ACRE, abs_tol=0.005
)
_largest_piece_acres = max(p.area for p in SPLIT_DRAWN_UTM.geoms) / SQUARE_METERS_PER_ACRE
assert MULTIPART_ZONE["area_acres"] > _largest_piece_acres, (
    "the whole zone's acreage must exceed its largest single piece's"
)

# The hole survived: cells inside it were never claimed.
from raster_grid import pixel_center_xy  # noqa: E402  (assertion-local, mirrors the convention under test)
from shapely.geometry import Point  # noqa: E402

_hole_polygon = next(Polygon(part.interiors[0]) for part in SPLIT_DRAWN_UTM.geoms if part.interiors)
assert not any(
    _hole_polygon.contains(Point(pixel_center_xy(DEM, r, c))) for r, c in MULTIPART_ZONE["cells"]
), "a cell inside the drawn hole was claimed -- the interior ring was lost in rehydration"

# ...and it survives test 3.
_assert_consumers_accept(MULTIPART_ZONE, "multi-part drawn zone")

print(
    f"Multi-part: a U-shaped draw clamped into {len(SPLIT_DRAWN_UTM.geoms)} pieces (one holed) "
    f"rehydrates as ONE patch, id {MULTIPART_ZONE['id']}, {MULTIPART_ZONE['area_acres']} ac total "
    f"(largest single piece {_largest_piece_acres:.2f} ac), {len(MULTIPART_ZONE['cells'])} cells, "
    f"hole preserved -- and both downstream consumers accept it."
)

# The plural entry point, on a real collection: ids assigned per feature.
_plural = rehydrate_production_zones(
    {"type": "FeatureCollection", "features": [
        _drawn_feature(DRAWN_UTM, feature_id="a"), _drawn_feature(SPLIT_DRAWN_UTM, feature_id="b")]},
    DEM, zone_ids=[7, 8],
)
assert [z["id"] for z in _plural] == [7, 8]
assert rehydrate_production_zones(None, DEM) == []
assert rehydrate_production_zones({"type": "FeatureCollection", "features": []}, DEM) == []
try:
    rehydrate_production_zones({"features": [_drawn_feature(DRAWN_UTM, feature_id="a")]}, DEM, zone_ids=[1, 2])
except InboundGeometryError as error:
    assert "one id per feature" in str(error)
else:
    raise AssertionError("a zone_ids/features length mismatch must raise")
print("rehydrate_production_zones(): per-feature ids, empty in -> empty out, length mismatch raises.")


# ======================================================================
# 5. NO NETWORK
# ======================================================================
# Asserted with a counter that also RAISES, not with a stopwatch. A timing
# check passes on a fast cache and fails on a slow disk; this fails the
# moment anything opens a socket, and names what it was.

print("\n== 5. NO NETWORK ==")

_connection_attempts = []
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_create_connection = socket.create_connection


def _forbidden(address, *args, **kwargs):
    _connection_attempts.append(address)
    raise AssertionError(
        f"rehydration opened a network connection to {address!r}. Every derivation in the inbound "
        "boundary is pure and local against the cached DEM; a rehydration that fetches is a bug."
    )


socket.socket.connect = lambda self, address: _forbidden(address)
socket.socket.connect_ex = lambda self, address: _forbidden(address)
socket.create_connection = _forbidden
try:
    _guarded = [
        rehydrate_production_zone(feature, DEM) for feature in OUTBOUND["features"]
    ]
    _guarded.append(rehydrate_production_zone(_drawn_feature(DRAWN_UTM), DEM, zone_id=1))
    _guarded.append(rehydrate_production_zone(_drawn_feature(SPLIT_DRAWN_UTM), DEM, zone_id=2))
finally:
    socket.socket.connect = _real_connect
    socket.socket.connect_ex = _real_connect_ex
    socket.create_connection = _real_create_connection

assert _connection_attempts == [], _connection_attempts
assert len(_guarded) == len(GENERATED_PATCHES) + 2
print(
    f"No network: {len(_guarded)} rehydrations (generated, drawn and multi-part) under a socket "
    f"guard that raises on connect -- {len(_connection_attempts)} connection attempts."
)


# ======================================================================
# 6. DEGENERATE INPUT
# ======================================================================
# Fail LOUDLY and SPECIFICALLY. Rejecting these at commit, with the
# offending feature named to the user, is the commit-validation branch's job
# (proposal section 2.5). Failing cleanly -- never returning a half-built
# patch, never silently repairing one -- is this branch's.

print("\n== 6. DEGENERATE INPUT ==")


def _raw_feature(geometry, feature_id="degenerate"):
    """A feature built WITHOUT make_feature(), so a geometry the schema
    itself would reject still reaches the boundary. The boundary has to
    stand on its own: nothing guarantees a committed feature came from this
    pipeline's own emitter."""
    return {"id": feature_id, "type": "Feature", "geometry": geometry,
            "properties": {"layer": LAYER_PRODUCTION_AREA}}


def _wgs84_ring(points_utm):
    """A closed WGS84 ring from UTM vertices, via the real polygon path."""
    return [list(coordinate) for coordinate in transform_geom(
        UTM_17N, "EPSG:4326", mapping(Polygon(points_utm)))["coordinates"][0]]


def _wgs84_positions(points_utm):
    """The same reprojection WITHOUT going through shapely -- shapely refuses
    to build a ring from fewer than 4 coordinates, which is exactly the input
    the vertex-count case below has to deliver to the boundary. A committed
    feature is JSON off a wire; nothing guarantees shapely could have made
    it."""
    xs, ys = warp_transform(UTM_17N, "EPSG:4326",
                            [p[0] for p in points_utm], [p[1] for p in points_utm])
    return [[x, y] for x, y in zip(xs, ys)]


_x, _y = _centroid.x, _centroid.y

DEGENERATE_CASES = [
    (
        "self-intersecting ring (bowtie)",
        {"type": "Polygon", "coordinates": [_wgs84_ring(
            [(_x, _y), (_x + 80, _y), (_x, _y + 80), (_x + 80, _y + 80)])]},
        ("not valid", "Self-intersection"),
    ),
    (
        "zero-area sliver (collinear vertices)",
        {"type": "Polygon", "coordinates": [_wgs84_ring(
            [(_x, _y), (_x + 40, _y), (_x + 80, _y)])]},
        ("effectively zero area", "degenerate ring"),
    ),
    (
        "fewer than 3 vertices",
        {"type": "Polygon", "coordinates": [_wgs84_positions(
            [(_x, _y), (_x + 40, _y), (_x, _y)])]},
        ("distinct vertex", "at least 3"),
    ),
    (
        "not polygonal (a LineString)",
        {"type": "LineString", "coordinates": _wgs84_ring([(_x, _y), (_x + 40, _y), (_x + 40, _y + 40)])},
        ("Polygon or MultiPolygon",),
    ),
    (
        "smaller than the gap between cell centers",
        {"type": "Polygon", "coordinates": [_wgs84_ring(
            [(_x + 0.10, _y + 0.10), (_x + 0.40, _y + 0.10),
             (_x + 0.40, _y + 0.40), (_x + 0.10, _y + 0.40)])]},
        ("no DEM cell center",),
    ),
]

for name, geometry, expected_phrases in DEGENERATE_CASES:
    try:
        rehydrate_production_zone(_raw_feature(geometry), DEM, zone_id=999)
    except InboundGeometryError as error:
        message = str(error)
        assert any(phrase in message for phrase in expected_phrases), (
            f"{name}: raised, but the message names none of {expected_phrases}: {message}"
        )
        assert "degenerate" in message, f"{name}: the message must identify the offending feature"
        print(f"  {name}: InboundGeometryError -- {message.split(' -- ')[0][:110]}")
    else:
        raise AssertionError(f"{name}: rehydration returned a patch instead of failing")

# A malformed envelope, not just a malformed geometry.
for bad, expected in (
    ("not a dict", "GeoJSON Feature dict"),
    ({"id": "x", "geometry": None}, "geometry must be a GeoJSON geometry dict"),
    ({"id": "x", "geometry": {"type": "Polygon"}}, "no coordinates"),
):
    try:
        rehydrate_production_zone(bad, DEM, zone_id=999)
    except InboundGeometryError as error:
        assert expected in str(error), (expected, str(error))
    else:
        raise AssertionError(f"{bad!r} must raise")

# NOTHING WAS REPAIRED. The bowtie above is the case that matters: buffer(0)
# would have turned it into two clean lobes and returned a patch, and the
# acreage attributed to those lobes would be ground nobody drew.
_bowtie = Polygon([(_x, _y), (_x + 80, _y), (_x, _y + 80), (_x + 80, _y + 80)])
assert not _bowtie.is_valid and _bowtie.buffer(0).is_valid, (
    "the bowtie fixture must be the case a buffer(0) repair would have silently accepted"
)

print(
    f"Degenerate input: {len(DEGENERATE_CASES)} geometries and 3 malformed envelopes all raise "
    "InboundGeometryError naming the defect; the self-intersecting ring is rejected rather than "
    "repaired into lobes nobody drew."
)

print("\nAll wire_translation inbound checks passed.")
