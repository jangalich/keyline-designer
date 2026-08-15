"""
diagnose_fetch_layout_layers_redundant_fetches.py

Permanent, standalone diagnostic -- parallel to diagnose_pipeline_
redundant_fetches.py, but targets render_layout_map.fetch_layout_layers()
directly rather than pipeline_context.build_pipeline_context() alone.
Measures REAL call counts (via unittest.mock.patch.object(..., wraps=
real_function), never a canned mock) for every expensive entry point
fetch_layout_layers()'s own call graph reaches, across ONE real run, and
prints a before/after-comparable table. Confirms the specific redundancy
this branch (render-layout-map-context-wiring) set out to eliminate:
production areas recomputed up to 5x, water zone selection 3-4x, and road
corridor routing 3-4x per single render, none of it reusing what an
earlier call in the SAME fetch_layout_layers() invocation already
computed.

--- Why call-site patching, not one shared patch ---

Every target function (identify_optimized_production_areas, fetch_and_
select_optimal_water_zone/identify_water_suitability, delineate_valleys,
identify_road_corridor_candidates, identify_solar_candidate_zones,
identify_tree_zone_candidates, get_dem_for_boundary) is imported via
`from X import Y` at each of several call sites (render_layout_map.py,
road_corridors.py, solar_suitability.py, tree_zone_candidates.py, water_
suitability.py, fencing.py, pipeline_context.py) -- each of those creates
its OWN independent name binding in the importing module's namespace,
decoupled from the defining module's own attribute (classic "patch where
it's looked up, not where it's defined", same lesson diagnose_pipeline_
redundant_fetches.py's own module docstring already documents). Patching
only the defining module's attribute would silently miss every call made
through a `from X import Y` binding elsewhere -- so this script patches
EVERY known binding site for each target function (enumerated by hand via
`grep -rn "^from <module> import" *.py`, not guessed), wraps=real_function
throughout, and SUMS call counts across all of a function's own binding
sites to report one real total. A binding that doesn't exist on the
CURRENT checkout (e.g. render_layout_map.fetch_and_select_optimal_water_
zone, present pre-branch, absent post-branch) is skipped via a hasattr()
guard -- this lets the SAME script run unmodified against the BEFORE and
AFTER checkouts (`git stash` / `git stash pop` around each run).

--- Offline stubbing ---

Outbound network is proxy-blocked in this environment (confirmed: a
direct probe to planetarycomputer.microsoft.com's own CONNECT gets a 403
from the gateway) -- this script never even attempts NETWORK MODE, unlike
diagnose_pipeline_redundant_fetches.py's own dual-mode design. It reuses
that script's own proven-sufficient SYNTHETIC MODE stub set for making
build_pipeline_context() (now called once, inside fetch_layout_layers()
itself) complete fully offline: dem_data.get_dem_for_boundary (every
binding, return_value=SYNTHETIC_DEM), production_area._fetch_
disqualifying_soil_union / production_area_ceiling._fetch_disqualifying_
soil_union (return_value=None -- degrades to "unchecked", not an error,
per production_area.py's own _ROAD_CHECK_UNCHECKED convention),
production_area.get_canopy_height_for_boundary (ONE shared stub covers
EVERY mandatory-canopy gate across the whole pipeline -- get_required_
tree_root_zone_mask_utm, wherever it's imported from, resolves this name
via production_area.py's own module globals at call time, confirmed
working end-to-end by test_solar_suitability_pipeline.py's own identical
technique), farm_roads_data.get_road_exclusion_union_utm and road_
corridors._fetch_floodplain_hydric_union (both called UNPROTECTED, no
try/except, directly inside build_pipeline_context() itself -- every
OTHER caller of these two wraps its own call in try/except and degrades
gracefully on its own, confirmed by reading production_area_ceiling.py/
road_corridors.py/water_suitability.py directly, so only these two
specific unwrapped call sites need stubbing). Two more, specific to
fetch_layout_layers() itself rather than build_pipeline_context(): render_
layout_map's own get_water_features_for_boundary binding (called
UNPROTECTED at fetch_layout_layers()'s own top level -- stubbed to an
empty streams/water_bodies dict) and farm_roads_data.get_farm_roads_for_
boundary (fetch_layout_layers() already wraps this ONE call in its own
try/except and degrades to farm_road_features=[] on failure -- stubbed
here anyway purely so the run doesn't burn real retry time against a
proxy that will reject it every time).

--- Terrain: a real, non-empty production area, unlike diagnose_pipeline_
--- redundant_fetches.py's own steep two-landform fixture ---

diagnose_pipeline_redundant_fetches.py's synthetic terrain (~80% slope
everywhere) was built for delineate_valleys()'s own ridge/valley tracing
and is EXPLICITLY documented there as too steep to clear identify_
optimized_production_areas()'s own eligibility gate -- production_areas
comes back empty on it, which makes "how many times was production
recomputed" a much weaker signal (an empty list is trivially "free" to
recompute). This script instead reuses test_solar_suitability_pipeline.
py's own uniform ~4% gentle-slope terrain (proven, in that file, to make
identify_production_areas() claim real, non-empty production area) --
selected_water_zone/selected_road_corridor still legitimately resolve to
None on this fixture (it's flat, not ridge-shaped -- no real valleys ever
delineate), same limitation that file's own docstring already documents
and works around by treating "0 calls to a self-compute fallback" as the
signal, independent of what the fallback would have returned. This
script's PRIMARY, most concrete measurement -- identify_optimized_
production_areas()'s own real total call count across a single fetch_
layout_layers() run -- is fully meaningful on this fixture precisely
because production areas here are real and non-empty.

--- What this does NOT do ---

Does not modify render_layout_map.py or any of its dependency modules --
read-only. Does not attempt NETWORK MODE (see above). Does not assert/gate
on an exit code the way diagnose_pipeline_redundant_fetches.py's PRIMARY
table does -- this script is a point-in-time before/after measurement
tool run by hand around a `git stash`/`git stash pop` pair, not a CI
regression gate (fetch_layout_layers()'s own expected counts differ
between the BEFORE and AFTER checkouts by design, so there is no single
"expected" table a gate could check against both).
"""

