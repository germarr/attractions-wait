# Capture scheduled entertainment, and treat a showtime as irreplaceable

> **Reverses a scoping decision.** `CONTEXT.md`'s glossary said a Show "has
> showtimes, not a standby wait, and is therefore excluded from this app
> entirely." That was right while the app only answered "how long is the queue".
> It stops being right the moment the route planner tries to plan a *day*, because
> a day at Magic Kingdom contains a 14:00 parade and 21:30 fireworks whether the
> model knows about them or not.

We **capture `SHOW` entities and their published performance times** into two new
tables, `show` and `showtime`, and we **never prune them**.

## What was being discarded

The feed already carried all of it, on every poll. Magic Kingdom's `/live` returns
71 entries — **35 ATTRACTION, 27 SHOW, 23 RESTAURANT, 1 PARK** — and 19 of the
shows carry a `showtimes` array. Two lines threw the lot away:
`app/collector.py`'s `entityType != "ATTRACTION"` gate, and `fetch_geo`'s matching
branch, which kept 129 of the 504 children Walt Disney World returns.

So the cost of this change is zero extra requests. It is purely a matter of
storing what was already being fetched and dropped.

## A showtime has no second copy, and no future feed

This is the reason the table is exempt from retention, and it is worth stating
precisely because it is the opposite of how `reading` behaves.

A raw `reading` is safe to delete after 35 days (ADR-0005) because the hourly
rollups already absorbed what it contributed. **A showtime has no rollup.** And,
unlike park hours, it cannot be re-fetched later:

| endpoint | gives us |
|---|---|
| `/entity/{park_id}/live` | **today's** showtimes only |
| `/entity/{show_id}/schedule` | **zero rows** — verified for both the fireworks and the parade |
| `/entity/{park_id}/schedule` | operating hours and ticketed events; no performances |

There is no historical archive and no forward publication. A performance not
recorded on the day it ran is unrecoverable, permanently. `app/retention.py`
therefore prunes exactly two tables, carries a comment saying why `showtime` is
not one of them, and `tests/test_showtimes.py` asserts a prune run leaves it
untouched.

## `kind` is preserved, because the values are not interchangeable

The feed's `showtimes[].type` is stored verbatim rather than normalized away:

- **`Performance Time`** — a real scheduled show. 347 of the 367 rows in the first
  capture. This is the only kind the planner will schedule around.
- **`Operating`** — a continuously-open meet-and-greet whose "showtime" is just its
  opening hours. Treating this as a performance would have the planner queue you
  for a 9-hour event.
- **`Special Ticketed Event`** — belongs to a party the regular ticket does not
  admit you to.

## A Show is a sibling table, not a column on `attraction`

An `entity_type` column would have been fewer lines. It was rejected because
`CONTEXT.md` defines an Attraction as *a thing that reports a standby wait*, and
that definition earns its keep: every wait query, rollup and estimator in the
codebase relies on it without having to filter. Adding shows to the same table
would mean either rows with a permanently NULL wait polluting those aggregates, or
a type predicate that each of them has to remember. One forgotten filter is a
silently wrong mean.

## Consequences

- 144 shows and 367 showtimes on the first poll; Universal's parks carry far fewer
  performances than Disney's (2–10 against 37–122), which is real, not a bug.
- **The history starts now.** The planner therefore needs a seeded reference for
  the two Magic Kingdom shows it offers, labelled as a single observation, until
  enough dates accrue to report a measured modal time per weekday. That provenance
  is surfaced in the UI rather than smoothed over.
- Shows are geocoded but **not** in `attractiondistance`. The planner aliases each
  to its nearest *routable* Attraction, the pattern already used to alias the park
  entrance to the Main Street railroad station, so no new footpath routing is
  needed. "Routable" does real work here: Cinderella Castle is 32 m from the
  fireworks viewing hub and would be the obvious alias, but it reports no standby
  wait and so is not in the roster at all, which leaves Mickey's PhilharMagic at
  **100 m**. The parade aliases to Country Bear Musical Jamboree at **20 m**. Both
  are inside the park's 403 m average leg, and both offsets are reported in the
  plan rather than hidden.
- `collect()` gained a `--reseed-geo` flag, because coordinates refresh only every
  six hours and a newly-added entity kind would otherwise wait out the interval.

## What this does not do

It does not model show *capacity* or how early you must really arrive to see
anything — the planner carries those as stated estimates. It does not capture
restaurant availability, though `/live` offers `diningAvailability` on two
entities. And it does not fix `_ensure_schedules`' upsert on
`(park_id, date, type)`, which would keep only one TICKETED_EVENT if a date ever
published both an early entry and a party; no date currently does, so that stays a
noted hazard rather than a change.
