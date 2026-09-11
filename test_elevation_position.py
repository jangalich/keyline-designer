"""
test_elevation_position.py

Offline (no-network) checks for production_area_ceiling.py's
elevation_position -- the WORDS ("lower field" / "mid field" / "upper
field") that narrative_data emits beside, never instead of,
elevation_percentile_of_parcel.

WHY THE WORDS EXIST AT ALL, since the number was already there: the data
panel puts every field in one narrow column, so a bare 68 sitting under a
"42.9 /100 score" reads as a SECOND score. The report keeps reading the
number and explaining the axis inline ("at the 68 elevation percentile of
the parcel (0 = the parcel's lowest ground, 100 = its highest)") -- prose
the panel has no room for. Both consumers are served, off one computation.

Scenarios, each isolating ONE thing:

  1. The words match ELEVATION_POSITION_BANDS exactly -- AT each cut point
     and on either side of it, and across a full sweep of the range.
     Derived FROM the constant, never from hardcoded cut numbers, so
     retuning the bands retunes this file with them.
  2. A None percentile yields a None position, never a default word --
     checked as a unit AND end to end on a parcel with no relief, which is
     the real case that produces it.
  3. The bands are readable by an IMPORTER. Trees and structures are
     downstream of production and will read this constant rather than
     declaring their own cuts (see production_area_ceiling.py's module
     docstring), so this asserts the constant is public, importable, and
     sufficient on its own to reproduce _elevation_position()'s answers.
  4. position_in_parcel is UNCHANGED -- still the 8-point map bearing it
     always was, and its vocabulary is disjoint from the elevation words
     so the two can never be mistaken for each other in one panel.
  5. End to end: a real pipeline run over three benches at low, middle and
     high ground reports the three words in elevation order, and every
     patch's word agrees with its own percentile banded independently.
"""

import functools
import importlib
import json
import math
from unittest.mock import patch as mock_patch

import numpy as np
from rasterio.warp import transform as warp_transform
from shapely.geometry import box

import production_area as pa
import production_area_ceiling as pac
from production_area import compute_step1_eligible_cells

# Same isolation every other narrative_data fixture in this pipeline uses:
# these grids are sized exactly to their own eligible ground, so the real
# boundary setback would shave an unpredictable ring off them and break
# assertions that have nothing to do with the setback. Patched on BOTH
# names, since identify_optimized_production_areas() calls its own module
# reference.
compute_step1_eligible_cells = functools.partial(compute_step1_eligible_cells, boundary_setback_meters=0.0)
pac.compute_step1_eligible_cells = compute_step1_eligible_cells

RESOLUTION = (5.0, 5.0)
CRS = "EPSG:32617"


def _dem(array: np.ndarray, origin_y: float = 4500600.0) -> dict:
    return {
        "array": array,
        "resolution_meters": RESOLUTION,
        "origin_x": 500000.0,
        "origin_y": origin_y,
        "crs": CRS,
    }


def _boundary(dem: dict):
    """Full-grid-extent parcel boundary, in UTM and in WGS84 -- every cell
    center in the grid sits inside it."""
    rows, cols = dem["array"].shape
    px, py = dem["resolution_meters"]
    boundary_utm = box(
        dem["origin_x"], dem["origin_y"] - rows * py, dem["origin_x"] + cols * px, dem["origin_y"]
    )
    lons, lats = warp_transform(CRS, "EPSG:4326", *boundary_utm.exterior.coords.xy)
    return boundary_utm, list(zip(lons, lats))


def _run(dem, boundary_coords, canopy_mask=None):
    """The real entry point, offline: the mandatory canopy fetch replaced
    by an exact synthetic mask, soil and roads left on their own 'not
    checked' paths. Patches the name production_area_ceiling.py itself
    calls, so the fixture controls precisely which cells survive."""
    if canopy_mask is None:
        canopy_mask = np.zeros(dem["array"].shape, dtype=bool)
    with mock_patch.object(
        pac,
        "get_required_tree_root_zone_mask_utm",
        lambda boundary_polygon_utm, dem_arg, canopy_height=None: canopy_mask,
    ):
        return pac.identify_optimized_production_areas(
            boundary_coords, dem=dem, check_soil=False, check_roads=False, ceiling_pct=100.0
        )


