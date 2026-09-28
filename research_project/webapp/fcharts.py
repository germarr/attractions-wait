"""Chart datasets for the forecasting article.

Same contract as `charts.py`: every series is computed from the live tables or
from a committed backtest CSV, so a number in a figure and the same number in a
sentence come from one calculation and cannot drift apart. Nothing here refits a
model — the backtest writes CSVs, and this module reads them.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from forecast import artifacts, config as fconfig, data as fdata, profile as fprofile
from research_project import db as rdb

RESULTS = Path(__file__).resolve().parents[2] / "forecast" / "results"

# One ride per panel of the decomposition figure: a headliner with a pronounced
# intraday shape reads far better than a walk-on whose curve is nearly flat.
DECOMP_RIDE = "Seven Dwarfs Mine Train"


@lru_cache(maxsize=1)
def _panel():
    conn = rdb.connect()
    try:
        panel = fdata.load_panel(conn)
        levels = fdata.park_day_levels(panel)
        calendar = fdata.calendar(
            conn, start=fconfig.HISTORY_START, end=max(panel["date"])
        )
    finally:
        conn.close()
    return panel, levels, calendar


def _csv(name: str) -> pd.DataFrame:
    path = RESULTS / name
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing — run `python -m forecast.cli backtest` first"
        )
    return pd.read_csv(path)


def _eta_squared(frame: pd.DataFrame, keys: list[str], value: str) -> float:
    """Share of variance in `value` explained by the grouping `keys`."""
    grand = frame[value].mean()
    total = float(((frame[value] - grand) ** 2).sum())
    if total <= 0:
        return float("nan")
    grouped = frame.groupby(keys)[value].agg(["mean", "size"])
    between = float((grouped["size"] * (grouped["mean"] - grand) ** 2).sum())
    return between / total


def variance_ladder() -> dict:
    """How much of an hourly wait a plain lookup table already explains."""
    panel, levels, _ = _panel()
    core = panel[panel["hour"].isin(fconfig.CORE_HOURS)].copy()
    steps = [
        {"label": "Park only", "keys": ["park_id"]},
        {"label": "Attraction", "keys": ["attraction_id"]},
        {"label": "Attraction x hour", "keys": ["attraction_id", "hour"]},
        {"label": "+ weekday", "keys": ["attraction_id", "hour", "weekday"]},
    ]
    for step in steps:
        step["eta"] = round(_eta_squared(core, step["keys"], "log_wait"), 4)
        step.pop("keys")

    # What the day explains of what is LEFT after attraction x hour — the slice the
    # forecaster is actually competing for.
    shape = core.groupby(["attraction_id", "hour"])["log_wait"].transform("mean")
    residual = core.assign(resid=core["log_wait"] - shape)
    day_share = _eta_squared(residual, ["park_id", "date"], "resid")
    return {
        "steps": steps,
        "day_share_of_residual": round(day_share, 4),
        "n_rows": int(len(core)),
        "n_park_days": int(len(levels)),
        "hours": [min(fconfig.CORE_HOURS), max(fconfig.CORE_HOURS)],
    }


def season() -> dict:
    """Park-day mean wait across the whole history, per park and overall."""
    _, levels, _ = _panel()
    dates = sorted(levels["date"].unique())
    series = []
    for park_id, name in fconfig.PARKS.items():
        part = levels[levels["park_id"] == park_id].set_index("date")["mean_wait"]
        series.append(
            {
                "park": name,
                "values": [
                    round(float(part[d]), 2) if d in part.index else None
                    for d in dates
                ],
            }
        )
    overall = levels.groupby("date")["mean_wait"].mean()
    weekday = levels.groupby("weekday")["mean_wait"].mean()
    trough = overall.idxmin()
    peak = overall.idxmax()
    return {
        "dates": dates,
        "series": series,
        "overall": [round(float(overall[d]), 2) for d in dates],
        "weekday_means": [round(float(weekday[i]), 2) for i in range(7)],
        "weekday_eta": round(_eta_squared(levels, ["weekday"], "level"), 4),
        "trough": {"date": trough, "value": round(float(overall[trough]), 2)},
        "peak": {"date": peak, "value": round(float(overall[peak]), 2)},
        "span": round(float(overall.max() - overall.min()), 2),
        "weekday_span": round(float(weekday.max() - weekday.min()), 2),
    }


def horizon_decay() -> dict:
    """Does last week's crowd level tell you anything about a date weeks out?"""
    frame = _csv("horizon_decay.csv")
    crossing = None
    values = frame["corr"].tolist()
    horizons = frame["horizon"].tolist()
    for i in range(1, len(values)):
        if values[i - 1] > 0 >= values[i]:
            crossing = horizons[i]
            break
    return {
        "horizons": horizons,
        "corr": [round(float(v), 4) for v in values],
        "n": [int(v) for v in frame["n"]],
        "crossing": crossing,
        "at_1": round(float(values[0]), 3),
        "at_end": round(float(values[-1]), 3),
    }


