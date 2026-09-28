# Wait-time forecasting

Given a future date, predict the standby wait at every attraction in all seven
Orlando parks, hour by hour. The companion to `research_project/`, which routes a
day *given* wait estimates; this package is where the estimates for a specific
future date come from.

Like `research_project/`, it is **read-only**: it opens `data/attractions.db` with
`mode=ro` (reusing `research_project.db.connect`) and can never write to it, so it
cannot disturb the collector or the dashboard.

- `ARTICLE.md` — the write-up, rendered at `/forecast/article` in the web app.
- `results/` — committed backtest output. Every figure and every number in the
  article reads from these, so prose and charts cannot drift.
- `artifacts/` — the trained model, as JSON and CSV.

## The shape of the problem

Measured before anything was built, because it decides what is worth doing:

| | |
|---|---|
| Panel | **102,698** rows · 91 dates (2026-06-25 → 2026-09-24) · 123 attractions · 7 parks |
| Modelling unit | **637 park-days** |
| Variance explained by attraction × hour alone | **0.80** |
| Share of the *remainder* explained by the day | **0.28** |
| Weekday's share of park-day variance | **0.017** |

A lookup table indexed by ride and hour is already most of a wait predictor. The
forecast supplies one number per park-day — how busy it is — and that is the only
thing there is to compete for. Hence two stages, kept structurally separate:

```
wait(attraction, hour, date) = expm1( shape(attraction, hour) + level(park, date) ) × smearing
```

`shape` is measured; `level` is predicted. Making the model's unit a park-day
means the training set is a few hundred rows, not a hundred thousand — which is
the honest count, and pretending otherwise is the classic way this kind of project
overstates itself.

## Modules

| File | Purpose |
|---|---|
| `config.py` | Every invented number; `ForecastAssumptions` (frozen, hashed into each result row). Imports `HISTORY_START`/`EXCLUDED_DATES` from `research_project.config` rather than restating them |
| `data.py` | Panel loading, park-day levels, the published calendar, the forecast origin and the horizon ceiling |
| `profile.py` | The centered attraction × hour shape, with a provenance tier per estimate, and Duan's smearing factor |
| `features.py` | The design matrix, the `KNOWABLE` gate, and `training_pairs` |
| `baselines.py` | Climatology, the weekday × hour incumbent (all-history and 28-day rolling), persistence |
| `model.py` | `DayLevelRidge` (JSON round-trip, numpy prediction), `GbmDayLevel`, `Forecaster`, hyperparameter selection |
| `backtest.py` | Rolling origin, skill with a bootstrap over origins, residual quantiles, the leakage demonstration |
| `artifacts.py` | Save/load as JSON + CSV — deliberately not a pickle |
| `predict.py` | The serving path. Numpy only; reads the schedule and nothing else |
| `cli.py` | `dates`, `backtest`, `leakage`, `train`, `predict` |

## Two rules enforced in code

**Nothing inadmissible can be a feature.** `features.KNOWABLE` marks every name
admissible or not, and the module refuses to import if `SHIPPED` contains one that
isn't. Weather, ride downtime and same-day readings are listed as `False`
explicitly rather than omitted. The stronger guard is behavioural, in
`tests/test_forecast.py`: corrupt every observation after the origin, rebuild, and
require the design matrix, the profile and the training pairs to come back
bit-identical.

**A date past the schedule ceiling is refused.** The published operating window is
the model's strongest feature (r = 0.69 at Magic Kingdom — the operator sets hours
from its own demand forecast, so the schedule is a leaked forecast). The feed
reaches about a month out; beyond that `predict.forecast_day` raises
`HorizonError` rather than imputing hours and returning a confident number built
on an invention.

## Results, including where it loses

Skill = share of squared error removed, against the weekday × hour estimate the
route optimizer uses. Intervals are a bootstrap over the 16 retraining **origins**,
which is the real sample size for such a claim.

| Days ahead | Skill | 90% interval |
|---|---|---|
| 1–7 | +0.113 | +0.012 … +0.196 |
| 8–14 | +0.172 | +0.096 … +0.237 |
| 15–29 | +0.163 | +0.064 … +0.243 |

Against simply carrying last week's crowd level forward, the intervals mostly span
zero — at short range a one-line heuristic is as good. The gradient-boosted variant
is slightly better at 1–7 days and clearly worse at 15–29 (skill −0.098 against
persistence), so the ridge ships: chosen on a measurement, not on preference.

Error is very unequal: MAE 3.83 on walk-ons against **11.57** on headliners, and
4.76 at Magic Kingdom against **13.27** at Epic Universe. Five predictions were
pre-registered and **one held**; the article records all five.

## Dependencies — read this before running `uv sync`

Fitting needs **scikit-learn**, installed with:

```bash
uv pip install scikit-learn        # brings scipy, joblib, threadpoolctl
```

It is deliberately **not** in `pyproject.toml`: that file and `uv.lock` currently
carry uncommitted changes from other work, and `uv add` re-resolves the whole lock
and entangles the two beyond separating. `uv pip install` touches neither. See
`requirements-research.txt`.

The consequence is that a future `uv sync` will **remove scikit-learn from the
venv**, so the serving path is built to survive it: sklearn is imported only inside
`fit()`, prediction is a dot product in numpy, and the artifact is JSON and CSV.
`cli predict` and the whole web app keep working without it. A test asserts
`"sklearn" not in sys.modules` after serving a prediction.

Do not install lightgbm (wrong tool for 637 rows;
`HistGradientBoostingRegressor` already covers the boosted arm), statsmodels (η²
and the bootstrap are a few numpy lines) or matplotlib (the charts are inline SVG
against a validated palette).

## Running it

```bash
.venv/bin/python -m forecast.cli dates
.venv/bin/python -m forecast.cli backtest --out forecast/results
.venv/bin/python -m forecast.cli leakage  --out forecast/results
.venv/bin/python -m forecast.cli train
.venv/bin/python -m forecast.cli predict --date 2026-10-15 --explain
.venv/bin/python tests/test_forecast.py
```

`backtest` must run before `train`: the prediction intervals are the model's
measured historical error and the skill figures shown in the app both come from
it, rather than being asserted by the model about itself.

The web app serves both projects on **127.0.0.1:3467** — `/forecast` for the
predictor, `/forecast/article` for the write-up.
