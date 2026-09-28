# Magic Kingdom route optimization

Can you plan a day at Magic Kingdom that maximizes the number of attractions you
ride? This package answers that from the wait-time history, the
OpenStreetMap-routed walking distances, and the park's real operating hours.

It is **self-contained and read-only**: it opens `data/attractions.db` with
`mode=ro` and never writes to it, so it cannot disturb the collector or the
dashboard. It imports from `app/` only for definitions it must agree with
(`app.stats.walking_distance_between`, `app.db.DB_PATH`).

- `ARTICLE.md` — the write-up.
- `results/` — committed solver output and sweep results.
- `figures/` — charts for the article.
- `plan.py` — the shared planner both the CLI and the web app call, so a plan is
  built one way. Resolves the day, the show times and the commitments, runs the
  ranked solve, and describes the result.
- `shows.py` — when the parade and fireworks start, with a provenance tier
  (`observed` / `weekday_mode` / `park_mode` / `reference`) and a refusal on party
  nights.
- `webapp/` — serves **both** research projects on 127.0.0.1:3467: this one at
  `/` and `/article`, and the wait-time forecaster in `../forecast/` at
  `/forecast` and `/forecast/article`. One templates directory, one static mount
  and one nav rather than two servers on two ports.

A companion package, [`forecast/`](../forecast/README.md), predicts the wait
estimates for a *specific* future date. This optimizer deliberately still uses the
weekday x hour averages it was validated against; wiring the forecast in would
change its inputs and invalidate the numbers published here, so it is left as its
own piece of work.

## Two ways to use it

**Maximize the count** — the original question, and what the article analyses. No
commitments, every attraction interchangeable.

**Plan a day you would actually want** — name up to 5 must-do attractions in
priority order, choose when to eat and for how long, and optionally add the
fireworks and the parade. The must-dos become hard constraints and the
count-maximizer becomes the filler.

```bash
.venv/bin/python -m research_project.cli plan --date 2026-10-05 --list-attractions
.venv/bin/python -m research_project.cli plan --date 2026-10-05 \
    --must-do "Seven Dwarfs,TRON,Peter Pan,Jungle Cruise,Haunted Mansion" \
    --lunch 12:30 --lunch-minutes 45 --shows fireworks,parade --explain
```

Making the picks mandatory is not sufficient on its own. With the objective still
count-first, a 15-hour Saturday schedules all five commitments **in the evening at
their worst queues** — 241.6 minutes on the picks — because the cheap morning
maximizes the count if spent on walk-ons. Adding queueing-on-the-picks as a
lexicographic term ahead of the count cuts that to **129.7 minutes for the same 25
attractions**. The term is bucketed to 5 minutes so it cannot trade a whole
attraction for one saved minute.

Three things are modelled as commitments rather than attractions:

| | How |
|---|---|
| **Must-dos** | `Instance.required`; the objective's leading term is missing commitments, which is what makes `remove`, `swap_in` and `ruin_recreate` safe without guarding each move |
| **Lunch** | a *blackout interval*, not a node. A zero-distance node would break the triangle inequality `bounds.travel_lower_bound` and the exact DP rest on |
| **Shows** | pseudo-stops with `depart(t) = max(t, start) + watch` and a `latest_arrival`. Still non-decreasing, so FIFO and the DP's dominance survive |

When the five picks cannot all fit, the **lowest-ranked** one is dropped first and
the plan says which, and whether it was itself unplaceable or shed to make room for
something above it.

## The problem is not TSP

| | Classic TSP | This |
|---|---|---|
| Nodes visited | **all** of them | **as many as fit** |
| Objective | minimize distance | maximize count, then finish earliest |
| Edge cost | fixed | **depends on arrival time** |
| Time limits | none | park open → close |

