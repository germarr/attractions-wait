"""The three things the model has to beat, implemented before the model exists.

Written first on purpose. A baseline chosen after seeing the model's score is not
a baseline, it is a decoration, and the whole value of this project rests on the
comparison being decided in advance.

B0 climatology   — per-park average level. Knows nothing about the date.
B1 weekday x hour — the incumbent: what `research_project.waits` already uses,
                    generalized to seven parks. Offered in two forms, all-history
                    and a 28-day rolling window, because measurement showed the
                    rolling form is dramatically stronger (holdout MAE 6.197 vs
                    7.094) and the weaker one would be a straw man.
B2 persistence   — carry the trailing-7 level forward unchanged. Hard to beat at
                    one day out, actively harmful at a month.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from forecast import config
from forecast.profile import Profile


def compose(
    profile: Profile,
    levels: Mapping[tuple[str, str], float],
    rows: pd.DataFrame,
    *,
    smearing: float = 1.0,
) -> np.ndarray:
    """shape + level -> minutes, with the log back-transform corrected.

    `rows` needs park_id, date, attraction_id, hour. Any (park, date) absent from
    `levels` yields NaN rather than a quiet zero, so a missing prediction is
    visible instead of looking like a walk-on ride. Vectorized through index
    mapping because the backtest composes this a few million times.
    """
    shape = pd.MultiIndex.from_arrays(
        [rows["attraction_id"], rows["hour"]]
    ).map(profile.offset)
    level = pd.MultiIndex.from_arrays(
        [rows["park_id"], rows["date"]]
    ).map(levels)
    shape = pd.to_numeric(pd.Series(shape), errors="coerce").to_numpy(dtype=float)
    level = pd.to_numeric(pd.Series(level), errors="coerce").to_numpy(dtype=float)
    minutes = np.expm1(level + shape) * smearing
    # A walk-through attraction posts a wait of zero, so log1p(0) = 0 and a slightly
    # negative (level + shape) turns into a fraction of a negative minute. Clamped,
    # because a negative queue is not a modelling nuance, it is a wrong number on a
    # page. NaN is preserved — it means "no estimate", which is different from zero.
    return np.where(np.isnan(minutes), minutes, np.maximum(0.0, minutes))


@dataclass(frozen=True)
class LevelBaseline:
    """A baseline expressed purely as a choice of day level per (park, date)."""

    name: str
    levels: Mapping[tuple[str, str], float]

    def predict(self, profile: Profile, rows: pd.DataFrame, *, smearing: float = 1.0):
        return compose(profile, self.levels, rows, smearing=smearing)


def climatology(levels: pd.DataFrame, *, up_to_date: str, targets: pd.DataFrame) -> LevelBaseline:
    """B0 — the park's mean level over the training window, for every date."""
    past = levels[levels["date"] <= up_to_date]
    by_park = past.groupby("park_id")["level"].mean().to_dict()
    overall = float(past["level"].mean())
    table = {
        (row.park_id, row.date): by_park.get(row.park_id, overall)
        for row in targets.itertuples()
    }
    return LevelBaseline("climatology", table)


def persistence(
    levels: pd.DataFrame, *, up_to_date: str, targets: pd.DataFrame, window: int = 7
) -> LevelBaseline:
    """B2 — the last `window` days' mean level, carried forward unchanged."""
    cutoff = (pd.Timestamp(up_to_date) - pd.Timedelta(days=window - 1)).date().isoformat()
    past = levels[(levels["date"] <= up_to_date) & (levels["date"] >= cutoff)]
    by_park = past.groupby("park_id")["level"].mean().to_dict()
    overall = float(past["level"].mean()) if len(past) else float(levels["level"].mean())
    table = {
        (row.park_id, row.date): by_park.get(row.park_id, overall)
        for row in targets.itertuples()
    }
    return LevelBaseline(f"persistence_{window}", table)


@dataclass
class WeekdayHourCells:
    """B1 — the incumbent estimator, predicting minutes directly.

    Not expressible as a day level: it is a per-(attraction, weekday, hour) mean,
    so its shape and its level move together. That is exactly the property the
    two-stage model is testing itself against, which is why this one keeps its own
    prediction path rather than being folded into `LevelBaseline`.
    """

    name: str
    cell: Mapping[tuple[str, int, int], float]
    attraction_hour: Mapping[tuple[str, int], float]
    attraction: Mapping[str, float]
    park: Mapping[str, float]
    overall: float

    @classmethod
    def fit(
        cls,
        panel: pd.DataFrame,
        *,
        up_to_date: str,
        window_days: int | None = None,
        name: str | None = None,
    ) -> "WeekdayHourCells":
        train = panel[panel["date"] <= up_to_date]
        if window_days is not None:
            cutoff = (
                pd.Timestamp(up_to_date) - pd.Timedelta(days=window_days - 1)
            ).date().isoformat()
            train = train[train["date"] >= cutoff]
        if train.empty:
            raise ValueError(f"no training rows for {name or 'weekday_hour'} at {up_to_date}")
        return cls(
            name=name or (f"weekday_hour_{window_days}d" if window_days else "weekday_hour_all"),
            cell=train.groupby(["attraction_id", "weekday", "hour"])["mean_wait"].mean().to_dict(),
            attraction_hour=train.groupby(["attraction_id", "hour"])["mean_wait"].mean().to_dict(),
            attraction=train.groupby("attraction_id")["mean_wait"].mean().to_dict(),
            park=train.groupby("park_id")["mean_wait"].mean().to_dict(),
            overall=float(train["mean_wait"].mean()),
        )

    def predict(self, rows: pd.DataFrame) -> np.ndarray:
        """Most specific cell available, falling back a tier at a time."""
        def look(keys, table):
            return pd.to_numeric(
                pd.Series(keys.map(table)), errors="coerce"
            ).to_numpy(dtype=float, copy=True)

        out = look(
            pd.MultiIndex.from_arrays(
                [rows["attraction_id"], rows["weekday"], rows["hour"]]
            ),
            self.cell,
        )
        for keys, table in (
            (pd.MultiIndex.from_arrays([rows["attraction_id"], rows["hour"]]),
             self.attraction_hour),
            (pd.Index(rows["attraction_id"]), self.attraction),
            (pd.Index(rows["park_id"]), self.park),
        ):
            gaps = np.isnan(out)
            if not gaps.any():
                return out
            out[gaps] = look(keys, table)[gaps]
        return np.where(np.isnan(out), self.overall, out)
