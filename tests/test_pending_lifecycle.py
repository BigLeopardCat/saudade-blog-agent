"""Cancellation revokes an old approval without truncating the current request."""

import json
import secrets
import threading
import time
from copy import deepcopy

import httpx
import pytest
from _framework_ledger import Ledger
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from agent import confirm
from agent.domains.favorites import transactions
from agent.products.blog.pending import PendingOperation
from agent.runtime import writes
from agent.runtime.control import RequestControl, request_scope
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.turns import AgentCancelled
from tools import base


def call(name, identifier, **args):
    return {"name": name, "args": args, "id": identifier}


def batch(*calls):
    return AIMessage(content="", tool_calls=list(calls))


LOAD = batch(call("load_pending_info_tools", "load-pending"),
             call("load_favorites_info_tools", "load-favorites"))
CANCEL = call("cancel_pending_operation", "cancel-old")
REPLACE = call("prepare_favorite_change", "prepare-new", action="add", article_id=20)


class Model:
    max_tokens = 128

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.prompts = []

    def bind_tools(self, *args, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        self.prompts.append(deepcopy(messages))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


@pytest.fixture
def pending_task(monkeypatch, tmp_path):
    requests, stop, saver = [], threading.Event(), InMemorySaver()

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/favorites"):
            data = []
        elif request.url.path.endswith(("/notes/19", "/notes/20")):
            ident = int(request.url.path.rsplit("/", 1)[-1])
            data = {"noteKey": ident, "noteTitle": f"文章{ident}"}
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"code": 200, "data": data})

    ledger = Ledger(str(tmp_path / "lifecycle.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda _: ledger)
    monkeypatch.setattr(confirm.settings, "jwt_secret", secrets.token_urlsafe(32))
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(base, "_client", client)
        cfg = config(stop_event=stop)

        def runtime(model):
            return NativeRuntime(NativeConversationStore(saver), model_factory=lambda **_: model)

        selected = runtime(Model(batch(call("load_favorites_info_tools", "load-original")),
                                 batch(call("prepare_favorite_change", "prepare-original",
                                            action="add", article_id=19))))
        original = list(selected.stream_events({
            "messages": [HumanMessage(content="收藏文章19")], "task_id": "original"}, cfg))[-1]
        token = original.execution["pending_confirm"]["token"]
        grant = confirm.verify(token, 7, 42)
        yield runtime, cfg, stop, selected, grant, token, requests, ledger


def execute(selected, cfg, *, text=None, resume=None):
    state = ({"resume_task": resume} if resume else {
        "messages": [HumanMessage(content=text)], "task_id": "replacement"})
    return list(selected.stream_events(state, cfg))[-1]


def assert_revoked(selected, cfg, grant, requests):
    result = transactions.execute(grant, cfg, selected.saver)
    assert not result["receipts"] and result["blocked"]
    assert not any(request.method != "GET" for request in requests)


@pytest.mark.parametrize("same_batch", [False, True])
def test_cancel_then_prepare_keeps_current_request_and_revokes_old_card(pending_task, same_batch):
    runtime, cfg, _, _, grant, token, requests, _ = pending_task
    model = Model(LOAD, *([batch(CANCEL, REPLACE)] if same_batch else
                          [batch(CANCEL), batch(REPLACE)]))
    selected = runtime(model)
    result = execute(selected, cfg, text="取消之前的收藏，改为收藏文章20")
    replacement = result.execution["pending_confirm"]
    assert replacement["token"] != token
    assert result.execution["pending_event"]["jti"] == grant["jti"]
    assert confirm.verify(replacement["token"], 7, 42)["specs"][0]["args"] == {"article_id": 20}
    assert selected.store.load(cfg).approval["jti"] != grant["jti"]
    if not same_batch:
        observation = next(m for m in model.prompts[-1]
                           if isinstance(m, ToolMessage) and m.tool_call_id == CANCEL["id"])
        assert "取消" in observation.content and token not in str(model.prompts)
    assert_revoked(selected, cfg, grant, requests)
    list(selected.stream_events({"confirm_grant": grant}, cfg))
    assert selected.store.load(cfg).pending_confirm == replacement


def test_pure_cancel_returns_a_model_reply_and_preserves_cancellation_fact(pending_task):
    runtime, cfg, _, _, grant, token, requests, _ = pending_task
    model = Model(LOAD, batch(CANCEL), AIMessage(content="好，之前那项已经取消啦。"))
    selected = runtime(model)
    result = execute(selected, cfg, text="取消刚才的收藏")
    assert result.reply == "好，之前那项已经取消啦。"
    assert result.execution["pending_event"]["jti"] == grant["jti"]
    assert len(model.prompts) == 3 and token not in str(model.prompts)
    snapshot = selected.store.load(cfg)
    assert not snapshot.approval and not snapshot.has_pending and not snapshot.run
    assert any(isinstance(m, ToolMessage) and m.name == CANCEL["name"] for m in snapshot.messages)
    assert_revoked(selected, cfg, grant, requests)


@pytest.mark.parametrize("same_batch", [False, True])
@pytest.mark.parametrize("request_control", [False, True])
def test_cancel_boundary_survives_stop_and_resumes_without_reviving_approval(
        pending_task, monkeypatch, same_batch, request_control):
    runtime, cfg, stop, _, grant, token, requests, _ = pending_task
    cancel = PendingOperation.cancel

    def stop_after_cancel(self, *args):
        result = cancel(self, *args)
        stop.set()
        return result

    monkeypatch.setattr(PendingOperation, "cancel", stop_after_cancel)
    model = Model(LOAD, batch(CANCEL, REPLACE) if same_batch else batch(CANCEL))
    selected = runtime(model)
    with pytest.raises(AgentCancelled):
        if request_control:
            with request_scope(RequestControl(stop, time.monotonic() + 30)):
                execute(selected, cfg, text="取消旧收藏，再收藏文章20")
        else:
            execute(selected, cfg, text="取消旧收藏，再收藏文章20")
    saved = selected.store.load(cfg)
    assert not saved.has_pending and not saved.approval
    assert saved.unfinished and saved.run["id"] == "replacement"
    stop.clear()
    resumed_model = Model(*([] if same_batch else [batch(REPLACE)]))
    restored = runtime(resumed_model)
    final = execute(restored, cfg, resume="replacement")
    assert final.execution["pending_event"]["jti"] == grant["jti"]
    new = confirm.verify(final.execution["pending_confirm"]["token"], 7, 42)
    assert new["specs"][0]["args"] == {"article_id": 20}
    assert len(resumed_model.prompts) == int(not same_batch)
    assert token not in str(resumed_model.prompts)
    history = restored.store.load(cfg).messages
    assert sum(isinstance(m, HumanMessage) and m.content == "取消旧收藏，再收藏文章20"
               for m in history) == 1
    assert_revoked(restored, cfg, grant, requests)


@pytest.mark.parametrize("state", ["intent", "done"])
def test_unknown_or_completed_old_write_blocks_cancellation_and_replacement(pending_task, state):
    runtime, cfg, _, _, grant, _, requests, ledger = pending_task
    name, args = transactions.validate(grant, cfg)
    key = writes.operation_key(grant, name, args)
    ledger.claim(key, name, grant["jti"])
    if state == "done":
        ledger.finish(key, base.ok("业务已经执行"))
    model = Model(LOAD, batch(CANCEL, REPLACE))
    selected = runtime(model)
    result = execute(selected, cfg, text="取消旧收藏，再收藏文章20")
    assert not result.execution and "不能" in result.reply
    assert selected.store.load(cfg).pending_action["jti"] == grant["jti"]
    assert not any(request.url.path.endswith("/notes/20") for request in requests)
    assert len(model.prompts) == 2


def test_reads_after_revocation_are_checkpointed_before_the_next_model_round(pending_task):
    runtime, cfg, _, _, grant, _, requests, _ = pending_task
    first = Model(LOAD, batch(CANCEL), batch(call("list_my_favorites", "read-after-cancel")),
                  AgentCancelled())
    selected = runtime(first)
    with pytest.raises(AgentCancelled):
        execute(selected, cfg, text="取消旧操作，查询我的收藏并说明")
    before = len(requests)
    model = Model(AIMessage(content="已取消旧操作，收藏夹目前为空。"))
    restored = runtime(model)
    result = execute(restored, cfg, resume="replacement")
    assert len(requests) == before
    assert result.execution["pending_event"]["jti"] == grant["jti"]
    assert not restored.store.load(cfg).has_pending
    observed = [m for m in model.prompts[0] if isinstance(m, ToolMessage)]
    assert sum(m.tool_call_id == CANCEL["id"] for m in observed) == 1
    assert sum(m.tool_call_id == "read-after-cancel" for m in observed) == 1
    assert_revoked(restored, cfg, grant, requests)


def test_failed_boundary_checkpoint_cannot_reenable_the_revoked_signature(
        pending_task, monkeypatch):
    runtime, cfg, _, original, grant, _, requests, _ = pending_task

    def fail(*args, **kwargs):
        raise RuntimeError("checkpoint unavailable")

    with monkeypatch.context() as changes:
        changes.setattr(original.saver, "put", fail)
        selected = runtime(Model(LOAD, batch(CANCEL)))
        with pytest.raises(RuntimeError, match="checkpoint unavailable"):
            execute(selected, cfg, text="取消刚才的收藏")
    # The old display checkpoint remains, but its authorization was durably revoked.
    assert original.store.load(cfg).pending_action["jti"] == grant["jti"]
    assert_revoked(original, cfg, grant, requests)
    selected = runtime(Model(LOAD, batch(CANCEL), AIMessage(content="旧操作已取消。")))
    result = execute(selected, cfg, text="取消刚才的收藏")
    assert result.execution["pending_event"]["jti"] == grant["jti"]
    assert not selected.store.load(cfg).has_pending


def test_verified_pending_can_be_cancelled_without_loading_another_domain(pending_task):
    runtime, cfg, _, _, grant, _, requests, _ = pending_task
    model = Model(batch(CANCEL), AIMessage(content="已取消，还没有更改收藏。"))
    selected = runtime(model)
    result = execute(selected, cfg, text="取消这次收藏")
    assert result.reply == "已取消，还没有更改收藏。"
    assert not result.execution.get("pending_confirm")
    assert len(model.prompts) == 2
    assert_revoked(selected, cfg, grant, requests)


def test_preparation_and_chat_cancellation_survive_reconstruction(pending_task):
    runtime, cfg, _, _, _, token, _, _ = pending_task
    model = Model(batch(CANCEL), AIMessage(content="旧操作已取消。"))
    selected = runtime(model)
    execute(selected, cfg, text="取消这次收藏")
    followup = Model(AIMessage(content="欢迎回来。"))
    rebuilt = runtime(followup)
    list(rebuilt.stream_events({"messages": [HumanMessage(content="只用一句话欢迎我")],
                               "task_id": "greeting"}, cfg))
    for prompt in (model.prompts[0], followup.prompts[0]):
        prepared = [m for m in prompt if isinstance(m, ToolMessage)
                    and m.tool_call_id == "prepare-original"]
        assert len(prepared) == 1
        assert json.loads(prepared[0].content)["status"] == "awaiting_confirmation"
        assert "19" in json.loads(prepared[0].content)["result"]
        assert prepared[0].artifact == {"kind": "prepared"}
        assert token not in str(prompt)
    observed = [m for m in followup.prompts[0] if isinstance(m, ToolMessage)]
    cancelled = [m for m in observed if m.tool_call_id == CANCEL["id"]]
    assert len(cancelled) == 1
    fact = json.loads(cancelled[0].content)
    assert fact == {"status": "cancelled", "scope": "operation", "executed": False,
                    "business_state": "not_observed",
                    "result": "已取消这项待确认操作，没有执行改动。"}
    assert any(isinstance(m, HumanMessage) and m.content == "取消这次收藏"
               for m in followup.prompts[0])
    assert not rebuilt.store.load(cfg).has_pending


def test_button_cancellation_keeps_one_fact_and_never_recreates_pending_state(pending_task):
    from agent.products.blog.confirmation import cancel

    runtime, cfg, _, selected, grant, token, _, _ = pending_task
    result = cancel(selected, grant, cfg)
    assert result["cancelled"]
    before = list(selected.store.load(cfg).messages)
    assert cancel(selected, grant, cfg) == result
    assert selected.store.load(cfg).messages == before
    followup = Model(AIMessage(content="欢迎回来。"))
    rebuilt = runtime(followup)
    list(rebuilt.stream_events({"messages": [HumanMessage(content="你好")],
                               "task_id": "after-button"}, cfg))
    tools = [m for m in followup.prompts[0] if isinstance(m, ToolMessage)]
    cancelled = [m for m in tools if m.name == "cancel_pending_operation"]
    assert len(cancelled) == 1
    assert json.loads(cancelled[0].content)["business_state"] == "not_observed"
    assert json.loads(cancelled[0].content)["scope"] == "operation"
    assert json.loads(cancelled[0].content)["executed"] is False
    assert token not in str(followup.prompts[0])
    assert not rebuilt.store.load(cfg).has_pending
