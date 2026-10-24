"""Bounded notification reads preserve source links without changing read state."""

import json
import subprocess
import sys

import pytest
from _own_support import config
from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.read_tools import own_tools
from tools import base
from tools import notifications as reads


def rows(size):
    return [{"id": 1000 - index, "type": "comment_reply", "title": "评论回复",
             "content": "正文" * 1500, "link": f"/article/19?cid={1000-index}#reply",
             "isRead": index % 3 == 0, "createdAt": "2026-10-25 12:00:00"}
            for index in range(size)]


def read(**args):
    return reads.list_notifications.invoke(args, config=config())


def test_registered_default_read_bounds_contents_and_preserves_links(notifications):
    notifications["items"] = rows(100)
    assert own_tools()["list_notifications"] is base.list_notifications is reads.list_notifications
    result = json.loads(str(read()))
    assert len(result["items"]) == 5 and result["windowCount"] == 100
    assert result["windowComplete"] is False and result["hasMoreInWindow"] is True
    assert result["unreadInWindow"] == 66
    assert result["items"][0]["link"] == "/article/19?cid=1000#reply"
    assert result["items"][0]["contentTruncated"] is True
    assert len(result["items"][0]["content"]) == 240
    assert len(notifications["items"][0]["content"]) == 3000
    assert [request.method for request in notifications["requests"]] == ["GET"]


def test_page_size_one_can_reach_the_whole_window_and_unread_filter_is_local(notifications):
    notifications["items"] = rows(100)
    last = json.loads(str(read(page=100, limit=1)))
    assert last["items"][0]["id"] == 901 and last["hasMoreInWindow"] is False
    unread = json.loads(str(read(unread_only=True, page=2, limit=3)))
    expected = [row["id"] for row in notifications["items"] if not row["isRead"]][3:6]
    assert [row["id"] for row in unread["items"]] == expected
    assert unread["matchedInWindow"] == 66 and unread["windowComplete"] is False


def test_single_id_read_ignores_list_filters_and_never_marks_read(notifications):
    notifications["items"] = rows(5)
    before = [row["isRead"] for row in notifications["items"]]
    result = json.loads(str(read(notification_id=1000, unread_only=True, page=20)))
    assert len(result["items"]) == 1 and result["items"][0]["id"] == 1000
    assert len(result["items"][0]["content"]) == 2000
    assert result["items"][0]["contentTruncated"] is True
    assert result["windowComplete"] is True and "hasMoreInWindow" not in result
    assert [row["isRead"] for row in notifications["items"]] == before
    assert all(request.method == "GET" for request in notifications["requests"])


@pytest.mark.parametrize("size", [0, 3, 100])
def test_missing_id_reports_only_the_known_window(notifications, size):
    notifications["items"] = rows(size)
    result = json.loads(str(read(notification_id=1)))
    assert result["items"] == [] and result["windowComplete"] is (size < 100)


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 11}, {"limit": True},
                                  {"page": 101}, {"page": "2"}, {"notification_id": False},
                                  {"notification_id": -1}, {"unread_only": "false"}])
def test_invalid_bounds_fail_before_http(notifications, args):
    with pytest.raises(ValidationError):
        read(**args)
    assert not notifications["requests"]


@pytest.mark.parametrize("values", [
    {"user_id": 8}, {"user_id": None}, {"user_id": True}, {"principal": None},
    {"principal": Principal(7, "user", SOURCE_BODY)},
    {"principal": Principal(True, "user", SOURCE_ASSERTION)},
    {"principal": Principal(7, "zako", SOURCE_ASSERTION)},
    {"principal": Principal(7, "invented", SOURCE_ASSERTION)},
])
def test_unverified_mismatched_or_unprivileged_identity_never_reads(notifications, values):
    assert reads.list_notifications.invoke({}, config=config(**values)).kind == "unavailable"
    assert not notifications["requests"]


@pytest.mark.parametrize("data", [None, {}, {"items": [None], "unread": 0},
                                  {"items": rows(1) * 2, "unread": 0},
                                  {"items": rows(1), "unread": 1},
                                  {"items": rows(101), "unread": 67}])
def test_unverifiable_backend_data_is_not_an_empty_success(monkeypatch, data):
    monkeypatch.setattr(base, "_own_get", lambda *args: data)
    assert read().kind == "unavailable"


def test_network_failure_and_oversized_page_never_return_partial_json(notifications):
    notifications["unavailable"] = True
    assert read().kind == "unavailable"
    notifications["unavailable"] = False
    notifications["items"] = rows(10)
    for row in notifications["items"]:
        row["link"] = "/article/19?anchor=" + "x" * 980
    assert read(limit=10).kind == "unavailable"
    smaller = read(limit=2)
    assert smaller.kind == "ok" and len(str(smaller)) < 7500
    assert len(json.loads(str(smaller))["items"]) == 2


def test_native_read_can_navigate_to_the_exact_notification_reply(notifications):
    notifications["items"] = rows(1)
    requests = []

    def call(name, **args):
        return AIMessageChunk(content="", tool_calls=[{"id": name, "name": name, "args": args}])

    script = iter([call("load_notifications_info_tools"), call("list_notifications"),
                   None, AIMessageChunk(content="已发送跳转指令，等待浏览器回报。")])

    class Model:
        max_tokens = 64

        def bind_tools(self, tools, **kwargs):
            return self

        def stream(self, prompt, **kwargs):
            requests.append(list(prompt))
            response = next(script)
            if response is None:
                source = json.loads(prompt[-1].content)
                response = call("navigate_page", path=source["items"][0]["link"])
            yield response

    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()),
                            model_factory=lambda **kwargs: Model())
    events = list(runtime.stream_events({"messages": [HumanMessage(content="打开这条回复")]},
                                      config()))
    receipts = events[-1].execution["receipts"]
    assert len(receipts) == 1
    assert receipts[0]["cmd"]["url"].endswith("/article/19?cid=1000#reply")
    assert "尚未回传" in receipts[0]["result"]
    assert len(requests) == 4
    assert [request.method for request in notifications["requests"]] == ["GET"]


@pytest.mark.parametrize("first", ["agent.authz", "tools.notifications", "tools.base"])
def test_registry_import_order_resolves_to_one_tool(first):
    script = (f"import {first}\nfrom tools import base\n"
              "from tools.notifications import list_notifications\n"
              "from agent.runtime.read_tools import own_tools\n"
              "assert own_tools()['list_notifications'] is base.list_notifications "
              "is list_notifications\n")
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            timeout=20)
    assert result.returncode == 0, result.stderr
