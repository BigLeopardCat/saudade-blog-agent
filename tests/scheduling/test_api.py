"""Scheduling endpoints share the signed administrator boundary and do not cache private data."""

import secrets
import time
from types import SimpleNamespace

import jwt
import pytest
from _service import app
from fastapi.testclient import TestClient

from config.settings import settings


@pytest.mark.parametrize(
    "role,expected", [(None, 401), ("user", 403), ("zako", 403), ("admin", 200)]
)
def test_api_rejects_untrusted_actors_before_contacting_service(monkeypatch, role, expected):
    monkeypatch.setattr(settings, "agent_assertion_secret", secrets.token_hex(32))
    calls = []

    def listing(principal, limit, offset):
        calls.append(principal)
        return {"items": [], "has_more": False}

    monkeypatch.setattr(app.state, "scheduling", SimpleNamespace(list=listing))
    headers = (
        {}
        if role is None
        else {
            "X-Agent-Assertion": jwt.encode(
                {"sub": 7, "role": role, "aud": "agent", "exp": time.time() + 60},
                settings.agent_assertion_secret,
                algorithm="HS256",
            )
        }
    )
    client = TestClient(app)
    try:
        response = client.get("/automations", headers=headers)
    finally:
        client.close()
    assert response.status_code == expected
    assert len(calls) == (1 if expected == 200 else 0)
    if expected == 200:
        assert response.headers["Cache-Control"] == "no-store"
