"""Previously loaded schemas must not starve later authorized work."""

import pytest
from _framework_fixture import call as wire_call
from _framework_fixture import model_replies
from _own_support import config
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage
from test_tool_state_lifecycle import Model, call, names, turn
from test_tool_state_lifecycle import setup as setup

from agent.runtime.budget import BudgetLimits, TaskBudget, budget_scope
from agent.runtime.session import ToolSession
from agent.runtime.turns import AgentCancelled

pytestmark = pytest.mark.tool_scopes(read_alpha="read.public", read_beta="read.public")


@pytest.mark.parametrize("previous_task", [False, True])
def test_capacity_can_release_an_idle_domain_to_load_a_requested_domain(request, previous_task):
    runtime, domains, calls = request.getfixturevalue("setup")
    for domain in domains:
        domains[domain]["read_" + domain].description = "Read domain data. " * 200
    if previous_task:
        turn(runtime(Model(call("load_alpha_info_tools"), call("read_alpha"),
                           AIMessageChunk(content="First task complete."))))
    replies = [] if previous_task else [call("load_alpha_info_tools"), call("read_alpha")]
    model = Model(*replies, call("load_beta_info_tools"), call("read_beta"),
                  AIMessageChunk(content="Both facts delivered."))
    budget = TaskBudget(BudgetLimits(context_tokens=1900, task_tokens=30000))
    with budget_scope(budget):
        result = turn(runtime(model), "Read beta." if previous_task else "Read alpha then beta.")
    assert result.reply == "Both facts delivered." and calls == ["alpha", "beta"]
    assert "read_beta" in names(model.requests[-1])
    assert "read_alpha" not in names(model.requests[-1])
    assert budget.reason is None and budget.model_calls == len(model.requests)
    assert 0 < budget.tokens <= budget.limits.task_tokens


def test_real_sdk_omits_released_schema_and_keeps_its_discovery_entry(request):
    runtime, domains, calls = request.getfixturevalue("setup")
    domains["beta"]["read_beta"].description = "Large read schema. " * 1500
    with model_replies({"tool_calls": [wire_call("load_beta_info_tools", "load")]},
                       {"tool_calls": [wire_call("read_beta", "read")]},
                       {"content": "Read complete."}) as (model, requests):
        selected = runtime(model)
        turn(selected)
    assert "read_beta" in {t["function"]["name"] for t in requests[-1]["tools"]}
    with model_replies({"content": "Hello."}) as (model, requests):
        with budget_scope(TaskBudget(BudgetLimits(context_tokens=3500, task_tokens=6000))):
            assert turn(runtime(model), "Just chat.").reply == "Hello."
    assert len(requests) == 1 and calls == ["beta"]
    assert {t["function"]["name"] for t in requests[0]["tools"]} == {
        "get_agent_capabilities", "load_alpha_info_tools", "load_beta_info_tools"}


def test_current_business_batch_is_not_evicted_to_fit_another_domain(request):
    import json

    runtime, domains, calls = request.getfixturevalue("setup")
    for domain in domains:
        domains[domain]["read_" + domain].description = "Read domain data. " * 200
    selected = runtime(Model(call("load_alpha_info_tools"), AIMessageChunk(content="Ready.")))
    turn(selected)
    batch = AIMessageChunk(content="", tool_calls=[
        {"name": "load_beta_info_tools", "id": "load-b", "args": {}},
        {"name": "read_alpha", "id": "read-a", "args": {}}])
    model = Model(batch, AIMessageChunk(content="Alpha result remains available."))
    with budget_scope(TaskBudget(BudgetLimits(context_tokens=1900, task_tokens=30000))):
        result = turn(runtime(model), "Read alpha and load beta.")
    assert result.reply == "Alpha result remains available." and calls == ["alpha"]
    assert "read_alpha" in names(model.requests[-1])
    assert "read_beta" not in names(model.requests[-1])
    error = next(m for m in model.requests[-1]["messages"] if m.get("tool_call_id") == "load-b")
    assert error["status"] == "error" and json.loads(error["content"])["status"] == "not_loaded"
    assert [d["domain"] for d in selected.store.load(config()).tool_domains["domains"]] == ["alpha"]


