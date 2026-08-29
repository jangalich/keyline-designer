"""
api.py

A thin web API wrapping generate_full_report.py, so the frontend (running
in a browser) can trigger the full pipeline over HTTP instead of you
running it manually in a terminal.

This doesn't replace any of the existing modules — it just exposes the
same generate_full_report() function as a web endpoint.

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

import os
import re
import tempfile

from flask import Flask, after_this_request, request, jsonify, send_file
from flask_cors import CORS

import session_api
from generate_full_report import generate_full_report
from generate_pdf_report import generate_full_report_pdf
from geocode import geocode_address
from production_zone_payload import (
    LayerFetchError,
    build_production_zone_payload,
)

app = Flask(__name__)


def _parse_access_point(data: dict) -> tuple[float, float]:
    """
    Pulls the required 'access_point' field out of a request body and
    validates its shape -- a single [lon, lat] pair, same convention as
    'boundary' coordinates. Raises ValueError with a caller-facing message
    on anything missing/malformed; callers turn that into a 400, same
    "fail loud, don't fake a good result" pattern the rest of this
    pipeline uses for a bad/missing anchor point (see road_corridors.
    validate_access_point_on_boundary(), which handles the separate
    geometric check -- is it actually on the boundary -- once the pipeline
    itself runs).
    """
    if "access_point" not in data:
        raise ValueError(
            "Request must include an 'access_point' field: a single [lon, lat] point "
            "where the property meets a road along its boundary."
        )

    access_point = data["access_point"]

    if (
        not isinstance(access_point, (list, tuple))
        or len(access_point) != 2
        or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in access_point)
    ):
        raise ValueError("'access_point' must be a [longitude, latitude] pair of numbers.")

    return (float(access_point[0]), float(access_point[1]))


def _parse_boundary(data: dict) -> list:
    """
    Pulls and validates the required 'boundary' field, raising ValueError
    with a caller-facing message -- same shape and same convention as
    _parse_access_point() above, and the caller turns it into a 400.

    /api/generate-report and /api/generate-report-pdf still carry their own
    inline copies of these two checks. That duplication is REAL and is left
    in place deliberately: collapsing it would mean editing the request
    handling of the two endpoints the existing boundary -> access point ->
    PDF flow runs through, and this branch adds a parallel path rather than
    disturbing that one. Worth folding together in a branch that is allowed
    to touch them.
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


@app.route("/api/generate-report", methods=["POST"])
def generate_report_endpoint():
    """
    Expects a JSON body like:
        {
            "boundary": [[lon, lat], [lon, lat], ...],
            "access_point": [lon, lat]
        }
    ("access_point" is required -- the real point where the property meets
    a road along its boundary; road-corridor routing starts from it.)

    Returns:
        { "report": "..." }  on success
        { "error": "..." }   on failure, with an appropriate HTTP status
    """
    data = request.get_json(silent=True)

    if not data or "boundary" not in data:
        return jsonify({"error": "Request must include a 'boundary' field."}), 400

    boundary = data["boundary"]

    if not isinstance(boundary, list) or len(boundary) < 3:
        return jsonify({"error": "Boundary must be a list of at least 3 [lon, lat] points."}), 400

    try:
        access_point = _parse_access_point(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    try:
        # generate_full_report expects a list of (lon, lat) tuples; JSON
        # gives us lists, but Python's tuple-unpacking in the downstream
        # functions works the same either way, so no conversion needed.
        report = generate_full_report(boundary, access_point)
        return jsonify({"report": report})

    except ValueError as e:
        # Covers validate_access_point_on_boundary()'s own rejection of an
        # access_point that isn't actually on this boundary.
        return jsonify({"error": str(e)}), 400

    except RuntimeError as e:
        # Covers the missing ANTHROPIC_API_KEY case specifically
        return jsonify({"error": str(e)}), 500

    except Exception as e:
        return jsonify({"error": f"Report generation failed: {str(e)}"}), 500


@app.route("/api/generate-report-pdf", methods=["POST"])
def generate_report_pdf_endpoint():
    """
    Expects a JSON body like:
        {
            "boundary": [[lon, lat], ...],
            "access_point": [lon, lat],
            "property_label": "..."
        }
    ("access_point" is required -- the real point where the property meets
    a road along its boundary; road-corridor routing starts from it, same
    convention as "boundary" coordinates. "property_label" is optional --
    a display label for the report's cover page, e.g. the geocoded
    address; defaults to a generic label.)

    Returns the assembled PDF (the existing narrative Scale of Permanence
    report, unchanged, plus the new final-page static layout map) as a
    binary file download, or { "error": "..." } with an appropriate HTTP
    status on failure.

    This is a NEW, SEPARATE endpoint from /api/generate-report rather
    than an addition to that endpoint's response -- the return type here
    is fundamentally different (a binary PDF file, not JSON), so folding
    it into the existing response would mean either always paying the
    map-rendering + PDF-assembly cost for callers who only want the
    narrative text, or awkwardly base64-encoding a PDF into a JSON field.
    A separate endpoint also means the frontend's "Generate Report"
    button (which calls /api/generate-report today) is completely
    unaffected by this addition -- wiring the button to PDF output is a
    deliberate, separate next step, not part of this change.
    """
    data = request.get_json(silent=True)

    if not data or "boundary" not in data:
        return jsonify({"error": "Request must include a 'boundary' field."}), 400

    boundary = data["boundary"]

    if not isinstance(boundary, list) or len(boundary) < 3:
        return jsonify({"error": "Boundary must be a list of at least 3 [lon, lat] points."}), 400

    try:
        access_point = _parse_access_point(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    property_label = data.get("property_label") or "Property Design Report"

    try:
        tmp_dir = tempfile.mkdtemp(prefix="keyline-report-")
        pdf_path = os.path.join(tmp_dir, "scale_of_permanence_report.pdf")

        generate_full_report_pdf(boundary, pdf_path, access_point, property_label=property_label)

        @after_this_request
        def _cleanup_temp_pdf(response):
            try:
                os.remove(pdf_path)
                os.rmdir(tmp_dir)
            except OSError:
                pass
            return response

        return send_file(
            pdf_path,
            mimetype="application/pdf",
            as_attachment=True,
            download_name="scale_of_permanence_report.pdf",
        )

    except ValueError as e:
        # Covers validate_access_point_on_boundary()'s own rejection of an
        # access_point that isn't actually on this boundary.
        return jsonify({"error": str(e)}), 400

    except RuntimeError as e:
        # Covers the missing ANTHROPIC_API_KEY case specifically
        return jsonify({"error": str(e)}), 500

    except Exception as e:
        return jsonify({"error": f"PDF report generation failed: {str(e)}"}), 500


@app.route("/api/production-zones", methods=["POST"])
def production_zones_endpoint():
    """
    Expects a JSON body like:
        { "boundary": [[lon, lat], [lon, lat], ...] }

    NO 'access_point'. Unlike /api/generate-report and
    /api/generate-report-pdf, nothing on this path routes a road, so
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
    that this parcel has no lidar coverage). The exception is logged
    server-side instead.

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
        return jsonify({
            "error": f"The {e.label} could not be retrieved.",
            "failed_layer": {"type": e.layer, "label": e.label},
        }), 502

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
