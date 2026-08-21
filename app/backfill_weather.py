"""One-time backfill of historical weather.

    .venv/bin/python -m app.backfill_weather

Weather collection started later than wait collection, so the earliest hours of
the wait timeline have no weather. Open-Meteo's historical (hourly) API can fill
those gaps. This inserts ONE WeatherReading per park per past hour that:

  - falls within the span we have wait data for, and
  - is not in the future, and
  - has no weather reading yet (live or already backfilled).

Hours that already have live per-minute weather are left untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlmodel import Session, func, select

from app import weather
from app.db import engine, init_db
from app.models import Park, Reading, WeatherReading

_FIELDS = ("temperature_c", "precipitation_mm", "weather_code", "wind_speed_kmh", "is_day")


def backfill() -> int:
    """Insert missing historical WeatherReadings. Returns rows written."""
    init_db()
    now_iso = datetime.now(timezone.utc).isoformat()

    with Session(engine) as session:
        parks = session.exec(
            select(Park).where(Park.latitude != None)  # noqa: E711
        ).all()
        coords = [(p.id, p.latitude, p.longitude) for p in parks]

        wait_start = session.exec(select(func.min(Reading.observed_at))).one()
        if wait_start is None:
            print("[backfill] no wait data yet; nothing to backfill")
            return 0
        start_hour = wait_start[:13]  # "YYYY-MM-DDTHH"

        # Hours (per park) that already have any weather reading.
        existing: set[tuple[str, str]] = set()
        for park_id, observed_at in session.exec(
            select(WeatherReading.park_id, WeatherReading.observed_at)
        ).all():
            existing.add((park_id, observed_at[:13]))

        history = weather.fetch_history(coords)

        inserted = 0
        for park_id, rows in history.items():
            for r in rows:
                observed_at = f"{r['time']}:00+00:00"
                hour_key = observed_at[:13]
                if hour_key < start_hour:
                    continue  # before we have any waits
                if observed_at > now_iso:
                    continue  # forecast hour, no waits
                if (park_id, hour_key) in existing:
                    continue  # live or already-backfilled
                if r["temperature_c"] is None:
                    continue
                session.add(
                    WeatherReading(
                        park_id=park_id,
                        observed_at=observed_at,
                        **{k: r[k] for k in _FIELDS},
                    )
                )
                existing.add((park_id, hour_key))
                inserted += 1
        session.commit()
        return inserted


def main() -> None:
    print(f"[backfill] inserted {backfill()} historical weather rows")


if __name__ == "__main__":
    main()
