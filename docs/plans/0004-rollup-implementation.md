# Implementation plan — precomputed daily rollups

Companion to [ADR-0004](../adr/0004-precomputed-daily-rollups.md). This is the
concrete build: schema, the nightly builder, the one-time backfill, the cron
line, per-endpoint merge math, rollout order, and tests.

> **Status: implemented.** Shipped in `app/models.py` (5 rollup tables +
> composite index), `app/rollup.py` (builder), `app/backfill_rollups.py`, and the
> four endpoint migrations in `app/stats.py`. Two refinements vs. this plan's
> first draft: (1) a **5th table `AttractionHourly`** (per attraction/date/hour
> wait sums) was needed so week/month Crowd Index reconstructs exactly; (2) the
> today-merge became a **self-healing live tail** — the request layer live-computes
> every date *after* the last rolled one, not just today, so a late/failed nightly
> never drops the just-finished day. Each endpoint was gated on a golden test
> (rollup+tail == old all-live, within rounding) at the same instant; all pass at
> 0.000, including a clock-mocked gap scenario. Cron runs 06:15 UTC.

## 0. Measured baseline (why)

On the live DB at time of writing:

| | value |
|---|---|
| Readings | 1,674,238 (290 MB, ~209 K/day) |
| `/api/parks/compare` | **7.6–11.6 s**, reads **1,088,286 rows** for one Destination |
| `/api/reliability` | **1.7 s**, scans a park's full history |
| everything else (`stats`, `series`, `recent`, `attractions`) | < 0.2 s (bounded to today) |

Target after rollups: `parks/compare` and `reliability` < 1 s and **flat** as
history grows (per-request raw read bounded to today only).

## 1. Schema — new tables (`app/models.py`)

All aggregates are computed over **Operating-Hours-filtered** readings, bucketed
in **park-local time** (`America/New_York`) — identical rules to the query layer
(`stats._within_operating_hours`). `date` is `YYYY-MM-DD`, park-local.

```python
class AttractionDaily(SQLModel, table=True):
    """One finalized park-local day of an Attraction, Operating-Hours-filtered."""
    attraction_id: str = Field(foreign_key="attraction.id", primary_key=True)
    date: str = Field(primary_key=True)              # YYYY-MM-DD park-local
    n_readings: int                                  # in-hours reading-minutes (downtime denominator)
    n_down: int                                      # DOWN|CLOSED in-hours (downtime numerator)
    n_wait: int                                      # non-null wait minutes
    sum_wait: float                                  # Σ wait_time over n_wait   → window mean = Σsum/Σn
    mean_wait: float | None                          # per-day bucket for week/month charts
    median_wait: float | None                        # per-day bucket (can't be summed → stored)
    std_wait: float | None                           # per-day ±1σ bucket
    built_at: str                                    # UTC ISO, provenance

class ParkDaily(SQLModel, table=True):
    """One finalized park-local day of a Park's synthetic Park Average + Headliner."""
    park_id: str = Field(foreign_key="park.id", primary_key=True)
    date: str = Field(primary_key=True)
    n_readings: int                                  # in-hours reading-minutes across roster (park downtime denom)
    n_down: int                                      # in-hours DOWN|CLOSED across roster
    n_min: int                                       # distinct operating minutes (park-average denominator)
    sum_minavg: float                                # Σ per-minute Park Average  → avg_wait = Σsum/Σn_min
    sum_top5: float                                  # Σ per-minute top-5 mean     → headliner = Σsum/Σn_min_top5
    n_min_top5: int
    mean_wait: float | None                          # per-day Park-Average bucket for park-avg charts
    median_wait: float | None
    std_wait: float | None
    built_at: str

class AttractionHourly(SQLModel, table=True):
    """Per attraction, per date, per hour-of-day wait sums. Backs the Crowd Index:
    the baseline is Σsum/Σn over trailing days (→ AttractionHourBaseline), and a
    week/month window's numerator sums sum_wait/baseline over its dates. Added
    after the first draft — the daily grain alone can't reconstruct crowd."""
    attraction_id: str = Field(foreign_key="attraction.id", primary_key=True)
    date: str = Field(primary_key=True)              # YYYY-MM-DD park-local
    hour: int = Field(primary_key=True)              # 0–23 park-local
    n_wait: int
    sum_wait: float
    built_at: str

class AttractionHourBaseline(SQLModel, table=True):
    """Crowd-Index baseline: hour-of-day wait sum/n per Attraction over the
    trailing CROWD_BASELINE_DAYS. Fully rebuilt each night (small). Stores sum+n
    (not a finished mean) so the request layer folds today's live hours in — the
    live baseline includes today."""
    attraction_id: str = Field(foreign_key="attraction.id", primary_key=True)
    hour: int = Field(primary_key=True)              # 0–23 park-local
    sum_wait: float
    n: int
    built_at: str

class ParkCorrelation(SQLModel, table=True):
    """Nightly Pearson matrix (wait/downtime/temp/rain, 30-day hourly) per park.
    Stored as JSON so the shape can evolve without a migration."""
    park_id: str = Field(foreign_key="park.id", primary_key=True)
    labels_json: str
    matrix_json: str | None
    n: int
    built_at: str
```

