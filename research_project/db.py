"""Read-only access to the collector's database.

This package must never be able to damage the live dataset, so every connection
opens `file:{DB_PATH}?mode=ro` — the same discipline `tests/test_walking.py`
uses. We deliberately do NOT import `app.db.engine`: that engine is read-write,
sets WAL pragmas on connect, and `app.db.init_db()` creates tables. Only the
path is borrowed.

The ~15 lines of schedule reading here duplicate `app.stats._operating_windows`
rather than importing it, because importing it would drag in the read-write
engine. The duplication is pinned to the original by a test that compares the two
on a sample of dates.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path
from typing import Collection, Literal, Sequence

from app.db import DB_PATH  # path only — not the engine

from research_project import config


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open the dataset read-only. Writes raise sqlite3.OperationalError."""
    path = db_path or DB_PATH
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(frozen=True)
class RosterAttraction:
    """One attraction the optimizer may schedule."""

    id: str
    name: str
    latitude: float
    longitude: float
    ride_minutes: float
    refurbishment: bool


@dataclass(frozen=True)
class DayWindow:
    """One date's OPERATING window, in minutes from that date's local midnight.

    `close_minute` can exceed 1440: Magic Kingdom really does close at 00:00 on
    some Saturdays and ran 08:00 -> 00:00 on 2026-07-04. Keeping the window in
    minutes-from-midnight rather than wall-clock makes that a number greater than
    1440 instead of a special case.
    """

    date: str
    weekday: int  # Monday 0 .. Sunday 6
    open_minute: float
    close_minute: float
    party_night: bool

    @property
    def length_minutes(self) -> float:
        return self.close_minute - self.open_minute


def _minutes_from_local_midnight(stamp: str, on_date: str) -> float:
    """Minutes from `on_date`'s local midnight to an ISO8601 schedule stamp."""
    moment = datetime.fromisoformat(stamp)
    midnight = datetime.combine(
        date_cls.fromisoformat(on_date),
        datetime.min.time(),
        tzinfo=moment.tzinfo,
    )
    return (moment - midnight).total_seconds() / 60.0


def roster(
    conn: sqlite3.Connection,
    park_id: str = config.PARK_ID,
    *,
    mode: Literal["history", "open_today"] = "open_today",
    ride_scale: float = 1.0,
) -> list[RosterAttraction]:
    """Attractions the optimizer may schedule, ordered by name.

    `history` keeps every geocoded attraction with wait history (31 at Magic
    Kingdom). `open_today` additionally drops anything whose most recent reading
    says REFURBISHMENT (30 — Walt Disney's Carousel of Progress), and is the right
    mode for any forward-looking claim: a ride that is closed for refurbishment
    has plenty of history and cannot be visited tomorrow.

    The four geocoded Magic Kingdom entities with no wait history at all — Casey
    Jr. Splash 'N' Soak Station, A Pirate's Adventure, Cinderella Castle, Main
    Street Vehicles — are excluded by the EXISTS clause, since an attraction with
    no queue cannot be scheduled against a wait model.
    """
    rows = conn.execute(
        """
        SELECT a.id, a.name, a.latitude, a.longitude,
               (SELECT r.status FROM reading r
                 WHERE r.attraction_id = a.id
                 ORDER BY r.observed_at DESC LIMIT 1) AS latest_status
        FROM attraction a
        WHERE a.park_id = ?
          AND a.latitude IS NOT NULL
          AND EXISTS (SELECT 1 FROM attractionhourly h
                       WHERE h.attraction_id = a.id)
        ORDER BY a.name
        """,
        (park_id,),
    ).fetchall()

    out: list[RosterAttraction] = []
    for row in rows:
        refurbishment = row["latest_status"] == "REFURBISHMENT"
        if mode == "open_today" and refurbishment:
            continue
        out.append(
            RosterAttraction(
                id=row["id"],
                name=row["name"],
                latitude=row["latitude"],
                longitude=row["longitude"],
                ride_minutes=config.ride_minutes(row["name"], scale=ride_scale),
                refurbishment=refurbishment,
            )
        )
    return out


def usable_dates(
    conn: sqlite3.Connection,
    park_id: str = config.PARK_ID,
    *,
    start: str = config.HISTORY_START,
    excluded: Collection[str] = config.EXCLUDED_DATES,
) -> list[str]:
    """Historical dates the wait estimator may learn from.

    A date qualifies only if it has an OPERATING row with both times present —
    that is the invariant whose absence made 2026-06-24 unusable, and testing it
    directly catches the whole class rather than that one date.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT h.date
        FROM attractionhourly h
        JOIN attraction a ON a.id = h.attraction_id
        JOIN parkschedule s ON s.date = h.date AND s.park_id = a.park_id
        WHERE a.park_id = ?
          AND h.date >= ?
          AND s.type = 'OPERATING'
          AND s.opening_time IS NOT NULL
          AND s.closing_time IS NOT NULL
        ORDER BY h.date
        """,
        (park_id, start),
    ).fetchall()
    return [r["date"] for r in rows if r["date"] not in excluded]


def operating_days(
    conn: sqlite3.Connection,
    park_id: str = config.PARK_ID,
    *,
    start: str,
    end: str,
) -> list[DayWindow]:
    """OPERATING windows in [start, end], with the after-hours party flag set.

    A party night is a TICKETED_EVENT starting at or after that day's close. Those
    are the 18:00 closes — Friday closes at 18:00 on 12 of 18 observed dates —
    and they matter enormously: a 9-hour window against Saturday's 14 is the
    single largest lever on how many attractions fit.
    """
    rows = conn.execute(
        """
        SELECT date, type, opening_time, closing_time
        FROM parkschedule
        WHERE park_id = ? AND date >= ? AND date <= ?
          AND opening_time IS NOT NULL AND closing_time IS NOT NULL
        ORDER BY date
        """,
        (park_id, start, end),
    ).fetchall()

    operating: dict[str, tuple[float, float]] = {}
    events: dict[str, list[float]] = {}
    for row in rows:
        opens = _minutes_from_local_midnight(row["opening_time"], row["date"])
        closes = _minutes_from_local_midnight(row["closing_time"], row["date"])
        if row["type"] == "OPERATING":
            operating[row["date"]] = (opens, closes)
        elif row["type"] == "TICKETED_EVENT":
            events.setdefault(row["date"], []).append(opens)

    out: list[DayWindow] = []
    for day, (opens, closes) in sorted(operating.items()):
        out.append(
            DayWindow(
                date=day,
                weekday=date_cls.fromisoformat(day).weekday(),
                open_minute=opens,
                close_minute=closes,
                party_night=any(start_min >= closes for start_min in events.get(day, [])),
            )
        )
    return out


def party_nights(
    conn: sqlite3.Connection,
    park_id: str = config.PARK_ID,
    *,
    dates: Sequence[str],
) -> set[str]:
    """The subset of `dates` carrying an after-hours ticketed event."""
    if not dates:
        return set()
    days = operating_days(conn, park_id, start=min(dates), end=max(dates))
    wanted = set(dates)
    return {d.date for d in days if d.party_night and d.date in wanted}
