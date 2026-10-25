"""Only verified actors can read the bounded shared demonstration device list."""

import json
import secrets

import httpx
import jwt
import pytest
from pydantic import ValidationError

from agent.domains import device_reads
from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from tools import device


def config(uid=7, source=SOURCE_ASSERTION):
    return {"configurable": {"user_id": uid, "principal": Principal(uid, "user", source)}}


@pytest.fixture
def service(monkeypatch):
    secret = secrets.token_urlsafe(32)
    monkeypatch.setattr(device, "_jwt_secret", lambda: secret)
    monkeypatch.setattr(device_reads.settings, "iot_enabled", True)
    state = {"rows": [{"id": f"esp-{i}", "name": "演示设备" + str(i), "online": bool(i % 2),
                       "config": {"secret": secrets.token_urlsafe(32)}} for i in range(12)],
             "requests": [], "status": 200}

    def handle(request):
        state["requests"].append(request)
        assert request.method == "GET" and request.url.path == "/api/devices"
        token = request.headers["Authorization"].removeprefix("Bearer ")
        claims = jwt.decode(token, secret, algorithms=["HS256"], options={"verify_sub": False})
        assert claims["sub"] == 7 and claims["role"] == "user"
        assert request.extensions["timeout"]["read"] == 10
        if state.get("timeout"):
            raise httpx.ReadTimeout("test outage", request=request)
        return httpx.Response(state["status"], json=state["rows"])

    original_stream = httpx.stream
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(httpx, "stream", lambda *a, **kw: client.stream(*a, **kw))
        yield state
    monkeypatch.setattr(httpx, "stream", original_stream)


def read(**args):
    return device_reads.list_devices.invoke(args, config=config())


def test_bounded_projection_reports_shared_access_and_real_totals(service):
    first, last = json.loads(str(read())), json.loads(str(read(page=3)))
    assert first["total"] == last["total"] == 12
    assert len(first["items"]) == 5 and len(last["items"]) == 2
    assert first["hasMore"] and not last["hasMore"]
    assert first["access"] == "current_account_and_shared_demo"
    assert set(first["items"][0]) == {"id", "name", "online", "nameTruncated"}
    assert json.loads(str(read(page=4)))["items"] == []
    rendered = json.dumps(first, ensure_ascii=False)
    assert all(row["config"]["secret"] not in rendered for row in service["rows"])


@pytest.mark.parametrize("case", ["body", "mismatch", "missing", "boolean", "none"])
def test_identity_failures_never_sign_or_contact_service(service, case):
    cfg = config(source=SOURCE_BODY) if case == "body" else config()
    if case == "mismatch":
        cfg["configurable"]["user_id"] = 8
    elif case == "missing":
        cfg["configurable"].pop("user_id")
    elif case == "boolean":
        cfg["configurable"]["user_id"] = True
    elif case == "none":
        cfg = {}
    result = device_reads.list_devices.invoke({}, config=cfg)
    assert result.kind == "unavailable" and not service["requests"]


def test_disabled_iot_is_unavailable_without_network(service, monkeypatch):
    monkeypatch.setattr(device_reads.settings, "iot_enabled", False)
    assert not device_reads.enabled()
    assert read().kind == "unavailable" and not service["requests"]


@pytest.mark.parametrize("status", [302, 401, 403, 404, 500])
def test_device_http_failure_never_becomes_an_empty_list(service, status):
    service["status"] = status
    assert read().kind == "unavailable"


@pytest.mark.parametrize("case", ["object", "online", "id", "duplicate", "timeout", "oversized"])
def test_device_malformed_and_unbounded_responses_fail(service, case):
    if case == "object":
        service["rows"] = {"error": "database failure"}
    elif case == "online":
        service["rows"][0]["online"] = "false"
    elif case == "id":
        service["rows"][0]["id"] = "../../device"
    elif case == "duplicate":
        service["rows"].append(service["rows"][0])
    elif case == "oversized":
        service["rows"][0]["config"]["huge"] = "x" * 131073
    else:
        service["timeout"] = True
    assert read().kind == "unavailable"


def test_empty_devices_is_a_successful_structured_page(service):
    service["rows"] = []
    result = read()
    assert result.kind == "ok"
    assert json.loads(str(result))["total"] == 0


@pytest.mark.parametrize("args", [{"limit": 11}, {"page": 0}, {"limit": True}])
def test_invalid_page_does_not_contact_service(service, args):
    with pytest.raises(ValidationError):
        read(**args)
    assert not service["requests"]


def test_device_domain_exposes_only_verified_matching_actor_when_enabled(service, monkeypatch):
    from agent.products.blog.read_tools import device_domain

    domain = device_domain(config())
    assert set(domain) == {"devices"}
    assert set(domain["devices"]) == {"list_devices", "device_oled_display"}
    assert domain["devices"]["list_devices"] is device_reads.list_devices
    for source in (SOURCE_BODY, None):
        assert device_domain(config(source=source)) == {}
    for actor_id in (None, True, 8):
        cfg = config()
        cfg["configurable"]["user_id"] = actor_id
        assert device_domain(cfg) == {}
    assert device_domain({}) == {}
    cfg = config()
    cfg["configurable"]["principal"] = Principal(7, "zako", SOURCE_ASSERTION)
    assert device_domain(cfg) == {}
    monkeypatch.setattr(device_reads.settings, "iot_enabled", False)
    assert device_domain(config()) == {}
    assert not service["requests"]
