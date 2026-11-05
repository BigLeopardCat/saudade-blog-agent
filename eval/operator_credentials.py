"""Explicit operator login credentials for live probes; never mint login tokens."""

import json
import os
from pathlib import Path

import httpx


def session(uid):
    path = os.environ.get("SAUDADE_OPERATOR_SESSIONS_FILE")
    if not path:
        raise PermissionError(
            "set SAUDADE_OPERATOR_SESSIONS_FILE to a private uid-to-session-token JSON file"
        )
    source = Path(path)
    if source.stat().st_mode & 0o077:
        raise PermissionError("operator session file must have mode 0600")
    token = json.loads(source.read_text()).get(str(uid))
    if not isinstance(token, str) or not token:
        raise PermissionError("operator has no authenticated session for this actor")
    return token


def tools(uid):
    from config.settings import settings

    with httpx.Client(trust_env=False, timeout=15) as client:
        response = client.get(
            settings.agent_admin_base.rstrip("/") + "/api/protected/agent/delegation",
            headers={"Authorization": "Bearer " + session(uid)},
        )
        response.raise_for_status()
        return response.json()["data"]["token"]


def assertion(uid, role, ttl=60):
    import time

    import jwt

    from config.settings import settings

    if len(settings.agent_assertion_secret) < 32:
        raise PermissionError("host assertion key is unavailable")
    return jwt.encode(
        {
            "sub": uid,
            "role": role,
            "aud": "agent",
            "exp": int(time.time()) + ttl,
            "delegation": tools(uid),
        },
        settings.agent_assertion_secret,
        algorithm="HS256",
    )
