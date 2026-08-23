# Interactive Design — Architecture Proposal

## Status

Proposal only. No module changes are proposed or implied here — this
document describes how the existing architecture extends to support an
interactive, step-by-step design flow, what new *architectural* components
that requires, and the tradeoffs between the viable options. The companion
document `interactive-design-frontend-architecture.md` (in
`keyline-designer-frontend`) covers the frontend half; this document covers
the analysis, the shared session model, and the backend.

---

## 1. Analysis of the current architecture

### What exists today

The backend follows the three-layer architecture described in the pipeline
architecture guide, and — importantly — actually implements it end to end:

- **Layer 1 — `parcel_data.fetch_parcel_data()`.** Every raw,
  network-backed layer for one boundary, fetched exactly once, hard-failing
  on any gap. Returns one `ParcelData` dataclass (~14 fields: DEM, soil
  composition/geometry, hydrology, farm roads, climate, canopy, imagery,
  irradiance, …).

- **Layer 2 — `pipeline_context.build_pipeline_context()`.** One
  orchestrator call that walks the KSOP dependency chain in order —
  valleys → keypoints → production areas → road/soil exclusion unions →
  water candidate zones → selected water zone → keypoint relationships →
  road network (from the user's access point) → structure (solar) site →
  tree zone candidates — and returns one `PipelineContext`. Every entry
  point it calls follows the **override pattern**: all upstream inputs are
  optional parameters; supplied values are used directly, absent ones are
  self-computed, and overrides are forwarded through nested calls.

- **Layer 3 — consumers.** `generate_full_report.py` /
  `generate_pdf_report.py` (narrative + PDF) and
  `render_layout_map.fetch_layout_layers()` (map layers), each building
  Layer 1 once, Layer 2 once, and assembling output from those.

- **API.** `api.py` is a thin, stateless, synchronous Flask wrapper:
  `boundary + access_point` in, finished PDF out, one request, ~30–60s.

- **`feature_schema.py`.** A shared WGS84 GeoJSON FeatureCollection
  contract (id, layer, label, confidence, confidence_notes) already used by
  the vector layers — the natural wire format for anything the frontend
  displays or edits.

### The key observation

`build_pipeline_context()` is a **batch selector**: at every decision point
in the chain it takes the rank-1 winner (selected water zone, selected road
network, selected structure site) or the full computed set (production
areas, tree zones) and passes it downstream *through the override
parameters of the next step's entry point*.

The interactive future state is the **same chain with a human in the
selector's seat**. "User adjusts production zones, then water zones are
generated excluding them" is, mechanically, "call
`identify_water_system_candidate_zones()` with `production_areas=` set to
the user's committed set instead of the optimizer's" — an argument the
function already accepts. The override pattern, built to eliminate
redundant computation, turns out to be exactly the injection seam
interactive design needs. **The pipeline modules do not need to change.
The orchestration around them does.**

### What is actually missing

Four things, all architectural, none inside the KSOP modules:

1. **State across requests.** Today the entire run lives inside one HTTP
   request. Interactive design spans many requests over minutes-to-days;
   `ParcelData` and the partially built context must survive between them
   (or be reconstructable) without re-fetching — the one-data-fetch
   principle now has to hold *across a session*, not just within a call.

2. **A serialization / translation boundary.** Internal step results carry
   UTM shapely geometry, numpy-backed DEM references, and per-feature
   derived fields (`polygon_utm`, `render_fill_polygon_utm`, `cells`,
   `representative_elevation_m`, …). The frontend speaks WGS84 GeoJSON.
   Outbound conversion partially exists (`feature_schema.py`,
   `*_to_geojson()` helpers); **inbound** conversion — a user-edited or
   user-drawn polygon arriving as GeoJSON and needing the full internal
   dict shape so downstream overrides accept it — exists nowhere.

3. **Selection as a first-class, explicit value.** The guide's known
   limitation ("not supplied" and "computed nothing" look identical) is
   tolerable in batch mode; in interactive mode "the user committed *no*
   water zone" must be distinguishable from "the water step hasn't run
   yet," or downstream steps will silently self-compute a water zone the
   user rejected. (The `selected_road_corridor` field already solves this
   locally by always carrying the full network dict, `branches=[]` and all,
   never `None` — that precedent generalizes.)

4. **Invalidation semantics.** When an upstream commit changes, what
   happens to downstream results? Batch mode has no such question;
   interactive mode is defined by it.

---

## 2. The session model (shared foundation)

Everything below rests on one structural decision: split session state into
a small durable record and a large rebuildable cache.

### 2.1 The Design Document (small, durable, canonical)

One JSON document per design session — the **single source of truth** for
everything the *user* has decided:

```
{
  "session_id": "…",
  "schema_version": 1,
  "document_revision": 17,          // bumped on every mutation
  "boundary": [[lon, lat], …],      // step 0 commit
  "access_point": [lon, lat],       // committed at the roads step
  "steps": {
    "landform":   { "status": "committed", "revision": 3,
                    "features": <FeatureCollection>,       // production zones + keypoints
                    "provenance": {...per-feature: generated | user_modified | user_added} },
    "water":      { "status": "committed", "revision": 1,
                    "features": <FeatureCollection> },     // possibly empty = "none, on purpose"
    "roads":      { "status": "generated" },               // proposals shown, not yet committed
    "structures": { "status": "not_started" },
    "trees":      { "status": "not_started" },
    "report":     { "status": "not_started" }
  }
}
```

Properties that make this the right canonical record:

- **It contains only decisions, never derived bulk data.** Geometry the
  user committed (as `feature_schema` GeoJSON, WGS84), per-feature
  provenance, step statuses, revisions. No DEM, no rasters, no candidate
  sets. Kilobytes, not megabytes.
- **Every step's `status` is explicit** — `not_started` / `generated` /
  `committed` — so "committed empty" is representable and unambiguous.
  This resolves missing-piece #3 *at the orchestration layer*, without
  touching any module signature.
- **It is a replayable script.** Given the boundary and the committed
  features, the whole pipeline state is deterministically reconstructable:
  fetch Layer 1, then walk the steps injecting each committed value as the
  override for the next. That property is what makes every persistence
  option in §5 workable and what makes back-tracking (§4) cheap.

### 2.2 The Session Cache (large, rebuildable, disposable)

Per-session server-side storage of the heavy native objects:

- the `ParcelData` (fetched once, at session creation),
- the incrementally accreted context fields in their internal shape
  (valleys, DEM-derived arrays, scored production patches, candidate sets,
  exclusion unions),
- the last `generate` proposals per step (so a commit doesn't recompute
  them).

The cache is **never authoritative**. Any entry — or the whole cache — can
be dropped and rebuilt from the Design Document. External fetches inside
the rebuild are served by the fetch cache (§5), so a rebuild is compute,
not network.

### 2.3 The Step Registry (the interactive Layer 2)

A declarative registry — the interactive sibling of
`build_pipeline_context()`, living at the same architectural layer —
listing the steps in KSOP order. Each entry declares:

| Declares | Example (water step) |
|---|---|
| **consumes** (upstream context/committed fields) | dem, boundary_polygon_utm, valleys, committed production areas, canopy |
| **generate** (the existing entry point to call, with consumed values passed as its overrides) | `identify_water_system_candidate_zones(…)` |
| **produces** (context fields it contributes) | `water_zones`, `selected_water_zone` |
| **commit contract** (what a valid commit looks like; eligibility rules to enforce) | zero or more zones; geometry inside eligible area |
| **user inputs** (extra parameters collected at this step) | roads step: `access_point` |

The registry is what the generic session endpoints (§3) iterate over; the
dependency edges in `consumes` are also exactly the invalidation edges for
§4. `build_pipeline_context()` itself remains untouched as the batch path:
the existing one-shot report endpoint, the diagnostics, and the call-count
tests keep working. Batch mode is simply the degenerate session in which
every step auto-commits its own rank-1 winner — one mental model, two
drivers.

### 2.4 The translation boundary (outbound + inbound)

A single adapter layer, deliberately **outside** the KSOP modules, at the
edge between the session orchestrator and the wire:

- **Outbound:** internal step results → `feature_schema` GeoJSON, per
  layer, for map display. Largely an extension of the existing
  `*_to_geojson()` pattern into one consistent place.
- **Inbound (the genuinely new piece):** committed GeoJSON → the internal
  per-feature dict shape downstream overrides expect. For a user-drawn or
  user-adjusted production zone that means recomputing the derived fields
  (`polygon_utm` via reprojection against the cached DEM CRS,
  `representative_elevation_m`, `render_fill_polygon_utm`, raster `cells`,
  …) from the cached `ParcelData` — pure, local, deterministic derivation,
  no fetch. This "rehydration" is what lets a user-authored feature travel
  down the same override parameters as a computer-authored one, which is
  the whole trick.

Keeping both directions in one boundary layer preserves the guide's
discipline: modules stay agnostic of the wire, the wire stays agnostic of
shapely/numpy, and there is exactly one place where shape drift between the
two can be caught.

### 2.5 Commit validation (server-authoritative)

The frontend receives **eligibility layers** (exclusion unions, canopy
mask, slope ceiling — already computed as part of the context) as GeoJSON
so it can constrain drawing interactively. That client-side constraint is
UX only. Every commit is re-validated server-side against the same masks
before it enters the Design Document; an invalid commit is rejected with
the offending features identified. The client is advisory, the server is
authoritative — the same fail-loud posture as `parcel_data.py`.

---

## 3. Backend architecture

### 3.1 API shape — session-scoped REST

```
POST   /api/sessions                          { boundary }            → { session_id, job }
GET    /api/sessions/{id}                                             → Design Document + step statuses
POST   /api/sessions/{id}/steps/{step}/generate   { params? }         → job → proposals (GeoJSON)
POST   /api/sessions/{id}/steps/{step}/commit     { features,
                                                    base_revision }   → new revisions + invalidated steps
POST   /api/sessions/{id}/steps/{step}/reopen                         → step (and downstream) reset; see §4
GET    /api/sessions/{id}/layers/{name}                               → context/eligibility layers (GeoJSON)
POST   /api/sessions/{id}/report                                      → job → PDF
GET    /api/jobs/{job_id}                                             → { status, result | error }
```

- **Session creation runs Layer 1.** `POST /api/sessions` validates the
  boundary, fires `fetch_parcel_data()` exactly once, and hard-fails the
  session creation the same way the batch pipeline hard-fails a run —
  incomplete raw data means no session, not a degraded one. The
  one-data-fetch principle holds for the whole session lifetime: every
  later `generate` reads the cached `ParcelData` through the same override
  parameters `generate_full_report.py` passes today.
- **`generate` is idempotent and repeatable** — regenerating a step's
  proposals (e.g. after the user deleted everything and wants a fresh
  start) recomputes only that step, since all upstream inputs come from
  cache/commits.
- **`commit` is synchronous and cheap** (validate + rehydrate + write
  document); **`generate` and `report` are asynchronous jobs** (they carry
  DEM-wide computation — Dijkstra runs, scoring passes — and, for the
  report, a Claude call). Two viable job transports:
  - *202 + polling* (`GET /api/jobs/{id}`): simplest, survives proxies,
    fits Flask as it stands. **Recommended first.**
  - *Server-sent events / websockets:* nicer progress UX, more moving
    parts. A later upgrade, not a fork in the architecture — the job
    resource stays the same either way.
- **Optimistic concurrency.** Every commit carries the `base_revision` of
  the step it was edited against; a mismatch (another tab, a stale client)
  returns 409 with the current document, and the frontend reconciles. This
  is cheap insurance that becomes essential the moment a session outlives
  one browser tab.

### 3.2 How a step actually executes (worked example: water)

1. User committed landform (adjusted two production zones, deleted one
   keypoint, drew one new zone — all validated, rehydrated, stored).
2. `POST …/steps/water/generate`: the orchestrator looks up the water
   entry in the registry, assembles its `consumes` set — `dem`,
   `boundary_polygon_utm`, `valleys` from cache; **production areas from
   the committed landform step (rehydrated), not from the optimizer**;
   canopy from `ParcelData` — and calls
   `identify_water_system_candidate_zones()` with those as its existing
   override arguments. Nothing upstream re-runs; the call count discipline
   the guide demands is preserved because commits *are* the overrides.
3. Candidate zones return, are cached, translated outbound, and shown.
4. User selects zone(s); `POST …/steps/water/commit` validates, rehydrates,
   writes the document (`status: committed`, possibly an empty selection —
   explicitly).
5. The roads step's `generate` later consumes `selected_water_zone` from
   that commit — including the explicit-empty case, forwarded as a real
   answer, never as "not supplied."

### 3.3 What deliberately does not change

- The KSOP modules, their signatures, and the override pattern.
- `parcel_data.py` and its hard-fail contract.
- `build_pipeline_context()` and the batch consumers
  (`generate_full_report`, `fetch_layout_layers`) — the report step of an
  interactive session is a Layer 3 consumer reading the accreted context,
  exactly as the guide prescribes; the only new consumer-side need is an
  entry path that accepts an already-built context rather than building
  its own, which is orchestration wiring, not module redesign.
- `feature_schema.py` — it becomes the wire contract it was already
  shaped to be.

---

## 4. Going back after commit

**Decision: going back is allowed, and a recommit cascades — every
downstream step reverts to `not_started` (its commits and proposals
discarded), after an explicit client-side warning listing what will be
reset.**

Why allow it at all: the Design Document/cache split makes it nearly free
(a reopen is a document edit; downstream state was rebuildable anyway), and
a design tool that locks early decisions fights how design actually
happens — the water step is often what teaches the user their production
zones were wrong.

Why cascade rather than preserve: downstream results were computed *from*
the upstream value that just changed. KSOP's entire premise is that later
factors answer to earlier ones; a tree layout that was excluded around a
water zone that no longer exists is not "slightly stale," it is wrong in a
way the map will not show. Keeping it — even flagged — invites exactly the
"misleading partial result" the architecture guide's hard-fail philosophy
exists to prevent. Discarding is honest, cheap to recover from
(regeneration is warm — Layer 1 cached, upstream commits intact), and
simple enough to be explained in one warning dialog.

**Considered and rejected (for v1):**

| Alternative | Why not |
|---|---|
| **No going back** (commits final; start a new session to change) | Simplest backend, brutal UX; also weakest fit with the document model, which makes reopen trivially cheap — we'd be paying UX to avoid a cost we don't have. |
| **Mark-stale-and-keep** (downstream commits survive, flagged, user re-validates each) | Preserves user work after upstream edits, but downstream edits were made against geometry that no longer exists; "re-validating" them is a hard, module-touching problem (which features still make sense against the new upstream?), and a stale-but-visible layout on the map is exactly the misleading artifact to avoid. Worth revisiting later as an *assistive* feature (e.g. re-offering the user's old features as suggestions where still eligible) once the strict model is proven. |

Mechanics: `reopen` on step N sets N to `generated` (its last commit
retained as the editable starting point) and N+1… to `not_started`;
`commit` on an already-committed step does the same cascade implicitly.
Revisions make the cascade observable to any stale client via the 409 path.

---

## 5. What persists a session

The three candidate models, weighed:

### Option A — pure server-side session state (in-memory / Redis)

Everything (ParcelData, context, decisions) lives server-side keyed by a
session id; the client holds only the id.

- **Pros:** native objects never cross a serialization boundary; smallest
  payloads; fastest steps; simplest mental model.
- **Cons:** the server becomes stateful — sessions die with the process
  (or need shapely/numpy pickling into Redis, which is version-fragile);
  horizontal scaling needs sticky sessions; memory per live session is
  large (DEM + derived arrays); eviction policy becomes a correctness
  question, because evicting the *only* copy of the user's decisions is
  data loss.

### Option B — client-held state token

The server is stateless; each response returns the full state blob (or a
signed token), and the client posts it back on every request.

- **Pros:** zero server state, trivially scalable, survives any restart.
- **Cons:** the heavy state (DEM, rasters, shapely) is megabytes and
  doesn't round-trip JSON cleanly — so in practice the token could only
  carry the *decisions*, forcing every request to rebuild the heavy state
  anyway (collapsing into Option C with extra upload cost); repeated
  multi-MB uploads on every step; the authoritative record lives on the
  least trustworthy node, so every commit needs full re-validation and
  tamper protection (signing). No advantage the hybrid below doesn't get
  more cheaply.

### Option C — stateless recompute over a warm fetch cache

The session record is just `{boundary, commits}`; every step request
replays the pipeline from Layer 1, with all external fetches memoized
(keyed by boundary hash) in a shared cache.

- **Pros:** near-stateless backend; the session record is tiny, durable,
  versionable; crash recovery for free; the replay path *must* work, which
  keeps the document model honest.
- **Cons:** per-step latency — replaying derived computation (valleys,
  scoring, exclusion unions) on every request is seconds of CPU even with
  all fetches warm; fetch-cache invalidation (upstream data updates)
  becomes a correctness concern; feels sluggish exactly where
  interactivity matters most.

### Recommendation — durable Design Document + rebuildable Session Cache (A ⊕ C)

The split in §2 is deliberately this hybrid:

- The **Design Document** persists durably (start: a JSON file per session
  on disk; later: any small document store). It is small, canonical, and
  survives anything. The client holds only the `session_id` (URL +
  localStorage) — resuming is `GET /api/sessions/{id}`.
- The **Session Cache** is Option A's speed without its fragility: native
  objects in process memory (single Flask process today; per-node caches
  later), evictable at will because a miss degrades to Option C's replay —
  rebuild from the document over the warm fetch cache — not to data loss.
- The **fetch cache** (memoized external fetches keyed by boundary hash)
  backs the rebuild path and incidentally de-duplicates fetches across
  sessions on the same land.

This keeps the one-data-fetch principle as the *steady-state* guarantee
(one fetch per session in the normal case; one fetch per cache lifetime in
the degraded case), scales from the current single-process Flask deployment
to multi-node (shared document store + per-node caches + sticky-preferred
routing, correct even without stickiness) without an architectural rewrite,
and makes the failure story boring: lose a process, lose only warm caches.

---

## 6. Frontend architecture (summary)

Detailed in `interactive-design-frontend-architecture.md`
(keyline-designer-frontend). The shape, in brief, so this document stands
alone:

- A **session store** as the single client-side source of truth, mirroring
  the Design Document (session id, per-step status/features/revision),
  replacing the current flat `useState` cluster in `App.jsx`.
- A **step controller** — a linear KSOP wizard where every step is the same
  small state machine (`idle → generating → reviewing/editing →
  committing → committed`) driven by a per-step declarative definition
  that mirrors the backend Step Registry.
- A **map layer stack**: basemap → context/eligibility layers (server
  GeoJSON, read-only) → committed layers from prior steps (settled
  styling) → the active step's editable layer (vertex drag, feature
  delete, draw-within-eligibility — generalizing the existing
  DrawTool/AccessPointTool pattern).
- **Server-authoritative discipline:** the client never derives design
  content, only displays and edits server GeoJSON; eligibility checks in
  the browser are UX hints, commits are validated server-side.
- **Job handling** (poll `GET /api/jobs/{id}`) and **conflict handling**
  (409 → reconcile from the returned document) as shared plumbing, not
  per-step code.

---

## 7. Migration path

Each stage ships working software; none touches KSOP module internals:

1. **Session skeleton.** Sessions resource, Design Document + on-disk
   persistence, Layer-1 fetch at creation, fetch cache. The existing
   one-shot PDF endpoint is re-expressed over a session internally (create
   → auto-commit every step batch-style → report) so both paths exercise
   the same orchestrator from day one.
2. **First interactive step (landform).** Step Registry with one entry,
   generate/commit/reopen endpoints, outbound translation, inbound
   rehydration for production zones + keypoint deletion, eligibility
   layers, frontend wizard with one step.
3. **Remaining steps.** Water, roads (access point moves here as a step
   input), structures, trees — each a registry entry + a frontend step
   definition; the cascade-invalidation rule falls out of the registry's
   dependency edges.
4. **Report step.** The Layer-3 consumer path over the accreted context;
   the batch endpoint becomes a thin alias and can eventually be retired.

The one-data-fetch principle, the override pattern, the hard-fail
contract, and the three-layer separation are not casualties of this
proposal — they are its load-bearing structure. The interactive mode is
the same pipeline, driven slower, by the person it was always for.
