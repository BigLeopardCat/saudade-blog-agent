"""A notice decision cannot authorize, erase or misdescribe the later role change."""

import json
from contextlib import ExitStack

import httpx
import pytest
import test_account_actions_native as accounts_fixture
import test_notices_native as notices_fixture
from _framework_fixture import call, model_replies
from _framework_ledger import Ledger
from _own_support import config
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import confirm
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.products.blog.confirmation import cancel
from agent.runtime import writes
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.results import TurnResult
from agent.runtime.service import AgentRuntime
from tools import base


@pytest.fixture
def business(monkeypatch, tmp_path):
    with ExitStack() as stack:
        def backend(fixture):
            selected = fixture.__wrapped__(monkeypatch)
            state = next(selected)
            stack.callback(selected.close)
            return state, base._client

        notices, notice_client = backend(notices_fixture.notices)
        accounts, account_client = backend(accounts_fixture.accounts)

        def handle(request):
            if "/notice-actions" in request.url.path:
                return notice_client.send(request)
            if "/account-actions" in request.url.path:
                return account_client.send(request)
            raise AssertionError(
                f"Unexpected business request: {request.method} {request.url.path}")

        client = stack.enter_context(httpx.Client(transport=httpx.MockTransport(handle)))
        monkeypatch.setattr(base, "_client", client)
        monkeypatch.setattr(base, "host_credential", lambda _: "isolated-business-fixture")
        ledger = Ledger(str(tmp_path / "sequence.sqlite3"))
        stack.callback(ledger.close)
        monkeypatch.setattr(writes, "ledger_for", lambda _: ledger)
        yield notices, accounts


def actor():
    return config(principal=Principal(7, "admin", SOURCE_ASSERTION))


def run(runtime, text=None, grant=None):
    state = ({"confirm_grant": grant, "messages": []} if grant else
             {"messages": [HumanMessage(content=text)]})
    result = list(runtime.stream_events(state, actor()))[-1]
    assert isinstance(result, TurnResult)
    return result


def signed(result):
    return confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)


def replies():
    return [
        {"tool_calls": [call("load_admin_notices_info_tools", "notice-domain")]},
        {"tool_calls": [call("prepare_user_notice", "notice-prepare", user_id=11,
                             content="因近期不活跃，拟调整为杂鱼。")]},
        {"content": "通知待确认，身份调整尚未准备；这张卡只授权通知。"},
        {"tool_calls": [call("load_admin_account_actions_info_tools", "role-domain")]},
        {"tool_calls": [call("prepare_account_role", "role-prepare", account_id=11, role="zako")]},
        {"content": "身份调整请另行核对确认卡。"},
    ]


def test_approval_resumes_role_preparation_with_its_own_grant(business):
    notices, accounts = business
    with model_replies(*replies(), {"content": "两项各自批准的操作已完成。"}) as (model, requests):
        runtime = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
                               application=BlogApplication(), model_factory=lambda **_: model)
        first = run(runtime, "给账号11发通知说明因为不活跃拟降为杂鱼，然后调整身份。")
        assert notices["writes"] == accounts["writes"] == 0
        # Approval disables calls, but cannot make the canonical target vocabulary disappear.
        prompt = json.dumps(requests[-1]["messages"], ensure_ascii=False)
        assert "role_vocabulary" in prompt and "zako" in prompt and "杂鱼" in prompt
        second = run(runtime, grant=signed(first))
        assert notices["writes"] == 1 and accounts["writes"] == 0
        grant = signed(second)
        assert grant["jti"] != signed(first)["jti"]
        assert grant["specs"][0]["args"]["intent"]["role"] == "zako"
        run(runtime, grant=signed(first))
        assert notices["writes"] == 1 and accounts["writes"] == 0
        pending = runtime.get_state(actor()).values["pending_confirm"]
        assert pending["token"] == second.execution["pending_confirm"]["token"]
        run(runtime, grant=grant)
        assert notices["writes"] == accounts["writes"] == 1
        assert accounts["rows"][11]["role"] == "zako"


def test_cancelling_notice_preserves_explicit_later_role_request(business):
    notices, accounts = business
    with model_replies(*replies()) as (model, _requests):
        runtime = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
                               application=BlogApplication(), model_factory=lambda **_: model)
        first = run(runtime, "给账号11发通知说明因为不活跃拟降为杂鱼，然后调整身份。")
        assert cancel(runtime, signed(first), actor())["cancelled"] is True
        second = run(runtime, "通知取消，只继续准备将账号11调整为杂鱼。")
        assert signed(second)["specs"][0]["args"]["intent"]["role"] == "zako"
        assert notices["writes"] == accounts["writes"] == 0
