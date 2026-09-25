"""
diagnose_report_generation_time.py

HOW LONG THE SITE DATA REPORT TAKES, BY STAGE, LIVE -- the measurement
behind switching the report job from the narrated generator to
site_report.generate_session_site_report_pdf(). Changes nothing: every
hook below wraps a function and records, and the calls run exactly as
they would on the job.

    python3 diagnose_report_generation_time.py warm [out_dir]
    python3 diagnose_report_generation_time.py cold [out_dir]

THE SESSION. A real session on reference_fixture.REAL_BOUNDARY, created
live (Layer 1 fetched, terrain warm-up run), with design_record_fixture.
json's committed "full" steps grafted onto its document -- branch 14's
committed fixture, the same design whole_report_fixture renders. The
report reads committed steps off the document, never the cache, so the
graft is what a user who committed all six would have.

THE TWO SCENARIOS.
  warm  the session context is in the session cache, as it is the moment
        the last step is committed. The report-layer cache is empty: the
        report layer is fetched at report time in both scenarios.
  cold  the session cache AND the Layer 1 fetch cache are emptied first,
        so get_session_context() rebuilds from the Design Document and
        refetches Layer 1 -- the user who comes back the next day.

WHAT IS RECORDED. Per-layer fetch times through run_diagnostics.
time_layer (the hook parcel_data and report_data already carry); every
HTTP response through requests.Session.send (host, bytes, seconds, the
layer it was fetched for); inclusive and exclusive times for the section
inputs, the section builders, every SVG map and chart, the back matter,
the HTML assembly and WeasyPrint's parse, layout and PDF write; and call
counts with call sites for the heavy derivations the report is meant to
run once. Prints a report and writes timings.json beside the PDF.

LIVE NETWORK, BY DESIGN: this is the one diagnostic that must not install
offline_harness. Report-layer times vary with the services' own load; run
it more than once before trusting a single fetch's figure.
"""

import collections
import functools
import inspect
import json
import os
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

_T0 = time.perf_counter()
import requests  # noqa: E402

import design_document  # noqa: E402
import document_store  # noqa: E402
import report_data  # noqa: E402
import run_diagnostics  # noqa: E402
import session_cache  # noqa: E402
import session_manager  # noqa: E402
import site_report  # noqa: E402
from reference_fixture import REAL_BOUNDARY  # noqa: E402
_IMPORT_APP_SECONDS = time.perf_counter() - _T0
_T0 = time.perf_counter()
import weasyprint  # noqa: E402
_IMPORT_WEASYPRINT_SECONDS = time.perf_counter() - _T0

HERE = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------
# The recorder
# ----------------------------------------------------------------------

_LOCAL = threading.local()
RECORDS = []          # {"kind", "name", "inclusive", "exclusive", "caller", "start"}
CALLS = collections.defaultdict(list)   # counted name -> [caller, ...]
HTTP = []             # {"layer", "host", "path", "bytes", "seconds", "status"}
LAYERS = []           # {"group", "layer", "seconds", "error"}
_RUN_START = [0.0]


def _stack():
    stack = getattr(_LOCAL, "stack", None)
    if stack is None:
        stack = _LOCAL.stack = []
    return stack


def _caller(skip_files=()):
    """file:line function of the first frame outside this module and
    outside `skip_files` -- the call site."""
    frame = inspect.currentframe()
    here = os.path.abspath(__file__)
    while frame is not None:
        path = os.path.abspath(frame.f_code.co_filename)
        if path != here and os.path.basename(path) not in skip_files and "functools" not in path:
            return f"{os.path.basename(path)}:{frame.f_lineno} {frame.f_code.co_name}"
        frame = frame.f_back
    return "?"


def _replace_everywhere(original, replacement):
    """Every loaded module attribute bound to `original` -> `replacement`,
    so a `from x import f` binding is wrapped as well as `x.f`."""
    for module in list(sys.modules.values()):
        namespace = getattr(module, "__dict__", None)
        if not namespace:
            continue
        for key, value in list(namespace.items()):
            if value is original:
                setattr(module, key, replacement)


