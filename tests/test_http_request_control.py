"""Every retry obeys the active request, including public body-based reads."""

from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from agent.runtime import control
from tools import base


@pytest.mark.parametrize("stop", [False, True])
@pytest.mark.parametrize("private", [False, True])
def test_active_request_stops_another_attempt(monkeypatch, stop, private):
    now, calls, event = [0.0], [], Event()
    monkeypatch.setattr(control, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(base, "_jwt_secret", lambda: "offline-request-control")

    def handle(request):
        calls.append(request)
        assert all(0 < value <= 3 for value in request.extensions["timeout"].values())
        if stop:
            event.set()
        else:
            now[0] = 4
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        with control.request_scope(control.RequestControl(event, 3)):
            with pytest.raises(control.AgentCancelled if stop else TimeoutError):
                if private:
                    from agent.principal import SOURCE_ASSERTION, Principal

                    base._admin_get("/api/protected/stats", {"configurable": {
                        "user_id": 7, "principal": Principal(7, "admin", SOURCE_ASSERTION)}})
                else:
                    base._get("/notes/search", payload={"keyword": "example"})
    assert len(calls) == 1


def test_failure_logs_exclude_query_values_and_credentials(monkeypatch, caplog):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(base, "API_BASE", "https://username:private-password@example.test")
        base._get("/notes?token=private-token&keyword=private-query")
    assert "request failed" in caplog.text
    assert not any(value in caplog.text for value in (
        "private-password", "private-token", "private-query"))
