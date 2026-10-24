"""Exercise mounted service routes without starting models, databases or build jobs."""

import asyncio
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import jwt
import pytest
from fastapi.testclient import TestClient

import server
from agent.api.routes import knowledge_graph, moderation
from config.settings import settings
from utils.logging import get_trace_id

BUILD_ENDPOINTS = [
    ("POST", "/graph/rebuild"),
    ("GET", "/graph/rebuild/status"),
    ("POST", "/graph/rebuild/cancel"),
]
SERVICE_ENDPOINTS = [("POST", "/review"), ("POST", "/graph/query"), *BUILD_ENDPOINTS]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "jwt_secret", secrets.token_hex(32))
    monkeypatch.setattr(settings, "agent_require_assertion", True)
    # No lifespan: service endpoints must work independently of a chat runtime.
    monkeypatch.setattr(server, "_agent", None)
    result = TestClient(server.app, raise_server_exceptions=False)
    yield result
    result.close()


def signed(role="admin", *, ttl=60):
    assertion = jwt.encode({"sub": 7, "role": role, "aud": "agent",
                            "exp": int(time.time()) + ttl},
                           settings.jwt_secret, algorithm="HS256")
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
    monkeypatch.setattr(knowledge_graph.wordgraph, "query_words", forbidden)
    for name in ("start", "status", "cancel"):
        monkeypatch.setattr(knowledge_graph.graph_build, name, forbidden)
    headers = {"X-Agent-Assertion": secrets.token_urlsafe()} if identity == "invalid" else {}
    if identity == "expired":
        headers = signed(ttl=-10)
    assert request(client, method, path, headers).status_code == 401


@pytest.mark.parametrize("method,path", BUILD_ENDPOINTS)
@pytest.mark.parametrize("role", ["user", "zako", "unknown", None])
def test_build_permissions_ignore_body_identity_and_role(client, monkeypatch, method, path, role):
    def forbidden(*args, **kwargs):
        pytest.fail("non-admin request reached build operations")

    for name in ("start", "status", "cancel"):
        monkeypatch.setattr(knowledge_graph.graph_build, name, forbidden)
    assert request(client, method, path, signed(role), role="superadmin").status_code == 403


def test_compatibility_identity_does_not_grant_console_permissions(client, monkeypatch):
    monkeypatch.setattr(settings, "agent_require_assertion", False)
    response = request(client, "POST", "/graph/rebuild", role="superadmin")
    assert response.status_code == 403


@pytest.mark.parametrize("role", ["admin", "superadmin"])
@pytest.mark.parametrize("excluded", [None, "", "9,10"])
def test_build_parameters_and_results_round_trip(client, monkeypatch, role, excluded):
    seen = []

    def start(params, mode):
        seen.append((params, mode))
        return {"ok": False, "reason": "busy"}

    monkeypatch.setattr(knowledge_graph.graph_build, "start", start)
    response = request(client, "POST", "/graph/rebuild", signed(role),
                       mode="precheck", exclude_ids=excluded, argv="untrusted")
    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "busy"}
    params, mode = seen.pop()
    assert mode == "precheck" and params["exclude_ids"] == excluded
    assert not {"uid", "mode", "role", "argv"}.intersection(params)


@pytest.mark.parametrize("method,path,name", [
    ("GET", "/graph/rebuild/status", "status"),
    ("POST", "/graph/rebuild/cancel", "cancel"),
])
def test_build_state_operations_forward_results(client, monkeypatch, method, path, name):
    result = {"ok": False, "reason": "not_running", "run_id": "fixture"}
    monkeypatch.setattr(knowledge_graph.graph_build, name, lambda: result)
    response = request(client, method, path, signed())
    assert response.status_code == 200 and response.json() == result


def test_graph_degradation_and_context_survive_worker_dispatch(client, monkeypatch):
    def query(q, limit):
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        assert q == "测试查询" and limit == knowledge_graph.wordgraph.TOP_K_DEFAULT
        assert get_trace_id() == "router-thread-test"
        return {"ok": False, "reason": "no_index", "words": []}

    monkeypatch.setattr(knowledge_graph.wordgraph, "query_words", query)
    response = request(client, "POST", "/graph/query",
                       {**signed("user"), "X-Request-ID": "router-thread-test"})
    assert response.status_code == 200
    assert response.json() == {"ok": False, "reason": "no_index", "words": []}
    assert response.headers["X-Request-ID"] == "router-thread-test"


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
    ("/graph/query", "q", "x" * 129),
    ("/graph/rebuild", "site", "x" * 201),
])
def test_request_limits_remain_enforced(client, path, field, value):
    assert request(client, "POST", path, signed(), **{field: value}).status_code == 422
