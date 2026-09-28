"""Showtimes are captured, idempotent, and never pruned.

A showtime has no rollup and no second copy: the feed publishes performances for
today only, and `/entity/{show_id}/schedule` returns nothing at all, so a row not
captured on the day it ran is unrecoverable (ADR-0010). `app/retention.py` prunes
exactly two tables and this asserts that `showtime` is not quietly added to them —
the failure mode being a planner whose record of when the fireworks start silently
shrinks to the last 35 days.

Runs against a temporary database, not the live one, except for the final group
which checks the real capture actually landed.

    .venv/bin/python tests/test_showtimes.py

Non-zero exit if anything diverges.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlmodel import Session, SQLModel, create_engine, select  # noqa: E402

from app import collector, retention  # noqa: E402
from app.models import (  # noqa: E402
    Attraction,
    AttractionDaily,
    Park,
    Reading,
    Show,
    ShowTime,
)
from research_project import config, db as rdb, shows  # noqa: E402

PARK_ID = "test-park"
SHOW_ID = "test-show"
OLD_DATE = "2026-05-01"  # far outside any retention window


def _live_entity(date: str) -> dict:
    """A SHOW entry shaped like the feed's, with the three kinds it publishes."""
    return {
        "id": SHOW_ID,
        "name": "Test Fireworks",
        "entityType": "SHOW",
        "parkId": PARK_ID,
        "status": "OPERATING",
        "showtimes": [
            {
                "type": "Performance Time",
                "startTime": f"{date}T21:30:00-04:00",
                "endTime": f"{date}T21:48:00-04:00",
            },
            {
                "type": "Operating",
                "startTime": f"{date}T09:00:00-04:00",
                "endTime": f"{date}T22:00:00-04:00",
            },
        ],
    }


