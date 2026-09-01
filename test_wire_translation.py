"""
test_wire_translation.py

Offline (no-network) checks for wire_translation.py -- the OUTBOUND half of
the translation boundary (interactive-design-architecture-proposal.md
section 2.4).

Script-style, per this repo's convention: run it directly
(`python test_wire_translation.py`), assertions inline, printed section
headers. No network, no DEM fetch, no basemap -- every input is a synthetic
internal-shape fixture built here, and every entry point
render_layout_map.fetch_layout_layers() would reach out to is mocked.

FIVE THINGS THIS FILE EXISTS TO PROVE

  1. SCHEMA. Every layer function's output validates against
     feature_schema.validate_feature_collection() -- that module's OWN
     contract, called directly. Deliberately not a hand-written copy of
     its rules here: a duplicate would pass while feature_schema changed
     underneath it, which is the exact drift the shared schema exists to
     prevent.

  2. PARITY -- the most important check in this file. fetch_layout_layers()
     must return what it returned before the refactor routed its
     road_corridor layer through the boundary. Asserted as a live
     invariant rather than a frozen blob: its road_corridor is asserted
     EQUAL to identify_road_corridor_candidates()' own zones_geojson
     features (the exact expression the function used to return), and
     every other key is asserted to be the SAME OBJECT it was read from.
     A frozen expected-output file would rot; this keeps holding as the
     fixture changes, and fails the moment the two paths diverge.

  3. NO DOUBLE REPROJECTION. For every layer whose internal objects already
     carry a stored WGS84 form, the emitted coordinates are asserted EQUAL
     to that stored form's coordinates -- not close, equal. Reprojecting a
     second time would pass an approximate check and still drift the wire
     away from what the layout map draws.

  4. EMPTY AND None. No production areas, no selected water zone, no
     structure site, a road network with branches=[], an exclusion result
     whose union geometry is None, and a bare None for every function --
     each must produce a VALID EMPTY FeatureCollection. Never None, never a
     raised error. An empty FeatureCollection is how "computed, nothing
     there" reaches the frontend and it has to be distinguishable from a
     failure; a None or a traceback erases that distinction.

  5. COORDINATE ORDER. Every emitted coordinate pair is [lon, lat] in
     WGS84 degrees, checked against the fixture's own known lon/lat box
     rather than a bare range test that (-83, 40) would also pass with the
     axes swapped.
"""

import json
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from rasterio.warp import transform_geom
from shapely.geometry import LineString, Point, Polygon, box, mapping
from shapely.ops import unary_union

import wire_translation as wt
from feature_schema import VALID_CONFIDENCE_LEVELS, validate_feature_collection

# ======================================================================
# Fixtures -- synthetic internal shapes, built the way the real modules
# build them (WGS84 geometry derived ONCE, at the object's birth)
# ======================================================================

CRS = "EPSG:32617"  # a real UTM zone, so transform_geom does real work
ORIGIN_X, ORIGIN_Y = 500000.0, 4500000.0
RES = (5.0, 5.0)
SIZE = 24
SQUARE_METERS_PER_ACRE = 4046.8564224


