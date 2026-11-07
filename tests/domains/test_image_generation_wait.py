"""Observe actual SSE bytes without approving or replaying a paid operation."""

import copy
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from _own_support import config

from agent.domains.image_generation import tools, waiting
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.control import AgentCancelled, RequestControl, request_scope
from agent.runtime.progress import ProgressEmitter
from tools import base

ID = "6b9e2bf1-56af-428c-8177-e3ad0641e036"
OTHER = "7b9e2bf1-56af-428c-8177-e3ad0641e036"


def job(status="generating", **extra):
    return {"id": ID, "status": status, "requestHash": "a" * 64, "imageRemoved": False,
            "request": {"provider": "https://images.test/v1", "prompt": "frozen prompt",
                        "model": "fixture", "size": "1536x1024", "quality": "high",
                        "references": []}, **extra}


def event(value, kind="image_job"):
    return f"event: {kind}\ndata: {json.dumps(value)}\n\n".encode()


class Stream(httpx.SyncByteStream):
    def __init__(self, *chunks, before=None):
        self.chunks, self.before, self.closed = chunks, before, False

    def __iter__(self):
        for index, chunk in enumerate(self.chunks):
            if self.before:
                self.before(index)
            yield chunk

    def close(self):
        self.closed = True


def observe(monkeypatch, stream, *, headers=None, control=None, status=200):
    requests, progress = [], []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers=headers or {"Content-Type": "text/event-stream"},
                              stream=stream)

    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    cfg["configurable"]["progress_emitter"] = ProgressEmitter(progress.append)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        with request_scope(control or RequestControl(threading.Event(), time.monotonic() + 300)):
            result = tools.get_image_generation.invoke({"job_id": ID}, config=cfg)
    return result, requests, progress


def test_real_stream_review_has_safe_model_projection_and_actual_work(monkeypatch):
    signed = f"/api/public/image-generation/{ID}/preview?token=secret-preview"
    stream = Stream(b": keepalive\n\n", event(job()),
                    event(job("review", outputHash="b" * 64, previewUrl=signed)))
    result, requests, progress = observe(monkeypatch, stream)
    assert result.kind == "ok" and result.data["status"] == "review"
    assert result.data["hasPreview"] is True and result.data["waitOutcome"] == "observed"
    assert "secret-preview" not in str(result) and "frozen prompt" not in str(result)
    assert len(requests) == 1 and requests[0].method == "GET"
    assert requests[0].url.params["wait_seconds"] == "240"
    assert [item["work"]["stage"] for item in progress] == ["generating", "review"]
    assert stream.closed


@pytest.mark.parametrize("status", ["generating", "saving"])
def test_timeout_is_verified_unfinished_state_not_gallery_success(monkeypatch, status):
    stream = Stream(event(job(status)), event(job(status), "image_job_timeout"))
    result, requests, _ = observe(monkeypatch, stream)
    assert result.kind == "ok" and result.data["status"] == status
    assert result.data["waitOutcome"] == "timeout" and result.data["imageId"] is None
    assert len(requests) == 1 and stream.closed


def test_save_accepted_requires_real_gallery_identifier(monkeypatch):
    result, _, progress = observe(monkeypatch, Stream(event(job("saving")),
        event(job("accepted", imageId=31))))
    assert result.data["imageId"] == 31
    assert [item["work"]["status"] for item in progress] == ["running", "completed"]


@pytest.mark.parametrize("chunks", [
    [event(job("accepted"))],
    [event(job("review", id=OTHER))],
    [event(job()), event(job("review", requestHash="c" * 64))],
    [event({"code": "unavailable"}, "image_job_error")],
    [b"event: image_job\ndata: {broken\n\n"],
    [event(job()), b": keepalive\n\n"],
    [event(job(), "unexpected")],
])
def test_unverified_or_broken_stream_never_claims_completion(monkeypatch, chunks):
    stream = Stream(*copy.deepcopy(chunks))
    result, requests, _ = observe(monkeypatch, stream)
    assert result.kind == "unavailable" and stream.closed
    assert len(requests) == 1 and requests[0].method == "GET"


@pytest.mark.parametrize("headers", [
    {"Content-Type": "application/json"},
    {"Content-Type": "text/event-stream", "Content-Encoding": "gzip"},
])
def test_protocol_rejection_closes_response(monkeypatch, headers):
    stream = Stream(event(job("review")))
    result, _, _ = observe(monkeypatch, stream, headers=headers)
    assert result.kind == "unavailable" and stream.closed


@pytest.mark.parametrize("status", [401, 429, 503])
def test_http_failure_is_unavailable_without_automatic_retry(monkeypatch, status):
    stream = Stream(event(job("review")))
    result, requests, _ = observe(monkeypatch, stream, status=status)
    assert result.kind == "unavailable" and stream.closed and len(requests) == 1


def test_stream_read_failure_closes_response_and_does_not_claim_saved(monkeypatch):
    def fail(index):
        if index == 1:
            raise httpx.ReadError("synthetic stream interruption")

    stream = Stream(event(job("saving")), b": keepalive\n\n", before=fail)
    result, requests, _ = observe(monkeypatch, stream)
    assert result.kind == "unavailable" and stream.closed and len(requests) == 1