You choose a *subset* and an order, and the cost of a stop depends on when you
reach it. That is the **Orienteering Problem** — TSP's selective cousin — and
with hourly wait variation and a closing time it is the **Time-Dependent
Orienteering Problem with Time Windows**. TSP is the right ancestor to explain;
it is not what we solve.

## What the data supports

Measured, not assumed. All figures from `data/attractions.db`.

| | |
|---|---|
| Hourly wait history | **93 park-local dates**, 2026-06-24 → 2026-09-24 |
| Reach vs raw readings | rollups go **58 days further back** than `reading` (ADR-0005 verified) |
| Cell density, hours 9–21 | 2,787 of 2,821 (attraction, weekday, hour) cells non-empty |
| Samples per cell | min 1, p10 6, **median 13**, max 14 |
| Walking graph | **465 of 465** pairs among the roster, zero withheld |
| Forward date horizon | **~31 OPERATING dates** from 2026-09-25 |

### The roster is 30–31, not 35

Four geocoded Magic Kingdom entities have no wait history and are not queue
attractions: **Casey Jr. Splash 'N' Soak Station**, **A Pirate's Adventure**,
**Cinderella Castle**, **Main Street Vehicles**. That leaves **31 with history**.

**Walt Disney's Carousel of Progress is in REFURBISHMENT** as of the latest
poll, so it cannot be visited on any forward date. Hence two roster modes:
`history` (31, for backward-looking analysis) and `open_today` (30, the default
for any forward-looking claim). Both get reported.

### Waits move enough that timing is the whole problem

Peak/trough ratio across the roster: **median 1.7×, p90 3.5×, max 4.4×**.

```
ride                             9   10   11   12   13   14   15   16   17   18   19   20   21
TRON Lightcycle / Run           52   51   57   55   55   54   57   59   61   68   74   83   80
Space Mountain                  27   31   39   39   37   33   33   37   37   40   43   40   31
Pirates of the Caribbean         5   12   21   24   21   17   16   16   16   16   17   16    8
Dumbo the Flying Elephant        6    8   10   11    9    7    7    8    9   11   14   20   13
```

The curves do not share a shape — Pirates peaks at noon and recovers, TRON
climbs monotonically to 83 minutes. A single park-wide crowd curve would misprice
both, so the model uses per-attraction hourly curves.

And the cheap hour is 09:00 for nearly every ride, which is the crux: **the cheap
hour is a contested resource and you can only be in one place for it.**

### The problem binds on every forward date

A design review raised the worry that the count objective might be saturated —
that "all of them" would fit on a long day, making the answer trivial. Measured
against the honest ride-duration table and the real forward calendar, it is not:

| Component | Minutes |
|---|---|
| Σ estimated ride durations (30 rides) | 236 |
| Σ mean hourly waits | 616 |
| Nearest-neighbour travel (3,774 m + 30 transitions) | 87 |
| Entrance overhead | 10 |
| **Total demand to ride all 30** | **950 = 15.8 h** |

Against the forward windows:

| Window | Length | demand ÷ window | |
|---|---|---|---|
| shortest — 2026-09-27 | 9.0 h | **1.76** | binds |
| median — 2026-10-09 | 10.0 h | **1.58** | binds |
| longest — 2026-09-26 | 15.0 h | **1.06** | binds |

Two things made the difference. The honest durations add **86 minutes** over a
flat 5-minute assumption (236 vs 150), because the theater attractions are long.
And **16 of the 31 forward dates are party nights**, so the median forward window
is **10 hours, not 14** — the long Saturday everyone pictures is the exception.

So the count is a real number, the solver does real work, and the relaxation
bound has something to bite on. The lexicographic tie-break (maximize count, then
finish earliest) stays, because it gives local search a gradient on the plateaus
that do occur and because "all 30 with 52 minutes of slack" is a better sentence
than "30" — but it is no longer load-bearing for solvability.

### Count and quality pull hard against each other

Day-weighted mean waits, hours 9–20, split the roster cleanly:

