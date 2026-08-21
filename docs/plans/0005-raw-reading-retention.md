# Raw-reading retention & storage reduction

Status: **IMPLEMENTED (option A, 35 days)** — 2026-07-04. See
[../adr/0005-raw-reading-retention.md](../adr/0005-raw-reading-retention.md).
Author: planning session 2026-07-03.

## Problem

The `reading` fact table grows ~200 K rows/day (~50 MB/day) unbounded. On
2026-07-03 the DB was 416 MB + a 121 MB stale WAL, of which the raw `reading`
table is essentially the whole cost: 1.71 M rows = **141 MB data + 278 MB of
indexes** (indexes are ~2× the data they cover). Growth is linear and has no
ceiling.

## Key insight — the rollups are already the "downsample" store

ADR-0004's nightly rollups (`AttractionDaily`, `AttractionHourly`, `ParkDaily`,
`AttractionHourBaseline`, `ParkCorrelation`) are a permanent, aggregated store —
per-day and per-hour, finer than weekly — and they already back **every
historical chart**. So we do **not** add a weekly-average table. Reducing
storage = **deleting the raw `reading` rows the rollups have already absorbed.**
The only constraint is how far back anything still reads raw.

## Audit — how far back raw is actually read

Traced through `app/stats.py` and `app/rollup.py`. Raw `reading` /
`weatherreading` rows are read only by:

| Consumer | Deepest raw reach | Source |
|---|---|---|
| Live Trace / Momentum / today cards | last 90 min / today | `get_recent`, `get_stats` |
| Nightly rollup recompute (`trailing_days=7`) | **7 days** | `rollup.run_nightly` |
| Day-page overlay `_compare_traces` (WoW/MoM full-day minute trace) | **28 days** | `stats._compare_traces` |
| Nightly weather correlation | **30 days** | `rollup.rebuild_correlations` |
| WoW / MoM / **YoY** reference-hour mean | 7 / 28 / **364 days** | `stats._wow_mom_yoy` |
| All windowed history, reliability, crowd index, weekday means | **rollups only — never raw** | merged endpoints |

**The cliff:** past ~30 days nothing reads raw **except YoY** (`_wow_mom_yoy`,
364-day reference hour). YoY currently returns `None` (DB is 9 days old) and is a
documented future refinement. Retaining 365 days of raw solely to feed YoY would
defeat the goal; when YoY ships it should read `AttractionHourly`, not raw.

## Plan

### 1. Index diet — instant reclaim, no code, no risk
`ix_reading_attraction_id` (single column `attraction_id`) is fully redundant
with the composite `ix_reading_attr_observed` `(attraction_id, observed_at)` —
the composite serves every `attraction_id`-only lookup. **Drop it**: reclaims its
share of the 278 MB index bloat (~86 MB order-of-magnitude) and speeds every
insert. Worth doing independent of the retention choice.

### 2. Retention prune — the main lever
New decoupled cron (per ADR-0001/0004 style), run **after** `run_nightly`
succeeds:
- `DELETE FROM reading WHERE observed_at < :cutoff` and same for
  `weatherreading`.
- **Safety gates:** cutoff must be strictly older than the nightly's 7-day
  re-roll window; only prune dates that already have rollup rows; never prune the
  in-progress local day or anything the live tail (`_combined_daily_map`) may
  still recompute. Prune is a no-op if the nightly hasn't run.

### 3. Reclaim disk after deletes
`DELETE` does not shrink SQLite. Enable `PRAGMA auto_vacuum=INCREMENTAL` (needs
one `VACUUM` to activate) + run `PRAGMA incremental_vacuum` in the prune job, and
keep `PRAGMA wal_checkpoint(TRUNCATE)` (validated 2026-07-03: 121 MB WAL → 0).
Steady-state file size then stops growing.

## Open decision — retention window (sets size & code scope)

- **A. 35 days, no code changes (RECOMMENDED).** Covers 30-day correlation +
  28-day overlay + 7-day re-roll with margin. Every active feature keeps working
  unchanged; only sacrifices not-yet-live YoY. Steady state ~1.2 GB, flat.
- **B. ~14 days, re-point old reads to rollups.** Feed WoW/MoM/YoY reference
  means from `AttractionHourly`; add a weather+wait hourly rollup for the
  correlation; Day-page 28-day overlay degrades minute→hourly resolution. Steady
  state ~350–450 MB. Requires query edits + golden tests.
- **C. Keep raw forever.** Only the index diet + vacuum + WAL truncate. One-time
  ~90 MB reclaim; file still grows ~50 MB/day unbounded.

Recommendation: **A** now (biggest win for least risk, zero behavior change),
revisit **B** if/when the file or YoY forces it. A warrants a short ADR since it
changes the durability guarantee of raw readings.

## Rejected

- **New weekly-average table.** The daily/hourly rollups already are the
  permanent downsample; a weekly grain would be coarser and redundant.
- **Change-only / delta storage** (store a reading only when wait/status
  changes). Largest possible reduction but breaks the "one Reading per poll
  minute" model (CONTEXT.md) — Downtime Rate denominators and `n_readings`
  counts assume per-minute rows. Too invasive.
- **Integer epoch `observed_at`** (vs ISO8601 text). Would roughly halve row +
  index size, but it's a schema migration touching every string-time comparison
  in `stats.py`. Note as a future option, not this plan.
