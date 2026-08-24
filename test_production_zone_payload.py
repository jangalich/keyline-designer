"""
test_production_zone_payload.py

Exercises production_zone_payload.build_production_zone_payload() and
api.py's /api/production-zones route end to end, with NO NETWORK.

The DEM and canopy overrides both entry points accept are what make this
possible: a synthetic 5 m grid over the real reference boundary drives the
real gate/geometry/serialisation code, so everything this test asserts --
the contract's field set, hole preservation, coordinate precision, the
layer-named failure, and the soft-degrade flags -- is asserted against the
code that actually runs in production, not a mock of it.

The TERRAIN is synthetic and the acreages it produces are meaningless as
statements about the real parcel. Nothing here asserts a real-property
number; every assertion is about SHAPE and INVARIANTS.
"""

import json

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon

import api
from exclusion_zones import LAYER_ORDER
from production_zone_payload import (
    COORDINATE_PRECISION_DP,
    LayerFetchError,
    build_production_zone_payload,
)

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


def _synthetic_dem_and_canopy():
    """A dissected plateau: a bench tilted about 4%, cut by two incised
    drainages, with riparian woodland in the cuts and six interior canopy
    pockets. Chosen so the slope gate genuinely bites (steep drainage walls)
    and the canopy gate genuinely punches HOLES in the eligible union --
    both of which the assertions below depend on."""
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
    return dem, canopy


DEM, CANOPY = _synthetic_dem_and_canopy()
PAYLOAD = build_production_zone_payload(REFERENCE_BOUNDARY, dem=DEM, canopy_height=CANOPY)


# --- the contract's top level ------------------------------------------------

assert set(PAYLOAD) == {
    "eligible_union", "exclusion_layers", "suggested_zones",
    "zones", "summary", "scales", "wire",
}, f"unexpected top-level keys: {sorted(PAYLOAD)}"

assert json.dumps(PAYLOAD), "payload must be JSON-serialisable as-is"


# --- five exclusion layers, in order, every one present ----------------------

types = [layer["type"] for layer in PAYLOAD["exclusion_layers"]]
assert types == list(LAYER_ORDER), f"expected LAYER_ORDER {list(LAYER_ORDER)}, got {types}"

for layer in PAYLOAD["exclusion_layers"]:
    assert set(layer) == {"type", "label", "data_available", "geometry_wgs84"}, layer
    # data_available is NOT "is the geometry empty" -- a layer that was never
    # checked and a layer that excludes nothing both carry a null geometry.
    assert isinstance(layer["data_available"], bool)

print(f"exclusion layers: {types}")


# --- the eligible union's holes are real and must survive --------------------

union = PAYLOAD["eligible_union"]
assert union is not None, "synthetic fixture must produce a non-empty eligible union"
assert union["type"] == "MultiPolygon", union["type"]

holes = sum(len(polygon) - 1 for polygon in union["coordinates"])
assert holes > 0, (
    "the fixture's interior canopy pockets must show up as interior rings -- "
    "a union with no holes means hole geometry is being dropped somewhere"
)
vertices = sum(len(ring) for polygon in union["coordinates"] for ring in polygon)
print(f"eligible union: {len(union['coordinates'])} polygons, {vertices} vertices, {holes} holes")


# --- coordinate precision ----------------------------------------------------

def _decimals(value):
    text = repr(float(value))
    return len(text.split(".")[1]) if "." in text and "e" not in text else 0


def _all_coordinates(geometry):
    if geometry is None:
        return
    stack = [geometry["coordinates"]]
    while stack:
        node = stack.pop()
        if isinstance(node, (list, tuple)):
            if node and isinstance(node[0], (int, float)):
                yield from node
            else:
                stack.extend(node)


checked = 0
for geometry in (
    [PAYLOAD["eligible_union"]]
    + [layer["geometry_wgs84"] for layer in PAYLOAD["exclusion_layers"]]
    + [feature["geometry"] for feature in PAYLOAD["suggested_zones"]["features"]]
):
    for value in _all_coordinates(geometry):
        assert _decimals(value) <= COORDINATE_PRECISION_DP, (
            f"coordinate {value!r} carries more than {COORDINATE_PRECISION_DP} dp -- "
            "the rounding walker missed a container type"
        )
        checked += 1

