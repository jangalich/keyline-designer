"""
test_landform_derivations.py

THE LANDFORM SECTION'S DERIVATIONS ON REAL TERRAIN -- branch 8, phase 1
of site-data-report-proposal.md's build sequence. Runs offline on the
captured 3DEP grid (terrain_reference_fixture.py) through an offline
session, so every product below is read the way the report job reads it.

  1. THE FIXTURE PINS THE DERIVATION: valleys and keypoints recomputed
     from the stored DEM match the stored copy cell for cell -- 4
     valleys; 3 keypoints, 2 on the parcel and 1 just outside it, 19.6 m
     from the boundary.
  2. THE CALL COUNT: building the section from the session calls
     delineate_valleys() and detect_keypoints() ZERO times and the flow
     pass (fill_and_resolve, compute_flow_direction,
     compute_flow_accumulation) ONCE each.
  3. Ridges are the divides between the valleys' drainage areas: every
     cell labelled, the areas tile the window, each divide lies on the
     boundary of both its areas (within the rounding), the off-window
     drainage takes part, and three divides cross the parcel.
  4. Keylines: one per keypoint, none without; each lies on the contour
     at its keypoint's elevation (checked against the DEM independently),
     passes through the keypoint, and ends at a ridge on both sides --
     or at the DEM window's edge, said so -- and is clipped to the
     parcel for the page. No keypoint, no keyline.
  5. The profile: the primary valley's stem, upstream first, raw
     elevations, the keypoint where the detector put it with the
     detector's own grades, boundary crossings where the stem crosses
     the drawn boundary; the chart states a whole-number exaggeration.
  6. The valley rows: one per valley whose stem crosses the parcel (three
     of four), lengths and falls measured against shapely independently,
     the profile's valley first, dashes for a valley without a keypoint.
  7. The section's words: the keypoint count as a rule (n keypoints, m on
     the property, k just outside within so many feet), the valley table
     and its caption, the nine key figures, the structure map's hierarchy
     and labels, the sources and methods; and on the synthetic parcel
     with no keypoint at all, the plain statement that there is no
     keyline. No water or storage language anywhere in the section.
  8. The report on the real terrain through WeasyPrint: six pages, the
     three Landform pages in order, no box past the measure, the captions
     and the continuation eyebrows on the page.
"""

import math
import re
from unittest.mock import patch

import numpy as np
from shapely.geometry import LineString, MultiLineString, Point

import offline_harness

offline_harness.install()

import keypoint_detection  # noqa: E402
import landform_derivations as ld  # noqa: E402
import landform_section as ls  # noqa: E402
import report_map  # noqa: E402
import roads_step_fixture as synthetic  # noqa: E402
import terrain_reference_fixture as real  # noqa: E402
import valley_delineation  # noqa: E402
from landform_section import ZERO_DASH  # noqa: E402
from raster_grid import pixel_center_xy  # noqa: E402
from reference_fixture import REAL_BOUNDARY  # noqa: E402
from site_report import TOKENS  # noqa: E402

METERS_PER_FOOT = ls.METERS_PER_FOOT


def _text(parts) -> str:
    return "".join(p if isinstance(p, str) else p["value"] for p in parts)


def _parts(geometry) -> list:
    return [geometry] if isinstance(geometry, LineString) else list(geometry.geoms)


# ======================================================================
# 1. The fixture
# ======================================================================
print("1. the stored derivations are what the current code produces on the stored DEM")
DEM = real.load_dem()
STORED = real.load_derivations()
assert DEM["array"].shape == (108, 96) and DEM["array"].dtype == np.float32 and not np.isnan(DEM["array"]).any()
assert DEM["crs"] == "EPSG:32617" and abs(DEM["resolution_meters"][0] - 5.0) < 0.01
valleys = valley_delineation.delineate_valleys(DEM)
assert [v["id"] for v in valleys] == [v["id"] for v in STORED["valleys"]] == [2, 4, 8, 11]
assert [v["branches_rowcol"] for v in valleys] == [v["branches_rowcol"] for v in STORED["valleys"]]
assert [v["max_contributing_area_acres"] for v in valleys] == [34.54, 6.48, 8.17, 3.13]
keypoints = keypoint_detection.detect_keypoints(DEM, real.BOUNDARY_POLYGON_UTM, valleys=valleys)
assert len(keypoints) == len(STORED["keypoints"]) == 3
for got, stored in zip(keypoints, STORED["keypoints"]):
    for field in stored:
        assert got[field] == stored[field], (field, got[field], stored[field])