def timed(owner, attribute, kind, name=None, count=False, skip_files=()):
    original = getattr(owner, attribute)
    label = name or f"{getattr(owner, '__name__', owner)}.{attribute}"

    @functools.wraps(original)
    def wrapper(*args, **kwargs):
        caller = _caller(skip_files)
        if count:
            CALLS[label].append(caller)
        entry = {"kind": kind, "name": label, "caller": caller, "child": 0.0,
                 "start": time.perf_counter() - _RUN_START[0]}
        stack = _stack()
        stack.append(entry)
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            elapsed = time.perf_counter() - started
            stack.pop()
            if stack:
                stack[-1]["child"] += elapsed
            entry["inclusive"] = elapsed
            entry["exclusive"] = elapsed - entry.pop("child")
            RECORDS.append(entry)

    if inspect.isclass(owner):
        setattr(owner, attribute, wrapper)
    else:
        _replace_everywhere(original, wrapper)
    return wrapper


class _LayerTimer:
    def __init__(self, layer, function):
        self.layer = layer
        self.group = "report" if layer in report_data.REPORT_FETCH_LAYERS else "layer1"

    def __enter__(self):
        self.started = time.perf_counter()
        _LOCAL.layer = self.layer
        return self

    def __exit__(self, exc_type, exc, tb):
        LAYERS.append({"group": self.group, "layer": self.layer,
                       "seconds": time.perf_counter() - self.started,
                       "error": None if exc is None else f"{type(exc).__name__}: {str(exc)[:160]}"})
        _LOCAL.layer = None
        return False


def _install_http_hook():
    send = requests.Session.send

    def recording_send(self, request, **kwargs):
        started = time.perf_counter()
        response = send(self, request, **kwargs)
        # .content forces the body in when the caller streams; the time
        # to read it is part of the fetch either way.
        size = len(response.content)
        parts = urlsplit(request.url)
        HTTP.append({"layer": getattr(_LOCAL, "layer", None), "host": parts.netloc, "path": parts.path[:90],
                     "bytes": size, "seconds": time.perf_counter() - started, "status": response.status_code})
        return response

    requests.Session.send = recording_send


