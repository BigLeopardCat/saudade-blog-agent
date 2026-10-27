"""Steering reaches standard Agent boundaries without restarting the task."""

import secrets
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.capabilities import ToolCatalog
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.task_inputs import InputConflict, TaskInputEvent, binding


def config():
    return {"configurable": {"thread_id": "conv_42", "conversation_id": 42,
        "user_id": 7, "principal": Principal(7, "user", SOURCE_ASSERTION),
        "task_inputs": True}}


def runtime(model, tools=(), saver=None):
    return NativeRuntime(NativeConversationStore(saver or InMemorySaver()),
        catalog=ToolCatalog(read_tools={item.name: item for item in tools}),
        model_factory=lambda **_: model)


class Model:
    max_tokens = 128

    def __init__(self, *answers):
        self.answers, self.prompts = iter(answers), []

    def bind_tools(self, *args, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        self.prompts.append(messages)
        answer = next(self.answers)
        return answer(messages) if callable(answer) else answer


def start(selected, cfg):
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(list, selected.stream_events({"task_id": "same-task", "messages": [
        HumanMessage(content="找一篇文章并解释")]}, cfg))
    return executor, future


@pytest.mark.parametrize("propose_tool", [False, True])
def test_input_during_model_changes_next_round_and_supersedes_unexecuted_calls(propose_tool):
    entered, release, calls = threading.Event(), threading.Event(), []

    @tool
    def lookup() -> str:
        """Read a fixture article."""
        calls.append("lookup")
        return "article"

    def first(_):
        entered.set()
        assert release.wait(5)
        return AIMessage(content="", tool_calls=[{"name": "lookup", "args": {}, "id": "old"}]) \
            if propose_tool else AIMessage(content="之前的回答")

    model = Model(first, AIMessage(content="改为只列标题"))
    selected, cfg = runtime(model, [lookup]), config()
    scope = binding(cfg["configurable"]["principal"], 42)
    executor, future = start(selected, cfg)
    try:
        assert entered.wait(5)
        queued = selected.inputs.submit("same-task", scope, "input-1", "只列标题，不要解读")
        assert queued["status"] == "queued"
        assert selected.inputs.submit("same-task", scope, "input-1", queued["text"]) == queued
    finally:
        release.set()
        try:
            events = future.result(timeout=5)
        finally:
            executor.shutdown(wait=True)
    assert not calls
    assert len(model.prompts) == 2
    assert any(isinstance(m, HumanMessage)
               and m.content == "用户补充的当前任务要求：\n" + queued["text"]
               for m in model.prompts[1])
    if propose_tool:
        assert any(isinstance(m, ToolMessage) and m.tool_call_id == "old"
                   and (m.artifact or {}).get("phase") == "superseded" for m in model.prompts[1])
    assert any(isinstance(e, TaskInputEvent)
               and any(item["status"] == "applied" for item in e.data.get("inputs", []))
               for e in events)
    assert "改为只列标题" in events[-1].reply
    saved = selected.store.load(cfg)
    assert sum(isinstance(m, HumanMessage) and m.content == queued["text"]
               for m in saved.messages) == 1
    assert selected.inputs.read("same-task", scope)["inputs"][0]["status"] == "applied"
    with pytest.raises(InputConflict, match="not_running"):
        selected.inputs.submit("same-task", scope, "late", "新请求")


def test_inbox_seal_and_submission_are_atomic_and_bound():
    from agent.runtime.task_input_store import MemoryInputs
    from agent.runtime.task_inputs import TaskInputs

    inbox = TaskInputs(MemoryInputs())
    task = {"id": "one", "scope": {"uid": 7, "role": "user", "conversation_id": 42}}
    inbox.open(task)
    for field, replacement in [("uid", 8), ("role", "admin"), ("conversation_id", 43)]:
        with pytest.raises(InputConflict, match="not_available"):
            inbox.submit("one", {**task["scope"], field: replacement}, "x", "text")
    inbox.submit("one", task["scope"], "x", "text")
    with pytest.raises(InputConflict, match="id_conflict"):
        inbox.submit("one", task["scope"], "x", "different")
    assert len(inbox.boundary(task, set(), final=True)) == 1
    assert inbox.boundary(task, {"x"}, final=True, applied={"x"}) == []
    with pytest.raises(InputConflict, match="not_running"):
        inbox.submit("one", task["scope"], "y", "late")
    assert inbox.submit("one", task["scope"], "x", "text")["status"] == "applied"


@pytest.mark.parametrize("resume_capable", [False, True])
def test_cancelled_model_keeps_queued_input_for_checkpoint_continuation(resume_capable):
    from agent.runtime.control import AgentCancelled

    cfg, saver = config(), InMemorySaver()
    selected = None
    scope = binding(cfg["configurable"]["principal"], 42)

    def interrupted(_):
        selected.inputs.submit("same-task", scope, "one", "用两句话解释")
        raise AgentCancelled()

    model = Model(interrupted, AIMessage(content="按两句话继续完成。"))
    selected = runtime(model, saver=saver)
    with pytest.raises(AgentCancelled):
        list(selected.stream_events({"task_id": "same-task", "messages": [
            HumanMessage(content="解释文章")]}, cfg))
    assert selected.inputs.read("same-task", scope)["phase"] == "paused"
    assert selected.inputs.read("same-task", scope)["inputs"][0]["status"] == "queued"
    restored = runtime(model, saver=saver)
    cfg["configurable"]["task_inputs"] = resume_capable
    events = list(restored.stream_events({"resume_task": "same-task"}, cfg))
    assert "按两句话继续完成" in events[-1].reply
    assert sum(isinstance(m, HumanMessage)
               and m.content == "用户补充的当前任务要求：\n用两句话解释"
               for m in model.prompts[-1]) == 1
    if not resume_capable:
        assert not any(isinstance(event, TaskInputEvent) for event in events)
    assert restored.inputs.read("same-task",scope)["phase"] == "completed"


def test_input_without_a_remaining_model_call_is_not_reported_as_applied():
    from dataclasses import replace

    from agent.runtime.budget import RequestBudget, budget_scope

    cfg = config()
    scope = binding(cfg["configurable"]["principal"], 42)
    selected = None

    def answer(_):
        selected.inputs.submit("same-task", scope, "last", "改为总结另一篇")
        return AIMessage(content="第一轮完成")

    model = Model(answer)
    selected = runtime(model)
    budget = RequestBudget()
    budget.limits = replace(budget.limits, model_calls=1)
    with budget_scope(budget):
        events = list(selected.stream_events({"task_id":"same-task", "messages":[
            HumanMessage(content="解释文章")]}, cfg))
    status = selected.inputs.read("same-task", scope)
    assert status["inputs"][0]["status"] == "not_applied"
    assert len(model.prompts) == 1
    assert not any(isinstance(e, TaskInputEvent)
                   and any(item["status"] == "applied" for item in e.data.get("inputs", []))
                   for e in events)
    assert not any(isinstance(m, HumanMessage) and m.content == "改为总结另一篇"
                   for m in selected.store.load(cfg).messages)


def test_terminal_tool_reports_queued_input_as_unapplied_instead_of_executing_more():
    from agent.runtime.results import TurnResult

    cfg = config()
    scope = binding(cfg["configurable"]["principal"], 42)
    selected = None

    @tool
    def terminal_read() -> object:
        """Return a terminal read result."""
        selected.inputs.submit("same-task", scope, "late", "再查另一篇")
        return TurnResult("读取已完成。")

    model = Model(AIMessage(content="",tool_calls=[{
        "name":"terminal_read","args":{},"id":"read"}]))
    selected = runtime(model, [terminal_read])
    events = list(selected.stream_events({"task_id":"same-task", "messages":[
        HumanMessage(content="读取文章")]}, cfg))
    assert events[-1].reply == "读取已完成。"
    assert selected.inputs.read("same-task", scope)["inputs"][0]["status"] == "not_applied"


def test_chat_only_role_can_steer_without_gaining_tools():
    cfg = config()
    cfg["configurable"]["principal"] = Principal(7, "zako", SOURCE_ASSERTION)
    selected = None
    scope = binding(cfg["configurable"]["principal"], 42)

    def first(_):
        selected.inputs.submit("same-task", scope, "tone", "温柔一点")
        return AIMessage(content="原回答")

    model = Model(first, AIMessage(content="好呀，我们慢慢聊。"))
    selected = NativeRuntime(NativeConversationStore(InMemorySaver()),
                             model_factory=lambda **_: model)
    events = list(selected.stream_events({"task_id":"same-task", "messages":[
        HumanMessage(content="聊聊天")]}, cfg))
    assert "慢慢聊" in events[-1].reply
    assert not selected._session(cfg,selected.store.load(cfg)).tools
    assert selected.inputs.read("same-task",scope)["inputs"][0]["status"] == "applied"


def test_new_requirements_supersede_a_card_that_has_not_been_published():
    from agent.runtime.results import TurnResult

    cfg = config()
    scope = binding(cfg["configurable"]["principal"], 42)
    selected = None
    unpublished = secrets.token_urlsafe(24)

    @tool
    def prepare_fixture() -> object:
        """Prepare a synthetic proposal without performing a business write."""
        selected.inputs.submit("same-task", scope, "changed", "先不修改，只介绍功能")
        return TurnResult("确认修改？", {"pending_confirm": {"q":"确认修改？",
            "token":unpublished, "opts":[],"exp":9999999999},
            "pending_action":{"jti":"a"*32}})

    model = Model(AIMessage(content="",tool_calls=[{
        "name":"prepare_fixture","args":{},"id":"proposal"}]),
        AIMessage(content="我只介绍功能，没有修改。"))
    selected = runtime(model,[prepare_fixture])
    events = list(selected.stream_events({"task_id":"same-task", "messages":[
        HumanMessage(content="准备修改")]},cfg))
    assert "只介绍功能" in events[-1].reply
    assert not (events[-1].execution or {}).get("pending_confirm")
    assert not selected.store.load(cfg).has_pending
    assert unpublished not in str(model.prompts[-1])
    assert selected.inputs.read("same-task",scope)["inputs"][0]["status"] == "applied"


def test_conversation_deletion_removes_its_inputs_without_affecting_other_threads():
    from agent import checkpoint

    selected = runtime(Model())
    scope = binding(config()["configurable"]["principal"],42)
    selected.inputs.open({"id":"one","scope":scope})
    selected.inputs.open({"id":"other","scope":{**scope,"conversation_id":43}})
    selected.inputs.submit("one",scope,"private","补充内容")
    assert checkpoint.delete_thread(selected.saver,42)
    with pytest.raises(InputConflict):
        selected.inputs.read("one",scope)
    assert selected.inputs.read("other",{**scope,"conversation_id":43})["phase"] == "running"
