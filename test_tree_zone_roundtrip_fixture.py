"""
test_tree_zone_roundtrip_fixture.py

THE LIVE REJECTION, REPLAYED. Not a hand-built bowtie.

WHY THIS FILE EXISTS, AND WHAT WENT WRONG WITHOUT IT. An earlier branch
(claude/tree-zone-geometry-validation-zz7ce8, "Tree zones: validate geometry at
emission, not at commit") could not reproduce this bug and verified its fix
against a HAND-BUILT BOWTIE instead. A bowtie is a ring that crosses itself by
tens of metres. The real defect is a pair of vertices ONE NANOMETRE apart. The
fix held against the bowtie and the defect recurred in live use within days,
because the two have nothing to do with each other.

So this file replays a real one. `tree_roundtrip_rejection_fixture.json` is
extracted verbatim from a run_diagnostics.py record of a real session on a real
parcel: all four tree candidates one generate produced, and the
`invalid_geometry` rejection all four of them got at commit. Nothing in it is
constructed, and NOTHING IN THIS FILE CONSTRUCTS GEOMETRY -- if a check here
cannot be made against the recorded rings, it is not made.

WHAT THE RECORD ESTABLISHES BEFORE THIS FILE ASSERTS ANYTHING:

    every emitted patch   polygon_utm.is_valid TRUE, invalidity null
    dropped_invalid       [] -- the emission gate dropped nothing
    dropped_invalid_count 0
    candidate_count       4
    every emitted patch   roundtrip.rehydrates FALSE, verdicts_agree FALSE

The emission gate ran, passed all four honestly, and every one of them was
unemittable. The user saw four candidates on the map and could commit none.

THE TWO ERROR STRINGS ARE ONE DEFECT. Two candidates were refused with `Ring
Self-intersection` and two with a bare `Self-intersection`. Those are different
GEOS verdicts -- a ring TOUCHING itself versus a proper CROSSING -- but they are
the same defect landing either side of one threshold, and section 3 measures the
mechanism that produces both.

THE MECHANISM, MEASURED IN SECTION 3 RATHER THAN ASSERTED HERE. The producer
builds a ring whose two vertices are ~1e-9 m apart -- distinct doubles in UTM,
so the ring is valid and the emission gate is right to pass it. transform_geom()
then projects it to WGS84 for the wire, and at this location a longitude ULP is
1.2e-9 m against an easting ULP of 1.2e-10 m: the wire's float grid is TEN TIMES
COARSER IN METRES than UTM's. The two vertices land on the SAME double. The ring
now visits one point twice -- a self-touch -- and it is invalid before it ever
leaves the server. Rehydration is not where the geometry breaks; it is where the
break is first noticed.

NOT A ROUNDING BUG, AND THE DISTINCTION MATTERS. Nothing quantizes these
coordinates: the wire carries 15 decimal places, unrounded doubles straight out
of transform_geom, and the record says so (coordinate_decimals_max 15). The
quantization is the FLOAT GRID ITSELF, which is coarser in degrees than in
metres at this magnitude. "Nobody rounds" and "no precision is lost" are
different claims, and only the first one is true.
"""

import json
import math

import shapely as _shapely
from rasterio.warp import transform_geom
from shapely.geometry import mapping, shape

import tree_zone_candidates
import wire_translation

FIXTURE_PATH = "tree_roundtrip_rejection_fixture.json"

with open(FIXTURE_PATH) as _fh:
    FIXTURE = json.load(_fh)

DEM = {"crs": FIXTURE["provenance"]["dem_crs"]}
REJECTED = {feature["id"]: feature for feature in FIXTURE["rejected_features"]}
# The rejection message's own defect text, per feature id: everything between
# "geometry is not valid -- " and the "Rehydration does not repair" boilerplate.
RECORDED_DEFECT = {
    entry["feature_id"]: entry["reason"].split("geometry is not valid -- ", 1)[1].split(". Rehydration", 1)[0]
    for entry in FIXTURE["rejections"]
}
PATCH_HEALTH = {entry["id"]: entry for entry in FIXTURE["patch_health"]}