assert [kp["on_parcel"] for kp in keypoints] == [True, True, False]
assert keypoints[2]["valley_id"] == 2 and keypoints[2]["distance_outside_boundary_m"] == 19.55
assert keypoints[1]["rowcol"] == (66, 25) and keypoints[1]["elevation_m"] == 333.5, "the keypoint the margin fix recovered"
print(f"   4 valleys {[v['id'] for v in valleys]}; 3 keypoints, 2 on the parcel, 1 at "
      f"{keypoints[2]['distance_outside_boundary_m']} m outside; retrieved {STORED['retrieved_on']}")

# ======================================================================
# 2. The call count
# ======================================================================
print("2. the section reads valleys and keypoints and runs the flow pass once")
with real.Harness():
    SESSION = real.Session()
    CONTEXT = SESSION.context()
    DOCUMENT = SESSION.stored()
    assert [kp["rowcol"] for kp in CONTEXT.keypoints] == [kp["rowcol"] for kp in keypoints]
    with patch.object(valley_delineation, "delineate_valleys", wraps=valley_delineation.delineate_valleys) as valley_calls, \
         patch.object(keypoint_detection, "detect_keypoints", wraps=keypoint_detection.detect_keypoints) as keypoint_calls, \
         patch.object(valley_delineation, "fill_and_resolve", wraps=valley_delineation.fill_and_resolve) as fill_calls, \
         patch.object(valley_delineation, "compute_flow_direction", wraps=valley_delineation.compute_flow_direction) as direction_calls, \
         patch.object(valley_delineation, "compute_flow_accumulation", wraps=valley_delineation.compute_flow_accumulation) as accumulation_calls:
        TERRAIN = ls.terrain_inputs_from_context(CONTEXT, DOCUMENT)
        SECTION = ls.build_landform_section(TERRAIN, TOKENS)
    assert valley_calls.call_count == 0 and keypoint_calls.call_count == 0
    assert fill_calls.call_count == 1 and direction_calls.call_count == 1 and accumulation_calls.call_count == 1
DERIVED = SECTION["derived"]
assert isinstance(DERIVED, ld.TerrainDerived)
print("   delineate_valleys 0, detect_keypoints 0; fill 1, flow direction 1, flow accumulation 1")

# ======================================================================
# 3. Ridges
# ======================================================================
print("3. ridges are the divides between drainage areas, the off-window drainage included")
labels = DERIVED.labels
assert labels.shape == DEM["array"].shape and (labels >= -1).all(), "every cell labelled"
assert set(np.unique(labels).tolist()) == {ld.OFF_WINDOW, 2, 4, 8, 11}
# A valley's own cells carry its label; a cell's label is its downstream valley's.
for valley in valleys:
    for branch in valley["branches_rowcol"]:
        assert all(labels[r, c] == valley["id"] for r, c in branch)
r, c = 0, 0
while DERIVED.flow_to_row[r, c] >= 0 and labels[r, c] == labels[int(DERIVED.flow_to_row[r, c]), int(DERIVED.flow_to_col[r, c])]:
    r, c = int(DERIVED.flow_to_row[r, c]), int(DERIVED.flow_to_col[r, c])
