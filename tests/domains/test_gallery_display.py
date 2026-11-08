"""Explicit image display survives omitted prose without inspecting pixels or writing data."""


import httpx
import pytest
from _framework_fixture import call, model_replies
from _own_support import config
from langchain_core.messages import HumanMessage

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


def test_nonadmin_show_refuses_before_reading_gallery(backend):
    cfg = config(principal=Principal(7, "user", SOURCE_ASSERTION))
    assert show_gallery_image.invoke({"image_id": 74}, config=cfg).kind == "unavailable"
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
