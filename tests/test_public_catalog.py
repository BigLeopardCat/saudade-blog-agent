"""Public metadata pages preserve totals, visibility and tag namespaces."""

import json

import httpx
import pytest
from pydantic import ValidationError

from agent.domains import public_catalog
from agent.products.blog.application import BlogApplication
from tools import base


@pytest.fixture
def backend(monkeypatch):
    state = {"requests": [], "articles": [{"id": i, "title": "article", "isPublic": True,
        "status": "published", "isTop": 1, "categoryId": 2, "categoryName": "分类",
        "tags": [], "content": "SECRET", "sourceUrl": f"https://canonical.example/article/{i}"}
        for i in range(1, 8)],
        "categories": [{"id": 2, "name": "分类", "color": None, "icon": None,
                        "pathName": "category", "publicArticleCount": 2}],
        "tags": [{"id": 1, "name": "一级", "level": 1, "parentId": None, "publicArticleCount": 2},
                 {"id": 1, "name": "二级", "level": 2, "parentId": 1, "publicArticleCount": 1}]}

    def handle(request):
        state["requests"].append(request)
        assert request.method == "GET"
        assert "Authorization" not in request.headers
        if "override" in state:
            return httpx.Response(200, json=state["override"])
        rows = state[request.url.path.rsplit("/", 1)[-1]]
        query = request.url.params
        size, page = int(query["page_size"]), int(query["page"])
        for query_key, key in [("level", "level"), ("parent_id", "parentId")]:
            if query_key in query:
                rows = [row for row in rows if row[key] == int(query[query_key])]
        return httpx.Response(200, json={"code": 200, "data": rows[(page-1)*size:page*size],
                                       "total": len(rows)})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


def test_public_pages_have_total_without_body_or_identity(backend):
    first = json.loads(str(public_catalog.list_notes.invoke({})))
    second = json.loads(str(public_catalog.list_notes.invoke({"page": 2})))
    assert first["total"] == second["total"] == 7
    assert len(first["items"]) == 5 and first["hasMore"]
    assert len(second["items"]) == 2 and not second["hasMore"]
    assert "SECRET" not in str(first)
    assert json.loads(str(public_catalog.get_top_notes.invoke({})))["items"][0]["isTop"] == 1
    assert backend["requests"][-1].url.params["top_only"] == "true"


@pytest.mark.parametrize("field,value", [("status", "draft"), ("isPublic", False), ("isPublic", 1)])
def test_non_public_results_are_not_exposed(backend, field, value):
    backend["articles"][0][field] = value
    assert public_catalog.list_notes.invoke({}).kind == "unavailable"


def test_missing_counts_are_not_zero_and_tag_ids_keep_levels(backend):
    data = json.loads(str(public_catalog.list_tags.invoke({})))
    assert [row["level"] for row in data["items"]] == [1, 2]
    assert [row["publicArticleCount"] for row in data["items"]] == [2, 1]
    assert json.loads(str(public_catalog.list_tags.invoke({"parent_id": 1})))["total"] == 1
    backend["categories"][0].pop("publicArticleCount")
    assert public_catalog.list_categories.invoke({}).kind == "unavailable"


def test_mismatched_filters_and_bad_totals_fail(backend):
    assert public_catalog.list_notes.invoke({"category_id": 99}).kind == "unavailable"
    backend["articles"][0]["isTop"] = 0
    assert public_catalog.get_top_notes.invoke({}).kind == "unavailable"
    backend["override"] = {"code": 200, "data": [], "total": 1}
    assert public_catalog.list_notes.invoke({}).kind == "unavailable"
    backend["override"] = {"code": 200, "data": []}
    assert public_catalog.list_notes.invoke({}).kind == "unavailable"


@pytest.mark.parametrize("args", [{"page": 0}, {"page_size": 11}, {"category_id": True}])
def test_arguments_reject_unbounded_or_coerced_values(backend, args):
    with pytest.raises(ValidationError):
        public_catalog.list_notes.invoke(args)
    assert not backend["requests"]


@pytest.mark.parametrize("name", ["list_notes", "get_top_notes", "list_categories", "list_tags"])
def test_runtime_loads_public_tools_without_identity_or_legacy(backend, monkeypatch, name):
    from langchain_core.messages import AIMessage, HumanMessage

    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.runtime.service import AgentRuntime

    def call(tool_name):
        return AIMessage(content="", tool_calls=[{"name": tool_name, "args": {}, "id": tool_name}])

    responses = iter([call("load_public_catalog_info_tools"), call(name),
                      AIMessage(content="已读取公开资料。")])

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
        {"messages": [HumanMessage(content="读取公开资料")]}, {}))
    assert events[-1].reply == "已读取公开资料。"
    assert len(backend["requests"]) == 1


