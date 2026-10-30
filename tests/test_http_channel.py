"""Read channels classify failures and retry only explicit transient faults.

The public and identity reads share ``tools/http_channel.py``. These tests pin
the closed classification, the one-retry bound, write-once semantics and the
absence of exception text in model-visible results. Everything runs against an
in-process ``httpx.MockTransport``; no network, database or model is involved.
"""

import asyncio
import json
import threading

import httpx
import pytest

from agent.principal import SOURCE_ASSERTION, Principal
from tools import base
from tools import http_channel as channel


def config(stop=None):
    configurable = {"user_id": 7, "principal": Principal(7, "admin", SOURCE_ASSERTION)}
    if stop is not None:
        configurable["stop_event"] = stop
    return {"configurable": configurable}


@pytest.fixture(autouse=True)
def signing(monkeypatch):
    monkeypatch.setattr(base, "_jwt_secret", lambda: "offline-test-signing-key")


def install(monkeypatch, handler):
    """Install a mock transport and return the recorded requests."""
    calls = []

    def handle(request):
        calls.append(request)
        return handler(request, len(calls))

    monkeypatch.setattr(base, "_client",
                        httpx.Client(transport=httpx.MockTransport(handle)))
    return calls


# ── reads: transient recovery and exhaustion ────────────────────────────────

def test_transient_read_recovers_on_second_attempt(monkeypatch):
    calls = install(monkeypatch, lambda request, n: (
        httpx.Response(503) if n == 1
        else httpx.Response(200, json={"code": 200, "data": {"ok": True}})))
    assert base._admin_get("/api/protected/stats", config()) == {"ok": True}
    assert len(calls) == 2


def test_transient_status_exhausts_two_attempts(monkeypatch):
    calls = install(monkeypatch, lambda request, n: httpx.Response(503))
    result = base._admin_get("/api/protected/stats", config())
    assert result.kind == "unavailable"
    assert result.meta["error"] == channel.ERROR_UPSTREAM_UNAVAILABLE
    assert result.meta["attempts"] == 2
    assert len(calls) == 2


def test_read_timeout_is_retried_but_never_reported_as_success(monkeypatch):
    def handler(request, n):
        raise httpx.ReadTimeout("upstream deadline", request=request)

    calls = install(monkeypatch, handler)
    result = base._admin_get("/api/protected/stats", config())
    assert result.kind == "unavailable"
    assert result.meta["error"] == channel.ERROR_UPSTREAM_UNAVAILABLE
    assert result.meta["attempts"] == 2
    assert len(calls) == 2


def test_public_read_retries_once(monkeypatch):
    calls = install(monkeypatch, lambda request, n: (
        httpx.Response(502) if n == 1
        else httpx.Response(200, json={"code": 200, "data": [{"noteId": 1}]})))
    assert base._get("/notes") == [{"noteId": 1}]
    assert len(calls) == 2


# ── reads: never-retried classifications ────────────────────────────────────

def test_authorization_denial_is_not_retried(monkeypatch):
    calls = install(monkeypatch, lambda request, n: httpx.Response(403))
    result = base._admin_get("/api/protected/stats", config())
    assert result.kind == "unavailable"
    assert result.meta["error"] == channel.ERROR_FORBIDDEN
    assert result.meta["attempts"] == 1
    assert "无权" in str(result)
    assert len(calls) == 1


def test_missing_target_is_not_retried(monkeypatch):
    calls = install(monkeypatch, lambda request, n: httpx.Response(404))
    result = base._admin_get("/api/protected/notes/19", config())
    assert result.kind == "not_found"
    assert result.meta["error"] == channel.ERROR_NOT_FOUND
    assert len(calls) == 1


def test_missing_public_target_keeps_caller_wording(monkeypatch):
    calls = install(monkeypatch, lambda request, n: httpx.Response(404))
    result = base._get("/notes/19", not_found_text="站内没有这篇文章")
    assert result.kind == "not_found"
    assert str(result) == "站内没有这篇文章"
    assert len(calls) == 1


def test_rejected_arguments_are_not_retried(monkeypatch):
    calls = install(monkeypatch, lambda request, n: httpx.Response(422))
    result = base._admin_get("/api/protected/stats", config())
    assert result.kind == "invalid_args"
    assert result.meta["error"] == channel.ERROR_INVALID_ARGS
    assert len(calls) == 1


def test_undecodable_read_keeps_failure_kind(monkeypatch):
    calls = install(monkeypatch, lambda request, n: httpx.Response(
        200, content=b"not-json", headers={"content-type": "text/plain"}))
    result = base._admin_get("/api/protected/stats", config())
    assert result.kind == "unavailable"
    assert result.meta["error"] == channel.ERROR_INVALID_RESPONSE
    assert "不代表没有内容" in str(result)
    assert len(calls) == 1


def test_business_rejection_is_typed_and_carries_safe_message(monkeypatch):
    install(monkeypatch, lambda request, n: httpx.Response(
        200, json={"code": 409, "message": "corpus_revision_changed"}))
    result = base._admin_get("/api/protected/stats", config())
    assert result.meta["error"] == channel.ERROR_UPSTREAM_REJECTED
    assert "接口报错: corpus_revision_changed" == str(result)


