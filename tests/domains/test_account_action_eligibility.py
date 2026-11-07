"""Read eligibility facts without fabricating write parameters or authorization."""

import json

import httpx
import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from agent.domains.account_actions import eligibility, preparation
from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from tools import base


def admin_config(**values):
    return config(
        **{"principal": Principal(uid=7, role="admin", source=SOURCE_ASSERTION), **values}
    )


@pytest.fixture
def backend(monkeypatch):
    state = {
        "requests": [],
        "allowed": True,
        "reasonCode": None,
        "message": "当前操作者可以准备此账号操作",
    }

    def handle(request):
        state["requests"].append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/protected/account-actions/eligibility"
        assert request.headers["Authorization"].startswith("Bearer ")
        assert set(request.url.params) <= {"accountId", "action", "role"}
        body = {
            "code": 200,
            "data": {
                "accountId": int(request.url.params["accountId"]),
                "action": request.url.params["action"],
                "role": request.url.params.get("role"),
                **{key: state[key] for key in ("allowed", "reasonCode", "message")},
                **state.get("override", {}),
            },
        }
        return httpx.Response(state.get("status", 200), json=body)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


def read(**values):
    return eligibility.get_account_action_eligibility.invoke(
        {"account_id": 11, "action": "mute", **values}, config=admin_config()
    )


def test_mute_eligibility_needs_no_duration_or_preparation(backend):
    result = read()
    assert result.kind == "ok" and result.data["allowed"] is True
    assert result.data["reasonCode"] is None
    assert json.loads(result.text) == result.data
    assert dict(backend["requests"][0].url.params) == {"accountId": "11", "action": "mute"}
    missing = preparation.prepare_account_mute.invoke({"account_id": 11}, config=admin_config())
    assert missing.kind == "invalid_args" and len(backend["requests"]) == 1
    assert not getattr(missing, "execution", {}).get("pending_confirm")


def test_policy_denial_remains_a_successful_read_not_an_absent_account(backend):
    backend.update(allowed=False, reasonCode="target_superadmin", message="不能禁言超级管理员账号")
    result = read(account_id=1)
    assert result.kind == "ok" and result.data == {
        "accountId": 1,
        "action": "mute",
        "role": None,
        "allowed": False,
        "reasonCode": "target_superadmin",
        "message": "不能禁言超级管理员账号",
    }
    assert len(backend["requests"]) == 1


def test_observation_records_only_the_verified_policy_facts_for_this_call(backend, monkeypatch):
    from agent.runtime.tool_evidence import scope

    records = []
    monkeypatch.setattr(
        "agent.runtime.tool_evidence.record", lambda *args, **values: records.append((args, values))
    )
    with scope({"id": "policy-check"}, 2):
        result = read()
    assert records == [
        (
            ("native", "preparation_observation"),
            {
                "call_id": "policy-check",
                "round": 2,
                "source": "account_action_eligibility",
                "facts": result.data,
                "complete": True,
            },
        )
    ]


def test_proposed_role_is_sent_and_echoed_without_agent_owned_role_policy(backend):
    result = read(action="set_role", role="zako")
    assert result.data["role"] == "zako" and result.data["allowed"] is True
    backend.update(allowed=False, reasonCode="role_not_assignable", message="不能分配该身份")
    assert read(action="set_role", role="superadmin").data["reasonCode"] == "role_not_assignable"
    assert backend["requests"][-1].url.params["role"] == "superadmin"


@pytest.mark.parametrize("args", [{"action": "set_role"}, {"role": "user"}])
def test_role_only_belongs_to_a_role_change(backend, args):
    assert read(**args).kind == "invalid_args"
    assert not backend["requests"]


@pytest.mark.parametrize(
    "args",
    [
        {"account_id": True},
        {"account_id": 0},
        {"account_id": 2147483648},
        {"action": "delete"},
        {"action": "set_role", "role": "invented"},
    ],
)
def test_invalid_schema_never_reaches_backend(backend, args):
    with pytest.raises(ValidationError):
        read(**args)
    assert not backend["requests"]


@pytest.mark.parametrize(
    "override",
    [
        {"accountId": 12},
        {"action": "freeze"},
        {"role": "user"},
        {"allowed": 1},
        {"allowed": "true"},
        {"reasonCode": "self_target"},
        {"allowed": False},
        {"allowed": False, "reasonCode": "invented"},
        {"message": ""},
        {"message": "x" * 501},
        {"password": "sensitive"},
    ],
)
def test_unverified_or_mismatched_result_does_not_become_a_policy_fact(backend, override):
    backend["override"] = override
    result = read()
    assert result.kind == "unavailable" and result.data is None


@pytest.mark.parametrize(
    "status,kind",
    [
        (400, "invalid_args"),
        (401, "unavailable"),
        (403, "unavailable"),
        (404, "not_found"),
        (500, "unavailable"),
    ],
)
def test_read_failures_are_never_a_policy_denial_or_permission(backend, status, kind):
    backend["status"] = status
    result = read()
    assert result.kind == kind and result.data is None


@pytest.mark.parametrize(
    "cfg",
    [
        admin_config(principal=Principal(7, "user", SOURCE_ASSERTION)),
        admin_config(principal=Principal(7, "admin", SOURCE_BODY)),
        admin_config(user_id=8),
        admin_config(user_id=True),
    ],
)
def test_direct_tool_refuses_unverified_or_mismatched_actor(backend, cfg):
    result = eligibility.get_account_action_eligibility.invoke(
        {"account_id": 11, "action": "mute"}, config=cfg
    )
    assert result.kind == "unavailable" and not backend["requests"]


@pytest.mark.parametrize("allowed", [True, False])
def test_framework_loads_the_tool_and_preserves_policy_facts_without_a_card(backend, allowed):
    if not allowed:
        backend.update(
            allowed=False, reasonCode="target_superadmin", message="不能禁言超级管理员账号"
        )
    target = 11 if allowed else 1
    final = (
        "允许准备禁言，请指定禁言多久。"
        if allowed
        else "不能禁言超级管理员账号，本次没有准备操作。"
    )
    responses = iter(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "load_admin_account_actions_info_tools", "args": {}, "id": "load"}
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_account_action_eligibility",
                        "args": {"account_id": target, "action": "mute"},
                        "id": "eligibility",
                    }
                ],
            ),
            AIMessage(content=final),
        ]
    )
    prompts = []

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt):
            prompts.append(list(prompt))
            return next(responses)

    runtime = AgentRuntime(
        CheckpointConversationStore(None),
        application=BlogApplication(),
        model_factory=lambda **_: Model(),
    )
    cfg = admin_config()
    initial, domains = runtime.application.tools(cfg, runtime.store.load(cfg), None, None, True)
    assert "get_account_action_eligibility" not in initial
    assert "get_account_action_eligibility" in domains["admin_account_actions"]
    events = list(
        runtime.stream_events({"messages": [HumanMessage(content=f"禁言账号{target}。")]}, cfg)
    )
    assert events[-1].reply == final and events[-1].execution is None
    message = next(
        item
        for item in prompts[-1]
        if isinstance(item, ToolMessage) and item.name == "get_account_action_eligibility"
    )
    assert message.artifact["kind"] == "ok"
    assert message.artifact["data"]["allowed"] is allowed
    assert json.loads(message.content)["accountId"] == target
    assert len(backend["requests"]) == 1
