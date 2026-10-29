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
    first = json.loads(str(tool(limit=5)))
    second = json.loads(str(tool(page=2, limit=5)))
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


@pytest.mark.parametrize("args", [{"limit": True}, {"page": 0}, {"limit": 21},
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

    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore

    def call(tool_name):
        return AIMessage(content="", tool_calls=[{"name": tool_name, "args": {}, "id": tool_name}])

    domain = "admin_account_actions" if name == "list_accounts" else "admin_catalog"
    responses = iter([call(f"load_{domain}_info_tools"), call(name),
                      AIMessage(content="已读取当前页面，尚有后续记录。")])

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt):
            return next(responses)

    runtime = NativeRuntime(NativeConversationStore(None), model_factory=lambda **_: Model())
    events = list(runtime.stream_events(
        {"messages": [HumanMessage(content="读取后台清单")]}, config()))
    assert events[-1].reply.startswith("已读取")
    assert len(backend["requests"]) == 1


def test_account_lookup_and_history_restore_do_not_load_article_preparations(backend):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore

    responses = iter([
        AIMessage(content="", tool_calls=[{
            "name": "load_admin_account_actions_info_tools", "args": {}, "id": "load"}]),
        AIMessage(content="", tool_calls=[{
            "name": "list_accounts", "args": {"username": "account_1"}, "id": "read"}]),
        AIMessage(content="账号已找到，当前是普通用户。"), AIMessage(content="我记得这个账号。"),
    ])

    class Model:
        def __init__(self):
            self.menus, self.used_menus, self.prompts = [], [], []

        def bind_tools(self, tools, **kwargs):
            self.menus.append({convert_to_openai_tool(t)["function"]["name"] for t in tools})
            return self

        def invoke(self, prompt):
            self.prompts.append(list(prompt))
            self.used_menus.append(self.menus[-1])
            return next(responses)

    cfg = {"configurable": {**config()["configurable"],
                            "conversation_id": 42, "thread_id": "conv_42"}}
    model = Model()
    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()),
                            model_factory=lambda **_: model)
    for message in ("查一下account_1，核对身份。", "还记得刚才的账号吗？"):
        list(runtime.stream_events({"messages": [HumanMessage(content=message)]}, cfg))
    assert len(backend["requests"]) == 1
    assert "list_accounts" not in model.used_menus[0]
    assert all({"list_accounts", "prepare_account_role"} <= menu for menu in model.used_menus[1:3])
    assert "list_accounts" not in model.used_menus[-1]
    assert all(not {"list_admin_notes", "prepare_article_state", "prepare_article_tags"} & menu
               for menu in model.menus)
    fact = next(message for message in model.prompts[-1] if isinstance(message, ToolMessage))
    assert json.loads(fact.content)["items"][0]["username"] == "account_1"


def test_empty_draft_title_is_preserved_instead_of_losing_the_page(backend):
    backend["articles"][0]["title"] = ""
    row = json.loads(str(articles(article_id=1)))["items"][0]
    assert row["title"] == "" and row["status"] == "draft"


def test_large_metadata_page_rejects_before_runtime_can_cut_json(backend):
    for row in backend["articles"]:
        row["tags"] = [{"id": i+1, "level": 1, "name": "x" * 80} for i in range(20)]
    assert articles().kind == "unavailable"
    assert articles(limit=1).kind == "ok"


@pytest.mark.parametrize("tool,key", [(articles, "articles"), (accounts, "accounts")])
def test_compact_catalog_default_reads_twenty_complete_records(backend, tool, key):
    template = backend[key][0]
    backend[key] = [{**template, "id": i} for i in range(1, 26)]
    first = json.loads(str(tool()))
    second = json.loads(str(tool(page=2)))
    assert [row["id"] for row in first["items"]] == list(range(1, 21))
    assert [row["id"] for row in second["items"]] == list(range(21, 26))
    assert first["total"] == second["total"] == 25
    assert first["hasMore"] and not second["hasMore"]
    assert "password" not in str(first) and "secret article body" not in str(first)