| Band | Rides | Examples |
|---|---|---|
| under 10 min | 9 | Swiss Family Treehouse 5.1, Carousel of Progress 5.6, Regal Carrousel 6.7 |
| 10–25 min | 13 | "it's a small world" 12.3, Pirates 16.5, Haunted Mansion 22.9 |
| 25 min and over | 9 | Space Mountain 36.2, Tiana's 38.4, Seven Dwarfs 48.2, **TRON 59.7** |

The nine headliners cost **≈353 minutes of queueing**. The nine cheapest cost
**≈74 minutes for the same ride count** — nearly 5× less. A count-maximizing
solver trades TRON for the Tiki Room every time, and is right to.

### How much of the model is direct evidence

The estimator tags every value with the tier it came from, so the model's
confidence is reportable rather than implied:

| Tier | hours 9–21 | hours 7–23 |
|---|---|---|
| `cell` — this attraction, this weekday, this hour | **94.1%** | 77.7% |
| `weekday_class` — weekday/weekend pooled | 3.4% | 6.1% |
| `hour` — all weekdays pooled | 1.0% | 1.0% |
| `shape` — attraction level × park hour profile | 1.5% | 15.2% |
| `park` — the last resort | **0%** | **0%** |

Median evidence behind a `cell` estimate: **12 dates**. The park-mean tier never
fires, which is asserted rather than hoped.

Exactly one attraction has **no** direct-evidence cells at all — *Walt Disney's
Carousel of Progress*, which runs a limited seasonal schedule. It is also the one
ride in refurbishment, so the attraction with the weakest evidence is the one
that cannot be visited anyway.

### Why waits are interpolated, not stepped

A stored value is a mean *over* an hour, so it estimates the wait at that hour's
midpoint. Treating it as a step function is not merely coarse — it breaks the
model:

| interpolation | FIFO violations | worst backward step |
|---|---|---|
| **linear** | **0** of 312,263 | 0.00 min |
| step | 957 | **−40.40 min** |

Under a step model, arriving one minute *later* can get you out **40 minutes
earlier**. A solver would find that and "save" 40 minutes by loitering at a ride
entrance, and the exact subset DP's `(visited, last) → earliest completion`
dominance would be invalid, returning wrong optima silently.

Linear interpolation between hour midpoints fixes it because the measured
`max |dw/dt|` is **0.690 < 1**, so `depart(t) = t + w(t) + R` is strictly
increasing. The test asserts FIFO *holds* under linear and *fails* under step, so
the reason for the choice is pinned in code rather than in a comment.

### Weather cannot support the model yet

`weatherreading` is pruned on the same 35-day cutoff as raw readings, leaving
**36 days against 93 days of waits**, split **30 rainy / 6 dry**. In a Florida
summer the *dry* days are the rare class, so there is no usable control group.
Fixing this is prerequisite work outside this package.

## Three traps in the data

1. **The poll interval changed** on 2026-08-04 (ADR-0006). `n_wait` per
   `attractionhourly` row averaged 53.7–58.9 before and ~11.3 after, so pooling
   `SUM(sum_wait)/SUM(n_wait)` over-weights pre-August dates ~5× and is biased
   **high by 0.74 min on average** (up to 15 min on individual cells). The cell
   estimate must be the **unweighted mean of per-row `sum_wait/n_wait`**, with
   n = count of distinct dates.
2. **Two dates are unusable.** `2026-06-24` has no `OPERATING` schedule row, so
   its rows were never operating-hours filtered and carry post-close stale waits.
   `2026-08-04` is the interval changeover itself and is internally mixed
   (n_wait 26.1, between 58.4 and 11.2). Both excluded — and note that hour-23
   rows are *not* all suspect: 2026-07-04 legitimately ran 08:00 → 00:00, so the
   exclusion is on the missing-schedule criterion, never on the hour.