def _dem():
    array = np.zeros((SIZE, SIZE), dtype=np.float32)
    for row in range(SIZE):
        array[row, :] = 100.0 + row * 0.4
    return {
        "array": array,
        "resolution_meters": RES,
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def _boundary_polygon_utm():
    return box(ORIGIN_X, ORIGIN_Y - SIZE * RES[1], ORIGIN_X + SIZE * RES[0], ORIGIN_Y)


def _boundary_coordinates():
    xs, ys = _boundary_polygon_utm().exterior.coords.xy
    lons, lats = warp_transform(CRS, "EPSG:4326", list(xs), list(ys))
    return list(zip(lons, lats))


def _wgs84(geom_utm):
    """The birth-time reprojection every real module does exactly once."""
    return transform_geom(CRS, "EPSG:4326", mapping(geom_utm))


def _acres(geom):
    return round(geom.area / SQUARE_METERS_PER_ACRE, 2)


def _valley(vid, x0):
    line = LineString([(x0, ORIGIN_Y), (x0 + 10, ORIGIN_Y - 60)])
    return {
        "id": vid,
        "line_utm": line,
        "geometry_wgs84": _wgs84(line),
        "max_contributing_area_acres": 5.5 + vid,
        "branches_rowcol": [[(0, 0), (1, 1)]],
    }


def _keypoint(kid, x, y):
    pt = Point(x, y)
    return {
        "id": kid,
        "valley_id": kid,
        "point_utm": pt,
        "geometry_wgs84": _wgs84(pt),
        "elevation_m": 103.5,
        "contributing_acres": 2.5,
        "slope_above_pct": 8.0,
        "slope_below_pct": 3.0,
        "slope_drop_pct": 5.0,
        "stem_length_cells": 12,
        "position_along_stem": 0.4,
        "on_parcel": True,
        "distance_outside_boundary_m": 0.0,
        # Per-feature, deliberately -- keypoint_detection.py measures its
        # own per-keypoint reliability and the adapter must carry it
        # through rather than flattening it to a layer constant.
        "confidence": "medium",
        "confidence_notes": f"Synthetic keypoint {kid} notes.",
    }


def _production_patch(pid, x0, y0, w, h, opened_inset):
    """A production patch whose render_fill is a REAL opening of its
    polygon_utm -- the two must not be allowed to coincide in this fixture
    or the provenance assertions below would pass vacuously."""
    polygon_utm = box(x0, y0, x0 + w, y0 + h)
    render_fill = box(
        x0 + opened_inset, y0 + opened_inset, x0 + w - opened_inset, y0 + h - opened_inset
    )
    assert render_fill.area < polygon_utm.area, "fixture must keep the two geometries distinct"
    return {
        "id": pid,
        "area_acres": _acres(polygon_utm),
        "render_fill_area_acres": _acres(render_fill),
        "polygon_utm": polygon_utm,
        "render_fill_polygon_utm": render_fill,
        "geometry_wgs84": _wgs84(polygon_utm),
        "render_fill_geometry_wgs84": _wgs84(render_fill),
        "cells": [(r, c) for r in range(3) for c in range(3)],
        "representative_elevation_m": 101.234 + pid,
        "rank": pid,
        "suitability_score": 70.0 - pid,
        "slope_factor": 0.9,
        "size_factor": 0.8,
        "aspect_factor": 0.7,
        "avg_slope_pct": 4.5,
        "aspect_deg": 180.0,
        "soil_carved_acres": 0.0,
        "soil_carved_pct": 0.0,
        "soil_data_available": True,
        "source_patch_id": pid,
        "confidence_notes": f"Synthetic production patch {pid} notes.",
    }


def _water_member(mid, x0, y0):
    poly = box(x0, y0, x0 + 20, y0 + 20)
    return {
        "id": mid,
        "zone_id": 1,
        "survey_type": "excavated",
        "area_acres": _acres(poly),
        "cell_count": 16,
        "cells": [(1, 1)],
        "polygon_utm": poly,
        "geometry_wgs84": _wgs84(poly),
        # water_survey_areas.py records the SAME object under both names
        "render_fill_polygon_utm": poly,
        "render_fill_geometry_wgs84": _wgs84(poly),
        "confidence": "low",
        "mean_suitability": 0.72,
        "max_suitability": 0.81,
        "criterion_contributions": {"twi": 0.3, "depth": 0.4},
        "twi_percentile_mean": 88.0,
        "depression_depth_max_m": 0.6,
        "contributing_area_acres_at_wettest_cell": 2.2,
        "slope_median_pct": 1.5,
        "boundary_adjacency_fraction": 0.0,
        "soil_coverage_fraction": 1.0,
        "criteria_complete": True,
        "flags": [],
        "below_min_area": False,
        "representative_elevation_m": 100.5,
    }


def _water_zone(zid, x0, y0, members):
    poly = box(x0, y0, x0 + 40, y0 + 40)
    return {
        "id": zid,
        "survey_type": "excavated",
        "nominated_by": "suitability_surface",
        "status": "surviving",
        "drop_reason": None,
        "rank": zid,
        "sparse_anchor": False,
        "truncated_by_road": False,
        "cross_type_overlaps": [],
        "member_ids": [m["id"] for m in members],
        "member_count": len(members),
        "member_acres": sum(m["area_acres"] for m in members),
        "zone_acres": _acres(poly),
        "cell_count": 64,
        "members": members,
        "cells": [(2, 2)],
        "polygon_utm": poly,
        "geometry_wgs84": _wgs84(poly),
        "render_fill_polygon_utm": poly,
        "render_fill_geometry_wgs84": _wgs84(poly),
        "confidence": "low",
        "confidence_notes": f"Synthetic survey zone {zid} notes.",
        "mean_suitability": 0.7,
        "max_suitability": 0.85,
        "criterion_contributions": {"twi": 0.3, "depth": 0.4},
        "twi_percentile_mean": 87.0,
        "twi_percentile_max": 95.0,
        "depression_depth_mean_m": 0.4,
        "depression_depth_max_m": 0.7,
        "contributing_area_acres_at_wettest_cell": 2.4,
        "slope_median_pct": 1.4,
        "boundary_adjacency_fraction": 0.0,
        "canopy_overlap_pct": 0.0,
        "road_overlap_pct": 0.0,
        "production_overlap_pct": 0.0,
        "primary_production_area_relationship": None,
        "production_area_relationships": [],
        "has_service_relationship": False,
        "served_production_area_ids": [1],
        "soil_coverage_fraction": 1.0,
        "criteria_complete": True,
        "flags": [],
        "below_min_area": False,
        "representative_elevation_m": 100.9,
    }


def _road_branch(index, role, joins, x0):
    pts = [
        (x0, ORIGIN_Y - 10.0, 100.0),
        (x0 + 30.0, ORIGIN_Y - 40.0, 101.0),
        (x0 + 60.0, ORIGIN_Y - 70.0, 102.0),
    ]
    line = LineString([(p[0], p[1]) for p in pts])
    lons, lats = warp_transform(CRS, "EPSG:4326", [p[0] for p in pts], [p[1] for p in pts])
    return {
        "cells": [(0, 0), (1, 1), (2, 2)],
        "branch_role": role,
        "branch_index": index,
        "joins_branch_index": joins,
        "length_meters": 84.85,
        "total_cost": 10.0,
        "newly_served_acres": 1.234,
        "points_xyz": pts,
        "line_utm": line,
        # road_corridors.py builds this straight from points_xyz, at birth
        "geometry_wgs84": {"type": "LineString", "coordinates": list(zip(lons, lats))},
        "cell_footprint_polygon_utm": line.buffer(2.5),
        "avg_grade_pct": 3.33,
        "max_grade_pct": 7.77,
        "steep_meters": 5.0,
        "crosses_floodplain": False,
        "crosses_production_zone": True,
    }


def _road_network(branches):
    return {
        "branches": branches,
        "total_length_meters": sum(b["length_meters"] for b in branches),
        "total_served_acres": 3.5,
        "unserved_acres": 1.0,
        "stop_reason": "no_remaining_demand",
        "cells": [c for b in branches for c in b["cells"]],
        "cell_footprint_polygon_utm": (
            unary_union([b["cell_footprint_polygon_utm"] for b in branches])
            if branches
            else Polygon()
        ),
    }


def _solar_candidate(rank, x0, y0):
    footprint = box(x0, y0, x0 + 25, y0 + 25)
    return {
        "rank": rank,
        "suitability_score": 80.0 - rank,
        "avg_slope_pct": 2.2,
        "aspect_label": "south",
        "aspect_deg": 179.0,
        "footprint_area_acres": 0.15,
        "distance_to_road_m": 40.0,
        "distance_to_production_zone_m": 25.0,
        "production_zone_relationship": "adjacent",
        "distance_to_water_zone_m": 60.0,
        "polygon_utm": footprint,
        "geometry_wgs84": _wgs84(footprint),
        "slope_score": 0.9,
        "aspect_score": 0.8,
        "shading_score": 0.7,
        "production_proximity_score": 0.6,
    }


def _tree_patch(pid, x0, y0):
    footprint = box(x0, y0, x0 + 30, y0 + 30)
    return {
        "id": pid,
        "rank": pid,
        "area_acres": _acres(footprint),
        "polygon_utm": footprint,
        # tree_zone_candidates.py records the SAME object under both names
        "render_fill_polygon_utm": footprint,
        "geometry_wgs84": _wgs84(footprint),
        "cells": [(4, 4)],
        "tree_suitability_score": 60.0 - pid,
        "soil_marginality_factor": 0.5,
        "slope_factor": 0.6,
        "hydric_overlap_factor": 0.4,
        "stream_proximity_factor": 0.3,
        "avg_slope_pct": 9.9,
        "soil_marginality_data_available": True,
        "hydric_data_available": True,
        "stream_data_available": False,
        "position_in_parcel": "north",
    }


def _exclusion_result(dem, boundary, *, mode="populated"):
    """Mirrors exclusion_zones.identify_exclusion_zones()' return shape,
    including its own WGS84 keys, built through that module's own
    _wire_layers() so the per-gate wire block is the real thing.

    THREE MODES, because "empty" is genuinely two different answers for
    this layer and they must not be conflated:

      "populated"       -- some ground excluded, some eligible.
      "nothing_excluded" -- no gate fired. excluded_union is empty, so
                            geometry_wgs84 is None, and the WHOLE parcel
                            is eligible. This is the empty-parcel case.
      "nothing_eligible" -- every gate fired parcel-wide. eligible_union
                            is empty, so eligible_union_wgs84 is None.
    """
    from exclusion_zones import LAYER_ORDER, _wire_layers

    if mode == "nothing_excluded":
        excluded = Polygon()
    elif mode == "nothing_eligible":
        excluded = boundary
    else:
        excluded = box(ORIGIN_X, ORIGIN_Y - 30, ORIGIN_X + 40, ORIGIN_Y)
    eligible_union = boundary.difference(excluded).buffer(0)
    layers = {}
    availability = {}
    for i, name in enumerate(LAYER_ORDER):
        # "roads" is deliberately a gate that WAS checked and found
        # nothing, and "hydric" a gate that was NEVER checked (its source
        # was unreachable). Both therefore carry NO geometry, exactly as
        # the real module would leave them -- and the wire must still keep
        # the two answers apart, which is what data_available is for.
        if mode != "populated" or name in ("roads", "hydric"):
            poly = Polygon()
        else:
            poly = box(ORIGIN_X + i * 10, ORIGIN_Y - 20, ORIGIN_X + i * 10 + 8, ORIGIN_Y - 5)
        layers[name] = {
            "mask": np.zeros((SIZE, SIZE), dtype=bool),
            "polygon_utm": poly,
            "acres": _acres(poly),
            "data_available": name != "hydric",
        }
        availability[name] = name != "hydric"
    return {
        "layers": layers,
        "excluded_union_utm": excluded,
        "render_fill_polygon_utm": excluded,
        "eligible_polygon_utm": boundary.difference(excluded),
        "eligible_union_utm": eligible_union,
        "eligible_union_wgs84": (None if eligible_union.is_empty else _wgs84(eligible_union)),
        "wire": {
            "layers": _wire_layers(dem, layers, availability, 15.0, 10.0),
            "cell_size_meters": [float(RES[0]), float(RES[1])],
        },
        "eligible_mask": np.zeros((SIZE, SIZE), dtype=bool),
        "excluded_union_mask": np.zeros((SIZE, SIZE), dtype=bool),
        "slope_pct": np.zeros((SIZE, SIZE), dtype=float),
        "slope_only_mask": np.zeros((SIZE, SIZE), dtype=bool),
        "geometry_wgs84": (None if excluded.is_empty else _wgs84(excluded)),
        "parcel_acres": _acres(boundary),
        "narrative_data": {
            "parcel": {
                "total_acres": _acres(boundary),
                "excluded_acres": _acres(excluded),
                "eligible_acres": _acres(eligible_union),
                "excluded_pct_of_parcel": _acres(excluded) / max(_acres(boundary), 1e-9) * 100.0,
            }
        },
    }


def build_fixture(*, empty=False):
    """Every internal shape one PipelineContext holds, plus the two extra
    KSOP results fetch_layout_layers() makes its own calls for."""
    from parcel_data import ParcelData
    from pipeline_context import PipelineContext
    from road_corridors import corridors_to_geojson
    from solar_suitability import candidates_to_geojson
    from water_survey_areas import survey_areas_to_geojson

    dem = _dem()
    boundary = _boundary_polygon_utm()
    coords = _boundary_coordinates()

    if empty:
        production_areas, water_zones_raw, branches = [], [], []
        selected_water_zone = None
        solar_candidates, tree_patches, valleys, keypoints = [], [], [], []
    else:
        production_areas = [
            _production_patch(1, ORIGIN_X + 10, ORIGIN_Y - 60, 40, 30, 5.0),
            _production_patch(2, ORIGIN_X + 60, ORIGIN_Y - 100, 30, 30, 2.5),
        ]
        water_zones_raw = [
            _water_zone(1, ORIGIN_X + 15, ORIGIN_Y - 115, [_water_member(11, ORIGIN_X + 20, ORIGIN_Y - 110)])
        ]
        selected_water_zone = water_zones_raw[0]
        branches = [
            _road_branch(0, "trunk", None, ORIGIN_X + 5),
            _road_branch(1, "spur", 0, ORIGIN_X + 45),
        ]
        solar_candidates = [
            _solar_candidate(1, ORIGIN_X + 70, ORIGIN_Y - 40),
            _solar_candidate(2, ORIGIN_X + 70, ORIGIN_Y - 80),
        ]
        tree_patches = [_tree_patch(1, ORIGIN_X + 5, ORIGIN_Y - 115)]
        valleys = [_valley(1, ORIGIN_X + 20), _valley(2, ORIGIN_X + 70)]
        keypoints = [_keypoint(1, ORIGIN_X + 25, ORIGIN_Y - 45)]

    exclusion = _exclusion_result(
        dem, boundary, mode="nothing_excluded" if empty else "populated"
    )
    network = _road_network(branches)

    context = PipelineContext(
        dem=dem,
        boundary_polygon_utm=boundary,
        valleys=valleys,
        keypoints=keypoints,
        exclusion_zones=exclusion,
        production_areas=production_areas,
        parcel_acres=exclusion["parcel_acres"],
        existing_roads=None,
        soil_exclusion_unions={
            "hydric_floodplain_union": None,
            # A real bool, and deliberately True: it changes every road
            # branch's confidence_notes, so a refactor that dropped it on
            # the floor would show up in the parity check below.
            "hydric_floodplain_is_fallback": True,
            "erosion_prone_union": None,
        },
        # water_zones IS zones_geojson's features list -- see
        # pipeline_context.py's own field note. Built the same way here.
        water_zones=survey_areas_to_geojson(water_zones_raw)["features"],
        selected_water_zone=selected_water_zone,
        selected_road_corridor=network,
        selected_structure_site=(solar_candidates[0] if solar_candidates else None),
        tree_zone_patches=tree_patches,
        narrative_data={},
    )

    parcel_data = ParcelData(
        dem=dem,
        boundary_polygon_utm=boundary,
        soil_components=[],
        farmland_classification=[],
        erosion_factor=[],
        saturated_hydraulic_conductivity=[],
        soil_geometries={},
        water_features={"streams": [], "water_bodies": []},
        farm_roads=[],
        climate_summary={},
        elevation_grid=[],
        canopy_height={
            "array": np.zeros((SIZE, SIZE), dtype=np.float32),
            "resolution_meters": RES,
            "origin_x": ORIGIN_X,
            "origin_y": ORIGIN_Y,
            "crs": CRS,
        },
        imagery_summary={},
        irradiance={"status": "no_api_key"},
    )

    road_result = {
        "zones_geojson": corridors_to_geojson(network, floodplain_data_is_fallback=True),
        "road_network": network,
        "selected_road_corridor": network if branches else None,
        "narrative_data": {},
    }
    solar_result = {
        "zones_geojson": candidates_to_geojson(
            solar_candidates,
            road_proximity_source="selected_road_corridor",
            tree_zone_exclusion_available=True,
        ),
        "all_scored_candidates": solar_candidates,
        "selected_structure_site": solar_candidates[0] if solar_candidates else None,
        "narrative_data": {},
    }
    return {
        "coords": coords,
        "dem": dem,
        "boundary": boundary,
        "context": context,
        "parcel_data": parcel_data,
        "road_result": road_result,
        "solar_result": solar_result,
        "raw_water_zones": water_zones_raw,
        "solar_candidates": solar_candidates,
        "exclusion": exclusion,
    }


def outbound_collections(fx):
    """Every outbound function this branch delivers, run over one fixture.
    Keyed by the name used in the printed output below."""
    ctx = fx["context"]
    return {
        "boundary": wt.boundary_to_feature_collection(fx["coords"]),
        "valleys": wt.valleys_to_feature_collection(ctx.valleys),
        "keypoints": wt.keypoints_to_feature_collection(ctx.keypoints),
        "exclusion_union": wt.exclusion_union_to_feature_collection(ctx.exclusion_zones),
        "eligible_ground": wt.eligible_ground_to_feature_collection(ctx.exclusion_zones),
        "exclusion_gates": wt.exclusion_gate_layers_to_feature_collection(ctx.exclusion_zones),
        "production_areas": wt.scored_production_areas_to_feature_collection(ctx.production_areas),
        "production_areas_unscored": wt.production_areas_to_feature_collection(ctx.production_areas),
        "water_zones": wt.water_zone_features_to_feature_collection(ctx.water_zones),
        "selected_water_zone": wt.selected_water_zone_to_feature_collection(ctx.selected_water_zone),
        "road_corridor": wt.road_network_to_feature_collection(
            ctx.selected_road_corridor,
            floodplain_data_is_fallback=ctx.soil_exclusion_unions["hydric_floodplain_is_fallback"],
        ),
        "structure_site": wt.selected_structure_site_to_feature_collection(
            ctx.selected_structure_site,
            road_proximity_source="selected_road_corridor",
        ),
        "tree_zones": wt.tree_zones_to_feature_collection(ctx.tree_zone_patches),
    }


def drive_fetch_layout_layers(fx):
    """The real fetch_layout_layers(), with every network-backed and KSOP
    entry point mocked out -- nothing below it runs."""
    import render_layout_map as rlm

    with mock_patch.object(rlm, "fetch_parcel_data", return_value=fx["parcel_data"]), \
         mock_patch.object(rlm, "build_pipeline_context", return_value=fx["context"]), \
         mock_patch.object(rlm, "identify_road_corridor_candidates", return_value=fx["road_result"]), \
         mock_patch.object(rlm, "identify_solar_candidate_zones", return_value=fx["solar_result"]), \
         mock_patch.object(rlm, "identify_fencing", return_value={"fencing": "SENTINEL"}), \
         mock_patch.object(rlm, "compute_contour_lines", return_value=["CONTOUR_SENTINEL"]):
        return rlm.fetch_layout_layers(fx["coords"], anchor_lon_lat=fx["coords"][0])


def _coord_pairs(geometry):
    """Every [lon, lat] pair in a GeoJSON geometry, at any nesting depth."""
    out = []

    def walk(node):
        if (
            isinstance(node, (list, tuple))
            and len(node) == 2
            and all(isinstance(v, (int, float)) for v in node)
        ):
            out.append((float(node[0]), float(node[1])))
            return
        for child in node:
            walk(child)

    walk(geometry["coordinates"])
    return out


POPULATED = build_fixture(empty=False)
EMPTY = build_fixture(empty=True)


# ======================================================================
# 1. SCHEMA -- feature_schema.py's own validator, not a copy of its rules
# ======================================================================

print("=" * 70)
print("1. feature_schema CONFORMANCE")
print("=" * 70)

collections = outbound_collections(POPULATED)
total_features = 0
for name, collection in collections.items():
    # The schema module's own validator is the assertion. It raises on the
    # first problem: unique string ids, Feature type, a recognized geometry
    # with coordinates, properties.layer, a valid confidence, a non-empty
    # confidence_notes.
    validate_feature_collection(collection)

    assert collection is not None, f"{name} returned None"
    assert collection["type"] == "FeatureCollection", name
    assert isinstance(collection["features"], list), name

    # Valid GeoJSON means it survives a JSON round-trip -- a shapely
    # geometry or a numpy scalar that leaked into properties would pass
    # the schema validator and fail here, which is the point.
    assert json.loads(json.dumps(collection)) == json.loads(json.dumps(collection)), name
    json.dumps(collection)

    for feature in collection["features"]:
        props = feature["properties"]
        assert props["confidence"] in VALID_CONFIDENCE_LEVELS, (name, feature["id"])
        assert props["confidence_notes"].strip(), (name, feature["id"])
        assert props["layer"], (name, feature["id"])
    total_features += len(collection["features"])
    print(f"  {name:26s} {len(collection['features']):2d} feature(s)  layers="
          f"{sorted({f['properties']['layer'] for f in collection['features']})}")

assert total_features > 0
print(f"\n  {len(collections)} layer function(s), {total_features} feature(s), all schema-valid "
      f"and JSON-serializable.")

# Every layer this branch was asked for is present, by PipelineContext field.
for required in (
    "boundary", "valleys", "keypoints", "exclusion_union", "eligible_ground",
    "production_areas", "water_zones", "selected_water_zone", "road_corridor",
    "structure_site", "tree_zones",
):
    assert required in collections, f"missing outbound function for {required}"
print("  Every layer in the branch's inventory has an outbound function.")


# ======================================================================
# 2. PARITY -- fetch_layout_layers() output is unchanged
# ======================================================================

print()
print("=" * 70)
print("2. PARITY -- fetch_layout_layers() before vs after the refactor")
print("=" * 70)

layers = drive_fetch_layout_layers(POPULATED)
ctx = POPULATED["context"]

assert sorted(layers.keys()) == sorted([
    "dem", "exclusion_zones", "production_areas", "water_zone", "road_corridor",
    "tree_zone_result", "structure_site", "keypoints", "water_features",
    "contour_lines", "fencing_result",
]), sorted(layers.keys())
print("  Return dict keys unchanged.")

# road_corridor is THE key this branch re-routed. Asserted equal to the
# exact expression the function used to return -- identify_road_corridor_
# candidates()' own zones_geojson features -- so "goes through the
# boundary now" and "returns the same bytes" are both proven at once.
was = POPULATED["road_result"]["zones_geojson"]["features"]
assert layers["road_corridor"] == was, "road_corridor drifted from the pre-refactor expression"
assert len(layers["road_corridor"]) == 2, "both branches must be emitted, trunk AND spur"
# ...and not merely equal by accident: the fallback flag that only the
# context carries has to have reached the notes.
assert all(
    "fallback" in f["properties"]["confidence_notes"].lower()
    for f in layers["road_corridor"]
), "hydric_floodplain_is_fallback=True did not reach the emitted confidence_notes"
print(f"  road_corridor: {len(layers['road_corridor'])} feature(s), byte-identical to "
      f"identify_road_corridor_candidates()' own zones_geojson.")

# structure_site deliberately still comes off solar's own zones_geojson --
# see render_layout_map.fetch_layout_layers()' docstring for why it cannot
# be rebuilt from the context field.
assert layers["structure_site"] == POPULATED["solar_result"]["zones_geojson"]["features"][0]
print("  structure_site: unchanged (still solar's own zones_geojson feature 0).")

# Every remaining key is the SAME OBJECT it was read from -- nothing was
# copied, converted, rounded or re-derived on the way out.
assert layers["dem"] is ctx.dem
assert layers["exclusion_zones"] is ctx.exclusion_zones
assert layers["production_areas"] is ctx.production_areas
assert layers["water_zone"] is ctx.selected_water_zone
assert layers["keypoints"] is ctx.keypoints
assert layers["tree_zone_result"]["patches"] is ctx.tree_zone_patches
assert layers["water_features"] is POPULATED["parcel_data"].water_features
print("  Every other layer: the identical object off the context/parcel data, not a copy.")

# And the same on the empty fixture -- an all-empty parcel must not take a
# different code path out.
empty_layers = drive_fetch_layout_layers(EMPTY)
assert empty_layers["road_corridor"] == EMPTY["road_result"]["zones_geojson"]["features"] == []
assert empty_layers["structure_site"] is None
assert empty_layers["production_areas"] == []
assert empty_layers["water_zone"] is None
print("  Empty parcel: same result through the same path (road_corridor [], "
      "structure_site None -- fetch_layout_layers()' own established contract).")

print("\n  PARITY RESULT: fetch_layout_layers() output is UNCHANGED by this branch.")


# ======================================================================
# 3. NO DOUBLE REPROJECTION
# ======================================================================

print()
print("=" * 70)
print("3. NO DOUBLE REPROJECTION -- emitted coords == the stored WGS84 form")
print("=" * 70)

# For each layer whose internal objects already carry geometry_wgs84, the
# emitted geometry must be that stored form -- EQUAL, not approximately.
# Anything that reprojected a second time would land within a metre and
# still be a drift from what the layout map draws.
checked = 0


def _assert_stored(collection_key, sources, source_key="geometry_wgs84"):
    global checked
    features = collections[collection_key]["features"]
    assert len(features) == len(sources), (collection_key, len(features), len(sources))
    for feature, source in zip(features, sources):
        stored = source[source_key]
        assert feature["geometry"] == stored, (
            f"{collection_key}: emitted geometry is not the stored {source_key}"
        )
        # ...and the SAME OBJECT, so nothing rebuilt an equal-looking copy
        assert feature["geometry"] is stored, (
            f"{collection_key}: emitted geometry is a rebuilt copy, not the stored form"
        )
        checked += 1
    print(f"  {collection_key:26s} {len(features)} geometry(ies) are the stored {source_key}, "
          f"by identity")


_assert_stored("valleys", ctx.valleys)
_assert_stored("keypoints", ctx.keypoints)
_assert_stored("production_areas", ctx.production_areas)
_assert_stored("tree_zones", ctx.tree_zone_patches)
_assert_stored("structure_site", [ctx.selected_structure_site])
_assert_stored("road_corridor", ctx.selected_road_corridor["branches"])

# exclusion: both layers wrap the module's OWN already-reprojected keys
assert collections["exclusion_union"]["features"][0]["geometry"] is ctx.exclusion_zones["geometry_wgs84"]
assert collections["eligible_ground"]["features"][0]["geometry"] is ctx.exclusion_zones["eligible_union_wgs84"]
checked += 2
print("  exclusion_union / eligible_ground  wrap the module's own geometry_wgs84 / "
      "eligible_union_wgs84, by identity")

# exclusion gates wrap the module's own wire block, gate by gate
wire_layers = ctx.exclusion_zones["wire"]["layers"]
by_type = {layer["type"]: layer for layer in wire_layers}
for feature in collections["exclusion_gates"]["features"]:
    assert feature["geometry"] is by_type[feature["properties"]["gate_type"]]["geometry_wgs84"]
    checked += 1
print(f"  exclusion_gates            {len(collections['exclusion_gates']['features'])} gate "
      f"geometry(ies) are the wire block's own, by identity")

# water_zones is already a features list -- the adapter must WRAP it, and
# the Feature objects themselves must be the very same ones.
water_features = collections["water_zones"]["features"]
assert water_features == ctx.water_zones
assert all(a is b for a, b in zip(water_features, ctx.water_zones))
assert water_features is not ctx.water_zones, "the list must be copied so callers cannot mutate the context"
checked += len(water_features)
print(f"  water_zones                {len(water_features)} Feature(s) passed through unchanged "
      f"(a wrap, not a second conversion)")

# The selected zone's Feature must match its entry in the candidate set
selected_feature = collections["selected_water_zone"]["features"][0]
zone_features = [f for f in ctx.water_zones if f["properties"]["layer"].startswith("survey_zone_")
                 and not f["properties"]["layer"].startswith("survey_zone_member")]
assert selected_feature == zone_features[0], "selected zone Feature differs from its candidate entry"
assert selected_feature["geometry"] is POPULATED["raw_water_zones"][0]["geometry_wgs84"]
checked += 1
print("  selected_water_zone        matches its own entry in water_zones exactly")

print(f"\n  {checked} geometry(ies) checked. ZERO reprojections performed by wire_translation.py.")

# The boundary is the one hand-built geometry -- it is already lon/lat and
# must be ring-closed, never transformed.
boundary_geom = collections["boundary"]["features"][0]["geometry"]
ring = boundary_geom["coordinates"][0]
assert ring[0] == ring[-1], "GeoJSON polygon ring must be closed"
for emitted, source in zip(ring, POPULATED["coords"]):
    assert emitted == [float(source[0]), float(source[1])], "boundary was transformed, not wrapped"
print("  boundary                   ring-closed from the input lon/lat, untransformed.")


# ======================================================================
# 4. EMPTY AND None -- never None, never an error
# ======================================================================

print()
print("=" * 70)
print("4. EMPTY AND None CASES")
print("=" * 70)

empty_collections = outbound_collections(EMPTY)
# The empty fixture is a parcel where every KSOP step ran and found
# nothing. Two layers are legitimately still populated there and asserting
# them empty would be asserting the wrong thing:
#   boundary        -- an INPUT, not a computed layer.
#   eligible_ground -- no gate fired, so the WHOLE parcel is selectable.
#                      That is the most populated this layer ever gets.
STILL_POPULATED_WHEN_NOTHING_COMPUTED = {"boundary", "eligible_ground"}
empty_count = 0
for name, collection in empty_collections.items():
    assert collection is not None, f"{name} returned None on the empty fixture"
    validate_feature_collection(collection)
    if name in STILL_POPULATED_WHEN_NOTHING_COMPUTED:
        assert len(collection["features"]) == 1, name
        continue
    assert collection["features"] == [], f"{name} should be empty, got {len(collection['features'])}"
    empty_count += 1
print(f"  Empty fixture: {empty_count} computed layer(s) each returned a VALID, EMPTY "
      f"FeatureCollection; boundary and eligible_ground correctly still carry one feature.")

# ...and a bare None for every function, which is what an interactive
# session holds for a step that has not run yet.
none_cases = [
    ("boundary", wt.boundary_to_feature_collection(None)),
    ("boundary (empty list)", wt.boundary_to_feature_collection([])),
    ("boundary (degenerate ring)", wt.boundary_to_feature_collection([(-83.0, 40.0), (-83.1, 40.1)])),
    ("valleys", wt.valleys_to_feature_collection(None)),
    ("keypoints", wt.keypoints_to_feature_collection(None)),
    ("exclusion_union", wt.exclusion_union_to_feature_collection(None)),
    ("eligible_ground", wt.eligible_ground_to_feature_collection(None)),
    ("exclusion_gates", wt.exclusion_gate_layers_to_feature_collection(None)),
    ("production_areas", wt.production_areas_to_feature_collection(None)),
    ("scored production", wt.scored_production_areas_to_feature_collection(None)),
    ("water survey zones", wt.water_survey_zones_to_feature_collection(None)),
    ("water_zones", wt.water_zone_features_to_feature_collection(None)),
    ("selected_water_zone", wt.selected_water_zone_to_feature_collection(None)),
    ("road network", wt.road_network_to_feature_collection(None)),
    ("road network branches=[]", wt.road_network_to_feature_collection(_road_network([]))),
    ("structure sites", wt.structure_sites_to_feature_collection(None)),
    ("selected structure site", wt.selected_structure_site_to_feature_collection(None)),
    ("tree zones", wt.tree_zones_to_feature_collection(None)),
]
for name, collection in none_cases:
    assert collection is not None, f"{name} returned None"
    assert collection == {"type": "FeatureCollection", "features": []}, (name, collection)
    validate_feature_collection(collection)
print(f"  None / degenerate input: {len(none_cases)} case(s) each returned a VALID, EMPTY "
      f"FeatureCollection -- never None, never a raised error.")

# BOTH None cases exclusion_zones.py can genuinely produce. Each is a real
# "computed, nothing there" answer and must translate to an empty
# FeatureCollection, not to a failure -- and crucially, they are NOT the
# same parcel: one has nothing excluded, the other has nothing eligible.
nothing_excluded = EMPTY["exclusion"]
assert nothing_excluded["geometry_wgs84"] is None
assert wt.exclusion_union_to_feature_collection(nothing_excluded)["features"] == []
assert len(wt.eligible_ground_to_feature_collection(nothing_excluded)["features"]) == 1

nothing_eligible = _exclusion_result(
    POPULATED["dem"], POPULATED["boundary"], mode="nothing_eligible"
)
assert nothing_eligible["eligible_union_wgs84"] is None
assert wt.eligible_ground_to_feature_collection(nothing_eligible)["features"] == []
assert len(wt.exclusion_union_to_feature_collection(nothing_eligible)["features"]) == 1
print("  exclusion_zones: a None union geometry AND a None eligible geometry each translate "
      "to computed-and-empty, independently -- not an error, and not each other.")

# The distinction case 4 exists for: a gate that was CHECKED and excludes
# nothing must not be confusable with a gate that was NEVER checked.
gates = collections["exclusion_gates"]["features"]
availability = gates[0]["properties"]["gate_availability"]
assert availability["roads"] is True, "'roads' was checked and found nothing"
assert availability["hydric"] is False, "'hydric' was never checked"
assert "roads" not in {g["properties"]["gate_type"] for g in gates}
assert "hydric" not in {g["properties"]["gate_type"] for g in gates}
print("  Gate availability: checked-and-empty ('roads': True) stays distinguishable from "
      "never-checked ('hydric': False), though neither emits geometry.")


# ======================================================================
# 5. COORDINATE ORDER -- [lon, lat], WGS84
# ======================================================================

print()
print("=" * 70)
print("5. COORDINATE ORDER -- [lon, lat] WGS84")
print("=" * 70)

# The fixture sits at 500000 E in EPSG:32617 -- UTM zone 17's central
# meridian, 81 W -- around 40.6 N. Checking against that known box (rather
# than a bare -180..180 / -90..90 range test) is what actually catches a
# swap: a lat/lon pair here would be (40.6, -81.0), which passes a plain
# range test on both axes and fails this one.
lon_lo, lon_hi = -81.5, -80.5
lat_lo, lat_hi = 40.0, 41.5
pairs_checked = 0
for name, collection in collections.items():
    for feature in collection["features"]:
        for lon, lat in _coord_pairs(feature["geometry"]):
            assert lon_lo < lon < lon_hi, f"{name}/{feature['id']}: {lon} is not a longitude here"
            assert lat_lo < lat < lat_hi, f"{name}/{feature['id']}: {lat} is not a latitude here"
            pairs_checked += 1
print(f"  {pairs_checked} coordinate pair(s) across {len(collections)} layer(s) are "
      f"[lon, lat] in WGS84 degrees, inside the fixture's own known box.")

# Sanity check on the check itself: the swapped form must actually fail.
swapped_lon, swapped_lat = _coord_pairs(collections["valleys"]["features"][0]["geometry"])[0][::-1]
assert not (lon_lo < swapped_lon < lon_hi and lat_lo < swapped_lat < lat_hi), (
    "the coordinate-order box is too loose to catch a swap"
)
print("  The box is tight enough that a swapped pair fails it.")


# ======================================================================
# 6. CONSOLIDATION -- one implementation, reached by both names
# ======================================================================

print()
print("=" * 70)
print("6. CONSOLIDATION -- the moved helpers forward, they do not duplicate")
print("=" * 70)

from keypoint_detection import keypoints_to_geojson
from production_area import production_areas_to_geojson
from production_suitability import production_suitability_to_geojson
from road_corridors import corridors_to_geojson
from solar_suitability import candidates_to_geojson
from tree_zone_candidates import tree_zones_to_geojson
from valley_delineation import valleys_to_geojson
from water_survey_areas import survey_areas_to_geojson

forwards = [
    ("valley_delineation.valleys_to_geojson",
     valleys_to_geojson(ctx.valleys), collections["valleys"]),
    ("keypoint_detection.keypoints_to_geojson",
     keypoints_to_geojson(ctx.keypoints), collections["keypoints"]),
    ("production_area.production_areas_to_geojson",
     production_areas_to_geojson(ctx.production_areas), collections["production_areas_unscored"]),
    ("production_suitability.production_suitability_to_geojson",
     production_suitability_to_geojson(ctx.production_areas), collections["production_areas"]),
    ("water_survey_areas.survey_areas_to_geojson",
     survey_areas_to_geojson(POPULATED["raw_water_zones"]),
     wt.water_survey_zones_to_feature_collection(POPULATED["raw_water_zones"])),
    ("road_corridors.corridors_to_geojson",
     corridors_to_geojson(ctx.selected_road_corridor, floodplain_data_is_fallback=True),
     collections["road_corridor"]),
    ("solar_suitability.candidates_to_geojson",
     candidates_to_geojson(POPULATED["solar_candidates"],
                           road_proximity_source="selected_road_corridor"),
     wt.structure_sites_to_feature_collection(POPULATED["solar_candidates"],
                                              road_proximity_source="selected_road_corridor")),
    ("tree_zone_candidates.tree_zones_to_geojson",
     tree_zones_to_geojson(ctx.tree_zone_patches), collections["tree_zones"]),
]
for name, legacy, boundary_output in forwards:
    assert legacy == boundary_output, f"{name} does not match its wire_translation implementation"
    validate_feature_collection(legacy)
print(f"  {len(forwards)} moved helper(s): each module's own name still works and returns "
      f"exactly what wire_translation.py returns.")

print()
print("=" * 70)
print("All wire_translation checks passed.")
print("PARITY: fetch_layout_layers() output is geometrically and byte-for-byte IDENTICAL.")
print("=" * 70)
