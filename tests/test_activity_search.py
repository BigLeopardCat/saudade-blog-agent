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
        "keyword": "OTA", "matchMode": "literal_case_insensitive",
        "sources": ["announcements", "board", "talk"],
        "counts": {"announcements": 0, "board": 1, "talk": 0}, "total": 1,
        "page": 1, "pageSize": 5, "hasMore": False,
        "items": [{"id": 9, "source": "board", "title": "", "titleTruncated": False,
                   "content": "OTA upgrade", "contentTruncated": True, "createdAt": None,
                   "approved": 1, "displayName": "free name", "displayNameTruncated": False,
                   "userId": 123, "rejectReason": "private"}]}}

    def handle(request):
        state["requests"].append(request)
        assert request.method == "GET" and "authorization" not in request.headers
        return httpx.Response(200, json={"code": state["code"], "data": state["data"]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


def test_one_search_preserves_coverage_and_drops_private_metadata(backend):
    result = search_public_activity.invoke({"keyword": " OTA "})
    assert result.kind == "ok" and result.data["total"] == 1
    assert result.data["sources"] == ("announcements", "board", "talk")
    assert "userId" not in str(result) and "rejectReason" not in str(result)
    assert result.data["items"][0]["contentTruncated"]
    assert len(backend["requests"]) == 1
    request = backend["requests"][0]
    assert request.url.path.endswith('/activity/search')
    assert dict(request.url.params) == {"keyword": "OTA", "page": "1", "page_size": "5"}
    assert json.loads(str(result))["counts"] == {"announcements": 0, "board": 1, "talk": 0}


@pytest.mark.parametrize("field,value", [
    ("counts", {"board": 1}), ("total", 2), ("page", 2), ("pageSize", 3),
    ("hasMore", True), ("keyword", "other"), ("matchMode", "semantic"),
    ("sources", ["board"]), ("items", []), ("total", True),
])
def test_unknown_scope_or_inconsistent_totals_never_mean_no_match(backend, field, value):
    backend["data"][field] = value
    assert search_public_activity.invoke({"keyword": "OTA"}).kind == "unavailable"


@pytest.mark.parametrize("field,value", [("approved", 0), ("approved", True),
                                        ("source", "private"), ("displayName", None),
                                        ("contentTruncated", None)])
def test_unverified_visibility_is_rejected(backend, field, value):
    backend["data"]["items"][0][field] = value
    assert search_public_activity.invoke({"keyword": "OTA"}).kind == "unavailable"


def test_valid_empty_and_out_of_range_pages_retain_source_totals(backend):
    backend["data"].update(items=[], page=2)
    assert search_public_activity.invoke({"keyword": "OTA", "page": 2}).data["total"] == 1
    backend["data"].update(page=1, total=0, counts={"announcements": 0, "board": 0, "talk": 0})
    assert search_public_activity.invoke({"keyword": "OTA"}).data["total"] == 0
    backend["code"] = 500
    assert search_public_activity.invoke({"keyword": "OTA"}).kind == "unavailable"


@pytest.mark.parametrize("args", [{"keyword": ""}, {"keyword": "a\0b"},
                                  {"keyword": "x" * 129}, {"keyword": 7},
                                  {"keyword": "x", "limit": 11}])
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
