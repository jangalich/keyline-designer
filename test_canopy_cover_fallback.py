"""
test_canopy_cover_fallback.py

The NLCD Tree Canopy Cover fallback: what it does, what it refuses to do,
and what it costs against the real measurement.

THE PROBLEM IT EXISTS FOR. `canopy_height` is the only layer in this
pipeline with genuine national coverage gaps, and it HARD-FAILS the whole
session -- a parcel with no coverage cannot be used at all, the user
cannot draw a boundary, let alone reach a step. Planetary Computer's
`3dep-lidar-hag` is a DERIVED product keyed per acquisition project, so
its coverage is narrower than 3DEP's own near-complete national lidar.
Confirmed by the user on a Maryland property: no HAG item, and no
`3dep-lidar-dsm` either, so deriving HAG from DSM-DTM does not help.

NINE CHECKS, and the ninth is a separate file (the full suite). Numbered
here as they are in the branch that added them:

  1  A parcel WITH HAG uses HAG, and TCC is fetched EXACTLY zero times.
  2  A parcel WITHOUT HAG falls back to TCC and the session is CREATED.
  3  The source flag names which source was used, on both parcels.
  4  254 and 255 are excluded -- against REAL pixel values.       [LIVE]
  5  Any nonzero cover is canopy; zero is not.
  6  The fallback mask satisfies BOTH consumers: get_required_tree_root_
     zone_mask_utm() (which RAISES rather than degrading) and the
     exclusion gate (which degrades).
  7  Both sources failing still hard-fails, with a message that says the
     data is ABSENT rather than unresponsive.
  8  CALIBRATION: on the Gibsonia reference parcel, where BOTH sources
     exist, run the HAG mask and a TCC-derived mask over the same land
     and report where they disagree, in acres.              [LIVE]

CHECKS 4 AND 8 REQUIRE REAL NETWORK ACCESS and are deliberately NOT
mocked -- 4 is explicitly an assertion against real pixel values rather
than a synthetic raster, and 8 is a measurement of two real products over
real ground, which a fixture cannot produce. They live in the LIVE
section at the bottom, are skipped (loudly, with a non-zero-free report
of WHY) when the hosts are unreachable, and are run by:

    python3 test_canopy_cover_fallback.py --live

Everything above them is offline and runs unconditionally, in this
repo's plain-assert style (test_canopy_height_data.py, test_parcel_
data.py) with unittest.mock standing in for the fetch layer.
"""

import sys
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import Polygon

import canopy_cover_data as ccd
import canopy_height_data as chd
import exclusion_zones as ez
import parcel_data
import production_area as pa
from canopy_cover_data import (
    CANOPY_COVER_THRESHOLD_PCT,
    TCC_BACKGROUND_VALUE,
    TCC_NON_PROCESSING_VALUE,
    _classify_cover,
    canopy_cover_cell_mask,
    canopy_cover_root_zone_mask,
    dem_grid_window,
)
from canopy_height_data import (
    CANOPY_HEIGHT_THRESHOLD_METERS,
    CANOPY_SOURCE_LIDAR_HAG,
    CANOPY_SOURCE_NLCD_TCC,
    canopy_source,
    root_zone_mask_from_canopy,
)
from dem_data import _utm_epsg_for_lonlat
from parcel_data import ParcelDataIncompleteError
from raster_grid import cell_area_acres

LIVE = "--live" in sys.argv


# =====================================================================
# Fixtures: two real boundaries, one synthetic DEM grid per boundary.
# =====================================================================

# THE REFERENCE PARCEL, which HAS HAG coverage -- 5614 N Montour Rd,
# Gibsonia, PA. The same drawn boundary every other module in this repo
# validates against.
GIBSONIA_BOUNDARY = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

# THE MARYLAND BOUNDARY, which does NOT -- a ~13-acre rectangle of
# farmland in Kent County, on the Eastern Shore, a few miles outside
# Chestertown. Written out here as the exact coordinates used, so the
# result is reproducible against the same ground rather than against "a
# Maryland property".
#
#   (-76.0820, 39.2110)   NW
#   (-76.0820, 39.2089)   SW
#   (-76.0793, 39.2089)   SE
#   (-76.0793, 39.2110)   NE
#
# The LIVE section below VERIFIES the premise rather than assuming it: it
# asserts that the STAC search genuinely returns no `3dep-lidar-hag` item
# here. If Microsoft ever processes this project area, that assertion
# fails loudly and says so -- which is the correct outcome, not a test to
# relax.
MARYLAND_BOUNDARY = [
    (-76.0820, 39.2110),
    (-76.0820, 39.2089),
    (-76.0793, 39.2089),
    (-76.0793, 39.2110),
]


def _fake_dem(boundary, rows=24, cols=24, resolution=(5.0, 5.0)):
    """A synthetic DEM on the boundary's OWN UTM zone, sized and placed so
    every cell center falls inside the boundary -- the on-parcel test the
    real fetch applies is a cell-center-containment test, and a grid hung
    somewhere else would make every on-parcel count zero."""
    lons = [p[0] for p in boundary]
    lats = [p[1] for p in boundary]
    center_lon = sum(lons) / len(lons)
    center_lat = sum(lats) / len(lats)
    crs = f"EPSG:{_utm_epsg_for_lonlat(center_lon, center_lat)}"
    xs, ys = warp_transform("EPSG:4326", crs, lons, lats)
    # Centre the grid on the boundary's centroid so it sits inside it.
    cx = (min(xs) + max(xs)) / 2.0
    cy = (min(ys) + max(ys)) / 2.0
    px, py = resolution
    return {
        "array": np.full((rows, cols), 300.0, dtype="float32"),
        "resolution_meters": (px, py),
        "origin_x": cx - (cols / 2.0) * px,
        "origin_y": cy + (rows / 2.0) * py,
        "crs": crs,
    }


