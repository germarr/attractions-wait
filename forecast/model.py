"""The day-level model, and the forecaster that composes it with the shape.

Two deliberate constraints shape this file.

**Ridge, not a boosted ensemble.** The modelling unit is a park-day, so there are
a few hundred rows and about nineteen features, with one regime change inside the
window. Ridge gives coefficients that can be printed and argued about, a closed
form that needs no library at prediction time, and stability under the
collinearity of hours/open_minute/climatology. A histogram booster is run in the
backtest so "did you try a real ML model" is answered by a measurement rather
than an opinion, and it ships only if it wins.

**Serving must not need scikit-learn.** sklearn is imported inside `fit` and
nowhere else; `predict` is pure numpy over stored coefficients. This matters
because the dependency is installed with `uv pip install` and is deliberately not
in `pyproject.toml`, so a later `uv sync` would remove it — and the web app and
the CLI's predict path have to keep working when that happens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from forecast import config, data
from forecast.baselines import compose
from forecast.profile import Profile


@dataclass
class DayLevelRidge:
    """Standardize, then ridge. JSON round-trips exactly; predicts in numpy."""

    names: tuple[str, ...] = ()
    mean: np.ndarray = field(default_factory=lambda: np.zeros(0))
    scale: np.ndarray = field(default_factory=lambda: np.ones(0))
    coef: np.ndarray = field(default_factory=lambda: np.zeros(0))
    intercept: float = 0.0
    alpha: float = 1.0
    name: str = "ridge"

    def fit(self, X: np.ndarray, y: np.ndarray, names: Sequence[str], *, alpha: float = 1.0):
        from sklearn.linear_model import Ridge  # lazily: serving must not need sklearn

        self.names = tuple(names)
        self.mean = X.mean(axis=0)
        # A constant column (a park with no rows in this fold) would divide by zero.
        spread = X.std(axis=0)
        self.scale = np.where(spread > 1e-12, spread, 1.0)
        model = Ridge(alpha=alpha).fit((X - self.mean) / self.scale, y)
        self.coef = np.asarray(model.coef_, dtype=float)
        self.intercept = float(model.intercept_)
        self.alpha = alpha
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        return ((X - self.mean) / self.scale) @ self.coef + self.intercept

    def standardized(self) -> list[tuple[str, float]]:
        """Coefficients on the standardized scale, largest magnitude first."""
        pairs = list(zip(self.names, self.coef))
        return sorted(pairs, key=lambda kv: -abs(kv[1]))

    def to_dict(self) -> dict:
        return {
            "kind": "ridge",
            "names": list(self.names),
            "mean": self.mean.tolist(),
            "scale": self.scale.tolist(),
            "coef": self.coef.tolist(),
            "intercept": self.intercept,
            "alpha": self.alpha,
        }

    @classmethod
    def from_dict(cls, blob: Mapping) -> "DayLevelRidge":
        return cls(
            names=tuple(blob["names"]),
            mean=np.asarray(blob["mean"], dtype=float),
            scale=np.asarray(blob["scale"], dtype=float),
            coef=np.asarray(blob["coef"], dtype=float),
            intercept=float(blob["intercept"]),
            alpha=float(blob.get("alpha", 1.0)),
        )


@dataclass
class GbmDayLevel:
    """Histogram gradient boosting, for the backtest's "we tried it" arm.

    Shallow and short on purpose: a few hundred rows cannot support a deep
    ensemble, and early stopping would need a validation split carved out of an
    already small training fold.
    """

    model: object | None = None
    names: tuple[str, ...] = ()
    name: str = "gbm"

    def fit(self, X: np.ndarray, y: np.ndarray, names: Sequence[str], *, seed: int = 0):
        from sklearn.ensemble import HistGradientBoostingRegressor

        self.names = tuple(names)
        self.model = HistGradientBoostingRegressor(
            max_depth=3, max_iter=200, learning_rate=0.05,
            early_stopping=False, random_state=seed,
        ).fit(X, y)
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("GbmDayLevel.predict before fit")
        return np.asarray(self.model.predict(X), dtype=float)


# ── the composed forecaster ───────────────────────────────────────────────


@dataclass
class Forecaster:
    """Profile + day model + smearing + residual quantiles, ready to serve."""

    profile: Profile
    day_model: DayLevelRidge
    smearing: float
    residuals: pd.DataFrame          # horizon_band, wait_band, ratio_lo, ratio_hi, n
    manifest: dict
    baseline: pd.DataFrame | None = None   # the incumbent, shipped for comparison

    @property
    def state(self) -> dict:
        from forecast.features import state_from_json

        return state_from_json(self.manifest["origin_state"])

    def baseline_waits(self, rows: pd.DataFrame) -> "np.ndarray":
        """The incumbent weekday x hour estimate for the same rows.

        Shipped alongside the model so the page cannot show a prediction without
        the number it claims to improve on sitting next to it.
        """
        if self.baseline is None or self.baseline.empty:
            return np.full(len(rows), np.nan)
        cells = self.baseline.set_index(["attraction_id", "weekday", "hour"])["wait"]
        per_hour = self.baseline.groupby(["attraction_id", "hour"])["wait"].mean()
        per_attraction = self.baseline.groupby("attraction_id")["wait"].mean()
        def look(keys, table):
            return pd.to_numeric(pd.Series(keys.map(table.to_dict())),
                                 errors="coerce").to_numpy(float, copy=True)
        out = look(pd.MultiIndex.from_arrays(
            [rows["attraction_id"], rows["weekday"], rows["hour"]]), cells)
        gaps = np.isnan(out)
        if gaps.any():
            out[gaps] = look(pd.MultiIndex.from_arrays(
                [rows["attraction_id"], rows["hour"]]), per_hour)[gaps]
        gaps = np.isnan(out)
        if gaps.any():
            out[gaps] = look(pd.Index(rows["attraction_id"]), per_attraction)[gaps]
        return out

    @property
    def origin(self) -> str:
        return self.manifest["origin"]

    def predict_levels(self, targets: pd.DataFrame, state: Mapping,
                       *, assumptions=None) -> dict[tuple[str, str], float]:
        """Day level per (park, date), using the stored feature set and tau.

        The assumptions come from the artifact, not from the module defaults: the
        stored coefficients were fitted under a particular tau and a particular
        subset of columns, and rebuilding the matrix under any other would silently
        feed the model the wrong numbers in the right shape.
        """
        from dataclasses import replace

        from forecast.features import build_features, columns_for

        if assumptions is None:
            stored = self.manifest.get("assumptions") or {}
            assumptions = replace(config.DEFAULTS, **{
                k: (tuple(v) if isinstance(v, list) else v)
                for k, v in stored.items()
                if k in config.ForecastAssumptions.__dataclass_fields__
            })
        X, _, meta = build_features(targets, state, assumptions=assumptions)
        cols = list(columns_for(self.manifest["feature_set"]))
        values = self.day_model.predict(X[:, cols])
        return {
            (row.park_id, row.date): float(value)
            for row, value in zip(meta.itertuples(), values)
        }

    def predict_rows(
        self,
        rows: pd.DataFrame,
        levels: Mapping[tuple[str, str], float],
        *,
        interval: bool = True,
    ) -> pd.DataFrame:
        """Minutes per (park, date, attraction, hour), with an error band."""
        out = rows.copy().reset_index(drop=True)
        out["wait"] = compose(self.profile, levels, out, smearing=self.smearing)
        out["tier"] = [
            self.profile.tier_at(a, h)
            for a, h in zip(out["attraction_id"], out["hour"])
        ]
        out["band"] = [data.wait_band(w) for w in out["wait"]]
        if interval:
            lo, hi = self._interval(out)
            out["lo"], out["hi"] = lo, hi
        return out

    def _interval(self, rows: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        """Multiply by the model's own measured historical error ratio.

        Applied in the ratio domain rather than added in minutes, because a
        +/- 8 minute band is nonsense on a walk-on ride and far too narrow on a
        60-minute queue. Bucketed by (horizon band, wait band) because measurement
        showed the error scales with both.
        """
        table = {
            (row.horizon_band, row.wait_band): (row.ratio_lo, row.ratio_hi)
            for row in self.residuals.itertuples()
        }
        fallback = (
            float(self.residuals["ratio_lo"].median()) if len(self.residuals) else 0.7,
            float(self.residuals["ratio_hi"].median()) if len(self.residuals) else 1.4,
        )
        lo = np.empty(len(rows))
        hi = np.empty(len(rows))
        for i, row in enumerate(rows.itertuples()):
            band = data.horizon_band(data.horizon_of(self.origin, row.date))
            ratio_lo, ratio_hi = table.get((band, row.band), fallback)
            lo[i] = row.wait * ratio_lo
            hi[i] = row.wait * ratio_hi
        return lo, hi


# ── hyperparameter selection, on training data only ───────────────────────

ALPHA_GRID = (0.1, 1.0, 10.0, 100.0)
TAU_GRID = (3.0, 7.0, 14.0, 30.0)


def select_hyperparameters(
    levels: pd.DataFrame,
    calendar: pd.DataFrame,
    *,
    up_to_date: str,
    assumptions: config.ForecastAssumptions = config.DEFAULTS,
    max_horizon: int = config.MAX_HORIZON,
    alphas: Sequence[float] = ALPHA_GRID,
    taus: Sequence[float] = TAU_GRID,
    feature_sets: Sequence[str] = ("full", "shrunk", "minimal"),
    inner_share: float = 0.7,
) -> tuple[float, float, str, float]:
    """Pick (alpha, tau) by an inner temporal split of the training pairs.

    Every candidate is scored on targets that are LATER than the ones it was
    fitted on, and nothing here ever sees a date after `up_to_date`. Choosing
    these by looking at the backtest's own scores would be the same leak the whole
    project is about, one level up.
    """
    from dataclasses import replace

    from forecast.features import columns_for, FEATURE_SETS, training_pairs

    best = (assumptions.ridge_alpha, assumptions.shrink_tau_days,
            assumptions.feature_set, float("inf"))
    for tau in taus:
        candidate = replace(assumptions, shrink_tau_days=tau)
        X, y, meta = training_pairs(
            levels, calendar, up_to_date=up_to_date,
            assumptions=candidate, max_horizon=max_horizon,
        )
        ordered = sorted(meta["date"].unique())
        if len(ordered) < 4:
            continue
        cutoff = ordered[max(1, int(len(ordered) * inner_share)) - 1]
        inner = (meta["date"] <= cutoff).to_numpy()
        held = ~inner
        if inner.sum() < 20 or held.sum() < 10:
            continue
        for name in feature_sets:
            cols = list(columns_for(name))
            labels = FEATURE_SETS[name]
            for alpha in alphas:
                fitted = DayLevelRidge().fit(
                    X[np.ix_(inner, cols)], y[inner], labels, alpha=alpha
                )
                mse = float(
                    np.mean((fitted.predict(X[np.ix_(held, cols)]) - y[held]) ** 2)
                )
                if mse < best[3]:
                    best = (float(alpha), float(tau), name, mse)
    return best


def fit_forecaster(
    panel: pd.DataFrame,
    levels: pd.DataFrame,
    calendar: pd.DataFrame,
    *,
    origin: str,
    residuals: pd.DataFrame,
    assumptions: config.ForecastAssumptions = config.DEFAULTS,
    max_horizon: int = config.MAX_HORIZON,
    skill: Mapping | None = None,
) -> "Forecaster":
    """Fit the shipped model at `origin` and wrap it up ready to serve."""
    from dataclasses import asdict, replace

    from forecast import features as ffeat
    from forecast import profile as fprof

    alpha, tau, feature_set, inner_mse = select_hyperparameters(
        levels, calendar, up_to_date=origin, assumptions=assumptions,
        max_horizon=max_horizon,
    )
    tuned = replace(assumptions, ridge_alpha=alpha, shrink_tau_days=tau,
                    feature_set=feature_set)

    shape = fprof.build_profile(panel, levels, up_to_date=origin, assumptions=tuned)
    smearing = (
        fprof.smearing_factor(panel, levels, shape, up_to_date=origin)
        if tuned.smearing else 1.0
    )
    X, y, meta = ffeat.training_pairs(
        levels, calendar, up_to_date=origin, assumptions=tuned,
        max_horizon=max_horizon,
    )
    cols = list(ffeat.columns_for(feature_set))
    day_model = DayLevelRidge().fit(
        X[:, cols], y, ffeat.FEATURE_SETS[feature_set], alpha=alpha
    )

    state = ffeat.origin_state(levels, origin=origin,
                               windows=tuned.trailing_windows)
    from forecast.baselines import WeekdayHourCells

    incumbent = WeekdayHourCells.fit(panel, up_to_date=origin, window_days=28)
    baseline_rows = pd.DataFrame(
        [{"attraction_id": a, "weekday": w, "hour": h, "wait": v}
         for (a, w, h), v in incumbent.cell.items()]
    )

    manifest = {
        "origin": origin,
        "origin_state": ffeat.state_to_json(state),
        "trained_at": pd.Timestamp.utcnow().isoformat(),
        "assumptions_key": tuned.key(),
        "assumptions": asdict(tuned),
        "feature_set": feature_set,
        "features": list(ffeat.FEATURE_SETS[feature_set]),
        "ridge_alpha": alpha,
        "shrink_tau_days": tau,
        "inner_validation_mse": inner_mse,
        "smearing": smearing,
        "train_pairs": int(len(y)),
        "train_first_date": min(panel["date"]),
        "train_last_date": origin,
        "n_park_days": int(len(levels[levels["date"] <= origin])),
        "n_attractions": int(panel["attraction_id"].nunique()),
        "profile_support": shape.support(),
        "skill": dict(skill or {}),
        "library_versions": _library_versions(),
    }
    return Forecaster(profile=shape, day_model=day_model, smearing=smearing,
                      residuals=residuals, manifest=manifest,
                      baseline=baseline_rows)


def _library_versions() -> dict:
    import numpy
    import pandas

    versions = {"numpy": numpy.__version__, "pandas": pandas.__version__}
    try:  # only present when the model was fitted, never needed to predict
        import sklearn

        versions["scikit-learn"] = sklearn.__version__
    except ImportError:  # pragma: no cover
        versions["scikit-learn"] = "absent"
    return versions
