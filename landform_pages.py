"""
landform_pages.py

THE USER'S OWN LANDFORM PAGES, FREE: section III of the site data report,
rendered for one session from the data the session already holds, as page
images rather than a document.

    render_landform_pages(session_id, store, ...)  -> LandformPages
    LandformPageStore                              -> the rendered pages, by session
    page_url(session_id, number, size)             -> where the frontend fetches one

session_api.py wires the store to two routes: GET /api/sessions/<sid>/
landform-pages (the manifest, rendering on first call) and GET /api/
sessions/<sid>/landform-pages/<n> (one page's bytes).

--- WHY LANDFORM, AND WHY THESE ARE NOT A SAMPLE ------------------------

Landform is the one section built entirely from data a session holds the
moment its boundary is committed: the DEM and its derivations. Its source
line names USGS 3DEP and nothing else. So the user's own three pages --
contours and slope classes, the keyline structure and valley profile, the
numbers -- can be generated on arrival at the report page with no report-
time fetch at all. A sample page proves the report is well made; the
user's own pages prove it is about their land, and because they come off
the same section builder, template, stylesheet and WeasyPrint pass as the
paid report, the reader is holding part of their report and being offered
the rest.

--- THE STRUCTURAL PROPERTY: NO REPORT-LAYER FETCH, EVER -----------------

This module's entry point takes a session id, the document store and the
two SESSION caches. It has no report fetch cache parameter and never
imports the report layer, so a report-time fetch cannot be added to this
path without changing its signature -- which is the point of the module
existing rather than the full report being rendered with seven sections
filtered out. The section itself is built from landform_section.
TerrainInputs, a dataclass with no ReportData in it; landform_section's
own docstring and call-count tests hold it to reading the session and
computing nothing the warm-up already computed.

test_landform_pages.py asserts both halves: statically, that this file
names none of the report layer's entry points; and at run time, that a
render under the offline harness refuses zero requests with the report
layer's fetch patched to raise.

--- THE ONE CAVEAT, WHICH IS LAYER 1 AND NOT THE REPORT LAYER -----------

The session context is read through session_manager.get_session_context():
a cache hit, or a REBUILD from the Design Document for a session the cache
has let go (thirty idle minutes). With the boundary still in the Layer 1
fetch cache the rebuild is the terrain warm-up, under a second of compute
and no network. If both caches have dropped it, the rebuild refetches
Layer 1 -- 3DEP, SSURGO, the canopy, the roads -- which is tens of seconds
and is the ONE path on which these pages can meet a network failure.

Arriving from the delivery card the context is always live, so "no
network at all" holds exactly where the claim is made. A bookmarked URL
opened days later can pay the rebuild; a source that does not answer then
raises parcel_data.ParcelDataIncompleteError, which session_api's table
maps to 502 with failed_layer, so the page fails gracefully rather than
appearing to hang. That is the same rebuild the full report's cold
scenario pays, and nothing here adds a second one.

--- IMAGES, NOT A PDF -----------------------------------------------------

A downloadable three-page PDF gives away a usable artifact; images
demonstrate one. The treatment is the marketing page's sample pages'
(keyline-designer-frontend/src/assets/README.md): WebP, the full page at
1275 x 1650 -- 150 dpi of US Letter -- encoded lossless because these
pages are line art and type, and a 480 x 621 thumbnail at quality 85 for
the slot. The PDF WeasyPrint lays out is rasterized by pypdfium2 and
exists only in memory, for the length of this function.

PYPDFIUM2, NOT PYMUPDF. The sample pages were rendered once by hand with
PyMuPDF, which is AGPL-licensed -- the network clause is exactly the case
a hosted service that charges for a report runs into. pypdfium2 is BSD and
Apache licensed, a pip wheel with no system package behind it (nothing in
the Dockerfile changes), and measured five times faster on these pages.

--- TIME, MEASURED ------------------------------------------------------

Offline, on a 4-core box, the reference parcel, cached context: terrain
inputs and derivations 0.2 s, the section (two SVG maps, the profile, the
tables) 0.35 s, Jinja and WeasyPrint layout 0.5 s, the PDF write 0.2 s,
rasterizing 0.07 s, WebP encoding 0.3 s across three threads -- under
2 s end to end, about 2.5 s with the first request's imports. Short enough
that the report page draws no progress bar for it: its own text is
readable while the pages come, and one quiet line marks the slot.

--- THE STORE -------------------------------------------------------------

Rendered once per session and kept in memory -- about a quarter of a
megabyte for the six images -- so a refresh, the back button, a return
from a payment provider or a second tab is served instantly. The pages
depend on the boundary and the DEM, which a session fixes at creation, so
there is nothing that can make a cached render stale. Capped and evicted
oldest-first like job_runner's registry; a restart loses it, and the next
request renders again, identically.
"""

