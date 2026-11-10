"""Exercise mounted service routes without starting models, databases or build jobs."""


import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import jwt
import pytest
from _service import app
from fastapi.testclient import TestClient

from agent.api.routes import moderation
from config.settings import settings

SERVICE_ENDPOINTS = [("POST", "/review")]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "agent_assertion_secret", secrets.token_hex(32))
    # No lifespan: service endpoints must work independently of a chat runtime.
    monkeypatch.setattr(app.state, "runtime", None)
    result = TestClient(app, raise_server_exceptions=False)
    yield result
    result.close()


def signed(role="admin", *, ttl=60):
    assertion = jwt.encode({"sub": 7, "role": role, "aud": "agent",
                            "exp": int(time.time()) + ttl},
                           settings.agent_assertion_secret, algorithm="HS256")
    return {"X-Agent-Assertion": assertion}


def request(client, method, path, headers=None, **body):
    payload = {"uid": 999, "content": "测试留言", "q": "测试查询", **body}
    return client.request(method, path, headers=headers,
                          params={"uid": 999} if method == "GET" else None,
                          json=payload if method == "POST" else None)


@pytest.mark.parametrize("method,path", SERVICE_ENDPOINTS)
@pytest.mark.parametrize("identity", ["missing", "invalid", "expired"])
def test_services_reject_untrusted_identity_before_work(client, monkeypatch, method, path,
                                                       identity):
    def forbidden(*args, **kwargs):
        pytest.fail("unauthenticated request reached business work")

    monkeypatch.setattr(moderation.moderator, "review", forbidden)
    headers = {"X-Agent-Assertion": secrets.token_urlsafe()} if identity == "invalid" else {}
    if identity == "expired":
        headers = signed(ttl=-10)
    assert request(client, method, path, headers).status_code == 401


def test_review_authenticates_before_admission_and_releases_on_failure(client, monkeypatch):
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(moderation, "_review_slots", slots)
    monkeypatch.setattr(moderation, "REVIEW_QUEUE_WAIT", 0)
    slots.acquire()
    try:
        assert request(client, "POST", "/review").status_code == 401
        assert request(client, "POST", "/review", signed()).status_code == 503
    finally:
        slots.release()

    def fail(*args, **kwargs):
        raise RuntimeError("private provider detail")

    monkeypatch.setattr(moderation.moderator, "review", fail)
    failed = request(client, "POST", "/review", signed())
    assert failed.status_code == 500 and "private provider detail" not in failed.text
    assert slots.acquire(blocking=False)
    slots.release()


def test_review_holds_permit_until_work_finishes_and_reports_usage(client, monkeypatch, caplog):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(moderation, "_review_slots", threading.BoundedSemaphore(1))
    monkeypatch.setattr(moderation, "REVIEW_QUEUE_WAIT", 0)

    def review(text, *, on_usage):
        assert text == "留言"
        entered.set()
        assert release.wait(3)
        on_usage({"input": 12, "output": 3})
        return {"verdict": "flag", "reason": "待人工审核"}

    monkeypatch.setattr(moderation.moderator, "review", review)
    with caplog.at_level("INFO", logger=moderation.__name__), ThreadPoolExecutor(1) as workers:
        pending = workers.submit(request, client, "POST", "/review", signed(), content=" 留言 ")
        try:
            assert entered.wait(3)
            assert request(client, "POST", "/review", signed()).status_code == 503
        finally:
            release.set()
        assert pending.result(timeout=3).json() == {"verdict": "flag", "reason": "待人工审核"}
    assert "uid=7" in caplog.text and "uid=999" not in caplog.text
    assert "用量" in caplog.text and "12" in caplog.text
    assert request(client, "POST", "/review", signed(), content=" 留言 ").status_code == 200


@pytest.mark.parametrize("path,field,value", [
    ("/review", "content", "x" * 4001),
    ("/review", "author", "x" * 101),
])
def test_request_limits_remain_enforced(client, path, field, value):
    assert request(client, "POST", path, signed(), **{field: value}).status_code == 422

@pytest.mark.parametrize("method,path",[("POST","/graph/query"),("POST","/graph/rebuild"),
    ("GET","/graph/rebuild/status"),("POST","/graph/rebuild/cancel")])
def test_graph_operations_are_not_mounted_in_the_agent(client,method,path):
    assert request(client,method,path,signed()).status_code == 404
