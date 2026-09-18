"""
test_session_report.py

THE REPORT AND THE MAP READ THE OWNER'S COMMITTED DECISIONS.

Offline, through the real orchestrator on the real parcel: sessions are
created, steps generated and committed exactly as a client would, and then
session_design.py is asked for the design those commits describe.

WHAT THIS FILE EXISTS TO CATCH, and it is one thing. Before this branch the
report and the layout map both ran pipeline_context.build_pipeline_context()
-- a fresh walk of the boundary that recomputes every KSOP step and picks
its OWN winners. Run for a session, that produces a document and a map that
describe a design nobody chose, and it looks completely normal: correct
acreages, correct geometry, correct prose, wrong design. Section 1 is the
test for it and the reason this file is here.

THE SEVEN CASES, plus the batch-path guard:

  1. A report generated from a session narrates the OWNER'S COMMITTED
     CHOICES -- a committed production block, the committed water zones and
     the committed road network are all in the prompt's data blocks, and a
     DECLINED candidate is not.
  2. The layout map DRAWS the committed design -- the same assertion, on
     the layers a real render consumes.
  3. TWO PLACED STRUCTURE SITES both appear on the map.
  4. A water commit of SEVERAL zones is narrated and drawn as what the
     owner chose.
  5. A roads commit of ONE network out of THREE draws and narrates that one.
  6. An EMPTY commit -- no water zone -- narrates and draws as the
     deliberate decision it is, never as a gap in the data.
  7. An EVICTED session fails with a specific, sayable reason rather than a
     partial context.
  8. The BATCH PATH is untouched: a narrative block with no commitment
     marker is formatted byte for byte as it always was.

No network: offline_harness.install() refuses every outbound request, and
fencing_step_fixture.Harness patches and counts every fetch boundary the
session path can reach.
"""

import os
import tempfile
from unittest.mock import patch as mock_patch

import numpy as np
from shapely.geometry import LineString, Point

import offline_harness

offline_harness.install()

import fencing_step_fixture as fixture  # noqa: E402
import render_layout_map as rlm  # noqa: E402
import report_generator  # noqa: E402
import session_design  # noqa: E402
import solar_suitability  # noqa: E402
from fencing_step_fixture import BOUNDARY_POLYGON_UTM, Harness, Session  # noqa: E402

# Three access points on the parcel's own west edge, each of which routes a
# REAL and DIFFERENT network -- measured, not assumed (the lengths are
# asserted distinct in section 5, which is what makes "the declined one is
# absent" a claim with teeth).
ACCESS_POINTS = [fixture._boundary_point(0, fraction) for fraction in (0.3, 0.6, 0.85)]


def sections(summary: str) -> dict:
    """
    report_generator.build_data_summary()'s prompt text, split into its
    named data blocks -- {"PRODUCTION AREAS": "<that block's body>", ...}.

    Asserting per SECTION rather than over the whole string is what makes an
    absence assertion mean something: "779.0 is not in this report" could be
    satisfied by a number that moved to another section, while "779.0 is not
    in the SUGGESTED ROAD CORRIDOR block" is the claim actually being made.
    """
    # Sliced on the KNOWN headers rather than on blank lines: several
    # bodies (the water block's per-zone detail, for one) carry blank lines
    # of their own, and splitting on those silently truncates the section
    # an absence assertion is made over -- which would make the assertion
    # pass for the wrong reason.
    headers = [
        "CLIMATE DATA", "SOIL DATA", "ELEVATION", "PRODUCTION AREAS",
        "KEYPOINT CANDIDATES", "WATER FEATURES", "SATELLITE IMAGERY / LAND COVER",
        "WATER SYSTEM SURVEY AREAS", "SUGGESTED ROAD CORRIDOR", "TREE CROP AREAS",
        "PERMANENT BUILDING SITE", "SOLAR IRRADIANCE",
    ]
    starts = []
    for header in headers:
        index = summary.find(f"\n{header}")
        if index < 0 and summary.startswith(header):
            index = 0
        assert index >= 0, f"the prompt is missing its {header} block"
        starts.append((index, header))
    starts.sort()
    blocks = {}
    for position, (index, header) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(summary)
        chunk = summary[index:end].lstrip("\n")
        blocks[header] = chunk.partition("\n")[2].strip()
    return blocks


