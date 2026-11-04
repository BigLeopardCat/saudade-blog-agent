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


def test_thinking_summary_reads_the_same_sdk_extra_body_as_the_request(monkeypatch):
    events = []
    monkeypatch.setattr(request_trace, "record", lambda *args, **data: events.append(data))
    for enabled in (True, False):
        mode = {"type": "enabled" if enabled else "disabled"}
        model = DeepSeekChat(api_key="private-credential", model="deepseek-v4.1-flash",
                            extra_body={"thinking": mode})
        payload = model._get_request_payload("hello")
        assert events[-1]["thinking"] == payload["extra_body"]["thinking"] == mode
    assert events[0]["controls"] != events[1]["controls"]
    assert "private-credential" not in json.dumps(events)


def test_http_wire_records_the_deepseek_sdk_body_and_actual_header(monkeypatch):
    import httpx

    from config.settings import settings
    from models.llm import get_llm
    from models.transport import ModelIO, io_scope

    events, requests = [], []
    monkeypatch.setattr(settings, 'llm_provider', 'opencode')
    monkeypatch.setattr(request_trace, 'record',
                        lambda _node, event, **data: events.append((event, data)))

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'id':'fixture','object':'chat.completion',
            'created':0,'model':'deepseek-v4.1-flash','choices':[{'index':0,
            'message':{'role':'assistant','content':'ok'},'finish_reason':'stop'}]})

    with ModelIO(transport=httpx.MockTransport(handle)) as io, io_scope(io), \
            session_scope('intended-private-session'):
        model = get_llm(api_key='private-credential', model='deepseek-v4.1-flash',
            base_url='https://fixture.test/v1', streaming=False, enable_thinking=False)
        model.invoke('private-user-text',
                     extra_headers={'x-opencode-session':'actual-private-session'})
    wire = next(data for event, data in events if event == 'wire_request')
    before = next(data for event, data in events if event == 'request_layout')
    actual = json.loads(requests[0].content)
    assert wire['session'] == request_trace.fingerprint('actual-private-session')
    assert before['session'] == request_trace.fingerprint('intended-private-session')
    assert wire['messages'] == [request_trace.fingerprint(row) for row in actual['messages']]
    assert wire['thinking'] == actual['thinking'] == {'type':'disabled'}
    assert 'extra_body' not in wire['control_fingerprints']
    assert all(value not in json.dumps(events) for value in (
        'private-credential','private-user-text','intended-private-session','actual-private-session'))


def test_large_or_non_chat_wire_observations_do_not_break_the_request(monkeypatch):
    import asyncio

    import httpx

    events = []
    monkeypatch.setattr(request_trace, 'record',
                        lambda _node, event, **data: events.append((event, data)))
    for body in (b'x'*(request_trace.MAX_WIRE_LAYOUT_BYTES+1), b'not JSON',
                 b'[]', b'{"messages":["private-user-text"]}'):
        request = httpx.Request('POST','https://fixture.test/v1/chat/completions',content=body)
        asyncio.run(request_trace.record_wire_request(request))
    assert len(events) == 4
    assert all(event == 'wire_request_unavailable' and data['session'] is None
               for event, data in events)
    assert 'private-user-text' not in json.dumps(events)
