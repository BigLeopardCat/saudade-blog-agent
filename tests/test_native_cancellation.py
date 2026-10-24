"""Native cancellation checkpoints prevent late tool writes."""

import threading

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver

from agent.principal import Principal, SOURCE_ASSERTION
from agent.runtime.capabilities import ToolCatalog
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.turns import AgentCancelled
from tools.base import ok


def config(stop_event):
    return {"configurable": {"thread_id": "conv_7", "conversation_id": 7,
                             "user_id": 7,
                             "principal": Principal(7, "user", SOURCE_ASSERTION),
                             "stop_event": stop_event}}


class Model:
    def __init__(self, responses):
        self.responses = iter(responses)

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, prompt):
        return next(self.responses)


class Tool:
    def __init__(self, name, on_call=None):
        self.name = name
        self.on_call = on_call
        self.calls = []
        self.tool = StructuredTool.from_function(self._invoke, name=name,
                                                 description="test tool")

    def _invoke(self, **args):
        self.calls.append(args)
        if self.on_call:
            self.on_call()
        return ToolMessage(content="已读取", name=self.name, tool_call_id="call")


def runtime(model, tools):
    catalog = ToolCatalog(read_tools={tool.name: tool.tool for tool in tools})
    return NativeRuntime(NativeConversationStore(InMemorySaver()), catalog=catalog,
                         model_factory=lambda **_: model)


def test_cancel_at_entry_never_calls_model():
    stop = threading.Event()
    stop.set()
    model = Model([AIMessage(content="不应调用")])
    selected = runtime(model, [])
    with pytest.raises(AgentCancelled):
        list(selected.stream_events({"messages": [HumanMessage(content="停止")]}, config(stop)))


def test_cancel_after_first_tool_prevents_late_tool_call():
    stop = threading.Event()
    first = Tool("first", on_call=stop.set)
    second = Tool("second")
    model = Model([
        AIMessage(content="", tool_calls=[{"name": "first", "args": {}, "id": "first"}]),
        AIMessage(content="", tool_calls=[{"name": "second", "args": {}, "id": "second"}]),
    ])
    selected = runtime(model, [first, second])
    with pytest.raises(AgentCancelled):
        list(selected.stream_events({"messages": [HumanMessage(content="读取")]}, config(stop)))
    assert len(first.calls) == 1
    assert second.calls == []


def test_cancel_during_model_call_is_seen_before_persistence():
    stop = threading.Event()

    class BlockingModel(Model):
        def invoke(self, prompt):
            stop.set()
            return AIMessage(content="晚到的答复")

    selected = runtime(BlockingModel([]), [])
    with pytest.raises(AgentCancelled):
        list(selected.stream_events({"messages": [HumanMessage(content="停止")]}, config(stop)))
    assert selected.get_state(config(stop)).values == {}
