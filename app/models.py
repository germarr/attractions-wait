"""SQLModel definitions for the wait-time dataset.

Three core tables: two small dimension tables (Park, Attraction) and the
Reading fact table that holds one row per Attraction per poll.
"""

from __future__ import annotations

from sqlmodel import Field, SQLModel


class Destination(SQLModel, table=True):
    """A top-level resort the app polls (e.g. Universal Orlando, Walt Disney World)."""

    id: str = Field(primary_key=True)  # themeparks.wiki uuid
    name: str
    # UTC ISO8601 of the last /children fetch that seeded Park + Attraction
    # coordinates. Drives the ~6-hourly refresh in collector._ensure_geo; NULL
    # means "never fetched", which forces a seed on the next poll.
    geo_fetched_at: str | None = Field(default=None)


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
    # Seeded from /children, like Park's. Storing the two coordinates is how the
    # dataset holds every pairwise distance: n points encode n(n-1)/2 distances
    # exactly, so there is no distance table to build or invalidate (ADR-0008).
    latitude: float | None = Field(default=None)
    longitude: float | None = Field(default=None)


class AttractionDistance(SQLModel, table=True):
    """Walking distance between two Attractions in the same Park (ADR-0009).

    Materialized, unlike the straight-line distance of ADR-0008: a routed
    distance is *not* derivable from the two coordinates — it comes from a
    Dijkstra over an OpenStreetMap footpath graph — so recomputing it per query
    is not the microsecond operation haversine is.

    One row per unordered pair, keyed with `attraction_a_id < attraction_b_id`
    so a pair is stored exactly once; `stats._WALK_BOTH_WAYS` unions the mirror
    image back in for origin-anchored queries.

    Two distinct kinds of "no answer", deliberately not collapsed:
      * **row absent** — no footpath connects the pair at all.
      * **`walk_meters` NULL** — a route was found and rejected as implausible
        (see `walking.SUSPECT_RATIO`), which in practice means an unmapped
        connection in OSM. The row is kept so the rejection stays queryable
        without a rebuild, and so a park's row count still equals n(n-1)/2 —
        the invariant that catches rides stranded on disconnected path stubs.
    Every query filters `walk_meters IS NOT NULL`, so both read as "unknown".

    Straight-line distance is deliberately NOT stored alongside: it is one
    haversine over columns we already have, and ADR-0008's rule is that cheaply
    derivable values do not earn a column.
    """

    attraction_a_id: str = Field(foreign_key="attraction.id", primary_key=True)
    attraction_b_id: str = Field(foreign_key="attraction.id", primary_key=True)
    park_id: str = Field(foreign_key="park.id", index=True)
    walk_meters: float | None = None  # NULL = routed, rejected as implausible
    built_at: str  # UTC ISO8601, provenance


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


class Show(SQLModel, table=True):
    """A scheduled performance entity: a parade, a fireworks show, a stage act.

    A sibling of Attraction rather than a row in it, because an Attraction is
    defined by reporting a standby wait and a Show never does — it has showtimes
    instead. Collapsing the two would mean either an Attraction with a permanently
    NULL wait or a type column that every wait query has to remember to filter on.

    Coordinates come from /children like an Attraction's, so a Show can be placed
    on the park map and routed to. They are NOT in `attractiondistance`: the
    planner aliases each show to the nearest Attraction instead (see
    `research_project.config.SHOWS`), which costs 20-32 m of error against the
    park's 403 m average leg and needs no new footpath routing.
    """

    id: str = Field(primary_key=True)  # themeparks.wiki uuid
    name: str
    park_id: str = Field(foreign_key="park.id", index=True)
    last_seen: str  # UTC ISO8601 of the most recent poll that saw this show
    latitude: float | None = Field(default=None)
    longitude: float | None = Field(default=None)


class ShowTime(SQLModel, table=True):
    """One published performance of a Show on one park-local date.

    **Never pruned.** Raw Readings are discarded after RETENTION_DAYS because the
    hourly rollups preserve what matters; a showtime has no rollup and no other
    copy, and the upstream feed publishes only *today* — `/entity/{id}/schedule`
    returns nothing for a show. So this table is the only record that will ever
    exist of when the fireworks actually started, and losing a row loses it for
    good. `app/retention.py` is asserted by test to leave it alone.

    `kind` preserves the feed's own `showtimes[].type`, because the values mean
    genuinely different things: "Performance Time" is a real scheduled show,
    "Operating" is a continuously-open meet-and-greet whose "showtime" is just its
    opening hours, and "Special Ticketed Event" belongs to a party the regular
    ticket does not admit you to. Only the first is a show you can plan around.
    """

    show_id: str = Field(foreign_key="show.id", primary_key=True)
    date: str = Field(primary_key=True)  # YYYY-MM-DD, park-local
    start_time: str = Field(primary_key=True)  # ISO8601 with park offset
    end_time: str | None = Field(default=None)
    kind: str  # the feed's showtimes[].type, verbatim
    first_seen: str  # UTC ISO8601 of the poll that first published this performance
