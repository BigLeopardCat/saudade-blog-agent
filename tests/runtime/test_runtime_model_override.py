"""Public model overrides survive the application's shared budget middleware."""

import pytest
from _framework_fixture import model_replies
from _own_support import config
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from agent.runtime import middleware
from agent.runtime.session import ToolSession


def test_outer_model_override_reaches_the_sdk_once_and_preserves_usage(monkeypatch):
    events = []
    monkeypatch.setattr(middleware, "record", lambda node, event, **fields:
                        events.append({"event": event, **fields}))

    with model_replies({"content": "不应使用默认模型。"}) as (default, default_requests), \
            model_replies({"content": "使用已选模型。"}) as (chosen, chosen_requests):
        selected = chosen.model_copy(update={"model_name": "routed-fixture"})

        class SelectModel(AgentMiddleware):
            def wrap_model_call(self, request, handler):
                return handler(request.override(model=selected))

        session = ToolSession(default, config(), {}, {}, middleware=[lambda _: SelectModel()])
        result = session.run([HumanMessage(content="测试当前模型接缝")])
        assert result.reply == "使用已选模型。"
        assert not default_requests and len(chosen_requests) == 1
        assert chosen_requests[0]["model"] == "routed-fixture"
        assert session.budget.model_calls == 1 and session.budget.tool_calls == 0
        assert session.budget.tokens == 15
        start = next(event for event in events if event["event"] == "llm_start")
        done = next(event for event in events if event["event"] == "llm_done")
        assert start["model"] == done["model"] == "routed-fixture"
        assert done["response_model"] == "gpt-4o-mini"


def test_model_identity_never_uses_an_objects_string_representation():
    class SensitiveObject:
        def __repr__(self):
            pytest.fail("model identity must not serialize the client")

    assert middleware._model_name(SensitiveObject()) is None
