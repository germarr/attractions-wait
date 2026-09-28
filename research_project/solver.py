"""Heuristic solver for the time-dependent orienteering instance.

GRASP construction followed by variable-neighbourhood descent, best of several
randomized restarts, deterministic under a fixed seed.

**Why not simulated annealing.** The primary objective is a small integer that
plateaus: on a long day most moves leave the count unchanged, so SA's acceptance
probability degenerates to a random walk and its temperature schedule has nothing
to grip. The lexicographic `(-count, finish)` key turns that plateau into a
descent direction, and on a descent-shaped landscape VND with multi-start beats
SA at equal budget while staying reproducible.

**Every move evaluation replays the suffix.** Time dependence removes the usual
O(1) move delta — a 2-opt reversal changes the arrival time of every stop in the
reversed segment and everything after it, so the value of the move is exactly the
term an O(1) formula omits. Each neighbourhood therefore yields the first
modified position along with the candidate order, and evaluation resumes from
there against the cached prefix. `tests/test_research_orienteering.py` asserts
that path equals a from-scratch evaluation bit for bit.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, replace, field
from typing import Iterator, Literal, Sequence

from research_project import config, model
from research_project.model import Instance, Itinerary

Move = Literal["insert", "two_opt", "or_opt", "swap_in", "remove", "ruin_recreate"]

# Ordered cheapest-and-most-valuable first. `insert` leads because it is the only
# move that raises the primary objective; the reordering moves follow because
# their job is to create the slack that makes the next insert feasible. VND
# restarts from the top on any improvement, which is what lets those two
# alternate productively.
DEFAULT_MOVES: tuple[Move, ...] = (
    "insert",
    "two_opt",
    "or_opt",
    "swap_in",
    "remove",
    "ruin_recreate",
)


@dataclass(frozen=True)
class SolverConfig:
    seed: int = config.SEED
    restarts: int = 20
    alpha: float = 0.3  # GRASP breadth; 0.0 is pure greedy
    moves: tuple[Move, ...] = DEFAULT_MOVES
    max_passes: int = 50
    ruin_tries: int = 12
    time_limit_s: float | None = 30.0


@dataclass(frozen=True)
class SolveResult:
    itinerary: Itinerary
    count: int
    finish_minute: float
    evaluations: int
    restarts_used: int
    seconds: float
    seed: int
    assumptions_key: str
    trace: tuple[tuple[int, int, float], ...] = field(default=())
    upper_bound: int | None = None
    optimal: bool | None = None

    @property
    def slack_minutes(self) -> float:
        """Unused minutes against the constraint that actually binds.

        Under `queue_closes_at_close` the binding moment is joining the last
        queue, not finishing the last ride — so a route can correctly finish
        after closing time. Measuring finish-against-close instead would report
        spurious negative slack on every day that uses its full window.
        """
        if not self.itinerary.stops:
            return self.itinerary.instance.close_minute - self.finish_minute
        last = self.itinerary.stops[-1]
        binding = (
            last.arrive
            if self.itinerary.instance.assumptions.queue_closes_at_close
            else last.leave
        )
        return self.itinerary.instance.close_minute - binding


# ── scoring ───────────────────────────────────────────────────────────────


def _finish(key: tuple) -> float:
    """The finish-time term of an objective key.

    Read by name because the key has grown once already: it was
    `(-count, finish)` and is now `(missing required, -count, finish)`. An
    insertion cost computed as `key[1] - base[1]` silently became a *count* delta
    when that happened, which ties every option and reduces the greedy to
    arbitrary order — with no test failure, because the count itself was still
    correct.
    """
    return key[-1]


class _Scorer:
    """Evaluates candidate orders, counting how much work the search does."""

    def __init__(self, instance: Instance) -> None:
        self.instance = instance
        self.evaluations = 0

    def score(
        self,
        order: Sequence[int],
        *,
        from_stop: int = 0,
        cached: Sequence[float] | None = None,
    ) -> tuple[tuple[int, int, int, float], list[float], int]:
        """Return (objective key, departures, feasible_length) — see `objective_key`.

        An order whose tail does not fit is scored as its feasible prefix rather
        than rejected, so the search can walk through slightly-too-long routes
        instead of falling off a cliff.

        The leading term is what makes every removal move safe without guarding
        each one: dropping a must-do raises `missing` from 0 to 1, which is worse
        than any number of extra rides, so `remove`, `swap_in` and
        `ruin_recreate` reject it on the objective rather than on a special case.
        The count itself excludes scheduled shows — a show occupies the day but is
        not an attraction ridden.
        """
        self.evaluations += 1
        if cached is None or from_stop <= 0:
            departures, first_bad = model.forward_pass(self.instance, order)
        else:
            departures, first_bad = model.forward_pass(
                self.instance, order, from_stop=from_stop, cached_departures=cached
            )
        count = min(len(order), first_bad)
        finish = departures[-1] if departures else self.instance.start_minute
        kept = order[:count]
        instance = self.instance
        rides = sum(1 for i in kept if instance.kinds[i] == "ride")
        missing = len(instance.required.difference(kept))

        # Queueing on the named must-dos, reconstructed from the departures the
        # pass already produced: arrive = previous departure + travel, and the wait
        # is the curve at that arrival. Only walked when there is something to
        # measure, so the plain count-maximizing path pays nothing for it.
        must_wait = 0.0
        if instance.required:
            clock = instance.start_minute
            previous = 0
            for position, index in enumerate(kept):
                arrive = clock + instance.travel_minutes[previous][index + 1]
                if instance.blackouts:
                    arrive = model._apply_blackouts(instance, index, arrive)
                if index in instance.required and instance.kinds[index] == "ride":
                    must_wait += instance.wait_at(index, arrive)
                clock = departures[position]
                previous = index + 1

        key = model.objective_key(
            missing=missing,
            must_do_wait=must_wait,
            rides=rides,
            finish=finish,
            bucket=instance.assumptions.must_do_wait_bucket_min,
        )
        return key, departures, count


# ── construction ──────────────────────────────────────────────────────────


def greedy_insertion(
    instance: Instance,
    rng: random.Random,
    *,
    alpha: float = 0.0,
    candidates: Sequence[int] | None = None,
    scorer: _Scorer | None = None,
) -> list[int]:
    """Build a route by repeatedly making the cheapest feasible insertion.

    Cost of an insertion is its marginal effect on the finish time, which is the
    knapsack greedy the upper bound also reasons with — construction and bound
    then share an intuition rather than pulling in different directions.

    With `alpha > 0` the pick is uniform over the best ceil(alpha * m) candidates,
    which is what makes restarts explore instead of repeating.
    """
    scorer = scorer or _Scorer(instance)
    pool = list(candidates if candidates is not None else range(instance.n))
    for index in sorted(instance.required):
        if index not in pool:
            pool.append(index)  # a commitment is never optional
    order: list[int] = []
    _, departures, _ = scorer.score(order)

    # Place the commitments first. Inserting them into an already-full route is
    # usually infeasible — a 21:30 show cannot be squeezed between two rides that
    # already occupy the evening — so they choose their positions while the day is
    # still empty, and the optional stops fill in around them afterwards.
    required_first = [i for i in sorted(instance.required) if i in set(pool)]
    for which in required_first:
        base_key, base_departures, _ = scorer.score(order)
        best_option: tuple[float, int] | None = None
        for position in range(len(order) + 1):
            trial = order[:position] + [which] + order[position:]
            key, _, count = scorer.score(
                trial,
                from_stop=position,
                cached=base_departures if position > 0 else None,
            )
            if count != len(trial):
                continue
            delta = _finish(key) - _finish(base_key)
            if best_option is None or delta < best_option[0]:
                best_option = (delta, position)
        if best_option is None:
            continue  # reported by the caller as an unmet commitment
        position = best_option[1]
        order = order[:position] + [which] + order[position:]
        pool.remove(which)

    while pool:
        options: list[tuple[float, int, int]] = []
        base_key, base_departures, base_count = scorer.score(order)
        base_finish = _finish(base_key)
        for which in pool:
            for position in range(len(order) + 1):
                trial = order[:position] + [which] + order[position:]
                key, _, count = scorer.score(
                    trial,
                    from_stop=position,
                    cached=base_departures if position > 0 else None,
                )
                if count != len(trial):
                    continue  # the insertion does not fit
                options.append((_finish(key) - base_finish, which, position))
        if not options:
            break
        options.sort()
        cut = max(1, int(len(options) * alpha + 0.999)) if alpha > 0 else 1
        _, which, position = options[rng.randrange(cut)]
        order = order[:position] + [which] + order[position:]
        pool.remove(which)

    return order


# ── neighbourhoods ────────────────────────────────────────────────────────


def _neighbourhood(
    move: Move,
    order: Sequence[int],
    unrouted: Sequence[int],
) -> Iterator[tuple[list[int], int]]:
    """Yield (candidate order, first modified position) for one neighbourhood."""
    route = list(order)
    size = len(route)

    if move == "insert":
        for which in unrouted:
            for position in range(size + 1):
                yield route[:position] + [which] + route[position:], position

    elif move == "two_opt":
        for i in range(size - 1):
            for j in range(i + 2, size):
                yield route[:i] + route[i : j + 1][::-1] + route[j + 1 :], i

    elif move == "or_opt":
        for length in (1, 2, 3):
            for start in range(size - length + 1):
                segment = route[start : start + length]
                rest = route[:start] + route[start + length :]
                for target in range(len(rest) + 1):
                    if target == start:
                        continue
                    yield rest[:target] + segment + rest[target:], min(start, target)

    elif move == "swap_in":
        for position in range(size):
            for which in unrouted:
                yield route[:position] + [which] + route[position + 1 :], position

    elif move == "remove":
        for position in range(size):
            yield route[:position] + route[position + 1 :], position


def local_search(
    instance: Instance,
    order: Sequence[int],
    rng: random.Random,
    *,
    moves: Sequence[Move] = DEFAULT_MOVES,
    max_passes: int = 50,
    ruin_tries: int = 12,
    candidates: Sequence[int] | None = None,
    scorer: _Scorer | None = None,
    deadline: float | None = None,
) -> list[int]:
    """Variable-neighbourhood descent with first improvement.

    Neighbourhoods are tried in order; any improvement restarts from the first,
    which is what lets a reordering move create slack that the next `insert` then
    consumes.
    """
    scorer = scorer or _Scorer(instance)
    pool = set(candidates if candidates is not None else range(instance.n))
    best = list(order)
    best_key, best_departures, _ = scorer.score(best)

    for _ in range(max_passes):
        if deadline is not None and time.perf_counter() > deadline:
            break
        improved = False
        for move in moves:
            if move == "ruin_recreate":
                continue  # applied below, it is stochastic rather than systematic
            unrouted = [i for i in sorted(pool) if i not in set(best)]
            for candidate, first_modified in _neighbourhood(move, best, unrouted):
                key, departures, count = scorer.score(
                    candidate,
                    from_stop=first_modified,
                    cached=best_departures if first_modified > 0 else None,
                )
                if count != len(candidate):
                    continue  # scoring truncated it; not a clean move
                if key < best_key:
                    best, best_key, best_departures = candidate, key, departures
                    improved = True
                    break
            if improved:
                break
        if improved:
            continue

        # Ruin and recreate: remove a contiguous chunk and greedily refill. This
        # is what escapes a local optimum whose only improving move needs two
        # simultaneous changes, which no single neighbourhood above can express.
        if "ruin_recreate" in moves and len(best) >= 4:
            for _ in range(ruin_tries):
                length = rng.randint(2, 4)
                start = rng.randrange(0, max(1, len(best) - length + 1))
                kept = best[:start] + best[start + length :]
                rebuilt = _refill(instance, kept, pool, rng, scorer)
                key, departures, count = scorer.score(rebuilt)
                if count == len(rebuilt) and key < best_key:
                    best, best_key, best_departures = rebuilt, key, departures
                    improved = True
                    break
        if not improved:
            break

    return best


def _refill(
    instance: Instance,
    order: Sequence[int],
    pool: set[int],
    rng: random.Random,
    scorer: _Scorer,
) -> list[int]:
    """Greedily reinsert every unrouted attraction into an existing route."""
    route = list(order)
    remaining = [i for i in sorted(pool) if i not in set(route)]
    rng.shuffle(remaining)
    for which in remaining:
        base_key, base_departures, _ = scorer.score(route)
        best_option: tuple[float, int] | None = None
        for position in range(len(route) + 1):
            trial = route[:position] + [which] + route[position:]
            key, _, count = scorer.score(
                trial,
                from_stop=position,
                cached=base_departures if position > 0 else None,
            )
            if count != len(trial):
                continue
            delta = _finish(key) - _finish(base_key)
            if best_option is None or delta < best_option[0]:
                best_option = (delta, position)
        if best_option is not None:
            position = best_option[1]
            route = route[:position] + [which] + route[position:]
    return route


# ── the entry points ──────────────────────────────────────────────────────


def solve(
    instance: Instance,
    solver_config: SolverConfig | None = None,
    *,
    candidates: Sequence[int] | None = None,
) -> SolveResult:
    """Best itinerary found, maximizing count and then finishing earliest."""
    solver_config = solver_config or SolverConfig()
    rng = random.Random(solver_config.seed)
    scorer = _Scorer(instance)
    started = time.perf_counter()
    deadline = (
        started + solver_config.time_limit_s
        if solver_config.time_limit_s is not None
        else None
    )

    best_order: list[int] | None = None
    best_key: tuple[int, int, int, float] | None = None
    trace: list[tuple[int, int, float]] = []
    used = 0

    for restart in range(solver_config.restarts):
        used = restart + 1
        # The first restart is pure greedy, so the result never depends on luck
        # alone; the rest randomize the construction to explore.
        alpha = 0.0 if restart == 0 else solver_config.alpha
        order = greedy_insertion(
            instance, rng, alpha=alpha, candidates=candidates, scorer=scorer
        )
        order = local_search(
            instance,
            order,
            rng,
            moves=solver_config.moves,
            max_passes=solver_config.max_passes,
            ruin_tries=solver_config.ruin_tries,
            candidates=candidates,
            scorer=scorer,
            deadline=deadline,
        )
        key, _, count = scorer.score(order)
        trace.append((restart, count, _finish(key)))
        if best_key is None or key < best_key:
            best_order, best_key = list(order), key
        if deadline is not None and time.perf_counter() > deadline:
            break

    assert best_order is not None
    itinerary = model.evaluate(instance, best_order, truncate=True)

    # `truncate=True` cuts the tail that does not fit, which must never quietly
    # discard something the visitor named. If it did, retry without truncation so
    # the itinerary reports its own violation instead of presenting a short plan as
    # a complete one.
    if itinerary.missing_required and truncate_dropped_required(
        instance, best_order, itinerary
    ):
        itinerary = model.evaluate(instance, best_order, truncate=False)

    # The roster a count is measured against excludes scheduled shows: a plan with
    # the fireworks in it has one more stop, not one more attraction.
    if candidates is not None:
        pool_size = sum(1 for i in candidates if instance.kinds[i] == "ride")
    else:
        pool_size = instance.n_rides

    return SolveResult(
        itinerary=itinerary,
        count=itinerary.count,
        finish_minute=itinerary.finish_minute,
        evaluations=scorer.evaluations,
        restarts_used=used,
        seconds=time.perf_counter() - started,
        seed=solver_config.seed,
        assumptions_key=instance.assumptions.key(),
        trace=tuple(trace),
        # Visiting everything is trivially optimal — no bound is needed to certify
        # it, because the count cannot exceed the roster. Only claimable when every
        # commitment was also met; a plan missing a must-do is not optimal for the
        # problem that was actually posed.
        optimal=(
            True
            if itinerary.count == pool_size and not itinerary.missing_required
            else None
        ),
    )


def truncate_dropped_required(
    instance: Instance, order: Sequence[int], itinerary: model.Itinerary
) -> bool:
    """Did truncation, rather than the search, lose a commitment?"""
    return any(i in instance.required for i in order) and bool(
        itinerary.missing_required
    )


def solve_min_finish(
    instance: Instance,
    subset: Sequence[int] | None = None,
    solver_config: SolverConfig | None = None,
) -> SolveResult:
    """Earliest finish that covers `subset` (default: every attraction).

    On long days this, not the count, is the interesting answer: if everything
    fits, "all 30 by 21:47" says far more than "30".
    """
    return solve(instance, solver_config, candidates=list(subset or range(instance.n)))


# ── commitments that may not all fit ──────────────────────────────────────


@dataclass(frozen=True)
class RankedResult:
    """A solve that honoured as many ranked must-dos as the day allowed."""

    result: SolveResult
    honoured: tuple[int, ...]
    dropped: tuple[tuple[int, str], ...]  # (index, why it was dropped)
    attempts: int

    @property
    def complete(self) -> bool:
        return not self.dropped


def solve_ranked(
    instance: Instance,
    ranked: Sequence[int],
    solver_config: SolverConfig | None = None,
) -> RankedResult:
    """Solve with `ranked` must-dos, dropping from the bottom until it fits.

    `ranked` is in the visitor's own priority order, highest first, and the rule is
    the literal one: when the whole set will not fit, the **lowest-ranked** pick
    goes. Dropping whichever pick the search happened to fail on would be easier
    and is what a first version did — but it dropped a rank-2 headliner while
    keeping rank 5, which is not what ranking a list means.

    The cost of honouring the order is that an expensive high-ranked pick can force
    several cheaper ones out. That is a real trade and the visitor is the only one
    who can make it, so each drop records whether the pick was *itself*
    unplaceable or was shed to make room for something ranked above it. Told "TRON
    alone cost you three other picks", they can re-rank; told only "we dropped
    three", they cannot.

    Scheduled shows stay required throughout: they were already screened against the
    day's closing time, and silently dropping one would answer a question the
    visitor did not ask.
    """
    solver_config = solver_config or SolverConfig()
    shows = frozenset(instance.show_indices)
    wanted = list(ranked)
    dropped: list[tuple[int, str]] = []
    attempts = 0

    while True:
        attempts += 1
        trial = replace(instance, required=frozenset(wanted) | shows)
        result = solve(trial, solver_config)
        unmet = set(result.itinerary.missing_required) - shows
        if not unmet or not wanted:
            return RankedResult(
                result=result,
                honoured=tuple(wanted),
                dropped=tuple(dropped),
                attempts=attempts,
            )

        victim = wanted[-1]
        wanted.remove(victim)
        if victim in unmet:
            reason = "does not fit on this date"
        else:
            blockers = ", ".join(
                instance.attractions[i].name for i in sorted(unmet)
            )
            reason = f"dropped to make room for higher-ranked picks ({blockers})"
        dropped.append((victim, reason))
