"""Rolling-origin evaluation, the skill scores, and the leakage demonstration.

Written before the model was tuned, because an evaluation designed after seeing a
score is not an evaluation. Three properties matter:

1. **Nothing is fitted once.** At every origin the profile, the smearing factor,
   the trailing state, every baseline and every model are rebuilt from scratch
   using only dates <= origin. Reusing any of them across origins would leak the
   future into the past, and it is the easiest mistake in this entire project.

2. **Two units, always.** A park-day is the honest unit for a model whose target
   is a park-day; an attraction-hour is what the user actually experiences. Both
   are reported side by side, and no skill number is ever quoted without its n.

3. **The exclusion is reported.** Rows whose attraction has no profile estimate
   (a ride that opened after the origin) are counted, not silently dropped.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd

from forecast import baselines, config, data, features, model as fmodel, profile as fprofile

# Every predictor scored. B1-28d is the headline baseline: measurement showed the
# rolling window beats all-history weekday cells by ~12%, so comparing against the
# weaker form would be flattering the model rather than testing it.
HEADLINE_BASELINE = "weekday_hour_28d"
PREDICTORS = (
    "ridge",
    "gbm",
    "climatology",
    "persistence_7",
    "weekday_hour_all",
    HEADLINE_BASELINE,
)


def origins_for(dates: list[str], *, min_train_days: int, stride: int) -> list[str]:
    """Origins with enough history behind them and at least one date ahead."""
    return [d for d in dates[min_train_days:-1:stride]]


def rolling_origin(
    panel: pd.DataFrame,
    levels: pd.DataFrame,
    calendar: pd.DataFrame,
    *,
    assumptions: config.ForecastAssumptions = config.DEFAULTS,
    min_train_days: int = config.MIN_TRAIN_DAYS,
    stride: int = config.ORIGIN_STRIDE,
    max_horizon: int = config.MAX_HORIZON,
    fit_gbm: bool = True,
    verbose: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Score every predictor at every horizon from every origin."""
    dates = sorted(panel["date"].unique())
    cal_keys = ["park_id", "date", "weekday"]
    row_frames: list[pd.DataFrame] = []
    day_frames: list[pd.DataFrame] = []

    for origin in origins_for(dates, min_train_days=min_train_days, stride=stride):
        horizon_end = (
            pd.Timestamp(origin) + pd.Timedelta(days=max_horizon)
        ).date().isoformat()
        test_dates = [d for d in dates if origin < d <= horizon_end]
        if not test_dates:
            continue

        shape = fprofile.build_profile(panel, levels, up_to_date=origin,
                                       assumptions=assumptions)
        smearing = (
            fprofile.smearing_factor(panel, levels, shape, up_to_date=origin)
            if assumptions.smearing else 1.0
        )
        state = features.origin_state(levels, origin=origin,
                                      windows=assumptions.trailing_windows)

        test_days = levels[levels["date"].isin(test_dates)].merge(calendar, on=cal_keys)
        if test_days.empty:
            continue

        # Hyperparameters are chosen by an inner temporal split of THIS origin's
        # training pairs, so the grid never sees a date after the origin. Tuning
        # them against the backtest's own scores would be the same leak this file
        # exists to measure, one level up.
        alpha, tau, feature_set, _ = fmodel.select_hyperparameters(
            levels, calendar, up_to_date=origin, assumptions=assumptions,
            max_horizon=max_horizon,
        )
        tuned = replace(assumptions, ridge_alpha=alpha, shrink_tau_days=tau,
                        feature_set=feature_set)
        cols = list(features.columns_for(feature_set))
        labels = features.FEATURE_SETS[feature_set]

        # Training examples are (earlier origin, the dates that followed it) pairs,
        # so a training row has the same shape as the question being asked. Fitting
        # on `date <= origin` instead gives every training row a non-positive
        # horizon and puts two features outside their serving range entirely.
        X_train, y_train, _ = features.training_pairs(
            levels, calendar, up_to_date=origin, assumptions=tuned,
            max_horizon=max_horizon,
        )
        X_test, _, meta = features.build_features(test_days, state,
                                                 assumptions=tuned)

        level_tables: dict[str, dict] = {}
        ridge = fmodel.DayLevelRidge().fit(
            X_train[:, cols], y_train, labels, alpha=alpha
        )
        level_tables["ridge"] = dict(
            zip(zip(test_days["park_id"], test_days["date"]),
                ridge.predict(X_test[:, cols]))
        )
        if fit_gbm:
            gbm = fmodel.GbmDayLevel().fit(
                X_train, y_train, features.SHIPPED, seed=assumptions.seed
            )
            level_tables["gbm"] = dict(
                zip(zip(test_days["park_id"], test_days["date"]), gbm.predict(X_test))
            )

        targets = test_days[["park_id", "date"]]
        level_tables["climatology"] = dict(
            baselines.climatology(levels, up_to_date=origin, targets=targets).levels
        )
        level_tables["persistence_7"] = dict(
            baselines.persistence(levels, up_to_date=origin, targets=targets).levels
        )

        test_rows = panel[panel["date"].isin(test_dates)].copy().reset_index(drop=True)
        out = test_rows[["park_id", "attraction_id", "name", "date", "hour",
                         "weekday", "mean_wait"]].copy()
        out = out.rename(columns={"mean_wait": "actual"})
        out["origin"] = origin
        out["horizon"] = [data.horizon_of(origin, d) for d in out["date"]]
        out["horizon_band"] = [data.horizon_band(h) for h in out["horizon"]]
        out["wait_band"] = [data.wait_band(w) for w in out["actual"]]
        out["tier"] = [
            shape.tier_at(a, h) for a, h in zip(out["attraction_id"], out["hour"])
        ]

        for name, table in level_tables.items():
            out[name] = baselines.compose(shape, table, test_rows, smearing=smearing)
        for label, window in ((HEADLINE_BASELINE, 28), ("weekday_hour_all", None)):
            cells = baselines.WeekdayHourCells.fit(panel, up_to_date=origin,
                                                   window_days=window, name=label)
            out[label] = cells.predict(test_rows)

        row_frames.append(out)

        days = test_days[["park_id", "date", "level", "weekday", "hours",
                          "party_night"]].copy()
        days["origin"] = origin
        days["ridge_alpha"] = alpha
        days["shrink_tau"] = tau
        days["feature_set"] = feature_set
        days["horizon"] = meta["horizon"].to_numpy()
        days["horizon_band"] = meta["horizon_band"].to_numpy()
        for name, table in level_tables.items():
            days[name] = [table.get((p, d), np.nan)
                          for p, d in zip(days["park_id"], days["date"])]
        day_frames.append(days)

        if verbose:
            print(f"[backtest] origin {origin}: {len(test_dates)} dates, "
                  f"{len(out)} rows")

    rows = pd.concat(row_frames, ignore_index=True)
    days = pd.concat(day_frames, ignore_index=True)
    rows["assumptions"] = assumptions.key()
    days["assumptions"] = assumptions.key()
    return rows, days


