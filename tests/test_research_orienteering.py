"""Model and solver invariants for the route optimizer.

Pure logic, no slow queries beyond building one instance — this should stay under
a second so it can be run on every change.

The load-bearing assertion here is that incremental re-evaluation equals
from-scratch evaluation bit for bit. Time-dependent routing has no valid O(1)
move delta: a 2-opt reversal changes the arrival time of every stop in the
reversed segment and everything after it, so the whole value of the move lives in
the term the usual formula omits. Any "optimisation" that skips the suffix replay
is a bug, and this is what catches it.

    .venv/bin/python tests/test_research_orienteering.py

Non-zero exit if anything diverges.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import dataclasses  # noqa: E402

from research_project import shows  # noqa: E402
from research_project import (  # noqa: E402
    bounds,
    config,
    db,
    model,
    scenarios,
    solver,
    travel,
    waits,
)

SEED = 20260925
MOVE_TRIALS = 1000


def _instance(date: str = "2026-09-26") -> model.Instance:
    conn = db.connect()
    roster = db.roster(conn, mode="open_today")
    table = waits.build_wait_table(
        conn, names={a.id: a.name for a in db.roster(conn, mode="history")}
    )
    matrix = travel.build_travel_matrix(conn, config.PARK_ID, [a.id for a in roster])
    day = db.operating_days(conn, start=date, end=date)[0]
    conn.close()
    return model.build_instance(day, roster, table=table, travel=matrix)



def _committed_instance(date: str = "2026-10-03", *, picks: int = 3,
                        lunch=(750.0, 795.0), show_keys=("fireworks", "parade")):
    """An instance carrying must-dos, a break and scheduled shows."""
    conn = db.connect()
    try:
        roster = db.roster(conn, mode="open_today")
        table = waits.build_wait_table(
            conn, names={a.id: a.name for a in db.roster(conn, mode="history")}
        )
        matrix = travel.build_travel_matrix(
            conn, config.PARK_ID, [a.id for a in roster]
        )
        day = db.operating_days(conn, start=date, end=date)[0]
        commitments = []
        for key in show_keys:
            resolved = shows.showtime_for(conn, key, date=date)
            commitments.append(
                model.ShowCommitment(
                    node_id=f"__show_{key}__",
                    name=resolved.name,
                    start_minute=resolved.start_minute,
                    watch_minutes=resolved.watch_minutes,
                    latest_arrival=resolved.latest_arrival,
                    proxy_attraction=resolved.proxy_attraction,
                    tier=f"show:{resolved.tier}",
                    latitude=0.0,
                    longitude=0.0,
                )
            )
    finally:
        conn.close()
    # The dearest attractions, so the commitments genuinely bind.
    by_cost = sorted(
        range(len(roster)),
        key=lambda i: -float(table.minutes[table.index_of(roster[i].id), :, 9:21].mean()),
    )
    ranked = [roster[i].id for i in by_cost[:picks]]
    instance = model.build_instance(
        day, roster, table=table, travel=matrix,
        shows=commitments, lunch=lunch, required_ids=ranked,
    )
    return instance, ranked


def _random_move(rng: random.Random, order: list[int], unrouted: list[int]):
    """Return (new_order, first_modified_position) for a random neighbourhood move."""
    kind = rng.choice(["two_opt", "or_opt", "insert", "remove", "swap"])
    new = list(order)

    if kind == "two_opt" and len(new) >= 4:
        i, j = sorted(rng.sample(range(len(new)), 2))
        if j - i < 2:
            return None
        new[i : j + 1] = reversed(new[i : j + 1])
        return new, i

    if kind == "or_opt" and len(new) >= 3:
        length = rng.randint(1, min(3, len(new) - 1))
        start = rng.randrange(0, len(new) - length)
        segment = new[start : start + length]
        rest = new[:start] + new[start + length :]
        target = rng.randrange(0, len(rest) + 1)
        return rest[:target] + segment + rest[target:], min(start, target)

    if kind == "insert" and unrouted:
        which = rng.choice(unrouted)
        position = rng.randrange(0, len(new) + 1)
        return new[:position] + [which] + new[position:], position

    if kind == "remove" and len(new) >= 2:
        position = rng.randrange(0, len(new))
        return new[:position] + new[position + 1 :], position

    if kind == "swap" and len(new) >= 2:
        i, j = sorted(rng.sample(range(len(new)), 2))
        new[i], new[j] = new[j], new[i]
        return new, i

    return None


def failures() -> list[str]:
    out: list[str] = []

    def check(ok: bool, msg: str) -> None:
        if not ok:
            out.append(msg)

    instance = _instance()
    rng = random.Random(SEED)

    # 1. The departure table is monotone at instance level too. waits.py proves it
    #    for the curve; the past-midnight padding could in principle have broken it.
    non_monotone = 0
    for row in instance.depart_table:
        non_monotone += sum(1 for a, b in zip(row, row[1:]) if b < a)
    check(non_monotone == 0, f"{non_monotone} non-monotone steps in the departure table")

    # 2. Forward-pass internals, recomputed independently here rather than by
    #    calling the code under test.
    order = list(range(instance.n))
    itinerary = model.evaluate(instance, order)
    clock = instance.start_minute
    previous = 0
    for position, stop in enumerate(itinerary.stops):
        arrive = clock + instance.travel(previous, stop.index + 1)
        wait = instance.wait_at(stop.index, arrive)
        ride = (
            instance.attractions[stop.index].ride_minutes
            * instance.assumptions.ride_scale
        )
        if abs(stop.arrive - arrive) > 1e-9:
            out.append(f"stop {position}: arrive {stop.arrive} != recomputed {arrive}")
            break
        if abs(stop.wait - wait) > 1e-9:
            out.append(f"stop {position}: wait {stop.wait} != fresh lookup {wait}")
            break
        if abs(stop.leave - (stop.board + stop.ride)) > 1e-9:
            out.append(f"stop {position}: leave != board + ride")
            break
        if abs(stop.ride - ride) > 1e-9:
            out.append(f"stop {position}: ride {stop.ride} != configured {ride}")
            break
        clock = stop.leave
        previous = stop.index + 1

    arrivals = [s.arrive for s in itinerary.stops]
    check(
        all(b > a for a, b in zip(arrivals, arrivals[1:])),
        "arrival times are not strictly increasing",
    )

    # 3. The feasibility boundary is what the assumption says it is.
    close = instance.close_minute
    if instance.assumptions.queue_closes_at_close:
        check(
            all(s.arrive <= close + 1e-9 for s in itinerary.stops),
            "a kept stop joins its queue after closing time",
        )
    else:
        check(
            all(s.leave <= close + 1e-9 for s in itinerary.stops),
            "a kept stop finishes after closing time",
        )

    # An over-long order must be reported, not silently truncated.
    everything = list(range(instance.n))
    full = model.evaluate(instance, everything)
    if full.count < instance.n:
        check(
            full.violation is not None and not full.feasible,
            "an infeasible itinerary reported no violation",
        )
        check(
            "close" in (full.violation or ""),
            f"violation message is not diagnostic: {full.violation!r}",
        )

    # 4. THE assertion: incremental re-evaluation == from scratch, bit for bit.
    base = list(range(instance.n))[:20]
    departures, _ = model.forward_pass(instance, base)
    mismatches = 0
    worst = 0.0
    tried = 0
    for _ in range(MOVE_TRIALS):
        unrouted = [i for i in range(instance.n) if i not in base]
        move = _random_move(rng, base, unrouted)
        if move is None:
            continue
        new_order, first_modified = move
        tried += 1
        scratch, scratch_bad = model.forward_pass(instance, new_order)
        if first_modified > len(departures):
            continue
        incremental, incremental_bad = model.forward_pass(
            instance,
            new_order,
            from_stop=min(first_modified, len(departures)),
            cached_departures=departures,
        )
        if scratch_bad != incremental_bad or len(scratch) != len(incremental):
            mismatches += 1
            continue
        for a, b in zip(scratch, incremental):
            if a != b:
                mismatches += 1
                worst = max(worst, abs(a - b))
                break
    check(tried > 100, f"only {tried} moves exercised; the generator is not working")
    check(
        mismatches == 0,
        f"{mismatches} of {tried} moves disagree between incremental and "
        f"from-scratch evaluation (worst {worst:.3e} min)",
    )

    # 5. Structural validity of what evaluate() returns.
    check(
        len(set(itinerary.order)) == len(itinerary.order),
        "an attraction appears twice in one itinerary",
    )
    check(
        all(0 <= i < instance.n for i in itinerary.order),
        "an itinerary references an attraction outside the roster",
    )
    check(
        itinerary.count == len(itinerary.stops),
        "count does not match the number of stops on a plain instance "
        "(no shows, so every stop is a ride)",
    )
    check(
        itinerary.objective
        == model.objective_key(
            missing=len(itinerary.missing_required),
            must_do_wait=itinerary.must_do_wait,
            rides=itinerary.count,
            finish=itinerary.finish_minute,
            bucket=instance.assumptions.must_do_wait_bucket_min,
        ),
        "Itinerary.objective disagrees with model.objective_key; the search and the "
        "reported plan would then be optimizing different things",
    )

    # 6. Time accounting adds up: walking + waiting + riding + the start overhead
    #    must equal the elapsed span from park open to finish.
    if itinerary.stops:
        elapsed = itinerary.finish_minute - instance.day.open_minute
        accounted = (
            instance.assumptions.start_overhead_min
            + itinerary.walking_minutes
            + itinerary.waiting_minutes
            + itinerary.riding_minutes
        )
        check(
            abs(elapsed - accounted) < 1e-6,
            f"time does not account: elapsed {elapsed:.3f} vs "
            f"walking+waiting+riding+overhead {accounted:.3f}",
        )

    # 7. A shorter window can never admit more attractions than a longer one.
    short_day = db.DayWindow(
        date=instance.day.date,
        weekday=instance.day.weekday,
        open_minute=instance.day.open_minute,
        close_minute=instance.day.open_minute + 300,
        party_night=True,
    )
    conn = db.connect()
    roster = db.roster(conn, mode="open_today")
    table = waits.build_wait_table(
        conn, names={a.id: a.name for a in db.roster(conn, mode="history")}
    )
    matrix = travel.build_travel_matrix(conn, config.PARK_ID, [a.id for a in roster])
    conn.close()
    short = model.build_instance(short_day, roster, table=table, travel=matrix)
    check(
        model.evaluate(short, order).count <= itinerary.count,
        "a 5-hour window admitted more attractions than a 15-hour one",
    )

    # 8. The solver.
    quick = solver.SolverConfig(restarts=2, time_limit_s=30.0)
    first = solver.solve(instance, quick)
    again = solver.solve(instance, quick)
    check(
        first.itinerary.order == again.itinerary.order,
        "the solver is not deterministic under a fixed seed",
    )
    check(
        (first.count, first.finish_minute) == (again.count, again.finish_minute),
        "two runs with the same seed disagree on count or finish",
    )

    other = solver.solve(instance, solver.SolverConfig(restarts=2, seed=quick.seed + 1))
    check(
        abs(other.count - first.count) <= 1,
        f"a different seed changed the count by {abs(other.count - first.count)}; "
        "the search is too unstable to report a single number",
    )

    check(
        first.count <= instance.n,
        f"the solver returned {first.count} attractions from a roster of {instance.n}",
    )
    check(
        len(set(first.itinerary.order)) == len(first.itinerary.order),
        "the solver visited an attraction twice",
    )
    check(first.itinerary.feasible, "the solver returned an infeasible itinerary")

    # The itinerary must survive an independent re-evaluation — the solver scores
    # orders through its own path, so this checks the two agree.
    replay = model.evaluate(instance, first.itinerary.order)
    check(
        replay.count == first.count,
        f"replaying the solver's order gives {replay.count}, solver claimed {first.count}",
    )
    check(
        abs(replay.finish_minute - first.finish_minute) < 1e-9,
        "replaying the solver's order gives a different finish time",
    )

    # Local search must never lose ground against the construction alone.
    import random as _random

    constructed = solver.greedy_insertion(instance, _random.Random(quick.seed))
    check(
        first.count >= model.evaluate(instance, constructed).count,
        "local search returned fewer attractions than greedy construction alone",
    )

    # Every kept stop respects the binding constraint, and slack is measured
    # against that constraint rather than against the finish time.
    if first.itinerary.stops:
        last = first.itinerary.stops[-1]
        binding = (
            last.arrive if instance.assumptions.queue_closes_at_close else last.leave
        )
        check(
            binding <= instance.close_minute + 1e-9,
            "the last stop breaches the closing constraint",
        )
        check(
            abs(first.slack_minutes - (instance.close_minute - binding)) < 1e-9,
            "slack is not measured against the binding constraint",
        )

    # A short window must not admit more than a long one, for the solver too.
    short_result = solver.solve(short, quick)
    check(
        short_result.count <= first.count,
        f"a 5-hour window admitted {short_result.count} against the long day's "
        f"{first.count}",
    )

    # 9. Bounds and exact optimality.
    bound = bounds.relaxation_bound(instance)
    check(
        first.count <= bound.value,
        f"the heuristic found {first.count}, above its own upper bound {bound.value}",
    )
    check(
        bound.value <= instance.n,
        f"the bound {bound.value} exceeds the roster size {instance.n}",
    )
    check(
        bound.vacuous == (bound.value >= instance.n),
        "the bound's vacuous flag disagrees with its value",
    )

    # A shorter window cannot admit more, for the bound either.
    short_bound = bounds.relaxation_bound(short)
    check(
        short_bound.value <= bound.value,
        f"a 5-hour window bounds at {short_bound.value}, above the 15-hour "
        f"day's {bound.value}",
    )

    # The exact DP, on a subproblem built to be genuinely hard.
    reduced, subset = bounds.reduced_instance(instance, 12, window_minutes=200)
    dp_count, dp_order, dp_finish = bounds.exact_dp(reduced, subset)

    # Anti-tautology: if the window fits the whole subset, the DP proves nothing.
    check(
        dp_count < len(subset),
        f"the reduced instance is not binding (DP took all {len(subset)}), so "
        "matching it demonstrates nothing",
    )

    dp_solved = solver.solve(
        reduced, solver.SolverConfig(restarts=4), candidates=list(subset)
    )
    check(
        dp_solved.count == dp_count,
        f"the heuristic found {dp_solved.count} where the exact optimum is {dp_count}",
    )

    # The DP's own route must survive an independent evaluation.
    dp_itinerary = model.evaluate(reduced, dp_order)
    check(
        dp_itinerary.count == dp_count,
        f"the DP's order re-evaluates to {dp_itinerary.count}, not {dp_count}",
    )
    check(
        dp_itinerary.feasible,
        "the DP returned an order that does not actually fit the day",
    )
    check(
        abs(dp_itinerary.finish_minute - dp_finish) < 1e-9,
        "the DP's reported finish disagrees with an independent evaluation",
    )
    check(
        len(set(dp_order)) == len(dp_order),
        "the DP visited an attraction twice",
    )
    check(
        set(dp_order) <= set(subset),
        "the DP visited something outside its subset",
    )

    # Brute force validates the DP itself on a tiny instance: every ordering of
    # every subset, no dynamic programming, no dominance argument.
    import itertools

    tiny, tiny_subset = bounds.reduced_instance(instance, 6, window_minutes=110)
    brute_count, brute_finish = 0, float("inf")
    for size in range(len(tiny_subset), 0, -1):
        for combination in itertools.combinations(tiny_subset, size):
            for permutation in itertools.permutations(combination):
                resolved = model.evaluate(tiny, permutation)
                if resolved.count == len(permutation):
                    if size > brute_count or (
                        size == brute_count and resolved.finish_minute < brute_finish
                    ):
                        brute_count, brute_finish = size, resolved.finish_minute
        if brute_count == size:
            break
    tiny_dp, _, tiny_finish = bounds.exact_dp(tiny, tiny_subset)
    check(
        tiny_dp == brute_count,
        f"the DP says {tiny_dp} where exhaustive enumeration says {brute_count}",
    )
    check(
        abs(tiny_finish - brute_finish) < 1e-6,
        f"the DP's finish {tiny_finish:.3f} differs from brute force's {brute_finish:.3f}",
    )

    # 10. The scenario machinery.
    conn = db.connect()
    table = waits.build_wait_table(
        conn, names={a.id: a.name for a in db.roster(conn, mode="history")}
    )
    levels = scenarios.weekday_levels(table)
    check(len(levels) == 7, f"weekday levels covers {len(levels)} days, expected 7")
    check(
        all(v > 0 for v in levels.values()),
        "a weekday level is non-positive",
    )

    # Normalizing must make every weekday's effective level identical — that is
    # the whole point, and without it "weekday" and "crowd level" are one
    # variable measured twice, which makes any decomposition meaningless.
    target = 1.15
    effective = [
        levels[weekday] * scenarios.crowd_multiplier(levels, weekday, target)
        for weekday in range(7)
    ]
    check(
        max(effective) - min(effective) < 1e-9,
        f"normalized weekday levels still differ by {max(effective) - min(effective):.6f}; "
        "crowd level is not separated from weekday shape",
    )

    # The realistic crowd range should be narrow — if it were wide, the article's
    # claim that scheduling dominates crowds would need re-examining.
    import statistics as _stats

    mean_level = _stats.mean(levels.values())
    spread = max(levels.values()) / mean_level - min(levels.values()) / mean_level
    check(
        spread < 0.40,
        f"weekday crowd levels span {spread:.2f} of the mean, wider than assumed",
    )

    # The grid is the full cross product, with nothing silently dropped.
    grid = scenarios.factorial_scenarios(
        weekdays=(0, 5), crowd_levels=(0.9, 1.1), window_minutes=(600, 900),
        open_minutes=(540,),
    )
    check(len(grid) == 2 * 2 * 2, f"factorial grid has {len(grid)} cells, expected 8")
    check(
        len({s.label for s in grid}) == len(grid),
        "two grid cells share a label, so results would collide",
    )

    # Assumption plumbing: a swept constant must change the key, or a sweep can
    # silently mix results computed under different constants.
    base_key = config.Assumptions().key()
    for field, value in (
        ("walk_speed_mps", 1.3),
        ("ride_scale", 1.25),
        ("crowd_multiplier", 1.1),
        ("min_cell_dates", 8),
    ):
        changed = dataclasses.replace(config.Assumptions(), **{field: value})
        check(
            changed.key() != base_key,
            f"changing {field} did not change the assumptions key",
        )
    check(
        config.Assumptions().key() == base_key,
        "the assumptions key is not stable across identical instances",
    )

    # A small sweep runs, carries its key, and respects its own bound.
    small = scenarios.run_sweep(
        conn, grid[:4], solver_config=solver.SolverConfig(restarts=2, time_limit_s=20.0)
    )
    conn.close()
    check(len(small) == 4, f"sweep returned {len(small)} rows for 4 scenarios")
    check(
        small["assumptions"].nunique() >= 1,
        "sweep rows carry no assumptions key",
    )
    check(
        bool((small["count"] <= small["upper_bound"]).all()),
        "a sweep row exceeds its own upper bound",
    )
    check(
        bool((small["count"] <= small["roster"]).all()),
        "a sweep row visits more attractions than exist",
    )

    decomposition = scenarios.variance_decomposition(
        small, factors=("window_minutes", "crowd_level")
    )
    check(
        bool((decomposition["eta_squared"] >= -1e-9).all()),
        "variance decomposition produced a negative eta squared",
    )

    # 11. The web app's API contract, exercised without a server.
    from research_project.webapp import app as webapp

    dates = webapp.api_dates()
    check(bool(dates), "the web app lists no plannable dates")
    required = {"date", "weekday", "open", "close", "window_hours", "party_night"}
    check(
        required <= set(dates[0]),
        f"a date row is missing keys: {sorted(required - set(dates[0]))}",
    )

    payload = webapp.plan_route(dates[0]["date"], restarts=2)
    for key in ("count", "roster", "finish", "stops", "skipped", "entrance", "support"):
        if key not in payload:
            out.append(f"the route payload is missing {key!r}")
            break
    check(
        payload["count"] == len(payload["stops"]),
        "the payload's count disagrees with the number of stops it carries",
    )
    check(
        payload["count"] + len(payload["skipped"]) == payload["roster"],
        "stops plus skipped does not account for the whole roster",
    )
    check(
        all(
            s.get("lat") is not None and s.get("lon") is not None
            for s in payload["stops"] + payload["skipped"]
        ),
        "a stop reached the front end without coordinates, so it cannot be drawn",
    )
    check(
        all(s["arrive_minute"] < s["leave_minute"] for s in payload["stops"]),
        "a stop leaves before it arrives",
    )
    arrivals = [s["arrive_minute"] for s in payload["stops"]]
    check(
        arrivals == sorted(arrivals),
        "the payload's stops are not in chronological order",
    )


    # ── scheduled commitments: shows, a break, and must-dos ─────────────
    committed, ranked = _committed_instance()
    lunch_start, lunch_end = committed.blackouts[0]

    check(
        committed.n_rides == instance.n_rides,
        f"adding shows changed the ride roster from {instance.n_rides} to "
        f"{committed.n_rides}; a show is not an attraction",
    )
    check(
        len(committed.show_indices) == 2,
        f"{len(committed.show_indices)} show pseudo-stops, expected 2",
    )

    # The invariant that keeps the drawn timeline honest about the solver's clock:
    # evaluate() re-derives `ride` from the roster rather than from the departure
    # table, so wait + ride must equal depart(t) - t exactly for a pseudo-stop.
    worst = 0.0
    for index in committed.show_indices:
        for minute in range(480, 1400, 7):
            wait = committed.wait_at(index, float(minute))
            ride = committed.attractions[index].ride_minutes
            depart = committed.depart_at(index, float(minute))
            worst = max(worst, abs((wait + ride) - (depart - minute)))
    check(
        worst < 1e-6,
        f"a show pseudo-stop breaks wait + ride == depart(t) - t by {worst:.6f} min; "
        "the timeline would then disagree with the schedule the solver optimized",
    )

    # FIFO must survive the flat region a fixed start time introduces.
    for index in committed.show_indices:
        row = committed.depart_table[index]
        drops = sum(1 for a, b in zip(row, row[1:]) if b < a)
        check(
            drops == 0,
            f"show departure row has {drops} decreasing steps; the exact DP's "
            "dominance argument requires depart(t) to be non-decreasing",
        )

    # A show cannot be attended late.
    for index in committed.show_indices:
        deadline = committed.latest_arrival[index]
        check(
            deadline < float("inf"),
            "a show pseudo-stop has no latest arrival, so the solver could 'attend' "
            "the parade an hour after it ended",
        )
        late = model.forward_pass(
            committed, [index], start=deadline + 5.0
        )[1]
        check(late == 0, "arriving after the deadline was accepted as feasible")
        ontime = model.forward_pass(
            committed, [index], start=max(0.0, deadline - 30.0)
        )[1]
        check(ontime == 1, "arriving before the deadline was rejected")

    # The break blocks the whole interval, on many random orders.
    rng = random.Random(SEED)
    ride_pool = list(committed.ride_indices)
    overlaps = 0
    for _ in range(60):
        order = rng.sample(ride_pool, rng.randint(4, 12))
        itinerary = model.evaluate(committed, order, truncate=True)
        for stop in itinerary.stops:
            if stop.arrive < lunch_end and stop.leave > lunch_start:
                overlaps += 1
    check(
        overlaps == 0,
        f"{overlaps} stops were in progress during the break; the plan promises that "
        "window is free of queueing",
    )

    # forward_pass and evaluate must produce the same schedule. They compute it
    # twice, independently, and a rule applied in only one of them is a timeline
    # that lies about the plan it is drawing.
    worst_gap = 0.0
    for _ in range(40):
        order = rng.sample(ride_pool, rng.randint(4, 12))
        departures, first_bad = model.forward_pass(committed, order)
        itinerary = model.evaluate(committed, order, truncate=True)
        for position, stop in enumerate(itinerary.stops[: len(departures)]):
            worst_gap = max(worst_gap, abs(stop.leave - departures[position]))
    check(
        worst_gap < 1e-6,
        f"evaluate and forward_pass disagree by {worst_gap:.6f} min on a stop's "
        "departure",
    )

    # The scaling knobs must not move a commitment.
    conn = db.connect()
    try:
        scaled_roster = db.roster(conn, mode="open_today")
        scaled_table = waits.build_wait_table(
            conn, names={a.id: a.name for a in db.roster(conn, mode="history")}
        )
        scaled_matrix = travel.build_travel_matrix(
            conn, config.PARK_ID, [a.id for a in scaled_roster]
        )
        scaled_day = db.operating_days(conn, start="2026-10-03", end="2026-10-03")[0]
        resolved = shows.showtime_for(conn, "fireworks", date="2026-10-03")
    finally:
        conn.close()
    commitment = model.ShowCommitment(
        node_id="__show_fireworks__", name=resolved.name,
        start_minute=resolved.start_minute, watch_minutes=resolved.watch_minutes,
        latest_arrival=resolved.latest_arrival,
        proxy_attraction=resolved.proxy_attraction, tier="show:t",
        latitude=0.0, longitude=0.0,
    )
    scaled = model.build_instance(
        scaled_day, scaled_roster, table=scaled_table, travel=scaled_matrix,
        assumptions=config.Assumptions(ride_scale=2.5, crowd_multiplier=1.8),
        shows=[commitment], lunch=(750.0, 795.0),
    )
    show_index = scaled.show_indices[0]
    check(
        abs(scaled.attractions[show_index].ride_minutes - resolved.watch_minutes) < 1e-9,
        "ride_scale changed the length of the fireworks; a show's duration is not an "
        "estimate of queue behaviour and must not move with that slider",
    )
    check(
        abs(scaled.wait_at(show_index, resolved.start_minute - 30.0) - 30.0) < 1e-6,
        "crowd_multiplier scaled the countdown to a fixed show time",
    )
    check(
        abs(scaled.committed_minutes - 45.0) < 1e-9,
        f"the break is {scaled.committed_minutes} min under scaling, expected 45",
    )

    # No move may drop a commitment. The objective's leading term is what enforces
    # this, so it is checked through the scorer rather than move by move.
    scorer = solver._Scorer(committed)
    base = [i for i in committed.required]
    base_key, _, _ = scorer.score(base)
    dropped_key, _, _ = scorer.score(base[1:])
    check(
        dropped_key > base_key,
        "an order missing a required stop does not score worse than one containing "
        "it, so `remove`, `swap_in` and `ruin_recreate` would be free to drop must-dos",
    )

    # Ranked fallback: the bottom of the list goes first, and says why.
    tight = bounds.with_window(
        committed, close_minute=committed.day.open_minute + 200
    )
    outcome = solver.solve_ranked(
        tight,
        [i for i, a in enumerate(committed.attractions) if a.id in ranked],
        solver.SolverConfig(restarts=3),
    )
    if outcome.dropped:
        order_of = {
            index: rank
            for rank, index in enumerate(
                i for i, a in enumerate(committed.attractions) if a.id in ranked
            )
        }
        first_dropped = order_of[outcome.dropped[0][0]]
        check(
            first_dropped == len(ranked) - 1,
            f"the first pick dropped was rank {first_dropped + 1} of {len(ranked)}; "
            "ranking means the LOWEST-ranked one goes first",
        )
        check(
            all(reason for _, reason in outcome.dropped),
            "a pick was dropped without a reason attached",
        )
    check(
        all(i in committed.show_indices or True for i, _ in outcome.dropped)
        and not any(i in tight.show_indices for i, _ in outcome.dropped),
        "the ranked fallback dropped a scheduled show; only ranked attractions are shed",
    )

    # A plan carrying commitments still reports counts about rides only.
    result = solver.solve(committed, solver.SolverConfig(restarts=2))
    itinerary = result.itinerary
    check(
        itinerary.count == sum(1 for s in itinerary.stops if s.kind == "ride"),
        "the itinerary count includes non-ride stops",
    )
    check(
        itinerary.n_stops >= itinerary.count,
        "n_stops is smaller than the ride count",
    )
    check(
        itinerary.waiting_minutes
        == sum(s.wait for s in itinerary.stops if s.kind == "ride"),
        "waiting_minutes absorbed the time spent holding a spot for a show",
    )

    # The bound must shrink by exactly the time the commitments consume.
    plain_budget = bounds.relaxation_bound(instance).budget_minutes
    committed_budget = bounds.relaxation_bound(committed).budget_minutes
    show_total = sum(
        committed.attractions[i].ride_minutes for i in committed.show_indices
    )
    expected_drop = committed.committed_minutes + show_total
    check(
        abs((plain_budget - committed_budget) - expected_drop) < 1e-6,
        f"the bound's budget dropped by {plain_budget - committed_budget:.2f} min, "
        f"expected {expected_drop:.2f}; a budget that still counts committed time as "
        "available would inflate the ceiling",
    )


    # The exact DP must solve the SAME problem the heuristic does. It carries its own
    # copy of the feasibility rules, so a rule added to forward_pass and forgotten
    # here would make "provably optimal" a proof about a different problem — one
    # where you may queue through lunch and arrive at the parade after it ended.
    conn = db.connect()
    try:
        dp_roster = db.roster(conn, mode="open_today")
        dp_table = waits.build_wait_table(
            conn, names={a.id: a.name for a in db.roster(conn, mode="history")}
        )
        dp_matrix = travel.build_travel_matrix(
            conn, config.PARK_ID, [a.id for a in dp_roster]
        )
        dp_day = db.operating_days(conn, start="2026-10-03", end="2026-10-03")[0]
        dp_show = shows.showtime_for(conn, "parade", date="2026-10-03")
    finally:
        conn.close()
    dp_instance = model.build_instance(
        dp_day, dp_roster, table=dp_table, travel=dp_matrix,
        shows=[
            model.ShowCommitment(
                node_id="__show_parade__", name=dp_show.name,
                start_minute=dp_show.start_minute,
                watch_minutes=dp_show.watch_minutes,
                latest_arrival=dp_show.latest_arrival,
                proxy_attraction=dp_show.proxy_attraction, tier="show:t",
                latitude=0.0, longitude=0.0,
            )
        ],
        lunch=(750.0, 795.0),
    )
    # A window that reaches past the parade, so the show is genuinely available.
    dp_tight = bounds.with_window(
        dp_instance, close_minute=dp_show.start_minute + dp_show.watch_minutes + 20.0
    )
    dp_subset = list(dp_tight.ride_indices[:5]) + list(dp_tight.show_indices)
    dp_count, dp_order, dp_finish = bounds.exact_dp(dp_tight, dp_subset)

    brute = (0, None, float("inf"))
    for size in range(len(dp_subset), 0, -1):
        for combo in itertools.combinations(dp_subset, size):
            for perm in itertools.permutations(combo):
                trial = model.evaluate(dp_tight, list(perm))
                if not trial.feasible:
                    continue
                stops = len(trial.stops)
                if stops > brute[0] or (
                    stops == brute[0] and trial.finish_minute < brute[2]
                ):
                    brute = (stops, perm, trial.finish_minute)
        if brute[0] == size:
            break
    check(
        dp_count == brute[0] and abs(dp_finish - brute[2]) < 1e-6,
        f"the exact DP found {dp_count} stops finishing {dp_finish:.3f} where "
        f"exhaustive enumeration found {brute[0]} finishing {brute[2]:.3f}, on an "
        "instance carrying a pinned show and a break",
    )
    dp_plan = model.evaluate(dp_tight, list(dp_order))
    check(
        not any(
            stop.arrive < 795.0 and stop.leave > 750.0 for stop in dp_plan.stops
        ),
        "the DP's own optimal plan queues through the break",
    )
    check(
        any(stop.kind == "show" for stop in dp_plan.stops),
        "the DP never scheduled the show even though the window reaches past it, so "
        "this comparison did not exercise the pinned-stop path",
    )

    return out


def test_research_orienteering():  # pytest entry point
    assert failures() == []


if __name__ == "__main__":
    problems = failures()
    for p in problems:
        print("FAIL:", p)
    print(
        "research orienteering: OK"
        if not problems
        else f"research orienteering: {len(problems)} failure(s)"
    )
    sys.exit(1 if problems else 0)
