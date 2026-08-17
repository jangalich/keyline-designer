"""
test_generate_full_report.py

Offline (no-network, no-LLM) checks for generate_full_report.py's rewired
architecture: it builds ParcelData ONCE and PipelineContext ONCE, sources
every report input from those two objects, HARD-FAILS upstream (Option A --
no per-section try/except), and makes exactly ONE extra KSOP call beyond
build_pipeline_context() (the full ranked solar list the narrative needs).

Every real fetch/compute function is mocked here, patched in generate_full_
report's OWN namespace (it imports each with `from X import Y`, so patching
the source module's namespace would leave generate_full_report's already-
bound reference untouched -- same discipline test_parcel_data.py documents).
validate_access_point_on_boundary is patched to a no-op everywhere so the
synthetic boundary/anchor never trip a real geometry check.

Covers:
  2. fetch_parcel_data() raising -> generate_full_report() raises the SAME
     exception, uncaught, before build_pipeline_context() is EVER called.
  3. build_pipeline_context() raising -> generate_full_report() raises the
     SAME exception, uncaught, before generate_scale_of_permanence_report()
     is EVER called.
  4. Happy path with a full synthetic ParcelData + PipelineContext:
     identify_solar_candidate_zones() is called EXACTLY once (the one
     accepted extra call), and NO other KSOP entry point (water/road/tree/
     keypoints) is called a second time by generate_full_report.py itself.
  5. road_network handed to generate_scale_of_permanence_report() is the
     SAME object (`is`) as context.selected_road_corridor -- not rebuilt.
  6. water_candidate_zones_geojson is correctly wrapped from context.water_
     zones: same feature count, and schema-valid via validate_feature_
     collection().
"""

from contextlib import ExitStack
from unittest.mock import Mock, patch as mock_patch

from shapely.geometry import Polygon

import generate_full_report
from generate_full_report import generate_full_report as run_report
from feature_schema import make_feature, validate_feature_collection
from parcel_data import ParcelData
from pipeline_context import PipelineContext


# A real point ON this boundary is irrelevant here (validate_access_point_
# on_boundary is patched to a no-op), but use realistic values anyway.
BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]
ANCHOR = (-79.98374275, 40.6443462)


class _Boom(Exception):
    """Distinct sentinel exception so identity checks (`is`) are unambiguous."""


def _synthetic_water_features() -> list[dict]:
    """Two schema-conformant water_system_candidate Features -- the exact
    shape context.water_zones carries (identify_water_system_candidate_
    zones()'s own zones_geojson['features'])."""
    return [
        make_feature(
            feature_id="water_zone_0",
            geometry={"type": "Point", "coordinates": [-79.983, 40.645]},
            layer="water_system_candidate",
            label="Candidate pond A",
            confidence="medium",
            confidence_notes="DEM/LiDAR-derived; coarse valley delineation.",
        ),
        make_feature(
            feature_id="water_zone_1",
            geometry={
                "type": "Polygon",
                "coordinates": [[[-79.982, 40.644], [-79.981, 40.644], [-79.981, 40.645], [-79.982, 40.644]]],
            },
            layer="water_system_candidate",
            label="Candidate pond B",
            confidence="low",
            confidence_notes="DEM/LiDAR-derived; setback estimate only.",
        ),
    ]


def _synthetic_parcel_data() -> ParcelData:
    """A fully-populated ParcelData with correctly-shaped synthetic fields.
    build_pipeline_context() is mocked so most fields are never actually
    consumed downstream; the ones generate_full_report.py reads directly
    (water_features['streams'|'water_bodies'], imagery_summary['scene_date'],
    canopy_height, soil_components, elevation_grid, climate_summary,
    irradiance) are shaped for real use."""
    return ParcelData(
        dem={"crs": "EPSG:32617", "synthetic": True},
        boundary_polygon_utm=Polygon([(0, 0), (100, 0), (100, 100), (0, 100)]),
        soil_components=[{"muname": "Synthetic soil", "comppct_r": 100}],
        farmland_classification=[{"class": "synthetic"}],
        erosion_factor=[{"kffact": 0.2}],
        saturated_hydraulic_conductivity=[{"ksat": 9.0}],
        soil_geometries={"type": "FeatureCollection", "features": []},
        water_features={"streams": [{"name": "Synthetic Run"}], "water_bodies": []},
        farm_roads=[{"synthetic": True}],
        climate_summary={"prevailing_wind_direction": "WSW", "avg_annual_precipitation_mm": 1020.0},
        elevation_grid=[{"latitude": 40.6443, "longitude": -79.9821, "elevation": 330.0}],
        canopy_height={"synthetic_canopy": True},
        imagery_summary={"scene_date": "2026-05-14", "days_since_scene": 63, "cloud_cover_pct": 4.2},
        irradiance={"status": "ok", "annual_ghi_kwh_m2_day": 4.21},
    )


