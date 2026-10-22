"""Public utilities validate their upstream and never turn an outage into observations."""

import json
from datetime import datetime

import httpx
import pytest
from pydantic import ValidationError

from agent.domains import utilities
from tools import base
from utils import clock


def body():
    return {"current_condition": [{"temp_C": "12", "humidity": "67",
            "windspeedKmph": "9", "winddir16Point": "NE",
            "weatherDesc": [{"value": "Clear"}], "localObsDateTime": "2026-10-23 02:10 PM"}],
            "nearest_area": [{key: [{"value": value}] for key, value in
                              (("areaName", "Beijing"), ("region", "Beijing"),
                               ("country", "China"))}]}


@pytest.fixture
def weather(monkeypatch):
    state = {"body": body(), "status": 200, "requests": []}

    def handle(request):
        state["requests"].append(request)
        if state.get("timeout"):
            raise httpx.ReadTimeout("test outage", request=request)
        if "raw" in state:
            return httpx.Response(state["status"], content=state["raw"])
        return httpx.Response(state["status"], json=state["body"])

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


def test_clock_reuses_the_canonical_local_time(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 23, 14, 25)

    monkeypatch.setattr(clock, "datetime", Frozen)
    assert utilities.get_current_time is base.get_current_time
    assert utilities.get_current_time.invoke({}) == clock.format_local_now()
    assert "2026年10月23日" in utilities.get_current_time.invoke({})


def test_weather_current_projection_and_fixed_host(weather):
    result = utilities.get_weather.invoke({"location": " 杭州 "})
    data = json.loads(str(result))
    assert result.kind == "ok"
    assert data["temperatureC"] == 12 and data["humidityPercent"] == 67
    assert data["resolvedLocation"]["areaName"] == "Beijing"
    assert data["locationQuery"] == "杭州" and "observedAtLocal" in data
    request = weather["requests"][0]
    assert request.url.host == "wttr.in" and request.url.path == "/杭州"
    assert request.url.params["format"] == "j1"
    assert "Authorization" not in request.headers
    assert request.extensions["timeout"]["read"] == 10
    assert "weather" not in data and len(str(result)) < 1000


@pytest.mark.parametrize("location", ["", " ", "Beijing/../x?format=1", "x" * 41, 7])
def test_invalid_city_never_uses_network(weather, location):
    try:
        result = utilities.get_weather.invoke({"location": location})
    except ValidationError:
        pass
    else:
        assert result.kind == "invalid_args"
    assert not weather["requests"]


@pytest.mark.parametrize("status", [302, 401, 404, 429, 500])
def test_weather_http_failure_is_unavailable(weather, status):
    weather["status"] = status
    assert utilities.get_weather.invoke({}).kind == "unavailable"


@pytest.mark.parametrize("corruption", ["empty", "missing", "wrongtype", "range", "nan",
                                          "location", "html", "huge", "timeout"])
def test_weather_invalid_payloads_are_not_facts(weather, corruption):
    if corruption == "empty":
        weather["body"] = {}
    elif corruption == "missing":
        del weather["body"]["current_condition"][0]["humidity"]
    elif corruption == "wrongtype":
        weather["body"]["current_condition"] = ["not an observation"]
    elif corruption == "range":
        weather["body"]["current_condition"][0]["humidity"] = "101"
    elif corruption == "nan":
        weather["body"]["current_condition"][0]["temp_C"] = "NaN"
    elif corruption == "location":
        weather["body"]["nearest_area"] = []
    elif corruption == "html":
        weather["raw"] = b"<html>service maintenance</html>"
    elif corruption == "huge":
        weather["raw"] = b" " * 131073
    else:
        weather["timeout"] = True
    assert utilities.get_weather.invoke({}).kind == "unavailable"


@pytest.mark.parametrize("city", ["São Paulo", "東京", "München"])
def test_unicode_city_names_keep_a_fixed_weather_host(weather, city):
    result = utilities.get_weather.invoke({"location": city})
    assert result.kind == "ok"
    assert weather["requests"][0].url.host == "wttr.in"
    assert weather["requests"][0].url.path == "/" + city
