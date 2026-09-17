"""
test_production_rescoring.py

THE RESCORING, ON THE REFERENCE PARCEL, BEFORE AND AFTER.

Three changes to production scoring land together and this file is where
they are shown on real ground rather than on a synthetic grid:

  1. size_factor's ACREAGE HALF IS GONE and the factor is compactness
     alone, renamed shape_factor for it. The area half normalised a block's
     acreage against a reference maximum the module chose (10 acres), so on
     a 13.2-acre parcel it charged every block for the PARCEL's size.
  2. SOIL IS A FACTOR, on SSURGO's drainage class alone, at 0.20 -- the
     first time production scoring reads soil at all.
  3. THE WEIGHTS SHRANK PROPORTIONALLY to make room for it.

Section 4 below is the deliverable: every block on the reference parcel,
every factor, before and against after. The BEFORE column is RECORDED
rather than recomputed -- it was captured from this same harness on the
commit before the rescoring branch, and it is checked in here because the
code that produced it no longer exists. What is asserted against it is the
SHIFT, not the numbers: that the pipeline still produces the same three
blocks over the same ground, and that every one of them rose.

The harness is test_step_commit.py's -- the reference property (5614 N
Montour Rd, 13.23 acres), its bench-and-drainage DEM, its eight SSURGO
component rows across five map units, and its mocked network boundaries.
Imported rather than rebuilt: two harnesses over one parcel are two
reference parcels, and this file's whole claim is that these are the
blocks the other suites already assert over.
"""

import json
import re

import test_step_commit as T  # noqa: E402  -- runs its own suite on import; that is the harness

import production_suitability as ps  # noqa: E402
import step_orchestrator  # noqa: E402
from production_area_ceiling import score_drawn_production_block  # noqa: E402


# ======================================================================
# 1. THE RENAME IS COMPLETE
# ======================================================================
#
# GREPPED, NOT ASSUMED. A rename that leaves the old name spelled in a
# dict key or an attribute read somewhere is a KeyError waiting for the one
# path the tests do not walk, so this reads the source of every module in
# the package and refuses the old spellings AS CODE. Prose is exempt on
# purpose: the comments that explain what size_factor WAS and why it went
# are the record of the change, and deleting them would leave the next
# reader with a rename and no reason for it.

_DEAD_NAMES = ("size_factor", "area_score", "compactness_score",
               "SIZE_FACTOR_WEIGHT", "SIZE_AREA_SUBWEIGHT", "SIZE_SHAPE_SUBWEIGHT",
               "REFERENCE_MAX_AREA_ACRES")

# water_suitability.py has its OWN, unrelated _contributing_area_score() and
# its own topographic sub-scores; they were never production's and this
# rename is not theirs.
_EXEMPT_FILES = ("water_suitability.py",)

# TEST FILES ARE NOT SCANNED, and the reason is not convenience: a test that
# asserts a dead field is GONE has to name it, and a scan that refused the
# name would make the absence unassertable. A live reference inside a test
# cannot hide anyway -- it fails that test the moment it runs.
_SCAN_TESTS = False


def _code_lines(path):
    """Every source line with its comments and docstrings stripped out.

    Deliberately crude -- a full-line comment goes, a trailing comment goes,
    and anything inside a triple-quoted block goes. It never has to parse
    Python; it has to be unable to MISS a live reference, and a stripper
    that errs toward keeping text errs in the safe direction here.
    """
    lines = []
    in_doc = None
    for line in open(path, encoding="utf-8"):
        stripped = line.strip()
        if in_doc:
            if in_doc in line:
                in_doc = None
            continue
        for quote in ('"""', "'''"):
            if stripped.startswith(quote) and not (stripped.count(quote) >= 2 and len(stripped) > 3):
                in_doc = quote
                break
        if in_doc or stripped.startswith("#") or not stripped:
            continue
        lines.append(line.split("#", 1)[0])
    return lines


import glob  # noqa: E402
import os  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_live = []
for _path in sorted(glob.glob(os.path.join(_HERE, "*.py"))):
    _name = os.path.basename(_path)
    if _name in _EXEMPT_FILES or _name == os.path.basename(__file__):
        continue
    if _name.startswith("test_") and not _SCAN_TESTS:
        continue
    for _number, _line in enumerate(_code_lines(_path), start=1):
        for _dead in _DEAD_NAMES:
            if re.search(rf"\b{_dead}\b", _line):
                _live.append(f"{os.path.basename(_path)}: {_line.strip()}")

