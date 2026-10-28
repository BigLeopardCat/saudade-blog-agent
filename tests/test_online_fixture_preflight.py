"""Changing production content must fail preflight, not masquerade as model failure."""

import httpx
import pytest

from eval.online_baseline import POC_IDS, load_suite
from eval.online_transport import EvaluationError, OnlineTransport


def adapter(tmp_path, status, body):
    def respond(request):
        assert request.url.path == "/api/public/notes/23"
        assert "Authorization" not in request.headers
        return httpx.Response(status, json=body)
    return OnlineTransport(agent_url="http://agent", blog_url="http://blog",
                           report_dir=tmp_path, client=httpx.Client(
                               transport=httpx.MockTransport(respond)))


def test_preflight_accepts_a_matching_public_dto_without_model_calls(tmp_path):
    transport = adapter(tmp_path, 200, {"code": 200, "data": {"noteKey": 23}})
    transport.check_public_article_fixture(23)
    assert transport.requests == []


@pytest.mark.parametrize("status,body", [
    (404, {}), (200, {"code": 404}), (200, {"code": 200, "data": {"noteKey": 54}}),
    (200, {"code": 200, "data": {"noteKey": True}}), (200, []),
])
def test_absent_private_mismatched_and_invalid_targets_are_not_model_failures(
        tmp_path, status, body):
    transport = adapter(tmp_path, status, body)
    with pytest.raises(EvaluationError):
        transport.check_public_article_fixture(23)
    assert transport.requests == []


def test_backend_failure_stays_an_http_error(tmp_path):
    with pytest.raises(httpx.HTTPStatusError):
        adapter(tmp_path, 500, {}).check_public_article_fixture(23)


@pytest.mark.parametrize("article_id", [0, -1, True, "23"])
def test_invalid_fixture_identifiers_are_rejected_before_http(tmp_path, article_id):
    with pytest.raises(EvaluationError, match="invalid public article"):
        adapter(tmp_path, 200, {}).check_public_article_fixture(article_id)


def test_favorite_poc_target_and_expected_action_agree_with_the_preflight():
    case = next(case for case in load_suite() if case["id"] == "favorite_prepare")
    assert case["id"] in POC_IDS
    assert case["requires_public_article"] == 23
    assert case["context"]["current_url"] == "/article/23"
    expected = case["gold"]["require_prepared_or_noop"][0]
    assert expected["spec"]["args"]["article_id"] == expected["facts"]["article_id"] == 23
