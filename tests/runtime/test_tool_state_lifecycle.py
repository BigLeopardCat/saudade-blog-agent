"""Tool activation survives window changes and follows explicit task checkpoints."""

from copy import deepcopy
from threading import Event

import pytest
from _conversation_seed import seed_turn
from _memory_support import ACTOR
from _own_support import config
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver

from agent import checkpoint
from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.products.blog.capabilities import ToolCatalog
from agent.runtime import memory
from agent.runtime.budget import BudgetLimits, TaskBudget, budget_scope
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.conversation_store import ConversationAccessDenied
from agent.runtime.service import AgentRuntime
from agent.runtime.turns import AgentCancelled
from tools.result import ok

pytestmark = pytest.mark.tool_scopes(
    read_alpha="read.public",
    read_beta="read.public",
)


class Model:
    max_tokens = 64

    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def bind_tools(self, tools, **kwargs):
        self.menu = [convert_to_openai_tool(item) for item in tools]
        return self

    def stream(self, prompt, **kwargs):
        self.requests.append({"tools": deepcopy(self.menu),
                              "messages": [m.model_dump() for m in prompt]})
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        yield response


class Catalog(ToolCatalog):
    def __init__(self, domains):
        self.fixture_domains = domains

    def initial_tools(self, own_enabled):
        return {}

    def domains(self, config, can_prepare, pending=None, saver=None):
        return self.fixture_domains


@pytest.fixture
def setup():
    calls = []

    @tool
    def read_alpha() -> object:
        """Read alpha fixture data."""
        calls.append("alpha")
        return ok("Alpha fact.")

    @tool
    def read_beta() -> object:
        """Read beta fixture data."""
        calls.append("beta")
        return ok("Beta fact.")

    domains = {"alpha": {read_alpha.name: read_alpha}, "beta": {read_beta.name: read_beta}}
    saver = InMemorySaver()

    def runtime(model, selected=None):
        allowed = domains if selected is None else selected
        application = BlogApplication(Catalog(allowed))
        names = {name for tools in allowed.values() for name in tools}
        application.history_access = lambda principal, name: name in names
        application.prompt = lambda messages, principal, pending=None: [
            SystemMessage(content="Read authorized fixture data."),
            *memory.prompt_dialogue(messages, principal,
                                    tool_access=application.history_access)]
        return AgentRuntime(CheckpointConversationStore(saver),
                             application=application,
                             model_factory=lambda **_: model)

    return runtime, domains, calls


def call(name):
    return AIMessageChunk(content="", tool_calls=[{"name": name, "id": name, "args": {}}])


def turn(runtime, text="Read.", cfg=None, **values):
    return list(runtime.stream_events({"messages": [HumanMessage(content=text)], **values},
                                      cfg or config()))[-1]


def names(request):
    return [item["function"]["name"] for item in request["tools"]]


@pytest.mark.parametrize("trimmed", [False, True])
def test_new_task_keeps_activation_order_after_history_compaction(
        setup, trimmed, monkeypatch):
    runtime, _, calls = setup
    first = Model(call("load_beta_info_tools"), call("load_alpha_info_tools"),
                  call("read_beta"), AIMessageChunk(content="Read complete."))
    selected = runtime(first)
    turn(selected)
    if trimmed:
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        import models
        from config.settings import settings

        monkeypatch.setattr(settings, "agent_context_tokens", 8000)
        monkeypatch.setattr(settings, "agent_summary_output_tokens", 512)

        monkeypatch.setattr(models, "get_llm", lambda **kwargs: FakeListChatModel(
            responses=["Historical fixture queries completed."]))
        for index in range(4):
            seed_turn(selected.store, config(), [HumanMessage(content=f"Later {index}")],
                                     "无关的长答复。" * 350)
        turn(runtime(Model(AIMessageChunk(content="Compacted."))), "Continue.")
        assert not any(isinstance(m, ToolMessage) for m in selected.store.load(config()).messages)
    after = Model(AIMessageChunk(content="Follow-up complete."))
    assert turn(runtime(after), "Continue.").reply == "Follow-up complete."
    assert names(after.requests[0]) == [
        "get_agent_capabilities", "load_alpha_info_tools", "load_beta_info_tools",
        "read_beta", "read_alpha"]
    assert "read_alpha" in names(first.requests[-1]) and "read_beta" in names(first.requests[-1])
    assert calls == ["beta"]
    if trimmed:
        source = selected.store.load(config()).tool_domains["domains"][0]["turn_id"]
        assert checkpoint.drop_last_user_turn(selected, 42, source, principal=ACTOR)["dropped"]
        assert selected.store.load(config()).tool_domains["domains"] == []