3. **Ticketed events shape the day.** Every date carries both an `OPERATING` and
   a `TICKETED_EVENT` row. **Friday closes at 18:00 on 12 of 18 dates; Saturday
   at 23:00 on 15 of 18**, because every 18:00 close pairs with a same-night
   after-hours party. On the real calendar, weekday and window length are
   therefore **confounded by construction** — which is why attribution needs a
   factorial arm, not just the calendar.

## The assumption ledger

Everything below is invented, not measured. All of it lives in `config.py`, all
of it is swept, and the article states it.

| Assumption | Value | Why it is a guess |
|---|---|---|
| `walk_speed_mps` | 1.1 | No walking-speed data exists. ADR-0009: "Distance is not time." |
| `RIDE_MINUTES` | per-ride table | **Confirmed absent** from the DB, the repo, and themeparks.wiki at every endpoint |
| `transition_overhead_min` | 1.0 | Exiting, queue entry, orientation — not in the graph |
| `start_overhead_min` | 10.0 | Gates, bag check, tapstiles to Main Street |
| `queue_closes_at_close` | True | Parks close the queue and let the line drain |

**Ride durations are the load-bearing guess.** Roughly ten of the roster are
theater or transport attractions with tiny posted waits and long durations — the
Hall of Presidents (23 min), Carousel of Progress (21), Enchanted Tales with
Belle (20), the railroad circuits (20). A flat 5-minute assumption understates
the day by **100–150 minutes**, which is exactly the margin that makes "all 31"
look feasible. So the table is not a detail; it decides whether the study is
degenerate. Correct it and the conclusions may move.

## The start point, and how much it matters

The park entrance is **aliased to the Walt Disney World Railroad – Main Street,
U.S.A. station**, which sits over the entrance turnstiles. Every entrance leg
then comes out of `attractiondistance`, routed by the same OSM graph, obeying the
same triangle inequality, with no invented geometry.

`park.latitude/longitude` (28.416004, −81.581190) is 60 m from that station and
would also have served, so `entrance="osm"` implements it as a **control** —
snapping the park coordinate into the cached OSM graph and routing from there:

| | difference vs the alias |
|---|---|
| per entrance leg | mean **+87 m**, median +96 m, max 106 m |
| in time | mean **+1.32 min**, max 1.61 min |

A route touches the entrance once, so the choice moves a 540–900 minute day by
about a minute — under 0.5%. The alias is safe, and that is now measured rather
than argued. The largest single disagreement is the alias attraction itself: 0 m
under aliasing against 106 m of routed path from the park coordinate, which is a
60 m straight line — a detour ratio of 1.77, entirely plausible for a walkway
curving up Main Street.

The matrix agrees with the published definition,
`app.stats.walking_distance_between`, to **0.0000 m**, and satisfies symmetry, a
zero diagonal and the triangle inequality exactly. The last one matters: the
per-leg transition overhead could in principle have broken it, and both the exact
DP and the travel lower bound depend on it holding.

Mean walking leg across the roster is **7.1 minutes**. From the entrance the
nearest ride is Monsters, Inc. Laugh Floor at 338 m; the farthest is Tiana's
Bayou Adventure at 671 m.

## How a day is simulated

The forward pass resolves an order into timed stops:

```
t = open + start_overhead
for each ride in order:
    arrive = t + travel(previous, ride)
    wait   = curve(ride, weekday, arrive)     # ONE lookup, at the arrival minute
    board  = arrive + wait
    leave  = board + ride_duration
    t      = leave
```

Two decisions are encoded there rather than left implicit.

**The wait is read once, at arrival, and never re-integrated while queueing.** A
posted wait is by definition "how long this will take if you join now", which is
exactly what `attractionhourly` measures; re-evaluating it as the queue advanced
would double-count the forecast already inside the number. A useful consequence:
a wait straddling an hour boundary is a non-question, since the only hour that
matters is the one you arrive in.

