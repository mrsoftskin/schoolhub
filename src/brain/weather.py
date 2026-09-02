"""Local weather via Open-Meteo (no API key, no account).

Fetched server-side on purpose. The page's CSP allows no off-origin requests -
that is what stops a prompt-injected answer from beaconing retrieved context
out through markup - so the browser must not be the one calling a weather
host. The server fetches and the page reads it from this app's own origin.

Fails loud: a failed fetch returns an error string for the UI to show, never
a silently empty forecast.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

ENDPOINT = "https://api.open-meteo.com/v1/forecast"
CACHE_SECONDS = 900  # 15 minutes; the app polls far more often than weather moves
TIMEOUT_SECONDS = 6.0

# WMO 4677 weather codes, condensed to what a person actually wants to read.
WMO_CODES: dict[int, str] = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Rime fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle",
    56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain",
    66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Light showers", 81: "Showers", 82: "Violent showers",
    85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm, hail", 99: "Thunderstorm, hail",
}


@dataclass
class _Cache:
    at: float = 0.0
    payload: dict = field(default_factory=dict)


_cache = _Cache()


def describe(code: int | None) -> str:
    if code is None:
        return "Unknown"
    return WMO_CODES.get(int(code), f"Code {code}")


def get_weather(latitude: float, longitude: float, label: str = "",
                *, force: bool = False) -> dict:
    """Current conditions plus today's range. Cached for CACHE_SECONDS."""
    now = time.time()
    if not force and _cache.payload and (now - _cache.at) < CACHE_SECONDS:
        return {**_cache.payload, "cached": True}

    import httpx

    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": "temperature_2m,apparent_temperature,weather_code,is_day,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "timezone": "auto",
        "forecast_days": 1,
    }
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            resp = client.get(ENDPOINT, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "location": label,
        }

    cur = data.get("current", {})
    daily = data.get("daily", {})

    def first(key):
        seq = daily.get(key) or []
        return seq[0] if seq else None

    payload = {
        "ok": True,
        "location": label,
        "temperature": cur.get("temperature_2m"),
        "feels_like": cur.get("apparent_temperature"),
        "code": cur.get("weather_code"),
        "description": describe(cur.get("weather_code")),
        "is_day": bool(cur.get("is_day", 1)),
        "wind": cur.get("wind_speed_10m"),
        "high": first("temperature_2m_max"),
        "low": first("temperature_2m_min"),
        "precip_chance": first("precipitation_probability_max"),
        "unit": "F",
        "fetched_at": now,
    }
    _cache.at = now
    _cache.payload = payload
    return {**payload, "cached": False}