import os
import sys
import traceback
from collections import Counter
from contextlib import ExitStack
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import box

import dem_data
import farm_roads_data
import fencing
import parcel_data as pd_module
import pipeline_context as pc
import production_area
import production_area_ceiling
import render_layout_map
import road_corridors
import solar_suitability
import tree_zone_candidates
import water_candidate_zones
import water_suitability
from dem_data import _utm_epsg_for_lonlat

# --- synthetic gentle-slope DEM fixture -- construction lifted verbatim
# from test_solar_suitability_pipeline.py's own module-level fixture (a
# uniform ~4% south-facing grade, proven there to make identify_
# production_areas() claim a real, non-empty production area offline). ---

CENTER_LON, CENTER_LAT = -79.98, 40.64
EPSG = _utm_epsg_for_lonlat(CENTER_LON, CENTER_LAT)
DST_CRS = f"EPSG:{EPSG}"

_center_x, _center_y = warp_transform("EPSG:4326", DST_CRS, [CENTER_LON], [CENTER_LAT])
center_x, center_y = _center_x[0], _center_y[0]

RESOLUTION = 5.0
SIZE = 60  # 300m x 300m, big enough for CANDIDATE_POINT_SPACING_METERS (25m) to sample several points
HALF_EXTENT = SIZE * RESOLUTION / 2
origin_x = center_x - HALF_EXTENT
origin_y = center_y + HALF_EXTENT

_utm_corners_x = [origin_x, origin_x + SIZE * RESOLUTION, origin_x + SIZE * RESOLUTION, origin_x, origin_x]
_utm_corners_y = [origin_y, origin_y, origin_y - SIZE * RESOLUTION, origin_y - SIZE * RESOLUTION, origin_y]
_lons, _lats = warp_transform(DST_CRS, "EPSG:4326", _utm_corners_x, _utm_corners_y)
SYNTHETIC_BOUNDARY_COORDINATES = list(zip(_lons, _lats))
SYNTHETIC_ANCHOR_LON_LAT = SYNTHETIC_BOUNDARY_COORDINATES[0]

_array = np.zeros((SIZE, SIZE), dtype=np.float32)
for _row in range(SIZE):
    _array[_row, :] = 100.0 - _row * 0.2

SYNTHETIC_DEM = {
    "array": _array,
    "resolution_meters": (RESOLUTION, RESOLUTION),
    "origin_x": origin_x,
    "origin_y": origin_y,
    "crs": DST_CRS,
}

