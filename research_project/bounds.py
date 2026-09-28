"""Upper bounds and exact optimality proofs.

The heuristic reports a number; these say how much of the gap to the true optimum
is left. Two independent instruments:

**A relaxation bound** that is valid for every instance but loose, and **an exact
subset dynamic program** that is tight but only runs on a reduced instance. A
heuristic that matches the DP exactly on a hard 16-attraction subproblem and sits
inside a stated gap of the relaxation on the full one is a defensible result;
either alone is not.

The DP is sound **only because the wait curves are linearly interpolated**. Its
dominance relation — for a given (visited set, last attraction), keep only the
earliest departure — assumes that leaving earlier is never worse. That is FIFO,
and FIFO requires `depart(t) = t + w(t) + R` to be non-decreasing. Under a step
function it is not: arriving at 18:01 can beat 17:59, so a later arrival can
produce an earlier departure and the DP silently returns a wrong optimum.
`waits.py` measures `max |dw/dt| = 0.690 < 1`, and the test suite asserts FIFO
holds under linear interpolation and fails under step.
"""

from __future__ import annotations

import dataclasses
import heapq
import math
from dataclasses import dataclass
from typing import Literal, Sequence

from research_project import config, model
from research_project.model import Instance


@dataclass(frozen=True)
class UpperBound:
    """A ceiling on how many attractions any route could possibly fit."""

    value: int
    method: Literal["relaxation", "relaxation+degree", "roster"]
    budget_minutes: float
    residual_minutes: float
    items: tuple[tuple[str, float], ...]  # (name, relaxed cost), ascending
    vacuous: bool  # equal to the roster size, so it says nothing

    def __str__(self) -> str:
        tail = " (vacuous)" if self.vacuous else ""
        return f"<= {self.value} by {self.method}{tail}"


def _minimum_visit_costs(instance: Instance) -> list[tuple[float, int]]:
    """Per attraction, the cheapest (wait + ride) available anywhere in the day.

    Only minutes inside the operating window count: a ride's global minimum wait
    might occur at 07:00 on a day that opens at 09:00, and using that would make
    the bound optimistic in a way the park never permits.
    """
    low = int(math.floor(instance.day.open_minute))
    high = int(math.ceil(min(instance.close_minute, model.TABLE_MINUTES - 1)))
    out: list[tuple[float, int]] = []
    for index, attraction in enumerate(instance.attractions):
        # A scheduled show is not a ride the bound is counting, and its "wait" is a
        # countdown to a fixed start time rather than a queue — including it would
        # make the cheapest-first sort meaningless.
        if instance.kinds[index] != "ride":
            continue
        row = instance.wait_table[index]
        cheapest = min(row[low : high + 1]) if high >= low else row[low]
        ride = attraction.ride_minutes * instance.assumptions.ride_scale
        out.append((cheapest + ride, index))
    return out


def _edge_bounds(instance: Instance) -> tuple[list[float], list[float]]:
    """Per node, its shortest and second-shortest edge in the travel matrix."""
    size = len(instance.travel_minutes)
    shortest: list[float] = []
    second: list[float] = []
    for i in range(size):
        row = sorted(
            instance.travel_minutes[i][j] for j in range(size) if j != i
        )
        shortest.append(row[0] if row else 0.0)
        second.append(row[1] if len(row) > 1 else row[0] if row else 0.0)
    return shortest, second


def travel_lower_bound(instance: Instance, k: int) -> float:
    """Least possible walking for any route with `k` stops.

    Two valid arguments, whichever is stronger:

    *Distinct edges.* A simple path visiting k attractions from the entrance uses
    k distinct edges of the matrix, so it costs at least the sum of the k smallest
    edges in the whole graph.

    *Node degrees.* Every visited node is incident to at least one path edge, and
    summing over nodes counts each edge twice, so travel >= half the sum of each
    visited node's shortest edge. Taking the k smallest such halves keeps it a
    lower bound whichever k attractions are chosen.
    """
    if k <= 0:
        return 0.0
    size = len(instance.travel_minutes)
    edges = sorted(
        instance.travel_minutes[i][j]
        for i in range(size)
        for j in range(i + 1, size)
    )
    by_edges = sum(edges[:k]) if len(edges) >= k else sum(edges)

    shortest, _ = _edge_bounds(instance)
    halves = sorted(value / 2.0 for value in shortest[1:])  # attractions only
    by_degree = sum(halves[:k]) + shortest[0] / 2.0  # plus the entrance's own edge

    return max(by_edges, by_degree)