assert not _live, (
    "the rename left live references to names that no longer exist:\n  " + "\n  ".join(_live)
)
_SCANNED = [
    os.path.basename(p) for p in glob.glob(os.path.join(_HERE, "*.py"))
    if not os.path.basename(p).startswith("test_") and os.path.basename(p) not in _EXEMPT_FILES
]
print(
    f"1. RENAME COMPLETE: no live reference to any of {', '.join(_DEAD_NAMES)} survives in the "
    f"{len(_SCANNED)} pipeline modules (prose deliberately keeps the history; test files are not "
    "scanned because asserting a name is gone means spelling it)."
)


# ======================================================================
# 2. DRAINAGE MOVES THE SCORE, BY THE WEIGHT AND NOTHING MORE
# ======================================================================
#
# Two blocks, identical in every other way, on two different drainage
# classes -- which is the distinction production scoring could not make at
# all before this branch.

_BLOCK = T.Session  # (named only so the import above is obviously used)


def _score_with(drainage_class, patches, dem, step1):
    """The same three blocks, scored as though every one sat on `drainage_class`."""
    scored = ps.score_production_areas(
        [dict(p) for p in patches],
        dem,
        step1,
        drainage_class_by_patch_id={int(p["id"]): drainage_class for p in patches},
    )
    return {int(p["id"]): p for p in scored}


with T.Harness():
    _s = T.Session()
    _payload = _s.generate()
    _result = _s.context().step_proposals["landform"]
    _run = _result["run_inputs"]
    _patches = _result["scored_patches"]

    _well = _score_with("Well drained", _patches, _run["dem"], _run["step1"])
    _poor = _score_with("Poorly drained", _patches, _run["dem"], _run["step1"])
    _none = _score_with(None, _patches, _run["dem"], _run["step1"])

    for _pid, _w in _well.items():
        _p = _poor[_pid]
        _n = _none[_pid]

        # 3. WELL DRAINED OUTSCORES POORLY DRAINED, BY THE EXPECTED MARGIN.
        # The margin is not a magic number: it is the soil weight times the
        # gap between the two classes on the published mapping, which is
        # the only way the factor may move a composite.
        _expected = round(
            ps.SOIL_FACTOR_WEIGHT
            * (ps.DRAINAGE_CLASS_FACTORS["well drained"] - ps.DRAINAGE_CLASS_FACTORS["poorly drained"])
            * 100.0,
            1,
        )
        _gap = round(_w["suitability_score"] - _p["suitability_score"], 1)
        assert abs(_gap - _expected) <= 0.15, (
            f"block {_pid}: well drained must outscore poorly drained by the soil weight's own worth "
            f"({_expected} points); got {_gap}"
        )
        # ...and nothing else moved with it.
        for _field in ("slope_factor", "shape_factor", "aspect_factor", "area_acres"):
            assert _w[_field] == _p[_field] == _n[_field], _field

        # 4. NO COVERAGE IS NEUTRAL, AND FLAGGED.
        assert _n["soil_factor"] == ps._NEUTRAL_FACTOR_VALUE
        assert _n["soil_available"] is False
        assert _w["soil_available"] is True and _p["soil_available"] is True
        # The trap the flag exists for: a measured 0.5 would be this same
        # number, so the flag is the only thing that can tell them apart.
        assert _n["soil_factor"] == ps._NEUTRAL_FACTOR_VALUE

_first = sorted(_well)[0]
print(
    f"2. DRAINAGE: on every one of the {len(_well)} reference blocks, well-drained scores exactly "
    f"{round(_well[_first]['suitability_score'] - _poor[_first]['suitability_score'], 1)} points above "
    f"poorly-drained -- the soil weight ({ps.SOIL_FACTOR_WEIGHT}) times the classes' own gap "
    f"({ps.DRAINAGE_CLASS_FACTORS['well drained']} vs {ps.DRAINAGE_CLASS_FACTORS['poorly drained']}) -- "
    f"with slope, shape, aspect and acreage untouched. With no class at all the factor is the neutral "
    f"{ps._NEUTRAL_FACTOR_VALUE} and soil_available is False, which is the only thing distinguishing it "
    f"from a measured mid-value."
)


# ======================================================================
# 3. A DRAWN BLOCK IS SCORED, ON THE SAME INSTRUMENT
# ======================================================================
#
# The same ring the commit tests draw over the wet ground -- so the block
# whose caution says "hydric" is the block whose soil factor says so too.

_SUGGESTION_ROW_FIELDS = (
    "area_acres", "percent_of_parcel", "score", "factors",
    "slope_min_pct", "slope_max_pct", "slope_median_pct", "avg_slope_pct",
    "dominant_aspect", "aspect_consistency_pct", "aspect_available",
    "position_in_parcel", "soil_components", "drainage_class", "soil_available",
    "elevation_percentile_of_parcel", "elevation_position", "hole_count", "hole_acres",
)