GIBSONIA_DEM = _fake_dem(GIBSONIA_BOUNDARY)
MARYLAND_DEM = _fake_dem(MARYLAND_BOUNDARY)


def _hag_canopy(dem, height=1.0):
    """A lidar-HAG canopy dict of the shape get_canopy_height_for_
    boundary() returns on its HAG path."""
    rows, cols = dem["array"].shape
    return {
        "array": np.full((rows, cols), height, dtype="float32"),
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source": CANOPY_SOURCE_LIDAR_HAG,
        "units": "meters_above_ground",
        "source_item_id": "offline-hag-fixture",
    }


def _tcc_canopy(dem, cover):
    """A TCC canopy dict of the shape get_tree_canopy_cover_for_boundary()
    returns."""
    return {
        "array": np.asarray(cover, dtype="float32"),
        "units": "percent_cover",
        "resolution_meters": dem["resolution_meters"],
        "origin_x": dem["origin_x"],
        "origin_y": dem["origin_y"],
        "crs": dem["crs"],
        "source": CANOPY_SOURCE_NLCD_TCC,
        "source_item_id": "offline-tcc-fixture",
        "native_resolution_meters": 30.0,
        "dem_cells_per_source_pixel": 36.0,
        "value_counts": {
            "non_processing_254": 0,
            "background_255": 0,
            "service_nodata": 0,
            "out_of_range": 0,
            "valid_cover": int(np.asarray(cover).size),
        },
        "on_parcel_nodata_pct": 0.0,
    }


def _boundary_polygon_utm(boundary, dem):
    xs, ys = warp_transform(
        "EPSG:4326", dem["crs"], [p[0] for p in boundary], [p[1] for p in boundary]
    )
    return Polygon(zip(xs, ys))


class _CountingTcc:
    """Counts calls to get_tree_canopy_cover_for_boundary() and returns a
    canned TCC dict. Test 1 asserts the count is EXACTLY zero, which is a
    stronger statement than "the HAG array was used" -- it says the
    fallback source was never even contacted."""

    def __init__(self, result):
        self.calls = 0
        self._result = result

    def __call__(self, boundary_coordinates, dem, *args, **kwargs):
        self.calls += 1
        return self._result


print("=" * 72)
print("OFFLINE CHECKS (1, 2, 3, 5, 6, 7)")
print("=" * 72)

# =====================================================================
# 1. A parcel WITH HAG uses HAG. TCC is NOT fetched -- exactly zero calls.
# =====================================================================

_hag_array = np.full((24, 24), 1.0, dtype="float32")
_hag_array[10:14, 10:14] = CANOPY_HEIGHT_THRESHOLD_METERS + 3.0  # a real stand of trees

_stac_item = type(
    "Item",
    (),
    {
        "id": "gibsonia-hag-item",
        # _read_clipped_hag() is stubbed out below, so the href is never
        # opened -- but item.assets[HAG_ASSET_KEY] IS read, deliberately
        # (canopy_height_data.py fails loudly on a missing asset key rather
        # than silently reading the wrong thing), so the fixture carries one.
        "assets": {chd.HAG_ASSET_KEY: type("Asset", (), {"href": "https://example.invalid/hag.tif"})()},
        "geometry": None,
    },
)()

spy_tcc = _CountingTcc(_tcc_canopy(GIBSONIA_DEM, np.full((24, 24), 50.0)))

with mock_patch.object(chd, "_search_hag_items", return_value=[_stac_item]), mock_patch.object(
    chd, "_read_clipped_hag", return_value=(_hag_array, None, GIBSONIA_DEM["crs"], -9999.0)
), mock_patch.object(
    chd, "_reproject_to_dem_grid", return_value=_hag_array
), mock_patch.object(
    chd, "_on_parcel_nan_fraction", return_value=(0.0, 576)
), mock_patch.object(
    ccd, "get_tree_canopy_cover_for_boundary", spy_tcc
):
    canopy_with_hag = chd.get_canopy_height_for_boundary(GIBSONIA_BOUNDARY, GIBSONIA_DEM)

assert canopy_with_hag is not None, "a parcel with HAG coverage must produce a canopy dict"
assert spy_tcc.calls == 0, (
    "TEST 1: a parcel WITH lidar HAG coverage must not contact the TCC fallback at all -- "
    f"get_tree_canopy_cover_for_boundary() was called {spy_tcc.calls} time(s), expected EXACTLY 0"
)
assert canopy_with_hag["array"] is _hag_array, (
    "TEST 1: the HAG array itself must be what the canopy dict carries, not a fallback array"
)
assert canopy_with_hag["units"] == "meters_above_ground"
print(
    "TEST 1 PASS: a parcel WITH HAG uses HAG -- the returned array is the HAG array itself, and "
    f"get_tree_canopy_cover_for_boundary() was called exactly {spy_tcc.calls} times."
)


