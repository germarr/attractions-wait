# How many rides can you fit into one day at Magic Kingdom?

Between **23 and 30**, out of a roster of 30. Which of those you get is decided
almost entirely by one thing — and it isn't the crowds.

This is an account of building a route optimizer on top of three months of
wait-time data, walking distances routed over OpenStreetMap footpaths, and the
park's own operating calendar. It covers the theory, because the problem is a
famous one wearing a disguise; the data pipeline, because most of the difficulty
turned out to live there; and the results, including the ones that contradicted
what I expected.

---

## Part 1 — The Traveling Salesman Problem

A salesman must visit every city on a list exactly once and return home, and
wants the shortest possible tour. That's the Traveling Salesman Problem. It is
probably the most studied optimization problem in existence, and its appeal is
that it is trivial to state and brutal to solve.

### Why it is hard

There are `(n−1)!/2` distinct tours of `n` cities. For 10 cities that's 181,440
— a laptop does it instantly. For 20 cities it's about 6×10¹⁶. For our 30
attractions it is roughly **4.4×10³⁰**, which is more tours than there are
grains of sand on Earth, by several orders of magnitude.

TSP is NP-hard: nobody knows an algorithm that solves it in polynomial time, and
most people believe none exists. But "NP-hard" is often misread as "hopeless",
and that is wrong. Modern exact solvers routinely handle tens of thousands of
cities, because the combinatorial explosion is an upper bound on a *naive*
search, not a description of how good search actually behaves. What NP-hardness
really promises is that somebody can always construct an instance that defeats
you.

<!-- chart:search_space -->

### The trade-off that defines the field

Every approach to TSP sits somewhere on one axis:

| | Exact methods | Heuristics |
|---|---|---|
| Guarantee | provably optimal | usually very good |
| Cost | grows explosively | grows gently |
| Typical use | small or structured instances | everything else |

The interesting engineering is rarely "which algorithm" and almost always "how
do I know my answer is any good?" A heuristic without a bound is a number with no
error bar. Much of the work below is about earning that error bar.

---

## Part 2 — Except this isn't TSP

It is worth being precise, because the difference changes the algorithm.

| | Classic TSP | A day at Magic Kingdom |
|---|---|---|
| Which nodes | **all** of them | **as many as fit** |
| Objective | minimize distance | maximize count |
| Edge cost | fixed | **depends on when you arrive** |
| Time limits | none | the park opens and closes |

You cannot ride all 30 attractions in nine hours, so *selection* is the core
decision — and a problem where you choose a profitable subset under a budget is
the **Orienteering Problem**, named for the sport. Add waits that change by the
hour and a closing time and it becomes the **Time-Dependent Orienteering Problem
with Time Windows**.

TSP is still the right ancestor to understand. It supplies the hardness
intuition, the exact-versus-heuristic trade-off, and most of the move
vocabulary. But we never solve TSP, and saying we did would be wrong.

### What time-dependence actually breaks

This is the part that surprised me most. In ordinary routing, you evaluate a
candidate move in constant time: reversing a segment of the tour changes exactly
two edges, so the delta is a four-term arithmetic expression.

Here that is false. Reversing a segment changes the arrival time at every stop in
the segment **and every stop after it**, and therefore every wait downstream.
The entire value of the move lives in the term the constant-time formula omits.
Worse, a reversal can be travel-neutral and still be a large improvement — or a
disaster — purely through timing.

<!-- chart:intraday -->

So every move evaluation replays the route from the first changed position. At
this size that costs about 8 microseconds, which is affordable. But any
implementation claiming an O(1) delta for this problem is simply wrong, and the
test suite asserts that the incremental path matches a from-scratch evaluation
bit for bit — across 981 random moves, with an explicit 12-stop reversal that
shifts downstream departures by 16.7 minutes and still agrees to zero.

---

## Part 3 — The data

### Collecting it

