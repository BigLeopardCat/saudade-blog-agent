"""Domain activation preserves wire schemas, scope, batch state and request budgets."""

import json
from dataclasses import replace
from operator import add
from typing import Annotated

import httpx
import pytest
from _memory_support import Capture, config
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from agent import checkpoint
from agent.api.context import build_messages
from agent.api.schemas import ChatRequest
from agent.domains.activity_search import search_public_activity
from agent.products.blog.application import BlogApplication
from agent.products.blog.capabilities import ToolCatalog
from agent.products.blog.history import read_tool
from agent.runtime.budget import BudgetLimits, TaskBudget, budget_scope
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.conversation_store import bound_tool_domains
from agent.runtime.service import AgentRuntime
from agent.runtime.session import ToolSession
from agent.runtime.tool_state import activate, apply
from agent.runtime.turns import AgentCancelled
from models.llm import get_llm
from tools import base
from tools.notes import search_notes
from tools.result import empty as empty_result
from tools.result import ok, unavailable


def session():
    return ToolSession(Capture(), config(), {search_notes.name: search_notes},
                       {"public_activity": {search_public_activity.name: search_public_activity}})


def saved_domains():
    return {"scope": {"uid": 7, "role": "user", "conversation_id": 42},
            "domains": [{"domain": "public_activity", "turn_id": "original"}]}


def test_saved_activation_uses_current_canonical_schema_without_inline_definitions():
    current = session()
    baseline = list(current.menu)
    saved = saved_domains()
    saved["domains"].append({"domain": "unknown_operation", "turn_id": "original"})
    apply(current, saved)
    assert current.menu == [*baseline, convert_to_openai_tool(search_public_activity)]
    assert [item["domain"] for item in current.state["tool_domains"]["domains"]] == [
        "public_activity"]
    forged = saved_domains()
    forged["domains"][0]["schema"] = {"injected_parameter": "forged"}
    apply(current, forged)
    assert current.menu == baseline and not current.state["tool_domains"]["domains"]


def test_new_domain_rejection_preserves_complete_menu_without_spending_or_poisoning_budget():
    current, prompt = session(), [HumanMessage(content="original source request")]
    baseline = list(current.menu)
    estimate = current.budget.input_tokens(prompt, baseline, model_key=current.model)
    current.budget = TaskBudget(BudgetLimits(input_tokens=estimate,
        context_tokens=estimate+current.output_limit+1, task_tokens=100000))
    assert activate(current, "public_activity", prompt) == "context_tokens"
    assert current.menu == baseline and current.budget.reason is None
    assert current.budget.model_calls == current.budget.tool_calls == current.budget.tokens == 0
    current.budget = TaskBudget()
    assert activate(current, "public_activity", prompt) is None
    assert current.menu == [*baseline, convert_to_openai_tool(search_public_activity)]


def test_cancellation_during_domain_admission_rolls_back_the_uncommitted_menu(monkeypatch):
    current = session()
    baseline = list(current.menu)
    def cancelled(*args, **kwargs):
        raise AgentCancelled()
    monkeypatch.setattr(current.budget, "call_block_reason", cancelled)
    with pytest.raises(AgentCancelled):
        activate(current, "public_activity", [])
    assert current.menu == baseline and not current.state["tool_domains"]["domains"]


@pytest.mark.parametrize("change", ["uid", "role", "conversation_id", "forged"])
def test_activation_cannot_cross_actor_role_or_conversation_scope(change):
    saved = saved_domains()
    assert bound_tool_domains(saved, config()) == saved
    if change == "forged":
        saved["extra"] = "untrusted"
    else:
        saved["scope"][change] = {"uid": 8, "role": "admin", "conversation_id": 43}[change]
    assert bound_tool_domains(saved, config()) is None


@pytest.mark.parametrize("name", ["prepare_favorite_change", "write_todos", "navigate_page"])
def test_direct_read_activation_excludes_preparation_planning_or_business_write_tools(name):
    @tool(name)
    def action() -> object:
        """A non-read fixture with an existing product permission."""
        raise AssertionError("A schema check must never execute a tool.")
    assert read_tool(name, {name: action}, lambda _: True) is None


def test_direct_read_activation_revalidates_current_catalog_and_authorization():
    assert read_tool(search_public_activity.name, {}, lambda _: True) is None
    assert read_tool(search_public_activity.name,
                     {search_public_activity.name: search_public_activity}, lambda _: False) is None


