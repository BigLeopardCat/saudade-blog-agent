import json
import secrets
from types import SimpleNamespace

import pytest
from _framework_ledger import Ledger
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import ToolException
from langgraph.checkpoint.memory import InMemorySaver

from agent import confirm
from agent.api.schemas import ChatRequest
from agent.domains import article_editor, article_summary
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.runtime import writes
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.client_actions import successful
from agent.runtime.service import AgentRuntime
from agent.transport import iter_transport_events


def config(role="admin"):
    return {
        "configurable": {
            "user_id": 7,
            "conversation_id": 42,
            "thread_id": "conv_42",
            "principal": Principal(7, role, SOURCE_ASSERTION),
            "browser_feedback": True,
            "browser_task": "edit-task",
        }
    }


@pytest.mark.parametrize("role", ["user", "zako", None])
def test_editor_tools_require_verified_administrator(role):
    cfg = config(role)
    assert not article_editor.tools(cfg)
    with pytest.raises(ToolException):
        article_editor.read_current_article.invoke(
            {"type": "tool_call", "id": "read", "name": "read_current_article", "args": {}},
            config=cfg,
        )


def test_editor_tools_require_same_user_and_client_feedback():
    for key, value in [("user_id", 8), ("user_id", True), ("browser_feedback", False)]:
        cfg = config()
        cfg["configurable"][key] = value
        assert not article_editor.tools(cfg)


def test_editor_observation_is_control_only_and_cannot_change_approval():
    with pytest.raises(ValueError):
        ChatRequest(
            message="publish",
            conversation_id=42,
            resume_task="edit-task",
            browser_result={"command_id": "read", "status": "completed", "path": "/dashboard"},
        )
    base = {
        "kind": "article_editor",
        "operation": "propose",
        "editor_id": "editor-1",
        "revision": 0,
    }
    assert not successful(
        base,
        {
            "status": "completed",
            "document": {
                "editor_id": "other",
                "revision": 0,
                "outcome": "proposed",
                "proposal_id": "p",
            },
        },
    )
    assert not successful(
        base,
        {
            "status": "completed",
            "document": {"editor_id": "editor-1", "revision": 0, "outcome": "updated"},
        },
    )