Row-count sanity: `attraction_daily` ≈ 142 × 365 ≈ 52 K rows/year;
`park_daily` ≈ 7 × 365 ≈ 2.5 K/year; baselines 142 × 24 ≈ 3.4 K (rewritten
nightly); correlations 7. Trivial next to the 76 M raw rows/year.

`init_db()` already calls `SQLModel.metadata.create_all` — the new tables appear
automatically. No `_migrate()` change needed (these are brand-new tables, not
altered ones).

Also add the composite index (quick win, independent of rollups):

```python
# on Reading, replacing the two single-column indexes' role for the IN + range scan
__table_args__ = (Index("ix_reading_attr_observed", "attraction_id", "observed_at"),)
```

## 2. The nightly builder — `app/rollup.py`

One module, importable and callable both from cron and the backfill.

```python
def build_day(session, local_date: str, *, parks=None) -> None:
    """Upsert AttractionDaily + ParkDaily for one finalized park-local date."""
def rebuild_baselines(session) -> None:
    """Full rebuild of AttractionHourBaseline over trailing CROWD_BASELINE_DAYS."""
def rebuild_correlations(session) -> None:
    """Full rebuild of ParkCorrelation (30-day hourly, per park)."""
def run_nightly(trailing_days: int = 3) -> None:
    """Entry point: rebuild the last `trailing_days` finalized days + baselines +
    correlations. Idempotent (upsert by PK)."""
```

Key implementation notes:

- **Reuse the existing query helpers.** `build_day` reads that date's raw rows
  once (bounded `WHERE observed_at >= day_start AND < day_end`), runs them through
  the *same* `_within_operating_hours(df, park_id)` filter, then groups. Do **not**
  re-implement the Operating-Hours or park-local logic — import it, so the rollup
  and the live path can never drift.
- **`park_daily` per-minute figures** come from the same computation `parks/
  compare` does today: `groupby(observed_at).mean()` for the Park Average
  (`sum_minavg`/`n_min`), and the vectorized descending-rank top-5 (`_rk <
  HEADLINER_N`) for the Headliner (`sum_top5`/`n_min_top5`). Storing the sums —
  not the finished means — is what lets a week/month window combine days by
  Σsum ÷ Σn.
- **Recompute a trailing window, not just yesterday** (default 3 days), so a late
  `/schedule` fetch or a weather backfill that changes a recent day is absorbed.
  Upsert by primary key → idempotent, safe to re-run.
