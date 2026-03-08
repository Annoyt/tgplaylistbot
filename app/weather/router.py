"""Weather API endpoints."""

from fastapi import APIRouter, Query
from app.weather.service import weather_service

router = APIRouter(prefix="/api/weather", tags=["weather"])


@router.get("/geocode")
async def geocode(city: str = Query(..., min_length=1)):
    result = await weather_service.geocode(city)
    if not result:
        return {"error": "Город не найден"}
    return result


@router.get("/reverse-geocode")
async def reverse_geocode(lat: float, lon: float):
    result = await weather_service.reverse_geocode(lat, lon)
    if not result:
        return {"error": "Локация не найдена"}
    return result


@router.get("/current")
async def current_weather(lat: float, lon: float):
    return await weather_service.get_current(lat, lon)


@router.get("/air-quality")
async def air_quality(lat: float, lon: float):
    return await weather_service.get_air_quality(lat, lon)


@router.get("/forecast")
async def forecast(lat: float, lon: float, days: int = Query(7, ge=1, le=30)):
    return await weather_service.get_forecast(lat, lon, days)


@router.get("/history")
async def history(lat: float, lon: float, dt: int = Query(..., description="Unix timestamp")):
    return await weather_service.get_history(lat, lon, dt)
