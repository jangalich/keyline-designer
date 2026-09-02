"""
test_embankment_compartments.py

THE COMPARTMENT CHANGE's own test file: seed-based valley compartments
replace extraction on the EMBANKMENT path (water_survey_areas.py's
compartment section). Hand-derived fixtures throughout -- every asserted
width, offset, cell count and acreage is computed by hand in the fixture
comments before the code asserts it. Run as:

    python test_embankment_compartments.py

Sections (the design's numbered test items in brackets):
  1  [1]   THE PINCHED VALLEY -- a wide bowl narrowing to a waist then
           widening: crest-to-crest widths at three stations asserted
           against hand-derived values; the embankment cell lands at the
           hand-known waist row; the false-crest prominence guard walks
           past a sub-prominence knoll.
  2  [2]   TERMINAL ACCEPTANCE (the accepted-not-refused correction) --
           a width minimum at the boundary / a road strip / the 100 m
           walk bound is ACCEPTED with its pinch_at_* flag and the
           still-narrowing disclosure; the rewidened variant keeps its
           interior pinch; monotone widening is the sole failure,
           no_constriction. Plus the AST-level retirement of the three
           refusal codes and a shared-terminal-pinch dedupe.
  3  [3]   COMPARTMENT ASSEMBLY -- transect crest points at hand-computed
           offsets; the watershed-band ridge connection matches the
           fixture's hand-enumerated divides (the staircase); compartment
           area within tolerance; boundary and road clips each flagged.
  4  [4]   DEDUPE -- two seeds, one pinch -> one compartment, higher
           blend kept, loser reason-coded; compartment-overlap dedupe on
           hand-built polygons.
  5  [5]   THE FLOOR on compartment acres -- dropped visible with BOTH
           the reason and the acreage.
  6  [6]   THE REPORTING HONESTY SPLIT -- seed score vs compartment
           criteria means asserted distinct where they differ; the
           narrative and wire sentences carry both.
  7  [7]   EXCAVATED REGRESSION -- byte-identical output on a roadless
           fixture (road-unchecked vs checked-clean vs road-far-away);
           road clip + truncated_by_road flag on a road fixture.
  8  [9]   EXPORT VALIDATION -- the new layers (embankment_seed /
           embankment_seed_failed / embankment_pinch / embankment_baseline
           / embankment_transect), linkage, stored wire forms.

(The design's item [8] -- the full-context synthetic through
build_pipeline_context() with a compartment as rank-1 -- lives in
test_pipeline_context.py, whose fixture valley now carries a throat for
exactly that purpose; the orchestrated payload path is
test_water_step.py.)
"""

import json

import numpy as np
from rasterio.warp import transform_geom
from shapely.geometry import Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely import contains_xy

import water_survey_areas as wsa
from feature_schema import validate_feature_collection
from keypoint_detection import build_upstream_map
from raster_grid import SQUARE_METERS_PER_ACRE, pixel_center_xy
from valley_delineation import compute_flow_accumulation, compute_flow_direction, fill_depressions
from water_survey_areas import (
    EMBANKMENT_WEIGHTS,
    SURVEY_TYPE_EMBANKMENT,
    SURVEY_TYPE_EXCAVATED,
    build_embankment_compartment,
    compute_water_survey_areas,
    dedupe_compartments_by_overlap,
    duplicate_of_zone_reason,
    generate_embankment_compartments,
    measure_valley_width,
    ridge_crest_walk,
    walk_embankment_pinch,
    watershed_cells,
)

RESOLUTION = 5.0
ORIGIN_X, ORIGIN_Y = 500000.0, 4500000.0
CRS = "EPSG:32617"


def _dem(array):
    return {
        "array": np.asarray(array, dtype=np.float64),
        "resolution_meters": (RESOLUTION, RESOLUTION),
        "origin_x": ORIGIN_X,
        "origin_y": ORIGIN_Y,
        "crs": CRS,
    }


def _flow(dem):
    filled = fill_depressions(dem["array"])
    flow_to_row, flow_to_col = compute_flow_direction(filled, dem["resolution_meters"])
    return filled, flow_to_row, flow_to_col


def _on_parcel(dem, boundary):
    rows, cols = dem["array"].shape
    col_x = dem["origin_x"] + (np.arange(cols) + 0.5) * RESOLUTION
    row_y = dem["origin_y"] - (np.arange(rows) + 0.5) * RESOLUTION
    xs, ys = np.meshgrid(col_x, row_y)
    return contains_xy(boundary, xs, ys) & ~np.isnan(dem["array"])


# =========================================================================
# FIXTURE A: the pinched valley. 40x21 at 5 m, channel down col 10,
# flow +row (south; elevation falls 0.25 m/row). Cross-section per row:
#
#     d = |c - 10|;  k(r) = 4 (rows < 18), 2 (rows 18-21), 5 (rows >= 22)
#     d <  k : valley floor,  base(r) + 0.5*d      (10% cross-slope in)
#     d == k : levee crest,   base(r) + 3.0
#     d >  k : outside,       base(r) + 1.0 - 0.05*(d-k-1)   (2 m below
#              the crest, falling gently away -- the prominence's fall)
#
# HAND-DERIVED CREST WALK (samples every 2.5 m, so every other sample
# lands EXACTLY on a cell boundary and floor() assigns it to the
# higher-index cell): walking +x, the j-th cell out is first sampled at
# distance 5j - 2.5; walking -x, at distance 5j (the boundary sample at
# 5j - 2.5 still reads the previous cell). The running max first records
# the levee and the next cell out sits 2.0 m below it (>= the 1.0 m
# prominence), so the crest is declared at the levee's first sample:
#
#     +x half = 5k - 2.5, -x half = 5k  ->  WIDTH = 10k - 2.5
#     k=4 -> 17.5 + 20.0 = 37.5         (the bowl)
#     k=2 ->  7.5 + 10.0 = 17.5         (the waist)
#     k=5 -> 22.5 + 25.0 = 47.5         (the widening below)
#
# (measure_valley_width's 'left' ray is +x for a southbound channel:
# perpendicular = (-dy, dx) = (1, 0).)
#
# FLOW, hand-checked: a floor cell's steepest descent is the DIAGONAL
# into the next channel row (drop 0.75/7.07 m = 10.6% beats the 10%
# straight-in and the 5% straight-down), so the floor drains through the
# channel; the channel's steepest descent is straight down-channel (5%);
# levee and outside cells drain away from the valley. The watershed of a
# channel cell is therefore the STAIRCASE {(r, 10 +/- m): m <= k(r)-1
# and r + m <= pinch_row} -- a floor cell at lateral offset m enters the
# channel m rows further down.
# =========================================================================

A_ROWS, A_COLS, A_CHANNEL = 40, 21, 10


def _k_of_row(r):
    if 18 <= r <= 21:
        return 2
    return 4 if r < 18 else 5


def _valley_array(rows, cols, channel, k_of_row):
    array = np.zeros((rows, cols))
    for r in range(rows):
        base = 100.0 - 0.25 * r
        k = k_of_row(r)
        for c in range(cols):
            d = abs(c - channel)
            if d < k:
                array[r, c] = base + 0.5 * d
            elif d == k:
                array[r, c] = base + 3.0
            else:
                array[r, c] = base + 1.0 - 0.05 * (d - k - 1)
    return array


A_DEM = _dem(_valley_array(A_ROWS, A_COLS, A_CHANNEL, _k_of_row))
A_BOUNDARY = box(
    ORIGIN_X + 1 * RESOLUTION + 0.1,
    ORIGIN_Y - 39 * RESOLUTION + 0.1,
    ORIGIN_X + 20 * RESOLUTION - 0.1,
    ORIGIN_Y - 1 * RESOLUTION - 0.1,
)
A_FILLED, A_FTR, A_FTC = _flow(A_DEM)
A_ON_PARCEL = _on_parcel(A_DEM, A_BOUNDARY)
A_NO_ROAD = np.zeros(A_DEM["array"].shape, dtype=bool)

# Flow sanity, per the fixture comment: floor drains diagonally into the
# channel, channel drains down-channel.
assert (int(A_FTR[12, 9]), int(A_FTC[12, 9])) == (13, 10), "floor d=1 takes the diagonal into the channel"
assert (int(A_FTR[12, 8]), int(A_FTC[12, 8])) == (13, 9), "floor d=2 chains inward one row per column"
assert (int(A_FTR[12, 10]), int(A_FTC[12, 10])) == (13, 10), "the channel drains straight down-channel"

# --- 1 [1]. widths at three stations + the pinch at the hand-known waist ---

_down = (0.0, -1.0)  # channel flow direction in ground space (south)
for row, k, expected_width, station in ((10, 4, 37.5, "bowl"), (18, 2, 17.5, "waist"), (23, 5, 47.5, "widening")):
    measured = measure_valley_width(A_DEM, (row, A_CHANNEL), _down)
    assert measured["width_m"] == expected_width, (
        f"{station} station (row {row}): hand-derived crest-to-crest width {expected_width}, "
        f"got {measured['width_m']}"
    )
    assert measured["bound_hit"] is False, f"{station}: both crests confirmed by the prominence fall"
    assert measured["left"]["half_width_m"] == 5.0 * k - 2.5, "the +x ray reads the levee at 5k - 2.5"
    assert measured["right"]["half_width_m"] == 5.0 * k, "the -x ray reads it one boundary sample later"

