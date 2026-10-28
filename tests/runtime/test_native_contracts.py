"""Verify that the reusable model evaluation exercises the actual lifecycle."""

import json

import pytest
from langchain_core.messages import AIMessage

from eval import native_contracts
from eval.native_contracts import golden_trace


@pytest.fixture(autouse=True)
def narration_model(monkeypatch):
    class Model:
        def invoke(self, prompt, **kwargs):
            return AIMessage(content="操作完成。", usage_metadata={
                "input_tokens": 20, "output_tokens": 5, "total_tokens": 25})

    monkeypatch.setattr("agent.products.blog.narration.get_llm", lambda **_: Model())


def test_confirmed_write_evaluates_continuation_and_replay_without_another_write(
    monkeypatch, tmp_path
):
    responses = iter([
        AIMessage(content="", tool_calls=[{"name": "load_favorites_info_tools",
                                          "args": {}, "id": "load"}]),
        AIMessage(content="", tool_calls=[{"name": "prepare_favorite_change",
                  "args": {"action": "add", "article_id": 19}, "id": "prepare"}]),
        AIMessage(content="操作完成。"),
    ])

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt, **kwargs):
            return next(responses)

    monkeypatch.setattr("agent.runtime.native.get_llm", lambda **_: Model())
    monkeypatch.setattr(golden_trace, "trace_root", lambda: str(tmp_path / "traces"))
    report = native_contracts.run(["favorite_confirm"])
    row = report["cases"][0]
    assert row["passed"] and row["business_writes"] == 1
    assert [turn["model_calls"] for turn in row["rounds"]] == [2, 1, 1]
    assert [turn["text"] for turn in row["rounds"]][1:] == ["操作完成。", "操作完成。"]


def test_preflight_stops_at_first_failure_without_counting_unattempted_cases(monkeypatch):
    calls = []

    def sample(name, *args):
        calls.append(name)
        return {"case": name, "evaluated": True, "passed": False, "rounds": []}, []

    monkeypatch.setattr(native_contracts, "_sample", sample)
    report = native_contracts.run(["chat", "unread_counts"], fail_fast=True)
    assert calls == ["chat"] and report["stopped_early"]
    assert report["planned_samples"] == 2
    assert report["scores"][-1] == {"case": "unread_counts", "samples": 0,
                                    "evaluated": 0, "passed": 0}


def test_oled_fixture_runs_real_tool_and_never_uses_the_real_device_api():
    from agent.domains import device_actions
    from eval.device_fixture import backend

    with backend() as state:
        result = device_actions.device_oled_display.invoke(
            {"name": "device_oled_display", "args": {"text": "你好，世界"},
             "id": "fixture-display", "type": "tool_call"},
            config={"configurable": {"user_id": 7, "principal": native_contracts._principal()}},
        )
        assert result.artifact["receipts"][0]["device_status"] == "queued"
        assert [request.method for request in state["requests"]] == ["PUT", "GET"]
        assert not state["unsupported_reads"]


def test_article_discovery_counts_actual_native_tool_calls(monkeypatch, tmp_path):
    responses = iter([
        AIMessage(content="", tool_calls=[{"name": "search_notes",
                   "args": {"keyword": "区块链"}, "id": "search"}]),
        AIMessage(content="找到《区块链笔记》：https://saudade.site/article/23"),
    ])

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt, **kwargs):
            return next(responses)

    monkeypatch.setattr("agent.runtime.native.get_llm", lambda **_: Model())
    monkeypatch.setattr(golden_trace, "trace_root", lambda: str(tmp_path / "traces"))
    report = native_contracts.run(["article_discovery"])
    assert report["cases"][0]["passed"], report["cases"]


def test_tool_call_stats_excludes_requested_calls_and_other_nodes(monkeypatch):
    events = [
        {"node": "native", "event": "tool_requested", "name": "not_executed"},
        {"node": "native", "event": "call", "name": "search_notes", "round": 0},
        {"node": "native", "event": "call", "name": "get_article_detail", "round": 1},
        {"node": "planner", "event": "call", "name": "legacy_plan", "round": 2},
    ]
    monkeypatch.setattr("utils.trace.events_of", lambda _: events)
    assert golden_trace.tool_call_stats("fixture") == (["search_notes", "get_article_detail"], 2)
    assert golden_trace.tool_call_stats(None) == ([], 0)


