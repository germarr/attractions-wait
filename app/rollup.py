"""Nightly rollup builder — precomputes the historical half of the heavy pages.

See docs/adr/0004-precomputed-daily-rollups.md and
docs/plans/0004-rollup-implementation.md.

The aggregation primitives here (`_park_day_agg`, `_attraction_day_aggs`) are the
single source of truth for how a finalized day is summarized. The request layer
reuses them on *today's* raw rows so the live day-bucket is computed identically
to the stored ones — the two never drift. Every aggregate is taken over
Operating-Hours-filtered readings (`stats._within_operating_hours`) in park-local
time, exactly matching the query layer.

Run nightly:  python -m app.rollup      (calls run_nightly)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

import pandas as pd
from sqlalchemy import func, text
from sqlmodel import Session, select

from app import stats
from app.db import DB_PATH, engine
from app.models import (
    Attraction,
    AttractionDaily,
    AttractionHourBaseline,
    AttractionHourly,
    Park,
    ParkCorrelation,
    ParkDaily,
)
from app.stats import (
    CROWD_BASELINE_DAYS,
    DOWNTIME_STATUSES,
    HEADLINER_N,
    PARK_TZ,
    UTC,
    _within_operating_hours,
)


# ── raw reads ──────────────────────────────────────────────────────────────

def _read_between(start_utc: str, end_utc: str) -> pd.DataFrame:
    """All readings in [start_utc, end_utc), park-local, via a read-only conn."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        df = pd.read_sql_query(
            "SELECT observed_at, attraction_id, wait_time, status FROM reading "
            "WHERE observed_at >= ? AND observed_at < ?",
            conn,
            params=[start_utc, end_utc],
        )
    finally:
        conn.close()
    if not df.empty:
        df["observed_at"] = pd.to_datetime(df["observed_at"], utc=True).dt.tz_convert(
            PARK_TZ
        )
    return df


def _local_date_bounds_utc(local_date: str) -> tuple[str, str]:
    """(start, end) UTC-ISO for one park-local calendar date [00:00, next 00:00)."""
    d = date.fromisoformat(local_date)
    d1 = d + timedelta(days=1)
    start = datetime(d.year, d.month, d.day, tzinfo=PARK_TZ).astimezone(UTC).isoformat()
    end = datetime(d1.year, d1.month, d1.day, tzinfo=PARK_TZ).astimezone(UTC).isoformat()
    return start, end


# ── aggregation primitives (shared with the request-time today-merge) ──────

def _park_day_agg(sub: pd.DataFrame) -> dict:
    """Park Average / Headliner / Downtime figures from a park's Operating-Hours-
    filtered frame for one day. Sums (not finished means) are stored so windows
    combine by Σsum/Σn; mean/median/std are the per-day chart buckets."""
    n_readings = int(len(sub))
    n_down = int(sub["status"].isin(DOWNTIME_STATUSES).sum())
    waits = sub.dropna(subset=["wait_time"])
    out = {
        "n_readings": n_readings,
        "n_down": n_down,
        "n_min": 0,
        "sum_minavg": 0.0,
        "sum_top5": 0.0,
        "n_min_top5": 0,
        "mean_wait": None,
        "median_wait": None,
        "std_wait": None,
    }
    if waits.empty:
        return out
    per_min = waits.groupby("observed_at")["wait_time"].mean()  # Park Average / minute
    ranked = waits.sort_values("wait_time", ascending=False)
    ranked = ranked.assign(_rk=ranked.groupby("observed_at").cumcount())
    top = ranked[ranked["_rk"] < HEADLINER_N]
    per_min_top = top.groupby("observed_at")["wait_time"].mean()
    n_min = int(per_min.size)
    out.update(
        n_min=n_min,
        sum_minavg=float(per_min.sum()),
        sum_top5=float(per_min_top.sum()),
        n_min_top5=int(per_min_top.size),
        mean_wait=round(float(per_min.mean()), 4),
        median_wait=round(float(per_min.median()), 4),
        std_wait=round(float(per_min.std(ddof=1)), 4) if n_min > 1 else 0.0,
    )
    return out


def _attraction_day_aggs(sub: pd.DataFrame) -> tuple[dict, list[tuple]]:
    """Per-attraction daily dict {aid: {...}} and hourly rows
    [(aid, hour, n_wait, sum_wait)] from a park's filtered day frame."""
    daily: dict = {}
    hourly: list[tuple] = []
    for aid, g in sub.groupby("attraction_id"):
        n_readings = int(len(g))
        n_down = int(g["status"].isin(DOWNTIME_STATUSES).sum())
        wser = g.dropna(subset=["wait_time"])
        n_wait = int(len(wser))
        rec = {
            "n_readings": n_readings,
            "n_down": n_down,
            "n_wait": n_wait,
            "sum_wait": float(wser["wait_time"].sum()) if n_wait else 0.0,
            "mean_wait": None,
            "median_wait": None,
            "std_wait": None,
        }
        if n_wait:
            vals = wser["wait_time"]
            rec["mean_wait"] = round(float(vals.mean()), 4)
            rec["median_wait"] = round(float(vals.median()), 4)
            rec["std_wait"] = round(float(vals.std(ddof=1)), 4) if n_wait > 1 else 0.0
            hg = wser.groupby(wser["observed_at"].dt.hour)["wait_time"].agg(
                ["count", "sum"]
            )
            for hour, r in hg.iterrows():
                hourly.append((aid, int(hour), int(r["count"]), float(r["sum"])))
        daily[aid] = rec
    return daily, hourly


# ── day builder ────────────────────────────────────────────────────────────

