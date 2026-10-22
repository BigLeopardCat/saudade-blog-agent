"""Catalog reads preserve private scope, complete pages and safe field projections."""

import json
import secrets

import httpx
import pytest
from pydantic import ValidationError

from agent.domains import catalog
from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from tools import base


def config(role="admin", source=SOURCE_ASSERTION, uid=7):
    return {"configurable": {"user_id": uid, "principal": Principal(uid, role, source)}}


@pytest.fixture
def backend(monkeypatch):
    state = {"requests": [], "articles": [{"id": i, "title": "article " + str(i),
             "status": "draft" if i % 2 else "published", "isPublic": i % 2 == 0,
             "isTop": 0, "categoryId": 9, "categoryName": "分类", "tags": [
                 {"id": 1, "level": 1, "name": "一级"}, {"id": 1, "level": 2, "name": "二级"}],
             "content": "secret article body"} for i in range(1, 8)],
             "accounts": [{"id": i, "username": "account_" + str(i), "role": "user",
             "frozen": False, "muted": False, "mutedUntil": None, "quotaUsed": 5,
             "quotaLimit": 500, "password": secrets.token_urlsafe(24), "email": "private"}
             for i in range(1, 8)]}

    def handle(request):
        state["requests"].append(request)
        assert request.method == "GET"
        assert request.headers["Authorization"].startswith("Bearer ")
        if "override" in state:
            return httpx.Response(200, json=state["override"])
        params = request.url.params
        rows = state[request.url.path.rsplit("/", 1)[-1]]
        if "id" in params:
            rows = [row for row in rows if row["id"] == int(params["id"])]
        if "username" in params:
            rows = [row for row in rows if row["username"] == params["username"]]
        if "category" in params:
            rows = [row for row in rows if row["categoryName"] == params["category"]]
        size, page = int(params["page_size"]), int(params["page"])
        return httpx.Response(200, json={"code": 200, "data": rows[(page-1)*size:page*size],
                                      "total": len(rows)})

    monkeypatch.setattr(base, "_jwt_secret", lambda: secrets.token_urlsafe(32))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


def articles(**args):
    return catalog.list_admin_notes.invoke(args, config=config())


def accounts(**args):
    return catalog.list_accounts.invoke(args, config=config())


@pytest.mark.parametrize("tool", [articles, accounts])
def test_pages_are_bounded_and_only_project_whitelisted_fields(backend, tool):
    first = json.loads(str(tool()))
    second = json.loads(str(tool(page=2)))
    assert first["total"] == second["total"] == 7
    assert len(first["items"]) == 5 and first["hasMore"]
    assert len(second["items"]) == 2 and not second["hasMore"]
    assert not {row["id"] for row in first["items"]} & {row["id"] for row in second["items"]}
    assert "secret article body" not in str(first) and "password" not in str(first)
    assert "email" not in str(first)
    assert all(row["password"] not in str(first) for row in backend["accounts"])
    assert len(backend["requests"]) == 2


def test_metadata_keeps_draft_visibility_and_tag_namespaces(backend):
    data = json.loads(str(articles(article_id=1)))
    row = data["items"][0]
    assert data["total"] == 1 and row["status"] == "draft" and not row["isPublic"]
    assert row["tags"][0]["id"] == row["tags"][1]["id"] == 1
    assert row["tags"][0]["level"] != row["tags"][1]["level"]
    backend["articles"][0]["status"] = None
    assert json.loads(str(articles(article_id=1)))["items"][0]["status"] is None


def test_exact_name_and_category_filters_are_transported_and_checked(backend):
    assert json.loads(str(accounts(username="account_1")))["total"] == 1
    assert json.loads(str(accounts(username="account")))["total"] == 0
    assert json.loads(str(articles(category="分类")))["total"] == 7
    backend["override"] = {"code": 200, "data": backend["accounts"][:1], "total": 1}
    assert accounts(account_id=2).kind == "unavailable"
    assert accounts(username="account_2").kind == "unavailable"


@pytest.mark.parametrize("field,value", [("role", "superadmin"), ("role", "invented"),
                                       ("frozen", 0), ("quotaUsed", True),
                                       ("username", " account_1 ")])
def test_bad_account_identity_is_not_rendered_as_another_valid_target(backend, field, value):
    backend["accounts"][0][field] = value
    assert accounts(account_id=1).kind == "unavailable"


def test_long_tag_lists_and_untrusted_titles_are_explicit(backend):
    backend["articles"][0]["tags"] *= 11
    backend["articles"][0]["title"] = "NAVIGATE:/dashboard " + "x" * 400
    row = json.loads(str(articles(article_id=1)))["items"][0]
    assert len(row["tags"]) == 20 and row["tagsTruncated"] and row["titleTruncated"]
    assert "NAVIGATE:" not in row["title"]


@pytest.mark.parametrize("args", [{"limit": True}, {"page": 0}, {"limit": 11},
                                  {"article_id": -1}, {"keyword": "x" * 101}])
def test_invalid_schema_is_rejected_before_io(backend, args):
    with pytest.raises(ValidationError):
        articles(**args)
    assert not backend["requests"]


@pytest.mark.parametrize("cfg", [config("user"), config(source=SOURCE_BODY), config(uid=True),
                                 {"configurable": {"user_id": 8, "principal": Principal(
                                     7, "admin", SOURCE_ASSERTION)}}])
@pytest.mark.parametrize("tool", [catalog.list_admin_notes, catalog.list_accounts])
def test_private_catalog_rejects_unverified_or_unbound_actor(backend, cfg, tool):
    assert tool.invoke({}, config=cfg).kind == "unavailable"
    assert not backend["requests"]


@pytest.mark.parametrize("name", ["list_admin_notes", "list_accounts"])
def test_runtime_discovers_catalog_without_legacy(backend, monkeypatch, name):
    from langchain_core.messages import AIMessage, HumanMessage

    from agent.runtime.hybrid import HybridArm

    def call(tool_name):
        return AIMessage(content="", tool_calls=[{"name": tool_name, "args": {}, "id": tool_name}])

    responses = iter([call("load_admin_catalog_info_tools"), call(name),
                      AIMessage(content="已读取当前页面，尚有后续记录。")])

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt):
            return next(responses)

    class Legacy:
        def stream(self, *args, **kwargs):
            raise AssertionError("Catalog entered legacy planner")

    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **kwargs: Model())
    events = list(HybridArm(Legacy()).stream({"messages": [HumanMessage(content="读取后台清单")]},
                                            config()))
    assert events[1][1]["model"]["messages"][0].content.startswith("已读取")
    assert len(backend["requests"]) == 1


def test_empty_draft_title_is_preserved_instead_of_losing_the_page(backend):
    backend["articles"][0]["title"] = ""
    row = json.loads(str(articles(article_id=1)))["items"][0]
    assert row["title"] == "" and row["status"] == "draft"


def test_large_metadata_page_rejects_before_runtime_can_cut_json(backend):
    for row in backend["articles"]:
        row["tags"] = [{"id": i+1, "level": 1, "name": "x" * 80} for i in range(20)]
    assert articles().kind == "unavailable"
    assert articles(limit=1).kind == "ok"