@pytest.mark.parametrize("has_explicit_state", [False, True])
def test_historical_observations_do_not_activate_domains(setup, has_explicit_state):
    runtime, _, calls = setup
    selected = runtime(Model(call("load_beta_info_tools"), AIMessageChunk(content="Ready.")))
    if has_explicit_state:
        turn(selected)
    observed = memory.observation({"name": "read_alpha", "args": {}, "id": "old"},
                                  ok("Historical alpha fact."))
    seed_turn(selected.store, config(), [HumanMessage(content="Previous read.")], "Past answer.",
                             observations=observed)
    model = Model(AIMessageChunk(content="No read requested."))
    turn(runtime(model), "Just chat.")
    assert "read_alpha" not in names(model.requests[0])
    assert ("read_beta" in names(model.requests[0])) is has_explicit_state
    assert any(m["type"] == "tool" for m in model.requests[0]["messages"])
    assert calls == []


@pytest.mark.parametrize("inherited", [False, True])
def test_resume_uses_checkpoint_menu_and_messages_without_repeating_a_read(setup, inherited):
    runtime, _, calls = setup
    if inherited:
        turn(runtime(Model(call("load_alpha_info_tools"), call("load_beta_info_tools"),
                           AIMessageChunk(content="Ready."))))
    stop = Event()
    cfg = config(stop_event=stop)
    first = Model(call("load_beta_info_tools"), call("read_beta"), AgentCancelled())
    selected = runtime(first)
    with pytest.raises(AgentCancelled):
        turn(selected, cfg=cfg, task_id="recover-menu")
    restored = Model(AIMessageChunk(content="Recovered."))
    result = list(runtime(restored).stream_events({"resume_task": "recover-menu"}, cfg))[-1]
    assert result.reply == "Recovered."
    assert restored.requests[0] == first.requests[-1]
    assert names(restored.requests[0])[-1] == "read_beta"
    assert calls == ["beta"]
    assert not runtime(restored).store.load(cfg).unfinished


@pytest.mark.parametrize("changed", ["role", "actor", "conversation"])
def test_new_task_rebuilds_catalog_for_the_verified_scope(setup, changed):
    runtime, _, _ = setup
    selected = runtime(Model(call("load_beta_info_tools"), call("read_beta"),
                             AIMessageChunk(content="Ready.")))
    turn(selected)
    cfg = config()
    if changed == "role":
        cfg["configurable"]["principal"] = Principal(7, "superadmin", SOURCE_ASSERTION)
    elif changed == "actor":
        cfg = config(user_id=8, principal=Principal(8, "user", SOURCE_ASSERTION))
    else:
        cfg = config(conversation_id=43, thread_id="conv_43")
    model = Model(AIMessageChunk(content="New scope."))
    if changed == "actor":
        before = selected.get_state(config())
        with pytest.raises(ConversationAccessDenied):
            turn(runtime(model), cfg=cfg)
        assert not model.requests and selected.get_state(config()) == before
        return
    turn(runtime(model), cfg=cfg)
    assert names(model.requests[0]) == [
        "get_agent_capabilities", "load_alpha_info_tools", "load_beta_info_tools"]


def test_catalog_removal_rechecks_loaded_domains(setup):
    runtime, domains, _ = setup
    turn(runtime(Model(call("load_beta_info_tools"), call("read_beta"),
                       AIMessageChunk(content="Ready."))))
    model = Model(AIMessageChunk(content="Current catalog."))
    turn(runtime(model, {"alpha": domains["alpha"]}))
    assert names(model.requests[0]) == ["get_agent_capabilities", "load_alpha_info_tools"]


