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
    prompts, requests = [], []
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

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages):
            prompts.append(messages)
            return next(self.messages)

    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    domain = ToolCatalog().domains(cfg, True)["admin_gallery"]
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        result = ToolSession(Model(), cfg, {}, {"admin_gallery": domain},
            middleware=(BlogToolMiddleware,)).run([HumanMessage(content="读图片11的完整描述。")])
    assert result.reply == "已读取完整描述。" and len(requests) == 1
    read = next(message for message in prompts[-1]
        if isinstance(message, ToolMessage) and message.name == "get_gallery_image_metadata")
    assert json.loads(read.content)["description"] == description
    assert "data:image" not in str(read.content)
