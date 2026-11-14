"""Page reads and browser effects use separate native tool calls."""

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


def call(name, ident):
    return {"name": name, "args": {"path": "/guestbook"}, "id": ident, "type": "tool_call"}


def content():
    return ok("source", data={"resource": "activity/guestbook",
        "items": [{"id": 9, "content": "public source"}],
        "total": 12, "page": 1, "pageSize": 5, "hasMore": True})


@pytest.mark.parametrize("visit", [False, True])
def test_native_batch_reads_content_with_navigation_only_when_requested(monkeypatch, visit):
    reads = []
    monkeypatch.setattr(public_activity, "_read", lambda *args: reads.append(args) or content())
    calls = [call("read_public_page", "read")]
    if visit:
        calls.insert(0, call("navigate_page", "visit"))
    model = Capture(AIMessageChunk(content="", tool_calls=calls),
                    AIMessageChunk(content="The recent public source is available."))
    runtime = AgentRuntime(CheckpointConversationStore(InMemorySaver()),
                           application=BlogApplication(), model_factory=lambda **_: model)
    result = list(runtime.stream_events({"messages": [HumanMessage(content=
        "Visit and read the guestbook." if visit else "Read without changing the page.")]},
        config()))[-1]
    assert result.reply and len(model.prompts) == 2 and len(reads) == 1
    messages = [m for m in model.prompts[-1] if isinstance(m, ToolMessage)]
    reader = next(m for m in messages if m.name == "read_public_page")
    assert reader.artifact["data"]["resource"] == "activity/guestbook"
    assert not reader.artifact.get("receipts")
    if visit:
        navigation = next(m for m in messages if m.name == "navigate_page")
        assert json.loads(navigation.content)["status"] == "sent"
        assert navigation.artifact["receipts"][0]["cmd"]["kind"] == "navigate"
        assert "preview" not in navigation.content
    else:
        assert all(not m.artifact.get("receipts") for m in messages)


@pytest.mark.parametrize("status", ["completed", "failed", "timeout"])
def test_browser_checkpoint_resumes_the_separate_read_once_after_feedback(monkeypatch, status):
    from _browser_support import acknowledge

    reads = []
    monkeypatch.setattr(public_activity, "_read", lambda *args: reads.append(args) or content())
    model = Capture(AIMessageChunk(content="", tool_calls=[call("navigate_page", "visit"),
                                                          call("read_public_page", "read")]),
                    AIMessageChunk(content="Browser result and recent source received."))
    saver = InMemorySaver()

    def runtime():
        return AgentRuntime(CheckpointConversationStore(saver), application=BlogApplication(),
                            model_factory=lambda **_: model)

    cfg = config(browser_feedback=True)
    first = list(runtime().stream_events({"messages": [HumanMessage(content="Visit and read")],
                                         "task_id": "visit-read"}, cfg))[-1]
    command = first.execution["client_action"]
    assert not reads and len(model.prompts) == 1
    resumed = acknowledge(cfg, command, status, "/guestbook" if status == "completed" else "/")
    result = list(runtime().stream_events({"resume_task": "visit-read"}, resumed))[-1]
    assert result.reply and len(reads) == 1 and len(model.prompts) == 2
    messages = [m for m in model.prompts[-1] if isinstance(m, ToolMessage)]
    navigation = next(m for m in messages if m.name == "navigate_page")
    assert json.loads(navigation.content)["completion_confirmed"] is (status == "completed")
    assert any(m.name == "read_public_page" and m.artifact["kind"] == "ok" for m in messages)
    duplicate = list(runtime().stream_events({"resume_task": "visit-read"}, resumed))[-1]
    assert duplicate.execution["blocked"] and len(reads) == 1 and len(model.prompts) == 2


@pytest.mark.parametrize("state", ["completed", "timeout", "failed"])
def test_navigation_receipt_tracks_browser_feedback_and_never_reads(monkeypatch, state):
    monkeypatch.setattr(browser, "_await_browser", lambda *args: {"status": state,
                                                                "path": "/guestbook"})
    monkeypatch.setattr(public_activity, "_read", lambda *args: pytest.fail("Navigation read"))
    value = browser.navigate_page.invoke(call("navigate_page", "visit"),
        config=config(browser_feedback=True, browser_task={"id": "task"}))
    frame = json.loads(value.content)
    assert frame["completion_confirmed"] is (state == "completed")
    assert value.artifact["kind"] == ("ok" if state == "completed" else "unavailable")


