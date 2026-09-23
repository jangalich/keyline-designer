"""
test_water_sources.py

THE WATER SECTION'S FOUR NEW FETCH MODULES AND THE TWO EXTENDED ONES,
offline: the requests each makes (mocked at the HTTP call, every
parameter inspected), the shapes each parser returns on synthetic
answers, and what each does under the refused network.

  1. NWI (nwi_data): the two-stage fetch asks for attributes only, then
     the small features by objectid at 1 m and the oversize ones at 10 m;
     the parser strips the joined-layer prefixes, clips to the window,
     repairs an invalid polygon and says which stage drew each feature;
     no features means no geometry request at all.
  2. FEMA (nfhl_data): three queries (availability, zones over the
     buffered window in UTM at a 5 m offset, panels); the parser turns
     the epoch-millisecond date into a date, -9999 into no BFE, an empty
     availability answer into available False.
  3. NLCD (nlcd_landcover_data): one exportImage on the boundary's DEM
     window with the year pinned in the mosaic rule; the parser maps
     unknown values -- including 250 -- to nodata and counts classes and
     groups; a non-image answer raises.
  4. NHDPlus HR (nhdplus_data): attributes only, keyed by
     permanent_identifier, square kilometres to acres.
  5. SSURGO (soil_water_table): the joined rows -> map units, components
     and months; the three states; a component with no month rows.
  6. NHD points (hydrology_data): upper-case or lower-case field names,
     the spring/seep filter.
  7. Under the refused network every fetch raises a RequestException at
     once, with attempts published.
"""

import json
from unittest import mock

import numpy as np
import requests
from shapely.geometry import Polygon, box, mapping

import offline_harness

offline_harness.install()

import hydrology_data  # noqa: E402
import nfhl_data  # noqa: E402
import nhdplus_data  # noqa: E402
import nlcd_landcover_data  # noqa: E402
import nwi_data  # noqa: E402
import soil_water_table  # noqa: E402
from reference_fixture import REAL_BOUNDARY  # noqa: E402

# ======================================================================
# 1. NWI
# ======================================================================
print("1. NWI: attributes, then fine geometry for the small features and coarse for the oversize; the parser")
calls = []
window = nwi_data.fetch_window(REAL_BOUNDARY)
minx, miny, maxx, maxy = window["bbox"]
small = box(minx + 10, miny + 10, minx + 60, miny + 60)
# A bow-tie: invalid as drawn, repaired by make_valid.
bowtie = Polygon([(minx - 50, miny - 50), (minx + 100, miny + 100), (minx - 50, miny + 100), (minx + 100, miny - 50)])


def _fake_get(url, params, max_retries=2):
    calls.append((url, dict(params)))
    if url == nwi_data.NWI_DATA_SOURCE_QUERY:
        return {"features": [{"attributes": {"PROJECT_NAME": "Test", "IMAGE_YR": 2020, "IMAGE_DATE": "xx/20", "SOURCE_TYPE": "CIR",
                                             "EMULSION": "CIR", "ALL_SCALES": "1 Foot", "STATUS": "Digital"}}]}
    if params.get("returnGeometry") == "false":
        return {"features": [
            {"attributes": {"Wetlands.OBJECTID": 1, "Wetlands.ATTRIBUTE": "PEM1C", "Wetlands.WETLAND_TYPE": "Freshwater Emergent Wetland",
                            "Wetlands.ACRES": 0.6, "NWI_Wetland_Codes.SYSTEM_NAME": "Palustrine", "NWI_Wetland_Codes.CLASS_NAME": "Emergent",
                            "NWI_Wetland_Codes.WATER_REGIME_NAME": "Seasonally Flooded"}},
            {"attributes": {"Wetlands.OBJECTID": 2, "Wetlands.ATTRIBUTE": "R2UBH", "Wetlands.WETLAND_TYPE": "Riverine",
                            "Wetlands.ACRES": 44740.0, "NWI_Wetland_Codes.SYSTEM_NAME": "Riverine", "NWI_Wetland_Codes.CLASS_NAME": "Unconsolidated Bottom",
                            "NWI_Wetland_Codes.WATER_REGIME_NAME": "Permanently Flooded"}},
        ]}
    ids = params["objectIds"]
    geometry = mapping(small) if ids == "1" else mapping(bowtie)
    return {"type": "FeatureCollection", "features": [{"type": "Feature", "id": int(ids), "properties": {"Wetlands.OBJECTID": int(ids)}, "geometry": geometry}]}