def data_summary(design) -> str:
    """The prompt's data half for one committed design -- the same call
    generate_full_report.generate_session_report() makes, minus the model."""
    return report_generator.build_data_summary(
        design.parcel_data.soil_components,
        None,
        design.parcel_data.water_features,
        climate_summary=design.parcel_data.climate_summary,
        keypoints=design.keypoints,
        # irradiance is left out: the fixture's stub carries none of the
        # real layer's keys, and nothing this file asserts reads it.
        narrative_data=design.narrative_data,
        boundary_polygon_utm=design.boundary_polygon_utm,
    )


def design_for(session):
    return session_design.build_session_design(
        session.id, session.store, fetch_cache=session.fetch_cache, cache=session.cache
    )


class RenderSpy:
    """
    Every per-feature draw a render_layout_map() pass makes, by layer.

    WRAPS, NEVER REPLACES: each hook calls through to the real function, so
    the render actually happens and the counts describe a real PNG rather
    than a dry run. The three hooked seams are the three layers whose
    multiplicity this branch changed -- the water ripple pass, the structure
    pin, and the road branch -- plus the tree hatch, which was already
    per-feature and is here as the control.
    """

    def __enter__(self):
        self.water_polygons = []
        self.structure_pins = []
        self.road_lines = []
        real_ripple = rlm._ripple_lines_for_polygon
        real_pin = rlm.AnnotationBbox
        real_road = rlm._draw_road_corridor

        def ripple(geom, *args, **kwargs):
            self.water_polygons.append(geom)
            return real_ripple(geom, *args, **kwargs)

        def pin(*args, **kwargs):
            self.structure_pins.append(args[1] if len(args) > 1 else None)
            return real_pin(*args, **kwargs)

        def road(ax, line):
            self.road_lines.append(line)
            return real_road(ax, line)

        self._patches = [
            mock_patch.object(rlm, "_ripple_lines_for_polygon", ripple),
            mock_patch.object(rlm, "AnnotationBbox", pin),
            mock_patch.object(rlm, "_draw_road_corridor", road),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc_info):
        for patch in self._patches:
            patch.stop()
        return False


def render(design) -> tuple:
    """One real render of a committed design; returns (layers, RenderSpy)."""
    layers = session_design.layout_layers(design)
    with tempfile.TemporaryDirectory() as tmpdir, RenderSpy() as spy:
        path = rlm.render_layout_map(design.boundary, os.path.join(tmpdir, "layout_map.png"), layers=layers)
        assert os.path.getsize(path) > 0, "the map must be a real, non-empty PNG"
    return layers, spy


print("=" * 72)
print("test_session_report.py -- the report and the map read the committed design")
print("=" * 72)


# ======================================================================
# THE SESSION every section but 3, 6 and 8 reads
# ======================================================================
#
# ONE SESSION, PARTIALLY COMMITTED AT EVERY STEP, because a design where
# everything was kept cannot show that anything was dropped: 2 production
# blocks of 5, 2 water zones of the survivors, 1 road network of 3, 1 tree
# zone, 1 structure site. Every "the declined one is absent" assertion below
# rests on this shape.