# --- 1b. ...and the same holds at each of the other two HAG-present exits.
# A tile that intersects but reads all-nodata is NOT "HAG present": it is
# absence, and DOES fall back. Asserted here so the boundary between the
# two is a tested line rather than a comment.
spy_tcc_nodata = _CountingTcc(_tcc_canopy(GIBSONIA_DEM, np.full((24, 24), 50.0)))
_all_nodata = np.full((24, 24), -9999.0, dtype="float32")
with mock_patch.object(chd, "_search_hag_items", return_value=[_stac_item]), mock_patch.object(
    chd, "_read_clipped_hag", return_value=(_all_nodata, None, GIBSONIA_DEM["crs"], -9999.0)
), mock_patch.object(ccd, "get_tree_canopy_cover_for_boundary", spy_tcc_nodata):
    canopy_nodata = chd.get_canopy_height_for_boundary(GIBSONIA_BOUNDARY, GIBSONIA_DEM)
assert spy_tcc_nodata.calls == 1, (
    "TEST 1b: a HAG tile whose every pixel over the boundary is nodata is ABSENCE, and must "
    f"fall back -- expected 1 TCC call, got {spy_tcc_nodata.calls}"
)
assert canopy_source(canopy_nodata) == CANOPY_SOURCE_NLCD_TCC
print("TEST 1b PASS: an all-nodata HAG tile counts as absence and falls back (1 TCC call).")


# --- 1c. A HAG fetch that RAISES does NOT fall back. "Unavailable" is not
# "absent": a source that did not answer may answer next time, and
# swapping in a coarser product for it would hide a real outage.
spy_tcc_raise = _CountingTcc(_tcc_canopy(GIBSONIA_DEM, np.full((24, 24), 50.0)))
with mock_patch.object(
    chd, "_search_hag_items", side_effect=RuntimeError("STAC did not answer")
), mock_patch.object(ccd, "get_tree_canopy_cover_for_boundary", spy_tcc_raise):
    try:
        chd.get_canopy_height_for_boundary(GIBSONIA_BOUNDARY, GIBSONIA_DEM)
        raise AssertionError("TEST 1c: a raising HAG search must propagate, not fall back")
    except RuntimeError as exc:
        assert "STAC did not answer" in str(exc)
assert spy_tcc_raise.calls == 0, (
    "TEST 1c: a HAG fetch that RAISED is a source that did not answer, not an absent one -- "
    f"the fallback must not run for it, but it was called {spy_tcc_raise.calls} time(s)"
)
print("TEST 1c PASS: a raising HAG fetch propagates uncaught and never reaches the fallback.")


# =====================================================================
# 2. A parcel WITHOUT HAG falls back to TCC, and the session is CREATED.
# =====================================================================
# "Created" means fetch_parcel_data() returns a populated ParcelData --
# the exact call POST /api/sessions makes before it can return a session
# at all. Before the fallback this boundary raised ParcelDataIncomplete
# Error here and no session could exist.

_md_cover = np.zeros((24, 24), dtype="float32")
_md_cover[8:16, 8:16] = 40.0  # a woodlot
_md_tcc = _tcc_canopy(MARYLAND_DEM, _md_cover)

spy_tcc_md = _CountingTcc(_md_tcc)
with mock_patch.object(chd, "_search_hag_items", return_value=[]), mock_patch.object(
    ccd, "get_tree_canopy_cover_for_boundary", spy_tcc_md
):
    canopy_md = chd.get_canopy_height_for_boundary(MARYLAND_BOUNDARY, MARYLAND_DEM)

assert spy_tcc_md.calls == 1, (
    f"TEST 2: a boundary with NO HAG item must fall back exactly once, got {spy_tcc_md.calls}"
)
assert canopy_md is _md_tcc, "TEST 2: the fallback's own dict must be what is returned"

_OTHER_LAYERS = {
    "get_dem_for_boundary": MARYLAND_DEM,
    "get_soil_data_for_polygon": [{"mukey": "1", "component_name": "Loam", "pct_of_mapunit": 90}],
    "get_farmland_classification_for_polygon": [{"mukey": "1", "farmlndcl": "Prime"}],
    "get_erosion_factor_for_polygon": [{"mukey": "1", "kwfact": 0.3}],
    "get_saturated_hydraulic_conductivity_for_polygon": [{"mukey": "1", "ksat": 9.0}],
    "get_soil_geometries_for_polygon": {"type": "FeatureCollection", "features": []},
    "get_water_features_for_boundary": {"streams": [], "ponds": []},
    "get_farm_roads_for_boundary": [],
    "get_climate_summary_for_point": {"annual_precip_mm": 1100},
    "get_imagery_summary_for_boundary": {"scene_id": "fixture"},
    "get_regional_irradiance_baseline": {"status": "no_api_key"},
}


def _patched_parcel_fetch(canopy_result):
    """Every Layer 1 fetch stubbed except canopy, which runs the REAL
    get_canopy_height_for_boundary() with its HAG search stubbed out --
    so the fallback path under test is genuinely exercised inside
    fetch_parcel_data(), not replaced by it."""
    from contextlib import ExitStack

    stack = ExitStack()
    for name, value in _OTHER_LAYERS.items():
        stack.enter_context(mock_patch.object(parcel_data, name, return_value=value))
    stack.enter_context(mock_patch.object(chd, "_search_hag_items", return_value=[]))
    stack.enter_context(
        mock_patch.object(ccd, "get_tree_canopy_cover_for_boundary", return_value=canopy_result)
    )
    return stack


with _patched_parcel_fetch(_md_tcc):
    md_parcel = parcel_data.fetch_parcel_data(MARYLAND_BOUNDARY)