def install_instrumentation():
    import access_derivations
    import access_section
    import back_matter
    import climate_section
    import contour_lines
    import contourpy
    import design_section
    import exclusion_zones
    import keypoint_detection
    import landform_derivations
    import landform_section
    import overview_derivations
    import overview_section
    import parcel_data
    import production_area
    import report_chart
    import report_map
    import road_corridors
    import soils_derivations
    import soils_section
    import terrain_metrics
    import trees_derivations
    import trees_section
    import valley_delineation
    import water_derivations
    import water_section

    run_diagnostics.time_layer = lambda layer, function=None: _LayerTimer(layer, function)
    _install_http_hook()

    # Stages
    timed(session_manager, "get_session_context", "stage", "session context")
    timed(session_cache, "build_session_context", "stage", "build_session_context")
    timed(parcel_data, "fetch_parcel_data", "stage", "Layer 1 fetch")
    timed(session_cache, "run_terrain_warm_up", "stage", "terrain warm-up")
    timed(report_data, "fetch_report_data", "stage", "report layer fetch")
    timed(site_report, "render_site_report_html", "stage", "render_site_report_html")
    timed(site_report, "build_sections", "stage", "build_sections")
    timed(site_report, "render_stylesheet", "stage", "render_stylesheet")
    timed(back_matter, "build_back_matter", "stage", "back matter")
    timed(weasyprint.HTML, "__init__", "weasy", "WeasyPrint parse (HTML())")
    timed(weasyprint.HTML, "render", "weasy", "WeasyPrint layout (HTML.render)")
    timed(weasyprint.document.Document, "write_pdf", "weasy", "WeasyPrint PDF write (Document.write_pdf)")

    # Warm-up internals
    timed(exclusion_zones, "identify_exclusion_zones", "warmup", "identify_exclusion_zones", count=True)
    timed(road_corridors, "_fetch_floodplain_hydric_unions", "warmup", "floodplain/hydric unions")

    # Section inputs
    for module, attribute in ((landform_section, "terrain_inputs_from_context"),
                              (water_derivations, "water_inputs_from_context"),
                              (access_derivations, "access_inputs_from_context"),
                              (trees_derivations, "trees_inputs_from_context"),
                              (soils_derivations, "soils_inputs_from_context"),
                              (overview_derivations, "overview_inputs_from_context"),
                              (design_section, "design_inputs_from_context")):
        timed(module, attribute, "inputs", f"{module.__name__}.{attribute}", count=True)

    # Section builders (exclusive of the SVG renders inside them)
    for module, attribute in ((overview_section, "build_overview_section"),
                              (climate_section, "build_climate_section"),
                              (landform_section, "build_landform_section"),
                              (water_section, "build_water_section"),
                              (access_section, "build_access_section"),
                              (trees_section, "build_trees_section"),
                              (soils_section, "build_soils_section"),
                              (design_section, "build_design_section")):
        timed(module, attribute, "section", attribute.replace("build_", "").replace("_section", ""), count=True)

    # Section derive() entry points
    for module in (landform_derivations, access_derivations, trees_derivations, soils_derivations,
                   overview_derivations):
        timed(module, "derive", "derive", f"{module.__name__}.derive", count=True)
    if hasattr(water_derivations, "derive"):
        timed(water_derivations, "derive", "derive", "water_derivations.derive", count=True)

    # SVG
    timed(report_map, "render_map", "svg", "report_map.render_map")
    for chart in ("render_water_balance", "render_valley_profile", "render_wind_roses"):
        timed(report_chart, chart, "svg", f"report_chart.{chart}")

    # The redundancy check: counted
    counted = [
        (valley_delineation, "fill_and_resolve"),
        (valley_delineation, "compute_flow_direction"),
        (valley_delineation, "compute_flow_accumulation"),
        (landform_derivations, "flow_pass"),
        (production_area, "compute_slope_percent"),
        (terrain_metrics, "compute_slope_and_aspect"),
        (valley_delineation, "delineate_valleys"),
        (keypoint_detection, "detect_keypoints"),
        (water_derivations, "grid_bookkeeping"),
        (water_derivations, "derive_hydric"),
        (contour_lines, "compute_contour_lines"),
        (report_map, "parcel_contours"),
        (landform_derivations, "contour_generator"),
    ]
    for module, attribute in counted:
        timed(module, attribute, "counted", f"{module.__name__}.{attribute}", count=True,
              skip_files=("contour_lines.py",) if attribute == "compute_contour_lines" else ())
    timed(contourpy, "contour_generator", "counted", "contourpy.contour_generator", count=True)

    # Every report-layer fetch function, counted wherever it is called from
    fetchers = {
        "daymet_daily": (report_data, "get_daymet_daily_for_point"),
        "atlas14": (report_data, "get_atlas14_for_point"),
        "power_wind": (report_data, "get_power_wind_for_point"),
    }
    import bedrock_geology, census_geography, context_map_data, forest_type_data, hydrology_data  # noqa: E401
    import naip_imagery, nfhl_data, nhdplus_data, nlcd_landcover_data, nwi_data, soil_road_ratings  # noqa: E401
    import soil_survey, soil_water_table, soil_woodland, structures_data, transmission_lines  # noqa: E401
    fetchers.update({
        "nhd_points": (hydrology_data, "get_nhd_points_for_boundary"),
        "nhdplus_hr": (nhdplus_data, "get_flowline_attributes_for_boundary"),
        "nwi": (nwi_data, "get_wetlands_for_boundary"),
        "fema_nfhl": (nfhl_data, "get_flood_hazard_for_boundary"),
        "nlcd_landcover": (nlcd_landcover_data, "get_land_cover_for_boundary"),
        "soil_water_table": (soil_water_table, "get_seasonal_water_table_for_boundary"),
        "soil_road_ratings": (soil_road_ratings, "get_road_ratings_for_boundary"),
        "forest_type_group": (forest_type_data, "get_forest_type_for_boundary"),
        "soil_woodland": (soil_woodland, "get_woodland_for_boundary"),
        "soil_survey": (soil_survey, "get_survey_for_boundary"),
        "bedrock_geology": (bedrock_geology, "get_geology_for_boundary"),
        "naip_imagery": (naip_imagery, "get_naip_for_boundary"),
        "context_dem": (context_map_data, "get_context_dem_for_boundary"),
        "context_water": (context_map_data, "get_context_water_for_boundary"),
        "context_roads": (context_map_data, "get_context_roads_for_boundary"),
        "county_state": (census_geography, "get_county_state_for_point"),
        "structures": (structures_data, "get_structures_for_boundary"),
        "transmission_lines": (transmission_lines, "get_transmission_lines_near_boundary"),
    })
    for layer, (module, attribute) in fetchers.items():
        timed(module, attribute, "fetchfn", f"fetch:{layer}", count=True)