assert DERIVED.flow_to_row[r, c] < 0 or labels[r, c] in (2, 4, 8, 11), "a walk down the flow field stays in one label"
# The areas tile the window.
window_area = DEM["array"].size * DEM["resolution_meters"][0] * DEM["resolution_meters"][1]
assert abs(sum(p.area for p in DERIVED.catchments.values()) - window_area) < 1e-3
assert abs(DERIVED.catchments[2].area / 4046.8564224 - 34.54) < 0.01, "valley 2's area is its contributing area"
# Each divide lies on the boundary of both areas it separates, within the rounding.
for ridge in DERIVED.ridges:
    a, b = ridge["between"]
    for part in _parts(ridge["geometry"]):
        for x, y in part.coords[::5]:
            assert DERIVED.catchments[a].boundary.distance(Point(x, y)) < 3.0
            assert DERIVED.catchments[b].boundary.distance(Point(x, y)) < 3.0
    raw = DERIVED.catchments[a].boundary.intersection(DERIVED.catchments[b].boundary)
    assert ridge["geometry"].hausdorff_distance(raw) < max(DEM["resolution_meters"]), "the rounded divide stays within a cell"
on_parcel = [r for r in DERIVED.ridges if r["on_parcel"] is not None]
assert [r["between"] for r in on_parcel] == [(ld.OFF_WINDOW, 4), (2, 8), (4, 8)], [r["between"] for r in on_parcel]
assert all(r["on_parcel"].within(real.BOUNDARY_POLYGON_UTM.buffer(0.01)) for r in on_parcel)
assert DERIVED.keypoint_counts["ridges_on_parcel"] == 3
lengths = {r["between"]: round(r["length_on_parcel_m"]) for r in on_parcel}
assert 300 < lengths[(2, 8)] < 380 and 200 < lengths[(4, 8)] < 260 and 40 < lengths[(ld.OFF_WINDOW, 4)] < 70, lengths
print(f"   {len(DERIVED.ridges)} divides in the window, 3 on the parcel: {lengths} m")

# ======================================================================
# 4. Keylines
# ======================================================================
print("4. one keyline per keypoint, on the contour, through the keypoint, ending at the ridges")
assert len(DERIVED.keylines) == 3 == DERIVED.keypoint_counts["detected"]
arr = DEM["array"]
rows, cols = arr.shape
resx, resy = DEM["resolution_meters"]


def _bilinear(x, y):
    cc = (x - DEM["origin_x"]) / resx - 0.5
    rr = (DEM["origin_y"] - y) / resy - 0.5
    c0, r0 = min(max(int(math.floor(cc)), 0), cols - 2), min(max(int(math.floor(rr)), 0), rows - 2)
    fc, fr = cc - c0, rr - r0
    return (arr[r0, c0] * (1 - fr) * (1 - fc) + arr[r0, c0 + 1] * (1 - fr) * fc
            + arr[r0 + 1, c0] * fr * (1 - fc) + arr[r0 + 1, c0 + 1] * fr * fc)


by_keypoint = {kp["id"]: kp for kp in keypoints}
for keyline in DERIVED.keylines:
    kp = by_keypoint[keyline["keypoint_id"]]
    assert keyline["reason"] is None and keyline["geometry"] is not None
    assert keyline["elevation_m"] == kp["elevation_m"] and keyline["valley_id"] == kp["valley_id"]
    line = keyline["geometry"]
    samples = [line.interpolate(i / 200.0, normalized=True) for i in range(201)]
    misses = [abs(_bilinear(p.x, p.y) - keyline["elevation_m"]) for p in samples]
    assert max(misses) < 0.15, (keyline["keypoint_id"], max(misses))
    assert line.distance(kp["point_utm"]) < 0.15, "the keyline passes through the keypoint"
    stem = ld.stem_line(DERIVED.stems[kp["valley_id"]], DEM)
    assert line.distance(stem) < 0.01, "the keypoint is where the keyline crosses the valley line"
    catchment = DERIVED.catchments[kp["valley_id"]]
    assert line.within(catchment.buffer(ld.KEYLINE_CLIP_PAD_M + 0.01)), "the keyline stays inside its valley's drainage area"
    assert len(keyline["ends"]) == 2
    for end in keyline["ends"]:
        assert end["at_window_edge"] or end["to_divide_m"] <= ld.KEYLINE_CLIP_PAD_M + 0.01, end
    assert keyline["on_parcel"] is not None and keyline["on_parcel"].within(real.BOUNDARY_POLYGON_UTM.buffer(0.01))
    assert abs(keyline["length_on_parcel_m"] - keyline["on_parcel"].length) < 1e-9
