"""
api.py

A thin web API wrapping generate_full_report.py, so the frontend (running
in a browser) can trigger the full pipeline over HTTP instead of you
running it manually in a terminal.

This doesn't replace any of the existing modules — it just exposes the
same generate_full_report() function as a web endpoint.

Run locally with:
    python3 api.py

Then it's reachable at http://localhost:5000
"""

import os
import tempfile

from flask import Flask, after_this_request, request, jsonify, send_file
from flask_cors import CORS

from generate_full_report import generate_full_report
from generate_pdf_report import generate_full_report_pdf
from geocode import geocode_address

app = Flask(__name__)

# CORS (Cross-Origin Resource Sharing) lets the frontend — running on a
# different local port (5173) — actually call this API. Browsers block
# cross-origin requests by default unless the server explicitly allows
# it; this line is what allows it during local development.
CORS(app)


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
        { "boundary": [[lon, lat], [lon, lat], ...] }

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
        # generate_full_report expects a list of (lon, lat) tuples; JSON
        # gives us lists, but Python's tuple-unpacking in the downstream
        # functions works the same either way, so no conversion needed.
        report = generate_full_report(boundary)
        return jsonify({"report": report})

    except RuntimeError as e:
        # Covers the missing ANTHROPIC_API_KEY case specifically
        return jsonify({"error": str(e)}), 500

    except Exception as e:
        return jsonify({"error": f"Report generation failed: {str(e)}"}), 500


@app.route("/api/generate-report-pdf", methods=["POST"])
def generate_report_pdf_endpoint():
    """
    Expects a JSON body like:
        { "boundary": [[lon, lat], ...], "property_label": "..." }
    ("property_label" is optional -- a display label for the report's
    cover page, e.g. the geocoded address; defaults to a generic label.)

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

    property_label = data.get("property_label") or "Property Design Report"

    try:
        tmp_dir = tempfile.mkdtemp(prefix="keyline-report-")
        pdf_path = os.path.join(tmp_dir, "scale_of_permanence_report.pdf")

        generate_full_report_pdf(boundary, pdf_path, property_label=property_label)

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

    except RuntimeError as e:
        # Covers the missing ANTHROPIC_API_KEY case specifically
        return jsonify({"error": str(e)}), 500

    except Exception as e:
        return jsonify({"error": f"PDF report generation failed: {str(e)}"}), 500


@app.route("/api/health", methods=["GET"])
def health_check():
    """Simple endpoint to confirm the API is running and reachable."""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
