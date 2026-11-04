"""Optional client weather is bounded display data, never authority."""

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

WEATHER_CODES = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


class WeatherSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, revalidate_instances="always")

    city: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    temperature_c: float = Field(ge=-100, le=70, allow_inf_nan=False)
    weather_code: StrictInt
    humidity_percent: float = Field(ge=0, le=100, allow_inf_nan=False)
    wind_speed_kmh: float = Field(ge=0, le=500, allow_inf_nan=False)
    valid_at: StrictInt = Field(gt=0, le=9_007_199_254_740_991)
    source: Literal["Open-Meteo"]
    location_source: Literal["GeoJS IP"]

    @model_validator(mode="after")
    def usable_snapshot(self):
        if (
            not self.city.strip()
            or self.weather_code not in WEATHER_CODES
            or not -300 <= time.time() - self.valid_at <= 3600
        ):
            raise ValueError("Unknown or stale client weather")
        return self

    def facts(self) -> dict:
        return {
            **self.model_dump(),
            "conditions": WEATHER_CODES[self.weather_code],
            "location_kind": "ip_estimate",
            "data_kind": "weather_model_current",
        }
