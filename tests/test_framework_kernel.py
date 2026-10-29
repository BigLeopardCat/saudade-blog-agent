"""Framework and application contracts with isolated model and business transports."""

import json
from types import SimpleNamespace

import pytest
from _framework_fixture import call, model_replies, prepared_notice
from _framework_ledger import Ledger
from _own_support import config
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware, wrap_model_call
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent import confirm
from agent.domains.catalog import list_accounts
from agent.domains.notices.preparation import prepare_user_notice
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import writes
from agent.runtime.budget import BudgetExceeded, BudgetLimits, RequestBudget
from agent.runtime.model_budget import ModelBudgetHandler, ToolBudgetHandler
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.session import ToolSession
from eval.admin_task_fixture import backend


def admin_config():
    return config(principal=Principal(uid=7, role="admin", source=SOURCE_ASSERTION))


def candidate(model, tools, saver, *, question="", middleware=()):
    hooks = [*middleware]
    if question:
        hooks.append(HumanInTheLoopMiddleware(interrupt_on={
            tool.name: {"allowed_decisions": ["approve", "reject"], "description": question}
            for tool in tools}, interrupt_mode="per_call"))
    return create_agent(model=model, tools=tools, checkpointer=saver, middleware=hooks)


def request(text):
    return {"messages": [{"role": "user", "content": text}]}


def decision(kind, paused):
    return Command(resume={paused["__interrupt__"][0].id: {"type": kind}})


def test_framework_reuses_existing_account_tool_and_native_tool_exchange():
    with backend() as business, model_replies(
            {"tool_calls": [call("list_accounts", username="青叶")]},
            {"content": "查到了账号青叶。"}) as (model, requests):
        agent = candidate(model, [list_accounts], InMemorySaver())
        result = agent.invoke(request("查询账号青叶"), admin_config())
        assert result["messages"][-1].content == "查到了账号青叶。"
        assert len(requests) == 2 and not business["writes"]
        observations = [m for m in result["messages"] if m.type == "tool"]
        assert len(observations) == 1 and "青叶" in observations[0].content
        assert observations[0].tool_call_id == "call-1"
        assert requests[1]["messages"][-1]["role"] == "tool"


def test_application_session_uses_the_framework_with_the_real_model_transport():
    with backend() as business, model_replies(
            {"tool_calls": [call("list_accounts", username="青叶")]},
            {"content": "查到了账号青叶。"}) as (model, requests):
        session = ToolSession(model, admin_config(), {list_accounts.name: list_accounts}, {})
        result = session.run([HumanMessage(content="查询账号青叶")])
        assert result.reply == "查到了账号青叶。"
        assert len(requests) == 2 and not business["writes"]
        assert session.calls_used == session.budget.tool_calls == 1
        assert session.budget.model_calls == 2 and session.budget.tokens == 30
        assert session.observations and requests[0]["stream"] is True
        assert all(request.get("parallel_tool_calls") is not False for request in requests)


