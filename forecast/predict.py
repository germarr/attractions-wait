"""The serving path: a date in, per-park hourly wait curves out.

Numpy and pandas only. scikit-learn is never imported here, and the only thing
read from the database is the published schedule for the requested date, because
the trained artifact already carries the shape, the coefficients, the trailing
state as of the origin and the incumbent baseline.

Two honesty rules are enforced in code rather than left to the page:

* A date with no published park hours is REFUSED, not imputed. The scheduled
  operating window is the model's strongest feature, so inventing it would mean
  returning a confident number resting on a guess.
* Every payload carries the origin, the horizon, the measured skill at that
  horizon, and the incumbent's prediction beside the model's. A caller cannot
  render the model's number without the context that says how much to trust it.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

from forecast import artifacts, config, data
from forecast.model import Forecaster
from research_project import db as rdb


class HorizonError(ValueError):
    """Raised when a date lies beyond the published schedule."""


def _rows_for(forecaster: Forecaster, calendar: pd.DataFrame,
              parks: Iterable[str]) -> pd.DataFrame:
    """Every (park, date, attraction, hour) the request covers.

    Hours come from the published window, so a park that closes at 18:00 is never
    given a 21:00 prediction. `hour` is the hour the guest would be in the queue,
    so the last modelled hour is the one that starts before closing.
    """
    by_park: dict[str, list[str]] = {}
    for attraction_id, park_id in forecaster.profile.park_of.items():
        by_park.setdefault(park_id, []).append(attraction_id)

    out = []
    for row in calendar.itertuples():
        if row.park_id not in parks:
            continue
        first = int(np.floor(row.open_minute / 60))
        last = int(np.ceil(row.close_minute / 60)) - 1
        hours = [h for h in range(first, last + 1) if h in config.MODEL_HOURS]
        for attraction_id in sorted(by_park.get(row.park_id, [])):
            for hour in hours:
                out.append(
                    {
                        "park_id": row.park_id,
                        "date": row.date,
                        "attraction_id": attraction_id,
                        "hour": hour,
                        "weekday": row.weekday,
                    }
                )
    return pd.DataFrame(out)


def _skill_at(forecaster: Forecaster, horizon_band: str) -> dict:
    """The measured skill for this horizon, as recorded at training time."""
    table = forecaster.manifest.get("skill") or {}
    entry = table.get(horizon_band)
    if not entry:
        return {"available": False, "horizon_band": horizon_band}
    return {"available": True, "horizon_band": horizon_band, **entry}


def forecast_day(
    date: str,
    *,
    parks: Iterable[str] | None = None,
    forecaster: Forecaster | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Predicted hourly waits for every attraction in each park on `date`."""
    model = forecaster or artifacts.load()
    park_ids = tuple(parks or config.PARK_ORDER)

    owned = conn is None
    connection = conn or rdb.connect()
    try:
        calendar = data.calendar(connection, start=date, end=date, parks=park_ids)
        ceiling = data.forecastable_dates(connection, origin=model.origin)
    finally:
        if owned:
            connection.close()

    if calendar.empty:
        limit = ceiling[-1] if ceiling else "unknown"
        raise HorizonError(
            f"no published park hours for {date}; the schedule feed reaches "
            f"{limit}, and park hours are the model's strongest input, so this "
            "date is refused rather than answered on an invented schedule"
        )

    horizon = data.horizon_of(model.origin, date)
    band = data.horizon_band(horizon)
    levels = model.predict_levels(calendar, model.state)
    rows = _rows_for(model, calendar, park_ids)
    predicted = model.predict_rows(rows, levels)
    predicted["baseline"] = model.baseline_waits(predicted)

    parks_out = []
    for park_id in park_ids:
        part = predicted[predicted["park_id"] == park_id]
        if part.empty:
            continue
        day = calendar[calendar["park_id"] == park_id].iloc[0]
        attractions = []
        for attraction_id, group in part.groupby("attraction_id"):
            group = group.sort_values("hour")
            waits = group["wait"].to_numpy(dtype=float)
            if not np.isfinite(waits).any():
                # A ride that opened after the origin has no shape to stand on.
                # Reported as having no estimate rather than given a number.
                attractions.append(
                    {
                        "attraction_id": attraction_id,
                        "name": model.profile.names.get(attraction_id, attraction_id),
                        "estimate": False,
                        "tier": "none",
                    }
                )
                continue
            peak = int(group["hour"].to_numpy()[int(np.nanargmax(waits))])
            attractions.append(
                {
                    "attraction_id": attraction_id,
                    "name": model.profile.names.get(attraction_id, attraction_id),
                    "estimate": True,
                    "day_mean": round(float(np.nanmean(waits)), 1),
                    "day_lo": round(float(np.nanmean(group["lo"])), 1),
                    "day_hi": round(float(np.nanmean(group["hi"])), 1),
                    "peak_hour": peak,
                    "peak_wait": round(float(np.nanmax(waits)), 1),
                    "band": data.wait_band(float(np.nanmean(waits))),
                    "tier": group["tier"].iloc[0],
                    "baseline": (
                        round(float(np.nanmean(group["baseline"])), 1)
                        if np.isfinite(group["baseline"]).any() else None
                    ),
                    "hourly": [
                        {
                            "hour": int(r.hour),
                            "wait": round(float(r.wait), 1),
                            "lo": round(float(r.lo), 1),
                            "hi": round(float(r.hi), 1),
                        }
                        for r in group.itertuples()
                        if np.isfinite(r.wait)
                    ],
                }
            )
        attractions.sort(key=lambda a: -(a.get("day_mean") or -1))
        estimated = [a for a in attractions if a["estimate"]]
        parks_out.append(
            {
                "park_id": park_id,
                "park": config.PARKS.get(park_id, park_id),
                "hours": round(float(day.hours), 2),
                "open": _clock(day.open_minute),
                "close": _clock(day.close_minute),
                "party_night": bool(day.party_night),
                "day_mean": round(float(np.mean([a["day_mean"] for a in estimated])), 1)
                if estimated else None,
                "baseline_day_mean": round(
                    float(np.nanmean([a["baseline"] for a in estimated
                                      if a["baseline"] is not None])), 1
                ) if estimated else None,
                "level": round(float(levels[(park_id, date)]), 4),
                "n_attractions": len(estimated),
                "n_no_estimate": len(attractions) - len(estimated),
                "attractions": attractions,
            }
        )

    return {
        "date": date,
        "weekday": _weekday_name(calendar["weekday"].iloc[0]),
        "origin": model.origin,
        "horizon": horizon,
        "horizon_band": band,
        "skill": _skill_at(model, band),
        "smearing": round(model.smearing, 4),
        "interval_level": model.manifest["assumptions"].get("interval_level", 0.8),
        "trained_on": {
            "first_date": model.manifest["train_first_date"],
            "last_date": model.manifest["train_last_date"],
            "park_days": model.manifest["n_park_days"],
            "attractions": model.manifest["n_attractions"],
        },
        "parks": parks_out,
        "disclosure": (
            f"Trained on {model.manifest['n_park_days']} park-days from "
            f"{model.manifest['train_first_date']} to {model.origin}. The model's "
            f"information ends {model.origin}, so this is a {horizon}-day-ahead "
            "forecast. Intervals are the model's own measured historical error at "
            "this horizon, not a probability statement about the future."
        ),
    }


