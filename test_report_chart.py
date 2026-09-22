"""
test_report_chart.py

Offline checks for report_chart.py -- the water balance diagram and the
wind roses -- against the measurements each render returns, and the
rendered SVG where a fact lives only there (the labels, the fonts, the
colours). No network is needed; offline_harness is installed to prove
the render reaches none.

  1. NO COLOUR LITERAL in the module; every colour in a rendered SVG is
     a value of the tokens dict passed in, and a token the chart needs
     but the dict lacks raises rather than falling back.
  2. THE BANDS: on a constructed year the crossings land where linear
     interpolation puts them, consecutive months of one sign merge into
     one band, a year of surplus is one band and no crossing, and a band
     always runs upper edge over lower edge.
  3. THE AXIS: y runs from zero to the next tick above the larger
     series; months sit at twelve equal x positions across the plot;
     month labels are in the prose face and tick labels in the data
     face; the unit rides beside the top tick value.
  4. THE ROSES: sector order clockwise from north, a wedge's radius in
     proportion to its share against a ring scale shared by both roses,
     the LONGEST wedge filled solid (a caller naming another sector as
     prevailing is refused) and the others tinted, the four
     cardinal letters placed outside the rings at their compass angles,
     "wind from" under each title, the speed label set in the data face.
  5. THE LEGEND is legend_entries()' shape, four entries in the order
     precipitation, evaporation, surplus, deficit, each with a swatch
     drawn in the chart's own colours.
"""

import math
import re

from xml.dom import minidom

import offline_harness

offline_harness.install()

import report_chart
from report_chart import balance_bands, profile_exaggeration, render_valley_profile, render_water_balance, render_wind_roses

TOKENS = {
    "page": "#ffffff", "stock": "#f4f1ea", "rule": "#ddd6c8", "ink": "#2b2b26", "ink-muted": "#8a8477",
    "oxide": "#9c4a2f", "terrain": "#7a5c3a", "water": "#3f5d75", "ochre": "#c99a2e",
}
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
MONTHS = list("JFMAMJJASOND")


