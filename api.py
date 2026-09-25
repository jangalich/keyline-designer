"""
api.py

The web API the frontend (running in a browser) calls: the geocoder, the
production-zone spike, the health check, and the interactive session
surface below.

THE BATCH REPORT ROUTES ARE GONE. /api/generate-report and /api/generate-
report-pdf ran generate_full_report.py's narrated report over a bare
boundary; both were retired with it, and the frontend called neither. The
report a user receives is the site data report, produced by the session
surface's POST /api/sessions/<sid>/report (session_report.py).

THE INTERACTIVE SESSION SURFACE mounts here too, as session_api.py's
blueprint: /api/sessions, its per-step generate/commit/reopen/layers verbs,
and /api/jobs. It is a separate module because it is a separate surface with
its own conventions, and it is registered on THIS app so both live behind
one origin and one CORS policy. The endpoints below are untouched by it --
/api/production-zones in particular is what the shipped frontend calls
today, and it keeps working exactly as it did until the frontend migrates.

The session surface's security posture -- unguessable session ids, no auth,
no rate limiting, an accepted v1 position rather than an oversight -- and
the ephemeral-filesystem question its document store raises are both written
up in session_api.py's module docstring. Read it before deploying.

Run locally with:
    python3 api.py

Then it's reachable at http://localhost:5000
"""

import re

from flask import Flask, request, jsonify
from flask_cors import CORS

import session_api
from geocode import geocode_address
from production_zone_payload import (
    LayerFetchError,
    build_production_zone_payload,
)

app = Flask(__name__)


def _parse_boundary(data: dict) -> list:
    """
    Pulls and validates the required 'boundary' field, raising ValueError
    with a caller-facing message; the caller turns it into a 400.
    """
    if not data or "boundary" not in data:
        raise ValueError("Request must include a 'boundary' field.")

    boundary = data["boundary"]

    if not isinstance(boundary, list) or len(boundary) < 3:
        raise ValueError("Boundary must be a list of at least 3 [lon, lat] points.")

    return boundary


# CORS (Cross-Origin Resource Sharing) lets the frontend call this API
# from a different origin. Browsers block cross-origin requests by
# default unless the server explicitly allows it. Restricted to the
# deployed frontend's real origin, the local Vite dev server, and
# per-branch/commit Vercel preview URLs for this project (Vercel mints a
# new one on every deploy, so those can only be matched by pattern, not
# listed individually) -- not left wide open to any *.vercel.app site.
CORS(app, origins=[
    "https://keyline-designer-frontend.vercel.app",
    "http://localhost:5173",
    re.compile(r"^https://keyline-designer-frontend-[a-z0-9-]+\.vercel\.app$"),
])


@app.route("/api/geocode", methods=["POST"])
def geocode_endpoint():
    """
    Expects a JSON body like:
        { "address": "5614 N Montour Rd, Gibsonia, PA 15044" }

    Returns:
        { "latitude": ..., "longitude": ..., "matched_address": "..." }
        on success, or { "error": "..." } if no match was found.
    """
    data = request.get_json(silent=True)

    if not data or "address" not in data:
        return jsonify({"error": "Request must include an 'address' field."}), 400

    address = data["address"].strip()

    if not address:
        return jsonify({"error": "Address cannot be empty."}), 400

    try:
        result = geocode_address(address)

        if result is None:
            return jsonify({"error": "No match found for that address. Check for typos, or try including city and state."}), 404

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": f"Geocoding failed: {str(e)}"}), 500


@app.route("/api/production-zones", methods=["POST"])
def production_zones_endpoint():
    """
    Expects a JSON body like:
        { "boundary": [[lon, lat], [lon, lat], ...] }

    NO 'access_point'. Nothing on this path routes a road, so
    requiring the anchor point road routing starts from would be demanding a
    decision the user has not been asked to make yet. This step runs on a
    traced boundary alone.

    Returns the production-zone payload -- see production_zone_payload.
    build_production_zone_payload() for the full shape -- or, on a hard
    failure of one upstream layer:

        { "error": "...", "failed_layer": { "type": ..., "label": ... } }

    with a 502. `failed_layer.type` is the stable identifier a client
    branches on; `label` is display prose. NOTE the deliberate departure
    from the other endpoints in this file: they pass str(e) straight
    through into "error", and this one does not. A user looking at a panel
    that says their zones could not be generated is not helped by a
    rasterio traceback or a STAC status code, and the layer identity is the
    only part of the failure they can act on (wait and retry, or accept
    that this parcel has no canopy coverage from either source). The
    exception is logged server-side instead.

    `failed_layer.reason` is present when the raise site knew which kind of
    failure it was: "source_unavailable" (the source did not answer -- a
    retry is reasonable) or "no_data_for_parcel" (the source answered and
    has nothing for this land -- permanent for this boundary, and a retry
    is wasted). The two used to be indistinguishable on the wire and in the
    sentence, which is how a user ended up retrying a permanent gap.

    502 rather than 500: the request was well-formed and this service did
    nothing wrong -- an upstream data source (USGS 3DEP, Microsoft
    Planetary Computer) did not answer. These sources are known to be
    intermittent, so this is a recurring state rather than an edge case,
    and it is worth telling a client that a retry of the identical request
    is a reasonable next move.
    """
    data = request.get_json(silent=True)

    try:
        boundary = _parse_boundary(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        payload = build_production_zone_payload(boundary)
        return jsonify(payload)

    except LayerFetchError as e:
        app.logger.exception("production-zones: %s layer failed", e.layer)
        # ABSENT IS NOT UNAVAILABLE. "could not be retrieved" is outage
        # wording and invites a retry; for a layer with no coverage over
        # this land that retry can never succeed. e.reason says which case
        # it is -- see LayerFetchError's own docstring -- and a raise site
        # that recorded none keeps the original wording exactly.
        reason = getattr(e, "reason", None)
        if reason == LayerFetchError.REASON_NO_DATA_FOR_PARCEL:
            error = (
                f"There is no {e.label} data available for this land. This is a permanent "
                f"gap in the data for this area, not a temporary outage -- retrying will "
                f"not help. Try drawing a boundary elsewhere."
            )
        else:
            error = f"The {e.label} could not be retrieved."
        failed_layer = {"type": e.layer, "label": e.label}
        if reason:
            failed_layer["reason"] = reason
        return jsonify({"error": error, "failed_layer": failed_layer}), 502

    except Exception:
        app.logger.exception("production-zones: unexpected failure")
        return jsonify({"error": "Production zones could not be generated."}), 500


# The interactive session surface -- see session_api.py. Registered with no
# Dependencies argument, so it resolves the process-wide store (the
# KEYLINE_SESSION_STORE_DIR directory), session caches and job runner.
app.register_blueprint(session_api.build_blueprint())


@app.route("/api/health", methods=["GET"])
def health_check():
    """Simple endpoint to confirm the API is running and reachable."""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
