"""
diagnose_road_network_quantity.py

THE SWEEP BEHIND MAX_ROAD_METERS_PER_SERVED_ACRE = 125.0, kept
runnable. Fully offline -- synthetic DEMs only, no fetches -- so the
measurement that set the ceiling can be re-made after any change to
the router, the cost surface, or the constants, and compared against
the tables recorded below.

WHAT IT MEASURES. road_network_router.route_road_network() run over
four synthetic terrains x four parcel sizes (10/25/50/100 acres,
square parcels, 5 m grid -- dem_data.DEFAULT_RESOLUTION_METERS), plus
a "remote" fixture (flat parcel whose only demand is a small block in
the far corner -- the shape that historically produced ZERO road).
Demand is every on-parcel cell at or below production_area.
MAX_PRODUCTION_SLOPE_PCT (20%), the same gate the real pipeline's
production areas pass; the cost raster is assembled exactly the way
road_corridors.build_road_network() assembles it (slope + TPI +
production multiplier + 35% cliff cutoff, boundary hard-excluded).
Terrains:

    flat     dead flat; demand wall-to-wall.
    rolling  gentle swells, slopes 0-12%; demand near wall-to-wall.
    steep    15% hillside with cross-corrugation, slopes 15-23%;
             demand in the downslope bands at or under 20%, the
             corridors between them steeper than demand allows.
    mixed    flat bottom, a ~22% climb band, flat bench on top --
             the road must buy the bench crossing non-demand ground.

WHAT THE 2026-10 SWEEP FOUND (router at the per-candidate-filter +
guaranteed-trunk algorithm, radius 50 m, leaf pruning 50 m; lengths in
meters of NEW construction, serv% of reachable demand acreage,
branches post-pruning):

1. THE OLD STOP RULE HAD NO WORKABLE CEILING. Stop-at-first-violation
   against the same terrains: every ceiling at or under 150 collapsed
   steep parcels to near-nothing (steep-100ac at ceiling 150: 2
   branches accepted, 133 branches that individually satisfied the
   ceiling cut off behind one over-ceiling stub; the remote fixtures:
   0 m at every ceiling under 200) while every ceiling at or over 200
   swung to near-full coverage (93-99% served). The acceptance
   trajectory's meters-per-acre is NOT monotone -- accepting a branch
   rewrites the cost field, so cheap branches regularly become
   selectable only after expensive ones -- and a first-violation stop
   is therefore an accident of ordering, not a policy. That is the
   measured root cause of BOTH "ceiling 100 built nothing" and
   "ceiling 250 builds too much".

2. UNDER PER-CANDIDATE FILTERING THE CEILING IS SMOOTH, and 125 is
   its elbow. Ceiling 75 -> 250 moves total length only modestly and
   monotonically (steep-50ac: 1490 -> 1865 m; flat-50ac: 1950 ->
   2041 m) -- no collapse, no explosion, every terrain, every size.
   Below ~125, real collector corridors drop out (flat-100ac at 100:
   served 88%; at 125: 97%). Above 125, the added road is
   nook-chasing: +2-5% served for +10-25% length and ~2x the branch
   count (mixed-100ac: 25 branches/91% served at 125 vs 55/96% at
   250). At 125 every swept parcel serves 82-97% of reachable demand
   at 38-73 m per served acre -- a skeleton, with the expensive tail
   left for the person to decide on.

3. THE GUARANTEED TRUNK ENDS THE ZERO-NETWORK FAILURE. The remote
   fixtures (1-3 acres of demand 150-500 m from the anchor, trunk at
   165-177 m per acre -- over ANY sane ceiling) now produce exactly
   the one access road a person would draw first, at every ceiling:
   90-97% of the block served by 1-2 branches.

4. RADIUS IS THE QUANTITY LEVER, NOT THE CEILING, on broad demand.
   Full coverage of flat wall-to-wall demand measures within ~10% of
   area / (2 * radius) -- strip coverage -- so 50 flat acres full-
   covered takes 4161 m at 25 m radius, 2238 m at 50 m, 1478 m at
   75 m, whatever the ceiling does. ~40 m of road per served acre AT
   50 M RADIUS IS GEOMETRY; a parcel that reads as "too much road" at
   a sane ceiling is asking for a bigger service radius (75 m serves
   96-98% at ~27-33 m/acre on the same parcels, at the price of a
   longer in-field haul and a larger no-road zone around the anchor),
   not a lower ceiling. 25 m doubles road on flat ground AND
   under-serves steep parcels (its thin per-meter coverage cannot pay
   for the detours). PRODUCTION_SERVICE_RADIUS_METERS has since been
   moved 50 -> 75 as a product decision riding exactly this trade --
   see that constant's own comment for the chosen balance and for the
   measured small-parcel price (on a square 10-acre parcel with a
   mid-edge access point, 75 m is the practical ceiling; the network
   is a token stub chain by ~100 m and correctly zero at ~215-220 m,
   the farthest-demand-cell distance, via "all_demand_served").

RUNTIME. The default matrix (10/25-acre parcels) runs in roughly a
minute. --full adds the 50/100-acre parcels and takes tens of
minutes -- the 100-acre full-coverage reference runs are the slow
part. This script asserts nothing: it reports, and the judgement
stays in MAX_ROAD_METERS_PER_SERVED_ACRE's own comment.
"""

