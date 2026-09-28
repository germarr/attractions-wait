"""Command line entry point for the wait-time forecaster.

    python -m forecast.cli dates
    python -m forecast.cli backtest --out forecast/results
    python -m forecast.cli leakage  --out forecast/results
    python -m forecast.cli train    --out forecast/artifacts
    python -m forecast.cli predict --date 2026-10-15 --explain
    python -m forecast.cli predict --date 2026-10-15 --park "Magic Kingdom Park"

A thin argparse shell in the style of `research_project/cli.py` — all the work
lives in the modules. `backtest` must run before `train`, because the residual
quantiles the intervals are drawn from and the skill numbers the app reports are
both measured by the backtest, not asserted by the model.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from forecast import artifacts, backtest, config, data, model as fmodel, predict
from research_project import db as rdb


def _load(conn):
    panel = data.load_panel(conn)
    levels = data.park_day_levels(panel)
    horizon_end = max(data.forecastable_dates(conn) or [max(panel["date"])])
    calendar = data.calendar(conn, start=config.HISTORY_START, end=horizon_end)
    return panel, levels, calendar


def cmd_dates(args) -> int:
    conn = rdb.connect()
    try:
        info = predict.available_dates(conn, not_before=args.not_before)
    except FileNotFoundError as exc:
        print(f"[dates] {exc}")
        conn.close()
        return 1
    conn.close()
    print(f"origin {info['origin']}  ->  {info['n']} forecastable dates "
          f"(to {info['max_date']})")
    for row in info["dates"]:
        print(f"  {row['date']}  {row['weekday']:<9} h+{row['horizon']:<3} "
              f"{row['parks']} parks")
    print(f"\n{info['limit_reason']}")
    return 0


def cmd_backtest(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    conn = rdb.connect()
    panel, levels, calendar = _load(conn)
    conn.close()

    rows, days = backtest.rolling_origin(
        panel, levels, calendar, max_horizon=args.max_horizon,
        stride=args.stride, fit_gbm=not args.no_gbm, verbose=args.verbose,
    )
    residuals = backtest.residual_quantiles(rows, "ridge")
    skill = backtest.skill_table(rows, predictors=("ridge", "gbm"), draws=args.draws)
    coverage = backtest.coverage(rows, residuals, "ridge")
    honest = backtest.honest_coverage(rows, "ridge")

    # The row-level frame is ~300k rows; the committed artifact is the aggregate,
    # which is what every chart and every sentence in the article reads.
    backtest.summary(rows, group="horizon_band").to_csv(
        out / "error_by_horizon.csv", index=False)
    backtest.summary(rows, group="wait_band").to_csv(
        out / "error_by_band.csv", index=False)
    backtest.summary(rows, group="park_id").to_csv(
        out / "error_by_park.csv", index=False)
    backtest.summary(days, truth="level",
                     predictors=("ridge", "gbm", "climatology", "persistence_7"),
                     group="horizon_band").to_csv(
        out / "error_by_horizon_level.csv", index=False)
    skill.to_csv(out / "skill.csv", index=False)
    residuals.to_csv(out / "residuals.csv", index=False)
    coverage.to_csv(out / "coverage.csv", index=False)
    honest.to_csv(out / "coverage_honest.csv", index=False)
    # Rounded: full float repr made this 385 KB of 16-digit noise for a diagnostic
    # nobody reads to more than a few decimals. Not committed (see .gitignore).
    day_floats = days.select_dtypes("float").columns
    days.assign(**{c: days[c].round(6) for c in day_floats}).to_csv(
        out / "backtest_days.csv", index=False
    )
    _horizon_decay(levels).to_csv(out / "horizon_decay.csv", index=False)
    _calibration(rows).to_csv(out / "calibration.csv", index=False)

    print(f"[backtest] {len(rows):,} row predictions over "
          f"{rows['origin'].nunique()} origins -> {out}")
    headline = skill[(skill["predictor"] == "ridge")
                     & (skill["baseline"] == backtest.HEADLINE_BASELINE)]
    for row in headline.itertuples():
        flag = "spans zero" if row.spans_zero else "excludes zero"
        print(f"  skill vs {backtest.HEADLINE_BASELINE} at h {row.horizon_band}: "
              f"{row.skill:+.4f}  90% CI [{row.lo:+.4f}, {row.hi:+.4f}]  "
              f"n={row.n_rows:,} over {row.n_origins} origins ({flag})")
    return 0


def _horizon_decay(levels: pd.DataFrame, *, max_horizon: int = 28) -> pd.DataFrame:
    """Does last week's crowd level still tell you anything h days later?

    Measured on park-DEMEANED levels. Pooling the seven parks raw would answer a
    different and uninteresting question: Epic Universe averages 43.8 minutes and
    Magic Kingdom 20.8, so a raw correlation sits near 0.7 at every horizon purely
    because busy parks stay busy. Subtracting each park's own mean leaves the
    day-to-day deviation, which is the only part a forecast has to supply.
    """
    import numpy as np

    from forecast import features

    dates = sorted(levels["date"].unique())
    park_mean = levels.groupby("park_id")["level"].mean().to_dict()
    rows = []
    for horizon in range(1, max_horizon + 1):
        pairs = []
        for i, origin in enumerate(dates):
            target_index = i + horizon
            if target_index >= len(dates):
                break
            target = dates[target_index]
            if data.horizon_of(origin, target) != horizon:
                continue
            state = features.origin_state(levels, origin=origin)
            for row in levels[levels["date"] == target].itertuples():
                trailing = state["trailing"][7].get(row.park_id)
                if trailing is None:
                    continue
                base = park_mean[row.park_id]
                pairs.append((trailing - base, row.level - base))
        if len(pairs) > 8:
            a, b = np.array(pairs).T
            rows.append({"horizon": horizon,
                         "corr": float(np.corrcoef(a, b)[0, 1]),
                         "n": len(pairs)})
    return pd.DataFrame(rows)


def _calibration(rows: pd.DataFrame, bins: int = 12) -> pd.DataFrame:
    """Predicted against actual, binned, per wait band."""
    import numpy as np

    work = rows[["ridge", "actual", "wait_band"]].dropna().copy()
    work["bucket"] = pd.qcut(work["ridge"], bins, duplicates="drop")
    grouped = work.groupby(["wait_band", "bucket"], observed=True).agg(
        predicted=("ridge", "mean"), actual=("actual", "mean"), n=("actual", "size")
    ).reset_index()
    grouped["bucket"] = grouped["bucket"].astype(str)
    return grouped


def cmd_leakage(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    conn = rdb.connect()
    panel, levels, calendar = _load(conn)
    conn.close()
    frame = backtest.leakage_comparison(panel, levels, calendar)
    frame.to_csv(out / "leakage.csv", index=False)
    print(f"[leakage] -> {out / 'leakage.csv'}")
    for row in frame.itertuples():
        print(f"  {row.protocol:<15} level R2 {row.level_r2:.4f} "
              f"(x{row.level_r2_inflation:.2f} the honest figure)  "
              f"MAE {row.level_mae:.4f}  n={row.n:,}")
    return 0


def cmd_train(args) -> int:
    results = Path(args.results)
    skill_path = results / "skill.csv"
    resid_path = results / "residuals.csv"
    if not resid_path.exists():
        print(f"[train] {resid_path} is missing — run `backtest` first: the "
              "intervals are measured, not assumed.")
        return 1

    conn = rdb.connect()
    panel, levels, calendar = _load(conn)
    origin = args.origin or data.last_observed_date(conn)
    conn.close()

    residuals = pd.read_csv(resid_path)
    skill = {}
    if skill_path.exists():
        table = pd.read_csv(skill_path)
        table = table[(table["predictor"] == "ridge")
                      & (table["baseline"] == backtest.HEADLINE_BASELINE)]
        skill = {
            row.horizon_band: {
                "value": round(float(row.skill), 4),
                "lo": round(float(row.lo), 4),
                "hi": round(float(row.hi), 4),
                "n_rows": int(row.n_rows),
                "n_origins": int(row.n_origins),
                "n_park_days": int(row.n_park_days),
                "spans_zero": bool(row.spans_zero),
                "baseline": backtest.HEADLINE_BASELINE,
            }
            for row in table.itertuples()
        }

    forecaster = fmodel.fit_forecaster(
        panel, levels, calendar, origin=origin, residuals=residuals, skill=skill,
    )
    path = artifacts.save(forecaster, args.out)
    print(f"[train] origin {origin} -> {path}")
    print(f"  feature set    {forecaster.manifest['feature_set']} "
          f"({len(forecaster.manifest['features'])} columns)")
    print(f"  ridge alpha    {forecaster.manifest['ridge_alpha']}  "
          f"tau {forecaster.manifest['shrink_tau_days']}")
    print(f"  training pairs {forecaster.manifest['train_pairs']:,}")
    print(f"  smearing       {forecaster.smearing:.4f}")
    print("  top standardized coefficients:")
    for name, value in forecaster.day_model.standardized()[:6]:
        print(f"    {name:<22} {value:+.4f}")
    return 0


def cmd_predict(args) -> int:
    conn = rdb.connect()
    try:
        forecaster = artifacts.load()
    except FileNotFoundError as exc:
        print(f"[predict] {exc}")
        conn.close()
        return 1
    parks = None
    if args.park:
        matched = [p for p, name in config.PARKS.items()
                   if args.park.lower() in name.lower()]
        if not matched:
            print(f"[predict] no park matches {args.park!r}. Known parks:")
            for name in config.PARKS.values():
                print(f"    {name}")
            conn.close()
            return 1
        parks = tuple(matched)
    try:
        payload = predict.forecast_day(args.date, parks=parks,
                                       forecaster=forecaster, conn=conn)
    except predict.HorizonError as exc:
        print(f"[predict] {exc}")
        return 1
    finally:
        conn.close()

    if args.json:
        print(json.dumps(payload, indent=2))
        return 0

    skill = payload["skill"]
    print(f"{payload['date']} ({payload['weekday']})  "
          f"origin {payload['origin']}  horizon +{payload['horizon']} days")
    if skill.get("available"):
        flag = "spans zero" if skill["spans_zero"] else "excludes zero"
        print(f"  measured skill vs {skill['baseline']} at h {skill['horizon_band']}: "
              f"{skill['value']:+.3f} (90% CI {skill['lo']:+.3f}..{skill['hi']:+.3f}, "
              f"n={skill['n_rows']:,} over {skill['n_origins']} origins, {flag})")
    else:
        print("  measured skill: not available for this horizon band")

    for park in payload["parks"]:
        party = "  party night" if park["party_night"] else ""
        print(f"\n{park['park']}   {park['open']}-{park['close']} "
              f"({park['hours']}h){party}")
        print(f"  predicted day mean {park['day_mean']} min "
              f"(incumbent {park['baseline_day_mean']})")
        if park["n_no_estimate"]:
            print(f"  {park['n_no_estimate']} attraction(s) have no estimate")
        shown = [a for a in park["attractions"] if a["estimate"]][: args.top]
        print(f"  {'attraction':<44} {'mean':>6} {'80% band':>14} {'peak':>7} "
              f"{'base':>6}  tier")
        for a in shown:
            print(f"  {a['name'][:43]:<44} {a['day_mean']:>6.1f} "
                  f"{a['day_lo']:>6.1f}-{a['day_hi']:<7.1f} "
                  f"{a['peak_hour']:>2}:00{'':>2} "
                  f"{(a['baseline'] if a['baseline'] is not None else float('nan')):>6.1f}"
                  f"  {a['tier']}")

    if args.explain:
        print("\nmodel")
        manifest = artifacts.load().manifest
        print(f"  feature set {manifest['feature_set']}, "
              f"alpha {manifest['ridge_alpha']}, tau {manifest['shrink_tau_days']}")
        print(f"  trained on {manifest['n_park_days']} park-days "
              f"{manifest['train_first_date']}..{manifest['train_last_date']}")
        print(f"  smearing {manifest['smearing']:.4f}  "
              f"(log back-transform correction)")
        print("  evidence tiers in the shape:")
        for tier, share in manifest["profile_support"].items():
            if share:
                print(f"    {tier:<18} {share:6.1%}")
        print("\nassumptions (selected or invented, none of them measured outcomes)")
        for name, value, source in config.ForecastAssumptions(
            **manifest["assumptions"]
        ).provenance():
            print(f"  {name:<20} {str(value):<12} {source}")
        print(f"\n{payload['disclosure']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m forecast.cli",
        description="Predict attraction wait times for a future date.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    dates = sub.add_parser("dates", help="list forecastable dates")
    dates.add_argument("--not-before", default=None)
    dates.set_defaults(func=cmd_dates)

    bt = sub.add_parser("backtest", help="rolling-origin evaluation, writes CSVs")
    bt.add_argument("--out", default="forecast/results")
    bt.add_argument("--max-horizon", type=int, default=config.MAX_HORIZON)
    bt.add_argument("--stride", type=int, default=config.ORIGIN_STRIDE)
    bt.add_argument("--draws", type=int, default=2000)
    bt.add_argument("--no-gbm", action="store_true")
    bt.add_argument("--verbose", action="store_true")
    bt.set_defaults(func=cmd_backtest)

    leak = sub.add_parser("leakage", help="the three-split leakage demonstration")
    leak.add_argument("--out", default="forecast/results")
    leak.set_defaults(func=cmd_leakage)

    train = sub.add_parser("train", help="fit the shipped model and save it")
    train.add_argument("--out", default=str(config.ARTIFACT_DIR))
    train.add_argument("--results", default="forecast/results")
    train.add_argument("--origin", default=None)
    train.set_defaults(func=cmd_train)

    pred = sub.add_parser("predict", help="predict one date")
    pred.add_argument("--date", required=True)
    pred.add_argument("--park", default=None)
    pred.add_argument("--top", type=int, default=12)
    pred.add_argument("--json", action="store_true")
    pred.add_argument("--explain", action="store_true")
    pred.set_defaults(func=cmd_predict)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
