"""Invalid signed executor bindings are rejected before worker, model or business I/O."""

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from types import SimpleNamespace

import httpx
import pytest

from agent import confirm
from agent.api.routes.chat import INVALID_CONFIRM_TEXT
from agent.main import create_app


@pytest.mark.parametrize("change", [
    {"executor": None}, {"executor": "unknown"}, {"executor": True},
    {"uid": 7.0}, {"conv": 42.0},
])
@pytest.mark.parametrize("path", ["/chat", "/confirmation/cancel"])
def test_malformed_signed_bindings_never_enter_execution(monkeypatch, host_identity, change, path):
    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    payload = {"v": confirm._VERSION, "uid": 7, "conv": 42,
               "workflow": "favorite_add", "executor": "favorites", "exp": int(time.time()) + 60,
               "jti": secrets.token_hex(16),
               "specs": [{"tool": "add_favorite", "args": {"article_id": 19}}], **change}
    if payload["executor"] is None:
        payload.pop("executor")
    body = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(confirm.settings.confirmation_secret.encode(),
                         confirm._DOMAIN + body, hashlib.sha256).digest()
    token = confirm._b64e(body) + "." + confirm._b64e(signature)
    app = create_app()
    calls = []

    async def run(*args):
        calls.append(args)
        pytest.fail("Malformed authorization must not enter any worker or business operation")

    app.state.runtime = object()
    app.state.workers = SimpleNamespace(closing=False, run=run)

    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://test") as client:
            payload = {"user_id": 7, "conversation_id": 42}
            if path == "/chat":
                payload.update(message="确认", confirm_token=token)
            else:
                payload["token"] = token
            return await client.post(path, json=payload, headers=host_identity())

    response = asyncio.run(request())
    if path == "/chat":
        assert response.status_code == 200 and response.json()["reply"] == INVALID_CONFIRM_TEXT
    else:
        assert response.status_code == 403
    assert not calls and token not in response.text
