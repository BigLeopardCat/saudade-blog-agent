"""Tool traces join framework exchanges without exposing approval bearers."""

import hashlib
import json
import secrets
from types import SimpleNamespace

from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.tools import tool

from agent.runtime.middleware import StreamGuard
from agent.runtime.results import TurnResult
from agent.runtime.session import ToolSession
from tools.result import ok


def test_first_model_token_ignores_metadata_and_never_logs_content(monkeypatch):
    events = []
    clock = iter([10.0, 10.3])
    monkeypatch.setattr("agent.runtime.middleware.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("agent.runtime.middleware.record",
                        lambda node, event, **data: events.append({"event": event, **data}))
    guard = StreamGuard(SimpleNamespace(config={}, _round_index=0))
    guard.on_chat_model_start({}, [])
    guard.on_llm_new_token("", chunk=SimpleNamespace(message=AIMessageChunk(content="")))
    assert not events
    guard.on_llm_new_token("PRIVATE_MODEL_TEXT", chunk=SimpleNamespace(
        message=AIMessageChunk(content="PRIVATE_MODEL_TEXT")))
    assert events[0]["event"] == "model_first_token"
    assert abs(events[0]["duration_s"] - 0.3) < 1e-8
    assert "PRIVATE_MODEL_TEXT" not in json.dumps(events)


def test_repeated_reads_and_preparation_keep_distinct_framework_call_ids(monkeypatch):
    credential = secrets.token_urlsafe()
    events = []
    monkeypatch.setattr("agent.runtime.session.record",
                        lambda node, event, **data: events.append({"event": event, **data}))
    monkeypatch.setattr("agent.runtime.tool_execution.record",
                        lambda node, event, **data: events.append({"event": event, **data}))

    @tool
    def read_article(article_id: int) -> object:
        """Read the selected article."""
        return ok(f"Article {article_id}")

    @tool
    def prepare_article(article_id: int) -> object:
        """Prepare the selected article change."""
        return TurnResult("Please confirm.", {"pending_confirm": {"token": credential}})

    responses = iter([
        AIMessage(content="", tool_calls=[
            {"name": "read_article", "id": "read-1", "args": {"article_id": 19}},
            {"name": "read_article", "id": "read-2", "args": {"article_id": 19}}]),
        AIMessage(content="", tool_calls=[
            {"name": "prepare_article", "id": "prepare-1", "args": {"article_id": 19}}]),
    ])

    class Model:
        max_tokens = 128

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages, **kwargs):
            assert credential not in str(messages)
            return next(responses)

    session = ToolSession(Model(), {}, {t.name: t for t in (read_article, prepare_article)}, {})
    list(session.stream([HumanMessage(content="Read twice and prepare the change.")]))
    calls = [e for e in events if e["event"] == "call"]
    assert [e["call_id"] for e in calls] == ["read-1", "read-2", "prepare-1"]
    assert all(e["args"] == {"article_id": 19} for e in calls[:2])
    assert "args" not in calls[-1]
    assert calls[-1]["args_hash"] == hashlib.sha256(b'{"article_id":19}').hexdigest()
    assert [e["kind"] for e in calls] == ["ok", "ok", "prepared"]
    requested = [e["call_id"] for e in events if e["event"] == "tool_requested"]
    assert requested == [e["call_id"] for e in calls]
    results = [e["call_id"] for e in events if e["event"] == "tool_result"]
    assert results == ["read-1", "read-2"]
    assert credential not in json.dumps(events)
