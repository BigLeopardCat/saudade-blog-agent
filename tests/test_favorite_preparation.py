"""Favorite preparation, confirmation and execution through actual tool boundaries."""


import asyncio
import json

import pytest
from _own_support import config
from _service import app
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from agent import confirm, idem
from agent.api.schemas import ChatRequest
from agent.domains.favorites import preparation
from agent.principal import Principal
from agent.runtime import writes
from agent.runtime.native import NativeRuntime
from agent.runtime.native_store import NativeConversationStore
from agent.runtime.results import TurnResult


class Model:
    def __init__(self, action="add"):
        self.calls, self.menus = [], []
        self.responses = [
            AIMessage(content="", tool_calls=[{"name": "load_favorites_info_tools",
                                              "args": {}, "id": "load"}]),
            AIMessage(content="", tool_calls=[{"name": "prepare_favorite_change",
                                              "args": {"action": action, "article_id": 19},
                                              "id": "prepare"}]),
            AIMessage(content="收藏操作已完成。"),
        ]

    def bind_tools(self, tools, **kwargs):
        self.menus.append(tools)
        return self

    def invoke(self, prompt):
        self.calls.append(list(prompt))
        return self.responses.pop(0)


def test_preparation_signs_real_target_without_writing_or_exposing_token(site):
    result = preparation.prepare_favorite_change.invoke(
        {"action": "add", "article_id": 19}, config=config())
    assert isinstance(result, TurnResult)
    popup = result.execution["pending_confirm"]
    grant = confirm.verify(popup["token"], 7, 42)
    assert grant["workflow"] == "favorite_add"
    assert grant["specs"] == [{"tool": "add_favorite", "args": {"article_id": 19}}]
    assert "真实文章标题" in popup["q"] and "真实文章标题" in result.reply
    assert popup["exp"] == grant["exp"]
    assert not result.execution["receipts"]
    assert all(request.method == "GET" for request in site["requests"])
    assert popup["token"] not in result.reply
    assert confirm.verify(popup["token"], 8, 42) is None
    assert confirm.verify(popup["token"], 7, 43) is None


@pytest.mark.parametrize("identifiers", [{"noteKey": 19}, {"noteId": 19},
                                       {"noteKey": 19, "noteId": 19}])
def test_public_article_and_favorite_dtos_produce_the_same_signed_target(site, identifiers):
    site["article"] = {**identifiers, "noteTitle": "真实文章标题"}
    result = preparation.prepare_favorite_change.invoke(
        {"action": "add", "article_id": 19}, config=config())
    grant = confirm.verify(result.execution["pending_confirm"]["token"], 7, 42)
    assert grant["specs"] == [{"tool": "add_favorite", "args": {"article_id": 19}}]
    assert all(request.method == "GET" for request in site["requests"])


@pytest.mark.parametrize("identifiers", [{}, {"noteKey": 20}, {"noteKey": True},
                                       {"noteKey": 19, "noteId": 20},
                                       {"noteKey": None, "noteId": 19}])
def test_invalid_or_conflicting_article_identifiers_do_not_create_a_card(site, identifiers):
    site["article"] = {**identifiers, "noteTitle": "真实文章标题"}
    result = preparation.prepare_favorite_change.invoke(
        {"action": "add", "article_id": 19}, config=config())
    assert result.kind == "unavailable"
    assert all(request.method == "GET" for request in site["requests"])


@pytest.mark.parametrize("action", ["add", "remove"])
def test_preparation_checkpoints_then_executes_once(site, monkeypatch, tmp_path, action):
    initial = ([{"noteId": 19, "title": "真实文章标题"}] if action == "remove" else [])
    site["favorites"] = initial
    model = Model(action)
    runtime = NativeRuntime(NativeConversationStore(InMemorySaver()),
                            model_factory=lambda **_: model)
    first = list(runtime.stream_events(
        {"messages": [HumanMessage(content="收藏文章 19")]}, config()))[-1]
    popup = first.execution["pending_confirm"]
    assert "未知状态" not in popup["q"]
    assert runtime.get_state(config()).values["pending_confirm"]["token"] == popup["token"]
    assert runtime.get_state(config()).next == ("approval",)
    assert runtime.get_state(config()).interrupts
    assert len(model.calls) == 2
    assert popup["token"] not in repr(model.calls)
    assert site["favorites"] == initial

    ledger = idem.Ledger(str(tmp_path / "writes.sqlite3"))
    monkeypatch.setattr(writes, "ledger_for", lambda saver: ledger)
    grant = confirm.verify(popup["token"], 7, 42)
    for _ in range(2):
        done = list(runtime.stream_events(
            {"messages": [HumanMessage(content="确认")], "confirm_grant": grant}, config()))
        expected = "add_favorite" if action == "add" else "remove_favorite"
        assert done[-1].execution["receipts"][0]["tool"] == expected
    method = "POST" if action == "add" else "DELETE"
    assert sum(request.method == method for request in site["requests"]) == 1
    assert len(model.calls) == 3
    assert runtime.get_state(config()).values["pending_action"] is None


