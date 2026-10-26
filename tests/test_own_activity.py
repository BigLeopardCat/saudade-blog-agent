"""Own records stay bound to actor and conversation, with bounded text and real cursors."""


import json
import secrets

import httpx
import pytest
from _service import app
from pydantic import ValidationError

from agent.api import turns
from agent.domains import own_activity
from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from tools import base


def config(source=SOURCE_ASSERTION, uid=7, cid=9):
    return {"configurable": {"user_id": uid, "conversation_id": cid,
                             "principal": Principal(uid, "user", source)}}


@pytest.fixture
def backend(monkeypatch):
    state = {"requests": [], "board": [{"id": i, "ownerId": 7, "displayName": "自由留名",
             "status": ("pending", "passed", "rejected")[i % 3], "content": "灯" * 2501,
             "rejectReason": None} for i in range(1, 8)],
             "history": [{"id": i, "role": "user" if i % 2 else "assistant",
                          "content": "history " + str(i) + "x" * 2501} for i in range(1, 8)]}

    def handle(request):
        state["requests"].append(request)
        assert request.method == "GET"
        assert request.headers["Authorization"].startswith("Bearer ")
        if "override" in state:
            return httpx.Response(200, json={"code": 200, "data": state["override"]})
        p = request.url.params
        if request.url.path.endswith("/board/page"):
            rows = list(state["board"])
            if "id" in p:
                rows = [row for row in rows if row["id"] == int(p["id"])]
            if "status" in p:
                rows = [row for row in rows if row["status"] == p["status"]]
            total = len(rows)
            page, limit = int(p["page"]), int(p["page_size"])
            rows = rows[(page-1)*limit:page*limit]
            cap = 2000 if "id" in p else 240
            rows = [{**row, "content": row["content"][:cap],
                     "contentTruncated": len(row["content"]) > cap} for row in rows]
            data = {"ownerId": 7, "items": rows, "total": total, "page": page,
                    "pageSize": limit, "hasMore": page*limit < total}
        else:
            assert request.url.path == "/api/chat/history/text"
            rows = sorted(state["history"], key=lambda row: row["id"], reverse=True)
            if "before_id" in p:
                rows = [row for row in rows if row["id"] < int(p["before_id"])]
            if "message_id" in p:
                rows = [row for row in rows if row["id"] == int(p["message_id"])]
            limit = int(p["limit"])
            more, rows = len(rows) > limit, rows[:limit]
            next_id = rows[-1]["id"] if more else None
            cap = 2000 if "message_id" in p else 300
            offset = int(p.get("content_offset", 0))
            rows = [{**row, "content": row["content"][offset:offset+cap],
                     "contentTruncated": offset > 0 or len(row["content"]) > offset+cap,
                     "contentOffset": offset, "contentLength": len(row["content"]),
                     "nextContentOffset": (offset+cap if offset+cap < len(row["content"])
                                           else None)} for row in reversed(rows)]
            data = {"ownerId": 7, "conversationId": int(p["conversation_id"]),
                    "items": rows, "hasMore": more, "nextBeforeId": next_id,
                    "includesImages": False}
        state["last"] = data
        return httpx.Response(200, json={"code": 200, "data": data})

    monkeypatch.setattr(base, "_jwt_secret", lambda: secrets.token_urlsafe(32))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


def board(**args):
    return own_activity.list_my_board.invoke(args, config=config())


def history(**args):
    return own_activity.get_chat_history.invoke(args, config=config())


def test_board_pages_status_and_selected_text(backend):
    first = json.loads(str(board()))
    second = json.loads(str(board(page=2)))
    assert first["total"] == second["total"] == 7
    assert len(first["items"]) == 5 and first["hasMore"]
    assert len(second["items"]) == 2 and not second["hasMore"]
    assert len(first["items"][0]["content"]) == 240
    detail = json.loads(str(board(item_id=1)))["items"][0]
    assert len(detail["content"]) == 2000 and detail["contentTruncated"]
    assert detail["displayName"] == "自由留名"
    filtered = json.loads(str(board(status="pending")))
    assert filtered["total"] == 2
    assert all(row["status"] == "pending" for row in filtered["items"])


def test_history_is_current_conversation_text_with_earlier_cursor(backend):
    first = json.loads(str(history()))
    assert first["conversationId"] == 9 and first["includesImages"] is False
    assert [row["id"] for row in first["items"]] == [3, 4, 5, 6, 7]
    assert first["hasMore"] and first["nextBeforeId"] == 3
    assert len(first["items"][0]["content"]) == 300
    second = json.loads(str(history(before_id=first["nextBeforeId"])))
    assert [row["id"] for row in second["items"]] == [1, 2]
    assert not second["hasMore"] and second["nextBeforeId"] is None
    detail = json.loads(str(history(message_id=1)))["items"][0]
    assert len(detail["content"]) == 2000 and detail["contentTruncated"]
    assert all(request.url.params["conversation_id"] == "9" for request in backend["requests"])