def relaxation_bound(instance: Instance, *, strengthen: bool = True) -> UpperBound:
    """Largest attraction count that could fit if everything went perfectly.

    Relaxes three things at once: every ride is taken at its cheapest minute of
    the day, the route is free to take them in any order, and walking is replaced
    by the lower bound above. Each relaxation only ever *increases* what fits, so
    the result is a genuine ceiling.

    Scheduled commitments are subtracted from the budget rather than relaxed away.
    A lunch break and a show consume wall-clock the visitor cannot spend on rides,
    so counting that time as available would inflate the ceiling; removing it keeps
    the bound valid *and* makes it tighter. The bound remains a ceiling on
    **rides**, so the show pseudo-stops are excluded from the item list entirely.
    """
    committed = instance.committed_minutes + sum(
        instance.attractions[i].ride_minutes for i in instance.show_indices
    )
    budget = instance.close_minute - instance.start_minute - committed
    costs = sorted(_minimum_visit_costs(instance))

    best_k = 0
    residual = budget
    for k in range(1, instance.n_rides + 1):
        visit = sum(cost for cost, _ in costs[:k])
        travel = travel_lower_bound(instance, k) if strengthen else 0.0
        total = visit + travel
        if total <= budget:
            best_k = k
            residual = budget - total
        else:
            break

    return UpperBound(
        value=best_k,
        method="relaxation+degree" if strengthen else "relaxation",
        budget_minutes=budget,
        residual_minutes=residual,
        items=tuple(
            (instance.attractions[index].name, round(cost, 2))
            for cost, index in costs
        ),
        vacuous=best_k >= instance.n_rides,
    )


# ── exact dynamic program ─────────────────────────────────────────────────


