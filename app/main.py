"""FastAPI web app: serves the dashboard shell and JSON for the charts.

One route layer, two readers (ADR-0007), chosen by the READER env var:

    READER=sqlite    (default) app.stats computes over the local SQLite file
    READER=postgres            app.serve SELECTs finished payloads from the
                               Serving Store — no pandas, no arithmetic

Both expose the same nine functions and return byte-identical payloads, so this
module, the Jinja templates, and the frontend JS are shared verbatim between the
collector host and the Azure Function App.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

READER = os.environ.get("READER", "sqlite").strip().lower()

if READER == "postgres":
    from app import serve as reader
else:
    try:
        from app import stats as reader
    except ImportError as exc:  # pragma: no cover - deployment guard
        # The cloud package deliberately ships without app/stats.py and its
        # pandas dependency (ADR-0007). Reaching here means READER was lost or
        # misspelt in the Function App settings; say so plainly rather than
        # surfacing a bare ImportError on a dead site.
        raise RuntimeError(
            f"READER={READER!r} selects the SQLite reader, but app.stats is not "
            "importable. In the Function App this must be READER=postgres."
        ) from exc

APP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Wait Times")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")

WINDOWS = {"today", "week", "month"}
COMPARE_WINDOWS = {"today", "week", "month"}


@app.on_event("startup")
def _startup() -> None:
    # Only the SQLite reader owns a schema; the Serving Store is created by the
    # publisher (app/publish.py --schema) and is read-only from here.
    if READER != "postgres":
        from app.db import init_db

        init_db()


def _asset_version() -> str:
    """Max mtime of the static assets — stamps URLs so browsers refetch on change."""
    static = APP_DIR / "static"
    files = [
        static / "dashboard.js",
        static / "day.js",
        static / "parks.js",
        static / "app.css",
        static / "output.css",
    ]
    mtimes = [int(f.stat().st_mtime) for f in files if f.exists()]
    return str(max(mtimes)) if mtimes else "0"


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html", {"asset_v": _asset_version()}
    )


@app.get("/day")
def day(request: Request):
    return templates.TemplateResponse(
        request, "day.html", {"asset_v": _asset_version()}
    )


@app.get("/parks")
def parks(request: Request):
    return templates.TemplateResponse(
        request, "parks.html", {"asset_v": _asset_version()}
    )


@app.get("/api/attractions")
def api_attractions():
    return reader.get_attractions()


@app.get("/api/stats")
def api_stats(attraction: str = Query(...)):
    return reader.get_stats(attraction)


@app.get("/api/series")
def api_series(
    attraction: str = Query(...),
    window: str = Query("today"),
):
    if window not in WINDOWS:
        window = "today"
    return reader.get_series(attraction, window)


@app.get("/api/recent")
def api_recent(
    attraction: str = Query(...),
    minutes: int = Query(90, ge=1, le=720),
):
    return reader.get_recent(attraction, minutes)


@app.get("/api/reliability")
def api_reliability(attraction: str = Query(...)):
    return reader.get_reliability(attraction)


@app.get("/api/day/live")
def api_day_live(attraction: str = Query(...)):
    return reader.get_day_live(attraction)


@app.get("/api/day/summary")
def api_day_summary(attraction: str = Query(...)):
    return reader.get_day_summary(attraction)


@app.get("/api/destinations")
def api_destinations():
    return reader.get_destinations()


@app.get("/api/watermark")
def api_watermark():
    """How fresh the data actually is.

    Under the Serving Store a 200 no longer implies fresh data — a dead publish
    leaves the last good rows in place. This exposes the real observed-at so the
    UI can degrade to "stale" instead of claiming "live" over old numbers.
    """
    if READER == "postgres":
        return reader.get_watermark()
    ts = reader._latest_observed_at()
    return {"live": {"observed_at": ts, "built_at": ts}}


@app.get("/api/parks/compare")
def api_parks_compare(
    destination: str = Query(...),
    window: str = Query("today"),
):
    if window not in COMPARE_WINDOWS:
        window = "today"
    return reader.get_park_comparison(destination, window)