ends = {k["keypoint_id"]: [("edge" if e["at_window_edge"] else "divide") for e in k["ends"]] for k in DERIVED.keylines}
assert ends[0] == ["divide", "divide"] and ends[1] == ["divide", "divide"] and sorted(ends[2]) == ["divide", "edge"], ends
outside = [k for k in DERIVED.keylines if not k["keypoint_on_parcel"]]
assert len(outside) == 1 and outside[0]["valley_id"] == 2 and 140 < outside[0]["length_on_parcel_m"] < 165
total_ft = sum(k["length_on_parcel_m"] for k in DERIVED.keylines) / METERS_PER_FOOT
assert 1000 < total_ft < 1150, total_ft
# No keypoint, no keyline: a terrain with no keypoints derives none, and says so.
bare = ls.TerrainInputs(**{**TERRAIN.__dict__, "keypoints": []})
none = ld.derive(bare)
assert none.keylines == [] and none.keypoint_counts == {"detected": 0, "on_parcel": 0, "outside": 0,
                                                        "keylines_on_parcel": 0, "valleys_on_parcel": 3, "ridges_on_parcel": 3}
assert ls.build_keypoint_statement(none, []) == ["No keypoint was found on this property, so there is no keyline to draw."]
assert none.profile["keypoint"] is None
print(f"   keylines on the parcel {[round(k['length_on_parcel_m']) for k in DERIVED.keylines]} m, "
      f"{total_ft:.0f} ft in all; ends {ends}; valley 2's keypoint outside, its keyline drawn")

# ======================================================================
# 5. The profile
# ======================================================================
print("5. the primary valley's stem, the keypoint where the detector put it, ticks at the boundary")
profile = DERIVED.profile
assert DERIVED.primary_valley_id == 2 == profile["valley_id"], "largest contributing area among the valleys on the parcel"
stem = DERIVED.stems[2]
assert stem == keypoint_detection.trace_stem_from_outlet(
    valleys[0], keypoint_detection.build_upstream_map(DERIVED.flow_to_row, DERIVED.flow_to_col), DERIVED.accumulation)
assert len(profile["distance_m"]) == len(profile["elevation_m"]) == len(stem) == keypoints[2]["stem_length_cells"]
assert profile["elevation_m"] == [float(arr[r, c]) for r, c in stem], "raw elevations, never the filled surface"
steps = [math.dist(pixel_center_xy(DEM, *a), pixel_center_xy(DEM, *b)) for a, b in zip(stem, stem[1:])]
assert all(abs(profile["distance_m"][i + 1] - profile["distance_m"][i] - steps[i]) < 1e-9 for i in range(len(steps)))
assert profile["distance_m"][0] == 0.0 and profile["elevation_m"][0] > profile["elevation_m"][-1], "upstream first"
kp = profile["keypoint"]
assert kp["index"] == keypoints[2]["position_along_stem"] and stem[kp["index"]] == keypoints[2]["rowcol"]
assert kp["grade_above_pct"] == keypoints[2]["slope_above_pct"] == 8.88 and kp["grade_below_pct"] == 5.87
assert kp["on_parcel"] is False and kp["distance_outside_boundary_m"] == 19.55
# The detector's grades are the means of its smoothed slope over min_run cells either side of the split.
distance, elevation, slope = keypoint_detection._profile_along_stem(stem, arr, DEM, keypoint_detection.KEYPOINT_PROFILE_SMOOTH_CELLS)
run = keypoint_detection.KEYPOINT_MIN_RUN_CELLS
assert abs(np.mean(slope[kp["index"] - run:kp["index"]]) - kp["grade_above_pct"]) < 0.005
assert abs(np.mean(slope[kp["index"]:kp["index"] + run]) - kp["grade_below_pct"]) < 0.005
# The stem crosses the boundary where shapely says it does: twice, at the parcel's east corner.
crossings = profile["crossings"]
assert [c["entering"] for c in crossings] == [True, False] and 0 < crossings[1]["distance_m"] - crossings[0]["distance_m"] < 20
stem_geometry = ld.stem_line(stem, DEM)
assert abs(stem_geometry.intersection(real.BOUNDARY_POLYGON_UTM).length - (crossings[1]["distance_m"] - crossings[0]["distance_m"])) < 1e-6
assert sum(profile["on_parcel"]) == 3
chart = SECTION["profile"]["chart"]
assert chart["exaggeration"] == 3 and isinstance(chart["exaggeration"], int)
assert len(chart["boundary_ticks_x"]) == 2 and chart["keypoint_xy"] is not None
assert abs(chart["y_per_ft"] / chart["x_per_ft"] - 3.0) < 1e-9, "the exaggeration is the ratio of the scales"
assert "parcel boundary" in chart["svg"] and "8.9% above" in chart["svg"] and "5.9% below" in chart["svg"]
assert set(re.findall(r'(?:fill|stroke)="(#[0-9a-fA-F]{6})"', chart["svg"])) <= set(TOKENS.values())
print(f"   stem {len(stem)} cells, {profile['distance_m'][-1]:.0f} m; keypoint at {kp['distance_m']:.0f} m, "
      f"{kp['grade_above_pct']}% -> {kp['grade_below_pct']}%; crossings at "
      f"{[round(c['distance_m']) for c in crossings]} m; exaggeration {chart['exaggeration']}x")

