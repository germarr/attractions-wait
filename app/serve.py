"""Cloud reader over the Serving Store (ADR-0007).

The nine functions here mirror app/stats.py's public surface exactly — same
names, same arguments, same return shapes — but compute nothing. Every one is a
flat SELECT of a payload that app/publish.py already finished on the collector
host.

MUST NOT import pandas, numpy, or sqlmodel. `import app.main` with the SQLite
reader costs 0.82 s and 109 MB of package; this module is what keeps the Azure
Function App's cold start tolerable.
"""

from __future__ import annotations

import os
from typing import Any

from psycopg_pool import ConnectionPool

from app import pgstore

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    """Lazy pool. Small by design: the Function App scales out rather than up,
    and every query is a single-row primary-key lookup."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            pgstore.dsn(),
            min_size=int(os.environ.get("PG_POOL_MIN", "1")),
            max_size=int(os.environ.get("PG_POOL_MAX", "4")),
            open=True,
        )
    return _pool


def _one(sql: str, params: tuple = ()) -> Any:
    with pool().connection() as conn:
        row = conn.execute(sql, params).fetchone()
    return row


def _scalar(sql: str, params: tuple = (), default: Any = None) -> Any:
    row = _one(sql, params)
    return default if row is None or row[0] is None else row[0]


# ── Watermark ─────────────────────────────────────────────────────────────

def get_watermark() -> dict:
    """Freshness of each pass. Surfaced so the UI can say honestly how current
    it is rather than reporting its own fetch time."""
    out: dict[str, Any] = {}
    with pool().connection() as conn:
        for pass_, observed_at, built_at in conn.execute(
            "select pass, observed_at, built_at from pub.watermark"
        ):
            out[pass_] = {
                "observed_at": observed_at.isoformat() if observed_at else None,
                "built_at": built_at.isoformat() if built_at else None,
            }
    return out


# ── Dimensions ────────────────────────────────────────────────────────────

def get_attractions() -> list[dict]:
    return _scalar("select payload from pub.dimensions where key='attractions'", (), [])


def get_destinations() -> list[dict]:
    return _scalar("select payload from pub.dimensions where key='destinations'", (), [])


# ── Per-target ────────────────────────────────────────────────────────────

def get_stats(target: str) -> dict:
    return _scalar("select stats from pub.target_live where target_id=%s", (target,), {})


def get_day_live(target: str) -> dict:
    return _scalar("select day_live from pub.target_live where target_id=%s", (target,), {})


def get_recent(target: str, minutes: int = 90) -> list[dict]:
    """Published at the UI's 90-minute window. A shorter request is served by
    slicing the tail; a longer one cannot be answered beyond what was published.
    """
    trace = _scalar("select recent from pub.target_live where target_id=%s", (target,), [])
    if not trace or minutes >= 90:
        return trace
    keep = max(1, round(len(trace) * minutes / 90))
    return trace[-keep:]


def get_series(target: str, window: str) -> list[dict]:
    col = {"today": "series_today", "week": "series_week", "month": "series_month"}.get(
        window, "series_today"
    )
    return _scalar(f"select {col} from pub.target_live where target_id=%s", (target,), [])


def get_day_summary(target: str) -> dict:
    """Reassembles the endpoint from its live half and its nightly half.

    `compare` is rebuilt rather than stored whole: its three reference traces are
    fixed historical days (published nightly as {"HH:MM": wait} maps) while its
    `today` series grows all day. Projecting them onto a shared label axis is
    list indexing, not statistics — Postgres still computes nothing.
    """
    row = _one(
        """select tl.day_summary, tl.day_live, td.compare_ref, td.weekday, td.correlation
             from pub.target_live tl
             left join pub.target_daily td on td.target_id = tl.target_id
            where tl.target_id = %s""",
        (target,),
    )
    if row is None:
        return {}
    day_summary, day_live, compare_ref, weekday, correlation = row
    out = dict(day_summary or {})
    out["correlation"] = correlation or {"labels": ["Wait", "Temp", "Rain"], "matrix": None, "n": 0}
    out["weekday"] = weekday or {}
    out["compare"] = _compare(day_live, compare_ref)
    return out


def _compare(day_live: dict | None, compare_ref: dict | None) -> dict:
    """Rebuild get_day_summary's `compare` block: today's live trace plus the
    fixed reference days, all projected onto the union of their time labels."""
    today = {
        p["t"]: p.get("wait")
        for p in ((day_live or {}).get("trace") or [])
        if p.get("t")
    }
    ref = compare_ref or {}
    series = {
        "today": today,
        "yesterday": ref.get("yesterday") or {},
        "wow": ref.get("wow") or {},
        "mom": ref.get("mom") or {},
    }
    labels = sorted(set().union(*[set(d) for d in series.values()])) if series else []
    return {"labels": labels, **{k: [series[k].get(l) for l in labels] for k in series}}


# ── Park / destination scoped ─────────────────────────────────────────────

def get_reliability(target: str) -> dict:
    """Park-scoped: resolve the target's Park, then return that Park's payload."""
    park_id = _scalar("select park_id from pub.target_live where target_id=%s", (target,))
    if park_id is None:
        return {}
    return _scalar("select payload from pub.reliability where park_id=%s", (park_id,), {})


def get_park_comparison(destination_id: str, window: str) -> dict:
    return _scalar(
        "select payload from pub.park_window where destination_id=%s and window_key=%s",
        (destination_id, window),
        {},
    )
