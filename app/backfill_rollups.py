"""One-time backfill of the daily rollups over all existing history.

Iterates every park-local date from the earliest Reading up to yesterday, calls
the same `rollup.build_day` the nightly cron uses, then rebuilds baselines and
correlations. Idempotent — safe to re-run.

    python -m app.backfill_rollups
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from app import stats
from app.db import engine
from app.models import Attraction, Park, Reading
from app.rollup import build_day, rebuild_baselines, rebuild_correlations
from app.stats import PARK_TZ, UTC


def _local_date_range() -> list[str]:
    """Every park-local date from the earliest Reading through yesterday."""
    with Session(engine) as session:
        earliest = session.exec(
            select(Reading.observed_at).order_by(Reading.observed_at.asc())
        ).first()
    if not earliest:
        return []
    first = (
        datetime.fromisoformat(earliest).astimezone(PARK_TZ).date()
    )
    yesterday = stats._now_local().date() - timedelta(days=1)
    days, d = [], first
    while d <= yesterday:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def main() -> None:
    built_at = datetime.now(tz=UTC).isoformat()
    now_local = stats._now_local()
    days = _local_date_range()
    print(f"backfilling {len(days)} day(s): {days[0]} … {days[-1]}" if days else "no data")
    with Session(engine) as session:
        aid_to_park = {a.id: a.park_id for a in session.exec(select(Attraction)).all()}
        park_ids = [p.id for p in session.exec(select(Park)).all()]
        for d in days:
            build_day(session, d, aid_to_park, park_ids, built_at)
            session.commit()
            print(f"  {d} ✓")
        rebuild_baselines(session, built_at, now_local)
        rebuild_correlations(session, built_at, now_local, park_ids, aid_to_park)
        session.commit()
        print("baselines + correlations rebuilt")


if __name__ == "__main__":
    main()