_FAKE_EXISTING_ROADS_UNION = box(
    origin_x + 500_000, origin_y + 500_000, origin_x + 500_010, origin_y + 500_010
)
_FAKE_HYDRIC_UNION = box(
    origin_x + 500_000, origin_y + 500_000, origin_x + 500_010, origin_y + 500_010
)
_EMPTY_WATER_FEATURES = {"streams": [], "water_bodies": []}


def _fake_clean_canopy(boundary_coordinates, dem):
    """Offline stand-in for canopy_height_data.get_canopy_height_for_boundary()
    -- the ONE shared fetch every get_required_tree_root_zone_mask_utm()
    call (production/water/road/solar/tree/fencing, however each imports
    it) ultimately resolves via production_area.py's own module globals.
    All-below-threshold (no tree cover anywhere) is a pure no-op against
    every downstream canopy-exclusion gate."""
    return {
        "array": np.full(dem["array"].shape, 1.0, dtype=np.float32),
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source_item_id": "offline-test-stub",
    }


def _fake_water_features(boundary_coordinates, buffer_meters=150):
    return dict(_EMPTY_WATER_FEATURES)


# --- (module, attr) call-site registry -- every real `from X import Y`
# binding of each target function, enumerated by hand via
# `grep -rn "^from <module> import" *.py`, not guessed. hasattr() gates
# each entry so the SAME registry works against both the BEFORE and AFTER
# checkouts. ---

WRAPS_SITES = {
    "get_dem_for_boundary": [
        (dem_data, "get_dem_for_boundary"),
        (render_layout_map, "get_dem_for_boundary"),  # BEFORE only
        (fencing, "get_dem_for_boundary"),
        (production_area_ceiling, "get_dem_for_boundary"),
        (road_corridors, "get_dem_for_boundary"),
        (solar_suitability, "get_dem_for_boundary"),
        (tree_zone_candidates, "get_dem_for_boundary"),
        (water_candidate_zones, "get_dem_for_boundary"),
        (water_suitability, "get_dem_for_boundary"),
        (pd_module, "get_dem_for_boundary"),  # AFTER only -- parcel_data.fetch_parcel_data()'s own DEM fetch
    ],
    "fetch_parcel_data": [
        (render_layout_map, "fetch_parcel_data"),  # AFTER only -- the fetch-layer entry point this branch adds
    ],
    "identify_optimized_production_areas": [
        (render_layout_map, "identify_optimized_production_areas"),  # BEFORE only, and briefly AFTER Fix 1; absent once Fix 2 removes fetch_layout_layers()'s own 2nd call and its import
        (production_area_ceiling, "identify_optimized_production_areas"),  # pipeline_context.py's own attribute-style call
        (road_corridors, "identify_optimized_production_areas"),
        (solar_suitability, "identify_optimized_production_areas"),
        (tree_zone_candidates, "identify_optimized_production_areas"),
        (water_suitability, "identify_optimized_production_areas"),
    ],
    "fetch_and_select_optimal_water_zone": [
        (render_layout_map, "fetch_and_select_optimal_water_zone"),  # BEFORE only
        (fencing, "fetch_and_select_optimal_water_zone"),
        (road_corridors, "fetch_and_select_optimal_water_zone"),
        (pc, "fetch_and_select_optimal_water_zone"),
        (water_suitability, "fetch_and_select_optimal_water_zone"),
    ],
    "identify_water_suitability (nested, via identify_*_candidate_zones self-compute)": [
        (solar_suitability, "identify_water_suitability"),
        (tree_zone_candidates, "identify_water_suitability"),
    ],
    "identify_road_corridor_candidates": [
        (render_layout_map, "identify_road_corridor_candidates"),
        (fencing, "identify_road_corridor_candidates"),
        (road_corridors, "identify_road_corridor_candidates"),  # pipeline_context.py's own attribute-style call
        (solar_suitability, "identify_road_corridor_candidates"),
        (tree_zone_candidates, "identify_road_corridor_candidates"),
    ],
    "identify_solar_candidate_zones": [
        (render_layout_map, "identify_solar_candidate_zones"),  # AFTER only
        (pc, "identify_solar_candidate_zones"),
        (solar_suitability, "identify_solar_candidate_zones"),  # covers fetch_and_select_optimal_structure_site()'s own internal call too
    ],
    "fetch_and_select_optimal_structure_site": [
        (render_layout_map, "fetch_and_select_optimal_structure_site"),  # BEFORE only
    ],
    "identify_tree_zone_candidates": [
        (render_layout_map, "identify_tree_zone_candidates"),  # BEFORE only
        (fencing, "identify_tree_zone_candidates"),
        (pc, "identify_tree_zone_candidates"),
        (solar_suitability, "identify_tree_zone_candidates"),
    ],
}

