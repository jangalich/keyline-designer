"""
serve_test_backend.py

THE REAL BACKEND, SERVED, WITH ONLY THE NETWORK MOCKED.

    python serve_test_backend.py [port]

api.py's own Flask app -- every session route, /api/production-zones and
/api/generate-report-pdf, from the same blueprint the deployment carries --
running over HTTP against the real pipeline: session_manager,
step_orchestrator, commit_validation, production_zone_payload and
wire_translation all execute for real, on the real reference boundary's DEM.

WHAT IS MOCKED IS THE NETWORK AND ONLY THE NETWORK. test_step_commit.py's
Harness, imported rather than copied, so the fixtures the frontend's end-to-end
test drives against are byte-for-byte the fixtures the backend's own test suite
asserts on. A sandbox has no route to the USGS, SSURGO or Overpass endpoints
this pipeline reads; without these patches nothing would run at all, and with
anything more than these patches the test would stop being end to end.

The frontend's landform.live.test.jsx points VITE_API_URL at this and drives
the whole boundary -> generate -> select -> draw -> commit -> reopen flow
through it.
"""

import os
import sys
import tempfile

# The store has to exist before session_api reads its directory.
STORE = os.environ.setdefault(
    "KEYLINE_SESSION_STORE_DIR", tempfile.mkdtemp(prefix="keyline-live-store-")
)

import test_step_commit as T  # noqa: E402  -- installs nothing; imported for Harness


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5099
    import api

    # CORS for the test origin. api.py allows its own deployment origins; the
    # jsdom test runs as http://localhost:3000.
    from flask_cors import CORS

    CORS(api.app, origins="*")

    with T.Harness():
        print(f"READY {port} store={STORE}", flush=True)
        api.app.run(host="127.0.0.1", port=port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
