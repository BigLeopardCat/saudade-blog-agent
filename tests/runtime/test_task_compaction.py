"""Compact working exchanges without losing task ownership or replaying tools."""

from types import SimpleNamespace

import pytest
from _memory_support import ACTOR, Capture, config
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from agent.runtime import compaction, memory
from agent.runtime.execution_state import initial_state
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.session import ToolSession
from agent.runtime.turns import AgentCancelled
from config.settings import settings
from tools import base


@pytest.mark.parametrize("interrupt", [False, True])
def test_multiple_task_compactions_survive_reload_and_do_not_restore_covered_observations(
        monkeypatch, interrupt):
    monkeypatch.setattr(settings, "agent_context_tokens", 8000)
    monkeypatch.setattr(settings, "agent_summary_output_tokens", 512)
    monkeypatch.setattr(settings, "agent_model_calls", 20)
    monkeypatch.setattr("models.get_llm", lambda **_: FakeListChatModel(responses=["unused"]))
    summaries, reads = [], []

    def summarize(source, **kwargs):
        summaries.append(source)
        return f"已核实前 {len(reads)} 份材料；仅分析，不执行写入。"

    monkeypatch.setattr(compaction, "summarize", summarize)

    @tool
    def read_material() -> str:
        """Read one bounded synthetic source."""
        reads.append(len(reads))
        return base.ok(f"source-{len(reads)} " + "材料" * 1100)

    class Model(Capture):
        def stream(self, messages, **kwargs):
            self.prompts.append(list(messages))
            number = len(self.prompts)
            if number == 1:
                yield AIMessageChunk(content="", tool_calls=[{
                    "name": "load_reading_info_tools", "args": {}, "id": "load"}])
            elif number <= 9:
                yield AIMessageChunk(content="", tool_calls=[{
                    "name": "read_material", "args": {}, "id": f"read-{number - 1}"}])
            elif number == 10 and interrupt:
                raise AgentCancelled()
            else:
                yield AIMessageChunk(content="任务已完成。")

    model, saver = Model(), InMemorySaver()
    catalog = SimpleNamespace(initial_tools=lambda _: {},
                              domains=lambda *_: {"reading": {read_material.name: read_material}})

    def selected():
        return NativeRuntime(NativeConversationStore(saver), catalog=catalog,
                             model_factory=lambda **_: model)

    runtime = selected()
    source = memory.turn_messages([HumanMessage(content="旧问题")], [], "旧回答", ACTOR)
    runtime.store.update_state(config(), {"messages": source})
    inputs = {"messages": [HumanMessage(content="保留原任务：逐份分析，仅分析不写入。") ]}
    if interrupt:
        with pytest.raises(AgentCancelled):
            list(runtime.stream_events(inputs, config()))
        task = runtime.store.load(config()).run
        summary_count = len(summaries)
        runtime = selected()
        result = list(runtime.stream_events({"resume_task": task["id"]}, config()))[-1]
        assert len(summaries) == summary_count
    else:
        result = list(runtime.stream_events(inputs, config()))[-1]
    assert result.reply == "任务已完成。"
    assert len(reads) == 8 and len(summaries) >= 2
    assert all("保留原任务：逐份分析，仅分析不写入。" in str(prompt) for prompt in model.prompts)
    final = model.prompts[-1]
    calls = {call["id"] for m in final if isinstance(m, AIMessage) for call in m.tool_calls}
    answers = {m.tool_call_id for m in final if isinstance(m, ToolMessage)}
    assert calls == answers and "read-8" in answers
    saved = runtime.store.load(config()).messages
    assert len([m for m in saved if m.additional_kwargs.get("lc_source")]) == 1
    assert not any(isinstance(m, ToolMessage) and m.tool_call_id == "read-1" for m in saved)
    assert saved[-1].content == "任务已完成。"
    assert not runtime.store.load(config()).unfinished


def test_original_request_and_approval_feedback_remain_verbatim(monkeypatch):
    monkeypatch.setattr(settings, "agent_context_tokens", 8000)
    monkeypatch.setattr(settings, "agent_summary_output_tokens", 512)
    monkeypatch.setattr("models.get_llm", lambda **_: FakeListChatModel(responses=["unused"]))
    monkeypatch.setattr(compaction, "summarize", lambda *a, **k: "较早材料已汇总。")
    source = [HumanMessage(content="原始要求：仅分析，不通知。", id="root")]
    for number in range(7):
        source.extend([AIMessage(content="", id=f"ai-{number}", tool_calls=[{
            "name": "read", "args": {}, "id": f"read-{number}"}]), ToolMessage(
                content="材料" * 1000, tool_call_id=f"read-{number}", id=f"tool-{number}")])
    feedback = HumanMessage(content="补充：保留文章19，不要修改。", id="feedback")
    source.append(feedback)
    session = ToolSession(Capture(), config(), {}, {})
    state = {**initial_state(), "messages": source, "context_anchor": "root"}
    update = compaction.ConversationCompaction(session).before_model(state, None)
    kept = update["messages"][1:]
    assert source[0] in kept and feedback in kept
    assert not {"root", "feedback"}.intersection(update["context_update"]["covered_ids"])