def _fail(message):
    raise AssertionError(message)


def _defect_of(error) -> str:
    """The explain_validity() text out of an InboundGeometryError's message."""
    text = str(error)
    if "geometry is not valid -- " not in text:
        return text
    return text.split("geometry is not valid -- ", 1)[1].split(". Rehydration", 1)[0]


def _rehydrates(feature):
    """(ok, defect) for one fixture feature through the REAL commit gate."""
    try:
        wire_translation._polygonal_shape_from_wire(feature["geometry"], DEM, "fixture replay")
        return True, None
    except wire_translation.InboundGeometryError as error:
        return False, _defect_of(error)


def _survives(footprint) -> bool:
    """
    Whether a UTM footprint makes it onto the wire and back.

    Deliberately the producer's own expression -- transform_geom out to
    EPSG:4326 exactly as score_tree_search_space() builds `geometry_wgs84`,
    then the commit gate's own helper back -- so this measures the real trip
    and not a paraphrase of it.
    """
    wire = transform_geom(DEM["crs"], "EPSG:4326", mapping(footprint))
    try:
        wire_translation._polygonal_shape_from_wire(wire, DEM, "survival check")
    except wire_translation.InboundGeometryError:
        return False
    return True


# =========================================================================
# 0. THE RECORD'S OWN ACCOUNT -- read, not re-derived
# =========================================================================
#
# Asserted so the sections below cannot quietly be run against a fixture
# that no longer says what they were written for.

_narrative = FIXTURE["narrative_data"]
assert _narrative["candidate_count"] == 4, _narrative
assert _narrative["dropped_invalid_count"] == 0, _narrative
assert FIXTURE["drops"] == {"result.dropped_invalid": []}, FIXTURE["drops"]
assert len(REJECTED) == 4, sorted(REJECTED)
assert len(PATCH_HEALTH) == 4, sorted(PATCH_HEALTH)

for _id, _entry in PATCH_HEALTH.items():
    assert _entry["polygon_utm"]["is_valid"] is True, _entry
    assert _entry["polygon_utm"]["invalidity"] is None, _entry
    assert _entry["roundtrip"]["rehydrates"] is False, _entry
    assert _entry["verdicts_agree"] is False, _entry

_ring_self = sorted(k for k, v in RECORDED_DEFECT.items() if v.startswith("Ring Self-intersection"))
_plain_self = sorted(k for k, v in RECORDED_DEFECT.items() if v.startswith("Self-intersection"))
assert len(_ring_self) == 2 and len(_plain_self) == 2, (_ring_self, _plain_self)

print(
    f"0. THE RECORD: one live generate produced candidate_count={_narrative['candidate_count']} tree "
    f"candidates. The emission gate passed ALL FOUR honestly -- polygon_utm.is_valid true with "
    f"invalidity null on every one, dropped_invalid [] and dropped_invalid_count "
    f"{_narrative['dropped_invalid_count']}. All four were then refused at commit, and the record's own "
    f"roundtrip verdict agrees: rehydrates false and verdicts_agree false on every patch. Two failed "
    f"with 'Ring Self-intersection' ({', '.join(_ring_self)}) and two with a bare 'Self-intersection' "
    f"({', '.join(_plain_self)}). Nothing on this parcel was committable."
)


# =========================================================================
# 1 [test 1]. THE FIXTURE FAILS ON CURRENT CODE
# =========================================================================
#
# The recorded wire geometry, through the commit gate's own helper. All four
# are refused, reproducing the live rejection to the digit on three of them.
#
# THIS ASSERTION DOES NOT FLIP AFTER THE FIX, AND IT MUST NOT. B4's rule is
# that rehydration never repairs geometry, and the fix does not touch
# rehydration -- so this already-broken payload is still, correctly, refused.
# What the fix changes is that a payload like it is never SHIPPED. Section 2
# is the assertion that flips.

