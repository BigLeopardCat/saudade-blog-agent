"""Generated review messages cannot replace user boundaries during recovery."""

import pytest
from _own_support import config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from test_confirmation_continuation import FAVORITE, LOAD, Model, call, run, runtime
from test_confirmation_continuation import backend as backend_fixture

from agent import confirm
from agent.products.blog.capabilities import ToolCatalog
from agent.runtime import compaction, memory
from agent.runtime.execution_state import initial_state
from agent.runtime.recovery import restore_session
from agent.runtime.session import ToolSession
from agent.runtime.turns import AgentCancelled
from tools.result import ok

pytestmark = pytest.mark.tool_scopes(
    fixture_read="read.public",
)


backend = backend_fixture


def feedback(kind):
    return HumanMessage(content="Runtime feedback", id="internal-feedback",
                        additional_kwargs={"runtime_feedback": kind})


@pytest.mark.parametrize("kind", ["response_repair", "completion_review"])
def test_restore_uses_real_user_boundary_before_generated_feedback(kind):
    selected = ToolSession(None, config(), {}, {})
    source = [HumanMessage(content="Original request", id="original-user"),
        AIMessage(content="", tool_calls=[call("fixture_read", "read")]),
        ToolMessage(content="VERIFIED_SOURCE", name="fixture_read", tool_call_id="read",
                    artifact={"kind": "ok", "receipts": [{"tool": "fixture_write"}]}),
        feedback(kind)]
    state = {**initial_state(), "messages": source, "response_repairs": 1,
             "completion_checks": 1}
    restore_session(selected, state)
    assert len(selected.receipts) == 1
    assert any(isinstance(message, ToolMessage) and message.content == "VERIFIED_SOURCE"
               for message in selected.observations)
    assert selected.state["response_repairs"] == selected.state["completion_checks"] == 1


@pytest.mark.parametrize("kind", ["response_repair", "completion_review"])
@pytest.mark.parametrize("anchor", [None, "internal-feedback"])
def test_compaction_anchor_and_summary_guard_remain_real_user_input(kind, anchor, monkeypatch):
    from _memory_support import Capture

    from config.settings import settings

    monkeypatch.setattr(settings, "agent_context_tokens", 8000)
    monkeypatch.setattr(settings, "agent_summary_output_tokens", 512)
    source = [HumanMessage(content="Original request", id="original-user")]
    source.extend(AIMessage(content="earlier observed facts " * 120, id=f"source-{number}")
                  for number in range(8))
    steering = HumanMessage(content="User steering", id="steering", additional_kwargs={
        "task_input": {"id": "steer", "task_id": "task"}})
    source.extend([steering, feedback(kind)])
    selected_anchor, protected = compaction._protected(source, anchor)
    assert selected_anchor == "original-user" and {"original-user", "steering"} <= protected
    guards = []
    selected = ToolSession(Capture(), config(), {}, {})
    monkeypatch.setattr(compaction.ConversationCompaction, "_summarize",
                        lambda self, source, cutoff, guard, *args: guards.append(guard))
    compaction.ConversationCompaction(selected).before_model({**initial_state(),
        "messages": source, "context_anchor": anchor}, None)
    assert guards == [steering]
    assert memory.is_user_input(steering) and not memory.is_user_input(feedback(kind))


class BoundModel(Model):
    def bind(self, **kwargs):
        return self

    def invoke(self, prompt, **kwargs):
        return next(self.stream(prompt, **kwargs))


@pytest.mark.parametrize("kind", ["response_repair", "completion_review"])
def test_pause_after_internal_feedback_retains_reads_and_does_not_replay_write(backend, kind):
    state, _ = backend
    reads = []

    @tool
    def fixture_read() -> object:
        """Read one isolated factual source."""
        reads.append(1)
        return ok("RECOVERED_SOURCE_CANARY")

    selected = runtime(BoundModel(), catalog=ToolCatalog(
        read_tools={fixture_read.name: fixture_read}))
    selected.model_factory = lambda **_: BoundModel(
        LOAD, AIMessage(content="", tool_calls=[FAVORITE]))
    prepared = run(selected, "收藏文章19，之后读取资料并解释。")
    grant = confirm.verify(prepared.execution["pending_confirm"]["token"], 7, 42)
    pending = [{"content": "解释核实资料", "status": "in_progress"}]

    class Interrupted(BoundModel):
        def stream(self, prompt, **kwargs):
            if any(message.additional_kwargs.get("runtime_feedback") == kind
                   for message in prompt):
                raise AgentCancelled()
            yield from super().stream(prompt, **kwargs)

    responses = [AIMessage(content="", tool_calls=[call("fixture_read", "source")])]
    if kind == "response_repair":
        responses.append(AIMessage(content="", invalid_tool_calls=[{
            "name": "fixture_read", "args": "{", "id": "bad", "error": "JSON"}]))
    else:
        responses.extend([AIMessage(content="", tool_calls=[call("write_todos", "plan",
                                                                  todos=pending)]),
                          AIMessage(content="我会继续解释。")])
    selected.model_factory = lambda **_: Interrupted(*responses)
    with pytest.raises(AgentCancelled):
        run(selected, grant=grant)
    saved = selected.store.load(config())
    assert saved.unfinished and len(state["writes"]) == len(reads) == 1
    continuation = []
    if kind == "completion_review":
        continuation.append(AIMessage(content="", tool_calls=[call("write_todos", "done",
            todos=[{"content": pending[0]["content"], "status": "completed"}])]))
    model = BoundModel(*continuation, AIMessage(content="资料已核实，收藏回执已保留。"))
    rebuilt = runtime(model, saver=selected.saver, catalog=selected.application.catalog)
    result = list(rebuilt.stream_events({"resume_task": saved.run["id"]}, config()))[-1]
    assert result.execution["receipts"] and len(state["writes"]) == len(reads) == 1
    messages = rebuilt.store.load(config()).messages
    assert any(isinstance(message, ToolMessage) and "RECOVERED_SOURCE_CANARY" in message.content
               for message in messages)
    assert not any(message.additional_kwargs.get("runtime_feedback") for message in messages)
    assert all("RECOVERED_SOURCE_CANARY" in str(prompt) for prompt in model.prompts)