def test_ignored_keepalives_are_also_counted_in_byte_limit(monkeypatch):
    stream = Stream(event(job()), b":" + b"x" * waiting.MAX_STREAM_BYTES + b"\n\n",
                    event(job("review")))
    result, _, _ = observe(monkeypatch, stream)
    assert result.kind == "unavailable" and stream.closed


def test_cancellation_during_keepalive_closes_stream_without_post(monkeypatch):
    stopped = threading.Event()
    stream = Stream(event(job()), b": keepalive\n\n", event(job("review")),
                    before=lambda index: stopped.set() if index == 1 else None)
    with pytest.raises(AgentCancelled):
        observe(monkeypatch, stream,
                control=RequestControl(stopped, time.monotonic() + 300))
    assert stream.closed


def test_wait_uses_remaining_request_deadline(monkeypatch):
    result, requests, _ = observe(monkeypatch, Stream(event(job(), "image_job_timeout")),
        control=RequestControl(threading.Event(), time.monotonic() + 30))
    assert result.kind == "ok" and 1 <= int(requests[0].url.params["wait_seconds"]) <= 7
    assert requests[0].extensions["timeout"]["read"] <= 3


def test_insufficient_finish_reserve_does_not_open_a_long_read(monkeypatch):
    stream = Stream(event(job(), "image_job_timeout"))
    result, requests, _ = observe(monkeypatch, stream,
        control=RequestControl(threading.Event(), time.monotonic() + 15))
    assert result.kind == "unavailable" and requests == []


def test_final_blocking_read_cannot_spend_the_fifteen_second_finish_reserve(monkeypatch):
    clock, requests = [0.0], []
    remaining = 80
    monkeypatch.setattr(waiting, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(waiting, "model_options", lambda _: {"timeout": remaining})

    def progress(job, config):
        # The next real read starts just before the observation window ends.
        clock[0] = int(requests[0].url.params["wait_seconds"]) + 1 - 0.01

    monkeypatch.setattr(waiting, "show_progress", progress)

    def last_read(index):
        if index == 1:
            clock[0] += waiting.IO_TIMEOUT_SECONDS

    stream = Stream(event(job()), b": late keepalive\n\n", event(job("accepted", imageId=31)),
                    before=last_read)

    def host(request):
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    with httpx.Client(transport=httpx.MockTransport(host)) as client:
        monkeypatch.setattr(base, "_client", client)
        verified, outcome = waiting.wait(ID, 240, cfg, tools.PATH)
    assert verified.status == "generating" and outcome == "timeout"
    assert remaining - clock[0] >= waiting.FINISH_RESERVE_SECONDS
    assert len(requests) == 1 and stream.closed


@pytest.mark.parametrize("seconds", [True, -1, 241])
def test_invalid_wait_window_never_reaches_the_host(monkeypatch, seconds):
    def unexpected(request):
        raise AssertionError("invalid window opened a request")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        monkeypatch.setattr(base, "_client", client)
        with pytest.raises(ValueError):
            tools.get_image_generation.invoke({"job_id": ID, "wait_seconds": seconds},
                config=config(principal=Principal(7, "admin", SOURCE_ASSERTION)))


def test_role_or_user_binding_rejected_before_opening_sse(monkeypatch):
    def unexpected(request):
        raise AssertionError("unauthorized SSE request")

    with httpx.Client(transport=httpx.MockTransport(unexpected)) as client:
        monkeypatch.setattr(base, "_client", client)
        for cfg in (config(principal=Principal(7, "user", SOURCE_ASSERTION)),
                    config(principal=Principal(8, "admin", SOURCE_ASSERTION))):
            cfg["configurable"]["user_id"] = 7
            result = tools.get_image_generation.invoke({"job_id": ID}, config=cfg)
            assert result.kind == "unavailable"


def test_removed_accepted_job_is_historical_not_a_new_image_to_show(monkeypatch):
    result, requests, progress = observe(monkeypatch,
        Stream(event(job("accepted", imageId=31, imageRemoved=True))))
    assert result.kind == "ok" and result.data["status"] == "accepted"
    assert result.data["imageRemoved"] is True and not result.data["hasPreview"]
    assert result.data["imageId"] == 31 and "图片已从图库移除" in progress[-1]["text"]
    assert len(requests) == 1 and requests[0].method == "GET"


@pytest.mark.parametrize("value", [
    job("accepted", imageRemoved=True),
    job("review", imageRemoved=True),
    job("accepted", imageId=31, imageRemoved=True, previewUrl="https://example.invalid/old"),
    {key: value for key, value in job("accepted", imageId=31).items() if key != "imageRemoved"},
])
def test_removed_state_cannot_weaken_identity_or_lie_about_current_preview(monkeypatch, value):
    result, _, _ = observe(monkeypatch, Stream(event(value)))
    assert result.kind == "unavailable"
