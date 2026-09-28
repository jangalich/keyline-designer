"""
test_report_text.py

THE WORDS OF THE REPORT, HELD TO THE DATA RULE.

  1. report_text's helpers: sentences() puts exactly one space between
     clauses and none at either end; series() keeps a data part a data
     part; plural and agree; the formatters put the unit inside the span.
  2. EVERY CAPTION, SUMMARY AND STATEMENT THE WHOLE REPORT BUILDS carries
     no digit in its prose -- a figure is a data part a report_text
     formatter made, or a named term (report_text.TERMS). Asserted on the
     whole-report fixture's sections.
  3. THE SOURCE, READ: in every function that composes a caption or a
     summary, no report_text formatter is called with a literal number,
     no string literal carries a digit, and no numeric literal other than
     0 and 1 appears outside an index. A threshold that picks a word is a
     named constant.

WHAT THIS CANNOT SEE. (2) holds for the branches the fixture exercises;
(3) reads every branch, but only the functions it recognises as prose
builders -- by name, or by composing with rt.clause / rt.sentences. A
number computed inside a helper those do not reach, then passed in as a
variable, is outside both. The two together are the mechanical check the
brief asked for; they are not a proof.
"""

import ast
import contextlib
import io
import os
import re

import offline_harness

offline_harness.install()

import report_text as rt  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# ======================================================================
# 1. The helpers
# ======================================================================
print("1. report_text's helpers")
assert rt.sentences(["One. "], None, [], [" Two."], rt.clause(rt.number(3), " three. ")) == ["One. Two. ", {"value": "3"}, " three."]
assert rt.flatten(rt.sentences(["  Lead."], ["Tail.  "])) == "Lead. Tail."
assert rt.series(["a"]) == ["a"] and rt.series(["a", "b"]) == ["a and b"] and rt.series(["a", "b", "c"]) == ["a, b and c"]
kept = rt.series([[rt.text("oak"), " on ", rt.acres(1.0)], [rt.text("ash"), " on ", rt.acres(0.5)]])
assert [p for p in kept if isinstance(p, rt.Value)] == [{"value": "oak"}, {"value": "1.0 acres"}, {"value": "ash"}, {"value": "0.5 acres"}]
assert rt.plural(1, "patch", "patches") == "patch" and rt.plural(2, "patch", "patches") == "patches" and rt.plural(0, "unit") == "units"
assert rt.agree(1, "is", "are") == "is" and rt.agree(3, "is", "are") == "are"
assert rt.count(6, "wooded patch", "wooded patches") == [{"value": "6"}, " wooded patches"]
assert rt.feet(15.0) == {"value": "49 ft"} and rt.feet(4.5) == {"value": "15 ft"} and rt.feet_of(1263.4) == {"value": "1,263 ft"}
assert rt.acres(1.63) == {"value": "1.6 acres"} and rt.acres(0.01) == {"value": "<0.1 acres"} and rt.percent(37.6) == {"value": "38%"}
assert rt.inches(25.4, places=0) == {"value": "1 in"} and rt.scale(24_000) == {"value": "1:24,000"}
assert rt.year(2019) == {"value": "2019", "kind": "word"}, "a year is a label, set as prose"
assert rt.feet_text(150.0) == "492" and rt.percent_text(15.0) == "15%" and rt.whole_text(1234.5) == "1,234"
assert rt.digits_in_prose(["within 500 ft", rt.feet(150.0)]) == ["within 500 ft"]
assert rt.digits_in_prose(["the 1%-annual-chance (100-year) floodplain"]) == []
assert rt.untracked_values([{"value": "3"}, rt.number(3)]) == [{"value": "3"}]
print("   sentences, series, plural, agree, count and the formatters")

# ======================================================================
# 2. Every caption, summary and statement the report builds
# ======================================================================
print("2. no digit typed into the prose of any caption, summary or statement")
import back_matter  # noqa: E402
import site_report  # noqa: E402
import whole_report_fixture as w  # noqa: E402

with contextlib.redirect_stdout(io.StringIO()):
    BUNDLE = w.inputs()
    SECTIONS = site_report.build_sections(BUNDLE["data"], BUNDLE["terrain"], BUNDLE["water"], BUNDLE["access"], BUNDLE["trees"],
                                          BUNDLE["soils"], BUNDLE["design"], BUNDLE["overview"])
PROSE_KEY = re.compile(r"(caption|summary|statement|unavailable|imagery_line|^note$|cross_reference|profile_water|soil_test|"
                       r"^geology$|^forest_type$)")
SKIP = {"derived", "map", "structure_map", "wetness_map", "chart", "methods", "sources", "contours", "key_figures"}


