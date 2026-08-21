"""Wait-time aggregation.

All bucketing happens in park-local time (America/New_York) even though
Readings are stored in UTC. SQLite has no median/stddev, so we pull the window's
rows and aggregate with pandas.

A "target" is either a single attraction (`<uuid>`) or a Park Average
(`park:<park_uuid>`) — the synthetic per-park mean wait.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
from sqlmodel import Session, select

from app.db import DB_PATH, engine
from app.models import (
    Attraction,
    AttractionDaily,
    AttractionHourBaseline,
    AttractionHourly,
    Destination,
    Park,
    ParkCorrelation,
    ParkDaily,
    ParkSchedule,
    Reading,
    WeatherReading,
)

# Statuses that count as Downtime when they occur during park operating hours.
DOWNTIME_STATUSES = {"DOWN", "CLOSED"}

# Minutes each Reading represents — the collector's cron period (ADR-0006).
# Absolute durations (down_minutes) must weight rows by this; ratios (Downtime
# Rate) are interval-independent and must NOT. Also caps the gap-derived weight
# so a missed poll can't inflate a duration.
POLL_INTERVAL_MINUTES = 5

# Sentinel "all history" start for trailing-historic windows.
EPOCH = "1970-01-01T00:00:00+00:00"
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

PARK_TZ = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# How many park-local days each window spans (today counts as 1).
WINDOW_DAYS = {"today": 1, "week": 7, "month": 30}

# WMO weather codes that count as rain: drizzle, rain, freezing rain, rain
# showers, and thunderstorms. (Snow codes 71-77 / 85-86 excluded — Orlando.)
RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}


def _is_raining(weather_code, precipitation_mm) -> bool:
    """Derived 'Raining' flag — a rain Weather Event or measured precipitation."""
    if weather_code in RAIN_CODES:
        return True
    return (precipitation_mm or 0) > 0


def _park_id_for_target(target: str) -> str | None:
    """The park whose weather applies to this target (attraction or Park Average)."""
    attraction_id, park_id = _parse_target(target)
    if park_id is not None:
        return park_id
    with Session(engine) as session:
        attraction = session.get(Attraction, attraction_id)
        return attraction.park_id if attraction else None


def _now_local() -> datetime:
    return datetime.now(tz=UTC).astimezone(PARK_TZ)


def _window_start_utc(window: str, now_local: datetime) -> str:
    """Park-local start of the window, expressed as a UTC ISO string for SQL."""
    days = WINDOW_DAYS[window]
    midnight_today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = midnight_today - timedelta(days=days - 1)
    return start_local.astimezone(UTC).isoformat()


def _parse_target(target: str) -> tuple[str | None, str | None]:
    """Return (attraction_id, park_id); exactly one is set."""
    if target.startswith("park:"):
        return None, target[len("park:") :]
    return target, None


def _operating_windows(park_id: str | None, dates: list[str]) -> dict:
    """{date: (opening_dt, closing_dt)} from OPERATING schedules for the park."""
    if not park_id:
        return {}
    with Session(engine) as session:
        rows = session.exec(
            select(
                ParkSchedule.date,
                ParkSchedule.opening_time,
                ParkSchedule.closing_time,
            )
            .where(ParkSchedule.park_id == park_id)
            .where(ParkSchedule.type == "OPERATING")
            .where(ParkSchedule.date.in_(dates))
        ).all()
    out: dict = {}
    for date, opening, closing in rows:
        if opening and closing:
            out[date] = (
                datetime.fromisoformat(opening),
                datetime.fromisoformat(closing),
            )
    return out


def _within_operating_hours(df: pd.DataFrame, park_id: str | None) -> pd.DataFrame:
    """Keep only readings inside their park's Operating Hours for that date.

    Query-time filter only — all data stays stored. The API echoes stale waits
    for hours after a park closes; this drops those so charts/cards reflect
    "while the park was open". Dates with no stored OPERATING schedule are kept
    as-is, so coverage gaps never blank a chart.
    """
    if not park_id or df.empty:
        return df
    # Work per distinct local date (~tens), not per row (six figures): strftime
    # and the window lookup happen once per day, then map back onto the frame.
    day = df["observed_at"].dt.normalize()
    uniq_days = pd.DatetimeIndex(day.dropna().unique())
    date_strs = uniq_days.strftime("%Y-%m-%d").tolist()
    windows = _operating_windows(park_id, date_strs)
    if not windows:
        return df
    op_by_day, cl_by_day = {}, {}
    for ts, ds in zip(uniq_days, date_strs):
        w = windows.get(ds)
        if w:
            op_by_day[ts] = pd.Timestamp(w[0]).tz_convert("UTC")
            cl_by_day[ts] = pd.Timestamp(w[1]).tz_convert("UTC")
    op = pd.to_datetime(day.map(op_by_day), utc=True)
    cl = pd.to_datetime(day.map(cl_by_day), utc=True)
    obs = df["observed_at"].dt.tz_convert("UTC")
    keep = op.isna() | ((obs >= op) & (obs < cl))
    return df[keep]


def _load_minute_series(target: str, start_utc: str) -> pd.Series:
    """Per-minute wait values for the target since start, indexed by local time.

    Closed/NULL readings are dropped. For a Park Average, each minute is the
    mean wait across that park's operating attractions at that minute.
    """
    attraction_id, park_id = _parse_target(target)

    with Session(engine) as session:
        if park_id is not None:
            stmt = (
                select(Reading.observed_at, Reading.wait_time)
                .join(Attraction, Attraction.id == Reading.attraction_id)
                .where(Attraction.park_id == park_id)
                .where(Reading.observed_at >= start_utc)
                .where(Reading.wait_time != None)  # noqa: E711
            )
        else:
            stmt = (
                select(Reading.observed_at, Reading.wait_time)
                .where(Reading.attraction_id == attraction_id)
                .where(Reading.observed_at >= start_utc)
                .where(Reading.wait_time != None)  # noqa: E711
            )
        rows = session.exec(stmt).all()

    if not rows:
        return pd.Series(dtype="float64")

    df = pd.DataFrame(rows, columns=["observed_at", "wait_time"])
    df["observed_at"] = pd.to_datetime(df["observed_at"], utc=True).dt.tz_convert(
        PARK_TZ
    )

    park = park_id if park_id is not None else _park_id_for_target(target)
    df = _within_operating_hours(df, park)
    if df.empty:
        return pd.Series(dtype="float64")

    if park_id is not None:
        # Collapse the park's attractions into one mean wait per poll minute.
        series = df.groupby("observed_at")["wait_time"].mean()
    else:
        series = df.set_index("observed_at")["wait_time"]

    return series.sort_index()


def get_weather_now(target: str) -> dict | None:
    """Latest Weather Reading for the target's park, with derived 'raining'."""
    park_id = _park_id_for_target(target)
    if park_id is None:
        return None
    with Session(engine) as session:
        w = session.exec(
            select(WeatherReading)
            .where(WeatherReading.park_id == park_id)
            .order_by(WeatherReading.observed_at.desc())
            .limit(1)
        ).first()
    if w is None:
        return None
    return {
        "temperature_c": w.temperature_c,
        "weather_code": w.weather_code,
        "wind_speed_kmh": w.wind_speed_kmh,
        "is_day": w.is_day,
        "raining": _is_raining(w.weather_code, w.precipitation_mm),
        "as_of": w.observed_at,
    }