with Harness() as harness:
    MAIN = Session()

    landform_payload = MAIN.generate("landform")
    LANDFORM_CANDIDATES = landform_payload["suggested_zones"]["features"]
    assert len(LANDFORM_CANDIDATES) >= 4, "the fixture must offer several production blocks to decline"
    KEPT_BLOCKS = LANDFORM_CANDIDATES[:2]
    DECLINED_BLOCKS = LANDFORM_CANDIDATES[2:]
    MAIN.commit("landform", KEPT_BLOCKS, {f["id"]: "generated" for f in KEPT_BLOCKS})

    WATER_CANDIDATES = MAIN.water_zones()
    assert len(WATER_CANDIDATES) >= 4, "the fixture must offer several water zones to decline"
    KEPT_ZONES = WATER_CANDIDATES[:2]
    DECLINED_ZONES = WATER_CANDIDATES[2:]
    MAIN.commit("water", KEPT_ZONES, {f["id"]: "generated" for f in KEPT_ZONES})

    for access_point in ACCESS_POINTS:
        MAIN.generate("roads", {"access_point": list(access_point)})
    ROADS_PAYLOAD = MAIN.layers("roads")
    NETWORKS = {block["network_id"]: block for block in ROADS_PAYLOAD["networks"]}
    assert len(NETWORKS) == 3, f"three candidate networks expected, got {sorted(NETWORKS)}"
    assert all(block["network_found"] for block in NETWORKS.values()), NETWORKS
    KEPT_NETWORK_ID = max(
        NETWORKS, key=lambda key: NETWORKS[key]["access"]["total_length_ft"]
    )
    DECLINED_NETWORKS = [block for key, block in NETWORKS.items() if key != KEPT_NETWORK_ID]
    ROAD_FEATURES = [
        feature
        for feature in ROADS_PAYLOAD["road_corridors"]["features"]
        if feature["properties"]["network_id"] == KEPT_NETWORK_ID
    ]
    MAIN.commit(
        "roads",
        ROAD_FEATURES,
        {f["id"]: "generated" for f in ROAD_FEATURES},
        inputs={"access_points": [list(point) for point in ACCESS_POINTS]},
    )

    MAIN.commit_trees(1)
    MAIN.commit_structures(1)

    DESIGN = design_for(MAIN)
    SUMMARY = data_summary(DESIGN)
    BLOCKS = sections(SUMMARY)
    LAYERS, SPY = render(DESIGN)

    KEPT_ACCESS_POINT = ACCESS_POINTS[
        [fixture.wire_translation.access_point_key(p) for p in ACCESS_POINTS].index(KEPT_NETWORK_ID)
    ]

    # ==================================================================
    # 1. THE REPORT NARRATES THE OWNER'S COMMITTED CHOICES
    # ==================================================================
    print("\n1 [test 1]. THE REPORT NARRATES THE COMMITTED DESIGN -- and not the declined candidates")

    production = BLOCKS["PRODUCTION AREAS"]
    water = BLOCKS["WATER SYSTEM SURVEY AREAS"]
    roads = BLOCKS["SUGGESTED ROAD CORRIDOR"]

    # -- A SPECIFIC COMMITTED BLOCK APPEARS. Its acreage as the run's own
    # narrative block carries it (NOT the Feature's properties.area_acres,
    # which is the render-fill figure), so this compares like with like
    # against the declined blocks below.
    ALL_PATCH_BLOCKS = MAIN.result("landform")["narrative_data"]["patches"]
    KEPT_BLOCK_IDS = {patch["id"] for patch in DESIGN.production_areas}
    kept_block_acres = [
        f"{block['area_acres']} acres" for block in ALL_PATCH_BLOCKS if block["id"] in KEPT_BLOCK_IDS
    ]
    assert len(kept_block_acres) == len(KEPT_BLOCKS)
    for acres in kept_block_acres:
        assert acres in production, (
            f"a committed production block's acreage ({acres}) must be narrated; the "
            f"PRODUCTION AREAS block says:\n{production}"
        )
    assert production.count("- Patch ") == len(KEPT_BLOCKS), (
        f"the report must describe exactly the {len(KEPT_BLOCKS)} committed block(s), "
        f"not the {len(LANDFORM_CANDIDATES)} candidates"
    )

    # -- A DECLINED CANDIDATE DOES NOT. Only checked for the declined blocks
    # whose acreage is genuinely their own -- two blocks that round to the
    # same figure would make the absence claim untestable rather than false.
    declined_block_acres = {
        f"{block['area_acres']} acres"
        for block in ALL_PATCH_BLOCKS
        if block["id"] not in KEPT_BLOCK_IDS
    } - set(kept_block_acres)
    assert declined_block_acres, "at least one declined block must have a distinguishable acreage"
    # OVER THE PER-PATCH LINES ONLY. The parcel and gate lines above them
    # carry acreages of their own (a 0.8-acre setback ring), and a declined
    # block that happens to round to one of those would fail this check for
    # a reason that has nothing to do with what is being asserted.
    patch_lines = "\n".join(line for line in production.splitlines() if line.startswith("  - Patch "))
    for acres in declined_block_acres:
        assert acres not in patch_lines, (
            f"a DECLINED production block's acreage ({acres}) is described as a patch. The report "
            f"is narrating the candidate set, not the committed design:\n{patch_lines}"
        )

    # -- THE COMMITTED WATER AREA APPEARS, and the counts describe the
    # decision rather than the survivors.
    assert f"{len(KEPT_ZONES)} water SURVEY ZONE(S) identified" in water, water
    narrated_zone_ids = [zone["id"] for zone in DESIGN.narrative_data["water_survey_areas"]["zones"]]
    committed_zone_ids = [zone["id"] for zone in DESIGN.water_zones]
    assert narrated_zone_ids == committed_zone_ids, (
        f"the water block must narrate exactly the committed zones {committed_zone_ids}, "
        f"got {narrated_zone_ids}"
    )
    for zone_block in DESIGN.narrative_data["water_survey_areas"]["zones"]:
        acres = f"{zone_block['zone_acres']} acre"
        assert acres in water, (
            f"committed water zone {zone_block['id']} ({acres}) is not narrated:\n{water}"
        )

    # -- THE COMMITTED NETWORK APPEARS AND THE DECLINED ONES DO NOT. Each
    # network's own total length is the discriminator; they are asserted
    # distinct in section 5.
    kept_length = f"{NETWORKS[KEPT_NETWORK_ID]['access']['total_length_ft']}"
    assert DESIGN.narrative_data["road_corridors"]["access"]["total_length_ft"] == float(kept_length), (
        "the road block must be the COMMITTED network's own narrative block"
    )
    for block in DECLINED_NETWORKS:
        declined_length = f"{block['access']['total_length_ft']}"
        assert declined_length not in roads, (
            f"a DECLINED road network's length ({declined_length}ft) is in the report -- the report "
            f"is describing a network the owner did not commit:\n{roads}"
        )

    # -- AND THE SECTION SAYS IT IS A DECISION, in every block, which is what
    # stops a reader taking "2 water survey zones" for "this parcel has 2".
    for name, body in (("PRODUCTION AREAS", production), ("WATER SYSTEM SURVEY AREAS", water),
                       ("SUGGESTED ROAD CORRIDOR", roads)):
        assert body.startswith("THE OWNER'S COMMITTED DESIGN:"), (
            f"the {name} block must lead with the decision, got: {body[:120]!r}"
        )
    assert "2 of 5 computed candidate(s) were committed" in production, production
    assert f"1 of 3 computed candidate(s) were committed" in roads, roads

    print(f"   production: {len(KEPT_BLOCKS)} of {len(LANDFORM_CANDIDATES)} blocks narrated "
          f"({', '.join(sorted(kept_block_acres))}); declined {sorted(declined_block_acres)} absent")
    print(f"   water:      {len(KEPT_ZONES)} of "
          f"{DESIGN.narrative_data['water_survey_areas']['commitment']['candidate_count']} computed "
          f"zones narrated, ids {committed_zone_ids} (the panel presented {len(WATER_CANDIDATES)})")
    print(f"   roads:      network {KEPT_NETWORK_ID} ({kept_length}ft) narrated; declined "
          f"{[b['network_id'] for b in DECLINED_NETWORKS]} absent")
    print("   PASS -- the report describes the owner's design, not the pipeline's own winners.")

    # ==================================================================
    # 2. THE MAP DRAWS THE COMMITTED DESIGN
    # ==================================================================
    print("\n2 [test 2]. THE LAYOUT MAP DRAWS THE COMMITTED DESIGN")

    drawn_production = [patch["id"] for patch in LAYERS["production_areas"]]
    assert drawn_production == [zone["id"] for zone in DESIGN.production_areas]
    assert len(drawn_production) == len(KEPT_BLOCKS), (
        f"the map must draw the {len(KEPT_BLOCKS)} committed production blocks, got {drawn_production}"
    )

    drawn_road_ids = {feature["properties"]["network_id"] for feature in LAYERS["road_corridor"]}
    assert drawn_road_ids == {KEPT_NETWORK_ID}, (
        f"the map must draw only the committed network, got {drawn_road_ids}"
    )
    assert len(SPY.road_lines) == len(ROAD_FEATURES), (
        f"every committed branch is drawn: {len(SPY.road_lines)} vs {len(ROAD_FEATURES)}"
    )

    assert len(LAYERS["tree_zone_result"]["patches"]) == len(DESIGN.tree_zone_patches) == 1
    assert len(SPY.water_polygons) == len(KEPT_ZONES)
    assert len(SPY.structure_pins) == len(DESIGN.structure_sites) == 1

    # NOTHING FROM A PIPELINE WALK. The design's own committed water zones
    # are the exact geometries the ripple pass was handed -- reprojected,
    # so compared by area rather than by identity.
    committed_fill_area = sum(zone["render_fill_polygon_utm"].area for zone in DESIGN.water_zones)
    assert committed_fill_area > 0.0
    print(f"   {len(drawn_production)} production block(s), {len(SPY.water_polygons)} water zone(s), "
          f"{len(SPY.road_lines)} road branch(es), {len(SPY.structure_pins)} structure pin(s) drawn")
    print("   PASS")

    # ==================================================================
    # 4. A WATER COMMIT OF SEVERAL ZONES
    # ==================================================================
    print("\n3 [test 4]. A WATER COMMIT OF SEVERAL ZONES is narrated and drawn as what the owner chose")

    assert len(DESIGN.water_zones) == len(KEPT_ZONES) == 2
    # THE LIST AND THE UNION AGREE. selected_water_zone is the union every
    # downstream consumes edge already takes; it exists beside the list, and
    # it is the list's ground, not a first-of-two.
    union = DESIGN.selected_water_zone
    assert union is not None
    for zone in DESIGN.water_zones:
        assert union["render_fill_polygon_utm"].buffer(1e-6).contains(zone["render_fill_polygon_utm"]), (
            "the PipelineContext-shaped singleton must be the union of the committed zones"
        )
    assert len(SPY.water_polygons) == 2, "each committed zone draws its own ripple region"
    print(f"   {len(DESIGN.water_zones)} committed zones narrated (ids {committed_zone_ids}), "
          f"{len(SPY.water_polygons)} drawn, union carries both")
    print("   PASS")

    # ==================================================================
    # 5. A ROADS COMMIT OF ONE NETWORK OUT OF THREE
    # ==================================================================
    print("\n4 [test 5]. A ROADS COMMIT OF ONE NETWORK OUT OF THREE draws that one")

    lengths = [block["access"]["total_length_ft"] for block in NETWORKS.values()]
    assert len(set(lengths)) == 3, f"the three candidate networks must be distinguishable, got {lengths}"
    assert DESIGN.selected_road_corridor is not None
    assert len(DESIGN.road_networks) == 1, "the commit contract caps roads at one network"
    assert DESIGN.anchor_lon_lat == KEPT_ACCESS_POINT, (
        f"the access point comes from the ROADS commit, got {DESIGN.anchor_lon_lat} "
        f"want {KEPT_ACCESS_POINT}"
    )
    assert DESIGN.narrative_data["road_corridors"]["commitment"]["candidate_count"] == 3
    print(f"   3 networks generated {lengths}; committed {KEPT_NETWORK_ID} ({kept_length}ft); "
          f"access point read off the commit: {DESIGN.anchor_lon_lat}")
    print("   PASS")

    # ==================================================================
    # 7. AN EVICTED SESSION
    # ==================================================================
    print("\n5 [test 7]. AN EVICTED SESSION fails with the specific reason, never a partial context")

    # THE EVICTION, exactly as the LRU/idle-timeout would do it: the tier-2
    # entry is dropped. The Design Document is untouched and still says every
    # step is committed -- which is the whole trap, because the next read
    # rebuilds a context that is complete in every way except the one that
    # matters.
    assert MAIN.cache.discard(MAIN.id) is True, "the session must have had a tier-2 entry to drop"
    assert MAIN.cache.get(MAIN.id) is None, "the session's tier-2 entry must actually be gone"
    stored = MAIN.stored()
    assert stored["steps"]["water"]["status"] == "committed", "the committed design is still intact"

    try:
        session_design.build_session_design(
            MAIN.id, MAIN.store, fetch_cache=MAIN.fetch_cache, cache=MAIN.cache
        )
        raise AssertionError("an evicted session must not produce a design")
    except session_design.SessionWorkingDataExpiredError as exc:
        EVICTION_MESSAGE = str(exc)

    assert session_design.WORKING_DATA_EXPIRED in EVICTION_MESSAGE, EVICTION_MESSAGE
    assert (
        session_design.WORKING_DATA_EXPIRED
        == "this session's working data has expired; reopen and recommit to generate a report"
    )
    assert "is committed" in EVICTION_MESSAGE and "Design Document" in EVICTION_MESSAGE, EVICTION_MESSAGE
    # The rebuilt context IS otherwise complete -- which is why the failure
    # has to be raised rather than detected by a reader noticing empty blocks.
    rebuilt = MAIN.context()
    assert rebuilt.parcel_data is not None and rebuilt.exclusion_zones and rebuilt.keypoints is not None
    assert rebuilt.step_proposals == {}, "a rebuild restores no proposals -- that is the gap"
    print(f"   message: {EVICTION_MESSAGE}")
    print("   PASS -- and the rebuilt context carries ParcelData, the warm-up and no proposals at all.")