def _synthetic_context(water_zones: list[dict]) -> PipelineContext:
    """A fully-populated PipelineContext. selected_road_corridor is a truthy
    network dict carrying cell_footprint_polygon_utm (generate_full_report.py
    reads that key when the corridor is truthy). production_areas exercises
    the render_fill_polygon_utm filter (one patch has it, one doesn't)."""
    return PipelineContext(
        dem={"crs": "EPSG:32617", "synthetic": True},
        boundary_polygon_utm=Polygon([(0, 0), (100, 0), (100, 100), (0, 100)]),
        valleys=[{"valley_id": 3}],
        keypoints=[{"id": 0, "valley_id": 3, "elevation_m": 346.5}],
        production_areas=[
            {"render_fill_polygon_utm": Polygon([(1, 1), (2, 1), (2, 2), (1, 2)])},
            {"render_fill_polygon_utm": None},
        ],
        parcel_acres=2.47,
        existing_roads=None,
        soil_exclusion_unions={
            "hydric_floodplain_union": None,
            "hydric_floodplain_is_fallback": True,
        },
        water_zones=water_zones,
        selected_water_zone={"zone_id": "w0"},
        selected_road_corridor={
            "branches": [],
            "total_length_meters": 0.0,
            "cell_footprint_polygon_utm": Polygon([(3, 3), (4, 3), (4, 4), (3, 4)]),
        },
        selected_structure_site={"site_id": "s0"},
        tree_zone_patches=[{"patch_id": "t0"}],
    )


# =====================================================================
# 2. fetch_parcel_data() raising -> generate_full_report() raises the SAME
#    exception, uncaught, and build_pipeline_context() is NEVER reached.
# =====================================================================
boom = _Boom("raw parcel data fetch failed")
with ExitStack() as stack:
    stack.enter_context(mock_patch.object(generate_full_report, "validate_access_point_on_boundary", Mock()))
    fetch_mock = stack.enter_context(
        mock_patch.object(generate_full_report, "fetch_parcel_data", Mock(side_effect=boom))
    )
    ctx_mock = stack.enter_context(mock_patch.object(generate_full_report, "build_pipeline_context", Mock()))
    report_mock = stack.enter_context(
        mock_patch.object(generate_full_report, "generate_scale_of_permanence_report", Mock())
    )

    raised = None
    try:
        run_report(BOUNDARY, ANCHOR)
    except _Boom as e:
        raised = e

    assert raised is boom, (
        "a fetch_parcel_data() failure must propagate UNCAUGHT as the same exception -- "
        "no per-section try/except softens it (Option A hard-fail)"
    )
    assert fetch_mock.call_count == 1
    assert ctx_mock.call_count == 0, (
        "build_pipeline_context() must NOT be called after a raw-data failure -- the report is "
        "stopped before any KSOP computation begins"
    )
    assert report_mock.call_count == 0, "no report may be generated after a raw-data failure"
print(
    "2. fetch_parcel_data() raising propagates the SAME exception uncaught; build_pipeline_context() "
    "is never reached (0 calls) and no report is generated."
)


