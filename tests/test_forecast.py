"""Invariants for the wait-time forecaster.

    .venv/bin/python tests/test_forecast.py

Non-zero exit if anything diverges. Same shape as the other research tests: every
failure is collected rather than raised, so one run reports all of them, and the
real entry point is __main__ rather than a test framework. pytest is deliberately
not a dependency of this repo.

Several assertions pin measured values from the live database and from committed
backtest output. They are expected to need re-pinning as history accumulates —
that is the point: a silent change in a load-bearing number should break a test
rather than quietly rewrite the article.
"""

from __future__ import annotations

import json
import subprocess
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from forecast import artifacts, backtest, config, data, features, model  # noqa: E402
from forecast import predict as fpredict  # noqa: E402
from forecast import profile as fprofile  # noqa: E402
from research_project import db as rdb  # noqa: E402
from research_project.webapp import charts, fcharts  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "forecast" / "results"

# How far the trained artifact may lag the newest rollup before it counts as stale.
# A week: long enough that a rollover does not fail the suite, short enough that a
# forgotten retrain surfaces.
MAX_ARTIFACT_LAG_DAYS = 7


def failures() -> list[str]:
    out: list[str] = []

    def check(ok: bool, msg: str) -> None:
        if not ok:
            out.append(msg)

    conn = rdb.connect()
    # Derived, not pinned: the rollup advances every night, and a hardcoded origin
    # makes this suite fail every morning for no reason anyone should have to read.
    ORIGIN = data.last_observed_date(conn)

    # ── the connection must not be able to write ─────────────────────────
    try:
        conn.execute("CREATE TABLE _forecast_write_probe (x INTEGER)")
        out.append("the forecast connection is WRITABLE — it must be mode=ro")
    except sqlite3.OperationalError:
        pass

    # ── 1. panel hygiene ────────────────────────────────────────────────
    panel = data.load_panel(conn)
    levels = data.park_day_levels(panel)
    # Pinned as INVARIANTS rather than constants. The panel grows by a date every
    # night, so `len(panel) == 102_698` fails every morning — which trains everyone
    # to re-pin without looking, the opposite of what a pinned test is for. These
    # relationships hold on any day and still catch a real regression: a lost date,
    # a park dropping out, or the screening silently widening.
    expected_dates = len(
        [
            d
            for d in pd.date_range(config.HISTORY_START, panel["date"].max())
            .strftime("%Y-%m-%d")
            if d not in config.EXCLUDED_DATES
        ]
    )
    check(panel["date"].nunique() == expected_dates,
          f"panel spans {panel['date'].nunique()} dates; every date from "
          f"{config.HISTORY_START} to {panel['date'].max()} except the "
          f"{len(config.EXCLUDED_DATES)} excluded ones should be present "
          f"({expected_dates})")
    check(panel["park_id"].nunique() == 7,
          f"panel covers {panel['park_id'].nunique()} parks, expected 7")
    check(120 <= panel["attraction_id"].nunique() <= 135,
          f"panel covers {panel['attraction_id'].nunique()} attractions, outside the "
          "120-135 band the seven rosters have held")
    check(len(panel) > 100_000,
          f"panel has only {len(panel)} rows; it has held over 100,000 since "
          "2026-09-24 and can only grow")
    check(len(levels) == panel["date"].nunique() * 7,
          f"levels has {len(levels)} park-days, expected "
          f"{panel['date'].nunique()} dates x 7 parks; a mismatch means a park went "
          "dark for a day and the panel is no longer a full grid")
    check(set(panel["date"]).isdisjoint(config.EXCLUDED_DATES),
          "an excluded date survived into the panel")
    check(panel["mean_wait"].min() >= 0, "a negative mean wait reached the panel")
    check(panel["hour"].between(0, 23).all(), "an hour outside 0..23 reached the panel")

    # ── 2. the poll seam: per-row division, never pooled ────────────────
    def seam_bias(park_id: str | None) -> float:
        sql = """
            SELECT h.sum_wait, h.n_wait
            FROM attractionhourly h JOIN attraction a ON a.id = h.attraction_id
            WHERE h.n_wait > 0 AND h.date >= ? AND h.date NOT IN (?, ?)
        """
        params = [config.HISTORY_START, *sorted(config.EXCLUDED_DATES)]
        if park_id:
            sql += " AND a.park_id = ?"
            params.append(park_id)
        raw = pd.read_sql_query(sql, conn, params=params)
        return float(raw["sum_wait"].sum() / raw["n_wait"].sum()
                     - (raw["sum_wait"] / raw["n_wait"]).mean())

    # ADR-0006's +0.74 min was measured on Magic Kingdom, so the cross-check has to
    # be made on Magic Kingdom too. Pooling seven parks gives a larger figure
    # (~+1.26) because the parks differ in both wait level and polling density —
    # comparing that against +0.74 would be comparing two different populations.
    mk_bias = seam_bias("75ea578a-adc8-4116-a54d-dccb60765ef9")
    check(0.55 <= mk_bias <= 0.95,
          f"Magic Kingdom pooled-vs-per-row bias is {mk_bias:+.3f} min, expected "
          "about +0.74 (ADR-0006); if this moved, the poll-interval seam changed")
    all_bias = seam_bias(None)
    check(all_bias > 0.9,
          f"all-park pooled-vs-per-row bias is {all_bias:+.3f} min; the article "
          "quotes the seam as material across all seven parks")
    check("sum_wait / h.n_wait" in data._PANEL_SQL or "sum_wait / n_wait" in data._PANEL_SQL,
          "the panel SQL no longer divides per row — the pooled form biases high")
    check("SUM(h.sum_wait)" not in data._PANEL_SQL,
          "the panel SQL pools SUM(sum_wait), which crosses the poll-interval seam")

    # ── 3. the coverage probe is per (park, date), not park-wide ────────
    check("GROUP BY park_id, date" in data._PANEL_SQL,
          "the density probe must group by (park_id, date): parks keep different "
          "hours, so a global MAX(n_wait) mis-scales the short-hours parks")
    # a park that closes early must not be systematically screened out
    by_park = panel.groupby("park_id")["coverage"].median()
    check(by_park.min() > 0.8,
          f"lowest per-park median coverage is {by_park.min():.3f}; a park-wide "
          "probe would depress this for early-closing parks")

    # ── 4. no inadmissible feature can be shipped ──────────────────────
    bad = [n for n in features.SHIPPED if not features.KNOWABLE.get(n, False)]
    check(not bad, f"inadmissible features in SHIPPED: {bad}")
    for banned in ("weather", "n_down", "same_day_wait", "day_index"):
        check(features.KNOWABLE.get(banned) is False,
              f"{banned} must be declared inadmissible")
        check(banned not in features.SHIPPED, f"{banned} is being shipped")
    for name, cols in features.FEATURE_SETS.items():
        unknown = [c for c in cols if c not in features.SHIPPED]
        check(not unknown, f"feature set {name} names unknown columns {unknown}")

    # ── 5. the behavioural leakage test ────────────────────────────────
    # Corrupt every observation AFTER the origin. Anything that reads the future
    # changes the design matrix; nothing legitimate can.
    calendar = data.calendar(conn, start=config.HISTORY_START, end="2026-10-25")
    cut = "2026-09-10"
    poisoned = panel.copy()
    mask = poisoned["date"] > cut
    poisoned.loc[mask, "mean_wait"] *= 10.0
    poisoned.loc[mask, "log_wait"] = np.log1p(poisoned.loc[mask, "mean_wait"])
    poisoned_levels = data.park_day_levels(poisoned)

    state_clean = features.origin_state(levels, origin=cut)
    state_dirty = features.origin_state(poisoned_levels, origin=cut)
    targets = calendar[calendar["date"] > cut].head(40)
    X_clean, _, _ = features.build_features(targets, state_clean)
    X_dirty, _, _ = features.build_features(targets, state_dirty)
    check(np.array_equal(X_clean, X_dirty),
          "the design matrix changed when post-origin observations were corrupted "
          "— a feature is reading the future")

    shape_clean = fprofile.build_profile(panel, levels, up_to_date=cut)
    shape_dirty = fprofile.build_profile(poisoned, poisoned_levels, up_to_date=cut)
    same = all(
        abs(shape_clean.offset[k] - shape_dirty.offset.get(k, 1e9)) < 1e-12
        for k in shape_clean.offset
    )
    check(same, "the profile changed when post-origin observations were corrupted")

    X_pairs_clean, y_clean, _ = features.training_pairs(
        levels, calendar, up_to_date=cut)
    X_pairs_dirty, y_dirty, _ = features.training_pairs(
        poisoned_levels, calendar, up_to_date=cut)
    check(np.array_equal(X_pairs_clean, X_pairs_dirty) and np.array_equal(y_clean, y_dirty),
          "training pairs changed when post-origin observations were corrupted")

    # ── 6. training examples have the shape of the question ────────────
    _, _, meta = features.training_pairs(levels, calendar, up_to_date=ORIGIN)
    check(int(meta["horizon"].min()) >= 1,
          f"a training pair has horizon {meta['horizon'].min()}; every example must "
          "be a genuine forecast, or two features train outside their serving range")
    check(meta["date"].max() <= ORIGIN,
          "a training pair targets a date after the origin")

    # Not committed: ~300 KB, regenerable, and the only results file no figure
    # reads (see .gitignore). Absent on a fresh clone, so its absence is announced
    # rather than treated as a failure — a check that silently disappears is worse
    # than one that says it was skipped.
    if (RESULTS / "backtest_days.csv").exists():
        days = pd.read_csv(RESULTS / "backtest_days.csv")
        check((days["origin"] < days["date"]).all(),
              "a backtest row predicts a date at or before its own origin")
        computed = [data.horizon_of(o, d) for o, d in zip(days["origin"], days["date"])]
        check(list(days["horizon"]) == computed,
              "a backtest row's stored horizon disagrees with its dates")
    else:
        print(
            "  [skip] backtest_days.csv is not present, so the train/test ordering "
            "invariant was not checked. Run `python -m forecast.cli backtest` to "
            "generate it."
        )

    # ── 7. the pre-registered predictions ──────────────────────────────
    if (RESULTS / "skill.csv").exists():
        skill = pd.read_csv(RESULTS / "skill.csv")
        ridge = skill[skill["predictor"] == "ridge"]

        def row(baseline: str, band: str):
            hit = ridge[(ridge["baseline"] == baseline) & (ridge["horizon_band"] == band)]
            return hit.iloc[0] if len(hit) else None

        p1 = row("climatology", "1-7")
        check(p1 is not None and bool(p1["spans_zero"]),
              "P1 was recorded REFUTED (beats the park average at <=7 days: interval "
              f"spans zero). It now reads spans_zero={p1 is not None and p1['spans_zero']} "
              "— the article says otherwise")
        p2 = row(backtest.HEADLINE_BASELINE, "1-7")
        check(p2 is not None and p2["skill"] > 0.05 and not p2["spans_zero"],
              "P2 was recorded CONFIRMED (beats the incumbent at <=7 days with "
              f"skill > 0.05). It now reads skill={p2['skill']:.4f}, "
              f"spans_zero={p2['spans_zero']}")
        p3 = row(backtest.HEADLINE_BASELINE, "15-29")
        check(p3 is not None and not p3["spans_zero"],
              "P3 was recorded REFUTED (skill at >=15 days is NOT indistinguishable "
              "from zero against the incumbent). It now spans zero")
        # P6: the GBM must not beat the ridge at long horizon, which is why the
        # simpler model ships and serving needs no scikit-learn.
        gbm = skill[(skill["predictor"] == "gbm")
                    & (skill["baseline"] == backtest.HEADLINE_BASELINE)
                    & (skill["horizon_band"] == "15-29")]
        check(len(gbm) and gbm.iloc[0]["skill"] < p3["skill"],
              "the GBM now beats the ridge at 15-29 days; the article says the "
              "simpler model was chosen on a measurement, so re-check that claim")
    else:
        out.append(f"{RESULTS / 'skill.csv'} missing — run the backtest")

    # ── 8. the leakage demonstration must actually demonstrate leakage ──
    if (RESULTS / "leakage.csv").exists():
        leak = pd.read_csv(RESULTS / "leakage.csv").set_index("protocol")
        honest = float(leak.loc["rolling_origin", "level_r2"])
        row_r2 = float(leak.loc["random_row", "level_r2"])
        day_r2 = float(leak.loc["random_day", "level_r2"])
        check(row_r2 > day_r2 > honest,
              f"the three protocols no longer order row({row_r2:.3f}) > "
              f"day({day_r2:.3f}) > honest({honest:.3f}); the figure's whole argument "
              "is that ordering")
        inflation = float(leak.loc["random_row", "level_r2_inflation"])
        check(1.3 <= inflation <= 2.0,
              f"random-row R2 inflation is {inflation:.2f}x; the article states 1.62x "
              "and explicitly records P5 as refuted for being under 2x")
        mae_ratio = float(leak.loc["rolling_origin", "level_mae"]) / float(
            leak.loc["random_row", "level_mae"])
        check(mae_ratio > 3.0,
              f"random-row error is only {mae_ratio:.1f}x lower; the article says ~6x")
    else:
        out.append(f"{RESULTS / 'leakage.csv'} missing — run the leakage demo")

    # ── 9. the artifact ────────────────────────────────────────────────
    check("data" not in config.ARTIFACT_DIR.parts,
          "the artifact directory is under a data/ path, which .gitignore drops "
          "at any depth")
    check("data" not in config.RESULTS_DIR.parts,
          "the results directory is under a data/ path")
    try:
        forecaster = artifacts.load()
    except FileNotFoundError as exc:
        forecaster = None
        out.append(f"artifact will not load: {exc}")

    if forecaster is not None:
        manifest = forecaster.manifest
        check(list(forecaster.day_model.names) == list(manifest["features"]),
              "the stored coefficients and the stored feature list disagree")
        # A tolerance, not equality. The rollup advances every night, and requiring
        # the artifact to match it exactly makes this suite fail every morning for a
        # model that is fine. Retraining is also not free of consequences: the
        # forecast article quotes ~35 figures from the trained artifact, so a retrain
        # is a deliberate act that comes with updating the write-up. The test's job
        # is to catch an artifact going genuinely stale, not to nag daily.
        newest = data.last_observed_date(conn)
        lag = (
            pd.Timestamp(newest) - pd.Timestamp(manifest["origin"])
        ).days
        check(
            0 <= lag <= MAX_ARTIFACT_LAG_DAYS,
            f"the artifact was trained at {manifest['origin']} and the newest rollup "
            f"is {newest}, a lag of {lag} days (limit {MAX_ARTIFACT_LAG_DAYS}). "
            "Re-run `cli backtest`, `cli leakage`, `cli train`, then re-check the "
            "figures quoted in forecast/ARTICLE.md against the regenerated CSVs.",
        )
        check(1.0 < forecaster.smearing < 1.3,
              f"smearing factor is {forecaster.smearing:.4f}; it corrects a log "
              "back-transform and should sit just above 1")
        check(forecaster.manifest["profile_support"].get("park", 0.0) == 0.0,
              "the weakest profile tier (park-wide) is firing; it should never")

        # P4 was recorded REFUTED: `hours` is NOT the largest coefficient after park
        # identity — it is third, behind the park's own climatology and the shrunk
        # persistence term. Pinned so the article cannot quietly drift from this.
        dummies = {f"park_{i}" for i in range(len(config.PARK_ORDER))}
        ranked = [n for n, _ in forecaster.day_model.standardized() if n not in dummies]
        check(ranked[:3] == ["park_climatology", "persistence_shrunk", "hours"],
              "P4 was recorded REFUTED with `hours` third behind park climatology "
              f"and persistence. The ranking is now {ranked[:3]}")

        # round-trip: reload and re-predict to the last decimal
        rows = pd.DataFrame(
            [{"park_id": p, "date": "2026-10-15", "attraction_id": a, "hour": 13,
              "weekday": 2}
             for a, p in list(forecaster.profile.park_of.items())[:50]]
        )
        cal = data.calendar(conn, start="2026-10-15", end="2026-10-15")
        lv = forecaster.predict_levels(cal, forecaster.state)
        first = forecaster.predict_rows(rows, lv)["wait"].to_numpy(float)
        again = artifacts.load().predict_rows(rows, lv)["wait"].to_numpy(float)
        check(np.allclose(first, again, atol=1e-9, equal_nan=True),
              "reloading the artifact changed its predictions")
        check(np.nanmin(first) >= 0.0 and np.nanmax(first) < 300,
              f"predicted waits out of range: {np.nanmin(first):.1f}.."
              f"{np.nanmax(first):.1f} (a negative queue must be clamped)")

    # ── 10. serving must not need scikit-learn ─────────────────────────
    probe = (
        "import sys; sys.path.insert(0, %r);"
        "from forecast import artifacts, predict;"
        "p = predict.forecast_day('2026-10-15');"
        "assert p['parks'], 'no parks';"
        "print('sklearn' in sys.modules)" % str(REPO)
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    if done.returncode != 0:
        out.append(f"serving probe failed: {done.stderr.strip().splitlines()[-1:]}")
    else:
        check(done.stdout.strip() == "False",
              "scikit-learn was imported while serving a prediction; the artifact "
              "is meant to be numpy-only so a later `uv sync` cannot break the app")

    # ── 11. the webapp contract, callable outside FastAPI ──────────────
    from research_project.webapp import app as webapp

    payload = webapp.forecast_for_date("2026-10-15")
    check(len(payload["parks"]) == 7,
          f"forecast_for_date returned {len(payload['parks'])} parks, expected 7")
    # The payload's origin is the ARTIFACT's origin — what the model actually knows —
    # not the newest date in the database. Comparing it to the latter was wrong: it
    # asserted the served model is always retrained, which is a different claim and
    # not one this payload can make.
    served_origin = artifacts.load().manifest["origin"] if forecaster is None \
        else forecaster.origin
    check(payload["origin"] == served_origin,
          f"payload origin {payload['origin']} is not the trained artifact's origin "
          f"{served_origin}")
    check(payload["horizon"] == data.horizon_of(served_origin, "2026-10-15"),
          "payload horizon disagrees with its own origin and date")
    check(bool(payload["disclosure"]), "payload carries no disclosure")
    check(payload["skill"].get("available") is True,
          "payload carries no measured skill for this horizon")
    for park in payload["parks"]:
        for attraction in park["attractions"]:
            if not attraction["estimate"]:
                continue
            check(attraction["day_lo"] <= attraction["day_mean"] <= attraction["day_hi"],
                  f"{attraction['name']}: interval does not contain the point estimate")
            check(0 <= attraction["day_mean"] < 300,
                  f"{attraction['name']}: implausible predicted wait "
                  f"{attraction['day_mean']}")
            # Zero is legitimate: several walk-through attractions genuinely post
            # no wait at all. Negative is not.
            check(attraction["baseline"] is None or attraction["baseline"] >= 0,
                  f"{attraction['name']}: negative incumbent estimate")

    # ── 12. the horizon ceiling is a refusal, not an imputation ────────
    info = webapp.forecast_dates(None)
    check(info["max_date"] is not None and info["max_date"] <= "2026-10-31",
          f"forecastable dates reach {info['max_date']}, further than the schedule feed")
    try:
        fpredict.forecast_day("2026-12-01", forecaster=forecaster, conn=conn)
        out.append("a date beyond the schedule ceiling returned a forecast; park "
                   "hours are the strongest feature and must not be imputed")
    except fpredict.HorizonError:
        pass

    # ── 13. chart coverage, both directions, both articles ────────────
    js = (REPO / "research_project" / "webapp" / "static" / "charts.js").read_text()
    fjs = (REPO / "research_project" / "webapp" / "static" / "fcharts.js").read_text()
    for label, module, article, script in (
        ("route", charts, REPO / "research_project" / "ARTICLE.md", js),
        ("forecast", fcharts, REPO / "forecast" / "ARTICLE.md", fjs),
    ):
        prose = article.read_text()
        keys = set(module.everything())
        markers = set(
            line.split("chart:")[1].split(" -->")[0]
            for line in prose.splitlines() if "<!-- chart:" in line
        )
        check(markers <= keys,
              f"{label}: markers with no dataset: {sorted(markers - keys)}")
        check(keys <= markers,
              f"{label}: datasets with no marker in the prose: {sorted(keys - markers)}")
        for key in keys:
            check(f"{key}(figure, d)" in script,
                  f"{label}: no renderer for {key} in the figures script")

    # ── 14. band thresholds agree with the presentation layer ─────────
    check(config.WALK_ON_MAX == charts.WALK_ON_MAX,
          f"walk-on threshold differs: {config.WALK_ON_MAX} vs {charts.WALK_ON_MAX}")
    check(config.HEADLINER_MIN == charts.HEADLINER_MIN,
          f"headliner threshold differs: {config.HEADLINER_MIN} vs {charts.HEADLINER_MIN}")

    # ── 15. determinism ───────────────────────────────────────────────
    X, y, _ = features.training_pairs(levels, calendar, up_to_date=ORIGIN)
    cols = list(features.columns_for("shrunk"))
    labels = features.FEATURE_SETS["shrunk"]
    a = model.DayLevelRidge().fit(X[:, cols], y, labels, alpha=10.0).predict(X[:, cols])
    b = model.DayLevelRidge().fit(X[:, cols], y, labels, alpha=10.0).predict(X[:, cols])
    check(np.allclose(a, b), "refitting the ridge on identical data changed its output")

    conn.close()
    return out


def test_forecast():  # pytest entry point
    assert failures() == []


if __name__ == "__main__":
    problems = failures()
    for problem in problems:
        print("FAIL:", problem)
    print("forecast: OK" if not problems
          else f"forecast: {len(problems)} failure(s)")
    sys.exit(1 if problems else 0)