def _weather_buckets(park_id: str, window: str, now_local: datetime) -> dict:
    """{bucket_label: {temp, rain}} for the park over the window.

    `temp` is the mean temperature in the bucket; `rain` is the fraction of
    minutes in the bucket that were Raining (0–1), used to shade the chart.
    """
    if park_id is None:
        return {}
    start_utc = _window_start_utc(window, now_local)
    with Session(engine) as session:
        rows = session.exec(
            select(
                WeatherReading.observed_at,
                WeatherReading.temperature_c,
                WeatherReading.weather_code,
                WeatherReading.precipitation_mm,
            )
            .where(WeatherReading.park_id == park_id)
            .where(WeatherReading.observed_at >= start_utc)
        ).all()
    if not rows:
        return {}

    df = pd.DataFrame(
        rows, columns=["observed_at", "temp", "code", "precip"]
    )
    df["observed_at"] = pd.to_datetime(df["observed_at"], utc=True).dt.tz_convert(
        PARK_TZ
    )
    df["raining"] = [
        _is_raining(c, p) for c, p in zip(df["code"], df["precip"])
    ]

    if window == "today":
        keys = df["observed_at"].dt.floor("h")
        label_fmt = lambda ts: ts.strftime("%H:%M")  # noqa: E731
    else:
        keys = df["observed_at"].dt.normalize()
        label_fmt = lambda ts: ts.strftime("%Y-%m-%d")  # noqa: E731

    out: dict = {}
    for bucket_ts, grp in df.groupby(keys):
        temp = grp["temp"].dropna()
        out[label_fmt(bucket_ts)] = {
            "temp": round(float(temp.mean()), 1) if len(temp) else None,
            "rain": round(float(grp["raining"].mean()), 2),
        }
    return out


def get_recent(target: str, minutes: int = 90) -> list[dict]:
    """Raw, unbucketed Wait Time per minute over a rolling recent window.

    The Live Trace: one point per polled minute (no aggregation), park-local
    times. Closed/DOWN minutes are emitted with `wait = null` (NOT dropped) so
    the chart can break the line there instead of connecting across a closure.
    For a Park Average target, each minute is the mean across the park's
    operating attractions (null only if none were operating that minute).
    """
    start_utc = (datetime.now(tz=UTC) - timedelta(minutes=minutes)).isoformat()
    attraction_id, park_id = _parse_target(target)

    with Session(engine) as session:
        if park_id is not None:
            stmt = (
                select(Reading.observed_at, Reading.wait_time)
                .join(Attraction, Attraction.id == Reading.attraction_id)
                .where(Attraction.park_id == park_id)
                .where(Reading.observed_at >= start_utc)
            )
        else:
            stmt = (
                select(Reading.observed_at, Reading.wait_time)
                .where(Reading.attraction_id == attraction_id)
                .where(Reading.observed_at >= start_utc)
            )
        rows = session.exec(stmt).all()

    if not rows:
        return []

    df = pd.DataFrame(rows, columns=["observed_at", "wait_time"])
    df["observed_at"] = pd.to_datetime(df["observed_at"], utc=True).dt.tz_convert(
        PARK_TZ
    )
    df = _within_operating_hours(
        df, park_id if park_id is not None else _park_id_for_target(target)
    )
    if df.empty:
        return []
    # One value per polled minute: mean of operating waits (NaN if all closed).
    g = df.groupby("observed_at")["wait_time"].mean().sort_index()
    return [
        {"t": ts.strftime("%H:%M"), "wait": round(float(v), 1) if pd.notna(v) else None}
        for ts, v in g.items()
    ]


def _operating_row_today(park_id: str | None):
    """The OPERATING ParkSchedule row for the park today, or None."""
    if park_id is None:
        return None
    today = _now_local().strftime("%Y-%m-%d")
    with Session(engine) as session:
        return session.exec(
            select(ParkSchedule)
            .where(ParkSchedule.park_id == park_id)
            .where(ParkSchedule.date == today)
            .where(ParkSchedule.type == "OPERATING")
        ).first()


def get_park_hours(target: str) -> dict | None:
    """Today's operating hours for the target's park (for header display)."""
    row = _operating_row_today(_park_id_for_target(target))
    if row is None or not row.opening_time or not row.closing_time:
        return None
    opening = datetime.fromisoformat(row.opening_time)
    closing = datetime.fromisoformat(row.closing_time)
    now = datetime.now(tz=UTC)
    return {
        "opening": opening.strftime("%H:%M"),  # park-local
        "closing": closing.strftime("%H:%M"),
        "is_open_now": opening <= now < closing,
    }


def get_downtime(target: str) -> dict:
    """Today's downtime for the target — DOWN or CLOSED *during park hours*.

    Park operating hours (from ParkSchedule) sharpen this: a non-operating
    minute only counts when the park was open, so overnight/closed-park minutes
    are excluded. Without a schedule we fall back to DOWN-only (any time).

    `down_minutes` is true elapsed downtime minutes today (summed across a
    Park's attractions), derived from gaps between readings rather than a row
    count. `outages` counts distinct downtime episodes (per attraction).
    `currently_down` is how many of the target's attractions are unavailable now.
    """
    now_local = _now_local()
    start_utc = _window_start_utc("today", now_local)
    attraction_id, park_id = _parse_target(target)
    schedule = _operating_row_today(_park_id_for_target(target))

    with Session(engine) as session:
        if park_id is not None:
            stmt = (
                select(Reading.observed_at, Reading.attraction_id, Reading.status)
                .join(Attraction, Attraction.id == Reading.attraction_id)
                .where(Attraction.park_id == park_id)
                .where(Reading.observed_at >= start_utc)
            )
        else:
            stmt = (
                select(Reading.observed_at, Reading.attraction_id, Reading.status)
                .where(Reading.attraction_id == attraction_id)
                .where(Reading.observed_at >= start_utc)
            )
        rows = session.exec(stmt).all()

    if not rows:
        return {"down_minutes": 0, "outages": 0, "currently_down": 0, "is_park": park_id is not None}

    df = pd.DataFrame(rows, columns=["observed_at", "attraction_id", "status"])
    ts = pd.to_datetime(df["observed_at"], utc=True)

    if schedule and schedule.opening_time and schedule.closing_time:
        opening = pd.Timestamp(datetime.fromisoformat(schedule.opening_time)).tz_convert(UTC)
        closing = pd.Timestamp(datetime.fromisoformat(schedule.closing_time)).tz_convert(UTC)
        in_hours = (ts >= opening) & (ts < closing)
        df["down"] = df["status"].isin(DOWNTIME_STATUSES) & in_hours
    else:
        # No schedule → fall back to unplanned DOWN at any time.
        df["down"] = df["status"] == "DOWN"

    latest = df["observed_at"].max()
    currently_down = int((df["down"] & (df["observed_at"] == latest)).sum())

    # Weight each down row by the real elapsed gap to that attraction's next
    # reading rather than counting rows, so a day that straddles a poll-interval
    # change still totals true minutes (ADR-0006). Clipped to
    # POLL_INTERVAL_MINUTES so a missed poll can't inflate the total, and the
    # final row of each group (no successor) is charged one interval.
    order = df.assign(_ts=ts).sort_values("_ts")
    down_total = 0.0
    outages = 0
    for _aid, grp in order.groupby("attraction_id"):
        gaps = (
            grp["_ts"].shift(-1).sub(grp["_ts"]).dt.total_seconds().div(60)
            .fillna(POLL_INTERVAL_MINUTES)
            .clip(lower=0, upper=POLL_INTERVAL_MINUTES)
        )
        down_total += float(gaps[grp["down"]].sum())
        prev = False
        for cur in grp["down"].tolist():
            if cur and not prev:
                outages += 1
            prev = cur
    down_minutes = int(round(down_total))

    return {
        "down_minutes": down_minutes,
        "outages": outages,
        "currently_down": currently_down,
        "is_park": park_id is not None,
    }