walk = walk_embankment_pinch(A_DEM, (10, A_CHANNEL), A_FTR, A_FTC, A_ON_PARCEL, A_NO_ROAD)
assert walk["found"] is True, f"the pinch walk must find the waist: {walk.get('reason_code')}"
assert walk["pinch_rowcol"] == (18, A_CHANNEL), (
    f"the embankment cell is the along-channel width minimum -- the FIRST waist row (18), "
    f"got {walk['pinch_rowcol']}"
)
assert walk["pinch_width_m"] == 17.5
assert walk["walk_distance_m"] == 40.0, "8 channel steps x 5 m from the seed to the waist"
assert walk["half_width_bound_hit"] is False
station_widths = [s["width_m"] for s in walk["stations"]]
assert station_widths[0] == 37.5 and 17.5 in station_widths and station_widths[-1] == 47.5, (
    f"the walk's stations trace bowl -> waist -> widening: {station_widths}"
)
print(
    "1a. Pinched valley: widths 37.5 / 17.5 / 47.5 at the three hand-derived stations; the "
    "embankment cell lands at the waist (18, 10) at 40 m downstream, width 17.5."
)

# --- 1b. the false-crest prominence guard, on a fully hand-controlled strip ---
# Walking +x along the middle row: 0.0 (start), 0.5, 1.4 (a 0.5m-scale
# knoll), 0.9 (a 0.5 m dip -- UNDER the 1.0 m prominence), 2.0, 3.0 (the
# real crest), 1.5 (a 1.5 m fall -- declares it). The guard must walk
# PAST the knoll: the crest is the 3.0 cell (first sampled at 22.5 m),
# never the knoll.
strip = np.zeros((3, 12))
strip[:] = [0.0, 0.0, 0.0, 0.5, 1.4, 0.9, 2.0, 3.0, 1.5, 1.4, 1.3, 1.2]
STRIP_DEM = _dem(strip)
start_xy = pixel_center_xy(STRIP_DEM, 1, 2)
guarded = ridge_crest_walk(STRIP_DEM, start_xy, (1.0, 0.0))
assert guarded["crest_rowcol"] == (1, 7), (
    f"the 0.5 m knoll dip stays under the prominence guard -- the crest is the real ridge at col 7, "
    f"got {guarded['crest_rowcol']}"
)
assert guarded["half_width_m"] == 22.5 and guarded["bound_hit"] is False
# Control: a monotone rising strip never confirms a crest -- bound hit,
# honestly flagged.
rising = np.tile(np.arange(12, dtype=float) * 0.3, (3, 1))
unbounded = ridge_crest_walk(_dem(rising), pixel_center_xy(_dem(rising), 1, 1), (1.0, 0.0), max_half_width_meters=20.0)
assert unbounded["bound_hit"] is True and unbounded["half_width_m"] == 20.0
print("1b. Prominence guard: the walk passes the 0.5 m knoll (crest at the real ridge, 22.5 m); a monotone rise bound-hits, flagged.")

# --- 2 [1-2 of the terminal-acceptance correction]. TERMINAL MINIMA ARE
# --- ACCEPTED, DISCLOSED, NOT REFUSED; the sole failure is
# --- no_constriction (minimum at the seed station) ---

# Monotonically widening: k grows downstream from the seed, so the width
# minimum is the seed's own station -- a dam at the storage cell is
# degenerate, and this is the ONE remaining walk failure.
def _widening_k(r):
    if r < 5:
        return 2
    if r < 10:
        return 3
    return 4 if r < 15 else 5


B_DEM = _dem(_valley_array(A_ROWS, A_COLS, A_CHANNEL, _widening_k))
B_FILLED, B_FTR, B_FTC = _flow(B_DEM)
B_ON_PARCEL = _on_parcel(B_DEM, A_BOUNDARY)
widening = walk_embankment_pinch(B_DEM, (2, A_CHANNEL), B_FTR, B_FTC, B_ON_PARCEL, A_NO_ROAD)
assert widening["found"] is False and widening["reason_code"] == wsa.REASON_NO_CONSTRICTION, (
    f"a monotonically widening valley yields nothing -- no_constriction: {widening.get('reason_code')}"
)
assert widening["still_narrowing_at_termination"] is False
assert widening["width_profile_min_m"] == 17.5 and widening["width_profile_max_m"] == 47.5, (
    "the failure record still discloses the walked width profile"
)

# TERMINAL ACCEPTANCE at the BOUNDARY: the boundary ends at row 18 --
# the walk's LAST on-parcel station is the first waist row, the valley
# still narrowing at the line. The old rule refused this
# (pinch_off_parcel); the dam-at-the-edge doctrine ACCEPTS it: the
# narrowest buildable crossing within the surveyed extent, flagged and
# disclosed.
SHORT_BOUNDARY = box(
    ORIGIN_X + 1 * RESOLUTION + 0.1,
    ORIGIN_Y - 19 * RESOLUTION + 0.1,
    ORIGIN_X + 20 * RESOLUTION - 0.1,
    ORIGIN_Y - 1 * RESOLUTION - 0.1,
)
at_boundary = walk_embankment_pinch(
    A_DEM, (10, A_CHANNEL), A_FTR, A_FTC, _on_parcel(A_DEM, SHORT_BOUNDARY), A_NO_ROAD
)
assert at_boundary["found"] is True, "a terminal minimum is accepted, not refused"
assert at_boundary["pinch_rowcol"] == (18, A_CHANNEL) and at_boundary["terminal"] == "boundary"
assert at_boundary["terminator"] == "boundary"
assert at_boundary["still_narrowing_at_termination"] is True, (
    "17.5 at the terminal station, strictly below the 37.5 before it"
)
assert at_boundary["width_profile_min_m"] == 17.5 and at_boundary["width_profile_max_m"] == 37.5

# TERMINAL ACCEPTANCE at a ROAD: a road strip across the valley at row
# 19 -- same acceptance, the road being the named terminator.
road_mask = np.zeros(A_DEM["array"].shape, dtype=bool)
road_mask[19, :] = True
at_road = walk_embankment_pinch(A_DEM, (10, A_CHANNEL), A_FTR, A_FTC, A_ON_PARCEL, road_mask)
assert at_road["found"] is True and at_road["terminal"] == "road"
assert at_road["pinch_rowcol"] == (18, A_CHANNEL) and at_road["terminator"] == "road"
assert at_road["still_narrowing_at_termination"] is True

# TERMINAL ACCEPTANCE at the 100 m WALK BOUND: a valley whose crest
# offset steps down from 6 to 5 exactly at the walk's 20th step (row
# 25 for a row-5 seed: 20 x 5 m = 100 m, the last station the bound
# admits), so the minimum (47.5) sits at the terminal station.
def _bound_k(r):
    return 6 if r < 25 else 5


C_DEM = _dem(_valley_array(A_ROWS, A_COLS, A_CHANNEL, _bound_k))
C_FILLED, C_FTR, C_FTC = _flow(C_DEM)
C_ON_PARCEL = _on_parcel(C_DEM, A_BOUNDARY)
at_bound = walk_embankment_pinch(C_DEM, (5, A_CHANNEL), C_FTR, C_FTC, C_ON_PARCEL, A_NO_ROAD)
assert at_bound["found"] is True and at_bound["terminal"] == "walk_bound"
assert at_bound["terminator"] == "distance_bound"
assert at_bound["pinch_rowcol"] == (25, A_CHANNEL) and at_bound["walk_distance_m"] == 100.0
assert at_bound["pinch_width_m"] == 47.5 and at_bound["still_narrowing_at_termination"] is True
assert at_bound["width_profile_min_m"] == 47.5 and at_bound["width_profile_max_m"] == 57.5

# THE TERMINAL-WIDENED VARIANT: the waist sits mid-walk near the end
# and the terminal station WIDENS again (k back up to 3), so the pinch
# is the interior waist, no terminal flag, still_narrowing False.
def _rewiden_k(r):
    if r < 16:
        return 4
    return 2 if r <= 18 else 3


D_DEM = _dem(_valley_array(A_ROWS, A_COLS, A_CHANNEL, _rewiden_k))
D_FILLED, D_FTR, D_FTC = _flow(D_DEM)
REWIDEN_BOUNDARY = box(
    ORIGIN_X + 1 * RESOLUTION + 0.1,
    ORIGIN_Y - 20 * RESOLUTION + 0.1,
    ORIGIN_X + 20 * RESOLUTION - 0.1,
    ORIGIN_Y - 1 * RESOLUTION - 0.1,
)
rewidened = walk_embankment_pinch(
    D_DEM, (10, A_CHANNEL), D_FTR, D_FTC, _on_parcel(D_DEM, REWIDEN_BOUNDARY), A_NO_ROAD
)
assert rewidened["found"] is True and rewidened["terminal"] is None, (
    "the mid-walk waist is an ordinary interior pinch -- no terminal flag"
)
assert rewidened["pinch_rowcol"] == (16, A_CHANNEL)
assert rewidened["still_narrowing_at_termination"] is False, (
    "the terminal station widened (27.5 after 17.5) -- the disclosure reads False"
)
print(
    "2. Terminal acceptance x3 (boundary / road / walk bound), each flagged with its terminator and "
    "still-narrowing True; the rewidened variant keeps its interior pinch (flag None, disclosure "
    "False); monotone widening is the one failure: no_constriction."
)

