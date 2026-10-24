"""Offline contract tests for native answer streaming and tool-call replay."""

import threading
from queue import Queue

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.tools import tool

from agent.runtime.budget import BudgetLimits, RequestBudget, budget_scope
from agent.runtime.session import ToolSession
from agent.runtime.turns import AgentCancelled


class _StreamingModel:
    max_tokens = 64

    def __init__(self, rounds, *, gate=None):
        self.rounds = rounds
        self.gate = gate
        self.calls = []
        self.prompts = []

    def bind_tools(self, tools, **kwargs):
        self.tools = tools
        return self

    def stream(self, prompt, **kwargs):
        index = len(self.calls)
        self.calls.append(index)
        self.prompts.append(list(prompt))
        for position, chunk in enumerate(self.rounds[index]):
            if self.gate and index == 0 and position == 2:
                self.gate.set()
                assert self.release.wait(2)
            yield chunk


@tool
def fixture_lookup(query: str) -> str:
    """Read a fixture resource."""
    FIXTURE_CALLS.append(query)
    return "fixture result"


FIXTURE_CALLS = []


def test_text_is_visible_before_stream_completion_and_tool_args_wait_for_full_chunks():
    entered, release = threading.Event(), threading.Event()
    model = _StreamingModel([
        [
            AIMessageChunk(content="我先查一下。",
                           additional_kwargs={"reasoning_content": "r"}),
            AIMessageChunk(content="", tool_call_chunks=[
                {"name": "fixture_lookup", "args": '{"query":', "id": "call-1", "index": 0}
            ]),
            AIMessageChunk(content="", additional_kwargs={"reasoning_content": "s"},
                           tool_call_chunks=[
                               {"name": None, "args": '"weather"}', "id": "call-1", "index": 0}
                           ]),
        ],
        [AIMessageChunk(content="查到了。", usage_metadata={
            "input_tokens": 5, "output_tokens": 3, "total_tokens": 8})],
    ], gate=entered)
    model.release = release
    FIXTURE_CALLS.clear()
    session = ToolSession(model, {}, {"fixture_lookup": fixture_lookup}, {})
    chunks, outcome, yielded = [], [], Queue()

    def consume():
        runner = session.stream([HumanMessage(content="查天气")])
        try:
            while True:
                yielded.put(next(runner))
        except StopIteration as stop:
            outcome.append(stop.value)

    worker = threading.Thread(target=consume)
    worker.start()
    first = yielded.get(timeout=2)
    assert first.content == "我先查一下。"
    assert entered.wait(2)
    assert FIXTURE_CALLS == []
    release.set()
    worker.join(2)
    while not yielded.empty():
        chunks.append(yielded.get())

    assert outcome and outcome[0].reply == "我先查一下。查到了。"
    assert FIXTURE_CALLS == ["weather"]
    replay = next(message for message in model.prompts[1] if isinstance(message, AIMessage))
    assert replay.tool_calls[0]["args"] == {"query": "weather"}
    assert replay.additional_kwargs["reasoning_content"] == "rs"
    assert sum((chunk.usage_metadata or {}).get("output_tokens", 0)
               for chunk in chunks) == 3


def test_stream_cancellation_closes_provider_iterator_before_returning():
    stop = threading.Event()
    closed = threading.Event()

    class IteratorModel(_StreamingModel):
        def stream(self, prompt, **kwargs):
            try:
                yield AIMessageChunk(content="首段")
                yield AIMessageChunk(content="后段")
            finally:
                closed.set()

    session = ToolSession(IteratorModel([[]]), {"configurable": {"stop_event": stop}}, {}, {})
    runner = session.stream([HumanMessage(content="停下")])
    assert next(runner).content == "首段"
    stop.set()
    with pytest.raises(AgentCancelled):
        next(runner)
    assert closed.wait(1)


def test_stream_usage_settles_one_shared_budget_ticket():
    model = _StreamingModel([[
        AIMessageChunk(content="完成", usage_metadata={
            "input_tokens": 4, "output_tokens": 2, "total_tokens": 6}),
    ]])
    budget = RequestBudget(BudgetLimits(model_calls=1))
    with budget_scope(budget):
        result = ToolSession(model, {}, {}, {}).run([HumanMessage(content="预算")])
    assert result.reply == "完成"
    assert budget.model_calls == 1 and budget.tokens == 6


def test_stream_error_closes_iterator_and_does_not_emit_a_fake_answer():
    closed = threading.Event()

    class ErrorModel(_StreamingModel):
        def stream(self, prompt, **kwargs):
            try:
                yield AIMessageChunk(content="首段")
                raise RuntimeError("private transport detail")
            finally:
                closed.set()

    session = ToolSession(ErrorModel([[]]), {}, {}, {})
    runner = session.stream([HumanMessage(content="错误")])
    assert next(runner).content == "首段"
    with pytest.raises(RuntimeError, match="private transport detail"):
        next(runner)
    assert closed.wait(1)


@pytest.mark.parametrize("arguments,metadata", [
    ('{"query":"weather"', {}),
    ('{"query":', {}),
    ('{"query":"weather"}', {"finish_reason": "length"}),
])
def test_repaired_or_truncated_tool_arguments_are_never_executed(arguments, metadata):
    FIXTURE_CALLS.clear()
    chunk = AIMessageChunk(content="", response_metadata=metadata, tool_call_chunks=[
        {"name": "fixture_lookup", "args": arguments, "id": "partial", "index": 0}])
    model = _StreamingModel([[chunk]])
    result = ToolSession(model, {}, {"fixture_lookup": fixture_lookup}, {}).run([
        HumanMessage(content="read")])
    assert FIXTURE_CALLS == []
    assert "未执行" in result.reply
    assert len(model.calls) == 1


def test_result_budget_stop_is_visible_after_a_streamed_preface():
    FIXTURE_CALLS.clear()
    model = _StreamingModel([[
        AIMessageChunk(content="我先查一下。"),
        AIMessageChunk(content="", tool_calls=[
            {"name": "fixture_lookup", "args": {"query": "weather"}, "id": "read"}]),
    ]])
    session = ToolSession(model, {}, {"fixture_lookup": fixture_lookup}, {})
    session.result_chars = 16000
    stream = session.stream([HumanMessage(content="read")])
    shown = []
    while True:
        try:
            shown.append(next(stream).content)
        except StopIteration as stop:
            result = stop.value
            break
    assert FIXTURE_CALLS == []
    assert "上限" in "".join(shown)
    assert result.reply == "".join(shown)