@pytest.mark.parametrize("domain,name", [("admin_tags", "list_tags"),
                                        ("admin_categories", "list_categories")])
def test_administration_domain_can_read_without_a_second_loader(backend, domain, name):
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.principal import SOURCE_ASSERTION, Principal
    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.runtime.service import AgentRuntime

    def call(tool):
        return AIMessage(content="", tool_calls=[{"name": tool, "args": {}, "id": tool}])

    replies = iter([call(f"load_{domain}_info_tools"), call(name),
                    call("load_public_catalog_info_tools"), AIMessage(content="已读取目录。")])
    menus = []

    class Model:
        def bind_tools(self, tools, **kwargs):
            menus.append([tool["function"]["name"] for tool in tools])
            return self

        def invoke(self, prompt):
            return next(replies)

    runtime = AgentRuntime(
        CheckpointConversationStore(InMemorySaver()),
        application=BlogApplication(),
        model_factory=lambda **_: Model(),
    )
    cfg = {"configurable": {"principal": Principal(7, "admin", SOURCE_ASSERTION),
                            "user_id": 7, "conversation_id": 42, "thread_id": "conv_42"}}
    events = list(runtime.stream_events({"messages": [HumanMessage(content="查看目录")]}, cfg))
    assert events[-1].reply == "已读取目录。" and len(backend["requests"]) == 1
    assert name in menus[1] and menus[-1].count(name) == 1


def test_nonadmin_cannot_load_administration_domains():
    from agent.principal import SOURCE_ASSERTION, Principal
    from agent.products.blog.capabilities import ToolCatalog

    domains = ToolCatalog().domains({"configurable": {
        "principal": Principal(7, "user", SOURCE_ASSERTION), "user_id": 7,
        "conversation_id": 42}}, True)
    assert "admin_tags" not in domains and "admin_categories" not in domains
    assert "list_tags" in domains["public_catalog"]



def test_transport_rejects_oversized_raw_page_before_model_projection(backend):
    backend["override"] = {"code": 200, "total": 1, "data": [{
        **backend["articles"][0], "tags": [
            {"id": i + 1, "level": 1, "name": "x" * 80} for i in range(2000)]}]}
    assert public_catalog.list_notes.invoke({}).kind == "unavailable"


def test_database_tag_limit_propagates_truncation_and_rejects_fake_flag(backend):
    backend["articles"][0]["tagsTruncated"] = True
    result = public_catalog.list_notes.invoke({})
    assert json.loads(str(result))["items"][0]["tagsTruncated"] is True
    backend["articles"][0]["tagsTruncated"] = "false"
    assert public_catalog.list_notes.invoke({}).kind == "unavailable"


@pytest.mark.parametrize("name,key", [("list_categories", "categories"), ("list_tags", "tags")])
def test_small_metadata_catalogs_default_to_twenty_records(backend, name, key):
    template = backend[key][0]
    backend[key] = [{**template, "id": i} for i in range(1, 26)]
    tool = getattr(public_catalog, name)
    first = json.loads(str(tool.invoke({})))
    second = json.loads(str(tool.invoke({"page": 2})))
    assert len(first["items"]) == 20 and first["hasMore"]
    assert len(second["items"]) == 5 and not second["hasMore"]
    assert first["total"] == second["total"] == 25
    with pytest.raises(ValidationError):
        tool.invoke({"page_size": 21})
    assert len(backend["requests"]) == 2


@pytest.mark.parametrize("name", ["list_notes", "get_top_notes"])
def test_catalog_preserves_host_canonical_links_without_document_requests(backend, name):
    result = json.loads(str(getattr(public_catalog, name).invoke({"page_size": 3})))
    assert [item["sourceUrl"] for item in result["items"]] == [
        f"https://canonical.example/article/{identifier}" for identifier in range(1, 4)]
    assert len(backend["requests"]) == 1
    assert backend["requests"][0].url.path.endswith("/articles")


@pytest.mark.parametrize("value", [
    "javascript:alert(1)", "//elsewhere.invalid/article/1",
    "https://user:secret@canonical.example/article/1", "https://canonical.example/article/2",
    "https://canonical.example:wrong/article/1", "https://canonical.example/article/1?redirect=elsewhere",
    "https://canonical.example/article/1#section", "https://canonical.example/\narticle/1",
    "https://" + "x" * 2048 + "/article/1", True, {"url": "https://canonical.example/article/1"},
])
def test_invalid_or_mismatched_source_links_fail_closed_without_fallback_io(backend, value):
    backend["articles"][0]["sourceUrl"] = value
    assert public_catalog.list_notes.invoke({}).kind == "unavailable"
    assert len(backend["requests"]) == 1