# --- 3 [3]. compartment assembly: transects, the watershed band, area ---

A_UPSTREAM = build_upstream_map(A_FTR, A_FTC)
_seed = {
    "rowcol": (10, A_CHANNEL),
    "xy": pixel_center_xy(A_DEM, 10, A_CHANNEL),
    "geometry_wgs84": {
        "type": "Point",
        "coordinates": tuple(
            transform_geom(CRS, "EPSG:4326", {"type": "Point", "coordinates": pixel_center_xy(A_DEM, 10, A_CHANNEL)})["coordinates"]
        ),
    },
    "blend_score": 0.8,
    "criteria_signature": {name: 0.5 for name in EMBANKMENT_WEIGHTS},
}
_screens = {
    "twi_score": np.full(A_DEM["array"].shape, 0.5),
    "depression_depth": np.zeros(A_DEM["array"].shape),
    "flow_accumulation": compute_flow_accumulation(A_FILLED, A_FTR, A_FTC),
    "slope_pct": np.full(A_DEM["array"].shape, 5.0),
    "soil_covered_mask": np.zeros(A_DEM["array"].shape, dtype=bool),
    "soil_checked": False,
}
_surfaces = {
    SURVEY_TYPE_EMBANKMENT: np.full(A_DEM["array"].shape, 0.6),
    "criteria": {
        SURVEY_TYPE_EMBANKMENT: {name: np.full(A_DEM["array"].shape, 0.5) for name in EMBANKMENT_WEIGHTS}
    },
}
compartment = build_embankment_compartment(
    A_DEM, _seed, walk, A_UPSTREAM, A_BOUNDARY, None, _surfaces, _screens
)
assert compartment is not None

# Transect crest points at hand-computed offsets: perpendicular to the
# BASELINE (the channel line), so the same crest math as the widths --
# +17.5/-20.0 m at the seed end (k=4), +7.5/-10.0 m at the pinch end
# (k=2), the +x/-x sample-boundary split stated at the fixture.
by_end = {t["end"]: t for t in compartment["transects"]}
seed_x = pixel_center_xy(A_DEM, 10, A_CHANNEL)[0]
assert by_end["seed"]["width_m"] == 37.5 and by_end["pinch"]["width_m"] == 17.5
assert round(by_end["seed"]["left"]["crest_xy"][0] - seed_x, 1) == 17.5
assert round(by_end["seed"]["right"]["crest_xy"][0] - seed_x, 1) == -20.0
assert round(by_end["pinch"]["left"]["crest_xy"][0] - seed_x, 1) == 7.5
assert round(by_end["pinch"]["right"]["crest_xy"][0] - seed_x, 1) == -10.0
assert not by_end["seed"]["bound_hit"] and not by_end["pinch"]["bound_hit"]

# The watershed-band ridge connection matches the fixture's divides: the
# watershed of the pinch is the hand-enumerated STAIRCASE (see the
# fixture comment), and the compartment is its rows 10..18 slice:
#   rows 10..15: all 7 floor columns (m <= 3, r+m <= 18)
#   row 16: m <= 2 (5 cells);  row 17: m <= 1 (3 cells);  row 18: the
#   pinch cell alone (its own row's floor neighbors enter the channel
#   BELOW the pinch and are honestly outside the compartment)
# = 42 + 5 + 3 + 1 = 51 cells = 1275 m^2 = 0.3151 ac.
shed = watershed_cells((18, A_CHANNEL), A_UPSTREAM)
expected_staircase = {
    (r, A_CHANNEL + s * m)
    for r in range(0, 19)
    for m in range(0, min(_k_of_row(r) - 1, 18 - r) + 1)
    for s in (-1, 1)
}
assert shed == expected_staircase, (
    f"the watershed must be the hand-enumerated staircase divide: "
    f"extra={sorted(shed - expected_staircase)[:5]} missing={sorted(expected_staircase - shed)[:5]}"
)
expected_cells = {
    (r, A_CHANNEL + s * m)
    for r in range(10, 19)
    for m in range(0, min(_k_of_row(r) - 1, 18 - r) + 1)
    for s in (-1, 1)
}
assert len(expected_cells) == 51
assert set(compartment["cells"]) == expected_cells, (
    "the compartment's cell population is the watershed staircase clipped to the transect band"
)
expected_footprint_acres = 51 * 25.0 / SQUARE_METERS_PER_ACRE
assert abs(compartment["compartment_footprint_acres"] - expected_footprint_acres) < 0.002, (
    f"hand-derived compartment footprint {expected_footprint_acres:.4f} ac, got "
    f"{compartment['compartment_footprint_acres']}"
)

# THE DRAWN HULL, HAND-DERIVED FROM THE SAME STAIRCASE. In cell units
# (x = column edge, y = row edge downward) the 51-cell footprint is
#   x 7..14, y 10..16   |  x 8..13, y 16..17
#   x 9..12, y 17..18   |  x 10..11, y 18..19
# whose extreme points are (7,10) (14,10) (14,16) (13,17) (12,18)
# (11,19) (10,19) (9,18) (8,17) (7,16). The three right-side steps sit
# EXACTLY on the line (14,16)->(11,19) (each step is -1 in x per +1 in
# y) and the three left-side steps on (10,19)->(7,16), so the hull is
# the hexagon (7,10) (14,10) (14,16) (11,19) (10,19) (7,16). Shoelace:
#   |(-70) + 84 + 90 + 19 + 27 + (-42)| / 2 = 54 cell^2
# = 54 * 25 m^2 = 1350 m^2 -- three cells of side-slope more than the
# 51-cell band, which is the hull reading wider where the valley
# pinches (build_embankment_compartment()'s own statement).
expected_hull_acres = 54 * 25.0 / SQUARE_METERS_PER_ACRE
assert abs(compartment["zone_acres"] - expected_hull_acres) < 0.002, (
    f"hand-derived drawn hull {expected_hull_acres:.4f} ac, got {compartment['zone_acres']}"
)
assert compartment["zone_acres"] > compartment["compartment_footprint_acres"], (
    "the hull is the wider claim; the footprint is the measured ground beneath it"
)
# THE FOOTPRINT SURVIVES INTACT -- the cell staircase is still there,
# which is what makes it the honest record rather than a second drawing.
footprint_polygon = compartment["compartment_footprint_polygon_utm"]
assert abs(footprint_polygon.area - 51 * 25.0) < 1e-6
assert footprint_polygon.within(compartment["polygon_utm"].buffer(1e-9)), (
    "the drawn hull contains its own footprint by construction"
)
assert compartment["polygon_utm"].equals(footprint_polygon.convex_hull), (
    "with no clip in play the drawn zone IS the footprint's convex hull"
)
assert len(footprint_polygon.exterior.coords) > len(compartment["polygon_utm"].exterior.coords), (
    "the staircase is preserved in the footprint and absent from the hull"
)
# BOTH GEOMETRIES CARRY A STORED WGS84 FORM, built at birth.
for key in ("geometry_wgs84", "compartment_footprint_geometry_wgs84"):
    assert compartment[key]["type"] == "Polygon", key
assert shape(compartment["compartment_footprint_geometry_wgs84"]).is_valid
# The sparse-anchor guard is SILENT on a dense compartment: 51/54 =
# 0.94, far above the 0.2 ratio.
assert compartment["sparse_anchor"] is False and wsa.FLAG_SPARSE_ANCHOR not in compartment["flags"]
assert compartment["truncated_by_boundary"] is False and compartment["truncated_by_road"] is False
assert compartment["baseline"]["length_m"] == 40.0
assert compartment["render_fill_polygon_utm"] is compartment["polygon_utm"], "render_fill identity"
assert "members" not in compartment and "member_acres" not in compartment, (
    "member-only statistics have no members here -- the honesty split made structural"
)

# Boundary and road clips each flagged on their fixtures: a boundary that
# cuts the compartment's west flank, and a road union overlapping its
# east flank.
narrow_boundary = box(
    ORIGIN_X + 8 * RESOLUTION + 0.1,  # cuts the floor columns 7..? west of col 8
    ORIGIN_Y - 39 * RESOLUTION + 0.1,
    ORIGIN_X + 20 * RESOLUTION - 0.1,
    ORIGIN_Y - 1 * RESOLUTION - 0.1,
)
clipped_compartment = build_embankment_compartment(
    A_DEM, _seed, walk, A_UPSTREAM, narrow_boundary, None, _surfaces, _screens
)
assert clipped_compartment["truncated_by_boundary"] is True
assert wsa.FLAG_TRUNCATED_BY_BOUNDARY in clipped_compartment["flags"]
assert clipped_compartment["zone_acres"] < compartment["zone_acres"]