# ======================================================================
# 1. THE WORDS MATCH THE BANDS -- AT THE CUTS AND EITHER SIDE
# ======================================================================
#
# THE CUT POINTS ARE READ OFF THE CONSTANT, NOT TYPED IN. A test that
# hardcodes 33.3 and 66.7 passes a retune that silently moves them and
# stops testing the thing it names. Everything below is derived from
# ELEVATION_POSITION_BANDS itself, so retuning the bands retunes the test.

BANDS = pac.ELEVATION_POSITION_BANDS
ORDERED = sorted(BANDS.items(), key=lambda kv: kv[1][0])

assert len(ORDERED) >= 2, "a single band would make the whole field a constant word"
assert ORDERED[0][1][0] == 0.0, f"the bands must start at 0: {ORDERED[0]}"
assert ORDERED[-1][1][1] == 100.0, f"the bands must close at 100: {ORDERED[-1]}"
for _left, _right in zip(ORDERED, ORDERED[1:]):
    assert _left[1][1] == _right[1][0], (
        "bands must be gapless and non-overlapping -- a percentile between two of them would "
        f"have no word at all: {_left[0]} ends at {_left[1][1]}, {_right[0]} starts at {_right[1][0]}"
    )

# EVERY CUT POINT, AND EITHER SIDE OF IT. The bounds are lower-inclusive /
# upper-EXCLUSIVE (narrative_data declares exactly that), so the cut point
# itself must belong to the band ABOVE it and the value just below it to
# the band below -- which is the single assertion that would catch an
# off-by-one interval convention. The step is 0.1 because
# elevation_percentile_of_parcel is itself rounded to 1 decimal place: the
# value just below a cut, in the data that actually exists, is cut - 0.1.
_cuts = [low for _, (low, _high) in ORDERED[1:]]
assert _cuts, "expected at least one interior cut point"

_cut_report = []
for _cut in _cuts:
    _below = round(_cut - 0.1, 1)
    _at = pac._elevation_position(_cut)
    _under = pac._elevation_position(_below)
    _over = pac._elevation_position(round(_cut + 0.1, 1))

    _expected_below = next(name for name, (lo, hi) in ORDERED if lo <= _below < hi)
    _expected_at = next(name for name, (lo, hi) in ORDERED if lo <= _cut < hi)

    assert _under == _expected_below, (
        f"{_below} sits below the {_cut} cut and must read {_expected_below!r}, got {_under!r}"
    )
    assert _at == _expected_at, (
        f"bounds are lower-inclusive/upper-exclusive, so the cut point {_cut} itself belongs to the "
        f"band ABOVE it ({_expected_at!r}) -- got {_at!r}"
    )
    assert _over == _expected_at, f"{_cut} + 0.1 must read {_expected_at!r}, got {_over!r}"
    assert _under != _at, f"the {_cut} cut must actually change the word; both sides read {_at!r}"
    _cut_report.append((_below, _under, _cut, _at))

# The two closed ends, which no interior cut covers.
assert pac._elevation_position(0.0) == ORDERED[0][0], "the parcel's lowest ground must take the first band"
assert pac._elevation_position(100.0) == ORDERED[-1][0], (
    "100.0 is the top band's CLOSING edge -- an exclusive upper bound cannot match it, so it needs its "
    "own handling, and the parcel's highest ground must not fall out of the bands entirely"
)

# ...and the whole range, at the resolution the data is emitted in: every
# value 0.0-100.0 gets exactly one word, and it is the band's own.
_swept = 0
for _tenth in range(0, 1001):
    _value = round(_tenth / 10.0, 1)
    _word = pac._elevation_position(_value)
    _expected = next(
        (name for name, (lo, hi) in ORDERED if lo <= _value < hi),
        ORDERED[-1][0] if _value == 100.0 else None,
    )
    assert _word == _expected, f"{_value} -> {_word!r}, but the bands say {_expected!r}"
    assert _word in BANDS, f"{_value} produced {_word!r}, which is not a declared band"
    _swept += 1

print(
    f"1. WORDS MATCH THE BANDS: {len(BANDS)} bands "
    f"({', '.join(f'{n} [{lo}, {hi})' for n, (lo, hi) in ORDERED)}), gapless over [0, 100]. "
    f"At every cut point and either side: "
    + "; ".join(f"{b} -> {bw!r} | {c} -> {cw!r}" for b, bw, c, cw in _cut_report)
    + f". Swept all {_swept} one-decimal values in range, every one banded correctly."
)


