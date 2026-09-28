"""Panel loading and the calendar facts a future date can legitimately carry.

Two properties here are load-bearing and are the reason this file exists rather
than a call to `research_project.waits`:

1. **Per-row division.** `n_wait` is a POLL COUNT, and the collector moved from
   one-minute to five-minute polling on 2026-08-04 (ADR-0006), so it fell from
   ~54 per hour to ~11. Pooling `SUM(sum_wait) / SUM(n_wait)` across that seam
   weights the older, denser period about 5x and biases estimates high by ~0.74
   minutes on average. Every mean here divides per row first, so each observation
   is a mean over its own hour whatever the interval was.

2. **The coverage probe is per (park, date), not per date.** `waits.py` computes
   `MAX(n_wait)` per date because it only ever looks at one park. Across seven
   parks with different opening hours, a park-wide maximum would mis-scale
   coverage for the short-hours parks and silently screen out their real rows.
   This is the one genuine change from `waits.py`, and a test pins it.
"""

from __future__ import annotations

import sqlite3
from datetime import date as date_cls
from typing import Collection, Iterable

import numpy as np
import pandas as pd

from forecast import config
from research_project import db as rdb

# ── the panel ─────────────────────────────────────────────────────────────

_PANEL_SQL = """
WITH roster AS (
    SELECT a.id AS attraction_id, a.park_id AS park_id, a.name AS name
    FROM attraction a
    WHERE a.park_id IN ({parks})
      AND EXISTS (SELECT 1 FROM attractionhourly h WHERE h.attraction_id = a.id)
),
scheduled AS (
    SELECT DISTINCT park_id, date
    FROM parkschedule
    WHERE type = 'OPERATING'
      AND opening_time IS NOT NULL
      AND closing_time IS NOT NULL
),
row_mean AS (
    SELECT r.park_id              AS park_id,
           r.attraction_id        AS attraction_id,
           r.name                 AS name,
           h.date                 AS date,
           h.hour                 AS hour,
           h.sum_wait / h.n_wait  AS mean_wait,   -- per row; never pooled
           h.n_wait               AS n_wait
    FROM attractionhourly h
    JOIN roster    r ON r.attraction_id = h.attraction_id
    JOIN scheduled s ON s.date = h.date AND s.park_id = r.park_id
    WHERE h.n_wait > 0
      AND h.date >= :history_start
),
density AS (
    -- per (park, date): parks keep different hours, so a global MAX would
    -- mis-scale coverage for whichever park closes earliest.
    SELECT park_id, date, MAX(n_wait) AS rows_per_full_hour
    FROM row_mean
    GROUP BY park_id, date
)
SELECT rm.park_id, rm.attraction_id, rm.name, rm.date, rm.hour, rm.mean_wait,
       CAST(rm.n_wait AS REAL) / d.rows_per_full_hour AS coverage
FROM row_mean rm
JOIN density d ON d.park_id = rm.park_id AND d.date = rm.date
ORDER BY rm.park_id, rm.attraction_id, rm.date, rm.hour
"""


def load_panel(
    conn: sqlite3.Connection,
    *,
    history_start: str = config.HISTORY_START,
    excluded_dates: Collection[str] = config.EXCLUDED_DATES,
    min_hour_coverage: float = config.DEFAULTS.min_hour_coverage,
    parks: Iterable[str] = config.PARK_ORDER,
) -> pd.DataFrame:
    """One row per (park, attraction, date, hour) with its mean posted wait."""
    park_ids = list(parks)
    sql = _PANEL_SQL.format(parks=",".join("?" * len(park_ids)))
    # sqlite3 will not mix qmark and named params, so history_start rides along
    # positionally at the end.
    frame = pd.read_sql_query(sql.replace(":history_start", "?"), conn,
                              params=[*park_ids, history_start])
    frame = frame[~frame["date"].isin(set(excluded_dates))]
    frame = frame[frame["coverage"] >= min_hour_coverage]
    frame["weekday"] = pd.to_datetime(frame["date"]).dt.weekday
    frame["log_wait"] = np.log1p(frame["mean_wait"])
    return frame.reset_index(drop=True)


