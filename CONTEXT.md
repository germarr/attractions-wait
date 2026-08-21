# Attractions Wait-Time Dashboard

A webapp that polls themeparks.wiki for live attraction wait times at the
Universal Orlando Resort, stores each reading, and visualizes how waits move
over the day, week, and month.

## Language

**Destination**:
A top-level resort the app polls each minute (e.g. Universal Orlando Resort,
Walt Disney World Resort). Each Destination's feed yields its Parks and
Attractions. The app tracks several — Parks belong to exactly one Destination.
_Avoid_: Resort (in code/identifiers — use "Destination" to match the API)

**Park**:
A gated property inside the Destination (e.g. Universal Studios Florida,
Islands of Adventure, Volcano Bay, Epic Universe). Identified by `parkId`.

**Attraction**:
A single ride that reports a standby wait time. The unit a Reading is about.
_Avoid_: Ride, entity (the API's `ATTRACTION` entityType; excludes `SHOW`)

**Water Park**:
A swimming/slide park within a Destination (Volcano Bay, Typhoon Lagoon,
Blizzard Beach). Excluded from the app — only the main theme Parks are tracked.

**Reading**:
One observation of one Attraction's standby wait time at one moment, captured
from a poll. The atomic row the charts are built from.
_Avoid_: Sample, datapoint, record

**Wait Time**:
The standby queue wait in minutes for an Attraction, from `queue.STANDBY.waitTime`.
Only present when the Attraction is operating and has a standby queue.
_Avoid_: Waiting time, queue length

**Show**:
A scheduled performance entity in the feed (`entityType: SHOW`). Has showtimes,
not a standby wait, and is therefore excluded from this app entirely.

**Park Average**:
A synthetic "attraction" the dashboard offers per Park: at each moment, the mean
standby Wait Time across that Park's operating Attractions. Lets the same
per-attraction charts express overall Park busyness.
_Avoid_: Park pulse, park-wide wait

**Park Roster**:
The set of a Park's Attractions considered "present" right now: the distinct
Attractions in the latest poll, **excluding long-term REFURBISHMENT**. Serves as
both the count of Attractions a Park "has" and the denominator of "open" on the
Park Comparison page. Mirrors Downtime's rule that a months-long refurbishment
isn't something a guest expects open today.
_Avoid_: all tracked Attractions (the Attraction table keeps removed/ghost rides
forever; the Roster is the live, refurb-pruned subset)

**Operating Hours**:
The daily open/close times of a Park, from the /schedule endpoint. Define when
an Attraction is *expected* to be available. All wait charts, cards, and the
Downtime calculation are bounded to Operating Hours — readings outside them
(e.g. the API's stale post-close waits) are stored but excluded at query time.
_Avoid_: Park hours, schedule (those name the data; this is the concept)

**Downtime**:
Minutes an Attraction was unavailable (status DOWN or CLOSED) **while its Park
was within Operating Hours** — i.e. unavailable when a guest would expect it.
Excludes outside-hours closures and long-term REFURBISHMENT. Derived from the
per-poll status on Readings against Operating Hours; never stored separately.
Minutes come from the elapsed gap between Readings, not a row count, so the
figure survives a change of poll interval (ADR-0006).
_Avoid_: Closure, outage (outside-hours CLOSED ≠ Downtime)

**Downtime Rate**:
Downtime expressed as a percentage of a Park's Operating Hours over a window
(Week / Month / Year / Historic) — downtime minutes ÷ operating minutes. The
length-independent, comparable form of Downtime used in the reliability table.
_Avoid_: Downtime total (raw minutes aren't comparable across windows)

**Park Downtime Rate**:
A Park's Downtime aggregated across its Roster: total down readings ÷ total
operating readings across all the Park's Attractions over a window, as a
percentage. A ratio of reading counts, so it is independent of the poll interval
(ADR-0006). **Pooled** (attraction-reading weighted), not a mean of
per-attraction Downtime Rates — so a chronically-down headliner moves it more
than a flaky kiddie ride. The reliability bar on the Park Comparison page.
_Avoid_: mean of per-attraction rates (dilutes one bad ride across a big Roster)

**Downtime z-score**:
How abnormal today's Downtime Rate is for an Attraction: today's rate minus the
mean of the trailing month's daily rates, divided by their standard deviation.
+2 ≈ an unusually bad day; near 0 ≈ typical; negative ≈ unusually reliable.
_Avoid_: z-index (the CSS term — this is a statistical z-score)

**WoW / MoM / YoY**:
A period-over-period change in an Attraction's current Wait Time vs the same
time-of-day a fixed, weekday-aligned span earlier: WoW = 7 days ago, MoM = 28
days (4 weeks), YoY = 364 days (52 weeks). Expressed as a % change against the
reference hour's mean. Weekday alignment matters — wait patterns differ by day.
_Avoid_: month-over-month meaning calendar months (we use 28 days to hold weekday)

**Crowd Index**:
How busy a Park is *relative to its own normal*: for each operating Attraction,
its current Wait Time ÷ its baseline wait for the same local hour-of-day (mean
over recent history — a rolling ~90 days, ample for an hour-of-day mean while
bounding query cost as the DB grows); the Park's index is the mean of those ratios
across operating Attractions, as a percentage (100% = a typical day, 130% =
unusually busy). Unlike the raw avg-wait bar it strips out roster composition,
so Parks compare fairly. Baseline is weekday-agnostic for now — weekday
alignment (as in WoW/MoM/YoY) is a future refinement once history accrues.
_Avoid_: crowd level meaning raw average wait (that's roster-biased; this is normalized)

**Headliner Wait**:
The mean of a Park's five longest operating standby Wait Times at a moment — the
"how bad does it actually get here" signal, robust to a Park diluting its average
with walk-on family rides. Shown live (current) and, over a window, as the mean
of the per-poll top-5 mean.
_Avoid_: max wait (a single ride is noisy; top-5 is steadier)

**Momentum**:
The live direction of a Park's busyness: the change in its Park Average over the
last ~60 minutes (median of the last ~10 min minus the median around 60 min
ago), in minutes. Positive = filling up, negative = emptying. A purely live
signal — never windowed.
_Avoid_: trend (unqualified; this is the short-term live slope only)

**Live Trace**:
The raw, minute-by-minute standby Wait Time for a target over a rolling recent
window (last 90 minutes) — a single unaggregated line, distinct from the
bucketed Today/Week/Month views. Shows current movement at full resolution.
_Avoid_: Live chart, recent series

**Weather Reading**:
One observation of a Park's weather (temperature, precipitation, condition) at a
poll minute, sharing its timestamp with the Wait Time Readings of that minute.
The weather counterpart of a Reading.

**Weather Event**:
The categorical weather condition at a Park — clear, cloudy, fog, drizzle, rain,
showers, thunderstorm — carried as a WMO weather code on a Weather Reading.
_Avoid_: Condition, weather type

**Raining**:
A derived yes/no for a Park at a moment: true when its Weather Event is a
rain/shower/thunderstorm condition or measured precipitation is above zero.
Computed from a Weather Reading, never stored.
