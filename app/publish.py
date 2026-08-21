"""Publish finished results into the Serving Store (ADR-0007).

The collector host stays the sole source of truth: every statistic is computed
here by app/stats.py and pushed as a finished payload. Postgres performs no
arithmetic, and app/serve.py only ever SELECTs what this module wrote.

Two passes:

    python -m app.publish --live     every 5 min, offset past the collector
    python -m app.publish --daily    after the nightly rollup

Each pass writes inside a single transaction and stamps pub.watermark LAST, so
the site can never observe a torn state and the watermark can never advance
ahead of the data it describes.
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from app import pgstore, stats

# The three get_day_summary blocks that ship nightly instead of every five
# minutes: 97% of the payload, and one partial day moves them imperceptibly.
SLOW_BLOCKS = ("correlation", "compare", "weekday")

COMPARE_REF_KEYS = {"yesterday": 1, "wow": 7, "mom": 28}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _targets() -> list[tuple[str, str]]:
    """Every (target_id, park_id) the dashboard offers — 142 Attractions + 7 Park Averages.

    Taken from get_attractions() so the published set is exactly what the UI
    can select, rather than the Attraction table's ghost-inclusive contents.
    """
    out: list[tuple[str, str]] = []
    for group in stats.get_attractions():
        for opt in group["options"]:
            out.append((opt["id"], group["park_id"]))
    return out


# ── Live pass ─────────────────────────────────────────────────────────────

def _live_row(target: str, park_id: str) -> tuple:
    return (
        target,
        park_id,
        Jsonb(stats.get_stats(target)),
        Jsonb(stats.get_day_live(target)),
        Jsonb(stats.get_day_summary(target, include_slow=False)),
        Jsonb(stats.get_recent(target)),
        Jsonb(stats.get_series(target, "today")),
        Jsonb(stats.get_series(target, "week")),
        Jsonb(stats.get_series(target, "month")),
    )


TARGET_LIVE_SQL = """
INSERT INTO pub.target_live
    (target_id, park_id, stats, day_live, day_summary, recent,
     series_today, series_week, series_month, observed_at, built_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (target_id) DO UPDATE SET
    park_id      = EXCLUDED.park_id,
    stats        = EXCLUDED.stats,
    day_live     = EXCLUDED.day_live,
    day_summary  = EXCLUDED.day_summary,
    recent       = EXCLUDED.recent,
    series_today = EXCLUDED.series_today,
    series_week  = EXCLUDED.series_week,
    series_month = EXCLUDED.series_month,
    observed_at  = EXCLUDED.observed_at,
    built_at     = EXCLUDED.built_at
"""

RELIABILITY_SQL = """
INSERT INTO pub.reliability (park_id, payload, observed_at, built_at)
VALUES (%s,%s,%s,%s)
ON CONFLICT (park_id) DO UPDATE SET
    payload = EXCLUDED.payload,
    observed_at = EXCLUDED.observed_at,
    built_at = EXCLUDED.built_at
"""

PARK_WINDOW_SQL = """
INSERT INTO pub.park_window (destination_id, window_key, payload, observed_at, built_at)
VALUES (%s,%s,%s,%s,%s)
ON CONFLICT (destination_id, window_key) DO UPDATE SET
    payload = EXCLUDED.payload,
    observed_at = EXCLUDED.observed_at,
    built_at = EXCLUDED.built_at
"""

WATERMARK_SQL = """
INSERT INTO pub.watermark (pass, observed_at, built_at, duration_ms, n_targets)
VALUES (%s,%s,%s,%s,%s)
ON CONFLICT (pass) DO UPDATE SET
    observed_at = EXCLUDED.observed_at,
    built_at = EXCLUDED.built_at,
    duration_ms = EXCLUDED.duration_ms,
    n_targets = EXCLUDED.n_targets
"""


def publish_live() -> dict:
    started = time.perf_counter()
    built_at = _now_utc()
    observed_at = stats._latest_observed_at()
    targets = _targets()

    rows = [(*_live_row(t, p), observed_at, built_at) for t, p in targets]

    # get_reliability is Park-scoped: it takes an attraction argument but
    # returns that Park's whole table. 7 payloads, not 149.
    park_ids = sorted({p for _, p in targets})
    rel_rows = [
        (pid, Jsonb(stats.get_reliability(f"park:{pid}")), observed_at, built_at)
        for pid in park_ids
    ]

    win_rows = [
        (d["id"], w, Jsonb(stats.get_park_comparison(d["id"], w)), observed_at, built_at)
        for d in stats.get_destinations()
        for w in ("today", "week", "month")
    ]

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    with pgstore.connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(TARGET_LIVE_SQL, rows)
            cur.executemany(RELIABILITY_SQL, rel_rows)
            cur.executemany(PARK_WINDOW_SQL, win_rows)
            cur.execute(WATERMARK_SQL, ("live", observed_at, built_at, elapsed_ms, len(rows)))
        conn.commit()

    return {
        "pass": "live", "targets": len(rows), "parks": len(rel_rows),
        "windows": len(win_rows), "observed_at": observed_at,
        "seconds": round(time.perf_counter() - started, 1),
    }


# ── Daily pass ────────────────────────────────────────────────────────────

def _compare_ref(target: str, now_local: datetime) -> dict:
    """The FIXED reference traces of get_day_summary's `compare` block.

    Stored as {"HH:MM": wait} maps rather than the endpoint's label-aligned
    arrays: today's trace grows all day, so the union of labels changes, but the
    reference days themselves never do. app/serve.py re-projects them against
    today's trace at read time.
    """
    out: dict[str, dict] = {}
    for key, days in COMPARE_REF_KEYS.items():
        ref_mid = (now_local - timedelta(days=days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        s = stats._raw_minute_series(
            target,
            ref_mid.astimezone(stats.UTC).isoformat(),
            (ref_mid + timedelta(days=1)).astimezone(stats.UTC).isoformat(),
        )
        out[key] = {
            ts.strftime("%H:%M"): (round(float(v), 1) if v == v else None)
            for ts, v in s.items()
        }
    return out


TARGET_DAILY_SQL = """
INSERT INTO pub.target_daily (target_id, compare_ref, weekday, correlation, built_at)
VALUES (%s,%s,%s,%s,%s)
ON CONFLICT (target_id) DO UPDATE SET
    compare_ref = EXCLUDED.compare_ref,
    weekday     = EXCLUDED.weekday,
    correlation = EXCLUDED.correlation,
    built_at    = EXCLUDED.built_at
"""

SERIES_DAILY_SQL = """
INSERT INTO pub.series_daily (target_id, date, mean, median, std, n, temp, rain, built_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (target_id, date) DO UPDATE SET
    mean = EXCLUDED.mean, median = EXCLUDED.median, std = EXCLUDED.std,
    n = EXCLUDED.n, temp = EXCLUDED.temp, rain = EXCLUDED.rain,
    built_at = EXCLUDED.built_at
"""

DIMENSIONS_SQL = """
INSERT INTO pub.dimensions (key, payload, built_at) VALUES (%s,%s,%s)
ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload, built_at = EXCLUDED.built_at
"""


def publish_daily() -> dict:
    started = time.perf_counter()
    built_at = _now_utc()
    now_local = stats._now_local()
    observed_at = stats._latest_observed_at()
    targets = _targets()

    # One month-window weather map per Park, reused across that Park's targets.
    # _wait_weather_corr reloads the Park's whole month of WeatherReadings on
    # every call; without memoising, seven Parks get fetched 149 times.
    month_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=29)
    wx_cache: dict[str, dict] = {}

    daily_rows: list[tuple] = []
    target_rows: list[tuple] = []
    for target, park_id in targets:
        if park_id not in wx_cache:
            wx_cache[park_id] = stats._weather_buckets(park_id, "month", now_local)
        wx = wx_cache[park_id]

        target_rows.append((
            target,
            Jsonb(_compare_ref(target, now_local)),
            Jsonb(stats._weekday_means(target, now_local)),
            Jsonb(stats._wait_weather_corr(target, park_id, month_start)),
            built_at,
        ))

        # Durable per-day record. Weather is only carried where the 30-day
        # window supplies it; older dates keep NULL temp/rain.
        for date, r in stats._daily_rollup_map(target).items():
            w = wx.get(date, {})
            daily_rows.append((
                target, date, r.get("mean"), r.get("median"), r.get("std"),
                r.get("n"), w.get("temp"), w.get("rain"), built_at,
            ))

    dim_rows = [
        ("attractions", Jsonb(stats.get_attractions()), built_at),
        ("destinations", Jsonb(stats.get_destinations()), built_at),
    ]

    elapsed_ms = int((time.perf_counter() - started) * 1000)
    with pgstore.connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(TARGET_DAILY_SQL, target_rows)
            cur.executemany(SERIES_DAILY_SQL, daily_rows)
            cur.executemany(DIMENSIONS_SQL, dim_rows)
            cur.execute(WATERMARK_SQL, ("daily", observed_at, built_at, elapsed_ms, len(target_rows)))
        conn.commit()

    return {
        "pass": "daily", "targets": len(target_rows), "series_days": len(daily_rows),
        "observed_at": observed_at, "seconds": round(time.perf_counter() - started, 1),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--live", action="store_true", help="intraday pass (every 5 min)")
    g.add_argument("--daily", action="store_true", help="nightly pass (after rollup)")
    ap.add_argument("--schema", action="store_true", help="apply DDL first")
    args = ap.parse_args()

    if args.schema:
        pgstore.apply_schema()
    result = publish_live() if args.live else publish_daily()
    print(f"[{_now_utc().isoformat(timespec='seconds')}] {result}")


if __name__ == "__main__":
    main()
