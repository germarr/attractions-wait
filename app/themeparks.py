"""Thin client for the themeparks.wiki API.

Two endpoints per Destination: /live (every poll) and /children (to resolve
park names + coordinates). The app polls several Destinations.
"""

from __future__ import annotations

import requests

BASE_URL = "https://api.themeparks.wiki/v1"
TIMEOUT = 30

# Destinations polled every minute (source of truth for the poll loop).
DESTINATION_IDS = [
    "89db5d43-c434-4097-b71f-f6869f495a22",  # Universal Orlando Resort
    "e957da41-3552-4cf6-b636-5babc5cbc4e5",  # Walt Disney World Resort
]

# Water parks — excluded everywhere (see CONTEXT.md "Water Park").
EXCLUDED_PARK_IDS = {
    "fe78a026-b91b-470c-b906-9d2266b692da",  # Universal Volcano Bay
    "b070cbc5-feaa-4b87-a8c1-f94cca037a18",  # Disney's Typhoon Lagoon
    "ead53ea5-22e5-4095-9a83-8c29300d7c63",  # Disney's Blizzard Beach
}


def fetch_live(destination_id: str) -> dict:
    """Return a Destination's live payload (id, name, and all live entities)."""
    resp = requests.get(
        f"{BASE_URL}/entity/{destination_id}/live", timeout=TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


def fetch_parks(destination_id: str) -> list[dict]:
    """Return [{id, name, latitude, longitude}] for each PARK of a Destination.

    Excluded (water) parks are filtered out.
    """
    resp = requests.get(
        f"{BASE_URL}/entity/{destination_id}/children", timeout=TIMEOUT
    )
    resp.raise_for_status()
    children = resp.json().get("children", [])
    parks = []
    for c in children:
        if c.get("entityType") != "PARK":
            continue
        if c["id"] in EXCLUDED_PARK_IDS:
            continue
        loc = c.get("location") or {}
        parks.append(
            {
                "id": c["id"],
                "name": c["name"],
                "latitude": loc.get("latitude"),
                "longitude": loc.get("longitude"),
            }
        )
    return parks


def fetch_schedule(park_id: str) -> list[dict]:
    """Return a Park's daily schedule [{date, type, opening_time, closing_time}]."""
    resp = requests.get(
        f"{BASE_URL}/entity/{park_id}/schedule", timeout=TIMEOUT
    )
    resp.raise_for_status()
    out = []
    for e in resp.json().get("schedule", []):
        out.append(
            {
                "date": e.get("date"),
                "type": e.get("type"),
                "opening_time": e.get("openingTime"),
                "closing_time": e.get("closingTime"),
            }
        )
    return out
