"""Summary approval binds a private payload without claiming a browser write."""

import json
import secrets
from types import SimpleNamespace

import pytest
from _framework_ledger import Ledger
from langchain_core.messages import ToolMessage

from agent import confirm
from agent.domains import article_editor, article_summary
from agent.principal import SOURCE_ASSERTION, Principal
from agent.runtime import approval
from agent.runtime.results import TurnResult, approval_observations
from tools.result import unavailable


@pytest.fixture
def prepared(monkeypatch):
    monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
    config = {"configurable": {"user_id": 7, "conversation_id": 42, "thread_id": "conv_42",
        "principal": Principal(7, "admin", SOURCE_ASSERTION),
        "browser_feedback": True, "browser_task": "edit-task"}}
    runtime = SimpleNamespace(config=config, tool_call_id="summary", state={"messages": [
        ToolMessage(name="read_current_article", tool_call_id="read", content=json.dumps({
            "scope": "editor_draft", "editor_id": "editor", "revision": 3, "description": ""}))]})
    result = article_editor.prepare_article_summary.func(
        editor_id="editor", revision=3, description="新摘要", runtime=runtime)
    grant = confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)
    return config, result.execution["pending_action"], grant


@pytest.mark.parametrize("tamper", ["content", "operation", "missing"])
def test_checkpoint_payload_must_match_signed_digest(prepared, monkeypatch, tmp_path, tamper):
    config, pending, grant = prepared
    if tamper == "content":
        pending["payload"]["description"] += "篡改"
    elif tamper == "operation":
        pending["jti"] = "other"
    else:
        pending.pop("payload")
    monkeypatch.setattr(article_summary, "CheckpointConversationStore", lambda _: SimpleNamespace(
        load=lambda _: SimpleNamespace(pending_action=pending)))
    ledger = Ledger(str(tmp_path / "summary.sqlite3"))
    result = article_summary.execute(grant, config, None, ledger=ledger)
    assert result["blocked"] and not result["receipts"]
    assert not result.get("editor_summary_command")
    args = grant["specs"][0]["args"]
    assert ledger.lookup(article_summary.writes.operation_key(grant, "set_article_summary", args)) \
        is None


def test_approval_result_is_not_an_execution_receipt(prepared, monkeypatch, tmp_path):
    config, pending, grant = prepared
    monkeypatch.setattr(article_summary, "CheckpointConversationStore", lambda _: SimpleNamespace(
        load=lambda _: SimpleNamespace(pending_action=pending)))
    ledger = Ledger(str(tmp_path / "summary.sqlite3"))
    execution = article_summary.execute(grant, config, None, ledger=ledger)
    assert not execution["receipts"]
    assert execution["editor_summary_command"]["description"] == "新摘要"
    assert approval.has_result(execution, grant["specs"], jti=grant["jti"])
    assert not approval.has_result(execution, grant["specs"], jti="wrong")
    assert not approval.has_result(execution, [{"tool": "other", "args": {}}], jti=grant["jti"])
    messages = approval_observations(TurnResult(execution["reply"], execution))
    fact = json.loads(messages[-1].content)
    assert fact["status"] == "approved" and fact["executed"] is False
    replay_config = {"configurable": {**config["configurable"], "confirmation_replay_only": True}}
    replay = article_summary.execute(grant, replay_config, None, ledger=ledger)
    assert not replay.get("editor_summary_command") and not replay["receipts"]
    operation = grant["specs"][0]
    key = article_summary.writes.operation_key(grant, operation["tool"], operation["args"])
    ledger.finish(key, unavailable("cancelled"))
    revoked = article_summary.execute(grant, config, None, ledger=ledger)
    assert revoked["blocked"] and not revoked["receipts"]
    assert not revoked.get("editor_summary_command")


@pytest.mark.parametrize("role,uid,cid", [("user", 7, 42), ("admin", 8, 42), ("admin", 7, 43)])
def test_approval_cannot_cross_identity_or_conversation(prepared, role, uid, cid):
    config, _, grant = prepared
    config["configurable"].update(user_id=uid, conversation_id=cid, thread_id=f"conv_{cid}",
                                  principal=Principal(uid, role, SOURCE_ASSERTION))
    with pytest.raises(ValueError):
        article_summary.validate(grant, config)
