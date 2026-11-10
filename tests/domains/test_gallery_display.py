"""Explicit image display survives omitted prose without inspecting pixels or writing data."""


import httpx
import pytest
from _framework_fixture import call, model_replies
from _own_support import config
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.domains.gallery.reads import show_gallery_image
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.capabilities import ToolCatalog
from agent.products.blog.media import GalleryDisplayMiddleware
from agent.products.blog.tool_policy import BlogToolMiddleware
from agent.runtime.session import ToolSession
from agent.transport import collect_turn
from tools import base

URL = "https://images.example/selected.png"


@pytest.fixture
def backend(monkeypatch):
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path.endswith("/gallery/images/74")
        return httpx.Response(200, json={"code": 200, "data": {
            "imageKey": 74, "imageUrl": URL, "displayName": "猫咪", "description": "封面",
            "folderId": 0, "isFavorite": False, "version": "a" * 64}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield requests


@pytest.mark.parametrize("reply", ["喏，主人看看这张。", f"![猫咪]({URL})"])
def test_actual_framework_display_does_not_depend_on_model_repeating_image_markdown(backend, reply):
    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    tools = ToolCatalog().domains(cfg, False)["admin_gallery"]
    with model_replies({"tool_calls": [call("load_admin_gallery_info_tools", "load")]},
                       {"tool_calls": [call("show_gallery_image", "show", image_id=74)]},
                       {"content": reply}) as (model, requests):
        session = ToolSession(model, cfg, {}, {"admin_gallery": tools},
                              middleware=(GalleryDisplayMiddleware, BlogToolMiddleware))
        result = session.run([HumanMessage(content="给我展示图片74，不要分析画面。")])
    assert result.reply.count(f"]({URL})") == 1
    assert result.reply.startswith(reply)
    assert len(backend) == 1 and len(requests) == 3
    assert "data:image" not in str(requests)
    assert not result.execution or not result.execution.get("pending_confirm")
    assert collect_turn([result]).reply == result.reply


def test_unverified_image_reference_is_replaced_before_streaming(backend):
    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    tools = ToolCatalog().domains(cfg, False)["admin_gallery"]
    bad_reply = {"content": "请看：![猫咪](https://invented.example/wrong.png)"}
    with model_replies({"tool_calls": [call("show_gallery_image", "show", image_id=74)]},
                       bad_reply) as (model, _):
        session = ToolSession(model, cfg, tools, {},
                              middleware=(GalleryDisplayMiddleware, BlogToolMiddleware))
        stream, chunks = session.stream([HumanMessage(content="展示图片74。")]), []
        while True:
            try:
                chunks.append(next(stream))
            except StopIteration as stop:
                result = stop.value
                break
    assert result.reply == f"![猫咪]({URL})"
    assert "invented.example" not in str(chunks)
    assert len(backend) == 1


def test_nonadmin_show_refuses_before_reading_gallery(backend):
    cfg = config(principal=Principal(7, "user", SOURCE_ASSERTION))
    runtime = ToolRuntime(state={}, context=None, config=cfg, stream_writer=lambda _: None,
                          tool_call_id="show", store=None)
    assert show_gallery_image.func(74, runtime).kind == "unavailable"
    assert backend == []


def test_metadata_inspection_alone_does_not_display_an_image(backend):
    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    tools = ToolCatalog().domains(cfg, False)["admin_gallery"]
    with model_replies({"tool_calls": [call("get_gallery_image_metadata", "read", image_id=74)]},
                       {"content": "名称是猫咪。"}) as (model, _):
        session = ToolSession(model, cfg, tools, {},
                              middleware=(GalleryDisplayMiddleware, BlogToolMiddleware))
        result = session.run([HumanMessage(content="只查图片名称，不要展示。")])
    assert result.reply == "名称是猫咪。"
    assert len(backend) == 1


def test_display_is_persisted_once_and_does_not_reappear_in_the_next_turn(backend):
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.products.blog.application import BlogApplication
    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.runtime.service import AgentRuntime

    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    with model_replies({"tool_calls": [call("load_admin_gallery_info_tools", "load")]},
                       {"tool_calls": [call("show_gallery_image", "show", image_id=74)]},
                       {"content": "看这张。"}, {"content": "晚安。"}) as (model, requests):
        store = CheckpointConversationStore(InMemorySaver())
        runtime = AgentRuntime(store, application=BlogApplication(),
                               model_factory=lambda **_: model)
        first = list(runtime.stream_events(
            {"messages": [HumanMessage(content="展示图片74")]}, cfg))
        assert first[-1].reply.count(f"]({URL})") == 1
        assert store.load(cfg).messages[-1].content == first[-1].reply
        next_turn = list(runtime.stream_events({"messages": [HumanMessage(content="晚安")]}, cfg))
    assert next_turn[-1].reply == "晚安。" and len(requests) == 4
    assert store.load(cfg).messages[-1].content == "晚安。"
    assert len(backend) == 1


def test_failed_display_does_not_append_or_invent_an_image(monkeypatch):
    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    tools = ToolCatalog().domains(cfg, False)["admin_gallery"]
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(503))) as client:
        monkeypatch.setattr(base, "_client", client)
        with model_replies({"tool_calls": [call("show_gallery_image", "show", image_id=74)]},
                           {"content": "图片服务暂不可用。"}) as (model, _):
            result = ToolSession(model, cfg, tools, {},
                middleware=(GalleryDisplayMiddleware, BlogToolMiddleware)).run(
                    [HumanMessage(content="给我展示图片74")])
    assert result.reply == "图片服务暂不可用。"
    assert "![" not in result.reply