# ----------------------------------------------------------------------
# The session
# ----------------------------------------------------------------------


def committed_steps() -> dict:
    with open(os.path.join(HERE, "design_record_fixture.json"), encoding="utf-8") as handle:
        return json.load(handle)["full"]["document"]["steps"]


def make_session(store, fetch_cache, cache) -> tuple:
    started = time.perf_counter()
    document = session_manager.create_session([list(p) for p in REAL_BOUNDARY], store,
                                              fetch_cache=fetch_cache, cache=cache)
    created = time.perf_counter() - started
    document = dict(document, steps=committed_steps())
    store.put(document)
    return document["session_id"], created


# ----------------------------------------------------------------------
# The report
# ----------------------------------------------------------------------


def _sum(kind=None, name=None, field="inclusive", records=None):
    rows = records if records is not None else RECORDS
    return sum(r[field] for r in rows if (kind is None or r["kind"] == kind) and (name is None or r["name"] == name))


def summarise(scenario: str, total: float, pdf_bytes: int, pages: int, created: float) -> dict:
    by_name = collections.OrderedDict()
    for r in sorted(RECORDS, key=lambda r: r["start"]):
        by_name.setdefault((r["kind"], r["name"]), []).append(r)

    svg_rows = []
    for r in sorted((r for r in RECORDS if r["kind"] == "svg"), key=lambda r: r["start"]):
        section = next((s["name"] for s in RECORDS if s["kind"] == "section"
                        and s["start"] <= r["start"] <= s["start"] + s["inclusive"]), "?")
        svg_rows.append({"section": section, "renderer": r["name"].split(".")[-1], "caller": r["caller"],
                         "seconds": r["inclusive"]})

    def within(outer, kind):
        return [r for r in RECORDS if r["kind"] == kind and r is not outer
                and outer["start"] <= r["start"] <= outer["start"] + outer["inclusive"]]

    sections = []
    for r in sorted((r for r in RECORDS if r["kind"] == "section"), key=lambda r: r["start"]):
        svg = sum(x["inclusive"] for x in within(r, "svg"))
        parts = collections.Counter()
        for x in within(r, "counted") + within(r, "derive"):
            parts[x["name"]] += x["inclusive"]
        sections.append({"section": r["name"], "inclusive": r["inclusive"], "svg": svg,
                         "exclusive_of_svg": r["inclusive"] - svg,
                         "parts": dict(parts.most_common(6))})
    inputs = [{"name": r["name"].split(".")[-1], "seconds": r["inclusive"]}
              for r in sorted((r for r in RECORDS if r["kind"] == "inputs"), key=lambda r: r["start"])]

    html_total = _sum("stage", "render_site_report_html")
    build_sections = _sum("stage", "build_sections")
    back = _sum("stage", "back matter")
    stylesheet = _sum("stage", "render_stylesheet")

    report_http = [h for h in HTTP if h["layer"] in report_data.REPORT_FETCH_LAYERS]
    layer1_http = [h for h in HTTP if h not in report_http]
    counts = {name: {"count": len(callers), "callers": dict(collections.Counter(callers))}
              for name, callers in sorted(CALLS.items())}
    return {
        "scenario": scenario,
        "imports": {"app_modules": _IMPORT_APP_SECONDS, "weasyprint": _IMPORT_WEASYPRINT_SECONDS},
        "session_creation_untimed_setup": created,
        "total": total,
        "session_context": _sum("stage", "session context"),
        "session_context_hit": _sum("stage", "build_session_context") == 0,
        "layer1_fetch": _sum("stage", "Layer 1 fetch"),
        "terrain_warm_up": _sum("stage", "terrain warm-up"),
        "warm_up_parts": {r["name"]: r["inclusive"] for r in RECORDS if r["kind"] == "warmup"},
        "layers": LAYERS,
        "report_layer_fetch": _sum("stage", "report layer fetch"),
        "report_layer_timed_sum": sum(l["seconds"] for l in LAYERS if l["group"] == "report"),
        "inputs": inputs,
        "sections": sections,
        "svg": svg_rows,
        "build_sections": build_sections,
        "back_matter": back,
        "stylesheet": stylesheet,
        "html_assembly_jinja": html_total - build_sections - back - stylesheet,
        "render_site_report_html": html_total,
        "weasy_parse": _sum("weasy", "WeasyPrint parse (HTML())"),
        "weasy_layout": _sum("weasy", "WeasyPrint layout (HTML.render)"),
        "weasy_write": _sum("weasy", "WeasyPrint PDF write (Document.write_pdf)"),
        "counts": counts,
        "http": {
            "report_requests": len(report_http), "report_bytes": sum(h["bytes"] for h in report_http),
            "layer1_requests": len(layer1_http), "layer1_bytes": sum(h["bytes"] for h in layer1_http),
            "largest": sorted(HTTP, key=lambda h: -h["bytes"])[:8],
            "by_layer": {layer: {"requests": len(rows), "bytes": sum(h["bytes"] for h in rows),
                                 "seconds": sum(h["seconds"] for h in rows)}
                         for layer, rows in _group(HTTP, "layer").items()},
        },
        "pdf_bytes": pdf_bytes,
        "pages": pages,
    }


