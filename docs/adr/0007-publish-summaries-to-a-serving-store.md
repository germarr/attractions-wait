# Serve the public site from a published Serving Store, not from the collector's database

The dashboard is moving off this box to an Azure Function App running the same
FastAPI application. Rather than give the cloud a copy of the 4.48 M-row
Reading table, **this box computes every result and publishes finished rows into
a Postgres Serving Store that performs no calculation of its own.** Two passes
fill it: a Live Publish at the poll interval and a Daily Publish after the
nightly Rollup.

## Why

The collector's database is 1.55 GB and 4,480,179 raw Readings; every public
answer it produces is kilobytes. `/api/parks/compare` reads roughly a million
rows and returns **949 bytes**. Shipping the raw fact table to the cloud would
move ~500× the data needed to draw the site, and would oblige the cloud to
re-implement the Operating-Hours filter, park-local bucketing, pooled Downtime,
and the Crowd Index — statistics that already exist, correct and tested, in
`app/stats.py`.

So the split is by *result*, not by table. The Serving Store holds ~12 K rows:
per-target live state, finalized daily buckets, weekday means, reliability
windows, park-window comparisons, and correlations. It holds no Readings.
`AttractionHourly` — the largest rollup table at 70,519 rows — never leaves this
box at all, because it exists solely to *compute* the Crowd Index and the
Serving Store publishes that as a single number per park per window.

This extends [ADR-0004](0004-precomputed-daily-rollups.md) rather than replacing
it. That decision already split every metric into "historical & stable" and
"live / today" and merged them at request time. This one moves the merge to
**publish** time and pushes the merged result outward. The Rollup tables stay
exactly as they are and become the Daily Publish's input.

## The live/stable boundary is per-field, not per-endpoint

The endpoint names mislead. `get_day_summary` is documented as "slow-refreshing
bits" yet returns `means.mean`, `downtime.today`, and every `deltas.*` — all of
which move each poll, because a delta is a live value minus a stable one.
`get_day_live` is documented as "fast-refreshing" yet computes `_wow_mom_yoy`
from 7/28/364-day historical scans. The passes therefore classify **fields**:

- **Live** — current Wait Time, Live Trace, Momentum, roster open/down, today's
  Downtime. Cheap; bounded to today.
- **Stable** — Historic means and Downtime Rates, weekday means, the 30-day
  wait/weather correlation. Expensive; computed once a night.
- **Derived** — deltas, WoW/MoM/YoY, Crowd Index with today folded in. Cheap
  *only* because the stable half is read from the Rollup instead of rescanned.

A naive per-endpoint publish is not viable: looping all nine endpoints over all
149 targets measures **530 s**, against a 300 s budget.

**Week and month windows are live, not nightly.** `_window_dates`
(`app/stats.py:1414`) is *today-inclusive*, so a week mean carries today at 1/7
weight and grows through the day; it visibly moves and cannot be published once a
night. Only **Historic** — 55+ days and growing — is stable enough to go nightly.
An earlier draft of this ADR claimed the Daily Publish carried "Week/Month/
Historic means"; that was wrong, and measurement is what caught it.

What makes the live pass fit is a different reassignment: `_wait_weather_corr`
(`app/stats.py:1068`) costs 2.78 s of each 3.36 s `day_summary` call because it
reloads *the same Park's* month of WeatherReadings once per target — seven Parks
fetched 149 times, re-parsing ~8,640 timestamp strings each. It is a 30-day
Pearson correlation, so one partial day moves it invisibly. Moving it to the
nightly pass removes ~83% of the cost and brings the live pass to **~130 s**.

