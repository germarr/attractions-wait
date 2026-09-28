"""The routing instance and its feasibility forward pass.

An `Instance` bakes everything the solver needs into flat Python lists: the
travel matrix, and per attraction an `arrival minute -> departure minute` table
that already folds in the wait and the ride. The solver then never touches numpy
or the database — its inner loop is list indexing, which is roughly an order of
magnitude faster than numpy scalar access at this size and is where essentially
all of the runtime goes.

Two modelling decisions are encoded here rather than left implicit.

**The wait is evaluated once, at the arrival minute, and never re-integrated
while queueing.** A posted wait is by definition an estimate of "how long this
will take if you join now", which is exactly what `attractionhourly` measures. A
model that re-evaluated the wait as the queue advanced would be double-counting
the forecast already baked into the number. A useful consequence: a wait
straddling an hour boundary is a non-question, because the only hour that matters
is the one you arrive in — and under linear interpolation there are no hour
buckets to straddle anyway, just a continuous curve sampled at the arrival minute.

**`queue_closes_at_close` decides what "fits" means.** With it true (the default)
you must *join* the queue by closing time and may ride after — which is how parks
actually work, since the queue closes and the line drains. The strict alternative
requires the whole visit to finish by close and costs roughly one attraction.
Both are in the assumption ledger; the article must say which it used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Sequence

from research_project import config, travel as travel_mod
from research_project.db import DayWindow, RosterAttraction
from research_project.waits import Tier, WaitTable

# Tables run past midnight so a 00:00 close (Magic Kingdom really does have them,
# and 2026-07-04 ran 08:00 -> 00:00) is an index rather than a special case.
TABLE_MINUTES = 1500


@dataclass(frozen=True)
class Stop:
    """One visit, fully resolved in time."""

    index: int  # position in Instance.attractions
    name: str
    arrive: float
    wait: float
    board: float
    ride: float
    leave: float
    tier: Tier
    # "ride" for an attraction, "show" for a scheduled performance. A show occupies
    # time and space exactly like a ride but is not an attraction you visited, so
    # nothing that counts attractions may count it.
    kind: str = "ride"
    must_do: bool = False


@dataclass(frozen=True)
class Instance:
    """One park-day, with every input the solver needs pre-baked."""

    park_id: str
    day: DayWindow
    attractions: tuple[RosterAttraction, ...]
    travel_minutes: tuple[tuple[float, ...], ...]  # (n+1, n+1); node 0 = entrance
    depart_table: tuple[tuple[float, ...], ...]  # (n, TABLE_MINUTES)
    wait_table: tuple[tuple[float, ...], ...]  # (n, TABLE_MINUTES), already factored
    tier_table: tuple[tuple[str, ...], ...]  # (n, TABLE_MINUTES)
    start_minute: float
    assumptions: config.Assumptions

    # Per-index metadata for scheduled commitments. All defaulted, so every
    # existing construction site is unchanged and a plain count-maximizing instance
    # behaves exactly as it did.
    kinds: tuple[str, ...] = ()  # "ride" | "show", parallel to `attractions`
    latest_arrival: tuple[float, ...] = ()  # inf unless the stop is time-pinned
    # Intervals during which no stop may be in progress — the lunch break. Not a
    # node: a zero-distance node would break the triangle inequality that
    # `bounds.travel_lower_bound` and the exact DP's dominance argument rest on.
    blackouts: tuple[tuple[float, float], ...] = ()
    required: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        # Defaults are filled in here rather than at every call site, so an
        # Instance built the old way is still internally consistent.
        n = len(self.attractions)
        if not self.kinds:
            object.__setattr__(self, "kinds", ("ride",) * n)
        if not self.latest_arrival:
            object.__setattr__(self, "latest_arrival", (float("inf"),) * n)

    @property
    def n(self) -> int:
        return len(self.attractions)

    @property
    def ride_indices(self) -> tuple[int, ...]:
        """Indices that are real attractions, excluding scheduled shows."""
        return tuple(i for i, kind in enumerate(self.kinds) if kind == "ride")

    @property
    def n_rides(self) -> int:
        """The roster size a count is measured against. Shows do not inflate it."""
        return len(self.ride_indices)

    @property
    def show_indices(self) -> tuple[int, ...]:
        return tuple(i for i, kind in enumerate(self.kinds) if kind == "show")

    @property
    def committed_minutes(self) -> float:
        """Time the day owes to breaks, before any attraction is considered."""
        return sum(end - start for start, end in self.blackouts)

    @property
    def close_minute(self) -> float:
        return self.day.close_minute

    def travel(self, from_node: int, to_node: int) -> float:
        """Minutes between graph nodes. Node 0 is the entrance; ride i is i+1."""
        return self.travel_minutes[from_node][to_node]

    def wait_at(self, index: int, minute: float) -> float:
        return _sample(self.wait_table[index], minute)

    def depart_at(self, index: int, arrival: float) -> float:
        return _sample(self.depart_table[index], arrival)

    def tier_at(self, index: int, minute: float) -> Tier:
        clamped = int(min(max(minute, 0.0), TABLE_MINUTES - 1))
        return self.tier_table[index][clamped]  # type: ignore[return-value]


def _sample(row: Sequence[float], minute: float) -> float:
    """Linear read between whole minutes.

    Rounding to the nearest minute instead would accumulate up to +/-0.5 min per
    stop, which over ~25 stops is +/-12 minutes — a whole attraction. Interpolating
    costs two list indexes and is exact, because the underlying wait curve is
    piecewise linear between hour midpoints.
    """
    clamped = minute
    if clamped < 0.0:
        clamped = 0.0
    elif clamped > TABLE_MINUTES - 1:
        clamped = float(TABLE_MINUTES - 1)
    low = int(clamped)
    high = low + 1 if low + 1 < TABLE_MINUTES else low
    weight = clamped - low
    return row[low] * (1.0 - weight) + row[high] * weight


def build_instance(
    day: DayWindow,
    attractions: Sequence[RosterAttraction],
    *,
    table: WaitTable,
    travel: travel_mod.TravelMatrix,
    assumptions: config.Assumptions | None = None,
    start_minute: float | None = None,
    park_id: str = config.PARK_ID,
    shows: Sequence["ShowCommitment"] = (),
    lunch: tuple[float, float] | None = None,
    required_ids: Sequence[str] = (),
) -> Instance:
    """Bake a day's wait curves and travel matrix into a solver-ready instance.

    `attractions` must be in the same order as `travel.node_ids[1:]`, since the
    solver indexes both by position; that is asserted rather than assumed.

    `shows` appends one pseudo-attraction per scheduled performance. Each borrows
    its travel costs from a proxy attraction already in the graph, the same device
    that aliases the park entrance to the Main Street railroad station — shows are
    geocoded but are not in `attractiondistance`, and the alias costs 20-32 m
    against a 403 m average leg.

    `lunch` is `(start, end)` in minutes from midnight and becomes a blackout: no
    stop may be in progress across it. It is deliberately not a node, because a
    zero-distance node violates the triangle inequality the bounds rely on.

    `required_ids` names attractions that must appear in any accepted solution.
    """
    assumptions = assumptions or config.Assumptions()

    expected = tuple(travel.node_ids[1:])
    actual = tuple(a.id for a in attractions)
    if expected != actual:
        raise ValueError(
            "attraction order does not match the travel matrix; "
            f"{len(set(expected) ^ set(actual))} node(s) differ"
        )

    factor = assumptions.posted_wait_factor * assumptions.crowd_multiplier
    weekday = day.weekday

    wait_rows: list[tuple[float, ...]] = []
    depart_rows: list[tuple[float, ...]] = []
    tier_rows: list[tuple[str, ...]] = []

    for attraction in attractions:
        index = table.index_of(attraction.id)
        curve = table.minute_curve(index, weekday)
        tiers = table.tier_curve(index, weekday)
        ride = attraction.ride_minutes * assumptions.ride_scale

        # Pad past midnight by holding the last value, matching the flat clamp
        # the wait curve already applies outside its supported hours.
        waits = [factor * float(curve[m]) for m in range(len(curve))]
        waits += [waits[-1]] * (TABLE_MINUTES - len(waits))
        tier_list = [str(t) for t in tiers]
        tier_list += [tier_list[-1]] * (TABLE_MINUTES - len(tier_list))

        wait_rows.append(tuple(waits))
        depart_rows.append(tuple(m + waits[m] + ride for m in range(TABLE_MINUTES)))
        tier_rows.append(tuple(tier_list))

    start = (
        start_minute
        if start_minute is not None
        else day.open_minute + assumptions.start_overhead_min
    )

    roster = list(attractions)
    matrix = [list(row) for row in travel.as_lists()]
    kinds = ["ride"] * len(roster)
    latest = [float("inf")] * len(roster)

    # ── scheduled shows, as pseudo-stops ─────────────────────────────────
    by_name = {a.name: i for i, a in enumerate(roster)}
    overhead = assumptions.transition_overhead_min
    for show in shows:
        if show.proxy_attraction not in by_name:
            raise ValueError(
                f"show {show.name!r} aliases {show.proxy_attraction!r}, which is not "
                "in the roster passed to build_instance"
            )
        proxy_node = by_name[show.proxy_attraction] + 1

        # The show stands where its proxy stands, so it inherits the proxy's row and
        # column verbatim. Walking from the show to its own proxy costs only the
        # transition overhead: they are ~30 m apart, which is inside the noise of
        # the walking estimate itself.
        new_row = [matrix[proxy_node][j] for j in range(len(matrix))]
        for row_index, row in enumerate(matrix):
            row.append(matrix[row_index][proxy_node])
        new_row.append(0.0)
        new_row[proxy_node] = overhead
        matrix[proxy_node][len(matrix)] = overhead
        matrix.append(new_row)

        # depart(t) = max(t, start) + watch, which is flat then slope 1 and so
        # still non-decreasing — the FIFO property the exact DP depends on. The
        # wait row must satisfy wait + ride == depart(t) - t exactly, or
        # `evaluate` (which re-derives ride from the roster) would disagree with
        # the solver's own clock and the timeline would silently lie.
        waits = [max(0.0, show.start_minute - m) for m in range(TABLE_MINUTES)]
        wait_rows.append(tuple(waits))
        depart_rows.append(
            tuple(
                max(float(m), show.start_minute) + show.watch_minutes
                for m in range(TABLE_MINUTES)
            )
        )
        tier_rows.append((show.tier,) * TABLE_MINUTES)

        # NOT scaled by ride_scale: the length of the fireworks is not an estimate
        # of queue behaviour, and must not move when that slider does.
        roster.append(
            RosterAttraction(
                id=show.node_id,
                name=show.name,
                latitude=show.latitude,
                longitude=show.longitude,
                ride_minutes=show.watch_minutes,
                refurbishment=False,
            )
        )
        kinds.append("show")
        latest.append(show.latest_arrival)

    required = frozenset(
        i for i, a in enumerate(roster)
        if a.id in set(required_ids) or kinds[i] == "show"
    )

    blackouts: tuple[tuple[float, float], ...] = ()
    if lunch is not None:
        blackouts = ((float(lunch[0]), float(lunch[1])),)

    return Instance(
        park_id=park_id,
        day=day,
        attractions=tuple(roster),
        travel_minutes=tuple(tuple(row) for row in matrix),
        depart_table=tuple(depart_rows),
        wait_table=tuple(wait_rows),
        tier_table=tuple(tier_rows),
        start_minute=start,
        assumptions=assumptions,
        kinds=tuple(kinds),
        latest_arrival=tuple(latest),
        blackouts=blackouts,
        required=required,
    )


@dataclass(frozen=True)
class ShowCommitment:
    """A scheduled performance, resolved to this day's clock and graph.

    Built from `research_project.shows.Showtime`; kept as its own type here so
    `model` does not import the database layer.
    """

    node_id: str
    name: str
    start_minute: float
    watch_minutes: float
    latest_arrival: float
    proxy_attraction: str
    tier: str
    latitude: float
    longitude: float


# ── the forward pass ──────────────────────────────────────────────────────


def forward_pass(
    instance: Instance,
    order: Sequence[int],
    *,
    start: float | None = None,
    from_stop: int = 0,
    cached_departures: Sequence[float] | None = None,
) -> tuple[list[float], int]:
    """Departure minute per stop, and the index of the first infeasible stop.

    Returns `(departures, first_infeasible)`, where `first_infeasible == len(order)`
    means the whole route fits.

    `from_stop` with `cached_departures` replays only the suffix, which is the
    correct incremental re-evaluation for a time-dependent problem and the only
    legitimate shortcut. There is no valid O(1) move delta here: a 2-opt reversal
    changes the arrival time of every stop in the reversed segment *and*
    everything after it, so the entire value of the move lives in the term an
    O(1) formula would omit. `tests/test_research_orienteering.py` asserts the
    incremental path equals the from-scratch path bit for bit.
    """
    close = instance.close_minute
    queue_closes = instance.assumptions.queue_closes_at_close
    travel_rows = instance.travel_minutes
    depart_table = instance.depart_table
    blackouts = instance.blackouts
    latest_arrival = instance.latest_arrival

    if from_stop > 0:
        if cached_departures is None or len(cached_departures) < from_stop:
            raise ValueError("from_stop requires cached_departures for the prefix")
        departures = list(cached_departures[:from_stop])
        clock = departures[-1]
        previous = order[from_stop - 1] + 1
    else:
        departures = []
        clock = instance.start_minute if start is None else start
        previous = 0

    first_infeasible = len(order)
    for position in range(from_stop, len(order)):
        index = order[position]
        arrive = clock + travel_rows[previous][index + 1]

        # A scheduled break blocks the whole interval: you cannot stand in a queue
        # while you are eating, so a stop that would still be in progress when the
        # break starts waits until it ends. This keeps depart(t) non-decreasing —
        # flat across the break, then slope 1 — so the FIFO property the exact DP's
        # dominance argument needs survives unchanged.
        if blackouts:
            arrive = _apply_blackouts(instance, index, arrive)

        # A time-pinned stop has a deadline as well as a start: you cannot attend
        # the 14:00 parade at 14:40. Unlike `close`, missing this makes only this
        # stop infeasible, and the search will drop or reorder it.
        if arrive > latest_arrival[index]:
            first_infeasible = position
            break

        leave = _sample(depart_table[index], arrive)
        limit = arrive if queue_closes else leave
        if limit > close:
            first_infeasible = position
            break
        departures.append(leave)
        clock = leave
        previous = index + 1

    return departures, first_infeasible


def objective_key(
    *, missing: int, must_do_wait: float, rides: int, finish: float, bucket: float
) -> tuple[int, int, int, float]:
    """The lexicographic sort key, smaller is better. One definition, two callers.

    `(missing commitments, bucketed must-do queueing, -rides, finish)`.

    The second term is the whole point of asking for must-dos. With only
    `(-rides, finish)` the search correctly maximizes the count and, in doing so,
    spends the cheap early morning on walk-on rides and defers the headliners to
    the evening — on a measured 15-hour Saturday it put TRON at a 77-minute queue
    and Seven Dwarfs at 56. That is the right answer to "how many rides can I fit"
    and a terrible answer to "make sure I get these five".

    The bucket is what stops it becoming a different kind of wrong. Strictly
    minimizing must-do queueing would trade any number of extra attractions for one
    saved minute, so waits are compared in `bucket`-minute steps: materially
    cheaper must-dos win, and among roughly-equal ones the fuller day wins.

    `_Scorer.score` and `Itinerary.objective` both route through here, because a
    search optimizing one key while the plan is reported against another is a bug
    that shows up only as inexplicably poor plans.
    """
    steps = int(must_do_wait // bucket) if bucket > 0 else int(must_do_wait)
    return (missing, steps, -rides, finish)


def _apply_blackouts(instance: Instance, index: int, arrive: float) -> float:
    """Push an arrival past any break it would otherwise overlap.

    Shared by `forward_pass` and `evaluate` on purpose: they compute the schedule
    twice, once for search and once for display, and a rule applied in only one of
    them is a timeline that disagrees with the plan it is drawing.
    """
    for b_start, b_end in instance.blackouts:
        if arrive < b_end and _sample(instance.depart_table[index], arrive) > b_start:
            arrive = b_end
    return arrive


def longest_feasible_prefix(instance: Instance, order: Sequence[int]) -> int:
    """How many leading stops of `order` fit inside the day."""
    _, first_infeasible = forward_pass(instance, order)
    return first_infeasible


@dataclass(frozen=True)
class Itinerary:
    """A resolved route: the order, every stop's timing, and whether it fits."""

    instance: Instance
    order: tuple[int, ...]
    stops: tuple[Stop, ...]
    feasible: bool
    violation: str | None

    @property
    def count(self) -> int:
        """Attractions visited. A scheduled show is not an attraction.

        Deliberately not `len(self.stops)`: a plan with the fireworks in it has one
        more stop but has not ridden one more ride, and every count, bound and
        optimality claim in the package is about rides.
        """
        return sum(1 for stop in self.stops if stop.kind == "ride")

    @property
    def n_stops(self) -> int:
        """Every scheduled stop, shows included — what the timeline draws."""
        return len(self.stops)

    @property
    def must_do_count(self) -> int:
        return sum(1 for stop in self.stops if stop.must_do)

    @property
    def missing_required(self) -> tuple[int, ...]:
        """Required indices the route failed to include."""
        scheduled = {stop.index for stop in self.stops}
        return tuple(sorted(self.instance.required - scheduled))

    @property
    def finish_minute(self) -> float:
        return self.stops[-1].leave if self.stops else self.instance.start_minute

    @property
    def must_do_wait(self) -> float:
        """Queueing spent on the attractions the visitor named."""
        return sum(stop.wait for stop in self.stops if stop.must_do)

    @property
    def objective(self) -> tuple[int, int, int, float]:
        """Lexicographic sort key, smaller is better. See `objective_key`.

        The count alone plateaus — on a long day most moves are neutral and local
        search has no gradient at all. Breaking ties on the earliest finish turns
        the plateau into a descent direction, and makes the result expressible:
        "all 30, with 52 minutes to spare" says more than "30".
        """
        return objective_key(
            missing=len(self.missing_required),
            must_do_wait=self.must_do_wait,
            rides=self.count,
            finish=self.finish_minute,
            bucket=self.instance.assumptions.must_do_wait_bucket_min,
        )

    @property
    def walking_minutes(self) -> float:
        total = 0.0
        previous = 0
        for stop in self.stops:
            total += self.instance.travel(previous, stop.index + 1)
            previous = stop.index + 1
        return total

    @property
    def waiting_minutes(self) -> float:
        """Queueing for rides. Holding a spot for a show is reported separately."""
        return sum(stop.wait for stop in self.stops if stop.kind == "ride")

    @property
    def riding_minutes(self) -> float:
        return sum(stop.ride for stop in self.stops if stop.kind == "ride")

    @property
    def show_minutes(self) -> float:
        """Time spent holding a spot for a show and then watching it."""
        return sum(
            stop.wait + stop.ride for stop in self.stops if stop.kind == "show"
        )

    @property
    def break_minutes(self) -> float:
        return self.instance.committed_minutes

    def support(self) -> dict[str, float]:
        """Provenance of the wait estimates this itinerary actually used.

        More honest than the table-wide figure: the optimizer leans on the cheap
        early hours, which are the thinnest cells.
        """
        if not self.stops:
            return {}
        tiers = [stop.tier for stop in self.stops]
        return {t: tiers.count(t) / len(tiers) for t in sorted(set(tiers))}

    def __iter__(self) -> Iterator[Stop]:
        return iter(self.stops)


