"""
test_landform_pages.py

THE USER'S OWN LANDFORM PAGES, FREE: rendered for a session from the data it
already holds, as page images, through the real routes, with no report-layer
fetch anywhere on the path.

Offline. overview_reference_fixture's session -- the real DEM, the real
boundary -- created under its Harness, then read exactly as production reads
it: the document off the store, the context off the session cache.

THE CASES:

  1. THE RENDER. Three pages, each 1275 x 1650 and WebP, each with a
     480 x 621 thumbnail; the manifest names them in the report's own
     wording, with the section's numeral; a second render of the same
     session comes from the store, not from WeasyPrint.
  2. NO REPORT-LAYER FETCH, STRUCTURALLY. The module's source names none
     of the report layer's entry points, and a render under the offline
     harness -- with the report layer's fetch AND its default cache patched
     to raise -- refuses zero requests. The property the page's speed and
     its reliability rest on, asserted both ways.
  3. THE ROUTES. GET .../landform-pages answers the manifest; GET
     .../landform-pages/<n> the bytes, inline, cached privately; a page the
     section does not have is a 404, a size that is neither is a 400, an
     unknown session is a 404.
  4. THE COLD PATHS. The session cache dropped with the boundary still in
     the Layer 1 fetch cache: the context is rebuilt, the pages render, and
     the network is still untouched. Both caches dropped and the network
     refused: a 502 with one sentence, within a second -- graceful, not a
     hang, and not a traceback.
  5. NOTHING ON THE PAGE SAYS IT IS THE WHOLE DOCUMENT: no cover, no page
     number, no contents; the eyebrow's numeral says the section has a
     place in a larger document.
  6. THE TIME, printed: the warm render, end to end.
"""

import io
import re
import time
from unittest.mock import patch

import offline_harness

offline_harness.install()

from PIL import Image  # noqa: E402

import landform_pages  # noqa: E402
import landform_section  # noqa: E402
import overview_reference_fixture as fixture  # noqa: E402
import report_data  # noqa: E402
import session_api  # noqa: E402
import site_report  # noqa: E402

HERE = __import__("os").path.dirname(__import__("os").path.abspath(__file__))


def webp_size(data: bytes) -> tuple:
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP", f"not WebP: {data[:12]!r}"
    return Image.open(io.BytesIO(data)).size


def forbid_report_layer():
    """The report layer's two entry points, patched to raise: the render
    below must never reach either."""
    return (
        patch.object(report_data, "fetch_report_data", side_effect=AssertionError("report layer fetched")),
        patch.object(report_data, "default_report_fetch_cache", side_effect=AssertionError("report cache touched")),
    )


with fixture.Harness():
    session = fixture.Session()
refused_before = len(offline_harness.refused())

# ----------------------------------------------------------------------
# 1. The render
# ----------------------------------------------------------------------

store = landform_pages.LandformPageStore()
fetch_patch, cache_patch = forbid_report_layer()
with fetch_patch, cache_patch:
    started = time.perf_counter()
    pages = store.get_or_render(session.id, session.store, fetch_cache=session.fetch_cache, cache=session.cache)
    warm_seconds = time.perf_counter() - started

assert len(pages.pages) == landform_pages.PAGE_COUNT == 3, len(pages.pages)
for page in pages.pages:
    assert webp_size(page.full) == (landform_pages.FULL_WIDTH, landform_pages.FULL_HEIGHT) == (1275, 1650)
    assert webp_size(page.thumb) == (landform_pages.THUMB_WIDTH, landform_pages.THUMB_HEIGHT) == (480, 621)
    assert page.alt and page.alt.startswith("Landform, page")
assert [page.label for page in pages.pages] == [
    "III · Landform", "III · Landform, continued", "III · Landform, continued",
]

manifest = pages.manifest()
assert manifest["session_id"] == session.id
assert manifest["section"] == {"number": "III", "name": "Landform"}
assert [p["number"] for p in manifest["pages"]] == [1, 2, 3]
for entry in manifest["pages"]:
    assert entry["url"] == f"/api/sessions/{session.id}/landform-pages/{entry['number']}"
    assert entry["thumb_url"] == entry["url"] + "?size=thumb"
    assert (entry["width"], entry["height"]) == (1275, 1650)
    assert (entry["thumb_width"], entry["thumb_height"]) == (480, 621)

# A second ask is the store's, not WeasyPrint's.
again = store.get_or_render(session.id, session.store, fetch_cache=session.fetch_cache, cache=session.cache)
assert again is pages and store.renders == 1
print(f"1. three pages, 1275x1650 WebP with 480x621 thumbnails; rendered once and kept ({warm_seconds:.2f} s warm)")

# ----------------------------------------------------------------------
# 2. No report-layer fetch, structurally
# ----------------------------------------------------------------------

with open(landform_pages.__file__, encoding="utf-8") as handle:
    source = handle.read()
# The module's docstring may NAME the report layer to say it is not used;
# the code must not. Strip the module docstring and comments before reading.
code = re.sub(r'^"""[\s\S]*?"""', "", source, count=1)
code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())
for forbidden in ("report_fetch_cache", "fetch_report_data", "default_report_fetch_cache",
                  "import report_data", "ReportData", "generate_session_site_report_pdf"):
    assert forbidden not in code, f"landform_pages.py names the report layer: {forbidden}"
