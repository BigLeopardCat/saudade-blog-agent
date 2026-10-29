"""Explicit continuation reuses framework state, including incomplete tool batches."""

import threading
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.capabilities import ToolCatalog
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.turns import AgentCancelled
from tools.result import ok


class Responses:
    max_tokens = 128

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.prompts = []

    def bind_tools(self, *args, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        self.prompts.append(messages)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])


def cfg(stop, role="user"):
    return {"configurable": {"thread_id": "conv_42", "conversation_id": 42,
        "user_id": 7, "principal": Principal(7, role, SOURCE_ASSERTION), "stop_event": stop}}


@pytest.mark.parametrize("parallel", [False, True])
def test_reconstructed_runtime_continues_after_read_without_repeating_it(parallel):
    stop, persisted, calls = threading.Event(), threading.Event(), []

    class ObservedSaver(InMemorySaver):
        def put_writes(self, config, writes, task_id, task_path=""):
            super().put_writes(config, writes, task_id, task_path)
            for channel, value in writes:
                messages = value if isinstance(value, list) else [value]
                if channel == "messages" and any(isinstance(m, ToolMessage)
                        and m.tool_call_id == "first" for m in messages):
                    persisted.set()

    saver = ObservedSaver()

    @tool
    def first() -> object:
        """Read the first fixture."""
        calls.append("first")
        return ok("first fact")

    @tool
    def second() -> object:
        """Read the second fixture."""
        if parallel and not model.continuing:
            assert persisted.wait(3), "first result was not persisted"
            stop.set()
        if stop.is_set():
            raise AgentCancelled()
        calls.append("second")
        return ok("second fact")

    class Model:
        max_tokens = 128

        def bind_tools(self, *args, **kwargs):
            return self

        def invoke(self, messages, **kwargs):
            if not calls:
                return AIMessage(content="", tool_calls=[
                    {"name": name, "args": {}, "id": name}
                    for name in (["first", "second"] if parallel else ["first"])])
            if not self.continuing:
                stop.set()
                raise AgentCancelled()
            assert "first fact" in str(messages)
            return AIMessage(content="Both facts are available.")

        continuing = False

    model = Model()
    catalog = ToolCatalog(read_tools={t.name: t for t in (first, second)})

    def runtime():
        return NativeRuntime(NativeConversationStore(saver), catalog=catalog,
                             model_factory=lambda **_: model)

    selected = runtime()
    with pytest.raises(AgentCancelled):
        list(selected.stream_events({"messages": [HumanMessage(content="Read and explain.")],
                                     "task_id": "original-task"}, cfg(stop)))
    assert selected.store.load(cfg(stop)).unfinished
    stop.clear()
    model.continuing = True
    restored = runtime()
    result = list(restored.stream_events({"resume_task": "original-task"}, cfg(stop)))[-1]
    assert result.reply == "Both facts are available."
    assert calls.count("first") == 1
    assert calls.count("second") == int(parallel)
    assert not restored.store.load(cfg(stop)).unfinished
    assert not restored.store.load(cfg(stop)).run
    # A stale continuation cannot start a new model invocation.
    replay = list(runtime().stream_events({"resume_task": "original-task"}, cfg(stop)))[-1]
    assert "没有恢复执行" in replay.reply


def test_resume_requires_current_role_task_and_conversation():
    from dataclasses import replace

    from agent.runtime.conversation_store import ConversationSnapshot
    from agent.runtime.recovery import allowed, new_run

    stop = threading.Event()
    run = new_run(cfg(stop), [HumanMessage(content="Read.")], "task")
    saved = ConversationSnapshot(run=run, unfinished=True)
    assert allowed(saved, "task", cfg(stop))
    assert not allowed(saved, "other", cfg(stop))
    assert not allowed(saved, "task", cfg(stop, "admin"))
    assert not allowed(replace(saved, pending_action={"jti": "card"}), "task", cfg(stop))


