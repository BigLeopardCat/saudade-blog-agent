"""Production JSON traces distinguish tool calls, HTTP attempts and consumed bytes."""

import asyncio
import gzip
import hashlib
import json
from contextvars import ContextVar
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from agent.runtime import tool_evidence
from agent.runtime.session import ToolSession
from eval import request_metrics
from tools import base, http_observability
from tools import http_channel as channel
from utils import tool_context, trace


@pytest.fixture
def recorder(monkeypatch, tmp_path):
    monkeypatch.setattr(trace, "_recorder", ContextVar("offline_trace", default=None))
    rec = trace.start_trace("offline-http", 0, "synthetic", dir=str(tmp_path), by_day=False)
    yield rec
    path = (trace.finish_trace("offline-http", "producer_done", 1.0)
            or next(tmp_path.glob("*.json")))
    with open(path) as saved:
        document = json.load(saved)
    assert document["events"] == rec.events
    assert document["observability"] == {"json_http": 1}


def http_events(recorder):
    return [event for event in recorder.events if event["node"] == "tools"
            and event["event"] == "http_attempt"]


def test_native_tool_call_records_each_real_json_attempt_without_changing_model_history(
        monkeypatch, recorder):
    requests, prompts = [], []
    content = {"code": 200, "data": {"content": "PRIVATE_BODY_CANARY" * 50}}

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=content)

    @tool
    def inspect_resources() -> str:
        """Read synthetic resources through the public business channel."""
        base._get("/gallery?page=1")
        base._get("/gallery?page=1")
        base._get("/articles/54")
        return "Read three resources."

    class Model:
        max_tokens = 128

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages, **kwargs):
            prompts.append(messages[:])
            return (AIMessage(content="", tool_calls=[
                {"name": inspect_resources.name, "args": {}, "id": "inspect-call"}])
                if len(prompts) == 1 else AIMessage(content="Read complete."))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(base, "_client", client)
        session = ToolSession(Model(), {}, {inspect_resources.name: inspect_resources}, {})
        list(session.stream([HumanMessage(content="Read the synthetic resources.")]))
    events = http_events(recorder)
    native = [event for event in recorder.events if event["event"] == "tool_requested"]
    assert len(native) == 1 and len(events) == len(requests) == 3
    assert all(event["call_id"] == "inspect-call" for event in events)
    call = next(event for event in recorder.events if event["event"] == "call")
    assert all(event["round"] == call["round"] for event in events)
    assert events[0]["endpoint_key"] == events[1]["endpoint_key"]
    assert events[0]["query_key"] == events[1]["query_key"]
    assert events[2]["endpoint_key"] != events[0]["endpoint_key"]
    assert all(event["response_bytes"] == len(httpx.Response(200, json=content).content)
               and event["response_complete"] for event in events)
    assert "PRIVATE_BODY_CANARY" not in json.dumps(events)
    assert "http_attempt" not in str(prompts) and "endpoint_key" not in str(prompts)
    assert tool_context.current() == (None, None)
    path = trace.finish_trace(recorder.trace_id, "producer_done", 1.0)
    group, = request_metrics.collect([path])["groups"]
    assert group["http_observed_requests"] == 1 and group["tool_requests"] == 1
    assert group["http_attempts"] == group["http_attempts_sum"] == len(requests) == 3
    assert group["http_retry_attempts"] == group["http_incomplete_attempts"] == 0
    assert group["http_response_bytes"] == 3 * len(httpx.Response(200, json=content).content)


def test_retry_failure_and_success_each_have_one_event_with_the_same_tool_identity(
        monkeypatch, recorder):
    requests = []
    clock = iter([1.0, 1.125, 2.0, 2.25])
    monkeypatch.setattr(http_observability, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    monkeypatch.setattr(channel, "RETRY_WAIT_SECONDS", 0)

    def respond(request):
        requests.append(request)
        return (httpx.Response(503, content=b"DO_NOT_READ") if len(requests) == 1
                else httpx.Response(200, json={"code": 200, "data": []}))

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with tool_evidence.scope({"id": "retry-call"}, 4):
            body, failure = channel.request_json(client, method="GET", url="https://unit.invalid/x")
    assert body["data"] == [] and failure is None
    events = http_events(recorder)
    assert len(events) == len(requests) == 2
    assert [event["attempt"] for event in events] == [1, 2]
    assert [event["status"] for event in events] == [503, 200]
    assert [event["duration_ms"] for event in events] == [125, 250]
    assert events[0]["error"] == channel.ERROR_UPSTREAM_UNAVAILABLE
    assert events[0]["response_bytes"] == 0 and not events[0]["response_complete"]
    assert events[1]["error"] is None and events[1]["response_complete"]
    assert all(event["call_id"] == "retry-call" and event["round"] == 4 for event in events)


def test_fingerprints_distinguish_query_variants_without_retaining_private_data(recorder):
    secrets = ["LOGIN_CANARY", "PASSWORD_CANARY", "AUTH_CANARY", "CONFIRM_CANARY",
               "QUERY_CANARY", "BODY_CANARY", "FRAGMENT_CANARY"]
    with httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"code": 200, "data": secrets}))) as client:
        for page in [1, 2, 1]:
            channel.request_json(client, method="POST", attempts=1, write=True,
                url=f"https://{secrets[0]}:{secrets[1]}@unit.invalid/private?page={page}"
                    f"&keyword={secrets[4]}#{secrets[6]}",
                headers={"Authorization": secrets[2]},
                json_body={"token": secrets[3], "content": secrets[5]})
    events = http_events(recorder)
    assert len(events) == 3
    assert events[0]["query_key"] == events[2]["query_key"] != events[1]["query_key"]
    assert all(event["endpoint_key"] == hashlib.sha256(b"/private").hexdigest()
               and event["host"] == "unit.invalid" for event in events)
    assert not any(secret in json.dumps(events) for secret in secrets)
    assert all(event["call_id"] is None and event["round"] is None for event in events)


