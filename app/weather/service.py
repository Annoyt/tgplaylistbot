"""OpenWeatherMap async client."""

from __future__ import annotations

import httpx
from config import settings

BASE = "https://api.openweathermap.org"


class WeatherService:
    """Async OpenWeatherMap API wrapper."""

    def __init__(self) -> None:
        self._key = settings.openweathermap_api_key
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=15)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ── Geocoding ─────────────────────────────────────

    async def geocode(self, city: str) -> dict | None:
        """City name → {lat, lon, name, country}."""
        client = await self._get_client()
        r = await client.get(
            f"{BASE}/geo/1.0/direct",
            params={"q": city, "limit": 1, "appid": self._key},
        )
        data = r.json()
        if not data:
            return None
        loc = data[0]
        return {"lat": loc["lat"], "lon": loc["lon"], "name": loc.get("local_names", {}).get("ru", loc["name"]), "country": loc["country"]}

    async def reverse_geocode(self, lat: float, lon: float) -> dict | None:
        """Coords → city name."""
        client = await self._get_client()
        r = await client.get(
            f"{BASE}/geo/1.0/reverse",
            params={"lat": lat, "lon": lon, "limit": 1, "appid": self._key},
        )
        data = r.json()
        if not data:
            return None
        loc = data[0]
        return {"name": loc.get("local_names", {}).get("ru", loc["name"]), "country": loc["country"]}

    # ── Current Weather ───────────────────────────────

    async def get_current(self, lat: float, lon: float) -> dict:
        client = await self._get_client()
        r = await client.get(
            f"{BASE}/data/2.5/weather",
            params={"lat": lat, "lon": lon, "appid": self._key, "units": "metric", "lang": "ru"},
        )
        return r.json()

    # ── Air Quality ───────────────────────────────────

    async def get_air_quality(self, lat: float, lon: float) -> dict:
        client = await self._get_client()
        r = await client.get(
            f"{BASE}/data/2.5/air_pollution",
            params={"lat": lat, "lon": lon, "appid": self._key},
        )
        return r.json()

    # ── Precipitation / Forecast ──────────────────────

    async def get_forecast(self, lat: float, lon: float, days: int = 7) -> dict:
        """Get weather forecast. Free plan gives 5-day/3h; paid gives daily up to 16d."""
        client = await self._get_client()
        cnt = min(days * 8, 40)  # 3h intervals, max 40 = 5 days on free
        r = await client.get(
            f"{BASE}/data/2.5/forecast",
            params={"lat": lat, "lon": lon, "cnt": cnt, "appid": self._key, "units": "metric", "lang": "ru"},
        )
        return r.json()

    # ── Historical Weather ────────────────────────────

    async def get_history(self, lat: float, lon: float, dt: int) -> dict:
        """Get historical weather for a Unix timestamp. Requires One Call 3.0."""
        client = await self._get_client()
        r = await client.get(
            f"{BASE}/data/3.0/onecall/timemachine",
            params={"lat": lat, "lon": lon, "dt": dt, "appid": self._key, "units": "metric", "lang": "ru"},
        )
        return r.json()


weather_service = WeatherService()
