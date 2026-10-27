"""Capture real native requests without relying on provider cache availability."""

import json
from copy import deepcopy

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver

from agent.adminops_account import role_cn
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.prompt import build_prompt
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.results import TurnResult
from agent.runtime.session import ToolSession
from tools import base


class CaptureModel:
    max_tokens = 64

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def bind_tools(self, tools, **kwargs):
        self.kwargs = {"tools": [convert_to_openai_tool(item) for item in tools]}
        return self

    def stream(self, prompt, **kwargs):
        self.requests.append({"tools": deepcopy(self.kwargs["tools"]),
                              "messages": [message.model_dump() for message in prompt]})
        yield next(self.responses)


def call(name, identifier):
    return AIMessageChunk(content="", tool_calls=[
        {"name": name, "id": identifier, "args": {}}])


def test_domain_loading_extends_wire_schemas_and_repeated_loading_is_idempotent():
    calls = []

    @tool
    def fixture_read() -> object:
        """Read a public fixture."""
        calls.append("read")
        return base.ok("fixture contents")

    @tool
    def fixture_catalog() -> str:
        """List a public fixture catalog."""
        return "fixture catalog"

    model = CaptureModel(call("load_public_catalog_info_tools", "load-1"),
                         call("load_public_activity_info_tools", "load-2"),
                         call("load_public_catalog_info_tools", "load-again"),
                         call("fixture_read", "read"), AIMessageChunk(content="完成。"))
    session = ToolSession(model, config(), {}, {
        "public_activity": {"fixture_read": fixture_read},
        "public_catalog": {"fixture_catalog": fixture_catalog},
    })
    result = session.run(build_prompt([HumanMessage(content="查看内容")],
                                      config()["configurable"]["principal"]))
    assert result == TurnResult("完成。") and calls == ["read"]
    menus = [request["tools"] for request in model.requests]
    for previous, current in zip(menus, menus[1:]):
        assert current[:len(previous)] == previous
        names = [item["function"]["name"] for item in current]
        assert len(names) == len(set(names))
    repeated = [message for message in model.requests[-1]["messages"]
                if message.get("tool_call_id") == "load-again"]
    assert len(repeated) == 1 and repeated[0]["status"] == "success"
    assert "error" not in repeated[0]["content"]
    assert menus[2] == menus[3] == menus[4]
    assert session.budget.tool_calls == 4
    assert "delegate_to_site_agent" not in str(menus)


def context(title, summary):
    return HumanMessage(content="legacy context is not a prompt instruction", additional_kwargs={
        "memory_context": True, "page_context": {"title": title, "url": "/article/19"},
        "conversation_summary": summary, "conversation_id": 42,
        "summary_scope": {"user_id": 7, "role": "user", "conversation_id": 42}})


class Pending:
    def __init__(self, target):
        self.target = target

    def context(self):
        return {"target": self.target, "status": "awaiting_confirmation"}


@pytest.mark.parametrize("changed", ["page", "summary", "pending"])
def test_changing_current_data_preserves_older_prefix_and_latest_exchange(changed):
    principal = Principal(7, "user", SOURCE_ASSERTION)
    history = [HumanMessage(content="Earlier question " * 30),
               AIMessage(content="Earlier answer " * 30),
               HumanMessage(content="最近的问题"), AIMessage(content="最近的提议？")]
    current = HumanMessage(content="继续")
    before = build_prompt([*history, context("page A", "summary A"), current],
                          principal, pending=Pending("target A"))
    after = build_prompt([*history, context("page B" if changed == "page" else "page A",
                                           "summary B" if changed == "summary" else "summary A"),
                          current], principal,
                         pending=Pending("target B" if changed == "pending" else "target A"))
    assert before[:3] == after[:3] == [before[0], *history[:2]]
    assert before[-3:] == after[-3:] == [*history[-2:], current]
    assert before[-1] is after[-1] is current
    assert before != after
    for prompt in (before, after):
        assert all(value not in prompt[0].content for value in
                   ("page A", "page B", "summary A", "summary B", "target A", "target B"))
        assert all(isinstance(message, HumanMessage) for message in prompt[3:-3])


def test_new_turn_page_state_preserves_the_recent_exchange_in_the_actual_request():
    model = CaptureModel(AIMessageChunk(content="原有答复。"), AIMessageChunk(content="继续答复。"))
    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()),
                            model_factory=lambda **_: model)
    first = list(runtime.stream_events({"messages": [context("page A", ""),
                                                     HumanMessage(content="原有问题")]}, config()))
    second = list(runtime.stream_events({"messages": [context("page B", "最新摘要"),
                                                      HumanMessage(content="继续")]}, config()))
    assert first[-1] == TurnResult("原有答复。")
    assert second[-1] == TurnResult("继续答复。")
    before, after = model.requests
    assert before["tools"] == after["tools"]
    assert before["messages"][0] == after["messages"][0]
    assert [message["content"] for message in after["messages"][-3:]] == [
        "原有问题", "原有答复。", "继续"]
    assert "page B" in json.dumps(after["messages"][:-3], ensure_ascii=False)
    assert "page A" not in json.dumps(after["messages"], ensure_ascii=False)
    saved = runtime.store.load(config()).messages
    assert all("page B" not in str(message.content) for message in saved)


def test_private_history_still_disappears_when_authority_changes():
    from agent.runtime import memory

    principal = Principal(7, "superadmin", SOURCE_ASSERTION)
    saved = memory.turn_messages([HumanMessage(content="查看私密文章")], memory.observation(
        {"name": "list_admin_notes", "args": {}, "id": "private"},
        base.ok("PRIVATE_CANARY")), "PRIVATE_CANARY", principal)
    prompt = build_prompt([*saved, context("page", ""), HumanMessage(content="继续")],
                          Principal(7, "user", SOURCE_ASSERTION))
    assert "PRIVATE_CANARY" not in str(prompt)
    assert not any(isinstance(message, ToolMessage) for message in prompt)


def test_static_renderer_prefix_is_shared_without_sharing_caller_authority():
    from os.path import commonprefix

    from agent.authz import scopes_for
    from agent.stickers import STICKER_NAMES
    from models.deepseek import DeepSeekChat

    model = DeepSeekChat(api_key="synthetic", model="deepseek-v4.1-flash")
    systems = []
    for uid, role in [(7, "superadmin"), (8, "user"), (9, "zako")]:
        principal = Principal(uid, role, SOURCE_ASSERTION)
        prompt = build_prompt([HumanMessage(content="你好")], principal, own_enabled=role != "zako")
        wire = model._get_request_payload(prompt)
        system = wire["messages"][0]["content"]
        facts = json.loads(system.split("运行时能力（可信数据）：", 1)[1])
        assert facts["caller"]["role"] == role_cn(role)
        assert facts["permissions"] == sorted(scopes_for(role))
        assert facts["tools"]["loading"] == ("disabled" if role == "zako" else "on_demand")
        assert str(uid) in wire["messages"][-2]["content"]
        systems.append(system)
    shared = commonprefix(systems)
    assert all(f":{name}:" in shared for name in STICKER_NAMES)
    assert '"loading"' in shared
    assert "superadmin" not in shared
