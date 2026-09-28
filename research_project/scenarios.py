"""Scenario sweep and factor attribution.

Two arms, because the calendar alone cannot attribute cause.

**Arm A, the calendar** — the real forward dates with their true windows. It says
which actual day is worst, and produces the itineraries a guest could use. It
cannot say *why*, because weekday and window length are confounded by
construction: Friday closes at 18:00 on 12 of 18 observed dates and Saturday at
23:00 on 15 of 18, so "Friday" and "9-hour day" are very nearly the same
observation.

**Arm B, a factorial grid** — combinations the calendar never produces, which is
the only way to separate the two. A long Friday and a short Saturday both exist
here even though neither exists in reality.

The grid's load-bearing detail is that **crowd level is normalized out of the
weekday shape and given its own axis**. A weekday curve carries both a shape (when
the peaks fall) and a level (how busy the day is overall); leaving them merged
would mean measuring one variable twice and calling the result a decomposition.
So each weekday's own level is divided out and the target level multiplied back
in, letting the sweep ask "Saturday's shape at Tuesday's crowd level" — a question
the calendar cannot pose.
"""

from __future__ import annotations

import csv
import dataclasses
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Sequence

import numpy as np
import pandas as pd

from research_project import bounds, config, db, model, solver, travel, waits
from research_project.waits import WaitTable

# Realistic ranges, measured: the calendar's own weekday levels span 0.93 (Sunday)
# to 1.08 (Wednesday). The outer two levels exist to show the shape of the
# response; the inner four sample *inside* the realistic band, because every
# headline claim is read off the restricted sub-range.
#
# An earlier grid used 0.80/0.90/1.00/1.10/1.25, which put exactly one level
# inside the band — so restricting collapsed the crowd axis to a single value,
# dropped it from the decomposition, and reported a crowd effect of zero that was
# an artifact of the design rather than a fact about the park.
CROWD_LEVELS = (0.80, 0.93, 0.98, 1.03, 1.08, 1.25)
REALISTIC_CROWD = (0.92, 1.09)
WINDOW_MINUTES = (540, 600, 660, 720, 780, 840, 900)
OPEN_MINUTES = (480, 540)  # 08:00 and 09:00; 25 forward dates open at 08:00

WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class Scenario:
    """One instance to solve, from either arm."""

    arm: Literal["calendar", "factorial"]
    weekday: int
    open_minute: float
    close_minute: float
    crowd_level: float
    date: str | None = None
    party_night: bool = False
    assumptions: config.Assumptions = dataclasses.field(
        default_factory=config.Assumptions
    )

    @property
    def window_minutes(self) -> float:
        return self.close_minute - self.open_minute

    @property
    def label(self) -> str:
        if self.date:
            return self.date
        return (
            f"{WEEKDAY_NAMES[self.weekday]}-{int(self.window_minutes)}m"
            f"-c{self.crowd_level:.2f}-o{int(self.open_minute)}"
        )


# ── normalizing the weekday level out of the shape ────────────────────────


def weekday_levels(table: WaitTable) -> dict[int, float]:
    """Mean wait per weekday over the modelled hours — the level, not the shape."""
    window = list(config.MODEL_HOURS)
    return {
        weekday: float(np.nanmean(table.minutes[:, weekday, window]))
        for weekday in range(7)
    }


def crowd_multiplier(levels: dict[int, float], weekday: int, crowd_level: float) -> float:
    """Divide out a weekday's own busyness, multiply the target level back in.

    Without this, "weekday" and "crowd level" are the same variable and the
    variance decomposition is meaningless.
    """
    mean_level = statistics.mean(levels.values())
    own = levels[weekday]
    if own <= 0:
        return crowd_level
    return crowd_level * mean_level / own


# ── building the two arms ─────────────────────────────────────────────────


def calendar_scenarios(
    conn,
    *,
    start: str,
    end: str,
    assumptions: config.Assumptions | None = None,
) -> list[Scenario]:
    """The real forward dates, with their true operating windows."""
    assumptions = assumptions or config.Assumptions()
    return [
        Scenario(
            arm="calendar",
            weekday=day.weekday,
            open_minute=day.open_minute,
            close_minute=day.close_minute,
            crowd_level=1.0,
            date=day.date,
            party_night=day.party_night,
            assumptions=assumptions,
        )
        for day in db.operating_days(conn, config.PARK_ID, start=start, end=end)
    ]


def factorial_scenarios(
    *,
    weekdays: Sequence[int] = tuple(range(7)),
    crowd_levels: Sequence[float] = CROWD_LEVELS,
    window_minutes: Sequence[int] = WINDOW_MINUTES,
    open_minutes: Sequence[int] = OPEN_MINUTES,
    assumptions: config.Assumptions | None = None,
) -> list[Scenario]:
    """The crossed grid, including combinations the calendar never produces."""
    assumptions = assumptions or config.Assumptions()
    return [
        Scenario(
            arm="factorial",
            weekday=weekday,
            open_minute=float(opens),
            close_minute=float(opens + window),
            crowd_level=crowd,
            assumptions=assumptions,
        )
        for weekday in weekdays
        for crowd in crowd_levels
        for window in window_minutes
        for opens in open_minutes
    ]


