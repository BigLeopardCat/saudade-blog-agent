"""Source facts remain separate from proposed text and signed confirmation credentials."""

import json
import secrets

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from agent.runtime.results import TurnResult
from agent.runtime.session import ToolSession
from agent.runtime.tool_evidence import MAX_FACT_CHARS, observed, scope
from eval import native_assertions, online_judge


def test_framework_tool_scope_traces_only_selected_original_facts(monkeypatch, tmp_path):
    credential = secrets.token_urlsafe()
    events = []
    def record(node, event, **data):
        events.append({"node": node, "event": event, **data})
    monkeypatch.setattr("agent.runtime.tool_evidence.record", record)
    monkeypatch.setattr("agent.runtime.session.record", record)

    @tool
    def prepare_change() -> object:
        """Prepare a synthetic change."""
        observed("categories", {"before": {"id": 9}, "articleCount": 5,
                                "after": {"content": "UNVERIFIED_DRAFT"}},
                 ("before", "articleCount"))
        return TurnResult("Confirm?", {"pending_confirm": {"token": credential,
            "q": "UNVERIFIED_DRAFT", "specs": [{"content": "UNVERIFIED_DRAFT"}],
            "preparation_facts": {"execution_policy": {"result_notification": "best_effort"}}}})

    class Model:
        max_tokens = 128
        def bind_tools(self, *args, **kwargs):
            return self
        def invoke(self, messages, **kwargs):
            return AIMessage(content="", tool_calls=[
                {"name": "prepare_change", "args": {}, "id": "framework-call"}])

    session = ToolSession(Model(), {}, {prepare_change.name: prepare_change}, {})
    list(session.stream([HumanMessage(content="Prepare the change")]))
    evidence = next(event for event in events if event["event"] == "preparation_observation")
    assert evidence["call_id"] == "framework-call"
    assert evidence["facts"] == {"before": {"id": 9}, "articleCount": 5}
    assert credential not in json.dumps(events) and "UNVERIFIED_DRAFT" not in json.dumps(events)
    context = next(event for event in events if event["event"] == "preparation_context")
    assert context["call_id"] == "framework-call" and context["complete"] is True
    assert context["facts"] == {"execution_policy": {"result_notification": "best_effort"}}
    path = tmp_path / "trace.json"
    path.write_text(json.dumps({"input": {"message": "prepare"}, "events": events}))
    material = json.loads(online_judge.material({"id": "prepare"}, [
        {"trace": str(path), "text": "Please confirm"}]))["turns"][0]
    assert material["runtime_preparation_context"] == [context]
    assert material["validated_preparation_facts"] == [evidence]
    assert material["execution_receipts"] == []



def test_oversize_facts_are_explicitly_incomplete(monkeypatch):
    events = []
    monkeypatch.setattr("agent.runtime.tool_evidence.record", lambda *_, **kw: events.append(kw))
    with scope({"id": "a"}):
        observed("test", {"before": "x" * MAX_FACT_CHARS}, ("before",))
    assert events[0]["complete"] is False and events[0]["facts"] is None


def test_todo_creation_records_verified_owner_without_promoting_draft_text(monkeypatch):
    from _own_support import config

    from agent import confirm
    from agent.domains.todos.preparation import prepare_dashboard_todo
    from agent.principal import SOURCE_ASSERTION, Principal

    events = []
    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    monkeypatch.setattr("agent.runtime.tool_evidence.record", lambda node, event, **data:
                        events.append({"node": node, "event": event, **data}))
    cfg = config(principal=Principal(7, "admin", SOURCE_ASSERTION))
    with scope({"id": "create-todo"}, 2):
        result = prepare_dashboard_todo.invoke(
            {"action": "create", "text": "UNVERIFIED_DRAFT_CANARY"}, config=cfg)
    assert result.execution["pending_confirm"]
    assert events == [{"node": "native", "event": "preparation_observation",
                       "call_id": "create-todo", "round": 2, "source": "todo_create",
                       "facts": {"ownerUserId": 7}, "complete": True}]
    assert "UNVERIFIED_DRAFT_CANARY" not in json.dumps(events)
    assert result.execution["pending_confirm"]["token"] not in json.dumps(events)


