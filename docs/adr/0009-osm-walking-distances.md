# Route walking distances over OpenStreetMap, and materialize them

> **Amends [ADR-0008](0008-attraction-coordinates.md).** That ADR's reasoning —
> "n coordinates already encode all n(n−1)/2 distances, so storing a distance
> table is redundant" — holds for *straight-line* distance and only for it. This
> ADR adds a second metric whose economics are the reverse, and materializes it.

ADR-0008 closed by naming its own limitation: haversine gives great-circle
distance, guests walk paths, and TRON → Tiana's is 684 m in a straight line but
does not go through the castle. We **route walking distance over an
OpenStreetMap footpath graph and store the result in `attractiondistance`**.

## A correction factor does not exist

The obvious cheap fix is to scale the straight line by a constant. Routing all
462 viable Magic Kingdom pairs shows why that fails:

| | walk ÷ straight line |
|---|---|
| min | 1.04× |
| p10 | 1.15× |
| **median** | **1.26×** |
| p90 | 1.54× |
| max | **2.24×** |

The maximum is the instructive one: **Big Thunder Mountain → Haunted Mansion is
174 m apart and 391 m to walk**, because Rivers of America is between them. That
error is structural, not scalar — no multiplier recovers it.

## Why not Google Maps

Google would route on real walkways. The blocker is that **you may not keep the
answer.** From the Google Maps Platform Service Specific Terms, §19.3 (Routes
API):

> Customer may temporarily cache **latitude (lat) and longitude (lng) values**
> from the Routes API for up to 30 consecutive calendar days…

That permits caching *coordinates*. It does not mention distance or duration —
and the omission is deliberate, because §11.8 (Navigation Connect API) shows
Google enumerating exactly those fields where it does intend to permit them
("latitude (lat), longitude (lng), **distance, duration, time**…"). §5 (Distance
Matrix API) carries no caching clause at all. Since the master terms prohibit
caching except as expressly permitted, a permanent Google-derived distance table
is not something the licence grants.

Cost is the lesser problem — Compute Route Matrix bills per element (origins ×
destinations) at roughly $5–8 per 1,000, so 1,856 within-park pairs is about
$9–15, free under the $200 monthly credit — but it would recur indefinitely,
because the results cannot be retained. §19.2 additionally forbids using Routes
content alongside a non-Google map, which would foreclose ever pairing these
numbers with a Leaflet map.

OpenStreetMap is ODbL: storable permanently with attribution, free per query,
and routable offline from a cached extract.

## Why materializing is right here, having been wrong there

A routed distance is not recoverable from the two endpoints. Producing one means
fetching an extract, building a graph of thousands of nodes, and running
Dijkstra — seconds per park, against the ~0.03 ms of a haversine. So the
ADR-0008 trade-off inverts, and the O(n²) table earns its place: 1,856 rows,
rebuilt only when the roster or the extract changes, which is rare. This is
still not the [ADR-0004](0004-precomputed-daily-rollups.md) nightly pattern —
there is nothing daily about it.

Straight-line distance is deliberately **not** stored alongside the walk. It is
one haversine over columns we already have, and ADR-0008's rule stands: cheaply
derivable values do not earn a column. `stats.walking_pairs()` computes it, and
the Detour Ratio, at query time.

## Three findings that decide the mechanism

All three were measured, and all three were wrong in an earlier implementation.

**Snap into one component, not to the nearest node.** Snapping each ride to its
globally nearest graph node strands rides on disconnected forecourt stubs. The
first build routed **11 of Universal Studios Florida's 276 pairs (4%)**.
Snapping every ride into a single connected component instead takes it to
**100%** — as it does for every park: all 1,856 within-park pairs now route,
with a median snap of 7–25 m depending on the park.

**Choose that component by fit, not by size.** The largest component at
Universal Studios Florida is off-property sidewalk; snapping the rides to it
leaves them a median 202 m from their own entrances. Scoring candidate
components by *median snap distance* selects the in-park guest network instead.

The way set matters for the same reason. `footway|path|pedestrian|steps` alone
fragments Universal Studios Florida into 162 components, because the paths
joining its themed areas are tagged `service` or `unclassified`. Including those
takes the median snap from 202 m to 25 m **without changing any Magic Kingdom
route** — TRON → Tiana's stays 877 m either way, which is the control that says
the wider set is not inventing shortcuts. Major roads
(motorway/primary/secondary/tertiary) stay out.

**Give each ride a node, rather than minimising over candidate pairs.** A ride's
nearest path node is frequently the wrong access point: Gran Fiesta Tour sits
inside the Mexico pavilion, and its closest node can be a service path behind the
building while the real entrance is on the plaza — sending the route the long way
around. Offering several candidates fixes that, but *how* you combine them
matters. Taking the shortest total over every candidate pair is the obvious
approach and it is wrong: `d(a,b)` and `d(b,c)` may enter and leave `b` at
different candidates, so the result violates the triangle inequality — measurably,
by up to 556 m at Universal Studios Florida.