def park_day_levels(panel: pd.DataFrame) -> pd.DataFrame:
    """The modelling unit: one row per (park, date) with its mean log wait.

    Restricted to CORE_HOURS so the level tracks how busy the day was rather
    than which parks happened to open early. This is what the day model is
    fitted on and scored against — n is a few hundred, not a hundred thousand,
    and keeping that visible in the shape of the data is the point.
    """
    core = panel[panel["hour"].isin(config.CORE_HOURS)]
    levels = (
        core.groupby(["park_id", "date"], as_index=False)
        .agg(level=("log_wait", "mean"),
             mean_wait=("mean_wait", "mean"),
             n_rows=("log_wait", "size"))
    )
    levels["weekday"] = pd.to_datetime(levels["date"]).dt.weekday
    return levels.sort_values(["park_id", "date"]).reset_index(drop=True)


# ── calendar facts, all knowable before the date arrives ──────────────────


def calendar(
    conn: sqlite3.Connection,
    *,
    start: str,
    end: str,
    parks: Iterable[str] = config.PARK_ORDER,
) -> pd.DataFrame:
    """Published schedule per (park, date): hours, opening, and the party flag.

    Built by looping `research_project.db.operating_days`, so the after-hours
    party definition and the past-midnight close handling (close_minute > 1440)
    are shared with the optimizer rather than re-derived here.
    """
    rows = []
    for park_id in parks:
        for day in rdb.operating_days(conn, park_id, start=start, end=end):
            rows.append(
                {
                    "park_id": park_id,
                    "date": day.date,
                    "weekday": day.weekday,
                    "open_minute": day.open_minute,
                    "close_minute": day.close_minute,
                    "hours": day.length_minutes / 60.0,
                    "party_night": bool(day.party_night),
                }
            )
    columns = ["park_id", "date", "weekday", "open_minute", "close_minute",
               "hours", "party_night"]
    if not rows:
        # A date past the schedule ceiling has no rows at all. Returning a frame
        # with the right columns lets callers test `.empty` instead of crashing on
        # a missing key, which is how the horizon refusal is supposed to trigger.
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(rows, columns=columns)
    return frame.sort_values(["park_id", "date"]).reset_index(drop=True)


def last_observed_date(conn: sqlite3.Connection) -> str:
    """The newest date with hourly rollups — the forecast ORIGIN.

    Deliberately not `today`: the rollup runs nightly and lags the raw feed by a
    day or two, so the model's information genuinely ends here. Every horizon in
    the app, the CLI and the article is measured from this date, and it is shown
    to the user rather than left implicit.
    """
    row = conn.execute("SELECT MAX(date) AS d FROM attractionhourly").fetchone()
    return row["d"] if isinstance(row, sqlite3.Row) else row[0]


def forecastable_dates(
    conn: sqlite3.Connection,
    *,
    origin: str | None = None,
    not_before: str | None = None,
    parks: Iterable[str] = config.PARK_ORDER,
) -> list[str]:
    """Future dates with a published OPERATING row for every park.

    The schedule feed reaches about a month out. Past that there are no real
    park hours, and park hours are the single strongest feature the model has,
    so those dates are refused rather than served on an imputed input.

    `origin` is what horizons are measured from (the last rollup date).
    `not_before` additionally hides dates the *user* would find odd to plan for —
    the app passes today, so it never offers a "forecast" for yesterday, while
    the backtest leaves it unset and scores every date after the origin.
    """
    park_ids = list(parks)
    origin = origin or last_observed_date(conn)
    placeholders = ",".join("?" * len(park_ids))
    rows = conn.execute(
        f"""
        SELECT date, COUNT(DISTINCT park_id) AS n
        FROM parkschedule
        WHERE type = 'OPERATING' AND date > ?
          AND park_id IN ({placeholders})
          AND opening_time IS NOT NULL AND closing_time IS NOT NULL
        GROUP BY date ORDER BY date
        """,
        [origin, *park_ids],
    ).fetchall()
    out = [r["date"] for r in rows if r["n"] == len(park_ids)]
    if not_before:
        out = [d for d in out if d >= not_before]
    return out


def horizon_of(origin: str, target: str) -> int:
    return (date_cls.fromisoformat(target) - date_cls.fromisoformat(origin)).days


def horizon_band(horizon: int) -> str:
    for lo, hi in config.HORIZON_BANDS:
        if lo <= horizon <= hi:
            return f"{lo}-{hi}"
    return f"{config.HORIZON_BANDS[-1][1]}+"


def wait_band(wait: float) -> str:
    if wait < config.WALK_ON_MAX:
        return "walk-on"
    if wait >= config.HEADLINER_MIN:
        return "headliner"
    return "middle"
