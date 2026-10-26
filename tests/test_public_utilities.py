"""Public utility reads preserve location, unit and valid-time evidence."""

import json
import time
from datetime import datetime
from threading import Event

import httpx
import pytest
from pydantic import ValidationError

from agent.domains import utilities
from agent.runtime.control import AgentCancelled, RequestControl, request_scope
from tools import base
from utils import clock


def geocoding():
    return {"results": [{"id": 1808926, "name": "杭州", "admin1": "浙江",
                         "admin2": "杭州市", "feature_code": "PPLA",
                         "country": "中国", "country_code": "CN",
                         "latitude": 30.2936, "longitude": 120.1614}]}


def forecast():
    return {"timezone": "Asia/Shanghai", "utc_offset_seconds": 28800,
            "current_units": {"time": "unixtime", "temperature_2m": "°C",
                              "relative_humidity_2m": "%", "weather_code": "wmo code",
                              "wind_speed_10m": "km/h", "wind_direction_10m": "°"},
            "current": {"time": int(time.time()) - 300, "interval": 900,
                        "temperature_2m": 12, "relative_humidity_2m": 67,
                        "weather_code": 0, "wind_speed_10m": 9, "wind_direction_10m": 45}}


@pytest.fixture
def weather(monkeypatch):
    state = {"geocoding": geocoding(), "forecast": forecast(), "status": 200, "requests": []}

    def handle(request):
        state["requests"].append(request)
        if state.get("timeout"):
            raise httpx.ReadTimeout("test outage", request=request)
        if "raw" in state:
            return httpx.Response(state["status"], content=state["raw"])
        key = "geocoding" if request.url.host == "geocoding-api.open-meteo.com" else "forecast"
        return httpx.Response(state["status"], json=state[key])

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


def read(**args):
    return utilities.get_weather.invoke({"location": "杭州, 浙江", **args})


def test_clock_reuses_the_canonical_local_time(monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 23, 14, 25)

    monkeypatch.setattr(clock, "datetime", Frozen)
    assert utilities.get_current_time is base.get_current_time
    assert utilities.get_current_time.invoke({}) == clock.format_local_now()
    assert "2026年10月23日" in utilities.get_current_time.invoke({})


def test_weather_has_verified_units_place_source_and_model_valid_time(weather):
    result = read(location=" 杭州, 浙江 ")
    data = json.loads(str(result))
    assert result.kind == "ok"
    assert data["temperatureC"] == 12 and data["humidityPercent"] == 67
    assert data["resolvedLocation"]["areaName"] == "杭州"
    assert data["locationQuery"] == "杭州, 浙江"
    assert data["validAtLocal"].endswith("+08:00")
    assert data["dataKind"] == "weather_model_current" and "observedAtLocal" not in data
    assert data["source"] == "Open-Meteo" and "CC BY 4.0" in data["attribution"]
    first, second = weather["requests"]
    assert first.url.host == "geocoding-api.open-meteo.com"
    assert first.url.params["name"] == "杭州, 浙江" and first.url.params["count"] == "5"
    assert second.url.host == "api.open-meteo.com"
    assert second.url.params["latitude"] == "30.2936"
    assert second.url.params["timeformat"] == "unixtime"
    assert "hourly" not in second.url.params and "daily" not in second.url.params
    assert all("Authorization" not in request.headers for request in weather["requests"])
    assert all(request.extensions["timeout"]["read"] == 10 for request in weather["requests"])
    assert len(str(result)) < 1200


def test_location_is_required_and_bad_inputs_never_use_network(weather):
    with pytest.raises(ValidationError):
        utilities.get_weather.invoke({})
    for location in ["", " ", "Beijing/../x?format=1", "x" * 101, 7, "x\n"]:
        try:
            result = read(location=location)
        except ValidationError:
            pass
        else:
            assert result.kind == "invalid_args"
    for selected in [0, -1, True, "123"]:
        with pytest.raises(ValidationError):
            read(location_id=selected)
    assert not weather["requests"]


def test_ambiguous_places_return_candidates_and_do_not_select_a_weather_grid(weather):
    other = {**geocoding()["results"][0], "id": 123, "areaName": "other", "admin1": "另一省"}
    weather["geocoding"]["results"].append(other)
    result = read()
    assert result.kind == "invalid_args"
    assert result.data["status"] == "ambiguous"
    assert [place["id"] for place in result.data["candidates"]] == [1808926, 123]
    assert result.data["candidates"][0]["featureCode"] == "PPLA"
    assert result.data["candidates"][0]["district"] == "杭州市"
    assert "supplied context" in result.data["instruction"]
    assert "temperatureC" not in str(result) and len(weather["requests"]) == 1
    chosen = read(location_id=123)
    assert chosen.kind == "ok"
    assert json.loads(str(chosen))["resolvedLocation"]["id"] == 123
    assert len(weather["requests"]) == 3