# delineate_valleys needs the real valley_delineation module object, only
# reachable via pipeline_context's own `import valley_delineation` --
# assigned here rather than inline above for readability.
WRAPS_SITES["delineate_valleys"] = [
    (pc.valley_delineation, "delineate_valleys"),
    (road_corridors, "delineate_valleys"),
    (water_suitability, "delineate_valleys"),
    (water_candidate_zones, "delineate_valleys"),
]

STUB_SITES = [
    # (module, attr, kwargs) -- genuinely-external, offline-unreachable
    # primitive fetches. return_value/side_effect, never wraps=, for these.
    (production_area, "_fetch_disqualifying_soil_union", {"return_value": None}),
    (production_area_ceiling, "_fetch_disqualifying_soil_union", {"return_value": None}),
    # get_canopy_height_for_boundary is NOT here -- see COUNTED_STUB_SITES
    # below. This branch (canopy-mask-wiring) wires parcel_data.canopy_
    # height into build_pipeline_context()/the direct identify_solar_
    # candidate_zones() call, so a plain stubbed-and-ignored count would
    # hide the exact before/after proof this branch's own testing
    # requirements ask for -- same reasoning COUNTED_STUB_SITES' own
    # leading comment already gives for get_farm_roads_for_boundary/
    # get_water_features_for_boundary.
    # NOT (farm_roads_data, "get_road_exclusion_union_utm", ...) -- every real
    # caller (production_area._fetch_road_exclusion_union_utm(), whether
    # invoked from production_area.py itself, production_area_ceiling.py,
    # water_candidate_zones.py, or water_suitability.py) calls the bare name
    # `get_road_exclusion_union_utm`, which resolves via production_area.py's
    # OWN module globals at call time (see that wrapper's own docstring:
    # "a single test mock of get_road_exclusion_union_utm here covers every
    # entry point") -- patching farm_roads_data's own attribute instead left
    # this unstubbed in practice, causing a real, slow live-network attempt
    # against the proxy on every run (confirmed live: hangs past this
    # script's 2-minute harness timeout rather than failing fast).
    (production_area, "get_road_exclusion_union_utm", {"return_value": _FAKE_EXISTING_ROADS_UNION}),
    (road_corridors, "_fetch_floodplain_hydric_union", {"return_value": (_FAKE_HYDRIC_UNION, False)}),
    # get_water_features_for_boundary and get_farm_roads_for_boundary are
    # BOTH counted (not just stubbed) -- see COUNTED_STUB_SITES below, not
    # here. This is the real before/after proof this branch's own testing
    # section asks for: how many times the raw NHD-streams/USGS-roads fetch
    # actually runs, collapsing from "once per self-computing consumer" to
    # "exactly once, inside parcel_data.fetch_parcel_data()."
    # fencing.identify_fencing()'s own unprotected get_water_features_geojson()
    # fetch (no try/except at that call site -- see fencing.py's own docstring:
    # "fetched here if not already supplied", and fetch_layout_layers() never
    # supplies water_features_geojson=) -- unstubbed, this hangs for minutes
    # (hydrology_data._query_layer()'s own 30/60/90s escalating-timeout
    # retries x2 layers, confirmed live while building this script).
    (fencing, "get_water_features_geojson", {"return_value": {"type": "FeatureCollection", "features": []}}),
    # tree_zone_candidates.py's own three Step-2 soil/stream fetches -- each
    # IS try/except-protected at its own call site (confirmed by reading:
    # each degrades to a real, correct "data unavailable" flag on failure),
    # so none of these are a correctness gap. Stubbed here purely so this
    # script's own repeated (by design -- that's the redundancy being
    # measured) identify_tree_zone_candidates() calls don't each separately
    # pay soil_data.py's real max_retries=2 backoff sleep against a proxy
    # that will reject every attempt anyway.
    (tree_zone_candidates, "_fetch_prime_farmland_union", {"return_value": None}),
    (tree_zone_candidates, "_fetch_hydric_soil_union", {"return_value": None}),
    (tree_zone_candidates, "_fetch_stream_union", {"return_value": None}),
    # solar_suitability.py's own prime-farmland-conflict check -- also
    # already try/except-protected at its own call site (see identify_
    # solar_candidate_zones()'s own "except Exception: pass"); stubbed to
    # fail fast for the same reason as the three above.
    (solar_suitability, "get_farmland_classification_for_polygon", {"side_effect": RuntimeError("offline stub: no network")}),
    # --- parcel_data.py's own 12 raw-layer fetches -- ONLY reached once
    # fetch_layout_layers() is wired to call parcel_data.fetch_parcel_data()
    # itself (the "AFTER" checkout this branch produces; hasattr() guards
    # below make these no-ops, never even entered, against the "BEFORE"
    # checkout, where parcel_data isn't imported by render_layout_map.py at
    # all yet). Every one of these is parcel_data.py's OWN `from X import Y`
    # binding, not the defining module's attribute -- same "patch where it's
    # looked up" requirement as get_road_exclusion_union_utm/get_water_
    # features_for_boundary above. get_dem_for_boundary is handled separately
    # below (added to the shared get_dem_for_boundary WRAPS_SITES list, same
    # SYNTHETIC_DEM return_value every other binding gets); this module's own
    # get_farm_roads_for_boundary/get_water_features_for_boundary bindings
    # are handled in COUNTED_STUB_SITES below instead, not here.
    (pd_module, "get_soil_data_for_polygon", {"return_value": []}),
    (pd_module, "get_farmland_classification_for_polygon", {"return_value": []}),
    (pd_module, "get_erosion_factor_for_polygon", {"return_value": []}),
    (pd_module, "get_saturated_hydraulic_conductivity_for_polygon", {"return_value": []}),
    (pd_module, "get_soil_geometries_for_polygon", {"return_value": {}}),
    (pd_module, "get_canopy_height_for_boundary", {"side_effect": _fake_clean_canopy}),
    (pd_module, "get_climate_summary_for_point", {"return_value": {"source": "offline-test-stub"}}),
    (pd_module, "get_elevation_grid", {"return_value": []}),
    (pd_module, "get_imagery_summary_for_boundary", {"return_value": {"source": "offline-test-stub"}}),
]