def get_series(target: str, window: str) -> list[dict]:
    """Bucketed mean/median/±1σ band for a chart, with weather overlay.

    today  → one bucket per hour of the local day
    week   → one bucket per local date (7 days)
    month  → one bucket per local date (30 days)

    Each bucket also carries `temp` (mean °C) and `rain` (0–1 fraction raining)
    for the target's park, aligned on the same bucket label.
    """
    now_local = _now_local()
    if window != "today":
        return _series_from_rollup(target, window, now_local)

    # today → one hourly bucket per local hour, fully live.
    series = _load_minute_series(target, _window_start_utc(window, now_local))
    if series.empty:
        return []
    keys = series.index.floor("h")
    weather = _weather_buckets(_park_id_for_target(target), window, now_local)

    out: list[dict] = []
    for bucket_ts, values in series.groupby(keys):
        label = bucket_ts.strftime("%H:%M")
        out.append(_series_bucket(
            label,
            float(values.mean()),
            float(values.median()),
            float(values.std(ddof=1)) if len(values) > 1 else 0.0,
            int(len(values)),
            weather.get(label, {}),
        ))
    return out


def _series_bucket(label, mean, median, std, n, wx) -> dict:
    return {
        "label": label,
        "mean": round(mean, 1),
        "median": round(median, 1),
        "std": round(std, 1),
        "upper": round(mean + std, 1),
        "lower": round(max(mean - std, 0.0), 1),
        "n": int(n),
        "temp": wx.get("temp"),
        "rain": wx.get("rain", 0.0),
    }


def _series_from_rollup(target: str, window: str, now_local: datetime) -> list[dict]:
    """week/month chart: one per-day bucket, from AttractionDaily/ParkDaily for
    finalized days plus today's live bucket. See docs/adr/0004."""
    dates = set(_window_dates(window, now_local))
    daily = _combined_daily_map(target, now_local)

    buckets: dict[str, tuple] = {}  # date -> (mean, median, std, n)
    for d in dates:
        r = daily.get(d)
        if r and r["n"] and r["mean"] is not None:
            buckets[d] = (r["mean"], r["median"], r["std"], r["n"])
    if not buckets:
        return []

    weather = _weather_buckets(_park_id_for_target(target), window, now_local)
    out: list[dict] = []
    for d in sorted(buckets):
        mean, median, std, n = buckets[d]
        out.append(_series_bucket(d, mean, median, std, n, weather.get(d, {})))
    return out


def _daily_rollup_map(target: str) -> dict:
    """{date: {n_down, n_readings, n, sum, mean, median, std}} of finalized per-day
    aggregates for a target. `n`/`sum` are minute-count/wait-sum in the target's
    native grain (Park Average minutes for a park, ride minutes for an attraction).
    """
    attraction_id, park_id = _parse_target(target)
    out: dict = {}
    with Session(engine) as session:
        if park_id is not None:
            rows = session.exec(
                select(
                    ParkDaily.date,
                    ParkDaily.n_down,
                    ParkDaily.n_readings,
                    ParkDaily.n_min,
                    ParkDaily.sum_minavg,
                    ParkDaily.mean_wait,
                    ParkDaily.median_wait,
                    ParkDaily.std_wait,
                ).where(ParkDaily.park_id == park_id)
            ).all()
        else:
            rows = session.exec(
                select(
                    AttractionDaily.date,
                    AttractionDaily.n_down,
                    AttractionDaily.n_readings,
                    AttractionDaily.n_wait,
                    AttractionDaily.sum_wait,
                    AttractionDaily.mean_wait,
                    AttractionDaily.median_wait,
                    AttractionDaily.std_wait,
                ).where(AttractionDaily.attraction_id == attraction_id)
            ).all()
    for d, nd, nr, n, s, mn, md, sd in rows:
        out[d] = {
            "n_down": nd,
            "n_readings": nr,
            "n": n,
            "sum": s,
            "mean": mn,
            "median": md,
            "std": sd,
        }
    return out


def _target_aids(target: str) -> list[str]:
    """Attraction ids a target reads over: one ride, or a park's whole roster."""
    attraction_id, park_id = _parse_target(target)
    if park_id is None:
        return [attraction_id]
    with Session(engine) as session:
        return [
            a
            for a in session.exec(
                select(Attraction.id).where(Attraction.park_id == park_id)
            ).all()
        ]


def _local_midnight_utc(local_date: str) -> str:
    """UTC-ISO of a park-local calendar date's 00:00."""
    d = date.fromisoformat(local_date)
    return datetime(d.year, d.month, d.day, tzinfo=PARK_TZ).astimezone(UTC).isoformat()


def _target_day_agg(g: pd.DataFrame, is_park: bool) -> dict:
    """Per-day aggregate for a target's one-day frame, in the same shape (and
    rounding) as the stored rollup — so a live-computed tail day is identical to
    what the nightly job would have written."""
    if is_park:
        from app.rollup import _park_day_agg  # lazy: import cycle

        pa = _park_day_agg(g)
        return {
            "n_down": pa["n_down"],
            "n_readings": pa["n_readings"],
            "n": pa["n_min"],
            "sum": pa["sum_minavg"],
            "mean": pa["mean_wait"],
            "median": pa["median_wait"],
            "std": pa["std_wait"],
        }
    n_readings = int(len(g))
    n_down = int(g["status"].isin(DOWNTIME_STATUSES).sum())
    w = g.dropna(subset=["wait_time"])["wait_time"]
    n = int(len(w))
    if n:
        return {
            "n_down": n_down,
            "n_readings": n_readings,
            "n": n,
            "sum": float(w.sum()),
            "mean": round(float(w.mean()), 4),
            "median": round(float(w.median()), 4),
            "std": round(float(w.std(ddof=1)), 4) if n > 1 else 0.0,
        }
    return {
        "n_down": n_down,
        "n_readings": n_readings,
        "n": 0,
        "sum": 0.0,
        "mean": None,
        "median": None,
        "std": None,
    }


def _live_daily_map(target: str, start_utc: str, now_local: datetime) -> dict:
    """Per-day aggregates computed live from raw readings since `start_utc`, keyed
    by park-local date. Bounded to the not-yet-rolled tail (usually just today)."""
    attraction_id, park_id = _parse_target(target)
    pid = park_id if park_id is not None else _park_id_for_target(target)
    df = _readings_frame(_target_aids(target), start_utc)
    if not df.empty:
        df = _within_operating_hours(df, pid)
    if df.empty:
        return {}
    out: dict = {}
    for d, g in df.groupby(df["observed_at"].dt.strftime("%Y-%m-%d")):
        out[d] = _target_day_agg(g, park_id is not None)
    return out


def _combined_daily_map(target: str, now_local: datetime) -> dict:
    """Finalized rollup days overlaid with a live-computed tail for every date
    after the last rolled one — so today *and* any day a missed/late nightly hasn't
    finalized are always present and correct. See docs/adr/0004."""
    roll = _daily_rollup_map(target)
    if roll:
        tail_start = (date.fromisoformat(max(roll)) + timedelta(days=1)).isoformat()
        start_utc = _local_midnight_utc(tail_start)
    else:
        start_utc = EPOCH  # no rollup yet (pre-backfill): compute all live
    roll.update(_live_daily_map(target, start_utc, now_local))
    return roll