def test_existing_runtime_preserves_sequential_preparation_and_approval_on_the_sdk_wire(
        tmp_path, monkeypatch):
    cfg = admin_config()
    ledger = Ledger(str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda _: ledger)
    catalog = SimpleNamespace(initial_tools=lambda _: {}, domains=lambda *_: {
        "admin_notices": {prepare_user_notice.name: prepare_user_notice}})
    with backend() as business, model_replies(
            {"tool_calls": [call("load_admin_notices_info_tools", "load")]},
            {"tool_calls": [call(prepare_user_notice.name, "first",
                                 username="青叶", content="第一条"),
                            call(prepare_user_notice.name, "second",
                                 username="青叶", content="第二条")]},
            {"content": "请确认第一条通知。"},
            {"content": "请确认第二条通知。"},
            {"content": "两条通知都发好啦。"}) as (model, requests):
        runtime = NativeRuntime(NativeConversationStore(InMemorySaver()), catalog=catalog,
                                model_factory=lambda **_: model)
        first = list(runtime.stream_events(
            {"messages": [HumanMessage(content="给青叶分别发送两条通知。") ]}, cfg))[-1]
        assert first.execution["pending_confirm"] and not business["writes"]
        token = first.execution["pending_confirm"]["token"]
        grant = confirm.verify(token, 7, 42)
        second = list(runtime.stream_events({"confirm_grant": grant}, cfg))[-1]
        assert second.execution["pending_confirm"] and business["writes"] == ["notice"]
        assert len(requests) == 4
        assert requests[2]["tool_choice"] == requests[3]["tool_choice"] == "none"
        next_token = second.execution["pending_confirm"]["token"]
        done = list(runtime.stream_events({
            "confirm_grant": confirm.verify(next_token, 7, 42)}, cfg))[-1]
        assert done.reply == "两条通知都发好啦。" and len(requests) == 5
        assert business["writes"] == ["notice", "notice"]
        assert [row["content"] for row in business["notices"]] == ["第一条", "第二条"]
        assert token not in json.dumps(requests) and next_token not in json.dumps(requests)


def test_framework_middleware_filters_the_actual_model_menu():
    @wrap_model_call
    def no_tools(model_request, handler):
        return handler(model_request.override(tools=[]))

    with backend() as business, model_replies({"content": "你好。"}) as (model, requests):
        result = candidate(model, [list_accounts], InMemorySaver(), middleware=[no_tools]).invoke(
            request("你好"), admin_config())
        assert result["messages"][-1].content == "你好。"
        assert not requests[0].get("tools") and not business["requests"]


def test_framework_streams_visible_tokens_without_the_custom_tool_loop():
    with model_replies({"content": "这是流式答复。"}) as (model, requests):
        agent = candidate(model, [], InMemorySaver())
        events = list(agent.stream(request("你好"), admin_config(),
                                   stream_mode=["messages", "updates"]))
        body = "".join(data[0].content for mode, data in events if mode == "messages")
        assert body == "这是流式答复。"
        assert requests[0].get("stream") is True


def test_existing_request_budget_stops_framework_before_an_extra_provider_call():
    budget = RequestBudget(BudgetLimits(model_calls=1))
    cfg = {**admin_config(), "callbacks": [ModelBudgetHandler(budget), ToolBudgetHandler(budget)]}
    with backend(), model_replies(
            {"tool_calls": [call("list_accounts", username="青叶")]}) as (model, requests):
        agent = candidate(model, [list_accounts], InMemorySaver())
        with pytest.raises(BudgetExceeded, match="model_calls"):
            agent.invoke(request("查询账号青叶"), cfg)
        assert len(requests) == budget.model_calls == budget.tool_calls == 1
        assert budget.tokens == 15


@pytest.mark.parametrize("kind", ["approve", "reject"])
def test_framework_checkpoints_approval_and_resumes_existing_business_transaction(tmp_path, kind):
    cfg, saver = admin_config(), InMemorySaver()
    with backend() as business:
        tool, question, token = prepared_notice(cfg, str(tmp_path / "ledger.sqlite3"))
        with model_replies({"tool_calls": [call(tool.name)]}) as (first_model, first_requests):
            first = candidate(first_model, [tool], saver, question=question)
            paused = first.invoke(request("执行已准备的通知"), cfg)
            assert paused.get("__interrupt__") and not business["writes"]
            assert len(first_requests) == 1
        # Reconstruct the framework with its actual saved state, not a hand-built cursor.
        with model_replies({"content": "本次操作已处理。"}) as (next_model, next_requests):
            rebuilt = candidate(next_model, [tool], saver, question=question)
            result = rebuilt.invoke(decision(kind, paused), cfg)
            assert not result.get("__interrupt__") and len(next_requests) == 1
            assert business["writes"] == (["notice"] if kind == "approve" else [])
            assert token not in str(result["messages"])
            assert token not in json.dumps([first_requests, next_requests], ensure_ascii=False)
            if kind == "approve":
                observations = [m for m in result["messages"] if m.type == "tool"]
                facts = json.loads(observations[-1].content)
                assert len(facts["receipts"]) == 1 and not facts["blocked"]
                assert facts["receipts"][0]["tool"] == "send_user_notice"
                assert business["notices"][0]["content"] == "请查看新的站内通知。"


