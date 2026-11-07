"""The actual provider SDK receives the same bounded action shapes ToolNode accepts."""

import json
import secrets

import httpx
import pytest
from _own_support import config

from agent import confirm
from agent.domains.gallery import preparation
from agent.principal import SOURCE_ASSERTION, Principal
from models.deepseek import DeepSeekChat
from tools import base


@pytest.mark.parametrize("intent", [
    {"action": "move", "imageIds": [11], "folderId": 0},
    {"action": "favorite", "imageIds": [11], "isFavorite": False},
    {"action": "metadata", "items": [{"imageId": 11, "changes": {"description": ""}}]},
    {"action": "delete", "imageIds": [11]},
])
def test_sdk_sends_nonnullable_action_variants_and_can_prepare_the_returned_call(
    monkeypatch, intent
):
    import time

    business_requests, model_requests = [], []

    def provider(request):
        body = json.loads(request.content)
        model_requests.append(body)
        function = body["tools"][0]["function"]
        assert function["name"] == "prepare_gallery_images"
        assert "strict" not in function
        schema = function["parameters"]["properties"]["intent"]
        variants = schema.get("oneOf", schema.get("anyOf"))
        shapes = {variant["properties"]["action"]["const"]: variant for variant in variants}
        assert set(shapes) == {"move", "favorite", "metadata", "delete"}
        for name, required in {
            "move": {"action", "imageIds", "folderId"},
            "favorite": {"action", "imageIds", "isFavorite"},
            "metadata": {"action", "items"}, "delete": {"action", "imageIds"},
        }.items():
            assert set(shapes[name]["properties"]) == set(shapes[name]["required"]) == required
            assert shapes[name]["additionalProperties"] is False
        for name in ("move", "favorite", "delete"):
            assert shapes[name]["properties"]["imageIds"]["uniqueItems"] is True
        patch = shapes["metadata"]["properties"]["items"]["items"]["properties"]["changes"]
        assert patch["minProperties"] == 1 and patch["additionalProperties"] is False
        assert "required" not in patch
        for value in patch["properties"].values():
            assert "default" not in value and value["type"] != "null"
            assert "anyOf" not in value and "oneOf" not in value
        return httpx.Response(200, json={"id": "gallery", "model": "deepseek-v4.1-flash",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": "", "tool_calls": [{"id": "prepare",
                    "type": "function", "function": {"name": function["name"],
                        "arguments": json.dumps({"intent": intent})}}]}}]})

    def backend(request):
        business_requests.append(request)
        assert request.method == "POST" and request.url.path.endswith("/image-actions/prepare")
        assert json.loads(request.content) == intent
        return httpx.Response(200, json={"code": 200, "data": {
            "version": "a" * 64, "expiresAt": int(time.time()) + 900, "intent": intent,
            "matchedIds": [11], "previewTruncated": False,
            "destination": {"id": 0, "name": "未分类", "parentId": 0}
                if intent["action"] == "move" else None,
            "preview": [{"imageId": 11, "displayName": None, "folderId": 0,
                         "isFavorite": False}],
        }})

    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    with httpx.Client(transport=httpx.MockTransport(provider)) as model_client, \
            httpx.Client(transport=httpx.MockTransport(backend)) as business_client:
        monkeypatch.setattr(base, "_client", business_client)
        model = DeepSeekChat(model="deepseek-v4.1-flash", api_key="fixture",
            base_url="https://gateway.test/v1", http_client=model_client, max_retries=0)
        response = model.bind_tools([preparation.prepare_gallery_images]).invoke("准备图片操作")
        tool_call, = response.tool_calls
        prepared = preparation.prepare_gallery_images.invoke(tool_call["args"], config=config(
            principal=Principal(7, "admin", SOURCE_ASSERTION)))
    grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
    assert grant["specs"][0]["args"]["intent"] == intent
    assert len(model_requests) == len(business_requests) == 1