# ── running ───────────────────────────────────────────────────────────────


def run_sweep(
    conn,
    scenarios: Iterable[Scenario],
    *,
    solver_config: solver.SolverConfig | None = None,
    with_bounds: bool = True,
    progress: bool = False,
) -> pd.DataFrame:
    """Solve every scenario and return one row each.

    Every row carries `assumptions` (the hash from `Assumptions.key()`), so a
    sweep can never silently mix results computed under different constants —
    the classic way a parameter study quietly invalidates itself.
    """
    solver_config = solver_config or solver.SolverConfig(restarts=4, time_limit_s=30.0)
    roster = db.roster(conn, mode="open_today")
    names = {a.id: a.name for a in db.roster(conn, mode="history")}
    table = waits.build_wait_table(conn, names=names)
    matrix = travel.build_travel_matrix(conn, config.PARK_ID, [a.id for a in roster])
    levels = weekday_levels(table)

    rows: list[dict] = []
    scenarios = list(scenarios)
    for position, scenario in enumerate(scenarios):
        multiplier = crowd_multiplier(levels, scenario.weekday, scenario.crowd_level)
        assumptions = dataclasses.replace(
            scenario.assumptions, crowd_multiplier=multiplier
        )
        day = db.DayWindow(
            date=scenario.date or f"synthetic-{scenario.label}",
            weekday=scenario.weekday,
            open_minute=scenario.open_minute,
            close_minute=scenario.close_minute,
            party_night=scenario.party_night,
        )
        instance = model.build_instance(
            day, roster, table=table, travel=matrix, assumptions=assumptions
        )
        result = solver.solve(instance, solver_config)
        row = {
            "arm": scenario.arm,
            "label": scenario.label,
            "date": scenario.date or "",
            "weekday": scenario.weekday,
            "weekday_name": WEEKDAY_NAMES[scenario.weekday],
            "open_minute": scenario.open_minute,
            "window_minutes": scenario.window_minutes,
            "window_hours": round(scenario.window_minutes / 60, 2),
            "crowd_level": scenario.crowd_level,
            "party_night": int(scenario.party_night),
            "count": result.count,
            "roster": instance.n,
            "finish_minute": round(result.finish_minute, 2),
            "slack_minutes": round(result.slack_minutes, 2),
            "walking_minutes": round(result.itinerary.walking_minutes, 2),
            "waiting_minutes": round(result.itinerary.waiting_minutes, 2),
            "assumptions": result.assumptions_key,
        }
        if with_bounds:
            bound = bounds.relaxation_bound(instance)
            row["upper_bound"] = bound.value
            row["gap"] = bound.value - result.count
        rows.append(row)
        if progress and (position + 1) % 25 == 0:
            print(f"  ... {position + 1}/{len(scenarios)}", flush=True)
    return pd.DataFrame(rows)


# ── attribution ───────────────────────────────────────────────────────────


def variance_decomposition(
    frame: pd.DataFrame,
    *,
    response: str = "count",
    factors: Sequence[str] = ("window_minutes", "crowd_level", "weekday", "open_minute"),
    restrict: dict | None = None,
) -> pd.DataFrame:
    """Share of variance in `response` attributable to each factor (eta squared).

    Report this **twice**: once over the full grid and once restricted to the
    calendar-realistic sub-range. The unrestricted version flatters whichever
    factor was swept widest, which is a choice of the experimenter rather than a
    fact about the park; the restricted version is the honest answer.
    """
    data = frame
    if restrict:
        for column, (low, high) in restrict.items():
            data = data[(data[column] >= low) & (data[column] <= high)]
    if data.empty:
        return pd.DataFrame(columns=["factor", "eta_squared", "levels", "n"])

    grand = data[response].mean()
    total_ss = float(((data[response] - grand) ** 2).sum())
    rows = []
    for factor in factors:
        if factor not in data or data[factor].nunique() < 2:
            continue
        between = 0.0
        for _, group in data.groupby(factor):
            between += len(group) * (group[response].mean() - grand) ** 2
        rows.append(
            {
                "factor": factor,
                "eta_squared": round(between / total_ss, 4) if total_ss else 0.0,
                "levels": int(data[factor].nunique()),
                "n": int(len(data)),
            }
        )
    return pd.DataFrame(rows).sort_values("eta_squared", ascending=False)


def response_curve(
    frame: pd.DataFrame, *, x: str = "window_hours", by: str = "crowd_level"
) -> pd.DataFrame:
    """Mean response against `x`, one column per level of `by`."""
    return frame.pivot_table(
        index=x, columns=by, values="count", aggfunc="mean"
    ).round(2)