# ======================================================================
# 2. A None PERCENTILE IS A None POSITION -- NEVER A DEFAULT WORD
# ======================================================================
#
# THE FAILURE THIS GUARDS. A parcel with no relief has no lowest and
# highest ground to sit between, so elevation_percentile_of_parcel is
# None there. A function that reached for a middle band anyway would
# emit "mid field" -- a word the reader cannot tell from a measurement,
# on ground where the concept does not apply. That is the null-not-zero
# rule this whole block obeys, applied to a category instead of a number.

assert pac._elevation_position(None) is None, (
    "a None percentile must produce a None position, not a default word"
)

# End to end, on the case that actually produces it: a dead-flat parcel.
# build_narrative_data() sets parcel_elevation_range to None when the
# parcel's own min and max elevation are equal, and that None has to carry
# all the way through to the word.
flat_dem = _dem(np.full((40, 40), 300.0, dtype=np.float32))
flat_boundary_utm, flat_coords = _boundary(flat_dem)
flat_result = _run(flat_dem, flat_coords)
flat_patches = flat_result["narrative_data"]["patches"]

assert flat_patches, "expected the flat fixture to still produce patches"
for _p in flat_patches:
    assert _p["elevation_percentile_of_parcel"] is None, (
        f"a parcel with no relief has no percentile to report -- patch {_p['id']} got "
        f"{_p['elevation_percentile_of_parcel']!r}"
    )
    assert "elevation_position" in _p, "the key must be present even when its value is unknown"
    assert _p["elevation_position"] is None, (
        f"patch {_p['id']} invented {_p['elevation_position']!r} on ground where 'upper' and 'lower' "
        "mean nothing -- None is the only honest answer, and the panel renders it as an em-dash"
    )
    # Not any of the words, stated separately from "is None" so a future
    # empty-string or "unknown" sentinel fails here too.
    assert _p["elevation_position"] not in BANDS

print(
    f"2. None PERCENTILE -> None POSITION: _elevation_position(None) is None, and end to end on a "
    f"dead-flat parcel all {len(flat_patches)} patch(es) report elevation_percentile_of_parcel=None "
    "alongside elevation_position=None -- never a default word."
)


# ======================================================================
# 3. THE BANDS ARE READABLE BY AN IMPORTER
# ======================================================================
#
# Trees and structures need the SAME words on the SAME parcel and are both
# downstream of production, so both will import this constant rather than
# declaring their own cuts. This asserts the constant can actually carry
# that: public name, importable, and sufficient ON ITS OWN -- an importer
# holding only the bands must be able to reproduce every answer
# _elevation_position() gives, without reaching for a private helper.

assert not "ELEVATION_POSITION_BANDS".startswith("_"), "a private name cannot be the shared definition"
assert hasattr(pac, "ELEVATION_POSITION_BANDS")

# The import an actual downstream module will write, through a fresh
# module object rather than this file's existing binding.
_fresh = importlib.import_module("production_area_ceiling")
from production_area_ceiling import ELEVATION_POSITION_BANDS as _imported  # noqa: E402

assert _imported is _fresh.ELEVATION_POSITION_BANDS is BANDS, (
    "an importer must get the SAME mapping this module bands against, not a copy that can drift"
)
assert isinstance(_imported, dict) and _imported, "the bands must be a non-empty mapping"
for _name, _bounds in _imported.items():
    assert isinstance(_name, str) and _name, f"band name {_name!r} is not usable as a word"
    assert isinstance(_bounds, list) and len(_bounds) == 2, f"band {_name} bounds are not a [low, high] pair"
    _low, _high = _bounds
    assert isinstance(_low, float) and isinstance(_high, float), f"band {_name} bounds are not floats"
    assert _low < _high, f"band {_name} is empty or inverted: {_bounds}"

# SUFFICIENT ON ITS OWN. This is the classifier a downstream step would
# write, using nothing but the imported constant and the bound convention
# narrative_data declares -- and it must agree with this module's own
# helper everywhere, or "upper" means one thing in the tool and another in
# the report, off the same number.
def _importer_band(value):
    if value is None:
        return None
    ordered = sorted(_imported.items(), key=lambda kv: kv[1][0])
    for name, (low, high) in ordered:
        if low <= float(value) < high:
            return name
    return ordered[-1][0]


for _tenth in range(0, 1001):
    _value = round(_tenth / 10.0, 1)
    assert _importer_band(_value) == pac._elevation_position(_value), (
        f"an importer reading only the bands disagrees with _elevation_position() at {_value}: "
        f"{_importer_band(_value)!r} vs {pac._elevation_position(_value)!r}"
    )
assert _importer_band(None) is None

