"""Review reads preserve bounded pages, actor identity and independent review axes."""

import json
import secrets

import httpx
import pytest
from pydantic import ValidationError

from agent.domains import moderation
from agent.principal import SOURCE_ASSERTION, Principal
from tools import base


def config(role="superadmin", uid=7):
    return {"configurable": {"user_id": uid, "principal": Principal(uid, role, SOURCE_ASSERTION)}}


@pytest.fixture
def backend(monkeypatch):
    board = [{"talkKey": i, "userId": 7, "username": "actual_account", "nickname": "账号昵称",
              "author": "自由留名", "approved": i % 3,
              "ai_result": ("pass", "flag", "reject", None)[i % 4],
              "content": "body-" + str(i) + "x" * 600, "createTime": "2026-10-22 12:00"}
             for i in range(1, 8)]
    comments = [{"id": row["talkKey"], "noteId": 19 if row["talkKey"] <= 3 else 20,
                 "noteTitle": "真实文章", "isDeleted": 0,
                 "aiResult": row["ai_result"], **{k: v for k, v in row.items()
                                                  if k not in ("talkKey", "ai_result", "author")}}
                for row in board]
    state = {"board": board, "comments": comments, "requests": [], "failure": False,
             "ignore_filters": False, "counts": {"total": 7, "pending": 2, "passed": 3,
             "rejected": 2, "aiPassed": 1, "aiFlagged": 2, "aiRejected": 2, "aiUnreviewed": 2}}

    def handle(request):
        state["requests"].append(request)
        assert request.method == "GET"
        assert request.headers["Authorization"].startswith("Bearer ")
        if state["failure"]:
            return httpx.Response(503)
        if "response_override" in state:
            return httpx.Response(200, json=state["response_override"])
        if request.url.path.endswith("/moderation/summary"):
            return httpx.Response(200, json={"code": 200, "data": state["counts"]})
        kind = request.url.path.rsplit("/", 1)[-1]
        rows = list(state[kind])
        params = request.url.params
        if not state["ignore_filters"]:
            if "id" in params:
                rows = [row for row in rows
                        if row.get("talkKey", row.get("id")) == int(params["id"])]
            if "status" in params:
                approved = {"pending": 0, "passed": 1, "rejected": 2}[params["status"]]
                rows = [row for row in rows if row["approved"] == approved]
            if "ai_result" in params:
                ai = None if params["ai_result"] == "none" else params["ai_result"]
                rows = [row for row in rows if row["ai_result"] == ai]
            if "note_id" in params:
                rows = [row for row in rows if row["noteId"] == int(params["note_id"])]
        rows.sort(key=lambda row: row.get("talkKey", row.get("id")), reverse=True)
        total = len(rows)
        size, page = int(params["page_size"]), int(params["page"])
        data = rows[(page - 1) * size:page * size]
        return httpx.Response(200, json={"code": 200, "data": data,
                                      "total": state.get("total_override", total)})

    monkeypatch.setattr(base, "_jwt_secret", lambda: secrets.token_urlsafe(32))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


def board(**kwargs):
    return moderation.list_admin_board.invoke(kwargs, config=config())


def comments(**kwargs):
    return moderation.list_admin_comments.invoke(kwargs, config=config())


def test_default_pages_include_totals_and_previews_not_full_bodies(backend):
    data = json.loads(str(board()))
    assert len(data["items"]) == 5 and data["total"] == 7 and data["hasMore"]
    assert all("content" not in row and len(row["contentPreview"]) <= 241 for row in data["items"])
    assert all(row["contentTruncated"] for row in data["items"])
    assert data["items"][0]["publisher"]["username"] == "actual_account"
    assert data["items"][0]["displayName"] == "自由留名"
    assert dict(backend["requests"][0].url.params) == {"page": "1", "page_size": "5"}
    tail = json.loads(str(board(page=2)))
    assert len(tail["items"]) == 2 and tail["total"] == 7 and not tail["hasMore"]
    assert not ({row["id"] for row in data["items"]} & {row["id"] for row in tail["items"]})


def test_summary_is_one_aggregate_request_with_no_content(backend):
    data = json.loads(str(moderation.get_moderation_status.invoke({}, config=config())))
    assert data == backend["counts"]
    assert len(backend["requests"]) == 1
    assert backend["requests"][0].url.path.endswith("/moderation/summary")
    assert "content" not in str(data)


