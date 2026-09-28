"""Saving and loading the trained forecaster as JSON and CSV.

Deliberately not a pickle. A pickled estimator silently binds to the numpy and
scikit-learn versions that wrote it, cannot be reviewed in a diff, and cannot be
read at all by a process that does not have scikit-learn installed — which is
exactly the situation the serving path has to survive, because the dependency is
installed with `uv pip install` and is not in `pyproject.toml`, so a later
`uv sync` removes it. The shipped model is a handful of coefficients and a table
of per-(attraction, hour) offsets; JSON and CSV cost nothing and stay readable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from forecast import config
from forecast.model import DayLevelRidge, Forecaster
from forecast.profile import Profile

MANIFEST = "manifest.json"
DAY_MODEL = "day_model.json"
PROFILE = "profile.csv"
RESIDUALS = "residuals.csv"
BASELINE = "baseline.csv"


def save(forecaster: Forecaster, directory: Path | str = config.ARTIFACT_DIR) -> Path:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    (out / MANIFEST).write_text(json.dumps(forecaster.manifest, indent=2, default=str))
    (out / DAY_MODEL).write_text(json.dumps(forecaster.day_model.to_dict(), indent=2))
    forecaster.profile.frame().to_csv(out / PROFILE, index=False)
    forecaster.residuals.to_csv(out / RESIDUALS, index=False)
    if forecaster.baseline is not None:
        forecaster.baseline.to_csv(out / BASELINE, index=False)
    return out


def load(directory: Path | str = config.ARTIFACT_DIR) -> Forecaster:
    """Rebuild the forecaster using numpy and pandas only."""
    src = Path(directory)
    missing = [n for n in (MANIFEST, DAY_MODEL, PROFILE, RESIDUALS)
               if not (src / n).exists()]
    if missing:
        raise FileNotFoundError(
            f"{src} is not a trained artifact — missing {missing}. "
            "Run `python -m forecast.cli train`."
        )
    manifest = json.loads((src / MANIFEST).read_text())
    day_model = DayLevelRidge.from_dict(json.loads((src / DAY_MODEL).read_text()))
    frame = pd.read_csv(src / PROFILE)
    residuals = pd.read_csv(src / RESIDUALS)
    baseline = (
        pd.read_csv(src / BASELINE) if (src / BASELINE).exists() else None
    )

    keys = list(zip(frame["attraction_id"], frame["hour"]))
    profile = Profile(
        offset=dict(zip(keys, frame["offset"].astype(float))),
        tier=dict(zip(keys, frame["tier"])),
        n_dates=dict(zip(keys, frame["n_dates"].astype(int))),
        park_of=dict(zip(frame["attraction_id"], frame["park_id"])),
        names=dict(zip(frame["attraction_id"], frame["name"])),
        up_to_date=manifest["origin"],
    )

    # The stored feature list is part of the contract: a model whose columns no
    # longer match the code would predict confident nonsense, so refuse instead.
    if list(day_model.names) != list(manifest["features"]):
        raise ValueError(
            "artifact is inconsistent: day_model names do not match the manifest"
        )
    return Forecaster(profile=profile, day_model=day_model,
                      smearing=float(manifest["smearing"]),
                      residuals=residuals, manifest=manifest,
                      baseline=baseline)