**`queue_closes_at_close = True`** means you must *join* by closing time and may
ride after — which is how parks work, the queue closes and the line drains. The
strict alternative (finish by close) costs roughly one attraction. The article
must say which it used.

### A baseline to beat

Running the roster in alphabetical order on the longest Saturday
(2026-09-26, 08:00–23:00) fits **26 of 30**, spending 198 min walking, 519 min
waiting and 180 min riding. A deliberately terrible route already gets 26, so the
solver's job is the last few — and 198 minutes of walking against the
nearest-neighbour figure of 87 shows how much of that is recoverable by ordering
alone.

### What the tests pin

Time-dependent routing has **no valid O(1) move delta**: a 2-opt reversal changes
the arrival time of every stop in the reversed segment *and* everything after it,
so the entire value of the move lives in the term the usual formula omits. The
only legitimate shortcut is replaying the suffix from the first modified
position, and that is asserted rather than trusted:

- **981 random moves** across reorders, insertions and removals — incremental
  re-evaluation matches from-scratch **bit for bit**, 0 mismatches.
- An explicit 12-stop 2-opt reversal shifts downstream departures by **16.7 min**
  and still matches to 0.000e+00, so the check is not passing by doing nothing.
- Time accounts exactly: walking + waiting + riding + entrance overhead equals
  the elapsed span from park open to finish, to 1e-6.
- The departure table is monotone at instance level, including the past-midnight
  padding that a 00:00 close needs.

## First results

Solved for all 31 forward dates in 31 seconds (8 restarts each, ~8 us per
evaluation). `results/forward_dates.csv` has the full table.

### How many can you ride?

**Between 23 and 30**, and the window length decides it almost entirely:

| Window | Attractions | Dates |
|---|---|---|
| 9.0 h | **23** | 11 |
| 10.0 h | 25–26 | 5 |
| 13.0 h | 28–29 | 10 |
| 14.0 h | 30 | 1 |
| 15.0 h | **30** | 4 |

**All eleven nine-hour days return exactly 23** — across Tuesday, Thursday,
Friday and Sunday. The weekday crowd effect is not merely smaller than the
scheduling effect; at this resolution it is *zero*. The answer to "which
combination of factors leaves you riding the least" is an after-hours party
night, whatever the day of the week.

Best day: **Saturday, all 30 rides, finishing 21:54** with 89 minutes still in
hand before the last queue closes.

### Optimal timing beats the arithmetic

The naive demand estimate says all 30 rides need **950 minutes** against
Saturday's 900 — impossible. The solver does it anyway, because that estimate
uses each ride's *mean* wait and the solver picks *when* to ride:

> Space Mountain at 08:28 costs **12.4 minutes**, against a daily mean of 36.2.

Total queueing on that Saturday is **468 minutes against the 616-minute sum of
means** — sequencing alone saves **148 minutes**, about five attractions' worth.
That gap is the entire value of treating this as a time-dependent problem rather
than a static one.

### What a count-maximizer throws away

Skipped most often across the 31 dates:

| | Skipped | Mean wait | Ride |
|---|---|---|---|
| TRON Lightcycle / Run | 26/31 | 59.7 | 1.0 |
| Seven Dwarfs Mine Train | 19/31 | 48.2 | 3.0 |
| **WDW Railroad – Main Street** | **16/31** | 17.5 | **20.0** |
| Jungle Cruise | 12/31 | 32.5 | 10.0 |
| Peter Pan's Flight | 12/31 | 41.5 | 3.0 |
| **WDW Railroad – Fantasyland** | **11/31** | 17.6 | **20.0** |
| **The Hall of Presidents** | **8/31** | 15.2 | **23.0** |

The headliners go, as predicted. But the more interesting casualties are the
railroad and the Hall of Presidents, which have *modest* waits and are discarded
for their **duration**. A count-maximizer prices the total time a visit costs,
not the queue — so a 23-minute show is as expensive as Seven Dwarfs' 48-minute
line.

