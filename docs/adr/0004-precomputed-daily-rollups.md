# Precompute historical aggregates into nightly daily-rollup tables

The dashboard's heavy pages recompute *all history* on every request. Measured
on the live DB (1.67 M Readings, ~209 K added/day), `/api/parks/compare` reads
**1.09 M rows into pandas and takes 7.6–11.6 s** — and `parks.js` re-runs it
every 60 s and on every window toggle; `/api/reliability` scans a park's entire
history on every call (~1.7 s). Both costs grow linearly with retained history:
at 90 days they read ~12 M rows per call. We will **precompute the stable,
historical half of these metrics once a night into small per-day rollup tables,
keep the live/today half computed from bounded raw reads, and merge the two at
request time.**

## Why

Every expensive metric splits cleanly in two:

- **Historical & stable** — crowd-index hour-of-day baselines, week/month/
  historic averages, weekday means, past-window Downtime Rates, weather
  correlations. These do not change during the day, so recomputing them per
  request (and per 60 s refresh) is pure waste that scales with the DB.
- **Live / today** — current wait, today's trace, Momentum, Roster/open/down-
  now, WoW/MoM/YoY. These must stay real-time, but they are *cheap*: bounded to
  today (~1,440 rows/attraction, ~105 K/destination) or to a short window.

A nightly job (one more decoupled cron, in the spirit of
[ADR-0001](0001-decoupled-cron-collector.md)) rolls finalized days up into
`AttractionDaily`, `AttractionHourly`, `AttractionHourBaseline`, `ParkDaily`, and
`ParkCorrelation` — a few thousand rows total per day, ~100× smaller than the raw
fact table. (`AttractionHourly` — per attraction, per date, per hour-of-day wait
sums — is what lets the week/month **Crowd Index** reconstruct both its
hour-of-day baseline and its window numerator without a raw scan.) Requests read
the rollup for prior days, compute the not-yet-rolled **tail** live from raw, and
combine. `parks/compare` drops from ~10 s to well under 1 s and, critically,
**stays flat as history grows** — the per-request read is bounded to the recent
tail, not "all time". Measured on the live DB: `parks/compare` 6.3 s → 0.7 s,
`reliability` 3.4 s → 0.18 s, `day_summary` 3.9 s → 1.7 s.

The rollups live as **tables in the same SQLite file**, not a JSON "calculated
file": they are transactional and WAL-safe alongside the per-minute collector,
they join directly against live today-rows, and they avoid a per-attraction ×
per-window file-key explosion. The daily grain (sum/count, plus per-day
mean/median/std) is chosen so downstream windows reconstruct by summing counts
(Downtime Rate = Σdown ÷ Σreadings; window mean = Σwait ÷ Σn) and the
per-day chart buckets are read directly.

See [../plans/0004-rollup-implementation.md](../plans/0004-rollup-implementation.md)
for the table DDL, the per-endpoint merge math, the one-time backfill, the cron
line, and the rollout order.

## Consequences / trade-offs

- **Self-healing live tail (no morning gap).** The request layer computes live
  not just "today" but **every date after the last rolled one** — so between
  park-local midnight and the nightly run, the just-finished day is filled from
  raw and answers stay correct even if the nightly is late or fails. The tail is
  strictly `> max(rolled date)`, never "any window date missing from the rollup"
  (that mistake live-reads pre-history dates inside a 30-day window and
  double-counts the rolled days — a bug the golden tests caught).
- **Eventual-consistency for prior days.** A finalized day's numbers reflect the
  rollup as of the last nightly. Late `/schedule` fetches or backfilled weather
  can change a recent day, so the nightly **recomputes a trailing window (last 7
  local days), not just yesterday**, upserting idempotently by key — wide enough
  to cover how long the themeparks `/schedule` feed keeps refreshing a recent
  date's Operating Hours.
- **Two sources of truth to keep in step.** The rollup builder must apply the
  exact same Operating-Hours filter and park-local bucketing as the query layer.
  We guard this with golden tests: rollup-plus-today-merge must equal the current
  all-live computation on the existing DB, within rounding, before each endpoint
  is switched over.
- **A known approximation for pooled means.** Park "avg wait" is defined as the
  mean over minutes of the per-minute Park Average. To keep it exactly, we roll
  it up at **park grain** (`park_daily` stores per-day sums of the per-minute
  Park Average and the per-minute top-5 Headliner), not by pooling attraction-
  minutes — so the precomputed value matches the live one.
- **Crowd-Index baseline stays weekday-agnostic**, exactly as today (see
  [ADR-0003](0003-normalized-park-comparison.md)); it simply moves from a live
  90-day scan to a nightly-rebuilt `AttractionHourBaseline`. It stores sum + n
  (not a finished mean) so the request layer can fold today's in-progress hours
  in — the live baseline includes today, and matching it needs the raw
  components. Weekday alignment
  remains the future refinement.

## Considered and rejected

- **Composite index only** (`reading(attraction_id, observed_at)`). Worth doing
  anyway (it speeds the per-attraction paths), but it cannot save `parks/compare`:
  that call legitimately *returns* ~1 M rows for the 90-day baseline, so no index
  removes the row-transfer and pandas cost. Indexing treats the symptom, not the
  linear growth.
- **A nightly precomputed JSON file** (the literal "calculated file"). Simplest,
  but it still needs the today-merge, can't be joined in SQL, must be parsed on
  every request, and explodes into per-attraction × per-window keys. A table is
  strictly better for the same effort.
- **Incremental rollup inside the per-minute collector** (finalize yesterday when
  the local date rolls). Avoids a second cron, but puts a heavier, variable-time
  job on the minute-critical write path that `flock` would then skip — risking
  gaps in the live series exactly when the rollup runs. A separate nightly cron
  keeps the collector lean, per ADR-0001.
- **A separate rollup DB file.** Needs `ATTACH` to join live data and adds a
  second file to back up and keep in WAL; no benefit over new tables in the one
  DB.
