"""
main.py

Ties the pipeline together end to end:

    address (typed in) --> geocode.py --> lat/long --> soil_data.py --> report

This is the first full "vertical slice" of the tool: type in any US address,
get back a real soil report for that location. Elevation, hydrology, and
imagery will plug into this same pattern as they're built.

Run it with:
    python main.py

Then type any US address when prompted.
"""

from geocode import geocode_address
from soil_data import get_soil_data_for_point, summarize_soil_report


def run_report_for_address(address: str) -> None:
    print(f"\nLooking up: {address}")
    print("-" * 50)

    location = geocode_address(address)

    if location is None:
        print("Couldn't find that address. Try including city and state,")
        print("or double-check for typos.")
        return

    print(f"Matched to: {location['matched_address']}")
    print(f"Coordinates: {location['latitude']}, {location['longitude']}\n")

    print("Fetching soil survey data...\n")

    components = get_soil_data_for_point(
        location["latitude"], location["longitude"]
    )

    print(summarize_soil_report(components))


if __name__ == "__main__":
    user_address = input("Enter a US address to analyze: ").strip()

    if not user_address:
        print("No address entered — using the example property instead.")
        user_address = "5614 N Montour Rd, Gibsonia, PA 15044"

    run_report_for_address(user_address)
