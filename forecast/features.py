"""The design matrix, and the rule that decides what is allowed into it.

`KNOWABLE` is not documentation. It is a gate: a feature may only be shipped if
it could have been computed before the target date arrived, and `build_features`
asserts that at import time. The three tempting failures are listed explicitly as
False rather than simply left out, because "we didn't think of it" and "we
considered it and it is inadmissible" are different states and only one of them
survives a reader asking why.

Everything trailing is computed strictly from dates <= origin. The behavioural
test for this is worth more than the assertion: corrupt every observation after
the origin, rebuild, and require the matrix to come back bit-identical.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from forecast import config, data

# ── what may and may not be known ─────────────────────────────────────────

KNOWABLE: Mapping[str, bool] = {
    # published schedule — the operator commits to these weeks ahead
    "hours": True,
    "open_minute": True,
    "party_night": True,
    # calendar arithmetic
    "is_weekend": True,
    "weekday_sin": True,
    "weekday_cos": True,
    "horizon": True,
    # state as of the origin, never as of the target
    "trailing_7": True,
    "trailing_28": True,
    "trailing_7_all_parks": True,
    "persistence_shrunk": True,
    "park_climatology": True,
    # identity
    **{f"park_{i}": True for i in range(len(config.PARK_ORDER))},
    # ── inadmissible, and why ────────────────────────────────────────────
    "weather": False,          # 35 days of history, zero forecast rows, pruned at 35
    "n_down": False,           # breakdowns are unknowable for a future date
    "same_day_wait": False,    # it IS the target
    "day_index": False,        # a linear trend would extrapolate one regime change
}

SHIPPED: tuple[str, ...] = (
    *(f"park_{i}" for i in range(len(config.PARK_ORDER))),
    "hours",
    "open_minute",
    "party_night",
    "is_weekend",
    "weekday_sin",
    "weekday_cos",
    "horizon",
    "trailing_7",
    "trailing_28",
    "trailing_7_all_parks",
    "persistence_shrunk",
    "park_climatology",
)

_unknowable = [name for name in SHIPPED if not KNOWABLE.get(name, False)]
assert not _unknowable, f"inadmissible features in SHIPPED: {_unknowable}"

# Candidate feature sets, chosen between by an inner temporal split rather than by
# taste. `full` carries the raw trailing levels; `shrunk` keeps only the
# horizon-decayed persistence term, on the theory that the raw levels are what
# mislead a model at long horizon, where their correlation with the target has
# already gone negative; `minimal` is the smallest thing that could work.
_PARKS = tuple(f"park_{i}" for i in range(len(config.PARK_ORDER)))
FEATURE_SETS: Mapping[str, tuple[str, ...]] = {
    "full": SHIPPED,
    "shrunk": (*_PARKS, "hours", "open_minute", "party_night", "is_weekend",
               "weekday_sin", "weekday_cos", "horizon", "persistence_shrunk",
               "park_climatology"),
    "minimal": (*_PARKS, "hours", "horizon", "persistence_shrunk",
                "park_climatology"),
}
for _name, _cols in FEATURE_SETS.items():
    _bad = [c for c in _cols if c not in SHIPPED]
    assert not _bad, f"feature set {_name} names unknown columns: {_bad}"


def columns_for(feature_set: str) -> tuple[int, ...]:
    """Positions of a named feature set inside the full design matrix."""
    wanted = FEATURE_SETS[feature_set]
    return tuple(SHIPPED.index(name) for name in wanted)


# ── trailing state, as of one origin ──────────────────────────────────────


def origin_state(
    levels: pd.DataFrame,
    *,
    origin: str,
    windows: Sequence[int] = config.DEFAULTS.trailing_windows,
) -> dict:
    """Everything the model may remember at `origin`, and nothing after it."""
    past = levels[levels["date"] <= origin]
    if past.empty:
        raise ValueError(f"no observed levels at or before {origin}")

    state: dict = {"origin": origin, "trailing": {}, "climatology": {}}
    for window in windows:
        cutoff = (
            pd.Timestamp(origin) - pd.Timedelta(days=window - 1)
        ).date().isoformat()
        recent = past[past["date"] >= cutoff]
        state["trailing"][window] = (
            recent.groupby("park_id")["level"].mean().to_dict()
        )
        state[f"all_parks_{window}"] = float(recent["level"].mean())
    state["climatology"] = past.groupby("park_id")["level"].mean().to_dict()
    state["climatology_all"] = float(past["level"].mean())
    return state


def build_features(
    targets: pd.DataFrame,
    state: Mapping,
    *,
    assumptions: config.ForecastAssumptions = config.DEFAULTS,
) -> tuple[np.ndarray, tuple[str, ...], pd.DataFrame]:
    """Design matrix for the (park, date) rows in `targets`.

    `targets` must already carry the published schedule columns — `hours`,
    `open_minute`, `party_night`, `weekday` — so this function never touches the
    database and therefore cannot reach the target date's own observations.
    """
    required = {"park_id", "date", "weekday", "hours", "open_minute", "party_night"}
    missing = required - set(targets.columns)
    if missing:
        raise ValueError(f"targets is missing {sorted(missing)}")

    origin = state["origin"]
    tau = assumptions.shrink_tau_days
    short, long = assumptions.trailing_windows

    rows = []
    for row in targets.itertuples():
        horizon = data.horizon_of(origin, row.date)
        trail_s = state["trailing"][short].get(row.park_id, state["climatology_all"])
        trail_l = state["trailing"][long].get(row.park_id, state["climatology_all"])
        clim = state["climatology"].get(row.park_id, state["climatology_all"])

        # Persistence has a shelf life: measured correlation with the target level
        # runs 0.60 at one day, 0.16 at two weeks and -0.36 at four. Decaying the
        # recent level toward climatology is what stops the model carrying "last
        # week was busy" into a month's time, where it is worse than useless.
        weight = float(np.exp(-horizon / tau))
        shrunk = weight * trail_s + (1.0 - weight) * clim

        onehot = [1.0 if row.park_id == pid else 0.0 for pid in config.PARK_ORDER]
        rows.append(
            [
                *onehot,
                float(row.hours),
                float(row.open_minute),
                1.0 if row.party_night else 0.0,
                1.0 if row.weekday >= 5 else 0.0,
                float(np.sin(2 * np.pi * row.weekday / 7)),
                float(np.cos(2 * np.pi * row.weekday / 7)),
                float(horizon),
                float(trail_s),
                float(trail_l),
                float(state[f"all_parks_{short}"]),
                float(shrunk),
                float(clim),
            ]
        )

    matrix = np.asarray(rows, dtype=float)
    meta = targets[["park_id", "date"]].copy().reset_index(drop=True)
    meta["horizon"] = [data.horizon_of(origin, d) for d in meta["date"]]
    meta["horizon_band"] = [data.horizon_band(h) for h in meta["horizon"]]
    meta["origin"] = origin
    return matrix, SHIPPED, meta


def training_pairs(
    levels: pd.DataFrame,
    calendar: pd.DataFrame,
    *,
    up_to_date: str,
    assumptions: config.ForecastAssumptions = config.DEFAULTS,
    max_horizon: int = config.MAX_HORIZON,
    stride: int = 1,
    min_history_days: int = 21,
) -> tuple[np.ndarray, np.ndarray, pd.DataFrame]:
    """Training examples shaped like the task: one row per (past origin, target).

    This exists because of a bug worth recording. Fitting the day model on
    `levels[date <= origin]` with features built at that same origin gives every
    training row a horizon of zero or less — the range was -77..0 — while every
    prediction is made at horizon +1..+21. Two features then had training ranges
    disjoint from their serving ranges, and `persistence_shrunk`, which carries
    `exp(-horizon / tau)`, reached -429 in training against 2.6..3.4 at serving.
    The fitted model was extrapolating far outside its own data and scored worse
    than predicting the park average.

    The fix is to train on the same shape as the question: walk earlier origins,
    and for each one take the dates that genuinely followed it. Every example then
    has a positive horizon and features computed only from that origin's past. No
    target here is ever later than `up_to_date`, so this stays usable inside the
    rolling-origin backtest.
    """
    dates = [d for d in sorted(levels["date"].unique()) if d <= up_to_date]
    if len(dates) <= min_history_days + 1:
        raise ValueError(f"not enough history before {up_to_date} to build pairs")

    cal_keys = ["park_id", "date", "weekday"]
    blocks: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    metas: list[pd.DataFrame] = []

    for origin in dates[min_history_days:-1:stride]:
        horizon_end = (
            pd.Timestamp(origin) + pd.Timedelta(days=max_horizon)
        ).date().isoformat()
        window = [d for d in dates if origin < d <= horizon_end]
        if not window:
            continue
        targets = levels[levels["date"].isin(window)].merge(calendar, on=cal_keys)
        if targets.empty:
            continue
        state = origin_state(levels, origin=origin,
                            windows=assumptions.trailing_windows)
        X, _, meta = build_features(targets, state, assumptions=assumptions)
        blocks.append(X)
        ys.append(targets["level"].to_numpy(dtype=float))
        metas.append(meta)

    if not blocks:
        raise ValueError(f"no training pairs available at {up_to_date}")
    return (
        np.vstack(blocks),
        np.concatenate(ys),
        pd.concat(metas, ignore_index=True),
    )


def state_to_json(state: Mapping) -> dict:
    """JSON-safe form of an origin state, for storing inside a trained artifact.

    The trailing state is fixed once the origin is fixed, so storing it means the
    serving path never has to reload the 100,000-row panel just to remember how
    busy last week was. JSON turns the integer window keys into strings; the
    reader below turns them back.
    """
    return {
        "origin": state["origin"],
        "trailing": {str(k): dict(v) for k, v in state["trailing"].items()},
        "all_parks": {
            key.removeprefix("all_parks_"): float(value)
            for key, value in state.items()
            if key.startswith("all_parks_")
        },
        "climatology": dict(state["climatology"]),
        "climatology_all": float(state["climatology_all"]),
    }


def state_from_json(blob: Mapping) -> dict:
    state: dict = {
        "origin": blob["origin"],
        "trailing": {int(k): dict(v) for k, v in blob["trailing"].items()},
        "climatology": dict(blob["climatology"]),
        "climatology_all": float(blob["climatology_all"]),
    }
    for window, value in blob["all_parks"].items():
        state[f"all_parks_{window}"] = float(value)
    return state