def dp_cost_estimate(n: int) -> tuple[int, float]:
    """(peak layer states, megabytes) for an n-attraction exact solve."""
    peak = math.comb(n, n // 2) * n
    return peak, peak * 8 / 1024 / 1024


def with_window(instance: Instance, *, close_minute: float) -> Instance:
    """The same instance with a shorter day, for building binding subproblems."""
    day = dataclasses.replace(instance.day, close_minute=close_minute)
    return dataclasses.replace(instance, day=day)


def exact_dp(
    instance: Instance,
    subset: Sequence[int],
    *,
    prune: bool = True,
) -> tuple[int, tuple[int, ...], float]:
    """Provably optimal (count, order, finish) over `subset`.

    State is `(visited set, last attraction) -> earliest departure`, layered by
    popcount so only two layers are ever held. Pruning states that have already
    passed closing time is what keeps the reachable space far below 2^k.

    Returns the maximum attraction count, one order achieving it, and its finish
    time — ties broken on the earliest finish, matching the heuristic's objective.
    """
    items = list(subset)
    k = len(items)
    if k == 0:
        return 0, (), instance.start_minute

    close = instance.close_minute
    queue_closes = instance.assumptions.queue_closes_at_close
    travel = instance.travel_minutes
    depart = instance.depart_table
    latest = instance.latest_arrival

    # The two feasibility rules the forward pass applies must be applied here too,
    # or the "provably optimal" answer is optimal for a different problem than the
    # heuristic is solving, and the DP would certify a plan that eats lunch in a
    # queue. Neither disturbs dominance: pushing an arrival later or rejecting one
    # outright is monotone in the incoming clock, so keeping the earliest departure
    # per (mask, last) remains valid.
    def step(clock: float, from_node: int, index: int) -> tuple[float, float] | None:
        arrive = model._apply_blackouts(instance, index, clock + travel[from_node][index + 1])
        if arrive > latest[index]:
            return None
        leave = model._sample(depart[index], arrive)
        limit = arrive if queue_closes else leave
        if prune and limit > close:
            return None
        return arrive, leave

    # layer[mask][last] = (earliest departure, predecessor mask, predecessor last)
    start_layer: dict[int, dict[int, tuple[float, int, int]]] = {}
    for position, index in enumerate(items):
        moved = step(instance.start_minute, 0, index)
        if moved is None:
            continue
        start_layer.setdefault(1 << position, {})[position] = (moved[1], 0, -1)

    best_count = 0
    best_state: tuple[int, int] | None = None
    best_finish = math.inf
    seen: list[dict[int, dict[int, tuple[float, int, int]]]] = [{}, start_layer]

    if start_layer:
        best_count = 1
        mask, lasts = min(
            ((m, v) for m, v in start_layer.items()),
            key=lambda item: min(v[0] for v in item[1].values()),
        )
        last = min(lasts, key=lambda p: lasts[p][0])
        best_state, best_finish = (mask, last), lasts[last][0]

    history: list[dict[int, dict[int, tuple[float, int, int]]]] = [{}, start_layer]
    current = start_layer
    for _ in range(1, k):
        nxt: dict[int, dict[int, tuple[float, int, int]]] = {}
        for mask, lasts in current.items():
            for last, (clock, _, _) in lasts.items():
                from_node = items[last] + 1
                for position, index in enumerate(items):
                    bit = 1 << position
                    if mask & bit:
                        continue
                    moved = step(clock, from_node, index)
                    if moved is None:
                        continue
                    leave = moved[1]
                    new_mask = mask | bit
                    slot = nxt.setdefault(new_mask, {})
                    previous = slot.get(position)
                    if previous is None or leave < previous[0]:
                        slot[position] = (leave, mask, last)
        if not nxt:
            break
        history.append(nxt)
        current = nxt
        depth = len(history) - 1
        for mask, lasts in nxt.items():
            for last, (clock, _, _) in lasts.items():
                if depth > best_count or (depth == best_count and clock < best_finish):
                    best_count, best_state, best_finish = depth, (mask, last), clock

    if best_state is None:
        return 0, (), instance.start_minute

    # Walk the predecessors back out.
    order: list[int] = []
    mask, last = best_state
    depth = bin(mask).count("1")
    while depth > 0:
        order.append(items[last])
        _, previous_mask, previous_last = history[depth][mask][last]
        mask, last, depth = previous_mask, previous_last, depth - 1
    order.reverse()

    return best_count, tuple(order), best_finish


def reduced_instance(
    instance: Instance,
    k: int,
    *,
    method: Literal["top_wait", "random"] = "top_wait",
    window_minutes: int | None = None,
    seed: int = config.SEED,
) -> tuple[Instance, tuple[int, ...]]:
    """A subproblem small enough to solve exactly, and hard enough to be worth it.

    The trap this exists to avoid: if the window comfortably fits the whole
    subset, the DP returns `k` and proves nothing — the same degeneracy as
    reporting "all of them" on a long day. Picking the longest-wait attractions
    and optionally truncating the window makes the subproblem genuinely binding,
    and the caller should assert the DP optimum is strictly below `k`.
    """
    if method == "random":
        import random

        chosen = tuple(random.Random(seed).sample(range(instance.n), k))
    else:
        by_cost = sorted(
            range(instance.n),
            key=lambda i: -max(instance.wait_table[i]),
        )
        chosen = tuple(sorted(by_cost[:k]))

    reduced = instance
    if window_minutes is not None:
        reduced = with_window(
            instance, close_minute=instance.day.open_minute + window_minutes
        )
    return reduced, chosen


def certify(
    instance: Instance,
    count: int,
    bound: UpperBound,
) -> dict[str, object]:
    """How much of the gap to the true optimum is left unexplained."""
    return {
        "count": count,
        "upper_bound": bound.value,
        "gap": bound.value - count,
        "method": bound.method,
        "vacuous": bound.vacuous,
        # Visiting the whole roster needs no certificate: the count cannot exceed
        # the number of attractions that exist.
        "optimal": count >= instance.n_rides or count >= bound.value,
    }
