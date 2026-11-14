"""Article reads use typed host references without taxonomy lookup or ID guessing."""

from urllib.parse import parse_qs, urlsplit

import pytest

from agent.domains.article_metadata import tag_references
from agent.domains.articles.requests import tool_args
from eval.fixtures.document_reads import document_page
from tools import base
from tools.notes import get_article_detail, search_notes
from tools.result import unavailable

COLLIDING_REFS = [
    {"id": 7, "level": 1, "name": "一级", "color": "#123456"},
    {"id": 7, "level": 2, "name": "二级", "color": None},
]
EXPECTED = [{"id": 7, "level": 1, "name": "一级"},
            {"id": 7, "level": 2, "name": "二级"}]


def _source(refs):
    return {"noteKey": 19, "noteTitle": "T", "noteContent": "body",
            "noteTags": "7,7", "noteTagRefs": refs}


def _transport(monkeypatch, data):
    calls = []

    def read(path, **kwargs):
        calls.append((path, kwargs))
        if path.startswith("/documents/note/") and isinstance(data, dict):
            query = {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}
            return document_page("note", 19, data["noteTitle"], data["noteContent"], query,
                                 {"tags": data.get("noteTagRefs"), "tagsTruncated": False})
        assert path not in ("/tagone", "/tagtwo")
        return data

    monkeypatch.setattr(base, "_get", read)
    return calls


def test_search_preserves_same_id_in_both_levels_without_directory_requests(monkeypatch):
    calls = _transport(monkeypatch, [_source(COLLIDING_REFS)])
    result = search_notes.invoke({"keyword": "T"})
    assert result.kind == "ok" and result.data["items"][0]["tags"] == EXPECTED
    assert result.data["items"][0]["tagsTruncated"] is False
    assert len(calls) == 1 and calls[0][0] == "/notes/search"


def test_detail_uses_host_refs_without_duplicate_raw_tags_or_directory_requests(monkeypatch):
    calls = _transport(monkeypatch, _source(COLLIDING_REFS))
    result = get_article_detail.invoke(tool_args({"view": "full_text", "article_id": 19}))
    assert result.kind == "ok" and result.data["tags"] == EXPECTED
    assert "noteTagRefs" not in result.data and "noteTags" not in result.data
    assert [path.split("?")[0] for path, _ in calls] == ["/documents/note/19/read"]


@pytest.mark.parametrize("refs", [None, "7", {}, [None], [{"id": True, "level": 1, "name": "T"}],
                                  [{"id": 7, "level": "1", "name": "T"}],
                                  [{"id": 7, "level": 1, "name": ""}],
                                  [COLLIDING_REFS[0], COLLIDING_REFS[0]]])
def test_malformed_refs_remain_unknown_without_raw_id_guessing(monkeypatch, refs):
    calls = _transport(monkeypatch, _source(refs))
    result = get_article_detail.invoke(tool_args({"view": "full_text", "article_id": 19}))
    assert result.kind == "ok" and result.data["tags"] is None
    assert result.data["noteContent"] == "body"
    assert len(calls) == 1
    with pytest.raises(ValueError):
        tag_references(refs)


def test_missing_refs_do_not_claim_no_tags(monkeypatch):
    source = _source([])
    source.pop("noteTagRefs")
    _transport(monkeypatch, [source])
    assert search_notes.invoke({"keyword": "T"}).data["items"][0]["tags"] is None


def test_empty_refs_are_observed_no_tags(monkeypatch):
    _transport(monkeypatch, _source([]))
    result = get_article_detail.invoke(tool_args({"view": "full_text", "article_id": 19}))
    assert result.data["tags"] == [] and result.data["tagsTruncated"] is False


def test_unavailable_search_stays_unavailable(monkeypatch):
    calls = _transport(monkeypatch, unavailable("offline"))
    result = search_notes.invoke({"keyword": "T"})
    assert result.kind == "unavailable" and result.data is None and len(calls) == 1


def test_typed_reference_projection_is_bounded_without_claiming_completeness(monkeypatch):
    refs = [{"id": i, "level": 1, "name": str(i)} for i in range(1, 24)]
    _transport(monkeypatch, [_source(refs)])
    result = search_notes.invoke({"keyword": "T"})
    row = result.data["items"][0]
    assert len(row["tags"]) == 20 and row["tagsTruncated"] is True
