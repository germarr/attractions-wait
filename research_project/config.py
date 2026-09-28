"""Constants and the swept assumption set for the route optimizer.

Every invented number in this package lives here. Nothing else reads a
module-level tuning constant at call time: tunables are threaded through the
frozen `Assumptions` dataclass and every result row carries `assumptions.key()`,
so a sweep can never silently mix results computed under different constants.

`Assumptions.provenance()` classifies each value as measured / ADR / estimate /
swept, which is what the article's methods paragraph is generated from.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping
from zoneinfo import ZoneInfo

# ── the park ──────────────────────────────────────────────────────────────

PARK_ID = "75ea578a-adc8-4116-a54d-dccb60765ef9"  # Magic Kingdom
PARK_TZ = ZoneInfo("America/New_York")

# ── history window ────────────────────────────────────────────────────────

# attractionhourly runs 2026-06-24..2026-09-24. Two dates are unusable:
#
#   2026-06-24 — no OPERATING row in parkschedule, so rollup.py's
#                _within_operating_hours passed the day through UNFILTERED
#                (app/stats.py:122-149 deliberately does that rather than blank a
#                chart) and its rows carry the API's post-close stale waits.
#   2026-08-04 — the poll-interval changeover itself (ADR-0006). Mean n_wait per
#                row is 26.1 that day, between 58.4 on 08-03 and 11.2 on 08-05,
#                so the day is internally mixed and no per-date interval constant
#                describes it.
#
# Note the exclusion is on the *missing schedule row*, never on the hour: hour-23
# rows are legitimate on 2026-07-04, which really did run 08:00 -> 00:00.
HISTORY_START = "2026-06-25"
EXCLUDED_DATES = frozenset({"2026-06-24", "2026-08-04"})
POLL_CHANGE_DATE = "2026-08-04"  # ADR-0006; documentation only, never a branch

# Hours the model reasons about. 8 exists (25 forward dates open at 08:00) and
# so do 22-23 on party and holiday nights, so the range is wider than the dense
# 9-21 core; thin hours are handled by the estimator's fallback chain.
MODEL_HOURS = range(7, 24)

WEEKEND = frozenset({5, 6})  # Python weekday(): Sat, Sun

# ── estimator thresholds ──────────────────────────────────────────────────

# Dates required before a (attraction, weekday, hour) cell is used directly.
# Measured over hours 9-21: 2,786 cells, min 1 date, p10 6, median 13, max 14.
# Demotions by threshold: 3 -> 93 cells (3.3%), 5 -> 97 (3.5%), 6 -> 113 (4.1%),
# 8 -> 333 (12.0%). The ceiling is 13-14 dates, so no threshold buys much
# precision; 5 halves the standard error against a 1-date cell while demoting
# only 3.5% of cells, where 8 costs 12% of the direct evidence for very little.
MIN_CELL_DATES = 5

# Drop a (date, hour) observation built from less than half an hour of readings.
# These come from partial hours at the window edges and from downtime inside the
# hour; a 20-minute slice is a mean over an unrepresentative sub-hour and the
# unweighted mean-of-means would give it equal weight to a full hour. Induced
# bias, stated plainly: surviving cells are conditioned on the ride being up for
# most of the hour, so the model assumes an operating ride.
MIN_HOUR_COVERAGE = 0.5

# ── geography ─────────────────────────────────────────────────────────────

# The park entrance is aliased to this attraction rather than given its own
# coordinate. Its station sits over the entrance turnstiles, so every
# entrance-to-ride leg comes straight out of `attractiondistance` — same OSM
# graph, same triangle inequality, no invented geometry. park.latitude/longitude
# is 60 m away and would also have worked, but it is an unverified centroid that
# would need snapping into the graph.
ENTRANCE_PROXY_NAME = "Walt Disney World Railroad - Main Street, U.S.A."

# ── ride durations: ESTIMATES, not data ───────────────────────────────────

# Confirmed absent from the database, the repository, and the themeparks.wiki API
# at every endpoint app/themeparks.py touches. These are editorial estimates of
# on-ride/in-show time in minutes, excluding the queue.
#
# This table is load-bearing, not a detail. Roughly ten of the roster are theater
# or transport attractions with tiny posted waits and long durations — Hall of
# Presidents, Carousel of Progress, Enchanted Tales with Belle, the railroad
# circuits. Assuming a flat 5 minutes understates a full day by 100-150 minutes,
# which is exactly the margin that decides whether "all 31" looks feasible. Every
# headline result is therefore reported alongside a `ride_scale` sweep.
#
# Correct any value you have better information for; the sensitivity pass exists
# to show how much the conclusions depend on them.
DEFAULT_RIDE_MINUTES = 5.0

RIDE_MINUTES: Mapping[str, float] = {
    # theater and transport — long, and the reason a flat constant fails
    "The Hall of Presidents": 23.0,
    "Walt Disney's Carousel of Progress": 21.0,
    "Enchanted Tales with Belle": 20.0,
    "Walt Disney World Railroad - Main Street, U.S.A.": 20.0,
    "Walt Disney World Railroad - Fantasyland": 20.0,
    "Monsters, Inc. Laugh Floor": 15.0,
    "Mickey's PhilharMagic": 12.0,
    "Country Bear Musical Jamboree": 11.0,
    "Walt Disney's Enchanted Tiki Room": 10.0,
    "Tomorrowland Transit Authority PeopleMover": 10.0,
    "Swiss Family Treehouse": 10.0,  # self-paced walkthrough
    # boat and dark rides
    '"it\'s a small world"': 11.0,
    "Jungle Cruise": 10.0,
    "Pirates of the Caribbean": 8.5,
    "Haunted Mansion": 8.5,
    "Under the Sea - Journey of The Little Mermaid": 6.0,
    "Tiana's Bayou Adventure": 5.0,
    "Tomorrowland Speedway": 5.0,
    "Buzz Lightyear’s Space Ranger Spin": 4.5,
    "The Many Adventures of Winnie the Pooh": 4.0,
    "Big Thunder Mountain Railroad": 3.5,
    "Peter Pan's Flight": 3.0,
    "Seven Dwarfs Mine Train": 3.0,
    "Space Mountain": 2.5,
    # short spinners and coasters
    "Prince Charming Regal Carrousel": 2.0,
    "Dumbo the Flying Elephant": 2.0,
    "Astro Orbiter": 2.0,
    "Mad Tea Party": 1.5,
    "The Magic Carpets of Aladdin": 1.5,
    "The Barnstormer": 1.0,
    "TRON Lightcycle / Run": 1.0,
}

# ── scheduled entertainment ───────────────────────────────────────────────

# The two Magic Kingdom shows the planner can schedule around. Each is aliased to
# the nearest Attraction in the walking graph, the same device that aliases the park
# entrance to the Main Street railroad station: shows are geocoded but are not in
# `attractiondistance`, and the alias costs less error than the walking estimate
# itself carries. Offsets measured by haversine against the show's own coordinates.
#
# `watch_minutes` and `arrive_early_min` are ESTIMATES, not data. The feed publishes
# when a show starts and nothing about how long you must hold a spot to see it. They
# are in the assumption ledger and exposed in the web app so their influence can be
# seen rather than argued about — the same treatment ride durations get.
@dataclass(frozen=True)
class ShowSpec:
    """One schedulable performance, and how it attaches to the walking graph."""

    key: str
    name: str  # exact `show.name` as the feed publishes it
    proxy_attraction: str  # the graph node its travel costs are taken from
    proxy_offset_m: float  # how far the alias actually is, for honest reporting
    watch_minutes: float
    arrive_early_min: float  # how early you must be standing there to see it
    needs_late_close: bool  # refuse on party nights, when the park shuts at 18:00


SHOWS: Mapping[str, ShowSpec] = {
    "fireworks": ShowSpec(
        key="fireworks",
        name="Happily Ever After",
        # Cinderella Castle sits 32 m from the viewing hub and would be the
        # natural alias, but it reports no standby wait so it is not in the roster
        # at all. The nearest node that IS routable is Mickey's PhilharMagic.
        proxy_attraction="Mickey's PhilharMagic",
        proxy_offset_m=100.0,
        watch_minutes=18.0,
        # A hub spot goes early; 45 minutes is the common advice and is a guess.
        arrive_early_min=45.0,
        # On a party night the park closes at 18:00 for a regular ticket and the
        # show runs inside the separately-ticketed event, so it cannot be planned.
        needs_late_close=True,
    ),
    "parade": ShowSpec(
        key="parade",
        name="Disney Festival of Fantasy Parade",
        proxy_attraction="Country Bear Musical Jamboree",
        proxy_offset_m=20.0,
        # The parade takes ~45 min to run its route but passes one spot in ~12.
        watch_minutes=12.0,
        arrive_early_min=20.0,
        needs_late_close=False,
    ),
}

# Seeded reference times, park-local minutes from midnight, used only when no
# showtime has been captured for the date or its weekday. Observed once, from the
# live feed on 2026-09-26 — a single observation, labelled as such everywhere it is
# used, and superseded automatically as `showtime` history accrues (ADR-0010).
SHOW_REFERENCE_MINUTES: Mapping[str, float] = {
    "fireworks": 21 * 60 + 30,  # 21:30 observed 2026-09-26 (park closed 23:00)
    "parade": 14 * 60,  # 14:00 observed 2026-09-26
}
SHOW_REFERENCE_DATE = "2026-09-26"

# How many dates a (show, weekday) cell needs before its modal time is preferred
# over the seeded reference. Low because a show's time is near-constant; two
# agreeing observations are already better evidence than one.
MIN_SHOWTIME_DATES = 2

SEED = 20260925


@dataclass(frozen=True)
class Assumptions:
    """Every tunable, in one immutable bundle threaded through the model.

    Frozen so a result row's `key()` provably describes the numbers that produced
    it. Add a field here rather than reading a module constant deeper in the
    package, or the sweep stops being trustworthy.
    """

    # walking
    walk_speed_mps: float = 1.1
    transition_overhead_min: float = 1.0  # exit, re-orient, find the queue entry
    start_overhead_min: float = 10.0  # gates, bag check, tapstiles to Main Street

    # scaling knobs the sweep moves
    ride_scale: float = 1.0
    posted_wait_factor: float = 1.0  # posted vs actual queue time
    crowd_multiplier: float = 1.0  # busier or quieter than the measured mean

    # scheduled commitments
    #
    # Deliberately NOT scaled by ride_scale or crowd_multiplier: a 45-minute wait
    # for the fireworks is a decision the visitor made, not an estimate of queue
    # behaviour, so it must not move when those sliders do. `build_instance`
    # exempts the pseudo-stops explicitly.
    show_arrive_early_scale: float = 1.0  # scales every show's arrive_early_min
    lunch_minutes: float = 45.0
    # Granularity at which must-do queueing beats a fuller day. At 0 the planner
    # would trade any number of extra attractions for one saved minute on a
    # must-do; at a large value it stops caring when the headliners are ridden.
    must_do_wait_bucket_min: float = 5.0

    # modelling choices
    queue_closes_at_close: bool = True
    roster_mode: Literal["history", "open_today"] = "open_today"
    min_cell_dates: int = MIN_CELL_DATES

    # Linear interpolation is a CORRECTNESS requirement, not smoothing. A step
    # function makes arriving at 18:01 cheaper than 17:59, so a later arrival can
    # yield an earlier departure — the model stops being FIFO, which invalidates
    # the (visited_set, last) -> earliest-completion dominance the exact DP rests
    # on, and gives local search artificial cliffs to hunt. Measured worst
    # hour-to-hour change is 22.8 min per 60 (|dw/dt| <= 0.38, median 1.2), so
    # under linear interpolation depart(t) = t + w(t) + R is strictly increasing
    # and the DP is sound. "step" is kept only to demonstrate the artifact.
    interpolation: Literal["linear", "step"] = "linear"

    def key(self) -> str:
        """Short stable hash; every sweep result row carries it."""
        blob = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def provenance(self) -> list[tuple[str, Any, str]]:
        """(name, value, source) per assumption, for the article's methods note."""
        sources = {
            "walk_speed_mps": "estimate (swept)",
            "transition_overhead_min": "estimate",
            "start_overhead_min": "estimate",
            "ride_scale": "swept",
            "posted_wait_factor": "swept",
            "crowd_multiplier": "swept",
            "show_arrive_early_scale": "estimate (swept)",
            "lunch_minutes": "user input",
            "must_do_wait_bucket_min": "modelling choice (swept)",
            "queue_closes_at_close": "modelling choice",
            "roster_mode": "measured (refurbishment status)",
            "min_cell_dates": "measured (cell density)",
            "interpolation": "required for FIFO; see field comment",
        }
        return [(k, v, sources[k]) for k, v in asdict(self).items()]


def ride_minutes(name: str, *, scale: float = 1.0) -> float:
    """Estimated on-ride minutes for an attraction, scaled by the sweep factor."""
    return RIDE_MINUTES.get(name, DEFAULT_RIDE_MINUTES) * scale
