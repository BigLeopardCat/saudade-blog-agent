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
    assert event["roles"] == ["user", "assistant"]
    assert event["message_chars"] == [len(json.dumps(m, ensure_ascii=False))
                                       for m in payload["messages"]]
    assert event["tool_count"] == 0 and event["tools_chars"] == 2
    assert event["tool_fingerprints"] == [] and event["controls"]
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
    assert events[0]["tool_count"] == 2
    assert events[0]["tool_fingerprints"] == list(reversed(events[1]["tool_fingerprints"]))


def test_generic_provider_records_final_payload_and_hashes_endpoint(monkeypatch):
    from langchain_core.messages import HumanMessage

    from models.cancellable import CancellableChatOpenAI

    events = []
    monkeypatch.setattr(request_trace, "record", lambda *args, **data: events.append(data))
    model = CancellableChatOpenAI(api_key="private-credential", base_url="https://private/api")
    payload = model._get_request_payload([HumanMessage(content="secret")])
    assert events[-1]["messages"] == [request_trace.fingerprint(m) for m in payload["messages"]]
    assert events[-1]["endpoint"] == request_trace.fingerprint("https://private/api")
    assert "private" not in json.dumps(events)


def test_transport_deadline_and_header_changes_are_not_generation_changes(monkeypatch):
    events = []
    monkeypatch.setattr(request_trace, "record", lambda *args, **data: events.append(data))
    request_trace.record_layout({"temperature": 0.7, "timeout": 10,
                                 "extra_headers": {"Authorization": "secret-one"}})
    request_trace.record_layout({"extra_headers": {"Authorization": "secret-two"},
                                 "timeout": 9, "temperature": 0.7})
    assert events[0]["controls"] == events[1]["controls"]
    assert set(events[0]["control_fingerprints"]) == {"temperature"}
    assert "secret" not in json.dumps(events)
