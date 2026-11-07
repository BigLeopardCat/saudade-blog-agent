"""One committed read capability survives compaction without retaining an entire domain."""

import json
from dataclasses import replace
from operator import add
from types import SimpleNamespace
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
from agent.products.blog.tool_policy import BlogToolMiddleware
from agent.runtime.budget import BudgetLimits, TaskBudget, budget_scope
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.conversation_store import bound_recent_read
from agent.runtime.service import AgentRuntime
from agent.runtime.tool_state import model_menu
from agent.runtime.turns import AgentCancelled
from models.llm import get_llm
from tools import base
from tools.notes import search_notes
from tools.result import empty as empty_result
from tools.result import ok, unavailable


def session():
    protected = convert_to_openai_tool(search_notes)
    return SimpleNamespace(menu=[protected], execution_tools={
        search_notes.name: search_notes, search_public_activity.name: search_public_activity},
        budget=TaskBudget(), output_limit=64)


def test_standard_override_preserves_protected_tools_and_uses_current_canonical_schema():
    current = session()
    forged = {"type": "function", "function": {"name": "search_public_activity",
              "parameters": {"type": "object", "properties": {"password": {"type": "string"}}}}}
    unknown = {"type": "function", "function": {"name": "unknown_operation"}}
    chosen = model_menu(current, [forged, unknown], [HumanMessage(content="follow up")], object())
    assert chosen == [*current.menu, convert_to_openai_tool(search_public_activity)]
    assert model_menu(current, [], [], object()) == current.menu


def test_complete_input_and_output_budget_can_omit_hint_without_spending_or_poisoning_budget():
    current, model = session(), object()
    prompt = [HumanMessage(content="original source request")]
    baseline = current.budget.input_tokens(prompt, current.menu, model_key=model)
    current.budget = TaskBudget(BudgetLimits(input_tokens=baseline,
        context_tokens=baseline+current.output_limit+1, task_tokens=100000))
    chosen = model_menu(current, [search_public_activity], prompt, model)
    assert chosen == current.menu and current.budget.reason is None
    assert current.budget.model_calls == current.budget.tool_calls == current.budget.tokens == 0
    current.budget = TaskBudget()
    assert model_menu(current, [search_public_activity], prompt, model) == [
        *current.menu, convert_to_openai_tool(search_public_activity)]


def test_cancellation_from_request_budget_is_not_swallowed(monkeypatch):
    current = session()
    def cancelled(*args, **kwargs):
        raise AgentCancelled()
    monkeypatch.setattr(current.budget, "call_block_reason", cancelled)
    with pytest.raises(AgentCancelled):
        model_menu(current, [search_public_activity], [], object())


@pytest.mark.parametrize("change", ["uid", "role", "conversation_id", "forged"])
def test_hint_cannot_cross_actor_role_or_conversation_scope(change):
    hint = {"tool_name": "search_public_activity", "turn_id": "original",
            "scope": {"uid": 7, "role": "user", "conversation_id": 42}}
    assert bound_recent_read(hint, config()) == hint
    if change == "forged":
        hint["extra"] = "untrusted"
    else:
        hint["scope"][change] = {"uid": 8, "role": "admin", "conversation_id": 43}[change]
    assert bound_recent_read(hint, config()) is None


@pytest.mark.parametrize("name", ["prepare_favorite_change", "write_todos", "navigate_page"])
def test_hint_cannot_expose_preparation_planning_or_business_write_tools(name):
    @tool(name)
    def action() -> object:
        """A non-read fixture with an existing product permission."""
        raise AssertionError("A menu hint must never execute a tool.")
    assert read_tool(name, {name: action}, lambda _: True) is None


def test_hint_revalidates_current_catalog_and_authorization():
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
    assert calls == ["a", "b"] and selected.store.load(config()).recent_read is None
    assert observed[-1] == ["get_blog_info", "get_social_links"]
    read_messages = [message for message in model.prompts[-1] if message.type == "tool"]
    assert [message.tool_call_id for message in read_messages] == ["a", "b"]
    rebuilt = runtime(Capture(AIMessage(content="Both source outcomes are preserved.")))
    list(rebuilt.stream_events({"resume_task": "batch-run"}, config()))
    assert calls == ["a", "b"]
    expected = "get_social_links" if second_kind == "ok" else "get_blog_info"
    assert rebuilt.store.load(config()).recent_read == {
        "tool_name": expected, "turn_id": "batch-turn",
        "scope": {"uid": 7, "role": "user", "conversation_id": 42}}
    assert observed[-1] == ["get_blog_info", "get_social_links"]
    rebuilt.update_state(config(), {"recent_read": None})
    next_turn = runtime(Capture(AIMessage(content="A new turn with the hint cleared.")))
    list(next_turn.stream_events({"messages": [HumanMessage(content="New task.")]}, config()))
    assert next_turn.store.load(config()).recent_read is None


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_real_sdk_followup_has_one_read_schema_and_independent_task_budget(
    monkeypatch, reuse, empty, tmp_path
):
    if not reuse:
        monkeypatch.setattr(BlogToolMiddleware, "wrap_model_call",
                            lambda self, request, handler: handler(request))
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
            with budget_scope(budgets[number]):
                result = list(selected.stream_events({"messages": build_messages(ChatRequest(
                    message=message, conversation_id=42), holds_context=True),
                    "turn_id": f"turn-{number}"}, config()))[-1]
            assert result.reply == "已完成本轮公开来源检索。"
        turn1 = [body for turn, body in requests if turn == 1]
        turn2 = [body for turn, body in requests if turn == 2]
        assert len(turn1) == budgets[0].model_calls == 3
        assert len(turn2) == budgets[1].model_calls == (2 if reuse else 3)
        initial = [item["function"]["name"] for item in turn1[0]["tools"]]
        continued = [item["function"]["name"] for item in turn2[0]["tools"]]
        assert continued == [*initial, "search_public_activity"] if reuse else continued == initial
        assert not {"get_announcements", "list_guestbook", "list_talks"}.intersection(continued)
        assert any(first in message.get("content", "") for message in turn2[0]["messages"])
        assert len(backend_calls) == 2 and backend_calls[1] == {
            "keywords": ["猫咪"], "page": 1, "page_size": 3}
        hint = selected.store.load(config()).recent_read
        assert hint == {"tool_name": "search_public_activity", "turn_id": "turn-1",
                        "scope": {"uid": 7, "role": "user", "conversation_id": 42}}
        assert checkpoint.drop_last_user_turn(selected, 42, "turn-1")["dropped"]
        assert selected.store.load(config()).recent_read is None
        cost = {"reuse": reuse, "empty": empty, "first_menu": len(initial),
                "next_menu": len(continued), "next_model_calls": len(turn2),
                "first_schema_chars": len(json.dumps(turn1[0]["tools"], ensure_ascii=False)),
                "next_schema_chars": len(json.dumps(turn2[0]["tools"], ensure_ascii=False))}
        (tmp_path / "wire-cost.json").write_text(json.dumps(cost))
