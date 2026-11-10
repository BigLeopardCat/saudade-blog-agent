"""Host diagnostics come from authenticated blog observations, never local machine state."""
import copy
from datetime import datetime

import httpx
import pytest
from _framework_fixture import call, model_replies
from _own_support import config
from langchain_core.messages import HumanMessage

from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.session import ToolSession
from tools import base
from tools.diagnostics import get_server_status, get_service_health


@pytest.fixture
def backend(monkeypatch):
    data = {"schemaVersion": 1, "observedAt": "2026-11-10T10:00:00+00:00", "scope": "blog_host",
        "host": {"cpuCount": 4, "cpuPercent": None, "cpuSampleAt": None,
            "cpuSampleWindowSeconds": None, "load": [0.1, 0.2, 0.3],
            "memory": {"totalBytes": 8 * 1024**3, "availableBytes": 3 * 1024**3,
                "swapTotalBytes": 0, "swapUsedBytes": 0},
            "disks": [{"mount": "/", "totalBytes": 40 * 1024**3,
                "availableBytes": 10 * 1024**3}], "disksTruncated": False,
            "uptimeSeconds": 86400},
        "services": [{"name": "blog", "state": "running"},
            {"name": "database", "state": "ready"}, {"name": "agent", "state": "not_configured"}]}
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/protected/diagnostics"
        assert request.headers["Authorization"].startswith("Bearer ")
        return httpx.Response(200, json={"code": 200, "data": data})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield data, requests


def admin():
    return config(principal=Principal(7, "admin", SOURCE_ASSERTION))


def test_server_status_keeps_unknown_cpu_and_the_host_observation(backend):
    data, requests = backend
    result = get_server_status.func(admin())
    assert result.kind == "ok"
    assert "博客宿主" in str(result) and "尚无完整采样" in str(result)
    assert "8.0 GB" in str(result) and "40.0 GB" in str(result)
    assert result.data["host"]["cpuPercent"] is None
    observed = datetime.fromisoformat(result.data["observedAt"])
    assert observed == datetime.fromisoformat(data["observedAt"])
    assert len(requests) == 1


def test_optional_agent_and_service_failure_are_distinct_from_healthy(backend):
    data, requests = backend
    result = get_service_health.func(admin())
    assert "Agent 服务：未配置" in str(result) and "博客数据库：就绪" in str(result)
    data["services"][1]["state"] = "unavailable"
    result = get_service_health.func(admin())
    assert "博客数据库：本次核验不可用" in str(result)
    assert "今日对话" not in str(result) and "今日服务请求" not in str(result)
    assert len(requests) == 2


@pytest.mark.parametrize("role", ["user", "zako", "unknown"])
def test_nonadmin_cannot_start_a_host_read(backend, role):
    _, requests = backend
    cfg = config(principal=Principal(7, role, SOURCE_ASSERTION))
    assert get_server_status.func(cfg).kind == "unavailable"
    assert get_service_health.func(cfg).kind == "unavailable"
    assert requests == []


@pytest.mark.parametrize("fault", ["scope", "memory", "cpu", "sample", "service"])
def test_invalid_measurements_never_become_a_healthy_report(backend, fault):
    data, _ = backend
    if fault == "scope":
        data["scope"] = "agent_machine"
    elif fault == "memory":
        data["host"]["memory"]["availableBytes"] = 99 * 1024**3
    elif fault == "cpu":
        data["host"]["cpuPercent"] = 101
    elif fault == "sample":
        data["host"]["cpuPercent"] = 5
    else:
        data["services"][-1] = copy.deepcopy(data["services"][0])
    assert get_server_status.func(admin()).kind == "unavailable"
    assert get_service_health.func(admin()).kind == "unavailable"


def test_completed_sample_keeps_timestamp_and_interval(backend):
    data, _ = backend
    data["host"].update(cpuPercent=12.5, cpuSampleAt=data["observedAt"],
                        cpuSampleWindowSeconds=3.25)
    result = get_server_status.func(admin())
    assert "12.5%" in str(result) and "3.25 秒" in str(result)


def test_current_framework_receives_typed_host_evidence(backend):
    from agent.products.blog.capabilities import ToolCatalog
    from agent.products.blog.tool_policy import BlogToolMiddleware

    cfg = admin()
    tools = ToolCatalog().domains(cfg, False)["admin_reports"]
    with model_replies({"tool_calls": [call("get_server_status", "host")]},
                       {"content": "博客宿主内存总量是 8 GB，CPU 仍在采样。"}) as (model, requests):
        result = ToolSession(model, cfg, tools, {}, middleware=(BlogToolMiddleware,)).run([
            HumanMessage(content="看看博客服务器的内存与CPU。")])
    assert "8 GB" in result.reply
    assert len(backend[1]) == 1
    assert not (result.execution or {}).get("pending_confirm")
    tool_messages = [message for message in requests[-1]["messages"] if message["role"] == "tool"]
    assert len(tool_messages) == 1
    evidence = tool_messages[0]["content"]
    assert "博客宿主" in evidence and "2026-11-10" in evidence
    assert "8.0 GB" in evidence and "尚无完整采样" in evidence