def test_envelope_without_data_is_an_invalid_response(monkeypatch):
    install(monkeypatch, lambda request, n: httpx.Response(200, json={"code": 200}))
    result = base._get("/notes")
    assert result.meta["error"] == channel.ERROR_INVALID_RESPONSE


# ── cancellation and deadlines ──────────────────────────────────────────────

def test_stop_signal_suppresses_the_retry(monkeypatch):
    stop = threading.Event()

    def handler(request, n):
        stop.set()
        return httpx.Response(503)

    calls = install(monkeypatch, handler)
    result = base._admin_get("/api/protected/stats", config(stop))
    assert result.kind == "unavailable"
    assert result.meta["attempts"] == 1
    assert len(calls) == 1


def test_cancellation_is_not_swallowed(monkeypatch):
    def handler(request, n):
        raise asyncio.CancelledError()

    calls = install(monkeypatch, handler)
    with pytest.raises(asyncio.CancelledError):
        base._admin_get("/api/protected/stats", config())
    assert len(calls) == 1


def test_unexpected_exceptions_are_not_converted_to_unavailable(monkeypatch):
    def handler(request, n):
        raise RuntimeError("programming error")

    install(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="programming error"):
        base._admin_get("/api/protected/stats", config())


# ── writes: exactly once and explicitly unknown ─────────────────────────────

def test_write_response_lost_is_unknown_and_sent_once(monkeypatch):
    calls = install(monkeypatch, lambda request, n: httpx.Response(
        200, content=b"<html>", headers={"content-type": "text/html"}))
    result = base._own_post("/api/protected/favorites", {"noteId": 1}, config())
    assert result.kind == "unavailable"
    assert result.meta["error"] == channel.ERROR_WRITE_UNKNOWN
    assert "结果未知" in str(result)
    assert "本次改动未确认生效" in str(result)
    assert len(calls) == 1


def test_write_transport_failure_is_unknown_and_sent_once(monkeypatch):
    def handler(request, n):
        raise httpx.ConnectError("token=SECRET-CREDENTIAL", request=request)

    calls = install(monkeypatch, handler)
    result = base._own_post("/api/protected/favorites", {"noteId": 1}, config())
    assert result.meta["error"] == channel.ERROR_WRITE_UNKNOWN
    assert "SECRET-CREDENTIAL" not in str(result)
    assert "SECRET-CREDENTIAL" not in json.dumps(result.meta)
    assert "ConnectError" not in str(result)
    assert len(calls) == 1


def test_write_transient_status_is_never_replayed(monkeypatch):
    calls = install(monkeypatch, lambda request, n: httpx.Response(503))
    result = base._own_post("/api/protected/favorites", {"noteId": 1}, config())
    assert result.meta["error"] == channel.ERROR_WRITE_UNKNOWN
    assert len(calls) == 1


def test_write_conflict_is_unknown_not_success(monkeypatch):
    calls = install(monkeypatch, lambda request, n: httpx.Response(409))
    result = base._own_post("/api/protected/favorites", {"noteId": 1}, config())
    assert result.meta["error"] == channel.ERROR_WRITE_UNKNOWN
    assert len(calls) == 1


def test_write_rejected_arguments_say_nothing_changed(monkeypatch):
    calls = install(monkeypatch, lambda request, n: httpx.Response(400))
    result = base._own_post("/api/protected/favorites", {"noteId": 1}, config())
    assert result.kind == "invalid_args"
    assert result.meta["error"] == channel.ERROR_INVALID_ARGS
    assert "本次未改动任何内容" in str(result)
    assert len(calls) == 1


def test_write_authorization_denial_is_not_retried(monkeypatch):
    calls = install(monkeypatch, lambda request, n: httpx.Response(403))
    result = base._own_post("/api/protected/favorites", {"noteId": 1}, config())
    assert result.meta["error"] == channel.ERROR_FORBIDDEN
    assert len(calls) == 1


def test_write_success_returns_data_once(monkeypatch):
    calls = install(monkeypatch, lambda request, n: httpx.Response(
        200, json={"code": 200, "data": {"changed": True}}))
    assert base._own_post("/api/protected/favorites", {"noteId": 1},
                          config()) == {"changed": True}
    assert len(calls) == 1


# ── explicit read verbs and message hygiene ─────────────────────────────────

def test_body_read_is_chosen_by_caller_not_by_path(monkeypatch):
    methods = []

    def handler(request, n):
        methods.append(request.method)
        return httpx.Response(200, json={"code": 200, "data": {"items": []}})

    install(monkeypatch, handler)
    base._admin_get("/api/protected/notes/search", config())
    base._admin_read_post("/api/protected/notes/search", {"keyword": "x"}, config())
    base._get("/notes/search")
    base._get("/notes/search", payload={"keyword": "x"})
    assert methods == ["GET", "POST", "GET", "POST"]


def test_backend_message_is_stripped_and_capped(monkeypatch):
    install(monkeypatch, lambda request, n: httpx.Response(
        200, json={"code": 400, "message": "bad\x00\x1f " + "x" * 500}))
    result = base._admin_get("/api/protected/stats", config())
    text = str(result)
    assert "\x00" not in text and "\x1f" not in text
    assert channel.MESSAGE_MAX < len(text) <= channel.MESSAGE_MAX + 40