# =====================================================================
# 3. build_pipeline_context() raising -> generate_full_report() raises the
#    SAME exception, uncaught, and generate_scale_of_permanence_report() is
#    NEVER reached.
# =====================================================================
boom = _Boom("KSOP context computation failed")
with ExitStack() as stack:
    stack.enter_context(mock_patch.object(generate_full_report, "validate_access_point_on_boundary", Mock()))
    stack.enter_context(
        mock_patch.object(generate_full_report, "fetch_parcel_data", Mock(return_value=_synthetic_parcel_data()))
    )
    ctx_mock = stack.enter_context(
        mock_patch.object(generate_full_report, "build_pipeline_context", Mock(side_effect=boom))
    )
    solar_mock = stack.enter_context(mock_patch.object(generate_full_report, "identify_solar_candidate_zones", Mock()))
    fencing_mock = stack.enter_context(mock_patch.object(generate_full_report, "identify_fencing", Mock()))
    report_mock = stack.enter_context(
        mock_patch.object(generate_full_report, "generate_scale_of_permanence_report", Mock())
    )

    raised = None
    try:
        run_report(BOUNDARY, ANCHOR)
    except _Boom as e:
        raised = e

    assert raised is boom, (
        "a build_pipeline_context() failure must propagate UNCAUGHT as the same exception -- a "
        "derived-computation failure anywhere in the KSOP chain stops the whole report (Option A)"
    )
    assert ctx_mock.call_count == 1
    assert report_mock.call_count == 0, (
        "generate_scale_of_permanence_report() must NOT be called after a KSOP computation failure -- "
        "a report built on failed KSOP computation is never generated"
    )
print(
    "3. build_pipeline_context() raising propagates the SAME exception uncaught; the report generator "
    "is never reached (0 calls)."
)


# =====================================================================
# 4/5/6. Happy path with a full synthetic ParcelData + PipelineContext.
#    build_pipeline_context() is mocked to RETURN the synthetic context (so
#    it performs ZERO internal KSOP calls); generate_full_report.py's own
#    body is what runs on top. Assert the one accepted extra solar call, no
#    second call to any other KSOP entry point, road_network identity, and
#    correct water-zone wrapping.
# =====================================================================
_water_zones = _synthetic_water_features()
_parcel = _synthetic_parcel_data()
_context = _synthetic_context(_water_zones)

# wraps= a synthetic stub: the mock both records call_count AND returns a
# real-shaped ranked solar result (with a non-empty features list so the
# structure_site derivation downstream is exercised).
_SOLAR_RESULT = {
    "zones_geojson": {
        "type": "FeatureCollection",
        "features": [
            make_feature(
                feature_id="solar_0",
                geometry={"type": "Point", "coordinates": [-79.9825, 40.6448]},
                layer="solar_infrastructure",
                label="Rank-1 structure site",
                confidence="high",
                confidence_notes="DEM slope/aspect/shading-derived; ranked candidate.",
            ),
        ],
    }
}


def _solar_stub(*args, **kwargs):
    return _SOLAR_RESULT