That is also the clearest vindication of insisting on honest ride durations. Under
a flat 5-minute assumption those three would have looked cheap and been kept, and
the answer would have been wrong in a way nothing in the output would have shown.

The published routes rest on direct evidence: **91–93% `cell` tier** measured over
the lookups the itineraries actually use, not over the table as a whole.

## How much of this is proven

Two independent instruments, because neither alone is enough: a relaxation bound
that is valid everywhere but loose, and an exact dynamic program that is tight
but only runs on reduced instances.

### The exact dynamic program

State is `(visited set, last attraction) → earliest departure`, layered by
popcount. It is sound **only because the wait curves are interpolated**: its
dominance rule assumes leaving earlier is never worse, which is FIFO, which needs
`depart(t)` non-decreasing. Under a step model it is not, and the DP would return
wrong optima in silence.

| n | window | exact optimum | heuristic | binding? | DP time |
|---|---|---|---|---|---|
| 10 | 180 min | 6 | **6** | yes | 0.01 s |
| 12 | 200 min | 7 | **7** | yes | 0.04 s |
| 14 | 220 min | 8 | **8** | yes | 0.18 s |
| 16 | 240 min | 9 | **9** | yes | 0.78 s |
| 16 | 540 min (real) | 16 | 16 | no | 2.59 s |
| 18 | 540 min (real) | 17 | **17** | yes | 13.95 s |

**The heuristic matched the exact optimum on every binding subproblem**, up to
n=20 against the real 9-hour window (18 rides, 76 s). The "binding" column is not
decoration: if a window comfortably fits the whole subset the DP returns n and
matching it proves nothing, so `reduced_instance` deliberately picks the
longest-wait attractions and the tests assert the subproblem is hard before
trusting the match. The DP is separately validated against exhaustive enumeration
of every ordering of every subset on a 6-attraction instance.

A pleasing side result: the DP's optimal 17-ride order runs **Adventureland →
Frontierland → Liberty Square → Fantasyland → Tomorrowland → Main Street**. It
rediscovered the counter-clockwise loop every guidebook recommends, knowing
nothing about the park but pairwise walking distances.

### The relaxation bound