def test_selected_location_must_appear_in_the_same_city_query(weather):
    assert read(location_id=999).kind == "invalid_args"
    assert len(weather["requests"]) == 1
    weather["geocoding"] = {"results": []}
    assert read().kind == "not_found"
    assert len(weather["requests"]) == 2


@pytest.mark.parametrize("status", [302, 401, 404, 429, 500])
def test_weather_http_failure_is_unavailable_without_retry(weather, status):
    weather["status"] = status
    assert read().kind == "unavailable"
    assert len(weather["requests"]) == 1


@pytest.mark.parametrize("corruption", ["empty", "missing", "wrongtype", "range", "nan",
                                       "units", "stale", "future", "code", "timezone"])
def test_invalid_current_weather_is_never_returned_as_a_fact(weather, corruption):
    data = weather["forecast"]
    if corruption == "empty":
        weather["forecast"] = {}
    elif corruption == "missing":
        del data["current"]["relative_humidity_2m"]
    elif corruption == "wrongtype":
        data["current"] = "not current weather"
    elif corruption == "range":
        data["current"]["relative_humidity_2m"] = 101
    elif corruption == "nan":
        data["current"]["temperature_2m"] = float("nan")
    elif corruption == "units":
        data["current_units"]["temperature_2m"] = "°F"
    elif corruption == "stale":
        data["current"]["time"] = time.time() - 7200
    elif corruption == "future":
        data["current"]["time"] = time.time() + 7200
    elif corruption == "code":
        data["current"]["weather_code"] = 100
    else:
        data["utc_offset_seconds"] = 72000
    assert read().kind == "unavailable"


@pytest.mark.parametrize("corruption", ["list", "bool", "coordinates", "duplicate", "oversized",
                                       "html", "huge", "timeout"])
def test_invalid_or_unbounded_geocoding_cannot_become_weather(weather, corruption):
    if corruption == "list":
        weather["geocoding"] = []
    elif corruption == "bool":
        weather["geocoding"]["results"][0]["id"] = True
    elif corruption == "coordinates":
        weather["geocoding"]["results"][0]["latitude"] = 91
    elif corruption == "duplicate":
        weather["geocoding"]["results"] *= 2
    elif corruption == "oversized":
        weather["geocoding"]["results"] *= 6
    elif corruption == "html":
        weather["raw"] = b"<html>service maintenance</html>"
    elif corruption == "huge":
        weather["raw"] = b" " * 131073
    else:
        weather["timeout"] = True
    assert read().kind == "unavailable"
    assert len(weather["requests"]) == 1


@pytest.mark.parametrize("city", ["São Paulo", "東京", "München"])
def test_unicode_city_qualifiers_remain_a_query_on_a_fixed_host(weather, city):
    assert read(location=city).kind == "ok"
    assert weather["requests"][0].url.host == "geocoding-api.open-meteo.com"
    assert weather["requests"][0].url.params["name"] == city


def test_deadline_and_cancellation_apply_to_both_network_reads(weather):
    control = RequestControl(Event(), time.monotonic() + 2)
    with request_scope(control):
        assert read().kind == "ok"
    assert all(0 < request.extensions["timeout"]["read"] <= 2
               for request in weather["requests"])
    before = len(weather["requests"])
    with request_scope(control):
        control.stop_event.set()
        with pytest.raises(AgentCancelled):
            read()
    assert len(weather["requests"]) == before


def test_compatibility_weather_uses_the_same_provider_and_validated_contract(weather):
    result = base.get_weather.invoke({"location":"杭州, 浙江"})
    assert result.kind == "ok" and json.loads(str(result))["source"] == "Open-Meteo"
    assert [request.url.host for request in weather["requests"]] == [
        "geocoding-api.open-meteo.com", "api.open-meteo.com"]


def test_weather_failure_trace_distinguishes_outage_from_invalid_data(weather, monkeypatch):
    events = []
    monkeypatch.setattr(utilities, "record", lambda *args, **fields: events.append((args,fields)))
    weather["status"] = 503
    assert read().kind == "unavailable"
    assert events[-1][1]["phase"] == "geocoding"
    assert events[-1][1]["status_code"] == 503
    weather["status"] = 200
    weather["forecast"]["current"]["time"] = int(time.time())-7200
    assert read().kind == "unavailable"
    assert events[-1][1]["phase"] == "current_weather"
    assert events[-1][1]["error_type"] == "ValueError"
    assert "stale" in events[-1][1]["reason"]


def test_compatibility_weather_preserves_upstream_failure_kind(weather):
    weather["status"] = 503
    result = base.get_weather.invoke({"location":"杭州, 浙江"})
    assert result.kind == "unavailable"
    assert len(weather["requests"]) == 1
