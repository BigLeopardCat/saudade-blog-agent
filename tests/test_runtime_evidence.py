"""Diagnostic evidence comes from actual binding and completed runtime phases."""

import json

from _own_support import config
from langchain_core.messages import AIMessageChunk, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver

from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from eval.online_judge import runtime_context
from utils import trace


def test_actual_bound_schemas_and_caller_are_recorded_without_modifying_model_input(tmp_path):
    prompts, menus = [], []

    class Model:
        max_tokens = 64

        def bind_tools(self, tools, **kwargs):
            self.kwargs = {"tools": [convert_to_openai_tool(tool) for tool in tools]}
            menus.append(self.kwargs["tools"])
            return self

        def stream(self, prompt, **kwargs):
            prompts.append(prompt)
            yield AIMessageChunk(content="你好。")

    recorder = trace.start_trace("fixture-context", 0, "conv_42", dir=str(tmp_path))
    try:
        runtime = NativeRuntime(NativeConversationStore(InMemorySaver()),
                                model_factory=lambda **_: Model())
        list(runtime.stream_events({"messages": [HumanMessage(content="你好")]},
                                   config(principal=Principal(7, "user", SOURCE_ASSERTION))))
        context = runtime_context(recorder.events)
        assert context["caller"]["role"] == "user" and not context["missing"]
        assert context["turn_phase"] == "responded" and context["receipt_count"] == 0
        actual = [row["schema"] for row in recorder.events if row["event"] == "tool_contract"]
        assert actual == menus[-1]
        assert not any("admin_account_actions" in item["function"]["name"] for item in actual)
        assert "tool_contract" not in str(prompts[0])
        trusted = json.loads(prompts[0][0].content.split("运行时能力（可信数据）：")[1])
        assert trusted["caller"]["role"] == "user" and "identity" not in trusted
    finally:
        trace.finish_trace("fixture-context", "complete", 0)


def test_old_trace_context_is_unknown_instead_of_reconstructed_from_current_code():
    context = runtime_context([])
    assert context["caller"] is None and context["turn_phase"] == "not_recorded"
    assert context["available_tools"] == [] and context["missing"]