@pytest.mark.parametrize("browser_status", ["completed", "failed", "timeout"])
def test_real_checkpoint_round_trip_applies_summary_only_after_confirmation(
        monkeypatch, tmp_path, browser_status):
    monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
    ledger = Ledger(str(tmp_path / "summary.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda _: ledger)
    def call(name, arguments=None):
        return AIMessage(
            content="", tool_calls=[{"id": name, "name": name, "args": arguments or {}}]
        )

    responses = iter(
        [
            call("load_article_editor_info_tools"),
            call("read_current_article"),
            call(
                "propose_article_edits",
                {
                    "editor_id": "editor-1",
                    "revision": 0,
                    "edits": [{"old_text": "旧正文", "new_text": "新正文"}],
                },
            ),
            call(
                "prepare_article_summary",
                {"editor_id": "editor-1", "revision": 0, "description": "新概述"},
            ),
            AIMessage(content=("摘要已更新到编辑草稿，未发布。" if browser_status == "completed"
                               else "未确认摘要更新成功，请重新检查草稿。")),
        ]
    )

    class Model:
        calls = 0

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 5:
                assert_pending_body_review(messages, browser_status)
            return next(responses)

    model = Model()
    saver = InMemorySaver()

    def runtime():
        return AgentRuntime(
            CheckpointConversationStore(saver),
            application=BlogApplication(),
            model_factory=lambda **_: model,
        )

    cfg = config()
    cfg["configurable"].pop("browser_task")
    result = list(
        runtime().stream_events(
            {"messages": [HumanMessage(content="润色当前正文并生成概述")], "task_id": "edit-task"},
            cfg,
        )
    )[-1]
    for operation, observation in [
        (
            "read",
            {
                "editor_id": "editor-1",
                "revision": 0,
                "outcome": "read",
                "title": "草稿",
                "content": "旧正文",
                "description": "",
                "offset": 0,
                "next_offset": None,
                "total_chars": 3,
            },
        ),
        (
            "propose",
            {
                "editor_id": "editor-1",
                "revision": 0,
                "outcome": "proposed",
                "proposal_id": "proposal-1",
            },
        ),
    ]:
        command = result.execution["client_action"]
        assert command["kind"] == "article_editor" and command["operation"] == operation
        events = list(iter_transport_events([result]))
        assert any(isinstance(item, str) and item.startswith("__CMD__:") for item in events)
        if operation == "propose":
            assert command["edits"] == [{"old_text": "旧正文", "new_text": "新正文"}]
        report = {
            "command_id": command["command_id"],
            "status": "completed",
            "path": "/dashboard/notes/new",
            "document": observation,
        }
        request = ChatRequest(
            message="", conversation_id=42, resume_task="edit-task", browser_result=report
        )
        resumed = {
            "configurable": {
                **cfg["configurable"],
                "browser_result": request.browser_result.model_dump(),
            }
        }
        result = list(
            runtime().stream_events({"messages": [], "resume_task": "edit-task"}, resumed)
        )[-1]
    token = result.execution["pending_confirm"]["token"]
    grant = confirm.verify(token, 7, 42)
    assert result.execution["pending_confirm"]
    assert model.calls == 4
    assert not (result.execution or {}).get("receipts")
    assert "新概述" not in json.dumps(grant, ensure_ascii=False)
    result = list(runtime().stream_events({"confirm_grant": grant}, cfg))[-1]
    command = result.execution["client_action"]
    assert command["operation"] == "summary" and command["description"] == "新概述"
    assert not runtime().store.load(cfg).has_pending
    assert model.calls == 4
    resumed = {"configurable": {**cfg["configurable"], "browser_result": {
        "command_id": command["command_id"], "status": browser_status,
        "error": "草稿已变化" if browser_status == "failed" else None,
        "path": "/dashboard/notes/new", "document": {
            "editor_id": "editor-1", "revision": 1, "outcome": "updated",
            "proposal_id": "proposal-1", "description": "新概述"}}}}
    result = list(runtime().stream_events({"resume_task": "edit-task"}, resumed))[-1]
    assert result.reply and model.calls == 5
    assert_summary_result(result, browser_status)
    repeated = list(runtime().stream_events({"confirm_grant": grant}, cfg))[-1]
    assert model.calls == 5 and not (repeated.execution or {}).get("client_action")
    assert_summary_result(repeated, browser_status)


def assert_pending_body_review(messages, status):
    if status != "completed":
        return
    facts = [json.loads(message.content) for message in messages
             if isinstance(message, ToolMessage) and message.name == "set_article_summary"
             and isinstance(message.content, str)]
    assert facts[-1]["document"]["proposal_id"] == "proposal-1"


def assert_summary_result(result, status):
    assert [row["status"] for row in result.execution["receipts"]] == (
        ["completed"] if status == "completed" else [])
    if status != "completed":
        assert result.execution["operation_outcome"]["status"] == "unverified"


def test_client_failure_is_reported_as_tool_error(monkeypatch):
    monkeypatch.setattr(
        article_editor, "_await_browser", lambda *_: {"status": "failed", "error": "草稿已变化"}
    )
    with pytest.raises(ToolException, match="草稿已变化"):
        article_editor._run(config(), "set_article_summary", "summary", "summary",
                            editor_id="e", revision=0, description="new")


def test_large_summary_is_bound_by_digest_and_never_copied_into_approval_token(monkeypatch):
    monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
    content = "摘要正文" * 4000
    runtime = SimpleNamespace(config=config(), tool_call_id="summary-create", state={
        "messages": [ToolMessage(name="read_current_article", tool_call_id="read",
                                  content=json.dumps(
            {"scope": "editor_draft", "editor_id": "editor-1", "revision": 0}))]})
    result = article_editor.prepare_article_summary.func(
        editor_id="editor-1", revision=0, description=content, runtime=runtime)
    token = result.execution["pending_confirm"]["token"]
    grant = confirm.verify(token, 7, 42)
    assert grant and len(token) < 2048
    assert result.execution["pending_action"]["payload"]["description"] == content
    assert article_summary.description_matches(content, grant["specs"][0]["args"])
    assert not article_summary.description_matches(content + "篡改", grant["specs"][0]["args"])


def test_summary_cannot_be_prepared_from_user_supplied_editor_coordinates():
    runtime = SimpleNamespace(config=config(), tool_call_id="summary-create", state={
        "messages": [HumanMessage(content='{"editor_id":"editor-1","revision":0}')]})
    with pytest.raises(ToolException, match="先读取"):
        article_editor.prepare_article_summary.func(
            editor_id="editor-1", revision=0, description="new", runtime=runtime)