def get_stats(target: str) -> dict:
    """Card values for the target, against today's local window.

    current wait is the latest operating reading; deltas are current minus the
    day's mean/median (positive = busier than typical today).
    """
    now_local = _now_local()
    series = _load_minute_series(target, _window_start_utc("today", now_local))
    weather = get_weather_now(target)
    downtime = get_downtime(target)
    park_hours = get_park_hours(target)

    if series.empty:
        return {
            "current": None,
            "mean": None,
            "median": None,
            "delta_mean": None,
            "delta_median": None,
            "as_of": None,
            "weather": weather,
            "downtime": downtime,
            "park_hours": park_hours,
        }

    current = float(series.iloc[-1])
    mean = float(series.mean())
    median = float(series.median())
    return {
        "current": round(current, 1),
        "mean": round(mean, 1),
        "median": round(median, 1),
        "delta_mean": round(current - mean, 1),
        "delta_median": round(current - median, 1),
        "as_of": series.index[-1].isoformat(),
        "weather": weather,
        "downtime": downtime,
        "park_hours": park_hours,
    }


def _short_destination(name: str) -> str:
    """Short resort label for selector grouping (e.g. 'Disney', 'Universal')."""
    if "Disney" in name:
        return "Disney"
    if "Universal" in name:
        return "Universal"
    return name.split()[0] if name else ""


CORR_LABELS = ["Wait", "Downtime", "Temp", "Rain"]


def _weather_correlation(park_id: str, df: pd.DataFrame, month_start) -> dict:
    """Pearson correlation among wait, downtime %, temp, precip (hourly obs)."""
    empty = {"labels": CORR_LABELS, "matrix": None, "n": 0}
    month_df = df[df["observed_at"] >= month_start]
    if month_df.empty:
        return empty

    hb = month_df["observed_at"].dt.floor("h")
    hd = month_df.groupby(hb).agg(
        d=("down", "sum"), c=("down", "count"), wait=("wait_time", "mean")
    )
    hd["downtime"] = 100 * hd["d"] / hd["c"]

    start_utc = month_start.astimezone(UTC).isoformat()
    with Session(engine) as session:
        wrows = session.exec(
            select(
                WeatherReading.observed_at,
                WeatherReading.temperature_c,
                WeatherReading.precipitation_mm,
            )
            .where(WeatherReading.park_id == park_id)
            .where(WeatherReading.observed_at >= start_utc)
        ).all()
    if not wrows:
        return empty

    wdf = pd.DataFrame(wrows, columns=["observed_at", "temp", "precip"])
    wdf["observed_at"] = pd.to_datetime(wdf["observed_at"], utc=True).dt.tz_convert(
        PARK_TZ
    )
    whb = wdf["observed_at"].dt.floor("h")
    wg = wdf.groupby(whb).agg(temp=("temp", "mean"), precip=("precip", "mean"))

    merged = pd.DataFrame(
        {
            "Wait": hd["wait"],
            "Downtime": hd["downtime"],
            "Temp": wg["temp"],
            "Rain": wg["precip"],
        }
    ).dropna()
    n = int(len(merged))
    if n < 2:
        return {"labels": CORR_LABELS, "matrix": None, "n": n}

    corr = merged[CORR_LABELS].corr(method="pearson")
    k = len(CORR_LABELS)
    matrix = [
        [
            round(float(corr.iloc[i, j]), 2) if pd.notna(corr.iloc[i, j]) else None
            for j in range(k)
        ]
        for i in range(k)
    ]
    return {"labels": CORR_LABELS, "matrix": matrix, "n": n}


def _rate_over(by_date: dict, dates) -> tuple:
    """(rounded Downtime Rate %, reading-count) summed across the given dates."""
    nd = nr = 0
    for d in dates:
        if d in by_date:
            a, b = by_date[d]
            nd += a
            nr += b
    return (round(100 * nd / nr, 1), nr) if nr else (None, 0)


def _std_z(prior_rates, today_rate) -> float | None:
    """z of today's rate vs a distribution of prior daily rates (ddof=1, like the
    original pandas .std()); None when <1 prior point or zero/NaN spread."""
    if today_rate is None or not prior_rates:
        return None
    ser = pd.Series(prior_rates, dtype="float64")
    std = ser.std()  # ddof=1
    if pd.isna(std) or std == 0:
        return None
    return round((today_rate - ser.mean()) / std, 2)


def _live_downtime_tail(park_id, aids, start_utc, by_attr, pby_date) -> None:
    """Overlay live per-attraction/park (n_down, n_readings) for every date since
    `start_utc` (the not-yet-rolled tail) onto the rollup maps. In-place."""
    from app.rollup import _attraction_day_aggs, _park_day_agg  # lazy: import cycle

    df = _readings_frame(aids, start_utc)
    if not df.empty:
        df = _within_operating_hours(df, park_id)
    if df.empty:
        return
    for d, g in df.groupby(df["observed_at"].dt.strftime("%Y-%m-%d")):
        daily, _ = _attraction_day_aggs(g)
        for aid, r in daily.items():
            by_attr.setdefault(aid, {})[d] = (r["n_down"], r["n_readings"])
        pa = _park_day_agg(g)
        if pa["n_readings"]:
            pby_date[d] = (pa["n_down"], pa["n_readings"])


