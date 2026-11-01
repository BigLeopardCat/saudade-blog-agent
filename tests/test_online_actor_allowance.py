"""Host quota exhaustion must stop a paid regression before its first model call."""

from secrets import token_urlsafe

import httpx
import pytest

from eval import online_baseline
from eval.online_transport import EvaluationError, OnlineTransport


@pytest.fixture(autouse=True)
def synthetic_signer(monkeypatch):
    monkeypatch.setattr("eval.online_transport._sign_local_jwt", lambda *_: token_urlsafe())


@pytest.mark.parametrize("used,limit,remaining,unlimited,required,accepted", [
    (3, 5, 2, False, 2, True),
    (3, 5, 2, False, 3, False),
    (500, 500, 0, False, 1, False),
    (0, 0, 0, True, 1000, True),
    (1, 0, 0, True, 1, False),
    (3, 5, 3, False, 1, False),
    (-1, 5, 6, False, 1, False),
    (False, 5, 5, False, 1, False),
])
def test_quota_contract_and_complete_planned_count(
    tmp_path, used, limit, remaining, unlimited, required, accepted
):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/api/protected/quota"
        assert "Authorization" in request.headers
        return httpx.Response(200, json={"code": 200, "data": {
            "used": used, "limit": limit, "remaining": remaining, "unlimited": unlimited}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = OnlineTransport(agent_url="http://agent", blog_url="http://blog",
                                  report_dir=tmp_path, client=client, host=True)
        if accepted:
            value = adapter.check_turn_allowance(7, required)
            assert value["required_turns"] == required and value["remaining"] == remaining
        else:
            with pytest.raises(EvaluationError):
                adapter.check_turn_allowance(7, required)
    assert len(calls) == 1 and adapter.requests == []


@pytest.mark.parametrize("body", [{}, {"code": 200, "data": {"unlimited": True}},
                                {"code": 500, "data": {}}])
def test_missing_quota_is_unknown_instead_of_unlimited(tmp_path, body):
    with httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=body))) as client:
        adapter = OnlineTransport(agent_url="http://agent", blog_url="http://blog",
                                  report_dir=tmp_path, client=client)
        with pytest.raises(EvaluationError, match="missing"):
            adapter.check_turn_allowance(7, 1)


def test_all_actor_turns_are_counted_before_any_model_or_conversation(tmp_path, monkeypatch):
    cases = [{"id": "one", "identity": "user", "tags": []},
             {"id": "many", "identity": "user", "tags": [],
              "rounds": [{"user_input": "first"}, {"user_input": "second"}]}]

    class Host:
        host = True
        agent_url = "http://agent"
        client = httpx.Client(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"sha": "fixed"})))

        def profile(self, uid, roles):
            assert uid == 7 and roles == {"user"}
            return "user"

        def check_turn_allowance(self, uid, required):
            assert uid == 7 and required == 3
            raise EvaluationError("quota exhausted")

        def conversation(self, *_):
            pytest.fail("must not open a conversation with exhausted allowance")

    monkeypatch.setattr(online_baseline.golden, "run_case",
                        lambda *_a, **_kw: pytest.fail("must not spend a model call"))
    host = Host()
    try:
        with pytest.raises(EvaluationError, match="quota exhausted"):
            online_baseline.run(cases, host, {"user": 7}, tmp_path / "report",
                                phase="full", suite_hash="frozen")
    finally:
        host.client.close()
