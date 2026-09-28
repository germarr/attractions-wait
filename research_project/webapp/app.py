"""Local web app for the Magic Kingdom route optimizer and the wait forecaster.

    .venv/bin/python -m research_project.webapp.app        # 127.0.0.1:3467

Serves two research projects and their write-ups: the route optimizer in
`research_project/`, and the wait-time forecaster in `forecast/`. They share one
templates directory, one static mount and one nav rather than running two servers
on two ports.

Runs as the `attractions-research` systemd user service, which pins the port via
`RESEARCH_WEBAPP_PORT` rather than relying on the default below — the literal in
this file has been rewritten from under us several times (8017, 8016, 8020 have
all appeared), so the service does not depend on it. 8005 is the live dashboard
(`attractions.service`), and 8010 and 8090 belong to other projects on this host.

Unlike the deployed dashboard, this solves **live**. That is the whole point — a
static table of precomputed routes could be a CSV, whereas solving on request lets
the assumptions that the results depend on (walking speed, ride durations, crowd
level) be moved and their effect watched. ADR-0007's "no arithmetic in the serving
layer" does not apply here: this never runs in the cloud and reads SQLite directly.

The wait table is built once at startup (~1 s); everything after that is
arithmetic, and identical requests are cached.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from research_project import bounds, config, db, model, plan, solver, travel, waits
from research_project import shows as shows_mod
from research_project.webapp import charts, fcharts

# One place, and overridable. Every doc and the systemd unit reference this rather
# than a literal buried in main().
DEFAULT_PORT = 3467
PORT = int(os.environ.get("RESEARCH_WEBAPP_PORT", DEFAULT_PORT))

HERE = Path(__file__).resolve().parent
ARTICLE = HERE.parents[0] / "ARTICLE.md"
FORECAST_ARTICLE = HERE.parents[1] / "forecast" / "ARTICLE.md"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class ArticleSpec:
    """One write-up: its prose, its figures, and how the shell should label it."""

    path: Path
    charts: object
    title: str
    subtitle: str
    nav: str
    endpoint: str
    script: str | None


ARTICLES: dict[str, ArticleSpec] = {
    "route": ArticleSpec(
        path=ARTICLE,
        charts=charts,
        title="How many rides can you fit into one day at Magic Kingdom?",
        subtitle="Route optimization over wait-time history and OpenStreetMap footpaths.",
        nav="route-article",
        endpoint="/api/charts",
        script=None,
    ),
    "forecast": ArticleSpec(
        path=FORECAST_ARTICLE,
        charts=fcharts,
        title="Can you predict how long you'll queue three weeks from now?",
        subtitle="Forecasting wait times for seven parks, and measuring honestly whether it works.",
        nav="forecast-article",
        endpoint="/api/forecast/charts",
        script="/static/fcharts.js",
    ),
}

app = FastAPI(title="Magic Kingdom route optimizer")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


class _Context:
    """Everything expensive, built once."""

    def __init__(self) -> None:
        conn = db.connect()
        self.roster = db.roster(conn, mode="open_today", ride_scale=1.0)
        self.names = {a.id: a.name for a in db.roster(conn, mode="history")}
        self.table = waits.build_wait_table(conn, names=self.names)
        self.mean_wait = {
            a.id: float(self.table.minutes[self.table.index_of(a.id), :, 9:21].mean())
            for a in self.roster
        }
        conn.close()

    @lru_cache(maxsize=16)
    def matrix(self, walk_speed: float) -> travel.TravelMatrix:
        """Travel times depend on the speed assumption, so cache per value."""
        conn = db.connect()
        try:
            return travel.build_travel_matrix(
                conn,
                config.PARK_ID,
                [a.id for a in self.roster],
                assumptions=config.Assumptions(walk_speed_mps=walk_speed),
            )
        finally:
            conn.close()


CONTEXT: _Context | None = None


def context() -> _Context:
    global CONTEXT
    if CONTEXT is None:
        CONTEXT = _Context()
    return CONTEXT


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "route.html", {})


@lru_cache(maxsize=4)
def _article_html(slug: str) -> str:
    """Render one article's markdown, swapping chart markers for mount points.

    The markdown file stays the single source of the prose — the web version adds
    figures rather than forking the text, so a sentence and the chart beside it
    cannot drift apart.
    """
    import markdown

    spec = ARTICLES[slug]
    html = markdown.markdown(
        spec.path.read_text(),
        extensions=["tables", "fenced_code", "toc", "attr_list"],
    )
    # python-markdown passes HTML comments through untouched, so the markers
    # survive conversion and can be swapped for mount points here.
    for name in spec.charts.everything():
        html = html.replace(
            f"<!-- chart:{name} -->", f'<figure class="chart" data-chart="{name}"></figure>'
        )
    return html


def _render_article(request: Request, slug: str):
    spec = ARTICLES[slug]
    return templates.TemplateResponse(
        request,
        "article.html",
        {
            "body": _article_html(slug),
            "title": spec.title,
            "subtitle": spec.subtitle,
            "nav": spec.nav,
            "charts_endpoint": spec.endpoint,
            "charts_script": spec.script,
        },
    )


@app.get("/article", response_class=HTMLResponse)
def article(request: Request):
    return _render_article(request, "route")


@app.get("/forecast", response_class=HTMLResponse)
def forecast_page(request: Request):
    return templates.TemplateResponse(request, "forecast.html", {})


@app.get("/forecast/article", response_class=HTMLResponse)
def forecast_article(request: Request):
    return _render_article(request, "forecast")


@app.get("/api/forecast/charts")
def api_forecast_charts():
    """Every figure in the forecasting article, from live tables and backtest CSVs."""
    return fcharts.everything()


@lru_cache(maxsize=1)
def _forecaster():
    """The trained artifact, loaded once. Numpy only — no scikit-learn at serve time."""
    from forecast import artifacts

    return artifacts.load()


@lru_cache(maxsize=1)
def forecast_dates(not_before: str | None = None) -> dict:
    from forecast import predict as fpredict

    return fpredict.available_dates(forecaster=_forecaster(), not_before=not_before)


@lru_cache(maxsize=128)
def forecast_for_date(date: str, park: str = "") -> dict:
    """Predicted hourly waits for one date.

    A plain function with real defaults, not the route handler: FastAPI's
    `Query(...)` defaults only resolve through the request machinery, so a handler
    is not callable from a test, the CLI, or a notebook. The handler below is a
    one-line forwarder.
    """
    from forecast import config as fconfig
    from forecast import predict as fpredict

    parks = None
    if park:
        parks = tuple(p for p, name in fconfig.PARKS.items() if p == park or name == park)
        if not parks:
            raise HTTPException(404, f"unknown park {park!r}")
    return fpredict.forecast_day(date, parks=parks, forecaster=_forecaster())


@app.get("/api/forecast/dates")
def api_forecast_dates(not_before: str | None = Query(None)):
    return forecast_dates(not_before)


@app.get("/api/forecast")
def api_forecast(date: str = Query(...), park: str = Query("")):
    from forecast import predict as fpredict

    try:
        return forecast_for_date(date, park)
    except fpredict.HorizonError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/charts")
def api_charts():
    """Every chart series, computed from the same tables the prose quotes."""
    return charts.everything()


@app.get("/api/dates")
def api_dates(start: str = "2026-09-25", end: str = "2026-10-25"):
    """Every date that can be planned, with the window that shapes the answer."""
    conn = db.connect()
    try:
        days = db.operating_days(conn, config.PARK_ID, start=start, end=end)
    finally:
        conn.close()
    return [
        {
            "date": day.date,
            "weekday": WEEKDAYS[day.weekday],
            "open": model._clock(day.open_minute),
            "close": model._clock(day.close_minute),
            "window_hours": round(day.length_minutes / 60, 1),
            "party_night": day.party_night,
        }
        for day in days
    ]


@lru_cache(maxsize=256)
def plan_route(
    date: str,
    walk_speed: float = 1.1,
    ride_scale: float = 1.0,
    crowd: float = 1.0,
    restarts: int = 8,
    must_do: tuple[str, ...] = (),
    lunch_start: float | None = None,
    lunch_minutes: float | None = None,
    shows: tuple[str, ...] = (),
) -> dict:
    """Solve one date and return the itinerary plus the geometry to draw it.

    A plain function with real defaults, not the route handler: FastAPI's
    `Query(...)` defaults only resolve through the request machinery, so a
    handler is not callable from a test, the CLI, or a notebook. The handler
    below is a one-line forwarder.

    Every argument is hashable, because this is `lru_cache`d — a list of must-do
    ids would raise, and worse, a mutable default would have the cache serve one
    visitor's picks to the next.
    """
    ctx = context()
    assumptions = config.Assumptions(
        walk_speed_mps=walk_speed,
        ride_scale=ride_scale,
        crowd_multiplier=crowd,
        lunch_minutes=(
            lunch_minutes
            if lunch_minutes is not None
            else config.Assumptions().lunch_minutes
        ),
    )
    conn = db.connect()
    try:
        return plan.build_plan(
            conn,
            date=date,
            roster=ctx.roster,
            table=ctx.table,
            matrix=ctx.matrix(walk_speed),
            mean_wait=ctx.mean_wait,
            must_do=list(must_do),
            lunch_start=lunch_start,
            lunch_minutes=lunch_minutes,
            show_keys=list(shows),
            assumptions=assumptions,
            restarts=restarts,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        conn.close()


@lru_cache(maxsize=1)
def attraction_options() -> list[dict]:
    """The must-do menu the onboarding step renders."""
    ctx = context()
    return plan.roster_options(ctx.roster, ctx.mean_wait)


@lru_cache(maxsize=64)
def show_options(date: str) -> dict:
    """Which shows can be planned on this date, with their timing provenance."""
    conn = db.connect()
    try:
        days = db.operating_days(conn, config.PARK_ID, start=date, end=date)
        if not days:
            raise HTTPException(404, f"no OPERATING schedule for {date}")
        day = days[0]
        return {
            "date": date,
            "close": plan.clock(day.close_minute),
            "party_night": day.party_night,
            "shows": list(
                shows_mod.available(
                    conn,
                    date=date,
                    close_minute=day.close_minute,
                    party_night=day.party_night,
                ).values()
            ),
        }
    finally:
        conn.close()


@app.get("/api/attractions")
def api_attractions():
    return {"max_must_do": plan.MAX_MUST_DO, "attractions": attraction_options()}


@app.get("/api/shows")
def api_shows(date: str = Query(...)):
    return show_options(date)


@app.get("/api/route")
def api_route(
    date: str = Query(...),
    walk_speed: float = Query(1.1, ge=0.5, le=2.0),
    ride_scale: float = Query(1.0, ge=0.25, le=3.0),
    crowd: float = Query(1.0, ge=0.5, le=2.0),
    restarts: int = Query(8, ge=1, le=40),
    must_do: str = Query("", description="comma-separated ids, highest priority first"),
    lunch_start: float | None = Query(None, ge=0, le=1439),
    lunch_minutes: float | None = Query(None, ge=0, le=240),
    shows: str = Query("", description="comma-separated show keys"),
):
    picks = tuple(x for x in (p.strip() for p in must_do.split(",")) if x)
    show_keys = tuple(x for x in (p.strip() for p in shows.split(",")) if x)
    if len(picks) > plan.MAX_MUST_DO:
        raise HTTPException(400, f"at most {plan.MAX_MUST_DO} must-do picks")
    return plan_route(
        date,
        round(walk_speed, 3),
        round(ride_scale, 3),
        round(crowd, 3),
        restarts,
        picks,
        round(lunch_start, 1) if lunch_start is not None else None,
        round(lunch_minutes, 1) if lunch_minutes is not None else None,
        show_keys,
    )


def main() -> None:
    import uvicorn

    context()  # build the wait table before accepting traffic
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
