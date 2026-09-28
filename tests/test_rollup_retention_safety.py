"""The nightly rollup must never destroy history it cannot rebuild.

Raw readings are pruned after 35 days (ADR-0005); rollups are kept forever
(ADR-0004). For any date older than the retention window the rollup is therefore
the **only** surviving copy, and raw can never regenerate it.

`build_day` clears a date before re-inserting so re-runs are idempotent. If that
delete runs before the "no raw data" check, calling it for a pruned date deletes
the rollup and returns without re-inserting — months of irrecoverable history
gone, silently. The defaults hide it (`run_nightly` re-rolls 7 days, well inside
the 36-day raw window), so nothing fails until someone widens the window or
writes a backfill that ranges over rollups instead of raw.

This runs against a temporary database, not the live one.

    .venv/bin/python tests/test_rollup_retention_safety.py

Non-zero exit if anything diverges.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

from app import rollup, stats  # noqa: E402
from app.models import (  # noqa: E402
    Attraction,
    AttractionDaily,
    AttractionHourly,
    Park,
    ParkSchedule,
    Reading,
)

PARK_ID = "test-park"
ATTRACTION_ID = "test-attraction"
PRUNED_DATE = "2026-06-24"  # old enough that raw would have been pruned
LIVE_DATE = "2026-09-20"  # inside the raw retention window


def _seed(engine) -> None:
    """A park, one attraction, a schedule, and raw readings for LIVE_DATE only."""
    with Session(engine) as session:
        session.add(
            Park(id=PARK_ID, name="Test Park", latitude=28.4, longitude=-81.5)
        )
        session.add(
            Attraction(
                id=ATTRACTION_ID,
                name="Test Ride",
                park_id=PARK_ID,
                last_seen=f"{LIVE_DATE}T12:00:00+00:00",
            )
        )
        for date in (PRUNED_DATE, LIVE_DATE):
            session.add(
                ParkSchedule(
                    park_id=PARK_ID,
                    date=date,
                    type="OPERATING",
                    opening_time=f"{date}T09:00:00-04:00",
                    closing_time=f"{date}T22:00:00-04:00",
                    fetched_at=f"{date}T06:00:00+00:00",
                )
            )

        # A surviving rollup for the pruned date — the only copy of that history.
        session.add(
            AttractionHourly(
                attraction_id=ATTRACTION_ID,
                date=PRUNED_DATE,
                hour=13,
                n_wait=12,
                sum_wait=360.0,
                built_at="2026-06-25T06:15:00+00:00",
            )
        )
        session.add(
            AttractionDaily(
                attraction_id=ATTRACTION_ID,
                date=PRUNED_DATE,
                n_readings=12,
                n_down=0,
                n_wait=12,
                sum_wait=360.0,
                mean_wait=30.0,
                median_wait=30.0,
                std_wait=0.0,
                built_at="2026-06-25T06:15:00+00:00",
            )
        )

        # Raw readings only for LIVE_DATE, mimicking a pruned database.
        noon = datetime(2026, 9, 20, 16, 0, tzinfo=timezone.utc)  # 12:00 park-local
        for minute in range(0, 60, 5):
            session.add(
                Reading(
                    attraction_id=ATTRACTION_ID,
                    observed_at=(noon + timedelta(minutes=minute)).isoformat(),
                    wait_time=25,
                    status="OPERATING",
                )
            )
        session.commit()


def failures() -> list[str]:
    out: list[str] = []

    def check(ok: bool, msg: str) -> None:
        if not ok:
            out.append(msg)

    with tempfile.TemporaryDirectory() as workspace:
        path = Path(workspace) / "test.db"
        engine = create_engine(f"sqlite:///{path}")
        SQLModel.metadata.create_all(engine)
        _seed(engine)

        # Point the rollup and query layers at the temporary database.
        original = (rollup.DB_PATH, rollup.engine, stats.DB_PATH, stats.engine)
        rollup.DB_PATH, rollup.engine = path, engine
        stats.DB_PATH, stats.engine = path, engine
        try:
            aid_to_park = {ATTRACTION_ID: PARK_ID}
            built_at = "2026-09-26T06:15:00+00:00"

            with Session(engine) as session:
                # 1. THE INVARIANT. Re-rolling a date whose raw has been pruned
                #    must leave the stored rollup untouched.
                rollup.build_day(session, PRUNED_DATE, aid_to_park, [PARK_ID], built_at)
                session.commit()

            with Session(engine) as session:
                hourly = session.execute(
                    text("SELECT count(*) FROM attractionhourly WHERE date = :d"),
                    {"d": PRUNED_DATE},
                ).scalar_one()
                daily = session.execute(
                    text("SELECT count(*) FROM attractiondaily WHERE date = :d"),
                    {"d": PRUNED_DATE},
                ).scalar_one()
                total = session.execute(
                    text("SELECT sum_wait FROM attractionhourly WHERE date = :d"),
                    {"d": PRUNED_DATE},
                ).scalar_one_or_none()

            check(
                hourly == 1,
                f"re-rolling a pruned date destroyed its hourly rollup "
                f"({hourly} rows left, expected 1) — history is unrecoverable",
            )
            check(
                daily == 1,
                f"re-rolling a pruned date destroyed its daily rollup "
                f"({daily} rows left, expected 1)",
            )
            check(
                total == 360.0,
                f"the surviving rollup was altered: sum_wait {total}, expected 360.0",
            )

            # 2. The idempotency the delete exists for must still hold: a date
            #    that DOES have raw data is rebuilt, not duplicated.
            with Session(engine) as session:
                rollup.build_day(session, LIVE_DATE, aid_to_park, [PARK_ID], built_at)
                session.commit()
            with Session(engine) as session:
                first = session.execute(
                    text("SELECT count(*) FROM attractionhourly WHERE date = :d"),
                    {"d": LIVE_DATE},
                ).scalar_one()
                rollup.build_day(session, LIVE_DATE, aid_to_park, [PARK_ID], built_at)
                session.commit()
            with Session(engine) as session:
                second = session.execute(
                    text("SELECT count(*) FROM attractionhourly WHERE date = :d"),
                    {"d": LIVE_DATE},
                ).scalar_one()

            check(first > 0, "a date with raw readings produced no rollup rows")
            check(
                first == second,
                f"build_day is not idempotent: {first} rows became {second} on a "
                "second run — the delete-then-reinsert has stopped working",
            )

            # 3. The wide-window scenario that motivated this, end to end: a
            #    re-roll spanning both dates must rebuild one and preserve the
            #    other.
            with Session(engine) as session:
                for date in (PRUNED_DATE, LIVE_DATE):
                    rollup.build_day(session, date, aid_to_park, [PARK_ID], built_at)
                session.commit()
            with Session(engine) as session:
                survived = session.execute(
                    text("SELECT count(*) FROM attractionhourly WHERE date = :d"),
                    {"d": PRUNED_DATE},
                ).scalar_one()
            check(
                survived == 1,
                "a wide re-roll spanning pruned and live dates destroyed the "
                "pruned date's history",
            )
        finally:
            rollup.DB_PATH, rollup.engine, stats.DB_PATH, stats.engine = original

    return out


def test_rollup_retention_safety():  # pytest entry point
    assert failures() == []


if __name__ == "__main__":
    problems = failures()
    for p in problems:
        print("FAIL:", p)
    print(
        "rollup retention safety: OK"
        if not problems
        else f"rollup retention safety: {len(problems)} failure(s)"
    )
    sys.exit(1 if problems else 0)
