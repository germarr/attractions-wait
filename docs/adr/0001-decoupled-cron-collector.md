# Collect wait times with a standalone cron-driven collector, not an in-process scheduler

The web app needs a Reading from every Attraction once per minute regardless of
whether anyone is viewing the dashboard. We run collection as a standalone
`app/collector.py` invoked by OS cron every minute — deliberately a separate
process from the FastAPI/uvicorn web app — rather than an in-process
APScheduler/asyncio task.

## Why

Decoupling means history keeps accumulating even while the web app is being
redeployed, crashing, or restarting; the collector is the source of truth and
the web app is a read-only consumer. The cost is that two processes now share
one SQLite file, so we require **WAL mode** for concurrent reader/writer access,
and **`flock`** on the cron command to skip a run if a slow API call makes the
previous poll overrun its minute.

## Considered and rejected

- **In-process APScheduler in the FastAPI lifespan** — one process, simplest to
  run, but collection stops whenever the web app stops, creating gaps in the
  time series exactly when the app is unhealthy.
- **Bare `asyncio` loop** — no missed-run handling, drifts, and dies silently on
  an unhandled exception.