def get_reliability(target: str) -> dict:
    """Per-attraction Downtime Rate across trailing windows + z-score + weather
    correlation, for the target's park. See CONTEXT.md (Downtime Rate, z-score).

    Windows combine precomputed per-day rollups (AttractionDaily / ParkDaily) with
    today's live bucket; the weather correlation is read from ParkCorrelation. No
    per-request full-history scan. See docs/adr/0004.
    """
    park_id = _park_id_for_target(target)
    base = {
        "park": None,
        "rows": [],
        "park_total": None,
        "correlation": {"labels": CORR_LABELS, "matrix": None, "n": 0},
    }
    if park_id is None:
        return base

    now_local = _now_local()
    today_str = now_local.date().isoformat()
    yest_str = (now_local.date() - timedelta(days=1)).isoformat()

    with Session(engine) as session:
        park = session.get(Park, park_id)
        attractions = session.exec(
            select(Attraction).where(Attraction.park_id == park_id)
        ).all()
        aids = [a.id for a in attractions]
        adaily = session.exec(
            select(
                AttractionDaily.attraction_id,
                AttractionDaily.date,
                AttractionDaily.n_down,
                AttractionDaily.n_readings,
            ).where(AttractionDaily.attraction_id.in_(aids))
        ).all() if aids else []
        pdaily = session.exec(
            select(ParkDaily.date, ParkDaily.n_down, ParkDaily.n_readings)
            .where(ParkDaily.park_id == park_id)
        ).all()
        corr_row = session.get(ParkCorrelation, park_id)

    base["park"] = park.name if park else None
    names = {a.id: a.name for a in attractions}

    # Per-attraction and park-pooled counts by park-local date: rollup (prior) …
    by_attr: dict[str, dict] = {}
    for aid, d, nd, nr in adaily:
        by_attr.setdefault(aid, {})[d] = (nd, nr)
    pby_date: dict[str, tuple] = {d: (nd, nr) for d, nd, nr in pdaily}

    # … plus a live tail for every date after the last rolled one (today, and any
    # day a missed/late nightly hasn't finalized).
    rolled = set(pby_date) | {d for m in by_attr.values() for d in m}
    tail_utc = (
        _local_midnight_utc((date.fromisoformat(max(rolled)) + timedelta(days=1)).isoformat())
        if rolled
        else EPOCH
    )
    _live_downtime_tail(park_id, aids, tail_utc, by_attr, pby_date)

    if not by_attr and not pby_date:
        return base  # no data at all

    all_dates = sorted(set(pby_date) | {d for m in by_attr.values() for d in m})
    window_dates = {
        "today": [today_str],
        "yesterday": [yest_str],
        "week": _window_dates("week", now_local),
        "month": _window_dates("month", now_local),
        "historic": all_dates,
    }
    month_prior = [d for d in _window_dates("month", now_local) if d != today_str]

    out_rows = []
    for aid, name in names.items():
        m = by_attr.get(aid, {})
        rates = {w: _rate_over(m, ds) for w, ds in window_dates.items()}
        prior_rates = [
            100 * m[d][0] / m[d][1] for d in month_prior if d in m and m[d][1]
        ]
        out_rows.append(
            {
                "attraction": name,
                "rates": {w: rates[w][0] for w in window_dates},
                "counts": {w: rates[w][1] for w in window_dates},
                "z": _std_z(prior_rates, rates["today"][0]),
            }
        )
    out_rows.sort(key=lambda r: (r["z"] is None, -(r["z"] or 0)))

    park_rates = {w: _rate_over(pby_date, ds) for w, ds in window_dates.items()}
    park_prior = [
        100 * pby_date[d][0] / pby_date[d][1]
        for d in month_prior
        if d in pby_date and pby_date[d][1]
    ]
    park_z = _std_z(park_prior, park_rates["today"][0]) if len(park_prior) >= 2 else None
    park_total = {
        "rates": {w: park_rates[w][0] for w in window_dates},
        "counts": {w: park_rates[w][1] for w in window_dates},
        "z": park_z,
    }

    if corr_row is not None:
        correlation = {
            "labels": json.loads(corr_row.labels_json),
            "matrix": json.loads(corr_row.matrix_json) if corr_row.matrix_json else None,
            "n": corr_row.n,
        }
    else:
        correlation = {"labels": CORR_LABELS, "matrix": None, "n": 0}

    return {
        "park": park.name,
        "rows": out_rows,
        "park_total": park_total,
        "correlation": correlation,
    }


def get_attractions() -> list[dict]:
    """Parks with their attractions, plus a Park Average entry per park.

    Grouped/ordered by Destination. Shaped for the selector:
    [{park, park_id, destination, options: [{id, name}]}].
    """
    with Session(engine) as session:
        dest_names = {
            d.id: d.name for d in session.exec(select(Destination)).all()
        }
        parks = session.exec(select(Park)).all()
        attractions = session.exec(
            select(Attraction).order_by(Attraction.name)
        ).all()

    by_park: dict[str, list[dict]] = {}
    for a in attractions:
        by_park.setdefault(a.park_id, []).append({"id": a.id, "name": a.name})

    def sort_key(p: Park) -> tuple[str, str]:
        return (_short_destination(dest_names.get(p.destination_id, "")), p.name)

    result: list[dict] = []
    for park in sorted(parks, key=sort_key):
        destination = _short_destination(dest_names.get(park.destination_id, ""))
        options = [{"id": f"park:{park.id}", "name": f"★ {park.name} (average)"}]
        options.extend(by_park.get(park.id, []))
        result.append(
            {
                "park": park.name,
                "park_id": park.id,
                "destination": destination,
                "options": options,
            }
        )
    return result


# ── Daily Performance page ────────────────────────────────────────────────

def _raw_minute_series(target: str, start_utc: str, end_utc: str | None = None):
    """Per-minute mean wait (NaN at closed minutes → gaps), operating-hours only."""
    attraction_id, park_id = _parse_target(target)
    with Session(engine) as session:
        stmt = select(Reading.observed_at, Reading.wait_time)
        if park_id is not None:
            stmt = stmt.join(
                Attraction, Attraction.id == Reading.attraction_id
            ).where(Attraction.park_id == park_id)
        else:
            stmt = stmt.where(Reading.attraction_id == attraction_id)
        stmt = stmt.where(Reading.observed_at >= start_utc)
        if end_utc:
            stmt = stmt.where(Reading.observed_at < end_utc)
        rows = session.exec(stmt).all()
    if not rows:
        return pd.Series(dtype="float64")
    df = pd.DataFrame(rows, columns=["observed_at", "wait_time"])
    df["observed_at"] = pd.to_datetime(df["observed_at"], utc=True).dt.tz_convert(PARK_TZ)
    df = _within_operating_hours(
        df, park_id if park_id is not None else _park_id_for_target(target)
    )
    if df.empty:
        return pd.Series(dtype="float64")
    return df.groupby("observed_at")["wait_time"].mean().sort_index()


def _park_temp_map(park_id: str | None, start_utc: str) -> dict:
    """{local minute Timestamp: temperature_c} for the park since start."""
    if park_id is None:
        return {}
    with Session(engine) as session:
        rows = session.exec(
            select(WeatherReading.observed_at, WeatherReading.temperature_c)
            .where(WeatherReading.park_id == park_id)
            .where(WeatherReading.observed_at >= start_utc)
        ).all()
    if not rows:
        return {}
    df = pd.DataFrame(rows, columns=["observed_at", "temp"])
    df["observed_at"] = pd.to_datetime(df["observed_at"], utc=True).dt.tz_convert(PARK_TZ)
    return df.groupby("observed_at")["temp"].mean().to_dict()


def _downtime_rate(target: str, start_utc: str):
    """Downtime % of operating time for the target over the window."""
    attraction_id, park_id = _parse_target(target)
    with Session(engine) as session:
        stmt = select(Reading.observed_at, Reading.status)
        if park_id is not None:
            stmt = stmt.join(
                Attraction, Attraction.id == Reading.attraction_id
            ).where(Attraction.park_id == park_id)
        else:
            stmt = stmt.where(Reading.attraction_id == attraction_id)
        rows = session.exec(stmt.where(Reading.observed_at >= start_utc)).all()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["observed_at", "status"])
    df["observed_at"] = pd.to_datetime(df["observed_at"], utc=True).dt.tz_convert(PARK_TZ)
    df = _within_operating_hours(
        df, park_id if park_id is not None else _park_id_for_target(target)
    )
    n = len(df)
    if not n:
        return None
    down = int(df["status"].isin(DOWNTIME_STATUSES).sum())
    return round(100 * down / n, 1)


def _wow_mom_yoy(target: str, now_local: datetime, current) -> dict:
    """% change of current wait vs the reference hour's mean N days ago."""
    out = {}
    for key, days in (("wow", 7), ("mom", 28), ("yoy", 364)):
        if current is None:
            out[key] = None
            continue
        ref = (now_local - timedelta(days=days)).replace(minute=0, second=0, microsecond=0)
        start = ref.astimezone(UTC).isoformat()
        end = (ref + timedelta(hours=1)).astimezone(UTC).isoformat()
        s = _raw_minute_series(target, start, end).dropna()
        if s.empty or float(s.mean()) == 0:
            out[key] = None
        else:
            out[key] = round((current - float(s.mean())) / float(s.mean()) * 100, 1)
    return out


