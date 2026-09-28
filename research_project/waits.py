"""Time-dependent wait estimator.

Turns `attractionhourly` into a per-attraction, per-weekday, per-minute wait
curve, with a provenance tier on every estimate so the article can state how much
of the model rests on direct evidence rather than on a fallback.

Two properties are load-bearing:

**The cell mean is the unweighted mean of per-row `sum_wait / n_wait`**, never the
pooled `SUM(sum_wait) / SUM(n_wait)`. The poll interval changed on 2026-08-04
(ADR-0006), so `n_wait` is a poll count that fell from ~54 to ~11 per hour.
Pooling weights each *poll* equally and therefore weights pre-August dates ~5x,
biasing estimates high by ~0.74 min on average and up to 15 min on single cells.
Dividing per row makes every (date, hour) observation a mean over its own hour,
whatever the interval was, and the cell is then a mean over dates.

**Waits are interpolated linearly between hour midpoints**, which is a
correctness requirement rather than smoothing. A stored value is a mean *over* an
hour, so it estimates the wait at that hour's midpoint. A step function would
both carry a phase error of up to 30 minutes and make `depart(t) = t + w(t) + R`
non-monotonic — arriving at 18:01 could beat 17:59, so a later arrival could
produce an earlier departure. That breaks FIFO, which invalidates the
`(visited_set, last) -> earliest completion` dominance the exact DP rests on, and
hands local search artificial cliffs to hunt. The measured worst hour-to-hour
change is 22.8 min per 60 (|dw/dt| <= 0.38), so under linear interpolation
`depart` is strictly increasing and the DP is sound. `tests/test_research_waits.py`
asserts FIFO holds under "linear" and fails under "step", so the reason for the
choice is pinned in code.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Literal, Mapping, Sequence

import numpy as np
import pandas as pd

from research_project import config

Tier = Literal["cell", "weekday_class", "hour", "shape", "attraction", "park"]

# Worst-first ordering: the tier of an interpolated minute is the worse of the
# two hours bracketing it, so a minute is never reported as better-supported than
# the evidence on both sides of it.
TIER_ORDER: tuple[Tier, ...] = (
    "cell",
    "weekday_class",
    "hour",
    "shape",
    "attraction",
    "park",
)
_TIER_RANK = {t: i for i, t in enumerate(TIER_ORDER)}

MINUTES_PER_DAY = 1440


# ── loading ───────────────────────────────────────────────────────────────

# `MAX(n_wait)` per date is an interval *probe*, not a hardcoded 1-vs-5 minutes:
# it reads ~60 on a one-minute date and ~12 on a five-minute one without knowing
# when ADR-0006 landed, so a future interval change needs no edit here. It only
# works because the mixed changeover date is excluded (config.EXCLUDED_DATES).
_CELL_SQL = """
WITH roster AS (
    SELECT a.id
    FROM attraction a
    WHERE a.park_id = :park_id
      AND a.latitude IS NOT NULL
      AND EXISTS (SELECT 1 FROM attractionhourly h WHERE h.attraction_id = a.id)
),
scheduled AS (
    SELECT DISTINCT date
    FROM parkschedule
    WHERE park_id = :park_id
      AND type = 'OPERATING'
      AND opening_time IS NOT NULL
      AND closing_time IS NOT NULL
),
row_mean AS (
    SELECT h.attraction_id        AS attraction_id,
           h.date                 AS date,
           h.hour                 AS hour,
           h.sum_wait / h.n_wait  AS mean_wait,
           h.n_wait               AS n_wait
    FROM attractionhourly h
    JOIN roster    r ON r.id   = h.attraction_id
    JOIN scheduled s ON s.date = h.date
    WHERE h.n_wait > 0
      AND h.date >= :history_start
),
density AS (
    SELECT date, MAX(n_wait) AS rows_per_full_hour
    FROM row_mean
    GROUP BY date
)
SELECT rm.attraction_id, rm.date, rm.hour, rm.mean_wait,
       CAST(rm.n_wait AS REAL) / d.rows_per_full_hour AS coverage
