"""Construct requests for adapter unit tests without starting service dependencies."""

from fastapi import Request

from agent.main import app


def service_request(application=app, *, headers=None):
    return Request(
        {
            "type": "http",
            "app": application,
            "method": "POST",
            "path": "/chat",
            "headers": [(key.lower().encode(), value.encode())
                        for key, value in (headers or {}).items()],
            "query_string": b"",
        }
    )


def assertion_headers(uid=7, role="user"):
    """Issue a short-lived fixture assertion using the explicitly installed test key."""
    import time

    import jwt

    from config.settings import settings

    assert settings.agent_assertion_secret, "use the host_identity fixture"
    token = jwt.encode({"sub": uid, "role": role, "aud": "agent", "exp": int(time.time()) + 60},
                       settings.agent_assertion_secret, algorithm="HS256")
    return {"X-Agent-Assertion": token}