assert isinstance(md_parcel, parcel_data.ParcelData), (
    "TEST 2: a Maryland boundary with no HAG coverage must now produce a ParcelData -- "
    "i.e. a session can be created for it at all"
)
assert md_parcel.canopy_height is _md_tcc
print(
    "TEST 2 PASS: a parcel WITHOUT HAG falls back to TCC and fetch_parcel_data() returns a "
    "populated ParcelData -- the session is created where it previously hard-failed."
)


# =====================================================================
# 3. The source flag names WHICH source was used, on BOTH parcels.
# =====================================================================

assert canopy_source(canopy_with_hag) == CANOPY_SOURCE_LIDAR_HAG, (
    "TEST 3: the HAG parcel's canopy dict must name lidar HAG as its source"
)
assert canopy_source(canopy_md) == CANOPY_SOURCE_NLCD_TCC, (
    "TEST 3: the Maryland parcel's canopy dict must name the TCC fallback as its source"
)
assert canopy_source(None) is None, "TEST 3: no canopy means no source, not a default"
assert canopy_source({"array": None}) == CANOPY_SOURCE_LIDAR_HAG, (
    "TEST 3: a canopy dict predating the 'source' key is a HAG dict and must read as one"
)

# ...and the flag SURVIVES to the two places a report and a UI read it:
# exclusion_zones' canopy layer (and its wire entry), and STEP 1's own
# returned gates.
_gib_poly = _boundary_polygon_utm(GIBSONIA_BOUNDARY, GIBSONIA_DEM)
_md_poly = _boundary_polygon_utm(MARYLAND_BOUNDARY, MARYLAND_DEM)


def _exclusion_for(boundary_polygon, dem, canopy):
    return ez.identify_exclusion_zones(
        None,
        dem=dem,
        boundary_polygon_utm=boundary_polygon,
        canopy_height=canopy,
        check_soil=False,
        check_roads=False,
    )


_hag_for_gibsonia = _hag_canopy(GIBSONIA_DEM)
_hag_for_gibsonia["array"] = _hag_array
ex_hag = _exclusion_for(_gib_poly, GIBSONIA_DEM, _hag_for_gibsonia)
ex_tcc = _exclusion_for(_md_poly, MARYLAND_DEM, _md_tcc)

assert ex_hag["layers"]["canopy"]["data_source"] == CANOPY_SOURCE_LIDAR_HAG
assert ex_tcc["layers"]["canopy"]["data_source"] == CANOPY_SOURCE_NLCD_TCC
_wire_canopy_hag = next(e for e in ex_hag["wire"]["layers"] if e["type"] == "canopy")
_wire_canopy_tcc = next(e for e in ex_tcc["wire"]["layers"] if e["type"] == "canopy")
assert _wire_canopy_hag["data_source"] == CANOPY_SOURCE_LIDAR_HAG
assert _wire_canopy_tcc["data_source"] == CANOPY_SOURCE_NLCD_TCC

# data_available is True on BOTH -- the check ran on both parcels. That is
# exactly why a second field was needed: availability cannot say "ran, but
# on the coarser product".
assert _wire_canopy_hag["data_available"] is True
assert _wire_canopy_tcc["data_available"] is True

# ...and no OTHER layer grows a data_source, because no other layer has two.
for entry in ex_tcc["wire"]["layers"]:
    if entry["type"] != "canopy":
        assert "data_source" not in entry, (
            f"TEST 3: only the canopy layer has two sources; {entry['type']} must not carry a "
            "data_source key"
        )

step1_tcc = pa.compute_step1_eligible_cells(
    MARYLAND_DEM, _md_poly, exclusion_result=ex_tcc, tree_root_zone_mask_utm=None
)
assert step1_tcc["canopy_data_source"] == CANOPY_SOURCE_NLCD_TCC, (
    "TEST 3: STEP 1 must carry the canopy source out of the exclusion result it consumed"
)
assert step1_tcc["canopy_data_available"] is True
step1_hag = pa.compute_step1_eligible_cells(
    GIBSONIA_DEM, _gib_poly, exclusion_result=ex_hag, tree_root_zone_mask_utm=None
)
assert step1_hag["canopy_data_source"] == CANOPY_SOURCE_LIDAR_HAG

# A gate that never ran has no source at all, whatever a caller passes.
step1_unchecked = pa.compute_step1_eligible_cells(
    MARYLAND_DEM, _md_poly, canopy_source=CANOPY_SOURCE_NLCD_TCC
)
assert step1_unchecked["canopy_data_available"] is False
assert step1_unchecked["canopy_data_source"] is None, (
    "TEST 3: an unchecked canopy gate must report no source, not the source it would have used"
)
print(
    "TEST 3 PASS: canopy_data_source reads 'lidar_hag' on the HAG parcel and 'nlcd_tcc' on the "
    "fallback parcel -- on the canopy dict, on exclusion_zones' canopy layer, on its wire entry "
    "and on STEP 1's gates -- while data_available is True for both."
)


# =====================================================================
# 5. Any nonzero cover produces canopy in the mask; zero does not.
# =====================================================================
# (4 is the LIVE 254/255 check against real pixels, below.)