def test_rebalanced_checkpoint_resumes_the_same_payload_without_repeating_reads(request):
    runtime, domains, calls = request.getfixturevalue("setup")
    for domain in domains:
        domains[domain]["read_" + domain].description = "Read domain data. " * 200
    selected = runtime(Model(call("load_alpha_info_tools"), AIMessageChunk(content="Ready.")))
    turn(selected)
    interrupted = Model(call("load_beta_info_tools"), call("read_beta"), AgentCancelled())
    with budget_scope(TaskBudget(BudgetLimits(context_tokens=1900, task_tokens=30000))):
        with pytest.raises(AgentCancelled):
            turn(runtime(interrupted), "Read beta.", task_id="capacity-resume")
    resumed = Model(AIMessageChunk(content="Beta result delivered."))
    list(runtime(resumed).stream_events({"resume_task": "capacity-resume"}, config()))
    assert calls == ["beta"] and resumed.requests[0] == interrupted.requests[-1]
    assert "read_beta" in names(resumed.requests[0])
    assert "read_alpha" not in names(resumed.requests[0])
    assert [d["domain"] for d in selected.store.load(config()).tool_domains["domains"]] == ["beta"]


def test_domain_admission_includes_the_actual_system_prompt(request):
    from agent.runtime.tool_state import activate

    _, domains, _ = request.getfixturevalue("setup")
    with budget_scope(TaskBudget(BudgetLimits(context_tokens=500, task_tokens=5000))):
        session = ToolSession(Model(), config(), {}, domains)
    session.system_message = SystemMessage(content="Required system policy. " * 1000)
    assert activate(session, "beta", [HumanMessage(content="Load beta.")]) == "context_tokens"
    assert not session.state["tool_domains"]["domains"]


def test_task_working_set_releases_old_domains_with_plenty_of_model_capacity(request, monkeypatch):
    from langchain_core.messages.utils import count_tokens_approximately
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from config.settings import settings

    runtime, domains, calls = request.getfixturevalue("setup")
    monkeypatch.setattr(settings, "agent_tool_schema_tokens", 768)
    for domain in domains:
        domains[domain]["read_" + domain].description = "Read domain data. " * 100
    alpha = convert_to_openai_tool(domains["alpha"]["read_alpha"])
    beta = convert_to_openai_tool(domains["beta"]["read_beta"])
    assert count_tokens_approximately([], tools=[alpha]) <= 768
    assert count_tokens_approximately([], tools=[alpha, beta]) > 768
    selected = runtime(Model(call("load_alpha_info_tools"), call("read_alpha"),
                             AIMessageChunk(content="Alpha delivered.")))
    turn(selected)
    idle = Model(AIMessageChunk(content="You're welcome."))
    turn(runtime(idle), "Thanks.")
    assert "read_alpha" in names(idle.requests[0]) and calls == ["alpha"]
    model = Model(call("load_beta_info_tools"), call("read_beta"),
                  AIMessageChunk(content="Beta delivered."))
    assert turn(runtime(model), "Now read beta.").reply == "Beta delivered."
    assert "read_alpha" in names(model.requests[0])
    assert "read_alpha" not in names(model.requests[-1])
    assert "read_beta" in names(model.requests[-1])
    assert calls == ["alpha", "beta"]
    following = Model(AIMessageChunk(content="Still here."))
    turn(runtime(following), "Thanks again.")
    assert following.requests[0]["tools"] == model.requests[-1]["tools"]


def test_required_multi_domain_task_can_exceed_soft_target_then_bound_its_carryover(request,
                                                                                 monkeypatch):
    from config.settings import settings

    runtime, domains, calls = request.getfixturevalue("setup")
    monkeypatch.setattr(settings, "agent_tool_schema_tokens", 128)
    for domain in domains:
        domains[domain]["read_" + domain].description = "Read fixture data. " * 10
    model = Model(call("load_alpha_info_tools"), call("read_alpha"),
                  call("load_beta_info_tools"), call("read_beta"),
                  AIMessageChunk(content="Both facts delivered."))
    selected = runtime(model)
    assert turn(selected, "Read both.").reply == "Both facts delivered."
    assert names(model.requests[-1])[-2:] == ["read_alpha", "read_beta"]
    following = Model(AIMessageChunk(content="Hello."))
    assert turn(runtime(following), "Hello.").reply == "Hello."
    assert names(following.requests[0]) == [
        "get_agent_capabilities", "load_alpha_info_tools", "load_beta_info_tools", "read_beta"]
    assert calls == ["alpha", "beta"]
    assert [d["domain"] for d in selected.store.load(config()).tool_domains["domains"]] == ["beta"]