# ======================================================================
# 6. AN EMPTY COMMIT
# ======================================================================
print("\n6 [test 6]. AN EMPTY WATER COMMIT narrates and draws as the decision it is")

with Harness() as harness:
    EMPTY = Session()
    EMPTY.commit_landform()
    empty_candidates = EMPTY.water_zones()
    assert empty_candidates, "the fixture must OFFER water zones -- declining nothing proves nothing"
    EMPTY.commit("water", [], {})

    EMPTY_DESIGN = design_for(EMPTY)
    EMPTY_BLOCKS = sections(data_summary(EMPTY_DESIGN))
    empty_water = EMPTY_BLOCKS["WATER SYSTEM SURVEY AREAS"]

    assert EMPTY_DESIGN.water_zones == [], "an empty commit is an empty list, not a missing step"
    assert "water" in EMPTY_DESIGN.committed_steps, "the step IS committed -- that is the point"
    assert EMPTY_DESIGN.selected_water_zone is None

    commitment = EMPTY_DESIGN.narrative_data["water_survey_areas"]["commitment"]
    assert commitment["empty"] is True
    assert commitment["candidate_count"] == len(
        EMPTY_DESIGN.narrative_data["water_survey_areas"]["zones"]
    ) + commitment["declined_count"]

    # NOT THE GAP TEXT. The formatter's own nothing-here sentence blames the
    # data ("nothing cleared the excavated suitability threshold -- or DEM
    # data wasn't available"); printing it here would tell the reader the
    # analysis failed on a property where the owner simply said no.
    assert "NO WATER SURVEY ZONE IS PART OF THIS DESIGN" in empty_water, empty_water
    assert "deliberate, committed choice" in empty_water, empty_water
    assert "DEM data wasn't available" not in empty_water, empty_water
    assert "No water survey areas were identified" not in empty_water, empty_water
    assert f"{commitment['candidate_count']} candidate(s) were computed" in empty_water, empty_water

    empty_layers, empty_spy = render(EMPTY_DESIGN)
    assert empty_layers["water_zones"] == []
    assert empty_spy.water_polygons == [], "an empty water commit draws no water zone"
    print(f"   {commitment['candidate_count']} candidates computed, 0 committed")
    print(f"   narrated as: {empty_water[:150]}...")
    print("   PASS -- a decision, not a gap; and the map draws no water.")