_cover = np.array(
    [
        [0.0, 0.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 3.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 100.0, 0.0],
        [0.0, 0.0, 0.0, 0.0, 0.0],
    ],
    dtype="float32",
)
_cells = canopy_cover_cell_mask(_cover)
assert _cells[1, 1] and _cells[2, 2] and _cells[3, 3], (
    "TEST 5: 1%, 3% and 100% cover must ALL read as canopy -- any nonzero value is canopy"
)
assert not _cells[0, 0] and not _cells[4, 4], "TEST 5: 0% cover must NOT read as canopy"
assert int(_cells.sum()) == 3, f"TEST 5: exactly the three nonzero cells, got {int(_cells.sum())}"
assert CANOPY_COVER_THRESHOLD_PCT == 0, (
    "TEST 5: the threshold is zero and is not a tuning knob -- a threshold above zero would "
    "miss the thin strips TCC already under-detects, in the unsafe direction"
)

# The smallest representable nonzero value on a U8 product is 1. It counts.
assert canopy_cover_cell_mask(np.array([[1.0]], dtype="float32"))[0, 0], (
    "TEST 5: cover of exactly 1% is canopy"
)
assert not canopy_cover_cell_mask(np.array([[0.0]], dtype="float32"))[0, 0]
# NaN (an excluded 254/255/nodata cell) is never canopy -- it is not
# "checked and clear" either, but it must not be counted as tree cover.
assert not canopy_cover_cell_mask(np.array([[np.nan]], dtype="float32"))[0, 0], (
    "TEST 5: a cell with no usable cover value must never read as canopy"
)
print(
    "TEST 5 PASS: 1%, 3% and 100% cover all read as canopy; 0% does not; NaN (254/255/nodata) "
    "does not; the threshold constant is exactly 0."
)


# --- 5b. The dilation is the SAME operation the HAG path applies, so a
# calibration between the two masks measures the MEASUREMENT and not the
# buffer. Single canopy cell, same grid, same buffer, both paths.
_one_cover = np.zeros((15, 15), dtype="float32")
_one_cover[7, 7] = 5.0
_one_hag = np.zeros((15, 15), dtype="float32")
_one_hag[7, 7] = CANOPY_HEIGHT_THRESHOLD_METERS + 1.0
_tcc_ring = canopy_cover_root_zone_mask(_one_cover, (5.0, 5.0))
_hag_ring = chd.tree_root_zone_mask(_one_hag, (5.0, 5.0))
assert np.array_equal(_tcc_ring, _hag_ring), (
    "TEST 5b: one canopy cell must dilate to the SAME root zone on both paths -- the two masks "
    "must differ by the measurement and by nothing else"
)
print("TEST 5b PASS: both sources dilate one canopy cell to a byte-identical root zone.")


# --- 5c. root_zone_mask_from_canopy() dispatches on source, and refuses an
# unknown one rather than guessing. A unit error here would be invisible:
# 4.5 read as a percentage marks nearly everything wooded, 15 read as
# metres marks nearly nothing.
_tcc_dict = _tcc_canopy(_fake_dem(MARYLAND_BOUNDARY, 15, 15), _one_cover)
_hag_dict = _hag_canopy(_fake_dem(GIBSONIA_BOUNDARY, 15, 15))
_hag_dict["array"] = _one_hag
assert np.array_equal(root_zone_mask_from_canopy(_tcc_dict), _tcc_ring)
assert np.array_equal(root_zone_mask_from_canopy(_hag_dict), _hag_ring)
try:
    root_zone_mask_from_canopy({"source": "guesswork", "array": _one_cover, "resolution_meters": (5.0, 5.0)})
    raise AssertionError("TEST 5c: an unrecognised canopy source must raise, not be guessed at")
except ValueError as exc:
    assert "guesswork" in str(exc)
print("TEST 5c PASS: the mask derivation dispatches on source and refuses an unknown one.")


# =====================================================================
# 6. The fallback mask satisfies BOTH consumers.
# =====================================================================
# The strict one is get_required_tree_root_zone_mask_utm(): it RAISES
# rather than degrading, and the "required" is in the name. The lenient
# one is the exclusion gate, which produces a standing caveat when a layer
# is unavailable. Both take a boolean mask over the 5 m DEM grid.

required_mask = pa.get_required_tree_root_zone_mask_utm(
    _md_poly, MARYLAND_DEM, canopy_height=_md_tcc
)
assert required_mask.dtype == bool, (
    "TEST 6: get_required_tree_root_zone_mask_utm() must return a BOOLEAN mask on the fallback"
)
assert required_mask.shape == MARYLAND_DEM["array"].shape, (
    f"TEST 6: the fallback mask must be on the DEM's own grid -- got {required_mask.shape}, "
    f"DEM is {MARYLAND_DEM['array'].shape}"
)
assert required_mask.any(), (
    "TEST 6: the fallback parcel's woodlot must actually reach the mask, or this check is vacuous"
)
assert not required_mask.all(), "TEST 6: ...and it must not blanket the whole parcel"

# It did NOT raise -- which is the whole point. The same call on a parcel
# with no canopy source at all still does.
with mock_patch.object(chd, "_search_hag_items", return_value=[]), mock_patch.object(
    ccd, "get_tree_canopy_cover_for_boundary", return_value=None
):
    try:
        pa.get_required_tree_root_zone_mask_utm(_md_poly, MARYLAND_DEM)
        raise AssertionError(
            "TEST 6: with BOTH sources empty, the required mask must still raise -- the fallback "
            "is not an exemption from the mandatory gate"
        )
    except RuntimeError as exc:
        assert "Canopy height data unavailable" in str(exc)

