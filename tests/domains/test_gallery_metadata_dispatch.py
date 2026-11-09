"""Discover complete metadata through the real product middleware and ToolNode."""

import json

import httpx
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.capabilities import ToolCatalog
from agent.products.blog.tool_policy import BlogToolMiddleware
from agent.runtime.session import ToolSession
from tools import base


def test_metadata_discovery_reads_once_without_preparing_a_mutation(monkeypatch):
    prompts, requests, menus = [], [], []
    description = "完整描述" * 400

    def handle(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path.endswith("/gallery/images/11")
        return httpx.Response(200, json={"code": 200, "data": {
            "imageKey": 11, "imageUrl": "https://images.example/cover.png",
            "displayName": "山景", "description": description, "folderId": 0,
            "isFavorite": False, "version": "a" * 64}})

    class Model:
        def __init__(self):
            self.messages = iter([
                AIMessage(content="", tool_calls=[{"id": "load", "args": {},
                    "name": "load_admin_gallery_info_tools"}]),
                AIMessage(content="", tool_calls=[{"id": "read", "args": {"image_id": 11},
                    "name": "get_gallery_image_metadata"}]),
                AIMessage(content="已读取完整描述。"),
            ])

        def bind_tools(self, schemas, **kwargs):
            menus.append({item["function"]["name"] for item in schemas})
            return self

        def invoke(self, messages):
            prompts.append(messages)
            return next(self.messages)

    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    domains = ToolCatalog().domains(cfg, True)
    domain = domains["admin_gallery"]
    management = domains["admin_gallery_management"]
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        result = ToolSession(Model(), cfg, {}, {"admin_gallery": domain,
            "admin_gallery_management": management},
            middleware=(BlogToolMiddleware,)).run([HumanMessage(content="读图片11的完整描述。")])
    assert result.reply == "已读取完整描述。" and len(requests) == 1
    assert "prepare_gallery_images" in management
    assert "prepare_article_cover" not in management
    assert "prepare_article_cover" in domains["admin_article_management"]
    assert all(not menu.intersection(management) for menu in menus)
    assert domain.keys() <= menus[-1]
    assert "load_admin_gallery_management_info_tools" in menus[-1]
    read = next(message for message in prompts[-1]
        if isinstance(message, ToolMessage) and message.name == "get_gallery_image_metadata")
    assert json.loads(read.content)["description"] == description
    assert "data:image" not in str(read.content)