def test_framework_repeated_approval_still_uses_the_existing_idempotency_ledger(tmp_path):
    cfg, saver = admin_config(), InMemorySaver()
    with backend() as business:
        tool, question, _ = prepared_notice(cfg, str(tmp_path / "ledger.sqlite3"))
        with model_replies({"tool_calls": [call(tool.name)]}, {"content": "已处理。"},
                           {"tool_calls": [call(tool.name, "call-2")]},
                           {"content": "该操作已处理过。"}) as (model, requests):
            agent = candidate(model, [tool], saver, question=question)
            for _ in range(2):
                paused = agent.invoke(request("执行这项操作"), cfg)
                assert paused.get("__interrupt__")
                done = agent.invoke(decision("approve", paused), cfg)
                assert not done.get("__interrupt__")
            assert len(requests) == 4
            assert business["writes"] == ["notice"] and len(business["notices"]) == 1


def test_framework_approval_does_not_grant_business_permission(tmp_path):
    saver = InMemorySaver()
    with backend() as business:
        tool, question, _ = prepared_notice(admin_config(), str(tmp_path / "ledger.sqlite3"))
        with model_replies({"tool_calls": [call(tool.name)]},
                           {"content": "没有执行权限。"}) as (model, requests):
            agent = candidate(model, [tool], saver, question=question)
            paused = agent.invoke(request("执行这项操作"), admin_config())
            assert paused.get("__interrupt__")
            result = agent.invoke(decision("approve", paused), config())
            assert not business["writes"] and len(requests) == 2
            observations = [m for m in result["messages"] if m.type == "tool"]
            assert '"receipts": []' in observations[-1].content


@pytest.mark.parametrize("second_decision", ["approve", "reject"])
def test_partial_approval_preserves_independent_decisions_and_business_versions(
        tmp_path, second_decision):
    cfg = admin_config()
    with backend() as business:
        tool, question, _ = prepared_notice(cfg, str(tmp_path / "ledger.sqlite3"))
        other, _, _ = prepared_notice(cfg, str(tmp_path / "ledger.sqlite3"),
                                     content="第二条独立通知。", name="execute_another_notice")
        with model_replies({"tool_calls": [call(tool.name), call(other.name, "call-2")]},
                           {"content": "已处理本次决定。"}) as (model, requests):
            agent = candidate(model, [tool, other], InMemorySaver(), question=question)
            paused = agent.invoke(request("执行这批操作"), cfg)
            assert len(paused["__interrupt__"]) == 2 and not business["writes"]
            first = next(item for item in paused["__interrupt__"]
                         if item.value["name"] == tool.name)
            partial = agent.invoke(Command(resume={first.id: {"type": "approve"}}), cfg)
            assert business["writes"] == ["notice"] and len(requests) == 1
            assert len(partial["__interrupt__"]) == 1
            assert partial["__interrupt__"][0].value["name"] == other.name
            done = agent.invoke(decision(second_decision, partial), cfg)
            assert not done.get("__interrupt__") and len(requests) == 2
            # The fixture advances the recipient revision after the first write.
            # A framework approval must not bypass the second operation's stale version.
            if second_decision == "approve":
                results = [m for m in done["messages"] if m.type == "tool"]
                blocked = json.loads(results[-1].content)
                assert blocked["blocked"] and not blocked["receipts"]
            assert [item["content"] for item in business["notices"]] == ["请查看新的站内通知。"]