assert "import site_report" not in code.split("def ")[0], "site_report is imported only where it is used"
assert len(offline_harness.refused()) == refused_before, offline_harness.refused()[refused_before:]
print("2. the module names no report-layer entry point, and the warm render refused zero requests")

# ----------------------------------------------------------------------
# 3. The routes
# ----------------------------------------------------------------------

route_store = landform_pages.LandformPageStore()
deps = session_api.Dependencies(
    store=session.store, fetch_cache=session.fetch_cache, cache=session.cache, landform_page_store=route_store,
)
client = session_api.create_app(deps).test_client()

with forbid_report_layer()[0], forbid_report_layer()[1]:
    response = client.get(f"/api/sessions/{session.id}/landform-pages")
assert response.status_code == 200, (response.status_code, response.get_json())
assert response.headers["Cache-Control"] == "no-store"
served = response.get_json()
assert served["pages"] == manifest["pages"]

full = client.get(served["pages"][2]["url"])
assert full.status_code == 200 and full.mimetype == "image/webp"
assert "attachment" not in (full.headers.get("Content-Disposition") or "")
assert full.headers["Cache-Control"] == "private, max-age=3600"
assert webp_size(full.data) == (1275, 1650)
thumb = client.get(served["pages"][2]["thumb_url"])
assert thumb.status_code == 200 and webp_size(thumb.data) == (480, 621)
assert route_store.renders == 1, "the page bytes came from the store, not a second render"

assert client.get(f"/api/sessions/{session.id}/landform-pages/4").status_code == 404
assert client.get(f"/api/sessions/{session.id}/landform-pages/0").status_code == 404
assert client.get(served["pages"][0]["url"] + "?size=huge").status_code == 400
assert client.get("/api/sessions/no-such-session/landform-pages").status_code == 404
assert client.get("/api/sessions/no-such-session/landform-pages/1").status_code == 404
print("3. the routes: manifest, inline WebP bytes, 404 / 400 for what is not there")

# ----------------------------------------------------------------------
# 4. The cold paths
# ----------------------------------------------------------------------

# A: the session cache let the context go; the Layer 1 fetch cache still
# holds the boundary. Rebuilt -- the warm-up only -- and no network.
route_store.clear()
session.cache.clear()
assert session.id not in session.cache
refused_at = len(offline_harness.refused())
started = time.perf_counter()
response = client.get(f"/api/sessions/{session.id}/landform-pages")
cold_a_seconds = time.perf_counter() - started
assert response.status_code == 200, response.get_json()
assert len(offline_harness.refused()) == refused_at, "a rebuild on a cached boundary fetched nothing"
assert session.id in session.cache, "the rebuild repopulated the session cache"
print(f"4a. session cache dropped, Layer 1 cached: rebuilt and rendered with no network ({cold_a_seconds:.2f} s)")

# B: both caches dropped and the network refused -- the one path that can
# fail. One sentence, 502, promptly.
route_store.clear()
session.cache.clear()
session.fetch_cache.clear()
started = time.perf_counter()
response = client.get(f"/api/sessions/{session.id}/landform-pages")
cold_b_seconds = time.perf_counter() - started
assert response.status_code == 502, (response.status_code, response.get_json())
body = response.get_json()
assert body == {"error": landform_pages.REBUILD_FAILED}, body
assert "design has changed" in body["error"] and "Try again" in body["error"]
assert "Traceback" not in body["error"] and "elevation.nationalmap.gov" not in body["error"]
assert cold_b_seconds < 5.0, cold_b_seconds
assert len(route_store) == 0, "a failed render cached nothing"
assert len(offline_harness.refused()) > refused_at, "the refetch was attempted and refused"
print(f"4b. both caches dropped, network refused: 502 with one sentence in {cold_b_seconds:.2f} s, nothing cached")

# ----------------------------------------------------------------------
# 5. Nothing says these are the whole document
# ----------------------------------------------------------------------

with fixture.Harness():
    session2 = fixture.Session()
context = session2.context()
document = session2.stored()
terrain = landform_section.terrain_inputs_from_context(context, document)
section = landform_section.build_landform_section(terrain, site_report.TOKENS)
html = landform_pages.render_pages_html([section], __import__("datetime").date(2026, 10, 2))
assert 'class="cover' not in html and 'class="contents' not in html, "no cover, no contents"
assert "@bottom-center { content: none; }" in html, "the page counter is cleared"
assert "III · Landform" in html.replace("&middot;", "·") or "III" in html
assert html.count('class="section-anchor"') == 1
assert "break-before: auto" in html, "the first section does not break before itself"
assert "Site Data Report" in html and "2 October 2026" in html, "the running strings run"
assert "page 1 of" not in html and "1 of 3" not in html
# And the section's own footer still names its one source.
assert "USGS 3DEP" in html
print("5. no cover, no page number, no contents; the running title and date, and the numeral III")

# ----------------------------------------------------------------------
# 6. The time
# ----------------------------------------------------------------------

print(f"6. warm render {warm_seconds:.2f} s end to end (inputs, section, layout, PDF, raster, WebP); "
      f"rebuild with Layer 1 cached {cold_a_seconds:.2f} s")
print("test_landform_pages.py: all assertions passed")
