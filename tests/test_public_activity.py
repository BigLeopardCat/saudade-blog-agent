"""Public activity reads keep visibility, totals and bounded text explicit."""

import json

import httpx
import pytest
from pydantic import ValidationError

from agent.domains import public_activity, public_pages
from tools import base


def record(identifier=9, source="board"):
    row = {"id": identifier, "title": "title", "titleTruncated": False,
           "content": "text", "contentTruncated": False, "createdAt": None}
    if source != "announcements":
        row.update({"source": source, "approved": 1, "displayName": "free name",
                    "displayNameTruncated": False, "userId": 123,
                    "rejectReason": "must not project", "mine": True})
    return row


@pytest.fixture
def backend(monkeypatch):
    state = {"data": [record()], "total": 1, "requests": [], "code": 200}

    def handle(request):
        state["requests"].append(request)
        assert request.method == "GET"
        assert "authorization" not in request.headers
        assert "x-user-assertion" not in request.headers
        return httpx.Response(state.get("http", 200),
                              json={key: state[key] for key in ("code", "data", "total")})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        yield state


@pytest.mark.parametrize(("name", "resource", "source"), [
    ("get_announcements", "announcements", "announcements"),
    ("list_guestbook", "guestbook", "board"),
    ("list_talks", "talks", "talk"),
])
def test_public_tool_contract_and_safe_projection(backend, name, resource, source):
    backend["data"] = [record(source=source)]
    result = getattr(public_activity, name).invoke({"page": 1, "limit": 3})
    assert result.kind == "ok"
    data = json.loads(result)
    assert data["total"] == 1 and data["hasMore"] is False
    assert data["pageSize"] == 3 and data["items"][0]["id"] == 9
    assert "userId" not in result and "rejectReason" not in result and "mine" not in result
    request = backend["requests"][0]
    assert request.url.path.endswith("/activity/" + resource)
    assert dict(request.url.params) == {"page": "1", "page_size": "3"}


def test_detail_reads_are_bound_to_id_and_page_one(backend):
    result = public_activity.list_guestbook.invoke({"item_id": 9, "page": 2, "limit": 10})
    assert result.kind == "ok"
    assert dict(backend["requests"][0].url.params) == {"page": "1", "page_size": "1", "id": "9"}
    backend["data"][0]["id"] = 10
    assert public_activity.list_guestbook.invoke({"item_id": 9}).kind == "unavailable"


@pytest.mark.parametrize("args", [{"page": 0}, {"page": 10001}, {"limit": 11},
                                  {"limit": True}, {"page": "1"}, {"item_id": -1}])
def test_invalid_arguments_never_reach_io(backend, args):
    with pytest.raises(ValidationError):
        public_activity.list_talks.invoke(args)
    assert backend["requests"] == []


@pytest.mark.parametrize(("key", "value"), [
    ("code", True), ("code", 500), ("total", None), ("total", True),
    ("total", -1), ("total", 2), ("data", {}), ("http", 503),
])
def test_missing_totals_failures_and_inconsistent_pages_are_unknown(backend, key, value):
    backend[key] = value
    assert public_activity.list_guestbook.invoke({}).kind == "unavailable"


@pytest.mark.parametrize(("key", "value"), [
    ("source", "talk"), ("approved", 0), ("approved", 2), ("approved", True),
    ("contentTruncated", None), ("displayName", None), ("title", []),
    ("id", True), ("createdAt", {}), ("displayNameTruncated", None),
])
def test_visibility_and_text_fields_fail_closed(backend, key, value):
    backend["data"][0][key] = value
    assert public_activity.list_guestbook.invoke({}).kind == "unavailable"


def test_duplicates_are_not_presented_as_distinct_records(backend):
    backend.update(data=[record(), record()], total=2)
    assert public_activity.list_guestbook.invoke({}).kind == "unavailable"


def test_page_metadata_distinguishes_empty_page_from_empty_collection(backend):
    backend.update(data=[], total=7)
    result = public_activity.list_guestbook.invoke({"page": 3})
    data = json.loads(result)
    assert data["items"] == [] and data["total"] == 7 and data["hasMore"] is False
    backend.update(data=[record(i) for i in range(1, 6)], total=7)
    data = json.loads(public_activity.list_guestbook.invoke({}))
    assert data["hasMore"] is True


def test_explicit_truncation_flags_and_untrusted_text_are_preserved(backend):
    backend["data"][0].update(content="文" * 240, contentTruncated=True,
                              displayName="名" * 100, title="题" * 300)
    result = public_activity.list_guestbook.invoke({})
    row = json.loads(result)["items"][0]
    assert row["contentTruncated"] and row["titleTruncated"] and row["displayNameTruncated"]
    assert len(row["content"]) == 240
    assert len(row["title"]) <= 256 and len(row["displayName"]) <= 81


def test_large_pages_remain_valid_json_or_return_unknown(backend):
    backend.update(data=[{**record(i), "content": "文" * 2000} for i in range(1, 6)], total=5)
    assert public_activity.list_guestbook.invoke({}).kind == "unavailable"


def test_transport_failures_do_not_claim_empty_data(monkeypatch):
    def handle(request):
        raise httpx.ReadTimeout("offline", request=request)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        assert public_activity.list_talks.invoke({}).kind == "unavailable"


def test_common_transport_allows_composite_keys(backend):
    backend.update(data=[{"level": 1, "id": 7}, {"level": 2, "id": 7}], total=2)
    result = public_pages.read_page("tags", 1, 5, {}, lambda row: row,
                                    identity=lambda row: (row["level"], row["id"]))
    assert result.kind == "ok" and len(json.loads(result)["items"]) == 2
