# Store Attraction coordinates, not a distance matrix

> **Amended by [ADR-0009](0009-osm-walking-distances.md) (2026-09-15).** The
> "no distance table" decision below holds for *straight-line* distance, whose
> economics it argues from: n coordinates encode all n(n−1)/2 haversines, and
> recomputing one costs microseconds. ADR-0009 adds walking distance, which is
> routed over an OpenStreetMap footpath graph, is not recoverable from the two
> endpoints, and therefore *is* materialized. The limitation this ADR closes
> with is what prompted it.

The dataset could answer "how far is TRON from Tiana's Bayou Adventure?" only at
Park grain: `park` carried `latitude`/`longitude`, `attraction` did not. We
**store two floats per Attraction and compute distance on demand**, rather than
materializing the pairwise distances into a table.

## Coordinates cost nothing to collect

`/live`, the payload every poll already reads, carries **no** location block. But
`/entity/{destinationId}/children` — which `collector._ensure_geo` (formerly
`_ensure_parks`) already calls to resolve Park names and coordinates — is
**recursive**: for Walt Disney World it returns 504 children, of which 6 are the
Parks we kept and **123 are Attractions, every one geocoded**. We were fetching
the coordinates and discarding them.

So geocoding the roster added **zero API calls**. Coverage is complete: 152 of
152 Attractions in the live roster. The single ungeocoded row is a ghost the
feed no longer lists, which `IS NOT NULL` guards exclude from every pair query.

## Why no distance table

A distance table is O(n²) derived data reconstructible exactly from O(n) source.
For 152 Attractions that is 1,856 within-Park pairs (11,476 including
cross-Park) against 152 coordinate pairs — and the table would need invalidating
every time a ride opens, closes, or moves between Parks.

It would buy nothing, because the computation is three trig calls. SQLite ships
the math functions from 3.35 (the host runs 3.45), so the haversine is a plain
SQL expression over two aliased `attraction` rows. Measured on the real roster:

| Query | Rows | Time |
|---|---|---|
| Nearest 5 to one Attraction | 5 | **0.03 ms** |
| Every within-Park pair | 1,856 | 6.9 ms |
| Every pair, cross-Park included | 11,476 | 35 ms |

This is deliberately **not** the [ADR-0004](0004-precomputed-daily-rollups.md)
rollup pattern. Rollups earn their storage by collapsing millions of Readings
into a few thousand rows, turning a 6.3 s page load into 0.7 s. Here the
"expensive" computation is arithmetic on 152 points, so precomputing would add
an invalidation burden to save microseconds. Storing the coordinates *is*
storing every distance — n points encode all n(n−1)/2 of them, losslessly, in
2.4 KB instead of ~90 KB.

Keeping it on-demand also leaves the within-Park vs. cross-Park question open
rather than baking one answer into a schema: `all_pairs()` scopes with a
`WHERE` clause.

## Mechanism

- **Schema** (`app/models.py`): nullable `latitude`/`longitude` on `Attraction`,
  mirroring `Park`; `geo_fetched_at` on `Destination`. Both added by
  `db._migrate` for the existing file.
- **Seeding** (`collector._ensure_geo`): one `/children` call per Destination
  updates Park rows and the coordinates of Attractions we already have. It
  ignores the water-park and resort-area Attractions the poll loop never stores.
- **Refresh is time-based, not missing-data-based** (`GEO_REFRESH_SECONDS`, 6 h).
  Triggering on "some Attraction lacks coordinates" would make any ride the feed
  never geocodes re-fetch `/children` on *every* poll, forever. A NULL
  `geo_fetched_at` forces the initial seed, which is what backfilled the existing
  roster; afterwards a newly-opened ride is geocoded within 6 hours. Coordinates
  are static, so that latency costs nothing.
- **Queries** (`app/stats.py`): `distance_between`, `nearest`, `all_pairs` share
  one `_HAVERSINE_M_SQL` constant; `haversine_m` is the Python equivalent for
  callers holding raw coordinates. `tests/test_geography.py` pins the two
  implementations together and checks the formula against an independent
  reference (one degree of latitude = 111,195 m).

## Limitation: straight-line, not walking distance

Haversine gives great-circle distance between two points. Guests walk paths.
TRON → Tiana's is 684 m as the crow flies but routes around the hub, not through
the castle — and it is the *farthest* pair in Magic Kingdom, so the error is
largest exactly where the number is most likely to be quoted. This is adequate
for "what is near me" and for clustering, and **must not** be presented as a
walking time. Doing that properly needs path graph data the feed does not carry
— which is what [ADR-0009](0009-osm-walking-distances.md) sources from
OpenStreetMap.
