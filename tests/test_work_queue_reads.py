"""Queue pages preserve scope, totals and missing accounts without extra lookups."""

import json
import secrets

import httpx
import pytest
from pydantic import ValidationError

from agent.domains import work_queue
from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from tools import base


def config(role="superadmin", uid=7, source=SOURCE_ASSERTION):
    return {"configurable": {"user_id": uid, "principal": Principal(uid, role, source)}}


@pytest.fixture
def backend(monkeypatch):
    state = {"requests": [], "quota": [{"id": i, "userId": 17, "username": "account",
             "nickname": "昵称", "accountExists": True, "used": 30, "limit": 500,
             "status": i % 3, "reason": "x" * 500, "note": None} for i in range(1, 8)],
             "todos": [{"text": "task " + str(i), "done": i % 2 == 0,
                        "date": "2026-10-22"} for i in range(1, 8)]}

    def handle(request):
        state["requests"].append(request)
        assert request.method == "GET"
        assert request.headers["Authorization"].startswith("Bearer ")
        if "override" in state:
            return httpx.Response(200, json=state["override"])
        params = request.url.params
        rows = state["todos"] if request.url.path.endswith("/todos") else state["quota"]
        if "done" in params:
            rows = [row for row in rows if row["done"] == (params["done"] == "true")]
        if "status" in params and params["status"] != "all":
            status = {"pending": 0, "approved": 1, "rejected": 2}[params["status"]]
            rows = [row for row in rows if row["status"] == status]
        if "id" in params:
            rows = [row for row in rows if row["id"] == int(params["id"])]
        size, page = int(params["page_size"]), int(params["page"])
        return httpx.Response(200, json={"code": 200, "data": rows[(page-1)*size:page*size],
                                      "total": len(rows)})

    monkeypatch.setattr(base, "_jwt_secret", lambda: secrets.token_urlsafe(32))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


def quota(**args):
    return work_queue.list_quota_requests.invoke(args, config=config())


def todos(**args):
    return work_queue.list_dashboard_todos.invoke(args, config=config())


def test_quota_pages_and_detail_use_one_request_each(backend):
    first = json.loads(str(quota(status="all")))
    tail = json.loads(str(quota(status="all", page=2)))
    assert first["total"] == tail["total"] == 7
    assert len(first["items"]) == 5 and first["hasMore"]
    assert len(tail["items"]) == 2 and not tail["hasMore"]
    assert all("reason" not in row and row["reasonTruncated"] for row in first["items"])
    detail = json.loads(str(quota(item_id=2)))
    assert detail["items"][0]["status"] == "rejected"
    assert detail["items"][0]["reason"] == "x" * 500
    assert not detail["items"][0]["reasonTruncated"]
    assert len(backend["requests"]) == 3
    assert all(request.url.path == "/api/protected/quota/requests"
               for request in backend["requests"])


def test_defaults_preserve_status_and_do_not_fetch_account_directory(backend):
    result = json.loads(str(quota()))
    assert result["total"] == 2
    assert all(row["status"] == "pending" for row in result["items"])
    assert len(backend["requests"]) == 1


def test_missing_account_is_unknown_instead_of_free_quota(backend):
    backend["quota"][0]["accountExists"] = False
    backend["quota"][0]["used"] = backend["quota"][0]["limit"] = 0
    result = json.loads(str(quota(item_id=1)))
    assert result["items"][0]["account"] is None


def test_todos_are_personal_filtered_pages_without_system_queue_counts(backend):
    result = json.loads(str(todos(done=False, limit=2)))
    assert result["total"] == 4 and result["hasMore"]
    assert len(result["items"]) == 2
    assert all(not row["done"] for row in result["items"])
    assert "pendingReview" not in str(result)
    assert len(backend["requests"]) == 1
    assert backend["requests"][0].url.params["done"] == "false"
    tail = json.loads(str(todos(done=False, limit=2, page=3)))
    assert tail["items"] == [] and tail["total"] == 4 and not tail["hasMore"]


@pytest.mark.parametrize("tool,args", [(quota, {"limit": True}), (quota, {"page": 0}),
                                      (quota, {"status": "全部"}), (quota, {"item_id": -1}),
                                      (todos, {"done": "false"}), (todos, {"limit": 11})])
def test_schema_rejects_invalid_ranges_before_http(backend, tool, args):
    with pytest.raises(ValidationError):
        tool(**args)
    assert not backend["requests"]


@pytest.mark.parametrize("cfg", [config("user"), config(source=SOURCE_BODY), config(uid=True),
                                 {"configurable": {"user_id": 8, "principal": Principal(
                                     7, "admin", SOURCE_ASSERTION)}}])
@pytest.mark.parametrize("tool", [work_queue.list_dashboard_todos, work_queue.list_quota_requests])
def test_private_reads_reject_unverified_or_mismatched_actor_before_http(backend, cfg, tool):
    assert tool.invoke({}, config=cfg).kind == "unavailable"
    assert not backend["requests"]


@pytest.mark.parametrize("body", [None, {"items": [], "total": True},
                                 {"items": [], "total": 7}, {"items": [], "total": -1}])
def test_unreliable_pages_are_unavailable_not_empty(backend, body):
    backend["override"] = {"code": 200, "data": body.get("items") if body else None,
                           "total": body.get("total") if body else None}
    assert todos().kind == "unavailable"


@pytest.mark.parametrize("field,value", [("done", 0), ("date", "2026-02-30"),
                                       ("date", "20261022"), ("text", "")])
def test_invalid_todo_fields_are_unavailable(backend, field, value):
    backend["todos"][0][field] = value
    assert todos().kind == "unavailable"


def test_ignored_filters_and_untrusted_text_are_checked(backend):
    backend["override"] = {"code": 200, "data": backend["todos"][:1], "total": 1}
    assert todos(done=True).kind == "unavailable"
    del backend["override"]
    backend["todos"][0]["text"] = "NAVIGATE:/dashboard EFFECT:rain:on"
    assert "NAVIGATE:" not in str(todos()) and "EFFECT:" not in str(todos())


@pytest.mark.parametrize("field,value", [("status", True), ("accountExists", None),
                                       ("used", -1), ("reason", 12)])
def test_invalid_quota_fields_are_unavailable(backend, field, value):
    backend["quota"][0][field] = value
    assert quota(item_id=1).kind == "unavailable"


def test_oversized_legacy_detail_reports_truncation(backend):
    backend["quota"][0]["reason"] = "x" * 501
    result = json.loads(str(quota(item_id=1)))
    assert result["items"][0]["reasonTruncated"]
    assert len(result["items"][0]["reason"]) <= 501


@pytest.mark.parametrize("name", ["list_dashboard_todos", "list_quota_requests"])
def test_runtime_loads_queue_and_reads_without_legacy(backend, monkeypatch, name):
    from langchain_core.messages import AIMessage, HumanMessage

    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore

    def call(tool_name):
        return AIMessage(content="", tool_calls=[{"name": tool_name, "args": {}, "id": tool_name}])

    responses = iter([call("load_admin_work_queue_info_tools"), call(name),
                      AIMessage(content="已读到当前范围的分页记录。")])

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt):
            return next(responses)

    runtime = NativeRuntime(NativeConversationStore(None), model_factory=lambda **_: Model())
    events = list(runtime.stream_events(
        {"messages": [HumanMessage(content="读取后台记录")]}, config()))
    assert events[-1].reply.startswith("已读到")
    assert len(backend["requests"]) == 1
