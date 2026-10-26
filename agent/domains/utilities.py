"""Bounded public clock and current weather-model reads."""

import json
import math
import time
from datetime import UTC, datetime, timedelta, timezone
from typing import Annotated

import httpx
from langchain_core.tools import tool
from pydantic import Field, StrictInt, StrictStr

from agent.reports import sanitize_untrusted
from agent.runtime.control import check_active, model_options
from tools import base
from tools.result import invalid_args
from tools.site import get_current_time as get_current_time
from utils.trace import record

City = Annotated[StrictStr, Field(min_length=1, max_length=100,
                                pattern=r"^[^\x00-\x1f\x7f/?#\\]+$")]
LocationId = Annotated[StrictInt, Field(gt=0)]

# Open-Meteo's documented WMO weather-code display values, not intent routing.
_WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog", 51: "Light drizzle", 53: "Moderate drizzle",
    55: "Dense drizzle", 56: "Light freezing drizzle", 57: "Dense freezing drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain", 66: "Light freezing rain",
    67: "Heavy freezing rain", 71: "Slight snowfall", 73: "Moderate snowfall",
    75: "Heavy snowfall", 77: "Snow grains", 80: "Slight rain showers",
    81: "Moderate rain showers", 82: "Violent rain showers", 85: "Slight snow showers",
    86: "Heavy snow showers", 95: "Thunderstorm", 96: "Thunderstorm with slight hail",
    97: "Heavy thunderstorm", 99: "Thunderstorm with heavy hail",
}
_CURRENT = "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,wind_direction_10m"
_UNITS = {"time": "unixtime", "temperature_2m": "°C", "relative_humidity_2m": "%",
          "weather_code": "wmo code", "wind_speed_10m": "km/h", "wind_direction_10m": "°"}


def _json(url, params):
    check_active()
    timeout = model_options(10).get("timeout", 10)
    with base._client.stream("GET", url, params=params, timeout=timeout,
                             follow_redirects=False) as response:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            check_active()
            size += len(chunk)
            if size > 131072:
                raise ValueError("Weather response exceeds limit")
            chunks.append(chunk)
    data = json.loads(b"".join(chunks))
    if not isinstance(data, dict) or data.get("error"):
        raise ValueError("Invalid weather response")
    return data


def _number(row, key, lower, upper):
    value = row.get(key)
    if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
        raise ValueError("Invalid weather measurement")
    return value


def _label(value, limit=80):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError("Invalid weather location label")
    return sanitize_untrusted(value, limit)


def _place(row):
    if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0:
        raise ValueError("Invalid geocoding location")
    return {"id": row["id"], "areaName": _label(row.get("name")),
            "region": _label(row["admin1"]) if row.get("admin1") else None,
            "district": _label(row["admin2"]) if row.get("admin2") else None,
            "featureCode": _label(row["feature_code"], 20) if row.get("feature_code") else None,
            "country": _label(row.get("country")),
            "countryCode": _label(row.get("country_code"), 2),
            "latitude": _number(row, "latitude", -90, 90),
            "longitude": _number(row, "longitude", -180, 180)}


def _locations(location, selected):
    data = _json("https://geocoding-api.open-meteo.com/v1/search",
                 {"name": location, "count": 5, "language": "zh", "format": "json"})
    rows = data.get("results", [])
    if not isinstance(rows, list) or len(rows) > 5:
        raise ValueError("Invalid geocoding result")
    places = [_place(row) for row in rows]
    ids = [row["id"] for row in places]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate geocoding locations")
    if selected is not None:
        places = [row for row in places if row["id"] == selected]
        if not places:
            return invalid_args("所选地点不在本次城市查询结果中，请重新核对城市及地点编号。")
    if not places:
        return base.not_found("未找到该地点，请提供城市与国家或省份。")
    if len(places) != 1:
        payload = {"status": "ambiguous", "locationQuery": location, "candidates": places,
                   "instruction": "Use supplied context to select a matching candidate, then retry "
                                  "with its location_id; ask only if the place remains ambiguous."}
        return invalid_args(json.dumps(payload, ensure_ascii=False), data=payload)
    return places[0]


def _current(place, location):
    data = _json("https://api.open-meteo.com/v1/forecast", {
        "latitude": place["latitude"], "longitude": place["longitude"], "current": _CURRENT,
        "timezone": "auto", "timeformat": "unixtime", "forecast_days": 1,
        "temperature_unit": "celsius", "wind_speed_unit": "kmh"})
    row, units = data.get("current"), data.get("current_units")
    if not isinstance(row, dict) or not isinstance(units, dict):
        raise ValueError("Missing current weather")
    if any(units.get(key) != unit for key, unit in _UNITS.items()):
        raise ValueError("Unexpected current weather units")
    stamp = _number(row, "time", 0, 253402300799)
    if not -300 <= time.time() - stamp <= 3600:
        raise ValueError("Weather valid time is stale or in the future")
    code = row.get("weather_code")
    if type(code) is not int or code not in _WEATHER_CODES:
        raise ValueError("Unknown weather code")
    offset = _number(data, "utc_offset_seconds", -50400, 50400)
    local = datetime.fromtimestamp(stamp, UTC).astimezone(timezone(timedelta(seconds=offset)))
    return {"locationQuery": location, "resolvedLocation": place,
            "source": "Open-Meteo", "sourceUrl": "https://open-meteo.com/",
            "attribution": "Weather: Open-Meteo (CC BY 4.0); location: GeoNames",
            "dataKind": "weather_model_current", "validAtLocal": local.isoformat(),
            "timezone": _label(data.get("timezone")), "weatherCode": code,
            "description": _WEATHER_CODES[code],
            "temperatureC": _number(row, "temperature_2m", -100, 70),
            "humidityPercent": _number(row, "relative_humidity_2m", 0, 100),
            "windSpeedKmph": _number(row, "wind_speed_10m", 0, 500),
            "windDirectionDegrees": _number(row, "wind_direction_10m", 0, 360)}


@tool
def get_weather(location: City, location_id: LocationId | None = None) -> object:
    """Read current weather-model estimates for a specified city, with source and valid time.

    Provide a city, optionally qualified by country or province, e.g. Hangzhou, Zhejiang.
    Multiple locations return candidates without weather. Use the supplied context to select a
    matching candidate, then retry the same location query with its location_id.
    Ask only if the place remains ambiguous.
    No default city, forecasts or station observations.
    """
    location = location.strip()
    if not location:
        return invalid_args("城市名不能为空。")
    phase = "geocoding"
    try:
        place = _locations(location, location_id)
        if isinstance(place, base.ToolResult):
            return place
        phase = "current_weather"
        data = _current(place, location)
    except (httpx.HTTPError, ValueError, TypeError, UnicodeError, OverflowError) as error:
        record("utilities", "weather_unavailable", phase=phase, error_type=type(error).__name__,
               status_code=getattr(getattr(error, "response", None), "status_code", None),
               reason=str(error) if isinstance(error, ValueError) else None)
        return base.unavailable("天气服务不可用，或未返回有效时刻的完整当前天气模型数据。"
                                "请稍后重试。")
    return base.ok(json.dumps(data, ensure_ascii=False, separators=(",", ":")), data=data)