@pytest.mark.parametrize("continuation", ["resume", "new_instruction"])
def test_stopping_after_confirmed_write_preserves_write_and_history(
        monkeypatch, tmp_path, continuation):
    from contextlib import closing

    from agent import confirm, idem
    from agent.runtime import writes
    from eval.own_fixtures import favorite_backend

    stop, saver = threading.Event(), InMemorySaver()
    model = Responses(call("load_favorites_info_tools"),
                      call("prepare_favorite_change", action="add", article_id=19))

    def runtime():
        return NativeRuntime(NativeConversationStore(saver), model_factory=lambda **_: model)

    with favorite_backend() as business, closing(idem.Ledger(str(tmp_path / "ledger"))) as ledger:
        monkeypatch.setattr(writes, "ledger_for", lambda _: ledger)
        initial = {"messages": [HumanMessage(content="收藏文章19并告诉我结果")],
                   "task_id": "write-task"}
        first = list(runtime().stream_events(initial, cfg(stop)))[-1]
        token = first.execution["pending_confirm"]["token"]
        waiting = list(runtime().stream_events({"resume_task": "write-task"}, cfg(stop)))[-1]
        assert waiting.execution["pending_confirm"]["token"] == token
        assert not any(r.method == "POST" for r in business["requests"])
        grant = confirm.verify(token, 7, 42)
        model = Responses(AgentCancelled())
        with pytest.raises(AgentCancelled):
            list(runtime().stream_events({"confirm_grant": grant}, cfg(stop)))
        snapshot = runtime().store.load(cfg(stop))
        assert snapshot.unfinished and not snapshot.has_pending and snapshot.receipts
        reply = "已收藏文章19，已核实成功。" if continuation == "resume" else "你好。"
        model = Responses(AIMessage(content=reply))
        request = ({"resume_task": "write-task"} if continuation == "resume" else {
            "messages": [HumanMessage(content="现在你好")], "task_id": "new-task"})
        final = list(runtime().stream_events(request, cfg(stop)))[-1]
        assert final.reply == reply
        if continuation == "resume":
            assert len(final.execution["receipts"]) == 1
        assert len(model.prompts) == 1 and token not in str(model.prompts)
        assert sum(r.method == "POST" for r in business["requests"]) == 1
        assert not runtime().store.load(cfg(stop)).run
        history = runtime().store.load(cfg(stop)).messages
        ids = [m.tool_call_id for m in history if isinstance(m, ToolMessage)]
        assert len(ids) == len(set(ids))
        assert sum(isinstance(m, HumanMessage) and "收藏文章19" in str(m.content)
                   for m in history) == 1
        assert sum(isinstance(m, HumanMessage) and m.content == "现在你好"
                   for m in history) == int(continuation == "new_instruction")


def test_explicit_regeneration_discards_only_the_unfinished_cursor():
    from agent import checkpoint

    stop, saver = threading.Event(), InMemorySaver()
    selected = NativeRuntime(NativeConversationStore(saver),
                             model_factory=lambda **_: Responses(AgentCancelled()))
    selected.store.save_turn(cfg(stop), [HumanMessage(content="之前的问题")], "之前的答复")
    before = selected.store.load(cfg(stop)).messages
    with pytest.raises(AgentCancelled):
        list(selected.stream_events({"messages": [HumanMessage(content="重新生成这个问题")],
                                     "task_id": "discard-task"}, cfg(stop)))
    assert checkpoint.drop_last_user_turn(selected, 42, "其他问题")["why"] == "mismatch"
    assert selected.store.load(cfg(stop)).run
    assert checkpoint.drop_last_user_turn(selected, 42, "重新生成这个问题")["dropped"]
    assert selected.store.load(cfg(stop)).messages == before
    assert not selected.store.load(cfg(stop)).run


@pytest.mark.parametrize("extra", [{"message": "new"}, {"image": ["url"]},
                                   {"confirm_token": uuid.uuid4().hex}, {"task_id": "new-task"}])
def test_resume_control_rejects_new_content_and_approval(extra):
    from pydantic import ValidationError

    from agent.api.schemas import ChatRequest

    with pytest.raises(ValidationError):
        ChatRequest(**{"message": "", "resume_task": "task", "conversation_id": 42, **extra})


def test_completed_child_survives_failure_before_the_conversation_commit(monkeypatch):
    from agent.runtime import turn_commit

    commit = turn_commit.commit

    def fail(*args, **kwargs):
        raise OSError("conversation commit failed")

    monkeypatch.setattr(turn_commit, "commit", fail)
    stop, saver = threading.Event(), InMemorySaver()
    model = Responses(AIMessage(content="这是已完成的答复。"))
    first = NativeRuntime(NativeConversationStore(saver), model_factory=lambda **_: model)
    with pytest.raises(OSError):
        list(first.stream_events({"messages": [HumanMessage(content="回答一次")],
                                  "task_id": "commit-task"}, cfg(stop)))
    assert first.store.load(cfg(stop)).unfinished
    assert first.store.load(cfg(stop)).messages == []
    monkeypatch.setattr(turn_commit, "commit", commit)
    recovered_model = Responses()
    restored = NativeRuntime(NativeConversationStore(saver),
                             model_factory=lambda **_: recovered_model)
    result = list(restored.stream_events({"resume_task": "commit-task"}, cfg(stop)))[-1]
    assert result.reply == "这是已完成的答复。" and not recovered_model.prompts
    assert not restored.store.load(cfg(stop)).run