import math
import sys
import time

import numpy as np

from production_area import MAX_PRODUCTION_SLOPE_PCT
from raster_grid import SQUARE_METERS_PER_ACRE, cell_area_acres
from road_corridors import MAX_ROAD_GRADE_PCT
from road_cost_path import build_cost_raster
from road_network_router import (
    MIN_LEAF_BRANCH_METERS,
    PRODUCTION_SERVICE_RADIUS_METERS,
    route_road_network,
)
from terrain_metrics import compute_slope_and_aspect
from topographic_position import compute_tpi

RESOLUTION_METERS = 5.0  # == dem_data.DEFAULT_RESOLUTION_METERS
MARGIN_CELLS = 3  # off-parcel ring, hard-excluded like real boundary clipping

CEILINGS = [75.0, 100.0, 125.0, 150.0, 200.0, 250.0]


def _smooth_ramp(y: float, y0: float, y1: float, grade: float, s: float = 15.0) -> float:
    """Flat, then a `grade` climb from y0 to y1, then flat -- with s-meter
    eased ends so the Horn slope window never sees a fake spike."""
    def g(t: float) -> float:
        if t < y0 - s or t > y1 + s:
            return 0.0
        if t < y0 + s:
            return grade * (t - (y0 - s)) / (2 * s)
        if t > y1 - s:
            return grade * ((y1 + s) - t) / (2 * s)
        return grade

    total, t = 0.0, y0 - s
    while t < y:
        step = min(1.0, y - t)
        total += g(t + step / 2) * step
        t += step
    return total


def _terrain_elevation(terrain: str, side_m: float):
    if terrain == "flat":
        return lambda x, y: 0.0
    if terrain == "rolling":
        return lambda x, y: 2.5 * math.sin(2 * math.pi * x / 160.0) * math.cos(2 * math.pi * y / 180.0)
    if terrain == "steep":
        return lambda x, y: 0.15 * y + 4.0 * math.sin(2 * math.pi * x / 140.0)
    if terrain == "mixed":
        return lambda x, y: _smooth_ramp(y, 0.42 * side_m, 0.70 * side_m, 0.22)
    raise ValueError(f"unknown terrain {terrain!r}")


def _make_dem(acres: float, elev_fn):
    side_m = math.sqrt(acres * SQUARE_METERS_PER_ACRE)
    n_parcel = int(round(side_m / RESOLUTION_METERS))
    n = n_parcel + 2 * MARGIN_CELLS
    array = np.zeros((n, n), dtype=np.float32)
    for r in range(n):
        for c in range(n):
            x = (c - MARGIN_CELLS + 0.5) * RESOLUTION_METERS
            y = (r - MARGIN_CELLS + 0.5) * RESOLUTION_METERS
            array[r, c] = elev_fn(x, y)
    dem = {
        "array": array,
        "resolution_meters": (RESOLUTION_METERS, RESOLUTION_METERS),
        "origin_x": 500000.0,
        "origin_y": 4500000.0,
        "crs": "EPSG:32617",
    }
    parcel = np.zeros((n, n), dtype=bool)
    parcel[MARGIN_CELLS : n - MARGIN_CELLS, MARGIN_CELLS : n - MARGIN_CELLS] = True
    return dem, parcel