def _group(rows, key):
    grouped = collections.OrderedDict()
    for row in rows:
        grouped.setdefault(str(row[key]), []).append(row)
    return grouped


HTTP_ROWS = HTTP


def print_summary(s: dict) -> None:
    f = lambda x: f"{x:8.2f} s"  # noqa: E731
    print("=" * 78)
    print(f"SCENARIO: {s['scenario'].upper()}   total wall clock {s['total']:.2f} s   "
          f"PDF {s['pdf_bytes']:,} bytes, {s['pages']} pages")
    print("=" * 78)
    print(f"(imports, not in total: app {s['imports']['app_modules']:.2f} s, weasyprint "
          f"{s['imports']['weasyprint']:.2f} s; session creation for setup {s['session_creation_untimed_setup']:.1f} s)")
    print(f"session context     {f(s['session_context'])}   {'HIT' if s['session_context_hit'] else 'REBUILD'}")
    if not s["session_context_hit"]:
        print(f"  Layer 1 fetch     {f(s['layer1_fetch'])}")
        for l in s["layers"]:
            if l["group"] == "layer1":
                print(f"    {l['layer']:<24}{f(l['seconds'])}  {l['error'] or ''}")
        print(f"  terrain warm-up   {f(s['terrain_warm_up'])}")
        for name, sec in s["warm_up_parts"].items():
            print(f"    {name:<30}{f(sec)}")
    print(f"report layer        {f(s['report_layer_fetch'])}  (serial; per-layer sum {s['report_layer_timed_sum']:.2f} s)")
    by_layer = s["http"]["by_layer"]
    for l in s["layers"]:
        if l["group"] == "report":
            h = by_layer.get(l["layer"], {"requests": 0, "bytes": 0})
            print(f"    {l['layer']:<22}{f(l['seconds'])}  {h['requests']:>3} req {h['bytes']:>12,} B  {l['error'] or ''}")
    print("section inputs")
    for row in s["inputs"]:
        print(f"    {row['name']:<34}{f(row['seconds'])}")
    print("sections (derivations = exclusive of SVG)")
    for row in s["sections"]:
        print(f"    {row['section']:<14} derivations {f(row['exclusive_of_svg'])}   SVG {f(row['svg'])}"
              f"   total {f(row['inclusive'])}")
        for name, sec in row["parts"].items():
            if sec >= 0.05:
                print(f"        {name:<44}{f(sec)} (inclusive)")
    print("SVG maps and charts")
    for row in s["svg"]:
        print(f"    {row['section']:<10} {row['renderer']:<22}{f(row['seconds'])}   {row['caller']}")
    print(f"back matter         {f(s['back_matter'])}")
    print(f"HTML assembly       {f(s['html_assembly_jinja'] + s['stylesheet'])}  (jinja + stylesheet)")
    print(f"WeasyPrint parse    {f(s['weasy_parse'])}")
    print(f"WeasyPrint layout   {f(s['weasy_layout'])}")
    print(f"WeasyPrint write    {f(s['weasy_write'])}")
    print(f"TOTAL               {f(s['total'])}")
    print("\nCALL COUNTS (whole report)")
    for name, row in s["counts"].items():
        flag = "  <-- more than once" if row["count"] > 1 else ""
        print(f"  {row['count']:>3}  {name}{flag}")
        if row["count"] > 1:
            for caller, n in row["callers"].items():
                print(f"         {n} x {caller}")
    h = s["http"]
    print(f"\nHTTP via requests: report layer {h['report_requests']} requests {h['report_bytes']:,} B; "
          f"Layer 1 {h['layer1_requests']} requests {h['layer1_bytes']:,} B")
    print("  every request, in order")
    for row in HTTP_ROWS:
        print(f"    {row['layer'] or '-':<20}{row['status']:>4} {row['seconds']:6.2f} s {row['bytes']:>10,} B  "
              f"{row['host']}{row['path'][:60]}")
    print("  largest")
    for row in h["largest"]:
        print(f"    {row['bytes']:>12,} B {row['seconds']:6.2f} s  {row['layer']}  {row['host']}{row['path']}")