# ======================================================================
# 6. The valley rows
# ======================================================================
print("6. one row per valley whose stem crosses the parcel, measured independently")
rows_ = DERIVED.valley_rows
assert [r["valley_id"] for r in rows_] == [2, 8, 4] and [r["number"] for r in rows_] == [1, 2, 3]
assert 11 not in [r["valley_id"] for r in rows_], "valley 11 never touches the parcel"
for row in rows_:
    line = ld.stem_line(DERIVED.stems[row["valley_id"]], DEM)
    clipped = line.intersection(real.BOUNDARY_POLYGON_UTM)
    assert abs(row["length_m"] - clipped.length) < 1e-9
    assert row["fall_m"] > 0 and abs(row["grade_pct"] - row["fall_m"] / row["length_m"] * 100) < 1e-9
    # The fall is bounded by the raw relief along the clipped stem.
    zs = [float(arr[r, c]) for r, c in DERIVED.stems[row["valley_id"]] if real.BOUNDARY_POLYGON_UTM.contains(Point(pixel_center_xy(DEM, r, c)))]
    assert row["fall_m"] <= (max(zs) - min(zs)) + 2 * max(DEM["resolution_meters"]) * row["grade_pct"] / 100 + 1e-6
assert 10 < rows_[0]["length_m"] < 20, "valley 2's main stem clips the parcel's east corner only"
assert 190 < rows_[1]["length_m"] < 210 and 145 < rows_[2]["length_m"] < 160
assert all(r["keypoint"] is not None for r in rows_)
assert rows_[0]["keypoint"]["on_parcel"] is False and rows_[1]["keypoint"]["on_parcel"] and rows_[2]["keypoint"]["on_parcel"]
without = ld.valley_rows(valleys, DERIVED.stems, [], DEM, real.BOUNDARY_POLYGON_UTM)
assert [r["keypoint"] for r in without] == [None, None, None]
print("   rows " + "; ".join(f"valley {r['valley_id']} -> {r['number']}: {r['length_m']:.0f} m, {r['fall_m']:.1f} m, {r['grade_pct']:.1f}%" for r in rows_))

# ======================================================================
# 7. The section's words
# ======================================================================
print("7. the counting rule, the table, the figures; the no-keypoint statement on the synthetic parcel")
statement = _text(SECTION["keypoint_statement"])
assert statement == ("3 keypoints: 2 on the property and 1 just outside the boundary, within 64 ft of it; "
                     "its keyline still crosses the parcel and is drawn."), statement
