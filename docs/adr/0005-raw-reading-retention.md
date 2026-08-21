# Bound raw-reading growth with a nightly retention prune

> **Amended by [ADR-0006](0006-five-minute-poll-interval.md) (2026-08-04).**
> "Raw" is no longer per-*minute*: the collector polls every 5 minutes, so the
> steady-state figures below (~200 K rows/day, ~1.2 GB) fall by ~5×, and
> references to "per-minute rows" should be read as "per-poll rows". The 35-day
> horizon and the fail-safe gate are unchanged.

The `reading` fact table grows ~200 K rows/day (~50 MB/day) with no ceiling. On
2026-07-03 it held 1.71 M rows = 141 MB of data plus **278 MB of indexes** (≈2×
the data), and the file was 416 MB after only 9 days. Left alone it grows
linearly forever. We **cap raw retention at 35 park-local days and delete older
raw**, because [ADR-0004](0004-precomputed-daily-rollups.md)'s nightly rollups
are already a permanent, aggregated store of everything historical.

## Why 35 days is safe

The rollups (`AttractionDaily`, `AttractionHourly`, `ParkDaily`,
`AttractionHourBaseline`, `ParkCorrelation`) back **every historical chart**, so
deleting raw only matters where the query/rollup layer still reads raw rows. An
audit of `app/stats.py` + `app/rollup.py` found the deepest raw reaches are:

- **7 days** — the nightly rollup's trailing re-roll (`run_nightly`).
- **28 days** — the Day-page overlay's same-day-of-week minute trace
  (`_compare_traces`).
- **30 days** — the nightly weather correlation (`rebuild_correlations`).
- Everything older (all windows, reliability, crowd index, weekday means) reads
  **rollups only, never raw.**

The one exception is **YoY** (`_wow_mom_yoy`, a 364-day reference hour). YoY is
not live yet (it returns `None` until a year of history accrues) and is a
documented future refinement; when it ships it must read `AttractionHourly`, not
raw — keeping a year of per-minute raw solely to feed one reference hour would
defeat this ADR. So 35 days clears every **active** raw horizon with a ~5-day
margin, and only defers not-yet-live YoY.

## Mechanism

- **Prune job** (`app/retention.py`, `run_prune`): a decoupled daily cron in the
  spirit of [ADR-0001](0001-decoupled-cron-collector.md), scheduled at **06:45
  UTC — after** the 06:15 rollup, under its own `flock`. It deletes `reading` and
  `weatherreading` rows with `observed_at` older than `RETENTION_DAYS` (35)
  park-local days.
- **Fail-safe gate:** the prune is a **no-op** unless the rollup is present and
  fresh (`max(AttractionDaily.date)` within `MAX_ROLLUP_LAG_DAYS = 3`). A
  missing/late/failed nightly therefore never deletes raw the rollups haven't yet
  absorbed. The cutoff (35 d) also sits far outside the 7-day re-roll window, so
  a day still being recomputed is never pruned.
- **Index diet (one-off + durable):** dropped the redundant single-column
  `ix_reading_attraction_id`. The composite `ix_reading_attr_observed`
  `(attraction_id, observed_at)` leads with `attraction_id`, so it serves every
  `attraction_id` lookup on its own (verified: attraction-only queries use it as
  a *covering* index). Removes ~86 MB and one index write per insert. The
  `index=True` was removed from `Reading.attraction_id` in `models.py` and a
  `DROP INDEX IF EXISTS` added to `db._migrate` so it stays dropped.

## Consequences / trade-offs

- **Bounded, flat steady state.** Raw plateaus at ~35 days (~7 M rows). Because
  the collector keeps inserting, deleted pages are *reused* rather than needing a
  `VACUUM` — the file grows to the 35-day working set (~1.2 GB) and then holds.
  The prune ends with `wal_checkpoint(TRUNCATE)` so the WAL doesn't balloon after
  a large delete.
- **No behavior change.** Every currently-active metric is unaffected; only
  not-yet-live YoY is deferred to a future rollup-fed implementation.
- **Raw history is irrecoverable past 35 days.** Accepted: the rollups retain the
  daily/hourly aggregates permanently; only per-minute resolution older than 35
  days is lost. Anyone needing longer per-minute history must widen
  `RETENTION_DAYS` (and pay the linear storage) before the first prune reaches
  those days.

## Considered and rejected

- **A new weekly-average table.** The daily/hourly rollups already are the
  permanent downsample, at finer grain; a weekly table would be coarser and
  redundant.
- **Aggressive ~14-day retention** by re-pointing WoW/MoM/YoY and the weather
  correlation at the rollups (option B in the plan). ~350–450 MB steady state,
  but it degrades the 28-day Day-page overlay from minute to hourly resolution
  and needs query changes + golden tests. Deferred; revisit if 1.2 GB proves too
  large.
- **Change-only / delta storage** (store a reading only when wait/status
  changes). Largest possible reduction, but it breaks the "one Reading per poll
  minute" model (CONTEXT.md): Downtime Rate denominators and `n_readings` assume
  per-minute rows. Too invasive.
- **Integer epoch `observed_at`** instead of ISO8601 text. Would roughly halve
  row + index bytes, but it's a schema migration touching every string-time
  comparison in the query layer. A separate future option, orthogonal to
  retention.

See [../plans/0005-raw-reading-retention.md](../plans/0005-raw-reading-retention.md)
for the full audit and the rejected aggressive variant.
