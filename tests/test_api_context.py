"""HTTP facts must reach the model as transient data, preserving history and authority."""


import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from agent.api import host_context
from agent.api.context import build_messages
from agent.api.routes import chat as server
from agent.api.schemas import ChatRequest
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.prompt import build_prompt
from agent.runtime.budget import BudgetLimits
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.prompt_budget import fit_history

ACTOR = Principal(7, "user", SOURCE_ASSERTION)


def facts(prompt):
    return next(json.loads(message.content.split("：", 1)[1]) for message in prompt
                if isinstance(message.content, str) and message.content.startswith("本轮会话数据"))


def test_http_request_facts_reach_native_prompt_without_credentials(monkeypatch):
    monkeypatch.setattr(host_context, "format_local_now", lambda: "2026-10-25 12:00")
    canary = "CREDENTIAL_CANARY"
    request = ChatRequest(
        message="还剩多少额度？", chat_quota={"remaining": 3, "limit": 10, "used": 7},
        executions="10-25 11:59 已发送跳转指令", pending_action="旧卡片展示记录",
        agent_tasks=json.dumps([{
            "task_id": "t1", "state": "input_required", "goal": "处理文章",
            "cursor": 1, "steps": [{"label": "读取"}, {"label": "等待选择"}],
        }]),
        confirm_token=canary, execution_rows="PRIVATE_PAYLOAD_CANARY",
        recent_tools=["INTERNAL_TOOL_CANARY"],
    )
    prompt = build_prompt(server.build_messages(request), ACTOR, True)
    current = facts(prompt)
    assert current["current_time"] == "2026-10-25 12:00"
    assert current["chat_quota"]["remaining"] == 3
    assert "已发送跳转指令" in current["recent_execution_summary"]
    assert current["reported_pending_action"] == "旧卡片展示记录"
    assert "reported_open_tasks" not in current
    assert "agent_tasks" not in ChatRequest.model_fields
    assert "chat_quota" not in prompt[0].content
    assert all(value not in str(prompt) for value in
               ("CREDENTIAL_CANARY", "PRIVATE_PAYLOAD_CANARY", "INTERNAL_TOOL_CANARY"))
    assert prompt[-1].content == "[当前问题]: 还剩多少额度？"


def test_unknown_quota_is_distinct_from_known_zero_quota():
    unknown = build_prompt(build_messages(ChatRequest(message="额度"), holds_context=True), ACTOR)
    zero = build_prompt(build_messages(ChatRequest(message="额度", chat_quota={}),
                                      holds_context=True), ACTOR)
    assert "chat_quota" not in facts(unknown)
    assert facts(zero)["chat_quota"]["remaining"] == 0


@pytest.mark.parametrize("raw", ["not json", "{}", "[null]", '[{"task_id":"t","state":[]}]'])
def test_malformed_host_task_data_cannot_break_a_turn(raw):
    prompt = build_prompt(build_messages(ChatRequest(message="继续", agent_tasks=raw),
                                        holds_context=True), ACTOR)
    assert "reported_open_tasks" not in facts(prompt)


def test_retired_host_plans_do_not_enter_the_model_or_wire_schema():
    raw = json.dumps([{"task_id": "old-plan", "state": "running", "goal": "STALE_PLAN_CANARY",
                       "steps": [{"tool": "delete_article", "args": "ARGS_CANARY"}]}])
    request = ChatRequest(message="继续", agent_tasks=raw)
    prompt = build_prompt(build_messages(request, holds_context=True), ACTOR)
    assert "agent_tasks" not in request.model_dump()
    assert "reported_open_tasks" not in facts(prompt)
    assert "STALE_PLAN_CANARY" not in str(prompt) and "ARGS_CANARY" not in str(prompt)


def test_current_facts_preserve_latest_exchange_and_do_not_survive_checkpoint_reload(monkeypatch):
    requests = []

    class Model:
        max_tokens = 32

        def stream(self, prompt, **kwargs):
            requests.append(deepcopy(prompt))
            yield AIMessageChunk(content="收到。")

    store = NativeConversationStore(InMemorySaver())
    catalog = SimpleNamespace(initial_tools=lambda _: {}, domains=lambda *_: {})
    config = {"configurable": {"thread_id": "conv_42", "user_id": 7,
                                "conversation_id": 42, "principal": ACTOR}}
    for marker in ("FIRST_TIME_CANARY", "SECOND_TIME_CANARY"):
        monkeypatch.setattr(host_context, "format_local_now", lambda: marker)
        runtime = NativeRuntime(store, catalog=catalog, model_factory=lambda **_: Model())
        incoming = build_messages(ChatRequest(message="继续", chat_quota={"remaining": 3}),
                                  holds_context=True)
        list(runtime.stream_events({"messages": incoming}, config))
    assert "request_context" not in str(store.load(config).messages)
    assert "FIRST_TIME_CANARY" not in str(requests[1])
    assert facts(requests[1])["current_time"] == "SECOND_TIME_CANARY"
    assert requests[0][0] == requests[1][0]
    assert [message.content for message in requests[1][-3:]] == [
        "[当前问题]: 继续", "收到。", "[当前问题]: 继续"]


def test_context_and_summary_do_not_split_an_offer_from_a_short_answer():
    request = ChatRequest(message="嗯", conversation_id=42, summary="之前用户累了想睡觉。",
        summary_scope={"user_id":7, "role":"user", "conversation_id":42})
    previous = [HumanMessage(content="把第一条待办完成"),
                AIMessage(content="第一条完成了。第二条也要完成吗？")]
    prompt = build_prompt([*previous, *build_messages(request)], ACTOR)
    prompt = fit_history(prompt, ACTOR, [], BudgetLimits(), 128)
    assert [message.content for message in prompt[-3:]] == [
        "把第一条待办完成", "第一条完成了。第二条也要完成吗？", "[当前问题]: 嗯"]
    assert any("之前用户累了" in str(message.content) for message in prompt[:-3])
    assert "current_time" in facts(prompt)


@pytest.mark.parametrize("values", [
    {"message": "x" * 4001}, {"message": "x", "image": ["image"] * 7},
    {"message": "x", "image": "x" * 1_600_001},
    {"message": "x", "execution_rows": "x" * 8001},
])
def test_extracted_wire_schema_still_rejects_oversized_payloads(values):
    with pytest.raises(ValidationError):
        ChatRequest(**values)