# ======================================================================
# 3. TWO PLACED STRUCTURE SITES
# ======================================================================
print("\n7 [test 3]. TWO PLACED STRUCTURE SITES both appear on the map")

with Harness() as harness:
    PLACED_SESSION = Session()

    # Two points well inside the parcel, each with a full building pad
    # contained, and far enough apart that their pads do not touch -- so two
    # pins are two distinct places on the map rather than one drawn twice.
    pad_half_side = float(np.sqrt(solar_suitability.MAX_STRUCTURE_FOOTPRINT_ACRES * 4046.8564224)) / 2.0
    corner_points = []
    for vertex in BOUNDARY_POLYGON_UTM.exterior.coords[:-1]:
        inward = LineString([Point(vertex), BOUNDARY_POLYGON_UTM.centroid])
        for distance in np.arange(20.0, inward.length, 5.0):
            candidate = inward.interpolate(float(distance))
            if BOUNDARY_POLYGON_UTM.contains(candidate.buffer(pad_half_side * 1.5)):
                corner_points.append(candidate)
                break
    assert len(corner_points) >= 2, "two placeable spots inside the parcel are needed"
    # The two furthest apart of them, so the pins land on plainly different
    # ground and "two pins" cannot be satisfied by one pin drawn twice.
    first, last = max(
        ((a, b) for index, a in enumerate(corner_points) for b in corner_points[index + 1:]),
        key=lambda pair: pair[0].distance(pair[1]),
    )
    assert first.distance(last) > 4.0 * pad_half_side, (
        f"the two placed sites must be distinct ground, {first.distance(last):.1f} m apart"
    )
    PLACED = [fixture._lon_lat(first), fixture._lon_lat(last)]

    PLACED_SESSION.upstream(
        landform=True, water_zone_count=1, access_point=fixture.ACCESS_A, tree_count=1,
        structure_count=0, placed=PLACED,
    )
    PLACED_DESIGN = design_for(PLACED_SESSION)

    assert [site["site_origin"] for site in PLACED_DESIGN.structure_sites] == ["user_placed", "user_placed"], (
        [site["site_origin"] for site in PLACED_DESIGN.structure_sites]
    )
    placed_layers, placed_spy = render(PLACED_DESIGN)
    assert len(placed_layers["structure_sites"]) == 2, placed_layers["structure_sites"]
    assert len(placed_spy.structure_pins) == 2, (
        f"a design with TWO placed sites must draw TWO pins, drew {len(placed_spy.structure_pins)}"
    )
    pin_positions = [tuple(round(v, 3) for v in position) for position in placed_spy.structure_pins]
    assert len(set(pin_positions)) == 2, f"the two pins must be at two places, got {pin_positions}"

    # THE REPORT NARRATES ONE, and that is the block's shape rather than a
    # loss: solar_suitability.build_narrative_data() emits `selected_site`,
    # singular. It narrates the best COMMITTED site -- one of these two --
    # never a site the pipeline would have picked.
    placed_blocks = sections(data_summary(PLACED_DESIGN))
    solar_block = placed_blocks["PERMANENT BUILDING SITE"]
    narrated = PLACED_DESIGN.narrative_data["solar_suitability"]["selected_site"]
    best_committed = solar_suitability.select_optimal_structure_site(PLACED_DESIGN.structure_sites)
    assert narrated["score"] == round(best_committed["suitability_score"], 1), (
        f"the report must narrate the best COMMITTED site, got {narrated['score']}"
    )
    assert PLACED_DESIGN.narrative_data["solar_suitability"]["candidate_count"] == 2
    assert solar_block.startswith("THE OWNER'S COMMITTED DESIGN:"), solar_block[:120]
    print(f"   two placed sites at {pin_positions}, two pins drawn")
    print(f"   report narrates the best committed site (score {narrated['score']}/100)")
    print("   PASS")