# Consumer two: the exclusion gate. Same mask, real gate, real acreage.
assert ex_tcc["layers"]["canopy"]["acres"] > 0, (
    "TEST 6: the exclusion gate must actually exclude ground from the fallback mask"
)
assert ex_tcc["layers"]["canopy"]["mask"].dtype == bool
assert ex_tcc["layers"]["canopy"]["mask"].shape == MARYLAND_DEM["array"].shape
assert ex_tcc["eligible_mask"].shape == MARYLAND_DEM["array"].shape
# The canopy gate is evaluated over slope_only_mask, so its hits must be a
# subset of it -- the same contract the HAG path satisfies.
assert not (ex_tcc["layers"]["canopy"]["mask"] & ~ex_tcc["slope_only_mask"]).any()
print(
    "TEST 6 PASS: the fallback mask is a boolean array on the DEM grid that "
    "get_required_tree_root_zone_mask_utm() returns without raising, and that the exclusion gate "
    f"excludes {ex_tcc['layers']['canopy']['acres']:.2f} acres with."
)


# =====================================================================
# 7. Both sources failing still HARD-FAILS, and says ABSENT not DOWN.
# =====================================================================

with _patched_parcel_fetch(None):
    try:
        parcel_data.fetch_parcel_data(MARYLAND_BOUNDARY)
        raise AssertionError(
            "TEST 7: with BOTH canopy sources empty, fetch_parcel_data() must still hard-fail"
        )
    except ParcelDataIncompleteError as exc:
        both_failed = exc

assert both_failed.layer == "canopy" and both_failed.label == "tree canopy height"
assert both_failed.reason == ParcelDataIncompleteError.REASON_NO_DATA_FOR_PARCEL, (
    "TEST 7: the reason must say the data is ABSENT for this land, not that a source was down"
)
_msg = str(both_failed)
assert "no canopy data exists" in _msg.lower(), f"TEST 7: message must say absent -- got {_msg!r}"
assert "not a service outage" in _msg.lower()
assert "retrying will not help" in _msg.lower()
assert "NLCD Tree Canopy Cover fallback" in _msg, (
    "TEST 7: the message must say the fallback was tried too, or a reader cannot tell whether "
    "one source or both is out"
)

# ...and what the API actually puts on the wire.
import session_api  # noqa: E402

_payload = session_api._failed_layer_payload(both_failed)
assert _payload["failed_layer"] == {
    "type": "canopy",
    "label": "tree canopy height",
    "reason": ParcelDataIncompleteError.REASON_NO_DATA_FOR_PARCEL,
}
assert "no tree canopy height data available for this land" in _payload["error"].lower()
assert "could not be retrieved" not in _payload["error"], (
    "TEST 7: 'could not be retrieved' is outage wording and must not be what a permanent gap says"
)

# The other branch is unchanged: a source that genuinely did not answer
# still reads as an outage.
_down = ParcelDataIncompleteError(
    "soil source did not answer",
    "soil",
    "soil data",
    reason=ParcelDataIncompleteError.REASON_SOURCE_UNAVAILABLE,
)
_down_payload = session_api._failed_layer_payload(_down)
assert _down_payload["error"] == "The soil data could not be retrieved."
# ...as does a raise site that recorded no reason at all.
_unknown_payload = session_api._failed_layer_payload(
    ParcelDataIncompleteError("x", "soil", "soil data")
)
assert _unknown_payload["error"] == "The soil data could not be retrieved."
assert "reason" not in _unknown_payload["failed_layer"]
print("TEST 7 PASS: both sources empty still hard-fails; the message and the wire say ABSENT.")
print(f"           message: {_payload['error']}")


# =====================================================================
# Offline support checks for the LIVE ones (so a skip is not total).
# =====================================================================
# 4's LOGIC, against hand-built pixel values. This does NOT replace the
# live check -- the branch requirement is an assertion against real pixel
# values -- but it does mean the classifier itself is never untested.

_band = np.array(
    [[0, 3, 50], [100, TCC_NON_PROCESSING_VALUE, TCC_BACKGROUND_VALUE], [7, 0, 101]], dtype="uint8"
)
_classified, _counts = _classify_cover(_band, service_nodata=None)
assert np.isnan(_classified[1, 1]), "254 (non-processing) must be excluded, not read as 254% cover"
assert np.isnan(_classified[1, 2]), "255 (background) must be excluded, not read as 255% cover"
assert np.isnan(_classified[2, 2]), "101 is not a percentage and must be excluded"
assert _classified[0, 1] == 3.0 and _classified[1, 0] == 100.0
assert _counts["non_processing_254"] == 1 and _counts["background_255"] == 1
assert _counts["out_of_range"] == 1 and _counts["valid_cover"] == 6
_m = canopy_cover_cell_mask(_classified)
assert not _m[1, 1] and not _m[1, 2], (
    "under an any-nonzero rule, 254 and 255 would BOTH read as canopy if they were not excluded "
    "-- and 255, which is what a service returns for ground not in the dataset, could blanket a "
    "whole parcel as wooded"
)
print(
    "TEST 4 (offline half) PASS: _classify_cover() excludes 254 and 255 by value and counts them "
    "separately; neither reaches the canopy mask. The real-pixel half is LIVE, below."
)