# It also SHIPS, in the one place a consumer already looks to learn how to
# read a value -- so a panel that never imports Python still gets the same
# cuts, and can check a word against the number it came from.
_scales = flat_result["narrative_data"]["scales"]
assert "elevation_position" in _scales, (
    "the bands must ship in narrative_data['scales'] -- a word with no published cuts cannot be "
    "checked against the percentile beside it"
)
_el_scale = _scales["elevation_position"]
assert _el_scale["bands"] == _imported
assert _el_scale["range"] == [0.0, 100.0]
assert _el_scale["band_bounds"] == "lower_inclusive_upper_exclusive_last_band_inclusive"
assert "elevation_percentile_of_parcel" in _el_scale["applies_to"]
assert "elevation_position" in _el_scale["applies_to"]
# NOT folded into the 0-100 higher-is-better scale around it: an elevation
# percentile is a position, not a quality, and nothing in this pipeline
# scores ground on being high or low.
assert _scales["direction"] == "higher_is_better"
assert _el_scale["direction"] != "higher_is_better", (
    "declaring an elevation percentile 'higher is better' would tell a narrative that upper ground "
    "outscores lower ground, which this pipeline does not claim"
)
assert json.loads(json.dumps(_el_scale)) == _el_scale, "the published bands must be plain JSON"

print(
    f"3. READABLE BY AN IMPORTER: ELEVATION_POSITION_BANDS is public and importable, a downstream "
    f"classifier written from the constant alone agrees with _elevation_position() on all 1001 "
    f"one-decimal values, and the same bands ship in narrative_data['scales']['elevation_position'] "
    f"(direction={_el_scale['direction']!r}, JSON-clean)."
)


# ======================================================================
# 4. position_in_parcel IS UNCHANGED
# ======================================================================
#
# It is a DIFFERENT FACT: an 8-point compass word for where the patch sits
# on the MAP, from the parcel centroid to the patch centroid, and it
# exists because the map legend labels feature CLASSES rather than
# individual features. It stays as it is, for the report. The panel shows
# elevation position only -- two compass words in one panel ("south
# facing" for aspect, "northeast" for map position) would read as a
# contradiction.

_COMPASS = {"north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"}
_POSITIONS = _COMPASS | {"center"}

assert set(pac._COMPASS_WORDS) == _COMPASS, "the 8-point compass vocabulary must not have moved"

# THE VOCABULARIES ARE DISJOINT, which is what makes the two fields
# unmistakable for each other in one panel.
assert not (set(BANDS) & _POSITIONS), (
    f"an elevation word that is also a map-position word would make the panel ambiguous: "
    f"{set(BANDS) & _POSITIONS}"
)

# Recomputed independently from the scored patches, against the same
# helper and the same drawn geometry the block reads -- so a rewiring that
# quietly sourced position_in_parcel from somewhere else fails here.
_pos_dem = _dem(np.full((40, 40), 300.0, dtype=np.float32))
_pos_boundary_utm, _pos_coords = _boundary(_pos_dem)
_pos_result = _run(_pos_dem, _pos_coords)
_pos_by_id = {int(p["id"]): p for p in _pos_result["scored_patches"]}

assert _pos_result["narrative_data"]["patches"], "expected patches to check position_in_parcel on"
for _p in _pos_result["narrative_data"]["patches"]:
    assert _p["position_in_parcel"] in _POSITIONS, (
        f"position_in_parcel must still be an 8-point compass word or 'center', got "
        f"{_p['position_in_parcel']!r}"
    )
    _expected = pac._position_in_parcel(
        _pos_by_id[int(_p["id"])]["render_fill_polygon_utm"], _pos_boundary_utm
    )
    assert _p["position_in_parcel"] == _expected, (
        f"patch {_p['id']}'s position_in_parcel must still be the bearing from the parcel centroid to "
        f"its DRAWN footprint's centroid: {_p['position_in_parcel']!r} != {_expected!r}"
    )
    # And it is emitted BESIDE the elevation fields, not replaced by them.
    for _key in ("position_in_parcel", "elevation_percentile_of_parcel", "elevation_position"):
        assert _key in _p, f"{_key} must be present on every patch entry"

print(
    f"4. position_in_parcel UNCHANGED: still an 8-point map bearing or 'center' "
    f"({', '.join(sorted({p['position_in_parcel'] for p in _pos_result['narrative_data']['patches']}))}), "
    f"recomputed identically from each patch's own drawn footprint, and its vocabulary is disjoint from "
    f"the elevation words ({', '.join(sorted(BANDS))})."
)


