"""Standalone collector. Run once per minute by cron.

    .venv/bin/python -m app.collector

Each run polls every Destination in themeparks.DESTINATION_IDS (each isolated so
one resort's API failure can't starve the others), writing one Reading per
ATTRACTION (SHOWs and water parks skipped; closed rides stored with
wait_time = NULL). All Destinations share one poll minute. Then one batched
Open-Meteo call writes one WeatherReading per park, also failure-isolated.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, func, select

from app import themeparks, weather
from app.db import engine, init_db
from app.models import (
    Attraction,
    Destination,
    Park,
    ParkSchedule,
    Reading,
    Show,
    ShowTime,
    WeatherReading,
)

# Re-fetch a park's schedule at most this often (~daily).
SCHEDULE_REFRESH_SECONDS = 20 * 3600

# Re-fetch Park + Attraction coordinates at most this often. They effectively
# never change, so this is just how fast a newly-opened ride gets geocoded.
GEO_REFRESH_SECONDS = 6 * 3600


def _standby_wait(entity: dict) -> int | None:
    """Extract the standby wait in minutes, or None if not reporting one."""
    return entity.get("queue", {}).get("STANDBY", {}).get("waitTime")


def _record_showtimes(session: Session, entity: dict, observed_at: str) -> int:
    """Persist a Show's published performances for their park-local dates.

    Idempotent on (show_id, date, start_time): the same performance is re-published
    on every poll all day, so the first sighting wins and `first_seen` records when
    the schedule was announced rather than when it was last echoed.

    The Show row itself is seeded by _ensure_geo from /children, which is the only
    call carrying coordinates. If it has not run yet, the showtime is skipped
    rather than written against a missing parent.
    """
    show_id = entity.get("id")
    if show_id is None or session.get(Show, show_id) is None:
        return 0

    written = 0
    for slot in entity.get("showtimes") or []:
        start = slot.get("startTime")
        if not start:
            continue
        # The park-local date comes from the offset-aware timestamp itself, so a
        # performance at 00:30 belongs to the operating date that started the
        # evening before only if the feed says so — we do not re-derive it.
        date = start[:10]
        existing = session.exec(
            select(ShowTime)
            .where(ShowTime.show_id == show_id)
            .where(ShowTime.date == date)
            .where(ShowTime.start_time == start)
        ).first()
        if existing is not None:
            continue
        session.add(
            ShowTime(
                show_id=show_id,
                date=date,
                start_time=start,
                end_time=slot.get("endTime"),
                kind=slot.get("type", "UNKNOWN"),
                first_seen=observed_at,
            )
        )
        written += 1
    return written


def _ensure_geo(session: Session, destination: Destination, *, force: bool = False) -> None:
    """Seed/backfill a Destination's Park rows and its Attractions' coordinates.

    Both come from ONE recursive /children call (see themeparks.fetch_geo), so
    geocoding the whole roster costs no extra requests beyond this refresh.

    Coordinates are static, so the refresh is time-based rather than
    missing-data-based: a ride the feed never geocodes would otherwise re-trigger
    a fetch every single poll. A NULL `geo_fetched_at` forces a seed, which is
    what backfills the existing roster the first time this runs; after that a new
    ride picks up its coordinates within GEO_REFRESH_SECONDS.
    """
    last = destination.geo_fetched_at
    if last is not None and not force:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        if age < GEO_REFRESH_SECONDS:
            return

    parks, attractions, shows = themeparks.fetch_geo(destination.id)
    for p in parks:
        existing = session.get(Park, p["id"])
        if existing is None:
            session.add(
                Park(
                    id=p["id"],
                    name=p["name"],
                    destination_id=destination.id,
                    latitude=p["latitude"],
                    longitude=p["longitude"],
                )
            )
        else:
            existing.name = p["name"]
            existing.destination_id = destination.id
            existing.latitude = p["latitude"]
            existing.longitude = p["longitude"]
            session.add(existing)

    # Only update rows we already have: /children also lists water-park and
    # resort-area attractions the poll loop deliberately never stores.
    for a in attractions:
        existing = session.get(Attraction, a["id"])
        if existing is None:
            continue
        existing.latitude = a["latitude"]
        existing.longitude = a["longitude"]
        session.add(existing)

    # Shows are seeded here rather than from /live, because /children is the only
    # call that carries their coordinates and their park. A show with no parkId is
    # a resort-area entity; one under a water park never matches a Park row.
    known_parks = {p["id"] for p in parks}
    seen_at = datetime.now(timezone.utc).isoformat()
    for sh in shows:
        if sh["park_id"] not in known_parks:
            continue
        existing = session.get(Show, sh["id"])
        if existing is None:
            session.add(
                Show(
                    id=sh["id"],
                    name=sh["name"],
                    park_id=sh["park_id"],
                    last_seen=seen_at,
                    latitude=sh["latitude"],
                    longitude=sh["longitude"],
                )
            )
        else:
            existing.name = sh["name"] or existing.name
            existing.park_id = sh["park_id"]
            existing.last_seen = seen_at
            existing.latitude = sh["latitude"]
            existing.longitude = sh["longitude"]
            session.add(existing)

    destination.geo_fetched_at = datetime.now(timezone.utc).isoformat()
    session.add(destination)
    session.commit()


def _collect_destination(
    destination_id: str, observed_at: str, *, reseed_geo: bool = False
) -> tuple[int, int]:
    """Poll one Destination. Returns (readings written, showtimes written)."""
    payload = themeparks.fetch_live(destination_id)
    written = 0
    showtimes = 0
    with Session(engine) as session:
        # Upsert the Destination dimension straight from the live payload.
        dest_id = payload.get("id", destination_id)
        dest = session.get(Destination, dest_id)
        if dest is None:
            dest = Destination(id=dest_id, name=payload.get("name", ""))
        else:
            dest.name = payload.get("name", dest.name)
        session.add(dest)
        session.commit()

        _ensure_geo(session, dest, force=reseed_geo)

        for entity in payload.get("liveData", []):
            if entity.get("entityType") == "SHOW":
                # A Show reports showtimes instead of a standby wait, so it gets no
                # Reading. Captured here because /live is the ONLY place these
                # appear: /entity/{show_id}/schedule returns nothing, so a
                # performance not recorded today is unrecoverable tomorrow.
                showtimes += _record_showtimes(session, entity, observed_at)
                continue
            if entity.get("entityType") != "ATTRACTION":
                continue  # restaurants and the park entity itself
            park_id = entity.get("parkId")
            if park_id is None or park_id in themeparks.EXCLUDED_PARK_IDS:
                continue  # resort-area attraction (no park) or water park

            attraction_id = entity["id"]
            name = entity.get("name", "")

            attraction = session.get(Attraction, attraction_id)
            if attraction is None:
                session.add(
                    Attraction(
                        id=attraction_id,
                        name=name,
                        park_id=park_id,
                        last_seen=observed_at,
                    )
                )
            else:
                attraction.name = name
                attraction.park_id = park_id
                attraction.last_seen = observed_at
                session.add(attraction)

            session.add(
                Reading(
                    attraction_id=attraction_id,
                    observed_at=observed_at,
                    wait_time=_standby_wait(entity),
                    status=entity.get("status", "UNKNOWN"),
                )
            )
            written += 1

        session.commit()
    return written, showtimes


def _ensure_schedules() -> int:
    """Refresh each park's operating-hours schedule about once a day.

    Each /schedule call returns ~a month of dates; we re-fetch a park only when
    its latest fetch is older than SCHEDULE_REFRESH_SECONDS, so this is a daily
    refresh that also keeps the future buffer topped up and catches hour changes.
    """
    now = datetime.now(timezone.utc)
    written = 0
    with Session(engine) as session:
        parks = session.exec(
            select(Park).where(Park.latitude != None)  # noqa: E711
        ).all()
        for park in parks:
            last = session.exec(
                select(func.max(ParkSchedule.fetched_at)).where(
                    ParkSchedule.park_id == park.id
                )
            ).one()
            if last is not None:
                age = (now - datetime.fromisoformat(last)).total_seconds()
                if age < SCHEDULE_REFRESH_SECONDS:
                    continue
            fetched_at = now.isoformat()
            for entry in themeparks.fetch_schedule(park.id):
                existing = session.exec(
                    select(ParkSchedule)
                    .where(ParkSchedule.park_id == park.id)
                    .where(ParkSchedule.date == entry["date"])
                    .where(ParkSchedule.type == entry["type"])
                ).first()
                if existing is None:
                    session.add(
                        ParkSchedule(
                            park_id=park.id,
                            date=entry["date"],
                            type=entry["type"],
                            opening_time=entry["opening_time"],
                            closing_time=entry["closing_time"],
                            fetched_at=fetched_at,
                        )
                    )
                    written += 1
                else:
                    existing.opening_time = entry["opening_time"]
                    existing.closing_time = entry["closing_time"]
                    existing.fetched_at = fetched_at
                    session.add(existing)
        session.commit()
    return written


def _collect_weather(observed_at: str) -> int:
    """Fetch and store one WeatherReading per park. Returns rows written.

    Only seeded (non-water) parks have coordinates, so water parks are naturally
    excluded. Isolated from wait collection by the caller.
    """
    with Session(engine) as session:
        parks = session.exec(
            select(Park).where(Park.latitude != None)  # noqa: E711
        ).all()
        coords = [(p.id, p.latitude, p.longitude) for p in parks]
        readings = weather.fetch_weather(coords)
        for park_id, fields in readings.items():
            session.add(
                WeatherReading(park_id=park_id, observed_at=observed_at, **fields)
            )
        session.commit()
        return len(readings)


def collect(*, reseed_geo: bool = False) -> tuple[int, int, int]:
    """Run one poll across all Destinations.

    Returns (readings, weather rows, showtimes). `reseed_geo` bypasses the
    GEO_REFRESH_SECONDS throttle for this poll, which is how a newly-added entity
    kind gets backfilled without waiting out the interval or editing the database
    by hand.
    """
    init_db()
    observed_at = (
        datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
    )

    written = 0
    showtimes = 0
    for destination_id in themeparks.DESTINATION_IDS:
        try:
            rows, shows = _collect_destination(
                destination_id, observed_at, reseed_geo=reseed_geo
            )
            written += rows
            showtimes += shows
        except Exception as exc:  # noqa: BLE001 - isolate each destination
            print(f"[collector] destination {destination_id} failed: {exc}")

    weather_rows = 0
    try:
        weather_rows = _collect_weather(observed_at)
    except Exception as exc:  # noqa: BLE001 - never let weather break collection
        print(f"[collector] weather fetch failed: {exc}")

    # Park schedules: ~daily, failure-isolated like weather.
    try:
        sched_rows = _ensure_schedules()
        if sched_rows:
            print(f"[collector] refreshed schedules ({sched_rows} new rows)")
    except Exception as exc:  # noqa: BLE001
        print(f"[collector] schedule refresh failed: {exc}")

    return written, weather_rows, showtimes


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m app.collector")
    parser.add_argument(
        "--reseed-geo",
        action="store_true",
        help="ignore the coordinate-refresh throttle for this poll",
    )
    args = parser.parse_args()

    written, weather_rows, showtimes = collect(reseed_geo=args.reseed_geo)
    extra = f", {showtimes} showtimes" if showtimes else ""
    print(f"[collector] wrote {written} readings, {weather_rows} weather rows{extra}")


if __name__ == "__main__":
    main()