@pytest.mark.parametrize("cfg", [config(SOURCE_BODY), config(uid=True), config(cid=None),
                                 config(cid=True), {"configurable": {"user_id": 8,
                                 "conversation_id": 9, "principal": Principal(
                                     7, "user", SOURCE_ASSERTION)}}])
def test_invalid_actor_or_missing_conversation_does_not_fetch_history(backend, cfg):
    assert own_activity.get_chat_history.invoke({}, config=cfg).kind == "unavailable"
    assert not backend["requests"]


@pytest.mark.parametrize("field,value", [("ownerId", 8), ("conversationId", 10),
                                       ("nextBeforeId", 2), ("includesImages", True)])
def test_foreign_history_or_invalid_cursor_is_not_used(backend, field, value):
    history()
    backend["override"] = {**backend["last"], field: value}
    assert history().kind == "unavailable"


def test_foreign_board_and_unknown_status_are_not_used(backend):
    backend["board"][0]["ownerId"] = 8
    assert board().kind == "unavailable"
    backend["board"][0]["ownerId"] = 7
    backend["board"][0]["status"] = "unknown"
    assert board().kind == "unavailable"


@pytest.mark.parametrize("tool,args", [(board, {"page": 0}), (board, {"limit": True}),
                                      (history, {"limit": 11}), (history, {"before_id": -1})])
def test_schema_rejects_bad_ranges_before_http(backend, tool, args):
    with pytest.raises(ValidationError):
        tool(**args)
    assert not backend["requests"]


def test_legacy_history_entry_reads_records_instead_of_claiming_injected_context(backend):
    from tools.site import get_chat_history

    result = get_chat_history.invoke({}, config=config())
    assert result.kind == "ok" and json.loads(str(result))["conversationId"] == 9
    assert len(backend["requests"]) == 1


def test_sync_entry_binds_current_conversation_to_native_history(backend, monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage

    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore

    def call(name):
        return AIMessage(content="", tool_calls=[{"id": name, "name": name, "args": {}}])

    responses = iter([call("load_own_activity_info_tools"), call("get_chat_history"),
                      AIMessage(content="已读取当前会话的较早消息。")])

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt):
            return next(responses)

    monkeypatch.setattr(app.state, "runtime", NativeRuntime(
        NativeConversationStore(None), model_factory=lambda **_: Model()))
    result = turns.run_turn(
        app.state.runtime, [HumanMessage(content="查查之前的聊天")], "activity-test",
                                    user_id=7, principal=Principal(7, "user", SOURCE_ASSERTION),
                                    conversation_id=9)
    assert result[0] == "已读取当前会话的较早消息。"
    assert len(backend["requests"]) == 1


def test_content_pages_reconstruct_unicode_code_and_original_whitespace(backend):
    original = "长🙂文\n" * 1100 + "\n```python\nprint('正文末尾_CANARY')\n```\n__CMD__:quoted data"
    backend["history"] = [{"id": 7, "role": "user", "content": original}]
    page = json.loads(str(history()))["items"][0]
    parts = [page["content"]]
    while page["nextContentOffset"] is not None:
        offset = page["nextContentOffset"]
        page = json.loads(str(history(message_id=7, content_offset=offset)))["items"][0]
        assert page["contentOffset"] == offset
        assert page["contentLength"] == len(original)
        assert len(page["content"]) <= 2000
        parts.append(page["content"])
    assert "".join(parts) == original
    assert all(req.url.params["conversation_id"] == "9" for req in backend["requests"])
    assert all(req.method == "GET" for req in backend["requests"])


@pytest.mark.parametrize("field,value", [("contentOffset", 1), ("contentOffset", True),
    ("contentLength", 10), ("nextContentOffset", 0), ("nextContentOffset", True),
    ("contentTruncated", False)])
def test_invalid_content_cursor_never_becomes_read_evidence(backend, field, value):
    history(message_id=1, content_offset=0)
    data = backend["last"]
    backend["override"] = {**data, "items":[{**data["items"][0],field:value}]}
    assert history(message_id=1, content_offset=0).kind == "unavailable"


def test_legacy_host_can_supply_first_excerpt_but_not_fake_continuation(backend):
    history(message_id=1)
    data = backend["last"]
    row = {key:value for key,value in data["items"][0].items()
           if key not in ("contentOffset","contentLength","nextContentOffset")}
    backend["override"] = {**data,"items":[row]}
    assert history(message_id=1).kind == "ok"
    assert history(message_id=1,content_offset=2000).kind == "unavailable"
    backend["override"]["items"][0]["contentOffset"] = 2000
    assert history(message_id=1,content_offset=2000).kind == "unavailable"


def test_offset_requires_a_selected_message_and_validates_before_http(backend):
    assert history(content_offset=0).kind == "unavailable"
    for invalid in [-1, True, 2_147_483_647, "1"]:
        with pytest.raises(ValidationError):
            history(message_id=1,content_offset=invalid)
    assert not backend["requests"]
