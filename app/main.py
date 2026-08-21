"""FastAPI web app: serves the dashboard shell and JSON for the charts.

Read-only over the SQLite file; the cron collector is the sole writer.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app import stats
from app.db import init_db

APP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Wait Times")
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")
templates = Jinja2Templates(directory=APP_DIR / "templates")

WINDOWS = {"today", "week", "month"}
COMPARE_WINDOWS = {"today", "week", "month"}


@app.on_event("startup")
def _startup() -> None:
    # Ensure the schema exists even if the collector hasn't run yet.
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
    return stats.get_attractions()


@app.get("/api/stats")
def api_stats(attraction: str = Query(...)):
    return stats.get_stats(attraction)


@app.get("/api/series")
def api_series(
    attraction: str = Query(...),
    window: str = Query("today"),
):
    if window not in WINDOWS:
        window = "today"
    return stats.get_series(attraction, window)


@app.get("/api/recent")
def api_recent(
    attraction: str = Query(...),
    minutes: int = Query(90, ge=1, le=720),
):
    return stats.get_recent(attraction, minutes)


@app.get("/api/reliability")
def api_reliability(attraction: str = Query(...)):
    return stats.get_reliability(attraction)


@app.get("/api/day/live")
def api_day_live(attraction: str = Query(...)):
    return stats.get_day_live(attraction)


@app.get("/api/day/summary")
def api_day_summary(attraction: str = Query(...)):
    return stats.get_day_summary(attraction)


@app.get("/api/destinations")
def api_destinations():
    return stats.get_destinations()


@app.get("/api/parks/compare")
def api_parks_compare(
    destination: str = Query(...),
    window: str = Query("today"),
):
    if window not in COMPARE_WINDOWS:
        window = "today"
    return stats.get_park_comparison(destination, window)