@pytest.mark.parametrize("second_kind", ["ok", "unavailable"])
def test_same_step_read_commands_recover_stably_without_losing_other_updates(second_kind):
    calls, observed = [], []

    @tool
    def get_blog_info() -> object:
        """Read an empty public fixture."""
        calls.append("a")
        return empty_result("No matching rows.")

    @tool
    def get_social_links() -> object:
        """Read a second public fixture."""
        calls.append("b")
        return ok("Second fact.") if second_kind == "ok" else unavailable("Unavailable.")

    class FixtureState(AgentState):
        fixture_updates: Annotated[list[str], add]

    class FixtureCommands(AgentMiddleware):
        state_schema = FixtureState
        def __init__(self, _session):
            pass
        def before_model(self, state, runtime):
            observed.append(list(state.get("fixture_updates", [])))
        def wrap_tool_call(self, request, handler):
            result = handler(request)
            update = {"fixture_updates": [request.tool_call["name"]]}
            if isinstance(result, Command):
                return replace(result, update={**result.update, **update})
            return Command(update={"messages": [result], **update})

    saver = InMemorySaver()
    def runtime(model):
        application = BlogApplication(ToolCatalog(read_tools={
            get_blog_info.name: get_blog_info, get_social_links.name: get_social_links},
            site_read_tools={}))
        application.middleware = (*application.middleware, FixtureCommands)
        return AgentRuntime(CheckpointConversationStore(saver), application=application,
                            model_factory=lambda **_: model)
    def responses():
        yield AIMessage(content="", tool_calls=[
            {"name": get_blog_info.name, "id": "a", "args": {}},
            {"name": get_social_links.name, "id": "b", "args": {}}])
        raise AgentCancelled()
    model = Capture()
    model.responses = responses()
    selected = runtime(model)
    with pytest.raises(AgentCancelled):
        list(selected.stream_events({"messages": [HumanMessage(content="Read both sources.")],
             "task_id": "batch-run", "turn_id": "batch-turn"}, config()))
    assert calls == ["a", "b"] and selected.store.load(config()).tool_domains is None
    assert observed[-1] == ["get_blog_info", "get_social_links"]
    read_messages = [message for message in model.prompts[-1] if message.type == "tool"]
    assert [message.tool_call_id for message in read_messages] == ["a", "b"]
    rebuilt = runtime(Capture(AIMessage(content="Both source outcomes are preserved.")))
    list(rebuilt.stream_events({"resume_task": "batch-run"}, config()))
    assert calls == ["a", "b"]
    assert rebuilt.store.load(config()).tool_domains["domains"] == []
    assert observed[-1] == ["get_blog_info", "get_social_links"]
    rebuilt.update_state(config(), {"tool_domains": None})
    next_turn = runtime(Capture(AIMessage(content="A new turn without optional domains.")))
    list(next_turn.stream_events({"messages": [HumanMessage(content="New task.")]}, config()))
    assert next_turn.store.load(config()).tool_domains["domains"] == []


