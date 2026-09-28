"""Command line entry point for the route optimizer.

    python -m research_project.cli plan --date 2026-10-03
    python -m research_project.cli plan --date 2026-10-03 --explain
    python -m research_project.cli dates
    python -m research_project.cli sweep --out research_project/results
    python -m research_project.cli prove --n 16 --window 240

A thin argparse shell in the style of `app/walking.py:main` — all the work lives
in the modules, so this file stays readable and every subcommand is a few lines.
"""

from __future__ import annotations

import argparse
import sys

from research_project import plan as plan_mod
from research_project import (
    bounds,
    config,
    db,
    model,
    scenarios,
    solver,
    travel,
    waits,
)

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _load(conn, *, assumptions: config.Assumptions):
    roster = db.roster(conn, mode=assumptions.roster_mode, ride_scale=1.0)
    names = {a.id: a.name for a in db.roster(conn, mode="history")}
    table = waits.build_wait_table(conn, assumptions=assumptions, names=names)
    matrix = travel.build_travel_matrix(
        conn, config.PARK_ID, [a.id for a in roster], assumptions=assumptions
    )
    return roster, table, matrix


def cmd_plan(args: argparse.Namespace) -> int:
    assumptions = config.Assumptions(
        walk_speed_mps=args.walk_speed,
        ride_scale=args.ride_scale,
        crowd_multiplier=args.crowd,
    )
    conn = db.connect()
    roster, table, matrix = _load(conn, assumptions=assumptions)
    mean_wait = {
        a.id: float(table.minutes[table.index_of(a.id), :, 9:21].mean())
        for a in roster
    }

    if args.list_attractions:
        # The onboarding menu, in the terminal: what there is to choose from and
        # what each one costs, so a `--must-do` list can be written from evidence.
        conn.close()
        options = plan_mod.roster_options(roster, mean_wait)
        print(f"{len(options)} attractions, dearest first — pick up to "
              f"{plan_mod.MAX_MUST_DO} and rank them")
        for option in options:
            print(f"  {option['mean_wait']:5.1f} min mean   "
                  f"ride {option['ride']:4.1f}   {option['band']:<10} {option['name']}")
        return 0

    picks: list[str] = []
    if args.must_do:
        by_name = {a.name.lower(): a.id for a in roster}
        for raw in args.must_do.split(","):
            wanted = raw.strip()
            if not wanted:
                continue
            match = by_name.get(wanted.lower()) or next(
                (a.id for a in roster if wanted.lower() in a.name.lower()), None
            )
            if match is None:
                print(f"no attraction matches {wanted!r}; try --list-attractions",
                      file=sys.stderr)
                conn.close()
                return 1
            if match not in picks:
                picks.append(match)
        if len(picks) > plan_mod.MAX_MUST_DO:
            print(f"at most {plan_mod.MAX_MUST_DO} must-do picks, got {len(picks)}",
                  file=sys.stderr)
            conn.close()
            return 1

    lunch_start = None
    if args.lunch:
        try:
            hours, minutes = args.lunch.split(":")
            lunch_start = int(hours) * 60 + int(minutes)
        except ValueError:
            print(f"--lunch wants HH:MM, got {args.lunch!r}", file=sys.stderr)
            conn.close()
            return 1

    show_keys = tuple(k.strip() for k in args.shows.split(",") if k.strip()) \
        if args.shows else ()
    for key in show_keys:
        if key not in config.SHOWS:
            print(f"unknown show {key!r}; known: {', '.join(config.SHOWS)}",
                  file=sys.stderr)
            conn.close()
            return 1

    try:
        result = plan_mod.build_plan(
            conn,
            date=args.date,
            roster=roster,
            table=table,
            matrix=matrix,
            mean_wait=mean_wait,
            must_do=picks,
            lunch_start=lunch_start,
            lunch_minutes=args.lunch_minutes,
            show_keys=show_keys,
            assumptions=assumptions,
            restarts=args.restarts,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        conn.close()
        return 1
    conn.close()

    print(
        f"{result['date']} ({result['weekday']})  "
        f"{result['open']}-{result['close']}  {result['window_hours']:.1f} h"
        + ("  [after-hours party]" if result["party_night"] else "")
    )
    print(
        f"{result['count']} of {result['roster']} attractions, finishing "
        f"{result['finish']} ({result['slack_minutes']:.0f} min before the last "
        "queue closes)"
    )
    verdict = (
        "proven optimal"
        if result["proven_optimal"]
        else f"upper bound {result['upper_bound']}, gap {result['gap']}"
    )
    print(f"{verdict}   [assumptions {result['assumptions_key']}]")

    if result["must_do"]:
        print()
        print("must-dos:")
        for entry in result["must_do"]:
            if entry["honoured"]:
                print(f"  {entry['rank']}. {entry['name']}")
            else:
                print(f"  {entry['rank']}. {entry['name']}  — DROPPED: {entry['reason']}")
        if not result["must_do_complete"]:
            print("  (ranked lowest-first, so the bottom of your list goes first)")
        print(f"  queueing on your picks: {result['must_do_wait']:.0f} min")

    for show in result["shows"]:
        state = "scheduled" if show["scheduled"] else "NOT scheduled"
        note = f" — {show['reason']}" if show.get("reason") else ""
        print(f"\n{show['name']}: {show['start']} ({state}), "
              f"be there by {show['arrive_by']}{note}")
        print(f"  timing evidence: {show['tier']} "
              f"({show['n_dates']} captured date(s)); "
              f"routed via {show['proxy_attraction']}, "
              f"{show['proxy_offset_m']:.0f} m from the real spot")

    if result["lunch"]:
        print(f"\nlunch {result['lunch']['start']}-{result['lunch']['end']} "
              f"({result['lunch']['minutes']:.0f} min, kept free of queues)")

    print()
    for stop in result["stops"]:
        tag = "MUST" if stop["must_do"] else ("SHOW" if stop["kind"] == "show" else "    ")
        print(
            f"{stop['n']:3d}. {stop['arrive']}  {tag}  {stop['name']:<44}"
            f" wait {stop['wait']:5.1f}  ride {stop['ride']:4.1f}"
            + (f"   [{stop['tier']}]" if args.explain else "")
        )

    if result["skipped"]:
        print()
        print(f"skipped ({len(result['skipped'])}):")
        for entry in sorted(result["skipped"], key=lambda r: -r["mean_wait"]):
            print(f"     {entry['mean_wait']:5.1f} min mean   {entry['name']}")

    print()
    print("the plan, in words:")
    for line in result["narrative"]:
        print(f"  - {line}")

    if result["risks"]:
        print()
        print("worth knowing:")
        for risk in result["risks"]:
            print(f"  ! {risk['message']}")

    if args.explain:
        print()
        print(
            f"walking {result['walking_minutes']:.0f} min | "
            f"queueing {result['waiting_minutes']:.0f} min | "
            f"riding {result['riding_minutes']:.0f} min | "
            f"shows {result['show_minutes']:.0f} min | "
            f"break {result['break_minutes']:.0f} min"
        )
        print(
            "wait-estimate provenance over the stops actually used: "
            + ", ".join(f"{k} {v:.0%}" for k, v in sorted(result["support"].items()))
        )
        print()
        print("assumptions (none of these are measured):")
        for name, value in result["assumptions"].items():
            print(f"     {name:<26} {str(value):<10} {result['provenance'][name]}")
    return 0


def cmd_dates(args: argparse.Namespace) -> int:
    conn = db.connect()
    days = db.operating_days(conn, config.PARK_ID, start=args.start, end=args.end)
    conn.close()
    print(f"{len(days)} OPERATING dates")
    for day in days:
        print(
            f"  {day.date} {WEEKDAYS[day.weekday]}  "
            f"{model._clock(day.open_minute)}-{model._clock(day.close_minute)}  "
            f"{day.length_minutes / 60:4.1f} h"
            + ("  party" if day.party_night else "")
        )
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    conn = db.connect()
    solver_config = solver.SolverConfig(restarts=args.restarts, seed=args.seed)
    grid = scenarios.factorial_scenarios()
    print(f"factorial grid: {len(grid)} scenarios", flush=True)
    factorial = scenarios.run_sweep(
        conn, grid, solver_config=solver_config, progress=True
    )
    scenarios.write_csv(factorial, f"{args.out}/factorial_sweep.csv")

    calendar_scenarios = scenarios.calendar_scenarios(
        conn, start=args.start, end=args.end
    )
    calendar = scenarios.run_sweep(conn, calendar_scenarios, solver_config=solver_config)
    scenarios.write_csv(calendar, f"{args.out}/calendar_sweep.csv")

    dates = [s.date for s in calendar_scenarios if s.date]
    shapley = scenarios.shapley_attribution(conn, dates, solver_config=solver_config)
    scenarios.write_csv(shapley, f"{args.out}/shapley.csv")
    conn.close()
    print(f"wrote 3 files to {args.out}/")
    return 0


def cmd_prove(args: argparse.Namespace) -> int:
    """Solve a reduced instance exactly and compare the heuristic against it."""
    conn = db.connect()
    assumptions = config.Assumptions()
    roster, table, matrix = _load(conn, assumptions=assumptions)
    day = db.operating_days(conn, config.PARK_ID, start=args.date, end=args.date)[0]
    instance = model.build_instance(day, roster, table=table, travel=matrix)
    conn.close()

    states, megabytes = bounds.dp_cost_estimate(args.n)
    print(f"n={args.n}: peak layer {states:,} states, ~{megabytes:.1f} MB")
    reduced, subset = bounds.reduced_instance(
        instance, args.n, window_minutes=args.window
    )
    exact, order, finish = bounds.exact_dp(reduced, subset)
    heuristic = solver.solve(
        reduced, solver.SolverConfig(restarts=args.restarts), candidates=list(subset)
    )
    binding = exact < len(subset)
    print(f"exact optimum {exact}, heuristic {heuristic.count}, finish {model._clock(finish)}")
    print(
        ("MATCH" if heuristic.count == exact else "HEURISTIC SHORT")
        + (
            ""
            if binding
            else "  — but the subproblem is NOT binding, so this proves nothing"
        )
    )
    for index in order:
        print(f"     {reduced.attractions[index].name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="best itinerary for one date")
    plan.add_argument("--date", required=True, help="YYYY-MM-DD")
    plan.add_argument("--restarts", type=int, default=12)
    plan.add_argument("--seed", type=int, default=config.SEED)
    plan.add_argument("--walk-speed", type=float, default=1.1, dest="walk_speed")
    plan.add_argument("--ride-scale", type=float, default=1.0, dest="ride_scale")
    plan.add_argument("--crowd", type=float, default=1.0)
    plan.add_argument(
        "--must-do",
        dest="must_do",
        default=None,
        help=(
            "comma-separated attractions you will not leave without, HIGHEST "
            "PRIORITY FIRST (max 5). Partial names match. When they cannot all "
            "fit, the last one goes."
        ),
    )
    plan.add_argument(
        "--list-attractions",
        action="store_true",
        dest="list_attractions",
        help="print the pickable attractions with their mean waits, then exit",
    )
    plan.add_argument("--lunch", default=None, help="break start time, HH:MM")
    plan.add_argument(
        "--lunch-minutes", type=float, default=None, dest="lunch_minutes",
        help="how long the break lasts (default from the assumption ledger)",
    )
    plan.add_argument(
        "--shows",
        default=None,
        help=f"comma-separated scheduled shows to attend: {', '.join(config.SHOWS)}",
    )
    plan.add_argument(
        "--explain",
        action="store_true",
        help="show provenance tiers and the assumption ledger",
    )
    plan.set_defaults(func=cmd_plan)

    dates = sub.add_parser("dates", help="list OPERATING dates and their windows")
    dates.add_argument("--start", default="2026-09-25")
    dates.add_argument("--end", default="2026-10-25")
    dates.set_defaults(func=cmd_dates)

    sweep = sub.add_parser("sweep", help="run both sweep arms and write CSVs")
    sweep.add_argument("--out", default="research_project/results")
    sweep.add_argument("--start", default="2026-09-25")
    sweep.add_argument("--end", default="2026-10-25")
    sweep.add_argument("--restarts", type=int, default=4)
    sweep.add_argument("--seed", type=int, default=config.SEED)
    sweep.set_defaults(func=cmd_sweep)

    prove = sub.add_parser("prove", help="exact DP on a reduced instance")
    prove.add_argument("--n", type=int, default=16)
    prove.add_argument("--window", type=int, default=None)
    prove.add_argument("--date", default="2026-09-27")
    prove.add_argument("--restarts", type=int, default=8)
    prove.set_defaults(func=cmd_prove)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