road_union = box(
    ORIGIN_X + 12.4 * RESOLUTION, ORIGIN_Y - 16 * RESOLUTION, ORIGIN_X + 14 * RESOLUTION, ORIGIN_Y - 11 * RESOLUTION
)
road_clipped = build_embankment_compartment(
    A_DEM, _seed, walk, A_UPSTREAM, A_BOUNDARY, road_union, _surfaces, _screens
)
assert road_clipped["truncated_by_road"] is True
assert wsa.FLAG_TRUNCATED_BY_ROAD in road_clipped["flags"]
assert road_clipped["polygon_utm"].intersection(road_union).area < 1e-6, (
    "the compartment's drawn geometry stops at the road exclusion union"
)
assert road_clipped["pre_road_clip_polygon_utm"].intersection(road_union).area > 1.0, (
    "the PRE-clip geometry keeps the removed ground for the road_overlap_pct measurement"
)

# HULL ORDER, THE WHOLE POINT OF IT: a road strip cutting a notch clean
# THROUGH the compartment is bridged straight back over by the convex
# hull of the notched shape. Hulling AFTER the clip would hand that
# ground back; hulling FIRST and re-clipping does not. The strip below
# spans the full width of the band at rows 13-14, so the footprint is
# genuinely severed and its hull spans the gap.
severing_road = box(
    ORIGIN_X + 0 * RESOLUTION,
    ORIGIN_Y - 15 * RESOLUTION,
    ORIGIN_X + 21 * RESOLUTION,
    ORIGIN_Y - 13 * RESOLUTION,
)
severed = build_embankment_compartment(
    A_DEM, _seed, walk, A_UPSTREAM, A_BOUNDARY, severing_road, _surfaces, _screens
)
severed_footprint = severed["compartment_footprint_polygon_utm"]
assert severed_footprint.geom_type == "MultiPolygon", "the road strip severs the band in two"
assert severed_footprint.convex_hull.intersection(severing_road).area > 100.0, (
    "the naive hull of the clipped footprint DOES re-swallow the road strip -- which is the "
    "failure the documented order exists to prevent"
)
assert severed["polygon_utm"].intersection(severing_road).area < 1e-6, (
    "hull FIRST, then re-clip: the delivered geometry excludes the road strip"
)
assert severed["truncated_by_road"] is True and wsa.FLAG_TRUNCATED_BY_ROAD in severed["flags"]

# THE BOUNDARY EQUIVALENT, same shape of argument. A straight cut can
# never be re-crossed by the convex hull of what it left behind, so the
# fixture is a parcel with a NOTCH biting into the compartment's east
# flank at rows 14-15 (cols 12-13): the footprint goes concave around
# it, the hull bridges straight over it, and the re-clip takes it out
# again.
notched_boundary = A_BOUNDARY.difference(
    box(
        ORIGIN_X + 12 * RESOLUTION,
        ORIGIN_Y - 16 * RESOLUTION,
        ORIGIN_X + 21 * RESOLUTION,
        ORIGIN_Y - 14 * RESOLUTION,
    )
)
notched = build_embankment_compartment(
    A_DEM, _seed, walk, A_UPSTREAM, notched_boundary, None, _surfaces, _screens
)
notched_footprint = notched["compartment_footprint_polygon_utm"]
assert notched_footprint.difference(notched_boundary).area < 1e-6, "the footprint is on-parcel"
assert notched_footprint.convex_hull.difference(notched_boundary).area > 25.0, (
    "the naive hull of the clipped footprint bridges straight back over the boundary notch -- "
    "the failure the documented order exists to prevent, boundary edition"
)
assert notched["polygon_utm"].difference(notched_boundary).area < 1e-6, (
    "hull FIRST, then re-clip: the delivered geometry stays on-parcel"
)
assert notched["truncated_by_boundary"] is True
assert wsa.FLAG_TRUNCATED_BY_BOUNDARY in notched["flags"]

# AND THE FLAG SURVIVES A STRAIGHT CUT, which is the case a naive
# before/after comparison on the hull loses: the west-flank box above
# clips the compartment at a straight line, so hull(footprint) sits
# flat against it and crosses nothing -- yet the boundary plainly
# removed ground this survey area would otherwise claim, and the flag
# says so because it is measured against the unconstrained claim.
assert clipped_compartment["polygon_utm"].convex_hull.difference(narrow_boundary).area < 1e-6, (
    "the straight cut leaves a hull that crosses nothing -- the case being guarded"
)
assert clipped_compartment["truncated_by_boundary"] is True

print(
    f"3. Assembly: transect crests at +/-17.5 (seed) and +/-7.5 m (pinch); the watershed staircase "
    f"matches the hand-enumerated divide; footprint = 51 cells = {expected_footprint_acres:.4f} ac, "
    f"drawn hull = 54 cells = {expected_hull_acres:.4f} ac (got {compartment['zone_acres']}); "
    f"hull-then-clip proven on a severing road and a boundary notch the naive hull bridges; "
    f"boundary and road clips each flagged."
)

# --- 3b. THE SPARSE-ANCHOR GUARD ON THE EMBANKMENT PATH ---
#
# The guard that makes a hull-based floor honest for a type whose anchor
# is one narrow watershed band. anchor = compartment_footprint_acres,
# claim = zone_acres (the drawn hull); under
# SPARSE_ANCHOR_MEMBER_FRACTION the zone says so on its own record.
#
# THE THIN FIXTURE IS A V-SHAPED WATERSHED, hand-built: two one-cell
# arms converging on the embankment cell. A convex hull only inflates
# over a non-convex shape, so a straight band (fixture A's staircase, 51
# cells under a 54-cell hull, ratio 0.94) can never trip this -- a
# BENDING valley is what can, and it is the shape the guard is for. The
# upstream map is handed in directly, which is what watershed_cells()
# takes; no DEM is bent to produce it.
#
# HAND-DERIVED. Arms (8+i, i) and (8+i, 20-i) for i in 0..10, meeting at
# (18, 10): 21 distinct cells. In cell units (x = column edge, ry = row
# edge downward) their extreme corners are (0,8) (21,8) (21,9) (11,19)
# (10,19) (0,9) -- every intermediate arm corner sits EXACTLY on
# (21,9)->(11,19) or (0,9)->(10,19) (each arm steps one column per row),
# so the hull is that hexagon. Shoelace:
#   |(-168) + 21 + 300 + 19 + 90 + 0| / 2 = 131 cell^2
# Anchor/claim = 21/131 = 0.160, under the 0.2 guard -> it fires.
WIDE_BOUNDARY = box(
    ORIGIN_X - RESOLUTION,
    ORIGIN_Y - 41 * RESOLUTION,
    ORIGIN_X + 22 * RESOLUTION,
    ORIGIN_Y + RESOLUTION,
)
_v_pinch = (18, A_CHANNEL)
_v_arm_cells = [(8 + i, i) for i in range(11)] + [(8 + i, 20 - i) for i in range(11)]
assert len(set(_v_arm_cells)) == 21, "the two arms share only the embankment cell"
# One-step feeder adjacency along each arm, toward the pinch.
_v_upstream = {}
for _arm in ((8 + i, i) for i in range(11)), ((8 + i, 20 - i) for i in range(11)):
    _chain = list(_arm)
    for _upper, _lower in zip(_chain, _chain[1:]):
        _v_upstream.setdefault(_lower, []).append(_upper)
assert watershed_cells(_v_pinch, _v_upstream) == set(_v_arm_cells)

_v_seed_xy = pixel_center_xy(A_DEM, 8, A_CHANNEL)
_v_seed_lon, _v_seed_lat = transform_geom(CRS, "EPSG:4326", {"type": "Point", "coordinates": _v_seed_xy})[
    "coordinates"
]
_v_seed = {
    "rowcol": (8, A_CHANNEL),
    "xy": _v_seed_xy,
    "geometry_wgs84": {"type": "Point", "coordinates": (_v_seed_lon, _v_seed_lat)},
    "blend_score": 0.8,
    "criteria_signature": {name: 0.5 for name in EMBANKMENT_WEIGHTS},
}
_v_walk = {
    "found": True,
    "terminator": None,
    "stations": [],
    "pinch_index": 2,
    "pinch_rowcol": _v_pinch,
    "pinch_width_m": 17.5,
    "walk_distance_m": 50.0,
    "half_width_bound_hit": False,
    "terminal": None,
    "still_narrowing_at_termination": False,
    "width_profile_min_m": 17.5,
    "width_profile_max_m": 37.5,
}
sparse = build_embankment_compartment(
    A_DEM, _v_seed, _v_walk, _v_upstream, WIDE_BOUNDARY, None, _surfaces, _screens
)
assert abs(sparse["compartment_footprint_acres"] - 21 * 25.0 / SQUARE_METERS_PER_ACRE) < 0.002, (
    f"hand-derived V band 21 cells, got {sparse['compartment_footprint_acres']}"
)
assert abs(sparse["zone_acres"] - 131 * 25.0 / SQUARE_METERS_PER_ACRE) < 0.002, (
    f"hand-derived hull 131 cells, got {sparse['zone_acres']}"
)
_sparse_ratio = sparse["compartment_footprint_acres"] / sparse["zone_acres"]
assert _sparse_ratio < wsa.SPARSE_ANCHOR_MEMBER_FRACTION, _sparse_ratio
assert sparse["sparse_anchor"] is True and wsa.FLAG_SPARSE_ANCHOR in sparse["flags"], (
    "a hull vastly exceeding its band announces itself rather than reading as solid candidate ground"
)
# ... and stays SILENT on the dense one, which is the same code path with
# a different shape underneath it (0.94, nowhere near the ratio).
_dense_ratio = compartment["compartment_footprint_acres"] / compartment["zone_acres"]
assert _dense_ratio > wsa.SPARSE_ANCHOR_MEMBER_FRACTION
assert compartment["sparse_anchor"] is False and wsa.FLAG_SPARSE_ANCHOR not in compartment["flags"]
# The guard rides the wire on both the feature and the narrative block --
# it is the reason a hull-based floor is honest, so it may not be
# internal-only.
_sparse_feature = wsa._zone_feature_properties(
    {
        **sparse,
        "id": 0, "rank": 1, "status": wsa.ZONE_STATUS_NOMINATED, "drop_reason": None,
        "cross_type_overlaps": [], "canopy_overlap_pct": None, "road_overlap_pct": None,
        "production_overlap_pct": None, "primary_production_area_relationship": None,
        "production_area_relationships": [], "has_service_relationship": False,
        "served_production_area_ids": [],
    }
)
assert _sparse_feature["sparse_anchor"] is True
assert _sparse_feature["compartment_footprint_acres"] == sparse["compartment_footprint_acres"]
print(
    f"3b. Sparse anchor on the embankment path: a V-shaped 21-cell band under a hand-derived "
    f"131-cell hull reads {_sparse_ratio:.3f} and FIRES; fixture A's straight band reads "
    f"{_dense_ratio:.3f} and stays silent. Both acreages and the guard ride the wire."
)