# Same (module, attr) shape as WRAPS_SITES, grouped by logical name and
# TRACED (per-call-site, like TRACED_LOGICAL_NAMES below) rather than just
# stubbed-and-summed -- these two are genuinely-external raw fetches
# (return_value/side_effect only, real network never touched), but a PLAIN
# total count is misleading here: both are ALSO called deep inside the
# COMPUTE-layer's own redundant self-recomputation (e.g. water_suitability.
# identify_water_suitability()'s own unconditional internal self-fetch,
# with no override param to short-circuit it -- see that module's own code,
# untouched by this branch, backlog item 5, deferred). Per-call-site tracing
# is what actually proves the FETCH-layer claim this branch makes: fetch_
# layout_layers()'s own former TOP-LEVEL call (render_layout_map.py, BEFORE
# only) and get_road_exclusion_union_utm's own farm_roads_data-internal
# self-fetch call collapse to a single parcel_data.py call site in AFTER,
# while every OTHER call site (inside the untouched compute-layer
# self-recompute paths) fires exactly as often as it did before -- a flat
# aggregate total conflates "this branch's own fetch, now gone" with "a
# pre-existing, deferred, unrelated redundancy," which is exactly the
# distinction this script's own testing requirements ask to prove.
# hasattr()-guarded so the SAME registry works on both the BEFORE and AFTER
# checkouts.
COUNTED_STUB_SITES = {
    "get_farm_roads_for_boundary": {
        "stub": lambda *a, **k: [],
        "sites": [
            (farm_roads_data, "get_farm_roads_for_boundary"),
            (render_layout_map, "get_farm_roads_for_boundary"),  # BEFORE only
            (pd_module, "get_farm_roads_for_boundary"),  # AFTER only
            (fencing, "get_farm_roads_for_boundary"),
            (solar_suitability, "get_farm_roads_for_boundary"),
        ],
    },
    "get_water_features_for_boundary": {
        "stub": _fake_water_features,
        "sites": [
            (render_layout_map, "get_water_features_for_boundary"),  # BEFORE only
            (water_suitability, "get_water_features_for_boundary"),
            (tree_zone_candidates, "get_water_features_for_boundary"),
            (road_corridors, "get_water_features_for_boundary"),
            (pd_module, "get_water_features_for_boundary"),  # AFTER only
        ],
    },
    # get_canopy_height_for_boundary: this branch's own real, final proof.
    # Only TWO real `from canopy_height_data import get_canopy_height_for_
    # boundary` bindings exist anywhere in this codebase (confirmed via
    # `grep -rn "^from canopy_height_data import" *.py`): parcel_data.py's
    # own single fetch (pd_module -- the fetch-layer call this whole
    # session's canopy effort was building TOWARD reusing) and production_
    # area.py's own module global (the ONE shared leaf get_required_tree_
    # root_zone_mask_utm()/_fetch_tree_root_zone_mask_utm() call from
    # EVERY consumer -- production/optimized-production, water-system-
    # candidate, water-suitability, solar, tree-zone, AND fencing, however
    # each imports get_required_tree_root_zone_mask_utm -- all resolve
    # this same name via production_area.py's own module globals at call
    # time, same "one shared stub covers every mandatory-canopy gate"
    # property _fake_clean_canopy already relies on above). Measured on the
    # canopy-mask-wiring-road-fencing branch: pd_module's own site reads
    # exactly 1 (ParcelData's single upstream fetch, unrelated to and
    # unchanged by this branch). production_area's own site dropped from
    # 10 to 5 on this fixture -- render_layout_map.py's own direct
    # identify_road_corridor_candidates()/identify_fencing() calls and
    # pipeline_context.py's own identify_road_corridor_candidates() call
    # now forward canopy_height=parcel_data.canopy_height (this branch's
    # own change), which fencing.identify_fencing()'s mandatory boundary-
    # fencing canopy gate and both functions' selected_water_zone self-
    # compute fallbacks now honor (road_corridors.py/fencing.py themselves
    # gained the canopy_height parameter and forwarding on the prior
    # branch, canopy-height-road-corridors). The remaining 5 are
    # STRUCTURAL, not a gap in this branch's own scope: on this flat,
    # no-water fixture, selected_water_zone comes back None, so tree_zone_
    # candidates.py's (3 calls) and solar_suitability.py's (2 calls) own
    # DIRECT identify_road_corridor_candidates() calls -- both files
    # explicitly out of scope here, unwired to canopy_height on their own
    # call sites -- each re-trigger identify_road_corridor_candidates()'s
    # own selected_water_zone self-compute fallback with no override to
    # forward, hitting the real gate. Closing those needs canopy_height
    # wiring inside tree_zone_candidates.py/solar_suitability.py
    # themselves, a separate, later branch.
    "get_canopy_height_for_boundary": {
        "stub": _fake_clean_canopy,
        "sites": [
            (production_area, "get_canopy_height_for_boundary"),
            (pd_module, "get_canopy_height_for_boundary"),
        ],
    },
}


