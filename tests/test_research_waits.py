"""Data-hygiene tests for the route optimizer's inputs.

The optimizer's conclusions are only as good as the cells it learns from, and two
of the three traps in this dataset are silent: a pooled wait estimator is biased
but plausible-looking, and an unfiltered date carries post-close stale waits that
look like ordinary numbers. These assertions catch both by construction rather
than by inspection.

    .venv/bin/python tests/test_research_waits.py

Non-zero exit if anything diverges.
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from app import stats  # noqa: E402
from research_project import config, db, travel, waits  # noqa: E402

# The four geocoded Magic Kingdom entities with no queue and no wait history.
NON_QUEUE = {
    "Casey Jr. Splash 'N' Soak Station",
    "A Pirate's Adventure ~ Treasures of the Seven Seas",
    "Cinderella Castle",
    "Main Street Vehicles",
}


def failures() -> list[str]:
    out: list[str] = []

    def check(ok: bool, msg: str) -> None:
        if not ok:
            out.append(msg)

    conn = db.connect()

    # 1. Roster composition, both modes.
    history = db.roster(conn, mode="history")
    open_today = db.roster(conn, mode="open_today")
    check(len(history) == 31, f"history roster is {len(history)}, expected 31")
    check(
        len(open_today) == len(history) - 1,
        f"open_today roster is {len(open_today)}, expected one fewer than history",
    )
    names = {a.name for a in history}
    leaked = NON_QUEUE & names
    check(not leaked, f"non-queue entities in the roster: {sorted(leaked)}")
    refurbished = [a.name for a in history if a.refurbishment]
    check(
        len(refurbished) == len(history) - len(open_today),
        f"refurbishment count {len(refurbished)} does not explain the roster gap",
    )
    check(
        all(a.ride_minutes > 0 for a in history),
        "some attraction has a non-positive ride duration",
    )
    # Every roster name should have a curated duration, not the fallback, or the
    # ride-duration table has drifted from the roster.
    missing = sorted(n for n in names if n not in config.RIDE_MINUTES)
    check(not missing, f"no curated ride duration for: {missing}")

    # 2. Excluded dates appear nowhere in the learning set.
    dates = db.usable_dates(conn)
    check(bool(dates), "usable_dates returned nothing")
    present = sorted(set(config.EXCLUDED_DATES) & set(dates))
    check(not present, f"excluded dates leaked into the learning set: {present}")
    check(
        min(dates) >= config.HISTORY_START,
        f"learning set starts {min(dates)}, before HISTORY_START",
    )

    # 3. Every learning date has a complete OPERATING row. This is the invariant
    #    whose absence made 2026-06-24 unusable — asserting it directly catches
    #    the whole class, not just that one date.
    placeholders = ",".join("?" * len(dates))
    scheduled = {
        r[0]
        for r in conn.execute(
            f"""
            SELECT DISTINCT date FROM parkschedule
            WHERE park_id = ? AND type = 'OPERATING'
              AND opening_time IS NOT NULL AND closing_time IS NOT NULL
              AND date IN ({placeholders})
            """,
            (config.PARK_ID, *dates),
        ).fetchall()
    }
    unscheduled = sorted(set(dates) - scheduled)
    check(not unscheduled, f"learning dates with no OPERATING window: {unscheduled}")

    # 4. The estimator identity, recomputed here independently: the correct cell
    #    mean is the unweighted mean of per-row sum_wait/n_wait, and it must
    #    differ from the pooled Sum/Sum that the poll-interval change corrupts.
    aid = sorted(a.id for a in open_today)[0]
    rows = conn.execute(
        """
        SELECT date, hour, sum_wait, n_wait FROM attractionhourly
        WHERE attraction_id = ? AND hour = 11 AND n_wait > 0 AND date >= ?
        """,
        (aid, config.HISTORY_START),
    ).fetchall()
    rows = [r for r in rows if r["date"] not in config.EXCLUDED_DATES]
    if len(rows) > 2:
        per_row = [r["sum_wait"] / r["n_wait"] for r in rows]
        mean_of_means = statistics.mean(per_row)
        pooled = sum(r["sum_wait"] for r in rows) / sum(r["n_wait"] for r in rows)
        check(
            mean_of_means != pooled,
            "mean-of-means equals the pooled estimator — the poll-interval trap "
            "is not being exercised, so this test proves nothing",
        )

    # 5. Regression pin on the size of that trap, across the whole park. Pooling
    #    is biased HIGH because the dense pre-August dates are the high season.
    #    If anyone reintroduces a pooled estimator anywhere, this moves.
    ids = [a.id for a in history]
    ph = ",".join("?" * len(ids))
    cells: dict[tuple[str, int, int], list[tuple[float, int]]] = {}
    for r in conn.execute(
        f"""
        SELECT attraction_id, date, hour, sum_wait, n_wait
        FROM attractionhourly
        WHERE attraction_id IN ({ph}) AND date >= ? AND n_wait > 0
          AND hour BETWEEN 9 AND 21
        """,
        (*ids, config.HISTORY_START),
    ).fetchall():
        if r["date"] in config.EXCLUDED_DATES:
            continue
        import datetime as _dt

        weekday = _dt.date.fromisoformat(r["date"]).weekday()
        cells.setdefault((r["attraction_id"], weekday, r["hour"]), []).append(
            (r["sum_wait"], r["n_wait"])
        )
    deltas = []
    for parts in cells.values():
        if len(parts) < 2:
            continue
        mom = statistics.mean(s / n for s, n in parts)
        pooled = sum(s for s, _ in parts) / sum(n for _, n in parts)
        deltas.append(pooled - mom)
    if deltas:
        bias = statistics.mean(deltas)
        check(
            0.55 <= bias <= 0.95,
            f"pooled-minus-mean-of-means bias is {bias:+.3f} min, expected ~+0.74 "
            "(the poll-interval seam has shifted, or an estimator changed)",
        )

    # 6. The self-contained schedule reader agrees with the app's definition.
    sample = dates[-10:]
    theirs = stats._operating_windows(config.PARK_ID, sample)
    mine = {
        d.date: d
        for d in db.operating_days(conn, start=min(sample), end=max(sample))
    }
    for day in sample:
        if day not in theirs:
            continue
        check(day in mine, f"{day}: app has an OPERATING window, we do not")
        if day in mine:
            opened = theirs[day][0].hour * 60 + theirs[day][0].minute
            check(
                abs(mine[day].open_minute - opened) < 1e-6,
                f"{day}: open minute {mine[day].open_minute} != app's {opened}",
            )

    # 7. Windows are coherent, and closes past midnight survive as >1440 rather
    #    than wrapping to a negative length.
    forward = db.operating_days(conn, start="2026-09-25", end="2026-10-25")
    check(bool(forward), "no forward OPERATING days found")
    check(
        all(d.length_minutes > 0 for d in forward),
        "some window has a non-positive length (a past-midnight close wrapped)",
    )
    check(
        all(0 <= d.open_minute < 1440 for d in forward),
        "some window opens outside its own day",
    )
    check(
        all(0 <= d.weekday <= 6 for d in forward),
        "some window has an out-of-range weekday",
    )

    # 8. The estimator itself.
    names = {a.name: a.id for a in history}
    durations = {a.id: a.ride_minutes for a in history}
    table = waits.build_wait_table(conn, names={a.id: a.name for a in history})
    check(
        len(table.attraction_ids) == len(history),
        f"wait table covers {len(table.attraction_ids)} attractions, roster is {len(history)}",
    )

    core = table.support(hours=range(9, 22))
    check(
        core.get("cell", 0.0) >= 0.90,
        f"only {core.get('cell', 0.0):.1%} of hour-9-21 cells are direct evidence, expected >=90%",
    )
    check(
        core.get("park", 0.0) == 0.0,
        "the park-mean tier fired inside hours 9-21 — the fallback chain has a hole",
    )
    full = table.support()
    check(
        full.get("park", 0.0) == 0.0,
        "the park-mean tier fired inside MODEL_HOURS — the fallback chain has a hole",
    )

    # Every estimate is finite, non-negative and within what was ever observed.
    for i, aid in enumerate(table.attraction_ids):
        block = table.minutes[i, :, list(config.MODEL_HOURS)]
        if not np.all(np.isfinite(block)):
            out.append(f"{aid}: non-finite wait estimate")
            break
        if block.min() < 0:
            out.append(f"{aid}: negative wait estimate")
            break
        if block.max() > table.observed_max[i] + 1e-9:
            out.append(
                f"{aid}: estimate {block.max():.1f} exceeds observed max "
                f"{table.observed_max[i]:.1f}"
            )
            break

    # 9. Interpolation anchors: at an hour's midpoint the curve equals that
    #    hour's stored value, because the stored value IS the midpoint estimate.
    index = table.index_of(names["Pirates of the Caribbean"])
    for hour in (10, 14, 19):
        anchored = table.at(index, 5, 60 * hour + 30)
        stored = float(table.minutes[index, 5, hour])
        check(
            abs(anchored - stored) < 1e-6,
            f"hour {hour} midpoint reads {anchored:.3f}, stored value is {stored:.3f}",
        )

    # 10. FIFO — the single most valuable assertion here. depart(t) must be
    #     non-decreasing, or the exact subset DP's (visited, last) -> earliest
    #     completion dominance is invalid and local search can exploit cliffs.
    #     Asserted to HOLD under linear and to FAIL under step, so the reason for
    #     the choice is pinned in code rather than in a comment.
    violations = 0
    worst_slope = 0.0
    for i, aid in enumerate(table.attraction_ids):
        for weekday in range(7):
            departures = table.departure_curve(i, weekday, durations.get(aid, 5.0))
            steps = np.diff(departures)
            violations += int((steps < 0).sum())
            worst_slope = max(
                worst_slope, float(np.abs(np.diff(table.minute_curve(i, weekday))).max())
            )
    check(violations == 0, f"{violations} FIFO violations under linear interpolation")
    check(
        worst_slope < 1.0,
        f"max |dw/dt| is {worst_slope:.3f}; depart() is only increasing below 1.0",
    )

    stepped = waits.build_wait_table(
        conn,
        assumptions=config.Assumptions(interpolation="step"),
        names={a.id: a.name for a in history},
    )
    step_violations = 0
    for i, aid in enumerate(stepped.attraction_ids):
        for weekday in range(7):
            steps = np.diff(stepped.departure_curve(i, weekday, durations.get(aid, 5.0)))
            step_violations += int((steps < 0).sum())
    check(
        step_violations > 0,
        "step interpolation produced no FIFO violations — either the data changed "
        "or this control is no longer exercising the artifact it exists to show",
    )

    # 11. The travel matrix.
    ids = [a.id for a in open_today]
    absent = travel.missing_pairs(conn, config.PARK_ID, ids)
    check(not absent, f"{len(absent)} roster pairs have no walking distance")

    matrix = travel.build_travel_matrix(conn, config.PARK_ID, ids)
    size = len(matrix.node_ids)
    check(size == len(ids) + 1, f"matrix has {size} nodes, expected {len(ids) + 1}")
    check(matrix.node_ids[0] == travel.ENTRANCE, "the entrance is not node 0")
    check(
        np.isfinite(matrix.minutes).all(),
        "the travel matrix contains a non-finite entry",
    )
    check(
        np.abs(matrix.minutes - matrix.minutes.T).max() < 1e-9,
        "the travel matrix is not symmetric",
    )
    check(
        np.abs(np.diag(matrix.minutes)).max() < 1e-9,
        "the travel matrix has a non-zero diagonal",
    )

    # The per-leg overhead must not break the triangle inequality: a two-leg path
    # collects two overheads where a one-leg path collects one, so it survives —
    # but the exact DP and the travel lower bound both depend on it, so assert it.
    violations = 0
    for i in range(size):
        slack = (
            matrix.minutes[i, :, None] + matrix.minutes[None, :, :] - matrix.minutes[i, :]
        )
        violations += int((slack.min(axis=0) < -1e-9).sum())
    check(
        violations == 0,
        f"{violations} triangle-inequality violations in the travel matrix",
    )

    # The entrance sits on top of its alias, and nowhere else is free.
    alias_index = matrix.index(matrix.entrance_alias)
    check(
        matrix.meters[0, alias_index] == 0.0,
        "the entrance is not co-located with its alias attraction",
    )
    others = [
        matrix.meters[0, i]
        for i in range(1, size)
        if i != alias_index
    ]
    check(min(others) > 0.0, "some non-alias attraction is 0 m from the entrance")

    # Agreement with the published definition, which this bulk query replaces.
    worst = 0.0
    for a_id in ids[:8]:
        for b_id in ids[8:16]:
            published = stats.walking_distance_between(a_id, b_id)
            if published is not None:
                worst = max(worst, abs(matrix.meters_between(a_id, b_id) - published))
    check(
        worst < 1e-6,
        f"bulk walking distances diverge from app.stats.walking_distance_between "
        f"by up to {worst:.4f} m",
    )

    # 12. Read-only proof. Cheap, and it is what guarantees a research package can
    #     never damage the collector's database.
    try:
        conn.execute("CREATE TABLE _research_write_probe (x INTEGER)")
        out.append("the research connection is WRITABLE — it must be mode=ro")
    except sqlite3.OperationalError:
        pass

    conn.close()
    return out


def test_research_waits():  # pytest entry point
    assert failures() == []


if __name__ == "__main__":
    problems = failures()
    for p in problems:
        print("FAIL:", p)
    print(
        "research waits: OK"
        if not problems
        else f"research waits: {len(problems)} failure(s)"
    )
    sys.exit(1 if problems else 0)
