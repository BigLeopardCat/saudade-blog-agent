"""Provider length stops must never look like complete task answers."""

import json

import httpx
import pytest
from langchain_core.messages import HumanMessage

from agent.runtime.session import ToolSession
from eval import native_assertions
from models.llm import get_llm


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_real_sdk_stream_preserves_finish_reason_and_marks_incomplete_answers(monkeypatch, finish):
    events = []
    monkeypatch.setattr("agent.runtime.middleware.record",
                        lambda node, event, **data: events.append({"event": event, **data}))

    def handle(request):
        assert json.loads(request.content)["stream"] is True
        base = {"id": "fixture", "object": "chat.completion.chunk", "created": 0,
                "model": "fixture"}
        chunks = [
            {**base, "choices": [{"index": 0, "delta": {"content": "已展示的文字"},
                                  "finish_reason": None}]},
            {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            {**base, "choices": [], "usage": {
                "prompt_tokens": 18, "completion_tokens": 8, "total_tokens": 26}},
        ]
        wire = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              content=(wire + "data: [DONE]\n\n").encode())

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        model = get_llm(base_url="https://fixture.invalid/v1", api_key="fixture-only",
                        http_client=client, streaming=True, stream_usage=True, max_tokens=64)
        result = ToolSession(model, {}, {}, {}).run([HumanMessage(content="回答")])
    done = next(event for event in events if event["event"] == "llm_done")
    assert done["finish_reason"] == finish
    assert result.reply.startswith("已展示的文字")
    failures = native_assertions.check({"forbid_runtime_stop": True}, {"native_events": events})
    if finish == "length":
        assert "尚未完整生成" in result.reply and failures
    else:
        assert result.reply == "已展示的文字" and not failures


@pytest.mark.parametrize("event", [
    {"event": "llm_done", "finish_reason": "length"},
    {"event": "answer_truncated"},
    {"event": "answer_blocked"},
    {"event": "invalid_tool_call"},
])
def test_incomplete_response_cannot_pass_just_because_it_is_nonempty(event):
    assert native_assertions.check({"forbid_runtime_stop": True}, {
        "text": "有正文也不能代表完成", "native_events": [event]})