@pytest.mark.parametrize("missing", [False, True])
def test_unknown_source_links_do_not_trigger_guessing_or_document_reads(backend, missing):
    row = backend["articles"][0]
    row["sourceUrl"] = None
    if missing:
        row.pop("sourceUrl")
    row["title"] = "https://untrusted.invalid/article/1"
    result = json.loads(str(public_catalog.list_notes.invoke({})))
    assert result["items"][0]["sourceUrl"] is None
    assert len(backend["requests"]) == 1


@pytest.mark.parametrize("canonical_reply", [True, False])
def test_real_runtime_binds_canonical_catalog_links_independently_of_api_transport_origin(
    backend, monkeypatch, canonical_reply,
):
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from agent.products.blog import articles
    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.runtime.service import AgentRuntime

    class Model:
        calls = 0
        menus = []

        def bind_tools(self, tools, **kwargs):
            self.menus.append([tool["function"]["name"] for tool in tools])
            return self

        def invoke(self, prompt):
            self.calls += 1
            if self.calls == 1:
                name, arguments = "load_public_catalog_info_tools", {}
            elif self.calls == 2:
                name, arguments = "list_notes", {"page_size": 3}
            else:
                assert self.calls == 3, "Catalog recommendation added an unnecessary model round"
                result = next(message for message in reversed(prompt)
                              if isinstance(message, ToolMessage))
                assert "https://canonical.example/article/1" in str(result.content)
                assert "SECRET" not in str(result.content)
                link = ("https://canonical.example/article/1" if canonical_reply
                        else "https://other.example/article/1")
                return AIMessage(content=f"推荐[article]({link})。")
            return AIMessage(content="", tool_calls=[{"name": name, "args": arguments, "id": name}])

    monkeypatch.setattr(articles, "SITE_BASE", "http://127.0.0.1:3000")
    model = Model()
    runtime = AgentRuntime(CheckpointConversationStore(None), application=BlogApplication(),
                           model_factory=lambda **_: model)
    events = list(runtime.stream_events(
        {"messages": [HumanMessage(content="推荐最新文章并附链接")]}, {}))
    if canonical_reply:
        assert events[-1].reply == "推荐[article](https://canonical.example/article/1)。"
    else:
        assert "无法核实" in events[-1].reply
        assert "other.example" not in events[-1].reply
    assert model.calls == 3
    assert all("get_article_detail" in menu for menu in model.menus)
    assert len(backend["requests"]) == 1
    assert backend["requests"][0].url.path.endswith("/articles")


def test_parent_name_is_one_exact_backend_request_without_listing_parents(backend):
    backend["override"] = {"code": 200, "data": [backend["tags"][1]], "total": 1,
                           "resolvedParentName": "一级"}
    data = public_catalog.list_tags.invoke({"parent_name": "一级", "level": 2}).data
    assert data["total"] == 1 and data["items"][0]["parentId"] == 1
    assert len(backend["requests"]) == 1
    assert dict(backend["requests"][0].url.params) == {
        "page": "1", "page_size": "20", "level": "2", "parent_name": "一级"}
    backend["override"] = {"code": 500, "data": None}
    assert public_catalog.list_tags.invoke({"parent_name": "ambiguous"}).kind == "unavailable"


@pytest.mark.parametrize("parent", [None, "other"])
def test_unverified_parent_name_cannot_become_a_scoped_result(backend, parent):
    backend["override"] = {"code": 200, "data": [backend["tags"][1]], "total": 1,
                           "resolvedParentName": parent}
    assert public_catalog.list_tags.invoke({"parent_name": "一级"}).kind == "unavailable"


@pytest.mark.parametrize("args", [
    {"parent_name": " ", "level": 2}, {"parent_name": "一级", "level": 1},
    {"parent_name": "一级", "parent_id": 1},
])
def test_invalid_parent_filters_do_not_issue_a_request(backend, args):
    assert public_catalog.list_tags.invoke(args).kind == "invalid_args"
    assert backend["requests"] == []


def test_empty_children_still_require_server_verified_parent_scope(backend):
    backend["override"] = {"code": 200, "data": [], "total": 0}
    assert public_catalog.list_tags.invoke({"parent_name": "一级"}).kind == "unavailable"
    backend["override"]["resolvedParentName"] = "一级"
    data = public_catalog.list_tags.invoke({"parent_name": "一级"}).data
    assert data["items"] == [] and data["total"] == 0
    assert data["resolvedParentName"] == "一级"