@pytest.mark.parametrize("action,favorites", [
    ("add", [{"noteId": 19, "title": "真实文章标题"}]), ("remove", []),
])
def test_satisfied_state_does_not_create_a_card_or_write(site, action, favorites):
    site["favorites"] = favorites
    result = preparation.prepare_favorite_change.invoke(
        {"action": action, "article_id": 19}, config=config())
    assert isinstance(result, TurnResult)
    assert result.execution["noop_text"] == result.reply
    assert "pending_confirm" not in result.execution
    assert not result.execution["receipts"]
    assert all(request.method == "GET" for request in site["requests"])


@pytest.mark.parametrize("article_id", [True, "19", 0, -1])
def test_invalid_article_id_never_reaches_http(site, article_id):
    with pytest.raises(ValidationError):
        preparation.prepare_favorite_change.invoke(
            {"action": "add", "article_id": article_id}, config=config())
    assert not site["requests"]


@pytest.mark.parametrize("values", [
    {"principal": Principal(uid=7, role="user")},
    {"conversation_id": None}, {"thread_id": "conv_43"},
    {"user_id": 8},
])
def test_missing_binding_prevents_preparation_and_reads(site, values):
    result = preparation.prepare_favorite_change.invoke(
        {"action": "add", "article_id": 19}, config=config(**values))
    assert result.kind == "unavailable"
    assert not site["requests"]


def test_read_failure_never_becomes_an_empty_favorite_state(site):
    site["unavailable"] = True
    result = preparation.prepare_favorite_change.invoke(
        {"action": "remove", "article_id": 19}, config=config())
    assert result.kind == "unavailable"
    assert not isinstance(result, TurnResult)


def test_stream_emits_one_card_and_one_reply_from_current_page(site, monkeypatch):
    from agent.api.routes import chat as server
    from agent.runtime.native import NativeRuntime
    from agent.runtime.native_store import NativeConversationStore

    model = Model()
    monkeypatch.setattr(app.state, "runtime", NativeRuntime(
        NativeConversationStore(InMemorySaver()), model_factory=lambda **_: model))
    req = ChatRequest(message="收藏当前这篇", user_id=7, conversation_id=42,
                             current_url="/article/19", page_title="不可信页面标题")

    async def collect():
        queue = asyncio.Queue()
        producer = asyncio.create_task(asyncio.to_thread(
            server.stream_to_queue, app.state.runtime, server.build_messages(req), "conv_42", queue,
            asyncio.get_running_loop(), user_id=7,
            principal=config()["configurable"]["principal"], conversation_id=42))
        frames = []
        while (frame := await asyncio.wait_for(queue.get(), 5)) is not None:
            frames.append(frame)
        await producer
        return frames

    frames = asyncio.run(collect())
    cards = [json.loads(frame.removeprefix("__CONFIRM__:")) for frame in frames
             if isinstance(frame, str) and frame.startswith("__CONFIRM__:")]
    assert len(cards) == 1
    assert "真实文章标题" in cards[0]["q"]
    assert "不可信页面标题" not in cards[0]["q"]
    replies = [str(frame.content) for frame in frames if isinstance(frame, AIMessageChunk)]
    assert sum("确认后执行" in frame for frame in replies) == 1
    assert any(frame.startswith("__PENDING__:") for frame in frames if isinstance(frame, str))
    assert all(not frame.startswith("__EXEC__:") for frame in frames if isinstance(frame, str))
    assert "/article/19" not in model.calls[0][0].content
    assert any(isinstance(message, HumanMessage) and "/article/19" in str(message.content)
               for message in model.calls[0][1:])
    assert cards[0]["token"] not in repr(model.calls)
    assert not site["favorites"]
