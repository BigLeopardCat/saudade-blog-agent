"""Image re-reading must be authorized, bounded and genuinely multimodal."""

import threading

import httpx
import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from test_memory_boundaries import ACTOR, Capture, config, run, runtime

from agent.api.schemas import ChatRequest
from agent.domains import conversation_images
from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from agent.runtime import memory, native, native_store, session
from agent.runtime.control import AgentCancelled
from agent.runtime.memory_media import image_id, image_urls
from agent.runtime.results import TurnResult
from tools import base
from utils import trace

URL = "data:image/png;base64," + "R" * 32000
REF = image_id(URL)


def saved_messages():
    return memory.turn_messages([HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": URL}}])], [], "图中有三个参数。", ACTOR)


def call(name, identifier, **args):
    return AIMessageChunk(content="", tool_calls=[{"name": name, "id": identifier, "args": args}])


def tool_result(tool, cfg=None, ref=REF):
    return tool.invoke({"name": tool.name, "id": "read", "type": "tool_call",
                        "args": {"image_id": ref}}, config=cfg or config())


def host(monkeypatch, handler):
    monkeypatch.setattr(base, "_sign_local_jwt", lambda *_: "TEST_CREDENTIAL")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(base, "_client", client)
    return client


def test_native_followup_loads_original_as_image_blocks_without_leaking_bytes_to_trace(monkeypatch):
    requests, events = [], []

    def respond(request):
        requests.append(request)
        assert request.url.path == "/api/chat/history"
        assert request.url.params["conversation_id"] == "42"
        assert request.headers["authorization"] == "Bearer TEST_CREDENTIAL"
        return httpx.Response(200, json={"conversation_id": 42, "items": [
            {"id": 10, "role": "user", "images": [URL]}]})

    client = host(monkeypatch, respond)
    def capture(*args, **kwargs):
        events.append((args, kwargs))
    for module in (trace, native, native_store, session):
        monkeypatch.setattr(module, "record", capture)
    model = Capture(call("load_conversation_images_info_tools", "load"),
                    call("get_conversation_image", "image", image_id=REF),
                    AIMessageChunk(content="第三个参数是连接数。"))
    saver = InMemorySaver()
    selected = runtime(saver, model)
    selected.store.save_turn(config(), saved_messages()[:1], "图中有三个参数。")
    try:
        result = run(selected, ChatRequest(message="图里第三个参数是什么"))[-1]
    finally:
        client.close()
    assert result == TurnResult("第三个参数是连接数。")
    assert len(requests) == 1 and len(model.prompts) == 3
    source = next(message for message in model.prompts[-1]
                  if isinstance(message, ToolMessage) and message.name == "get_conversation_image")
    assert isinstance(source.content, str) and REF in source.content
    assert any(isinstance(message, HumanMessage) and image_urls(message.content) == [URL]
               for message in model.prompts[-1])
    assert any(args[:2] == ("native", "call") and data.get("name") == "get_conversation_image"
               for args, data in events)
    assert URL not in str(events) and "TEST_CREDENTIAL" not in str(events)
    assert URL not in str(selected.store.load(config()).messages)
    assert REF in str(selected.store.load(config()).messages)


@pytest.mark.parametrize("principal", [Principal(8, "user", SOURCE_ASSERTION),
                                      Principal(7, "admin", SOURCE_ASSERTION),
                                      Principal(7, "user", SOURCE_BODY)])
def test_tool_revalidates_captured_identity_and_role_without_issuing_http(monkeypatch, principal):
    tool = conversation_images.tools(saved_messages(), config())["get_conversation_image"]
    calls = []
    client = host(monkeypatch, lambda req: calls.append(req))
    try:
        result = tool_result(tool, config(principal))
    finally:
        client.close()
    assert result.artifact["kind"] == "unavailable" and not calls


@pytest.mark.parametrize("change", ["reference", "conversation", "user_id"])
def test_unknown_reference_or_changed_coordinates_fail_before_http(monkeypatch, change):
    tool = conversation_images.tools(saved_messages(), config())["get_conversation_image"]
    cfg = config()
    if change in ("conversation", "user_id"):
        cfg["configurable"]["conversation_id" if change == "conversation" else "user_id"] = 99
    calls = []
    client = host(monkeypatch, lambda req: calls.append(req))
    try:
        result = tool_result(tool, cfg, image_id("other") if change == "reference" else REF)
    finally:
        client.close()
    assert result.artifact["kind"] == "unavailable" and not calls


@pytest.mark.parametrize("status,body", [
    (403, {}), (404, {}),
    (200, {"conversation_id": 43, "items": [{"id": 1, "role": "user", "images": [URL]}]}),
    (200, {"conversation_id": 42, "items": [{"id": True, "images": [URL]}]}),
    (200, {"conversation_id": 42, "items": [{"id": 1, "role": "user", "images": []}]}),
])
def test_host_failures_or_scope_mismatch_never_return_image_blocks(monkeypatch, status, body):
    tool = conversation_images.tools(saved_messages(), config())["get_conversation_image"]
    client = host(monkeypatch, lambda _: httpx.Response(status, json=body))
    try:
        result = tool_result(tool)
    finally:
        client.close()
    assert not image_urls(result.content)
    assert result.artifact["kind"] in ("unavailable", "not_found")


def test_history_page_download_is_bounded_before_json_decode(monkeypatch):
    monkeypatch.setattr(conversation_images, "_MAX_RESPONSE_BYTES", 1000)
    tool = conversation_images.tools(saved_messages(), config())["get_conversation_image"]
    client = host(monkeypatch, lambda _: httpx.Response(200, content=b"X" * 2000))
    try:
        result = tool_result(tool)
    finally:
        client.close()
    assert result.artifact["kind"] == "unavailable"


def test_cancelled_image_read_propagates_and_does_not_become_a_success(monkeypatch):
    event = threading.Event()
    cfg = config()
    cfg["configurable"]["stop_event"] = event
    tool = conversation_images.tools(saved_messages(), cfg)["get_conversation_image"]

    def respond(_):
        event.set()
        return httpx.Response(200, json={"conversation_id": 42, "items": []})

    client = host(monkeypatch, respond)
    try:
        with pytest.raises(AgentCancelled):
            tool_result(tool, cfg)
    finally:
        client.close()


def test_pagination_stops_at_a_bounded_window_and_requires_monotonic_cursor(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        before = int(request.url.params.get("before_id", "200"))
        rows = [{"id": mid, "role": "user", "images": []} for mid in range(before-50, before)]
        return httpx.Response(200, json={"conversation_id": 42, "items": rows})

    tool = conversation_images.tools(saved_messages(), config())["get_conversation_image"]
    client = host(monkeypatch, respond)
    try:
        result = tool_result(tool)
    finally:
        client.close()
    assert len(calls) == 3 and result.artifact["kind"] == "not_found"
    assert [request.url.params.get("before_id") for request in calls] == [None, "150", "100"]


def test_image_tool_output_serializes_to_the_actual_chat_completions_wire(monkeypatch):
    import json

    from langchain_openai import ChatOpenAI

    from agent.runtime.memory_media import tool_messages

    requests = []

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "fixture",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "seen"},
                         "finish_reason": "stop"}],
        })

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        model = ChatOpenAI(api_key="test-key", model="fixture", http_client=client)
        from langchain_core.messages import AIMessage
        result = ToolMessage(content=[{"type": "image_url", "image_url": {"url": URL}}],
                             name="get_conversation_image", tool_call_id="image")
        answer = model.invoke([
            HumanMessage(content="read the old image"),
            AIMessage(content="", tool_calls=[{"name": "get_conversation_image",
                                              "id": "image", "args": {"image_id": REF}}]),
            *tool_messages(result, "Historical image: " + REF, "image"),
        ])
    assert answer.content == "seen" and len(requests) == 1
    wire = requests[0]["messages"]
    assert wire[2]["role"] == "tool" and isinstance(wire[2]["content"], str)
    assert wire[3]["role"] == "user" and image_urls(wire[3]["content"]) == [URL]


