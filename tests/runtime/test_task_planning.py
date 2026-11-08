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
from agent.products.blog.application import BlogApplication
from agent.products.blog.capabilities import ToolCatalog
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.service import AgentRuntime
from agent.runtime.session import ToolSession
from agent.runtime.task_planning import TaskPlanningMiddleware, tools

pytestmark = pytest.mark.tool_scopes(
    inspect_plan="read.public",
)


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
        return AgentRuntime(
            CheckpointConversationStore(saver),
            application=BlogApplication(catalog),
            model_factory=lambda **_: model,
        )

    first_model = Model(AIMessage(content="", tool_calls=[
        call("load_task_planning_info_tools", "load-plan")]),
        AIMessage(content="", tool_calls=[
        call("write_todos", "plan", todos=PLAN)]), LOAD,
        AIMessage(content="", tool_calls=[FAVORITE]))
    first = selected(first_model)
    prepared = run(first, "收藏文章19，确认后核对。")
    assert not state["writes"]
    from agent.runtime.task_planning import SYSTEM_PROMPT
    assert SYSTEM_PROMPT in str(first_model.review_prompts)
    assert first_model.review_prompts[0][0] == first_model.prompts[0][0]
    grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
    next_model = Model(AIMessage(content="", tool_calls=[call("inspect_plan", "inspect")]),
                       AIMessage(content="", tool_calls=[call("write_todos", "done", todos=[{
                           "content": PLAN[0]["content"], "status": "completed"}])]),
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


@pytest.mark.parametrize("omission", [None, "body", "summary", "navigation", "scope"])
def test_composite_acceptance_rejects_partial_delivery_even_with_a_valid_card(
    monkeypatch, tmp_path, omission
):
    from eval import composite_task_contracts, native_contracts
    from eval.golden_evaluation import golden_trace

    ids = [101] if omission == "scope" else [101, 102]
    calls = [call("prepare_notification_read", "prepare", scope="selected", ids=ids)]
    if omission != "navigation":
        calls.append(call("navigate_page", "navigate", path="/guestbook"))
    reads = [AIMessage(content="", tool_calls=[
        call("load_notifications_info_tools", "load")]),
        AIMessage(content="", tool_calls=[call("list_notifications", "read", unread_only=True)])]
    if omission != "body":
        reads.append(AIMessage(content="", tool_calls=[
            call("list_notifications", "body-101", notification_id=101),
            call("list_notifications", "body-102", notification_id=102)]))
    model = Model(*reads,
        AIMessage(content="" if omission == "summary" else "部署 N-417，审核 R-862。",
                  tool_calls=calls), AIMessage(content="已标记并发送页面指令。"))
    monkeypatch.setattr("agent.runtime.service.get_llm", lambda **_: model)
    monkeypatch.setattr(golden_trace, "trace_root", lambda: str(tmp_path / "traces"))
    report = native_contracts.run([composite_task_contracts.CASES[0][0]])
    result = report["cases"][0]
    assert result["passed"] is (omission is None), result.get("reason")
    if omission:
        reason = {"body": "body", "summary": "summary", "navigation": "navigation",
                  "scope": "approval"}[omission]
        assert reason in result["reason"]


def test_execution_instructions_are_system_policy_and_not_quoted_capability_data():
    import json

    from agent.products.blog.prompt import build_prompt

    prompt = build_prompt([HumanMessage(content="继续")], config()["configurable"]["principal"])
    data = prompt[0].content.split("运行时能力（可信数据）：", 1)[1]
    facts, end = json.JSONDecoder().raw_decode(data)
    assert "等待确认前先交付" in data[end:]
    assert "guidance" not in facts["tools"]


@pytest.mark.parametrize("malformed", [False, True])
@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_same_agent_delivers_read_facts_after_preparation_without_execution_or_schema_change(
    monkeypatch, tmp_path, provider, malformed
):
    from _framework_fixture import call as wire_call
    from _framework_fixture import model_replies
    from _framework_ledger import Ledger

    from agent.runtime import writes
    from agent.runtime.budget import TaskBudget, budget_scope
    from eval.composite_task_contracts import REQUEST, backend

    ledger = Ledger(str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda _: ledger)
    summary = "部署已完成，校验码 N-417；评论待审核，校验码 R-862。确认后我再标记并打开页面。"
    try:
        with backend() as state, model_replies(
                {"tool_calls": [wire_call("load_notifications_info_tools", "load")]},
                {"tool_calls": [wire_call("list_notifications", "read", unread_only=True)]},
                {"tool_calls": [wire_call("list_notifications", "body-101", notification_id=101),
                                wire_call("list_notifications", "body-102", notification_id=102)]},
                {"tool_calls": [wire_call("prepare_notification_read", "prepare",
                                         scope="selected", ids=[101, 102])]},
                *([{"content": '<tool_call>{"name":"write_todos"}</tool_call>'}]
                  if malformed else []), {"content": summary}) as (model, requests):
            if provider == "deepseek":
                from models.deepseek import DeepSeekChat
                from models.llm import get_llm

                monkeypatch.setattr("config.settings.llm_provider", "opencode")
                model = get_llm(model="deepseek-v4.1-flash", api_key="synthetic",
                    base_url="https://model.fixture.test/v1", http_client=model.http_client,
                    streaming=True, stream_usage=True, max_tokens=128, enable_thinking=False)
                assert isinstance(model, DeepSeekChat)
            budget = TaskBudget()
            selected = AgentRuntime(
                CheckpointConversationStore(InMemorySaver()),
                application=BlogApplication(),
                model_factory=lambda **_: model,
            )
            with budget_scope(budget):
                events = list(selected.stream_events({"messages": [HumanMessage(content=REQUEST)]},
                                                     config()))
            result = events[-1]
            assert result.reply == summary and result.execution["pending_confirm"]
            assert not [r for r in state["requests"] if r.method != "GET"]
            assert budget.model_calls == len(requests) == (6 if malformed else 5)
            assert requests[-1]["tool_choice"] == "none"
            assert "<tool_call>" not in str(requests[-1]["messages"])
            assert requests[-2]["tools"] == requests[-1]["tools"]
            assert all(row["messages"][0] == requests[0]["messages"][0] for row in requests)
            assert result.execution["pending_confirm"]["token"] not in str(requests)
            assert "N-417" not in str(requests[2]["messages"])
            assert all(code in str(requests[-1]["messages"]) for code in ("N-417", "R-862"))
            assert selected.store.load(config()).messages[-1].content == summary
    finally:
        ledger.close()


def test_approval_wire_distinguishes_pending_work_from_unavailable_tools(backend):
    import json

    from _framework_fixture import call as wire_call
    from _framework_fixture import model_replies

    pending = [*PLAN, {"content": "再把通知101标为已读", "status": "pending"}]
    with model_replies(
            {"tool_calls": [wire_call("load_favorites_info_tools", "load-f"),
                            wire_call("load_notifications_info_tools", "load-n")]},
            {"tool_calls": [wire_call("write_todos", "plan", todos=pending)]},
            {"tool_calls": [wire_call("prepare_favorite_change", "favorite", action="add",
                                     article_id=19),
                            wire_call("prepare_notification_read", "notice", scope="selected",
                                      ids=[101])]},
            {"content": "先确认收藏，再继续通知的处理。"}) as (model, requests):
        selected = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
                                application=BlogApplication(), model_factory=lambda **_: model)
        result = run(selected, "收藏文章19，然后将通知101标为已读。")
        final = requests[-1]
        assert len(requests) == 4 and final["tool_choice"] == "none"
        assert final["tools"] == requests[-2]["tools"]
        messages = {row["tool_call_id"]: json.loads(row["content"]) for row in final["messages"]
                    if row["role"] == "tool" and row["tool_call_id"] in ("favorite", "notice")}
        assert messages["favorite"]["remaining_work_not_execution_evidence"] == pending
        assert messages["favorite"]["executed"] is False
        assert messages["favorite"]["approval_scope"] == "current_operation_only"
        assert messages["notice"]["reason"] == "awaiting_prior_confirmation"
        assert messages["notice"]["available_in_catalog"] is True
        assert result.execution["pending_confirm"]["token"] not in str(final)
        assert not backend[0]["writes"]
        assert not any(path.endswith("/notifications") for _, path in backend[0]["requests"])


