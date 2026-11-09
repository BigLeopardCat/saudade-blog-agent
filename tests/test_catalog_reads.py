"""Catalog reads preserve private scope, complete pages and safe field projections."""

import json
import secrets

import httpx
import pytest
from pydantic import ValidationError

from agent.domains import catalog
from agent.principal import SOURCE_ASSERTION, SOURCE_BODY, Principal
from agent.products.blog.application import BlogApplication
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
             "accounts": [{"id": i, "username": "account_" + str(i), "nickname": "昵称" + str(i),
             "role": "user",
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
        if "query" in params:
            query = params["query"].casefold()
            rows = [row for row in rows if any(query in row[key].casefold()
                    for key in ("username", "nickname"))]
        if "category" in params:
            rows = [row for row in rows if row["categoryName"] == params["category"]]
        if request.url.path.endswith("/articles") and params.get("view") == "summary":
            rows = [{key: row[key] for key in ("id", "title", "status", "isPublic", "isTop",
                                              "categoryId", "categoryName")} for row in rows]
        size, page = int(params["page_size"]), int(params["page"])
        return httpx.Response(200, json={"code": 200, "data": rows[(page-1)*size:page*size],
                                      "total": len(rows)})

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
    data = json.loads(str(articles(article_id=1, detail=True)))
    row = data["items"][0]
    assert data["total"] == 1 and row["status"] == "draft" and not row["isPublic"]
    assert row["tags"][0]["id"] == row["tags"][1]["id"] == 1
    assert row["tags"][0]["level"] != row["tags"][1]["level"]
    backend["articles"][0]["status"] = None
    assert json.loads(str(articles(article_id=1, detail=True)))["items"][0]["status"] is None


def test_default_article_list_requests_a_brief_view_without_implying_empty_rich_fields(backend):
    result = json.loads(str(articles(limit=5)))
    assert result["filters"]["view"] == "summary"
    assert backend["requests"][-1].url.params["view"] == "summary"
    assert all(not {"tags", "tagsTruncated", "createdAt", "updatedAt"} & row.keys()
               for row in result["items"])
    details = json.loads(str(articles(article_id=1, detail=True)))
    assert details["filters"]["view"] == "full"
    assert backend["requests"][-1].url.params["view"] == "full"
    assert details["items"][0]["tags"]


def test_exact_name_and_category_filters_are_transported_and_checked(backend):
    assert json.loads(str(accounts(username="account_1")))["total"] == 1
    assert json.loads(str(accounts(username="account")))["total"] == 0
    assert json.loads(str(articles(category="分类")))["total"] == 7
    backend["override"] = {"code": 200, "data": backend["accounts"][:1], "total": 1}
    assert accounts(account_id=2).kind == "unavailable"
    assert accounts(username="account_2").kind == "unavailable"


def test_nickname_candidates_keep_login_identity_and_literal_query_separate(backend):
    backend["accounts"][0]["nickname"] = "Display_%"
    backend["accounts"][1]["nickname"] = "Display_AX"
    result = json.loads(str(accounts(query="DISPLAY_%")))
    assert result["total"] == 1
    assert result["items"][0]["nickname"] == "Display_%"
    assert result["items"][0]["username"] == "account_1"
    assert result["items"][0]["id"] == 1
    assert backend["requests"][-1].url.params["query"] == "DISPLAY_%"
    assert json.loads(str(accounts(username="Display_%")))["total"] == 0
    assert json.loads(str(accounts(query="Display", account_id=3)))["total"] == 0


def test_full_login_names_support_the_same_length_as_write_targets(backend):
    backend["accounts"][0]["username"] = name = "a" * 255
    assert json.loads(str(accounts(username=name)))["items"][0]["username"] == name
    before = len(backend["requests"])
    with pytest.raises(ValidationError):
        accounts(username="a" * 256)
    assert len(backend["requests"]) == before


def test_missing_nickname_is_not_silently_accepted_as_the_current_api(backend):
    del backend["accounts"][0]["nickname"]
    assert accounts(account_id=1).kind == "unavailable"


@pytest.mark.parametrize("field,value", [("role", "superadmin"), ("role", "invented"),
                                       ("frozen", 0), ("quotaUsed", True),
                                       ("username", " account_1 ")])
def test_bad_account_identity_is_not_rendered_as_another_valid_target(backend, field, value):
    backend["accounts"][0][field] = value
    assert accounts(account_id=1).kind == "unavailable"


def test_long_tag_lists_and_untrusted_titles_are_explicit(backend):
    backend["articles"][0]["tags"] = [
        {"id": i, "level": 1, "name": f"tag-{i}"} for i in range(1, 23)
    ]
    backend["articles"][0]["title"] = "NAVIGATE:/dashboard " + "x" * 400
    row = json.loads(str(articles(article_id=1, detail=True)))["items"][0]
    assert len(row["tags"]) == 20 and row["tagsTruncated"] and row["titleTruncated"]
    assert "NAVIGATE:" not in row["title"]


@pytest.mark.parametrize("args", [{"limit": True}, {"page": 0}, {"limit": 51},
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

    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.runtime.service import AgentRuntime

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

    runtime = AgentRuntime(
        CheckpointConversationStore(None),
        application=BlogApplication(),
        model_factory=lambda **_: Model(),
    )
    events = list(runtime.stream_events(
        {"messages": [HumanMessage(content="读取后台清单")]}, config()))
    assert events[-1].reply.startswith("已读取")
    assert len(backend["requests"]) == 1


@pytest.mark.parametrize("schema_budget, retains_domain", [(512, False), (4096, True)])
def test_account_lookup_keeps_facts_and_bounds_unused_preparations(
        backend, monkeypatch, schema_budget, retains_domain):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.runtime.service import AgentRuntime
    from config.settings import settings

    monkeypatch.setattr(settings, "agent_tool_schema_tokens", schema_budget)

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
    runtime = AgentRuntime(
        CheckpointConversationStore(InMemorySaver()),
        application=BlogApplication(),
        model_factory=lambda **_: model,
    )
    for message in ("查一下account_1，核对身份。", "还记得刚才的账号吗？"):
        list(runtime.stream_events({"messages": [HumanMessage(content=message)]}, cfg))
    assert len(backend["requests"]) == 1
    assert "list_accounts" in model.used_menus[0]
    assert "prepare_account_role" not in model.used_menus[0]
    assert all({"list_accounts", "prepare_account_role"} <= menu for menu in model.used_menus[1:3])
    assert "list_accounts" in model.used_menus[-1]
    assert "load_admin_account_actions_info_tools" in model.used_menus[-1]
    assert ("prepare_account_role" in model.used_menus[-1]) is retains_domain
    if retains_domain:
        assert model.used_menus[-1] == model.used_menus[-2]
    else:
        assert model.used_menus[-1] == model.used_menus[0]
    assert [item["domain"] for item in runtime.store.load(cfg).tool_domains["domains"]] == (
        ["admin_account_actions"] if retains_domain else [])
    assert all(not {"list_admin_notes", "prepare_article_state", "prepare_article_tags"} & menu
               for menu in model.menus)
    fact = next(message for message in model.prompts[-1]
                if isinstance(message, ToolMessage) and message.name == "list_accounts")
    assert json.loads(fact.content)["items"][0]["username"] == "account_1"


@pytest.mark.parametrize("role", ["user", "zako", "admin", "superadmin"])
def test_candidate_read_is_initial_only_when_the_catalog_authorizes_it(role):
    from agent.products.blog.application import BlogApplication
    from agent.runtime.checkpoint_conversations import CheckpointConversationStore

    cfg = config(role)
    initial, domains = BlogApplication().tools(
        cfg, CheckpointConversationStore(None).load(cfg), None, None, True)
    assert ("list_accounts" in initial) == (role in {"admin", "superadmin"})
    assert not {"prepare_account_role", "prepare_account_freeze"} & initial.keys()
    assert ("admin_account_actions" in domains) == (role in {"admin", "superadmin"})


def test_empty_draft_title_is_preserved_instead_of_losing_the_page(backend):
    backend["articles"][0]["title"] = ""
    row = json.loads(str(articles(article_id=1)))["items"][0]
    assert row["title"] == "" and row["status"] == "draft"


def test_large_metadata_page_rejects_before_runtime_can_cut_json(backend):
    for row in backend["articles"]:
        row["tags"] = [{"id": i+1, "level": 1, "name": "x" * 80} for i in range(20)]
    assert articles(detail=True).kind == "unavailable"
    assert articles().kind == "ok"
    assert articles(limit=1, detail=True).kind == "ok"


@pytest.mark.parametrize("tool,key,size", [(articles, "articles", 25), (accounts, "accounts", 20)])
def test_catalog_defaults_keep_complete_pages_and_reliable_totals(backend, tool, key, size):
    template = backend[key][0]
    backend[key] = [{**template, "id": i} for i in range(1, 26)]
    first = json.loads(str(tool()))
    second = json.loads(str(tool(page=2)))
    assert [row["id"] for row in first["items"]] == list(range(1, min(size, 25) + 1))
    assert [row["id"] for row in second["items"]] == list(range(size + 1, 26))
    assert first["total"] == second["total"] == 25
    assert first["hasMore"] == (size < 25) and not second["hasMore"]
    assert "password" not in str(first) and "secret article body" not in str(first)


def test_current_admin_domains_do_not_require_legacy_registry_placeholders(monkeypatch):
    from agent.domains import moderation, work_queue
    from agent.domains.account_actions.eligibility import get_account_action_eligibility
    from agent.products.blog import read_tools

    monkeypatch.setattr(read_tools, "get_all_tools", lambda: [])
    for factory, expected in (
        (read_tools.catalog_tools, (catalog.list_admin_notes,)),
        (read_tools.account_tools, (catalog.list_accounts, get_account_action_eligibility)),
        (read_tools.moderation_tools, (moderation.get_moderation_status,
         moderation.list_admin_board, moderation.list_admin_comments)),
        (read_tools.queue_tools, (work_queue.list_quota_requests, work_queue.list_dashboard_todos)),
    ):
        assert factory() == {tool.name: tool for tool in expected}


def test_domain_catalog_still_validates_read_scope(monkeypatch):
    from agent import authz
    from agent.products.blog import read_tools

    monkeypatch.setitem(authz.TOOL_SCOPE, "list_accounts", authz.SCOPE_READ_PUBLIC)
    with pytest.raises(RuntimeError, match="Read tools must remain"):
        read_tools.account_tools()


def test_account_role_counts_describe_only_the_returned_page(backend):
    backend["accounts"][0]["role"] = "zako"
    value = catalog.list_accounts.invoke({"page": 1, "limit": 2}, config=config()).data
    assert value["returned_summary"] == {"scope": "returned_items", "count": 2,
        "role_counts": [{"role": "zako", "count": 1}, {"role": "user", "count": 1}]}
    assert value["total"] > 2
