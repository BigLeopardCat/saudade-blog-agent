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
        call("load_own_info_tools", {}), call("list_my_messages", {}),
        AIMessage(content="收件箱里有《欢迎来信》和《回访来信》。"),
        call("load_messages_info_tools", {}),
        call("prepare_message_read", {"scope": "all_unread"}),
    ])

    class Model:
        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, prompt):
            return next(responses)

    monkeypatch.setattr("agent.runtime.hybrid.get_llm", lambda **_: Model())
    monkeypatch.setattr(golden_trace, "trace_root", lambda: str(tmp_path / "traces"))
    monkeypatch.delenv(golden_trace.ENV_OFF, raising=False)
    report = native_contracts.run()
    assert all(case["passed"] for case in report["cases"]), report["cases"]
    assert sum(case["requests"] for case in report["cases"]) == 11
    assert all(g["source"] == "evaluation" and g["path"] == "hybrid"
               for g in report["metrics"]["groups"])
    documents = [json.loads(path.read_text()) for path in tmp_path.rglob("*.json")]
    terminal = [event for doc in documents for event in doc["events"]
                if event.get("kind") in {"prepared", "cancelled"}]
    assert len(terminal) == 4
    assert all("args" not in event and "result" not in event for event in terminal)
    assert all("pending_confirm" not in event and "token" not in event for event in terminal)
    assert "confirm_tokens" not in json.dumps(report)