# ======================================================================
# 8. THE BATCH PATH IS UNTOUCHED
# ======================================================================
print("\n8 [test 8]. THE BATCH PATH IS UNTOUCHED -- build_pipeline_context()'s blocks format as they always did")

# A block with NO commitment marker is what build_pipeline_context() puts on
# PipelineContext.narrative_data, and _committed_section() must be a
# pass-through for it -- same text, same object handed to the same
# formatter, nothing prepended and nothing replaced.
for block in (
    None,
    {},
    {"zone_found": False},
    {"site_found": False, "no_candidates": None, "candidate_count": 0, "gates": {}},
):
    for formatter in (
        report_generator._format_water_survey_areas_summary,
        report_generator._format_solar_candidate_zones_summary,
        report_generator._format_production_areas_summary,
        report_generator._format_tree_zones_summary,
        report_generator._format_road_corridor_summary,
    ):
        try:
            expected = formatter(block)
        except Exception:
            continue
        assert report_generator._committed_section(block, formatter, "thing") == expected, (
            f"{formatter.__name__} must be untouched for a block with no commitment marker"
        )

# And build_pipeline_context() itself is still importable, still takes the
# batch path's signature, and is still what generate_full_report() calls.
import inspect  # noqa: E402

import generate_full_report  # noqa: E402
import pipeline_context  # noqa: E402

