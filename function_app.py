"""Azure Functions entry point (ADR-0007).

Wraps the unmodified FastAPI app in an ASGI passthrough, so the routes, Jinja
templates, and static assets served here are the same files the collector host
serves on :8005. `routePrefix: ""` in host.json keeps paths at the root, so
`/api/stats` stays `/api/stats` rather than becoming `/api/api/stats`.

READER=postgres must be set in the Function App's settings; without it the app
would try to open a SQLite file that does not exist in the cloud.
"""

import azure.functions as func

from app.main import app as fastapi_app

app = func.AsgiFunctionApp(app=fastapi_app, http_auth_level=func.AuthLevel.ANONYMOUS)
