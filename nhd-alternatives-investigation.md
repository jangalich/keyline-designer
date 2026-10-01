# Investigation: alternatives to the NHD query service

Investigation only; no code was changed and nothing was committed.

## Preliminaries

`git log --oneline -3` on `main` of `jangalich/keyline-designer`:

```
600a2ea Merge branch 'claude/fervent-cori-wq7fi9'
0975831 Fencing: the boundary fence type is labelled "Perimeter fencing"
b7283b9 Merge branch 'claude/exciting-feynman-d6glgu'
```

Working tree clean (`git status`: nothing to commit). The checked-out session
branch `claude/clever-brown-9gwdwe` is three commits ahead of `main` with
already-pushed keypoint work; it was left untouched.

`nhd_probe.py` is not in the repository and not in this container — it
evidently lived in a previous session's ephemeral scratchpad. The probe was
rebuilt from the production query code (`hydrology_data.py`,
`nhdplus_data.py`, `context_map_data.py`) so that it issues the six real
queries byte-for-byte: flowlines (layer 6) and waterbodies (layer 12) over
the reference parcel's bbox+150 m, the Point layer (layer 0) over the same
box, NHDPlus HR NetworkNHDFlowline (layer 3) attributes-only, and flowlines
plus waterbodies over bbox+1 mile. Probe script:
`alt_probe.py` (session scratchpad), results in `alt_probe_results.jsonl`.

## Candidates assessed

| Candidate | What it is | Endpoint |
|---|---|---|
| **Esri Living Atlas NHDPlus HR** | Esri-hosted copy of USGS NHDPlus High Resolution (1:24k), Release 2 (Feb 2025). Flowlines, Waterbodies+Areas, Sinks. | `services5.arcgis.com/7weheFjxuNkGGiZi/.../National_Hydrography_Dataset_Plus_High_Resolution/FeatureServer` (AGOL item `21b3bad712c84cae82830569ce53326f`, owner `esri_environments`) |
| **EPA WATERS NHDPlus v2.1** | EPA-hosted NHDPlus **medium resolution** (1:100k, 2012 NHD snapshot). Network/non-network flowlines, waterbody, NHD Area, catchments. | `watersgeo.epa.gov/arcgis/rest/services/NHDPlus/NHDPlus/MapServer` |
| **TNM download service** | USGS staged FileGDB/GPKG downloads over S3 (`prd-tnm.s3.amazonaws.com`). | via `tnmaccess.nationalmap.gov/api/v1/products` |

Checked and set aside:

- **Esri "USA Rivers and Streams"** (the layer named in the brief): National
  Atlas-derived, fields are `Name, Feature, State, Region, Miles` only — no
  FCode, no permanence, no order, no drainage area. Disqualified on sight.
- **Esri "NHDPlusV21" Living Atlas layer**: has FCODE/StreamOrde/TotDASqKM
  but is the same 1:100k medium-res data EPA serves, under the same Esri
  licence problem as the HR layer. Dominated on every axis by the two
  candidates above.
- **USGS NLDI** (`labs.waterdata.usgs.gov`): navigation/basin service keyed
  to medium-res COMIDs; no FCode, no waterbody geometry. Not an attribute
  source.
- **3DHP** (`hydro.nationalmap.gov/.../3DHP_all`): NHD's successor, including
  a HydroLocation layer that carries springs. Same host as NHD, so not an
  alternative to the reliability problem — relevant to vintage only (below).
- **OpenStreetMap waterways**: excluded by the brief (ODbL).
- State-portal NHD mirrors (Colorado, Nevada, etc.): per-state patchwork,
  not viable for a US-wide product.

## 1. Attributes

| Attribute the product uses | NHD today | Esri Living Atlas NHDPlus HR | EPA WATERS NHDPlus v2.1 |
|---|---|---|---|
| FCode / permanence (perennial, intermittent, ephemeral) | ✓ | ✓ `fcode` on flowlines and waterbodies | ✓ `fcode`, but on the 1:100k network (see coverage below) |
| Waterbody geometry | ✓ | ✓ (merged "Waterbodies and Areas" layer — NHDArea polygons share it; filterable on `ftype`) | ✓ layer exists, but coverage: **0 of the reference parcel's nearby ponds at bbox+150 m, 3 of NHD's 14 at one mile** |
| Spring and seep points (FCode 45800) | ✓ Point layer | ✗ **none** — its point layer is NHDPlus "Sinks", not NHDPoint | ✗ **none** — layers are Streamgage, flowlines, waterbody, area, catchment |
| Strahler stream order | ✓ NHDPlus HR `streamorde` | ✓ `streamorde`, **identical values** to USGS (verified reach-for-reach) | ✓ `streamorder`, but computed on the 1:100k network: reference reach reads **order 1 where NHDPlus HR reads order 2** |
| Total upstream drainage area | ✓ NHDPlus HR `totdasqkm` | ✓ `totdasqkm`, **identical values** to USGS | ✓ `totdasqkm`, but a different network's number: 2.996 km² vs HR's 2.155 km² for Montour Run at the parcel |