def hours_signal() -> dict:
    """Scheduled operating hours against how busy the day turned out."""
    _, levels, calendar = _panel()
    joined = levels.merge(calendar, on=["park_id", "date", "weekday"])
    points, correlations = [], []
    for park_id, name in fconfig.PARKS.items():
        part = joined[joined["park_id"] == park_id]
        if len(part) < 5:
            continue
        correlations.append(
            {
                "park": name,
                "corr": round(float(np.corrcoef(part["hours"], part["mean_wait"])[0, 1]), 3),
                "n": int(len(part)),
            }
        )
        for row in part.itertuples():
            points.append(
                {
                    "park": name,
                    "hours": round(float(row.hours), 2),
                    "wait": round(float(row.mean_wait), 2),
                    "date": row.date,
                }
            )
    correlations.sort(key=lambda r: -r["corr"])
    strongest = correlations[0]["park"]
    focus = [p for p in points if p["park"] == strongest]
    slope, intercept = np.polyfit([p["hours"] for p in focus],
                                  [p["wait"] for p in focus], 1)
    return {
        "points": points,
        "correlations": correlations,
        "focus": strongest,
        "fit": {"slope": round(float(slope), 3), "intercept": round(float(intercept), 3)},
        "overall_corr": round(float(np.corrcoef(joined["hours"], joined["mean_wait"])[0, 1]), 3),
    }


def decomposition() -> dict:
    """The model's mechanism: one shape, two day levels, two resulting curves."""
    panel, levels, _ = _panel()
    origin = max(panel["date"])
    shape = fprofile.build_profile(panel, levels, up_to_date=origin)
    smearing = fprofile.smearing_factor(panel, levels, shape, up_to_date=origin)

    match = [a for a, n in shape.names.items() if n == DECOMP_RIDE]
    if not match:
        match = [max(shape.names, key=lambda a: shape.at(a, 13))]
    ride = match[0]
    park_id = shape.park_of[ride]
    hours = [h for h in shape.hours_for(ride) if h in fconfig.CORE_HOURS]

    park_levels = levels[levels["park_id"] == park_id].sort_values("level")
    quiet = park_levels.iloc[0]
    busy = park_levels.iloc[-1]

    def curve(level: float) -> list[float]:
        return [round(float(np.expm1(level + shape.at(ride, h)) * smearing), 1)
                for h in hours]

    observed = {}
    for label, day in (("quiet", quiet), ("busy", busy)):
        rows = panel[(panel["attraction_id"] == ride) & (panel["date"] == day.date)]
        seen = dict(zip(rows["hour"], rows["mean_wait"]))
        observed[label] = [
            round(float(seen[h]), 1) if h in seen else None for h in hours
        ]

    return {
        "ride": shape.names[ride],
        "park": fconfig.PARKS.get(park_id, park_id),
        "hours": hours,
        "shape": [round(float(shape.at(ride, h)), 4) for h in hours],
        "levels": [
            {"label": "A quiet day", "date": quiet.date,
             "level": round(float(quiet.level), 3), "curve": curve(quiet.level),
             "observed": observed["quiet"]},
            {"label": "A busy day", "date": busy.date,
             "level": round(float(busy.level), 3), "curve": curve(busy.level),
             "observed": observed["busy"]},
        ],
        "smearing": round(float(smearing), 4),
    }


def leakage() -> dict:
    """The same model and features, scored under three ways of splitting."""
    frame = _csv("leakage.csv")
    order = {"random_row": 0, "random_day": 1, "rolling_origin": 2}
    frame = frame.sort_values("protocol", key=lambda s: s.map(order))
    return {
        "rows": [
            {
                "protocol": row.protocol,
                "label": {"random_row": "Rows split at random",
                          "random_day": "Days split at random",
                          "rolling_origin": "Trained only on the past"}[row.protocol],
                "r2": round(float(row.level_r2), 4),
                "mae": round(float(row.level_mae), 4),
                "inflation": round(float(row.level_r2_inflation), 3),
                "n": int(row.n),
                "honest": row.protocol == "rolling_origin",
                "note": row.note,
            }
            for row in frame.itertuples()
        ]
    }