def _clock(minutes: float) -> str:
    total = int(round(minutes))
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday")


def _weekday_name(index: int) -> str:
    return _WEEKDAYS[int(index) % 7]


def available_dates(conn: sqlite3.Connection | None = None,
                    *, forecaster: Forecaster | None = None,
                    not_before: str | None = None) -> dict:
    """The dates the picker may offer, and why it stops where it does."""
    model = forecaster or artifacts.load()
    owned = conn is None
    connection = conn or rdb.connect()
    try:
        dates = data.forecastable_dates(connection, origin=model.origin,
                                        not_before=not_before)
        calendar = data.calendar(connection, start=dates[0], end=dates[-1]) \
            if dates else pd.DataFrame()
    finally:
        if owned:
            connection.close()

    out = []
    for date in dates:
        part = calendar[calendar["date"] == date]
        out.append(
            {
                "date": date,
                "weekday": _weekday_name(part["weekday"].iloc[0]),
                "horizon": data.horizon_of(model.origin, date),
                "parks": int(len(part)),
            }
        )
    return {
        "origin": model.origin,
        "max_date": dates[-1] if dates else None,
        "n": len(out),
        "dates": out,
        "limit_reason": (
            "The schedule feed publishes park hours about a month ahead. Beyond "
            f"{dates[-1] if dates else 'the last published date'} there are no real "
            "opening and closing times, and those are the model's strongest input."
        ),
    }
