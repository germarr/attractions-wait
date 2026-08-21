"""SQLModel definitions for the wait-time dataset.

Three tables: two small dimension tables (Park, Attraction) and the Reading
fact table that holds one row per Attraction per poll.
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel


class Destination(SQLModel, table=True):
    """A top-level resort the app polls (e.g. Universal Orlando, Walt Disney World)."""

    id: str = Field(primary_key=True)  # themeparks.wiki uuid
    name: str


class Park(SQLModel, table=True):
    """A theme park inside a Destination. Seeded from /children (water parks excluded)."""

    id: str = Field(primary_key=True)  # themeparks.wiki uuid
    name: str
    destination_id: str | None = Field(default=None, foreign_key="destination.id", index=True)
    latitude: float | None = Field(default=None)  # seeded from /children
    longitude: float | None = Field(default=None)


class Attraction(SQLModel, table=True):
    """A single ride that reports a standby wait. Upserted from each /live poll."""

    id: str = Field(primary_key=True)  # themeparks.wiki uuid
    name: str
    park_id: str = Field(foreign_key="park.id", index=True)
    last_seen: str  # UTC ISO8601 of the most recent poll that saw this ride


class Reading(SQLModel, table=True):
    """One observation of one Attraction's standby wait at one poll.

    `observed_at` is the shared poll time (every row from a single poll carries
    the same value), stored as UTC ISO8601. `wait_time` is NULL when the ride is
    not reporting a standby wait (closed/down); `status` always records why.
    """

    id: int | None = Field(default=None, primary_key=True)
    # No single-column index on attraction_id: the composite ix_reading_attr_observed
    # (attraction_id, observed_at) created in db._migrate covers every attraction_id
    # lookup, so a standalone index is pure write cost + ~86 MB. See ADR-0005.
    attraction_id: str = Field(foreign_key="attraction.id")
    observed_at: str = Field(index=True)  # UTC ISO8601, poll time
    wait_time: int | None = Field(default=None)  # minutes; NULL when not operating
    status: str  # OPERATING / CLOSED / DOWN / REFURBISHMENT / ...


class ParkSchedule(SQLModel, table=True):
    """A Park's operating hours for one date, from the /schedule endpoint.

    Times are ISO8601 with the park's local offset. `fetched_at` (UTC ISO) drives
    the daily refresh. Multiple rows per date are possible (e.g. OPERATING plus a
    TICKETED_EVENT); operating hours come from the OPERATING row.
    """

    id: int | None = Field(default=None, primary_key=True)
    park_id: str = Field(foreign_key="park.id", index=True)
    date: str = Field(index=True)  # YYYY-MM-DD, park-local
    type: str  # OPERATING / TICKETED_EVENT / INFO / ...
    opening_time: str | None = Field(default=None)
    closing_time: str | None = Field(default=None)
    fetched_at: str  # UTC ISO8601 of the last fetch


class AttractionDaily(SQLModel, table=True):
    """One finalized park-local day of an Attraction, Operating-Hours-filtered.

    Nightly rollup (see docs/adr/0004). Counts/sums combine across days by
    summation (window Downtime Rate = Σn_down/Σn_readings; window mean =
    Σsum_wait/Σn_wait); mean/median/std are the per-day chart buckets that can't
    be reconstructed from sums, so they're stored directly.
    """

    attraction_id: str = Field(foreign_key="attraction.id", primary_key=True)
    date: str = Field(primary_key=True)  # YYYY-MM-DD, park-local
    n_readings: int  # in-hours reading-minutes (Downtime Rate denominator)
    n_down: int  # DOWN|CLOSED in-hours (Downtime Rate numerator)
    n_wait: int  # non-null wait minutes
    sum_wait: float  # Σ wait_time over n_wait
    mean_wait: float | None = None
    median_wait: float | None = None
    std_wait: float | None = None
    built_at: str  # UTC ISO8601, provenance


class AttractionHourly(SQLModel, table=True):
    """Per Attraction, per park-local date, per hour-of-day wait sums.

    Backs the Crowd Index: the hour-of-day baseline is Σsum_wait/Σn_wait over the
    trailing window (materialized into AttractionHourBaseline), and a window's
    numerator sums sum_wait/baseline over its dates. Wait-only (Downtime lives in
    AttractionDaily).
    """

    attraction_id: str = Field(foreign_key="attraction.id", primary_key=True)
    date: str = Field(primary_key=True)  # YYYY-MM-DD, park-local
    hour: int = Field(primary_key=True)  # 0–23, park-local
    n_wait: int
    sum_wait: float
    built_at: str


class AttractionHourBaseline(SQLModel, table=True):
    """Crowd Index baseline: hour-of-day wait sums per Attraction over the
    trailing CROWD_BASELINE_DAYS, fully rebuilt each night (small).

    Stores sum_wait + n (not a pre-divided mean) so the request layer can fold
    today's in-progress hours in — the live baseline includes today, so matching
    it requires the raw components, not a finalized average.
    """

    attraction_id: str = Field(foreign_key="attraction.id", primary_key=True)
    hour: int = Field(primary_key=True)  # 0–23, park-local
    sum_wait: float
    n: int
    built_at: str


class ParkDaily(SQLModel, table=True):
    """One finalized park-local day of a Park's synthetic Park Average + Headliner.

    Park-grain (not pooled attraction-minutes) so avg wait / Headliner match the
    live per-minute computation exactly: sum_minavg/n_min is Σ of the per-minute
    Park Average; sum_top5/n_min_top5 is Σ of the per-minute top-5 mean.
    n_readings/n_down are the roster-wide Downtime Rate counts for the park.
    """

    park_id: str = Field(foreign_key="park.id", primary_key=True)
    date: str = Field(primary_key=True)  # YYYY-MM-DD, park-local
    n_readings: int  # in-hours reading-minutes across roster (Park Downtime denom)
    n_down: int  # in-hours DOWN|CLOSED across roster
    n_min: int  # distinct operating minutes (Park Average denominator)
    sum_minavg: float  # Σ per-minute Park Average
    sum_top5: float  # Σ per-minute top-5 (Headliner) mean
    n_min_top5: int
    mean_wait: float | None = None  # per-day Park-Average chart bucket
    median_wait: float | None = None
    std_wait: float | None = None
    built_at: str


class ParkCorrelation(SQLModel, table=True):
    """Nightly Pearson matrix (Wait/Downtime/Temp/Rain, 30-day hourly) per park.

    Stored as JSON so the label set can evolve without a migration. Read verbatim
    by the reliability endpoint.
    """

    park_id: str = Field(foreign_key="park.id", primary_key=True)
    labels_json: str
    matrix_json: str | None = None
    n: int = 0
    built_at: str


class WeatherReading(SQLModel, table=True):
    """One observation of a Park's weather at a poll minute.

    `observed_at` is the SAME shared poll time as that minute's wait Readings, so
    weather and waits join on an identical minute key. "Raining" is not stored —
    it is derived from `weather_code` + `precipitation_mm` at query time.
    """

    id: int | None = Field(default=None, primary_key=True)
    park_id: str = Field(foreign_key="park.id", index=True)
    observed_at: str = Field(index=True)  # UTC ISO8601, shared poll time
    temperature_c: float | None = Field(default=None)  # temperature_2m, °C
    precipitation_mm: float | None = Field(default=None)  # precipitation, mm
    weather_code: int | None = Field(default=None)  # WMO code (the Weather Event)
    wind_speed_kmh: float | None = Field(default=None)  # wind_speed_10m, km/h
    is_day: int | None = Field(default=None)  # 1 day / 0 night
