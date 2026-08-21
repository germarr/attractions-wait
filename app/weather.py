"""Open-Meteo client. Fetches current weather for several coordinates at once.

Keyless and free. One batched call returns current conditions for every park.
"""

from __future__ import annotations

import requests

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_FIELDS = "temperature_2m,precipitation,weather_code,wind_speed_10m,is_day"
TIMEOUT = 30


def fetch_weather(coords: list[tuple[str, float, float]]) -> dict[str, dict]:
    """Return {park_id: {weather fields}} for the given (park_id, lat, lon) list.

    Open-Meteo accepts comma-separated coordinates and returns results in the
    same order, so we zip the response back onto the park ids.
    """
    if not coords:
        return {}

    lats = ",".join(str(c[1]) for c in coords)
    lons = ",".join(str(c[2]) for c in coords)
    resp = requests.get(
        OPEN_METEO_URL,
        params={
            "latitude": lats,
            "longitude": lons,
            "current": CURRENT_FIELDS,
            "timezone": "America/New_York",
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    # A single coordinate returns one object; multiple return a list.
    if isinstance(data, dict):
        data = [data]

    result: dict[str, dict] = {}
    for (park_id, _lat, _lon), entry in zip(coords, data):
        cur = entry.get("current", {})
        result[park_id] = {
            "temperature_c": cur.get("temperature_2m"),
            "precipitation_mm": cur.get("precipitation"),
            "weather_code": cur.get("weather_code"),
            "wind_speed_kmh": cur.get("wind_speed_10m"),
            "is_day": cur.get("is_day"),
        }
    return result


HOURLY_FIELDS = "temperature_2m,precipitation,weather_code,wind_speed_10m,is_day"


def fetch_history(
    coords: list[tuple[str, float, float]], past_days: int = 2
) -> dict[str, list[dict]]:
    """Return {park_id: [hourly readings]} for recent past hours (UTC times).

    Used to backfill WeatherReadings for hours that were polled for waits before
    weather collection started. Each reading dict carries the same fields as a
    live one, plus `time` (UTC, "YYYY-MM-DDTHH:MM").
    """
    if not coords:
        return {}

    lats = ",".join(str(c[1]) for c in coords)
    lons = ",".join(str(c[2]) for c in coords)
    resp = requests.get(
        OPEN_METEO_URL,
        params={
            "latitude": lats,
            "longitude": lons,
            "hourly": HOURLY_FIELDS,
            "past_days": past_days,
            "forecast_days": 1,
            "timezone": "UTC",
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        data = [data]

    result: dict[str, list[dict]] = {}
    for (park_id, _lat, _lon), entry in zip(coords, data):
        h = entry.get("hourly", {})
        times = h.get("time", [])
        rows = []
        for i, t in enumerate(times):
            rows.append(
                {
                    "time": t,  # UTC, e.g. "2026-06-24T15:00"
                    "temperature_c": h["temperature_2m"][i],
                    "precipitation_mm": h["precipitation"][i],
                    "weather_code": h["weather_code"][i],
                    "wind_speed_kmh": h["wind_speed_10m"][i],
                    "is_day": h["is_day"][i],
                }
            )
        result[park_id] = rows
    return result
