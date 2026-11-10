"""Live evaluations use explicit private sessions and host-issued delegated credentials."""

import json
import secrets

import httpx
import pytest

from config.settings import settings
from eval import operator_credentials
from eval.online_transport import EvaluationError, OnlineTransport


def session_file(monkeypatch, tmp_path):
    token = secrets.token_urlsafe(24)
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"7": token}))
    path.chmod(0o600)
    monkeypatch.setenv("SAUDADE_OPERATOR_SESSIONS_FILE", str(path))
    return path, token


def test_operator_sessions_require_explicit_private_files(monkeypatch, tmp_path):
    monkeypatch.delenv("SAUDADE_OPERATOR_SESSIONS_FILE", raising=False)
    with pytest.raises(PermissionError, match="private"):
        operator_credentials.session(7)
    path, token = session_file(monkeypatch, tmp_path)
    assert operator_credentials.session(7) == token
    with pytest.raises(PermissionError, match="no authenticated session"):
        operator_credentials.session(8)
    path.chmod(0o644)
    with pytest.raises(PermissionError, match="0600"):
        operator_credentials.session(7)


def test_operator_delegation_uses_the_provided_session_and_preserves_denial(monkeypatch, tmp_path):
    _, token = session_file(monkeypatch, tmp_path)
    delegated = secrets.token_urlsafe(24)
    real_client = httpx.Client
    status = {"value": 200}
    requests = []

    def handle(request):
        requests.append(request)
        assert request.url.path == "/api/protected/agent/delegation"
        assert request.headers["Authorization"] == "Bearer " + token
        return httpx.Response(status["value"], json={"data": {"token": delegated}})

    monkeypatch.setattr(settings, "agent_admin_base", "https://fixture.invalid")
    monkeypatch.setattr(
        operator_credentials.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    assert operator_credentials.tools(7) == delegated
    status["value"] = 401
    with pytest.raises(httpx.HTTPStatusError):
        operator_credentials.tools(7)
    assert len(requests) == 2


def test_online_driver_checks_host_role_against_expected_role(monkeypatch, tmp_path):
    _, token = session_file(monkeypatch, tmp_path)

    def handle(request):
        assert request.headers["Authorization"] == "Bearer " + token
        return httpx.Response(200, json={"data": {"role": "user"}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        driver = OnlineTransport(
            agent_url="https://agent.invalid",
            blog_url="https://blog.invalid",
            report_dir=tmp_path,
            client=client,
            host=True,
        )
        assert driver.profile(7, {"user"}) == "user"
        with pytest.raises(EvaluationError, match="expected"):
            driver.profile(7, {"admin"})