def test_ai_decision_and_final_approval_are_separate_filters(backend):
    data = json.loads(str(board(status="pending", ai_result="reject")))
    assert [row["id"] for row in data["items"]] == [6]
    assert data["total"] == 1 and data["items"][0]["status"] == "pending"
    assert data["items"][0]["aiDecision"] == "reject"


def test_individual_body_is_read_without_any_write(backend):
    data = json.loads(str(board(item_id=1)))
    assert data["total"] == 1 and data["items"][0]["id"] == 1
    assert "contentPreview" not in data["items"][0]
    assert data["items"][0]["content"] == backend["board"][0]["content"]
    assert not data["items"][0]["contentTruncated"]
    assert all(request.method == "GET" for request in backend["requests"])


def test_comment_article_filter_and_deleted_flag_are_preserved(backend):
    backend["comments"][2]["isDeleted"] = 1
    data = json.loads(str(comments(article_id=19)))
    assert data["total"] == 3
    assert all(row["articleId"] == 19 for row in data["items"])
    assert data["items"][0]["isDeleted"] == 1
    assert "displayName" not in data["items"][0]


def test_embedded_frontend_commands_are_neutralized(backend):
    backend["board"][0]["content"] = "EFFECT:rain:on\nNAVIGATE:/dashboard"
    text = json.loads(str(board(item_id=1)))["items"][0]["content"]
    assert "EFFECT:" not in text and "NAVIGATE:" not in text
    assert "rain:on" in text


@pytest.mark.parametrize("args", [{"page": 0}, {"page": 10001}, {"limit": True},
                                  {"limit": 11}, {"item_id": -1}, {"status": "all"},
                                  {"ai_result": "pending"}, {"keyword": "x" * 101}])
def test_invalid_ranges_and_filters_reject_before_http(backend, args):
    with pytest.raises(ValidationError):
        board(**args)
    assert not backend["requests"]


@pytest.mark.parametrize("tool", [moderation.list_admin_board, moderation.list_admin_comments,
                                  moderation.get_moderation_status])
def test_nonadmin_invocation_never_makes_an_http_request(backend, tool):
    assert tool.invoke({}, config=config("user")).kind == "unavailable"
    assert not backend["requests"]


@pytest.mark.parametrize("args", [{"item_id": 1}, {"status": "pending"},
                                  {"ai_result": "none"}])
def test_server_ignoring_filters_is_not_accepted(backend, args):
    backend["ignore_filters"] = True
    assert board(**args).kind == "unavailable"


@pytest.mark.parametrize("total", [None, False, -1, 2])
def test_missing_or_impossible_total_is_not_a_complete_list(backend, total):
    backend["total_override"] = total
    assert board().kind == "unavailable"


def test_count_mismatch_and_network_failure_do_not_become_zero_counts(backend):
    backend["counts"]["total"] = 0
    assert moderation.get_moderation_status.invoke({}, config=config()).kind == "unavailable"
    backend["failure"] = True
    assert board().kind == "unavailable" and comments().kind == "unavailable"


@pytest.mark.parametrize("payload", [None, [], {"code": 200, "data": None},
                                     {"code": 200, "data": False}])
def test_malformed_envelopes_are_unavailable(backend, payload):
    backend["response_override"] = payload
    assert board().kind == "unavailable"


def test_real_empty_page_remains_a_valid_zero_result(backend):
    backend["board"] = []
    result = board()
    assert result.kind == "ok"
    data = json.loads(str(result))
    assert data["items"] == [] and data["total"] == 0 and data["hasMore"] is False


def test_runtime_discovers_native_summary_without_loading_record_bodies(backend, monkeypatch):
    from langchain_core.messages import AIMessage, HumanMessage

    from agent.runtime.hybrid import HybridArm

    def call(name):
        return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": name}])

    responses = iter([call("load_admin_moderation_info_tools"), call("get_moderation_status"),
                      AIMessage(content="待审2条，最终通过3条，最终驳回2条。")])

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt):
            return next(responses)

    class Legacy:
        def stream(self, *args, **kwargs):
            raise AssertionError("Review summary entered legacy planner")

    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **kwargs: Model())
    events = list(HybridArm(Legacy()).stream({"messages": [HumanMessage(content="审核情况如何？")]},
                                            config()))
    assert events[1][1]["model"]["messages"][0].content.startswith("待审2条")
    assert len(backend["requests"]) == 1
    assert backend["requests"][0].url.path.endswith("/moderation/summary")