with mock.patch.object(nwi_data, "_get", _fake_get):
    raw = nwi_data.get_wetlands_for_boundary(REAL_BOUNDARY)
assert [u for u, _ in calls] == [nwi_data.NWI_WETLANDS_QUERY] * 3 + [nwi_data.NWI_DATA_SOURCE_QUERY]
attributes, fine, coarse = (p for _, p in calls[:3])
assert attributes["returnGeometry"] == "false" and attributes["geometryType"] == "esriGeometryEnvelope" and attributes["inSR"] == window["epsg"]
assert attributes["geometry"] == f"{minx},{miny},{maxx},{maxy}" and "Wetlands.ACRES" in attributes["outFields"]
assert fine["objectIds"] == "1" and fine["maxAllowableOffset"] == nwi_data.NWI_FINE_OFFSET_METERS == 1.0 and fine["outSR"] == window["epsg"]
assert coarse["objectIds"] == "2" and coarse["maxAllowableOffset"] == nwi_data.NWI_COARSE_OFFSET_METERS == 10.0
block = nwi_data.parse_wetlands(raw)
by_id = {f["objectid"]: f for f in block["features"]}
assert by_id[1]["code"] == "PEM1C" and by_id[1]["wetland_class"] == "Emergent" and by_id[1]["water_regime"] == "Seasonally Flooded"
assert by_id[1]["geometry_detail"] == nwi_data.DETAIL_FINE and abs(by_id[1]["geometry_utm"].area - 2500.0) < 1e-6
assert by_id[2]["geometry_detail"] == nwi_data.DETAIL_COARSE and by_id[2]["geometry_utm"].is_valid
assert by_id[2]["geometry_utm"].within(box(minx, miny, maxx, maxy).buffer(1e-6)), "clipped to the window"
assert block["project"]["image_year"] == 2020 and block["window"]["epsg"] == window["epsg"]
# No features: no geometry request.
calls.clear()
with mock.patch.object(nwi_data, "_get", lambda url, params, max_retries=2: (calls.append(url), {"features": []})[1]):
    empty = nwi_data.parse_wetlands(nwi_data.get_wetlands_for_boundary(REAL_BOUNDARY))
assert calls == [nwi_data.NWI_WETLANDS_QUERY, nwi_data.NWI_DATA_SOURCE_QUERY] and empty["features"] == [] and empty["project"] is None
print("   3 queries for 2 features (attributes, fine #1 at 1 m, coarse #2 at 10 m); the bow-tie repaired and clipped; empty -> no geometry query")

# ======================================================================
# 2. FEMA
# ======================================================================
print("2. FEMA NFHL: availability, zones in UTM at 5 m, panels; dates, BFE and availability parsed")
calls.clear()


def _fema_get(url, params, max_retries=2):
    calls.append((url, dict(params)))
    layer = int(url.rsplit("/", 2)[-2])
    if layer == nfhl_data.LAYER_AVAILABILITY:
        return {"features": [{"attributes": {"STUDY_ID": "42003C"}}]}
    if layer == nfhl_data.LAYER_FIRM_PANELS:
        return {"features": [{"attributes": {"FIRM_PAN": "42003C0065H", "EFF_DATE": 1411689600000, "SCALE": "12000",
                                             "PANEL_TYP": "Countywide, Panel Printed", "DFIRM_ID": "42003C"}}]}
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"FLD_ZONE": "X", "ZONE_SUBTY": "AREA OF MINIMAL FLOOD HAZARD", "SFHA_TF": "F", "STUDY_TYP": "NP",
                                           "STATIC_BFE": -9999, "DFIRM_ID": "42003C", "FLD_AR_ID": "42003C_1"},
         "geometry": mapping(box(minx - 1000, miny - 1000, maxx + 1000, maxy + 1000))},
        {"type": "Feature", "properties": {"FLD_ZONE": "AE", "ZONE_SUBTY": None, "SFHA_TF": "T", "STUDY_TYP": "NP",
                                           "STATIC_BFE": 1012.5, "DFIRM_ID": "42003C", "FLD_AR_ID": "42003C_2"},
         "geometry": mapping(box(minx, miny, minx + 40, miny + 40))},
    ]}