Verification was live, not from docs: field lists pulled from each service's
layer JSON, and the reference parcel's bbox queried on each. Esri's HR copy
returned the same three Montour Run reaches with the same
`permanent_identifier`s (123973363/72/73), same orders, and `totdasqkm`
agreeing to the eighth decimal — the existing `nhdplus_data.parse_flowline_attributes`
join would work unchanged against it. On feature counts over the reference
parcel: Esri HR matched NHD exactly (3 flowlines at 150 m; 48 flowlines and
14 waterbodies at one mile — the same counts recorded in
`context_map_data.py`'s header). EPA's medium-res returned 1, 10 and 3
respectively, and 0 waterbodies at 150 m.

**The disqualifier the brief predicted lands on EPA, not Esri**: medium
resolution drops two of the parcel's three reaches and all nearby ponds.
Layer 1's waterbody query exists to find exactly the small farm ponds that
1:100k compilation omits. Esri's HR copy fails a different attribute —
springs and seeps — because Esri did not publish NHD's Point feature class.

## 2. Licence and commercial use

**Esri Living Atlas NHDPlus HR.** The AGOL item's `licenseInfo` reads,
verbatim: *"This work is licensed under the Esri Master License Agreement."*
The linked terms-of-use summary (esri.com `tou_summary.pdf`, last updated
April 21, 2025) states, verbatim, under **YOU MAY NOT**:

> "Download, redistribute or self-host any content hosted by Esri."
>
> "Use content from ArcGIS Living Atlas for commercial use in your
> application or product. Please contact Esri to discuss licensing
> Esri-owned Living Atlas items for commercial use."

and under its conditions: *"Use with Esri software and comply with its terms
of use. If you do not have Esri software, you must purchase an ArcGIS Online
subscription."* The item is owned by `esri_environments`, i.e. Esri-owned
Living Atlas content. The underlying data is public-domain USGS, but this
hosted copy is contractually closed to exactly our use: a paid product
hitting Esri's endpoint. The REST endpoint answers anonymous queries today;
that is technical reachability, not permission. This is the same Esri MLA
question already open on the HIFLD transmission layer, enlarged.

**EPA WATERS NHDPlus v2.1.** The service publishes **no licence text**: the
service item info carries no `licenseInfo`, `accessInformation` is "US EPA
Office of Water", and no use-constraint metadata record was found on the
service. Recording the basis rather than claiming permission: NHDPlus v2.1
is a US EPA/USGS federal product (a US government work, not copyrightable
under 17 U.S.C. §105), consistent with the repo's own note in
`nhdplus_data.py` ("A USGS product, public domain"). No commercial-use
constraint exists to quote because none could be found — that is an absence,
verified on the service metadata, not a statement by EPA.

**USGS TNM downloads.** Public-domain USGS, same as today's source.

## 3. Reliability, measured

Probe: every ~2.5 minutes, each query issued once per round, sequentially,
2 s apart; 150 s timeout; `ok_fast` ≤ 30 s mirrors the original probe's
"answered within 30 s". NHD ran its six real queries; each candidate ran
its five equivalents (no point-layer equivalent exists on either).

Run: 2026-10-01 16:28–17:55 UTC, 10 rounds, 160 requests total, from this
session's container (which reaches all three hosts through the same proxy).

| Outcome | NHD (`hydro.nationalmap.gov`), 60 req | Esri Living Atlas HR, 50 req | EPA WATERS v2.1, 50 req |
|---|---|---|---|
| Answered within 30 s | 26 — **43.3%** | 50 — **100%** | 50 — **100%** |
| Answered slowly (36–121 s) | 19 — 31.7% | 0 | 0 |
| HTTP 5xx | 3 — 5.0% | 0 | 0 |
| Timeout (150 s) | 12 — **20.0%** | 0 | 0 |
| Connection / TLS error | 0 | 0 | 0 |
| Median / p90 / max time to answer | 22.0 s / 100 s / 121 s | 0.69 s / 0.86 s / 1.6 s | 0.77 s / 0.91 s / 1.0 s |

NHD's afternoon was worse than the overnight probe's baseline (17% 5xx +
12% slow there; 25% 5xx-or-timeout + 32% slow here) — this window caught a
heavy episode, which is exactly the comparison wanted: during it, both
candidates stayed flat at sub-second. Per NHD query, the two hard-fail
Layer 1 queries went: flowlines 10/10 answered but 6 of 10 over 30 s;
waterbodies 7/10 answered, 3 timeouts. The degradable VAA query was the
worst (4 timeouts, 4 slow, 2 fast).

Feature counts were stable across all rounds on all hosts (Esri identical
to NHD on every query; EPA's medium-res fractions as in §1), so the
candidates' speed is not an artifact of returning less.

Caveats stated plainly: one 87-minute window, one vantage point, ~50
requests per candidate. That establishes "not merely different — measurably
flat while NHD was shedding load", not a long-run SLA. Esri's endpoint is
anonymous today; Esri rate-limits and can change anonymous access policy,
and no SLA attaches to either candidate.

## 4. Vintage and currency

| Source | Derived from | Maintained? |
|---|---|---|
| NHD (today's source) | Final NHD snapshot; NHD was retired as of October 2023, final static version published December 27, 2023 | **Frozen.** Successor is 3DHP (same `hydro.nationalmap.gov` host; service updated quarterly). No announced retirement date for the `nhd` MapServer was found. |
| Esri Living Atlas NHDPlus HR | **NHDPlus HR Release 2, February 2025 data** (so *newer* than the frozen NHD snapshot path for VAA); item created May 2026, modified July 2026 | Item states "Update Frequency: Uncertain." A dated predecessor view ("NHDPlus_High_Resolution_9March2023_view") suggests periodic re-publication, not a live feed. Not the frozen-archive trap — but no commitment either. |
| EPA WATERS NHDPlus v2.1 | 2012 snapshot of the **1:100,000** medium-res NHD | The *service* is maintained as EPA's WATERS framework; the *data* is a 13-year-old snapshot of a coarser dataset, permanently. |
| TNM downloads | NHD per-state GDBs frozen at 2023-12-27; NHDPlus HR per-HU4 GDBs 2018–2022 vintage; national NHD GDB republished 2025-09-18 | Frozen like the source; 3DHP downloads are the maintained line. |

## 5. The download-service option, sized

Measured from `tnmaccess.nationalmap.gov` and S3 HEAD requests:

- NHD Best Resolution, **one HU4** (0505, the reference parcel's): **47 MB** zipped FileGDB.
- NHD Best Resolution, **one state**: Pennsylvania **250 MB**, Kentucky 359 MB, Ohio 330 MB, North Carolina 409 MB zipped FileGDB. All ~50 states ≈ 15–20 GB.
- NHD Best Resolution, national: **31 GB** zipped.
- **NHDPlus HR** (needed for stream order and drainage area — the plain NHD GDB does not carry VAA), per HU4: **~280–320 MB** zipped GDB, ~220 HU4s nationally ≈ 60–70 GB.

So: no, there is no subset small enough to bundle the way the storm reports
and physiography map were, even regionally — one HU4 of NHD alone is 47 MB
and the product would need NHDPlus HR's ~300 MB HU4s for the report's
order/drainage numbers. What the sizes *do* support is an on-demand,
server-side HU4 cache (download the parcel's HU4 on first use, S3 transport
is reliable). That is an architecture project — geodatabase readers, spatial
indexing, cache eviction — not a data swap.

## 6. Recommendation

**Ship the in-house fixes; adopt no candidate now.** Fold the Point layer
into Layer 1's existing call, lengthen the pause between attempts past the
episode length the probes measured, and add the per-host circuit breaker.
They cost no licence question and no new dependency, and the overnight
probe — which this investigation runs alongside, not instead of — is the
instrument that says whether they are enough. Nothing found here needs to
preempt that answer.

Per candidate:

- **EPA WATERS NHDPlus v2.1 — recommend against, permanently.** It is the
  licence-clean, reliable candidate, and it fails on the data: 1:100k
  medium resolution returns 1 of the reference parcel's 3 reaches, none of
  the small ponds Layer 1's waterbody query exists to find, and different
  report numbers (stream order 1 vs 2 on the same reach). A candidate that
  silently changes "Montour Run drains N acres" while missing the farm pond
  is not a fallback; it is a different, worse product.

- **Esri Living Atlas NHDPlus HR — recommend against adopting now; it is
  the one worth a licence conversation if one ever opens.** It is the only
  candidate that is *better*, not merely different: identical values on the
  same join keys, a newer NHDPlus HR snapshot (Release 2, Feb 2025) than
  the frozen path USGS serves, flawless in the measured window, and it
  would collapse the flowline and VAA queries into one call. It is blocked
  by exactly one thing, and it is the thing the brief flagged: Esri-owned
  Living Atlas content "may not [be used] for commercial use in your
  application or product" without contacting Esri. Adopting it without an
  agreement would turn the open HIFLD MLA question into an active breach on
  the product's core data path. If the business ever opens an Esri licence
  conversation over HIFLD, this layer is what to add to it — and even
  licensed, it covers 4 of 5 attributes (no springs/seeps layer), so the
  Point query stays on NHD or moves to 3DHP regardless.

- **TNM downloads — not a bundle, keep as the documented escape hatch.**
  No subset is small enough to ship (47 MB per HU4 for NHD alone, ~300 MB
  per HU4 with the NHDPlus HR VAA the report needs). An on-demand
  server-side HU4 cache over S3 would remove the query service entirely and
  carries every attribute including NHDPoint's springs — but it is an
  architecture project (GDB readers, spatial indexing, eviction), justified
  only if the in-house fixes fail the overnight probe *and* the gateway
  keeps degrading. Record it; do not build it.

- **Watch 3DHP, separately from this question.** NHD is frozen and its
  MapServer has no announced retirement date, but the successor — quarterly
  updated, springs included as HydroLocations — lives on the same
  `hydro.nationalmap.gov` host behind the same gateway. The eventual 3DHP
  migration is a data-currency task, not a reliability fix, and nothing
  about it changes the recommendation above.