def assert_followup_wire(turn1, turn2, budgets, reuse, first, backend_calls):
    assert len(turn1) == budgets[0].model_calls == 3
    assert len(turn2) == budgets[1].model_calls == (2 if reuse else 3)
    initial = [item["function"]["name"] for item in turn1[0]["tools"]]
    continued = [item["function"]["name"] for item in turn2[0]["tools"]]
    assert turn1[-1]["tools"] == turn1[-2]["tools"]
    assert turn2[0]["tools"] == turn1[-1]["tools"] if reuse else continued == initial
    expected_reads = {"get_announcements", "list_guestbook", "list_talks"}
    assert (expected_reads.issubset(continued) if reuse
            else not expected_reads.intersection(continued))
    assert any(first in message.get("content", "") for message in turn2[0]["messages"])
    assert len(backend_calls) == 2 and backend_calls[1] == {
        "keywords": ["猫咪"], "page": 1, "page_size": 3}
    return initial, continued


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_real_sdk_followup_retains_domain_schema_and_independent_task_budget(
    monkeypatch, reuse, empty, tmp_path
):
    requests, backend_calls = [], []
    phase = {"turn": 1, "step": 0}
    total = 0 if empty else 1

    def backend(request):
        assert request.method == "POST" and request.url.path == "/api/public/activity/search"
        body = json.loads(request.content)
        backend_calls.append(body)
        counts = {"announcements": total, "board": 0, "talk": 0}
        data = {"keywords": body["keywords"], "keywordMode": "any",
            "keywordCounts": [{"keyword": word, "total": total, "counts": counts}
                              for word in body["keywords"]],
            "matchMode": "literal_case_insensitive", "sources": ["announcements", "board", "talk"],
            "counts": counts, "total": total, "page": body["page"], "pageSize": body["page_size"],
            "hasMore": False, "items": ([] if empty else [{"id": 9, "source": "announcements",
                "title": "泠月猫咪", "titleTruncated": False, "content": "泠月猫咪的合成公告正文",
                "contentTruncated": False, "createdAt": None, "matchedKeywordIndices": [0]}])}
        return httpx.Response(200, json={"code": 200, "data": data})

    def model(request):
        body = json.loads(request.content)
        requests.append((phase["turn"], body))
        names = {tool["function"]["name"] for tool in body["tools"]}
        tool_messages = [message for message in body["messages"] if message["role"] == "tool"]
        last_call = (next((message for message in reversed(body["messages"])
                          if message["role"] == "assistant" and message.get("tool_calls")), None))
        last_name = last_call["tool_calls"][0]["function"]["name"] if last_call else None
        if phase["step"] == 0 and "search_public_activity" not in names:
            call = {"name": "load_public_activity_info_tools", "arguments": "{}"}
        elif phase["step"] == 0 or last_name == "load_public_activity_info_tools":
            call = {"name": "search_public_activity", "arguments": json.dumps({
                "keywords": ["泠月" if phase["turn"] == 1 else "猫咪"], "page": 1, "limit": 3})}
        else:
            assert tool_messages
            call = None
        delta = ({"role": "assistant", "content": "", "tool_calls": [{"index": 0,
            "id": "call-"+str(len(requests)), "type": "function", "function": call}]}
            if call else {"role": "assistant", "content": "已完成本轮公开来源检索。"})
        phase["step"] += 1
        response = {"id": "fixture", "model": "fixture", "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta,
                         "finish_reason": "tool_calls" if call else "stop"}],
            "usage": {"prompt_tokens": 4000, "completion_tokens": 10, "total_tokens": 4010}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            text="data: "+json.dumps(response)+"\n\ndata: [DONE]\n\n")

    with httpx.Client(transport=httpx.MockTransport(backend)) as backend_client, \
            httpx.Client(transport=httpx.MockTransport(model)) as model_client:
        monkeypatch.setattr(base, "_client", backend_client)
        saver = InMemorySaver()
        def runtime():
            return AgentRuntime(CheckpointConversationStore(saver), application=BlogApplication(),
                model_factory=lambda **kwargs: get_llm(model="fixture", api_key="fixture-key",
                    base_url="https://model.invalid/v1", http_client=model_client,
                    **{**kwargs, "max_tokens": 256}))
        budgets = [TaskBudget(), TaskBudget()]
        first = "在公开公告、留言和说说里，先检索“泠月”，只看第一页、最多3条。"
        following = "猫咪的关键词也搜一下，还是只看第一页、最多3条。"
        selected = runtime()
        for number, message in enumerate((first, following)):
            phase.update(turn=number+1, step=0)
            selected = runtime()
            if number == 1 and not reuse:
                selected.store.update_state(config(), {"tool_domains": None})
            with budget_scope(budgets[number]):
                result = list(selected.stream_events({"messages": build_messages(ChatRequest(
                    message=message, conversation_id=42), holds_context=True),
                    "turn_id": f"turn-{number}"}, config()))[-1]
            assert result.reply == "已完成本轮公开来源检索。"
        turn1 = [body for turn, body in requests if turn == 1]
        turn2 = [body for turn, body in requests if turn == 2]
        initial, continued = assert_followup_wire(
            turn1, turn2, budgets, reuse, first, backend_calls)
        saved = selected.store.load(config()).tool_domains
        assert saved["domains"] == [{"domain": "public_activity", "turn_id": (
            "turn-0" if reuse else "turn-1")}]
        assert saved["scope"] == {"uid": 7, "role": "user", "conversation_id": 42}
        assert checkpoint.drop_last_user_turn(selected, 42, "turn-1")["dropped"]
        retained = selected.store.load(config()).tool_domains["domains"]
        assert retained == (saved["domains"] if reuse else [])
        cost = {"reuse": reuse, "empty": empty, "first_menu": len(initial),
                "next_menu": len(continued), "next_model_calls": len(turn2),
                "first_schema_chars": len(json.dumps(turn1[0]["tools"], ensure_ascii=False)),
                "next_schema_chars": len(json.dumps(turn2[0]["tools"], ensure_ascii=False))}
        (tmp_path / "wire-cost.json").write_text(json.dumps(cost))


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
def test_real_sdk_keeps_the_entire_schema_prefix_across_different_read_tools(provider, monkeypatch):
    from hashlib import sha256

    from _framework_fixture import call as wire_call
    from _framework_fixture import model_replies

    @tool
    def get_blog_info() -> object:
        """Read the first synthetic public source."""
        return ok("First fact.")

    @tool
    def get_social_links() -> object:
        """Read the second synthetic public source."""
        return ok("Second fact.")

    class Catalog(ToolCatalog):
        def initial_tools(self, own_enabled):
            return {}

        def domains(self, config, can_prepare):
            return {"alpha": {get_blog_info.name: get_blog_info},
                    "beta": {get_social_links.name: get_social_links}}

    saver = InMemorySaver()

    def selected(model):
        if provider == "deepseek":
            from models.deepseek import DeepSeekChat

            monkeypatch.setattr("config.settings.llm_provider", "opencode")
            model = get_llm(model="deepseek-v4.1-flash", api_key="synthetic",
                base_url="https://model.fixture.test/v1", http_client=model.http_client,
                streaming=True, stream_usage=True, max_tokens=128, enable_thinking=False)
            assert isinstance(model, DeepSeekChat)
        return AgentRuntime(CheckpointConversationStore(saver),
                            application=BlogApplication(Catalog()), model_factory=lambda **_: model)

    with model_replies(
            {"tool_calls": [wire_call("load_alpha_info_tools", "load-a")]},
            {"tool_calls": [wire_call("get_blog_info", "first-read")]},
            {"content": "First fact."}) as (first_model, first_requests):
        list(selected(first_model).stream_events(
            {"messages": [HumanMessage(content="Read alpha.")]}, config()))
    with model_replies(
            {"tool_calls": [wire_call("load_beta_info_tools", "load-b")]},
            {"tool_calls": [wire_call("get_social_links", "second-read")]},
            {"tool_calls": [wire_call("get_blog_info", "third-read")]},
            {"content": "Both facts."}) as (next_model, requests):
        list(selected(next_model).stream_events({"messages": [HumanMessage(content="Read both.")]},
                                                config()))
    before = first_requests[-1]["tools"]
    assert requests[0]["tools"] == before
    assert requests[1]["tools"][:len(before)] == before
    assert requests[1]["tools"] == requests[2]["tools"] == requests[3]["tools"]
    fingerprints = [sha256(json.dumps(row["tools"], separators=(",", ":")).encode()).hexdigest()
                    for row in requests[1:]]
    assert len(set(fingerprints)) == 1
    systems = [row["messages"][0] for row in [*first_requests, *requests]]
    assert all(item == systems[0] for item in systems)


def test_saved_domains_are_isolated_from_background_runs_and_invalid_bindings():
    from agent.runtime.task_scope import background_thread

    foreground = saved_domains()
    background = config()
    background["configurable"].update(
        conversation_id=None, background_run_id="background-one",
        thread_id=background_thread(7, "background-one"))
    assert bound_tool_domains(foreground, background) is None
    value = {"scope": {"uid": 7, "role": "user", "background_run_id": "background-one"},
             "domains": foreground["domains"]}
    assert bound_tool_domains(value, background) == value
    assert bound_tool_domains(value, config()) is None
    background["configurable"]["thread_id"] = background_thread(7, "background-two")
    assert bound_tool_domains(value, background) is None


def test_direct_read_cannot_promote_other_unauthorized_tools_in_its_domain():
    from agent.runtime.tool_state import activate_read

    current = session()
    original = list(current.menu)
    decision = activate_read(current, "search_public_activity", [],
                             authorized=lambda _: False)
    assert decision == {"domain": "public_activity", "status": "rejected",
                        "reason": "domain_not_permitted"}
    assert current.menu == original and current.state["tool_domains"] is None