# --- 4 [4]. dedupe: two seeds, one pinch -> one compartment ---

_two_seed_surface = np.zeros(A_DEM["array"].shape)
_two_seed_surface[6, A_CHANNEL] = 0.9
_two_seed_surface[13, A_CHANNEL] = 0.8  # 35 m from the first: both seed
_two_surfaces = {
    SURVEY_TYPE_EMBANKMENT: _two_seed_surface,
    "criteria": {
        SURVEY_TYPE_EMBANKMENT: {name: np.full(A_DEM["array"].shape, 0.5) for name in EMBANKMENT_WEIGHTS}
    },
}
gate = A_ON_PARCEL.copy()
compartments, seed_records = generate_embankment_compartments(
    A_DEM,
    _two_surfaces,
    gate,
    A_ON_PARCEL,
    A_NO_ROAD,
    None,
    A_BOUNDARY,
    A_FTR,
    A_FTC,
    _screens,
)
assert len(seed_records) == 2 and [r["blend_score"] for r in seed_records] == [0.9, 0.8]
assert len(compartments) == 1, "two seeds walking to the same embankment cell collapse to ONE compartment"
assert compartments[0]["seed"]["rowcol"] == (6, A_CHANNEL), "the higher-blend seed keeps the compartment"
assert compartments[0]["pinch"]["rowcol"] == (18, A_CHANNEL)
winner_record, loser_record = seed_records
assert winner_record["status"] == wsa.SEED_STATUS_COMPARTMENT
assert loser_record["status"] == wsa.SEED_STATUS_FAILED
assert loser_record["_duplicate_of_zone"] is compartments[0], (
    "the loser carries the winner reference; the compute core writes duplicate_of_zone_<id> once ids exist"
)

# Compartment-overlap dedupe on hand-built polygons: A (blend 0.9,
# 20x20) and B (blend 0.8, 20x20 shifted 8 m: 240 m^2 shared > half of
# 400) collapse; C (far away) survives.
_mini = lambda blend, poly: {"seed_blend_score": blend, "polygon_utm": poly}
mini_a = _mini(0.9, box(0, 0, 20, 20))
mini_b = _mini(0.8, box(8, 0, 28, 20))
mini_c = _mini(0.7, box(100, 0, 120, 20))
kept, duplicates = dedupe_compartments_by_overlap([mini_b, mini_a, mini_c])
assert kept == [mini_a, mini_c] and duplicates == [mini_b], (
    "overlap beyond half the smaller collapses to the higher-blend seed's compartment"
)
assert duplicates[0]["_duplicate_of_zone"] is mini_a
assert duplicate_of_zone_reason(7) == "duplicate_of_zone_7", "the reason code names the surviving zone"

# DEDUPE ON A SHARED TERMINAL PINCH (likely on a real parcel, where
# parallel reaches exit the same boundary stretch): two seeds on the
# same channel, the boundary cutting the walk at the still-narrowing
# waist -- both walks end at the SAME terminal embankment cell, and the
# pinch-cell dedupe collapses them exactly as it does an interior pair.
_terminal_surface = np.zeros(A_DEM["array"].shape)
_terminal_surface[6, A_CHANNEL] = 0.9
_terminal_surface[13, A_CHANNEL] = 0.8
_terminal_surfaces = {
    SURVEY_TYPE_EMBANKMENT: _terminal_surface,
    "criteria": {
        SURVEY_TYPE_EMBANKMENT: {name: np.full(A_DEM["array"].shape, 0.5) for name in EMBANKMENT_WEIGHTS}
    },
}
_short_on_parcel = _on_parcel(A_DEM, SHORT_BOUNDARY)
terminal_compartments, terminal_records = generate_embankment_compartments(
    A_DEM,
    _terminal_surfaces,
    _short_on_parcel,
    _short_on_parcel,
    A_NO_ROAD,
    None,
    SHORT_BOUNDARY,
    A_FTR,
    A_FTC,
    _screens,
)
assert len(terminal_compartments) == 1, "two seeds, one boundary throat -> one compartment"
assert terminal_compartments[0]["seed"]["rowcol"] == (6, A_CHANNEL), "the higher-blend seed keeps it"
assert terminal_compartments[0]["pinch"]["rowcol"] == (18, A_CHANNEL)
assert terminal_compartments[0]["pinch_terminal"] == "boundary"
assert wsa.FLAG_PINCH_AT_BOUNDARY in terminal_compartments[0]["flags"]
assert wsa.FLAG_STILL_NARROWING in terminal_compartments[0]["flags"]
assert terminal_compartments[0]["still_narrowing_at_termination"] is True
assert terminal_records[1]["status"] == wsa.SEED_STATUS_FAILED
assert terminal_records[1]["_duplicate_of_zone"] is terminal_compartments[0]
print(
    "4. Dedupe: same-pinch seeds collapse to the higher blend (loser attributed) -- for interior AND "
    "shared TERMINAL pinches (one boundary throat, one compartment, flagged); overlap beyond half "
    "the smaller collapses likewise."
)

# --- reason-code retirement, AST-level per house pattern ---
# The refusal vocabulary is DELETED, not zeroed: the module has no such
# constants and no code path names them (docstrings may narrate the
# history -- an AST Name scan does not see string literals inside
# docstrings, which is exactly the house distinction).
import ast as _ast  # noqa: E402
import inspect as _inspect  # noqa: E402

for retired in ("REASON_PINCH_OFF_PARCEL", "REASON_PINCH_BLOCKED_BY_ROAD", "REASON_NO_PINCH_WITHIN_BOUND"):
    assert not hasattr(wsa, retired), f"{retired} is retired -- deleted, never aliased"
_wsa_ast = _ast.parse(_inspect.getsource(wsa))
_named = {node.id for node in _ast.walk(_wsa_ast) if isinstance(node, _ast.Name)}
for retired in ("REASON_PINCH_OFF_PARCEL", "REASON_PINCH_BLOCKED_BY_ROAD", "REASON_NO_PINCH_WITHIN_BOUND"):
    assert retired not in _named, f"no code path may still name {retired}"
_string_values = {
    node.value
    for node in _ast.walk(_wsa_ast)
    if isinstance(node, _ast.Constant) and isinstance(node.value, str)
}
for retired_value in ("pinch_off_parcel", "pinch_blocked_by_road", "no_pinch_within_bound"):
    assert retired_value not in _string_values, (
        f"the retired code {retired_value!r} must not survive as an exact string constant anywhere "
        "in the module -- a docstring may NARRATE it inside prose, but no literal equal to the code "
        "itself may exist for anything to emit"
    )
assert wsa.REASON_NO_CONSTRICTION == "no_constriction"
print("   Retirement: the three refusal codes are absent at the attribute, AST-name, and string-constant level; no_constriction replaces them.")

# --- the full compute path: seeds, waist pinch, dedupe codes ---
# FIXTURE A2: the same construction with the waist moved DOWNSTREAM
# (rows 28-31) so the iterative seeding's own 30 m spacing puts a
# qualifying seed (row 24 -- the highest-blend channel cell its claim
# window leaves above the waist) far enough above the pinch for the
# compartment to clear the 0.1 ac floor: the staircase for a 4-row
# span is 7+7+5+3+1 = 23 cells = 0.1421 ac of FOOTPRINT, hulled to
# 26 cells = 0.1606 ac (hand-derived below). On this valley the
# excavated surface scores nothing (the floor's ~11% Horn slope is past
# the seep taper's meaningful range at these wetness levels), so the
# channel compartment is also the pooled rank-1 selection.
#
# THE RESURRECTIONS ARE THE POINT OF THE HULL CHANGE AND ARE ASSERTED
# HERE. Two off-channel compartments on the valley's outer flanks
# measured 0.0801/0.0803 ac of watershed band and were dropped under
# the 0.1 ac floor; their hulls measure 0.1197/0.1205 ac and they now
# survive. That is the floor asking the right question (the walkable
# claim) rather than a floor quietly widened -- and the read the design
# asked for is whether a resurrection is a survey area or a sliver
# wearing a generous hull. These two are survey areas: anchor/claim of
# ~0.67, nowhere near the 0.2 sparse_anchor ratio. A compartment
# genuinely too small still drops.