# The resampling contract, asserted without a fetch: the exportImage
# request is the DEM's OWN window, so nothing is reprojected afterward.
_win = dem_grid_window(MARYLAND_DEM)
_rows, _cols = MARYLAND_DEM["array"].shape
_px, _py = MARYLAND_DEM["resolution_meters"]
assert _win["size"] == (_cols, _rows), "the request must be for the DEM's exact pixel dimensions"
assert _win["bbox"][0] == MARYLAND_DEM["origin_x"]
assert _win["bbox"][3] == MARYLAND_DEM["origin_y"]
assert abs(_win["bbox"][2] - (MARYLAND_DEM["origin_x"] + _cols * _px)) < 1e-6
assert abs(_win["bbox"][1] - (MARYLAND_DEM["origin_y"] - _rows * _py)) < 1e-6
assert _win["crs"] == MARYLAND_DEM["crs"], "...in the DEM's own CRS"
_blockiness = (ccd.TCC_NATIVE_RESOLUTION_METERS / ((_px + _py) / 2.0)) ** 2
assert 30 < _blockiness < 45, (
    f"one 30m TCC pixel should span ~36 cells of a 5m grid, computed {_blockiness}"
)
print(
    f"RESAMPLING PASS: the TCC request is the DEM's exact window ({_cols}x{_rows} at "
    f"{_px:.2f}x{_py:.2f}m, {_win['crs']}), nearest-neighbour, so nothing is reprojected after "
    f"it. One 30m source pixel spans ~{_blockiness:.0f} DEM cells -- the mask is blocky in a way "
    "a HAG mask is not."
)


print()
print("All OFFLINE canopy-cover-fallback checks passed (1, 1b, 1c, 2, 3, 5, 5b, 5c, 6, 7).")
print()


# =====================================================================
# LIVE SECTION -- checks 4 and 8. Real network, real pixels, real ground.
# =====================================================================
# Run with:  python3 test_canopy_cover_fallback.py --live
#
# These two are NOT mocked and must not be: 4 is explicitly an assertion
# against REAL pixel values rather than a synthetic raster, and 8 is a
# measurement of two real products over real ground, which no fixture can
# produce. A skip here is reported as a skip, never as a pass.

def _live_or_skip():
    if not LIVE:
        print("LIVE CHECKS SKIPPED: re-run with --live to fetch real data.")
        return False
    return True


class LiveDataUnreachable(Exception):
    """The live checks could not RUN -- a host was unreachable. Distinct
    from a live check that ran and FAILED, which is an ordinary assertion
    error. Never swallowed into a pass: the runner below reports it and
    exits non-zero."""