def test_noop_requires_same_call_verified_target_and_state():
    wanted = {"workflow": "set_status", "spec": {"tool": "set_article_status",
              "args": {"article_id": 54, "status": "private"}},
              "noop_tools": ["prepare_article_state"], "source": "article_state",
              "facts": {"id": 54, "status": "private"}}
    events = [{"node": "native", "event": "call", "kind": "noop",
               "name": "prepare_article_state", "call_id": "a"},
              {"event": "preparation_observation", "source": "article_state", "call_id": "a",
               "complete": True, "facts": {"id": 54, "status": "private"}}]
    gold = {"require_prepared_or_noop": [wanted]}
    assert native_assertions.check(gold, {"native_events": events}) == []
    for field, bad in (("call_id", "other"), ("complete", False),
                       ("round", 2), ("facts", {"id": 23, "status": "private"})):
        altered = [events[0], {**events[1], field: bad}]
        assert native_assertions.check(gold, {"native_events": altered})


def test_semantic_material_includes_verified_facts_but_not_draft_as_evidence(tmp_path):
    trace = {"input": {"message": "Prepare a notice", "principal_role": "admin"}, "events": [
        {"node": "native", "event": "call", "name": "prepare_user_notice",
         "kind": "prepared", "call_id": "a"},
        {"node": "native", "event": "preparation_observation", "call_id": "a",
         "source": "notices", "complete": True, "facts": {"recipient": {"id": 20}}}]}
    path = tmp_path / "trace.json"
    path.write_text(json.dumps(trace))
    result = {"trace": str(path), "text": "Please confirm", "confirm_payloads": [{
        "specs": [{"tool": "send_user_notice", "args": {"content": "Invented recovery rule"}}]}]}
    material = json.loads(online_judge.material({"id": "task"}, [result]))["turns"][0]
    assert material["validated_preparation_facts"][0]["facts"] == {"recipient": {"id": 20}}
    assert "Invented recovery rule" not in str(material["validated_preparation_facts"])
    assert "Invented recovery rule" in str(material["proposed_actions_not_facts"])


def test_selection_policy_is_initial_default_without_execution(monkeypatch):
    from agent import confirm
    from agent.principal import Principal
    from agent.proposals import proposal

    monkeypatch.setattr(confirm.settings, "confirmation_secret", secrets.token_urlsafe(32))
    facts = {"verified_count": 2}
    result = proposal(Principal(7, "admin", "assertion"), 42, "article_deletions",
                      [{"tool": "delete_articles", "args": {"items": [{"id": ident,
                        "version": "a" * 64}]}} for ident in (7, 8)],
                      executor="article_deletions", question="Review both", target="two items",
                      choices=["first", "second"], preparation_facts=facts)
    popup = result.execution["pending_confirm"]
    assert popup["preparation_facts"] == {"verified_count": 2, "selection_policy": {
        "initial_selection": "all", "user_changeable": True}}
    assert [o["value"] for o in popup["opts"]] == ["yes", "pick:0", "pick:1", "no"]
    assert facts == {"verified_count": 2} and result.execution["receipts"] == []


def test_oversize_preparation_context_does_not_leak_partial_facts(monkeypatch):
    from agent.runtime.tool_evidence import preparation_context

    events = []
    monkeypatch.setattr("agent.runtime.tool_evidence.record", lambda *_, **kw: events.append(kw))
    preparation_context({"id": "large"}, 3, {"preparation_facts": {"x": "private" * MAX_FACT_CHARS}})
    assert events == [{"call_id": "large", "round": 3, "facts": None, "complete": False}]
