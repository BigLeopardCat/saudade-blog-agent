"""Public metadata pages preserve totals, visibility and tag namespaces."""

import json

import httpx
import pytest
from pydantic import ValidationError

from agent.domains import public_catalog
from tools import base


@pytest.fixture
def backend(monkeypatch):
    state = {"requests": [], "articles": [{"id": i, "title": "article", "isPublic": True,
        "status": "published", "isTop": 1, "categoryId": 2, "categoryName": "分类",
        "tags": [], "content": "SECRET"} for i in range(1, 8)],
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

    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore

    def call(tool_name):
        return AIMessage(content="", tool_calls=[{"name": tool_name, "args": {}, "id": tool_name}])

    responses = iter([call("load_public_catalog_info_tools"), call(name),
                      AIMessage(content="已读取公开资料。")])

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt):
            return next(responses)

    runtime = NativeRuntime(NativeConversationStore(None), model_factory=lambda **_: Model())
    events = list(runtime.stream_events(
        {"messages": [HumanMessage(content="读取公开资料")]}, {}))
    assert events[-1].reply == "已读取公开资料。"
    assert len(backend["requests"]) == 1



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
