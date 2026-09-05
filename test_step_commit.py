"""
test_step_commit.py

The COMMIT and REOPEN paths, end to end, run as:

    python test_step_commit.py

REAL COORDINATES, REAL PIPELINE CODE. The boundary is the actual drawn
property from generate_full_report.py -- 5614 N Montour Rd, Gibsonia, PA
(~13.23 acres, UTM 17N / EPSG:32617) -- the SAME six lon/lat pairs
test_session_manager.py (B2), test_wire_translation_inbound.py (B4) and
test_step_orchestrator.py (B5a) use, so every figure printed below is
comparable with theirs. session_manager.create_session(), the terrain
warm-up, identify_exclusion_zones(), identify_optimized_production_areas(),
the rehydrator and the whole payload assembly all RUN, for real. They are
wrapped (wraps=) to be COUNTED, never replaced.

What is mocked is the NETWORK and only the network -- B5a's harness shape,
reused rather than reinvented. An assertion that a count is zero only means
something if a nonzero count was reachable, so every layer fetch stays a
real, working path that would fire if an override stopped being forwarded.

THE TERRAIN IS A FIXTURE and the acreages it produces are meaningless as
statements about the real parcel. Nothing here asserts a real-property
number; every assertion is about SHAPE, COUNTS and INVARIANTS.

THE ONE THING THIS FILE EXISTS TO PROVE, above all the rest: section 6. A
committed zone that overlaps an exclusion mask is COMMITTED SUCCESSFULLY,
with the crossing recorded. That is where the settled contract differs from
the written architecture proposal -- section 2.5 had the server re-validate
a commit against the eligibility masks and reject it; the shipped frontend's
posture, which is the settled one, makes the parcel boundary the ONLY hard
gate and treats every exclusion gate as advisory. See commit_validation.py's
docstring for the argument.

Sections:
  1.  SUBSET COMMIT -- generated proposals, some selected.
  2.  A USER-DRAWN ZONE -- rehydrated, provenance user_added, and NO scoring
      field invented for it.
  3.  EMPTY COMMIT -- zero features, status committed, distinguishable from
      not_started. A decision, not an error.
  4.  REJECTION, BOUNDARY -- a zone partly off-parcel, named, nothing written.
  5.  REJECTION, VALIDITY -- a self-intersecting ring, a per-feature reason,
      not a 500. And the provenance the system no longer accepts.
  6.  ACCEPTANCE, EXCLUSION CROSSING -- the branch's most important test.
  7.  CROSSING AGREEMENT -- against a direct port of zoneGeometry.js's
      cautionsFor(), floor included.
  8.  REVISION CONFLICT -- carrying the current document.
  9.  THE KEYPOINT HOOK -- declared in the registry, run after the commit.
  10. ID STABILITY -- two generates, identical feature id sets. The restore
      path in section 11 does not work without this.
  11. REOPEN RESTORE -- proposals back, prior selection restored, drawn zone
      present.
  12. CASCADE INVALIDATION -- and the SOURCE_COMMITTED resolver, both against
      a synthetic second registry entry.
  13. NO NETWORK -- counted, not timed.
"""

import copy
import math
import tempfile
from contextlib import ExitStack
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon, shape

import canopy_height_data
import commit_validation
import design_document
import exclusion_zones
import farm_roads_data
import job_runner
import keypoint_detection
import parcel_data
import production_area
import production_area_ceiling
import session_cache
import session_manager
import step_orchestrator
import step_registry
import valley_delineation
import wire_translation
from dem_data import _utm_epsg_for_lonlat
from document_store import JSONFileStore
from parcel_data import ParcelData
from raster_grid import SQUARE_METERS_PER_ACRE

# --- the real property, verbatim from B2, B4 and B5a -----------------

REAL_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

_mean_lon = sum(lon for lon, _ in REAL_BOUNDARY) / len(REAL_BOUNDARY)
_mean_lat = sum(lat for _, lat in REAL_BOUNDARY) / len(REAL_BOUNDARY)
CRS = f"EPSG:{_utm_epsg_for_lonlat(_mean_lon, _mean_lat)}"
_xs, _ys = warp_transform(
    "EPSG:4326", CRS, [lon for lon, _ in REAL_BOUNDARY], [lat for _, lat in REAL_BOUNDARY]
)
BOUNDARY_POLYGON_UTM = Polygon(zip(_xs, _ys))
PARCEL_ACRES = BOUNDARY_POLYGON_UTM.area / SQUARE_METERS_PER_ACRE

# --- the DEM fixture, B5a's verbatim ---------------------------------

RESOLUTION_METERS = 5.0
BUFFER_METERS = 100.0
_minx, _miny, _maxx, _maxy = BOUNDARY_POLYGON_UTM.bounds
ORIGIN_X = _minx - BUFFER_METERS
ORIGIN_Y = _maxy + BUFFER_METERS
COLS = int(np.ceil((_maxx - _minx + 2 * BUFFER_METERS) / RESOLUTION_METERS))
ROWS = int(np.ceil((_maxy - _miny + 2 * BUFFER_METERS) / RESOLUTION_METERS))
_centroid = BOUNDARY_POLYGON_UTM.centroid
CHANNEL_COL = int(round((_centroid.x - ORIGIN_X) / RESOLUTION_METERS))
KNEE_ROW = int(round((ORIGIN_Y - _centroid.y) / RESOLUTION_METERS))