def test_exhausted_task_budget_is_not_reset_by_releasing_domains(setup):
    runtime, domains, _ = setup
    domains["beta"]["read_beta"].description = "Large domain description. " * 1500
    selected = runtime(Model(call("load_beta_info_tools"), AIMessageChunk(content="Ready.")))
    turn(selected)
    saved = selected.store.load(config()).tool_domains
    model = Model(AIMessageChunk(content="Must not call."))
    budget = TaskBudget(BudgetLimits(context_tokens=3500, task_tokens=6000))
    budget.tokens = budget.limits.task_tokens
    budget.reason = "task_tokens"
    with budget_scope(budget):
        result = turn(runtime(model), "Just chat.")
    assert result.execution["blocked"][0]["reason"] == "task_tokens"
    assert not model.requests and budget.model_calls == 0
    assert budget.tokens == budget.limits.task_tokens
    assert selected.store.load(config()).tool_domains == saved


def test_history_compaction_preserves_explicit_tool_activation(setup):
    runtime, _, _ = setup
    turn(runtime(Model(call("load_beta_info_tools"), AIMessageChunk(content="Ready."))))
    selected = runtime(Model(AIMessageChunk(content="Short answer.")))
    cfg = config()
    seed_turn(selected.store, cfg, [HumanMessage(content="Older background. " * 1000)],
                             "Earlier reply. " * 1000)
    seed_turn(selected.store, cfg, [HumanMessage(content="Keep this latest exchange.")],
                             "Latest answer.")
    model = Model(AIMessageChunk(content="Current tools stay loaded."))
    budget = TaskBudget(BudgetLimits(context_tokens=3500, task_tokens=6000))
    with budget_scope(budget):
        result = turn(runtime(model), "Short follow-up.")
    assert result.reply == "Current tools stay loaded."
    assert "read_beta" in names(model.requests[0])



def test_retained_schemas_count_against_the_multistep_task_budget(setup):
    runtime, domains, calls = setup
    domains["alpha"]["read_alpha"].description = "Large completed domain. " * 140
    turn(runtime(Model(call("load_alpha_info_tools"), AIMessageChunk(content="Ready."))))
    model = Model(call("load_beta_info_tools"), call("read_beta"),
                  AIMessageChunk(content="Must not reach."))
    budget = TaskBudget(BudgetLimits(context_tokens=2500, task_tokens=2400))
    with budget_scope(budget):
        result = turn(runtime(model), "Read beta and answer.")
    assert result.execution["blocked"][0]["reason"] == "task_tokens"
    assert calls == ["beta"] and budget.tokens <= budget.limits.task_tokens
    assert len(model.requests) == budget.model_calls == 2
    assert all("read_alpha" in names(request) for request in model.requests)


def test_loading_another_domain_preserves_current_task_tools_until_completion(setup):
    runtime, _, calls = setup
    model = Model(call("load_beta_info_tools"), call("read_beta"),
                  call("load_alpha_info_tools"), call("read_alpha"),
                  AIMessageChunk(content="Both facts read."))
    selected = runtime(model)
    assert turn(selected).reply == "Both facts read."
    assert calls == ["beta", "alpha"]
    assert names(model.requests[-1])[-2:] == ["read_beta", "read_alpha"]
    following = Model(AIMessageChunk(content="Next task."))
    turn(runtime(following))
    assert names(following.requests[0]) == [
        "get_agent_capabilities", "load_alpha_info_tools", "load_beta_info_tools",
        "read_beta", "read_alpha"]


def test_over_budget_saved_domains_release_capacity_and_can_be_loaded_again(setup):
    runtime, domains, calls = setup
    domains["beta"]["read_beta"].description = "Large read schema. " * 1500
    selected = runtime(Model(call("load_beta_info_tools"), call("read_beta"),
                             AIMessageChunk(content="Read complete.")))
    turn(selected)
    model = Model(AIMessageChunk(content="Hello."))
    budget = TaskBudget(BudgetLimits(context_tokens=3500, task_tokens=6000))
    with budget_scope(budget):
        result = turn(runtime(model), "Just chat.")
    assert result.reply == "Hello." and calls == ["beta"]
    assert names(model.requests[0]) == [
        "get_agent_capabilities", "load_alpha_info_tools", "load_beta_info_tools"]
    assert selected.store.load(config()).tool_domains["domains"] == []
    assert budget.reason is None and budget.model_calls == 1
    restored = Model(call("load_beta_info_tools"), call("read_beta"),
                     AIMessageChunk(content="Read again."))
    turn(runtime(restored), "Read beta again.")
    assert calls == ["beta", "beta"] and "read_beta" in names(restored.requests[-1])