with ExitStack() as stack:
    stack.enter_context(mock_patch.object(generate_full_report, "validate_access_point_on_boundary", Mock()))
    stack.enter_context(
        mock_patch.object(generate_full_report, "fetch_parcel_data", Mock(return_value=_parcel))
    )
    stack.enter_context(
        mock_patch.object(generate_full_report, "build_pipeline_context", Mock(return_value=_context))
    )
    solar_mock = stack.enter_context(
        mock_patch.object(generate_full_report, "identify_solar_candidate_zones", Mock(wraps=_solar_stub))
    )
    fencing_mock = stack.enter_context(
        mock_patch.object(
            generate_full_report,
            "identify_fencing",
            Mock(return_value={"fencing_geojson": {"type": "FeatureCollection", "features": []}}),
        )
    )
    report_mock = stack.enter_context(
        mock_patch.object(
            generate_full_report,
            "generate_scale_of_permanence_report",
            Mock(return_value="SYNTHETIC REPORT"),
        )
    )

    # Patch the OTHER KSOP entry points at their SOURCE modules and assert
    # they are never called: generate_full_report.py does not import them,
    # and build_pipeline_context() is mocked, so any call would be a
    # regression (a re-introduced redundant self-fetch/self-compute).
    import water_candidate_zones
    import road_corridors
    import tree_zone_candidates
    import keypoint_detection

    water_entry = stack.enter_context(
        mock_patch.object(water_candidate_zones, "identify_water_system_candidate_zones", Mock())
    )
    road_entry = stack.enter_context(
        mock_patch.object(road_corridors, "identify_road_corridor_candidates", Mock())
    )
    tree_entry = stack.enter_context(
        mock_patch.object(tree_zone_candidates, "identify_tree_zone_candidates", Mock())
    )
    keypoint_entry = stack.enter_context(
        mock_patch.object(keypoint_detection, "identify_keypoints", Mock())
    )

    result = run_report(BOUNDARY, ANCHOR)

    assert result == "SYNTHETIC REPORT", "generate_full_report() must return the report generator's output verbatim"

    # ---- 4: exactly ONE extra solar call, no other KSOP entry re-called ----
    assert solar_mock.call_count == 1, (
        f"identify_solar_candidate_zones() must be called EXACTLY once by generate_full_report.py "
        f"(the one accepted extra call for the full ranked list), got {solar_mock.call_count}"
    )
    for name, entry in (
        ("identify_water_system_candidate_zones", water_entry),
        ("identify_road_corridor_candidates", road_entry),
        ("identify_tree_zone_candidates", tree_entry),
        ("identify_keypoints", keypoint_entry),
    ):
        assert entry.call_count == 0, (
            f"{name}() must NOT be called by generate_full_report.py itself -- that value comes from "
            f"the single build_pipeline_context() call (got {entry.call_count} call(s))"
        )
    print(
        "4. identify_solar_candidate_zones() is called exactly once (the accepted extra); no other KSOP "
        "entry point (water/road/tree/keypoints) is called a second time by generate_full_report.py."
    )

    # ---- report generator call args (shared by checks 5 and 6) ----
    report_mock.assert_called_once()
    call_args, call_kwargs = report_mock.call_args
    passed_water_fc = call_args[5]
    passed_solar_fc = call_args[6]
    passed_road_network = call_args[7]

    # ---- 5: road_network is context.selected_road_corridor by identity ----
    assert passed_road_network is _context.selected_road_corridor, (
        "road_network handed to generate_scale_of_permanence_report() must be the SAME object as "
        "context.selected_road_corridor (build_road_network()'s own dict), not rebuilt or re-derived"
    )
    print(
        "5. road_network passed to the report generator IS context.selected_road_corridor by identity "
        "(`is`) -- not rebuilt."
    )

    # ---- 6: water_candidate_zones_geojson correctly wrapped ----
    assert passed_water_fc["type"] == "FeatureCollection"
    assert len(passed_water_fc["features"]) == len(_context.water_zones), (
        "the wrapped water FeatureCollection must carry exactly the features from context.water_zones"
    )
    assert passed_water_fc["features"] == _context.water_zones, (
        "the wrapped features must be context.water_zones verbatim (make_feature_collection wraps, "
        "it does not copy or transform)"
    )
    validate_feature_collection(passed_water_fc)  # raises if not schema-valid
    print(
        f"6. water_candidate_zones_geojson wraps context.water_zones exactly "
        f"({len(passed_water_fc['features'])} feature(s)) and is schema-valid via "
        "validate_feature_collection()."
    )

    # ---- bonus: solar candidate list and irradiance seam are forwarded ----
    assert passed_solar_fc is _SOLAR_RESULT["zones_geojson"], (
        "solar_candidate_zones_geojson must be the extra solar call's own zones_geojson"
    )
    assert call_kwargs["keypoints"] is _context.keypoints, (
        "keypoints handed to the report generator must be context.keypoints (no separate detection call)"
    )
    assert call_kwargs["irradiance"] is _parcel.irradiance, (
        "irradiance must be forwarded straight from parcel_data.irradiance (the stored, inert seam)"
    )
    print(
        "   bonus: solar_candidate_zones_geojson is the extra call's ranked list; keypoints come from "
        "the context; irradiance is forwarded from parcel_data.irradiance."
    )


print("\nAll generate_full_report checks passed.")