import io
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from typing import Optional

import requests
from PIL import Image

import landform_section
import parcel_data
import session_manager
from climate_section import format_generated_on
from report_outline import section_number

# The route suffix under a session. One spelling, imported by session_api.py
# for its rule and used here for the manifest's URLs.
PAGES_URL_SUFFIX = "landform-pages"

# The template that carries sections with no cover (templates/report/pages.html).
PAGES_TEMPLATE = "pages.html"

# The running footer's label. The report prints the property label or the
# centroid here; these pages print neither -- the centroid is locatable,
# which is why the sample pages' footers were painted out -- and say what
# the document is instead.
RUNNING_LABEL = "Site Data Report"

# The sample pages' sizes, exactly: 150 dpi of 8.5 x 11 in, and the thumbnail.
FULL_WIDTH, FULL_HEIGHT = 1275, 1650
THUMB_WIDTH, THUMB_HEIGHT = 480, 621
FULL_DPI = 150
POINTS_PER_INCH = 72

# WebP encoding. Lossless for the full page (line art and type: smaller
# lossless than lossy at any quality that keeps the type crisp, the sample
# pages' finding); the thumbnail lossy. method=4 rather than 6: the
# difference is a few percent of size and a third of the encode time.
WEBP_METHOD = 4
THUMB_QUALITY = 85

SIZE_FULL = "full"
SIZE_THUMB = "thumb"
SIZES = (SIZE_FULL, SIZE_THUMB)

WEBP_MIME_TYPE = "image/webp"

# THE THREE PAGES, BY RULE (landform_section's module docstring and
# landform.html): the terrain, the keyline structure, the numbers. The
# section is laid out to exactly this many pages and the test holds it
# there; the alt text is the page's content for a reader who cannot see it.
PAGE_COUNT = 3
PAGE_ALT = (
    "Landform, page one: a summary sentence, the terrain map of the parcel with "
    "slope classes tinted and contours labelled, and the slope class table in acres.",
    "Landform, page two: the keyline structure map at the same extent -- contours, "
    "ridges, valley stems, keylines with their elevations and keypoints -- and the "
    "primary valley's profile.",
    "Landform, page three: nine key figures, the aspect table, the valley table and "
    "the source line.",
)

DEFAULT_MAX_SESSIONS = 64


@dataclass(frozen=True)
class RenderedPage:
    """One page, as the two WebP encodings the route serves."""

    number: int
    label: str
    alt: str
    full: bytes
    thumb: bytes


@dataclass(frozen=True)
class LandformPages:
    """A session's three pages and when they were made."""

    session_id: str
    generated_on: date
    pages: tuple

    def manifest(self) -> dict:
        """What GET .../landform-pages answers: the pages, their URLs and
        sizes. The URLs are paths, as a report's download_url is -- the
        server does not know what origin it is reached on, and the client
        joins them against the API's address it already holds."""
        return {
            "session_id": self.session_id,
            "generated_on": self.generated_on.isoformat(),
            "section": {"number": section_number(landform_section.SECTION_NAME), "name": landform_section.SECTION_NAME},
            "pages": [
                {
                    "number": page.number,
                    "label": page.label,
                    "alt": page.alt,
                    "url": page_url(self.session_id, page.number, SIZE_FULL),
                    "thumb_url": page_url(self.session_id, page.number, SIZE_THUMB),
                    "width": FULL_WIDTH,
                    "height": FULL_HEIGHT,
                    "thumb_width": THUMB_WIDTH,
                    "thumb_height": THUMB_HEIGHT,
                }
                for page in self.pages
            ],
        }

    def page(self, number: int) -> RenderedPage:
        for page in self.pages:
            if page.number == number:
                return page
        raise PageNotFoundError(number)


class PageNotFoundError(KeyError):
    """A page number the section does not have."""