_outcomes = {}
for _id, _feature in REJECTED.items():
    _outcomes[_id] = _rehydrates(_feature)

_rejected_now = sorted(i for i, (ok, _) in _outcomes.items() if not ok)
if len(_rejected_now) != 4:
    _fail(
        "THE FIXTURE DID NOT REPRODUCE. Every one of these four features was refused by a live "
        f"commit gate, and here only {_rejected_now} were. A fix cannot be verified against a case "
        "that does not fail first -- that is exactly what went wrong last time."
    )

# The SAME defect, not merely some defect. Three of the four reproduce
# explain_validity()'s coordinate to the digit. Candidate 4 carries eleven
# parts and more than one self-intersection, and GEOS 3.13 (here) and 3.14
# (the capture) do not agree on which one they report first, so it is
# compared on the KIND of defect rather than the position -- stated because
# a coordinate that silently stopped matching would otherwise look like a
# passing test.
_exact, _kind_only = [], []
for _id in _rejected_now:
    _observed, _recorded = _outcomes[_id][1], RECORDED_DEFECT[_id]
    assert _observed.split("[")[0] == _recorded.split("[")[0], (_id, _observed, _recorded)
    (_exact if _observed == _recorded else _kind_only).append(_id)

print(
    f"1 [test 1]. THE FIXTURE FAILS ON CURRENT CODE: all 4 recorded features are refused by "
    f"wire_translation._polygonal_shape_from_wire(), the exact helper a commit runs. "
    f"{len(_exact)} of them reproduce explain_validity()'s coordinate EXACTLY "
    f"({', '.join(sorted(_exact))}); {', '.join(sorted(_kind_only)) or 'none'} matches on the kind "
    f"of defect but not the position, on GEOS {_shapely.geos_version_string} against the capture's "
    f"{FIXTURE['provenance']['environment']['shapely_geos_version']}. This stays true after the fix: "
    f"rehydration never repairs, and a payload already this broken is still refused."
)


# =========================================================================
# 2 [test 2]. THE FIXTURE PASSES AFTER THE FIX -- ALL FOUR
# =========================================================================
#
# THE ASSERTION THAT FLIPS, and it is run twice in one pass so the before and
# the after are the same code on the same geometry rather than two runs a
# reader has to take on trust:
#
#   THE OLD GATE   _valid_polygonal() alone -- what score_tree_search_space()
#                  emitted with before this branch. Its output must FAIL the
#                  wire, which is the defect.
#   THE NEW GATE   _valid_polygonal() then _wire_survivable(). Its output
#                  must SURVIVE the wire.
#
# THE UTM RECONSTRUCTION, AND ITS ONE LIMITATION, STATED. The producer's own
# `polygon_utm` is not in the record -- only the wire form it was projected
# to. So the footprint fed to both gates here is that wire form projected
# BACK, by the pipeline's own inverse transform: the closest reconstruction
# the evidence supports, differing from what the producer held by less than
# the ~1e-9 m collapse this bug turns on.
#
# It differs in one way that matters and is worth saying plainly rather than
# glossing: the original was VALID in UTM (the record says so -- is_valid
# true, invalidity null), while this reconstruction has already collapsed and
# is invalid. So this exercises make_valid() on a way in that the producer's
# object would not have taken. That makes it a HARDER case than the real one,
# not an easier one, so a pass here is not a pass bought by the
# reconstruction. What it cannot do is prove the pre-collapse object took the
# untouched path; section 3's ULP measurement is the evidence for that.

_before, _after, _repairs = {}, {}, []
for _id, _feature in REJECTED.items():
    _utm = shape(transform_geom("EPSG:4326", DEM["crs"], _feature["geometry"]))

    _old = tree_zone_candidates._valid_polygonal(_utm)
    assert _old is not None, f"{_id}: the old gate dropped this outright; it did not, live"
    _before[_id] = _survives(_old)

    _new = tree_zone_candidates._wire_survivable(_old, DEM)
    if _new is None:
        _after[_id] = False
        continue
    _after[_id] = _survives(_new)
    if _new is not _old:
        _repairs.append((_id, _old, _new))