def test_soft_target_alone_releases_heavy_carryover_without_an_extra_model_call(request):
    runtime, domains, calls = request.getfixturevalue("setup")
    domains["beta"]["read_beta"].description = "Large read schema. " * 1500
    with model_replies({"tool_calls": [wire_call("load_beta_info_tools", "load")]},
                       {"tool_calls": [wire_call("read_beta", "read")]},
                       {"content": "Read complete."}) as (model, requests):
        selected = runtime(model)
        turn(selected)
    loaded = requests[-1]["tools"]
    assert "read_beta" in {s["function"]["name"] for s in loaded}
    with model_replies({"content": "Hello."}, {"content": "Still here."}) as (model, requests):
        selected = runtime(model)
        assert turn(selected, "Just chat.").reply == "Hello."
        assert turn(selected, "Another chat.").reply == "Still here."
    assert len(requests) == 2 and calls == ["beta"]
    assert requests[0]["tools"] == requests[1]["tools"]
    assert {s["function"]["name"] for s in requests[0]["tools"]} == {
        "get_agent_capabilities", "load_alpha_info_tools", "load_beta_info_tools"}
    assert selected.store.load(config()).tool_domains["domains"] == []


def test_released_authorized_read_can_reactivate_without_an_extra_discovery_round(request):
    runtime, domains, calls = request.getfixturevalue("setup")
    domains["beta"]["read_beta"].description = "Large read schema. " * 1500
    turn(runtime(Model(call("load_beta_info_tools"), call("read_beta"),
                       AIMessageChunk(content="Beta delivered."))))
    with model_replies({"tool_calls": [wire_call("read_beta", "again")]},
                       {"content": "Fresh beta fact."}) as (model, requests):
        assert turn(runtime(model), "Read the same source again.").reply == "Fresh beta fact."
    assert calls == ["beta", "beta"] and len(requests) == 2
    assert "read_beta" not in {s["function"]["name"] for s in requests[0]["tools"]}
    assert "read_beta" in {s["function"]["name"] for s in requests[1]["tools"]}


def test_large_latest_domain_does_not_prevent_retaining_a_smaller_domain(request, monkeypatch):
    from config.settings import settings

    runtime, domains, calls = request.getfixturevalue("setup")
    monkeypatch.setattr(settings, "agent_tool_schema_tokens", 768)
    domains["beta"]["read_beta"].description = "Large read schema. " * 1500
    turn(runtime(Model(call("load_alpha_info_tools"), call("read_alpha"),
                       call("load_beta_info_tools"), call("read_beta"),
                       AIMessageChunk(content="Both delivered."))))
    model = Model(AIMessageChunk(content="Hello."))
    assert turn(runtime(model), "Just chat.").reply == "Hello."
    assert "read_alpha" in names(model.requests[0])
    assert "read_beta" not in names(model.requests[0])
    assert calls == ["alpha", "beta"]


def test_fixed_catalog_does_not_evict_a_reused_domain_in_the_actual_sdk_request(request,
                                                                             monkeypatch):
    from config.settings import settings

    runtime, domains, calls = request.getfixturevalue("setup")
    monkeypatch.setattr(settings, "agent_tool_schema_tokens", 768)
    domains["alpha"]["read_alpha"].description = "Read domain data. " * 100
    descriptions = {"alpha": "A stable catalog description. " * 250}
    with model_replies({"tool_calls": [wire_call("load_alpha_info_tools", "load")]},
                       {"tool_calls": [wire_call("read_alpha", "read")]},
                       {"content": "First fact delivered."}) as (model, requests):
        selected = runtime(model)
        selected.application.descriptions = descriptions
        turn(selected)
    loaded = requests[-1]["tools"]
    with model_replies({"tool_calls": [wire_call("read_alpha", "again")]},
                       {"content": "Fresh fact delivered."}) as (model, requests):
        selected = runtime(model)
        selected.application.descriptions = descriptions
        assert turn(selected, "Read alpha again.").reply == "Fresh fact delivered."
    assert len(requests) == 2 and calls == ["alpha", "alpha"]
    assert requests[0]["tools"] == requests[1]["tools"] == loaded