class RebuildFailedError(RuntimeError):
    """
    The module docstring's caveat, as an exception: the session's context
    had to be rebuilt, the rebuild had to refetch Layer 1, and a source did
    not answer. Carries the cause for the log; the message is for the
    client, and it says the design is untouched and asks for a retry, which
    is the one thing that can help -- an outage passes.

    session_api maps it to 502, as it maps the session-creation path's
    own fetch failures: the request was fine and this server's code did
    not break; an upstream source did. A raise site that already names its
    layer (parcel_data.ParcelDataIncompleteError) is NOT wrapped -- it
    reaches the table on its own and keeps its failed_layer.
    """

    def __init__(self, session_id: str, cause: BaseException):
        self.session_id = session_id
        self.cause = cause
        super().__init__(REBUILD_FAILED)


REBUILD_FAILED = (
    "This session's elevation data is no longer cached and could not be fetched again, so its "
    "Landform pages could not be made right now. Nothing about your design has changed. Try "
    "again in a moment."
)


def pages_url(session_id: str) -> str:
    return f"/api/sessions/{session_id}/{PAGES_URL_SUFFIX}"


def page_url(session_id: str, number: int, size: str = SIZE_FULL) -> str:
    """The URL for one page's bytes. The full size is the bare URL; the
    thumbnail says so in the query."""
    base = f"{pages_url(session_id)}/{number}"
    return base if size == SIZE_FULL else f"{base}?size={size}"


def page_label(number: int) -> str:
    """The slot's caption, in the report's own eyebrow wording."""
    numeral = section_number(landform_section.SECTION_NAME)
    name = landform_section.SECTION_NAME
    return f"{numeral} · {name}" if number == 1 else f"{numeral} · {name}, continued"


# ======================================================================
# The render
# ======================================================================


def render_pages_html(sections: list, generated_on: date, env=None, label: str = RUNNING_LABEL) -> str:
    """The sections through pages.html: the report's stylesheet, no cover,
    no page number. `env` defaults to site_report's Jinja environment,
    imported here rather than at module scope so this module's own import
    graph stays what the docstring says it is."""
    import site_report

    env = env or site_report.jinja_environment()
    for section in sections:
        section.setdefault("anchor", site_report.section_anchor(section["number"]))
    return env.get_template(PAGES_TEMPLATE).render(
        stylesheet=site_report.render_stylesheet(env),
        sections=sections,
        title=f"{RUNNING_LABEL} — {landform_section.SECTION_NAME}",
        label=label,
        generated_on=format_generated_on(generated_on),
    )


def render_pdf(html: str) -> bytes:
    """HTML -> PDF bytes, one WeasyPrint pass. In memory: the PDF is an
    intermediate here, never served and never written. WeasyPrint is
    imported where it is used, site_report's reason."""
    import site_report
    from weasyprint import HTML

    return HTML(string=html, base_url=site_report.TEMPLATES_DIRECTORY).render().write_pdf()