def test_known_metadata_link_is_available_next_turn_without_another_read(backend):
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.products.blog.application import BlogApplication
    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.runtime.service import AgentRuntime

    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    with model_replies({"tool_calls": [call("load_admin_gallery_info_tools", "load")]},
                       {"tool_calls": [call("get_gallery_image_metadata", "read", image_id=74)]},
                       {"content": "名称是猫咪。"},
                       {"content": f"![猫咪]({URL})"}) as (model, requests):
        store = CheckpointConversationStore(InMemorySaver())
        runtime = AgentRuntime(store, application=BlogApplication(),
                               model_factory=lambda **_: model)
        list(runtime.stream_events({"messages": [HumanMessage(
            content="只查图片74的名称，不要展示。")]}, cfg))
        replies = list(runtime.stream_events({"messages": [HumanMessage(
            content="展示刚才那张，复用链接，不要重新查询。")]}, cfg))
    assert len(requests) == 4 and len(backend) == 1
    assert URL in str(requests[-1])
    assert replies[-1].reply == f"![猫咪]({URL})"
    assert store.load(cfg).messages[-1].content == replies[-1].reply


def test_explicit_show_reuses_a_verified_link_after_runtime_reload(backend):
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.products.blog.application import BlogApplication
    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.runtime.service import AgentRuntime

    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    with model_replies({"tool_calls": [call("load_admin_gallery_info_tools", "load")]},
                       {"tool_calls": [call("get_gallery_image_metadata", "read", image_id=74)]},
                       {"content": "名称是猫咪。"},
                       {"tool_calls": [call("show_gallery_image", "show", image_id=74)]},
                       {"content": "这张喵。"}) as (model, requests):
        saver = InMemorySaver()

        def runtime():
            return AgentRuntime(CheckpointConversationStore(saver), application=BlogApplication(),
                                model_factory=lambda **_: model)

        list(runtime().stream_events(
            {"messages": [HumanMessage(content="只查74的元数据。") ]}, cfg))
        result = list(runtime().stream_events(
            {"messages": [HumanMessage(content="展示刚才那张。") ]}, cfg))[-1]
    assert len(requests) == 5 and len(backend) == 1
    assert result.reply.count(f"]({URL})") == 1
    last = requests[-1]["messages"][-1]
    assert last["tool_call_id"] == "show" and URL in last["content"]
    parameters = next(t["function"]["parameters"] for t in requests[-2]["tools"]
                      if t["function"]["name"] == "show_gallery_image")
    assert "runtime" not in str(parameters)


@pytest.mark.parametrize("later", [
    ToolMessage(content="Unknown write outcome", tool_call_id="write",
                name="manage_gallery_images", status="error"),
    ToolMessage(content="Read failed", tool_call_id="failed",
                name="get_gallery_image_metadata", status="error"),
])
def test_a_later_write_or_failed_read_invalidates_a_display_snapshot(later):
    from agent.domains.gallery.display import snapshot

    read = ToolMessage(content="verified", name="get_gallery_image_metadata", tool_call_id="read",
        artifact={"kind": "ok", "data": {"image_id": 74, "url": URL, "name": "猫咪"}})
    assert snapshot([read], 74)["imageUrl"] == URL
    assert snapshot([read, later], 74) is None
    assert snapshot([read], 75) is None
    assert snapshot([HumanMessage(content=URL), AIMessage(content=URL)], 74) is None