def test_failed_approval_response_recovers_without_repreparing_or_rereading(backend):
    state, _ = backend
    first_model = Model(LOAD, AIMessage(content="", tool_calls=[FAVORITE]))
    first_model.approval_reply = OSError("synthetic reply failure")
    saver = InMemorySaver()
    selected = AgentRuntime(
        CheckpointConversationStore(saver),
        application=BlogApplication(),
        model_factory=lambda **_: first_model,
    )
    with pytest.raises(OSError, match="synthetic reply failure"):
        list(selected.stream_events({"messages": [HumanMessage(content="准备收藏文章19")],
                                     "task_id": "review-recovery"}, config()))
    previous_reads = list(state["requests"])
    assert selected.store.load(config()).unfinished and not state["writes"]
    next_model = Model()
    next_model.approval_reply = AIMessage(content="收藏准备好了，等你确认。")
    rebuilt = AgentRuntime(
        CheckpointConversationStore(saver),
        application=BlogApplication(),
        model_factory=lambda **_: next_model,
    )
    result = list(rebuilt.stream_events({"resume_task": "review-recovery"}, config()))[-1]
    assert result.reply == "收藏准备好了，等你确认。"
    assert result.execution["pending_confirm"] and state["requests"] == previous_reads
    assert len(next_model.review_prompts) == 1 and not next_model.prompts


