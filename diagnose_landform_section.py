"""
diagnose_landform_section.py

THE LANDFORM SECTION'S PROOF RENDER, judged as pages -- branch 6, phase 2
of site-data-report-proposal.md's build sequence:

    python3 diagnose_landform_section.py [out_dir]          # offline, the fixture DEM
    python3 diagnose_landform_section.py [out_dir] --live   # the REAL parcel, over the network

  * an OFFLINE SESSION (roads_step_fixture.Harness: every network call
    mocked, every real computation run and counted) created on the real
    reference boundary, its terrain warm-up done -- the same context the
    report reads in production; or, with --live, a REAL session --
    session_manager.create_session() on the reference boundary with
    Layer 1 fetched from USGS/USDA and the Daymet record fetched for
    Climate -- which is the real-DEM render the offline run cannot give;
  * the whole report -- cover, Climate from the Daymet fixture, Landform
    from that session -- as PDF, and EVERY PAGE as PNG;
  * the measurements: both acreage columns against the cover's acreage;
    the terrain token and the slope ramp; the classes present; the
    contour interval, count and labels; the legend entries; the call
    counts that prove Landform recomputed nothing the warm-up built.

THE DEM, offline, is the fixture's synthetic terrain over the real
boundary (a 4% bench, an incised drainage, flanking levees) -- no real
3DEP raster is checked in and the sandbox has no network. The classes,
acreages and contours are real computations over that array; the land is
not the parcel's. Run --live from a machine with network for the parcel's
own ground.
"""

import os
import sys
import tempfile
from datetime import date
from unittest.mock import patch

LIVE = "--live" in sys.argv
if not LIVE:
    import offline_harness

    offline_harness.install()

import exclusion_zones  # noqa: E402
import keypoint_detection  # noqa: E402
import landform_section  # noqa: E402
import roads_step_fixture as fixture  # noqa: E402
import site_report  # noqa: E402
import terrain_metrics  # noqa: E402
import valley_delineation  # noqa: E402
from daymet_data import parse_daymet_csv  # noqa: E402
from reference_fixture import REAL_BOUNDARY  # noqa: E402
from report_data import report_data_from_daily  # noqa: E402

FIXTURE = "daymet_reference_fixture.csv"
# The generation date is the render date, as in real use (site_report
# defaults generated_on to date.today()); pinning it here would put the
# running footer's date before the source line's retrieval date.
GENERATED_ON = date.today()


def report_data():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), FIXTURE), encoding="utf-8") as handle:
        return report_data_from_daily(REAL_BOUNDARY, parse_daymet_csv(handle.read()))


class _LiveSession:
    """A REAL session on the reference boundary -- Layer 1 over the network,
    the warm-up on the parcel's own DEM. Same three reads the offline
    Session offers."""

    def __init__(self):
        import tempfile as _tempfile

        import session_cache
        import session_manager
        from document_store import JSONFileStore

        self.store = JSONFileStore(_tempfile.mkdtemp(prefix="landform_live_"))
        self.fetch_cache = session_cache.FetchCache(max_entries=2)
        self.cache = session_cache.SessionCache(max_sessions=2, idle_timeout_seconds=1800.0)
        self.document = session_manager.create_session(
            REAL_BOUNDARY, self.store, fetch_cache=self.fetch_cache, cache=self.cache
        )
        self.id = self.document["session_id"]
        self._session_manager = session_manager

    def context(self):
        return self._session_manager.get_session_context(
            self.id, self.store, fetch_cache=self.fetch_cache, cache=self.cache
        )

    def stored(self):
        return self.store.get(self.id)