def _compare_traces(target: str, now_local: datetime) -> dict:
    """Today's minute trace + yesterday and the same day-of-week 7/28 days ago, by time-of-day."""
    today_start = _window_start_utc("today", now_local)
    series = {}
    for key, days in (("today", 0), ("yesterday", 1), ("wow", 7), ("mom", 28)):
        if days == 0:
            s = _raw_minute_series(target, today_start)
        else:
            ref_mid = (now_local - timedelta(days=days)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            s = _raw_minute_series(
                target,
                ref_mid.astimezone(UTC).isoformat(),
                (ref_mid + timedelta(days=1)).astimezone(UTC).isoformat(),
            )
        series[key] = {
            ts.strftime("%H:%M"): (round(float(v), 1) if pd.notna(v) else None)
            for ts, v in s.items()
        }
    labels = sorted(set().union(*[set(d) for d in series.values()])) if series else []
    return {"labels": labels, **{k: [series[k].get(l) for l in labels] for k in series}}


def _wait_weather_corr(target: str, park_id: str | None, month_start: datetime) -> dict:
    labels = ["Wait", "Temp", "Rain"]
    empty = {"labels": labels, "matrix": None, "n": 0}
    s = _raw_minute_series(target, month_start.astimezone(UTC).isoformat()).dropna()
    if s.empty or park_id is None:
        return empty
    hourly_wait = s.groupby(s.index.floor("h")).mean()
    with Session(engine) as session:
        wrows = session.exec(
            select(
                WeatherReading.observed_at,
                WeatherReading.temperature_c,
                WeatherReading.precipitation_mm,
            )
            .where(WeatherReading.park_id == park_id)
            .where(WeatherReading.observed_at >= month_start.astimezone(UTC).isoformat())
        ).all()
    if not wrows:
        return empty
    wdf = pd.DataFrame(wrows, columns=["observed_at", "temp", "precip"])
    wdf["observed_at"] = pd.to_datetime(wdf["observed_at"], utc=True).dt.tz_convert(PARK_TZ)
    whb = wdf["observed_at"].dt.floor("h")
    wg = wdf.groupby(whb).agg(temp=("temp", "mean"), precip=("precip", "mean"))
    merged = pd.DataFrame(
        {"Wait": hourly_wait, "Temp": wg["temp"], "Rain": wg["precip"]}
    ).dropna()
    n = int(len(merged))
    if n < 2:
        return {"labels": labels, "matrix": None, "n": n}
    corr = merged[labels].corr(method="pearson")
    matrix = [
        [round(float(corr.iloc[i, j]), 2) if pd.notna(corr.iloc[i, j]) else None for j in range(3)]
        for i in range(3)
    ]
    return {"labels": labels, "matrix": matrix, "n": n}


def _weekday_means(target: str, now_local: datetime) -> dict:
    """Mean wait by weekday over all history (minute-pooled), from the per-day
    rollup + live tail. `n` is the number of distinct dates seen per weekday."""
    today_index = now_local.weekday()
    today_s = _load_minute_series(target, _window_start_utc("today", now_local))
    today_mean = round(float(today_s.mean()), 1) if not today_s.empty else None

    daily = _combined_daily_map(target, now_local)
    sums = [0.0] * 7
    ns = [0] * 7
    dcount = [0] * 7
    for d, r in daily.items():
        if r["n"]:
            wd = date.fromisoformat(d).weekday()
            sums[wd] += r["sum"]
            ns[wd] += r["n"]
            dcount[wd] += 1
    rows = [
        {
            "day": WEEKDAY_NAMES[w],
            "mean": round(sums[w] / ns[w], 1) if ns[w] else None,
            "n": dcount[w],
        }
        for w in range(7)
    ]
    return {"rows": rows, "today_index": today_index, "today_mean": today_mean}


def get_day_live(target: str) -> dict:
    """Fast-refreshing bits: current wait, WoW/MoM/YoY, today's minute trace + temp."""
    now_local = _now_local()
    today_start = _window_start_utc("today", now_local)
    today_series = _load_minute_series(target, today_start)
    current = round(float(today_series.iloc[-1]), 1) if not today_series.empty else None
    mean = round(float(today_series.mean()), 1) if not today_series.empty else None
    median = round(float(today_series.median()), 1) if not today_series.empty else None

    raw = _raw_minute_series(target, today_start)
    temp_map = _park_temp_map(_park_id_for_target(target), today_start)
    trace = [
        {
            "t": ts.strftime("%H:%M"),
            "wait": round(float(v), 1) if pd.notna(v) else None,
            "temp": round(float(temp_map[ts]), 1) if ts in temp_map and pd.notna(temp_map[ts]) else None,
        }
        for ts, v in raw.items()
    ]
    dt = get_downtime(target)
    return {
        "current": current,
        "mean": mean,
        "median": median,
        **_wow_mom_yoy(target, now_local, current),
        "trace": trace,
        "downtime": {
            "today": _downtime_rate(target, today_start),  # % of operating time
            "currently_down": dt["currently_down"],
            "is_park": dt["is_park"],
        },
        "park_hours": get_park_hours(target),
        "as_of": today_series.index[-1].isoformat() if not today_series.empty else None,
    }


def get_day_summary(target: str, include_slow: bool = True) -> dict:
    """Slow-refreshing bits: window means + deltas, downtime vs historic,
    correlation, period-comparison traces, weekday means.

    `include_slow=False` returns only the intraday-moving half (current, means,
    deltas, downtime), skipping correlation/compare/weekday. The Live Publish
    (ADR-0007) uses it: those three are 97% of the payload and shift
    imperceptibly in one partial day, so they ship in the Daily Publish instead.
    """
    now_local = _now_local()
    today_start = _window_start_utc("today", now_local)
    today_series = _load_minute_series(target, today_start)
    current = round(float(today_series.iloc[-1]), 1) if not today_series.empty else None

    # Windowed means / historic downtime from precomputed per-day rollups + a live
    # tail (today and any un-rolled recent day). Today's own mean/median stay live.
    daily = _combined_daily_map(target, now_local)

    def _mean_over(dates):
        s = n = 0
        for d in dates:
            r = daily.get(d)
            if r and r["n"]:
                s += r["sum"]
                n += r["n"]
        return round(s / n, 1) if n else None

    def _dt_over(dates):
        dn = tn = 0
        for d in dates:
            r = daily.get(d)
            if r:
                dn += r["n_down"]
                tn += r["n_readings"]
        return round(100 * dn / tn, 1) if tn else None

    means = {
        "mean": round(float(today_series.mean()), 1) if not today_series.empty else None,
        "median": round(float(today_series.median()), 1) if not today_series.empty else None,
        "week": _mean_over(_window_dates("week", now_local)),
        "month": _mean_over(_window_dates("month", now_local)),
        "historic": _mean_over(daily.keys()),
    }

    def _delta(m):
        return round(current - m, 1) if (current is not None and m is not None) else None

    dt_today = _downtime_rate(target, today_start)
    dt_hist = _dt_over(daily.keys())
    month_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=29)
    out = {
        "current": current,
        "means": means,
        "deltas": {k: _delta(v) for k, v in means.items()},
        "downtime": {
            "today": dt_today,
            "historic": dt_hist,
            "delta": round(dt_today - dt_hist, 1) if (dt_today is not None and dt_hist is not None) else None,
        },
    }
    if include_slow:
        out["correlation"] = _wait_weather_corr(target, _park_id_for_target(target), month_start)
        out["compare"] = _compare_traces(target, now_local)
        out["weekday"] = _weekday_means(target, now_local)
    return out