def main(scenario: str, out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    store = document_store.JSONFileStore(os.path.join(out_dir, "store"))
    fetch_cache = session_cache.FetchCache()
    cache = session_cache.SessionCache()

    # SETUP RETRIES, NOT THE PRODUCT'S. Open-Meteo (Layer 1's climate
    # summary) answers 429, or drops the TLS connection, intermittently
    # from a shared egress address, and either hard-fails a session
    # creation. Setup is not what is being measured, so it is retried
    # here; a failure inside the timed report is not retried and is
    # reported as what it is.
    for attempt in range(1, 11):
        try:
            session_id, created = make_session(store, fetch_cache, cache)
            break
        except requests.exceptions.RequestException as exc:
            print(f"setup attempt {attempt}: session creation failed ({type(exc).__name__}: {str(exc)[:120]}); "
                  f"waiting 20 s")
            time.sleep(20)
    else:
        raise SystemExit("could not create the session")
    if scenario == "cold":
        cache.clear()
        fetch_cache.clear()

    install_instrumentation()
    report_cache = session_cache.FetchCache(fetch_function=report_data.fetch_report_data)
    pdf_path = os.path.join(out_dir, f"site-report-{scenario}.pdf")
    _RUN_START[0] = time.perf_counter()
    started = time.perf_counter()
    site_report.generate_session_site_report_pdf(session_id, store, pdf_path, property_label="5614 N Montour Rd",
                                                 report_fetch_cache=report_cache, fetch_cache=fetch_cache,
                                                 cache=cache)
    total = time.perf_counter() - started

    try:
        import pymupdf
        pages = len(pymupdf.open(pdf_path))
    except ImportError:
        pages = -1
    summary = summarise(scenario, total, os.path.getsize(pdf_path), pages, created)
    print_summary(summary)
    with open(os.path.join(out_dir, f"timings-{scenario}.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, default=str)
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("warm", "cold"):
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp(prefix="report-time-")))