def _k2_of_row(r):
    if 28 <= r <= 31:
        return 2
    return 4 if r < 28 else 5


A2_DEM = _dem(_valley_array(A_ROWS, A_COLS, A_CHANNEL, _k2_of_row))

# THE ACCUMULATION OVERRIDE, AND WHY THE ABSOLUTE TWI CURVE NEEDED IT.
# Every cell carries THREE TIMES the upslope area this 40-row window
# models -- the coherent reading of a fixture window that is the lower
# reach of a longer valley, applied UNIFORMLY so no cell's standing
# relative to another changes.
#
# It is here because the retired parcel-relative TWI was inflating these
# flank draws. They carry 15-39 cells (0.09-0.24 ac) of catchment; a
# percentile scored them near the top of the parcel simply for being the
# wettest ground PRESENT, which put their blends at 0.5468-0.566 and
# over the seeding minimum. twi_score() reads the same cells at raw TWI
# 7.3-8.3 -- real convergence, modest -- and their blends land at 0.4885,
# just under. The absolute curve is right about them and the arithmetic
# is worth stating: an off-channel cell with no drainage credit tops out
# at 0.25*slope + 0.25*soil = 0.375 plus its TWI, so under the absolute
# curve an EMBANKMENT SEED REQUIRES REAL CONTRIBUTING AREA -- which is
# the correct reading of a dam across a drainageway, and a finding this
# branch reports rather than tunes around.
#
# So the fixture gives these draws catchment that genuinely earns the
# score, instead of borrowing it from a ranking. THE SCALE IS CHOSEN, NOT
# ROUNDED: 2.9 is the value at which EVERY hand-derived number below is
# reproduced exactly -- the channel compartment still seeds at
# (24, A_CHANNEL) and pinches at (28, A_CHANNEL) with its 23-cell
# staircase under a 26-cell hull, and the two flank compartments still
# measure 0.0801/0.0803 ac of band under 0.1197/0.1205 ac of hull, an
# anchor ratio of ~0.67. Nothing about the geometry this fixture tests
# moved; only the wetness the flanks are entitled to claim did.
_A2_FILLED, _A2_FTR, _A2_FTC = _flow(A2_DEM)
A_ACCUMULATION_SCALE = 2.9
a2_accumulation = (
    compute_flow_accumulation(_A2_FILLED, _A2_FTR, _A2_FTC).astype(float) * A_ACCUMULATION_SCALE
)
a_result = compute_water_survey_areas(A2_DEM, A_BOUNDARY, flow_accumulation=a2_accumulation)
a_comps = a_result["zones_by_type"][SURVEY_TYPE_EMBANKMENT]
assert a_comps, "the real blend seeds the channel and at least one compartment survives"
_channel_comps = [z for z in a_comps if z["seed"]["rowcol"][1] == A_CHANNEL]
assert len(_channel_comps) == 1, "one compartment is seeded on the channel itself"
assert _channel_comps[0]["pinch"]["rowcol"] == (28, A_CHANNEL), (
    "the channel compartment's embankment cell sits at the waist"
)

# THE RESURRECTED FLANK COMPARTMENTS -- see the fixture note. Each
# carries BOTH acreages, each clears the floor on the hull and would
# not have on the band, and neither trips the sparse-anchor guard.
_flank_comps = [z for z in a_comps if z["seed"]["rowcol"][1] != A_CHANNEL]
assert len(_flank_comps) == 2, f"two flank compartments resurrect: {[z['id'] for z in a_comps]}"
for zone in _flank_comps:
    assert zone["compartment_footprint_acres"] < wsa.MIN_SURVEY_REGION_AREA_ACRES, (
        "the band alone would not have cleared the floor -- this zone is a resurrection"
    )
    assert zone["zone_acres"] >= wsa.MIN_SURVEY_REGION_AREA_ACRES, "its walkable claim does"
    assert zone["sparse_anchor"] is False, (
        "read for sliver-with-a-generous-hull: these are anchored at ~0.67, not near the 0.2 ratio"
    )
# ... AND A GENUINELY TINY ONE STILL DROPS, with the reason and both
# acreages on its record. The floor moving basis is not the floor going
# away: the same run that resurrects two compartments drops a third.
_floor_drops = [
    zone
    for zone in a_result["dropped_zones"]
    if zone["survey_type"] == SURVEY_TYPE_EMBANKMENT
    and zone["drop_reason"] == wsa.FLAG_BELOW_MIN_AREA
]
assert _floor_drops, "a compartment too small even as a hull must still be dropped on this fixture"
for zone in _floor_drops:
    assert zone["zone_acres"] < wsa.MIN_SURVEY_REGION_AREA_ACRES, (
        "the judged number is the DRAWN HULL -- one rule, both types"
    )
    assert 0 < zone["compartment_footprint_acres"] <= zone["zone_acres"], (
        "both acreages ride the dropped record, never one"
    )
    assert zone["status"] == wsa.ZONE_STATUS_DROPPED and zone["rank"] is None

# INTERIOR-PINCH REGRESSION, pinned: the accepted-terminal correction
# must not move an interior pinch by a cell or flag it -- same seed,
# same pinch, same hand-derived acreage, terminal None, none of the
# pinch_at_* flags, and the failure vocabulary is no_constriction plus
# dedupe codes ONLY.
_interior = _channel_comps[0]
assert _interior["seed"]["rowcol"] == (24, A_CHANNEL) and _interior["pinch"]["rowcol"] == (28, A_CHANNEL)
# DUAL ACREAGE, both hand-derived. The footprint is the 23-cell
# staircase; its hull is the hexagon (7,24) (14,24) (14,26) (11,29)
# (10,29) (7,26) in cell units -- the three right-side steps sit on
# (14,26)->(11,29) and the three left-side on (10,29)->(7,26), same
# construction as the section-3 hull. Shoelace: |(-168) + 28 + 120 +
# 29 + 57 + (-14)| / 2 = 26 cell^2.
assert _interior["compartment_footprint_acres"] == round(23 * 25.0 / SQUARE_METERS_PER_ACRE, 4)
assert _interior["zone_acres"] == round(26 * 25.0 / SQUARE_METERS_PER_ACRE, 4)
assert _interior["pinch_terminal"] is None and _interior["still_narrowing_at_termination"] is False
assert not any(
    flag in _interior["flags"]
    for flag in (wsa.FLAG_PINCH_AT_BOUNDARY, wsa.FLAG_PINCH_AT_ROAD, wsa.FLAG_PINCH_AT_WALK_BOUND, wsa.FLAG_STILL_NARROWING)
), "an interior pinch carries no terminal disclosure -- unchanged behavior"

a_seeds = a_result["embankment_seeds"]
a_failed = [r for r in a_seeds if r["status"] == wsa.SEED_STATUS_FAILED]
assert a_failed, "the never-narrowing/duplicate seeds report their reasons"
for record in a_failed:
    assert record.get("reason_code"), f"every failed seed carries a reason code: {record}"
    assert record["reason_code"] == wsa.REASON_NO_CONSTRICTION or record["reason_code"].startswith(
        wsa.DUPLICATE_OF_ZONE_REASON_PREFIX
    ), f"the failure vocabulary is no_constriction + dedupe only now: {record['reason_code']}"
_dup_codes = [r["reason_code"] for r in a_failed if r["reason_code"].startswith(wsa.DUPLICATE_OF_ZONE_REASON_PREFIX)]
for code in _dup_codes:
    named = int(code[len(wsa.DUPLICATE_OF_ZONE_REASON_PREFIX):])
    assert named in {z["id"] for z in a_result["zones"] + a_result["dropped_zones"]}, (
        "a duplicate reason names a real zone id"
    )

# --- 5 [5]. the floor on compartment acres, visible with reason AND acreage ---

# Fixture A carries the SAME uniform accumulation scale as A2 and for
# the same reason (see A_ACCUMULATION_SCALE): without it, the absolute
# TWI curve leaves this DEM's three qualifying seeds all DOWNSTREAM of
# the waist, so every walk honestly reports no_constriction and no
# compartment is built at all -- which would leave this section with
# nothing to drop and would silently stop testing the floor.
a_floor_accumulation = (
    compute_flow_accumulation(A_FILLED, A_FTR, A_FTC).astype(float) * A_ACCUMULATION_SCALE
)
_real_floor = wsa.MIN_SURVEY_REGION_AREA_ACRES
try:
    wsa.MIN_SURVEY_REGION_AREA_ACRES = 1000.0  # everything sinks under it
    floored = compute_water_survey_areas(
        A_DEM, A_BOUNDARY, flow_accumulation=a_floor_accumulation
    )
finally:
    wsa.MIN_SURVEY_REGION_AREA_ACRES = _real_floor