def _run_live():
    import requests

    from dem_data import get_dem_for_boundary

    def _unreachable(host, exc):
        raise LiveDataUnreachable(
            f"{host} could not be reached ({type(exc).__name__}: {exc})"
        ) from exc

    # ---- 4. 254 and 255, against REAL pixel values ------------------
    print("=" * 72)
    print("TEST 4 (LIVE): 254/255 against real NLCD TCC pixels")
    print("=" * 72)
    try:
        md_dem = get_dem_for_boundary(MARYLAND_BOUNDARY)
    except requests.exceptions.RequestException as exc:
        _unreachable("elevation.nationalmap.gov (USGS 3DEP ImageServer)", exc)
    try:
        band, service_nodata = ccd._retry(
            lambda timeout: ccd._export_tcc_on_dem_grid(md_dem, timeout), max_retries=2
        )
    except requests.exceptions.RequestException as exc:
        _unreachable("apps.fs.usda.gov (USDA FS NLCD TCC ImageServer)", exc)
    uniq, uniq_counts = np.unique(band, return_counts=True)
    print(f"  raw TCC values returned over the Maryland parcel: {dict(zip(uniq.tolist(), uniq_counts.tolist()))}")
    print(f"  service-declared nodata: {service_nodata}")
    classified, counts = _classify_cover(band, service_nodata)
    print(f"  classified: {counts}")

    # ---- THE PREMISE, VERIFIED RATHER THAN ASSUMED ------------------
    # This boundary is in the suite because it has NO 3dep-lidar-hag
    # coverage. If Microsoft ever processes this acquisition project, the
    # assertion below fails loudly and says so -- which is the correct
    # outcome (the fixture has stopped testing what it claims to), not a
    # check to relax.
    from shapely.geometry import Polygon as _Poly

    _md_items = chd._search_hag_items(_Poly(MARYLAND_BOUNDARY + [MARYLAND_BOUNDARY[0]]))
    print(f"  3dep-lidar-hag items intersecting the Maryland boundary: {len(_md_items)}")
    assert _md_items == [], (
        "the Maryland boundary is in this suite BECAUSE it has no 3dep-lidar-hag coverage, and "
        f"the STAC search just returned {len(_md_items)} item(s) for it. Either the premise has "
        "changed (Microsoft processed this project area) or the boundary is wrong -- pick a "
        "boundary that is still uncovered rather than relaxing this."
    )
    _md_canopy = chd.get_canopy_height_for_boundary(MARYLAND_BOUNDARY, md_dem)
    assert _md_canopy is not None and canopy_source(_md_canopy) == CANOPY_SOURCE_NLCD_TCC, (
        "...and with HAG genuinely absent, the live fetch must come back on the TCC fallback"
    )
    print(f"  live canopy source for the Maryland parcel: {canopy_source(_md_canopy)!r}")
    print(f"  {ccd.summarize_tree_canopy_cover(_md_canopy)}")
    n254 = counts["non_processing_254"]
    n255 = counts["background_255"]
    if n254 or n255:
        assert np.isnan(classified[band == TCC_NON_PROCESSING_VALUE]).all() if n254 else True
        assert np.isnan(classified[band == TCC_BACKGROUND_VALUE]).all() if n255 else True
        mask = canopy_cover_cell_mask(classified)
        if n254:
            assert not mask[band == TCC_NON_PROCESSING_VALUE].any(), (
                "254 reached the canopy mask -- non-processing area was marked wooded"
            )
        if n255:
            assert not mask[band == TCC_BACKGROUND_VALUE].any(), (
                "255 reached the canopy mask -- background was marked wooded"
            )
        print(f"  TEST 4 PASS: {n254} cells were 254 and {n255} were 255; none reached the mask.")
    else:
        print(
            "  TEST 4: neither 254 nor 255 appeared on this parcel. Reported as a finding, not a "
            "pass of the exclusion itself -- the exclusion's own logic is covered by the offline "
            "half above, which asserts both codes by value."
        )

    # ---- 8. CALIBRATION, on ground the user knows -------------------
    print()
    print("=" * 72)
    print("TEST 8 (LIVE): HAG vs TCC over the SAME land -- the Gibsonia parcel")
    print("=" * 72)
    print("  NOT a pass/fail. It measures how much the fallback over- or under-includes")
    print("  against the real measurement, on ground the user knows.")
    try:
        gib_dem = get_dem_for_boundary(GIBSONIA_BOUNDARY)
    except requests.exceptions.RequestException as exc:
        _unreachable("elevation.nationalmap.gov (USGS 3DEP ImageServer)", exc)

    hag = chd.get_canopy_height_for_boundary(GIBSONIA_BOUNDARY, gib_dem)
    assert hag is not None and canopy_source(hag) == CANOPY_SOURCE_LIDAR_HAG, (
        "TEST 8 requires the Gibsonia parcel to genuinely HAVE HAG coverage -- it did not, so "
        "there is nothing to calibrate against"
    )
    tcc = ccd.get_tree_canopy_cover_for_boundary(GIBSONIA_BOUNDARY, gib_dem)
    assert tcc is not None, "TEST 8 requires TCC coverage on the same parcel"

    gib_poly = _boundary_polygon_utm(GIBSONIA_BOUNDARY, gib_dem)
    from shapely.geometry import Point
    from shapely.prepared import prep

    from raster_grid import pixel_center_xy

    prepared = prep(gib_poly)
    rows, cols = gib_dem["array"].shape
    on_parcel = np.zeros((rows, cols), dtype=bool)
    for r in range(rows):
        for c in range(cols):
            if prepared.contains(Point(pixel_center_xy(gib_dem, r, c))):
                on_parcel[r, c] = True

    hag_mask = chd.tree_root_zone_mask(hag["array"], hag["resolution_meters"]) & on_parcel
    tcc_mask = canopy_cover_root_zone_mask(tcc["array"], tcc["resolution_meters"]) & on_parcel

    acres = cell_area_acres(gib_dem)
    parcel_acres = int(on_parcel.sum()) * acres
    both = hag_mask & tcc_mask
    hag_only = hag_mask & ~tcc_mask
    tcc_only = tcc_mask & ~hag_mask

    def a(mask):
        return int(mask.sum()) * acres

    print(f"  parcel (on-parcel cells):            {parcel_acres:8.2f} ac")
    print(f"  canopy by HAG   (lidar, >= {CANOPY_HEIGHT_THRESHOLD_METERS}m):   {a(hag_mask):8.2f} ac"
          f"  ({100 * a(hag_mask) / parcel_acres:5.1f}% of parcel)")
    print(f"  canopy by TCC   (any nonzero cover): {a(tcc_mask):8.2f} ac"
          f"  ({100 * a(tcc_mask) / parcel_acres:5.1f}% of parcel)")
    print(f"  agreed by BOTH:                      {a(both):8.2f} ac")
    print(f"  HAG only (TCC MISSED it):            {a(hag_only):8.2f} ac")
    print(f"  TCC only (TCC OVER-INCLUDED):        {a(tcc_only):8.2f} ac")
    print(f"  net: TCC marks {a(tcc_mask) - a(hag_mask):+.2f} ac more canopy than HAG")
    if a(hag_mask) > 0:
        print(f"  TCC recovers {100 * a(both) / a(hag_mask):.1f}% of the ground HAG calls canopy")
    print(f"  TCC value counts on this parcel: {tcc['value_counts']}")
    print(f"  one 30m TCC pixel spans ~{tcc['dem_cells_per_source_pixel']:.0f} DEM cells")

    return True


if _live_or_skip():
    try:
        _run_live()
    except LiveDataUnreachable as exc:
        # NOT A PASS, AND NOT A FAILING ASSERTION EITHER. The checks did
        # not run. Exit non-zero so no runner can read this as green.
        print()
        print("=" * 72)
        print("LIVE CHECKS 4 AND 8 DID NOT RUN -- HOST UNREACHABLE")
        print("=" * 72)
        print(f"  {exc}")
        print()
        print("  These two checks are deliberately not mocked: 4 asserts against REAL pixel")
        print("  values and 8 measures two real products over real ground. Neither can be")
        print("  satisfied from a fixture, so neither is reported as passing here.")
        print("  Re-run from an environment with egress to elevation.nationalmap.gov,")
        print("  planetarycomputer.microsoft.com and apps.fs.usda.gov.")
        sys.exit(2)
    print()
    print("All LIVE canopy-cover-fallback checks completed (4, 8).")
