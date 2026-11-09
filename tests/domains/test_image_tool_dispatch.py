"""Exercise gallery/image tools through discovery, LangChain ToolNode and blog policy."""

import json
import secrets
import time

import httpx
import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage

from agent import confirm
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.admin_domains import preparation_names
from agent.products.blog.capabilities import ToolCatalog
from agent.products.blog.tool_policy import BlogToolMiddleware
from agent.runtime.session import ToolSession
from tools import base

ID = "6b9e2bf1-56af-428c-8177-e3ad0641e036"


class Model:
    def __init__(self, *messages):
        self.messages = iter(messages)

    def bind_tools(self, *args, **kwargs):
        return self

    def invoke(self, prompt):
        return next(self.messages)


@pytest.mark.parametrize(
    "domain,name,args",
    [
        (
            "admin_image_generation",
            "prepare_image_generation",
            {"prompt": "未经批准不生成的证件照"},
        ),
        ("admin_image_generation", "prepare_generated_image_acceptance", {"job_id": ID}),
        (
            "admin_gallery_management",
            "prepare_gallery_images",
            {"intent": {"action": "metadata", "items": [{"imageId": 11,
                "changes": {"displayName": "新名称"}}]}},
        ),
        ("admin_gallery_management", "prepare_gallery_images", {"intent": {
            "action": "move", "imageIds": list(range(100, 0, -1)), "folderId": 2}}),
        ("admin_gallery_management", "prepare_gallery_images", {"intent": {
            "action": "favorite", "imageIds": [11, 12], "isFavorite": False}}),
        ("admin_gallery_management", "prepare_gallery_images", {"intent": {
            "action": "delete", "imageIds": [11, 12]}}),
        ("admin_article_management", "prepare_article_cover", {"article_id": 3, "image_id": 11}),
    ],
)
def test_discovered_preparations_reach_approval_through_the_real_tool_chain(
    monkeypatch, domain, name, args
):
    requests = []
    job = {
        "id": ID,
        "requestHash": "a" * 64,
        "imageRemoved": False,
        "status": "pending",
        "outputHash": "b" * 64,
        "previewUrl": f"/api/public/image-generation/{ID}/preview?token=fixture",
        "request": {
            "model": "fixture",
            "provider": "https://images.test/v1",
            "prompt": "已批准的原始提示词",
            "size": "1536x1024",
            "quality": "high",
            "references": [],
        },
    }

    def handle(request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/options"):
            data = {"configured": True, "defaultModel": "fixture", "models": ["fixture"]}
        elif path.endswith("/jobs"):
            intent = json.loads(request.content)
            data = {**job, "request": {**intent, "references": [], "provider": "https://images.test/v1"}}
        elif path.endswith(ID):
            data = {**job, "status": "review"}
        elif path.endswith("/image-actions/prepare"):
            intent = json.loads(request.content)
            ids = intent.get("imageIds", [item["imageId"] for item in intent.get("items", [])])
            data = {"version": "c" * 64, "expiresAt": int(time.time()) + 900,
                    "intent": intent, "matchedIds": ids,
                    "preview": [{"imageId": i, "displayName": "图片", "folderId": 0,
                        "isFavorite": False} for i in ids[:10]],
                    "previewTruncated": len(ids) > 10,
                    "destination": {"id": 2, "name": "封面", "parentId": 0}
                        if intent["action"] == "move" else None}
        elif path.endswith("/cover"):
            data = {"articleId": 3, "title": "文章", "version": "b" * 64}
        else:
            data = {
                "imageKey": 11,
                "imageUrl": "https://images.example/a.png",
                "displayName": "图片",
                "description": "风景",
                "folderId": 0,
                "isFavorite": False,
                "version": "c" * 64,
            }
        return httpx.Response(200, json={"code": 200, "data": data})

    cfg = config(principal=Principal(7, "superadmin", SOURCE_ASSERTION))
    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        domains = ToolCatalog().domains(cfg, True)
        assert name in domains[domain]
        model = Model(
            AIMessage(
                content="",
                tool_calls=[{"name": f"load_{domain}_info_tools", "args": {}, "id": "load"}],
            ),
            AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "prepare"}]),
        )
        session = ToolSession(
            model, cfg, {}, {domain: domains[domain]}, middleware=(BlogToolMiddleware,)
        )
        result = session.run([HumanMessage(content="准备对应图片操作，批准前不要执行")])
    assert result.execution["pending_confirm"]["q"]
    assert requests
    assert all(not str(r.url).endswith(("/approve", "/accept")) for r in requests)
    assert all(r.method == "GET" or r.url.path.endswith(("/jobs", "/image-actions/prepare"))
               for r in requests)
    if name == "prepare_gallery_images":
        assert len(requests) == 1
        grant = confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)
        assert grant and len(grant["specs"]) == 1
        assert grant["specs"][0]["tool"] == "manage_gallery_images"
        assert "prepare_gallery_image_change" not in domains[domain]


def test_all_catalog_preparations_pass_policy_but_direct_business_writes_remain_blocked():
    cfg = config(principal=Principal(7, "superadmin", SOURCE_ASSERTION))
    session = ToolSession(Model(), cfg, {}, {}, middleware=(BlogToolMiddleware,))
    policy = BlogToolMiddleware(session)
    assert all(policy._allowed(name) for name in preparation_names())
    for name in (
        "start_image_generation",
        "accept_generated_image",
        "manage_gallery_images",
        "set_article_cover",
    ):
        assert not policy._allowed(name)
