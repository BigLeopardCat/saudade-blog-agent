"""Policy-specific scenarios verify their target and decision before model calls."""

import json

import httpx
import pytest

from eval import native_assertions, online_baseline
from eval.online_transport import EvaluationError, OnlineTransport

EXPECTED = {"accountId": 11, "action": "mute", "role": None, "allowed": True, "reasonCode": None}


@pytest.mark.parametrize(
    "value",
    [
        {},
        [],
        {**EXPECTED, "accountId": True},
        {**EXPECTED, "action": "delete"},
        {**EXPECTED, "allowed": 1},
        {**EXPECTED, "allowed": False},
        {**EXPECTED, "reasonCode": "target_superadmin"},
        {**EXPECTED, "role": "user"},
        {**EXPECTED, "action": "set_role"},
        {**EXPECTED, "extra": "field"},
    ],
)
def test_preflight_rejects_malformed_or_contradictory_declarations(value):
    with pytest.raises(ValueError, match="account eligibility fixture"):
        online_baseline.fixture_account_eligibility(
            {"id": "fixture", "requires_account_eligibility": value}
        )


def test_fixture_requires_a_fresh_authenticated_read_of_the_exact_operation(tmp_path, monkeypatch):
    seen = []

    def handle(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.path == "/api/protected/account-actions/eligibility"
        assert dict(request.url.params) == {"accountId": "11", "action": "mute"}
        assert request.headers["Authorization"] == "Bearer fixture"
        return httpx.Response(200, json={"code": 200, "data": {**EXPECTED, "message": "允许准备"}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        transport = OnlineTransport(
            agent_url="http://agent", blog_url="http://blog", report_dir=tmp_path, client=client
        )
        transport.uid = 7
        monkeypatch.setattr(
            transport, "host_headers", lambda uid: {"Authorization": "Bearer fixture"}
        )
        online_baseline.check_case_fixtures(
            {"id": "fixture", "requires_account_eligibility": EXPECTED}, transport
        )
    assert len(seen) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"accountId": 12},
        {"action": "freeze"},
        {"role": "admin"},
        {"allowed": False, "reasonCode": "target_superadmin"},
        {"allowed": False},
        {"allowed": 1},
        {"message": ""},
    ],
)
def test_changed_or_invalid_backend_policy_fails_the_fixture(tmp_path, monkeypatch, override):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"code": 200, "data": {**EXPECTED, "message": "允许准备", **override}}
            )
        )
    ) as client:
        transport = OnlineTransport(
            agent_url="http://agent", blog_url="http://blog", report_dir=tmp_path, client=client
        )
        monkeypatch.setattr(transport, "host_headers", lambda uid: {})
        with pytest.raises(EvaluationError):
            transport.check_account_eligibility_fixture(EXPECTED)


def test_missing_target_is_an_evaluation_error_before_any_model_call(tmp_path, monkeypatch):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(404))) as client:
        transport = OnlineTransport(
            agent_url="http://agent", blog_url="http://blog", report_dir=tmp_path, client=client
        )
        transport.uid = 99
        monkeypatch.setattr(transport, "host_headers", lambda uid: {})
        case = {"id": "fixture", "identity": "admin", "requires_account_eligibility": EXPECTED}
        result = online_baseline.fixture_preflight([case], transport, {"admin": 7})
        assert result[0]["status"] == "evaluation_error" and transport.uid == 99
        assert transport.requests == []


def test_golden_distinguishes_missing_duration_from_protected_target_policy():
    cases = {case["id"]: case for case in online_baseline.load_suite()}
    for name, allowed in (
        ("account_mute_missing_duration", True),
        ("account_mute_protected_target", False),
    ):
        case = cases[name]
        expected = online_baseline.fixture_account_eligibility(case)
        assert expected["allowed"] is allowed
        observation = {
            "node": "native",
            "event": "call",
            "name": "get_account_action_eligibility",
            "kind": "ok",
            "args": {"account_id": expected["accountId"], "action": "mute"},
            "result": json.dumps({**expected, "message": "后端事实"}),
        }
        result = {"text": "后端事实", "native_events": [observation]}
        assert not native_assertions.check(case["gold"], result)
        result["native_events"][0]["result"] = json.dumps({**expected, "allowed": not allowed})
        assert native_assertions.check(case["gold"], result)