FROM row_mean rm
JOIN density d ON d.date = rm.date
ORDER BY rm.attraction_id, rm.hour, rm.date
"""


def load_cells(
    conn: sqlite3.Connection,
    park_id: str = config.PARK_ID,
    *,
    history_start: str = config.HISTORY_START,
    excluded_dates: Iterable[str] = config.EXCLUDED_DATES,
    min_hour_coverage: float = config.MIN_HOUR_COVERAGE,
) -> pd.DataFrame:
    """One row per (attraction, date, hour) observation that survives screening.

    Excluded dates are dropped here rather than in SQL so the exclusion list stays
    in one visible place and a test can assert it took effect.
    """
    frame = pd.read_sql_query(
        _CELL_SQL,
        conn,
        params={"park_id": park_id, "history_start": history_start},
    )
    frame = frame[~frame["date"].isin(set(excluded_dates))]
    frame = frame[frame["coverage"] >= min_hour_coverage]
    frame["weekday"] = pd.to_datetime(frame["date"]).dt.weekday
    frame["weekend"] = frame["weekday"].isin(config.WEEKEND)
    return frame.reset_index(drop=True)


# ── the table ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WaitTable:
    """Hourly wait estimates and their provenance, per attraction and weekday."""

    attraction_ids: tuple[str, ...]
    names: Mapping[str, str]
    minutes: np.ndarray  # (n_attr, 7, 24) float64
    tier: np.ndarray  # (n_attr, 7, 24) unicode
    n_dates: np.ndarray  # (n_attr, 7, 24) int32
    observed_max: np.ndarray  # (n_attr,) float64
    interpolation: Literal["linear", "step"] = "linear"
    _curves: dict = field(default_factory=dict, repr=False, compare=False)

    def index_of(self, attraction_id: str) -> int:
        return self.attraction_ids.index(attraction_id)

    # ── per-minute curves ────────────────────────────────────────────────

    def minute_curve(self, index: int, weekday: int) -> np.ndarray:
        """Wait in minutes for every minute-of-day, interpolated from the hours."""
        cached = self._curves.get(("w", index, weekday))
        if cached is None:
            cached = self._build_minute_curve(index, weekday)
            self._curves[("w", index, weekday)] = cached
        return cached

    def tier_curve(self, index: int, weekday: int) -> np.ndarray:
        """Provenance tier for every minute-of-day (the worse bracketing hour)."""
        cached = self._curves.get(("t", index, weekday))
        if cached is None:
            cached = self._build_tier_curve(index, weekday)
            self._curves[("t", index, weekday)] = cached
        return cached

    def _supported_hours(self, index: int, weekday: int) -> list[int]:
        return [
            h
            for h in config.MODEL_HOURS
            if np.isfinite(self.minutes[index, weekday, h])
        ]

    def _build_minute_curve(self, index: int, weekday: int) -> np.ndarray:
        hours = self._supported_hours(index, weekday)
        if not hours:
            return np.zeros(MINUTES_PER_DAY)
        values = np.array([self.minutes[index, weekday, h] for h in hours])
        if self.interpolation == "step":
            # Kept only to demonstrate the artifact it causes; see module docstring.
            curve = np.empty(MINUTES_PER_DAY)
            for minute in range(MINUTES_PER_DAY):
                hour = min(hours, key=lambda h: abs(h - minute // 60))
                curve[minute] = self.minutes[index, weekday, hour]
            return curve
        # A stored value is a mean OVER the hour, so it belongs at the midpoint.
        # np.interp clamps flat outside the supported range, which is what we want
        # at the ends of the day.
        midpoints = np.array([60 * h + 30 for h in hours], dtype=float)
        curve = np.interp(np.arange(MINUTES_PER_DAY, dtype=float), midpoints, values)
        return np.clip(curve, 0.0, self.observed_max[index])

    def _build_tier_curve(self, index: int, weekday: int) -> np.ndarray:
        hours = self._supported_hours(index, weekday)
        curve = np.full(MINUTES_PER_DAY, "park", dtype="<U13")
        if not hours:
            return curve
        midpoints = [60 * h + 30 for h in hours]
        tiers = [self.tier[index, weekday, h] for h in hours]
        for minute in range(MINUTES_PER_DAY):
            position = np.searchsorted(midpoints, minute)
            left = max(0, position - 1)
            right = min(len(hours) - 1, position)
            curve[minute] = max(
                (tiers[left], tiers[right]), key=lambda t: _TIER_RANK[str(t)]
            )
        return curve

    def departure_curve(
        self,
        index: int,
        weekday: int,
        ride_minutes: float,
        *,
        wait_factor: float = 1.0,
    ) -> np.ndarray:
        """`arrival -> departure` for every minute-of-day.

        Strictly increasing under linear interpolation, which is what licenses the
        exact subset DP and the monotone forward pass.
        """
        arrivals = np.arange(MINUTES_PER_DAY, dtype=float)
        return arrivals + wait_factor * self.minute_curve(index, weekday) + ride_minutes

    # ── point lookups ────────────────────────────────────────────────────

    def at(self, index: int, weekday: int, minute_of_day: float) -> float:
        """Wait at a fractional minute, interpolated between whole minutes."""
        curve = self.minute_curve(index, weekday)
        clamped = min(max(minute_of_day, 0.0), MINUTES_PER_DAY - 1.0)
        low = int(np.floor(clamped))
        high = min(low + 1, MINUTES_PER_DAY - 1)
        weight = clamped - low
        return float(curve[low] * (1.0 - weight) + curve[high] * weight)

    def tier_at(self, index: int, weekday: int, minute_of_day: float) -> Tier:
        clamped = int(min(max(minute_of_day, 0.0), MINUTES_PER_DAY - 1.0))
        return str(self.tier_curve(index, weekday)[clamped])  # type: ignore[return-value]

    # ── provenance reporting ─────────────────────────────────────────────

    def support(self, *, hours: Sequence[int] | None = None) -> dict[str, float]:
        """Share of (attraction, weekday, hour) cells at each tier."""
        window = list(hours) if hours is not None else list(config.MODEL_HOURS)
        block = self.tier[:, :, window].ravel()
        total = block.size
        return {
            tier: float((block == tier).sum()) / total
            for tier in TIER_ORDER
            if (block == tier).any()
        }

    def itinerary_support(
        self, lookups: Iterable[tuple[int, int, float]]
    ) -> dict[str, float]:
        """Share of *actually used* lookups at each tier.

        This matters more than `support()`: the optimizer leans on the cheapest
        hours, and the cheapest hour is 09:00 — plus hour 8 on early-open days,
        which has only 17 dates of history. Aggregate cell coverage can look
        excellent while the itinerary that gets published leans on the weakest
        cells.
        """
        seen: list[str] = [
            str(self.tier_at(index, weekday, minute))
            for index, weekday, minute in lookups
        ]
        if not seen:
            return {}
        return {
            tier: seen.count(tier) / len(seen)
            for tier in TIER_ORDER
            if tier in seen
        }


# ── construction ──────────────────────────────────────────────────────────


def build_wait_table(
    conn: sqlite3.Connection,
    park_id: str = config.PARK_ID,
    *,
    assumptions: config.Assumptions | None = None,
    names: Mapping[str, str] | None = None,
) -> WaitTable:
    """Build the estimate/provenance tables with the full fallback chain.

    The chain, worst case last:

    | tier | key | gate |
    |---|---|---|
    | `cell` | (attraction, weekday, hour) | >= min_cell_dates |
    | `weekday_class` | (attraction, weekend?, hour) | >= min_cell_dates |
    | `hour` | (attraction, hour) | >= min_cell_dates |
    | `shape` | attraction level x park hour profile | profile hour supported |
    | `attraction` | attraction day-level mean | any data |
    | `park` | park mean | never, in practice |

    The `shape` tier is what keeps thin hours usable: 17 dates of *park-level*
    hour-8 shape is plenty for a ratio even when one ride has 2 dates of its own.
    It is the same multiplicative logic as the existing Crowd Index (current wait
    divided by an hour-of-day baseline), so it is idiomatic here rather than a new
    invention. The profile is computed per weekday-class because the hour-of-day
    profile above 18:00 is conditioned on the park being open then, which
    correlates with weekday — pooling all weekdays at hour 20 would silently pool
    only the long-day weekdays.
    """
    assumptions = assumptions or config.Assumptions()
    cells = load_cells(conn, park_id)
    if cells.empty:
        raise RuntimeError("no wait cells survived screening")

    ids = tuple(sorted(cells["attraction_id"].unique()))
    lookup = {aid: i for i, aid in enumerate(ids)}
    n = len(ids)

    minutes = np.full((n, 7, 24), np.nan)
    tier = np.full((n, 7, 24), "", dtype="<U13")
    n_dates = np.zeros((n, 7, 24), dtype=np.int32)

    gate = assumptions.min_cell_dates

    # tier 1: (attraction, weekday, hour)
    by_cell = cells.groupby(["attraction_id", "weekday", "hour"]).agg(
        mean_wait=("mean_wait", "mean"), dates=("date", "nunique")
    )
    # tier 2: (attraction, weekend?, hour)
    by_class = cells.groupby(["attraction_id", "weekend", "hour"]).agg(
        mean_wait=("mean_wait", "mean"), dates=("date", "nunique")
    )
    # tier 3: (attraction, hour)
    by_hour = cells.groupby(["attraction_id", "hour"]).agg(
        mean_wait=("mean_wait", "mean"), dates=("date", "nunique")
    )
    # tier 5: attraction level
    by_attraction = cells.groupby("attraction_id")["mean_wait"].mean()
    # tier 6: park level
    park_mean = float(cells["mean_wait"].mean())

    # tier 4 ingredient: park hour profile per weekday-class, normalised to 1.0
    profile: dict[tuple[bool, int], float] = {}
    for is_weekend, group in cells.groupby("weekend"):
        hourly = group.groupby("hour")["mean_wait"].mean()
        scale = float(hourly.mean())
        if scale <= 0:
            continue
        for hour, value in hourly.items():
            profile[(bool(is_weekend), int(hour))] = float(value) / scale

    for aid in ids:
        i = lookup[aid]
        level = float(by_attraction.get(aid, park_mean))
        for weekday in range(7):
            is_weekend = weekday in config.WEEKEND
            for hour in config.MODEL_HOURS:
                chosen: tuple[float, Tier, int] | None = None

                row = by_cell.loc[(aid, weekday, hour)] if (aid, weekday, hour) in by_cell.index else None
                if row is not None and int(row["dates"]) >= gate:
                    chosen = (float(row["mean_wait"]), "cell", int(row["dates"]))

                if chosen is None and (aid, is_weekend, hour) in by_class.index:
                    row = by_class.loc[(aid, is_weekend, hour)]
                    if int(row["dates"]) >= gate:
                        chosen = (
                            float(row["mean_wait"]),
                            "weekday_class",
                            int(row["dates"]),
                        )

                if chosen is None and (aid, hour) in by_hour.index:
                    row = by_hour.loc[(aid, hour)]
                    if int(row["dates"]) >= gate:
                        chosen = (float(row["mean_wait"]), "hour", int(row["dates"]))

                if chosen is None:
                    ratio = profile.get((is_weekend, hour))
                    if ratio is None:
                        # Edge hours (7, and 23 on midnight-close days) have no
                        # park-wide profile of their own. Clamping to the nearest
                        # profiled hour keeps a shape-based estimate rather than
                        # dropping two tiers to a flat day mean, which would
                        # price 23:00 like 13:00.
                        candidates = [h for (we, h) in profile if we == is_weekend]
                        if candidates:
                            nearest = min(candidates, key=lambda h: abs(h - hour))
                            ratio = profile[(is_weekend, nearest)]
                    if ratio is not None:
                        chosen = (level * ratio, "shape", 0)

                if chosen is None and aid in by_attraction.index:
                    chosen = (level, "attraction", 0)

                if chosen is None:
                    chosen = (park_mean, "park", 0)

                minutes[i, weekday, hour] = max(0.0, chosen[0])
                tier[i, weekday, hour] = chosen[1]
                n_dates[i, weekday, hour] = chosen[2]

    observed_max = np.array(
        [
            float(cells.loc[cells["attraction_id"] == aid, "mean_wait"].max())
            for aid in ids
        ]
    )

    return WaitTable(
        attraction_ids=ids,
        names=dict(names or {}),
        minutes=minutes,
        tier=tier,
        n_dates=n_dates,
        observed_max=observed_max,
        interpolation=assumptions.interpolation,
    )


def coverage_table(table: WaitTable) -> pd.DataFrame:
    """Per-attraction tier breakdown over MODEL_HOURS, for the article."""
    window = list(config.MODEL_HOURS)
    rows = []
    for i, aid in enumerate(table.attraction_ids):
        block = table.tier[i, :, window].ravel()
        record: dict[str, object] = {
            "attraction_id": aid,
            "name": table.names.get(aid, aid),
            "cells": int(block.size),
        }
        for tier_name in TIER_ORDER:
            record[tier_name] = int((block == tier_name).sum())
        record["median_dates"] = float(
            np.median(table.n_dates[i, :, window][table.n_dates[i, :, window] > 0])
            if (table.n_dates[i, :, window] > 0).any()
            else 0
        )
        rows.append(record)
    return pd.DataFrame(rows)


def hourly_frame(table: WaitTable, weekday: int) -> pd.DataFrame:
    """Wide hour-by-attraction frame of estimates for one weekday."""
    window = list(config.MODEL_HOURS)
    return pd.DataFrame(
        {
            table.names.get(aid, aid): [
                table.minutes[i, weekday, h] for h in window
            ]
            for i, aid in enumerate(table.attraction_ids)
        },
        index=window,
    ).T
