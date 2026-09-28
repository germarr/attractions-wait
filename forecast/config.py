"""Constants and the frozen assumption set for the wait-time forecaster.

Same discipline as `research_project/config.py`: every invented number lives
here, nothing deeper in the package reads a module-level tunable at call time,
and every result row carries `ForecastAssumptions.key()` so a backtest cannot
silently mix results computed under different constants.

What this package does NOT redefine: `HISTORY_START` and `EXCLUDED_DATES` are
imported from `research_project.config`, because they encode facts about the
dataset (a missing schedule row on 2026-06-24, the poll-interval changeover on
2026-08-04) rather than choices about this model. Two copies of a fact drift;
one copy cannot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from research_project.config import (  # facts about the data, not choices here
    EXCLUDED_DATES,
    HISTORY_START,
    PARK_TZ,
    SEED,
)

__all__ = [
    "PARKS",
    "PARK_ORDER",
    "HISTORY_START",
    "EXCLUDED_DATES",
    "PARK_TZ",
    "SEED",
    "CORE_HOURS",
    "MODEL_HOURS",
    "WALK_ON_MAX",
    "HEADLINER_MIN",
    "HORIZON_BANDS",
    "ForecastAssumptions",
]

# ── the parks ─────────────────────────────────────────────────────────────

# All seven Orlando parks across both destinations. Pinned here rather than read
# from the DB so a park silently disappearing from the feed fails a test instead
# of quietly shrinking the model; `tests/test_forecast.py` asserts they agree.
PARKS: Mapping[str, str] = {
    "75ea578a-adc8-4116-a54d-dccb60765ef9": "Magic Kingdom Park",
    "47f90d2c-e191-4239-a466-5892ef59a88b": "EPCOT",
    "288747d1-8b4f-4a64-867e-ea7c9b27bad8": "Disney's Hollywood Studios",
    "1c84a229-8862-4648-9c71-378ddd2c7693": "Disney's Animal Kingdom Theme Park",
    "eb3f4560-2383-4a36-9152-6b3e5ed6bc57": "Universal Studios Florida",
    "267615cc-8943-4c2a-ae2c-5da728ca591f": "Universal Islands of Adventure",
    "12dbb85b-265f-44e6-bccf-f1faa17211fc": "Universal Epic Universe",
}
PARK_ORDER: tuple[str, ...] = tuple(PARKS)

# ── hours ─────────────────────────────────────────────────────────────────

# `attractionhourly.hour` is park-local and spans 8..23. The day LEVEL is defined
# over a narrower core, because the edge hours are thin (hour 8 has 1,048 rows
# against ~9,400 at midday, hour 23 has 69) and a mean taken over them would move
# with which parks happened to open early rather than with how busy the day was.
MODEL_HOURS = range(8, 24)
CORE_HOURS = range(9, 22)

# Band thresholds. These duplicate `research_project/webapp/charts.py`, which is
# presentation and must not be imported from here; a test pins the two together,
# the same way research_project/db.py pins its copy of app.stats._operating_windows.
WALK_ON_MAX = 10.0
HEADLINER_MIN = 25.0

# ── reporting and backtest geometry ───────────────────────────────────────

# Three horizon buckets. 1-7 is "the trip is nearly here", 8-14 is the planning
# window, 15+ is where measurement said persistence has already gone negative.
HORIZON_BANDS: tuple[tuple[int, int], ...] = ((1, 7), (8, 14), (15, 29))

MIN_TRAIN_DAYS = 42   # a rolling origin needs enough history to fit 16 features
ORIGIN_STRIDE = 3     # ~17 origins across 92 days
MAX_HORIZON = 21      # beyond this the holdout tail is too thin to score

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

# `.gitignore` carries a bare `data/` rule that matches at ANY depth, so an
# artifact directory named data/ would be silently untracked. Asserted in tests.
assert "data" not in ARTIFACT_DIR.parts, "artifacts must not live under a data/ dir"


@dataclass(frozen=True)
class ForecastAssumptions:
    """Every tunable, in one immutable bundle threaded through the model."""

    # target construction
    target_transform: str = "log1p"      # waits are right-skewed and multiplicative
    smearing: bool = True                # Duan correction on the back-transform

    # features
    trailing_windows: tuple[int, ...] = (7, 28)
    shrink_tau_days: float = 10.0        # persistence -> climatology decay constant
    profile_min_dates: int = 5           # below this an attraction falls back a tier

    # screening
    min_hour_coverage: float = 0.5       # share of a full hour's polls required

    # model
    feature_set: str = "shrunk"   # chosen per origin by inner validation
    ridge_alpha: float = 1.0
    interval_level: float = 0.80
    seed: int = SEED

    def key(self) -> str:
        """Short stable hash; every result row carries it."""
        blob = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    def provenance(self) -> list[tuple[str, Any, str]]:
        """(name, value, source) per assumption, for the article's methods note."""
        sources = {
            "target_transform": "modelling choice (skew)",
            "smearing": "required; expm1(mean of logs) is a median",
            "trailing_windows": "measured (persistence decay)",
            "shrink_tau_days": "fitted on training folds",
            "profile_min_dates": "measured (cell density)",
            "min_hour_coverage": "measured (poll density)",
            "feature_set": "selected on training folds",
            "ridge_alpha": "fitted on training folds",
            "interval_level": "reporting choice",
            "seed": "fixed for determinism",
        }
        return [(k, v, sources[k]) for k, v in asdict(self).items()]


DEFAULTS = ForecastAssumptions()
