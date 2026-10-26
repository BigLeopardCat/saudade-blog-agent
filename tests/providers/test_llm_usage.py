"""Provider usage stays measurable through the native request trace."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent.llm_usage import usage_fields
from agent.runtime.session import ToolSession


@pytest.mark.parametrize("metadata,expected", [
    ({"input_tokens": 100, "output_tokens": 7,
      "input_token_details": {"cache_read": 64}}, {"input": 100, "output": 7, "cache_read": 64}),
    ({"input_tokens": "12", "output_tokens": "3"}, {"input": 12, "output": 3}),
    ({"input_tokens": 100, "output_tokens": 7}, {"input": 100, "output": 7}),
    ({"input_tokens": 100, "input_token_details": {"cache_read": 0}},
     {"input": 100, "cache_read": 0}),
    ({"input_tokens": True, "output_tokens": 1}, {"output": 1}),
    ({"input_tokens": "invalid"}, {}), ({}, {}), ("invalid", {}),
])
def test_normalized_usage_preserves_unknown_fields(metadata, expected):
    assert usage_fields(SimpleNamespace(usage_metadata=metadata)) == expected


def test_raw_gateway_usage_and_absent_responses():
    raw = AIMessage(content="", response_metadata={"token_usage": {
        "prompt_tokens": 100, "completion_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 64}}})
    assert usage_fields(raw) == {"input": 100, "output": 7, "cache_read": 64}
    for response in (None, object(), AIMessage(content="")):
        assert usage_fields(response) == {}


@pytest.mark.parametrize("cache", [None, 0, 64])
def test_native_loop_records_measured_cache_usage_without_inventing_a_zero(monkeypatch, cache):
    events = []
    monkeypatch.setattr("agent.runtime.middleware.record",
                        lambda node, event, **data: events.append({"event": event, **data}))
    usage = {"input_tokens": 100, "output_tokens": 7, "total_tokens": 107}
    if cache is not None:
        usage["input_token_details"] = {"cache_read": cache}

    class Model:
        def invoke(self, messages, **kwargs):
            return AIMessage(content="完成。", usage_metadata=usage)

    session = ToolSession(Model(), {}, {}, {})
    assert session.run([HumanMessage(content="你好")]).reply == "完成。"
    done = [event for event in events if event["event"] == "llm_done"]
    assert len(done) == 1 and done[0]["engine"] == "langchain"
    assert done[0]["input"] == 100 and done[0]["output"] == 7
    if cache is None:
        assert "cache_read" not in done[0]
    else:
        assert done[0]["cache_read"] == cache
    assert session.budget.tokens == 107 and session.budget.model_calls == 1
