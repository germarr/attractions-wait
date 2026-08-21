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
    WeatherReading,
)

# Re-fetch a park's schedule at most this often (~daily).
SCHEDULE_REFRESH_SECONDS = 20 * 3600


def _standby_wait(entity: dict) -> int | None:
    """Extract the standby wait in minutes, or None if not reporting one."""
    return entity.get("queue", {}).get("STANDBY", {}).get("waitTime")


def _ensure_parks(session: Session, destination_id: str) -> None:
    """Seed/backfill a Destination's parks (names, coords, destination_id).

    Re-fetches /children only when this Destination has no parks yet or any of
    its parks still lacks coordinates — so it self-heals pre-existing rows
    (e.g. Universal parks seeded before destination_id existed).
    """
    parks = session.exec(
        select(Park).where(Park.destination_id == destination_id)
    ).all()
    if parks and all(p.latitude is not None for p in parks):
        return
    for p in themeparks.fetch_parks(destination_id):
        existing = session.get(Park, p["id"])
        if existing is None:
            session.add(
                Park(
                    id=p["id"],
                    name=p["name"],
                    destination_id=destination_id,
                    latitude=p["latitude"],
                    longitude=p["longitude"],
                )
            )
        else:
            existing.name = p["name"]
            existing.destination_id = destination_id
            existing.latitude = p["latitude"]
            existing.longitude = p["longitude"]
            session.add(existing)
    session.commit()


def _collect_destination(destination_id: str, observed_at: str) -> int:
    """Poll one Destination and write its Readings. Returns rows written."""
    payload = themeparks.fetch_live(destination_id)
    written = 0
    with Session(engine) as session:
        # Upsert the Destination dimension straight from the live payload.
        dest_id = payload.get("id", destination_id)
        dest = session.get(Destination, dest_id)
        if dest is None:
            session.add(Destination(id=dest_id, name=payload.get("name", "")))
        else:
            dest.name = payload.get("name", dest.name)
            session.add(dest)
        session.commit()

        _ensure_parks(session, destination_id)

        for entity in payload.get("liveData", []):
            if entity.get("entityType") != "ATTRACTION":
                continue  # Shows have no standby wait
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
    return written


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


def collect() -> tuple[int, int]:
    """Run one poll across all Destinations. Returns (readings, weather rows)."""
    init_db()
    observed_at = (
        datetime.now(timezone.utc).replace(second=0, microsecond=0).isoformat()
    )

    written = 0
    for destination_id in themeparks.DESTINATION_IDS:
        try:
            written += _collect_destination(destination_id, observed_at)
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

    return written, weather_rows


def main() -> None:
    written, weather_rows = collect()
    print(f"[collector] wrote {written} readings, {weather_rows} weather rows")


if __name__ == "__main__":
    main()