def cliff_location(frame: pd.DataFrame, *, by: str = "crowd_level") -> pd.DataFrame:
    """Shortest window at which the full roster still fits, per level of `by`.

    The response is expected to be flat-then-cliff rather than a smooth slope, so
    the interesting statistic is where the cliff sits, not the gradient.
    """
    rows = []
    for level, group in frame.groupby(by):
        roster = int(group["roster"].max())
        full = group[group["count"] >= roster]
        rows.append(
            {
                by: level,
                "cliff_hours": (
                    round(float(full["window_hours"].min()), 2) if not full.empty else None
                ),
                "max_count": int(group["count"].max()),
            }
        )
    return pd.DataFrame(rows)


def marginal_rates(frame: pd.DataFrame) -> dict[str, float]:
    """Attractions gained per extra hour, and lost per unit of crowd level.

    Both are read over the realistic crowd range, and the window slope is taken
    below the cliff where the response is not saturated.
    """
    realistic = frame[
        (frame["crowd_level"] >= REALISTIC_CROWD[0])
        & (frame["crowd_level"] <= REALISTIC_CROWD[1])
    ]
    below = realistic[realistic["count"] < realistic["roster"]]

    by_window = below.groupby("window_hours")["count"].mean()
    window_slope = (
        float(np.polyfit(by_window.index, by_window.values, 1)[0])
        if len(by_window) > 1
        else float("nan")
    )

    by_crowd = below.groupby("crowd_level")["count"].mean()
    crowd_span = (
        float(by_crowd.iloc[0] - by_crowd.iloc[-1]) if len(by_crowd) > 1 else 0.0
    )

    return {
        "attractions_per_hour": round(window_slope, 3),
        "attractions_over_realistic_crowd_span": round(crowd_span, 3),
        "equivalence_minutes": (
            round(abs(crowd_span) / window_slope * 60, 1) if window_slope else float("nan")
        ),
    }


def shapley_attribution(
    conn,
    dates: Sequence[str],
    *,
    solver_config: solver.SolverConfig | None = None,
) -> pd.DataFrame:
    """Exact two-player Shapley per date: window length versus crowd level.

    With two factors the Shapley value is exact and costs four evaluations — the
    four corners of {own window, median window} x {own crowd, median crowd},
    averaged over both orderings. That turns "this day is bad" into "this day
    loses N attractions, of which X is the schedule and Y is the crowd".
    """
    solver_config = solver_config or solver.SolverConfig(restarts=4, time_limit_s=30.0)
    days = {
        day.date: day
        for day in db.operating_days(
            conn, config.PARK_ID, start=min(dates), end=max(dates)
        )
    }
    windows = [days[d].length_minutes for d in dates if d in days]
    median_window = statistics.median(windows)

    # The crowd axis is the normalized level, not the weekday. Swapping in a
    # different weekday to represent "median crowd" would change the curve's
    # *shape* as well as its level, and would silently pick a weekday that is not
    # actually median — Wednesday is the busiest day, not the middle one. Since
    # `crowd_multiplier` already divides each weekday's own level out, holding the
    # weekday fixed and setting crowd_level isolates the level cleanly.
    table = waits.build_wait_table(
        conn, names={a.id: a.name for a in db.roster(conn, mode="history")}
    )
    levels = weekday_levels(table)
    mean_level = statistics.mean(levels.values())
    own_crowd = {wd: levels[wd] / mean_level for wd in levels}
    median_crowd = statistics.median(own_crowd.values())

    scenarios: list[Scenario] = []
    index: list[tuple[str, str]] = []
    for date in dates:
        day = days.get(date)
        if day is None:
            continue
        for window_label, window in (
            ("own", day.length_minutes),
            ("median", median_window),
        ):
            for crowd_label, crowd in (
                ("own", own_crowd[day.weekday]),
                ("median", median_crowd),
            ):
                scenarios.append(
                    Scenario(
                        arm="factorial",
                        weekday=day.weekday,
                        open_minute=day.open_minute,
                        close_minute=day.open_minute + window,
                        crowd_level=crowd,
                        date=None,
                        party_night=day.party_night,
                    )
                )
                index.append((date, f"{window_label}_{crowd_label}"))

    frame = run_sweep(conn, scenarios, solver_config=solver_config, with_bounds=False)
    frame["date"] = [d for d, _ in index]
    frame["corner"] = [c for _, c in index]
    wide = frame.pivot_table(index="date", columns="corner", values="count")

    out = []
    for date, row in wide.iterrows():
        both = row.get("own_own")
        neither = row.get("median_median")
        window_only = row.get("own_median")
        crowd_only = row.get("median_own")
        if None in (both, neither, window_only, crowd_only):
            continue
        # Exact Shapley for two players: average the two orderings.
        window_share = ((window_only - neither) + (both - crowd_only)) / 2
        crowd_share = ((crowd_only - neither) + (both - window_only)) / 2
        out.append(
            {
                "date": date,
                "count": both,
                "baseline": neither,
                "total_effect": both - neither,
                "window_share": round(window_share, 3),
                "crowd_share": round(crowd_share, 3),
            }
        )
    return pd.DataFrame(out).sort_values("total_effect")


def write_csv(frame: pd.DataFrame, path: str | Path) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(destination, index=False, quoting=csv.QUOTE_MINIMAL)
    return destination
