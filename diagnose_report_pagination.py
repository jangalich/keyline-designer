"""
diagnose_report_pagination.py

WHERE THE REPORT'S PAGES GO -- branch 24's measurement. Per page: the
characters on it (test_whole_report's sparse-page check), how far down
the content reaches (fill %, from WeasyPrint's line, row and image
boxes), and its first words; then each section's page count and every
bound caption that is not with its block (report_layout.captions_apart).

    python3 diagnose_report_pagination.py                       # the offline fixture
    python3 diagnose_report_pagination.py --grow 3              # ... with 3 more map units
    python3 diagnose_report_pagination.py --live [boundary.json] [--save inputs.pkl]
    python3 diagnose_report_pagination.py --inputs inputs.pkl --templates DIR

  --live       creates a real session (Layer 1 and the report layer
               fetched, design_record_fixture's committed steps grafted
               on, as diagnose_report_generation_time does) on
               reference_fixture.REAL_BOUNDARY or on a boundary file's
               [[lon, lat], ...]; --save pickles every section input so
               the same live content re-renders offline.
  --inputs     re-renders a saved pickle, offline.
  --templates  a template directory to render against instead of
               templates/report -- a stylesheet variant, or an older
               commit's templates -- so layouts compare on ONE content.
  --grow N     adds N map-unit rows to the Water table (in the four-month
               form water_section takes past WATER_TABLE_TWELVE_MONTH_
               MAX_UNITS) and to both Soils unit tables: a larger parcel's
               shape, from content that is to hand.
  --pdf PATH   writes the PDF.

Changes nothing. Offline unless --live.
"""

import argparse
import copy
import json
import os
import pickle
import re
import sys
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
SPARSE_PAGE_CHARS = 600
EYEBROW = re.compile(r"^[IVX]+ · |^BACK MATTER")


def fixture_inputs() -> dict:
    import whole_report_fixture as w

    b = w.inputs()
    keys = ("terrain", "water", "access", "trees", "soils", "design", "overview")
    return dict({k: b[k] for k in keys}, data=b["data"], generated_on=w.GENERATED_ON)


def live_inputs(boundary) -> dict:
    """A real session, rendered once through the report job's own path; the
    inputs are caught on their way to generate_site_report_pdf."""
    import tempfile

    import document_store
    import report_data
    import session_cache
    import session_manager
    import site_report

    captured = {}
    real = site_report.generate_site_report_pdf

    def capture(data, output_path, **kw):
        captured.update(kw, data=data)
        return real(data, output_path, **kw)

    site_report.generate_site_report_pdf = capture
    try:
        out = tempfile.mkdtemp(prefix="pagination-")
        store = document_store.JSONFileStore(os.path.join(out, "store"))
        fetch_cache, cache = session_cache.FetchCache(), session_cache.SessionCache()
        with open(os.path.join(HERE, "design_record_fixture.json"), encoding="utf-8") as handle:
            steps = json.load(handle)["full"]["document"]["steps"]
        document = session_manager.create_session([list(p) for p in boundary], store, fetch_cache=fetch_cache, cache=cache)
        store.put(dict(document, steps=steps))
        site_report.generate_session_site_report_pdf(
            document["session_id"], store, os.path.join(out, "site-report.pdf"),
            report_fetch_cache=session_cache.FetchCache(fetch_function=report_data.fetch_report_data),
            generated_on=date.today(), fetch_cache=fetch_cache, cache=cache)
    finally:
        site_report.generate_site_report_pdf = real
    captured.pop("property_label", None)
    captured.pop("complete", None)
    return captured