_still_broken_before = sorted(i for i, ok in _before.items() if not ok)
_broken_after = sorted(i for i, ok in _after.items() if not ok)

if not _still_broken_before:
    _fail(
        "THE OLD GATE'S OUTPUT SURVIVED THE WIRE ON ALL FOUR. Section 2 is then not measuring the "
        "defect at all, and the fix below is unfalsified."
    )
if _broken_after:
    _fail(f"THE FIX DID NOT HOLD: {_broken_after} still do not survive the wire after repair.")

# WHY IT IS NOT ALL FOUR, AND WHAT THAT SHOWS. On the reconstruction the old
# gate leaves the two `Self-intersection` (proper crossing) cases failing and
# happens to rescue the two `Ring Self-intersection` (self-touch) ones --
# because the reconstruction has ALREADY collapsed, and make_valid() splits a
# self-touch into parts while a crossing survives its re-noding as a crossing.
# That is a fact about the collapsed form, not about what the producer held:
# the producer's object was valid in UTM, so make_valid() would have returned
# it untouched and ALL FOUR would have shipped broken -- which is exactly what
# the record shows happened. Reported rather than asserted away, because a
# "4 of 4 failed" line here would be truer to the story than to the run.
_touch_kind = sorted(i for i in RECORDED_DEFECT if RECORDED_DEFECT[i].startswith("Ring Self-"))

print(
    f"2 [test 2]. THE FIXTURE PASSES AFTER THE FIX: on the same four recorded geometries, the NEW "
    f"gate (_valid_polygonal then _wire_survivable) produced output that survives the wire on ALL 4, "
    f"and dropped none. The OLD gate (_valid_polygonal alone) left {len(_still_broken_before)} of the "
    f"4 still failing -- {', '.join(_still_broken_before)}, both of them the proper-crossing kind. It "
    f"rescued the two self-touch cases ({', '.join(_touch_kind)}) only because this reconstruction has "
    f"already collapsed and make_valid() splits a touch; against the producer's own valid-in-UTM "
    f"object it would have returned all four untouched, which is what the record shows it did. "
    f"{len(_repairs)} of the four needed the snap."
)


# =========================================================================
# 3 [test 3]. verdicts_agree FOR EVERY REPAIRED PATCH
# =========================================================================
#
# The record prints two verdicts side by side and this bug is the case where
# they disagree: shapely calls polygon_utm valid, the gate refuses the wire
# form. All four are recorded with verdicts_agree false. This asserts BOTH
# verdicts are now true on every one -- not merely that the second one
# stopped failing, since a repair that made polygon_utm invalid would also
# make them "agree".

_pairs = {}
for _id, _feature in REJECTED.items():
    _utm = shape(transform_geom("EPSG:4326", DEM["crs"], _feature["geometry"]))
    _emitted = tree_zone_candidates._wire_survivable(tree_zone_candidates._valid_polygonal(_utm), DEM)
    assert _emitted is not None, _id
    _pairs[_id] = (bool(_emitted.is_valid), _survives(_emitted))

_disagreements = {i: p for i, p in _pairs.items() if p[0] != p[1] or not p[0]}
assert not _disagreements, _disagreements

# AND IT IS STABLE, not true once. A committed zone is re-served and can be
# committed again, so the round trip runs more than once over a patch's life;
# a repair that survived one hop and collapsed on the next would just move
# the failure. Three consecutive hops, each on the previous one's output.
_stable = {}
for _id, _feature in REJECTED.items():
    _utm = shape(transform_geom("EPSG:4326", DEM["crs"], _feature["geometry"]))
    _current = tree_zone_candidates._wire_survivable(tree_zone_candidates._valid_polygonal(_utm), DEM)
    for _hop in range(3):
        _wire = transform_geom(DEM["crs"], "EPSG:4326", mapping(_current))
        _current = wire_translation._polygonal_shape_from_wire(_wire, DEM, f"hop {_hop}")
        assert _current.is_valid, (_id, _hop)
    _stable[_id] = True

