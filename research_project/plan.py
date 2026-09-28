"""Turn a visitor's stated wishes into one plan, in one place.

The CLI and the web app both need the same orchestration — resolve the day, resolve
the showtimes, build the instance, run the ranked solve, and describe the result —
so it lives here rather than twice. The output is a plain dict because the web app
serializes it and the CLI prints it, and neither should need to know about
`Itinerary` internals.

What this adds over `solver.solve_ranked` is the explaining: which picks were
honoured and which were shed and why, what the plan expects you to do first, and
how much of each number is measured rather than assumed.
"""

from __future__ import annotations

import sqlite3
from typing import Mapping, Sequence

from research_project import bounds, config, db, model, shows, solver, travel, waits

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def clock(minute: float) -> str:
    total = int(round(minute))
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def roster_options(
    roster: Sequence[db.RosterAttraction], mean_wait: Mapping[str, float]
) -> list[dict]:
    """The must-do menu: every attraction the visitor may pick, with its cost.

    Sorted by mean wait descending, because the choice a visitor actually agonizes
    over is which headliners to commit to; a 5-minute walk-on needs no planning and
    the solver will fit it in regardless.
    """
    out = [
        {
            "id": a.id,
            "name": a.name,
            "mean_wait": round(float(mean_wait.get(a.id, 0.0)), 1),
            "ride": round(a.ride_minutes, 1),
            "band": (
                "headliner"
                if mean_wait.get(a.id, 0.0) >= 25.0
                else "walk-on"
                if mean_wait.get(a.id, 0.0) < 10.0
                else "middle"
            ),
        }
        for a in roster
    ]
    out.sort(key=lambda r: -r["mean_wait"])
    return out


def narrative(
    itinerary: model.Itinerary,
    *,
    lunch: tuple[float, float] | None,
    mean_wait: Mapping[str, float] | None = None,
) -> list[str]:
    """A few sentences saying what to actually do, in order.

    The table and the map both answer "what is the plan"; neither answers "where do
    I go first", which is the question a visitor standing at the gate is asking.
    """
    lines: list[str] = []
    stops = itinerary.stops
    if not stops:
        return ["No attraction fits this day under these settings."]

    first = stops[0]
    lines.append(
        f"Head straight to {first.name} and be in the queue by "
        f"{clock(first.arrive)} — the first hour is the cheapest of the day."
    )

    must = [s for s in stops if s.must_do]
    if must:
        total = sum(s.wait for s in must)
        # Say what the schedule actually does rather than asserting it front-loads.
        # Most must-dos go early because queues are shortest then, but a cheap one
        # is deliberately deferred — and claiming otherwise while the table shows
        # Jungle Cruise at 21:55 would just look like the planner cannot read its
        # own plan.
        midday = (stops[0].arrive + itinerary.finish_minute) / 2
        early = [s for s in must if s.arrive <= midday]
        late = [s for s in must if s.arrive > midday]
        lines.append(
            f"Your {len(must)} must-do{'s' if len(must) != 1 else ''} cost "
            f"{total:.0f} minutes of queueing in total."
        )
        if early:
            lines.append(
                f"{len(early)} of them "
                f"{'goes' if len(early) == 1 else 'go'} in the first half of the day — "
                + ", ".join(f"{s.name} at {clock(s.arrive)}" for s in early[:3])
                + (", and so on" if len(early) > 3 else "")
                + " — because that is when their queues are shortest."
            )
        if late:
            # State the comparison that is actually true. The tempting sentence —
            # "shorter in the evening than this morning" — is false for a ride like
            # Jungle Cruise, which is cheap at BOTH ends of the day (7 min at 08:00,
            # 11 min at 22:00) and expensive in the middle. It is scheduled late
            # because the morning trough was worth more spent on a pricier must-do,
            # and that is a different claim.
            averages = mean_wait or {}
            parts = []
            for stop in late:
                key = itinerary.instance.attractions[stop.index].id
                average = averages.get(key)
                if average:
                    parts.append(
                        f"{stop.name} at {clock(stop.arrive)} for {stop.wait:.0f} min "
                        f"against a {average:.0f}-minute daily average"
                    )
                else:
                    parts.append(
                        f"{stop.name} at {clock(stop.arrive)} for {stop.wait:.0f} min"
                    )
            lines.append(
                "Held back on purpose: "
                + "; ".join(parts)
                + ". The evening is a second cheap window, so the plan spends the "
                "morning one on the must-dos that cost the most."
            )

    if lunch is not None:
        lines.append(
            f"Eat between {clock(lunch[0])} and {clock(lunch[1])} — the plan keeps "
            "that window clear of queues rather than assuming you can eat in one."
        )

    for stop in stops:
        if stop.kind == "show":
            lines.append(
                f"Be in position for {stop.name} by {clock(stop.arrive)}; it starts "
                f"at {clock(stop.board)} and you are moving again by "
                f"{clock(stop.leave)}."
            )

    extras = [s for s in stops if s.kind == "ride" and not s.must_do]
    if extras:
        lines.append(
            f"Everything else — {len(extras)} further attraction"
            f"{'s' if len(extras) != 1 else ''} — is filler the plan slots around "
            "those commitments, so dropping one costs you nothing that you asked for."
        )
    return lines