Splicing each ride into the graph as its own node, joined to each candidate by an
edge weighted with the straight-line walk to it, gives the same flexibility as a
genuine shortest path. Distances are then symmetric and metric by construction,
and the two walk-to-path legs, being ordinary edges, also make a result shorter
than the straight line impossible — the measured minimum ratio is exactly 1.00,
where node-to-node distance had produced physically impossible ratios as low as
0.71. `tests/test_walking.py` asserts both properties.

Queue paths are excluded: OSM maps them ("Standby Queue", "Lightning Lane
Queue"), and a router will otherwise send a guest up the Haunted Mansion standby
line and out the exit.

## Mechanism

- **`app/walking.py`**, run on demand, not on a schedule: `--rebuild` for every
  park, `--park <uuid>` for one, `--refresh-osm` to re-download.
- **Extracts cached** under `data/osm/` (gitignored, on the big disk). The query
  fetches all of `highway=*` and filters in code, so changing `WALKABLE_HIGHWAYS`
  is a rebuild against cache rather than new Overpass traffic. The cache name
  carries `OSM_CACHE_VERSION` so a stale extract is never silently reused.
- **Schema**: `attractiondistance`, one row per unordered pair keyed
  `attraction_a_id < attraction_b_id`; `stats._WALK_BOTH_WAYS` unions the mirror
  image for origin-anchored queries.
- **Queries**: `stats.walking_distance_between`, `nearest_walking`,
  `walking_pairs`, `walking_withheld`. `walking_distance_between` returns `None`
  rather than falling back to the straight line — a caller that silently received
  684 m where it asked for the 877 m walk would have no way to tell.

## Limitations

- **OSM gaps are withheld, not published.** Where the walkway between two rides
  is unmapped, the router goes the long way around: Villain-Con Minion Blast and
  Jack & Oddfellow are 70 m apart and routed 1,187 m. Returning that number is
  worse than returning nothing, because nothing about it looks wrong to a caller.
  So a pair whose route is implausible for its straight-line distance is stored
  with **`walk_meters` NULL**, and every query filters those out — a withheld
  pair and an unconnected one both read as "we don't know".

  The row is kept rather than dropped for two reasons: the rejection stays
  queryable via `stats.walking_withheld()` without re-running a build, and a
  park's row count still equals n(n−1)/2, which is the invariant that catches
  rides stranded on disconnected path stubs. **30 of 1,856 pairs (1.6%)** are
  withheld, and they are not evenly spread:

  | Park | pairs | suspect (before → after) |
  |---|---|---|
  | Magic Kingdom | 595 | 0 → 0 |
  | Disney's Hollywood Studios | 55 | 0 → 0 |
  | Disney's Animal Kingdom | 105 | 0 → 0 |
  | Universal Epic Universe | 66 | 4 → 0 |
  | EPCOT | 528 | 22 → 1 |
  | Universal Islands of Adventure | 231 | 7 → 3 |
  | Universal Studios Florida | 276 | 32 → 26 |

  What remains is an upstream data gap, not an algorithmic one, and it is
  concentrated: 26 of the 30 are at Universal Studios Florida, on the Minion /
  Springfield side of the park. The specific defect was traced: the walkways
  there *are* mapped, but as polygons on small disconnected islands (12, 39 and
  143 nodes) sitting **42–62 m from the routable network**. Villain-Con's nearest
  reachable node is 91.8 m away even though a mapped way passes 1.3 m from Jack &
  Oddfellow.

  Three repairs were tried against this and all three rejected: adding way types
  (already included), making pedestrian areas crossable (no change whatsoever),
  and joining nearby components by proximity (no gain at 2 m, and at 5–10 m it
  *broke* Magic Kingdom, pushing its worst ratio to 94×). A 50 m gap cannot be
  bridged by inference. Fixing it means mapping those connecting walkways in
  OSM — traceable from the aerial imagery licensed in OSM editors, and findable
  with Osmose or the JOSM validator, which flag this exact class. Once mapped,
  `--refresh-osm --rebuild` picks it up with no code change.

- **Flagging uses absolute excess as well as ratio.** On a short pair the ratio is
  dominated by its denominator: Bruce's Shark World and SeaBase Aquarium are 3 m
  apart and route 14 m, which is a 4.2× ratio and entirely correct. A pair is only
  suspect when the detour is large in metres too (`SUSPECT_EXCESS_M`).
- **Distance is not time.** Crowds, strollers, and parade closures dominate walk
  time and none of them are in this graph.
- **Within-park only.** Cross-park pairs are absent by construction; walking
  between parks is not a thing a guest does.
- **Attribution.** OSM data is ODbL — anything published from it must carry
  "© OpenStreetMap contributors".