assert floored["zones"] == [] and floored["selected_water_zone"] is None
floored_comps = [z for z in floored["dropped_zones"] if z["survey_type"] == SURVEY_TYPE_EMBANKMENT]
assert floored_comps, "the compartments exist and are dropped, never silently absent"
for zone in floored_comps:
    if zone["drop_reason"].startswith(wsa.DUPLICATE_OF_ZONE_REASON_PREFIX):
        continue  # dedupe drops keep their own reason -- dedupe decides existence before the floor
    assert zone["drop_reason"] == wsa.FLAG_BELOW_MIN_AREA, zone["drop_reason"]
    assert zone["zone_acres"] > 0 and zone["zone_acres"] < 1000.0, (
        "the judged acreage -- the DRAWN HULL, one rule for both types -- rides the dropped record"
    )
    assert zone["compartment_footprint_acres"] > 0, (
        "and so does the anchoring band beneath it: both acreages, never one"
    )
    assert zone["zone_acres"] >= zone["compartment_footprint_acres"]
    assert zone["seed_blend_score"] > 0
floored_narrative = wsa.build_narrative_data(floored)
assert floored_narrative["dropped_count"] == len(floored["dropped_zones"]), (
    "the drops are visible in the narrative accounting"
)
print(
    f"5. Floor: at a prohibitive floor every compartment lands in dropped_zones with "
    f"below_min_area + its acreage ({len(floored_comps)} attributed drops); diagnostics/narrative carry the count."
)

# --- 6 [6]. the reporting honesty split: seed score vs compartment means ---

split_zone = _channel_comps[0]
assert split_zone["seed_blend_score"] != split_zone["mean_suitability"], (
    "the anchor claim and the walked ground's mean differ on this fixture -- the compartment "
    "deliberately averages in its side slopes"
)
assert split_zone["seed_blend_score"] > split_zone["mean_suitability"], (
    "the seed is the compartment's best cell by construction here"
)
assert set(split_zone["seed"]["criteria_signature"]) == set(EMBANKMENT_WEIGHTS)
narrative = wsa.build_narrative_data(a_result)
block = next(b for b in narrative["zones"] if b["id"] == split_zone["id"])
assert block["seed_blend_score"] == split_zone["seed_blend_score"]
assert block["criteria"][next(iter(EMBANKMENT_WEIGHTS))]["mean_score"] == (
    split_zone["criterion_contributions"][next(iter(EMBANKMENT_WEIGHTS))]["mean_score"]
), "the block's criteria are the COMPARTMENT's means, beside the seed's own score"
assert "seed_criteria_signature" in block and block["seed_criteria_signature"] != {
    name: entry["mean_score"] for name, entry in split_zone["criterion_contributions"].items()
}, "the seed signature and the compartment means are distinct payloads"
json.dumps(narrative)
# The wire label carries the narrative sentence's spine:
feature = next(
    f
    for f in wsa.survey_areas_to_geojson(a_result["zones"])["features"]
    if f["properties"]["layer"] == "survey_zone_embankment" and f["properties"]["zone_id"] == split_zone["id"]
)
assert f"{split_zone['zone_acres']} ac to survey, anchored by a" in feature["properties"]["label"]
assert (
    f"{split_zone['compartment_footprint_acres']} ac valley compartment" in feature["properties"]["label"]
), "the label carries BOTH acreages -- the drawn claim and the band anchoring it"
assert f"{split_zone['seed_blend_score']}-scoring storage cell" in feature["properties"]["label"]
assert "dam reach at the downstream end" in feature["properties"]["label"]
assert feature["properties"]["seed_blend_score"] == split_zone["seed_blend_score"]
assert feature["properties"]["mean_suitability"] == split_zone["mean_suitability"]
assert feature["properties"]["compartment_footprint_acres"] == split_zone["compartment_footprint_acres"]
assert feature["properties"]["sparse_anchor"] == split_zone["sparse_anchor"]
print(
    f"6. Honesty split: seed {split_zone['seed_blend_score']} vs compartment mean "
    f"{split_zone['mean_suitability']} -- distinct on the record, both in the narrative block and the wire label."
)

# --- the consumer contract, replayed on a compartment (design item 8's
# --- contract half; the build_pipeline_context() run itself lives in
# --- test_pipeline_context.py with a compartment as rank-1) ---

# The pooled selection is one of the survivors (the pooled scale is the
# per-type instruments' -- asserted numerically elsewhere); the CONTRACT
# is replayed on the rank-1 COMPARTMENT itself, whichever type the pool
# crowned (test_pipeline_context.py additionally proves a compartment AS
# the selection through the full context).
assert a_result["selected_water_zone"] in a_result["zones"]
selected = next(z for z in a_comps if z["rank"] == 1)
assert selected["render_fill_polygon_utm"] is selected["polygon_utm"]
assert selected["render_fill_geometry_wgs84"] is selected["geometry_wgs84"]
assert isinstance(selected["representative_elevation_m"], float)
assert isinstance(selected["id"], int) and selected["rank"] == 1
assert selected["served_production_area_ids"] == []
_ = selected["render_fill_polygon_utm"].buffer(6.096)          # road_corridors pond exclusion
_ = unary_union([selected["render_fill_polygon_utm"]])          # solar water_zones union
_ = selected["render_fill_polygon_utm"] if selected else None   # fencing truthiness guard
_ = 101.5 - selected["representative_elevation_m"]              # keypoint elevation differential
_ = f"Water zone {selected['id']}: log line"                    # render_layout_map id branch
print("   Contract: the rank-1 compartment carries every consumer access pattern intact.")

# --- terminal-pinch compartment as the pooled RANK-1, full compute path ---
# Fixture A2 with the parcel line drawn at the FIRST waist row (28):
# the winning seed's walk is cut at the line while still narrowing, the
# terminal minimum is accepted, and the boundary-flagged compartment is
# the pooled rank-1 -- the first networked run's pinch_off_parcel
# failures, converted into the property's honest answer. (The same
# case runs through the real build_pipeline_context() in
# test_pipeline_context.py.)
WAIST_BOUNDARY = box(
    ORIGIN_X + 1 * RESOLUTION + 0.1,
    ORIGIN_Y - 29 * RESOLUTION + 0.1,
    ORIGIN_X + 20 * RESOLUTION - 0.1,
    ORIGIN_Y - 1 * RESOLUTION - 0.1,
)
# Same A2 DEM, same A_ACCUMULATION_SCALE override (accumulation is a
# property of the DEM, not of the boundary, so the identical array
# serves both runs) -- only the parcel line moves.
terminal_result = compute_water_survey_areas(
    A2_DEM, WAIST_BOUNDARY, flow_accumulation=a2_accumulation
)
terminal_zone = terminal_result["selected_water_zone"]
assert terminal_zone is not None and terminal_zone["survey_type"] == SURVEY_TYPE_EMBANKMENT, (
    "the boundary-terminal compartment is the pooled rank-1 on this fixture"
)
assert terminal_zone["rank"] == 1
assert terminal_zone["pinch"]["rowcol"] == (28, A_CHANNEL), "the dam reach sits at the line"
assert terminal_zone["pinch_terminal"] == "boundary"
assert terminal_zone["still_narrowing_at_termination"] is True
assert wsa.FLAG_PINCH_AT_BOUNDARY in terminal_zone["flags"]
assert wsa.FLAG_STILL_NARROWING in terminal_zone["flags"]
assert wsa.FLAG_PINCH_AT_ROAD not in terminal_zone["flags"] and wsa.FLAG_PINCH_AT_WALK_BOUND not in terminal_zone["flags"], (
    "exactly one terminal flag, naming the terminator"
)
# The consumer contract holds on a terminal-pinch selection:
assert terminal_zone["render_fill_polygon_utm"] is terminal_zone["polygon_utm"]
assert isinstance(terminal_zone["representative_elevation_m"], float)
assert isinstance(terminal_zone["id"], int) and terminal_zone["served_production_area_ids"] == []
_ = terminal_zone["render_fill_polygon_utm"].buffer(6.096)
_ = unary_union([terminal_zone["render_fill_polygon_utm"]])
# The disclosure travels: narrative block and wire properties both
# carry the terminator, the still-narrowing statement, and the walked
# width profile's extremes.
terminal_narrative = wsa.build_narrative_data(terminal_result)
terminal_block = next(b for b in terminal_narrative["zones"] if b["id"] == terminal_zone["id"])
assert terminal_block["pinch_terminal"] == "boundary"
assert terminal_block["still_narrowing_at_termination"] is True
assert terminal_block["width_profile_min_ft"] < terminal_block["width_profile_max_ft"]
json.dumps(terminal_narrative)
terminal_feature = next(
    f
    for f in wsa.survey_areas_to_geojson(terminal_result["zones"])["features"]
    if f["properties"].get("zone_id") == terminal_zone["id"]
)
assert terminal_feature["properties"]["pinch_terminal"] == "boundary"
assert terminal_feature["properties"]["still_narrowing_at_termination"] is True
assert terminal_feature["properties"]["width_profile_min_m"] == terminal_zone["pinch"]["width_profile_min_m"]
assert wsa.FLAG_PINCH_AT_BOUNDARY in terminal_feature["properties"]["flags"]
print(
    f"   Terminal rank-1: the boundary-cut walk yields a {terminal_zone['zone_acres']} ac compartment "
    "with pinch_at_boundary + still_narrowing_at_termination, selected, contract intact, disclosure "
    "on the narrative block and the wire."
)