def test_read_failure_does_not_claim_a_navigation_or_write(monkeypatch):
    monkeypatch.setattr(public_activity, "_read", lambda *args: unavailable("outage"))
    result = public_activity.read_public_page.invoke({"path": "/guestbook"}, config=config())
    assert result.kind == "unavailable" and result.data is None


def test_cancelled_request_does_not_start_public_read_io(monkeypatch):
    stop = Event()
    monkeypatch.setattr(public_activity, "_read", lambda *args: pytest.fail("Read after cancel"))
    with request_scope(RequestControl(stop, time.monotonic() + 60)):
        stop.set()
        with pytest.raises(AgentCancelled):
            public_activity.read_public_page.invoke({"path": "/guestbook"}, config=config())


def test_direct_read_rechecks_the_verified_public_scope(monkeypatch):
    monkeypatch.setattr(public_activity, "_read", lambda *args: pytest.fail("Unauthorized read"))
    result = public_activity.read_public_page.invoke({"path": "/talk"}, config={})
    assert result.kind == "unavailable"


@pytest.mark.parametrize("path", ["/about", "/dashboard/users", "/guestbook?lid=9", "/talk#item"])
def test_reader_never_substitutes_recent_rows_for_a_different_scope(monkeypatch, path):
    monkeypatch.setattr(public_activity, "_read", lambda *args: pytest.fail("Wrong source read"))
    with pytest.raises(ValidationError):
        public_activity.read_public_page.invoke({"path": path}, config=config())


@pytest.mark.parametrize("path,source", [("/guestbook", ("guestbook", "board")),
                                        ("/talk", ("talks", "talk"))])
def test_reader_uses_the_existing_bounded_public_source(monkeypatch, path, source):
    seen = []
    monkeypatch.setattr(public_activity, "_read", lambda *args: seen.append(args) or content())
    result = public_activity.read_public_page.invoke({"path": path}, config=config())
    assert result.kind == "ok" and seen == [(*source, 1, 5, None)]


@pytest.mark.parametrize("navigate,read", [(False, False), (True, False),
                                         (False, True), (True, True)])
def test_compound_acceptance_requires_both_effect_and_actual_content(navigate, read):
    from eval.golden_evaluation import check_gold
    from eval.online_baseline import load_suite

    case = next(c for c in load_suite() if c["id"] == "nav_guestbook_content")
    result = {"text": "Source received.", "commands": ["NAVIGATE:/guestbook"] if navigate else [],
              "tool_calls": ["read_public_page"] if read else [], "exec_rows": [],
              "native_events": [{"node": "native", "event": "llm_start"},
                *([{"node": "native", "event": "call", "name": "read_public_page",
                    "args": {"path": "/guestbook"}, "kind": "ok",
                    "result": json.dumps(content().data)}] if read else [])]}
    assert (not check_gold(case["gold"], result)) is (navigate and read)


@pytest.mark.parametrize("source,navigated,valid", [
    ("activity/guestbook", False, True), ("activity/talks", False, False),
    ("activity/guestbook", True, False)])
def test_guestbook_overview_accepts_source_evidence_without_allowing_navigation(
    source, navigated, valid,
):
    from eval.golden_evaluation import check_gold
    from eval.online_baseline import load_suite

    case = next(c for c in load_suite() if c["id"] == "guestbook")
    result = {"text": "Recent entries.", "commands": ["NAVIGATE:/guestbook"] if navigated else [],
              "tool_calls": [], "exec_rows": [], "native_events": [{"node": "native",
                  "event": "call", "name": "read_public_page", "kind": "ok",
                  "args": {"path": "/guestbook"}, "result": json.dumps({
                      "resource": source, "page": 1, "pageSize": 5})}]}
    assert (not check_gold(case["gold"], result)) is valid
