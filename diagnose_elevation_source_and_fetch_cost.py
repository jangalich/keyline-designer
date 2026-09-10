"""
diagnose_elevation_source_and_fetch_cost.py

THE LIVE HALF OF THE ELEVATION-LAYER BRANCH. Two measurements this repo's
offline tests structurally cannot make, because both need real answers
from real USGS services:

  1. THE TWO SOURCES, COMPARED. The min and max the report USED to quote
     -- elevation_data.get_elevation_grid(boundary, grid_size=6), 36 EPQS
     point samples over the boundary's bounding box -- against the min and
     max of the 3DEP DEM raster over the same boundary, masked to it. Both
     are 3DEP, but they are different services (the EPQS point query vs the
     3DEPElevation ImageServer's resampled raster), so they are not
     guaranteed to agree, and the report's numbers change by whatever the
     gap is. Printed in meters and feet, along with the two report
     sentences side by side, so the change to the report is a stated
     consequence rather than a surprise.

  2. THE COLD-CREATION TOTAL, BEFORE AND AFTER. One real
     fetch_parcel_data() on the reference parcel, timed per layer through
     run_diagnostics' own timers, run twice: once as this branch leaves it
     (TWELVE layers) and once with the elevation lattice fetched alongside
     them the way the thirteenth layer used to be. The second run's extra
     wait IS the layer this branch deleted, measured rather than estimated.

Run:  python diagnose_elevation_source_and_fetch_cost.py

REQUIRES REAL NETWORK ACCESS to epqs.nationalmap.gov, elevation.national
map.gov, sdmdataaccess.sc.egov.usda.gov, hydro.nationalmap.gov, overpass
-api.de and planetarycomputer.microsoft.com -- like every other network-
backed module in this repo, it will not run in a sandbox whose egress
policy blocks them, and it says so plainly rather than inventing numbers.
Measurement 1 alone needs only the two elevation hosts; pass --sources-only
to run just that half.

NOTHING HERE IS ON THE PIPELINE PATH. This is a measurement harness, in
the same standalone-diagnostic family as diagnose_pipeline_redundant_
fetches.py: it imports the real modules and calls them, and no pipeline
module imports it. It is also the reason elevation_data.py is DEMOTED
rather than deleted -- see that module's own docstring.
"""

import sys
import time

from shapely.geometry import Polygon

from dem_data import get_dem_for_boundary, summarize_dem
from elevation_data import get_elevation_grid, summarize_elevation_grid
from parcel_data import FETCH_LAYERS, fetch_parcel_data
from raster_grid import elevation_range_in_polygon
from report_generator import _format_elevation_summary

# The reference parcel -- the same real drawn boundary generate_full_
# report.py, dem_data.py, test_elevation_grid.py and test_session_manager.py
# all use (5614 N Montour Rd, Gibsonia, PA; ~13.23 acres, UTM 17N). Every
# timing quoted in this branch is this parcel's.
REFERENCE_PARCEL = [
    (-79.9838154, 40.6458343),
    (-79.9836701, 40.6428581),
    (-79.9813665, 40.6440549),
    (-79.9804741, 40.6445667),
    (-79.9827466, 40.6458894),
    (-79.9838258, 40.6458343),
]

FEET_PER_METER = 1.0 / 0.3048


def _boundary_polygon_utm(boundary_coordinates, dem):
    """parcel_data._boundary_polygon_utm()'s own expression, reached
    through the public module rather than copied -- imported lazily so the
    import cost lands inside the measurement's own section."""
    import parcel_data

    return parcel_data._boundary_polygon_utm(boundary_coordinates, dem)


