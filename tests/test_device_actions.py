"""Optional OLED commands use verified identity and report delivery truthfully."""

import copy
import json
import secrets

import httpx
import jwt
import pytest
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    ToolMessage,
    message_to_dict,
    messages_from_dict,
)
from langchain_core.tools import ToolException

from agent.domains import device_actions, device_reads
from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from agent.products.blog.read_tools import device_domain
from agent.products.blog.tool_policy import BlogToolMiddleware
from agent.runtime import memory
from agent.runtime.session import ToolSession
from tools import device


def config(**changes):
    return {
        "configurable": {
            "user_id": 7,
            "principal": Principal(7, "user", SOURCE_ASSERTION),
            **changes,
        }
    }


@pytest.fixture
def service(monkeypatch):
    secret = secrets.token_urlsafe(32)
    monkeypatch.setattr(device, "_jwt_secret", lambda: secret)
    monkeypatch.setattr(device_reads.settings, "iot_enabled", True)
    state = {
        "devices": [{"id": "demo-a", "name": "demo", "online": True}],
        "requests": [],
        "reads": [],
        "status": 200,
        "acked": False,
    }
    monkeypatch.setattr(
        device_reads,
        "_read",
        lambda uid: (state["reads"].append(uid) or copy.deepcopy(state["devices"])),
    )
    original_client = httpx.Client

    def handle(req):
        state["requests"].append(req)
        claims = jwt.decode(
            req.headers["Authorization"].removeprefix("Bearer "),
            secret,
            algorithms=["HS256"],
            options={"verify_sub": False},
        )
        assert claims["sub"] == 7 and claims["role"] == "user"
        parts = req.url.path.split("/")
        if req.method == "PUT":
            state["rid"] = req.headers["X-Request-Id"]
            assert json.loads(req.content)["type"] == "display"
            if state.get("invalid_json"):
                return httpx.Response(200, text="not json")
            return httpx.Response(
                state["status"],
                json={
                    "published": True,
                    "device_id": parts[3],
                    "req_id": "wrong" if state.get("wrong_receipt") else state["rid"],
                },
            )
        return httpx.Response(
            200, json={"device_id": parts[3], "req_id": state["rid"], "acked": state["acked"]}
        )

    def client(**kwargs):
        assert kwargs == {"trust_env": False, "timeout": 10}
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(device_actions.httpx, "Client", client)
    return state


def display(args, cfg=None):
    return device_actions.device_oled_display.invoke(
        {"name": device_actions.NAME, "args": args, "id": "display", "type": "tool_call"},
        config=cfg or config(),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"principal": Principal(7, "user", SOURCE_BODY)},
        {"principal": Principal(7, "zako", SOURCE_ASSERTION)},
        {"user_id": 8},
        {"user_id": True},
        {"principal": None},
    ],
)
def test_unauthorized_direct_and_loaded_calls_do_no_io(service, overrides):
    cfg = config(**overrides)
    assert not device_actions.tools(cfg)
    result = display({"text": "hello"}, cfg)
    assert result.artifact["kind"] == "unavailable" and not service["requests"]


def test_disabled_demo_exposes_no_tools_and_makes_no_requests(service, monkeypatch):
    monkeypatch.setattr(device_reads.settings, "iot_enabled", False)
    assert not device_domain(config())
    assert display({"text": "x"}).artifact["kind"] == "unavailable"
    assert not service["requests"]


@pytest.mark.parametrize("acked,status", [(False, "queued"), (True, "acknowledged")])
def test_exact_delivery_and_acknowledgement_are_distinct(service, acked, status):
    service["acked"] = acked
    result = display({"text": "你好"})
    receipt = result.artifact["receipts"][0]
    assert receipt["device_status"] == status and receipt["args"]["device_id"] == "demo-a"
    assert service["reads"] == [7] and [r.method for r in service["requests"]] == ["PUT", "GET"]
    restored = messages_from_dict([message_to_dict(result)])[0]
    assert restored.artifact == result.artifact
    assert "未确认" in result.content if not acked else "已确认" in result.content


def test_two_devices_with_same_text_both_receive_absolute_commands(service):
    for name in ["demo-a", "demo-b"]:
        display({"text": "same", "device_id": name})
    requests = [r for r in service["requests"] if r.method == "PUT"]
    assert len(requests) == 2 and requests[0].url != requests[1].url
    assert requests[0].headers["X-Request-Id"] != requests[1].headers["X-Request-Id"]
    assert not service["reads"]


def test_ambiguous_default_needs_explicit_device_before_sending(service):
    service["devices"].append({"id": "demo-b", "name": "other", "online": True})
    with pytest.raises(ToolException):
        display({"text": "x"})
    assert not service["requests"]


@pytest.mark.parametrize("key", ["invalid_json", "wrong_receipt"])
def test_unverifiable_response_is_unknown_and_preserves_the_operation(service, key):
    service[key] = True
    result = display({"text": "hello", "device_id": "demo-a"})
    assert result.artifact["receipts"][0]["device_status"] == "unknown"
    assert "未知" in result.content and len(service["requests"]) == 1


@pytest.mark.parametrize("status", [401, 404, 409, 500])
def test_service_refusal_is_not_a_success_receipt(service, status):
    service["status"] = status
    result = display({"text": "hello", "device_id": "demo-a"})
    assert result.artifact["kind"] == "unavailable" and not result.artifact.get("receipts")
    assert len(service["requests"]) == 1


def test_native_loop_keeps_device_receipt_and_blocks_whole_turn_legacy_replay(service):
    class Model:
        responses = iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": device_actions.NAME,
                            "args": {"device_id": "demo-a", "text": "hello"},
                            "id": "write",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "delegate_to_site_agent", "args": {}, "id": "delegate"}],
                ),
                AIMessage(content="设备回执已保留。"),
            ]
        )

        def bind_tools(self, *a, **kw):
            return self

        def invoke(self, prompt):
            return next(self.responses)

    session = ToolSession(Model(), config(), device_actions.tools(config()), {},
                          middleware=(BlogToolMiddleware,))
    result = session.run([HumanMessage(content="显示hello，继续处理")])
    assert len(result.execution["receipts"]) == 1
    assert len([r for r in service["requests"] if r.method == "PUT"]) == 1
    assert session.observations
    unverified = Principal(7, "user", SOURCE_BODY)
    assert not memory._allowed(
        [
            HumanMessage(content="x"),
            ToolMessage(content="x", tool_call_id="write", name=device_actions.NAME),
        ],
        unverified,
    )