assert {"value": "3"} in SECTION["keypoint_statement"] and {"value": "64 ft"} in SECTION["keypoint_statement"]
figures = {f["label"]: f["value"] for f in SECTION["key_figures"]}
assert list(figures) == ["lowest elevation", "highest elevation", "relief", "mean slope", "dominant aspect",
                         "keypoints detected", "valleys crossing the parcel", "ridges crossing the parcel",
                         "keyline length on the parcel"]
assert figures["keypoints detected"] == "3" and figures["valleys crossing the parcel"] == "3" and figures["ridges crossing the parcel"] == "3"
assert figures["keyline length on the parcel"] == f"{round(total_ft):,} ft"
table = SECTION["valley_table"]
assert table["columns"] == ["Stem, ft", "Fall, ft", "Grade, %", "Keypoint, ft", "Above, %", "Below, %"] and table["corner"] == "Valley"
assert [_text(r["label"]) for r in table["rows"]] == ["Valley 1", "Valley 2", "Valley 3"]
for row, derived_row in zip(table["rows"], rows_):
    assert row["cells"][0] == f"{round(derived_row['length_m'] / METERS_PER_FOOT):,}"
    assert row["cells"][3] == f"{round(derived_row['keypoint']['elevation_m'] / METERS_PER_FOOT):,}"
    assert re.match(r"^\d+\.\d$", row["cells"][2]) and re.match(r"^\d+\.\d$", row["cells"][4])
assert table["rows"][0]["cells"][3] == "1,109" and table["rows"][0]["cells"][4:] == ["8.9", "5.9"]
caption = _text(SECTION["valley_table_caption"])
assert "Valley 1's keypoint lies 64 ft outside the boundary." in caption
profile_caption = _text(SECTION["profile"]["caption"])
assert profile_caption.startswith("Valley 1, the full stem from its head") and "vertical exaggeration 3×" in profile_caption
assert "64 ft outside the boundary" in profile_caption
# The table with a valley lacking a keypoint reads dashes.
dashed = ls.build_valley_table(none)
assert all(r["cells"][3:] == [ZERO_DASH] * 3 for r in dashed["rows"])
assert "has no keypoint, so it has no keyline" in _text(ls.build_valley_table_caption(none))
# No water language anywhere in the section's words.
words = " ".join([
    statement, caption, profile_caption, _text(SECTION["summary"]),
    " ".join(f["label"] for f in SECTION["key_figures"]), " ".join(table["columns"]),
    " ".join(_text(e["parts"]) for e in SECTION["structure_map"]["legend"]),
    " ".join(SECTION["footer"]["caveat"] + SECTION["footer"]["citation"]),
]).lower()
for banned in ("water", "dam", "storage", "catchment", "irrigat", "harvest"):
    assert banned not in words, banned