# --- Addendum: per-call-site tracing for the two blown-up counts ---
#
# fetch_and_select_optimal_water_zone and identify_road_corridor_candidates
# are the two logical functions whose AFTER count is HIGHER than BEFORE
# (10 vs 7, 8 vs 5 on the last measured run) -- surprising, since this
# branch's whole point is eliminating redundant recomputation. Plain
# wraps=original (used for every other target) only proves HOW MANY times
# a function ran, not WHO called it or why -- not enough to tell "a real
# wiring bug in this branch's own implementation" apart from "the
# documented ambiguous-None-sentinel behavior (a selected_water_zone=None/
# selected_road_corridor=None override is indistinguishable from 'not
# supplied' to every receiving function, and correctly self-computes in
# that case -- see pipeline_context.py's own KNOWN LIMITATIONS #4) firing
# MORE often because this branch added an extra outer layer (build_
# pipeline_context()) that ALSO hits the same ambiguity on this flat,
# no-real-water-or-road fixture." This section replaces wraps= with a
# side_effect that ALSO records the real caller (module:line in function)
# before invoking the real function -- so each of the two breakdown tables
# printed at the end directly answers "which function's own body issued
# this call," not just "how many."
TRACED_LOGICAL_NAMES = {"fetch_and_select_optimal_water_zone", "identify_road_corridor_candidates"}