def skill_by_horizon() -> dict:
    """The headline result, with its bootstrap interval and its n."""
    frame = _csv("skill.csv")
    frame = frame[frame["predictor"] == "ridge"]
    band_order = ["1-7", "8-14", "15-29"]
    series = []
    for baseline in ("weekday_hour_28d", "climatology", "persistence_7"):
        part = frame[frame["baseline"] == baseline].set_index("horizon_band")
        series.append(
            {
                "baseline": {
                    "weekday_hour_28d": "vs weekday x hour (28-day)",
                    "climatology": "vs park average",
                    "persistence_7": "vs last week carried forward",
                }[baseline],
                "key": baseline,
                "values": [
                    {
                        "band": band,
                        "skill": round(float(part.loc[band, "skill"]), 4),
                        "lo": round(float(part.loc[band, "lo"]), 4),
                        "hi": round(float(part.loc[band, "hi"]), 4),
                        "n_rows": int(part.loc[band, "n_rows"]),
                        "n_origins": int(part.loc[band, "n_origins"]),
                        "spans_zero": bool(part.loc[band, "spans_zero"]),
                    }
                    for band in band_order if band in part.index
                ],
            }
        )
    errors = _csv("error_by_horizon.csv")
    return {
        "bands": band_order,
        "series": series,
        "mae": [
            {
                "band": row.horizon_band,
                "predictor": row.predictor,
                "mae": round(float(row.mae), 3),
            }
            for row in errors.itertuples()
        ],
    }


def calibration_bands() -> dict:
    """Predicted against actual per wait band, plus out-of-sample coverage."""
    frame = _csv("calibration.csv")
    cover = _csv("coverage_honest.csv")
    bands = ["walk-on", "middle", "headliner"]
    series = []
    for band in bands:
        part = frame[frame["wait_band"] == band].sort_values("predicted")
        series.append(
            {
                "band": band,
                "points": [
                    {"predicted": round(float(r.predicted), 2),
                     "actual": round(float(r.actual), 2),
                     "n": int(r.n)}
                    for r in part.itertuples()
                ],
            }
        )
    errors = _csv("error_by_band.csv")
    errors = errors[errors["predictor"] == "ridge"]
    return {
        "series": series,
        "coverage": [
            {
                "band": row.wait_band,
                "horizon_band": row.horizon_band,
                "covered": round(float(row.covered), 4),
                "n": int(row.n),
            }
            for row in cover.itertuples()
        ],
        "nominal": float(cover["nominal"].iloc[0]) if len(cover) else 0.8,
        "band_error": [
            {"band": row.wait_band, "mae": round(float(row.mae), 3),
             "bias": round(float(row.bias), 3), "n": int(row.n)}
            for row in errors.itertuples()
        ],
        "tested_on_origins": int(cover["tested_on_origins"].iloc[0]) if len(cover) else 0,
    }


def model_card() -> dict:
    """What is actually deployed — read from the trained artifact, not asserted."""
    forecaster = artifacts.load()
    manifest = forecaster.manifest
    return {
        "origin": manifest["origin"],
        "feature_set": manifest["feature_set"],
        "features": manifest["features"],
        "coefficients": [
            {"name": name, "value": round(float(value), 4)}
            for name, value in forecaster.day_model.standardized()
        ],
        "alpha": manifest["ridge_alpha"],
        "tau": manifest["shrink_tau_days"],
        "smearing": round(float(manifest["smearing"]), 4),
        "train_pairs": manifest["train_pairs"],
        "n_park_days": manifest["n_park_days"],
        "n_attractions": manifest["n_attractions"],
        "first_date": manifest["train_first_date"],
        "profile_support": {k: round(v, 4) for k, v in manifest["profile_support"].items()},
        "library_versions": manifest["library_versions"],
    }


def everything() -> dict:
    return {
        "variance_ladder": variance_ladder(),
        "season": season(),
        "horizon_decay": horizon_decay(),
        "hours_signal": hours_signal(),
        "decomposition": decomposition(),
        "leakage": leakage(),
        "skill_by_horizon": skill_by_horizon(),
        "calibration_bands": calibration_bands(),
    }
