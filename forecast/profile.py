"""The attraction x hour shape, centered so it carries no day-level information.

The whole model is `wait = shape + level`. This file is the shape: for each
attraction and hour, how far above or below its park's day level that ride sits,
averaged over the training dates. Because every observation is centered by its
own park-day before averaging, the shape is orthogonal to how busy the days in
the training window happened to be — which is what lets the day model be fitted
separately without the two stages fighting each other.

`up_to_date` is a REQUIRED keyword. A profile built once over all history and
reused inside a rolling-origin backtest is the commonest silent leak in a project
like this, and making the parameter mandatory forces every call site to say which
information it is allowed to see.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from forecast import config

# Fallback order, mirroring `research_project.waits.TIER_ORDER`: use the most
# specific evidence available, and label every estimate with which tier it came
# from so the model's confidence is reportable instead of implied.
TIER_ORDER = ("cell", "attraction_hour", "attraction", "park_hour", "park")


@dataclass(frozen=True)
class Profile:
    """Centered log-wait offsets per (attraction, hour), with a provenance tier."""

    offset: Mapping[tuple[str, int], float]
    tier: Mapping[tuple[str, int], str]
    n_dates: Mapping[tuple[str, int], int]
    park_of: Mapping[str, str]
    names: Mapping[str, str]
    up_to_date: str

    def at(self, attraction_id: str, hour: int) -> float:
        return self.offset.get((attraction_id, hour), 0.0)

    def tier_at(self, attraction_id: str, hour: int) -> str:
        return self.tier.get((attraction_id, hour), "park")

    @property
    def attractions(self) -> tuple[str, ...]:
        return tuple(sorted(self.park_of))

    def hours_for(self, attraction_id: str) -> tuple[int, ...]:
        return tuple(sorted(h for a, h in self.offset if a == attraction_id))

    def support(self) -> dict[str, float]:
        """Share of (attraction, hour) estimates coming from each tier."""
        total = len(self.tier) or 1
        counts: dict[str, int] = {}
        for tier in self.tier.values():
            counts[tier] = counts.get(tier, 0) + 1
        return {t: counts.get(t, 0) / total for t in TIER_ORDER}

    def frame(self) -> pd.DataFrame:
        rows = [
            {
                "attraction_id": aid,
                "park_id": self.park_of[aid],
                "name": self.names.get(aid, aid),
                "hour": hour,
                "offset": value,
                "tier": self.tier[(aid, hour)],
                "n_dates": self.n_dates[(aid, hour)],
            }
            for (aid, hour), value in sorted(self.offset.items())
        ]
        return pd.DataFrame(rows)


def build_profile(
    panel: pd.DataFrame,
    levels: pd.DataFrame,
    *,
    up_to_date: str,
    assumptions: config.ForecastAssumptions = config.DEFAULTS,
) -> Profile:
    """Centered shape from every panel row on or before `up_to_date`."""
    train = panel[panel["date"] <= up_to_date]
    lv = levels[levels["date"] <= up_to_date]
    if train.empty:
        raise ValueError(f"no panel rows at or before {up_to_date}")

    joined = train.merge(lv[["park_id", "date", "level"]], on=["park_id", "date"])
    joined = joined.assign(centered=joined["log_wait"] - joined["level"])

    park_of = dict(zip(joined["attraction_id"], joined["park_id"]))
    names = dict(zip(joined["attraction_id"], joined["name"]))

    cell = joined.groupby(["attraction_id", "hour"])["centered"].agg(["mean", "size"])
    per_attraction = joined.groupby("attraction_id")["centered"].mean()
    park_hour = joined.groupby(["park_id", "hour"])["centered"].mean()
    per_park = joined.groupby("park_id")["centered"].mean()

    offset: dict[tuple[str, int], float] = {}
    tier: dict[tuple[str, int], str] = {}
    n_dates: dict[tuple[str, int], int] = {}

    # Every attraction gets an estimate at every hour its park is ever open, so
    # the serving path never has a hole where the user asked for a real hour.
    hours_by_park = (
        joined.groupby("park_id")["hour"].agg(lambda s: tuple(sorted(set(s)))).to_dict()
    )
    for aid, pid in park_of.items():
        for hour in hours_by_park[pid]:
            key = (aid, hour)
            if key in cell.index:
                mean, size = cell.loc[key, "mean"], int(cell.loc[key, "size"])
            else:
                mean, size = np.nan, 0
            if size >= assumptions.profile_min_dates:
                offset[key], tier[key], n_dates[key] = float(mean), "cell", size
            elif size > 0:
                offset[key], tier[key], n_dates[key] = float(mean), "attraction_hour", size
            elif aid in per_attraction.index:
                offset[key] = float(per_attraction[aid])
                tier[key], n_dates[key] = "attraction", 0
            elif (pid, hour) in park_hour.index:
                offset[key] = float(park_hour[(pid, hour)])
                tier[key], n_dates[key] = "park_hour", 0
            else:
                offset[key] = float(per_park.get(pid, 0.0))
                tier[key], n_dates[key] = "park", 0

    return Profile(
        offset=offset,
        tier=tier,
        n_dates=n_dates,
        park_of=park_of,
        names=names,
        up_to_date=up_to_date,
    )


def smearing_factor(panel: pd.DataFrame, levels: pd.DataFrame, profile: Profile,
                    *, up_to_date: str) -> float:
    """Duan's smearing estimate for the log -> minutes back-transform.

    `expm1(mean of logs)` is a median, not a mean, and understates the average
    wait by roughly `exp(sigma^2 / 2)`. Correcting multiplicatively by the mean
    of `exp(residual)` over training rows is assumption-free and cheap. Without
    it every number the app shows is quietly biased low and nobody notices.
    """
    train = panel[panel["date"] <= up_to_date]
    lv = levels[levels["date"] <= up_to_date]
    joined = train.merge(lv[["park_id", "date", "level"]], on=["park_id", "date"])
    fitted = joined["level"] + [
        profile.at(a, h) for a, h in zip(joined["attraction_id"], joined["hour"])
    ]
    residual = joined["log_wait"] - fitted
    return float(np.mean(np.exp(residual)))