def failures() -> list[str]:
    out: list[str] = []

    def check(ok: bool, msg: str) -> None:
        if not ok:
            out.append(msg)

    # ── the temp-database half ───────────────────────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.db"
        engine = create_engine(f"sqlite:///{path}")
        SQLModel.metadata.create_all(engine)

        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        with Session(engine) as session:
            session.add(Park(id=PARK_ID, name="Test Park", latitude=28.4, longitude=-81.5))
            session.add(
                Show(
                    id=SHOW_ID,
                    name="Test Fireworks",
                    park_id=PARK_ID,
                    last_seen=now.isoformat(),
                    latitude=28.42,
                    longitude=-81.58,
                )
            )
            # An attraction with a rollup, so retention is willing to prune at all.
            session.add(
                Attraction(
                    id="a1", name="Test Ride", park_id=PARK_ID,
                    last_seen=now.isoformat(),
                )
            )
            session.add(
                AttractionDaily(
                    attraction_id="a1", date=today, n_readings=1, n_down=0,
                    n_wait=1, sum_wait=10.0, mean_wait=10.0, built_at=now.isoformat(),
                )
            )
            # Raw readings old enough to be pruned, proving the prune really ran.
            session.add(
                Reading(
                    attraction_id="a1",
                    observed_at=(now - timedelta(days=200)).isoformat(),
                    wait_time=10, status="OPERATING",
                )
            )
            session.commit()

        # capture, twice, from the same payload
        with Session(engine) as session:
            first = collector._record_showtimes(
                session, _live_entity(OLD_DATE), now.isoformat()
            )
            session.commit()
        with Session(engine) as session:
            second = collector._record_showtimes(
                session, _live_entity(OLD_DATE), now.isoformat()
            )
            session.commit()

        check(first == 2, f"first capture wrote {first} showtimes, expected 2")
        check(
            second == 0,
            f"re-capturing the same payload wrote {second} more rows; the feed "
            "republishes every performance on every poll all day, so this must be "
            "idempotent or the table grows by ~1,400 duplicate rows a day",
        )

        with Session(engine) as session:
            kinds = {
                row.kind
                for row in session.exec(select(ShowTime)).all()
            }
        check(
            kinds == {"Performance Time", "Operating"},
            f"stored kinds are {kinds}; the feed's own type must be preserved "
            "verbatim, because 'Operating' is a meet-and-greet's opening hours and "
            "scheduling it as a performance would queue the visitor for 13 hours",
        )

        # ── the load-bearing one: a prune must not touch showtimes ───────
        original = retention.engine
        retention.engine = engine
        try:
            report = retention.run_prune(retention_days=35)
        finally:
            retention.engine = original

        check(
            report.get("pruned") is True,
            f"the prune did not run ({report.get('reason')}), so this proves nothing",
        )
        check(
            report.get("deleted_readings", 0) >= 1,
            "the prune deleted no readings, so it cannot be said to have run",
        )
        with Session(engine) as session:
            survivors = len(session.exec(select(ShowTime)).all())
            shows_left = len(session.exec(select(Show)).all())
        check(
            survivors == 2,
            f"{survivors} of 2 showtimes survived a prune. A showtime has no rollup "
            "and cannot be re-fetched — the feed publishes today only — so pruning "
            "it destroys the record permanently (ADR-0010)",
        )
        check(shows_left == 1, "the Show dimension row did not survive a prune")

    # ── the live-database half, read-only ───────────────────────────────
    conn = rdb.connect()
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        check("show" in tables and "showtime" in tables,
              "the show/showtime tables do not exist in the live database")

        captured = conn.execute("SELECT COUNT(*) FROM showtime").fetchone()[0]
        check(captured > 0,
              "no showtimes have been captured yet; run "
              "`python -m app.collector --reseed-geo`")

        # The two the planner offers must resolve, and say how well they are known.
        for key, spec in config.SHOWS.items():
            row = conn.execute(
                "SELECT COUNT(*) FROM show WHERE park_id = ? AND name = ?",
                (config.PARK_ID, spec.name),
            ).fetchone()[0]
            check(row == 1,
                  f"{spec.name!r} is not in the show table exactly once (found {row})")

            resolved = shows.showtime_for(conn, key, date="2026-10-05")
            check(resolved.tier in shows.TIER_ORDER,
                  f"{key}: unknown provenance tier {resolved.tier!r}")
            check(0 < resolved.start_minute < 1440,
                  f"{key}: start minute {resolved.start_minute} is not a time of day")
            check(resolved.latest_arrival < resolved.start_minute,
                  f"{key}: latest arrival is not before the start")

            # The proxy must be a real, routable attraction, or build_instance raises.
            roster = {a.name for a in rdb.roster(conn, mode="open_today")}
            check(spec.proxy_attraction in roster,
                  f"{key}: proxy {spec.proxy_attraction!r} is not in the roster, so "
                  "the show cannot be placed in the travel graph")
            check(spec.proxy_offset_m < 403.0,
                  f"{key}: proxy is {spec.proxy_offset_m} m away, which exceeds the "
                  "park's mean leg of 403 m — the alias would dominate the estimate")

        # Only real performances may drive the planner.
        kinds = {
            row[0]
            for row in conn.execute("SELECT DISTINCT kind FROM showtime")
        }
        check(shows.PERFORMANCE_KIND in kinds,
              f"no {shows.PERFORMANCE_KIND!r} rows captured; kinds seen: {kinds}")

        # A party night must refuse the fireworks rather than plan around them.
        party = conn.execute(
            """
            SELECT o.date FROM parkschedule o
            JOIN parkschedule e ON e.park_id = o.park_id AND e.date = o.date
                 AND e.type = 'TICKETED_EVENT'
            WHERE o.park_id = ? AND o.type = 'OPERATING' AND o.date > '2026-09-25'
              AND substr(e.opening_time, 12, 5) >= substr(o.closing_time, 12, 5)
            ORDER BY o.date LIMIT 1
            """,
            (config.PARK_ID,),
        ).fetchone()
        if party is None:
            out.append("no forward party night found to test the fireworks refusal")
        else:
            day = rdb.operating_days(conn, start=party[0], end=party[0])[0]
            offered = shows.available(
                conn, date=party[0], close_minute=day.close_minute,
                party_night=day.party_night,
            )
            check(
                offered["fireworks"]["available"] is False,
                f"fireworks were offered on party night {party[0]}, when the park "
                "closes at 18:00 for a regular ticket",
            )
            check(
                bool(offered["fireworks"]["reason"]),
                "the fireworks were refused without saying why",
            )
    finally:
        conn.close()

    return out


def test_showtimes():  # pytest entry point
    assert failures() == []


if __name__ == "__main__":
    problems = failures()
    for problem in problems:
        print("FAIL:", problem)
    print("showtimes: OK" if not problems else f"showtimes: {len(problems)} failure(s)")
    sys.exit(1 if problems else 0)