Two endpoints also don't need 149 publishes at all: `get_reliability` is
Park-scoped (7 payloads — it takes an `attraction` argument but returns that
Park's whole table) and `get_park_comparison` is per Destination × window (6).

## One route layer, two readers

`app/main.py` calls exactly nine functions — `get_attractions`, `get_stats`,
`get_series`, `get_recent`, `get_reliability`, `get_day_live`,
`get_day_summary`, `get_destinations`, `get_park_comparison`. That surface *is*
the reader interface, so no new abstraction is invented:

```
app/main.py        routes + Jinja + static   ← byte-identical in both places
   ├─ app/stats.py   computes over SQLite    ← local: dev, fallback, publisher engine
   └─ app/serve.py   SELECTs pub_* from PG   ← cloud: no pandas, no arithmetic
```

Response contracts are preserved exactly, which buys three things: the frontend
JS ships unchanged, the local app on :8005 keeps working as a fallback, and
ADR-0004's golden-test technique applies again — publish-then-serve must equal
today's all-live output before cutover.

Critically, `app/serve.py` must not import pandas. `import app.main` costs 0.82 s
warm and drags 109 MB of pandas/numpy into the package; the cloud reader needs
none of it, and dropping it is what makes a scale-to-zero plan tolerable.

## Measured outcome

As built, against the live database (4.48 M Readings, 149 targets, 7 Parks,
2 Destinations):

| | |
|---|---|
| Live Publish | **41 s** (budget 300 s) |
| Daily Publish | **86 s** |
| Serving Store total | **3.7 MB** (0.24% of the 1.55 GB source) |
| `pub.series_daily` | 8,642 rows, every finalized day |
| `/api/parks/compare` | **5 ms** served, was ~700 ms |
| `import app.main` | **0.37 s** cloud reader vs 0.74 s local |
| Golden test | 0 diffs across 13 targets x 8 endpoints |

Two implementation decisions were forced by measurement and are worth recording:

- **`get_day_summary` grew an additive `include_slow` flag.** The live pass needs
  only its intraday half, and duplicating the means/deltas arithmetic in the
  publisher would have recreated exactly the two-sources-of-truth problem this
  ADR warns about. With `include_slow=False` the call drops from 3.36 s to
  **0.020 s** and returns a byte-identical live half. This is why `app/stats.py`
  was not left untouched.
- **`compare` is stored decomposed, not whole.** It is 21,898 of the endpoint's
  22,627 bytes, but its three reference traces are fixed historical days; only
  `today` moves. Publishing them nightly as `{"HH:MM": wait}` maps and
  re-projecting at read time cut live write volume from **1.29 GB/day to
  0.33 GB/day**. `app/serve.py` rebuilds the block from today's already-published
  trace — list indexing, not statistics.

## As deployed

`attractions-dashboard` — Flex Consumption, Python 3.12, East US, resource group
`miscellaneous_projects`, scale-to-zero (`alwaysReady: []`), HTTPS-only.
Live at https://attractions-dashboard.azurewebsites.net.

The deployed package is **15 files, 35 KB** — `function_app.py`, `host.json`,
`requirements.txt`, four `app/*.py` modules, three templates, five static assets.
`app/stats.py`, `db.py`, `models.py`, and the collector/rollup/retention/publish
modules are excluded outright, and CI fails the build if any of them, or an
import of pandas/numpy/sqlmodel/sqlalchemy, reaches the package.

Two limits were set deliberately from measurement, not defaults:

- **`maximumInstanceCount` 100 → 8.** The Serving Store shares a Burstable
  `Standard_B1ms` server (resource group `gerardoma_personal_site`) with the
  personal site and `football_prod`. It has **`max_connections = 50`**, ten of
  them superuser-reserved, and ~16 already in use. At the default 100 instances
  and a pool of 4, the Function App alone could have demanded 400 connections
  and taken the server down for its other tenants. Eight instances × `PG_POOL_MAX=2`
  caps it at 16.
- **`perInstanceConcurrency` explicitly 16.** Left at its default, twenty
  sequential requests fanned out across eight instances — each paying a cold
  start — and produced a 5.2/10.2/15.2/30.5 s latency ladder. The app is
  I/O-bound on ~60 ms primary-key lookups, so one instance should absorb many
  concurrent requests rather than triggering scale-out. Warm latency after the
  change: **p50 67–115 ms, max 129 ms, zero requests over 1 s.**

This also confirms Q7's premise the hard way: **PgBouncer is unavailable**, because
Azure offers it only on General Purpose and Memory Optimized tiers, and this
server is Burstable. Application-level pooling plus a tight instance cap is not a
preference here, it is the only option that does not involve a tier upgrade.

## Consequences / trade-offs

- **The site can serve stale data while returning 200.** Under FastAPI-over-
  SQLite a successful fetch implied fresh data. It no longer does: a failed
  Publish leaves the last good rows in place and the Function serves them
  happily. Every pass therefore writes a **Watermark**, and the frontend badge
  moves off `stamp()`'s browser clock (`day.js:19`) onto the real observed-at.
  Without this the site would claim "● live" over arbitrarily old numbers.
- **Series are stored by lifecycle, not uniformly.** Rolling, whole-array-read
  series (Live Trace, today's hourly buckets) are one row per target with a
  `jsonb` array; append-only, range-sliced series (finalized daily buckets) stay
  one row per day. Uniform per-point rows would mean ~5,400 row-writes every
  five minutes — 1.55 M/day — to rewrite a window that mostly shifts rather than
  changes, and the site reads those arrays whole regardless.
- **All finalized days are published, not just the charted 30.** ~8 K rows now,
  ~55 K/year. Deliberately more than the minimum: at daily grain the saving from
  pruning is negligible, while a longer chart window later becomes a frontend
  change instead of a schema migration and backfill. The real saving was
  dropping the raw Readings.
- **Cold start on the first visit after idle: measured 2.23 s.** Flex Consumption
  scales to zero; after twelve minutes of forced idle the first request to `/`
  took 2.23 s, the next 0.08 s, and an API call 0.24 s. Every page polls at 60 s,
  so any open tab keeps the app warm and only the first visitor after a gap pays.
  Accepted over paying for always-ready instances to remove a 2 s delay on data
  that changes every five minutes.
- **No CDN for static assets.** SWA would have hosted the 58 KB of CSS/JS free
  at the edge; the Function App serves them itself as billed invocations.
  Mitigated by `Cache-Control` and the existing `asset_v` busting; revisit with
  Front Door only if traffic ever justifies it.
- **The database is reachable from the whole internet, pre-existing.** The
  server carries an `AllowAll` firewall rule spanning `0.0.0.0`–`255.255.255.255`
  from 2023, alongside the `AllowAllAzureServicesAndResources` rule the Function
  App actually needs. Not introduced by this work and not removed by it — the
  personal site and `football_prod` may depend on it — and still worth closing
  separately.
- **The Function App holds a SELECT-only credential.** It authenticates as
  `pub_reader` (see [../../sql/pub_reader_role.sql](../../sql/pub_reader_role.sql)),
  which can read the `pub` schema and nothing else: writes, DDL, and `pg_authid`
  are all denied, and `CREATE` on `public` was revoked from `PUBLIC` in this
  database. This matters more than usual here because the app is internet-facing
  and the server is shared with unrelated databases. The publisher on the
  collector host keeps a separate writing credential — a private box, not an
  exposed one. Residual: PostgreSQL grants `CONNECT` to `PUBLIC` on every
  database, so `pub_reader` can occupy a connection slot on `football_prod`
  while reading nothing from it; revoking that would risk the other tenants.
- **Two sources of truth to keep in step, again.** ADR-0004 raised this for
  rollup-vs-query; the same discipline now extends to publish-vs-serve, guarded
  the same way — by golden tests before cutover.

## Considered and rejected

- **Azure Static Web Apps with JavaScript Functions.** The original plan. It
  forced a language split: Python computing here, JavaScript reassembling the
  same nine contracts in the cloud — the same statistics maintained twice, with
  no golden test possible between them. A Python Function App running the actual
  FastAPI app erases the split. SWA's free edge CDN was the one real loss.
- **Mirroring the Rollup tables and letting the cloud compute.** Keeps Postgres
  general and queryable, but re-implements the Operating-Hours filter, pooled
  Downtime, and Crowd Index in a second codebase, and requires shipping today's
  raw Readings (~41 K rows/day) as well.
- **Opaque response documents** — one `jsonb` blob per (endpoint, params) key.
  Maximally dumb in the cloud, but the Serving Store becomes unqueryable and
  every shape change needs a full republish to inspect.
- **Capping published history at the 30-day charting horizon.** Saves ~4 K rows
  and forecloses any longer window without a migration. Not worth it.
- **App Service B1 with Always On.** Honestly the most natural host for a Jinja-
  rendering FastAPI app — no ASGI shim, no cold start. Rejected at ~$13/mo flat
  for a workload that is idle most of the day and warm whenever anyone is
  looking at it.
- **Premium plan with VNet + NAT Gateway** for a static egress IP, so Postgres
  could allowlist exactly two addresses. Reverses the hosting decision and adds
  ~$32/mo of pure networking to serve 12 K rows.

## Deployment identity

CI deploys with an Entra app registration (`github-attractions-wait-deploy`,
appId `4fcf90ae-5420-4c4a-9912-2a20c7a9d6b7`) using **OIDC federated
credentials** — no publish profile or client secret is stored anywhere. The
credential is bound to exactly one subject:

    repo:germarr/attractions-wait:ref:refs/heads/main

so only a push to `main` (or a `workflow_dispatch` on `main`) can obtain a token;
a fork, a pull request, or any other branch cannot.

The role is **Website Contributor scoped to the Function App resource**, not
Contributor on the resource group. Verified against the CLI's actual Flex
deployment path (`enable_zip_deploy_flex`), which reads only the site's SCM URL
and runtime config and then POSTs to the SCM endpoint — it never touches the
server farm, so nothing broader is required. The practical consequence is that a
compromised workflow can redeploy this Function App and nothing else: not the
storage account, not the App Service plan, and not the unrelated
`YoutubeTrendingDashboard` sharing the resource group.
