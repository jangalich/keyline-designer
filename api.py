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

from flask import Flask, request, jsonify
from flask_cors import CORS

from generate_full_report import generate_full_report

app = Flask(__name__)

# CORS (Cross-Origin Resource Sharing) lets the frontend — running on a
# different local port (5173) — actually call this API. Browsers block
# cross-origin requests by default unless the server explicitly allows
# it; this line is what allows it during local development.
CORS(app)


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


@app.route("/api/health", methods=["GET"])
def health_check():
    """Simple endpoint to confirm the API is running and reachable."""
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(debug=True, port=5000)