def rasterize(pdf_bytes: bytes) -> list:
    """Every page of a PDF as an RGB PIL image at the sample pages' size.

    pypdfium2 rounds the bitmap up to whole pixels, so 792 pt at 150 dpi
    can come out a pixel tall; the render is cropped to exactly
    FULL_WIDTH x FULL_HEIGHT so every page and every thumbnail is the size
    the manifest states."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf_bytes)
    images = []
    try:
        for page in document:
            bitmap = page.render(scale=FULL_DPI / POINTS_PER_INCH, draw_annots=False, may_draw_forms=False,
                                 rev_byteorder=True)
            image = bitmap.to_pil().convert("RGB")
            if image.size != (FULL_WIDTH, FULL_HEIGHT):
                canvas = Image.new("RGB", (FULL_WIDTH, FULL_HEIGHT), (255, 255, 255))
                canvas.paste(image.crop((0, 0, min(image.width, FULL_WIDTH), min(image.height, FULL_HEIGHT))), (0, 0))
                image = canvas
            images.append(image)
    finally:
        document.close()
    return images


def encode_page(image: Image.Image) -> tuple:
    """(full WebP bytes, thumbnail WebP bytes) for one page image."""
    full = io.BytesIO()
    image.save(full, "WEBP", lossless=True, method=WEBP_METHOD)
    thumb = io.BytesIO()
    image.resize((THUMB_WIDTH, THUMB_HEIGHT), Image.LANCZOS).save(thumb, "WEBP", quality=THUMB_QUALITY, method=WEBP_METHOD)
    return full.getvalue(), thumb.getvalue()


def encode_pages(images: list) -> list:
    """Every page encoded, one thread each: Pillow releases the GIL while
    it encodes, and the three encodes were the longest line of the render."""
    with ThreadPoolExecutor(max_workers=max(1, len(images))) as pool:
        return list(pool.map(encode_page, images))


def render_landform_pages(
    session_id: str,
    store,
    fetch_cache=None,
    cache=None,
    generated_on: Optional[date] = None,
) -> LandformPages:
    """
    The session's Landform pages: the document off the store, the context
    off the session cache (or rebuilt -- the module docstring's caveat),
    the terrain inputs read off both, the section built, laid out,
    rasterized and encoded.

    Raises document_store.SessionNotFoundError for an id the store does not
    hold, and whatever a Layer 1 rebuild raises when it must refetch and a
    source does not answer. Raises RuntimeError if the section did not lay
    out to PAGE_COUNT pages: the frontend's copy says "three", and a
    fourth page would be a layout regression, not a fourth page to show.
    """
    import site_report

    document = store.get(session_id)
    # THE REBUILD, AND THE ONE FAILURE IT CAN HAVE. A cache hit returns at
    # once; a rebuild on a cached boundary computes; a rebuild that must
    # refetch and cannot raises here. A layer that names itself passes
    # through to the error table with its failed_layer; anything else the
    # fetch layers raise -- a connection refused, a timeout, a malformed
    # answer -- becomes RebuildFailedError, so the client gets one sentence
    # and a 502 rather than a traceback and a 500.
    try:
        context = session_manager.get_session_context(session_id, store, fetch_cache=fetch_cache, cache=cache)
    except parcel_data.ParcelDataIncompleteError:
        raise
    except (requests.exceptions.RequestException, OSError, ValueError, KeyError, TypeError) as exc:
        raise RebuildFailedError(session_id, exc) from exc
    terrain = landform_section.terrain_inputs_from_context(context, document)
    section = landform_section.build_landform_section(terrain, site_report.TOKENS)
    generated_on = generated_on or date.today()
    images = rasterize(render_pdf(render_pages_html([section], generated_on)))
    if len(images) != PAGE_COUNT:
        raise RuntimeError(
            f"the Landform section laid out to {len(images)} pages for session '{session_id}', not {PAGE_COUNT}"
        )
    encoded = encode_pages(images)
    pages = tuple(
        RenderedPage(number=n, label=page_label(n), alt=PAGE_ALT[n - 1], full=full, thumb=thumb)
        for n, (full, thumb) in enumerate(encoded, start=1)
    )
    return LandformPages(session_id=session_id, generated_on=generated_on, pages=pages)


# ======================================================================
# The store
# ======================================================================


class LandformPageStore:
    """
    Rendered pages by session id. In memory, capped, oldest-first eviction,
    one render per session however many requests arrive for it at once.

    THE PER-SESSION LOCK is session_cache.FetchCache's arrangement: two tabs
    loading the report page together must not render twice, and a lock over
    the whole store would make one session's two-second render every other
    session's wait.
    """

    def __init__(self, max_sessions: int = DEFAULT_MAX_SESSIONS):
        if max_sessions < 1:
            raise ValueError(f"max_sessions must be >= 1, got {max_sessions}")
        self._max_sessions = max_sessions
        self._pages = OrderedDict()  # session_id -> LandformPages
        self._lock = threading.Lock()
        self._session_locks = {}
        self.renders = 0  # how many times this store rendered; a test's count

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._lock:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = self._session_locks[session_id] = threading.Lock()
            return lock

    def get(self, session_id: str) -> Optional[LandformPages]:
        with self._lock:
            return self._pages.get(session_id)

    def get_or_render(self, session_id: str, store, fetch_cache=None, cache=None) -> LandformPages:
        """The session's pages, rendered on the first call and served from
        memory after. A render that raises caches nothing, so the next
        request tries again -- the rebuild caveat's outage passes."""
        held = self.get(session_id)
        if held is not None:
            return held
        with self._session_lock(session_id):
            held = self.get(session_id)
            if held is not None:
                return held
            pages = render_landform_pages(session_id, store, fetch_cache=fetch_cache, cache=cache)
            with self._lock:
                self.renders += 1
                self._pages[session_id] = pages
                while len(self._pages) > self._max_sessions:
                    evicted, _ = self._pages.popitem(last=False)
                    self._session_locks.pop(evicted, None)
            return pages

    def discard(self, session_id: str) -> bool:
        with self._lock:
            self._session_locks.pop(session_id, None)
            return self._pages.pop(session_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._pages.clear()
            self._session_locks.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._pages)


# The process-wide default, session_report.DEFAULT_REPORT_STORE's shape: a
# caller with its own store (a test) passes it, everyone else gets this one.
DEFAULT_LANDFORM_PAGE_STORE = LandformPageStore()
