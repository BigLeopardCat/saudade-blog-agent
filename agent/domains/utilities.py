"""Bounded public clock and current weather reads."""

import json
from typing import Annotated
from urllib.parse import quote

import httpx
from langchain_core.tools import tool
from pydantic import Field, StrictStr

from agent.reports import sanitize_untrusted
from tools import base
from tools.result import invalid_args
from tools.site import get_current_time as get_current_time

City = Annotated[StrictStr, Field(min_length=1, max_length=40,
                                pattern=r"^[^\x00-\x1f\x7f/?#\\]+$")]


def _weather_body(location):
    url = f"https://wttr.in/{quote(location, safe='')}?format=j1&num_of_days=1"
    with base._client.stream("GET", url, timeout=10) as response:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > 131072:
                raise ValueError("Weather response exceeds limit")
            chunks.append(chunk)
    return json.loads(b"".join(chunks))


def _number(row, key, lower, upper):
    value = row.get(key)
    if not isinstance(value, str) or len(value) > 12:
        raise ValueError("Invalid weather measurement")
    number = float(value)
    if not lower <= number <= upper:
        raise ValueError("Weather measurement out of range")
    return number


def _label(value, limit=80):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing weather label")
    return sanitize_untrusted(value, limit)


def _weather(data, location):
    conditions = data.get("current_condition") if isinstance(data, dict) else None
    if not isinstance(conditions, list) or len(conditions) != 1:
        raise ValueError("Invalid current weather")
    row = conditions[0]
    if not isinstance(row, dict):
        raise ValueError("Invalid current weather record")
    descriptions = row.get("weatherDesc")
    if (not isinstance(descriptions, list) or not descriptions
            or not isinstance(descriptions[0], dict)):
        raise ValueError("Missing weather description")
    areas = data.get("nearest_area")
    if not isinstance(areas, list) or len(areas) != 1 or not isinstance(areas[0], dict):
        raise ValueError("Missing resolved weather location")
    resolved = {}
    for key in ("areaName", "region", "country"):
        labels = areas[0].get(key)
        if not isinstance(labels, list) or not labels or not isinstance(labels[0], dict):
            raise ValueError("Missing resolved weather location label")
        resolved[key] = _label(labels[0].get("value"))
    return {"locationQuery": location, "resolvedLocation": resolved,
            "source": "wttr.in", "temperatureC":
            _number(row, "temp_C", -100, 70), "humidityPercent":
            _number(row, "humidity", 0, 100), "windSpeedKmph":
            _number(row, "windspeedKmph", 0, 500), "windDirection":
            _label(row.get("winddir16Point"), 8), "description":
            _label(descriptions[0].get("value")), "observedAtLocal":
            _label(row.get("localObsDateTime"), 40)}


@tool
def get_weather(location: City = "Beijing") -> object:
    """Read current weather by city name (default Beijing), with source and observation time."""
    location = location.strip()
    if not location:
        return invalid_args("城市名不能为空。")
    try:
        data = _weather(_weather_body(location), location)
    except (httpx.HTTPError, ValueError, TypeError, UnicodeError):
        return base.unavailable("天气服务不可用或未返回完整的当前天气，请稍后重试。")
    return base.ok(json.dumps(data, ensure_ascii=False, separators=(",", ":")), data=data)
