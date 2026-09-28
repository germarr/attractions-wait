"""When the parade and the fireworks start, and how much we actually know.

The feed publishes performance times for *today only* — `/entity/{show_id}/schedule`
returns nothing at all — so a future date's showtime cannot be looked up. It has to
come from captured history (ADR-0010), and that history began on
`config.SHOW_REFERENCE_DATE`. Until enough dates accrue, the planner runs on a
single observed reference.

Every answer therefore carries a provenance tier, the same discipline
`waits.WaitTable` applies to wait estimates:

    observed      — this exact date was captured
    weekday_mode  — the modal start time for this weekday, over >= MIN_SHOWTIME_DATES
    park_mode     — the modal start time across all captured dates
    reference     — the seeded single observation; we are guessing

A tier is reported, never hidden, so "fireworks at 21:30" and "fireworks at 21:30,
which we have seen once" can be told apart by whoever reads the plan.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import date as date_cls
from typing import Literal

from research_project import config

Tier = Literal["observed", "weekday_mode", "park_mode", "reference"]
TIER_ORDER: tuple[Tier, ...] = ("observed", "weekday_mode", "park_mode", "reference")

# Only a real scheduled performance can be planned around. "Operating" is a
# continuously-open meet-and-greet whose showtime is its opening hours, and
# "Special Ticketed Event" belongs to a party a regular ticket does not admit you to.
PERFORMANCE_KIND = "Performance Time"


@dataclass(frozen=True)
class Showtime:
    """A show's start time on one date, with how well we know it."""

    key: str
    name: str
    start_minute: float
    tier: Tier
    n_dates: int
    watch_minutes: float
    arrive_early_min: float
    proxy_attraction: str
    proxy_offset_m: float

    @property
    def latest_arrival(self) -> float:
        """The last minute you can turn up and still expect to see it."""
        return self.start_minute - self.arrive_early_min

    @property
    def end_minute(self) -> float:
        return self.start_minute + self.watch_minutes

    @property
    def measured(self) -> bool:
        return self.tier != "reference"


def _minute_of_day(stamp: str) -> float:
    """Park-local minutes from midnight for an offset-aware ISO8601 stamp."""
    return float(int(stamp[11:13]) * 60 + int(stamp[14:16]))


def _history(
    conn: sqlite3.Connection, park_id: str, show_name: str
) -> list[tuple[str, float]]:
    """(date, start minute) for every captured performance of this show."""
    rows = conn.execute(
        """
        SELECT st.date, st.start_time
        FROM showtime st
        JOIN show s ON s.id = st.show_id
        WHERE s.park_id = ? AND s.name = ? AND st.kind = ?
        ORDER BY st.date
        """,
        (park_id, show_name, PERFORMANCE_KIND),
    ).fetchall()
    return [(r[0], _minute_of_day(r[1])) for r in rows]


def _mode(values: list[float]) -> float:
    """Most common start time, tie-broken toward the later one.

    A mode rather than a mean: showtimes are near-constant with occasional shifts,
    and averaging 21:30 across thirty dates with one 20:00 special gives 21:27,
    which is a time the show has never once started.
    """
    counts = Counter(values)
    best = max(counts.values())
    return max(v for v, n in counts.items() if n == best)


def showtime_for(
    conn: sqlite3.Connection,
    show_key: str,
    *,
    date: str,
    park_id: str = config.PARK_ID,
    assumptions: config.Assumptions | None = None,
) -> Showtime:
    """The best available start time for `show_key` on `date`, with its tier."""
    spec = config.SHOWS[show_key]
    assumptions = assumptions or config.Assumptions()
    early = spec.arrive_early_min * assumptions.show_arrive_early_scale
    history = _history(conn, park_id, spec.name)

    def build(minute: float, tier: Tier, n: int) -> Showtime:
        return Showtime(
            key=spec.key,
            name=spec.name,
            start_minute=minute,
            tier=tier,
            n_dates=n,
            watch_minutes=spec.watch_minutes,
            arrive_early_min=early,
            proxy_attraction=spec.proxy_attraction,
            proxy_offset_m=spec.proxy_offset_m,
        )

    exact = [m for d, m in history if d == date]
    if exact:
        return build(_mode(exact), "observed", 1)

    weekday = date_cls.fromisoformat(date).weekday()
    same_weekday = [
        m for d, m in history if date_cls.fromisoformat(d).weekday() == weekday
    ]
    if len({d for d, _ in history if date_cls.fromisoformat(d).weekday() == weekday}) \
            >= config.MIN_SHOWTIME_DATES:
        return build(_mode(same_weekday), "weekday_mode", len(same_weekday))

    if len({d for d, _ in history}) >= config.MIN_SHOWTIME_DATES:
        return build(_mode([m for _, m in history]), "park_mode", len(history))

    return build(
        config.SHOW_REFERENCE_MINUTES[show_key], "reference", len(history)
    )


def available(
    conn: sqlite3.Connection,
    *,
    date: str,
    close_minute: float,
    party_night: bool,
    park_id: str = config.PARK_ID,
    assumptions: config.Assumptions | None = None,
) -> dict[str, dict]:
    """Which shows can be planned on this date, and the reason when one cannot.

    A show is refused rather than quietly shifted. On a party night the park closes
    at 18:00 for a regular ticket and the fireworks run inside the separately
    ticketed event, so planning around them would be planning a day the user has not
    bought. The same check catches an ordinary short day.
    """
    out: dict[str, dict] = {}
    for key, spec in config.SHOWS.items():
        showtime = showtime_for(
            conn, key, date=date, park_id=park_id, assumptions=assumptions
        )
        reason: str | None = None
        if spec.needs_late_close and party_night:
            reason = (
                "the park closes at "
                f"{int(close_minute // 60):02d}:{int(close_minute % 60):02d} for a "
                "regular ticket on this date and the show runs inside the "
                "separately ticketed evening party"
            )
        elif showtime.end_minute > close_minute:
            reason = (
                f"it ends at {_clock(showtime.end_minute)}, after the park closes at "
                f"{_clock(close_minute)}"
            )
        elif showtime.latest_arrival <= 0:
            reason = "no usable arrival time"
        out[key] = {
            "key": key,
            "name": spec.name,
            "start_minute": showtime.start_minute,
            "start": _clock(showtime.start_minute),
            "end_minute": showtime.end_minute,
            "watch_minutes": showtime.watch_minutes,
            "arrive_early_min": showtime.arrive_early_min,
            "arrive_by": _clock(showtime.latest_arrival),
            "tier": showtime.tier,
            "n_dates": showtime.n_dates,
            "measured": showtime.measured,
            "proxy_attraction": showtime.proxy_attraction,
            "proxy_offset_m": showtime.proxy_offset_m,
            "available": reason is None,
            "reason": reason,
        }
    return out


def _clock(minute: float) -> str:
    total = int(round(minute))
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"