_REPO_DIR = os.path.dirname(os.path.abspath(__file__))


def _find_real_caller() -> str:
    """Walks the current stack from the top down, skipping unittest.mock's
    own internal frames and this diagnostic script's own wrapper frame, and
    returns the first frame that belongs to actual pipeline code -- module
    basename:lineno in the enclosing function, e.g.
    'solar_suitability.py:1153 in identify_solar_candidate_zones'. This is
    the real answer to "which function's own body issued this call,"
    regardless of exactly how many internal mock.py frames sit between the
    traced side_effect and the genuine caller."""
    for frame in reversed(traceback.extract_stack()[:-1]):  # [:-1] drops _find_real_caller's own frame
        if "unittest" in frame.filename and "mock" in frame.filename:
            continue
        if os.path.abspath(frame.filename) == os.path.abspath(__file__):
            continue
        return f"{os.path.basename(frame.filename)}:{frame.lineno} in {frame.name}"
    return "unknown (stack exhausted)"


def _make_traced_side_effect(original, log: Counter):
    def _traced(*args, **kwargs):
        log[_find_real_caller()] += 1
        return original(*args, **kwargs)

    return _traced


def _make_traced_stub(stub, log: Counter):
    """Same call-site tracing as _make_traced_side_effect(), but for a
    COUNTED_STUB_SITES entry -- `stub` is a fixed offline replacement
    (never the real, network-touching function), so there is no `original`
    to delegate to afterward."""

    def _traced(*args, **kwargs):
        log[_find_real_caller()] += 1
        return stub(*args, **kwargs)

    return _traced


def run() -> tuple[dict[str, int], dict[str, Counter]]:
    with ExitStack() as stack:
        enter = stack.enter_context

        for module, attr, kwargs in STUB_SITES:
            if hasattr(module, attr):
                enter(mock_patch.object(module, attr, **kwargs))

        call_site_logs: dict[str, Counter] = {
            name: Counter() for name in TRACED_LOGICAL_NAMES | set(COUNTED_STUB_SITES)
        }

        counted_stub_mocks: dict[str, list] = {}
        for logical_name, spec in COUNTED_STUB_SITES.items():
            counted_stub_mocks[logical_name] = []
            traced_stub = _make_traced_stub(spec["stub"], call_site_logs[logical_name])
            for module, attr in spec["sites"]:
                if hasattr(module, attr):
                    counted_stub_mocks[logical_name].append(enter(mock_patch.object(module, attr, side_effect=traced_stub)))

        # get_dem_for_boundary: return_value=SYNTHETIC_DEM at every site
        # (never wraps= -- the real function needs network this
        # environment doesn't have). call_count is still tracked.
        dem_mocks = []
        for module, attr in WRAPS_SITES["get_dem_for_boundary"]:
            if hasattr(module, attr):
                dem_mocks.append(enter(mock_patch.object(module, attr, return_value=SYNTHETIC_DEM)))

        wraps_mocks: dict[str, list] = {}
        for logical_name, sites in WRAPS_SITES.items():
            if logical_name == "get_dem_for_boundary":
                continue
            wraps_mocks[logical_name] = []
            for module, attr in sites:
                if module is not None and hasattr(module, attr):
                    original = getattr(module, attr)
                    if logical_name in TRACED_LOGICAL_NAMES:
                        mock_kwargs = {"side_effect": _make_traced_side_effect(original, call_site_logs[logical_name])}
                    else:
                        mock_kwargs = {"wraps": original}
                    wraps_mocks[logical_name].append(enter(mock_patch.object(module, attr, **mock_kwargs)))

        layers = render_layout_map.fetch_layout_layers(
            SYNTHETIC_BOUNDARY_COORDINATES, anchor_lon_lat=SYNTHETIC_ANCHOR_LON_LAT
        )

    assert isinstance(layers, dict) and "production_areas" in layers

    counts = {"get_dem_for_boundary": sum(m.call_count for m in dem_mocks)}
    for logical_name, mocks in counted_stub_mocks.items():
        counts[logical_name] = sum(m.call_count for m in mocks)
    for logical_name, mocks in wraps_mocks.items():
        counts[logical_name] = sum(m.call_count for m in mocks)

    counts["_production_areas_found"] = len(layers["production_areas"])
    counts["_selected_water_zone_is_none"] = layers["water_zone"] is None
    counts["_selected_road_corridor_is_none"] = layers["fencing_result"] is not None  # always true; sanity check fencing ran
    return counts, call_site_logs


