# Poll every 5 minutes instead of every minute

The collector has run at `* * * * *` since [ADR-0001](0001-decoupled-cron-collector.md)
— 180 API requests/hour and ~204 K rows/day — on the untested assumption that
wait times move minute-to-minute. They do not. We **drop the poll to
`*/5 * * * *`**, cutting API calls ~80% and the steady-state database ~75%,
because measurement shows a 5-minute sample is *below the resolution of the
upstream data itself*.

## The data doesn't move minute-to-minute

Over 552,671 consecutive 1-minute pairs of OPERATING readings (7 days):

- **95.7% are identical to the previous minute.** Mean absolute change 0.36 min.
- Every real delta is a **multiple of 5** (2.61% at ±5, 1.04% at ±10, 0.37% at
  ±15). themeparks.wiki reports standby waits in **5-minute buckets** — we were
  sampling 60×/hour a signal that cannot express a change smaller than 5.
- Mean gap between actual changes: **18.9 minutes**.

Replaying the real series with last-value-hold at coarser intervals:

| Interval | Mean abs error | % of minutes wrong | reqs/hr | rows/day |
|---|---|---|---|---|
| 1 min (was) | 0.000 | 0.00% | 180 | 204,480 |
| **5 min** | **0.719** | **7.99%** | **36** | **40,896** |
| 10 min | 1.503 | 15.60% | 18 | 20,448 |
| 15 min | 2.174 | 21.35% | 12 | 13,632 |

At 5 minutes the mean error (0.72 min) is **smaller than the 5-minute quantum
the API reports in** — the added error sits under the data's own noise floor.

## Why 5 and not 10 or 15

Downtime is the constraint, not wait accuracy. Of 1,466 DOWN episodes over 14
days (median 20 min), the short tail is fragile:

| Sampling | DOWN episodes detected |
|---|---|
| 1 min | 100% |
| **5 min** | **94.6%** |
| 10 min | 86.6% |
| 15 min | 79.0% |

Episodes under 5 minutes are **11.1% of all outages**; at a 15-minute poll each
has only a **17%** chance of being observed. Losing a fifth of all outages would
bias `get_reliability` and the Downtime z-scores **silently** — the metric would
still render, just wrong. 10 and 15 minutes save little beyond 5 (18 and 12
reqs/hr vs 36) and pay for it in the one place this project is opinionated.

## Consequences

- **Ratios are interval-independent and unchanged.** Downtime Rate, Park
  Downtime Rate, and every rollup `n_down / n_readings` are quotients in which
  the interval cancels. `AttractionHourly`/`AttractionDaily` store `n_wait` and
  `sum_wait`, so means stay correct with **no backfill and no rollup rebuild**.
- **Absolute durations had to be fixed.** `stats.get_downtime` computed
  `down_minutes` as `df["down"].sum()` — a *row count* rendered on the dashboard
  with unit "min". Correct only while 1 row = 1 minute; at a 5-minute poll it
  under-reports 5×. It now weights each down row by the **real elapsed gap to
  that attraction's next reading**, clipped to `POLL_INTERVAL_MINUTES`. This is
  deliberately gap-derived rather than `rows × 5`, so that:
  - the changeover day, which holds both 1-minute and 5-minute rows, totals
    true minutes rather than a blended fiction;
  - a missed poll cannot inflate a duration (the clip);
  - a future interval change needs only the constant updated.
  Verified against a pre-change day: old row-count 3,620 vs new gap-weighted
  3,628 (+0.2%, from trailing rows charged a full interval), so historical
  figures stay comparable.
- **A seam in sample density, not in values.** Readings now land on 5-minute
  boundaries. Estimates either side of 2026-08-04 remain unbiased, but their
  *variance* differs — a rate computed from 5× fewer samples is noisier. WoW /
  MoM / YoY comparisons straddling this date are still valid, just less precise
  on the recent side. No correction is applied; the effect is small relative to
  day-to-day variation and correcting it would imply a precision we don't have.
- **Storage falls to ~400 MB** from 1.56 GB, reached *gradually*: the
  [ADR-0005](0005-raw-reading-retention.md) prune retires dense 1-minute history
  over 35 days. Freed pages are reused, not returned, so the file itself only
  shrinks after a one-off `VACUUM` once the transition completes.
- **Sub-5-minute wait changes are now unobservable.** Accepted: they are 0.01%
  of observed deltas (the non-multiple-of-5 rows), almost certainly upstream
  correction artifacts rather than real guest-facing movement.

## Considered and rejected

- **10 or 15 minutes.** Rejected on downtime detection (86.6% / 79.0%), above.
- **Change-only / delta storage.** Still rejected for the reason given in
  ADR-0005 — it breaks the one-row-per-poll model that the Downtime Rate
  denominator assumes. Note this ADR *weakens* that objection: the duration path
  is now gap-derived and would tolerate irregular rows. The rate denominators
  still would not.
- **Adaptive polling** (fast during park hours, slow overnight). Real savings,
  but overnight rows are already excluded at query time by Operating Hours, and
  it makes the cron line stateful and the sample density time-of-day dependent
  — which would bias exactly the pooled rates above. Revisit only if API quota
  becomes a constraint.
- **Keeping 1 minute and pruning harder.** Attacks storage but not the 180
  reqs/hour against a third-party API, and leaves 95.7% of writes redundant.

## Mechanism

Single crontab line, edited surgically (the crontab is shared with an unrelated
project, so it is never rewritten wholesale):

```
*/5 * * * * cd /home/azureuser/live_events/attractions && flock -n /tmp/attractions-poll.lock .venv/bin/python -m app.collector >> /tmp/attractions-poll.log 2>&1
```

`POLL_INTERVAL_MINUTES` in `app/stats.py` must be kept in sync with this period.
It is the single point of coupling between the cron schedule and the query
layer; nothing else reads the interval.