# --- 7 [7]. excavated regression: byte-identical roadless, clipped+flagged with a road ---

# The flat wet fixture from the excavated arc: uniform ground + best-wet
# soil -> one excavated zone, zero embankment seeds (the embankment
# blend cannot reach 0.5 on dead-flat ground), so the excavated pipeline
# is isolated.
FLAT_DEM = _dem(np.full((20, 20), 100.0))
FLAT_BOUNDARY = box(
    ORIGIN_X + 5 * RESOLUTION + 0.1,
    ORIGIN_Y - 15 * RESOLUTION + 0.1,
    ORIGIN_X + 15 * RESOLUTION - 0.1,
    ORIGIN_Y - 5 * RESOLUTION - 0.1,
)
WET_SOIL = {
    "ksat_rows": [{"mukey": "1", "ksat_r": 0.05}],
    "components": [{"mukey": "1", "hydricrating": "Yes", "comppct_r": 100, "hydgrp": "D"}],
    "geometries_by_mukey": {
        "1": transform_geom(CRS, "EPSG:4326", mapping(FLAT_BOUNDARY.buffer(30.0))),
    },
}


def _excavated_wire(result):
    """The excavated output as one JSON string -- the byte-identity
    instrument. road_overlap_pct is excluded (None vs 0.0 is the
    sentinel distinction between never-checked and checked-clean, a
    reported property, not geometry)."""
    features = []
    for feature in wsa.survey_areas_to_geojson(result["zones"])["features"]:
        properties = {k: v for k, v in feature["properties"].items() if k != "road_overlap_pct"}
        features.append({"id": feature["id"], "geometry": feature["geometry"], "properties": properties})
    return json.dumps(features, sort_keys=True)


unchecked = compute_water_survey_areas(FLAT_DEM, FLAT_BOUNDARY, soil_inputs=WET_SOIL)
checked_clean = compute_water_survey_areas(
    FLAT_DEM, FLAT_BOUNDARY, soil_inputs=WET_SOIL, road_exclusion_union_utm=None
)
far_road = box(ORIGIN_X + 90 * RESOLUTION, ORIGIN_Y - 95 * RESOLUTION, ORIGIN_X + 95 * RESOLUTION, ORIGIN_Y - 90 * RESOLUTION)
checked_far = compute_water_survey_areas(
    FLAT_DEM, FLAT_BOUNDARY, soil_inputs=WET_SOIL, road_exclusion_union_utm=far_road
)
assert unchecked["embankment_seeds"] == [] and unchecked["zones_by_type"][SURVEY_TYPE_EMBANKMENT] == []
assert _excavated_wire(unchecked) == _excavated_wire(checked_clean) == _excavated_wire(checked_far), (
    "BYTE-IDENTICAL excavated output on a roadless fixture -- road unchecked, checked-clean, and a "
    "road far away all produce the same wire bytes (road_overlap_pct's sentinel aside)"
)
assert unchecked["zones"][0]["road_overlap_pct"] is None, "never checked stays None"
assert checked_clean["zones"][0]["road_overlap_pct"] == 0.0, "checked-and-clean stays 0.0"
assert checked_far["zones"][0]["truncated_by_road"] is False

# The road fixture: a strip across the excavated envelope -> the hull
# clips at it exactly as at the boundary, flagged, and road_overlap_pct
# measures the PRE-clip envelope (the removed share), never a
# guaranteed zero.
crossing_road = box(
    ORIGIN_X + 9 * RESOLUTION, ORIGIN_Y - 15 * RESOLUTION, ORIGIN_X + 11 * RESOLUTION, ORIGIN_Y - 5 * RESOLUTION
)
road_result = compute_water_survey_areas(
    FLAT_DEM, FLAT_BOUNDARY, soil_inputs=WET_SOIL, road_exclusion_union_utm=crossing_road
)
road_zone = road_result["zones_by_type"][SURVEY_TYPE_EXCAVATED][0]
assert road_zone["truncated_by_road"] is True and wsa.FLAG_TRUNCATED_BY_ROAD in road_zone["flags"]
assert road_zone["polygon_utm"].intersection(crossing_road).area < 1e-6, (
    "the excavated hull now clips at roads exactly as it clips at the boundary"
)
assert road_zone["zone_acres"] < checked_clean["zones"][0]["zone_acres"], "the clip removed real acreage"
assert road_zone["road_overlap_pct"] is not None and road_zone["road_overlap_pct"] > 0.0, (
    f"road_overlap_pct measures the PRE-clip envelope (the removed share), got {road_zone['road_overlap_pct']}"
)
assert road_zone["render_fill_polygon_utm"] is road_zone["polygon_utm"]
print(
    f"7. Excavated regression: roadless output byte-identical across three road postures; a crossing "
    f"road clips the hull (flagged, {road_zone['road_overlap_pct']}% of the pre-clip claim removed)."
)

# --- 8 [9]. export validation: the new layers, linkage, stored wire forms ---

import diagnose_water_survey_areas as diag  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402

identify_like = {
    "zones": a_result["zones"],
    "zones_by_type": a_result["zones_by_type"],
    "dropped_zones": a_result["dropped_zones"],
    "regions": a_result["regions"],
    "regions_by_type": a_result["regions_by_type"],
    "embankment_seeds": a_result["embankment_seeds"],
    "gate_mask_stats": a_result["gate_mask_stats"],
    "result": a_result,
}
isobands_by_type = {
    survey_type: diag.compute_suitability_isobands(A2_DEM, a_result["surfaces"][survey_type])
    for survey_type in (SURVEY_TYPE_EMBANKMENT, SURVEY_TYPE_EXCAVATED)
}
boundary_wgs84 = transform_geom(CRS, "EPSG:4326", mapping(A_BOUNDARY))
boundary_coords = [tuple(point) for point in boundary_wgs84["coordinates"][0]]
export_dir = tempfile.mkdtemp(prefix="embankment_compartments_test_")
export_path = os.path.join(export_dir, "export.geojson")
export = diag.export_water_survey_areas_geojson(
    identify_like, boundary_coords, [], isobands_by_type, path=export_path
)
with open(export_path, encoding="utf-8") as handle:
    collection = json.load(handle)
validate_feature_collection(collection)
for feature in collection["features"]:
    shape(feature["geometry"])
by_layer = export["by_layer"]
surviving_comps = len(a_comps)
dropped_comps = len([z for z in a_result["dropped_zones"] if z["survey_type"] == SURVEY_TYPE_EMBANKMENT])
total_comps = surviving_comps + dropped_comps
failed_seed_count = len(a_failed)
assert by_layer.get("embankment_seed", 0) == total_comps, by_layer
assert by_layer.get("embankment_pinch", 0) == total_comps
assert by_layer.get("embankment_baseline", 0) == total_comps
assert by_layer.get("embankment_transect", 0) == 2 * total_comps, "two transects per compartment"
assert by_layer.get("embankment_seed_failed", 0) == failed_seed_count
assert by_layer.get("survey_zone_embankment", 0) == surviving_comps
assert by_layer.get("survey_zone_member_embankment", 0) == 0, (
    "the compartment polygons REPLACE the embankment zone/member layers -- no member features exist"
)
# Linkage: every instrument feature points at its compartment; failed
# seeds carry their reason codes on the wire.
zone_ids = {z["id"] for z in a_result["zones"] + a_result["dropped_zones"] if z["survey_type"] == SURVEY_TYPE_EMBANKMENT}
for feature in collection["features"]:
    layer = feature["properties"]["layer"]
    if layer in ("embankment_seed", "embankment_pinch", "embankment_baseline", "embankment_transect"):
        assert feature["properties"]["zone_id"] in zone_ids, f"{layer} links a real compartment"
    if layer == "embankment_seed_failed":
        assert feature["properties"]["reason_code"], "every failed seed ships its reason"
# Stored wire forms by IDENTITY on the pipeline-facing collection (the
# exported file is parsed JSON, so identity is asserted on the builder):
from wire_translation import water_embankment_detail_features  # noqa: E402

detail = water_embankment_detail_features(a_result["zones"], a_result["embankment_seeds"])
for feature in detail:
    layer = feature["properties"]["layer"]
    zone = next((z for z in a_result["zones"] if z["survey_type"] == SURVEY_TYPE_EMBANKMENT and z["id"] == feature["properties"].get("zone_id")), None)
    if zone is None:
        continue
    if layer == "embankment_baseline":
        assert feature["geometry"] is zone["baseline"]["geometry_wgs84"], "stored wire form, by identity"
    if layer == "embankment_pinch":
        assert feature["geometry"] is zone["pinch"]["geometry_wgs84"]
    if layer == "embankment_seed":
        assert feature["geometry"] is zone["seed"]["geometry_wgs84"]
print(
    f"8. Export: {export['feature_count']} features validate; layers embankment_seed/{'pinch'}/"
    f"baseline/transect at {total_comps}/{total_comps}/{total_comps}/{2 * total_comps} plus "
    f"{failed_seed_count} failed seeds with reason codes; no member layer exists for the type; "
    "stored wire forms ride by identity."
)

print("\nAll embankment compartment checks passed.")
