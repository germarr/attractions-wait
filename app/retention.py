"""Raw-reading retention — prune raw the rollups have already absorbed.

See docs/adr/0005-raw-reading-retention.md. The nightly rollup (ADR-0004) is the
permanent aggregated store; nothing in the query layer reads raw `reading` rows
older than ~30 park-local days (the 30-day weather correlation and 28-day
Day-page overlay are the deepest, plus the nightly's 7-day re-roll) — except YoY,
which isn't live yet and, when it ships, must read `AttractionHourly` rather than
raw. This job deletes raw `reading` and `weatherreading` rows older than
RETENTION_DAYS so raw growth is bounded while every active dashboard metric stays
intact.

Run daily AFTER the nightly rollup:  python -m app.retention
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, text
from sqlmodel import Session, select

from app.db import engine
from app.models import AttractionDaily
from app.stats import _local_midnight_utc, _now_local

# Keep this many park-local days of raw readings. Must stay comfortably above
# every raw-read horizon in the query/rollup layer: the nightly 7-day re-roll,
# the 28-day Day-page overlay, and the 30-day weather correlation. 35 leaves a
# ~5-day margin. Going below this requires re-pointing those reads to the rollups
# first (option B in ADR-0005).
RETENTION_DAYS = 35

# Refuse to prune unless the rollup is finalized at least this recent. A stale or
# never-run nightly means the aggregated store may not yet cover the days we're
# about to delete, so we skip rather than lose raw data irrecoverably.
MAX_ROLLUP_LAG_DAYS = 3


def run_prune(retention_days: int = RETENTION_DAYS) -> dict:
    """Delete raw readings/weather older than `retention_days` park-local days.

    Fail-safe: no-op (and reports why) if the rollup is missing or stale, so a
    broken nightly can never cause raw to be deleted before it was aggregated.
    """
    now_local = _now_local()
    cutoff_date = (now_local.date() - timedelta(days=retention_days)).isoformat()
    cutoff_utc = _local_midnight_utc(cutoff_date)

    with Session(engine) as session:
        latest_rolled = session.exec(select(func.max(AttractionDaily.date))).one()
        if latest_rolled is None:
            return {"pruned": False, "reason": "no rollup rows — refusing to prune"}
        lag = (now_local.date() - date.fromisoformat(latest_rolled)).days
        if lag > MAX_ROLLUP_LAG_DAYS:
            return {
                "pruned": False,
                "reason": (
                    f"rollup stale (latest {latest_rolled}, lag {lag}d "
                    f"> {MAX_ROLLUP_LAG_DAYS}) — refusing to prune"
                ),
            }

        readings = session.execute(
            text("DELETE FROM reading WHERE observed_at < :c"), {"c": cutoff_utc}
        ).rowcount
        weather = session.execute(
            text("DELETE FROM weatherreading WHERE observed_at < :c"), {"c": cutoff_utc}
        ).rowcount
        session.commit()

    # A large delete leaves free pages the collector reuses (the file plateaus at
    # steady state; no VACUUM needed). Just keep the WAL from ballooning.
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")

    return {
        "pruned": True,
        "cutoff": cutoff_utc,
        "deleted_readings": readings,
        "deleted_weather": weather,
    }


if __name__ == "__main__":
    from datetime import datetime, timezone

    result = run_prune()
    print(f"{datetime.now(tz=timezone.utc).isoformat()} retention: {result}")