def build_fixture(terrain: str, acres: float):
    """DEM + cost raster + west-edge anchor + demand mask, assembled the
    way road_corridors.build_road_network() assembles the real thing."""
    side_m = math.sqrt(acres * SQUARE_METERS_PER_ACRE)
    dem, parcel = _make_dem(acres, _terrain_elevation(terrain, side_m))
    slope_pct, _aspect = compute_slope_and_aspect(dem["array"], dem["resolution_meters"])
    tpi = compute_tpi(dem)
    demand = parcel & ~np.isnan(slope_pct) & (slope_pct <= MAX_PRODUCTION_SLOPE_PCT)
    cost = build_cost_raster(
        dem, slope_pct, ~parcel,
        tpi=tpi, production_mask=demand, impassable_grade_pct=MAX_ROAD_GRADE_PCT,
    )
    anchor = (dem["array"].shape[0] // 2, MARGIN_CELLS)
    return dem, cost, anchor, demand


def build_remote_fixture(acres: float, demand_acres: float):
    """Flat parcel whose ONLY demand is a small block in the far corner --
    the first branch legitimately costs the most meters per acre of any
    branch shape, which is exactly what used to zero these parcels out."""
    dem, parcel = _make_dem(acres, lambda x, y: 0.0)
    slope_pct, _aspect = compute_slope_and_aspect(dem["array"], dem["resolution_meters"])
    n = dem["array"].shape[0]
    block_side = max(1, int(round(math.sqrt(demand_acres / cell_area_acres(dem)))))
    demand = np.zeros_like(parcel)
    demand[n - MARGIN_CELLS - block_side : n - MARGIN_CELLS, n - MARGIN_CELLS - block_side : n - MARGIN_CELLS] = True
    demand &= parcel
    cost = build_cost_raster(dem, slope_pct, ~parcel, production_mask=demand, impassable_grade_pct=MAX_ROAD_GRADE_PCT)
    anchor = (n // 2, MARGIN_CELLS)
    return dem, cost, anchor, demand


def run_one(fixture, radius: float, ceiling: float) -> dict:
    dem, cost, anchor, demand = fixture
    started = time.time()
    result = route_road_network(
        dem, cost, anchor, demand,
        water_target_cells=None,
        service_radius_meters=radius,
        max_meters_per_served_acre=ceiling,
        min_leaf_branch_meters=MIN_LEAF_BRANCH_METERS,
    )
    demand_acres = float(demand.sum()) * cell_area_acres(dem)
    served = result["total_served_acres"]
    return {
        "elapsed_s": time.time() - started,
        "length_m": result["total_length_meters"],
        "served_acres": served,
        "served_pct": 100.0 * served / demand_acres if demand_acres > 0 else float("nan"),
        "m_per_served_acre": result["total_length_meters"] / served if served > 0 else float("nan"),
        "branches": len(result["branches"]),
        "stop_reason": result["stop_reason"],
    }


def main() -> None:
    full = "--full" in sys.argv
    sizes = [10, 25, 50, 100] if full else [10, 25]
    radius = PRODUCTION_SERVICE_RADIUS_METERS

    print(f"radius {radius:.0f} m, leaf pruning {MIN_LEAF_BRANCH_METERS:.0f} m, "
          f"demand gate slope <= {MAX_PRODUCTION_SLOPE_PCT:.0f}%  "
          f"({'full matrix' if full else 'quick matrix; --full adds 50/100 acres'})")
    header = f"{'parcel':>16} |" + "".join(f"{int(c):>21} |" for c in CEILINGS)
    print(header + "   (len | m/served-ac | served% | branches)")

    for terrain in ["flat", "rolling", "steep", "mixed"]:
        for acres in sizes:
            fixture = build_fixture(terrain, acres)
            row = f"{terrain + '-' + str(acres) + 'ac':>16} |"
            for ceiling in CEILINGS:
                m = run_one(fixture, radius, ceiling)
                row += (f" {m['length_m']:5.0f}m {m['m_per_served_acre']:4.0f} "
                        f"{m['served_pct']:3.0f}% {m['branches']:3d}b |")
            print(row, flush=True)

    for acres, demand_acres in [(10, 1.0), (25, 2.0)] + ([(50, 3.0)] if full else []):
        fixture = build_remote_fixture(acres, demand_acres)
        row = f"{'remote-' + str(acres) + 'ac':>16} |"
        for ceiling in CEILINGS:
            m = run_one(fixture, radius, ceiling)
            row += (f" {m['length_m']:5.0f}m {m['m_per_served_acre']:4.0f} "
                    f"{m['served_pct']:3.0f}% {m['branches']:3d}b |")
        print(row, flush=True)

    print("\nReport only -- the reading of these tables lives in "
          "MAX_ROAD_METERS_PER_SERVED_ACRE's own comment and this script's docstring.")


if __name__ == "__main__":
    main()