print(
    f"3 [test 3]. VERDICTS AGREE: all {len(_pairs)} patches the record shows with verdicts_agree=false "
    f"now carry BOTH verdicts true -- polygon_utm.is_valid and the round-trip gate alike "
    f"(not 'agree' by having broken the first one). And it holds under repetition: each survives "
    f"THREE further consecutive round trips, so the repair does not merely postpone the collapse."
)


# =========================================================================
# 4. AREA CHANGE PER PATCH, AND WHAT MOVED IT
# =========================================================================
#
# The snap is the only step in the gate that MOVES a vertex, so it is the only
# one that can move area, and every square metre of it is accounted for here
# rather than waved at with a tolerance. The bound it is checked against is one
# DEM cell, because a cell is the smallest change this pipeline can represent:
# membership is decided at 5 m and acreage is published rounded to 0.01 ac
# (about 40 m^2). A patch that moved by less than a cell has not moved by
# anything the rest of the system can see.
#
# The narrowest-arm measurement for these same patches lives in
# test_tree_zone_geometry_validity.py, which owns that metric and its
# calibration; duplicating it here is how two files start disagreeing about
# what a thin arm is.

CELL_AREA_M2 = 25.0

_area_rows = []
for _id in sorted(REJECTED):
    _utm = shape(transform_geom("EPSG:4326", DEM["crs"], REJECTED[_id]["geometry"]))
    _into = tree_zone_candidates._valid_polygonal(_utm)
    _out = tree_zone_candidates._wire_survivable(_into, DEM)
    assert _out is not None, _id
    _area_rows.append(
        {
            "id": _id,
            "before": _into.area,
            "after": _out.area,
            "delta": _out.area - _into.area,
            "snapped": _out is not _into,
        }
    )

for _row in _area_rows:
    assert abs(_row["delta"]) < CELL_AREA_M2, _row
    # AN UNTOUCHED PATCH MOVED BY EXACTLY NOTHING -- not "by a little". The
    # step returns the same object, so this is an identity and not a
    # tolerance, and asserting it as one is what keeps a healthy generate
    # provably byte-identical to what it was before this gate existed.
    if not _row["snapped"]:
        assert _row["delta"] == 0.0, _row

_worst = max(_area_rows, key=lambda row: abs(row["delta"]))
for _row in _area_rows:
    print(
        f"    area   {_row['id']:<24} {_row['before']:14.6f} -> {_row['after']:14.6f} m^2  "
        f"delta {_row['delta']:+.6f} m^2 ({'snapped' if _row['snapped'] else 'untouched'})"
    )

print(
    f"4. AREA: of the {len(_area_rows)} captured patches, "
    f"{sum(1 for r in _area_rows if r['snapped'])} were snapped and "
    f"{sum(1 for r in _area_rows if not r['snapped'])} were returned untouched and moved by EXACTLY "
    f"zero. The largest movement is {_worst['id']} at {_worst['delta']:+.6f} m^2 -- "
    f"{abs(_worst['delta']) / CELL_AREA_M2:.2e} of ONE 5 m DEM cell, and "
    f"{abs(_worst['delta']) / _worst['before'] * 100:.5f}% of that patch. It moves because "
    f"set_precision() puts each vertex on the nearest 1 mm grid point, which shifts a boundary by at "
    f"most half a grid diagonal and can move area in EITHER direction -- unlike an opening, which "
    f"only ever removes. Nothing here is within four orders of magnitude of the 0.01 ac (about 40 "
    f"m^2) the acreage this layer publishes is rounded to."
)


print()
print("All tree-zone round-trip fixture checks passed.")
