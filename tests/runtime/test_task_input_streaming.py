"""SDK deltas and framework state updates have different display semantics."""

import json

import httpx
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime.control import AgentCancelled
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.task_inputs import binding
from models.llm import get_llm


def test_resumed_sdk_stream_does_not_echo_injected_user_or_repeat_final_message():
    saver = InMemorySaver()
    cfg = {"configurable": {"thread_id": "conv_42", "conversation_id": 42,
        "user_id": 7, "principal": Principal(7, "user", SOURCE_ASSERTION),
        "task_inputs": True}}
    guide = "只回复：恢复完成。不要调用工具。"
    selected = None

    class CancelledModel:
        max_tokens = 64

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages, **kwargs):
            selected.inputs.submit("same-task", scope, "one", guide)
            raise AgentCancelled()

    scope = binding(cfg["configurable"]["principal"], 42)
    selected = NativeRuntime(NativeConversationStore(saver),
                             model_factory=lambda **_: CancelledModel())
    with pytest.raises(AgentCancelled):
        list(selected.stream_events({"task_id": "same-task", "messages": [
            HumanMessage(content="详细介绍功能")]}, cfg))
    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["messages"][-1] == {"role": "user", "content": guide}
        base = {"id": "resumed", "object": "chat.completion.chunk", "created": 0,
                "model": "fixture"}
        chunks = [{**base, "choices": [{"index": 0, "delta": {"content": text},
                                       "finish_reason": None}]}
                  for text in ["恢复", "完成。"]]
        chunks.append({**base, "choices": [{"index": 0, "delta": {},
                                            "finish_reason": "stop"}]})
        wire = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              content=(wire + "data: [DONE]\n\n").encode())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        model = get_llm(base_url="https://fixture.invalid/v1", api_key="fixture-only",
                        http_client=client, streaming=True, max_tokens=64)
        restored = NativeRuntime(NativeConversationStore(saver), model_factory=lambda **_: model)
        events = list(restored.stream_events({"resume_task": "same-task"}, cfg))
    assert len(requests) == 1
    assert events[-1].reply == "恢复完成。"
    assert "".join(event.content for event in events
                   if isinstance(event, AIMessageChunk)) == "恢复完成。"
    saved = restored.store.load(cfg).messages
    assert [m.content for m in saved if isinstance(m, HumanMessage)].count(guide) == 1
