"""Tool activation survives window changes and follows explicit task checkpoints."""

from copy import deepcopy
from threading import Event

import pytest
from _own_support import config
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver

from agent.principal import SOURCE_ASSERTION, Principal
from agent.products.blog.application import BlogApplication
from agent.products.blog.capabilities import ToolCatalog
from agent.runtime import memory
from agent.runtime.budget import BudgetLimits, RequestBudget, budget_scope
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.turns import AgentCancelled
from tools.result import ok


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
        return NativeRuntime(NativeConversationStore(saver),
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
def test_completed_menu_survives_reload_and_history_trimming_in_activation_order(
        setup, trimmed, monkeypatch):
    runtime, _, calls = setup
    first = Model(call("load_beta_info_tools"), call("load_alpha_info_tools"),
                  call("read_beta"), AIMessageChunk(content="Read complete."))
    selected = runtime(first)
    turn(selected)
    if trimmed:
        from langchain_core.language_models.fake_chat_models import FakeListChatModel

        import models

        monkeypatch.setattr(models, "get_llm", lambda **kwargs: FakeListChatModel(
            responses=["Historical fixture queries completed."]))
        for index in range(4):
            selected.store.save_turn(config(), [HumanMessage(content=f"Later {index}")],
                                     "无关的长答复。" * 350)
        turn(runtime(Model(AIMessageChunk(content="Compacted."))), "Continue.")
        assert not any(isinstance(m, ToolMessage) for m in selected.store.load(config()).messages)
    after = Model(AIMessageChunk(content="Follow-up complete."))
    assert turn(runtime(after), "Continue.").reply == "Follow-up complete."
    assert after.requests[0]["tools"] == first.requests[-1]["tools"]
    assert names(after.requests[0])[-2:] == ["read_beta", "read_alpha"]
    assert calls == ["beta"]


@pytest.mark.parametrize("has_explicit_state", [False, True])
def test_historical_observations_do_not_activate_domains(setup, has_explicit_state):
    runtime, _, calls = setup
    selected = runtime(Model(call("load_beta_info_tools"), AIMessageChunk(content="Ready.")))
    if has_explicit_state:
        turn(selected)
    observed = memory.observation({"name": "read_alpha", "args": {}, "id": "old"},
                                  ok("Historical alpha fact."))
    selected.store.save_turn(config(), [HumanMessage(content="Previous read.")], "Past answer.",
                             observations=observed)
    model = Model(AIMessageChunk(content="No read requested."))
    turn(runtime(model), "Just chat.")
    assert "read_alpha" not in names(model.requests[0])
    assert ("read_beta" in names(model.requests[0])) == has_explicit_state
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
def test_menu_inheritance_is_bound_to_verified_scope(setup, changed):
    runtime, _, _ = setup
    selected = runtime(Model(call("load_beta_info_tools"), AIMessageChunk(content="Ready.")))
    turn(selected)
    cfg = config()
    if changed == "role":
        cfg["configurable"]["principal"] = Principal(7, "superadmin", SOURCE_ASSERTION)
    elif changed == "actor":
        cfg = config(user_id=8, principal=Principal(8, "user", SOURCE_ASSERTION))
    else:
        cfg = config(conversation_id=43, thread_id="conv_43")
        runtime(Model()).store.update_state(cfg, {
            "tool_state": selected.store.load(config()).tool_state})
    model = Model(AIMessageChunk(content="New scope."))
    turn(runtime(model), cfg=cfg)
    assert names(model.requests[0]) == ["load_alpha_info_tools", "load_beta_info_tools"]


def test_catalog_removal_rechecks_loaded_domains(setup):
    runtime, domains, _ = setup
    turn(runtime(Model(call("load_beta_info_tools"), AIMessageChunk(content="Ready."))))
    model = Model(AIMessageChunk(content="Current catalog."))
    turn(runtime(model, {"alpha": domains["alpha"]}))
    assert names(model.requests[0]) == ["load_alpha_info_tools"]


def test_oversized_inherited_menu_resets_before_new_task_without_losing_discovery(setup):
    runtime, domains, _ = setup
    domains["beta"]["read_beta"].description = "Large domain description. " * 1500
    first = Model(call("load_beta_info_tools"), AIMessageChunk(content="Ready."))
    turn(runtime(first))
    model = Model(AIMessageChunk(content="New task fits."))
    budget = RequestBudget(BudgetLimits(context_tokens=3500, request_tokens=6000))
    with budget_scope(budget):
        result = turn(runtime(model), "Just chat.")
    assert result.reply == "New task fits."
    assert names(model.requests[0]) == ["load_alpha_info_tools", "load_beta_info_tools"]
    assert budget.model_calls == 1 and budget.tokens <= budget.limits.request_tokens


def test_compressible_old_history_does_not_unload_the_inherited_menu(setup):
    runtime, _, _ = setup
    turn(runtime(Model(call("load_beta_info_tools"), AIMessageChunk(content="Ready."))))
    selected = runtime(Model(AIMessageChunk(content="Short answer.")))
    cfg = config()
    selected.store.save_turn(cfg, [HumanMessage(content="Older background. " * 1000)],
                             "Earlier reply. " * 1000)
    selected.store.save_turn(cfg, [HumanMessage(content="Keep this latest exchange.")],
                             "Latest answer.")
    model = Model(AIMessageChunk(content="Current tools stay loaded."))
    budget = RequestBudget(BudgetLimits(context_tokens=3500, request_tokens=6000))
    with budget_scope(budget):
        result = turn(runtime(model), "Short follow-up.")
    assert result.reply == "Current tools stay loaded."
    assert "read_beta" in names(model.requests[0])


def test_budget_pressure_reclaims_only_the_old_domain_and_preserves_a_usable_followup(
        setup, monkeypatch):
    from agent.runtime import tool_state

    runtime, domains, calls = setup
    domains["alpha"]["read_alpha"].description = "Large old domain definition. " * 600
    original = Model(call("load_alpha_info_tools"), call("load_beta_info_tools"),
                     AIMessageChunk(content="Ready."))
    turn(runtime(original))
    observed = []
    monkeypatch.setattr(tool_state, "record", lambda node, event, **data: observed.append({
        "event": event, **deepcopy(data)}))
    model = Model(call("read_beta"), AIMessageChunk(content="Beta fact confirmed."))
    budget = RequestBudget(BudgetLimits(context_tokens=1800, request_tokens=5000))
    with budget_scope(budget):
        selected = runtime(model)
        result = turn(selected, "Read beta again.")
    assert result.reply == "Beta fact confirmed." and calls == ["beta"]
    assert names(model.requests[0]) == [
        "load_alpha_info_tools", "load_beta_info_tools", "read_beta"]
    assert budget.model_calls == 2 and budget.tool_calls == 1
    saved = selected.store.load(config()).tool_state
    assert saved["loaded"] == ["beta"]
    assert saved["scope"]["uid"] == 7
    trimming = [event for event in observed if event["event"] == "tool_menu_trim"]
    assert len(trimming) == 1 and trimming[0]["domains"] == ["alpha"]
    assert trimming[0]["retained"] == ["beta"]
    footprint = next(event for event in observed if event["event"] == "tool_working_set")
    assert footprint["estimated_schema_tokens"] < footprint["previous_estimated_schema_tokens"]
    assert not any(event["event"] == "tool_menu_reset" for event in observed)
    following = Model(AIMessageChunk(content="Next follow-up."))
    with budget_scope(RequestBudget(BudgetLimits(context_tokens=1800, request_tokens=5000))):
        turn(runtime(following), "Continue.")
    assert following.requests[0]["tools"] == model.requests[0]["tools"]


def test_loading_an_evicted_domain_reclaims_unused_inheritance_before_budget_stop(setup):
    runtime, domains, calls = setup
    for name, tools in domains.items():
        tools[f"read_{name}"].description = f"Large {name} schema. " * 140
    turn(runtime(Model(call("load_alpha_info_tools"), call("load_beta_info_tools"),
                       AIMessageChunk(content="Ready."))))
    model = Model(call("load_alpha_info_tools"), call("read_alpha"),
                  AIMessageChunk(content="Alpha read."))
    selected = runtime(Model())
    cfg = config()
    snapshot = selected.store.load(cfg)
    prompt = selected.application.prompt([*snapshot.messages,
        HumanMessage(content="Read alpha instead.")], cfg["configurable"]["principal"])
    session = selected._session(cfg, snapshot)
    from langchain_core.messages.utils import count_tokens_approximately

    from agent.runtime.tool_state import apply

    single_cost = []
    for name in domains:
        apply(session, [name])
        single_cost.append(memory._tokens(prompt)
                           + count_tokens_approximately([], tools=session.menu))
    context_limit = max(single_cost) + model.max_tokens + 220
    limits = BudgetLimits(context_tokens=context_limit, request_tokens=20000)
    with budget_scope(RequestBudget(limits)):
        result = turn(runtime(model), "Read alpha instead.")
    assert result.reply == "Alpha read." and calls == ["alpha"]
    assert "read_alpha" not in names(model.requests[0]) and "read_beta" in names(model.requests[0])
    assert "read_alpha" in names(model.requests[1]) and "read_beta" not in names(model.requests[1])
    assert len(model.requests) == 3


def test_a_loader_cannot_evict_a_domain_already_selected_in_the_same_model_batch(setup):
    from langchain_core.messages.utils import count_tokens_approximately

    from agent.runtime.tool_state import apply

    runtime, domains, calls = setup
    for name, tools in domains.items():
        tools[f"read_{name}"].description = f"Large {name} schema. " * 140
    turn(runtime(Model(call("load_alpha_info_tools"), call("load_beta_info_tools"),
                       AIMessageChunk(content="Ready."))))
    load = call("load_alpha_info_tools").tool_calls[0]
    read = call("read_beta").tool_calls[0]
    model = Model(AIMessageChunk(content="", tool_calls=[load, read]),
                  AIMessageChunk(content="Beta read; alpha not read."))
    selected = runtime(Model())
    snapshot = selected.store.load(config())
    session = selected._session(config(), snapshot)
    apply(session, ["beta"])
    context_limit = count_tokens_approximately([], tools=session.menu) + 500
    limits = BudgetLimits(context_tokens=context_limit, request_tokens=20000)
    with budget_scope(RequestBudget(limits)):
        result = turn(runtime(model), "Load alpha and read beta.")
    assert calls == ["beta"] and "Beta read" in result.reply
    assert any(m.get("tool_call_id") == "read_beta" and m.get("status") == "success"
               for m in model.requests[-1]["messages"])
    state = selected.store.load(config()).tool_state
    assert state["loaded"] == ["beta", "alpha"]