class _NoHarness:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def main(out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    if LIVE:
        import report_data as report_data_module

        data = report_data_module.default_report_fetch_cache().get_or_fetch(REAL_BOUNDARY)
        print("LIVE: Layer 1 and Daymet fetched over the network for the reference parcel")
    else:
        data = report_data()

    with (_NoHarness() if LIVE else fixture.Harness()):
        session = _LiveSession() if LIVE else fixture.Session()
        context = session.context()
        document = session.stored()

        # THE CALL COUNT. Everything the warm-up built must be READ here,
        # not rebuilt: zero calls to the slope grid, the valleys and the
        # keypoints while the section is built. Aspect is the one
        # conditional pass -- once, when no landform proposal exists.
        with patch.object(exclusion_zones, "compute_slope_percent", wraps=exclusion_zones.compute_slope_percent) as slope, \
             patch.object(valley_delineation, "delineate_valleys", wraps=valley_delineation.delineate_valleys) as valleys, \
             patch.object(keypoint_detection, "detect_keypoints", wraps=keypoint_detection.detect_keypoints) as keypoints, \
             patch.object(terrain_metrics, "compute_slope_and_aspect", wraps=terrain_metrics.compute_slope_and_aspect) as aspect:
            terrain = landform_section.terrain_inputs_from_context(context, document)
            section = landform_section.build_landform_section(terrain, site_report.TOKENS)
        print(
            "call counts while building Landform: compute_slope_percent "
            f"{slope.call_count}, delineate_valleys {valleys.call_count}, detect_keypoints {keypoints.call_count}, "
            f"compute_slope_and_aspect {aspect.call_count} (aspect source: {section['aspect_source']})"
        )

        html = site_report.render_site_report_html(data, generated_on=GENERATED_ON, terrain=terrain)
        pdf_path = os.path.join(out_dir, "site-report.pdf")
        site_report.generate_site_report_pdf(data, pdf_path, generated_on=GENERATED_ON, terrain=terrain)

    with open(os.path.join(out_dir, "site-report.html"), "w", encoding="utf-8") as handle:
        handle.write(html)

    cover_acres = round(terrain.parcel_acres, 1)
    slope_table, aspect_table = section["slope_table"], section["aspect_table"]
    print(f"parcel {terrain.parcel_acres:.4f} acres -> cover {cover_acres:.1f}")
    print(f"slope classes present {section['classes_present']}; acres {slope_table['acres']} "
          f"sum {round(sum(slope_table['acres']), 6)}; shares {slope_table['shares']} sum {round(sum(slope_table['shares']), 6)}")
    print(f"aspect acres {aspect_table['acres']} sum {round(sum(aspect_table['acres']), 6)}; "
          f"shares {aspect_table['shares']} sum {round(sum(aspect_table['shares']), 6)}")
    classified = section["classified"]
    print(f"cells {classified['valid_cells']}; slope counts {classified['slope_counts']}; "
          f"aspect counts {classified['aspect_counts']}; unclassified aspect {classified['unclassified_aspect']}; "
          f"mean slope {classified['mean_slope_pct']:.2f}%")
    c = section["contours"]
    print(f"contours: relief {c['relief_ft']:.1f} ft ({c['min_ft']:.0f}-{c['max_ft']:.0f}); interval {c['interval_ft']} ft; "
          f"{c['line_count']} lines, {c['index_count']} index")
    print(f"terrain token {site_report.TOKENS['terrain']}; slope ramp {landform_section.SLOPE_TINT_OPACITY} "
          f"(cap {landform_section.SLOPE_TINT_CAP})")
    print("legend:", [("".join(p if isinstance(p, str) else p["value"] for p in e["parts"])) for e in section["map"]["legend"]])
    svg = section["map"]["svg"]
    print(f"index labels in SVG: {svg.count('rotate(')}; keypoints drawn {len(landform_section.parcel_keypoints(terrain))}; "
          f"valley lines {len(landform_section.valley_lines(terrain))}")
    print("summary:", "".join(p if isinstance(p, str) else p["value"] for p in section["summary"]))
    print("key figures:", [(f["value"], f["label"]) for f in section["key_figures"]])

    # THE RAMP, FOR THE EYE: every class tint with a full-strength terrain
    # contour and an index label across it, so the cap can be judged where
    # it matters -- class F under a labelled contour. Literals: none; the
    # swatches are the renderer's own legend swatches at a larger size.
    from weasyprint import HTML

    tokens = site_report.TOKENS
    cells = []
    for name, _, _ in landform_section.SLOPE_CLASSES:
        opacity = landform_section.SLOPE_TINT_OPACITY[name]
        cells.append(
            f'<div style="display:inline-block;width:1.05in;margin-right:6pt;text-align:center">'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="1.05in" height="0.6in" viewBox="0 0 75 43">'
            f'<rect width="75" height="43" fill="{tokens["terrain"]}" fill-opacity="{opacity}"/>'
            f'<path d="M0 30 L28 27" stroke="{tokens["terrain"]}" stroke-width="0.95" fill="none"/>'
            f'<path d="M47 25 L75 22" stroke="{tokens["terrain"]}" stroke-width="0.95" fill="none"/>'
            f'<text x="37.5" y="28.5" font-family="IBM Plex Mono" font-size="6.5" fill="{tokens["terrain"]}" text-anchor="middle">1,025</text>'
            f'<path d="M0 15 L75 12" stroke="{tokens["terrain"]}" stroke-width="0.45" fill="none"/>'
            f'</svg><div class="eyebrow" style="margin-top:3pt">{name} · {landform_section.slope_range_label(name)} · {opacity}</div></div>'
        )
    ramp_html = (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{site_report.render_stylesheet()}'
        f"@page {{ size: 7.4in 2.0in; margin: 0.2in; @bottom-left{{content:none}} @bottom-center{{content:none}} "
        f"@bottom-right{{content:none}} }}</style></head><body>"
        f'<p class="eyebrow">Slope ramp -- terrain token {tokens["terrain"]}, opacities light to dark, cap {landform_section.SLOPE_TINT_CAP}</p>'
        + "".join(cells) + "</body></html>"
    )
    ramp_pdf = os.path.join(out_dir, "slope-ramp.pdf")
    HTML(string=ramp_html).write_pdf(ramp_pdf)

    import pymupdf

    pymupdf.open(ramp_pdf)[0].get_pixmap(dpi=200).save(os.path.join(out_dir, "slope-ramp.png"))
    doc = pymupdf.open(pdf_path)
    for index, page in enumerate(doc, start=1):
        png = os.path.join(out_dir, f"page-{index}.png")
        page.get_pixmap(dpi=110).save(png)
    images = sum(len(p.get_images()) for p in doc)
    fonts = sorted({f[3].split("+", 1)[-1] for p in doc for f in p.get_fonts(full=True)})
    print(f"{len(doc)} pages -> {out_dir}/page-N.png; raster images {images}; fonts {fonts}")
    return 0


if __name__ == "__main__":
    positional = [a for a in sys.argv[1:] if not a.startswith("--")]
    sys.exit(main(positional[0] if positional else tempfile.mkdtemp(prefix="landform-")))
