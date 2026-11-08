"""Gallery metadata stays parseable through ToolNode and never skips trimmed rows."""

import json

import httpx
import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.domains.gallery.reads import get_gallery_index
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.tool_policy import BlogToolMiddleware
from agent.runtime.message_content import estimate_tokens
from agent.runtime.session import ToolSession
from config.settings import settings
from tools import base


@pytest.fixture
def index(monkeypatch):
    state = {"rows": [], "requests": []}

    def handle(request):
        state["requests"].append(request)
        assert request.method == "GET" and request.url.path.endswith("/gallery/images")
        params = request.url.params
        before, limit = int(params.get("before", "9999")), int(params["limit"])
        rows = [row for row in state["rows"] if row["imageKey"] < before]
        selected = rows[:limit]
        cursor = selected[-1]["imageKey"] if len(rows) > limit else None
        return httpx.Response(200, json={"code": 200, "data": {
            "items": selected, "nextCursor": cursor}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


def rows(count, character):
    return [{"imageKey": 1000 - number, "imageUrl": "https://images.example/image.png",
             "displayName": character * 120, "description": character * 160,
             "folderId": 0, "isFavorite": False, "version": "a" * 64}
            for number in range(count)]


def dispatch(args):
    prompts = []

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages):
            prompts.append(messages)
            return (AIMessage(content="", tool_calls=[{"id": "gallery-read-" + "x" * 128,
                "name": get_gallery_index.name, "args": args}]) if len(prompts) == 1
                else AIMessage(content="已读取这页图库。"))

    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    session = ToolSession(Model(), cfg, {get_gallery_index.name: get_gallery_index}, {},
                          middleware=(BlogToolMiddleware,))
    session.run([HumanMessage(content="读取图库索引。")])
    message = next(message for message in prompts[-1] if isinstance(message, ToolMessage))
    return message


@pytest.mark.parametrize("count,character", [(30, '"'), (50, "图")])
def test_large_pages_survive_the_real_toolnode_with_matching_artifacts(index, count, character):
    index["rows"] = rows(count, character)
    message = dispatch({"limit": count})
    page = json.loads(message.content)
    assert message.status == "success" and message.artifact["data"] == page
    assert 0 < len(page["items"]) < count
    assert page["next_cursor"] == page["items"][-1]["image_id"]
    assert estimate_tokens([message]) <= settings.agent_tool_result_tokens
    assert len(index["requests"]) == 1


def test_local_shortening_keeps_continuation_even_when_the_backend_reached_its_end(index):
    index["rows"] = rows(47, "图")
    message = dispatch({"limit": 50})
    page = json.loads(message.content)
    assert len(page["items"]) < 47 and page["next_cursor"] is not None
    assert page["next_cursor"] > index["rows"][-1]["imageKey"]


def test_following_budgeted_cursors_returns_every_record_once(index):
    index["rows"] = rows(80, '"')
    found, before = [], None
    for _ in range(10):
        message = dispatch({"limit": 50, **({"before": before} if before else {})})
        page = json.loads(message.content)
        assert message.artifact["data"] == page
        found.extend(row["image_id"] for row in page["items"])
        before = page["next_cursor"]
        if before is None:
            break
    assert found == [row["imageKey"] for row in index["rows"]]
    assert len(found) == len(set(found)) and before is None


def test_small_metadata_can_return_all_fifty_records_in_one_call(index):
    index["rows"] = rows(50, "")
    message = dispatch({"limit": 50})
    page = json.loads(message.content)
    assert len(page["items"]) == 50 and page["next_cursor"] is None
    assert message.artifact["data"] == page


def test_empty_backend_is_empty_success_with_no_continuation(index):
    message = dispatch({"limit": 50})
    assert json.loads(message.content) == message.artifact["data"] == {
        "items": [], "next_cursor": None}


@pytest.mark.parametrize("count", [0, 1])
def test_too_little_budget_is_explicitly_unavailable_not_empty_success(index, monkeypatch, count):
    monkeypatch.setattr(settings, "agent_tool_result_tokens", 128)
    index["rows"] = rows(count, "图")
    message = dispatch({"limit": 50})
    assert message.status == "error" and message.artifact["kind"] == "unavailable"
    assert message.artifact["data"] is None and "读取预算" in message.content
    assert "next_cursor" not in message.content
