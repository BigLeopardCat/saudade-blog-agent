"""Cache observations describe SDK payloads without exposing their private values."""

import json

from models import request_trace
from models.deepseek import DeepSeekChat
from models.session import session_scope


def test_actual_deepseek_payload_is_fingerprinted_after_reasoning_replay(monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage

    events = []
    monkeypatch.setattr(request_trace, "record", lambda *args, **data: events.append(data))
    model = DeepSeekChat(api_key="private-credential", model="deepseek-v4.1-flash")
    with session_scope("private-session"):
        payload = model._get_request_payload([
            HumanMessage(content="private-user-text"), AIMessage(content="reply",
                additional_kwargs={"reasoning_content": "private-reasoning"})])
    event = events[-1]
    assert event["messages"] == [request_trace.fingerprint(m) for m in payload["messages"]]
    assert event["session"] == request_trace.fingerprint("private-session")
    assert all(value not in json.dumps(event) for value in
               ("private-user-text", "private-reasoning", "private-credential", "private-session"))


def test_wire_tool_order_and_message_changes_have_distinct_fingerprints(monkeypatch):
    events = []
    monkeypatch.setattr(request_trace, "record", lambda *args, **data: events.append(data))
    tools = [{"name": "first"}, {"name": "second"}]
    for menu, text in [(tools, "before"), (list(reversed(tools)), "before"), (tools, "after")]:
        request_trace.record_layout({"tools": menu,
                                     "messages": [{"role": "user", "content": text}]})
    assert events[0]["tools"] != events[1]["tools"]
    assert events[0]["messages"] == events[1]["messages"]
    assert events[0]["tools"] == events[2]["tools"]
    assert events[0]["messages"] != events[2]["messages"]
