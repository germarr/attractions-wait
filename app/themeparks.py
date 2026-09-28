"""Thin client for the themeparks.wiki API.

Two endpoints per Destination: /live (every poll) and /children (to resolve
park names + coordinates). The app polls several Destinations.

/children is recursive — one call returns a Destination's Parks *and* every
Attraction beneath them, each with a location — so `fetch_geo` gets both from a
single request. /live carries no coordinates, which is why they are seeded here
rather than read off the poll payload.
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


def _coords(entity: dict) -> tuple[float | None, float | None]:
    """Pull (latitude, longitude) out of an entity's optional location block."""
    loc = entity.get("location") or {}
    return loc.get("latitude"), loc.get("longitude")


def fetch_geo(destination_id: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Return (parks, attractions, shows) for a Destination from ONE /children call.

    Parks are [{id, name, latitude, longitude}] with excluded (water) parks
    filtered out. Attractions are [{id, latitude, longitude}] and include only
    those the feed actually geocodes; the caller matches them to rows it already
    has, so attractions under a water park simply never match.

    Shows are [{id, name, park_id, latitude, longitude}] — parades, fireworks and
    stage acts. They carry `name` and `park_id` where Attractions do not, because
    nothing else seeds a Show: an Attraction's name arrives with its standby wait
    on every /live poll, and a Show has no wait to arrive with. The feed geocodes
    all of them.
    """
    resp = requests.get(
        f"{BASE_URL}/entity/{destination_id}/children", timeout=TIMEOUT
    )
    resp.raise_for_status()
    children = resp.json().get("children", [])

    parks: list[dict] = []
    attractions: list[dict] = []
    shows: list[dict] = []
    for c in children:
        entity_type = c.get("entityType")
        latitude, longitude = _coords(c)
        if entity_type == "PARK":
            if c["id"] in EXCLUDED_PARK_IDS:
                continue
            parks.append(
                {
                    "id": c["id"],
                    "name": c["name"],
                    "latitude": latitude,
                    "longitude": longitude,
                }
            )
        elif entity_type == "ATTRACTION" and latitude is not None:
            attractions.append(
                {"id": c["id"], "latitude": latitude, "longitude": longitude}
            )
        elif entity_type == "SHOW" and latitude is not None:
            shows.append(
                {
                    "id": c["id"],
                    "name": c.get("name", ""),
                    "park_id": c.get("parentId"),
                    "latitude": latitude,
                    "longitude": longitude,
                }
            )
    return parks, attractions, shows


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