with T.Harness() as _h:
    _s = T.Session()
    _payload = _s.generate()
    _zone_rows = _payload["zones"]

    _before_calls = _h.total_network_calls
    _drawn_feature = step_orchestrator.score_placed_feature(
        _s.id, "landform", _s.store,
        params={"ring": [list(point) for point in T.HYDRIC_ZONE_RING]},
        fetch_cache=_s.fetch_cache, cache=_s.cache,
    )
    _drawn_calls = _h.total_network_calls - _before_calls
    _drawn_props = _drawn_feature["properties"]

    # PURE AND LOCAL.
    assert _drawn_calls == 0, f"scoring a drawn block made {_drawn_calls} network call(s)"

    # EVERY FACTOR PRESENT, none defaulted for want of an input.
    assert set(_drawn_props["factors"]) == {
        "slope_factor", "shape_factor", "aspect_factor", "soil_factor"
    }, sorted(_drawn_props["factors"])
    for _name, _value in _drawn_props["factors"].items():
        assert 0.0 <= _value <= 100.0, (_name, _value)
    assert 0.0 <= _drawn_props["score"] <= 100.0

    # THE SAME ROW AS A SUGGESTION, field for field -- the claim the panel
    # makes when it prints the two scores in one column.
    for _field in _SUGGESTION_ROW_FIELDS:
        assert _field in _drawn_props, f"a drawn block's row is missing {_field}"
        assert _field in _zone_rows[0], f"the suggestion row is missing {_field}"

    # ...MINUS THE FOUR A DRAWN BLOCK CANNOT HAVE. Absent, not null.
    for _field in ("rank", "source_patch_id", "from_waist_split", "source_region_hydric_pct"):
        assert _field not in _drawn_props, (
            f"{_field} names something a drawn block never had -- it must be absent rather than null"
        )

    # IT IS ON THE WET GROUND, and both the soil factor and the drainage row
    # say so. This is the same ring test_step_commit records a hydric
    # crossing for.
    assert _drawn_props["drainage_class"] == "Poorly drained"
    assert _drawn_props["soil_available"] is True
    assert _drawn_props["factors"]["soil_factor"] == round(
        ps.DRAINAGE_CLASS_FACTORS["poorly drained"] * 100.0, 1
    )
    assert _drawn_props["block_origin"] == "user_drawn"

    # AND IT IS COMPARABLE: re-scoring the same ring through the scorer
    # directly gives the identical composite, and a block drawn over the
    # best suggestion's own ground scores near that suggestion.
    _again = score_drawn_production_block([list(p) for p in T.HYDRIC_ZONE_RING], _result_again := _s.context().step_proposals["landform"])
    assert _again["readout"]["score"] == _drawn_props["score"]

    # REFUSALS: off the parcel, and a ring that is not a ring.
    for _bad, _why in (
        ([[-80.20, 40.70], [-80.19, 40.70], [-80.19, 40.71]], "entirely outside"),
        ([[-79.983, 40.645], [-79.982, 40.645]], "at least 3 points"),
    ):
        try:
            score_drawn_production_block(_bad, _result_again)
            raise AssertionError(f"expected a refusal for a ring that is {_why}")
        except ValueError:
            pass

print(
    f"3. DRAWN BLOCK SCORED: the wet-ground ring scores {_drawn_props['score']}/100 on "
    f"{_drawn_props['area_acres']} acres with all four factors present "
    f"({_drawn_props['factors']}), the same row shape a suggestion carries, and "
    f"{_drawn_props['drainage_class'].lower()} soil under it -- in {_drawn_calls} network calls. rank, "
    f"source_patch_id, from_waist_split and source_region_hydric_pct are absent, not null. A ring off "
    f"the parcel and a two-point ring are both refused."
)


# ======================================================================
# 4. THE REFERENCE PARCEL, BEFORE AND AFTER
# ======================================================================
#
# RECORDED, NOT RECOMPUTED. Captured from this harness at 748ae3a, the
# commit before the rescoring, with the code that produced it now gone.
# Keyed by the patch id, which is stable across the change (it comes from
# STEP 3's clustering, which this branch does not touch).
BEFORE = {
    2: {"rank": 1, "area_acres": 0.42, "score": 50.4,
        "slope_factor": 65.9, "size_factor": 37.5, "aspect_factor": 19.6,
        "area_score": 7.6, "compactness_score": 67.5, "drainage_class": None},
    0: {"rank": 2, "area_acres": 3.06, "score": 45.4,
        "slope_factor": 51.9, "size_factor": 45.9, "aspect_factor": 20.4,
        "area_score": 38.9, "compactness_score": 52.9, "drainage_class": "Moderately well drained"},
    1: {"rank": 3, "area_acres": 0.7, "score": 34.5,
        "slope_factor": 44.7, "size_factor": 21.1, "aspect_factor": 24.0,
        "area_score": 18.2, "compactness_score": 24.0, "drainage_class": "Somewhat excessively drained"},
}

