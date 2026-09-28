# Attractions — Orlando theme-park wait-time dashboard

Polls [themeparks.wiki](https://api.themeparks.wiki/docs/v1/) once a minute for
several Orlando **Destinations** (Universal Orlando + Walt Disney World; theme
parks only — water parks excluded), stores each attraction's standby wait in
SQLite, and serves a dashboard charting how waits move over the day, week, month,
and the last 90 minutes. Add a resort by appending its id to `DESTINATION_IDS`
in [app/themeparks.py](app/themeparks.py).

See [CONTEXT.md](CONTEXT.md) for the domain glossary and
[docs/adr/](docs/adr/) for architecture decisions.

## Architecture

- **Collector** (`app/collector.py`) — standalone script run by **cron every
  minute** (not in-process; see [ADR-0001](docs/adr/0001-decoupled-cron-collector.md)).
  One `/live` call writes ~67 `Reading` rows, then one batched
  [Open-Meteo](https://open-meteo.com/) call writes one `WeatherReading` per park
  (temperature °C, precipitation, WMO `weather_code`), sharing the same poll
  minute. Weather is collected after the wait commit and failure-isolated.
- **Web app** (`app/main.py`) — FastAPI + Jinja shell; read-only JSON endpoints
  feed ChartJS. Stats (mean/median/±1σ) are computed in pandas, bucketed in
  park-local time (`America/New_York`).
- **Rollups** (`app/rollup.py`) — a **nightly cron** that precomputes per-day
  aggregate tables so the heavy pages don't rescan all history on every request
  (see [ADR-0004](docs/adr/0004-precomputed-daily-rollups.md)). The web app reads
  the rollups for finalized days and computes only the recent tail live.
- **Storage** — SQLite at `data/attractions.db` in WAL mode (so the cron writer
  and web reader don't block each other). SQLModel models in `app/models.py`.

## Setup

Python deps are managed with `uv`; the frontend uses Tailwind v4 via `npm`.

```bash
uv sync                 # install Python deps into .venv
npm install             # install Tailwind
npm run build           # build app/static/output.css  (npm run watch while developing)
```

## Run

**Collector** — install the per-minute cron job (preserve any existing crontab lines):

```cron
* * * * * cd /ABSOLUTE/PATH/TO/attractions && flock -n /tmp/attractions-poll.lock .venv/bin/python -m app.collector >> /tmp/attractions-poll.log 2>&1
```

Run a single poll manually to test: `.venv/bin/python -m app.collector`

**Rollups** — one-time backfill over existing history, then a nightly cron
(preserve existing crontab lines):

```bash
.venv/bin/python -m app.backfill_rollups   # once, populates the rollup tables
```

```cron
15 6 * * * cd /ABSOLUTE/PATH/TO/attractions && flock -n /tmp/attractions-rollup.lock .venv/bin/python -m app.rollup >> /tmp/attractions-rollup.log 2>&1
```

**Walking distances** — routed over OpenStreetMap footpaths
([ADR-0009](docs/adr/0009-osm-walking-distances.md)). Not a cron job: rebuild
only when the attraction roster changes or the OSM data improves.

```bash
.venv/bin/python -m app.walking --rebuild      # all parks, from cached extracts
.venv/bin/python -m app.walking --refresh-osm  # re-download extracts first
```

Extracts are cached under `data/osm/` (~6 MB). A pair whose walk is implausibly
long for its straight-line distance is **withheld** rather than published — the
build logs it, `stats.walking_withheld()` lists it, and distance queries return
`None`. Those are unmapped connections in OSM upstream, not errors here (30 of
1,856 pairs, 26 of them at Universal Studios Florida).
Data © OpenStreetMap contributors.

**Web app**:

```bash
python main.py          # serves http://127.0.0.1:8000
```

## API

- `GET /api/attractions` — parks + attractions for the selector (incl. per-park average)
- `GET /api/stats?attraction=<id>` — today's current/mean/median + deltas, plus
  `weather` (current conditions for the target's park, incl. derived `raining`)
- `GET /api/series?attraction=<id>&window=today|week|month` — bucketed mean/median/±1σ,
  each bucket also carrying `temp` (mean °C) and `rain` (0–1 fraction raining)

`<id>` is an attraction uuid or `park:<park_uuid>` for a park-wide average.
