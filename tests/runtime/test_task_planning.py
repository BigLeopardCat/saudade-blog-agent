"""Framework todo state is task-local work memory across approval and recovery."""

import pytest
from _own_support import config
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from test_confirmation_continuation import FAVORITE, LOAD, Model, call, run
from test_confirmation_continuation import backend as backend_fixture

from agent import confirm
from agent.products.blog.capabilities import ToolCatalog
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.session import ToolSession
from agent.runtime.task_planning import TaskPlanningMiddleware, tools

backend = backend_fixture
PLAN = [{"content": "收藏文章19，确认后核对回执", "status": "in_progress"}]


def test_standard_todo_state_survives_approval_reconstruction_and_resets_for_new_task(backend):
    state, _ = backend
    seen = []

    @tool
    def inspect_plan(runtime: ToolRuntime) -> str:
        """Read synthetic task working memory."""
        seen.append(runtime.state.get("todos"))
        return "checked"

    catalog = ToolCatalog(read_tools={inspect_plan.name: inspect_plan})
    saver = InMemorySaver()

    def selected(model):
        return NativeRuntime(NativeConversationStore(saver), catalog=catalog,
                             model_factory=lambda **_: model)

    first_model = Model(AIMessage(content="", tool_calls=[
        call("load_task_planning_info_tools", "load-plan")]),
        AIMessage(content="", tool_calls=[
        call("write_todos", "plan", todos=PLAN)]), LOAD,
        AIMessage(content="", tool_calls=[FAVORITE]))
    first = selected(first_model)
    prepared = run(first, "收藏文章19，确认后核对。")
    assert not state["writes"]
    from agent.runtime.task_planning import SYSTEM_PROMPT
    assert SYSTEM_PROMPT not in str(first_model.review_prompts)
    grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
    next_model = Model(AIMessage(content="", tool_calls=[call("inspect_plan", "inspect")]),
                       AIMessage(content="已核对收藏回执。"))
    run(selected(next_model), grant=grant)
    assert seen == [PLAN] and len(state["writes"]) == 1
    run(selected(Model(AIMessage(content="", tool_calls=[call("inspect_plan", "new-inspect")]),
                       AIMessage(content="新任务没有旧清单。"))), "开始新的检查")
    assert seen == [PLAN, []] and len(state["writes"]) == 1


def test_standard_todo_middleware_rejects_duplicate_updates_in_a_single_model_turn():
    model = Model(AIMessage(content="", tool_calls=[
        call("load_task_planning_info_tools", "load-plan")]),
        AIMessage(content="", tool_calls=[
        call("write_todos", "one", todos=PLAN), call("write_todos", "two", todos=[])]),
        AIMessage(content="清单更新冲突，没有执行业务操作。"))
    session = ToolSession(model, config(), {}, {"task_planning": tools()},
                          middleware=(TaskPlanningMiddleware,))
    session.run([HumanMessage(content="核对多个要求")])
    assert session.framework_state.get("todos") == []
    errors = [m for m in model.prompts[-1] if m.type == "tool"]
    assert len(errors) == 3 and all(m.status == "error" for m in errors[-2:])


def test_chat_only_session_has_no_planning_tool_or_additional_model_call():
    model = Model(AIMessage(content="你好。"))
    session = ToolSession(model, config(), {}, {}, middleware=(TaskPlanningMiddleware,))
    assert session.run([HumanMessage(content="你好")]).reply == "你好。"
    assert len(model.prompts) == 1 and not session.menu


@pytest.mark.parametrize("omission", [None, "summary", "navigation", "scope"])
def test_composite_acceptance_rejects_partial_delivery_even_with_a_valid_card(
    monkeypatch, tmp_path, omission
):
    from eval import composite_task_contracts, native_contracts
    from eval.run_golden import golden_trace

    ids = [101] if omission == "scope" else [101, 102]
    calls = [call("prepare_notification_read", "prepare", scope="selected", ids=ids)]
    if omission != "navigation":
        calls.append(call("navigate_page", "navigate", path="/guestbook"))
    model = Model(AIMessage(content="", tool_calls=[
        call("load_notifications_info_tools", "load")]),
        AIMessage(content="", tool_calls=[call("list_notifications", "read", unread_only=True)]),
        AIMessage(content="" if omission == "summary" else "部署 N-417，审核 R-862。",
                  tool_calls=calls), AIMessage(content="已标记并发送页面指令。"))
    monkeypatch.setattr("agent.runtime.native.get_llm", lambda **_: model)
    monkeypatch.setattr(golden_trace, "trace_root", lambda: str(tmp_path / "traces"))
    report = native_contracts.run([composite_task_contracts.CASES[0][0]])
    result = report["cases"][0]
    assert result["passed"] is (omission is None), result.get("reason")
    if omission:
        reason = {"summary": "summary", "navigation": "navigation", "scope": "approval"}[omission]
        assert reason in result["reason"]


