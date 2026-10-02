# 3DHP: can it replace NHD under the Water section? (branch 31, step 0)

Assessment only; the migration was **not** started. Everything below was
verified live against `hydro.nationalmap.gov/arcgis/rest/services/3DHP_all/
MapServer` on 2026-10-02 (service version 11.3, "Data refreshed September 4,
2026"), with the reference parcel's own queries -- bbox + 150 m and bbox + one
mile, the boxes `hydrology_data` and `context_map_data` use -- issued against
NHD, NHDPlus HR and 3DHP side by side.

## The verdict

**3DHP cannot supply total upstream drainage area, and it cannot supply
permanence. NHDPlus HR stays, and so does NHD.** The phase is stopped per the
branch brief ("if 3DHP cannot supply stream order and drainage area, say so
and stop that phase"). It supplies one of the two.

| What the product uses | Carried by 3DHP? | Verified how |
|---|---|---|
| Flowline geometry | **Yes.** Layer 50 `Flowline`. 3 reaches at 150 m, 48 at one mile -- the same counts as NHD, the same `lengthkm` to the metre (1.7361 km / 22.3142 km summed), coordinates differing only in the last digits. | counts, lengths and geometry diffed |
| FCode / permanence (perennial, intermittent, ephemeral) | **No.** 3DHP's flowline `featuretype` is a *kind of line* -- Channel Line, Waterbody Connector, Canal, Drainageway, connectors -- not a hydrographic category. There is no FCode, no `hydrographiccategory`, nothing that separates Montour Run (NHD 46006, perennial) from its unnamed tributary (NHD 46003, intermittent); both read "Channel Line". | layer 50 field list and the three reaches' attributes |
| Waterbody geometry | **Yes.** Layer 60 `Waterbody`: 14 at one mile, the same 14 as NHD, with `areasqkm` and `featuretype` Lake/River/Canal (NHD's 39004 reads "Lake"). | counts and geometry diffed |
| Springs and seeps (NHD Point, FCode 45800) | **Yes.** Layer 20 `HydroLocation - Sink, Spring, Waterbody Outlet`, `featuretype` 7 = Spring. Zero at the reference parcel on both services; on a springs-rich Ozark bbox NHD returns 29 FCode-45800 points and 3DHP 29 Springs with the same GNIS names. | a second bbox, by name |
| **Strahler stream order** | **Yes.** `streamorder` is ON the flowline (with `streamlevel`, `streamcalculator`, `arbolatesum`, `hydrosequence`): populated for all 48 reaches, and reach for reach **identical to NHDPlus HR** (Montour Run 2, 2; the tributary 1; stream level 4, 4, 5; arbolate sums 2.407 / 3.401 / 0.493 km). | values compared per reach |
| **Total upstream drainage area** | **No.** Not on the flowline. It lives on layer 80 `Catchment` as `totestdrainareasqkm`, and catchments exist only where 3DHP has elevation-derived hydrography (EDH). The reference parcel has **0 catchments** and every reach has `catchmentid3dhp` empty and `workunitid = NHD`; a Vermont bbox tried as a second sample read the same (331 flowlines, 0 catchments). The service description says catchments and drainage areas "will be populated in the future" as EDH replaces NHD. | layer 80 queried at both bboxes; flowline `catchmentid3dhp` |

## Vintage

Where the parcel sits, 3DHP *is* NHD: every reach carries `featuredate`
2023-09-14 and `workunitid = NHD`, i.e. the frozen final NHD snapshot
re-attributed into the 3DHP schema. The service's "refreshed September 4,
2026" is the service, not this data. Migrating would change the data
currency of the reference parcel's figures by nothing, and the same holds
for every parcel in an area EDH has not yet reached.

## Schema differences from NHD, and the join

* **Identifiers.** NHD's `permanent_identifier` (`123973372`) is replaced by
  `id3dhp` (`68L3`); 0 of 48 reaches share an id between the two. 3DHP's
  flowlines carry no `reachcode` either. **The existing join does not
  survive**: `nhdplus_data.parse_flowline_attributes` keys NHDPlus HR's
  `totdasqkm` by `permanent_identifier`, which a 3DHP row cannot supply.
  A migrated flowline fetch would need a geometry-based match (same
  `gnisid` + `lengthkm`, or nearest geometry) to pull drainage area off
  NHDPlus HR -- a new, fragile join to carry a figure NHD gives for free.
* **Field names.** `gnis_name` -> `gnisidlabel`, `gnis_id` -> `gnisid`
  (integer), `fcode`/`ftype` -> `featuretype` + `featuretypelabel` (a
  different vocabulary, above), `lengthkm` and `areasqkm` kept, `reachcode`
  gone, `mainstemid` (a geoconnex URI) added.
* **Casing.** Lower case throughout; NHD's Point layer was upper case.
* **Layer ids.** 50 / 60 / 20 for 6 / 12 / 0.

## What a partial migration would buy

Nothing the report needs today:

* **Stream order** is the one attribute 3DHP carries that NHD does not, and
  the report already has it, identically, from the NHDPlus HR query that
  must stay for drainage area anyway.
* **Springs** could move to HydroLocations, but the Point query is already
  folded into Layer 1's water fetch and costs no extra request.
* **Permanence** would be lost outright, and the Water section's permanence
  table, the stream-length-by-permanence figures and the methods note all
  depend on it.

## Recommendation

Defer. Re-check when `3DHP_all` reports catchments (`totestdrainareasqkm`)
for the parcel's work unit and a permanence attribute on the flowline; until
then NHD at 1:24,000 plus NHDPlus HR is the only pairing on this host that
carries all six things the section prints, and the citations stay as they
are.
