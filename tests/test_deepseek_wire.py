"""Provider reasoning, tools, images and usage survive the actual SDK boundary."""

import json

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from models.deepseek import DeepSeekChat


@pytest.mark.parametrize("streaming", [False, True])
def test_reasoning_tool_roundtrip_retains_gateway_headers_media_and_usage(streaming):
    requests = []
    image = "data:image/png;base64,AA=="

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.host == "gateway.test" and request.url.path == "/go/v1/chat/completions"
        assert request.headers["x-opencode-session"] == "stable-session"
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "low"
        assert body["messages"][0]["content"][1]["image_url"]["url"] == image
        if len(requests) == 2:
            assistant = body["messages"][1]
            assert assistant["reasoning_content"] == "provider-private-replay"
            assert assistant["tool_calls"][0]["id"] == "lookup"
            assert body["messages"][2]["tool_call_id"] == "lookup"
        message = ({"role": "assistant", "content": "",
                    "reasoning_content": "provider-private-replay",
                    "tool_calls": [{"id": "lookup", "type": "function", "function": {
                        "name": "lookup", "arguments": "{}"}}]} if len(requests) == 1 else
                   {"role": "assistant", "content": "Read complete.",
                    "reasoning_content": "verified"})
        usage = {"prompt_tokens": 30, "completion_tokens": 8, "total_tokens": 38,
                 "prompt_cache_hit_tokens": 20,
                 "completion_tokens_details": {"reasoning_tokens": 3}}
        finish = "tool_calls" if len(requests) == 1 else "stop"
        if not streaming:
            return httpx.Response(200, json={"id": "reply", "model": "deepseek-v4.1-flash",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": usage})
        if message.get("tool_calls"):
            message["tool_calls"][0]["index"] = 0
        chunks = [{"id": "reply", "model": "deepseek-v4.1-flash", "choices": [
            {"index": 0, "delta": message, "finish_reason": finish}]},
            {"id": "reply", "choices": [], "usage": usage}]
        wire = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=wire)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        model = DeepSeekChat(model="deepseek-v4.1-flash", api_key="test-key",
            base_url="https://gateway.test/go/v1", http_client=client, max_retries=0,
            stream_usage=True, reasoning_effort="low", extra_body={"thinking": {"type": "enabled"}},
            default_headers={"x-opencode-session": "stable-session"})
        model = model.bind_tools([{"name": "lookup", "description": "Read fixture",
                                   "parameters": {"type": "object", "properties": {}}}])
        messages = [HumanMessage(content=[{"type": "text", "text": "Read"},
            {"type": "image_url", "image_url": {"url": image}}])]

        def invoke(prompt):
            if not streaming:
                return model.invoke(prompt)
            chunks = list(model.stream(prompt))
            result = chunks[0]
            for chunk in chunks[1:]:
                result += chunk
            return result

        first = invoke(messages)
        final = invoke([*messages, first, ToolMessage(content="found", tool_call_id="lookup")])
    assert final.content == "Read complete."
    assert final.usage_metadata["input_token_details"]["cache_read"] == 20
    assert final.usage_metadata["output_token_details"]["reasoning"] == 3


def test_canonical_assistant_history_uses_empty_reasoning_without_inventing_it():
    model = DeepSeekChat(model="deepseek-v4.1-flash", api_key="test-key",
                         base_url="https://gateway.test/go/v1")
    previous = AIMessage(content="Earlier reply")
    outgoing = model._get_request_payload([previous])["messages"][0]
    assert outgoing["reasoning_content"] == "" and previous.additional_kwargs == {}
