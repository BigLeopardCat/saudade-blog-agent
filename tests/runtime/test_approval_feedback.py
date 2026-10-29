"""User feedback resumes the original durable task and supersedes its old preview."""

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage
from test_confirmation_continuation import FAVORITE, LOAD, Model, call, prepare, run, runtime
from test_confirmation_continuation import backend as backend_fixture

from agent import confirm
from agent.runtime import approval, writes
from agent.runtime.task_inputs import TaskInputEvent
from agent.runtime.turns import AgentCancelled

backend = backend_fixture


class StoppedModel(Model):
    def stream(self, *args, **kwargs):
        raise AgentCancelled()


def test_feedback_stop_reconstruct_and_reconfirm_keeps_one_task_and_write(backend):
    state, _ = backend
    selected = runtime(Model())
    _, old_grant, _ = prepare(selected, FAVORITE)
    task = selected.store.load(config()).run["id"]
    selected.model_factory = lambda **_: StoppedModel()
    text = "改成收藏文章20。"
    with pytest.raises(AgentCancelled):
        run(selected, text)
    saved = selected.store.load(config())
    assert saved.run["id"] == task and saved.unfinished and not saved.has_pending
    replacement = Model(AIMessage(content="", tool_calls=[call(
        "prepare_favorite_change", "revised", action="add", article_id=20)]))
    rebuilt = runtime(replacement, selected.saver)
    card = list(rebuilt.stream_events({"resume_task": task}, config()))[-1]
    assert not run(rebuilt, grant=old_grant).execution["receipts"]
    new_grant = confirm.verify(card.execution["pending_confirm"]["token"], 7, 42)
    rebuilt.model_factory = lambda **_: Model(AIMessage(content="已收藏文章20。"))
    assert run(rebuilt, grant=new_grant).reply == "已收藏文章20。"
    assert state["favorites"] == {20} and len(state["writes"]) == 1
    assert sum(isinstance(m, HumanMessage) and m.content == text
               for m in rebuilt.store.load(config()).messages) == 1, [
                   (m.type, m.content) for m in rebuilt.store.load(config()).messages]


def test_feedback_review_failure_retries_revocation_without_repeating_it(backend, monkeypatch):
    state, ledger = backend
    selected = runtime(Model())
    prepare(selected, FAVORITE)
    task = selected.store.load(config()).run["id"]
    review, finish = approval.review, ledger.finish
    revoked = []

    def counted(*args, **kwargs):
        revoked.append(1)
        return finish(*args, **kwargs)

    def failed(state, config, **kwargs):
        review(state, config, **kwargs)
        raise OSError("feedback checkpoint unavailable")

    monkeypatch.setattr(ledger, "finish", counted)
    monkeypatch.setattr(approval, "review", failed)
    unused_model = Model()
    selected.model_factory = lambda **_: unused_model
    text = "先不要收藏，解释一下。"
    with pytest.raises(OSError):
        run(selected, text)
    assert not unused_model.prompts
    monkeypatch.setattr(approval, "review", review)
    rebuilt = runtime(Model(AIMessage(content="这项操作没有执行。")), selected.saver)
    result = list(rebuilt.stream_events({"resume_task": task}, config()))[-1]
    assert result.reply
    assert revoked == [1] and not state["writes"]
    assert sum(isinstance(m, HumanMessage) and m.content == text
               for m in rebuilt.store.load(config()).messages) == 1, [
                   (m.type, m.content) for m in rebuilt.store.load(config()).messages]


def test_feedback_navigation_keeps_interrupt_and_rejects_the_old_grant(backend):
    state, _ = backend
    selected = runtime(Model())
    _, grant, _ = prepare(selected, FAVORITE)
    task = selected.store.load(config()).run["id"]
    selected.model_factory = lambda **_: Model(AIMessage(content="", tool_calls=[
        call("navigate_page", "navigation", path="/guestbook")]))
    paused = run(selected, "先去留言板，不收藏了。", cfg=config(browser_feedback=True))
    command = paused.execution["client_action"]
    assert command["task_id"] == task and selected.get_state(config(), subgraphs=True).interrupts
    assert not run(selected, grant=grant).execution["receipts"]
    assert selected.store.load(config()).run["id"] == task
    cfg = config(browser_feedback=True, browser_result={
        "command_id": command["command_id"], "path": "/guestbook", "status": "completed"})
    rebuilt = runtime(Model(AIMessage(content="已到留言板。")), selected.saver)
    assert list(rebuilt.stream_events({"resume_task": task}, cfg))[-1].reply == "已到留言板。"
    assert not state["writes"] and not rebuilt.store.load(cfg).unfinished


def test_unknown_operation_cannot_be_superseded_or_reported_cancelled(backend):
    state, ledger = backend
    selected = runtime(Model())
    _, grant, _ = prepare(selected, FAVORITE)
    name, args = selected.application.confirmation(grant).validate(grant, config())
    ledger.claim(writes.operation_key(grant, name, args), name, grant["jti"])
    unused_model = Model()
    selected.model_factory = lambda **_: unused_model
    with pytest.raises(ValueError, match="不能"):
        run(selected, "取消吧")
    assert not unused_model.prompts
    assert selected.store.load(config()).has_pending and not state["writes"]


def test_feedback_reports_original_task_identity_and_seals_inputs_after_recovery(backend):
    cfg = config(task_inputs=True)
    selected = runtime(Model(LOAD, AIMessage(content="", tool_calls=[FAVORITE])))
    run(selected, "收藏文章19", cfg=cfg)
    task = selected.store.load(cfg).run
    assert selected.inputs.read(task["id"], task["scope"])["phase"] == "paused"
    selected.model_factory = lambda **_: StoppedModel()
    stream = selected.stream_events({"messages": [HumanMessage(content="先解释一下")],
                                     "task_id": "new-http-request"}, cfg)
    event = next(stream)
    assert isinstance(event, TaskInputEvent) and event.data["task_id"] == task["id"]
    with pytest.raises(AgentCancelled):
        list(stream)
    rebuilt = runtime(Model(AIMessage(content="这项操作需要你的明确确认。")), selected.saver)
    list(rebuilt.stream_events({"resume_task": task["id"]}, cfg))
    assert rebuilt.inputs.read(task["id"], task["scope"])["phase"] == "completed"


def test_feedback_projects_current_page_facts_without_changing_saved_user_text(backend):
    selected = runtime(Model())
    prepare(selected, FAVORITE)
    model = Model(AIMessage(content="先核实新页面。"))
    selected.model_factory = lambda **_: model
    incoming = [HumanMessage(content="unrendered host context", additional_kwargs={
        "memory_context": True,
        "page_context": {"title": "反馈时的新页面", "url": "/guestbook"},
        "request_context": {"current_time": "2026-10-30T10:00:00+08:00"}}),
        HumanMessage(content="先解释当前页面。")]
    list(selected.stream_events({"messages": incoming}, config()))
    prompt = model.prompts[0]
    assert "反馈时的新页面" in prompt[-1].content
    assert "2026-10-30T10:00:00+08:00" in prompt[-1].content
    assert "unrendered host context" not in str(prompt)
    saved = selected.store.load(config()).messages
    assert sum(isinstance(m, HumanMessage) and m.content == "先解释当前页面。"
               for m in saved) == 1
    assert "反馈时的新页面" not in str(saved)