with mock.patch.object(nfhl_data, "_get", _fema_get):
    fema = nfhl_data.parse_flood_hazard(nfhl_data.get_flood_hazard_for_boundary(REAL_BOUNDARY))
layers_asked = [int(u.rsplit("/", 2)[-2]) for u, _ in calls]
assert layers_asked == [nfhl_data.LAYER_AVAILABILITY, nfhl_data.LAYER_FLOOD_ZONES, nfhl_data.LAYER_FIRM_PANELS]
zones_params = calls[1][1]
assert zones_params["outSR"] == window["epsg"] and zones_params["maxAllowableOffset"] == 5.0 and zones_params["f"] == "geojson"
assert calls[0][1]["returnGeometry"] == "false" and json.loads(calls[0][1]["geometry"])["rings"][0][0] == list(REAL_BOUNDARY[0])
assert fema["available"] is True and fema["study_ids"] == ["42003C"]
x, ae = fema["zones"]
assert x["zone"] == "X" and x["sfha"] is False and x["static_bfe"] is None and x["subtype"] == "AREA OF MINIMAL FLOOD HAZARD"
assert ae["zone"] == "AE" and ae["sfha"] is True and ae["static_bfe"] == 1012.5 and ae["subtype"] is None
assert x["geometry_utm"].within(box(minx, miny, maxx, maxy).buffer(1e-6)), "the county-wide polygon is clipped to the window"
assert str(fema["panels"][0]["effective_on"]) == "2014-09-26" and fema["panels"][0]["firm_pan"] == "42003C0065H"
unstudied = nfhl_data.parse_flood_hazard({"window": window, "availability": {"features": []}, "zones": {"features": []}, "panels": {"features": []}})
assert unstudied["available"] is False and unstudied["zones"] == [] and unstudied["panels"] == []
print("   layers 0, 28 (UTM, 5 m offset), 3; Zone X clipped, AE with a BFE, the panel date 2014-09-26; no study -> available False")

# ======================================================================
# 3. NLCD
# ======================================================================
print("3. NLCD: the DEM window, nearest neighbour, the year pinned; unknown values are nodata")
captured = {}


class _Response:
    def __init__(self, content, content_type="image/tiff", status=200):
        self.content, self.headers, self.status_code, self.text = content, {"content-type": content_type}, status, ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(str(self.status_code))


def _tiff(values: np.ndarray) -> bytes:
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin

    with MemoryFile() as memfile:
        with memfile.open(driver="GTiff", height=values.shape[0], width=values.shape[1], count=1, dtype="uint8",
                          crs="EPSG:32617", transform=from_origin(minx, maxy, 5, 5)) as dst:
            dst.write(values.astype(np.uint8), 1)
        return memfile.read()


from dem_data import dem_window_bounds  # noqa: E402

dem_window = dem_window_bounds(REAL_BOUNDARY)
width, height = dem_window["size"]
grid = np.full((height, width), 81, dtype=np.uint8)
grid[0, :] = 41
grid[1, :3] = 250     # NLCD's own no-data value
grid[1, 3] = 7        # not a class either


def _nlcd_get(url, params, timeout):
    captured.update(params)
    return _Response(_tiff(grid))


with mock.patch.object(nlcd_landcover_data.requests, "get", _nlcd_get):
    raw = nlcd_landcover_data.get_land_cover_for_boundary(REAL_BOUNDARY)