def compare_sources(boundary):
    """MEASUREMENT 1. Both elevation answers for one boundary, and the gap."""
    print("=" * 72)
    print("1. THE TWO SOURCES, COMPARED -- reference parcel")
    print("=" * 72)

    started = time.perf_counter()
    dem = get_dem_for_boundary(boundary)
    dem_seconds = time.perf_counter() - started
    print(f"\n  DEM fetched in {dem_seconds:.1f} s")
    print(f"  {summarize_dem(dem)}")

    polygon_utm = _boundary_polygon_utm(boundary, dem)
    dem_answer = elevation_range_in_polygon(dem, polygon_utm)
    if not dem_answer:
        print("\n  The DEM has no data inside this boundary -- nothing to compare.")
        return None

    started = time.perf_counter()
    grid = get_elevation_grid(boundary, grid_size=6)
    grid_seconds = time.perf_counter() - started
    print(f"\n  36-point EPQS lattice fetched in {grid_seconds:.1f} s ({len(grid)} points returned)")
    print(f"  {summarize_elevation_grid(grid)}")

    if not grid:
        print("\n  The lattice returned no points -- nothing to compare.")
        return None

    grid_elevations = [point["elevation"] for point in grid]
    grid_min, grid_max = min(grid_elevations), max(grid_elevations)
    dem_min, dem_max = dem_answer["min_meters"], dem_answer["max_meters"]

    min_gap = dem_min - grid_min
    max_gap = dem_max - grid_max

    print("\n  ----------------------------------------------------------------")
    print(f"  {'':<26}{'min (m)':>10}{'max (m)':>10}{'relief (m)':>12}{'n':>8}")
    print(
        f"  {'EPQS 36-point lattice':<26}{grid_min:>10.2f}{grid_max:>10.2f}"
        f"{grid_max - grid_min:>12.2f}{len(grid):>8}"
    )
    print(
        f"  {'3DEP DEM, masked':<26}{dem_min:>10.2f}{dem_max:>10.2f}"
        f"{dem_answer['relief_meters']:>12.2f}{dem_answer['cell_count']:>8}"
    )
    print(
        f"  {'DIFFERENCE (DEM - EPQS)':<26}{min_gap:>+10.2f}{max_gap:>+10.2f}"
        f"{(dem_answer['relief_meters'] - (grid_max - grid_min)):>+12.2f}"
    )
    print(
        f"  {'  in feet':<26}{min_gap * FEET_PER_METER:>+10.1f}"
        f"{max_gap * FEET_PER_METER:>+10.1f}"
        f"{(dem_answer['relief_meters'] - (grid_max - grid_min)) * FEET_PER_METER:>+12.1f}"
    )
    print("  ----------------------------------------------------------------")

    # THE REPORT'S OWN SENTENCE, BOTH WAYS. What actually changes in the
    # delivered report is these two lines, so print them rather than
    # leaving a reader to work it out from the table.
    old_min_ft = round(grid_min * FEET_PER_METER)
    old_max_ft = round(grid_max * FEET_PER_METER)
    print("\n  The report's ELEVATION sentence:")
    print(
        f"    BEFORE: Elevation range: {old_min_ft}ft to {old_max_ft}ft "
        f"(total relief: {old_max_ft - old_min_ft}ft) across {len(grid)} sample points."
    )
    print(f"    AFTER:  {_format_elevation_summary(dem_answer)}")

    print(
        f"\n  Fetch cost of those two numbers: {grid_seconds:.1f} s from the lattice, "
        f"0 s from the DEM (it was already fetched, for the terrain analysis)."
    )
    return {
        "grid_min": grid_min,
        "grid_max": grid_max,
        "dem_min": dem_min,
        "dem_max": dem_max,
        "grid_seconds": grid_seconds,
        "dem_seconds": dem_seconds,
    }


def _timed_fetch(boundary, with_lattice):
    """One real fetch_parcel_data(), wall-clocked. `with_lattice` adds the
    deleted thirteenth layer back ALONGSIDE it -- the same call with the
    same arguments fetch_parcel_data() used to make, in the same sequential
    position relative to the rest -- so the difference between the two
    totals is that layer and nothing else."""
    started = time.perf_counter()
    parcel = fetch_parcel_data(boundary)
    lattice_seconds = 0.0
    if with_lattice:
        lattice_started = time.perf_counter()
        get_elevation_grid(boundary, grid_size=6)
        lattice_seconds = time.perf_counter() - lattice_started
    total = time.perf_counter() - started
    return parcel, total, lattice_seconds


def measure_cold_creation(boundary, runs):
    """MEASUREMENT 2. Cold fetch totals, this branch vs the layer restored."""
    print("\n" + "=" * 72)
    print(f"2. COLD FETCH TOTAL, BEFORE AND AFTER -- reference parcel, {runs} run(s) each")
    print("=" * 72)
    print(
        f"\n  AFTER  = this branch as it stands: {len(FETCH_LAYERS)} layers.\n"
        f"  BEFORE = the same fetch plus the elevation lattice, the thirteenth\n"
        f"           layer this branch deleted, fetched with the same arguments.\n"
        f"  Each run is COLD: fetch_parcel_data() is called directly, so no\n"
        f"  fetch cache stands between these numbers and the network.\n"
    )

    rows = []
    for run in range(1, runs + 1):
        _, after_total, _ = _timed_fetch(boundary, with_lattice=False)
        _, before_total, lattice = _timed_fetch(boundary, with_lattice=True)
        rows.append((run, before_total, after_total, lattice))
        print(
            f"  run {run}:  BEFORE {before_total:>7.1f} s   AFTER {after_total:>7.1f} s   "
            f"(the lattice alone: {lattice:>6.1f} s, "
            f"{lattice / before_total:>5.1%} of BEFORE)"
        )

    before_avg = sum(row[1] for row in rows) / len(rows)
    after_avg = sum(row[2] for row in rows) / len(rows)
    lattice_avg = sum(row[3] for row in rows) / len(rows)
    print(
        f"\n  mean:   BEFORE {before_avg:>7.1f} s   AFTER {after_avg:>7.1f} s   "
        f"saved {before_avg - after_avg:>6.1f} s ({1 - after_avg / before_avg:.1%})"
    )
    print(f"  the deleted layer accounted for {lattice_avg:.1f} s of the BEFORE mean.")
    return rows


def main(argv):
    sources_only = "--sources-only" in argv
    runs = 3
    for arg in argv:
        if arg.startswith("--runs="):
            runs = int(arg.split("=", 1)[1])

    print("Reference parcel: 5614 N Montour Rd, Gibsonia, PA (~13.23 acres)\n")

    try:
        compare_sources(REFERENCE_PARCEL)
        if not sources_only:
            measure_cold_creation(REFERENCE_PARCEL, runs)
    except Exception as exc:  # noqa: BLE001 -- a diagnostic reports, it does not swallow
        print(f"\nMEASUREMENT DID NOT COMPLETE: {type(exc).__name__}: {exc}")
        print(
            "\nThis script needs real access to the USGS/USDA/Overpass/Planetary Computer\n"
            "hosts listed in the module docstring. In an environment whose egress policy\n"
            "blocks them there is nothing to measure here -- run it on a machine with real\n"
            "network access rather than substituting an estimate."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