def main() -> None:
    print("diagnose_fetch_layout_layers_redundant_fetches.py -- real call-count measurement for fetch_layout_layers()\n")
    print(f"Checkout under test: render_layout_map.py as currently on disk (git status shows which).")
    counts, call_site_logs = run()

    print(f"\n{'function (summed across every real call site)':<70} {'calls':>6}")
    print("-" * 78)
    for name, value in counts.items():
        if name.startswith("_"):
            continue
        print(f"{name:<70} {value:>6}")

    print(f"\nproduction_areas found (non-empty terrain check): {counts['_production_areas_found']}")
    if counts["_production_areas_found"] == 0:
        print(
            "WARNING: production_areas came back EMPTY on this run -- the identify_optimized_production_areas "
            "call-count numbers above are still real, but any zero elsewhere in the table may reflect an empty "
            "result short-circuiting, not confirmed dedup. Investigate before trusting this run's numbers."
        )
    print(f"selected_water_zone is None: {counts['_selected_water_zone_is_none']} (expected True on this flat, "
          f"non-ridge-shaped fixture -- see module docstring)")

    canopy_total = counts.get("get_canopy_height_for_boundary")
    if canopy_total is not None:
        canopy_from_parcel_data = call_site_logs["get_canopy_height_for_boundary"].get(
            "parcel_data.py:163 in fetch_parcel_data", 0
        )
        canopy_from_compute_layer = canopy_total - canopy_from_parcel_data
        print(
            f"\nget_canopy_height_for_boundary(): {canopy_total} total ({canopy_from_parcel_data} from parcel_data."
            f"fetch_parcel_data()'s own single upstream fetch, {canopy_from_compute_layer} from the compute-layer "
            "gate production_area.py's own _fetch_tree_root_zone_mask_utm() reaches -- measured at 23 before any "
            "canopy wiring existed, 11 after road_corridors.py/fencing.py gained the canopy_height parameter "
            "(canopy-height-road-corridors) but before render_layout_map.py/pipeline_context.py forwarded their "
            "own already-fetched parcel_data.canopy_height into their own identify_road_corridor_candidates()/ "
            "identify_fencing() calls, and now this (canopy-mask-wiring-road-fencing); see the per-call-site "
            "breakdown below for the current number and why any nonzero remainder there is a pre-existing, "
            "separately-scoped gap (tree_zone_candidates.py's and solar_suitability.py's own DIRECT identify_"
            "road_corridor_candidates() calls are not wired to canopy_height on their own call sites -- both "
            "files out of scope for this branch, see COUNTED_STUB_SITES' own comment above)."
        )

    print(
        "\n--- Addendum: per-call-site breakdown for the traced functions "
        "(diagnostic only, no fix applied) ---"
    )
    for logical_name in sorted(TRACED_LOGICAL_NAMES):
        log = call_site_logs[logical_name]
        total = sum(log.values())
        print(f"\n{logical_name}  (total: {total})")
        print(f"  {'call site (module:line in function)':<70} {'count':>6}")
        print("  " + "-" * 78)
        for site, count in log.most_common():
            print(f"  {site:<70} {count:>6}")

    print(
        "\n--- Addendum: per-call-site breakdown for the two raw fetches this "
        "branch's own testing requirements ask to isolate (fetch-layer, this "
        "branch's target, vs. compute-layer, deferred -- see COUNTED_STUB_SITES' "
        "own comment above) ---"
    )
    for logical_name in sorted(COUNTED_STUB_SITES):
        log = call_site_logs[logical_name]
        total = sum(log.values())
        print(f"\n{logical_name}  (total: {total})")
        print(f"  {'call site (module:line in function)':<70} {'count':>6}")
        print("  " + "-" * 78)
        for site, count in log.most_common():
            print(f"  {site:<70} {count:>6}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"\nRequest failed: {e}")
        sys.exit(1)
