"""Navigation and bounded source reads remain independent observed outcomes."""

import json
import time
from threading import Event

import pytest
from _memory_support import Capture
from _own_support import config
from langchain_core.messages import AIMessageChunk, HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from agent.domains import browser, public_activity
from agent.products.blog.application import BlogApplication
from agent.runtime.checkpoint_conversations import CheckpointConversationStore
from agent.runtime.control import AgentCancelled, RequestControl, request_scope
from agent.runtime.service import AgentRuntime
from tools.result import ok, unavailable


@pytest.mark.parametrize("include_source", [False, True])
def test_compound_task_acceptance_requires_navigation_and_verified_content(include_source):
    from eval.golden_evaluation import check_gold
    from eval.online_baseline import load_suite

    case = next(c for c in load_suite() if c["id"] == "nav_guestbook_content")
    value = {"status": "sent"}
    if include_source:
        value["preview"] = {"kind": "ok", "data": preview().data}
    result = {"text": "Navigation sent.", "commands": ["NAVIGATE:/guestbook"],
              "tool_calls": ["navigate_page"], "exec_rows": [],
              "native_events": [{"node": "native", "event": "llm_done", "tokens_prompt": 20,
                                  "tokens_completion": 5},
                                 {"node": "native", "event": "call", "name": "navigate_page",
                                  "args": {"path": "/guestbook", "preview": True},
                                  "kind": "ok", "result": json.dumps(value)}]}
    failures = check_gold(case["gold"], result)
    assert (not failures) is include_source, failures


def call(**args):
    return {"name": "navigate_page", "args": {"path": "/guestbook", **args},
            "id": "visit", "type": "tool_call"}


def preview():
    return ok("not duplicated in the model content", data={
        "resource": "activity/guestbook", "items": [{"id": 9, "content": "public source"}],
        "total": 12, "page": 1, "pageSize": 5, "hasMore": True})


def test_navigation_and_preview_share_one_framework_exchange(monkeypatch):
    seen = []
    monkeypatch.setattr(public_activity, "page_preview",
                        lambda path: seen.append(path) or preview())
    model = Capture(AIMessageChunk(content="", tool_calls=[call(preview=True)]),
                    AIMessageChunk(content="Navigation sent; recent public source received."))
    runtime = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
                           application=BlogApplication(), model_factory=lambda **_: model)
    result = list(runtime.stream_events(
        {"messages": [HumanMessage(content="Visit the guestbook and read its latest posts.")]},
        config()))[-1]
    assert result.reply and seen == ["/guestbook"] and len(model.prompts) == 2
    message = next(m for m in model.prompts[-1] if isinstance(m, ToolMessage))
    data = json.loads(message.content)
    assert data == message.artifact["data"]
    assert data["status"] == "sent" and data["completion_confirmed"] is False
    assert data["preview"]["data"]["total"] == 12
    assert data["preview"]["kind"] == "ok" and "message" not in data["preview"]
    assert "not duplicated" not in message.content
    assert message.artifact["receipts"][0]["cmd"]["kind"] == "navigate"


@pytest.mark.parametrize("state", ["completed", "timeout", "failed"])
def test_preview_waits_for_browser_completion_and_skips_failed_navigation(monkeypatch, state):
    seen = []
    monkeypatch.setattr(browser, "_await_browser", lambda *args: {"status": state,
                                                                "path": "/guestbook"})
    monkeypatch.setattr(public_activity, "page_preview",
                        lambda path: seen.append(path) or preview())
    value = browser.navigate_page.invoke(call(preview=True),
        config=config(browser_feedback=True, browser_task={"id": "task"}))
    frame = json.loads(value.content)
    assert frame["completion_confirmed"] is (state == "completed")
    assert seen == (["/guestbook"] if state == "completed" else [])
    assert frame["preview"]["kind"] == ("ok" if state == "completed" else "unavailable")
    assert value.artifact["kind"] == ("ok" if state == "completed" else "unavailable")


def test_preview_failure_does_not_erase_navigation_receipt(monkeypatch):
    monkeypatch.setattr(public_activity, "page_preview", lambda _: unavailable("outage"))
    value = browser.navigate_page.invoke(call(preview=True), config=config())
    assert value.artifact["kind"] == "ok" and value.artifact["receipts"]
    frame = json.loads(value.content)
    assert frame["status"] == "sent" and frame["preview"]["kind"] == "unavailable"
    assert frame["preview"]["data"] is None


def test_cancel_after_browser_completion_prevents_preview_io(monkeypatch):
    stop = Event()

    def completed(*args):
        stop.set()
        return {"status": "completed", "path": "/guestbook"}

    monkeypatch.setattr(browser, "_await_browser", completed)
    monkeypatch.setattr(public_activity, "page_preview", lambda _: pytest.fail("Read after cancel"))
    with request_scope(RequestControl(stop, time.monotonic() + 60)), pytest.raises(AgentCancelled):
        browser.navigate_page.invoke(call(preview=True),
            config=config(browser_feedback=True, browser_task={"id": "task"}))


def test_plain_navigation_never_reads_content(monkeypatch):
    def unexpected(_):
        pytest.fail("No preview was requested")

    monkeypatch.setattr(public_activity, "page_preview", unexpected)
    value = browser.navigate_page.invoke(call(), config=config())
    assert "preview" not in json.loads(value.content) and "data" not in value.artifact


def test_preview_rechecks_public_read_permission(monkeypatch):
    original = browser.authz.holds
    monkeypatch.setattr(browser.authz, "holds", lambda principal, scope:
                        scope != browser.authz.SCOPE_READ_PUBLIC and original(principal, scope))
    monkeypatch.setattr(public_activity, "page_preview", lambda _: pytest.fail("Unauthorized read"))
    value = browser.navigate_page.invoke(call(preview=True), config=config())
    assert json.loads(value.content)["preview"]["meta"]["reason"] == "preview_denied"


def test_preview_is_a_strict_boolean():
    with pytest.raises(ValidationError):
        browser.navigate_page.invoke(call(preview="true"), config=config())


@pytest.mark.parametrize("path", ["/about", "/dashboard/users", "/guestbook?lid=9", "/talk#item"])
def test_preview_never_substitutes_recent_rows_for_a_different_scope(monkeypatch, path):
    monkeypatch.setattr(public_activity, "_read", lambda *args: pytest.fail("Wrong source read"))
    result = public_activity.page_preview(path)
    assert result.kind == "unavailable" and result.meta["reason"] == "preview_not_supported"


@pytest.mark.parametrize("path,source", [("/guestbook", ("guestbook", "board")),
                                        ("/talk", ("talks", "talk"))])
def test_preview_uses_the_existing_bounded_public_reader(monkeypatch, path, source):
    seen = []
    monkeypatch.setattr(public_activity, "_read", lambda *args: seen.append(args) or preview())
    result = public_activity.page_preview(path)
    assert result.kind == "ok" and seen == [(*source, 1, 5, None)]