@pytest.mark.parametrize("second", ["image", "text"])
def test_batch_tool_receipts_precede_all_image_attachments_on_the_sdk_wire(monkeypatch, second):
    import json
    from types import SimpleNamespace

    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI

    @tool
    def fixture_read() -> str:
        """Read a text fixture."""
        return "fixture data"

    other_url = "data:image/png;base64," + "B" * 100
    urls = [URL, other_url]
    batch = [{"name": "get_conversation_image", "id": "first",
              "args": {"image_id": REF}},
             {"name": "get_conversation_image" if second == "image" else "fixture_read",
              "id": "second", "args": {"image_id": image_id(other_url)}
              if second == "image" else {}}]
    model = Capture(call("load_conversation_images_info_tools", "load"),
                    AIMessageChunk(content="", tool_calls=batch), AIMessageChunk(content="seen"))
    selected = runtime(InMemorySaver(), model)
    selected.application.catalog = SimpleNamespace(
        initial_tools=lambda _: {fixture_read.name: fixture_read}, domains=lambda *_: {})
    selected.store.save_turn(config(), [HumanMessage(content=[
        {"type": "image_url", "image_url": {"url": url}} for url in urls])], "initial analysis")
    client = host(monkeypatch, lambda _: httpx.Response(200, json={
        "conversation_id": 42, "items": [{"id": 10, "role": "user", "images": urls}]}))
    try:
        assert run(selected, ChatRequest(message="Read both sources"))[-1].reply == "seen"
    finally:
        client.close()
    wires = []

    def handle(request):
        wire = json.loads(request.content)["messages"]
        wires.append(wire)
        pending = set()
        for message in wire:
            if message["role"] == "tool":
                assert message["tool_call_id"] in pending
                pending.remove(message["tool_call_id"])
                assert isinstance(message["content"], str)
            else:
                assert not pending, "Non-tool message interrupted pending sibling receipts"
                pending.update(item["id"] for item in message.get("tool_calls", []))
        assert not pending
        return httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 0, "model": "fixture",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "seen"},
                         "finish_reason": "stop"}],
        })

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        ChatOpenAI(api_key="fixture", model="fixture", http_client=client).invoke(model.prompts[-1])
    assert len(wires) == 1
    assert sum(len(image_urls(message["content"])) for message in wires[0]
               if message["role"] == "user") == (2 if second == "image" else 1)
    assert all(url not in str(selected.store.load(config()).messages) for url in urls)


