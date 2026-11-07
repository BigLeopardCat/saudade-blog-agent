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


def cleanup_headers(conversation_id, *, uid=7, turn_id=None, **changes):
    """Sign the same scoped maintenance claims as the Rust outbox, using a test key."""
    import time

    import jwt

    from config.settings import settings

    assert settings.agent_assertion_secret, "use the host_identity fixture"
    grant = {"operation": "delete_conversation" if turn_id is None else "drop_turn",
             "conversation_id": conversation_id}
    if turn_id is not None:
        grant["turn_id"] = turn_id
    claims = {"sub": uid, "aud": "agent-checkpoint", "exp": int(time.time()) + 60,
              "checkpoint": grant, **changes}
    return {"X-Agent-Assertion": jwt.encode(claims, settings.agent_assertion_secret,
                                           algorithm="HS256")}