def build_plan(
    conn: sqlite3.Connection,
    *,
    date: str,
    roster: Sequence[db.RosterAttraction],
    table: waits.WaitTable,
    matrix: travel.TravelMatrix,
    mean_wait: Mapping[str, float],
    must_do: Sequence[str] = (),
    lunch_start: float | None = None,
    lunch_minutes: float | None = None,
    show_keys: Sequence[str] = (),
    assumptions: config.Assumptions | None = None,
    restarts: int = 8,
) -> dict:
    """Plan one day. `must_do` is attraction ids in the visitor's priority order."""
    assumptions = assumptions or config.Assumptions()
    days = db.operating_days(conn, config.PARK_ID, start=date, end=date)
    if not days:
        raise ValueError(f"no OPERATING schedule for {date}")
    day = days[0]

    if len(must_do) > MAX_MUST_DO:
        raise ValueError(f"at most {MAX_MUST_DO} must-do picks, got {len(must_do)}")

    by_id = {a.id: i for i, a in enumerate(roster)}
    unknown = [i for i in must_do if i not in by_id]
    if unknown:
        raise ValueError(f"unknown attraction id(s): {unknown}")

    # ── shows ────────────────────────────────────────────────────────────
    offered = shows.available(
        conn,
        date=date,
        close_minute=day.close_minute,
        party_night=day.party_night,
        assumptions=assumptions,
    )
    commitments: list[model.ShowCommitment] = []
    show_rows: list[dict] = []
    for key in show_keys:
        if key not in offered:
            raise ValueError(f"unknown show {key!r}")
        info = offered[key]
        if not info["available"]:
            show_rows.append({**info, "scheduled": False})
            continue
        resolved = shows.showtime_for(
            conn, key, date=date, assumptions=assumptions
        )
        commitments.append(
            model.ShowCommitment(
                node_id=f"__show_{key}__",
                name=resolved.name,
                start_minute=resolved.start_minute,
                watch_minutes=resolved.watch_minutes,
                latest_arrival=resolved.latest_arrival,
                proxy_attraction=resolved.proxy_attraction,
                tier=f"show:{resolved.tier}",
                latitude=_proxy_coord(roster, resolved.proxy_attraction, "lat"),
                longitude=_proxy_coord(roster, resolved.proxy_attraction, "lon"),
            )
        )
        show_rows.append({**info, "scheduled": True})

    # ── lunch ────────────────────────────────────────────────────────────
    lunch: tuple[float, float] | None = None
    if lunch_start is not None:
        minutes = lunch_minutes if lunch_minutes is not None else assumptions.lunch_minutes
        lunch = (float(lunch_start), float(lunch_start) + float(minutes))

    instance = model.build_instance(
        day,
        roster,
        table=table,
        travel=matrix,
        assumptions=assumptions,
        shows=commitments,
        lunch=lunch,
        required_ids=list(must_do),
    )

    ranked = [by_id[i] for i in must_do]
    outcome = solver.solve_ranked(
        instance, ranked, solver.SolverConfig(restarts=restarts)
    )
    result = outcome.result
    itinerary = result.itinerary
    bound = bounds.relaxation_bound(instance)
    certificate = bounds.certify(instance, itinerary.count, bound)

    entrance = next(
        (a for a in roster if a.name == config.ENTRANCE_PROXY_NAME), roster[0]
    )

    return {
        "date": date,
        "weekday": WEEKDAYS[day.weekday],
        "open": clock(day.open_minute),
        "close": clock(day.close_minute),
        "open_minute": day.open_minute,
        "close_minute": day.close_minute,
        "window_hours": round(day.length_minutes / 60.0, 2),
        "party_night": day.party_night,
        # what was asked for, and what became of it
        "must_do": [
            {
                "id": instance.attractions[i].id,
                "name": instance.attractions[i].name,
                "rank": position + 1,
                "honoured": True,
            }
            for position, i in enumerate(outcome.honoured)
        ]
        + [
            {
                "id": instance.attractions[i].id,
                "name": instance.attractions[i].name,
                "rank": ranked.index(i) + 1 if i in ranked else None,
                "honoured": False,
                "reason": reason,
            }
            for i, reason in outcome.dropped
        ],
        "must_do_complete": outcome.complete,
        "must_do_wait": round(itinerary.must_do_wait, 1),
        "attempts": outcome.attempts,
        "shows": show_rows,
        "lunch": (
            {
                "start": clock(lunch[0]),
                "end": clock(lunch[1]),
                "start_minute": lunch[0],
                "minutes": lunch[1] - lunch[0],
            }
            if lunch
            else None
        ),
        # the plan
        "count": itinerary.count,
        "roster": instance.n_rides,
        "n_stops": itinerary.n_stops,
        "finish": clock(itinerary.finish_minute),
        "finish_minute": itinerary.finish_minute,
        "slack_minutes": round(result.slack_minutes, 1),
        "upper_bound": bound.value,
        "gap": certificate["gap"],
        "proven_optimal": bool(certificate["optimal"]),
        "bound_vacuous": bound.vacuous,
        "walking_minutes": round(itinerary.walking_minutes, 1),
        "waiting_minutes": round(itinerary.waiting_minutes, 1),
        "riding_minutes": round(itinerary.riding_minutes, 1),
        "show_minutes": round(itinerary.show_minutes, 1),
        "break_minutes": round(itinerary.break_minutes, 1),
        "support": itinerary.support(),
        "narrative": narrative(itinerary, lunch=lunch, mean_wait=mean_wait),
        "risks": risks(itinerary, day.close_minute),
        "stops": [
            {
                "n": position + 1,
                "name": stop.name,
                "kind": stop.kind,
                "must_do": stop.must_do,
                "arrive": clock(stop.arrive),
                "arrive_minute": stop.arrive,
                "wait": round(stop.wait, 1),
                "ride": round(stop.ride, 1),
                "leave": clock(stop.leave),
                "leave_minute": stop.leave,
                "tier": stop.tier,
                "lat": instance.attractions[stop.index].latitude,
                "lon": instance.attractions[stop.index].longitude,
            }
            for position, stop in enumerate(itinerary.stops)
        ],
        "skipped": [
            {
                "name": a.name,
                "lat": a.latitude,
                "lon": a.longitude,
                "mean_wait": round(float(mean_wait.get(a.id, 0.0)), 1),
                "ride": round(a.ride_minutes, 1),
            }
            for index, a in enumerate(instance.attractions)
            if instance.kinds[index] == "ride"
            and index not in {s.index for s in itinerary.stops}
        ],
        "entrance": {"lat": entrance.latitude, "lon": entrance.longitude},
        "violation": itinerary.violation,
        "seconds": round(result.seconds, 2),
        "evaluations": result.evaluations,
        "assumptions_key": result.assumptions_key,
        "assumptions": {name: value for name, value, _ in assumptions.provenance()},
        "provenance": {name: source for name, _, source in assumptions.provenance()},
    }