@pytest.mark.parametrize("cancel", [False, True])
def test_admin_compound_task_preserves_approval_stages_and_final_business_state(
    monkeypatch, tmp_path, cancel
):
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from eval.admin_task_contracts import BODY

    def call(name, **args):
        return {"name": name, "args": args, "id": name, "type": "tool_call"}

    responses = [
        AIMessage(content="", tool_calls=[call("load_admin_account_actions_info_tools"),
                                          call("load_admin_notices_info_tools")]),
        AIMessage(content="", tool_calls=[call("list_accounts", username="青叶")]),
        AIMessage(content="", tool_calls=[call("prepare_user_notice", user_id=20, content=BODY),
                                          call("prepare_account_role", account_id=20,
                                               role="zako")]),
    ]
    if cancel:
        responses.extend([
            AIMessage(content="", tool_calls=[call("load_pending_info_tools")]),
            AIMessage(content="", tool_calls=[call("cancel_pending_operation")]),
            AIMessage(content="已取消剩下的任务。")])
    else:
        responses.append(AIMessage(content="通知已发送，账号身份已调整为杂鱼。"))
    remaining = iter(responses)

    class Model:
        def bind_tools(self, tools, **kwargs):
            self.kwargs = {"tools": [convert_to_openai_tool(tool) for tool in tools]}
            return self

        def invoke(self, prompt, **kwargs):
            return next(remaining)

    monkeypatch.setattr("agent.runtime.native.get_llm", lambda **_: Model())
    monkeypatch.setattr(golden_trace, "trace_root", lambda: str(tmp_path / "traces"))
    report = native_contracts.run(["notice_role_cancel" if cancel else "notice_then_role"])
    row = report["cases"][0]
    assert row["passed"] and row["evaluated"], row
    documents = [json.loads(path.read_text()) for path in (tmp_path / "traces").rglob("*.json")]
    phases = [event for doc in documents for event in doc["events"]
              if event.get("event") == "turn_state"]
    approvals = sum(event["phase"] == "awaiting_confirmation" for event in phases)
    assert approvals == (1 if cancel else 2)
    assert row["business_writes"] == (0 if cancel else 2)
    assert all(event.get("caller", {}).get("role") == "admin" for doc in documents
               for event in doc["events"] if event.get("event") == "caller_context")
    assert "confirm_tokens" not in json.dumps(report)


def test_native_outcome_suite_uses_verified_identity_and_redacts_tokens(monkeypatch, tmp_path):
    def call(name, args):
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])

    responses = iter([
        AIMessage(content="你好"),
        call("load_favorites_info_tools", {}),
        call("prepare_favorite_change", {"action": "add", "article_id": 19}),
        call("load_pending_info_tools", {}), call("cancel_pending_operation", {}),
        AIMessage(content="旧操作已取消。"),
        call("load_notifications_info_tools", {}),
        call("prepare_notification_read", {"scope": "all_unread"}),
        AIMessage(content="已标记本次确认的通知为已读。"),
        call("get_unread_counts", {}), AIMessage(content="通知2条，站内信2封，合计4条。"),
        call("load_messages_info_tools", {}), call("list_my_messages", {}),
        AIMessage(content="收件箱里有《欢迎来信》和《回访来信》。"),
        call("load_messages_info_tools", {}), call("list_my_messages", {"message_id": 201}),
        AIMessage(content="这封信的内容是：仅单封正文。"),
        call("load_messages_info_tools", {}),
        call("prepare_message_read", {"scope": "all_unread"}),
        AIMessage(content="已标记本次确认的信件为已读。"),
    ])

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt, **kwargs):
            return next(responses)

    monkeypatch.setattr("agent.runtime.native.get_llm", lambda **_: Model())
    monkeypatch.setattr(golden_trace, "trace_root", lambda: str(tmp_path / "traces"))
    monkeypatch.delenv(golden_trace.ENV_OFF, raising=False)
    report = native_contracts.run(["chat", "favorite_cancel", "notification_replay",
                                   "unread_counts", "message_read_replay"])
    assert all(case["passed"] for case in report["cases"]), report["cases"]
    assert sum(case["requests"] for case in report["cases"]) == 13
    assert all(g["source"] == "evaluation" and g["path"] == "native"
               for g in report["metrics"]["groups"])
    documents = [json.loads(path.read_text()) for path in tmp_path.rglob("*.json")]
    terminal = [event for doc in documents for event in doc["events"]
                if event.get("kind") in {"prepared", "cancelled"}]
    assert len(terminal) == 4
    assert all("args" not in event and "result" not in event for event in terminal)
    assert all("pending_confirm" not in event and "token" not in event for event in terminal)
    assert "confirm_tokens" not in json.dumps(report)