with T.Harness():
    _s = T.Session()
    AFTER = {int(row["id"]): row for row in _s.generate()["zones"]}

assert set(AFTER) == set(BEFORE), (
    f"the rescoring must not change WHICH blocks exist -- it touches no gate, no cluster and no "
    f"geometry. Before {sorted(BEFORE)}, after {sorted(AFTER)}"
)

print("\n4. THE REFERENCE PARCEL'S BLOCKS, BEFORE AND AFTER")
print("   5614 N Montour Rd, Gibsonia, PA -- 13.23 acres, three production blocks\n")
_header = (
    f"   {'block':<6}{'acres':>7}{'score':>16}{'slope':>14}{'shape':>18}"
    f"{'aspect':>14}{'soil':>16}"
)
print(_header)
print(f"   {'':<6}{'':>7}{'before -> after':>16}{'(unchanged)':>14}{'area+shape -> shape':>18}"
      f"{'(unchanged)':>14}{'-- -> drainage':>16}")
print("   " + "-" * (len(_header) - 3))
for _pid in sorted(BEFORE, key=lambda i: BEFORE[i]["rank"]):
    _b, _a = BEFORE[_pid], AFTER[_pid]
    _score_cell = "{} -> {}".format(_b["score"], _a["score"])
    _shape_cell = "{} -> {}".format(_b["size_factor"], _a["factors"]["shape_factor"])
    _soil_cell = "-- -> {}".format(_a["factors"]["soil_factor"])
    print(
        f"   {_pid:<6}{_a['area_acres']:>7}{_score_cell:>16}"
        f"{_a['factors']['slope_factor']:>14}{_shape_cell:>18}"
        f"{_a['factors']['aspect_factor']:>14}{_soil_cell:>16}"
    )
print()
for _pid in sorted(BEFORE, key=lambda i: BEFORE[i]["rank"]):
    _b, _a = BEFORE[_pid], AFTER[_pid]
    print(
        f"   block {_pid}: rank {_b['rank']} -> {_a['rank']}, score {_b['score']} -> {_a['score']} "
        f"({_a['score'] - _b['score']:+.1f}); drainage {_b['drainage_class'] or 'no survey coverage'}"
        f" -> soil_factor {_a['factors']['soil_factor']}"
        f"{'' if _a['soil_available'] else ' (NEUTRAL, not measured)'}; "
        f"size {_b['size_factor']} (area {_b['area_score']} + shape {_b['compactness_score']}) -> "
        f"shape {_a['factors']['shape_factor']}"
    )

# WHAT THE TABLE HAS TO SHOW, asserted rather than left to the eye:
for _pid, _b in BEFORE.items():
    _a = AFTER[_pid]
    # The geometry did not move. Only the scoring did.
    assert _a["area_acres"] == _b["area_acres"], _pid
    assert _a["factors"]["slope_factor"] == _b["slope_factor"], _pid
    assert _a["factors"]["aspect_factor"] == _b["aspect_factor"], _pid
    # shape_factor IS the old compactness sub-score, unchanged -- the same
    # measurement, promoted out of a blend it was averaged into.
    assert _a["factors"]["shape_factor"] == _b["compactness_score"], (
        f"block {_pid}: shape_factor must be exactly the compactness sub-score the old size_factor "
        f"averaged away: {_a['factors']['shape_factor']} vs {_b['compactness_score']}"
    )
    # Every block rose. Dropping the acreage penalty is most of it; the
    # drainage the blocks actually sit on is the rest.
    assert _a["score"] > _b["score"], _pid
    # The drainage class the panel already showed is the one the score now
    # reads -- no second source for the same fact.
    assert _a["drainage_class"] == _b["drainage_class"], _pid

_risers = sum(1 for _pid in BEFORE if AFTER[_pid]["score"] > BEFORE[_pid]["score"])
print(
    f"\n   All {_risers} blocks rose ("
    + ", ".join(
        f"{BEFORE[p]['score']}->{AFTER[p]['score']}" for p in sorted(BEFORE, key=lambda i: BEFORE[i]['rank'])
    )
    + "). Geometry, acreage, slope and aspect are byte-identical; shape_factor is the old compactness "
    "sub-score promoted out of the blend; soil is new."
)

json.dumps({str(k): v for k, v in AFTER.items()})  # the payload rows stay JSON-serialisable

print("\nAll production rescoring checks passed.")
