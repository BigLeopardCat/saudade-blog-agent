"""Public search preserves full-source coverage, query scope and verified pagination."""

import json

import httpx
import pytest
from pydantic import ValidationError

from agent.domains.activity_search import search_public_activity
from tools import base


@pytest.fixture
def backend(monkeypatch):
    state = {"requests": [], "code": 200, "data": {
        "keywords": ["OTA"], "keywordMode": "any",
        "keywordCounts": [{"keyword": "OTA", "total": 1,
                           "counts": {"announcements": 0, "board": 1, "talk": 0}}],
        "matchMode": "literal_case_insensitive",
        "sources": ["announcements", "board", "talk"],
        "counts": {"announcements": 0, "board": 1, "talk": 0}, "total": 1,
        "page": 1, "pageSize": 5, "hasMore": False,
        "items": [{"id": 9, "source": "board", "title": "", "titleTruncated": False,
                   "content": "OTA upgrade", "contentTruncated": True, "createdAt": None,
                   "matchedKeywords": ["OTA"],
                   "approved": 1, "displayName": "free name", "displayNameTruncated": False,
                   "userId": 123, "rejectReason": "private"}]}}

    def handle(request):
        state["requests"].append(request)
        assert request.method == "POST" and "authorization" not in request.headers
        assert "x-user-assertion" not in request.headers
        return httpx.Response(200, json={"code": state["code"], "data": state["data"]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


def test_one_search_preserves_coverage_and_drops_private_metadata(backend):
    result = search_public_activity.invoke({"keywords": [" OTA "]})
    assert result.kind == "ok" and result.data["total"] == 1
    assert result.data["sources"] == ("announcements", "board", "talk")
    assert "userId" not in str(result) and "rejectReason" not in str(result)
    assert result.data["items"][0]["contentTruncated"]
    assert len(backend["requests"]) == 1
    request = backend["requests"][0]
    assert request.url.path.endswith('/activity/search')
    assert dict(request.url.params) == {}
    assert json.loads(request.content) == {"keywords": ["OTA"], "page": 1, "page_size": 5}
    assert json.loads(str(result))["counts"] == {"announcements": 0, "board": 1, "talk": 0}


@pytest.mark.parametrize("field,value", [
    ("counts", {"board": 1}), ("total", 2), ("page", 2), ("pageSize", 3),
    ("hasMore", True), ("keywords", ["other"]), ("keywordMode", "all"),
    ("matchMode", "semantic"),
    ("sources", ["board"]), ("items", []), ("total", True),
])
def test_unknown_scope_or_inconsistent_totals_never_mean_no_match(backend, field, value):
    backend["data"][field] = value
    assert search_public_activity.invoke({"keywords": ["OTA"]}).kind == "unavailable"


@pytest.mark.parametrize("field,value", [("approved", 0), ("approved", True),
                                        ("source", "private"), ("displayName", None),
                                        ("contentTruncated", None)])
def test_unverified_visibility_is_rejected(backend, field, value):
    backend["data"]["items"][0][field] = value
    assert search_public_activity.invoke({"keywords": ["OTA"]}).kind == "unavailable"


def test_valid_empty_and_out_of_range_pages_retain_source_totals(backend):
    backend["data"].update(items=[], page=2)
    assert search_public_activity.invoke({"keywords": ["OTA"], "page": 2}).data["total"] == 1
    backend["data"].update(page=1, total=0, counts={"announcements": 0, "board": 0, "talk": 0})
    _batch(backend, ["OTA"], [0])
    assert search_public_activity.invoke({"keywords": ["OTA"]}).data["total"] == 0
    backend["code"] = 500
    assert search_public_activity.invoke({"keywords": ["OTA"]}).kind == "unavailable"


@pytest.mark.parametrize("args", [
    {"keyword": "OTA"}, {"keywords": []}, {"keywords": "OTA"}, {"keywords": [""]},
    {"keywords": ["a\0b"]}, {"keywords": ["x" * 129]}, {"keywords": [7]},
    {"keywords": [True]}, {"keywords": ["x"] * 9}, {"keywords": ["x"], "limit": 11},
])
def test_invalid_parameters_cannot_reach_io(backend, args):
    with pytest.raises(ValidationError):
        search_public_activity.invoke(args)
    assert backend["requests"] == []


def test_multi_source_golden_accepts_aggregation_only_with_full_source_evidence():
    from eval import native_assertions, online_baseline
    case = next(case for case in online_baseline.load_suite() if case["id"] == "three_sources")
    gold = {"require_observations": case["gold"]["require_observations"]}
    event = {"node": "native", "event": "call", "name": "search_public_activity", "kind": "ok"}
    assert native_assertions.check(gold, {"native_events": [event]})
    complete = {**event, "result": json.dumps({"sources": ["announcements", "board", "talk"],
                                             "matchMode": "literal_case_insensitive"})}
    assert not native_assertions.check(gold, {"native_events": [complete]})
    old = [{**event, "name": name}
           for name in ("list_guestbook", "list_talks", "get_announcements")]
    assert not native_assertions.check(gold, {"native_events": old})
    assert native_assertions.check(gold, {"native_events": old[:2]})


def _batch(backend, words, totals):
    backend["data"]["keywords"] = words
    backend["data"]["keywordCounts"] = [
        {"keyword": word, "total": count,
         "counts": {"announcements": 0, "board": count, "talk": 0}}
        for word, count in zip(words, totals, strict=True)]


def test_expanded_keywords_use_one_post_and_preserve_overlapping_union(backend):
    words = ["OTA", "upgrade", "固件", "升级"]
    _batch(backend, words, [1, 1, 0, 0])
    backend["data"]["items"][0]["matchedKeywords"] = words[:2]
    result = search_public_activity.invoke({"keywords": words})
    assert result.kind == "ok" and result.data["total"] == 1
    assert sum(row["total"] for row in result.data["keywordCounts"]) == 2
    assert result.data["items"][0]["matchedKeywords"] == words[:2]
    assert len(backend["requests"]) == 1
    assert json.loads(backend["requests"][0].content)["keywords"] == words


def test_trimmed_case_insensitive_duplicates_keep_first_spelling_and_order(backend):
    _batch(backend, ["OTA", "升级"], [1, 0])
    result = search_public_activity.invoke({"keywords": [" OTA ", "升级", "ota", "升级"]})
    assert result.kind == "ok" and len(backend["requests"]) == 1
    assert json.loads(backend["requests"][0].content)["keywords"] == ["OTA", "升级"]


@pytest.mark.parametrize("matched", [[], ["unknown"], ["OTA", "OTA"], [True], "OTA"])
def test_hit_keywords_must_be_nonempty_unique_and_in_scope(backend, matched):
    backend["data"]["items"][0]["matchedKeywords"] = matched
    assert search_public_activity.invoke({"keywords": ["OTA"]}).kind == "unavailable"


@pytest.mark.parametrize("counts", [
    [], [{"keyword": "wrong", "total": 1,
          "counts": {"announcements": 0, "board": 1, "talk": 0}}],
    [{"keyword": "OTA", "total": 0,
      "counts": {"announcements": 0, "board": 1, "talk": 0}}],
    [{"keyword": "OTA", "total": 2,
      "counts": {"announcements": 0, "board": 2, "talk": 0}}],
    [{"keyword": "OTA", "total": 0,
      "counts": {"announcements": 0, "board": 0, "talk": 0}}],
])
def test_keyword_totals_cannot_conflict_with_union_or_page_evidence(backend, counts):
    backend["data"]["keywordCounts"] = counts
    assert search_public_activity.invoke({"keywords": ["OTA"]}).kind == "unavailable"


def test_batch_count_order_and_match_order_are_verified(backend):
    _batch(backend, ["OTA", "upgrade"], [1, 1])
    backend["data"]["items"][0]["matchedKeywords"] = ["upgrade", "OTA"]
    assert search_public_activity.invoke({"keywords": ["OTA", "upgrade"]}).kind == "unavailable"
    backend["data"]["items"][0]["matchedKeywords"] = ["OTA", "upgrade"]
    backend["data"]["keywordCounts"].reverse()
    assert search_public_activity.invoke({"keywords": ["OTA", "upgrade"]}).kind == "unavailable"


def test_duplicate_records_are_not_counted_as_separate_batch_results(backend):
    row = backend["data"]["items"][0]
    backend["data"].update(total=2, counts={"announcements": 0, "board": 2, "talk": 0},
                           items=[row, dict(row)])
    _batch(backend, ["OTA"], [2])
    assert search_public_activity.invoke({"keywords": ["OTA"]}).kind == "unavailable"


def test_zero_hit_keywords_and_unavailable_sources_are_distinct(backend):
    _batch(backend, ["OTA", "absent"], [1, 0])
    result = search_public_activity.invoke({"keywords": ["OTA", "absent"]})
    assert result.kind == "ok" and result.data["keywordCounts"][1]["total"] == 0
    backend["code"] = 503
    assert search_public_activity.invoke({"keywords": ["OTA", "absent"]}).kind == "unavailable"


def test_whitespace_only_keywords_do_not_reach_io(backend):
    assert search_public_activity.invoke({"keywords": [" "]}).kind == "unavailable"
    assert not backend["requests"]


def test_result_size_uses_utf8_bytes_and_never_truncates_json(backend):
    words = [chr(0x4E00 + index) * 128 for index in range(8)]
    _batch(backend, words, [1] * 8)
    backend["data"]["items"][0]["matchedKeywords"] = words
    assert search_public_activity.invoke({"keywords": words}).kind == "unavailable"
    assert len(backend["requests"]) == 1


def test_runtime_loads_the_public_domain_and_executes_one_batch_request(backend):
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from agent.principal import SOURCE_ASSERTION, Principal
    from agent.products.blog.application import BlogApplication
    from agent.runtime.checkpoint_conversations import CheckpointConversationStore
    from agent.runtime.service import AgentRuntime

    words = ["OTA", "upgrade", "固件", "升级"]
    _batch(backend, words, [1, 1, 0, 0])
    backend["data"]["items"][0]["matchedKeywords"] = words[:2]
    replies = iter([
        AIMessage(content="", tool_calls=[{"name": "load_public_activity_info_tools", "args": {},
                                          "id": "load"}]),
        AIMessage(content="", tool_calls=[{"name": "search_public_activity",
                                          "args": {"keywords": words}, "id": "search"}]),
        AIMessage(content="同一条公开留言提到了 OTA 和 upgrade，去重后一条。"),
    ])
    menus = []

    class Model:
        def bind_tools(self, tools, **kwargs):
            menus.append([tool["function"]["name"] for tool in tools])
            return self

        def invoke(self, prompt):
            return next(replies)

    runtime = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
                           application=BlogApplication(), model_factory=lambda **_: Model())
    config = {"configurable": {"principal": Principal(7, "user", SOURCE_ASSERTION),
                               "user_id": 7, "conversation_id": 42, "thread_id": "conv_42"}}
    request = {"messages": [HumanMessage(content="检索相关公开动态")]}
    events = list(runtime.stream_events(request, config))
    assert events[-1].reply == "同一条公开留言提到了 OTA 和 upgrade，去重后一条。"
    assert "search_public_activity" in menus[1]
    assert len(backend["requests"]) == 1
    assert json.loads(backend["requests"][0].content)["keywords"] == words


def test_same_numeric_id_in_different_sources_is_not_a_duplicate(backend):
    announcement = {"id": 9, "source": "announcements", "title": "T", "titleTruncated": False,
                    "content": "OTA", "contentTruncated": False, "createdAt": None,
                    "matchedKeywords": ["OTA"]}
    backend["data"]["items"].append(announcement)
    counts = {"announcements": 1, "board": 1, "talk": 0}
    backend["data"].update(total=2, counts=counts,
                           keywordCounts=[{"keyword": "OTA", "total": 2, "counts": counts}])
    result = search_public_activity.invoke({"keywords": ["OTA"]})
    assert result.kind == "ok" and len(result.data["items"]) == 2


def test_batch_transport_failure_is_unknown_not_zero(monkeypatch):
    requests = []

    def fail(request):
        requests.append(request)
        raise httpx.ReadTimeout("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        monkeypatch.setattr(base, "_client", client)
        result = search_public_activity.invoke({"keywords": ["OTA", "升级"]})
    assert result.kind == "unavailable" and result.data is None
    assert len(requests) == 1 and requests[0].method == "POST"