def evaluate(
    instance: Instance,
    order: Sequence[int],
    *,
    start: float | None = None,
    truncate: bool = False,
) -> Itinerary:
    """Resolve an order into a timed itinerary.

    With `truncate`, an over-long order is cut at the last stop that fits and the
    result is feasible; otherwise the infeasible stop is reported in `violation`.
    """
    departures, first_infeasible = forward_pass(instance, order, start=start)
    feasible = first_infeasible == len(order)
    kept = list(order) if feasible else list(order[:first_infeasible])

    violation: str | None = None
    if not feasible:
        index = order[first_infeasible]
        name = instance.attractions[index].name
        previous = kept[-1] + 1 if kept else 0
        clock = departures[-1] if departures else (
            instance.start_minute if start is None else start
        )
        arrive = _apply_blackouts(instance, index, clock + instance.travel(previous, index + 1))
        deadline = instance.latest_arrival[index]
        if arrive > deadline:
            # A missed commitment reads nothing like an overrun of the day, so it
            # must not borrow the closing-time wording.
            violation = (
                f"stop {first_infeasible} ({name}) reaches it at {_clock(arrive)}, "
                f"after the latest useful arrival of {_clock(deadline)}"
            )
        else:
            verb = "joins at" if instance.assumptions.queue_closes_at_close else "finishes at"
            moment = arrive if instance.assumptions.queue_closes_at_close else (
                instance.depart_at(index, arrive)
            )
            violation = (
                f"stop {first_infeasible} ({name}) {verb} {_clock(moment)} "
                f"> close {_clock(instance.close_minute)}"
            )
        if not truncate:
            kept = list(order[:first_infeasible])

    stops: list[Stop] = []
    clock = instance.start_minute if start is None else start
    previous = 0
    for index in kept:
        # The blackout push has to be applied here too. This loop re-derives the
        # schedule independently of forward_pass, so anything the pass does to the
        # clock and this does not makes the timeline the user reads disagree with
        # the timeline the solver optimized.
        arrive = _apply_blackouts(
            instance, index, clock + instance.travel(previous, index + 1)
        )
        kind = instance.kinds[index]
        wait = instance.wait_at(index, arrive)
        ride = instance.attractions[index].ride_minutes
        if kind == "ride":
            ride *= instance.assumptions.ride_scale
        board = arrive + wait
        leave = board + ride
        stops.append(
            Stop(
                index=index,
                name=instance.attractions[index].name,
                arrive=arrive,
                wait=wait,
                board=board,
                ride=ride,
                leave=leave,
                tier=instance.tier_at(index, arrive),
                kind=kind,
                must_do=index in instance.required and kind == "ride",
            )
        )
        clock = leave
        previous = index + 1

    return Itinerary(
        instance=instance,
        order=tuple(kept),
        stops=tuple(stops),
        feasible=feasible or truncate,
        violation=violation,
    )


def _clock(minute: float) -> str:
    """Minutes-from-local-midnight as HH:MM, past midnight included."""
    total = int(round(minute))
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"