def grow_sections(build, extra: int):
    """build_sections, with `extra` more map-unit rows where a parcel's unit
    count sets a table's length."""
    import water_section as ws

    def grown(*args):
        sections = build(*args)
        for section in sections:
            name = section.get("template", "")
            tables = []
            if name.startswith("water") and section.get("water_table"):
                table = section["water_table"]
                if len(table["columns"]) == 12:
                    table.update(monthly=False, compact=True,
                                 columns=[ws.MONTH_NAMES[m - 1] for m in ws.REPRESENTATIVE_MONTHS])
                    for row in table["rows"]:
                        row["cells"] = [row["cells"][m - 1] for m in ws.REPRESENTATIVE_MONTHS]
                tables.append((table, 4))  # the four parcel rows stay last
            if name.startswith("soils"):
                tables += [(section[k], 0) for k in ("map_unit_table", "properties_table") if section.get(k)]
            for table, tail in tables:
                rows = table["rows"]
                body, end = rows[:len(rows) - tail], rows[len(rows) - tail:]
                table["rows"] = body + [copy.deepcopy(body[-1]) for _ in range(extra)] + end
        return sections

    return grown


def _walk(box):
    yield box
    for child in getattr(box, "children", []) or []:
        yield from _walk(child)


def measure(inputs: dict, templates: str, pdf_path=None) -> dict:
    import pymupdf
    from weasyprint import HTML

    import report_layout
    import site_report

    kw = dict(inputs)
    data, generated_on = kw.pop("data"), kw.pop("generated_on", date.today())
    env = site_report.jinja_environment(templates)
    html = site_report.render_site_report_html(data, property_label="5614 N Montour Rd", generated_on=generated_on,
                                               env=env, complete=True, **kw)
    document = HTML(string=html, base_url=templates).render()
    pdf = document.write_pdf()
    if pdf_path:
        with open(pdf_path, "wb") as handle:
            handle.write(pdf)
    texts = [page.get_text() for page in pymupdf.open(stream=pdf, filetype="pdf")]
    pages, sections, current = [], {}, None
    for number, (text, page) in enumerate(zip(texts, document.pages), start=1):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        eyebrow = next((line for line in lines[:3] if EYEBROW.match(line)), None)
        if eyebrow:
            current = eyebrow.replace(", CONTINUED", "")
        sections[current or "Cover"] = sections.get(current or "Cover", 0) + 1
        box = page._page_box
        top = box.content_box_y()
        # The page's own content, not its margin boxes (the running footer).
        body = [child for child in box.children if type(child).__name__ != "MarginBox"]
        bottom = max((b.position_y + b.margin_height() for child in body for b in _walk(child)
                      if type(b).__name__ in ("LineBox", "BlockReplacedBox", "InlineReplacedBox", "TableRowBox")),
                     default=top)
        pages.append({"page": number, "chars": len(text), "fill": round(100 * (bottom - top) / box.height),
                      "section": current or "Cover", "first": " / ".join(lines[:3])[:80]})
    return {"pages": pages, "sections": sections, "captions_apart": report_layout.captions_apart(document)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", nargs="?", const="", default=None)
    parser.add_argument("--save")
    parser.add_argument("--inputs")
    parser.add_argument("--templates", default=os.path.join(HERE, "templates", "report"))
    parser.add_argument("--grow", type=int, default=0)
    parser.add_argument("--pdf")
    args = parser.parse_args()

    if args.live is None:
        import offline_harness

        offline_harness.install()
    import site_report

    if args.inputs:
        with open(args.inputs, "rb") as handle:
            inputs = pickle.load(handle)
    elif args.live is not None:
        from reference_fixture import REAL_BOUNDARY

        boundary = REAL_BOUNDARY
        if args.live:
            with open(args.live, encoding="utf-8") as handle:
                boundary = json.load(handle)
        inputs = live_inputs(boundary)
    else:
        inputs = fixture_inputs()
    if args.save:
        with open(args.save, "wb") as handle:
            pickle.dump(inputs, handle)
    if args.grow:
        site_report.build_sections = grow_sections(site_report.build_sections, args.grow)

    result = measure(inputs, args.templates, args.pdf)
    print(f"{len(result['pages'])} pages  ({args.templates}, grow {args.grow})")
    for p in result["pages"]:
        flag = "SPARSE" if p["chars"] < SPARSE_PAGE_CHARS else ""
        print(f"  p{p['page']:>2} {p['chars']:>5} chars {p['fill']:>3}% {flag:<6} {p['first']}")
    print("sections: " + "; ".join(f"{name} {count}" for name, count in result["sections"].items()))
    print(f"captions apart from their block: {result['captions_apart'] or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