assert checked > 0, "no coordinates were checked"
print(f"coordinate precision: {checked:,} coordinates, all <= {COORDINATE_PRECISION_DP} dp")


# --- scores, and the aspect field the frontend must not render blindly -------

scales = PAYLOAD["scales"]
assert set(scales["bands"]) == {"poor", "fair", "good", "excellent"}, scales["bands"]
assert scales["range"] == [0.0, 100.0]
assert scales["direction"] == "higher_is_better"

for zone in PAYLOAD["zones"]:
    for field in ("rank", "area_acres", "score", "slope_min_pct", "slope_max_pct",
                  "aspect_available", "factors", "percent_of_parcel"):
        assert field in zone, f"zone missing {field}: {sorted(zone)}"
    assert isinstance(zone["aspect_available"], bool)
    # An aspect_factor of 100.0 on flat ground is the neutral default, not a
    # measurement. The flag is the only thing that distinguishes them.
    if not zone["aspect_available"]:
        assert zone["dominant_aspect"] is None, (
            "aspect_available False must not ship a dominant_aspect"
        )

assert set(PAYLOAD["summary"]) == {
    "total_acres", "slope_passing_acres", "eligible_acres",
    "selected_acres", "selected_pct_of_parcel",
}, sorted(PAYLOAD["summary"])

print(f"zones: {len(PAYLOAD['zones'])}  summary: {PAYLOAD['summary']}")


# --- soil and roads degrade, they do not raise -------------------------------
#
# The synthetic run above reaches no network, so the soil and road fetches
# both fail internally and degrade. That IS the soft-degrade case, and it is
# asserted here rather than assumed: these two layers arrive available=False
# with the ground they would have excluded reported as eligible.

soft = {layer["type"]: layer["data_available"] for layer in PAYLOAD["exclusion_layers"]}
assert soft["hydric"] is False and soft["roads"] is False, soft
assert soft["slope"] is True and soft["setback"] is True and soft["canopy"] is True, soft
print(f"soft-degrade flags: {soft}")


# --- the layer-named hard failure --------------------------------------------

class _Boom(Exception):
    pass


def _exploding_dem(*_args, **_kwargs):
    raise _Boom("3DEP did not answer")


import dem_data
import production_zone_payload as pzp

_real_get_dem = pzp.dem_data.get_dem_for_boundary
pzp.dem_data.get_dem_for_boundary = _exploding_dem
try:
    build_production_zone_payload(REFERENCE_BOUNDARY)
except LayerFetchError as e:
    assert e.layer == "elevation", e.layer
    assert "3DEP" not in str(e), "the raw upstream message must not ride along"
    print(f"hard failure names its layer: type={e.layer!r} label={e.label!r}")
else:
    raise AssertionError("a DEM failure must raise LayerFetchError")
finally:
    pzp.dem_data.get_dem_for_boundary = _real_get_dem


# --- the route ---------------------------------------------------------------

api.app.config["TESTING"] = True
client = api.app.test_client()

response = client.post("/api/production-zones", json={})
assert response.status_code == 400, response.status_code
assert "boundary" in response.get_json()["error"]

response = client.post("/api/production-zones", json={"boundary": [[-79.98, 40.64]]})
assert response.status_code == 400, response.status_code

_real_build = api.build_production_zone_payload
api.build_production_zone_payload = lambda *a, **k: (_ for _ in ()).throw(
    LayerFetchError("canopy", "tree canopy height")
)
try:
    response = client.post("/api/production-zones", json={"boundary": REFERENCE_BOUNDARY})
    assert response.status_code == 502, response.status_code
    body = response.get_json()
    assert body["failed_layer"] == {"type": "canopy", "label": "tree canopy height"}, body
    assert "Traceback" not in body["error"] and "Error" not in body["error"], body["error"]
    print(f"route 502 body: {body}")
finally:
    api.build_production_zone_payload = _real_build

api.build_production_zone_payload = lambda *a, **k: PAYLOAD
try:
    response = client.post("/api/production-zones", json={"boundary": REFERENCE_BOUNDARY})
    assert response.status_code == 200, response.status_code
    assert response.get_json()["eligible_union"]["type"] == "MultiPolygon"
    print(f"route 200: {len(response.data):,} B of JSON")
finally:
    api.build_production_zone_payload = _real_build

print("\nALL ASSERTIONS PASSED")