- **Time source.** `run_nightly` derives "today" in park-local time and finalizes
  days strictly before it (never rolls up the in-progress local day — that's the
  live path's job).

## 3. One-time backfill — `app/backfill_rollups.py`

Iterate every park-local date from `MIN(observed_at)` to yesterday, call
`build_day` for each, then `rebuild_baselines` + `rebuild_correlations`. Runs
once at deploy against the existing 8 days of history (seconds), and is the same
code path the nightly cron exercises. Pattern mirrors the existing
`app/backfill_weather.py`.

## 4. Cron (respect the shared crontab)

The crontab is **shared with an unrelated YouTube project** and holds the
per-minute collector — **append one line, never rewrite** (a backup was saved to
`docs/crontab.backup.*.txt` first). Server clock is UTC; NY midnight is
04:00–05:00 UTC, so it runs a couple hours after at **06:15 UTC (~2am ET)`** so
yesterday (park-local) is complete. `flock`-guarded like the collector:

```cron
15 6 * * * cd /home/azureuser/live_events/attractions && flock -n /tmp/attractions-rollup.lock .venv/bin/python -m app.rollup >> /tmp/attractions-rollup.log 2>&1
```

`python -m app.rollup` calls `run_nightly()` (installed). No web-service restart
needed — the systemd user service on :8005 reads the new tables on the next
request. Exact timing is not correctness-critical: the request layer's live tail
self-heals any day the nightly hasn't finalized.

## 5. Per-endpoint changes (the merge math)

General shape everywhere: **prior days from rollup + today computed live from a
bounded raw read, appended as one more day-bucket.**

### `get_park_comparison` (the 10 s call → the big win)
- Drop the 90-day `_readings_frame` bulk read. Replace with:
  - **Baselines** (Crowd Index): read `attraction_hour_baseline`. Today's window
    readings ÷ baseline, mean × 100 — unchanged formula, precomputed denominator.
  - **avg_wait / headliner / downtime_rate over the window:** sum `park_daily`
    rows for prior dates in the window + today's live park-frame figures.
    `avg_wait = (Σsum_minavg + today.sum) / (Σn_min + today.n)`; `headliner`
    analogously; `downtime_rate = 100·(Σn_down)/(Σn_readings)`.
  - **Live strip** (Roster, open-now, down-now, **Momentum**): unchanged, still
    from a *today-only* raw read (`_readings_frame(aids, today_start)` — ~105 K
    rows, sub-second) and the latest poll.
- Net raw read per call: today only, bounded and flat.

### `get_reliability` (kills the full-history scan)
- Window rates (today/yesterday/week/month/historic) = sum `attraction_daily`
  `n_down`/`n_readings` over each window's dates (+ today live).
- **z-score** = today's rate vs the trailing-month **daily-rate series** read
  straight from `attraction_daily` (excluding today) — no re-derivation.
- **Correlation** = read the precomputed `park_correlation` row.
- Park totals = the same, aggregated across the park's roster.

### `get_series` (week/month) and `get_day_summary`
- `series` week/month buckets = per-day `mean_wait`/`median_wait`/`std_wait` from
  the daily table (attraction target → `attraction_daily`; `park:` target →
  `park_daily`) + today's live bucket. `today` window stays fully live.
- `get_day_summary`: `means.week/month/historic` = Σsum ÷ Σn from the daily
  table; `weekday` means = group daily rows by weekday (replaces the current
  `EPOCH` all-history scan); `downtime.historic` = Σ from daily; `correlation` =
  `park_correlation`. `compare` traces + WoW/MoM/YoY stay live (bounded).

### Unchanged (already cheap, stay fully live)
`get_stats`, `get_recent`, `get_day_live` (current/trace/WoW/MoM/YoY), `get_attractions`,
`get_destinations`, Momentum, Roster.

## 6. Frontend — `app/static/parks.js` (not changed; no longer needed)
The original worry was `parks.js` re-reading 90 days every 60 s. Post-rollup the
whole `/api/parks/compare` call is ~0.7 s and bounded, so the single 60 s timer
is now *desirable* (keeps the live strip fresh) rather than wasteful. Splitting it
would add complexity for no real gain — intentionally left as-is.

## 7. Rollout order — DONE
1. ✅ Composite index `ix_reading_attr_observed` (via `db._migrate`, applies to the
   existing DB) + the `LIMIT 1` fix on `_latest_observed_at`/`get_weather_now`.
2. ✅ Schema (5 tables) + `app/rollup.py` builder + `app/backfill_rollups.py`
   (backfilled, counts verified).
3. ✅ Cron installed (06:15 UTC); `run_nightly` verified idempotent.
4. ✅ `get_park_comparison` — golden match 0.000, 6.3 s → 0.7 s.
5. ✅ `get_reliability` — golden match, 3.4 s → 0.18 s.
6. ✅ `get_series` week/month + `get_day_summary` — golden match 0.000.

## 8. Tests (the safety net for two-sources-of-truth) — all passing
- **Golden equivalence:** rollup+tail == a frozen copy of the old all-live code,
  computed in the *same instant* (avoids per-minute-collector drift), across every
  destination/park/target × window. All 0.000. Gated every endpoint switch.
- **Gap / self-heal:** clock mocked +1 day so a finalized date isn't yet rolled;
  the live tail must fill it. All four endpoints match 0.000 — this caught the
  double-count bug (§ live tail must be `> max(rolled)`, not "missing from rollup").
- **Idempotency:** `run_nightly` twice → identical rows (built_at aside). ✅
- **Boundary:** ghost attraction with no data (empty in both), park closed all day
  (n_min=0), pre-history dates inside a 30-day window.

Note: the golden harness lived in a scratch module (a frozen copy of the old
functions). If these are to become permanent regression tests, port that pattern
into a `tests/` dir with a small fixture DB.

## 9. Open questions for review
- Trailing recompute window: 3 days enough, or match the schedule-refresh
  cadence?
- Keep `attraction_daily.mean/median/std` (chart buckets) **and** sum/n
  (window means) in one table (proposed), or split fact vs. presentation?
- Retention: do we ever prune raw `reading` once a day is rolled up, or keep raw
  forever for re-derivation? (Proposed: keep raw; rollups are additive.)
