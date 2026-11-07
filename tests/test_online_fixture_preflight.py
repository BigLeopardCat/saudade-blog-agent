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
    assert case["requires_public_articles"] == [23]
    assert case["context"]["current_url"] == "/article/23"
    expected = case["gold"]["require_prepared_or_noop"][0]
    assert expected["spec"]["args"]["article_id"] == expected["facts"]["article_id"] == 23


@pytest.mark.parametrize('fault', ['none', 'wrong_id', 'invalid_version', 'missing'])
def test_gallery_preflight_reads_only_authoritative_metadata(tmp_path, monkeypatch, fault):
    from eval import image_generation_contracts

    row = {'imageKey': 72, 'imageUrl': 'https://cdn.test/cover.webp', 'displayName': 'fixture',
           'description': '', 'folderId': 0, 'isFavorite': False, 'version': 'a' * 64}
    if fault == 'wrong_id':
        row['imageKey'] = 73
    if fault == 'invalid_version':
        row['version'] = 'unknown'
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == 'GET'
        assert request.url.path == '/api/protect/gallery/images/72'
        return httpx.Response(200, json={'code': 404 if fault == 'missing' else 200, 'data': row})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        transport = OnlineTransport(agent_url='http://agent', blog_url='http://blog',
                                    report_dir=tmp_path, client=client)
        monkeypatch.setattr(transport, 'host_headers',
                            lambda _: {'Authorization': 'Bearer fixture'})
        if fault == 'none':
            transport.check_gallery_fixture(72)
        else:
            with pytest.raises(EvaluationError):
                transport.check_gallery_fixture(72)
    assert len(calls) == 1 and not transport.requests
    # The paid preparation is registered on the synthetic transport, not the HTTP suite.
    assert image_generation_contracts.REQUEST
    assert 'release_image_generation_prepare' not in {case['id'] for case in load_suite()}


def test_synthetic_image_backend_prepares_a_current_job_without_paid_execution():
    from _own_support import config

    from agent.domains.image_generation import tools
    from agent.principal import SOURCE_ASSERTION, Principal
    from agent.runtime.results import TurnResult
    from eval import image_generation_contracts as contracts

    with contracts.backend() as state:
        card = tools.prepare_image_generation.invoke(
            {"prompt": contracts.PROMPT},
            config=config(principal=Principal(7, "admin", SOURCE_ASSERTION)),
        )
        assert isinstance(card, TurnResult)
        pending = card.execution["pending_confirm"]
        assert pending["image_review"]["prompt"] == contracts.PROMPT
        assert pending["image_review"]["references"] == []
        contracts.prepare(lambda _: {"confirm_tokens": [pending["token"]], "exec_rows": []}, state)
        assert state["contract_evidence"]["external_image_calls"] == 0
        assert not state["unsupported_reads"]