def test_discarded_child_activation_does_not_replace_the_committed_domains(setup):
    runtime, _, calls = setup
    turn(runtime(Model(call("load_alpha_info_tools"), call("read_alpha"),
                       AIMessageChunk(content="Alpha committed."))))
    selected = runtime(Model(call("load_beta_info_tools"), call("read_beta"), AgentCancelled()))
    with pytest.raises(AgentCancelled):
        turn(selected, turn_id="discarded-child", task_id="discarded-run")
    assert [item["domain"] for item in selected.store.load(config()).tool_domains["domains"]] == [
        "alpha"]
    assert checkpoint.drop_last_user_turn(
        selected, 42, "discarded-child", principal=ACTOR)["dropped"]
    model = Model(AIMessageChunk(content="Fresh task."))
    turn(runtime(model), "Start a fresh task.")
    assert names(model.requests[0]) == [
        "get_agent_capabilities", "load_alpha_info_tools", "load_beta_info_tools", "read_alpha"]
    assert calls == ["alpha", "beta"]


def test_multiple_direct_reads_activate_domains_once_in_call_order_and_survive_rebuild(setup):
    runtime, _, calls = setup
    model = Model(AIMessageChunk(content="", tool_calls=[
        {"name": "read_beta", "id": "b", "args": {}},
        {"name": "read_alpha", "id": "a", "args": {}}]),
        call("read_beta"), AIMessageChunk(content="Both read."))
    selected = runtime(model)
    turn(selected, turn_id="direct-batch")
    assert calls == ["beta", "alpha", "beta"]
    assert names(model.requests[1])[-2:] == ["read_beta", "read_alpha"]
    assert model.requests[1]["tools"] == model.requests[2]["tools"]
    assert selected.store.load(config()).tool_domains["domains"] == [
        {"domain": "beta", "turn_id": "direct-batch"},
        {"domain": "alpha", "turn_id": "direct-batch"}]
    following = Model(AIMessageChunk(content="Follow-up."))
    turn(runtime(following))
    assert following.requests[0]["tools"] == model.requests[-1]["tools"]


def test_discard_removes_only_domains_first_activated_in_the_discarded_turn(setup):
    runtime, _, _ = setup
    selected = runtime(Model(call("load_beta_info_tools"), AIMessageChunk(content="First.")))
    turn(selected, turn_id="retained")
    turn(runtime(Model(call("load_alpha_info_tools"), call("load_beta_info_tools"),
                       AIMessageChunk(content="Second."))), turn_id="removed")
    assert checkpoint.drop_last_user_turn(selected, 42, "removed", principal=ACTOR)["dropped"]
    assert selected.store.load(config()).tool_domains["domains"] == [
        {"domain": "beta", "turn_id": "retained"}]
    following = Model(AIMessageChunk(content="Rebuilt."))
    turn(runtime(following))
    assert names(following.requests[0]) == [
        "get_agent_capabilities", "load_alpha_info_tools", "load_beta_info_tools", "read_beta"]


def test_explicit_load_over_budget_has_error_receipt_and_no_partial_activation(setup):
    import json

    runtime, domains, calls = setup
    domains["beta"]["read_beta"].description = "Oversized domain schema. " * 1500
    model = Model(call("load_beta_info_tools"), AIMessageChunk(content="Domain could not load."))
    selected = runtime(model)
    budget = TaskBudget(BudgetLimits(context_tokens=3500, task_tokens=10000))
    with budget_scope(budget):
        result = turn(selected)
    assert result.reply == "Domain could not load." and not calls
    assert model.requests[0]["tools"] == model.requests[1]["tools"]
    response = next(m for m in model.requests[1]["messages"] if m["type"] == "tool")
    assert response["status"] == "error"
    assert json.loads(response["content"]) == {
        "status": "not_loaded", "domain": "beta", "reason": "context_tokens"}
    assert selected.store.load(config()).tool_domains["domains"] == []
    assert budget.reason is None and budget.model_calls == 2