# ── Parks comparison page ─────────────────────────────────────────────────
# Compares a Destination's theme Parks side-by-side. Every comparative metric is
# size-normalized (rates / ratios), never a raw total — see
# docs/adr/0003-normalized-park-comparison.md. The window selector (today/week/
# month) drives the windowed metrics; the live strip (roster/open/down/momentum)
# always reflects "now".

# Absolute radar caps: the outer rim of each axis is a fixed real value, so a
# quiet day honestly draws small polygons. Crowd Index / Uptime / % open ride
# their natural 0–100(–200) scales and aren't listed here.
PARK_COMPARE_CAPS = {"avg_wait": 75, "headliner": 120, "crowd_index": 200, "roster": 50}

# Top-N longest queues that define a Park's Headliner Wait.
HEADLINER_N = 5


def get_destinations() -> list[dict]:
    """Destinations with at least one tracked Park — feeds the page's toggle."""
    with Session(engine) as session:
        dests = session.exec(select(Destination)).all()
        have = {p.destination_id for p in session.exec(select(Park)).all()}
    out = [
        {"id": d.id, "name": _short_destination(d.name), "full_name": d.name}
        for d in dests
        if d.id in have
    ]
    out.sort(key=lambda d: d["name"])
    return out


# Crowd Index baseline lookback. A rolling window (rather than literally all
# history) bounds the nightly AttractionHourBaseline rebuild as the DB grows; an
# hour-of-day mean is stable well inside it. With only days of data today this is
# every reading anyway. Weekday alignment remains the future refinement (see
# CONTEXT). The materialized baseline is read per request; today's hours are
# folded in live so it matches the all-history-including-today live baseline.
CROWD_BASELINE_DAYS = 90


def _latest_observed_at() -> str | None:
    """The most recent poll minute across all readings (collector writes one)."""
    with Session(engine) as session:
        return session.exec(
            select(Reading.observed_at)
            .order_by(Reading.observed_at.desc())
            .limit(1)
        ).first()


def _readings_frame(attraction_ids: list[str], since_utc: str) -> pd.DataFrame:
    """Bulk-read readings for the given attractions since `since_utc`, parsed to
    park-local time. Goes straight to a read-only SQLite connection with
    `read_sql` — the ORM is far too slow for this page's six-figure row counts.
    """
    cols = ["observed_at", "attraction_id", "wait_time", "status"]
    if not attraction_ids:
        return pd.DataFrame(columns=cols)
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        ph = ",".join("?" * len(attraction_ids))
        df = pd.read_sql_query(
            f"SELECT observed_at, attraction_id, wait_time, status FROM reading "
            f"WHERE attraction_id IN ({ph}) AND observed_at >= ?",
            conn,
            params=[*attraction_ids, since_utc],
        )
    finally:
        conn.close()
    if df.empty:
        return df
    df["observed_at"] = pd.to_datetime(df["observed_at"], utc=True).dt.tz_convert(PARK_TZ)
    return df


def _live_counts(raw_sub: pd.DataFrame, park_id: str, latest_ts) -> dict:
    """Live Park Roster snapshot from the latest poll — computed on the UNFILTERED
    park frame so the roster still shows every Attraction when the Park is closed
    overnight. "Down now" is DOWN/CLOSED within Operating Hours, else unplanned
    DOWN. Long-term REFURBISHMENT is excluded from the Roster (absent in current
    data, but honored for correctness).
    """
    snap = raw_sub[(raw_sub["observed_at"] == latest_ts) & (raw_sub["status"] != "REFURBISHMENT")]
    roster = int(len(snap))
    open_now = int((snap["status"] == "OPERATING").sum())

    sched = _operating_row_today(park_id)
    now = datetime.now(tz=UTC)
    in_hours = bool(
        sched
        and sched.opening_time
        and sched.closing_time
        and datetime.fromisoformat(sched.opening_time)
        <= now
        < datetime.fromisoformat(sched.closing_time)
    )
    if in_hours:
        down_now = int(snap["status"].isin(DOWNTIME_STATUSES).sum())
    else:
        down_now = int((snap["status"] == "DOWN").sum())
    return {"roster": roster, "open_now": open_now, "down_now": down_now}


def _momentum_from(sub: pd.DataFrame) -> float | None:
    """Live Momentum from an operating-hours-filtered park frame: median Park
    Average of the last ~10 min minus the median around 60 min ago, in minutes.
    """
    waits = sub.dropna(subset=["wait_time"])
    if waits.empty:
        return None
    s = waits.groupby("observed_at")["wait_time"].mean().sort_index()
    if s.empty:
        return None
    last = s.index[-1]
    recent = s[s.index >= last - pd.Timedelta(minutes=10)]
    center = last - pd.Timedelta(minutes=60)
    past = s[
        (s.index >= center - pd.Timedelta(minutes=5))
        & (s.index <= center + pd.Timedelta(minutes=5))
    ]
    if recent.empty or past.empty:
        return None
    return round(float(recent.median()) - float(past.median()), 1)


def _crowd_from(sub: pd.DataFrame, start_ts) -> float | None:
    """Crowd Index from an operating-hours-filtered park frame: mean of each
    window reading's wait ÷ that Attraction's hour-of-day baseline (over the whole
    frame), as a percentage. Per-attraction normalization removes roster bias.
    """
    waits = sub.dropna(subset=["wait_time"]).copy()
    if waits.empty:
        return None
    waits["hour"] = waits["observed_at"].dt.hour
    base = (
        waits.groupby(["attraction_id", "hour"])["wait_time"]
        .mean()
        .rename("baseline")
        .reset_index()
    )
    win = waits[waits["observed_at"] >= start_ts]
    if win.empty:
        return None
    m = win.merge(base, on=["attraction_id", "hour"], how="left")
    m = m[m["baseline"].notna() & (m["baseline"] > 0)]
    if m.empty:
        return None
    return round(float((m["wait_time"] / m["baseline"]).mean()) * 100, 1)


def _windowed_metrics(sub: pd.DataFrame, start_ts) -> dict:
    """avg wait, Headliner Wait, Crowd Index and Park Downtime Rate over the
    window, from an operating-hours-filtered park frame (all in memory)."""
    out = {
        "avg_wait": None,
        "headliner_wait": None,
        "crowd_index": _crowd_from(sub, start_ts),
        "downtime_rate": None,
        "uptime": None,
    }
    win = sub[sub["observed_at"] >= start_ts]
    if win.empty:
        return out

    n = len(win)
    down = int(win["status"].isin(DOWNTIME_STATUSES).sum())
    dr = round(100 * down / n, 1)
    out["downtime_rate"] = dr
    out["uptime"] = round(100 - dr, 1)

    waits = win.dropna(subset=["wait_time"])
    if not waits.empty:
        out["avg_wait"] = round(
            float(waits.groupby("observed_at")["wait_time"].mean().mean()), 1
        )
        # Headliner: mean of each minute's top-N waits. Vectorized via a
        # descending rank (cumcount) — far faster than a per-group nlargest apply
        # across the thousands of minutes a week/month window holds.
        ranked = waits.sort_values("wait_time", ascending=False)
        ranked = ranked.assign(_rk=ranked.groupby("observed_at").cumcount())
        top = ranked[ranked["_rk"] < HEADLINER_N]
        per_min_head = top.groupby("observed_at")["wait_time"].mean()
        out["headliner_wait"] = round(float(per_min_head.mean()), 1)
    return out