Every ride at its cheapest minute, any order, walking replaced by a lower bound
(the larger of "k distinct edges" and "half of each visited node's shortest
edge"). Each relaxation only ever increases what fits, so the result is a genuine
ceiling.

Gap to the heuristic across the 31 forward dates:

| gap | 0 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|---|
| dates | 5 | 7 | 3 | 11 | 4 | 1 |

**Proven optimal on 5 of 31 dates** — all of them long days where the count
reaches the full roster, which needs no certificate since the count cannot exceed
the attractions that exist.

So the honest claim: the 9-hour figure of **23 is what the heuristic finds, with
a valid ceiling of 26**. It is not proven optimal, and the article should say so.
What supports it is the DP evidence that this heuristic does not fall short on
hard subproblems — five for five — rather than a certificate on the full instance.
Closing that gap needs a sharper bound; the obvious candidate is an hour-capacity
argument (only 60 guest-minutes exist per clock hour, so the whole roster cannot
sit at its individual cheapest hour), which needs an LP and is not built.

## Why the calendar cannot answer "why"

The forward dates show a stark pattern — every nine-hour day yields exactly 23
attractions, every fifteen-hour day yields 30 — but the calendar cannot attribute
it, because **weekday and window length are confounded by construction**. Friday
closes at 18:00 on 12 of 18 observed dates and Saturday at 23:00 on 15 of 18, so
"Friday" and "nine-hour day" are very nearly the same observation. Reading a
crowd effect off that would be crediting the day of the week for the park's
events calendar.

So the sweep runs two arms:

- **Arm A, the calendar** — the real dates with their true windows. Says which
  actual day is worst and produces usable itineraries.
- **Arm B, a factorial grid** — 588 cells crossing weekday x crowd level x window
  length x opening hour, including combinations the calendar never produces. A
  long Friday and a short Saturday both exist here even though neither exists in
  reality, which is the only way to separate the factors.

### Separating crowd level from weekday shape

A weekday curve carries two things: a **shape** (when its peaks fall) and a
**level** (how busy the day is overall). Leaving them merged would mean measuring
one variable twice and calling the result a decomposition, so each weekday's own
level is divided out and the target level multiplied back in. The test asserts
the normalization works — after it, all seven weekdays have **identical**
effective levels to within 1e-9.

That makes questions like "Saturday's shape at Tuesday's crowd level" askable,
and it means the two axes in the decomposition are genuinely independent.

Measured weekday levels, which set the realistic band:

| Sun | Tue | Fri | Mon | Thu | Sat | Wed |
|---|---|---|---|---|---|---|
| 0.927 | 0.963 | 0.979 | 0.984 | 1.027 | 1.039 | 1.080 |

**A design error worth recording.** The first grid swept crowd levels
0.80/0.90/1.00/1.10/1.25 — which puts exactly *one* level inside the realistic
0.93–1.08 band. Restricting the decomposition to that band therefore collapsed
the crowd axis to a single value, dropped it, and reported a crowd effect of
exactly zero. That number was an artifact of the sweep design, not a fact about
the park, and it would have looked like a spectacular confirmation of the
hypothesis. The grid now samples four levels inside the band.

## What actually determines how many rides you get

Three independent instruments, all agreeing. The headline is read off the grid
**restricted to the realistic crowd band**, because the unrestricted version
flatters whichever factor was swept widest — a choice of the experimenter, not a
fact about the park.

### 1. Variance decomposition (588 cells, restricted to crowd 0.92–1.09)

| Factor | eta squared |
|---|---|
| **window length** | **0.930** |
| opening hour | 0.020 |
| crowd level | 0.019 |
| **weekday shape** | **0.004** |

Window length explains **93% of the variance**; crowd level explains **1.9%** —
window matters roughly **49x** more. And once each weekday's level is normalized
out, its *shape* — when the peaks fall — explains **0.4%**, which is to say
nothing at all.

### 2. The exchange rate

- **1.16 attractions per extra hour** of opening time, below saturation
- **0.76 attractions** across the *entire* realistic weekday crowd span
- so the whole difference between the quietest weekday and the busiest is worth
  **39 minutes of opening hours**

A party night costs five to six hours. The schedule is roughly **eight times**
the lever that the crowd calendar is.

### 3. Shapley attribution, per date

With two factors the Shapley value is exact and costs four evaluations — the
corners of {own window, median window} x {own crowd, median crowd}, averaged over
both orderings. Across the 31 forward dates, of all attributed effect:

> **90.4% window length, 9.6% crowd level.**

### Where the cliff sits

The response is flat-then-cliff, not a smooth slope, so the useful statistic is
the shortest window that still fits the whole roster:

| crowd level | 0.80 | 0.93 | 0.98 | 1.03 | 1.08 | 1.25 |
|---|---|---|---|---|---|---|
| cliff (hours) | 12 | 13 | 13 | 14 | 14 | 15 |

Across the realistic band the cliff moves by **one hour**. Crowd level shifts the
cliff; the party schedule carries you across it.

## Pre-registered predictions

Written down before the sweep ran, so the analysis could fail:

| Prediction | Result | |
|---|---|---|
| d(count)/d(hour) between 1.5 and 2.5 | **1.16** | **refuted** |
| crowd effect over the realistic span <= 1 attraction | 0.76 | confirmed |
| entire crowd spread worth 20–40 window-minutes | 39.0 | confirmed |
| eta2(window) at least 5x eta2(crowd) | 49x | confirmed |
| worst calendar date is a short/party window | 2026-09-27, 9 h, party | confirmed |

Four of five. The refuted one is reported as refuted: an extra hour of park time
buys **1.16** attractions, not the 1.5–2.5 predicted. The direction and the
ordering of the factors survive; the magnitude was optimistic, which is worth
more as a recorded miss than as a quietly widened band.

## How much the invented constants matter

The web app's three sliders are the model's guesses, and moving them is the
sensitivity analysis. Measured at both ends of the calendar:

| | 15 h Saturday (saturated) | 9 h party night (binding) |
|---|---|---|
| walking speed 0.8 → 1.5 m/s | 30 → 30 | 23 → **24** |
| ride durations x0.5 → x1.5 | 30 → 30 | **26 → 21** |
| crowd level x0.8 → x1.3 | 30 → 29 | 25 → **21** |

Two things to take from this.

**Ride durations are the most sensitive input**, which is exactly the constant
with no data behind it. A ±50% error moves the count by ±3 attractions out of 23
— about 13%. Walking speed, by contrast, is nearly inert (±1), so the missing
walking-speed data that ADR-0009 warns about turns out not to be the binding
uncertainty here. That is worth knowing, and it is the opposite of what the
assumption ledger's prominence would suggest.

**On saturated days the count is robust and the finish time is not.** The 15-hour
Saturday returns 30 of 30 under almost every setting, but its finish moves from
19:56 to 00:13 as ride durations scale — over four hours. Reporting only the count
on a long day would hide all of that, which is the other reason the objective
carries the finish time as a tie-break.

## The web app

```bash
.venv/bin/python -m research_project.webapp.app     # 127.0.0.1:3467
```

It runs as a **systemd user service**, so it survives a reboot (lingering is on
for this account):

```bash
systemctl --user status  attractions-research     # 127.0.0.1:3467
systemctl --user restart attractions-research
journalctl --user -u attractions-research -n 50
```

The unit pins the port with `Environment=RESEARCH_WEBAPP_PORT=3467` rather than
trusting `DEFAULT_PORT` in `webapp/app.py`: that literal has been rewritten from
outside this repo several times (8017, 8016 and 8020 have all appeared), so the
service states the port it wants. To run it in the foreground instead, stop the
service first or the port is already taken.

Ports on this host: 3467 is this app, 8005 is the live dashboard
(`attractions.service`), 8010 and 8090 belong to other projects.

Two pages:

- **`/`** — the planner. Pick a date, move the assumption sliders, see the route.
- **`/article`** — the write-up, rendered from `ARTICLE.md` with seven figures.

The article page renders the markdown file server-side rather than keeping a
second copy of the prose, and swaps `<!-- chart:name -->` markers for figures. So
a number in a chart and the same number in a sentence come from one computation
(`webapp/charts.py`), and the markdown stays readable on its own.

The planner solves **live** rather than serving precomputed routes, which is the
point — a static table could be a CSV, whereas solving on request lets the
assumptions be moved and their effect watched. ADR-0007's "no arithmetic in the serving layer"
does not apply: this never runs in the cloud and reads SQLite directly.

The park map and the day timeline are hand-drawn inline SVG rather than a
charting library. The map is a projection, not a chart — it needs a cosine
correction on longitude or the park comes out stretched east-west — and the
timeline needs stacked queue/ride segments on a clock axis. Both are a few lines
of geometry, and avoiding a CDN keeps the package runnable offline.

One honesty note carried in the UI: the map draws straight lines between stops,
while the distances behind them are routed over real footpaths. The numbers are
routed; the drawing is not.

## Running it

```bash
.venv/bin/python -m research_project.cli --help
.venv/bin/python tests/test_research_waits.py
.venv/bin/python tests/test_research_orienteering.py
```

The web app runs as the `attractions-research` systemd user service on
**127.0.0.1:3467** — 8005 is the live dashboard (`attractions.service`), 8010 and
8090 belong to other projects on this host.
