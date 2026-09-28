"""Chart datasets for the article page.

Every series here is computed from the same tables and result files the article's
prose quotes, so a number in a chart and the same number in a sentence cannot
drift apart. Nothing is hardcoded except the factorial growth in `search_space`,
which is arithmetic rather than data.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import pandas as pd

from research_project import config, db, model, scenarios, travel, waits

RESULTS = Path(__file__).resolve().parents[1] / "results"

# Rides chosen to show four genuinely different intraday shapes, not four
# arbitrary series: a monotone climber, a midday peak that recovers, a classic
# hump, and a flat walk-on.
INTRADAY_RIDES = (
    "TRON Lightcycle / Run",
    "Pirates of the Caribbean",
    "Space Mountain",
    "Dumbo the Flying Elephant",
)

WALK_ON_MAX = 10.0
HEADLINER_MIN = 25.0


@lru_cache(maxsize=1)
def _tables():
    conn = db.connect()
    try:
        roster = db.roster(conn, mode="open_today")
        names = {a.id: a.name for a in db.roster(conn, mode="history")}
        table = waits.build_wait_table(conn, names=names)
        matrix = travel.build_travel_matrix(
            conn, config.PARK_ID, [a.id for a in roster]
        )
        day = db.operating_days(conn, config.PARK_ID, start="2026-09-26", end="2026-09-26")[0]
    finally:
        conn.close()
    return roster, names, table, matrix, day


def intraday() -> dict:
    """Mean wait by hour for four rides with different shapes (Saturday)."""
    roster, names, table, _, _ = _tables()
    by_name = {a.name: a.id for a in roster}
    hours = list(range(8, 23))
    series = []
    for ride in INTRADAY_RIDES:
        index = table.index_of(by_name[ride])
        values = [round(float(table.minutes[index, 5, h]), 1) for h in hours]
        series.append({"name": ride, "values": values})
    return {"hours": hours, "series": series}


# Jungle Cruise on a Tuesday evening carries the worst mid-day step violation in
# the dataset: the departure curve falls 18.6 minutes across 20:59 -> 21:00.
# Deliberately not the global worst (TRON at 22:59, 40.4 min), which sits on the
# last modelled minute and reads as an edge artifact rather than the real effect.
FIFO_RIDE = "Jungle Cruise"
FIFO_WEEKDAY = 1  # Tuesday
FIFO_WINDOW = (19 * 60, 22 * 60)


def fifo() -> dict:
    """Departure curves under linear vs step interpolation, across an hour edge.

    The whole model's correctness argument in one picture: under the step curve
    the line goes *down*, which means arriving later gets you out earlier. Sampled
    every minute, because the violation is exactly one minute wide.
    """
    conn = db.connect()
    try:
        names = {a.id: a.name for a in db.roster(conn, mode="history")}
        ride_id = next(i for i, n in names.items() if n == FIFO_RIDE)
        linear = waits.build_wait_table(conn, names=names)
        stepped = waits.build_wait_table(
            conn, assumptions=config.Assumptions(interpolation="step"), names=names
        )
    finally:
        conn.close()
    index = linear.index_of(ride_id)
    minutes = list(range(FIFO_WINDOW[0], FIFO_WINDOW[1] + 1))
    ride_minutes = config.ride_minutes(FIFO_RIDE)
    lin = linear.departure_curve(index, FIFO_WEEKDAY, ride_minutes)
    stp = stepped.departure_curve(index, FIFO_WEEKDAY, ride_minutes)
    step_values = [round(float(stp[m]), 2) for m in minutes]
    drops = [
        (minutes[i], round(step_values[i] - step_values[i + 1], 1))
        for i in range(len(step_values) - 1)
        if step_values[i + 1] < step_values[i]
    ]
    return {
        "minutes": minutes,
        "linear": [round(float(lin[m]), 2) for m in minutes],
        "step": step_values,
        "ride": FIFO_RIDE,
        "weekday": "Tuesday",
        "drops": drops,
        "worst_drop": max((d for _, d in drops), default=0.0),
    }


def distribution() -> dict:
    """Every attraction by mean wait, banded walk-on / middle / headliner."""
    roster, _, table, _, _ = _tables()
    rows = []
    for attraction in roster:
        index = table.index_of(attraction.id)
        mean = float(table.minutes[index, :, 9:21].mean())
        band = (
            "walk-on"
            if mean < WALK_ON_MAX
            else "headliner"
            if mean >= HEADLINER_MIN
            else "middle"
        )
        rows.append(
            {
                "name": attraction.name,
                "wait": round(mean, 1),
                "ride": round(attraction.ride_minutes, 1),
                "band": band,
            }
        )
    rows.sort(key=lambda r: r["wait"])
    bands = {b: sum(1 for r in rows if r["band"] == b) for b in ("walk-on", "middle", "headliner")}
    top = [r for r in rows if r["band"] == "headliner"]
    bottom = rows[: len(top)]
    return {
        "rows": rows,
        "bands": bands,
        "headliner_total": round(sum(r["wait"] for r in top)),
        "cheapest_total": round(sum(r["wait"] for r in bottom)),
        "n": len(top),
    }


def cliff() -> dict:
    """Attractions against window length, one line per crowd level."""
    frame = pd.read_csv(RESULTS / "factorial_sweep.csv")
    pivot = frame.pivot_table(
        index="window_hours", columns="crowd_level", values="count", aggfunc="mean"
    )
    levels = [float(c) for c in pivot.columns]
    return {
        "windows": [float(x) for x in pivot.index],
        "levels": levels,
        "series": [
            {
                "level": level,
                "values": [round(float(v), 2) for v in pivot[level].tolist()],
            }
            for level in levels
        ],
        "roster": int(frame["roster"].max()),
    }


def variance() -> dict:
    """Share of variance explained, restricted to the realistic crowd band."""
    frame = pd.read_csv(RESULTS / "factorial_sweep.csv")
    table = scenarios.variance_decomposition(
        frame, restrict={"crowd_level": scenarios.REALISTIC_CROWD}
    )
    label = {
        "window_minutes": "Window length",
        "open_minute": "Opening hour",
        "crowd_level": "Crowd level",
        "weekday": "Weekday shape",
    }
    return {
        "rows": [
            {"factor": label.get(r.factor, r.factor), "eta": round(float(r.eta_squared), 4)}
            for r in table.itertuples()
        ]
    }


def timing_gain() -> dict:
    """Queueing under optimal sequencing against the sum of average waits."""
    roster, names, table, matrix, day = _tables()
    from research_project import solver

    instance = model.build_instance(day, roster, table=table, travel=matrix)
    result = solver.solve(instance, solver.SolverConfig(restarts=8))
    naive = sum(
        float(table.minutes[table.index_of(a.id), :, 9:21].mean()) for a in roster
    )
    return {
        "sum_of_means": round(naive),
        "optimized": round(result.itinerary.waiting_minutes),
        "saved": round(naive - result.itinerary.waiting_minutes),
        "count": result.count,
        "window": round(day.length_minutes),
    }


def search_space() -> dict:
    """Distinct tours of n stops — why brute force is not an option."""
    points = [5, 10, 15, 20, 25, 30]
    return {
        "n": points,
        "tours": [math.factorial(n - 1) / 2 for n in points],
    }


def everything() -> dict:
    return {
        "intraday": intraday(),
        "fifo": fifo(),
        "distribution": distribution(),
        "cliff": cliff(),
        "variance": variance(),
        "timing_gain": timing_gain(),
        "search_space": search_space(),
    }
