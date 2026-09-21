# Site data report — investigation and proposal

Standing reference for the replacement of the narrated Scale of Permanence PDF
with a **site data collection report**: nine property-wide inventory sections,
the rendered layout map, then a design record built from the committed steps.
No LLM narration anywhere.

Code examined: backend `jangalich/keyline-designer` at `f010088`; frontend
`jangalich/keyline-designer-frontend` at `6ce1a8d` (the checkout is `9b87a54`,
one commit ahead, which changes only `vercel.json`). Nothing was executed
against the network and nothing was changed.

Two documents the code cites — the pipeline architecture guide
(`pipeline-architecture-guide.md` / `architecture-guide.md`) and
`water-standards-alignment.md` — are **not in either repository** and are not
in either repository's git history. Statements below about "the architecture
guide's hard-fail principle" are taken from the code that implements it
(`parcel_data.py`'s module docstring, `session_manager.py`, `session_cache.py`),
not from the guide itself. The **VERIFY-BEFORE-BUILD** convention is taken
from the brief.

Contents

1. Field inventory
2. Source assessments (classes D and E)
3. Recommended v1 scope
4. The hard-fail question
5. Report pipeline findings
6. The acreage discrepancy
7. Honest limits per section
8. Build sequence
9. Open decisions
10. VERIFY-BEFORE-BUILD list

---

## 0. Classification key and what exists today

| Class | Meaning |
|---|---|
| **A** | Already a `ParcelData` field (or on the session context / committed document) |
| **B** | Computable from A with no fetch |
| **C** | An existing source, extra columns, years or parameters |
| **D** | New live source, fetched per parcel |
| **E** | Static reference bundled with the tool, refreshed occasionally |
| **F** | No public source |

**What `ParcelData` holds at `f010088`** (`parcel_data.py`, twelve fetches, in
order): `dem` (USGS 3DEP `exportImage`, 5 m UTM grid, boundary + 100 m,
≤300×300), `soil_components` (SDA `mapunit`+`component`: mukey, muname,
compname, comppct_r, drainagecl, slope_r, slopelenusle_r, hydricrating,
taxorder, hydgrp), `farmland_classification` (`mapunit.farmlndcl`),
`erosion_factor` (`chorizon.kwfact`, shallowest mineral horizon, dominant
component per mukey), `saturated_hydraulic_conductivity` (`chorizon.ksat_r`,
same selection), `soil_geometries` (`mupolygon` clipped to the parcel, per
mukey), `water_features` (NHD flowline layer 6 and waterbody layer 12, bbox +
150 m, name/fcode/geometry/permanent_identifier), `farm_roads` (USGS National
Map transportation layers 30/31/32, bbox + 150 m, name/geometry),
`climate_summary` (Open-Meteo ERA5 archive, last 10 complete years, daily
tmax/tmin/precip/wind → eight summary numbers; the daily arrays are discarded),
`canopy_height` (3DEP lidar HAG on the DEM grid, or NLCD TCC 30 m fallback;
`source` says which), `imagery_summary` (Sentinel-2 L2A NDVI buckets, scene
date, cloud cover), `irradiance` (NREL PVWatts, optional, needs `NREL_API_KEY`).
Plus `boundary_polygon_utm`. The session warm-up adds `valleys`, `keypoints`,
`exclusion_zones` (per-gate acreages: slope, canopy, hydric, roads, setback,
pairwise overlaps, eligible acres) — all class A for this report.

Everything below that is class B was checked against a concrete function that
already exists: `raster_grid.elevation_range_in_polygon`,
`terrain_metrics.compute_slope_and_aspect` (Horn, NaN on edges),
`raster_grid.cells_in_polygon`, `valley_delineation.compute_flow_accumulation`,
`canopy_height_data.tree_root_zone_mask`, `soil_data.hydric_disqualifying_mukeys`,
`water_suitability._stream_permanence_label` (NHD FCode 46006/46003/46007),
`farm_roads_data.get_road_exclusion_union_utm`, shapely area/length/distance.

---

## 1. Field inventory

Every field, its class, its source, and what the report can honestly say.
"Footer" is the caveat that must sit under the section (expanded in §7).

### I. Site overview

| Field | Class | Source / computation | Notes |
|---|---|---|---|
| Address / label | A (plumbed) / F (today) | `property_label` on the report request; the route defaults to "Property Design Report" | **The frontend never sends it**: `ReportAction.jsx` calls `actions.generateReport()` with no argument, and the address typed into `AddressSearch.jsx` (Census geocoder `matched_address`) is not kept on the session or the document. Retaining it is a small frontend change; until then the cover carries the default label. |
| Centroid (lat/lon) | B | `boundary_polygon_utm.centroid` warped to EPSG:4326 (the irradiance centroid code in `parcel_data.py` already does this) | |
| Parcel acreage | A | `SessionDesign.parcel_acres` (`boundary_polygon_utm.area / SQUARE_METERS_PER_ACRE`) | 13.234 ac on the reference parcel; round once. |
| Perimeter | B | `boundary_polygon_utm.length` | Also feeds §VIII. |
| County and state | D | Census Geocoder reverse lookup (`geocoder/geographies/coordinates`) — same service `geocode.py` already calls forward | Free, no key. VERIFY. Fallback: parse from `property_label` is not reliable; report "not resolved". |
| UTM zone / CRS | A | `dem['crs']` | |
| Elevation range and relief | B | `elevation_range_in_polygon(dem, boundary)` — already computed for the current report | Cell count and ~5 m resolution come with it. |
| DEM source resolution (1 m lidar vs 1/3 arc-second) | F today, D possible | `exportImage` returns a resampled 5 m grid and does not say which source resolution underlies it; 3DEP's index services could answer | VERIFY. v1: state "USGS 3DEP, resampled to 5 m". |
| Lidar acquisition (HAG path) | B | `canopy_height['source_item_id']` (STAC item id encodes the 3DEP project name and year) | Only on the HAG path; TCC path has an NLCD year instead. |
| Satellite scene date, cloud cover | A | `imagery_summary.scene_date`, `cloud_cover_pct`, `days_since_scene` | Sentinel-2; this is the NDVI source, **not** the map's basemap. |
| Basemap (NAIP) vintage | F today, D possible | `render_layout_map.py` composites USGSImageryOnly XYZ tiles, which carry no date; NAIP date would need a NAIP STAC/ImageServer query | VERIFY. v1: "USGS NAIP basemap, vintage not queried". |
| SSURGO survey area and version date | C | SDA `sacatalog` (`areasymbol`, `saverest`) for the intersecting survey area | Column names VERIFY against SDA schema. |
| NHD / transportation vintage | F | The MapServer query returns no dataset date | State the service name only. |
| Climate period | B | `years_analyzed` and the start/end years the fetch chose | Becomes 30 years under §II. |
| Data-vintage table (all sources) | B/C | Assembled from the rows above | The honest limit of several rows is "not published by the service". |

### II. Climate

Everything here comes from **one Open-Meteo archive call extended from 10 to
~30 years and made to return its daily arrays** instead of only the eight
summary numbers `climate_data.get_climate_summary_for_point` returns today.
That is a class C change to an existing Layer 1 fetch (three times the payload,
one request, no new host).

| Field | Class | Source / computation | Notes |
|---|---|---|---|
| Median last spring frost, first fall frost | C | Daily `temperature_2m_min` ≤ 0 °C, per year, then median date over 30 years | Requires the daily arrays, which the current function discards. |
| Frost-free days | C | Per-year gap between the two dates, median | |
| Annual precipitation | A→C | Already `avg_annual_precipitation_mm`; recompute over 30 years | |
| Monthly mean high / low | C | Daily tmax/tmin grouped by month | |
| Monthly precipitation | C | Daily `precipitation_sum` by month | |
| Monthly and annual GDD base 50 °F | C | From daily tmax/tmin, standard (tmax+tmin)/2 − 50 with tmax capped at 86 °F if that convention is wanted | State the cap convention in the footer. |
| Monthly solar kWh/m²/day | C | Open-Meteo daily `shortwave_radiation_sum` (MJ/m²) → kWh; or PVWatts `solrad_monthly` from the optional irradiance layer | Open-Meteo path keeps it in one call and needs no key. VERIFY the variable name. |
| Estimated hardiness zone | B (from C data) or D | Mean annual extreme minimum over the period → zone table (USDA PHZM definition uses 1991–2020 PRISM; a reanalysis estimate will disagree by up to a zone) | Label "est." as the brief does. Official map: D, VERIFY. |
| Prevailing wind, heaviest day, record high/low | A | Existing summary fields | Keep; the interactive product does not use them either, so they are report-only already. |
| Reanalysis model and grid | B | Open-Meteo returns the model/elevation used; record it | For the footer. |

### III. Landform

| Field | Class | Source / computation | Notes |
|---|---|---|---|
| Elevation range, relief, cell count | B | `elevation_range_in_polygon` | Already in the current report. |
| Slope distribution by class (e.g. 0–3, 3–8, 8–15, 15–25, >25 %) | B | `compute_slope_and_aspect(dem.array, resolution)` masked to on-parcel cells (`cells_in_polygon`) | Horn's method returns NaN on grid edges and beside nodata; the DEM has a 100 m buffer so the parcel is interior. The exclusion warm-up already computed a slope raster (`slope_pct` on the exclusion result) — reuse it rather than recompute. |
| Aspect distribution (8 sectors, plus flat) | B | Same call; `aspect_to_compass_label` | |
| Steep ground acreage (> production slope gate) | A | `exclusion_zones.narrative_data.slope_detail.too_steep_acres` | Property-wide, not a selection. |
| Valley count, each valley's max contributing area | A | `SessionDesign.valleys` (`max_contributing_area_acres`) | Warm-up product, no user decision. |
| Keypoints: count, elevation, contributing acres, slope above/below, on/off parcel | A | `SessionDesign.keypoints` | Same. The brief's "count and location" — location as cardinal position (`report_generator._locative_descriptor`) or as marks on the landform map. |
| Landform map (contours, valleys, keypoints) | B | `contour_lines.compute_contour_lines(dem)` + valley `geometry_wgs84` + keypoint points; a renderer without the layout layers | New renderer; the pieces exist in `render_layout_map.py`. |

### IV. Water and hydrology

| Field | Class | Source / computation | Notes |
|---|---|---|---|
| NHD streams and water bodies on/adjacent | A | `water_features` (bbox + 150 m) | "Adjacent" means within the fetch box, not a measured buffer. |
| Stream permanence (perennial/intermittent/ephemeral) | B | `feature_code` → `water_suitability._stream_permanence_label` | NHD FCode 46006/46003/46007; 46000 unknown. |
| Distance from boundary to nearest stream | B | shapely distance in UTM over the fetched features | Bounded by the 150 m fetch box: "none within 150 m" is the honest negative. |
| Stream length inside the parcel | B | Intersection length | |
| Stream order | C/D | Not on NHD MapServer layer 6; NHDPlus HR flowline VAA (`StreamOrde`) via the NHDPlus HR service | VERIFY; NHD itself is being superseded by 3DHP. |
| Contributing area at the parcel's outlet(s) | B, limited | `compute_flow_accumulation` on the fetched DEM | The DEM window is boundary + 100 m, so accumulation is truncated at the window edge; report as "at least N acres within the analysed window". The valleys' own `max_contributing_area_acres` carry the same limit. |
| Hydric soil extent (predominantly hydric, partially hydric) | B | `hydric_disqualifying_mukeys(soil_components)` and per-component `is_hydric` × `soil_geometries` areas clipped to the parcel | Acres by mukey. |
| Depth to water table, flooding and ponding frequency | C | SDA `muaggatt.wtdepannmin`, `flodfreqdcd`, `pondfreqprs` | Column names VERIFY. |
| FEMA flood zone(s) | D | FEMA NFHL `S_FLD_HAZ_AR` | See §2. Unmapped counties are a real outcome. |
| NWI wetlands | D | USFWS Wetlands MapServer | See §2. |
| Hydrology map (streams, hydric mukeys, valleys) | B | Existing geometry | New renderer. |

### V. Access

| Field | Class | Source / computation | Notes |
|---|---|---|---|
| Mapped public roads within 150 m: names, count | A | `farm_roads` | Layers 30/31/32 only; layers 8/9 (small-scale) excluded by design. |
| Nearest public road and distance | B | shapely distance boundary → road centerline | Bounded by the 150 m fetch box. |
| Public road frontage length | B | Length of boundary within a small tolerance (e.g. `ROAD_EXCLUSION_BUFFER_METERS` + right-of-way allowance) of a road centerline | A centerline is not a right-of-way edge; state the tolerance used. |
| Existing farm tracks and driveways | F | Not in USGS transportation data (`FARM_ROAD_CONFIDENCE_NOTES` says so) | Report "not in public data". Could be read from imagery by the user; not by this tool. |
| Road right-of-way acreage excluded from production | A | `exclusion_zones` roads layer acres | |
| Committed access point | A | Roads commit input | Belongs to the design record, not the inventory. |

### VI. Trees and forestry

| Field | Class | Source / computation | Notes |
|---|---|---|---|
| Canopy extent (acres, % of parcel) | B | HAG path: cells ≥ `CANOPY_HEIGHT_THRESHOLD_METERS` (4.5 m); TCC path: nonzero cover, 30 m | State which source, per `canopy_height['source']`. |
| Canopy height distribution | B (HAG only) | Histogram of HAG over canopy cells | Not available on the TCC path (percent cover, not height); say so rather than substitute. |
| Percent cover distribution | B (TCC only) | | |
| Canopy root-zone exclusion acres | A | `exclusion_zones` canopy layer | |
| Forest type / cover class | D or E | LANDFIRE EVT (30 m ImageServer), NLCD Land Cover (30 m), or USFS FIA forest-type-group raster (static, 250 m) | 250 m on a 5–30 acre parcel is one to five pixels. See §2. |
| NDVI land-cover buckets | A | `imagery_summary` | Cannot separate hayfield from canopy; already labelled so. |

### VII. Buildings and utilities

| Field | Class | Source / computation | Notes |
|---|---|---|---|
| Existing structures (count, footprints) | D or E | FEMA USA Structures (ArcGIS FeatureServer), Microsoft/Overture building footprints (static, ODbL/CDLA), OSM Overpass | See §2. Nothing in the pipeline reads buildings today. |
| Distance to transmission lines | D/E, weak | HIFLD / EIA transmission line data | Transmission (≥69 kV) is the wrong network for a farm building; **distribution lines are not public → F**. Report only if the product is comfortable with the caveat. |
| Broadband availability | D, gated | FCC National Broadband Map (BDC) | Address/Fabric-location keyed; API access terms unclear. See §2. |
| Committed structure sites | A | Design record, not inventory | |

### VIII. Fencing and animals

| Field | Class | Source / computation | Notes |
|---|---|---|---|
| Perimeter length | B | `boundary_polygon_utm.length` in feet | The fencing step's boundary fence is the *developed footprint*, not the parcel perimeter — different number, different question. |
| Stream exclusion fencing length | A | Fencing narrative `narrative_only.stream_exclusion.total_length_ft` (computed on generate) | Available once fencing is generated; property-wide (buffers real NHD streams). |
| Browsers (deer, rabbit, groundhog) occurrence | D | GBIF occurrence search by polygon + buffer | Presence-only. |
| Deer density / pressure | E (per state) / F (national) | PA Game Commission WMU harvest reports (PDF, annual); no national dataset | See §2 and §3 on the Pennsylvania rule. |
| Predators (coyote, fox, raccoon, mink) | D / E | GBIF occurrence; or a static "expected present" statement by state from range data | Range data (IUCN) is non-commercial licensed. |
| Raptors and crop-pest birds | D | eBird API (30-day recency, key), or GBIF (eBird data is in GBIF) | See §2. |
| Federally listed species that may occur | D | USFWS ECOS/IPaC services; species-by-county lists | Informational, never a determination. |
| State natural heritage screening | F (national) / E (PA only, and account-gated) | PA Conservation Explorer / PNDI | Do not generalise the PA tool. |

### IX. Soils and geology

| Field | Class | Source / computation | Notes |
|---|---|---|---|
| Map unit table: symbol, name, acres, % of parcel | A+B | `soil_components` (muname; `musym` needs adding — C) × `soil_geometries` clipped areas | `musym` is on `mapunit`; add to the SELECT. |
| Dominant component, drainage class | A | `compname`, `drainagecl`, first row per mukey | |
| Hydrologic group | A | `hydgrp` (component; dominant) | |
| Ksat (shallowest mineral horizon) | A | `saturated_hydraulic_conductivity` | µm/s; class breakpoints in `soil_data.py`. |
| K-factor | A | `erosion_factor` | |
| Farmland classification | A | `farmland_classification` | |
| Capability class and subclass (non-irrigated) | C | `component.nirrcapcl`, `nirrcapscl` | VERIFY column names against SDA. |
| Depth to bedrock | C | `muaggatt.brockdepmin`, or `corestrictions` (`reskind`, `resdept_r`) | VERIFY. |
| Available water capacity | C | `chorizon.awc_r` (sum over root-zone depth is more work than the shallowest horizon) | Decide the depth convention. |
| Forage / crop yields | C | `cocropyld` (`cropname`, `nonirryield_r`, `yldunits`) | Sparse; many components carry none. VERIFY. |
| Hydric rating by map unit | B | `hydric_disqualifying_mukeys` + per-component sums | |
| Companion soil map (polygons labelled by symbol) | B | `soil_geometries` + boundary; new renderer | Requires `musym` (C). |
| Bedrock geology (unit name, lithology, age) | D or E | USGS State Geologic Map Compilation (SGMC) | See §2. |
| Survey vintage | C | `sacatalog.saverest` | |

### Design record (section 3 of the report)

Per committed step, the panel data the user saw. What each committed feature
carries on the wire (durable, in the Design Document) versus what only the
step's generate result carries (tier-2 cache, evictable):

| Step | On the committed feature (document) | Only on the generate result's `narrative_data` | Frontend rows shown |
|---|---|---|---|
| Landform | `area_acres` (**opened** acreage, see §6), advisory block: rank, suitability_score, four factors, avg_slope_pct, aspect_deg, soil_carved_*, soil_data_available, source_patch_id; exclusion crossings for drawn blocks | slope min/max/median, dominant aspect word, position in parcel, elevation percentile/position, soil component list and drainage class, hole count | acres, score; aspect, position, median slope, soil run, drainage |
| Water | The full measurement contract (`_zone_feature_properties`): zone_acres, survey_type, rank, mean/max suitability, criterion contributions, overlaps, gravity relationship, pinch/catchment fields, flags | The curated `panel` rows and the step's `scales`; `soil_checked` | acres (survey), score; water delivery, elevation above block, block served, binding shoulder / max depth, three overlaps, cross-type agreement |
| Roads | Per branch: length_ft, avg/max grade, newly_served_acres, role, network_id, total_length_ft | Network `access` (served acres, served %), `determination`, `crossings`, quality score | length, avg grade, max grade; crossings |
| Trees | score, four factors, avg_slope_pct, slope_median_pct, elevation percentile, rank, three data-available flags | marginal_benefits list, elevation_position word, weights | acres, score; benefits |
| Structures | rank, composite, four factor scores, slope, aspect, three distances, production relationship, road source, constraints, prime farmland, site_origin, solar rating/value, elevation position | gates block, run flags, weights | score; the rows above; siting rules broken |
| Fencing | fence_type, length_ft, loop_count, fence_index/count | per-type totals, narrative_only stream length | feet per type |

`session_design.py` exists to reach the right-hand column and raises
`SessionWorkingDataExpiredError` when it cannot. See §5 and §9 for what that
means for the design record.

---

## 2. Source assessments — classes D and E

**The sandbox has no network.** Every endpoint, term of use and rate limit
below is stated from documentation as remembered and from what the code
already targets; none was called. Each row's claims are collected in §10.

### 2.1 Sources the code already targets (relevant to class C changes)

**Open-Meteo Historical Weather API** — `archive-api.open-meteo.com/v1/archive`
(`climate_data.py`). No key today. Coverage global; ERA5 at ~25 km (0.25°),
ERA5-Land at ~9–11 km where the API selects it; hourly reanalysis aggregated to
daily. At 5–30 acres this is the regional climate, not the parcel's. Reliability
in this codebase: the one Layer 1 fetch with no retry loop (a single 60 s
`requests.get`). **License and terms: the free API is stated by Open-Meteo as
non-commercial use only; commercial use requires a paid subscription and an API
key on a customer endpoint. Data are CC BY 4.0.** The current product already
calls the free endpoint on the session-creation path of what is meant to be a
paid product. This predates this report but the report is where the climate
data becomes the product. VERIFY-BEFORE-BUILD, and see §9.

**USDA Soil Data Access** — `sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest`.
No key, public domain, national (CONUS + territories with survey coverage),
1:24,000 survey scale. Every class C soils row is a column added to an existing
query; the codebase's own rule applies: verify each column name against a
published SDA example query before trusting it (`farmlndcl` and `kwfact` both
failed that test once).

**USGS National Map services** (NHD, transportation, 3DEP) — no key, public
domain. NHD is being superseded by 3DHP; the `hydro.nationalmap.gov/.../nhd`
MapServer the code targets still answered at the last live run recorded in the
README, but its retirement date is not in the repo. VERIFY before adding
NHDPlus HR fields.

**Planetary Computer STAC** (Sentinel-2, 3DEP HAG) — free, signed URLs, no key.
A NAIP collection exists on the same catalog if the basemap vintage is wanted.

**NREL PVWatts** — `developer.nlr.gov` (README notes the domain migration is
itself unverified live). Free key. Monthly `solrad_monthly` is in its outputs.

### 2.2 New live sources (class D)

| Source | Endpoint (VERIFY) | Auth / limits | License, commercial use | Coverage | Resolution at 5–30 ac | Known reliability |
|---|---|---|---|---|---|---|
| **Census Geocoder, reverse** (county/state) | `geocoding.geo.census.gov/geocoder/geographies/coordinates?x=&y=&benchmark=Public_AR_Current&vintage=Current_Current&format=json` | None | Public domain | National | Exact (polygon lookup) | The forward geocoder is already in production use here; the reverse endpoint is the same service. |
| **FEMA NFHL** (flood zones) | `hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer`, flood hazard area layer (`S_FLD_HAZ_AR`), ArcGIS `query` with the parcel polygon | None | Public domain | National **where mapped**; many rural counties have no digital FIRM ("not mapped" is a real answer) | Panel scale ~1:24k; AE/A/X zones as polygons | Service is known to be slow and to time out under load; same retry pattern as `hydrology_data._query_layer`. |
| **USFWS National Wetlands Inventory** | `fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer/0`, `query` by polygon | None | Public domain | National; vintage varies by state from 1980s photo-interpretation to recent | 1:24k; Cowardin codes | Moderate; occasional outages. |
| **NHDPlus HR** (stream order) | `hydro.nationalmap.gov/arcgis/rest/services/NHDPlus_HR/MapServer` flowline layer with VAA fields (`StreamOrde`) | None | Public domain | National, but NHDPlus HR is not complete everywhere | Reach-level | Same host as NHD; same retirement question. |
| **LANDFIRE EVT** / **NLCD Land Cover** (forest type / cover class) | LANDFIRE ImageServer (`landfire.gov/arcgis/rest/services/...`), or the IIPP host already used for TCC (`imagery.geoplatform.gov/iipp/rest/services/...`) | None | Public domain | CONUS | 30 m: 40–500 pixels on the parcel | IIPP host confirmed live for TCC; LANDFIRE host unverified. |
| **FEMA USA Structures** (existing buildings) | ArcGIS FeatureServer (`services2.arcgis.com/.../USA_Structures_View/FeatureServer`) | None | FEMA/ORNL, public use | National (2018–2022 imagery) | Building footprints; occupancy class where known | Unverified; large service. |
| **OSM Overpass** (buildings, tracks) | `overpass-api.de/api/interpreter` | None; public instance rate-limits and disallows heavy commercial use | ODbL: attribution and share-alike on derived databases | Global, uneven rural completeness | Vector | Public instance is not an SLA. Not recommended for a paid product. |
| **FCC National Broadband Map (BDC)** | Public API requires a registered account token; availability is keyed by Fabric location id, and the Fabric itself is CostQuest-licensed | Account | Availability data are public; Fabric is not | National | Per serviceable location | High risk of not being buildable as a lat/lon lookup. Recommend deferral. |
| **HIFLD / EIA transmission lines** | HIFLD Open (retired/moved during 2024–25) or EIA's atlas ArcGIS services | None | Public | National | Transmission only (≥69 kV) | Host instability; distribution lines are F anyway. |
| **GBIF occurrence API** | `api.gbif.org/v1/occurrence/search?geometry=<WKT>&hasCoordinate=true&taxonKey=…&license=CC0_1_0,CC_BY_4_0&limit=300` | None for search; no published rate limit (be polite) | Per-dataset CC0 / CC BY / **CC BY-NC**; filter out BY-NC for a paid product, which drops most iNaturalist records | Global | Point records with `coordinateUncertaintyInMeters`; buffer the parcel by 1–5 km or nothing is found | Reliable; eBird and iNaturalist are both republished through it. |
| **iNaturalist API v1** | `api.inaturalist.org/v1/observations?nelat…&quality_grade=research&taxon_id=` | None for reads; ~100 req/min, ~10k/day | Observer-chosen; most research-grade records are CC BY-NC; API terms restrict commercial use | Global | Points, obscured for sensitive taxa | Reliable. Recommend reaching it through GBIF with the license filter instead. |
| **eBird API 2.0** | `api.ebird.org/v2/data/obs/geo/recent?lat&lng&dist&back=30` | Free key; per-key limits | Terms of use restrict to non-commercial/personal use unless agreed with Cornell | Global | Hotspot/point, 30-day recency only | Reliable, but the recency window makes it a poor "what occurs here" source. Historical data (EBD) is by request. |
| **USFWS ECOS / IPaC** (listed species) | ECOS critical-habitat FeatureServer; IPaC has no supported public API for third-party products; species-by-county report | None / n/a | Public domain | National | County or range polygon; critical habitat as polygons | IPaC resource lists are meant to be generated interactively; do not present a scrape as an IPaC result. |
| **USGS SGMC** (bedrock geology) | `mrdata.usgs.gov/services/sgmc2` (WMS/WFS) or the downloadable geodatabase | None | Public domain | National | 1:100k–1:500k source maps; one or two units on a parcel | Static download makes this a class E candidate (large). |
| **NAIP vintage** | Planetary Computer `naip` collection (item datetime) or USDA APFO ImageServer | None | Public domain | CONUS | Scene date | The map's tile service is a different product; the date would be *a* NAIP date for the area, not proof of which tiles were composited. |

### 2.3 Static reference candidates (class E)

| Reference | Source | Refresh | Coverage | Notes |
|---|---|---|---|---|
| Hardiness zone lookup table (mean annual extreme minimum → zone) | USDA PHZM definition | Rarely | National | Tiny; makes the zone a class B derivation from the 30-year daily data. |
| Ksat / hydrologic group / capability class glossaries | NRCS Soil Survey Manual | Rarely | n/a | Already partly in `soil_data.py` comments. |
| NHD FCode → permanence | Already in `water_suitability.py` | Rarely | n/a | Class A. |
| Deer density / harvest by WMU | **Pennsylvania Game Commission annual reports (PDF)** | Annual | **Pennsylvania only** | No national equivalent exists (state agencies publish harvest, not density, on their own schedules and units). Ship as a per-state table only where a source is bundled; the section says "no density figure available for this state" elsewhere. |
| Predator / browser "expected present" statements | Species range compilations (IUCN Red List ranges are **non-commercial** licensed; NatureServe is licensed; USGS GAP species ranges are public domain) | Rarely | National (GAP) | GAP ranges are 12-digit HUC polygons; public domain; large download. VERIFY. |
| Federally listed species by county | ECOS county lists (downloadable) | Quarterly-ish | National | Small per state. Still informational. |
| Bedrock geology (SGMC) | USGS download | Rarely | National | Multi-GB geodatabase; per-state extracts are practical. |
| Building footprints (Microsoft / Overture) | Static per-state files | Yearly | National | ODbL / CDLA-permissive; multi-GB; needs a spatial index at deploy time. Heavy for a single-container Render/Railway deploy. |
| USFS forest type group raster | FIA / TreeMap | Rarely | CONUS | 250 m (older) or 30 m (TreeMap 2016); large. |

### 2.4 Pennsylvania-specific sources and national equivalents

| PA source | What it gives | National equivalent | Rule |
|---|---|---|---|
| PA Game Commission WMU deer data | Harvest and density estimates per WMU | **None.** Each state game agency publishes its own; units, metrics and formats differ. | Bundle per state as class E where a source is vetted; never cite PA figures for another state. |
| PA Conservation Explorer / PNDI | Natural heritage review (account, project-based) | **None at the same function.** USFWS IPaC covers federally listed species only; NatureServe is licensed; state heritage programs vary. | Screening in the report is federal-only and informational; state review is named as a step the reader takes, not a figure the report shows. |
| PA DCNR PAMAP lidar / geology | State lidar, state geologic map | USGS 3DEP (already used), USGS SGMC | Use the national one. |

---

## 3. Recommended v1 scope

Favour sections built from classes A–C; propose a section smaller or deferred
rather than padded.

| Section | v1 | Content in v1 | Deferred |
|---|---|---|---|
| I. Site overview | **Ship** | Label, centroid, acreage, perimeter, elevation range, scene date, data-vintage table (with "not published" rows) | County/state via Census reverse geocode (class D but cheap and same service family — see §9 D7) |
| II. Climate | **Ship, first** | Everything in §1-II from the 30-year extended fetch; est. hardiness zone | Official PHZM lookup |
| III. Landform | **Ship** | Elevation, slope and aspect distributions, steep acres, valleys, keypoints, landform map | — |
| IV. Water | **Ship reduced** | NHD features with permanence, distance, in-parcel length, hydric extent by mukey, water-table/flooding columns (C), hydrology map | FEMA, NWI, stream order (D) |
| V. Access | **Ship** | Mapped roads, nearest road, frontage length, "farm tracks not in public data" | — |
| VI. Trees | **Ship reduced** | Canopy acres and %, height distribution (HAG) or cover distribution (TCC), source named | Forest type (D/E) |
| VII. Buildings & utilities | **Defer** | — | Structures (D/E), transmission (weak), broadband (gated). A v1 page that says only "not assessed from public data" is not worth a page; put the sentence in the overview's vintage table instead. |
| VIII. Fencing & animals | **Ship reduced** | Perimeter length, stream-exclusion fencing length; a "what this section will carry" note is not needed — just the two figures | All animal content (D/E/F); deer density per state (E, PA-only) |
| IX. Soils & geology | **Ship** | Map-unit table with the C columns (capability, bedrock depth, `musym`), companion soil map | Bedrock geology (D/E), forage yields (sparse; include only if the SDA join proves populated for the reference parcel) |
| Layout map | **Ship** | Existing renderer, unchanged | — |
| Design record | **Ship** | Per-step tables from the sources in §1's design-record table | — |

Sections II, III, V, IX and the design record are complete with no new host.
Section I needs one new call if county/state is wanted. IV and VI ship honest
subsets.

---

## 4. The hard-fail question

**The principle as implemented.** `fetch_parcel_data()` raises on any failure
among eleven layers and converts a `None` from imagery or canopy into
`ParcelDataIncompleteError`; `session_manager.create_session()` runs it before
the document is persisted, so a Layer 1 failure creates no session and the
frontend renders `failed_layer {type, label, reason}`. The stated rationale
(`parcel_data.py`): design steps depend on that data, and a design computed on
incomplete data must not exist. Irradiance is the one named carve-out ("optional
regional context worth a single narrative sentence"); canopy has a fallback,
not an exemption. `FetchCache` memoises the whole `ParcelData` by boundary.

**What is different about report-only data.** No design step consumes county
name, FEMA zone, NWI, stream order, building footprints, or occurrence records
(`step_registry` `consumes` edges name none of them). A report is also a
terminal, paid, one-off action taken from a finished session — it is generated
minutes to days after the session was created, on a job thread, with its own
failure payload (`session_report.error_payload`).

**Options, with what each does to the principle and which sources fall under it.**

**1. Every new source joins Layer 1 and hard-fails.**
Add each to `fetch_parcel_data()` and `FETCH_LAYERS`; `run_diagnostics`
self-check will demand a timer per layer.
- *Principle:* preserved verbatim, but its justification no longer covers the
  new layers — a FEMA outage would stop someone drawing a boundary for a
  design that never reads FEMA. `parcel_data.py`'s docstring argues at length
  that the two carve-outs are the only two; this option either adds seven more
  or forces every report source to be mandatory.
- *Cost:* every session creation pays every report fetch, sequentially, whether
  or not a report is ever bought; the fetch cache holds it all.
- *Would fall here:* nothing comfortably. Census county lookup is the only
  candidate cheap and reliable enough, and even it gates nothing.

**2. Report-only sources degrade, with an explicit "unavailable" statement in
the affected section.**
Fetch at report time (or at creation, in a try/except); a failed source produces
a section that says which source did not answer, when, and what the section
therefore cannot say.
- *Principle:* Layer 1 untouched; a **third** carve-out class is created beside
  irradiance ("report-only, degradable"), and the docstring's "exactly two
  departures" sentence has to be rewritten rather than quietly contradicted.
- *Cost:* a paid PDF can ship with holes. The honest statement makes the hole
  visible, which is the brief's own requirement for footers, but a buyer who
  paid for FEMA and got "unavailable today" has a legitimate complaint. Whether
  to sell that PDF or hold it is a product decision (§9 D1).
- *Would fall here:* FEMA, NWI, stream order, GBIF/eBird, ECOS, LANDFIRE/NLCD,
  buildings, NAIP vintage, Census county.

**3. A separate report-data layer, fetched only at report time, with its own
failure policy.**
A `ReportData` dataclass with its own `fetch_report_data(boundary)`, its own
`FetchCache` instance (the class is already parameterised by a fetch function),
called inside `run_report_job` before the render. Its policy can be either
all-or-nothing (the job fails cleanly; the design is unharmed; the user retries
later — the shape `session_report.py` already documents for a report failure)
or per-source degrade (option 2 inside the layer).
- *Principle:* Layer 1's contract is untouched and its docstring stays true.
  A new principle is written for a new layer instead of amending an old one:
  "report data is fetched when a report is asked for; a report is not generated
  on incomplete report data" (mirrors Layer 1) or "…and says so" (mirrors
  option 2). The session cache rule ("nothing here is authoritative; a miss
  degrades to slower") holds because report data is re-fetchable from the
  boundary.
- *Cost:* a second fetch layer to maintain (timers, diagnostics, retries); the
  report wait grows by the sequential fetch time; a source outage fails the
  report rather than the session.
- *Would fall here:* every class D source; class C additions to SSURGO and
  Open-Meteo could go either way (see below).

**4. Other shapes the findings suggest.**
- *4a. Class C additions stay in Layer 1; class D goes to the report layer.*
  The SSURGO columns and the 30-year climate window are changes to fetches that
  already hard-fail, so they inherit hard-fail for free and cost nothing new at
  creation (same request, more columns). The climate change triples one
  payload; the SDA changes add columns to existing joins and `sacatalog` and
  `muaggatt` add one query each.
- *4b. Prefetch at creation, do not gate.* Report-layer fetch is started in the
  background after the session is created, cached by boundary, never awaited
  by the creation path; the report job uses the cache or fetches. Hides latency;
  adds a thread and a second place the same fetch can be in flight (the
  `FetchCache` per-key lock already handles that).
- *4c. Tiered inside the report layer.* "Required" report sources (Census
  county, SSURGO extras) hard-fail the report; "optional" (FEMA, NWI, animals)
  degrade with the visible statement. This is option 3 with option 2's
  per-source policy declared per source, in one table, the way `FETCH_LAYERS`
  declares Layer 1's.
- *4d. Reduce D by moving to E.* Bedrock geology, listed species by county, and
  building footprints can be bundled; a bundled dataset cannot be down. The
  cost is deploy size on a single small container (§2.3), and the footer must
  carry the bundle's vintage.
- *4e. Sell after render, not before.* Generate the PDF, then show the user
  which sections are complete before payment. Outside this investigation's
  scope but it changes how much option 2's holes matter.

No recommendation is made here; §9 D1 records the trade-off the decision turns
on.

---

## 5. Report pipeline findings

### 5.1 Where the report is generated, and every module involved

Session path (the product path):

1. **Frontend.** `ReportAction.jsx` (button below the rail; offered only when
   `selectDesignIsComplete`) → `SessionStore.generateReport({propertyLabel})` (called with **no** label today) →
   `session/report.js runReport()` → `apiClient.generateReport()` `POST
   /api/sessions/<id>/report` with `{property_label}` → `jobs.js pollJob()`
   `GET /api/jobs/<id>` → download link `GET /api/reports/<id>`
   (`reportDownloadUrl`). Failure copy lives in `ReportAction.jsx`
   (EXPIRED / UNAVAILABLE); wait phrases in `WaitingLine.jsx` (`REPORTING`).
2. **`session_api.py`** `generate_report_endpoint` → `session_report.submit_report`
   (`check_ready` refuses a partially committed session with 409 and no job) →
   `job_runner` thread → `run_report_job` → **`generate_pdf_report.generate_session_report_pdf`**.
3. **`session_design.build_session_design`** — reads the document and the
   tier-2 cache; rehydrates committed features through each step's inbound
   translator (`wire_translation`); reduces each step's `narrative_data` block
   to the committed members; raises `SessionWorkingDataExpiredError` if any
   committed step's generate result is evicted.
4. **`generate_full_report.report_from_design`** → **`report_generator.generate_scale_of_permanence_report`**:
   builds the data blocks (`build_data_summary`, twelve `_format_*` functions,
   `_committed_section` wrapper) and makes **the one LLM call** —
   `Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(model="claude-sonnet-5",
   max_tokens=20000, system=SYSTEM_PROMPT, …)`, non-streaming — and returns
   markdown.
5. **`render_layout_map.render_layout_map(design.boundary, png, layers=session_design.layout_layers(design))`**
   — matplotlib at 150 DPI, contextily NAIP tiles (the only network on this
   path besides the LLM), plus `contour_lines`, `display_outline`,
   `fence_display_geometry`, `wire_translation` for road features.
6. **`generate_pdf_report._write_pdf`** — `markdown` (extensions `extra`,
   `sane_lists`) → `_build_html_document` (cover div, narrative div, map page
   div; `REPORT_CSS` f-string) → `weasyprint.HTML(string=…, base_url=<pdf
   dir>).write_pdf(path)`.
7. **`session_report.ReportStore`** — in-memory registry, temp directory,
   capped at 64, evicts files; `download_url`; `DOWNLOAD_FILENAME =
   "scale-of-permanence-report.pdf"`.

Batch path (not the product path, still deployed): `api.py`
`/api/generate-report` and `/api/generate-report-pdf` →
`generate_full_report.generate_full_report` / `generate_pdf_report.generate_full_report_pdf`
→ `pipeline_context.build_pipeline_context` → same LLM call →
`render_layout_map.fetch_layout_layers` (three more KSOP calls) → same
`_write_pdf`. `main.py` is a soil-only CLI slice that does not reach the report.

Deployment: `Dockerfile` (python:3.11-slim-bookworm, pango/cairo/gdk-pixbuf,
`fonts-liberation`, `gunicorn api:app --workers 1 --timeout 600`).
`requirements.txt` carries `anthropic`, `markdown`, `weasyprint`, `contextily`,
`matplotlib`.

### 5.2 The LLM call and everything that depends on its output

The call is made in exactly one place: `report_generator.generate_scale_of_permanence_report`
(lines 1853–1891). Its output is a markdown string. Consumers of that string:

- `generate_pdf_report._narrative_html` / `_build_html_document` — the only
  runtime consumer. Removing the narration means replacing the `narrative`
  div's content, not just deleting the call.
- Tests that mock it and assert it was (or was not) called with certain
  arguments: `test_generate_full_report.py` (sections 2–6 patch
  `generate_full_report.generate_scale_of_permanence_report`),
  `test_session_report_route.py` (patches it and feeds real markdown to the
  PDF assembly), `test_session_report.py` (calls `build_data_summary` and
  asserts on the prompt text — this is where "the report narrates the committed
  design" is proven).
- Copy that names it: `session_report.GENERATION_FAILED` ("the narrative
  service or the map imagery"), `WaitingLine.jsx` comments, `README.md`'s
  opening paragraph, the `api.py` endpoint docstrings.

**What depends on the data blocks, not the prose.** `build_data_summary` and
the `_format_*` functions are consumed by `test_report_generator.py` (1,418
lines, formatter-level tests), `test_dem_elevation_summary.py`,
`test_display_scale.py`, `test_twi_boundary_independence.py` (each asserts on a
formatter's sentence), `diagnose_elevation_source_and_fetch_cost.py`. Deleting
the formatters breaks these; leaving them in place while nothing calls them is
dead code the tests keep alive. This is a decision (§9 D4), not a default.

**Import edge that must not be missed.** `session_design.py` imports
`COMMITTED_DESIGN_KEY` from `report_generator` (line 233, "report_generator.py is
the only reader"). Retiring `report_generator.py` without moving that constant
breaks `session_design`, and with it the session report and every test that
builds a design.

**`narrative_data` is not removable.** The brief's "removes the need to capture
`narrative_data` at every commit" needs qualifying: each KSOP module's
`build_narrative_data()` is the source of the interactive **wire payloads**
(`step_orchestrator.build_water_payload` etc. read `result["narrative_data"]`
for `zones`, `summary`, `scales`, water's `panel` rows), of `run_diagnostics`
records, and of `production_zone_payload`'s summary. What the new report can
drop is the *report-time read of those blocks from the evictable tier-2 cache*
— the five `_*_block` functions in `session_design.py` and
`SessionWorkingDataExpiredError` — **if** the design record is built from the
committed features in the Design Document instead. §1's design-record table
shows that is possible for water, roads (per branch), trees, structures and
fencing, and only partly for landform (slope stats, soil list, drainage,
position are not on the committed feature). See §9 D3.

**Other things removing the narration touches.** `ANTHROPIC_API_KEY` handling
(`api.py` lines 192/292 special-case the missing-key error); the `anthropic`
requirement; `MODEL`/`SYSTEM_PROMPT`; the `_committed_section` mechanism (its
"the owner declined all N" sentence is prose the new report may still want as a
templated line — the `commitment` sub-block that feeds it is small and
document-derivable: `candidate_count` is the only value that needs the run).
Nothing in `pipeline_context.py`, `step_orchestrator.py`, `wire_translation.py`
or the frontend reads the narrative string.

### 5.3 The WeasyPrint template and stylesheet as they stand

`generate_pdf_report.py` — one f-string, `REPORT_CSS` (lines 53–119):

- `@page { size: Letter; margin: 0.9in 0.85in 0.8in 0.85in; @bottom-center
  { content: counter(page); Arial 9pt #777 } }` and a named page
  `@page layoutMapPage { margin: 0 }` for the full-bleed map (`.map-page img
  { width: 8.5in; height: 11in }`).
- Body Georgia/'Times New Roman' 11pt, line-height 1.55, `#1a1a1a`;
  headings Arial in `#14401d`; `p { text-align: justify }`; a `.cover` block
  (title, subtitle from `property_label`, tagline); `.narrative { break-before:
  page }`.
- No `@font-face`, no CSS variables, no tables, no figure/caption styles, no
  running headers, no section numbering. Georgia and Arial are not installed
  in the container (`fonts-liberation` only), so the PDF today renders in
  Liberation Serif/Sans by fontconfig substitution.
- HTML is built by string formatting with no escaping of `property_label`
  (a `<` in an address would break the document). A templating step (Jinja2 is
  not a dependency today) or `html.escape` belongs in the foundation.
- The map PNG is referenced by absolute `file://` URI; `base_url` is the
  output directory.

Nothing in the stylesheet is reusable for the new report except the named-page
mechanism for the map and the page-number footer pattern. The six reusable
components (eyebrow, heading, summary line with mono values, key-figure grid,
data table, source footer) do not exist in any form.

### 5.4 The frontend's self-hosted fonts and WeasyPrint

Frontend `src/fonts/`: `bitter-latin-wght-normal.woff2` (variable, wght
100–900), `source-serif-4-latin-wght-normal.woff2` (variable, 200–900),
`ibm-plex-mono-latin-400-normal.woff2`, `ibm-plex-mono-latin-500-normal.woff2`,
three OFL 1.1 licence files; latin subset (unicode-range covers °, ·, –, −, ×
via U+0000–00FF and U+2000–206F). Declared in `src/index.css` with `@font-face`;
tokens `--font-display: 'Bitter'`, `--font-prose: 'Source Serif 4'`,
`--font-data: 'IBM Plex Mono'`. The fonts README already says: "The PDF report
(`generate_pdf_report.py`, WeasyPrint) will need the same files, so keep this
directory flat and the licenses beside the fonts."

Are they usable by WeasyPrint, and by what path:

- WeasyPrint supports `@font-face` with `src: url(...)`; it fetches the file
  through its URL fetcher (file URLs resolve against `base_url`), parses it with
  **fontTools**, and installs a temporary fontconfig configuration for Pango.
  **WOFF2 requires fontTools' brotli extra** (`fonttools[woff]`), which
  WeasyPrint lists as a dependency; whether the wheel set that lands in the
  `python:3.11-slim` image includes `brotli` must be checked in the container.
  VERIFY. (Neither `weasyprint` nor `fonttools` is installed in this sandbox,
  so this could not be exercised.)
- **Variable fonts:** Pango ≥ 1.44 supports variation axes; WeasyPrint maps
  `font-weight` to a variable font's `wght` axis in recent versions, but the
  behaviour has changed across releases and static-instance fallback is the
  safe answer if 400 and 600 render identically in a test. VERIFY by rendering
  a probe page at 400/600 and inspecting. The frontend uses only 400 and 600
  (fonts README), so two static instances per family would be an acceptable
  substitute.
- **Path:** the frontend and backend are separate repositories and separate
  deploys (Vercel; Render/Railway Docker), so the backend cannot read
  `../keyline-designer-frontend/src/fonts` at runtime. The files (four woff2 +
  three OFL texts) need to be vendored into the backend — `assets/fonts/`
  beside `assets/icons/` fits the existing layout; `.dockerignore` excludes
  `*.png`/`*.pdf` but not `*.woff2`, so `COPY . .` picks them up. The
  stylesheet references them by absolute `file://` URI built from
  `os.path.abspath(__file__)` (the way the map PNG is referenced) or by setting
  `base_url` to the assets directory instead of the PDF's output directory.
- Two copies of one file across two repos is the cost; the README's request
  to keep the directory flat is what makes a checked-in copy a copy rather
  than a rewrite. A `scripts/` sync or a shared package is more machinery than
  four files justify today.

### 5.5 Job, timing and failure shapes the new report inherits

- `job_runner` `ThreadPoolExecutor`, in-memory, single worker process; gunicorn
  timeout 600 s does not bound a job thread.
- `error_payload`: `session_expired` (actionable) vs `report_failed`
  (`actionable: false`). A report-layer fetch failure under option 2/3 needs
  to decide which of these it is, or add a third shape that names the source
  (the `failed_layer {type, label, reason}` shape session creation already
  uses is the obvious candidate).
- `ReportStore` is ephemeral (documented); nothing changes.

---

## 6. The acreage discrepancy — current status

**What the brief's line numbers point at.** At `f010088`,
`report_generator.py:797` is the embankment failed-seed line in the water
formatter and `:865` is a comment in the same function; neither quotes acreage.
The lines that do are `report_generator.py:1506–1509` (the parcel totals
sentence: `parcel['selected_acres']`, `selected_pct_of_parcel`) and
`:1576–1577` (per patch: `patch['area_acres']`, `percent_of_parcel`), inside
`_format_production_areas_summary`. The line numbers in the brief probably
predate this branch.

**The two geometries.** Every production patch carries both
(`production_area.cluster_and_gate`, lines 1533–1596):

- `polygon_utm` / `area_acres` — the cell-union **footprint** clipped to the
  parcel: every 5 m cell that cleared every gate, staircase and one-cell
  fingers intact.
- `render_fill_polygon_utm` / `render_fill_area_acres` — a bounded
  morphological **opening** (erode by `RENDER_OPENING_RADIUS_METERS` +
  `RENDER_LEAD_ERODE_CELLS`, dilate back, clip); anti-extensive, asserted so;
  can be empty for a thin cluster.

**Who quotes which today.**

| Reader | Geometry | Acreage |
|---|---|---|
| Layout map (`render_layout_map.py` 1909–1947) | opening (smoothed for the contour clip) | none on the map (legend carries no data) |
| Interactive wire (`production_zone_payload.assemble_production_zone_payload`) | opening, as the feature geometry | `render_fill_area_acres` captioned **as `area_acres`** on features and rows; `summary.selected_acres` is the sum of those |
| Frontend panel | — | the wire's figure (opened) |
| Committed feature in the Design Document | opening (the wire geometry comes home unchanged) | opened |
| Rehydration (`wire_translation.rehydrate_production_zone`, 2110–2116) | `polygon_utm` = the committed (opened) geometry; a **second** opening is computed from it | `area_acres` = opened area; `render_fill_area_acres` = area of the opening-of-the-opening |
| Ceiling trim (`production_area_ceiling`) | footprint | footprint |
| Report narrative block (`production_area_ceiling.build_narrative_data`, 970–971) | — | footprint `area_acres`, `percent_of_parcel` |
| Report (`report_generator` 1576) per patch | — | **footprint** |
| Report parcel totals on the session path (`session_design._landform_block`, 393–398) | — | `selected_acres` = sum of committed patches' `area_acres` = **opened**; `selected_pct_of_parcel` from that |

So the current session-path report's PRODUCTION AREAS block mixes the two:
per-patch lines quote footprint acreage from the run's narrative block, while
the "acres selected as production candidates" total is recomputed from the
rehydrated (opened) committed features. `test_session_report.py` section 1
asserts the per-patch figure is the run's narrative (footprint) figure — "NOT
the Feature's properties.area_acres, which is the render-fill figure" — and
does not test the total against either.

**Status: known, deliberate, unresolved.** `production_zone_payload.py`'s
docstring records the decision: "The ceiling trim … still measures itself
against FOOTPRINT acreage, and narrative_data still reports footprint acreage
to the report. Both are left alone on purpose: the trim is an algorithm whose
behaviour would change if its input did, and the narrative is the PDF's
contract, not this one. The consequence is real and is reported rather than
hidden." The interactive product moved to the opening; the report did not.

**The 22 % figure.** It is not recorded anywhere in either repository.
`test_production_zone_payload.py` prints `footprint X -> drawn Y (Z% of
footprint)` on a synthetic DEM, and the frontend fixture
`src/fixtures/landform-session.json` holds only the opened figures (0.42, 3.06,
0.70 ac; selected 4.2 of 13.2). A live run against the reference parcel is the
only place the 22 % could have come from and it cannot be reproduced here.

**Why it must be settled before the design record.** The design record will
quote zone acreage beside the map. If it quotes the footprint, it disagrees
with the panel the user committed from and with the shape on the map; if it
quotes the opened figure, it disagrees with the ceiling's own accounting
(`ceiling.acres_trimmed`, `parcel.eligible_acres`) and with the current report's
tests. Either is defensible; mixing them, as today, is not. Recommendation is
in §9 D2. Water (`zone_acres` is the envelope, one object), trees (footprint
verbatim) and structures (pad) carry no such split.

---

## 7. Honest limits per section (source footers)

| Section | Footer content |
|---|---|
| I. Site overview | Boundary is user-drawn, not a legal parcel; acreage is the drawn polygon's. Vintages listed are the services' where published; "not published" where not. |
| II. Climate | Reanalysis (Open-Meteo/ERA5-family), ~9–25 km grid, N-year means, period stated. Regional climate, not a station at the parcel: low ground, valley floors and frost pockets frost later in spring and earlier in fall than these dates; hardiness zone is an estimate from this data, not the USDA map. |
| III. Landform | USGS 3DEP resampled to 5 m; source resolution (1 m lidar or 10 m) not known from the fetch. Slope by Horn's method over 5 m cells; keypoints and valleys are DEM-derived and not surveyed (`KEYPOINT_CONFIDENCE_NOTES`, `VALLEY_CONFIDENCE_NOTES`). Contributing areas are truncated at the 100 m analysis window. |
| IV. Water | NHD compiled at ~1:24,000 (1:100,000 in places); small, seasonal or recently changed features may be missing or mislocated (`NHD_CONFIDENCE_NOTES`); "nearest stream" is within a 150 m search. Hydric extent is SSURGO map-unit level. FEMA: flood zones are regulatory panels and many rural counties are unmapped; NWI: photo-interpreted, vintage varies by state; neither is a wetland delineation. |
| V. Access | USGS transportation data are public road/right-of-way sources; private tracks and driveways will not appear; absence means "no mapped public road", not "no access" (`FARM_ROAD_CONFIDENCE_NOTES`). Frontage is measured against a centerline with a stated tolerance. |
| VI. Trees | HAG: 3DEP lidar first-return height above ground, project-year stated, thresholded at 4.5 m; TCC: NLCD 30 m percent cover, any nonzero counted as canopy, misses thin strips and single trees, blocky at parcel scale. Sentinel-2 NDVI cannot separate a vigorous hayfield from canopy. Forest type (if shipped): 30–250 m classification, one to a few pixels on this parcel. |
| VII. Buildings & utilities | Footprints from national imagery-derived datasets of stated year; recent construction absent. Transmission data cover ≥69 kV lines only; the distribution line a farm connects to is not public data. Broadband: FCC availability is provider-reported at serviceable locations. |
| VIII. Fencing & animals | Perimeter is the drawn boundary's. Occurrence records are presence-only and biased toward observer effort and roads; absence of records is not absence of species; coordinates carry stated uncertainty and sensitive taxa are obscured. Deer density, where shown, is the state agency's management-unit estimate, not a parcel figure; none is shown where no state source is bundled. Listed-species screening is informational and is not a determination or a substitute for agency consultation. |
| IX. Soils & geology | SSURGO is survey-scale (~1:24,000) and generalised; component percentages describe a map unit's typical composition, not any point (`SSURGO_CONFIDENCE_NOTES`); Ksat and K-factor are the shallowest mineral horizon of the dominant component; capability class is non-irrigated. Survey area and version date stated. It does not replace test holes or a soil test. Bedrock geology (if shipped) is a 1:100k–1:500k compilation. |
| Design record | Every figure is the pipeline's measurement of a committed shape; opened versus footprint acreage stated (§6); "candidate of N" counts come from the generate that produced the commit. |

---

## 8. Build sequence

Expected shape, dependencies first.

1. **Decide §9 D1–D4** (fail policy, acreage, design-record source, fate of
   the LLM path). Steps 2–4 do not depend on D1; step 6 does.
2. **Report foundation.** Vendor the four woff2 + OFL files into the backend;
   prove `@font-face` + woff2 + variable weights in the container (VERIFY
   items 1–3); a stylesheet with tokens (three faces, ink/paper/rule colours
   mirroring `index.css`, Letter page geometry, running footer with page
   number, named map page kept); a templating step with escaping; the six
   components — eyebrow, heading, summary line (templated sentence with mono
   values), key-figure grid, data table (mono tabular figures, hairline rules,
   serif row labels), source footer. Build these on **Climate alone**: extend
   `climate_data` to 30 years with daily arrays retained (class C, Layer 1),
   derive frost dates, GDD, monthly table, radiation, est. zone; render one
   section to PDF; keep the existing map page after it. This is the first
   milestone that produces a printable page.
3. **Site overview (I)** on the same foundation: vintage table, centroid,
   acreage, elevation range; county/state only if D7 says yes.
4. **Landform (III)**, then **Water (IV, reduced)**, **Access (V)**, **Trees
   (VI, reduced)**, **Soils (IX)** — KSOP order, all classes A–C. IX adds the
   SDA columns (`musym`, capability, bedrock depth, water table, flooding,
   `sacatalog`) and the companion soil map renderer; III/IV add the landform
   and hydrology map renderers (share one small "inventory map" renderer
   parameterised by layers rather than three copies).
5. **Fencing (VIII, reduced)** — two figures.
6. **Design record** from the source D3 chose, in KSOP order, quoting the
   acreage D2 chose; a templated "N of M candidates committed / declined all"
   line replaces `_committed_section`'s prose.
7. **Remove narration**: delete the LLM call from the session path, move
   `COMMITTED_DESIGN_KEY` (or retire it), rewrite `GENERATION_FAILED`, update
   the tests that mock the call, update `README`, drop `anthropic` and
   `markdown` if D4 retires the batch path; keep or delete the formatters per
   D4. Rename `DOWNLOAD_FILENAME`.
8. **Class D sources last**, under the D1 policy, as a `ReportData` layer or as
   Layer 1 additions: Census county (I), FEMA + NWI (IV), forest type (VI),
   then the animals content (VIII), then buildings (VII) if it is ever worth a
   page. Each with its own timer, retry pattern (`fetch_attempts`), diagnostic
   row, and footer vintage.
9. **Class E bundles** (per-state deer table, listed species by county,
   bedrock extract) when a section they serve is scheduled; each with a
   refresh note in the repo.

---

## 9. Open decisions

**D1. Failure policy for report-only sources.** Options in §4.
*Recommendation:* option 3 with 4a and 4c — class C stays in Layer 1 (it
already hard-fails and costs nothing new); class D lives in a report-time
layer with its own `FetchCache`, declared per source as required-or-degradable
in one table; a degradable source that fails renders the visible "unavailable"
statement; a required one fails the job with a `failed_layer`-shaped payload.
This keeps `parcel_data.py`'s contract and docstring true, keeps session
creation as fast as it is, and puts the paid-PDF-with-holes decision in a table
rather than in each fetch.

**D2. Which production acreage the report quotes.**
*Recommendation:* the opened figure — it is what the user committed from, what
the committed feature carries, and what the map draws; state "drawn (opened)
acreage; the eligible-cell footprint is larger" in the design record's footer,
and have the ceiling summary line quote its own footprint numbers under their
own label. Change `test_session_report.py` section 1 accordingly. Do not change
the ceiling algorithm.

**D3. Source of the design record.**
Options: (a) tier-2 `narrative_data` via `session_design` as today (keeps
`SessionWorkingDataExpiredError`); (b) committed features in the document plus
class B recomputation for landform's missing rows; (c) persist the panel rows
into the document at commit.
*Recommendation:* (b) for water, roads, trees, structures, fencing now; landform
via (a) until its missing rows (slope stats, soil list, drainage, position) are
either put on the wire feature or recomputed from geometry + DEM + soil at
report time (the rehydrator already recomputes cells; `_patch_soil_fields` and
the slope stats are cell-level reads). (c) contradicts `design_document.py`'s
"decisions, never derived bulk data" rule and should be argued explicitly if
chosen.

**D4. Fate of the LLM path and the formatters.** The batch endpoints in
`api.py` and `generate_full_report.generate_full_report` still narrate, and
`test_report_generator.py` tests formatters nothing else will call.
*Recommendation:* retire the batch narration in the same change (the batch
path's PDF can call the new renderer over a `PipelineContext`-shaped design, or
be removed if the frontend no longer calls it — the frontend calls only the
session routes and `/api/geocode`; the legacy endpoints appear in its comments
only); delete the formatters and their
tests rather than keep them green for nothing; keep `build_data_summary`'s
*inputs* (the narrative blocks) untouched.

**D5. Open-Meteo commercial terms.** The report makes the climate data a
deliverable of a paid product on an endpoint documented as non-commercial.
*Recommendation:* verify the current terms; if they read as remembered, budget
the commercial API key before v1 ships and switch the endpoint host (the
request shape is the same).

**D6. Fonts: variable or static instances, and where they live.**
*Recommendation:* vendor the four frontend files into `assets/fonts/` with the
OFL texts; if the container renders variable weights correctly, use them; if
not, add static 400/600 instances for Bitter and Source Serif 4 and keep Plex
Mono's two statics.

**D7. County/state in v1.** One new host (Census reverse geocode) for one line.
*Recommendation:* yes, degradable — the forward geocoder is already a
production dependency and the line is what a reader looks for first.

**D8. Buildings & utilities.** Nothing in it is A–C.
*Recommendation:* defer the whole section; do not ship a page of "not
assessed".

**D9. Animals in v2: GBIF only, or GBIF + iNaturalist + eBird direct.**
*Recommendation:* GBIF only, with the licence filter (CC0/CC BY), a stated
buffer, and record counts by group rather than species lists; add the direct
APIs only if their terms are confirmed compatible with a paid product.

**D10. Hardiness zone: estimate or official.**
*Recommendation:* estimate from the same 30-year data and label it "est." (the
brief's own wording); add the official lookup later if a public service is
confirmed.

**D11. Climate window and the interactive product.** Extending
`get_climate_summary_for_point` to 30 years changes `prevailing_wind_direction`
and the other summary figures the current report quotes; no KSOP step consumes
`climate_summary` (`step_registry` declares no such edge), so nothing on the
map changes.
*Recommendation:* extend to 30 and retain the daily arrays on the dict (an
additive key), keep the eight summary keys.

**D12. Templating.** f-string HTML today, no escaping.
*Recommendation:* Jinja2 (one small dependency) with autoescape; one template
per component.

**D13. Where the class D fetches are timed and recorded.** `run_diagnostics`
cross-checks `FETCH_LAYERS` against the compiled `fetch_parcel_data`.
*Recommendation:* give the report layer its own `REPORT_FETCH_LAYERS` and the
same self-check, so a thirteenth untimed layer is reported there too.

---

## 10. VERIFY-BEFORE-BUILD

Nothing below was executed; each is a claim to test from a machine with
network access before it is built on.

Fonts and rendering
1. WeasyPrint in the `python:3.11-slim-bookworm` image loads `@font-face`
   WOFF2 files — i.e. `fonttools` is present with the `brotli` extra.
2. WeasyPrint + Pango in that image render a variable font at `font-weight:
   400` and `600` as two visibly different weights (else use static instances).
3. The latin-subset files cover every glyph the report uses (°, ·, –, −, ×, ½
   if used; `U+00BD` is in range, `U+2153` is not).
4. `base_url`/`file://` resolution of `assets/fonts/*.woff2` from inside the
   container's `/app`.

Open-Meteo (class C)
5. Current terms of use: free tier non-commercial; commercial endpoint and
   pricing; rate limits.
6. The archive API returns 30 complete years of daily `temperature_2m_min`,
   `temperature_2m_max`, `precipitation_sum`, `shortwave_radiation_sum` in one
   call within the current 60 s timeout, and the payload size.
7. Which model/grid the API reports for the reference parcel (for the footer).

SDA (class C)
8. Column names and tables: `mapunit.musym`, `component.nirrcapcl`,
   `component.nirrcapscl`, `muaggatt.brockdepmin`, `muaggatt.wtdepannmin`,
   `muaggatt.flodfreqdcd`, `muaggatt.pondfreqprs`, `corestrictions.reskind`
   / `resdept_r`, `chorizon.awc_r`, `cocropyld.cropname` / `nonirryield_r` /
   `yldunits`, `sacatalog.areasymbol` / `saverest` — each against a published
   SDA example query, per the codebase's own rule.
9. `cocropyld` is populated for the reference parcel's components (else drop
   forage yields from v1).

USGS / NHD
10. `hydro.nationalmap.gov/arcgis/rest/services/nhd` retirement date and the
    3DHP replacement service; whether NHDPlus HR exposes `StreamOrde` on its
    flowline layer and covers the reference parcel.
11. Whether 3DEP's index services can state the source resolution under the
    fetched 5 m grid.

New live sources (class D)
12. Census reverse geocoder: endpoint path, parameters, county/state fields.
13. FEMA NFHL: service URL, flood-hazard layer id, field names (`FLD_ZONE`,
    `SFHA_TF`), polygon `query` support, and behaviour for an unmapped county.
14. USFWS NWI: service URL, layer id, `ATTRIBUTE`/`WETLAND_TYPE` fields.
15. LANDFIRE EVT / NLCD Land Cover: ImageServer URL, `exportImage` on the
    DEM window (the TCC pattern), class code tables.
16. FEMA USA Structures: service URL, fields, query limits, licence text.
17. HIFLD/EIA transmission lines: current host after the HIFLD Open changes;
    whether any distribution-line data are public (expected: no).
18. FCC BDC: whether a lat/lon availability query exists without a Fabric
    location id and an account; terms for commercial reuse.
19. GBIF: polygon search parameters, `license` filter values, per-dataset
    licence distribution for records near the reference parcel, rate policy.
20. iNaturalist API and eBird API terms of use for a paid product; eBird key
    limits and the 30-day window.
21. USFWS ECOS critical-habitat service URL and the species-by-county source;
    confirmation that IPaC has no supported third-party API.
22. USGS SGMC service (`mrdata.usgs.gov/services/sgmc2`) WFS/WMS query by
    point/polygon, or the size of a per-state extract.
23. Planetary Computer `naip` collection item dates near the reference parcel,
    and whether the USGSImageryOnly tile service publishes a vintage.

Static references (class E)
24. USGS GAP species range data licence and format; USDA PHZM 2023 GIS
    availability and licence; PA Game Commission WMU report format and whether
    it publishes density or only harvest.
25. Microsoft/Overture building footprint licence (ODbL vs CDLA) and per-state
    file sizes against the container's disk.

Acreage
26. Run `identify_optimized_production_areas` on the reference parcel with
    network access and record footprint vs opened acreage per patch and in
    total — the 22 % figure is not in the repository.