A cron job polls [themeparks.wiki](https://themeparks.wiki) every five minutes
and writes one row per attraction per poll: the posted standby wait, and a status
saying whether the ride is operating, down, or closed. It has been running since
June 2026. Weather comes from Open-Meteo on the same cadence.

The collector is a standalone script rather than an in-process scheduler, so a
crash in the web app cannot take down collection, and vice versa. Storage is a
single SQLite file in WAL mode, which lets the writer and readers coexist without
ceremony. It is currently 1.5 GB.

### What survives, and why that matters

Raw readings are pruned after 35 days to bound growth, but before that happens
they are rolled up into per-attraction, per-date, **per-hour** aggregates that are
never deleted. The practical consequence is the thing that made this project
possible:

> The hourly rollups reach **58 days further back** than the raw data they came
> from. Raw covers 36 days; the rollups cover 93.

For a model that needs "what is the wait at this ride, on this weekday, at this
hour", that is the difference between a month of history and three.

After screening, **91 usable dates** remain. Within the park's core hours of
09:00–21:00, the model has direct evidence for **94.1%** of all
(attraction, weekday, hour) cells, with a median of **13 dates** behind each one.
Every estimate carries a tag saying which tier it came from, so the model's
confidence is reportable instead of implied. The weakest tier — a park-wide
fallback — never fires at all, which is asserted rather than hoped.

### Three traps

Real datasets have seams, and all three of these would have produced
plausible-looking wrong answers.

**The poll interval changed.** In August the collector moved from one-minute to
five-minute polling. The stored field `n_wait` is a *poll count*, so it fell from
~54 per hour to ~11. Any estimator that pools `SUM(wait) / SUM(count)` across
that seam silently weights the older, denser period about 5× — biasing estimates
**high by 0.74 minutes on average**, and by up to 15 minutes on individual cells.
The fix is to divide per row first, making each observation a mean over its own
hour whatever the interval was, and then average across dates. There is a test
pinned to that 0.74-minute figure, so reintroducing the pooled form anywhere
would fail loudly.

**One day was never filtered.** The rollup pipeline discards readings from
outside operating hours, because the API keeps echoing stale waits after a park
closes. It does that by looking up the day's schedule — and for one date, no
schedule row exists, so the filter passed the whole day through untouched, stale
waits included. The exclusion is written against the missing-schedule condition
rather than that specific date, because the class matters more than the instance.

**The changeover day is internally mixed.** The date the poll interval changed
averages 26 polls per hour, sitting between the 58 before and 11 after. No
per-date constant describes it. Excluded.

### Walking distances

The dataset stores each attraction's coordinates, but straight-line distance is a
poor guide to walking. Big Thunder Mountain and the Haunted Mansion are **174 m
apart and 391 m to walk**, because Rivers of America is between them. Across
Magic Kingdom the ratio of walking to straight-line distance runs from 1.04 to
2.24, so no single correction factor exists.

The distances therefore come from a shortest path over an OpenStreetMap footpath
graph, computed once and stored. Queue paths are excluded from the graph — OSM
maps them, and a router will otherwise cheerfully send you up the Haunted Mansion
standby line and out through the exit.

The mean leg between two Magic Kingdom attractions is **403 m, about 7 minutes**.
A 20-stop day is over two hours of walking.

### What had to be invented

Three numbers are not in any dataset. They live in one file, they are labelled as
estimates, and the web app exposes them as sliders so their influence can be
seen rather than argued about.

| Assumption | Value | Why it is a guess |
|---|---|---|
| Walking speed | 1.1 m/s | No walking-speed data exists anywhere |
| Ride durations | per-ride table | Absent from the database, the API, and everywhere else |
| Transition overhead | 1 min | Exiting, re-orienting, finding the next queue |

Ride durations deserve emphasis, because they decide whether the study is
degenerate. About ten attractions are theatre or transport — the Hall of
Presidents runs 23 minutes, Carousel of Progress 21, the railroad circuit 20 —
with short queues and long durations. Assuming a flat five minutes each
understates a full day by **86 minutes**, which is precisely the margin between
"everything fits" and "it doesn't".

---

## Part 4 — The stack

Deliberately small.

| | |
|---|---|
| Collection | Python, cron, `requests` |
| Storage | SQLite (WAL), 1.5 GB |
| Aggregation | pandas, nightly rollups |
| Walking graph | OpenStreetMap via Overpass, cached locally |
| Solver | **pure Python**, no dependencies |
| Web app | FastAPI, Jinja2, hand-drawn inline SVG |

The solver has no optimization library behind it, and that was a considered
choice rather than stubbornness. OR-Tools' routing layer wants transit costs that
don't depend on state, which is exactly what this problem violates; expressing
time-dependent service times means fighting the library to recover a model that
fits in forty lines of Python. At 30 nodes, a hand-written local search evaluates
a candidate in 8 microseconds and a whole day in under a second.

The front end draws its park map and timeline as inline SVG rather than pulling
in a charting library — the map is a projection, not a chart, and it needs a
cosine correction on longitude or the park comes out stretched east-west.

---

## Part 5 — Results

### The shape of the answer

Solving all 31 upcoming dates takes 31 seconds. The counts range from 23 to 30,
and **the length of the operating day explains nearly all of the variation**:

| Window | Attractions | Dates |
|---|---|---|
| 9.0 h | **23** | 11 |
| 10.0 h | 25–26 | 5 |
| 13.0 h | 28–29 | 10 |
| 14.0 h | 30 | 1 |
| 15.0 h | **30** | 4 |

All eleven nine-hour days return exactly 23 — across Tuesday, Thursday, Friday
*and* Sunday.

### The best day

A 15-hour Saturday: **all 30 attractions, finishing at 21:54**, with 89 minutes
still in hand before the last queue closes. The plan opens like this:

```
 1. 08:17  Astro Orbiter                         wait  5.9
 2. 08:28  Space Mountain                        wait 12.4
 3. 08:46  Buzz Lightyear's Space Ranger Spin    wait 10.2
 4. 09:11  Big Thunder Mountain Railroad         wait 15.9
 5. 09:33  Tiana's Bayou Adventure               wait 10.0
 6. 09:55  Haunted Mansion                       wait 14.0
```

and closes with the walk-on Adventureland cluster — Swiss Family Treehouse, the
Tiki Room, Country Bears, Pirates, Jungle Cruise — between 19:53 and 21:31.

Nobody told it to do that. Front-loading headliners into the cheap first hour and
saving low-wait attractions for the evening is what falls out of the arithmetic.

### Sequencing beats the arithmetic

Here is the result I did not expect. Add up every attraction's *average* wait,
its ride duration, and a reasonable amount of walking, and one day needs **950
minutes**. The longest Saturday offers 900. On paper, all 30 is impossible.

It isn't, because the average is the wrong number. Space Mountain at 08:28 costs
**12.4 minutes against a daily mean of 36.2**. Across that Saturday the optimizer
spends **468 minutes queueing against a 619-minute sum of means** — sequencing
alone saves **151 minutes**, about five attractions' worth.

That gap *is* the value of treating this as a time-dependent problem. A static
model would have concluded, with confidence and a clean derivation, that the day
was impossible.

It also explains the structure of the whole problem. Nearly every ride is
cheapest at opening, and you can only be in one place at 09:00. The scarce
resource is not time in general — it is the early hours specifically, and the
optimizer is really deciding who gets them.

<!-- chart:timing_gain -->

### What a count-maximizer throws away

Maximizing the *number* of attractions is exactly what was asked for, and it
produces a day most people would hate. Across the 31 dates:

| | Skipped | Mean wait | Ride |
|---|---|---|---|
| TRON Lightcycle / Run | 26/31 | 59.7 | 1.0 |
| Seven Dwarfs Mine Train | 19/31 | 48.2 | 3.0 |
| **WDW Railroad – Main Street** | **16/31** | 17.5 | **20.0** |
| Jungle Cruise | 12/31 | 32.5 | 10.0 |
| Peter Pan's Flight | 12/31 | 41.5 | 3.0 |
| **The Hall of Presidents** | **8/31** | 15.2 | **23.0** |

The headliners go, as anyone would predict: the nine longest-wait rides cost
**353 minutes of queueing**, while the nine shortest cost **74 minutes for the
same ride count**. Trading TRON for the Tiki Room buys you four more attractions,
and a count-maximizer will do it every time.

But the more interesting casualties are the railroad and the Hall of Presidents,
which have *modest* queues and are discarded for their **length**. The objective
prices the total time a visit consumes, so a 23-minute show is as expensive as
Seven Dwarfs' 48-minute line. That is also the sharpest argument for getting ride
durations right: under a flat five-minute assumption those attractions look cheap,
stay in the plan, and the answer is wrong with nothing in the output to reveal it.

If you want a day worth having rather than a high score, the objective needs to
change. That is a feature of objective design, not a bug in the solver — but it
is worth seeing stated in numbers.

<!-- chart:distribution -->

### Changing the objective, and what it costs

So the planner now asks first. You name up to five attractions you will not leave
without, **in priority order**, choose when to eat and for how long, and optionally
add the fireworks and the parade. Those become hard commitments; maximizing the
count becomes the *filler*, which is the job it is actually good at.

Making the named attractions mandatory is the easy half. The instructive half is
that it is not sufficient. With the objective still
`(commitments met, −count, finish)`, a 15-hour Saturday with five headliners
chosen produces this:

```
15:44  Jungle Cruise             34.4        08:18  Peter Pan's Flight        22.1
16:36  Peter Pan's Flight        45.7        08:53  TRON Lightcycle / Run     39.4
17:27  Haunted Mansion           29.2   ->   10:09  Haunted Mansion           14.9
18:10  Seven Dwarfs Mine Train   55.7        10:37  Seven Dwarfs Mine Train   42.3
19:16  TRON Lightcycle / Run     76.7        21:55  Jungle Cruise             11.0
        241.6 minutes queueing                      129.7 minutes queueing
```

Both plans ride **25** attractions. The left-hand one gets every commitment and
still gives terrible advice: it spends the cheap morning on walk-on rides, because
that maximizes the count, and pays for it with a 77-minute TRON queue at 19:16.
The fix is a second lexicographic term — queueing *on the named attractions* —
ahead of the count. That single change cuts it to **129.7 minutes, a 46%
reduction, for no loss of attractions at all.**

The term is bucketed to 5-minute steps rather than compared exactly, and that
detail matters: minimizing must-do queueing strictly would trade any number of
extra attractions for one saved minute. Materially cheaper commitments win; among
roughly-equal ones, the fuller day wins.

The right-hand plan also shows the model reasoning about something it was never
told: Jungle Cruise is deliberately held to 21:55, where it costs 11 minutes
against a 33-minute daily average. Waits are cheap at *both* ends of the day, and
with only one cheap morning to spend, the expensive commitments get it.

### So which factors leave you riding the least?

The obvious hypothesis is crowds: go on a quiet day, ride more. The data says
otherwise, and three independent methods agree.

On the real calendar the question can't even be asked cleanly, because weekday
and window length are confounded by construction — Friday closes at 18:00 on 12
of 18 dates and Saturday at 23:00 on 15 of 18, so "Friday" and "short day" are
nearly the same observation. So the analysis also runs a factorial grid of 588
combinations that the calendar never produces: long Fridays, short Saturdays, and
crowd levels varied independently of the weekday they came from.

Separating those two required care. A weekday carries both a *shape* (when its
peaks fall) and a *level* (how busy it is overall); leaving them merged means
measuring one variable twice and calling the result a decomposition. Each
weekday's own level is divided out and the target level multiplied back in, which
is asserted in a test — after normalization, all seven weekdays have identical
effective levels.

**Variance decomposition**, restricted to the realistic crowd range:

| Factor | η² |
|---|---|
| **Window length** | **0.930** |
| Opening hour | 0.020 |
| Crowd level | 0.019 |
| **Weekday shape** | **0.004** |

**The exchange rate.** An extra hour of park time is worth 1.16 attractions. The
entire spread between the quietest weekday and the busiest is worth **0.76**. In
other words:

> The whole difference between Magic Kingdom's quietest and busiest day of the
> week is worth **39 minutes of opening hours**. An after-hours party costs you
> five to six.

**Shapley attribution**, computed per date and arriving independently at the same
place: **90.4% window length, 9.6% crowd level.**

The answer to "which combination of factors leaves you riding the least" is
therefore an after-hours party night, whatever the day of the week. Those
evenings are excellent if you hold a party ticket. If you don't, the park closes
at six and takes a third of your day with it.

<!-- chart:cliff -->

<!-- chart:variance -->

### How much of this is proven

A heuristic without a bound is a number with no error bar, so there are two
instruments.

An **exact dynamic program** solves reduced instances outright, and the heuristic
matched its optimum on **every binding subproblem tested**, up to 20 attractions
against the real nine-hour window. "Binding" is doing real work in that sentence:
if the window comfortably fits the whole subset, the exact answer is just the
subset size and matching it proves nothing, so the subproblems are deliberately
built from the longest-wait attractions and the tests assert they are hard before
trusting the match. The dynamic program is itself checked against exhaustive
enumeration of every ordering of every subset on a six-attraction instance.

The dynamic program is only *valid* because waits are interpolated between hour
midpoints rather than treated as hourly steps. Its whole logic is that for a given
set of visited rides, leaving earlier is never worse — and with step functions
that is false. Arriving at 18:01 can beat arriving at 17:59, so a later arrival
can produce an *earlier* departure. Measured: under a step model there are **957
such violations, the worst worth 40 minutes**. A solver would find that and
"save" forty minutes by loitering at a ride entrance. Under linear interpolation
there are zero.

A **relaxation bound** covers the full instances, but it is honest about being
loose. It proves optimality on only 5 of 31 dates — all of them long days where
the count reaches the whole roster and no certificate was needed anyway. On a
nine-hour day it admits 26 where the heuristic finds 23.

So the honest claim is: **23 is what a good heuristic finds, with a valid ceiling
of 26.** It is not proven optimal. What supports it is evidence that this
heuristic doesn't fall short on hard subproblems, not a certificate.

<!-- chart:fifo -->

### The prediction that failed

Five predictions were written down before the sweep ran. Four held. This one
didn't: an extra hour was predicted to buy 1.5–2.5 attractions, and it buys
**1.16**. The direction and the ranking of factors survive; the magnitude was
optimistic. It is recorded as refuted rather than quietly accommodated, because
a pre-registered prediction that can be widened after the fact was never a
prediction.

---

## Part 6 — What this doesn't do

**Weather is not in the model.** Not because it doesn't matter, but because the
data can't support it yet: weather readings are pruned on the same 35-day cycle
as raw waits, leaving 36 days against 93 days of wait history, split 30 rainy to
6 dry. In a Florida summer the *dry* days are the rare class, so there is no
usable control group. Fixing it means adding a durable weather rollup and
backfilling from a historical archive. Until then, every number here describes a
day with the weather averaged out and no claim is made about rain.

**Ride durations are estimates**, and they are the model's most sensitive input.
Scaling them ±50% moves the count by ±3 attractions out of 23. Walking speed, by
contrast, is nearly inert at ±1 — which is the opposite of what I expected, since
walking speed is the assumption the source data warns loudest about.

**Three months is not a year.** Monthly mean waits across the sample vary by
about 15%, and the model cannot extrapolate into a season it has never seen. The
honest form of every claim is "under the summer-2026 wait regime", not "on 16
October you will ride 24 things".

**Averages are not experiences.** The model uses mean waits, so it describes a
typical day rather than your day. A breakdown, a parade closing a walkway, or one
unusually long queue will beat the plan.

**Show times are a single observation, not history.** The parade and the fireworks
are now schedulable, but the feed publishes performance times for *today only* —
`/entity/{show_id}/schedule` returns nothing at all — so a future date's times
cannot be looked up anywhere. Capture started on first poll and accrues forward;
until enough dates land, the planner runs on one observed reference and says so on
every plan it draws. It also routes you to the show by aliasing it to the nearest
routable attraction, 100 m away for the fireworks and 20 m for the parade, because
shows are geocoded but are not in the walking graph.

**The fireworks are unavailable on half the calendar**, and that is a fact about
the park rather than the model: 16 of the next 32 Magic Kingdom dates are Halloween
party nights that close at 18:00 for a regular ticket, with the show running inside
the separately ticketed evening. The planner refuses those rather than planning a
day you have not bought.

**Lunch costs time but not walking.** The plan keeps your chosen window free of
queueing and assumes you eat wherever you happen to be. Quick service is spread
across the park so this is a fair approximation, but a sit-down reservation on the
other side of the park is not modelled.

**Lightning Lane doesn't exist here.** Paid queue-skipping would change the
answer substantially and is not modelled. Worth noting for anyone building
something similar: the feed also publishes the vendor's own hourly wait *forecast*
and Lightning Lane return windows, and this pipeline still throws both away. The
forecast would make an excellent benchmark to score a model against. Capturing it
is a small change that nobody has made yet, and the history would only accrue
forward from the day it was — which is exactly what happened with show times.

**Four attractions are excluded** because they have no queue and no wait history:
Casey Jr. Splash 'N' Soak Station, A Pirate's Adventure, Cinderella Castle, and
Main Street Vehicles. A fifth, Carousel of Progress, is in refurbishment and so
sits outside the forward-looking roster of 30.

---

## Reproducing this

```bash
.venv/bin/python -m research_project.cli plan --date 2026-10-03 --explain
.venv/bin/python -m research_project.cli prove --n 16 --window 240
.venv/bin/python -m research_project.cli sweep
.venv/bin/python -m research_project.webapp.app        # 127.0.0.1:3467
```

Everything is seeded and deterministic. Every result row carries a hash of the
assumptions that produced it, so a sweep cannot silently mix constants — which is
the usual way a parameter study invalidates itself without anyone noticing.

Walking distances © OpenStreetMap contributors.