def test_execution_instructions_are_system_policy_and_not_quoted_capability_data():
    import json

    from agent.products.blog.prompt import build_prompt

    prompt = build_prompt([HumanMessage(content="继续")], config()["configurable"]["principal"])
    data = prompt[0].content.split("运行时能力（可信数据）：", 1)[1]
    facts, end = json.JSONDecoder().raw_decode(data)
    assert "等待确认前先交付" in data[end:]
    assert "guidance" not in facts["tools"]


def test_same_agent_delivers_read_facts_after_preparation_without_execution_or_schema_change(
    monkeypatch, tmp_path
):
    from _framework_fixture import call as wire_call
    from _framework_fixture import model_replies
    from _framework_ledger import Ledger

    from agent.runtime import writes
    from agent.runtime.budget import RequestBudget, budget_scope
    from eval.composite_task_contracts import REQUEST, backend

    ledger = Ledger(str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda _: ledger)
    summary = "部署已完成，校验码 N-417；评论待审核，校验码 R-862。确认后我再标记并打开页面。"
    try:
        with backend() as state, model_replies(
                {"tool_calls": [wire_call("load_notifications_info_tools", "load")]},
                {"tool_calls": [wire_call("list_notifications", "read", unread_only=True)]},
                {"tool_calls": [wire_call("prepare_notification_read", "prepare",
                                         scope="selected", ids=[101, 102])]},
                {"content": summary}) as (model, requests):
            budget = RequestBudget()
            selected = NativeRuntime(NativeConversationStore(InMemorySaver()),
                                     model_factory=lambda **_: model)
            with budget_scope(budget):
                events = list(selected.stream_events({"messages": [HumanMessage(content=REQUEST)]},
                                                     config()))
            result = events[-1]
            assert result.reply == summary and result.execution["pending_confirm"]
            assert not [r for r in state["requests"] if r.method != "GET"]
            assert budget.model_calls == len(requests) == 4
            assert requests[-1]["tool_choice"] == "none"
            assert requests[-2]["tools"] == requests[-1]["tools"]
            assert result.execution["pending_confirm"]["token"] not in str(requests)
            assert "N-417" in str(requests[-1]["messages"])
            assert selected.store.load(config()).messages[-1].content == summary
    finally:
        ledger.close()


def test_failed_approval_response_recovers_without_repreparing_or_rereading(backend):
    state, _ = backend
    first_model = Model(LOAD, AIMessage(content="", tool_calls=[FAVORITE]))
    first_model.approval_reply = OSError("synthetic reply failure")
    saver = InMemorySaver()
    selected = NativeRuntime(NativeConversationStore(saver), model_factory=lambda **_: first_model)
    with pytest.raises(OSError, match="synthetic reply failure"):
        list(selected.stream_events({"messages": [HumanMessage(content="准备收藏文章19")],
                                     "task_id": "review-recovery"}, config()))
    previous_reads = list(state["requests"])
    assert selected.store.load(config()).unfinished and not state["writes"]
    next_model = Model()
    next_model.approval_reply = AIMessage(content="收藏准备好了，等你确认。")
    rebuilt = NativeRuntime(NativeConversationStore(saver), model_factory=lambda **_: next_model)
    result = list(rebuilt.stream_events({"resume_task": "review-recovery"}, config()))[-1]
    assert result.reply == "收藏准备好了，等你确认。"
    assert result.execution["pending_confirm"] and state["requests"] == previous_reads
    assert len(next_model.review_prompts) == 1 and not next_model.prompts


def test_approval_response_cannot_execute_another_tool_and_keeps_original_preview(backend):
    state, _ = backend
    model = Model(LOAD, AIMessage(content="", tool_calls=[FAVORITE]))
    model.approval_reply = AIMessage(content="", tool_calls=[
        call("prepare_notification_read", "forbidden", scope="selected", ids=[101])])
    selected = NativeRuntime(NativeConversationStore(InMemorySaver()),
                             model_factory=lambda **_: model)
    result = run(selected, "准备收藏文章19")
    grant = confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)
    assert grant["executor"] == "favorites" and len(model.review_prompts) == 1
    assert not state["writes"] and not state["read"]
    assert not any(path.endswith("/notifications") for _, path in state["requests"])


def test_empty_approval_response_retries_once_through_the_framework(backend):
    class Replies(Model):
        @property
        def approval_reply(self):
            return next(self.reviews)

    model = Replies(LOAD, AIMessage(content="", tool_calls=[FAVORITE]))
    model.reviews = iter([AIMessage(content=""), AIMessage(content="请确认这项收藏。")])
    selected = NativeRuntime(NativeConversationStore(InMemorySaver()),
                             model_factory=lambda **_: model)
    result = run(selected, "准备收藏文章19")
    assert result.reply == "请确认这项收藏。" and len(model.review_prompts) == 2
    assert result.execution["pending_confirm"] and not backend[0]["writes"]