@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("persistent", [False, True])
def test_current_image_wire_keeps_question_before_original_attachments(count, persistent):
    from types import SimpleNamespace

    from _framework_fixture import model_replies

    from agent.api.context import build_messages

    urls = ["data:image/png;base64,RED_FIXTURE", "data:image/png;base64,BLUE_FIXTURE"][:count]
    req = ChatRequest(message="这些图片是什么颜色", image=urls)
    saver = InMemorySaver() if persistent else None
    with model_replies({"content":"已看到图片。"}) as (model, requests):
        selected = native.NativeRuntime(native_store.NativeConversationStore(saver),
            catalog=SimpleNamespace(initial_tools=lambda _: {}, domains=lambda *_:{}),
            model_factory=lambda **_:model)
        events = list(selected.stream_events({"messages":build_messages(req)}, config()))
        assert events[-1].reply == "已看到图片。"
    assert len(requests) == 1
    content = requests[0]["messages"][-1]["content"]
    assert content[0] == {"type":"text", "text":"[当前问题]: 这些图片是什么颜色"}
    assert [part["image_url"]["url"] for part in content[1:]] == urls
    assert all(part["type"] == "image_url" for part in content[1:])
    assert all(message["role"] == "user" for message in requests[0]["messages"]
               if isinstance(message.get("content"),list))
    if persistent:
        assert not any(url in str(selected.store.load(config()).messages) for url in urls)