def _close(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ======================================================================
# 1. No colour literal
# ======================================================================
print("1. no colour literal in the module; rendered colours are the tokens' values; a missing token raises")
with open(report_chart.__file__, encoding="utf-8") as handle:
    assert HEX.findall(handle.read()) == []
balance = render_water_balance(MONTHS, [3.0] * 12, [1.0] * 12, "in", TOKENS)
roses = render_wind_roses(
    [{"title": "Winter", "months": "Dec–Feb", "sectors": list("N NE E SE S SW W NW".split()),
      "frequency": {s: 0.125 for s in "N NE E SE S SW W NW".split()}, "prevailing": None, "speed_label": "6.1 mph"}],
    TOKENS,
)
for svg in (balance["svg"], roses["svg"]):
    used = set(HEX.findall(svg))
    assert used and used <= set(TOKENS.values()), used - set(TOKENS.values())
try:
    render_water_balance(MONTHS, [3.0] * 12, [1.0] * 12, "in", {k: v for k, v in TOKENS.items() if k != "water"})
except KeyError as exc:
    assert "water" in str(exc)
else:
    raise AssertionError("a chart must not invent a colour for a token the dict lacks")
assert len(offline_harness.refused()) == 0
print("   module clean; SVG colours are a subset of the tokens; KeyError names the missing token")

# ======================================================================
# 2. The bands
# ======================================================================
print("2. crossings by linear interpolation; one band per run of sign")
P = [4.0, 4.0, 4.0, 4.0, 4.0, 2.0, 2.0, 2.0, 4.0, 4.0, 4.0, 4.0]
E = [1.0, 1.0, 1.0, 1.0, 3.0, 3.0, 3.0, 3.0, 3.0, 1.0, 1.0, 1.0]
bands = balance_bands(P, E)
assert [b["sign"] for b in bands] == ["surplus", "deficit", "surplus"], [b["sign"] for b in bands]
# May (index 4): P 4 > E 3; June: P 2 < E 3. d0 = 1, d1 = -1 -> crossing halfway, at x 4.5, y 3.0.
first = bands[0]["upper"][-1]
assert _close(first[0], 4.5) and _close(first[1], 3.0), first
# August (7): 2 < 3; September (8): 4 > 3. d0 = -1, d1 = 1 -> halfway, x 7.5, y 3.0.
second = bands[1]["upper"][-1]
assert _close(second[0], 7.5) and _close(second[1], 3.0), second
assert bands[1]["upper"][0] == first and bands[2]["upper"][0] == second, "a band starts where the last ended"
for band in bands:
    assert len(band["upper"]) == len(band["lower"])
    assert all(u[1] >= l[1] and _close(u[0], l[0]) for u, l in zip(band["upper"], band["lower"])), band["sign"]
    assert [x for x, _ in band["upper"]] == sorted(x for x, _ in band["upper"])
rendered = render_water_balance(MONTHS, P, E, "in", TOKENS)
assert [round(x, 6) for x in rendered["crossings"]] == [4.5, 7.5]
all_surplus = balance_bands([3.0] * 12, [1.0] * 12)
assert len(all_surplus) == 1 and all_surplus[0]["sign"] == "surplus" and len(all_surplus[0]["upper"]) == 12
# An unequal crossing: P 4 -> 1 against E 2 -> 2: d0 = 2, d1 = -1, t = 2/3.
uneven = balance_bands([4.0, 1.0] + [1.0] * 10, [2.0] * 12)
assert _close(uneven[0]["upper"][-1][0], 2.0 / 3.0) and _close(uneven[0]["upper"][-1][1], 2.0)
print(f"   crossings at {rendered['crossings']} on the constructed year; 12-month surplus is one band")

# ======================================================================
# 3. The axis
# ======================================================================
print("3. the y axis, the month positions, the faces")
assert rendered["y_max"] == 4.0 and rendered["tick_step"] == 0.5 and rendered["ticks"][-1] == 4.0
tall = render_water_balance(MONTHS, [5.56] * 12, [0.2] * 12, "in", TOKENS)
assert tall["y_max"] == 6.0 and tall["tick_step"] == 1.0 and tall["ticks"] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
x0, y0, x1, y1 = rendered["plot"]
assert _close(rendered["points_per_month"], (x1 - x0) / 11.0)
assert rendered["frame"] == report_chart.BALANCE_FRAME and x1 < rendered["frame"][0] and y1 < rendered["frame"][1]
svg = rendered["svg"]
month_texts = re.findall(r'<text[^>]*font-family="Source Serif 4"[^>]*>([A-Z])</text>', svg)
assert month_texts == MONTHS, month_texts
tick_texts = re.findall(r'<text[^>]*font-family="IBM Plex Mono"[^>]*>([\d.]+(?: in)?)</text>', svg)
assert tick_texts == [f"{t:g}" for t in rendered["ticks"][:-1]] + [f"{rendered['ticks'][-1]:g} in"], tick_texts
assert svg.count(">in</text>") == 0, "the unit rides beside the top value, never on its own above it"
assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"') and 'viewBox="0 0 489.60 212.00"' in svg
# Fills: the surplus band in water, the deficit band in ochre; lines the same.
assert svg.count(f'fill="{TOKENS["water"]}"') == 2 and svg.count(f'fill="{TOKENS["ochre"]}"') == 1
assert f'stroke="{TOKENS["water"]}"' in svg and f'stroke="{TOKENS["ochre"]}"' in svg
try:
    render_water_balance(MONTHS[:11], P, E, "in", TOKENS)
except ValueError:
    pass
else:
    raise AssertionError("eleven months must be refused")
print(f"   y to {rendered['y_max']} by {rendered['tick_step']}; 12 prose month labels, {len(tick_texts)} data tick labels")

# ======================================================================
# 4. The roses
# ======================================================================
print("4. the roses: sector order, radii, prevailing fill, compass letters, 'wind from'")
sectors = "N NE E SE S SW W NW".split()
winter = {s: f for s, f in zip(sectors, [0.04, 0.03, 0.04, 0.08, 0.14, 0.24, 0.30, 0.13])}
summer = {s: f for s, f in zip(sectors, [0.09, 0.06, 0.06, 0.08, 0.14, 0.25, 0.19, 0.13])}
seasons = [
    {"title": "Winter", "months": "Dec–Feb", "sectors": sectors, "frequency": winter, "prevailing": "W", "speed_label": "6.1 mph"},
    {"title": "Summer", "months": "Jun–Aug", "sectors": sectors, "frequency": summer, "prevailing": "SW", "speed_label": "3.6 mph"},
]
two = render_wind_roses(seasons, TOKENS)
assert two["ring_max"] == 0.3 and two["rings"] == [0.1, 0.2, 0.3]
assert [r["title"] for r in two["roses"]] == ["Winter", "Summer"]
w, s = two["roses"]
assert w["sectors"] == sectors and s["sectors"] == sectors
assert w["centre"][0] < s["centre"][0] and _close(w["centre"][1], s["centre"][1]), "side by side, on one baseline"
assert _close(w["wedge_radii"]["W"], w["radius"]) and _close(w["wedge_radii"]["N"], w["radius"] * 0.04 / 0.3)
assert _close(s["wedge_radii"]["SW"], s["radius"] * 0.25 / 0.3), "both roses share one ring scale"
assert w["prevailing"] == "W" and s["prevailing"] == "SW"
svg = two["svg"]
assert svg.count('fill-opacity="1.00"') == 2, "one solid wedge per rose"
assert svg.count(f'fill-opacity="{report_chart.ROSE_WEDGE_OPACITY:.2f}"') == 14
assert svg.count(">wind from</text>") == 2
assert ">Winter (Dec–Feb)</text>" in svg and ">Summer (Jun–Aug)</text>" in svg
assert svg.count('font-family="IBM Plex Mono"') >= 2 and ">6.1 mph</text>" in svg and ">3.6 mph</text>" in svg
# The cardinal letters sit outside the rings at their compass angle: N above the centre, E to its right.
letters = re.findall(r'<text x="([\d.]+)" y="([\d.]+)" font-family="Source Serif 4" font-size="7.00" fill="[^"]+" text-anchor="middle">([NESW])</text>', svg)
assert [l[2] for l in letters] == ["N", "E", "S", "W", "N", "E", "S", "W"], letters
cx, cy = w["centre"]
by_letter = {l[2]: (float(l[0]), float(l[1])) for l in letters[:4]}
assert by_letter["N"][1] < cy - w["radius"] and _close(by_letter["N"][0], cx, 0.01)
assert by_letter["E"][0] > cx + w["radius"] and by_letter["W"][0] < cx - w["radius"]
assert by_letter["S"][1] > cy + w["radius"]
# The wedge for a sector is drawn clockwise from north: N's wedge straddles the top; E's sits to the right.
wedges = re.findall(r'<path d="M([\d.]+) ([\d.]+) L([\d.]+) ([\d.]+) A', svg)
first_wedge_start = (float(wedges[0][2]), float(wedges[0][3]))
assert first_wedge_start[0] < cx and first_wedge_start[1] < cy, "N's wedge begins 22.5 degrees west of north"
try:
    render_wind_roses([dict(seasons[0], sectors=sectors[:4])], TOKENS)
except ValueError:
    pass
else:
    raise AssertionError("four sectors must be refused")
# The solid wedge is the longest by construction; a caller's "prevailing" that disagrees is refused.
try:
    render_wind_roses([dict(seasons[0], prevailing="SW")], TOKENS)
except ValueError as exc:
    assert "modal" in str(exc) and "'W'" in str(exc)
else:
    raise AssertionError("SW is not the longest winter wedge; naming it prevailing must be refused")
unnamed = render_wind_roses([dict(seasons[0], prevailing=None)], TOKENS)
assert unnamed["roses"][0]["prevailing"] == "W" and unnamed["svg"].count('fill-opacity="1.00"') == 1
print(f"   ring scale {two['ring_max']}; W solid in winter, SW in summer; letters at their angles")

# ======================================================================
# 5. The legend
# ======================================================================
print("5. the water balance legend is legend_entries()' shape")
legend = rendered["legend"]
assert [e["id"] for e in legend] == ["precipitation", "evaporation", "surplus", "deficit"]
assert [e["parts"] for e in legend] == [["precipitation"], ["potential evaporation"], ["surplus"], ["deficit"]]
assert TOKENS["water"] in legend[0]["swatch"] and TOKENS["ochre"] in legend[1]["swatch"]
assert 'class="report-map__swatch"' in legend[2]["swatch"] and f'fill="{TOKENS["water"]}"' in legend[2]["swatch"]
assert roses.get("legend") is None, "the roses carry their own labels; no legend strip"
print("   four entries, swatches in water and ochre")

# ======================================================================
# 6. The valley profile
# ======================================================================
print("6. the valley profile: whole-number exaggeration, the keypoint and its grades, boundary ticks, feet")
PROFILE = {
    "distance": [0.0, 200.0, 400.0, 600.0, 800.0, 1000.0],
    "elevation": [1240.0, 1220.0, 1195.0, 1180.0, 1172.0, 1165.0],
    "crossings": [300.0, 700.0],
    "keypoint": {"distance": 400.0, "elevation": 1195.0, "grade_above_pct": 8.88, "grade_below_pct": 5.87, "label": "1,195 ft"},
}
profile = render_valley_profile(PROFILE, TOKENS)
x0, y0, x1, y1 = profile["plot"]
assert profile["frame"][0] == 489.6 and x1 - x0 > 400
assert isinstance(profile["exaggeration"], int) and profile["exaggeration"] >= 1
assert abs(profile["y_per_ft"] / profile["x_per_ft"] - profile["exaggeration"]) < 1e-9
assert profile["exaggeration"] == profile_exaggeration(x1 - x0, y1 - y0, 1000.0, profile["y_range"][1] - profile["y_range"][0])
# The relief at that exaggeration fits the plot; one step more would not.
relief_pt = (profile["y_range"][1] - profile["y_range"][0]) * profile["y_per_ft"]
assert relief_pt <= y1 - y0 + 1e-9 and (profile["exaggeration"] == 10 or relief_pt * (profile["exaggeration"] + 1) / profile["exaggeration"] > y1 - y0)
assert profile["y_range"] == (1160.0, 1240.0) and profile["y_ticks"][0] == 1160.0 and profile["y_ticks"][-1] == 1240.0
assert profile["x_ticks"] == [0.0, 200.0, 400.0, 600.0, 800.0, 1000.0]
kx, ky = profile["keypoint_xy"]
assert abs(kx - (x0 + 400.0 * profile["x_per_ft"])) < 1e-9 and abs(ky - (y1 - (1195.0 - 1160.0) * profile["y_per_ft"])) < 1e-9
assert [round(b - x0, 6) for b in profile["boundary_ticks_x"]] == [round(300.0 * profile["x_per_ft"], 6), round(700.0 * profile["x_per_ft"], 6)]
svg = profile["svg"]
assert "1,195 ft" in svg and "8.9% above" in svg and "5.9% below" in svg and "parcel boundary" in svg
assert svg.count("<circle") == 1 and f'fill="{TOKENS["ink"]}"' in svg.split("<circle", 1)[1].split("/>", 1)[0]
assert f'stroke="{TOKENS["water"]}"' in svg, "the valley floor is in the water token"
assert "1,240 ft" in svg and "1,000 ft" in svg, "the unit rides beside the top and the last tick"
assert set(re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{6})"', svg)) <= set(TOKENS.values())
doc = minidom.parseString(svg)
for text in doc.documentElement.getElementsByTagName("text"):
    assert text.getAttribute("font-family") in ("IBM Plex Mono", "Source Serif 4")
    if text.firstChild.data == "parcel boundary":
        assert text.getAttribute("font-family") == "Source Serif 4", "a word is prose"
    elif text.firstChild.data[0].isdigit():
        assert text.getAttribute("font-family") == "IBM Plex Mono", text.firstChild.data
legend = ["".join(p if isinstance(p, str) else p["value"] for p in e["parts"]) for e in profile["legend"]]
assert legend == ["valley stem", "keypoint"] and "<circle" in profile["legend"][1]["swatch"]
# No keypoint: the line alone, no dot, no grades, one legend entry.
bare = render_valley_profile(dict(PROFILE, keypoint=None, crossings=[]), TOKENS)
assert bare["keypoint_xy"] is None and "<circle" not in bare["svg"] and "above" not in bare["svg"]
assert bare["boundary_ticks_x"] == [] and "parcel boundary" not in bare["svg"] and len(bare["legend"]) == 1
# A steep short profile caps at the maximum; a flat one at 1; bad input refused.
assert profile_exaggeration(400.0, 120.0, 4000.0, 10.0) == 10 and profile_exaggeration(400.0, 120.0, 100.0, 200.0) == 1
for bad in (dict(PROFILE, distance=[0.0]), dict(PROFILE, elevation=PROFILE["elevation"][:-1])):
    try:
        render_valley_profile(bad, TOKENS)
    except ValueError:
        pass
    else:
        raise AssertionError(bad)
print(f"   exaggeration {profile['exaggeration']}x; keypoint at {kx:.1f}, {ky:.1f}; ticks at {[round(b, 1) for b in profile['boundary_ticks_x']]}")

print("\ntest_report_chart.py: all sections passed")
print(offline_harness.summary())
