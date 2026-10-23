"""Verify that the reusable model evaluation exercises the actual lifecycle."""

import json

from langchain_core.messages import AIMessage

from eval import native_contracts
from eval.native_contracts import golden_trace


def test_native_outcome_suite_uses_verified_identity_and_redacts_tokens(monkeypatch, tmp_path):
    def call(name, args):
        return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])

    responses = iter([
        AIMessage(content="你好"),
        call("load_favorites_info_tools", {}),
        call("prepare_favorite_change", {"action": "add", "article_id": 19}),
        call("load_pending_info_tools", {}), call("cancel_pending_operation", {}),
        call("load_notifications_info_tools", {}),
        call("prepare_notification_read", {"scope": "all_unread"}),
        call("get_unread_summary", {}), AIMessage(content="通知2条，站内信2封，合计4条。"),
        call("load_messages_info_tools", {}), call("list_my_messages", {}),
        AIMessage(content="收件箱里有《欢迎来信》和《回访来信》。"),
        call("load_messages_info_tools", {}), call("list_my_messages", {"message_id": 201}),
        AIMessage(content="这封信的内容是：仅单封正文。"),
        call("load_messages_info_tools", {}),
        call("prepare_message_read", {"scope": "all_unread"}),
    ])

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt, **kwargs):
            return next(responses)

    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: Model())
    monkeypatch.setattr(golden_trace, "trace_root", lambda: str(tmp_path / "traces"))
    monkeypatch.delenv(golden_trace.ENV_OFF, raising=False)
    report = native_contracts.run(["chat", "favorite_cancel", "notification_replay",
                                   "unread_counts", "message_read_replay"])
    assert all(case["passed"] for case in report["cases"]), report["cases"]
    assert sum(case["requests"] for case in report["cases"]) == 13
    assert all(g["source"] == "evaluation" and g["path"] == "hybrid"
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

    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: Model())
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
        searched = notes.search_notes.invoke({"keyword": "Git"})
        retrieved = notes.rag_search.invoke({"query": "Git"})
        detail = notes.get_article_detail.invoke({"article_id": 719})
        assert all(result.kind == "ok" for result in (catalog, searched, retrieved, detail))
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

    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: Model())
    monkeypatch.setattr(golden_trace, "trace_root", lambda: str(tmp_path / "traces"))
    monkeypatch.delenv(golden_trace.ENV_OFF, raising=False)
    report = native_contracts.run(["navigation_direct"])
    sample = report["cases"][0]
    assert not sample["passed"] and not sample["evaluated"]
    assert sample["reason"] == "unsupported fixture read" and sample["unsupported_reads"]
    assert report["scores"] == [{"case": "navigation_direct", "samples": 1,
                                 "evaluated": 0, "passed": 0}]