assert captured["size"] == f"{width},{height}" and captured["interpolation"] == "RSP_NearestNeighbor" and captured["pixelType"] == "U8"
assert captured["bboxSR"] == captured["imageSR"] == dem_window["epsg"]
assert json.loads(captured["mosaicRule"])["where"] == f"beginyear={nlcd_landcover_data.NLCD_YEAR}"
block = nlcd_landcover_data.parse_land_cover(raw)
assert block["array"].shape == (height, width) and block["year"] == nlcd_landcover_data.NLCD_YEAR and block["nodata_cells"] == 4
assert np.isnan(block["array"][1, :4]).all() and block["array"][0, 0] == 41 and block["array"][2, 0] == 81
counts = nlcd_landcover_data.class_counts(block["array"])
assert counts == {41: width, 81: width * height - width - 4}
assert nlcd_landcover_data.group_counts(counts) == {"forest": width, "pasture": width * height - width - 4}
with mock.patch.object(nlcd_landcover_data.requests, "get", lambda url, params, timeout: _Response(b"{}", "application/json")):
    try:
        nlcd_landcover_data.get_land_cover_for_boundary(REAL_BOUNDARY)
    except requests.exceptions.RequestException:
        pass
    else:
        raise AssertionError("a non-image answer must raise")
print(f"   exportImage {captured['size']} at {dem_window['crs']}, mosaic rule beginyear={nlcd_landcover_data.NLCD_YEAR}; 250 and 7 -> nodata")

# ======================================================================
# 4. NHDPlus HR
# ======================================================================
print("4. NHDPlus HR: attributes only, keyed by permanent_identifier, km² to acres")
captured.clear()
answer = {"features": [{"attributes": {"permanent_identifier": "123", "gnis_name": "Test Run", "fcode": 46006, "streamorde": 3,
                                       "streamleve": 4, "totdasqkm": 4.0468564224, "divdasqkm": 4.0, "areasqkm": 0.5, "lengthkm": 1.2,
                                       "nhdplusid": 99, "reachcode": "05"}}]}
with mock.patch.object(nhdplus_data.requests, "get", lambda url, params, timeout: (captured.update(params), _Json(answer))[1]):
    class _Json:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    parsed = nhdplus_data.parse_flowline_attributes(nhdplus_data.get_flowline_attributes_for_boundary(REAL_BOUNDARY))
assert captured["returnGeometry"] == "false" and "streamorde" in captured["outFields"] and captured["geometryType"] == "esriGeometryEnvelope"
assert parsed["123"]["stream_order"] == 3 and abs(parsed["123"]["total_drainage_acres"] - 1000.0) < 1e-6
assert parsed["123"]["gnis_name"] == "Test Run" and parsed["123"]["catchment_sqkm"] == 0.5
assert nhdplus_data.parse_flowline_attributes({"features": [{"attributes": {"streamorde": 1}}]}) == {}, "no identifier, no row"
print("   1 query, 4.0469 km² -> 1,000.0 ac, order 3")

# ======================================================================
# 5. SSURGO
# ======================================================================
print("5. SSURGO: rows -> map units, components, months; wet, deeper, no data")
rows = []
base = {"mukey": "1", "muname": "Test loam", "areasymbol": "PA003", "saverest": "9/5/2025", "wtdepannmin": "30", "wtdepaprjunmin": "30",
        "flodfreqdcd": "None", "pondfreqprs": "0", "drclassdcd": "Moderately well drained", "hydclprs": "5"}
for m in range(1, 13):
    rows.append(dict(base, cokey="a", compname="Alpha", comppct_r="60", majcompflag="Yes", hydricrating="No", drainagecl="Moderately well drained",
                     comonthkey=f"a{m}", month=soil_water_table.MONTHS[m - 1], monthseq=str(m), flodfreqcl="None", floddurcl=None,
                     pondfreqcl=None, ponddurcl=None, ponddep_r=None, wet_top_cm="30" if m <= 4 else None, described_bottom_cm="152", layer_count="2"))