def _window_dates(window: str, now_local: datetime) -> list[str]:
    """Park-local date strings the window covers, today-inclusive (newest first)."""
    today = now_local.date()
    return [(today - timedelta(days=i)).isoformat() for i in range(WINDOW_DAYS[window])]


def _crowd_index_combined(park_aids, prior_hourly, today_hourly, base) -> float | None:
    """Crowd Index over the window from precomputed hour-of-day pieces.

    Reproduces the live mean-of-(wait ÷ baseline) exactly: each (attraction, hour)
    group contributes Σwait ÷ baseline to the numerator and its reading-count to
    the denominator. The baseline is the trailing-90d hour-of-day mean *including
    today* (materialized `base` + today's in-progress hours folded in via St/Nt),
    matching the live baseline which spans the same frame.
    """
    aidset = set(park_aids)
    groups: dict = {}  # (aid, hour) -> [S, N, S_today, N_today]
    for aid in park_aids:
        for hour, (s, n) in prior_hourly.get(aid, {}).items():
            g = groups.setdefault((aid, hour), [0.0, 0, 0.0, 0])
            g[0] += s
            g[1] += n
    for aid, hour, n_wait, sum_wait in today_hourly:
        if aid not in aidset:
            continue
        g = groups.setdefault((aid, hour), [0.0, 0, 0.0, 0])
        g[0] += sum_wait
        g[1] += n_wait
        g[2] += sum_wait
        g[3] += n_wait

    num, den = 0.0, 0
    for (aid, hour), (S, N, St, Nt) in groups.items():
        bs, bn = base.get((aid, hour), (0.0, 0))
        beff_n = bn + Nt
        if beff_n <= 0:
            continue
        beff = (bs + St) / beff_n
        if beff <= 0:
            continue
        num += S / beff
        den += N
    if den <= 0:
        return None
    return round(100 * num / den, 1)


def get_park_comparison(destination_id: str, window: str) -> dict:
    """Side-by-side metrics for every Park in a Destination over the window.

    Per park: live Roster/open/down/Momentum, plus windowed avg wait (mean of the
    Park Average), Headliner Wait, Crowd Index, and Park Downtime Rate (Uptime =
    100 − rate). The windowed metrics combine precomputed prior-day rollups with a
    bounded live read of the not-yet-rolled tail (today, and any day a missed
    nightly hasn't finalized) — the per-request scan no longer grows with retained
    history. See docs/adr/0004. `caps` carries the absolute radar-axis maxima.
    """
    from app.rollup import _attraction_day_aggs, _park_day_agg  # lazy: import cycle

    now_local = _now_local()
    window_dates = _window_dates(window, now_local)
    today_str = now_local.date().isoformat()
    candidate_prior = [d for d in window_dates if d < today_str]
    latest = _latest_observed_at()
    latest_ts = pd.Timestamp(latest).tz_convert(PARK_TZ) if latest else None

    with Session(engine) as session:
        dest = session.get(Destination, destination_id)
        parks = sorted(
            session.exec(
                select(Park).where(Park.destination_id == destination_id)
            ).all(),
            key=lambda p: p.name,
        )
        aid_to_park = {
            a.id: a.park_id
            for a in session.exec(
                select(Attraction)
                .join(Park, Park.id == Attraction.park_id)
                .where(Park.destination_id == destination_id)
            ).all()
        }
        # Prior-day park rollups for the window's finalized dates.
        pdaily: dict[str, list] = {}
        if candidate_prior:
            for row in session.exec(
                select(ParkDaily).where(ParkDaily.date.in_(candidate_prior))
            ).all():
                pdaily.setdefault(row.park_id, []).append(row)
        rolled = {r.date for rows in pdaily.values() for r in rows}
        # Crowd baseline (materialized) keyed by (attraction, hour).
        base: dict = {}
        for row in session.exec(
            select(AttractionHourBaseline).where(
                AttractionHourBaseline.attraction_id.in_(list(aid_to_park))
            )
        ).all():
            base[(row.attraction_id, row.hour)] = (row.sum_wait, row.n)
        # Prior-day hourly wait sums (Crowd numerator for finalized days).
        prior_hourly: dict[str, dict] = {}
        if rolled:
            for row in session.exec(
                select(AttractionHourly).where(
                    AttractionHourly.attraction_id.in_(list(aid_to_park)),
                    AttractionHourly.date.in_(list(rolled)),
                )
            ).all():
                d = prior_hourly.setdefault(row.attraction_id, {})
                s, n = d.get(row.hour, (0.0, 0))
                d[row.hour] = (s + row.sum_wait, n + row.n_wait)

    aids_by_park: dict[str, list] = {}
    for aid, pid in aid_to_park.items():
        aids_by_park.setdefault(pid, []).append(aid)

    # Live read of the tail: window dates AFTER the last rolled day (today, plus
    # any day a missed nightly hasn't finalized). Not "any date missing from the
    # rollup" — pre-history dates inside the window are genuinely empty, and
    # live-reading from them would re-read and double-count the rolled days.
    max_rolled = max(rolled) if rolled else ""
    live_dates = [d for d in window_dates if d > max_rolled]
    live_start = _local_midnight_utc(min(live_dates)) if live_dates else EPOCH
    df_today = _readings_frame(list(aid_to_park), live_start)
    if not df_today.empty:
        df_today["park_id"] = df_today["attraction_id"].map(aid_to_park)

    out_parks: list[dict] = []
    for park in parks:
        raw_sub = df_today[df_today["park_id"] == park.id] if not df_today.empty else df_today
        sub = _within_operating_hours(raw_sub, park.id)
        live = (
            _live_counts(raw_sub, park.id, latest_ts)
            if latest_ts is not None and not raw_sub.empty
            else {"roster": 0, "open_now": 0, "down_now": 0}
        )

        today_pa = _park_day_agg(sub)
        _, today_hourly = _attraction_day_aggs(sub)
        prior_rows = pdaily.get(park.id, [])

        n_readings = today_pa["n_readings"] + sum(r.n_readings for r in prior_rows)
        n_down = today_pa["n_down"] + sum(r.n_down for r in prior_rows)
        n_min = today_pa["n_min"] + sum(r.n_min for r in prior_rows)
        sum_minavg = today_pa["sum_minavg"] + sum(r.sum_minavg for r in prior_rows)
        sum_top5 = today_pa["sum_top5"] + sum(r.sum_top5 for r in prior_rows)
        n_min_top5 = today_pa["n_min_top5"] + sum(r.n_min_top5 for r in prior_rows)

        dr = round(100 * n_down / n_readings, 1) if n_readings else None
        out_parks.append(
            {
                "park_id": park.id,
                "name": park.name,
                **live,
                "momentum": _momentum_from(sub),
                "avg_wait": round(sum_minavg / n_min, 1) if n_min else None,
                "headliner_wait": round(sum_top5 / n_min_top5, 1) if n_min_top5 else None,
                "crowd_index": _crowd_index_combined(
                    aids_by_park.get(park.id, []), prior_hourly, today_hourly, base
                ),
                "downtime_rate": dr,
                "uptime": round(100 - dr, 1) if dr is not None else None,
            }
        )

    return {
        "window": window,
        "destination": {
            "id": destination_id,
            "name": _short_destination(dest.name) if dest else "",
            "full_name": dest.name if dest else "",
        },
        "as_of": latest,
        "caps": PARK_COMPARE_CAPS,
        "parks": out_parks,
    }
