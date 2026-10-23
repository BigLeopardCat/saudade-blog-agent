"""Conversation continuity must survive persistence without reusing authority."""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

import server
from agent.agent import create_agent
from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from agent.runtime import memory
from agent.runtime.hybrid import HybridArm, _input
from tools import base


def actor(role="superadmin", uid=7, source=SOURCE_ASSERTION):
    return Principal(uid, role, source)


def test_summary_reaches_native_input_as_data_not_runtime_authority():
    request = server.ChatRequest(message="继续", summary="用户希望中文答复；助手曾误称不能管理。")
    prompt = _input(server._build_messages(request), actor())
    trusted, _ = json.JSONDecoder().raw_decode(
        prompt[0].content.split("运行时能力（可信数据）：", 1)[1])
    assert trusted["identity"]["role"] == "superadmin"
    assert "曾误称不能管理" not in prompt[0].content
    assert any("用户希望中文答复" in str(m.content) for m in prompt[1:-1])
    assert prompt[-1].content == "[当前问题]: 继续"
    empty = server._build_messages(server.ChatRequest(message="继续"))
    assert memory.summary_message(empty) is None


def test_short_prior_preference_survives_more_than_two_exchanges():
    messages = [HumanMessage(content="请只用简体中文"), AIMessage(content="好。")]
    for number in range(4):
        messages.extend([HumanMessage(content=f"话题{number}"), AIMessage(content="答复。")])
    prompt = _input([*messages, HumanMessage(content="继续")], actor())
    assert any(m.content == "请只用简体中文" for m in prompt)


@pytest.mark.parametrize("principal", [actor("user"), actor(uid=8), actor(source=SOURCE_BODY)])
def test_private_observation_and_derived_reply_do_not_survive_role_or_actor_change(principal):
    call = {"name": "list_admin_notes", "args": {}, "id": "catalog"}
    saved = memory.turn_messages(
        [HumanMessage(content="看看后台")],
        memory.observation(call, base.ok('{"id":19,"title":"PRIVATE_CANARY"}')),
        "PRIVATE_CANARY 是私密文章。", actor(),
    )
    assert memory.history(saved, principal) == []
    assert any("PRIVATE_CANARY" in str(m.content) for m in memory.history(saved, actor()))


def test_observations_are_bounded_pairs_and_failures_are_not_remembered_as_facts():
    call = {"name": "search_notes", "args": {"keyword": "架构"}, "id": "search"}
    pair = memory.observation(call, base.ok("x" * 20000))
    assert len(pair[1].content) < memory.OBSERVATION_CHARS + 100
    assert pair[0].tool_calls[0]["id"] == pair[1].tool_call_id
    assert pair[1].name == "search_notes"
    assert memory.observation(call, base.unavailable("读不到")) == []
    serializer = JsonPlusSerializer()
    restored = serializer.loads_typed(serializer.dumps_typed(pair))
    assert restored[0].tool_calls[0]["args"] == {"keyword": "架构"}
    assert restored[1].tool_call_id == "search"


def test_real_native_loop_persists_source_observations_across_followup_and_reload(monkeypatch):
    class Search:
        def invoke(self, args, config=None):
            return base.ok('{"items":[{"id":19,"title":"架构文档"}]}')

    class Model:
        def __init__(self):
            self.responses = iter([
                AIMessage(content="", tool_calls=[
                    {"name": "search_notes", "args": {"keyword": "架构"}, "id": "source"}]),
                AIMessage(content="找到了。"), AIMessage(content="好的。"),
                AIMessage(content="文章编号是19。"),
            ])
            self.prompts = []

        def bind_tools(self, tools, **kwargs):
            return self

        def invoke(self, prompt):
            self.prompts.append(list(prompt))
            return next(self.responses)

    model = Model()
    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: model)
    saver = InMemorySaver()
    graph = create_agent(checkpointer=saver)
    monkeypatch.setattr(graph, "stream", lambda *a, **k: pytest.fail("legacy planner invoked"))
    config = {"configurable": {"thread_id": "memory-followup", "conversation_id": 8,
                               "user_id": 7, "principal": actor()}}
    arm = HybridArm(graph, {"search_notes": Search()}, {}, {})
    for message in ("找一下架构文档", "好的"):
        list(arm.stream({"messages": [HumanMessage(content=message)]}, config))
    reloaded = HybridArm(graph, {"search_notes": Search()}, {}, {})
    list(reloaded.stream({"messages": [HumanMessage(content="刚才那篇编号是多少？")]}, config))
    tools = [m for m in model.prompts[-1] if isinstance(m, ToolMessage)]
    assert len(tools) == 1 and tools[0].name == "search_notes"
    assert json.loads(tools[0].content)["items"][0]["id"] == 19
    assert tools[0].tool_call_id == "source"
    assert any(m.tool_calls[0]["id"] == "source" for m in model.prompts[-1]
               if isinstance(m, AIMessage) and m.tool_calls)
    assert not graph.get_state(config).next