def build_day(
    session: Session,
    local_date: str,
    aid_to_park: dict[str, str],
    park_ids: list[str],
    built_at: str,
) -> None:
    """Upsert AttractionDaily/AttractionHourly/ParkDaily for one finalized date.

    Idempotent: deletes the date's rows first, then re-inserts. Reuses the exact
    Operating-Hours filter of the query layer, per park.
    """
    start_utc, end_utc = _local_date_bounds_utc(local_date)
    df = _read_between(start_utc, end_utc)

    # Clear this date so re-runs (trailing recompute / backfill) are idempotent.
    for tbl in ("attractiondaily", "attractionhourly", "parkdaily"):
        session.execute(text(f"DELETE FROM {tbl} WHERE date = :d"), {"d": local_date})

    if df.empty:
        return
    df["park_id"] = df["attraction_id"].map(aid_to_park)

    for park_id in park_ids:
        raw_sub = df[df["park_id"] == park_id]
        if raw_sub.empty:
            continue
        sub = _within_operating_hours(raw_sub, park_id)
        if sub.empty:
            continue

        pa = _park_day_agg(sub)
        session.add(
            ParkDaily(park_id=park_id, date=local_date, built_at=built_at, **pa)
        )

        daily, hourly = _attraction_day_aggs(sub)
        for aid, rec in daily.items():
            session.add(
                AttractionDaily(
                    attraction_id=aid, date=local_date, built_at=built_at, **rec
                )
            )
        for aid, hour, n_wait, sum_wait in hourly:
            session.add(
                AttractionHourly(
                    attraction_id=aid,
                    date=local_date,
                    hour=hour,
                    n_wait=n_wait,
                    sum_wait=sum_wait,
                    built_at=built_at,
                )
            )


# ── baselines (Crowd Index) ────────────────────────────────────────────────

def rebuild_baselines(session: Session, built_at: str, now_local: datetime) -> None:
    """Full rebuild of AttractionHourBaseline: hour-of-day sum/n over the trailing
    CROWD_BASELINE_DAYS of finalized days. Today is folded in at request time."""
    cutoff = (now_local.date() - timedelta(days=CROWD_BASELINE_DAYS)).isoformat()
    rows = session.exec(
        select(
            AttractionHourly.attraction_id,
            AttractionHourly.hour,
            func.sum(AttractionHourly.n_wait),
            func.sum(AttractionHourly.sum_wait),
        )
        .where(AttractionHourly.date >= cutoff)
        .group_by(AttractionHourly.attraction_id, AttractionHourly.hour)
    ).all()
    session.execute(text("DELETE FROM attractionhourbaseline"))
    for aid, hour, n, s in rows:
        session.add(
            AttractionHourBaseline(
                attraction_id=aid,
                hour=int(hour),
                sum_wait=float(s),
                n=int(n),
                built_at=built_at,
            )
        )


# ── weather correlation (per park, 30-day hourly) ──────────────────────────

def rebuild_correlations(
    session: Session,
    built_at: str,
    now_local: datetime,
    park_ids: list[str],
    aid_to_park: dict[str, str],
) -> None:
    """Full rebuild of ParkCorrelation. Reads ~30 days of raw per park — a nightly
    cost, off the request path — and reuses stats._weather_correlation verbatim."""
    month_start = now_local.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=29)
    start_utc = month_start.astimezone(UTC).isoformat()
    end_utc = datetime.now(tz=UTC).isoformat()
    df_all = _read_between(start_utc, end_utc)
    if not df_all.empty:
        df_all["park_id"] = df_all["attraction_id"].map(aid_to_park)

    session.execute(text("DELETE FROM parkcorrelation"))
    for park_id in park_ids:
        corr = {"labels": stats.CORR_LABELS, "matrix": None, "n": 0}
        if not df_all.empty:
            raw_sub = df_all[df_all["park_id"] == park_id]
            sub = _within_operating_hours(raw_sub, park_id)
            if not sub.empty:
                sub = sub.copy()
                sub["down"] = sub["status"].isin(DOWNTIME_STATUSES)
                corr = stats._weather_correlation(park_id, sub, month_start)
        session.add(
            ParkCorrelation(
                park_id=park_id,
                labels_json=json.dumps(corr["labels"]),
                matrix_json=json.dumps(corr["matrix"]) if corr["matrix"] else None,
                n=int(corr["n"]),
                built_at=built_at,
            )
        )


# ── entry points ───────────────────────────────────────────────────────────

def run_nightly(trailing_days: int = 7) -> None:
    """Finalize the last `trailing_days` days, then rebuild baselines +
    correlations. The trailing window re-rolls recent days so late /schedule
    fetches and weather backfills are absorbed — it must be wide enough to cover
    how long a day's Operating Hours can still change after the fact (the
    themeparks /schedule feed refreshes a rolling window of recent dates). Never
    rolls up the in-progress local day (the request layer's live tail owns it, and
    also self-heals any day this job hasn't finalized yet). See docs/adr/0004."""
    built_at = datetime.now(tz=UTC).isoformat()
    now_local = stats._now_local()
    with Session(engine) as session:
        aid_to_park = {a.id: a.park_id for a in session.exec(select(Attraction)).all()}
        park_ids = [p.id for p in session.exec(select(Park)).all()]
        for i in range(1, trailing_days + 1):
            d = (now_local.date() - timedelta(days=i)).isoformat()
            build_day(session, d, aid_to_park, park_ids, built_at)
        rebuild_baselines(session, built_at, now_local)
        rebuild_correlations(session, built_at, now_local, park_ids, aid_to_park)
        session.commit()


if __name__ == "__main__":
    run_nightly()
