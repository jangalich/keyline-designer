#!/usr/bin/env python3
"""
make_climate_fixtures.py

CAPTURES THE CLIMATE SECTION'S FIXTURES FROM THE LIVE SERVICES, for the
reference parcel -- the ONE place these fetches run against the network
outside a report job. Run by hand when a fixture must be refreshed (a
new Daymet version, a new normals archive, a moved endpoint); the tests
never do.

    python3 make_climate_fixtures.py [--only atlas14|power|stations]

What it writes, each a verbatim service response:

    atlas14_reference_fixture.csv          Atlas 14 labelled CSV, partial
                                           duration, inches (1.6 KB)
    power_wind_reference_fixture.csv       POWER daily WS10M/WD10M for the
                                           Daymet fixture's years (225 KB)
    daymet_station_fixtures/<id>.csv       Daymet prcp 1991-2020 at each of
                                           the five nearest normals stations
                                           (150 KB each)

The Daymet fixture at the parcel (daymet_reference_fixture.csv) is not
refreshed here: the station fixtures follow the station list the
normals bundle yields for the parcel, and the POWER window follows the
years in the Daymet fixture, so the parcel fixture is the anchor the
others are captured against.
"""

import os
import sys

import atlas14_data
import daymet_data
import power_wind_data
import precipitation_normals
from reference_fixture import REAL_BOUNDARY
from report_data import boundary_centroid_lat_lon

STATION_FIXTURE_DIR = "daymet_station_fixtures"


def main(argv) -> int:
    only = argv[argv.index("--only") + 1] if "--only" in argv else None
    lat, lon = boundary_centroid_lat_lon(REAL_BOUNDARY)
    with open("daymet_reference_fixture.csv", encoding="utf-8") as handle:
        daily = daymet_data.parse_daymet_csv(handle.read())
    # The fixtures were captured at the hand-picked point the Daymet
    # fixture names, inside the same 1 km cell as the polygon centroid.
    lat, lon = daily["latitude"], daily["longitude"]
    print(f"reference point {lat}, {lon}; Daymet years {daily['years'][0]}-{daily['years'][-1]}")

    if only in (None, "atlas14"):
        text = atlas14_data._request_csv(lat, lon, atlas14_data.SERIES_PARTIAL_DURATION, "english", 2)
        atlas14_data.parse_atlas14_csv(text)
        with open("atlas14_reference_fixture.csv", "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"atlas14_reference_fixture.csv: {len(text)} bytes")

    if only in (None, "power"):
        text = power_wind_data._request_csv(lat, lon, daily["years"][0], daily["years"][-1], 2)
        parsed = power_wind_data.parse_power_csv(text)
        assert parsed["years"] == daily["years"], (parsed["years"][0], parsed["years"][-1])
        with open("power_wind_reference_fixture.csv", "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"power_wind_reference_fixture.csv: {len(text)} bytes, {parsed['row_count']} rows")

    if only in (None, "stations"):
        os.makedirs(STATION_FIXTURE_DIR, exist_ok=True)
        for station in precipitation_normals.nearest_stations(lat, lon):
            text = daymet_data._request_csv(
                station["latitude"], station["longitude"], precipitation_normals.NORMALS_YEARS, 2, ("prcp",)
            )
            parsed = daymet_data.parse_daymet_csv(text, required_variables=("prcp",))
            missing = daymet_data.incomplete_years(parsed, precipitation_normals.NORMALS_YEARS)
            assert not missing, (station["station"], missing)
            path = os.path.join(STATION_FIXTURE_DIR, station["station"] + ".csv")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            print(f"{path}: {len(text)} bytes, {station['name']} at {station['distance_miles']:.1f} mi")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