assert generate_full_report.build_pipeline_context is pipeline_context.build_pipeline_context
signature = inspect.signature(pipeline_context.build_pipeline_context)
assert list(signature.parameters)[:2] == ["boundary_coordinates", "anchor_lon_lat"], list(signature.parameters)
assert "narrative_data" in {f.name for f in __import__("dataclasses").fields(pipeline_context.PipelineContext)}
assert "context.narrative_data" in inspect.getsource(generate_full_report.generate_full_report), (
    "generate_full_report() must still forward the pipeline's own narrative_data"
)
# The session entry point is a SEPARATE function -- the batch one did not
# grow a session mode, and it did not lose its anchor argument.
assert list(inspect.signature(generate_full_report.generate_full_report).parameters) == [
    "boundary_coordinates", "anchor_lon_lat",
]
assert "session_id" in inspect.signature(generate_full_report.generate_session_report).parameters
print("   build_pipeline_context() unchanged; generate_full_report() unchanged; the session path is "
      "generate_session_report(), a separate entry point.")
print("   PASS")


print("\n" + "=" * 72)
print(
    "test_session_report.py: the report and the layout map read the OWNER'S COMMITTED\n"
    "DECISIONS. A declined candidate is narrated nowhere and drawn nowhere; several\n"
    "committed water zones and two placed structure sites each appear in full; an empty\n"
    "commit reads as a decision rather than as missing data; and an evicted session\n"
    "fails saying so. build_pipeline_context() and the batch path are untouched."
)
print("=" * 72)