def _build_dem() -> dict:
    """A 4% bench with one incised drainage down CHANNEL_COL -- B5a's fixture,
    so the proposals committed here are the proposals that branch asserted
    over -- PLUS flanking levee ridges whose spacing narrows to a throat at
    KNEE_ROW.

    THE THROAT IS NOT DECORATION, AND IT IS NOT LANDFORM'S. It is what the
    EMBANKMENT survey type needs in order to exist at all, and this harness is
    the one every consumer of this file drives: serve_test_backend.py serves
    it, and the frontend's water.test.jsx runs its whole end-to-end suite
    through that server.

    B5a's prism has a CONSTANT CROSS-SECTION, so an embankment seed's pinch
    walk finds no crest-to-crest width minimum anywhere and every seed fails
    honestly -- `no_constriction`, which is the correct reading of a landform
    with no pinch in it. The consequence was ZERO embankment zones from this
    harness while test_water_step.py's own fixture (which grew the throat when
    the compartment change landed, and says so in its own docstring) produced
    four from the same boundary. Six frontend tests then failed on a terrain
    gap rather than on anything either side had got wrong: no embankment rank 1
    to name, no second type to draw into its own pane, and no cross-type
    agreement to report.

    The levees are LOCAL BUMPS BESIDE ONE REACH OF THE CHANNEL -- 2.5 m at
    13 cells (65 m) off-channel, closing to 9 cells (45 m) at the throat, and
    fading out along the valley either side of it -- so the bench's own
    landform ground is left alone and the incision, the gradient and the
    exclusion fixtures below are B5a's, unchanged. Three zones still come out
    of the landform generate, as they did before; the water generate that
    followed it now returns five embankment zones and three excavated ones,
    where it returned none and three.

    THREE THINGS ABOUT THE SHAPE, AND ALL THREE ARE THIS FILE'S OTHER
    FIXTURES RATHER THAN A VIEW ABOUT THE LANDFORM.

    THEY SIT FURTHER OFF THE CHANNEL THAN test_water_step.py'S (9/13 cells,
    not 5/8). HYDRIC_ZONE_RING (section 6, the most important test here) sits
    5 to 7 cells west of the channel, and the hydric gate is derived as
    `slope_ok & disqualifying_soil` -- exclusion_zones.py says so at length --
    so a levee crest laid over that ground turns those cells too steep, drops
    them out of the HYDRIC layer, and leaves the section asserting a crossing
    that has quietly become a slope one. Pushed out, the two fixtures do not
    touch: the hydric layer, its crossing with HYDRIC_ZONE_RING (0.089 acres)
    and the graze ring's above/below-floor pair are all identical to what
    B5a's bare prism produced.

    THEY FADE OUT ALONG THE VALLEY rather than running the parcel's whole
    length, and that is what a throat IS -- a valley narrows at a reach, and a
    ridge pair running end to end is a canyon. It is also what the ROADS
    fixtures need: two of the four surveyed access points sit OUTSIDE the
    western crest line, and a 2.5 m ridge across their whole approach is a
    grade the router cannot cross, so a full-length pair returned
    network_found false from points that had always routed. At 100 m of reach
    the crest is about a metre where those two points are -- 1.2 m and 0.9 m --
    and the routes are back.

    THEY ARE THE SAME EXPRESSION test_water_step.py USES otherwise. Two
    harnesses over one boundary that disagree about its terrain are two
    reference parcels, and the whole value of this one is that it is the
    other's.
    """
    rows = np.arange(ROWS)[:, None].astype(np.float32)
    cols = np.arange(COLS)[None, :].astype(np.float32)
    array = 300.0 + 0.20 * rows + 0.05 * cols
    array -= 9.0 * np.exp(-((cols - CHANNEL_COL) ** 2) / (2 * 3.0 ** 2))
    levee_offset = 9.0 + 4.0 * (1.0 - np.exp(-((rows - KNEE_ROW) ** 2) / (2 * 8.0 ** 2)))
    levee_height = 2.5 * np.exp(-((rows - KNEE_ROW) ** 2) / (2 * 20.0 ** 2))
    for side in (-1, 1):
        array = array + levee_height * np.exp(
            -((cols - (CHANNEL_COL + side * levee_offset)) ** 2) / (2 * 2.0 ** 2)
        )
    return {
        "array": array.astype(np.float32),
        "resolution_meters": (RESOLUTION_METERS, RESOLUTION_METERS),
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def _build_canopy(dem: dict) -> dict:
    hag = np.zeros((ROWS, COLS), dtype=np.float32)
    hag[KNEE_ROW - 6 : KNEE_ROW + 8, CHANNEL_COL + 14 : CHANNEL_COL + 26] = 15.0
    return {
        "array": hag,
        "resolution_meters": dem["resolution_meters"],
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
        "source_item_id": "fixture-hag",
    }


HYDRIC_COMPONENTS = [
    {
        "mukey": "111111",
        "comppct_r": "85",
        "hydricrating": "Yes",
        "compname": "Fixture silt loam",
    }
]
HYDRIC_GEOMETRIES = {
    "111111": {
        "type": "Polygon",
        "coordinates": [
            [
                [-79.9830, 40.6434],
                [-79.9822, 40.6434],
                [-79.9822, 40.6439],
                [-79.9830, 40.6439],
                [-79.9830, 40.6434],
            ]
        ],
    }
}
FIXTURE_ROADS = [
    {
        "name": "Fixture Rd",
        "geometry": {
            "type": "LineString",
            "coordinates": [[-79.9840, 40.6436], [-79.9805, 40.6436]],
        },
    }
]


# --- a second DEM, for the keypoint hook -----------------------------
#
# B5a's bench-and-drainage fixture above produces production zones and NO
# KEYPOINTS: its long profile down the drainage is straight, and a keypoint
# is by definition a slope INFLECTION. Section 9 needs both -- committed
# production areas to measure to, and keypoints to measure from -- so it runs
# over test_session_manager.py's (B2's) fixture instead, verbatim: one
# V-shaped valley draining north to south through the parcel centroid with a
# deliberate slope break at the centroid row, ~30% above and ~3% below. That
# break is what a keypoint IS, and B2 asserts it produces exactly one inside
# the parcel.
#
# TWO FIXTURES RATHER THAN ONE COMPROMISE. A single DEM tuned to yield both
# would be a shape neither branch had asserted anything against; taking each
# from the branch that established it keeps every figure here comparable with
# the branch it came from.
WALL_RISE_PER_COL_M = 0.25  # 5% cross slope
STEEP_DROP_PER_ROW_M = 1.5  # 30% above the knee
GENTLE_DROP_PER_ROW_M = 0.15  # 3% below the knee


def _build_keypoint_dem() -> dict:
    array = np.zeros((ROWS, COLS), dtype=np.float32)
    for row in range(ROWS):
        if row < KNEE_ROW:
            drop = row * STEEP_DROP_PER_ROW_M
        else:
            drop = KNEE_ROW * STEEP_DROP_PER_ROW_M + (
                row - KNEE_ROW
            ) * GENTLE_DROP_PER_ROW_M
        for col in range(COLS):
            array[row, col] = (
                1000.0 + abs(col - CHANNEL_COL) * WALL_RISE_PER_COL_M - drop
            )
    return {
        "array": array,
        "resolution_meters": (RESOLUTION_METERS, RESOLUTION_METERS),
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def _build_parcel_data(_boundary=None) -> ParcelData:
    dem = _build_dem()
    return ParcelData(
        dem=dem,
        boundary_polygon_utm=BOUNDARY_POLYGON_UTM,
        soil_components=HYDRIC_COMPONENTS,
        farmland_classification=[],
        erosion_factor=[],
        saturated_hydraulic_conductivity=[],
        soil_geometries=HYDRIC_GEOMETRIES,
        water_features={"features": []},
        farm_roads=FIXTURE_ROADS,
        climate_summary={},
        elevation_grid=[],
        canopy_height=_build_canopy(dem),
        imagery_summary={},
        irradiance={"status": "ok"},
    )


def _build_keypoint_parcel_data(_boundary=None) -> ParcelData:
    """The same ParcelData over B2's keypoint-bearing DEM."""
    parcel = _build_parcel_data()
    dem = _build_keypoint_dem()
    parcel.dem = dem
    parcel.canopy_height = _build_canopy(dem)
    return parcel


# --- the harness -----------------------------------------------------


class Harness:
    """
    Every network boundary mocked, every real computation wrapped and
    counted. B5a's harness, with the rehydrator added so a commit's
    translation work can be counted too.
    """

    def __init__(self, fetch_side_effect=None):
        self._stack = ExitStack()
        self._fetch_side_effect = fetch_side_effect or _build_parcel_data

    def __enter__(self):
        patch = self._stack.enter_context

        self.fetch_parcel_data = patch(
            mock_patch.object(
                parcel_data, "fetch_parcel_data", side_effect=self._fetch_side_effect
            )
        )
        self.soil_components = patch(
            mock_patch.object(
                production_area, "get_soil_data_for_polygon",
                return_value=HYDRIC_COMPONENTS,
            )
        )
        self.soil_geometries = patch(
            mock_patch.object(
                production_area, "get_soil_geometries_for_polygon",
                return_value=HYDRIC_GEOMETRIES,
            )
        )
        self.canopy_refetch = patch(
            mock_patch.object(
                production_area, "get_canopy_height_for_boundary", return_value=None
            )
        )
        self.canopy_module_refetch = patch(
            mock_patch.object(
                canopy_height_data, "get_canopy_height_for_boundary", return_value=None
            )
        )
        self.roads_refetch = patch(
            mock_patch.object(
                farm_roads_data, "get_farm_roads_for_boundary",
                return_value=FIXTURE_ROADS,
            )
        )
        self.roads_helper_refetch = patch(
            mock_patch.object(
                production_area, "_fetch_road_exclusion_union_utm",
                wraps=production_area._fetch_road_exclusion_union_utm,
            )
        )
        self.dem_refetch = patch(
            mock_patch.object(
                production_area_ceiling, "get_dem_for_boundary",
                side_effect=AssertionError("get_dem_for_boundary() must not run"),
            )
        )
        self.delineate_valleys = patch(
            mock_patch.object(
                valley_delineation, "delineate_valleys",
                wraps=valley_delineation.delineate_valleys,
            )
        )
        self.keypoint_delineate_valleys = patch(
            mock_patch.object(
                keypoint_detection, "delineate_valleys",
                wraps=keypoint_detection.delineate_valleys,
            )
        )
        self.identify_exclusion_zones = patch(
            mock_patch.object(
                exclusion_zones, "identify_exclusion_zones",
                wraps=exclusion_zones.identify_exclusion_zones,
            )
        )
        self.identify_production = patch(
            mock_patch.object(
                production_area_ceiling, "identify_optimized_production_areas",
                wraps=production_area_ceiling.identify_optimized_production_areas,
            )
        )
        self.rehydrate = patch(
            mock_patch.object(
                wire_translation, "rehydrate_production_zones",
                wraps=wire_translation.rehydrate_production_zones,
            )
        )
        return self

    def __exit__(self, *exc_info):
        self._stack.close()
        return False

    @property
    def total_soil_queries(self) -> int:
        return self.soil_components.call_count + self.soil_geometries.call_count

    @property
    def total_network_calls(self) -> int:
        """Every mocked network boundary, summed. The honest total for a
        'zero network' claim -- a per-layer zero says nothing about the layer
        next to it."""
        return (
            self.fetch_parcel_data.call_count
            + self.total_soil_queries
            + self.canopy_refetch.call_count
            + self.canopy_module_refetch.call_count
            + self.roads_refetch.call_count
            + self.roads_helper_refetch.call_count
        )


def _fresh_caches():
    return session_cache.FetchCache(max_entries=8), session_cache.SessionCache(
        max_sessions=8, idle_timeout_seconds=1800.0
    )


def _fresh_store():
    return JSONFileStore(tempfile.mkdtemp(prefix="step_commit_test_"))


def _fresh_runner():
    return job_runner.JobRunner(max_workers=2, max_jobs=32)


class Session:
    """One created session plus the caches and store behind it, so a section
    reads as `s = Session()` rather than five lines of wiring."""

    def __init__(self):
        self.store = _fresh_store()
        self.fetch_cache, self.cache = _fresh_caches()
        self.runner = _fresh_runner()
        self.document = session_manager.create_session(
            REAL_BOUNDARY, self.store, fetch_cache=self.fetch_cache, cache=self.cache
        )
        self.id = self.document["session_id"]

    def generate(self, step_id="landform"):
        job = step_orchestrator.generate_step(
            self.id, step_id, self.store, fetch_cache=self.fetch_cache,
            cache=self.cache, runner=self.runner,
        ).wait(timeout=600)
        if job.status != job_runner.STATUS_DONE:
            raise AssertionError(f"generate failed: {job.error} ({job.exception!r})")
        # The PAYLOAD half; a done job carries {"payload", "document"}
        # (step_orchestrator.run_generate_job). These are commit tests -- the
        # document they care about is the one the commit returns.
        return job.result["payload"]

    def commit(self, features, provenance, base_revision, step_id="landform", inputs=None):
        return step_orchestrator.commit_step(
            self.id, step_id, features, provenance, base_revision, self.store,
            inputs=inputs, fetch_cache=self.fetch_cache, cache=self.cache,
        )

    def reopen(self, step_id="landform"):
        return step_orchestrator.reopen_step(
            self.id, step_id, self.store,
            fetch_cache=self.fetch_cache, cache=self.cache,
        )

    def context(self):
        return session_manager.get_session_context(
            self.id, self.store, fetch_cache=self.fetch_cache, cache=self.cache
        )

    def stored(self):
        return self.store.get(self.id)


# --- building a commit ------------------------------------------------

LAYER = wire_translation.LAYER_PRODUCTION_AREA


def _collection(features):
    return {"type": "FeatureCollection", "features": list(features)}


def _drawn(feature_id: str, ring_lon_lat, label="Drawn zone"):
    """
    A user-drawn zone in the shape the frontend commits: a schema-conformant
    Feature carrying the layer and the ring, and NOTHING a pipeline would
    have computed -- no rank, no suitability score, no slope range. What a
    drawing tool can honestly produce is a shape and a caption.
    """
    ring = [list(point) for point in ring_lon_lat]
    ring.append(list(ring_lon_lat[0]))
    return {
        "id": feature_id,
        "type": "Feature",
        "geometry": {"type": "Polygon", "coordinates": [ring]},
        "properties": {
            "layer": LAYER,
            "label": label,
            "confidence": "low",
            "confidence_notes": "Drawn by hand on the map; no survey backs it.",
        },
    }


def _rect(west, east, south, north):
    return [(west, south), (east, south), (east, north), (west, north)]


# A rectangle sitting INSIDE the parcel and squarely over the fixture's
# HYDRIC GATE FOOTPRINT -- 0.13 acres of ground the hydric gate excludes,
# about 0.09 of it hydric. Section 6 commits it; the settled contract says
# that commit succeeds with the crossing recorded, and the written proposal
# said it should be refused.
#
# It also grazes the slope and roads gates by 0.003 and 0.024 acres, both
# under the floor and therefore dropped by BOTH sides -- which is a second,
# free assertion that the floor is doing its job on real gate geometry.
HYDRIC_ZONE_RING = _rect(-79.98303, -79.98291, 40.64342, 40.64390)

# A rectangle just past the hydric footprint's northern edge: 0.09 acres of
# ground that genuinely crosses the hydric gate, by about 0.01 acres -- under
# the floor. Section 7 asserts both the server and the client drop that
# crossing, which is what a shared floor means. It crosses the SLOPE gate by
# 0.07 acres, well above the floor, so it is not a zone that misses every
# mask: the same commit records one crossing and drops the other.
HYDRIC_GRAZE_RING = _rect(-79.98295, -79.98275, 40.64380, 40.64400)

# A rectangle straddling the parcel's western edge: about half of it is off
# the property. Section 4 rejects it -- the one hard spatial gate there is.
OFF_PARCEL_RING = _rect(-79.9845, -79.9834, 40.6448, 40.6452)

# A BOWTIE: a self-intersecting ring, which is a valid GeoJSON Polygon
# coordinate array and not a valid polygon. Section 5 rejects it per feature
# rather than letting InboundGeometryError escape as a 500.
BOWTIE_RING = [
    (-79.9826, 40.6448),
    (-79.9820, 40.6452),
    (-79.9826, 40.6452),
    (-79.9820, 40.6448),
]


print(
    f"Real property: 5614 N Montour Rd, Gibsonia, PA -- {len(REAL_BOUNDARY)} "
    f"vertices, {PARCEL_ACRES:.2f} acres, {CRS}, {ROWS}x{COLS} DEM cells at "
    f"{RESOLUTION_METERS:.0f} m. Same boundary as test_session_manager.py (B2), "
    f"test_wire_translation_inbound.py (B4) and test_step_orchestrator.py "
    f"(B5a).\n"
)


# --- 1. SUBSET COMMIT -------------------------------------------------

with Harness() as h:
    s = Session()
    payload = s.generate()
    proposals = payload["suggested_zones"]["features"]
    assert len(proposals) >= 2, (
        f"the fixture must propose at least two zones for a SUBSET to mean "
        f"anything; got {len(proposals)}"
    )

    selected = proposals[:2]
    selected_ids = [feature["id"] for feature in selected]
    document = s.commit(
        _collection(selected),
        {feature_id: "generated" for feature_id in selected_ids},
        base_revision=0,
    )

    entry = document["steps"]["landform"]
    assert entry["status"] == design_document.STATUS_COMMITTED, entry["status"]
    assert entry["revision"] == 1, entry["revision"]
    assert [f["id"] for f in entry["features"]["features"]] == selected_ids, (
        "the committed features are the ones that were selected, in order"
    )
    assert all(value == "generated" for value in entry["provenance"].values())
    # THE SUBSET IS A SUBSET. A commit path that quietly committed every
    # proposal would pass every assertion above.
    assert len(entry["features"]["features"]) < len(proposals), (
        "this section commits a STRICT subset; committing all of them would "
        "make the selection meaningless"
    )
    # Persisted, not just returned.
    assert s.stored()["steps"]["landform"]["revision"] == 1
    design_document.validate_document(s.stored())

    committed_acres = sum(
        f["properties"]["area_acres"] for f in entry["features"]["features"]
    )

print(
    f"1. SUBSET COMMIT: {len(proposals)} proposals generated, {len(selected_ids)} "
    f"selected and committed ({committed_acres:.2f} acres) -- status "
    f"'{entry['status']}', step revision {entry['revision']}, provenance "
    f"'generated' on every committed feature. The document validates."
)


# --- 2. A USER-DRAWN ZONE ---------------------------------------------

with Harness() as h:
    s = Session()
    payload = s.generate()
    proposals = payload["suggested_zones"]["features"]

    drawn = _drawn("drawn-1", HYDRIC_ZONE_RING)
    features = _collection([proposals[0], drawn])
    provenance = {proposals[0]["id"]: "generated", "drawn-1": "user_added"}
    document = s.commit(features, provenance, base_revision=0)

    entry = document["steps"]["landform"]
    stored_drawn = next(f for f in entry["features"]["features"] if f["id"] == "drawn-1")
    assert entry["provenance"]["drawn-1"] == "user_added"

    # NO SCORING FIELD INVENTED. A drawn zone was never scored; 0.0 is a
    # legible suitability score meaning "worst possible ground" and a drawn
    # zone has not been scored rather than scored badly. The document must
    # carry what the user sent, plus the crossings, and nothing else.
    invented = [
        field
        for field in ("rank", "suitability_score", "slope_factor", "avg_slope_pct",
                      "aspect_deg", "source_patch_id")
        if field in stored_drawn["properties"]
    ]
    assert not invented, (
        f"a drawn zone must carry NO scoring fields; the commit path invented "
        f"{invented}"
    )
    assert set(stored_drawn["properties"]) == {
        "layer", "label", "confidence", "confidence_notes", "exclusion_crossings"
    }, sorted(stored_drawn["properties"])

    # AND IT REHYDRATES. The cached committed value is the internal shape a
    # downstream override takes, with the drawn zone in it carrying every
    # field a consumer reads -- and, again, no advisory block.
    cached = s.context().step_committed["landform"]
    assert cached["revision"] == 1
    drawn_patch = cached["value"][1]
    for field in ("polygon_utm", "render_fill_polygon_utm", "cells",
                  "representative_elevation_m", "area_acres", "geometry_wgs84"):
        assert field in drawn_patch, f"the rehydrated drawn zone lacks {field}"
    assert "suitability_score" not in drawn_patch
    # ITS INTERNAL ID DOES NOT COLLIDE with the selected proposal's, which is
    # the whole reason the commit path allocates one rather than letting the
    # rehydrator invent it.
    assert drawn_patch["id"] != cached["value"][0]["id"], (
        "a drawn zone must be allocated an internal id that no other zone in "
        "the same commit uses -- a collision silently merges their "
        "served-area accounting downstream"
    )

print(
    f"2. USER-DRAWN ZONE: committed alongside a selected proposal -- provenance "
    f"'user_added', rehydrated to a patch of {drawn_patch['area_acres']} acres "
    f"over {len(drawn_patch['cells'])} DEM cells at "
    f"{drawn_patch['representative_elevation_m']:.1f} m, internal id "
    f"{drawn_patch['id']} (the proposal's is {cached['value'][0]['id']}). NO "
    f"scoring field invented: properties are exactly "
    f"{sorted(stored_drawn['properties'])}."
)


# --- 3. EMPTY COMMIT --------------------------------------------------
#
# A DECISION, NOT AN ERROR. "No production ground on this parcel" is a real
# answer that water and roads downstream must receive AS an answer. The
# document's own governing distinction (design_document.py's docstring) is
# that this must never collapse into not_started.

with Harness() as h:
    s = Session()
    s.generate()
    before = s.stored()["steps"]["landform"]
    assert before["status"] == design_document.STATUS_GENERATED

    document = s.commit(_collection([]), {}, base_revision=0)
    entry = document["steps"]["landform"]

    assert entry["status"] == design_document.STATUS_COMMITTED, (
        f"an empty commit is COMMITTED, not {entry['status']!r}"
    )
    assert entry["features"] == {"type": "FeatureCollection", "features": []}
    assert entry["provenance"] == {}
    assert entry["revision"] == 1
    # DISTINGUISHABLE FROM not_started, in the document itself: a not_started
    # step carries ONLY its status and no revision at all.
    not_started = document["steps"]["water"]
    assert not_started == {"status": design_document.STATUS_NOT_STARTED}
    assert entry != not_started
    assert "features" in entry and "features" not in not_started
    design_document.validate_document(document)

    # AND IT REACHES A CONSUMER AS AN ANSWER. The committed value for an
    # empty commit is the rehydrator's own explicit empty -- [] for a list
    # shape -- and NEVER None, which every consumer reads as "not supplied,
    # go compute it yourself".
    empty_value = step_orchestrator.committed_internal_value(
        s.context(), document, "landform"
    )
    assert empty_value == [], f"an empty commit must rehydrate to [], got {empty_value!r}"
    assert empty_value is not None

print(
    f"3. EMPTY COMMIT: zero features committed -- status "
    f"'{entry['status']}' at revision {entry['revision']} carrying an empty "
    f"FeatureCollection, against a not_started step which carries "
    f"{sorted(not_started)} and nothing else. It reaches a consumer as [] "
    f"(a checked, empty answer), never as None."
)


# --- 4. REJECTION: BOUNDARY -------------------------------------------

with Harness() as h:
    s = Session()
    payload = s.generate()
    proposals = payload["suggested_zones"]["features"]

    off_parcel = _drawn("drawn-off", OFF_PARCEL_RING)
    features = _collection([proposals[0], off_parcel])
    provenance = {proposals[0]["id"]: "generated", "drawn-off": "user_added"}

    revision_before = s.stored()["document_revision"]
    entry_before = copy.deepcopy(s.stored()["steps"]["landform"])

    try:
        s.commit(features, provenance, base_revision=0)
        raise AssertionError("a zone partly outside the parcel must be rejected")
    except commit_validation.CommitRejectedError as exc:
        # REBOUND, because Python unbinds an `except ... as` name at the end
        # of the block and every assertion below reads the rejection set.
        rejected = exc
    boundary_rejections = [
        r for r in rejected.rejections
        if r.code == commit_validation.REJECT_OUTSIDE_BOUNDARY
    ]

    assert len(boundary_rejections) == 1, rejected.rejections
    assert boundary_rejections[0].feature_id == "drawn-off", (
        f"the rejection must NAME the offending feature: "
        f"{boundary_rejections[0].feature_id!r}"
    )
    # THE VALID FEATURE IS NOT BLAMED. A per-feature contract that rejected
    # the whole set without saying which one is the problem is the banner
    # this design exists to avoid.
    assert not [r for r in rejected.rejections if r.feature_id == proposals[0]["id"]]
    assert "outside the parcel boundary" in boundary_rejections[0].reason
    payload_shape = rejected.as_payload()
    assert payload_shape["rejections"][0]["feature_id"] == "drawn-off"
    assert set(payload_shape["rejections"][0]) == {"feature_id", "code", "reason"}

    # NOTHING WAS WRITTEN.
    after = s.stored()
    assert after["document_revision"] == revision_before, (
        f"a rejected commit must write NOTHING: document_revision "
        f"{revision_before} -> {after['document_revision']}"
    )
    assert after["steps"]["landform"] == entry_before, (
        "a rejected commit must leave the step exactly as it was"
    )
    assert after["steps"]["landform"]["status"] == design_document.STATUS_GENERATED

print(
    f"4. REJECTION -- BOUNDARY: a zone straddling the property line rejected "
    f"as {boundary_rejections[0].code!r}, naming feature "
    f"{boundary_rejections[0].feature_id!r}; the valid feature in the same "
    f"commit is not blamed. document_revision stayed at {revision_before} and "
    f"the step entry is byte-identical to before the attempt."
)


# --- 5. REJECTION: VALIDITY -------------------------------------------

with Harness() as h:
    s = Session()
    payload = s.generate()
    proposals = payload["suggested_zones"]["features"]

    bowtie = _drawn("drawn-bowtie", BOWTIE_RING)
    try:
        s.commit(
            _collection([bowtie]), {"drawn-bowtie": "user_added"}, base_revision=0
        )
        raise AssertionError("a self-intersecting ring must be rejected")
    except commit_validation.CommitRejectedError as exc:
        rejected = exc
    validity = [
        r for r in rejected.rejections
        if r.code == commit_validation.REJECT_INVALID_GEOMETRY
    ]
    assert len(validity) == 1, rejected.rejections
    assert validity[0].feature_id == "drawn-bowtie"
    # THE REASON IS THE REHYDRATOR'S OWN, naming the defect -- not a generic
    # "invalid geometry", and emphatically not a traceback.
    assert "not valid" in validity[0].reason, validity[0].reason
    assert "Self-intersection" in validity[0].reason, validity[0].reason
    assert "Traceback" not in validity[0].reason
    assert s.stored()["steps"]["landform"]["status"] == design_document.STATUS_GENERATED

    # A DEGENERATE RING -- three collinear points -- is the same class of
    # rejection through the same gate, and worth asserting separately because
    # it is the one the rehydrator catches by area rather than by validity.
    sliver = _drawn(
        "drawn-sliver",
        [(-79.9826, 40.6448), (-79.9822, 40.6448), (-79.9818, 40.6448)],
    )
    try:
        s.commit(_collection([sliver]), {"drawn-sliver": "user_added"}, base_revision=0)
        raise AssertionError("a degenerate ring must be rejected")
    except commit_validation.CommitRejectedError as exc:
        sliver_codes = [r.code for r in exc.rejections]
    
    assert sliver_codes == [commit_validation.REJECT_INVALID_GEOMETRY], sliver_codes

    # THE PROVENANCE NOTHING CAN EMIT. 'user_modified' was removed from
    # design_document.PROVENANCE_VALUES on this branch -- generated
    # candidates are SELECT-ONLY at every step, so a modified candidate
    # cannot arise -- and a commit carrying it is rejected by name.
    assert "user_modified" not in design_document.PROVENANCE_VALUES
    try:
        s.commit(
            _collection([proposals[0]]),
            {proposals[0]["id"]: "user_modified"},
            base_revision=0,
        )
        raise AssertionError("'user_modified' must be rejected")
    except commit_validation.CommitRejectedError as exc:
        provenance_rejections = list(exc.rejections)
    assert [r.code for r in provenance_rejections] == [
        commit_validation.REJECT_UNKNOWN_PROVENANCE
    ]
    assert "select-only" in provenance_rejections[0].reason

print(
    f"5. REJECTION -- VALIDITY: a self-intersecting ring rejected per feature "
    f"as {validity[0].code!r} carrying the rehydrator's own defect report, and "
    f"a collinear ring rejected the same way -- neither escapes as a 500. "
    f"Provenance 'user_modified' (removed from PROVENANCE_VALUES on this "
    f"branch) is rejected by name: accepted values are "
    f"{design_document.PROVENANCE_VALUES}."
)


# --- 6. ACCEPTANCE: EXCLUSION CROSSING --------------------------------
#
# THE MOST IMPORTANT TEST IN THIS FILE. A zone over the hydric mask is
# COMMITTED, and what it crosses is RECORDED. The written proposal (section
# 2.5) would have rejected it; the settled contract, taken from the shipped
# frontend, does not. If this test ever starts asserting a rejection, the
# contract has been silently reverted.

with Harness() as h:
    s = Session()
    payload = s.generate()

    hydric_zone = _drawn("drawn-hydric", HYDRIC_ZONE_RING, label="Over the wet ground")
    document = s.commit(
        _collection([hydric_zone]), {"drawn-hydric": "user_added"}, base_revision=0
    )

    entry = document["steps"]["landform"]
    assert entry["status"] == design_document.STATUS_COMMITTED, (
        "a zone crossing an exclusion gate is a VALID commit -- the parcel "
        "boundary is the only hard spatial gate"
    )
    stored = entry["features"]["features"][0]
    crossings = stored["properties"]["exclusion_crossings"]
    assert crossings, (
        "the zone sits on the fixture's hydric soil polygon; the crossing must "
        "be RECORDED, not merely tolerated"
    )
    crossed_types = [crossing["type"] for crossing in crossings]
    assert "hydric" in crossed_types, crossings
    hydric_crossing = next(c for c in crossings if c["type"] == "hydric")
    assert hydric_crossing["acres"] >= commit_validation.CROSSING_MIN_ACRES
    assert hydric_crossing["label"], "a crossing carries the gate's own prose"
    # THE LABEL IS THE GATE'S OWN, verbatim off the exclusion result's wire
    # block -- the same string the client already showed the user.
    wire_labels = {
        layer["type"]: layer["label"] for layer in payload["exclusion_layers"]
    }
    assert hydric_crossing["label"] == wire_labels["hydric"], (
        f"{hydric_crossing['label']!r} != {wire_labels['hydric']!r}"
    )
    # RECORDED IN THE GATES' OWN ORDER, and only for gates that were checked.
    assert crossed_types == [
        name for name in exclusion_zones.LAYER_ORDER if name in crossed_types
    ]

    # A SELECTED PROPOSAL CROSSES NOTHING, and that is not a vacuous zero:
    # a suggested zone is an opening of ground that already cleared every
    # gate, which zoneGeometry.js asserts as a DEV invariant on the same
    # data. An EMPTY list, present -- "checked, crosses nothing" -- never an
    # absent key.
    # REOPENED FIRST, because a committed step does not regenerate --
    # design_document.mark_step_generated() refuses to downgrade one, and
    # reopen is the verb that carries the cascade with it. The reopen's own
    # restore is what re-runs the generate, so the proposals come from there.
    s.reopen()
    clean = s.context().step_restored["landform"]["payload"]["suggested_zones"][
        "features"
    ][0]
    clean_document = s.commit(
        _collection([clean]), {clean["id"]: "generated"}, base_revision=1
    )
    clean_stored = clean_document["steps"]["landform"]["features"]["features"][0]
    assert clean_stored["properties"]["exclusion_crossings"] == [], (
        f"a suggested zone cleared every gate by construction: "
        f"{clean_stored['properties']['exclusion_crossings']}"
    )
    assert "exclusion_crossings" in clean_stored["properties"], (
        "always present -- [] says 'checked, crosses nothing' and an absent "
        "key would say 'this commit predates crossings being recorded'"
    )

_crossing_summary = ", ".join(
    f"{c['type']} {c['acres']} ac ({c['label']})" for c in crossings
)

print(
    f"6. ACCEPTANCE -- EXCLUSION CROSSING: a {stored['properties']['label']!r} "
    f"zone sitting on the hydric mask was COMMITTED (status "
    f"'{entry['status']}'), with the crossing recorded alongside it: "
    f"{_crossing_summary}. A selected proposal in the same session records []. "
    f"This is where the settled contract differs from the written proposal: "
    f"the exclusion gates are advisory and recorded, never refused."
)


# --- 7. CROSSING AGREEMENT WITH THE CLIENT ----------------------------
#
# A DIRECT PORT of zoneGeometry.js's cautionsFor(), run over the SAME zone
# and the SAME exclusion_layers the payload carries, and compared against
# what the server recorded. The port is line-for-line the JS: intersect per
# layer independently, skip a layer with data_available false or no
# geometry, measure with geo.js's own lon/lat area formula, drop anything
# under CAUTION_MIN_ACRES.
#
# WHAT MUST MATCH EXACTLY: which gates are crossed, and whether the floor
# dropped one. WHAT CANNOT: the acreage to the last decimal -- the client
# measures in lon/lat against a cosine-latitude scale and the server in the
# DEM's own UTM metres. The tolerance below is the projection difference and
# nothing else.

CAUTION_MIN_ACRES = 0.05  # zoneGeometry.js's own constant, at its own value
METRES_PER_DEGREE_LATITUDE = 111132.0  # geo.js
METRES_PER_DEGREE_LONGITUDE_AT_EQUATOR = 111320.0  # geo.js
SQUARE_METRES_PER_ACRE = 4046.8564224  # geo.js


def js_multi_polygon_area_acres(polygons) -> float:
    """
    geo.js multiPolygonAreaAcres(), ported. `polygons` is a list of polygons,
    each a list of rings, each a list of [lng, lat] -- polygon-clipping's own
    shape. One scale for the whole geometry, off the FIRST ring's mean
    latitude, because mixing scales between a polygon and its own hole would
    subtract an area measured in a different frame from the one it sits in.
    """
    if not polygons:
        return 0.0
    first = polygons[0][0]
    scale = math.cos(
        math.radians(sum(lat for _, lat in first) / len(first))
    )
    square_degrees = 0.0
    for polygon in polygons:
        for ring_index, ring in enumerate(polygon):
            double_area = 0.0
            for index in range(len(ring)):
                lng1, lat1 = ring[index]
                lng2, lat2 = ring[(index + 1) % len(ring)]
                double_area += lat1 * (lng2 * scale) - lat2 * (lng1 * scale)
            square_degrees += (1 if ring_index == 0 else -1) * abs(double_area) / 2
    square_metres = (
        square_degrees
        * METRES_PER_DEGREE_LATITUDE
        * METRES_PER_DEGREE_LONGITUDE_AT_EQUATOR
    )
    return square_metres / SQUARE_METRES_PER_ACRE


def _as_multi(geometry):
    """A shapely geometry -> polygon-clipping's [[ring, hole...], ...]."""
    if geometry.is_empty:
        return []
    parts = list(geometry.geoms) if geometry.geom_type.startswith("Multi") else [geometry]
    multi = []
    for part in parts:
        if part.geom_type != "Polygon":
            continue
        multi.append(
            [list(part.exterior.coords)]
            + [list(interior.coords) for interior in part.interiors]
        )
    return multi


def js_cautions_for(geometry_wgs84, exclusion_layers) -> list:
    """zoneGeometry.js cautionsFor(), ported. Returns [(type, acres), ...]."""
    drawn = shape(geometry_wgs84)
    cautions = []
    for layer in exclusion_layers:
        if not layer["data_available"] or not layer["geometry_wgs84"]:
            continue
        hit = drawn.intersection(shape(layer["geometry_wgs84"]))
        if hit.is_empty:
            continue
        acres = js_multi_polygon_area_acres(_as_multi(hit))
        if acres < CAUTION_MIN_ACRES:
            continue
        cautions.append((layer["type"], acres))
    return cautions


with Harness() as h:
    s = Session()
    payload = s.generate()
    exclusion_layers = payload["exclusion_layers"]

    hydric_zone = _drawn("drawn-hydric", HYDRIC_ZONE_RING)
    document = s.commit(
        _collection([hydric_zone]), {"drawn-hydric": "user_added"}, base_revision=0
    )
    recorded = document["steps"]["landform"]["features"]["features"][0]["properties"][
        "exclusion_crossings"
    ]

    client = js_cautions_for(hydric_zone["geometry"], exclusion_layers)
    assert [c["type"] for c in recorded] == [t for t, _ in client], (
        f"the server's recorded crossings and the client's cautions must name "
        f"the same gates: server {[c['type'] for c in recorded]} vs client "
        f"{[t for t, _ in client]}"
    )
    for server_crossing, (gate, client_acres) in zip(recorded, client):
        assert abs(server_crossing["acres"] - client_acres) <= 0.02 + 0.02 * client_acres, (
            f"gate {gate}: server {server_crossing['acres']} ac vs client "
            f"{client_acres:.4f} ac -- further apart than the projection "
            f"difference between UTM metres and geo.js's lon/lat scaling"
        )
    agreement = [
        f"{c['type']} {c['acres']:.2f}/{a:.2f}" for c, (_, a) in zip(recorded, client)
    ]

    # THE FLOOR IS THE SAME NUMBER, and it is REACHABLE. A zone crossing a
    # gate by less than the floor is dropped by BOTH -- so the document never
    # carries a caution the user was never shown, and the client never shows
    # one the document does not carry.
    assert commit_validation.CROSSING_MIN_ACRES == CAUTION_MIN_ACRES, (
        f"the server's crossing floor ({commit_validation.CROSSING_MIN_ACRES}) "
        f"must be zoneGeometry.js's CAUTION_MIN_ACRES ({CAUTION_MIN_ACRES})"
    )
    # A sliver of a crossing: a zone overlapping the hydric polygon by a
    # strip a few metres wide. Both sides drop it.
    sliver_zone = _drawn("drawn-graze", HYDRIC_GRAZE_RING)
    s.reopen()
    sliver_document = s.commit(
        _collection([sliver_zone]), {"drawn-graze": "user_added"}, base_revision=1
    )
    sliver_recorded = sliver_document["steps"]["landform"]["features"]["features"][0][
        "properties"
    ]["exclusion_crossings"]
    sliver_client = js_cautions_for(sliver_zone["geometry"], exclusion_layers)
    # The raw overlap exists -- measured without the floor -- and BOTH sides
    # drop it, which is what makes this a test of the floor rather than of a
    # zone that misses the mask.
    raw_hydric = shape(sliver_zone["geometry"]).intersection(
        shape(next(l for l in exclusion_layers if l["type"] == "hydric")["geometry_wgs84"])
    )
    raw_acres = js_multi_polygon_area_acres(_as_multi(raw_hydric))
    assert 0 < raw_acres < CAUTION_MIN_ACRES, (
        f"this zone must genuinely graze the hydric mask, below the floor, or "
        f"the floor is not what is being tested: {raw_acres:.4f} acres"
    )
    assert not [c for c in sliver_recorded if c["type"] == "hydric"], sliver_recorded
    assert not [c for c in sliver_client if c[0] == "hydric"], sliver_client

print(
    f"7. CROSSING AGREEMENT: server vs a direct port of zoneGeometry.js "
    f"cautionsFor() over the same zone and the same exclusion_layers -- same "
    f"gates, agreeing acreages ({', '.join(agreement)}; server UTM vs client "
    f"lon/lat). The floor is the same constant "
    f"({commit_validation.CROSSING_MIN_ACRES} acres) and both sides drop the "
    f"same {raw_acres:.4f}-acre graze."
)


# --- 8. REVISION CONFLICT ---------------------------------------------

with Harness() as h:
    s = Session()
    payload = s.generate()
    proposals = payload["suggested_zones"]["features"]
    first = _collection([proposals[0]])
    provenance = {proposals[0]["id"]: "generated"}

    s.commit(first, provenance, base_revision=0)
    s.reopen()

    # A second client still holding base_revision 0 -- the step is now at 1.
    try:
        s.commit(first, provenance, base_revision=0)
        raise AssertionError("a stale base_revision must raise")
    except design_document.RevisionConflictError as exc:
        conflict = exc  # rebound: the `as` name is unbound after the block

    assert conflict.step_id == "landform"
    assert conflict.expected == 1 and conflict.received == 0
    # CARRYING THE CURRENT DOCUMENT, so the caller can rebase without a
    # second round trip that could itself lose another race.
    assert conflict.document is not None, (
        "RevisionConflictError must carry the current document -- a client "
        "that has to refetch before it can retry is one race away from "
        "conflicting again"
    )
    assert conflict.document["steps"]["landform"]["revision"] == 1
    assert conflict.document["session_id"] == s.id
    design_document.validate_document(conflict.document)
    # A COPY, not the live document: a caller reading it out of the exception
    # cannot mutate what the raiser is still holding.
    conflict.document["steps"]["landform"]["status"] = "vandalised"
    assert s.stored()["steps"]["landform"]["status"] == design_document.STATUS_GENERATED

print(
    f"8. REVISION CONFLICT: a commit at base_revision "
    f"{conflict.received} against a step at revision {conflict.expected} "
    f"raised RevisionConflictError carrying the CURRENT document (session "
    f"{conflict.document['session_id'][:8]}..., document_revision "
    f"{conflict.document['document_revision']}) -- deep-copied, so a caller "
    f"cannot mutate the live one through it."
)


# --- 9. THE KEYPOINT HOOK ---------------------------------------------
#
# ON B2's FIXTURE, not B5a's -- see _build_keypoint_dem() for why: a keypoint
# is a slope inflection and the bench-and-drainage DEM has none.

with Harness(fetch_side_effect=_build_keypoint_parcel_data) as h:
    s = Session()
    payload = s.generate()
    proposals = payload["suggested_zones"]["features"]

    keypoints = s.context().keypoints
    assert keypoints, "the fixture must produce keypoints or this is vacuous"
    # NOT ATTACHED BEFORE THE COMMIT. The warm-up computes keypoints; the
    # relationship layer is derived from COMMITS and has nothing to derive
    # from yet.
    assert not any("feature_relationships" in kp for kp in keypoints), (
        "the relationship layer must not exist before a commit -- it is "
        "derived from committed features, not from the terrain warm-up"
    )

    document = s.commit(
        _collection(proposals[:2]),
        {f["id"]: "generated" for f in proposals[:2]},
        base_revision=0,
    )
    keypoints = s.context().keypoints

    for kp in keypoints:
        assert "feature_relationships" in kp, (
            "every keypoint must carry feature_relationships after the "
            "landform commit -- the hook is declared on the registry entry"
        )
        relationships = kp["feature_relationships"]
        assert relationships["nearest_production_area"]["status"] == "computed", (
            f"production areas are committed, so the relationship is computed: "
            f"{relationships['nearest_production_area']}"
        )
        assert "distance_m" in relationships["nearest_production_area"]
        assert "elevation_differential_m" in relationships["nearest_production_area"]
        # NO WATER STEP YET. "no_feature" is the truthful answer -- no water
        # zone has been selected -- not a placeholder.
        assert relationships["water_zone"]["status"] == "no_feature", (
            f"with no water commit the water half must read no_feature: "
            f"{relationships['water_zone']}"
        )
        assert "distance_m" not in relationships["water_zone"]

    # KEYPOINTS ARE NOT IN THE DOCUMENT, and this commit did not put them
    # there. They are a read-only context layer.
    assert "keypoints" not in document
    assert set(document["steps"]["landform"]) == {
        "status", "revision", "features", "provenance"
    }

    # IDEMPOTENT. The hook overwrites the key rather than appending, so
    # re-committing converges rather than accumulating -- which is what makes
    # it safe to declare on two steps.
    first_relationships = copy.deepcopy(keypoints[0]["feature_relationships"])
    s.reopen()
    s.commit(
        _collection(proposals[:2]),
        {f["id"]: "generated" for f in proposals[:2]},
        base_revision=1,
    )
    assert s.context().keypoints[0]["feature_relationships"] == first_relationships, (
        "the hook must be idempotent -- it overwrites its key, so a re-commit "
        "of the same features converges on the same answer"
    )

    # DECLARED, NOT BRANCHED. The hook the commit ran is the one the registry
    # entry names; there is no if-landform anywhere in the commit path.
    declared = [hook.target for hook in step_registry.get_step("landform").post_commit]
    assert "step_orchestrator.attach_keypoint_relationships" in declared, declared

print(
    f"9. THE KEYPOINT HOOK: {len(keypoints)} keypoint(s) carried no "
    f"feature_relationships before the commit; after it every one carries "
    f"nearest_production_area status 'computed' (nearest "
    f"{keypoints[0]['feature_relationships']['nearest_production_area']['distance_m']} m, "
    f"differential "
    f"{keypoints[0]['feature_relationships']['nearest_production_area']['elevation_differential_m']} m "
    f"on the first) and water_zone status 'no_feature'. Re-committing "
    f"converges rather than accumulating. Declared as {declared}, not "
    f"branched on in the orchestrator."
)


# --- 10. ID STABILITY -------------------------------------------------
#
# THE ASSERTION THE REOPEN RESTORE PATH STANDS ON. Section 11 restores a
# selection by matching committed feature ids against a freshly regenerated
# proposal set. If ids are not stable across regenerates, that match finds
# nothing and a user loses every selection on reopen -- silently, because an
# empty selection is a legitimate state. So it is asserted here, explicitly,
# rather than assumed.

with Harness() as h:
    s = Session()
    first = s.generate()
    second = s.generate()

    first_ids = {feature["id"] for feature in first["suggested_zones"]["features"]}
    second_ids = {feature["id"] for feature in second["suggested_zones"]["features"]}
    assert first_ids, "the fixture must propose zones or this is vacuous"
    assert first_ids == second_ids, (
        f"proposal feature ids MUST be stable across regenerates -- the reopen "
        f"restore path matches on them. First {sorted(first_ids)}, second "
        f"{sorted(second_ids)}; the difference is "
        f"{sorted(first_ids ^ second_ids)}. If this cannot be made true, the "
        f"restore path needs redesigning around a stored proposal set."
    )
    # AND ACROSS A CACHE EVICTION, which is the case that would break it in
    # production: a rebuilt context re-runs the whole warm-up.
    assert s.cache.discard(s.id)
    third_ids = {
        feature["id"] for feature in s.generate()["suggested_zones"]["features"]
    }
    assert third_ids == first_ids, (
        f"ids must survive a session-cache eviction and rebuild too: "
        f"{sorted(third_ids ^ first_ids)}"
    )

print(
    f"10. ID STABILITY: two generates on one session produced identical "
    f"feature id sets ({len(first_ids)} ids: {sorted(first_ids)}), and a third "
    f"after a cache eviction and rebuild produced the same set. The reopen "
    f"restore path depends on exactly this."
)


# --- 11. REOPEN RESTORE -----------------------------------------------

with Harness() as h:
    s = Session()
    payload = s.generate()
    proposals = payload["suggested_zones"]["features"]
    assert len(proposals) >= 2

    selected = proposals[:2]
    drawn = _drawn("drawn-1", HYDRIC_ZONE_RING)
    provenance = {feature["id"]: "generated" for feature in selected}
    provenance["drawn-1"] = "user_added"
    s.commit(_collection(list(selected) + [drawn]), provenance, base_revision=0)

    reopened = s.reopen()
    entry = reopened["steps"]["landform"]

    assert entry["status"] == design_document.STATUS_GENERATED, entry["status"]
    assert entry["revision"] == 1, (
        "the revision is retained through a reopen, so the eventual re-commit "
        "carries the optimistic-concurrency chain forward"
    )
    assert "features" in entry, "the reopened step keeps its editable starting point"

    restored = s.context().step_restored["landform"]
    restored_ids = {
        feature["id"] for feature in restored["payload"]["suggested_zones"]["features"]
    }
    assert restored_ids == {feature["id"] for feature in proposals}, (
        "the proposals come back -- by RE-RUNNING generate, not from a stored "
        "copy"
    )
    assert restored["selected_feature_ids"] == [f["id"] for f in selected], (
        f"the prior selection must be restored: "
        f"{restored['selected_feature_ids']} vs {[f['id'] for f in selected]}"
    )
    assert not restored["missing_feature_ids"], (
        f"every selected id must still be proposed: "
        f"{restored['missing_feature_ids']}"
    )
    # THE DRAWN ZONE, from the document directly. It carries its own geometry
    # and needs no id matching -- it was never a proposal.
    drawn_back = restored["user_added"]["features"]
    assert [feature["id"] for feature in drawn_back] == ["drawn-1"], drawn_back
    assert drawn_back[0]["geometry"] == drawn["geometry"], (
        "a drawn zone restores from the document verbatim -- its geometry is "
        "the only record of it there is"
    )
    assert restored["provenance"]["drawn-1"] == "user_added"

    # AND THE RESTORED STATE IS RE-COMMITTABLE at the retained revision.
    recommitted = s.commit(
        _collection(list(selected) + drawn_back), restored["provenance"], base_revision=1
    )
    assert recommitted["steps"]["landform"]["revision"] == 2

print(
    f"11. REOPEN RESTORE: committed {len(selected)} selected proposals plus one "
    f"drawn zone, then reopened. Status "
    f"'{entry['status']}' at retained revision {entry['revision']}; generate "
    f"re-ran and returned {len(restored_ids)} proposals with the prior "
    f"selection {restored['selected_feature_ids']} restored and the drawn zone "
    f"{[f['id'] for f in drawn_back]} present with its geometry intact. The "
    f"restored state re-committed at revision "
    f"{recommitted['steps']['landform']['revision']}."
)


# --- 12. CASCADE INVALIDATION, AND THE COMMITTED RESOLVER -------------
#
# A MINIMAL SECOND ENTRY, SYNTHESISED, and it stays synthetic now that the
# real water entry exists. Both assertions here are about the CASCADE and the
# committed resolver -- one consumes edge, one commit, one invalidation -- and
# the real water entry brings six more edges, a union `combine` and an
# empty-commit sentinel, none of which this section is testing. Keeping the
# fixture minimal is what makes a failure here point at the cascade rather
# than at water's own machinery (test_water_step.py covers that).
#
# It is also still the standing assertion that a second entry is a ROW IN THE
# TABLE and nothing else: this one was written before the water entry existed
# and needed no orchestrator change then, and needs none now.

SYNTHETIC_WATER = step_registry.StepDefinition(
    step_id="water",
    consumes=(
        step_registry.Consumed(
            name="production_areas",
            source=step_registry.SOURCE_COMMITTED,
            from_step="landform",
            rehydrate="wire_translation.rehydrate_production_zones",
            forward_as="production_areas",
            why="Water is sited to serve the production ground the user chose.",
        ),
    ),
    generate="water_candidate_zones.identify_water_candidate_zones",
    payload="step_orchestrator.build_landform_payload",
    proposal_collection="suggested_zones",
    produces=("selected_water_zone",),
    commit_contract=step_registry.CommitContract(
        layers=("water_candidate_zone",),
        geometry_types=("Polygon", "MultiPolygon"),
        min_features=0,
        max_features=None,
        rehydrate="wire_translation.rehydrate_production_zones",
    ),
)

with Harness() as h, mock_patch.dict(
    step_registry.STEP_REGISTRY, {"water": SYNTHETIC_WATER}
):
    assert step_registry.dependents_of("landform") == ("water", "roads", "trees"), (
        "the consumes edge is the invalidation edge -- read off the "
        "declaration, never restated (roads and trees consume landform directly too)"
    )
    assert step_registry.transitive_dependents("landform") == ("water", "roads", "trees")

    s = Session()
    payload = s.generate()
    proposals = payload["suggested_zones"]["features"]

    # THE RESOLVER REFUSES an uncommitted upstream step. It does not generate
    # anyway and it does not self-compute -- which for water would mean
    # re-running the production optimiser and siting water on zones the user
    # never selected.
    try:
        step_orchestrator.assemble_consumes(
            SYNTHETIC_WATER, s.context(), s.stored()
        )
        raise AssertionError("an uncommitted upstream step must be refused")
    except step_orchestrator.UpstreamNotCommittedError as exc:
        refusal = exc
    assert refusal.upstream_step == "landform"
    assert refusal.upstream_status == design_document.STATUS_GENERATED
    assert refusal.step_id == "water"

    document = s.commit(
        _collection(proposals[:2]),
        {f["id"]: "generated" for f in proposals[:2]},
        base_revision=0,
    )

    # NOW IT RESOLVES -- to the rehydrated internal shape, not to the
    # optimiser's own output.
    assembled = step_orchestrator.assemble_consumes(
        SYNTHETIC_WATER, s.context(), document
    )
    resolved = assembled["production_areas"]
    assert len(resolved) == 2, resolved
    assert all("polygon_utm" in patch for patch in resolved)
    assert [patch["id"] for patch in resolved] == [
        wire_translation.internal_zone_id(f["id"]) for f in proposals[:2]
    ], "a selected proposal keeps its own pipeline id through the round trip"

    # SERVED FROM THE REVISION-KEYED CACHE the commit populated -- a second
    # read does not rehydrate again.
    rehydrations_before = h.rehydrate.call_count
    step_orchestrator.assemble_consumes(SYNTHETIC_WATER, s.context(), document)
    assert h.rehydrate.call_count == rehydrations_before, (
        "a second read of the same committed revision must come from "
        "SessionContext.step_committed, not rehydrate again"
    )

    # A DOWNSTREAM CACHED VALUE, then a reopen of landform.
    context = s.context()
    context.step_proposals["water"] = {"sentinel": "computed from the old commit"}
    context.step_restored["water"] = {"sentinel": "restored from the old commit"}
    assert "landform" in context.step_committed

    s.reopen("landform")

    context = s.context()
    assert "water" not in context.step_proposals, (
        "reopening landform must drop the water proposals -- they were "
        "computed from the commit now being re-edited, and the consumes edge "
        "is what says so"
    )
    assert "water" not in context.step_restored
    assert "landform" not in context.step_committed, (
        "the reopened step's own committed value is no longer committed"
    )

    # AND THE PRECISION IS REAL: a registered step that consumes NOTHING from
    # landform keeps its cache, where design_document.downstream_steps()
    # would reset it. That difference is why the edges are written down.
    assert step_registry.transitive_dependents("water") == ("roads", "trees"), (
        "the real roads and trees entries consume the water commit; the synthetic "
        "water entry here changes nothing about those edges"
    )
    assert "water" in design_document.downstream_steps("landform")

print(
    f"12. CASCADE INVALIDATION: with a synthetic water entry (one row in the "
    f"table, no orchestrator change) consuming landform's commit -- the "
    f"resolver REFUSED the uncommitted upstream step, then resolved to 2 "
    f"rehydrated patches keeping their pipeline ids, served from the "
    f"revision-keyed cache on the second read. Reopening landform dropped "
    f"water's cached proposals and restored state along the consumes edge, "
    f"and dropped landform's own committed value."
)


# --- 13. NO NETWORK DURING COMMIT OR REOPEN ---------------------------
#
# COUNTED, NOT TIMED. Every network boundary in the harness is a mock whose
# call_count is summed; a stopwatch would measure the fixture, not the code.
# And the zero is REACHABLE -- section 2 of test_step_orchestrator.py shows
# the same fetches firing when an override is dropped.

with Harness() as h:
    s = Session()
    payload = s.generate()
    proposals = payload["suggested_zones"]["features"]

    drawn = _drawn("drawn-1", HYDRIC_ZONE_RING)
    features = _collection([proposals[0], drawn])
    provenance = {proposals[0]["id"]: "generated", "drawn-1": "user_added"}

    before_commit = h.total_network_calls
    s.commit(features, provenance, base_revision=0)
    commit_network = h.total_network_calls - before_commit

    before_reopen = h.total_network_calls
    s.reopen()
    reopen_network = h.total_network_calls - before_reopen

    assert commit_network == 0, (
        f"a commit must make ZERO network calls -- rehydration works against "
        f"the cached DEM and the crossings against the cached exclusion "
        f"result. Got {commit_network}"
    )
    assert reopen_network == 0, (
        f"a reopen re-runs generate, which is network-free by contract. Got "
        f"{reopen_network}"
    )
    # The reopen DID re-run the generate (that is the restore mechanism), so
    # the zero above is a closed fetch rather than a path that never ran.
    assert h.identify_production.call_count >= 2, h.identify_production.call_count
    assert h.rehydrate.call_count >= 1, "the commit did rehydrate"

print(
    f"13. NO NETWORK: {commit_network} network calls during the commit and "
    f"{reopen_network} during the reopen, summed over every mocked boundary "
    f"(parcel fetch, two SDA queries, two canopy bindings, two road bindings). "
    f"The reopen re-ran the generate "
    f"({h.identify_production.call_count} production runs in the section) and "
    f"the commit rehydrated {h.rehydrate.call_count} time(s), so both zeros "
    f"are closed fetches rather than paths that never ran."
)


print("\nAll step_commit checks passed.")