def _part_lists(node, path):
    """(path, part list) for every prose part list under a section dict."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in SKIP:
                continue
            if PROSE_KEY.search(key) and isinstance(value, list) and value and all(isinstance(p, (str, dict)) for p in value):
                yield f"{path}.{key}", value
            elif PROSE_KEY.search(key) and isinstance(value, list) and value and all(isinstance(p, list) for p in value):
                for index, item in enumerate(value):
                    yield f"{path}.{key}[{index}]", item
            else:
                yield from _part_lists(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _part_lists(item, f"{path}[{index}]")


checked, failures = 0, []
back = back_matter.build_back_matter(SECTIONS, BUNDLE["data"], BUNDLE["terrain"].retrieved_on)
for section in SECTIONS + [{"name": "Back matter", "vintage_caption": back["vintage_caption"]}]:
    for path, parts in _part_lists(section, section["name"]):
        checked += 1
        typed = rt.digits_in_prose(parts)
        untracked = rt.untracked_values(parts)
        if typed or untracked:
            failures.append((path, typed, untracked))
assert not failures, "\n".join(f"{p}: typed {t} untracked {u}" for p, t, u in failures)
assert checked >= 60, checked
print(f"   {checked} part lists across {len(SECTIONS)} sections and the back matter: every figure a report_text Value")

# ======================================================================
# 3. The source of every prose builder
# ======================================================================
print("3. no formatter called with a literal, no digit in a string, no bare threshold, in every prose builder")
MODULES = ("climate_section.py", "landform_section.py", "water_section.py", "access_section.py", "trees_section.py",
           "soils_section.py", "overview_section.py", "design_section.py")
BUILDER_NAME = re.compile(r"(caption|summary|sentence|clause|statement|imagery_line|unavailable|build_forest_type$|build_geology$|"
                          r"build_profile_water|_frost_years|_water_balance|_frost_season|_stream_named|_mapped_within|"
                          r"_open_water_and_springs|build_keypoint_statement|build_profile$)")
FORMATTERS = {"number", "feet", "feet_of", "feet_range", "meters", "miles", "inches", "inches_of", "acres", "acres_adjective",
              "acres_of_m2", "percent", "percent_range", "centimeters", "share", "year", "scale", "times", "count"}
# Literal numbers that are not figures: a count's singular/plural boundary, a list's first and last, a slice.
ALLOWED_NUMBERS = {0, 1, -1}


def _is_prose_builder(func: ast.FunctionDef) -> bool:
    if "table" in func.name:
        return False
    if BUILDER_NAME.search(func.name):
        return True
    return any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "rt" and n.attr in ("clause", "sentences")
               for n in ast.walk(func))


def _docstring_nodes(func: ast.FunctionDef) -> set:
    body = func.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        return {id(body[0].value)}
    return set()


def _index_nodes(func: ast.FunctionDef) -> set:
    """Constants that are not words on the page: an index or a slice bound (names[0], s[1:]), a key (block["cells"],
    .get("atlas14")), and an f-string's format spec (:.1f)."""
    ids = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Subscript):
            for inner in ast.walk(node.slice):
                ids.add(id(inner))
        if isinstance(node, ast.FormattedValue) and node.format_spec is not None:
            for inner in ast.walk(node.format_spec):
                ids.add(id(inner))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and re.fullmatch(r"[a-z0-9_]+", node.value):
            ids.add(id(node))
    return ids


problems = []
builders = 0
for module in MODULES:
    tree = ast.parse(open(os.path.join(HERE, module), encoding="utf-8").read())
    for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if not _is_prose_builder(func):
            continue
        builders += 1
        skip = _docstring_nodes(func) | _index_nodes(func)
        for node in ast.walk(func):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id == "rt" and node.func.attr in FORMATTERS and node.args \
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, (int, float)):
                problems.append(f"{module}:{node.lineno} {func.name}: rt.{node.func.attr}({node.args[0].value!r}) formats a typed number")
            if isinstance(node, ast.Dict) and any(isinstance(k, ast.Constant) and k.value == "value" for k in node.keys):
                problems.append(f"{module}:{node.lineno} {func.name}: a data part written as a dict; use a report_text formatter")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "Value":
                problems.append(f"{module}:{node.lineno} {func.name}: a Value made by hand; use a report_text formatter")
            if isinstance(node, ast.Constant) and id(node) not in skip:
                value = node.value
                if isinstance(value, str):
                    stripped = value
                    for term in rt.TERMS:
                        stripped = stripped.replace(term, "")
                    if re.search(r"\d", stripped):
                        problems.append(f"{module}:{node.lineno} {func.name}: a digit typed into {value[:50]!r}")
                elif isinstance(value, (int, float)) and not isinstance(value, bool) and value not in ALLOWED_NUMBERS:
                    problems.append(f"{module}:{node.lineno} {func.name}: the literal {value!r}; name it")
assert not problems, "\n".join(problems)
assert builders >= 60, builders
print(f"   {builders} prose builders across {len(MODULES)} section modules read")

print("\ntest_report_text.py: all sections passed")
print(offline_harness.summary())
