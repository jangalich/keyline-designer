# Keyline Designer — Backend

Backend services for the regenerative farm design tool. Fetches public
climate and geospatial data (climate, soil, elevation, hydrology) for a
given property boundary and generates a narrative Scale of Permanence
report using the Claude API.

## What's built and working

- `feature_schema.py` — the shared GeoJSON FeatureCollection data contract
  every vector-data layer wraps its output in: `make_feature()`,
  `make_feature_collection()`, and `validate_feature_collection()`. Every
  feature requires a `properties.confidence` and a non-empty, plain-language
  `properties.confidence_notes` — no layer gets to skip stating its own data
  quality caveats. `hydrology_data.py` and `soil_data.py` are converted to
  it today (see `get_water_features_geojson()` and `get_soil_data_as_geojson()`
  respectively); other layers convert to it in later passes. See
  `test_feature_schema.py` for an offline (no-network) validation example.
- `climate_data.py` — fetches historical wind, rainfall, and temperature
  data from Open-Meteo (free, no API key). Prevailing wind and rainfall
  intensity feed directly into the report's design reasoning; temperature
  is included as reference context.
- `soil_data.py` — fetches SSURGO soil survey data from USDA's Soil Data
  Access API, for either a single point or a full parcel boundary.
  `get_soil_data_as_geojson()` additionally fetches each map unit's actual
  polygon boundary and returns it as a schema-conformant FeatureCollection.
- `elevation_data.py` — fetches elevation data from USGS 3DEP for a point
  or as a grid sampled across a boundary (used to gauge slope/relief).
- `hydrology_data.py` — fetches nearby streams and standing water from
  USGS's National Hydrography Dataset, with a buffer zone so features
  just outside the exact drawn boundary are still caught.
  `get_water_features_geojson()` returns the same fetch as a
  schema-conformant FeatureCollection.
- `dem_data.py` — fetches a real DEM (digital elevation model) raster grid
  for a property from USGS 3DEP's ImageServer (`exportImage`), reprojected
  to the property's local UTM zone so pixels are true meters. This is the
  raster counterpart of `elevation_data.py`'s point-sampled grid — what
  flow-direction/accumulation terrain analysis actually needs.
- `raster_grid.py` — tiny shared, numpy-only helpers (pixel<->coordinate
  math, 8-connected component labeling) used by every module downstream
  of the DEM fetch, so their core logic stays unit-testable against a
  synthetic DEM dict without touching rasterio or the network.
- `valley_delineation.py` — delineates primary valleys from a DEM via
  standard D8 terrain analysis (priority-flood depression fill -> flow
  direction -> flow accumulation -> threshold -> trace). Outputs a
  schema-conformant `valley` layer.