def test_approval_response_cannot_execute_another_tool_and_keeps_original_preview(backend):
    state, _ = backend
    model = Model(LOAD, AIMessage(content="", tool_calls=[FAVORITE]))
    model.approval_reply = AIMessage(content="", tool_calls=[
        call("prepare_notification_read", "forbidden", scope="selected", ids=[101])])
    selected = AgentRuntime(
        CheckpointConversationStore(InMemorySaver()),
        application=BlogApplication(),
        model_factory=lambda **_: model,
    )
    result = run(selected, "准备收藏文章19")
    grant = confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)
    assert grant["executor"] == "favorites" and len(model.review_prompts) == 1
    assert not state["writes"] and not state["read"]
    assert not any(path.endswith("/notifications") for _, path in state["requests"])


@pytest.mark.parametrize("first", [
    AIMessage(content=""),
    AIMessage(content='<tool_call>{"name":"write_todos","arguments":{}}</tool_call>'),
    AIMessage(content="", invalid_tool_calls=[{"name": "write_todos", "args": "{",
                                            "id": "broken", "error": "invalid JSON"}]),
])
def test_unusable_approval_response_retries_once_through_the_framework(backend, first):
    class Replies(Model):
        @property
        def approval_reply(self):
            return next(self.reviews)

    model = Replies(LOAD, AIMessage(content="", tool_calls=[FAVORITE]))
    model.reviews = iter([first, AIMessage(content="请确认这项收藏。")])
    selected = AgentRuntime(
        CheckpointConversationStore(InMemorySaver()),
        application=BlogApplication(),
        model_factory=lambda **_: model,
    )
    result = run(selected, "准备收藏文章19")
    assert result.reply == "请确认这项收藏。" and len(model.review_prompts) == 2
    assert result.execution["pending_confirm"] and not backend[0]["writes"]