class CountedStream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.consumed, self.closed = chunks, 0, False

    def __iter__(self):
        for chunk in self.chunks:
            self.consumed += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


@pytest.mark.parametrize("reason", ["limit", "declared_limit", "status", "malformed", "cancel"])
def test_failed_stream_records_partial_bytes_without_draining_or_retrying(recorder, reason):
    def chunks():
        yield b"x" * 65536
        pytest.fail("observation must not drain a failed response")

    stream = CountedStream([b"invalid"] if reason == "malformed" else chunks())
    headers = {"content-length": "999999"} if reason == "declared_limit" else {}

    def check():
        if reason == "cancel" and stream.consumed:
            raise asyncio.CancelledError("SECRET_CANCEL_REASON")
        return 1

    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            403 if reason == "status" else 200, headers=headers, stream=stream))) as client:
        if reason == "cancel":
            with pytest.raises(asyncio.CancelledError):
                channel.request_json(client, method="GET", url="https://unit.invalid/limited",
                                     max_response_bytes=128, timeout=check)
        else:
            body, failure = channel.request_json(
                client, method="GET", url="https://unit.invalid/limited", max_response_bytes=128)
            assert body is None and failure.attempts == 1
    event, = http_events(recorder)
    assert stream.closed and event["response_bytes"] == stream.consumed
    assert event["response_complete"] is (reason == "malformed")
    assert event["status"] == (403 if reason == "status" else 200)
    assert event["error"] is not None
    assert "SECRET_CANCEL_REASON" not in json.dumps(event)
    assert stream.consumed == (0 if reason in {"status", "declared_limit"}
                               else 7 if reason == "malformed" else 65536)


def test_gzip_measures_decoded_bytes_and_not_compressed_wire_length(recorder):
    content = json.dumps({"code": 200, "data": "正文" * 100}).encode()
    encoded = gzip.compress(content)
    stream = CountedStream([encoded])
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-encoding": "gzip", "content-length": str(len(encoded))},
            stream=stream))) as client:
        body, failure = channel.request_json(client, method="GET", url="https://unit.invalid/body")
    event, = http_events(recorder)
    assert failure is None and body["data"] == "正文" * 100
    assert event["response_bytes"] == len(content) > stream.consumed == len(encoded)
    assert event["response_complete"] and stream.closed


def test_transport_failure_is_recorded_once_and_write_is_never_replayed(recorder):
    requests = []

    def respond(request):
        requests.append(request)
        raise httpx.ReadTimeout("SECRET_TRANSPORT_EXCEPTION", request=request)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        body, failure = channel.request_json(client, method="POST", url="https://unit.invalid/write",
                                            write=True, attempts=1)
    event, = http_events(recorder)
    assert body is None and failure.attempts == len(requests) == 1
    assert event["status"] is None and event["response_bytes"] == 0
    assert event["error"] == channel.ERROR_WRITE_UNKNOWN and not event["response_complete"]
    assert "SECRET_TRANSPORT_EXCEPTION" not in json.dumps(event)


@pytest.mark.parametrize("reason", ["stop", "deadline", "invalid_limit"])
def test_presend_rejection_does_not_record_an_http_attempt(recorder, reason):
    def deadline():
        raise TimeoutError("deadline before send")

    transport = httpx.MockTransport(lambda _: pytest.fail("request sent"))
    with httpx.Client(transport=transport) as client:
        kwargs = {"stop": lambda: True} if reason == "stop" else (
            {"timeout": deadline} if reason == "deadline" else {"max_response_bytes": 0})
        if reason == "stop":
            assert channel.request_json(client, method="GET", url="https://unit.invalid/x",
                                        **kwargs)[1].attempts == 0
        else:
            with pytest.raises(TimeoutError if reason == "deadline" else ValueError):
                channel.request_json(client, method="GET", url="https://unit.invalid/x", **kwargs)
    assert http_events(recorder) == []


def test_nested_tool_identity_resets_after_failure(recorder):
    with tool_evidence.scope({"id": "outer"}, 1):
        with pytest.raises(ValueError), tool_evidence.scope({"id": "inner"}, 2):
            assert tool_context.current() == ("inner", 2)
            raise ValueError("synthetic failure")
        assert tool_context.current() == ("outer", 1)
    assert tool_context.current() == (None, None)


def test_preparation_facts_keep_the_same_scoped_call_identity(recorder):
    with tool_evidence.scope({"id": "preparation-call"}, 3):
        tool_evidence.observed("synthetic", {"id": 9, "draft": "DO_NOT_RECORD"}, ("id",))
    event, = recorder.events
    assert event["event"] == "preparation_observation"
    assert event["call_id"] == "preparation-call" and event["round"] == 3
    assert event["facts"] == {"id": 9} and "DO_NOT_RECORD" not in json.dumps(event)


def test_callback_exception_records_only_its_type_and_not_arbitrary_attributes(recorder):
    class DeadlineError(TimeoutError):
        code = "SECRET_CALLBACK_CODE"

    checks = []

    def timeout():
        checks.append(True)
        if len(checks) > 1:
            raise DeadlineError("SECRET_CALLBACK_MESSAGE")
        return 1

    stream = CountedStream([b"x" * 65536])
    with httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=stream))) as client:
        with pytest.raises(DeadlineError):
            channel.request_json(client, method="GET", url="https://unit.invalid/x",
                                 timeout=timeout)
    event, = http_events(recorder)
    assert event["error"] == "DeadlineError" and stream.closed
    assert "SECRET_CALLBACK" not in json.dumps(event)