rows.append(dict(base, cokey="b", compname="Beta", comppct_r="40", majcompflag="Yes", hydricrating="No", drainagecl="Well drained",
                 comonthkey=None, month=None, monthseq=None, flodfreqcl=None, floddurcl=None, pondfreqcl=None, ponddurcl=None, ponddep_r=None,
                 wet_top_cm=None, described_bottom_cm=None, layer_count=None))
block = soil_water_table.parse_seasonal_water_table(rows)
assert list(block["map_units"]) == ["1"] and block["map_units"]["1"]["components"] == ["a", "b"] and block["map_units"]["1"]["wtdepannmin_cm"] == 30.0
alpha, beta = block["components"]["a"], block["components"]["b"]
assert alpha["has_rows"] and len(alpha["months"]) == 12 and alpha["months"][1]["wet_top_cm"] == 30.0 and alpha["months"][7]["wet_top_cm"] is None
assert alpha["months"][7]["described_bottom_cm"] == 152.0 and alpha["months"][7]["layer_count"] == 2 and alpha["months"][1]["flood"] == "None"
assert alpha["months"][1]["pond"] is None, "a NULL frequency stays None -- 'not stated', never 'None'"
assert beta["has_rows"] is False and beta["months"] == {} and beta["major"] is True and beta["comppct"] == 40.0
assert block["survey_areas"] == [{"areasymbol": "PA003", "saverest": "9/5/2025"}]
sql = soil_water_table.seasonal_water_table_sql("polygon((0 0, 1 0, 1 1, 0 0))")
assert "LEFT JOIN comonth" in sql and "soimoiststat = 'Wet'" in sql and "SDA_Get_Mukey_from_intersection_with_WktWgs84" in sql
print("   Alpha wet Jan-Apr at 30 cm then deeper than 152; Beta no rows -> no data; wtdepannmin 30")

# ======================================================================
# 6. NHD points
# ======================================================================
print("6. NHD points: field case, the spring/seep filter")
with mock.patch.object(hydrology_data, "_query_layer", return_value=[
    {"properties": {"GNIS_NAME": "Cold Spring", "FCODE": 45800, "FTYPE": 458, "PERMANENT_IDENTIFIER": "p1"}, "geometry": {"type": "Point", "coordinates": [-79.98, 40.64]}},
    {"properties": {"gnis_name": None, "fcode": 36700, "ftype": 367, "objectid": 7}, "geometry": {"type": "Point", "coordinates": [-79.98, 40.65]}},
]):
    points = hydrology_data.get_nhd_points_for_boundary(REAL_BOUNDARY)
assert [p["feature_code"] for p in points] == [45800, 36700] and points[0]["name"] == "Cold Spring" and points[1]["permanent_identifier"] == 7
assert [p["name"] for p in hydrology_data.springs_and_seeps(points)] == ["Cold Spring"]
assert hydrology_data.springs_and_seeps(None) == [] and hydrology_data.SPRING_SEEP_FCODE == 45800
print("   two points, one spring")

# ======================================================================
# 7. The refused network
# ======================================================================
print("7. under the refused network every fetch raises a RequestException at once")
offline_harness.clear()
for module, function in ((nwi_data, nwi_data.get_wetlands_for_boundary), (nfhl_data, nfhl_data.get_flood_hazard_for_boundary),
                         (nlcd_landcover_data, nlcd_landcover_data.get_land_cover_for_boundary),
                         (nhdplus_data, nhdplus_data.get_flowline_attributes_for_boundary),
                         (hydrology_data, hydrology_data.get_nhd_points_for_boundary)):
    try:
        function(REAL_BOUNDARY)
    except requests.exceptions.RequestException:
        pass
    else:
        raise AssertionError(f"{module.__name__} must raise with no network")
    assert module.LAST_FETCH_ATTEMPTS == 3, (module.__name__, module.LAST_FETCH_ATTEMPTS)
try:
    soil_water_table.get_seasonal_water_table_for_boundary(REAL_BOUNDARY)
except requests.exceptions.RequestException:
    pass
else:
    raise AssertionError("SDA must raise with no network")
print(f"   {offline_harness.summary()}")

print("\ntest_water_sources.py: all sections passed")