MAX_MUST_DO = 5

# A must-do scheduled inside this many minutes of the last valid queue-join is
# flagged. The plan is not wrong — evening queues really are shorter — but "your
# top pick is at 22:52 and the queue shuts at 23:00" is something a visitor must be
# told rather than left to discover at 22:55.
LATE_RISK_MINUTES = 60.0


def risks(itinerary: model.Itinerary, close_minute: float) -> list[dict]:
    """Things about this plan a visitor should know before following it.

    The optimizer answers "what is best on average". It has no concept of a ride
    breaking down, and it will happily put the single attraction you most wanted at
    the very end of the day because that is when its queue is shortest. Both facts
    are true and only one of them is comfortable.
    """
    out: list[dict] = []
    for stop in itinerary.stops:
        if not stop.must_do:
            continue
        margin = close_minute - stop.arrive
        if margin <= LATE_RISK_MINUTES:
            out.append(
                {
                    "kind": "late_must_do",
                    "name": stop.name,
                    "arrive": clock(stop.arrive),
                    "margin_minutes": round(margin, 1),
                    "message": (
                        f"{stop.name} is one of your must-dos and the plan joins its "
                        f"queue at {clock(stop.arrive)}, only "
                        f"{margin:.0f} minutes before the last valid join. Its queue "
                        "is genuinely shorter then, but one breakdown or one slow "
                        "walk and you lose it — consider ranking it higher or "
                        "riding it early and accepting the longer wait."
                    ),
                }
            )
    return out


def _proxy_coord(
    roster: Sequence[db.RosterAttraction], name: str, which: str
) -> float:
    for attraction in roster:
        if attraction.name == name:
            return attraction.latitude if which == "lat" else attraction.longitude
    return 0.0