# The structure map, lightest context first: contours, ridges (terrain, long dash, lightest), valley STEMS
# (water, dashed, no tributaries), keylines (ink, heaviest) with their elevations, keypoints as haloed dots --
# the outside one in the muted ink, its keyline carried out to it. The hierarchy is explicit in the weights.
structure = SECTION["structure_map"]["svg"]
structure_layers = ls.build_structure_layers(TERRAIN, report_map.parcel_contours(DEM, real.BOUNDARY_POLYGON_UTM), DERIVED)
order = [structure.index(f'id="layer-{name}"') for name in ("contours", "ridges", "valley-stems", "keylines", "keylines-outside", "keypoints", "keypoints-outside")]
assert order == sorted(order)
assert "layer-valleys" not in structure, "tributaries are not drawn on the keyline map"
weights = {l["id"]: l["stroke_width"] for l in structure_layers}
assert weights["keylines"] > weights["index-contours"] > weights["valley-stems"] > weights["contours"] > weights["ridges"], weights
stem_layer = [l for l in structure_layers if l["id"] == "valley-stems"][0]
assert stem_layer["stroke"] == "water" and stem_layer["dash"] and stem_layer["labels"] == ["1", "2", "3"]
assert all(g.within(real.BOUNDARY_POLYGON_UTM.buffer(0.01)) for g in stem_layer["geometries"])
assert abs(stem_layer["geometries"][1].length - rows_[1]["length_m"]) < 1e-6, "the drawn stem is the measured stem"
ridge_layer = [l for l in structure_layers if l["id"] == "ridges"][0]
assert ridge_layer["stroke"] == "terrain" and ridge_layer["dash"] == ls.RIDGE_DASH and ls.RIDGE_STROKE_PT < report_map.CONTOUR_STROKE_PT
index_layer = [l for l in structure_layers if l["id"] == "index-contours"][0]
assert index_layer["labels"] is None, "contour elevations are on the terrain map at the same extent"
assert SECTION["structure_map"]["labels_placed"]["valley-stems"] == [False, True, True], "valley 1's 14 m stem cannot carry its number and drops it"
assert SECTION["structure_map"]["meters_per_unit"] == SECTION["map"]["meters_per_unit"]
assert SECTION["structure_map"]["drawn_bbox"] == SECTION["map"]["drawn_bbox"], "the two maps share extent and scale"
keyline_group = structure.split('<g id="layer-keylines">', 1)[1].split("</g>", 1)[0]
assert f'stroke="{TOKENS["ink"]}"' in keyline_group and f'stroke-width="{ls.KEYLINE_STROKE_PT:.2f}"' in keyline_group
# Every keyline carries its elevation as a label; the renderer sets a label in the line only where the line has
# room for it clear of the keypoint's dot (the keyline is split at its keypoint so a label never lands on the
# dot). On this parcel the two short keylines (89 and 84 m) cannot carry theirs, so their elevations are set
# beside the dots.
keyline_layer = [l for l in structure_layers if l["id"] == "keylines"][0]
assert keyline_layer["labels"] == ["1,173", "1,094", "1,109"]
for g, k in zip(keyline_layer["geometries"], DERIVED.keylines):
    if k["keypoint_on_parcel"]:
        endpoints = [Point(c) for part in _parts(g) for c in (part.coords[0], part.coords[-1])]
        assert len(_parts(g)) >= 2 and min(e.distance(by_keypoint[k["keypoint_id"]]["point_utm"]) for e in endpoints) < 0.2, "split at the keypoint"
placed = set(re.findall(r">([\d,]+)</text>", keyline_group))
assert placed == {"1,109"}, placed
assert SECTION["structure_map"]["labels_placed"]["keylines"] == [False, False, True]
# ...so those two elevations are set beside their keypoints' dots instead, and 1,109's is not.
keypoint_layer = [l for l in structure_layers if l["id"] == "keypoints"][0]
assert keypoint_layer["labels"] == ["1,173", "1,094"] and [l for l in structure_layers if l["id"] == "keypoints-outside"][0]["labels"] == [None]
reach_group = structure.split('<g id="layer-keylines-outside">', 1)[1].split("</g>", 1)[0]
assert f'stroke="{TOKENS["ink-muted"]}"' in reach_group, "the outside keypoint's keyline reaches it in the muted ink"
outside_group = structure.split('<g id="layer-keypoints-outside">', 1)[1].split("</g>", 1)[0]
assert outside_group.count("<circle") == 2 and f'fill="{TOKENS["ink-muted"]}"' in outside_group, "one dot: a halo and a fill"
on_group = structure.split('<g id="layer-keypoints">', 1)[1].split("</g>", 1)[0]
assert on_group.count("<circle") == 4 and f'fill="{TOKENS["ink"]}"' in on_group and on_group.count(f'fill="{TOKENS["page"]}"') == 2
assert {"1,173", "1,094"} == set(re.findall(r">([\d,]+)</text>", on_group)) and "<text" not in outside_group
caption_text = _text(SECTION["structure_caption"])
assert caption_text.startswith("A keyline is the contour through its keypoint") and caption_text.endswith(statement)
# The sources line and the methods entry the Site overview will render.
assert SECTION["sources"] == [["USGS 3DEP elevation, 1/3 arc-second, resampled to 5 m, retrieved " + ls.format_retrieved_on(TERRAIN.retrieved_on) + "."]]
methods = SECTION["methods"]
assert len(methods) == 1 and methods[0]["source"] == "USGS 3DEP" and methods[0]["citation"] == ls.CITATION_3DEP
assert len(methods[0]["notes"]) == 6 and any("No keypoint, no keyline" in n for n in methods[0]["notes"])