# ======================================================================
# 5. END TO END: THREE BENCHES, LOW / MIDDLE / HIGH
# ======================================================================
#
# Three flat benches at 400 m, 450 m and 500 m, the steep risers between
# them excluded by canopy so each bench clusters as its own patch. The
# parcel's elevation range is 400-500, so the benches sit at the bottom,
# the middle and the top of it and must take the first, middle and last
# band in elevation order. The percentile itself is UNCHANGED and still
# emitted beside the word -- the words are an addition, not a replacement.

_rows, _cols = 60, 30
_bench_array = np.empty((_rows, _cols), dtype=np.float32)
for _r in range(_rows):
    if _r <= 17:
        _e = 500.0          # high bench -- northern third of the grid
    elif _r <= 23:
        _e = 500.0 - (_r - 17) * (50.0 / 6.0)   # riser, canopy-excluded
    elif _r <= 35:
        _e = 450.0          # middle bench
    elif _r <= 41:
        _e = 450.0 - (_r - 35) * (50.0 / 6.0)   # riser, canopy-excluded
    else:
        _e = 400.0          # low bench -- southern third
    _bench_array[_r, :] = _e

_bench_dem = _dem(_bench_array, origin_y=4501500.0)
_bench_boundary_utm, _bench_coords = _boundary(_bench_dem)

# Canopy takes the risers AND one row of bench either side of each, so no
# surviving cell borders a riser and every patch is flat all the way
# through -- the benches' own mean elevations are then exactly 500/450/400.
_bench_canopy = np.zeros((_rows, _cols), dtype=bool)
_bench_canopy[16:25, :] = True
_bench_canopy[34:43, :] = True

_bench_result = _run(_bench_dem, _bench_coords, canopy_mask=_bench_canopy)
_bench_patches = _bench_result["narrative_data"]["patches"]
assert len(_bench_patches) == 3, (
    f"expected exactly 3 benches to survive as 3 patches, got {len(_bench_patches)}"
)

_by_elevation = sorted(_bench_patches, key=lambda p: p["elevation_percentile_of_parcel"])
_low, _mid, _high = _by_elevation

# The percentiles themselves: unchanged in meaning, and exactly what the
# fixture's geometry says they are.
assert _low["elevation_percentile_of_parcel"] == 0.0, _low["elevation_percentile_of_parcel"]
assert _mid["elevation_percentile_of_parcel"] == 50.0, _mid["elevation_percentile_of_parcel"]
assert _high["elevation_percentile_of_parcel"] == 100.0, _high["elevation_percentile_of_parcel"]

assert _low["elevation_position"] == ORDERED[0][0], (
    f"the parcel's lowest bench must take the first band {ORDERED[0][0]!r}, got "
    f"{_low['elevation_position']!r}"
)
assert _high["elevation_position"] == ORDERED[-1][0], (
    f"the parcel's highest bench must take the last band {ORDERED[-1][0]!r}, got "
    f"{_high['elevation_position']!r}"
)
assert _mid["elevation_position"] not in (ORDERED[0][0], ORDERED[-1][0]), (
    f"the middle bench must not read as either extreme, got {_mid['elevation_position']!r}"
)
assert len({p["elevation_position"] for p in _bench_patches}) == 3, (
    "three benches at the bottom, middle and top of the parcel's range must produce three DIFFERENT "
    f"words: {[p['elevation_position'] for p in _bench_patches]}"
)

# EVERY patch's word agrees with its own percentile, banded independently.
for _p in _bench_patches:
    assert _p["elevation_position"] == _importer_band(_p["elevation_percentile_of_parcel"]), (
        f"patch {_p['id']} reports {_p['elevation_position']!r} at percentile "
        f"{_p['elevation_percentile_of_parcel']}, which the bands say is "
        f"{_importer_band(_p['elevation_percentile_of_parcel'])!r}"
    )

# The block still survives a plain JSON round trip with the new field on it.
_bench_json = json.dumps(_bench_result["narrative_data"])
assert json.loads(_bench_json) == _bench_result["narrative_data"]

print(
    "5. END TO END (three benches): "
    + "; ".join(
        f"{p['elevation_percentile_of_parcel']} -> {p['elevation_position']!r} "
        f"(map position {p['position_in_parcel']!r})"
        for p in _by_elevation
    )
    + f". narrative_data still json.dumps()-clean ({len(_bench_json)} chars)."
)

print("\nALL ELEVATION-POSITION CHECKS PASSED")