def test_notification_summary_approval_and_browser_ack_keep_one_durable_task(monkeypatch, tmp_path):
    from _framework_ledger import Ledger

    from agent.runtime import writes
    from eval.composite_task_contracts import REQUEST, backend

    saver = InMemorySaver()
    ledger = Ledger(str(tmp_path / "ledger.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda _: ledger)

    def selected(model):
        return AgentRuntime(
            CheckpointConversationStore(saver),
            application=BlogApplication(),
            model_factory=lambda **_: model,
        )

    first_model = Model(AIMessage(content="", tool_calls=[
        call("load_notifications_info_tools", "load")]),
        AIMessage(content="", tool_calls=[call("list_notifications", "read", unread_only=True)]),
        AIMessage(content="", tool_calls=[
            call("list_notifications", "body-101", notification_id=101),
            call("list_notifications", "body-102", notification_id=102)]),
        AIMessage(content="", tool_calls=[call("prepare_notification_read", "prepare",
                                              scope="selected", ids=[101, 102])]))
    first_model.approval_reply = AIMessage(
        content="部署 N-417，评论待审 R-862；确认后标记并打开留言板。")
    cfg = config(browser_feedback=True)
    try:
        with backend() as state:
            initial = selected(first_model)
            prepared = run(initial, REQUEST, cfg=cfg)
            task = initial.store.load(cfg).run["id"]
            grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
            assert not [r for r in state["requests"] if r.method == "POST"]
            after_confirm = selected(Model(AIMessage(content="", tool_calls=[
                call("navigate_page", "open", path="/guestbook")])))
            waiting = run(after_confirm, grant=grant, cfg=cfg)
            action = waiting.execution["client_action"]
            assert action["task_id"] == task
            assert after_confirm.store.load(cfg).unfinished
            assert all(row["isRead"] for row in state["items"][:2])
            assert not state["items"][-1]["isRead"]
            assert after_confirm.get_state(cfg, subgraphs=True).interrupts
            acknowledged = config(browser_feedback=True, browser_result={
                "command_id": action["command_id"], "path": "/guestbook", "status": "completed"})
            rebuilt = selected(Model(AIMessage(content="所选两条通知已读，浏览器已到留言板。")))
            done = list(rebuilt.stream_events({"resume_task": task}, acknowledged))[-1]
            assert done.reply == "所选两条通知已读，浏览器已到留言板。"
            assert any(r.get("browser_status") == "completed" for r in done.execution["receipts"])
            assert not rebuilt.store.load(cfg).unfinished
            assert len([r for r in state["requests"] if r.method == "POST"]) == 1
            replay = run(rebuilt, grant=grant, cfg=cfg)
            assert not replay.execution.get("client_action")
            assert len([r for r in state["requests"] if r.method == "POST"]) == 1
    finally:
        ledger.close()


@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_approval_repair_does_not_retry_provider_limits(backend, reason):
    model = Model(LOAD, AIMessage(content="", tool_calls=[FAVORITE]))
    model.approval_reply = AIMessage(content="", response_metadata={"finish_reason": reason})
    selected = AgentRuntime(
        CheckpointConversationStore(InMemorySaver()), application=BlogApplication(),
        model_factory=lambda **_: model,
    )
    result = run(selected, "准备收藏文章19")
    assert len(model.review_prompts) == 1
    assert result.execution["pending_confirm"] and not backend[0]["writes"]


def test_malformed_approval_repair_is_bounded_and_keeps_same_proposal(backend):
    model = Model(LOAD, AIMessage(content="", tool_calls=[FAVORITE]))
    model.approval_reply = AIMessage(content='<tool_call>{"name":"write_todos"}</tool_call>')
    selected = AgentRuntime(
        CheckpointConversationStore(InMemorySaver()), application=BlogApplication(),
        model_factory=lambda **_: model,
    )
    result = run(selected, "准备收藏文章19")
    assert len(model.review_prompts) == 2 and "tool_call" not in result.reply
    grant = confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)
    assert grant["executor"] == "favorites" and len(grant["specs"]) == 1
    assert not backend[0]["writes"]
