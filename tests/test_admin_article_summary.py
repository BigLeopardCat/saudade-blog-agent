"""Page-level status counts never claim an unseen catalog distribution."""

import json

import pytest
from test_catalog_reads import accounts, articles
from test_catalog_reads import backend as backend_fixture

backend = backend_fixture


def test_status_summary_counts_only_the_returned_page(backend):
    statuses = ["public"] * 9 + ["private"] * 8 + ["draft"] * 8
    template = backend["articles"][0]
    backend["articles"] = [{**template, "id": number, "tags": [], "status": status}
                           for number, status in enumerate(statuses, 1)]
    first, second = (json.loads(str(articles(page=page))) for page in (1, 2))
    assert first["total"] == second["total"] == 25
    assert first["hasMore"] and not second["hasMore"]
    assert first["returned_summary"] == {
        "scope": "returned_items", "count": 20, "status_counts": [
            {"status": "public", "count": 9}, {"status": "private", "count": 8},
            {"status": "draft", "count": 3}]}
    assert second["returned_summary"] == {
        "scope": "returned_items", "count": 5, "status_counts": [{"status": "draft", "count": 5}]}
    assert len(backend["requests"]) == 2


def test_unknown_statuses_and_null_stay_distinct_from_visibility(backend):
    statuses = [None, "null", "PUBLIC", "archived", "draft"]
    for row, status in zip(backend["articles"], statuses):
        row.update(status=status, isPublic=True)
    result = json.loads(str(articles(limit=5)))
    assert result["returned_summary"] == {
        "scope": "returned_items", "count": 5, "status_counts": [
            {"status": status, "count": 1} for status in statuses]}
    assert result["total"] == 7 and result["hasMore"]


@pytest.mark.parametrize("args,total", [({"article_id": 999}, 0), ({"page": 2}, 7)])
def test_empty_page_summary_does_not_replace_the_matching_total(backend, args, total):
    result = json.loads(str(articles(**args)))
    assert result["items"] == [] and result["total"] == total
    assert result["returned_summary"] == {"scope": "returned_items", "count": 0,
                                           "status_counts": []}


def test_summary_is_inside_the_existing_result_size_boundary(backend):
    backend["articles"][0]["status"] = "unknown-" + "x" * 4000
    result = articles(article_id=1)
    assert result.kind == "unavailable"
    assert "读取上限" in str(result)


def test_account_catalog_does_not_gain_an_unrelated_article_summary(backend):
    result = json.loads(str(accounts(limit=5)))
    assert "returned_summary" not in result
