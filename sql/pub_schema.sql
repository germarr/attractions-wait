-- Serving Store schema (ADR-0007).
--
-- Everything here is a *finished result* computed on the collector host and
-- pushed by app/publish.py. Postgres performs no arithmetic: every read in
-- app/serve.py is a flat SELECT of a payload, and the cloud reader never
-- recomputes a statistic.
--
-- Two passes fill it (see CONTEXT.md):
--   Live Publish   every 5 min  -> target_live, reliability, park_window, watermark
--   Daily Publish  after rollup -> target_daily, series_daily, dimensions, watermark
--
-- Target keys use the app's existing format (_parse_target, app/stats.py:89):
-- a bare uuid for an Attraction, 'park:<uuid>' for a Park Average.

CREATE SCHEMA IF NOT EXISTS pub;

-- Freshness stamp, one row per pass. Written LAST in each pass's transaction so
-- it can never advance ahead of the data it describes.
CREATE TABLE IF NOT EXISTS pub.watermark (
    pass         text PRIMARY KEY,              -- 'live' | 'daily'
    observed_at  timestamptz,                   -- newest Reading included
    built_at     timestamptz NOT NULL,
    duration_ms  integer,
    n_targets    integer
);

-- Dimension payloads: the attraction roster and destination list.
CREATE TABLE IF NOT EXISTS pub.dimensions (
    key      text PRIMARY KEY,                  -- 'attractions' | 'destinations'
    payload  jsonb NOT NULL,
    built_at timestamptz NOT NULL
);

-- Per-target payloads that move within the day.
-- day_summary holds only the LIVE half of get_day_summary (current, means,
-- deltas, downtime); compare/weekday/correlation live in target_daily because
-- they are 97% of the payload and shift imperceptibly in one partial day.
CREATE TABLE IF NOT EXISTS pub.target_live (
    target_id     text PRIMARY KEY,
    park_id       text,                         -- resolves target -> reliability row
    stats         jsonb,
    day_live      jsonb,
    day_summary   jsonb,
    recent        jsonb,
    series_today  jsonb,
    series_week   jsonb,
    series_month  jsonb,
    observed_at   timestamptz,
    built_at      timestamptz NOT NULL
);

-- Per-target payloads too slow-moving for one partial day to shift.
-- compare_ref holds the FIXED reference traces as {"HH:MM": wait} maps; the
-- cloud reader unions their labels with today's trace (already published in
-- target_live.day_live) to rebuild get_day_summary's `compare` block.
CREATE TABLE IF NOT EXISTS pub.target_daily (
    target_id   text PRIMARY KEY,
    compare_ref jsonb,                          -- {yesterday:{}, wow:{}, mom:{}}
    weekday     jsonb,
    correlation jsonb,
    built_at    timestamptz NOT NULL
);

-- get_reliability is Park-scoped: it takes an attraction argument but returns
-- that Park's whole table. 7 payloads, not 149.
CREATE TABLE IF NOT EXISTS pub.reliability (
    park_id     text PRIMARY KEY,
    payload     jsonb NOT NULL,
    observed_at timestamptz,
    built_at    timestamptz NOT NULL
);

-- get_park_comparison is per Destination x window. 6 payloads.
CREATE TABLE IF NOT EXISTS pub.park_window (
    destination_id text,
    window_key     text,                        -- 'today' | 'week' | 'month'
    payload        jsonb NOT NULL,
    observed_at    timestamptz,
    built_at       timestamptz NOT NULL,
    PRIMARY KEY (destination_id, window_key)
);

-- The durable per-day record. Not required to serve today's three windows
-- (those ship complete in target_live), but kept for every finalized day so a
-- longer chart window later is a frontend change, not a migration + backfill.
CREATE TABLE IF NOT EXISTS pub.series_daily (
    target_id text,
    date      date,
    mean      double precision,
    median    double precision,
    std       double precision,
    n         integer,
    temp      double precision,
    rain      double precision,
    built_at  timestamptz NOT NULL,
    PRIMARY KEY (target_id, date)
);
CREATE INDEX IF NOT EXISTS ix_pub_series_daily_date ON pub.series_daily (date);