- `production_area.py` / `production_area_ceiling.py` /
  `production_suitability.py` — the consolidated production-zone
  pipeline, restructured (see Roadmap history) from an earlier three-pass
  architecture that independently rebuilt the same slope-eligibility mask
  in three places and carved hydric soil out via continuous-geometry
  `Polygon.difference()` against real, jagged SSURGO polygons — a real,
  confirmed bug (crash risk from an unbounded lettered-sub-id suffix, and
  ~65% of genuinely usable ground lost to spurious sub-minimum-area
  slivers on a real reference boundary, since differencing two complex
  polygons can fragment cleanly-contiguous ground into artifacts that
  were never a reflection of real cell eligibility). The pipeline is now
  ONE linear pass, each step computed once and reused everywhere it's
  needed:
  - **STEP 1** (`production_area.compute_step1_eligible_cells()`) — for
    every on-parcel DEM cell, two HARD, cell-level gates: slope
    (`MAX_PRODUCTION_SLOPE_PCT`, 20.0 by default) and hydric/disqualifying
    soil (`soil_data.hydric_disqualifying_mukeys()`, tested against each
    cell's own CENTER POINT against a single disqualifying-soil union
    fetched once for the whole parcel — not per-patch, since patches
    don't exist yet). A cell is either in the eligible mask or it isn't —
    no continuous-geometry differencing, so no spurious slivers are
    possible by construction. Every eligible cell is also scored on
    slope+aspect only (`per_cell_score()`, the existing zone-level
    0.55:0.15 slope:aspect ratio renormalized — `PER_CELL_SLOPE_WEIGHT`/
    `PER_CELL_ASPECT_WEIGHT`); size/compactness has no per-cell meaning,
    so it isn't computed until STEP 4.
  - **STEP 2** (`production_area_ceiling.trim_to_ceiling()`, conditional)
    — sorts STEP 1's eligible cells worst-to-best by that per-cell score
    and removes cells one at a time (worst first) until the remainder
    lands at or just above `PRODUCTION_CEILING_PCT_OF_PARCEL` (80.0) of
    the REAL, FULL parcel — not the pre-trim eligible acreage. If
    eligible acreage is already at/under the ceiling, nothing is removed
    and `production_ceiling_target_met` is honestly reported `False`
    rather than claiming 80% was hit. `production_area.identify_production_
    areas()` (the "raw" entry point every existing consumer below already
    reads) skips this step entirely and clusters STEP 1's mask directly —
    a deliberate, already-tracked wiring gap (see below), not something
    this consolidation changes.
  - **STEP 3** (`production_area.cluster_and_gate()`) — 8-connected-
    component labeling of whichever cell mask a caller passes (STEP 1's
    raw mask, or STEP 2's post-trim survivor mask), each cluster's REAL
    per-cell-square-union footprint (`_cell_union_footprint()` — NOT a
    convex hull of cell centers, which fills in concave gaps with ground
    that was never actually eligible and over-reports area), and a PURE
    area survival gate (`MIN_PRODUCTION_AREA_ACRES`) — nothing else gates
    survival here; soil was already excluded at STEP 1, so there's
    nothing left to carve. One shared implementation for both the raw and
    ceiling-optimized entry points, not two. `polygon_utm`/`geometry_wgs84`
    can legitimately come back as a `MultiPolygon` (two cells whose real
    ground squares touch only at a shared corner under 8-connectivity
    don't merge into one solid Polygon) — a convex hull is still exposed
    separately as `display_polygon_utm` (always a single Polygon, useful
    for rendering smoothness), but nothing spatial reads that field.
  - **STEP 4** (`production_suitability.score_production_areas()`) —
    PURELY ADVISORY description of survivors, not a gate: `slope_factor`/
    `aspect_factor` are averaged directly from STEP 1's ALREADY-COMPUTED
    per-cell scores (no second recomputation), `size_factor`/compactness
    is computed fresh (the one thing that can't exist before clustering),
    and `soil_carved_acres`/`soil_carved_pct` are now simple bookkeeping
    (how much of a cluster's own originally slope-eligible source region
    was excluded at STEP 1) rather than a second carving pass. Combined
    into `suitability_score`/`rank` via the existing weights
    (`SLOPE_FACTOR_WEIGHT`/`SIZE_FACTOR_WEIGHT`/`ASPECT_FACTOR_WEIGHT`,
    0.55/0.30/0.15) — soil is deliberately NOT one of these (Scale of
    Permanence sequencing: soil is step 8, the last and most improvable
    step, so it shouldn't gate/rank where production zones go the way
    slope/size/aspect, step 2, do; it already had an ABSOLUTE effect via
    STEP 1's hard exclusion). This module is now fully offline/
    network-free — it takes already-computed clusters and STEP 1's
    already-computed per-cell arrays, and does no fetching or
    recomputation of its own.

  `production_area.identify_production_areas()` (STEP 1, no trim, + STEP
  3) is the "raw" candidate list `water_candidate_zones.py`,
  `road_corridors.py`, and `solar_suitability.py` already consume via
  this exact function/signature — they get STEP 1's cell-level hydric
  exclusion automatically, with no changes needed on their end (their own
  wiring to the RAW rather than ceiling-optimized output is a separate,
  already-tracked roadmap item, unchanged by this consolidation).
  `production_area_ceiling.identify_optimized_production_areas()` (STEPs
  1-4, with the ceiling trim) is the entry point `tree_zone_candidates.py`
  and `render_layout_map.py` consume, returning the same
  `production_area_candidate` GeoJSON / `scored_patches` shape plus
  top-level summary fields (`total_selected_acreage`, `percent_of_parcel`,
  `production_ceiling_target_met`, `total_cells_removed`). Every module-
  level tunable (`MAX_PRODUCTION_SLOPE_PCT`, `MIN_PRODUCTION_AREA_ACRES`,
  `PRODUCTION_CEILING_PCT_OF_PARCEL`, `MIN_HYDRIC_COMPONENT_PCT_TO_EXCLUDE`,
  the three composite weights, the two per-cell weights) kept its exact
  existing value through this restructuring — only where each lives/how
  many times each is computed changed. Offline, synthetic-DEM-only tests
  throughout (`test_production_area.py`, `test_production_area_ceiling.py`,
  `test_production_suitability.py`), including a dedicated regression
  test constructing a jagged disqualifying-soil polygon that would
  fragment a real footprint into dozens of sub-minimum-area slivers under
  the OLD continuous-geometry differencing approach, but which the NEW
  cell-level gate (correctly) excludes zero cells from, since no cell's
  own center falls inside the jagged polygon's thin teeth.
- `water_candidate_zones.py` — the actual water-system candidate-zone
  feature: for each primary valley, finds the portion within a configurable
  service-distance window of a candidate production area, outside a
  configurable property-boundary setback, and buffers the qualifying
  segment(s) into a zone polygon (not a point). Outputs the
  schema-conformant `water_system_candidate` layer that
  `generate_full_report.py`/`report_generator.py` consume. Elevation
  relative to the production area(s) a zone could serve is NOT a
  generation-time exclusion (it used to be — a hard "must clear a minimum
  gravity gradient" gate — removed because it discarded genuinely
  well-suited ground before scoring ever weighed it: a site that needs a
  pump is a real, valid candidate, not a disqualified one); the raw
  elevation-differential/gradient data is instead attached to each zone
  (`production_area_relationships` / `primary_production_area_relationship`)
  for `water_suitability.py` to turn into a scored preference. The core
  filtering logic (`find_candidate_zones()`) is a pure function over
  already-computed valleys/production areas — deliberately separable from
  DEM fetching and valley delineation, so "is the terrain data right" and
  "is the zone logic right" can be debugged independently (see
  `test_valley_delineation.py`, `test_production_area.py`,
  `test_water_candidate_zones.py`, and the end-to-end
  `test_water_system_candidate_pipeline.py`).
- `water_suitability.py` — adds a suitability RANKING to the water-system
  candidate zones `water_candidate_zones.py` already identifies (same
  "score, don't regenerate" pattern as `production_suitability.py`):
  enriches the same `water_system_candidate` layer with a 0-100
  `suitability_score`, real differentiated `confidence`, and four
  independently-stored, positively-weighted 0-1 factors — gravity-feed
  preference (real elevation differential/gradient vs. the production
  area served, `production_area_relationships`; below-elevation/
  pump-required candidates score lower but remain valid, scored
  candidates, not excluded ones), soil water-holding capacity (SSURGO
  `chorizon.ksat_r`, saturated hydraulic conductivity — lower conductivity
  holds water better, verified against the chorizon table the same way
  `soil_data.get_erosion_factor_for_polygon()`'s `kwfact` horizon-selection
  was), NHD stream-permanence proximity (`FCode` perennial/intermittent/
  ephemeral — a real weighted factor, with a documented, known ~100-300m
  DEM-vs-NHD offset limitation surfaced in `confidence_notes`), and
  existing topographic signals (valley gradient steepness + contributing
  area, from `valley_delineation.py`). See `test_water_suitability.py`.
- `terrain_metrics.py` — DEM-derived slope, aspect (Horn's method), and a
  horizon-based shading proxy (no vegetation/canopy signal — see its
  docstring), feeding `solar_suitability.py`'s scoring. Numpy-only, no
  network, unit-tested against synthetic terrain
  (`test_terrain_metrics.py`).
- `farm_roads_data.py` — fetches real, existing road geometry near a
  property from USGS National Map's Transportation dataset (same
  ArcGIS-`query` pattern as `hydrology_data.py`, different theme).
  Public road/right-of-way data only — a private farm track or driveway
  not captured in that dataset won't appear; see its confidence_notes.
  Queries `ROAD_LAYERS` (30/31/32 — Secondary Highways, Local Connecting
  Roads, Local Roads) together and merges the results, rather than a
  single layer id. **Real bug found and fixed live**: this originally
  queried layer 0 ("Small-Scale"), a container/group layer, not a real
  Feature Layer — ArcGIS returned an error embedded in the JSON body
  while the HTTP status stayed 200, which `raise_for_status()` never
  catches, so it silently read as "zero roads found" at every buffer
  size, every time. `_query_road_layer()` now checks the response body
  itself for an `error` key even on HTTP 200 and raises explicitly, so
  this exact failure mode can't hide silently again
  (`test_farm_roads_data.py`).
- `soil_data.py` additionally has `get_farmland_classification_for_polygon()`
  and `is_prime_farmland()` — SSURGO's official Farmland Classification
  (`farmlndcl`), used to flag (never exclude) solar candidates that
  overlap prime agricultural soil (`test_farmland_classification.py`).
- `irradiance_data.py` — a regional (not per-candidate) solar production
  baseline from NREL's PVWatts API, for a rough "expect about X kWh/kW/
  year here" report note. Optional (needs a free `NREL_API_KEY`);
  degrades to `None` without one, same as the other optional layers.
- `solar_suitability.py` — the solar/structure siting data layer for
  Scale of Permanence step 6 (Permanent Buildings): a POINT-CANDIDATE
  model — candidate SITES for a small, fixed-footprint structure (a barn
  or shed with rooftop panels, not a ground-mounted array), sampled on a
  `CANDIDATE_POINT_SPACING_METERS` grid across the property, each capped
  at `MAX_STRUCTURE_FOOTPRINT_ACRES` (1 acre by default) and scored by
  DEM slope + aspect + shading (`terrain_metrics.py`, averaged over a
  local window matching the candidate's own footprint) plus a scored
  PREFERENCE (not exclusion) for proximity to a production zone's own
  edge — producing RANKED candidates (layer `solar_infrastructure`,
  properties `production_zone_relationship` = inside/adjacent/outside
  and `distance_to_production_zone_ft`), not one forced placement.
  Water-candidate (pond/dam siting) zones are still HARD-excluded
  (buffered); road proximity is still a hard constraint with a reported
  distance. Flags (never excludes) SSURGO prime-farmland overlap as a
  tradeoff note, unchanged. This REPLACED an earlier eligible-AREA/
  connected-component "zone" model (production zones hard-excluded, the
  same shape `production_area.py` itself uses) after `production_area.py`'s
  own slope ceiling was raised to match this module's — with both layers
  drawing eligibility from nearly the same gentle-ground footprint,
  production's exclusion started consuming essentially all of solar's
  own eligible area, and real-property runs returned ZERO candidates. A
  small structure isn't actually a competing zone the way production,
  water, or a future trees/windbreak layer are — it can coexist with
  production land around and under it — so modeling it as point
  candidates instead of a shared eligible-area pool fixes the collision
  at the root. Same pure-core-logic-vs-network-fetch split as
  `water_candidate_zones.py` (`find_candidate_solar_zones()` /
  `flag_prime_farmland_conflicts()` are both network-free and
  unit-tested against synthetic input — `test_solar_suitability.py`,
  end-to-end wiring in `test_solar_suitability_pipeline.py`).
- `soil_data.py` additionally has `get_erosion_factor_for_polygon()` and
  `is_erosion_prone()` (SSURGO K-factor, `kwfact`) and `is_hydric()`
  (SSURGO `hydricrating`) — the erosion-prone-soil (scored preference) and
  hydric/floodplain (hard exclusion) signals `road_corridors.py` uses
  (`test_erosion_hydric_soil.py`).
- `road_corridors.py` — computes suggested road corridor geometry from
  the DEM, replacing having Claude infer a plausible-sounding corridor in
  prose (the old Farm Roads behavior) for properties with no existing
  road/access data. Two independently-generated, jointly-ranked corridor
  types (no hardcoded type preference): CONTOUR-BAND (elevation-band
  slicing + PCA/binned-median centerline extraction) and RIDGE-TOP
  (reuses `valley_delineation.py`'s own flow-routing algorithm against an
  *inverted* DEM — a ridge in real terrain is a valley in its negation).
  HARD-excludes pond/water-system zones (`water_candidate_zones.py`,
  buffered) and floodplain/hydric ground (real NHD stream/water-body
  buffers + SSURGO hydric soil — falls back to buffered valley lines,
  flagged in confidence_notes, only if neither reachable). Production
  zones (`production_area.py`) AND erosion-prone soil (SSURGO K-factor)
  are both scored PREFERENCES, not exclusions — a road is a thin linear
  feature, not a large permanent land claim, so a candidate may cross
  either; a non-crossing candidate scores higher, all else equal
  (`PRODUCTION_AVOIDANCE_SCORE_WEIGHT` / `EROSION_AVOIDANCE_SCORE_WEIGHT`,
  same reasoning as `solar_suitability.py`'s analogous production
  preference; erosion's weight is smaller — a mitigatable engineering
  concern, not a committed alternate land use). Both were hard exclusions
  in earlier versions — with `production_area.py`'s own slope ceiling at
  20%, one large production zone can cover most of a property's gentle
  ground, and 6 of this property's 7 real soil units clear
  `soil_data.DEFAULT_EROSION_KWFACT_THRESHOLD` — hard-excluding either
  left nowhere for a corridor to exist at all (confirmed live: zero
  candidates with the production exclusion, then zero again with the
  erosion exclusion even after production was softened; real candidates
  once both were softened — see the bullet below).
  Grade-capped at a pinned, documented `MAX_ROAD_GRADE_PCT` (see the
  module for the rationale). Anchors each candidate to the property
  boundary — preferring the nearest point on real, mapped road frontage
  (`farm_roads_data.py`, reporting `anchor_road_name`/
  `anchor_road_distance_ft`) where one is reachable nearby, otherwise an
  explicitly-flagged arbitrary nearest boundary point
  (`connection_point_is_arbitrary`). Outputs the `suggested_road_corridor`
  layer. Same pure-core-logic-vs-network-fetch split as the other
  candidate-zone features (`find_candidate_road_corridors()` is
  network-free and unit-tested against synthetic terrain —
  `test_road_corridors.py`, end-to-end wiring in
  `test_road_corridors_pipeline.py`).
- `_fetch_floodplain_hydric_union()`'s NHD stream/water-body piece had a
  bug of the same root-cause CATEGORY as the original
  `soil_data.get_soil_geometries_for_polygon()` bug below: `query`
  operation returns each matching feature's FULL, un-clipped geometry for
  anything merely intersecting the query bounding box, so a long stream or
  large waterbody just touching that box could come back with geometry
  extending far past the property — buffered and unioned into the
  floodplain exclusion mask wholesale. Confirmed live: a 33.9-acre
  floodplain/hydric union on a 13.23-acre parcel, large enough alone to
  zero out every corridor candidate. Fixed by clipping each fetched NHD
  feature to a generous context region around the parcel
  (`FLOODPLAIN_FETCH_CONTEXT_BUFFER_METERS`, comfortably larger than both
  `dem_data.py`'s own DEM fetch buffer and `FLOODPLAIN_STREAM_BUFFER_METERS`
  itself) client-side in `road_corridors.py`, since NHD's ArcGIS `query`
  endpoint — unlike SDA's SQL-based query used for SSURGO — has no
  server-side clip parameter. `hydrology_data.py` itself was left
  unmodified since its other consumers (e.g. `generate_full_report.py`'s
  narrative) may legitimately want full, unclipped geometry. Separately
  checked `_fetch_erosion_prone_union()` against the same suspicion (its
  13.17-acre result on the same 13.23-acre parcel looked suspicious by the
  same size-comparison test) and confirmed it does NOT share this bug — it
  exclusively sources geometry from the already-fixed
  `get_soil_geometries_for_polygon()`, which clips server-side via SQL
  Server `STIntersection` against the parcel's own boundary, so its result
  is mathematically bounded by the parcel's own area; 13.17/13.23 acres is
  a plausible real finding (most of this parcel's soil is erosion-prone per
  SSURGO K-factor), not a bug — left unmodified.
- **A second, separate bug** in the same `_fetch_floodplain_hydric_union()`
  hydric-soil piece, found live AFTER the NHD-clipping fix above landed and
  was re-checked against the real property: a mukey was flagged
  hydric-disqualifying (and its FULL mapped polygon pulled into the
  exclusion) if ANY component within it was hydric at all, regardless of
  that component's share of the map unit's composition. Confirmed live: two
  large, mostly well/moderately-well-drained map units — hydric via a
  1%-of-composition component (Guernsey-Vandergrift) and via components
  totaling 5%+3%=8% (Ernest-Vandergrift) — had their entire polygons
  (~58x and ~40x larger than the genuinely, overwhelmingly wet Atkins
  floodplain mukey, itself hydric via an 85%-dominant component) excluded
  right alongside it, producing an 18.77-acre union on the 13.23-acre
  parcel even with the NHD fix in place. `production_suitability.py`'s
  hydric-soil-carving logic (`_fetch_disqualifying_soil_union()`) had the
  IDENTICAL bug, sourced from the same flawed rollup pattern. Fixed with
  one shared function, `soil_data.hydric_disqualifying_mukeys()`, used by
  both: sums `comppct_r` across a mukey's hydric (`is_hydric()`-true)
  components and only flags the mukey once that sum meets
  `soil_data.MIN_HYDRIC_COMPONENT_PCT_TO_EXCLUDE` (50%, mirroring
  SSURGO/NRCS's own "predominantly hydric" majority-share map-unit
  convention rather than an arbitrary invented number — see that
  constant's own comment). `is_hydric()`/`is_disqualifying_soil_condition()`'s
  PER-COMPONENT definition of hydric is unchanged — this only changes how
  per-component flags roll up to a whole-mukey decision. Regression-tested
  offline against the real mukeys/percentages from the live bug report in
  both `test_floodplain_union_scope.py` and `test_production_suitability.py`.
  The NHD stream piece separately showed some spillover across N Montour
  Rd onto the far side of Montour Run, outside the parcel, per a
  plotted-GeoJSON visual check — see the THIRD bug/fix below for that.
- **A third bug**, found live after fixing the two above: even with the
  whole-mukey hydric fix in place, erosion-prone soil was STILL a hard
  exclusion — and confirmed live, 6 of this property's 7 real soil units
  (K-factor 0.32-0.37) clear `DEFAULT_EROSION_KWFACT_THRESHOLD`, so that
  exclusion alone covered 99.5% of the parcel (13.17 of 13.23 acres) and
  was, by itself, enough to keep zeroing out every road corridor candidate.
  There's no real, citable "K-factor >= X means physically unsafe to build
  a road" threshold the way there is for a water zone — K-factor measures
  water-erosion susceptibility, a mitigatable engineering concern
  (drainage, surfacing, ground cover), not a genuine physical
  impossibility. Fixed the same way production-zone crossing was already
  softened: erosion-prone soil is now a scored PREFERENCE
  (`_erosion_avoidance_score()`, `EROSION_AVOIDANCE_SCORE_WEIGHT` — see
  the `road_corridors.py` bullet above), with a new
  `crosses_erosion_prone_soil` property and a confidence_notes caveat
  (real drainage/erosion-control engineering — surfacing, culverts,
  grading — would be needed) when a candidate crosses it, mirroring
  `crosses_production_zone`'s existing treatment.
- **A fourth bug**, the N Montour Rd/Montour Run spillover flagged above:
  even after the fetch-context clip (`FLOODPLAIN_FETCH_CONTEXT_BUFFER_METERS`)
  bounded each raw NHD feature before buffering, nothing bounded the
  result AFTER buffering — the buffer stroke itself (and however much
  stream length survived the looser fetch-context clip) could still
  produce a final exclusion piece extending well past any distance
  actually relevant to the parcel. Confirmed live and visually (plotted
  GeoJSON): an 11.2-acre union of which only 0.077 acres (0.6% of the
  parcel) actually overlapped it — a long buffered band along Montour Run,
  entirely on the far side of N Montour Rd from the field. Fixed by
  intersecting each buffered NHD stream/water-body piece against
  `boundary_polygon_utm.buffer(FLOODPLAIN_FINAL_RELEVANCE_BUFFER_METERS)`
  (75m — meaningfully smaller than the 200m fetch-context buffer, but
  meaningfully larger than the 30m stream buffer alone, so genuine
  near-parcel floodplain risk on a differently-shaped property isn't
  clipped away) before adding it to the union. Scoped to the NHD piece
  only — the SSURGO hydric piece is already bounded to the parcel via
  `STIntersection` (a strict subset of any buffer around it), so this
  clip is a no-op there and was left untouched, along with the
  whole-mukey hydric threshold fix above.
  Both the erosion-preference and NHD-final-clip fixes are regression-
  tested offline (`test_road_corridors.py`'s new erosion-preference
  section; `test_floodplain_union_scope.py`'s new "bug 3" section, which
  mocks a distant stream mirroring the real Montour Run finding and a near
  one to confirm genuine near-parcel floodplain risk survives the clip).
- `solar_suitability.py`'s road-proximity scoring falls back to the
  top-ranked `suggested_road_corridor` when no existing-road data is
  reachable (real road data always wins when available) — see
  `_suggested_corridor_as_road_fallback()` and
  `test_solar_road_fallback.py`. Unchanged by the later point-candidate
  redesign above — the fallback logic works the same way against a
  candidate footprint's own distance to the corridor.
- `fencing.py` — the Subdivision Fences data layer (Scale of Permanence
  step 7): real computed geometry for exactly two fencing types.
  STREAM EXCLUSION fencing buffers each real NHD stream (`hydrology_data.py`,
  already line geometry, not a candidate zone) by a configurable
  `STREAM_EXCLUSION_BUFFER_METERS` and outputs the buffer's OUTLINE (a
  line, not a filled area) as the fence-line layer `exclusion_fencing`.
  PERIMETER fencing wraps the property boundary itself, unmodified, as
  layer `perimeter_fencing` — geometry only, no fence type/height/material
  guidance (explicitly out of scope). Everything else Subdivision Fences
  covers — pond/water zone exclusion, tree crop/windbreak exclusion,
  subdivision/rotational fencing — is deliberately NOT computed here: it
  all keys off candidate/planning geometry (a water system candidate
  zone, a proposed windbreak) rather than a real, already-sited feature,
  so buffering it would draw a misleadingly specific fence line around
  ground that isn't confirmed yet. `report_generator.py`'s step 7 prompt
  handles those narratively instead — pond/water and tree crop exclusion
  are framed as a future consideration (once a site is actually sited),
  and subdivision/rotational fencing is explicitly conditional on
  livestock being part of the operation, reasoning only about general
  routing (e.g. along a ridge/valley line) with no specific paddock
  sizes, counts, or rotation schedules. Same pure-core-logic split as the
  other candidate-geometry layers (`find_stream_exclusion_fencing()` is
  network-free and unit-tested against synthetic stream geometry —
  `test_fencing.py`).
- **`scenario_generation.py` (REMOVED)** — an earlier N-ranked-scenario
  design (computing water/solar/road/fencing candidates once per
  production-zone subset instead of once against the union of every
  candidate) was implemented, offline-tested, and merged, then deleted
  before ever being wired into `report_generator.py`'s prompt or
  ground-truthed against a real property: it was abandoned in favor of a
  different approach, and had never been re-verified against several
  downstream changes that landed after it (the production slope/road
  grade threshold changes, the solar/road exclusion-to-preference
  softening, the hydric-composition-threshold and NHD-clipping fixes).
  Deleted alongside its two test files
  (`test_scenario_generation.py`/`test_scenario_generation_pipeline.py`)
  rather than carrying unverified, unwired code forward — nothing else in
  the codebase imported from it (confirmed via a full-repo grep
  immediately before deletion). `road_corridors.py`'s
  `_fetch_existing_road_union()` was also removed in the same pass — it
  existed solely as `scenario_generation.py`'s own shared per-report road
  fetch (unnamed-union shape, as opposed to
  `_fetch_existing_road_features_utm()`, which
  `identify_road_corridor_candidates()` itself uses) and had no other
  caller left once `scenario_generation.py` was gone.
- `report_generator.py` — combines all of the above and calls the Claude
  API to generate the narrative Scale of Permanence report.
- `generate_full_report.py` — the full end-to-end pipeline: give it a
  boundary once, it runs every data-fetching step and generates the
  final report. This is the main script to run for a real test.
- `parcel_boundary.py` — fetches legal parcel boundaries from Allegheny
  County's GIS system specifically. Superseded by manual boundary drawing
  (see below) as the actual product approach, but kept as a working
  reference.

## Running it yourself

Needs internet access (won't run in a fully offline sandbox). Setup:

```
pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."
export NREL_API_KEY="..."  # optional -- only needed for irradiance_data.py's regional baseline note
python3 generate_full_report.py
```

Individual modules can also be run standalone (`python3 climate_data.py`,
etc.) to test just one data layer without triggering a full report /
Claude API call — useful when only testing a new or changed module.

## Key product decision: manual boundary drawing

Rather than trying to auto-fetch legal parcel boundaries (which vary by
county/state with no unified nationwide API), the tool has users draw
their intended farm area directly on a map. This also correctly handles
the common case where someone wants to design only part of their land
(e.g. 50 of 100 acres). See the frontend project for the actual drawing
tool (built with Leaflet).

## Roadmap / next pieces

**Near-term:**
- Connect the frontend's drawn boundary to this backend (currently two
  separate, unconnected pieces — frontend has the map, backend has the
  pipeline)
- Imagery/land cover data as an additional layer (NAIP/Sentinel-2)
- Uploaded soil test result parsing (PDF/photo → structured data →
  incorporated into the report, alongside SSURGO data)
- Live address autocomplete — tried using Photon (free, OSM-based), but
  its coverage is thin for rural addresses specifically, which is most
  of this tool's actual target audience. Reverted to a simple "type
  full address, hit Enter" search using the Census geocoder, which is
  reliable for rural addresses. Worth revisiting with a paid service
  (e.g. Mapbox, better rural coverage) during a future polish pass if
  live suggestions turn out to be worth the added cost/complexity.

**Future / potential premium tier:**
- Real LiDAR-based *keypoint* detection specifically — a single precise
  pond/dam siting point (storage volume, dam wall geometry) within a
  water system candidate zone. The DEM/raster foundation and
  valley-based *zone* identification this would build on now exists
  (`dem_data.py`, `valley_delineation.py`, `water_candidate_zones.py`) —
  deliberately a zone, not a point, in this pass. Precise keypoint/keyline
  siting on top of it is meaningfully more work (still a plausible paid
  add-on candidate) and is explicitly out of scope for the current layer.
- Validate `valley_delineation.py`/`water_candidate_zones.py` against a
  real property with known ground truth, and tune
  `MIN_STREAM_CONTRIBUTING_AREA_ACRES` / `MIN_PRIMARY_VALLEY_CONTRIBUTING_AREA_ACRES`
  (`valley_delineation.py`), `MAX_PRODUCTION_SLOPE_PCT`
  (`production_area.py`), `MIN_BOUNDARY_SETBACK_METERS` /
  `MAX_SERVICE_DISTANCE_METERS`
  (`water_candidate_zones.py`), and `water_suitability.py`'s own composite
  factor weights accordingly — all deliberately exposed as module-level
  constants for exactly this.
- `production_area_ceiling.py`'s `PRODUCTION_CEILING_PCT_OF_PARCEL` (80.0)
  is a documented starting ceiling, not a value derived from or validated
  against a real property yet — same "tune once ground-truthed" status
  as `production_suitability.py`'s own composite weights. Verified only
  offline against synthetic terrain so far (`test_production_area_ceiling.py`);
  run `python3 production_area_ceiling.py` against the real six-point
  reference property (blocked in this sandbox by egress policy) and
  confirm the trimmed result's `percent_of_parcel`/
  `production_ceiling_target_met` look sensible before treating the 80%
  figure as validated rather than just a reasonable-sounding default.
- **RESOLVED** — `production_area.py`'s own `identify_production_areas()`
  used to have the same convex-hull-inflation pattern noted here in an
  earlier revision of this Roadmap: `polygon_utm`/`area_acres`/
  `geometry_wgs84` built from `MultiPoint(cell centers).convex_hull`
  rather than the real cell-square-union footprint. The production-zone
  pipeline consolidation (`production_area.py`/`production_area_ceiling.py`/
  `production_suitability.py`) fixed this by construction: STEP 3
  (`production_area.cluster_and_gate()`) is now the ONE shared
  implementation building every candidate's `polygon_utm` from
  `_cell_union_footprint()`, used by both the raw and ceiling-optimized
  entry points — there is no longer a second, hull-based code path to
  drift out of sync. `display_polygon_utm` (the convex hull) remains, but
  only as an explicitly rendering-only convenience field.
- Same ground-truth validation pass for `solar_suitability.py`'s
  point-candidate model: check that top-ranked candidates are near a
  real road, low-slope, south-facing, and (per the new proximity
  preference, not exclusion) sensibly close to a production zone's edge
  when one is nearby, on a known property, and tune `MAX_SOLAR_SLOPE_PCT`,
  the score weights (`SLOPE_SCORE_WEIGHT` / `ASPECT_SCORE_WEIGHT` /
  `SHADING_SCORE_WEIGHT` / `PRODUCTION_PROXIMITY_SCORE_WEIGHT`),
  `MIN_SUITABILITY_SCORE`, `CANDIDATE_POINT_SPACING_METERS`,
  `MAX_STRUCTURE_FOOTPRINT_ACRES`, `PRODUCTION_PROXIMITY_REFERENCE_METERS`,
  and `ROAD_PROXIMITY_BUFFER_METERS` accordingly.
- `terrain_metrics.py`'s shading proxy is DEM-only horizon/terrain
  shading — it has no vegetation/canopy signal at all (no DSM-derived
  canopy height model exists in this pipeline yet). A real canopy height
  model, or a per-pixel NDVI overlay reprojected onto the DEM grid (using
  `imagery_data.py`'s already-merged Sentinel-2 fetch — NOT the separate,
  still-unmerged NLCD/NDVI branch), would be a meaningfully better shading
  signal and is a reasonable next step once the DEM-only version is
  validated against real tree cover on the ground.
- `irradiance_data.py` targets `developer.nlr.gov` per the current
  guidance this was built against (NREL's API domain migrating off
  `developer.nrel.gov`). That's the one constant
  (`NLR_PVWATTS_ENDPOINT`) to double-check/update if that domain changes
  again — this module was written and unit-tested without live network
  access to that endpoint in this environment, so it's unverified against
  a real request.
- `farm_roads_data.py` targets USGS National Map's Transportation
  MapServer (`carto.nationalmap.gov`), following the same
  service-catalog convention as `hydrology_data.py`'s NHD endpoint. This
  note used to say the layer ID was unverified against a live request —
  it turned out to matter: the original layer id (0) was a
  container/group layer, not real road data, and silently returned zero
  results forever. Confirmed live and fixed — see `farm_roads_data.py`'s
  own module docstring and the `ROAD_LAYERS` bullet above.
- **Outstanding, required, not yet done**: re-run `production_suitability.py`
  against the real six-point reference property from an environment with
  actual network access (this sandbox's egress policy blocks
  `elevation.nationalmap.gov`/`sdmdataaccess.sc.egov.usda.gov` — confirmed
  policy denial, not transient, both before and after the hydric-only fix
  below) and report the real resulting candidate(s): id, `area_acres`,
  `soil_carved_acres`, `soil_carved_pct`, `suitability_score`, AND the
  full `confidence_notes` text for each. A prior live run (before the
  hydric-only narrowing and the empty-confidence_notes fix, both below)
  found two candidates — 17a (2.15 ac, 3.08 ac carved) and 19a (1.83 ac,
  0.18 ac carved) — that run needs repeating to confirm those numbers
  either hold (expected, if every "poorly drained" component on this
  property also happened to be hydric — spot-checked and true for what
  was seen so far, but not exhaustively) or change, and to confirm
  `confidence_notes` is now actually populated (it shipped empty on that
  run — see the fix in `score_production_areas()`/`_confidence_notes_for()`).
  **A later live run reported patch 17's carve at 59% — that number is
  now suspected inflated by the whole-mukey hydric bug documented in the
  `road_corridors.py` section above** (`_fetch_disqualifying_soil_union()`
  had the identical bug, now fixed via the same shared
  `soil_data.hydric_disqualifying_mukeys()` threshold). This 59% figure
  needs re-checking live against the fix, not assumed either fixed or
  still accurate — this sandbox could only verify the fix offline/
  synthetically (see `test_production_suitability.py`'s trace-hydric
  regression section, built from this property's own real mukeys/
  percentages).
  Confirm at least one real, non-empty production candidate survives (an
  earlier whole-patch-exclusion version of this module shipped without
  this check at all and excluded BOTH real surviving candidates on this
  property — see the module's own docstring). Also cross-check the carved
  geometry's area against the sum of the original patch's non-hydric soil
  component acreages as a rough sanity check that carving didn't remove
  too much or too little. Once done, check that the ranking matches
  ground truth more generally (e.g. a known compact, gently-sloped block
  outranks a fragmented one) and tune
  `SLOPE_FACTOR_WEIGHT` / `SIZE_FACTOR_WEIGHT` / `ASPECT_FACTOR_WEIGHT`,
  the `SIZE_AREA_SUBWEIGHT` / `SIZE_SHAPE_SUBWEIGHT` sub-weights, and
  `REFERENCE_MAX_AREA_ACRES` accordingly. Soil is intentionally NOT one of
  the tunable weights (see above — carving-only by design, not a scored
  factor). Once validated, wire `suitability_score` into
  `report_generator.py`'s narrative and use it as an input to future
  scenario-selection logic — both deliberately out of scope for this pass.
- Ground-truth validation pass for `road_corridors.py` against a real
  property with real existing-road data now reachable (see
  `farm_roads_data.py`'s layer fix): confirm both contour-band and
  ridge-top candidates actually show up where terrain supports them, that
  they route around real pond zones and real floodplain/hydric ground
  (the only two HARD exclusions left) rather than the DEM-only fallback,
  that a candidate crossing a production zone OR erosion-prone soil
  genuinely scores lower than a comparable non-crossing one (both are now
  the preference, not exclusion, model), and that anchoring prefers real
  road frontage over the arbitrary boundary fallback where a road is
  reachable nearby. **Specifically re-confirm ALL FOUR floodplain/erosion
  fixes above against live data**: the NHD-clipping fix, the whole-mukey
  hydric threshold fix (`MIN_HYDRIC_COMPONENT_PCT_TO_EXCLUDE`), the
  erosion-prone-soil hard-exclusion-to-preference fix
  (`EROSION_AVOIDANCE_SCORE_WEIGHT`/`crosses_erosion_prone_soil`), and the
  NHD final-relevance clip (`FLOODPLAIN_FINAL_RELEVANCE_BUFFER_METERS`) —
  report the actual `hydric_floodplain_union` area in acres compared to
  the real parcel's own acreage (the size-comparison/plotted-GeoJSON
  approach that caught all of these: 33.9 acres, then 18.77 acres after
  the NHD-clip fix, on a 13.23-acre parcel; separately, an 11.2-acre union
  of which only 0.077 acres actually overlapped the parcel, before the
  final-relevance clip), which mukeys are now included/excluded from the
  hydric exclusion set and their summed hydric composition percentage,
  and whether the corrected union visually looks like a plausible
  near-parcel floodplain exclusion rather than a band reaching across N
  Montour Rd to the far side of Montour Run. Also report the resulting
  candidate count/types/grades/scores/`crosses_production_zone`/
  `crosses_erosion_prone_soil`/anchor info from
  `identify_road_corridor_candidates()` — confirm real candidates exist
  again (previously confirmed possible: 5 candidates once all three
  exclusions — production, floodplain/hydric, erosion — were set aside).
  This session's sandbox could only verify all four fixes offline/
  synthetically (mocked unclipped/distant/near stream features; the real
  property's own reported mukeys/percentages — Atkins 85%,
  Guernsey-Vandergrift 1%, Ernest-Vandergrift 5%+3%=8% — mocked to confirm
  only the genuinely dominant one disqualifies; synthetic erosion-prone-
  soil crossing/non-crossing candidates to confirm the preference, not
  exclusion, scoring), never against the real property, due to the same
  blocked `hydro.nationalmap.gov`/`sdmdataaccess.sc.egov.usda.gov` egress
  noted throughout this file. Tune `MAX_ROAD_GRADE_PCT` (see the module for the
  current rationale/sourcing caveat), `CONTOUR_BAND_WIDTH_METERS`, the
  ridge `RIDGE_MIN_AREA_ACRES` / `RIDGE_MIN_PRIMARY_AREA_ACRES`
  thresholds, the exclusion buffers (`POND_ZONE_EXCLUSION_BUFFER_METERS`,
  `FLOODPLAIN_STREAM_BUFFER_METERS`, `FLOODPLAIN_FINAL_RELEVANCE_BUFFER_METERS`),
  the avoidance preferences (`PRODUCTION_AVOIDANCE_REFERENCE_METERS`,
  `EROSION_AVOIDANCE_REFERENCE_METERS`), `soil_data.DEFAULT_EROSION_KWFACT_THRESHOLD`,
  and the scoring weights (`GRADE_SCORE_WEIGHT` / `EXCLUSION_MARGIN_WEIGHT`
  / `LENGTH_SCORE_WEIGHT` / `PRODUCTION_AVOIDANCE_SCORE_WEIGHT` /
  `EROSION_AVOIDANCE_SCORE_WEIGHT`) accordingly.
- `road_corridors.py`'s PCA-based centerline extraction (contour-band
  candidates) is a simple, explainable thinning heuristic, not a proper
  skeletonization algorithm — on an oddly-shaped or branching low-slope
  patch it can produce a less-than-ideal path through the middle of it.
  Reasonable to revisit with a real raster skeletonization approach if
  ground-truthing shows this matters in practice.
- `fencing.py`'s core buffer/geometry logic is verified offline
  (`test_fencing.py`, synthetic stream geometry) but not yet run against
  a live NHD fetch for the user's own property in this environment (no
  outbound route to `hydro.nationalmap.gov` here — same live-request gap
  noted above for `farm_roads_data.py` and `irradiance_data.py`). Run
  `python3 fencing.py` from an environment with real network access to
  confirm real stream geometry buffers as expected, and tune
  `STREAM_EXCLUSION_BUFFER_METERS` (currently a commonly-cited rule-of-
  thumb minimum, not a site-specific or regulatory value — see the
  module for the current rationale/sourcing caveat) against real
  livestock-exclusion requirements once ground-truthed.

## Deploying

Once ready, this backend deploys to Render or Railway (connected to this
GitHub repo), giving it a live URL with real internet access to reach
USDA/USGS/Open-Meteo APIs. The frontend (separate repo) deploys to
Vercel.