# The synthetic parcel: valleys but no keypoint anywhere -> no keyline, said plainly.
with synthetic.Harness():
    session = synthetic.Session()
    synthetic_terrain = ls.terrain_inputs_from_context(session.context(), session.stored())
    synthetic_section = ls.build_landform_section(synthetic_terrain, TOKENS)
assert synthetic_terrain.keypoints == []
assert _text(synthetic_section["keypoint_statement"]) == "No keypoint was found on this property, so there is no keyline to draw."
assert synthetic_section["derived"].keylines == [] and "layer-keylines" not in synthetic_section["structure_map"]["svg"]
assert "layer-keypoints" not in synthetic_section["structure_map"]["svg"] and "layer-valley-stems" in synthetic_section["structure_map"]["svg"]
assert {f["label"]: f["value"] for f in synthetic_section["key_figures"]}["keypoints detected"] == "0"
assert {f["label"]: f["value"] for f in synthetic_section["key_figures"]}["keyline length on the parcel"] == "0 ft"
synthetic_profile = synthetic_section["profile"]
assert synthetic_profile["chart"] is not None and synthetic_profile["chart"]["keypoint_xy"] is None
assert "No keypoint was found on this valley, so no grades are marked and it has no keyline." in _text(synthetic_profile["caption"])
assert "The parcel is the short reach between the ticks, 45 ft of it." in profile_caption, profile_caption
assert all(r["cells"][3:] == [ZERO_DASH] * 3 for r in synthetic_section["valley_table"]["rows"])
print(f"   \"{statement}\"; synthetic parcel: \"{_text(synthetic_section['keypoint_statement'])}\"")

# ======================================================================
# 8. The pages, on the real terrain
# ======================================================================
print("8. the report on the real terrain: six pages, the three Landform pages in order, nothing past the measure")
import os  # noqa: E402
from datetime import date  # noqa: E402

import report_layout  # noqa: E402
import site_report  # noqa: E402
from daymet_data import parse_daymet_csv  # noqa: E402
from report_data import report_data_from_daily  # noqa: E402
from weasyprint import HTML  # noqa: E402

with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "daymet_reference_fixture.csv"), encoding="utf-8") as handle:
    DATA = report_data_from_daily(REAL_BOUNDARY, parse_daymet_csv(handle.read()))
html = site_report.render_site_report_html(DATA, generated_on=date(2026, 9, 22), terrain=TERRAIN)
document = HTML(string=html, base_url=site_report.TEMPLATES_DIRECTORY).render()
assert len(document.pages) == 6, len(document.pages)
assert report_layout.overflowing_boxes(document) == []


def _walk(box):
    yield box
    for child in getattr(box, "children", []) or []:
        yield from _walk(child)


def _classes(page):
    return {(b.element.get("class") or "").split()[0] for b in _walk(page._page_box)
            if getattr(b, "element", None) is not None and b.element.get("class")}


pages = document.pages
assert {"heading", "summary", "report-map"} <= _classes(pages[3]) and "key-figures" not in _classes(pages[3])
assert {"report-map", "report-chart", "caption"} <= _classes(pages[4]) and "data-table" not in _classes(pages[4])
assert {"key-figures", "data-table", "source-footer"} <= _classes(pages[5]) and "report-map" not in _classes(pages[5])
flat = "".join("".join(b.text for b in _walk(p._page_box) if type(b).__name__ == "TextBox") for p in pages[3:])
squash = "".join(flat.split())
for expected in ("3keypoints:2onthepropertyand1justoutsidetheboundary", "verticalexaggeration3×", "Valley1'skeypointlies64ftoutside",
                 "III·LANDFORM,CONTINUED", "Keylines,elevationinft"):
    assert expected in squash, expected
assert squash.count("III·LANDFORM,CONTINUED") == 2
print("   6 pages; pages 4-6 are the terrain, the structure and the numbers; the captions read on the page")

print("\ntest_landform_derivations.py: all sections passed")
print(offline_harness.summary())