# ── metrics ───────────────────────────────────────────────────────────────


def _errors(frame: pd.DataFrame, column: str, truth: str) -> np.ndarray:
    return frame[column].to_numpy(dtype=float) - frame[truth].to_numpy(dtype=float)


def skill(frame: pd.DataFrame, predictor: str, baseline: str, *,
          truth: str = "actual") -> float:
    """1 - MSE(model) / MSE(baseline). Positive means better than the baseline."""
    usable = frame[[predictor, baseline, truth]].dropna()
    if usable.empty:
        return float("nan")
    mse_model = float(np.mean(_errors(usable, predictor, truth) ** 2))
    mse_base = float(np.mean(_errors(usable, baseline, truth) ** 2))
    return 1.0 - mse_model / mse_base if mse_base > 0 else float("nan")


def bootstrap_skill(
    frame: pd.DataFrame,
    predictor: str,
    baseline: str,
    *,
    truth: str = "actual",
    draws: int = 2000,
    seed: int = config.DEFAULTS.seed,
    level: float = 0.90,
) -> tuple[float, float, float, int]:
    """Block bootstrap over ORIGINS, not rows.

    Rows from one origin share a fitted model and a training window, so resampling
    rows independently would treat ~20,000 correlated predictions as 20,000
    independent ones and return an interval far too narrow to be honest. The
    resampling unit is therefore the origin, of which there are sixteen — which is
    why the resulting intervals are wide. That is information, not noise.

    Implemented on per-origin squared-error sums rather than by resampling the
    frame itself: the skill score is a ratio of two sums, so a draw only needs the
    per-origin (sse_model, sse_baseline, n) triples. Concatenating the rows
    instead turns a fraction of a second into several minutes.
    """
    usable = frame[[predictor, baseline, truth, "origin"]].dropna()
    if usable.empty:
        return float("nan"), float("nan"), float("nan"), 0
    err_model = (usable[predictor] - usable[truth]) ** 2
    err_base = (usable[baseline] - usable[truth]) ** 2
    grouped = pd.DataFrame(
        {"origin": usable["origin"], "model": err_model, "base": err_base}
    ).groupby("origin").agg(model=("model", "sum"), base=("base", "sum"),
                            n=("model", "size"))
    model_sse = grouped["model"].to_numpy(dtype=float)
    base_sse = grouped["base"].to_numpy(dtype=float)
    counts = grouped["n"].to_numpy(dtype=float)
    point = 1.0 - model_sse.sum() / base_sse.sum() if base_sse.sum() > 0 else float("nan")

    rng = np.random.default_rng(seed)
    n_origins = len(grouped)
    picks = rng.integers(0, n_origins, size=(draws, n_origins))
    model_draw = model_sse[picks].sum(axis=1)
    base_draw = base_sse[picks].sum(axis=1)
    count_draw = counts[picks].sum(axis=1)
    keep = (base_draw > 0) & (count_draw > 0)
    values = 1.0 - model_draw[keep] / base_draw[keep]
    if values.size == 0:
        return point, float("nan"), float("nan"), n_origins
    lo, hi = np.quantile(values, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(point), float(lo), float(hi), n_origins


def summary(frame: pd.DataFrame, *, truth: str = "actual",
            predictors: tuple[str, ...] = PREDICTORS,
            group: str | None = None) -> pd.DataFrame:
    """MAE / RMSE / median error / bias per predictor, optionally grouped."""
    rows = []
    groups = [("all", frame)] if group is None else list(frame.groupby(group))
    for key, part in groups:
        for predictor in predictors:
            if predictor not in part.columns:
                continue
            usable = part[[predictor, truth]].dropna()
            if usable.empty:
                continue
            err = _errors(usable, predictor, truth)
            rows.append(
                {
                    (group or "scope"): key,
                    "predictor": predictor,
                    "n": len(usable),
                    "n_no_estimate": int(part[predictor].isna().sum()),
                    "mae": float(np.mean(np.abs(err))),
                    "rmse": float(np.sqrt(np.mean(err**2))),
                    "median_abs": float(np.median(np.abs(err))),
                    "bias": float(np.mean(err)),
                }
            )
    return pd.DataFrame(rows)


def skill_table(frame: pd.DataFrame, *, truth: str = "actual",
                predictors: tuple[str, ...] = ("ridge", "gbm"),
                baselines_: tuple[str, ...] = (HEADLINE_BASELINE, "climatology",
                                               "persistence_7"),
                group: str = "horizon_band",
                draws: int = 2000) -> pd.DataFrame:
    """Skill with a bootstrap interval, per horizon band and per baseline."""
    rows = []
    for key, part in frame.groupby(group):
        for predictor in predictors:
            if predictor not in part.columns:
                continue
            for baseline in baselines_:
                point, lo, hi, n_origins = bootstrap_skill(
                    part, predictor, baseline, truth=truth, draws=draws
                )
                usable = part[[predictor, baseline, truth]].dropna()
                rows.append(
                    {
                        group: key,
                        "predictor": predictor,
                        "baseline": baseline,
                        "skill": point,
                        "lo": lo,
                        "hi": hi,
                        "n_rows": len(usable),
                        "n_origins": n_origins,
                        "n_park_days": int(
                            part.drop_duplicates(["origin", "park_id", "date"]).shape[0]
                        ),
                        "spans_zero": bool(lo <= 0 <= hi),
                    }
                )
    return pd.DataFrame(rows)


def residual_quantiles(frame: pd.DataFrame, predictor: str = "ridge", *,
                       truth: str = "actual",
                       level: float = config.DEFAULTS.interval_level) -> pd.DataFrame:
    """Empirical actual/predicted ratio quantiles per (horizon band, wait band).

    Stored as RATIOS rather than minutes: a +/-8 minute band is nonsense on a
    walk-on and far too narrow on a 60-minute queue, and measurement showed the
    error scales with the level. Banded on the PREDICTED wait, because that is
    what is known when the interval has to be drawn.
    """
    work = frame[[predictor, truth, "horizon_band"]].dropna().copy()
    work = work[work[predictor] > 0.5]  # a ratio to a near-zero prediction explodes
    work["wait_band"] = [data.wait_band(w) for w in work[predictor]]
    work["ratio"] = work[truth] / work[predictor]
    lo_q, hi_q = (1 - level) / 2, 1 - (1 - level) / 2
    out = (
        work.groupby(["horizon_band", "wait_band"])["ratio"]
        .agg(ratio_lo=lambda s: float(np.quantile(s, lo_q)),
             ratio_hi=lambda s: float(np.quantile(s, hi_q)),
             n="size")
        .reset_index()
    )
    return out


def coverage(frame: pd.DataFrame, residuals: pd.DataFrame, predictor: str = "ridge",
             *, truth: str = "actual") -> pd.DataFrame:
    """Does the stated interval actually contain the outcome that often?

    Measuring this against quantiles taken from the same rows returns exactly the
    nominal level by construction and proves nothing, so `honest_coverage` below
    is what the article reports. This function is kept for the in-sample figure
    and for the contrast between the two.
    """
    table = {(r.horizon_band, r.wait_band): (r.ratio_lo, r.ratio_hi)
             for r in residuals.itertuples()}
    work = frame[[predictor, truth, "horizon_band"]].dropna().copy()
    work = work[work[predictor] > 0.5]
    work["wait_band"] = [data.wait_band(w) for w in work[predictor]]
    inside = []
    for row in work.itertuples():
        ratio_lo, ratio_hi = table.get((row.horizon_band, row.wait_band), (0.0, 9e9))
        value = getattr(row, truth)
        point = getattr(row, predictor)
        inside.append(point * ratio_lo <= value <= point * ratio_hi)
    work["inside"] = inside
    return (
        work.groupby(["horizon_band", "wait_band"])["inside"]
        .agg(covered="mean", n="size").reset_index()
    )


# ── the leakage demonstration ─────────────────────────────────────────────

LEAKAGE_NOTES = {
    "random_row": "Rows split at random. ~130 rows share each park-day, so the "
                  "target day's own crowd level is in the training set. Not a "
                  "valid estimate of anything.",
    "random_day": "Park-days split at random. No row of the target day is used, "
                  "but the days either side of it are, so the model interpolates "
                  "a bracketed date instead of extrapolating past the last one.",
    "rolling_origin": "Trained only on dates strictly before the origin. The "
                      "actual task.",
}


def _r2(pred: np.ndarray, truth: np.ndarray) -> float:
    pred, truth = np.asarray(pred, float), np.asarray(truth, float)
    keep = np.isfinite(pred) & np.isfinite(truth)
    if keep.sum() < 2:
        return float("nan")
    pred, truth = pred[keep], truth[keep]
    ss_res = float(np.sum((truth - pred) ** 2))
    ss_tot = float(np.sum((truth - truth.mean()) ** 2))
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")


def leakage_comparison(
    panel: pd.DataFrame,
    levels: pd.DataFrame,
    calendar: pd.DataFrame,
    *,
    assumptions: config.ForecastAssumptions = config.DEFAULTS,
    folds: int = 5,
    max_horizon: int = config.MAX_HORIZON,
) -> pd.DataFrame:
    """The same target, the same features, three ways of splitting the data.

    This is a deliverable, not a footnote. Two distinct leaks are separated:
    splitting rows at random leaks the target day's own level through the other
    rows of that day, and splitting *days* at random still leaks the trend,
    because a held-out date sits between two training dates instead of after all
    of them. Only the third protocol answers the question actually being asked.
    """
    rng = np.random.default_rng(assumptions.seed)
    last = max(panel["date"])
    out: list[dict] = []

    # ── A. rows split at random ──────────────────────────────────────────
    # The "model" here is deliberately naive in exactly the way a random split
    # invites: estimate each park-day's level from that day's own training rows.
    rows = panel.copy().reset_index(drop=True)
    fold = rng.integers(0, folds, size=len(rows))
    shape_all = fprofile.build_profile(panel, levels, up_to_date=last,
                                       assumptions=assumptions)
    preds = np.full(len(rows), np.nan)
    truth_level = np.full(len(rows), np.nan)
    pred_level = np.full(len(rows), np.nan)
    offsets = pd.MultiIndex.from_arrays(
        [rows["attraction_id"], rows["hour"]]
    ).map(shape_all.offset)
    offsets = pd.to_numeric(pd.Series(offsets), errors="coerce").to_numpy(float)
    rows["centered"] = rows["log_wait"] - offsets
    true_levels = dict(zip(zip(levels["park_id"], levels["date"]), levels["level"]))
    for k in range(folds):
        train = rows[fold != k]
        seen = train.groupby(["park_id", "date"])["centered"].mean()
        held = np.flatnonzero(fold == k)
        keys = pd.MultiIndex.from_arrays(
            [rows.loc[held, "park_id"], rows.loc[held, "date"]]
        )
        level = pd.to_numeric(pd.Series(keys.map(seen.to_dict())),
                              errors="coerce").to_numpy(float)
        pred_level[held] = level
        preds[held] = np.expm1(level + offsets[held])
        truth_level[held] = pd.to_numeric(
            pd.Series(keys.map(true_levels)), errors="coerce"
        ).to_numpy(float)
    out.append(
        {
            "protocol": "random_row",
            "unit": "attraction-hour",
            "n": int(np.isfinite(preds).sum()),
            "wait_mae": float(np.nanmean(np.abs(preds - rows["mean_wait"]))),
            "wait_r2": _r2(preds, rows["mean_wait"].to_numpy(float)),
            "level_mae": float(np.nanmean(np.abs(pred_level - truth_level))),
            "level_r2": _r2(pred_level, truth_level),
            "note": LEAKAGE_NOTES["random_row"],
        }
    )

    # ── B and C. the same pairs, split two ways ──────────────────────────
    X, y, meta = features.training_pairs(
        levels, calendar, up_to_date=last, assumptions=assumptions,
        max_horizon=max_horizon,
    )
    cols = list(features.columns_for(assumptions.feature_set))
    labels = features.FEATURE_SETS[assumptions.feature_set]

    # B: fold assigned by DATE at random, so neighbours of a held-out date train.
    dates = sorted(meta["date"].unique())
    date_fold = {d: int(f) for d, f in zip(dates, rng.integers(0, folds, len(dates)))}
    assigned = meta["date"].map(date_fold).to_numpy()
    pred_b = np.full(len(y), np.nan)
    for k in range(folds):
        tr, te = assigned != k, assigned == k
        if tr.sum() < 20 or te.sum() == 0:
            continue
        fitted = fmodel.DayLevelRidge().fit(
            X[np.ix_(tr, cols)], y[tr], labels, alpha=assumptions.ridge_alpha
        )
        pred_b[te] = fitted.predict(X[np.ix_(te, cols)])

    # C: one temporal cut — train on the earlier dates, test on the later ones.
    cut = dates[int(len(dates) * 0.7)]
    tr, te = (meta["date"] <= cut).to_numpy(), (meta["date"] > cut).to_numpy()
    fitted = fmodel.DayLevelRidge().fit(
        X[np.ix_(tr, cols)], y[tr], labels, alpha=assumptions.ridge_alpha
    )
    pred_c = np.full(len(y), np.nan)
    pred_c[te] = fitted.predict(X[np.ix_(te, cols)])

    for protocol, pred, mask in (("random_day", pred_b, assigned >= 0),
                                 ("rolling_origin", pred_c, te)):
        keep = mask & np.isfinite(pred)
        out.append(
            {
                "protocol": protocol,
                "unit": "park-day",
                "n": int(keep.sum()),
                "wait_mae": float("nan"),
                "wait_r2": float("nan"),
                "level_mae": float(np.mean(np.abs(pred[keep] - y[keep]))),
                "level_r2": _r2(pred[keep], y[keep]),
                "note": LEAKAGE_NOTES[protocol],
            }
        )

    frame = pd.DataFrame(out)
    honest = frame.loc[frame["protocol"] == "rolling_origin", "level_r2"].iloc[0]
    frame["level_r2_inflation"] = frame["level_r2"] / honest
    frame["assumptions"] = assumptions.key()
    return frame


def honest_coverage(frame: pd.DataFrame, predictor: str = "ridge", *,
                    truth: str = "actual", share: float = 0.7,
                    level: float = config.DEFAULTS.interval_level) -> pd.DataFrame:
    """Out-of-sample interval coverage: quantiles from early origins, tested late.

    Taking the quantiles from the same rows you then score makes coverage equal
    the nominal level to four decimal places, which is arithmetic rather than
    evidence. Here the bands are built from the earlier origins and measured on
    the later ones, which is the only version of this number worth printing.
    """
    origins = sorted(frame["origin"].unique())
    if len(origins) < 4:
        return pd.DataFrame()
    cut = origins[max(1, int(len(origins) * share)) - 1]
    early = frame[frame["origin"] <= cut]
    late = frame[frame["origin"] > cut]
    if early.empty or late.empty:
        return pd.DataFrame()
    bands = residual_quantiles(early, predictor, truth=truth, level=level)
    out = coverage(late, bands, predictor, truth=truth)
    out["fitted_on_origins"] = len(early["origin"].unique())
    out["tested_on_origins"] = len(late["origin"].unique())
    out["nominal"] = level
    return out