def test_direct_read_keeps_facts_when_domain_activation_is_over_budget(setup, monkeypatch):
    from agent.runtime import tool_state

    runtime, domains, calls = setup
    events = []
    monkeypatch.setattr(tool_state, "record", lambda *args, **kwargs: events.append((args, kwargs)))
    domains["beta"]["read_beta"].description = "Oversized domain schema. " * 1500
    model = Model(call("read_beta"), AIMessageChunk(content="Beta fact."))
    selected = runtime(model)
    with budget_scope(TaskBudget(BudgetLimits(context_tokens=3500, task_tokens=10000))):
        result = turn(selected)
    assert result.reply == "Beta fact." and calls == ["beta"]
    assert model.requests[0]["tools"] == model.requests[1]["tools"]
    response = next(m for m in model.requests[1]["messages"] if m["type"] == "tool")
    assert response["status"] == "success" and response["content"] == "Beta fact."
    assert response["artifact"]["kind"] == "ok"
    assert response["artifact"]["domain_activation"] == {
        "domain": "beta", "status": "rejected", "reason": "context_tokens"}
    assert selected.store.load(config()).tool_domains["domains"] == []
    assert events == [(("native", "tool_domain_activation_rejected"),
                       {"domain": "beta", "reason": "context_tokens"})]
    larger = Model(call("read_beta"), AIMessageChunk(content="Read with a larger budget."))
    turn(runtime(larger))
    assert "read_beta" in names(larger.requests[-1])


def test_partial_direct_batch_recovers_both_activations_without_repeating_reads(setup):
    runtime, _, calls = setup
    first = Model(AIMessageChunk(content="", tool_calls=[
        {"name": "read_beta", "id": "b", "args": {}},
        {"name": "read_alpha", "id": "a", "args": {}}]), AgentCancelled())
    selected = runtime(first)
    with pytest.raises(AgentCancelled):
        turn(selected, task_id="direct-recovery", turn_id="batch-origin")
    assert selected.store.load(config()).tool_domains is None
    rebuilt = Model(AIMessageChunk(content="Recovered both reads."))
    list(runtime(rebuilt).stream_events({"resume_task": "direct-recovery"}, config()))
    assert calls == ["beta", "alpha"]
    assert rebuilt.requests[0] == first.requests[-1]
    assert selected.store.load(config()).tool_domains["domains"] == [
        {"domain": "beta", "turn_id": "batch-origin"},
        {"domain": "alpha", "turn_id": "batch-origin"}]


def test_deleted_conversation_removes_tool_metadata_and_cannot_resume_it(setup):
    from agent.runtime import checkpoint_store

    runtime, _, _ = setup
    selected = runtime(Model(call("load_beta_info_tools"), AIMessageChunk(content="Loaded.")))
    turn(selected)
    assert selected.store.load(config()).tool_domains["domains"]
    assert checkpoint.delete_thread(selected.saver, 42, principal=ACTOR)
    assert selected.store.load(config()).tool_domains is None
    with pytest.raises(checkpoint_store.ConversationDeleted):
        checkpoint_store.require_active(selected.saver, "conv_42")


@pytest.mark.parametrize("kind", ["ok", "empty", "unavailable"])
def test_only_successful_direct_reads_activate_their_domain(setup, monkeypatch, kind):
    from tools import result as tool_result

    runtime, domains, _ = setup
    monkeypatch.setattr(domains["beta"]["read_beta"], "func",
                        lambda: getattr(tool_result, kind)("Synthetic source outcome."))
    model = Model(call("read_beta"), AIMessageChunk(content="Source outcome reported."))
    selected = runtime(model)
    turn(selected)
    actual = selected.store.load(config()).tool_domains["domains"]
    assert bool(actual) is (kind in {"ok", "empty"})
    response = next(m for m in model.requests[-1]["messages"] if m["type"] == "tool")
    assert response["status"] == ("error" if kind == "unavailable" else "success")
    assert response["content"] == "Synthetic source outcome."