def test_navigation_samples_keep_failed_attempts_and_real_effects(monkeypatch, tmp_path):
    from itertools import count

    from tools import base

    sequence = count()
    url = base.SITE_BASE.rstrip("/") + "/article/719"

    def call(name, args):
        return AIMessage(content="", tool_calls=[{
            "name": name, "args": args, "id": f"eval-{next(sequence)}"}])

    def recommendation():
        return [call("load_public_catalog_info_tools", {}), call("get_top_notes", {}),
                AIMessage(content="推荐《Git 分支实践》：" + url)]

    def navigate():
        return [call("navigate_page", {"path": "/article/719"}),
                AIMessage(content="已发送跳转指令：" + url)]

    # A link-only combined response must remain a failure, even if a later sample succeeds.
    responses = iter([*recommendation(), *recommendation(), *navigate(),
                      *recommendation(), *navigate(),
                      *recommendation(), *recommendation(), *navigate(),
                      call("load_public_catalog_info_tools", {}), call("get_top_notes", {}),
                      *navigate(), *navigate()])

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt, **kwargs):
            return next(responses).model_copy(update={"usage_metadata": {
                "input_tokens": 100, "output_tokens": 10, "total_tokens": 110}})

    monkeypatch.setattr("agent.runtime.native.get_llm", lambda **_: Model())
    monkeypatch.setattr(golden_trace, "trace_root", lambda: str(tmp_path / "traces"))
    monkeypatch.delenv(golden_trace.ENV_OFF, raising=False)
    report = native_contracts.run([case[0] for case in native_contracts.navigation_contracts.CASES],
                                  repeat=2)
    combined = [row for row in report["cases"] if row["case"] == "navigation_combined"]
    assert [row["passed"] for row in combined] == [False, True]
    assert all(row["passed"] for row in report["cases"] if row not in combined)
    assert next(row for row in report["scores"] if row["case"] == "navigation_combined") == {
        "case": "navigation_combined", "samples": 2, "evaluated": 2, "passed": 1}
    assert all(row["business_writes"] == 0 for row in report["cases"])
    traces = [round_["trace"] for row in report["cases"] for round_ in row["rounds"]]
    assert len(traces) == len(set(traces)) == 10
    assert all(round_["charged_tokens"] == round_["model_calls"] * 110
               for row in report["cases"] for round_ in row["rounds"])
    assert "confirm_tokens" not in json.dumps(report)


def test_navigation_fixture_supports_alternative_article_reads():
    from agent.domains import public_catalog
    from tools import notes

    with native_contracts.navigation_contracts.backend() as state:
        catalog = public_catalog.get_top_notes.invoke({})
        categories = public_catalog.list_categories.invoke({})
        searched = notes.search_notes.invoke({"keyword": "Git"})
        retrieved = notes.rag_search.invoke({"query": "Git"})
        detail = notes.get_article_detail.invoke({"article_id": 719})
        assert all(result.kind == "ok" for result in (
            catalog, categories, searched, retrieved, detail))
        assert json.loads(str(categories))["items"][0]["id"] == 11
        assert "719" in str(searched) and "id=719" in str(retrieved)
        assert "Git 分支实践" in str(detail)
        assert not native_contracts._posts(state)
        assert not state["unsupported_reads"]


def test_unsupported_fixture_read_is_not_a_model_quality_score(monkeypatch, tmp_path):
    from tools import base

    def call(name, args):
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])

    responses = iter([call("load_site_info_tools", {}), call("get_blog_info", {}),
                      call("navigate_page", {"path": "/article/719"}),
                      AIMessage(content="已发送指令：" + base.SITE_BASE + "/article/719")])

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt, **kwargs):
            return next(responses)

    monkeypatch.setattr("agent.runtime.native.get_llm", lambda **_: Model())
    monkeypatch.setattr(golden_trace, "trace_root", lambda: str(tmp_path / "traces"))
    monkeypatch.delenv(golden_trace.ENV_OFF, raising=False)
    report = native_contracts.run(["navigation_direct"])
    sample = report["cases"][0]
    assert not sample["passed"] and not sample["evaluated"]
    assert sample["reason"] == "unsupported fixture read" and sample["unsupported_reads"]
    assert report["scores"] == [{"case": "navigation_direct", "samples": 1,
                                 "evaluated": 0, "passed": 0}]
